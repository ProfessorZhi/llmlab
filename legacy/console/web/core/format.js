/* ==========================================================================
   format -- every number, byte, duration and timestamp the UI renders.

   One rule runs through all of it: **a missing value renders as an em dash,
   never as `0`, `NaN`, `undefined` or an empty string.**  A zero is a
   measurement; an em dash is the absence of one.  A dashboard that prints 0
   for "we could not read it" is worse than one that prints nothing, because
   the 0 looks like a fact.
   ========================================================================== */

export const DASH = '—';

const missing = (v) => v === null || v === undefined || v === '' || (typeof v === 'number' && !Number.isFinite(v));

export function num(v, digits = 1) {
  if (missing(v)) return DASH;
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(digits) : DASH;
}

export function int(v) {
  if (missing(v)) return DASH;
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 0 }) : DASH;
}

/** Fixed-decimal with separators -- for values that sit in a column. */
export function fixed(v, digits = 2) {
  if (missing(v)) return DASH;
  const n = Number(v);
  return Number.isFinite(n)
    ? n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : DASH;
}

const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

/** Binary units, because the node reports binary (GiB) and Windows shows GiB. */
export function bytes(n, digits) {
  if (missing(n)) return DASH;
  let v = Number(n);
  if (!Number.isFinite(v)) return DASH;
  let i = 0;
  while (Math.abs(v) >= 1024 && i < UNITS.length - 1) { v /= 1024; i++; }
  const d = digits !== undefined ? digits : (i === 0 ? 0 : Math.abs(v) < 10 ? 1 : 0);
  return `${v.toFixed(d)} ${UNITS[i]}`;
}

/** MiB in, GiB out -- the node speaks MiB, humans read GiB. */
export const gib = (mib, digits = 1) => missing(mib) ? DASH : num(Number(mib) / 1024, digits);

export function dur(sec) {
  if (missing(sec)) return DASH;
  const s = Math.max(0, Math.floor(Number(sec)));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

/** Compact duration for a latency: 1.234 s or 412 ms. */
export function lat(sec, digits = 2) {
  if (missing(sec)) return DASH;
  const v = Number(sec);
  return v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(digits)} s`;
}

export function pct(v, digits = 1, of = 100) {
  if (missing(v)) return DASH;
  return `${num((Number(v) / of) * 100, digits)}%`;
}

export function ts(seconds, opts = {}) {
  if (missing(seconds)) return DASH;
  const d = new Date(Number(seconds) * 1000);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString('zh-CN', { hour12: false, ...opts });
}

/** "3 分钟前" -- but only for things that really are recent. */
export function ago(seconds) {
  if (missing(seconds)) return DASH;
  const delta = Math.floor(Date.now() / 1000 - Number(seconds));
  if (delta < 0) return '刚刚';
  if (delta < 60) return `${delta} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

/** Truncate with an ellipsis, for text that has to fit a chip or a cell. */
export function clip(s, n = 60) {
  const str = String(s ?? '');
  return str.length > n ? `${str.slice(0, n - 1)}…` : str;
}

/** A short, stable hash rendered as a CSS-friendly hue -- used to tint
 *  repeated-but-distinct labels (run ids, model names) without a lookup. */
export function hueOf(s) {
  let h = 0;
  for (const ch of String(s ?? '')) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}
