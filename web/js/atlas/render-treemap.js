/*! web/js/atlas/render-treemap.js
 *  Two-level treemap: direction blocks, then one rectangle per workload. */
import { el, clear, t } from '../dom.js';
import { fmt } from '../format.js';
import { sum } from './selectors.js';
import { squarify } from './squarify.js';
import { bindWorkload } from './binding.js';

const FRAME = { x: 11, y: 15, w: 738, h: 510 };

export function renderTreemap(svg, ctx) {
  const { groups, cols } = ctx;
  clear(svg);
  svg.setAttribute('aria-label', '合成请求量矩形图，可点击色块查看工作负载');

  const rows = ctx.visible();
  const dirs = groups
    .map((g) => ({ ...g, value: sum(rows.filter((d) => d.g === g.id), 'calls') }))
    .filter((g) => g.value);

  const blocks = squarify(dirs, FRAME);
  // The prototype picked label ink per theme rather than per fill colour; kept
  // as-is so the two trees render identically.
  const darkTheme = document.documentElement.dataset.theme === 'dark';
  const textFill = darkTheme ? '#15221c' : '#fffef9';

  blocks.forEach((b) => {
    el('rect', {
      x: b.x, y: b.y, width: b.w, height: b.h, rx: 3,
      fill: cols.css('--atlas-wash'), stroke: cols.rule(), 'stroke-width': 1,
    }, svg);

    const pad = Math.max(4, Math.min(10, b.w * 0.025));
    const top = b.h > 80 ? 29 : 7;
    if (b.h > 55 && b.w > 115) t(svg, b.name, b.x + pad, b.y + 17, 13, cols.ink(), { 'font-weight': 650 });

    const leaves = rows.filter((d) => d.g === b.id).map((d) => ({ ...d, value: d.calls }));
    if (!leaves.length) return;

    const itemRects = squarify(leaves, {
      x: b.x + pad, y: b.y + top, w: Math.max(1, b.w - 2 * pad), h: Math.max(1, b.h - top - pad),
    });
    itemRects.forEach((r) => {
      const inner = el('rect', {
        x: r.x + 1.2, y: r.y + 1.2,
        width: Math.max(1, r.w - 2.4), height: Math.max(1, r.h - 2.4),
        rx: 2, fill: cols.col(r.g), 'fill-opacity': 0.85,
      }, svg);
      bindWorkload(inner, r, ctx);
      if (r.w > 75 && r.h > 48) {
        t(svg, r.name, r.x + 10, r.y + 19, Math.min(13, r.w / 7), textFill, { 'font-weight': 650 });
        if (r.h > 75) t(svg, `${fmt(r.calls)} 次`, r.x + 10, r.y + 43, 17, textFill, { 'font-weight': 610 });
      }
    });
  });
}
