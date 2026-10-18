/*! tests/modules-view.test.js — the Portal module layer's browser-side contract.
 *
 *  Two pure layers, both covered here:
 *
 *  * `web/js/modules/contract.js` — the payload shield. The tests below check
 *    what must *fail*: a link that leaves the origin or names an upstream port,
 *    a GPU number, an `available` module with nothing to show, a live claim on
 *    synthetic data.
 *  * `web/js/modules/render.js` — the markup, as strings, so no browser is
 *    needed (this build has none; see `web/README.md`).
 *
 *  Fixtures are built inline so the hostile shapes are exact.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MODULE_STATES,
  PORTAL_SCHEMA,
  PORTAL_STATES,
  TRANSPORT_MODES,
  VISIBILITY,
  emptyPortal,
  factLabel,
  isBlockedLink,
  isInternalHref,
  moduleById,
  reasonLabel,
  requirements,
  stateFor,
  validatePortal,
} from '../web/js/modules/contract.js';
import {
  panelStateText,
  renderEndpoints,
  renderModule,
  renderModulePanel,
  renderNotice,
  renderPlaygroundNote,
  renderTransportStrip,
} from '../web/js/modules/render.js';

// --- fixtures ---------------------------------------------------------------

const SCOPES = ['portal:modules:read', 'portal:engines:native', 'atlas:calls:self:read'];

const metric = (over = {}) => ({
  name: 'gateway_requests_24h',
  label: '24h 调用数',
  unit: '次',
  value: null,
  state: MODULE_STATES.UNKNOWN,
  source: null,
  reason: 'no_verified_metric_source',
  ...over,
});

const module_ = (over = {}) => ({
  id: 'engines',
  title: '引擎与部署',
  detail: '推理引擎类别、部署状态与原生控制台入口。',
  state: MODULE_STATES.AVAILABLE,
  reason: null,
  requires_scopes: [],
  requires: [],
  data: { engine: 'strata' },
  metrics: [],
  links: [],
  ...over,
});

const payload = (over = {}) => ({
  source: 'synthetic-preview',
  synthetic: true,
  schema_version: PORTAL_SCHEMA,
  transport: { mode: TRANSPORT_MODES.DEMO, gateway_attached: false, synthetic: true, label: 'DEMO' },
  scope: {
    subject: 'local:abc',
    roles: ['user'],
    scopes: SCOPES,
    projects: ['proj-a'],
    visibility: VISIBILITY.PRIVATE,
    visible_modules: ['engines'],
  },
  deployment: {
    engine: 'strata',
    gateway_attached: false,
    attached_sources: [],
    native_console_ready: false,
    facts: { engine_is_strata: true, gateway_attached: false, has_gpu_source: false },
  },
  endpoints: { visibility: VISIBILITY.PRIVATE, can_publish_shared: true, items: [] },
  modules: [module_()],
  display_hints: ['SYNTHETIC / DEMO 数据'],
  notes: ['模块状态由服务端判定。'],
  ...over,
});

const ok = (over = {}) => {
  const result = validatePortal(payload(over));
  assert.deepEqual(result.errors, [], 'the fixture itself must be valid');
  assert.equal(result.ok, true);
  return result.portal;
};

const refuses = (over = {}) => {
  const result = validatePortal(payload(over));
  assert.equal(result.ok, false, `expected a refusal, got ${JSON.stringify(result.portal)}`);
  return result.errors;
};

// --- contract: what must be refused -----------------------------------------

test('a well-formed payload passes', () => {
  assert.equal(validatePortal(payload()).ok, true);
});

test('a link that leaves the origin is refused', () => {
  for (const href of ['https://lab.example/gw', '//evil.example/x', 'http://127.0.0.1/x']) {
    const errors = refuses({ modules: [module_({ links: [{ href, label: 'x', kind: 'native', requires: 'y' }] })] });
    assert.match(errors.join(' '), /not a same-origin path/, href);
  }
});

test('a link naming an upstream port is refused', () => {
  const errors = refuses({
    modules: [module_({ links: [{ href: '/proxy/127.0.0.1:8080/v1', label: 'x', kind: 'native', requires: 'y' }] })],
  });
  assert.match(errors.join(' '), /not a same-origin path/);
});

test('the upstream-port rule is about a port, not about the digits', () => {
  assert.equal(isInternalHref('/gw/strata/chat'), true);
  assert.equal(isInternalHref('/8080/v1'), true);
  assert.equal(isInternalHref('/x:8080'), false);
  assert.equal(isInternalHref('//x'), false);
});

test('a blocked link must name what it needs, and carries no address', () => {
  // The server emits `requires` (what is missing) *and* `blocked` (why). Both
  // are asserted here so a fixture cannot be "fixed" by dropping one.
  const legal = validatePortal(
    payload({
      modules: [
        module_({
          links: [
            { href: null, label: '原生 Chat', kind: 'native', requires: 'portal:engines:native', blocked: 'missing_required_scope' },
          ],
        }),
      ],
    })
  );
  assert.deepEqual(legal.errors, [], 'a blocked entry is legal when it names a requirement');
  assert.equal(legal.ok, true);

  const bad = refuses({ modules: [module_({ links: [{ href: null, label: 'x', kind: 'native' }] })] });
  assert.match(bad.join(' '), /names no requirement/);
  assert.equal(isBlockedLink({ href: null }), true);
  assert.equal(isBlockedLink({ href: '/gw' }), false);
});

test('a GPU metric may never carry a value in this build', () => {
  const errors = refuses({
    modules: [
      module_({
        metrics: [metric({ name: 'gpu_vram_used_mib', value: 4096, state: MODULE_STATES.AVAILABLE, source: 'prometheus-readonly', reason: null })],
      }),
    ],
  });
  assert.match(errors.join(' '), /GPU metric/);
});

test('a metric with a value and no source is refused', () => {
  const errors = refuses({
    modules: [module_({ metrics: [metric({ value: 12, state: MODULE_STATES.AVAILABLE, source: null, reason: null })] })],
  });
  assert.match(errors.join(' '), /without state\/source/);
});

test('an available module with nothing to show is refused', () => {
  const errors = refuses({
    modules: [module_({ metrics: [], links: [], data: {} })],
  });
  assert.match(errors.join(' '), /nothing to show/);
});

test('an available module whose scopes the session lacks is refused', () => {
  const errors = refuses({
    modules: [module_({ requires_scopes: ['portal:admin:read'] })],
  });
  assert.match(errors.join(' '), /claims available without/);
});

test('a non-available module without a reason is refused', () => {
  const errors = refuses({
    modules: [module_({ state: MODULE_STATES.DISABLED, reason: null })],
  });
  assert.match(errors.join(' '), /states no reason/);
});

test('a synthetic payload may never claim to be live', () => {
  const errors = refuses({
    transport: { mode: TRANSPORT_MODES.LIVE, gateway_attached: true, synthetic: true },
  });
  assert.match(errors.join(' '), /may not claim to be live/);
});

test('a live payload must state that the gateway is attached', () => {
  const errors = refuses({
    synthetic: false,
    transport: { mode: TRANSPORT_MODES.LIVE, gateway_attached: false, synthetic: false },
  });
  assert.match(errors.join(' '), /gateway is attached/);
});

test('a shared endpoint leaking into a private view is refused', () => {
  const errors = refuses({
    endpoints: {
      visibility: VISIBILITY.PRIVATE,
      can_publish_shared: true,
      items: [{ endpoint_id: 'e', name: 'n', project: 'p', protocol: 'x', model: 'm', owner: 'shared', scope: VISIBILITY.SHARED }],
    },
  });
  assert.match(errors.join(' '), /leaked into a private view/);
});

test('an unknown schema version is refused rather than parsed', () => {
  const errors = refuses({ schema_version: 'atlas-calls/1' });
  assert.match(errors.join(' '), /unsupported schema_version/);
});

test('a non-object payload is refused', () => {
  for (const value of [null, undefined, 7, 'text', []]) {
    assert.equal(validatePortal(value).ok, false, String(value));
  }
});

// --- contract: derived display values ---------------------------------------

test('a declared non-ready state wins over the module list', () => {
  assert.equal(stateFor(emptyPortal(PORTAL_STATES.SIGNED_OUT)), PORTAL_STATES.SIGNED_OUT);
  assert.equal(stateFor(emptyPortal(PORTAL_STATES.DENIED)), PORTAL_STATES.DENIED);
  assert.equal(stateFor(emptyPortal(PORTAL_STATES.UNKNOWN)), PORTAL_STATES.UNKNOWN);
  assert.equal(stateFor(ok()), PORTAL_STATES.READY);
  assert.equal(stateFor(null), PORTAL_STATES.UNKNOWN);
});

test('reasons and facts render in the reader\'s language, and unknown ones survive', () => {
  assert.equal(reasonLabel('missing_required_scope'), '缺少所需权限（scope）');
  assert.equal(reasonLabel('something_new'), 'something_new');
  assert.equal(reasonLabel(null), '');
  assert.equal(factLabel('gateway_attached'), 'Gateway 已接线');
  assert.equal(factLabel('brand_new_fact'), 'brand_new_fact');
});

test('the readiness checklist reports each precondition separately', () => {
  // A module that is *not* available is exactly the case the checklist exists
  // for: it has to say which precondition is missing.
  const portal = ok({
    modules: [
      module_({
        state: MODULE_STATES.UNAVAILABLE,
        reason: 'missing_required_scope',
        requires_scopes: ['portal:admin:read'],
        requires: ['gateway_attached', 'engine_is_strata'],
      }),
    ],
  });
  const rows = requirements(moduleById(portal, 'engines'), portal.deployment, new Set(portal.scope.scopes));
  assert.deepEqual(
    rows.map((row) => [row.name, row.met]),
    [['portal:admin:read', false], ['gateway_attached', false], ['engine_is_strata', true]]
  );
});

test('an unknown module id resolves to null rather than throwing', () => {
  assert.equal(moduleById(ok(), 'nope'), null);
});

// --- rendering --------------------------------------------------------------

test('a module card states its state, its reason and its checklist', () => {
  const portal = ok({
    modules: [module_({ state: MODULE_STATES.UNKNOWN, reason: 'no_verified_metric_source', data: {} })],
  });
  const html = renderModulePanel(portal, 'engines');
  assert.match(html, /state-pill state-unknown/);
  assert.match(html, /没有已声明的可信数据来源/);
  assert.match(html, /no_verified_metric_source/);
});

test('an unknown metric renders as an em dash and says why, never as zero', () => {
  const portal = ok({ modules: [module_({ metrics: [metric()] })] });
  const html = renderModulePanel(portal, 'engines');
  assert.match(html, /is-unknown/);
  assert.match(html, /—/);
  assert.doesNotMatch(html, />\s*0\s*次\s*</);
});

test('a measured metric shows its value and names its source', () => {
  const portal = ok({
    modules: [
      module_({
        metrics: [metric({ value: 812, state: MODULE_STATES.AVAILABLE, source: 'prometheus-readonly', reason: null })],
      }),
    ],
  });
  const html = renderModulePanel(portal, 'engines');
  assert.match(html, /812/);
  assert.match(html, /prometheus-readonly/);
});

test('a permitted link is rendered as a link to a same-origin path', () => {
  const portal = ok({
    modules: [module_({ links: [{ href: '/gw/strata/chat', label: '原生 Chat', kind: 'native', requires: 'x' }] })],
  });
  const html = renderModulePanel(portal, 'engines');
  assert.match(html, /href="\/gw\/strata\/chat"/);
});

test('a blocked link renders as a disabled explanation, not as an anchor', () => {
  const portal = ok({
    modules: [
      module_({
        links: [
          { href: null, label: '原生 Chat', kind: 'native', requires: 'portal:engines:native', blocked: 'missing_required_scope' },
        ],
      }),
    ],
  });
  const html = renderModulePanel(portal, 'engines');
  assert.match(html, /aria-disabled="true"/);
  assert.doesNotMatch(html, /<a [^>]*原生 Chat/);
  assert.match(html, /需要 portal:engines:native/);
});

test('upstream strings are escaped before they reach the markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const portal = ok({ modules: [module_({ title: hostile, data: { engine: hostile } })] });
  const html = renderModulePanel(portal, 'engines');
  assert.ok(!html.includes('<img'), html);
  assert.match(html, /&lt;img/);
});

test('the endpoint table shows the private subset and a read-only selector', () => {
  const portal = ok({
    endpoints: {
      visibility: VISIBILITY.PRIVATE,
      can_publish_shared: false,
      items: [
        { endpoint_id: 'ep-1', name: 'my-chat', project: 'proj-a', protocol: 'OpenAI Chat', model: 'llm-a', owner: 'self', scope: VISIBILITY.PRIVATE },
      ],
    },
  });
  const html = renderEndpoints(portal);
  assert.match(html, /my-chat/);
  assert.match(html, /seg-readonly/);
  assert.match(html, /aria-current="true">Private/);
  assert.match(html, /尚<b>未<\/b>具备/);
});

test('the endpoint table says so, rather than showing nothing, when empty', () => {
  const html = renderEndpoints(ok());
  assert.match(html, /没有 Endpoint/);
});

test('the playground panel refuses to offer a send button', () => {
  const html = renderPlaygroundNote();
  assert.match(html, /合成输出不是「试跑」/);
  assert.doesNotMatch(html, /<button[^>]*>发送/);
});

test('the demo transport strip says DEMO and names the unknown engine', () => {
  const html = renderTransportStrip(ok({ deployment: { engine: null, facts: {} } }));
  assert.match(html, /DEMO/);
  assert.match(html, /UNKNOWN/);
  assert.doesNotMatch(html, /LIVE/);
});

test('a live transport strip says LIVE instead', () => {
  const html = renderTransportStrip({ synthetic: false, transport: { mode: TRANSPORT_MODES.LIVE, gateway_attached: true } });
  assert.match(html, /LIVE/);
  assert.doesNotMatch(html, /DEMO/);
});

test('signed-out and denied are different messages, not one generic failure', () => {
  const signedOut = renderNotice(PORTAL_STATES.SIGNED_OUT, 'http-401');
  const denied = renderNotice(PORTAL_STATES.DENIED, 'http-403');
  assert.match(signedOut, /请先登录/);
  assert.match(denied, /未获得/);
  assert.match(denied, /portal:modules:read/);
  assert.notEqual(signedOut, denied);
});

test('an unknown notice shows no state and offers a retry', () => {
  const html = renderNotice(PORTAL_STATES.UNKNOWN, 'contract:problem');
  assert.match(html, /无法确定/);
  assert.match(html, /contract:problem/);
  assert.match(html, /data-modules-retry/);
});

test('the panel header text is informative before the card is read', () => {
  const portal = ok();
  assert.equal(panelStateText(portal, 'engines'), 'AVAILABLE');
  assert.equal(panelStateText(portal, 'nope'), '未返回');
});

test('a missing module renders an explanation, not a blank card', () => {
  const html = renderModulePanel(ok(), 'playground');
  assert.match(html, /契约不一致/);
});
