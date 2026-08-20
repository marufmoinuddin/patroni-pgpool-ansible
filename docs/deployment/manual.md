# Deployment Method B — Manual (No Ansible)

> **Scope:** The hand-by-hand path, for learning exactly what the playbooks automate. Run these on **all nodes** unless stated otherwise.

**Pick your distro family** — commands differ only in package management and a few paths. The logic (etcd → Patroni → pgpool → pgBackRest → PMM) is identical.

| Area | RHEL / CentOS / Stream 9 | Debian / Ubuntu (12, 22.04, 24.04) |
|------|-------------------------|-----------------------------------|
| Package manager | `dnf` | `apt` (with `apt update`) |
| Percona repo | RPM + `percona-release setup ppg-16` | `.deb` + `percona-release setup ppg-16` |
| PostgreSQL data dir | `/var/lib/pgsql/16/data/maruf` | `/postgres/data/16/maruf` |
| PostgreSQL bin dir | `/usr/pgsql-16/bin` | `/usr/lib/postgresql/16/bin` |
| PostgreSQL service | `postgresql-16` (systemd) | `postgresql` (via `pg_ctlcluster`) |
| Patroni binary | `/usr/bin/patroni` | `/bin/patroni` |
| **Pgpool config dir** | `/etc/pgpool-II` | `/etc/pgpool2` |
| **Pgpool service name** | `pgpool` | `pgpool2` |
| **Pgpool package** | `percona-pgpool-II-pg16` (4.7) | **native `pgpool2` (4.3.5)** — NOT `postgresql-16-pgpool2` |
| Postgres user home | `/var/lib/pgsql` | `/var/lib/postgresql` |

---

## Phase 0 — OS Preparation (all 4 nodes)

```bash
# 1. Hostnames
hostnamectl set-hostname db1      # db2, db3, db-backup respectively

# 2. /etc/hosts — same on every node
cat >> /etc/hosts <<'EOF'
192.168.122.150  db1
192.168.122.151  db2
192.168.122.152  db3
192.168.122.153  db-backup
192.168.122.200  pgpool-vip
EOF

# 3. Firewall: open cluster ports (between nodes)
# RHEL/CentOS:
firewall-cmd --permanent --add-port={2379,2380}/tcp
firewall-cmd --permanent --add-port=5432/tcp
firewall-cmd --permanent --add-port=8008/tcp
firewall-cmd --permanent --add-port=9000/tcp
firewall-cmd --permanent --add-port={9898,9999}/tcp
firewall-cmd --reload

# Debian/Ubuntu (UFW):
ufw allow 2379/tcp
ufw allow 2380/tcp
ufw allow 5432/tcp
ufw allow 8008/tcp
ufw allow 9000/tcp
ufw allow 9898/tcp
ufw allow 9999/tcp
ufw --force enable
```

---

## Phase 1 — Install Percona Packages (all 3 DB nodes + backup node)

#### RHEL / CentOS / Stream 9

```bash
# 1. Enable EPEL and CRB (CodeReady Builder) — needed for libssh2 etc.
dnf install -y epel-release
dnf config-manager --set-enabled crb

# 2. Install Percona release RPM (GPG key comes with the package)
dnf install -y https://repo.percona.com/yum/percona-release-latest.noarch.rpm

# 3. Enable the PostgreSQL 16 Percona repository
percona-release setup ppg-16

# 4. Install everything
dnf install -y \
  percona-postgresql16-server \
  percona-patroni percona-patroni-etcd etcd jq \
  percona-pgpool-II-pg16 percona-pgpool-II-pg16-extensions \
  percona-pgbackrest
```

#### Debian / Ubuntu

```bash
# 1. Install prerequisites
apt update && apt install -y curl wget gnupg2

# 2. Download and install Percona release .deb
wget https://repo.percona.com/apt/percona-release_latest.generic_all.deb
apt install -y ./percona-release_latest.generic_all.deb

# 3. Enable the PostgreSQL 16 Percona repository
percona-release setup ppg-16

# 4. Install native pgpool2 FIRST (from Debian repo) to avoid Percona libpgpool2 conflict
#    Then pin libpgpool2 to 4.3.5* so Percona's 4.7.0 doesn't upgrade it
apt update
apt install -y pgpool2 libpgpool2=4.3.5-1+deb12u1
# Pin the version (write before any Percona install)
cat > /etc/apt/preferences.d/pgpool2 <<'EOF'
Package: pgpool2
Pin: version 4.3.5*
Pin-Priority: 1001

Package: libpgpool2
Pin: version 4.3.5*
Pin-Priority: 1001
EOF

# 5. Install the rest from Percona
apt install -y \
  percona-postgresql-16 \
  percona-patroni etcd \
  percona-pgbackrest
```

> ⚠️ **Critical Debian pgpool2 note:** The Percona package `postgresql-16-pgpool2` is a **PostgreSQL 16 extension module only** (no pgpool daemon, no systemd unit, no `/etc/pgpool2` config dir). It also installs `libpgpool2=4.7.0` which **hard-conflicts** with native `pgpool2` 4.3.5's `libpgpool2=4.3.5`. **Use native `pgpool2` only** — do not install `postgresql-16-pgpool2`.

---

## Phase 2 — etcd Cluster (db1, db2, db3)

Create `/etc/etcd/etcd.conf` — the token and member IPs **must be identical** on all three nodes; only `name` and `initial-advertise-peer-urls` differ:

```ini
# /etc/etcd/etcd.conf  (db1 example)
ETCD_NAME="db1"
ETCD_DATA_DIR="/var/lib/etcd"
ETCD_LISTEN_CLIENT_URLS="http://0.0.0.0:2379"
ETCD_LISTEN_PEER_URLS="http://0.0.0.0:2380"
ETCD_ADVERTISE_CLIENT_URLS="http://192.168.122.150:2379"
ETCD_INITIAL_ADVERTISE_PEER_URLS="http://192.168.122.150:2380"
ETCD_INITIAL_CLUSTER="db1=http://192.168.122.150:2380,db2=http://192.168.122.151:2380,db3=http://192.168.122.152:2380"
ETCD_INITIAL_CLUSTER_STATE="new"
ETCD_INITIAL_CLUSTER_TOKEN="PostgreSQL_HA_Cluster_1"
```

> Swap `ETCD_NAME` and the two `...ADVERTISE...` values on db2 (`192.168.122.151`) and db3 (`192.168.122.152`).

Create the systemd unit (identical on both distros):

```bash
cat > /etc/systemd/system/etcd.service <<'EOF'
[Unit]
Description=etcd key-value store
Documentation=https://etcd.io/docs/
After=network.target

[Service]
Environment="TOKEN=PostgreSQL_HA_Cluster_1"
Environment="CLUSTER_STATE=new"
Environment="THIS_NAME={{ ansible_hostname }}"
Environment="THIS_IP={{ ansible_default_ipv4.address }}"
Environment="CLUSTER=db1=http://192.168.122.150:2380,db2=http://192.168.122.151:2380,db3=http://192.168.122.152:2380"

ExecStart=/usr/bin/etcd \
  --data-dir=/var/lib/etcd \
  --name ${THIS_NAME} \
  --initial-advertise-peer-urls http://${THIS_IP}:2380 \
  --listen-peer-urls http://${THIS_IP}:2380 \
  --advertise-client-urls http://${THIS_IP}:2379 \
  --listen-client-urls http://${THIS_IP}:2379 \
  --initial-cluster ${CLUSTER} \
  --initial-cluster-state ${CLUSTER_STATE} \
  --initial-cluster-token ${TOKEN}

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

```bash
# Start etcd on ALL THREE nodes (roughly together — quorum needs 2/3)
systemctl daemon-reload
systemctl enable --now etcd

# Verify quorum from any node
ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 endpoint health
ETCDCTL_API=3 etcdctl --endpoints=http://192.168.122.150:2379 member list
# All 3 members must show "healthy: true"
```

---

## Phase 3 — Patroni + PostgreSQL (db1, db2, db3)

Create `/etc/patroni/patroni.yml` on **each** node. The structure is the same everywhere; only `name` and the two `connect_address`/etcd host lines change per node.

```yaml
# /etc/patroni/patroni.yml  (db1 example)
namespace: percona_lab
scope: maruf
name: db1                       # db2 / db3 on the other nodes

restapi:
  listen: 0.0.0.0:8008
  connect_address: 192.168.122.150:8008   # this node's IP

etcd3:
  host: 192.168.122.150:2379               # this node's etcd

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576

    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:
        wal_level: replica
        hot_standby: "on"
        max_wal_senders: 5
        max_replication_slots: 10
        wal_log_hints: "on"
        logging_collector: "on"
        max_wal_size: '10GB'
        archive_mode: "off"
        archive_timeout: 600s
        archive_command: "cp -f %p /postgres/pgbackup/maruf/archive/%f"

  initdb:
    - encoding: UTF8
    - data-checksums

  pg_hba:
    - host replication replicator 127.0.0.1/32 trust
    - host replication replicator 0.0.0.0/0 scram-sha-256
    - host all all 0.0.0.0/0 scram-sha-256
    - host all all ::0/0 scram-sha-256

  users:
    admin:
      password: CHANGE_ME_ADMIN
      options:
        - createrole
        - createdb
    percona:
      password: CHANGE_ME_PERCONA
      options:
        - createrole
        - createdb
    pgpool:
      password: CHANGE_ME_PGPOOL
      options:
        - createrole
        - createdb

postgresql:
  cluster_name: cluster_1
  listen: 0.0.0.0:5432
  connect_address: 192.168.122.150:5432   # this node's IP
  data_dir: /postgres/data/16/maruf         # DEBIAN PATH — change for RHEL below
  bin_dir: /usr/lib/postgresql/16/bin      # DEBIAN PATH — change for RHEL below
  pgpass: /tmp/pgpass0
  authentication:
    replication:
      username: replicator
      password: CHANGE_ME_REPLICATOR
    superuser:
      username: postgres
      password: CHANGE_ME_POSTGRES
  parameters:
    unix_socket_directories: /var/run/postgresql
  create_replica_methods:
    - basebackup
  basebackup:
    checkpoint: 'fast'

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
  nosync: false
```

> 📋 **Path differences for `patroni.yml`:**
> - **RHEL/CentOS:** `data_dir: /var/lib/pgsql/16/data/maruf`, `bin_dir: /usr/pgsql-16/bin`
> - **Debian/Ubuntu:** `data_dir: /postgres/data/16/maruf`, `bin_dir: /usr/lib/postgresql/16/bin`

#### Initialize PostgreSQL + systemd unit

##### RHEL / CentOS / Stream 9

```bash
# Data directory will be created by Patroni on bootstrap
mkdir -p /var/lib/pgsql/16 /etc/patroni
chown -R postgres:postgres /var/lib/pgsql

# systemd unit (Patroni binary at /usr/bin/patroni)
cat > /etc/systemd/system/patroni.service <<'EOF'
[Unit]
Description=Runners to orchestrate a high-availability PostgreSQL
After=syslog.target network.target etcd.service

[Service]
Type=simple
User=postgres
Group=postgres
ExecStart=/usr/bin/patroni /etc/patroni/patroni.yml
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=process
TimeoutSec=30
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
```

##### Debian / Ubuntu

```bash
# Create the cluster with pg_createcluster (on PRIMARY ONLY - db1)
mkdir -p /postgres/data/16 /etc/patroni
chown -R postgres:postgres /postgres

# On PRIMARY (db1) only:
pg_createcluster 16 maruf -d /postgres/data/16/maruf

# Stop it so Patroni can take over
pg_ctlcluster 16 maruf stop

# systemd unit (Patroni binary at /bin/patroni)
cat > /etc/systemd/system/patroni.service <<'EOF'
[Unit]
Description=Runners to orchestrate a high-availability PostgreSQL
After=syslog.target network.target etcd.service

[Service]
Type=simple
User=postgres
Group=postgres
ExecStart=/bin/patroni /etc/patroni/patroni.yml
ExecReload=/bin/kill -s HUP $MAINPID
KillMode=process
TimeoutSec=30
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
```

#### Bootstrap the cluster — start the primary FIRST

```bash
# On db1 ONLY — this creates the data directory and makes db1 the leader
systemctl enable --now patroni

# Wait ~30 seconds for initdb + leader lock
patronictl -c /etc/patroni/patroni.yml list
# db1 should appear as Leader

# Now start replicas — db2, db3
systemctl enable --now patroni

# Wait ~1-2 minutes for pg_basebackup + catch-up
patronictl -c /etc/patroni/patroni.yml list
# db1 Leader, db2 Streaming, db3 Streaming, lag 0
```

> 🧠 **Why primary first?** If you start a replica before any primary exists, Patroni would *also* try to bootstrap — two nodes racing to initdb is chaos. The leader lock in etcd prevents a real split-brain, but starting in order is clean and predictable.

---

## Phase 4 — pgpool-II + Watchdog + VIP (db1, db2, db3)

The full `pgpool.conf` is long — here is the **watchdog-relevant essence** (per node; the `pgpool_node_id` file differs: `0`, `1`, `2`).

> 📋 **Config directory and package version differences:**
> - **RHEL/CentOS:** `/etc/pgpool-II`, pgpool-II 4.7 (Percona package), watchdog in **separate `pgpool_watchdog.conf`**
> - **Debian/Ubuntu:** `/etc/pgpool2`, native `pgpool2` 4.3.5 (Debian repo), watchdog **inline in `pgpool.conf`** with legacy parameter names

#### RHEL/CentOS (pgpool-II 4.7) — Separate Watchdog Config

```ini
# pgpool.conf  (db1 example — key lines)
listen_addresses = '*'
port = 9999
socket_dir = '/var/run/pgpool'

backend_hostname0 = '192.168.122.150'
backend_port0 = 5432
backend_weight0 = 1
backend_data_directory0 = '/var/lib/pgsql/16/data/maruf'   # RHEL PATH
backend_flag0 = 'ALLOW_TO_FAILOVER'

backend_hostname1 = '192.168.122.151'
backend_port1 = 5432
backend_weight1 = 1
backend_data_directory1 = '/var/lib/pgsql/16/data/maruf'   # RHEL PATH
backend_flag1 = 'ALLOW_TO_FAILOVER'

backend_hostname2 = '192.168.122.152'
backend_port2 = 5432
backend_weight2 = 1
backend_data_directory2 = '/var/lib/pgsql/16/data/maruf'   # RHEL PATH
backend_flag2 = 'ALLOW_TO_FAILOVER'

# Health checks
health_check_period = 10
health_check_timeout = 20
health_check_user = 'pgpool'
health_check_password = 'CHANGE_ME_HEALTH'

# Streaming replication check
sr_check_period = 3
sr_check_user = 'pgpool'
sr_check_password = 'CHANGE_ME_HEALTH'
delay_threshold = 1048576

# PCP
pcp_listen_addresses = '*'
pcp_port = 9898
pcp_socket_dir = '/var/run/pgpool'
```

And the watchdog section in **separate `pgpool_watchdog.conf`** (4.7 parameter names):

```ini
# pgpool_watchdog.conf (db1 example)
use_watchdog = on

wd_hostname = '192.168.122.150'
wd_port = 9000
wd_priority = 1
wd_authkey = 'CHANGE_ME_WD_AUTH'

# Watchdog peers — IMPORTANT: list ALL nodes on every node (4.7 requires indexed params)
wd_hostname0 = '192.168.122.150'
wd_port0 = 9000
wd_hostname1 = '192.168.122.151'
wd_port1 = 9000
wd_hostname2 = '192.168.122.152'
wd_port2 = 9000

# Heartbeat (4.7 names: heartbeat_hostname, heartbeat_port, heartbeat_device)
heartbeat_destination0 = '192.168.122.151'
heartbeat_port0 = 9694
heartbeat_device0 = 'eth0'
heartbeat_destination1 = '192.168.122.152'
heartbeat_port1 = 9694
heartbeat_device1 = 'eth0'

# Virtual IP — only active on the watchdog leader
vip = 1
vip_ip = '192.168.122.200'
vip_ifconfig = 'ifconfig eth0:0 192.168.122.200/24'
vip_arping = 'arping -U -I eth0 -c 3 192.168.122.200'
vip_cidr_prefix_length = 24
delegate_ip = '192.168.122.200'
```

#### Debian/Ubuntu (pgpool2 4.3.5) — Inline Watchdog Config

```ini
# pgpool.conf  (db1 example — key lines, watchdog INLINE)
listen_addresses = '*'
port = 9999
socket_dir = '/var/run/pgpool'

backend_hostname0 = '192.168.122.150'
backend_port0 = 5432
backend_weight0 = 1
backend_data_directory0 = '/postgres/data/16/maruf'   # DEBIAN PATH
backend_flag0 = 'ALLOW_TO_FAILOVER'

backend_hostname1 = '192.168.122.151'
backend_port1 = 5432
backend_weight1 = 1
backend_data_directory1 = '/postgres/data/16/maruf'   # DEBIAN PATH
backend_flag1 = 'ALLOW_TO_FAILOVER'

backend_hostname2 = '192.168.122.152'
backend_port2 = 5432
backend_weight2 = 1
backend_data_directory2 = '/postgres/data/16/maruf'   # DEBIAN PATH
backend_flag2 = 'ALLOW_TO_FAILOVER'

# Health checks
health_check_period = 10
health_check_timeout = 20
health_check_user = 'pgpool'
health_check_password = 'CHANGE_ME_HEALTH'

# Streaming replication check
sr_check_period = 3
sr_check_user = 'pgpool'
sr_check_password = 'CHANGE_ME_HEALTH'
delay_threshold = 1048576

# PCP
pcp_listen_addresses = '*'
pcp_port = 9898
pcp_socket_dir = '/var/run/pgpool'

# Watchdog (INLINE in pgpool.conf — 4.3.5 legacy parameter names)
use_watchdog = on
wd_lifecheck_method = 'heartbeat'

wd_hostname = '192.168.122.150'
wd_port = 9000
wd_priority0 = 1
wd_authkey0 = 'CHANGE_ME_WD_AUTH'

# Watchdog peers (indexed: 0,1,2 — must list ALL nodes)
wd_hostname0 = '192.168.122.150'
wd_port0 = 9000
wd_hostname1 = '192.168.122.151'
wd_port1 = 9000
wd_hostname2 = '192.168.122.152'
wd_port2 = 9000

# Heartbeat (4.3.5 legacy names: heartbeat_destination, heartbeat_destination_port, heartbeat_interface)
heartbeat_destination0 = '192.168.122.151'
heartbeat_destination_port0 = 9694
heartbeat_interface0 = 'enp3s0'
heartbeat_destination1 = '192.168.122.152'
heartbeat_destination_port1 = 9694
heartbeat_interface1 = 'enp3s0'

# Virtual IP
vip = 1
vip_ip = '192.168.122.200'
vip_ifconfig = 'ifconfig enp3s0:0 192.168.122.200/24'
vip_arping = 'arping -U -I enp3s0 -c 3 192.168.122.200'
vip_cidr_prefix_length = 24
delegate_IP = '192.168.122.200'
```

> 🔑 **Key 4.3.5 vs 4.7 parameter differences:**
> | 4.7 (CentOS, separate file) | 4.3.5 (Debian, inline) |
> |-----------------------------|------------------------|
> | `heartbeat_hostnameN` | `heartbeat_destinationN` |
> | `heartbeat_portN` | `heartbeat_destination_portN` |
> | `heartbeat_deviceN` | `heartbeat_interfaceN` |
> | `wd_priority` (unindexed) | `wd_priority0` (indexed) |
> | `wd_authkey` (unindexed) | `wd_authkey0` (indexed) |
> | `delegate_ip` | `delegate_IP` (uppercase) |
> | `pgpool_watchdog.conf` | inline in `pgpool.conf` |

Per-node files (identical on both distros except config dir):

```bash
# db1 → 0, db2 → 1, db3 → 2
echo -n "0" > /etc/pgpool-II/pgpool_node_id    # RHEL path
# echo -n "0" > /etc/pgpool2/pgpool_node_id    # Debian path

# pgpool needs to raise the VIP → sudoers entry
cat > /etc/sudoers.d/pgpool-vip <<'EOF'
pgpool ALL=(root) NOPASSWD: /sbin/ip, /usr/sbin/arping
EOF
chmod 440 /etc/sudoers.d/pgpool-vip
```

> 📋 **Config dir for these files:**
> - **RHEL/CentOS:** `/etc/pgpool-II/`
> - **Debian/Ubuntu:** `/etc/pgpool2/`

Create the PCP passfile and pool password file:

```bash
# PCP user (pgpool_pcp / your password) — generates a pg_md5 hash
pg_md5 -p -u pgpool_pcp   # enter the password when prompted → /etc/pgpool-II/pcp.conf

# PostgreSQL user passwords for pgpool (plaintext file works with SCRAM)
echo "postgres:CHANGE_ME_POSTGRES" > /etc/pgpool-II/pool_passwd
echo "pgpool:CHANGE_ME_PGPOOL"   >> /etc/pgpool-II/pool_passwd
chown pgpool:pgpool /etc/pgpool-II/pool_passwd /etc/pgpool-II/pcp.conf
chmod 600 /etc/pgpool-II/pool_passwd /etc/pgpool-II/pcp.conf
```

> 📋 **Config dir for these files:**
> - **RHEL/CentOS:** `/etc/pgpool-II/`
> - **Debian/Ubuntu:** `/etc/pgpool2/`

Deploy the Patroni-aware failover scripts (simplified):

```bash
# /etc/pgpool-II/failover.sh — on backend failover, query Patroni for the new primary
cat > /etc/pgpool-II/failover.sh <<'EOF'
#!/bin/bash
# pgpool calls this when a backend goes down
# ... query each Patroni REST API :8008 for the leader ...
# ... update pgpool backend list to point at the new primary ...
EOF

# /etc/pgpool-II/follow_master.sh — on primary change, tell replicas to re-follow
cat > /etc/pgpool-II/follow_master.sh <<'EOF'
#!/bin/bash
# Patroni already handles re-following; this script logs the event
EOF

chmod +x /etc/pgpool-II/failover.sh /etc/pgpool-II/follow_master.sh
chown pgpool:pgpool /etc/pgpool-II/failover.sh /etc/pgpool-II/follow_master.sh
```

> 📄 The repository's `04_Configure_Pgpool.yml` contains the **complete, working** versions of every file above, inline. For a manual deployment, copy them from there — they are battle-tested.

Start pgpool on **all three nodes**:

```bash
# RHEL/CentOS:
systemctl enable --now pgpool

# Debian/Ubuntu:
systemctl enable --now pgpool2

# Verify watchdog + VIP from any node
pcp_watchdog_info -h localhost -p 9898 -U pgpool_pcp -w
# One node is "LEADER" and owns 192.168.122.200
ip addr show eth0 | grep 192.168.122.200    # RHEL (or your NIC)
# ip addr show enp3s0 | grep 192.168.122.200  # Debian (predictable NIC name)
```

---

## Phase 5 — pgBackRest (backup node `.153` + PG nodes)

> 📋 **Postgres user home:**
> - **RHEL/CentOS:** `/var/lib/pgsql`
> - **Debian/Ubuntu:** `/var/lib/postgresql`

```bash
# On the backup node (.153)
mkdir -p /postgres/pgbackup
chown -R postgres:postgres /postgres

# SSH key exchange: backup node ↔ each PG node (as postgres user)
# 1. On backup node:  sudo -iu postgres ssh-keygen -t ed25519
# 2. Copy key to each PG node:  sudo -iu postgres ssh-copy-id postgres@db1 (etc.)
# 3. Also allow postgres@dbX to SSH into the backup node (for archiving)

# /etc/pgbackrest.conf on the BACKUP node
cat > /etc/pgbackrest.conf <<'EOF'
[global]
repo1-path = /postgres/pgbackup
repo1-retention-full = 2

[maruf]
pg1-host = 192.168.122.150
pg1-path = /postgres/data/16/maruf      # DEBIAN PATH — change for RHEL
pg1-port = 5432
EOF

# /etc/pgbackrest.conf on each PG NODE (client only, for archiving)
cat > /etc/pgbackrest.conf <<'EOF'
[global]
repo1-host = 192.168.122.153
repo1-path = /postgres/pgbackup

[maruf]
pg1-path = /postgres/data/16/maruf      # DEBIAN PATH — change for RHEL
EOF
```

> 📋 **pgBackRest `pg1-path` values:**
> - **RHEL/CentOS:** `/var/lib/pgsql/16/data/maruf`
> - **Debian/Ubuntu:** `/postgres/data/16/maruf`

```bash
# Create the stanza, then the first full backup (on the backup node)
sudo -iu postgres pgbackrest --stanza=maruf stanza-create
sudo -iu postgres pgbackrest --stanza=maruf --type=full backup
sudo -iu postgres pgbackrest --stanza=maruf info
```

> The `archive_command` in Patroni (Phase 3) sends WAL to `/postgres/pgbackup/maruf/archive/` — pgBackRest picks it up from there. Archiving must be enabled **before** the first backup for a complete PITR chain.

---

## Phase 6 — PMM (backup node `.153` + PG nodes)

#### RHEL / CentOS / Stream 9

```bash
# 1. On the backup node — run PMM Server as a Docker container
dnf install -y docker-ce docker-ce-cli
systemctl enable --now docker

# NOTE: the image listens on 8443 internally; map host 443 → container 8443
docker run -d --name pmm-server --restart always \
  -p 443:8443 -v pmm-data:/srv perconalab/pmm-server:3

# Wait 2-3 minutes, then change the default admin password
docker exec pmm-server change-admin-password YourNewPassword

# 2. On each PG node — install PMM Client
percona-release enable pmm3-client release
dnf install -y pmm3-client percona-pg_stat_monitor16

# 3. Register each node with the server (retry until it succeeds)
pmm-admin config --server-insecure-tls \
  --server-url=https://admin:YourNewPassword@192.168.122.153:443 --force

# 4. Add PostgreSQL monitoring (repeat on each node)
pmm-admin add postgresql --username=pg_pmm --password=CHANGE_ME \
  --service-name=db1-pg --host=localhost --port=5432

# 5. Browse to https://192.168.122.153:443 and watch the dashboards!
```

#### Debian / Ubuntu

```bash
# 1. On the backup node — run PMM Server as a Docker container
apt update && apt install -y docker.io
systemctl enable --now docker

# NOTE: the image listens on 8443 internally; map host 443 → container 8443
docker run -d --name pmm-server --restart always \
  -p 443:8443 -v pmm-data:/srv perconalab/pmm-server:3

# Wait 2-3 minutes, then change the default admin password
docker exec pmm-server change-admin-password YourNewPassword

# 2. On each PG node — install PMM Client
percona-release enable pmm3-client release
apt update && apt install -y pmm3-client
# Note: percona-pg-stat-monitor16 is NOT available on Debian/Ubuntu — skip it

# 3. Register each node with the server (retry until it succeeds)
pmm-admin config --server-insecure-tls \
  --server-url=https://admin:YourNewPassword@192.168.122.153:443 --force

# 4. Add PostgreSQL monitoring (repeat on each node)
pmm-admin add postgresql --username=pg_pmm --password=CHANGE_ME \
  --service-name=db1-pg --host=localhost --port=5432

# 5. Browse to https://192.168.122.153:443 and watch the dashboards!
```

> 🔥 If the PG nodes cannot reach `https://192.168.122.153:443`, open the firewall on the backup node:
> - **RHEL:** `firewall-cmd --permanent --add-port=443/tcp && firewall-cmd --reload`
> - **Debian:** `ufw allow 443/tcp`
> and add an iptables FORWARD rule for Docker if needed.

---

## Further Reading

- [Ansible Deployment](ansible.md) — the automated equivalent of all these phases
- [Repository Contents](repository-contents.md) — where each piece lives in the playbooks
- [Operations Guide](../operations/operations.md) — daily commands after deployment
- [Troubleshooting](../troubleshooting/troubleshooting.md) — common issues (especially the Debian pgpool trap)