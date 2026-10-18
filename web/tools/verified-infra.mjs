/*! web/tools/verified-infra.mjs — the read-only host telemetry projection.
 *
 *  This is the only place in the build that is allowed to know about the Dell
 *  host's loopback services. It answers one question — "what are the *host-level*
 *  GPU facts this preview may show?" — and it answers it by asking Prometheus a
 *  fixed, hard-coded set of metric names over loopback and projecting the
 *  numbers into a small, explicit contract.
 *
 *  Deliberate limits, all of them load-bearing:
 *    - it runs only when the operator sets ATELIER_INFRA_READONLY=1; unset (the
 *      default) means the route is a 404 and this file is never reached;
 *    - the queries are a fixed whitelist. Nothing from the request reaches the
 *      Prometheus URL, so there is no query injection and no arbitrary proxy;
 *    - it talks to 127.0.0.1 only, with a short timeout;
 *    - the response is *constructed field by field*. No upstream body or JSON is
 *      echoed or spread, so a Prometheus label — or a Strata /health `api_key` —
 *      cannot ride out inside it. Series labels are read for counting only and
 *      never copied;
 *    - metric names and byte units are pinned to the Dell Prometheus metadata
 *      API observations (2026-10-16); a missing sample is UNKNOWN;
 *    - a missing, non-finite or stale sample (older than MAX_SAMPLE_AGE_S) is
 *      UNKNOWN, never 0. "No data" and "measured zero" must not look alike.
 *
 *  Nothing here groups by user, project, key or model: only host-level GPU
 *  gauges and boolean service liveness cross the boundary.
 */

export const INFRA_PATH = '/api/infra/summary';
export const CONTRACT_VERSION = 1;
export const SOURCE = 'prometheus-readonly';

/** Loopback only. A host name here would make the preview a network client. */
export const PROMETHEUS_ORIGIN = 'http://127.0.0.1:9090';
export const MAX_SAMPLE_AGE_S = 90;
export const REQUEST_TIMEOUT_MS = 1500;

/** Operator gate. Anything other than the exact string '1' keeps the route off. */
export function infraEnabled(env = process.env) {
  return env?.ATELIER_INFRA_READONLY === '1';
}

/**
 * The exact metric names this projection may ask for.
 *
 * All the names below have been verified against Dell Prometheus /api/v1/metadata
 * (including the precise GPU memory used/total byte gauges). A removed or
 * missing source produces UNKNOWN rather than a guessed zero.
 *
 * `scale` converts the raw sample into `unit` (e.g. bytes -> MiB). `agg` says how
 * several per-GPU series collapse into one host-level number: 'max' everywhere,
 * because these are gauges reported per device and the honest host summary is
 * the highest observed value — with `series` returned so the reader can tell one
 * device from several.
 */
export const METRICS = Object.freeze([
  { key: 'gpu_utilization_ratio', metric: 'llmlab_strata_gpu_utilization_ratio', unit: 'ratio', scale: 1, agg: 'max' },
  { key: 'gpu_temperature_celsius', metric: 'llmlab_strata_gpu_temperature_celsius', unit: 'celsius', scale: 1, agg: 'max' },
  { key: 'gpu_power_watts', metric: 'llmlab_strata_gpu_power_watts', unit: 'watts', scale: 1, agg: 'max' },
  { key: 'strata_up', metric: 'llmlab_strata_up', unit: 'bool', scale: 1, agg: 'max' },
  { key: 'gpu_memory_used_mib', metric: 'llmlab_strata_gpu_memory_used_bytes', unit: 'mib', scale: 1 / 1048576, agg: 'max' },
  { key: 'gpu_memory_total_mib', metric: 'llmlab_strata_gpu_memory_total_bytes', unit: 'mib', scale: 1 / 1048576, agg: 'max' },
]);

/** Loopback health probes. Only the HTTP status is read; the body is drained and
 *  discarded, never parsed and never returned. Strata is deliberately absent —
 *  its state comes from `llmlab_strata_up` so its /health body (which carries an
 *  `api_key`) is never fetched at all. */
export const HEALTH_TARGETS = Object.freeze([
  { key: 'prometheus', url: `${PROMETHEUS_ORIGIN}/-/healthy` },
  { key: 'gateway', url: 'http://127.0.0.1:8200/healthz' },
  { key: 'grafana', url: 'http://127.0.0.1:3000/api/health' },
  { key: 'evalscope', url: 'http://127.0.0.1:9000/health' },
]);

const unknownMetric = (spec, reason) => ({
  state: 'unknown',
  value: null,
  unit: spec.unit,
  source: SOURCE,
  metric: spec.metric,
  reason,
});

const isFiniteNumber = (n) => typeof n === 'number' && Number.isFinite(n);

/** Read one instant vector into `{value, series, observed_at, age_s}` or a reason. */
export function projectVector(vector, spec, nowMs) {
  if (!vector || vector.status !== 'success' || vector?.data?.resultType !== 'vector') {
    return { reason: 'prometheus-error' };
  }
  const result = vector?.data?.result;
  if (!Array.isArray(result) || result.length === 0) return { reason: 'no-sample' };
  const samples = [];
  let minStaleAge = Infinity;
  for (const series of result) {
    // Never copy or inspect series.metric labels (user/host/endpoint identifiers).
    const raw = Array.isArray(series?.value) ? series.value : null;
    if (!raw || raw.length !== 2) continue;
    if (typeof raw[1] !== 'string' || !raw[1].trim()) continue;
    const t = Number(raw[0]);
    const v = Number(raw[1]);
    if (!isFiniteNumber(t) || !isFiniteNumber(v) || t <= 0 || v < 0) continue;
    const age = (nowMs / 1000) - t;
    // Do not let an old/future sample contaminate an aggregate just because
    // another series was fresh. Prometheus timestamps may lead by <=15s.
    if (age > MAX_SAMPLE_AGE_S) { minStaleAge = Math.min(minStaleAge, age); continue; }
    if (age < -15) continue;
    samples.push({ t, v });
  }
  if (samples.length === 0) {
    return Number.isFinite(minStaleAge)
      ? { reason: 'stale', age_s: Math.round(minStaleAge) }
      : { reason: 'non-finite-sample' };
  }
  const maxTimestamp = Math.max(...samples.map((s) => s.t));
  const value = spec.agg === 'max' ? Math.max(...samples.map((s) => s.v)) : samples[0].v;
  return {
    value: value * spec.scale,
    series: samples.length,
    age_s: Math.round(Math.max(0, nowMs / 1000 - maxTimestamp)),
    sample_at: new Date(maxTimestamp * 1000).toISOString(),
  };
}

async function getJson(fetchImpl, url, timeoutMs) {
  const res = await fetchImpl(url, { method: 'GET', signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`http ${res.status}`);
  return res.json();
}

async function probeUp(fetchImpl, url, timeoutMs) {
  const res = await fetchImpl(url, { method: 'GET', signal: AbortSignal.timeout(timeoutMs) });
  // Drain so the socket is released, then drop the body on the floor.
  await res.arrayBuffer?.().catch?.(() => {});
  return res.status === 200;
}

/**
 * Build the summary the preview is allowed to show.
 *
 * @param {{fetchImpl?:Function, now?:()=>number, timeoutMs?:number}} [deps]
 *   `fetchImpl` is injected so the Node tests never touch a socket.
 * @returns {Promise<object>} the contract object — always a complete shape.
 */
export async function collectInfraSummary({
  fetchImpl = globalThis.fetch,
  now = () => Date.now(),
  timeoutMs = REQUEST_TIMEOUT_MS,
} = {}) {
  const startedMs = now();
  const metrics = {};

  await Promise.all(METRICS.map(async (spec) => {
    if (!spec.metric) {
      metrics[spec.key] = unknownMetric(spec, 'metric-name-unconfirmed');
      return;
    }
    try {
      const url = `${PROMETHEUS_ORIGIN}/api/v1/query?query=${encodeURIComponent(spec.metric)}`;
      const body = await getJson(fetchImpl, url, timeoutMs);
      const projected = projectVector(body, spec, startedMs);
      metrics[spec.key] = projected.value === undefined
        ? { ...unknownMetric(spec, projected.reason), ...(projected.age_s !== undefined ? { age_s: projected.age_s } : {}) }
        : {
          state: 'known',
          value: projected.value,
          unit: spec.unit,
          source: SOURCE,
          metric: spec.metric,
          series: projected.series,
          age_s: projected.age_s,
          sample_at: projected.sample_at,
        };
    } catch (error) {
      metrics[spec.key] = unknownMetric(spec, error?.name === 'TimeoutError' ? 'timeout' : 'unreachable');
    }
  }));

  const services = {};
  await Promise.all(HEALTH_TARGETS.map(async ({ key, url }) => {
    try {
      services[key] = { state: 'known', up: await probeUp(fetchImpl, url, timeoutMs), reason: null };
    } catch {
      services[key] = { state: 'unknown', up: null, reason: 'unreachable' };
    }
  }));
  // Strata's state is projected from the verified metric, never from its /health.
  const strata = metrics.strata_up;
  services.strata = strata.state === 'known'
    ? { state: 'known', up: strata.value >= 1, reason: null }
    : { state: 'unknown', up: null, reason: strata.reason };

  const values = Object.values(metrics);
  const anyKnown = values.some((m) => m.state === 'known');
  return {
    version: CONTRACT_VERSION,
    source: SOURCE,
    synthetic: false,
    readonly: true,
    state: anyKnown ? 'known' : 'unknown',
    observed_at: new Date(startedMs).toISOString(),
    metrics,
    services,
  };
}

/** A complete UNKNOWN answer, for when the host is unreachable or the flag is a
 *  transient error. Same shape as `collectInfraSummary`, so the client never
 *  needs a second code path. */
export function offlineSummary(now = () => Date.now()) {
  const metrics = {};
  for (const spec of METRICS) metrics[spec.key] = unknownMetric(spec, 'offline');
  const services = {};
  for (const key of ['strata', 'gateway', 'prometheus', 'grafana', 'evalscope']) {
    services[key] = { state: 'unknown', up: null, reason: 'offline' };
  }
  return {
    version: CONTRACT_VERSION,
    source: SOURCE,
    synthetic: false,
    readonly: true,
    state: 'unknown',
    observed_at: new Date(now()).toISOString(),
    metrics,
    services,
  };
}
