/*! web/console/js/charts.js — 图表几何（纯函数，可在 Node 下测试）与 SVG 渲染。
 *
 *  三种图：
 *    - 旭日图：内环为来源（用户或 API 密钥），外环为其调用的模型服务；
 *    - 点阵图：显存分配，一格代表固定容量（借鉴国考图谱的「一点一名额」）；
 *    - 柱状图：每小时请求数，缺测的小时画成空位，不画成 0。
 *  缺失的数据保持缺失：任何输入为 null 的量都不会被当作 0 计入。
 */

/** 苹果系统色：图表系列颜色（顺序固定，保证同一对象每次同色）。 */
export const SERIES_COLORS = Object.freeze([
  '#5856D6', '#30B0C7', '#FF9500', '#AF52DE', '#34C759', '#FF2D55', '#007AFF', '#A2845E',
]);
/** 内环用的浅色版本（与 SERIES_COLORS 一一对应）。 */
export const SERIES_TINTS = Object.freeze([
  '#9B9AE9', '#86D1DE', '#FFC266', '#D19BEB', '#88DE9E', '#FF8AA3', '#7FBCFF', '#CBB59B',
]);

export const colorAt = (i) => SERIES_COLORS[i % SERIES_COLORS.length];
export const tintAt = (i) => SERIES_TINTS[i % SERIES_TINTS.length];

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

/** 环形扇区路径。角度以弧度计，0 指向正上方，顺时针。pad 为两侧留缝。 */
export function arcPath(cx, cy, r0, r1, a0, a1, pad = 0.006) {
  let s = a0 + pad;
  let e = a1 - pad;
  if (e <= s) { s = a0; e = a1; }
  const full = e - s >= Math.PI * 2 - 1e-6;
  if (full) e = s + Math.PI * 2 - 1e-4;
  const pt = (r, a) => [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  const large = e - s > Math.PI ? 1 : 0;
  const [x0, y0] = pt(r1, s);
  const [x1, y1] = pt(r1, e);
  const [x2, y2] = pt(r0, e);
  const [x3, y3] = pt(r0, s);
  const f = (n) => n.toFixed(2);
  return `M${f(x0)} ${f(y0)} A${r1} ${r1} 0 ${large} 1 ${f(x1)} ${f(y1)} `
    + `L${f(x2)} ${f(y2)} A${r0} ${r0} 0 ${large} 0 ${f(x3)} ${f(y3)} Z`;
}

/**
 * 旭日图布局。
 * @param {{id:string,label:string,children:{id:string,label:string,value:number}[]}[]} groups
 * @param {{size?:number, colorOf?:(childId:string)=>string}} opts
 */
export function sunburstLayout(groups, { size = 360, colorOf } = {}) {
  const c = size / 2;
  const clean = (groups ?? [])
    .map((g) => ({ ...g, children: (g.children ?? []).filter((ch) => finite(ch.value) && ch.value > 0) }))
    .filter((g) => g.children.length);
  const total = clean.reduce((t, g) => t + g.children.reduce((u, ch) => u + ch.value, 0), 0);
  const inner = [];
  const outer = [];
  if (!total) return { total: 0, inner, outer };
  const rIn0 = size * 0.222;
  const rIn1 = size * 0.344;
  const rOut0 = size * 0.356;
  const rOut1 = size * 0.478;
  let a = 0;
  clean.forEach((g, gi) => {
    const gv = g.children.reduce((u, ch) => u + ch.value, 0);
    const a1 = a + (gv / total) * Math.PI * 2;
    inner.push({ id: g.id, label: g.label, value: gv, share: gv / total, color: tintAt(gi), d: arcPath(c, c, rIn0, rIn1, a, a1) });
    let b = a;
    g.children.forEach((ch) => {
      const b1 = b + (ch.value / total) * Math.PI * 2;
      outer.push({ id: ch.id, label: ch.label, group: g.id, value: ch.value, color: colorOf ? colorOf(ch.id) : '#8E8E93', d: arcPath(c, c, rOut0, rOut1, b, b1) });
      b = b1;
    });
    a = a1;
  });
  return { total, inner, outer };
}

/**
 * 显存点阵布局：容量按 unit 切成格，依次分给各部分，剩余为空闲。
 * 未知占用（null）的部分不画格子，只在图例里注明。
 * @param {{totalMiB:number, parts:{id:string,label:string,mib:number|null,color:string}[], unitMiB?:number, cols?:number}} input
 */
export function dotGridLayout({ totalMiB, parts = [], unitMiB = 256, cols = 24 }) {
  if (!finite(totalMiB) || totalMiB <= 0) return { cells: [], cols, rows: 0, unitMiB };
  const count = Math.max(1, Math.round(totalMiB / unitMiB));
  const cells = [];
  for (const part of parts) {
    if (!finite(part.mib) || part.mib <= 0) continue;
    const n = Math.round(part.mib / unitMiB);
    for (let i = 0; i < n && cells.length < count; i += 1) cells.push({ id: part.id, color: part.color });
  }
  while (cells.length < count) cells.push({ id: null, color: null });
  return {
    cells: cells.map((cell, i) => ({ ...cell, col: i % cols, row: Math.floor(i / cols) })),
    cols,
    rows: Math.ceil(count / cols),
    unitMiB,
  };
}

/** 柱状图布局：null 点不产生柱子（留空位）。 */
export function barsLayout(points, { width = 312, height = 96, gap = 4 } = {}) {
  const pts = Array.isArray(points) ? points : [];
  const values = pts.filter(finite);
  const max = values.length ? Math.max(...values) : 0;
  const n = pts.length || 1;
  const bw = (width - gap * (n - 1)) / n;
  let peak = -1;
  pts.forEach((v, i) => { if (finite(v) && (peak < 0 || v > pts[peak])) peak = i; });
  const bars = [];
  pts.forEach((v, i) => {
    if (!finite(v)) return;
    const bh = max > 0 ? Math.max(2, (v / max) * (height - 2)) : 2;
    bars.push({ index: i, value: v, x: i * (bw + gap), y: height - bh, w: bw, h: bh, peak: i === peak });
  });
  return { bars, max, peak, width, height };
}

/** 千分位格式。null → —。 */
export function fmtInt(v) {
  return finite(v) ? Math.round(v).toLocaleString('en-US') : '—';
}

export function fmtGiB(mib, digits = 1) {
  return finite(mib) ? (mib / 1024).toFixed(digits) : '—';
}
