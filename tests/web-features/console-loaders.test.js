/*! tests/web-features/console-loaders.test.js — 整机读数与引擎实时读数的取数契约。
 *
 *  这两条读数的共同点是「读不到也要说得出来」：
 *
 *    1. 引擎没起来、凭据没配 ⇒ ``reachable: false`` 是**合法结果**，页面照实显示；
 *       认不出的 schema 才是契约错误。
 *    2. ``/api/infra/system`` 只认 ``source: 'proc-verified'`` 这份投影；别的 JSON
 *       哪怕长得像资源读数，也不能当成读数画出去。
 *
 *  另钉一条接线：两个页面必须走这里校验过的加载器，而不是绕过它们直接
 *  ``loadData`` —— 否则校验函数只是死代码，页面照样会把坏形状当读数渲染。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { PATHS, getJSON, loadSystem, loadEngineLive } from '../../web/console/js/data.js';

const here = dirname(fileURLToPath(import.meta.url));

test('离开页面时取消仍在等待的读取', async () => {
  const lifetime = new AbortController();
  let requestSignal, finish;
  const request = getJSON('/slow', {
    signal: lifetime.signal,
    fetchImpl: (_path, opts) => {
      requestSignal = opts.signal;
      return new Promise(resolve => { finish = resolve; });
    },
  });
  try {
    lifetime.abort();
    assert.equal(requestSignal.aborted, true);
  } finally {
    finish(new Response('{}'));
    await request;
  }
});
const serviceSource = readFileSync(join(here, '../../web/console/js/service.js'), 'utf8');

function fakeFetch(routes) {
  return async (path) => {
    const [status, body] = routes[path] ?? [404, { error: { code: 'not_found' } }];
    return new Response(JSON.stringify(body), { status });
  };
}

const SYSTEM_OK = {
  data: {
    source: 'proc-verified',
    schema: 'system/1',
    observed_at: '2026-10-23T10:00:00+08:00',
    window_seconds: 1,
    cpu: { cores: 32, utilization_ratio: 0.75 },
    memory: { total_mib: 65536 },
    per_user: [],
  },
};

test('a proc-verified system reading passes through', async () => {
  const r = await loadSystem({ fetchImpl: fakeFetch({ [PATHS.system]: [200, SYSTEM_OK] }) });
  assert.equal(r.state, 'ready');
  assert.equal(r.value.cpu.utilization_ratio, 0.75);
});

test('another JSON that merely looks like a system reading is a contract error', async () => {
  const r = await loadSystem({
    fetchImpl: fakeFetch({ [PATHS.system]: [200, { data: { cpu: { utilization_ratio: 0 } } }] }),
  });
  assert.equal(r.state, 'error');
  assert.equal(r.reason, 'contract');
  assert.equal(r.value, null, '不能用一份没通过校验的读数');
});

test('a system reading without per_user is refused', async () => {
  const body = { data: { ...SYSTEM_OK.data, per_user: undefined } };
  const r = await loadSystem({ fetchImpl: fakeFetch({ [PATHS.system]: [200, body] }) });
  assert.equal(r.state, 'error');
});

test('an engine that is not reachable is still a legitimate answer', async () => {
  const r = await loadEngineLive({
    fetchImpl: fakeFetch({
      [PATHS.engineLive]: [200, { data: { schema_version: 'engine-live/1', reachable: false, reason: 'engine_down' } }],
    }),
  });
  assert.equal(r.state, 'ready', '引擎没起来是要显示的现状，不是取数失败');
  assert.equal(r.value.reachable, false);
});

test('an unrecognised engine-live schema is a contract error', async () => {
  const r = await loadEngineLive({
    fetchImpl: fakeFetch({ [PATHS.engineLive]: [200, { data: { reachable: true } }] }),
  });
  assert.equal(r.state, 'error');
  assert.equal(r.reason, 'contract');
});

test('a deployment without the engine-live route says NOT CONFIGURED', async () => {
  const r = await loadEngineLive({ fetchImpl: fakeFetch({}) });
  assert.equal(r.state, 'not_configured');
});

test('the service page uses the validated engine loader', () => {
  assert.match(serviceSource, /loadEngineLive\(\{ engineId, signal: ctx.signal \}\)/);
  assert.doesNotMatch(serviceSource, /loadData\(PATHS\.engineLive\)/);
});
