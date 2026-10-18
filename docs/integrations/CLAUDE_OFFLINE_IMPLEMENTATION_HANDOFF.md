# Claude Code offline-first handoff · Full frontend WITHOUT invented lab data

**Mission:** build the full V6.2 Apple management UX in parallel with backends even when the Claude Code runner **cannot access Dell/A6000**. No fictitious Engine/Model/Endpoint, Key, fake Linux account, fake prompt, made-up benchmark, synthetic usage series, chart dots or human-readable demo performance numbers in the authoritative UI.

**Use these files in order:**
[product data needs](PRODUCT_DATA_REQUIREMENTS.md) · [source inventory](SOURCE_CAPABILITY_INVENTORY.md) · [BFF API state](PORTAL_BFF_API_CONTRACT.md) · [identity/admin ADR](ADR_LINUX_IDENTITY_AND_ADMIN.md) · [machine register](portal-capabilities.v1.json). Then read source modules in `backend/{bff,registry,atlas,auth_linux,evaluation,observability}/**` and existing frontend validators.

## 1. Develop by contract, not by an imaginary running cluster

### Exactly what a frontend developer can complete offline

- Real nine-page navigation, table layout, filters/column controls, relative-time handling, detail panels, keyboard navigation, dialogs, pagination and exporter serialization **against declared schemas**.
- Auth UX: signed-out, login-unavailable, invalid password, CSRF/Session/expired, current member identity **as returned from authenticated endpoint**, admin-elevation configured/unconfigured/expired/denied states; no hard-coded admin account.
- Correct Model/Engine/Deployment/Endpoint relational UI, exact ownership labels, pending Router ACK vs ready, ACL-sharing submit/cancel/warnings. Writes disabled if route/capability is not verified.
- Observability gauge/chart components with source/metric ID/unit/sample_at/observed_at/quality, NULL vs real zero, stale/future/rejected samples, multi-GPU card structure; axes and empty/unknown chart affordances **without sample points**.
- Calls/Atlas shape validators, owner-scoped query parameters, cached pagination controls, request metadata columns, missing-token and missing-latency logic, 401/403/429/503/error handling, exports of **actual authorized** rows only; charts correctly say "no usable data" when absent.
- Evaluation two-source catalogue UI, owner-scoped reports, missing dimensions, bounded downloads/error states, disabled GPU-runner submit.
- Operator diagnostics with bounded, redacted structured event fields and capability matrix, even if no log adapter exists yet.

### What CANNOT be claimed from offline coding

- Actual A6000 temperature/VRAM/utilization, Strata model loaded, Gateway route available or a user's inference latency.
- Real PAM accepting the Linux password, real admin step-up installed, or a per-UID data separation proof on actual OS.
- Actual shared endpoint, real key issuance, live traffic trend, real EvalScope report, historical logs or Grafana read permissions.
- "Production-ready" just because schema tests pass. The last gate requires live source + real users + rollback evidence.

## 2. Build a contract-state harness, not a business-data Mock server

The harness may return **only control-state outcomes** without fabricating business values:

| Harness response | What it tests | Business records allowed? |
| --- | --- | --- |
| Network pending / timeout / aborted | spinner, cancellation, retry, stale in-flight result | **No** |
| `401 unauthorized` | signed-out identity screen, clear previous private cache | **No** |
| `403 forbidden` | clear private data, deny export/action, source-based reason | **No** |
| `404 route missing` | feature "not connected"/"missing adapter" | **No** |
| `404 object hidden` | cannot infer another Owner's existence | **No** |
| `429 rate_limited` | bounded retry and safe reason | **No** |
| `503 pam_unavailable` | login setup state | **No** |
| `503 registry/observability_adapter_not_attached` | missing upstream wiring | **No** |
| `200 validated producer envelope where every optional metric is null/unconfigured` | units/copy/quality/freshness layout (do not claim measured zero) | **No** |
| Malformed schema / unexpected source/version | fail-closed validation, error and audit | **No** |

A successful **real** `200` with loaded models or nonempty calls requires a trusted producer or an explicitly approved **redacted** historical production capture, held in a restricted **non-repo** location. This task does not approve capturing or publishing that data. Do not create sample users, invented models, fake Key IDs, numeric series or "friendly" data to fill the space.

### Safe shape-only response principle

For each actual DTO family, use its real module schema and an empty/missing state only when valid for that producer. Example conceptual frontend state, **not a claiming-to-be-live HTTP response**:

```json
{
  "view_state": "unconfigured",
  "reason": "adapter_not_attached",
  "source_id": null,
  "sample_at": null,
  "observed_at": null,
  "value": null,
  "items": null
}
```

This is a **frontend view model** produced from an error/absence; do not assume existing BFF already returns it, and do not send `200` + `items: []` claiming that a real query proved emptiness.

For the host summary, use the existing `prometheus-readonly` `version:1` producer shape with all metric `state:"unknown", value:null` and correctly declared units/reasons; a harness response must carry **test-only** provenance at its test boundary. Its `synthetic:false` field cannot turn a fake measurement into a valid one: the client must also verify source, version, sample time, unit and trusted delivery path.

## 3. Full lifecycle to implement with capability guards

```text
Unconfigured
  -> Authenticated member + authorized Registry list
  -> Own or shared Engine/Model/Deployment/Endpoint
  -> Verified deployment + Router ACK state
  -> Usable endpoint + browser-approved scoped Token
  -> User invokes real Gateway (only if server capability says yes)
  -> Real request metadata -> My Calls + authorized Atlas
  -> Owner revokes -> next invoke denies
  -> Audit confirms actor/principal/outcome
```

All 9 pages should be visually complete **without** fabricated rows. Detail layouts may show headings/placeholder skeletons, but must not show invented `Qwen-Example` instances or any fake utilization/call totals. "Connect real datasource" is a legitimate empty-state action only for an operator with that capability; a member sees a non-actionable explanation, not a fake Setup button.

## 4. Source selection and production build

- Default ordinary URL: source-backed or **UNCONFIGURED**. No implicit `demo` state anywhere (Atlas, Models/ops, Calls, Eval, Overview).
- Current [V6.2 integration PR #71](https://github.com/ProfessorZhi/llm-lab-portal/pull/71) removes implicit Atlas and Ops samples and legacy `?source=mock` bypass; preserve this safety.
- Existing `?demo=1` is a legacy **separate QA mode**. Product owner explicitly wants a real management console, so a production distribution should **disable/offline-gate** the demo control rather than promote synthetic test fixtures. This requires its own regression and review; don't just hide the visible DEMO label and leave mock data active.
- Only a **reviewed, configured same-origin path** is a browser data source; page query args must never specify arbitrary upstream URLs or PromQL. Backend must still authorize every request — a correct URL is not proof of permission.
- Use the authentic Apple V6.2 design in `prototypes/v6/additions.*`, preserve V5's immutable hash. `web/index.html` and `web/css/*.css` are generated artifacts. JS and TS state controllers may be edited in their original source directories, then derive/extract.
- No credentials, actual user names/UID maps, real host IP addresses, snippets with bearer secrets, actual report bodies, complete logs or raw Prometheus series-labels in screenshots.

## 5. Route readiness is data too

Before enabling a control, read server-side capability state. Until a dedicated `GET /api/portal/capabilities` is implemented, derive only from the available *trusted* status endpoints and explicit configured adapter policy, never from silently probing or inventing route availability. A 404 could mean hidden resource rather than missing route: feature-level route readiness must come from **operator-controlled capability declarations**, not an arbitrary error reinterpretation.

| Feature | Minimum server evidence before enable |
| --- | --- |
| Host live card | `/api/infra/summary` trusted version/source, fresh samples, known units |
| Registry list/detail | Linux Session + BFF-attached Registry + correct Owner ACL |
| Share/revoke | Owner-scoped write route, CSRF, real Registry, next-request deny tested |
| Chat/Playground | callable Gateway route + verified owner/share ACL + scoped Token; streaming/stop handling |
| Token issue/revoke | functioning Session/CSRF/Token Authority, real one-time secret behavior |
| My Calls | real Gateway Key Directory with current UID mapping and `atlas-calls/1` |
| Atlas | real authorized aggregate derived from request events, not individual GPU gauges |
| Eval report viewer | real source adapter + audited per-report Owner, proper file references |
| Evaluation submit | explicit real GPU admission, safe queue/scheduler and operator approval |
| Admin enter | real per-user eligibility + step-up configured, TTL and audit validated |
| Logs | fixed allowlisted service/event API, redaction and bounded window |

## 6. Independent test gates and actual handoff

1. **Contract QA**: compare route register to real module route tables; no promotion from module-only to BFF-wired merely because a function exists.
2. **State QA**: every one of nine pages handles signed-out, denied, route not configured, stale, malformed, partial and empty; no synthetic in ordinary browser location.
3. **Security QA**: header/body/query UID/Role/Source spoofing cannot change returned or rendered scope; another tenant's response must never flash while switching accounts; session expiry clears data; 404 concealment.
4. **Data QA**: explicit units, source freshness and quality; values cannot be inferred from missing metrics; p95 from raw events and normal data aggregation cannot sample another user's events.
5. **UI QA**: real Chrome images at 320/390/768/1024/1440, both themes, all nine routes, keyboard and responsive overflow; indicate **NOT VERIFIED** if browser isn't available.
6. **Source checks**: run `npm test`, Python focused contract tests, derive/extract and V5 hash.
7. **GitHub**: isolated worktree/branch, real file and test changes pushed, Draft PR with remote SHA, no fake success or "deployed" wording.
8. **Dell final gate**: independent operator-approved real PAM/Gateway sources, A→B share/invoke/meter/revoke negative tests, actual host age/value proof, 8443 version/rollback checks. **Only then consider production replacement.**

**Implementation instruction:** code the entire frontend against these contracts. A no-A6000 local environment should show truthful "not connected" surfaces; the UI must never depend on invented operational readings to look complete.
