# Atelier V1 production blueprint — 2026-10-17

> Status: **target design / not a statement that V1 is deployed**. V1 is the release objective for the V6.2 Apple interface, not a request to abandon the current UI. Baseline: [V6.2 UI PR #71](https://github.com/ProfessorZhi/llm-lab-portal/pull/71) and [source contracts PR #72](https://github.com/ProfessorZhi/llm-lab-portal/pull/72). Runtime measurements, login and deployment must be re-attested on Dell.

## One target, two kinds of completion

1. **CODE INTEGRATED** — source changes are integrated and all required local/CPU/browser/security tests are evidenced. This milestone can proceed with no A6000 connection and no GitHub Actions minutes.
2. **V1 HOST VERIFIED** — actual Dell/A6000 producers, independent Linux identities, Gateway routing, audit, authorized usage and protected HTTPS browser access have been checked. **V1 PRODUCTION** additionally requires explicitly approved release operations, backups and rollback.

Never promote a component from source-described / module-only / BFF-declared to LIVE based on code existence or a TCP listener. On the normal production UI, fake models, example keys, fabricated calls, synthetic performance charts and invented reports are forbidden.

## Canonical reading order

| Document | Owner / decision |
| --- | --- |
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | V1 definition, trust boundaries, runtime architecture and user journeys |
| [SOURCE_AND_API_MAPPING.md](SOURCE_AND_API_MAPPING.md) | Strata, Prometheus, Grafana, Linux, Registry, Gateway, Atlas, EvalScope, events → browser BFF and status |
| [IAM_SECURITY.md](IAM_SECURITY.md) | Own Linux credential, stable UID, ACL, temporary elevation, write gates |
| [OBSERVABILITY.md](OBSERVABILITY.md) | Business questions, signal provenance, service/metric freshness and redacted diagnostics |
| [LANES_AND_DEPENDENCIES.md](LANES_AND_DEPENDENCIES.md) | Non-overlapping development ownership, dependency DAG and handoff DoD |
| [DEVICE_EXECUTION_POLICY.md](DEVICE_EXECUTION_POLICY.md) | Dell and explicitly authorized Linux servers first, no Windows by default, Fleet limits |
| [HOURLY_ORCHESTRATION.md](HOURLY_ORCHESTRATION.md) | Durable dispatcher control loop, idempotence, recovery, authorization and stop conditions |
| [LOCAL_TEST_AND_RELEASE.md](LOCAL_TEST_AND_RELEASE.md) | GitHub CI exhausted policy, locally generated evidence and release gates |
| [V1_ACCEPTANCE.md](V1_ACCEPTANCE.md) | Measurable testable V1 acceptance matrix, distinguishing offline and host evidence |
| [lanes.v1.json](lanes.v1.json) | Machine-readable issue / ownership / dependency catalogue (no agent sessions or telemetry values invented) |

## Source-of-truth precedence

- Existing code and schemas are authoritative about **what is implemented**. This plan defines the **target**.
- The [2026-10-17 producer catalogue](../integrations/SOURCE_CAPABILITY_INVENTORY.md), [machine register](../integrations/portal-capabilities.v1.json), [route inventory](../integrations/PORTAL_BFF_API_CONTRACT.md) and [Linux admin ADR](../integrations/ADR_LINUX_IDENTITY_AND_ADMIN.md) constrain implementation. Treat their statuses as as-of-document, not live health.
- [Architecture](../architecture.md), [production master #65](https://github.com/ProfessorZhi/llm-lab-portal/issues/65), [contract master #73](https://github.com/ProfessorZhi/llm-lab-portal/issues/73) and [dispatch master #80](https://github.com/ProfessorZhi/llm-lab-portal/issues/80) remain valid; this V1 package is a focused execution baseline, not an alternate backend or permission system.
- GitHub Actions red / absent because quota is depleted is **NOT** treated as a code failure and does not block development iteration, but required independent local tests and source/runtime verification cannot be skipped. No bypass of protection/security approval solely because CI is exhausted.

## V1 definition in one sentence

Three independently authenticated Linux principals should be able to use a real, attractive nine-view LLM control plane to understand trustworthy host/engine/service health, discover only authorized resources, invoke verified routes, manage owned/shared endpoints, view attributed request metadata and evaluated reports, and diagnose changes through redacted audit, with proven rollback and without disturbing existing workloads.
