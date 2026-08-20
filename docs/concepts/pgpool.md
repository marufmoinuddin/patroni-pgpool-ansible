# pgpool-II — The Traffic Controller

> **Scope:** What pgpool-II does in this architecture, what it deliberately does NOT do, the watchdog/VIP mechanism, and how applications connect.

---

## What pgpool-II Does

pgpool-II sits between applications and PostgreSQL. It provides:

| Feature | Description |
|---------|-------------|
| **Connection Pooling** | Reuses PostgreSQL connections — reduces overhead of frequent connect/disconnect |
| **Read/Write Splitting** | Sends SELECT to replicas, INSERT/UPDATE/DELETE to the primary (via `load_balance_mode`) |
| **Primary Detection** | Uses streaming replication checks (`sr_check`) to determine which backend is primary |
| **Virtual IP (VIP) Management** | The **watchdog** module manages a floating IP — applications connect to the VIP, not to individual nodes |
| **Failover Detection** | Health checks (`health_check_period`) detect backend failures |
| **PCP (Pgpool Control Protocol)** | Administrative interface for `pcp_*` commands (attach/detach nodes, promote, etc.) |

## What pgpool-II Does NOT Do

| Not pgpool-II's Job | Handled By |
|---------------------|------------|
| PostgreSQL replication management | Patroni |
| Leader election for PostgreSQL | Patroni + etcd |
| pg_basebackup / pg_rewind | Patroni |
| PostgreSQL configuration management | Patroni (via etcd) |
| Data consistency / split-brain prevention for PostgreSQL | Patroni + etcd |

## Why Patroni Manages PostgreSQL HA, and pgpool-II Sits in Front

**Separation of concerns:**

```
Patroni's Domain (Data Plane):
├── Which PostgreSQL node is primary?
├── Streaming replication setup
├── Failover / switchover execution
├── Replication slot management
├── Configuration distribution
└── Timeline history

pgpool-II's Domain (Access Plane):
├── Where do applications connect? (VIP)
├── Connection pooling
├── Query routing (read/write split)
├── Backend health monitoring
└── Failover signaling to applications (via the VIP)
```

Why this matters: If pgpool-II tried to manage PostgreSQL failover, you would have two systems fighting over who is primary. **Patroni is the single source of truth for PostgreSQL leadership. pgpool-II follows Patroni's lead.**

## The Watchdog and the Virtual IP

The pgpool-II **watchdog** runs on all 3 nodes. The members elect a **watchdog leader** (by priority). The watchdog leader:
- Owns the **floating VIP** (`192.168.122.200`)
- Monitors the other pgpool nodes' heartbeats (UDP 9000)
- If the watchdog leader dies, another member **takes over the VIP** within seconds

This means applications always have ONE address to connect to, and that address **never goes down** — as long as at least 2 of 3 pgpool nodes are alive.

## How Applications Connect

```
Application
    │
    ▼
┌────────────────────────────────────────────┐
│  pgpool-II VIP: 192.168.122.200:9999       │
│  (floats between db1, db2, db3 via watchdog)│
└────────────────────────────────────────────┘
    │
    ├──► Write queries ──► Current PostgreSQL Primary (detected via sr_check)
    │
    └──► Read queries  ──► Load balanced across Replicas (if load_balance_mode=on)
```

```bash
# From psql
psql -h 192.168.122.200 -p 9999 -U postgres -d postgres

# From an application
postgresql://postgres:***@192.168.122.200:9999/postgres
```

## Dual-Distro pgpool-II Packaging

The repo deploys pgpool-II on **both** distro families with OS-conditional config:

| Aspect | RHEL / CentOS / Stream 9 | Debian / Ubuntu |
|--------|--------------------------|-----------------|
| Package | `percona-pgpool-II-pg16` (4.7) | native `pgpool2` (4.3.5) — NOT `postgresql-16-pgpool2` |
| Config dir | `/etc/pgpool-II` | `/etc/pgpool2` |
| Service | `pgpool` | `pgpool2` |
| Watchdog config | separate `pgpool_watchdog.conf` | inline in `pgpool.conf` (legacy param names) |
| Watchdog params | `heartbeat_hostnameN`, `heartbeat_portN`, `heartbeat_deviceN`, `wd_priority` (unindexed) | `heartbeat_destinationN`, `heartbeat_destination_portN`, `heartbeat_interfaceN`, `wd_priority0` (indexed) |

> ⚠️ **Debian pitfall:** The Percona package `postgresql-16-pgpool2` is a **PostgreSQL 16 extension module only** — no daemon, no systemd unit, no config dir. It also installs `libpgpool2=4.7.0` which **hard-conflicts** with native `pgpool2` 4.3.5. Use native `pgpool2` only.

---

## Further Reading

- [Patroni + pgpool-II vs Standalone pgpool-II](../solutions/patroni-pgpool-vs-standalone-pgpool.md) — why Patroni underneath matters
- [Patroni + pgpool-II vs Patroni + HAProxy + PgBouncer](../solutions/patroni-pgpool-vs-haproxy-pgbouncer.md) — access-layer alternatives
- [Operations Guide](../operations/operations.md) — daily `pcp_*` commands
- [Resilience & Self-Healing](resilience.md) — watchdog hardening, `wd_quorum_exit`, active switchover notification