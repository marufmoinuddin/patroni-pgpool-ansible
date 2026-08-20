# Quick Start

> **Scope:** The fastest path from zero to a running cluster with Ansible. For the full prerequisite detail, topology, and sizing read [Server Planning](../deployment/server-planning.md).

---

## 1. Clone the Repository

```bash
git clone https://github.com/marufmoinuddin/patroni-pgpool-ansible.git
cd patroni-pgpool-ansible
```

## 2. Configure the Inventory

```bash
cp hosts.ini.example hosts.ini
vim hosts.ini
```

The inventory has 4 hosts: 3 PostgreSQL/Patroni/etcd/pgpool nodes (`pg_nodes`) and 1 backup node (`pg_backrest`). Replace the IPs with your own and make sure your SSH key is authorized on every node:

```bash
ssh-copy-id root@192.168.122.150
ssh-copy-id root@192.168.122.151
ssh-copy-id root@192.168.122.152
ssh-copy-id root@192.168.122.153
```

## 3. Adjust Variables (Important!)

All secrets live in a separate, git-ignored `variables.yaml` (not in playbooks). Copy the example, fill in real passwords, and encrypt it:

```bash
cp variables.yaml.example variables.yaml
vim variables.yaml

# (Recommended) Encrypt before production use
ansible-vault encrypt variables.yaml
```

> ⚠️ **Never commit `variables.yaml` to git.** Only `variables.yaml.example` is committed. See [Security](../operations/security.md).

## 4. Deploy

```bash
# Check the whole suite parses
ansible-playbook -i hosts site.yml --syntax-check

# Run everything (~10-15 minutes). If you encrypted variables.yaml:
ansible-playbook -i hosts site.yml --ask-vault-pass
```

> ⏳ Play 01 installs packages (slowest), play 02 forms the etcd quorum, play 03 bootstraps PostgreSQL + Patroni, play 04 starts the pgpool watchdog cluster + VIP, play 05 wires pgBackRest, play 06 brings up PMM, play 07 deploys self-healing timers, play 08 deploys the switchover-signal callback. **Green = done.**

## 5. Post-Deployment Checklist

1. **Patroni cluster is green:**
   ```bash
   patronictl -c /etc/patroni/patroni.yml list
   ```
   One `Leader`, two `Streaming` replicas, `0` lag.

2. **etcd quorum healthy:**
   ```bash
   ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 endpoint health
   ```

3. **pgpool watchdog owns the VIP:**
   ```bash
   pcp_watchdog_info -h localhost -p 9898 -U pgpool_pcp -w
   ip addr show eth0 | grep 192.168.122.200
   ```

4. **Connect through the VIP:**
   ```bash
   psql -h 192.168.122.200 -p 9999 -U postgres -d postgres -c "SELECT 1;"
   ```

5. **Create the pgBackRest stanza + first backup** (intentionally manual — you decide when):
   ```bash
   sudo -iu postgres pgbackrest --stanza=maruf stanza-create
   sudo -iu postgres pgbackrest --stanza=maruf --type=full backup
   sudo -iu postgres pgbackrest --stanza=maruf info
   ```

6. **Log into PMM:** `https://192.168.122.153:443` (user `admin` / your password) — 3 PostgreSQL nodes reporting.

## Rerunning Safely

Every playbook is idempotent. If something failed midway, fix it and re-run `ansible-playbook -i hosts site.yml`. Re-running on a healthy cluster **no longer wipes etcd** — only `etcd_force_reset: true` (fresh bootstrap / DR restore) wipes the DCS.

---

## Further Reading

- [Ansible Deployment](../deployment/ansible.md) — step-by-step deployment detail
- [Server Planning](../deployment/server-planning.md) — topology and sizing
- [Repository Contents](../deployment/repository-contents.md) — playbook sequence and defaults
- [Validation](../operations/validation.md) — the fault-injection evidence behind this repo