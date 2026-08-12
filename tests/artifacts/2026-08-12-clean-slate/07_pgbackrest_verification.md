# pgBackRest Verification (clean-slate run)

Stack archives over **SSH** (`repo1-host=percona-pgbackrest` on pg nodes, `pg-host` on br1) — the
`pgbackrest server` TLS daemon is NOT used (see `README` troubleshooting: `pgbackrest.service` is
expected to be failed, and is now **masked** by playbook 05).

## Commands that matter

- Config: `/etc/pgbackrest.conf` (NOT `/etc/pgbackrest/pgbackrest.conf`)
- Stanza: **`kyc`** (NOT `pg1`)
- Data dir (per stanza): `/var/lib/pgsql/16/data/kyc` (workload database is `postgres`/`txn_track`)

## Check results (2026-08-12)

```
$ pgbackrest --stanza=kyc --config=/etc/pgbackrest.conf check
P00 INFO: check command end: completed successfully (3140ms)
```

- Stanza `kyc`: **ok**
- Full backup exists: `20260812-002436F` — db size 22.3MB, backup size 3MB
- WAL archiving verified end-to-end: segment `00000007000000000000000F` switched on the leader and
  arrived in the repo on br1 over SSH (timeline 7 range being archived, min/max
  `000000010000000000000006` / `00000007000000000000000E`)

## Note for future runs

`pgbackrest --stanza=pg1 check` will fail with "stanza path missing" — that is the wrong stanza
name. Always use `--stanza=kyc`.

