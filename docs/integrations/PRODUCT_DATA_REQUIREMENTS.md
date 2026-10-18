# Product-driven V6.2 data requirements · 先问业务问题，再选监控指标

> Design acceptance specification, **not a synthetic dashboard dataset**. All facts, counters and rows below must be traceable to a real, authorized producer. V6.2 keeps the Apple UI, while the frozen V5 remains the source of the inherited page/Atlas geometry.
>
> Scope: one shared Dell Linux/A6000 host, separate Linux user identities, own/shared Endpoint routing, Gateway usage, evaluation and diagnostics. Goals: truthful availability, safe model reuse, self-service invocation, attributable accounting, rapid failure diagnosis, operator audit and reversible administration.

## 1. What decisions should the portal enable?

| User question | Required trusted information | Action / outcome | Explicit non-goal |
| --- | --- | --- | --- |
| Can I use a model **now**? | Permission + actual loaded model + Engine health + Deployment verify + Endpoint routing ACK | Show usable endpoint or exact reason blocked | Don't label a registered model "running" |
| Is the A6000 saturated or failing? | Per-device GPU/VRAM/temperature/power, CPU/RAM/disk, sample time, warnings | Read-only diagnostics, conservative capacity warnings | GPU util=0% is not free VRAM or user allocation |
| Who owns the service, and can I share it? | Stable Owner Principal, endpoint ACL, verified Deployment, effective routing generation | Register, verify, share, revoke, see audit | No filesystem scan or arbitrary engine kill |
| Why did my inference fail/slow down? | Own request IDs, status, queue/TTFT/total latency, known routing and health, time window | Filter, inspect metadata, retry only if permitted | No prompt/completion contents in telemetry |
| How is my/project traffic changing? | Trusted Gateway key→principal→project→Endpoint mapping and bounded history | Authorized Atlas drill-down and exports | Do not infer per-user calls from node GPU metrics |
| Can I trust this number? | Producer, metric ID, units, state, sample_at, observed_at, age, reducer, sample count, synthetic=false attestation | Show Live/Partial/Stale/Unknown/Denied distinctly | Do not paint UNKNOWN as 0 |
| Which evaluation results are mine? | Source (EvalScope/legacy), actual owner/run, metric units, report timestamp and ACL | Report catalogue/details/export | No fabricated evaluations or GPU runner without admission |
| What is broken operationally? | Fixed service readiness, bounded logs and event audit, source freshness | Diagnose cause and locate approved Grafana/metrics UI | No raw shell, raw logs or password/key output |
| Who changed my permissions? | Principal UID, audited action, target, outcome, previous/new effective ACL/revision | Read scoped audit, revoke when owner | Admin doesn't read private prompts by default |

## 2. Product modules: what to implement without the physical Dell

| Workspace | Priority | Required widgets/columns when backend is authorized | No trusted data or denied | Source authority |
| --- | --- | --- | --- | --- |
| **Overview** | P0 | Real node GPU & VRAM readings with age; host/service health; current principal's verified owned/shared Endpoints; own request summary only if Gateway metering is attached; scoped alerts and navigation | Host may be LIVE while per-user panels say NOT CONFIGURED. No fake model counts or 24h calls series | host `/api/infra/summary`; Registry; Gateway counters |
| **Compute** | P0 | Per-GPU device index/name (verified), util/VRAM/temperature/power; CPU/RAM/disk if actually scraped; short measured history + scrape gaps; Strata vs Gateway process health; units and physical source | Show individual unavailable metric labels; never reconstruct missing curve; no phantom additional GPU | fixed Prom adapter, Strata status, service probes |
| **Models & Chat** | P1 | Separate Engine / Model / Deployment / Endpoint entities; owner, visibility and grantees for owner only, verified time, protocol, routing revision/generation ACK, real errors; after auth live chat stream/cancel | Owned 0 must be based on authorized Registry query; no mock endpoints; chat disabled without Gateway invoke | Registry ACL + Router + user-authenticated Gateway |
| **API Management** | P1 | Exact route/protocol, masked own token list with scope/expiry/last use, create/rotate/revoke on verified backend, code samples generated from configured same-origin route | Don't show invented Key IDs or fake curl URL; disable unverified write | Auth/Token Authority, Registry, Gateway |
| **My Calls** | P1 | Exact-self records: time, request ID, Endpoint/model, HTTP status, queue/TTFT/latency, input/output tokens, stream, errors, keyset pagination, metadata-only CSV/JSON | 401/403 vs 404 missing route vs truly 0 results; no raw prompts | Gateway metadata + trusted Key Directory |
| **Observability** | P0 read / P1 trends | Clearly separated host/device/engine/service/user-level metric families; time range + quality/coverage and source; approved Grafana link; later warnings | Missing 1h/7d trend is UNKNOWN even if current gauge exists | Prometheus, service health, scoped observability |
| **Traffic Atlas** | P1 | Drill-down grouped by authorized project/workload/model/hour; source and completeness, request count, status/error distribution, percentiles and tokens; linked charts; scoped exports | No synthetic 9 workloads in plain URL; no heatmap zero-fill on unknown buckets | authorized aggregation over Gateway events |
| **Evaluation** | P1 reports / P2 real jobs | EvalScope and legacy Benchmark **separate source badges**, owned report list, dataset/metric/version/observed_at, quality gaps, read-only details and exports | Denied/unconfigured/unknown not "No runs"; real GPU submit disabled until explicit admission | EvalScope + historical audited report catalogue |
| **Administration** | P1 access / P2 ops | Current Linux principal, session/security, temporary admin-mode TTL, access policy/ACL audit, scoped integration status, redacted bounded error logs; operator docs | No shared admin account, root powers or raw credentials, even with role string | PAM Broker (when deployed), BFF, Registry audit, allowlisted event source |

**First deployment slice:** user can open Apple Overview/Compute and observe **truthful host GPU gauges and known service health**, plus precise "not connected" for every private Gateway/user block. This is valuable even before IAM. It is *not* yet the complete production multi-user Control Plane.

## 3. Indicator catalogue by operational family

### 3.1 Host/device/resource — read-only gauges and time series

| Data series | Value type | Preferred producer | Freshness, filter, caution |
| --- | --- | --- | --- |
| GPU utilization | physical ratio/percent, per device | verified Strata → adapter → Prometheus | 90s snapshot gate; source sample_at; multi-GPU indices require separately verified identity |
| GPU used/total VRAM | bytes (upstream), formatted GiB | same | total/used both observed, no subtraction when either null; no per-user attribution |
| GPU temperature / power / limit | °C/W | Strata-adapter Prometheus | threshold alarms only with configured limits; null for missing |
| CPU utilization/load | % / load units | verified Strata adapter or confirmed node exporter | **producer must be proved first**; provenance distinct from GPU |
| RAM total/used/available | bytes | verified host exporter or Strata metric | used/available never inferred from untrusted value |
| Filesystem capacity | bytes/% per approved mount | fixed host exporter / read-only agent | no arbitrary host filesystem paths |
| Disk read/write and IO errors | source-declared rate or cumulative counter | verified adapter/node exporter | don't label source's bytes counter "bytes/s" without rate derivation |
| Network RX/TX | measured bytes/s per whitelisted interface | fixed host exporter, **currently proposed** | never expose private peer addresses |
| Engine availability and served-model readiness | enum + sample time, separate from host gauge | Strata `/health`, `/v1/models`, `/v1/status` | /health alone cannot prove model loaded |
| Named service uptime | known up/down/unknown, last checked | fixed probe/status source | TCP listener ≠ healthy model; unknown if probe failed |

### 3.2 Identity/Registry — source of resource truth

- **Principal**: `linux:<host_id>:<uid>` (never trust username/UID/Role from browser). Separate session and token capabilities, source of the identity.
- **Engine**: Owner, protocol, approved local target, verified health and current deployment associations.
- **Model**: Actual registered metadata and safe weight reference (opaque, not downloadable arbitrary path), context/config from verified engine if available.
- **Deployment**: Model on Engine, health check/protocol validation, created/verified timestamps, runtime status and backend reason.
- **Endpoint**: Stable alias/path, Owner, effective ACL, visibility, last verified deployed revision, desired route revision, Router snapshot SHA/ACK and fail state.
- **Ownership transitions**: register, verify, share, unshare, repoint/rollback, revoke; all audited. A Registry write does **not** prove Gateway applied it.

### 3.3 Gateway requests — first class user accounting

- `request_id`, UTC start, verified `key_id`→principal/project/Endpoint mapping, model ID, status/outcome, stream bool.
- `input_tokens`/`output_tokens` nullable, total/queue/TTFT/upstream milliseconds nullable, `bytes_out` nullable; counted only if source actually captures them.
- True error rate / p50 / p95 computed with declared sample criteria; **do not average precomputed percentiles**.
- Per-user/project scopes enforced **before** aggregation; normal member cannot infer activity in another project from counts, totals, empty states or timestamps.
- Quotas/cost/rate limits only if a real authoritative policy + rate counter exists. No invented currency estimate or SLA graph.
- Calls detail contains **metadata only**. Raw prompt, response, file content and bearer key never in telemetry payload.

### 3.4 Log/event categories to expose safely

| Event group | Required fields | Permissions | Safe V1 interface |
| --- | --- | --- | --- |
| Auth/session/security | timestamp, event_code, actor Principal (scoped), outcome, request_id | self-scoped, admin limited summary | paginated audit entries |
| Endpoint/ACL/router changes | actor, affected resource (opaque ID), change type, expected/applied revisions, outcome | Owner scope; admin scoped metadata | auditable timeline |
| Gateway call failures | request_id, registered model/Endpoint, status, error_code, duration, sample time | exact self; admin diagnostics without raw user content | My Calls + bounded error aggregates |
| Engine/infra service | service/component allowlist, severity, event_code, timestamps, sanitized short message | operations/admin only | **PLANNED** bounded log view, not unrestricted journald |
| Evaluation lifecycle | job_id, state transitions, run reference, source, error code, owner | report/job owner | status timeline |

**No `GET /logs?path=/var/log/...`, open PromQL, arbitrary process command or free-text upstream URL API.** Redaction on producer + BFF, bounded results, audit reads and retention controls are part of the product requirement.

## 4. Recommended staged milestones

- **P0 – Real host dashboard:** reproduce on Dell a trusted sample, verify metric HELP/units/age with producer, wire same-origin host projection; make Overview/Compute truthful. Provide service health from fixed probes. Snapshot and source-quality regression.
- **P1A – Login and resource truth:** operator-approved PAM Broker; current Principal, Session/CSRF; real Registry lists with exact Owner/shared ACL; Router readiness separation. Web and CLI must agree.
- **P1B – Inference and metering:** production Gateway route/Key Directory per call; My Calls, Atlas, per-user utilization, real SDK samples/Playground; revoke next request denies.
- **P1C – Reports:** EvalScope and legacy catalogue with audited owners, read-only downloads; dedicated provenance state and evaluation chart policies.
- **P2 – Operator depth:** bounded logs, threshold alerts, long-term trends, quota/engine controls, GPU evaluation dispatch only with explicit admission and ops approval.
- **Final GO:** Chrome/Node/Python + live UID separation/negative tests, a backup/rollback plan and operator-authorized production routing.

## 5. What not to put in the frontend

No sample users/instance counts by default; no fixed "Qwen loaded" story; no fabricated 24-point request graph; no simulated saved Tokens or API Key names; no fake 7×24 activity heatmap; no inferred per-user GPU VRAM from whole-device reading; no public raw service logs or private historical EvalScope runs; no "admin" session with unrestricted root powers.

Missing data is a product state, **not** a license to manufacture attractive graphs.
