# Atelier V6.2 · Production interface catalogue / 开发契约入口

> **V1 delivery and operations:** [V1 consolidated architecture index](../v1/README.md), [Lane DAG](../v1/LANES_AND_DEPENDENCIES.md), [hourly orchestration](../v1/HOURLY_ORCHESTRATION.md), [local test policy without GitHub Actions credits](../v1/LOCAL_TEST_AND_RELEASE.md). Implementation statuses below are source observations; all LIVE claims require host-specific evidence.

> **Contract-first documentation, not a record of deployed production APIs.** Baseline: V6.2 [integrated code candidate #71](https://github.com/ProfessorZhi/llm-lab-portal/pull/71) @ `d63b0fb4a92fc6d777b64fbe340c8e1eb837b57f`, plus read-only legacy repository `ProfessorZhi/llm-lab` @ main. Prepared 2026-10-17.
>
> The hardware/software facts below are evidence from repository code and earlier read-only Dell inventory; **current endpoint readiness and data values must be probed and attested on the actual host**. There are NO purported live readings, fake users, fake model instances, fake calls, or arbitrary fixture charts in these documents.

## Frontend developers: start here

1. **[What should the product actually observe/manage?](PRODUCT_DATA_REQUIREMENTS.md)** — 9 workspaces, source-of-truth, units, freshness and priorities.
2. **[Local providers and capability inventory](SOURCE_CAPABILITY_INVENTORY.md)** — Dell / Strata / Prometheus / Grafana / EvalScope / Gateway / Registry / OS / legacy, including **known vs proposed** API paths.
3. **[Portal BFF route and DTO contract](PORTAL_BFF_API_CONTRACT.md)** — the browser-facing API surface, auth/scopes, error and empty semantics, shared metadata.
4. **[Linux login + administrator ADR](ADR_LINUX_IDENTITY_AND_ADMIN.md)** — separate identity, eligibility, time-limited platform admin, ownership vs root; the open decision needed before production.
5. **[Offline, no-fabrication frontend workflow](CLAUDE_OFFLINE_IMPLEMENTATION_HANDOFF.md)** — exact rules for working without access to Dell; typed unconfigured / denied / empty / stale / malformed cases instead of sample telemetry.
6. **[Machine-readable capability register](portal-capabilities.v1.json)** — structured source/route/status list for QA tooling and parallel task handoffs.

## Product principle

The portal does **not** need a JSON dump of every metric offered by every application. It needs a **small, correct data plane** that answers operational questions and permits audited, scoped actions.

```text
Dell host, Strata -> verified adapter + Prometheus -> host/resource telemetry
Existing Caddy / HTTP status -------------------> service health (fixed allowlist)
Linux PAM broker -> Session / UID / scopes -----> current viewer (future real integration)
Registry + Router ACK --------------------------> engines/models/deployments/endpoints, ownership, visibility
Gateway key directory + request metadata ------> My Calls -> aggregate Atlas / per-user counters
EvalScope + audited historical Benchmark ------> authorized report catalogue
Grafana ----------------------------------------> visualization of approved metrics (not a source of identity)
All of the above -> same-origin Portal BFF ----> V6.2 nine-page Apple frontend
```

**Do not build a second Auth/ACL rulebook in the browser.** Each current source contract remains authoritative:
[Host telemetry](../observability-contract.md) ·
[Registry and CLI](../registry-cli-contract.md) ·
[Calls](../portal-call-log-contract.md) ·
[Atlas](../atlas-data-contract.md) ·
[Modules](../portal-modules-contract.md) ·
[Evaluation](../evaluation-report-sources-contract.md) ·
[Linux PAM](../linux-pam-login.md) ·
[Temporary admin](../v6-shared-admin-mode.md).

## Scope and explicit exclusions

- Publicly committed materials: **capability definitions, typed response contracts, canonical source names, producer code pointers, review status, 401/403/404/429/503 logic, schema validators and empty-state tests**.
- Not committed: actual Linux passwords, API tokens, upstream keys, owner-home paths, raw HTTP headers/cookies, client IPs, prompt/completion bodies, private reports, usage logs, user records, sampled host/IP identifiers or personal activity.
- The repository may contain **test-only protocol stubs** returning **unconfigured/denied/stale/unknown and schema-invalid outcomes**, but not invented business records or synthetic time series that resemble real production readings. A server's factual zero must only follow an authorized real query.
- Real snapshots, if absolutely necessary for regression, are captured under separately authorized local staging, redacted, encrypted/expiring and reviewed **before** any decision to publish. This project has not received such approval.
- Existing `?demo=1` frontend assets are historical QA functionality; they are **not** data-source contracts or production fallbacks. Production may disable the demo switch entirely.
- No endpoint in this catalogue grants permission to access another user's model files, start/kill their processes, activate GPU evaluation or change Strata/Gateway/systemd/Caddy.
- Every "PLANNED" API is an implementation requirement and **not a working route**.

## Delivery parallelism / recommended ownership

| Lane | Ownership | Independent deliverable | Prerequisites for production |
| --- | --- | --- | --- |
| Data-inventory | source specs, metrics and signed source facts | catalogue + measurement coverage/gaps | host read-only audit |
| IAM | login, session, scoped CLI tokens, admin elevation | stable frontend auth contract + negative tests | operator-approved real PAM backend/broker |
| Registry | engine/model/deployment/Endpoint/ACL/revision | typed list/detail and owner→share→revoke contracts | real owner/Router integration |
| Telemetry/Calls/Atlas | trusted Prom/usage ingestion, self calls and authorized aggregates | metadata contracts and schema tests | real Gateway Key Directory and ingestion |
| Evaluation | report-sources, report catalogue, safe downloads | authorized metadata contract and empty/error tests | real EvalScope + audited legacy report owners |
| BFF integration | one stable same-origin API, error envelope, provenance and auth guard | read-only route composition + contract tests | no browser upstream ports; real Session for user data |
| Frontend | 9-page Apple V6.2 UX on those contracts | complete screens/flows even when unavailable | real gateway only for enabled write actions |
| Release | environment inventory, no-fabrication gate, backup/rollback | independent preview candidate checklist | full checks + explicit production change approval |

**Release status:** `UNWIRED/NOT_DEPLOYED` unless an independent, actual environment check states otherwise. Read [production master #65](https://github.com/ProfessorZhi/llm-lab-portal/issues/65) and [Release #69](https://github.com/ProfessorZhi/llm-lab-portal/issues/69).
