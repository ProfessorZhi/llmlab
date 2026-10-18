/**
 * tests/auth-ui/mock-server.mjs
 *
 * 登录页的**离线回归替身**：在 127.0.0.1 随机端口上逐条复刻后端真实契约
 * （`backend/auth/routes.py` + `backend/auth/errors.py`），供 Node 与 Chromium 测试使用。
 *
 * 为什么必须复刻而不是「自洽地编一套」：如果替身和真后端各说各话，那么「UI 与
 * 后端联调」这件事就一次也没被验证过——测试全绿也只是在验证替身自己。下面每一条
 * 都对应后端的真实行为：
 *
 *  1. 所有成功响应是 `{"ok":true,"data":{…}}`，所有失败响应是
 *     `{"ok":false,"error":{"code":…,"message":…}}`（`routes.py` 的信封）。
 *  2. `GET /auth/csrf` 是**只读**的，既是 CSRF 值的来源，也是登录方式可用性
 *     （`data.providers`）的唯一来源；它**不下发任何 Cookie**。
 *  3. 写请求（POST）必须同时满足：同源 `Origin`/`Referer` **且** `X-CSRF-Token`
 *     命中一个未用过、未过期的值；否则 403 `csrf_rejected`。守卫在 `_preflight`
 *     里，在进入业务逻辑**之前**——所以「口令对不对」根本轮不到被判断。
 *  4. 匿名 CSRF 值是**一次性的**（`verify_write_csrf` → `consume_login_state`），
 *     用掉即废，重放必 403。有会话时才换成会话绑定的稳定值。
 *  5. `/auth/me` 的身份在 `data.user` 里，项目授权是 `data.projects` 的
 *     `{project_id, role}` 列表。本地管理员**没有任何项目授权**（`projects: []`）
 *     但 `is_platform_admin: true`。
 *
 * 纪律：
 *  - 只监听 127.0.0.1，端口随机；每次运行结束由测试关闭；
 *  - 不访问任何外部网络：飞书跳转被模拟成本源 `/__feishu/authorize` 页面；
 *  - 管理员口令**每次启动随机生成**，不写入仓库、不打印到日志，
 *    仅通过返回的句柄在测试进程内可见；
 *  - 所有身份、项目、用量数据均为合成的虚构值。
 */

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { randomUUID, randomBytes, timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join, normalize, extname } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
/** 被测试的静态资源根目录。 */
export const WEB_AUTH_DIR = normalize(join(HERE, '..', '..', 'web', 'auth'));

const MIME = Object.freeze({
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
});

/** 允许被静态服务的文件后缀，其余一律 404。 */
const STATIC_EXT = new Set(Object.keys(MIME));

/**
 * 静态服务白名单：登录页只应当暴露这几个资源。
 * 显式 allowlist 而非「按目录放行」，避免 `package.json` 之类的工程文件被顺手服务出去。
 */
const SERVABLE = new Set(['index.html', 'auth.css', 'auth.js', 'auth-core.js']);

/** 会话 Cookie 名，与后端 `AuthConfig.cookie_name` 一致。 */
export const SESSION_COOKIE = 'llm_lab_session';

/** 支持的场景名。每个场景决定各端点的行为。 */
export const SCENARIOS = Object.freeze([
  'member-feishu-unconfigured',
  'member-feishu-configured',
  'local-admin-missing',
  'session-authenticated',
  'session-no-project',
  'session-admin-no-project',
  'session-expired',
  'login-success',
  'login-bad-credentials',
  'login-locked',
  'login-disabled',
  'login-rate-limited',
  'login-csrf-failed',
  'login-invalid-input',
  'login-server-error',
  'logout-rejected',
  'api-down',
  // Lane B：Linux PAM 就绪门槛。只有 `pam-ready*` 才声明 linux_pam 可用，
  // 其余场景要么不声明该 provider，要么明确声明为不可用 / 范围不安全。
  'pam-ready',
  'pam-ready-bad-credentials',
  'pam-disabled',
  'pam-unsafe-scope',
  // Lane B：`/auth/csrf` 的传输层失败形态——都必须让表单 fail-closed。
  'csrf-html',
  'csrf-401',
  'csrf-403',
  'csrf-timeout',
]);

/* ==========================================================================
   信封
   ========================================================================== */

/** 成功信封：`{"ok":true,"data":{…}}`。 */
function okBody(data) {
  return { ok: true, data };
}

/** 失败信封：`{"ok":false,"error":{"code","message"}}`。 */
function errorBody(code, message) {
  return { ok: false, error: { code, message } };
}

/**
 * 构造响应。
 * @param {import('node:http').ServerResponse} res
 * @param {number} status
 * @param {any} body
 * @param {Record<string,string|string[]>} [headers]
 */
function sendJson(res, status, body, headers = {}) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Content-Length': Buffer.byteLength(payload),
    ...headers,
  });
  res.end(payload);
}

/**
 * 读取请求体。
 * @param {import('node:http').IncomingMessage} req
 * @returns {Promise<string>}
 */
function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > 64 * 1024) { req.destroy(); resolve(''); return; }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', () => resolve(''));
  });
}

/**
 * 恒定时间比较，避免用长度或提前返回泄露信息。
 * @param {string} a
 * @param {string} b
 */
function safeEqual(a, b) {
  const bufA = Buffer.from(String(a), 'utf8');
  const bufB = Buffer.from(String(b), 'utf8');
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

/** 解析 Cookie 头。 */
function parseCookies(header) {
  const jar = new Map();
  for (const part of String(header ?? '').split(';')) {
    const eq = part.indexOf('=');
    if (eq < 0) continue;
    const name = part.slice(0, eq).trim();
    if (name) jar.set(name, part.slice(eq + 1).trim());
  }
  return jar;
}

/* ==========================================================================
   各场景下的身份 / 项目
   ========================================================================== */

/** 合成成员身份（普通成员，非平台管理员）。 */
const MEMBER = Object.freeze({
  id: 'synthetic-user-member-a',
  display_name: 'synthetic-member-a',
  role: 'member',
  auth_source: 'feishu',
  is_platform_admin: false,
});

/** 合成的 Dell Linux 账号身份（`auth_source: 'linux_pam'`），同样无项目授权。 */
const LINUX_ACCOUNT = Object.freeze({
  id: 'synthetic-user-linux-pam',
  display_name: 'alice',
  role: 'member',
  auth_source: 'linux_pam',
  is_platform_admin: false,
});

/** 合成本地管理员身份：**没有任何项目授权**，但确为平台管理员。 */
const ADMIN = Object.freeze({
  id: 'synthetic-user-local-admin',
  display_name: 'admin',
  role: 'platform_admin',
  auth_source: 'local_admin',
  is_platform_admin: true,
});

/**
 * 启动 mock 认证服务。
 *
 * @param {{scenario?: string}} [options]
 * @returns {Promise<{
 *   port: number, origin: string, baseUrl: string,
 *   state: any, requests: Array<any>, adminCredentials: {username: string, password: string},
 *   setScenario: (name: string) => void, close: () => Promise<void>
 * }>}
 */
export async function startMockServer(options = {}) {
  /** 每次启动随机生成，仅存在于测试进程内存中。 */
  const adminCredentials = Object.freeze({
    username: 'admin',
    password: `mock-${randomBytes(18).toString('base64url')}`,
  });

  /** Dell Linux 账号（合成）。与管理员凭据**不同源**：这是 PAM 那条路线专属的。 */
  const linuxCredentials = Object.freeze({
    username: 'alice',
    password: `mock-pam-${randomBytes(18).toString('base64url')}`,
  });

  const state = {
    scenario: options.scenario ?? 'member-feishu-unconfigured',
    /** 未使用的匿名 CSRF 值（一次性）。 */
    anonymousCsrf: new Set(),
    /** 已用过 / 已失效的匿名 CSRF 值，用于「重放必败」的断言。 */
    spentCsrf: new Set(),
    /** 当前会话。 */
    session: null,
    loginAttempts: 0,
  };

  /** 记录所有收到的请求，供断言使用。 */
  const requests = [];

  /** 场景 ⇒ 登录方式可用性（对应后端 `AuthService.providers()`）。 */
  function providers() {
    const feishu = state.scenario === 'member-feishu-configured'
      || /^(session-|login-|logout-)/.test(state.scenario)
      || state.scenario === 'api-down';
    const localAdmin = state.scenario !== 'local-admin-missing';
    const out = { local_admin: localAdmin, feishu };
    const linuxPam = linuxPamDeclaration();
    if (linuxPam) out.linux_pam = linuxPam;
    return out;
  }

  /**
   * 场景 ⇒ `providers.linux_pam` 的声明。
   *
   * 默认（包括所有旧飞书 / 管理员场景）**完全不声明**这个键——这正是「旧 provider
   * 已启用不得让 Linux 表单可用」的那条契约在替身侧的对应实现。只有显式的
   * `pam-*` 场景才会给出声明，且 `pam-unsafe-scope` 故意给出 root:true。
   *
   * @returns {{enabled: boolean, uid_scope: string, root: boolean,
   *            service_account: boolean}|null}
   */
  function linuxPamDeclaration() {
    switch (state.scenario) {
      case 'pam-ready':
      case 'pam-ready-bad-credentials':
        return {
          enabled: true,
          uid_scope: 'dell-linux-account',
          root: false,
          service_account: false,
        };
      case 'pam-disabled':
        return {
          enabled: false,
          uid_scope: 'dell-linux-account',
          root: false,
          service_account: false,
        };
      case 'pam-unsafe-scope':
        return {
          enabled: true,
          uid_scope: 'dell-linux-account',
          root: true,
          service_account: false,
        };
      default:
        return null;
    }
  }

  /** 场景 ⇒ `/auth/me` 的结果。 */
  function meResult() {
    switch (state.scenario) {
      case 'session-expired':
        return {
          status: 401,
          body: errorBody('session_expired', '登录状态已过期，请重新登录。'),
        };
      case 'session-no-project':
        return {
          status: 200,
          body: okBody({
            authenticated: true,
            user: MEMBER,
            projects: [],
            session_expires_at: '2026-10-14T12:00:00Z',
          }),
        };
      case 'session-admin-no-project':
        return {
          status: 200,
          body: okBody({
            authenticated: true,
            user: ADMIN,
            projects: [],
            session_expires_at: '2026-10-14T12:00:00Z',
          }),
        };
      case 'session-authenticated':
        return {
          status: 200,
          body: okBody({
            authenticated: true,
            user: MEMBER,
            projects: [{ project_id: 'synthetic-atlas', role: 'member' }],
            session_expires_at: '2026-10-14T12:00:00Z',
          }),
        };
      default:
        break;
    }
    // 登录态只在「本次场景就是登录成功」时生效，避免跨用例的会话残留
    if (state.session && state.scenario === 'login-success') {
      return {
        status: 200,
        body: okBody({
          authenticated: true,
          user: ADMIN,
          // 本地管理员在后端就是 0 个项目授权
          projects: [],
          session_expires_at: '2026-10-14T12:00:00Z',
        }),
      };
    }
    return {
      status: 401,
      body: errorBody('not_authenticated', '当前请求未携带有效会话。'),
    };
  }

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');
    const path = url.pathname;
    const cookies = parseCookies(req.headers.cookie);
    const sessionId = cookies.get(SESSION_COOKIE) ?? null;
    const hasLiveSession = Boolean(sessionId) && Boolean(state.session)
      && state.session.id === sessionId;

    requests.push({
      method: req.method,
      path,
      search: url.search,
      origin: req.headers.origin ?? null,
      referer: req.headers.referer ?? null,
      contentType: req.headers['content-type'] ?? null,
      accept: req.headers.accept ?? null,
      cookies: req.headers.cookie ?? null,
      csrfHeader: req.headers['x-csrf-token'] ?? null,
      body: null,
    });
    const record = requests[requests.length - 1];

    /* ---- 测试控制面（不属于产品契约） ---------------------------------- */
    if (path === '/__control') {
      const raw = await readBody(req);
      try {
        const patch = JSON.parse(raw || '{}');
        if (typeof patch.scenario === 'string') state.scenario = patch.scenario;
        if (patch.session === 'clear') state.session = null;
        if (patch.resetRequests === true) requests.length = 0;
      } catch { /* 忽略非法控制请求 */ }
      sendJson(res, 200, okBody({ scenario: state.scenario }));
      return;
    }

    /* ---- 认证服务整体不可达：直接掐断连接，制造真实的网络层失败 ---------- */
    // 页面据此必须进入「离线」分支，而不是把失败当成未登录或伪造成功。
    if (state.scenario === 'api-down' && path.startsWith('/auth/')) {
      req.socket.destroy();
      return;
    }

    /* ---- 模拟的飞书授权页（本源，不产生外部流量） ---------------------- */
    if (path === '/__feishu/authorize') {
      const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>模拟飞书授权页</title></head><body>
<h1>模拟飞书授权页（合成测试页）</h1>
<p>本页由本地 mock 服务生成，代表真实环境中的飞书 OAuth 授权页。</p>
<p id="client">client_id: ${url.searchParams.get('client_id') ?? ''}</p>
<p id="redirect">redirect_uri: ${url.searchParams.get('redirect_uri') ?? ''}</p>
<p id="state">state: ${url.searchParams.get('state') ?? ''}</p>
</body></html>`;
      res.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
      });
      res.end(html);
      return;
    }

    /* ---- 登录成功后的站内首页（合成测试页） ---------------------------- */
    if (path === '/') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        + '<title>Portal 首页（合成测试页）</title></head>'
        + '<body><h1 id="portal-home">Portal 首页 · 合成测试页</h1></body></html>');
      return;
    }

    /* ---- 浏览器的自动请求：明确回 204，避免与「资源缺失」混淆 ---------- */
    if (path === '/favicon.ico') {
      res.writeHead(204, { 'Cache-Control': 'no-store' });
      res.end();
      return;
    }

    /* ======================================================================
       认证契约
       ====================================================================== */

    /**
     * 后端 `_preflight`：写请求先过同源 + CSRF 两道关，**在业务逻辑之前**。
     * @returns {boolean} 是否放行
     */
    const preflight = () => {
      if (!(req.method === 'POST' || req.method === 'PUT' || req.method === 'PATCH'
        || req.method === 'DELETE')) return true;

      const candidate = req.headers.origin ?? req.headers.referer ?? null;
      let sameOrigin = false;
      if (candidate) {
        try {
          sameOrigin = new URL(candidate).origin === origin;
        } catch { sameOrigin = false; }
      }
      if (!sameOrigin) {
        sendJson(res, 403, errorBody('csrf_rejected', '请求来源不是同源页面。'));
        return false;
      }

      const token = req.headers['x-csrf-token'];
      if (typeof token !== 'string' || token === '') {
        sendJson(res, 403, errorBody('csrf_rejected', '缺少 X-CSRF-Token 请求头。'));
        return false;
      }
      // 有会话时用会话绑定的稳定值；否则匿名值一次性核销（重放必败）
      if (hasLiveSession && state.session.csrfToken && safeEqual(token, state.session.csrfToken)) {
        return true;
      }
      if (state.anonymousCsrf.has(token)) {
        state.anonymousCsrf.delete(token);
        state.spentCsrf.add(token);
        return true;
      }
      sendJson(res, 403, errorBody('csrf_rejected', '校验值缺失、不正确或已被使用过。'));
      return false;
    };

    if (path === '/auth/csrf') {
      // Lane B：先处理传输层失败形态。这几种都不该让登录表单可用。
      if (state.scenario === 'csrf-html') {
        // 最阴险的一种：HTTP 200 + HTML 正文（例如被 BasicAuth 门页或反代拦截）。
        res.writeHead(200, {
          'Content-Type': 'text/html; charset=utf-8',
          'Cache-Control': 'no-store',
        });
        res.end('<!doctype html><html><body><h1>需要登录</h1></body></html>');
        return;
      }
      if (state.scenario === 'csrf-401') {
        sendJson(res, 401, errorBody('unauthenticated', '需要先通过 BasicAuth。'));
        return;
      }
      if (state.scenario === 'csrf-403') {
        sendJson(res, 403, errorBody('forbidden', '当前身份无权读取认证状态。'));
        return;
      }
      if (state.scenario === 'csrf-timeout') {
        // 持有连接不响应，逼客户端走自己的超时分支（页面 5s 上限）。
        setTimeout(() => {
          try {
            sendJson(res, 200, okBody({ csrf_token: 'late', authenticated: false, providers: {} }));
          } catch { /* 客户端可能已经断开 */ }
        }, 12000);
        return;
      }

      // 只读端点：既给 CSRF 值，也声明可用的登录方式；**不下发任何 Cookie**。
      let token;
      if (hasLiveSession && state.session.csrfToken) {
        token = state.session.csrfToken;
      } else {
        token = randomUUID();
        state.anonymousCsrf.add(token);
      }
      sendJson(res, 200, okBody({
        csrf_token: token,
        authenticated: hasLiveSession,
        providers: providers(),
      }));
      return;
    }

    if (path === '/auth/feishu/start') {
      if (state.scenario === 'member-feishu-configured'
        || /^(session-|login-|logout-)/.test(state.scenario)) {
        const target = `/__feishu/authorize?client_id=mock-client`
          + `&redirect_uri=${encodeURIComponent('/auth/feishu/callback')}`
          + '&response_type=code&state=' + encodeURIComponent(`st-${randomBytes(6).toString('hex')}`);
        res.writeHead(302, { Location: target, 'Cache-Control': 'no-store' });
        res.end();
        return;
      }
      sendJson(res, 503, errorBody('not_configured', '飞书单点登录尚未在本环境启用。'));
      return;
    }

    if (path === '/auth/feishu/callback') {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      res.end('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        + '<title>飞书回调（合成测试页）</title></head>'
        + '<body><h1 id="feishu-callback">飞书回调已处理（合成测试页）</h1></body></html>');
      return;
    }

    if (path === '/auth/me') {
      const result = meResult();
      sendJson(res, result.status, result.body);
      return;
    }

    if (path === '/auth/logout' && req.method === 'POST') {
      record.body = await readBody(req);
      if (!preflight()) return;
      if (state.scenario === 'logout-rejected') {
        sendJson(res, 403, errorBody('csrf_rejected', '校验值缺失、不正确或已被使用过。'));
        return;
      }
      state.session = null;
      sendJson(res, 200, okBody({ authenticated: false }), {
        'Set-Cookie': `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`,
      });
      return;
    }

    if (path === '/auth/local/login' && req.method === 'POST') {
      const raw = await readBody(req);
      record.body = raw;
      state.loginAttempts += 1;

      // 守卫在前：口令是否正确根本轮不到被判断
      if (!preflight()) return;

      const scenario = state.scenario;

      if (scenario === 'local-admin-missing') {
        sendJson(res, 503, errorBody('not_configured', '本地管理员尚未完成初始化。'));
        return;
      }
      if (scenario === 'login-rate-limited') {
        sendJson(res, 429, errorBody('rate_limited', '尝试过于频繁，请稍后再试。'),
          { 'Retry-After': '60' });
        return;
      }
      if (scenario === 'login-locked') {
        // 后端把「失败锁定」表达成同一个 429，只是 Retry-After 更长
        sendJson(res, 429, errorBody('rate_limited', '账号因多次登录失败已被暂时锁定。'),
          { 'Retry-After': '900' });
        return;
      }
      if (scenario === 'login-csrf-failed') {
        sendJson(res, 403, errorBody('csrf_rejected', '校验值缺失、不正确或已被使用过。'));
        return;
      }
      if (scenario === 'login-disabled') {
        sendJson(res, 403, errorBody('account_disabled', '该账号已被停用，请联系管理员。'));
        return;
      }
      if (scenario === 'login-server-error') {
        sendJson(res, 500, errorBody('server_misconfigured', '认证服务配置异常。'));
        return;
      }

      // 请求体必须是 JSON 且含 username / password（后端 `_route_local_login`）
      const isJson = String(req.headers['content-type'] ?? '').includes('application/json');
      let parsed = null;
      try { parsed = JSON.parse(raw); } catch { parsed = null; }
      if (!isJson || parsed === null || typeof parsed.username !== 'string'
        || typeof parsed.password !== 'string' || !parsed.username || !parsed.password) {
        sendJson(res, 400, errorBody('invalid_request', 'username and password are required'));
        return;
      }
      if (scenario === 'login-invalid-input') {
        sendJson(res, 400, errorBody('invalid_request', 'username and password are required'));
        return;
      }

      // 错误口令与未知用户名返回同一个 401，不泄露「账号是否存在」
      if (!safeEqual(parsed.username, adminCredentials.username)
        || !safeEqual(parsed.password, adminCredentials.password)) {
        sendJson(res, 401, errorBody('invalid_credentials', '用户名或口令不正确。'));
        return;
      }
      if (scenario !== 'login-success') {
        // 场景没切到 login-success 时，正确凭据也不放行，避免误判
        sendJson(res, 401, errorBody('invalid_credentials', '用户名或口令不正确。'));
        return;
      }

      state.session = { id: randomUUID(), csrfToken: randomUUID() };
      sendJson(res, 200, okBody({
        user: ADMIN,
        csrf_token: state.session.csrfToken,
        session_expires_at: '2026-10-14T12:00:00Z',
      }), {
        'Set-Cookie': `${SESSION_COOKIE}=${state.session.id}; Path=/; HttpOnly; SameSite=Lax`,
      });
      return;
    }

    if (path === '/auth/linux/login' && req.method === 'POST') {
      const raw = await readBody(req);
      record.body = raw;
      state.loginAttempts += 1;

      // 与本地管理员同样的守卫顺序：同源 + CSRF 在业务逻辑之前。
      if (!preflight()) return;

      const scenario = state.scenario;

      // 服务端没有把 Linux PAM provider 打开时，这条路由本身就不该工作。
      const declaration = linuxPamDeclaration();
      if (!declaration || declaration.enabled !== true) {
        sendJson(res, 503, errorBody('not_configured', 'Linux PAM 服务端登录尚未开通。'));
        return;
      }

      if (scenario === 'pam-ready-bad-credentials') {
        sendJson(res, 401, errorBody('invalid_credentials', 'Linux 账号或口令不正确。'));
        return;
      }

      const isJson = String(req.headers['content-type'] ?? '').includes('application/json');
      let parsed = null;
      try { parsed = JSON.parse(raw); } catch { parsed = null; }
      if (!isJson || parsed === null || typeof parsed.username !== 'string'
        || typeof parsed.password !== 'string' || !parsed.username || !parsed.password) {
        sendJson(res, 400, errorBody('invalid_request', 'username and password are required'));
        return;
      }

      // 错误口令与未知 Linux 账号返回同一个 401，不泄露账号是否存在。
      if (!safeEqual(parsed.username, linuxCredentials.username)
        || !safeEqual(parsed.password, linuxCredentials.password)) {
        sendJson(res, 401, errorBody('invalid_credentials', 'Linux 账号或口令不正确。'));
        return;
      }

      state.session = { id: randomUUID(), csrfToken: randomUUID() };
      sendJson(res, 200, okBody({
        user: LINUX_ACCOUNT,
        csrf_token: state.session.csrfToken,
        session_expires_at: '2026-10-14T12:00:00Z',
      }), {
        'Set-Cookie': `${SESSION_COOKIE}=${state.session.id}; Path=/; HttpOnly; SameSite=Lax`,
      });
      return;
    }

    /* ---- 静态资源 -------------------------------------------------------- */
    if (req.method === 'GET' || req.method === 'HEAD') {
      const relative = normalize(path).replace(/^([/\\])+/, '');
      if (relative === '' || relative.startsWith('..')) {
        sendJson(res, 404, errorBody('not_found', '资源不存在。'));
        return;
      }
      const ext = extname(relative);
      if (!STATIC_EXT.has(ext) || !SERVABLE.has(relative)) {
        sendJson(res, 404, errorBody('not_found', '资源不存在。'));
        return;
      }
      const filePath = join(WEB_AUTH_DIR, relative);
      if (!filePath.startsWith(WEB_AUTH_DIR)) {
        sendJson(res, 404, errorBody('not_found', '资源不存在。'));
        return;
      }
      try {
        const content = await readFile(filePath);
        res.writeHead(200, {
          'Content-Type': MIME[ext],
          'Cache-Control': 'no-store',
          'X-Content-Type-Options': 'nosniff',
          'Content-Length': content.length,
        });
        if (req.method === 'HEAD') res.end(); else res.end(content);
      } catch {
        sendJson(res, 404, errorBody('not_found', '资源不存在。'));
      }
      return;
    }

    sendJson(res, 405, errorBody('method_not_allowed', '方法不被允许。'));
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  const origin = `http://127.0.0.1:${port}`;

  return {
    port,
    origin,
    baseUrl: `${origin}/auth/`,
    state,
    requests,
    adminCredentials,
    linuxCredentials,
    /**
     * 切换场景。**同时清空会话**：否则上一个用例登录成功后留下的 Cookie，
     * 会让后续用例的 `/auth/me` 一直返回「已登录」，页面直接跳走。
     */
    setScenario(name) {
      state.scenario = name;
      state.session = null;
    },
    close() {
      return new Promise((resolve) => server.close(() => resolve()));
    },
  };
}
