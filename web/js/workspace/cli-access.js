/*! web/js/workspace/tokens.js — the API page's scoped-token list (read-only).
 *
 *  Contract: `GET /auth/tokens`, served by `backend/auth/routes.py`
 *  (`tokens_list`, IAM lane #45 / PR #52) and reached through the BFF's
 *  delegated `/auth/` prefix. It returns `TokenRecord.to_public_dict()` — a
 *  record that by construction never held the plaintext. This module refuses a
 *  payload that carries one anyway: a management page that can be made to render
 *  a live secret is a credential-disclosure bug, not a display bug.
 *
 *  Writes stay disabled. `POST /auth/tokens`, `/revoke` and `/rotate` exist in
 *  the same backend, but the browser flow they need — a CSRF token bound to a
 *  real Linux/PAM session, and a one-time reveal that is never re-rendered — has
 *  not been verified end to end. Each button says exactly that instead of being a
 *  control that looks live and does nothing.
 *
 *  `?demo=1` swaps the network for a SYNTHETIC list; nothing is fetched.
 */

import { escapeHtml } from '../dom.js';
import { WS_LABELS, WS_STATES } from './readiness.js';

export const TOKENS_ENDPOINT = '/auth/tokens';

/** Fields that would mean the server handed the browser a credential. */
const FORBIDDEN = Object.freeze(['token', 'secret', 'plaintext', 'value', 'key']);

export const TOKEN_WRITE_REASON =
  '后端已有 POST /auth/tokens（签发）/ revoke / rotate（IAM #52），但浏览器侧的 CSRF + Linux PAM 会话 + 一次性明文展示流程尚未端到端验证；本构建不启用写入。';

/** Validate a `GET /auth/tokens` body. Returns `{ok, tokens, error}`. */
export function validateTokenList(body) {
  const list = body?.data?.tokens;
  if (body?.ok !== true || !Array.isArray(list)) return { ok: false, tokens: [], error: 'bad-shape' };
  for (const [i, t] of list.entries()) {
    if (!t || typeof t !== 'object') return { ok: false, tokens: [], error: `tokens[${i}]-not-object` };
    for (const f of FORBIDDEN) {
      if (f in t) return { ok: false, tokens: [], error: `tokens[${i}]-carries-${f}` };
    }
    if (typeof t.token_id !== 'string' || !t.token_id) return { ok: false, tokens: [], error: `tokens[${i}]-no-id` };
    if (!Array.isArray(t.scopes)) return { ok: false, tokens: [], error: `tokens[${i}]-no-scopes` };
  }
  return { ok: true, tokens: list, error: null };
}

/** active / expired / revoked / rotated — the record's own facts, at `now`. */
export function tokenLifecycle(t, now = Date.now() / 1000) {
  if (t.revoked || t.revoked_at) return t.rotated_at ? 'rotated' : 'revoked';
  if (Number.isFinite(t.expires_at) && t.expires_at <= now) return 'expired';
  return 'active';
}

const LIFECYCLE_LABEL = { active: '有效', expired: '已过期', revoked: '已吊销', rotated: '已轮换' };

const when = (sec) => (Number.isFinite(sec)
  ? new Date(sec * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z').replace('T', ' ')
  : '—');

/** SYNTHETIC list for `?demo=1`. Every id and label is visibly demo. */
export function demoTokens(now = Date.now() / 1000) {
  const day = 86400;
  return [
    { token_id: 'tok_demo_7f3a', principal: 'demo-user-a', scopes: ['endpoints:invoke', 'registry:read'], roles: ['member'], label: 'demo · notebook', created_at: now - 9 * day, expires_at: now + 21 * day, revoked_at: null, rotated_at: null, rotation_of: null, last_used_at: now - 3600, revoked: false },
    { token_id: 'tok_demo_c219', principal: 'demo-user-a', scopes: ['endpoints:invoke'], roles: ['member'], label: 'demo · ci-runner', created_at: now - 40 * day, expires_at: now - 10 * day, revoked_at: null, rotated_at: null, rotation_of: null, last_used_at: now - 12 * day, revoked: false },
    { token_id: 'tok_demo_0b88', principal: 'demo-user-a', scopes: ['registry:read'], roles: ['member'], label: 'demo · laptop (rotated)', created_at: now - 30 * day, expires_at: now + 5 * day, revoked_at: now - 2 * day, rotated_at: now - 2 * day, rotation_of: null, last_used_at: now - 2 * day, revoked: true },
  ];
}

/** Map one response to a page state. */
export function tokensStateFor(status, validated) {
  if (status === 401) return WS_STATES.SIGNED_OUT;
  if (status === 403) return WS_STATES.DENIED;
  if ([404, 501, 503].includes(status)) return WS_STATES.NOT_CONFIGURED;
  if (status < 200 || status >= 300 || !validated?.ok) return WS_STATES.ERROR;
  return validated.tokens.length ? WS_STATES.READY : WS_STATES.EMPTY;
}

const NOTICE = {
  loading: '正在读取你的 scoped token 列表…',
  empty: '服务端确认你名下没有 token。签发需在已验证的会话中进行（见下方原因）。',
  not_configured: '本部署没有提供 /auth/tokens（404/501/503）：token 服务未配置或 BFF 未接线。这不是「没有 token」。',
  signed_out: '需要 Linux PAM 会话（401）。登录之后，这里列出你本人名下的 token —— 不含任何明文。',
  denied: '会话有效但没有查看 token 的授权（403）。',
  error: '响应无法通过契约校验，或请求失败；不渲染任何部分结果。',
};

export function renderTokens({ state, tokens = [], reason = null, demo = false, now = Date.now() / 1000 }) {
  const head =
    `<div class="group-title" style="margin-top:0"><span class="ws-state" data-state="${state}">${escapeHtml(WS_LABELS[state] ?? state)}</span>` +
    `<span class="actions" style="margin:0"><button type="button" class="primary" data-token-issue disabled aria-disabled="true" title="${escapeHtml(TOKEN_WRITE_REASON)}">签发新 Token</button>` +
    (state === WS_STATES.READY || state === WS_STATES.DEMO || state === WS_STATES.EMPTY
      ? '' : `<button type="button" class="secondary" data-token-retry>重试</button>`) +
    `</span></div>`;
  const demoNote = demo
    ? '<div class="guard warn">SYNTHETIC / DEMO：以下 token 记录由浏览器内 fixture 生成，未请求任何服务器。</div>'
    : '';
  if (!tokens.length) {
    return head + demoNote +
      `<p class="module-empty">${escapeHtml(NOTICE[state] ?? NOTICE.error)}</p>` +
      (reason ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>` : '') +
      `<p class="footnote">${escapeHtml(TOKEN_WRITE_REASON)}</p>`;
  }
  const rows = tokens.map((t) => {
    const life = tokenLifecycle(t, now);
    return (
      `<tr data-token-row="${escapeHtml(t.token_id)}" data-lifecycle="${life}">` +
      `<th scope="row">${escapeHtml(t.label ?? '（无标签）')}<span class="tiny mono">${escapeHtml(t.token_id)}</span></th>` +
      `<td>${t.scopes.map((s) => `<span class="badge">${escapeHtml(s)}</span>`).join(' ')}</td>` +
      `<td><span class="ws-state" data-state="${life === 'active' ? 'ready' : life === 'expired' ? 'stale' : 'error'}">${LIFECYCLE_LABEL[life]}</span></td>` +
      `<td class="mono">${when(t.created_at)}</td><td class="mono">${when(t.expires_at)}</td><td class="mono">${when(t.last_used_at)}</td>` +
      `<td><button type="button" class="secondary" disabled aria-disabled="true" title="${escapeHtml(TOKEN_WRITE_REASON)}">轮换</button> ` +
      `<button type="button" class="secondary" disabled aria-disabled="true" title="${escapeHtml(TOKEN_WRITE_REASON)}">吊销</button></td>` +
      `</tr>`
    );
  }).join('');
  return head + demoNote +
    `<div class="tablewrap"><table class="ws-table" data-token-table><thead><tr>` +
    `<th scope="col">标签 / ID</th><th scope="col">Scopes</th><th scope="col">状态</th><th scope="col">创建 (UTC)</th>` +
    `<th scope="col">到期 (UTC)</th><th scope="col">最近使用</th><th scope="col">操作</th></tr></thead><tbody>${rows}</tbody></table></div>` +
    `<p class="footnote">列表只含 token 的元数据；明文仅在签发那一刻返回一次，之后任何接口都不再提供。${escapeHtml(TOKEN_WRITE_REASON)}</p>`;
}

/**
 * Mount and load the panel in `#view-api`.
 * @param {{doc?:Document, demo?:boolean, fetchImpl?:Function}} opts
 */
export function mountTokens({ doc = globalThis.document, demo = false, fetchImpl } = {}) {
  const view = doc?.getElementById?.('view-api');
  if (!view) return null;
  const section = doc.createElement('section');
  section.className = 'page-block ws-panel';
  section.dataset.ws = 'tokens';
  section.setAttribute('aria-labelledby', 'ws-tokens-title');
  section.innerHTML =
    '<div class="group-title"><h2 class="section-h" id="ws-tokens-title">我的 Scoped Token <em class="heading-en">/ CLI CREDENTIALS</em></h2>' +
    '<span class="smallmeta">GET /auth/tokens · READ-ONLY</span></div>' +
    '<p class="ws-lede">供 <span class="mono">llmlab</span> CLI 与 SDK 使用的个人凭据。服务端按会话主体返回本人名下的记录；页面只读元数据，从不显示明文。</p>' +
    '<div data-ws-body></div>';
  const hero = view.querySelector(':scope > .hero');
  if (hero) hero.after(section); else view.prepend(section);
  const body = section.querySelector('[data-ws-body]');

  let state = WS_STATES.LOADING;
  let tokens = [];
  let reason = null;
  const paint = () => {
    body.innerHTML = renderTokens({ state, tokens, reason, demo });
    body.querySelector('[data-token-retry]')?.addEventListener('click', () => load());
  };

  async function load() {
    state = WS_STATES.LOADING;
    tokens = [];
    paint();
    if (demo) {
      tokens = demoTokens();
      state = WS_STATES.DEMO;
      reason = 'design-samples-opt-in';
      paint();
      return state;
    }
    const doFetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
    try {
      const res = await doFetch(TOKENS_ENDPOINT, {
        method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' },
      });
      let validated = null;
      if (res.ok) {
        try { validated = validateTokenList(await res.json()); } catch { validated = { ok: false, error: 'not-json', tokens: [] }; }
      }
      state = tokensStateFor(res.status, validated);
      tokens = state === WS_STATES.READY ? validated.tokens : [];
      reason = validated?.error ?? (res.ok ? null : `http-${res.status}`);
    } catch {
      state = WS_STATES.ERROR;
      reason = 'request-failed';
    }
    paint();
    return state;
  }

  const first = load();
  return { load, first, get state() { return state; }, get tokens() { return tokens; }, get reason() { return reason; } };
}
