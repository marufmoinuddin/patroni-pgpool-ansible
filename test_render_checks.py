#!/usr/bin/env python3
"""Render-validation harness for the Patroni/pgpool Ansible playbook templates.

Simulates a 3-node inventory (RHEL node 1) and renders the critical inline
Jinja2 `content` blocks from 02/03/04, asserting the resilience fixes are
actually present in the generated configuration. Uses plain Jinja2 with stub
filters for ansible's `extract`/`regex_replace` to keep the harness portable.
"""
import json
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# Simulated inventory / hostvars (matches hosts.ini.example, node 1 = RHEL)
# ---------------------------------------------------------------------------
HOSTS = {
    "centos-patroni-1": {"ansible_host": "192.168.122.200", "inventory_hostname": "centos-patroni-1"},
    "centos-patroni-2": {"ansible_host": "192.168.122.201", "inventory_hostname": "centos-patroni-2"},
    "centos-patroni-3": {"ansible_host": "192.168.122.202", "inventory_hostname": "centos-patroni-3"},
}
GROUPS = {"pg_nodes": list(HOSTS), "etcd_nodes": ["etcd-witness-1", "etcd-witness-2", "etcd-witness-3"]}
WITNESS_HOSTS = {
    "etcd-witness-1": {"ansible_host": "192.168.122.210", "inventory_hostname": "etcd-witness-1"},
    "etcd-witness-2": {"ansible_host": "192.168.122.211", "inventory_hostname": "etcd-witness-2"},
    "etcd-witness-3": {"ansible_host": "192.168.122.212", "inventory_hostname": "etcd-witness-3"},
}
ALL_HOSTVARS = {**HOSTS, **WITNESS_HOSTS}

VARS = {
    "patroni_scope": "kyc",
    "patroni_namespace": "percona_lab",
    "namespace": "percona_lab",
    "cluster_name": "cluster_1",
    "pg_version": "16",
    "vip_address": "192.168.122.200",
    "vip": "192.168.122.200",
    "vip_cidr": "24",
    "pgpool_port": 9999,
    "pcp_port": 9898,
    "wd_port": 9000,
    "heartbeat_port": 9694,
    "watchdog_authkey": "test-auth-key",
    "pgpool_wd_quorum_exit": "on",
    "wd_priority_base": 1,
    "etcd_token": "PostgreSQL_HA_Cluster_1",
    "etcd_data_dir": "/var/lib/etcd",
    "etcd_initial_cluster_state": "new",
    "etcd_member_hosts": list(HOSTS),
    "etcd_client_endpoints": "192.168.122.200:2379,192.168.122.201:2379,192.168.122.202:2379",
    "health_check_user": "pgpool",
    "health_check_password": "hp",
    "pcp_user": "pgpool_pcp",
    "pcp_password": "pcp",
    "pgpool_conf_dir": "/etc/pgpool-II",
    "pgpool_service_name": "pgpool",
    "pgpool_log_dir": "/var/log/pgpool",
    "if_cmd_path": "/usr/sbin",
    "arping_path": "/usr/sbin",
    "node_index": 1,
    "vip_interface": "eth0",
    "ansible_os_family": "RedHat",
    "patroni_watchdog": True,
    "etcd_wait_timeout": 90,
    "etcd_force_reset": False,
    "health_alert_command": "",
    "pmm_textfile_dir": "",
    "scope": "kyc",
    "node_ip": "192.168.122.200",
    "node_name": "centos-patroni-1",
    "data_dir": "/var/lib/pgsql/16/data/kyc",
    "base_data_dir": "/var/lib/pgsql/16",
    "pg_bin_dir": "/usr/pgsql-16/bin",
    "pg_socket_dir": "/var/run/postgresql",
    "pg_service": "postgresql-16",
    "pg_ctl": "/usr/pgsql-16/bin/pg_ctl",
    "patroni_admin_password": "ap",
    "percona_password": "pp",
    "pgpool_password": "gp",
    "replicator_username": "replicator",
    "replicator_password": "rp",
    "postgres_password": "sp",
    # pgtune values (stubs - only the blocks we render need these)
    "pg_shared_buffers_mb": 8192,
    "pg_effective_cache_size_mb": 24576,
    "pg_maintenance_work_mem_mb": 1024,
    "pg_work_mem_kb": 4096,
    "pg_max_connections": 200,
    "pg_wal_buffers_kb": 8192,
    "pg_min_wal_size_mb": 1024,
    "pg_max_wal_size_mb": 4096,
    "pg_checkpoint_completion_target": 0.9,
    "pg_random_page_cost": 1.1,
    "pg_effective_io_concurrency": 200,
    "pg_max_worker_processes": 8,
    "pg_max_parallel_workers_per_gather": 4,
    "pg_max_parallel_workers": 8,
    "pg_jit": "on",
    # Ansible magic vars for the "current" simulated node
    "inventory_hostname": "centos-patroni-1",
    "ansible_host": "192.168.122.200",
    "ansible_default_ipv4": {"address": "192.168.122.200", "interface": "eth0"},
}


def make_env():
    env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)

    def extract(container, key, default=None):
        keys = key if isinstance(key, list) else [key]
        try:
            value = container[keys[0]] if isinstance(container, dict) else getattr(container, keys[0])
        except (KeyError, AttributeError):
            return default
        for k in keys[1:]:
            try:
                value = value[k]
            except (KeyError, IndexError, TypeError):
                return default
        return value

    def regex_replace(s, pattern, replacement, ignorecase=False):
        flags = re.IGNORECASE if ignorecase else 0
        return re.sub(pattern, replacement, str(s), flags=flags)

    env.filters["extract"] = extract
    env.filters["regex_replace"] = regex_replace
    # NOTE: do NOT override `default` - jinja2's built-in handles Undefined properly
    return env


def render_block(block, extra=None):
    env = make_env()
    ctx = dict(VARS)
    ctx.update({"hostvars": ALL_HOSTVARS, "groups": GROUPS})
    if extra:
        ctx.update(extra)
    tpl = env.from_string(block)
    return tpl.render(**ctx)


def load_blocks(path, task_names):
    """Return {task_name: content_block} for copy tasks with a content key."""
    data = yaml.safe_load(Path(path).read_text())
    found = {}
    for play in data:
        for task in play.get("tasks", []):
            name = task.get("name", "")
            for tn in task_names:
                if tn in name:
                    # content may sit at task level or under the module key
                    # (copy uses "content", blockinfile uses "block")
                    content = task.get("content")
                    if content is None:
                        content = task.get("block")
                    if content is None:
                        for key, val in task.items():
                            if isinstance(val, dict) and ("content" in val or "block" in val):
                                content = val.get("content", val.get("block"))
                                break
                    if content is not None:
                        found[tn] = content
    return found


FAILURES = []
def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def main():
    print("=" * 70)
    print("02_Configure_Etcd.yml — etcd systemd unit")
    print("=" * 70)
    blocks = load_blocks("02_Configure_Etcd.yml", ["Create systemd service file"])
    etcd_unit = render_block(blocks["Create systemd service file"],
                             extra={"etcd_initial_cluster": "centos-patroni-1=http://192.168.122.200:2380,centos-patroni-2=http://192.168.122.201:2380,centos-patroni-3=http://192.168.122.202:2380",
                                    "etcd_initial_cluster_state": "new"})
    check("initial-cluster uses concrete values (no {{ CLUSTER }} indirection)", "{{ CLUSTER }}" not in etcd_unit and "--initial-cluster centos-patroni-1=http://192.168.122.200:2380" in etcd_unit)
    check("initial-cluster-state new on fresh bootstrap", "--initial-cluster-state new" in etcd_unit)
    check("network-online ordering", "After=network-online.target" in etcd_unit and "Wants=network-online.target" in etcd_unit)
    check("LimitNOFILE raised", "LimitNOFILE=65535" in etcd_unit)
    check("data dir persisted in unit", "--data-dir=/var/lib/etcd" in etcd_unit)

    print("=" * 70)
    print("03_Configure_Patroni.yml — patroni.yml + systemd unit")
    print("=" * 70)
    blocks = load_blocks("03_Configure_Patroni.yml",
                         ["Create Patroni configuration file", "Create Patroni systemd service", "Deploy etcd health wait script"])
    patroni_yml = render_block(blocks["Create Patroni configuration file"])
    check("namespace defined in patroni.yml", "namespace: percona_lab" in patroni_yml)
    check("etcd3 lists ALL endpoints", "hosts: 192.168.122.200:2379,192.168.122.201:2379,192.168.122.202:2379" in patroni_yml)
    check("old single-host etcd3 removed", "host: {{ node_ip }}:2379" not in patroni_yml)
    check("watchdog section present", "watchdog:" in patroni_yml and "/dev/watchdog" in patroni_yml and "safety_margin: 5" in patroni_yml)
    check("pgpool role-signal callback present INSIDE postgresql (this build reads callbacks from postgresql subsection)",
          "    callbacks:\n        on_role_change: /usr/local/sbin/pgpool_role_signal.sh" in patroni_yml)
    check("pg_hba still intact", "host replication replicator 0.0.0.0/0 scram-sha-256" in patroni_yml)
    check("archive_command still intact", "pgbackrest --stanza=kyc archive-push" in patroni_yml)

    patroni_unit = render_block(blocks["Create Patroni systemd service"])
    check("patroni.service Requires etcd (co-located)", "Requires=etcd.service" in patroni_unit)
    check("patroni.service After network-online", "After=syslog.target network-online.target etcd.service" in patroni_unit)
    check("patroni.service ExecStartPre wait script", "ExecStartPre=/usr/local/sbin/wait_for_etcd.sh" in patroni_unit)
    check("patroni.service Restart=on-failure + never give up",
          "Restart=on-failure" in patroni_unit and "StartLimitIntervalSec=0" in patroni_unit)

    wait_script = render_block(blocks["Deploy etcd health wait script"])
    check("wait_for_etcd.sh embeds endpoints fallback", "192.168.122.200:2379,192.168.122.201:2379,192.168.122.202:2379" in wait_script)
    check("wait_for_etcd.sh uses /dev/tcp (no deps)", "</dev/tcp/$host/$port" in wait_script)
    check("wait_for_etcd.sh timeout from var", "TIMEOUT=90" in wait_script)

    print("=" * 70)
    print("04_Configure_Pgpool.yml — pgpool.conf (RHEL 4.7)")
    print("=" * 70)
    blocks = load_blocks("04_Configure_Pgpool.yml", ["Configure pgpool.conf", "Configure pgpool_watchdog.conf"])
    pgpool_conf = render_block(blocks["Configure pgpool.conf"])
    check("heartbeat_port0 = 9694 (not wd_port)", "heartbeat_port0 = 9694" in pgpool_conf)
    check("wd_quorum_exit = on", "wd_quorum_exit = on" in pgpool_conf)
    check("sr_check_period = 3 (switchover detection)", "sr_check_period = 3" in pgpool_conf and "sr_check_period = 10" not in pgpool_conf)
    check("wd_authkey from variable", "wd_authkey = 'test-auth-key'" in pgpool_conf)
    check("watchdog enabled", "use_watchdog = on" in pgpool_conf)
    check("delegate_ip for VIP", "delegate_ip = '192.168.122.200'" in pgpool_conf)
    check("NO duplicate failover_command keys", pgpool_conf.count("failover_command =") == 1)
    check("NO duplicate health_check_period keys", pgpool_conf.count("health_check_period =") == 1)
    check("NO duplicate follow_master_command keys", pgpool_conf.count("follow_master_command =") == 1)

    wd_conf = render_block(blocks["Configure pgpool_watchdog.conf"])
    check("watchdog.conf heartbeat_port = 9694", "heartbeat_port0 = 9694" in wd_conf)
    check("watchdog.conf wd_quorum_exit = on", "wd_quorum_exit = on" in wd_conf)
    check("watchdog.conf authkey", "wd_authkey = 'test-auth-key'" in wd_conf)

    print("=" * 70)
    print("Dedicated witness mode (etcd_group=etcd_nodes)")
    print("=" * 70)
    # 02 unit with witness hosts
    witness_block = load_blocks("02_Configure_Etcd.yml", ["Create systemd service file"])["Create systemd service file"]
    witness_unit = render_block(witness_block, extra={
        "etcd_member_hosts": list(WITNESS_HOSTS),
        "etcd_initial_cluster": "etcd-witness-1=http://192.168.122.210:2380,etcd-witness-2=http://192.168.122.211:2380,etcd-witness-3=http://192.168.122.212:2380",
        "etcd_initial_cluster_state": "existing",
    })
    check("witness unit uses witness cluster string", "etcd-witness-1=http://192.168.122.210:2380" in witness_unit)
    check("witness unit initial-cluster-state existing (re-run)", "--initial-cluster-state existing" in witness_unit)
    # patroni unit without Requires=etcd.service when etcd is remote
    p_blocks = load_blocks("03_Configure_Patroni.yml", ["Create Patroni systemd service"])
    p_unit_remote = render_block(p_blocks["Create Patroni systemd service"], extra={"etcd_group": "etcd_nodes"})
    check("patroni.service NO Requires=etcd when etcd is remote", "Requires=etcd.service" not in p_unit_remote)

    print("=" * 70)
    print("07_Configure_Cluster_Health.yml — scripts")
    print("=" * 70)
    blocks = load_blocks("07_Configure_Cluster_Health.yml",
                         ["Deploy Patroni self-heal script", "Deploy cluster health monitor script"])
    heal = render_block(blocks["Deploy Patroni self-heal script"])
    check("self-heal never restarts leader", 'if [ "$role" = "Leader" ]; then' in heal and "refusing to restart" in heal)
    check("self-heal uses flock", "flock -n 9" in heal)
    check("self-heal matches local short hostname", "HOSTNAME_SHORT=$(hostname -s)" in heal)
    health = render_block(blocks["Deploy cluster health monitor script"])
    check("health monitor checks etcd quorum (majority math)", "ETCD_NEED=$(( (ETCD_TOTAL / 2) + 1 ))" in health)
    check("health monitor checks leader presence", "select(.Role == \"Leader\")" in health)
    check("health monitor checks pgpool wd quorum", 'grep -qi "quorum.*exist"' in health)
    check("health monitor writes textfile metrics", "cluster_health.prom" in health)
    check("health monitor alert hook", "CLUSTER CRITICAL" in health)
    check("health monitor PCPPASS per-OS", "PCPPASS=/etc/pgpool-II/.pcppass" in health)

    print("=" * 70)
    if FAILURES:
        print(f"FAILED CHECKS ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL RENDER CHECKS PASSED")


if __name__ == "__main__":
    main()
