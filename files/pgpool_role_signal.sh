#!/bin/bash
# pgpool_role_signal.sh - Patroni on_role_change callback.
#
# PURPOSE
#   Signal pgpool immediately when Patroni promotes a node, instead of
#   waiting for pgpool's periodic sr_check polling to notice the role
#   change. Without this, a clean `patronictl switchover` left the
#   write path pointing at the old primary for ~4 minutes (observed
#   2026-08-11: switchover 15:29:05, pgpool re-detected ~15:33:22,
#   VIP writes failing with "cannot execute CREATE TABLE in a
#   read-only transaction" in the window).
#
# Patroni invokes callbacks as:  <cmd> <cb_type> <role> <scope>
#   $1 = on_role_change
#   $2 = primary | replica
#   $3 = cluster scope
#
# SAFETY (mirrors the proven reattach_nodes.sh Case 2 model)
#   - Acts ONLY on promotion to primary (role=primary).
#   - Confirms via patronictl that THIS node is the Patroni Leader.
#     The etcd leader lease is the single authority; this blocks the
#     old primary during any split-brain window.
#   - Maps own IP -> pgpool backend node id and runs pcp_promote_node
#     for that id on ALL pgpool nodes (including the VIP-holding
#     watchdog leader, since pcp_listen_addresses='*').
#   - Idempotent: promoting an already-primary node is a no-op.
#
# Runs as postgres (the Patroni service user); uses .pcppass for PCP
# auth, exactly like the reattach timer.
#
# NOTE: this is an active signal, NOT a replacement for the reattach
# timer or sr_check. Those remain as the safety net for crash-failover
# paths where the callback may race pgpool's own failover machinery.

set -u

# Distro-aware pgpool config dir (RHEL/Percona: /etc/pgpool-II, Debian: /etc/pgpool2)
if [ -d "/etc/pgpool-II" ]; then
    PGCONF_DIR="/etc/pgpool-II"
elif [ -d "/etc/pgpool2" ]; then
    PGCONF_DIR="/etc/pgpool2"
else
    PGCONF_DIR="/etc/pgpool-II"
fi

PCP_PORT=9898
PCP_USER=pgpool_pcp
export PCPPASSFILE="$PGCONF_DIR/.pcppass"
LOG_DIR="/var/log/pgpool"
[ -d "$LOG_DIR" ] || LOG_DIR="/tmp"
LOG_FILE="$LOG_DIR/role_signal.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S%z') $*" >> "$LOG_FILE"; }

CB="${1:-}"
ROLE="${2:-}"
SCOPE="${3:-}"

# Only react to a promotion to primary.
if [ "$CB" != "on_role_change" ] || [ "$ROLE" != "primary" ]; then
    exit 0
fi

log "on_role_change fired: cb=$CB role=$ROLE scope=$SCOPE"

# 1. Authoritative check: Patroni must name THIS node the Leader.
#    Compare on the member name (hostname -s == Leader.Member); the
#    leader's Host field is the node IP (never the VIP).
MY_HOST=$(hostname -s)

LEADER_JSON=$(patronictl -c /etc/patroni/patroni.yml list -f json 2>/dev/null | jq -c '.[] | select(.Role == "Leader")' | head -1)
if [ -z "$LEADER_JSON" ]; then
    log "WARN: cannot determine Patroni Leader - skipping (reattach timer will catch up)"
    exit 0
fi
LEADER_IP=$(echo "$LEADER_JSON" | jq -r '.Host')
LEADER_MEMBER=$(echo "$LEADER_JSON" | jq -r '.Member')

if [ "$LEADER_MEMBER" != "$MY_HOST" ]; then
    log "INFO: this node ($MY_HOST) is not the Patroni Leader ($LEADER_MEMBER@$LEADER_IP) - skipping"
    exit 0
fi
log "confirmed: this node is Patroni Leader $LEADER_MEMBER ($LEADER_IP)"

# 2. Map own host/IP -> pgpool backend node id from pgpool.conf.
#    Match against ANY of this host's IPs (safe: the VIP 192.168.122.200
#    is never a backend_hostname, so only real node IPs match).
NODE_ID=""
CONF="$PGCONF_DIR/pgpool.conf"
MY_IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$')
for idx in $(seq 0 9); do
    HOST=$(grep -E "^backend_hostname${idx}[[:space:]]*=" "$CONF" 2>/dev/null | sed -E "s/.*=\s*'([^']+)'.*/\1/")
    if [ -n "$HOST" ]; then
        for ip in $MY_IPS; do
            if [ "$HOST" = "$ip" ] || [ "$HOST" = "$MY_HOST" ]; then
                NODE_ID=$idx
                break 2
            fi
        done
    fi
done
if [ -z "$NODE_ID" ]; then
    log "ERROR: cannot map $MY_IP/$MY_HOST to a pgpool backend node id in $CONF"
    exit 1
fi
log "own pgpool backend node id: $NODE_ID"

# 2b. If pgpool still marks this node down but the backend is up,
#     attach first (mirror reattach Case 1) so promote can take effect.
LINE=$(pcp_node_info -h localhost -p $PCP_PORT -U $PCP_USER -w "$NODE_ID" 2>/dev/null)
if [ -n "$LINE" ]; then
    STATUS=$(echo "$LINE" | awk '{print $5}')
    PG_STATUS=$(echo "$LINE" | awk '{print $6}')
    if [ "$STATUS" = "down" ] && [ "$PG_STATUS" = "up" ]; then
        log "node $NODE_ID marked down but backend up - attaching first"
        pcp_attach_node -h localhost -p $PCP_PORT -U $PCP_USER -w "$NODE_ID" >> "$LOG_FILE" 2>&1
    fi
fi

# 3. Promote this node id on ALL pgpool nodes (local + peers), so the
#    VIP-holding watchdog leader learns the new primary immediately.
PGPOOL_HOSTS=$(grep -E "^backend_hostname[0-9]+[[:space:]]*=" "$CONF" 2>/dev/null | sed -E "s/.*=\s*'([^']+)'.*/\1/" | sort -u)
[ -z "$PGPOOL_HOSTS" ] && PGPOOL_HOSTS="localhost"

for phost in $PGPOOL_HOSTS; do
    log "pcp_promote_node $NODE_ID -> pgpool $phost (leader=$LEADER_MEMBER)"
    if timeout 10 pcp_promote_node -h "$phost" -p $PCP_PORT -U $PCP_USER -w "$NODE_ID" >> "$LOG_FILE" 2>&1; then
        log "  ok: promote accepted on $phost"
    else
        rc=$?
        log "  warn: promote on $phost returned rc=$rc (pgpool may already be consistent)"
    fi
done

log "done: routing role corrected to node $NODE_ID on pgpool hosts: $PGPOOL_HOSTS"
exit 0
