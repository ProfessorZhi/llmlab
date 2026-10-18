/*! web/js/router.js
 *  Hash-only routing across the views. Hash-only on purpose: the build
 *  has to work as a plain static folder / downloaded file, with no server
 *  rewrite rules. The header never leaves the screen, so only the content area
 *  is a transition target.
 */
import { $, $$ } from './dom.js';

export const PAGES = Object.freeze([
  'overview', 'models', 'api', 'calls', 'observability', 'atlas', 'evaluation', 'admin',
]);
/** Subroutes are real pages but not ninth top-level navigation tabs. */
export const SUBPAGES = Object.freeze({ compute: 'overview' });
export const ALL_ROUTES = Object.freeze([...PAGES, ...Object.keys(SUBPAGES)]);

export const pageParent = (name) => SUBPAGES[name] ?? name;

export const PAGE_NAMES = Object.freeze({
  overview: '实验室概览',
  compute: '算力中心',
  models: '模型与对话',
  api: 'API 管理',
  calls: '我的调用日志',
  observability: '监控与日志',
  atlas: '调用结构图谱',
  evaluation: '评测中心',
  admin: '管理',
});

/** Accept both native V6 hashes (#compute) and legacy Portal hashes (#/compute). */
export function pageFromHash(hash) {
  const name = typeof hash === 'string' ? hash.replace(/^#\/?/, '') : '';
  return ALL_ROUTES.includes(name) ? name : 'overview';
}

export function createRouter({ doc = document } = {}) {
  const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const navs = [$('.topnav', doc), $('.mobile-nav', doc)].filter(Boolean);
  let activePage = 'overview';
  let currentTransition = null;

  for (const nav of navs) {
    const rail = doc.createElement('span');
    rail.className = 'nav-rail';
    rail.setAttribute('aria-hidden', 'true');
    nav.appendChild(rail);
  }

  function positionRails(instant = false) {
    for (const nav of navs) {
      const rail = nav.querySelector('.nav-rail');
      const selected = nav.querySelector('.navitem.active');
      if (!rail || !selected || !nav.offsetWidth) { if (rail) rail.style.opacity = '0'; continue; }
      const x = selected.getBoundingClientRect().left - nav.getBoundingClientRect().left + nav.scrollLeft;
      if (instant) rail.style.transition = 'none';
      rail.style.width = `${selected.offsetWidth}px`;
      rail.style.transform = `translateX(${x}px)`;
      rail.style.opacity = '1';
      if (instant) requestAnimationFrame(() => { rail.style.transition = ''; });
    }
  }

  function commitPage(name, keyboardFocus) {
    $$('.view', doc).forEach((v) => {
      const selected = v.id === `view-${name}`;
      v.classList.toggle('active', selected);
      v.setAttribute('aria-hidden', String(!selected));
    });
    $$('[data-page]', doc).forEach((b) => {
      const selected = b.dataset.page === pageParent(name);
      b.classList.toggle('active', selected);
      if (selected) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
    activePage = name;
    doc.title = `Dell LLM Lab · ${PAGE_NAMES[name]}`;
    window.scrollTo({ top: 0, behavior: 'instant' });
    positionRails();
    if (keyboardFocus) {
      const h = $(`#view-${name} h1`, doc);
      if (h) { h.setAttribute('tabindex', '-1'); h.focus({ preventScroll: true }); }
    }
  }

  function goto(name, options = {}) {
    if (!ALL_ROUTES.includes(name)) return;
    const same = activePage === name;
    if (!options.fromHistory) {
      const next = `#${name}`;
      if (location.hash !== next) {
        try { history.pushState({ page: name }, '', next); } catch { location.hash = next; }
      }
    }
    if (same) { positionRails(true); return; }
    if (currentTransition) { currentTransition.skipTransition(); currentTransition = null; }
    const update = () => commitPage(name, !!options.keyboardFocus);
    if (!options.instant && !reduceMotion() && typeof doc.startViewTransition === 'function') {
      try {
        currentTransition = doc.startViewTransition(update);
        const t = currentTransition;
        // The prototype only guarded `finished`. `ready` rejects with an
        // AbortError whenever a transition is skipped — i.e. whenever someone
        // navigates twice inside one ~250ms transition — and that surfaces as
        // an unhandled rejection in the console. Swallowing it changes no
        // pixels: the DOM work already happened in `update`.
        t.ready?.catch?.(() => {});
        t.finished.catch(() => {}).finally(() => { if (currentTransition === t) currentTransition = null; });
      } catch { update(); }
    } else update();
  }

  $$('[data-page], [data-go]', doc).forEach((el) => el.addEventListener('click', (e) => {
    e.preventDefault();
    goto(el.dataset.page || el.dataset.go, { keyboardFocus: e.detail === 0 && e.isTrusted });
  }));
  window.addEventListener('popstate', () => goto(
    pageFromHash(location.hash),
    { fromHistory: true },
  ));
  window.addEventListener('hashchange', () => {
    const name = pageFromHash(location.hash);
    if (name !== activePage) goto(name, { fromHistory: true });
  });
  window.addEventListener('resize', () => positionRails(true), { passive: true });
  navs.forEach((nav) => nav.addEventListener('scroll', () => positionRails(true), { passive: true }));
  if (doc.fonts?.ready) doc.fonts.ready.then(() => positionRails(true));

  commitPage(pageFromHash(location.hash), false);
  positionRails(true);

  return { goto, commitPage, positionRails, get page() { return activePage; } };
}
