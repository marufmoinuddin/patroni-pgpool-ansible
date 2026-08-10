#!/bin/bash
# ============================================================================
# txn_workload.sh — client-confirmed write workload for failover validation
# ============================================================================
# Writes monotonically increasing, client-assigned transaction IDs through
# the VIP/pgpool as separate transactions. Every COMMIT-confirmed ID is
# appended to the client log. After a failover, the client-confirmed set
# vs. what exists on the new primary reveals ANY lost committed transaction.
#
# Usage: txn_workload.sh <client_name> <duration_sec> <artifact_dir>
#   client_name  : unique workload client id (e.g. w1) for multi-client runs
#   duration_sec : run for N seconds, then exit 0
#   artifact_dir : where confirmed-ids log and summary are written
#
# Env: PGPASSFILE or PGPASSWORD must provide pgpool_admin credentials.
#      Connect string is hardcoded to the cluster VIP.
# ============================================================================
set -euo pipefail

CLIENT="${1:?client name required}"
DURATION="${2:?duration seconds required}"
ARTIFACT_DIR="${3:?artifact dir required}"
VIP="${VIP:-192.168.122.200}"
PORT="${PORT:-9999}"
DB="${DB:-postgres}"
USER="${PGADMIN_USER:-pgpool_admin}"
PSQL="/usr/pgsql-16/bin/psql"

# Credential fallback: if PGPASSWORD/PGPASSFILE are not set, source the
# working pgpool_admin password from the local pool_passwd (never printed).
if [ -z "${PGPASSWORD:-}" ] && [ -z "${PGPASSFILE:-}" ] && [ -f /etc/pgpool-II/pool_passwd ]; then
    export PGPASSWORD=$(grep "^pgpool_admin:" /etc/pgpool-II/pool_passwd | cut -d: -f2)
fi

mkdir -p "$ARTIFACT_DIR"
LOG="$ARTIFACT_DIR/txn_${CLIENT}.ids"
STATE="$ARTIFACT_DIR/txn_${CLIENT}.state"
touch "$LOG"

# Recover last confirmed id if resuming (allows restart mid-test)
LAST_ID=0
if [ -s "$LOG" ]; then
    LAST_ID=$(tail -1 "$LOG")
fi

echo "=== txn_workload $CLIENT start at $(date -u +%FT%TZ) from id=$LAST_ID dur=${DURATION}s ===" | tee -a "$ARTIFACT_DIR/txn_${CLIENT}.log"

START=$(date +%s)
CONFIRMED=0
FAILED=0
NEXT_ID=$((LAST_ID + 1))

while [ $(( $(date +%s) - START )) -lt "$DURATION" ]; do
    ID=$NEXT_ID
    # Single-statement transaction: INSERT ... RETURNING. Commit confirmation
    # is the psql exit code 0 + row returned.
    OUT=$("$PSQL" -h "$VIP" -p "$PORT" -U "$USER" -d "$DB" \
          -v ON_ERROR_STOP=1 -tA \
          -c "INSERT INTO txn_track(id, client, ts) VALUES ($ID, '$CLIENT', now()) RETURNING id;" 2>&1) \
        && { echo "$ID" >> "$LOG"; CONFIRMED=$((CONFIRMED + 1)); NEXT_ID=$((NEXT_ID + 1)); } \
        || { echo "FAILED id=$ID: $(echo "$OUT" | head -1)" >> "$ARTIFACT_DIR/txn_${CLIENT}.log"; FAILED=$((FAILED + 1)); }

    # Small pacing gap: keeps connection churn bounded; adjust if load needs
    # to be higher. 0.02s ~ 50 txn/s per client.
    sleep 0.02
done

echo "LAST_ID=$((NEXT_ID - 1))" > "$STATE"
echo "CONFIRMED=$CONFIRMED FAILED=$FAILED" >> "$STATE"
echo "=== txn_workload $CLIENT done: confirmed=$CONFIRMED failed=$FAILED last=$(tail -1 "$LOG") ===" | tee -a "$ARTIFACT_DIR/txn_${CLIENT}.log"
exit 0
