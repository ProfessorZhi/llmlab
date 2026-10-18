# Hourly coordination and continuation loop — V1 until acceptance

The durable task source is GitHub: [#80 Dispatcher](https://github.com/ProfessorZhi/llm-lab-portal/issues/80) plus worker Issues [#66](https://github.com/ProfessorZhi/llm-lab-portal/issues/66), [#74–#79](https://github.com/ProfessorZhi/llm-lab-portal/issues/73). ChatGPT scheduled hourly task is the **check and coordination trigger**, not a permanent open Claude Code process. Any unattended persistent process requires approved Linux host/runners and explicit capability; it is not assumed.

## Each hourly tick

1. Read #80, V1 gate matrix and linked work issues/PRs; inspect exact remote SHA and most recent test evidence. Read previous `run_id`/session log and avoid repeating actions already completed.
2. **Only after explicit Fleet authorization recorded** and within its device/session boundaries, check Fleet device reachability and each session lifecycle/run. Do not interpret a recently sent command as a finished result; verify latest run/PR commit.
3. Compare against dependency DAG: unassigned ready work, active but no evidence, finished with pending integration, integration failure, host approval blocked and release approval blocked.
4. For each finished approved worker PR, hand off to Integrator (if permitted/idle), preserving producer+consumer schemas. For red local tests, direct **targeted repairs** to the owning worker; do not blanket-merge or overwrite another worktree.
5. For newly unblocked independent work, assign/reuse only idle authorized sessions on Dell or preferred Linux servers under concurrency ceilings and exact branch ownership. Do not spin an unbounded number of agents.
6. Update durable issue board with exact timestamp (UTC), issue/session/branch/run/SHA, tested/host states, blockers and next action. Report substantive progress, newly unblocked work, failed gates or approval needed.
7. Once **every** item in [V1_ACCEPTANCE.md](V1_ACCEPTANCE.md) is verified with concrete live evidence and approved final release/rollback, mark `V1_VERIFIED_COMPLETE`, stop further dispatch and **disable hourly schedule** if scheduling controls are available. If schedule cannot be disabled, inform the user and do not continue dispatch.

## Required state machine

```text
NOT_STARTED → READY → ASSIGNED → RUNNING → PR_OPEN
        ↘ BLOCKED_ACCESS / BLOCKED_DEPENDENCY / BLOCKED_APPROVAL
PR_OPEN → LOCAL_TESTED → INTEGRATED → HOST_VERIFIED → RELEASE_APPROVED
                                  ↘ NEEDS_FIX → READY
RELEASE_APPROVED → V1_VERIFIED_COMPLETE (terminal, no dispatch)
```

All status transitions require evidence:
- **RUNNING** = actual Fleet run/session observed, not an Issue checklist.
- **PR_OPEN** = remote GitHub PR exists with expected changes and SHA.
- **LOCAL_TESTED** = all mandatory local tests have real commands, exit statuses, machine environment, run time and log/reference hashes.
- **INTEGRATED** = Integrator candidate contains relevant commit and cross-contract tests pass.
- **HOST_VERIFIED** = observed Dell authorized source/sample/UID behavior.
- **RELEASE_APPROVED** = separate explicit owner approval and non-destructive rollout/rollback plan.
- **COMPLETE** = protected V1 reachable with all acceptance gates tested, not just all agents stopped.

## Idempotence and failure handling

Use `work_key = issue_number + scope + base_sha + attempt` for dispatch ledger, store per-agent session/run and latest PR head. An hourly retry **first queries last known run**: if still running, don't issue the same task; if completed, inspect files/tests; if lost/error, record failed and retry only if safe and after checking worktree.

Cap retry loops (e.g. 2 automated assignment retries per unchanged blocker; further repetitions become `NEEDS_OPERATOR`). Don't re-create a session or duplicate PR every hour. Dispatch in dependency order and limit by device load; no interrupt/force push. If no authorization, simply update GitHub-only state and ask operator.

**Never auto-approve** Linux PAM/root, Gateway/Router writes, Strata, Caddy/systemd, host firewall, model deletions, GPU jobs or :8443 switching. A scheduled instruction cannot grant device privileges. Permanent Windows fallback prohibited.

## CI credits exhausted

Ignore **only** GitHub Actions non-run/redness attributable to exhausted billing/quota as a scheduling blocker. A real failing test, incomplete coverage, build mismatch, code review finding or host security fault remains blocking. Use authorized Linux/Dell local tests and report exact command, exit, runtime, SHA and SHA-256 of logs/evidence, optionally a redacted artifact link. Do not falsify GitHub CI as green or remove branch protection automatically. If protected branch requires unavailable CI, maintain tested Draft integration candidate and request explicit repository-maintainer decision instead of overriding policy.

## Reporting format per tick

```text
V1 checkpoint (UTC)
Target head SHA:
Device/session availability: verified / unknown / no auth
Lane statuses (#66,#74,#75,#76,#77,#78,#79):
Integrated candidate SHA and exact tests:
Host verified sources / missing:
Critical path blocker (owner + next action):
Dispatches this tick (confirmed run ids only):
Needs user operator approval:
V1 acceptance X/Y verified (no premature release claim):
```

Scheduled task should emit a concise update only when meaningful progress, blocker, repair dispatch or completion occurs; no need to notify all hourly checks without a change, but must still perform the check. If scheduler supports deactivation after V1, turn it off.
