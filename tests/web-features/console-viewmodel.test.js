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
import { requestsNote } from '../../web/console/js/ui.js';
import {
  percentile, servicesFromOps, modelsFromOps, hourlyFromOps, callsFacts, overviewModel, preflightChecks,
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

test('services: engines first, then owned/all/shared rows, without inventing runtime state', () => {
  // 夹具是 synthetic 的，而 viewmodel 现在明确规定「合成负载不呈现为已部署的
  // 引擎/路由」。这里要验的是**映射**，所以显式关掉 synthetic；synthetic 那条
  // 规则单独在下面断言，别让两件事互相隐身。
  const admin = servicesFromOps({ ...PAYLOADS.admin_private, synthetic: false });
  assert.deepEqual(servicesFromOps(PAYLOADS.admin_private), [],
    'a synthetic payload presents no engines or deployments');
  assert.deepEqual(admin.map((x) => x.kind), ['engine', 'deployment']);
  const [engine, deployment] = admin;
  assert.equal(engine.label, 'engine-alice');
  assert.equal(engine.owner, 'alice', 'owner is the readable name, not the principal id');
  assert.equal(engine.runAs, null, 'no runtime user was observed, so none is shown');
  assert.equal(engine.port, 8000);
  assert.equal(engine.runtime.text, '运行中');
  assert.equal(deployment.label, 'model-alice');
  assert.equal(deployment.runtime.text, '状态未知');
  assert.equal(deployment.vramMiB, null);
  const shared = servicesFromOps({ ...PAYLOADS.grantee_shared, synthetic: false });
  assert.equal(shared.filter((x) => x.sharedToMe).length, 1);
  assert.equal(servicesFromOps({ registry: { adapter_attached: false } }), null);
});

test('services: a runtime Linux user observed at registration is shown as recorded', () => {
  const list = servicesFromOps({ registry: { owned: [], shared: [], engines: [
    { engine_id: 'e1', name: 'qwen', protocol: 'openai_compatible', framework: 'vllm', port: 8000, owner: 'account:aa', owner_name: '张伟', owner_is_self: true, status: 'registered', run_as_user: 'zhang', run_as_uid: 1001 },
    { engine_id: 'e2', name: 'bge', protocol: 'openai_compatible', framework: null, port: 8001, owner: 'account:bb', owner_name: '李娜', owner_is_self: false, status: 'unavailable', run_as_user: null, run_as_uid: 1002 },
  ] } });
  assert.equal(list[0].runAs, 'zhang');
  assert.equal(list[0].engine, 'vllm');
  assert.equal(list[0].runtime.text, '未验证');
  assert.equal(list[1].runAs, 'uid 1002', 'an unnamed uid is still shown, never hidden');
  assert.equal(list[1].engine, 'openai_compatible');
  assert.equal(list[1].runtime.text, '无法访问');
});

test('overview counts a verified engine as running', () => {
  const ops = { registry: { owned: [], shared: [], engines: [
    { engine_id: 'e1', name: 'qwen', protocol: 'openai_compatible', owner: 'account:aa', owner_is_self: true, status: 'verified' },
    { engine_id: 'e2', name: 'bge', protocol: 'openai_compatible', owner: 'account:aa', owner_is_self: true, status: 'unavailable' },
  ] } };
  const m = overviewModel({ ops });
  assert.equal(m.kpis.servicesOnline, 1);
  assert.equal(m.kpis.servicesTotal, 2);
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
  assert.equal(bad.list[1].detail, '监听进程不是由管理员为此账户指定的 Linux 用户运行（实际 uid 1001）');
  const observed = preflightChecks({ all_ok: true, checks: { ownership: { ok: true, detail: 'runtime_observed', evidence: { uid: 1001, linux_user: 'zhang' } } } });
  assert.equal(observed.list[1].detail, '服务由 Linux 用户 zhang 运行，已记录');
  const taken = preflightChecks({ all_ok: false, checks: { ownership: { ok: false, detail: 'target_registered_by_another_account' } } });
  assert.equal(taken.blocked, true);
  assert.equal(taken.list[1].detail, '该地址已被其他账户登记');
  assert.equal(bad.list[0].ok, null, 'an absent check reads as not available, never as passed');
  assert.equal(preflightChecks({}), null);
});

test('models: owner name and weight path come straight from the payload', () => {
  const list = modelsFromOps({ registry: { owned: [], shared: [], models: [
    { model_id: 'm1', name: 'qwen', format: 'hf', owner: 'account:a', owner_name: '张伟', owner_is_self: true, weight_path: '/data/qwen' },
    { model_id: 'm2', name: 'bge', format: null, owner: 'account:b', owner_name: '李娜', owner_is_self: false, weight_path: null },
  ] } });
  assert.deepEqual(list.map((m) => [m.name, m.owner, m.self, m.weightPath]), [['qwen', '张伟', true, '/data/qwen'], ['bge', '李娜', false, null]]);
  assert.equal(modelsFromOps({ registry: { adapter_attached: false } }), null);
});

test('调用量脚注：没有源就说没接，接了就说日志里有什么', () => {
  // 1. 完全没有 gateway_feed —— 这一行部署没接线。
  assert.equal(requestsNote({ requests: null, requestsFeed: null }), '调用数据未接入');

  // 2. 有 feed 但读不到文件：不是「0 次」，是「看不见」。
  assert.match(
    requestsNote({ requests: null, requestsFeed: { readable: false, events: null, inWindow: null } }),
    /读不到/,
  );

  // 3. 读到了，日志是空的。
  assert.match(
    requestsNote({ requests: null, requestsFeed: { readable: true, events: 0, inWindow: 0 } }),
    /为空/,
  );

  // 4. 读到了、窗口内 0 条 —— 必须说「0 次」并交出最近一条的时间作证据。
  const idle = requestsNote({
    requests: null,
    requestsFeed: { readable: true, events: 173, inWindow: 0, lastTs: '2026-10-15T01:37:43Z' },
  });
  assert.match(idle, /^近 24 小时 0 次/);
  assert.match(idle, /日志最近/);

  // 5. 窗口内有调用、但没有一把 key 声明过属主 —— 归不到人，不能假装是自己。
  const unowned = requestsNote({
    requests: null,
    requestsFeed: { readable: true, events: 173, inWindow: 12, keysBound: 0, lastTs: '2026-10-15T01:37:43Z' },
  });
  assert.match(unowned, /12 次/);
  assert.match(unowned, /归不到人/);

  // 6. 有本人数字时不解释来源，照旧报口径。
  assert.equal(requestsNote({ requests: 7, requestsScope: '本人 · 近 24 小时' }), '本人 · 近 24 小时');
});

test('概览视图模型把 gateway_feed 折成 requestsFeed，没有就不编', () => {
  const withFeed = overviewModel({
    ops: {
      synthetic: false,
      metrics: { window: '24h', items: [] },
      gateway_feed: {
        feed: 'gateway-usage-export', readable: true, events: 173, unmapped: 0,
        in_window: 0, last_event_ts: '2026-10-15T01:37:43Z', keys_total: 16, keys_bound: 0,
      },
    },
  });
  assert.deepEqual(withFeed.kpis.requestsFeed, {
    readable: true, events: 173, inWindow: 0, lastTs: '2026-10-15T01:37:43Z',
    unmapped: 0, keysBound: 0, keysTotal: 16,
  });

  const without = overviewModel({ ops: { synthetic: false, metrics: { window: '24h', items: [] } } });
  assert.equal(without.kpis.requestsFeed, null);
});
