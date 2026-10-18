/*! tests/infra/compute-hero.test.js
 *  The Compute hero projection: only a verified, fresh, self-consistent host
 *  summary may turn the four cells into real numbers. Everything else is
 *  UNKNOWN — and zero is a legitimate measurement, not an absence.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { projectHostMetrics, isVerifiedHostSummary, HOST_CONTRACT } from '../../web/js/compute/view.js';

const T = '2026-10-16T03:57:50.000Z';
const metric = (value, over = {}) => ({
  state: 'known', value, unit: 'ratio', source: 'prometheus-readonly',
  metric: 'llmlab_strata_gpu_utilization_ratio', series: 1, age_s: 9, sample_at: T, ...over,
});

const summary = (over = {}) => ({
  version: 1,
  source: 'prometheus-readonly',
  synthetic: false,
  readonly: true,
  state: 'known',
  observed_at: '2026-10-16T03:58:00.000Z',
  metrics: {
    gpu_utilization_ratio: metric(0),
    gpu_memory_used_mib: metric(48884.3, { unit: 'mib', metric: 'llmlab_strata_gpu_memory_used_bytes' }),
    gpu_memory_total_mib: metric(49140, { unit: 'mib', metric: 'llmlab_strata_gpu_memory_total_bytes' }),
  },
  services: {},
  ...over,
});

test('the real Dell host summary drives the three GPU cells and the sample time', () => {
  const out = projectHostMetrics(summary());
  assert.equal(out.mode, 'host-live');
  assert.match(out.badge, /LIVE · Prometheus/);
  assert.equal(out.metrics.gpu_utilization_pct.value, '0%', 'a measured zero is a reading');
  assert.equal(out.metrics.gpu_vram_used_mib.value, '48884.3 MiB');
  assert.equal(out.metrics.gpu_vram_total_mib.value, '49140 MiB');
  assert.equal(out.updatedAt, T, 'the cell time must be the sample time');
  assert.notEqual(out.updatedAt, summary().observed_at, 'never the request observed_at');
});

test('a ratio above 1 is a contract violation, not a percentage', () => {
  const s = summary();
  s.metrics.gpu_utilization_ratio = metric(1.4);
  const out = projectHostMetrics(s);
  assert.equal(out.metrics.gpu_utilization_pct.value, 'UNKNOWN');
  assert.equal(out.metrics.gpu_vram_used_mib.value, '48884.3 MiB', 'the other cells survive');
});

test('used memory above total is refused as a pair', () => {
  const s = summary();
  s.metrics.gpu_memory_used_mib = metric(60000, { unit: 'mib', metric: 'llmlab_strata_gpu_memory_used_bytes' });
  const out = projectHostMetrics(s);
  assert.equal(out.metrics.gpu_vram_used_mib.value, 'UNKNOWN');
  assert.equal(out.metrics.gpu_vram_total_mib.value, 'UNKNOWN');
});

test('a stale metric is UNKNOWN, never a stale number', () => {
  const s = summary();
  s.metrics.gpu_temperature = metric(33);
  s.metrics.gpu_memory_total_mib = metric(49140, { unit: 'mib', age_s: HOST_CONTRACT.maxAgeS + 1 });
  const out = projectHostMetrics(s);
  assert.equal(out.metrics.gpu_vram_total_mib.value, 'UNKNOWN');
});

test('a non-finite, negative or string value is UNKNOWN', () => {
  for (const bad of ['lots', -1, NaN, Infinity, null]) {
    const s = summary();
    s.metrics.gpu_memory_used_mib = metric(bad, { unit: 'mib' });
    const out = projectHostMetrics(s);
    assert.equal(out.metrics.gpu_vram_used_mib.value, 'UNKNOWN', `value ${String(bad)} must be refused`);
  }
});

test('a missing sample time cannot be substituted by the request time', () => {
  const s = summary();
  s.metrics.gpu_memory_used_mib = metric(48884.3, { unit: 'mib', sample_at: '' });
  const out = projectHostMetrics(s);
  assert.equal(out.metrics.gpu_vram_used_mib.value, 'UNKNOWN');
});

test('partial data keeps the cells it can prove', () => {
  const s = summary();
  s.metrics.gpu_memory_total_mib = { state: 'unknown', value: null, reason: 'no-sample' };
  const out = projectHostMetrics(s);
  assert.equal(out.metrics.gpu_utilization_pct.value, '0%');
  assert.equal(out.metrics.gpu_vram_used_mib.value, '48884.3 MiB');
  assert.equal(out.metrics.gpu_vram_total_mib.value, 'UNKNOWN');
});

test('only an exact, non-synthetic, read-only, known contract is a host source', () => {
  assert.equal(isVerifiedHostSummary(summary()), true);
  for (const over of [
    { version: 2 },
    { source: 'prometheus' },
    { synthetic: true },
    { readonly: false },
    { state: 'unknown' },
  ]) {
    const s = summary(over);
    assert.equal(isVerifiedHostSummary(s), false, `${JSON.stringify(over)} must not be a host source`);
    assert.equal(projectHostMetrics(s), null, 'and it must not project');
  }
  assert.equal(projectHostMetrics(null), null);
  assert.equal(projectHostMetrics(undefined), null);
});

test('an all-unknown summary still yields a projection, badged UNKNOWN', () => {
  const s = summary();
  for (const key of Object.keys(s.metrics)) s.metrics[key] = { state: 'unknown', value: null, reason: 'no-sample' };
  const out = projectHostMetrics(s);
  assert.equal(out.mode, 'unverified');
  assert.match(out.badge, /UNKNOWN/);
  assert.equal(out.updatedAt, null);
  for (const m of Object.values(out.metrics)) assert.equal(m.value, 'UNKNOWN');
});
