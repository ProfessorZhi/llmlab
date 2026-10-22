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
  localLogin: '/auth/local/login',
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
  integrations: '/api/portal/integrations',
  discovery: '/api/v2/registry/discovery',
  registryModels: '/api/v2/registry/models',
  gatewayOverview: '/api/gateway/overview',
  gatewayKeys: '/api/gateway/keys',
  gatewayKeyCreate: '/api/gateway/keys/create',
  gatewayModels: '/api/gateway/models',
  gatewayPublish: '/api/gateway/models/publish',
  gatewayLogs: '/api/gateway/logs',
  gatewayPlayground: '/api/gateway/playground',
  // 主机系统读数与引擎实时负载：形状取自 2026-10-22 的真实快照（docs/console-snapshot-20261022）。
  // 本部署没有这两条路由时它们是 404 ⇒ not_configured，页面就不画那一块。
  infraSystem: '/api/infra/system',
  engineLive: '/api/v2/engine/live',
});

/** 路径里的 id 只允许安全字符，免得拼出另一条路由。 */
export const pathId = (id) => encodeURIComponent(String(id ?? '')).replace(/%/g, '');
export const gatewayKeyRevoke = (id) => `/api/gateway/keys/${pathId(id)}/revoke`;
export const gatewayKeyUpdate = (id) => `/api/gateway/keys/${pathId(id)}/update`;
export const gatewayLogDetail = (id) => `/api/gateway/logs/${pathId(id)}`;
export const gatewayModelUpdate = (id) => `/api/gateway/models/${pathId(id)}/update`;
export const engineParams = (id) => `/api/v2/registry/engines/${pathId(id)}/params`;
export const engineParamsUpdate = (id) => `/api/v2/registry/engines/${pathId(id)}/params/update`;

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
      // 未登录时的 CSRF 令牌是一次性的，失败后已被服务端消耗；会话令牌也可能已随会话失效。
      // 丢掉缓存，下一次写操作重新领取，否则重试登录会一直被拒。
      rememberCsrf(null);
      return { ok: false, state: stateForStatus(res.status), status: res.status, data: null, reason: payload?.error?.code ?? `http-${res.status}`, message: payload?.error?.detail ?? payload?.error?.message ?? null };
    }
    if (payload?.data?.csrf_token) rememberCsrf(payload.data.csrf_token);
    return { ok: true, state: 'ready', status: res.status, data: payload?.data ?? payload, reason: null, message: null };
  } catch {
    rememberCsrf(null);
    return { ok: false, state: 'error', status: 0, data: null, reason: 'network', message: null };
  }
}

/** 平台账户优先；账户登录拒绝时再试本机管理员（bootstrap 建立的 local admin）。 */
export async function login(username, password, opts) {
  const r = await send(PATHS.login, { username, password }, opts);
  if (r.ok || r.status !== 401) return r;
  const local = await send(PATHS.localLogin, { username, password }, opts);
  // 本机管理员未配置（503）时，向用户报告的仍是账户登录的结果。
  return local.ok || local.status === 401 || local.status === 429 ? local : r;
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

export async function loadDiscovery(opts) {
  const r = await getJSON(PATHS.discovery, { timeoutMs: 20000, ...opts });
  if (r.state !== 'ready') return r;
  const data = r.value?.data;
  return data && typeof data === 'object' ? { ...r, value: data } : { ...r, state: 'error', value: null, reason: 'bad-shape' };
}

export async function loadTokens(opts) {
  const r = await getJSON(PATHS.tokens, opts);
  if (r.state !== 'ready') return r;
  const list = r.value?.data?.tokens;
  return Array.isArray(list) ? { ...r, value: list } : { ...r, state: 'error', value: null, reason: 'bad-shape' };
}

export async function loadIntegrations(opts) {
  const r = await getJSON(PATHS.integrations, opts);
  if (r.state !== 'ready') return r;
  const list = r.value?.data?.integrations;
  return Array.isArray(list) ? { ...r, value: list } : { ...r, state: 'error', value: null, reason: 'bad-shape' };
}

/** ``{ok, data}`` 信封 ⇒ ``data``；形状不对就是 error。 */
export async function loadData(path, opts) {
  const r = await getJSON(path, opts);
  if (r.state !== 'ready') return r;
  const data = r.value?.data;
  return data && typeof data === 'object' ? { ...r, value: data } : { ...r, state: 'error', value: null, reason: 'bad-shape' };
}

/** 插件的跳转地址只允许 http(s) 绝对地址或站内路径；其余一律不渲染成链接。 */
export function safeJumpUrl(url) {
  if (typeof url !== 'string' || !url) return null;
  if (url.startsWith('/') && !url.startsWith('//') && !url.startsWith('/\\')) return url;
  try {
    const u = new URL(url);
    return (u.protocol === 'http:' || u.protocol === 'https:') && !u.username && !u.password ? u.href : null;
  } catch { return null; }
}

/** 各状态在页面上的说明文字。 */
export const STATE_TEXT = Object.freeze({
  signed_out: { title: '需要登录', body: '会话已失效，请重新登录。' },
  denied: { title: '没有访问权限', body: '当前账户没有查看此内容的权限，请联系管理员。' },
  not_configured: { title: '数据源未接入', body: '本部署尚未启用该数据源，接入后此处会自动显示。' },
  error: { title: '暂时无法读取', body: '数据读取失败或格式不符合约定，已停止显示以免误导。' },
});
