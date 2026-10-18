/*! web/js/atlas/render-ranking.js
 *  Sortable workload table and the ≤4-item comparison basket. */
import { escapeHtml } from '../dom.js';
import { fmt, withUnit, isKnown, UNKNOWN } from '../format.js';
import { filteredRows, groupName, MAX_COMPARE } from './selectors.js';

const valueFor = (d, sort) => (sort === 'calls' ? fmt(d.calls)
  : sort === 'p95' ? (isKnown(d.p95) ? `${fmt(d.p95)}ms` : UNKNOWN)
    : (isKnown(d.tokens) ? `${fmt(d.tokens)}k` : UNKNOWN));

export function renderRanking(host, ctx) {
  const { state, groups, cols } = ctx;
  const order = filteredRows(ctx.rows, groups, state);
  host.replaceChildren();

  if (!order.length) {
    host.innerHTML = '<div style="padding:25px;color:var(--muted);font-size:13px">当前条件没有匹配的工作负载。</div>';
    return;
  }

  const max = Math.max(...order.map((d) => d[state.sort]));

  order.forEach((d, i) => {
    const row = document.createElement('div');
    row.className = 'atlas-rankrow';
    row.dataset.atlasItem = d.id;
    row.setAttribute('role', 'row');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-label', `${d.name}，${fmt(d.calls)} 次请求，p95 ${withUnit(d.p95, '毫秒')}，点击选中`);

    const pct = (d[state.sort] / max) * 100;
    const inCompare = state.compare.includes(d.id);
    row.innerHTML = `<div class="rank-index">${String(i + 1).padStart(2, '0')}</div>`
      + `<div><div class="rank-name">${escapeHtml(d.name)}</div><div class="rank-sub">${escapeHtml(groupName(groups, d.g))}</div></div>`
      + `<div class="bar"><span style="width:${pct.toFixed(2)}%;background:${cols.col(d.g)}"></span></div>`
      + `<div class="rank-val">${valueFor(d, state.sort)}</div>`
      + `<div class="rank-p95">${withUnit(d.p95, 'ms')}</div>`
      + `<button class="rank-add" type="button" aria-label="${inCompare ? '移出' : '加入'}${escapeHtml(d.name)}对比" title="加入/移出对比">${inCompare ? '−' : '+'}</button>`;

    row.addEventListener('mouseenter', () => ctx.actions.preview(d.id));
    row.addEventListener('mouseleave', () => ctx.actions.unpreview());
    row.addEventListener('focus', (e) => { if (e.target === row) ctx.actions.preview(d.id); });
    row.addEventListener('blur', (e) => { if (e.target === row) ctx.actions.unpreview(); });
    row.addEventListener('click', (e) => { if (!e.target.closest('.rank-add')) ctx.actions.select(d.id); });
    row.addEventListener('keydown', (e) => {
      if (e.target !== row) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ctx.actions.select(d.id); }
    });
    row.querySelector('.rank-add').addEventListener('click', (e) => {
      e.stopPropagation();
      ctx.actions.toggleCompare(d.id);
    });
    host.append(row);
  });
}

export function renderCompare(panel, grid, countEl, ctx) {
  const { state, cols, groups } = ctx;
  panel.hidden = !state.compare.length;
  countEl.textContent = `${state.compare.length} / ${MAX_COMPARE}`;
  if (!state.compare.length) return;

  const rows = state.compare.map((id) => ctx.findRow(id)).filter(Boolean);
  if (!rows.length) return;
  const max = Math.max(...rows.map((d) => d.calls));

  grid.replaceChildren();
  rows.forEach((d) => {
    const card = document.createElement('article');
    card.className = 'atlas-compare-item';
    card.innerHTML = `<h4>${escapeHtml(d.name)}</h4>`
      + `<div class="big">${fmt(d.calls)} <small>次</small></div>`
      + `<div style="height:4px;background:var(--faint);margin-bottom:13px"><div style="height:100%;width:${((d.calls / max) * 100).toFixed(2)}%;background:${cols.col(d.g)}"></div></div>`
      + '<dl>'
      + `<dt>p95</dt><dd>${withUnit(d.p95, 'ms')}</dd>`
      + `<dt>Tokens</dt><dd>${isKnown(d.tokens) ? `${fmt(d.tokens)}k` : UNKNOWN}</dd>`
      + `<dt>方向</dt><dd>${escapeHtml(groupName(groups, d.g))}</dd>`
      + '</dl>';
    grid.append(card);
  });
}
