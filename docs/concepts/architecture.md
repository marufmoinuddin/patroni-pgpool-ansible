# Architecture Overview

> **Scope:** High-level view of the components, data flow, and network topology for this 3-node Patroni + pgpool-II cluster.

---

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           APPLICATION LAYER                                 │
│  Your application connects to: pgpool-II VIP (192.168.122.200:9999)        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         pgpool-II CLUSTER (Watchdog)                        │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                      │
│  │  pgpool-II  │    │  pgpool-II  │    │  pgpool-II  │   ← VIP floats here  │
│  │   (db1)     │◄───│   (db2)     │◄───│   (db3)     │   ← quorum = 2/3     │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘                      │
│         │                  │                  │                             │
│         └──────────────────┼──────────────────┘                             │
│                            ▼                                                │
│              ┌─────────────────────────────────┐                            │
│              │   Health Checks (pg_isready)    │                            │
│              │   Streaming Replication Checks  │                            │
│              └─────────────────────────────────┘                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
│    POSTGRESQL       │ │    POSTGRESQL       │ │    POSTGRESQL       │
│     PRIMARY         │ │     REPLICA         │ │     REPLICA         │
│    (db1:5432)       │ │    (db2:5432)       │ │    (db3:5432)       │
│  ┌───────────────┐  │ │  ┌───────────────┐  │ │  ┌───────────────┐  │
│  │   Patroni     │  │ │  │   Patroni     │  │ │  │   Patroni     │  │
│  │   (Leader)    │  │ │  │   (Follower)  │  │ │  │   (Follower)  │  │
│  └───────┬───────┘  │ │  └───────┬───────┘  │ │  └───────┬───────┘  │
└──────────│──────────┘ └──────────│──────────┘ └──────────│──────────┘
           │                       │                       │
           └───────────────────────┼───────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           etcd CLUSTER (3 nodes)                            │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                                      │
│  │  etcd   │  │  etcd   │  │  etcd   │   ← Raft consensus: leader lock      │
│  │ (db1)   │  │ (db2)   │  │ (db3)   │   ← cluster state, config, TTL       │
│  └────┬────┘  └────┬────┘  └────┬────┘   ← quorum = 2/3                     │
│       │            │            │                                           │
│       └────────────┴────────────┘                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

Plus a **backup/monitoring node** (db-backup: 192.168.122.153):
- **pgBackRest** — receives archived WAL + makes full backups of the cluster
- **PMM Server** — monitoring web dashboard (Docker container)

---

## Component Roles

| Component | Role | What It Does NOT Do |
|-----------|------|---------------------|
| **PostgreSQL 16** | Database engine — stores data, processes queries, streams WAL | Does not decide who is primary; does not fail over automatically |
| **Patroni** | Cluster manager — leader election, failover, replication orchestration, config management | Does not route client connections; does not pool connections |
| **etcd** | Distributed consensus store — holds leader lock, cluster state, configuration | Does not manage PostgreSQL; does not understand SQL |
| **pgpool-II** | Connection middleware — routing, pooling, read balancing, VIP management | Does not manage PostgreSQL replication; does not elect leaders |
| **pgBackRest** | Backup & restore engine — full/incremental backups, archiving, PITR | Does not manage HA; does not route traffic |
| **PMM** | Monitoring — dashboards, alerting, query analytics (pg_stat_monitor) | Does not manage HA; is not required for failover |

---

## Data Flow Summary

1. **Application connects to the pgpool-II VIP** (`192.168.122.200:9999`) — it never talks to individual nodes
2. pgpool-II **routes writes** to the current PostgreSQL primary (detected via streaming replication checks)
3. pgpool-II **load-balances reads** across replicas (optional, `load_balance_mode = on`)
4. **Patroni** on each PostgreSQL node watches etcd for leadership changes
5. **etcd** holds the leader lock — only one Patroni can hold it at a time
6. When the primary fails, Patroni on a replica **acquires the lock and promotes** PostgreSQL
7. pgpool-II detects the new primary via health checks and **routes traffic there**
8. The **VIP moves** to the pgpool-II node that is currently the watchdog leader (independent of the PostgreSQL primary)
9. **pgBackRest** keeps archiving WAL; **PMM** keeps graphing everything

---

## Network Endpoints

| Service | Port | Source → Destination | Purpose |
|---------|------|----------------------|---------|
| SSH | 22 | Admin → All nodes | Remote administration |
| **pgpool-II (VIP)** | **9999** | App servers → VIP | **Client connection endpoint** |
| PCP (pgpool control) | 9898 | Admin → All nodes | pcp_* admin commands |
| Watchdog heartbeat | 9000 | All nodes ↔ All nodes | pgpool-II watchdog inter-node checks |
| PostgreSQL | 5432 | All nodes ↔ All nodes | Replication + pgpool health checks |
| Patroni REST API | 8008 | All nodes ↔ All nodes | Health checks, `patronictl` |
| etcd client | 2379 | All nodes ↔ All nodes | Patroni ↔ etcd communication |
| etcd peer | 2380 | All nodes ↔ All nodes | etcd Raft (cluster) communication |
| pgBackRest | 22 (SSH) | PG nodes → backup node | WAL archiving / backups |
| PMM Server | 443 | Admin / PG nodes → backup node | Monitoring web UI (HTTPS) |

> 🔥 **Firewall rule:** Allow these ports **only between cluster nodes** (source = cluster subnet). Do not expose etcd, the Patroni REST API, or PostgreSQL directly to application networks — **only the pgpool-II VIP**.

---

## Why This Separation Matters

**Patroni owns the data plane** (which PostgreSQL node is primary, replication, failover, slots, config).
**pgpool-II owns the access plane** (where clients connect, connection pooling, query routing, VIP).

They do not fight over leadership. Patroni is the single source of truth for PostgreSQL; pgpool-II follows Patroni's lead via streaming replication checks and an active `on_role_change` callback.

---

## Further Reading

- [Patroni + pgpool-II vs Standalone pgpool-II](../solutions/patroni-pgpool-vs-standalone-pgpool.md) — decision guide
- [Patroni + pgpool-II vs Patroni + HAProxy + PgBouncer](../solutions/patroni-pgpool-vs-haproxy-pgbouncer.md) — access layer comparison
- [Patroni Promotion Mechanism](../solutions/patroni-promotion-mechanism.md) — how failover works internally
- [Preferred Primary Node](../solutions/preferred-primary-node.md) — configuring failover_priority
- [Resilience & Self-Healing](resilience.md) — architectural hardening