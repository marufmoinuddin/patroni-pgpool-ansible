# Deployment Result — Validated

> **Scope:** Evidence that this architecture works under real fault injection. This is a summary — the full per-iteration data lives in [step6_failover_report.md](../solutions/failover-test-report.md) and the procedures in [FAILOVER_TESTING.md](../troubleshooting/failover-testing.md).

This architecture has been **deployed end-to-end and validated on real hardware (kernel-level VMs), not just designed**. The automated failover harness in [`tests/`](../../tests/) was used to power-loss kill the cluster's leader five times in a row (`virsh destroy` — no graceful stops), observe every failover, and verify the recovery.

## Validated Headline Facts

- ✅ **5 / 5 consecutive power-loss failover iterations passed** (kills rotated across all three nodes: db1 → db3 → db1 → db3 → db2)
- ✅ **Zero lost commits** across ~104,000 confirmed writes (seed-line `comm -23` vs the actual table, every run)
- ✅ **Zero split-brain** across **3,600 direct node probes** (720 samples × 5 iterations; ≤1 node primary at all times)
- ✅ **~40s median failover** (38–43s from power loss to first successful write on the new primary), write interruption 34–38s
- ✅ **Killed-node rejoin 36–40s** — far inside the ≤10-minute budget, via Patroni's normal re-bootstrap (`pg_rewind` + WAL catch-up); no Ansible re-provisioning, no manual intervention
- ✅ **Single-node failure tolerance confirmed** — the cluster survives losing *any one* host (database node *or* etcd quorum member). It does **not** survive losing two hosts at once; that requires the 3–5 witness etcd topology (see [Resilience](../concepts/resilience.md)).

## Additional Fault-Injection Validation (beyond the 5 clean kills)

| Test | Scenario | Finding | Resolution |
|------|----------|---------|------------|
| **Test 1** | Watchdog timing audit | Config math correct (TTL=30s, safety_margin=5s, hardware i6300ESB available) | ✅ Passed |
| **Test 2** | **Asymmetric network partition** (iptables cut primary off from etcd/peers but left client-facing VIP open) | Primary self-demoted in 17–35s ✅, but promotion **hung >2h** due to `pgbackrest archive-get` without timeout on wedged SSH to backup node (upstream Patroni #3603) | **Fixed:** `restore_command: "timeout 60 pgbackrest..."`, pgbackrest `protocol-timeout`, SSH `ServerAliveInterval=10`, + "leader but read-only" detection check. Re-run: **31s promotion, 140/140 writes survived** |
| **Test 3** | **Mixed/cascading failure** (etcd node loss + 30% packet loss on survivors) | **DCS (etcd) is a single point of failure for write availability** — 2/3 PG nodes healthy, 0 writable primaries because 2-of-3 etcd quorum unreachable. Cluster correctly chose safe-unavailable (0 data loss) | Documented as architectural limitation; mitigations: dedicated etcd witnesses, decoupled DCS failure domain, 5-node etcd topology |
| **Test 4** | Durable false-positive logging (soak instrumentation) | Built append-only event log + Prometheus counters; smoke test found a 2nd bug: a timed-out leader REST probe produced an empty `LEADER_ROLE=""` which skipped the writability guard, so a stuck leader passed as healthy | **Fixed:** empty/timeout → `role="unknown"` → NOT writable |
| **Live discovery** | Manual planned switchover (`patronictl switchover`) | pgpool has no active signal on clean switchover — relies on `sr_check` polling → **~4 min write-availability gap** observed | **Fixed & verified:** `pgpool_role_signal.sh` callback (Patroni `on_role_change` → `pcp_promote_node` on all pgpool nodes) + `sr_check_period 10→3` → **~3–4 s gap, 999/999 writes survived, 0 lost, no split-brain** |

## Realistic Application Failover Testing (2026-08-20)

Beyond the shell harness, the stack is validated with a **realistic customer-facing application** — two Python scripts that together prove what real users experience during a failover:

| Script | Role |
|--------|------|
| [`tests/bulk_failover_test.py`](../../tests/bulk_failover_test.py) | **Failover trigger** — detects the leader (or pgpool/VIP node) and kills it. Failover-only; no data/verify logic. |
| [`tests/app_simulator.py`](../../tests/app_simulator.py) | **Realistic customer app** — fires purchase requests at the VIP with **zero cluster awareness**; never waits for the DB. |

**Why two scripts:** a failover-aware script that "waits for the DB" does **not** prove production failover — a real customer never waits. The app simulator behaves exactly like a real app: it keeps trying to buy during the incident and records every outcome (SUCCESS / FAILED / UNCERTAIN), then audits that no customer was wrongly charged.

**Validated results (all scenarios):**
- ✅ **Zero data loss** across crash (SIGKILL), graceful, extreme (3 sequential failovers), and keep-down (node stays dead) scenarios — 50/50 confirmed, 0 lost, 0 extra, balance exact
- ✅ **Money consistency** — balance always equals `seed − (purchases × price)`; no lost charge, no double charge
- ✅ **Real failovers observed** — leader changes like `db1 → db2`, `db2 → db1 → db2 → db3` (timeline advanced each time)
- ✅ **Self-healing** — killed nodes rejoin as replicas and catch up from archive (0 lag after recovery); pgpool watchdog moved the VIP (db1 → db2) when the VIP node died
- ⚠️ **Read availability measured, not assumed** — concurrent read probe through the VIP: **89%** on default config, **95.71%** after tuning `health_check_period` (leader-failover scenario). Reads are highly available, not perfect; the gap is understood and tunable.

> ⚠️ **Honest caveat — async replication.** Replication is asynchronous (`synchronous_standby_names` not set). A transaction committed on the old primary moments before a power loss can, in the worst case, be absent from the promoted replica. Zero lost commits were observed across all five runs, but that is empirical evidence, not a design guarantee: for zero-RPO the stack must be switched to synchronous replication. The 34–38s write interruption is the client-visible failover window (pgpool health-polling + Patroni election + attach cycle) — not zero downtime; stateful clients must retry.

---

## Further Reading

| Document | Purpose |
|----------|---------|
| [`FAILOVER_TESTING.md`](../troubleshooting/failover-testing.md) | How to run the tests: manual Tests 1–4 + automated harness (Test 5) |
| [`step6_failover_report.md`](../solutions/failover-test-report.md) | What actually happened: 5/5 PASS, timelines, durability, split-brain, limitations |
| [`charts/`](../charts/) | AntV G2 v5 visualizations of the Step 6 results |
| [Resilience & Self-Healing](../concepts/resilience.md) | Architectural fixes derived from these tests |