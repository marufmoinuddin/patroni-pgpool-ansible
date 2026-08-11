# Failure Testing — Learning by Breaking Things

> 🧪 Do this in a test environment first. It is the only way to trust your HA cluster.

This document is the canonical home for all **failover-testing procedure and
methodology** for the Patroni + pgpool-II cluster. For the actual validation
results — 5 consecutive power-loss failover iterations, zero lost commits,
zero split-brain, per-iteration timelines and durability tables — see
**[Step 6 Failover Validation Report](step6_failover_report.md)**.
The automated harness referenced in Test 5 lives in [`tests/`](../tests/).

---

## Test 1 — Kill the Primary (Automatic Failover)

```bash
# 1. From any node, note the current leader
patronictl -c /etc/patroni/patroni.yml list

# 2. SSH to the leader and kill PostgreSQL HARD (simulates a crash)
#    (on the leader node)
systemctl kill -s SIGKILL patroni
#    or even better — power it off:  poweroff

# 3. Wait ~40 seconds (TTL 30s + loop_wait 10s)
# 4. Check the cluster — a replica should now be Leader
patronictl -c /etc/patroni/patroni.yml list

# 5. Check pgpool followed
pcp_watchdog_info -h localhost -p 9898 -U pgpool_pcp -w

# 6. Reconnect the app — it should still work via the VIP with no code change
psql -h 192.168.122.200 -p 9999 -U postgres -d postgres -c "SELECT now();"
```

**Expected result:** writes fail for ~40 seconds, then resume on the new primary. The VIP keeps serving. **No manual intervention.**

## Test 2 — Kill a Replica

```bash
# Stop Patroni on a replica (simulate a node failing)
systemctl stop patroni        # on db3, for example

# The cluster should stay green — Patroni marks db3 as unavailable
patronictl -c /etc/patroni/patroni.yml list

# Restart it — Patroni brings it back as a streaming replica automatically
systemctl start patroni       # on db3
patronictl -c /etc/patroni/patroni.yml list
```

## Test 3 — Kill the pgpool Watchdog Leader

```bash
# 1. Find which pgpool node owns the VIP
pcp_watchdog_info -h localhost -p 9898 -U pgpool_pcp -w

# 2. Stop pgpool on that node
systemctl stop pgpool         # on the watchdog leader

# 3. Within seconds, another node takes over the VIP
ip addr show eth0 | grep 192.168.122.200   # run on other nodes

# 4. Applications keep connecting to the SAME VIP — nothing changed for them
```

## Test 4 — Planned Switchover (NOT zero-downtime — detection gap)

> ⚠️ **Correction (2026-08-12):** this test was previously labeled
> "Zero Downtime". That is **not true today**. A clean `patronictl switchover`
> does not signal pgpool: `failover_command` only fires when a backend goes
> *down*, and a graceful switchover demotes/promotes without any backend
> outage. pgpool therefore relies on its periodic `sr_check`/health-check
> polling to notice the role change, and writes through the VIP fail with
> `cannot execute CREATE TABLE in a read-only transaction` until it catches up.

```bash
# Move leadership gracefully, e.g. db2 → db1
patronictl -c /etc/patroni/patroni.yml switchover

# Patroni: demotes old leader → promotes new leader → old leader becomes a replica
patronictl -c /etc/patroni/patroni.yml list

# Watch pgpool's view catch up — expect a write blip in between
# (run this loop until node0/db1 shows "primary primary"):
PCPPASSFILE=/etc/pgpool-II/.pcppass pcp_node_info -h 192.168.122.200 -p 9898 -U pgpool_pcp -w 0
```

**Observed behavior (2026-08-11 manual run, three rapid switchovers in 5 min):**
the write blip was **~4 minutes** — switchover completed 15:29:05, pgpool still
routed writes to the old primary at 15:32:55 (VIP writes failed
`read-only transaction`), and re-detected the new primary by ~15:33:22. The
three back-to-back switchovers compounded the lag; a single clean switchover
should be quicker, but the gap is real and must not be sold as zero-downtime.

**Why:** pgpool has no active signal on a clean Patroni role change. `sr_check`
polling (now `sr_check_period = 3` after the fix; was 10 at the time of the
observation) is the polling-only mechanism that updates the primary role, so
a pure-polling design is bounded by polling cadence plus detection time.

**Fix (implemented 2026-08-12, PR #6 — `fix/switchover-detection`):**
a Patroni `on_role_change` callback (`/usr/local/sbin/pgpool_role_signal.sh`,
deployed by `08_Configure_Switchover_Signal.yml`) that, on promotion, confirms
via `patronictl` that this node is the Patroni Leader and immediately runs
`pcp_promote_node` for its backend id on **all** pgpool nodes (including the
VIP-holding watchdog leader). Plus `sr_check_period` tightened 10 → 3 as a
polling safety net. Callbacks must live **inside the `postgresql:` section**
of `patroni.yml` in this build (top-level `callbacks:` is invisible to
`Postgresql.callback` — the original placement failed the retest).

**Measured retest (2026-08-12, one clean switchover db2 → db1, VIP workload +
3-node observer):** write blip **~3–4 seconds** (first failure 20:48:18Z,
writes resumed 20:48:21Z; 28 failed attempts in the window, all retryable
read-only/connection errors, **0 lost commits** — 999/999 confirmed IDs
present on the new primary). The callback fired at 20:48:19Z and pgpool's
`pool_nodes` flipped to `0=up/up/primary` in the same second. No split-brain
samples (never two `recovery=f` at once). Previously: ~4 minutes.

**Honest bar:** still not literal zero, and it should not be sold as such —
an async, connection-pooled architecture always has *some* window between
the old primary demoting and pgpool routing to the new one. Single-digit
seconds is the legitimate target and the fix meets it. The residual window
is the connection teardown/retry latency of in-flight client connections.

## Test 5 — Automated Kill/Recovery Validation (recommended)

The repository ships an automated failover test harness for reproducible
kill → observe → recover → verify cycles. See
[`step6_failover_report.md`](step6_failover_report.md) for a full
5-iteration validation run with zero lost commits and zero split-brain.

```bash
# From your workstation (NOT the db nodes), in the repo checkout:
bash tests/failover_test_harness.sh --targets db1 --action vm-destroy --allow-leader   # dry-run first
bash tests/failover_test_harness.sh --targets db1 --action vm-destroy --allow-leader --execute   # power-loss kill

# Meanwhile, on the hypervisor/VPS:
bash tests/step4_observer.sh ~/deploy/artifacts/run1_iter1 720 2 192.168.122.152   # split-brain observer

# On a surviving node, keep a write workload running THROUGH outage + recovery:
bash tests/txn_workload.sh run1_iter1 2400 /var/lib/pgpool-artifacts/run1_iter1

# After the kill: recover the node, wait for rejoin, then verify durability
bash tests/failover_test_harness.sh --targets db1 --action vm-start --execute
#   comm -23 confirmed.ids table.ids  → must be EMPTY (zero lost commits)
```

**Lessons baked into the harness (do not regress these):**

1. **The workload window must cover the whole recovery** (`2400` seconds = 40 min).
   A short window (e.g. 480s) lets WAL production go idle mid-recovery; the
   rewound former primary then stalls on segment closure and can take 40+ min
   to rejoin. With continuous writes, rejoin is 36–40s.
2. **The workload always seeds from a fresh atomic `SELECT COALESCE(max(id),0)`**
   at startup — never from a remembered/`tail`-tracked value — and on a
   duplicate-key error it retries the colliding ID at most 3 times, then
   auto-resyncs. The old stale-seed path wedged in hundreds of failed retries.
3. **The observer samples `pg_is_in_recovery()` directly on all 3 nodes**
   (not just pool state) every ~2s for 720 samples, which is what proves
   "≤1 primary at all times" — the definitive split-brain check.
4. `vm-destroy` (virsh power loss) is the correct kill primitive; graceful
   stops are not a valid substitute for failover testing.

---

## Related documents

| Document | Purpose |
|----------|---------|
| [`step6_failover_report.md`](step6_failover_report.md) | Actual validation results: 5/5 iterations PASS, per-iteration timelines, durability + split-brain evidence, architectural limitations |
| [`charts/`](charts/) | AntV G2 v5 visualizations of the Step 6 results (failover performance, rejoin vs budget, durability) |
| [`../tests/`](../tests/) | The automated harness: `failover_test_harness.sh`, `step4_observer.sh`, `txn_workload.sh`, `step3_setup.sh` |
