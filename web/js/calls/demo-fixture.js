/*! web/js/calls/demo-fixture.js — an in-process, SYNTHETIC `atlas-calls/1` answerer.
 *
 *  Reached only through the explicit `?demo=1` sample opt-in (see
 *  `../demo-mode.js`). It never touches the network: it is handed to the call
 *  log controller as its `fetchImpl`, parses the same query string the real
 *  `GET /api/atlas/calls` would receive, and answers with a payload that passes
 *  `validateCallLog` — so the filters, pagination, insights, detail drawer and
 *  exports are exercised against the real contract rather than a bespoke shape.
 *
 *  Honesty markers, all of which the contract or the renderer checks:
 *    * `synthetic: true` and `transport.mode = 'demo'` (a synthetic payload may
 *      not claim to be live — the validator refuses it);
 *    * `gateway_attached: false`;
 *    * every subject, key, project and model name carries a `demo-` prefix, so
 *      a screenshot can never be mistaken for a real principal's history;
 *    * `content.included = false` — like the real view, no prompt or completion.
 *
 *  Deterministic: the rows are a pure function of `anchor` (the minute the page
 *  was opened, floored), so a reload shows the same history and a test can assert
 *  exact numbers.
 */

import { CALL_LOG_SCHEMA, CONSENT_STATES, CONTENT_REASONS, TRANSPORT_MODES } from './contract.js';

export const DEMO_SUBJECT = 'demo-user-a';
export const DEMO_KEYS = Object.freeze(['demo-key-cli', 'demo-key-notebook']);
export const DEMO_PROJECTS = Object.freeze(['demo-project']);
export const DEMO_MODELS = Object.freeze(['demo-llm-a', 'demo-llm-c', 'demo-embed-b']);
const WORKLOADS = Object.freeze(['chat', 'resume-parse', 'code-assist', 'eval-batch']);

const HOUR = 3600_000;
const DAY = 24 * HOUR;
/** How far back the synthetic history reaches. */
export const DEMO_HISTORY_DAYS = 7;
const MAX_LIMIT = 200;

/** mulberry32 — tiny, seeded, good enough for a fixture. */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

/** Build the whole synthetic history, newest first. */
export function buildDemoCalls(anchor) {
  const next = rng(20261016);
  const rows = [];
  let ts = anchor - 90_000;
  let n = 0;
  while (ts > anchor - DEMO_HISTORY_DAYS * DAY) {
    const hour = new Date(ts).getUTCHours();
    // Busier in the (UTC+8) working day, quiet overnight — a plausible shape,
    // not a measured one.
    const busy = hour >= 1 && hour <= 11;
    ts -= Math.round((busy ? 6 : 22) * 60_000 * (0.3 + next() * 1.4));
    n += 1;
    const model = DEMO_MODELS[next() < 0.58 ? 0 : next() < 0.7 ? 1 : 2];
    const embed = model === 'demo-embed-b';
    const roll = next();
    const outcome = roll < 0.9 ? 'success' : roll < 0.965 ? 'failed' : 'cancelled';
    const status = outcome === 'success' ? 200 : outcome === 'cancelled' ? 499 : (next() < 0.6 ? 429 : 503);
    const input = Math.round(embed ? 80 + next() * 400 : 180 + next() * 2400);
    const output = embed ? 0 : Math.round(60 + next() * 900);
    const tokensKnown = outcome !== 'cancelled' && next() > 0.03;
    const queue = next() < 0.05 ? null : Math.round(next() ** 2 * 180);
    const ttft = embed || next() < 0.04 ? null : Math.round(140 + next() ** 1.6 * 900);
    const upstream = Math.round((embed ? 40 : 600) + next() ** 1.8 * (embed ? 220 : 4200));
    const total = outcome === 'failed' && status === 429 ? Math.round(4 + next() * 20) : upstream + (queue ?? 0) + 12;
    const call = String(n).padStart(5, '0');
    rows.push({
      call_id: `call_demo_${call}`,
      call_id_basis: 'derived-from-metadata-fields',
      request_id: next() < 0.04 ? null : `req-DEMO-${call}`,
      ts: iso(ts),
      key_id: DEMO_KEYS[next() < 0.7 ? 0 : 1],
      project: DEMO_PROJECTS[0],
      workload: embed ? 'eval-batch' : WORKLOADS[Math.floor(next() * 3)],
      model,
      path: embed ? '/v1/embeddings' : '/v1/chat/completions',
      method: 'POST',
      stream: !embed && next() < 0.7,
      status,
      outcome,
      tokens: tokensKnown
        ? { input, output, total: input + output }
        : { input: null, output: null, total: null },
      queue_ms: queue,
      ttft_ms: outcome === 'success' ? ttft : null,
      upstream_ms: outcome === 'failed' && status === 429 ? null : upstream,
      total_ms: total,
    });
  }
  return rows;
}

const parseInstant = (value) => {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
};

/**
 * Answer one `GET /api/atlas/calls?...` with a SYNTHETIC `atlas-calls/1` payload.
 * @param {string} url  path + query, exactly as the controller builds it
 * @param {number} anchor  epoch ms the history ends at
 */
export function answerDemoCalls(url, anchor) {
  const query = new URL(url, 'http://demo.invalid').searchParams;
  const to = parseInstant(query.get('to')) ?? anchor;
  const from = parseInstant(query.get('from')) ?? to - DAY;
  const limit = Math.min(MAX_LIMIT, Math.max(1, Number(query.get('limit')) || 50));
  const key = query.get('key');
  const outcome = query.get('outcome');
  const model = query.get('model');
  const beforeTs = query.get('before_ts');
  const beforeId = query.get('before_call_id');

  if (key && !DEMO_KEYS.includes(key)) {
    // Same answer the real BFF gives for a key outside the caller's scope.
    return { status: 403, body: { error: 'key_not_in_scope' } };
  }

  const all = buildDemoCalls(anchor);
  const windowRows = all.filter((row) => {
    const at = Date.parse(row.ts);
    return at >= from && at < to;
  });
  const matching = windowRows.filter((row) => (
    (!key || row.key_id === key)
    && (!outcome || row.outcome === outcome)
    && (!model || row.model === model)
  ));
  let start = 0;
  if (beforeTs && beforeId) {
    const at = matching.findIndex((row) => row.ts === beforeTs && row.call_id === beforeId);
    start = at < 0 ? matching.length : at + 1;
  }
  const page = matching.slice(start, start + limit);
  const truncated = start + page.length < matching.length;
  const last = page[page.length - 1];
  const unknown = (pick) => page.filter(pick).length;

  return {
    status: 200,
    body: {
      data: {
        call_log: {
          source: 'synthetic-preview',
          synthetic: true,
          schema_version: CALL_LOG_SCHEMA,
          transport: { mode: TRANSPORT_MODES.DEMO, gateway_attached: false, synthetic: true, label: 'DEMO / 合成数据' },
          scope: {
            subject: DEMO_SUBJECT,
            roles: ['member'],
            scopes: ['atlas:calls:self:read'],
            projects: [...DEMO_PROJECTS],
            keys: [...DEMO_KEYS],
            requested_key: key || null,
            revoked_keys: [],
            cross_key: !key,
            content_consent: CONSENT_STATES.ABSENT,
          },
          window: { from: iso(from), to: iso(to) },
          filters: { outcome: outcome || 'all', model: model || null, limit, before: beforeTs || null },
          coverage: {
            rows: 'measured',
            tokens_unknown_rows: unknown((row) => row.tokens.total === null),
            ttft_unknown_rows: unknown((row) => row.ttft_ms === null),
            queue_unknown_rows: unknown((row) => row.queue_ms === null),
            request_id_absent_rows: unknown((row) => row.request_id === null),
          },
          counts: {
            returned: page.length,
            available: matching.length,
            truncated,
            next_before: truncated && last ? { ts: last.ts, call_id: last.call_id } : null,
            window_excluded: all.length - windowRows.length,
            revoked_key_rows: 0,
            unattributed_rows: 0,
          },
          content: {
            included: false,
            consent: CONSENT_STATES.ABSENT,
            reason: CONTENT_REASONS.CONSENT_ABSENT,
            opt_in_available: false,
            note: '调用日志只包含元数据；本演示不生成任何正文。',
          },
          calls: page,
          display_hints: ['SYNTHETIC / DEMO：这是浏览器内生成的合成调用，不是任何真实 Gateway 的记录，不得用于对账或容量决策。'],
          notes: ['模型、key、项目与主体名称都带 demo- 前缀；真实数据只来自已接线的同源 BFF。'],
        },
      },
    },
  };
}

/** A `fetch`-shaped function backed by the fixture. Never touches the network. */
export function createDemoCallsFetch({ now = () => Date.now() } = {}) {
  const anchor = Math.floor(now() / 60_000) * 60_000;
  return async (url) => {
    const { status, body } = answerDemoCalls(String(url), anchor);
    return {
      status,
      ok: status >= 200 && status < 300,
      json: async () => body,
    };
  };
}
