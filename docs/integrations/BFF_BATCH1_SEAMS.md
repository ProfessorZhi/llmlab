# BFF Batch-1 seams — what other lanes must hand over

> Owner: BFF / sole route integrator ([Issue #79](https://github.com/ProfessorZhi/llm-lab-portal/issues/79)).
> Baseline: `docs/v1-production-architecture-agent-orchestration-20261017` @ `5ea80535a3ccdde63281314324195cd727a94700`.
> Companion: [`PORTAL_BFF_API_CONTRACT.md`](PORTAL_BFF_API_CONTRACT.md).
>
> **Round 2 (this document's §6–§9).** Round 1 shipped the routes on
> `feature/v1-bff-auth-safe-routes-20261017` @ `6062ed391f9d2361600081cc7b84d141eca3de8d`
> (PR #85). Round 2 stacks on it and **wires the real adapters** the sibling lanes
> delivered. Exact heads consumed:
>
> | Lane | PR | HEAD | What BFF took |
> | --- | --- | --- | --- |
> | Host #74 | #88 | `1e5df8f6c163e2c9bc9355a8f0f93e4557106449` | `host_summary_reader()` |
> | Calls #77 | #90 | `9cf5ca268c7659164c018f1e1175c0e95924ee3f` | `build_atlas_source()` |
> | Evaluation #78 | #91 | `49c3c360ad963673c1cdbd6b1c208b4523a9bd3c` | `EvaluationService.report_download` |
> | Registry #76 | #89 | `fbba15d9e4360e2c45fe415f36199df4c4b6cb3a` | **nothing — see §9** |

`backend/bff` owns **wiring and guards**. It does not own any provider's behaviour, and
it does not re-decide an ACL that another module already decides. This file is the
hand-over: for each source the BFF now mounts, exactly which object a lane injects,
what the BFF does with it, and what it refuses to do.

The table is not new governance — it is the prose form of
`backend/bff/capabilities.py:SOURCE_SEAMS`, which is executed by
`tests/bff/test_capabilities.py`. **If the two disagree, the test is right.**

## 1. The injected object per source

| Source | Injected as | Provided by | BFF does | BFF refuses |
| --- | --- | --- | --- | --- |
| `registry` | `OpsApplication(service=...)` | Registry/Gateway [#76](https://github.com/ProfessorZhi/llm-lab-portal/issues/76) | renders `portal-ops/1` from `RegistryService.policy(requester)` | re-deciding row-level visibility; it reads `visible_*` and nothing else |
| `observability` | `OpsApplication(observability=...)` | Host [#74](https://github.com/ProfessorZhi/llm-lab-portal/issues/74) | bridges the trusted principal, passes the injected corpus through | reading a file, opening a socket, or inventing a metric source |
| `host_summary` | `OpsApplication(host_summary=...)` | Host [#74](https://github.com/ProfessorZhi/llm-lab-portal/issues/74) | calls a **zero-argument** `read()` and runs `validate_host_summary` server-side before the payload reaches the browser | accepting any address, query or window from the request |
| `atlas_calls` | `OpsApplication(atlas=...)` | Calls/Atlas [#77](https://github.com/ProfessorZhi/llm-lab-portal/issues/77) | bridges the trusted principal and the trusted project directory, then calls `build_self_calls_dto` | exposing `requested_key` / `requested_subject` / `content_consent` as request parameters |
| `evaluation` | `OpsApplication(evaluation=...)` | Evaluation [#78](https://github.com/ProfessorZhi/llm-lab-portal/issues/78) | forwards `reports` / `report_detail` / `report_sources` verbatim | re-mapping `source`, or granting `include_unowned` itself |

## 2. What each lane still has to deliver before a route can answer 200

Nothing here is a criticism of the provider code — it exists and is tested. What is
missing is the **host-side** half: the object a deployment injects.

### Host [#74] — `/api/infra/summary`, `/api/portal/services`
- **Needed:** a `HostSummaryStack(read=...)` where `read()` returns the parsed
  `prometheus-readonly` projection (or `None` when nothing has been fetched).
  Production should inject a reader backed by the refresh cycle in
  `backend/observability/host_refresh.py`, **not** a bare HTTP client: caching is a
  deployment decision, and the BFF deliberately does not cache.
- **Current state on this branch:** `read()` returns `None`. Both routes answer
  `503 observability.source_unavailable`, and the capability manifest reports
  `host_summary: stale / no_sample_yet`. That is the correct answer for a deployment
  with no projection, not a placeholder.
- **Not verified:** whether Dell's Prometheus actually exposes the six metric names in
  `HOST_METRIC_UNITS`, and whether the projection tool is deployed. `UNVERIFIED`.

### Calls/Atlas [#77] — `/api/atlas/calls`
- **Needed:** an `AtlasStack(directory=..., provenance=..., gateway_text=..., project_scope=...)`.
  - `directory` — a `backend.atlas.identity.KeyDirectory` of approved
    `key_id → principal/project/workload` bindings. This is the **only** attribution
    authority; the BFF never derives ownership from a name or a Linux home path.
  - `gateway_text(identity)` — the Gateway metadata corpus for that session. The BFF
    never reads a file itself.
  - `project_scope(identity)` — the projects that principal may see; the BFF passes the
    **same** `ProjectDirectory` the evaluation stack uses, so one deployment has one
    answer to "who belongs to which project".
- **Current state on this branch:** wired to a synthetic corpus in the test harness
  only. A real deployment gets `503 atlas_adapter_not_attached` until a directory and
  corpus are injected. `transport.mode` stays `demo` while `provenance.synthetic` is
  true — the BFF copies Atlas's own rule rather than overriding it.
- **Not verified:** the real `usage.jsonl` path, its retention, and whether the
  Gateway writes the fields `backend/atlas/ingest.py` requires. `UNVERIFIED`.

### Evaluation [#78] — `/api/evaluation/report-sources`, `/reports`, `/reports/{report_ref}`
- **Needed:** a `ModuleEvaluationAdapter` (already in `backend/bff/evaluation_adapter.py`)
  over an `EvaluationStack(service=...)`. The three report methods already exist as
  thin pass-throughs; this round only **mounts** them.
- **BFF refuses** to re-map `source` (`evalscope` vs `legacy-benchmark`) and refuses to
  decide `include_unowned`. Both stay where they are decided: inside
  `backend/evaluation`.
- **Not verified:** whether any real report root is configured on Dell. The adapter
  answers from whatever the module reports, including `unconfigured`.

### IAM [#75] — sessions, CSRF, elevation
- **Needed:** nothing new. `SessionPrincipalSource` + the existing `_preflight` are the
  only credential and CSRF paths, and no route added in this round introduced a second
  one. `POST /auth/linux/login` still answers `503 pam_unavailable`.

## 3. What this round deliberately did **not** mount

- **`/api/portal/events`** — **superseded: it is mounted as of 2026-10-18, see §11.**
  When this section was written its authority (an app-generated structured event store)
  did not exist. Two real, bounded, attributable ledgers have since been identified in
  the code, so the route now serves exactly those two and nothing else.
- **`/api/portal/metrics/history`** — a bounded time-series endpoint over proven
  actual samples. The samples do not exist yet; the capability manifest reports
  `host_summary` as `stale`, which is the honest precondition.
- **`/api/v2/registry/*`** and **`/api/atlas/modules`** — still `404` on the same-origin
  BFF, pinned by `tests/bff/test_route_map.py`. Wiring them is a wiring change, not a
  data change, but no lane has asked for it and no route was invented to fill the gap.

## 4. The manifest is the seam declaration

`GET /api/portal/capabilities` returns one row per source with a **closed** four-value
state:

| state | meaning | who can see it |
| --- | --- | --- |
| `ready` | adapter attached **and** scope granted **and** a sample inside the declared TTL | this session |
| `stale` | attached and permitted, but no sample inside the TTL (`no_sample_yet`, `sample_aged_out`, `sample_in_future`) | this session |
| `auth_required` | attached, but **this session** lacks the scope | this session |
| `not_configured` | not wired in this deployment | anyone |

Two things it is not: it is **not** a per-metric freshness report (each module's own DTO
carries that), and it is **not** a LIVE claim. `ready` describes one deployment's wiring
plus one session's grants. A `ready` source on a host nobody has inspected is still
`HOST_PENDING`.

`not_configured` vs `auth_required` is the distinction the manifest exists for: both
render as an empty panel in a frontend that only looks at HTTP status, and they send an
operator in opposite directions.

## 5. Status vocabulary for this round

Per [`SOURCE_CAPABILITY_INVENTORY.md`](SOURCE_CAPABILITY_INVENTORY.md) §status vocabulary:

- The six routes above are **`BFF-DECLARED`** — declared in `route_map.py`, may answer
  401/403/400/503, and need their adapter. This is a source-level statement, not a host
  observation.
- **`MODULE-ONLY` does not become `LIVE` by being mounted.** `backend/atlas` and
  `backend/evaluation` are still module-only in the sense that matters: no live host data
  has flowed through them.
- **`HOST_PENDING`**: every source is unverified on Dell. Nothing in this round asserted
  otherwise, and no fixture, example or demo payload is reachable from a production route.

## 6. Round-2 injection points (exact, executable)

The BFF does not re-implement any of these. It calls the seam and maps the result into
its own stack. All three live in `backend/bff/stacks.py`.

### 6.1 Host → `build_host_summary_stack_from_observability()`

```python
from backend.bff.stacks import build_host_summary_stack_from_observability
stack, reason = build_host_summary_stack_from_observability(enabled=True)
# stack is None  → reason == "host_source_reader_not_delivered"  (PR #88 not in this tree)
# stack is not None → stack.read is backend.observability.sources.host_summary_reader(...)
```

- **Import is lazy and isolated** in `_load_host_summary_reader()`. A lane's merge order
  must not decide whether another lane's module can be imported; the failure is a stable
  reason, not an `ImportError` traceback.
- `enabled` is passed through unchanged. Host Lane's default is **`False`**, which makes
  `read()` return `None`; the route then answers `503 observability.source_unavailable`.
  That is "not wired", not "empty".
- The BFF does **not** cache and does **not** add a URL/PromQL parameter. `read()` stays
  zero-argument — that is the seam's security boundary, and the BFF keeps it.

### 6.2 Calls/Atlas → `build_atlas_stack_from_source()`

```python
from backend.bff.stacks import build_atlas_stack_from_source
stack, reason = build_atlas_stack_from_source(atlas_source, project_scope=...)
# atlas_source is backend.atlas.corpus.AtlasSource (duck-typed; not imported)
```

- `project_scope` has **no default**. An empty authorised-project set makes Atlas refuse
  every call, which renders as "this person never called anything" rather than as a
  misconfiguration. The signature forces the assembly to own that decision.
- `AtlasSource.available is False` ⇒ the whole source is not wired and `reason` carries
  the source's own code (`source.file_missing`, `source.outside_root`, `source.too_large`,
  …). Those are stable, content-free, and tell an operator which bound failed.
- The BFF copies `gateway_attached` verbatim. It never infers it from "the file was
  readable" — Atlas's own rule is `live = gateway_attached and not synthetic`.

### 6.3 Evaluation → `ModuleEvaluationAdapter.report_download()`

- Added to `backend/bff/evaluation_adapter.py` as a thin bridge over
  `EvaluationService.report_download` (PR #91). `supports_download` answers whether the
  service in this tree has the method, so an older tree yields **503**, not an
  `AttributeError` → 500.
- **Status mapping is BFF-specific.** `backend/evaluation/api.py:CONTRACT_STATUS` maps
  `NotConfigured` → 501; the browser contract wants **503** for "this source is not in
  this deployment". `backend/bff/app.py:EVALUATION_STATUS` is the BFF's own table: the
  module's map with `NotConfigured` → 503 and `evaluation.report_download_refused` → 413.
  Two maps, two readers; neither is secretly the other.
- `Atlas` gained the same treatment: `ATLAS_STATUS` maps `atlas.source_unavailable` and
  `atlas.unverified_source` → 503, and a `SourceUnavailable` surfaces its own `reason`
  as the error token.

## 7. The fourth evaluation route

`GET /api/evaluation/reports/{report_ref}/download` is declared in `route_map.py`
**before** `evaluation.report_detail`. Both are prefix-pattern routes; today
`report_detail` (empty suffix) rejects any middle segment containing `/`, so it would not
swallow `/download` — but relying on another route's internal check for ordering is a
wire that eventually breaks. The order is therefore explicit.

Security properties, each pinned by a test in `tests/bff/test_real_adapter_wiring.py`:

| Property | Evidence |
| --- | --- |
| identity is the session, never the request | the seam double records the `requester` it received and it equals the session principal |
| the path is not a parameter | `?path=` / `?url=` / `?source=` → 400 before the module is reached |
| over-bound refuses instead of truncating | `evaluation.report_download_refused` → **413** |
| unreadable means unknown size | `content_read=false` ⇒ `size_bytes` is `null` |
| paths never reach the browser | the response body contains no `path_ref`, no `../`, no absolute path |
| missing/invisible shares one answer | `NotFound` → 404, no existence hint |

## 8. Verified cross-lane, locally, without touching anyone's branch

A throwaway clone (never pushed, deleted afterwards) merged #88 + #90 + #91 onto the BFF
branch with **zero conflicts** and ran the full BFF suite plus an end-to-end probe:

```text
merged probe tree (6062ed3 + 1e5df8f + 9cf5ca2 + 49c3c36)
  python3 -m unittest discover -s tests/bff -t . -p 'test_*.py'
    → Ran 392 tests ... OK            exit 0

real #88 reader → HostSummaryStack → /api/infra/summary
  → 200, accepted=true, source=prometheus-readonly, version=1, 5 live metrics
  → /api/portal/capabilities: host_summary = ready, ttl=90

real #90 AtlasSource (temp key-directory doc + corpus file) → /api/atlas/calls
  → 200, 1 row, scope.keys=["key-probe"], transport.mode="demo" (synthetic, not fake LIVE)

real #91 EvaluationService → /api/evaluation/reports/evrp_unknown/download
  → 404 evaluation.not_found     (?path= → 400, ?source= → 400)
```

This is **local evidence only**. It is not an integration claim, not a merge, and not a
LIVE assertion: no Dell host was contacted.

## 9. Registry #89 needs **no** BFF change — and that is checkable

PR #89 changes the meaning of the `router_sync` block (`router_sync_applied` now requires
Gateway read-back evidence; an operator integer yields `router_sync_attested`). The BFF
does not render that block: `/api/v2/ops` deployment rows come from
`registry_dto.render_deployment` → `Deployment.to_dict()`, and `router_sync` is an
**endpoint** view (`Endpoint.to_dict()`). Pinned by
`tests/bff/test_real_adapter_wiring.py:RegistryRouterAckIsInvisibleToTheBFFTests`, which
asserts the ops row has no `router_sync`, `pending_router_sync` or `router_ack_evidence`.
If someone later wants that block in the browser DTO, that test is where the question is
asked.

The Registry seam itself is unchanged in shape: `OpsApplication(service=RegistryService)`,
`requester` is `backend.registry.identity.RequesterContext`, and the BFF reads only
`visible_*`. `tests/registry/test_bff_seam.py` (PR #89) is the other half of that claim.

## 10. Integration order (for the Integrator)

1. **#85 → this PR** (BFF wiring) — must land before the report-download route exists.
2. **#88, #90, #91** in any order — this branch tolerates all three being absent
   (`503` + stable reason) and prefers all three being present.
3. **#89** — independent of the BFF; no ordering constraint.
4. After merge: the capability manifest should move `host_summary` from
   `not_configured` to `stale/no_sample_yet`; it becomes `ready` only once a deployment
   actually injects `enabled=True` and Prometheus answers.

## 11. V1-11 operations surface — what exists, what was added (2026-10-18)

V1-11 asks for *fixed-scope operator services/logs/audit and Grafana access, bounded
and redacted; no arbitrary raw shell/PromQL/path proxy*. Three of the four pieces
already existed; the audit timeline was the one with no implementation at all.

| V1-11 piece | Where it lives | Status |
| --- | --- | --- |
| platform **services** | `GET /api/portal/services` (PR #85) — the `services` slice of the server-validated host projection, `observability:host:read` | present |
| **Grafana access** | `external_ui` in `/api/v2/ops/usage` and `/api/v2/ops/engines` (`backend/observability/service.py`) — same-origin proxy path, `external_url` always `null`, `iframe_allowed: false`, scope-conditioned `visible` | **present — do not rebuild** |
| **logs / audit** | `GET /api/portal/events` (**this round**) — operator-scoped, bounded, redacted over two real structured ledgers | **added** |
| no arbitrary shell/PromQL/path proxy | closed-name guard `backend/bff/identity.FORBIDDEN_PROXY_QUERY_KEYS` on every data route | present |

### 11.1 Grafana is already guarded — and deliberately not a proxy

`external_ui` is not a new mechanism and this lane did **not** add a second one. It
returns, per operator session:

```json
{"id": "grafana", "label": "Grafana", "path": "/observability/ui/grafana/",
 "requires": "observability:host:read", "visible": true, "external_url": null}
```

`external_url` is **always** `null`: the platform never hands the browser an upstream
address, because a shareable URL would turn "who may see which dashboard" into a link
anyone can copy. The path is same-origin and still passes through Portal session
authorization. Grafana's own anonymous/org permissions remain a Grafana-side setting;
this module only owns the entry decision.

**Consequence for the Integrator:** there is no missing Grafana route to build. The
remaining V1-11 *host* evidence is the real operator-scoped request against a running
Grafana — `HOST_PENDING`, not code.

### 11.2 `/api/portal/events` — the two ledgers it serves

| source | backend | authorization decided by |
| --- | --- | --- |
| `elevation` | `backend/bff/admin_mode.py` `admin_audit` (enter/exit/failure) | the route's `portal:admin:read` gate |
| `registry` | `RegistryService.audit_log(requester, limit)` over `audit_log` | **Registry itself** (cross-subject needs admin read; a plain principal sees only its own rows) — the BFF does not re-implement it |

Both are read-only, bounded (`limit` 1..100) and newest-first. Source states reuse the
warehouse vocabulary `ok / empty / unconfigured / unreachable`; `unconfigured` (never
wired) and `empty` (wired, genuinely zero rows) are deliberately distinct. There is no
`stale`: an idle deployment legitimately has an old newest-entry, so the route reports
`observed_at` and `newest_at` as facts instead of inventing a freshness verdict.

**Redaction is whitelist-by-construction.** Only the declared field tuples are read out
of each row; both ledgers' `detail` columns are never copied into the response at all.

### 11.3 Not delivered, and why

- `/api/portal/metrics/history` — a bounded time-series endpoint needs a proven,
  bounded sample source. The host projection exposes instantaneous values only;
  inventing a history store here would be fabricating the very data V1-11 protects.
- A **self-scoped** audit view for members — the operator scope exists precisely
  because cross-subject audit is its only purpose. Adding a "your own rows" mode is a
  separate authorization decision, not a convenience.

### 11.4 What is still `HOST_PENDING`

Every item above is CPU-only and source-level. No Dell host was contacted: the real
Grafana entry, the real operator-scoped request, and the real Registry audit volume
remain unverified on the target host.

## 12. SEAM-1 — the same-origin invoke route (2026-10-18)

Issue [#68](https://github.com/ProfessorZhi/llm-lab-portal/issues/68)'s offline vertical
slice found that the browser had **no** same-origin way to call an authorized Endpoint:
`/api/v2/registry/*` is deliberately pinned at 404, so V1-07 could only be exercised
against the Registry service layer. This section records the closure.

### 12.1 What was added

`POST /api/v2/ops/endpoints/{endpoint_id}/invoke` — a **thin bridge**, not a second
invocation implementation. Everything authoritative stays in
`RegistryService.invoke`:

| Concern | Decided by |
| --- | --- |
| who is calling | `OpsApplication.resolve_credential` — session cookie **or** scoped token; no request field can name a principal |
| CSRF / same-origin | the existing `auth._preflight`, reused |
| invoke scope | Registry's `policy.require_invoke_scope()` (`registry:endpoint:invoke`) |
| endpoint visibility | Registry's `require_endpoint_read` — invisible and missing are **the same 404** |
| endpoint readiness | Registry's `require_endpoint_ready` (`status == ready`); unverified ⇒ 409 |
| ownership / protocol axes | Registry's `_gate_endpoint_backend`, per deployment policy |
| payload bound | JSON object; effective bound is `min(AuthConfig.max_request_bytes, 64 KiB)` — the BFF's whole-request cap is the smaller one today |
| metering-before-forward | Registry audits **before** calling the adapter, so an upstream outage cannot lose the count |
| Gateway-side ACL | the adapter (and, for a real Gateway, the Gateway's own key record) |

The BFF adds exactly one thing of its own: **response narrowing**. The adapter's `result`
contains `body` (the model's response) and, for the test double, `echo` (the request
payload). Forwarding those would turn the new route into an arbitrary content channel.
`backend/bff/invoke.py` therefore emits a **metadata whitelist** — `request_id`,
`synthetic`, `gateway`, `forwarded`, `served_by`, `http_status`, `model`, integer-only
`usage`, `tokens`, `client_latency_ms` — and never `body` / `echo`.

**This is a product decision the owner can flip**, not a security invariant. Showing model
text is a separate review (retention, redaction, who may read it). It lives in one tuple
so that flipping it is a visible edit, not a buried comment.

### 12.2 Router ACK — now enforced by Registry, reported by the BFF

V1's source mapping gives the Gateway Router the authority of *"verified invoke, **applied
routing ACK**, endpoint readiness"*. At the time of #115 `RegistryService.invoke` read only
`status == ready`, so this document recorded that as an open gap and the BFF **reported**
the router state instead of inventing a gate.

**The gate now exists in the authority**: Registry P0 ([PR #122](https://github.com/ProfessorZhi/llm-lab-portal/pull/122))
refuses `pending_router_sync`, `router_sync_attested`, missing `gateway_readback` evidence,
a Router that currently reports a different revision, and an endpoint absent from the
publishable snapshot — all **before** metering and before any Gateway call, with
`409 registry.not_verified`.

The BFF's role is unchanged and deliberate: it **does not duplicate** that gate. It
continues to surface `router_sync: {state, pending, applied, applied_generation,
current_generation, evidence, reason}` as a Registry fact, and lets the authority refuse.

The old `@unittest.expectedFailure` has been **promoted to a real assertion** —
`test_a_pending_route_is_never_reported_as_applied_and_never_forwarded` — with a second
case for `attested`:

- gate present (this tree has #122): **409** and `gateway.calls == 0` for both `pending`
  and `attested` — a genuine security-positive assertion;
- gate absent (this branch alone): the route must not misreport — `state` stays
  `pending_router_sync` / `router_sync_attested` and `applied` stays `false`.

Two branches, both real assertions, **no `expectedFailure` left**. A capability probe
(`_require_route_applied`) selects the branch and is itself pinned
(`test_the_gate_probe_matches_the_authority_it_probes`) so it cannot silently become a
constant.

**Why the BFF still must not add its own gate:** `attested` may describe a perfectly
working deployment; deciding whether invoke accepts it is a product decision, and the
enforcement point is `RegistryService.invoke` (Registry [#76]). Duplicating it here would
be a second rulebook that can drift from the one that actually guards the Gateway.

**Fixture consequence (BFF-owned):** every BFF test that invokes a **ready** endpoint now
first calls `Harness.acknowledge_route(...)`, which writes the same row a successful
Gateway read-back writes and installs a Router double reporting the same revision. Two
pre-existing fixtures (`test_cross_module_e2e.py::test_sharing_to_a_uid_gives_b_visibility_and_invocation`,
`test_two_user_acl.py::test_b_can_then_invoke_it_and_the_mock_gateway_answers`) and the
invoke suites were upgraded this way — **none were deleted, skipped or marked expected to
fail**. Tests that *want* an unconfirmed route (the pending/attested cases) deliberately
do not call it.

### 12.3 Tests — `tests/bff/test_v1_invoke_route.py` (24, 1 expected failure)

anonymous 401 (with CSRF) and 403 (without — the other gate, same convention as `share`) ·
forged `X-UID` 400 · `?uid`/`?owner`/`?key`/`?url`/`?path`/`?promql`/`?source` 400 ·
missing CSRF 403 · cross-origin 403 · invisible vs missing share one shape (only the
caller's own id is echoed) · grantee can invoke, **revocation denies the very next call
before the Gateway** · oversized body 413 before Registry · non-object body 400 ·
unverified endpoint 409 `registry.not_verified` · detached Gateway 503
`registry.upstream_unavailable` · `request_id` surfaced · body/echo withheld · no
credential or upstream address in the body.

Every refusal is paired with a **gateway call count** assertion — a 4xx that already
forwarded the call is invisible to a status-code-only test.

### 12.4 Handoff

- **Integrator:** `#112` → this PR. One new route (`ops.invoke`); the client mirror is
  route #20 for UI #66 if the UI wants to call it — otherwise the route is simply
  uncalled. `tests/bff/test_route_map.py`'s 404 pin for `/api/v2/registry/*` is untouched.
- **Registry [#76]:** if the Router-ACK gap is to close, the gate belongs in
  `RegistryService.invoke`; the BFF already forwards the state it would need.
- **Catalogue:** the invoke row **is now registered** — `POST`,
  `implementation_status: bff_declared`, `required_scope: registry:endpoint:invoke`,
  `required_auth: session-csrf`, `host_runtime_verification: not_run`. The earlier note
  that it was absent is superseded. `bff_declared` means *declared in `route_map.py`,
  may be unconfigured* — **not** a LIVE claim.
  `tests/bff/test_v1_invoke_catalogue.py` pins the row to the `RouteSpec` field by field
  and, behaviourally, pins that the route still answers **503** while carrying that label.
- **`HOST_PENDING`:** no real Gateway, no real key directory, no Dell. `request_id`
  provenance is asserted against a trusted-shaped adapter double, not a live Gateway.

### 12.5 D4 — the credential channel decides whether CSRF applies

Issue #68 D4, found by the test-only vertical slice ([PR #121](https://github.com/ProfessorZhi/llm-lab-portal/pull/121)):
a request carrying **only** `Authorization: Bearer …` and **no** session cookie was still
sent through the session write-preflight, so it failed with `403 csrf_rejected`. A
legitimate CLI could not call the route it was authorized for.

CSRF protects an **ambient** credential: a browser attaches the session cookie to
cross-site requests by itself, so a cookie-authenticated write must prove same-origin.
`Authorization: Bearer` is not ambient — no browser attaches it on a page's behalf, so
requiring CSRF for it buys nothing and breaks the caller. `invoke` now branches on the
channel (`OpsApplication._invoke_preflight`):

| Credential presented | Behaviour |
| --- | --- |
| `Authorization` only | token channel owns identity; **no session CSRF / Origin required**; body size limit still 413 |
| session cookie only | unchanged: **CSRF + same-origin** required |
| **both** | **400 `credential_confusion_rejected`** — never silently pick a side |
| invalid / revoked bearer | **401**; the bearer presence grants no exemption, because identity resolution happens before anything is forwarded |
| bearer + **cross-origin `Origin`** | **403** (a CLI sends no `Origin`, so absence is fine; a browser-origin marker that fails the check is not) |

Two properties make this safe rather than a hole:

- **It is scoped to one route.** `share`, `/auth/admin/*` and `/auth/linux/login` still
  require session CSRF. Nothing global was relaxed.
- **`resolve_credential` never falls back.** A request with an `Authorization` header is
  owned by the token channel; an invalid token is 401, not "ignore it and use the cookie".
  That is what makes the exemption safe: the exemption applies only after the token is
  the thing being authenticated.

Every refusal in `tests/bff/test_v1_invoke_bearer_channel.py` is paired with a
**Gateway call-count assertion** — a 4xx that already forwarded would otherwise be
invisible.
