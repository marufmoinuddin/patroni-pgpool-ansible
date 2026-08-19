#!/usr/bin/env python3
# ============================================================================
# bulk_failover_test.py — 50 MB write + forced leader kill + data-loss check
# ============================================================================
# PURPOSE
#   Stress-test the Patroni + pgpool-II HA stack with a write workload
#   (default 50 MiB) that behaves like a REAL APPLICATION: users buy "packs"
#   (each purchase = a multi-statement transaction that locks the user's
#   balance, checks funds, deducts the price and inserts the purchase record).
#   The workload streams through the pgpool Virtual IP while the current
#   Patroni leader is force-stopped MID-WRITE. After Patroni elects a new
#   leader and pgpool re-attaches, purchases resume and the script verifies
#   that ZERO confirmed purchases were lost and the money is consistent.
#
# WHAT HAPPENS IF THE DB GOES DOWN DURING A PURCHASE?
#   A purchase is an ATOMIC transaction, so there are exactly two outcomes:
#     * DB down BEFORE commit  -> the transaction ROLLS BACK. The user is NOT
#       charged and no purchase record exists. The app sees an error and can
#       safely retry (no double charge, no lost money).
#     * DB down AFTER commit but BEFORE the app gets confirmation -> the
#       purchase IS committed (charged + recorded) but the app is UNSURE.
#       This is the "uncertain commit". The app must reconcile by querying
#       the DB (by purchase_id) instead of blindly retrying, otherwise it
#       would double-charge the user.
#   This script models both: confirmed purchases are tracked client-side,
#   failed ones are retried, and uncertain ones are detected on reconnect via
#   max(purchase_id) resync. The final verification proves every confirmed
#   purchase survived the failover and the user's balance matches exactly
#   (no lost charge, no double charge).
#
# METHOD (mirrors the proven txn_workload.sh methodology, in Python/psycopg2)
#   1. Connect to the cluster ONLY through the pgpool VIP (default
#      192.168.122.200:9999) as pgpool_admin.
#   2. Create pack_users(user_id, balance_cents) and
#      pack_purchases(purchase_id, user_id, pack_id, qty, price_cents,
#      client, payload BYTEA, payload_md5, ts). Seed one user with enough
#      balance for the whole run.
#   3. Each purchase is one transaction: SELECT ... FOR UPDATE on the user,
#      check funds, UPDATE balance, INSERT purchase (carrying a
#      payload_size-byte payload, default 1 MiB -> 50 MiB total), COMMIT.
#      Every COMMIT-confirmed purchase_id is recorded client-side.
#   4. When bytes_written reaches --kill-at-bytes (default 25 MiB = mid-write),
#      SSH to the current Patroni leader and force-stop it
#      (systemctl kill -s SIGKILL patroni, or --kill-mode stop).
#   5. On connection failure the script logs FAILED, rolls back, reconnects
#      with backoff until pgpool re-attaches the new leader, then RESYNCS
#      from max(purchase_id) (resolves the uncertain-commit window).
#   6. After the target bytes are written, VERIFY through the VIP:
#        - every client-confirmed purchase_id exists on the new primary
#        - payload integrity via stored md5 (optional, --verify-payload)
#        - total bytes written vs target
#        - money consistency: user balance == seed - (purchases in DB * price)
#        - uncertain commits (landed but never confirmed) reported separately
#
# USAGE
#   python3 bulk_failover_test.py                              # 50 MiB, auto-kill at 25 MiB
#   python3 bulk_failover_test.py --target-bytes 50M --payload-size 1M --kill-at-bytes 25M
#   python3 bulk_failover_test.py --scenario bulk              # raw inserts (original mode)
#   python3 bulk_failover_test.py --no-kill                    # kill the leader externally
#   python3 bulk_failover_test.py --kill-mode stop             # graceful systemctl stop
#   python3 bulk_failover_test.py --kill-count 3               # EXTREME: 3 sequential failovers in one run
#   python3 bulk_failover_test.py --kill-count 2 --keep-down   # EXTREME: killed leaders stay DOWN until the end
#   python3 bulk_failover_test.py --kill-target pgpool        # kill the pgpool/VIP node (VIP-failover read test)
#   python3 bulk_failover_test.py --verify-payload             # also re-read + md5 every row
#   python3 bulk_failover_test.py --read-interval 0.2          # concurrent read probe (default on)
#   python3 bulk_failover_test.py --clean                      # wipe old table data + artifact files
#
# ENV
#   PGPASSWORD   pgpool_admin password (else auto-fetched from a node's pool_passwd)
#
# REQUIREMENTS
#   pip install psycopg2-binary
#   SSH root access to the DB nodes (for auto-kill + password fetch)
#
# OPTIONAL PARALLEL OBSERVER (split-brain / routing monitoring, run in another
#   terminal): ./step4_observer.sh <artifact_dir> 720 2 192.168.122.200
# ============================================================================

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ANSI color helpers (auto-disabled when not a TTY)
# ---------------------------------------------------------------------------
def _use_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class C:
    """ANSI color codes. Colors are only emitted when stdout is a TTY."""
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
    WHITE = "\033[37m" if _on else ""
    BOLD_RED = "\033[1;31m" if _on else ""
    BOLD_GREEN = "\033[1;32m" if _on else ""
    BOLD_YELLOW = "\033[1;33m" if _on else ""
    BOLD_CYAN = "\033[1;36m" if _on else ""
    BOLD_MAGENTA = "\033[1;35m" if _on else ""
    BG_RED = "\033[41m" if _on else ""
    BG_GREEN = "\033[42m" if _on else ""
    BG_YELLOW = "\033[43m" if _on else ""


def colorize(text, code):
    """Wrap text in a color code + reset (no-op when colors are off)."""
    return f"{code}{text}{C.RESET}" if C._on else text

try:
    import psycopg2
    from psycopg2 import sql
    _PSYCOPG2_IMPORT_ERROR = None
except ImportError as _e:
    psycopg2 = None
    sql = None
    _PSYCOPG2_IMPORT_ERROR = _e

# ---------------------------------------------------------------------------
# Defaults (match the repo topology in hosts.ini / variables.yaml)
# ---------------------------------------------------------------------------
DEFAULT_VIP = "192.168.122.200"
DEFAULT_PORT = 9999
DEFAULT_DB = "postgres"
DEFAULT_USER = "pgpool_admin"
DEFAULT_NODES = ["192.168.122.150", "192.168.122.151", "192.168.122.152"]
DEFAULT_SSH_USER = "root"
DEFAULT_PATRONI_CFG = "/etc/patroni/patroni.yml"
DEFAULT_TABLE = "pack_purchases"       # purchase scenario table
DEFAULT_USERS_TABLE = "pack_users"     # user balances (purchase scenario)
DEFAULT_BULK_TABLE = "bulk_write_track"  # legacy bulk scenario table
DEFAULT_USER_ID = 1001
DEFAULT_PACK_ID = 42
DEFAULT_PRICE_CENTS = 1000             # $10.00 per pack
DEFAULT_QTY = 1
DEFAULT_TARGET_BYTES = 50 * 1024 ** 2  # 50 MiB
DEFAULT_PAYLOAD_SIZE = 1024 * 1024     # 1 MiB per row
DEFAULT_KILL_AT_BYTES = 25 * 1024 ** 2  # 25 MiB (mid-write)
DEFAULT_MAX_RECONNECT = 120            # ~10+ min of retries with backoff
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TiB"


def parse_size(s):
    """Parse human-readable sizes: '2G', '500M', '1048576', '1.5G'."""
    s = s.strip().upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1024, s[:-1]
    elif s.endswith("M"):
        mult, s = 1024 ** 2, s[:-1]
    elif s.endswith("G"):
        mult, s = 1024 ** 3, s[:-1]
    elif s.endswith("T"):
        mult, s = 1024 ** 4, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid size: {s!r}")


class Logger:
    """Timestamped event logger: mirrors to stdout and an events file.
    Console output is colorized by event type; the events file stays plain."""

    def __init__(self, events_path):
        self._fh = open(events_path, "a", buffering=1) if events_path else None

    def log(self, msg):
        line = f"{utcnow()} {msg}"
        print(self._colorize(line), flush=True)
        if self._fh:
            self._fh.write(line + "\n")

    def close(self):
        if self._fh:
            self._fh.close()

    @staticmethod
    def _colorize(line):
        """Apply a color to the whole line based on its leading event tag."""
        if not C._on:
            return line
        tag = line.split(" ", 2)[-1].split(" ", 1)[0] if " " in line else line
        # Match the event keyword that follows the timestamp.
        body = line.split(" ", 2)[-1] if " " in line else line
        if body.startswith("CONFIRMED"):
            return colorize(line, C.GREEN)
        if body.startswith("SEED"):
            return colorize(line, C.BLUE)
        if body.startswith(("KILL_TRIGGER", "KILL_EXEC", "RESTORE")):
            return colorize(line, C.BOLD_RED)
        if body.startswith("FAILED"):
            return colorize(line, C.RED)
        if body.startswith("RECONNECTED"):
            return colorize(line, C.CYAN)
        if body.startswith("RECONNECT_FAIL"):
            return colorize(line, C.YELLOW)
        if body.startswith("READ_PROBE FAIL"):
            return colorize(line, C.BOLD_YELLOW)
        if body.startswith("READ_MONITOR"):
            return colorize(line, C.MAGENTA)
        if body.startswith("UNCERTAIN"):
            return colorize(line, C.BOLD_MAGENTA)
        if body.startswith("INSUFFICIENT"):
            return colorize(line, C.YELLOW)
        if body.startswith("FATAL"):
            return colorize(line, C.BOLD_RED)
        if body.startswith("WARN"):
            return colorize(line, C.YELLOW)
        if body.startswith("INFO"):
            return colorize(line, C.CYAN)
        if body.startswith("CLEAN"):
            return colorize(line, C.BLUE)
        return line


# ---------------------------------------------------------------------------
# SSH helpers (auto-kill + password fetch)
# ---------------------------------------------------------------------------
def run_ssh(host, remote_cmd, ssh_user=DEFAULT_SSH_USER, timeout=25):
    cmd = ["ssh", *SSH_OPTS, f"{ssh_user}@{host}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "ssh timeout"


def fetch_pgpool_password(nodes, ssh_user):
    """Read the pgpool_admin password from a node's pool_passwd (distro-aware)."""
    for ip in nodes:
        for pf in ("/etc/pgpool2/pool_passwd", "/etc/pgpool-II/pool_passwd"):
            rc, out, _ = run_ssh(ip, f"grep '^pgpool_admin:' {pf} | cut -d: -f2", ssh_user)
            if rc == 0 and out:
                return out.splitlines()[0]
    return None


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


def kill_leader(leader_ip, mode, ssh_user, keep_down=False):
    """Force-stop Patroni on the leader. kill=SIGKILL (crash), stop=graceful.
    With keep_down=True the service is masked first so the node stays DOWN
    (even across a watchdog reboot) until restore_nodes() brings it back."""
    if keep_down:
        run_ssh(leader_ip, "systemctl mask patroni", ssh_user)
    if mode == "stop":
        cmd = "systemctl stop patroni"
    else:
        cmd = "systemctl kill -s SIGKILL patroni"
    return run_ssh(leader_ip, cmd, ssh_user)


def restore_nodes(killed, ssh_user):
    """Bring kept-down (masked) nodes back: unmask + start the killed service.
    killed is a list of (ip, service) pairs."""
    restored = []
    for ip, svc in killed:
        rc, out, err = run_ssh(ip, f"systemctl unmask {svc} && systemctl start {svc}", ssh_user)
        restored.append((ip, rc == 0))
    return restored


def find_pgpool_vip_node(nodes, ssh_user, vip):
    """Return the IP of the node currently hosting the pgpool VIP."""
    for ip in nodes:
        rc, out, _ = run_ssh(ip, f"ip -4 addr show | grep -q '{vip}' && echo yes || echo no", ssh_user)
        if rc == 0 and "yes" in out:
            return ip
    return None


def kill_pgpool(ip, ssh_user, keep_down=False):
    """Stop pgpool on the given node (triggers watchdog VIP failover to another node).
    Returns ((rc, out, err), service_name)."""
    svc = "pgpool2"
    rc, _, _ = run_ssh(ip, f"systemctl is-active {svc}", ssh_user)
    if rc != 0:
        svc = "pgpool"
    if keep_down:
        run_ssh(ip, f"systemctl mask {svc}", ssh_user)
    return run_ssh(ip, f"systemctl stop {svc}", ssh_user), svc


# ---------------------------------------------------------------------------
# DB helpers (all through the pgpool VIP)
# ---------------------------------------------------------------------------
def connect(args, client):
    return psycopg2.connect(
        host=args.vip,
        port=args.port,
        dbname=args.db,
        user=args.user,
        password=args.password,
        connect_timeout=5,          # fail FAST when the VIP/primary is down
        keepalives=1,
        keepalives_idle=5,
        keepalives_interval=2,
        keepalives_count=3,
        application_name=f"bulk_failover_{client}",
    )


def ensure_tables(conn, args):
    """Create the tables needed for the selected scenario."""
    with conn.cursor() as cur:
        if args.scenario == "purchase":
            cur.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ("
                    " user_id BIGINT PRIMARY KEY,"
                    " balance_cents BIGINT NOT NULL)"
                ).format(sql.Identifier(args.users_table))
            )
            cur.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ("
                    " purchase_id BIGINT PRIMARY KEY,"
                    " user_id BIGINT NOT NULL,"
                    " pack_id INT NOT NULL,"
                    " qty INT NOT NULL,"
                    " price_cents BIGINT NOT NULL,"
                    " client TEXT NOT NULL,"
                    " payload BYTEA NOT NULL,"
                    " payload_md5 TEXT NOT NULL,"
                    " ts TIMESTAMPTZ NOT NULL DEFAULT now())"
                ).format(sql.Identifier(args.table))
            )
        else:  # bulk
            cur.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} ("
                    " id BIGINT PRIMARY KEY,"
                    " client TEXT NOT NULL,"
                    " seq BIGINT NOT NULL,"
                    " payload BYTEA NOT NULL,"
                    " payload_md5 TEXT NOT NULL,"
                    " ts TIMESTAMPTZ NOT NULL DEFAULT now())"
                ).format(sql.Identifier(args.table))
            )


def seed_user(conn, args):
    """Give the test user enough balance for the whole run (purchase scenario).
    Returns the seeded balance in cents."""
    n_purchases = math.ceil(args.target_bytes / args.payload_size)
    cost = args.price_cents * args.qty
    seed_balance = n_purchases * cost + 10 ** 9   # margin so funds never run out
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {} (user_id, balance_cents) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET balance_cents = EXCLUDED.balance_cents"
            ).format(sql.Identifier(args.users_table)),
            (args.user_id, seed_balance),
        )
    return seed_balance


def get_max_id(conn, table, id_col="id"):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}").format(
                sql.Identifier(id_col), sql.Identifier(table)
            )
        )
        return cur.fetchone()[0]


def execute_unit(conn, args, client, unit_id, seq, payload, md5):
    """Execute one unit of work as a single transaction.
    purchase: lock user, check funds, deduct balance, insert purchase, commit.
    bulk:     insert one row, commit.
    Returns 'ok' or 'insufficient' (business failure, rolled back).
    Raises psycopg2.Error on DB failure (caller handles reconnect)."""
    if args.scenario == "purchase":
        cost = args.price_cents * args.qty
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT balance_cents FROM {} WHERE user_id = %s FOR UPDATE").format(
                    sql.Identifier(args.users_table)
                ),
                (args.user_id,),
            )
            row = cur.fetchone()
            if row is None or row[0] < cost:
                conn.rollback()
                return "insufficient"
            cur.execute(
                sql.SQL("UPDATE {} SET balance_cents = balance_cents - %s WHERE user_id = %s").format(
                    sql.Identifier(args.users_table)
                ),
                (cost, args.user_id),
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} (purchase_id, user_id, pack_id, qty, price_cents, client, payload, payload_md5) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(sql.Identifier(args.table)),
                (unit_id, args.user_id, args.pack_id, args.qty, args.price_cents, client, payload, md5),
            )
            conn.commit()
            return "ok"
    else:  # bulk
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "INSERT INTO {} (id, client, seq, payload, payload_md5) "
                    "VALUES (%s, %s, %s, %s, %s)"
                ).format(sql.Identifier(args.table)),
                (unit_id, client, seq, payload, md5),
            )
            conn.commit()
            return "ok"


def reconnect_loop(args, client, logger, max_attempts):
    """Reconnect to the VIP with exponential backoff until success."""
    attempt = 0
    delay = 1.0
    while attempt < max_attempts:
        attempt += 1
        try:
            conn = connect(args, client)
            logger.log(f"RECONNECTED attempt={attempt}")
            return conn, True
        except psycopg2.Error as e:
            logger.log(f"RECONNECT_FAIL attempt={attempt}: {str(e).splitlines()[0]}")
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
    return None, False


class ReadMonitor:
    """Concurrent read probe through the pgpool VIP.

    Proves the pitch-critical property: reads are NEVER disrupted during a
    leader failover (the only read outage would be if the VIP node itself
    goes down). Runs a lightweight SELECT 1 through the VIP on a loop and
    records every probe plus any failures, so the report can show a
    read-availability percentage."""

    def __init__(self, args, client, logger, interval=0.2):
        self.args = args
        self.client = client
        self.logger = logger
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.probes = []          # (ts, ok, err)
        self._conn = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="read-monitor")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _run(self):
        while not self._stop.is_set():
            ts = utcnow()
            ok, err = False, ""
            t0 = time.monotonic()
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = connect(self.args, self.client + "_reader")
                with self._conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                ok = True
            except psycopg2.Error as e:
                err = str(e).splitlines()[0]
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None
            latency_ms = (time.monotonic() - t0) * 1000.0
            with self._lock:
                self.probes.append((ts, ok, err, latency_ms))
            if not ok:
                self.logger.log(f"READ_PROBE FAIL {ts}: {err}")
            time.sleep(self.interval)

    def summary(self):
        with self._lock:
            total = len(self.probes)
            failed = sum(1 for _, ok, _, _ in self.probes if not ok)
            failures = [(ts, err) for ts, ok, err, _ in self.probes if not ok]
            latencies = [lat for _, ok, _, lat in self.probes if ok]
            # per-second success rate (second -> [total, failed])
            per_sec = {}
            for ts, ok, _, _ in self.probes:
                sec = ts[:19]   # YYYY-MM-DDTHH:MM:SS
                if sec not in per_sec:
                    per_sec[sec] = [0, 0]
                per_sec[sec][0] += 1
                if not ok:
                    per_sec[sec][1] += 1
            worst_secs = sorted(
                ((s, t, f) for s, (t, f) in per_sec.items() if f > 0),
                key=lambda x: -x[2],
            )[:10]
        avail = (total - failed) / total * 100 if total else 0.0
        avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
        max_lat = max(latencies) if latencies else 0.0
        return {
            "total_probes": total,
            "failed_probes": failed,
            "availability_pct": round(avail, 2),
            "avg_latency_ms": round(avg_lat, 2),
            "max_latency_ms": round(max_lat, 2),
            "failures": failures[:50],
            "worst_seconds": worst_secs,
        }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def clean_old_data(args, log):
    """Remove all data this script has written: truncate the tracking tables
    (purchases + users, plus the legacy bulk table) and delete its artifact
    files. Safe to run after an interrupted/killed run."""
    ok = True

    # 1. DB: truncate the tables this script writes to.
    tables = [args.table]
    if args.scenario == "purchase":
        tables.append(args.users_table)
    tables.append(DEFAULT_BULK_TABLE)   # legacy bulk_write_track from older runs
    try:
        conn = connect(args, "cleanup")
        conn.autocommit = True
        with conn.cursor() as cur:
            for t in dict.fromkeys(tables):   # dedupe, keep order
                cur.execute("SELECT to_regclass(%s)", (t,))
                if cur.fetchone()[0] is not None:
                    cur.execute(sql.SQL("TRUNCATE TABLE {}").format(sql.Identifier(t)))
                    log(f"CLEAN: truncated table {t}")
                else:
                    log(f"CLEAN: table {t} does not exist — nothing to truncate")
        conn.close()
    except psycopg2.Error as e:
        log(f"CLEAN: DB truncate FAILED: {str(e).splitlines()[0]}")
        ok = False

    # 2. Artifacts: remove this script's files from the artifact dir.
    removed = 0
    if os.path.isdir(args.artifact_dir):
        for fname in os.listdir(args.artifact_dir):
            if fname.startswith("bulk_") and fname.endswith(
                (".events", ".ids", ".summary.json", ".report.txt")
            ):
                try:
                    os.remove(os.path.join(args.artifact_dir, fname))
                    removed += 1
                except OSError:
                    pass
    log(f"CLEAN: removed {removed} artifact file(s) from {args.artifact_dir}")

    return ok


# ---------------------------------------------------------------------------
# Write phase
# ---------------------------------------------------------------------------
def write_phase(args, client, logger, start_id):
    conn = connect(args, client)
    conn.autocommit = False
    ensure_tables(conn, args)
    id_col = "purchase_id" if args.scenario == "purchase" else "id"
    unit = "purchase" if args.scenario == "purchase" else "row"

    confirmed = []          # ids where the client got commit confirmation
    failed = []             # (id, error) — never confirmed
    uncertain = []          # ids that landed in the DB but were never confirmed
    insufficient = []       # business failures (purchase only): not enough funds
    id_to_md5 = {}          # id -> md5 of payload (for optional integrity check)
    bytes_written = 0
    next_id = start_id + 1
    seq = 0
    kill_points = list(getattr(args, "kill_points", None) or [])
    kill_index = 0
    kills_performed = 0
    killed_ips = []        # (ip, service) kept down (--keep-down) until the test ends
    leader_sequence = []   # (member, host) of the leader before each kill
    leader_after = None

    while bytes_written < args.target_bytes:
        # --- auto-kill trigger (fires at each kill point, mid-write) -------
        if kill_index < len(kill_points) and bytes_written >= kill_points[kill_index]:
            if args.kill_target == "pgpool":
                # Kill the pgpool/VIP node: the watchdog should move the VIP to
                # another pgpool node. Tests VIP-failover read disruption.
                ip = find_pgpool_vip_node(args.nodes, args.ssh_user, args.vip)
                member = "pgpool"
                leader_sequence.append((member, ip))
                if ip:
                    logger.log(
                        f"KILL_TRIGGER #{kill_index + 1} bytes={bytes_written} ({human_bytes(bytes_written)}) "
                        f"target=pgpool node={ip} mode=stop"
                    )
                    (rc, out, err), svc = kill_pgpool(ip, args.ssh_user, keep_down=args.keep_down)
                    kills_performed += 1
                    killed_ips.append((ip, svc))
                    logger.log(f"KILL_EXEC #{kill_index + 1} rc={rc} {err or out}"
                               + (" (kept down)" if args.keep_down else ""))
                else:
                    logger.log(f"KILL_TRIGGER #{kill_index + 1} WARN: could not find pgpool VIP node")
            else:
                member, ip = find_leader(args.nodes, args.ssh_user, args.patroni_cfg)
                leader_sequence.append((member, ip))
                if ip:
                    logger.log(
                        f"KILL_TRIGGER #{kill_index + 1} bytes={bytes_written} ({human_bytes(bytes_written)}) "
                        f"leader={member}@{ip} mode={args.kill_mode}"
                    )
                    rc, out, err = kill_leader(ip, args.kill_mode, args.ssh_user, keep_down=args.keep_down)
                    kills_performed += 1
                    killed_ips.append((ip, "patroni"))
                    logger.log(f"KILL_EXEC #{kill_index + 1} rc={rc} {err or out}"
                               + (" (kept down)" if args.keep_down else ""))
                else:
                    logger.log(f"KILL_TRIGGER #{kill_index + 1} WARN: could not resolve leader — no kill performed")
            kill_index += 1

        payload = os.urandom(args.payload_size)
        md5 = hashlib.md5(payload).hexdigest()
        try:
            status = execute_unit(conn, args, client, next_id, seq, payload, md5)
            if status == "ok":
                confirmed.append(next_id)
                id_to_md5[next_id] = md5
                bytes_written += len(payload)
                seq += 1
                next_id += 1
                if seq % args.log_every == 0:
                    logger.log(f"CONFIRMED {unit}_id={next_id - 1} {unit}s={seq} bytes={bytes_written} ({human_bytes(bytes_written)})")
            else:  # insufficient funds — business failure, not a DB failure
                insufficient.append(next_id)
                logger.log(f"INSUFFICIENT_FUNDS {unit}_id={next_id} (rolled back, no charge)")
                next_id += 1
        except psycopg2.Error as e:
            errmsg = str(e).splitlines()[0]
            logger.log(f"FAILED {unit}_id={next_id}: {errmsg}")
            failed.append((next_id, errmsg))
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            conn, ok = reconnect_loop(args, client, logger, args.max_reconnect)
            if not ok:
                logger.log("FATAL: reconnect exhausted — aborting write phase")
                break
            conn.autocommit = False
            # Resync from table max to resolve the uncertain-commit window:
            # if the pending purchase actually committed before the connection
            # died, max(id) >= next_id and we must NOT retry that id (the
            # balance was already deducted — retrying would double-charge).
            new_max = get_max_id(conn, args.table, id_col)
            if new_max >= next_id:
                uncertain.append(next_id)
                logger.log(f"UNCERTAIN_COMMITTED {unit}_id={next_id} (landed but unconfirmed) table_max={new_max}")
                next_id = new_max + 1
            # else: the pending purchase did not commit; retry the same id

        if args.pacing > 0:
            time.sleep(args.pacing)

    # Record the post-failover leader (may be unreachable if still rebooting).
    member, ip = find_leader(args.nodes, args.ssh_user, args.patroni_cfg)
    leader_after = (member, ip)

    return {
        "conn": conn,
        "confirmed": confirmed,
        "failed": failed,
        "uncertain": uncertain,
        "insufficient": insufficient,
        "id_to_md5": id_to_md5,
        "bytes_written": bytes_written,
        "rows_written": seq,
        "kill_performed": kills_performed > 0,
        "kills_performed": kills_performed,
        "killed_ips": killed_ips,
        "leader_before": leader_sequence[0] if leader_sequence else None,
        "leader_after": leader_after,
        "leader_sequence": leader_sequence,
    }


# ---------------------------------------------------------------------------
# Verification phase (through the VIP, i.e. what the application sees)
# ---------------------------------------------------------------------------
def verify_phase(args, client, logger, result):
    conn = result.get("conn")
    confirmed = result["confirmed"]
    id_to_md5 = result["id_to_md5"]
    id_col = "purchase_id" if args.scenario == "purchase" else "id"
    unit = "purchase" if args.scenario == "purchase" else "row"

    # Make sure we have a live connection (post-failover).
    if conn is None:
        conn, ok = reconnect_loop(args, client, logger, args.max_reconnect)
        if not ok:
            logger.log("FATAL: cannot reconnect for verification")
            return None
        conn.autocommit = False
    else:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg2.Error:
            conn, ok = reconnect_loop(args, client, logger, args.max_reconnect)
            if not ok:
                logger.log("FATAL: cannot reconnect for verification")
                return None
            conn.autocommit = False

    # 1. Presence + byte count for THIS client's rows.
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT {}, payload_md5, octet_length(payload) "
                "FROM {} WHERE client = %s ORDER BY {}"
            ).format(sql.Identifier(id_col), sql.Identifier(args.table), sql.Identifier(id_col)),
            (client,),
        )
        db_ids = set()
        db_bytes = 0
        db_rows = 0
        for rid, _rmd5, rsize in cur:
            db_ids.add(rid)
            db_bytes += rsize
            db_rows += 1

    confirmed_set = set(confirmed)
    lost = sorted(confirmed_set - db_ids)   # CONFIRMED but missing = DATA LOSS
    extra = sorted(db_ids - confirmed_set)  # in DB but never confirmed

    # 2. Money consistency (purchase scenario): the user's balance must equal
    #    seed - (purchases in DB * price). Any mismatch = lost or double charge.
    balance_ok = None
    balance_current = None
    balance_expected = None
    if args.scenario == "purchase":
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT balance_cents FROM {} WHERE user_id = %s").format(
                    sql.Identifier(args.users_table)
                ),
                (args.user_id,),
            )
            row = cur.fetchone()
            balance_current = row[0] if row else None
        cost = args.price_cents * args.qty
        balance_expected = args.seed_balance - db_rows * cost
        balance_ok = (balance_current == balance_expected)

    # 3. Payload integrity (optional: re-reads ~2 GB through the VIP).
    integrity_fail = []
    if args.verify_payload:
        logger.log("VERIFY_PAYLOAD re-reading all rows for md5 integrity check...")
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT {}, payload FROM {} WHERE client = %s ORDER BY {}").format(
                    sql.Identifier(id_col), sql.Identifier(args.table), sql.Identifier(id_col)
                ),
                (client,),
            )
            for rid, rpayload in cur:
                if hashlib.md5(rpayload).hexdigest() != id_to_md5.get(rid):
                    integrity_fail.append(rid)
        logger.log(f"VERIFY_PAYLOAD done: {len(integrity_fail)} integrity failures")

    return {
        "db_ids": db_ids,
        "db_bytes": db_bytes,
        "db_rows": db_rows,
        "lost": lost,
        "extra": extra,
        "integrity_fail": integrity_fail,
        "balance_ok": balance_ok,
        "balance_current": balance_current,
        "balance_expected": balance_expected,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def build_report(args, client, result, verify, read_summary=None):
    confirmed = result["confirmed"]
    failed = result["failed"]
    uncertain = result["uncertain"]
    insufficient = result.get("insufficient", [])
    kill_performed = result["kill_performed"]
    leader_before = result["leader_before"]
    leader_after = result["leader_after"]
    unit = "purchase" if args.scenario == "purchase" else "row"

    kills_performed = result.get("kills_performed", 1 if kill_performed else 0)
    leader_sequence = result.get("leader_sequence", [leader_before] if leader_before else [])

    # failover observed = any leader identity change across the kill sequence.
    seq = [lb for lb in leader_sequence if lb] + ([leader_after] if leader_after else [])
    failover_observed = bool(
        kill_performed and len(seq) >= 2 and any(a != b for a, b in zip(seq, seq[1:]))
    )

    if verify is None:
        lost, extra, integrity_fail = [], [], []
        db_bytes, db_rows = 0, 0
        balance_ok, balance_current, balance_expected = None, None, None
    else:
        lost = verify["lost"]
        extra = verify["extra"]
        integrity_fail = verify["integrity_fail"]
        db_bytes = verify["db_bytes"]
        db_rows = verify["db_rows"]
        balance_ok = verify["balance_ok"]
        balance_current = verify["balance_current"]
        balance_expected = verify["balance_expected"]

    data_ok = (
        len(lost) == 0
        and len(integrity_fail) == 0
        and db_bytes >= args.target_bytes
        and (balance_ok is None or balance_ok)   # money check only in purchase mode
    )
    verdict = "PASS" if data_ok else "FAIL"

    return {
        "client": client,
        "vip": f"{args.vip}:{args.port}",
        "scenario": args.scenario,
        "table": args.table,
        "target_bytes": args.target_bytes,
        "payload_size": args.payload_size,
        "kill_mode": args.kill_mode,
        "kill_at_bytes": args.kill_at_bytes,
        "kill_count": args.kill_count,
        "keep_down": args.keep_down,
        "kill_target": args.kill_target,
        "kill_performed": kill_performed,
        "kills_performed": kills_performed,
        "leader_before": {"member": leader_before[0], "host": leader_before[1]} if leader_before else None,
        "leader_after": {"member": leader_after[0], "host": leader_after[1]} if leader_after else None,
        "leader_sequence": [{"member": m, "host": h} for m, h in leader_sequence if m],
        "failover_observed": failover_observed,
        "rows_written": result["rows_written"],
        "bytes_written": result["bytes_written"],
        "confirmed_count": len(confirmed),
        "failed_count": len(failed),
        "uncertain_count": len(uncertain),
        "insufficient_count": len(insufficient),
        "verification": {
            "rows_in_db": db_rows,
            "bytes_in_db": db_bytes,
            "lost_ids": lost,
            "lost_count": len(lost),
            "extra_ids": extra,
            "extra_count": len(extra),
            "integrity_fail_ids": integrity_fail,
            "integrity_fail_count": len(integrity_fail),
            "balance_ok": balance_ok,
            "balance_current_cents": balance_current,
            "balance_expected_cents": balance_expected,
        },
        "read_monitor": read_summary,
        "verdict": verdict,
    }


def write_report(args, client, report, logger):
    base = os.path.join(args.artifact_dir, f"bulk_{client}")

    with open(base + ".summary.json", "w") as fh:
        json.dump(report, fh, indent=2)

    r = report
    v = r["verification"]
    unit = "purchase" if r["scenario"] == "purchase" else "row"
    verdict = r["verdict"]
    is_pass = verdict == "PASS"

    # --- helpers to build a line both plain and colorized -------------------
    def kv(label, value, value_color=None):
        plain = f"{label:<22}: {value}"
        if C._on and value_color:
            return plain, colorize(f"{label:<22}: ", C.DIM) + colorize(str(value), value_color)
        return plain, plain

    def section(title):
        bar = "-" * 62
        plain = f"{bar}\n{title}\n{bar}"
        if C._on:
            return plain, colorize(bar, C.DIM) + "\n" + colorize(title, C.BOLD_CYAN) + "\n" + colorize(bar, C.DIM)
        return plain, plain

    plain_lines = []
    color_lines = []

    # --- header banner ------------------------------------------------------
    bar = "=" * 62
    title = "BULK FAILOVER TEST REPORT"
    plain_lines += [bar, title, bar]
    if C._on:
        color_lines += [
            colorize(bar, C.BOLD_CYAN),
            colorize(title, C.BOLD + C.BOLD_CYAN),
            colorize(bar, C.BOLD_CYAN),
        ]
    else:
        color_lines += [bar, title, bar]

    # --- run config ---------------------------------------------------------
    p, c = section("RUN CONFIG")
    plain_lines.append(p); color_lines.append(c)
    for label, val in [
        ("client", r["client"]),
        ("vip", r["vip"]),
        ("scenario", r["scenario"]),
        ("table", r["table"]),
        ("target bytes", f"{r['target_bytes']} ({human_bytes(r['target_bytes'])})"),
        ("payload size", f"{r['payload_size']} ({human_bytes(r['payload_size'])})"),
        ("kill mode", f"{r['kill_mode']}  kill_at={r['kill_at_bytes']} ({human_bytes(r['kill_at_bytes'] or 0)})"),
        ("kill target", r.get("kill_target", "leader")),
        ("kills performed", f"{r['kills_performed']} (requested {r['kill_count']})"),
        ("keep-down mode", r.get("keep_down", False)),
    ]:
        p, c = kv(label, val)
        plain_lines.append(p); color_lines.append(c)

    # --- failover / leader info --------------------------------------------
    p, c = section("FAILOVER")
    plain_lines.append(p); color_lines.append(c)
    for label, val in [
        ("leader before kill", r["leader_before"]),
        ("leader after", r["leader_after"]),
        ("failover observed", r["failover_observed"]),
    ]:
        p, c = kv(label, val, C.BOLD_GREEN if val is True else (C.BOLD_RED if val is False else None))
        plain_lines.append(p); color_lines.append(c)
    if r.get("leader_sequence"):
        seq_str = " -> ".join(m["member"] for m in r["leader_sequence"])
        seq_str += f" -> {r['leader_after']['member']}" if r.get("leader_after") else ""
        p, c = kv("leader sequence", seq_str, C.BOLD_MAGENTA)
        plain_lines.append(p); color_lines.append(c)

    # --- write results ------------------------------------------------------
    p, c = section("WRITE RESULTS")
    plain_lines.append(p); color_lines.append(c)
    for label, val in [
        (f"{unit}s written", r["rows_written"]),
        ("bytes written", f"{r['bytes_written']} ({human_bytes(r['bytes_written'])})"),
        (f"confirmed {unit}s", r["confirmed_count"]),
        ("failed (unconfirmed)", r["failed_count"]),
        ("uncertain commits", r["uncertain_count"]),
        ("insufficient funds", r["insufficient_count"]),
    ]:
        p, c = kv(label, val)
        plain_lines.append(p); color_lines.append(c)

    # --- verification -------------------------------------------------------
    p, c = section("VERIFICATION (via pgpool VIP)")
    plain_lines.append(p); color_lines.append(c)
    for label, val in [
        (f"{unit}s in DB", v["rows_in_db"]),
        ("bytes in DB", f"{v['bytes_in_db']} ({human_bytes(v['bytes_in_db'])})"),
    ]:
        p, c = kv(label, val)
        plain_lines.append(p); color_lines.append(c)

    lost = v["lost_count"]
    p, c = kv("LOST (confirmed but missing)", lost, C.BOLD_RED if lost else C.BOLD_GREEN)
    plain_lines.append(p); color_lines.append(c)
    if v["lost_ids"]:
        p, c = kv("  lost ids", v["lost_ids"][:20])
        plain_lines.append(p); color_lines.append(c)

    extra = v["extra_count"]
    p, c = kv("EXTRA (in DB, unconfirmed)", extra, C.BOLD_YELLOW if extra else C.BOLD_GREEN)
    plain_lines.append(p); color_lines.append(c)
    if v["extra_ids"]:
        p, c = kv("  extra ids", v["extra_ids"][:20])
        plain_lines.append(p); color_lines.append(c)

    ifail = v["integrity_fail_count"]
    p, c = kv("integrity failures", ifail, C.BOLD_RED if ifail else C.BOLD_GREEN)
    plain_lines.append(p); color_lines.append(c)

    if v["balance_ok"] is not None:
        bok = v["balance_ok"]
        p, c = kv("balance check", f"{'OK' if bok else 'MISMATCH'} "
                   f"(current={v['balance_current_cents']} expected={v['balance_expected_cents']})",
                   C.BOLD_GREEN if bok else C.BOLD_RED)
        plain_lines.append(p); color_lines.append(c)

    # --- read monitor -------------------------------------------------------
    if r.get("read_monitor"):
        rm = r["read_monitor"]
        p, c = section("READ MONITOR (concurrent probe through VIP)")
        plain_lines.append(p); color_lines.append(c)
        for label, val in [
            ("read probes", f"{rm['total_probes']} total, {rm['failed_probes']} failed"),
            ("read availability", f"{rm['availability_pct']}%"),
            ("read latency", f"avg {rm['avg_latency_ms']}ms, max {rm['max_latency_ms']}ms"),
        ]:
            p, c = kv(label, val, C.BOLD_GREEN if rm['availability_pct'] >= 99 else C.BOLD_YELLOW)
            plain_lines.append(p); color_lines.append(c)
        if rm["failures"]:
            p, c = kv("  read outage windows", rm["failures"][:5])
            plain_lines.append(p); color_lines.append(c)
        if rm.get("worst_seconds"):
            p, c = kv("  worst seconds (failed/total)",
                      ", ".join(f"{s} {f}/{t}" for s, t, f in rm["worst_seconds"][:5]))
            plain_lines.append(p); color_lines.append(c)

    # --- verdict banner -----------------------------------------------------
    plain_lines.append("=" * 62)
    plain_lines.append(f"RESULT: {verdict}")
    plain_lines.append("=" * 62)
    if C._on:
        vcolor = C.BG_GREEN + C.BOLD if is_pass else C.BG_RED + C.BOLD
        color_lines.append(colorize("=" * 62, C.DIM))
        color_lines.append(colorize(f"  RESULT: {verdict}  ", vcolor))
        color_lines.append(colorize("=" * 62, C.DIM))
    else:
        color_lines.append("=" * 62)
        color_lines.append(f"RESULT: {verdict}")
        color_lines.append("=" * 62)

    report_txt = "\n".join(plain_lines)
    console_txt = "\n".join(color_lines)
    print("\n" + console_txt)
    with open(base + ".report.txt", "w") as fh:
        fh.write(report_txt + "\n")
    logger.log(f"REPORT verdict={r['verdict']} lost={v['lost_count']} extra={v['extra_count']} "
               f"integrity_fail={v['integrity_fail_count']} bytes_db={v['bytes_in_db']} "
               f"balance_ok={v['balance_ok']} failover_observed={r['failover_observed']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Write 2 GB through the pgpool VIP, force-stop the leader mid-write, "
                    "resume after failover, and verify zero data loss.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--vip", default=DEFAULT_VIP, help="pgpool Virtual IP")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="pgpool port")
    ap.add_argument("--db", default=DEFAULT_DB, help="database name")
    ap.add_argument("--user", default=DEFAULT_USER, help="DB user (via VIP)")
    ap.add_argument("--password", default=os.environ.get("PGPASSWORD", ""),
                    help="DB password (default: PGPASSWORD env, else auto-fetch from pool_passwd)")
    ap.add_argument("--table", default=DEFAULT_TABLE, help="tracking table name")
    ap.add_argument("--scenario", choices=["purchase", "bulk"], default="purchase",
                    help="purchase = realistic pack-buying app (default); bulk = raw inserts")
    ap.add_argument("--users-table", default=DEFAULT_USERS_TABLE,
                    help="user balances table (purchase scenario)")
    ap.add_argument("--user-id", type=int, default=DEFAULT_USER_ID,
                    help="buyer user id (purchase scenario)")
    ap.add_argument("--pack-id", type=int, default=DEFAULT_PACK_ID,
                    help="pack id being bought (purchase scenario)")
    ap.add_argument("--price-cents", type=int, default=DEFAULT_PRICE_CENTS,
                    help="price per pack in cents (purchase scenario)")
    ap.add_argument("--qty", type=int, default=DEFAULT_QTY,
                    help="packs per purchase (purchase scenario)")
    ap.add_argument("--client", default=None, help="unique client id (default: bulk_<timestamp>)")
    ap.add_argument("--target-bytes", type=parse_size, default=DEFAULT_TARGET_BYTES,
                    help="total bytes to write (default 50M, e.g. 500M, 2G)")
    ap.add_argument("--payload-size", type=parse_size, default=DEFAULT_PAYLOAD_SIZE,
                    help="bytes per row (e.g. 1M)")
    ap.add_argument("--kill-at-bytes", type=parse_size, default=DEFAULT_KILL_AT_BYTES,
                    help="auto-kill the leader after this many bytes (0 = kill at start)")
    ap.add_argument("--kill-count", type=int, default=1,
                    help="EXTREME: number of sequential leader kills during the write "
                         "(kills are evenly spaced; each triggers a failover)")
    ap.add_argument("--keep-down", action="store_true",
                    help="EXTREME: killed leaders stay DOWN (masked) until the write "
                         "finishes, then all are restored (cascade node-loss test)")
    ap.add_argument("--no-kill", action="store_true",
                    help="do NOT auto-kill; stop the leader externally while this runs")
    ap.add_argument("--kill-mode", choices=["kill", "stop"], default="kill",
                    help="kill = systemctl kill -s SIGKILL patroni (crash); stop = graceful stop")
    ap.add_argument("--kill-target", choices=["leader", "pgpool"], default="leader",
                    help="what to kill at each kill point: leader = Patroni DB leader; "
                         "pgpool = the pgpool/VIP node (tests VIP-failover read disruption)")
    ap.add_argument("--nodes", nargs="+", default=DEFAULT_NODES,
                    help="DB node IPs (for SSH leader detection + kill)")
    ap.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help="SSH user for DB nodes")
    ap.add_argument("--patroni-cfg", default=DEFAULT_PATRONI_CFG, help="patroni.yml path on nodes")
    ap.add_argument("--artifact-dir", default="artifacts", help="where events/report are written")
    ap.add_argument("--max-reconnect", type=int, default=DEFAULT_MAX_RECONNECT,
                    help="max reconnect attempts during the outage window")
    ap.add_argument("--pacing", type=float, default=0.0,
                    help="seconds to sleep between rows (slow the write down)")
    ap.add_argument("--log-every", type=int, default=1,
                    help="log every Nth CONFIRMED row")
    ap.add_argument("--verify-payload", action="store_true",
                    help="re-read every row and verify md5 (re-transfers ~2 GB)")
    ap.add_argument("--read-monitor", dest="read_monitor", action="store_true", default=True,
                    help="run a concurrent read probe through the VIP to prove reads are "
                         "never disrupted during failover (default: on)")
    ap.add_argument("--no-read-monitor", dest="read_monitor", action="store_false",
                    help="disable the concurrent read probe")
    ap.add_argument("--read-interval", type=float, default=0.2,
                    help="seconds between read probes (read monitor)")
    ap.add_argument("--clean", action="store_true",
                    help="wipe old data written by this script (truncate tracking table + "
                         "remove artifact files), then exit")
    args = ap.parse_args()

    if _PSYCOPG2_IMPORT_ERROR:
        sys.stderr.write("ERROR: psycopg2 is required. Install with: pip install psycopg2-binary\n")
        sys.exit(2)

    if args.scenario == "bulk" and args.table == DEFAULT_TABLE:
        args.table = DEFAULT_BULK_TABLE   # bulk mode uses the legacy table by default

    if args.no_kill:
        args.kill_at_bytes = None
    elif args.kill_at_bytes is not None and args.kill_at_bytes >= args.target_bytes:
        print(colorize(f"WARN: --kill-at-bytes ({args.kill_at_bytes}) >= --target-bytes "
              f"({args.target_bytes}); forcing kill at {args.target_bytes // 2} "
              f"so the failover happens mid-write.", C.BOLD_YELLOW))
        args.kill_at_bytes = args.target_bytes // 2

    # Compute the kill points (byte offsets where the leader is killed).
    # kill_count=1 -> single kill at --kill-at-bytes (default mid-write).
    # kill_count>1 -> EXTREME: evenly spaced kills across the whole write.
    if args.no_kill:
        args.kill_points = []
    elif args.kill_count <= 1:
        args.kill_points = [args.kill_at_bytes] if args.kill_at_bytes is not None else []
    else:
        args.kill_points = [
            int(args.target_bytes * i / (args.kill_count + 1))
            for i in range(1, args.kill_count + 1)
        ]
        print(colorize(f"INFO: EXTREME test with {args.kill_count} sequential kills at "
              f"{', '.join(human_bytes(p) for p in args.kill_points)}", C.BOLD_CYAN))

    if args.keep_down and args.kill_count >= len(args.nodes) - 1:
        print(colorize(f"WARN: --keep-down with --kill-count {args.kill_count} on {len(args.nodes)} nodes "
              f"may lose etcd quorum (need >= 2 nodes up); reconnects may fail.", C.BOLD_YELLOW))

    if not args.password:
        pw = fetch_pgpool_password(args.nodes, args.ssh_user)
        if pw:
            args.password = pw
            print(colorize(f"INFO: pgpool_admin password auto-fetched from pool_passwd", C.CYAN))
        else:
            sys.stderr.write(
                "ERROR: no DB password. Set PGPASSWORD or --password "
                "(auto-fetch from pool_passwd failed).\n"
            )
            sys.exit(2)

    if args.clean:
        # Standalone clean mode: wipe old data, then exit (no artifact files created).
        print(colorize(f"{utcnow()} === bulk_failover_test CLEAN mode ===", C.BOLD_BLUE if hasattr(C, 'BOLD_BLUE') else C.BOLD_CYAN))
        ok = clean_old_data(args, lambda m: print(f"{utcnow()} {m}"))
        sys.exit(0 if ok else 1)

    client = args.client or f"bulk_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    os.makedirs(args.artifact_dir, exist_ok=True)
    logger = Logger(os.path.join(args.artifact_dir, f"bulk_{client}.events"))

    logger.log(
        f"=== bulk_failover_test start client={client} vip={args.vip}:{args.port} "
        f"target={args.target_bytes} ({human_bytes(args.target_bytes)}) "
        f"payload={args.payload_size} kill_at={args.kill_at_bytes} mode={args.kill_mode} ==="
    )

    # Pre-flight: connect through the VIP, create tables, seed user + max(id).
    conn = connect(args, client)
    conn.autocommit = False
    ensure_tables(conn, args)
    args.seed_balance = None
    if args.scenario == "purchase":
        args.seed_balance = seed_user(conn, args)
        logger.log(f"SEED user_id={args.user_id} balance_cents={args.seed_balance}")
    id_col = "purchase_id" if args.scenario == "purchase" else "id"
    start_id = get_max_id(conn, args.table, id_col)
    logger.log(f"SEED max({id_col})={start_id}")
    conn.commit()   # persist the CREATE TABLE + user seed before closing
    conn.close()

    # Read monitor: proves reads are never disrupted during the failover
    # (the pitch-critical property). Runs concurrently with write + verify.
    read_monitor = None
    if args.read_monitor:
        read_monitor = ReadMonitor(args, client, logger, args.read_interval)
        read_monitor.start()
        logger.log(f"READ_MONITOR started (interval={args.read_interval}s)")

    result = write_phase(args, client, logger, start_id)

    # EXTREME keep-down: the write transaction is done, so bring the masked
    # (kept-down) nodes back before verification.
    if args.keep_down and result.get("killed_ips"):
        logger.log(f"RESTORE bringing {len(result['killed_ips'])} kept-down node(s) back up...")
        for ip, ok in restore_nodes(result["killed_ips"], args.ssh_user):
            logger.log(f"RESTORE {ip} {'OK' if ok else 'FAILED'}")

    # Write the confirmed-ids file (external cross-check, like txn_workload .ids).
    with open(os.path.join(args.artifact_dir, f"bulk_{client}.ids"), "w") as fh:
        for i in result["confirmed"]:
            fh.write(f"{i}\n")

    verify = verify_phase(args, client, logger, result)

    read_summary = None
    if read_monitor:
        read_monitor.stop()
        read_summary = read_monitor.summary()
        logger.log(f"READ_MONITOR done: {read_summary['total_probes']} probes, "
                   f"{read_summary['failed_probes']} failed, "
                   f"availability={read_summary['availability_pct']}%")

    report = build_report(args, client, result, verify, read_summary)
    write_report(args, client, report, logger)

    logger.close()
    sys.exit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()