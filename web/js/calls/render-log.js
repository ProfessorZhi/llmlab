/*! web/js/calls/render-log.js — the call-log view, as Atelier 05 markup.
 *
 *  Every function here is pure: `(log) -> html string`. That is deliberate.
 *
 *  The alternative — writing straight into a host node — would make the only way
 *  to test this file a real browser, and this build has none (see
 *  `web/README.md`). Pure string functions are covered by
 *  `tests/calls-log.test.js` under plain Node, so the thing that decides what a
 *  reader sees is the thing under test.
 *
 *  The markup is deliberately made of the components Atelier 05 already ships —
 *  `.demo-strip`, `.stats`/`.stat`, `.tablewrap`, `.flatpanel`, `.guard`,
 *  `.surface`, `.editorial-index`, `.legendline`, `.secondary`. Nothing here
 *  invents a look of its own, which is what makes the call log read as part of
 *  the same product rather than as a page bolted onto it.
 *
 *  Two display rules are inherited from the rest of the front-end and are not
 *  negotiable here:
 *
 *  * a missing measurement renders as `—` (via `../format.js`), never `0`;
 *  * every value that came from the payload is escaped before it reaches the
 *    markup, because `request_id`, `model` and friends are upstream strings.
 */

import { escapeHtml } from '../dom.js';
import { fmt, metric, num, UNKNOWN } from '../format.js';
import {
  CONSENT_STATES,
  LOG_STATES,
  consentLabel,
  contentReasonLabel,
  isDemo,
  statusClass,
  summarise,
} from './contract.js';
import { renderInsights } from './insights.js';

const cell = (value) => `<td>${value}</td>`;

const optionalText = (value) =>
  typeof value === 'string' && value.trim()
    ? `<span class="doc-id">${escapeHtml(value)}</span>`
    : UNKNOWN;

/** The DEMO strip. Rendered whenever the payload is not live — which, in this
 *  build, is always. It states the absence of a gateway up front rather than
 *  burying it in a footnote. */
export function renderDemoBanner(log) {
  if (!log) return '';
  if (!isDemo(log)) {
    return (
      `<div class="demo-strip" role="status" style="border-left-color:var(--good)">` +
      `<span><strong>LIVE</strong> · 数据来自已接线的 Gateway。</span>` +
      `<span class="mono">TRANSPORT ${escapeHtml(String(log.transport?.mode ?? 'live')).toUpperCase()}</span></div>`
    );
  }
  const synthetic = log.synthetic === true ? '合成数据' : '未接线';
  return (
    `<div class="demo-strip" role="status">` +
    `<span><strong>DEMO</strong> · ${escapeHtml(synthetic)} · 未接入真实 Gateway` +
    `（transport.mode = demo）。不得用于对账或容量决策。</span>` +
    `<span class="mono">TRANSPORT DEMO</span></div>`
  );
}

/** Whose calls, over which window, and how the window was narrowed. */
export function renderScopeLine(log) {
  if (!log?.scope || !log.window) return '';
  const keys = log.scope.keys
    .map((key) => `<b class="mono">${escapeHtml(key)}</b>`)
    .join('、');
  const revoked = log.scope.revoked_keys ?? [];
  const revokedNote = revoked.length
    ? `<span>另有 <b class="mono">${revoked.length}</b> 把已吊销的 key（` +
      `${revoked.map((key) => `<b class="mono">${escapeHtml(key)}</b>`).join('、')}）` +
      `的调用已从明细中排除</span>`
    : '';
  return (
    `<div class="legendline">` +
    `<span>主体 <b class="mono">${escapeHtml(log.scope.subject)}</b></span>` +
    `<span>窗口 <b class="mono">${escapeHtml(log.window.from)} → ${escapeHtml(log.window.to)}</b></span>` +
    revokedNote +
    `<span class="mono">UTC · 左闭右开 · 只含 ${keys}</span>` +
    `</div>`
  );
}

/** The content panel: the state of the opt-in, and the reason there is no text.
 *  It never renders a content field — because there is none, and saying so is
 *  the point of the panel. */
export function renderContentPanel(content) {
  if (!content) return '';
  const consent = content.consent;
  const tone = consent === CONSENT_STATES.GRANTED ? 'good' : 'warn';
  return (
    `<div class="flatpanel" style="margin-top:24px">` +
    `<h3>正文访问</h3>` +
    `<div class="statusrows">` +
    `<div class="statusitem"><div class="tiny">opt-in 状态</div>` +
    `<strong>${escapeHtml(consentLabel(consent))}</strong>` +
    `<div class="badge ${tone}" style="margin-top:10px">服务端记录的状态</div></div>` +
    `<div class="statusitem"><div class="tiny">本页正文</div><strong>不含</strong>` +
    `<div class="badge" style="margin-top:10px">content.included = false</div></div>` +
    `</div>` +
    `<p class="footnote">${escapeHtml(contentReasonLabel(content.reason))}</p>` +
    `<div class="guard">${escapeHtml(content.note ?? '')}</div>` +
    `</div>`
  );
}

/** Summary tiles, using 05's `.stats`/`.stat` rather than a bespoke card. */
export function renderSummary(log) {
  if (!log?.counts) return '';
  const totals = summarise(log);
  const counts = log.counts;
  const coverage = log.coverage ?? {};
  const tokenValue =
    totals.input === null ? UNKNOWN : `${fmt(totals.input)} → ${fmt(totals.output)}`;
  const tokenNote =
    totals.tokensKnown === totals.rows
      ? '输入 → 输出'
      : `${fmt(totals.rows - totals.tokensKnown)} 条未记录 Token`;
  return (
    `<div class="stats">` +
    `<div class="stat"><span>本页调用</span><strong class="number">${fmt(totals.rows)}</strong>` +
    `<small>共 ${fmt(counts.available)} 条符合条件</small></div>` +
    `<div class="stat"><span>本页 Token</span><strong class="number">${tokenValue}</strong>` +
    `<small>${escapeHtml(tokenNote)}</small></div>` +
    `<div class="stat"><span>本页平均排队</span>` +
    `<strong class="number">${totals.queueAvg === null ? UNKNOWN : `${num(totals.queueAvg)} ms`}</strong>` +
    `<small>${fmt(coverage.queue_unknown_rows ?? 0)} 条未记录排队</small></div>` +
    `<div class="stat"><span>本页非成功</span><strong class="number">${fmt(totals.failed)}</strong>` +
    `<small>状态码见下表</small></div>` +
    `</div>`
  );
}

function renderRow(row) {
  const tokens = row.tokens ?? {};
  const hasTokens = Number.isFinite(tokens.input) && Number.isFinite(tokens.output);
  const tokenText = hasTokens
    ? `${fmt(tokens.input)} / ${fmt(tokens.output)} / ${fmt(tokens.total)}`
    : UNKNOWN;
  const ms = (value) => (Number.isFinite(value) ? `${num(value)} ms` : UNKNOWN);
  const num1 = (value) => `<td class="num">${value}</td>`;
  // The row opens the detail drawer. It is a real focusable control (Enter /
  // Space), not a click-only <tr>, and the request_id is the accessible name.
  const label = `查看调用详情 ${row.request_id ?? row.call_id}`;
  return (
    `<tr class="calls-row" data-call-id="${escapeHtml(row.call_id)}" tabindex="0" aria-label="${escapeHtml(label)}">` +
    cell(`<time datetime="${escapeHtml(row.ts)}">${escapeHtml(row.ts)}</time>`) +
    cell(`<span class="badge calls-status-${statusClass(row.status)}">${metric(row.status)}</span>`) +
    cell(escapeHtml(row.outcome)) +
    cell(escapeHtml(row.model ?? '') || UNKNOWN) +
    cell(optionalText(row.request_id)) +
    cell(`<span class="doc-id">${escapeHtml(row.key_id)}</span>`) +
    cell(escapeHtml(row.workload ?? '') || UNKNOWN) +
    num1(tokenText) +
    num1(ms(row.queue_ms)) +
    num1(ms(row.ttft_ms)) +
    num1(ms(row.total_ms)) +
    `</tr>`
  );
}

const HEADERS = [
  '时间 (UTC)', '状态', '结果', '模型', 'request_id', 'key', '工作负载',
  'Token 流入/流出/合计', '排队', '首字 (TTFT)', '总耗时',
];

export function renderTable(log) {
  const rows = log?.calls ?? [];
  const head =
    `<thead><tr>${HEADERS.map((label) => `<th scope="col">${escapeHtml(label)}</th>`).join('')}</tr></thead>`;
  const body = rows.length
    ? `<tbody>${rows.map(renderRow).join('')}</tbody>`
    : `<tbody><tr><td colspan="${HEADERS.length}" class="muted" style="text-align:center;padding:34px 12px">` +
      `当前条件下没有匹配的调用。</td></tr></tbody>`;
  return (
    `<div class="tablewrap" style="margin-top:26px">` +
    `<table class="calls-table" id="callsTable">${head}${body}</table></div>`
  );
}

/** The server's own hints and notes, shown rather than paraphrased. */
export function renderNotes(log) {
  const hints = log?.display_hints ?? [];
  const notes = log?.notes ?? [];
  if (!hints.length && !notes.length) return '';
  const list = (items) =>
    items.length
      ? `<ul class="footnote" style="padding-left:17px">${items
          .map((item) => `<li>${escapeHtml(item)}</li>`)
          .join('')}</ul>`
      : '';
  return (
    `<div class="flatpanel" style="margin-top:24px">` +
    `<h3>口径与提示</h3>${list(hints)}${list(notes)}</div>`
  );
}

/** The "load older" control. It only appears when the server said the page was
 *  truncated, and it carries the keyset cursor verbatim — the page never builds
 *  a cursor of its own from row contents. */
export function renderPager(log) {
  const counts = log?.counts;
  if (!counts?.truncated || !counts.next_before) return '';
  return (
    `<div class="group-title">` +
    `<span class="smallmeta">已显示 ${fmt(counts.returned)} / ${fmt(counts.available)} 条，按时间倒序</span>` +
    `<button type="button" class="secondary" data-calls-more="1">加载更早的调用</button>` +
    `</div>`
  );
}

const NOTICES = Object.freeze({
  [LOG_STATES.SIGNED_OUT]: {
    title: '请先登录',
    body: '会话已失效或已注销（401）。登录后即可查看你自己的调用日志。',
    action: '<a class="primary" href="/login?next=%2Fatlas%23calls">去登录</a>',
  },
  [LOG_STATES.DENIED]: {
    title: '未获得「我的调用」授权',
    body:
      '你的身份已通过校验，但没有本视图的授权（403），或者请求的 key 不在你名下。' +
      '请联系管理员为你的账号开通 atlas:calls:self:read。',
    action: '',
  },
  [LOG_STATES.UNKNOWN]: {
    title: '暂时无法确定',
    body: '没有取到可用的调用数据，也没有得到「确实为空」的答复，因此这里不显示任何数字。',
    action: '<button type="button" class="secondary" data-calls-retry="1">重试</button>',
  },
  [LOG_STATES.LOADING]: {
    title: '正在读取你的调用记录',
    body: '已向同源 BFF 发出请求，尚未得到答复。答复之前这里不显示任何数字。',
    action: '',
  },
  [LOG_STATES.NOT_CONFIGURED]: {
    title: '本部署未提供「我的调用」数据路由',
    body: '同源路径 /api/atlas/calls 在这台服务器上不存在（404/501）：Gateway 调用元数据尚未接线到 BFF。这不是「零次调用」。',
    // The offer to open the samples belongs to the QA artifact only. On
    // production it would be an invitation to fabricated data, so neither the
    // hint nor the button is rendered — they are not hidden, they are absent.
    demoHint: '可用 ?demo=1 查看合成样例的交互。',
    demoAction: '<button type="button" class="secondary" data-demo-switch>查看设计样例</button>',
    action: '',
  },
  [LOG_STATES.EMPTY]: {
    title: '这段时间没有你的调用',
    body: '服务器确认该窗口内没有你名下 key 的调用记录 —— 这是「确实为空」，不是「测不到」。',
    action: '',
  },
});

export function renderNotice(state, reason, { demoSwitch = false } = {}) {
  const notice = NOTICES[state] ?? NOTICES[LOG_STATES.UNKNOWN];
  const body = demoSwitch ? `${notice.body}${notice.demoHint ?? ''}` : notice.body;
  const action = demoSwitch ? (notice.demoAction ?? notice.action) : notice.action;
  const detail = reason
    ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>`
    : '';
  return (
    `<div class="surface">` +
    `<span class="caps">${escapeHtml(state.toUpperCase())}</span>` +
    `<h3 style="margin:11px 0 5px;font-size:15px">${escapeHtml(notice.title)}</h3>` +
    `<p class="module-empty">${escapeHtml(body)}</p>${detail}` +
    (action ? `<div class="module-links">${action}</div>` : '') +
    `</div>`
  );
}

/** Everything below the filter bar, for one page state. */
export function renderLog(log, state, reason = null, { demoSwitch = false } = {}) {
  if (state !== LOG_STATES.READY && state !== LOG_STATES.EMPTY) {
    // A failed load has no payload to carry its reason, so the controller hands
    // it over explicitly. Without it the notice said "unknown" with no clue why.
    return renderNotice(state, log?.reason ?? reason, { demoSwitch });
  }
  return [
    renderDemoBanner(log),
    renderScopeLine(log),
    renderSummary(log),
    renderInsights(log),
    renderTable(log),
    renderPager(log),
    renderContentPanel(log.content),
    renderNotes(log),
  ].join('');
}
