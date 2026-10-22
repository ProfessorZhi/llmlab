/*! tests/infra/serve-infra.test.js
 *  The one non-static route. These bind a real loopback server (127.0.0.1 only)
 *  and drive it over HTTP, but the *upstream* fetch is injected, so nothing ever
 *  reaches a Dell host or the public network.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { startServer } from '../../web/tools/serve.mjs';
import { INFRA_PATH } from '../../web/tools/verified-infra.mjs';

const quietFetch = () => {
  const impl = async (url) => {
    impl.seen.push(String(url));
    return {
      ok: true,
      status: 200,
      json: async () => ({ status: 'success', data: { resultType: 'vector', result: [{ metric: {}, value: [Date.now() / 1000, '0.5'] }] } }),
      arrayBuffer: async () => new ArrayBuffer(0),
    };
  };
  impl.seen = [];
  return impl;
};

async function withServer(options, fn) {
  const s = await startServer({ port: 0, ...options });
  try {
    return await fn(s);
  } finally {
    await s.close();
  }
}

test('the route is a 404 while the operator flag is unset', async () => {
  await withServer({ env: {}, infraFetch: quietFetch() }, async (s) => {
    const res = await fetch(`${s.origin}${INFRA_PATH}`);
    assert.equal(res.status, 404);
    const body = await res.json();
    assert.deepEqual(body, { error: 'not found' });
  });
});

test('with the flag set it answers the projection as same-origin JSON', async () => {
  const infraFetch = quietFetch();
  await withServer({ env: { ATELIER_INFRA_READONLY: '1' }, infraFetch }, async (s) => {
    const res = await fetch(`${s.origin}${INFRA_PATH}`);
    assert.equal(res.status, 200);
    assert.match(res.headers.get('content-type'), /application\/json/);
    assert.equal(res.headers.get('cache-control'), 'no-store');
    assert.equal(res.headers.get('access-control-allow-origin'), null, 'no CORS may be opened');
    const body = await res.json();
    assert.equal(body.source, 'prometheus-readonly');
    assert.equal(body.synthetic, false);
    assert.equal(body.version, 1);
    assert.equal(typeof body.metrics, 'object');
    assert.equal(typeof body.services, 'object');
    assert.ok(infraFetch.seen.length > 0, 'the injected upstream fetch was used');
    for (const url of infraFetch.seen) {
      assert.equal(new URL(url).hostname, '127.0.0.1', `upstream target is not loopback: ${url}`);
    }
  });
});

test('a query string is refused rather than interpreted', async () => {
  await withServer({ env: { ATELIER_INFRA_READONLY: '1' }, infraFetch: quietFetch() }, async (s) => {
    const res = await fetch(`${s.origin}${INFRA_PATH}?query=up`);
    assert.equal(res.status, 400);
  });
});

test('only GET is accepted', async () => {
  await withServer({ env: { ATELIER_INFRA_READONLY: '1' }, infraFetch: quietFetch() }, async (s) => {
    for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
      const res = await fetch(`${s.origin}${INFRA_PATH}`, { method });
      assert.equal(res.status, 405, `${method} must not be routed`);
    }
  });
});

test('the static server keeps working beside the route', async () => {
  await withServer({ env: { ATELIER_INFRA_READONLY: '1' }, infraFetch: quietFetch() }, async (s) => {
    // /index.html is the console's front door now; the sibling static page is
    // /qa.html, which is what proves static serving still works beside the route.
    const res = await fetch(`${s.origin}/qa.html`);
    assert.equal(res.status, 200);
    assert.match(res.headers.get('content-type'), /text\/html/);
    const missing = await fetch(`${s.origin}/does-not-exist`);
    assert.equal(missing.status, 404);
  });
});

test('an upstream failure is a complete UNKNOWN answer, not a leaked error', async () => {
  const failing = async () => { throw new Error('ECONNREFUSED 127.0.0.1:9090'); };
  await withServer({ env: { ATELIER_INFRA_READONLY: '1' }, infraFetch: failing }, async (s) => {
    const res = await fetch(`${s.origin}${INFRA_PATH}`);
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.state, 'unknown');
    const text = JSON.stringify(body);
    assert.ok(!text.includes('ECONNREFUSED'), 'the upstream error text must not be reflected');
  });
});
