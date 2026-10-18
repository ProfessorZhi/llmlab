/*! web/js/atlas/heat.js
 *  The 7×24 activity matrix. `syntheticHour` / `syntheticLatency` are a closed-
 *  form wave — no stored history, no randomness — so the demo heatmap is
 *  reproducible and provably not production telemetry.
 */

export const HEAT_DAYS = Object.freeze(['周一', '周二', '周三', '周四', '周五', '周六', '周日']);
export const HOUR_COUNT = 24;
export const HEAT_CELLS = HEAT_DAYS.length * HOUR_COUNT; // 168

export const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

const WEEKLY = [0.95, 1.10, 0.94, 1.22, 1.16, 0.65, 0.50];

/** Synthetic request volume for one day/hour cell. */
export function syntheticHour(day, hour) {
  const peak = Math.exp(-Math.pow((hour - 14) / 5.3, 2));
  const morning = 0.55 * Math.exp(-Math.pow((hour - 9) / 2.8, 2));
  const night = 0.4 * Math.exp(-Math.pow((hour - 21) / 3, 2));
  const weekly = WEEKLY[day];
  return Math.round(6 + weekly * (17 + 55 * peak + 22 * morning + 17 * night) + 5 * Math.cos(day * 2.2 + hour * 0.8));
}

/** Synthetic latency pressure for one day/hour cell (ms). */
export function syntheticLatency(day, hour) {
  return Math.round(420 + 13 * syntheticHour(day, hour) + 170 * Math.sin((day + 1) * 0.9 + hour * 0.26));
}

/** Linear blend of two #rrggbb colours; `t` in [0,1]. */
export function hexMix(a, b, t) {
  const rgb = (x) => {
    const v = x.replace('#', '').trim();
    return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16) || 0);
  };
  const A = rgb(a);
  const B = rgb(b);
  const v = A.map((x, i) => Math.round(x * (1 - t) + B[i] * t));
  return `#${v.map((x) => x.toString(16).padStart(2, '0')).join('')}`;
}

/** Value + fill for one cell, given the active metric and theme colours. */
export function heatCell(day, hour, metric, wash, accent) {
  const value = metric === 'volume' ? syntheticHour(day, hour) : syntheticLatency(day, hour);
  const max = metric === 'volume' ? 130 : 2500;
  const strength = clamp(value / max, 0.07, 0.92);
  return { value, fill: hexMix(wash, accent, 0.09 + 0.83 * strength) };
}

/** Accessible name for one grid cell. */
export const heatDayLabel = (day, hour, metric, value) =>
  `${day}${String(hour).padStart(2, '0')}:00，模拟${metric === 'volume' ? '请求强度' : '延迟压力'} ${value}${metric === 'volume' ? '次' : 'ms'}`;

/** Every (day, hour) pair with its value — the test-friendly whole matrix. */
export function heatMatrix(metric) {
  const out = [];
  for (let d = 0; d < HEAT_DAYS.length; d++) {
    for (let h = 0; h < HOUR_COUNT; h++) {
      out.push({ day: d, hour: h, value: metric === 'volume' ? syntheticHour(d, h) : syntheticLatency(d, h) });
    }
  }
  return out;
}
