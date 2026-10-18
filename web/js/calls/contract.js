/*! web/js/calls/contract.js — the `GET /api/atlas/calls` payload, checked before it is shown.
 *
 *  This module is the browser-side half of the contract documented in
 *  `docs/portal-call-log-contract.md`. It holds no DOM and no network, so it can
 *  be unit-tested under plain Node — the same shape as `../data/contract.js`.
 *
 *  Why validate at all, when the server already produced the DTO? Because the
 *  page must not be the one place where a claim goes unchecked. Three specific
 *  things are refused here rather than rendered:
 *
 *  1. **Content.** `content.included` must be `false`. A payload that says
 *     otherwise is not shown in a "content: yes" state — the whole client
 *     refuses the payload. This is the last line of defence behind the server's
 *     `validate_self_calls_dto`, and it is deliberately not a soft warning.
 *  2. **A live claim on synthetic data.** `transport.mode === 'live'` together
 *     with `synthetic === true` is a contradiction: this build has no gateway,
 *     so that combination is a bug or a forgery, never a state to render.
 *  3. **Rows outside the paid-for keys.** Every row's `key_id` must be in
 *     `scope.keys`. A row from someone else's key is the exact failure this
 *     whole feature exists to prevent, so it is checked here too.
 *
 *  Missing values stay `null` all the way to the table (rendered as `—`), never
 *  `0`: "not measured" and "measured zero" are different facts.
 */

/** The only schema this client understands. A different version is refused
 *  rather than parsed optimistically — the per-call log and the project
 *  aggregate have deliberately different field meanings. */
export const CALL_LOG_SCHEMA = 'atlas-calls/1';

/** Content-access states, as recorded server-side. A status, never content. */
export const CONSENT_STATES = Object.freeze({
  ABSENT: 'absent',
  GRANTED: 'granted',
  WITHDRAWN: 'withdrawn',
});

/** Reasons the server gives for `content.included === false`. */
export const CONTENT_REASONS = Object.freeze({
  CONSENT_ABSENT: 'consent_absent',
  CONSENT_WITHDRAWN: 'consent_withdrawn',
  NOT_COLLECTED: 'not_collected_by_metadata_pipeline',
});

export const TRANSPORT_MODES = Object.freeze({ DEMO: 'demo', LIVE: 'live' });

/** What the page should draw. `DENIED` and `SIGNED_OUT` are separate states on
 *  purpose: 403 means "ask an operator", 401 means "sign in". */
export const LOG_STATES = Object.freeze({
  READY: 'ready',
  EMPTY: 'empty',
  UNKNOWN: 'unknown',
  DENIED: 'denied',
  SIGNED_OUT: 'signed_out',
  // Not answers about data: "asked, not told yet", and "this deployment does
  // not provide this route" (404/501/503). Neither may render a number.
  LOADING: 'loading',
  NOT_CONFIGURED: 'not_configured',
});

/**
 * Statuses that mean this deployment does not provide the route.
 *
 * 404/501: the route itself is absent. 503: the route exists but the BFF's
 * `*_adapter_not_attached` says the module behind it was not wired in this
 * deployment — explicitly "本部署没有这条功能", not "服务崩了"
 * (backend/bff/evaluation_adapter.py). All three are deployment facts, so they
 * share NOT_CONFIGURED and keep their own `http-<status>` reason; none of them
 * is a retryable unknown, and none may be rendered as an empty list.
 */
export const NOT_CONFIGURED_STATUSES = Object.freeze([404, 501, 503]);

const CONTENT_REASON_LABELS = Object.freeze({
  [CONTENT_REASONS.CONSENT_ABSENT]: '你尚未开启正文访问（未授权）',
  [CONTENT_REASONS.CONSENT_WITHDRAWN]: '正文访问授权已撤回',
  [CONTENT_REASONS.NOT_COLLECTED]: '已记录你的 opt-in；但本管道不采集正文',
});

const CONSENT_LABELS = Object.freeze({
  [CONSENT_STATES.ABSENT]: '未开启',
  [CONSENT_STATES.GRANTED]: '已开启',
  [CONSENT_STATES.WITHDRAWN]: '已撤回',
});

const OUTCOMES = Object.freeze(['success', 'failed', 'cancelled']);

/** A row has exactly these fields; anything else is a contract violation, not
 *  extra detail to display. Kept as a whitelist so a future server-side `debug`
 *  field cannot ride along unnoticed. */
const ROW_FIELDS = Object.freeze([
  'call_id', 'call_id_basis', 'request_id', 'ts', 'key_id', 'project', 'workload',
  'model', 'path', 'method', 'stream', 'status', 'outcome', 'tokens',
  'queue_ms', 'ttft_ms', 'upstream_ms', 'total_ms',
]);

/** Field names that must never appear on a row. `label` is client-supplied and
 *  carries no authority anywhere else in this codebase; it does not get to
 *  become a second identity here. */
const FORBIDDEN_ROW_FIELDS = Object.freeze([
  'prompt', 'prompts', 'completion', 'completions', 'messages', 'content',
  'text', 'input_text', 'output_text', 'label',
]);

export const consentLabel = (state) => CONSENT_LABELS[state] ?? '未知状态';

export const contentReasonLabel = (reason) =>
  CONTENT_REASON_LABELS[reason] ?? '正文不在本管道的采集范围内';

/** HTTP status class for display only. Returns `'—'` for a non-number. */
export function statusClass(status) {
  if (typeof status !== 'number' || !Number.isInteger(status)) return '—';
  return `${Math.floor(status / 100)}xx`;
}

/** A number or `null`. `Number.isFinite` rejects `NaN` and `Infinity`, which
 *  JSON cannot carry anyway — a payload that produced one is refused. */
const finiteOrNull = (value) =>
  value === null || value === undefined ? null : (Number.isFinite(value) ? value : null);

function checkRow(row, index, allowedKeys, allowedProjects, errors) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) {
    errors.push(`calls[${index}] must be an object`);
    return;
  }
  for (const field of ROW_FIELDS) {
    if (!(field in row)) errors.push(`calls[${index}] is missing ${field}`);
  }
  for (const field of Object.keys(row)) {
    if (!ROW_FIELDS.includes(field)) errors.push(`calls[${index}] has unexpected field ${field}`);
    if (FORBIDDEN_ROW_FIELDS.includes(String(field).toLowerCase())) {
      errors.push(`calls[${index}] carries a field this view must never show: ${field}`);
    }
  }
  if (!allowedKeys.has(row.key_id)) {
    errors.push(`calls[${index}] belongs to a key outside this view`);
  }
  if (!allowedProjects.has(row.project)) {
    errors.push(`calls[${index}] names a project outside this view`);
  }
  const tokens = row.tokens;
  if (!tokens || typeof tokens !== 'object' || Array.isArray(tokens)) {
    errors.push(`calls[${index}].tokens must be an object`);
    return;
  }
  const input = finiteOrNull(tokens.input);
  const output = finiteOrNull(tokens.output);
  const total = finiteOrNull(tokens.total);
  if ((input === null || output === null) && total !== null) {
    // The server promises this; re-checking costs nothing and stops a "0" that
    // was invented somewhere in between from reaching the reader.
    errors.push(`calls[${index}].tokens.total must be null when a side is null`);
  }
}

/**
 * Check a decoded `data.call_log` object.
 *
 * @param {unknown} raw
 * @returns {{ok: boolean, errors: string[], log: object|null}}
 */
export function validateCallLog(raw) {
  const errors = [];
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, errors: ['the payload is not an object'], log: null };
  }
  if (raw.schema_version !== CALL_LOG_SCHEMA) {
    errors.push(`unsupported schema_version: ${String(raw.schema_version)}`);
  }

  const transport = raw.transport;
  if (!transport || typeof transport !== 'object') {
    errors.push('transport must be an object');
  } else if (!Object.values(TRANSPORT_MODES).includes(transport.mode)) {
    errors.push(`transport.mode is not supported: ${String(transport.mode)}`);
  } else if (transport.mode === TRANSPORT_MODES.LIVE && raw.synthetic === true) {
    errors.push('a synthetic payload may not claim to be live');
  } else if (transport.mode === TRANSPORT_MODES.LIVE && transport.gateway_attached !== true) {
    errors.push('a live payload must state that the gateway is attached');
  }

  const content = raw.content;
  if (!content || typeof content !== 'object') {
    errors.push('content must be an object');
  } else if (content.included !== false) {
    errors.push('content.included must be false: this view never carries prompt or completion text');
  } else if (!Object.values(CONSENT_STATES).includes(content.consent)) {
    errors.push(`content.consent is not a known state: ${String(content.consent)}`);
  }

  const scope = raw.scope;
  const allowedKeys = new Set();
  const allowedProjects = new Set();
  if (!scope || typeof scope !== 'object' || !Array.isArray(scope.keys) || !scope.keys.length) {
    errors.push('scope.keys must be a non-empty list');
  } else {
    scope.keys.forEach((key) => allowedKeys.add(key));
  }
  if (!scope || !Array.isArray(scope.projects) || !scope.projects.length) {
    errors.push('scope.projects must be a non-empty list');
  } else {
    scope.projects.forEach((project) => allowedProjects.add(project));
  }

  const calls = raw.calls;
  if (!Array.isArray(calls)) {
    errors.push('calls must be a list');
  } else if (allowedKeys.size && allowedProjects.size) {
    calls.forEach((row, index) => checkRow(row, index, allowedKeys, allowedProjects, errors));
  }

  const counts = raw.counts;
  if (!counts || typeof counts !== 'object') {
    errors.push('counts must be an object');
  } else if (Array.isArray(calls) && counts.returned !== calls.length) {
    errors.push('counts.returned disagrees with the calls list');
  }

  const ok = errors.length === 0;
  return { ok, errors, log: ok ? raw : null };
}

/** Transport statuses that are answers about *access* rather than about data.
 *  They are not retried and not rendered as an empty list: 401 means "sign in",
 *  403 means "ask an operator", and neither is a statement about your calls. */
export const STATE_CHANGING_STATUSES = Object.freeze([401, 403]);

/** A page state that carries no data — used for `empty`, `unknown`, `denied`
 *  and `signed_out`. The `calls` list is empty by construction, never a fiction. */
export function emptyLog(state, reason) {
  return {
    state,
    reason: reason ?? null,
    transport: null,
    scope: null,
    counts: null,
    coverage: null,
    content: null,
    calls: [],
    displayHints: [],
    notes: [],
  };
}

/**
 * Decide which of the page states a validated log represents.
 *
 * `empty` is a real answer — "this window holds no calls of yours" — and is
 * never conflated with `unknown`, which means "we could not find out".
 *
 * A state produced by :func:`emptyLog` carries its own `state`, and it wins:
 * deriving "empty" from an empty `calls` list would relabel a signed-out or
 * denied page as *"this window holds no calls of yours"*. That is not a missing
 * answer, it is a wrong one — 401 and 403 say nothing about your calls.
 */
export function stateFor(log) {
  if (!log) return LOG_STATES.UNKNOWN;
  const declared = Object.values(LOG_STATES).includes(log.state) ? log.state : null;
  if (declared && declared !== LOG_STATES.READY && declared !== LOG_STATES.EMPTY) {
    return declared;
  }
  return (log.calls ?? []).length ? LOG_STATES.READY : LOG_STATES.EMPTY;
}

/** Display totals, computed from the rows that are actually present. Nothing
 *  here is extrapolated from `counts.available`. */
export function summarise(log) {
  const rows = log?.calls ?? [];
  let input = 0;
  let output = 0;
  let tokensKnown = 0;
  let queueSum = 0;
  let queueKnown = 0;
  let failed = 0;
  for (const row of rows) {
    const tokens = row.tokens ?? {};
    if (Number.isFinite(tokens.input) && Number.isFinite(tokens.output)) {
      input += tokens.input;
      output += tokens.output;
      tokensKnown += 1;
    }
    if (Number.isFinite(row.queue_ms)) {
      queueSum += row.queue_ms;
      queueKnown += 1;
    }
    if (row.outcome !== 'success') failed += 1;
  }
  return {
    rows: rows.length,
    failed,
    input: tokensKnown ? input : null,
    output: tokensKnown ? output : null,
    tokensKnown,
    queueKnown,
    queueAvg: queueKnown ? queueSum / queueKnown : null,
  };
}

/** The filter values a control may offer, derived from the payload itself so the
 *  page never invents an option the server cannot answer. */
export function filterOptions(log) {
  return {
    keys: log?.scope?.keys ?? [],
    outcomes: OUTCOMES,
  };
}

/** Whether the window is a demo window rather than a real one. */
export const isDemo = (log) => log?.transport?.mode === TRANSPORT_MODES.DEMO;
