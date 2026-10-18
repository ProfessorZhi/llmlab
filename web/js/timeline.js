/*! web/js/timeline.js
 *  The overview telemetry sparkline: metric switch, pointer probe, arrow-key
 *  probe, and the reveal animation. Same behaviour as the prototype, but the
 *  dataset and the drawing live apart so the numbers are inspectable.
 *
 *  **The series below is authored, so it is demo-only.** Live is the default
 *  mode, and on a live URL this module draws no curve at all: it paints the
 *  section's honest unavailable state instead, and disables the metric and range
 *  switches with the precondition spelled out. Before this, `initTimeline` drew
 *  the 24 synthetic points on every load — including a bare URL that had asked
 *  for nothing — while the page around it said the call data was not connected.
 *  A fabricated curve is the most convincing lie the overview can tell, so the
 *  gate belongs here and not only on the KPI row above it.
 */
import { $, $$, prefersReducedMotion } from './dom.js';
import { DATA_MODES } from './demo-mode.js';

/** Synthetic 24-point series for the overview chart — DEMO ONLY. */
export const TIMELINE_SERIES = Object.freeze({
  calls: [22, 18, 21, 25, 20, 24, 28, 27, 31, 38, 49, 45, 51, 53, 56, 51, 43, 49, 46, 42, 52, 62, 58, 54],
  latency: [31, 35, 28, 34, 36, 32, 39, 39, 37, 44, 46, 39, 47, 40, 43, 52, 51, 48, 46, 57, 61, 55, 49, 45],
  tokens: [20, 21, 23, 18, 24, 23, 34, 40, 36, 38, 47, 55, 52, 58, 51, 49, 46, 47, 48, 51, 60, 56, 62, 58],
});

export const METRIC_COLOR = Object.freeze({ calls: 'var(--rust)', latency: 'var(--sage)', tokens: 'var(--blue)' });
export const METRIC_UNIT = Object.freeze({ calls: '请求', latency: 'ms · p95', tokens: 'k tokens' });
export const METRIC_LABEL = Object.freeze({ calls: '请求量', latency: 'p95 延迟', tokens: 'Tokens' });

/** What the chart says on a live URL: no source, and the exact precondition. */
export const LIVE_CHART_REASON = '需要 Linux PAM 会话 + Gateway usage BFF 的服务端授权；本预览不推任何按用户聚合的调用数。';
export const LIVE_CHART_TITLE = '无授权数据源';

/** The switch tooltips in live mode: the control is real, the data is not there. */
export const LIVE_CONTROL_REASON = '无可授权数据源：切换只在有授权数据时才有意义。';

/**
 * The design-sample windows.
 *
 * The range switches used to repaint nothing but the SVG's `aria-label`: a
 * control that changed its own tooltip and no pixel. Each window now defines a
 * point count and its own axis ticks, and `deriveSeries` resamples the authored
 * 24-hour shape onto it — so the three buttons produce three visibly different
 * samples, which is the least a control may do to earn its place.
 */
export const RANGE_WINDOWS = Object.freeze({
  '24h': { count: 24, ticks: ['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '23:00'], point: (i, n) => `${String(Math.round((i * 23) / (n - 1))).padStart(2, '0')}:00` },
  '7d': { count: 7, ticks: ['-7d', '-6d', '-5d', '-4d', '-3d', '-2d', 'now'], point: (i, n) => (i === n - 1 ? 'now' : `-${n - 1 - i}d`) },
  '30d': { count: 30, ticks: ['-30d', '-25d', '-20d', '-15d', '-10d', '-5d', 'now'], point: (i, n) => (i === n - 1 ? 'now' : `-${n - 1 - i}d`) },
});

export const DEFAULT_RANGE = '24h';

/** Resample an authored series onto a window. Deterministic: bucket averages,
 *  so the shape is recognisably the same sample at every window size and a test
 *  can pin both ends without a browser. */
export function deriveSeries(base, range) {
  const spec = RANGE_WINDOWS[range] ?? RANGE_WINDOWS[DEFAULT_RANGE];
  const points = [...base];
  if (spec.count === points.length) return points;
  const out = [];
  for (let i = 0; i < spec.count; i++) {
    const from = Math.floor((i * points.length) / spec.count);
    const to = Math.max(from + 1, Math.floor(((i + 1) * points.length) / spec.count));
    const bucket = points.slice(from, to);
    out.push(bucket.reduce((sum, value) => sum + value, 0) / bucket.length);
  }
  return out;
}

const GEOM = { W: 730, H: 328, L: 42, R: 12, T: 20, B: 44, MIN: 12, MAX: 70 };

/** The chart's live stand-in. Drawn inside the same viewBox as the real plot so
 *  the section keeps its height and the page does not jump when data arrives. */
function liveChartMarkup() {
  const { W, H, L, R, T, B } = GEOM;
  const cx = (L + (W - R)) / 2;
  const cy = T + (H - T - B) / 2;
  return `<rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" rx="6"`
    + ' fill="none" stroke="var(--line)" stroke-width="1" stroke-dasharray="4 6"/>'
    + `<text x="${cx}" y="${cy - 12}" text-anchor="middle" fill="var(--muted)" font-size="13" font-weight="600">${LIVE_CHART_TITLE}</text>`
    + `<text x="${cx}" y="${cy + 10}" text-anchor="middle" fill="var(--dim)" font-size="11">等待 Gateway usage BFF 授权 · 不绘制合成曲线</text>`
    + `<text x="${L}" y="${H - 14}" fill="var(--dim)" font-size="10">—</text>`
    + `<text x="${W - R}" y="${H - 14}" text-anchor="end" fill="var(--dim)" font-size="10">—</text>`;
}

export function initTimeline({ doc = document, mode = DATA_MODES.LIVE } = {}) {
  const svg = $('#timelineChart', doc);
  if (!svg) return null;

  const demo = mode === DATA_MODES.DEMO;
  let metric = 'calls';
  let range = DEFAULT_RANGE;
  let points = [];
  let series = demo ? deriveSeries(TIMELINE_SERIES.calls, range) : [];
  let probeIndex = null;

  /** Disable a switch and say why, instead of leaving a live-looking control
   *  that changes nothing. `title` is the visible-on-hover reason; the section
   *  also carries it in text, so it is not hover-only. */
  function gateControl(node, reason = LIVE_CONTROL_REASON) {
    if (demo) return;
    node.disabled = true;
    node.setAttribute('aria-disabled', 'true');
    node.title = reason;
  }

  function chart(kind) {
    metric = kind;
    if (!demo) {
      // Live: no series, no probe points, no animation. The section states what
      // it is waiting for rather than drawing last week's design sample.
      points = [];
      probeIndex = null;
      series = [];
      svg.innerHTML = liveChartMarkup();
      svg.dataset.chartState = 'unavailable';
      if (svg.parentElement) svg.parentElement.dataset.metric = 'none';
      svg.setAttribute('tabindex', '-1');
      svg.setAttribute('aria-label', `${LIVE_CHART_TITLE}：${LIVE_CHART_REASON}`);
      return;
    }

    series = deriveSeries(TIMELINE_SERIES[kind] ?? TIMELINE_SERIES.calls, range);
    const { W, H, L, R, T, B, MIN: mi, MAX: ma } = GEOM;
    const PW = W - L - R;
    const PH = H - T - B;

    svg.parentElement.dataset.metric = kind;
    svg.parentElement.dataset.range = range;

    let lines = '';
    for (let i = 0; i < 5; i++) {
      const yy = T + (i * PH) / 4;
      lines += `<line x1="${L}" y1="${yy}" x2="${W - R}" y2="${yy}" stroke="var(--line)" stroke-width=".8"/>`
        + `<text x="${L - 11}" y="${yy + 4}" text-anchor="end" fill="var(--dim)" font-size="10">${Math.round(ma - (i * (ma - mi)) / 4)}</text>`;
    }

    points = series.map((d, i) => [L + (i * PW) / (series.length - 1), T + ((ma - d) / (ma - mi)) * PH]);
    const line = points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const area = `${line} L${(L + PW).toFixed(1)},${T + PH} L${L},${T + PH} Z`;

    const periods = (RANGE_WINDOWS[range] ?? RANGE_WINDOWS[DEFAULT_RANGE]).ticks;
    const labels = periods.map((label, i) => `<text x="${L + (i / (periods.length - 1)) * PW}" y="${H - 14}" text-anchor="${i === 0 ? 'start' : i === periods.length - 1 ? 'end' : 'middle'}" fill="var(--dim)" font-size="10">${label}</text>`).join('');

    svg.innerHTML = `<defs><linearGradient id="fillTrend" x1="0" y1="0" x2="0" y2="1">`
      + `<stop offset="0" stop-color="${METRIC_COLOR[kind]}" stop-opacity=".14"/>`
      + `<stop offset="1" stop-color="${METRIC_COLOR[kind]}" stop-opacity="0"/></linearGradient></defs>`
      + lines
      + `<path d="${area}" fill="url(#fillTrend)"/>`
      + `<path class="series-path" data-series-line d="${line}" fill="none" stroke="${METRIC_COLOR[kind]}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>`
      + points.filter((_, i) => i === 10 || i === 21).map(([x, y]) => `<circle cx="${x}" cy="${y}" r="4.2" fill="${METRIC_COLOR[kind]}" stroke="var(--bg)" stroke-width="2.6"/>`).join('')
      + labels
      + '<g class="plot-cursor" data-chart-probe visibility="hidden"></g>';

    svg.dataset.chartState = 'demo';
    svg.setAttribute('tabindex', '0');
    svg.setAttribute('aria-label', `交互式演示图表：${METRIC_LABEL[kind]}，移动指针或按左右方向键查看示意数值`);
    probeIndex = null;

    if (!prefersReducedMotion() && svg.getAnimations) {
      const path = svg.querySelector('[data-series-line]');
      if (path && typeof path.getTotalLength === 'function') {
        const length = path.getTotalLength();
        path.animate([
          { strokeDasharray: `${length} ${length}`, strokeDashoffset: length, opacity: 0.28 },
          { strokeDasharray: `${length} ${length}`, strokeDashoffset: 0, opacity: 1 },
        ], { duration: 550, easing: 'cubic-bezier(.22,1,.36,1)' });
      }
    }
  }

  function renderProbe(i, fromKeyboard = false) {
    const group = svg.querySelector('[data-chart-probe]');
    if (!group || !points.length) return;
    i = Math.max(0, Math.min(points.length - 1, i));
    probeIndex = i;
    const [x, y] = points[i];
    const bx = Math.max(50, Math.min(618, x - 54));
    const by = Math.max(12, y - 57);
    const window = RANGE_WINDOWS[range] ?? RANGE_WINDOWS[DEFAULT_RANGE];
    const time = window.point(i, points.length);
    group.innerHTML = `<line x1="${x}" y1="20" x2="${x}" y2="284" stroke="var(--ink)" stroke-opacity=".35" stroke-width=".9" stroke-dasharray="3 5"/>`
      + `<circle cx="${x}" cy="${y}" r="6" stroke="var(--bg)" stroke-width="3" fill="${METRIC_COLOR[metric]}"/>`
      + `<rect x="${bx}" y="${by}" width="112" height="41" rx="5" fill="var(--dark)"/>`
      + `<text x="${bx + 9}" y="${by + 15}" fill="var(--bg)" font-size="10" opacity=".8">${time} · DEMO</text>`
      + `<text x="${bx + 9}" y="${by + 32}" fill="var(--bg)" font-size="12" font-weight="600">${series[i]} ${METRIC_UNIT[metric]}</text>`;
    group.setAttribute('visibility', 'visible');
    if (fromKeyboard) {
      svg.setAttribute('aria-label', `${time}，演示 ${METRIC_UNIT[metric]} ${series[i]}。左右方向键浏览数据点`);
    }
  }

  svg.addEventListener('pointermove', (e) => {
    if (e.pointerType === 'touch' || !points.length) return;
    const bounds = svg.getBoundingClientRect();
    const svgX = ((e.clientX - bounds.left) / bounds.width) * GEOM.W;
    const i = Math.round(((svgX - GEOM.L) / (GEOM.W - GEOM.L - GEOM.R)) * (points.length - 1));
    if (i !== probeIndex) renderProbe(i);
  });
  svg.addEventListener('pointerleave', () => {
    const g = svg.querySelector('[data-chart-probe]');
    if (g) g.setAttribute('visibility', 'hidden');
    probeIndex = null;
  });
  svg.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    e.preventDefault();
    renderProbe((probeIndex ?? 0) + (e.key === 'ArrowRight' ? 1 : -1), true);
  });
  svg.addEventListener('blur', () => {
    const g = svg.querySelector('[data-chart-probe]');
    if (g) g.setAttribute('visibility', 'hidden');
    probeIndex = null;
  });

  chart(demo ? 'calls' : 'none');

  const metricButtons = $$('[data-chart]', doc);
  const rangeButtons = $$('[data-range]', doc);
  for (const node of [...metricButtons, ...rangeButtons]) gateControl(node);

  metricButtons.forEach((b) => b.addEventListener('click', () => {
    if (!demo) return;
    metricButtons.forEach((a) => a.classList.toggle('active', a === b));
    chart(b.dataset.chart);
  }));

  rangeButtons.forEach((b) => b.addEventListener('click', () => {
    if (!demo) return;
    setRange(b.dataset.range);
  }));

  /** A range switch redraws the sample at that window — it is not a relabelling. */
  function setRange(next) {
    if (!demo) return undefined;
    if (!Object.hasOwn(RANGE_WINDOWS, next)) return undefined;
    range = next;
    for (const b of rangeButtons) b.classList.toggle('active', b.dataset.range === next);
    chart(metric);
    svg.setAttribute('aria-label',
      `交互式演示图表：${METRIC_LABEL[metric]} · ${RANGE_WINDOWS[range].count} 点窗口 · 不代表真实历史数据`);
    paintLegendNote();
    return range;
  }

  // The legend's window caption is authored too ("DEMO · 24 POINTS"); on a live
  // URL it must not claim a window that has no data behind it.
  const legendNote = $('.legendline .mono', doc);
  function paintLegendNote() {
    if (!legendNote) return;
    legendNote.textContent = demo
      ? `DEMO · ${RANGE_WINDOWS[range].count} POINTS`
      : 'NO SOURCE · 0 POINTS';
  }
  paintLegendNote();

  return {
    chart,
    renderProbe,
    setRange,
    get metric() { return metric; },
    get range() { return range; },
    get mode() { return demo ? DATA_MODES.DEMO : DATA_MODES.LIVE; },
    get series() { return [...series]; },
  };
}
