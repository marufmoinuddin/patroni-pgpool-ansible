# Operations Guide — Daily Commands

> **Scope:** The commands you will run every day against this cluster, grouped by component.

---

## Patroni (run on any node)

```bash
# Cluster status — THE command you will use daily
patronictl -c /etc/patroni/patroni.yml list

# Details about one member
patronictl -c /etc/patroni/patroni.yml show-config

# Planned switchover (move primary to db2) — safe, zero downtime
patronictl -c /etc/patroni/patroni.yml switchover

# Restart a node's PostgreSQL (rolling, Patroni-aware)
patronictl -c /etc/patroni/patroni.yml restart maruf

# Failover NOW (promote a specific replica)
patronictl -c /etc/patroni/patroni.yml failover

# Pause automatic failover (maintenance window)
patronictl -c /etc/patroni/patroni.yml pause
# ... work on the cluster ...
patronictl -c /etc/patroni/patroni.yml resume

# History of leaders/timelines
patronictl -c /etc/patroni/patroni.yml history
```

## etcd

```bash
# Health of the quorum
ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 endpoint health

# Member list
ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 member list

# Inspect Patroni keys
ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 \
  get /percona_lab/maruf --prefix

# Who is the current leader?
ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 \
  get /percona_lab/maruf/leader
```

## pgpool-II

```bash
# Watchdog cluster status (who is leader, who owns the VIP)
pcp_watchdog_info -h localhost -p 9898 -U pgpool_pcp -w

# Node status (backend list)
pcp_node_info -h localhost -p 9898 -U pgpool_pcp -w

# Pool status per database
pcp_pool_status -h localhost -p 9898 -U pgpool_pcp -w

# Attach / detach a backend manually
pcp_attach_node -h localhost -p 9898 -U pgpool_pcp -w -n 1
pcp_detach_node -h localhost -p 9898 -U pgpool_pcp -w -n 1

# pgpool logs
journalctl -u pgpool -f
```

## Backups (pgBackRest — backup node)

```bash
# Backup info
sudo -iu postgres pgbackrest --stanza=maruf info

# Full backup
sudo -iu postgres pgbackrest --stanza=maruf --type=full backup

# Incremental backup
sudo -iu postgres pgbackrest --stanza=maruf --type=incr backup

# Restore to a point in time (example)
sudo -iu postgres pgbackrest --stanza=maruf --type=time \
  --target="2026-08-07 12:00:00" restore
```

## Monitoring (PMM)

- **Web UI:** `https://192.168.122.153:443` — PostgreSQL Overview, replication graphs, query analytics
- **CLI from any PG node:**
  ```bash
  pmm-admin list          # what's being monitored
  pmm-admin status        # agent health
  pmm-admin add postgresql   # add a PostgreSQL service
  ```

## Health & Self-Healing (installed by playbook 07)

```bash
# Self-heal timer status
systemctl status patroni-self-heal.timer
systemctl status cluster-health.timer

# Health monitor log
tail -f /var/log/patroni/cluster_health.log

# Durable leader-event log (every election, read-only leader, etcd quorum loss)
tail -f /var/log/patroni/leader_events.log
```

---

## Further Reading

- [Patroni Internals](../concepts/patroni-internals.md) — what these commands actually drive
- [pgpool-II](../concepts/pgpool.md) — watchdog and VIP under the hood
- [Troubleshooting](../troubleshooting/troubleshooting.md) — when things go wrong
- [Validation](validation.md) — measured failover/recovery numbers