/*! Ops per-source display leases. No browser or producer required. */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  opsMetricsForDisplay, nextOpsMetricExpiry, parseOpsExpiry, validateOps,
} from '../../web/js/features/contract.js';
import { renderOpsStrip } from '../../web/js/features/render.js';
import { createOps } from '../../web/js/features/controller.js';
import { createMockHttp } from '../../web/js/features/mock-fixture.js';

const NOW = Date.parse('2026-10-20T12:00:00Z');
const expiration = (ms) => new Date(ms).toISOString();
const metric = (name, value) => ({
  name, kind: 'observability', label: name, unit: '次',
  value, state: 'available', source: 'gateway-usage-export',
  updated_at: '2026-10-20T11:59:55Z', reason: null,
});
const sample = (leaseAt = NOW + 55_000) => ({
  transport: { mode: 'live' },
  metrics: {
    items: [metric('principal.requests', 42), metric('principal.tokens', 84)],
    expires_at: {
      'principal.requests': expiration(leaseAt),
      'principal.tokens': expiration(leaseAt + 60_000),
    },
  },
});
const emptyDoc = () => ({
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => { throw new Error('no panel should mount'); },
});

test('a fresh authoritative metric lease permits the measured value', () => {
  const raw = sample();
  const shown = opsMetricsForDisplay(raw, NOW);
  assert.equal(shown.metrics.items[0].value, 42);
  assert.equal(shown.metrics.items[1].value, 84);
  assert.equal(nextOpsMetricExpiry(raw, NOW), NOW + 55_000);
});

test('the earliest metric expires independently; no silent conversion to zero', () => {
  const raw = sample();
  const shown = opsMetricsForDisplay(raw, NOW + 55_000);
  assert.equal(shown.metrics.items[0].value, null);
  assert.equal(shown.metrics.items[0].state, 'unknown');
  assert.equal(shown.metrics.items[0].source, null);
  assert.equal(shown.metrics.items[0].updated_at, null);
  assert.equal(shown.metrics.items[0].reason, 'stale_sample');
  assert.equal(shown.metrics.items[1].value, 84);
  assert.equal(raw.metrics.items[0].value, 42, 'source snapshot must remain immutable');
  assert.equal(nextOpsMetricExpiry(raw, NOW + 55_000, new Set(['principal.requests'])), NOW + 115_000);
  assert.match(renderOpsStrip(shown), /STALE/);
  assert.doesNotMatch(renderOpsStrip(shown), /<strong>LIVE<\/strong>/);
});

test('missing, naive, invalid or expired deadlines mask live values', () => {
  for (const bad of [undefined, '2026-10-20T12:05:00', 'garbage', expiration(NOW - 1)]) {
    const raw = sample();
    if (bad === undefined) delete raw.metrics.expires_at['principal.requests'];
    else raw.metrics.expires_at['principal.requests'] = bad;
    const shown = opsMetricsForDisplay(raw, NOW);
    assert.equal(shown.metrics.items[0].value, null, String(bad));
  }
  assert.equal(parseOpsExpiry('2026-10-20T11:59:55'), null);
  assert.equal(parseOpsExpiry('garbage'), null);
});

test('an expired metric never resurrects on clock rollback within one session', () => {
  const raw = sample();
  const remembered = new Set();
  assert.equal(opsMetricsForDisplay(raw, NOW + 60_000, remembered).metrics.items[0].value, null);
  assert.equal(opsMetricsForDisplay(raw, NOW - 60_000, remembered).metrics.items[0].value, null);
});

test('QA demo payload keeps its intentionally synthetic values', () => {
  const raw = sample(NOW - 1);
  raw.transport.mode = 'demo';
  assert.equal(opsMetricsForDisplay(raw, NOW).metrics.items[0].value, 42);
  assert.equal(nextOpsMetricExpiry(raw, NOW), null);
});

test('validateOps rejects malformed expiry metadata but accepts valid optional map', () => {
  const fixture = createMockHttp({persona:'sharer'}).ops();
  fixture.metrics.expires_at = {'principal.requests': expiration(NOW + 60_000)};
  assert.equal(validateOps(fixture).ok, false, 'a lease cannot refer to a missing metric');
  fixture.metrics.expires_at = {'gateway_requests_24h': expiration(NOW + 60_000)};
  assert.equal(validateOps(fixture).ok, true);
  fixture.metrics.expires_at.gateway_requests_24h = '2026-10-20T12:01:00';
  assert.equal(validateOps(fixture).ok, false, 'naive timestamp cannot pass');
});

test('an idle live controller masks a displayed value when its source lease expires', async () => {
  const ops = createMockHttp({persona: 'sharer'}).ops();
  ops.transport = { mode: 'live', synthetic: false, gateway_attached: true, label: 'LIVE' };
  ops.synthetic = false;
  ops.metrics.items = [metric('principal.requests', 42)];
  ops.metrics.expires_at = {'principal.requests': expiration(Date.now() + 50)};
  const seen = [];
  const controller = createOps({
    doc: emptyDoc(),
    source: { mode: 'live', load: async () => ({state: 'ready', ops}) },
    onStateChange: ({ops: displayed}) => seen.push(displayed?.metrics?.items?.[0]?.value),
  });
  await controller.load();
  assert.equal(controller.ops.metrics.items[0].value, 42);
  await new Promise((resolve) => setTimeout(resolve, 125));
  assert.equal(controller.ops.metrics.items[0].value, null);
  assert.ok(seen.includes(null), 'an idle tab must repaint without a new fetch');
  controller.invalidate();
});

test('an idle metric expires even when the wall clock rolls back before the deadline', async () => {
  const realNow = Date.now;
  const initial = realNow();
  const ops = createMockHttp({persona: 'sharer'}).ops();
  ops.transport = { mode: 'live', synthetic: false, gateway_attached: true, label: 'LIVE' };
  ops.synthetic = false;
  ops.metrics.items = [metric('principal.requests', 42)];
  ops.metrics.expires_at = {'principal.requests': expiration(initial + 100)};
  const seen = [];
  const controller = createOps({
    doc: emptyDoc(),
    source: { mode: 'live', load: async () => ({state: 'ready', ops}) },
    onStateChange: ({ops: displayed}) => seen.push(displayed?.metrics?.items?.[0]?.value),
  });
  try {
    await controller.load();
    assert.equal(controller.ops.metrics.items[0].value, 42);
    // Changing the wall clock must not postpone the already-scheduled
    // monotonic timeout. A stale number must never become LIVE again.
    Date.now = () => initial - 60 * 60 * 1000;
    await new Promise((resolve) => setTimeout(resolve, 210));
    assert.equal(controller.ops.metrics.items[0].value, null);
    assert.ok(seen.includes(null), 'the idle UI must repaint a stale value');
  } finally {
    Date.now = realNow;
    controller.invalidate();
  }
});
