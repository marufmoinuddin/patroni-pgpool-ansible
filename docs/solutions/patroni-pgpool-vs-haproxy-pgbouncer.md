# Patroni + pgpool-II vs. Patroni + HAProxy + PgBouncer

> **Scope:** A detailed architecture and pros/cons comparison of two popular ways to
> put a *traffic/access layer* in front of a Patroni-managed 3-node PostgreSQL cluster.
> Both options share the **same data plane** (Patroni + etcd decide who is primary);
> they differ entirely in the **access plane** (how clients reach the database).
>
> This document is written against this repository's 3-node topology
> (db1/db2/db3 = `.150/.151/.152`, VIP `192.168.122.200:9999`), but the reasoning
> applies to any Patroni cluster.

---

## 1. The Shared Foundation: Patroni + etcd (identical in both)

Both architectures use the exact same Patroni + etcd data plane:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           etcd CLUSTER (3 nodes)                            │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                                      │
│  │  etcd   │  │  etcd   │  │  etcd   │   ← Raft consensus: leader lock      │
│  │ (db1)   │  │ (db2)   │  │ (db3)   │   ← cluster state, config, TTL       │
│  └────┬────┘  └────┬────┘  └────┬────┘   ← quorum = 2/3                     │
│       │            │            │                                           │
│       └────────────┴────────────┘                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ▲
                                    │  leader lock / state
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
                    (streaming replication, pg_rewind,
                     replication slots — all Patroni-managed)
```

**What is identical:** automatic failover, split-brain protection, `pg_rewind`
rejoin, replication slots, centralized config, `patronictl`. The difference is
**only** the layer in front of the database that clients actually connect to.

---

## 2. Architecture A — Patroni + pgpool-II (this repo)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           APPLICATION LAYER                                 │
│  App connects to pgpool-II VIP: 192.168.122.200:9999                        │
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
└─────────────────────────────────────────────────────────────────────────────┘
```

**pgpool-II is a single, integrated component that does everything in the access
plane:** connection pooling, read/write splitting, primary detection, and the
floating VIP (via its built-in **watchdog**). One process, one config, one VIP.

---

## 3. Architecture B — Patroni + HAProxy + PgBouncer

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           APPLICATION LAYER                                 │
│  App connects to HAProxy VIP: 192.168.122.200:6432 (or 5432)                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        HAProxy CLUSTER (Keepalived)                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                      │
│  │   HAProxy   │    │   HAProxy   │    │   HAProxy   │   ← VIP via keepalived│
│  │   (db1)     │◄───│   (db2)     │◄───│   (db3)     │   ← active/standby    │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘                      │
│         │                  │                  │                             │
│         └──────────────────┼──────────────────┘                             │
│                            ▼                                                │
│              ┌─────────────────────────────────┐                            │
│              │   Two backend pools:            │                            │
│              │   • primary pool  → PgBouncer on│                            │
│              │     the Patroni leader          │                            │
│              │   • replica pool → PgBouncer on │                            │
│              │     the replicas                │                            │
│              └─────────────────────────────────┘                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
┌─────────────────────┐ ┌─────────────────────┐ ┌─────────────────────┐
│   PgBouncer (db1)   │ │   PgBouncer (db2)   │ │   PgBouncer (db3)   │
│   local pool 6432   │ │   local pool 6432   │ │   local pool 6432   │
│         │           │ │         │           │ │         │           │
│         ▼           │ │         ▼           │ │         ▼           │
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
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key difference:** the access plane is **split into three specialized components**:

1. **HAProxy** — a fast TCP/L4 load balancer. It does **not** pool connections and
   does **not** understand SQL. It just forwards TCP to a backend. It needs a
   **separate VIP mechanism** (usually **Keepalived**) because HAProxy has no built-in
   watchdog/VIP.
2. **PgBouncer** — a dedicated **connection pooler** that runs **on each DB node**
   (or on app nodes). It pools PostgreSQL connections but does **not** route
   read/write or manage a VIP.
3. **Patroni REST API** — HAProxy is configured to check each node's `:8008/patroni`
   endpoint to decide which backend is the primary vs. a replica (via a small script
   or `httpchk`).

---

## 4. How Each Layer Handles the Same Jobs

| Job | Patroni + pgpool (A) | Patroni + HAProxy + PgBouncer (B) |
|-----|----------------------|------------------------------------|
| **Connection pooling** | pgpool-II (built-in) | PgBouncer (dedicated, per-node) |
| **Read/write splitting** | pgpool-II (SQL-aware, `load_balance_mode`) | HAProxy (two pools: primary vs replica) |
| **Primary detection** | pgpool-II `sr_check` + Patroni callback | HAProxy health check on Patroni `:8008` |
| **Floating VIP** | pgpool-II **watchdog** (built-in) | **Keepalived** (separate component) |
| **SQL awareness** | Yes (parses queries) | No (HAProxy is L4; PgBouncer is protocol-aware but not a router) |
| **Session-level features** | Yes (e.g. `SELECT` routing, function blacklist) | Limited (HAProxy can't see SQL) |
| **Components to run** | 1 (pgpool-II) | 2 (HAProxy + PgBouncer) + Keepalived |

---

## 5. Detailed Pros and Cons

### 5.1 Patroni + pgpool-II (Option A)

#### Pros

1. **One integrated component.** pgpool-II does pooling + routing + VIP in a single
   process with a single config. Simpler to operate than juggling HAProxy + PgBouncer
   + Keepalived.

2. **Built-in VIP (watchdog).** No separate Keepalived. The watchdog elects a leader
   and floats the VIP with quorum + `wd_quorum_exit` split-brain protection (already
   hardened in this repo).

3. **SQL-aware read/write splitting.** pgpool can route `SELECT` to replicas and
   writes to the primary, blacklist functions (`nextval`, `setval`, ...), and handle
   `SELECT ... FOR UPDATE` correctly. HAProxy cannot see SQL at all.

4. **Active failover signaling.** This repo wires a Patroni `on_role_change` callback
   (`pgpool_role_signal.sh`) that runs `pcp_promote_node` immediately — closing the
   switchover detection gap to ~3–4 s.

5. **Fewer moving parts to monitor.** One service (`pgpool`) per node instead of
   `haproxy` + `pgbouncer` + `keepalived`.

#### Cons

1. **pgpool-II is a heavier, more complex process.** It parses SQL and manages
   pooling + watchdog; it has more config surface and a steeper learning curve than
   HAProxy.

2. **Connection pooling is generally weaker than PgBouncer.** PgBouncer is the
   de-facto standard for high-concurrency pooling (transaction pooling, thousands of
   clients). pgpool's pooling is adequate but not as battle-tested at extreme scale.

3. **SQL parsing adds overhead and risk.** Routing decisions depend on query parsing;
   edge cases (prepared statements, `SELECT` in functions, `WITH` clauses) can be
   misrouted and need careful `white_function_list`/`black_function_list` tuning.

4. **Watchdog is pgpool-specific.** If you ever want to swap the access layer, you
   lose the VIP mechanism and must re-architect.

5. **Single point of failure per node.** If pgpool crashes on the VIP-holding node,
   the VIP must move (watchdog) — a brief blip (measured ~50 s in this repo for a
   VIP-node kill).

### 5.2 Patroni + HAProxy + PgBouncer (Option B)

#### Pros

1. **Best-in-class connection pooling.** PgBouncer is the industry standard for
   pooling thousands of connections into a small number of PostgreSQL backends
   (transaction pooling). This is the biggest win for high-concurrency OLTP.

2. **HAProxy is rock-solid and simple.** It is a mature, extremely fast L4 load
   balancer with a tiny footprint and a huge community. It does one thing well.

3. **Clean separation of concerns.** Pooling (PgBouncer), routing (HAProxy), and VIP
   (Keepalived) are independent, swappable components. You can replace any one
   without touching the others.

4. **Primary/replica split is explicit.** HAProxy exposes two clear pools (primary
   and replica) with health checks against Patroni's `:8008`. Read scaling is easy to
   reason about.

5. **Very high connection concurrency.** PgBouncer's transaction pooling lets you
   serve far more concurrent clients than pgpool's default pooling.

#### Cons

1. **More components to run and monitor.** HAProxy + PgBouncer + Keepalived (and
   their health checks) instead of one pgpool. More config files, more failure modes.

2. **No built-in VIP.** You must add **Keepalived** (or similar) for the floating IP.
   That is an extra daemon and an extra split-brain surface to manage.

3. **HAProxy is not SQL-aware.** It cannot route based on query type. Read/write
   splitting is done by *pool* (primary pool vs replica pool), so the **application
   must use separate connection strings** for reads vs writes — or you need an
   additional router. This is a real architectural difference.

4. **No active failover signal out of the box.** HAProxy relies on polling Patroni's
   `:8008` health check. You must configure the check cadence carefully; there is no
   built-in `pcp_promote_node`-style push. (You can script it, but it's extra work.)

5. **Two-hop latency.** Client → HAProxy → PgBouncer → PostgreSQL adds a hop compared
   to pgpool's single hop (though the overhead is small).

6. **More moving parts to keep consistent.** PgBouncer config on every node, HAProxy
   config on every node, Keepalived config on every node — more drift risk.

---

## 6. Side-by-Side Comparison Table

| Capability | Patroni + pgpool (A) | Patroni + HAProxy + PgBouncer (B) |
|------------|----------------------|------------------------------------|
| Automatic failover (data plane) | ✅ Patroni | ✅ Patroni (same) |
| Split-brain protection (data plane) | ✅ Patroni + etcd | ✅ Patroni + etcd (same) |
| Connection pooling | ✅ pgpool (adequate) | ✅✅ PgBouncer (best-in-class) |
| Read/write splitting | ✅ SQL-aware | ⚠️ By pool (app must split conn strings) |
| Floating VIP | ✅ Built-in watchdog | ⚠️ Requires Keepalived |
| Primary detection | ✅ `sr_check` + active callback | ⚠️ HAProxy polls `:8008` |
| Active failover signal | ✅ `pcp_promote_node` callback | ⚠️ Polling only (scriptable) |
| SQL awareness | ✅ Yes | ❌ No (HAProxy is L4) |
| High-concurrency pooling | ⚠️ Good | ✅✅ Excellent |
| Component count | 1 (pgpool) | 3 (HAProxy + PgBouncer + Keepalived) |
| Operational simplicity | ✅ Simpler | ⚠️ More moving parts |
| Config surface | Moderate | Larger (3 configs) |
| Best for | All-in-one, SQL routing, simpler ops | Extreme concurrency, clean separation |

---

## 7. Which Should You Choose?

### Choose Patroni + pgpool-II (Option A) when:
- You want **one integrated access layer** (pooling + routing + VIP) — simpler to
  operate and monitor.
- You want **SQL-aware read/write splitting** (the app uses a single connection
  string and pgpool routes queries).
- You value the **built-in watchdog VIP** with quorum + split-brain protection and
  the **active failover callback** already wired in this repo.
- Your concurrency needs are moderate (hundreds to low thousands of connections).

### Choose Patroni + HAProxy + PgBouncer (Option B) when:
- You need **very high connection concurrency** (thousands of clients) — PgBouncer's
  transaction pooling is the clear winner.
- You prefer **clean separation of concerns** and are comfortable running and
  monitoring three components.
- Your application can **split read vs write connection strings** (or you add a
  router), because HAProxy cannot route by SQL.
- You already run HAProxy/Keepalived elsewhere and want to reuse that expertise.

> **Bottom line for this repo:** the repository is built around **Patroni + pgpool-II**
> because it delivers a complete, validated, all-in-one access layer with SQL-aware
> routing and a built-in VIP — ideal for a self-contained 3-node HA cluster. If your
> workload later demands extreme connection concurrency or you want to decouple the
> access layer into swappable parts, the **Patroni + HAProxy + PgBouncer** model is the
> natural evolution — and it reuses the exact same Patroni + etcd data plane already
> deployed here.
