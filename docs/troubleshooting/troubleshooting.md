# Troubleshooting Common Issues

> **Scope:** The issues most likely to bite you, with causes and fixes. Read the [Operations Guide](../operations/operations.md) first for the daily commands referenced here.

| Issue | Likely Cause | Check / Fix |
|-------|--------------|-------------|
| **Patroni won't start** | etcd not reachable | `systemctl status etcd`, `ETCDCTL_API=3 etcdctl endpoint health`; check `patroni.yml` `etcd3.hosts` (all endpoints are listed now). `ExecStartPre` waits `etcd_wait_timeout` (90s) before giving up |
| **etcd quorum not forming** | Stale data from a previous run | On a FRESH bootstrap only: set `etcd_force_reset: true` in variables.yaml and re-run 02 (it wipes + sets `initial-cluster-state: new`). Never `rm -rf /var/lib/etcd/*` on a healthy cluster |
| **No leader after 2 hosts down** | Expected: 3-node etcd needs a 2/3 majority | That's consensus working correctly. For higher tolerance use `etcd_group: "etcd_nodes"` with 3–5 dedicated witnesses (see [Resilience](../concepts/resilience.md)) |
| **Crashed replica won't recover** | Corrupt data dir or DCS hiccup | `patroni-self-heal.timer` restarts a crashed LOCAL member automatically; for a corrupt data dir run `patronictl -c /etc/patroni/patroni.yml reinit maruf <member>` manually (never auto-reinit) |
| **Patroni won't start after reboot** | Patroni raced etcd at boot | `ExecStartPre=/usr/local/sbin/wait_for_etcd.sh` waits for DCS; check `journalctl -u patroni` and `/var/log/patroni/cluster_health.log` |
| **etcd member fails GPG validation** | Fresh OS missing Percona keys | The RPM installs its own key; the playbook uses `disable_gpg_check: true` for the release RPM |
| **Replicas stuck with lag** | Replication slot missing / WAL removed | `patronictl -c /etc/patroni/patroni.yml list`; check `pg_replication_slots`; a full `pg_basebackup` may be needed |
| **VIP not moving** | sudoers / capability issues | `sudoers.d/pgpool-vip` entry present? `journalctl -u pgpool` for vip_up/down errors |
| **Watchdog not forming** | Firewall 9000, auth key mismatch, heartbeat port collision | Open UDP/TCP 9000 (wd_port) and 9694 (heartbeat_port) between nodes; `wd_authkey` identical everywhere; heartbeat_port MUST differ from wd_port; nodes reachable |
| **pgpool rejects config** | Unindexed `wd_*` params | Pgpool 4.5+ requires `wd_port0/1/2` (indexed); remove bare `wd_port`, `wd_authkey`, etc. |
| **Debian installs wrong pgpool** | Percona repo pulls pgpool-II 4.7 libs | Debian MUST use native `pgpool2` 4.3.5: purge `percona-release`/`postgresql-client-common`/`libpgpool2`, `dpkg --configure -a && apt-get -f install`, install native `pgpool2` BEFORE enabling the Percona repo, then `apt-mark hold pgpool2` / pin `4.3.5*` |
| **Cannot connect via VIP** | VIP on wrong node / pgpool not started | `ip addr` (who owns .200?), `pcp_watchdog_info`, `systemctl status pgpool` |
| **pool_passwd auth fails** | MD5 vs SCRAM mismatch | This repo uses a plaintext `pool_passwd` + `pool_hba.conf` (SCRAM-safe); keep file perms 600 |
| **pgBackRest fails** | SSH keys / stanza missing | Run `stanza-create` first; `sudo -iu postgres pgbackrest --stanza=maruf info`; check `repo1-host` |
| **PMM not reachable** | Docker port mapping / firewall | Image listens on 8443 internally → map `-p 443:8443`; open 443 on backup node; iptables FORWARD ACCEPT for 443 |
| **`patronictl restart` hangs** | Interactive prompt | Use `patronictl restart maruf --no-wait` (or `-w` to wait) — never run bare in automation |
| **Deploy fails on a fresh VM** | Missing EPEL/CRB | `dnf install -y epel-release && dnf config-manager --set-enabled crb` before installing pgBackRest deps |

---

## Further Reading

- [Operations Guide](../operations/operations.md) — the command reference used above
- [Manual Deployment](../deployment/manual.md) — dual-distro path differences (especially the Debian pgpool trap)
- [Security](../operations/security.md) — password/auth-related failure modes