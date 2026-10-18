/*! web/js/features/admin-mode.js — 共享管理员模式的界面入口。
 *
 *  这一层只做三件事，而且顺序固定：
 *
 *      服务端说我是什么  →  画出来  →  把用户的动作发回服务端
 *
 *  中间没有第四件事。特别是：
 *
 *   * **提权状态只从服务端读。** 界面上没有任何"我现在是管理员"的本地开关；
 *     重画一次就是再问一次服务端。本地缓存一个 elevated 会让过期与轮换在界面
 *     上继续显示着管理员视图。
 *   * **口令只经过输入框和一次请求体。** 不进 localStorage / sessionStorage，
 *     不进 URL，不进日志，不回显；读走之后立刻清空输入框。
 *   * **未配置就画成不可用。** 部署没有共享管理口令时，按钮是 disabled 的，
 *     并且写明原因——不是藏起来，也不是画一个按不出东西的按钮。
 *   * **响应形状不对就当"未知"。** 缺字段、类型不对、elevated 为真而 configured
 *     为假，一律回落到未知态，绝不因为一个畸形响应就把界面画成已提权。
 *
 *  样式全部复用既有的 class（`.seg` / `.guard` / `.state-pill` / `.field`）：
 *  `web/index.html` 与 `web/css/*` 是生成物，`npm run verify:extract` 要求它们
 *  逐字节等于生成器输出，所以这里一行 CSS 都不加。
 */

import { ADMIN_MODE_TTL_CAP_SECONDS, adminModeAction, adminModeStatus } from './source.js';

export const ADMIN_MODE_STATES = Object.freeze({
  UNKNOWN: 'unknown',
  DISABLED: 'disabled',
  MEMBER: 'member',
  ELEVATED: 'elevated',
});

/** 状态码 ⇒ 人能读的一句话。未知码原样显示，不猜。 */
const REASON_TEXT = Object.freeze({
  'no-server-session': '本预览没有服务端会话，无法提权',
  'no-fetch-available': '这个运行环境没有可用的请求通道',
  'request-failed': '请求没有送达，请重试',
  'endpoint-rejected': '服务端地址不是同源，已拒绝',
  'missing-csrf-token': '缺少 CSRF token，写请求被拒绝',
  'admin_mode_not_configured': '本次部署未配置共享管理口令',
  'invalid_credentials': '口令不正确',
  'rate_limited': '尝试过于频繁，请稍后再试',
  'not_authenticated': '会话已失效，请重新登录',
  'linux_auth_not_enabled': '这条会话不是 Linux 登录来的，不能提权',
  'contract:no-admin-mode': '服务端没有给出管理员模式状态',
});

export const reasonText = (code) => REASON_TEXT[code] ?? (code ? String(code) : null);

const isPlainObject = (value) =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/**
 * 服务端状态 ⇒ 界面状态。**纯函数**，因此每一种畸形输入都能被单测钉住。
 *
 * @param {unknown} status 服务端 ``admin_mode`` 对象，或它缺失时的任何东西
 * @returns {{state: string, configured: boolean, elevated: boolean, ttlSeconds: number,
 *            expiresAt: number|null, canEnter: boolean, canExit: boolean,
 *            title: string, detail: string|null, tone: string}}
 */
export function describeAdminMode(status) {
  const unknown = {
    state: ADMIN_MODE_STATES.UNKNOWN,
    configured: false,
    elevated: false,
    ttlSeconds: ADMIN_MODE_TTL_CAP_SECONDS,
    expiresAt: null,
    canEnter: false,
    canExit: false,
    title: '管理员模式：状态未知',
    detail: '还没有读到服务端状态',
    tone: 'unknown',
  };

  if (!isPlainObject(status) || typeof status.configured !== 'boolean') return unknown;

  const ttlRaw = Number(status.ttl_seconds);
  const ttlSeconds = Number.isFinite(ttlRaw) && ttlRaw > 0
    // 服务端说多长都要按上限显示：界面不替服务端承诺更长的时间。
    ? Math.min(Math.max(Math.trunc(ttlRaw), 60), ADMIN_MODE_TTL_CAP_SECONDS)
    : ADMIN_MODE_TTL_CAP_SECONDS;

  const expiresRaw = Number(status.expires_at);
  const expiresAt = Number.isFinite(expiresRaw) && expiresRaw > 0 ? expiresRaw : null;

  if (!status.configured) {
    return {
      ...unknown,
      state: ADMIN_MODE_STATES.DISABLED,
      ttlSeconds,
      title: '管理员模式：本次部署未配置',
      detail: reasonText('admin_mode_not_configured'),
      tone: 'disabled',
    };
  }

  const elevated = status.elevated === true;
  if (elevated) {
    return {
      state: ADMIN_MODE_STATES.ELEVATED,
      configured: true,
      elevated: true,
      ttlSeconds,
      expiresAt,
      canEnter: false,
      canExit: true,
      title: '管理员模式：已进入',
      detail: `最长 ${Math.round(ttlSeconds / 60)} 分钟后自动退出`,
      tone: 'elevated',
    };
  }

  return {
    state: ADMIN_MODE_STATES.MEMBER,
    configured: true,
    elevated: false,
    ttlSeconds,
    expiresAt: null,
    canEnter: true,
    canExit: false,
    title: '管理员模式：未进入',
    detail: '当前是成员视图；输入共享管理口令可临时进入',
    tone: 'member',
  };
}

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);

const statePillClass = (tone) =>
  tone === 'elevated' || tone === 'member' ? 'state-available' : `state-${tone}`;

/**
 * 界面状态 ⇒ HTML 字符串。纯函数，无 DOM、无网络。
 *
 * ``notice`` 是一次动作之后的反馈（成功或失败码），只作为文本呈现，不参与判定。
 */
export function renderAdminModeControl(view, { idPrefix = 'admin-mode', notice = null } = {}) {
  const described = view && typeof view.state === 'string' ? view : describeAdminMode(null);
  const id = (suffix) => `${idPrefix}-${suffix}`;

  const noticeHtml = notice
    ? `<p class="footnote" data-admin-mode-notice="${escapeHtml(notice.tone || 'info')}">${escapeHtml(notice.text)}</p>`
    : '';

  const enter = `
        <input class="field" id="${id('password')}" type="password" autocomplete="off"
               aria-label="共享管理口令" data-admin-mode-password
               ${described.canEnter ? '' : 'disabled'}>
        <button type="button" data-admin-mode-action="enter"
                ${described.canEnter ? '' : 'disabled aria-disabled="true"'}
                title="${escapeHtml(described.canEnter ? '用共享管理口令临时进入管理员模式' : described.detail || '当前不可进入')}">
          进入管理员模式
        </button>`;

  const exit = `
        <button type="button" data-admin-mode-action="exit"
                ${described.canExit ? '' : 'disabled aria-disabled="true"'}
                title="${escapeHtml(described.canExit ? '立即退出管理员模式' : '当前不在管理员模式')}">
          退出管理员模式
        </button>`;

  return `
    <section class="page-block module-panel" data-admin-mode="${escapeHtml(described.state)}">
      <div class="editorial-index">
        <b>·</b>
        <span>ADMIN MODE</span>
        <span class="state-pill ${statePillClass(described.tone)}" data-admin-mode-state>${escapeHtml(described.state)}</span>
      </div>
      <div class="group-title">
        <h2 class="section-h">${escapeHtml(described.title)}</h2>
        <p class="footnote">登录决定你是谁；管理员模式决定你此刻能看什么。两者是两件事。</p>
      </div>
      <div class="module-body">
        <div class="seg" role="group" aria-label="管理员模式">${enter}${exit}</div>
        <p class="guard ${described.tone === 'disabled' || described.tone === 'unknown' ? 'warn' : ''}"
           data-admin-mode-detail>${escapeHtml(described.detail || '')}</p>
        ${noticeHtml}
      </div>
    </section>`;
}

/**
 * 把控件挂进页面，并接上那两条同源调用。
 *
 * @param {object} opts
 * @param {Document} opts.doc
 * @param {Element}  opts.root         挂载点；控件作为它的子节点插入
 * @param {object}   opts.transport    ``{mode, base, fetchImpl}``，直接交给 source.js
 * @param {Function} [opts.getCsrfToken] 取会话绑定的 CSRF token（异步或同步）
 * @param {Function} [opts.onChange]   每次状态变化后的回调，给上层重画别的面板用
 */
export function mountAdminModeControl({
  doc,
  root,
  transport = { mode: 'unavailable' },
  getCsrfToken = null,
  onChange = null,
  idPrefix = 'admin-mode',
} = {}) {
  if (!doc || !root) throw new Error('mountAdminModeControl needs a document and a root');

  const host = doc.createElement('div');
  host.className = 'admin-mode-control';
  host.setAttribute('data-admin-mode-host', '1');
  root.appendChild(host);

  let view = describeAdminMode(null);
  let notice = null;
  let inFlight = false;

  const paint = () => {
    host.innerHTML = renderAdminModeControl(view, { idPrefix, notice });
  };

  const applyStatus = (status) => {
    view = describeAdminMode(status);
    if (typeof onChange === 'function') onChange(view);
  };

  /** 清空口令输入框。每一次读取之后都要做，无论成败。 */
  const clearPasswordField = () => {
    const field = host.querySelector(`#${idPrefix}-password`)
      || host.querySelector('[data-admin-mode-password]');
    if (field) field.value = '';
  };

  const readPasswordField = () => {
    const field = host.querySelector('[data-admin-mode-password]');
    return field && typeof field.value === 'string' ? field.value : '';
  };

  async function refresh() {
    if (inFlight) return view;
    inFlight = true;
    const result = await adminModeStatus(transport);
    inFlight = false;
    if (result?.ok) {
      notice = null;
      applyStatus(result.adminMode);
    } else {
      applyStatus(null);
      notice = { tone: 'warn', text: reasonText(result?.reason) || '状态读取失败' };
    }
    paint();
    return view;
  }

  async function act(action) {
    if (inFlight) return view;
    if (action === 'enter' && !view.canEnter) return view;
    if (action === 'exit' && !view.canExit) return view;

    const password = action === 'enter' ? readPasswordField() : undefined;
    // 输入框在读走之后立刻清空：这个值此后只属于正在发出的那一个请求。
    if (action === 'enter') clearPasswordField();

    inFlight = true;
    let token = null;
    try {
      token = typeof getCsrfToken === 'function' ? await getCsrfToken() : null;
    } catch {
      token = null;
    }
    const result = await adminModeAction({ ...transport, action, password, csrfToken: token });
    inFlight = false;

    if (result?.ok) {
      applyStatus(result.adminMode);
      notice = {
        tone: 'info',
        text: action === 'enter' ? '已进入管理员模式' : '已退出管理员模式',
      };
    } else {
      // 失败时**重新问一次服务端**，而不是本地推断：一次失败的口令不该让界面
      // 自作主张地认为提权状态变了。
      const status = await adminModeStatus(transport);
      applyStatus(status?.ok ? status.adminMode : null);
      notice = { tone: 'warn', text: reasonText(result?.reason) || '操作失败' };
    }
    paint();
    return view;
  }

  // 事件绑定在 host 上，一次即可 —— 重画换掉的是它的子节点。
  host.addEventListener('click', (event) => {
    const target = event.target && event.target.closest
      ? event.target.closest('[data-admin-mode-action]')
      : null;
    if (!target || target.disabled) return;
    act(target.getAttribute('data-admin-mode-action'));
  });

  paint();
  refresh();

  return { refresh, enter: () => act('enter'), exit: () => act('exit'), view: () => view };
}
