/*! tests/web-features/admin-mode.test.js — 共享管理员模式的界面层。
 *
 *  两条主线：
 *
 *  1. **纯函数**（``describeAdminMode`` / ``renderAdminModeControl``）—— 状态翻
 *     译与画法，每一种畸形输入都要钉住，因为"服务端说了什么"是这一层唯一的输入。
 *  2. **调用**（``source.js`` 里的 adminModeStatus / adminModeAction）—— 只发
 *     同源请求、只带 CSRF、不带上任何自报身份的头、预览模式下干脆不发。
 *
 *  这里**没有** jsdom：与 ``ops-controller.test.js`` 一样用手写的 DOM 替身，
 *  只实现这一个控件真正用到的那几个方法。
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  ADMIN_MODE_STATES,
  describeAdminMode,
  reasonText,
  renderAdminModeControl,
  mountAdminModeControl,
} from '../../web/js/features/admin-mode.js';
import {
  ADMIN_MODE_PATHS,
  ADMIN_MODE_CSRF_PATH,
  ADMIN_MODE_TTL_CAP_SECONDS,
  adminModeAction,
  adminModeStatus,
  adminModeCsrf,
} from '../../web/js/features/source.js';
import { attachAdminMode } from '../../web/js/features/controller.js';

const status = (overrides = {}) => ({
  configured: true,
  elevated: false,
  ttl_seconds: 900,
  expires_at: null,
  ...overrides,
});

const jsonResponse = (payload, { status: code = 200 } = {}) => ({
  ok: code >= 200 && code < 300,
  status: code,
  json: async () => payload,
});

// --- 纯函数：状态翻译 --------------------------------------------------------

test('an absent status is unknown, not elevated', () => {
  const view = describeAdminMode(undefined);
  assert.equal(view.state, ADMIN_MODE_STATES.UNKNOWN);
  assert.equal(view.elevated, false);
  assert.equal(view.canEnter, false);
  assert.equal(view.canExit, false);
});

test('a malformed status fails closed', () => {
  const junk = [null, 42, 'yes', [], {}, { configured: 'true' }, { elevated: true }];
  for (const value of junk) {
    const view = describeAdminMode(value);
    assert.equal(view.state, ADMIN_MODE_STATES.UNKNOWN, JSON.stringify(value));
    assert.equal(view.canEnter, false, JSON.stringify(value));
    assert.equal(view.canExit, false, JSON.stringify(value));
  }
});

test('an unconfigured deployment is disabled and says why', () => {
  const view = describeAdminMode(status({ configured: false }));
  assert.equal(view.state, ADMIN_MODE_STATES.DISABLED);
  assert.equal(view.canEnter, false);
  assert.match(view.detail, /未配置/);
});

test('elevated is ignored unless the feature is configured', () => {
  const view = describeAdminMode(status({ configured: false, elevated: true }));
  assert.equal(view.state, ADMIN_MODE_STATES.DISABLED);
  assert.equal(view.elevated, false);
  assert.equal(view.canExit, false);
});

test('a configured member session may enter and may not exit', () => {
  const view = describeAdminMode(status());
  assert.equal(view.state, ADMIN_MODE_STATES.MEMBER);
  assert.equal(view.canEnter, true);
  assert.equal(view.canExit, false);
});

test('a configured elevated session may exit and may not enter', () => {
  const view = describeAdminMode(status({ elevated: true, expires_at: 1234 }));
  assert.equal(view.state, ADMIN_MODE_STATES.ELEVATED);
  assert.equal(view.canExit, true);
  assert.equal(view.canEnter, false);
  assert.equal(view.expiresAt, 1234);
});

test('the advertised lifetime is capped at fifteen minutes', () => {
  assert.equal(describeAdminMode(status({ ttl_seconds: 3600 })).ttlSeconds, 900);
  assert.equal(describeAdminMode(status({ ttl_seconds: 900 })).ttlSeconds, 900);
  // A server claiming a shorter life is not rounded up past its own claim.
  assert.equal(describeAdminMode(status({ ttl_seconds: 600 })).ttlSeconds, 600);
});

test('an absurd or malformed lifetime falls back to the cap, not to infinity', () => {
  for (const ttl of [0, -5, 'soon', null, NaN, undefined]) {
    const view = describeAdminMode(status({ ttl_seconds: ttl }));
    assert.ok(view.ttlSeconds <= ADMIN_MODE_TTL_CAP_SECONDS, String(ttl));
    assert.ok(view.ttlSeconds >= 60, String(ttl));
  }
});

test('a malformed expiry is reported as no expiry rather than a wrong one', () => {
  for (const value of ['later', 0, -1, null, NaN, {}]) {
    assert.equal(describeAdminMode(status({ elevated: true, expires_at: value })).expiresAt, null);
  }
});

test('reason codes have human text and unknown codes pass through', () => {
  assert.match(reasonText('admin_mode_not_configured'), /未配置/);
  assert.match(reasonText('invalid_credentials'), /口令/);
  assert.equal(reasonText('some_new_code'), 'some_new_code');
  assert.equal(reasonText(null), null);
});

// --- 纯函数：画法 ------------------------------------------------------------

test('the unconfigured control renders a disabled button and a reason', () => {
  const html = renderAdminModeControl(describeAdminMode(status({ configured: false })));
  assert.match(html, /disabled/);
  assert.match(html, /data-admin-mode="disabled"/);
  assert.match(html, /data-admin-mode-detail/);
});

test('the password field is a password field and is not autocompleted', () => {
  const html = renderAdminModeControl(describeAdminMode(status()));
  assert.match(html, /type="password"/);
  assert.match(html, /autocomplete="off"/);
});

test('the member control offers entering and not exiting', () => {
  const html = renderAdminModeControl(describeAdminMode(status()));
  assert.match(html, /data-admin-mode-action="enter"/);
  assert.match(html, /data-admin-mode-action="exit"[^>]*disabled/);
});

test('the elevated control offers exiting and not entering', () => {
  const html = renderAdminModeControl(describeAdminMode(status({ elevated: true })));
  assert.match(html, /data-admin-mode-action="enter"[^>]*disabled/);
  assert.match(html, /data-admin-mode-action="exit"(?![^>]*disabled)/);
});

test('server-supplied text is escaped rather than injected', () => {
  const hostile = status({ configured: false });
  const html = renderAdminModeControl({
    ...describeAdminMode(hostile),
    title: '<img src=x onerror="alert(1)">',
    detail: '"><script>alert(2)</script>',
  });
  assert.ok(!html.includes('<img'), 'raw tag survived');
  assert.ok(!html.includes('<script'), 'raw script survived');
  assert.match(html, /&lt;img/);
});

test('the rendered control carries no secret', () => {
  const html = renderAdminModeControl(describeAdminMode(status()));
  assert.ok(!/value="[^"]+"/.test(html), 'the control prefilled a value');
  assert.ok(!html.includes('$scrypt$'));
});

// --- 调用：source.js 的两条同源请求 -------------------------------------------

test('the admin endpoints are the three agreed paths', () => {
  assert.deepEqual(Object.values(ADMIN_MODE_PATHS).sort(), [
    '/auth/admin/enter',
    '/auth/admin/exit',
    '/auth/admin/status',
  ]);
});

test('the preview has no server session, and says so without calling fetch', async () => {
  let called = 0;
  const fetchImpl = async () => { called += 1; return jsonResponse({}); };
  for (const mode of ['mock', 'unavailable']) {
    const read = await adminModeStatus({ mode, base: '', fetchImpl });
    assert.equal(read.ok, false, mode);
    assert.equal(read.reason, 'no-server-session', mode);
    const write = await adminModeAction({ mode, base: '', fetchImpl, action: 'enter', password: 'x', csrfToken: 't' });
    assert.equal(write.ok, false, mode);
    assert.equal(write.reason, 'no-server-session', mode);
  }
  assert.equal(called, 0, 'the preview opened a request');
});

test('a live status read returns the server state', async () => {
  const fetchImpl = async (url, options) => {
    assert.equal(url, ADMIN_MODE_PATHS.status);
    assert.equal(options.method, 'GET');
    assert.equal(options.credentials, 'same-origin');
    return jsonResponse({ data: { admin_mode: status({ elevated: true }) } });
  };
  const result = await adminModeStatus({ mode: 'live', base: '', fetchImpl });
  assert.equal(result.ok, true);
  assert.equal(result.adminMode.elevated, true);
});

test('a malformed status payload is not reported as success', async () => {
  for (const payload of [{}, { data: {} }, { data: { admin_mode: { elevated: true } } }, null]) {
    const fetchImpl = async () => jsonResponse(payload);
    const result = await adminModeStatus({ mode: 'live', base: '', fetchImpl });
    assert.equal(result.ok, false, JSON.stringify(payload));
  }
});

test('an off-origin base is refused before any request', async () => {
  let called = 0;
  const fetchImpl = async () => { called += 1; return jsonResponse({}); };
  const result = await adminModeStatus({ mode: 'live', base: 'https://evil.example', fetchImpl });
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'endpoint-rejected');
  assert.equal(called, 0);
});

test('entering sends the password once, in the body, with only the CSRF header', async () => {
  const seen = [];
  const fetchImpl = async (url, options) => {
    seen.push({ url, options });
    return jsonResponse({ data: { admin_mode: status({ elevated: true }) } });
  };
  const result = await adminModeAction({
    mode: 'live', base: '', fetchImpl, action: 'enter',
    password: 'synthetic-fixture-value', csrfToken: 'csrf-token-value',
  });
  assert.equal(result.ok, true);
  assert.equal(seen.length, 1);
  const { url, options } = seen[0];
  assert.equal(url, ADMIN_MODE_PATHS.enter);
  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(options.body), { password: 'synthetic-fixture-value' });
  // No identity assertion, ever: the elevation is the server's to decide.
  for (const header of Object.keys(options.headers)) {
    assert.ok(
      ['Accept', 'Content-Type', 'X-CSRF-Token'].includes(header),
      `unexpected header ${header}`,
    );
  }
  assert.ok(!seen[0].url.includes('synthetic-fixture-value'), 'the password reached the URL');
});

test('the password never appears in the status URL or the exit body', async () => {
  const seen = [];
  const fetchImpl = async (url, options) => {
    seen.push({ url, options });
    return jsonResponse({ data: { admin_mode: status() } });
  };
  await adminModeAction({ mode: 'live', base: '', fetchImpl, action: 'exit', csrfToken: 't' });
  assert.equal(seen[0].url, ADMIN_MODE_PATHS.exit);
  assert.deepEqual(JSON.parse(seen[0].options.body), {});
});

test('a write without a CSRF token is refused before the wire', async () => {
  let called = 0;
  const fetchImpl = async () => { called += 1; return jsonResponse({}); };
  for (const token of [undefined, null, '', 42]) {
    const result = await adminModeAction({
      mode: 'live', base: '', fetchImpl, action: 'enter', password: 'x', csrfToken: token,
    });
    assert.equal(result.ok, false, String(token));
    assert.equal(result.reason, 'missing-csrf-token', String(token));
  }
  assert.equal(called, 0);
});

test('an unsupported action never reaches the network', async () => {
  let called = 0;
  const fetchImpl = async () => { called += 1; return jsonResponse({}); };
  for (const action of ['elevate', 'ENTER', '', undefined, 'promote']) {
    const result = await adminModeAction({
      mode: 'live', base: '', fetchImpl, action, csrfToken: 't',
    });
    assert.equal(result.ok, false, String(action));
    assert.equal(result.reason, 'unsupported-action', String(action));
  }
  assert.equal(called, 0);
});

test('a refusal from the server is reported by its code, not dressed up as success', async () => {
  for (const [code, reason] of [[401, 'invalid_credentials'], [429, 'rate_limited'], [503, 'admin_mode_not_configured']]) {
    const fetchImpl = async () => jsonResponse({ error: { code: reason, message: 'x' } }, { status: code });
    const result = await adminModeAction({
      mode: 'live', base: '', fetchImpl, action: 'enter', password: 'x', csrfToken: 't',
    });
    assert.equal(result.ok, false, String(code));
    assert.equal(result.reason, reason);
  }
});

// --- 挂载：DOM 行为 ----------------------------------------------------------

function makeDoc() {
  const passwordField = { value: '' };
  const node = (tag) => ({
    tagName: tag,
    className: '',
    children: [],
    listeners: {},
    attributes: {},
    html: '',
    set innerHTML(value) { this.html = value; },
    get innerHTML() { return this.html; },
    setAttribute(name, value) { this.attributes[name] = value; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    querySelector(selector) {
      return String(selector).includes('password') ? passwordField : null;
    },
  });
  return { doc: { createElement: (tag) => node(tag) }, passwordField };
}

test('mounting needs a document and a root', () => {
  assert.throws(() => mountAdminModeControl({}));
  assert.throws(() => mountAdminModeControl({ doc: { createElement: () => ({}) } }));
});

test('mounting in the preview paints the unknown state and opens nothing', async () => {
  const { doc } = makeDoc();
  const root = doc.createElement('div');
  let calls = 0;
  const control = mountAdminModeControl({
    doc,
    root,
    transport: { mode: 'mock', fetchImpl: async () => { calls += 1; } },
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls, 0);
  assert.equal(control.view().state, ADMIN_MODE_STATES.UNKNOWN);
  assert.equal(root.children.length, 1);
  assert.match(root.children[0].html, /data-admin-mode="unknown"/);
});

test('the password field is read once and cleared immediately', async () => {
  const { doc, passwordField } = makeDoc();
  const root = doc.createElement('div');
  const sent = [];
  const fetchImpl = async (url, options) => {
    if (options.method === 'POST') {
      sent.push(JSON.parse(options.body));
      // While the request is in flight the field must already be empty.
      assert.equal(passwordField.value, '', 'the field still held the password in flight');
      return jsonResponse({ data: { admin_mode: status({ elevated: true }) } });
    }
    // The initial read says "member", which is what offers the control at all.
    return jsonResponse({ data: { admin_mode: status() } });
  };
  passwordField.value = 'synthetic-fixture-value';

  const control = mountAdminModeControl({
    doc, root, transport: { mode: 'live', base: '', fetchImpl },
    getCsrfToken: () => 'csrf-token-value',
  });
  // The first paint is "unknown" until the server has been asked; entering is
  // only offered once the state says it is a member session.
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(control.view().canEnter, true);
  await control.enter();

  assert.deepEqual(sent, [{ password: 'synthetic-fixture-value' }]);
  assert.equal(passwordField.value, '');
  assert.equal(control.view().state, ADMIN_MODE_STATES.ELEVATED);
});

test('entering is refused when the state says it is not allowed', async () => {
  const { doc, passwordField } = makeDoc();
  const root = doc.createElement('div');
  let calls = 0;
  const control = mountAdminModeControl({
    doc, root,
    transport: { mode: 'mock', fetchImpl: async () => { calls += 1; } },
    getCsrfToken: () => 't',
  });
  passwordField.value = 'whatever';
  await control.enter();
  assert.equal(calls, 0);
  assert.ok(!root.children[0].html.includes('已进入管理员模式'));
});

test('a failed attempt re-reads the server state instead of assuming', async () => {
  const { doc, passwordField } = makeDoc();
  const root = doc.createElement('div');
  const paths = [];
  const fetchImpl = async (url) => {
    paths.push(url);
    if (url === ADMIN_MODE_PATHS.status) {
      return jsonResponse({ data: { admin_mode: status() } });
    }
    return jsonResponse({ error: { code: 'invalid_credentials' } }, { status: 401 });
  };
  passwordField.value = 'wrong-synthetic';
  const control = mountAdminModeControl({
    doc, root, transport: { mode: 'live', base: '', fetchImpl },
    getCsrfToken: () => 'csrf-token-value',
  });
  await new Promise((resolve) => setTimeout(resolve, 0)); // let the initial read land
  await control.enter();

  assert.equal(control.view().state, ADMIN_MODE_STATES.MEMBER);
  assert.equal(paths.filter((p) => p === ADMIN_MODE_PATHS.enter).length, 1);
  assert.ok(paths.filter((p) => p === ADMIN_MODE_PATHS.status).length >= 2, 'did not re-read');
  assert.equal(passwordField.value, '');
});

test('CSRF token is fetched from same-origin auth only for a bound Linux session', async () => {
  const seen = [];
  const fetchImpl = async (url, opts) => {
    seen.push({ url, method: opts.method, credentials: opts.credentials });
    return jsonResponse({
      ok: true,
      data: { authenticated: true, csrf_token: 'one-time-synthetic-token' },
    });
  };
  const result = await adminModeCsrf({ mode: 'live', base: '', fetchImpl });
  assert.equal(result.ok, true);
  assert.equal(result.csrfToken, 'one-time-synthetic-token');
  assert.deepEqual(seen, [{
    url: ADMIN_MODE_CSRF_PATH, method: 'GET', credentials: 'same-origin',
  }]);

  for (const mode of ['mock', 'unavailable']) {
    const rejected = await adminModeCsrf({ mode, base: '', fetchImpl });
    assert.equal(rejected.ok, false);
    assert.equal(rejected.reason, 'no-server-session');
  }
  const offOrigin = await adminModeCsrf({ mode: 'live', base: 'https://evil.example', fetchImpl });
  assert.equal(offOrigin.reason, 'endpoint-rejected');
  assert.equal(seen.length, 1, 'refused CSRF requests must not fetch');
});

test('anonymous, malformed, or failed CSRF responses never grant a write token', async () => {
  const payloads = [
    { ok: true, data: { authenticated: false, csrf_token: 'anonymous-token' } },
    { ok: true, data: { authenticated: true, csrf_token: '' } },
    { ok: false, data: { authenticated: true, csrf_token: 'invalid' } },
    { data: { authenticated: true, csrf_token: 'missing-ok' } },
    {},
  ];
  for (const payload of payloads) {
    const result = await adminModeCsrf({
      mode: 'live', base: '', fetchImpl: async () => jsonResponse(payload),
    });
    assert.equal(result.ok, false, JSON.stringify(payload));
    assert.equal(result.csrfToken, undefined);
  }
  const invalidJson = await adminModeCsrf({
    mode: 'live', base: '', fetchImpl: async () => ({
      ok: true, status: 200, json: async () => { throw Error('bad JSON'); },
    }),
  });
  assert.equal(invalidJson.ok, false);
});

test('V6 admin view wires status, one-use CSRF and elevation-driven ops reloads', async () => {
  const { doc, passwordField } = makeDoc();
  const root = doc.createElement('div');
  doc.querySelector = (selector) => selector === '#view-admin' ? root : null;

  const requests = [];
  let elevated = false;
  const fetchImpl = async (url, options) => {
    requests.push({ url, options });
    if (url === ADMIN_MODE_CSRF_PATH) {
      return jsonResponse({
        ok: true, data: { authenticated: true, csrf_token: 'synthetic-single-use' },
      });
    }
    if (url === ADMIN_MODE_PATHS.enter || url === ADMIN_MODE_PATHS.exit) {
      elevated = url === ADMIN_MODE_PATHS.enter;
    }
    return jsonResponse({ data: { admin_mode: status({ elevated }) } });
  };
  const lifecycle = [];
  const controller = {
    mode: 'live',
    invalidate: () => lifecycle.push('invalidate'),
    load: async () => { lifecycle.push('load'); },
  };
  const control = attachAdminMode({
    doc, controller, transport: { mode: 'live', base: '', fetchImpl },
  });
  assert.ok(control);
  assert.equal(root.children.length, 1, 'admin control was not mounted');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(control.view().state, ADMIN_MODE_STATES.MEMBER);

  passwordField.value = 'synthetic-test-password';
  await control.enter();
  assert.equal(passwordField.value, '');
  assert.equal(control.view().state, ADMIN_MODE_STATES.ELEVATED);
  assert.deepEqual(lifecycle, ['invalidate', 'load']);

  await control.exit();
  assert.equal(control.view().state, ADMIN_MODE_STATES.MEMBER);
  assert.deepEqual(lifecycle, ['invalidate', 'load', 'invalidate', 'load']);
  assert.deepEqual(
    requests.filter(({ url }) => [ADMIN_MODE_CSRF_PATH, ADMIN_MODE_PATHS.enter, ADMIN_MODE_PATHS.exit].includes(url))
      .map(({ url }) => url),
    [ADMIN_MODE_CSRF_PATH, ADMIN_MODE_PATHS.enter, ADMIN_MODE_CSRF_PATH, ADMIN_MODE_PATHS.exit],
    'each mutation must fetch a fresh CSRF token before POST',
  );
});

test('the source never reaches for browser storage', () => {
  // A password in localStorage outlives the tab and is readable by any script on
  // the origin. A textual guard, because the failure mode is someone adding the
  // line rather than someone calling a function.
  // Matched as *usage* rather than as a word, because both modules mention the
  // APIs in a comment explaining that they do not use them.
  const usage = /\b(?:local|session)Storage\s*[.[=]|\bwindow\.(?:local|session)Storage\b/;
  for (const relative of ['../web/js/features/admin-mode.js', '../web/js/features/source.js']) {
    const path = fileURLToPath(new URL(relative, new URL('..', import.meta.url)));
    const source = readFileSync(path, 'utf8');
    assert.ok(!usage.test(source), `${relative} uses browser storage`);
  }
});
