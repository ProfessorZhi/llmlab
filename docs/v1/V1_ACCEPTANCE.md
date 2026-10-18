# V1 measurable acceptance and exit conditions

**Status at creation:** all checkboxes below are unverified unless a future Issue attaches evidence; the V6.2 PRs and source-only catalogue do not automatically satisfy a runtime gate. The hourly Dispatcher must count only evidence-backed gates.

| ID | Mandatory V1 outcome | Evidence required | Owning Lane | Stage |
| --- | --- | --- | --- | --- |
| V1-01 | Normal Apple frontend shows **zero synthetic production records** on all nine views | source validation + Node/browser ordinary-URL and demo gate tests | #66 | offline |
| V1-02 | Overview/Compute show real Dell/A6000 GPU utilization, VRAM, temperature, power and exporter quality; no phantom card | actual fixed Prom sample and source/time/unit, BFF response and browser comparison | #74/#79 | host |
| V1-03 | Configured service/Engine/loaded model readiness distinguishes registration, verification, running and applied ACK | real Strata/Router probes and Registry state reconciliation | #76 | host |
| V1-04 | Three separate Linux users can log in/out using **own** credentials and have distinct UID-bound sessions | operator-approved PAM Broker and real authenticated negative tests | #75 | host + approval |
| V1-05 | Unauthorized resource and report read/write/invoke is denied across UID and tokens; session switch clears cached private data | real 401/403/404, UID spoof, CSRF, expiry, ACL and owner tests | #75/#76/#79 | host |
| V1-06 | Owner can register/verify its own Engine/Model/Deployment/Endpoint and see accurate route status | Registry state machine plus actual Engine/Router ACK proof | #76 | host |
| V1-07 | A shares authorized Endpoint to B, B calls via real Gateway, A revokes, B next request denied | real bounded invoke, request ID, route revision, authorization audit; do not affect others | #76/#68 | host + action approval |
| V1-08 | Each user's My Calls shows only actual Gateway-attributed metadata and truthful null Tokens/TTFT where missing | real per-key principal mapping, request trace, cross UID negative | #77 | host |
| V1-09 | Atlas charts/aggregates arise exclusively from scoped real Gateway events with declared coverage/window | sample-count/p95 calculation tests and real authenticated authorization proof | #77 | host |
| V1-10 | EvalScope and legacy reports are source-separated, owner-audited and downloadable only by the authorized user | real report inventories without publishing their contents, denied tests | #78/#79 | host |
| V1-11 | Platform services/logs/audit and Grafana access are fixed-scope, bounded and redacted; no arbitrary raw shell/PromQL/path proxy | BFF route/capability negative tests plus real operator-scoped request | #79 | host |
| V1-12 | Temporary Portal admin requires approved identity/step-up, time-limited scope and audit; never system root | policy decision, independent factor proof, TTL and cross-session tests | #75 | host + approval |
| V1-13 | All nine views responsive, accessible, correct empty/stale/denied/network states; model/Token writes disabled when unverified | Node + headless browser screenshots/checks in both themes, five viewport widths | #66 | offline + canary |
| V1-14 | Integrated code is reproducible from one commit with local Node/Python/security/browser/extract evidence despite GitHub Actions quota | pinned SHA, redacted real logs with exit codes/digests, manual review | Integrator | offline |
| V1-15 | Protected V1 deployment has tested baseline backup, noninterference and rollback; explicit owner GO | reachable URL under authorized access, exact deployed SHA and rollback drill | #69 | host + approval |

## Semantics of closure

- **Source-complete**: seven work Lanes have PRs and their own offline tests. Does not mean V1 accepted.
- **Integrated-code**: Integrator has single tested commit and no unresolved contract/security failures. Does not mean any Dell producer is LIVE.
- **Host-verified**: V1-02..V1-12 have actual scoped real-host evidence and no cross-tenant access.
- **V1 production complete**: **all mandatory V1-01..V1-15** evidence recorded, with reviewed rollout and healthy protected entry, and no unresolved critical security regressions. Only then close dispatch loop and disable hourly automation.
- Pending operator approval or host access is a legitimate BLOCKED state; never fake a success or silently relax criterion to close the project.

## Milestone grouping for parallel delivery

- **M0 / bootstrap**: device/session permission, worktree inventory, exact frozen baseline, tested local harness; #80.
- **M1 / truthful UI**: V1-01/02 offline and first source-backed protected read-only canary.
- **M2 / real multiuser control**: V1-03..07 + V1-12; Auth/Registry/Router and real permission cycle.
- **M3 / complete observability**: V1-08..11 including actual Calls/Atlas/Eval/Events.
- **M4 / production**: V1-13..15 final integration, browser and rolling release/rollback.
- Tasks may execute across milestones in parallel when dependencies permit, but **milestone acceptance cannot skip an upstream security gate**.
