import { h, s, put, clear } from './dom.js';
import { colorAt, sunburstLayout, fmtGiB } from './charts.js';
import { quietEmpty } from './ui.js';

const known = v => typeof v === 'number' && Number.isFinite(v) && v >= 0;
const short = v => Array.from(String(v)).slice(0, 10).join('');
const gib = v => `${fmtGiB(v)} GB`;
export function resourceSeries(sys) {
  return (sys.per_user ?? []).filter(u => u.account === 'human').map((u, i) => ({
    ...u, color: colorAt(i), cpu: known(u.cpu_ratio) && sys.cpu?.cores > 0 ? u.cpu_ratio / sys.cpu.cores * 100 : null,
  }));
}
export function gpuHierarchy(sys) {
  const groups = new Map();
  for (const p of sys.gpu_processes ?? []) {
    if (!known(p.used_mib) || p.used_mib === 0) continue;
    const user = p.user ?? `uid ${p.uid ?? '未知'}`;
    if (!groups.has(user)) groups.set(user, { id: user, label: user, children: [] });
    groups.get(user).children.push({ id: `${user}:${p.pid}:${groups.get(user).children.length}`, label: `PID ${p.pid}`, value: p.used_mib });
  }
  return [...groups.values()];
}
const empty = text => quietEmpty('ready', { title: '暂无可绘制的读数', body: text });
const axisText = (x, y, text, anchor = 'end') => s('text', { x, y, 'text-anchor': anchor, fill: 'var(--secondary)', 'font-size': 11 }, text);

function columns(rows) {
  return h('div', { class: 'resource-columns' }, [
    ['内存', 'memory_mib', gib], ['CPU · 整机占比', 'cpu', v => `${v.toFixed(1)}%`], ['显存', 'vram_mib', gib],
  ].map(([title, key, fmt]) => {
    const valid = rows.filter(r => known(r[key]));
    if (!valid.length) return h('div', {}, h('h3', {}, title), empty('该指标尚未上报。'));
    const max = Math.max(...valid.map(r => r[key]), 1);
    const width = Math.max(280, rows.length * 56 + 46), plot = width - 56, step = plot / Math.max(1, rows.length);
    return h('div', { class: 'resource-column' }, h('h3', {}, title), h('div', { class: 'chart-scroll', tabindex: 0, 'aria-label': `${title}柱状图，可横向滚动` },
      s('svg', { viewBox: `0 0 ${width} 250`, width, height: 250, role: 'img', 'aria-label': `${title}：${rows.map(r => `${r.user} ${known(r[key]) ? fmt(r[key]) : '未知'}`).join('；')}` },
        [0, .5, 1].map(t => [s('line', { x1: 48, x2: width - 8, y1: 202 - t * 166, y2: 202 - t * 166, stroke: 'var(--hairline-strong)', 'stroke-dasharray': t ? '3 5' : null }), axisText(42, 206 - t * 166, fmt(max * t))]),
        rows.map((r, i) => {
          const x = 48 + i * step + step / 2, height = known(r[key]) ? r[key] / max * 166 : 0;
          return [known(r[key]) ? s('rect', { x: x - Math.min(15, step * .28), y: 202 - height, width: Math.min(30, step * .56), height: Math.max(height, 1), rx: Math.min(5, height / 2), fill: r.color }, s('title', {}, `${r.user} · ${fmt(r[key])}`)) : axisText(x, 193, '—', 'middle'),
            axisText(x, 224, short(r.user), 'middle')];
        })))) ;
  }));
}

function scatter(rows, selected, pick) {
  const points = rows.filter(r => known(r.cpu) && known(r.memory_mib));
  if (!points.length) return empty('需要同一时刻的用户 CPU 与内存读数。');
  const xmax = Math.max(1, ...points.map(r => r.cpu)) * 1.15;
  const ymax = Math.max(1024, ...points.map(r => r.memory_mib)) * 1.15;
  const vmax = Math.max(1, ...points.map(r => r.vram_mib ?? 0));
  const active = points.find(r => r.user === selected);
  return h('div', { class: 'resource-scatter' },
    s('svg', { viewBox: '0 0 620 330', role: 'img', 'aria-label': '用户资源散点图：横轴 CPU 整机占比，纵轴内存，点面积表示显存。下方图例可选择用户。' },
      [0, .25, .5, .75, 1].map(t => [
        s('line', { x1: 64, x2: 592, y1: 274 - t * 238, y2: 274 - t * 238, stroke: 'var(--hairline-strong)', 'stroke-dasharray': '3 5' }),
        axisText(54, 278 - t * 238, gib(ymax * t)), axisText(64 + t * 528, 295, `${(xmax * t).toFixed(1)}%`, 'middle'),
      ]), axisText(64, 18, '内存', 'start'), axisText(592, 320, 'CPU · 整机占比'),
      points.map(r => s('circle', { cx: 64 + r.cpu / xmax * 528, cy: 274 - r.memory_mib / ymax * 238,
        r: known(r.vram_mib) ? Math.sqrt(36 + r.vram_mib / vmax * 364) : 6, fill: r.color,
        'fill-opacity': selected && selected !== r.user ? .18 : .7, stroke: r.color, 'stroke-width': selected === r.user ? 3 : 1,
        'stroke-dasharray': known(r.vram_mib) ? null : '2 2', onclick: () => pick(r.user),
      }, s('title', {}, `${r.user} · CPU ${r.cpu.toFixed(2)}% · 内存 ${gib(r.memory_mib)} · 显存 ${known(r.vram_mib) ? gib(r.vram_mib) : '未知'}`)))),
    h('p', { class: 'chart-caption', role: 'status' }, active ? `${active.user} · CPU ${active.cpu.toFixed(2)}% · 内存 ${gib(active.memory_mib)} · 显存 ${known(active.vram_mib) ? gib(active.vram_mib) : '未知'}` : '点面积表示显存；虚线点表示显存未知。选择图例查看数值。'),
    h('div', { class: 'resource-legend', role: 'group', 'aria-label': '选择用户' }, points.map(r => h('button', { type: 'button', class: 'resource-key', 'data-resource-focus': r.user, 'aria-pressed': String(r.user === selected), onclick: () => pick(r.user) },
      h('i', { style: { background: r.color }, 'aria-hidden': 'true' }), r.user))));
}

function hierarchy(sys, selected, pick) {
  const groups = gpuHierarchy(sys);
  const colors = new Map(groups.flatMap((g, i) => g.children.map(p => [p.id, colorAt(i)])));
  const lay = sunburstLayout(groups, { colorOf: id => colors.get(id) });
  if (!lay.total) return empty('检测到 GPU 进程的显存占用后，显示用户与进程的层级。');
  const current = lay.inner.find(g => g.id === selected);
  return h('div', { class: 'resource-hierarchy' },
    s('svg', { viewBox: '0 0 360 360', role: 'img', 'aria-label': '显存旭日图：内环是用户，外环是 GPU 进程。图例包含数值。' },
      [...lay.inner, ...lay.outer].map(p => s('path', { d: p.d, fill: p.color, opacity: selected && (p.group ?? p.id) !== selected ? .2 : 1, onclick: () => pick(p.group ?? p.id) }, s('title', {}, `${p.label} · ${gib(p.value)}`))),
      s('text', { x: 180, y: 170, 'text-anchor': 'middle', fill: 'var(--secondary)', 'font-size': 13 }, current ? short(current.label) : 'GPU 进程显存'),
      s('text', { x: 180, y: 198, 'text-anchor': 'middle', fill: 'var(--label)', 'font-size': 24, 'font-weight': 600 }, gib(current?.value ?? lay.total))),
    h('div', { class: 'resource-tree' }, groups.map((g, i) => h('div', { class: 'resource-branch' },
      h('button', { type: 'button', class: 'resource-key', 'data-resource-focus': g.id, 'aria-pressed': String(g.id === selected), onclick: () => pick(g.id) }, h('i', { style: { background: colorAt(i) }, 'aria-hidden': 'true' }), h('span', { class: 'grow' }, g.label), gib(g.children.reduce((t, p) => t + p.value, 0))),
      h('ul', {}, g.children.map(p => h('li', {}, h('span', {}, p.label), h('span', { class: 'tnum' }, gib(p.value)))))))));
}

export function resourceCharts(sys, state, donutFactory) {
  const modes = ['环形图', '柱状图', '散点图', '旭日图'];
  state.mode ??= modes[0];
  const root = h('section', { class: 'panel resource-panel' });
  const content = h('div', {});
  const buttons = modes.map(mode => h('button', { type: 'button', 'aria-pressed': String(mode === state.mode), 'data-chart-mode': mode, onclick: () => {
    state.mode = mode; buttons.forEach((b, i) => b.setAttribute('aria-pressed', String(modes[i] === mode))); draw();
  } }, mode));
  const pick = user => { state.user = state.user === user ? null : user; draw(); };
  const draw = () => {
    const active = document.activeElement;
    const focusKey = content.contains?.(active) ? active?.getAttribute?.('data-resource-focus') : null;
    const rows = resourceSeries(sys);
    put(clear(content), state.mode === '环形图' ? donutFactory() : state.mode === '柱状图' ? columns(rows) : state.mode === '散点图' ? scatter(rows, state.user, pick) : hierarchy(sys, state.user, pick));
    if (focusKey != null) {
      const replacement = [...content.querySelectorAll('[data-resource-focus]')].find(node => node.getAttribute('data-resource-focus') === focusKey);
      (replacement ?? buttons[modes.indexOf(state.mode)])?.focus({ preventScroll: true });
    }
  };
  put(root, h('div', { class: 'resource-heading' }, h('div', {}, h('h2', { class: 'panel-title' }, '资源分布'), h('p', { class: 'panel-sub' }, '按用户比较占用，或查看 GPU 进程层级。')),
    h('div', { class: 'segmented', role: 'group', 'aria-label': '图表类型' }, buttons)), content);
  root.update = (nextSystem, nextDonuts) => {
    sys = nextSystem;
    donutFactory = nextDonuts;
    draw();
  };
  draw(); return root;
}
