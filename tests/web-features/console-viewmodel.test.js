/*! tests/web-features/console-viewmodel.test.js
 *  The redesigned console (web/console/) draws charts from pure layout and
 *  view-model functions. These pin the honesty rules those functions carry:
 *  a missing reading stays missing (never 0), a synthetic series never reaches a
 *  live chart, and the demo gate is the same one the shell uses.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { sunburstLayout, dotGridLayout, barsLayout, fmtInt, fmtGiB } from '../../web/console/js/charts.js';
import {
  percentile, servicesFromOps, hourlyFromOps, callsFacts, overviewModel, preflightChecks,
} from '../../web/console/js/viewmodel.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const PAYLOADS = JSON.parse(readFileSync(join(REPO, 'tests', 'web-features', 'fixtures', 'bff-ops-payloads.json'), 'utf8'));

test('sunburst: rings partition the total and drop empty or non-finite slices', () => {
  const out = sunburstLayout([
    { id: 'a', label: 'A', children: [{ id: 'm1', label: 'm1', value: 3 }, { id: 'm2', label: 'm2', value: 1 }] },
    { id: 'b', label: 'B', children: [{ id: 'm1', label: 'm1', value: 4 }, { id: 'x', label: 'x', value: null }] },
    { id: 'c', label: 'C', children: [{ id: 'z', label: 'z', value: 0 }] },
  ]);
  assert.equal(out.total, 8);
  assert.deepEqual(out.inner.map((g) => g.id), ['a', 'b']);
  assert.deepEqual(out.inner.map((g) => g.share), [0.5, 0.5]);
  assert.equal(out.outer.length, 3);
  assert.ok(out.outer.every((s) => s.d.startsWith('M')));
  assert.deepEqual(sunburstLayout([]), { total: 0, inner: [], outer: [] });
});

test('dot grid: capacity is fixed by total, unknown occupancy draws no cells', () => {
  const out = dotGridLayout({
    totalMiB: 48 * 1024,
    parts: [{ id: 's1', mib: 18 * 1024, color: '#5856D6' }, { id: 's2', mib: null, color: '#30B0C7' }],
  });
  assert.equal(out.cells.length, 192);
  assert.equal(out.cells.filter((c) => c.id === 's1').length, 72);
  assert.equal(out.cells.filter((c) => c.id === 's2').length, 0);
  assert.equal(out.cells.filter((c) => c.id === null).length, 120);
  assert.equal(out.rows, 8);
  // Over-subscription is clipped to capacity, never extends the grid.
  assert.equal(dotGridLayout({ totalMiB: 1024, parts: [{ id: 'x', mib: 4096, color: '#000' }] }).cells.length, 4);
  assert.deepEqual(dotGridLayout({ totalMiB: null }).cells, []);
});

test('bars: null points leave a gap rather than a zero bar', () => {
  const out = barsLayout([null, 2, null, 4], { width: 100, height: 50, gap: 0 });
  assert.deepEqual(out.bars.map((b) => b.index), [1, 3]);
  assert.equal(out.peak, 3);
  assert.equal(out.bars.find((b) => b.peak).value, 4);
  assert.equal(barsLayout([null, null]).bars.length, 0);
});

test('formatters print a dash for a missing value, not 0', () => {
  assert.equal(fmtInt(null), '—');
  assert.equal(fmtInt(18240), '18,240');
  assert.equal(fmtGiB(undefined), '—');
  assert.equal(fmtGiB(18432), '18.0');
  assert.equal(percentile([], 0.95), null);
  assert.equal(percentile([1, 2, 3, 4, 5], 0.5), 3);
});

test('a synthetic trend series never reaches a live chart', () => {
  for (const [name, ops] of Object.entries(PAYLOADS)) {
    assert.equal(ops.trends.synthetic, true, `${name} fixture is the synthetic one`);
    assert.equal(hourlyFromOps(ops), null, `${name}: synthetic series must read as not connected`);
  }
  const live = { synthetic: false, trends: { synthetic: false, series: [{ kind: 'requests', points: [1, null, 3] }] } };
  assert.deepEqual(hourlyFromOps(live).points, [1, null, 3]);
});

test('overview from the real BFF payloads: unknown metrics stay null', () => {
  for (const [name, ops] of Object.entries(PAYLOADS)) {
    const m = overviewModel({ ops });
    assert.equal(m.kpis.requests, null, `${name}: no verified request metric`);
    assert.equal(m.kpis.successRate, null, name);
    assert.equal(m.kpis.p95ms, null, name);
    assert.equal(m.kpis.vramTotalMiB, null, name);
    assert.equal(m.vram, null, name);
    assert.equal(m.hourly, null, name);
    assert.ok(Array.isArray(m.services), name);
  }
});

test('services: owned, all and shared rows map without inventing runtime state', () => {
  const admin = servicesFromOps(PAYLOADS.admin_private);
  assert.equal(admin.length, 1);
  assert.equal(admin[0].label, 'model-alice');
  assert.equal(admin[0].runtime.text, '状态未知');
  assert.equal(admin[0].vramMiB, null);
  const shared = servicesFromOps(PAYLOADS.grantee_shared);
  assert.equal(shared.length, 1);
  assert.equal(shared[0].sharedToMe, true);
  assert.equal(servicesFromOps({ registry: { adapter_attached: false } }), null);
});

test('verified principal metrics feed the KPIs with their real names and units', () => {
  const item = (name, value) => ({ name, value, state: 'available' });
  const m = overviewModel({
    ops: {
      synthetic: false,
      metrics: {
        window: '24h',
        items: [
          item('principal.requests', 200), item('principal.errors', 4), item('principal.latency_p95_ms', 1620),
          item('device.gpu.memory.total_bytes', 48 * 1024 ** 3), item('device.gpu.memory.used_bytes', 12 * 1024 ** 3),
        ],
      },
      registry: { adapter_attached: true, all: [], owned: [], shared: [] },
    },
  });
  assert.equal(m.kpis.requests, 200);
  assert.equal(m.kpis.requestsScope, '本人 · 近 24 小时');
  assert.equal(m.kpis.successRate, 0.98);
  assert.equal(m.kpis.p95ms, 1620);
  assert.equal(m.kpis.vramTotalMiB, 48 * 1024);
  assert.equal(m.vram.parts.length, 1, 'unattributed usage is shown as one honest bucket');
});

test('call log: distribution groups by key then model, success rate counts outcomes', () => {
  const row = (key, model, outcome, ms) => ({ call_id: `${key}${model}${ms}`, ts: `2026-10-21T06:${String(ms).padStart(2, '0')}:00Z`, key_id: key, model, status: outcome === 'success' ? 200 : 500, outcome, total_ms: ms, tokens: { total: 10 } });
  const f = callsFacts({ calls: [row('k1', 'a', 'success', 10), row('k1', 'b', 'error', 20), row('k2', 'a', 'success', 30)] });
  assert.equal(f.count, 3);
  assert.equal(f.successRate, 2 / 3);
  assert.deepEqual(f.distribution.groups.map((g) => [g.id, g.children.length]), [['k1', 2], ['k2', 1]]);
  assert.deepEqual(f.models.map((m) => [m.id, m.value]), [['a', 2], ['b', 1]]);
  assert.equal(f.recent[0].totalMs, 30);
  assert.equal(callsFacts(null), null);
});

test('console demo gate: index.html is production, only qa.html can honour ?demo=1', () => {
  const index = readFileSync(join(REPO, 'web', 'console', 'index.html'), 'utf8');
  const qa = readFileSync(join(REPO, 'web', 'console', 'qa.html'), 'utf8');
  const main = readFileSync(join(REPO, 'web', 'console', 'js', 'main.js'), 'utf8');
  assert.doesNotMatch(index, /data-build-profile="qa"/);
  assert.match(qa, /data-build-profile="qa"/);
  assert.match(main, /resolveDataMode\(location\.search, \{ qa: isQaBuild\(document\) \}\)/);
  assert.doesNotMatch(main, /get\('demo'\)/, 'no second, ungated reading of ?demo');
});

test('preflight: three-state checks, missing source is not a failure', () => {
  const r = preflightChecks({
    all_ok: false,
    checks: {
      port_reachable: { ok: true, detail: 'connected' },
      ownership: { ok: null, detail: 'no_ownership_source_in_this_deployment' },
      openai_compat: { ok: true, detail: 'model_list_verified' },
      name_unique: { ok: true, detail: 'available' },
    },
  });
  assert.deepEqual(r.list.map((c) => c.ok), [true, null, true, true]);
  assert.equal(r.blocked, false);
  assert.equal(r.allOk, false);
  assert.equal(r.list[1].detail, '本部署未启用归属校验');

  const bad = preflightChecks({ all_ok: false, checks: { ownership: { ok: false, detail: 'owner_mismatch:1001' } } });
  assert.equal(bad.blocked, true);
  assert.equal(bad.list[1].detail, '监听进程属于其他账户（uid 1001）');
  assert.equal(bad.list[0].ok, null, 'an absent check reads as not available, never as passed');
  assert.equal(preflightChecks({}), null);
});
