/*! web/js/atlas/controller.js
 *  Orchestration for the Atlas view: owns the interaction state, wires the
 *  controls, and drives the render modules. Everything visual lives in a
 *  render-* module; everything numeric lives in selectors.js.
 */
import { $, $$, cssVar } from '../dom.js';
import { fmt } from '../format.js';
import {
  ATLAS_DEFAULTS, visibleRows, allRows, sum, hit, toggleCompare, MAX_COMPARE,
} from './selectors.js';
import { createPalette } from './theme-colors.js';
import { createTooltip } from './tooltip.js';
import { structureVisibility } from './structure.js';
import { createDotRenderer } from './render-dots.js';
import { renderSunburst } from './render-sunburst.js';
import { renderTreemap } from './render-treemap.js';
import { renderDonut } from './render-donut.js';
import { renderDetail, renderBreadcrumb } from './render-detail.js';
import { renderKpis } from './render-kpis.js';
import { renderRanking, renderCompare } from './render-ranking.js';
import { renderPareto, renderScatter } from './render-analysis.js';
import { renderHeatmap } from './render-heatmap.js';
import { renderChartEmpty, renderPanelEmpty } from './render-empty.js';
import { buildCsv, buildJson, saveFile, CSV_FILENAME, JSON_FILENAME } from './exporters.js';
import { DATA_STATES } from '../data/contract.js';
import { THEME_EVENT } from '../events.js';

const UNAVAILABLE_COPY = {
  [DATA_STATES.UNCONFIGURED]: '尚未配置后端数据源 · UNKNOWN',
  [DATA_STATES.UNKNOWN]: '数据来源不可用 · UNKNOWN',
  [DATA_STATES.EMPTY]: '当前来源没有数据 · EMPTY',
};

export function createAtlas({ source, doc = document }) {
  const view = $('#view-atlas', doc);
  const state = { ...ATLAS_DEFAULTS, compare: [] };

  const dom = {
    view,
    svg: $('#atlasMainChart', doc),
    canvas: $('#atlasDotCanvas', doc),
    tip: $('#atlasTip', doc),
    breadcrumb: $('#atlasBreadcrumb', doc),
    donut: $('#atlasDonut', doc),
    legend: $('#atlasLegend', doc),
    detail: $('#atlasDetail', doc),
    rankRows: $('#atlasRankRows', doc),
    pareto: $('#atlasPareto', doc),
    paretoNote: $('#atlasParetoNote', doc),
    scatter: $('#atlasScatter', doc),
    heatmap: $('#atlasHeatmap', doc),
    heatReadout: $('#atlasHeatReadout', doc),
    compare: $('#atlasCompare', doc),
    compareGrid: $('#atlasCompareGrid', doc),
    compareCount: $('#atlasCompareCount', doc),
    search: $('#atlasSearch', doc),
    sort: $('#atlasSort', doc),
    calls: $('#atlasCalls', doc),
    active: $('#atlasActive', doc),
    tokens: $('#atlasTokens', doc),
    callsNote: $('#atlasCallsNote', doc),
    p95: $('#atlasP95', doc),
    scope: $('#atlasScope', doc),
    basis: $('#atlasBasis', doc),
    miniStrip: $('#atlasMiniStrip', doc),
    frame: $('#atlasChartFrame', doc),
  };

  const rows = source.workloads ?? [];
  const groups = source.groups ?? [];
  const palette = createPalette(groups);
  const tooltip = createTooltip(dom.tip);

  const ctx = {
    state,
    rows,
    groups,
    cols: palette,
    tooltip,
    visible: () => visibleRows(rows, state),
    all: () => allRows(rows, state),
    findRow: (id) => allRows(rows, state).find((d) => d.id === id) || null,
    hitId: () => hit(state),
    isActive: () => view.classList.contains('active'),
    reducedMotion: () => matchMedia('(prefers-reduced-motion: reduce)').matches,
    actions: {},
    renderAll: () => renderAll(),
    source,
  };

  const dots = createDotRenderer(dom.canvas, ctx);

  // ------------------------------------------------------------- actions

  function updateFocus() {
    const id = hit(state);
    $$('#view-atlas [data-atlas-item]', doc).forEach((e) => {
      const active = !!id && e.dataset.atlasItem === id;
      e.dataset.highlight = String(active);
      e.dataset.dimmed = String(!!id && !active);
      if (e.closest('#atlasRanking')) e.dataset.selected = String(active);
    });
    $$('[data-atlas-group]', doc).forEach((e) => {
      e.setAttribute('aria-pressed', String(state.group !== 'all' && e.dataset.atlasGroup === state.group));
    });
  }

  function preview(id) {
    if (state.preview === id) return;
    state.preview = id;
    if (!available()) return;
    renderDetail(dom.detail, ctx);
    updateFocus();
    if (state.view === 'dots') dots.draw();
  }

  function unpreview() {
    if (state.preview === null) return;
    state.preview = null;
    tooltip.hideTip();
    if (!available()) return;
    renderDetail(dom.detail, ctx);
    updateFocus();
    if (state.view === 'dots') dots.draw();
  }

  function select(id) {
    state.preview = null;
    state.selected = state.selected === id ? null : id;
    tooltip.hideTip();
    if (!available()) return;
    renderDetail(dom.detail, ctx);
    updateFocus();
    renderBreadcrumb(dom.breadcrumb, ctx);
    if (state.view === 'dots') dots.draw();
  }

  function setGroup(id) {
    state.group = id;
    state.selected = null;
    state.preview = null;
    tooltip.hideTip();
    renderAll();
  }

  function reset() {
    state.group = 'all';
    state.selected = null;
    state.preview = null;
    state.search = '';
    dom.search.value = '';
    tooltip.hideTip();
    renderAll();
  }

  function compare(id) {
    const next = toggleCompare(state.compare, id, MAX_COMPARE);
    if (next.rejected) {
      dom.compareCount.textContent = `最多 ${MAX_COMPARE} 项，请先移除一项`;
      return;
    }
    state.compare = next.compare;
    renderCompare(dom.compare, dom.compareGrid, dom.compareCount, ctx);
    renderRanking(dom.rankRows, ctx);
    updateFocus();
  }

  Object.assign(ctx.actions, { preview, unpreview, select, setGroup, reset, toggleCompare: compare });

  // ------------------------------------------------------------- render

  const available = () => rows.length > 0;
  const unavailableMessage = () => UNAVAILABLE_COPY[source.state] ?? UNAVAILABLE_COPY[DATA_STATES.UNKNOWN];

  function renderStructure() {
    const { svgHidden, canvasHidden } = structureVisibility(state.view);
    dom.svg.toggleAttribute('hidden', svgHidden);
    dom.canvas.hidden = canvasHidden;

    if (!available()) {
      renderChartEmpty(dom.svg, palette, unavailableMessage(), '未连接真实来源前不显示任何图形或数值');
      return;
    }
    if (state.view === 'sunburst') renderSunburst(dom.svg, ctx);
    else if (state.view === 'treemap') renderTreemap(dom.svg, ctx);
    else requestAnimationFrame(() => dots.draw());
  }

  /** Zero-out every surface and explain why — used when the source is not usable. */
  function renderUnavailable() {
    const message = unavailableMessage();
    dom.calls.textContent = '—';
    dom.active.textContent = '—';
    dom.tokens.textContent = '—';
    dom.p95.textContent = '—';
    dom.callsNote.textContent = source.reason ? `来源状态 · ${source.reason}` : '来源状态 · UNKNOWN';
    dom.scope.textContent = '无可用范围';
    dom.basis.textContent = '分母 — · 未取得可信样本';
    dom.miniStrip.replaceChildren();

    renderBreadcrumb(dom.breadcrumb, ctx);
    dom.legend.replaceChildren();
    dom.donut.replaceChildren();
    dom.detail.innerHTML = `<div class="context">当前范围 · 不可用</div><h3>等待数据来源</h3>`
      + `<div class="atlas-focus-note">${message}。本页不会用合成值填充真实缺口，也不会以 0 代替缺失指标。</div>`;
    renderPanelEmpty(dom.rankRows, message);
    renderChartEmpty(dom.svg, palette, message);
    renderChartEmpty(dom.pareto, palette, message);
    dom.paretoNote.textContent = '—';
    renderChartEmpty(dom.scatter, palette, message);
    renderChartEmpty(dom.heatmap, palette, message);
    dom.heatReadout.textContent = '—';
    dom.compare.hidden = true;
    dom.compareGrid.replaceChildren();
    setPressedStates();
  }

  function setPressedStates() {
    $$('[data-atlas-range]', doc).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasRange === state.range)));
    $$('[data-atlas-group]', doc).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasGroup === state.group)));
    $$('[data-atlas-view]', doc).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasView === state.view)));
    $$('[data-atlas-heat]', doc).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.atlasHeat === state.heat)));
  }

  function renderAll() {
    if (!available()) { renderUnavailable(); return; }
    renderKpis(dom, ctx);
    renderBreadcrumb(dom.breadcrumb, ctx);
    renderStructure();
    renderDonut(dom.donut, dom.legend, ctx);
    renderDetail(dom.detail, ctx);
    renderRanking(dom.rankRows, ctx);
    renderPareto(dom.pareto, dom.paretoNote, ctx);
    renderScatter(dom.scatter, ctx);
    renderHeatmap(dom.heatmap, dom.heatReadout, ctx);
    renderCompare(dom.compare, dom.compareGrid, dom.compareCount, ctx);
    updateFocus();
  }

  // -------------------------------------------------------------- wiring

  $$('[data-atlas-group]', doc).forEach((b) => b.addEventListener('click', () => setGroup(b.dataset.atlasGroup)));
  $$('[data-atlas-range]', doc).forEach((b) => b.addEventListener('click', () => {
    state.range = b.dataset.atlasRange; state.preview = null; renderAll();
  }));
  $$('[data-atlas-view]', doc).forEach((b) => b.addEventListener('click', () => {
    state.view = b.dataset.atlasView; state.preview = null;
    if (!available()) { renderUnavailable(); return; }
    renderKpis(dom, ctx); renderStructure(); renderDetail(dom.detail, ctx);
  }));
  $$('[data-atlas-heat]', doc).forEach((b) => b.addEventListener('click', () => {
    state.heat = b.dataset.atlasHeat;
    state.heatFocus = null;
    dom.heatReadout.textContent = '点击或使用方向键浏览一个格子。';
    if (!available()) return;
    renderKpis(dom, ctx); renderHeatmap(dom.heatmap, dom.heatReadout, ctx);
  }));

  dom.search.addEventListener('input', (e) => { state.search = e.target.value; if (available()) renderRanking(dom.rankRows, ctx); });
  dom.sort.addEventListener('change', (e) => { state.sort = e.target.value; if (available()) renderRanking(dom.rankRows, ctx); });
  $('#atlasCompareClear', doc).addEventListener('click', () => {
    state.compare = [];
    renderCompare(dom.compare, dom.compareGrid, dom.compareCount, ctx);
    renderRanking(dom.rankRows, ctx);
  });
  $('#atlasReset', doc)?.addEventListener('click', reset);

  $('#atlasExport', doc).addEventListener('click', () => {
    if (!available()) return;
    saveFile(CSV_FILENAME, buildCsv(ctx.visible(), groups, state), 'text/csv;charset=utf-8', doc);
  });
  $('#atlasExportJSON', doc).addEventListener('click', () => {
    if (!available()) return;
    saveFile(JSON_FILENAME, buildJson(ctx.visible(), state), 'application/json;charset=utf-8', doc);
  });

  dom.svg.addEventListener('click', (e) => {
    if (!available()) return;
    if (!e.target.closest('.atlas-hit')) {
      state.selected = null; state.preview = null;
      renderDetail(dom.detail, ctx); updateFocus(); renderBreadcrumb(dom.breadcrumb, ctx);
    }
  });

  dom.canvas.addEventListener('pointermove', (e) => {
    if (!available()) return;
    const d = dots.datum(e);
    if (d) {
      preview(d.id);
      tooltip.showTip(`<strong>${d.name}</strong><small>1 点 = 1 次 · 共 ${fmt(d.calls)} 点</small>`, e);
    } else unpreview();
  });
  dom.canvas.addEventListener('pointerleave', unpreview);
  dom.canvas.addEventListener('click', (e) => {
    if (!available()) return;
    const d = dots.datum(e);
    if (d) select(d.id);
    else { state.selected = null; unpreview(); renderDetail(dom.detail, ctx); updateFocus(); }
  });

  doc.addEventListener(THEME_EVENT, () => requestAnimationFrame(() => { if (ctx.isActive()) renderAll(); }));
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.view === 'dots') dots.draw(); }, 100);
  }, { passive: true });
  $$('[data-page="atlas"],[data-go="atlas"]', doc).forEach((b) => b.addEventListener('click', () => {
    requestAnimationFrame(() => { if (ctx.isActive()) renderAll(); });
  }));
  window.addEventListener('hashchange', () => {
    if (location.hash === '#atlas') requestAnimationFrame(() => { if (ctx.isActive()) renderAll(); });
  });

  doc.addEventListener('keydown', (e) => {
    if (!ctx.isActive() || e.altKey || e.ctrlKey || e.metaKey
      || e.target.closest('input,select,textarea,[contenteditable=true]')) return;
    const byKey = { 1: 'sunburst', 2: 'treemap', 3: 'dots' };
    if (byKey[e.key]) {
      state.view = byKey[e.key];
      state.preview = null;
      if (!available()) { renderUnavailable(); return; }
      renderKpis(dom, ctx); renderStructure(); renderDetail(dom.detail, ctx);
    } else if (e.key === 'Escape' && (state.selected || state.preview)) {
      state.selected = null; state.preview = null; tooltip.hideTip();
      renderDetail(dom.detail, ctx); updateFocus(); renderBreadcrumb(dom.breadcrumb, ctx);
    } else if (e.key === '/' && !e.shiftKey) {
      e.preventDefault();
      dom.search.focus();
    }
  });

  renderAll();

  return {
    ctx,
    state,
    render: renderAll,
    select,
    setGroup,
    reset,
    /** Test/dev hook mirroring the prototype's `window.__ATLAS__`. */
    snapshot: () => ({
      ...state,
      compare: [...state.compare],
      sourceState: source.state,
      synthetic: !!source.synthetic,
    }),
    totals: () => ({ calls: sum(ctx.visible(), 'calls'), tokens: sum(ctx.visible(), 'tokens'), rows: ctx.visible().length }),
  };
}

/** The Atlas is only meaningful once the module has read :root colours. */
export const atlasThemeProbe = () => cssVar('--atlas-a');
