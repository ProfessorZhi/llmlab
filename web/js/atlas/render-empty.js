/*! web/js/atlas/render-empty.js
 *  The controlled empty/unknown state for every chart.
 *
 *  When the data source is not the synthetic fixture and no valid payload has
 *  arrived, charts must say so — an axis drawn over nothing reads as "zero
 *  activity", which is a different and false claim. */
import { clear, t } from '../dom.js';

export function renderChartEmpty(svg, cols, message, detail) {
  clear(svg);
  svg.setAttribute('aria-label', message);
  const [w, h] = (svg.getAttribute('viewBox') || '0 0 610 300').split(' ').slice(2).map(Number);
  t(svg, message, w / 2, h / 2 - 8, 14, cols.dim(), { 'text-anchor': 'middle' });
  if (detail) t(svg, detail, w / 2, h / 2 + 16, 11, cols.dim(), { 'text-anchor': 'middle' });
}

export function renderPanelEmpty(host, message) {
  host.replaceChildren();
  const div = document.createElement('div');
  div.style.cssText = 'padding:25px;color:var(--muted);font-size:13px';
  div.textContent = message;
  host.append(div);
}
