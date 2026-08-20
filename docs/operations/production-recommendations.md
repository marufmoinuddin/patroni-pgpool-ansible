# Production Recommendations

1. **Change the VIP interface** — the playbook defaults to `eth0`; verify with `ip link` and set `vip_interface` accordingly (e.g. `ens3`, `enp1s0`).
2. **Tune failover timing** — the defaults (`ttl: 30`, `loop_wait: 10`) give ~40s failover. For faster failover, lower `ttl` to 15–20s (but keep it comfortably above `loop_wait`).
3. **Set `maximum_lag_on_failover` sensibly** — 1 MB (current default) prevents promoting a far-behind replica; consider 100 MB–1 GB for busy workloads so a slightly-lagged replica can still be promoted.
4. **Use `wal_keep_size`** instead of the legacy `wal_keep_segments` on PG 16 if you tune WAL retention manually (Patroni's defaults with slots are fine for most setups).
5. **Separate disks** — put `/postgres/data` and `/var/lib/etcd` on dedicated NVMe/SSD storage; etcd is latency-sensitive.
6. **Automate the first backup** — the playbook intentionally stops at printing the pgBackRest commands. In production, schedule:
   ```bash
   # cron on the backup node
   0 1 * * * sudo -iu postgres pgbackrest --stanza=maruf --type=incr backup
   ```
7. **Test failover monthly** — run the procedures in [FAILOVER_TESTING.md](../troubleshooting/failover-testing.md) on a schedule. A HA cluster you never test is a false promise.
8. **Monitor the monitors** — PMM alerting should include: Patroni node down, etcd quorum lost, replica lag, backup age, VIP owner changes.
9. **Keep the deployment reproducible** — the whole point of this repo: one `ansible-playbook` run rebuilds the world. Store your customized `hosts` + vars in Git (secrets in Vault).
10. **Backup the etcd data too** — etcd holds the cluster brain (`/percona_lab/maruf/*`). A full backup strategy includes `etcdctl snapshot save`.

---

## Further Reading

- [Security](security.md) — password and exposure hardening
- [Resilience](../concepts/resilience.md) — architecture-level hardening (witness etcd, fencing, self-healing)