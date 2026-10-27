/*! tests/web-features/page-data.test.js — 页面级 ops 缓存与页面生命周期。
 *
 *  ``page-data.js`` 只有一层薄薄的接线：``createOpsCache`` 把 ``loadOps`` 塞进
 *  ``read-cache``，``readOps`` 决定有没有缓存并透传 signal，``pageScope`` 给每个
 *  页面一条自己的生命周期。这里测的是这层接线本身，**不重测** ``read-cache``
 *  （那在 ``read-cache.test.js``）。
 *
 *  测法：``createOpsCache`` 把 ``loadOps`` 写死，没法注入桩 loader；绕开它再造一份
 *  缓存又会把真正的接线测掉。所以只替身 ``globalThis.fetch``，用 fixture 里
 *  通过 ``validateOps`` 的载荷让真实的 ``loadOps`` 跑完，统计发出去的请求次数。
 *  这是依赖最少、又确实经过生产代码路径的一条路。
 */
import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { createOpsCache, readOps, pageScope } from '../../web/console/js/page-data.js';

const OPS = JSON.parse(readFileSync(new URL('./fixtures/bff-ops-payloads.json', import.meta.url), 'utf8')).admin_private;
const realFetch = globalThis.fetch;
const turn = () => new Promise(resolve => setImmediate(resolve));

/** 替换 fetch，返回契约认可的 ops；calls 记录真实发出的请求。 */
function stubOpsFetch() {
  const calls = [];
  globalThis.fetch = async (path) => {
    calls.push(path);
    return new Response(JSON.stringify({ data: { ops: OPS } }), { status: 200 });
  };
  return calls;
}

afterEach(() => { globalThis.fetch = realFetch; });

test('force 绕过缓存 TTL：连续两次强制读取真的读两次', async () => {
  const calls = stubOpsFetch();
  const ctx = { opsCache: createOpsCache() };

  const first = await readOps(ctx, { force: true });
  const second = await readOps(ctx, { force: true });

  assert.equal(first.state, 'ready');
  assert.equal(second.state, 'ready');
  assert.equal(calls.length, 2, 'force 不应命中上一次的缓存');
});

test('同一 revision 内普通读取命中缓存，不重复请求', async () => {
  const calls = stubOpsFetch();
  const ctx = { opsCache: createOpsCache() };

  const first = await readOps(ctx);
  const second = await readOps(ctx);

  assert.equal(first.state, 'ready');
  assert.deepEqual(second, first);
  assert.equal(calls.length, 1, '未过期且 revision 未变时应复用缓存');
});

test('ctx 没有 opsCache 时直接走 loadOps，不报错', async () => {
  const calls = stubOpsFetch();

  const result = await readOps({});

  assert.equal(result.state, 'ready');
  assert.equal(calls.length, 1);
  assert.equal(calls[0], '/api/v2/ops');
});

test('readOps 透传 ctx.signal：在途读取被 abort 后不再返回数据', async () => {
  const calls = [];
  globalThis.fetch = (_path, opts) => new Promise((_resolve, reject) => {
    calls.push(_path);
    opts.signal.addEventListener('abort',
      () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  });
  const ctx = { opsCache: createOpsCache() };
  const lifetime = new AbortController();

  const pending = readOps({ ...ctx, signal: lifetime.signal }, { force: true });
  await turn();                       // 让 loadOps 真正走到 fetch
  lifetime.abort();
  const result = await pending;

  assert.equal(result.state, 'error');
  assert.equal(result.reason, 'aborted');
  assert.equal(calls.length, 1);

  const after = await readOps({ ...ctx, signal: lifetime.signal }, { force: true });
  assert.equal(after.reason, 'aborted');
  assert.equal(calls.length, 1, '已经 abort 的 signal 不应再发请求');
});

test('页面 dispose 之后 readOps 的读取被 abort', async () => {
  const calls = stubOpsFetch();
  const root = {};
  const scoped = pageScope({}, root);
  const ctx = { ...scoped, opsCache: createOpsCache() };

  root.dispose();
  const result = await readOps(ctx, { force: true });

  assert.equal(result.state, 'error');
  assert.equal(result.reason, 'aborted');
  assert.equal(calls.length, 0, 'dispose 之后不应再发请求');
});

test('pageScope 给页面自己的 signal；root.dispose() abort 它并跑 cleanups', () => {
  const parent = new AbortController();
  const root = {};

  const ctx = pageScope({ signal: parent.signal, me: { username: 'fjd' } }, root);

  assert.notEqual(ctx.signal, parent.signal, '页面应拿到自己的 signal');
  assert.equal(ctx.signal.aborted, false);
  assert.equal(ctx.me.username, 'fjd', '其余 ctx 字段应保留');

  let cleaned = 0;
  ctx.onDispose(() => { cleaned += 1; });
  root.dispose();

  assert.equal(ctx.signal.aborted, true);
  assert.equal(cleaned, 1);
  assert.equal(parent.signal.aborted, false, '子页面的生命周期不应影响父级');
});

test('父 ctx 被 abort 时页面跟着 dispose，cleanup 也跑一次', () => {
  const parent = new AbortController();
  const root = {};
  const ctx = pageScope({ signal: parent.signal }, root);
  let cleaned = 0;
  ctx.onDispose(() => { cleaned += 1; });

  parent.abort();

  assert.equal(ctx.signal.aborted, true);
  assert.equal(cleaned, 1);
  root.dispose();
  assert.equal(cleaned, 1, '重复 dispose 不应重复跑 cleanup');
});

test('对已 abort 的父 ctx 调用 pageScope 时立即 dispose', () => {
  const parent = new AbortController();
  parent.abort();
  const root = {};

  const ctx = pageScope({ signal: parent.signal }, root);

  assert.equal(ctx.signal.aborted, true, '应立即 dispose');
  let ran = 0;
  ctx.onDispose(() => { ran += 1; });
  assert.equal(ran, 1, 'onDispose 应立刻执行，而不是等下一次 dispose');
  root.dispose();
  assert.equal(ran, 1);
});
