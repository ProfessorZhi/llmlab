/*! web/js/calls/controller.js — filters, one fetch, and the page state machine.
 *
 *  The only module in `web/js/calls/` that touches the network or the DOM. It is
 *  deliberately not built on `../data/remote-client.js`: that client's contract
 *  is the *Atlas bundle*, and reusing it here would mean teaching it about a
 *  second payload shape and a second set of failure states.
 *
 *  NOTE for reviewers: `web/README.md` says network calls live under
 *  `web/js/data/`. Putting this one there would require a fifth exception in
 *  `.gitignore`, which `tests/tracked-assets.test.js` pins as "exactly four
 *  front-end paths" — so the deviation is deliberate, local to this file, and
 *  is one `fetch` call. See `docs/portal-call-log-contract.md` §7.
 *
 *  The page state machine is the interesting part, and it maps 1:1 onto what the
 *  server said:
 *
 *     200 → ready (rows) | empty (a confirmed, measured zero)
 *     401 → signed_out   ("sign in")
 *     403 → denied       ("ask an operator") — never retried automatically
 *     anything else / unparsable / contract violation → unknown
 *
 *  `empty` and `unknown` are kept apart because they answer different questions,
 *  and only one of them is an answer at all.
 */

import { $ } from '../dom.js';
import {
  LOG_STATES,
  NOT_CONFIGURED_STATUSES,
  STATE_CHANGING_STATUSES,
  stateFor,
  validateCallLog,
} from './contract.js';
import { renderLog } from './render-log.js';
import { callsToCsv, callsToJson, exportName, renderCallDetail } from './insights.js';
import { createDemoCallsFetch } from './demo-fixture.js';
import { DATA_MODES, resolveDataMode } from '../demo-mode.js';
import { isQaBuild } from '../build-profile.js';
import { saveFile } from '../atlas/exporters.js';

/** The path the preview serves. A production BFF would have to serve it too;
 *  nothing here guesses an alternative. */
export const CALLS_ENDPOINT = '/api/atlas/calls';

const DEFAULT_LIMIT = 50;

/** Quick windows. Each one only fills the two datetime inputs and applies — the
 *  server still decides what the window means. */
export const TIME_PRESETS = Object.freeze({ '1h': 3600_000, '24h': 86400_000, '7d': 7 * 86400_000 });

/** `[from, to)` as datetime-local values for a preset ending at `now`. */
export function presetRange(preset, now = Date.now()) {
  const span = TIME_PRESETS[preset];
  if (!span) return null;
  const end = Math.ceil(now / 60_000) * 60_000;
  return {
    from: utcIsoToLocalInput(new Date(end - span).toISOString()),
    to: utcIsoToLocalInput(new Date(end).toISOString()),
  };
}

/** Local `<input type="datetime-local">` value → an ISO-8601 UTC instant.
 *  The input is in the reader's own timezone (that is what the widget means),
 *  and the endpoint requires an explicit offset, so this conversion is the only
 *  honest reading of what the reader typed. */
export function localInputToUtcIso(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/** UTC ISO → the value a `datetime-local` input wants, in local time. */
export function utcIsoToLocalInput(iso) {
  if (typeof iso !== 'string' || !iso.trim()) return null;
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return null;
  const local = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

/**
 * Build the query string from filter values.
 *
 * Order is fixed and every value is encoded; a parameter that is not set is
 * absent rather than empty, because the server refuses an empty one.
 */
export function buildQuery(filters = {}) {
  const params = new URLSearchParams();
  const put = (name, value) => {
    if (value === null || value === undefined || value === '') return;
    params.set(name, String(value));
  };
  put('from', filters.from);
  put('to', filters.to);
  put('key', filters.key);
  put('outcome', filters.outcome && filters.outcome !== 'all' ? filters.outcome : null);
  put('model', filters.model);
  put('limit', filters.limit);
  if (filters.cursor) {
    put('before_ts', filters.cursor.ts);
    put('before_call_id', filters.cursor.call_id);
  }
  const query = params.toString();
  return query ? `?${query}` : '';
}

/** The hero badge: which source the rows on screen came from, in one phrase. */
export function sourceBadgeText(state, demo, reason = null) {
  if (demo && (state === LOG_STATES.READY || state === LOG_STATES.EMPTY)) return 'DEMO · 合成数据 · 未接 Gateway';
  // Both are "not configured", but they are different wiring gaps and the badge
  // must not describe one as the other: 404/501 mean the route is absent, 503
  // means it exists with no adapter behind it.
  if (state === LOG_STATES.NOT_CONFIGURED) {
    return reason === 'http-503' ? '未接线 · 适配器未装配' : '未接线 · 路由不存在';
  }
  return {
    [LOG_STATES.READY]: 'LIVE · 同源 BFF · 已校验',
    [LOG_STATES.EMPTY]: 'LIVE · 窗口内确认为空',
    [LOG_STATES.LOADING]: '读取中 · 等待服务端答复',
    [LOG_STATES.DENIED]: '无授权 · 403',
    [LOG_STATES.SIGNED_OUT]: '需登录 · 401',
  }[state] ?? '来源不可用 · UNKNOWN';
}

/** Status codes that change what the page should say rather than what it shows. */
const stateChanging = (status) => STATE_CHANGING_STATUSES.includes(status);

/**
 * @param {object} [options]
 * @param {string} [options.endpoint]
 * @param {Function} [options.fetchImpl] injected so tests need no server
 * @param {Document} [options.doc]
 */
export function createCalls({
  endpoint = CALLS_ENDPOINT, fetchImpl, doc = globalThis.document, dialog = null, now = () => Date.now(),
  demoSwitch = false,
} = {}) {
  const dom = {
    filters: $('#callsFilters', doc),
    from: $('#callsFrom', doc),
    to: $('#callsTo', doc),
    key: $('#callsKey', doc),
    outcome: $('#callsOutcome', doc),
    model: $('#callsModel', doc),
    limit: $('#callsLimit', doc),
    apply: $('#callsApply', doc),
    reset: $('#callsReset', doc),
    host: $('#callsHost', doc),
    badge: $('#callsSourceBadge', doc),
    window: $('#callsWindow', doc),
    exportCsv: $('#callsExportCsv', doc),
    exportJson: $('#callsExportJson', doc),
    exportNote: $('#callsExportNote', doc),
    presets: [...(doc?.querySelectorAll?.('[data-calls-preset]') ?? [])],
  };

  // The accumulated pages of one query. Anything that changes the filters
  // clears it: a cursor taken under one filter set means something different
  // under another, and reusing it would silently skip or repeat rows.
  let filters = { limit: DEFAULT_LIMIT };
  let pages = [];
  let state = LOG_STATES.UNKNOWN;
  let lastReason = null;
  let lastAction = null;

  const resolveFetch = () =>
    fetchImpl ?? (typeof globalThis.fetch === 'function' ? globalThis.fetch.bind(globalThis) : null);

  function currentView() {
    if (!pages.length) return null;
    const first = pages[0];
    const last = pages[pages.length - 1];
    const rows = pages.flatMap((page) => page.calls);
    return {
      ...first,
      calls: rows,
      counts: {
        ...last.counts,
        returned: rows.length,
        available: first.counts.available,
        truncated: last.counts.truncated,
        next_before: last.counts.next_before,
      },
    };
  }

  function paint() {
    if (dom.host) dom.host.innerHTML = renderLog(currentView(), state, lastReason, { demoSwitch });
    if (dom.window) {
      const window = currentView()?.window;
      dom.window.textContent = window
        ? `生效窗口 ${window.from} → ${window.to} (UTC，左闭右开)`
        : '尚未取到生效窗口';
    }
    syncFilterControls();
    syncExportControls();
    if (dom.badge) {
      const view = currentView();
      const demo = view?.transport?.mode === 'demo';
      dom.badge.textContent = sourceBadgeText(state, demo, lastReason);
      dom.badge.classList.toggle('warn', demo || state !== LOG_STATES.READY);
    }
  }

  /** Exports only exist for rows that are on screen. With none, the buttons are
   *  disabled and the note says why — not a click that silently saves nothing. */
  function syncExportControls() {
    const rows = currentView()?.calls?.length ?? 0;
    for (const button of [dom.exportCsv, dom.exportJson]) {
      if (!button) continue;
      button.disabled = rows === 0;
      button.title = rows ? `导出已加载的 ${rows} 行（仅元数据）` : '没有已加载的调用可导出';
    }
    if (dom.exportNote) {
      dom.exportNote.textContent = rows
        ? `导出已加载的 ${rows} 行 · 仅元数据，不含正文`
        : '导出：没有已加载的行';
    }
  }

  function exportAs(kind) {
    const view = currentView();
    if (!view?.calls?.length) return { ok: false, reason: 'no-rows' };
    const text = kind === 'csv' ? callsToCsv(view) : callsToJson(view);
    const name = exportName(view, kind);
    lastAction = { kind: 'export', format: kind, rows: view.calls.length, name };
    saveFile(name, text, kind === 'csv' ? 'text/csv;charset=utf-8' : 'application/json', doc);
    return { ok: true, name };
  }

  function openDetail(callId) {
    const row = currentView()?.calls?.find((r) => r.call_id === callId);
    if (!row) return { ok: false, reason: 'no-such-row' };
    lastAction = { kind: 'detail', call_id: callId };
    dialog?.explain?.(`调用详情 · ${row.request_id ?? row.call_id}`, renderCallDetail(row, currentView()));
    return { ok: true };
  }

  function applyPreset(preset) {
    const range = presetRange(preset, now());
    if (!range) return undefined;
    if (dom.from) dom.from.value = range.from;
    if (dom.to) dom.to.value = range.to;
    for (const node of dom.presets) {
      const on = node.dataset.callsPreset === preset;
      node.classList.toggle('active', on);
      node.setAttribute('aria-pressed', String(on));
    }
    return load();
  }

  function syncFilterControls() {
    const view = currentView();
    if (dom.key && view?.scope?.keys) {
      const selected = dom.key.value;
      dom.key.replaceChildren();
      const all = doc.createElement('option');
      all.value = '';
      all.textContent = '全部（我名下的 key）';
      dom.key.append(all);
      for (const key of view.scope.keys) {
        const option = doc.createElement('option');
        option.value = key;
        option.textContent = key;
        dom.key.append(option);
      }
      dom.key.value = view.scope.keys.includes(selected) ? selected : '';
    }
  }

  function readFilters({ keepCursor = false } = {}) {
    return {
      from: localInputToUtcIso(dom.from?.value),
      to: localInputToUtcIso(dom.to?.value),
      key: dom.key?.value || null,
      outcome: dom.outcome?.value || 'all',
      model: dom.model?.value?.trim() || null,
      limit: Number(dom.limit?.value) || DEFAULT_LIMIT,
      cursor: keepCursor ? filters.cursor ?? null : null,
    };
  }

  async function request(current) {
    const doFetch = resolveFetch();
    if (!doFetch) {
      state = LOG_STATES.UNKNOWN;
      return { state, reason: 'no-fetch-available' };
    }
    let response;
    try {
      response = await doFetch(`${endpoint}${buildQuery(current)}`, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
    } catch (error) {
      state = LOG_STATES.UNKNOWN;
      return { state, reason: 'request-failed' };
    }

    if (stateChanging(response.status)) {
      state = response.status === 401 ? LOG_STATES.SIGNED_OUT : LOG_STATES.DENIED;
      pages = [];
      return { state, reason: `http-${response.status}` };
    }
    if (NOT_CONFIGURED_STATUSES.includes(response.status)) {
      state = LOG_STATES.NOT_CONFIGURED;
      pages = [];
      return { state, reason: `http-${response.status}` };
    }
    if (!response.ok) {
      state = LOG_STATES.UNKNOWN;
      pages = [];
      return { state, reason: `http-${response.status}` };
    }

    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      state = LOG_STATES.UNKNOWN;
      return { state, reason: 'not-json' };
    }

    const checked = validateCallLog(payload?.data?.call_log);
    if (!checked.ok) {
      // A payload that fails its own contract is not partially rendered — the
      // errors are surfaced instead, because a half-shown call log is worse
      // than an obviously empty one.
      state = LOG_STATES.UNKNOWN;
      pages = [];
      return { state, reason: `contract:${checked.errors[0]}` };
    }
    return { state: null, log: checked.log };
  }

  /** Fetch the next page for the current filters (`keepCursor`) or the first. */
  async function load({ keepCursor = false } = {}) {
    const current = readFilters({ keepCursor });
    if (!pages.length) {
      // Say "asked, not told yet" — not "unknown", which reports a failure that
      // has not happened.
      state = LOG_STATES.LOADING;
      lastReason = null;
      paint();
    }
    const outcome = await request(current);
    lastReason = outcome.reason ?? null;
    if (outcome.log) {
      filters = current;
      pages = keepCursor ? [...pages, outcome.log] : [outcome.log];
      state = stateFor(currentView());
    } else if (state === LOG_STATES.LOADING) {
      state = LOG_STATES.UNKNOWN;
    }
    paint();
    return { state, reason: outcome.reason ?? null, log: currentView() };
  }

  function bind() {
    dom.apply?.addEventListener('click', () => load());
    for (const node of dom.presets) {
      node.addEventListener('click', () => applyPreset(node.dataset.callsPreset));
    }
    dom.exportCsv?.addEventListener('click', () => exportAs('csv'));
    dom.exportJson?.addEventListener('click', () => exportAs('json'));
    dom.reset?.addEventListener('click', () => {
      for (const node of [dom.from, dom.to, dom.key, dom.model]) {
        if (node) node.value = '';
      }
      for (const node of dom.presets) {
        node.classList.remove('active');
        node.setAttribute('aria-pressed', 'false');
      }
      if (dom.outcome) dom.outcome.value = 'all';
      if (dom.limit) dom.limit.value = String(DEFAULT_LIMIT);
      return load();
    });
    dom.host?.addEventListener('click', (event) => {
      const target = event.target;
      if (!(target instanceof globalThis.Element)) return;
      if (target.closest('[data-calls-more]')) {
        const cursor = currentView()?.counts?.next_before ?? null;
        if (cursor) {
          filters = { ...filters, cursor };
          return load({ keepCursor: true });
        }
      }
      if (target.closest('[data-calls-retry]')) return load();
      const row = target.closest('[data-call-id]');
      if (row) return openDetail(row.dataset.callId);
      return undefined;
    });
    dom.host?.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return undefined;
      const row = event.target?.closest?.('[data-call-id]');
      if (!row || row !== event.target) return undefined;
      event.preventDefault();
      return openDetail(row.dataset.callId);
    });
  }

  bind();
  state = LOG_STATES.UNKNOWN;
  paint();

  return {
    load,
    paint,
    get state() {
      return state;
    },
    get log() {
      return currentView();
    },
    get reason() {
      return lastReason;
    },
    get lastAction() {
      return lastAction;
    },
    readFilters,
    openDetail,
    exportAs,
    applyPreset,
  };
}

/**
 * Wire the call log to its mounts inside the V5 shell and fetch the first page.
 *
 * There is no self-boot block here any more: the view is a route of the shell,
 * so `app.js` owns the lifecycle (same as every other view). Importing this
 * module must stay free of DOM access so the tests can drive it under Node.
 */
export function initCalls({
  doc = globalThis.document,
  endpoint = CALLS_ENDPOINT,
  fetchImpl,
  dialog = null,
  search = globalThis.location?.search ?? '',
} = {}) {
  // The in-process SYNTHETIC fixture needs the QA artifact *and* `?demo=1`; on
  // production the query is inert and the fixture is never constructed. Every
  // payload it returns is `transport.mode = demo`, so the DEMO strip, export
  // file names and the detail drawer all say so.
  const demo = resolveDataMode(search, { qa: isQaBuild(doc) }) === DATA_MODES.DEMO;
  const transport = fetchImpl ?? (demo ? createDemoCallsFetch() : undefined);
  const controller = createCalls({ endpoint, fetchImpl: transport, doc, dialog, demoSwitch: demo });
  const first = controller.load().catch(() => {
    /* `load` already maps every failure into a page state; nothing else to do. */
  });
  return { controller, first };
}
