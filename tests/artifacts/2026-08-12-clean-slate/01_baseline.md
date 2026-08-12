# Baseline — pre-test cluster state (2026-08-12)

Taken after `site.yml` completed, before any failure injection.

| Check | Result |
|---|---|
| Patroni | db1 **Leader**, db2/db3 streaming, TL 1, 0 lag |
| etcd | 3/3 healthy |
| Watchdog | QUORUM EXIST, db1 LEADER, VIP claimed |
| pool_nodes | 1 primary + 2 standby, all `up` |
| VIP write (`txn_track` id=1) | ✅ |
| pg_is_in_recovery | exactly one `f` (db1), db2/db3 `t` |
| pgBackRest | stanza `maruf`, `pgbackrest check` OK, full backup present |

## Notes

- Pool/watchdog checks require `PCPPASSFILE=/etc/pgpool-II/.pcppass` exported into the session (the
  PCP auth failed once when the env var wasn't inherited through nested ssh).
- Recovery sampling must be run per-node (`psql` against each node directly); a single-host sample
  accidentally running all three queries on db1 produced misleading output in an earlier session.
