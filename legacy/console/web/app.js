/* ==========================================================================
   app -- bootstrap.  Deliberately thin.

   Everything with a real responsibility lives in a module: this file only
   wires the shell (theme, sidebar, topbar, the one shared live poll) and then
   hands control to the router.
   ========================================================================== */

import { $, esc } from './core/dom.js';
import { get } from './core/api.js';
import { icon } from './core/icons.js';
import { initTheme, setTheme, theme } from './core/theme.js';
import { renderNav, startRouter, setRailOpen, toggleRail, moveIndicator, onRouteChange } from './core/router.js';
import { toast, reportError } from './core/ui.js';
import { since } from './core/store.js';
import { livePoll as live, overviewPoll, labPoll } from './core/sources.js';
import { requestRefresh, onRefresh } from './core/bus.js';

/* ------------------------------------------------------------------ theme */

function paintThemeIcon() {
  const host = $('#themeIco');
  if (host) host.innerHTML = icon(theme() === 'dark' ? 'sun' : 'moon', '', 18);
}

function wireTheme() {
  paintThemeIcon();
  $('#themeToggle')?.addEventListener('click', () => {
    const next = theme() === 'dark' ? 'light' : 'dark';
    setTheme(next);
    paintThemeIcon();
  });
  // Follow the OS while the user has not chosen, and keep the icon honest.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', paintThemeIcon);
}

/* ------------------------------------------------------------------- rail */

function wireRail() {
  const button = $('#railToggle');
  const scrim = $('#railScrim');
  const sync = () => {
    const open = document.body.classList.contains('rail-open');
    button?.setAttribute('aria-expanded', String(open));
    if (scrim) scrim.hidden = !open;
  };
  button?.addEventListener('click', () => { toggleRail(); sync(); });
  scrim?.addEventListener('click', () => { setRailOpen(false); sync(); });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && document.body.classList.contains('rail-open')) {
      setRailOpen(false); sync(); $('#railToggle')?.focus();
    }
  });
  // The drawer only exists below the tablet breakpoint; a resize past it must
  // not leave the page in a translated state.
  window.matchMedia('(min-width: 901px)').addEventListener('change', (e) => {
    if (e.matches) { setRailOpen(false); sync(); }
  });
  sync();
}

/* ------------------------------------------------------------- live poll */

/**
 * The shell subscribes to the one shared live poll defined in core/sources.js,
 * so the health pill and every view's "current value" tile are the same fetch.
 *
 * The pill is deliberately a *summary of three real probes*, and it names what
 * is wrong rather than just turning amber: a green dot over a node whose model
 * API is down is the single most misleading thing this console could draw.
 */
function summarise(d) {
  if (!d) return { state: 'unknown', text: '检测中', detail: '' };
  const ma = (d.strata || {}).model_api || {};
  const gpu = (d.strata || {}).gpu || {};
  const ssh = d.ssh || {};
  const bad = [];
  if (ma.ok === false) bad.push('模型 API');
  if (ssh.ok === false) bad.push('SSH 探针');
  if ((d.tunnel || {}).foreign) bad.push('隧道被占用');
  else if ((d.tunnel || {}).enabled && !(d.tunnel || {}).listening) bad.push('隧道');
  const gpuNote = gpu.available === false ? 'GPU 遥测不可用' : '';
  const state = bad.length ? 'warn' : 'ok';
  const text = bad.length ? `部分异常：${bad.join(' / ')}` : '节点可达';
  const bits = [];
  if (ma.model) bits.push(ma.model);
  if (typeof gpu.util_pct === 'number') bits.push(`GPU ${gpu.util_pct}%`);
  if (gpuNote) bits.push(gpuNote);
  return { state, text, detail: bits.join(' · ') };
}

function paintLive(d, meta) {
  const { state, text, detail } = summarise(d);
  const pill = $('#healthPill');
  if (pill) {
    pill.dataset.state = state;
    $('#healthText').textContent = text;
  }
  const dot = $('#footDot');
  if (dot) dot.dataset.state = state;
  const fs = $('#footStatus');
  if (fs) fs.textContent = meta.error ? `实时数据中断（${meta.error.label || '失败'}）` : detail || text;
  const fm = $('#footMeta');
  if (fm) {
    const n = d && d.node;
    const parts = [];
    if (n && n.hostname) parts.push(n.hostname);
    if (n && typeof n.uptime_s === 'number') parts.push(`up ${Math.round(n.uptime_s / 3600)}h`);
    if (d && d.ssh && d.ssh.mem && typeof d.ssh.mem.used_pct === 'number') {
      parts.push(`RAM ${d.ssh.mem.used_pct.toFixed(0)}%`);
    }
    parts.push(since(meta.at));
    fm.innerHTML = esc(parts.join(' · '));
  }
  if (pill) pill.title = `${text}${detail ? ` — ${detail}` : ''}（${since(meta.at)}）`;
}

live.subscribe(paintLive);

/* -------------------------------------------------------------- topbar */

function wireRefresh() {
  const button = $('#refreshAll');
  button?.addEventListener('click', async () => {
    button.classList.add('spin');
    requestRefresh('all');
    try { await live.refresh({ force: true }); }
    finally { setTimeout(() => button.classList.remove('spin'), 700); }
  });
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  initTheme();
  wireTheme();
  wireRail();
  wireRefresh();
  // The three shared sources are owned by the shell and started once, here.
  // Views subscribe and unsubscribe to them; none of them starts or stops
  // one, because a view that is left is not necessarily a source that is
  // unneeded -- the health pill needs the live poll on every page.
  live.start();
  overviewPoll.start();
  labPoll.start();

  // The nav's external half comes from the server: which systems exist, where
  // they are, and whether they answer right now.  It is fetched once and
  // refreshed on demand -- it is a link list, not a live gauge.
  let nav = [];
  let lab = null;
  try {
    lab = await get('/api/lab', { timeout: 15000 });
    nav = ((lab.nav || {}).groups || [])
      .flatMap((g) => (g.items || []).filter((i) => i.external && i.url))
      .map((i) => ({ ...i, note: i.note || '' }));
    const sub = $('#brandSub');
    if (sub && lab.node?.label) sub.textContent = lab.node.label;
  } catch (err) {
    reportError(err, '导航载入失败');
  }

  renderNav(nav);
  await startRouter('#view');

  onRouteChange(() => moveIndicator());
  onRefresh(() => {
    if (document.visibilityState !== 'visible') return;
    live.refresh({ force: true });
  });

  // A nav that renders blank is worse than one that says why.
  if (!nav.length) {
    const host = $('#navExternal');
    if (host) host.innerHTML = `<div class="nav-group">外部系统</div>`
      + `<p class="hint" style="padding:0 10px">未取到外部系统清单（/api/lab 的 nav.groups）。</p>`;
  }

  // Version stamp, from the same document, so a stale page is visible.
  const fm = $('#footMeta');
  if (fm && lab && lab.version && !fm.textContent.includes('v')) {
    fm.dataset.version = `v${lab.version}`;
  }
  if (lab && lab.version) {
    const brand = $('#brandSub');
    if (brand) brand.title = `LLM Lab v${lab.version}`;
  }
}

// Surface genuinely unexpected errors instead of a silently dead page.
window.addEventListener('unhandledrejection', (ev) => {
  const err = ev.reason;
  if (err && err.name === 'ApiError' && err.kind === 'aborted') { ev.preventDefault(); return; }
  console.error('[app] unhandled rejection', err);
});
window.addEventListener('error', (ev) => {
  if (ev.message && /ResizeObserver loop/.test(ev.message)) return;
});

boot().catch((err) => {
  console.error('[app] boot failed', err);
  const host = $('#view');
  if (host) {
    host.innerHTML = `<div class="state error" style="margin:var(--sp-6)">${icon('alert', 'state-ico', 30)}`
      + `<div class="state-title">控制台初始化失败</div>`
      + `<div class="state-msg mono">${esc(err && err.message || err)}</div></div>`;
  }
  toast('控制台初始化失败，请查看浏览器控制台', { kind: 'error' });
});
