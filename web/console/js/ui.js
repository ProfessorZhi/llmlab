/*! web/console/js/ui.js — 控制台的通用组件。 */
import { h, s } from './dom.js';
import { sunburstLayout, dotGridLayout, barsLayout, fmtInt, fmtGiB } from './charts.js';
import { STATE_TEXT } from './data.js';
import { engineLabel } from './viewmodel.js';

export function pageHead(title, sub, ...actions) {
  return h('header', { class: 'page-head' },
    h('div', {}, h('h1', { class: 'page-title' }, title), sub ? h('p', { class: 'page-sub' }, sub) : null),
    actions.length ? h('div', { style: { display: 'flex', gap: '10px', 'flex-wrap': 'wrap' } }, ...actions) : null);
}

export function emptyState(state, override) {
  const t = override ?? STATE_TEXT[state] ?? STATE_TEXT.error;
  return h('div', { class: 'empty' }, h('strong', {}, t.title), t.body);
}

export function statusBadge(label) {
  return h('span', { class: `status ${label.tone}` }, h('i', { 'aria-hidden': 'true' }), label.text);
}

export function segmented(label, options, current, onPick) {
  return h('div', { class: 'segmented', role: 'group', 'aria-label': label },
    options.map((o) => h('button', { type: 'button', 'aria-pressed': String(o === current), onclick: () => onPick(o) }, o)));
}

export function section(title, body, foot) {
  return h('div', { class: 'section' },
    title ? h('h2', { class: 'section-title' }, title) : null,
    body,
    foot ? h('p', { class: 'section-foot' }, foot) : null);
}

export function relTime(iso, now = Date.now()) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  const d = Math.max(0, now - t) / 1000;
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 86400 * 2) return '昨天';
  const dt = new Date(t);
  return `${dt.getMonth() + 1}月${dt.getDate()}日`;
}

export function clock(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '—';
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* ---------------- 指标条 ---------------- */
/** 顶部指标：资源与推理服务优先，调用量居末。没有读数一律显示「—」。 */
export function kpiBar(k) {
  const pct = (v) => `${Math.round(v * 100)}%`;
  const vramPct = k.vramUsedMiB != null && k.vramTotalMiB ? Math.min(1, k.vramUsedMiB / k.vramTotalMiB) : null;
  const meter = (ratio, label) => h('div', { class: 'meter', role: 'img', 'aria-label': `${label} ${Math.round(ratio * 100)}%` },
    h('div', { style: { width: `${(ratio * 100).toFixed(1)}%` } }));
  const KPI_STYLE = {
    '显存占用': ['#5856D6', 'memory'], 'GPU 利用率': ['#FF9500', 'usage'], '推理服务': ['#34C759', 'services'], '调用量': ['#007AFF', 'requests'],
  };
  const cell = (label, value, note) => {
    const [tint, glyph] = KPI_STYLE[label] ?? ['#007AFF', 'overview'];
    return h('div', { class: 'kpi', style: { '--tint': tint } },
      h('div', { class: 'kpi-label' }, h('span', { class: 'kpi-glyph', 'aria-hidden': 'true' }, s('svg', { viewBox: '0 0 14 14' }, ICONS[glyph]())), label),
      value, note);
  };
  const gpuNote = [k.tempC != null ? `${Math.round(k.tempC)} °C` : null, k.powerW != null ? `${Math.round(k.powerW)} W` : null].filter(Boolean).join(' · ');
  return h('section', { class: 'kpis', 'aria-label': '关键指标' },
    cell('显存占用',
      h('div', { class: 'kpi-value' }, fmtGiB(k.vramUsedMiB), h('small', {}, k.vramTotalMiB != null ? ` / ${fmtGiB(k.vramTotalMiB, 0)} GB` : ' GB')),
      vramPct == null ? h('div', { class: 'kpi-note' }, '暂无 GPU 读数') : meter(vramPct, '显存占用')),
    cell('GPU 利用率',
      h('div', { class: 'kpi-value' }, k.utilRatio == null ? '—' : pct(k.utilRatio)),
      k.utilRatio == null ? h('div', { class: 'kpi-note' }, '暂无 GPU 读数') : h('div', { class: 'kpi-note' }, gpuNote || ' ')),
    cell('推理服务',
      h('div', { class: 'kpi-value' }, k.servicesOnline == null ? '—' : String(k.servicesOnline), k.servicesTotal != null ? h('small', {}, ` / ${k.servicesTotal}`) : null),
      h('div', { class: 'kpi-note' }, k.servicesTotal == null ? '暂无登记信息' : k.servicesTotal === 0 ? '尚未登记' : '运行中 / 已登记')),
    cell('调用量',
      h('div', { class: 'kpi-value' }, k.requests == null ? '—' : fmtInt(k.requests)),
      h('div', { class: 'kpi-note' }, k.requests == null ? '暂无调用' : (k.requestsScope ?? ' '))));
}

/** 次要数据（API 调用等）的空态：没配置、没调用都属正常，用平静的说明而不是报错。读取失败仍如实说明。 */
export function quietEmpty(state, text) {
  if (state === 'error') return emptyState('error');
  if (state === 'signed_out') return emptyState('signed_out');
  return h('div', { class: 'empty quiet' }, h('strong', {}, text.title), text.body);
}

/* ---------------- 旭日图 ---------------- */
export function sunburstPanel(dist, colors) {
  let focus = null;
  const holder = h('div', { class: 'chart-row' });
  const total = () => dist.groups.reduce((t, g) => t + g.children.reduce((u, c) => u + c.value, 0), 0);
  const draw = () => {
    while (holder.firstChild) holder.firstChild.remove();
    const lay = sunburstLayout(dist.groups, { size: 360, colorOf: (id) => colors.get(id) ?? '#8E8E93' });
    const dim = (gid) => (focus && focus !== gid ? 0.2 : 1);
    const fg = dist.groups.find((g) => g.id === focus);
    const fv = fg ? fg.children.reduce((u, c) => u + c.value, 0) : lay.total;
    const aria = `调用分布：${dist.groups.map((g) => `${g.label}，${g.children.map((c) => `${c.label} ${c.value} 次`).join('，')}`).join('；')}`;
    holder.append(
      s('svg', { viewBox: '0 0 360 360', width: 300, height: 300, role: 'img', 'aria-label': aria },
        lay.inner.map((p) => s('path', { d: p.d, fill: p.color, opacity: dim(p.id), stroke: 'var(--bg)', 'stroke-width': 2.5, 'stroke-linejoin': 'round' })),
        lay.outer.map((p) => s('path', { d: p.d, fill: p.color, opacity: dim(p.group), stroke: 'var(--bg)', 'stroke-width': 2.5, 'stroke-linejoin': 'round' })),
        s('text', { x: 180, y: 168, 'text-anchor': 'middle', 'font-size': 13, fill: 'var(--secondary)' }, fg ? fg.label : '全部请求'),
        s('text', { x: 180, y: 198, 'text-anchor': 'middle', 'font-size': 28, 'font-weight': 600, fill: 'var(--label)' }, fmtInt(fv))),
      h('div', { class: 'legend', role: 'group', 'aria-label': `按${dist.innerLabel}筛选` },
        lay.inner.map((p) => h('button', {
          type: 'button', class: 'legend-item', 'aria-pressed': String(focus === p.id),
          onclick: () => { focus = focus === p.id ? null : p.id; draw(); },
        }, h('span', { class: 'swatch', style: { background: p.color, 'border-radius': '50%' } }), h('span', { class: 'name' }, p.label),
          h('span', { class: 'share' }, `${(p.value / total() * 100).toFixed(1)}%`), h('span', { class: 'val', style: { 'min-width': '56px', 'text-align': 'right' } }, fmtInt(p.value))))));
  };
  draw();
  return holder;
}

/* ---------------- 显存点阵 ---------------- */
export function vramPanel(vram) {
  let focus = null;
  const holder = h('div', { class: 'chart-row' });
  const used = vram.parts.reduce((t, p) => t + (p.mib ?? 0), 0);
  const free = vram.usedMiB != null ? Math.max(0, vram.totalMiB - vram.usedMiB) : Math.max(0, vram.totalMiB - used);
  const draw = () => {
    while (holder.firstChild) holder.firstChild.remove();
    const lay = dotGridLayout({ totalMiB: vram.totalMiB, parts: vram.parts, unitMiB: 256, cols: 24 });
    const cell = 20;
    const aria = `显存分配：${vram.parts.map((p) => `${p.label} ${fmtGiB(p.mib)} GB`).join('，')}，可用 ${fmtGiB(free)} GB`;
    holder.append(
      s('svg', { class: 'dots', viewBox: `0 0 ${lay.cols * cell} ${lay.rows * cell}`, width: lay.cols * cell, height: lay.rows * cell, role: 'img', 'aria-label': aria },
        lay.cells.map((c) => s('rect', {
          x: c.col * cell + 2, y: c.row * cell + 2, width: cell - 4, height: cell - 4, rx: 4,
          fill: c.color ?? 'var(--track)', opacity: focus && c.id !== focus ? (c.id ? 0.2 : 0.5) : 1,
        }))),
      h('div', { class: 'legend', role: 'group', 'aria-label': '按模型服务查看显存' },
        vram.parts.map((p) => h('button', {
          type: 'button', class: 'legend-item', 'aria-pressed': String(focus === p.id),
          onclick: () => { focus = focus === p.id ? null : p.id; draw(); },
        }, h('span', { class: 'swatch', style: { background: p.color } }),
          h('span', { class: 'name' }, p.label, p.owner ? h('span', { class: 'muted' }, `  ${p.owner}${p.engine ? ` · ${engineLabel(p.engine)}` : ''}`) : null),
          h('span', { class: 'val' }, `${fmtGiB(p.mib)} GB`))),
        h('div', { class: 'legend-item static' }, h('span', { class: 'swatch', style: { background: 'var(--track)' } }), h('span', { class: 'name' }, '可用'), h('span', { class: 'val' }, `${fmtGiB(free)} GB`))));
  };
  draw();
  return holder;
}

/* ---------------- 每小时柱状图 ---------------- */
export function hourlyChart(points, { title = true } = {}) {
  const lay = barsLayout(points, { width: 312, height: 96, gap: 4 });
  const peakVal = lay.peak >= 0 ? points[lay.peak] : null;
  const aria = peakVal != null ? `每小时请求数，峰值出现在 ${String(lay.peak).padStart(2, '0')}:00，为 ${fmtInt(peakVal)} 次` : '每小时请求数';
  return h('div', {},
    h('div', { style: { display: 'flex', 'justify-content': title ? 'space-between' : 'flex-end', 'align-items': 'baseline' } },
      title ? h('span', { style: { 'font-weight': 600 } }, '每小时请求数') : null,
      peakVal != null ? h('span', { class: 'muted tnum', style: { 'font-size': '13px' } }, `峰值 ${String(lay.peak).padStart(2, '0')}:00 · ${fmtInt(peakVal)}`) : null),
    s('svg', { viewBox: `0 0 ${lay.width} ${lay.height}`, width: '100%', height: lay.height, preserveAspectRatio: 'none', role: 'img', 'aria-label': aria, style: { 'margin-top': '10px', display: 'block' } },
      lay.bars.map((b) => s('rect', { x: b.x, y: b.y, width: b.w, height: b.h, rx: 2.5, fill: b.peak ? 'var(--blue)' : 'color-mix(in srgb, var(--blue) 32%, var(--bg))' }))),
    h('div', { class: 'axis', style: { 'margin-top': '4px' } }, ['00:00', '06:00', '12:00', '18:00', '24:00'].map((t) => h('span', {}, t))));
}

/* ---------------- 横向条形 ---------------- */
export function hbarList(items, colors) {
  const max = Math.max(...items.map((i) => i.value), 1);
  return items.map((i) => h('div', { class: 'hbar' },
    h('div', { class: 'hbar-top' }, h('span', {}, i.label), h('span', { class: 'muted tnum' }, fmtInt(i.value))),
    h('div', { class: 'hbar-track' }, h('div', { style: { width: `${(i.value / max * 100).toFixed(1)}%`, background: colors.get(i.id) ?? 'var(--blue)' } }))));
}

/* ---------------- 边栏图标 ---------------- */
const ICONS = {
  overview: () => [s('rect', { x: 1.5, y: 1.5, width: 4.5, height: 4.5, rx: 1, fill: '#fff' }), s('rect', { x: 8, y: 1.5, width: 4.5, height: 4.5, rx: 1, fill: '#fff' }), s('rect', { x: 1.5, y: 8, width: 4.5, height: 4.5, rx: 1, fill: '#fff' }), s('rect', { x: 8, y: 8, width: 4.5, height: 4.5, rx: 1, fill: '#fff' })],
  services: () => [s('path', { d: 'M7 1.5 12.5 4.5V9.5L7 12.5 1.5 9.5V4.5Z', fill: 'none', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linejoin': 'round' }), s('path', { d: 'M1.8 4.6 7 7.4 12.2 4.6M7 7.4V12.3', fill: 'none', stroke: '#fff', 'stroke-width': 1.4 })],
  keys: () => [s('circle', { cx: 4.5, cy: 7, r: 2.7, fill: 'none', stroke: '#fff', 'stroke-width': 1.4 }), s('path', { d: 'M7.2 7H12.5M10.5 7V9.2', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linecap': 'round' })],
  requests: () => [s('path', { d: 'M2 3.5H12M2 7H12M2 10.5H8.5', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linecap': 'round' })],
  usage: () => [s('path', { d: 'M2.5 11.5V8M5.5 11.5V5M8.5 11.5V7M11.5 11.5V2.5', stroke: '#fff', 'stroke-width': 1.6, 'stroke-linecap': 'round' })],
  evaluation: () => [s('path', { d: 'M3 7.3 5.8 10 11 4', fill: 'none', stroke: '#fff', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })],
  models: () => [s('ellipse', { cx: 7, cy: 3.6, rx: 4.6, ry: 1.8, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M2.4 3.6V10.4C2.4 11.4 4.5 12.2 7 12.2S11.6 11.4 11.6 10.4V3.6M2.4 7C2.4 8 4.5 8.8 7 8.8S11.6 8 11.6 7', fill: 'none', stroke: '#fff', 'stroke-width': 1.3 })],
  memory: () => [s('rect', { x: 2.5, y: 3.5, width: 9, height: 7, rx: 1.2, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M4.5 1.8V3.5M7 1.8V3.5M9.5 1.8V3.5M4.5 10.5V12.2M7 10.5V12.2M9.5 10.5V12.2', stroke: '#fff', 'stroke-width': 1.2, 'stroke-linecap': 'round' })],
  launch: () => [s('path', { d: 'M5.5 3H3V11H11V8.5M8 2.5H11.5V6M11.5 2.5 6.5 7.5', fill: 'none', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })],
  users: () => [s('circle', { cx: 7, cy: 4.8, r: 2.4, fill: 'none', stroke: '#fff', 'stroke-width': 1.4 }), s('path', { d: 'M2.5 12.2C3.2 9.8 5 8.8 7 8.8S10.8 9.8 11.5 12.2', fill: 'none', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linecap': 'round' })],
  extensions: () => [s('path', { d: 'M5 2V5M9 2V5M3.5 5H10.5V7.5A3.5 3.5 0 0 1 3.5 7.5Z M7 11V12.5', fill: 'none', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })],
};

export function navIcon(name, color) {
  return h('span', { class: 'nav-icon', style: { 'background-color': color }, 'aria-hidden': 'true' },
    s('svg', { viewBox: '0 0 14 14' }, ICONS[name]()));
}

export function brandMark(size = 28) {
  return s('svg', { width: size, height: size, viewBox: '0 0 28 28', 'aria-hidden': 'true' },
    s('rect', { width: 28, height: 28, rx: 7, fill: '#1D1D1F' }),
    s('path', { d: 'M8 18.5V9.5M8 18.5H12.5M15.5 9.5V18.5M15.5 18.5H20', stroke: '#FFFFFF', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
}

export function checkIcon(ok = true, size = 20) {
  if (ok === null) {
    // 未接入：灰色横线，不与失败混淆。
    return s('svg', { width: size, height: size, viewBox: '0 0 20 20', 'aria-hidden': 'true' },
      s('circle', { cx: 10, cy: 10, r: 9, fill: 'var(--fill-strong, #C7C7CC)' }),
      s('path', { d: 'M6.5 10h7', stroke: '#FFFFFF', 'stroke-width': 1.8, 'stroke-linecap': 'round' }));
  }
  return s('svg', { width: size, height: size, viewBox: '0 0 20 20', 'aria-hidden': 'true' },
    s('circle', { cx: 10, cy: 10, r: 9, fill: ok ? 'var(--green)' : 'var(--red)' }),
    ok ? s('path', { d: 'M6 10.3 8.7 12.8 14 7.4', fill: 'none', stroke: '#FFFFFF', 'stroke-width': 1.8, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })
      : s('path', { d: 'M7 7 13 13M13 7 7 13', stroke: '#FFFFFF', 'stroke-width': 1.8, 'stroke-linecap': 'round' }));
}
