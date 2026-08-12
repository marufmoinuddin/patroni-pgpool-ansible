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
#
# SEEDING (fixed in Iteration 3+): the starting ID is ALWAYS derived from a
# fresh atomic query SELECT COALESCE(max(id),0) FROM txn_track executed
# immediately before the loop starts — never from a remembered/manual value
# or a stale .ids file. On a duplicate-key error (stale state or concurrent
# writer), the same ID is retried at most 3 times, then the workload
# re-queries max(id) and RESUMES from there (RESYNC); it never wedges on one
# colliding ID.
# ============================================================================
set -euo pipefail

CLIENT="${1:?client name required}"
DURATION="${2:?duration seconds required}"
ARTIFACT_DIR="${3:?artifact dir required}"
VIP="${VIP:-192.168.122.200}"
PORT="${PORT:-9999}"
DB="${DB:-postgres}"
USER="${PGADMIN_USER:-pgpool_admin}"
# psql path differs by distro: RHEL ships /usr/pgsql-16/bin/psql, Debian/Ubuntu
# ships /usr/bin/psql (pg_wrapper). Allow explicit override via PSQL env.
if [ -n "${PSQL:-}" ]; then
    :
elif [ -x /usr/pgsql-16/bin/psql ]; then
    PSQL="/usr/pgsql-16/bin/psql"
elif [ -x /usr/bin/psql ]; then
    PSQL="/usr/bin/psql"
else
    PSQL="psql"
fi

# Credential fallback: if PGPASSWORD/PGPASSFILE are not set, source the
# working pgpool_admin password from a DB node's pool_passwd (never printed).
# Path differs by distro: /etc/pgpool-II (RHEL) vs /etc/pgpool2 (Debian).
if [ -z "${PGPASSWORD:-}" ] && [ -z "${PGPASSFILE:-}" ]; then
    # Try to fetch from db2 (watchdog leader, known to have pool_passwd)
    for PF in /etc/pgpool-II/pool_passwd /etc/pgpool2/pool_passwd; do
        if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@192.168.122.151 "[ -f $PF ]" 2>/dev/null; then
            export PGPASSWORD=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@192.168.122.151 "grep '^pgpool_admin:' $PF | cut -d: -f2" 2>/dev/null)
            break
        fi
    done
fi
# Hard connection timeout: if the primary/VIP is down or the node is destroyed,
# fail FAST (5s) instead of hanging for minutes on an in-flight connect.
# This is the workload-side parallel of the observer's `timeout 5` guard — without
# it a killed node stalls the writer indefinitely and no FAILED event is logged
# (observed: 46-minute hang in Iteration 2, zero outage-window events).
export PGCONNECT_TIMEOUT=5

mkdir -p "$ARTIFACT_DIR"
LOG="$ARTIFACT_DIR/txn_${CLIENT}.ids"
STATE="$ARTIFACT_DIR/txn_${CLIENT}.state"
EVENTS="$ARTIFACT_DIR/txn_${CLIENT}.events"
LOG_FILE="$ARTIFACT_DIR/txn_${CLIENT}.log"
: > "$EVENTS"
: > "$LOG"
touch "$STATE" "$LOG_FILE"

MAX_QUERY="SELECT COALESCE(max(id), 0) FROM txn_track;"

# --- Fresh atomic seed: ALWAYS query the table. Never trust the filesystem. ---
LAST_ID=""
for attempt in 1 2 3 4 5; do
    LAST_ID=$("$PSQL" -h "$VIP" -p "$PORT" -U "$USER" -d "$DB" -tA -c "$MAX_QUERY" 2>/dev/null | head -1) || true
    if [ -n "$LAST_ID" ] && [ "$LAST_ID" -ge 0 ] 2>/dev/null; then
        break
    fi
    echo "$(date -u +%FT%TZ) WARN seed max(id) query failed (attempt $attempt), retrying..." | tee -a "$LOG_FILE"
    sleep 2
done
if [ -z "$LAST_ID" ]; then
    echo "$(date -u +%FT%TZ) FATAL could not query table max(id) — is the VIP/pgpool up?" | tee -a "$LOG_FILE"
    exit 2
fi

echo "=== txn_workload $CLIENT start at $(date -u +%FT%TZ) seeded=max(id)=$LAST_ID (fresh query) dur=${DURATION}s ===" | tee -a "$LOG_FILE"

START=$(date +%s)
CONFIRMED=0
FAILED=0
RESYNC=0
SAME_ID_RETRIES=0
NEXT_ID=$((LAST_ID + 1))

while [ $(( $(date +%s) - START )) -lt "$DURATION" ]; do
    ID=$NEXT_ID
    # Single-statement transaction: INSERT ... RETURNING. Commit confirmation
    # is the psql exit code 0 + row returned.
    OUT=$("$PSQL" -h "$VIP" -p "$PORT" -U "$USER" -d "$DB" \
          -v ON_ERROR_STOP=1 -tA \
          -c "INSERT INTO txn_track(id, client, ts) VALUES ($ID, '$CLIENT', now()) RETURNING id;" 2>&1) \
        && { echo "$ID" >> "$LOG"; echo "$(date -u +%FT%TZ) CONFIRMED $ID" >> "$EVENTS"; CONFIRMED=$((CONFIRMED + 1)); NEXT_ID=$((NEXT_ID + 1)); SAME_ID_RETRIES=0; } \
        || { echo "$(date -u +%FT%TZ) FAILED $ID: $(echo "$OUT" | head -1)" >> "$EVENTS"; echo "FAILED id=$ID: $(echo "$OUT" | head -1)" >> "$LOG_FILE"; FAILED=$((FAILED + 1)); \
             if echo "$OUT" | grep -qi "duplicate key"; then \
                 SAME_ID_RETRIES=$((SAME_ID_RETRIES + 1)); \
                 if [ "$SAME_ID_RETRIES" -ge 3 ]; then \
                     NEW_MAX=$("$PSQL" -h "$VIP" -p "$PORT" -U "$USER" -d "$DB" -tA -c "$MAX_QUERY" 2>/dev/null | head -1) || true; \
                     if [ -n "$NEW_MAX" ] && [ "$NEW_MAX" -ge "$ID" ] 2>/dev/null; then \
                         NEXT_ID=$((NEW_MAX + 1)); SAME_ID_RETRIES=0; RESYNC=$((RESYNC + 1)); \
                         echo "$(date -u +%FT%TZ) RESYNC colliding-id=$ID -> next=$NEXT_ID (table max=$NEW_MAX)" >> "$EVENTS"; \
                         echo "RESYNC colliding-id=$ID -> next=$NEXT_ID (table max=$NEW_MAX)" >> "$LOG_FILE"; \
                     fi; \
                 fi; \
             fi; }

    # Small pacing gap: keeps connection churn bounded. 0.02s ~ 50 txn/s.
    sleep 0.02
done

echo "LAST_ID=$((NEXT_ID - 1))" > "$STATE"
echo "CONFIRMED=$CONFIRMED FAILED=$FAILED RESYNC=$RESYNC" >> "$STATE"
echo "=== txn_workload $CLIENT done: confirmed=$CONFIRMED failed=$FAILED resync=$RESYNC last=$(tail -1 "$LOG") ===" | tee -a "$LOG_FILE"
exit 0