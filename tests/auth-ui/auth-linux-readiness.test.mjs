/**
 * tests/auth-ui/auth-linux-readiness.test.mjs
 *
 * Lane B 核心契约的纯逻辑测试：**Linux PAM 登录表单的就绪门槛**。
 *
 * 运行：`node --test tests/auth-ui/auth-linux-readiness.test.mjs`
 *
 * 这一层要钉死的一件事：除了同源只读的 `GET /auth/csrf`，没有任何来源能让 Linux
 * 表单变为可用；而 `/auth/csrf` 只有在**明确**声明 `providers.linux_pam.enabled === true`
 * 且满足全部契约时才开闸。任何其它状态——旧飞书 provider 已启用、HTML 假 200、
 * 401/403、超时、schema 不认识、CSRF 缺失——一律 fail-closed。
 *
 * 契约正本见 `docs/auth-linux-ui-contract.md`。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import authCore from '../../web/auth/auth-core.js';

const {
  LINUX_PAM_REASON,
  LINUX_PAM_UID_SCOPE,
  readLinuxPamProvider,
  linuxLoginReadiness,
} = authCore;

/** 契约要求的 Linux provider 声明（可逐字段改写）。 */
function linuxPam(overrides = {}) {
  return Object.assign({
    enabled: true,
    uid_scope: LINUX_PAM_UID_SCOPE,
    root: false,
    service_account: false,
  }, overrides);
}

/** 构造一份**完全满足**契约的 `/auth/csrf` 响应。 */
function readyPayload(overrides = {}) {
  const data = Object.assign({
    csrf_token: 'csrf-value-123',
    authenticated: false,
    providers: {
      feishu: false,
      local_admin: false,
      linux_pam: overrides.linux_pam !== undefined ? overrides.linux_pam : linuxPam(),
    },
  }, overrides.data);
  if (overrides.providers !== undefined) data.providers = overrides.providers;
  return { ok: true, data };
}

/** 一次「一切正常」的输入。 */
function readyInput(overrides = {}) {
  return Object.assign({
    staticPreview: false,
    timedOut: false,
    status: 200,
    contentType: 'application/json; charset=utf-8',
    payload: overrides.payload !== undefined ? overrides.payload : readyPayload(overrides),
  }, overrides);
}

/* ------------------------------------------------------------------ 正例 */

test('a fully documented csrf response opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput());
  assert.equal(verdict.ready, true);
  assert.equal(verdict.reason, LINUX_PAM_REASON.READY);
});

/* ------------------------------------------------- 旧 provider 不得开闸 */

test('an enabled legacy feishu provider does not open the linux gate', () => {
  const verdict = linuxLoginReadiness(readyInput({
    providers: { feishu: true, local_admin: false },
  }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.PROVIDER_ABSENT);
});

test('an enabled legacy local_admin provider does not open the linux gate', () => {
  const verdict = linuxLoginReadiness(readyInput({
    providers: { feishu: false, local_admin: true },
  }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.PROVIDER_ABSENT);
});

/* ------------------------------------------------ 传输层：fail-closed */

test('a static file:// preview never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ staticPreview: true }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.STATIC_PREVIEW);
});

test('a timed-out csrf read never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ timedOut: true }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.TIMEOUT);
});

test('an HTML body served with 200 never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    payload: '<!doctype html><html><body>登录</body></html>',
  }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.HTML_RESPONSE);
});

test('a 401 never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({
    status: 401,
    payload: { ok: false, error: { code: 'unauthenticated' } },
  }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.HTTP_401);
});

test('a 403 never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({
    status: 403,
    payload: { ok: false, error: { code: 'forbidden' } },
  }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.HTTP_403);
});

test('a 500 never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ status: 500, payload: null }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.HTTP_ERROR);
});

test('a body that is not the documented envelope never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({
    payload: { providers: { linux_pam: linuxPam() } },
  }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.MALFORMED_ENVELOPE);
});

test('an envelope that omits the authenticated contract never opens the gate', () => {
  const payload = readyPayload();
  delete payload.data.authenticated;
  const verdict = linuxLoginReadiness(readyInput({ payload }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.MALFORMED_ENVELOPE);
});

test('a csrf response without a token never opens the gate', () => {
  const payload = readyPayload();
  delete payload.data.csrf_token;
  const verdict = linuxLoginReadiness(readyInput({ payload }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.CSRF_MISSING);
});

/* ------------------------------------------- provider schema：逐条收紧 */

test('an explicit enabled:false never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ linux_pam: linuxPam({ enabled: false }) }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.PROVIDER_DISABLED);
});

test('a truthy-but-not-true enabled never opens the gate', () => {
  for (const bogus of ['true', 1, {}, []]) {
    const verdict = linuxLoginReadiness(readyInput({ linux_pam: linuxPam({ enabled: bogus }) }));
    assert.equal(verdict.ready, false, `enabled=${JSON.stringify(bogus)} must not open the gate`);
    assert.equal(verdict.reason, LINUX_PAM_REASON.PROVIDER_DISABLED);
  }
});

test('a root-enabled linux provider never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ linux_pam: linuxPam({ root: true }) }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.UNSAFE_PRINCIPAL_SCOPE);
});

test('a service-account linux provider never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ linux_pam: linuxPam({ service_account: true }) }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.UNSAFE_PRINCIPAL_SCOPE);
});

test('a missing principal-scope declaration never opens the gate', () => {
  for (const drop of ['root', 'service_account', 'uid_scope']) {
    const declared = linuxPam();
    delete declared[drop];
    const verdict = linuxLoginReadiness(readyInput({ linux_pam: declared }));
    assert.equal(verdict.ready, false, `missing ${drop} must not open the gate`);
    assert.equal(verdict.reason, LINUX_PAM_REASON.UNSAFE_PRINCIPAL_SCOPE);
  }
});

test('a uid_scope that is not the Dell account scope never opens the gate', () => {
  const verdict = linuxLoginReadiness(readyInput({ linux_pam: linuxPam({ uid_scope: 'root' }) }));
  assert.equal(verdict.ready, false);
  assert.equal(verdict.reason, LINUX_PAM_REASON.UNSAFE_PRINCIPAL_SCOPE);
});

/* ------------------------------------------------ provider 只读解析 */

test('readLinuxPamProvider reports absence rather than a guess', () => {
  const absent = readLinuxPamProvider({ ok: true, data: { providers: { feishu: true } } });
  assert.equal(absent.present, false);
  assert.equal(absent.enabled, null);
  assert.equal(absent.known, false);

  const disabled = readLinuxPamProvider({ ok: true, data: { providers: { linux_pam: { enabled: false } } } });
  assert.equal(disabled.present, true);
  assert.equal(disabled.enabled, false);
  assert.equal(disabled.known, true);

  const enabled = readLinuxPamProvider(readyPayload());
  assert.equal(enabled.present, true);
  assert.equal(enabled.enabled, true);
  assert.equal(enabled.root, false);
  assert.equal(enabled.serviceAccount, false);
  assert.equal(enabled.uidScope, LINUX_PAM_UID_SCOPE);
});

test('readLinuxPamProvider never turns a non-boolean enabled into true', () => {
  const parsed = readLinuxPamProvider({ ok: true, data: { providers: { linux_pam: { enabled: 'yes' } } } });
  assert.equal(parsed.enabled, null);
});

test('readLinuxPamProvider refuses a non-object provider declaration', () => {
  for (const bogus of [true, 'enabled', 3, null, []]) {
    const parsed = readLinuxPamProvider({ ok: true, data: { providers: { linux_pam: bogus } } });
    assert.equal(parsed.present, false, `linux_pam=${JSON.stringify(bogus)} must read as absent`);
  }
});

/* ------------------------------------------------------ 文案安全 */

test('every non-ready verdict carries a message and never echoes the payload', () => {
  const secrets = 'hunter2-super-secret';
  const payload = readyPayload({ linux_pam: linuxPam({ enabled: false }) });
  payload.data.note = secrets;
  const verdict = linuxLoginReadiness(readyInput({ payload }));
  assert.equal(verdict.ready, false);
  assert.equal(typeof verdict.message, 'string');
  assert.ok(verdict.message.length > 0);
  assert.equal(verdict.message.includes(secrets), false);
});

test('the reason vocabulary is frozen and distinctive', () => {
  const values = Object.values(LINUX_PAM_REASON);
  assert.equal(new Set(values).size, values.length);
  assert.equal(Object.isFrozen(LINUX_PAM_REASON), true);
});
