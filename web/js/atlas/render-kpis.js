/*! web/js/atlas/render-kpis.js
 *  Summary metrics, the scope/basis rail under the chart, and the aria-pressed
 *  state of every toggle. The p95 KPI is hard-wired to "—": see selectors.js. */
import { $$ } from '../dom.js';
import { fmt, UNKNOWN } from '../format.js';
import { sum, groupName } from './selectors.js';
import { RANGE_NAMES } from '../data/demo-fixture.js';

export function renderKpis(dom, ctx) {
  const { state, groups } = ctx;
  const rows = ctx.visible();
  const total = sum(rows, 'calls');
  const tokens = sum(rows, 'tokens');

  dom.calls.textContent = fmt(total);
  dom.active.textContent = String(rows.length).padStart(2, '0');
  dom.tokens.textContent = `${fmt(tokens)}k`;
  dom.callsNote.textContent = `${RANGE_NAMES[state.range]} · 合成窗口`;
  dom.p95.textContent = UNKNOWN;

  $$('[data-atlas-range]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasRange === state.range)));
  $$('[data-atlas-group]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasGroup === state.group)));
  $$('[data-atlas-view]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasView === state.view)));
  $$('[data-atlas-heat]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasHeat === state.heat)));

  renderScope(dom, ctx, rows);
}

/** The "scope / denominator" rail and the proportional mini strip. */
function renderScope(dom, ctx, rows) {
  const { state, groups } = ctx;
  dom.scope.textContent = `${state.group === 'all' ? '全部' : groupName(groups, state.group)} / ${rows.length} 类工作负载`;
  dom.basis.textContent = `分母 ${fmt(sum(rows, 'calls'))} 次 · 完全合成`;

  dom.miniStrip.replaceChildren();
  rows.forEach((d) => {
    const s = document.createElement('span');
    s.style.flex = d.calls;
    s.style.background = ctx.cols.col(d.g);
    s.title = `${d.name} ${fmt(d.calls)} 次（演示）`;
    dom.miniStrip.append(s);
  });
}
