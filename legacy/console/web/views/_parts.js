/* ==========================================================================
   _parts -- the shared markup vocabulary for every view.

   Not a framework: a handful of functions that emit the class names
   components.css already defines.  The point is that eight views written
   independently still agree on what a card, a source line and a status flag
   look like -- the alternative is eight slightly different ones.

   Two conventions run through all of it:

     * **every number either has a source or it does not appear.**  `src()`
       is not decoration; it is the line that says which probe produced the
       figure above it.
     * **missing renders as —** (see core/format.js).  A card that prints 0
       because a read failed is claiming a measurement it does not have.
   ========================================================================== */

import { esc, raw } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { DASH } from '../core/format.js';

/* --------------------------------------------------------------- surfaces */

/** A content card.  `body` is already-escaped markup; pass `raw` through. */
export function card({ title = '', note = '', actions = '', body = '', cls = '', id = '' } = {}) {
  const head = (title || actions)
    ? `<div class="card-head"><h3>${esc(title)}</h3>`
      + (note ? `<span class="card-note" title="${esc(note)}">${esc(note)}</span>` : '')
      + (actions ? `<div class="card-actions">${actions}</div>` : '')
      + '</div>'
    : '';
  return `<section class="card ${cls}"${id ? ` id="${esc(id)}"` : ''}>${head}`
    + `<div class="card-body">${body}</div></section>`;
}

/** A titled section of the page.  Usually holds a grid of cards. */
export function band({ title = '', sub = '', tools = '', body = '', id = '' } = {}) {
  const head = (title || sub || tools)
    ? `<div class="band-head"><div class="band-text">`
      + (title ? `<h2>${esc(title)}</h2>` : '')
      + (sub ? `<p>${esc(sub)}</p>` : '')
      + `</div>${tools ? `<div class="band-tools">${tools}</div>` : ''}</div>`
    : '';
  return `<section class="band"${id ? ` id="${esc(id)}"` : ''}>${head}${body}</section>`;
}

export const grid = (cols, body) => `<div class="grid grid-${cols}">${body}</div>`;

/* ------------------------------------------------------------------ stats */

/**
 * One figure with its label, unit, a note and -- always -- where it came from.
 * `tone` tints the value: ok | warn | crit | muted.
 */
export function stat({ label, value, unit = '', sub = '', source = '', tone = '', ic = '' }) {
  return `<div class="stat">`
    + `<div class="stat-label">${ic ? icon(ic, '', 14) : ''}${esc(label)}</div>`
    + `<div class="stat-value ${tone}">${esc(value)}`
    + (unit ? `<span class="u">${esc(unit)}</span>` : '') + '</div>'
    + (sub ? `<div class="stat-sub${tone === 'warn' || tone === 'crit' ? ' warn' : ''}">${esc(sub)}</div>` : '')
    + src(source)
    + '</div>';
}

/** The provenance line.  Short, quiet, and never optional on a measured value. */
export const src = (text) => text ? `<span class="src">来源 · ${esc(text)}</span>` : '';

/** The two-source cross-check line: a value confirmed by a second probe. */
export const xcheck = (text) =>
  text ? `<div class="src" style="color:var(--ink-muted)">双源核对 · ${esc(text)}</div>` : '';

/**
 * A status flag.  ALWAYS a dot plus a word -- colour alone does not carry
 * state, and roughly one reader in twelve cannot separate the good hue from
 * the critical one.
 */
export function flag(state, label, { title = '' } = {}) {
  const tone = { ok: 'good', good: 'good', warn: 'warning', warning: 'warning',
                 crit: 'critical', critical: 'critical', degraded: 'serious',
                 offline: 'muted', unknown: 'muted', planned: 'info',
                 starting: 'warning' }[state] || 'muted';
  return `<span class="flag ${tone}"${title ? ` title="${esc(title)}"` : ''}>`
    + `<span class="dot"></span>${esc(label)}</span>`;
}

export const badge = (text, cls = '') => `<span class="badge ${cls}">${esc(text)}</span>`;

/* ------------------------------------------------------------- structures */

/** A definition list for facts that need no chart. */
export function kv(pairs) {
  const rows = pairs.filter(Boolean).map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd>${v === null || v === undefined || v === '' ? DASH : esc(v)}</dd>`).join('');
  return `<dl class="kv">${rows}</dl>`;
}

/** A labelled progress bar.  Used where a percentage is the whole story. */
export function meter({ name, value, max = 100, display = '', tone = '', thin = false }) {
  const has = typeof value === 'number' && Number.isFinite(value);
  const frac = has && max ? Math.max(0, Math.min(1, value / max)) : 0;
  const auto = frac >= 0.90 ? 'crit' : frac >= 0.75 ? 'warn' : '';
  return `<div class="meter-row"><div class="meter-top">`
    + `<span class="meter-name">${esc(name)}</span>`
    + `<span class="meter-val">${esc(has ? (display || `${(frac * 100).toFixed(0)}%`) : DASH)}</span>`
    + `</div><div class="meter ${tone || auto}${thin ? ' thin' : ''}" role="img"`
    + ` aria-label="${esc(`${name} ${has ? (display || `${(frac * 100).toFixed(0)}%`) : '无数据'}`)}">`
    + `<i style="width:${(frac * 100).toFixed(1)}%"></i></div></div>`;
}

/** A row in a list: a name, supporting detail, and right-aligned actions. */
export function row({ lead = '', name, sub = '', meta = '', actions = '', hoverable = false }) {
  return `<div class="row${hoverable ? ' hoverable' : ''}">`
    + (lead ? `<div class="row-lead">${lead}</div>` : '')
    + `<div class="row-main"><div class="row-name">${name}</div>`
    + (sub ? `<div class="row-sub">${sub}</div>` : '')
    + (meta ? `<div class="row-meta">${meta}</div>` : '')
    + `</div>${actions ? `<div class="row-actions">${actions}</div>` : ''}</div>`;
}

/** A <table> from a header list and row array; cells may pass raw markup. */
export function table(headers, rows, { num = [] } = {}) {
  const isNum = (i) => num.includes(i);
  return `<div class="table-wrap"><table class="data"><thead><tr>`
    + headers.map((h, i) => `<th${isNum(i) ? ' class="num"' : ''}>${esc(h)}</th>`).join('')
    + '</tr></thead><tbody>'
    + rows.map((r) => `<tr>` + r.map((c, i) =>
        `<td${isNum(i) ? ' class="num"' : ''}>${c === null || c === undefined ? DASH : (typeof c === 'object' && c.__raw ? c.value : esc(c))}</td>`
      ).join('') + '</tr>').join('')
    + '</tbody></table></div>';
}

/* --------------------------------------------------------------- tools */

export const button = ({ label, ic = '', id = '', cls = '', title = '', attrs = '' }) =>
  `<button class="btn ${cls}" type="button"${id ? ` id="${esc(id)}"` : ''}`
  + `${title ? ` title="${esc(title)}"` : ''}${attrs ? ` ${attrs}` : ''}>`
  + (ic ? icon(ic, '', 15) : '') + (label ? esc(label) : '') + '</button>';

export const linkButton = ({ label, href, ic = 'external', cls = 'btn-ghost', title = '' }) =>
  `<a class="btn ${cls}" href="${esc(href)}" target="_blank" rel="noopener noreferrer"`
  + `${title ? ` title="${esc(title)}"` : ''}>${icon(ic, '', 15)}${esc(label)}</a>`;

/** A segmented control; wire it with core/ui.js `segmented()`. */
export function segmented(name, options, active = 0) {
  return `<div class="segmented" id="${esc(name)}" role="tablist">`
    + `<span class="seg-thumb" aria-hidden="true"></span>`
    + options.map((o, i) =>
        `<button type="button" role="tab" data-value="${esc(o.value ?? o)}"`
        + ` aria-selected="${i === active}">${esc(o.label ?? o)}</button>`).join('')
    + '</div>';
}

/** An inline help dot.  `text` becomes the tooltip. */
export const hint = (text) => `<span class="hint" title="${esc(text)}" role="img" aria-label="${esc(text)}">?</span>`;

/** A page-level note, e.g. "this figure is a 30 s cache". */
export const asRaw = (markup) => raw(markup);
