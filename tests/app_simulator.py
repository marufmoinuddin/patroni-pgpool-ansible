#!/usr/bin/env python3
# ============================================================================
# app_simulator.py — a REALISTIC customer-facing purchase application
# ============================================================================
# PURPOSE
#   Simulate EXACTLY how a real application behaves during a database
#   failover — with NO awareness of the cluster, pgpool, Patroni, or the
#   failover itself. This is what the supervisor asked for: the existing
#   bulk_failover_test.py is "failover-aware" (it knows the leader was killed,
#   waits, and resumes). A real customer does NOT do that.
#
#   A real customer:
#     * just clicks "buy" (fires a purchase request at the VIP)
#     * has NO idea a failover is happening
#     * if the request fails, sees an error — and either retries (bounded,
#       like re-clicking) or gives up
#     * NEVER waits for the database to come back
#
#   This script models that exactly. It fires purchase requests at the VIP
#   continuously, records the outcome of EVERY request, and NEVER waits for
#   the DB. The failover is triggered EXTERNALLY (by the operator, or by a
#   separate kill script) while this app keeps trying to serve customers.
#
#   The result is a REALISTIC measure of what customers experience during a
#   failover:
#     * SUCCESS  — the purchase committed and the customer got confirmation
#     * FAILED   — the purchase did NOT commit; the customer saw an error
#     * UNCERTAIN — the purchase COMMITTED but the customer saw an error
#                  (the "uncertain commit" — the worst case, needs reconcile)
#
#   This is the script that persuades stakeholders: it proves that during a
#   real failover, customers who got a "success" were never charged wrongly,
#   and the only "lost" requests are ones that genuinely failed (rolled back).
#
# USAGE
#   # Terminal 1: run the app simulator (keeps firing purchases)
#   python3 tests/app_simulator.py --duration 120 --rate 5
#
#   # Terminal 2: trigger a failover while the app is running
#   python3 tests/bulk_failover_test.py --no-kill ...   # or kill the leader manually
#
#   # After: the app prints a summary of what customers experienced
#
# ENV
#   PGPASSWORD   pgpool_admin password (else auto-fetched from a node's pool_passwd)
#
# REQUIREMENTS
#   pip install psycopg2-binary
#   SSH root access to the DB nodes (for password fetch)
# ============================================================================

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

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
DEFAULT_TABLE = "pack_purchases"
DEFAULT_USERS_TABLE = "pack_users"
DEFAULT_USER_ID = 1001
DEFAULT_PACK_ID = 42
DEFAULT_PRICE_CENTS = 1000
DEFAULT_QTY = 1
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
]


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
# SSH helpers (password fetch)
# ---------------------------------------------------------------------------
def run_ssh(host, remote_cmd, ssh_user=DEFAULT_SSH_USER, timeout=25):
    cmd = ["ssh", *SSH_OPTS, f"{ssh_user}@{host}", remote_cmd]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "ssh timeout"


def fetch_pgpool_password(nodes, ssh_user):
    for ip in nodes:
        for pf in ("/etc/pgpool2/pool_passwd", "/etc/pgpool-II/pool_passwd"):
            rc, out, _ = run_ssh(ip, f"grep '^pgpool_admin:' {pf} | cut -d: -f2", ssh_user)
            if rc == 0 and out:
                return out.splitlines()[0]
    return None


# ---------------------------------------------------------------------------
# DB helpers (all through the pgpool VIP — the app has NO other view)
# ---------------------------------------------------------------------------
def connect(args, client):
    return psycopg2.connect(
        host=args.vip,
        port=args.port,
        dbname=args.db,
        user=args.user,
        password=args.password,
        connect_timeout=args.connect_timeout,
        keepalives=1,
        keepalives_idle=5,
        keepalives_interval=2,
        keepalives_count=3,
        application_name=f"app_sim_{client}",
    )


def ensure_tables(conn, args):
    with conn.cursor() as cur:
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


def seed_user(conn, args):
    """Give the test user enough balance for the whole run."""
    n_purchases = math.ceil(args.duration * args.rate) + 1000  # margin
    cost = args.price_cents * args.qty
    seed_balance = n_purchases * cost + 10 ** 9
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "INSERT INTO {} (user_id, balance_cents) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET balance_cents = EXCLUDED.balance_cents"
            ).format(sql.Identifier(args.users_table)),
            (args.user_id, seed_balance),
        )
    return seed_balance


def get_max_id(conn, table, id_col="purchase_id"):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT COALESCE(MAX({}), 0) FROM {}").format(
                sql.Identifier(id_col), sql.Identifier(table)
            )
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The realistic purchase attempt — behaves EXACTLY like a real app
# ---------------------------------------------------------------------------
def attempt_purchase(args, client, purchase_id, payload, md5):
    """One customer purchase attempt. This is what a real app does:
    open a connection to the VIP, run the transaction, commit, close.
    NO retry, NO waiting, NO cluster awareness. Returns a result dict.

    Returns:
      {"outcome": "success", "purchase_id": id}
      {"outcome": "failed", "purchase_id": id, "error": "..."}   # rolled back
      {"outcome": "uncertain", "purchase_id": id, "error": "..."} # committed but unknown
    """
    cost = args.price_cents * args.qty
    conn = None
    try:
        conn = connect(args, client)
        conn.autocommit = False
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
                return {"outcome": "failed", "purchase_id": purchase_id,
                        "error": "insufficient_funds"}
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
                (purchase_id, args.user_id, args.pack_id, args.qty, args.price_cents, client, payload, md5),
            )
            conn.commit()
        return {"outcome": "success", "purchase_id": purchase_id}
    except psycopg2.Error as e:
        errmsg = str(e).splitlines()[0]
        # The app cannot tell if the COMMIT landed. It just saw an error.
        # We classify it as "uncertain" (may have committed) vs "failed"
        # (definitely rolled back) by checking if the id exists — but a real
        # app would NOT do this check inline; it would reconcile later.
        # Here we do the check so we can REPORT the true outcome.
        uncertain = False
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                # Did the purchase actually commit before the error?
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("SELECT 1 FROM {} WHERE purchase_id = %s").format(
                            sql.Identifier(args.table)
                        ),
                        (purchase_id,),
                    )
                    uncertain = cur.fetchone() is not None
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        if uncertain:
            return {"outcome": "uncertain", "purchase_id": purchase_id, "error": errmsg}
        return {"outcome": "failed", "purchase_id": purchase_id, "error": errmsg}
    except Exception as e:
        return {"outcome": "failed", "purchase_id": purchase_id, "error": str(e)[:120]}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# The app loop — fires purchase requests continuously, never waits
# ---------------------------------------------------------------------------
def run_app(args, client, logger):
    """The main app loop. Fires purchase requests at the VIP at the given rate.
    NEVER waits for the DB — if a request fails, it just records it and moves
    on to the next customer (exactly like a real app)."""
    results = []
    start = time.monotonic()
    deadline = start + args.duration
    next_id = args.start_id + 1
    seq = 0
    last_log = time.monotonic()

    while time.monotonic() < deadline:
        payload = os.urandom(args.payload_size)
        md5 = hashlib.md5(payload).hexdigest()
        res = attempt_purchase(args, client, next_id, payload, md5)
        results.append(res)
        seq += 1
        next_id += 1

        # Log periodically (not every request, to avoid spam)
        now = time.monotonic()
        if now - last_log >= args.log_every_sec:
            last_log = now
            ok = sum(1 for r in results if r["outcome"] == "success")
            fail = sum(1 for r in results if r["outcome"] == "failed")
            unc = sum(1 for r in results if r["outcome"] == "uncertain")
            logger.log(
                f"APP requests={seq} success={ok} failed={fail} uncertain={unc} "
                f"last={res['outcome']}"
            )

        # Rate limiting: sleep to hit the target requests/sec.
        # NOTE: this is the app's OWN pacing (like a user clicking), NOT
        # waiting for the DB. If the DB is down, requests still fire and fail.
        if args.rate > 0:
            time.sleep(1.0 / args.rate)

    return results


# ---------------------------------------------------------------------------
# Verification — what customers actually experienced
# ---------------------------------------------------------------------------
def verify(args, client, results, logger):
    """Reconcile the app's view against the DB (what a real app would do
    after the incident, or what an auditor would check)."""
    conn = None
    try:
        conn = connect(args, client)
    except psycopg2.Error as e:
        logger.log(f"VERIFY cannot connect: {str(e).splitlines()[0]}")
        return None

    # All purchase ids the app attempted
    attempted_ids = [r["purchase_id"] for r in results]

    # What's actually in the DB for this client
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT purchase_id FROM {} WHERE client = %s").format(
                sql.Identifier(args.table)
            ),
            (client,),
        )
        db_ids = set(row[0] for row in cur.fetchall())

    # Classify
    success = [r for r in results if r["outcome"] == "success"]
    failed = [r for r in results if r["outcome"] == "failed"]
    uncertain = [r for r in results if r["outcome"] == "uncertain"]

    # Cross-check: did every "success" actually land? Did any "failed" land?
    success_missing = [r["purchase_id"] for r in success if r["purchase_id"] not in db_ids]
    failed_landed = [r["purchase_id"] for r in failed if r["purchase_id"] in db_ids]
    uncertain_landed = [r["purchase_id"] for r in uncertain if r["purchase_id"] in db_ids]

    # Money consistency
    balance_current = None
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
    balance_expected = args.seed_balance - len(db_ids) * cost
    balance_ok = (balance_current == balance_expected)

    conn.close()

    return {
        "attempted": len(results),
        "success": len(success),
        "failed": len(failed),
        "uncertain": len(uncertain),
        "success_missing": success_missing,     # BAD: customer told success but no record
        "failed_landed": failed_landed,         # BAD: customer told failure but was charged
        "uncertain_landed": uncertain_landed,   # expected: committed but customer saw error
        "db_rows": len(db_ids),
        "balance_current": balance_current,
        "balance_expected": balance_expected,
        "balance_ok": balance_ok,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_report(args, client, results, verify_result, logger):
    base = os.path.join(args.artifact_dir, f"app_{client}")
    report = {
        "client": client,
        "vip": f"{args.vip}:{args.port}",
        "duration_sec": args.duration,
        "rate_per_sec": args.rate,
        "attempted": len(results),
        "outcomes": {
            "success": sum(1 for r in results if r["outcome"] == "success"),
            "failed": sum(1 for r in results if r["outcome"] == "failed"),
            "uncertain": sum(1 for r in results if r["outcome"] == "uncertain"),
        },
        "verification": verify_result,
    }
    with open(base + ".summary.json", "w") as fh:
        json.dump(report, fh, indent=2)

    v = verify_result or {}
    lines = []
    lines.append("=" * 62)
    lines.append("REALISTIC APP SIMULATOR REPORT")
    lines.append("=" * 62)
    lines.append(f"client            : {client}")
    lines.append(f"vip               : {args.vip}:{args.port}")
    lines.append(f"duration          : {args.duration}s at {args.rate} req/s")
    lines.append("-" * 62)
    lines.append("WHAT CUSTOMERS EXPERIENCED")
    lines.append(f"requests attempted: {len(results)}")
    lines.append(f"SUCCESS (charged + confirmed): {report['outcomes']['success']}")
    lines.append(f"FAILED  (not charged, saw error): {report['outcomes']['failed']}")
    lines.append(f"UNCERTAIN (charged but saw error): {report['outcomes']['uncertain']}")
    lines.append("-" * 62)
    lines.append("AUDIT / RECONCILIATION")
    if v:
        lines.append(f"success but missing in DB (BAD): {len(v.get('success_missing', []))}")
        lines.append(f"failed but landed in DB (BAD): {len(v.get('failed_landed', []))}")
        lines.append(f"uncertain that landed (expected): {len(v.get('uncertain_landed', []))}")
        lines.append(f"rows in DB          : {v.get('db_rows')}")
        lines.append(f"balance check       : {'OK' if v.get('balance_ok') else 'MISMATCH'} "
                     f"(current={v.get('balance_current')} expected={v.get('balance_expected')})")
    lines.append("=" * 62)
    report_txt = "\n".join(lines)
    print("\n" + report_txt)
    with open(base + ".report.txt", "w") as fh:
        fh.write(report_txt + "\n")
    logger.log(f"REPORT attempted={len(results)} "
               f"success={report['outcomes']['success']} "
               f"failed={report['outcomes']['failed']} "
               f"uncertain={report['outcomes']['uncertain']} "
               f"balance_ok={v.get('balance_ok') if v else None}")


# ---------------------------------------------------------------------------
# Logger (colorized console, plain file)
# ---------------------------------------------------------------------------
class Logger:
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
        if not C._on:
            return line
        body = line.split(" ", 2)[-1] if " " in line else line
        if "success" in body and "failed" not in body:
            return colorize(line, C.GREEN)
        if body.startswith("APP"):
            return colorize(line, C.CYAN)
        if body.startswith("VERIFY"):
            return colorize(line, C.MAGENTA)
        if body.startswith("REPORT"):
            return colorize(line, C.BOLD_CYAN)
        return line


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Realistic customer-facing purchase app simulator. Fires "
                    "purchase requests at the pgpool VIP with NO cluster "
                    "awareness — never waits for the DB. Run a failover "
                    "externally while this runs to measure real customer impact.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--vip", default=DEFAULT_VIP, help="pgpool Virtual IP")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="pgpool port")
    ap.add_argument("--db", default=DEFAULT_DB, help="database name")
    ap.add_argument("--user", default=DEFAULT_USER, help="DB user (via VIP)")
    ap.add_argument("--password", default=os.environ.get("PGPASSWORD", ""),
                    help="DB password (default: PGPASSWORD env, else auto-fetch)")
    ap.add_argument("--table", default=DEFAULT_TABLE, help="purchases table")
    ap.add_argument("--users-table", default=DEFAULT_USERS_TABLE, help="users table")
    ap.add_argument("--user-id", type=int, default=DEFAULT_USER_ID, help="buyer user id")
    ap.add_argument("--pack-id", type=int, default=DEFAULT_PACK_ID, help="pack id")
    ap.add_argument("--price-cents", type=int, default=DEFAULT_PRICE_CENTS, help="price per pack")
    ap.add_argument("--qty", type=int, default=DEFAULT_QTY, help="packs per purchase")
    ap.add_argument("--client", default=None, help="unique client id (default: app_<timestamp>)")
    ap.add_argument("--duration", type=int, default=120, help="how long to run (seconds)")
    ap.add_argument("--rate", type=float, default=5.0, help="purchase requests per second")
    ap.add_argument("--payload-size", type=int, default=1024, help="payload bytes per purchase")
    ap.add_argument("--connect-timeout", type=int, default=5, help="connect timeout (seconds)")
    ap.add_argument("--log-every-sec", type=float, default=5.0, help="log summary every N seconds")
    ap.add_argument("--nodes", nargs="+", default=DEFAULT_NODES, help="DB node IPs (for password fetch)")
    ap.add_argument("--ssh-user", default=DEFAULT_SSH_USER, help="SSH user for DB nodes")
    ap.add_argument("--artifact-dir", default="artifacts", help="where reports are written")
    args = ap.parse_args()

    if _PSYCOPG2_IMPORT_ERROR:
        sys.stderr.write("ERROR: psycopg2 is required. Install with: pip install psycopg2-binary\n")
        sys.exit(2)

    if not args.password:
        pw = fetch_pgpool_password(args.nodes, args.ssh_user)
        if pw:
            args.password = pw
            print(colorize("INFO: pgpool_admin password auto-fetched from pool_passwd", C.CYAN))
        else:
            sys.stderr.write("ERROR: no DB password. Set PGPASSWORD or --password.\n")
            sys.exit(2)

    client = args.client or f"app_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    os.makedirs(args.artifact_dir, exist_ok=True)
    logger = Logger(os.path.join(args.artifact_dir, f"app_{client}.events"))

    # Pre-flight: connect, create tables, seed user, get max id.
    conn = connect(args, client)
    conn.autocommit = False
    ensure_tables(conn, args)
    args.seed_balance = seed_user(conn, args)
    args.start_id = get_max_id(conn, args.table, "purchase_id")
    logger.log(f"SEED user_id={args.user_id} balance_cents={args.seed_balance} start_id={args.start_id}")
    conn.commit()
    conn.close()

    logger.log(
        f"=== app_simulator start client={client} vip={args.vip}:{args.port} "
        f"duration={args.duration}s rate={args.rate}/s ==="
    )
    logger.log("APP is now firing purchase requests — trigger a failover externally!")

    results = run_app(args, client, logger)

    logger.log(f"APP finished: {len(results)} requests attempted")
    verify_result = verify(args, client, results, logger)
    write_report(args, client, results, verify_result, logger)

    logger.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
