/*! web/js/atlas/structure.js
 *  The "which layer owns the chart frame" rule, isolated so it can be tested.
 *
 *  The sunburst and treemap are SVG; the dot matrix is a canvas. Exactly one is
 *  visible at a time. The prototype's QA found a real regression here — after
 *  switching to dots the SVG stayed in flow and the frame grew — so the rule is
 *  stated once, in one place, and asserted in tests rather than re-derived at
 *  each call site.
 */

export const ATLAS_VIEWS = Object.freeze(['sunburst', 'treemap', 'dots']);

export const isAtlasView = (view) => ATLAS_VIEWS.includes(view);

/** @returns {{svgHidden:boolean, canvasHidden:boolean}} — never both false. */
export function structureVisibility(view) {
  const dots = view === 'dots';
  return { svgHidden: dots, canvasHidden: !dots };
}

/** Which renderer draws the current view. */
export const rendererFor = (view) => (view === 'sunburst' ? 'sun'
  : view === 'treemap' ? 'treemap'
    : view === 'dots' ? 'dots' : null);
