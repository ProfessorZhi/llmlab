/*! tests/web-features/host-freshness-contract.test.js
 *
 *  The host read-out's contract, in full. Closed from Issue #44's review on
 *  PR #54, which found three ways `web/js/infra/host-status.js` said LIVE when it
 *  had no right to:
 *
 *    1. UNKNOWN freshness still rendered LIVE — the renderer cleared on STALE
 *       only, so a body with no usable clock sailed straight through;
 *    2. a future `observed_at` was treated as "very fresh" by `Math.max(0, …)`;
 *    3. a hung request pinned the last LIVE numbers forever: the interval only
 *       called `refresh()`, which returned the in-flight promise, and there was
 *       no deadline.
 *
 *  The authority for "what counts as a reading" is Lane Data's
 *  `backend/observability/host_projection.py` plus the shared fixture set in
 *  `tests/observability/fixtures/host_summary/` (PR #53). Two layers of testing
 *  follow:
 *
 *    * **Boundary cases** — self-contained, always run, mirroring each fixture's
 *      intent so this file is meaningful even before #53 merges;
 *    * **Fixture conformance** — the *same* JSON files, asserted against the same
 *      expected answers as the Python suite. It runs when the fixtures are on the
 *      branch and skips with a named reason when they are not.
 *
 *  No jsdom; the doubles model only what `mountHostStatus` touches.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  DISPLAY,
  FUTURE_TOLERANCE_MS,
  MAX_SAMPLE_AGE_MS,
  REASON,
  REJECT,
  describeFreshness,
  hostStatusBody,
  hostStatusState,
  liveProjection,
  mountHostStatus,
  parseInstant,
  validateHostSummary,
} from '../../web/js/infra/host-status.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

/** The reference wall clock every case is judged against. */
const NOW = Date.parse('2026-10-16T12:00:00Z');
const iso = (ms) => new Date(ms).toISOString().replace('.000Z', 'Z');

// --- the shared input shape -------------------------------------------------

const metric = (value, unit, sampleAt) => ({
  state: 'known', value, unit, source: 'prometheus-readonly',
  metric: 'llmlab_strata', series: 1, age_s: 8, sample_at: sampleAt,
});

const FRESH_AT = iso(NOW - 8_000);

/** The only shape that may show LIVE: six fresh metrics, all dated. */
function liveFixture(overrides = {}) {
  return {
    version: 1,
    source: 'prometheus-readonly',
    synthetic: false,
    readonly: true,
    state: 'known',
    observed_at: iso(NOW - 4_000),
    metrics: {
      gpu_utilization_ratio: metric(0.62, 'ratio', FRESH_AT),
      gpu_temperature_celsius: metric(64, 'celsius', FRESH_AT),
      gpu_power_watts: metric(231, 'watts', FRESH_AT),
      strata_up: metric(1, 'bool', FRESH_AT),
      gpu_memory_used_mib: metric(9216, 'mib', FRESH_AT),
      gpu_memory_total_mib: metric(24576, 'mib', FRESH_AT),
    },
    services: {
      prometheus: { state: 'known', up: true, reason: null },
      gateway: { state: 'known', up: true, reason: null },
      grafana: { state: 'known', up: true, reason: null },
      evalscope: { state: 'unknown', up: null, reason: 'unreachable' },
      strata: { state: 'known', up: true, reason: null },
    },
    ...overrides,
  };
}

const judge = (payload, opts = {}) => validateHostSummary(payload, { now: NOW, ...opts });

// --- 1. the boundaries, one per shared fixture ------------------------------

test('01 a fresh projection is the only shape that is LIVE', () => {
  const v = judge(liveFixture());
  assert.equal(v.accepted, true);
  assert.equal(v.anyLive, true);
  assert.deepEqual(v.liveKeys, [
    'gpu_memory_total_mib', 'gpu_memory_used_mib', 'gpu_power_watts',
    'gpu_temperature_celsius', 'gpu_utilization_ratio', 'strata_up',
  ]);
  assert.deepEqual(v.metricReasons, []);
  assert.equal(hostStatusState(liveFixture(), { now: NOW }), DISPLAY.LIVE);
});

test('02 one missing metric stays UNKNOWN while the rest are LIVE', () => {
  const payload = liveFixture();
  payload.metrics.gpu_power_watts = { state: 'unknown', value: null, unit: 'watts', reason: 'no-sample' };
  const v = judge(payload);
  assert.equal(v.accepted, true);
  assert.equal(v.anyLive, true, 'a single unknown metric must not take the panel down');
  assert.equal(v.liveKeys.length, 5);
  assert.deepEqual(v.metricReasons, ['no-sample']);
  assert.equal(v.metrics.gpu_power_watts.live, false);
  // The reason is passed through, and the other cards still render.
  const body = hostStatusBody(payload, { now: NOW });
  assert.match(body, /LIVE · Prometheus verified/);
  assert.match(body, /上游无样本/);
});

test('03 a measured zero stays zero, and is not confused with "no sample"', () => {
  const payload = liveFixture();
  payload.metrics.gpu_utilization_ratio = metric(0, 'ratio', FRESH_AT);
  const v = judge(payload);
  assert.equal(v.metrics.gpu_utilization_ratio.live, true);
  assert.equal(v.metrics.gpu_utilization_ratio.value, 0);
  const body = hostStatusBody(payload, { now: NOW });
  assert.match(body, /data-host-metric="gpu_utilization_ratio"[\s\S]*?<strong class="number-host">0 <span class="tiny">%<\/span><\/strong>/);
});

test('04 state=known with no sample_at is not LIVE (review P0 #1)', () => {
  const payload = liveFixture();
  for (const key of Object.keys(payload.metrics)) delete payload.metrics[key].sample_at;
  const v = judge(payload);
  // The envelope is still trusted — this is not a "bad source" story, it is
  // "nothing in it can be dated", and the panel must not paper over that.
  assert.equal(v.accepted, true);
  assert.equal(v.anyLive, false);
  assert.deepEqual(v.metricReasons, [REJECT.MISSING_SAMPLE_AT]);
  assert.equal(hostStatusState(payload, { now: NOW }), DISPLAY.UNKNOWN);
  const body = hostStatusBody(payload, { now: NOW });
  assert.doesNotMatch(body, /LIVE · Prometheus verified/);
  assert.doesNotMatch(body, />UP</, 'no service may assert liveness off an undatable body');
  assert.match(body, /缺少 sample_at/);
});

test('05 a future timestamp is clock skew, not freshness (review P0 #2)', () => {
  const payload = liveFixture();
  const ahead = iso(NOW + 10 * 60_000);
  for (const key of Object.keys(payload.metrics)) payload.metrics[key].sample_at = ahead;
  const v = judge(payload);
  assert.equal(v.accepted, true);
  assert.equal(v.anyLive, false);
  assert.deepEqual(v.metricReasons, [REJECT.FUTURE_SAMPLE_AT]);
  assert.equal(hostStatusState(payload, { now: NOW }), DISPLAY.UNKNOWN);
});

test('05b the tolerance is bounded, and only the tolerance', () => {
  const payload = liveFixture();
  payload.metrics.strata_up.sample_at = iso(NOW + FUTURE_TOLERANCE_MS);
  assert.equal(judge(payload).metrics.strata_up.live, true, 'exactly at the tolerance is allowed');
  payload.metrics.strata_up.sample_at = iso(NOW + FUTURE_TOLERANCE_MS + 1);
  assert.equal(judge(payload).metrics.strata_up.live, false, 'one millisecond past it is not');
  assert.equal(judge(payload).metrics.strata_up.reason, REJECT.FUTURE_SAMPLE_AT);
});

test('06 a self-declared synthetic body is refused outright', () => {
  for (const synthetic of [true, 0, 'false', undefined]) {
    const v = judge(liveFixture({ synthetic }));
    assert.equal(v.accepted, false, `synthetic=${String(synthetic)} must not be accepted`);
    assert.equal(v.anyLive, false);
    assert.equal(v.reason, REJECT.SYNTHETIC_SOURCE);
    assert.deepEqual(v.metricReasons, [REJECT.SYNTHETIC_SOURCE]);
  }
});

test('07 a source outside the whitelist is refused, and every metric with it', () => {
  const v = judge(liveFixture({ source: 'prometheus' }));
  assert.equal(v.accepted, false);
  assert.equal(v.reason, REJECT.UNTRUSTED_SOURCE);
  assert.equal(Object.values(v.metrics).every((m) => !m.live), true);
});

test('07b readonly must be true, and version must be the integer 1', () => {
  assert.equal(judge(liveFixture({ readonly: false })).reason, REJECT.NOT_READONLY);
  assert.equal(judge(liveFixture({ readonly: undefined })).reason, REJECT.NOT_READONLY);
  assert.equal(judge(liveFixture({ version: 2 })).reason, REJECT.WRONG_VERSION);
  assert.equal(judge(liveFixture({ version: true })).reason, REJECT.WRONG_VERSION,
    'a boolean is not the number 1, however JS coerces');
});

test('08 samples older than the threshold are UNKNOWN, not "old but fine"', () => {
  const payload = liveFixture();
  const old = iso(NOW - MAX_SAMPLE_AGE_MS - 1);
  for (const key of Object.keys(payload.metrics)) payload.metrics[key].sample_at = old;
  assert.deepEqual(judge(payload).metricReasons, [REJECT.STALE_SAMPLE_AT]);
  assert.equal(judge(payload).anyLive, false);

  const atLimit = liveFixture();
  for (const key of Object.keys(atLimit.metrics)) atLimit.metrics[key].sample_at = iso(NOW - MAX_SAMPLE_AGE_MS);
  assert.equal(judge(atLimit).anyLive, true, 'exactly at the limit is still a reading');
});

test('09 the offline shape is a complete, accepted, all-unknown body', () => {
  const payload = liveFixture({ state: 'unknown' });
  for (const key of Object.keys(payload.metrics)) {
    payload.metrics[key] = { state: 'unknown', value: null, unit: payload.metrics[key].unit, reason: 'offline' };
  }
  const v = judge(payload);
  assert.equal(v.accepted, true, 'offline is a fact about the host, not a broken response');
  assert.equal(v.anyLive, false);
  assert.deepEqual(v.metricReasons, ['offline']);
});

test('10 a 404 body is not a projection at all', () => {
  const v = judge({ error: 'not found', status: 404 });
  assert.equal(v.accepted, false);
  assert.equal(v.reason, REJECT.BAD_SHAPE);
  assert.deepEqual(v.metricReasons, [REJECT.BAD_SHAPE]);
  for (const bad of [null, undefined, 'nope', 42, []]) {
    assert.equal(judge(bad).accepted, false, `${String(bad)} must not be accepted`);
    assert.equal(judge(bad).anyLive, false);
  }
});

// --- 2. the other fail-open edges -------------------------------------------

test('an unparsable or missing observed_at rejects the whole envelope', () => {
  for (const stamp of [undefined, null, '', '   ', 'yesterday', 0, {}, '2026-13-45T00:00:00Z']) {
    const v = judge(liveFixture({ observed_at: stamp }));
    assert.equal(v.accepted, false, `observed_at=${JSON.stringify(stamp)} must not be accepted`);
    assert.equal(v.anyLive, false);
    assert.equal(v.reason, REJECT.BAD_OBSERVED_AT);
  }
});

test('a timezone-naive timestamp is refused on both clocks', () => {
  // `Date.parse('2026-10-16T11:59:52')` reads as *local* time, so accepting a
  // naive string would make freshness depend on the reader's timezone.
  assert.equal(parseInstant('2026-10-16T11:59:52'), null);
  assert.equal(parseInstant('2026-10-16T11:59:52.123'), null);
  assert.equal(parseInstant('2026-10-16'), null);
  assert.equal(judge(liveFixture({ observed_at: '2026-10-16T11:59:56' })).reason, REJECT.BAD_OBSERVED_AT);

  const naiveSample = liveFixture();
  naiveSample.metrics.strata_up.sample_at = '2026-10-16T11:59:52';
  assert.equal(judge(naiveSample).metrics.strata_up.reason, REJECT.BAD_SAMPLE_AT);

  // …and the offset-bearing spellings it must still accept.
  for (const good of ['2026-10-16T11:59:52Z', '2026-10-16T11:59:52z', '2026-10-16T11:59:52+00:00',
    '2026-10-16T11:59:52.123Z', '2026-10-16T19:59:52+08:00']) {
    assert.equal(Number.isFinite(parseInstant(good)), true, `${good} must parse`);
  }
  assert.equal(parseInstant('2026-10-16T19:59:52+08:00'), Date.parse('2026-10-16T11:59:52Z'));
});

test('an observed_at in the future rejects the envelope', () => {
  const v = judge(liveFixture({ observed_at: iso(NOW + 10 * 60_000) }));
  assert.equal(v.accepted, false);
  assert.equal(v.reason, REJECT.FUTURE_OBSERVED_AT);
});

test('a missing metric key is reported as a contract gap, not as "no data"', () => {
  const payload = liveFixture();
  delete payload.metrics.gpu_power_watts;
  const v = judge(payload);
  assert.equal(v.anyLive, true, 'the keys that are present still read');
  assert.equal(v.metrics.gpu_power_watts.reason, REJECT.UNKNOWN_METRIC_KEY);
  assert.deepEqual(v.metricReasons, [REJECT.UNKNOWN_METRIC_KEY]);
});

test('unit, state-value and finiteness are contract violations, not style', () => {
  const unit = liveFixture();
  unit.metrics.gpu_temperature_celsius.unit = 'ratio';
  assert.equal(judge(unit).metrics.gpu_temperature_celsius.reason, REJECT.UNIT_MISMATCH);

  const state = liveFixture();
  state.metrics.strata_up.value = 0.4;
  assert.equal(judge(state).metrics.strata_up.reason, REJECT.BAD_STATE_VALUE);

  const nonFinite = liveFixture();
  nonFinite.metrics.gpu_power_watts.value = Number.POSITIVE_INFINITY;
  assert.equal(judge(nonFinite).metrics.gpu_power_watts.reason, REJECT.NON_FINITE_VALUE);
});

test('a service "up" must be a real boolean, and is gated by the panel verdict', () => {
  const stringy = liveFixture();
  stringy.services.gateway = { state: 'known', up: 'false', reason: null };
  assert.equal(judge(stringy).services.gateway.live, false,
    '"false" is truthy in JS; reading it would silently turn down into up');

  // A trusted envelope whose metrics cannot be dated: the probe result is only
  // as good as the snapshot it came in, so it must not claim UP.
  const undatable = liveFixture();
  for (const key of Object.keys(undatable.metrics)) delete undatable.metrics[key].sample_at;
  const body = hostStatusBody(undatable, { now: NOW });
  assert.doesNotMatch(body, />UP</);
  assert.equal(validateHostSummary(undatable, { now: NOW }).services.gateway.live, true,
    'the probe itself is fine — it is the panel that must withhold it');
});

test('an unreadable panel explains itself once, not on every tile', () => {
  // Issue #44's other complaint: a wall of repeated "UNKNOWN" is not an empty
  // state, it is noise. The long explanation belongs in the header chip; the
  // tiles carry the short, greppable code.
  const undatable = liveFixture();
  for (const key of Object.keys(undatable.metrics)) delete undatable.metrics[key].sample_at;
  const body = hostStatusBody(undatable, { now: NOW });
  assert.equal((body.match(/原因 /g) ?? []).length, 1, 'the explanation is stated once');
  assert.equal((body.match(/缺少 sample_at，无法判定是否当前/g) ?? []).length, 1);
  assert.equal((body.match(/<span class="mono">missing-sample-at<\/span>/g) ?? []).length, 11,
    'six metrics plus five services carry only the code');

  // But when the panel is live and one card is the exception, the sentence
  // belongs on that card.
  const partial = liveFixture();
  partial.metrics.gpu_power_watts = { state: 'unknown', value: null, unit: 'watts', reason: 'no-sample' };
  const partialBody = hostStatusBody(partial, { now: NOW });
  assert.match(partialBody, /data-host-metric="gpu_power_watts"[\s\S]*?上游无样本/);
  assert.doesNotMatch(partialBody, /原因 /, 'a live panel carries no failure chip');
});

test('the failure reason vocabulary is the contract\'s, and a stray reason collapses', () => {
  const stray = liveFixture();
  stray.metrics.gpu_power_watts = { state: 'unknown', value: null, unit: 'watts', reason: 'because I said so' };
  assert.equal(judge(stray).metricReasons[0], REJECT.UNKNOWN,
    'a free-text reason would be an uncontrolled rendering channel');
  const body = hostStatusBody(stray, { now: NOW });
  assert.doesNotMatch(body, /because I said so/);
});

// --- 3. freshness, re-checked rather than remembered ------------------------

test('age is recomputed on every call, so a snapshot expires without a new byte', () => {
  const payload = liveFixture();
  assert.equal(describeFreshness(payload, { now: NOW }).state, DISPLAY.LIVE);
  assert.equal(describeFreshness(payload, { now: NOW + 10_000 }).state, DISPLAY.LIVE);
  const later = describeFreshness(payload, { now: NOW + MAX_SAMPLE_AGE_MS + 1_000 });
  assert.equal(later.state, DISPLAY.UNKNOWN);
  assert.equal(later.reason, REJECT.STALE_SAMPLE_AT);
});

test('liveProjection carries only what was verified, with age recomputed', () => {
  const payload = liveFixture();
  payload.metrics.gpu_power_watts = { state: 'unknown', value: null, unit: 'watts', reason: 'no-sample' };
  const v = judge(payload);
  const projection = liveProjection(payload, v, { now: NOW });
  assert.equal(projection.source, 'prometheus-readonly');
  assert.equal(projection.synthetic, false);
  assert.equal(projection.readonly, true);
  assert.deepEqual(Object.keys(projection.metrics).sort(), v.liveKeys);
  assert.equal(projection.metrics.gpu_utilization_ratio.age_s, 8);
  assert.equal(projection.services.gateway.up, true);
  assert.equal('evalscope' in projection.services, false, 'a non-live service is not projected');
  assert.equal(liveProjection(payload, judge(null)), null);
});

// --- 4. the refresh machine (review P0 #3) ----------------------------------

const element = (extra = {}) => {
  const node = {
    dataset: {}, attributes: {}, style: {}, hidden: false, innerHTML: '', children: [],
    parentElement: null, disabled: false, title: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    getAttribute(name) { return node.attributes[name] ?? null; },
    hasAttribute: (name) => name in node.attributes,
    addEventListener(type, handler) { (node._l[type] ??= []).push(handler); },
    removeEventListener() {},
    appendChild(child) { child.parentElement = node; node.children.push(child); return child; },
    insertBefore(child) { node.children.push(child); child.parentElement = node; return child; },
    querySelector: () => null,
    querySelectorAll: () => [],
    _l: {},
    ...extra,
  };
  return node;
};

/** A document with the one view the panel mounts into, plus a captured body. */
function hostDoc() {
  const body = element();
  const hero = element();
  const overview = element({ querySelector: (sel) => (sel === '.hero' ? hero : null) });
  const doc = element({
    documentElement: element(),
    createElement: (tag) => element({ tagName: tag }),
    querySelector: (sel) => {
      if (sel === '#view-overview') return overview;
      if (sel === '#view-compute') return null;
      if (sel === '[data-host-status-body="overview"]') return body;
      return null;
    },
    querySelectorAll: () => [],
  });
  return { doc, body, overview };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

/**
 * Mount a panel over a mutable clock with captured timers.
 *
 * `h.clock` is the test-driven wall clock; `h.tick()` fires one interval; the
 * pending request deadlines land in `h.deadline` so a hung fetch can be timed
 * out deterministically rather than by waiting.
 */
function mountPanel(overrides = {}) {
  const { doc, body } = hostDoc();
  const h = {
    doc, body, clock: NOW,
    tick: null, deadline: null, deadlineMs: null, intervalMs: null,
  };
  const mounted = mountHostStatus({
    doc,
    now: () => h.clock,
    setIntervalFn: (fn, ms) => { h.tick = fn; h.intervalMs = ms; return 1; },
    clearIntervalFn: () => {},
    setTimeoutFn: (fn, ms) => { h.deadline = fn; h.deadlineMs = ms; return 1; },
    clearTimeoutFn: () => {},
    ...overrides,
  });
  return { h, mounted };
}

test('a hung request loses its deadline, its in-flight slot, and the LIVE badge', async () => {
  let hanging = true;
  const { h, mounted } = mountPanel({
    fetchImpl: () => (hanging
      ? new Promise(() => {}) // never settles
      : Promise.resolve({ ok: true, json: async () => liveFixture() })),
  });
  await flush();
  assert.equal(mounted.inFlight, 1, 'the hung request holds the only in-flight slot');
  assert.equal(mounted.state, DISPLAY.UNKNOWN, 'nothing was applied, so nothing is live');
  assert.equal(mounted.reason, REASON.NOT_FETCHED);

  // The deadline is the whole point: without it, `inFlight` stays pinned and
  // every later tick is a silent no-op.
  h.deadline();
  await flush();
  assert.equal(mounted.inFlight, 0, 'the deadline released the slot');
  assert.equal(mounted.reason, REASON.REQUEST_FAILED);

  // Now the source recovers and a fresh tick can actually reach the wire.
  hanging = false;
  h.clock += 31_000;
  h.tick();
  await flush();
  assert.equal(mounted.state, DISPLAY.LIVE, 'a released slot must be reusable');
});

test('a request in flight is never overlapped, and its deadline is bounded', async () => {
  const { h, mounted } = mountPanel({
    intervalMs: 1_000,
    timeoutMs: 5_000,
    fetchImpl: () => new Promise(() => {}),
  });
  await flush();
  assert.equal(mounted.sentSeq, 1);
  assert.equal(h.deadlineMs, 5_000, 'every request carries a bound');

  h.clock += 10_000;
  h.tick();
  h.tick();
  await flush();
  assert.equal(mounted.sentSeq, 1, 'a slow response must not be stacked on');
  assert.equal(mounted.inFlight, 1);
});

test('while a request hangs, the snapshot on screen still expires (rule 4)', async () => {
  let mode = 'fast';
  const { h, mounted } = mountPanel({
    intervalMs: 30_000,
    fetchImpl: () => (mode === 'fast'
      ? Promise.resolve({ ok: true, json: async () => liveFixture() })
      : new Promise(() => {})),
  });
  await flush();
  assert.equal(mounted.state, DISPLAY.LIVE);

  // The tab is left alone long enough for the sample to age out, and the next
  // request hangs. The old LIVE numbers must not survive either fact.
  mode = 'hang';
  h.clock += MAX_SAMPLE_AGE_MS + 5_000;
  h.tick();
  await flush();
  assert.equal(mounted.state, DISPLAY.UNKNOWN, 'an aged snapshot is not live');
  assert.equal(mounted.reason, REASON.EXPIRED);
  assert.equal(mounted.inFlight, 1, 'the refresh is still pending');
  assert.match(h.body.innerHTML, /UNKNOWN · NO SOURCE/);
  assert.doesNotMatch(h.body.innerHTML, /LIVE · Prometheus verified/);
});

test('a hidden tab fires nothing; a visible one fetches at once when due', async () => {
  let calls = 0;
  const { h, mounted } = mountPanel({
    intervalMs: 30_000,
    fetchImpl: async () => { calls += 1; return { ok: true, json: async () => liveFixture() }; },
  });
  await flush();
  assert.equal(calls, 1);

  h.doc.hidden = true;
  h.clock += 60_000;
  h.tick();
  await flush();
  assert.equal(calls, 1, 'a hidden tab must not poll the host');

  h.doc.hidden = false;
  h.tick();
  await flush();
  assert.equal(calls, 2, 'a visible tab with an elapsed interval fetches at once');

  // …and not again before the interval is up.
  h.tick();
  await flush();
  assert.equal(calls, 2, 'the interval still bounds the poll rate');
  assert.equal(mounted.state, DISPLAY.LIVE);
});

test('a late response cannot overwrite a newer one (rule 6)', async () => {
  const pending = [];
  const { h, mounted } = mountPanel({
    intervalMs: 1_000,
    fetchImpl: () => new Promise((resolve) => { pending.push(resolve); }),
  });
  await flush();
  assert.equal(mounted.sentSeq, 1);

  // The first request times out and frees the slot; the second is issued.
  h.deadline();
  await flush();
  h.clock += 2_000;
  h.tick();
  await flush();
  assert.equal(mounted.sentSeq, 2);

  // Now the *first* request finally answers, after the second was sent.
  pending[0]({ ok: true, json: async () => liveFixture() });
  await flush();
  assert.equal(mounted.state, DISPLAY.UNKNOWN, 'seq 1 must not be applied once seq 2 is out');

  // And the answer that belongs to the newest request is applied.
  pending[1]({ ok: true, json: async () => liveFixture() });
  await flush();
  assert.equal(mounted.state, DISPLAY.LIVE);
});

test('a local clock rollback clears the value and forces a refetch', async () => {
  let calls = 0;
  const { h, mounted } = mountPanel({
    intervalMs: 30_000,
    fetchImpl: async () => { calls += 1; return { ok: true, json: async () => liveFixture() }; },
  });
  await flush();
  assert.equal(mounted.state, DISPLAY.LIVE);

  h.clock -= 5 * 60_000; // the clock goes backwards
  h.tick();
  await flush();
  assert.equal(calls, 2, 'a rewound clock is not a reason to keep the old number');
  // The forced refetch answers with a body stamped in the *future* relative to
  // the rewound clock, which is itself fail-closed.
  assert.equal(mounted.state, DISPLAY.UNKNOWN);
  assert.match(h.body.innerHTML, /UNKNOWN · NO SOURCE/);
});

test('a manual reload joins an in-flight request instead of racing it (rule 1)', async () => {
  let calls = 0;
  const { mounted } = mountPanel({
    autoRefresh: false,
    fetchImpl: () => { calls += 1; return new Promise(() => {}); },
  });
  await flush();
  assert.equal(calls, 1);
  mounted.refresh();
  mounted.refresh();
  await flush();
  assert.equal(calls, 1, 'two reloads must not become two requests');
});

// --- 5. conformance against the shared fixture set (PR #53) -----------------

/** The fixtures are owned by Lane Data. This file asserts them when they are on
 *  the branch; `HOST_SUMMARY_FIXTURES` points at a scratch copy for local
 *  verification before that PR merges. */
function fixtureDir() {
  for (const candidate of [process.env.HOST_SUMMARY_FIXTURES, join(REPO, 'tests', 'observability', 'fixtures', 'host_summary')]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  return null;
}

const DIR = fixtureDir();
const SKIP = DIR ? false
  : 'tests/observability/fixtures/host_summary/ is not on this branch yet (Lane Data PR #53)';

test('the shared fixture set agrees with the JS validator', { skip: SKIP }, () => {
  const files = readdirSync(DIR).filter((f) => f.endsWith('.json')).sort();
  assert.ok(files.length >= 10, `expected the ten contract fixtures, found ${files.length}`);
  for (const file of files) {
    const fixture = JSON.parse(readFileSync(join(DIR, file), 'utf8'));
    const at = Date.parse(fixture.now);
    const v = validateHostSummary(fixture.payload, { now: at });
    const expect = fixture.expect;
    const label = `${file}: ${fixture.note}`;

    assert.equal(v.accepted, expect.accepted, `${label} — accepted`);
    assert.equal(v.anyLive, expect.any_live, `${label} — any_live`);
    assert.deepEqual(v.liveKeys, [...expect.live_keys].sort(), `${label} — live_keys`);
    assert.equal(v.accepted ? null : v.reason, expect.envelope_reason, `${label} — envelope_reason`);
    assert.deepEqual(v.metricReasons, [...expect.metric_reasons].sort(), `${label} — metric_reasons`);

    // The badge is the thing a reader sees, so assert it too.
    const body = hostStatusBody(fixture.payload, { now: at });
    const badge = expect.display_state === DISPLAY.LIVE
      ? /LIVE · Prometheus verified/
      : /UNKNOWN · NO SOURCE/;
    assert.match(body, badge, `${label} — display_state`);
    if (expect.display_state !== DISPLAY.LIVE) {
      assert.doesNotMatch(body, />UP</, `${label} — no liveness off a non-live body`);
    }
  }
});
