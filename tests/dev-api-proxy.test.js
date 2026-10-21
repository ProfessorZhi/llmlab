// The opt-in loopback bridge from the static server to a local BFF devserver.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';

import { devApiTarget, forwardedHeaders, isProxiedPath } from '../web/tools/dev-api-proxy.mjs';
import { startServer } from '../web/tools/serve.mjs';

test('the bridge is off without the variable', () => {
  assert.equal(devApiTarget({}), null);
  assert.equal(devApiTarget({ LLMLAB_DEV_API: '' }), null);
});

test('only bare loopback http origins are accepted', () => {
  assert.equal(devApiTarget({ LLMLAB_DEV_API: 'http://127.0.0.1:8124' }).port, '8124');
  assert.ok(devApiTarget({ LLMLAB_DEV_API: 'http://localhost:9000/' }));
  assert.ok(devApiTarget({ LLMLAB_DEV_API: 'http://[::1]:9000' }));
  for (const bad of ['https://127.0.0.1:1', 'http://10.0.0.5:80', 'http://example.com', 'http://127.0.0.1:1/api', 'http://u@127.0.0.1:1', 'nonsense']) {
    assert.throws(() => devApiTarget({ LLMLAB_DEV_API: bad }), /LLMLAB_DEV_API/, bad);
  }
});

test('Host and same-origin Origin/Referer are presented as the target; foreign origins are not laundered', () => {
  const target = new URL('http://127.0.0.1:8790');
  const own = forwardedHeaders({ host: '127.0.0.1:8123', origin: 'http://127.0.0.1:8123', referer: 'http://127.0.0.1:8123/console/' }, target);
  assert.equal(own.host, '127.0.0.1:8790');
  assert.equal(own.origin, 'http://127.0.0.1:8790');
  assert.equal(own.referer, 'http://127.0.0.1:8790/console/');
  const foreign = forwardedHeaders({ host: '127.0.0.1:8123', origin: 'http://evil.example', referer: 'http://127.0.0.1:81234/x' }, target);
  assert.equal(foreign.origin, 'http://evil.example');
  assert.equal(foreign.referer, 'http://127.0.0.1:81234/x');
  const lookalike = forwardedHeaders({ host: '127.0.0.1:8123', origin: 'http://127.0.0.1:8123.evil.example' }, target);
  assert.equal(lookalike.origin, 'http://127.0.0.1:8123.evil.example');
});

test('only /auth/ and /api/ are proxied', () => {
  assert.ok(isProxiedPath('/auth/me'));
  assert.ok(isProxiedPath('/api/v2/ops'));
  assert.ok(!isProxiedPath('/console/index.html'));
  assert.ok(!isProxiedPath('/authx'));
});

function listen(server) {
  return new Promise((ok) => server.listen(0, '127.0.0.1', () => ok(server.address().port)));
}

test('forwards method, body, Host, Origin and cookies, and returns the upstream answer', async () => {
  let seen = null;
  const upstream = createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() };
      res.writeHead(201, { 'content-type': 'application/json', 'set-cookie': 'llm_lab_session=abc; HttpOnly' });
      res.end('{"ok":true}');
    });
  });
  const upPort = await listen(upstream);
  const web = await startServer({ env: { LLMLAB_DEV_API: `http://127.0.0.1:${upPort}` } });
  try {
    const res = await fetch(`${web.origin}/auth/account/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: web.origin, cookie: 'a=b', 'x-csrf-token': 't' },
      body: '{"username":"u","password":"p"}',
    });
    assert.equal(res.status, 201);
    assert.equal(await res.text(), '{"ok":true}');
    assert.match(res.headers.get('set-cookie'), /llm_lab_session=abc/);
    assert.equal(seen.method, 'POST');
    assert.equal(seen.url, '/auth/account/login');
    assert.equal(seen.body, '{"username":"u","password":"p"}');
    assert.equal(seen.headers.host, `127.0.0.1:${upPort}`);
    assert.equal(seen.headers.origin, `http://127.0.0.1:${upPort}`);
    assert.equal(seen.headers.cookie, 'a=b');
    assert.equal(seen.headers['x-csrf-token'], 't');
    // Static files are still served locally.
    assert.equal((await fetch(`${web.origin}/console/index.html`)).status, 200);
  } finally {
    await web.close();
    await new Promise((d) => upstream.close(d));
  }
});

test('an unreachable backend is a 502, not a hang or a crash', async () => {
  const probe = createServer();
  const deadPort = await listen(probe);
  await new Promise((d) => probe.close(d));
  const web = await startServer({ env: { LLMLAB_DEV_API: `http://127.0.0.1:${deadPort}` } });
  try {
    const res = await fetch(`${web.origin}/auth/me`);
    assert.equal(res.status, 502);
    assert.equal((await res.json()).error.code, 'dev_api_unreachable');
  } finally {
    await web.close();
  }
});

test('without the variable /auth/ stays a static 404 and writes are refused', async () => {
  const web = await startServer({ env: {} });
  try {
    assert.equal((await fetch(`${web.origin}/auth/me`)).status, 404);
    assert.equal((await fetch(`${web.origin}/auth/account/login`, { method: 'POST' })).status, 405);
  } finally {
    await web.close();
  }
});
