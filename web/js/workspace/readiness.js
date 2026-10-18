/*! web/js/workspace/readiness.js — the Overview's "what is actually connected" board.
 *
 *  One row per workspace (the eight tabs plus the Compute subpage), each naming
 *  the same-origin route it reads and the state that route is in *right now*,
 *  read back from the controllers that own it. Nothing here fetches or guesses:
 *  it is a projection of states other modules already decided, so the board can
 *  never be more optimistic than the page it summarises.
 *
 *  The vocabulary is the one the brief asks every view to distinguish:
 *  READY · LOADING · EMPTY · NOT CONFIGURED · DENIED · SIGNED OUT · STALE ·
 *  ERROR · DEMO. "Ready" is only ever said about a validated, non-synthetic
 *  answer; a demo fixture is DEMO even when it rendered perfectly.
 */

import { escapeHtml } from '../dom.js';

export const WS_STATES = Object.freeze({
  READY: 'ready',
  LOADING: 'loading',
  EMPTY: 'empty',
  NOT_CONFIGURED: 'not_configured',
  DENIED: 'denied',
  SIGNED_OUT: 'signed_out',
  STALE: 'stale',
  ERROR: 'error',
  DEMO: 'demo',
});

export const WS_LABELS = Object.freeze({
  ready: 'READY · 已验证',
  loading: 'LOADING · 读取中',
  empty: 'EMPTY · 确认为空',
  not_configured: 'NOT CONFIGURED · 未接线',
  denied: 'DENIED · 无授权',
  signed_out: 'SIGNED OUT · 需登录',
  stale: 'STALE · 已过期',
  error: 'ERROR · 不可用',
  demo: 'DEMO · 合成样例',
});

/** Verified host telemetry: LIVE only when the validator said so *and* the copy
 *  on screen is still inside its age budget. */
export function hostState(host = {}) {
  if (host.state === 'live' && host.freshness === 'live') return { state: WS_STATES.READY, reason: null };
  const reason = host.reason ?? null;
  if (reason === 'not-yet-fetched') return { state: WS_STATES.LOADING, reason };
  if (reason === 'expired' || reason === 'clock-rollback' || host.freshness === 'stale') {
    return { state: WS_STATES.STALE, reason: reason ?? 'stale' };
  }
  return { state: WS_STATES.ERROR, reason: reason ?? 'unknown' };
}

/** The `portal-ops/1` layer (models, API, observability, evaluation, admin). */
export function opsState(ops = {}) {
  if (ops.mode === 'mock') return { state: WS_STATES.DEMO, reason: 'design-samples-opt-in' };
  if (ops.mode === 'unavailable') return { state: WS_STATES.NOT_CONFIGURED, reason: ops.reason ?? 'auth-bff-not-connected' };
  const map = {
    ready: WS_STATES.READY,
    loading: WS_STATES.LOADING,
    denied: WS_STATES.DENIED,
    signed_out: WS_STATES.SIGNED_OUT,
  };
  return { state: map[ops.state] ?? WS_STATES.ERROR, reason: ops.reason ?? null };
}

/** The call log. A synthetic payload is DEMO whatever it contained. */
export function callsState(calls = {}) {
  const demo = calls.transport === 'demo';
  const map = {
    ready: demo ? WS_STATES.DEMO : WS_STATES.READY,
    empty: demo ? WS_STATES.DEMO : WS_STATES.EMPTY,
    loading: WS_STATES.LOADING,
    not_configured: WS_STATES.NOT_CONFIGURED,
    denied: WS_STATES.DENIED,
    signed_out: WS_STATES.SIGNED_OUT,
  };
  return { state: map[calls.state] ?? WS_STATES.ERROR, reason: calls.reason ?? null };
}

/** The Atlas. In live mode its synthetic body is gated away, so "no source". */
export function atlasState(atlas = {}, dataMode = 'live') {
  if (dataMode === 'demo') return { state: WS_STATES.DEMO, reason: 'design-samples-opt-in' };
  if (atlas.state === 'live' && !atlas.synthetic) return { state: WS_STATES.READY, reason: null };
  if (atlas.state === 'empty') return { state: WS_STATES.EMPTY, reason: null };
  return { state: WS_STATES.NOT_CONFIGURED, reason: 'no-authorized-calls-source' };
}

/** The workspaces, in navigation order, and the route each one depends on. */
export const WORKSPACES = Object.freeze([
  { page: 'overview', name: '实验室概览', route: 'GET /api/infra/summary', source: 'host', what: '主机遥测、服务连通性' },
  { page: 'compute', name: '算力中心', route: 'GET /api/infra/summary', source: 'host', what: 'GPU / VRAM / 温度 / 功耗、引擎在位' },
  { page: 'models', name: '模型与对话', route: 'GET /api/v2/ops', source: 'ops', what: 'Registry 部署、所有权、共享范围' },
  { page: 'api', name: 'API 管理', route: 'GET /auth/tokens · GET /api/v2/ops', source: 'ops', what: 'Scoped Token、Endpoint、接入示例' },
  { page: 'calls', name: '我的调用', route: 'GET /api/atlas/calls', source: 'calls', what: '本人 key 的逐条调用元数据' },
  { page: 'observability', name: '监控与日志', route: 'GET /api/v2/ops/usage', source: 'ops', what: '我的 / 全局用量与趋势' },
  { page: 'atlas', name: '调用结构图谱', route: 'GET /api/atlas/calls (聚合)', source: 'atlas', what: '联动图表、排名、热力矩阵' },
  { page: 'evaluation', name: '评测中心', route: 'GET /api/v2/ops', source: 'ops', what: 'EvalScope / 旧 Benchmark 报告与队列' },
  { page: 'admin', name: '管理', route: 'GET /api/v2/ops · GET /auth/admin/status', source: 'ops', what: '会话、scope、限时管理员模式' },
]);

/** Project a snapshot of controller states onto the nine rows. Pure. */
export function deriveReadiness(snapshot = {}) {
  const by = {
    host: hostState(snapshot.host),
    ops: opsState(snapshot.ops),
    calls: callsState(snapshot.calls),
    atlas: atlasState(snapshot.atlas, snapshot.dataMode),
  };
  const rows = WORKSPACES.map((ws) => ({ ...ws, ...by[ws.source] }));
  const counts = {};
  for (const row of rows) counts[row.state] = (counts[row.state] ?? 0) + 1;
  return { rows, counts };
}

const ORDER = ['ready', 'demo', 'loading', 'empty', 'stale', 'not_configured', 'signed_out', 'denied', 'error'];

export function renderReadiness({ rows, counts }) {
  const summary = ORDER.filter((s) => counts[s])
    .map((s) => `<span><b>${counts[s]}</b> ${escapeHtml(WS_LABELS[s])}</span>`).join('');
  const body = rows.map((row) => (
    `<tr data-ws-row="${row.page}" data-state="${row.state}">` +
    `<th scope="row">${escapeHtml(row.name)}<span class="tiny">${escapeHtml(row.what)}</span></th>` +
    `<td><span class="mono">${escapeHtml(row.route)}</span></td>` +
    `<td><span class="ws-state" data-state="${row.state}">${escapeHtml(WS_LABELS[row.state])}</span>` +
    (row.reason ? `<span class="tiny mono">${escapeHtml(row.reason)}</span>` : '') + `</td>` +
    `<td><a class="secondary" href="#${row.page}" data-ws-go="${row.page}">前往 →</a></td>` +
    `</tr>`
  )).join('');
  return (
    `<div class="ws-summary" role="status">${summary}</div>` +
    `<div class="tablewrap"><table class="ws-table" data-ws-board>` +
    `<thead><tr><th scope="col">工作区</th><th scope="col">同源数据路由</th><th scope="col">当前状态</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead>` +
    `<tbody>${body}</tbody></table></div>` +
    `<p class="footnote">状态直接读自各视图的控制器，不另发请求；READY 只表示该路由返回了通过契约校验的非合成答复，不等于业务已上线。</p>`
  );
}

/**
 * Mount the board into the Overview and keep it current.
 * @param {{doc?:Document, read:() => object, intervalMs?:number}} opts
 *        `read` returns a fresh snapshot of controller states each call.
 */
export function mountReadiness({ doc = globalThis.document, read, intervalMs = 1000 } = {}) {
  const view = doc?.getElementById?.('view-overview');
  if (!view || typeof read !== 'function') return null;
  let section = view.querySelector('[data-ws="readiness"]');
  if (!section) {
    section = doc.createElement('section');
    section.className = 'page-block ws-panel';
    section.dataset.ws = 'readiness';
    section.setAttribute('aria-labelledby', 'ws-readiness-title');
    section.innerHTML =
      '<div class="group-title"><h2 class="section-h" id="ws-readiness-title">工作区就绪度 <em class="heading-en">/ WHAT IS CONNECTED</em></h2>' +
      '<span class="smallmeta">9 VIEWS · LIVE STATE</span></div>' +
      '<p class="ws-lede">每个工作区依赖哪条同源路由、此刻处于什么状态。未接线的路由不会被渲染成数字；点「前往」查看该视图给出的具体原因与下一步。</p>' +
      '<div data-ws-body></div>';
    const hero = view.querySelector(':scope > .hero');
    if (hero) hero.after(section); else view.prepend(section);
  }
  const body = section.querySelector('[data-ws-body]');
  let last = '';
  let current = null;
  function update() {
    current = deriveReadiness(read());
    const html = renderReadiness(current);
    if (html !== last) { body.innerHTML = html; last = html; }
    return current;
  }
  update();
  // The owning controllers settle within the first second of boot; re-read
  // promptly then, so the board does not lag behind the views it summarises.
  const early = [150, 500, 1000].map((ms) => setTimeout(update, ms));
  const timer = setInterval(update, intervalMs);
  return { update, stop: () => { clearInterval(timer); early.forEach(clearTimeout); }, get current() { return current; } };
}
