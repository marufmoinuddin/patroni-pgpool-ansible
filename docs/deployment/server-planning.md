# Server Planning — Before You Install Anything

> **Scope:** Reference topology, why 3 nodes, hardware sizing, and DNS/hostname requirements. Read this before deploying.

---

## Reference Architecture (3 Nodes + 1 Backup Node)

| Node | IP Address | Components |
|------|-----------|------------|
| db1 (`percona-node-1`) | 192.168.122.150 | PostgreSQL 16, Patroni, etcd, pgpool-II |
| db2 (`percona-node-2`) | 192.168.122.151 | PostgreSQL 16, Patroni, etcd, pgpool-II |
| db3 (`percona-node-3`) | 192.168.122.152 | PostgreSQL 16, Patroni, etcd, pgpool-II |
| db-backup (`percona-pgbackrest`) | 192.168.122.153 | pgBackRest server + PMM Server (Docker) |

**Applications connect to the Virtual IP `192.168.122.200:9999`** (served by whichever node is the pgpool-II watchdog leader).

## Why 3 Database Nodes?

- **PostgreSQL:** 1 primary + 2 replicas (tolerates 1 replica loss and still has HA)
- **etcd:** 3 nodes = quorum of 2 (tolerates 1 etcd node loss)
- **pgpool-II:** 3 nodes = watchdog quorum of 2 (tolerates 1 pgpool node loss)

## Hardware Sizing Guidelines (Per Node)

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| CPU | 4 vCPU | 8+ vCPU | Patroni + PostgreSQL + etcd + pgpool-II all run here |
| RAM | 8 GB | 32+ GB | PostgreSQL `shared_buffers` = 25–40% of RAM; etcd needs low-latency memory |
| Disk (OS) | 50 GB | 100 GB | Root filesystem |
| Disk (PostgreSQL data) | 100 GB | 500+ GB NVMe | Separate disk for `/postgres/data` — critical for performance |
| Disk (etcd) | 10 GB | 50 GB NVMe | Separate disk for `/var/lib/etcd` — etcd is latency-sensitive |
| Network | 1 Gbps | 10 Gbps | Low latency between nodes is essential for replication and etcd |

## DNS and Hostname Resolution

All nodes must resolve each other by hostname. Configure via `/etc/hosts` or internal DNS:

```
# /etc/hosts on ALL nodes
192.168.122.150  db1
192.168.122.151  db2
192.168.122.152  db3
192.168.122.153  db-backup
192.168.122.200  pgpool-vip
```

**Test:** `ping db1`, `ping db2`, `ping db3` from each node — all must succeed.

---

## Further Reading

- [Ansible Deployment](ansible.md) — the automated path
- [Manual Deployment](manual.md) — the hand-by-hand path
- [Architecture Overview](../concepts/architecture.md) — network endpoints and component roles