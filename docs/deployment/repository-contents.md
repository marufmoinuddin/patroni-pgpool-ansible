# Repository Contents — What's Inside

> **Scope:** Layout of this repository, the playbook sequence, and the default values you should know before running anything.

---

## Directory Layout

```
patroni-pgpool-ansible/
├── site.yml                        ← Master playbook: runs 01→08 in order
├── hosts.ini.example               ← Inventory template: 3 PG nodes + 1 backup (copy to hosts.ini)
├── ansible.cfg                     ← Ansible settings (root-based, no sudo prompts)
├── README.md                       ← This document's abstract (entry point)
├── 01_Install_Percona.yml          ← Repos, packages, prerequisites
├── 02_Configure_Etcd.yml           ← 3-node etcd cluster
├── 03_Configure_Patroni.yml        ← PostgreSQL 16 + Patroni HA bootstrap
├── 04_Configure_Pgpool.yml         ← pgpool-II 4.7/4.3.5 + Watchdog + VIP (OS-conditional)
├── 05_Configure_Pgbackrest.yml     ← pgBackRest server + client integration
├── 06_Install_Pmm_Monitoring.yml   ← PMM Server (Docker) + PMM Client
├── 07_Configure_Cluster_Health.yml ← Self-healing timers + health monitor + Prometheus metrics
├── 08_Configure_Switchover_Signal.yml ← Patroni on_role_change callback → pgpool active notification
├── tests/                          ← Fault-injection + realistic-app harness
│   ├── bulk_failover_test.py       ← Failover trigger: kill the leader/VIP node (failover-only)
│   ├── app_simulator.py            ← Realistic customer app: fires purchases, never waits
│   ├── failover_test_harness.sh    ← Shell fault-injection harness (Tests 1-5)
│   ├── step4_observer.sh           ← Split-brain / routing monitor
│   ├── txn_workload.sh             ← Transaction write workload
│   └── artifacts/                  ← Test reports (.report.txt, .summary.json, .events)
├── docs/                           ← All documentation (Percona-style topic tree)
│   ├── getting-started/            ← Quick start
│   │   └── quick-start.md          ← Fastest path: clone → configure → deploy
│   ├── concepts/                   ← Architecture & mental model
│   │   ├── architecture.md         ← Component diagram, data flow, network endpoints
│   │   ├── ha-fundamentals.md      ← HA mental model (streaming replication, split brain, quorum)
│   │   ├── patroni-internals.md    ← How Patroni bootstraps, heartbeats, fails over
│   │   ├── pgpool.md               ← pgpool-II role, watchdog/VIP, dual-distro packaging
│   │   └── resilience.md           ← Resilience & self-healing
│   ├── deployment/                 ← How to deploy
│   │   ├── ansible.md              ← Method A: automated deployment
│   │   ├── manual.md               ← Method B: manual deployment (no Ansible)
│   │   ├── repository-contents.md  ← This file: playbook sequence + defaults
│   │   └── server-planning.md      ← Topology, sizing, DNS
│   ├── operations/                 ← Day-2 operations
│   │   ├── operations.md           ← Daily commands (patronictl, etcdctl, pcp_*, backups)
│   │   ├── validation.md           ← Deployment result — validated evidence
│   │   ├── security.md             ← Change these before production
│   │   └── production-recommendations.md ← Production hardening
│   ├── troubleshooting/            ← When things go wrong
│   │   ├── troubleshooting.md      ← Common issues and fixes
│   │   └── failover-testing.md     ← How to run Tests 1-5
│   ├── solutions/                  ← Decision guides & deep dives
│   │   ├── patroni-pgpool-vs-standalone-pgpool.md ← Decision: Patroni vs standalone
│   │   ├── preferred-primary-node.md ← failover_priority deep-dive
│   │   ├── patroni-pgpool-vs-haproxy-pgbouncer.md ← Access-layer comparison
│   │   ├── patroni-promotion-mechanism.md ← Promotion mechanism deep-dive
│   │   └── failover-test-report.md ← 5/5 PASS evidence with timelines, durability, split-brain
│   ├── reference/                  ← Links & references
│   │   └── external-references.md  ← External references
│   └── charts/                     ← AntV G2 v5 visualizations of validation results
├── files/                          ← Supporting scripts deployed by playbooks
│   └── pgpool_role_signal.sh       ← Patroni callback for active switchover notification
├── variables.yaml.example          ← All secrets + tunables (copy → variables.yaml, encrypt with ansible-vault)
└── SKILLS.md                       ← Internal skill references for development
```

> 💡 **Design choice:** every config file is written **inline** in the playbooks (via `copy: content: |`) — no `templates/` directory. This makes each playbook fully self-contained: you see the exact config being deployed without opening another file.

## Playbook Sequence

| # | Playbook | What It Does (Plain English) |
|---|----------|------------------------------|
| 01 | `01_Install_Percona.yml` | Adds the Percona repository, enables EPEL + CRB (RHEL), installs PostgreSQL 16, Patroni, etcd, pgpool-II, pgBackRest, jq — and **purges any old/broken installs** so you start clean. On **Debian**: installs native `pgpool2` **BEFORE** enabling Percona repo, then pins `libpgpool2=4.3.5*` to prevent version conflicts with Percona's PostgreSQL 16 modules. |
| 02 | `02_Configure_Etcd.yml` | Writes the etcd config on all 3 nodes, **wipes stale etcd data** (so a re-run bootstraps cleanly), starts etcd, and verifies quorum. |
| 03 | `03_Configure_Patroni.yml` | Creates the PostgreSQL data directory, writes `patroni.yml` (the full HA config with pgtune-calculated parameters, watchdog, callbacks), installs the systemd unit with `ExecStartPre` waiting for etcd, **starts the primary first**, waits, then starts replicas — then verifies with `patronictl list`. |
| 04 | `04_Configure_Pgpool.yml` | Writes `pgpool.conf` + **OS-conditional watchdog config** (CentOS: separate `pgpool_watchdog.conf` with 4.7 params; Debian: inline in `pgpool.conf` with legacy 4.3.5 params), `pool_hba.conf` + `pool_passwd` (plaintext for SCRAM) + `pcp.conf`, deploys Patroni-aware `failover.sh` / `follow_master.sh`, sets `pgpool_node_id`, and starts the watchdog cluster so the VIP is claimed. **Auto-detects VIP interface** (`eth0` on CentOS, `enp3s0` on Debian). |
| 05 | `05_Configure_Pgbackrest.yml` | Installs/connects pgBackRest on the backup node, exchanges SSH keys with all PG nodes (using `StrictHostKeyChecking accept-new` for non-interactive automation), writes `pgbackrest.conf` with stanza `maruf`, and prints the exact commands to create the stanza + first backup. |
| 06 | `06_Install_Pmm_Monitoring.yml` | Pulls and runs the PMM Server Docker container on the backup node (cleans stale `pmm-data` volume first), opens the firewall for it, installs PMM Client on all 3 PG nodes (skips `pg_stat_monitor` on Debian where the package doesn't exist), and registers them with the server. |
| 07 | `07_Configure_Cluster_Health.yml` | Deploys two systemd timers: `patroni-self-heal.timer` (30s, restarts crashed local Patroni member) and `cluster-health.timer` (60s, checks etcd quorum, Patroni leader, pgpool watchdog quorum, backend status, VIP presence). Logs to `/var/log/patroni/cluster_health.log`, writes Prometheus textfile metrics for PMM's node_exporter, fires `health_alert_command` on CRITICAL. Also deploys durable event log (`leader_events.log`) and monotonic Prometheus counters for every leader election, read-only leader event, and etcd quorum loss. |
| 08 | `08_Configure_Switchover_Signal.yml` | Deploys `pgpool_role_signal.sh` as Patroni's `on_role_change` callback. On promotion to primary, it confirms via `patronictl` that THIS node holds the DCS leader lease, maps the local IP to a pgpool backend node_id, and runs `pcp_promote_node` on ALL pgpool nodes (including the VIP-holding watchdog leader) — eliminating the ~4-minute polling gap observed on clean switchover. |

## Default Values You Should Know

| Setting | Value |
|---------|-------|
| Cluster scope (PostgreSQL name) | `maruf` |
| Patroni namespace | `percona_lab` |
| PostgreSQL version | 16 |
| Data directory | `/postgres/data/16/maruf` |
| etcd token | `PostgreSQL_HA_Cluster_1` |
| etcd data directory | `/var/lib/etcd` |
| **Floating VIP** | **`192.168.122.200`** on port **9999** |
| VIP network interface | `eth0` (change if your NIC differs) |
| pgBackRest stanza | `maruf`, repo at `/postgres/pgbackup` |
| PMM Server URL | `https://192.168.122.153:443` |
| Patroni REST API | `:8008` on every node |
| PCP port / user | `9898` / `pgpool_pcp` |

---

## Further Reading

- [Ansible Deployment](ansible.md) — run these playbooks
- [Manual Deployment](manual.md) — what each playbook automates, by hand
- [Validation](../operations/validation.md) — the stress-test evidence behind this repo