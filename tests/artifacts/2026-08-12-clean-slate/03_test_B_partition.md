# Test B — Asymmetric Network Partition (corrected run)

Method per `tests/failover_test_harness.sh` Step 2 design: partition the PRIMARY from etcd/peers
while leaving it reachable to clients (VIP holder), confirm self-demotion + bounded writable
promotion.

## First attempt (rule-shape mistake, documented for future engineers)

The first injection dropped TCP 5432 to/from **both** peers. That cut the VIP-holder's route to the
*new* primary after self-demotion, so VIP writes hung on `timeout expired` — a harness error, NOT
a cluster defect (verified: `role_signal.log` showed `pcp_promote_node` had fired successfully on
all nodes; Patroni had self-demoted correctly and failsafe-stopped postgres, zero split-brain).

**Lesson (now in `ha-failover-resilience-audit` skill): NEVER drop 5432 to both peers — drop it to
the NON-app peer ONLY; keep it OPEN to the pgpool/VIP holder so the write path keeps flowing.**

## Corrected run — T0 2026-08-12 05:08:28Z (primary = db1, VIP holder = db2)

| Phase | Time (Z) | Observation |
|---|---|---|
| T0 | 05:08:28 | 2379/2380/8008 dropped to/from both peers; 5432 dropped to non-app peer (db3) only |
| Window | 05:08:29→05:08:56 | writes continue, db1 still `f` (primary) |
| Self-demotion | ~05:08:56 | `demoting self because DCS is not accessible`; failsafe stops postgres |
| Promotion | 05:09:07 | **db2 writable Leader (TL 7)**, pool rerouted |
| Recovery | 05:09:12 | in-flight id 9314 retried → CONFIRMED (**0 lost**) |
| Rejoin | 05:09:34 | db1 back in recovery, streaming from db2 |

**Result: bounded writable promotion ~40s — not a hang. 0 split-brain samples throughout.**

