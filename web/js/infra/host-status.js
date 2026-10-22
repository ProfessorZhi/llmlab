/*! web/js/infra/host-status.js — the verified host read-out.
 *
 *  Where the rest of the shell is synthetic by design, this panel is the one
 *  place that may show a real Dell number. It shows them only when the server
 *  has projected them from verified Prometheus or NVML facts — and it says so, or it
 *  says UNKNOWN. There is no third state and no fallback to the demo fixture:
 *  a silent 0 would be a lie about a measurement nobody took.
 *
 *  It mounts into the existing views at runtime (the same reason the ops layer
 *  does) so the generated shell stays a generated artefact, and it reads exactly
 *  one same-origin path. It never contacts 9090/8200/9000/8080/3000 itself —
 *  asking the browser to reach a host port is the thing this design refuses.
 *
 *  ## The contract is ported, not re-invented
 *
 *  `backend/observability/host_projection.py` (Lane Data, PR #53) is the single
 *  definition of "what counts as a real reading", and
 *  `tests/observability/fixtures/host_summary/` is the shared input both
 *  consumers are measured against. This file is the browser half of that
 *  hand-off: `validateHostSummary` mirrors the server validator rule for rule
 *  (envelope whitelist, per-metric `sample_at`, future tolerance, unit binding)
 *  and `mountHostStatus` mirrors `host_refresh.py`'s refresh state machine.
 *
 *  ## Three ways this panel used to say LIVE when it should not
 *
 *  1. **No verdict meant LIVE.** `describeFreshness` returned UNKNOWN for a
 *     missing or unparsable `observed_at`, but the renderer only cleared on
 *     STALE — so a body claiming `state: "known"` with no usable clock still
 *     painted LIVE numbers and an UP/DOWN row.
 *  2. **The future was "very fresh".** `Math.max(0, now - at)` clamped a
 *     timestamp ten minutes ahead to an age of zero. A clock-skewed or forged
 *     body held LIVE until the clock caught up.
 *  3. **A hung request froze LIVE forever.** The interval only called
 *     `refresh()`, which returned the existing in-flight promise, and there was
 *     no deadline — so one pending same-origin request pinned the badge and the
 *     numbers indefinitely.
 *
 *  All three are now fail-closed: LIVE requires `validateHostSummary` to accept
 *  the body *and* find a live metric, at the moment of every paint.
 */
import { escapeHtml } from '../dom.js';

export const HOST_STATUS_PATH = '/api/infra/summary';

// --- the contract -----------------------------------------------------------
//
// Mirrored from backend/observability/host_projection.py. Kept as literals here
// rather than imported: the browser never loads Python, and a value that drifts
// must fail the shared-fixture test rather than silently agree with itself.

export const PROJECTION_SOURCE = 'prometheus-readonly';
export const PROJECTION_SOURCES = Object.freeze(new Set([PROJECTION_SOURCE, 'nvml-verified']));
export const PROJECTION_VERSION = 1;

/** A sample older than this is not a reading. Same threshold as the server and
 *  as `verified-infra.mjs`'s `MAX_SAMPLE_AGE_S`; the client does not relax it. */
export const MAX_SAMPLE_AGE_MS = 90_000;

/** How far ahead of "now" a timestamp may be before it is clock skew rather
 *  than freshness. Prometheus timestamps can lead the scrape point slightly;
 *  that lead is bounded. */
export const FUTURE_TOLERANCE_MS = 15_000;

/** Client poll period, and the deadline for one request. */
export const REFRESH_INTERVAL_MS = 30_000;
export const REQUEST_TIMEOUT_MS = 5_000;

/** The key → unit binding. A key reported with another unit is data misrouting,
 *  not a style choice: a panel printing Celsius as a percentage is worse than an
 *  empty panel. */
export const METRIC_UNITS = Object.freeze({
  gpu_utilization_ratio: 'ratio',
  gpu_temperature_celsius: 'celsius',
  gpu_power_watts: 'watts',
  strata_up: 'bool',
  gpu_memory_used_mib: 'mib',
  gpu_memory_total_mib: 'mib',
});
export const OPTIONAL_METRIC_UNITS = Object.freeze({
  gpu_memory_free_mib: 'mib',
  gpu_memory_reserved_mib: 'mib',
});

export const SERVICE_KEYS = Object.freeze(['prometheus', 'gateway', 'grafana', 'evalscope', 'strata', 'strata_model']);

/** Upstream reasons the projection may pass through. Anything else collapses to
 *  `unknown` — a free-text "reason" would be an uncontrolled rendering channel. */
export const UPSTREAM_REASONS = Object.freeze(new Set([
  'no-sample', 'stale', 'non-finite-sample', 'prometheus-error', 'timeout',
  'unreachable', 'offline', 'metric-name-unconfirmed', 'invalid_response',
]));

/** Reject codes, verbatim from the server validator so a reason read on screen
 *  can be grepped for in the Python suite. */
export const REJECT = Object.freeze({
  BAD_SHAPE: 'bad-shape',
  UNTRUSTED_SOURCE: 'untrusted-source',
  SYNTHETIC_SOURCE: 'synthetic-source',
  NOT_READONLY: 'not-readonly',
  WRONG_VERSION: 'wrong-version',
  BAD_OBSERVED_AT: 'bad-observed-at',
  FUTURE_OBSERVED_AT: 'future-observed-at',
  UNKNOWN_METRIC_KEY: 'unknown-metric-key',
  NON_FINITE_VALUE: 'non-finite-value',
  UNIT_MISMATCH: 'unit-mismatch',
  BAD_STATE_VALUE: 'bad-state-value',
  MISSING_SAMPLE_AT: 'missing-sample-at',
  BAD_SAMPLE_AT: 'bad-sample-at',
  FUTURE_SAMPLE_AT: 'future-sample-at',
  STALE_SAMPLE_AT: 'stale-sample-at',
  UNKNOWN: 'unknown',
});

/** What the panel badge may say. There is no third state. */
export const DISPLAY = Object.freeze({ LIVE: 'live', UNKNOWN: 'unknown' });

/** Why there is nothing to show. Ported from `host_refresh.py`; `expired` and
 *  `clock-rollback` are about the *client's* copy, the rest about the request. */
export const REASON = Object.freeze({
  NOT_FETCHED: 'not-yet-fetched',
  REQUEST_FAILED: 'request-failed',
  CONTRACT: 'contract-violation',
  EXPIRED: 'expired',
  CLOCK_ROLLBACK: 'clock-rollback',
});

const HUMANS = Object.freeze({
  'not-yet-fetched': '尚未取到任何数据',
  'request-failed': '请求失败（超时 / 断连 / 401 / 403 / 404）',
  'contract-violation': '响应不符合投影契约',
  'expired': '本地副本已超过样本年龄阈值',
  'clock-rollback': '本地时钟发生回拨，新鲜度判断失去依据',
  'no-source': '没有可信数据源',
  'no-sample': '上游无样本',
  'stale': '样本过期',
  'offline': '未接线',
  'unreachable': '不可达',
  'timeout': '查询超时',
  'metric-name-unconfirmed': '指标名待 Dell 核验',
  'synthetic-source': '响应自称合成数据',
  'untrusted-source': '来源不在可信白名单',
  'not-readonly': '不是只读投影',
  'wrong-version': '契约版本不符',
  'bad-shape': '响应形状不是投影契约',
  'bad-observed-at': '缺少或无法解析 observed_at',
  'future-observed-at': 'observed_at 位于未来，属时钟偏斜',
  'missing-sample-at': '缺少 sample_at，无法判定是否当前',
  'bad-sample-at': 'sample_at 不是带时区的 ISO-8601',
  'future-sample-at': 'sample_at 位于未来，属时钟偏斜',
  'stale-sample-at': 'sample_at 已超过年龄阈值',
  'non-finite-value': '样本值不是有限数',
  'unit-mismatch': '单位与该指标定义不符',
  'bad-state-value': '状态量不是 0/1',
  'unknown-metric-key': '投影缺少该指标键',
});

const METRIC_LABELS = Object.freeze({
  gpu_utilization_ratio: 'GPU 利用率',
  gpu_temperature_celsius: 'GPU 温度',
  gpu_power_watts: 'GPU 功耗',
  strata_up: 'Strata 引擎',
  gpu_memory_used_mib: '显存已用',
  gpu_memory_total_mib: '显存总量',
  gpu_memory_free_mib: '显存可用',
  gpu_memory_reserved_mib: '驱动保留显存',
});
const UNIT_SUFFIX = Object.freeze({ ratio: '%', celsius: '°C', watts: 'W', mib: 'MiB', bool: '' });

const reasonText = (reason) => HUMANS[reason] ?? (reason ? escapeHtml(String(reason)) : 'UNKNOWN');
const isMapping = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/**
 * Parse an ISO-8601 instant that **must carry a timezone**, or null.
 *
 * A timezone-naive string is rejected on purpose. `Date.parse('2026-10-16T03:58:00')`
 * silently reads as *local* time, so a naive sample time is exactly where the
 * answer to "is this still current?" gets lost — the same reason
 * `host_projection.parse_instant` refuses it.
 *
 * @returns {number|null} epoch milliseconds, or null
 */
export function parseInstant(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?([Zz]|[+-]\d{2}:?\d{2})$/.exec(text);
  if (!m) return null;
  let zone = m[8];
  if (zone !== 'Z' && zone !== 'z' && !zone.includes(':')) zone = `${zone.slice(0, 3)}:${zone.slice(3)}`;
  // The ES date format allows three fractional digits; anything finer is
  // truncated rather than handed to a parser that may or may not accept it.
  const fraction = m[7] ? `.${m[7].slice(0, 3).padEnd(3, '0')}` : '';
  const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6] ?? '00'}${fraction}${zone === 'z' ? 'Z' : zone}`;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/** A finite JSON number. A boolean is not one, even though JS says otherwise. */
const isFiniteNumber = (v) => typeof v === 'number' && Number.isFinite(v);

const unknownMetric = (key, reason, unit) => ({
  key,
  live: false,
  value: null,
  unit: unit ?? METRIC_UNITS[key] ?? OPTIONAL_METRIC_UNITS[key] ?? null,
  sampleAt: null,
  ageMs: null,
  reason,
});

/** `{reason, source, version, readonly, synthetic}`; `reason === ''` means the
 *  envelope is trusted.
 *
 *  "Is this a projection at all?" is asked before "what does it say?". A 404
 *  body like `{"error": "..."}` carries none of the four envelope fields — that
 *  is not "self-declared synthetic", it is not a projection, and merging the two
 *  sends an operator to debug a data source that was never involved. */
function envelopeRejection(payload) {
  if (!isMapping(payload)) return { reason: REJECT.BAD_SHAPE };
  const { source, version, readonly, synthetic } = payload;
  for (const required of ['source', 'synthetic', 'readonly', 'version']) {
    if (!(required in payload)) return { reason: REJECT.BAD_SHAPE, source, version, readonly, synthetic };
  }
  // `synthetic` must be the JSON boolean false. 0, "false" and absent all fail:
  // an upstream that means "not synthetic" with 0 cannot be told apart from one
  // that means "unknown" with 0.
  if (synthetic !== false) return { reason: REJECT.SYNTHETIC_SOURCE, source, version, readonly, synthetic };
  if (!PROJECTION_SOURCES.has(source)) return { reason: REJECT.UNTRUSTED_SOURCE, source, version, readonly, synthetic };
  if (readonly !== true) return { reason: REJECT.NOT_READONLY, source, version, readonly, synthetic };
  if (version !== PROJECTION_VERSION) return { reason: REJECT.WRONG_VERSION, source, version, readonly, synthetic };
  return { reason: '', source, version, readonly, synthetic };
}

/** One metric, judged against the contract at an explicit `now`. */
function metricVerdict(key, entry, { now, maxAgeMs, toleranceMs }) {
  const expectedUnit = METRIC_UNITS[key] ?? OPTIONAL_METRIC_UNITS[key];
  if (!isMapping(entry)) return unknownMetric(key, REJECT.BAD_SHAPE, expectedUnit);

  // The session-protected BFF emits its validated verdict as `live`; the local
  // collector/preview projection uses `known`. Both are accepted only inside
  // the trusted source envelope and still pass the same unit/time checks here.
  if (entry.state !== 'known' && entry.state !== 'live') {
    const upstream = entry.reason;
    return unknownMetric(key, UPSTREAM_REASONS.has(upstream) ? upstream : REJECT.UNKNOWN, entry.unit);
  }
  if (!isFiniteNumber(entry.value)) return unknownMetric(key, REJECT.NON_FINITE_VALUE, entry.unit);
  if (entry.unit !== expectedUnit) return unknownMetric(key, REJECT.UNIT_MISMATCH, expectedUnit);
  // A state量 is 0 or 1 and nothing else. `strata_up: 0.4` is neither up nor
  // down; reading it against a threshold would be inventing a fact.
  if (expectedUnit === 'bool' && entry.value !== 0 && entry.value !== 1) {
    return unknownMetric(key, REJECT.BAD_STATE_VALUE, expectedUnit);
  }

  const raw = entry.sample_at;
  if (raw === undefined || raw === null || (typeof raw === 'string' && !raw.trim())) {
    return unknownMetric(key, REJECT.MISSING_SAMPLE_AT, expectedUnit);
  }
  const at = parseInstant(raw);
  if (at === null) return unknownMetric(key, REJECT.BAD_SAMPLE_AT, expectedUnit);

  const ageMs = now - at;
  if (ageMs < -toleranceMs) return unknownMetric(key, REJECT.FUTURE_SAMPLE_AT, expectedUnit);
  if (ageMs > maxAgeMs) {
    return { ...unknownMetric(key, REJECT.STALE_SAMPLE_AT, expectedUnit), sampleAt: raw, ageMs };
  }
  return {
    key,
    live: true,
    value: entry.value,
    unit: expectedUnit,
    sampleAt: raw,
    ageMs: Math.max(0, ageMs),
    reason: null,
  };
}

function serviceVerdict(key, entry) {
  if (!isMapping(entry)) return { key, live: false, up: null, reason: REJECT.BAD_SHAPE };
  if (entry.state !== 'known' && entry.state !== 'live') {
    const upstream = entry.reason;
    return { key, live: false, up: null, reason: UPSTREAM_REASONS.has(upstream) ? upstream : REJECT.UNKNOWN };
  }
  // `up` must be a real boolean. `"false"` is truthy, so accepting a string here
  // would silently read "down" as "up".
  if (typeof entry.up !== 'boolean') return { key, live: false, up: null, reason: REJECT.BAD_SHAPE };
  return { key, live: true, up: entry.up, reason: null };
}

/**
 * Judge a `/api/infra/summary` response: a trusted reading, or an untrusted
 * unknown. Pure; `now` is explicit because "stale" is only meaningful relative
 * to a stated present.
 *
 * Failure is closed, and **envelope-level problems downgrade every metric at
 * once** — a field inside an untrusted envelope is not made trustworthy by
 * having the right shape.
 *
 * @returns {{accepted:boolean, reason:string|null, metrics:object, services:object,
 *            liveKeys:string[], anyLive:boolean, metricReasons:string[]}}
 */
const RUNTIME_SCHEMA = 'portal-runtime/1';
const RUNTIME_SOURCE = 'loopback-runtime-readonly';
const RUNTIME_ID = /^[A-Za-z0-9._:/-]{1,200}$/;

function unknownRuntime(reason = 'unreachable') {
  const unknown = { state: 'unknown', checkedAt: null, reason };
  return {
    state: 'unknown',
    source: RUNTIME_SOURCE,
    engine: { name: 'Strata', ...unknown },
    model: { id: null, vision: null, ...unknown },
    gateway: { ...unknown },
    route: { alias: 'strata-vision', targetModel: null, ...unknown },
  };
}

function validateRuntimeSummary(payload, now) {
  if (!isMapping(payload) || payload.schema_version !== RUNTIME_SCHEMA
      || payload.source !== RUNTIME_SOURCE || payload.synthetic !== false || payload.readonly !== true) {
    return unknownRuntime('invalid_response');
  }
  const observed = parseInstant(payload.observed_at);
  if (observed === null) return unknownRuntime('invalid_response');
  if (now - observed > MAX_SAMPLE_AGE_MS || observed - now > FUTURE_TOLERANCE_MS) return unknownRuntime('stale');
  const rows = ['engine', 'model', 'gateway', 'route'].map((key) => payload[key]);
  if (!rows.every(isMapping)) return unknownRuntime('invalid_response');

  const checked = (item, allowed) => {
    if (!allowed.has(item.state)) return { state: 'unknown', checkedAt: null, reason: 'invalid_response' };
    const at = parseInstant(item.checked_at);
    if (at === null) return { state: 'unknown', checkedAt: null, reason: 'invalid_response' };
    if (now - at > MAX_SAMPLE_AGE_MS || at - now > FUTURE_TOLERANCE_MS) return { state: 'unknown', checkedAt: null, reason: 'stale' };
    return { state: item.state, checkedAt: item.checked_at, reason: null };
  };
  const engine = checked(payload.engine, new Set(['healthy', 'degraded', 'unreachable', 'unknown']));
  const model = checked(payload.model, new Set(['loaded', 'unloaded', 'unknown']));
  const gateway = checked(payload.gateway, new Set(['healthy', 'unreachable', 'unknown']));
  const route = checked(payload.route, new Set(['applied', 'not_applied', 'unknown']));
  let id = payload.model.id;
  if (id !== null && (typeof id !== 'string' || !RUNTIME_ID.test(id))) {
    id = null;
    model.state = 'unknown';
    model.checkedAt = null;
    model.reason = 'invalid_response';
  }
  if (model.state === 'loaded' && !id) {
    model.state = 'unknown';
    model.checkedAt = null;
    model.reason = 'invalid_response';
  }
  const vision = typeof payload.model.vision === 'boolean' ? payload.model.vision : null;
  const alias = payload.route.alias === 'strata-vision' ? payload.route.alias : 'strata-vision';
  let targetModel = payload.route.target_model;
  if (targetModel !== null && (typeof targetModel !== 'string' || !RUNTIME_ID.test(targetModel))) {
    targetModel = null;
    route.state = 'unknown';
    route.checkedAt = null;
    route.reason = 'invalid_response';
  }
  if (payload.route.alias !== alias) {
    route.state = 'unknown';
    route.checkedAt = null;
    route.reason = 'invalid_response';
  }
  return {
    state: [engine, model, gateway, route].some((item) => item.state !== 'unknown') ? 'known' : 'unknown',
    source: RUNTIME_SOURCE,
    observedAt: payload.observed_at,
    engine: { ...engine, name: 'Strata' },
    model: { ...model, id, vision },
    gateway,
    route: { ...route, alias, targetModel },
  };
}

export function validateHostSummary(payload, {
  now = Date.now(),
  maxAgeMs = MAX_SAMPLE_AGE_MS,
  toleranceMs = FUTURE_TOLERANCE_MS,
} = {}) {
  const envelope = envelopeRejection(payload);
  let reason = envelope.reason;
  let accepted = reason === '';
  let observedAt = null;

  if (accepted) {
    const observed = parseInstant(payload.observed_at);
    if (observed === null) {
      reason = REJECT.BAD_OBSERVED_AT;
      accepted = false;
    } else if (observed - now > toleranceMs) {
      // The whole response claims to be from the future: as untrustworthy as a
      // single future sample, and it must not hold the panel live.
      reason = REJECT.FUTURE_OBSERVED_AT;
      accepted = false;
    } else {
      observedAt = payload.observed_at;
    }
  }

  const rawMetrics = isMapping(payload) ? payload.metrics : null;
  const runtime = validateRuntimeSummary(isMapping(payload) ? payload.runtime : null, now);
  const outcome = (metrics, services) => {
    const liveKeys = Object.keys(metrics).filter((k) => metrics[k].live).sort();
    const metricReasons = [...new Set(
      Object.values(metrics).filter((m) => !m.live && m.reason).map((m) => m.reason),
    )].sort();
    return {
      accepted: accepted && services !== null,
      reason: accepted && services !== null ? null : reason,
      source: envelope.source ?? null,
      observedAt,
      metrics,
      services: services ?? {},
      runtime,
      liveKeys,
      anyLive: liveKeys.length > 0,
      metricReasons,
    };
  };

  if (!isMapping(rawMetrics)) {
    // A missing `metrics` object is a malformed projection, not "no metrics".
    const rejected = accepted ? REJECT.BAD_SHAPE : reason;
    accepted = false;
    reason = rejected;
    const metrics = {};
    for (const key of Object.keys(METRIC_UNITS)) metrics[key] = unknownMetric(key, rejected);
    return outcome(metrics, {});
  }

  const metrics = {};
  for (const key of Object.keys(METRIC_UNITS)) {
    const entry = rawMetrics[key];
    // A missing key means we do not know which version of the projection
    // produced this body — reported as such, ahead of any envelope verdict.
    if (entry === undefined) {
      metrics[key] = unknownMetric(key, REJECT.UNKNOWN_METRIC_KEY);
      continue;
    }
    metrics[key] = accepted
      ? metricVerdict(key, entry, { now, maxAgeMs, toleranceMs })
      : unknownMetric(key, reason);
  }
  for (const key of Object.keys(OPTIONAL_METRIC_UNITS)) {
    if (!(key in rawMetrics)) continue;
    metrics[key] = accepted
      ? metricVerdict(key, rawMetrics[key], { now, maxAgeMs, toleranceMs })
      : unknownMetric(key, reason);
  }

  const services = {};
  const rawServices = payload.services;
  if (isMapping(rawServices)) {
    for (const key of SERVICE_KEYS) {
      const entry = rawServices[key];
      if (entry === undefined) continue;
      services[key] = accepted
        ? serviceVerdict(key, entry)
        : { key, live: false, up: null, reason };
    }
  }

  return outcome(metrics, services);
}

/** The panel's display state, `live` iff the envelope is accepted and something
 *  in it is a real reading. `now` is explicit; there is no default clock here
 *  because the caller already owns one. */
export function hostStatusState(summary, options = {}) {
  const verdict = validateHostSummary(summary, options);
  return verdict.accepted && verdict.anyLive ? DISPLAY.LIVE : DISPLAY.UNKNOWN;
}

/**
 * What the panel may draw, and why. Age is recomputed at every call — this is
 * the rule that stops a tab suspended overnight from still showing last night's
 * number (issue #38, rule 4).
 *
 * @returns {{state:string, reason:string|null, ageMs:number|null, ageText:string|null,
 *            verdict:object}}
 */
export function describeFreshness(summary, options = {}) {
  const verdict = validateHostSummary(summary, options);
  if (!verdict.accepted || !verdict.anyLive) {
    return {
      state: DISPLAY.UNKNOWN,
      reason: verdict.reason ?? verdict.metricReasons[0] ?? REASON.CONTRACT,
      ageMs: null,
      ageText: null,
      verdict,
    };
  }
  const newest = Math.min(...verdict.liveKeys.map((k) => verdict.metrics[k].ageMs ?? 0));
  const ageMs = Number.isFinite(newest) ? Math.max(0, newest) : 0;
  return {
    state: DISPLAY.LIVE,
    reason: null,
    ageMs,
    ageText: ageMs >= 1000 ? `${Math.round(ageMs / 1000)}s` : '<1s',
    verdict,
  };
}

/** The subset of a summary this client has verified as live, with `age_s`
 *  recomputed from each `sample_at` and the client's own clock.
 *
 *  Consumers downstream (the Compute hero) re-check `age_s`; recomputing it here
 *  means their check agrees with ours instead of trusting a number the server
 *  chose. Returns null when nothing is live, so a caller keeps its own source. */
export function liveProjection(summary, verdict, { now = Date.now() } = {}) {
  if (!verdict?.accepted || !verdict.anyLive) return null;
  const metrics = {};
  for (const key of Object.keys(verdict.metrics)) {
    const m = verdict.metrics[key];
    if (!m.live) continue;
    metrics[key] = {
      state: 'known',
      value: m.value,
      unit: m.unit,
      source: summary?.source ?? PROJECTION_SOURCE,
      sample_at: m.sampleAt,
      age_s: Math.max(0, (now - parseInstant(m.sampleAt)) / 1000),
    };
  }
  return {
    version: PROJECTION_VERSION,
    source: summary?.source ?? PROJECTION_SOURCE,
    synthetic: false,
    readonly: true,
    state: 'known',
    observed_at: summary?.observed_at ?? null,
    metrics,
    services: Object.fromEntries(
      Object.entries(verdict.services)
        .filter(([, s]) => s.live)
        .map(([key, s]) => [key, { state: 'known', up: s.up, reason: null }]),
    ),
  };
}

/** A finite number at most three significant digits, or null. */
export function formatValue(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  if (value === 0) return '0';
  const abs = Math.abs(value);
  if (abs >= 1000) return String(Math.round(value));
  if (abs >= 1) return String(Math.round(value * 100) / 100);
  return String(Math.round(value * 1000) / 1000);
}

const UNKNOWN_BADGE = '<span class="badge warn"><span class="dot rust"></span>UNKNOWN · NO SOURCE</span>';
const liveBadge = (source) => `<span class="badge good"><span class="dot"></span>LIVE · ${escapeHtml(source === 'nvml-verified' ? 'NVML verified' : 'Prometheus verified')}</span>`;

/**
 * Render the panel body. Pure: same input, same string, no DOM.
 *
 * LIVE is drawn only when `validateHostSummary` accepts the body *at this
 * moment* and finds a live metric. An UNKNOWN verdict — no verdict at all, an
 * unparsable `observed_at`, a future timestamp, an expired sample — never reaches
 * the LIVE badge, the numbers, or the UP/DOWN row. The previous version cleared
 * only on STALE, so anything that returned UNKNOWN sailed straight into LIVE.
 *
 * @param {object|null} summary the projection body, or anything else
 * @param {{variant?:string, now?:number, reason?:string|null, maxAgeMs?:number}} [opts]
 */
export function hostStatusBody(summary, {
  variant = 'full',
  now = Date.now(),
  reason = null,
  maxAgeMs = MAX_SAMPLE_AGE_MS,
  toleranceMs = FUTURE_TOLERANCE_MS,
} = {}) {
  const verdict = validateHostSummary(summary, { now, maxAgeMs, toleranceMs });
  const live = verdict.accepted && verdict.anyLive;
  const stateReason = live ? null : (verdict.reason ?? verdict.metricReasons[0] ?? REASON.CONTRACT);
  // The caller's reason (request failed, clock rollback) outranks a per-metric
  // one: it explains why there is no snapshot at all.
  const shownReason = live ? null : (reason ?? stateReason);

  const badge = live ? liveBadge(verdict.source) : UNKNOWN_BADGE;
  const reasonChip = live
    ? ''
    : `<span class="tiny mono" data-host-reason="${escapeHtml(String(shownReason))}">原因 ${reasonText(shownReason)}</span>`;

  // On the compute page the three hero cells are already live, so this panel
  // narrows to what the hero does not cover (temperature, power, platform
  // health) instead of repeating them.
  const HERO_KEYS = new Set(['gpu_utilization_ratio', 'gpu_memory_used_mib', 'gpu_memory_total_mib']);
  const keys = [...Object.keys(METRIC_UNITS), ...Object.keys(OPTIONAL_METRIC_UNITS).filter((key) => key in verdict.metrics)]
    .filter((key) => variant !== 'compute' || !HERO_KEYS.has(key));

  const tiles = keys.map((key) => {
    const m = verdict.metrics[key];
    const unit = UNIT_SUFFIX[m.unit] ?? '';
    // Prometheus ratio gauges are 0..1; display as percent, never as raw 0.62.
    const shown = m.live
      ? `${formatValue(m.unit === 'ratio' ? m.value * 100 : m.value)}${
        unit ? ` <span class="tiny">${unit}</span>` : ''}`
      : '—';
    // When the whole panel is unreadable the header chip already explains why,
    // so a tile carries only the short, greppable code. When the panel is live
    // and this one card is the exception, the human sentence belongs here.
    const sub = m.live
      ? `${escapeHtml(m.sampleAt ?? '')} · ${formatValue((m.ageMs ?? 0) / 1000) ?? '?'}s`
      : (live ? reasonText(m.reason) : `<span class="mono">${escapeHtml(String(m.reason))}</span>`);
    return '<div class="statusitem" data-host-metric="' + escapeHtml(key) + '">' +
      '<div class="tiny">' + escapeHtml(METRIC_LABELS[key] ?? key) + '</div>' +
      '<strong class="number-host">' + shown + '</strong>' +
      '<div class="tiny mono">' + sub + '</div></div>';
  }).join('');

  const serviceItems = SERVICE_KEYS.map((key) => {
    const s = verdict.services[key];
    if (!s) return '';
    // Liveness rides the same snapshot as the numbers, so it ages with them. A
    // body we cannot date must not keep asserting "Strata UP" — the probe result
    // is exactly as old as the sample that could not be dated.
    const usable = live && s.live;
    const text = usable ? (s.up ? 'UP' : 'DOWN') : 'UNKNOWN';
    const cls = usable ? (s.up ? 'good' : 'warn') : 'warn';
    const note = usable
      ? 'status-only'
      : (live ? reasonText(s.reason) : `<span class="mono">${escapeHtml(String(shownReason))}</span>`);
    return '<div class="statusitem"><div class="tiny">' + escapeHtml(key) + '</div>' +
      '<span class="badge ' + cls + '">' + text + '</span>' +
      '<div class="tiny mono">' + note + '</div></div>';
  }).join('');

  const observed = live && summary?.observed_at
    ? `<span class="mono">observed_at ${escapeHtml(String(summary.observed_at))}</span>`
    : '';

  return (
    '<div class="group-title"><h2>主机资源 <em class="heading-en">/ HOST</em></h2>' +
    '<span class="smallmeta">READ-ONLY · HOST LEVEL</span></div>' +
    '<div class="headrow" style="margin-bottom:12px"><span class="tiny">来源 source=' +
    escapeHtml(String(summary?.source ?? 'none')) + '</span>' + reasonChip + badge + '</div>' +
    '<div class="statusrows">' + tiles + '</div>' +
    '<div class="group-title" style="margin-top:20px"><h2>平台连通性</h2><span class="smallmeta">STATUS ONLY</span></div>' +
    '<div class="statusrows">' + serviceItems + '</div>' +
    '<div class="guard">仅主机级只读遥测：GPU 利用率 / 温度 / 功耗与平台连通性。' +
    '这里<b>没有</b>按用户、项目、Key 或模型的调用量与显存；这些字段不在本投影内。' +
    ' LIVE 只在信封与每一条 <span class="mono">sample_at</span> 都通过校验时成立。' +
    (observed ? ' ' + observed : '') + '</div>'
  );
}

function ensureSection(doc, host, key, heading, intro, before = null) {
  const existing = host.querySelector(`[data-host-status="${key}"]`);
  if (existing) return existing;
  const section = doc.createElement('section');
  section.className = 'page-block module-panel';
  section.dataset.hostStatus = key;
  section.setAttribute('aria-labelledby', `host-status-${key}-title`);
  const title = doc.createElement('h2');
  title.id = `host-status-${key}-title`;
  title.className = 'section-h';
  title.textContent = heading;
  const body = doc.createElement('div');
  body.className = 'module-body';
  body.dataset.hostStatusBody = key;
  body.innerHTML = intro;
  section.appendChild(title);
  section.appendChild(body);
  host.insertBefore(section, before);
  return section;
}

/** The overview hero's infrastructure block. It reports the same host facts the
 *  panel below shows, so the page cannot contradict itself. Only host-level
 *  service liveness is projected — never a per-user figure. */
export function paintInfrastructure(doc, summary) {
  const block = doc.querySelector('#view-overview .right-hardware');
  if (!block) return;
  const live = PROJECTION_SOURCES.has(summary?.source) && summary?.state === 'known';
  const svc = summary?.services ?? {};
  const label = (key, fallback) => {
    if (!live) return fallback;
    const s = svc[key];
    if (!s || s.state !== 'known') return 'UNKNOWN';
    return s.up === true ? 'ONLINE' : 'DOWN';
  };
  const badge = block.querySelector('.badge');
  if (badge) {
    badge.className = live ? 'badge good' : 'badge';
    badge.textContent = live ? '主机遥测 LIVE' : '待核对';
  }
  const rows = [
    ['模型引擎', label('strata', '待接线'), 'Strata · 只读健康'],
    ['API Gateway', label('gateway', '待接线'), '只读健康；不代表用户数据已授权'],
    ['GPU 遥测', live ? 'LIVE' : 'UNKNOWN', live ? (summary?.source === 'nvml-verified' ? 'NVML 只读' : 'Prometheus 只读') : '数据来源待验证'],
  ];
  const values = block.querySelectorAll('.value-row');
  rows.forEach(([k, v, note], i) => {
    const row = values[i];
    if (!row) return;
    row.innerHTML = '<span class="tiny">' + k + '</span>' +
      '<span class="status"><span class="dot"></span><b>' + escapeHtml(String(v)) + '</b></span>' +
      '<span class="tiny mono" style="margin-left:auto">' + escapeHtml(note) + '</span>';
  });
}

/** Keep the shell's own status words in step with the host read-out. They must
 *  distinguish "this UI is a preview" from "the host telemetry is live" — and
 *  never announce overall production readiness. */
function paintShellStatus(doc, live, source) {
  const strip = doc.querySelector('[data-demo-state]');
  if (strip) {
    strip.textContent = live
      ? `主机遥测 LIVE（${source === 'nvml-verified' ? 'NVML' : 'Prometheus'} 只读）；个人调用 / 评测数据仍为演示或待授权。`
      : '主机遥测正在连接或不可用（UNKNOWN）；个人调用 / 评测数据仍为演示或待授权。';
  }
  const badge = doc.querySelector('[data-source-badge]');
  if (badge) {
    // The attribute stays `gated` in both states: this badge is about the
    // per-user call/usage data, which is not authorized either way.
    badge.className = live ? 'badge' : 'badge warn';
    badge.innerHTML = live
      ? '<span class="dot"></span>主机遥测 LIVE · 调用 / 用量待授权'
      : '<span class="dot rust"></span>待授权 · NOT CONNECTED';
  }
  const top = doc.querySelector('.topstatus');
  if (top) {
    top.innerHTML = live
      ? '<span class="dot"></span>DESIGN PREVIEW · 主机遥测 LIVE'
      : '<span class="dot rust"></span>DESIGN PREVIEW';
  }
}

/**
 * Fetch the projection and paint it. Mounting is idempotent; a second call only
 * reloads.
 *
 * This is a direct port of `HostRefreshMachine` (Lane Data, PR #53), which is
 * the contract's executable definition:
 *
 *   * at most one request in flight, and every request has a deadline;
 *   * a hidden tab fires nothing;
 *   * a visible tab fetches at once if the interval has elapsed;
 *   * age is re-checked on every paint, not only on every response, so a
 *     suspended tab drops out of LIVE without any new byte arriving;
 *   * failure, contract mismatch and a local clock rollback all clear the value;
 *   * a late response never overwrites a newer one.
 *
 * @param {{doc?:Document, fetchImpl?:Function, path?:string, onChange?:Function,
 *          now?:Function, setIntervalFn?:Function, clearIntervalFn?:Function,
 *          setTimeoutFn?:Function, clearTimeoutFn?:Function,
 *          intervalMs?:number, timeoutMs?:number, autoRefresh?:boolean}} [deps]
 */
export function mountHostStatus({
  doc = globalThis.document,
  fetchImpl = globalThis.fetch?.bind(globalThis),
  path = HOST_STATUS_PATH,
  onChange = null,
  now = () => Date.now(),
  setIntervalFn = globalThis.setInterval?.bind(globalThis),
  clearIntervalFn = globalThis.clearInterval?.bind(globalThis),
  setTimeoutFn = globalThis.setTimeout?.bind(globalThis),
  clearTimeoutFn = globalThis.clearTimeout?.bind(globalThis),
  intervalMs = REFRESH_INTERVAL_MS,
  timeoutMs = REQUEST_TIMEOUT_MS,
  maxAgeMs = MAX_SAMPLE_AGE_MS,
  toleranceMs = FUTURE_TOLERANCE_MS,
  autoRefresh = true,
} = {}) {
  const overview = doc?.querySelector('#view-overview');
  const compute = doc?.querySelector('#view-compute');
  if (!overview && !compute) return null;

  const pending = '<p class="module-empty">正在读取主机只读遥测…</p>';
  if (overview) {
    // Placed directly under the hero: the one real number on this page must be
    // on the first screen, not below a wall of demo call data.
    const hero = overview.querySelector('.hero');
    ensureSection(doc, overview, 'overview', '主机遥测 · 固定只读来源', pending, hero ? hero.nextSibling : null);
  }
  if (compute) ensureSection(doc, compute, 'compute', '主机资源（只读）', pending);

  let inFlight = 0;
  let sentSeq = 0;
  let lastRequestAt = null;
  // Seeded at mount, not left null: a rollback that happens between mounting and
  // the first tick is still a rollback, and a null clock cannot detect one.
  let lastClock = now();
  let timer = null;
  // `display.state === LIVE` is the only thing that may light a live indicator.
  let display = { state: DISPLAY.UNKNOWN, summary: null, reason: REASON.NOT_FETCHED, freshness: null };
  // The last verdict actually painted. Kept so `__HOST__.freshness` reports what
  // is on screen rather than re-deriving a second opinion.
  let lastFreshness = { state: DISPLAY.UNKNOWN, ageMs: null, ageText: null, reason: REASON.NOT_FETCHED };
  const events = [];

  const note = (event, detail) => events.push({ event, detail, at: now() });

  function render(extraReason = null) {
    const at = now();
    const freshness = display.summary
      ? describeFreshness(display.summary, { now: at, maxAgeMs, toleranceMs })
      : { state: DISPLAY.UNKNOWN, reason: display.reason, verdict: null, ageMs: null, ageText: null };
    lastFreshness = freshness;
    const live = freshness.state === DISPLAY.LIVE;
    const reason = live ? null : (extraReason ?? display.reason ?? freshness.reason);

    for (const key of ['overview', 'compute']) {
      const body = doc.querySelector(`[data-host-status-body="${key}"]`);
      if (!body) continue;
      body.dataset.freshness = live ? DISPLAY.LIVE : DISPLAY.UNKNOWN;
      body.dataset.hostReason = String(reason ?? '');
      body.innerHTML = hostStatusBody(display.summary, {
        variant: key === 'compute' ? 'compute' : 'full',
        now: at,
        reason,
        maxAgeMs,
        toleranceMs,
      });
    }
    const projection = live ? liveProjection(display.summary, freshness.verdict, { now: at }) : null;
    paintInfrastructure(doc, projection);
    paintShellStatus(doc, live, display.summary?.source);
    if (typeof onChange === 'function') {
      try { onChange(projection); } catch { /* a consumer must never break the shell */ }
    }
    return { live, reason, freshness };
  }

  function clear(reason) {
    display = { state: DISPLAY.UNKNOWN, summary: null, reason, freshness: null };
  }

  /** Re-age the snapshot already on screen. Nothing is fetched: this is how a
   *  copy that outlived its sample stops claiming to be current. */
  function expireIfAged() {
    if (display.state !== DISPLAY.LIVE || !display.summary) return false;
    const verdict = validateHostSummary(display.summary, { now: now(), maxAgeMs, toleranceMs });
    if (verdict.accepted && verdict.anyLive) return false;
    clear(REASON.EXPIRED);
    note('cleared', REASON.EXPIRED);
    return true;
  }

  /** Returns true when the local clock went backwards since the last call. */
  function syncClock(at) {
    const rollback = lastClock !== null && at < lastClock;
    lastClock = at;
    return rollback;
  }

  const intervalElapsed = (at) => lastRequestAt === null || (at - lastRequestAt) >= intervalMs;

  function fire(at) {
    sentSeq += 1;
    inFlight += 1;
    lastRequestAt = at;
    const seq = sentSeq;
    note('request', `seq=${seq}`);

    const controller = typeof globalThis.AbortController === 'function' ? new AbortController() : null;
    let deadline = null;
    const TIMED_OUT = Symbol('timeout');
    const expired = new Promise((resolve) => {
      if (!setTimeoutFn) return;
      deadline = setTimeoutFn(() => resolve(TIMED_OUT), timeoutMs);
    });
    const request = (async () => {
      try {
        const res = await fetchImpl(path, {
          method: 'GET',
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          ...(controller ? { signal: controller.signal } : {}),
        });
        return res.ok ? await res.json() : null;
      } catch {
        return null;
      }
    })();

    // The race is what makes the deadline real: a fetch implementation that
    // ignores `signal` (a test double, or a socket stuck below the platform)
    // would otherwise leave this awaiting forever and `inFlight` pinned.
    Promise.race([request, expired]).then((outcome) => {
      if (deadline !== null && clearTimeoutFn) clearTimeoutFn(deadline);
      if (outcome === TIMED_OUT) {
        controller?.abort?.();
        fail(seq, REASON.REQUEST_FAILED);
      } else {
        respond(seq, outcome);
      }
    });
    return seq;
  }

  function respond(seq, payload) {
    if (seq !== sentSeq) { note('ignored-late', `seq=${seq}`); return; }
    inFlight = Math.max(0, inFlight - 1);
    const at = now();
    const verdict = validateHostSummary(payload, { now: at, maxAgeMs, toleranceMs });
    if (!verdict.accepted || !verdict.anyLive) {
      clear(`${REASON.CONTRACT}:${verdict.reason ?? verdict.metricReasons[0] ?? 'no-live-metric'}`);
      note('cleared', display.reason);
      render();
      return;
    }
    display = { state: DISPLAY.LIVE, summary: payload, reason: null, freshness: null };
    note('applied', `seq=${seq}`);
    render();
  }

  function fail(seq, reason) {
    if (seq !== sentSeq) { note('ignored-late', `seq=${seq}`); return; }
    inFlight = Math.max(0, inFlight - 1);
    clear(reason);
    note('cleared', reason);
    render();
  }

  /** One timer tick. Ported from `HostRefreshMachine.tick`. */
  function tick() {
    const at = now();
    if (syncClock(at)) {
      // After a clock rollback, "is this sample still fresh?" has no answer.
      // Clear it and re-fetch rather than computing age against a rewound clock.
      clear(REASON.CLOCK_ROLLBACK);
      render();
      return fire(at);
    }
    expireIfAged();
    // Paint the re-aged verdict *before* deciding about the network. Otherwise a
    // tick that fires a request immediately loses the clear: the panel would keep
    // showing the old LIVE numbers until some response arrived — which is exactly
    // the "hung request pins LIVE" failure, just moved one step.
    render();
    if (doc.hidden) { note('skip', 'hidden'); return null; }
    if (inFlight >= 1) { note('skip', 'in-flight'); return null; }
    if (!intervalElapsed(at)) { note('skip', 'interval'); return null; }
    return fire(at);
  }

  /** Page visibility changed. Coming back fetches at once if due, and re-ages
   *  the snapshot either way so old numbers do not get one more glance. */
  function onVisibility() {
    const at = now();
    syncClock(at);
    if (doc.hidden) { note('visibility', 'hidden'); return null; }
    note('visibility', 'visible');
    expireIfAged();
    render();
    if (inFlight >= 1) return null;
    if (!intervalElapsed(at)) return null;
    return fire(at);
  }

  /** Manual reload. Exposed for the retry affordance and for tests. */
  function refresh() {
    if (inFlight >= 1) return inFlight;
    return fire(now());
  }

  function start() {
    if (!autoRefresh || !setIntervalFn || timer !== null) return;
    timer = setIntervalFn(() => { tick(); }, intervalMs);
    doc.addEventListener?.('visibilitychange', onVisibility);
  }

  function stop() {
    if (timer !== null && clearIntervalFn) clearIntervalFn(timer);
    timer = null;
    doc.removeEventListener?.('visibilitychange', onVisibility);
  }

  // Not awaited by the caller: a refused or slow source must not hold up the
  // shell, exactly like the layers above.
  const loading = Promise.resolve().then(() => { fire(now()); });
  start();

  return {
    refresh,
    stop,
    tick,
    expireIfAged,
    render,
    get state() { return display.state; },
    get display() { return display.state; },
    get reason() { return display.reason; },
    get freshness() { return lastFreshness.state; },
    get ageMs() { return lastFreshness.ageMs; },
    get summary() { return display.summary; },
    get inFlight() { return inFlight; },
    get sentSeq() { return sentSeq; },
    get events() { return [...events]; },
    loading,
  };
}
