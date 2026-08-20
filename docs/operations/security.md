# Security Notes — Change These Before Production

> ⚠️ **All secrets are now managed in `variables.yaml` (not in playbooks).** The repository ships with `variables.yaml.example` containing placeholder values. **Copy it to `variables.yaml`, fill in your real passwords, and encrypt with `ansible-vault` before production use:**

| Variable in `variables.yaml` | Purpose | Production Value |
|-------------------------------|---------|------------------|
| `postgres_password` | PostgreSQL superuser | **Strong random** |
| `replicator_password` | Streaming replication user | **Strong random** |
| `patroni_admin_password` | Patroni REST API admin | **Strong random** |
| `percona_password` | Percona monitoring user | **Strong random** |
| `pgpool_password` | pgpool monitoring user | **Strong random** |
| `pcp_password` | Pgpool PCP admin | **Strong random** |
| `pmm_admin_password` | PMM web UI admin | **Strong random** |
| `pg_pmm_user_password` | PMM PostgreSQL monitor user | **Strong random** |
| SSH (pgBackRest) | auto-generated keys | Already unique per deployment — store securely |

**Best practices:**

- Use **Ansible Vault** for the entire `variables.yaml`: `ansible-vault encrypt variables.yaml` — then run playbooks with `--ask-vault-pass`
- **Never commit `variables.yaml` to git** — it's in `.gitignore`. Only `variables.yaml.example` is committed.
- Restrict firewall rules to the **cluster subnet** only
- Never expose etcd (:2379/2380), Patroni REST (:8008), or PostgreSQL (:5432) to the public internet — only the pgpool VIP (:9999) and PMM (:443) should be reachable by application/admin networks
- Put the `pgpass` file somewhere private with `0600` permissions (the playbook uses `/tmp/pgpass0` for bootstrap simplicity — move it after first boot if you prefer)
- Keep PMM behind a VPN or at minimum behind strong auth (change the admin password on first login)

---

## Further Reading

- [Production Recommendations](production-recommendations.md) — hardening beyond passwords
- [Server Planning](../deployment/server-planning.md) — firewall/network topology