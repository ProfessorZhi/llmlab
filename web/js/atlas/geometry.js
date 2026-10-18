/*! web/js/atlas/geometry.js — polar helpers for the sunburst / donut / gauge.
 *  Pure math (no DOM), so arc geometry is unit-testable. */

/** Point on a circle at `angle` degrees, measured clockwise from 12 o'clock. */
export function point(cx, cy, r, angle) {
  const theta = ((angle - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(theta), cy + r * Math.sin(theta)];
}

/** SVG path for an annular sector from `start` to `end` degrees. */
export function arc(cx, cy, r0, r1, start, end) {
  // A full circle degenerates; the prototype clamps to just under 360°.
  if (end - start >= 359.999) end = start + 359.999;
  const A = point(cx, cy, r1, start);
  const B = point(cx, cy, r1, end);
  const C = point(cx, cy, r0, end);
  const D = point(cx, cy, r0, start);
  const large = end - start > 180 ? 1 : 0;
  return `M${A[0]} ${A[1]} A${r1} ${r1} 0 ${large} 1 ${B[0]} ${B[1]} `
    + `L${C[0]} ${C[1]} A${r0} ${r0} 0 ${large} 0 ${D[0]} ${D[1]}Z`;
}

/** Slice boundaries for a set of weighted segments summing to a whole circle. */
export function segments(rows, key) {
  const total = rows.reduce((n, d) => n + (+d[key] || 0), 0);
  let angle = 0;
  return rows.map((d) => {
    const share = total ? (+d[key] || 0) / total : 0;
    const start = angle;
    const end = angle + share * 360;
    angle = end;
    return { datum: d, start, end, mid: (start + end) / 2, span: end - start, total };
  });
}
