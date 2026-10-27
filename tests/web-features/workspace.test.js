/*! tests/web-features/workspace.test.js — V6.1 workspace layer.
 *
 *  Covers the new interactive surfaces and, more importantly, the honesty rules
 *  they must keep: a demo fixture is never READY, a missing measurement is never
 *  zero, an export never carries content or a credential, a write control with
 *  no verified contract is disabled, and the route matrix cannot drift from the
 *  BFF's declared route map.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { validateCallLog, LOG_STATES } from '../../web/js/calls/contract.js';
import {
  answerDemoCalls, buildDemoCalls, createDemoCallsFetch, DEMO_KEYS, DEMO_MODELS,
} from '../../web/js/calls/demo-fixture.js';
import {
  percentile, computeInsights, callsToCsv, callsToJson, csvCell, exportName,
  renderInsights, renderCallDetail, EXPORT_COLUMNS,
} from '../../web/js/calls/insights.js';
import { createCalls, presetRange, sourceBadgeText } from '../../web/js/calls/controller.js';
import { renderLog } from '../../web/js/calls/render-log.js';
import {
  deriveReadiness, renderReadiness, hostState, opsState, callsState, atlasState, WORKSPACES, WS_STATES,
} from '../../web/js/workspace/readiness.js';
import {
  validateTokenList, tokensStateFor, tokenLifecycle, renderTokens, demoTokens,
} from '../../web/js/workspace/cli-access.js';
import { LIFECYCLE_STEPS, sdkSnippets, renderSdk } from '../../web/js/workspace/lifecycle.js';
import { BFF_ROUTES, BFF_PENDING_ROWS, renderRoutes } from '../../web/js/workspace/routes.js';

const REPO = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const read = (p) => readFileSync(resolve(REPO, p), 'utf8');
const ANCHOR = Date.parse('2026-10-16T12:00:00Z');
const page = (url) => answerDemoCalls(url, ANCHOR).body.data.call_log;

// --- calls demo fixture ------------------------------------------------------

test('the demo call log satisfies the real atlas-calls/1 contract and says it is synthetic', () => {
  const log = page('/api/atlas/calls?limit=200');
  const checked = validateCallLog(log);
  assert.deepEqual(checked.errors, []);
  assert.equal(log.synthetic, true);
  assert.equal(log.transport.mode, 'demo');
  assert.equal(log.transport.gateway_attached, false);
  assert.equal(log.content.included, false);
  assert.ok(log.calls.length > 20, 'a 24h default window has rows');
  for (const row of log.calls) {
    assert.match(row.model, /^demo-/);
    assert.match(row.key_id, /^demo-/);
    assert.match(row.project, /^demo-/);
  }
  assert.match(log.scope.subject, /^demo-/);
});

test('the demo history is deterministic for one anchor and never in the future', () => {
  const a = buildDemoCalls(ANCHOR);
  const b = buildDemoCalls(ANCHOR);
  assert.deepEqual(a, b);
  for (const row of a) assert.ok(Date.parse(row.ts) < ANCHOR);
  for (let i = 1; i < a.length; i += 1) assert.ok(a[i - 1].ts >= a[i].ts, 'newest first');
});

test('demo filters narrow the rows the way the real query does', () => {
  const failed = page('/api/atlas/calls?outcome=failed&limit=200');
  assert.ok(failed.calls.length > 0);
  assert.ok(failed.calls.every((r) => r.outcome === 'failed'));
  const model = page(`/api/atlas/calls?model=${DEMO_MODELS[2]}&limit=200`);
  assert.ok(model.calls.every((r) => r.model === DEMO_MODELS[2]));
  const key = page(`/api/atlas/calls?key=${DEMO_KEYS[1]}&limit=200`);
  assert.ok(key.calls.every((r) => r.key_id === DEMO_KEYS[1]));
  assert.equal(answerDemoCalls('/api/atlas/calls?key=someone-else', ANCHOR).status, 403,
    'a key outside the caller scope is refused, not filtered to empty');
});

test('keyset pagination walks the window with no gap and no repeat', () => {
  const seen = [];
  let url = '/api/atlas/calls?limit=40';
  let first = null;
  for (let guard = 0; guard < 20; guard += 1) {
    const log = page(url);
    first ??= log;
    seen.push(...log.calls.map((r) => r.call_id));
    if (!log.counts.truncated) break;
    const { ts, call_id: id } = log.counts.next_before;
    url = `/api/atlas/calls?limit=40&before_ts=${encodeURIComponent(ts)}&before_call_id=${id}`;
  }
  assert.equal(new Set(seen).size, seen.length, 'no repeated rows');
  assert.equal(seen.length, first.counts.available, 'every matching row reached');
});

test('the demo fetch is fetch-shaped and never touches the network', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('network used'); };
  try {
    const f = createDemoCallsFetch({ now: () => ANCHOR });
    const res = await f('/api/atlas/calls?limit=5');
    assert.equal(res.status, 200);
    assert.equal((await res.json()).data.call_log.calls.length, 5);
  } finally {
    globalThis.fetch = realFetch;
  }
});

// --- calls insights / exports ------------------------------------------------

test('percentile is nearest-rank and a missing value is excluded, not zero', () => {
  assert.equal(percentile([], 50), null);
  assert.equal(percentile([null, undefined], 95), null);
  assert.equal(percentile([10, 20, 30, 40], 50), 20);
  assert.equal(percentile([10, 20, 30, 40], 95), 40);
  assert.equal(percentile([5, null, 15], 50), 5);
});

test('insights count only measured rows and keep an explicit denominator', () => {
  const rows = page('/api/atlas/calls?limit=200').calls;
  const s = computeInsights(rows);
  assert.equal(s.rows, rows.length);
  assert.equal(s.outcomes.success + s.outcomes.failed + s.outcomes.cancelled, rows.length);
  assert.equal(s.latencyKnown, rows.filter((r) => Number.isFinite(r.total_ms)).length);
  assert.equal(s.byModel.reduce((n, m) => n + m.count, 0), rows.length);
  assert.equal(computeInsights([]).successRate, null, 'no rows is unknown, not 0%');
  const html = renderInsights({ calls: rows });
  assert.match(html, /已加载/);
  assert.match(html, /不当作 0/);
});

test('CSV/JSON exports carry metadata columns only, and neutralise formulas', () => {
  const log = page('/api/atlas/calls?limit=10');
  const csv = callsToCsv(log);
  assert.equal(csv.split('\r\n')[0], EXPORT_COLUMNS.join(','));
  assert.equal(csv.trim().split('\r\n').length, 11);
  for (const banned of ['prompt', 'completion', 'messages', 'content', 'secret']) {
    assert.ok(!EXPORT_COLUMNS.includes(banned));
  }
  assert.equal(csvCell('=HYPERLINK("x")'), '"\'=HYPERLINK(""x"")"');
  assert.equal(csvCell('@SUM(A1)'), "'@SUM(A1)");
  assert.equal(csvCell(-12), '-12', 'a negative number stays a number');
  assert.equal(csvCell(null), '');
  const json = JSON.parse(callsToJson(log, { exportedAt: '2026-10-16T00:00:00Z' }));
  assert.equal(json.meta.synthetic, true);
  assert.equal(json.meta.transport, 'demo');
  assert.equal(json.calls.length, 10);
  assert.deepEqual(Object.keys(json.calls[0]), [...EXPORT_COLUMNS]);
  assert.match(exportName(log, 'csv', new Date(ANCHOR)), /^llmlab_my_calls_DEMO_2026-10-16\.csv$/);
});

test('the detail drawer is metadata-only and flags a demo row', () => {
  const log = page('/api/atlas/calls?limit=3');
  const html = renderCallDetail(log.calls[0], log);
  assert.match(html, /SYNTHETIC \/ DEMO/);
  assert.match(html, /content\.included = false/);
  assert.match(renderLog(log, LOG_STATES.READY), /data-call-id="call_demo_/);
  assert.match(renderLog(log, LOG_STATES.READY), /tabindex="0"/);
});

test('time presets produce a [from, to) pair ending at now', () => {
  const r = presetRange('24h', ANCHOR);
  assert.ok(r.from && r.to);
  assert.equal(Date.parse(r.to) - Date.parse(r.from), 86400_000);
  assert.equal(presetRange('nope', ANCHOR), null);
});

const fakeDoc = { querySelector: () => null, querySelectorAll: () => [] };

test('a route the deployment does not serve is NOT CONFIGURED, with its reason', async () => {
  const c = createCalls({ doc: fakeDoc, fetchImpl: async () => ({ status: 404, ok: false, json: async () => ({}) }) });
  await c.load();
  assert.equal(c.state, LOG_STATES.NOT_CONFIGURED);
  assert.equal(c.reason, 'http-404');
  assert.match(renderLog(null, c.state, c.reason), /http-404/);
  assert.equal(c.exportAs('csv').ok, false, 'nothing to export without rows');
});

test('a 503 from the BFF is NOT CONFIGURED, not an unknown failure', async () => {
  // The BFF answers 503 with `*_adapter_not_attached` when the deployment does
  // not provide the feature (backend/bff/evaluation_adapter.py: "那是「本部署
  // 没有这条功能」，不是「服务崩了」"). Lumping it into UNKNOWN offers a retry
  // for something that will never change, and hides a wiring gap behind an
  // outage. Tokens already read it this way (workspace/cli-access.js); Calls
  // must agree.
  const c = createCalls({ doc: fakeDoc, fetchImpl: async () => ({ status: 503, ok: false, json: async () => ({}) }) });
  await c.load();
  assert.equal(c.state, LOG_STATES.NOT_CONFIGURED,
    '503 means the adapter is not attached, not that the answer is unknown');
  assert.equal(c.reason, 'http-503', 'and it keeps its own reason, distinct from 404');
  assert.match(renderLog(null, c.state, c.reason), /http-503/);
  assert.equal(c.exportAs('csv').ok, false, 'nothing to export without rows');
});

test('the call log says LOADING while asked-but-not-answered', async () => {
  let release;
  const c = createCalls({ doc: fakeDoc, fetchImpl: () => new Promise((r) => { release = r; }) });
  const pending = c.load();
  assert.equal(c.state, LOG_STATES.LOADING);
  release({ status: 500, ok: false, json: async () => ({}) });
  await pending;
  assert.equal(c.state, LOG_STATES.UNKNOWN);
});

test('the hero badge never calls demo rows LIVE', () => {
  assert.match(sourceBadgeText(LOG_STATES.READY, true), /^DEMO/);
  assert.match(sourceBadgeText(LOG_STATES.READY, false), /^LIVE/);
  assert.match(sourceBadgeText(LOG_STATES.NOT_CONFIGURED, false), /未接线/);
});

test('the badge names a 503 by what it is, not as a missing route', () => {
  // "路由不存在" is false for 503: the route exists, the adapter behind it does
  // not. A reader who sees the wrong sentence goes looking for a deploy error.
  const missing = sourceBadgeText(LOG_STATES.NOT_CONFIGURED, false, 'http-404');
  const unattached = sourceBadgeText(LOG_STATES.NOT_CONFIGURED, false, 'http-503');
  assert.match(missing, /路由不存在/);
  assert.doesNotMatch(unattached, /路由不存在/);
  assert.match(unattached, /适配器未装配/);
  assert.notEqual(missing, unattached, 'the two not-configured causes stay distinguishable');
});

// --- readiness board ---------------------------------------------------------

test('the readiness board covers all nine workspaces', () => {
  assert.deepEqual(WORKSPACES.map((w) => w.page),
    ['overview', 'compute', 'models', 'api', 'calls', 'observability', 'atlas', 'evaluation', 'admin']);
});

test('demo is never READY, and stale host data is STALE', () => {
  assert.equal(opsState({ mode: 'mock', state: 'ready' }).state, WS_STATES.DEMO);
  assert.equal(callsState({ state: 'ready', transport: 'demo' }).state, WS_STATES.DEMO);
  assert.equal(atlasState({ state: 'demo', synthetic: true }, 'demo').state, WS_STATES.DEMO);
  assert.equal(atlasState({ state: 'demo', synthetic: true }, 'live').state, WS_STATES.NOT_CONFIGURED);
  assert.equal(hostState({ state: 'live', freshness: 'live' }).state, WS_STATES.READY);
  assert.equal(hostState({ state: 'unknown', freshness: 'stale', reason: 'expired' }).state, WS_STATES.STALE);
  assert.equal(hostState({ state: 'unknown', reason: 'not-yet-fetched' }).state, WS_STATES.LOADING);
  assert.equal(opsState({ mode: 'unavailable', reason: 'auth-bff-not-connected' }).state, WS_STATES.NOT_CONFIGURED);
  assert.equal(opsState({ mode: 'live', state: 'denied' }).state, WS_STATES.DENIED);
  assert.equal(opsState({ mode: 'live', state: 'unknown' }).state, WS_STATES.ERROR);
});

test('a bare preview reports nothing as READY', () => {
  const r = deriveReadiness({
    dataMode: 'live',
    host: { state: 'unknown', freshness: 'unknown', reason: 'request-failed' },
    ops: { mode: 'unavailable', state: 'unknown', reason: 'auth-bff-not-connected' },
    calls: { state: 'not_configured', reason: 'http-404', transport: null },
    atlas: { state: 'demo', synthetic: true },
  });
  assert.equal(r.rows.length, 9);
  assert.equal(r.counts.ready ?? 0, 0);
  const html = renderReadiness(r);
  assert.equal((html.match(/data-ws-go=/g) ?? []).length, 9);
  assert.doesNotMatch(html, /READY · 已验证<\/span>/);
});

// --- scoped tokens -----------------------------------------------------------

test('a token list that carries a plaintext token is refused outright', () => {
  const good = { ok: true, data: { tokens: [{ token_id: 't1', scopes: ['a'] }] } };
  assert.equal(validateTokenList(good).ok, true);
  const leak = { ok: true, data: { tokens: [{ token_id: 't1', scopes: [], token: 'llk_live_x' }] } };
  assert.equal(validateTokenList(leak).ok, false);
  assert.match(validateTokenList(leak).error, /carries-token/);
  assert.equal(validateTokenList({ ok: true, data: {} }).ok, false);
});

test('token states map access answers and absent routes distinctly', () => {
  const ok = validateTokenList({ ok: true, data: { tokens: [] } });
  assert.equal(tokensStateFor(200, ok), WS_STATES.EMPTY);
  assert.equal(tokensStateFor(401, null), WS_STATES.SIGNED_OUT);
  assert.equal(tokensStateFor(403, null), WS_STATES.DENIED);
  assert.equal(tokensStateFor(404, null), WS_STATES.NOT_CONFIGURED);
  assert.equal(tokensStateFor(500, null), WS_STATES.ERROR);
});

test('token writes are rendered disabled with the missing precondition', () => {
  const now = 1_790_000_000;
  const tokens = demoTokens(now);
  assert.ok(tokens.every((t) => t.token_id.startsWith('tok_demo_') && !('token' in t)));
  assert.deepEqual(tokens.map((t) => tokenLifecycle(t, now)), ['active', 'expired', 'rotated']);
  const html = renderTokens({ state: WS_STATES.DEMO, tokens, demo: true, now });
  const buttons = html.match(/<button[^>]*>(签发新 Token|轮换|吊销)<\/button>/g);
  assert.equal(buttons.length, 1 + tokens.length * 2);
  for (const b of buttons) assert.match(b, /disabled aria-disabled="true" title="[^"]*尚未端到端验证/);
  assert.match(html, /SYNTHETIC \/ DEMO/);
});

// --- lifecycle / SDK ---------------------------------------------------------

test('every lifecycle step names a real llmlab subcommand', () => {
  const entry = read('cli/llmlab/entry.py');
  const parsers = new Set([...entry.matchAll(/add_parser\(\s*"([a-z-]+)"/g)].map((m) => m[1]));
  for (const step of LIFECYCLE_STEPS) {
    for (const line of step.cli.split('\n')) {
      const [bin, cmd, sub] = line.trim().split(/\s+/);
      assert.equal(bin, 'llmlab', step.id);
      assert.ok(parsers.has(cmd), `${step.id}: llmlab ${cmd}`);
      if (sub && !sub.startsWith('<')) assert.ok(parsers.has(sub), `${step.id}: llmlab ${cmd} ${sub}`);
    }
  }
  const web = LIFECYCLE_STEPS.filter((s) => s.web).map((s) => s.id);
  assert.deepEqual(web, ['share', 'revoke'], 'only the ops.share route has a web action');
});

test('SDK snippets are placeholders only and sanitise the alias', () => {
  const s = sdkSnippets('my"model$(rm -rf)`');
  for (const text of Object.values(s)) {
    assert.doesNotMatch(text, /https?:\/\/(?!lab\.example)[a-z0-9.-]+\.[a-z]{2,}/i, 'no real host');
    assert.doesNotMatch(text, /:8080|:8200|:9000/);
    assert.doesNotMatch(text, /llk_[a-z0-9]{8,}/i, 'no key-shaped literal');
    assert.ok(!text.includes('$(rm'), 'command substitution stripped');
  }
  assert.match(s.curl, /\$LLMLAB_GATEWAY_URL/);
  assert.match(s.python, /os\.environ\["LLMLAB_KEY"\]/);
  const html = renderSdk('<x>', 'python');
  assert.match(html, /aria-selected="true"[^>]*data-sdk-tab="python"/);
  assert.equal((html.match(/ hidden>/g) ?? []).length, 3);
  assert.ok(!html.includes('<x>'), 'alias is escaped');
});

// --- route matrix ------------------------------------------------------------

/**
 * The route-map contract recorded here independently of `routes.js`: the 17 rows
 * declared by BFF PR #85 (`6062ed39`), `evaluation.report_download` mounted by
 * BFF #92 from Evaluation #78 (PR #91), `portal.events` mounted by BFF #110, and
 * `ops.invoke` mounted by BFF SEAM-1 (PR #115).
 *
 * It exists because the mirror is pinned to a contract a stacked branch may not
 * yet carry: this branch's local `route_map.py` still has 19 rows, so a
 * row-for-row check against it cannot validate `ops.invoke` at all. This pin
 * can — edit it only against the Python table.
 */
const BFF_ROUTE_CONTRACT = [
  { name: 'evaluation.benchmarks.stop', method: 'POST', path: '/api/evaluation/benchmarks/stop', auth: 'session', kind: 'write', scope: null },
  { name: 'evaluation.benchmarks', method: 'GET', path: '/api/evaluation/benchmarks', auth: 'session', kind: 'read', scope: null },
  { name: 'evaluation.benchmarks.submit', method: 'POST', path: '/api/evaluation/benchmarks/submit', auth: 'session', kind: 'write', scope: null },
  { name: 'evaluation.benchmarks.reconcile', method: 'POST', path: '/api/evaluation/benchmarks/reconcile', auth: 'session', kind: 'write', scope: null },
  { name: 'registry.engines.live', method: 'GET', path: '/api/v2/registry/engines/{engine_id}/live', auth: 'session', kind: 'read', scope: null },
  { name: 'ops.read', method: 'GET', path: '/api/v2/ops', auth: 'session', kind: 'read', scope: null },
  { name: 'ops.usage', method: 'GET', path: '/api/v2/ops/usage', auth: 'session', kind: 'read', scope: 'observability:self:read' },
  { name: 'ops.engines', method: 'GET', path: '/api/v2/ops/engines', auth: 'session', kind: 'read', scope: 'observability:self:read' },
  { name: 'ops.share', method: 'PUT', path: '/api/v2/ops/deployments/{deployment_id}/share', auth: 'session', kind: 'write', scope: 'portal:endpoints:share' },
  { name: 'registry.engines.preflight', method: 'POST', path: '/api/v2/registry/engines/preflight', auth: 'session', kind: 'write', scope: 'registry:write' },
  { name: 'registry.engines.register', method: 'POST', path: '/api/v2/registry/engines', auth: 'session', kind: 'write', scope: 'registry:write' },
  { name: 'registry.discovery', method: 'GET', path: '/api/v2/registry/discovery', auth: 'session', kind: 'read', scope: 'registry:read' },
  { name: 'registry.models.register', method: 'POST', path: '/api/v2/registry/models', auth: 'session', kind: 'write', scope: 'registry:write' },
  { name: 'gateway.overview', method: 'GET', path: '/api/gateway/overview', auth: 'session', kind: 'read', scope: null },
  { name: 'gateway.keys', method: 'GET', path: '/api/gateway/keys', auth: 'session', kind: 'read', scope: null },
  { name: 'gateway.keys.create', method: 'POST', path: '/api/gateway/keys/create', auth: 'session', kind: 'write', scope: null },
  { name: 'gateway.keys.revoke', method: 'POST', path: '/api/gateway/keys/{key_id}/revoke', auth: 'session', kind: 'write', scope: null },
  { name: 'gateway.keys.update', method: 'POST', path: '/api/gateway/keys/{key_id}/update', auth: 'session', kind: 'write', scope: null },
  { name: 'gateway.models', method: 'GET', path: '/api/gateway/models', auth: 'session', kind: 'read', scope: null },
  { name: 'gateway.models.publish', method: 'POST', path: '/api/gateway/models/publish', auth: 'session', kind: 'write', scope: null },
  { name: 'gateway.models.update', method: 'POST', path: '/api/gateway/models/{route_id}/update', auth: 'session', kind: 'write', scope: null },
  { name: 'gateway.logs', method: 'GET', path: '/api/gateway/logs', auth: 'session', kind: 'read', scope: null },
  { name: 'gateway.logs.detail', method: 'GET', path: '/api/gateway/logs/{request_id}', auth: 'session', kind: 'read', scope: null },
  { name: 'gateway.playground', method: 'POST', path: '/api/gateway/playground', auth: 'session', kind: 'write', scope: null },
  { name: 'registry.engines.params', method: 'GET', path: '/api/v2/registry/engines/{engine_id}/params', auth: 'session', kind: 'read', scope: 'registry:read' },
  { name: 'registry.engines.params.update', method: 'POST', path: '/api/v2/registry/engines/{engine_id}/params/update', auth: 'session', kind: 'write', scope: 'registry:write' },
  { name: 'registry.engines.serve', method: 'POST', path: '/api/v2/registry/engines/{engine_id}/serve', auth: 'session', kind: 'write', scope: 'registry:write' },
  { name: 'registry.agent_task', method: 'GET', path: '/api/v2/registry/agent-tasks/{task_id}', auth: 'session', kind: 'read', scope: 'registry:read' },
  { name: 'ops.invoke', method: 'POST', path: '/api/v2/ops/endpoints/{endpoint_id}/invoke', auth: 'session', kind: 'write', scope: 'registry:endpoint:invoke' },
  { name: 'portal.infra_summary', method: 'GET', path: '/api/infra/summary', auth: 'session', kind: 'read', scope: 'observability:host:read' },
  { name: 'portal.system_resources', method: 'GET', path: '/api/infra/system', auth: 'session', kind: 'read', scope: 'observability:host:read' },
  { name: 'engine.live', method: 'GET', path: '/api/v2/engine/live', auth: 'session', kind: 'read', scope: 'observability:host:read' },
  { name: 'portal.model_service_key_create', method: 'POST', path: '/api/model-service/keys', auth: 'session', kind: 'write', scope: null },
  { name: 'portal.model_service_key', method: 'GET', path: '/api/model-service/key', auth: 'session', kind: 'read', scope: null },
  { name: 'atlas.calls', method: 'GET', path: '/api/atlas/calls', auth: 'session', kind: 'read', scope: 'atlas:calls:self:read' },
  { name: 'evaluation.report_sources', method: 'GET', path: '/api/evaluation/report-sources', auth: 'session', kind: 'read', scope: 'evaluation:read' },
  { name: 'evaluation.reports', method: 'GET', path: '/api/evaluation/reports', auth: 'session', kind: 'read', scope: 'evaluation:read' },
  { name: 'evaluation.report_download', method: 'GET', path: '/api/evaluation/reports/{report_ref}/download', auth: 'session', kind: 'read', scope: 'evaluation:read' },
  { name: 'evaluation.report_detail', method: 'GET', path: '/api/evaluation/reports/{report_ref}', auth: 'session', kind: 'read', scope: 'evaluation:read' },
  { name: 'portal.services', method: 'GET', path: '/api/portal/services', auth: 'session', kind: 'read', scope: 'observability:host:read' },
  { name: 'portal.events', method: 'GET', path: '/api/portal/events', auth: 'session', kind: 'read', scope: 'portal:admin:read' },
  { name: 'portal.capabilities', method: 'GET', path: '/api/portal/capabilities', auth: 'session', kind: 'read', scope: null },
  { name: 'portal.integrations', method: 'GET', path: '/api/portal/integrations', auth: 'session', kind: 'read', scope: null },
  { name: 'portal.integrations.update', method: 'PUT', path: '/api/portal/integrations/{key}', auth: 'session', kind: 'write', scope: 'portal:admin:read' },
  { name: 'auth.linux.login', method: 'POST', path: '/auth/linux/login', auth: 'anonymous', kind: 'write', scope: null },
  { name: 'auth.linux.status', method: 'GET', path: '/auth/linux/status', auth: 'anonymous', kind: 'read', scope: null },
  { name: 'auth.admin.status', method: 'GET', path: '/auth/admin/status', auth: 'anonymous', kind: 'read', scope: null },
  { name: 'auth.admin.enter', method: 'POST', path: '/auth/admin/enter', auth: 'session', kind: 'write', scope: null },
  { name: 'auth.admin.exit', method: 'POST', path: '/auth/admin/exit', auth: 'session', kind: 'write', scope: null },
  { name: 'auth.logout', method: 'POST', path: '/auth/logout', auth: 'session', kind: 'write', scope: null },
];

test('the admin route matrix mirrors backend/bff/route_map.py exactly', () => {
  const py = read('backend/bff/route_map.py');
  const block = py.slice(py.indexOf('ROUTES: tuple[RouteSpec, ...] = ('), py.indexOf('ROUTES_BY_NAME'));
  const field = (chunk, re) => chunk.match(re)?.[1] ?? null;
  const specs = block.split('RouteSpec(').slice(1).map((chunk) => ({
    name: field(chunk, /name="([^"]+)"/),
    path: field(chunk, /path="([^"]+)"/),
    auth: field(chunk, /auth=AUTH_([A-Z]+)/)?.toLowerCase(),
    kind: field(chunk, /kind=KIND_([A-Z]+)/)?.toLowerCase(),
  }));
  assert.ok(specs.length >= 10, 'parser found the route table');

  // The mirror is pinned to the single BFF route-map contract (39 routes: the
  // 17 from PR #85 @ 6062ed39, `evaluation.report_download` from #92/#91,
  // `portal.events` from #110, `ops.invoke` from SEAM-1 #115, and the two
  // web-registration routes `registry.engines.{preflight,register}`, and the
  // plugin jump-address pair `portal.integrations{,.update}`, plus
  // `registry.discovery` and `registry.models.register`, plus the 11 integrated
  // gateway routes and the per-engine parameter pair). A stacked
  // checkout can predate one of those BFF PRs and carry a shorter table. Two
  // rules keep that honest — neither is a skip, and together they fail on any
  // divergence in either direction:
  //   1. every row the local Python table declares must match the mirror exactly;
  //   2. the rows the mirror has and the local table does not must be EXACTLY
  //      the pending set. Any other unexplained row fails.
  // 合并后只有一个 checkout：39（上游 PR#20 的网关/参数路由）+ 30 那版里上游没有的
  // 引擎实时读数与模型服务密钥，去掉重复项实际 43 条。镜像与 route_map 必须一致。
  assert.equal(BFF_ROUTES.length, 50, 'the mirror is pinned to the 50-route contract');

  // The independent pin: every row's method/path/session/read-write/scope must
  // match the recorded contract, including the rows a stacked checkout cannot
  // see in its own Python table. A typo in any of them fails here, not later.
  assert.deepEqual(
    BFF_ROUTES.map(({ name, method, path, auth, kind, scope }) => (
      { name, method, path, auth, kind, scope }
    )),
    BFF_ROUTE_CONTRACT,
  );

  const mirrorByName = new Map(BFF_ROUTES.map((row) => [row.name, row]));
  for (const spec of specs) {
    const row = mirrorByName.get(spec.name);
    assert.ok(row, `${spec.name} is declared by the BFF but missing from the mirror`);
    assert.deepEqual(
      { name: row.name, path: row.path, auth: row.auth, kind: row.kind }, spec,
      `${spec.name} diverges from the BFF route map`,
    );
  }

  if (specs.length < BFF_ROUTES.length) {
    const pending = BFF_ROUTES
      .filter((row) => !specs.some((spec) => spec.name === row.name))
      .map((row) => row.name)
      .sort();
    assert.deepEqual(pending, [...BFF_PENDING_ROWS].sort(),
      'the only rows not yet in this checkout are the declared pending set');
  } else {
    assert.equal(specs.length, BFF_ROUTES.length,
      'the BFF declared a route the mirror does not carry');
  }

  const html = renderRoutes();
  assert.equal((html.match(/data-route="/g) ?? []).length, BFF_ROUTES.length);
  assert.doesNotMatch(html, /:8080|:8200|:9000/);
});

test('the route matrix declares routes; it never claims one is live or writable', () => {
  // Listing a route is a statement about the contract, not a capability. No row
  // may read as LIVE, and nothing here may render as an enabled control — the
  // write rows say "写 · CSRF", which is a boundary, not a button.
  const html = renderRoutes();
  assert.doesNotMatch(html, /\bLIVE\b/);
  assert.doesNotMatch(html, /data-state="(?:live|ready)"/);
  assert.doesNotMatch(html, /<button/);
  const writes = BFF_ROUTES.filter((row) => row.kind === 'write').length;
  assert.equal((html.match(/写 · CSRF/g) ?? []).length, writes);
});

// --- generated shell ---------------------------------------------------------

test('the shipped calls view carries the new filter, preset and export controls', () => {
  const html = read('web/index.html');
  for (const id of ['callsFilters', 'callsWindow', 'callsExportCsv', 'callsExportJson', 'callsApply', 'callsReset']) {
    assert.match(html, new RegExp(`id="${id}"`), id);
  }
  assert.equal((html.match(/data-calls-preset="/g) ?? []).length, 3);
  assert.match(html, /id="callsExportCsv" disabled/);
  assert.match(html, /id="callsExportJson" disabled/);
});

test('the module catalogue reports a missing route as NOT CONFIGURED with its reason', async () => {
  const { createModules } = await import('../../web/js/modules/controller.js');
  const m = createModules({ doc: fakeDoc, fetchImpl: async () => ({ status: 404, ok: false, json: async () => ({}) }) });
  const r = await m.load();
  assert.equal(r.state, 'not_configured');
  assert.equal(r.reason, 'http-404');
});

test('the module catalogue reads a 503 the same way Calls and tokens do', async () => {
  const { createModules } = await import('../../web/js/modules/controller.js');
  const m = createModules({ doc: fakeDoc, fetchImpl: async () => ({ status: 503, ok: false, json: async () => ({}) }) });
  const r = await m.load();
  assert.equal(r.state, 'not_configured',
    'an unattached adapter is a deployment fact, not an unknown answer');
  assert.equal(r.reason, 'http-503');
});
