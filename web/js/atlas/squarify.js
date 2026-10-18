/*! web/js/atlas/squarify.js — treemap layout (Bruls/Huizing/van Wijk squarify).
 *  Extracted verbatim in behaviour from the prototype so the treemap geometry
 *  is testable without a browser. Deterministic: same input → same rectangles. */

const areaSum = (row) => row.reduce((n, d) => n + d.area, 0);

const worst = (row, side) => {
  if (!row.length || !side) return Infinity;
  const S = areaSum(row);
  const largest = Math.max(...row.map((x) => x.area));
  const smallest = Math.min(...row.map((x) => x.area));
  return Math.max((side * side * largest) / (S * S), (S * S) / (side * side * smallest));
};

/**
 * @param {Array<{value:number}>} list
 * @param {{x:number,y:number,w:number,h:number}} rect
 * @returns {Array<object>} rectangles carrying the original datum + x/y/w/h
 */
export function squarify(list, rect) {
  const total = list.reduce((n, d) => n + d.value, 0);
  if (!total) return [];
  const items = list
    .map((d) => ({ ...d, area: (d.value / total) * rect.w * rect.h }))
    .sort((a, b) => b.area - a.area);
  const box = { ...rect };
  const result = [];

  function place(row) {
    const S = areaSum(row);
    if (box.w >= box.h) {
      const w = S / box.h;
      let y = box.y;
      row.forEach((d) => {
        const h = d.area / w;
        result.push({ ...d, x: box.x, y, w, h });
        y += h;
      });
      box.x += w;
      box.w = Math.max(0, box.w - w);
    } else {
      const h = S / box.w;
      let x = box.x;
      row.forEach((d) => {
        const w = d.area / h;
        result.push({ ...d, x, y: box.y, w, h });
        x += w;
      });
      box.y += h;
      box.h = Math.max(0, box.h - h);
    }
  }

  while (items.length) {
    const row = [items.shift()];
    while (items.length && worst([...row, items[0]], Math.min(box.w, box.h)) <= worst(row, Math.min(box.w, box.h))) {
      row.push(items.shift());
    }
    place(row);
  }
  return result;
}
