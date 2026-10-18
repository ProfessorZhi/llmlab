/*! web/js/atlas/theme-colors.js
 *  Theme-aware colour reads. Charts are drawn into inline SVG/canvas, which
 *  cannot use CSS variables directly, so every colour is read back off :root at
 *  draw time — which is also why the Atlas re-renders on a theme switch. */
import { cssVar } from '../dom.js';

export function createPalette(groups) {
  const css = (name) => cssVar(name);
  return {
    css,
    /** Colour for a business direction; falls back to the first atlas hue. */
    col: (groupId) => css(groups.find((g) => g.id === groupId)?.color || '--atlas-a'),
    ink: () => css('--ink'),
    dim: () => css('--dim'),
    rule: () => css('--line'),
  };
}
