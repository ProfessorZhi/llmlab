/* ==========================================================================
   router -- hash routing over lazily imported view modules.

   Hash rather than History API because this is a local console served from a
   plain HTTP server: a path route would need a catch-all rewrite, and a
   catch-all is exactly the thing that lets a different service on the same
   machine answer 200 for a route that does not exist here.

   Views are imported on first visit.  There is no bundle step, so the module
   file *is* the chunk; the delay is one local file read.
   ========================================================================== */

import { $, $$, esc } from './dom.js';
import { icon } from './icons.js';

/**
 * The route table.  `id` is the hash, `module` is imported on demand.
 * `group` draws the sidebar's section rule.
 */
export const ROUTES = [
  { id: 'overview',     label: '总览',       icon: 'overview', group: '运行',
    title: '总览',       sub: '节点、模型与推理引擎的当前状态' },
  { id: 'models',       label: '模型与服务', icon: 'cube',     group: '运行',
    title: '模型与服务', sub: '正在提供服务的模型、引擎与网关端点' },
  { id: 'benchmark',    label: 'Benchmark',  icon: 'flask',    group: '评测',
    title: 'Benchmark Lab', sub: '解码 / 首字 / 预填充吞吐与质量评测' },
  { id: 'compute',      label: '计算与监控', icon: 'cpu',      group: '评测',
    title: '计算与监控', sub: 'GPU、显存、主机负载与遥测来源' },
  { id: 'reports',      label: '报告中心',   icon: 'file',     group: '交付',
    title: '报告中心',   sub: '评测产物、原始结果与导出' },
  { id: 'integrations', label: '集成',       icon: 'plug',     group: '交付',
    title: '外部集成',   sub: 'Strata 原生界面、EvalScope 与 MCP 端点' },
  { id: 'settings',     label: '设置',       icon: 'gear',     group: '系统',
    title: '设置',       sub: '主题、刷新策略、端点与安全边界' },
];

const byId = new Map(ROUTES.map((r) => [r.id, r]));

/** The route an empty or unknown hash falls back to. */
export const HOME = 'overview';

const cache = new Map();          // id -> module namespace
let currentId = null;
let currentView = null;           // { destroy?, onLeave? }
let host = null;
let hooks = new Set();

/** Normalise `#/models`, `#models`, `models` and `''` to a route id. */
function parseHash(hash = window.location.hash) {
  const raw = String(hash || '').replace(/^#\/?/, '').split(/[?/]/)[0].trim();
  return byId.has(raw) ? raw : HOME;
}

async function load(id) {
  if (!cache.has(id)) {
    cache.set(id, import(`../views/${id}.js`));         // one entry per route
  }
  return cache.get(id);
}

function paintNav(id) {
  $$('.nav-item').forEach((a) => {
    const on = a.dataset.route === id;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  moveIndicator();
}

/**
 * The sidebar has one moving indicator rather than seven per-item borders.
 * It is positioned from the live rect so it stays right when the labels wrap
 * or the rail is in drawer mode -- and it stays hidden until the first
 * position, so it never flies in from the top-left on load.
 */
export function moveIndicator(animate = true) {
  const rail = $('#nav');
  const ind = $('#navIndicator');
  if (!rail || !ind) return;
  const active = rail.querySelector('.nav-item.active');
  if (!active) { ind.classList.remove('ready'); return; }
  if (!animate) ind.style.transition = 'none';
  ind.style.transform = `translateY(${active.offsetTop}px)`;
  ind.style.height = `${active.offsetHeight}px`;
  ind.classList.add('ready');
  if (!animate) requestAnimationFrame(() => { ind.style.transition = ''; });
}

export const setRailOpen = (open) =>
  document.body.classList.toggle('rail-open', !!open);

export const toggleRail = () => setRailOpen(!document.body.classList.contains('rail-open'));

/** Build the sidebar from the route table plus the server's external links. */
export function renderNav(external = []) {
  const rail = $('#nav');
  if (!rail) return;
  // Group the routes into one .nav-group per section, then append the
  // indicator.  The indicator is a child of #nav so its offsetTop is measured
  // against the same box the items are laid out in.
  const groups = [];
  for (const r of ROUTES) {
    if (!groups.length || groups.at(-1).group !== r.group) groups.push({ group: r.group, items: [] });
    groups.at(-1).items.push(r);
  }
  const itemHtml = (r) => `<a class="nav-item" href="#/${r.id}" data-route="${r.id}">`
    + `${icon(r.icon, 'nav-icon')}<span class="nav-text">${esc(r.label)}</span></a>`;
  rail.innerHTML =
    groups.map((g) => `<div class="nav-group">`
      + `<div class="nav-group-label">${esc(g.group)}</div>`
      + g.items.map(itemHtml).join('') + '</div>').join('')
    + `<span class="nav-indicator" id="navIndicator" aria-hidden="true"></span>`;

  const ext = (external || []).filter((l) => l && l.url);
  const extHost = $('#navExternal');
  if (extHost) {
    extHost.innerHTML = ext.length
      ? `<div class="nav-group"><div class="nav-group-label">外部系统</div>`
        + ext.map((l) =>
            `<a class="nav-item" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer"`
            + ` title="${esc(l.note || l.label || '')}">`
            + `${icon(l.icon || 'external', 'nav-icon')}<span class="nav-text">${esc(l.label || l.url)}</span>`
            + icon('external', 'nav-out', 13) + '</a>').join('')
        + '</div>'
      : '';
  }
  moveIndicator(false);
}

/** Close the drawer on any navigation -- it covers the page it navigates to. */
$('#nav')?.addEventListener('click', (ev) => {
  if (ev.target.closest('.nav-item')) setRailOpen(false);
});

/* ------------------------------------------------------------------ view */

/**
 * Render a route into the view host.  The previous view's `destroy` runs
 * first, so nothing writes into a container that has been replaced.
 */
export async function renderRoute(id = parseHash(), { replace = false } = {}) {
  const route = byId.get(id) || byId.get(HOME);
  if (!host) return;

  if (currentView && typeof currentView.destroy === 'function') {
    try { currentView.destroy(); } catch (err) { console.error('[router] destroy failed', err); }
  }
  currentView = null;

  const seq = ++renderRoute.seq;
  const doc = document;
  doc.body.classList.add('is-loading-view');

  let mod;
  try {
    mod = await load(route.id);
  } catch (err) {
    console.error('[router] import failed', err);
    host.innerHTML = `<div class="state error">${icon('alert', 'state-ico', 30)}`
      + `<div class="state-title">页面模块加载失败</div>`
      + `<div class="state-msg mono">${esc(err && err.message || err)}</div></div>`;
    doc.body.classList.remove('is-loading-view');
    return;
  }
  // A second navigation while this import was in flight wins.
  if (seq !== renderRoute.seq) return;

  currentId = route.id;
  $('#viewTitle').textContent = route.title;
  $('#viewSub').textContent = route.sub;
  document.title = `${route.title} · LLM Lab`;

  // A fresh element per route, so the enter animation replays on every change
  // and the previous view's DOM is gone rather than merely hidden.
  host.innerHTML = '';
  const panel = document.createElement('div');
  panel.className = 'view-enter';
  panel.addEventListener('animationend', () => panel.classList.remove('view-enter'), { once: true });
  host.append(panel);

  paintNav(route.id);
  if (!replace) { /* hashchange already updated history */ }

  try {
    currentView = (await mod.render(panel, { route })) || {};
  } catch (err) {
    console.error('[router] render failed', err);
    panel.innerHTML = `<div class="state error">${icon('alert', 'state-ico', 30)}`
      + `<div class="state-title">页面渲染失败</div>`
      + `<div class="state-msg mono">${esc(err && err.message || err)}</div></div>`;
  }
  doc.body.classList.remove('is-loading-view');
  host.scrollTop = 0;
  hooks.forEach((fn) => fn(route, panel));
}
renderRoute.seq = 0;

export const currentRoute = () => byId.get(currentId) || byId.get(HOME);
export const onRouteChange = (fn) => { hooks.add(fn); return () => hooks.delete(fn); };

/** Programmatic navigation that also works when the hash is already there. */
export function go(id) {
  const target = `#/${byId.has(id) ? id : HOME}`;
  if (window.location.hash === target) renderRoute(parseHash(target));
  else window.location.hash = target;
}

/* ------------------------------------------------------------------ boot */

/** Wire the router.  `mount` is the scroller that holds the view panel. */
export function startRouter(mountSelector = '#view') {
  host = $(mountSelector);
  window.addEventListener('hashchange', () => renderRoute(parseHash()));
  window.addEventListener('resize', () => { moveIndicator(false); });
  // Re-place the indicator once the webfont metrics settle.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => moveIndicator(false));
  if (!window.location.hash) window.location.replace(`#/${HOME}`);
  return renderRoute(parseHash());
}
