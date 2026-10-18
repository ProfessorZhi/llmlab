/*! web/js/atlas/render-donut.js
 *  Direction share ring + legend. Always shows ALL directions (unfiltered) so
 *  the reader can see what the current filter is hiding. */
import { el, clear, t, escapeHtml } from '../dom.js';
import { fmt, num } from '../format.js';
import { sum } from './selectors.js';
import { arc } from './geometry.js';
import { bindGroup } from './binding.js';

export function renderDonut(svg, legendHost, ctx) {
  const { groups, cols, state } = ctx;
  clear(svg);

  const rows = ctx.all();
  const total = sum(rows, 'calls');
  let angle = 0;

  groups.forEach((g) => {
    const n = sum(rows.filter((d) => d.g === g.id), 'calls');
    const end = angle + (n / total) * 360;
    const path = el('path', {
      d: arc(100, 100, 60, 91, angle, end),
      fill: cols.col(g.id), stroke: cols.css('--bg'), 'stroke-width': 2,
      'fill-opacity': state.group !== 'all' && state.group !== g.id ? 0.3 : 0.94,
    }, svg);
    bindGroup(path, g, ctx);
    angle = end;
  });

  el('circle', { cx: 100, cy: 100, r: 55, fill: cols.css('--bg') }, svg);
  t(svg, '合成总量', 100, 88, 10, cols.dim(), { 'text-anchor': 'middle' });
  t(svg, fmt(total), 100, 112, 23, cols.ink(), { 'text-anchor': 'middle', 'font-weight': 650 });

  legendHost.replaceChildren();
  groups.forEach((g) => {
    const n = sum(rows.filter((d) => d.g === g.id), 'calls');
    const pct = num((100 * n) / total);
    const b = document.createElement('button');
    b.type = 'button';
    const part = document.createElement('span');
    part.innerHTML = `<i class="atlas-dot" style="background:${cols.col(g.id)}"></i> ${escapeHtml(g.name)}`;
    const count = document.createElement('b');
    count.textContent = `${pct}%`;
    b.append(part, count);
    b.onclick = () => ctx.actions.setGroup(state.group === g.id ? 'all' : g.id);
    b.setAttribute('aria-label', `查看${g.name}，占总数${pct}%`);
    legendHost.append(b);
  });
}
