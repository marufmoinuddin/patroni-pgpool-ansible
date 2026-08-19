#!/usr/bin/env python3
# ============================================================================
# bulk_failover_test.py — trigger a Patroni/pgpool failover (nothing else)
# ============================================================================
# PURPOSE
#   A focused failover trigger. It detects the current Patroni leader (or the
#   pgpool/VIP node), kills it, and reports what happened. That's all.
#
#   The realistic application behavior (firing purchases, measuring customer
#   impact) lives in tests/app_simulator.py. This script is ONLY the failover
#   injection — run it alongside the app simulator to see real failover.
#
# USAGE
#   # Kill the DB leader (crash):
#   python3 bulk_failover_test.py
#   # Graceful stop:
#   python3 bulk_failover_test.py --kill-mode stop
#   # Kill the pgpool/VIP node instead of the DB leader:
#   python3 bulk_failover_test.py --kill-target pgpool
#   # 3 sequential failovers:
#   python3 bulk_failover_test.py --kill-count 3
#   # Keep killed nodes down until the end, then restore:
#   python3 bulk_failover_test.py --kill-count 1 --keep-down
#
# REQUIREMENTS
#   SSH root access to the DB nodes (for leader detection + kill)
# ============================================================================

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ANSI color helpers (auto-disabled when not a TTY)
# ---------------------------------------------------------------------------
def _use_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class C:
    _on = _use_color()
    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    DIM = "\033[2m" if _on else ""
    RED = "\033[31m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    MAGENTA = "\033[35m" if _on else ""
    CYAN = "\033[36m" if _on else ""
    BOLD_RED = "\033[1;31m" if _on else ""
    BOLD_GREEN = "\033[1;32m" if _on else ""
    BOLD_YELLOW = "\033[1;33m" if _on else ""
    BOLD_CYAN = "\033[1;36m" if _on else ""
    BOLD_MAGENTA = "\033[1;35m" if _on else ""
    BG_RED = "\033[41m" if _on else ""
    BG_GREEN = "\033[42m" if _on else ""


def colorize(text, code):
    return f"{code}{text}{C.RESET}" if C._on else text


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Defaults (match the repo topology in hosts.ini / variables.yaml)
# ---------------------------------------------------------------------------
DEFAULT_VIP = "192.168.122.200"
DEFAULT_NODES = ["192.168.122.150", "192.168.122.151", "192.168.122.152"]
DEFAULT_SSH_USER = "root"
DEFAULT_PATRONI_CFG = "/etc/patroni/patroni.yml"
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
]


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------
def run_ssh(host, remote_cmd, ssh_user=DEFAULT_SSH_USER, timeout=25):
    cmd = ["ssh", *SSH_OPTS, f"{ssh_user}@{host}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "ssh timeout"


def find_leader(nodes, ssh_user, patroni_cfg):
    """Return (member, host_ip) of the current Patroni leader, or (None, None)."""
    for ip in nodes:
        rc, out, _ = run_ssh(ip, f"patronictl -c {patroni_cfg} list -f json 2>/dev/null", ssh_user)
        if rc == 0 and out:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                continue
            for m in data:
                if m.get("Role") == "Leader":
                    return m.get("Member"), m.get("Host")
    return None, None


def find_pgpool_vip_node(nodes, ssh_user, vip):
    """Return the IP of the node currently hosting the pgpool VIP."""
    for ip in nodes:
        rc, out, _ = run_ssh(ip, f"ip -4 addr show | grep -q '{vip}' && echo yes || echo no", ssh_user)
        if rc == 0 and "yes" in out:
            return ip
    return None


def kill_leader(leader_ip, mode, ssh_user, keep_down=False):
    """Force-stop Patroni on the leader. kill=SIGKILL (crash), stop=graceful.
    With keep_down=True the service is masked first so the node stays DOWN
    until restore_nodes() brings it back."""
    if keep_down:
        run_ssh(leader_ip, "systemctl mask patroni", ssh_user)
    if mode == "stop":
        cmd = "systemctl stop patroni"
    else:
        cmd = "systemctl kill -s SIGKILL patroni"
    return run_ssh(leader_ip, cmd, ssh_user)


def kill_pgpool(ip, ssh_user, keep_down=False):
    """Stop pgpool on the given node (triggers watchdog VIP failover).
    Returns ((rc, out, err), service_name)."""
    svc = "pgpool2"
    rc, _, _ = run_ssh(ip, f"systemctl is-active {svc}", ssh_user)
    if rc != 0:
        svc = "pgpool"
    if keep_down:
        run_ssh(ip, f"systemctl mask {svc}", ssh_user)
    return run_ssh(ip, f"systemctl stop {svc}", ssh_user), svc


def restore_nodes(killed, ssh_user):
    """Bring kept-down (masked) nodes back: unmask + start the killed service.
    killed is a list of (ip, service) pairs."""
    restored = []
    for ip, svc in killed:
        rc, out, err = run_ssh(ip, f"systemctl unmask {svc} && systemctl start {svc}", ssh_user)
        restored.append((ip, rc == 0))
    return restored


# ---------------------------------------------------------------------------
# Failover trigger
# ---------------------------------------------------------------------------
def trigger_failover(args, log):
    """Detect the target (leader or pgpool VIP node) and kill it.
    Returns a dict describing what happened."""
    kills_performed = 0
    killed = []            # (ip, service) kept down
    leader_sequence = []   # (member, host) before each kill
    leader_after = None

    for i in range(args.kill_count):
        if args.kill_target == "pgpool":
            ip = find_pgpool_vip_node(args.nodes, args.ssh_user, args.vip)
            member = "pgpool"
            leader_sequence.append((member, ip))
            if ip:
                log(f"KILL_TRIGGER #{i + 1} target=pgpool node={ip} mode={args.kill_mode}")
                (rc, out, err), svc = kill_pgpool(ip, args.ssh_user, keep_down=args.keep_down)
                kills_performed += 1
                killed.append((ip, svc))
                log(f"KILL_EXEC #{i + 1} rc={rc} {err or out}" + (" (kept down)" if args.keep_down else ""))
            else:
                log(f"KILL_TRIGGER #{i + 1} WARN: could not find pgpool VIP node")
        else:
            member, ip = find_leader(args.nodes, args.ssh_user, args.patroni_cfg)
            leader_sequence.append((member, ip))
            if ip:
                log(f"KILL_TRIGGER #{i + 1} leader={member}@{ip} mode={args.kill_mode}")
                rc, out, err = kill_leader(ip, args.kill_mode, args.ssh_user, keep_down=args.keep_down)
                kills_performed += 1
                killed.append((ip, "patroni"))
                log(f"KILL_EXEC #{i + 1} rc={rc} {err or out}" + (" (kept down)" if args.keep_down else ""))
            else:
                log(f"KILL_TRIGGER #{i + 1} WARN: could not resolve leader — no kill performed")

        # Wait for the failover to settle before the next kill (if any).
        if i < args.kill_count - 1:
            log(f"WAIT {args.settle_sec}s before next kill...")
            time.sleep(args.settle_sec)

    # Record the post-failover leader.
    member, ip = find_leader(args.nodes, args.ssh_user, args.patroni_cfg)
    leader_after = (member, ip)

    return {
        "kills_performed": kills_performed,
        "killed": killed,
        "leader_before": leader_sequence[0] if leader_sequence else None,
        "leader_after": leader_after,
        "leader_sequence": leader_sequence,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_report(args, result, log):
    seq = [lb for lb in result["leader_sequence"] if lb] + ([result["leader_after"]] if result["leader_after"] else [])
    failover_observed = bool(
        result["kills_performed"] and len(seq) >= 2 and any(a != b for a, b in zip(seq, seq[1:]))
    )

    lines = []
    lines.append("=" * 62)
    lines.append("FAILOVER TRIGGER REPORT")
    lines.append("=" * 62)
    lines.append(f"kill target       : {args.kill_target}")
    lines.append(f"kill mode         : {args.kill_mode}")
    lines.append(f"kills performed   : {result['kills_performed']} (requested {args.kill_count})")
    lines.append(f"keep-down mode    : {args.keep_down}")
    lines.append(f"leader before kill: {result['leader_before']}")
    lines.append(f"leader after      : {result['leader_after']}")
    if result["leader_sequence"]:
        seq_str = " -> ".join(m for m, _ in result["leader_sequence"])
        seq_str += f" -> {result['leader_after'][0]}" if result["leader_after"] else ""
        lines.append(f"leader sequence   : {seq_str}")
    lines.append(f"failover observed : {failover_observed}")
    lines.append("=" * 62)
    report_txt = "\n".join(lines)
    print("\n" + report_txt)
    log(f"REPORT kills={result['kills_performed']} failover_observed={failover_observed}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Trigger a Patroni/pgpool failover: detect the leader (or "
                    "pgpool VIP node), kill it, and report. Run alongside "
                    "tests/app_simulator.py to see real customer impact.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--vip", default=DEFAULT_VIP, help="pgpool Virtual IP")
    ap.add_argument("--kill-target", choices=["leader", "pgpool"], default="leader",
                    help="kill the DB leader or the pgpool/VIP node")
    ap.add_argument("--kill-mode", choices=["kill", "stop"], default="kill",
                    help="kill = systemctl kill -s SIGKILL patroni (crash); stop = graceful stop")
    ap.add_argument("--kill-count", type=int, default=1,
                    help="number of sequential failovers to trigger")
    ap.add_argument("--keep-down", action="store_true",
                    help="killed nodes stay DOWN (masked) until the end, then restored")
    ap.add_argument("--settle-sec", type=float, default=15.0,
                    help="seconds to wait between sequential kills")
    ap.add_argument("--nodes", nargs="+", default=DEFAULT_NODES,
                    help="DB node IPs (for SSH leader detection + kill)")
    ap.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help="SSH user for DB nodes")
    ap.add_argument("--patroni-cfg", default=DEFAULT_PATRONI_CFG, help="patroni.yml path on nodes")
    args = ap.parse_args()

    def log(msg):
        print(colorize(f"{utcnow()} {msg}", C.BOLD_RED if msg.startswith(("KILL", "RESTORE")) else C.CYAN), flush=True)

    print(colorize(f"{utcnow()} === failover trigger start target={args.kill_target} "
                   f"mode={args.kill_mode} count={args.kill_count} ===", C.BOLD_CYAN))

    result = trigger_failover(args, log)

    # Restore kept-down nodes after all kills.
    if args.keep_down and result["killed"]:
        log(f"RESTORE bringing {len(result['killed'])} kept-down node(s) back up...")
        for ip, ok in restore_nodes(result["killed"], args.ssh_user):
            log(f"RESTORE {ip} {'OK' if ok else 'FAILED'}")

    write_report(args, result, log)
    sys.exit(0)


if __name__ == "__main__":
    main()
