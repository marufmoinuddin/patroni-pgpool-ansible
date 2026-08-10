#!/bin/bash
# ============================================================================
# step3_setup.sh — Step 3: transaction-tracking setup + pre-failure baseline
# ============================================================================
# RUNS ON THE VPS (jump host) — it holds root SSH keys to all DB nodes.
#   1. Creates txn_track(id, client, ts) on the leader via the VIP
#   2. Validates the table is present on every node
#   3. Records the full pre-failure baseline snapshot:
#      - highest committed transaction ID (tracking table + postgres xid)
#      - replication state (leader pg_stat_replication, replica replay LSNs)
#      - primary identity (leader member + timeline)
#      - replica health (patronictl states, lag)
#      - etcd quorum (endpoint health)
#   4. Writes everything to an artifact dir for the Step 4 report.
#
# Usage: step3_setup.sh <artifact_dir>
# ============================================================================
set -euo pipefail

ARTIFACT_DIR="${1:?artifact dir required}"
VIP="192.168.122.200"
PORT="9999"
DB="postgres"
USER="pgpool_admin"
PATRONI_CFG="/etc/patroni/patroni.yml"
LEADER_IP="192.168.122.151"   # current leader (db2) — access point
ALL_NODES=("192.168.122.150" "192.168.122.151" "192.168.122.152")
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$ARTIFACT_DIR"
OUT="$ARTIFACT_DIR/baseline_${TS}.txt"
: > "$OUT"

ssh_node() { # ssh_node <ip> <command>
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 root@"$1" "$2"
}

echo "=== STEP 3 SETUP + PRE-FAILURE BASELINE $TS ===" | tee -a "$OUT"

# ---------- 1. Create tracking table (on leader via local psql) ----------
echo "--- 1. Tracking table ---" | tee -a "$OUT"
ssh_node "$LEADER_IP" "export PGPASSWORD=\$(grep '^pgpool_admin:' /etc/pgpool-II/pool_passwd | cut -d: -f2); /usr/pgsql-16/bin/psql -h $VIP -p $PORT -U $USER -d $DB -v ON_ERROR_STOP=1 -c 'CREATE TABLE IF NOT EXISTS txn_track (id BIGINT PRIMARY KEY, client TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now());' -c 'SELECT count(*) FROM txn_track;'" 2>&1 | tee -a "$OUT"

# ---------- 2. Validate presence on every node ----------
echo "--- 2. Replication validation ---" | tee -a "$OUT"
for ip in "${ALL_NODES[@]}"; do
  if ssh_node "$ip" "su - postgres -c \"psql -tAc 'SELECT count(*) FROM txn_track;'\"" >/dev/null 2>&1; then
    echo "node $ip: txn_track present" | tee -a "$OUT"
  else
    echo "node $ip: txn_track MISSING or unreachable" | tee -a "$OUT"
  fi
done

# ---------- 3. Baseline snapshot ----------
echo "--- 3. Baseline snapshot ---" | tee -a "$OUT"

echo "[3a] Tracking table max(id) + count (through VIP):" | tee -a "$OUT"
ssh_node "$LEADER_IP" "export PGPASSWORD=\$(grep '^pgpool_admin:' /etc/pgpool-II/pool_passwd | cut -d: -f2); /usr/pgsql-16/bin/psql -h $VIP -p $PORT -U $USER -d $DB -tA -c 'SELECT COALESCE(MAX(id),0), COUNT(*) FROM txn_track;'" 2>&1 | tee -a "$OUT"

echo "[3a] Postgres committed xid on leader (pg_current_xact_id):" | tee -a "$OUT"
ssh_node "$LEADER_IP" "su - postgres -c \"psql -tAc 'SELECT pg_current_xact_id();'\"" 2>&1 | tee -a "$OUT"

echo "[3b] pg_stat_replication on leader (replication state):" | tee -a "$OUT"
ssh_node "$LEADER_IP" "su - postgres -c \"psql -tAc 'SELECT application_name, state, sync_state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes, pg_wal_lsn_diff(pg_current_wal_lsn(), flush_lsn) AS flush_lag_bytes FROM pg_stat_replication ORDER BY application_name;'\"" 2>&1 | tee -a "$OUT"

echo "[3c] Primary identity (patronictl list):" | tee -a "$OUT"
ssh_node "$LEADER_IP" "patronictl -c $PATRONI_CFG list" 2>&1 | tee -a "$OUT"

echo "[3d] Replica health (patronictl json):" | tee -a "$OUT"
ssh_node "$LEADER_IP" "patronictl -c $PATRONI_CFG list -f json" | jq -r '.[] | .Member + " role=" + .Role + " state=" + (.State // "") + " tl=" + (.TL|tostring) + " lag=" + ((."Replay Lag" // 0)|tostring)' 2>&1 | tee -a "$OUT"

echo "[3e] etcd quorum:" | tee -a "$OUT"
ssh_node "$LEADER_IP" "etcdctl --endpoints=http://192.168.122.150:2379,http://192.168.122.151:2379,http://192.168.122.152:2379 endpoint health --cluster" 2>&1 | tee -a "$OUT"

echo "=== BASELINE COMPLETE: $ARTIFACT_DIR/baseline_${TS}.txt ===" | tee -a "$OUT"
