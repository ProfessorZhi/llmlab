/* ==========================================================================
   ui -- toasts, modals, popovers, and the four state renderers.

   The state renderers are the point.  A view that only knows how to draw
   success is a view that shows a blank panel when the node is down, and a
   blank panel is indistinguishable from "there is nothing to show" -- which
   is a different, and usually false, claim.
   ========================================================================== */

import { $, esc } from './dom.js';
import { icon } from './icons.js';
import { ApiError, isAborted } from './api.js';

/* ------------------------------------------------------------------ toast */

const TOAST_ICON = { good: 'check', error: 'alert', warning: 'alert', info: 'info' };

function toaster() {
  let host = $('#toaster');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toaster';
    host.className = 'toaster';
    host.setAttribute('role', 'region');
    host.setAttribute('aria-label', '通知');
    document.body.append(host);
  }
  return host;
}

/**
 * One transient message.  `kind` is good | error | warning | info.
 * Returns a function that dismisses it early.
 */
export function toast(message, { kind = 'info', title = '', ms = 5200, action = null } = {}) {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  node.innerHTML =
    icon(TOAST_ICON[kind] || 'info', 'toast-ico')
    + `<div class="toast-body">`
    + (title ? `<div class="toast-title">${esc(title)}</div>` : '')
    + `<div class="toast-msg">${esc(message)}</div></div>`;
  if (action) {
    const btn = document.createElement('button');
    btn.className = 'btn btn-quiet btn-sm';
    btn.type = 'button';
    btn.textContent = action.label;
    btn.addEventListener('click', () => { action.run(); dismiss(); });
    node.append(btn);
  }
  toaster().append(node);

  let timer = setTimeout(dismiss, ms);
  node.addEventListener('mouseenter', () => clearTimeout(timer));
  node.addEventListener('mouseleave', () => { timer = setTimeout(dismiss, 1600); });

  function dismiss() {
    clearTimeout(timer);
    if (!node.isConnected) return;
    node.classList.add('leaving');
    node.addEventListener('animationend', () => node.remove(), { once: true });
    // A reduced-motion user gets 1ms animations; this is the belt-and-braces
    // removal in case animationend never fires on a detached node.
    setTimeout(() => node.remove(), 700);
  }
  return dismiss;
}

/** Report an error the way its kind deserves, and never the aborted one. */
export function reportError(err, context = '') {
  if (isAborted(err)) return;
  const e = err instanceof ApiError ? err : new ApiError(String(err && err.message || err));
  toast(e.message, { kind: e.kind === 'offline' || e.kind === 'timeout' ? 'warning' : 'error',
                     title: context || e.label });
}

/* ------------------------------------------------------------------ modal */

/**
 * A modal built from a markup string.  `render(close)` returns the body;
 * `actions` is a list of {label, kind, run(close)}.
 * Focus is moved in on open and restored on close, and Escape closes it.
 */
export function modal({ title, body = '', actions = [], width = '' }) {
  const scrim = document.createElement('div');
  scrim.className = 'scrim';
  const box = document.createElement('div');
  box.className = 'modal';
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.setAttribute('aria-label', typeof title === 'string' ? title : '对话框');
  if (width) box.style.width = width;

  const prevFocus = document.activeElement;
  box.innerHTML =
    `<div class="modal-head"><h3>${esc(title)}</h3>`
    + `<button class="btn btn-quiet btn-sm btn-icon" type="button" data-close aria-label="关闭">${icon('close', '', 16)}</button></div>`
    + `<div class="modal-body"></div>`
    + (actions.length ? '<div class="modal-foot"></div>' : '');

  box.querySelector('.modal-body').innerHTML = body;
  const foot = box.querySelector('.modal-foot');
  for (const a of actions) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `btn ${a.kind === 'primary' ? 'btn-primary' : a.kind === 'danger' ? 'btn-danger' : 'btn-ghost'}`;
    b.textContent = a.label;
    b.addEventListener('click', () => a.run(close));
    foot?.append(b);
  }

  function close() {
    scrim.remove();
    document.removeEventListener('keydown', onKey);
    if (prevFocus instanceof HTMLElement && prevFocus.isConnected) prevFocus.focus();
  }
  function onKey(ev) { if (ev.key === 'Escape') close(); }

  scrim.addEventListener('mousedown', (ev) => { if (ev.target === scrim) close(); });
  box.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', close));
  document.addEventListener('keydown', onKey);

  scrim.append(box);
  document.body.append(scrim);
  // Focus the first real control, falling back to the dialog itself so Tab
  // still starts inside the trap.
  const first = box.querySelector('button:not([data-close]), a[href], select, input, textarea');
  (first || box).focus?.();
  return { close, box };
}

/* --------------------------------------------------------------- popover */

let openPopover = null;

export function popover(anchor, markup, { align = 'left' } = {}) {
  closePopover();
  const node = document.createElement('div');
  node.className = 'popover';
  node.setAttribute('role', 'dialog');
  node.innerHTML = markup;
  document.body.append(node);

  const a = anchor.getBoundingClientRect();
  const w = node.offsetWidth;
  let left = align === 'right' ? a.right - w : a.left;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  const below = a.bottom + window.scrollY + 6;
  const wouldOverflow = a.bottom + node.offsetHeight + 12 > window.innerHeight;
  node.style.position = 'absolute';
  node.style.left = `${left + window.scrollX}px`;
  node.style.top = `${(wouldOverflow ? a.top + window.scrollY - node.offsetHeight - 6 : below)}px`;

  openPopover = node;
  const onDown = (ev) => {
    if (!node.contains(ev.target) && !anchor.contains(ev.target)) closePopover();
  };
  const onKey = (ev) => { if (ev.key === 'Escape') closePopover(); };
  setTimeout(() => document.addEventListener('mousedown', onDown), 0);
  document.addEventListener('keydown', onKey);
  node.__cleanup = () => {
    document.removeEventListener('mousedown', onDown);
    document.removeEventListener('keydown', onKey);
  };
  return node;
}

export function closePopover() {
  if (!openPopover) return;
  openPopover.__cleanup?.();
  openPopover.remove();
  openPopover = null;
}

/* ---------------------------------------------------------------- states */

export const skeleton = (lines = 3, opts = {}) => {
  const rows = Array.from({ length: lines }, (_, i) =>
    `<div class="skeleton text ${i === lines - 1 ? 'w60' : i % 2 ? 'w60' : ''}"></div>`).join('');
  return `<div aria-busy="true" aria-live="polite">${opts.block ? '<div class="skeleton block"></div>' : ''}${rows}</div>`;
};

/** Nothing to show -- and it is a real, measured nothing. */
export const empty = (message, { title = '暂无数据', icon: ic = 'file' } = {}) =>
  `<div class="state">${icon(ic, 'state-ico', 30)}`
  + `<div class="state-title">${esc(title)}</div>`
  + `<div class="state-msg">${esc(message)}</div></div>`;

/** Something failed.  Always names the failure kind and offers a retry. */
export function errorState(err, { retry = null, action = null } = {}) {
  const e = err instanceof ApiError ? err : new ApiError(String(err && err.message || err));
  const ic = e.kind === 'offline' ? 'offline' : e.kind === 'timeout' ? 'clock' : 'alert';
  const cls = e.kind === 'offline' || e.kind === 'timeout' ? 'offline' : 'error';
  const hint = e.kind === 'offline'
    ? '本地控制台可能已停止。运行 scripts\\start.ps1 重新启动。'
    : e.kind === 'timeout'
      ? '节点或本地服务响应过慢。数据仍可能可用，可稍后重试。'
      : e.status === 401 || e.status === 403
        ? '凭证被拒绝。检查 config/secrets.env 中的 Key。'
        : '服务返回了错误。下方是原始信息。';
  const id = `retry-${Math.random().toString(36).slice(2, 8)}`;
  if (retry) queueMicrotask(() => {
    const b = document.getElementById(id);
    if (b) b.addEventListener('click', retry);
  });
  return `<div class="state ${cls}">${icon(ic, 'state-ico', 30)}`
    + `<div class="state-title">${esc(e.label)}</div>`
    + `<div class="state-msg">${esc(hint)}</div>`
    + `<div class="state-msg mono" style="color:var(--ink-faint)">${esc(e.message)}</div>`
    + (retry ? `<button class="btn btn-ghost btn-sm" type="button" id="${id}">重试</button>` : '')
    + (action || '') + '</div>';
}

/** Working, but with something the reader must know before trusting it. */
export const notice = (kind, title, message) =>
  `<div class="notice ${kind}">${icon(kind === 'error' ? 'alert' : kind === 'warning' ? 'alert' : 'info', 'notice-ico', 17)}`
  + `<div class="notice-body"><div class="notice-title">${esc(title)}</div>`
  + (message ? `<div class="notice-msg">${esc(message)}</div>` : '')
  + '</div></div>';

/* --------------------------------------------------------------- segment */

/**
 * Wire a .segmented control: clicking moves the thumb and calls back.  The
 * thumb is positioned from the live button rect, so it stays correct when the
 * labels change width (which they do -- the labels are Chinese).
 */
export function segmented(root, onChange) {
  const thumb = root.querySelector('.seg-thumb');
  const buttons = Array.from(root.querySelectorAll('button'));
  function place(btn, animate = true) {
    if (!thumb || !btn) return;
    if (!animate) thumb.style.transition = 'none';
    thumb.style.transform = `translateX(${btn.offsetLeft - 3}px)`;
    thumb.style.width = `${btn.offsetWidth}px`;
    if (!animate) requestAnimationFrame(() => { thumb.style.transition = ''; });
  }
  function select(btn, animate = true) {
    buttons.forEach((b) => b.setAttribute('aria-selected', String(b === btn)));
    place(btn, animate);
  }
  buttons.forEach((b) => b.addEventListener('click', () => { select(b); onChange?.(b.dataset.value ?? b.textContent); }));
  const initial = buttons.find((b) => b.getAttribute('aria-selected') === 'true') || buttons[0];
  requestAnimationFrame(() => select(initial, false));
  return { select: (i) => select(buttons[i]), buttons };
}

/** Copy to the clipboard, with the one fallback that works without https. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制', { kind: 'good', ms: 1800 });
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.append(ta); ta.select();
    try { document.execCommand('copy'); toast('已复制', { kind: 'good', ms: 1800 }); }
    catch { toast('复制失败，请手动选择文本', { kind: 'warning' }); }
    ta.remove();
  }
}
