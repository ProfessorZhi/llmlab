/*! tests/serve.test.js — the static server that makes the front-end testable. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { request } from 'node:http';
import { fileURLToPath } from 'node:url';
import { resolve, sep } from 'node:path';

import { resolveRequestPath, startServer, WEB_ROOT } from '../web/tools/serve.mjs';

const REPO_ROOT = resolve(fileURLToPath(new URL('../', import.meta.url)));

/** Raw request so the traversal path reaches the server verbatim — `fetch`
 *  normalises `..` segments in the client before anything is sent. */
function rawGet(origin, rawPath, method = 'GET') {
  const { hostname, port } = new URL(origin);
  return new Promise((ok, fail) => {
    const req = request({ hostname, port, path: rawPath, method }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { body += c; });
      res.on('end', () => ok({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', fail);
    req.end();
  });
}

test('maps request paths onto real files under the root', () => {
  assert.equal(resolveRequestPath('/'), resolve(WEB_ROOT, 'index.html'));
  assert.equal(resolveRequestPath('/index.html'), resolve(WEB_ROOT, 'index.html'));
  assert.equal(resolveRequestPath('/js/app.js'), resolve(WEB_ROOT, 'js', 'app.js'));
  assert.equal(resolveRequestPath('/js/app.js?v=2'), resolve(WEB_ROOT, 'js', 'app.js'));
});

test('accepts a root written with forward slashes', { skip: sep === '/' }, () => {
  // Regression guard: this build is developed on Windows, where a caller passes
  // 'D:/work/repo' while path.resolve hands back 'D:\work\repo'. A naive string
  // prefix test then rejects every request with a 403 and the page loads blank.
  const forward = REPO_ROOT.split(sep).join('/');
  assert.notEqual(forward, REPO_ROOT, 'the fake needs a differently-shaped root to be meaningful');
  assert.equal(
    resolveRequestPath('/web/index.html', forward),
    resolve(REPO_ROOT, 'web', 'index.html'),
  );
});

test('nothing resolves outside the root', () => {
  const escapes = [
    '/../package.json',
    '/web/../../package.json',
    '/%2e%2e/package.json',
    '/..%2f..%2fpackage.json',
    '/js/..%2f..%2f..%2fetc%2fpasswd',
    '/a/../../..',
    '/....//....//package.json',
  ];
  const rootIsPrefix = (p) => p === WEB_ROOT
    || p.toLowerCase().startsWith(`${WEB_ROOT.toLowerCase()}${sep.toLowerCase()}`);
  for (const bad of escapes) {
    const abs = resolveRequestPath(bad);
    // Two acceptable outcomes: refused outright, or folded back inside the
    // root. What must never happen is a path that lands outside it.
    if (abs !== null) assert.ok(rootIsPrefix(abs), `${bad} escaped the root → ${abs}`);
  }
  // Note: the URL parser folds real `..` segments before this function sees
  // them, so most of the list above lands back inside the root. That is a safe
  // outcome, but it means the only portable assertion is the invariant itself —
  // plus the HTTP-level check below, which is where a traversal would actually
  // leak bytes.
});

test('serves files over loopback and rejects everything else', async () => {
  const server = await startServer({ port: 0 });
  try {
    assert.match(server.origin, /^http:\/\/127\.0\.0\.1:\d+$/, 'must bind loopback only');

    const page = await fetch(`${server.origin}/index.html`);
    assert.equal(page.status, 200);
    assert.equal(page.headers.get('content-type'), 'text/html; charset=utf-8');
    assert.match(await page.text(), /Dell LLM Lab/);

    const mod = await fetch(`${server.origin}/js/app.js`);
    assert.equal(mod.status, 200);
    assert.equal(mod.headers.get('content-type'), 'text/javascript; charset=utf-8');

    assert.equal((await fetch(`${server.origin}/nope.js`)).status, 404);
    assert.equal((await fetch(`${server.origin}/`, { method: 'POST' })).status, 405);

    // Sent with `..` encoded so the client cannot tidy it away first. The bytes
    // that come back must be a file from inside web/, never the real prototype
    // that lives one level above it.
    const escaped = await rawGet(server.origin, '/..%2f..%2fprototypes%2fv5%2findex.html');
    assert.notEqual(escaped.status, 200, 'a traversal above the root must not resolve');
    assert.doesNotMatch(escaped.body ?? '', /Atelier/, 'no file from above the root may be served');
  } finally {
    await server.close();
  }
});

test('serves the repository root, not only web/', async () => {
  const server = await startServer({ root: REPO_ROOT.split(sep).join('/'), port: 0 });
  try {
    const proto = await fetch(`${server.origin}/prototypes/v5/index.html`);
    assert.equal(proto.status, 200, 'the frozen prototype must be reachable for comparison');
    assert.equal((await fetch(`${server.origin}/web/css/tokens.css`)).status, 200);
  } finally {
    await server.close();
  }
});
