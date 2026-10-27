import test from 'node:test';
import assert from 'node:assert/strict';
import { createReadCache } from '../../web/console/js/read-cache.js';
const ok = n => ({ state: 'ready', value: n });
const turn = () => new Promise(r => setImmediate(r));

test('coalesces concurrent reads and reuses only an unexpired session snapshot', async () => {
  let calls = 0, time = 0;
  const cache = createReadCache(async () => ok(++calls), { now: () => time, ttlMs: 5000 });
  assert.deepEqual(await Promise.all([cache.read(), cache.read()]), [ok(1), ok(1)]);
  assert.deepEqual(await cache.read(), ok(1));
  time = 5001;
  assert.deepEqual(await cache.read(), ok(2));
  assert.deepEqual(await cache.read({ force: true }), ok(3));
  const other = createReadCache(async () => ok('other session'));
  assert.equal(other.peek(), null);
});
test('one cancelled consumer does not abort another; last consumer aborts underlying work', async () => {
  let aborted = 0, finish;
  const cache = createReadCache(({ signal }) => new Promise(resolve => { finish = resolve; signal.addEventListener('abort', () => { aborted++; resolve({ state: 'error' }); }); }));
  const a = new AbortController(), b = new AbortController();
  const first = cache.read({ signal: a.signal }), second = cache.read({ signal: b.signal });
  await turn(); a.abort(); assert.equal((await first).reason, 'aborted'); assert.equal(aborted, 0);
  finish(ok(1)); assert.deepEqual(await second, ok(1));
  const c = new AbortController(); const last = cache.read({ force: true, signal: c.signal });
  await turn(); c.abort(); await last; assert.equal(aborted, 1);
});
test('mutation/logout invalidation rejects late results and drops previous user data', async () => {
  let revision = 0, finish;
  const cache = createReadCache(() => new Promise(resolve => { finish = resolve; }), { revision: () => revision });
  const pending = cache.read(); await turn(); revision++;
  assert.equal(cache.peek(), null); finish(ok('old')); await pending; assert.equal(cache.peek(), null);
  const next = cache.read(); await turn(); finish(ok('new')); await next; assert.deepEqual(cache.peek(), ok('new'));
  cache.clear(); assert.equal(cache.peek(), null);
});
test('401/403 clear cached identity data; transient refresh failure does not replace last success', async () => {
  let response = ok('private'); const cache = createReadCache(async () => response);
  await cache.read(); response = { state: 'error' }; await cache.read({ force: true }); assert.deepEqual(cache.peek(), ok('private'));
  response = { state: 'signed_out' }; await cache.read({ force: true }); assert.equal(cache.peek(), null);
});
