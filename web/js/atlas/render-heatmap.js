/*! web/js/atlas/render-heatmap.js
 *  7×24 activity matrix. Arrow keys walk the grid, Enter/Space select a cell —
 *  the matrix is a real keyboard grid, not just a picture. */
import { el, clear, t } from '../dom.js';
import { fmt } from '../format.js';
import { heatDayLabel, HEAT_DAYS, HOUR_COUNT, heatCell, clamp } from './heat.js';
import { heatReadout } from './exporters.js';

const GRID = { left: 54, top: 30, dx: 32, dy: 29, size: 24 };

export function renderHeatmap(svg, readoutEl, ctx) {
  const { state, cols } = ctx;
  clear(svg);

  const metric = state.heat;
  const bg = cols.css('--atlas-wash');
  const accent = cols.css(metric === 'volume' ? '--atlas-b' : '--atlas-a');
  const { left: l, top, dx, dy, size: sz } = GRID;

  for (let h = 0; h < HOUR_COUNT; h += 3) {
    t(svg, `${String(h).padStart(2, '0')}:00`, l + h * dx + 12, 18, 10, cols.dim(), { 'text-anchor': 'middle' });
  }

  HEAT_DAYS.forEach((day, d) => {
    t(svg, day, 4, top + d * dy + 12, 11, cols.dim());

    for (let h = 0; h < HOUR_COUNT; h++) {
      const { value, fill } = heatCell(d, h, metric, bg, accent);
      const selected = !!state.heatFocus && state.heatFocus.day === d && state.heatFocus.hour === h;
      const label = heatDayLabel(day, h, metric, value);

      const rect = el('rect', {
        x: l + h * dx, y: top + d * dy, width: sz, height: sz, rx: 3, fill,
        stroke: selected ? cols.ink() : cols.rule(), 'stroke-width': selected ? 2.5 : 0.4,
        role: 'gridcell', tabindex: d === 0 && h === 0 ? '0' : '-1',
        'data-heat-day': d, 'data-heat-hour': h, 'aria-label': label,
      }, svg);

      const interact = (focus = false) => {
        state.heatFocus = { day: d, hour: h };
        readoutEl.textContent = heatReadout(day, h, metric, value);
        svg.querySelectorAll('[data-heat-day]').forEach((cell) => {
          const match = cell.dataset.heatDay == d && cell.dataset.heatHour == h;
          cell.setAttribute('stroke', match ? cols.ink() : cols.rule());
          cell.setAttribute('stroke-width', match ? 2.5 : 0.4);
        });
        if (focus) rect.focus();
      };

      const tip = () => `<strong>${day} ${String(h).padStart(2, '0')}:00</strong><small>演示 · ${fmt(value)} ${metric === 'volume' ? '次' : 'ms'}</small>`;
      rect.addEventListener('mouseenter', (e) => ctx.tooltip.showTip(tip(), e));
      rect.addEventListener('mousemove', (e) => ctx.tooltip.showTip(tip(), e));
      rect.addEventListener('mouseleave', () => ctx.tooltip.hideTip());
      rect.addEventListener('click', () => interact(true));
      rect.addEventListener('keydown', (e) => {
        const delta = { ArrowRight: [0, 1], ArrowLeft: [0, -1], ArrowUp: [-1, 0], ArrowDown: [1, 0] }[e.key];
        if (delta) {
          e.preventDefault();
          const nd = clamp(d + delta[0], 0, HEAT_DAYS.length - 1);
          const nh = clamp(h + delta[1], 0, HOUR_COUNT - 1);
          const next = svg.querySelector(`[data-heat-day="${nd}"][data-heat-hour="${nh}"]`);
          if (next) {
            svg.querySelectorAll('[tabindex="0"]').forEach((c) => c.setAttribute('tabindex', '-1'));
            next.setAttribute('tabindex', '0');
            next.focus();
            next.dispatchEvent(new MouseEvent('click', { bubbles: false }));
          }
        } else if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          interact(true);
        }
      });
    }
  });

  t(svg, '颜色越深代表模拟负载越高', l, 255, 11, cols.dim());
}
