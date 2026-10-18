/*! web/js/calls/insights.js — what the loaded rows say, without inventing anything.
 *
 *  Pure functions over the rows the server already returned. Three rules keep the
 *  numbers honest:
 *
 *   * **Scope is "the loaded rows", always said out loud.** These figures are not
 *     the window total (that is `counts.available`), and the copy says which.
 *   * **A missing measurement is excluded, never zero.** A row with no
 *     `total_ms` does not drag p50 down; it is counted in `latencyKnown` instead.
 *   * **Percentiles are nearest-rank over individual rows**, not averaged across
 *     groups — the same rule the Atlas states for p95.
 *
 *  Exports carry only the metadata columns this view already shows. There is no
 *  prompt, completion or credential to leak, and every cell is guarded against
 *  spreadsheet formula injection.
 */

import { escapeHtml } from '../dom.js';
import { fmt, num, UNKNOWN } from '../format.js';
import { isDemo, statusClass } from './contract.js';

const known = (value) => typeof value === 'number' && Number.isFinite(value);

/** Nearest-rank percentile of a list of finite numbers; null for an empty list. */
export function percentile(values, p) {
  const list = values.filter(known).sort((a, b) => a - b);
  if (!list.length) return null;
  const rank = Math.ceil((p / 100) * list.length);
  return list[Math.min(list.length, Math.max(1, rank)) - 1];
}

/** Aggregate the loaded rows. Every ratio has an explicit denominator. */
export function computeInsights(rows = []) {
  const outcomes = { success: 0, failed: 0, cancelled: 0 };
  const statuses = new Map();
  const models = new Map();
  let tokensIn = 0;
  let tokensOut = 0;
  let tokensKnown = 0;
  for (const row of rows) {
    if (row.outcome in outcomes) outcomes[row.outcome] += 1;
    const cls = statusClass(row.status);
    statuses.set(cls, (statuses.get(cls) ?? 0) + 1);
    const entry = models.get(row.model) ?? { model: row.model, count: 0, latencies: [], tokens: 0, tokensKnown: 0, failed: 0 };
    entry.count += 1;
    if (row.outcome !== 'success') entry.failed += 1;
    if (known(row.total_ms)) entry.latencies.push(row.total_ms);
    const t = row.tokens ?? {};
    if (known(t.input) && known(t.output)) {
      tokensIn += t.input;
      tokensOut += t.output;
      tokensKnown += 1;
      entry.tokens += t.input + t.output;
      entry.tokensKnown += 1;
    }
    models.set(row.model, entry);
  }
  const latencies = rows.map((row) => row.total_ms).filter(known);
  const ttfts = rows.map((row) => row.ttft_ms).filter(known);
  return {
    rows: rows.length,
    outcomes,
    successRate: rows.length ? outcomes.success / rows.length : null,
    latencyKnown: latencies.length,
    p50: percentile(latencies, 50),
    p95: percentile(latencies, 95),
    ttftKnown: ttfts.length,
    ttftP50: percentile(ttfts, 50),
    tokensKnown,
    tokensIn: tokensKnown ? tokensIn : null,
    tokensOut: tokensKnown ? tokensOut : null,
    statuses: [...statuses.entries()].sort((a, b) => b[1] - a[1]).map(([cls, count]) => ({ cls, count })),
    byModel: [...models.values()]
      .map((m) => ({
        model: m.model,
        count: m.count,
        failed: m.failed,
        p95: percentile(m.latencies, 95),
        tokens: m.tokensKnown ? m.tokens : null,
      }))
      .sort((a, b) => b.count - a.count),
  };
}

const ms = (value) => (known(value) ? `${fmt(Math.round(value))} ms` : UNKNOWN);
const pct = (value) => (known(value) ? `${num(value * 100, 1)}%` : UNKNOWN);

/** Insight band: outcome mix, latency percentiles, and a per-model breakdown. */
export function renderInsights(log) {
  const rows = log?.calls ?? [];
  if (!rows.length) return '';
  const s = computeInsights(rows);
  const total = s.rows;
  const seg = (count, cls, label) => (count
    ? `<span class="calls-mix-seg ${cls}" style="flex:${count}" title="${escapeHtml(label)} ${fmt(count)} / ${fmt(total)}"></span>`
    : '');
  const peak = Math.max(...s.byModel.map((m) => m.count), 1);
  const models = s.byModel.map((m) => (
    `<tr><th scope="row"><span class="doc-id">${escapeHtml(m.model ?? '—')}</span></th>` +
    `<td><span class="calls-bar" style="--w:${(m.count / peak * 100).toFixed(1)}%"></span></td>` +
    `<td class="num">${fmt(m.count)}</td><td class="num">${fmt(m.failed)}</td>` +
    `<td class="num">${ms(m.p95)}</td><td class="num">${m.tokens === null ? UNKNOWN : fmt(m.tokens)}</td></tr>`
  )).join('');
  return (
    `<div class="calls-insights" data-calls-insights>` +
    `<div class="calls-insight-col">` +
    `<div class="tiny">结果构成 · 已加载 ${fmt(total)} 行</div>` +
    `<div class="calls-mix" role="img" aria-label="成功 ${s.outcomes.success}，失败 ${s.outcomes.failed}，取消 ${s.outcomes.cancelled}">` +
    seg(s.outcomes.success, 'ok', '成功') + seg(s.outcomes.failed, 'bad', '失败') + seg(s.outcomes.cancelled, 'mute', '取消') +
    `</div>` +
    `<dl class="calls-kv">` +
    `<div><dt>成功率</dt><dd>${pct(s.successRate)}</dd></div>` +
    `<div><dt>总耗时 p50</dt><dd>${ms(s.p50)}</dd></div>` +
    `<div><dt>总耗时 p95</dt><dd>${ms(s.p95)}</dd></div>` +
    `<div><dt>首字 p50</dt><dd>${ms(s.ttftP50)}</dd></div>` +
    `</dl>` +
    `<p class="footnote">百分位按单条调用的最近秩计算；${fmt(total - s.latencyKnown)} 行未记录总耗时、${fmt(total - s.ttftKnown)} 行无首字时间，均不计入（不当作 0）。状态码：` +
    s.statuses.map((x) => `<b class="mono">${escapeHtml(x.cls)}</b> ${fmt(x.count)}`).join(' · ') + `</p>` +
    `</div>` +
    `<div class="calls-insight-col">` +
    `<div class="tiny">按模型 · 已加载行</div>` +
    `<div class="tablewrap" style="border-top:0"><table class="calls-model-table"><thead><tr><th scope="col">模型</th><th scope="col"><span class="sr-only">占比</span></th>` +
    `<th scope="col" class="num">调用</th><th scope="col" class="num">非成功</th><th scope="col" class="num">p95</th><th scope="col" class="num">Token</th></tr></thead>` +
    `<tbody>${models}</tbody></table></div>` +
    `</div></div>`
  );
}

/** The detail drawer body for one row. Metadata only — there is no content. */
export function renderCallDetail(row, log) {
  const t = row.tokens ?? {};
  const item = (label, value) => `<div class="statusitem"><div class="tiny">${escapeHtml(label)}</div><strong class="mono">${value}</strong></div>`;
  const text = (value) => (value === null || value === undefined || value === '' ? UNKNOWN : escapeHtml(String(value)));
  const demo = isDemo(log)
    ? '<div class="guard warn">SYNTHETIC / DEMO：这一行由浏览器内的合成 fixture 生成，不对应任何真实请求。</div>'
    : '';
  return (
    demo +
    `<div class="statusrows calls-detail">` +
    item('request_id', text(row.request_id)) +
    item('call_id', text(row.call_id)) +
    item('时间 (UTC)', text(row.ts)) +
    item('状态 / 结果', `${text(row.status)} · ${text(row.outcome)}`) +
    item('模型', text(row.model)) +
    item('路径', `${text(row.method)} ${text(row.path)}${row.stream ? ' · stream' : ''}`) +
    item('key', text(row.key_id)) +
    item('项目 / 工作负载', `${text(row.project)} · ${text(row.workload)}`) +
    item('Token 输入 / 输出', known(t.input) && known(t.output) ? `${fmt(t.input)} / ${fmt(t.output)}` : UNKNOWN) +
    item('排队', ms(row.queue_ms)) +
    item('首字 (TTFT)', ms(row.ttft_ms)) +
    item('上游 / 总耗时', `${ms(row.upstream_ms)} / ${ms(row.total_ms)}`) +
    `</div>` +
    `<p class="footnote">正文不在本视图的采集范围内（content.included = false）。call_id 的来源：<span class="mono">${text(row.call_id_basis)}</span>。</p>`
  );
}

/** The columns an export carries — exactly the metadata the table shows. */
export const EXPORT_COLUMNS = Object.freeze([
  'ts', 'request_id', 'call_id', 'status', 'outcome', 'model', 'key_id', 'project', 'workload',
  'path', 'method', 'stream', 'tokens_input', 'tokens_output', 'tokens_total',
  'queue_ms', 'ttft_ms', 'upstream_ms', 'total_ms',
]);

const flatten = (row) => ({
  ...row,
  tokens_input: row.tokens?.input ?? null,
  tokens_output: row.tokens?.output ?? null,
  tokens_total: row.tokens?.total ?? null,
});

/** One CSV cell. A leading =,+,-,@ is neutralised so a spreadsheet never runs it. */
export function csvCell(value) {
  if (value === null || value === undefined) return '';
  let text = String(value);
  if (/^[=+\-@\t\r]/.test(text) && !/^-?\d+(\.\d+)?$/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function callsToCsv(log) {
  const rows = (log?.calls ?? []).map(flatten);
  const lines = [EXPORT_COLUMNS.join(',')];
  for (const row of rows) lines.push(EXPORT_COLUMNS.map((c) => csvCell(row[c])).join(','));
  return `${lines.join('\r\n')}\r\n`;
}

export function callsToJson(log, { exportedAt = new Date().toISOString() } = {}) {
  return JSON.stringify({
    meta: {
      view: 'my-calls',
      schema_version: log?.schema_version ?? null,
      synthetic: log?.synthetic === true,
      transport: log?.transport?.mode ?? null,
      subject: log?.scope?.subject ?? null,
      window: log?.window ?? null,
      filters: log?.filters ?? null,
      rows: (log?.calls ?? []).length,
      available: log?.counts?.available ?? null,
      exported_at: exportedAt,
      note: '仅元数据；不含正文。rows 是已加载的行，不是窗口总量。',
    },
    calls: (log?.calls ?? []).map((row) => EXPORT_COLUMNS.reduce((acc, c) => {
      acc[c] = flatten(row)[c] ?? null;
      return acc;
    }, {})),
  }, null, 2);
}

/** File name that says what it is: demo vs live, and the export date. */
export function exportName(log, ext, today = new Date()) {
  const tag = isDemo(log) ? 'DEMO' : 'live';
  return `llmlab_my_calls_${tag}_${today.toISOString().slice(0, 10)}.${ext}`;
}
