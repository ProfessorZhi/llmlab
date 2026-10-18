# Fleet device and workspace execution policy

## Placement goals

1. **Dell A6000 / Dell-LLM-Lab ({{LAB_HOST}}):** authoritative host-runtime inspection, actual GPU exporter and Strata/Gateway integration, protected canary and final host acceptance. Prefer read-only probes and CPU-bounded tests. Live GPU inference may run **only** as a separately approved minimal tenant-scoped smoke test; do not steal GPU resources or terminate other users' processes.
2. **Other Linux servers:** ideal primary home for parallel Claude Code workers, local Python/Node/browser tests, integration and polling, **once** Fleet reports the devices reachable, owner-approved and project workspace present. An earlier mention of a Linux VM is not evidence it is enrolled/authorized now.
3. **GitHub:** source, DTO truth, per-Lane PRs, blocker ledger and activity history; no CI minutes assumed.
4. **Windows workstations:** excluded from normal Batch-1 assignments, testing and orchestration. Do not use an online Windows host simply because an agent there happens to be idle.

Never invent names/capacity for additional Linux servers. Dispatcher must perform a fresh Fleet device and session inventory after explicit authorization, list exact authorized device refs and selected project IDs, record reason for placement.

## Preflight before sending or creating

- Owner authorized this **conversation/scope** for Fleet on named Linux device(s); no implication from GitHub Issue creation or generic planning.
- Device is reachable, authenticated and has required capability. Fleet message grants are per device and **per session**; `fleet:create` scope + local `allow_create` + allowed project ID are separately required to create.
- Session is truly idle/not running work (read metadata before message); avoid interrupting other tasks, never force reset sessions.
- CloudCLI project IDs are not display strings; choose only IDs exposed by the device's local policy. Confirm correct authorized real project workspace. Previous ServerOS work root and Portal artifact worktrees showed permission-request timeouts — **do not bypass via chmod, symlink, privilege change or a different machine**.
- Inspect worktree state, current branch, pending changes, repo permissions and available CPU/memory/disk. Assign isolated branch+worktree and cap concurrency.
- Record per run: `device_ref`, `session_ref`, `run_id`, `project_id`, `branch`, `base_sha`, `owner issue`, `last_observed_state`; do not paste credentials/host IPs/secret env.

## Execution matrix

| Action | Linux dev runner | Dell A6000 | Approval |
| --- | --- | --- | --- |
| Read GitHub / make isolated PR | preferred | possible if safe | standard repo/project approval |
| CPU-only tests, lint, Chrome headless | preferred | only if load permits | within restricted worktree |
| Read fixed metrics / service health | not a truth source for Dell | read-only preferred | permitted read-only scope |
| Start new GPU job / model inference | never infer Dell metrics | **off unless separately approved** | explicit resource/tenant approval |
| Change root/PAM/Caddy/systemd/Gateway/Strata/:8443 | prohibited | prohibited by default | separate precise operator approval |
| Remove real Engines/Models/weights or others' files | prohibited | prohibited | separate explicit high-risk approval |
| Use RDC as alternative | prohibited by default | only if Fleet blocked | separate named device + scope approval |

## Fallbacks and continuity

1. Authorized Linux worker free + project accessible: schedule work.
2. Fleet creation unavailable but already explicitly controllable, idle sessions exist: reuse within scope, record context.
3. Fleet/CloudCLI workspace blocked: continue **GitHub-only** docs/contract/PR review and mark `BLOCKED_ACCESS`; request local owner to approve correct project. No device-hop workaround.
4. Dell metrics unavailable: keep offline acceptance moving, do not promote source to LIVE.
5. No approved runner: Dispatcher records and reports blockers instead of claiming parallel agents executed.

Persistent orchestrator should be hosted on an approved **Linux** runner only after explicit device/service consent; a ChatGPT hourly automation can check GitHub/Fleet as tools permit but is not a guarantee that a resident systemd process exists.
