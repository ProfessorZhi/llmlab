# V1 target architecture — Atelier on Dell LLM Lab

## 0. Product requirements and non-goals

**Goal:** production-grade, multi-tenant-by-Linux-UID control plane for the existing Dell Precision 5820 / A6000 LLM laboratory, using Atelier V6.2 Apple UI as the frontend. Primary clients: browsers (members and approved operators), `llmlab` CLI, SDK users. Retain existing Strata, Gateway, evaluation sources and old portal until proven migration.

**V1 outcome:** a true, authorized, observable lifecycle — login → see actual resources → verify endpoint → share → invoke via Gateway → meter → inspect calls/Atlas → revoke → next invoke denied → audit. A second outcome is truthful host/service monitoring with provenance and sampling freshness. Evaluations are real owner-filtered reports, while production GPU job submission is a separately gated capability.

**Explicit non-goals:** artificial dashboard populations; claiming registered models are loaded; exposing arbitrary PromQL/SSH/journald paths; root-backed Web administrator; GPU utilization as per-user billing; automatic interference with live Strata, Gateway, other users' GPU jobs; automatic formal release.

## 1. Logical topology

```text
Browser (HTTPS / existing protected ingress)      llmlab CLI / SDK
             |                                             |
             +------------ Portal identity boundary -------+
                                  |
                    Portal BFF / API Facade
      Session / UID / scoped tokens / CSRF / ACL / source attestation
             |                 |                 |
     +-------+------+    +-----+-------+    +----+-----------+
     | Host telemetry |   | Resource CP |    | User workloads |
     | / observability|   | Registry/ACL|    | Gateway calls  |
     | fixed adapters |   | Router ACK  |    | Atlas aggregates|
     +-------+--------+   +------+-----+    +----+-----------+
             |                   |               |
  Strata adapter → Prometheus   Gateway → Strata / approved engines
             |                   |
  Grafana guarded read UI   Actual Deployment/Endpoint readiness
             |
  Host services/approved event sources (bounded read-only)
                      \
     Evaluation/Report reader → EvalScope and audited legacy benchmarks

Linux identity is verified through a restricted PAM broker (deployment pending
separate authorization); the browser does not access the broker or upstream
ports directly. Data ownership is always enforced on server-side principal.
```

The BFF is a control/security boundary, not an open reverse proxy. The web bundle must not embed IP/port/secret or independently infer a real resource from labels.

## 2. Domain objects and invariants

| Domain | Source of truth | Key invariant |
| --- | --- | --- |
| Principal | validated Linux `(host_id, uid)` via auth broker | Different accounts never collapse into shared `admin` |
| Session / scoped Token | Auth service / digest-backed issuer | Web temporary elevation does not upgrade CLI tokens |
| Engine | Registry + approved status adapter | Reachable port/HTTP health ≠ served model ready |
| Model | Registry and verified engine metadata | Display only real authorized rows; path references opaque |
| Deployment | verified Engine↔Model binding | Registered, verified, loaded, ready are distinct states |
| Endpoint | Registry Owner/ACL + Gateway Router actual ACK | Desired route revision ≠ route applied |
| Gateway request | Gateway trusted metadata + Key Directory | Identity assigned on server per request, never from browser |
| Atlas / personal usage | authorized Gateway events | filter by principal/project before count or percentile |
| Report | EvalScope or separately audited legacy reader | Unowned or unknown-owner report is not publicly visible |
| Host/device metric | fixed allowlisted Strata/Prometheus producer | source, units, sample_at, staleness, null vs measured 0 |
| Audit event | app/BFF-owned structured logs | actor, request ID, action, outcome; no secrets/prompts |

## 3. Deployment topology and separation

**Priority placement:** actual Dell A6000 host is the source for metrics/engine and the final acceptance target. Other Linux servers may act as isolated CPU-only development/test/integration runners **only if verified enrolled and authorized**; neither availability nor code checkout is assumed. Local Windows should not be assigned normal work. GitHub is the durable contract/PR/tracker source, not a runtime telemetry source.

Keep existing Caddy / protected HTTPS / :8443 and legacy portal intact during build. Use isolated release directories/worktrees and an explicitly protected read-only canary. Never install PAM broker, switch ingress, change systemd or Gateway routing as an implied consequence of merging a PR. Runtime external exposures require separate approved change order.

## 4. Frontend information architecture

Nine **views** (8 top-level + Compute child view), each with Loading / Ready / Partial / Empty (only confirmed by authorized query) / Not configured / Denied / Stale / Failed:

1. Overview: trusted host and selected available service signals, owned/shared verified Endpoint overview, scoped user summary when metering exists.
2. Compute: real GPU index/name, VRAM, temperature, power, CPU/RAM/disk when source-attested, freshness and historical gaps.
3. Models & Chat: Engine → Model → Deployment → Endpoint, Owner/ACL/readiness/router ACK, gated real chat stream.
4. API Management: scope-limited Token lifecycle, masked keys, authenticated endpoint docs; write only when backend verified.
5. My Calls: request IDs, actual status, model/endpoint, tokens/latency/TTFT where captured, metadata-only export.
6. Observability: separated physical GPU / service / engine / Gateway / personal request metrics, guarded Grafana link.
7. Traffic Atlas: authorized project/model/time aggregations and completeness/coverage, no synthetic default workload.
8. Evaluation: true EvalScope and legacy source catalogues, owner-authorized report detail and download.
9. Administration: own identity/session, operator-eligible elevation, source readiness, bounded redacted events and access changes.

## 5. State and capability contract

Route families keep their **existing versioned DTOs**; do not invent a second transport API to make frontend look complete. BFF may publish a new minimal operator-controlled capabilities manifest, but it cannot claim LIVE on an adapter string alone. Normal page source is trusted same-origin, otherwise Unconfigured/Denied/Unknown. Production build removes / guards all demo entrypoints. No frontend override for UID, owner, source URL or PromQL.

Write controls must gate on Session + CSRF + scope + Owner ACL + ready producer + actual Router/Gateway acknowledgement. A UI button does not constitute production authorization.

## 6. V1 sequencing and limits

- **P0 Foundation:** verified host read-only, security/error envelopes, authentic Apple pages, source manifest, development harness without fabricated business values.
- **P1 Identity + routing:** approved PAM broker, three Linux accounts, principal/session/ACL, Registry/Gateway real route health, token issue/revoke, share/revoke verified.
- **P1 Usage + evaluation + diagnostics:** attributable Gateway event ingestion, My Calls, Atlas, real ownered Evaluation reports, controlled events/services and Grafana access.
- **P1 Production qualification:** local deterministic tests, multi-UID real end-to-end, nine-page browser checks, canary, rollback; explicit cutover approval.
- **V1+ gated:** GPU evaluation job scheduler/admission, privileged machine operations, advanced alerting/quotas/cost only if authoritative source exists.

Definition of done is in [V1_ACCEPTANCE.md](V1_ACCEPTANCE.md). If a V1-critical producer cannot be deployed for security reasons, V1 is blocked and must **not** be declared achieved based on a mock result.
