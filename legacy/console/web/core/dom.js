/* ==========================================================================
   dom -- the eight DOM helpers the rest of the app is built on.

   Everything is built as an HTML string and assigned with innerHTML, which
   means *every* interpolation must go through `esc`.  That is the whole
   security model of the front end: the server never sends HTML, and the only
   way markup gets into the page is through these functions, where escaping is
   applied at the point of interpolation rather than at the point of use.
   ========================================================================== */

export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Escape for interpolation into an HTML text or attribute position. */
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * Tagged template that escapes every substitution.
 *
 *   html`<span title="${name}">${label}</span>`
 *
 * A value already known to be markup opts in explicitly with `raw()` -- so
 * "this is trusted" is always visible at the call site, and never the default.
 */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    out += (v && v.__raw === true ? v.value : esc(v)) + strings[i + 1];
  }
  return out;
}

export const raw = (value) => ({ __raw: true, value: String(value ?? '') });

/** Set innerHTML on a host, resolving either a selector or the element. */
export function setHTML(target, markup) {
  const node = typeof target === 'string' ? $(target) : target;
  if (node) node.innerHTML = markup;
  return node;
}

export function on(target, type, fn, opts) {
  const node = typeof target === 'string' ? $(target) : target;
  if (node) node.addEventListener(type, fn, opts);
  return node;
}

/** Delegated listener: one per container instead of one per rendered row. */
export function delegate(root, type, selector, fn) {
  const node = typeof root === 'string' ? $(root) : root;
  if (!node) return;
  node.addEventListener(type, (ev) => {
    const hit = ev.target instanceof Element ? ev.target.closest(selector) : null;
    if (hit && node.contains(hit)) fn(ev, hit);
  });
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
