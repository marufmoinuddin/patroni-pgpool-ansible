# Test A — Power-Loss Failover (virsh destroy), 2 iterations

Method per `tests/failover_test_harness.sh`: continuous `txn_track` write workload through the VIP
(runner on db2, artifact dir `/var/lib/pgpool-artifacts/`), observer sampling all nodes every 2 s
(runner on VPS), then hard `virsh destroy` of the current leader via the harness's PTY
confirmation gate. Leader-guard verified: harness refuses `vm-destroy` without `--allow-leader`.

## Iteration 1 — leader db1 (192.168.122.150)

| Time (Z) | Event |
|---|---|
| 04:35:29 | T0: `virsh destroy db1` (power loss, no graceful stop) |
| 04:35:55 | TTL-expiry path: db3 `promoted self to leader by acquiring session lock` (TL 2) |
| 04:35:59 | first successful write after T0 — in-flight ID 1341 retried → CONFIRMED (**30 s blip**) |
| 04:39:39 | db1 back streaming (TL 2, 0 lag) after vm-start; rejoin within 10-min rule |

FAILED events: 6 (all retries of the single in-flight ID 1341, then confirmed). Lost commits: **0**.

## Iteration 2 — leader db3 (192.168.122.152)

| Time (Z) | Event |
|---|---|
| 04:40:26 | T0: `virsh destroy db3` |
| ~04:41:01 | db2 promoted (TL 3) |
| 04:41:04 | first successful write after T0 — in-flight ID 3836 retried → CONFIRMED (**38 s blip**) |
| ~04:45:30 | db3 back streaming (TL 3, 0 lag); rejoin within 10-min rule |

FAILED events: 7 (all retries of the single in-flight ID 3836, then confirmed). Lost commits: **0**.

## Split-brain scan

Observer log (`observe_iter.log`), 151 samples across both iterations:
**0 samples with ≥ 2 primaries.** Each failover had exactly one `recovery=f` node at every poll.

## Outcome

Power-loss failover works from a clean deploy: TTL-expiry election in ~26 s, write path restored in
30–38 s, in-flight transaction retried and committed (idempotent insert), zero data loss, zero
split-brain, destroyed nodes rejoin cleanly with pg_rewind.
