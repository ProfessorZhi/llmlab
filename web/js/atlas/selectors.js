/*! web/js/atlas/selectors.js
 *  Pure state → derived-data functions for the Atlas. No DOM, no globals:
 *  every filter, scale and ranking rule the UI uses is computed here so the
 *  same rules can be asserted in tests instead of only observed in a browser.
 */
import { RANGE_SCALES } from '../data/demo-fixture.js';

export const ATLAS_DEFAULTS = Object.freeze({
  range: '24h',
  group: 'all',
  view: 'sunburst',
  selected: null,
  preview: null,
  sort: 'calls',
  search: '',
  heat: 'volume',
  compare: [],
  heatFocus: null,
});

export const MAX_COMPARE = 4;

/** Apply the window multiplier. p95 is per-workload and is deliberately NOT scaled. */
export function scaleWorkloads(rows, range) {
  const k = RANGE_SCALES[range] ?? 1;
  return rows.map((d) => ({
    ...d,
    calls: Math.round(d.calls * k),
    tokens: Math.round(d.tokens * k),
  }));
}

/** Rows passing the active business-direction filter, scaled to the window. */
export const visibleRows = (rows, state) =>
  scaleWorkloads(rows.filter((d) => state.group === 'all' || d.g === state.group), state.range);

/** Every row, scaled but unfiltered — the donut always shows all directions. */
export const allRows = (rows, state) => scaleWorkloads(rows, state.range);

export const sum = (rows, key) => rows.reduce((n, d) => n + (+d[key] || 0), 0);

export const groupName = (groups, id) =>
  groups.find((g) => g.id === id)?.name ?? '全部工作负载';

/** The row currently under the pointer (preview) or locked (selected). */
export const hit = (state) => state.preview || state.selected;

export function sortedRows(rows, sort) {
  return [...rows].sort((a, b) => (b[sort] ?? -Infinity) - (a[sort] ?? -Infinity)
    || a.name.localeCompare(b.name, 'zh-CN'));
}

export function filteredRows(rows, groups, state) {
  const q = state.search.trim().toLowerCase();
  return sortedRows(
    visibleRows(rows, state).filter((d) => `${d.name} ${groupName(groups, d.g)}`.toLowerCase().includes(q)),
    state.sort,
  );
}

/**
 * How many workloads carry >=80% of the calls — the Pareto note under the curve.
 * Returns the count, the total count, and the cumulative share at the cut.
 */
export function paretoCutoff(rows) {
  const sorted = [...rows].sort((a, b) => b.calls - a.calls);
  const n = sorted.length;
  const total = sum(sorted, 'calls');
  if (!n || total <= 0) return { cutoff: n, n, share: 0 };
  let cum = 0;
  for (let i = 0; i < n; i++) {
    cum += sorted[i].calls;
    if (cum / total >= 0.8) return { cutoff: i + 1, n, share: cum / total };
  }
  return { cutoff: n, n, share: cum / total };
}

/**
 * Comparison basket reducer. Returns the next basket plus whether the add was
 * refused because the basket was full (the UI surfaces that as a hint).
 */
export function toggleCompare(compare, id, max = MAX_COMPARE) {
  if (compare.includes(id)) return { compare: compare.filter((s) => s !== id), rejected: false };
  if (compare.length >= max) return { compare: [...compare], rejected: true };
  return { compare: [...compare, id], rejected: false };
}

/**
 * The Atlas never shows a total p95 — percentile sets from different workloads
 * cannot be averaged. Kept as a named function so the invariant is explicit.
 */
export const aggregateP95 = () => null;

/** Heatmap metric bounds, mirrored by the renderer. */
export const HEAT_MAX = Object.freeze({ volume: 130, latency: 2500 });
