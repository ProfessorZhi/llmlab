/*! tests/web-features/ops-source.test.js — where the data may come from.
 *
 *  The rule this file pins down is the one that keeps a management page from
 *  becoming an exfiltration surface: **the browser is never handed a way off the
 *  origin, and never silently falls back to the fixture.**
 *
 *  Four claims are checked, and each one is checked by observation rather than
 *  by reading the code:
 *
 *    * no URL can select a mock except explicit `?demo=1`, not even `?source=mock`;
 *    * a `live` base that is absolute, protocol-relative, or names `:8080` is
 *      refused before any request is built;
 *    * in `mock` mode the injected `fetchImpl` is **never called** — asserted
 *      with a spy that throws;
 *    * the 401/403/!ok/!json/contract ladder maps onto four distinct page
 *      states, and a contract violation never renders half a page.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SOURCE_MODES,
  createFeatureSource,
  endpointUrl,
  isSameOriginBase,
  resolveFeatureSource,
} from '../../web/js/features/source.js';
import { createMockHttp } from '../../web/js/features/mock-fixture.js';

/** A `fetch` that fails the test if it is ever reached. */
const forbiddenFetch = () => {
  throw new Error('the network was used where it must not be');
};

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: () => 'application/json' },
  json: async () => body,
});

const textResponse = (status) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: () => 'text/html' },
  json: async () => { throw new SyntaxError('not json'); },
});

// --- resolution -------------------------------------------------------------

test('a bare URL is not a licence to serve the fixture', () => {
  const resolved = resolveFeatureSource({ search: '' });
  assert.equal(resolved.mode, SOURCE_MODES.UNAVAILABLE, 'no explicit option must not fall back to the fixture');
  assert.equal(resolved.reason, 'auth-bff-not-connected');
  assert.equal(resolved.base, null);
});

test('the design samples need an explicit opt-in, and only the exact one', () => {
  for (const search of ['?demo=1', '?demo=true']) {
    const resolved = resolveFeatureSource({ search, allowDemo: true });
    assert.equal(resolved.mode, SOURCE_MODES.MOCK, `${search} must open the samples on the QA build`);
    assert.equal(resolved.reason, 'design-samples-opt-in');
    // Without the QA artifact the same query stays unavailable: the URL asks,
    // the build answers, and production answers no.
    assert.equal(resolveFeatureSource({ search }).mode, SOURCE_MODES.UNAVAILABLE, `${search} on production`);
  }
  for (const search of ['', '?demo=0', '?demo=', '?demo=yes', '?utm_source=chatgpt.com']) {
    const resolved = resolveFeatureSource({ search, allowDemo: true });
    assert.equal(resolved.mode, SOURCE_MODES.UNAVAILABLE, `${search} must stay unavailable`);
    assert.equal(resolved.reason, 'auth-bff-not-connected');
  }
  assert.equal(resolveFeatureSource({ search: '?demo=1&utm_source=chatgpt.com', allowDemo: true }).mode, SOURCE_MODES.MOCK,
    'the opt-in still works beside a tracking parameter');
});

test('legacy ?source=mock cannot leak mock instances onto a production page', () => {
  const resolved = resolveFeatureSource({ search: '?source=mock' });
  assert.equal(resolved.mode, SOURCE_MODES.UNAVAILABLE);
  assert.equal(resolved.reason, 'unknown-source-value');
  assert.equal(resolveFeatureSource({ search: '?source=mock&demo=1', allowDemo: true }).mode, SOURCE_MODES.MOCK,
    'only the explicit demo opt-in on the QA build is allowed to show sample instances');
  assert.equal(resolveFeatureSource({ search: '?source=mock&demo=1' }).mode, SOURCE_MODES.UNAVAILABLE,
    'and neither flag reaches the samples on production');
});

test('live without a configured base is unavailable, never the fixture', () => {
  const resolved = resolveFeatureSource({ search: '?source=live' });
  assert.equal(resolved.mode, SOURCE_MODES.UNAVAILABLE);
  assert.equal(resolved.reason, 'no-endpoint-configured');
});

test('an unrecognised source value is not a licence to serve the fixture', () => {
  for (const search of ['?source=prod', '?source=REMOTE', '?source=', '?source=live2']) {
    const resolved = resolveFeatureSource({ search });
    assert.equal(resolved.mode, SOURCE_MODES.UNAVAILABLE, `${search} resolved to ${resolved.mode}`);
  }
});

test('operator-configured same-origin BFF is used by default, never an in-process fixture', () => {
  for (const base of ['', '/gw', '/api']) {
    for (const search of ['', '?source=live']) {
      const resolved = resolveFeatureSource({ search, host: { opsBase: base } });
      assert.equal(resolved.mode, SOURCE_MODES.LIVE, `base ${JSON.stringify(base)}, query ${search}`);
      assert.equal(resolved.base, base);
      assert.equal(resolved.reason, search ? 'explicit-live' : 'configured-same-origin');
    }
  }
});

test('a base that leaves the origin is refused, and the difference is visible', () => {
  for (const base of [
    'https://lab.example',
    'http://127.0.0.1:8080',
    '//evil.example',
    '/proxy/127.0.0.1:8080',
    'lab.example',
  ]) {
    const resolved = resolveFeatureSource({ search: '?source=live', host: { opsBase: base } });
    assert.equal(resolved.mode, SOURCE_MODES.UNAVAILABLE, `base ${base} was accepted`);
    assert.equal(resolved.reason, 'endpoint-is-not-same-origin');
    assert.equal(resolved.base, null, 'a refused base must not be carried forward');
  }
});

test('isSameOriginBase agrees with the shared modules rule', () => {
  assert.equal(isSameOriginBase(''), true);
  assert.equal(isSameOriginBase('/gw'), true);
  assert.equal(isSameOriginBase('//evil'), false);
  assert.equal(isSameOriginBase('https://x'), false);
  assert.equal(isSameOriginBase('/x:8080'), false);
});

test('the persona is read from the URL and defaults visibly', () => {
  assert.equal(resolveFeatureSource({ search: '' }).persona, 'user');
  assert.equal(resolveFeatureSource({ search: '?persona=admin' }).persona, 'admin');
});

test('endpointUrl refuses to build a non-same-origin URL', () => {
  assert.equal(endpointUrl('', '/api/v2/ops'), '/api/v2/ops');
  assert.equal(endpointUrl('/gw', '/api/v2/ops'), '/gw/api/v2/ops');
  assert.throws(() => endpointUrl('https://evil.example', '/api/v2/ops'), /refusing/);
  assert.throws(() => endpointUrl('/x:8080', '/api/v2/ops'), /refusing/);
});

// --- mock mode never reaches the network ------------------------------------

test('in mock mode an injected fetch is never called', async () => {
  const source = createFeatureSource({
    mode: SOURCE_MODES.MOCK,
    persona: 'sharer',
    fetchImpl: forbiddenFetch,
  });
  const answer = await source.load();
  assert.equal(answer.state, 'ready');
  assert.equal(source.synthetic, true);

  // The write path too: a share change goes to the fixture, not the network.
  const result = await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal(result.ok, true);
});

test('a mock source reports the fixture it is using, and can be switched', async () => {
  const source = createFeatureSource({ mode: SOURCE_MODES.MOCK, persona: 'sharer' });
  assert.equal(source.mode, SOURCE_MODES.MOCK);
  assert.equal(source.visibility, 'private');
  await source.setVisibility('shared');
  assert.equal(source.visibility, 'shared');
  assert.equal((await source.load()).ops.registry.shared.length, 1);
});

test('an unsupported visibility is refused rather than silently ignored', async () => {
  const source = createFeatureSource({ mode: SOURCE_MODES.MOCK });
  const result = await source.setVisibility('global');
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'unsupported-visibility');
  assert.equal(source.visibility, 'private', 'the rejected value must not stick');
});

// --- the live ladder --------------------------------------------------------

const liveSource = (response, { base = '/gw', fetchImpl } = {}) => createFeatureSource({
  mode: SOURCE_MODES.LIVE,
  base,
  fetchImpl: fetchImpl ?? (async () => response),
});

test('a valid live payload becomes ready', async () => {
  const ops = createMockHttp({ persona: 'sharer' }).ops();
  const answer = await liveSource(jsonResponse(200, { data: { ops } })).load();
  assert.equal(answer.state, 'ready');
  assert.equal(answer.ops.registry.owned.length, 2);
});

test('the live request is same-origin, asks for JSON, and sends no credentials of its own', async () => {
  const seen = [];
  const ops = createMockHttp({ persona: 'sharer' }).ops();
  const source = liveSource(null, {
    base: '/gw',
    fetchImpl: async (url, init) => {
      seen.push({ url, init });
      return jsonResponse(200, { data: { ops } });
    },
  });
  await source.load();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, '/gw/api/v2/ops');
  assert.equal(seen[0].init.method, 'GET');
  assert.equal(seen[0].init.credentials, 'same-origin');
  assert.equal(seen[0].init.headers.Accept, 'application/json');
});

test('401 is signed-out, 403 is denied, and neither is retried or relabelled', async () => {
  const signedOut = await liveSource(jsonResponse(401, {})).load();
  assert.equal(signedOut.state, 'signed_out');
  assert.equal(signedOut.reason, 'http-401');

  const denied = await liveSource(jsonResponse(403, {})).load();
  assert.equal(denied.state, 'denied');
  assert.equal(denied.reason, 'http-403');
});

test('any other failure is unknown, with no data', async () => {
  for (const status of [500, 502, 404]) {
    const answer = await liveSource(jsonResponse(status, {})).load();
    assert.equal(answer.state, 'unknown', `status ${status}`);
  }
  const notJson = await liveSource(textResponse(200)).load();
  assert.equal(notJson.state, 'unknown');
  assert.equal(notJson.reason, 'not-json');
});

test('a contract violation turns into unknown and carries the errors', async () => {
  const ops = createMockHttp({ persona: 'sharer' }).ops();
  ops.registry.owned[0].resources.vram_total_mib = 49140; // no NVML source
  const answer = await liveSource(jsonResponse(200, { data: { ops } })).load();
  assert.equal(answer.state, 'unknown');
  assert.match(answer.reason, /^contract:/);
  assert.ok(answer.errors.length > 0);
  assert.equal(answer.ops.registry, null, 'a refused payload must not leak a half-page');
});

test('a network error is unknown, not a crash', async () => {
  const source = liveSource(null, { fetchImpl: async () => { throw new Error('offline'); } });
  const answer = await source.load();
  assert.equal(answer.state, 'unknown');
  assert.equal(answer.reason, 'request-failed');
});

test('with no fetch available at all, the answer is a rendered unknown', async () => {
  const source = createFeatureSource({ mode: SOURCE_MODES.LIVE, base: '/gw', fetchImpl: null });
  const original = globalThis.fetch;
  // Simulate a host with no fetch: the source must not throw at import or call time.
  globalThis.fetch = undefined;
  try {
    const answer = await source.load();
    assert.equal(answer.state, 'unknown');
  } finally {
    globalThis.fetch = original;
  }
});

// --- unavailable mode -------------------------------------------------------

test('an unavailable source opens no socket and says why', async () => {
  const source = createFeatureSource({ mode: SOURCE_MODES.UNAVAILABLE, fetchImpl: forbiddenFetch });
  const answer = await source.load();
  assert.equal(answer.state, 'unknown');
  assert.equal(answer.reason, 'unconfigured-source');
  assert.equal(answer.ops.registry, null);
});

test('writes against an unavailable source are refused before the wire', async () => {
  const source = createFeatureSource({ mode: SOURCE_MODES.UNAVAILABLE, fetchImpl: forbiddenFetch });
  const result = await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'source-unavailable');
});

// --- writes -----------------------------------------------------------------

test('a live share write goes to the same-origin share path', async () => {
  const seen = [];
  const source = createFeatureSource({
    mode: SOURCE_MODES.LIVE,
    base: '/gw',
    fetchImpl: async (url, init) => {
      seen.push({ url, init });
      return jsonResponse(200, { data: { deployment: { deployment_id: 'dep-demo-1', share: { mode: 'all' } } } });
    },
  });
  const result = await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal(result.ok, true);
  assert.equal(seen[0].url, '/gw/api/v2/ops/deployments/dep-demo-1/share');
  assert.equal(seen[0].init.method, 'PUT');
  assert.equal(JSON.parse(seen[0].init.body).mode, 'all');
});

test('a refused write reports the server error code, not a generic failure', async () => {
  const source = createFeatureSource({
    mode: SOURCE_MODES.LIVE,
    base: '/gw',
    fetchImpl: async () => jsonResponse(403, { error: { code: 'forbidden', message: 'no' } }),
  });
  const result = await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.status, 403);
  assert.equal(result.reason, 'forbidden');
});

test('a write with no deployment id is refused before the wire', async () => {
  const source = createFeatureSource({ mode: SOURCE_MODES.MOCK, persona: 'sharer', fetchImpl: forbiddenFetch });
  const result = await source.updateShare('', { mode: 'all', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'missing-deployment-id');
});

test('a write whose response carries no deployment is not treated as success', async () => {
  const source = createFeatureSource({
    mode: SOURCE_MODES.LIVE,
    base: '/gw',
    fetchImpl: async () => jsonResponse(200, { data: {} }),
  });
  const result = await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'contract:no-deployment');
});
