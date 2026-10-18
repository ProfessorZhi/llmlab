/*! web/js/features/events.js — the operator audit timeline, rendered honestly.
 *
 *  `GET /api/portal/events` is the V1-11 bounded audit surface: two real
 *  ledgers (this BFF's elevation audit and Registry's `audit_log`), reduced to
 *  a **whitelist** of fields before they ever leave the backend. This module is
 *  the read-only half in the browser: validate what arrived, render only the
 *  columns the contract names, and keep the four source states apart.
 *
 *  What it deliberately does not do:
 *
 *  * **No controls.** There is no filter, no range picker, no "open raw log",
 *    no path/url/promql/source input. The only request the UI may make is the
 *    fixed path with a fixed, bounded limit (see `loadEvents` in source.js), so
 *    there is nothing here for a query string to re-point.
 *  * **No second ACL.** Whether this session may open the timeline is decided
 *    by `portal:admin:read` in `capability.js`; ordinary members get a notice
 *    with no rows and no fetch, never a filtered view of someone else's
 *    activity.
 *  * **No invented rows.** `ok` with items renders them; `empty` says the
 *    ledger was read and holds nothing; `unconfigured` and `unreachable` say
 *    which one happened. A blank table is never used for any of these.
 *
 *  The field allowlists mirror `ELEVATION_FIELDS` / `REGISTRY_FIELDS` in
 *  `backend/bff/events.py`. They are re-declared here so a backend that starts
 *  sending a new column is *dropped* by the renderer rather than leaked into
 *  the page by a spread — the failure mode that a "render everything" table
 *  invites.
 */

import { escapeHtml } from '../dom.js';

export const EVENTS_SCHEMA = 'portal-events/1';
export const EVENTS_DEFAULT_LIMIT = 25;
export const EVENTS_MAX_LIMIT = 100;

/** Source states, same vocabulary as `backend/bff/events.py::STATES`. */
export const EVENT_STATES = Object.freeze({
  OK: 'ok',
  EMPTY: 'empty',
  UNCONFIGURED: 'unconfigured',
  UNREACHABLE: 'unreachable',
});

const SOURCE_STATES = Object.freeze(Object.values(EVENT_STATES));

/** Whitelist per source. Anything not named here is not rendered, even if a
 *  future backend sends it. Mirrors `backend/bff/events.py`. */
export const EVENT_FIELDS = Object.freeze({
  elevation: Object.freeze(['at', 'event', 'outcome', 'actor', 'principal', 'uid', 'linux_user', 'admin_mode']),
  registry: Object.freeze(['at', 'action', 'actor', 'actor_uid', 'object_type', 'object_id']),
});

/** How each source's whitelist reads on screen. `key` → column label. */
export const EVENT_COLUMNS = Object.freeze({
  elevation: Object.freeze([
    ['at', '时间'], ['event', '事件'], ['outcome', '结果'],
    ['principal', '主体'], ['uid', 'uid'], ['linux_user', 'Linux 账号'], ['admin_mode', '管理员模式'],
  ]),
  registry: Object.freeze([
    ['at', '时间'], ['action', '动作'], ['actor', '主体'],
    ['actor_uid', 'uid'], ['object_type', '对象类型'], ['object_id', '对象 ID'],
  ]),
});

export const EVENT_SOURCE_LABELS = Object.freeze({
  elevation: '提权审计 · 本层 admin_mode',
  registry: 'Registry · audit_log',
});

export const EVENT_STATE_LABELS = Object.freeze({
  [EVENT_STATES.OK]: '已读取',
  [EVENT_STATES.EMPTY]: '确实为空',
  [EVENT_STATES.UNCONFIGURED]: '本部署未接线',
  [EVENT_STATES.UNREACHABLE]: '账本读取失败',
});

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const isStr = (v) => typeof v === 'string' && v.length > 0;

/** Keep only the allowed keys. Returns a plain object; never a spread. */
function pickFields(item, allow) {
  const out = {};
  for (const key of allow) {
    if (!Object.prototype.hasOwnProperty.call(item, key)) continue;
    const value = item[key];
    out[key] = (value === null || typeof value === 'number' || typeof value === 'boolean')
      ? value
      : (typeof value === 'string' ? value : null);
  }
  return out;
}

/**
 * Validate a `portal-events/1` DTO.
 *
 * Returns `{ok, errors, dto}` and never throws, so a malformed payload becomes
 * a renderable `unknown` instead of a blank panel. Unknown *fields* are dropped
 * rather than rejected: the contract is "these may appear", and a future column
 * must not break an older page. Unknown *states* and an unknown schema are
 * errors, because those change what the page would claim.
 */
export function validateEventsDto(raw) {
  const errors = [];
  if (!isObj(raw)) return { ok: false, errors: ['payload is not an object'], dto: null };
  if (raw.schema_version !== EVENTS_SCHEMA) {
    errors.push(`schema_version must be ${EVENTS_SCHEMA}`);
  }
  if (!Number.isInteger(raw.limit) || raw.limit < 1 || raw.limit > EVENTS_MAX_LIMIT) {
    errors.push(`limit must be an integer in [1, ${EVENTS_MAX_LIMIT}]`);
  }
  if (!Array.isArray(raw.sources)) {
    errors.push('sources must be an array');
  }

  const sources = [];
  if (Array.isArray(raw.sources)) {
    raw.sources.forEach((source, i) => {
      if (!isObj(source)) { errors.push(`sources[${i}] is not an object`); return; }
      const name = source.source;
      if (!Object.prototype.hasOwnProperty.call(EVENT_FIELDS, name)) {
        errors.push(`sources[${i}].source is not a known ledger`);
        return;
      }
      if (!SOURCE_STATES.includes(source.state)) {
        errors.push(`sources[${i}].state is not one of ${SOURCE_STATES.join('/')}`);
      }
      if (!Array.isArray(source.items)) {
        errors.push(`sources[${i}].items must be an array`);
      }
      const items = Array.isArray(source.items)
        ? source.items.filter(isObj).map((item) => pickFields(item, EVENT_FIELDS[name]))
        : [];
      sources.push({
        source: name,
        state: source.state,
        reason: isStr(source.reason) ? source.reason : null,
        observed_at: isStr(source.observed_at) ? source.observed_at : null,
        newest_at: isStr(source.newest_at) ? source.newest_at : null,
        count: Number.isInteger(source.count) ? source.count : items.length,
        truncated: source.truncated === true,
        items,
      });
    });
  }
  if (errors.length) return { ok: false, errors, dto: null };

  return {
    ok: true,
    errors: [],
    dto: {
      schema_version: EVENTS_SCHEMA,
      observed_at: isStr(raw.observed_at) ? raw.observed_at : null,
      limit: raw.limit,
      sources,
      notes: Array.isArray(raw.notes) ? raw.notes.filter(isStr) : [],
    },
  };
}

/** One panel-level state for the section header. Each source still renders its
 *  own state beside it — this is a summary, never a replacement. */
export function eventsOverallState(dto) {
  const states = (dto?.sources ?? []).map((s) => s.state);
  if (!states.length) return 'unknown';
  // A ledger that could not be read makes the whole picture incomplete, even if
  // the other one answered — the summary must not round that up to "ready".
  if (states.some((s) => s === EVENT_STATES.UNREACHABLE)) return 'unknown';
  if (states.some((s) => s === EVENT_STATES.OK)) return 'ready';
  if (states.every((s) => s === EVENT_STATES.UNCONFIGURED)) return EVENT_STATES.UNCONFIGURED;
  if (states.some((s) => s === EVENT_STATES.EMPTY)) return EVENT_STATES.EMPTY;
  return 'unknown';
}

const cell = (value) => {
  if (value === null || value === undefined) return '<span class="is-unknown">—</span>';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return escapeHtml(String(value));
  return escapeHtml(String(value));
};

function renderSource(source) {
  const columns = EVENT_COLUMNS[source.source] ?? [];
  const stateLabel = EVENT_STATE_LABELS[source.state] ?? source.state;
  const head = `<div class="group-title" style="margin:18px 0 8px">` +
    `<h3 style="font-size:14px">${escapeHtml(EVENT_SOURCE_LABELS[source.source] ?? source.source)}</h3>` +
    `<span class="smallmeta" data-event-state="${escapeHtml(source.source)}:${escapeHtml(source.state)}">` +
    `${escapeHtml(stateLabel)}` +
    `${source.truncated ? ' · 已截断' : ''}` +
    `${source.count ? ` · ${escapeHtml(String(source.count))} 条` : ''}</span></div>`;

  const reason = source.reason
    ? `<p class="footnote">原因：<b class="mono">${escapeHtml(source.reason)}</b></p>`
    : '';
  const newest = source.newest_at
    ? `<p class="tiny">账本最新一条：<span class="mono">${escapeHtml(source.newest_at)}</span></p>`
    : '';

  if (!source.items.length) {
    // A blank table would read as "no events" for all four states. Say which
    // one it is instead.
    return head + `<p class="module-empty">${escapeHtml(stateLabel)}</p>` + newest + reason;
  }

  const headRow = columns.map(([, label]) => `<th scope="col">${escapeHtml(label)}</th>`).join('');
  const rows = source.items.map((item) => (
    '<tr>' + columns.map(([key]) => `<td>${cell(item[key])}</td>`).join('') + '</tr>'
  )).join('');

  return head +
    `<div class="tablewrap"><table class="ws-table" data-events="${escapeHtml(source.source)}">` +
    `<thead><tr>${headRow}</tr></thead><tbody>${rows}</tbody></table></div>` +
    newest + reason;
}

/**
 * Render the audit section for the Admin panel.
 *
 * `events` is the state the controller carries: `{state, reason, dto, pending}`.
 * The section never renders a control, and never renders rows unless the DTO
 * validated and this session holds the operator scope.
 */
export function renderEventsSection(events = null) {
  const state = events?.state ?? 'unknown';
  const reason = events?.reason ?? null;
  const heading = `<div class="group-title" style="margin:26px 0 8px">` +
    `<h3 class="section-h" style="font-size:15px">审计时间线 <em class="heading-en">/ OPERATOR AUDIT</em></h3>` +
    `<span class="smallmeta">GET /api/portal/events · 有界 · 脱敏 · 只读</span></div>`;

  if (state === 'denied') {
    return heading +
      `<div class="surface"><span class="caps">DENIED</span>` +
      `<h3 style="margin:11px 0 5px;font-size:15px">需要操作员范围</h3>` +
      `<p class="module-empty">审计时间线需要 <b class="mono">portal:admin:read</b>。` +
      `普通成员不会看到任何事件行，也不会发起这次请求。</p>` +
      (reason ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>` : '') +
      `</div>`;
  }
  if (state === 'signed_out') {
    return heading +
      `<div class="surface"><span class="caps">SIGNED_OUT</span>` +
      `<h3 style="margin:11px 0 5px;font-size:15px">请先登录</h3>` +
      `<p class="module-empty">会话已失效或已注销（401），因此这里不显示任何审计行。</p>` +
      (reason ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>` : '') +
      `</div>`;
  }
  if (state === 'not_configured') {
    return heading +
      `<div class="surface"><span class="caps">NOT_CONFIGURED</span>` +
      `<h3 style="margin:11px 0 5px;font-size:15px">本部署未提供审计时间线</h3>` +
      `<p class="module-empty">两条审计账本都没有接线（404/501/503）。` +
      `这是「本部署没有这条功能」，不是「没有任何审计事件」。</p>` +
      (reason ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>` : '') +
      `</div>`;
  }
  if (state === 'unknown' || !events?.dto) {
    return heading +
      `<div class="surface"><span class="caps">UNKNOWN</span>` +
      `<h3 style="margin:11px 0 5px;font-size:15px">暂时无法确定</h3>` +
      `<p class="module-empty">没有取到可用的审计数据，也没有得到明确答复，因此这里不显示任何行。</p>` +
      (reason ? `<p class="footnote">原因：<b class="mono">${escapeHtml(reason)}</b></p>` : '') +
      `</div>`;
  }

  const sources = events.dto.sources.map(renderSource).join('');
  const observed = events.dto.observed_at
    ? `<p class="tiny">本次读取：<span class="mono">${escapeHtml(events.dto.observed_at)}</span>` +
      ` · limit ${escapeHtml(String(events.dto.limit))}</p>`
    : '';
  return heading + `<div data-events-section="1">${sources}${observed}</div>`;
}
