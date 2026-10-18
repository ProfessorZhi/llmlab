# V6.2 source/capability inventory · 可验证的数据来源与 API 能力

> **As-of 2026-10-17 repository review.** Runtime services have not been queried in this documentation task. Paths prefixed `upstream` are **server-only**; the browser does not contact those ports. Neither "code exists" nor "port was listening in an earlier audit" means the service is configured in the new production BFF.

## Status vocabulary (do not conflate)

| Status | Meaning |
| --- | --- |
| **REPO** | Exact endpoint/metric or adapter exists in source, not a claim of production attachment |
| **HOST-OBSERVED-PAST** | Seen during a dated Dell read-only inspection; must recheck live host |
| **PREVIEW-ONLY** | Route runs only in sidecar/preview with config/guard; not in the Python production BFF |
| **MODULE-ONLY** | WSGI or pure Python service supports the route/DTO, but V6 same-origin BFF does not expose it |
| **BFF-DECLARED** | Declared by `backend/bff/route_map.py`, may return 401/403/503 and need adapters |
| **PLANNED** | New Portal API requirement; no corresponding production route established |
| **UNVERIFIED** | Identity, upstream or data volume/status not established on target host |

## Providers, upstream interfaces, identity and purpose

| Source/system | Exact upstream or code-provided interface | Authority for | Repo evidence / status | Browser contract |
| --- | --- | --- | --- | --- |
| Dell/Strata metrics adapter | `127.0.0.1:9101/metrics` | Strata-reported node+GPU **metrics exposition**, no user attribution | REPO `ProfessorZhi/llm-lab:deploy/dell/adapter/adapter.py`; port HOST-OBSERVED-PAST | through approved BFF host/metrics DTO only |
| Prometheus | `127.0.0.1:9090/api/v1/query` with **fixed, code-owned expressions**, `/-/healthy` | Numeric facts, last sample and history | REPO `web/tools/verified-infra.mjs` and `backend/observability/sources.py`; port HOST-OBSERVED-PAST | BFF; no arbitrary PromQL/query forwarded |
| Grafana | `127.0.0.1:3000/api/health` and authenticated dashboards (paths from explicit deployment config) | **Visualization** over Prometheus, not owner/session authority | REPO legacy `deploy/dell/grafana/**`; port HOST-OBSERVED-PAST | controlled same-origin Grafana proxy/link with own access model |
| Strata | `127.0.0.1:8080/health` | Transport/process/service response, **not** GPU/model loaded proof | REPO `ProfessorZhi/llm-lab:integrations/strata.py`; port HOST-OBSERVED-PAST | server-side only |
| Strata | `/v1/models`, `/v1/status`, `/metrics` (possibly bearer key) | Actual served model, loaded state, hardware/status, engine rolling telemetry | REPO legacy integration; endpoint currently UNVERIFIED | verified read adapter, secret held server side |
| Strata | `/v1/chat/completions` (POST, authenticated where configured) | Actual inference, **only on explicit authorized user action** | REPO upstream integration; not V6 secure call path | via Gateway/BFF authorized invoke, **no page-load probe** |
| Gateway | `127.0.0.1:8200/healthz` (fixed health probe); documented server usage metadata `usage.jsonl` | Routing health and key-attributed request metadata, Token/latency/TTFT when captured | REPO `web/tools/verified-infra.mjs`, `backend/atlas/**`, `backend/observability/gateway.py`; upstream port HOST-OBSERVED-PAST | V6 BFF, per-session and per-Key identity |
| Registry | Python WSGI `/api/v2/registry/{healthz,whoami,status,engines,models,deployments,endpoints,...}` | Owner and share ACL, Engine/Model/Deployment/Endpoint, verify, revision, Router ACK | MODULE-ONLY `backend/registry/api.py`; not automatically V6 BFF-wired | BFF scoped lists/details/actions; never client-derived owner |
| V6 Python BFF | `GET /api/v2/ops`, `/usage`, `/engines`; `PUT /api/v2/ops/deployments/{id}/share`; and since 2026-10-17 `GET /api/portal/capabilities`, `/api/portal/services`, `/api/infra/summary`, `/api/atlas/calls`, `/api/evaluation/{report-sources,reports,reports/{ref}}` | Authenticated aggregate of Registry, observability, Calls/Atlas and evaluation, plus a per-session source-readiness manifest | BFF-DECLARED `backend/bff/route_map.py`; every newly mounted route answers **503** on a deployment that has not injected its source | allow only when current Session + adapter valid; `MODULE-ONLY` did not become `LIVE` by being mounted |
| Host summary preview | `GET /api/infra/summary` with `ATELIER_INFRA_READONLY=1` (Node sidecar) **and** the Python BFF route since 2026-10-17 | Code-validated six host metric types + service probes | PREVIEW-ONLY in `web/tools/serve.mjs`; **BFF-DECLARED** in `backend/bff/host_summary.py`, validated server-side by `backend/observability/host_projection.py` before the payload reaches the browser | both need an injected reader; the BFF route answers 503 until one exists. Projection on Dell still UNVERIFIED |
| Atlas/Calls/Modules | `GET /api/atlas/calls` now **mounted**; `/api/atlas/usage` and `/api/atlas/modules` remain unwired (404 on the same-origin BFF) | Respectively authorized project aggregation, own request metadata and module availability | `calls`: BFF-DECLARED `backend/bff/atlas_adapter.py` (503 `atlas_adapter_not_attached` when unwired); `usage`/`modules`: still contract-only | rows pinned by `tests/bff/test_route_map.py`; no real Gateway record fallback, and the synthetic corpus is reachable only from tests |
| EvalScope | native UI `127.0.0.1:9000`; upstream reports adapter uses fixed `/api/v1/reports` | Evaluation reports and status from its own run catalogue | REPO `backend/evaluation/report_readers.py`, legacy configuration; route availability UNVERIFIED | evaluation BFF per-report owner/scope filter |
| Evaluation service | `/api/evaluation/health`, `/openapi.json`, `/datasets`, `/admission`, `/report-sources`, `/reports`, `/jobs`, etc. | Source-disjoint EvalScope vs legacy Benchmark catalogue; constrained jobs | MODULE-ONLY `backend/evaluation/api.py`; **report read routes BFF-DECLARED since 2026-10-17** (503 when the adapter is unattached); real GPU runner not accepted | BFF scoped read mounted; real job submission still disabled |
| Legacy LLM Lab Portal | loopback `:8765`, `ProfessorZhi/llm-lab:portal/server.py` (status/report/benchmark/Integrations) | Legacy operational facts and historical artifacts | REPO + port HOST-OBSERVED-PAST | **never direct proxy** anonymous report/file APIs; owner migration needed |
| Linux account service | NSS name/UID plus restricted PAM broker through owner-only Unix socket | True identity anchored by `(host_id,uid)`, not arbitrary browser fields | REPO `backend/auth_linux/**`; currently only `UnavailablePamBackend`, broker not installed | BFF `/auth/linux/login` (503 until real PAM installed) |
| Portal sessions/tokens | `/auth/me`, `/auth/csrf`, `/auth/tokens`, `/auth/logout`; admin mode `/auth/admin/*` | Auth cookies, CSRF, scoped API tokens and temporary elevation | REPO `backend/auth/routes.py` and `backend/bff/admin_mode.py`; no production IAM attestation | same-origin HTTPS; token secrets displayed once, never exposed via telemetry |
| OS process/systemd/journal | kernel proc/process facts / bounded read-only journal under restricted agent | Host/service diagnostics and operational audit **if approved** | **PLANNED**, not part of public V6 API | aggregated, whitelisted service status/log search with redaction and scopes, NOT raw shell |

### Earlier observed local ports (NOT LIVE proof)

Earlier Dell read-only session observed Caddy sidecar `127.0.0.1:18443`, V6 static preview Node `127.0.0.1:18765`, Tailnet HTTPS `:8443`, legacy Portal `:8765`, Prometheus `:9090`, metrics adapter `:9101`, Grafana `:3000`, Gateway-related `:8200`, Strata-related `:8080`, EvalScope-related `:9000`. Current role/owner/TLS/auth/health must be checked before publishing an access link or indicating the new release is live.

## Detailed metric mapping: verified source names vs candidates

| Metric / frontend question | Upstream metric/query | Current trust status | Unit/display | Ownership caution |
| --- | --- | --- | --- | --- |
| GPU utilization | `llmlab_strata_gpu_utilization_ratio` | REPO verified in `verified-infra.mjs` | ratio 0..1 → percentage | Whole GPU; not user's quota |
| GPU VRAM used/total | `llmlab_strata_gpu_memory_used_bytes` / `_total_bytes` | REPO verified in `verified-infra.mjs` | upstream bytes → BFF MiB/GiB with declared unit | Multi-GPU max aggregation is not total VRAM sum |
| GPU temperature | `llmlab_strata_gpu_temperature_celsius` | REPO verified | Celsius | Only when fresh valid sample |
| GPU power | `llmlab_strata_gpu_power_watts` | REPO verified | watts | Distinguish draw vs limit |
| Strata metrics reachable | `llmlab_strata_up` | REPO verified | bool | Metric exporter health not model loaded proof |
| Host CPU | `llmlab_strata_cpu_utilization_ratio` | LEGACY ADAPTER DEFINED; V6 live mapping NOT VERIFIED | ratio → % | Strata-reported view, not necessarily node_exporter |
| Host RAM used/total | `llmlab_strata_ram_used_bytes` / `_total_bytes` | LEGACY ADAPTER DEFINED; V6 mapping NOT VERIFIED | bytes | No owner attribution |
| Disk I/O | `llmlab_strata_disk_read_bytes` / `_write_bytes` | LEGACY ADAPTER DEFINED; exact derivative semantics require verification | bytes or bytes/s **after source confirmed** | Do not label as filesystem used/free |
| GPU power limit, PCIe throughput | legacy adapter `gpu_power_limit_watts`, `gpu_pcie_rx_bytes`, `gpu_pcie_tx_bytes` | SOURCE CANDIDATE, not V6 verified | source-specific | Advanced diagnostic, P2 |
| Host CPU/load/memory/disk `node.*` | `backend/observability/sources.py` has fixed queries referencing `node_cpu_seconds_total`, node exporter metrics | MODULE CODE ONLY; Prometheus source metrics on Dell UNKNOWN | units per catalog | Do not assume node_exporter installed |
| Request total/error/429 | **Gateway trusted metadata** → authoritative Key Directory | NOT WIRED PRODUCTION | count / time window | Not derivable from GPU utilization |
| Input/output tokens | Gateway `usage.input_tokens/output_tokens` if emitted and attributed | NOT WIRED PRODUCTION | tokens (nullable) | Absent usage is unknown, not zero |
| p50/p95 total/queue/TTFT | Gateway per-request `total_ms/queue_ms/ttft_ms` | NOT WIRED PRODUCTION | milliseconds with sample counts | Percentiles computed from eligible raw events, not avg(p95) |
| Eval scores/durations | EvalScope sourced reports or audited legacy run | UNWIRED; owner verification required | dimension-specific measured unit | No fake benchmark leaderboard |
| Loaded model, context length | Strata `/v1/models` + `/v1/status` | REPO integration, live identity/link unverified | model info and loaded boolean | Different from Registry's model name/Owner |
| Active endpoints/share ACL | Registry + effective Router ACK | MODULE CODE ONLY; production not attached | states and revisions | Registered, verified and routed are distinct |
| Service health, process state | fixed target HTTP health/status + authority state | mixed REPO and PLANNED | up/down/unknown, age, authority | Listening ≠ ready; do not infer healthy from port open alone |
| System logs | bounded explicitly allowlisted journald/export event source | PLANNED | timestamp/severity/component/event code | Redact secrets/raw prompt and rate limit |

### Data quality rules

- **Known zero**: only after a true observation of zero from the authorized source.
- **Empty**: only after a real authenticated query proved no visible rows; cannot decide globally empty from per-user empty.
- **Unconfigured/unknown/stale/denied**: values remain `null`, with stable reason and safe actions.
- **Timestamps**: UTC ISO8601 with offset; for real GPU host summary existing gate rejects samples over **90 seconds** old or over **15 seconds** in the future. Other data types need their own appropriate, declared thresholds (EvalScope report source 3600s in `evaluation-report-sources-contract.md`); do not indiscriminately reuse GPU interval.
- **Names/identifiers**: users never supply source/UID/owner or upstream base URL in public API. Actual owner and project are from server-controlled Registry/Token/Session mapping.
- **Grafana**: a dashboard health=200 does **not** mean it has live panels or a valid data source; BFF only advertises an operator-approved route.
- **Logs**: separate security/session audit, Gateway request metadata, engine health/diagnostics and user-owned evaluation records. No default unrestricted raw journal viewer.

## Evidence required before promoting to production

1. Verify each metric's **actual** Prometheus metadata HELP/type, instantaneous output, source timestamp, unit, count and reducer; don't interpolate missing metrics into healthy statuses.
2. Show actual `/v1/models` and `/v1/status` sanitized field names, prove model loaded status by real protocol validation (not header/transport).
3. Prove Gateway supports trusted Key→Principal and Endpoint mapping and the last request's call metadata (redacted).
4. Prove EvalScope and legacy report source configuration, owner audit and per-UID responses.
5. Prove PAM broker installed only after system-owner approval, Session cookie and write CSRF work in protected HTTPS.
6. Test Web↔CLI A→share B→invoke→meter→revoke→B denied, plus another unprivileged UID never sees it.
7. Confirm backup/release SHA and rollback on the actual Dell host; no synthetic data in production.
