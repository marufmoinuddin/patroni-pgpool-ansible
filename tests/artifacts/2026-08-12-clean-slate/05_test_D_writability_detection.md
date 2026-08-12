# Test D — Leader-Writability Detection (live + synthetic blip smoke)

Validates the Fix-2 detection layer (`/usr/local/sbin/cluster_health.sh` + systemd timer):
the checker must report CRITICAL when DCS says a leader is elected but the leader's REST endpoint
reports the node as not writable (role unknown / pg_is_in_recovery=true), and recover cleanly.

## Smoke procedure (2026-08-12, leader = db2)

1. Live run → expect rc=0, RESULT OK.
2. Enforced window: drop TCP 8008 (Patroni REST) from db1 to db2 for ~6s; run checker inside window
   → expect rc=1 + `false_positive` event.
3. Restore → expect rc=0 + `leader_writable_restored` event.

## Results

| Sample | Checker result | rc | Event logged |
|---|---|---|---|
| Live | `RESULT OK`, leader db2 writable=1, etcd 3/3 | 0 | — |
| Blip | `CRITICAL leader db2 is NOT writable (REST role=unknown) - DCS leader but read-only` | 1 | `false_positive leader=db2 not_writable role=unknown` |
| Restore | `RESULT OK`, writable=1 | 0 | `leader_writable_restored member=db2` |

Counters: `READ_ONLY_EVENTS_TOTAL` 1→2, `LEADER_LOST_TOTAL` 0.
The checker's state file also recorded correct events through all three prior failures (A/B/C).

