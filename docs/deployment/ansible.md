# Deployment Method A — Ansible (Automated)

> **Scope:** The recommended deployment path. One command deploys the whole cluster, and every playbook is **idempotent** — you can re-run it safely.

---

## Step 1 — Prerequisites

- **4 CentOS Stream 9 (or RHEL 9) VMs** with:
  - root SSH access (or a sudo user)
  - Outbound internet access (to reach Percona / EPEL / Docker repos)
- **Ansible 2.16+** installed on the machine you run from (your laptop or the VPS):
  ```bash
  # On CentOS/RHEL
  sudo dnf install -y ansible-core
  # On Ubuntu/Debian
  sudo apt install -y ansible
  # Or via pip in a venv
  python3 -m venv ~/ansible-venv && source ~/ansible-venv/bin/activate
  pip install ansible
  ```
- **Network connectivity** between all nodes (ports 2379, 2380, 5432, 8008, 9000, 9898, 9999)
- **VIP `192.168.122.200`** must be unused and routable on the same subnet
- The correct NIC name on your VMs (this repo uses `eth0`; check with `ip link`)

See [Server Planning](server-planning.md) for topology and sizing.

## Step 2 — Clone and Edit the Inventory

```bash
git clone https://github.com/marufmoinuddin/patroni-pgpool-ansible.git
cd patroni-pgpool-ansible
cp hosts.ini.example hosts.ini
vim hosts.ini
```

The inventory looks like this:

```ini
[pg_nodes]
percona-node-1 ansible_host=192.168.122.150 ansible_user=root
percona-node-2 ansible_host=192.168.122.151 ansible_user=root
percona-node-3 ansible_host=192.168.122.152 ansible_user=root

[pg_backrest]
percona-pgbackrest ansible_host=192.168.122.153 ansible_user=root
```

Replace the IPs with your own. The `ansible_user=root` means Ansible connects as root directly — make sure your SSH key is authorized on every node:

```bash
ssh-copy-id root@192.168.122.150
ssh-copy-id root@192.168.122.151
ssh-copy-id root@192.168.122.152
ssh-copy-id root@192.168.122.153
```

## Step 3 — Adjust Variables (Important!)

All secrets are now managed in a separate **`variables.yaml`** file (not committed to git). This allows you to encrypt it with `ansible-vault` and keep passwords out of playbooks.

**Quick start:**
```bash
# 1. Copy the example and edit with your real values
cp variables.yaml.example variables.yaml
vim variables.yaml

# 2. (Recommended) Encrypt it with ansible-vault
ansible-vault encrypt variables.yaml

# 3. Edit later with: ansible-vault edit variables.yaml
```

**These are the values you most likely need to change in `variables.yaml`:**

| Variable | Description | Example |
|----------|-------------|---------|
| `patroni_scope` | Cluster name | `maruf` |
| `postgres_password` | PostgreSQL superuser password | **strong random** |
| `replicator_password` | Replication user password | **strong random** |
| `patroni_admin_password` | Patroni REST API admin | **strong random** |
| `percona_password` | Percona monitoring user | **strong random** |
| `pgpool_password` | pgpool monitoring user | **strong random** |
| `pcp_password` | Pgpool PCP admin user | **strong random** |
| `pmm_admin_password` | PMM web UI admin | **strong random** |
| `pg_pmm_user_password` | PMM PostgreSQL monitor user | **strong random** |
| `vip_address` | Floating Virtual IP | `192.168.122.200` |
| `vip_interface` | NIC for VIP (check `ip link`) | `eth0` |

> ⚠️ **Never commit `variables.yaml` to git** — it's in `.gitignore`. Only commit `variables.yaml.example`. Full details in [Security](../operations/security.md).

## Step 4 — Run the Deployment

```bash
# Check the whole suite parses correctly
ansible-playbook -i hosts site.yml --syntax-check

# Run everything (this takes ~10-15 minutes)
# If you encrypted variables.yaml with ansible-vault:
ansible-playbook -i hosts site.yml --ask-vault-pass

# If you did NOT encrypt (not recommended for production):
ansible-playbook -i hosts site.yml
```

> ⏳ What you'll see: play 01 installs packages on all nodes (slowest), play 02 forms the etcd quorum, play 03 bootstraps PostgreSQL with Patroni (primary first, then replicas), play 04 starts the pgpool watchdog cluster, play 05 wires up pgBackRest, play 06 brings up PMM. **Green = done.**

## Step 5 — Post-Deployment Checklist

1. **Patroni cluster is green:**
   ```bash
   patronictl -c /etc/patroni/patroni.yml list
   ```
   You should see one `Leader` and two `Streaming` replicas with `0` lag.

2. **etcd quorum is healthy:**
   ```bash
   ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 endpoint health
   ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 member list
   ```

3. **pgpool watchdog elected a leader and owns the VIP:**
   ```bash
   pcp_watchdog_info -h localhost -p 9898 -U pgpool_pcp -w
   ip addr show eth0 | grep 192.168.122.200
   ```

4. **You can connect through the VIP:**
   ```bash
   psql -h 192.168.122.200 -p 9999 -U postgres -d postgres -c "SELECT 1;"
   ```

5. **Create the pgBackRest stanza + first backup** (the playbook prints these, by design — they're intentionally manual so *you* decide when to take the first backup):
   ```bash
   sudo -iu postgres pgbackrest --stanza=maruf stanza-create
   sudo -iu postgres pgbackrest --stanza=maruf --type=full backup
   sudo -iu postgres pgbackrest --stanza=maruf info
   ```

6. **Log into PMM:**
   - URL: `https://192.168.122.153:443`
   - User: `admin` / your chosen password
   - You should see 3 PostgreSQL nodes reporting metrics

## Rerunning Safely

Every playbook is idempotent. If something failed midway, fix it and re-run:

```bash
ansible-playbook -i hosts site.yml
```

Only play 02 wipes etcd data **by design** (it bootstraps a fresh cluster). If your cluster is already healthy and you re-run, Patroni will simply reconnect — **your data is safe**. On later builds, re-running `site.yml` no longer wipes etcd at all — only `etcd_force_reset: true` (fresh bootstrap / DR restore) wipes the DCS (see [Resilience](../concepts/resilience.md)).

---

## Further Reading

- [Repository Contents](repository-contents.md) — full playbook sequence and defaults
- [Manual Deployment](manual.md) — the same steps by hand (no Ansible)
- [Operations Guide](../operations/operations.md) — daily commands after deployment
- [Security](../operations/security.md) — change these before production