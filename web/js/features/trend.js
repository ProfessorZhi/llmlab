/*! web/js/features/trend.js — trend geometry, with gaps kept as gaps.
 *
 *  Pure arithmetic: `points -> polyline segments`. No DOM, no SVG, no strings of
 *  markup, so the two claims that matter about a drawn curve are testable under
 *  plain Node:
 *
 *  1. **A `null` point is a break, not a zero.** "We did not measure this hour"
 *     and "this hour had no traffic" are different facts, and a line that
 *     stepped down to the axis would assert the second one. So a run of `null`s
 *     splits the series into separate polylines and nothing is interpolated
 *     across it.
 *  2. **The vertical scale is shared.** When two series are drawn over one
 *     window they are scaled by the same extent, so the picture does not
 *     exaggerate whichever series happens to be smaller.
 */

/** The shared vertical extent of one or more series, ignoring gaps.
 *  Returns `null` when there is nothing measurable — the caller draws an empty
 *  state rather than an axis from 0 to 0. */
export function seriesExtent(seriesList) {
  let min = Infinity;
  let max = -Infinity;
  let known = 0;
  for (const series of seriesList ?? []) {
    for (const point of series?.points ?? []) {
      if (point === null || point === undefined) continue;
      if (!Number.isFinite(point)) continue;
      known += 1;
      if (point < min) min = point;
      if (point > max) max = point;
    }
  }
  if (!known) return null;
  return { min, max, known };
}

/**
 * Split a point list into drawable runs, in pixel space.
 *
 * @param {Array<number|null>} points
 * @param {{width:number, height:number, min:number, max:number, pad?:number}} frame
 * @returns {string[]} one SVG `points` attribute per uninterrupted run. A
 *   single-point run is returned as a two-point degenerate segment so it still
 *   renders (as a dot-sized tick) instead of vanishing.
 */
export function seriesSegments(points, frame) {
  const { width, height, min, max } = frame;
  const pad = frame.pad ?? 0;
  const n = points?.length ?? 0;
  if (!n) return [];

  const innerW = Math.max(width - pad * 2, 1);
  const innerH = Math.max(height - pad * 2, 1);
  const span = max - min;
  const stepX = n > 1 ? innerW / (n - 1) : 0;

  const place = (value, index) => {
    const x = pad + stepX * index;
    // A flat series (span 0) sits on the middle line: drawing it along the
    // bottom edge would read as "at the minimum", which is a claim.
    const ratio = span === 0 ? 0.5 : (value - min) / span;
    return { x, y: pad + innerH - ratio * innerH };
  };

  const segments = [];
  let current = [];
  for (let i = 0; i < n; i += 1) {
    const point = points[i];
    if (point === null || point === undefined || !Number.isFinite(point)) {
      if (current.length >= 2) segments.push(current);
      else if (current.length === 1) segments.push([current[0], current[0]]);
      current = [];
      continue;
    }
    current.push(place(point, i));
  }
  if (current.length >= 2) segments.push(current);
  else if (current.length === 1) segments.push([current[0], current[0]]);

  return segments.map((segment) =>
    segment.map(({ x, y }) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' '));
}

/** How many points in this series were actually measured. Used to caption a
 *  curve with its own coverage instead of implying it is complete. */
export const coverage = (points) =>
  (points ?? []).reduce((count, point) => count + (Number.isFinite(point) ? 1 : 0), 0);
