/**
 * tests/auth-ui/auth-core.test.mjs
 *
 * 纯逻辑层单元测试：不启动浏览器、不联网。
 * 运行：`node --test tests/auth-ui/`
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// auth-core.js 是 UMD：Node 侧通过 CJS 互操作拿到默认导出，再解构。
import authCore from '../../web/auth/auth-core.js';

const {
  VIEW,
  DEFAULT_LANDING,
  isStaticPreview,
  sanitizeNextPath,
  nextFromSearch,
  isRetiredEntryLink,
  isLoggedOutLanding,
  validateLoginForm,
  normalizeErrorCode,
  parseRetryAfter,
  classifyLoginResult,
  formatDuration,
  isAuthenticated,
  authorizedProjects,
  classifySession,
  readProviderState,
  readCsrfValue,
  serverMessage,
  redactCredential,
} = authCore;

/* ==========================================================================
   运行环境判定
   ========================================================================== */

test('isStaticPreview：file:// 与 data: 视为静态预览', () => {
  assert.equal(isStaticPreview({ protocol: 'file:' }), true);
  assert.equal(isStaticPreview({ protocol: 'data:' }), true);
  assert.equal(isStaticPreview({ protocol: 'about:' }), true);
  assert.equal(isStaticPreview({}), true);
  assert.equal(isStaticPreview(null), true);
});

test('isStaticPreview：http/https 允许真实交互', () => {
  assert.equal(isStaticPreview({ protocol: 'http:' }), false);
  assert.equal(isStaticPreview({ protocol: 'https:' }), false);
});

/* ==========================================================================
   开放重定向防护
   ========================================================================== */

test('sanitizeNextPath：接受站内绝对路径', () => {
  assert.equal(sanitizeNextPath('/'), '/');
  assert.equal(sanitizeNextPath('/atlas'), '/atlas');
  assert.equal(sanitizeNextPath('/atlas?range=7d&model=m1'), '/atlas?range=7d&model=m1');
  assert.equal(sanitizeNextPath('  /console  '), '/console');
});

test('sanitizeNextPath：拒绝绝对 URL 与协议相对 URL', () => {
  const evil = [
    'https://evil.example/steal',
    'http://evil.example',
    '//evil.example',
    '///evil.example',
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'ftp://evil.example',
    'mailto:a@b.c',
  ];
  for (const value of evil) {
    assert.equal(sanitizeNextPath(value), DEFAULT_LANDING, `应拒绝：${value}`);
  }
});

test('sanitizeNextPath：拒绝反斜杠与换行等解析器绕过手法', () => {
  const bypasses = [
    '/\\evil.example',
    '\\\\evil.example',
    '/\\/evil.example',
    '/atlas\nhttps://evil.example',
    '/atlas\r\nLocation: https://evil.example',
    '/atlas\t',
    '/at las',
    '/atlas\u0000',
  ];
  for (const value of bypasses) {
    assert.equal(sanitizeNextPath(value), DEFAULT_LANDING, `应拒绝：${JSON.stringify(value)}`);
  }
});

test('sanitizeNextPath：拒绝相对路径与空值', () => {
  assert.equal(sanitizeNextPath('atlas'), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath('..%2F..%2Fetc'), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath(''), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath('   '), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath(null), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath(undefined), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath(42), DEFAULT_LANDING);
  assert.equal(sanitizeNextPath({}), DEFAULT_LANDING);
});

test('nextFromSearch：支持 next / redirect / return_to 并统一收敛', () => {
  assert.equal(nextFromSearch('?next=/atlas'), '/atlas');
  assert.equal(nextFromSearch('?redirect=/usage'), '/usage');
  assert.equal(nextFromSearch('?return_to=/quota'), '/quota');
  assert.equal(nextFromSearch('?next=https://evil.example'), '/');
  assert.equal(nextFromSearch('?next=//evil.example'), '/');
  assert.equal(nextFromSearch(''), '/');
  assert.equal(nextFromSearch(undefined), '/');
  assert.equal(nextFromSearch('next=/atlas'), '/atlas');
});

/* ==========================================================================
   模式与落地
   ========================================================================== */

test('isRetiredEntryLink：认出已退役的成员 / 管理员双入口书签', () => {
  // 被认出来的意义是「告知入口已退役」——不是「据此切换入口」（已经没有第二个入口了）。
  assert.equal(isRetiredEntryLink('?mode=admin', ''), true);
  assert.equal(isRetiredEntryLink('?mode=member', ''), true);
  assert.equal(isRetiredEntryLink('', '#admin'), true);
  assert.equal(isRetiredEntryLink('', '#member'), true);
  assert.equal(isRetiredEntryLink('?admin=1', ''), true);
  assert.equal(isRetiredEntryLink('?mode=ADMIN', ''), false, '大小写不匹配时不猜测');
  assert.equal(isRetiredEntryLink('', ''), false);
  assert.equal(isRetiredEntryLink('?next=/', ''), false);
  assert.equal(isRetiredEntryLink(undefined, undefined), false);
});

test('isLoggedOutLanding：识别退出落地参数', () => {
  assert.equal(isLoggedOutLanding('?logged_out=1'), true);
  assert.equal(isLoggedOutLanding('?next=/'), false);
  assert.equal(isLoggedOutLanding(''), false);
});

/* ==========================================================================
   表单校验：不得内置任何默认凭据
   ========================================================================== */

test('validateLoginForm：空用户名与空口令分别给出字段级提示', () => {
  const empty = validateLoginForm({ username: '', password: '' });
  assert.equal(empty.ok, false);
  assert.equal(empty.field, 'username');

  const noUser = validateLoginForm({ username: '   ', password: 'x' });
  assert.equal(noUser.ok, false);
  assert.equal(noUser.field, 'username');

  const noPass = validateLoginForm({ username: 'admin', password: '' });
  assert.equal(noPass.ok, false);
  assert.equal(noPass.field, 'password');

  const ok = validateLoginForm({ username: 'admin', password: 'whatever-the-user-typed' });
  assert.equal(ok.ok, true);
  assert.equal(ok.field, null);
});

test('validateLoginForm：不把任何输入视为「正确」，只判非空', () => {
  // 任意非空组合都通过本地校验，真正的判定权在服务端
  for (const candidate of ['admin', 'root', 'someone']) {
    assert.equal(validateLoginForm({ username: candidate, password: 'p' }).ok, true);
  }
  assert.equal(validateLoginForm(null).ok, false);
  assert.equal(validateLoginForm({}).ok, false);
});

/* ==========================================================================
   错误码归一化与 Retry-After
   ========================================================================== */

test('normalizeErrorCode：兼容 error / code / 嵌套结构', () => {
  assert.equal(normalizeErrorCode({ error: 'not_configured' }), 'not_configured');
  assert.equal(normalizeErrorCode({ code: 'CSRF_INVALID' }), 'csrf_invalid');
  assert.equal(normalizeErrorCode({ error: { code: 'Account-Locked' } }), 'account_locked');
  assert.equal(normalizeErrorCode({ error_code: 'rate_limited' }), 'rate_limited');
  assert.equal(normalizeErrorCode(null), '');
  assert.equal(normalizeErrorCode('nope'), '');
});

test('parseRetryAfter：支持秒数与 HTTP 日期', () => {
  assert.equal(parseRetryAfter('60'), 60);
  assert.equal(parseRetryAfter('0'), 0);
  assert.equal(parseRetryAfter(null), null);
  assert.equal(parseRetryAfter(''), null);
  assert.equal(parseRetryAfter('not-a-number'), null);

  const now = Date.parse('2026-10-14T00:00:00Z');
  assert.equal(parseRetryAfter('Wed, 14 Oct 2026 00:01:00 GMT', now), 60);
  assert.equal(parseRetryAfter('Wed, 14 Oct 2026 00:00:30 GMT', now), 30);
  // 已过去的日期收敛为 0，不产生负数
  assert.equal(parseRetryAfter('Wed, 14 Oct 2026 00:00:00 GMT', now + 5000), 0);
});

test('formatDuration：中文可读时长', () => {
  assert.equal(formatDuration(1), '1 秒');
  assert.equal(formatDuration(59), '59 秒');
  assert.equal(formatDuration(60), '1 分钟');
  assert.equal(formatDuration(120), '2 分钟');
  assert.equal(formatDuration(3600), '1 小时');
});

/* ==========================================================================
   登录结果分类
   ========================================================================== */

test('classifyLoginResult：成功', () => {
  for (const status of [200, 201, 204]) {
    const r = classifyLoginResult(status, {});
    assert.equal(r.outcome, 'success');
    assert.equal(r.showError, false);
  }
});

test('classifyLoginResult：网络失败（status 0）进入离线', () => {
  const r = classifyLoginResult(0, null);
  assert.equal(r.outcome, 'offline');
  assert.equal(r.showError, true);
  assert.match(r.message, /无法连接认证服务/);
});

test('classifyLoginResult：401 归为口令错误', () => {
  const r = classifyLoginResult(401, { error: 'invalid_credentials' });
  assert.equal(r.outcome, 'bad-credentials');
  assert.equal(r.showError, true);
});

test('classifyLoginResult：403 按错误码细分 csrf / disabled / locked', () => {
  assert.equal(
    classifyLoginResult(403, { error: 'csrf_invalid' }).outcome, 'csrf-failed');
  assert.equal(
    classifyLoginResult(403, { error: 'account_disabled' }).outcome, 'disabled');
  assert.equal(
    classifyLoginResult(403, { error: 'account_locked' }).outcome, 'locked');
  assert.equal(
    classifyLoginResult(403, { error: 'something_else' }).outcome, 'forbidden');
});

test('classifyLoginResult：423 视为锁定', () => {
  assert.equal(classifyLoginResult(423, {}).outcome, 'locked');
});

test('classifyLoginResult：429 限速并带冷却时长', () => {
  const r = classifyLoginResult(429, { error: 'rate_limited' },
    { retryAfter: '90', nowMs: Date.now() });
  assert.equal(r.outcome, 'rate-limited');
  assert.equal(r.retryAfterSec, 90);
  // 冷却时长按「向上取整」表述：宁可让使用者晚一点重试，也不提前放行
  assert.match(r.message, /2 分钟/);
  assert.match(r.message, /后可重试/);
});

test('classifyLoginResult：503 not_configured 与 5xx 区分', () => {
  assert.equal(
    classifyLoginResult(503, { error: 'not_configured' }).outcome, 'not-configured');
  assert.equal(
    classifyLoginResult(500, { error: 'internal_error' }).outcome, 'server-error');
  assert.equal(
    classifyLoginResult(502, {}).outcome, 'server-error');
});

test('classifyLoginResult：优先使用服务端给出的中文文案', () => {
  const r = classifyLoginResult(401, { error: 'invalid_credentials', message: '服务端原文' });
  assert.equal(r.message, '服务端原文');
});

test('classifyLoginResult：未知状态码不冒充成功', () => {
  const r = classifyLoginResult(418, {});
  assert.equal(r.outcome, 'unknown');
  assert.equal(r.showError, true);
  assert.match(r.message, /418/);
});

/* ==========================================================================
   会话分类
   ========================================================================== */

test('isAuthenticated：只认明确信号', () => {
  assert.equal(isAuthenticated({ authenticated: true }), true);
  assert.equal(isAuthenticated({ authenticated: false, username: 'x' }), false);
  assert.equal(isAuthenticated({ username: 'x' }), true);
  assert.equal(isAuthenticated({ username: '  ' }), false);
  assert.equal(isAuthenticated({}), false);
  assert.equal(isAuthenticated(null), false);
  assert.equal(isAuthenticated('yes'), false);
});

test('authorizedProjects：只认显式项目字段，不认身份字段', () => {
  assert.deepEqual(authorizedProjects({ projects: ['a', 'b'] }), ['a', 'b']);
  assert.deepEqual(authorizedProjects({ projects: [{ key: 'k1' }, { id: 'k2' }, { name: 'k3' }] }), ['k1', 'k2', 'k3']);
  assert.deepEqual(authorizedProjects({ authorized_projects: ['x'] }), ['x']);
  assert.deepEqual(authorizedProjects({ projects: [] }), []);
  assert.deepEqual(authorizedProjects({ projects: 'not-an-array' }), []);
  assert.deepEqual(authorizedProjects({}), []);
});

test('authorizedProjects：识别后端 projects_for() 的 {project_id, role} 形状', () => {
  assert.deepEqual(
    authorizedProjects({ projects: [{ project_id: 'atlas', role: 'viewer' }] }),
    ['atlas'],
  );
  assert.deepEqual(
    authorizedProjects({
      projects: [
        { project_id: 'atlas', role: 'viewer' },
        { project_id: 'usage', role: 'member' },
      ],
    }),
    ['atlas', 'usage'],
  );
  // 只有 role 没有 id 的残缺项不产生授权
  assert.deepEqual(authorizedProjects({ projects: [{ role: 'viewer' }] }), []);
});

test('authorizedProjects：读嵌套的 data.projects', () => {
  assert.deepEqual(
    authorizedProjects({ ok: true, data: { projects: [{ project_id: 'atlas', role: 'viewer' }] } }),
    ['atlas'],
  );
});

test('authorizedProjects：飞书邮箱/昵称/角色不产生任何项目授权', () => {
  const feishuShaped = {
    authenticated: true,
    username: 'synthetic-member-a',
    email: 'synthetic-member-a@example.invalid',
    nickname: '合成成员甲',
    role: 'admin',
    is_admin: true,
    projects: [],
  };
  assert.deepEqual(authorizedProjects(feishuShaped), []);
  assert.equal(classifySession(200, feishuShaped).view, VIEW.NO_PROJECT);
});

test('classifySession：未登录 → 成员登录入口', () => {
  assert.equal(classifySession(401, { error: 'unauthenticated' }).view, VIEW.LINUX);
  assert.equal(classifySession(200, { authenticated: false }).view, VIEW.LINUX);
});

test('classifySession：会话过期单独给出提示', () => {
  const r = classifySession(401, { error: 'session_expired' });
  assert.equal(r.view, VIEW.LINUX);
  assert.match(r.message, /已过期/);
  assert.equal(classifySession(401, { error: 'unauthenticated' }).message, '');
});

test('classifySession：已登录但无项目 → 待分配视图', () => {
  const r = classifySession(200, { authenticated: true, username: 'synthetic-member-a', projects: [] });
  assert.equal(r.view, VIEW.NO_PROJECT);
  assert.equal(r.username, 'synthetic-member-a');
  assert.deepEqual(r.projects, []);
});

test('classifySession：已登录且有项目 → 可进入 Portal', () => {
  const r = classifySession(200, { authenticated: true, username: 'u', projects: ['p'] });
  assert.equal(r.view, VIEW.LINUX);
  assert.deepEqual(r.projects, ['p']);
});

test('classifySession：网络失败与 5xx 都进入离线视图，绝不冒充已登录', () => {
  assert.equal(classifySession(0, null).view, VIEW.OFFLINE);
  assert.equal(classifySession(500, {}).view, VIEW.OFFLINE);
  assert.equal(classifySession(503, {}).view, VIEW.OFFLINE);
});

/* ==========================================================================
   登录方式可用性：只读自 GET /auth/csrf 的 data.providers
   ==========================================================================
   这里**没有**任何「探测 /auth/feishu/start」的判定：该路由会真的创建 OAuth
   state 并绑定 Cookie，是有副作用的写入口，不能当 health 探针用。
   可用性只来自 /auth/csrf 的只读 `data.providers`。 */

test('readProviderState：读取 data.providers（真实后端信封）', () => {
  const state = readProviderState({
    ok: true,
    data: {
      csrf_token: 'tok',
      authenticated: false,
      providers: { local_admin: true, feishu: false },
    },
  });
  assert.deepEqual(state, { feishu: false, localAdmin: true, known: true });
});

test('readProviderState：兼容顶层 providers 的旧形态', () => {
  const state = readProviderState({ providers: { local_admin: false, feishu: true } });
  assert.deepEqual(state, { feishu: true, localAdmin: false, known: true });
});

test('readProviderState：缺字段或非布尔值时判为未知，绝不猜成可用', () => {
  assert.deepEqual(readProviderState(null), { feishu: null, localAdmin: null, known: false });
  assert.deepEqual(readProviderState({}), { feishu: null, localAdmin: null, known: false });
  assert.deepEqual(
    readProviderState({ ok: true, data: {} }),
    { feishu: null, localAdmin: null, known: false },
  );
  assert.deepEqual(
    readProviderState({ ok: true, data: { providers: { feishu: 'yes' } } }),
    { feishu: null, localAdmin: null, known: false },
    '字符串 truthy 不算「已配置」，必须是真正的布尔 true',
  );
});

/* ==========================================================================
   CSRF 与脱敏
   ========================================================================== */

test('readCsrfValue：兼容多种字段名，拒绝空值与非字符串', () => {
  assert.equal(readCsrfValue({ csrf_token: 'abc123' }), 'abc123');
  assert.equal(readCsrfValue({ csrf: '  def456  ' }), 'def456');
  assert.equal(readCsrfValue({ token: 'ghi789' }), 'ghi789');
  assert.equal(readCsrfValue({ csrf_token: '' }), '');
  assert.equal(readCsrfValue({ csrf_token: 12345 }), '');
  assert.equal(readCsrfValue({}), '');
  assert.equal(readCsrfValue(null), '');
  assert.equal(readCsrfValue({ csrf_token: 'x'.repeat(600) }), '', '超长值不采纳');
});

test('readCsrfValue：真实后端信封 data.csrf_token', () => {
  assert.equal(
    readCsrfValue({ ok: true, data: { csrf_token: 'real-token-value' } }),
    'real-token-value',
  );
  assert.equal(readCsrfValue({ ok: true, data: { csrf: '  nested  ' } }), 'nested');
  // 形态不全时如实回空，而不是退化成顶层取值
  assert.equal(readCsrfValue({ ok: true, data: null }), '');
  assert.equal(readCsrfValue({ ok: true, data: { csrf_token: 7 } }), '');
});

test('readCsrfValue：token 只从响应体读，不读 Cookie、不读 storage', () => {
  // 该函数只有 payload 一个入参，结构上就无法碰到 document / localStorage。
  assert.equal(readCsrfValue.length, 1);
  assert.equal(readCsrfValue({ ok: true, data: { csrf_token: 'from-body' } }), 'from-body');
});

/* ==========================================================================
   真实后端错误信封 {ok:false, error:{code, message}}
   ========================================================================== */

test('serverMessage：从真实错误信封的 error.message 取文案', () => {
  assert.equal(
    serverMessage({ ok: false, error: { code: 'csrf_rejected', message: '请求被拒绝。' } }),
    '请求被拒绝。',
  );
  // 扁平形态仍然兼容
  assert.equal(serverMessage({ message: ' 扁平文案 ' }), '扁平文案');
  assert.equal(serverMessage({ error: { code: 'x', message: '   ' } }), '');
  assert.equal(serverMessage(null), '');
  assert.equal(serverMessage({ error: 'just-a-code' }), '');
});

test('classifyLoginResult：真实信封的 403 csrf_rejected 归为 csrf-failed', () => {
  const r = classifyLoginResult(403, {
    ok: false,
    error: { code: 'csrf_rejected', message: 'Request rejected.' },
  });
  assert.equal(r.outcome, 'csrf-failed');
  assert.equal(r.message, 'Request rejected.');
});

test('classifyLoginResult：真实信封的 403 account_disabled 归为 disabled', () => {
  const r = classifyLoginResult(403, {
    ok: false,
    error: { code: 'account_disabled', message: '该账号已被停用。' },
  });
  assert.equal(r.outcome, 'disabled');
});

test('classifyLoginResult：真实信封的 429 rate_limited 带 Retry-After', () => {
  const r = classifyLoginResult(
    429,
    { ok: false, error: { code: 'rate_limited', message: '尝试过于频繁。' } },
    { retryAfter: '120', nowMs: Date.now() },
  );
  assert.equal(r.outcome, 'rate-limited');
  assert.equal(r.retryAfterSec, 120);
  assert.match(r.message, /2 分钟/);
});

test('classifyLoginResult：真实信封的 401/503 分别归为 bad-credentials 与 not-configured', () => {
  assert.equal(
    classifyLoginResult(401, { ok: false, error: { code: 'invalid_credentials' } }).outcome,
    'bad-credentials',
  );
  assert.equal(
    classifyLoginResult(503, { ok: false, error: { code: 'not_configured' } }).outcome,
    'not-configured',
  );
});

/* ==========================================================================
   真实 /auth/me 信封 → 会话判定
   ========================================================================== */

/** 一份贴合后端 public_identity()/projects_for() 的合成响应。 */
function meEnvelope({
  role = 'member',
  isPlatformAdmin = false,
  displayName = 'synthetic-member-a',
  projects = [],
} = {}) {
  return {
    ok: true,
    data: {
      authenticated: true,
      user: {
        id: 'synthetic-id',
        display_name: displayName,
        role,
        auth_source: isPlatformAdmin ? 'local' : 'feishu',
        is_platform_admin: isPlatformAdmin,
      },
      projects,
      session_expires_at: 4102444800,
    },
  };
}

test('classifySession：真实信封的普通成员有项目 ⇒ 允许进入受保护首页', () => {
  const r = classifySession(200, meEnvelope({ projects: [{ project_id: 'p1', role: 'viewer' }] }));
  assert.equal(r.view, VIEW.LINUX);
  assert.equal(r.authorized, true);
  assert.deepEqual(r.projects, ['p1']);
  assert.equal(r.username, 'synthetic-member-a');
  assert.equal(r.isPlatformAdmin, false);
});

test('classifySession：真实信封的普通成员 0 项目 ⇒ 待分配，不得放行', () => {
  const r = classifySession(200, meEnvelope({ projects: [] }));
  assert.equal(r.view, VIEW.NO_PROJECT);
  assert.equal(r.authorized, false);
  assert.deepEqual(r.projects, []);
});

test('classifySession：本地 admin 无项目授权 ⇒ 仍可进入受保护首页', () => {
  const r = classifySession(200, meEnvelope({
    role: 'platform_admin',
    isPlatformAdmin: true,
    displayName: 'admin',
    projects: [],
  }));
  assert.equal(r.authorized, true, '平台管理员不因没有项目 grant 被当成无权限');
  assert.equal(r.isPlatformAdmin, true);
  assert.equal(r.view, VIEW.LINUX);
  assert.equal(r.username, 'admin');
});

test('classifySession：真实信封里 role 字段不足以冒充管理员，必须看 is_platform_admin', () => {
  const r = classifySession(200, meEnvelope({ role: 'platform_admin', isPlatformAdmin: false }));
  assert.equal(r.authorized, false, 'role 字符串本身不构成服务端授权');
  assert.equal(r.view, VIEW.NO_PROJECT);
});

test('classifySession：真实错误信封 401 not_authenticated / session_expired', () => {
  assert.equal(
    classifySession(401, { ok: false, error: { code: 'not_authenticated' } }).view,
    VIEW.LINUX,
  );
  const expired = classifySession(401, {
    ok: false,
    error: { code: 'session_expired', message: '会话已过期。' },
  });
  assert.equal(expired.view, VIEW.LINUX);
  assert.match(expired.message, /过期/);
});

test('classifySession：account_disabled 如实提示且不放行', () => {
  const r = classifySession(403, {
    ok: false,
    error: { code: 'account_disabled', message: '该账号已被停用。' },
  });
  assert.equal(r.authorized, false);
  assert.match(r.message, /停用/);
});

test('redactCredential：任何凭据类输入都不回显原文', () => {
  assert.equal(redactCredential('super-secret-value'), '[已脱敏 18 字符]');
  assert.equal(redactCredential(''), '');
  assert.equal(redactCredential(null), '');
  const out = redactCredential('hunter2');
  assert.ok(!out.includes('hunter2'));
});
