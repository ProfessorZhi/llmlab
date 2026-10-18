# V1 work breakdown, code ownership and dependency DAG

Issue tracker: [Batch-1 #80](https://github.com/ProfessorZhi/llm-lab-portal/issues/80) / [contract master #73](https://github.com/ProfessorZhi/llm-lab-portal/issues/73) / [production master #65](https://github.com/ProfessorZhi/llm-lab-portal/issues/65). Use the existing seven issue lanes; do not create a competing set. **Every Lane is an implementation contract, not a claim that an Agent is running**.

## Roles

| Lane / issue | Exclusive or coordinated code ownership | Concrete deliverable | Must not do |
| --- | --- | --- | --- |
| UI [#66](https://github.com/ProfessorZhi/llm-lab-portal/issues/66) | Apple V6.2 frontend source and browser tests | nine accurate pages, all states, no synthetic normal-route content, real DTO consumption | implement own fake Session/Gateway backend |
| HOST [#74](https://github.com/ProfessorZhi/llm-lab-portal/issues/74) | host Prometheus/Strata adapter + host metric validators, agreed parts of backend/observability | source-stamped read-only host data, history quality, validated real gauges | modify current GPU services or owner accounting |
| IAM [#75](https://github.com/ProfessorZhi/llm-lab-portal/issues/75) | backend/auth_linux and backend/auth | session/scopes/admin-step-up/pam-broker interface and negative tests | install PAM or edit root policy without release approval |
| REGISTRY [#76](https://github.com/ProfessorZhi/llm-lab-portal/issues/76) | backend/registry, cli/llmlab, Router adapter contract | Engine/Model/Deployment/Endpoint Owner/ACL, verify and applied ACK | pretend registered equals loaded; change production Gateway |
| CALLS [#77](https://github.com/ProfessorZhi/llm-lab-portal/issues/77) | backend/atlas and Gateway metering-specific observability module | real Key mapping, self Calls, scoped Atlas, null/percentile correctness | infer user requests from GPU gauges |
| EVALUATION [#78](https://github.com/ProfessorZhi/llm-lab-portal/issues/78) | backend/evaluation and report readers | separate EvalScope/legacy real report readers, ACL, bounded export | submit real GPU jobs without admission/approval |
| BFF [#79](https://github.com/ProfessorZhi/llm-lab-portal/issues/79) | **single writer** to backend/bff route map/assembly; operations services/events | same-origin mount, per-route auth/source guards and capability manifest | reimplement provider ACL or open arbitrary proxy |

**Split a shared module by exact file** in the first assignment message and record it in PR #80; e.g. HOST owns host-specific metrics and CALLS owns `backend/observability/gateway.py`. If an Agent needs another Lane's file, agree a DTO seam first or transfer ownership in the dispatcher log. Never allow two agents to write the same working tree or route-map file concurrently.

**Coordination roles**: Dispatcher (session inventory, queue, blocker/status, permissions); Integrator (incremental protected integration PR, contract mismatch and local full test); Quality/Release (CPU/browser/security and later isolated canary; no automatic formal deploy), with a dedicated [Linux local validation Runner #82](https://github.com/ProfessorZhi/llm-lab-portal/issues/82) producing reproducible receipts while GitHub Actions billing is exhausted. These roles do not share writable worker branches. Release [#69](https://github.com/ProfessorZhi/llm-lab-portal/issues/69), IAM/Gateway vertical acceptance [#68](https://github.com/ProfessorZhi/llm-lab-portal/issues/68), BFF quality [#67](https://github.com/ProfessorZhi/llm-lab-portal/issues/67).

## Dependency DAG and fast path

```text
F0 (PR #72 contract pin + auth+device verification)
  |
  +-- HOST  -------+---------------------------> Host/BFF read-only gate
  +-- IAM ---------+----------+----------------> UID/Session BFF gate
  +-- REGISTRY ----+----------+-- Router adapter --> endpoint ACL gate
  +-- CALLS -------+----------+-- Gateway key map --> usage & Atlas gate
  +-- EVALUATION --+----------+----------------> report scope gate
  +-- UI ----------+----------+----------------> nine-page states
                   |
                 BFF route assembly
                   |
          INTEGRATOR incremental candidate
                   |
          Offline test/security/browser gate
                   |
       Dell live source + actual UID/Gateway E2E
                   |
        protected canary + rollback validation
                   |
       operator-approved V1 production cutover
```

Develop all seven Lane contracts in parallel *where resources permit*. Integration is incremental: first Host↔BFF↔UI, then IAM↔Registry↔Gateway and Calls↔Atlas, then reports/logs. IAM is a hard prerequisite for promoting any per-user resource to production; UI offline work and Host adapter CPU tests are not blocked on PAM.

## Shared baseline and branch protocol

- Freeze a single reviewed baseline commit (currently V6.2 contract branch PR #72 is a candidate, but dispatcher must read its **latest** HEAD and pin an exact SHA when launching Batch-1).
- Each worker gets its own branch and, if using a Linux host, separate isolated worktree. First inspect existing worktree and active sessions; do not overwrite uncommitted work.
- Worker PR must state `BASE_SHA`, `HEAD_SHA`, actual changed path list, schema changes, tests and commands executed, redacted evidence, deferred runtime gates and current blocker. **Status is actual observed state, never “finished because message sent.”**
- Integrator cherry-picks/merges only a reviewed and locally tested slice, recalculates committed SHA and reruns dependent tests. CI billing status is informational; concrete test failures are blockers.
- Source contracts changed by a worker require an explicit documented version delta and BFF/UI consumer review before integration; no uncontrolled parallel DTO drift.

## Local DoD for each Lane

1. Implementation and tests committed to unique GitHub Draft PR.
2. No invented normal-route business objects, synthetic metrics or bypassed security guard.
3. Focused automated local tests and negative tests pass, with environment/command/exit code and log digest recorded; unresolved failures described.
4. Exported DTO and auth states match the canonical route inventory.
5. Real-host dependency flagged `HOST_PENDING` until measured on an approved Dell source.
6. Integrator acknowledges compatibility and tests one integrated SHA.

## Queue policy

Priorities P0: blocked access, contract drift, Host/BFF/UI truthful read-only chain; P1: IAM/Registry and Gateway calls; P1: Eval/Observability/diagnostics; P2: aesthetic refinements not hiding operational quality. Do not starve independently executable lanes because IAM host install is blocked. Default limit of **at most 3–4 heavy CPU-only agents per device**, subject to measured host load and owner policy; total sessions can exceed concurrent heavy runs if distributed over authorized Linux servers. Avoid heavy testing on the live GPU host while users' jobs are running. No Windows allocation unless separately requested.
