# Clean-Slate Deployment & Resilience Validation — 2026-08-12

**This is the definitive "everything works together from a genuinely clean deploy" run.**

It is distinct from the earlier incremental, hand-patched validation cycle:
- The cluster was deployed **fresh** from `site.yml` (playbooks 01→07, PMM 06 skipped by policy) onto 4 clean CentOS Stream 9 KVM VMs.
- No manual configuration patching was applied before validation — every fix already lives in the repo at commit `350cc2e`.
- All four failure-mode tests ran against that untouched deployment, with continuous write workload and observer sampling throughout.

## Scope

| Item | Value |
|---|---|
| Date | 2026-08-12 |
| Repo commit | `350cc2e` (main, PR #7 merged) |
| Playbooks | 01 install/deps, 02 etcd, 03 Patroni, 04 pgpool+watchdog, 05 pgbackrest, 07 cluster-health |
| Skipped | 06 PMM (per policy, commented in site.yml) |
| VPS | 144.79.249.124 (libvirt/KVM host) |
| Nodes | db1 192.168.122.150, db2 .151, db3 .152 (Patroni+pgpool+etcd), br1 .153 (pgBackRest) |
| Stack | PostgreSQL 16 (Percona) · Patroni 4.1.3 · Pgpool-II 4.7 (watchdog/VIP 192.168.122.200) · etcd 3-node · pgBackRest |

## Files

- `00_deploy_play_recap.txt` — ansible PLAY RECAP, 0 failures on all 4 hosts
- `01_baseline.md` — pre-test cluster state (leader, etcd, watchdog, VIP, pool)
- `02_test_A_powerloss.md` — 2× power-loss failover (virsh destroy), 0 lost commits
- `03_test_B_partition.md` — asymmetric network partition, bounded writable promotion
- `04_test_C_switchover.md` — clean planned switchover, single-digit-second blip
- `05_test_D_writability_detection.md` — leader-writability checker live + blip smoke
- `06_data_integrity.md` — final 11,066-row accounting, `comm -23` empty, pgbench
- `07_pgbackrest_verification.md` — stanza `kyc` check, full backup, live WAL archiving
- `08_final_state.md` — cluster at rest after the full suite

## Headline numbers

- **0** failures on deploy (ok=178/156/156/57)
- **0** lost commits across power-loss ×2, partition, switchover (all in-flight IDs retried then confirmed)
- **0** split-brain samples across all observer polls (151 + corrected-partition windows)
- **8 s** VIP write blip on clean switchover (single-digit-second, callback-driven)
- **11,066** rows in `txn_track` at final accounting, `comm -23` EMPTY
- **1,945** pgbench transactions via VIP, 0 failed, ~197 tps
