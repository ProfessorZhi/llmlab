/**
 * web/auth/auth.js
 *
 * 登录页的行为层：视图切换、主题、同源认证请求。
 *
 * 硬性约束（与 docs/auth-ui.md、Issue #5/#7 一致）：
 *  - 只向**同源** `/auth/**` 发起请求，任何其它目标都被 {@link AUTH_PATHS} 白名单挡住；
 *  - 会话凭据只存在于服务端下发的 HttpOnly Cookie 中，本文件不读取、不写入、
 *    不放入 localStorage / sessionStorage / JS 全局变量；
 *  - CSRF 值仅保存在模块作用域的内存变量里，用完即弃，刷新即失效；
 *  - **每次写请求都重新取一次 CSRF 值**：后端的匿名 CSRF 值是单次性的
 *    （`verify_write_csrf` → `consume_login_state`），复用必被 `_preflight` 拒绝；
 *  - 写请求一律带 `X-CSRF-Token` 请求头，请求体里不放 CSRF（与后端协议一致）；
 *  - 登录入口只有**一个**：Dell Linux 账号。飞书身份与「本地管理员」双路线均已退役，
 *    本文件不导航到 `/auth/feishu/start`、也不向 `/auth/local/login` 提交（两者都不在
 *    {@link AUTH_PATHS} 里）；
 *  - Linux 表单只有在**服务端明确声明** `providers.linux_pam` 可用时才开闸，
 *    判定在 `auth-core.js` 的 `linuxLoginReadiness`；其余一切状态 fail-closed；
 *  - `file://` 静态预览下**一律不发送登录请求**。
 *
 * 本文件是**经典脚本**（非 ES module），原因见 auth-core.js 顶部的说明：
 * `file://` 下浏览器会以跨源为由拦截 module 脚本，静态预览将完全失效。
 * 纯逻辑全部来自 `auth-core.js`，本文件只负责视图与请求编排。
 */

(function () {
  'use strict';

  const core = window.LLMAuthCore;
  if (!core) return; // auth-core.js 未加载成功时不做任何事，页面停留在静态默认视图

  const {
    VIEW,
    DEFAULT_LANDING,
    isStaticPreview,
    nextFromSearch,
    isRetiredEntryLink,
    isLoggedOutLanding,
    validateLoginForm,
    classifyLoginResult,
    classifySession,
    linuxLoginReadiness,
    readCsrfValue,
    serverMessage,
    formatDuration,
  } = core;

/**
 * 允许请求的同源端点白名单——不是这些路径一律拒绝。
 *
 * 这里**没有** `/auth/feishu/start`，也没有 `/auth/local/login`：前者是已退役的
 * 飞书身份入口，后者是误导性的本地管理员路线。白名单是白名单，退役的端点不在里面，
 * 因此本页在结构上就不可能向它们发请求。
 */
const AUTH_PATHS = Object.freeze({
  csrf: '/auth/csrf',
  linuxLogin: '/auth/linux/login',
  me: '/auth/me',
  logout: '/auth/logout',
});

/** 读取 `/auth/csrf` 的时限。超时按 fail-closed 处理，不是「当作没读到」。 */
const GATE_TIMEOUT_MS = 5000;

/** 主题偏好的存储键。**只存主题**，与任何凭据无关。 */
const THEME_KEY = 'llm-lab.auth.theme';

/* ==========================================================================
   运行时状态（全部只在内存中）
   ========================================================================== */

/** 当前 CSRF 值。仅内存，不落任何存储。 */
let csrfValue = '';
/** 是否已有请求在途，防止重复提交。 */
let inFlight = false;
/** 冷却定时器句柄。 */
let cooldownTimer = null;
/** 是否已确认处于静态预览（file://）。 */
let staticPreview = false;
/** 登录成功后要跳转的站内路径。 */
let landingPath = DEFAULT_LANDING;
/**
 * 最近一次门槛结论。默认是**关**：在 `boot` 真正读到 `/auth/csrf` 之前，
 * 页面上任何位置都不得把表单当作可用（默认值即 fail-closed）。
 */
let lastGate = Object.freeze({ ready: false, reason: 'not-checked', message: '' });

/* ==========================================================================
   DOM 助手
   ========================================================================== */

const $ = (id) => document.getElementById(id);

const el = {
  root: document.documentElement,
  panels: () => Array.from(document.querySelectorAll('[data-view]')),
  themeToggle: $('theme-toggle'),
  statusbar: $('statusbar'),
  statusText: $('status-text'),
  linuxForm: $('linux-form'),
  linuxFields: $('linux-fields'),
  linuxSubmit: $('linux-submit'),
  linuxGate: $('linux-gate'),
  gateRetry: $('gate-retry'),
  username: $('username'),
  password: $('password'),
  pwToggle: $('pw-toggle'),
  npLogout: $('np-logout'),
  npUser: $('np-user'),
  loAgain: $('lo-again'),
  offlineRetry: $('offline-retry'),
  offlineDetail: $('offline-detail'),
};

/* ==========================================================================
   视图与状态
   ========================================================================== */

/**
 * 切换可见面板。
 * @param {string} view {@link VIEW} 中的值
 * @param {{focus?: boolean}} [opts] focus=true 时把焦点移到面板标题（用户主动切换时用）
 */
function setView(view, opts = {}) {
  let active = null;
  for (const panel of el.panels()) {
    const mine = panel.dataset.view === view;
    panel.hidden = !mine;
    if (mine) active = panel;
  }
  el.root.dataset.authState = view;

  if (opts.focus && active) {
    const heading = active.querySelector('.title');
    if (heading) {
      if (!heading.hasAttribute('tabindex')) heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: false });
    }
  }
}

/** 当前可见的视图名。 */
function currentView() {
  const found = el.panels().find((p) => !p.hidden);
  return found ? found.dataset.view : VIEW.BOOT;
}

/**
 * 更新状态条。
 * @param {'info'|'warn'|'error'} kind
 * @param {string} message 空串表示隐藏
 */
function setStatus(kind, message) {
  if (!el.statusbar || !el.statusText) return;
  if (!message) {
    el.statusbar.hidden = true;
    el.statusText.textContent = '';
    return;
  }
  el.statusbar.hidden = false;
  el.statusbar.dataset.kind = kind;
  el.statusText.textContent = message;
}

/**
 * 仅在状态条空闲时写入，避免后来者覆盖更重要的信息（如「会话已过期」）。
 * @param {'info'|'warn'|'error'} kind
 * @param {string} message
 */
function setStatusIfIdle(kind, message) {
  if (el.statusbar && !el.statusbar.hidden) return;
  setStatus(kind, message);
}

/** 表单忙碌态。 */
function setBusy(busy) {
  inFlight = busy;
  if (el.linuxForm) el.linuxForm.dataset.busy = busy ? 'true' : 'false';
  if (el.linuxSubmit) {
    el.linuxSubmit.disabled = busy;
    el.linuxSubmit.setAttribute('aria-busy', busy ? 'true' : 'false');
    const label = el.linuxSubmit.querySelector('.btnlabel');
    if (label) label.textContent = busy ? '正在登录…' : '登录';
  }
  if (el.username) el.username.disabled = busy;
  if (el.password) el.password.disabled = busy;
}

/** 标记字段错误并聚焦。 */
function markFieldError(field, message) {
  setStatus('error', message);
  const input = field === 'password' ? el.password : el.username;
  if (input) {
    input.setAttribute('aria-invalid', 'true');
    input.focus();
  }
}

/** 清除字段错误标记。 */
function clearFieldErrors() {
  for (const input of [el.username, el.password]) {
    if (input) input.removeAttribute('aria-invalid');
  }
}

/* ==========================================================================
   同源请求
   ========================================================================== */

/**
 * 断言目标是白名单内的同源路径。
 * @param {string} path
 * @throws {Error} 目标不在白名单内
 */
function assertAllowedPath(path) {
  const allowed = Object.values(AUTH_PATHS);
  if (!allowed.includes(path)) {
    throw new Error(`拒绝向非白名单端点发起请求：${path}`);
  }
}

/**
 * 发起一次同源 JSON 请求。网络失败统一映射为 `status: 0`，
 * 由 {@link classifyLoginResult} / {@link classifySession} 转成离线状态。
 *
 * `contentType` 与 `timedOut` 是给**就绪门槛**用的：一个 200 的 HTML 登录页和一个
 * 真正超时的接口，都必须能和「契约齐备的 JSON」区分开，否则 fail-closed 无从谈起。
 *
 * @param {string} path 必须来自 {@link AUTH_PATHS}
 * @param {RequestInit & {timeoutMs?: number}} [init]
 * @returns {Promise<{status: number, payload: any, retryAfter: string|null,
 *                    contentType: string, timedOut: boolean}>}
 */
async function request(path, init = {}) {
  assertAllowedPath(path);
  const { timeoutMs, ...rest } = init;
  let timer = null;
  let controller = null;
  if (typeof timeoutMs === 'number' && timeoutMs > 0 && typeof AbortController === 'function') {
    controller = new AbortController();
    timer = setTimeout(() => controller.abort(), timeoutMs);
  }
  try {
    const response = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...rest,
      ...(controller ? { signal: controller.signal } : {}),
    });
    let payload = null;
    const type = response.headers.get('content-type') || '';
    if (type.includes('json')) {
      payload = await response.json().catch(() => null);
    }
    return {
      status: response.status,
      payload,
      retryAfter: response.headers.get('retry-after'),
      contentType: type,
      timedOut: false,
    };
  } catch (error) {
    const timedOut = !!(error && error.name === 'AbortError');
    return { status: 0, payload: null, retryAfter: null, contentType: '', timedOut };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * 取一个**新的** CSRF 值，只保留在内存。
 *
 * 后端的匿名 CSRF 值是单次性的，所以这不能理解成「缓存」：每个写请求前都要调一次。
 * 顺带把 `/auth/csrf` 里声明的登录方式可用性也应用上——那是同一个只读响应，
 * 不需要也不允许再打第二个端点去「探测」。
 *
 * @returns {Promise<string>} 可用的 CSRF 值；拿不到时为空串
 */
async function fetchCsrf() {
  const { status, payload } = await request(AUTH_PATHS.csrf, { method: 'GET' });
  if (status < 200 || status >= 300) return '';
  const value = readCsrfValue(payload);
  csrfValue = value;
  return value;
}

/* ==========================================================================
   登录方式可用性（只读自 GET /auth/csrf 的 data.providers）
   ========================================================================== */

/* ==========================================================================
   Linux 账号登录门槛（本页唯一的开关）
   ========================================================================== */

/**
 * 把门槛结论画到页面上。
 *
 * 不通过时：整组字段、口令可见性按钮与提交按钮一律 `disabled`，说明文案直接写在
 * 表单上方（`#linux-gate`），**页面上不存在任何能发起登录请求的路径**。
 * 通过时：字段与按钮启用，说明换成一句「服务端已声明可用」。
 *
 * 这里只改 DOM，不发任何请求——门槛的**唯一**来源是 {@link loadLinuxGate}。
 *
 * @param {{ready: boolean, reason: string, message: string}} verdict
 */
function applyGate(verdict) {
  const ready = !!verdict.ready;
  lastGate = { ready: ready, reason: verdict.reason, message: verdict.message || '' };
  if (el.linuxFields) el.linuxFields.disabled = !ready;
  if (el.username) el.username.disabled = !ready;
  if (el.password) el.password.disabled = !ready;
  if (el.pwToggle) el.pwToggle.disabled = !ready;
  if (el.linuxSubmit) {
    el.linuxSubmit.disabled = !ready;
    el.linuxSubmit.dataset.gate = ready ? 'ready' : 'hold';
  }
  if (el.linuxGate) {
    el.linuxGate.dataset.gate = ready ? 'ready' : 'hold';
    el.linuxGate.textContent = ready
      ? '服务端已声明 Dell Linux 账号登录可用。请输入你在 Dell 主机上的账号与口令。'
      : (verdict.message || 'Linux 账号登录尚未开通。');
  }
  if (ready) clearFieldErrors();
}

/**
 * 读取**唯一**的开闸依据：同源只读的 `GET /auth/csrf`。
 *
 * 为什么只读这一个端点：
 *  - 飞书的 `/auth/feishu/start` 一被访问就会创建一次 OAuth login state，把它当
 *    健康探针等于每次开页都在服务端留下无人认领的 state；该入口已退役，本页更不会
 *    碰它——它也不在 {@link AUTH_PATHS} 里，`assertAllowedPath` 结构上就拦住。
 *  - 本地管理员那条路线已退役，不再有第二个入口需要判断。
 *
 * 任何失败——超时、200 的 HTML、401/403、schema 不认识、CSRF 缺失——都落到 `hold`，
 * 表单保持停用。**不存在**「读不到就放行」的分支。
 *
 * @returns {Promise<{ready: boolean, reason: string, message: string}>}
 */
async function loadLinuxGate() {
  if (staticPreview) {
    const verdict = linuxLoginReadiness({ staticPreview: true });
    applyGate(verdict);
    return verdict;
  }

  const response = await request(AUTH_PATHS.csrf, {
    method: 'GET',
    timeoutMs: GATE_TIMEOUT_MS,
  });

  const verdict = linuxLoginReadiness({
    staticPreview: false,
    timedOut: response.timedOut,
    status: response.status,
    contentType: response.contentType,
    payload: response.payload,
  });

  // 开闸时才读 CSRF 值，关闸时清掉，不留悬空状态。提交时会重新取一次
  // （匿名值是单次性的），这里只是让内存状态与页面状态一致。
  csrfValue = verdict.ready ? readCsrfValue(response.payload) : '';

  applyGate(verdict);
  return verdict;
}

/* ==========================================================================
   静态预览（file://）
   ========================================================================== */

/**
 * 进入静态预览：只显示页面与主题，不发任何请求。
 * @param {string} reason 展示给使用者的说明
 */
function enterStaticPreview(reason) {
  staticPreview = true;
  csrfValue = '';
  setStatus('warn', reason);
  setView(VIEW.LINUX);
  // 静态预览永远关闸：表单停用、说明如实写出，不从任何地方取「可用」。
  applyGate(linuxLoginReadiness({ staticPreview: true }));
}

/* ==========================================================================
   Linux 账号登录
   ========================================================================== */

/** 启动 429 / 锁定后的冷却倒计时。 @param {number} seconds */
function startCooldown(seconds) {
  if (cooldownTimer) { clearInterval(cooldownTimer); cooldownTimer = null; }
  let remaining = Math.max(1, Math.ceil(Number(seconds) || 0));
  const tick = () => {
    if (!el.linuxSubmit) return;
    el.linuxSubmit.disabled = true;
    const label = el.linuxSubmit.querySelector('.btnlabel');
    if (label) label.textContent = `请 ${formatDuration(remaining)}后重试`;
    remaining -= 1;
    if (remaining < 0) {
      clearInterval(cooldownTimer);
      cooldownTimer = null;
      const l = el.linuxSubmit.querySelector('.btnlabel');
      if (l) l.textContent = '登录';
      // 冷却结束不是「无条件重新可用」：只有门槛仍然开着才恢复。
      applyGate(lastGate);
    }
  };
  tick();
  cooldownTimer = setInterval(tick, 1000);
}

/** 进入「禁用 / 锁定」终态：表单不可再用。 */
function lockForm(message) {
  setStatus('error', message);
  setBusy(false);
  if (el.linuxForm) el.linuxForm.dataset.locked = 'true';
  for (const input of [el.username, el.password]) {
    if (input) input.disabled = true;
  }
  if (el.linuxSubmit) el.linuxSubmit.disabled = true;
}

/** 解除终态，让使用者可以改账号重试。 */
function unlockForm() {
  if (el.linuxForm) delete el.linuxForm.dataset.locked;
  // 解除终态**不等于**开闸：仍然由最近一次门槛结论决定字段与按钮是否可用。
  applyGate(lastGate);
}

/**
 * 提交本地管理员登录。
 * @param {SubmitEvent} event
 */
async function onLinuxSubmit(event) {
  event.preventDefault();
  if (staticPreview || inFlight) return;
  if (el.linuxForm?.dataset.locked === 'true') return;
  // 门槛没开就绝不提交：这里的 return 在 `fetch` 之前，因此不会发出任何请求。
  if (!lastGate.ready) {
    applyGate(lastGate);
    return;
  }

  const username = el.username?.value ?? '';
  const password = el.password?.value ?? '';

  const check = validateLoginForm({ username, password });
  if (!check.ok) {
    markFieldError(check.field, check.message);
    return;
  }

  clearFieldErrors();
  setBusy(true);
  setStatus('info', '');

  try {
    // 每次都取新值：匿名 CSRF 值是一次性的，复用会让 _preflight 先拒掉这一发
    const token = await fetchCsrf();
    if (!token) {
      setBusy(false);
      setView(VIEW.OFFLINE);
      setStatus('error', '无法从认证服务获取页面校验信息，登录请求未发送。');
      return;
    }

    const result = await request(AUTH_PATHS.linuxLogin, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        // 与后端 `_preflight` 的约定：CSRF 走请求头
        'X-CSRF-Token': token,
      },
      // 契约：请求体只含 username / password，不放任何其它字段
      body: JSON.stringify({ username, password }),
    });

    const verdict = classifyLoginResult(result.status, result.payload, {
      retryAfter: result.retryAfter,
    });

    // 口令用完即从内存与 DOM 中清除
    if (el.password) el.password.value = '';
    csrfValue = '';

    switch (verdict.outcome) {
      case 'success':
        setBusy(false);
        setStatus('info', '登录成功，正在进入 Portal…');
        window.location.assign(landingPath);
        return;

      case 'csrf-failed': {
        setBusy(false);
        const refreshed = await fetchCsrf();
        setStatus('error', refreshed
          ? '页面校验信息已失效并已重新获取，请再试一次。'
          : '页面校验信息已失效，且无法重新获取，请刷新页面。');
        if (!refreshed) setView(VIEW.OFFLINE);
        return;
      }

      case 'rate-limited':
        setBusy(false);
        setStatus('error', verdict.message);
        startCooldown(verdict.retryAfterSec ?? 60);
        return;

      case 'locked':
      case 'disabled':
        lockForm(verdict.message);
        return;

      case 'offline':
        setBusy(false);
        setView(VIEW.OFFLINE);
        setStatus('error', verdict.message);
        return;

      default:
        setBusy(false);
        setStatus('error', verdict.message);
        if (verdict.outcome === 'bad-credentials') markFieldError('password', verdict.message);
        return;
    }
  } catch {
    setBusy(false);
    setStatus('error', '登录过程中发生异常，请重试。');
  }
}

/* ==========================================================================
   会话与退出
   ========================================================================== */

/** 查询当前会话并落到对应视图。 @returns {Promise<void>} */
async function refreshSession() {
  const { status, payload } = await request(AUTH_PATHS.me, { method: 'GET' });
  const session = classifySession(status, payload);

  if (session.view === VIEW.OFFLINE) {
    setView(VIEW.OFFLINE);
    return;
  }

  // 服务端给出的授权判据：平台管理员，或至少有一个项目授权。
  // 注意本地管理员在后端就是 0 个项目授权 + is_platform_admin，若只看项目数
  // 就会把唯一的平台管理员挡在门外。
  // **这条判断只决定页面往哪里走，绝不取代服务端**：受保护页面的鉴权在服务端。
  if (session.authorized) {
    setStatus('info', '检测到有效会话，正在进入 Portal…');
    window.location.assign(landingPath);
    return;
  }

  if (session.view === VIEW.NO_PROJECT) {
    if (el.npUser) el.npUser.textContent = session.username || '—';
    setView(VIEW.NO_PROJECT);
    return;
  }

  setView(VIEW.LINUX);
  if (session.message) setStatus('warn', session.message);
  // 可用性只来自那一个只读端点；读不到就保持关闸状态。
  await loadLinuxGate();
}

/**
 * 退出登录。
 *
 * 只有服务端**真的**回了 2xx 才展示「已退出」——否则会话在服务端仍然有效，
 * 页面却宣称「已安全退出」，等于对使用者撒谎并可能把人留在无提示的登录态里。
 * 被拒绝时如实说明并提示重试，且不改变当前视图。
 */
async function onLogout() {
  if (staticPreview || inFlight) return;
  inFlight = true;
  try {
    const token = await fetchCsrf();
    if (!token) {
      setStatus('error', '无法从认证服务获取页面校验信息，退出请求未发送，请重试。');
      return;
    }

    const result = await request(AUTH_PATHS.logout, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-CSRF-Token': token,
      },
      body: JSON.stringify({}),
    });

    if (result.status >= 200 && result.status < 300) {
      csrfValue = '';
      unlockForm();
      setView(VIEW.LOGGED_OUT, { focus: true });
      return;
    }

    const detail = serverMessage(result.payload);
    setStatus('error', (detail ? detail + ' ' : '')
      + '退出未成功，会话可能仍然有效，请重试。');
  } catch {
    setStatus('error', '退出过程中发生异常，会话可能仍然有效，请重试。');
  } finally {
    inFlight = false;
  }
}

/* ==========================================================================
   主题
   ========================================================================== */

/** 读取已保存的主题偏好；无偏好时保持 HTML 上的默认（明亮）。 */
function loadTheme() {
  let saved = null;
  try { saved = window.localStorage.getItem(THEME_KEY); } catch { saved = null; }
  if (saved === 'dark' || saved === 'light') {
    el.root.dataset.theme = saved;
  }
  syncThemeButton();
}

/** 同步主题按钮的语义与标签。 */
function syncThemeButton() {
  if (!el.themeToggle) return;
  const dark = el.root.dataset.theme === 'dark';
  el.themeToggle.setAttribute('aria-pressed', dark ? 'true' : 'false');
  el.themeToggle.setAttribute('aria-label', dark ? '切换到明亮主题' : '切换到深色主题');
}

/** 切换主题并持久化（只持久化主题，不涉及任何凭据）。 */
function toggleTheme() {
  const next = el.root.dataset.theme === 'dark' ? 'light' : 'dark';
  el.root.dataset.theme = next;
  try { window.localStorage.setItem(THEME_KEY, next); } catch { /* 隐私模式下忽略 */ }
  syncThemeButton();
}

/* ==========================================================================
   门槛重检
   ========================================================================== */

/**
 * 使用者主动要求重新检查登录服务（`#gate-retry`）。
 *
 * 重检只重读那一个只读端点，不做任何「乐观启用」：结果依旧是门槛说了算。
 * 抖动期间按钮先停用，避免连点堆出并发读取。
 */
async function onGateRetry() {
  if (inFlight) return;
  if (el.gateRetry) el.gateRetry.disabled = true;
  setStatus('info', '正在重新检查登录服务…');
  try {
    const verdict = await loadLinuxGate();
    if (verdict.ready) setStatus('info', '');
    else if (verdict.reason !== 'provider-absent' && verdict.reason !== 'provider-disabled') {
      setStatus('warn', verdict.message);
    } else {
      setStatus('info', '');
    }
  } finally {
    if (el.gateRetry) el.gateRetry.disabled = false;
  }
}

/* ==========================================================================
   启动
   ========================================================================== */

function bindEvents() {
  el.themeToggle?.addEventListener('click', toggleTheme);
  el.linuxForm?.addEventListener('submit', onLinuxSubmit);
  el.gateRetry?.addEventListener('click', onGateRetry);
  el.npLogout?.addEventListener('click', onLogout);
  el.loAgain?.addEventListener('click', () => {
    setStatus('info', '');
    setView(VIEW.LINUX, { focus: true });
    loadLinuxGate();
  });
  el.offlineRetry?.addEventListener('click', () => { window.location.reload(); });

  el.pwToggle?.addEventListener('click', () => {
    if (!el.password) return;
    const revealed = el.password.type === 'password';
    el.password.type = revealed ? 'text' : 'password';
    el.pwToggle.setAttribute('aria-pressed', revealed ? 'true' : 'false');
    el.pwToggle.setAttribute('aria-label', revealed ? '隐藏口令' : '显示口令');
    el.pwToggle.title = revealed ? '隐藏口令' : '显示口令';
    el.password.focus();
  });

  // 输入时清除字段级错误标记
  for (const input of [el.username, el.password]) {
    input?.addEventListener('input', () => input.removeAttribute('aria-invalid'));
  }
}

/**
 * 页面入口。只负责一件事：无论 `bootInner` 走到哪一条路径（含提前 return 与
 * 抛错），都在收尾时打上 `data-auth-ready`。
 *
 * 这个标记存在的理由是一个**真实的时序**：`refreshSession` 在切到成员视图
 * （`dataset.authState` 离开 `"boot"`）之后，还会 `await loadLinuxGate()`
 * 再去应用 Linux 表单的就绪门槛。也就是说「视图已经就绪」比「provider 可用性已经
 * 应用」更早成立。验收脚本若只等前者，就会在中间那个窗口里读到还没被设置的
 * `aria-disabled`——这正是 `browser.auth-ui.test.mjs` 里那条例外「读到 null 而
 * 期望 'true'」的来源。等这个标记，等的是「初始化真的做完了」，不必靠 sleep，
 * 也没有放宽任何断言。
 */
async function boot() {
  try {
    await bootInner();
  } finally {
    el.root.dataset.authReady = 'true';
  }
}

/** `boot` 的实际内容。 */
async function bootInner() {
  landingPath = nextFromSearch(window.location.search);
  loadTheme();
  bindEvents();

  if (isStaticPreview(window.location)) {
    enterStaticPreview(
      '静态预览模式：页面通过 file:// 打开，未连接认证服务，'
      + '因此不会发送任何登录请求，也不显示登录结果。'
    );
    return;
  }

  if (isLoggedOutLanding(window.location.search)) {
    setView(VIEW.LOGGED_OUT);
    return;
  }

  setView(VIEW.BOOT);
  try {
    await refreshSession();
  } catch {
    setView(VIEW.OFFLINE);
    setStatus('error', '初始化会话检查失败，请刷新重试。');
  }

  // 带着旧书签（`?mode=admin` / `#admin` / `?mode=member`）来的人会被**明确告知**
  // 双入口已退役，而不是被静默送到一个看起来一样的表单上，误以为自己进的是
  // 「管理员登录」。判定纯逻辑在 core 里，这里只负责说一句人话。
  if (currentView() === VIEW.LINUX && isRetiredEntryLink(window.location.search, window.location.hash)) {
    setStatusIfIdle('warn', '成员 / 管理员双入口已退役，请使用 Dell Linux 账号登录。');
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}

}());
