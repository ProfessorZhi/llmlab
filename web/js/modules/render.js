/*! web/js/modules/render.js — the Portal module panels, as Atelier 05 markup.
 *
 *  Pure `(portal, moduleId) -> html string` functions, for the same reason
 *  `../calls/render-log.js` is: this build has no Chrome, so a renderer that
 *  writes into a live DOM could not be tested at all here.
 *
 *  Every element below is an Atelier 05 component — `.module-card`,
 *  `.state-pill`, `.module-kv`, `.badge`, `.guard`, `.tablewrap`, `.surface`.
 *  The only additions are the ones `prototypes/v6/additions.css` declares, and
 *  they reuse the 05 tokens so both themes keep working.
 */

import { escapeHtml } from '../dom.js';
import { num, UNKNOWN } from '../format.js';
import {
  MODULE_STATES,
  PORTAL_STATES,
  VISIBILITY,
  isBlockedLink,
  isDemo,
  moduleById,
  reasonLabel,
  requirements,
  stateFor,
} from './contract.js';

const PILL_CLASS = Object.freeze({
  [MODULE_STATES.AVAILABLE]: 'state-available',
  [MODULE_STATES.UNAVAILABLE]: 'state-unavailable',
  [MODULE_STATES.DISABLED]: 'state-disabled',
  [MODULE_STATES.UNKNOWN]: 'state-unknown',
});

const STATE_TEXT = Object.freeze({
  [MODULE_STATES.AVAILABLE]: 'AVAILABLE',
  [MODULE_STATES.UNAVAILABLE]: 'UNAVAILABLE',
  [MODULE_STATES.DISABLED]: 'DISABLED',
  [MODULE_STATES.UNKNOWN]: 'UNKNOWN',
});

const show = (value) =>
  value === null || value === undefined || value === '' ? UNKNOWN : escapeHtml(String(value));

const pill = (state) =>
  `<span class="state-pill ${PILL_CLASS[state] ?? 'state-unknown'}">` +
  `${escapeHtml(STATE_TEXT[state] ?? 'UNKNOWN')}</span>`;

/** A metric with a number, or an honest UNKNOWN that says why. */
function renderMetric(metric) {
  const unknown = metric.value === null || metric.value === undefined;
  const shown = unknown ? UNKNOWN : `${num(metric.value, 0)} ${escapeHtml(metric.unit ?? '')}`.trim();
  return (
    `<div class="module-kv"><dt>${escapeHtml(metric.label ?? metric.name)}</dt>` +
    `<dd class="${unknown ? 'is-unknown' : ''}">${shown}` +
    (unknown
      ? `<br><span class="sect-sub">${escapeHtml(reasonLabel(metric.reason))}</span>`
      : `<br><span class="sect-sub">来源 ${escapeHtml(metric.source)}</span>`) +
    `</dd></div>`
  );
}

/** The readiness checklist. This is the most useful thing on an unavailable
 *  module: it says exactly which precondition is missing. */
function renderRequirements(module, deployment, scopes) {
  const rows = requirements(module, deployment, scopes);
  if (!rows.length) return '';
  const items = rows
    .map(
      (row) =>
        `<div class="module-kv"><dt>${escapeHtml(row.label)}</dt>` +
        `<dd class="${row.met ? '' : 'is-unknown'}">${row.met ? '满足' : '未满足'}</dd></div>`
    )
    .join('');
  return `<div class="module-details">${items}</div>`;
}

function renderData(data) {
  const entries = Object.entries(data ?? {});
  if (!entries.length) return '';
  const rows = entries
    .map(([key, value]) => {
      const shown = Array.isArray(value)
        ? value.length
          ? value.map((item) => `<span class="tag">${escapeHtml(String(item))}</span>`).join(' ')
          : UNKNOWN
        : value === null || value === undefined
          ? UNKNOWN
          : escapeHtml(String(value));
      return `<div class="module-kv"><dt>${escapeHtml(key)}</dt><dd>${shown}</dd></div>`;
    })
    .join('');
  return `<div class="module-details">${rows}</div>`;
}

/** Links, or a blocked entry that names what it would need. */
function renderLinks(module) {
  const links = module.links ?? [];
  if (!links.length) return '';
  const parts = links.map((link) => {
    if (isBlockedLink(link)) {
      // A blocked entry has no address on purpose: it explains, it does not fail.
      return (
        `<span class="secondary unavailable" role="link" aria-disabled="true" ` +
        `title="${escapeHtml(reasonLabel(link.blocked))}">${escapeHtml(link.label)}</span>`
      );
    }
    return (
      `<a class="secondary" href="${escapeHtml(link.href)}" rel="noreferrer">` +
      `${escapeHtml(link.label)} ↗</a>`
    );
  });
  const blockedNote = links
    .filter(isBlockedLink)
    .map((link) => `${escapeHtml(link.label)}：需要 ${escapeHtml(link.requires)}`)
    .join('；');
  return (
    `<div class="module-links">${parts.join('')}` +
    (blockedNote ? `<span class="requires">${blockedNote}</span>` : '') +
    `</div>`
  );
}

/** One module as a card. */
export function renderModule(module, { deployment, scopes } = {}) {
  if (!module) return '';
  const reason = module.reason
    ? `<p class="module-card-sub">${escapeHtml(reasonLabel(module.reason))}` +
      `<br><span class="sect-sub">${escapeHtml(module.reason)}</span></p>`
    : `<p class="module-card-sub">${escapeHtml(module.detail ?? '')}</p>`;
  const metrics = (module.metrics ?? []).map(renderMetric).join('');
  return (
    `<article class="module-card" data-module-card="${escapeHtml(module.id)}">` +
    `<h3>${escapeHtml(module.title ?? module.id)}</h3>` +
    `${pill(module.state)}` +
    reason +
    (metrics ? `<div class="module-details">${metrics}</div>` : '') +
    renderData(module.data) +
    renderRequirements(module, deployment, scopes) +
    renderLinks(module) +
    `</article>`
  );
}

/** The body of one `[data-module]` panel. */
export function renderModulePanel(portal, moduleId) {
  const module = moduleById(portal, moduleId);
  if (!module) {
    return (
      `<p class="module-empty">服务端没有返回这个模块。这不是「无数据」，而是契约不一致 —— ` +
      `请刷新页面；如果仍然如此，说明前后端版本不匹配。</p>`
    );
  }
  return renderModule(module, {
    deployment: portal.deployment,
    scopes: new Set(portal.scope?.scopes ?? []),
  });
}

/** The state text for the panel header, so the collapsed line is informative
 *  before the card is read. */
export function panelStateText(portal, moduleId) {
  const module = moduleById(portal, moduleId);
  if (!module) return '未返回';
  return STATE_TEXT[module.state] ?? 'UNKNOWN';
}

/** The private/shared toggle plus the endpoint table.
 *
 *  The control is a **view selector**, not a permission switch: both modes are
 *  subsets the server already computed, and `can_publish_shared` (a separate
 *  fact) is what publishing would need. It is rendered read-only in the sense
 *  that nothing here can widen either subset. */
export function renderEndpoints(portal) {
  const block = portal?.endpoints;
  if (!block) return '';
  const items = block.items ?? [];
  const seg = (mode, label) =>
    `<span aria-current="${block.visibility === mode}">${escapeHtml(label)}</span>`;
  const rows = items.length
    ? items
        .map(
          (item) =>
            `<tr><td><span class="rowtitle">${escapeHtml(item.name)}</span>` +
            `<span class="rowmuted mono">${escapeHtml(item.endpoint_id)}</span></td>` +
            `<td>${escapeHtml(item.project)}</td>` +
            `<td>${escapeHtml(item.protocol)}</td>` +
            `<td>${escapeHtml(item.model)}</td>` +
            `<td><span class="badge ${item.owner === 'self' ? 'good' : ''}">` +
            `${item.owner === 'self' ? '私有' : '共享'}</span></td></tr>`
        )
        .join('')
    : `<tr><td colspan="5" class="muted" style="text-align:center;padding:30px 12px">` +
      `当前可见范围内没有 Endpoint。</td></tr>`;
  return (
    `<div class="headrow" style="margin-top:6px">` +
    `<div class="seg-readonly" role="group" aria-label="Endpoint 可见性（服务端判定）">` +
    seg(VISIBILITY.PRIVATE, 'Private · 仅我') +
    seg(VISIBILITY.SHARED, 'Shared · 含已授权项目') +
    `</div>` +
    `<div class="module-links">` +
    `<button type="button" class="secondary" data-visibility="private" ` +
    `aria-pressed="${block.visibility === VISIBILITY.PRIVATE}">仅看我自己的</button>` +
    `<button type="button" class="secondary" data-visibility="shared" ` +
    `aria-pressed="${block.visibility === VISIBILITY.SHARED}">含共享</button>` +
    `</div></div>` +
    `<div class="tablewrap" style="margin-top:14px"><table style="min-width:640px">` +
    `<thead><tr><th>Endpoint</th><th>项目</th><th>协议</th><th>模型</th><th>归属</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div>` +
    `<div class="guard">发布为 Shared 需要 <b class="mono">portal:endpoints:share</b>` +
    ` —— 本会话${block.can_publish_shared ? '<b>已</b>具备' : '尚<b>未</b>具备'}。` +
    `共享可见性由运维在服务端标记，客户端只在这两个已算好的子集之间切换。</div>`
  );
}

/** What the playground panel additionally says: the interactive backend is a
 *  separate authorisation, so "the gateway exists" is not "you may run here". */
export function renderPlaygroundNote() {
  return (
    `<div class="guard warn">合成输出不是「试跑」：在没有经授权、受配额与审计约束的` +
    `交互式后端之前，这里不提供发送按钮，也不返回任何看起来像模型回复的内容。</div>`
  );
}

const NOTICES = Object.freeze({
  [PORTAL_STATES.SIGNED_OUT]: {
    title: '请先登录',
    body: '会话已失效或已注销（401）。模块状态与你的可见范围都需要一个有效的会话。',
    action: '<a class="primary" href="/login?next=%2Fatlas">去登录</a>',
  },
  [PORTAL_STATES.DENIED]: {
    title: '未获得模块目录授权',
    body:
      '你的身份已通过校验，但没有本页的授权（403）。请联系管理员为你的账号开通 ' +
      'portal:modules:read —— 它只允许你看到「有哪些模块、每个为什么不能用」，不打开任何入口。',
    action: '',
  },
  [PORTAL_STATES.NOT_CONFIGURED]: {
    title: '本部署未提供模块状态路由',
    body: '同源路径 /api/atlas/modules 在这台服务器上不存在（404/501）：模块目录尚未接线到 BFF。这不是「没有模块」，也不代表任何服务已停止。',
    action: '',
  },
  [PORTAL_STATES.UNKNOWN]: {
    title: '暂时无法确定',
    body: '没有取到可用的模块状态，也没有得到明确答复，因此这里不显示任何状态或数字。',
    action: '<button type="button" class="secondary" data-modules-retry="1">重试</button>',
  },
});

export function renderNotice(state, reason) {
  const notice = NOTICES[state] ?? NOTICES[PORTAL_STATES.UNKNOWN];
  const detail = reason
    ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>`
    : '';
  return (
    `<div class="surface">` +
    `<span class="caps">${escapeHtml(String(state).toUpperCase())}</span>` +
    `<h3 style="margin:11px 0 5px;font-size:15px">${escapeHtml(notice.title)}</h3>` +
    `<p class="module-empty">${escapeHtml(notice.body)}</p>${detail}` +
    (notice.action ? `<div class="module-links">${notice.action}</div>` : '') +
    `</div>`
  );
}

/** The DEMO strip for the whole module layer. */
export function renderTransportStrip(portal) {
  if (!portal) return '';
  if (!isDemo(portal)) {
    return (
      `<div class="demo-strip" role="status" style="border-left-color:var(--good)">` +
      `<span><strong>LIVE</strong> · 部署事实来自已接线的服务。</span></div>`
    );
  }
  const engine = portal.deployment?.engine ?? null;
  return (
    `<div class="demo-strip" role="status">` +
    `<span><strong>DEMO</strong> · 未接入真实 Gateway / NVML / 大盘：` +
    `模块状态与入口都是本构建的<b>如实</b>状态，不是生产实况。` +
    `引擎 ${engine === null ? 'UNKNOWN' : escapeHtml(String(engine))}。</span>` +
    `<span class="mono">FAIL-CLOSED</span></div>`
  );
}

export { stateFor };
