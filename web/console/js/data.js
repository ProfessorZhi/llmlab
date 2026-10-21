/*! web/console/js/data.js — 控制台的网络层。只访问同源接口，携带会话 Cookie。
 *
 *  每个读取都返回 {state, value, reason}：
 *    ready          已取到并通过契约校验
 *    signed_out     401，需要登录
 *    denied         403，无权限
 *    not_configured 404/501/503，本部署未启用该接口
 *    error          网络失败、非 JSON 或契约校验未通过
 *  页面依据 state 渲染说明，绝不把失败渲染成数字。 */
import { validateOps } from '../../js/features/contract.js';
import { validateHostSummary } from '../../js/infra/host-status.js';
import { validateCallLog } from '../../js/calls/contract.js';

export const PATHS = Object.freeze({
  me: '/auth/me',
  csrf: '/auth/csrf',
  login: '/auth/account/login',
  logout: '/auth/logout',
  changePassword: '/auth/account/password',
  ops: '/api/v2/ops',
  host: '/api/infra/summary',
  calls: '/api/atlas/calls',
  tokens: '/auth/tokens',
  tokenRevoke: '/auth/tokens/revoke',
  users: '/auth/admin/users',
  userReset: '/auth/admin/users/reset-password',
  userRole: '/auth/admin/users/role',
  userLinux: '/auth/admin/users/linux',
  userStatus: '/auth/admin/users/status',
  preflight: '/api/v2/registry/engines/preflight',
  registryEngines: '/api/v2/registry/engines',
});

const NOT_CONFIGURED = new Set([404, 501, 503]);

function stateForStatus(status) {
  if (status === 401) return 'signed_out';
  if (status === 403) return 'denied';
  if (NOT_CONFIGURED.has(status)) return 'not_configured';
  return 'error';
}

export async function getJSON(path, { fetchImpl = globalThis.fetch, timeoutMs = 8000 } = {}) {
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  try {
    const res = await fetchImpl(path, { method: 'GET', headers: { Accept: 'application/json' }, credentials: 'same-origin', signal: ctrl?.signal });
    if (!res.ok) {
      let code = null;
      try { code = (await res.json())?.error?.code ?? null; } catch { /* 非 JSON 错误体 */ }
      return { state: stateForStatus(res.status), status: res.status, value: null, reason: code ?? `http-${res.status}` };
    }
    try {
      return { state: 'ready', status: res.status, value: await res.json(), reason: null };
    } catch {
      return { state: 'error', status: res.status, value: null, reason: 'not-json' };
    }
  } catch (err) {
    return { state: 'error', status: 0, value: null, reason: err?.name === 'AbortError' ? 'timeout' : 'network' };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

let csrfToken = null;

export async function csrf({ fetchImpl = globalThis.fetch } = {}) {
  if (csrfToken) return csrfToken;
  const r = await getJSON(PATHS.csrf, { fetchImpl });
  csrfToken = r.state === 'ready' ? r.value?.data?.csrf_token ?? null : null;
  return csrfToken;
}

export function rememberCsrf(token) { csrfToken = token || null; }

/** 带 CSRF 的写请求。 */
export async function send(path, body, { method = 'POST', fetchImpl = globalThis.fetch } = {}) {
  const token = await csrf({ fetchImpl });
  try {
    const res = await fetchImpl(path, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...(token ? { 'X-CSRF-Token': token } : {}) },
      credentials: 'same-origin',
      body: JSON.stringify(body ?? {}),
    });
    let payload = null;
    try { payload = await res.json(); } catch { /* 空响应 */ }
    if (!res.ok) {
      return { ok: false, state: stateForStatus(res.status), status: res.status, data: null, reason: payload?.error?.code ?? `http-${res.status}`, message: payload?.error?.detail ?? payload?.error?.message ?? null };
    }
    if (payload?.data?.csrf_token) rememberCsrf(payload.data.csrf_token);
    return { ok: true, state: 'ready', status: res.status, data: payload?.data ?? payload, reason: null, message: null };
  } catch {
    return { ok: false, state: 'error', status: 0, data: null, reason: 'network', message: null };
  }
}

export async function loadMe(opts) {
  const r = await getJSON(PATHS.me, opts);
  if (r.state !== 'ready') return r;
  const user = r.value?.data?.user;
  return user ? { ...r, value: user } : { ...r, state: 'error', value: null, reason: 'bad-shape' };
}

export async function loadOps(opts) {
  const r = await getJSON(PATHS.ops, opts);
  if (r.state !== 'ready') return r;
  const checked = validateOps(r.value?.data?.ops);
  return checked.ok ? { ...r, value: checked.ops } : { ...r, state: 'error', value: null, reason: 'contract', errors: checked.errors };
}

export async function loadHost(opts) {
  const r = await getJSON(PATHS.host, opts);
  if (r.state !== 'ready') return r;
  const verdict = validateHostSummary(r.value);
  return verdict.accepted ? { ...r, value: verdict } : { ...r, state: 'error', value: null, reason: verdict.reason ?? 'contract' };
}

export async function loadCalls(opts) {
  const r = await getJSON(PATHS.calls, opts);
  if (r.state !== 'ready') return r;
  const checked = validateCallLog(r.value?.data?.call_log);
  return checked.ok ? { ...r, value: checked.log } : { ...r, state: 'error', value: null, reason: 'contract', errors: checked.errors };
}

export async function loadUsers(opts) {
  const r = await getJSON(PATHS.users, opts);
  if (r.state !== 'ready') return r;
  const users = r.value?.data?.users ?? r.value?.users;
  return Array.isArray(users) ? { ...r, value: users } : { ...r, state: 'error', value: null, reason: 'bad-shape' };
}

/** 各状态在页面上的说明文字。 */
export const STATE_TEXT = Object.freeze({
  signed_out: { title: '需要登录', body: '会话已失效，请重新登录。' },
  denied: { title: '没有访问权限', body: '当前账户没有查看此内容的权限，请联系管理员。' },
  not_configured: { title: '数据源未接入', body: '本部署尚未启用该数据源，接入后此处会自动显示。' },
  error: { title: '暂时无法读取', body: '数据读取失败或格式不符合约定，已停止显示以免误导。' },
});
