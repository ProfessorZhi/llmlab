/*! web/js/atlas/render-dots.js
 *  One dot = one (simulated) call. Canvas, not SVG: 1,248+ marks per frame is
 *  beyond what an SVG scene graph wants to carry.
 *
 *  The layout is retained between draws so pointer hit-testing can map a pixel
 *  back to a workload without re-deriving the grid.
 */
import { fmt } from '../format.js';
import { sum } from './selectors.js';

/** Width below which the tooltip is redundant. */
const H_PAD = 42;
const W_PAD = 35;

export function createDotRenderer(canvas, ctx) {
  let layout = null;

  function draw() {
    if (ctx.state.view !== 'dots') return;
    if (!ctx.isActive()) return;

    const rows = ctx.visible();
    const N = sum(rows, 'calls');
    const rect = canvas.getBoundingClientRect();
    const W = rect.width || 720;
    const H = rect.height || 480;
    const dpr = Math.min(devicePixelRatio || 1, 2);

    canvas.width = Math.max(1, Math.round(W * dpr));
    canvas.height = Math.max(1, Math.round(H * dpr));
    const g2d = canvas.getContext('2d', { alpha: false });
    g2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    g2d.fillStyle = ctx.cols.css('--bg');
    g2d.fillRect(0, 0, W, H);

    if (!N) { layout = null; return; }

    const cols = Math.ceil(Math.sqrt((N * (W - W_PAD)) / (H - H_PAD)));
    const nrows = Math.ceil(N / cols);
    const step = Math.min((W - 30) / cols, (H - 44) / nrows);
    const x0 = (W - cols * step) / 2;
    const y0 = (H - nrows * step) / 2;

    let start = 0;
    layout = {
      W, H, cols, nrows, step, x0, y0,
      blocks: rows.map((d) => {
        const b = { d, start, end: start + d.calls };
        start = b.end;
        return b;
      }),
    };

    const id = ctx.hitId();
    for (const b of layout.blocks) {
      g2d.fillStyle = ctx.cols.col(b.d.g);
      g2d.globalAlpha = id && id !== b.d.id ? 0.13 : 0.83;
      const sz = Math.max(1, step * 0.73);
      for (let i = b.start; i < b.end; i++) {
        const x = x0 + ((i % cols) + 0.5) * step;
        const y = y0 + (Math.floor(i / cols) + 0.5) * step;
        g2d.fillRect(x - sz / 2, y - sz / 2, sz, sz);
      }
    }
    g2d.globalAlpha = 1;
    g2d.fillStyle = ctx.cols.dim();
    g2d.font = '12px sans-serif';
    g2d.textAlign = 'left';
    g2d.fillText('1 点 = 1 次模拟调用', 12, H - 11);
    g2d.textAlign = 'right';
    g2d.fillText(`${fmt(N)} 点`, W - 12, H - 11);
  }

  /** Which workload sits under a pointer event, or null. */
  function datum(event) {
    if (!layout) return null;
    const r = canvas.getBoundingClientRect();
    const { cols, step, x0, y0, blocks, nrows } = layout;
    const x = event.clientX - r.left;
    const y = event.clientY - r.top;
    const c = Math.floor((x - x0) / step);
    const row = Math.floor((y - y0) / step);
    if (c < 0 || c >= cols || row < 0 || row >= nrows) return null;
    const i = row * cols + c;
    return blocks.find((b) => i >= b.start && i < b.end)?.d ?? null;
  }

  return { draw, datum, get layout() { return layout; } };
}
