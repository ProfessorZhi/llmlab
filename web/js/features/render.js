/*! web/js/features/render.js — the ops panels, as Atelier 06 markup.
 *
 *  Every function is `(ops, capabilities, options) -> html string`. Pure, for
 *  the same reason `../calls/render-log.js` and `../modules/render.js` are: a
 *  renderer that wrote into a live DOM could not be tested at all in this
 *  build, which has no browser. So the *content* of every management panel is
 *  asserted here, in Node, and `tests/browser/ops-ui.browser.test.js` only has
 *  to prove the wiring.
 *
 *  **The visual contract.** Not one CSS class is introduced by this layer. The
 *  cards are `.module-card` inside `.module-grid`, the rows are `.module-kv`,
 *  the frames are `.page-block.module-panel` + `.editorial-index`, the caveats
 *  are `.guard`, the tables are `.tablewrap`. Those arrive from
 *  `prototypes/v6/additions.css`, which is derived from the frozen 05 authority,
 *  which is why `npm run verify:extract` still passes byte-for-byte. The single
 *  exception is the trend chart, and it carries its colours as inline
 *  `var(--…)` references to the existing tokens rather than as new rules.
 *
 *  Three rendering rules are load-bearing:
 *
 *  * **A missing value is `—`, never `0`,** and it says why.
 *  * **A value always shows its source and its time.** "1,204 requests" with no
 *    provenance is a rumour; "1,204 · gateway-usage-export · 12:04Z" is a
 *    reading.
 *  * **A control that cannot act renders as a disabled button with the reason
 *    in its `title` and in text next to it** — never as a button that looks
 *    live and does nothing.
 */

import { escapeHtml } from '../dom.js';
import { fmt, num, UNKNOWN } from '../format.js';
import { isGranted, capabilityReason } from './capability.js';
import { renderEventsSection } from './events.js';
import {
  EVAL_STATUS,
  GPU_METRIC_SOURCE,
  HEALTH,
  OPS_STATES,
  RUNTIME_STATUS,
  SHARE_MODES,
  effectiveShare,
  isDemo,
  reasonLabel,
} from './contract.js';
import { coverage, seriesExtent, seriesSegments } from './trend.js';

const show = (value) =>
  value === null || value === undefined || value === ''
    ? UNKNOWN
    : escapeHtml(String(value));

const yesno = (value) => (value ? '是' : '否');

/** A counted quantity, grouped in the reader's locale (`1,204`, not `1204`).
 *  Scores and percentages keep fixed decimals instead — see `metricValue`. */
const qty = (value) => fmt(Math.round(value));

/**
 * A segmented control built from the shell's own `.seg` component.
 *
 * This is not decoration. `aria-pressed` is correct for accessibility but the
 * stylesheet gives it **no** visual treatment — there is no `[aria-pressed]`
 * rule anywhere in `web/css/`. A toggle marked only with `aria-pressed` would
 * therefore look identical in every state, and "which share scope am I in?"
 * would be unanswerable from the screen. `.seg button.active` is the shell's
 * styled toggle, so that is what marks the current option.
 *
 * A disabled option gets `disabled` (the global `:disabled` rule) plus
 * `.unavailable`, so "cannot act" does not look like "not selected".
 */
function segGroup({ label, options }) {
  const buttons = options.map((option) => {
    const attrs = [
      'type="button"',
      option.active ? 'class="active"' : (option.disabled ? 'class="unavailable"' : ''),
      option.attrs ?? '',
      `aria-pressed="${option.active}"`,
      option.disabled ? 'disabled' : '',
      option.disabled && option.reason ? `title="${escapeHtml(option.reason)}"` : '',
    ].filter(Boolean).join(' ');
    return `<button ${attrs}>${escapeHtml(option.label)}</button>`;
  }).join('');
  return `<div class="seg" role="group" aria-label="${escapeHtml(label)}">${buttons}</div>`;
}

/** A metric value with its provenance. Missing ⇒ `—` plus the reason. */
function metricValue(metric) {
  const missing = metric.value === null || metric.value === undefined;
  if (missing) {
    return `<span class="is-unknown">${UNKNOWN}</span>` +
      `<br><span class="sect-sub">${escapeHtml(reasonLabel(metric.reason))}</span>`;
  }
  const shown = `${metric.unit === '%' ? num(metric.value, 1) : qty(metric.value)} ${escapeHtml(metric.unit ?? '')}`.trim();
  const source = escapeHtml(metric.source ?? '');
  const at = escapeHtml(metric.updated_at ?? '');
  return `${shown}<br><span class="sect-sub">来源 ${source} · 更新 ${at}</span>`;
}

const HEALTH_CLASS = Object.freeze({
  [HEALTH.HEALTHY]: 'state-available',
  [HEALTH.DEGRADED]: 'state-unavailable',
  [HEALTH.UNREACHABLE]: 'state-unknown',
  [HEALTH.UNKNOWN]: 'state-disabled',
});

const HEALTH_TEXT = Object.freeze({
  [HEALTH.HEALTHY]: 'HEALTHY',
  [HEALTH.DEGRADED]: 'DEGRADED',
  [HEALTH.UNREACHABLE]: 'UNREACHABLE',
  [HEALTH.UNKNOWN]: 'UNKNOWN',
});

const RUNTIME_TEXT = Object.freeze({
  [RUNTIME_STATUS.RUNNING]: '运行中',
  [RUNTIME_STATUS.STOPPED]: '已停止',
  [RUNTIME_STATUS.STARTING]: '启动中',
  [RUNTIME_STATUS.UNKNOWN]: '未确定',
});

export const healthPill = (health) =>
  `<span class="state-pill ${HEALTH_CLASS[health] ?? 'state-disabled'}">` +
  `${escapeHtml(HEALTH_TEXT[health] ?? 'UNKNOWN')}</span>`;

/** Links, or a blocked entry that names what it would need. Same shape as the
 *  module panels: a blocked link carries no address, on purpose. */
function renderLinks(links, { empty = '没有已授权的入口' } = {}) {
  if (!links?.length) return `<span class="requires">${escapeHtml(empty)}</span>`;
  return links.map((link) => {
    if (!link.href) {
      return `<span class="secondary unavailable" role="link" aria-disabled="true" ` +
        `title="${escapeHtml(link.requires ?? '')}">${escapeHtml(link.label)}</span>`;
    }
    return `<a class="secondary" href="${escapeHtml(link.href)}" rel="noreferrer">` +
      `${escapeHtml(link.label)} ↗</a>`;
  }).join('');
}

/**
 * The share control for one deployment **you own**.
 *
 * `private` / `selected` / `all`, defaulting to private. This is an *action*,
 * not a view: it is gated on `portal:endpoints:share`, the server re-checks
 * ownership, and every mode is revocable — going back to private clears the
 * principal list. When the capability is absent, or the source cannot accept
 * writes, the buttons render disabled and say which of the two it is.
 */
export function renderShareControl(deployment, capabilities, { writable = true } = {}) {
  const share = effectiveShare(deployment);
  const id = escapeHtml(deployment.deployment_id);
  const canShare = isGranted(capabilities, 'shareEndpoints');
  const enabled = canShare && writable;
  const disabledReason = !canShare
    ? capabilityReason(capabilities, 'shareEndpoints')
    : (writable ? '' : '当前数据源是只读的：没有可接受写入的服务端');

  const principalText = share.principals.length
    ? share.principals.map((p) => escapeHtml(p)).join('、')
    : UNKNOWN;

  return (
    `<div class="module-details">` +
    `<div class="module-kv"><dt>共享范围</dt><dd>` +
    `<b>${escapeHtml(shareLabel(share.mode))}</b></dd></div>` +
    `<div class="module-kv"><dt>可见的对象</dt><dd>${principalText}</dd></div>` +
    `<div class="module-kv"><dt>可撤销</dt><dd>${yesno(share.revocable)}</dd></div>` +
    `</div>` +
    segGroup({
      label: '共享范围',
      options: [
        { label: 'Private · 仅我', mode: SHARE_MODES.PRIVATE },
        { label: 'Selected · 指定账号', mode: SHARE_MODES.SELECTED },
        { label: 'All · 全部模型用户', mode: SHARE_MODES.ALL },
      ].map(({ label, mode }) => ({
        label,
        active: share.mode === mode,
        disabled: !enabled,
        reason: disabledReason,
        attrs: `data-share-mode="${mode}" data-deployment="${id}"`,
      })),
    }) +
    (enabled
      ? `<div class="module-links">` +
        `<label class="field"><span class="tiny">指定账号（逗号分隔，Selected 时必填）</span>` +
        `<input type="text" data-share-principals="${id}" data-deployment="${id}" ` +
        `placeholder="demo-user-b, demo-user-c" value="${escapeHtml(share.principals.join(', '))}"></label>` +
        `</div>`
      : '') +
    (disabledReason
      ? `<div class="guard warn">共享控制当前不可用：<span class="mono">${escapeHtml(disabledReason)}</span>` +
        ` —— 没有授权就不显示成可点的按钮。</div>`
      : `<div class="guard">共享范围由服务端按所有权重新校验；切回 Private 会清空指定账号，` +
        `因此任何一次发布都是可撤销的。</div>`)
  );
}

export const shareLabel = (mode) => ({
  [SHARE_MODES.PRIVATE]: 'Private · 仅自己',
  [SHARE_MODES.SELECTED]: 'Selected · 指定账号',
  [SHARE_MODES.ALL]: 'All · 全部模型用户',
}[mode] ?? '未声明（按 Private 处理）');

/** One owned deployment, as a `.module-card`. */
export function renderDeploymentCard(deployment, capabilities, options = {}) {
  const res = deployment.resources ?? {};
  const vram = res.vram_total_mib === null || res.vram_total_mib === undefined
    ? UNKNOWN
    : `${qty(res.vram_total_mib)} MiB`;
  const provenance = res.source
    ? `来源 ${escapeHtml(res.source)} · 更新 ${escapeHtml(res.updated_at ?? UNKNOWN)}`
    : `${escapeHtml(reasonLabel('no_verified_metric_source'))}`;

  return (
    `<article class="module-card" data-deployment-card="${escapeHtml(deployment.deployment_id)}">` +
    `<h3>${escapeHtml(deployment.model_label ?? deployment.model_id)}</h3>` +
    healthPill(deployment.health) +
    `<p class="module-card-sub">` +
    `<span class="mono">${escapeHtml(deployment.model_id)}</span>` +
    (deployment.health_reason
      ? `<br><span class="sect-sub">健康未知原因：${escapeHtml(reasonLabel(deployment.health_reason))}</span>`
      : '') +
    `</p>` +
    `<div class="module-details">` +
    `<div class="module-kv"><dt>所有者 UID</dt><dd><span class="mono">${escapeHtml(deployment.owner_uid)}</span></dd></div>` +
    `<div class="module-kv"><dt>Engine type</dt><dd>${
      deployment.engine_type ? escapeHtml(deployment.engine_type) : UNKNOWN}</dd></div>` +
    `<div class="module-kv"><dt>运行状态</dt><dd>${escapeHtml(RUNTIME_TEXT[deployment.runtime_status] ?? '未确定')}` +
    (deployment.runtime_reason
      ? `<br><span class="sect-sub">${escapeHtml(reasonLabel(deployment.runtime_reason))}</span>`
      : '') + `</dd></div>` +
    `<div class="module-kv"><dt>GPU 卡数</dt><dd>${res.gpu_count === null || res.gpu_count === undefined
      ? `<span class="is-unknown">${UNKNOWN}</span>` : `${qty(res.gpu_count)}`}` +
    `<br><span class="sect-sub">${provenance}</span></dd></div>` +
    `<div class="module-kv"><dt>显存总量</dt><dd>${vram}</dd></div>` +
    `<div class="module-kv"><dt>已登记</dt><dd>${yesno(deployment.registered)}</dd></div>` +
    `</div>` +
    `<div class="module-links">` +
    `<button type="button" class="secondary" data-drawer="deployment" ` +
    `data-deployment="${escapeHtml(deployment.deployment_id)}">模型详情 / 登记管理 ↗</button>` +
    `</div>` +
    `<div class="module-links">${renderLinks(deployment.links, { empty: '没有已授权的入口' })}</div>` +
    renderShareControl(deployment, capabilities, options) +
    `</article>`
  );
}

/** Someone else's endpoint, shared to me. Read-only by construction: I cannot
 *  change another principal's share scope, and this build offers no control
 *  that pretends otherwise. */
export function renderSharedEndpoint(item) {
  const share = effectiveShare(item);
  return (
    `<div class="module-kv"><dt>${escapeHtml(item.name)}</dt><dd>` +
    `<span class="mono">${escapeHtml(item.endpoint_id)}</span><br>` +
    `<span class="sect-sub">所有者 <span class="mono">${escapeHtml(item.owner_uid)}</span> · ` +
    `${escapeHtml(item.protocol ?? UNKNOWN)} · ${escapeHtml(item.model ?? UNKNOWN)} · ` +
    `${escapeHtml(item.project ?? UNKNOWN)}</span><br>` +
    `<span class="sect-sub">由 <span class="mono">${escapeHtml(item.granted_by ?? UNKNOWN)}</span> 授权 · ` +
    `${escapeHtml(shareLabel(share.mode))}</span></dd></div>`
  );
}

/**
 * The 模型与引擎 panel: **我拥有的部署** and **别人共享给我的 Endpoint**, kept
 * visibly apart, because "a model I can call" and "a model I am responsible
 * for" are different relationships and merging them is how an owner gets
 * blamed for someone else's downtime.
 */
export function renderRegistry(ops, capabilities, {
  visibility = 'private', error = null, writable = false,
} = {}) {
  const registry = ops.registry ?? { owned: [], shared: [] };
  const owned = registry.owned ?? [];
  const shared = registry.shared ?? [];

  const ownedCards = owned.length
    ? owned.map((dep) => renderDeploymentCard(dep, capabilities, { writable })).join('')
    : `<p class="module-empty">这个会话名下没有任何已登记的部署。空列表不是「全部不可用」，而是「没有登记」。</p>`;

  const sharedBlock = visibility === 'shared'
    ? (shared.length
      ? `<div class="module-details">${shared.map(renderSharedEndpoint).join('')}</div>`
      : `<p class="module-empty">已授权项目内没有别人共享给你的 Endpoint。</p>`)
    : `<p class="module-empty">当前是 Private 视图：只显示你自己的部署。切到「含共享」才会显示别人授权给你的 Endpoint。</p>`;

  return (
    (error
      ? `<div class="guard warn" role="alert">上一次共享操作没有被接受：` +
        `<span class="mono">${escapeHtml(error)}</span>。` +
        `界面显示的是服务端上一次给出的状态，不是乐观更新。</div>`
      : '') +
    `<div class="headrow">` +
    // The read-only display shows the *server's* decision: the two subsets it
    // already computed. The `.seg` beside it is the view switch between them,
    // and nothing here can widen either.
    `<div class="seg-readonly" role="group" aria-label="Endpoint 可见性（服务端判定）">` +
    `<span aria-current="${visibility === 'private'}">Private · 仅我</span>` +
    `<span aria-current="${visibility === 'shared'}">Shared · 含已授权项目</span>` +
    `</div>` +
    segGroup({
      label: '可见视图',
      options: [
        { label: '仅看我自己的', value: 'private' },
        { label: '含共享', value: 'shared' },
      ].map(({ label, value }) => ({
        label,
        active: visibility === value,
        disabled: false,
        attrs: `data-visibility="${value}"`,
      })),
    }) +
    `</div>` +
    `<div class="group-title" style="margin-top:22px">` +
    `<h3 class="section-h">我拥有的部署</h3>` +
    `<span class="smallmeta">OWNED · ${owned.length}</span></div>` +
    `<div class="module-grid">${ownedCards}</div>` +
    `<div class="group-title" style="margin-top:26px">` +
    `<h3 class="section-h">别人共享给我的 Endpoint</h3>` +
    `<span class="smallmeta">SHARED TO ME · ${visibility === 'shared' ? shared.length : '—'}</span></div>` +
    sharedBlock +
    renderAdminDeploymentView(ops, capabilities) +
    `<div class="guard">「我拥有的」与「共享给我的」是两个不同的关系：前者你要为可用性负责，` +
    `后者只有调用权。共享可见性由服务端按 principal × 项目判定，客户端只在这两个已算好的子集之间切换。</div>`
  );
}

/**
 * The deployment-wide view of share scopes — the **admin perspective**.
 *
 * It appears only when `portal:admin:read` is held, and it is the answer to
 * "who else's share scope can I see": for every deployment in the deployment,
 * its owner and its current scope. A session without the scope gets an
 * explanation instead of an empty table, because an empty table would read as
 * "there are none".
 */
export function renderAdminDeploymentView(ops, capabilities) {
  const rows = ops.registry?.all ?? [];
  if (!isGranted(capabilities, 'viewAllPrincipals')) {
    if (!rows.length) return '';
    // A payload carrying the deployment-wide rows anyway is a contract
    // violation and would already have been refused; if it somehow arrives,
    // do not render it.
    return '';
  }
  const body = rows.length
    ? `<div class="tablewrap"><table style="min-width:640px">` +
      `<thead><tr><th>Deployment</th><th>模型</th><th>所有者 UID</th><th>健康</th><th>共享范围</th></tr></thead>` +
      `<tbody>${rows.map((row) => (
        `<tr><td><span class="rowtitle mono">${escapeHtml(row.deployment_id)}</span></td>` +
        `<td>${escapeHtml(row.model_label ?? row.model_id)}</td>` +
        `<td><span class="mono">${escapeHtml(row.owner_uid)}</span>` +
        `${row.owner_uid === ops.scope?.subject ? ' <span class="badge good">自己</span>' : ''}</td>` +
        `<td>${healthPill(row.health)}</td>` +
        `<td>${escapeHtml(shareLabel(effectiveShare(row).mode))}</td></tr>`
      )).join('')}</tbody></table></div>`
    : `<p class="module-empty">服务端没有返回部署范围的部署行。</p>`;

  return (
    `<div class="group-title" style="margin-top:26px">` +
    `<h3 class="section-h">部署范围（admin 视角）</h3>` +
    `<span class="smallmeta">ALL DEPLOYMENTS · portal:admin:read</span></div>` +
    body +
    `<div class="guard">这一块只在服务端断言的 <span class="mono">portal:admin:read</span> 下出现，` +
    `并且显示的是<b>每个部署的共享范围</b>。它不是客户端可以打开或关闭的开关，` +
    `也不改变任何一次共享本身 —— 改共享仍然只能由所有者对自己名下的部署做。</div>`
  );
}

/** The 模型详情 / 登记管理 drawer body. Rendered into the shell's existing
 *  `<dialog id="explainDialog">` rather than a new overlay, so no new CSS and no
 *  second modal to keep in sync. */
export function renderDeploymentDrawer(deployment, capabilities, { writable = false } = {}) {
  const res = deployment.resources ?? {};
  const row = (label, value) =>
    `<div class="module-kv"><dt>${escapeHtml(label)}</dt><dd>${value}</dd></div>`;
  return (
    `<p class="module-empty">这是登记记录的服务端视图。下面每一项都来自受信目录，` +
    `客户端不能修改登记本身 —— 登记与部署是两次独立授权。</p>` +
    `<div class="module-details">` +
    row('Deployment ID', `<span class="mono">${escapeHtml(deployment.deployment_id)}</span>`) +
    row('模型', `${escapeHtml(deployment.model_label ?? deployment.model_id)}` +
      `<br><span class="sect-sub mono">${escapeHtml(deployment.model_id)}</span>`) +
    row('Engine type', deployment.engine_type ? escapeHtml(deployment.engine_type) : UNKNOWN) +
    row('所有者 UID', `<span class="mono">${escapeHtml(deployment.owner_uid)}</span>`) +
    row('健康', healthPill(deployment.health)) +
    row('运行状态', escapeHtml(RUNTIME_TEXT[deployment.runtime_status] ?? '未确定')) +
    row('GPU 卡数', res.gpu_count === null || res.gpu_count === undefined
      ? `<span class="is-unknown">${UNKNOWN}</span>` : qty(res.gpu_count)) +
    row('显存总量', res.vram_total_mib === null || res.vram_total_mib === undefined
      ? `<span class="is-unknown">${UNKNOWN}</span>` : `${qty(res.vram_total_mib)} MiB`) +
    row('显存占用', res.vram_used_mib === null || res.vram_used_mib === undefined
      ? `<span class="is-unknown">${UNKNOWN}</span>` : `${qty(res.vram_used_mib)} MiB`) +
    row('资源来源', res.source
      ? `${escapeHtml(res.source)}` +
        `<br><span class="sect-sub">更新 ${escapeHtml(res.updated_at ?? UNKNOWN)}</span>`
      : `<span class="is-unknown">${UNKNOWN}</span>` +
        `<br><span class="sect-sub">${escapeHtml(reasonLabel('no_verified_metric_source'))}</span>`) +
    row('入口', `<span class="module-links">${renderLinks(deployment.links, { empty: '没有已授权的入口' })}</span>`) +
    `</div>` +
    renderShareControl(deployment, capabilities, { writable }) +
    `<div class="guard warn">登记信息的写入路径尚未接线（<span class="mono">backend/registry</span> 独立开发中）。` +
    `本抽屉只读展示；在服务端确认之前，没有任何按钮能改动登记。</div>`
  );
}

/** 个人 API Keys. The prefix is re-checked as masked by the contract; nothing
 *  here can render a whole key, because a whole key never arrives. */
export function renderKeys(ops) {
  const keys = ops.keys ?? [];
  const rows = keys.length
    ? keys.map((key) => (
      `<tr><td><span class="rowtitle">${escapeHtml(key.name)}</span>` +
      `<span class="rowmuted mono">${escapeHtml(key.key_id)}</span></td>` +
      `<td><span class="key">${escapeHtml(key.prefix)}</span></td>` +
      `<td>${escapeHtml((key.scopes ?? []).join(' · ') || UNKNOWN)}</td>` +
      `<td><span class="badge ${key.status === 'active' ? 'good' : ''}">${escapeHtml(key.status)}</span></td>` +
      `<td>${key.last_used_at ? escapeHtml(key.last_used_at) : UNKNOWN}</td></tr>`
    )).join('')
    : `<tr><td colspan="5" class="muted" style="text-align:center;padding:30px 12px">` +
      `这个会话没有个人 Key。</td></tr>`;

  return (
    `<div class="tablewrap"><table style="min-width:720px">` +
    `<thead><tr><th>名称</th><th>前缀</th><th>Scope</th><th>状态</th><th>最近使用</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div>` +
    `<div class="guard">明文只在签发那一刻出现一次，服务端只保存摘要，` +
    `因此这一页能显示的只有掩码前缀。任何声称能在这里「查看 Key」的入口都是假的。</div>`
  );
}

/**
 * The restricted chat playground.
 *
 * The send button is disabled and there is no code path that could enable it in
 * this build: `playground.authorized` is a **deployment fact**, not a client
 * choice, and the preview has no authorized interactive backend. There is also
 * no hidden fallback — the disabled button issues no request, and nothing in
 * this layer knows an upstream address.
 */
export function renderPlayground(ops, capabilities) {
  const playground = ops.playground ?? { authorized: false, reason: 'no_authorized_backend' };
  const authorized = playground.authorized === true && playground.audited === true;
  const reason = authorized ? '' : reasonLabel(playground.reason) || '没有经授权的交互式后端';

  // "Models you can call" is not only "models you own": an endpoint someone
  // shared with you is callable too, and it is labelled as theirs so the reader
  // can tell the two relationships apart at the point of use.
  const callable = [
    ...(ops.registry?.owned ?? []).map((dep) => ({
      id: dep.deployment_id,
      label: `${dep.model_label ?? dep.model_id} · ${dep.model_id}`,
      owner: null,
    })),
    ...(ops.registry?.shared ?? []).map((item) => ({
      id: item.endpoint_id,
      label: `${item.name} · ${item.model ?? UNKNOWN}`,
      owner: item.owner_uid,
    })),
  ];
  const modelOptions = callable
    .map((entry) => `<option value="${escapeHtml(entry.id)}">` +
      `${escapeHtml(entry.label)}${entry.owner ? `（共享：${escapeHtml(entry.owner)}）` : ''}</option>`)
    .join('');

  return (
    `<div class="columns">` +
    `<div>` +
    `<div class="headrow" style="margin-bottom:15px">` +
    `<h3 class="section-h">受限试跑台</h3>` +
    `<div class="selector"><span class="tiny">目标模型</span>` +
    `<select aria-label="可调用模型" ${authorized ? '' : 'disabled'}>` +
    `${modelOptions || '<option>没有可调用的模型</option>'}</select></div></div>` +
    `<div class="chatbox"><div class="chatlog">` +
    `<div class="chatbubble bot">本构建没有经授权、受配额与审计约束的交互式推理后端，` +
    `因此这里不会返回任何看起来像模型回复的内容。</div>` +
    `</div><form class="chatcomposer">` +
    `<input aria-label="试跑输入" placeholder="没有授权后端：输入不发送" ` +
    `${authorized ? '' : 'disabled'}>` +
    `<button class="primary" type="button" data-playground-send="1" ` +
    `${authorized ? '' : 'disabled'} title="${escapeHtml(reason)}">发送 ↗</button>` +
    `</form></div></div>` +
    `<aside class="flatpanel"><h3>入口状态</h3>` +
    `<div class="statusrows">` +
    `<div class="statusitem"><div class="tiny">授权后端</div><strong>${authorized ? '已授权' : '未授权'}</strong></div>` +
    `<div class="statusitem"><div class="tiny">审计</div><strong>${authorized ? '开启' : '不适用'}</strong></div>` +
    `<div class="statusitem"><div class="tiny">上游直连</div><strong>不提供</strong></div>` +
    `<div class="statusitem"><div class="tiny">能力来源</div><strong>部署事实</strong></div>` +
    `</div>` +
    `<div class="guard warn">${escapeHtml(reason)}。合成输出不是「试跑」：` +
    `按钮在授权后端存在之前保持 disabled，也不会偷偷去命中 8080 那类上游端口。</div>` +
    `<div class="guard">当前数据源：${isDemo(ops) ? '合成 fixture' : '已接线服务'}。` +
    `本层的所有请求都只发往同源路径。</div>` +
    `</aside></div>`
  );
}

/** The request example. A placeholder address and a placeholder key — the
 *  `curl` here cannot be run against anything, which is the point. */
export function renderRequestExample(ops) {
  const first = (ops.registry?.owned ?? [])[0];
  const model = first?.model_id ?? 'demo-llm-a';
  return (
    `<div class="codearea">curl https://lab.example/v1/chat/completions \\
  -H 'Authorization: Bearer &lt;YOUR_SCOPED_KEY&gt;' \\
  -H 'Content-Type: application/json' \\
  -d '{"model":"${escapeHtml(model)}","messages":[]}'</div>` +
    `<div class="actions"><button class="secondary" type="button" data-copy-example="1">复制示例</button>` +
    `<span class="tiny">示例地址与 Key 占位符，不可直接调用</span></div>`
  );
}

// --- observability ----------------------------------------------------------

function renderTrendSeries(series, frame) {
  const segments = seriesSegments(series.points, frame);
  const measured = coverage(series.points);
  const total = series.points?.length ?? 0;
  if (!segments.length) return '';
  return (
    `<g data-trend-series="${escapeHtml(series.name)}">` +
    segments.map((points) =>
      `<polyline points="${points}" fill="none" stroke="var(--rust)" stroke-width="1.6" ` +
      `stroke-linecap="round" stroke-linejoin="round"></polyline>`).join('') +
    `</g>` +
    `<text x="${frame.width - frame.pad}" y="${frame.pad + 10}" font-size="10" ` +
    `text-anchor="end" fill="var(--dim)">${escapeHtml(series.name)} · ` +
    `${measured}/${total} 点有测量</text>`
  );
}

/** The trend chart. Gaps are gaps; a series with no measurements renders an
 *  empty state instead of a flat line along the axis. */
export function renderTrend(ops, { scope = 'mine' } = {}) {
  const trends = ops.trends;
  if (!trends || !trends.series?.length) {
    return `<p class="module-empty">没有可用的趋势序列。缺少测量不等于零：这里不会画一条 0 轴直线。</p>`;
  }
  const series = trends.series.filter((s) => s.scope === scope);
  const extent = seriesExtent(series);
  if (!series.length || !extent) {
    return `<p class="module-empty">${scope === 'global'
      ? '这个会话没有全局趋势序列（需要 portal:admin:read）。'
      : '这个会话没有可用的趋势序列。'}</p>`;
  }

  const frame = { width: 720, height: 220, pad: 14, min: extent.min, max: extent.max };
  const body = series.map((s) => renderTrendSeries(s, frame)).join('');
  const caption = trends.synthetic
    ? `SYNTHETIC · ${escapeHtml(trends.reason ?? '合成序列')}`
    : `来源 ${escapeHtml(trends.source ?? '')} · 更新 ${escapeHtml(trends.updated_at ?? '')}`;

  return (
    `<svg viewBox="0 0 ${frame.width} ${frame.height}" role="img" ` +
    `aria-label="调用 / Token / 延迟趋势图" data-trend-scope="${scope}">` +
    `<line x1="${frame.pad}" y1="${frame.height - frame.pad}" x2="${frame.width - frame.pad}" ` +
    `y2="${frame.height - frame.pad}" stroke="var(--line)" stroke-width="1"></line>` +
    body +
    `<text x="${frame.pad}" y="${frame.pad + 10}" font-size="10" fill="var(--dim)">` +
    `max ${qty(extent.max)}</text>` +
    `<text x="${frame.pad}" y="${frame.height - frame.pad - 4}" font-size="10" fill="var(--dim)">` +
    `min ${qty(extent.min)} · ${extent.known} 个测量点</text>` +
    `</svg>` +
    `<div class="legendline"><span class="mono">${caption}</span></div>`
  );
}

/**
 * 监控与日志: the metric cards (with source and time, UNKNOWN when missing) and
 * the trend view with its 我的 / 全局 scope switch.
 *
 * The global scope is gated on `portal:admin:read`. That gating is a
 * front-end-side proposal — see `docs/v6-ui-flows.md` §"集成清单" item 4 — and
 * it fails closed: without the scope the toggle is disabled and says which
 * scope is missing, rather than quietly showing everyone's numbers.
 */
export function renderObservability(ops, capabilities, { trendScope = 'mine' } = {}) {
  const metrics = ops.metrics?.items ?? [];
  const cards = metrics.map((metric) => (
    `<article class="module-card" data-metric="${escapeHtml(metric.name)}">` +
    `<h3>${escapeHtml(metric.label ?? metric.name)}</h3>` +
    `<span class="state-pill ${metric.value === null ? 'state-unknown' : 'state-available'}">` +
    `${metric.value === null ? 'UNKNOWN' : 'AVAILABLE'}</span>` +
    `<p class="module-card-sub mono">${escapeHtml(metric.name)}</p>` +
    `<div class="module-details"><div class="module-kv"><dt>当前值</dt><dd>${metricValue(metric)}</dd></div></div>` +
    (String(metric.name).startsWith('gpu_') && metric.value !== null && metric.source !== GPU_METRIC_SOURCE
      ? `<div class="guard warn">GPU 数值必须来自 ${GPU_METRIC_SOURCE}。</div>`
      : '') +
    `</article>`
  )).join('');

  const canGlobal = isGranted(capabilities, 'viewAllPrincipals');
  const globalReason = capabilityReason(capabilities, 'viewAllPrincipals');
  const scopeToggle = segGroup({
    label: '趋势范围',
    options: [
      { label: '我的', value: 'mine', disabled: false, reason: '' },
      { label: '全局', value: 'global', disabled: !canGlobal, reason: globalReason },
    ].map(({ label, value, disabled, reason }) => ({
      label,
      active: trendScope === value,
      disabled,
      reason,
      attrs: `data-trend-scope="${value}"`,
    })),
  });

  return (
    `<div class="group-title"><h3 class="section-h">指标卡</h3>` +
    `<span class="smallmeta">SOURCE + UPDATED_AT OR UNKNOWN</span></div>` +
    `<div class="module-grid" data-ops-metrics="1">` +
    (cards || `<p class="module-empty">服务端没有返回任何指标。</p>`) +
    `</div>` +
    (canGlobal ? '' :
      `<div class="guard">全局趋势需要一个更宽范围的授权：` +
      `<span class="mono">${escapeHtml(globalReason)}</span>。` +
      `在拿到之前，「全局」保持 disabled —— 而不是显示一份来路不明的汇总。</div>`) +
    `<div class="group-title" style="margin-top:26px"><h3 class="section-h">趋势</h3>` +
    `<span class="smallmeta">我的 / 全局</span></div>` +
    scopeToggle +
    `<div class="plot" data-ops-trend="${escapeHtml(trendScope)}">${renderTrend(ops, { scope: trendScope })}</div>` +
    `<div class="legendline"><span class="mono">窗口 ${escapeHtml(ops.trends?.window ?? ops.metrics?.window ?? UNKNOWN)}</span></div>` +
    `<div class="module-links">${renderLinks(ops.links, { empty: '没有已授权的原生大盘入口' })}</div>`
  );
}

// --- evaluation -------------------------------------------------------------

const SUBMIT_UNKNOWN = '评测后端状态未知';

/** The authenticated BFF transport is not yet proof of a real EvalScope report. */
export function evaluationDisplayState(ops = {}) {
  const t = ops.transport ?? {};
  if (ops.synthetic === true || t.synthetic === true) {
    return { kind: 'synthetic', label: 'SYNTHETIC · 演示记录',
      detail: '这是界面演示数据，不是 Dell 已完成的真实评测结果。' };
  }
  if (ops.synthetic === false && t.mode === 'live' && t.gateway_attached === true) {
    return { kind: 'bff', label: 'LIVE BFF · 报告待核验',
      detail: '数据来自认证的同源 BFF；EvalScope 报告的来源与权限仍需服务端逐条核对。' };
  }
  return { kind: 'unknown', label: 'UNKNOWN · 来源未确认',
    detail: '评测来源与当前会话的访问权限尚未确认，无法声明真实成绩。' };
}

/** Counts only server-scoped records in this session, not the global queue. */
export function evaluationVisibleCounts(runs = []) {
  const items = Array.isArray(runs) ? runs : [];
  return {
    visible: items.length,
    active: items.filter((r) => r.status === 'queued' || r.status === 'running').length,
    done: items.filter((r) => r.status === 'done').length,
    failed: items.filter((r) => r.status === 'failed').length,
  };
}


export function renderEvaluation(ops, capabilities) {
  const evaluation = ops.evaluation ?? { runs: [], submit: { ready: false, reason: 'evaluation_backend_not_ready' } };
  const submit = evaluation.submit ?? { ready: false, reason: 'evaluation_backend_not_ready' };
  const ready = submit.ready === true;
  const provenance = evaluationDisplayState(ops);
  const counts = evaluationVisibleCounts(evaluation.runs);
  const scopeText = isGranted(capabilities, 'viewAllPrincipals')
    ? '当前管理员有权查看的任务（含获授权的全局记录）'
    : '仅当前会话可见的任务';
  const summaryItems = [
    ['可见任务', counts.visible, 'visible'],
    ['排队 / 执行', counts.active, 'active'],
    ['已完成', counts.done, 'done'],
    ['失败', counts.failed, 'failed'],
  ];
  const summary = summaryItems.map(([label, value, kind]) =>
    '<div class="stat"><span>' + label + '</span>' +
    '<strong class="number" data-eval-count="' + kind + '">' + value + '</strong>' +
    '<small>' + (provenance.kind === 'synthetic' ? 'DEMO · 非实测' : '本会话可见') +
    '</small></div>').join('');

  const rows = (evaluation.runs ?? []).length
    ? evaluation.runs.map((run) => (
      `<tr><td><span class="rowtitle mono">${escapeHtml(run.run_id)}</span>` +
      `<span class="rowmuted">${run.scope === 'global' ? '全局' : '我的'}</span></td>` +
      `<td>${escapeHtml(run.model_id)}</td>` +
      `<td>${escapeHtml(run.dataset ?? UNKNOWN)}</td>` +
      `<td><span class="badge ${run.status === EVAL_STATUS.DONE ? 'good' : (run.status === EVAL_STATUS.FAILED ? 'warn' : '')}">` +
      `${escapeHtml(run.status)}</span></td>` +
      `<td>${run.metric === null || run.metric === undefined
        ? `<span class="is-unknown">${UNKNOWN}</span>`
        : `${num(run.metric, 3)} <span class="tiny">${escapeHtml(run.metric_unit ?? '')}</span>`}</td>` +
      `<td>${escapeHtml(run.updated_at ?? UNKNOWN)}</td></tr>`
    )).join('')
    : '<tr data-eval-empty="true"><td colspan="6" class="muted">' +
      '当前会话没有可见的评测任务。' +
      (provenance.kind === 'synthetic'
        ? '本预览不会产生生产评测报告。'
        : '这不意味着全平台没有报告，更不意味着模型得分为零。') +
      '</td></tr>';

  return (
    '<div class="headrow" data-eval-overview><div>' +
    '<h3 class="section-h">评测证据与运行记录</h3>' +
    '<p class="sect-sub">EVALUATION / EVIDENCE · ' + escapeHtml(scopeText) + '</p>' +
    '</div><span class="badge" data-eval-provenance="' + provenance.kind + '">' +
    escapeHtml(provenance.label) + '</span></div>' +
    '<p class="module-empty" data-eval-source-note>' + escapeHtml(provenance.detail) + '</p>' +
    '<div class="stats" aria-label="当前会话可见评测摘要">' + summary + '</div>' +
    '<div class="headrow" style="margin-bottom:14px">' +
    '<h3 class="section-h">评测运行记录</h3>' +
    `<button type="button" class="secondary" data-eval-submit="1" ${ready ? '' : 'disabled'} ` +
    `title="${escapeHtml(reasonLabel(submit.reason) || '评测后端未就绪')}">提交评测任务 ↗</button>` +
    `</div>` +
    `<div class="tablewrap"><table style="min-width:760px">` +
    `<caption class="tiny">当前会话获授权的评测记录及指标</caption>` +
    `<thead><tr><th scope="col">Run</th><th>模型</th><th>数据集</th><th>状态</th><th>指标</th><th>更新</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div>` +
    '<details class="flatpanel" data-eval-explainer><summary>如何判断这是正式评测数据？</summary>' +
    '<p>任务应由可信 BFF 按 Linux 主体及项目权限筛选；只有具备受信报告来源和时间戳的测量才能标记为正式结果。</p>' +
    '<p>没有数据表示本会话没有可见记录；不能据此推断其他用户没有任务，也不等于得分为 0。</p></details>' +
    (ready
      ? `<div class="guard">评测后端已就绪；提交后会进入隔离队列，不抢占现有推理服务。</div>`
      : `<div class="guard warn">提交按钮保持 disabled：<span class="mono">${
        escapeHtml(reasonLabel(submit.reason) || SUBMIT_UNKNOWN)}</span>。` +
        `在 <span class="mono">backend/evaluation</span> 接线并通过授权之前，这里不提供提交入口，` +
        `也不预置任何看起来像评测得分的数字。</div>`) +
    `<div class="module-links">${renderLinks(
      (ops.links ?? []).filter((link) => link.kind === 'evaluation'),
      { empty: '没有已授权的 EvalScope 入口' },
    )}</div>`
  );
}

// --- administration ---------------------------------------------------------

/** 系统管理. Everything here is a consequence of the *scope* the server
 *  asserted. There is no branch anywhere in this file on a name, and
 *  `tests/web-features/capability.test.js` reads the sources to prove it. */
export function renderAdmin(ops, capabilities, { visibility = 'private', events = null } = {}) {
  const admin = ops.admin ?? { available: false, reason: 'missing_required_scope', principals: [] };
  const scope = ops.scope ?? {};
  const canViewAll = isGranted(capabilities, 'viewAllPrincipals');

  const scopeRows = (scope.scopes ?? []).length
    ? (scope.scopes ?? []).map((s) => `<span class="tag mono">${escapeHtml(s)}</span>`).join(' ')
    : `<span class="is-unknown">${UNKNOWN}</span>`;

  const principalRows = admin.available && admin.principals?.length
    ? admin.principals.map((p) => (
      `<tr><td><span class="rowtitle mono">${escapeHtml(p.uid)}</span></td>` +
      `<td>${escapeHtml(p.display_name ?? UNKNOWN)}</td>` +
      `<td>${escapeHtml((p.roles ?? []).join(' · ') || UNKNOWN)}</td>` +
      `<td>${escapeHtml((p.scopes ?? []).join(' · ') || UNKNOWN)}</td></tr>`
    )).join('')
    : '';

  return (
    `<div class="columns">` +
    `<div><div class="group-title"><h3>本次会话的可见范围</h3>` +
    `<span class="smallmeta">SERVER-ASSERTED SCOPE</span></div>` +
    `<div class="statusrows">` +
    `<div class="statusitem"><div class="tiny">主体</div><strong class="mono">${show(scope.subject)}</strong></div>` +
    `<div class="statusitem"><div class="tiny">角色（不产生权限）</div><strong>${
      escapeHtml((scope.roles ?? []).join(' · ') || UNKNOWN)}</strong></div>` +
    `<div class="statusitem"><div class="tiny">Endpoint 可见性</div><strong>${
      visibility === 'shared' ? 'Shared · 含已授权项目' : 'Private · 仅我'}</strong></div>` +
    `<div class="statusitem"><div class="tiny">持有 scope</div><strong>${
      (scope.scopes ?? []).length ? `${(scope.scopes ?? []).length} 项` : UNKNOWN}</strong></div>` +
    `</div>` +
    `<div class="group-title" style="margin-top:24px"><h3>持有的 scope</h3>` +
    `<span class="smallmeta">${(scope.scopes ?? []).length} 项</span></div>` +
    `<div class="tags">${scopeRows}</div>` +
    `</div>` +
    `<aside class="flatpanel"><h3>部署范围视图</h3>` +
    (canViewAll
      ? `<p class="module-empty">已持有 <span class="mono">portal:admin:read</span>，下面是服务端给出的全部署主体清单。</p>`
      : `<p class="module-empty">需要 <span class="mono">portal:admin:read</span>。` +
        `这个判定只看 scope：<b>不是</b>看账号名，也不接受客户端声明。</p>`) +
    (!canViewAll
      ? `<div class="guard warn">${escapeHtml(capabilityReason(capabilities, 'viewAllPrincipals'))}。` +
        `未授权时不显示任何主体清单 —— 也不显示一个「看起来像管理员」的界面。</div>`
      : '') +
    (principalRows
      ? `<div class="tablewrap"><table style="min-width:520px">` +
        `<thead><tr><th>UID</th><th>显示名</th><th>角色</th><th>Scope</th></tr></thead>` +
        `<tbody>${principalRows}</tbody></table></div>` +
        `<div class="guard">UID 是 Linux 账号标识；权限来自 Scope，不来自账号名。` +
        `Linux 账号 + 受限 PAM 的产品认证路径尚未上线，因此这里不把任何账号当作可信身份来源。</div>`
      : '') +
    `</aside></div>` +
    `<div class="group-title" style="margin-top:26px"><h3>能力判定</h3>` +
    `<span class="smallmeta">DERIVED FROM SCOPES ONLY</span></div>` +
    `<div class="module-details">` +
    Object.entries(capabilities ?? {}).map(([name, entry]) =>
      `<div class="module-kv"><dt>${escapeHtml(name)}</dt>` +
      `<dd class="${entry.granted ? '' : 'is-unknown'}">${entry.granted ? '已授权' : '未授权'}` +
      (entry.granted ? '' : `<br><span class="sect-sub">需要 ${escapeHtml(entry.missing.join(' + '))}</span>`) +
      `</dd></div>`).join('') +
    `</div>` +
    // The operator audit timeline lives inside Administration, not as a tenth
    // view: it is the same session, the same scope gate and the same panel.
    renderEventsSection(events)
  );
}

// --- page-level notices -----------------------------------------------------

const NOTICES = Object.freeze({
  [OPS_STATES.SIGNED_OUT]: {
    title: '请先登录',
    body: '会话已失效或已注销（401）。部署、Endpoint、Key 与评测都需要一个有效的会话。',
    action: '<a class="primary" href="/login?next=%2Fatlas">去登录</a>',
  },
  [OPS_STATES.LOADING]: {
    title: '正在读取',
    body: '请求已经发出，还没有收到答复。这里既不会显示上一次的数字，也不会把「还没问完」画成「没有数据」。',
    action: '',
  },
  [OPS_STATES.DENIED]: {
    title: '未获得模块目录授权',
    body: '身份已通过校验，但没有本页的授权（403）。请联系管理员开通 portal:modules:read。',
    action: '',
  },
  [OPS_STATES.UNKNOWN]: {
    title: '暂时无法确定',
    body: '没有取到可用的管理数据，也没有得到明确答复，因此这里不显示任何状态或数字。',
    action: '<button type="button" class="secondary" data-ops-retry="1">重试</button>',
  },
});

export function renderNotice(state, reason) {
  const notice = NOTICES[state] ?? NOTICES[OPS_STATES.UNKNOWN];
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

/** The provenance strip. A synthetic source is stated on screen, at the top of
 *  every panel, and not only in a comment. */
export function renderOpsStrip(ops) {
  if (!ops) return '';
  if (!isDemo(ops)) {
    const expired = (ops.metrics?.items ?? []).some((metric) => metric.reason === 'stale_sample');
    if (expired) {
      return (
        `<div class="demo-strip" role="status">` +
        `<span><strong>STALE</strong> · 部分观测指标已过期或无法证明新鲜，数值已隐藏；请重新读取。</span></div>`
      );
    }
    return (
      `<div class="demo-strip" role="status" style="border-left-color:var(--good)">` +
      `<span><strong>LIVE</strong> · 管理数据来自已接线的同源服务。</span></div>`
    );
  }
  return (
    `<div class="demo-strip" role="status">` +
    `<span><strong>SYNTHETIC</strong> · 本面板的所有部署、Endpoint、Key 与指标都来自` +
    `进程内合成 fixture，<b>不是</b> Dell 生产读数；共享操作只改本地 fixture，不写任何服务。</span>` +
    `<span class="mono">FAIL-CLOSED</span></div>`
  );
}

/** Every panel body when there is no payload yet. */
export const renderPending = (state, reason) => renderNotice(state, reason);

/** The panel ids, in the order the controller mounts them. Declared here as well
 *  as in the controller so a test can assert the two lists agree — a panel that
 *  is rendered but never mounted (or mounted and never rendered) is a blank
 *  rectangle, and this is the cheapest way to notice. */
export const OPS_PANEL_IDS = Object.freeze([
  'registry', 'keys', 'playground', 'observability', 'evaluation', 'access',
]);

/**
 * Every panel body for one payload, as `{panelId: html}`.
 *
 * The controller paints exactly this, and the offline end-to-end test asserts
 * against exactly this, so "what the browser shows" and "what the test checks"
 * cannot drift apart into two different answers.
 */
export function renderPanels(ops, capabilities, options = {}) {
  return {
    registry: renderRegistry(ops, capabilities, options),
    keys: renderKeys(ops),
    playground: renderPlayground(ops, capabilities) + renderRequestExample(ops),
    observability: renderObservability(ops, capabilities, options),
    evaluation: renderEvaluation(ops, capabilities),
    access: renderAdmin(ops, capabilities, options),
  };
}
