/*! web/js/format.js — number formatting used by tables, tooltips and exports.
 *  Kept free of DOM access so it can be unit-tested under `node --test`. */

/** Group-separated integer, zh-CN locale (prototype's `fmt`). */
export const fmt = (n) => Number(n).toLocaleString('zh-CN');

/** Fixed-decimal string (prototype's `num`). */
export const num = (n, nf = 1) => Number(n).toFixed(nf);

/**
 * Render a value that may legitimately be missing.
 * The prototype treats "no data" as an explicit dash — never as zero — so that
 * a broken upstream can't be mistaken for a real measurement.
 */
export const UNKNOWN = '—';

export function metric(value, render = fmt) {
  return value === null || value === undefined || Number.isNaN(value)
    ? UNKNOWN
    : render(value);
}

/** True when a value is a usable finite number (0 is usable, null is not). */
export const isKnown = (v) => typeof v === 'number' && Number.isFinite(v);

/** `"1,420 ms"` for a known value, `"—"` for a missing one — never `"0 ms"`. */
export function withUnit(value, unit, render = fmt) {
  return isKnown(value) ? `${render(value)} ${unit}` : UNKNOWN;
}
