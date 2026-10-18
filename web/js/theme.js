/*! web/js/theme.js — light/dark switch.
 *  Matches the prototype: the page always opens light and the choice is
 *  in-session. Persisting it would be a behaviour change, so it is deliberately
 *  not done here (see the note in web/README.md). */
import { $ } from './dom.js';
import { THEME_EVENT } from './events.js';

export function initTheme({ doc = document } = {}) {
  const btn = $('#themeButton', doc);

  function applyTheme(theme) {
    doc.documentElement.dataset.theme = theme;
    btn.textContent = theme === 'light' ? '☾' : '☼';
    btn.title = theme === 'light' ? '切换深色模式' : '切换明亮模式';
    doc.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { theme } }));
  }

  applyTheme('light');
  btn.addEventListener('click', () => applyTheme(
    doc.documentElement.dataset.theme === 'light' ? 'dark' : 'light',
  ));

  return { applyTheme, get current() { return doc.documentElement.dataset.theme; } };
}
