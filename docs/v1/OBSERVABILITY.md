# V1 observability, logs and evidence contract

## 1. Design around operator questions, not attractive chart placeholders

| Question | Telemetry and truth source | UI | Failure case |
| --- | --- | --- | --- |
| Is Dell GPU saturated? | per-device GPU util, VRAM used/total, temperature, power from Strata adapter→Prometheus | Overview / Compute | unknown if exporter stale/unreachable |
| Can an authorized model be called now? | Registry owner/share, Deployment verification, Gateway applied Router ACK, Engine actual model readiness | Models / API | healthy port alone is insufficient |
| Is traffic/error/latency changing? | individual trusted Gateway requests and key-principal mapping | My Calls / Atlas | missing gateway events ≠ zero users |
| Does a system component work? | allowlisted service probes with endpoint-specific readiness | Observability / Admin | TCP listener ≠ ready |
| Which evaluation is real? | EvalScope report provenance and owner-audited legacy Benchmark | Evaluation | orphaned report hidden |
| Who changed permissions? | structured Auth/Registry/Router audit | Administration | no raw secret/log dump |

## 2. Minimum signal families

**Device/Host (P0):** GPU utilization ratio; VRAM used and total bytes; GPU temperature °C; power W; Strata exporter state. CPU %, RAM bytes and disk capacity/IO only if verified collectors expose exact units/semantics. Network RX/TX and physical device identity after allowlist. Never infer per-user GPU allocation.

**Engine/Service (P0/P1):** Strata current ready and served-model status, Gateway health/readiness, Registry/BFF/Prometheus/Grafana/EvalScope probes. Each has declared probe method, component identity, observed_at, timeout and state `ready/degraded/down/unknown`; do not show a model ready from port-open alone.

**Gateway/Workload (P1):** request_id, model and Endpoint opaque IDs, status/error code, UTC start, total latency, queue/TTFT where captured, stream flag, input/output tokens where actually reported, Key→Principal/Project validated server-side. Bounded time windows, request counts, actual p50/p95 computed from authorized raw samples, coverage and error rate. No monetary costing without trusted rate table.

**Reports/Evaluation (P1):** real run source, Owner, version, dataset, metric name/units, sample count, time. EvalScope and Legacy are separate lineages; GPU execution itself is explicitly gated.

**Events/Audit (P1):** logins/session failures (safe codes), Endpoint ACL/share/revoke/Router revision state, Gateway error outcomes and sanitized fixed service fault events.

## 3. Metric quality semantics

Each display must indicate original producer or measured metric ID, unit, `sample_at`, `observed_at`, age and source state. In HostSummary, reject values older than default 90 s or >15 s future tolerance (unless an explicitly versioned producer policy is adopted). Missing samples remain NULL, measured 0 remains 0. Source error, partial, stale, no-config, denied and actual-empty are distinct. Each trend uses actual stored history and reports gaps; don't interpolate fake values. A successful HTTP 200 is not sufficient attestation.

Grafana may present deep analysis but remains access controlled; the typed BFF still owns API data for cards/permissions. Never expose raw PromQL or direct loopback Prometheus access to browsers.

## 4. Controlled local logging

- Prefer a **structured event API**, e.g. proposed `GET /api/portal/events`, fixed component allowlist and exact scopes.
- Allowed fields: server timestamp, component, event code, severity, request_id, opaque resource ID, actor Principal within authorized view, outcome and bounded redacted message.
- Event paging, bounded time interval, low-privilege allowlisted collectors, retention and audit of administrative reads.
- Refuse arbitrary journald unit, path, shell command, Host override, upstream URL, raw stack traces containing keys, complete prompts/responses and private filesystem references. Producer-side AND BFF-side redaction.
- Avoid long-running log/metrics collection on the same GPU process; default passive read-only probes, bounded concurrency and query timeouts.

## 5. Real host verification worksheet (no sample values)

For each source: `device_ref`, `host_id`, `service_owner`, `collector_version`, `endpoint_allowlist`, `metric names`, `units`, `sample_at`, `observed_at`, `probe evidence time`, `authorized principal/scope`, `quality`, `attached BFF route SHA`, `health result`. Store redacted evidence in a restricted artifact or Issue, never raw logs/keys.

Release health metrics must cover BFF availability, session isolation, Gateway invocation correctness, GPU noninterference, data staleness and rollback readiness; threshold alarms only after a measured baseline and operator-defined limits. Do not invent SLO percentages.

> **Execution spec:** exact read-only commands, the allowed loopback endpoint set and the
> no-secret field allowlist are pinned in
> [`HOST_READONLY_EVIDENCE_CAPTURE.md`](HOST_READONLY_EVIDENCE_CAPTURE.md). Until #83 is
> confirmed fixed, every run is `BLOCKED_ACCESS` and this worksheet stays `HOST_PENDING`.
