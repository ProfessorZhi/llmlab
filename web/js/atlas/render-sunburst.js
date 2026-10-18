/*! web/js/atlas/render-sunburst.js
 *  Two-level sunburst: inner ring = business direction, outer ring = workload.
 *  Both rings are the same drawable selection state as every other chart. */
import { el, clear, t } from '../dom.js';
import { fmt } from '../format.js';
import { sum } from './selectors.js';
import { arc, point } from './geometry.js';
import { RANGE_NAMES } from '../data/demo-fixture.js';
import { bindWorkload, bindGroup } from './binding.js';

export function renderSunburst(svg, ctx) {
  const { state, groups, cols } = ctx;
  clear(svg);
  svg.setAttribute('aria-label', '合成调用两级旭日图。内环为业务方向、外环为九类工作负载；点击筛选。');

  const rows = ctx.visible();
  const total = sum(rows, 'calls');
  const cx = 380;
  const cy = 277;

  for (const r of [105, 158, 166, 253]) {
    el('circle', { cx, cy, r, fill: 'none', stroke: cols.rule(), 'stroke-width': 0.65 }, svg);
  }

  let angle = 0;
  groups.forEach((g) => {
    const entries = rows.filter((d) => d.g === g.id);
    if (!entries.length) return;
    const part = sum(entries, 'calls');
    const start = angle;
    const end = angle + (part / total) * 360;

    const sector = el('path', {
      d: arc(cx, cy, 110, 158, start, end),
      fill: cols.col(g.id), stroke: cols.css('--bg'), 'stroke-width': 2.3, 'fill-opacity': 0.92,
    }, svg);
    bindGroup(sector, g, ctx);

    const span = end - start;
    if (span > 26) {
      const p = point(cx, cy, 132, (start + end) / 2);
      t(svg, g.name, p[0], p[1], span > 85 ? 12 : 10, cols.css('--bg'), { 'text-anchor': 'middle', 'font-weight': 650 });
    }

    let a = start;
    entries.forEach((d) => {
      const de = a + (d.calls / total) * 360;
      const mid = (a + de) / 2;
      const width = de - a;
      const path = el('path', {
        d: arc(cx, cy, 168, 253, a, de),
        fill: cols.col(d.g), stroke: cols.css('--bg'), 'stroke-width': 2.5, 'fill-opacity': 0.77,
      }, svg);
      bindWorkload(path, d, ctx);
      if (width > 14) {
        const p = point(cx, cy, 210, mid);
        const size = width > 50 ? 15 : width > 27 ? 12 : 10;
        t(svg, d.name, p[0], p[1], size, cols.css('--bg'), { 'text-anchor': 'middle', 'font-weight': 610 });
      }
      a = de;
    });
    angle = end;
  });

  el('circle', { cx, cy, r: 100, fill: cols.css('--bg'), stroke: cols.rule(), 'stroke-width': 0.75 }, svg);
  t(svg, `${RANGE_NAMES[state.range]} · DEMO`, cx, cy - 35, 12, cols.dim(), { 'text-anchor': 'middle' });
  t(svg, fmt(total), cx, cy + 8, 47, cols.ink(), { 'text-anchor': 'middle', 'font-weight': 620, 'letter-spacing': '-2px' });
  t(svg, '调用请求', cx, cy + 42, 12, cols.dim(), { 'text-anchor': 'middle' });
  t(svg, '内环 / 业务方向   外环 / 工作负载', 380, 533, 12, cols.dim(), { 'text-anchor': 'middle' });
}
