# Data Integrity & Final Verification

## Workload accounting across all tests

All writes go through the VIP to `txn_track` (database `postgres`, NOT `kyc`) using the
`txn_workload.sh` idempotent insert-returning pattern; confirmed IDs are logged per commit and
compared against the table at rest.

| Metric | Value |
|---|---|
| Final row count (leader) | **11,066** (max id = 11066) |
| Confirmed IDs in final run (7659–11065) | all present |
| `comm -23` (confirmed NOT in table) | **EMPTY** (byte-order sort) |
| Unconfirmed rows | 1 (id 11066) = workload kill-tail (expected; process stopped by operator) |
| Split-brain samples (Tests A1+A2+B) | **0 / 151+** |
| Lost commits (all failure modes) | **0** |

## pgbench smoke (through VIP 192.168.122.200:9999)

- 1,945 transactions, 0 failed
- ~197 tps, avg latency ~20 ms

## Final cluster state (2026-08-12 ~05:2x Z)

- Leader: **db2** (192.168.122.151), timeline **TL 7**
- Replicas: db1, db3 — streaming, **0 lag**
- etcd: **3/3 healthy**
- Systemd self-heal/cluster-health timers: active on all 3 nodes (restored after tests)
- iptables: **0 leftover rules** (all test tags removed)
- Observer/workload processes: stopped, none running

