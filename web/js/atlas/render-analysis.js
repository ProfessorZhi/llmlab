/*! web/js/atlas/render-analysis.js
 *  Pareto concentration curve and the calls × p95 scatter.
 *  Both re-derive from the *current* filtered selection, so they always agree
 *  with the ranking table beside them. */
import { el, clear, t } from '../dom.js';
import { fmt, num } from '../format.js';
import { sum, paretoCutoff } from './selectors.js';
import { bindWorkload } from './binding.js';
import { renderChartEmpty } from './render-empty.js';

const EMPTY_MSG = '当前范围没有可展示的合成样本';

export function renderPareto(svg, noteEl, ctx) {
  const { cols } = ctx;
  const rows = [...ctx.visible()].sort((a, b) => b.calls - a.calls);
  const n = rows.length;
  if (!n) {
    renderChartEmpty(svg, cols, EMPTY_MSG);
    noteEl.textContent = '—';
    return;
  }

  clear(svg);
  const W = 610; const H = 300; const L = 46; const R = 18; const T = 26; const B = 38;
  const PW = W - L - R; const PH = H - T - B;
  const total = sum(rows, 'calls');

  let cum = 0;
  const points = [{ x: L, y: T + PH, share: 0 }];
  rows.forEach((d, i) => {
    cum += d.calls;
    points.push({ x: L + ((i + 1) / n) * PW, y: T + PH - (cum / total) * PH, share: cum / total, d });
  });

  [0, 0.25, 0.5, 0.75, 1].forEach((v) => {
    const y = T + PH - v * PH;
    el('line', {
      x1: L, y1: y, x2: W - R, y2: y, stroke: cols.rule(),
      'stroke-dasharray': v === 0 ? '0' : '2 5', 'stroke-width': 0.65,
    }, svg);
    t(svg, `${Math.round(v * 100)}%`, L - 7, y, 11, cols.dim(), { 'text-anchor': 'end' });
  });

  const y80 = T + PH - 0.8 * PH;
  el('line', {
    x1: L, y1: y80, x2: W - R, y2: y80, stroke: cols.css('--atlas-a'),
    'stroke-width': 1, 'stroke-dasharray': '5 6',
  }, svg);

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ');
  el('path', { d: `${line} L${W - R} ${T + PH} L${L} ${T + PH}Z`, fill: cols.css('--atlas-a'), 'fill-opacity': 0.08 }, svg);

  const route = el('path', {
    d: line, fill: 'none', stroke: cols.css('--atlas-a'), 'stroke-width': 2.5, 'stroke-linecap': 'round',
  }, svg);
  if (!ctx.reducedMotion() && route.getTotalLength) {
    const len = route.getTotalLength();
    route.animate([
      { strokeDasharray: `${len} ${len}`, strokeDashoffset: len },
      { strokeDasharray: `${len} ${len}`, strokeDashoffset: 0 },
    ], { duration: 550, easing: 'cubic-bezier(.2,.8,.2,1)' });
  }

  points.slice(1).forEach((p, i) => {
    const circle = el('circle', {
      cx: p.x, cy: p.y, r: 4, fill: cols.css('--atlas-a'), stroke: cols.css('--bg'), 'stroke-width': 1.5,
    }, svg);
    bindWorkload(circle, p.d, ctx);
    t(svg, String(i + 1), p.x, H - 15, 10, cols.dim(), { 'text-anchor': 'middle' });
  });

  const { cutoff, share } = paretoCutoff(rows);
  noteEl.textContent = `前 ${cutoff} / ${n} 类负载贡献 ≥80% 的演示请求 · 累计 ${num(share * 100)}%`;
}

export function renderScatter(svg, ctx) {
  const { cols } = ctx;
  const rows = ctx.visible();
  if (!rows.length) {
    renderChartEmpty(svg, cols, EMPTY_MSG);
    return;
  }
  clear(svg);

  const W = 610; const H = 300; const L = 58; const R = 26; const T = 26; const B = 44;
  const PW = W - L - R; const PH = H - T - B;
  const known = rows.filter((d) => Number.isFinite(d.p95));
  const maxC = Math.max(...rows.map((d) => d.calls), 1);
  const maxP = Math.max(...(known.length ? known : rows).map((d) => d.p95 ?? 0), 1) * 1.16;
  const maxT = Math.max(...rows.map((d) => d.tokens ?? 0), 1);

  const X = (v) => L + (Math.log10(v + 1) / Math.log10(maxC + 1)) * PW;
  const Y = (v) => T + PH - (v / maxP) * PH;

  [0, 0.25, 0.5, 0.75, 1].forEach((a) => {
    const y = T + PH - a * PH;
    el('line', { x1: L, y1: y, x2: W - R, y2: y, stroke: cols.rule(), 'stroke-width': 0.65 }, svg);
    t(svg, `${Math.round(maxP * a)}ms`, L - 9, y, 10, cols.dim(), { 'text-anchor': 'end' });
  });

  [1, 10, 100, 1000, 10000].filter((v) => v <= maxC * 1.1).forEach((v) => {
    const x = X(v);
    el('line', { x1: x, y1: T, x2: x, y2: T + PH, stroke: cols.rule(), 'stroke-width': 0.6, 'stroke-dasharray': '2 5' }, svg);
    t(svg, fmt(v), x, H - 22, 10, cols.dim(), { 'text-anchor': 'middle' });
  });

  t(svg, '调用请求 / 对数', W - R, H - 10, 11, cols.dim(), { 'text-anchor': 'end' });

  // Draw smallest-token marks first so the biggest read on top.
  [...rows].sort((a, b) => (a.tokens ?? 0) - (b.tokens ?? 0)).forEach((d) => {
    if (!Number.isFinite(d.p95)) return; // no latency sample → no vertical position
    const circle = el('circle', {
      cx: X(d.calls), cy: Y(d.p95), r: 5 + (8 * (d.tokens ?? 0)) / maxT,
      fill: cols.col(d.g), 'fill-opacity': 0.78, stroke: cols.css('--bg'), 'stroke-width': 2,
    }, svg);
    bindWorkload(circle, d, ctx);
  });
}
