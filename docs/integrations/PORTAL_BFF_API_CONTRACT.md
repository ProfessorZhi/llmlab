# Portal V6.2 browser-facing API · design + status inventory

> This describes what frontend teams may rely on **only once deployed**. Source-of-truth for existing code remains `backend/bff/route_map.py`, `backend/auth/routes.py`, `backend/registry/api.py`, `backend/evaluation/api.py` and corresponding DTO packages. Do **not** implement a second, competing ACL, Session or token service to satisfy this document.
>
> **As-of:** integration candidate [PR #71](https://github.com/ProfessorZhi/llm-lab-portal/pull/71) `d63b0fb4a92fc6d777b64fbe340c8e1eb837b57f`. No current production host/WAF/proxy wiring is asserted.
>
> **V1 Batch-1 update (2026-10-17), BFF lane [Issue #79](https://github.com/ProfessorZhi/llm-lab-portal/issues/79):** six rows below changed state because `backend/bff` now **mounts** routes it previously only declared. The new state is `BFF-DECLARED`: the route exists in `backend/bff/route_map.py` and answers 401/403/400/503 by its own guards. It is **not** a claim of LIVE. No Dell host, PAM broker, Gateway export or projection reader has been verified; every one of these routes answers **503** on a deployment that has not wired its source. Prose and DTO seams: [`BFF_BATCH1_SEAMS.md`](BFF_BATCH1_SEAMS.md).

## 1. API surface: declared, module-only, preview-only, planned

| Method | Browser path | Code state | Principal / scope | Owner and frontend use |
| --- | --- | --- | --- | --- |
| GET | `/auth/csrf` | AUTH-MODULE | anonymous CSRF issuance | Auth; login/CSRF bootstrap |
| GET | `/auth/me` | AUTH-MODULE | session | Auth; who am I, session state |
| GET | `/auth/linux/status` | BFF-DECLARED | anonymous | IAM; PAM readiness (not credentials) |
| POST | `/auth/linux/login` | BFF-DECLARED, **503 PAM unavailable** until installed | anonymous, same-origin+CSRF | IAM; username/password → broker → session |
| POST | `/auth/logout` | BFF-DECLARED / delegated AUTH | session+CSRF | Auth; invalidates cookie and elevation |
| GET | `/auth/tokens` | AUTH-MODULE / delegated | session | own masked tokens only |
| POST | `/auth/tokens` | AUTH-MODULE / delegated; real production wiring unverified | session+CSRF+issuer scope | issue own scoped Token; one-time plaintext |
| POST | `/auth/tokens/rotate`, `/revoke`, `/revoke-all` | AUTH-MODULE / delegated | session+CSRF | Token lifecycle; disabled until verified |
| GET | `/auth/admin/status` | BFF-DECLARED | anonymous safe status | administrator step-up configured/elevated/expiry |
| POST | `/auth/admin/enter`, `/exit` | BFF-DECLARED **nonproduction admin mode** | Linux Session+CSRF | short elevation; prod eligibility ADR unresolved |
| GET | `/api/infra/summary` | **BFF-DECLARED** (Python BFF, 2026-10-17); the Node `serve.mjs` preview still exists separately | session + `observability:host:read` | Host; GPU/VRAM/temp/power/Strata service health |
| GET | `/api/v2/ops` | BFF-DECLARED, runtime adapters may be detached | authenticated session; Registry object ACL | My/Shared models, scope, service capabilities |
| GET | `/api/v2/ops/usage` | BFF-DECLARED, may return `503 observability_adapter_not_attached` | session+`observability:self:read` | self usage summary (only real Gateway truth) |
| GET | `/api/v2/ops/engines` | BFF-DECLARED, may be detached | session+`observability:self:read` | per-scope engine status |
| PUT | `/api/v2/ops/deployments/{deployment_id}/share` | BFF-DECLARED; must validate full chain | Session+CSRF+`portal:endpoints:share`, Owner ACL | owner share/unshare after runtime attestation |
| POST | `/api/v2/ops/endpoints/{endpoint_id}/invoke` | **BFF-DECLARED** (2026-10-18, closes SEAM-1 of [#68](https://github.com/ProfessorZhi/llm-lab-portal/issues/68)); a thin bridge to `RegistryService.invoke` | Session **or scoped token** + CSRF/same-origin + Registry's own `registry:endpoint:invoke` / endpoint ACL / readiness; invisible ⇒ 404 same shape as missing | same-origin call of an authorized Endpoint. Returns **invocation metadata only** (request_id, gateway adapter, metering, router-sync state) — never the model body or the prompt echo |
| GET/POST | `/api/v2/registry/{engines,models,deployments,endpoints}`, details/actions | **MODULE-ONLY** WSGI Registry API | Registry-scoped Token/Principal; Router ACK | BFF integration REQUIRED; do not assume same-origin deployment |
| GET | `/api/atlas/calls` | **BFF-DECLARED** (Python BFF, 2026-10-17); 503 `atlas_adapter_not_attached` when unwired | session + `atlas:calls:self:read`; exact current `principal` | My Calls paginated metadata only |
| GET | `/api/atlas/usage` | **PREVIEW-ONLY / NOT BFF-WIRED** | `atlas:usage:read` + authorized projects | Atlas BFF aggregation |
| GET | `/api/atlas/modules` | **PREVIEW-ONLY / NOT BFF-WIRED** | `portal:modules:read` | module navigation readiness |
| GET | `/api/evaluation/report-sources`, `/reports`, `/reports/{report_ref}` | **BFF-DECLARED** (Python BFF, 2026-10-17); 503 when the evaluation adapter is unattached | session + `evaluation:read` + exact report Owner ACL (decided inside `backend/evaluation`) | read-only EvalScope/legacy reports |
| GET | `/api/evaluation/reports/{report_ref}/download` | **BFF-DECLARED** (2026-10-17, mounted on top of PR #85); the judgement is Evaluation Lane #78 / PR #91. 503 when the seam is absent | session + `evaluation:read` + exact report Owner ACL + bounded size/path policy (all decided inside `backend/evaluation`) | **download *decision*, not bytes.** `content_read=false` + reason on this deployment; 413 over bound; never a truncated or zero-byte "success" |
| GET | `/api/evaluation/jobs`, `/jobs/{job_id}`, `/jobs/{job_id}/result` | MODULE-ONLY | evaluation read scope + per-job ACL | evaluation status and results |
| POST | `/api/evaluation/jobs`, `/jobs/{job_id}/cancel`, `/retry` | MODULE-ONLY; **real GPU runner not admitted** | owner/scope/CSRF + resource scheduler/admission | remain disabled in production until separate approval |
| GET | `/api/portal/capabilities` | **BFF-DECLARED** (Python BFF, 2026-10-17); module-level readiness manifest, no host evidence | session (the manifest is per-session by construction) | frontend discovers what is wired, never infers from UI |
| GET | `/api/portal/events` | **BFF-DECLARED** (2026-10-18) — operator-scoped, bounded, redacted; see the note below | session + `portal:admin:read` (members get 403) | bounded, sanitized audit timeline over two real structured ledgers |
| GET | `/api/portal/services` | **BFF-DECLARED** (Python BFF, 2026-10-17); the `services` slice of the *same* validated host projection | session + `observability:host:read` | fixed service list with readiness evidence |
| GET | `/api/portal/metrics/history` | **PROPOSED, NOT IMPLEMENTED** | host/owner scope | bounded proven actual time-series query, no raw PromQL |

**Why `/api/portal/events` was PROPOSED, and why it is now mounted.** Its authority is an *app-generated structured event store* (§1.1). When this document was written no such store existed, and the only things that could answer were the ones §1.1 rules out — a raw logfile proxy, an unbounded journal query, or a fixed service error list dressed up as an audit trail. That premise was overturned by the code, not by a wish: the repository already contains **two** real, bounded, attributable structured ledgers — the BFF's own elevation audit (`backend/bff/admin_mode.py`, `admin_audit`) and Registry's `audit_log` (read through `RegistryService.audit_log(requester, limit)`).

The route therefore serves exactly those two and nothing else. Properties that make it V1-11-compliant:

- **fixed-scope**: `portal:admin:read` only; a member receives `403`, never a filtered view of other people's activity.
- **bounded**: `limit` is a closed range (1..100); out of range is `400`, not a silent truncation. One page, no time-range scan, no cursor walk.
- **redacted**: only whitelisted keys are constructed (`at, event, outcome, actor, principal, uid, linux_user, admin_mode` / `at, action, actor, actor_uid, object_type, object_id`). Both ledgers' private columns — including `detail` — are **never built into the response**, rather than filtered out of it.
- **no arbitrary target**: no parameter selects a table, file, URL or PromQL expression; `?url=`, `?path=`, `?promql=`, `?cmd=`, `?uid=`, `?source=` are refused with `400` by the same closed-name guard every data route uses.
- **no fabricated freshness**: an audit ledger has no `stale` semantics. Each source reports `observed_at` (when it was read) and `newest_at` (the newest entry) as facts, plus `ok | empty | unconfigured | unreachable` — the warehouse's existing source-state words.
- **fail closed**: if *no* source is configured the route answers `503`, not an empty timeline.

Still not mounted: `/api/portal/metrics/history` (no bounded proven sample source exists yet).

**No route should be exposed at the browser** for raw `:8080`, `:8200`, `:9000`, `:9090`, `:9101`, user-provided PromQL, arbitrary journald query, executable command, filesystem browsing or unfiltered legacy benchmark download.

### 1.1 Authority and producer mapping

- `/auth/*`: Auth+PAM (identity), CSRF and token issuer; source never taken from browser request claims.
- `/api/v2/ops` / Registry: Registry database and ACL, plus actual Gateway/Router ACK for applied routing. User interface cannot infer ownership from a model name or Linux HOME path.
- `/api/infra/summary`: fixed code allowlist of Prometheus metrics through Strata adapter and fixed server health probes. No per-user usage or Owner data here.
- `/api/atlas/calls`, `/api/atlas/usage`: Gateway request metadata after verified Key→Principal/Project/Endpoint mapping and ACL; no content.
- `/api/evaluation/*`: EvalScope reports and audited legacy Benchmark as two independent source families; unknown run Owner ⇒ hidden.
- `/api/portal/events`: **two** app-generated structured ledgers (BFF elevation audit; Registry `audit_log`), read-only, operator-scoped. Never a raw logfile or journal proxy.
- Grafana/Prometheus native UI: **a guarded link/proxy** only after independently authorized per-user access; not a stand-in for typed APIs.

### Ops metric display leases (M1 additive contract)

The current `portal-ops/1` `metrics` block may contain an optional `expires_at` map:
`{ "<visible metric name>": "<timezone-aware ISO-8601 instant>" }`.
The BFF derives each deadline as the original observation time plus **that
metric's own authoritative catalogue `stale_after_seconds`**; it does **not**
reuse Host Summary's separate 90-second threshold or invent a deadline.
Only visible, trustworthy metric names may appear in this map.

The browser accepts older responses and QA fixtures without this optional field,
but numeric **LIVE** cards without a valid unexpired lease are displayed as
`unknown` with no value, source or timestamp. A timer expires each already
rendered card individually while the tab is idle; it does not refresh the
user's session or make new requests. Once expired, a clock rollback cannot
make the old snapshot LIVE again. Synthetic QA mode remains explicitly
labelled and is unaffected by this production-only restriction.

This is a backwards-compatible shape extension, not proof of live Dell
producer wiring: older clients might ignore the map, so a protected rollout
requires simultaneous client/BFF review and its own V1 host acceptance.
Missing freshness evidence is intentionally fail-closed; `null` never becomes 0.

## 2. Response semantics: producer schema first, normalized UX second

**Do not silently rewrite shipped DTOs.** The contracts are versioned independently: `portal-ops/1`, `atlas-calls/1`, `atlas-usage/1`, `atlas-aggregate/1`, `portal-modules/1` and Evaluation's own envelope. The existing `/api/infra/summary` producer emits `version=1`, `source="prometheus-readonly"`, `synthetic=false`, `readonly=true`, `observed_at`, per-metric `state/value/unit/sample_at/age_s/reason`.

A future Portal BFF may supply a **cross-module capability manifest**, but cannot make a missing source look live just by setting `synthetic=false`.

Required frontend-normalized fields for any data point (derived from its actual family schema, NOT a new enforced payload shape):

| Field | Meaning |
| --- | --- |
| `state` | ready / empty / unconfigured / denied / stale / unknown / error |
| `producer` or declared `source` | origin from allowlisted server-side adapter; no arbitrary URL |
| `schema_version` or family `version` | accepted schema/DTO version; fail closed on mismatch |
| `sample_at` | when the producer actually measured metric/event |
| `observed_at` | server observation time; not proof an underlying sample is recent |
| `unit` / reducer / `sample_count` | preserve exact meaning; don't aggregate inconsistent units |
| `value` or scoped `items[]` | `null` when unknown; 0/[] only when an authenticated source actually established zero/empty |
| `synthetic` | must be exactly false for validated real data, **but false alone does not prove LIVE** |
| `scope` / `authorized_view` | granted view/scope from current server session; not a requested identity |
| `reason` | stable safe code for not configured, detached, timeout, source missing, denied, etc. |
| `request_id` | server-generated correlation ID, no private data |

For future APIs, select a common response style **only after** a concrete shared schema review. This doc does not claim all currently existing modules use the same JSON envelope.

### 2.1 HTTP / state decision table

| HTTP / source | UI state | Correct UI behavior |
| --- | --- | --- |
| Pending | loading | clear or preserve scoped cache only under safe rules, accessible progress |
| 200 with verified usable data | ready | render only actual records/metrics, source and age |
| 200 authenticated real query with 0 visible results | empty | "no resources/calls for this scope"; never reveal others' existence |
| 200 with missing metric (null) | partial/unknown per field | do not substitute 0, interpolate, extrapolate or draw phantom lines |
| Real sample older than producer TTL | stale | show age and disable source-sensitive action; avoid pretending current |
| 400 | invalid request | explain safe validation error, no identity information |
| 401 | signed_out | login required, clear personal data and per-user view |
| 403 | denied | do not show unauthorized data or offer an unverified workaround |
| 404 in data endpoint | not_configured (only if route known unavailable) | distinguish no backend route from an authorized empty list |
| 404 on a resource | not_found_or_denied | don't reveal existence of another Owner's resource |
| 429 | rate_limited | bounded retry on Retry-After when present |
| 5xx / timeout | unavailable/unknown | show retry; no fallback synthetic data |
| `503 pam_unavailable` | not_configured | login disabled until real broker installed |
| Live endpoint behind MockGatewayAdapter | NOT LIVE | no Playground, chat or billing despite HTTP 200 |

### 2.2 Security invariants (test gates)

1. Session UID anchored in PAM; browser-provided `uid`/`owner`/`role`/`principal`/`source` cannot alter it.
2. Proxy must never forward browser-provided upstream URL, Host override, target port, file path, PromQL or journal command. Since 2026-10-17 this is enforced as a **closed parameter-name refusal** on every data route (`backend/bff/identity.py:FORBIDDEN_PROXY_QUERY_KEYS`, 400 `proxy_assertion_rejected`), not as per-handler discipline: `?url=`, `?promql=`, `?path=`, `?cmd=`, `?owner=`, `?uid=`, `?key=` are refused **by name** on routes whose handler has no such parameter at all. A parameter that is accepted would exist in the code; one that is refused has no code path to reach.
3. Browser write requires authenticated Session, same-origin Host/Origin/CSRF **and** target Owner/ACL/scope; Token handling stores a digest and displays secret once.
4. A Portal admin **read** scope never implies system root, arbitrary Engine kill, another user's raw prompts or ability to manage Owner assets.
5. A revocation and Session expiry must be reflected in the **next request**, not eventually in a client cache.
6. Atlas must **filter authorized projects before calculating** any count/latency/unknown-key diagnostics, or it leaks others' activity.
7. Per-user usage must be derived from Gateway request metadata, not Strata device GPU metrics.
8. Each route family has explicit cardinality/window/page limits, bounded timeouts, data-source freshness and no redirect-to-upstream.
9. Audit captures principal, time, action, target ID, outcome, source revision, request ID but no secrets/raw user content.
10. Credentials never appear in logs, V6 screenshots, PRs, sample curl commands, or public Grafana panel URLs.
11. **"Not wired" and "no data" are different answers, and must have different statuses.** An unattached source answers `503`; an authenticated query that legitimately found nothing answers `200` with an empty result. `200` with an invented or empty payload standing in for an unattached source is a contract violation, not a graceful fallback.
12. **A capability verdict needs positive evidence.** `ready` in the `/api/portal/capabilities` manifest requires an attached adapter *and* the session's scope *and* a sample inside the declared TTL. Absence of a complaint is not evidence; a source that has never been sampled reports `stale` with reason `no_sample_yet`.
13. **A download endpoint returns a decision, not bytes.** `/api/evaluation/reports/{report_ref}/download` answers with a plan: `authorized`, `content_read`, `max_bytes`, and — when it cannot read — a stable `reason` with `size_bytes: null`. A non-readable plan must never assert a size, a readable plan must never exceed its own declared bound, and an over-bound request is **refused (413), not truncated**. The relative path never enters the DTO.
14. **CSRF protects ambient credentials, not bearer tokens.** `POST /api/v2/ops/endpoints/{endpoint_id}/invoke` branches on the credential channel: a request with **only** `Authorization: Bearer …` is authenticated by the token channel and does **not** need session CSRF/Origin; a request with **only** a session cookie keeps the existing CSRF + same-origin preflight; a request carrying **both** is refused with `400 credential_confusion_rejected` rather than silently choosing a side. An invalid or revoked bearer is `401` — presenting one grants no exemption, because identity resolution happens before anything is forwarded. This branch exists on **this route only**; other write routes still require session CSRF.
15. **A route is not "applied" until the router authority says so.** The invoke route forwards only after Registry's own route gate. A `pending_router_sync` or `router_sync_attested` endpoint — or one whose current Router read-back revision differs, or which is absent from the publishable snapshot — is refused **before** metering and before any Gateway call (`409 registry.not_verified`). The BFF reports `router_sync` as a Registry fact and does not duplicate the gate.

## 3. BFF integration acceptance sequence

- **A. Host read-only:** allowlisted, tested `/api/infra/summary` from actual Dell Prometheus+Strata adapter; validate source/version/time and missing values. Deploy as protected read-only if approved; no Linux credentials/other user data.
- **B. Identity:** approved PAM Broker installation, `/auth/linux/login`, `/auth/me` and Cookie Session/CSRF. Test separate Linux UIDs and invalid/locked accounts.
- **C. Registry:** same principal Web↔CLI resource lists, Owner-only share, verified readiness, Router revision ACK; no weight/path exposure.
- **D. Gateway:** actual authorized inference, call metadata, self calls, attributed Atlas aggregation, next-request revoke denial.
- **E. Evaluation/Logs:** per-owner report catalogue and bounded redacted event audit, no real GPU evaluator until permitted.
- **F. Release:** all nine pages, Node/Python/Chrome, security negatives, fixed deploy SHA, canary/rollback and operator approval.

**Implementation owners should update this table row-by-row** only after the route's source, authentication, tests and real hosting evidence are verified. "Implemented in a Python module" is not the same as "V6 browser can call it".
