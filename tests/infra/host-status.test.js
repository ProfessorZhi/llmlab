/*! Security and provenance tests for the pure, host-only UI projection.
 *
 *  The fixtures here follow the projection contract handed down by Lane Data
 *  (`tests/observability/fixtures/host_summary/`, PR #53): an envelope needs
 *  `readonly: true`, and every metric that claims to be `known` needs its **own**
 *  timezone-bearing `sample_at`. The previous version of this file used a body
 *  with neither — and that body is, by the contract, not a live reading. The
 *  boundary cases live in `tests/web-features/host-freshness-contract.test.js`;
 *  this file keeps the rendering, provenance and injection surface.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  hostStatusBody, hostStatusState, formatValue,
} from '../../web/js/infra/host-status.js';

/** The reference wall clock every case is judged against. */
const NOW = Date.parse('2026-10-16T12:00:00Z');
const SAMPLE_AT = '2026-10-16T11:59:52Z'; // 8 s old at NOW
const OBSERVED_AT = '2026-10-16T11:59:56Z'; // 4 s old at NOW

const metric = (value, unit) => ({
  state: 'known', value, unit, source: 'prometheus-readonly',
  metric: 'llmlab_strata', series: 1, age_s: 8, sample_at: SAMPLE_AT,
});

const live = {
  version: 1,
  source: 'prometheus-readonly',
  synthetic: false,
  readonly: true,
  state: 'known',
  observed_at: OBSERVED_AT,
  metrics: {
    gpu_utilization_ratio: metric(0.42, 'ratio'),
    gpu_memory_used_mib: metric(48883.8, 'mib'),
    gpu_memory_total_mib: { ...metric(49140, 'mib'), value: 49140 },
  },
  services: {
    strata: { state: 'known', up: true },
    prometheus: { state: 'known', up: true },
    gateway: { state: 'unknown', up: null, reason: 'offline' },
  },
};

test('real host summary shows Prometheus provenance and percent, not synthetic fixture', () => {
  const body = hostStatusBody(live, { now: NOW });
  assert.match(body, /LIVE · Prometheus verified/);
  assert.match(body, /42/);
  assert.match(body, /%/);
  assert.match(body, /48,?884|48884|48883/);
  assert.match(body, /2026-10-16T11:59:56Z/);
  assert.match(body, /已用/);
  assert.equal(hostStatusState(live, { now: NOW }), 'live');
  assert.doesNotMatch(body, /私人调用|全平台当前请求总量/);
});

test('a synthetic, wrong-version or unverified transport cannot claim LIVE', () => {
  const allUnknown = {
    ...live,
    state: 'unknown',
    metrics: Object.fromEntries(
      Object.entries(live.metrics).map(([k, m]) => [k, { ...m, state: 'unknown', value: null, reason: 'offline' }]),
    ),
  };
  for (const bad of [
    { ...live, synthetic: true },
    { ...live, synthetic: 0 },
    { ...live, version: 2 },
    { ...live, source: 'synthetic-fixture' },
    { ...live, readonly: false },
    { ...live, readonly: undefined },
    allUnknown,
    null,
    {},
  ]) {
    assert.equal(hostStatusState(bad, { now: NOW }), 'unknown', JSON.stringify(bad)?.slice(0, 60));
    assert.match(hostStatusBody(bad, { now: NOW }), /UNKNOWN · NO SOURCE/);
  }
});

test('the LIVE badge needs a live metric, not just a trusted envelope', () => {
  // A trusted envelope whose metrics cannot state when they were sampled: the
  // server calls this accepted-but-nothing-live, and so must the panel.
  const noSampleAt = {
    ...live,
    metrics: Object.fromEntries(
      Object.entries(live.metrics).map(([k, m]) => {
        const { sample_at: _drop, ...rest } = m;
        return [k, rest];
      }),
    ),
  };
  const body = hostStatusBody(noSampleAt, { now: NOW });
  assert.doesNotMatch(body, /LIVE · Prometheus verified/);
  assert.match(body, /UNKNOWN · NO SOURCE/);
  assert.match(body, /缺少 sample_at/);
  assert.doesNotMatch(body, />UP</, 'liveness must not be asserted off a body we cannot date');
});

test('host card refuses stale and invalid numbers rather than rendering zero', () => {
  const card = hostStatusBody({
    ...live,
    metrics: {
      ...live.metrics,
      gpu_utilization_ratio: { state: 'unknown', reason: 'stale', value: null, unit: 'ratio' },
      gpu_temperature_celsius: { state: 'known', value: Number.NaN, unit: 'celsius', sample_at: SAMPLE_AT },
    },
  }, { now: NOW });
  assert.match(card, /样本过期/);
  assert.doesNotMatch(card, /NaN|Infinity/);
  assert.equal(formatValue(NaN), null);
  assert.equal(formatValue(undefined), null);
});

test('untrusted envelope metadata never becomes markup', () => {
  const attack = '<img src=x onerror=alert(1)>';
  const body = hostStatusBody({
    ...live,
    source: attack,
    services: { gateway: { state: 'unknown', up: null, reason: attack } },
  }, { now: NOW });
  // Escaped attributes may contain the inert text "onerror="; only real tags are unsafe.
  assert.doesNotMatch(body, /<img(?:\s|>)/i);
  assert.match(body, /&lt;img/, 'the rejected source is the one echoed field, and it is escaped');
});

test('a hostile reason code collapses to "unknown" instead of being echoed', () => {
  const attack = '<img src=x onerror=alert(1)>';
  const body = hostStatusBody({
    ...live,
    metrics: {
      ...live.metrics,
      gpu_utilization_ratio: { state: 'unknown', value: null, unit: 'ratio', reason: attack },
    },
    services: { gateway: { state: 'unknown', up: null, reason: attack } },
  }, { now: NOW });
  // Reasons are a closed vocabulary: anything outside it becomes `unknown`, so a
  // free-text reason cannot become a rendering channel. The attack text is
  // neither rendered nor escaped-and-rendered — it is replaced outright.
  assert.doesNotMatch(body, /<img(?:\s|>)/i);
  assert.doesNotMatch(body, /&lt;img/);
  assert.doesNotMatch(body, /onerror/i);
  assert.match(body, /data-host-metric="gpu_utilization_ratio"[\s\S]*?<div class="tiny mono">unknown<\/div>/);
  // One unusable metric does not take the readable ones down with it.
  assert.match(body, /LIVE · Prometheus verified/);
  assert.match(body, /48884/);
});

test('an attacker-controlled observed_at or sample_at cannot reach the markup', () => {
  const attack = '"><script>alert(1)</script>';
  const body = hostStatusBody({
    ...live,
    observed_at: attack,
    metrics: { ...live.metrics, gpu_utilization_ratio: { ...live.metrics.gpu_utilization_ratio, sample_at: attack } },
  }, { now: NOW });
  assert.doesNotMatch(body, /<script(?:\s|>)/i);
  // Both are contract violations, so the panel is UNKNOWN and neither string is
  // echoed — an unparsable timestamp is not even a reason worth quoting.
  assert.match(body, /UNKNOWN · NO SOURCE/);
});
