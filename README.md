# Patroni + PostgreSQL + pgpool-II High Availability Cluster

> **Automated, validated, 3-node HA PostgreSQL 16** (with pgBackRest + PMM) on CentOS Stream 9 / RHEL 9 and Debian 12 / Ubuntu — deployed with Ansible or by hand.

## Abstract

This repository deploys a **production-grade, 3-node high-availability PostgreSQL 16 cluster** on CentOS Stream 9 (RHEL-family) and Debian 12 Bookworm (Debian-family) VMs using:

- **Percona Distribution for PostgreSQL 16** — the database engine
- **Patroni** — the "brain" that watches the cluster and fails over automatically
- **etcd** — the distributed consensus store that holds the leader lock
- **pgpool-II + Watchdog** — the "traffic controller" that gives applications one Virtual IP (VIP)
  - **CentOS/RHEL:** pgpool-II 4.7 (Percona package, config in `/etc/pgpool-II`, separate `pgpool_watchdog.conf`)
  - **Debian/Ubuntu:** native `pgpool2` 4.3.5 (Debian repo, config in `/etc/pgpool2`, watchdog params inline in `pgpool.conf`)
- **pgBackRest** — backup & point-in-time recovery
- **PMM** (Percona Monitoring & Management) — dashboards and alerts (optional, policy-controlled)

You can deploy everything **automatically with Ansible** (recommended), or **step-by-step by hand** (great for learning exactly what is happening under the hood). Both paths are documented.

> **This architecture has been stress-tested with real fault injection.** 5 consecutive power-loss failovers, an asymmetric network partition test that uncovered and fixed a Patroni `archive-get` hang, a cascading failure test that identified etcd as a write-availability SPOF, and a switchover detection gap (~4 min) that was **fixed and measured** (active `on_role_change` callback + `sr_check_period = 3` → **~3–4 s write-availability gap**, 999/999 confirmed writes survived, 0 lost, no split-brain). See [Validation](docs/operations/validation.md) and [Failure Testing](docs/troubleshooting/failover-testing.md) for the full evidence.

---

## Documentation

The docs live in `docs/`, organized by topic (Percona-style documentation):

| Topic | Document |
|-------|----------|
| **Getting started** | [Quick Start](docs/getting-started/quick-start.md) · [Server Planning](docs/deployment/server-planning.md) |
| **Architecture & concepts** | [Architecture](docs/concepts/architecture.md) · [HA Concepts](docs/concepts/ha-fundamentals.md) · [Patroni Internals](docs/concepts/patroni-internals.md) · [pgpool-II](docs/concepts/pgpool.md) |
| **Deployment** | [Ansible (auto)](docs/deployment/ansible.md) · [Manual (by hand)](docs/deployment/manual.md) · [Repository Contents](docs/deployment/repository-contents.md) |
| **Operations** | [Daily Commands](docs/operations/operations.md) · [Validation](docs/operations/validation.md) · [Resilience](docs/concepts/resilience.md) · [Security](docs/operations/security.md) · [Production Recommendations](docs/operations/production-recommendations.md) |
| **Troubleshooting** | [Common Issues](docs/troubleshooting/troubleshooting.md) · [Failure Testing](docs/troubleshooting/failover-testing.md) · [Step 6 Report](docs/solutions/failover-test-report.md) |
| **Decision guides** | See below |
| **References** | [External References](docs/reference/external-references.md) |

### Decision Guides

| Document | When to read it |
|----------|-----------------|
| [Patroni + pgpool-II vs Standalone pgpool-II](docs/solutions/patroni-pgpool-vs-standalone-pgpool.md) | Evaluating whether you need Patroni or can run pgpool alone |
| [Patroni + pgpool-II vs Patroni + HAProxy + PgBouncer](docs/solutions/patroni-pgpool-vs-haproxy-pgbouncer.md) | Choosing your access layer (pgpool all-in-one vs HAProxy + PgBouncer) |
| [Preferred Primary Node](docs/solutions/preferred-primary-node.md) | Configuring `failover_priority` so db1 is always preferred |
| [Patroni Promotion Mechanism](docs/solutions/patroni-promotion-mechanism.md) | Deep dive on how failover actually works (leader lock, timeline, lag) |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/marufmoinuddin/patroni-pgpool-ansible.git
cd patroni-pgpool-ansible

# 2. Configure
cp hosts.ini.example hosts.ini        # → edit IPs
cp variables.yaml.example variables.yaml  # → fill in passwords
ansible-vault encrypt variables.yaml  # → recommended

# 3. Deploy (takes ~10-15 minutes)
ansible-playbook -i hosts site.yml --ask-vault-pass

# 4. Verify
patronictl -c /etc/patroni/patroni.yml list
psql -h 192.168.122.200 -p 9999 -U postgres -d postgres -c "SELECT 1;"
```

That's it. The cluster is up: apps connect to the **VIP** (`192.168.122.200:9999`) and never touch individual nodes. If the primary dies, Patroni + etcd elect a new one automatically; pgpool-II routes traffic to it; the VIP floats to a surviving pgpool node.
See the full [Quick Start guide](docs/getting-started/quick-start.md) for prerequisites, inventory, variables, and the post-deployment checklist.

---

## Architecture at a Glance

```
App → pgpool-II VIP 192.168.122.200:9999 (watchdog cluster, 3 nodes)
        ↓
   PostgreSQL Primary/Replicas (3 nodes) ← Patroni (leader election via etcd)
        ← etcd (3 nodes, Raft consensus, leader lock)
   ↘ pgpool back to → PG nodes
        ↘ VIP floats via watchdog quorum

Backup node (.153): pgBackRest + PMM Server (Docker)
```

**Patroni owns the data plane** — which node is primary, replication, failover, slots, config.
**pgpool-II owns the access plane** — where clients connect, connection pooling, query routing, VIP.

---

## License

MIT — Adapted from Percona reference architectures and the community HA patterns for PostgreSQL.