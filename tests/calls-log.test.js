/*! tests/calls-log.test.js — the call-log view's browser-side contract.
 *
 *  Two layers are covered here, and both are pure functions:
 *
 *  * `web/js/calls/contract.js` — the payload shield. The tests below check the
 *    things that must *fail*, because those are the ones that matter: a payload
 *    claiming to carry content, a live claim on synthetic data, and a row from a
 *    key outside the view are all refused rather than rendered.
 *  * `web/js/calls/render-log.js` — the markup. No browser is involved: the
 *    renderers return strings, so the thing that decides what a reader sees is
 *    the thing under test. This build has no Chrome (see `web/README.md`), so a
 *    DOM-driven test would simply not run here.
 *
 *  Fixtures are built inline, never read from disk: the point is to control the
 *  payload exactly, including the hostile ones.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CALL_LOG_SCHEMA,
  CONSENT_STATES,
  CONTENT_REASONS,
  LOG_STATES,
  TRANSPORT_MODES,
  consentLabel,
  contentReasonLabel,
  emptyLog,
  filterOptions,
  isDemo,
  stateFor,
  statusClass,
  summarise,
  validateCallLog,
} from '../web/js/calls/contract.js';
import {
  renderContentPanel,
  renderDemoBanner,
  renderLog,
  renderNotice,
  renderPager,
  renderScopeLine,
  renderSummary,
  renderTable,
} from '../web/js/calls/render-log.js';
import {
  buildQuery,
  localInputToUtcIso,
  utcIsoToLocalInput,
} from '../web/js/calls/controller.js';

// --- fixtures ---------------------------------------------------------------

const row = (over = {}) => ({
  call_id: 'call_a1b2c3d4e5f60718',
  call_id_basis: 'derived-from-metadata-fields',
  request_id: 'req-SYN-0001',
  ts: '2026-10-14T00:05:00Z',
  key_id: 'key-alpha',
  project: 'proj-a',
  workload: 'resume-parse',
  model: 'llm-a',
  path: '/v1/chat/completions',
  method: 'POST',
  stream: true,
  status: 200,
  outcome: 'success',
  tokens: { input: 100, output: 200, total: 300 },
  queue_ms: 5,
  ttft_ms: 120,
  upstream_ms: 800,
  total_ms: 1000,
  ...over,
});

const payload = (over = {}) => ({
  source: 'synthetic-preview',
  synthetic: true,
  schema_version: CALL_LOG_SCHEMA,
  transport: { mode: TRANSPORT_MODES.DEMO, gateway_attached: false, synthetic: true, label: 'DEMO / 合成数据' },
  scope: {
    subject: 'local:abc',
    roles: ['platform_admin'],
    scopes: ['atlas:calls:self:read'],
    projects: ['proj-a'],
    keys: ['key-alpha', 'key-orphan'],
    requested_key: null,
    revoked_keys: ['key-retired'],
    cross_key: true,
    content_consent: CONSENT_STATES.ABSENT,
  },
  window: { from: '2026-10-14T00:00:00Z', to: '2026-10-14T04:00:00Z' },
  filters: { outcome: 'all', model: null, limit: 50, before: null },
  coverage: {
    rows: 'measured',
    tokens_unknown_rows: 1,
    ttft_unknown_rows: 2,
    queue_unknown_rows: 1,
    request_id_absent_rows: 1,
  },
  counts: {
    returned: 2,
    available: 2,
    truncated: false,
    next_before: null,
    window_excluded: 1,
    revoked_key_rows: 1,
    unattributed_rows: 0,
  },
  content: {
    included: false,
    consent: CONSENT_STATES.ABSENT,
    reason: CONTENT_REASONS.CONSENT_ABSENT,
    opt_in_available: true,
    note: '调用日志只包含元数据。',
  },
  calls: [
    row({ request_id: 'req-SYN-0003', status: 429, outcome: 'failed', tokens: { input: null, output: null, total: null }, queue_ms: null }),
    row({ request_id: null, key_id: 'key-orphan', workload: 'unattributed', ts: '2026-10-14T00:20:00Z' }),
  ],
  display_hints: ['SYNTHETIC / DEMO 数据：这不是真实 Gateway 的调用记录，不得用于对账。'],
  notes: ['「我的调用」与被授权项目的用量汇总是两条独立口径。'],
  ...over,
});

const ok = (over = {}) => {
  const result = validateCallLog(payload(over));
  assert.deepEqual(result.errors, [], 'the fixture itself must be valid');
  assert.equal(result.ok, true);
  return result.log;
};

const refuses = (over = {}) => {
  const result = validateCallLog(payload(over));
  assert.equal(result.ok, false, `expected a refusal, got ${JSON.stringify(result.log)}`);
  return result.errors;
};

// --- contract: what must be refused -----------------------------------------

test('a well-formed payload passes', () => {
  assert.equal(validateCallLog(payload()).ok, true);
});

test('a payload that claims to carry content is refused outright', () => {
  const errors = refuses({ content: { ...payload().content, included: true } });
  assert.match(errors.join(' '), /content\.included must be false/);
});

test('a synthetic payload may never claim to be live', () => {
  const errors = refuses({
    transport: { mode: TRANSPORT_MODES.LIVE, gateway_attached: true, synthetic: true },
  });
  assert.match(errors.join(' '), /may not claim to be live/);
});

test('a live payload must say the gateway is attached', () => {
  const errors = refuses({
    synthetic: false,
    transport: { mode: TRANSPORT_MODES.LIVE, gateway_attached: false, synthetic: false },
  });
  assert.match(errors.join(' '), /gateway is attached/);
});

test('a row from a key outside the view is refused', () => {
  const errors = refuses({ calls: [row({ key_id: 'key-beta' })] });
  assert.match(errors.join(' '), /outside this view/);
});

test('a row naming a project outside the view is refused', () => {
  const errors = refuses({ calls: [row({ project: 'proj-b' })] });
  assert.match(errors.join(' '), /project outside this view/);
});

test('a row carrying a content field is refused', () => {
  for (const field of ['prompt', 'completion', 'messages', 'text']) {
    const errors = refuses({ calls: [{ ...row(), [field]: 'CANARY' }] });
    assert.match(errors.join(' '), /must never show/, field);
  }
});

test('a row carrying a client-supplied label is refused', () => {
  const errors = refuses({ calls: [{ ...row(), label: 'spoof-admin' }] });
  assert.match(errors.join(' '), /must never show/);
});

test('an unexpected extra row field is refused', () => {
  const errors = refuses({ calls: [{ ...row(), debug: 'x' }] });
  assert.match(errors.join(' '), /unexpected field debug/);
});

test('a token total with a missing side is refused', () => {
  const errors = refuses({
    calls: [row({ tokens: { input: null, output: 5, total: 5 } })],
  });
  assert.match(errors.join(' '), /total must be null when a side is null/);
});

test('an unknown schema version is refused rather than parsed', () => {
  const errors = refuses({ schema_version: 'atlas-usage/1' });
  assert.match(errors.join(' '), /unsupported schema_version/);
});

test('a returned count that disagrees with the rows is refused', () => {
  const errors = refuses({ counts: { ...payload().counts, returned: 99 } });
  assert.match(errors.join(' '), /disagrees/);
});

test('a payload with no keys at all is refused', () => {
  const errors = refuses({ scope: { ...payload().scope, keys: [] } });
  assert.match(errors.join(' '), /scope\.keys/);
});

test('a non-object payload is refused', () => {
  for (const value of [null, undefined, 42, 'text', []]) {
    assert.equal(validateCallLog(value).ok, false, String(value));
  }
});

// --- contract: derived display values ---------------------------------------

test('the status class is derived from the code, and unknown stays unknown', () => {
  assert.equal(statusClass(200), '2xx');
  assert.equal(statusClass(404), '4xx');
  assert.equal(statusClass(500), '5xx');
  assert.equal(statusClass(null), '—');
  assert.equal(statusClass('200'), '—');
});

test('consent and reason render as Chinese labels', () => {
  assert.equal(consentLabel(CONSENT_STATES.ABSENT), '未开启');
  assert.equal(consentLabel(CONSENT_STATES.GRANTED), '已开启');
  assert.equal(consentLabel(CONSENT_STATES.WITHDRAWN), '已撤回');
  assert.equal(consentLabel('nonsense'), '未知状态');
  assert.match(contentReasonLabel(CONTENT_REASONS.NOT_COLLECTED), /不采集正文/);
});

test('summarise totals only what is present, and never invents a zero', () => {
  const log = ok();
  const totals = summarise(log);
  assert.equal(totals.rows, 2);
  // Only one row has tokens; the other is null on both sides.
  assert.equal(totals.tokensKnown, 1);
  assert.equal(totals.input, 100);
  assert.equal(totals.output, 200);
  assert.equal(totals.failed, 1);
  assert.equal(totals.queueKnown, 1);
  assert.equal(totals.queueAvg, 5);
});

test('a page with no measurable tokens reports null, not zero', () => {
  const log = ok({
    calls: [row({ tokens: { input: null, output: null, total: null }, queue_ms: null })],
    counts: { ...payload().counts, returned: 1, available: 1 },
  });
  const totals = summarise(log);
  assert.equal(totals.input, null);
  assert.equal(totals.output, null);
  assert.equal(totals.queueAvg, null);
});

test('stateFor separates a confirmed empty window from an unknown one', () => {
  const empty = ok({
    calls: [],
    counts: { ...payload().counts, returned: 0, available: 0 },
  });
  assert.equal(stateFor(empty), LOG_STATES.EMPTY);
  assert.equal(stateFor(ok()), LOG_STATES.READY);
  assert.equal(stateFor(null), LOG_STATES.UNKNOWN);
});

test('the demo flag is read from the transport block', () => {
  assert.equal(isDemo(ok()), true);
  assert.equal(isDemo(emptyLog(LOG_STATES.UNKNOWN)), false);
});

test('filter options come from the payload, not from a hard-coded list', () => {
  const options = filterOptions(ok());
  assert.deepEqual(options.keys, ['key-alpha', 'key-orphan']);
  assert.deepEqual(options.outcomes, ['success', 'failed', 'cancelled']);
});

// --- rendering --------------------------------------------------------------

test('a missing measurement renders as an em dash, never as zero', () => {
  const html = renderTable(ok());
  assert.match(html, /—/);
  assert.doesNotMatch(html, />\s*0 ms\s*</);
});

test('the demo banner says DEMO and states there is no gateway', () => {
  const html = renderDemoBanner(ok());
  assert.match(html, /DEMO/);
  assert.match(html, /未接入真实 Gateway/);
  assert.doesNotMatch(html, /LIVE/);
});

test('a live payload gets the live banner instead', () => {
  const html = renderDemoBanner({
    synthetic: false,
    transport: { mode: TRANSPORT_MODES.LIVE, gateway_attached: true },
  });
  assert.match(html, /LIVE/);
  assert.doesNotMatch(html, /DEMO/);
});

test('the table shows time, status, tokens, queue and request_id', () => {
  const html = renderTable(ok());
  for (const expected of ['2026-10-14T00:05:00Z', '2xx', '4xx', 'req-SYN-0003', '100 / 200 / 300', '5.0 ms']) {
    assert.ok(html.includes(expected), `missing from the table: ${expected}`);
  }
});

test('a row without a request_id shows an em dash, not a fabricated id', () => {
  const html = renderTable(
    ok({ calls: [row({ request_id: null })], counts: { ...payload().counts, returned: 1, available: 1 } })
  );
  // A missing id renders as a bare em dash — not styled like an id, because
  // there is no id to style. (The mono `.doc-id` wrapper is for real values.)
  assert.match(html, /<td>—<\/td>/);
  assert.doesNotMatch(html, /doc-id">—/);
});

test('upstream strings are escaped before they reach the markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const html = renderTable(
    ok({
      calls: [row({ request_id: hostile, model: hostile })],
      counts: { ...payload().counts, returned: 1, available: 1 },
    })
  );
  assert.ok(!html.includes('<img'), html);
  assert.match(html, /&lt;img/);
});

test('the content panel reports the consent state and never a content field', () => {
  const html = renderContentPanel(ok().content);
  assert.match(html, /未开启/);
  assert.match(html, /不含/);
  assert.match(html, /content\.included = false/);
  assert.ok(!/prompt|completion|messages|text/.test(html.replace(/不采集正文/g, '')));
});

test('the content panel says "granted" without pretending text is available', () => {
  const html = renderContentPanel({
    included: false,
    consent: CONSENT_STATES.GRANTED,
    reason: CONTENT_REASONS.NOT_COLLECTED,
    note: 'x',
  });
  assert.match(html, /已开启/);
  assert.match(html, /不含/);
  assert.match(html, /不采集正文/);
});

test('the scope line names the subject, the window and the revoked keys', () => {
  const html = renderScopeLine(ok());
  assert.match(html, /local:abc/);
  assert.match(html, /2026-10-14T00:00:00Z/);
  assert.match(html, /key-retired/);
});

test('the summary tiles count this page, not the whole window', () => {
  const html = renderSummary(ok());
  assert.match(html, /本页调用/);
  assert.match(html, /共 2 条符合条件/);
});

test('the pager appears only when the server said the page was truncated', () => {
  assert.equal(renderPager(ok()), '');
  const truncated = renderPager(
    ok({ counts: { ...payload().counts, truncated: true, available: 9, next_before: { ts: '2026-10-14T00:20:00Z', call_id: 'call_x' } } })
  );
  assert.match(truncated, /加载更早的调用/);
});

test('signed-out and denied are different messages, not one generic failure', () => {
  const signedOut = renderNotice(LOG_STATES.SIGNED_OUT, 'http-401');
  const denied = renderNotice(LOG_STATES.DENIED, 'http-403');
  assert.match(signedOut, /请先登录/);
  assert.match(signedOut, /去登录/);
  assert.match(denied, /未获得/);
  assert.match(denied, /atlas:calls:self:read/);
  assert.notEqual(signedOut, denied);
});

test('the unknown notice states that nothing is being shown, and offers a retry', () => {
  const html = renderNotice(LOG_STATES.UNKNOWN, 'contract:problem');
  assert.match(html, /无法确定/);
  assert.match(html, /contract:problem/);
  assert.match(html, /data-calls-retry/);
});

test('the empty notice says the window is empty, not that it could not be read', () => {
  const html = renderNotice(LOG_STATES.EMPTY);
  assert.match(html, /没有你的调用/);
  assert.match(html, /确实为空/);
});

test('renderLog paints the demo banner, the scope, the table and the notes for a page', () => {
  const html = renderLog(ok(), LOG_STATES.READY);
  assert.match(html, /DEMO/);
  assert.match(html, /calls-table/);
  assert.match(html, /口径与提示/);
});

test('renderLog paints a notice instead of a table when there is no data', () => {
  const html = renderLog(null, LOG_STATES.DENIED);
  assert.match(html, /未获得/);
  assert.doesNotMatch(html, /calls-table/);
});

test('an empty page still renders the table, with a stated empty row', () => {
  const log = ok({ calls: [], counts: { ...payload().counts, returned: 0, available: 0 } });
  const html = renderLog(log, LOG_STATES.EMPTY);
  // Atelier 05 tables have no dedicated empty class: the row is an ordinary
  // muted cell spanning the header count.
  assert.match(html, /colspan="11"/);
  assert.match(html, /没有匹配的调用/);
});

// --- query building ---------------------------------------------------------

test('unset filters are omitted rather than sent empty', () => {
  assert.equal(buildQuery({}), '');
  assert.equal(buildQuery({ outcome: 'all' }), '');
  assert.equal(buildQuery({ outcome: '', key: null, model: '' }), '');
});

test('set filters are encoded onto the query string', () => {
  const query = buildQuery({
    from: '2026-10-14T01:00:00Z',
    to: '2026-10-14T04:00:00Z',
    key: 'key-alpha',
    outcome: 'failed',
    model: 'llm-a',
    limit: 10,
  });
  const params = new URLSearchParams(query);
  assert.equal(params.get('from'), '2026-10-14T01:00:00Z');
  assert.equal(params.get('to'), '2026-10-14T04:00:00Z');
  assert.equal(params.get('key'), 'key-alpha');
  assert.equal(params.get('outcome'), 'failed');
  assert.equal(params.get('model'), 'llm-a');
  assert.equal(params.get('limit'), '10');
  assert.equal(params.get('before_ts'), null);
});

test('the cursor is carried verbatim, as a pair', () => {
  const query = buildQuery({ cursor: { ts: '2026-10-14T03:00:00Z', call_id: 'call_x' } });
  const params = new URLSearchParams(query);
  assert.equal(params.get('before_ts'), '2026-10-14T03:00:00Z');
  assert.equal(params.get('before_call_id'), 'call_x');
});

test('a hostile value is encoded, not concatenated', () => {
  const query = buildQuery({ model: 'llm-a&outcome=success' });
  assert.ok(!query.includes('&outcome=success'), query);
  assert.equal(new URLSearchParams(query).get('model'), 'llm-a&outcome=success');
});

test('a local datetime-local value converts to an explicit UTC instant', () => {
  assert.equal(localInputToUtcIso(''), null);
  assert.equal(localInputToUtcIso('   '), null);
  assert.equal(localInputToUtcIso('nonsense'), null);
  const iso = localInputToUtcIso('2026-10-14T01:30');
  // The widget's value means *local* time, so the assertion is on the instant,
  // not on the wall-clock string: the runner's timezone is not fixed.
  assert.match(iso, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$/);
  assert.equal(new Date(iso).getTime(), new Date('2026-10-14T01:30').getTime());
});

test('a UTC instant round-trips back into a datetime-local value', () => {
  const local = utcIsoToLocalInput('2026-10-14T01:30:00Z');
  assert.match(local, /^2026-10-14T\d{2}:30$/);
  assert.equal(utcIsoToLocalInput(null), null);
  assert.equal(utcIsoToLocalInput('nope'), null);
});

// --- empty state ------------------------------------------------------------

test('an empty page state carries no data and no fabricated rows', () => {
  const state = emptyLog(LOG_STATES.SIGNED_OUT, 'http-401');
  assert.deepEqual(state.calls, []);
  assert.equal(state.transport, null);
  assert.equal(state.reason, 'http-401');
  // Crucially *not* `EMPTY`: "signed out" says nothing about your calls.
  assert.equal(stateFor(state), LOG_STATES.SIGNED_OUT);
  assert.equal(stateFor(emptyLog(LOG_STATES.EMPTY)), LOG_STATES.EMPTY);
  assert.equal(stateFor(emptyLog(LOG_STATES.UNKNOWN)), LOG_STATES.UNKNOWN);
});
