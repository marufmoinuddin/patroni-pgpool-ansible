# Resilience & Self-Healing

> **Scope:** The resilience fixes in this repository so the cluster survives **single-host loss** without manual intervention, and *tells you* when it can't.

---

## DCS (etcd) Redundancy

| Topology | etcd failure tolerance | What you get |
|----------|------------------------|--------------|
| etcd co-located on 3 `pg_nodes` (default) | 1 etcd node | Losing any **2** DB hosts kills quorum → no leader (correct, but fragile) |
| **etcd on 3 dedicated witnesses** (`etcd_group: "etcd_nodes"`) | 1 etcd node, **plus** a DB host crash never touches quorum | DCS and DB failure domains are decoupled |
| etcd on **5 witnesses** | 2 etcd nodes | Tolerates two concurrent host losses end-to-end |

To use dedicated witnesses: add an `[etcd_nodes]` group (odd member count) to `hosts.ini` and set `etcd_group: "etcd_nodes"` in `variables.yaml`. Playbook `02` targets that group; Patroni on `pg_nodes` then talks to **all** etcd endpoints (`etcd3.hosts`), so a local etcd failure never blinds Patroni.

## Fencing: softdog Watchdog (split-brain protection)

`03_Configure_Patroni.yml` loads the kernel **softdog** module and configures Patroni's watchdog (`mode: automatic`, `/dev/watchdog`). If a primary is partitioned and loses DCS quorum, Patroni stops feeding the watchdog → the kernel **reboots the host** instead of letting a stale primary accept writes. Hardware watchdog `i6300ESB` is also supported for stronger guarantees. Disable with `patroni_watchdog: false` (e.g. on hardware with an external BMC).

## Boot Ordering & Restart Policy

- `etcd.service` now waits for `network-online.target`, never has its data directory wiped on re-runs, and uses `initial-cluster-state: existing` when member data already exists (fresh bootstrap still uses `new`).
- `patroni.service` has `Requires=etcd.service` (co-located mode), `After=network-online.target`, an `ExecStartPre` that **waits up to `etcd_wait_timeout` (90s) for a reachable etcd endpoint**, and `Restart=on-failure` with `StartLimitIntervalSec=0` — it never gives up.
- `pgpool.service` got `Restart=always` plus `network-online.target` ordering.
- **Data-dir guards:** playbooks `02`/`03` refuse to run when the etcd or PostgreSQL data directory sits on volatile storage (tmpfs/ramfs) — data must survive reboots.

> ⚠️ Re-running `site.yml` on a healthy cluster **no longer wipes etcd**. Only `etcd_force_reset: true` (fresh bootstrap / DR restore) wipes the DCS.

## pgpool Watchdog Hardening

- `heartbeat_port` (default `9694`) is now defined everywhere — it **must differ** from `wd_port` (`9000`) or watchdog heartbeats collide.
- `wd_quorum_exit = on` (default): a pgpool instance that loses watchdog quorum **exits** instead of serving the VIP alone → no split-brain VIP.
- `wd_authkey` is configurable via `watchdog_authkey` (identical on all nodes).
- Duplicate config keys were removed from `pgpool.conf` (last-wins behaviour was a silent trap).

## Self-Healing (07_Configure_Cluster_Health.yml)

| Timer | Interval | What it does |
|-------|----------|--------------|
| `patroni-self-heal.timer` | 30s | Restarts a **crashed/stopped/failed local** Patroni member. Never touches the leader. Remote crashed members are logged + alerted (manual `patronictl reinit maruf <member>` for corrupt data dirs) |
| `cluster-health.timer` | 60s | Checks etcd quorum, Patroni leader, pgpool watchdog quorum, backend status, VIP presence. Logs to `/var/log/patroni/cluster_health.log`, writes Prometheus textfile metrics for PMM, fires `health_alert_command` on CRITICAL |

Metrics exposed (scraped by PMM's node_exporter textfile collector):
`patroni_leader_present`, `patroni_leader{member=}`, `patroni_members_total`, `patroni_members_nonrunning`, `etcd_healthy`, `etcd_quorum`, `pgpool_wd_quorum`, `pgpool_backends_total/up`, `vip_present`.

Durable event log (Test 4 instrumentation): every leader election/change, leader-lost, DCS-leader-but-read-only false positive, writability-restored, and etcd quorum loss/restore is appended to `/var/log/patroni/leader_events.log` (ISO-8601 timestamps) and counted in monotonic Prometheus counters `patroni_leader_changes_total`, `patroni_leader_read_only_events_total`, `patroni_leader_lost_events_total`, `patroni_etcd_quorum_loss_total` — so a long soak produces a reviewable, queryable record of every transition instead of a flat log you have to grep.

Set `health_alert_command` (e.g. a webhook curl) in `variables.yaml` to get paged *before* an outage becomes permanent.

## Active Switchover Notification (08_Configure_Switchover_Signal.yml)

Patroni's `on_role_change` callback (`files/pgpool_role_signal.sh`) eliminates the ~4-minute polling gap on clean switchover:

1. **Trigger:** Patroni invokes callback on promotion to primary
2. **Authority check:** Confirms via `patronictl list -f json` that THIS node holds the DCS leader lease (etcd is the single source of truth — blocks old primary during any split-brain window)
3. **Mapping:** Maps local hostname/IP → pgpool backend node_id from `pgpool.conf`
4. **Notification:** Runs `pcp_promote_node` on ALL pgpool nodes (including the VIP-holding watchdog leader, since `pcp_listen_addresses='*'`)
5. **Pre-flight:** If pgpool marks this node down but backend is up, runs `pcp_attach_node` first (idempotent)

Also reduced `sr_check_period` from 10s → 3s in `04_Configure_Pgpool.yml` as a safety net (bounds the residual window; overhead: 1 trivial query/backend/3s — negligible).

## What About "All Hosts Down"?

No HA topology survives every node dying at once — that is disaster recovery, not high availability. Documented restore path: bring etcd up first (members with `initial-cluster-state: existing`), then Patroni on one node, then the rest — or restore from pgBackRest if data is unrecoverable. The fixes above buy you: **losing any single host** (or two, with 5-node etcd) with **no total outage**.

## Named Architectural Finding: DCS is a Single Point of Failure for Write Availability

> **Finding (confirmed by Test 3 — mixed/cascading failure, 2026-08-11):**
> **etcd is a single point of failure for *write availability* in this architecture.** During Test 3, two of three PostgreSQL nodes were fully healthy throughout, and yet **all writes stopped completely** because the etcd cluster lost quorum (one member killed + 30% packet loss between the two survivors made a 2-of-3 quorum unreachable). The cluster correctly chose **safe-unavailable** (zero primaries, read-only, no split-brain, no data loss — 53/53 confirmed writes survived), but the outcome stands: a healthy database cannot accept writes without a healthy DCS.
>
> **Implication:** DCS availability is the upper bound on write availability. Any failure that degrades etcd quorum — even while every Postgres node is healthy — takes the entire cluster read-only.
>
> **Proposed mitigation (cross-ref DCS Redundancy above):** decouple the DCS failure domain from the database failure domain so a DB-host problem cannot take quorum down with it. Concrete options, in increasing order of cost:
> 1. **Dedicated etcd witness nodes** (`etcd_group: "etcd_nodes"`) — etcd no longer co-locates with Postgres, so a DB host crash never touches quorum.
> 2. **5-node etcd topology** — tolerates two concurrent etcd losses, end-to-end, instead of one.
> 3. Combination of the above (dedicated witnesses *and* 5 members) for the strongest separation.
>
> This is an architectural recommendation for a follow-up decision — **not a blocker** for the current single-host-loss HA guarantees, which are unaffected.

## Known Non-Blockers (follow-up, not required for HA acceptance)

| Item | Status | Action |
|------|--------|--------|
| br1 under-provisioned (2 vCPU running ClickHouse + Docker + Grafana + VictoriaMetrics + PMM; thrashes to loadavg 60+; caused the Test 2 sshd wedge) | Open — not a blocker | Resize br1 or split the metrics stack (e.g. PMM server on its own host) |
| pgbackrest **archive-push** path can silently wedge for hours with no monitoring alert (observed ~2.5h before Test 2) | Open — not a blocker | Add an archive-staleness/backlog check (oldest unarchived WAL age) to the health monitor |
| PMM monitoring | **Disabled by policy** | PMM stays off on br1; do not redeploy; skip PMM checks in test reports |

---

## Further Reading

- [Validation](../operations/validation.md) — the fault-injection evidence behind these fixes
- [FAILOVER_TESTING.md](../troubleshooting/failover-testing.md) — how to reproduce the tests
- [Operations Guide](../operations/operations.md) — health timers and event logs