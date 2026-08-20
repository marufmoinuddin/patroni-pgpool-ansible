# Patroni + pgpool-II vs. Standalone PostgreSQL + pgpool-II

> **Scope:** A detailed, decision-oriented comparison of two ways to run a 3-node
> PostgreSQL cluster behind pgpool-II. This document is written against the exact
> architecture in this repository (3 DB nodes + 1 backup node, VIP `192.168.122.200:9999`),
> but the reasoning applies to any 3-node PostgreSQL + pgpool-II deployment.

---

## 1. The Two Architectures at a Glance

### Option A — Patroni + pgpool-II (what this repo deploys)

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
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                                      │
│  │  etcd   │  │  etcd   │  │  etcd   │   ← Raft consensus: leader lock      │
│  │ (db1)   │  │ (db2)   │  │ (db3)   │   ← cluster state, config, TTL       │
│  └────┬────┘  └────┬────┘  └────┬────┘   ← quorum = 2/3                     │
│       │            │            │                                           │
│       └────────────┴────────────┘                                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key idea:** Patroni (backed by etcd) is the **single source of truth** for
*which* PostgreSQL node is primary. pgpool-II only handles *routing* — it follows
Patroni's decision. Failover is automatic and consensus-driven.

### Option B — Standalone PostgreSQL + pgpool-II (no Patroni, no etcd)

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
│  (streaming repl)   │ │  (streaming repl)   │ │  (streaming repl)   │
│                     │ │                     │ │                     │
│  NO Patroni         │ │  NO Patroni         │ │  NO Patroni         │
│  NO etcd            │ │  NO etcd            │ │  NO etcd            │
└─────────────────────┘ └─────────────────────┘ └─────────────────────┘
```

**Key idea:** PostgreSQL streaming replication is configured **by hand** (or by a
script). There is **no consensus layer** deciding who is primary. pgpool-II is the
*only* thing that detects a primary failure and tries to promote a replica — and it
does so **without** the coordination guarantees that Patroni + etcd provide.

---

## 2. Who Decides "Who Is Primary"? — The Core Difference

This single question drives every other difference between the two options.

| Concern | Option A: Patroni + pgpool | Option B: Standalone + pgpool |
|---------|----------------------------|-------------------------------|
| **Primary election** | Patroni + etcd (Raft consensus, leader lock with TTL) | pgpool-II's `failover_command` / `pcp_promote_node` (no consensus) |
| **Split-brain protection** | Strong: etcd quorum + TTL lease + optional softdog fencing (host reboot) | Weak: relies on pgpool's health checks + watchdog; no fencing by default |
| **Replication setup** | Automatic (`pg_basebackup`, slots, `pg_rewind`) | Manual (`pg_basebackup` by hand, manual `standby.signal`, manual slot config) |
| **Replica rejoin after failover** | Automatic (`pg_rewind` + Patroni re-follows new leader) | Manual — you must re-point `primary_conninfo` and re-basebackup by hand |
| **Config distribution** | Patroni pushes `postgresql.conf` params via etcd to all nodes | Manual — edit every node's config yourself |
| **Single source of truth** | etcd (leader lease) | pgpool's view (polling-based, can be stale) |

> **The one-line summary:** In Option A, **Patroni owns the data plane** (who is
> primary) and pgpool owns the **access plane** (where clients connect). In Option B,
> **pgpool tries to own both** — and it is not designed to be a consensus engine.

---

## 3. Detailed Pros and Cons

### 3.1 Patroni + pgpool-II (Option A)

#### Pros

1. **Automatic, consensus-driven failover.** When the primary dies, etcd's leader
   lock expires (TTL) and a replica acquires it. No human, no script, no guesswork.
   Measured in this repo: ~38–43 s for a hard kill, ~3–4 s for a clean switchover
   (with the `on_role_change` callback + `sr_check_period = 3`).

2. **Split-brain protection is real.** etcd requires a 2/3 quorum, so only one node
   can ever hold the leader lease. Combined with the optional **softdog watchdog**
   (`patroni_watchdog: true`), a partitioned primary is **rebooted** rather than
   allowed to keep accepting writes. This is the strongest anti-split-brain story
   available for PostgreSQL.

3. **Automatic replica rejoin with `pg_rewind`.** After a failover, the old primary
   comes back and Patroni runs `pg_rewind` to resynchronize it with the new leader —
   no full `pg_basebackup` needed. This is a huge operational win.

4. **Replication slots managed automatically.** Patroni creates and tracks slots, so
   a slow replica never causes the primary to recycle WAL it still needs.

5. **Centralized configuration.** PostgreSQL parameters are stored in etcd and pushed
   to every node. Change once, apply everywhere (`patronictl reload`).

6. **A clean REST API + `patronictl`.** Monitoring, switchover, restart, and reinit are
   one command away. This is what makes the cluster *operable* by a human.

7. **Self-healing.** The repo's `07_Configure_Cluster_Health.yml` adds timers that
   restart a crashed local Patroni member and alert on quorum loss.

8. **Proven in this repo.** 5 consecutive power-loss failovers, zero lost commits,
   zero split-brain — all documented in `docs/step6_failover_report.md`.

#### Cons

1. **More moving parts.** You now run Patroni + etcd on every node. More processes,
   more config, more to learn, more to monitor.

2. **etcd is a write-availability SPOF.** If etcd loses quorum, the cluster goes
   read-only even if all PostgreSQL nodes are healthy (documented finding §12.8 in the
   README). Mitigation: dedicated etcd witness nodes or a 5-node etcd.

3. **Higher operational complexity.** Concepts like TTL, `loop_wait`, `pg_rewind`,
   fencing, and DCS must be understood. The learning curve is real.

4. **Slightly more resource overhead.** etcd is latency-sensitive and wants its own
   fast disk; Patroni adds a small CPU/memory footprint per node.

5. **Failover is not instant.** TTL + `loop_wait` means ~40 s worst case for a hard
   crash (tunable down to ~15–20 s). It is automatic, but not zero-downtime.

### 3.2 Standalone PostgreSQL + pgpool-II (Option B)

#### Pros

1. **Fewer components.** No etcd, no Patroni. Just PostgreSQL + pgpool-II. Simpler to
   install and reason about for a small cluster.

2. **Lower resource footprint.** No DCS to run or tune; no extra daemons.

3. **Faster to stand up for a demo / small workload.** If you only need read/write
   splitting + a VIP and are willing to manage failover by hand, this is quicker.

4. **pgpool-II still gives you pooling + read/write split + VIP.** Those features are
   identical in both options — pgpool does not care whether Patroni is underneath.

#### Cons

1. **No automatic, safe failover.** When the primary dies, pgpool detects it via
   health checks and runs `failover_command`, but *someone* (a script or a human) must
   promote a replica. There is no consensus — nothing stops two nodes from both
   thinking they are primary.

2. **High split-brain risk.** Without etcd's lease + fencing, a network partition can
   leave the old primary still accepting writes while pgpool promotes a new one. This
   is the single biggest reason not to run standalone + pgpool for anything important.

3. **Manual replication management.** You configure `primary_conninfo`, `standby.signal`,
   and replication slots by hand. After a failover you must manually re-point the old
   primary and re-basebackup it. Error-prone and slow.

4. **No `pg_rewind` automation.** Rejoining a node that diverged requires a full
   `pg_basebackup` — expensive on large databases.

5. **No centralized config.** Every node's `postgresql.conf` is edited independently;
   drift is easy.

6. **pgpool's primary detection is polling-based and can be stale.** It relies on
   `sr_check`/health-check cadence. In this repo we saw exactly this problem (a ~4 min
   switchover detection gap) — and we fixed it with a Patroni callback. Without Patroni,
   you have no such active signal, so the gap is unbounded by design.

7. **No self-healing.** A crashed node stays down until a human intervenes.

---

## 4. Side-by-Side Comparison Table

| Capability | Patroni + pgpool (A) | Standalone + pgpool (B) |
|------------|----------------------|--------------------------|
| Automatic failover | ✅ Consensus-driven | ⚠️ Script-driven, no consensus |
| Split-brain protection | ✅ etcd quorum + TTL + fencing | ❌ Weak (health-check only) |
| Automatic replica rejoin | ✅ `pg_rewind` | ❌ Manual `pg_basebackup` |
| Replication slots | ✅ Automatic | ❌ Manual |
| Centralized config | ✅ via etcd | ❌ Per-node editing |
| Connection pooling | ✅ pgpool | ✅ pgpool |
| Read/write splitting | ✅ pgpool | ✅ pgpool |
| Virtual IP (VIP) | ✅ pgpool watchdog | ✅ pgpool watchdog |
| REST API / CLI ops | ✅ `patronictl` | ❌ PCP only |
| Self-healing | ✅ timers + restart | ❌ |
| Component count | High (PG+Patroni+etcd+pgpool) | Low (PG+pgpool) |
| Learning curve | Steep | Moderate |
| Resource overhead | Higher (etcd) | Lower |
| Write-availability SPOF | etcd quorum | pgpool detection |
| Best for | Production HA, zero-data-loss | Demos, small/low-risk, manual ops |

---

## 5. Recommendation

- **Choose Patroni + pgpool-II (Option A)** for anything that matters: production,
  customer-facing, or where **zero data loss and no split-brain** are requirements.
  This is exactly what this repository builds and has validated with real fault
  injection.

- **Choose standalone + pgpool-II (Option B)** only for a quick lab, a read-mostly
  workload where you accept manual failover, or a place where the operational
  simplicity of "no etcd, no Patroni" outweighs the risk. Treat it as **not HA** in
  the strict sense — it is "manual failover with a VIP."

> **Bottom line:** pgpool-II is a *traffic controller*, not a *consensus engine*.
> Pair it with Patroni when you need real high availability; run it standalone only
> when you are willing to manage failover by hand.
