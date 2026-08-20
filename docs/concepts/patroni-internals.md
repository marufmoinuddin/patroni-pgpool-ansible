# Patroni Internals — How the Brain Works

> **Scope:** What actually happens inside Patroni: bootstrap, replica join, heartbeat, leader lock expiry, and failover. Pairs with the [Promotion Mechanism](../solutions/patroni-promotion-mechanism.md) document for the high-level story.

---

## How Patroni Starts

When `systemctl start patroni` runs:

1. Patroni reads `/etc/patroni/patroni.yml`
2. Connects to the etcd endpoints listed in `etcd3.hosts`
3. Checks whether a cluster with this **scope** already exists in etcd
4. **If no cluster exists** and this node has `bootstrap.method: initdb`:
   - Runs `initdb` to create a new PostgreSQL data directory
   - Creates users, replication slots, and configuration
   - Acquires the leader lock in etcd → becomes primary
5. **If a cluster exists:**
   - Reads the current leader from etcd
   - If this node was previously primary and has valid data → tries to acquire the leader lock; if successful, promotes PostgreSQL to primary; if another node holds the lock, starts as a replica
   - If this node was previously a replica (or is new): checks whether a data directory exists and is valid; if not, runs `pg_basebackup` from the current primary; starts PostgreSQL as a replica; registers as a follower in etcd

## How the First PostgreSQL Node Becomes Primary (Bootstrap)

1. Patroni starts on db1
2. Reads `patroni.yml` → `scope: "maruf"`, `bootstrap.method: initdb`
3. Connects to etcd → no existing cluster for this scope
4. Patroni runs `initdb`:
   - Creates the data directory with encoding UTF8
   - Enables **data checksums** (detects corruption)
   - Sets auth method to `scram-sha-256`
   - Creates users: `postgres` (superuser), `replicator` (replication), `pgpool` (monitoring), `admin` (management)
5. Patroni writes the initial cluster state to etcd (`/percona_lab/maruf/leader`, `.../members/db1`, `.../config`, `.../history`)
6. Patroni starts PostgreSQL as **PRIMARY**
7. Patroni begins heartbeating the leader lock (renews TTL every `loop_wait` seconds)

## How Replicas Join

1. Patroni starts on db2
2. Connects to etcd → finds existing cluster, leader = db1
3. Checks local data directory → empty or invalid → proceeds to `pg_basebackup`
4. Runs `pg_basebackup`:
   - Connects to db1:5432 as `replicator` user
   - Streams base backup + WAL to the local data directory
   - Creates `standby.signal` (tells PostgreSQL to start as a replica)
5. Patroni starts PostgreSQL as **REPLICA**
6. Patroni registers in etcd: `/percona_lab/maruf/members/db2` → `{role: "replica"}`
7. Patroni begins streaming replication from the primary

## How Patroni Communicates with etcd

| Operation | etcd Key | Purpose |
|-----------|----------|---------|
| Leader lock | `/percona_lab/maruf/leader` | Key with TTL; only the leader can write it |
| Member registration | `/percona_lab/maruf/members/<name>` | Node metadata, role, state, timeline |
| Cluster config | `/percona_lab/maruf/config` | PostgreSQL parameters (dynamic) |
| Timeline history | `/percona_lab/maruf/history` | Used by `pg_rewind` after failover |
| Watches | All keys above | Instant notification of changes |

**Heartbeat loop** (runs every `loop_wait` seconds, default 10s):
- If leader → renew the leader lock TTL
- If follower → check whether the leader lock has expired
- Read cluster state from etcd
- Reconcile local PostgreSQL state with the desired state
- Apply configuration changes from etcd to `postgresql.conf`
- Update local state in etcd

## How Leader Locks Work

In `patroni.yml` `bootstrap.dcs`:

```yaml
ttl: 30           # Lock expires after 30s without renewal
loop_wait: 10     # Patroni heartbeat interval
retry_timeout: 10 # Wait before retrying failed operations
```

**Scenario: Primary crashes at t=0**

| Time | Event |
|------|-------|
| t=0 | db1 (primary) crashes — Patroni stops, no more heartbeats |
| t=10 | db2 Patroni loop runs, sees the leader lock still valid (expires at t=30) |
| t=20 | db2 Patroni loop runs, leader lock still valid |
| t=30 | **Leader lock expires** in etcd (TTL reached) |
| t=30–40 | db2 (and db3) next loop iteration → both try to acquire the lock |
| t=30–40 | One wins (Raft consensus), becomes the new leader |
| t=30–40 | Winner promotes local PostgreSQL to primary |
| t=30–40 | Winner writes the new leader key to etcd |
| t=40 | Other node sees the new leader, becomes a replica |

**Failover time ≈ TTL + loop_wait** (30s + 10s = ~40s worst case with these defaults).

## How Failover Happens — Step by Step

```
NORMAL STATE:
etcd: leader = db1 (TTL=30s, renewed every 10s)
db1:  PostgreSQL PRIMARY, Patroni LEADER
db2:  PostgreSQL REPLICA, Patroni FOLLOWER
db3:  PostgreSQL REPLICA, Patroni FOLLOWER

FAILURE:
1. db1 crashes (power loss, kernel panic, OOM kill)
2. Patroni on db1 stops → no more leader lock renewals
3. The etcd leader lock TTL counts down... expires at t=30s

ELECTION:
4. db2 Patroni loop (t=30-40s): detects the expired lock
5. db2 attempts to write a new leader key with its identity
6. etcd Raft consensus: db2 wins (or db3, but only one)
7. db2 Patroni: "I am leader now"

PROMOTION:
8. db2 Patroni calls pg_ctl promote
9. PostgreSQL on db2 ends recovery and becomes PRIMARY
10. db2 Patroni writes a new leader key to etcd with a new TTL
11. db2 Patroni updates member state in etcd: role=leader

RECONCILIATION:
12. db3 Patroni loop: sees new leader = db2
13. db3 updates local state: role=replica, follows db2
14. db3 ensures the replication connection points to db2

CLIENT ROUTING (pgpool-II):
15. pgpool-II health check detects db1 down, db2 up as primary
16. pgpool-II routes writes to db2
17. The VIP may move independently (pgpool-II watchdog leader election)
```

---

## Further Reading

- [Patroni Promotion Mechanism](../solutions/patroni-promotion-mechanism.md) — timelines, history, lag, and the "wait for preferred leader" pattern
- [Concepts](ha-fundamentals.md) — the mental model (streaming replication, split brain, quorum)
- [Operations Guide](../operations/operations.md) — daily `patronictl` / `etcdctl` / `pcp_*` commands