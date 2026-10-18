/*! web/js/atlas/exporters.js
 *  CSV / JSON builders for the Atlas. Pure string work so the export contents
 *  are asserted in tests. Every file name and every payload carries the DEMO
 *  marker: an exported slice of this page must never be mistaken for a report.
 */
import { DEMO_SOURCE } from '../data/demo-fixture.js';
import { groupName } from './selectors.js';
import { fmt } from '../format.js';

export const CSV_FILENAME = 'llmlab_traffic_atlas_DEMO.csv';
export const JSON_FILENAME = 'llmlab_traffic_atlas_DEMO.json';

/** UTF-8 byte-order mark. Built from its code point so no invisible literal
 *  sits in the source where an editor's "strip BOM" could silently remove it. */
const BOM = String.fromCharCode(0xfeff);

const csvCell = (v) => `"${String(v).replace(/"/g, '""')}"`;

/** RFC-4180-ish CSV with a BOM so spreadsheet apps read the UTF-8 Chinese headers. */
export function buildCsv(rows, groups, state) {
  const header = ['业务方向', '工作负载', '调用请求_DEMO', 'p95毫秒_DEMO', '输出Tokens千_DEMO', '统计窗口', '来源'];
  const lines = [
    header,
    ...rows.map((d) => [
      groupName(groups, d.g), d.name, d.calls, d.p95, d.tokens, state.range, DEMO_SOURCE.kind,
    ]),
  ];
  return BOM + lines.map((r) => r.map(csvCell).join(',')).join('\r\n');
}

/** Structured export mirroring the on-screen slice, with provenance in `meta`. */
export function buildJson(rows, state) {
  return JSON.stringify({
    meta: {
      title: 'Dell LLM Lab · Traffic Atlas',
      source: DEMO_SOURCE.kind,
      status: DEMO_SOURCE.status,
      unit: 'calls',
      window: state.range,
      group: state.group,
      note: DEMO_SOURCE.note,
    },
    rows,
  }, null, 2);
}

/** Trigger a download in the browser. Separated from the builders so tests can
 *  validate file contents without a DOM. */
export function saveFile(name, text, type, doc = document) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = doc.createElement('a');
  a.href = url;
  a.download = name;
  doc.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/** Human-readable label for a heat-cell readout. */
export const heatReadout = (dayName, hour, metric, value) =>
  `${dayName} · ${String(hour).padStart(2, '0')}:00 — 合成${metric === 'volume'
    ? `请求量 ${fmt(value)} 次`
    : `延迟压力 ${fmt(value)} ms`}，不是实时监控`;
