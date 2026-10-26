/*! web/console/js/ui.js — 控制台的通用组件。 */
import { h, s } from './dom.js';
import { sunburstLayout, dotGridLayout, barsLayout, arcPath, fmtInt, fmtGiB } from './charts.js';
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
const shortStamp = (iso) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

/**
 * 调用量脚注。三种状态必须分开说：
 * 1. 有本人数字 —— 报数字与口径；
 * 2. **接了源、但窗口内没有** —— 说「0 次」并给出日志最近一条的时间；
 * 3. 没接线 / 读不到 —— 分别说明，都不能冒充成「0 次」。
 */
export function requestsNote(k) {
  if (k.requests != null) return k.requestsScope ?? ' ';
  const f = k.requestsFeed;
  if (!f) return '调用数据未接入';
  if (!f.readable) return '调用日志读不到（权限或路径）';
  if (!f.events) return '调用日志为空 · 还没有调用';
  const when = f.lastTs ? shortStamp(f.lastTs) : null;
  if (f.inWindow) {
    if (f.keysBound === 0) return `近 24 小时 ${fmtInt(f.inWindow)} 次 · 密钥无属主声明，归不到人`;
    return `近 24 小时 ${fmtInt(f.inWindow)} 次 · 不在你的密钥上`;
  }
  return `近 24 小时 0 次 · 日志最近 ${when ?? '—'}`;
}

/** 顶部指标：资源与推理服务优先，调用量居末。没有读数一律显示「—」。 */
export function kpiBar(k) {
  const pct = (v) => `${Math.round(v * 100)}%`;
  const vramPct = k.vramUsedMiB != null && k.vramTotalMiB ? Math.min(1, k.vramUsedMiB / k.vramTotalMiB) : null;
  const meter = (ratio, label) => h('div', { class: 'meter', role: 'img', 'aria-label': `${label} ${Math.round(ratio * 100)}%` },
    h('div', { style: { width: `${(ratio * 100).toFixed(1)}%` } }));
  const KPI_STYLE = {
    '显存占用': ['#5856D6', 'memory'], 'GPU 利用率': ['#FF9500', 'usage'], '登记引擎': ['#34C759', 'services'], '调用量': ['#007AFF', 'requests'],
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
    cell('登记引擎',
      h('div', { class: 'kpi-value' }, k.servicesOnline == null ? '—' : String(k.servicesOnline), k.servicesTotal != null ? h('small', {}, ` / ${k.servicesTotal}`) : null),
      h('div', { class: 'kpi-note' }, k.servicesTotal == null ? '暂无可信登记' : k.servicesTotal === 0 ? '尚未登记' : '健康 / 已登记')),
    cell('调用量',
      h('div', { class: 'kpi-value' }, k.requests == null ? '—' : fmtInt(k.requests)),
      h('div', { class: 'kpi-note' }, requestsNote(k))));
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
  const free = vram.freeMiB != null
    ? Math.max(0, vram.freeMiB)
    : vram.usedMiB != null ? Math.max(0, vram.totalMiB - vram.usedMiB) : Math.max(0, vram.totalMiB - used);
  const draw = () => {
    while (holder.firstChild) holder.firstChild.remove();
    const lay = dotGridLayout({ totalMiB: vram.totalMiB, parts: vram.parts, unitMiB: 256, cols: 24 });
    const cell = 20;
    const aria = `显存状态：${vram.parts.map((p) => `${p.label} ${fmtGiB(p.mib)} GB`).join('，')}，可用 ${fmtGiB(free)} GB`;
    holder.append(
      s('svg', { class: 'dots', viewBox: `0 0 ${lay.cols * cell} ${lay.rows * cell}`, width: lay.cols * cell, height: lay.rows * cell, role: 'img', 'aria-label': aria },
        lay.cells.map((c) => s('rect', {
          x: c.col * cell + 2, y: c.row * cell + 2, width: cell - 4, height: cell - 4, rx: 4,
          fill: c.color ?? 'var(--track)', opacity: focus && c.id !== focus ? (c.id ? 0.2 : 0.5) : 1,
        }))),
      h('div', { class: 'legend', role: 'group', 'aria-label': '显存组成与可用量' },
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
  // 齿轮：引擎是「装着的那个东西」，与「正在跑的服务」（立方体）分开用同一个隐喻。
  engines: () => [s('circle', { cx: 7, cy: 7, r: 2.2, fill: 'none', stroke: '#fff', 'stroke-width': 1.4 }), s('path', { d: 'M7 1.5V3.1M7 10.9V12.5M1.5 7H3.1M10.9 7H12.5M3.2 3.2 4.3 4.3M9.7 9.7 10.8 10.8M10.8 3.2 9.7 4.3M4.3 9.7 3.2 10.8', fill: 'none', stroke: '#fff', 'stroke-width': 1.3, 'stroke-linecap': 'round' })],
  memory: () => [s('rect', { x: 2.5, y: 3.5, width: 9, height: 7, rx: 1.2, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M4.5 1.8V3.5M7 1.8V3.5M9.5 1.8V3.5M4.5 10.5V12.2M7 10.5V12.2M9.5 10.5V12.2', stroke: '#fff', 'stroke-width': 1.2, 'stroke-linecap': 'round' })],
  gateway: () => [s('path', { d: 'M2 4.5H9.5M7.5 2.5 9.8 4.5 7.5 6.5M12 9.5H4.5M6.5 7.5 4.2 9.5 6.5 11.5', fill: 'none', stroke: '#fff', 'stroke-width': 1.4, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })],
  route: () => [s('circle', { cx: 3.5, cy: 3.5, r: 1.6, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('circle', { cx: 10.5, cy: 10.5, r: 1.6, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M5 3.5H8.5A2 2 0 0 1 8.5 7.5H5.5A2 2 0 0 0 5.5 11.5H9', fill: 'none', stroke: '#fff', 'stroke-width': 1.3, 'stroke-linecap': 'round' })],
  chat: () => [s('path', { d: 'M2.5 3.5A1.5 1.5 0 0 1 4 2H10A1.5 1.5 0 0 1 11.5 3.5V8A1.5 1.5 0 0 1 10 9.5H6L3.5 11.8V9.5H4A1.5 1.5 0 0 1 2.5 8Z', fill: 'none', stroke: '#fff', 'stroke-width': 1.3, 'stroke-linejoin': 'round' })],
  token: () => [s('circle', { cx: 7, cy: 7, r: 4.6, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M5 7H9M7 5V9', stroke: '#fff', 'stroke-width': 1.3, 'stroke-linecap': 'round' })],
  timer: () => [s('circle', { cx: 7, cy: 7.8, r: 4.4, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M7 5.4V7.9L8.6 9M5.8 1.8H8.2', fill: 'none', stroke: '#fff', 'stroke-width': 1.3, 'stroke-linecap': 'round' })],
  check: () => [s('path', { d: 'M3 7.3 5.8 10 11 4', fill: 'none', stroke: '#fff', 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })],
  cpu: () => [s('rect', { x: 3.5, y: 3.5, width: 7, height: 7, rx: 1.2, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M5.5 1.8V3.5M8.5 1.8V3.5M5.5 10.5V12.2M8.5 10.5V12.2M1.8 5.5H3.5M1.8 8.5H3.5M10.5 5.5H12.2M10.5 8.5H12.2', stroke: '#fff', 'stroke-width': 1.2, 'stroke-linecap': 'round' })],
  account: () => [s('circle', { cx: 7, cy: 5, r: 2.3, fill: 'none', stroke: '#fff', 'stroke-width': 1.3 }), s('path', { d: 'M2.8 12C3.4 9.7 5 8.7 7 8.7S10.6 9.7 11.2 12', fill: 'none', stroke: '#fff', 'stroke-width': 1.3, 'stroke-linecap': 'round' })],
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

export function glyph(name) {
  return s('svg', { viewBox: '0 0 14 14' }, (ICONS[name] ?? ICONS.overview)());
}

/** 指标卡（与概览同一套视觉）：``[{label, tint, glyph, value, unit, note}]``。 */
export function metricCards(items) {
  return h('section', { class: 'kpis', 'aria-label': '关键指标' }, items.map((it) => h('div', { class: 'kpi', style: { '--tint': it.tint } },
    h('div', { class: 'kpi-label' }, h('span', { class: 'kpi-glyph', 'aria-hidden': 'true' }, glyph(it.glyph)), it.label),
    h('div', { class: 'kpi-value' }, it.value ?? '—', it.unit ? h('small', {}, ` ${it.unit}`) : null),
    h('div', { class: 'kpi-note' }, it.note ?? ' '))));
}

/** 滚动窗口的柱状图：``labels`` 是横轴刻度（左到右），峰值高亮。 */
export function windowBars(points, { labels = [], height = 120, tint = 'var(--blue)', unit = '次' } = {}) {
  const lay = barsLayout(points, { width: 600, height, gap: points.length > 24 ? 3 : 5 });
  const peakVal = lay.peak >= 0 ? points[lay.peak] : null;
  return h('div', {},
    s('svg', { viewBox: `0 0 ${lay.width} ${lay.height}`, width: '100%', height: lay.height, preserveAspectRatio: 'none', role: 'img',
      'aria-label': peakVal != null ? `峰值 ${fmtInt(peakVal)} ${unit}` : '暂无数据', style: { display: 'block' } },
    lay.bars.map((b) => s('rect', { x: b.x, y: b.y, width: b.w, height: Math.max(b.h, 1.5), rx: 3, fill: b.peak ? tint : `color-mix(in srgb, ${tint} 30%, var(--card))` }))),
    h('div', { class: 'axis', style: { 'margin-top': '6px' } }, labels.map((t) => h('span', {}, t))));
}

/**
 * 环形图：``items`` = ``[{id, label, value, color, me?}]``。点图例或扇区聚焦；``me`` 的一块外扩并标「我」。
 * ``rest`` 是不属于任何人的剩余（如「空闲」），灰色、不可聚焦。
 *
 * ``focus`` / ``onFocus`` 是给**每秒重绘**的页面用的：聚焦状态放在页面自己的
 * state 里，重绘时传回来。不给的话就按「我」自动高亮一次——那是第一次渲染的行为，
 * 若每次都这样，用户点中的那一块会被下一秒的重绘悄悄清掉。
 */
export function donutPanel(items, { title = '', fmt = fmtInt, rest = null, size = 220, focus: keptFocus, onFocus = null } = {}) {
  let focus = keptFocus !== undefined ? keptFocus : items.find((i) => i.me)?.id ?? null;
  const holder = h('div', { class: 'donut-row' });
  const all = [...items.filter((i) => i.value > 0), ...(rest && rest.value > 0 ? [{ ...rest, id: '__rest', color: 'var(--track)', rest: true }] : [])];
  const total = all.reduce((t, i) => t + i.value, 0);
  const setFocus = (id) => {
    focus = id;
    if (onFocus) onFocus(id);
    draw();
  };
  const draw = () => {
    while (holder.firstChild) holder.firstChild.remove();
    const c = size / 2; const r0 = size * 0.30; const r1 = size * 0.46;
    let a = 0;
    const arcs = all.map((it) => {
      const a1 = a + (total ? it.value / total : 0) * Math.PI * 2;
      const pop = it.id === focus && !it.rest ? size * 0.025 : 0;
      const mid = (a + a1) / 2;
      const d = arcPath(c + pop * Math.sin(mid), c - pop * Math.cos(mid), r0, r1 + (it.id === focus ? size * 0.015 : 0), a, a1, 0.008);
      a = a1;
      return s('path', { d, fill: it.color, opacity: focus && it.id !== focus && !it.rest ? 0.35 : 1, stroke: 'var(--card)', 'stroke-width': 1.5,
        style: { cursor: it.rest ? 'default' : 'pointer', transition: 'opacity .2s' }, onclick: () => { if (!it.rest) setFocus(focus === it.id ? null : it.id); } });
    });
    const f = all.find((i) => i.id === focus);
    const aria = `${title}：${all.map((i) => `${i.label} ${fmt(i.value)}`).join('，')}`;
    holder.append(
      s('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img', 'aria-label': aria }, arcs,
        s('text', { x: c, y: c - 6, 'text-anchor': 'middle', 'font-size': 12, fill: 'var(--secondary)' }, f ? f.label : title),
        s('text', { x: c, y: c + 18, 'text-anchor': 'middle', 'font-size': 20, 'font-weight': 600, fill: 'var(--label)' }, f ? `${total ? Math.round(f.value / total * 100) : 0}%` : fmt(total))),
      h('div', { class: 'legend', role: 'group', 'aria-label': `${title} 图例` }, all.map((it) => h(it.rest ? 'div' : 'button', {
        type: it.rest ? null : 'button', class: `legend-item${it.rest ? ' static' : ''}${it.me ? ' me' : ''}`, 'aria-pressed': it.rest ? null : String(focus === it.id),
        onclick: it.rest ? null : () => setFocus(focus === it.id ? null : it.id),
      }, h('span', { class: 'swatch', style: { background: it.color, 'border-radius': '50%' } }),
        h('span', { class: 'name' }, it.label, it.me ? h('span', { class: 'me-tag' }, '我') : null),
        h('span', { class: 'share' }, total ? `${(it.value / total * 100).toFixed(1)}%` : '—'),
        h('span', { class: 'val', style: { 'min-width': '64px', 'text-align': 'right' } }, fmt(it.value))))));
  };
  draw();
  return holder;
}

/** 小号环形进度：``ratio`` 0–1，null 显示空环。 */
export function ringGauge(ratio, { tint = 'var(--blue)', size = 56 } = {}) {
  const c = size / 2; const r = size * 0.4;
  const len = 2 * Math.PI * r;
  const v = ratio == null ? 0 : Math.max(0, Math.min(1, ratio));
  return s('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img', 'aria-label': ratio == null ? '无读数' : `${Math.round(v * 100)}%` },
    s('circle', { cx: c, cy: c, r, fill: 'none', stroke: 'var(--track)', 'stroke-width': size * 0.12 }),
    s('circle', { cx: c, cy: c, r, fill: 'none', stroke: tint, 'stroke-width': size * 0.12, 'stroke-linecap': 'round',
      'stroke-dasharray': `${(len * v).toFixed(2)} ${len.toFixed(2)}`, transform: `rotate(-90 ${c} ${c})`, style: { transition: 'stroke-dasharray .6s' } }),
    s('text', { x: c, y: c + 4, 'text-anchor': 'middle', 'font-size': size * 0.22, 'font-weight': 600, fill: 'var(--label)' }, ratio == null ? '—' : `${Math.round(v * 100)}`));
}

/** 星期 × 小时热力图：``matrix[7][24]``（周一在上）。 */
export function heatmap(matrix, { tint = 'var(--blue)' } = {}) {
  const max = Math.max(1, ...matrix.flat());
  const days = ['一', '二', '三', '四', '五', '六', '日'];
  return h('div', {},
    h('div', { style: { display: 'grid', 'grid-template-columns': '18px 1fr', gap: '6px', 'align-items': 'center' } },
      matrix.map((row, d) => [h('span', { class: 'muted', style: { 'font-size': '11px' } }, days[d]),
        h('div', { class: 'heat', style: { 'grid-template-columns': 'repeat(24, 1fr)' } }, row.map((v, hr) => h('div', {
          class: 'cellh', title: `周${days[d]} ${String(hr).padStart(2, '0')}:00 · ${v} 次`,
          style: v ? { background: `color-mix(in srgb, ${tint} ${Math.round(18 + (v / max) * 82)}%, var(--card))` } : null })))])),
    h('div', { class: 'heat-axis', style: { 'margin-left': '24px' } }, ['00', '06', '12', '18', '24'].map((t) => h('span', {}, t))));
}
