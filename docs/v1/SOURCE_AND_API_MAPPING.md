# Integration map — source-of-truth to BFF to UI

**Read together with:** [existing source inventory](../integrations/SOURCE_CAPABILITY_INVENTORY.md), [route inventory](../integrations/PORTAL_BFF_API_CONTRACT.md), [machine-readable route/metric inventory](../integrations/portal-capabilities.v1.json). Those contain exact repository evidence. This file defines the V1 delivery boundaries and priority; it does **not** assert current Dell connectivity.

## Authority matrix

| Integration | Required authoritative facts | Proposed consumption / frontend surface | Known uncertainty | Work owner |
| --- | --- | --- | --- | --- |
| Strata | real loaded models, engine health, actual status, exported GPU observations | guarded server adapter → BFF / models and service health | `/v1/models` and `/v1/status` require live verification; health alone not ready | #74 / #76 |
| Strata metrics adapter | exported fixed llmlab_strata metrics with help/unit/sample | Prometheus and read-only HostSummary | exporter availability and scrape age need live check | #74 |
| Prometheus | physical GPU utilization, VRAM, temperature, power + verified CPU/RAM/disk and bounded history | fixed query whitelist, normalised source-stamped BFF, Overview/Compute/Observability | existing Node preview is not Python BFF mount; 90s stale default in host contract | #74 / #79 |
| Grafana | visualisation of Prometheus series, dashboards | guarded access link / proxy with independently reviewed access policy | not an ACL / model / user identity authority | #79 |
| Linux NSS/PAM | real permitted host UID and login authentication | restricted PAM Broker → Auth Session → BFF | `UnavailablePamBackend` is present, broker installation pending approval | #75 |
| Auth/Token | CSRF, expiry, per-session temporary-admin, scoped hashed Token | `/auth/*`, BFF call gates, Admin and API pages | production step-up method and exact eligibility unresolved | #75 / #79 |
| Registry | Engine, Model, Deployment, Endpoint, ownership/shares | BFF scoped `/api/v2/registry/*` and `/api/v2/ops*` | WSGI module presence ≠ mounted authenticated production API | #76 / #79 |
| Gateway Router | actual upstream target and accepted route revision | verified invoke, applied routing ACK, endpoint readiness | cannot mark applied before revision ACK; no proxy around existing ACL | #76 |
| Gateway metering | request_id, server-mapped key/principal/project, tokens, TTFT and latency when captured, status | self Calls and ACL-filtered Atlas via BFF | trusted Key Directory, timestamp coverage and export validation pending | #77 / #79 |
| EvalScope | actual report run/dataset/metric/result and owner provenance | scoped evaluation report list/detail/download | native UI :9000 not a Portal authentication source | #78 / #79 |
| Legacy Benchmark | historical report source with audited Owner mapping | separate source-labelled catalogue | do not expose old anonymous file-serving endpoints | #78 |
| OS service events | allowlisted service status and bounded, sanitized error/audit events | proposed `/api/portal/services`, `/api/portal/events` | planned, no arbitrary shell, path or journal query | #79 |

## Host metric shape, freshness and source policy

Per numeric metric keep `producer/metric_id`, `value` or `null`, `unit`, `state`, `sample_at`, `observed_at`, `age_s` and a safe `reason`. Correct 0 requires an attested actual sample; missing 0 is not a sample. Reject unknown metric names, future-dated samples (>15s tolerance), stale host gauge (>90s default) and implausible units/ranges; return Partial/Unknown rather than a fabricated zero. For multiple GPUs keep per-device labels and never sum or max across unrelated physical cards unless explicitly defined. Validate history coverage before drawing a trend; gaps remain gaps.

Gateway token and latency fields are **nullable** (some engines do not report them), percentile calculation uses raw authorized sample population. Do not put prompt/response, passwords, API token secrets, arbitrary filesystem paths or private scrape labels in response data.

## Browser API integration order

1. First establish endpoint contracts and cross-module states. The existing `GET /api/infra/summary` is **preview-only**, the `/api/v2/ops` family is declared but may be detached; production cannot pretend otherwise.
2. Mount and verify read-only HostSummary with trusted producer, guarded services/capabilities. BFF route declares `auth`, `scope`, `source`, `owner`, `handler`, limit and timeout.
3. Once PAM is approved, use Linux-backed Session on every personal read; no injected `uid` from query/body/headers.
4. Attach Registry and verified Gateway Router; only then enable owner-specific Endpoint writes and model Chat.
5. Attach trusted Gateway Key Directory → Calls → authorized Atlas aggregation; personal GPU usage never inferred from host metrics.
6. Attach EvalScope and legacy reports separately and scoped event API; verify Grafana tenancy.
7. Keep evaluation GPU-runner starts, raw host operations and cross-user management disabled until separately approved.

## API / error semantics

| Observation | User-facing state | Correct action |
| --- | --- | --- |
| fresh, validated, authorized real data | LIVE or PARTIAL per verified field | show exact source and timestamp |
| authorized query proved zero visible rows | EMPTY | empty table, not empty global database |
| known adapter/route disabled or 503 detached | UNCONFIGURED | read-only explanation, disable write |
| signed out / expired session | AUTH_REQUIRED | clear all user-scoped caches |
| insufficient scope / hidden object | DENIED / NOT_FOUND_OR_DENIED | no existence or count disclosure |
| source stale/unreachable/bad schema | STALE / UNKNOWN / ERROR | clear unsafe live claim, retry bounded |
| 429 / Retry-After | RATE_LIMITED | bounded retry, no request storm |
| test-state harness | TEST_ONLY | can exercise UI states but **never** claim real business results |

## Proof ledger required for promotion

Every provider gets: repository module, owning issue/PR/SHA, adapter version, BFF route mapping, source proof, security test evidence, runtime host/date/collector, authorization scope, exact data quality/time observed, **separate** LIVE status and owner review. Label early states SOURCE_ONLY / MODULE_ONLY / BFF_DECLARED / BFF_WIRED_UNVERIFIED / HOST_VERIFIED / LIVE_AUTHORIZED, and never skip levels.
