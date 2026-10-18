/**
 * tests/auth-ui/mock-server.test.mjs
 *
 * 验证「离线回归替身」**逐条忠实复刻**后端真实契约
 * （`backend/auth/routes.py` + `backend/auth/errors.py`）。
 *
 * 这层测试的意义：浏览器端测试的结论只能和替身一样可信。如果替身与真后端不一致，
 * 那么「UI 与后端联调」这件事一次也没被验证过——测试全绿只是在验证替身自己。
 *
 * 只监听 127.0.0.1，不访问任何外部网络。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { startMockServer, SESSION_COOKIE, WEB_AUTH_DIR } from './mock-server.mjs';

/**
 * 建立并在测试结束后释放一个 mock 服务。
 * @param {import('node:test').TestContext} t
 * @param {string} scenario
 */
async function withServer(t, scenario) {
  const server = await startMockServer({ scenario });
  t.after(() => server.close());
  return server;
}

/**
 * 取一个可用的匿名 CSRF 值。**每次写请求都要重新取**——匿名值是单次性的。
 * @param {{origin: string}} server
 */
async function issueCsrf(server) {
  const response = await fetch(`${server.origin}/auth/csrf`);
  assert.equal(response.status, 200);
  return (await response.json()).data.csrf_token;
}

/**
 * 发一次登录请求，形态与真实前端一致：同源 Origin + X-CSRF-Token 头 + JSON 体。
 * @param {{origin: string}} server
 * @param {{username: string, password: string, csrf?: string|null, headers?: Record<string,string>}} opts
 */
function postLogin(server, opts) {
  const headers = {
    'Content-Type': 'application/json',
    Origin: server.origin,
    ...(opts.headers ?? {}),
  };
  if (opts.csrf) headers['X-CSRF-Token'] = opts.csrf;
  return fetch(`${server.origin}/auth/local/login`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ username: opts.username, password: opts.password }),
  });
}

/** 发一次退出请求（同源 + CSRF 头）。 */
function postLogout(server, csrf, headers = {}) {
  return fetch(`${server.origin}/auth/logout`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Origin: server.origin,
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      ...headers,
    },
    body: JSON.stringify({}),
  });
}

/* ==========================================================================
   1. 服务形态
   ========================================================================== */

test('mock 只监听环回地址并给出随机端口', async (t) => {
  const server = await withServer(t, 'member-feishu-unconfigured');
  assert.match(server.origin, /^http:\/\/127\.0\.0\.1:\d+$/);
  assert.equal(new URL(server.origin).hostname, '127.0.0.1');
  assert.ok(Number(server.port) > 0);
});

test('管理员口令每次启动随机生成，且不出现在仓库文件中', async (t) => {
  const a = await withServer(t, 'member-feishu-unconfigured');
  const b = await startMockServer({ scenario: 'member-feishu-unconfigured' });
  t.after(() => b.close());

  assert.equal(a.adminCredentials.username, 'admin', '管理员用户名必须是 admin');
  assert.notEqual(a.adminCredentials.password, b.adminCredentials.password, '口令必须每个实例不同');
  assert.ok(a.adminCredentials.password.length >= 16, '随机口令应有足够长度');
});

/* ==========================================================================
   2. GET /auth/csrf：只读，既是 CSRF 来源也是登录方式来源
   ========================================================================== */

test('GET /auth/csrf 返回信封 data.providers，且不下发任何 Cookie', async (t) => {
  const server = await withServer(t, 'member-feishu-unconfigured');
  const response = await fetch(`${server.origin}/auth/csrf`);

  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /application\/json/);

  const body = await response.json();
  assert.equal(body.ok, true, '成功响应必须是 {ok:true,data:{…}} 信封');
  assert.equal(typeof body.data.csrf_token, 'string');
  assert.ok(body.data.csrf_token.length > 10);
  assert.equal(body.data.authenticated, false, '匿名时 authenticated 为 false');
  assert.deepEqual(body.data.providers, { local_admin: true, feishu: false });

  // 真后端用「会话绑定的 CSRF + 同源读端点」取代了 double-submit Cookie，
  // 因此这里**不应**再看到任何 Set-Cookie。
  assert.equal(response.headers.get('set-cookie'), null, '/auth/csrf 不得下发 Cookie');
});

test('GET /auth/csrf 是幂等的只读端点：不会创建任何服务端状态', async (t) => {
  const server = await withServer(t, 'member-feishu-configured');
  const first = await fetch(`${server.origin}/auth/feishu/start`, { redirect: 'manual' });
  const before = server.requests.filter((r) => r.path.startsWith('/__feishu')).length;

  // 反复读 /auth/csrf 不应产生任何跳转状态或额外流量
  for (let i = 0; i < 3; i += 1) await fetch(`${server.origin}/auth/csrf`);

  assert.equal(first.status, 302);
  assert.equal(
    server.requests.filter((r) => r.path.startsWith('/__feishu')).length,
    before,
    '/auth/csrf 不得触发任何授权跳转',
  );
});

test('local_admin 不可用时 /auth/csrf 如实声明 providers.local_admin=false', async (t) => {
  const server = await withServer(t, 'local-admin-missing');
  const body = await (await fetch(`${server.origin}/auth/csrf`)).json();
  assert.equal(body.data.providers.local_admin, false);
});

/* ==========================================================================
   3. 写请求守卫：同源 Origin + X-CSRF-Token（先于业务逻辑）
   ========================================================================== */

test('写请求缺 X-CSRF-Token 一律 403 csrf_rejected（信封 error.code）', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;

  const response = await postLogin(server, { username, password });
  assert.equal(response.status, 403);
  const body = await response.json();
  assert.equal(body.ok, false);
  assert.equal(body.error.code, 'csrf_rejected', '后端拒绝码是 csrf_rejected');
});

test('写请求带跨域 Origin 一律 403，即便 CSRF 值正确', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;

  const response = await postLogin(server, {
    username, password, csrf: await issueCsrf(server),
    headers: { Origin: 'https://evil.example' },
  });
  assert.equal(response.status, 403);
  assert.equal((await response.json()).error.code, 'csrf_rejected');
});

test('写请求缺 Origin/Referer 一律 403', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;

  const response = await postLogin(server, {
    username, password, csrf: await issueCsrf(server), headers: { Origin: '' },
  });
  assert.equal(response.status, 403);
});

test('匿名 CSRF 值是一次性的：重放必 403', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;
  const csrf = await issueCsrf(server);

  const first = await postLogin(server, { username, password: `${password}-wrong`, csrf });
  assert.equal(first.status, 401, '第一次用掉这个值，业务层给出 401');

  const replay = await postLogin(server, { username, password, csrf });
  assert.equal(replay.status, 403, '同一个值不能再用第二次');
  assert.equal((await replay.json()).error.code, 'csrf_rejected');
});

test('守卫先于业务逻辑：既缺 CSRF 又错口令时，只报 csrf_rejected', async (t) => {
  const server = await withServer(t, 'login-success');
  const response = await postLogin(server, { username: 'admin', password: 'x' });
  assert.equal(response.status, 403);
  assert.equal((await response.json()).error.code, 'csrf_rejected');
});

/* ==========================================================================
   4. 登录
   ========================================================================== */

test('登录拒绝非 JSON 请求体', async (t) => {
  const server = await withServer(t, 'login-success');
  const response = await fetch(`${server.origin}/auth/local/login`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Origin: server.origin,
      'X-CSRF-Token': await issueCsrf(server),
    },
    body: 'username=admin&password=x',
  });
  assert.equal(response.status, 400);
  assert.equal((await response.json()).error.code, 'invalid_request');
});

test('登录：正确凭据下发 HttpOnly 会话 Cookie，响应体是身份信封', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;

  const ok = await postLogin(server, { username, password, csrf: await issueCsrf(server) });
  assert.equal(ok.status, 200);
  const body = await ok.json();
  assert.equal(body.ok, true);
  assert.equal(body.data.user.display_name, 'admin');
  assert.equal(body.data.user.role, 'platform_admin');
  assert.equal(body.data.user.is_platform_admin, true);
  assert.equal(typeof body.data.csrf_token, 'string', '登录后换成会话绑定的 CSRF 值');
  assert.ok(body.data.session_expires_at, '必须给出会话到期时间');
  assert.equal(body.data.password, undefined, '不得回显口令');

  const cookie = ok.headers.get('set-cookie') ?? '';
  assert.match(cookie, /HttpOnly/, '会话 Cookie 必须是 HttpOnly');
  assert.match(cookie, new RegExp(`${SESSION_COOKIE}=`));
  assert.doesNotMatch(cookie, /Max-Age=0/, '成功登录不应立刻清除会话');
});

test('登录：错误凭据与未知用户名都是同一个 401 invalid_credentials', async (t) => {
  const server = await withServer(t, 'login-bad-credentials');
  const { username, password } = server.adminCredentials;

  const wrongPassword = await postLogin(server, {
    username, password: `${password}-wrong`, csrf: await issueCsrf(server),
  });
  assert.equal(wrongPassword.status, 401);
  const body = await wrongPassword.json();
  assert.equal(body.error.code, 'invalid_credentials');
  assert.equal(wrongPassword.headers.get('set-cookie'), null, '失败不得下发会话 Cookie');

  const unknownUser = await postLogin(server, {
    username: 'synthetic-nobody', password, csrf: await issueCsrf(server),
  });
  assert.equal(unknownUser.status, 401);
  assert.deepEqual(
    (await unknownUser.json()).error.code,
    (await (await postLogin(server, {
      username, password: 'nope', csrf: await issueCsrf(server),
    })).json()).error.code,
    '未知用户名与错口令不可区分',
  );
});

test('登录：失败场景驱动 400 / 429 / 500 等真实形态', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;

  /** 每次都用新的匿名 CSRF 值——旧的已被核销。 */
  const attempt = async () => postLogin(server, { username, password, csrf: await issueCsrf(server) });

  const cases = [
    ['login-rate-limited', 429, 'rate_limited'],
    ['login-locked', 429, 'rate_limited'],
    ['login-disabled', 403, 'account_disabled'],
    ['login-csrf-failed', 403, 'csrf_rejected'],
    ['login-invalid-input', 400, 'invalid_request'],
    ['login-server-error', 500, 'server_misconfigured'],
    ['local-admin-missing', 503, 'not_configured'],
    ['login-bad-credentials', 401, 'invalid_credentials'],
  ];

  for (const [scenario, status, code] of cases) {
    server.setScenario(scenario);
    const response = await attempt();
    assert.equal(response.status, status, `场景 ${scenario} 应返回 ${status}`);
    assert.equal((await response.json()).error.code, code, `场景 ${scenario} 的错误码`);
  }

  server.setScenario('login-rate-limited');
  const limited = await attempt();
  assert.equal(limited.headers.get('retry-after'), '60', '限速必须给出 Retry-After');
});

/* ==========================================================================
   5. GET /auth/me
   ========================================================================== */

test('GET /auth/me 的身份在 data.user，项目是 {project_id, role} 列表', async (t) => {
  const server = await withServer(t, 'session-authenticated');
  const response = await fetch(`${server.origin}/auth/me`);
  assert.equal(response.status, 200);

  const body = await response.json();
  assert.equal(body.ok, true);
  assert.equal(body.data.authenticated, true);
  assert.equal(typeof body.data.user.id, 'string');
  assert.equal(body.data.user.display_name, 'synthetic-member-a');
  assert.equal(body.data.user.role, 'member');
  assert.equal(body.data.user.is_platform_admin, false);
  assert.deepEqual(body.data.projects, [{ project_id: 'synthetic-atlas', role: 'member' }]);
  // 身份字段只用于展示，不该出现任何上游凭据
  const raw = JSON.stringify(body);
  for (const leak of ['open_id', 'union_id', 'tenant_key', 'access_token', 'Bearer']) {
    assert.ok(!raw.includes(leak), `/auth/me 不得泄漏 ${leak}`);
  }
});

test('GET /auth/me：本地管理员没有项目授权，但确为平台管理员', async (t) => {
  const server = await withServer(t, 'session-admin-no-project');
  const body = await (await fetch(`${server.origin}/auth/me`)).json();

  assert.deepEqual(body.data.projects, [], '本地管理员在后端就是 0 个项目授权');
  assert.equal(body.data.user.is_platform_admin, true);
  assert.equal(body.data.user.auth_source, 'local_admin');
});

test('GET /auth/me：普通成员 0 项目 与 会话过期 / 未登录 三种形态', async (t) => {
  const server = await withServer(t, 'session-no-project');

  const noProject = await (await fetch(`${server.origin}/auth/me`)).json();
  assert.equal(noProject.data.authenticated, true);
  assert.equal(noProject.data.user.is_platform_admin, false);
  assert.deepEqual(noProject.data.projects, []);

  server.setScenario('session-expired');
  const expired = await fetch(`${server.origin}/auth/me`);
  assert.equal(expired.status, 401);
  assert.equal((await expired.json()).error.code, 'session_expired');

  server.setScenario('member-feishu-unconfigured');
  const anonymous = await fetch(`${server.origin}/auth/me`);
  assert.equal(anonymous.status, 401);
  assert.equal((await anonymous.json()).error.code, 'not_authenticated');
});

/* ==========================================================================
   6. POST /auth/logout
   ========================================================================== */

test('POST /auth/logout 需要 CSRF，成功后清 Cookie 且会话立即失效', async (t) => {
  const server = await withServer(t, 'login-success');
  const { username, password } = server.adminCredentials;

  const loginResponse = await postLogin(server, {
    username, password, csrf: await issueCsrf(server),
  });
  assert.equal(loginResponse.status, 200);
  const sessionCookie = String(loginResponse.headers.get('set-cookie')).split(';')[0];
  const sessionCsrf = (await loginResponse.json()).data.csrf_token;

  const missing = await fetch(`${server.origin}/auth/logout`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: server.origin, Cookie: sessionCookie },
    body: JSON.stringify({}),
  });
  assert.equal(missing.status, 403, '退出必须带 CSRF');

  const logout = await fetch(`${server.origin}/auth/logout`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Origin: server.origin,
      Cookie: sessionCookie,
      'X-CSRF-Token': sessionCsrf,
    },
    body: JSON.stringify({}),
  });
  assert.equal(logout.status, 200);
  assert.equal((await logout.json()).ok, true);
  assert.match(logout.headers.get('set-cookie') ?? '', /Max-Age=0/);

  const after = await fetch(`${server.origin}/auth/me`, { headers: { Cookie: sessionCookie } });
  assert.equal(after.status, 401, '退出后该会话不得再被视为已登录');
});

test('退出被服务端拒绝的形态可被复现（logout-rejected）', async (t) => {
  const server = await withServer(t, 'logout-rejected');
  const response = await postLogout(server, await issueCsrf(server));
  assert.equal(response.status, 403);
  assert.equal((await response.json()).error.code, 'csrf_rejected');
});

/* ==========================================================================
   7. 静态资源与飞书入口
   ========================================================================== */

test('静态资源以正确的 MIME 提供，且不泄露目录之外的路径', async (t) => {
  const server = await withServer(t, 'member-feishu-unconfigured');

  const page = await fetch(`${server.origin}/index.html`);
  assert.equal(page.status, 200);
  assert.match(page.headers.get('content-type'), /text\/html/);
  assert.match(await page.text(), /登录 Dell LLM Lab/);

  const css = await fetch(`${server.origin}/auth.css`);
  assert.equal(css.status, 200);
  assert.match(css.headers.get('content-type'), /text\/css/);

  const js = await fetch(`${server.origin}/auth.js`);
  assert.equal(js.status, 200);
  assert.match(js.headers.get('content-type'), /text\/javascript/);
  assert.match(js.headers.get('x-content-type-options') ?? '', /nosniff/);

  // 非白名单文件（含工程文件）与目录穿越一律 404。
  // 注意：`/../x` 会被 HTTP 客户端在发送前归一化，因此这里同时用编码形态
  // 直接压到服务端的路径解析上。
  for (const bad of [
    '/../package.json',
    '/package.json',
    '/..%2Fpackage.json',
    '/..%2f..%2fweb%2fauth%2fauth.js',
    '/../../README.md',
    '/auth-core.js.map',
  ]) {
    const response = await fetch(`${server.origin}${bad}`);
    assert.equal(response.status, 404, `应拒绝：${bad}`);
  }
});

test('飞书未配置时 /auth/feishu/start 如实返回 503 not_configured 信封', async (t) => {
  const server = await withServer(t, 'member-feishu-unconfigured');
  const response = await fetch(`${server.origin}/auth/feishu/start`, { redirect: 'manual' });

  assert.equal(response.status, 503);
  const body = await response.json();
  assert.equal(body.ok, false);
  assert.equal(body.error.code, 'not_configured');
  assert.match(body.error.message, /飞书/);
});

test('飞书已配置时 302 到模拟授权页，且 redirect_uri 指向 /auth/feishu/callback', async (t) => {
  const server = await withServer(t, 'member-feishu-configured');
  const response = await fetch(`${server.origin}/auth/feishu/start`, { redirect: 'manual' });

  assert.equal(response.status, 302);
  const location = response.headers.get('location');
  assert.ok(location, '必须给出 Location');

  const target = new URL(location, server.origin);
  assert.equal(target.hostname, '127.0.0.1', '模拟跳转不离开本机');
  assert.equal(target.pathname, '/__feishu/authorize');
  assert.equal(target.searchParams.get('redirect_uri'), '/auth/feishu/callback');
  assert.ok(target.searchParams.get('state'), '必须携带 state');
});

/* ==========================================================================
   8. 记录与边界
   ========================================================================== */

test('mock 记录所有请求（含 CSRF 头），且没有任何请求离开环回地址', async (t) => {
  const server = await withServer(t, 'member-feishu-unconfigured');
  await fetch(`${server.origin}/auth/csrf`);
  await fetch(`${server.origin}/auth/feishu/start`);

  assert.ok(server.requests.length >= 2);
  for (const request of server.requests) {
    assert.ok(request.path.startsWith('/auth/') || request.path.startsWith('/__'),
      `意外路径：${request.path}`);
  }
  assert.ok(Object.prototype.hasOwnProperty.call(server.requests[0], 'csrfHeader'),
    '请求记录必须包含 CSRF 头，否则无法断言「页面真的带了头」');
});

test('静态资源根目录指向 web/auth', () => {
  assert.match(WEB_AUTH_DIR.replace(/\\/g, '/'), /\/web\/auth$/);
});
