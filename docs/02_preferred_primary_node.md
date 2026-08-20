# Can We Have a "Preferred" Node That Is Always Primary When Available?

> **Short answer: Yes.** Patroni supports this natively via the **`failover_priority`**
> tag (formerly `standby_priority`). You can make one node (e.g. `db1`) the preferred
> primary so that, whenever it is healthy and in sync, Patroni will steer leadership
> back to it — automatically after a failover, or on demand with a switchover.
>
> This document explains the mechanism, how to configure it in **this repository's**
> Ansible playbooks, and the important caveats (it is *preference*, not *guarantee*).

---

## 1. The 3-Node Architecture (for reference)

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

- **db1** = `192.168.122.150` (in this repo, the first node in `[pg_nodes]`)
- **db2** = `192.168.122.151`
- **db3** = `192.168.122.152`
- **VIP** = `192.168.122.200:9999` (pgpool-II watchdog)

---

## 2. How Patroni Chooses a Leader

Patroni does **not** pick a leader by "whoever is fastest." It uses a **priority
number** stored per member in etcd. When the current leader's lock expires (or on a
manual switchover), Patroni looks at the **`failover_priority`** of each candidate
replica and promotes the one with the **highest** value.

- Higher `failover_priority` = more preferred to become primary.
- A node with `failover_priority: 0` is **never** promoted (it can only be a replica).
- The **current leader** always keeps leadership until it loses the lease — priority
  only matters at *election time*.

> **Important:** `failover_priority` is a *preference*, not a hard guarantee. It is
> honored when the preferred node is **healthy and caught up** at the moment of
> election. If the preferred node is down or lagging, Patroni will promote the next
> best candidate rather than wait.

---

## 3. Two Ways to Get "Always Primary When Available"

### 3.1 Automatic failback (recommended) — `failover_priority`

Set `db1` to the highest priority. Then:

1. If `db1` is primary and dies, Patroni fails over to `db2` (or `db3`).
2. When `db1` comes back and rejoins as a healthy, in-sync replica, Patroni
   **automatically switches leadership back to `db1`** (because it has the highest
   `failover_priority`).

This gives you a **self-healing "preferred primary"** with no manual step.

### 3.2 Manual switchover — `patronictl switchover`

Even without automatic failback, you can always force leadership back to a node:

```bash
# Move leadership to db1 (interactive)
patronictl -c /etc/patroni/patroni.yml switchover maruf --master db2 --candidate db1

# Non-interactive (for scripts)
patronictl -c /etc/patroni/patroni.yml switchover maruf --master db2 --candidate db1 --force
```

This is a **graceful** operation (zero data loss) and is what you'd run for planned
maintenance or to "return home" after a failover.

---

## 4. How to Configure It in THIS Repository

### 4.1 The current state

In `03_Configure_Patroni.yml`, every node currently gets identical tags:

```yaml
tags:
    nofailover: false
    noloadbalance: false
    clonefrom: false
    nosync: false
```

There is **no `failover_priority`** set today, so all three nodes are equally
preferred. To make `db1` the preferred primary, add a `failover_priority` that is
**highest on db1** and lower on db2/db3.

### 4.2 Recommended change to `03_Configure_Patroni.yml`

The playbook already computes `node_index` (1-based position in `[pg_nodes]`) and
`is_primary` (true for the first node). You can derive a priority from it. For
example, make the **first node** the preferred primary:

```yaml
# Inside the "Create Patroni configuration file" task, in the tags: block
tags:
    nofailover: false
    noloadbalance: false
    clonefrom: false
    nosync: false
    # Higher = more preferred to become primary.
    # db1 (node_index 1) -> 100, db2 -> 50, db3 -> 0 (never primary).
    failover_priority: "{{ 100 if node_index == 1 else (50 if node_index == 2 else 0) }}"
```

> **Note:** `failover_priority` is a **dynamic** DCS setting. It is read from etcd,
> so you can also change it at runtime with `patronictl edit-config` or by editing the
> member's tags — you do **not** need to redeploy the whole cluster to change
> preference.

### 4.3 A cleaner, explicit approach (per-node)

If you prefer explicit control, set the priority per host. Add a variable in
`variables.yaml`:

```yaml
# Preferred primary = db1 (highest priority). 0 = never primary.
failover_priority_map:
  patroni-1: 100
  patroni-2: 50
  patroni-3: 0
```

Then in `03_Configure_Patroni.yml`:

```yaml
tags:
    nofailover: false
    noloadbalance: false
    clonefrom: false
    nosync: false
    failover_priority: "{{ failover_priority_map[inventory_hostname] | default(1) }}"
```

### 4.4 Verify it took effect

```bash
# From any node
patronictl -c /etc/patroni/patroni.yml list

# Inspect a member's tags / priority
patronictl -c /etc/patroni/patroni.yml show-config maruf
```

You should see `db1` with the highest `failover_priority`.

---

## 5. What About pgpool-II? (It Follows Patroni)

pgpool-II does **not** need to know about `failover_priority`. It simply routes
writes to whatever node Patroni says is primary (via `sr_check` + the `on_role_change`
callback in this repo). So once Patroni steers leadership back to `db1`, pgpool
automatically follows — the VIP keeps serving, and the app sees no change.

> **Tip:** If you want the **pgpool watchdog leader** (the node that owns the VIP) to
> also prefer `db1`, raise its `wd_priority`. In this repo that is controlled by
> `wd_priority_base` in `variables.yaml` (higher = more likely to be watchdog leader).
> This is independent of the PostgreSQL primary, but keeping both on the same node can
> reduce cross-node hops.

---

## 6. Caveats and Honest Expectations

1. **It is preference, not a hard guarantee.** If `db1` is down or lagging at election
   time, Patroni promotes the next-best node. It will *not* wait for `db1`.

2. **Automatic failback causes a switchover.** When `db1` returns and Patroni hands
   leadership back, that is a real (graceful) switchover — a brief write blip
   (in this repo, ~3–4 s with the callback fix). If you want to avoid the blip, use
   **manual switchover** instead of automatic failback.

3. **`failover_priority: 0` means "never primary."** Use this deliberately — a node
   with priority 0 can never be promoted, which reduces your HA options if the other
   two are down.

4. **Keep priorities sensible.** With 3 nodes, a common pattern is `100 / 50 / 0`
   (one preferred, one backup, one never-primary) or `100 / 50 / 25` (all can be
   primary, but in a fixed order). Do not set all three equal if you want a clear
   preference.

5. **`maximum_lag_on_failover` still applies.** A preferred node that is too far
   behind will not be promoted even if it has the highest priority.

---

## 7. Summary

| Question | Answer |
|----------|--------|
| Can we have a node that is always primary when available? | **Yes** — via Patroni `failover_priority` |
| How? | Give `db1` the highest `failover_priority` (e.g. 100), lower on db2/db3 |
| Automatic failback? | Yes — Patroni hands leadership back when the preferred node rejoins healthy |
| Manual alternative? | `patronictl switchover` to force leadership back |
| Does pgpool need changes? | No — it follows Patroni automatically |
| Is it a hard guarantee? | No — preference only; honored when the node is healthy and in sync |
| Where to configure in this repo? | `03_Configure_Patroni.yml` → `tags.failover_priority` (or via `patronictl edit-config`) |
