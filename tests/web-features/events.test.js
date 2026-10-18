/*! tests/web-features/events.test.js — the operator audit timeline.
 *
 *  Two halves, both testable under plain Node:
 *
 *  1. **What may be rendered.** `portal-events/1` is a whitelist contract. The
 *     validator drops any field the contract does not name, and the renderer
 *     keeps the four source states apart (`ok` / `empty` / `unconfigured` /
 *     `unreachable`) instead of drawing all of them as a blank table.
 *  2. **Who may ask at all.** Only a session whose server-asserted scopes carry
 *     `portal:admin:read` issues the request. A member gets a denial with no
 *     request and no rows. A scope change (elevation expiry, logout, revoke)
 *     clears whatever was cached and discards an answer already on the wire.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  EVENT_STATES, validateEventsDto, eventsOverallState, renderEventsSection,
} from '../../web/js/features/events.js';
import {
  SOURCE_MODES, createFeatureSource,
} from '../../web/js/features/source.js';
import { createOps } from '../../web/js/features/controller.js';
import { renderAdmin } from '../../web/js/features/render.js';
import { deriveCapabilities } from '../../web/js/features/capability.js';

const emptyDoc = () => ({
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => { throw new Error('nothing should be created against an empty document'); },
});

const dto = (overrides = {}) => ({
  schema_version: 'portal-events/1',
  observed_at: '2026-10-18T00:00:00Z',
  limit: 25,
  sources: [
    {
      source: 'elevation', state: 'ok', reason: null,
      observed_at: '2026-10-18T00:00:00Z', newest_at: '2026-10-18T00:00:00Z',
      count: 1, truncated: false,
      items: [{
        at: '2026-10-18T00:00:00Z', event: 'enter', outcome: 'ok', actor: 'ops-a',
        principal: 'p-a', uid: '1001', linux_user: 'ops-a', admin_mode: true,
        detail: 'SECRET-INTERNAL', extra: 'SHOULD-NOT-RENDER',
      }],
    },
    {
      source: 'registry', state: 'empty', reason: null,
      observed_at: '2026-10-18T00:00:00Z', newest_at: null,
      count: 0, truncated: false, items: [],
    },
  ],
  notes: ['只列白名单字段。'],
  ...overrides,
});

// --- 1. the DTO contract ----------------------------------------------------

test('only whitelisted event fields survive validation', () => {
  const checked = validateEventsDto(dto());
  assert.equal(checked.ok, true, checked.errors.join('; '));
  const elevation = checked.dto.sources.find((s) => s.source === 'elevation');
  assert.deepEqual(Object.keys(elevation.items[0]).sort(),
    ['admin_mode', 'at', 'actor', 'event', 'linux_user', 'outcome', 'principal', 'uid'].sort());
  assert.equal(JSON.stringify(checked.dto).includes('SECRET-INTERNAL'), false);
  assert.equal(JSON.stringify(checked.dto).includes('SHOULD-NOT-RENDER'), false);
});

test('a payload from another schema, or with an unknown state, is refused', () => {
  assert.equal(validateEventsDto(dto({ schema_version: 'portal-events/2' })).ok, false);
  const bad = dto();
  bad.sources[0] = { ...bad.sources[0], state: 'live' };
  assert.equal(validateEventsDto(bad).ok, false, 'an invented state must not render');
  const unknownSource = dto();
  unknownSource.sources[0] = { ...unknownSource.sources[0], source: 'journalctl' };
  assert.equal(validateEventsDto(unknownSource).ok, false, 'no arbitrary ledger name');
  assert.equal(validateEventsDto(dto({ limit: 1000 })).ok, false, 'limit stays bounded');
  assert.equal(validateEventsDto(null).ok, false);
});

test('the two ledgers keep their own state; empty is not unconfigured', () => {
  const ok = dto();
  assert.equal(eventsOverallState(validateEventsDto(ok).dto), 'ready');

  const empty = dto();
  empty.sources = empty.sources.map((s) => ({ ...s, state: EVENT_STATES.EMPTY, items: [], count: 0 }));
  assert.equal(eventsOverallState(validateEventsDto(empty).dto), EVENT_STATES.EMPTY);

  const off = dto();
  off.sources = off.sources.map((s) => ({ ...s, state: EVENT_STATES.UNCONFIGURED, items: [], count: 0 }));
  assert.equal(eventsOverallState(validateEventsDto(off).dto), EVENT_STATES.UNCONFIGURED);

  const broken = dto();
  broken.sources[0] = { ...broken.sources[0], state: EVENT_STATES.UNREACHABLE, items: [], count: 0 };
  assert.equal(eventsOverallState(validateEventsDto(broken).dto), 'unknown');
});

// --- rendering --------------------------------------------------------------

test('the timeline renders rows from the contract only, and never claims LIVE', () => {
  const { dto: valid } = validateEventsDto(dto());
  const html = renderEventsSection({ state: 'ready', dto: valid, reason: null });
  assert.match(html, /审计时间线/);
  assert.match(html, /data-events="elevation"/, 'the ledger with rows renders a table');
  assert.match(html, /data-event-state="registry:empty"/, 'an empty ledger says so instead of a table');
  assert.match(html, /确实为空/);
  assert.doesNotMatch(html, /SECRET-INTERNAL|SHOULD-NOT-RENDER/);
  assert.doesNotMatch(html, /\bLIVE\b/, 'a declared route is not a live claim');
  assert.doesNotMatch(html, /<button|<input|<select/, 'the section renders no controls');
});

test('every failure state is rendered as itself, not as an empty table', () => {
  const denied = renderEventsSection({ state: 'denied', dto: null, reason: 'http-403' });
  assert.match(denied, /portal:admin:read/);
  assert.doesNotMatch(denied, /<table/);
  assert.doesNotMatch(denied, /<button/);

  const out = renderEventsSection({ state: 'signed_out', dto: null, reason: 'http-401' });
  assert.match(out, /请先登录/);
  assert.doesNotMatch(out, /<table/);

  const off = renderEventsSection({ state: 'not_configured', dto: null, reason: 'http-503' });
  assert.match(off, /本部署未提供/);
  assert.match(off, /http-503/);
  assert.doesNotMatch(off, /没有事件/, 'must not claim the ledger is empty');

  const unknown = renderEventsSection({ state: 'unknown', dto: null, reason: 'request-failed' });
  assert.match(unknown, /暂时无法确定/);
  assert.doesNotMatch(unknown, /<table/);
});

test('the audit section is part of the Administration panel, not a tenth view', () => {
  const capabilities = deriveCapabilities({ scopes: ['portal:admin:read'] });
  const ops = { scope: { scopes: ['portal:admin:read'] }, admin: { available: false, principals: [] } };
  const withRows = renderAdmin(ops, capabilities, {
    events: { state: 'ready', dto: validateEventsDto(dto()).dto, reason: null },
  });
  assert.match(withRows, /审计时间线/);
  assert.match(withRows, /OPERATOR AUDIT/);
  assert.match(withRows, /data-events="elevation"/);
  assert.doesNotMatch(withRows, /\bLIVE\b/);

  const denied = renderAdmin(ops, deriveCapabilities({ scopes: [] }), {
    events: { state: 'denied', dto: null, reason: 'missing-scope:portal:admin:read' },
  });
  assert.match(denied, /需要操作员范围/);
  assert.doesNotMatch(denied, /<table[^>]*data-events/);
});

// --- 2. who may ask ---------------------------------------------------------

const opsWithScopes = (scopes) => ({
  mode: SOURCE_MODES.LIVE,
  load: async () => ({
    state: 'ready',
    ops: { scope: { scopes }, admin: { available: false, reason: 'missing_required_scope', principals: [] } },
    reason: null,
    errors: [],
  }),
  loadEvents: async () => { throw new Error('a member must not reach the events endpoint'); },
});

test('an ordinary member gets a denial, no rows, and no request', async () => {
  const controller = createOps({
    doc: emptyDoc(),
    source: opsWithScopes(['portal:modules:read']),
  });
  await controller.load();
  assert.equal(controller.events.state, 'denied');
  assert.equal(controller.events.dto, null);
  assert.match(controller.events.reason, /portal:admin:read/);
});

test('an operator asks once for the bounded timeline', async () => {
  const calls = [];
  const session = opsWithScopes(['portal:modules:read', 'portal:admin:read']);
  session.loadEvents = async () => {
    calls.push(1);
    return { state: 'ready', reason: null, dto: validateEventsDto(dto()).dto };
  };
  const controller = createOps({ doc: emptyDoc(), source: session });
  await controller.load();
  assert.equal(calls.length, 1, 'exactly one audit read');
  assert.equal(controller.events.state, 'ready');
  assert.equal(controller.events.dto.sources.length, 2);
});

test('a scope change clears the cached timeline and discards an in-flight answer', async () => {
  let release;
  const session = opsWithScopes(['portal:modules:read', 'portal:admin:read']);
  session.loadEvents = () => new Promise((resolve) => { release = resolve; });
  const controller = createOps({ doc: emptyDoc(), source: session });

  const pending = controller.load(); // reaches loadEvents() once the scopes are known
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(typeof release, 'function', 'the operator read was issued');

  controller.invalidate();
  assert.equal(controller.events.dto, null, 'invalidate clears the cache immediately');
  assert.equal(controller.events.reason, 'session-scope-changed');

  release({ state: 'ready', reason: null, dto: validateEventsDto(dto()).dto });
  await pending;
  assert.equal(controller.events.dto, null, 'a superseded answer never repaints');
});

// --- the request the UI is allowed to make ----------------------------------

test('the audit request is one fixed same-origin path with a bounded limit', async () => {
  const seen = [];
  const source = createFeatureSource({
    mode: SOURCE_MODES.LIVE,
    base: '',
    fetchImpl: async (url, init) => {
      seen.push({ url, init });
      return { ok: true, status: 200, json: async () => ({ data: dto() }) };
    },
  });
  const answer = await source.loadEvents();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, '/api/portal/events?limit=25');
  assert.equal(seen[0].init.credentials, 'same-origin');
  assert.deepEqual(seen[0].init.headers, { Accept: 'application/json' });
  assert.equal(answer.state, 'ready');

  // A caller-supplied limit cannot escape the contract, and no other parameter
  // (uid / owner / path / url / promql / source) is ever appended.
  seen.length = 0;
  await source.loadEvents({ limit: 10_000 });
  assert.equal(seen[0].url, '/api/portal/events?limit=25');
});

test('the audit status ladder matches the rest of the ops layer', async () => {
  const answerFor = async (status) => {
    const source = createFeatureSource({
      mode: SOURCE_MODES.LIVE,
      base: '',
      fetchImpl: async () => ({ ok: false, status, json: async () => ({}) }),
    });
    return source.loadEvents();
  };
  assert.equal((await answerFor(401)).state, 'signed_out');
  assert.equal((await answerFor(403)).state, 'denied');
  assert.equal((await answerFor(404)).state, 'not_configured');
  assert.equal((await answerFor(501)).state, 'not_configured');
  const refused = await answerFor(503);
  assert.equal(refused.state, 'not_configured');
  assert.equal(refused.reason, 'http-503');
  assert.equal((await answerFor(500)).state, 'unknown');
});

test('a non-live source never opens a socket for the timeline', async () => {
  for (const mode of [SOURCE_MODES.MOCK, SOURCE_MODES.UNAVAILABLE]) {
    let called = false;
    const source = createFeatureSource({
      mode, base: '', fetchImpl: async () => { called = true; return { ok: true, status: 200, json: async () => ({}) }; },
    });
    const answer = await source.loadEvents();
    assert.equal(answer.state, 'unconfigured', `${mode} must not invent audit rows`);
    assert.equal(called, false, `${mode} must not touch the network`);
  }
});
