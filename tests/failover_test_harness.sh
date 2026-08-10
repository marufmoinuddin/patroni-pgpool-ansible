#!/bin/bash
# ============================================================================
# failover_test_harness.sh — SAFE controlled failover/self-heal test driver
# ============================================================================
# Purpose: drive deliberate node-level test actions (stop/restart/kill of
# the Patroni unit) against an EXPLICITLY ALLOWLISTED subset of the cluster,
# with dry-run by default and a mandatory confirmation gate before any
# destructive command touches a live node.
#
# SAFETY MODEL
#   1. ALLOWLIST: only nodes passed via --targets (validated against the
#      static NODE_MAP below) may be touched. Anything else is refused,
#      even if it looks valid.
#   2. DRY-RUN DEFAULT: without --execute the harness only prints what it
#      WOULD do and exits 0. It never touches a node in dry-run mode.
#   3. CONFIRMATION GATE: with --execute, the harness prints the exact
#      command per target and REQUIRES the operator to type the node name
#      to confirm. Any other input aborts before anything runs.
#   4. CURRENT-ITERATION SCOPING: --targets defines the ONLY nodes this
#      invocation may act on. The harness never derives targets from
#      cluster state (e.g. "the leader") implicitly.
#   5. LEADER GUARD (default on): refuses to act on the current Patroni
#      Leader unless --allow-leader is explicitly passed (used only for
#      deliberate leader-targeted tests).
#
# USAGE
#   ./failover_test_harness.sh --targets db2 --action stop          # dry-run
#   ./failover_test_harness.sh --targets db2 --action stop --execute
#   ./failover_test_harness.sh --targets db1 --action kill --allow-leader --execute
#   ./failover_test_harness.sh --targets db2 db3 --action restart --execute
#
# ACTIONS (allowed set; anything else is refused):
#   stop | restart | kill        -> systemctl stop/restart/kill patroni on each target
#   status                       -> read-only cluster status (no gate needed)
# ============================================================================
set -u

# --- Static node map: short-name -> ssh root@IP on the management host ----
# This is the ONLY node universe the harness knows. Edit here when the
# topology changes.
declare -A NODE_MAP=(
    [db1]="192.168.122.150"
    [db2]="192.168.122.151"
    [db3]="192.168.122.152"
)
# Management host through which all node SSH happens (VPS).
MGMT_HOST="144.79.249.124"
MGMT_USER="maruf"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15"

LOG_FILE="/var/log/patroni/failover_harness.log"
PATRONI_CFG="/etc/patroni/patroni.yml"

# --- Parse arguments --------------------------------------------------------
TARGETS=()
ACTION=""
EXECUTE=0
ALLOW_LEADER=0

usage() {
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -40
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --targets)
            shift
            while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do
                TARGETS+=("$1")
                shift
            done
            ;;
        --action) shift; ACTION="${1:-}"; shift ;;
        --execute) EXECUTE=1; shift ;;
        --allow-leader) ALLOW_LEADER=1; shift ;;
        --help|-h) usage ;;
        *) echo "ERROR: unknown argument: $1"; usage ;;
    esac
done

log() { echo "$(date '+%F %T'): $*" | tee -a "$LOG_FILE"; }

# --- Validate allowlist -----------------------------------------------------
if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "ERROR: no --targets given. Refusing to run without an explicit allowlist."
    exit 2
fi
for t in "${TARGETS[@]}"; do
    if [ -z "${NODE_MAP[$t]:-}" ]; then
        echo "ERROR: target '$t' is not in the known node universe (NODE_MAP). Refusing."
        exit 2
    fi
done

case "$ACTION" in
    stop|restart|kill|status) ;;
    *) echo "ERROR: invalid/empty action '$ACTION'. Allowed: stop|restart|kill|status"; exit 2 ;;
esac

# --- Leader guard -----------------------------------------------------------
if [ "$ACTION" != "status" ]; then
    LEADER=$(ssh $SSH_OPTS "$MGMT_USER@$MGMT_HOST" \
        "ssh $SSH_OPTS root@${NODE_MAP[${TARGETS[0]}]} \
         'patronictl -c $PATRONI_CFG list -f json 2>/dev/null | jq -r \".[] | select(.Role==\\\"Leader\\\") | .Member\"'" 2>/dev/null | tr -d '\r\n')
    for t in "${TARGETS[@]}"; do
        if [ "$t" = "$LEADER" ] && [ "$ALLOW_LEADER" -eq 0 ]; then
            echo "REFUSED: '$t' is the current Patroni Leader. Pass --allow-leader to target it deliberately."
            exit 3
        fi
    done
fi

# --- Dry-run: print plan, touch nothing --------------------------------------
echo "============================================================"
echo "FAILOVER TEST PLAN"
echo "  Action : $ACTION on ${TARGETS[*]}"
echo "  Mode   : $([ $EXECUTE -eq 1 ] && echo EXECUTE || echo DRY-RUN)"
echo "  Nodes  :"
for t in "${TARGETS[@]}"; do
    echo "    - $t (${NODE_MAP[$t]})"
done
echo "============================================================"

if [ "$ACTION" = "status" ]; then
    for t in "${TARGETS[@]}"; do
        log "STATUS $t"
        ssh $SSH_OPTS "$MGMT_USER@$MGMT_HOST" \
            "ssh $SSH_OPTS root@${NODE_MAP[$t]} \
             'systemctl is-active patroni patroni-self-heal.timer cluster-health.timer; patronictl -c $PATRONI_CFG list'"
    done
    exit 0
fi

if [ $EXECUTE -eq 0 ]; then
    echo "[dry-run] Would execute:"
    for t in "${TARGETS[@]}"; do
        echo "    ssh ... root@${NODE_MAP[$t]} 'systemctl $ACTION patroni'"
    done
    echo "[dry-run] No nodes touched. Re-run with --execute to act."
    exit 0
fi

# --- Confirmation gate --------------------------------------------------------
echo ""
echo "DESTRUCTIVE ACTION AHEAD: systemctl $ACTION patroni on: ${TARGETS[*]}"
for t in "${TARGETS[@]}"; do
    echo "  - $t (${NODE_MAP[$t]})"
done
echo ""
for t in "${TARGETS[@]}"; do
    read -r -p "Type exactly '$t' to confirm action on $t (anything else aborts): " CONFIRM
    if [ "$CONFIRM" != "$t" ]; then
        echo "ABORTED: confirmation mismatch for $t (got '$CONFIRM'). No node was touched."
        exit 4
    fi
done

# --- Execute ----------------------------------------------------------------
for t in "${TARGETS[@]}"; do
    log "EXECUTE $ACTION on $t (${NODE_MAP[$t]})"
    ssh $SSH_OPTS "$MGMT_USER@$MGMT_HOST" \
        "ssh $SSH_OPTS root@${NODE_MAP[$t]} 'systemctl $ACTION patroni; echo RC=\$?'"
    log "Done $ACTION on $t"
    sleep 2
done

# --- Post-action verification --------------------------------------------------
echo ""
echo "=== POST-ACTION CLUSTER STATE ==="
ssh $SSH_OPTS "$MGMT_USER@$MGMT_HOST" \
    "ssh $SSH_OPTS root@${NODE_MAP[${TARGETS[0]}]} \
     'patronictl -c $PATRONI_CFG list'"
exit 0
