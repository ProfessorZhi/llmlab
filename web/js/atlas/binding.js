/*! web/js/atlas/binding.js
 *  Turns an SVG/canvas-drawn mark into an operable control: pointer preview,
 *  click to lock, Enter/Space to activate, focus parity with hover. Extracted so
 *  the sunburst, treemap, Pareto and scatter marks all behave identically.
 */
import { escapeHtml } from '../dom.js';
import { fmt, withUnit } from '../format.js';

/** Attach workload behaviour to a chart mark. */
export function bindWorkload(node, d, ctx) {
  const { actions, tooltip } = ctx;
  node.classList.add('atlas-hit');
  node.dataset.atlasItem = d.id;
  node.setAttribute('role', 'button');
  node.setAttribute('tabindex', '0');
  node.setAttribute('aria-label', `${d.name}，${fmt(d.calls)} 次调用；p95 ${withUnit(d.p95, '毫秒')}，演示数据`);

  const label = () => `<strong>${escapeHtml(d.name)}</strong><small>${fmt(d.calls)} 次 · p95 ${withUnit(d.p95, 'ms')}</small>`;

  node.addEventListener('pointerenter', (e) => { actions.preview(d.id); tooltip.showTip(label(), e); });
  node.addEventListener('pointermove', (e) => tooltip.showTip(label(), e));
  node.addEventListener('pointerleave', () => actions.unpreview());
  node.addEventListener('focus', () => actions.preview(d.id));
  node.addEventListener('blur', () => actions.unpreview());
  node.addEventListener('click', (e) => { e.stopPropagation(); actions.select(d.id); });
  node.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); actions.select(d.id); }
  });
}

/** Attach direction-filter behaviour to a group sector / donut slice / legend. */
export function bindGroup(node, g, ctx) {
  const { actions } = ctx;
  node.classList.add('atlas-hit');
  node.dataset.atlasGroup = g.id;
  node.setAttribute('role', 'button');
  node.setAttribute('tabindex', '0');
  node.setAttribute('aria-label', `筛选 ${g.name}`);
  const toggle = () => actions.setGroup(ctx.state.group === g.id ? 'all' : g.id);
  node.addEventListener('click', toggle);
  node.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
}
