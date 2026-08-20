# Concepts — High Availability Mental Model

> **Scope:** The foundational HA concepts you need before deploying or operating this cluster. If you have never worked with database HA, read this section carefully.

---

## Primary and Replica

- **Primary** (formerly "master"): The single PostgreSQL instance that accepts **reads AND writes**. It generates WAL (Write-Ahead Log) records for every change.
- **Replica** (formerly "slave" / "standby"): A PostgreSQL instance that **receives WAL from the primary and replays it**. It accepts reads ONLY (when `hot_standby = on`).

```
Primary (read/write) ──WAL stream──► Replica (read-only)
       │                                   │
       ▼                                   ▼
  WAL generated                      WAL replayed
```

## Streaming Replication

PostgreSQL's native replication mechanism. The primary continuously sends WAL records to connected replicas over a replication connection.

**Key settings on the primary:**

- `wal_level = replica` — includes enough information in WAL for replicas to replay
- `max_wal_senders` — maximum concurrent replication connections
- `max_replication_slots` — replication slots reserve WAL so replicas don't fall behind

**Key settings on the replica:**

- `hot_standby = on` — allows read queries while replaying WAL
- `primary_conninfo` — connection string to the primary (Patroni manages this)

## WAL (Write-Ahead Log)

The write-ahead log is PostgreSQL's transaction log. Every data modification is written to WAL **before** it is applied to data files. This ensures durability (crash recovery) and enables replication (replicas replay the same WAL).

WAL segments are 16 MB files in `pg_wal/`. They accumulate until:
- A checkpoint completes (data files synced to disk)
- `max_wal_size` is reached (forces a checkpoint)
- Archiving/cleanup removes old segments

## Replication Slots

A replication slot is a server-side bookmark that tells the primary: *"Do not remove WAL segments until this replica has received them."*

Without slots, a slow or disconnected replica could cause the primary to recycle WAL the replica still needs — breaking replication and requiring a full `pg_basebackup` to recover. **Patroni always uses replication slots** (`use_slots: true`).

## Failover vs. Switchover

| Aspect | Failover | Switchover |
|--------|----------|------------|
| **Trigger** | Unplanned — primary crashes, network partition, OOM | Planned — maintenance, OS upgrades, hardware replacement |
| **Initiation** | Automatic (Patroni detects failure) | Manual (`patronictl switchover`) |
| **Data Loss Risk** | Possible (async replication) | Zero (graceful handoff) |
| **Old Primary** | May still be running (split-brain risk) | Gracefully demoted to replica |
| **Speed** | Seconds to ~40s (depends on TTL) | Near-instant (with the switchover-signal fix) |

## Split Brain

**Split brain** occurs when two nodes believe they are the primary at the same time and both accept writes. This corrupts data irrecoverably.

**Causes:**
- Network partition: primary and replica lose contact, both think the other is down
- etcd quorum loss: multiple Patroni nodes think they hold the leader lock
- Clock drift: etcd election timeouts fire incorrectly

**Protection (all built into this cluster):**
- etcd quorum requirement (a majority of nodes must agree)
- Patroni TTL-based leader lock (expires if Patroni stops heartbeating)
- Fencing (watchdog / STONITH) — forcibly reboots a node that loses quorum
- `maximum_lag_on_failover` — prevents promotion of severely lagged replicas

## Leader Election and Distributed Consensus

Patroni uses etcd's **Raft consensus algorithm** for leader election. Simplified:

1. Each Patroni node tries to acquire a lock in etcd: `/service/<scope>/leader`
2. The lock is a key with a **TTL** (time-to-live, e.g. 30 seconds)
3. The node that creates the key becomes leader; the others become followers
4. The leader must **renew the lock** (heartbeat) before the TTL expires
5. If the leader stops heartbeating (crash, network), the lock expires
6. Followers race to acquire the expired lock — the winner becomes the new leader

## Why etcd? Because It Provides:

- **Strong consistency** — linearizable reads/writes via Raft
- **TTL/leases** — automatic lock expiration
- **Watch mechanism** — Patroni gets instant notification of changes
- **Cluster membership** — dynamic node discovery

## etcd Quorum

etcd uses Raft, which requires a majority (**quorum**) to operate:

| Cluster Size | Quorum Required | Tolerated Failures |
|--------------|-----------------|--------------------|
| 1 | 1 | 0 (no HA) |
| **3** | **2** | **1** |
| 5 | 3 | 2 |
| 7 | 4 | 3 |

This is why we use **3 etcd nodes** — it tolerates exactly one node failure while maintaining quorum. With 2 nodes, quorum = 2, so zero failures are tolerated (which defeats the purpose of HA).

---

## Further Reading

- [Architecture Overview](architecture.md) — the full component diagram and data flow
- [Patroni Promotion Mechanism](../solutions/patroni-promotion-mechanism.md) — how the leader lock works step by step
- [Resilience & Self-Healing](resilience.md) — what survives what