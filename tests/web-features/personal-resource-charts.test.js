import test from 'node:test';
import assert from 'node:assert/strict';
import { resourceSeries, gpuHierarchy } from '../../web/console/js/resource-charts.js';
import { groupBuilds } from '../../web/console/js/viewmodel.js';
import { loadEngineLive, engineLivePath } from '../../web/console/js/data.js';

test('scatter axes use host CPU percentage and retain unknown measurements', () => {
  const rows = resourceSeries({ cpu: { cores: 8 }, per_user: [
    { user: 'a', account: 'human', cpu_ratio: 2, memory_mib: 1024, vram_mib: null },
    { user: 'b', account: 'human', cpu_ratio: null, memory_mib: 100 },
    { user: 'root', account: 'system', cpu_ratio: 1 },
  ] });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].cpu, 25);
  assert.equal(rows[0].vram_mib, null);
  assert.equal(rows[1].cpu, null);
  assert.equal(resourceSeries({ per_user: [{ account: 'human', cpu_ratio: 3 }] })[0].cpu, null);
});

test('sunburst groups GPU processes by owner with consistent memory units', () => {
  const groups = gpuHierarchy({ gpu_processes: [
    { user: 'alice', pid: 1, used_mib: 1024 }, { user: 'alice', pid: 2, used_mib: 2048 },
    { user: 'bob', pid: 3, used_mib: null }, { user: 'bob', pid: 4, used_mib: NaN },
  ] });
  assert.equal(groups.length, 1);
  assert.equal(groups[0].children.reduce((n, p) => n + p.value, 0), 3072);
});

test('one environment folds package and executable but separate environments and owners remain', () => {
  const row = { framework: 'vllm', ownerId: 'alice', env: null, version: null };
  const rows = groupBuilds([
    { ...row, id: 'bin', foundAs: 'executable', path: '/a/venv/bin/vllm' },
    { ...row, id: 'pkg', foundAs: 'python_package', env: '/a/venv', path: '/a/venv/lib/vllm.dist-info', version: '1.0' },
    { ...row, id: 'other', foundAs: 'executable', path: '/b/venv/bin/vllm', env: '/b/venv' },
    { ...row, id: 'bob', ownerId: 'bob', path: null },
  ]);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].locations.length, 2);
  assert.equal(rows[0].version, '1.0');
});

test('service metrics loader addresses the selected engine, never the old global metrics endpoint', async () => {
  const paths = [];
  const result = await loadEngineLive({ engineId: 'eng_123', fetchImpl: async path => {
    paths.push(path);
    return new Response(JSON.stringify({ data: { schema_version: 'engine-live/1', reachable: false, reason: 'service_offline' } }), { status: 200 });
  } });
  assert.deepEqual(paths, [engineLivePath('eng_123')]);
  assert.equal(result.state, 'ready');
  assert.equal(result.value.reason, 'service_offline');
});
