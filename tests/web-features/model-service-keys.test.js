/*! tests/web-features/model-service-keys.test.js — 模型服务密钥的取数与面板口径。
 *
 *  这一支和网关密钥是两件事：它直连本机引擎、不经过网关。测试因此只钉两样东西：
 *
 *    1. **取数契约。** 清单为空是合法结果；形状不对是契约错误；403 是 denied，
 *       不是「没有密钥」。前端绝不把一次失败画成「还没签发过」。
 *    2. **面板的门。** 只有管理员请求和渲染这块；演示模式不签发真实凭据；
 *       「已签发」与「已生效」分开说 —— 引擎重载失败时，密钥写着但用不了。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { PATHS, loadModelKeys, createModelKey, rememberCsrf } from '../../web/console/js/data.js';

const here = dirname(fileURLToPath(import.meta.url));
const gatewaySource = readFileSync(join(here, '../../web/console/js/gateway.js'), 'utf8');
const dataSource = readFileSync(join(here, '../../web/console/js/data.js'), 'utf8');

function fakeFetch(routes) {
  const calls = [];
  const impl = async (path, init = {}) => {
    calls.push({ path, method: init.method ?? 'GET', body: init.body ?? null, csrf: init.headers?.['X-CSRF-Token'] ?? null });
    if (path === PATHS.csrf) {
      return new Response(JSON.stringify({ ok: true, data: { csrf_token: 'csrf-1' } }), { status: 200 });
    }
    const [status, body] = routes[path] ?? [404, { error: { code: 'not_found' } }];
    return new Response(JSON.stringify(body), { status });
  };
  return { impl, calls };
}

test('a minted key list is read as a list of keys', async () => {
  const f = fakeFetch({
    [PATHS.modelKey]: [200, { data: { key: 'sk-qwen-a', keys: [{ key: 'sk-qwen-a', label: '外部评估' }] } }],
  });
  const r = await loadModelKeys({ fetchImpl: f.impl });
  assert.equal(r.state, 'ready');
  assert.deepEqual(r.value, [{ key: 'sk-qwen-a', label: '外部评估' }]);
});

test('an empty list is a legitimate result, not an error', async () => {
  const f = fakeFetch({ [PATHS.modelKey]: [200, { data: { key: null, keys: [] } }] });
  const r = await loadModelKeys({ fetchImpl: f.impl });
  assert.equal(r.state, 'ready');
  assert.deepEqual(r.value, []);
});

test('a legacy single-key payload is still a list of one', async () => {
  const f = fakeFetch({ [PATHS.modelKey]: [200, { data: { key: 'sk-qwen-legacy', label: '旧' } }] });
  const r = await loadModelKeys({ fetchImpl: f.impl });
  assert.deepEqual(r.value.map((k) => k.key), ['sk-qwen-legacy']);
});

test('a key entry without a key is a contract error, not an empty page', async () => {
  for (const body of [
    { data: { keys: [null] } },
    { data: { keys: [{ label: '没有 key' }] } },
    { data: { keys: 'not-an-array' } },
  ]) {
    const f = fakeFetch({ [PATHS.modelKey]: [200, body] });
    const r = await loadModelKeys({ fetchImpl: f.impl });
    assert.equal(r.state, 'error', JSON.stringify(body));
    assert.equal(r.reason, 'contract');
  }
});

test('a refusal stays a refusal', async () => {
  const f = fakeFetch({ [PATHS.modelKey]: [403, { error: { code: 'bff.access_denied' } }] });
  const r = await loadModelKeys({ fetchImpl: f.impl });
  assert.equal(r.state, 'denied');
  assert.equal(r.reason, 'bff.access_denied');
});

test('minting posts the label with a CSRF token', async () => {
  rememberCsrf(null);
  const f = fakeFetch({
    [PATHS.modelKeys]: [201, { data: { key: 'sk-qwen-new', label: '客户端', restart: { triggered: true } } }],
  });
  const r = await createModelKey('客户端', { fetchImpl: f.impl });
  assert.equal(r.ok, true);
  const post = f.calls.find((c) => c.path === PATHS.modelKeys);
  assert.equal(post.method, 'POST');
  assert.equal(post.csrf, 'csrf-1');
  assert.deepEqual(JSON.parse(post.body), { label: '客户端' });
});

test('a missing label is sent as null, not as an empty string', async () => {
  rememberCsrf(null);
  const f = fakeFetch({ [PATHS.modelKeys]: [201, { data: { key: 'sk-qwen-new' } }] });
  await createModelKey('', { fetchImpl: f.impl });
  const post = f.calls.find((c) => c.path === PATHS.modelKeys);
  assert.deepEqual(JSON.parse(post.body), { label: null });
});

test('legacy credentials require admin and are never fetched in personal mode', () => {
  assert.match(gatewaySource, /if \(!ctx\.isAdmin\(\)\) return null;/);
  assert.match(gatewaySource, /ctx\.isAdmin\(\) && !st\.personalMode \? await loadModelKeys\(\{ signal: ctx.signal \}\)/);
  assert.match(gatewaySource, /modelServicePanel\(ctx, mst, \{ mint: mintModelKey, copy: copyKey \}\)/);
});

test('demo mode refuses to mint a real credential', () => {
  assert.match(gatewaySource, /演示模式不签发真实凭据/);
});

test('minted and in-effect are said separately', () => {
  // 成功：给出等待时间；失败：明说「密钥已写入但重载没有触发」。
  assert.match(gatewaySource, /restart\.triggered\) return '引擎正在重载/);
  assert.match(gatewaySource, /重载没有触发/);
  assert.match(gatewaySource, /在重载成功前它还不可用/);
});

test('a refusal or a missing route is not drawn as "nothing minted yet"', () => {
  // 空清单与读不到是两件事。读不到时用 emptyState 说明原因，并且不摆签发按钮。
  assert.match(gatewaySource, /mst\.state === 'ready'[\s\S]{0,40}\? quietEmpty\('ready'/);
  assert.match(gatewaySource, /: emptyState\(mst\.state\)\)/);
  assert.match(gatewaySource, /mst\.state === 'ready' \? h\('div', \{ class: 'cell'/);
});

test('the panel says this key bypasses the gateway', () => {
  assert.match(gatewaySource, /\*\*不经过网关\*\*/);
});

test('the console declares one path per resource, not two aliases', () => {
  const paths = [...dataSource.matchAll(/^\s{2}\w+: '([^']+)'/gm)].map((m) => m[1]);
  const duplicates = paths.filter((path, index) => paths.indexOf(path) !== index);
  assert.deepEqual(duplicates, [], `重复声明：${duplicates.join(', ')}`);
});
