// Console network layer: CSRF handling and the account → local-admin login order.
import test from 'node:test';
import assert from 'node:assert/strict';

import { login, send, rememberCsrf, PATHS } from '../../web/console/js/data.js';

function fakeFetch(routes) {
  const calls = [];
  let n = 0;
  const impl = async (path, init = {}) => {
    calls.push({ path, method: init.method ?? 'GET', csrf: init.headers?.['X-CSRF-Token'] ?? null });
    if (path === PATHS.csrf) {
      n += 1;
      return new Response(JSON.stringify({ ok: true, data: { csrf_token: `anon-${n}` } }), { status: 200 });
    }
    const [status, body] = routes[path] ?? [404, { ok: false, error: { code: 'not_found' } }];
    return new Response(JSON.stringify(body), { status });
  };
  return { impl, calls };
}

test('a failed write drops the cached single-use CSRF token', async () => {
  rememberCsrf(null);
  const f = fakeFetch({ [PATHS.login]: [401, { ok: false, error: { code: 'invalid_credentials' } }] });
  await send(PATHS.login, { username: 'a', password: 'b' }, { fetchImpl: f.impl });
  await send(PATHS.login, { username: 'a', password: 'c' }, { fetchImpl: f.impl });
  const tokens = f.calls.filter((c) => c.path === PATHS.login).map((c) => c.csrf);
  assert.deepEqual(tokens, ['anon-1', 'anon-2']);
});

test('login tries the platform account first and stops there on success', async () => {
  rememberCsrf(null);
  const f = fakeFetch({ [PATHS.login]: [200, { ok: true, data: { user: { id: 'account:1' }, csrf_token: 'sess' } }] });
  const r = await login('zhang', 'pw', { fetchImpl: f.impl });
  assert.equal(r.ok, true);
  assert.equal(r.data.user.id, 'account:1');
  assert.ok(!f.calls.some((c) => c.path === PATHS.localLogin));
});

test('a rejected account login falls back to the local administrator', async () => {
  rememberCsrf(null);
  const f = fakeFetch({
    [PATHS.login]: [401, { ok: false, error: { code: 'invalid_credentials' } }],
    [PATHS.localLogin]: [200, { ok: true, data: { user: { id: 'local:x', role: 'platform_admin' }, csrf_token: 'sess' } }],
  });
  const r = await login('admin', 'pw', { fetchImpl: f.impl });
  assert.equal(r.ok, true);
  assert.equal(r.data.user.role, 'platform_admin');
  const local = f.calls.find((c) => c.path === PATHS.localLogin);
  assert.equal(local.csrf, 'anon-2', 'fallback must use a fresh anonymous token');
});

test('an unconfigured local admin does not mask the account answer', async () => {
  rememberCsrf(null);
  const f = fakeFetch({
    [PATHS.login]: [401, { ok: false, error: { code: 'invalid_credentials' } }],
    [PATHS.localLogin]: [503, { ok: false, error: { code: 'not_configured' } }],
  });
  const r = await login('nobody', 'pw', { fetchImpl: f.impl });
  assert.equal(r.status, 401);
});

test('rate limiting on the account route is reported without a second attempt', async () => {
  rememberCsrf(null);
  const f = fakeFetch({ [PATHS.login]: [429, { ok: false, error: { code: 'rate_limited' } }] });
  const r = await login('zhang', 'pw', { fetchImpl: f.impl });
  assert.equal(r.status, 429);
  assert.ok(!f.calls.some((c) => c.path === PATHS.localLogin));
});

test('plugin jump links: only http(s) URLs and site paths become links', async () => {
  const { safeJumpUrl } = await import('../../web/console/js/data.js');
  assert.equal(safeJumpUrl('/grafana/'), '/grafana/');
  assert.equal(safeJumpUrl('https://dell.example/strata/'), 'https://dell.example/strata/');
  for (const bad of ['javascript:alert(1)', '//evil.example/', '/\\evil', 'data:text/html,x', 'https://u:p@h/', '', null, 42]) {
    assert.equal(safeJumpUrl(bad), null, String(bad));
  }
});
