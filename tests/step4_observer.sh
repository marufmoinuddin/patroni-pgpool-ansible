#!/bin/bash
# ============================================================================
# step4_observer.sh — split-brain + routing observer for failover iterations
# ============================================================================
# Purpose: during a failover iteration, sample every node's DIRECT
# pg_is_in_recovery() (bypassing pgpool so we see each node's real local
# state, not the pooled view) plus pgpool's pool_nodes routing, every POLL
# seconds, into a timestamped artifact file.
#
# SPLIT-BRAIN RULE (Step 5 acceptance): at every sampled instant, at most
# ONE node may report pg_is_in_recovery() = false (the primary). Two or more
# nodes reporting false = split-brain — flagged by the post-processing.
#
# USAGE
#   ./step4_observer.sh <artifact_dir> [iterations] [poll_secs] [pool_host_ip]
#   e.g. ./step4_observer.sh ~/deploy/artifacts/step3 720 2 192.168.122.151
#   default iterations=720 (24 min @ 2s — 2x longest observed outage+rejoin span),
#   default pool host = db1 (a NON-TARGET survivor; never the node being killed)
# ============================================================================
set -u

ARTIFACT_DIR="${1:?artifact dir required}"
ITERATIONS="${2:-720}"   # default 720 (24 min @ 2s) — covers the longest observed
                          # outage+rejoin span (~12 min) with 2x margin; hang bug
                          # (ServerAliveInterval) is fixed so long windows are free.
POLL_SECS="${3:-2}"
POOL_HOST_IP="${4:-192.168.122.150}"   # db1 by default — change per iteration

mkdir -p "$ARTIFACT_DIR"
OUT="$ARTIFACT_DIR/observe_iter.log"
: > "$OUT"

NODES=(db1 db2 db3)
declare -A NODE_IP=( [db1]="192.168.122.150" [db2]="192.168.122.151" [db3]="192.168.122.152" )
# SSH options: ConnectTimeout bounds the initial TCP connect; ServerAlive* bounds
# established connections (a node killed mid-session otherwise hangs the poll loop
# forever — observed in Iteration 2). timeout(1) is the last-resort hard cap.
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=2 -o ServerAliveInterval=2 -o ServerAliveCountMax=3"
POOL_HOST="$POOL_HOST_IP"   # any SURVIVOR node with pgpool for the pool_nodes read
# Helper scripts pre-shipped to each node (avoids fragile nested quoting).
REC_HELPER="/tmp/node_recovery.sh"
POOL_HELPER="/tmp/pool_nodes.sh"

for ((i=1; i<=ITERATIONS; i++)); do
    TS=$(date -u +%FT%TZ)
    LINE="$TS"

    # Direct per-node recovery state (bypasses pgpool entirely).
    # NOTE: this observer is designed to run ON the VPS (management host),
    # so node access is a single ssh hop, not a double hop.
    for n in "${NODES[@]}"; do
        R=$(timeout 5 ssh $SSH_OPTS "root@${NODE_IP[$n]}" "bash $REC_HELPER" \
            2>/dev/null | tr -d '[:space:]')
        [ -z "$R" ] && R="UNREACHABLE"
        LINE="$LINE $n.recovery=$R"
    done

    # Pooled routing view (pgpool) — from the configured SURVIVOR node
    P=$(timeout 5 ssh $SSH_OPTS "root@$POOL_HOST" "bash $POOL_HELPER" \
        2>/dev/null | tr -d '[:space:]')
    LINE="$LINE pool=[$P]"

    echo "$LINE" | tee -a "$OUT"
    sleep "$POLL_SECS"
done

echo "--- observer complete: $OUT"
