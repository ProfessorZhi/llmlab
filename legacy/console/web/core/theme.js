/* ==========================================================================
   theme -- dark / light, and the one place a transition is deliberately wide.

   Switching theme repaints every surface at once, so the *colour* properties
   get a short transition while layout properties do not.  Transitioning
   `all` here would animate the sidebar width and every grid track along with
   the colours, which reads as the whole page twitching.
   ========================================================================== */

const KEY = 'llmlab.theme';
const listeners = new Set();

export const theme = () => document.documentElement.dataset.theme || 'dark';

export function setTheme(next, { persist = true } = {}) {
  const value = next === 'light' ? 'light' : 'dark';
  if (theme() === value) return value;
  document.documentElement.dataset.theme = value;
  if (persist) { try { localStorage.setItem(KEY, value); } catch { /* private mode */ } }
  listeners.forEach((fn) => fn(value));
  return value;
}

export const toggleTheme = () => setTheme(theme() === 'dark' ? 'light' : 'dark');

export function onTheme(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/**
 * Resolve the starting theme: an explicit choice wins, otherwise the system
 * preference -- and the system preference keeps being followed until the
 * user makes a choice, at which point the OS stops mattering.
 */
export function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch { /* ignore */ }
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  setTheme(saved || (mq.matches ? 'dark' : 'light'), { persist: false });
  mq.addEventListener('change', (e) => {
    let has = null;
    try { has = localStorage.getItem(KEY); } catch { /* ignore */ }
    if (!has) setTheme(e.matches ? 'dark' : 'light', { persist: false });
  });
  return theme();
}

/**
 * Read a design token.  Charts are SVG and need real colour strings, not
 * `var(--x)`, so the palette is lifted out of the stylesheet rather than
 * duplicated as JS constants -- which is how a theme switch ends up applying
 * to everything except the charts.
 */
export function token(name, fallback = '') {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** The four categorical series, in their fixed slot order. */
export const series = () => [
  token('--series-1', '#4f95ee'),
  token('--series-2', '#e0713c'),
  token('--series-3', '#23a87b'),
  token('--series-4', '#a98bf0'),
];

/** Re-render charts after a theme change: canvas/SVG colours are baked in. */
export function onThemeRepaint(fn) {
  const root = document.documentElement;
  let raf = 0;
  const obs = new MutationObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => fn(theme()));
  });
  obs.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  return () => obs.disconnect();
}
