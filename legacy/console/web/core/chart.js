/* ==========================================================================
   chart -- hand-rolled SVG.  No charting library, no CDN.

   The method constraints are carried over verbatim from V1 because they are
   about honesty, not style:

     * one y-axis per chart, never a second scale on the right;
     * categorical hues assigned in fixed slot order, never cycled;
     * a legend whenever there are two or more series;
     * every chart has a table twin -- the accessible equivalent;
     * status colours never do duty as a series colour.

   Colours are read from the stylesheet at draw time (see theme.token), so a
   theme switch is a re-render rather than a second hard-coded palette.
   ========================================================================== */

import { esc, $ } from './dom.js';
import { num, int, DASH } from './format.js';
import { token, series as seriesPalette } from './theme.js';

/* ---------------------------------------------------------------- helpers */

export const PALETTE = {
  get s1() { return token('--series-1', '#4f95ee'); },
  get s2() { return token('--series-2', '#e0713c'); },
  get s3() { return token('--series-3', '#23a87b'); },
  get s4() { return token('--series-4', '#a98bf0'); },
  get good() { return token('--good', '#35c05a'); },
  get warning() { return token('--warning', '#e8b03c'); },
  get critical() { return token('--critical', '#f0616b'); },
  get grid() { return token('--grid', 'rgba(255,255,255,.065)'); },
  get axis() { return token('--axis', 'rgba(255,255,255,.13)'); },
  get ink() { return token('--ink', '#f2f4f8'); },
  get muted() { return token('--ink-muted', '#6f7889'); },
  get surface() { return token('--chart-surface', '#14161c'); },
  get series() { return seriesPalette(); },
};

/** Round an axis maximum up to a readable step (1, 2, 2.5, 5, 10 × 10^n). */
export function niceCeil(v) {
  if (!(v > 0)) return 1;
  const exp = Math.floor(Math.log10(v));
  const base = 10 ** exp;
  const m = v / base;
  const step = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
  return step * base;
}

function axisTicks(max, count = 4) {
  return Array.from({ length: count + 1 }, (_, i) => (max / count) * i);
}

const fmtDefault = (v) => num(v, v !== null && Math.abs(v) < 10 ? 1 : 0);

/** The value readout that follows the pointer. */
function showTip(host, x, y, markup) {
  let tip = host.querySelector('.chart-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'chart-tip';
    host.append(tip);
  }
  tip.innerHTML = markup;
  const hw = host.clientWidth;
  tip.style.left = `${Math.max(60, Math.min(x, hw - 60))}px`;
  tip.style.top = `${y}px`;
}
const hideTip = (host) => host.querySelector('.chart-tip')?.remove();

/** Every chart carries its table twin.  Not a fallback -- the equivalent. */
function tableTwin(categories, seriesList, unit, fmt) {
  const cols = ['分类', ...seriesList.map((s) => `${s.name}${unit ? ` (${unit})` : ''}`)];
  return `<details class="table-view"><summary>表格视图（与图表同源）</summary>`
    + `<div class="table-wrap"><table class="data"><thead><tr>`
    + cols.map((c, i) => `<th${i ? ' class="num"' : ''}>${esc(c)}</th>`).join('')
    + '</tr></thead><tbody>'
    + categories.map((cat, ci) => `<tr><td>${esc(cat)}</td>`
        + seriesList.map((s) => `<td class="num">${s.values[ci] == null ? DASH : (fmt ? fmt(s.values[ci]) : num(s.values[ci], 2))}</td>`).join('')
        + '</tr>').join('')
    + '</tbody></table></div></details>';
}

function legend(seriesList, extra = '') {
  if (!seriesList.length) return '';
  const items = seriesList.map((s) =>
    `<span class="legend-item"><i class="swatch" style="background:${s.color}"></i>${esc(s.name)}</span>`).join('');
  return `<div class="chart-legend">${items}${extra ? `<span class="legend-item">${esc(extra)}</span>` : ''}</div>`;
}

/* ============================================================== bar chart */

/**
 * Grouped bars.
 *   spec.categories : string[]
 *   spec.series     : [{name, color?, values: (number|null)[]}]
 *   spec.unit, spec.fmtVal, spec.fmtAxis, spec.aria, spec.note
 * A `null` leaves a gap.  It is never drawn as a zero.
 */
export function barChart(target, spec) {
  const host = typeof target === 'string' ? $(target) : target;
  if (!host) return;
  const cats = spec.categories || [];
  const list = (spec.series || []).filter((s) => (s.values || []).some((v) => v != null));
  if (!cats.length || !list.length) {
    host.innerHTML = `<p class="empty">${esc(spec.emptyText || '该视图没有可用的数据。')}</p>`;
    return;
  }
  const pal = PALETTE.series;
  const seriesList = list.map((s, i) => ({ ...s, color: s.color || pal[i % pal.length] }));

  const W = 680, H = spec.height || 236;
  const padL = 52, padR = 16, padT = 16, padB = 46;
  const pw = W - padL - padR, ph = H - padT - padB;

  const all = seriesList.flatMap((s) => s.values).filter((v) => v != null);
  const yMax = niceCeil(Math.max(...all, 0));
  const y = (v) => padT + ph - (v / yMax) * ph;

  const band = pw / cats.length;
  const groupW = Math.min(band * 0.7, 132);
  const gap = 2;                                       // 2px surface gap
  const barW = Math.max(6, (groupW - gap * (seriesList.length - 1)) / seriesList.length);
  const roundBar = (x, yy, w, hh, r) => {
    const rr = Math.min(r, w / 2, Math.max(hh, 0));
    return `M${x},${yy + hh} L${x},${yy + rr} Q${x},${yy} ${x + rr},${yy}`
         + ` L${x + w - rr},${yy} Q${x + w},${yy} ${x + w},${yy + rr} L${x + w},${yy + hh} Z`;
  };

  const labelsFit = seriesList.length === 1 && barW >= 17;
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(spec.aria || '柱状图')}">`;

  for (const v of axisTicks(yMax)) {
    const yy = y(v);
    svg += `<line class="${v === 0 ? 'axis-line' : 'grid-line'}" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/>`;
    svg += `<text class="axis-text tick" x="${padL - 10}" y="${yy + 4}" text-anchor="end">${esc(spec.fmtAxis ? spec.fmtAxis(v) : fmtDefault(v))}</text>`;
  }
  if (spec.unit) {
    svg += `<text class="axis-text" x="${padL - 10}" y="${padT - 3}" text-anchor="end">${esc(spec.unit)}</text>`;
  }

  cats.forEach((cat, ci) => {
    const cx = padL + band * ci + band / 2;
    const x0 = cx - (barW * seriesList.length + gap * (seriesList.length - 1)) / 2;
    seriesList.forEach((s, si) => {
      const v = s.values[ci];
      if (v == null) return;
      const x = x0 + si * (barW + gap);
      const yy = y(v);
      const hh = Math.max(padT + ph - yy, 1.5);
      svg += `<path class="mark" data-ci="${ci}" d="${roundBar(x, yy, barW, hh, 4)}" fill="${s.color}"/>`;
      if (labelsFit) {
        svg += `<text class="bar-label" x="${x + barW / 2}" y="${yy - 6}" text-anchor="middle">`
             + `${esc(spec.fmtVal ? spec.fmtVal(v) : fmtDefault(v))}</text>`;
      }
    });
    svg += `<text class="axis-text" x="${cx}" y="${padT + ph + 19}" text-anchor="middle">${esc(cat)}</text>`;
  });
  svg += `<line class="axis-line" x1="${padL}" x2="${W - padR}" y1="${y(0)}" y2="${y(0)}"/>`;
  svg += '</svg>';

  host.className = 'chart';
  host.innerHTML = legend(seriesList, spec.note)
    + svg + tableTwin(cats, seriesList, spec.unit, spec.fmtVal);

  wireBars(host, cats, seriesList, spec, W, padL, band);
}

function wireBars(host, cats, seriesList, spec, W, padL, band) {
  const svg = host.querySelector('svg');
  const marks = Array.from(host.querySelectorAll('.mark'));
  const fmt = spec.fmtVal || ((v) => num(v, 2));
  const box = host.getBoundingClientRect();

  const onMove = (ev) => {
    const r = svg.getBoundingClientRect();
    const scale = W / r.width;
    const px = (ev.clientX - r.left) * scale;
    const ci = Math.min(cats.length - 1, Math.max(0, Math.floor((px - padL) / band)));
    host.classList.add('has-hover');
    marks.forEach((m) => m.classList.toggle('hot', Number(m.dataset.ci) === ci));
    const rows = seriesList.map((s) => s.values[ci] == null ? '' :
      `<div class="tip-row"><i class="swatch" style="background:${s.color}"></i>${esc(s.name)}<span class="tip-val">${esc(fmt(s.values[ci]))}</span></div>`).join('');
    const cx = r.left - box.left + (padL + band * ci + band / 2) / scale;
    const top = Math.min(...seriesList.map((s) => s.values[ci]).filter((v) => v != null).map((v) => {
      const yMax = niceCeil(Math.max(...seriesList.flatMap((x) => x.values).filter((z) => z != null), 0));
      const ph = (spec.height || 236) - 16 - 46;
      return r.top - box.top + (16 + ph - (v / yMax) * ph) / scale;
    }), 200);
    showTip(host, cx, Math.max(38, top), `<div class="tip-cat">${esc(cats[ci])}</div>${rows}`);
  };
  const onLeave = () => { host.classList.remove('has-hover'); marks.forEach((m) => m.classList.remove('hot')); hideTip(host); };

  svg.addEventListener('pointermove', onMove);
  svg.addEventListener('pointerleave', onLeave);
}

/* ============================================================= line chart */

/**
 * Multi-series line.
 *   spec.series : [{name, color?, values}]  -- aligned on spec.categories
 *   spec.fill   : one index (or true) to fill the area under that series
 *   spec.yMin   : force a floor; default is 0 for rate-like metrics
 */
export function lineChart(target, spec) {
  const host = typeof target === 'string' ? $(target) : target;
  if (!host) return;
  const cats = spec.categories || [];
  const list = (spec.series || []).filter((s) => (s.values || []).some((v) => v != null));
  if (!cats.length || !list.length) {
    host.innerHTML = `<p class="empty">${esc(spec.emptyText || '该视图没有可用的数据。')}</p>`;
    return;
  }
  const pal = PALETTE.series;
  const seriesList = list.map((s, i) => ({ ...s, color: s.color || pal[i % pal.length] }));

  const W = 680, H = spec.height || 240;
  const padL = 52, padR = 16, padT = 16, padB = 42;
  const pw = W - padL - padR, ph = H - padT - padB;
  const all = seriesList.flatMap((s) => s.values).filter((v) => v != null);
  const yMax = niceCeil(Math.max(...all, 0));
  const yMin = spec.yMin ?? 0;
  const x = (i) => padL + (cats.length === 1 ? pw / 2 : (i / (cats.length - 1)) * pw);
  const y = (v) => padT + ph - ((v - yMin) / (yMax - yMin || 1)) * ph;

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(spec.aria || '折线图')}">`;
  for (const v of axisTicks(yMax)) {
    const yy = y(v);
    svg += `<line class="${v === 0 ? 'axis-line' : 'grid-line'}" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/>`;
    svg += `<text class="axis-text tick" x="${padL - 10}" y="${yy + 4}" text-anchor="end">${esc(spec.fmtAxis ? spec.fmtAxis(v) : fmtDefault(v))}</text>`;
  }
  if (spec.unit) svg += `<text class="axis-text" x="${padL - 10}" y="${padT - 3}" text-anchor="end">${esc(spec.unit)}</text>`;

  seriesList.forEach((s) => {
    const pts = s.values.map((v, i) => v == null ? null : [x(i), y(v)]).filter(Boolean);
    if (pts.length < 2) return;
    const d = pts.map(([px, py], i) => `${i ? 'L' : 'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ');
    if (spec.fill) {
      svg += `<path class="line-area" d="${d} L${pts.at(-1)[0].toFixed(1)},${y(yMin)} L${pts[0][0].toFixed(1)},${y(yMin)} Z"`
           + ` fill="${s.color}" opacity="0.13"/>`;
    }
    svg += `<path class="line-path" d="${d}" stroke="${s.color}"/>`;
    pts.forEach(([px, py], i) => {
      svg += `<circle class="point" data-i="${i}" cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="3"`
           + ` fill="${s.color}" stroke="${PALETTE.surface}" stroke-width="1.6"/>`;
    });
  });
  svg += `<line class="crosshair" x1="0" x2="0" y1="${padT}" y2="${padT + ph}" style="display:none"/>`;
  svg += '</svg>';

  host.className = 'chart';
  host.innerHTML = legend(seriesList, spec.note) + svg + tableTwin(cats, seriesList, spec.unit, spec.fmtVal);

  const svgEl = host.querySelector('svg');
  const ch = svgEl.querySelector('.crosshair');
  const fmt = spec.fmtVal || ((v) => num(v, 2));
  svgEl.addEventListener('pointermove', (ev) => {
    const r = svgEl.getBoundingClientRect();
    const scale = W / r.width;
    const px = (ev.clientX - r.left) * scale;
    const i = cats.length === 1 ? 0 : Math.round(((px - padL) / pw) * (cats.length - 1));
    const ci = Math.min(cats.length - 1, Math.max(0, i));
    ch.style.display = '';
    ch.setAttribute('x1', x(ci)); ch.setAttribute('x2', x(ci));
    host.classList.add('has-hover');
    host.querySelectorAll('.point').forEach((p) => p.classList.toggle('hot', Number(p.dataset.i) === ci));
    const rows = seriesList.map((s) => s.values[ci] == null ? '' :
      `<div class="tip-row"><i class="swatch" style="background:${s.color}"></i>${esc(s.name)}<span class="tip-val">${esc(fmt(s.values[ci]))}</span></div>`).join('');
    const cx = r.left - host.getBoundingClientRect().left + x(ci) / scale;
    showTip(host, cx, 70, `<div class="tip-cat">${esc(cats[ci])}</div>${rows}`);
  });
  svgEl.addEventListener('pointerleave', () => {
    ch.style.display = 'none';
    host.classList.remove('has-hover');
    host.querySelectorAll('.point').forEach((p) => p.classList.remove('hot'));
    hideTip(host);
  });
}

/* ============================================================= sparkline */

/** A tiny trend line.  No axes, no legend -- it is a shape, read at a glance. */
export function sparkline(target, values, { color, height = 56, unit = '', fmt = (v) => num(v, 1) } = {}) {
  const host = typeof target === 'string' ? $(target) : target;
  if (!host) return;
  const vals = (values || []).filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (vals.length < 2) {
    host.className = 'spark';
    host.innerHTML = `<p class="empty">已采集 ${vals.length} / 2 个样本，稍后自动补齐。</p>`;
    return;
  }
  const W = 680, H = height, pad = 7;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = (hi - lo) || 1;
  const px = (i) => pad + (i / (vals.length - 1)) * (W - pad * 2);
  const py = (v) => H - pad - ((v - lo) / span) * (H - pad * 2);
  const line = vals.map((v, i) => `${i ? 'L' : 'M'}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(' ');
  const area = `${line} L${px(vals.length - 1).toFixed(1)},${H - pad} L${px(0).toFixed(1)},${H - pad} Z`;
  const c = color || PALETTE.s1;
  const id = `sg-${Math.random().toString(36).slice(2, 8)}`;

  host.className = 'spark';
  host.style.height = `${height}px`;     // the viewBox is fixed; the box is not
  host.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="采样趋势，最新 ${fmt(vals.at(-1))} ${esc(unit)}">`
    + `<defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0%" stop-color="${c}" stop-opacity="0.26"/>`
    + `<stop offset="100%" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>`
    + `<path class="line-area" d="${area}" fill="url(#${id})"/>`
    + `<path class="line-path" d="${line}" stroke="${c}"/>`
    + `<circle cx="${px(vals.length - 1).toFixed(1)}" cy="${py(vals.at(-1)).toFixed(1)}" r="3.4" fill="${c}" stroke="${PALETTE.surface}" stroke-width="2"/>`
    + '</svg>';
  return { lo, hi, n: vals.length, last: vals.at(-1) };
}

/* ================================================================= gauge */

/** A 270-degree arc gauge.  Used for one number that needs a scale around it. */
export function gauge(target, { value, max = 100, label = '', unit = '', digits = 0, color } = {}) {
  const host = typeof target === 'string' ? $(target) : target;
  if (!host) return;
  const hasValue = typeof value === 'number' && Number.isFinite(value);
  const frac = hasValue && max ? Math.max(0, Math.min(1, value / max)) : 0;
  const R = 54, SW = 11, C = 2 * Math.PI * R;
  const SWEEP = 0.75;                                   // 270deg
  const c = color || (frac > 0.92 ? PALETTE.critical : frac > 0.75 ? PALETTE.warning : PALETTE.s1);
  const dash = `${(C * SWEEP * frac).toFixed(2)} ${C}`;
  const track = `${(C * SWEEP).toFixed(2)} ${C}`;

  host.className = 'gauge-wrap';
  host.innerHTML =
    `<div class="gauge" style="width:148px;height:148px">`
    + `<svg viewBox="0 0 140 140" role="img" aria-label="${esc(label)} ${hasValue ? fmtNum(value, digits) : '无数据'}">`
    + `<g transform="rotate(135 70 70)">`
    + `<circle class="gauge-track" cx="70" cy="70" r="${R}" stroke-width="${SW}" stroke-dasharray="${track}"/>`
    + `<circle class="gauge-fill" cx="70" cy="70" r="${R}" stroke-width="${SW}" stroke="${c}" stroke-dasharray="${dash}"/>`
    + '</g></svg>'
    + `<div class="gauge-mid"><div class="gauge-val">${hasValue ? esc(fmtNum(value, digits)) : DASH}</div>`
    + `<div class="gauge-unit">${esc(unit)}</div></div></div>`
    + `<div class="gauge-side">${label}</div>`;
}

const fmtNum = (v, d) => Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

/* ================================================================ donut */

/** A share-of-total ring.  `parts` is [{label, value, color?}]. */
export function donut(target, { parts = [], unit = '', center = '' } = {}) {
  const host = typeof target === 'string' ? $(target) : target;
  if (!host) return;
  const total = parts.reduce((a, p) => a + (p.value || 0), 0);
  if (!total) { host.innerHTML = '<p class="empty">没有可分配的数量。</p>'; return; }
  const pal = PALETTE.series;
  const R = 54, SW = 14, C = 2 * Math.PI * R;
  let offset = 0;
  const arcs = parts.filter((p) => p.value > 0).map((p, i) => {
    const color = p.color || pal[i % pal.length];
    const len = (p.value / total) * C;
    const seg = `<circle cx="70" cy="70" r="${R}" fill="none" stroke="${color}" stroke-width="${SW}"`
      + ` stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}"`
      + ` transform="rotate(-90 70 70)"/>`;
    offset += len;
    return seg;
  }).join('');

  host.className = 'gauge-wrap';
  host.innerHTML =
    `<div class="gauge" style="width:148px;height:148px">`
    + `<svg viewBox="0 0 140 140" role="img" aria-label="占比环形图">`
    + `<circle cx="70" cy="70" r="${R}" fill="none" stroke="${PALETTE.grid}" stroke-width="${SW}"/>${arcs}</svg>`
    + `<div class="gauge-mid"><div class="gauge-val">${esc(center || int(total))}</div>`
    + `<div class="gauge-unit">${esc(unit)}</div></div></div>`
    + `<div class="gauge-side">${legend(parts.filter((p) => p.value > 0).map((p, i) => ({
        name: `${p.label} · ${int(p.value)}`, color: p.color || pal[i % pal.length] })))}</div>`;
}

/* ============================================================ horizontal */

/** A ranked horizontal bar list -- better than a pie for "top N by value". */
export function barList(target, { items = [], unit = '', digits = 1, color } = {}) {
  const host = typeof target === 'string' ? $(target) : target;
  if (!host) return;
  if (!items.length) { host.innerHTML = '<p class="empty">没有可比较的条目。</p>'; return; }
  const max = Math.max(...items.map((i) => i.value || 0)) || 1;
  const c = color || PALETTE.s1;
  host.className = '';
  host.innerHTML = `<div class="rows">${items.map((it) => `
    <div class="row">
      <div class="row-main">
        <div class="row-name"><span class="truncate">${esc(it.label)}</span></div>
        <div class="meter" style="margin-top:7px"><i style="width:${Math.max(1, (it.value / max) * 100).toFixed(1)}%;background:${it.color || c}"></i></div>
      </div>
      <div class="row-actions"><span class="tnum" style="font-size:var(--fs-md);color:var(--ink-2)">${esc(num(it.value, digits))}${unit ? ` <span class="muted" style="font-size:var(--fs-xs)">${esc(unit)}</span>` : ''}</span></div>
    </div>`).join('')}</div>`;
}
