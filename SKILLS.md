# SKILLS.md — Operational Procedures for patroni-pgpool-ansible

> **Purpose:** Living reference document. Read at the start of any session on this repo to inherit established guardrails without re-deriving them from scratch.

---

## 1. Pre-Commit Review Procedure

**Never `git add -A` blindly.** Before staging any changes:

| Step | Action |
|------|--------|
| 1 | Run `git diff <file>` for **each modified file** individually. |
| 2 | Write a **one-line justification** per change explaining *what* changed and *why* (not just "fix"). |
| 3 | Check for **version-pinning regressions** — e.g., ensure OS-conditional blocks (`when: ansible_os_family == "RedHat"`) are not accidentally flattened, and that package names / config paths / service names remain correct for both Debian and RHEL variants. |
| 4 | Run `git status` and inspect **every untracked file** for secrets (passwords, keys, real IPs) before staging. If a file contains deploy-time secrets, **add it to `.gitignore` instead of committing**. |
| 5 | Verify `.gitignore` patterns: new entries must exclude only what's intended (e.g., `hosts` but **not** `hosts.ini.example`). |
| 6 | Run `test_render_checks.py` **after** all changes are staged — the test suite validates rendered templates, not just source diffs. |
| 7 | Only then: `git add <specific files>`, `git commit -m "<itemized message>"`. |

---

## 2. Repo Sync Verification Procedure (Six-Point Checklist)

After every push to GitHub, **on the target execution host (tioxide VPS)** run and confirm all six:

1. **Local commit hash** — `git log -1 --oneline` on local machine.
2. **Push success** — `git push origin main` completes without rejected push / merge conflicts.
3. **VPS pull output + hash** — `cd ~/git/patroni-pgpool-ansible && git pull origin main && git log -1 --oneline` on VPS.
4. **Hash match** — Local and VPS commit hashes are **identical**.
5. **Fresh `test_render_checks.py` on VPS** — Run the script **on the VPS checkout itself** (not reused local output). Must show `ALL RENDER CHECKS PASSED`. This catches Jinja/Ansible/Python version differences.
6. **Clean `git status` on VPS** — `git status` shows clean working tree, **no unexpected untracked files** (e.g., stray `hosts`, `credentials.yaml`, local logs).

**Do not proceed to any deployment playbook until all six are confirmed.**

---

## 3. Push Policy

| Scenario | Policy |
|----------|--------|
| **Foundational fixes required before *any* deployment/testing can run** (e.g., broken health checks, missing `sr_check` config, race conditions that prevent cluster bootstrap) | **One-time exception:** direct push to `main` acceptable. |
| **Fixes discovered *during or after* active testing** (failover loop, benchmark, pgBackRest validation, PMM checks) | **Branch + PR mandatory.** Create feature branch, push, open PR against `main`, review, merge. No direct pushes. |

> This distinction exists because pre-deployment fixes are "unblocking"; post-deployment fixes are "iterative improvements" that need review and traceability.

---

## 4. VM Disk Safety Rules (Permanent Ban List)

The following are **permanently banned** as recovery mechanisms after the incident where they corrupted all three VMs' backing chains:

- ❌ `virsh snapshot-create-as`
- ❌ External disk-only snapshots (qcow2 snapshot files)
- ❌ `virsh blockcommit`
- ❌ Backing-chain manipulation (rebase, pivot, commit)
- ❌ Manual qcow2 replacement / copy-on-write layer surgery

**Only acceptable recovery paths:**
1. **Patroni reinit / built-in re-bootstrap** (basebackup or pgBackRest restore from a healthy leader/replica) — allow up to 10 minutes for automatic progress before escalating.
2. **Full Ansible re-provisioning of the single failed node** (re-run playbooks targeting that host only).

> The purpose of failover testing is to validate PostgreSQL/Patroni/pgpool recovery, not to introduce additional VM-disk recovery mechanisms.

---

## 5. Failure Classification Discipline

Every test failure **must** be explicitly classified before reporting:

| Classification | Definition |
|----------------|------------|
| **HA architectural failure** | PostgreSQL/Patroni/etcd/pgpool/Watchdog stack failed to meet an acceptance criterion. |
| **Configuration/deployment failure** | The tested stack was incorrectly configured or deployed. |
| **Test-tooling failure** | The test itself produced an invalid or inconclusive result. |
| **Infrastructure/environment failure** | VM, libvirt, host, network, or other infrastructure failed independently of the HA mechanism. |
| **Inconclusive** | Available evidence insufficient to determine cause. |

**Rules:**
- Never silently convert an infrastructure flake into an HA pass.
- Never convert an infrastructure flake into an HA failure without evidence.
- Only a verified HA/configuration failure counts as an architectural failure.
- Regardless of classification, an iteration that cannot conclusively demonstrate all acceptance criteria **does not count as passing** and the consecutive-pass counter restarts at Iteration 1.

---

## 6. Health-Check Verification Discipline

Any fix to a health-check or self-heal script (e.g., `07_Configure_Cluster_Health.yml` quorum detection, VIP interface detection, backend counting) **must** be re-verified with the actual render-check output **captured after the fix is applied**, not before.

- Passing pre-fix output proves nothing about the corrected code.
- Run `test_render_checks.py` (or equivalent validation) **post-patch** and capture the fresh output.
- If the test suite doesn't structurally catch the bug (e.g., only checks keyword presence but not duplication), manually inspect the rendered script template to confirm the fix is present and not duplicated.

---

## 7. Iteration Integrity Rule (5× Failover Loop)

For the mandatory 5 consecutive successful failover iterations:

- **Any single failed iteration invalidates the entire count.**
- The test **restarts numbering from Iteration 1** after the root cause is fixed and the cluster returns to a fully healthy 3-node state.
- There is **no such thing as "5 out of 6 passed"** or "Iteration 3 failed but 4–6 passed."
- The sequence must be: `1 PASS, 2 PASS, 3 PASS, 4 PASS, 5 PASS` — five **uninterrupted consecutive** passing iterations.

> This rule exists because a single failure reveals a systemic gap; the fix must be proven stable across a full clean run, not just "eventually" passing.

---

## 8. Execution-Location Rule

**Unless explicitly stated otherwise, all infrastructure commands run from the tioxide VPS (144.79.249.124):**

| Runs on VPS | Runs Locally |
|-------------|--------------|
| `virsh` / libvirt VM ops | Repository development, editing, committing |
| Ansible deployment (`ansible-playbook -i hosts.ini ...`) | `git commit`, `git push` |
| PostgreSQL / Patroni / etcd / pgpool / pgBackRest / PMM CLI | `test_render_checks.py` (can run both, but VPS run is authoritative) |
| Failover testing (`virsh destroy`, `pcp_*` commands) | Documentation updates |
| Cluster validation | — |

> The local machine is the **authoritative working copy for repository changes**; the VPS is the **execution/control environment for the actual cluster**. Do not make uncommitted changes directly on the VPS and treat them as authoritative.

---

## 9. Step 0 Prerequisites (Run Before Every Deployment)

Before any `ansible-playbook` run:

1. `ansible all -i hosts.ini -m ping` — all VMs reachable.
2. Verify every VM's IP, hostname, OS, SSH match inventory.
3. `ansible-inventory --list -i hosts.ini` — display resolved inventory.
4. `git status && git branch --show-current && git log -1 --oneline && git remote -v` on VPS — confirm checkout is on `main`, clean, matches latest pushed commit.
5. Inspect current deployment/health-check/failover config before changing anything.
6. Report all findings — do not proceed until Step 0 is completely understood and clean.

---

## 10. Non-Negotiable Architecture Constraints

- **3-node PostgreSQL + 3-member etcd = single-node failure tolerance only.**
- Losing 2 of 3 etcd members **destroys quorum** — never test, claim, or imply tolerance of two simultaneous node failures.
- Failover test must prove: no committed transaction loss, no split-brain, automatic leader election, automatic pgpool rerouting, automatic failed-node rejoin, **quantified** client-visible write interruption (not "zero downtime" unless measured writes = 0 failed).

---

*End of SKILLS.md — update this document whenever a new guardrail is established or an existing one is refined.*