/*! web/js/dom.js — tiny DOM/SVG helpers shared by every view module. */

export const NS = 'http://www.w3.org/2000/svg';

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

/** Create an SVG (or HTML) element with attributes and optional text content. */
export function el(tag, attrs = {}, parent, text) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (text !== undefined) node.textContent = String(text);
  if (parent) parent.appendChild(node);
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** SVG <text> helper — the prototype's `t()`. */
export function t(parent, s, x, y, size = 12, fill, extra = {}) {
  return el('text', {
    x, y, 'font-size': size, fill, 'dominant-baseline': 'middle', ...extra,
  }, parent, s);
}

/** Read a CSS custom property off :root (theme-aware, so read it live). */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export const prefersReducedMotion = () =>
  matchMedia('(prefers-reduced-motion: reduce)').matches;
