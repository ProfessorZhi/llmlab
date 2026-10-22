/*! web/js/workspace/routes.js — Administration's integration matrix.
 *
 *  Which same-origin routes this front-end depends on, who declares them, and
 *  whether the browser is allowed to write through them. The first block is a
 *  mirror of `backend/bff/route_map.py::ROUTES`; `tests/web-features/
 *  workspace.test.js` parses that file and fails if the two drift, so this table
 *  cannot quietly claim a route the BFF does not declare (or forget one it does).
 *
 *  The second block names the routes the UI calls that are **not** in the BFF
 *  route map — they are served by the delegated auth application or only by the
 *  loopback preview server. Saying so is the point: an operator reading this page
 *  should be able to see exactly what a production BFF still has to serve.
 */

import { escapeHtml } from '../dom.js';

/**
 * Mirror of `backend/bff/route_map.py::ROUTES` — the single route-map contract.
 *
 * Pinned to the 20-route table: the 17 declared by BFF PR #85 (`6062ed39`),
 * `evaluation.report_download` mounted by BFF #92 from Evaluation #78 (PR #91),
 * `portal.events` mounted by BFF #110 (V1-11 operator audit timeline), and
 * `ops.invoke` mounted by BFF SEAM-1 (PR #115), in table order:
 * `ops.*`, then the session/read routes the earlier preview server used to serve
 * (`portal.infra_summary`, `atlas.calls`, `evaluation.*`, `portal.services`,
 * `portal.capabilities`), then `auth.*`. Every row keeps the method, the
 * session/anonymous requirement, read/write and the primary scope — the matrix is
 * a declaration, never a grant: listing a route here does not make it live and
 * does not enable a write.
 *
 * `tests/web-features/workspace.test.js` parses the Python table and requires
 * every row it declares to match this mirror exactly. On a checkout that is
 * stacked on the BFF PR (the Python file still has the shorter pre-#85 table),
 * the rows #85 adds are a named pending set — not a tolerated difference.
 */
export const BFF_ROUTES = Object.freeze([
  { name: 'ops.read', method: 'GET', path: '/api/v2/ops', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '模型 / API / 监控 / 评测 / 管理' },
  { name: 'ops.usage', method: 'GET', path: '/api/v2/ops/usage', auth: 'session', kind: 'read', scope: 'observability:self:read', owner: 'Data#47', ui: '监控与日志' },
  { name: 'ops.engines', method: 'GET', path: '/api/v2/ops/engines', auth: 'session', kind: 'read', scope: 'observability:self:read', owner: 'Data#47', ui: '算力中心 · 引擎' },
  { name: 'ops.share', method: 'PUT', path: '/api/v2/ops/deployments/{deployment_id}/share', auth: 'session', kind: 'write', scope: 'portal:endpoints:share', owner: 'Integration#49', ui: '模型与对话 · 共享' },
  // Web registration of an inference service (2026-10-21). Owner is the session
  // principal (platform account or Linux principal); the Registry records the
  // Linux user it observes serving the port. Used by the console's 登记推理服务 flow.
  { name: 'registry.engines.preflight', method: 'POST', path: '/api/v2/registry/engines/preflight', auth: 'session', kind: 'write', scope: 'registry:write', owner: 'Integration#49', ui: '推理服务 · 登记预检' },
  { name: 'registry.engines.register', method: 'POST', path: '/api/v2/registry/engines', auth: 'session', kind: 'write', scope: 'registry:write', owner: 'Integration#49', ui: '推理服务 · 登记' },
  { name: 'registry.discovery', method: 'GET', path: '/api/v2/registry/discovery', auth: 'session', kind: 'read', scope: 'registry:read', owner: 'Integration#49', ui: '推理服务 / 模型库 · 本机发现' },
  { name: 'registry.models.register', method: 'POST', path: '/api/v2/registry/models', auth: 'session', kind: 'write', scope: 'registry:write', owner: 'Integration#49', ui: '模型库 · 登记模型' },
  // Integrated gateway (2026-10-22): same-origin /v1 with per-user virtual keys, model routes,
  // per-key logs (bodies opt-in per key), and per-engine runtime parameters.
  { name: 'gateway.overview', method: 'GET', path: '/api/gateway/overview', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '网关 · 概览' },
  { name: 'gateway.keys', method: 'GET', path: '/api/gateway/keys', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '网关 · API 密钥' },
  { name: 'gateway.keys.create', method: 'POST', path: '/api/gateway/keys/create', auth: 'session', kind: 'write', scope: null, owner: 'Integration#49', ui: '网关 · 创建密钥' },
  { name: 'gateway.keys.revoke', method: 'POST', path: '/api/gateway/keys/{key_id}/revoke', auth: 'session', kind: 'write', scope: null, owner: 'Integration#49', ui: '网关 · 撤销密钥' },
  { name: 'gateway.keys.update', method: 'POST', path: '/api/gateway/keys/{key_id}/update', auth: 'session', kind: 'write', scope: null, owner: 'Integration#49', ui: '网关 · 密钥设置（内容记录）' },
  { name: 'gateway.models', method: 'GET', path: '/api/gateway/models', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '网关 · 模型路由' },
  { name: 'gateway.models.publish', method: 'POST', path: '/api/gateway/models/publish', auth: 'session', kind: 'write', scope: null, owner: 'Integration#49', ui: '网关 · 发布模型' },
  { name: 'gateway.models.update', method: 'POST', path: '/api/gateway/models/{route_id}/update', auth: 'session', kind: 'write', scope: null, owner: 'Integration#49', ui: '网关 · 修改路由' },
  { name: 'gateway.logs', method: 'GET', path: '/api/gateway/logs', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '网关 · 调用日志' },
  { name: 'gateway.logs.detail', method: 'GET', path: '/api/gateway/logs/{request_id}', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '网关 · 单次请求详情' },
  { name: 'gateway.playground', method: 'POST', path: '/api/gateway/playground', auth: 'session', kind: 'write', scope: null, owner: 'Integration#49', ui: '网关 · 在线体验' },
  { name: 'registry.engines.params', method: 'GET', path: '/api/v2/registry/engines/{engine_id}/params', auth: 'session', kind: 'read', scope: 'registry:read', owner: 'Integration#49', ui: '推理服务 · 运行参数' },
  { name: 'registry.engines.params.update', method: 'POST', path: '/api/v2/registry/engines/{engine_id}/params/update', auth: 'session', kind: 'write', scope: 'registry:write', owner: 'Integration#49', ui: '推理服务 · 保存目标参数' },
  // SEAM-1 (#115). A declared write boundary, not a button: the authority is
  // Registry's `RegistryService.invoke` (row ACL, readiness, payload bound,
  // audit-before-forward). No control is wired to this path in this build, and
  // the write gate still ships an empty verified-contract list.
  { name: 'ops.invoke', method: 'POST', path: '/api/v2/ops/endpoints/{endpoint_id}/invoke', auth: 'session', kind: 'write', scope: 'registry:endpoint:invoke', owner: 'Integration#49', ui: '模型与对话 · 同源调用（未启用）' },
  { name: 'portal.infra_summary', method: 'GET', path: '/api/infra/summary', auth: 'session', kind: 'read', scope: 'observability:host:read', owner: 'Data#47', ui: '概览 / 算力中心 · 主机投影' },
  { name: 'atlas.calls', method: 'GET', path: '/api/atlas/calls', auth: 'session', kind: 'read', scope: 'atlas:calls:self:read', owner: 'Data#47', ui: '我的调用 / 图谱' },
  { name: 'evaluation.report_sources', method: 'GET', path: '/api/evaluation/report-sources', auth: 'session', kind: 'read', scope: 'evaluation:read', owner: 'Data#47', ui: '评测 · 报告来源' },
  { name: 'evaluation.reports', method: 'GET', path: '/api/evaluation/reports', auth: 'session', kind: 'read', scope: 'evaluation:read', owner: 'Data#47', ui: '评测 · 报告目录' },
  { name: 'evaluation.report_download', method: 'GET', path: '/api/evaluation/reports/{report_ref}/download', auth: 'session', kind: 'read', scope: 'evaluation:read', owner: 'Data#47', ui: '评测 · 受限报告下载' },
  { name: 'evaluation.report_detail', method: 'GET', path: '/api/evaluation/reports/{report_ref}', auth: 'session', kind: 'read', scope: 'evaluation:read', owner: 'Data#47', ui: '评测 · 单份报告' },
  { name: 'portal.services', method: 'GET', path: '/api/portal/services', auth: 'session', kind: 'read', scope: 'observability:host:read', owner: 'Data#47', ui: '概览 · 服务健康' },
  { name: 'portal.events', method: 'GET', path: '/api/portal/events', auth: 'session', kind: 'read', scope: 'portal:admin:read', owner: 'Integration#49', ui: '管理 · 审计时间线' },
  { name: 'portal.capabilities', method: 'GET', path: '/api/portal/capabilities', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '模块入口 · 能力状态' },
  // Plugin page jump addresses (2026-10-21): each plugin may carry a native-UI URL; admins set it.
  { name: 'portal.integrations', method: 'GET', path: '/api/portal/integrations', auth: 'session', kind: 'read', scope: null, owner: 'Integration#49', ui: '插件 · 跳转入口' },
  { name: 'portal.integrations.update', method: 'PUT', path: '/api/portal/integrations/{key}', auth: 'session', kind: 'write', scope: 'portal:admin:read', owner: 'Integration#49', ui: '插件 · 设置跳转地址（管理员）' },
  { name: 'auth.linux.login', method: 'POST', path: '/auth/linux/login', auth: 'anonymous', kind: 'write', scope: null, owner: 'IAM#45', ui: '登录（PAM 未实现时 503）' },
  { name: 'auth.linux.status', method: 'GET', path: '/auth/linux/status', auth: 'anonymous', kind: 'read', scope: null, owner: 'IAM#45', ui: '登录入口状态' },
  { name: 'auth.admin.status', method: 'GET', path: '/auth/admin/status', auth: 'anonymous', kind: 'read', scope: null, owner: 'IAM#45', ui: '管理 · 管理员模式' },
  { name: 'auth.admin.enter', method: 'POST', path: '/auth/admin/enter', auth: 'session', kind: 'write', scope: null, owner: 'IAM#45', ui: '管理 · 进入管理员模式' },
  { name: 'auth.admin.exit', method: 'POST', path: '/auth/admin/exit', auth: 'session', kind: 'write', scope: null, owner: 'IAM#45', ui: '管理 · 退出管理员模式' },
  { name: 'auth.logout', method: 'POST', path: '/auth/logout', auth: 'session', kind: 'write', scope: null, owner: 'IAM#45', ui: '账户 · 登出' },
]);

/**
 * The rows a stacked checkout cannot yet see in its own `route_map.py`. On this
 * branch that is `ops.invoke` alone: it is declared by BFF SEAM-1 (PR #115),
 * which is not in this PR's base. They are the only rows allowed to be pending;
 * once the BFF PR is in the tree the test requires the two tables to be
 * identical.
 */
export const BFF_PENDING_ROWS = Object.freeze([
  'ops.invoke',
]);

/** Routes the UI calls that the BFF route map does not declare. */
export const OTHER_ROUTES = Object.freeze([
  { method: 'GET', path: '/auth/tokens', server: 'backend/auth（经 BFF /auth/ 委派）', ui: 'API 管理 · Scoped Token（只读）', status: '已实现 · 浏览器只读' },
  { method: 'POST', path: '/auth/tokens · /revoke · /rotate', server: 'backend/auth（经 BFF /auth/ 委派）', ui: 'API 管理 · 签发/轮换/吊销', status: '前端禁用 · 待会话端到端验证' },
  { method: 'GET', path: '/api/atlas/modules', server: '仅开发预览服务 (dev/preview_auth.py)', ui: '模块入口面板', status: 'BFF 未声明' },
  { method: 'GET', path: '/api/v2/registry/*', server: '—', ui: 'Registry 直读', status: '未实现' },
]);

export function renderRoutes() {
  const bff = BFF_ROUTES.map((r) => (
    `<tr data-route="${escapeHtml(r.name)}"><th scope="row"><span class="mono">${escapeHtml(r.method)} ${escapeHtml(r.path)}</span>` +
    `<span class="tiny mono">${escapeHtml(r.name)}</span></th>` +
    `<td>${r.auth === 'anonymous' ? '匿名' : '会话'}</td>` +
    `<td>${r.kind === 'write' ? '<span class="badge warn">写 · CSRF</span>' : '<span class="badge">读</span>'}</td>` +
    `<td class="mono">${escapeHtml(r.scope ?? '—')}</td><td>${escapeHtml(r.owner)}</td><td>${escapeHtml(r.ui)}</td></tr>`
  )).join('');
  const other = OTHER_ROUTES.map((r) => (
    `<tr><th scope="row"><span class="mono">${escapeHtml(r.method)} ${escapeHtml(r.path)}</span></th>` +
    `<td>${escapeHtml(r.server)}</td><td>${escapeHtml(r.ui)}</td><td><span class="ws-state" data-state="not_configured">${escapeHtml(r.status)}</span></td></tr>`
  )).join('');
  return (
    `<div class="tablewrap"><table class="ws-table" data-routes="bff"><caption class="sr-only">BFF 路由表</caption><thead><tr>` +
    `<th scope="col">路由（backend/bff/route_map.py）</th><th scope="col">身份</th><th scope="col">读 / 写</th><th scope="col">主门槛 scope</th><th scope="col">归属</th><th scope="col">前端使用处</th>` +
    `</tr></thead><tbody>${bff}</tbody></table></div>` +
    `<h3 class="section-h" style="font-size:15px;margin:22px 0 8px">BFF 路由表之外、前端仍依赖的路径</h3>` +
    `<div class="tablewrap"><table class="ws-table" data-routes="other"><thead><tr>` +
    `<th scope="col">路径</th><th scope="col">实际提供方</th><th scope="col">前端使用处</th><th scope="col">状态</th>` +
    `</tr></thead><tbody>${other}</tbody></table></div>` +
    `<p class="footnote">行级权限（谁能看见哪个部署 / Endpoint）只在 Registry 的 AccessPolicy 中判定；本表不是第二套 ACL。管理员模式是会话内限时提权，不等于 Linux root。</p>`
  );
}

export function mountRoutes({ doc = globalThis.document } = {}) {
  const view = doc?.getElementById?.('view-admin');
  if (!view) return null;
  const section = doc.createElement('section');
  section.className = 'page-block ws-panel';
  section.dataset.ws = 'routes';
  section.setAttribute('aria-labelledby', 'ws-routes-title');
  section.innerHTML =
    '<div class="group-title"><h2 class="section-h" id="ws-routes-title">集成与路由矩阵 <em class="heading-en">/ SAME-ORIGIN CONTRACTS</em></h2>' +
    '<span class="smallmeta">DECLARED ≠ DEPLOYED</span></div>' +
    '<p class="ws-lede">前端只调用同源路径，绝不直连 :8080 / :8200 / :9000。下表列出 BFF 已声明的路由与它们的门槛，以及前端仍依赖、但生产 BFF 尚未声明的路径——后者就是上线前必须补齐的接线清单。</p>' +
    renderRoutes();
  view.appendChild(section);
  return { section };
}
