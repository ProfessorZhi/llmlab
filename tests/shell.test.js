/*! tests/shell.test.js — the boot layer: source resolution, and the copy that
 *  the shell is allowed to say. These run in plain Node: every module imported
 *  here keeps its DOM access inside function bodies, so nothing needs a browser.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { loadSource, describeSource, VERSION } from '../web/js/app.js';
import { resolveDataMode } from '../web/js/demo-mode.js';
import { TIMELINE_SERIES, METRIC_COLOR, METRIC_UNIT, METRIC_LABEL } from '../web/js/timeline.js';
import { CURL_EXAMPLE, EXPLAIN_LINUX, EXPLAIN_ENDPOINT, EXPLAIN_COPIED, EXPLAIN_COPY_BLOCKED } from '../web/js/dialog.js';
import { OFFLINE_REPLY } from '../web/js/chat.js';
import { PAGES, PAGE_NAMES, pageFromHash, pageParent, ALL_ROUTES } from '../web/js/router.js';

const EXPLAINS = [EXPLAIN_LINUX, EXPLAIN_ENDPOINT, EXPLAIN_COPIED, EXPLAIN_COPY_BLOCKED];

/** A fetch that fails the test if it is ever reached. */
const forbiddenFetch = () => { throw new Error('the network must not be touched on this path'); };

/** The isolated QA artifact, as a document double: the only thing that lets a
 *  `?demo=1` mean anything. `undefined` — the default everywhere — is the
 *  artifact that ships. */
const QA_DOC = { documentElement: { dataset: { buildProfile: 'qa' } } };

test('a bare URL stays unconfigured, and the fixture needs the QA build too', async () => {
  // The production default: no authored number reaches the screen and no request
  // is made. `?source=demo` is a retired alias — it is an unrecognised value now,
  // not a second way to reach the fixture.
  for (const search of ['', '?', '?source=demo', '?source=demo&x=1']) {
    const { resolved, state } = await loadSource({ search, fetchImpl: forbiddenFetch });
    assert.equal(resolved.mode, 'unconfigured', `"${search}" must not resolve to a served source`);
    assert.equal(resolved.endpoint, null);
    assert.equal(state.state, 'unknown');
    assert.equal(state.synthetic, false);
    assert.equal(state.workloads.length, 0, `"${search}" must not fall back to the fixture`);
    assert.equal(describeSource(state).rows, 0);
  }
  const alias = await loadSource({ search: '?source=demo', fetchImpl: forbiddenFetch });
  assert.equal(alias.resolved.reason, 'unknown-source-value');

  // The fixture needs BOTH the isolated QA artifact and the explicit opt-in.
  const demo = await loadSource({ search: '?demo=1', fetchImpl: forbiddenFetch, doc: QA_DOC });
  assert.equal(demo.resolved.mode, 'demo');
  assert.equal(demo.state.state, 'demo');
  assert.equal(demo.state.synthetic, true);
  assert.equal(demo.state.workloads.length, 9);
  assert.deepEqual(demo.state.notes, ['Synthetic UI fixture — no production service was queried.']);

  // The same URL on the artifact that ships changes nothing at all: the URL asks,
  // the build answers, and production answers no.
  const inert = await loadSource({ search: '?demo=1', fetchImpl: forbiddenFetch });
  assert.equal(inert.resolved.mode, 'unconfigured');
  assert.equal(inert.state.state, 'unknown');
  assert.equal(inert.state.synthetic, false);
  assert.equal(inert.state.workloads.length, 0);

  // Source values are matched exactly, on purpose: 'DEMO' is not a spelling we
  // honour, it is an unrecognised value, and unrecognised values fail closed.
  const odd = await loadSource({ search: '?source=DEMO', fetchImpl: forbiddenFetch });
  assert.equal(odd.resolved.mode, 'unconfigured');
  assert.equal(odd.state.synthetic, false);
  assert.equal(odd.state.workloads.length, 0);
});

test('the shell mode and the Atlas source agree about what counts as a sample', async () => {
  // Two resolvers decide "is this a sample?": `resolveDataMode` gates the shell's
  // authored values, `resolveSource` gates the Atlas. They are separate modules,
  // so nothing but this test stops them drifting apart — and a drift shows one
  // surface painted with fabricated data while the other says it is unconfigured.
  const searches = [
    '', '?', '?demo=1', '?demo=true', '?demo=0', '?demo=yes',
    '?source=remote', '?source=demo', '?source=production', '?utm_source=newsletter',
  ];
  for (const search of searches) {
    for (const [label, doc] of [['production', undefined], ['qa', QA_DOC]]) {
      const shellDemo = resolveDataMode(search, { qa: doc === QA_DOC }) === 'demo';
      const { resolved } = await loadSource({ search, fetchImpl: forbiddenFetch, doc });
      assert.equal(
        resolved.mode === 'demo', shellDemo,
        `"${search}" (${label}): the shell and the Atlas must agree on whether this is a sample`,
      );
    }
  }
  // The one opt-in is honoured by both, and only on the QA artifact.
  assert.equal(resolveDataMode('?demo=1', { qa: true }), 'demo');
  assert.equal(resolveDataMode('?demo=1'), 'live');
  assert.equal(resolveDataMode('?source=demo', { qa: true }), 'live');
});

test('an unrecognised source value is refused rather than served the fixture', async () => {
  const { resolved, state } = await loadSource({ search: '?source=production', fetchImpl: forbiddenFetch });
  assert.equal(resolved.mode, 'unconfigured');
  assert.equal(resolved.reason, 'unknown-source-value');
  assert.equal(state.state, 'unknown');
  assert.equal(state.synthetic, false);
  assert.equal(state.workloads.length, 0);
});

test('?source=remote without an endpoint fails closed, with no fetch at all', async () => {
  const { resolved, state } = await loadSource({ search: '?source=remote', host: {}, fetchImpl: forbiddenFetch });
  assert.equal(resolved.mode, 'unconfigured');
  assert.equal(resolved.reason, 'no-endpoint-configured');
  assert.equal(resolved.endpoint, null);
  assert.equal(state.state, 'unknown');
  assert.equal(state.synthetic, false);
  assert.equal(state.workloads.length, 0, 'never fall back to synthetic values');
  assert.equal(describeSource(state).rows, 0);
});

test('a configured same-origin path fetches exactly that path, once', async () => {
  const seen = [];
  const payload = {
    synthetic: false,
    scope: 'project/llm-lab',
    quality: { sample: 1248 },
    groups: [{ id: 'conversation', name: '交互与内容', calls: 700, workloads: [
      { id: 'chat', name: '对话', calls: 700, p95: 420, tokens: 210 },
      { id: 'vision', name: '图片', calls: 120, p95: null, tokens: null },
    ] }],
  };
  const fetchImpl = async (url, init) => {
    seen.push({ url, init });
    return { ok: true, status: 200, json: async () => payload };
  };

  const { resolved, state } = await loadSource({
    search: '?source=remote',
    host: { endpoint: '/api/atlas/summary' },
    fetchImpl,
  });

  assert.equal(resolved.mode, 'remote');
  assert.equal(seen.length, 1, 'exactly one request');
  assert.equal(seen[0].url, '/api/atlas/summary');
  assert.equal(seen[0].init.credentials, 'same-origin', 'no ambient credentials are invented');
  assert.deepEqual(seen[0].init.headers, { Accept: 'application/json' });

  assert.equal(state.state, 'live');
  assert.equal(state.synthetic, false);
  assert.equal(state.workloads.length, 2);
  assert.equal(state.workloads[1].p95, null, 'an absent metric stays null, not 0');
  assert.equal(state.scope, 'project/llm-lab');
  assert.deepEqual(describeSource(state), {
    state: 'live', synthetic: false, reason: null, notes: [], rows: 2,
  });
});

test('a remote payload claiming to be synthetic is refused, not shown', async () => {
  const fetchImpl = async () => ({
    ok: true, status: 200, json: async () => ({ synthetic: true, groups: [{ id: 'g', calls: 1 }] }),
  });
  const { state } = await loadSource({
    search: '?source=remote',
    host: { endpoint: '/api/atlas/summary' },
    fetchImpl,
  });
  assert.equal(state.state, 'unknown');
  assert.equal(state.synthetic, false);
  assert.equal(state.workloads.length, 0);
});

test('a failing remote request degrades to UNKNOWN, never to the fixture', async () => {
  const cases = [
    ['http-500', async () => ({ ok: false, status: 500 })],
    ['not-json', async () => ({ ok: true, status: 200, json: async () => { throw new Error('bad json'); } })],
    ['throws', async () => { throw new Error('offline'); }],
  ];
  for (const [label, fetchImpl] of cases) {
    const { state } = await loadSource({
      search: '?source=remote',
      host: { endpoint: '/api/atlas/summary' },
      fetchImpl,
    });
    assert.equal(state.state, 'unknown', `${label} should be unknown`);
    assert.equal(state.workloads.length, 0, `${label} must not fall back to synthetic rows`);
    assert.match(state.reason, /^remote:/, `${label} should name the failure`);
  }
});

test('an authorised remote source with no rows reads as empty, not as unknown', async () => {
  const fetchImpl = async () => ({ ok: true, status: 200, json: async () => ({ synthetic: false, groups: [] }) });
  const { state } = await loadSource({
    search: '?source=remote',
    host: { endpoint: '/api/atlas/summary' },
    fetchImpl,
  });
  assert.equal(state.state, 'empty');
  assert.equal(state.synthetic, false);
  assert.equal(state.reason, 'remote:no-workloads');
});

test('a host-configured same-origin path is live by default, with no query string', async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({
        synthetic: false,
        groups: [{ id: 'g', calls: 3, workloads: [{ id: 'w', calls: 3, p95: 10, tokens: 2 }] }],
      }),
    };
  };
  // No `?source=` at all: the hosting environment injected the reviewed path, so
  // the reader is never asked to opt in by hand.
  const { resolved, state } = await loadSource({
    search: '',
    host: { endpoint: '/api/atlas/summary' },
    fetchImpl,
  });
  assert.equal(resolved.mode, 'remote');
  assert.equal(resolved.reason, 'configured-same-origin');
  assert.equal(seen.length, 1, 'exactly one request');
  assert.equal(state.state, 'live');
  assert.equal(state.synthetic, false);
  assert.equal(state.workloads.length, 1);
});

test('a caller-supplied absolute or traversing URL is refused, never fetched', async () => {
  // A client-selectable upstream URL is the failure mode the source contract
  // exists to prevent: only a reviewed same-origin BFF path may be fetched.
  const rejected = [
    'https://portal.example/api/atlas',
    'http://127.0.0.1:8443/api/atlas',
    '//portal.example/api/atlas',
    '/api/atlas/../secrets',
    '/api/atlas//x',
  ];
  for (const endpoint of rejected) {
    const { resolved, state } = await loadSource({
      search: '?source=remote',
      host: { endpoint },
      fetchImpl: forbiddenFetch,
    });
    assert.equal(resolved.mode, 'unconfigured', `${endpoint} must not become a served source`);
    assert.equal(resolved.reason, 'endpoint-not-approved', `${endpoint} must be refused by name`);
    assert.equal(resolved.endpoint, null);
    assert.equal(state.state, 'unknown');
    assert.equal(state.synthetic, false);
    assert.equal(state.workloads.length, 0);
  }
});

test('the overview sparkline is a fixed 24-point demo series per metric', () => {
  const metrics = ['calls', 'latency', 'tokens'];
  assert.deepEqual(Object.keys(TIMELINE_SERIES).sort(), metrics);
  for (const m of metrics) {
    assert.equal(TIMELINE_SERIES[m].length, 24, `${m} should have one point per hour`);
    assert.ok(TIMELINE_SERIES[m].every((n) => Number.isFinite(n) && n >= 0), `${m} must be finite`);
    assert.equal(METRIC_COLOR[m].slice(0, 4), 'var(', `${m} should paint from a CSS token`);
    assert.ok(METRIC_UNIT[m].length > 0 && METRIC_LABEL[m].length > 0);
  }
});

test('the shell copy never carries a credential', () => {
  const strings = [
    CURL_EXAMPLE,
    OFFLINE_REPLY,
    ...EXPLAINS.flatMap((e) => [e.title, e.body]),
    PAGE_NAMES[PAGES[4]],
  ];
  for (const s of strings) {
    assert.doesNotMatch(s, /llk_[A-Za-z0-9]{4,}/, `a key-shaped string leaked: ${s}`);
    assert.doesNotMatch(s, /Bearer\s+[A-Za-z0-9_\-.]{12,}/, `a token-shaped string leaked: ${s}`);
    assert.doesNotMatch(s, /sk-[A-Za-z0-9]{8,}/, `an API-key-shaped string leaked: ${s}`);
  }
  assert.match(CURL_EXAMPLE, /<YOUR_SCOPED_KEY>/, 'the example must use a placeholder');
  assert.match(CURL_EXAMPLE, /lab\.example/, 'the example must point at a non-existent host');
});

test('the demo chat is explicitly a dead end', () => {
  assert.match(OFFLINE_REPLY, /未发送/, 'the reply must say nothing was sent');
  assert.match(OFFLINE_REPLY, /原型/);
});

test('legacy /#/compute and native #compute both target the real Compute view', () => {
  assert.equal(pageFromHash('#/compute'), 'compute');
  assert.equal(pageFromHash('#compute'), 'compute');
  assert.equal(pageFromHash('#/models'), 'models');
  assert.equal(pageFromHash('#/not-a-page'), 'overview');
  assert.equal(PAGE_NAMES.compute, '算力中心');
  assert.equal(pageParent('compute'), 'overview');
  assert.equal(PAGES.includes('compute'), false, 'Compute is not a ninth primary tab');
  assert.equal(ALL_ROUTES.includes('compute'), true);
});

test('the shell advertises the prototype version', () => {
  assert.equal(VERSION, '6.0.0');
  assert.equal(PAGES.length, 8);
});
