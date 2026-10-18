/**
 * web/auth/auth-core.js
 *
 * 登录页的**纯逻辑层**：不触碰 DOM、不发起请求、不读写任何存储。
 * 浏览器（`auth.js`）与 Node 测试（`tests/auth-ui/**`）共用本模块，
 * 因此这里的每条规则都可以脱离浏览器被直接验证。
 *
 * 为什么是 UMD 而不是 ES module：
 *   本页需要能以 `file://` 直接打开做静态预览。Chrome / Firefox 会把
 *   `file://` 下的 `<script type="module">` 当作跨源请求拦截，ESM 版本在
 *   双击打开时完全不执行。经典脚本不受此限制，因此这里用一个无依赖的
 *   UMD 包装：浏览器挂到 `window.LLMAuthCore`，Node 走 `module.exports`，
 *   两种环境共用同一份代码，不需要任何构建步骤。
 *
 * 安全前提：
 *  - 本模块不接收、不返回、不缓存任何会话凭据；
 *  - 唯一的「凭据类」输入是用户键入的 username/password，仅用于本地校验，
 *    绝不写入存储、日志或页面；
 *  - 所有对外跳转目标都经过 `sanitizeNextPath` 收敛为同源站内路径。
 */

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;           // Node（测试）
  } else {
    root.LLMAuthCore = api;         // 浏览器
  }
}(typeof globalThis === 'object' && globalThis ? globalThis : this, function () {
  'use strict';

  /* ========================================================================
     1. 页面状态（与 index.html 的 [data-view] 一一对应）
     ======================================================================== */

  /** 页面可能处于的视图。 */
  const VIEW = Object.freeze({
    BOOT: 'boot',
    /** 唯一的登录入口：Dell Linux 账号。 */
    LINUX: 'linux',
    NO_PROJECT: 'no-project',
    LOGGED_OUT: 'logged-out',
    OFFLINE: 'offline',
  });

  /** 登录成功后默认落脚点。 */
  const DEFAULT_LANDING = '/';

  /* ========================================================================
     2. 运行环境判定
     ======================================================================== */

  /**
   * 判断当前是否为「静态预览」环境：即页面不是由同源 HTTP(S) 服务提供的。
   *
   * 在 `file://` 下打开静态页面时，**绝不允许**发送真实登录请求：
   * 既没有同源后端可依赖，`Origin` 语义也不成立。
   *
   * @param {{protocol?: string}} locationLike 形如 `window.location` 的对象
   * @returns {boolean} true 表示只允许离线预览，禁止发请求
   */
  function isStaticPreview(locationLike) {
    const protocol = String(locationLike && locationLike.protocol || '');
    return protocol !== 'http:' && protocol !== 'https:';
  }

  /* ========================================================================
     3. 跳转目标收敛（开放重定向防护）
     ======================================================================== */

  /**
   * 把任意外部输入收敛成一个**站内绝对路径**，否则回退到 `/`。
   *
   * 拒绝的形态：
   *  - 绝对 URL（`https://evil.example`）与协议相对 URL（`//evil.example`）；
   *  - 反斜杠变体（`/\evil.example`、`\\evil.example`）——浏览器把 `\` 视作 `/`；
   *  - 非路径 scheme（`javascript:`、`data:`、`vbscript:`）；
   *  - 含**控制字符**（含制表符与换行）的值——解析器绕过的经典载体；
   *  - 去掉首尾空白后仍含空白的值；
   *  - 不以单个 `/` 开头的相对值（避免相对当前目录逃逸）。
   *
   * 首尾的普通空白会被容忍（`?next=%20/atlas` 属常见编码痕迹），
   * 但**先做控制字符检查再 trim**，避免 `"/atlas\t"` 这类值被 trim 洗白。
   *
   * @param {unknown} raw 来自 `?next=` 等不可信来源的值
   * @returns {string} 必定以 `/` 开头、且不含 `//` 起始的站内路径
   */
  function sanitizeNextPath(raw) {
    if (typeof raw !== 'string') return DEFAULT_LANDING;

    // 控制字符：先于 trim 检查，防止被 trim 吞掉后再放行
    if (/[\u0000-\u001F\u007F\u0080-\u009F]/.test(raw)) return DEFAULT_LANDING;

    const value = raw.trim();
    if (value === '') return DEFAULT_LANDING;

    // trim 之后仍含空白（内部空格）一律拒绝
    if (/\s/.test(value)) return DEFAULT_LANDING;

    // 任何 scheme 或协议相对写法
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value)) return DEFAULT_LANDING;

    // 反斜杠变体（`/\evil`、`\\evil`、`/\/evil`）
    if (value.indexOf('\\') !== -1) return DEFAULT_LANDING;

    // 必须以单个 `/` 开头
    if (value.charAt(0) !== '/') return DEFAULT_LANDING;

    // 禁止协议相对 URL：`//host`
    if (value.charAt(1) === '/') return DEFAULT_LANDING;

    return value;
  }

  /**
   * 从查询串中取出 `next` 并收敛。同时兼容 `redirect` / `return_to` 两个别名。
   *
   * @param {string} search 形如 `?next=/atlas`
   * @returns {string} 安全的站内路径
   */
  function nextFromSearch(search) {
    let params;
    try {
      params = new URLSearchParams(String(search == null ? '' : search).replace(/^\?/, ''));
    } catch (error) {
      return DEFAULT_LANDING;
    }
    const keys = ['next', 'redirect', 'return_to'];
    for (let i = 0; i < keys.length; i += 1) {
      if (params.has(keys[i])) return sanitizeNextPath(params.get(keys[i]));
    }
    return DEFAULT_LANDING;
  }

  /**
   * 是否为指向**已退役**旧入口的链接（`?mode=admin` / `?mode=member` / `#admin`）。
   *
   * 成员 / 管理员双入口已退役，登录页只剩 Dell Linux 账号一个入口。这里保留一个
   * 可检验的判定，是为了让带着旧书签来的人**被告知入口已退役**，而不是被静默地
   * 送到一个看起来一样的表单上、以为自己进的是「管理员登录」。
   *
   * @param {string} search 查询串
   * @param {string} hash 片段
   * @returns {boolean}
   */
  function isRetiredEntryLink(search, hash) {
    const combined = String(search == null ? '' : search) + String(hash == null ? '' : hash);
    if (/[?&#](admin|member)\b/.test(combined)) return true;
    try {
      const value = new URLSearchParams(
        String(search == null ? '' : search).replace(/^\?/, ''),
      ).get('mode');
      return value === 'admin' || value === 'member';
    } catch (error) {
      return false;
    }
  }

  /**
   * 是否为「刚刚退出登录」的落地。
   * @param {string} search
   * @returns {boolean}
   */
  function isLoggedOutLanding(search) {
    try {
      return new URLSearchParams(String(search == null ? '' : search).replace(/^\?/, ''))
        .has('logged_out');
    } catch (error) {
      return false;
    }
  }

  /* ========================================================================
     4. 登录表单本地校验
     ======================================================================== */

  /**
   * 提交前的本地校验。**只做空值与非空判定**，不尝试猜测口令内容、
   * 不内置任何默认账号或默认口令。
   *
   * @param {{username?: string, password?: string}} input
   * @returns {{ok: boolean, field: null|'username'|'password', message: string}}
   */
  function validateLoginForm(input) {
    const username = typeof (input && input.username) === 'string' ? input.username.trim() : '';
    const password = typeof (input && input.password) === 'string' ? input.password : '';

    if (username === '' && password === '') {
      return { ok: false, field: 'username', message: '请输入用户名和口令。' };
    }
    if (username === '') {
      return { ok: false, field: 'username', message: '请输入用户名。' };
    }
    if (password === '') {
      return { ok: false, field: 'password', message: '请输入口令。' };
    }
    return { ok: true, field: null, message: '' };
  }

  /* ========================================================================
     5. 响应信封（后端契约 {ok, data} / {ok, error:{code, message}}）
     ======================================================================== */

  /**
   * 取出成功响应里的 `data` 载荷。
   *
   * 后端所有成功响应都是 `{"ok":true,"data":{…}}`。这里同时容忍「没有信封、
   * 字段直接平铺在顶层」的历史形态——上线前 UI 与后端曾各自约定过一套形状，
   * 兼容层让两者并存期间页面不会瞎猜。**信封里没有 `data` 时不会退化成把顶层
   * 当成 data**：`{ok:false,…}` 与 `{}` 都返回 `null`，交给调用方走失败分支。
   *
   * @param {any} payload 响应体
   * @returns {object|null} data 对象；不是对象时为 null
   */
  function envelopeData(payload) {
    if (!payload || typeof payload !== 'object') return null;
    if (payload.ok === false) return null;
    if (payload.data && typeof payload.data === 'object' && !Array.isArray(payload.data)) {
      return payload.data;
    }
    if (Object.prototype.hasOwnProperty.call(payload, 'data')) return null;
    return payload; // 无信封的平铺形态
  }

  /**
   * 归一化服务端错误码：兼容 `{error:{code}}` / `{error}` / `{code}` 三种形态。
   * @param {any} payload 响应体
   * @returns {string} 小写短码，无则空串
   */
  function normalizeErrorCode(payload) {
    if (!payload || typeof payload !== 'object') return '';
    let raw = payload.code;
    if (raw === undefined) raw = payload.error_code;
    if (raw === undefined) {
      raw = typeof payload.error === 'string' ? payload.error : payload.error && payload.error.code;
    }
    return typeof raw === 'string' ? raw.trim().toLowerCase().replace(/[\s-]+/g, '_') : '';
  }

  /**
   * 取服务端可展示的文案。
   *
   * 真实失败信封是 `{"ok":false,"error":{"code":…,"message":…}}`，文案在
   * `error.message` 里；平铺形态则直接在顶层 `message`。这里统一取。
   * 一律回服务端原文而不是自己编一句，避免「后端说 A、页面说 B」。
   *
   * @param {any} payload
   * @returns {string} 文案，无则空串
   */
  function serverMessage(payload) {
    if (!payload || typeof payload !== 'object') return '';
    const candidates = [
      payload.error && typeof payload.error === 'object' ? payload.error.message : undefined,
      payload.message,
    ];
    for (const candidate of candidates) {
      if (typeof candidate === 'string' && candidate.trim() !== '') return candidate.trim();
    }
    return '';
  }

  /**
   * 把 `Retry-After` 解析为秒数。支持「秒数」与「HTTP 日期」两种形式。
   *
   * @param {string|null|undefined} value 响应头原值
   * @param {number} [nowMs] 当前时间，便于测试注入
   * @returns {number|null} 秒数（>=0）；无法解析时为 null
   */
  function parseRetryAfter(value, nowMs) {
    if (value === null || value === undefined) return null;
    const raw = String(value).trim();
    if (raw === '') return null;

    if (/^\d+$/.test(raw)) return Number(raw);

    const when = Date.parse(raw);
    if (Number.isNaN(when)) return null;
    const now = typeof nowMs === 'number' ? nowMs : Date.now();
    return Math.max(0, Math.ceil((when - now) / 1000));
  }

  /**
   * 把人秒数格式化成中文可读时长（向上取整：宁可让使用者晚一点重试）。
   * @param {number} seconds
   * @returns {string}
   */
  function formatDuration(seconds) {
    const total = Math.max(0, Math.ceil(Number(seconds) || 0));
    if (total < 60) return total + ' 秒';
    const minutes = Math.ceil(total / 60);
    if (minutes < 60) return minutes + ' 分钟';
    return Math.ceil(minutes / 60) + ' 小时';
  }

  /**
   * 把一次登录请求的结果映射成页面要呈现的状态。
   *
   * @param {number} status HTTP 状态码；网络失败请传 0
   * @param {any} payload 解析后的响应体（解析失败传 null）
   * @param {{retryAfter?: string|null, nowMs?: number}} [opts]
   * @returns {{outcome: string, message: string, retryAfterSec: number|null, showError: boolean}}
   */
  function classifyLoginResult(status, payload, opts) {
    const options = opts || {};
    const code = normalizeErrorCode(payload);
    const message = serverMessage(payload);
    const retryAfterSec = parseRetryAfter(
      options.retryAfter === undefined ? null : options.retryAfter,
      options.nowMs,
    );

    // 网络层失败：不发下一个请求，直接进入离线提示
    if (status === 0) {
      return {
        outcome: 'offline',
        message: message || '无法连接认证服务，登录请求未发送成功。',
        retryAfterSec: null,
        showError: true,
      };
    }

    if (status === 204 || (status >= 200 && status < 300)) {
      return { outcome: 'success', message: '', retryAfterSec: null, showError: false };
    }

    if (status === 400) {
      return {
        outcome: 'invalid-input',
        message: message || '提交的登录信息格式不正确，请检查后重试。',
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    if (status === 401) {
      return {
        outcome: 'bad-credentials',
        message: message || '用户名或口令不正确。',
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    if (status === 403) {
      // 后端的写请求拒绝码是 csrf_rejected；csrf_invalid / invalid_csrf 是
      // 早期约定，一并接受，避免同一个失败在两套命名之间漏判。
      if (code === 'csrf_rejected' || code === 'csrf_invalid' || code === 'csrf_failed'
        || code === 'invalid_csrf') {
        return {
          outcome: 'csrf-failed',
          message: message || '页面校验信息已失效，已为你重新获取，请再试一次。',
          retryAfterSec: retryAfterSec,
          showError: true,
        };
      }
      if (code === 'account_disabled' || code === 'disabled' || code === 'suspended') {
        return {
          outcome: 'disabled',
          message: message || '该账号已被停用，请联系管理员。',
          retryAfterSec: retryAfterSec,
          showError: true,
        };
      }
      if (code === 'account_locked' || code === 'locked' || code === 'lockout') {
        return {
          outcome: 'locked',
          message: message || '账号因多次登录失败已被暂时锁定。',
          retryAfterSec: retryAfterSec,
          showError: true,
        };
      }
      return {
        outcome: 'forbidden',
        message: message || '当前请求被拒绝，请刷新页面后重试。',
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    if (status === 423) {
      return {
        outcome: 'locked',
        message: message || '账号因多次登录失败已被暂时锁定。',
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    if (status === 429) {
      // 后端的失败锁定与限速是同一个 429：把 Retry-After 如实讲给使用者，
      // 并按「向上取整」的冷却时长禁用提交，避免他立刻撞第二次。
      const base = message || '尝试过于频繁，请稍后再试。';
      return {
        outcome: 'rate-limited',
        message: retryAfterSec !== null
          ? base + '（约 ' + formatDuration(retryAfterSec) + '后可重试）'
          : base,
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    if (status === 503 && (code === 'not_configured' || code === 'unavailable')) {
      return {
        outcome: 'not-configured',
        message: message || '认证服务尚未完成配置，暂时无法登录。',
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    if (status >= 500) {
      return {
        outcome: 'server-error',
        message: message || '认证服务暂时不可用，请稍后重试。',
        retryAfterSec: retryAfterSec,
        showError: true,
      };
    }

    return {
      outcome: 'unknown',
      message: message || ('登录失败（HTTP ' + status + '）。'),
      retryAfterSec: retryAfterSec,
      showError: true,
    };
  }

  /* ========================================================================
     6. 会话（GET /auth/me）→ 页面状态
     ======================================================================== */

  /**
   * 判断 `/auth/me` 的响应体是否代表一个有效会话。
   *
   * 只看**明确的登录信号**：`data.authenticated === true` 或信封里真的带了
   * `data.user`。身份展示字段（昵称、邮箱）不参与判定。
   *
   * @param {any} payload
   * @returns {boolean}
   */
  function isAuthenticated(payload) {
    const data = envelopeData(payload);
    if (!data) return false;
    if (data.authenticated === false) return false;
    if (data.authenticated === true) return true;
    // 兼容「只回身份、不回 authenticated」的形态，但**必须有明确的用户对象**
    if (data.user && typeof data.user === 'object') return true;
    if (typeof data.username === 'string' && data.username.trim() !== '') return true;
    return false;
  }

  /**
   * 取出已授权的项目列表。**只认显式字段**，不从角色或身份推断授权。
   *
   * 兼容三种项目条目形状：
   *  - 字符串（`"atlas"`）；
   *  - 后端 `projects_for()` 的 `{project_id, role}`；
   *  - 早期原型用的 `{key}` / `{id}` / `{name}`。
   *
   * 安全性：飞书侧返回的邮箱、昵称、`role`、`is_admin` 等字段**不参与**任何
   * 授权判定——它们不在这里被读取。
   *
   * @param {any} payload 响应体，或已解包的 data 对象
   * @returns {string[]}
   */
  function authorizedProjects(payload) {
    const source = envelopeData(payload) || payload;
    if (!source || typeof source !== 'object') return [];
    const raw = source.projects !== undefined
      ? source.projects
      : source.authorized_projects;
    if (!Array.isArray(raw)) return [];
    return raw
      .map(function (item) {
        if (typeof item === 'string') return item;
        if (!item || typeof item !== 'object') return undefined;
        const candidates = [item.project_id, item.key, item.id, item.name];
        for (const candidate of candidates) {
          if (typeof candidate === 'string' && candidate.trim() !== '') return candidate;
        }
        return undefined;
      })
      .filter(function (item) { return typeof item === 'string' && item.trim() !== ''; })
      .map(function (item) { return item.trim(); });
  }

  /**
   * 判断是否平台管理员。**只信服务端**给出的 `data.user.is_platform_admin`。
   *
   * 特别注意：`role` 字符串**不**作为判据。后端只在 `is_platform_admin` 布尔
   * 上表达这件事；把 `role === 'platform_admin'` 也算数，就会让任何能影响
   * display name / role 文本的路径凭空拿到管理员入口。
   *
   * @param {any} payload
   * @returns {boolean}
   */
  function isPlatformAdmin(payload) {
    const data = envelopeData(payload);
    if (!data || !data.user || typeof data.user !== 'object') return false;
    return data.user.is_platform_admin === true;
  }

  /**
   * 根据 `/auth/me` 的响应决定页面视图。
   *
   * 返回里的 `authorized` 是**是否允许进入受保护页面**的唯一判据，规则只有两条：
   *
   *  - 服务端说这个人是平台管理员（`is_platform_admin === true`）；
   *  - 服务端给了他至少一个项目授权。
   *
   * 因此「本地 admin 一个项目 grant 都没有」不会被误判成无权限；而飞书成员
   * 在拿到授权之前只会看到**待审批**视图。**这条检查绝不取代服务端**——它只
   * 决定页面往哪里走，真正的鉴权在服务端每条受保护路由上。
   *
   * @param {number} status HTTP 状态码（网络失败传 0）
   * @param {any} payload
   * @returns {{view: string, authorized: boolean, username: string, projects: string[],
   *            role: string, isPlatformAdmin: boolean, message: string}}
   */
  function classifySession(status, payload) {
    const deny = (view, message) => ({
      view: view,
      authorized: false,
      username: '',
      projects: [],
      role: '',
      isPlatformAdmin: false,
      message: message || '',
    });

    if (status === 0) return deny(VIEW.OFFLINE);
    if (status >= 500) return deny(VIEW.OFFLINE);

    if (status === 401 || status === 403) {
      const code = normalizeErrorCode(payload);
      if (code === 'account_disabled' || code === 'disabled') {
        return deny(VIEW.LINUX, serverMessage(payload) || '该账号已被停用，请联系管理员。');
      }
      const expired = code === 'session_expired' || code === 'expired' || code === 'token_expired';
      return deny(VIEW.LINUX, expired ? '登录状态已过期，请重新登录。' : '');
    }

    if (!isAuthenticated(payload)) return deny(VIEW.LINUX);

    const data = envelopeData(payload) || {};
    const user = (data.user && typeof data.user === 'object') ? data.user : {};
    const username = String(
      user.display_name !== undefined ? user.display_name
        : (data.display_name !== undefined ? data.display_name : (data.username || '')),
    ).trim();
    const role = String(user.role !== undefined ? user.role : (data.role || '')).trim();
    const projects = authorizedProjects(data);
    const admin = isPlatformAdmin(payload);

    if (admin || projects.length > 0) {
      return {
        view: VIEW.LINUX,
        authorized: true,
        username: username,
        projects: projects,
        role: role,
        isPlatformAdmin: admin,
        message: '',
      };
    }

    return {
      view: VIEW.NO_PROJECT,
      authorized: false,
      username: username,
      projects: projects,
      role: role,
      isPlatformAdmin: false,
      message: '身份已确认，但账号尚未获得任何项目授权，请联系管理员审批。',
    };
  }

  /* ========================================================================
     7. 登录方式可用性（只读自 GET /auth/csrf 的 data.providers）
     ======================================================================== */

  /**
   * 读 `/auth/csrf` 里声明的可用登录方式。
   *
   * 为什么不去「探测」`GET /auth/feishu/start`：那条路由**有副作用**——被访问就
   * 会创建一次 OAuth login state 并下发浏览器绑定 Cookie。把它当 health 探针，
   * 等于每次打开登录页都在服务端留下一个无人认领的 state，还会提前消耗限速额度。
   * 可用性因此只来自 `/auth/csrf` 的**只读** `data.providers`。
   *
   * `known` 为 false 表示服务端没有给出可判定的布尔值；此时调用方**不得**
   * 假定可用或不可用，更不能假装「已配置」。
   *
   * @param {any} payload `/auth/csrf` 的响应体
   * @returns {{feishu: boolean|null, localAdmin: boolean|null, known: boolean}}
   */
  function readProviderState(payload) {
    const data = envelopeData(payload);
    const providers = data && data.providers && typeof data.providers === 'object'
      ? data.providers
      : null;
    if (!providers) {
      return { feishu: null, localAdmin: null, known: false };
    }
    const feishu = typeof providers.feishu === 'boolean' ? providers.feishu : null;
    const localAdmin = typeof providers.local_admin === 'boolean' ? providers.local_admin : null;
    return {
      feishu: feishu,
      localAdmin: localAdmin,
      known: feishu !== null || localAdmin !== null,
    };
  }

  /* ========================================================================
     7b. Linux PAM 登录就绪门槛（Lane B）
     ======================================================================== */

  /**
   * 契约要求的 Linux 账号范围标识。
   *
   * 服务端只有在「这一批凭据是 Dell Linux 账号」时才允许置为这个值。UI 侧把它当作
   * 断言而不是提示：值不是它，表单就不开。
   */
  const LINUX_PAM_UID_SCOPE = 'dell-linux-account';

  /** 门槛结论的原因码。文案与判定分离，便于测试与日志。 */
  const LINUX_PAM_REASON = Object.freeze({
    READY: 'ready',
    STATIC_PREVIEW: 'static-preview',
    TIMEOUT: 'timeout',
    HTML_RESPONSE: 'html-response',
    HTTP_401: 'http-401',
    HTTP_403: 'http-403',
    HTTP_ERROR: 'http-error',
    MALFORMED_ENVELOPE: 'malformed-envelope',
    PROVIDER_ABSENT: 'provider-absent',
    PROVIDER_DISABLED: 'provider-disabled',
    UNSAFE_PRINCIPAL_SCOPE: 'unsafe-principal-scope',
    CSRF_MISSING: 'csrf-missing',
  });

  /**
   * 未开通时那段**共同**的开场白。
   *
   * 真实部署里这个页面就挡在 alice:8443 的 BasicAuth 之后，而 PAM 服务端登录
   * 尚未投产——所以「被 BasicAuth 挡着 + PAM 没开通」是**默认**处境，不是某些
   * 失败分支的旁枝。每一种停用原因都以它开头，再补一句这次具体卡在哪，
   * 使用者才不会把「服务端返回了个错误」误读成「我该输 Dell 口令试试」。
   */
  const LINUX_PAM_PREVIEW_LEAD =
    '当前仅是 BasicAuth 保护的预览；Linux PAM 服务端登录未开通。';

  /** 原因码 ⇒ 面向使用者的直白说明。**不含任何响应体内容**。 */
  const LINUX_PAM_MESSAGE = Object.freeze({
    [LINUX_PAM_REASON.STATIC_PREVIEW]:
      '静态预览：页面通过 file:// 打开，未连接认证服务，不会发送任何登录请求。',
    [LINUX_PAM_REASON.TIMEOUT]:
      LINUX_PAM_PREVIEW_LEAD + '认证服务在时限内没有回应，表单保持停用。',
    [LINUX_PAM_REASON.HTML_RESPONSE]:
      LINUX_PAM_PREVIEW_LEAD + '认证接口返回的是网页而不是数据，表单保持停用。',
    [LINUX_PAM_REASON.HTTP_401]:
      LINUX_PAM_PREVIEW_LEAD + '认证服务要求先有身份（例如 BasicAuth 门页），表单保持停用。',
    [LINUX_PAM_REASON.HTTP_403]:
      LINUX_PAM_PREVIEW_LEAD + '认证服务拒绝了本次读取，表单保持停用。',
    [LINUX_PAM_REASON.HTTP_ERROR]:
      LINUX_PAM_PREVIEW_LEAD + '认证服务返回错误，表单保持停用。',
    [LINUX_PAM_REASON.MALFORMED_ENVELOPE]:
      LINUX_PAM_PREVIEW_LEAD + '认证服务返回的数据不符合约定，表单保持停用。',
    [LINUX_PAM_REASON.PROVIDER_ABSENT]:
      LINUX_PAM_PREVIEW_LEAD + '表单已停用，不会发送任何登录请求。',
    [LINUX_PAM_REASON.PROVIDER_DISABLED]:
      LINUX_PAM_PREVIEW_LEAD + '表单已停用，不会发送任何登录请求。',
    [LINUX_PAM_REASON.UNSAFE_PRINCIPAL_SCOPE]:
      LINUX_PAM_PREVIEW_LEAD
      + '服务端未确认账号范围（仅 Dell Linux 账号，禁 root，禁服务账号），表单保持停用。',
    [LINUX_PAM_REASON.CSRF_MISSING]:
      LINUX_PAM_PREVIEW_LEAD + '未取得页面校验信息，表单保持停用。',
  });

  /**
   * 只读解析 `/auth/csrf` 里声明的 Linux PAM provider。
   *
   * `known` 为 false 表示服务端没有给出可判定的布尔值——调用方不得猜「可用」。
   * 非对象（`true` / 字符串 / 数组）一律按**未声明**处理：一个裸布尔不足以承载
   * 「UID 范围、禁 root、禁服务账号」这三条，不能让它等效于开通。
   *
   * @param {any} payload `/auth/csrf` 的响应体
   * @returns {{present: boolean, enabled: boolean|null, root: boolean|null,
   *            serviceAccount: boolean|null, uidScope: string, known: boolean}}
   */
  function readLinuxPamProvider(payload) {
    const data = envelopeData(payload);
    const providers = data && data.providers && typeof data.providers === 'object'
      ? data.providers
      : null;
    const raw = providers ? providers.linux_pam : null;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      return {
        present: false,
        enabled: null,
        root: null,
        serviceAccount: null,
        uidScope: '',
        known: false,
      };
    }
    const enabled = typeof raw.enabled === 'boolean' ? raw.enabled : null;
    return {
      present: true,
      enabled: enabled,
      root: typeof raw.root === 'boolean' ? raw.root : null,
      serviceAccount: typeof raw.service_account === 'boolean' ? raw.service_account : null,
      uidScope: typeof raw.uid_scope === 'string' ? raw.uid_scope.trim() : '',
      known: enabled !== null,
    };
  }

  /**
   * 是否允许把 Linux 账号登录表单交给使用者。
   *
   * 这是本页唯一的开闸点，默认**关**：只有下面每一条都成立才返回 `ready: true`——
   *   1. 不是 `file://` 静态预览，也没有超时；
   *   2. HTTP 2xx，且 `Content-Type` 不是 HTML（假 200 登录页必须被拒）；
   *   3. 响应体是后端信封（`ok === true` + `data` 对象），并**声明**了
   *      `data.authenticated` 布尔契约——这是「服务端契约存在」的可检验形式；
   *   4. `data.providers.linux_pam` 是对象，且 `enabled === true`（严格布尔）；
   *   5. `uid_scope === 'dell-linux-account'`、`root === false`、
   *      `service_account === false`——三条缺一不可，缺字段不是「默认安全」；
   *   6. 存在可用的 CSRF 值（写请求的前置）。
   *
   * 注意第 4 条：旧 `providers.feishu` / `providers.local_admin` 为 true **不会**
   * 让本门槛通过。它们已被退役，且与 Linux 身份无关。
   *
   * @param {{staticPreview?: boolean, timedOut?: boolean, status?: number,
   *          contentType?: string, payload?: any}} [input]
   * @returns {{ready: boolean, reason: string, message: string}}
   */
  function linuxLoginReadiness(input) {
    const opts = input || {};
    const hold = (reason) => ({
      ready: false,
      reason: reason,
      message: LINUX_PAM_MESSAGE[reason] || '',
    });

    if (opts.staticPreview) return hold(LINUX_PAM_REASON.STATIC_PREVIEW);
    if (opts.timedOut) return hold(LINUX_PAM_REASON.TIMEOUT);

    const status = Number(opts.status);
    if (status === 401) return hold(LINUX_PAM_REASON.HTTP_401);
    if (status === 403) return hold(LINUX_PAM_REASON.HTTP_403);
    if (!(status >= 200 && status < 300)) return hold(LINUX_PAM_REASON.HTTP_ERROR);

    const contentType = String(opts.contentType == null ? '' : opts.contentType).toLowerCase();
    if (contentType.includes('html')) return hold(LINUX_PAM_REASON.HTML_RESPONSE);

    const payload = opts.payload;
    const data = envelopeData(payload);
    if (!payload || typeof payload !== 'object' || payload.ok !== true
      || !data || typeof data !== 'object' || typeof data.authenticated !== 'boolean') {
      return hold(LINUX_PAM_REASON.MALFORMED_ENVELOPE);
    }

    const provider = readLinuxPamProvider(payload);
    if (!provider.present) return hold(LINUX_PAM_REASON.PROVIDER_ABSENT);
    if (provider.enabled !== true) return hold(LINUX_PAM_REASON.PROVIDER_DISABLED);
    if (provider.root !== false || provider.serviceAccount !== false
      || provider.uidScope !== LINUX_PAM_UID_SCOPE) {
      return hold(LINUX_PAM_REASON.UNSAFE_PRINCIPAL_SCOPE);
    }
    if (!readCsrfValue(payload)) return hold(LINUX_PAM_REASON.CSRF_MISSING);

    return { ready: true, reason: LINUX_PAM_REASON.READY, message: '' };
  }

  /* ========================================================================
     8. CSRF
     ======================================================================== */

  /**
   * 判断一次 `/auth/csrf` 响应是否给出了可用的 CSRF 值。
   *
   * 只从**响应体**里取（后端为 `data.csrf_token`），本函数连 `document` 都不
   * 接触，因此结构上不可能把 token 读进任何浏览器持久存储。
   *
   * @param {any} payload 响应体
   * @returns {string} CSRF 值；不可用时为空串
   */
  function readCsrfValue(payload) {
    const data = envelopeData(payload);
    if (!data) return '';
    let raw = data.csrf_token;
    if (raw === undefined) raw = data.csrf;
    if (raw === undefined) raw = data.token;
    if (typeof raw !== 'string') return '';
    const value = raw.trim();
    if (value === '' || value.length > 512) return '';
    return value;
  }


  /**
   * 用于日志/诊断的安全脱敏——任何凭据类输入一律不落原文。
   * @param {unknown} value
   * @returns {string}
   */
  function redactCredential(value) {
    if (value === null || value === undefined || value === '') return '';
    return '[已脱敏 ' + String(value).length + ' 字符]';
  }

  /* ========================================================================
     导出
     ======================================================================== */

  return Object.freeze({
    VIEW: VIEW,
    DEFAULT_LANDING: DEFAULT_LANDING,
    isStaticPreview: isStaticPreview,
    sanitizeNextPath: sanitizeNextPath,
    nextFromSearch: nextFromSearch,
    isRetiredEntryLink: isRetiredEntryLink,
    isLoggedOutLanding: isLoggedOutLanding,
    validateLoginForm: validateLoginForm,
    normalizeErrorCode: normalizeErrorCode,
    parseRetryAfter: parseRetryAfter,
    formatDuration: formatDuration,
    envelopeData: envelopeData,
    serverMessage: serverMessage,
    classifyLoginResult: classifyLoginResult,
    isAuthenticated: isAuthenticated,
    authorizedProjects: authorizedProjects,
    isPlatformAdmin: isPlatformAdmin,
    classifySession: classifySession,
    readProviderState: readProviderState,
    LINUX_PAM_UID_SCOPE: LINUX_PAM_UID_SCOPE,
    LINUX_PAM_REASON: LINUX_PAM_REASON,
    LINUX_PAM_MESSAGE: LINUX_PAM_MESSAGE,
    readLinuxPamProvider: readLinuxPamProvider,
    linuxLoginReadiness: linuxLoginReadiness,
    readCsrfValue: readCsrfValue,
    redactCredential: redactCredential,
  });
}));
