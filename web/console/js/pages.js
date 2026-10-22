/*! web/console/js/pages.js — 控制台各页面。
 *  每个页面是 async (ctx) => Element。ctx.demo 为真时只使用 demo.js 的示例数据。 */
import { h, put } from './dom.js';
import { fmtInt, fmtGiB } from './charts.js';
import {
  PATHS, loadOps, loadHost, loadUsers, loadIntegrations, loadDiscovery, loadTokens, loadSystem, loadData,
  safeJumpUrl, send,
} from './data.js';
import {
  overviewModel, servicesFromOps, modelsFromOps, hostFacts, engineLabel, modelColorMap, preflightChecks,
} from './viewmodel.js';
import {
  demoOverview, demoServices, demoEvaluation, DEMO_USERS,
} from './demo.js';
import { SNAP_SYSTEM } from './snapshot.js';
import {
  pageHead, emptyState, statusBadge, segmented, section, relTime,
  kpiBar, sunburstPanel, vramPanel, hourlyChart, hbarList, checkIcon, quietEmpty, metricCards, donutPanel, ringGauge,
} from './ui.js';

const panel = (cls, title, sub, ...body) => h('section', { class: `panel ${cls}` },
  h('div', { class: 'panel-head' }, h('div', {}, h('h2', { class: 'panel-title' }, title), sub ? h('p', { class: 'panel-sub' }, sub) : null)),
  ...body);

/* ============================== 概览 ============================== */
// 平台的核心是资源监控与推理服务；API 调用统计是次要信息，没配置、没调用都属正常。
const CALLS_EMPTY = { title: '暂无调用记录', body: '把推理服务发布到「网关」并用 API 密钥调用后，这里会显示调用统计。' };
const SERVICES_EMPTY = { title: '还没有登记推理服务', body: '在自己的 Linux 用户下启动推理引擎，再用平台账户登记，这里会显示它的运行状态与显存占用。' };

function servicesTable(list, { compact = false } = {}) {
  const cols = compact ? ['推理服务', '推理框架', '所有者', '运行用户', '显存', '状态'] : ['推理服务', '推理框架', '所有者', '运行用户', '访问范围', '显存', '健康', '状态'];
  const vramCol = cols.indexOf('显存');
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', {}, h('tr', {}, cols.map((t, i) => h('th', { scope: 'col', class: i === vramCol ? 'num' : null }, t)))),
    h('tbody', {}, list.map((x) => h('tr', {},
      h('td', {}, h('div', { style: { 'font-weight': 500 } }, x.kind === 'engine' ? h('a', { href: `#/services/${encodeURIComponent(x.id)}` }, x.label) : x.label),
        x.sharedToMe ? h('div', { class: 'muted', style: { 'font-size': '12px' } }, '共享给我')
          : x.port ? h('div', { class: 'muted tnum', style: { 'font-size': '12px' } }, `端口 ${x.port}`) : null),
      h('td', {}, engineLabel(x.engine)),
      h('td', {}, x.owner ?? '—', x.self ? h('span', { class: 'muted' }, '（我）') : null),
      h('td', { class: x.runAs ? 'tnum' : 'muted' }, x.runAs ?? '—'),
      compact ? null : h('td', {}, x.share),
      h('td', { class: 'num' }, x.vramMiB == null ? '—' : `${fmtGiB(x.vramMiB)} GB`),
      compact ? null : h('td', {}, statusBadge(x.health)),
      h('td', {}, statusBadge(x.runtime)))))));
}

function gpuFacts(hf, hostState) {
  if (!hf) return quietEmpty(hostState, { title: '暂无 GPU 读数', body: '接入 Prometheus 后，这里显示 GPU 利用率、温度与功耗。' });
  const row = (label, value) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, label), h('span', { class: 'tnum' }, value ?? '—'));
  const SERVICES = [['prometheus', 'Prometheus'], ['strata', 'Strata'], ['gateway', 'Gateway'], ['evalscope', 'EvalScope']];
  const svc = (v) => (v === true ? { tone: 'ok', text: '在线' } : v === false ? { tone: 'bad', text: '未响应' } : { tone: 'off', text: '未检测' });
  return h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
    h('div', { class: 'group' },
      row('利用率', hf.utilRatio == null ? null : `${Math.round(hf.utilRatio * 100)}%`),
      row('温度', hf.tempC == null ? null : `${Math.round(hf.tempC)} °C`),
      row('功耗', hf.powerW == null ? null : `${Math.round(hf.powerW)} W`)),
    h('div', { class: 'group' }, SERVICES.map(([key, name]) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, name), statusBadge(svc(hf.services?.[key]))))));
}

/** 网关概览 ⇒ 概览页的「API 调用」部分（调用者 → 模型的旭日图、各模型调用量、24 小时柱状图）。 */
function gatewayCalls(o, kpis) {
  const c = o.calls;
  kpis.requests = c.requests; kpis.requestsScope = o.view === 'all' ? '近 24 小时 · 全实验室' : '近 24 小时 · 我发起的';
  if (!c.requests) return { distribution: null, modelUsage: null, hourly: null };
  const colors = modelColorMap(c.by_model.map((m) => m.model));
  return {
    distribution: { innerLabel: '调用者', groups: c.by_caller.map((x) => ({ id: x.caller, label: x.name, children: c.by_caller_model.filter((p) => p.caller === x.caller).map((p) => ({ id: p.model, label: p.model, value: p.requests })) })) },
    modelUsage: c.by_model.map((m) => ({ id: m.model, label: m.model, value: m.requests })),
    modelColors: colors,
    hourly: { points: c.series.requests, scope: o.view },
  };
}

export async function overview(ctx) {
  let model;
  let states = {};
  if (ctx.demo) {
    model = demoOverview();
  } else {
    const [ops, host, gw] = await Promise.all([loadOps(), loadHost(), loadData(PATHS.gatewayOverview)]);
    states = { ops: ops.state, host: host.state, calls: gw.state };
    model = overviewModel({ ops: ops.value, host: host.value });
    if (gw.state === 'ready') Object.assign(model, gatewayCalls(gw.value, model.kpis));
  }
  const now = new Date();
  const stamp = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const head = pageHead('概览', `RTX A6000 工作站 · 数据截至 ${stamp}`,
    h('a', { class: 'btn primary', href: '#/services/register' }, '登记推理服务'));

  const vramBody = model.vram
    ? vramPanel(model.vram)
    : quietEmpty(states.host ?? 'not_configured', { title: '暂无显存读数', body: '接入 Prometheus 后，这里会按推理服务显示显存分配。' });

  const services = model.services;
  const servicesBody = services
    ? (services.length ? servicesTable(services, { compact: true }) : quietEmpty('ready', SERVICES_EMPTY))
    : quietEmpty(states.ops ?? 'not_configured', { title: '推理服务列表暂不可用', body: '当前会话还读不到注册中心的服务列表。' });

  const dist = model.distribution
    ? sunburstPanel(model.distribution, model.modelColors)
    : quietEmpty(states.calls ?? 'not_configured', CALLS_EMPTY);
  const usage = model.modelUsage
    ? h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
      ...hbarList(model.modelUsage, model.modelColors),
      model.hourly ? h('div', { style: { 'border-top': '1px solid var(--hairline)', 'padding-top': '14px', 'margin-top': '4px' } }, hourlyChart(model.hourly.points)) : null)
    : quietEmpty(states.calls ?? 'not_configured', CALLS_EMPTY);
  const hasCalls = Boolean(model.distribution || model.modelUsage);

  return h('div', { class: 'page' },
    head,
    kpiBar(model.kpis),
    h('div', { class: 'row' },
      panel('grow', '显存分配', model.vram ? `GPU 显存 ${fmtGiB(model.vram.totalMiB, 0)} GB · 每个方块代表 0.25 GB` : null, vramBody),
      panel('side', 'GPU 与服务', null, gpuFacts(model.host, states.host ?? 'not_configured'))),
    panel('', '推理服务与模型', services?.length ? `${services.length} 个已登记 · 点击「推理服务」查看详情与访问范围` : null, servicesBody),
    h('section', { style: { display: 'flex', 'flex-direction': 'column', gap: '16px' }, 'aria-labelledby': 'calls-title' },
      h('div', { class: 'page-head' },
        h('h2', { id: 'calls-title', style: { margin: 0, 'font-size': '19px', 'font-weight': 600 } }, 'API 调用'),
        h('a', { class: 'linkbtn', href: '#/gateway' }, '打开网关')),
      hasCalls
        ? h('div', { class: 'row' },
          panel('grow', '调用分布', `内环为${model.distribution?.innerLabel ?? 'API 密钥'}，外环为其调用的模型`, dist),
          panel('side', '各服务调用量', null, usage))
        : h('div', { class: 'group' }, quietEmpty(states.calls ?? 'not_configured', CALLS_EMPTY))));
}

/* ============================== 推理服务 ============================== */
export async function services(ctx) {
  let list; let state = 'ready';
  if (ctx.demo) list = demoServices();
  else {
    const r = await loadOps();
    state = r.state;
    list = r.state === 'ready' ? servicesFromOps(r.value) : null;
  }
  const body = list
    ? (list.length ? servicesTable(list) : h('div', { class: 'group' }, quietEmpty('ready', SERVICES_EMPTY)))
    : emptyState(state);
  return h('div', { class: 'page' },
    pageHead('推理服务', '实验室内已登记的推理引擎。服务归登记它的平台账户所有，「运行用户」是登记时检测到的监听进程所属 Linux 用户。',
      h('a', { class: 'btn primary', href: '#/services/register' }, '手动登记')),
    body,
    discoveryPanel(ctx, 'engines'));
}

/* ---------- 本机发现：推理服务与模型共用 ---------- */
const fmtBytes = (n) => (n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 1024 ** 2))} MB`);

const scannedKinds = new Set();

function discoveryPanel(ctx, kind) {
  const holder = h('div', {});
  const st = { data: null, state: null, busy: false, msg: null, done: new Set() };
  const title = kind === 'engines' ? '本机发现 · 推理服务' : '本机发现 · 模型目录';
  const intro = kind === 'engines'
    ? '列出本机回环地址上正在监听、端口在允许列表内的服务，并读取它的模型清单与运行用户。只读；点「登记」才会加入资源池。'
    : '在管理员授权的模型目录里查找 Hugging Face 模型目录与 GGUF 文件。只读取 config.json 与文件名、大小，不打开权重。';

  async function scan() {
    scannedKinds.add(kind);
    st.busy = true; st.msg = null; draw();
    if (ctx.demo) {
      st.data = { configured: true, problems: [], engines: [
        { port: 8001, target_url: 'http://127.0.0.1:8001', run_as_user: 'li', framework: 'vllm', models: ['Qwen2.5-14B-Instruct'], openai_compatible: true, registered: false, registered_by_me: false, suggested_name: 'Qwen2.5-14B-Instruct' },
        { port: 8000, target_url: 'http://127.0.0.1:8000', run_as_user: 'zhang', framework: 'strata', models: ['Qwen2.5-32B-Instruct'], openai_compatible: true, registered: true, registered_by_me: true, suggested_name: 'Qwen2.5-32B-Instruct' },
      ], models: [
        { path: '/data/models/Qwen2.5-14B-Instruct', format: 'hf', size_bytes: 29.5 * 1024 ** 3, architecture: 'Qwen2ForCausalLM', registered: false, suggested_name: 'Qwen2.5-14B-Instruct' },
        { path: '/data/models/gguf/llama-3-8b.Q4_K_M.gguf', format: 'gguf', size_bytes: 4.9 * 1024 ** 3, architecture: null, registered: true, suggested_name: 'llama-3-8b.Q4_K_M' },
      ] };
      st.state = 'ready';
    } else {
      const r = await loadDiscovery();
      st.state = r.state; st.data = r.state === 'ready' ? r.value : null;
    }
    st.busy = false; draw();
  }

  async function register(item) {
    const key = item.port ?? item.path;
    if (ctx.demo) { st.done.add(key); draw(); return; }
    const body = kind === 'engines'
      ? { name: item.suggested_name, target_url: item.target_url, protocol: 'openai_compatible', ...(item.framework ? { tags: [`framework:${item.framework}`] } : {}) }
      : { name: item.suggested_name, weight_path: item.path, format: item.format };
    const r = await send(kind === 'engines' ? PATHS.registryEngines : PATHS.registryModels, body);
    if (!r.ok) {
      st.msg = r.status === 409 ? '已被登记（地址或名称冲突）。' : (r.message ?? `登记失败（${r.reason}）`);
      draw(); return;
    }
    st.done.add(key); st.msg = null;
    // 登记后整页重绘：上方的已登记列表与这里的候选状态一起刷新。
    if (ctx.rerender) { ctx.rerender(); return; }
    await scan();
  }

  function rows() {
    const items = st.data?.[kind] ?? [];
    if (!items.length) return quietEmpty('ready', kind === 'engines'
      ? { title: '没有发现正在监听的推理服务', body: `允许的端口：${(st.data?.allowed_ports ?? []).join('、') || '—'}。启动服务后再扫描一次。` }
      : { title: '没有发现模型', body: (st.data?.model_roots ?? []).length ? `已查找：${st.data.model_roots.join('、')}` : '管理员尚未配置可查找的模型目录。' });
    const head = kind === 'engines' ? ['端口', '运行用户', '推理框架', '模型', ''] : ['路径', '格式', '架构', '大小', ''];
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, head.map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, items.map((item) => {
        const key = item.port ?? item.path;
        const registered = item.registered || st.done.has(key);
        const action = registered
          ? h('span', { class: 'muted' }, item.registered_by_me || st.done.has(key) ? '已登记（我）' : '已被登记')
          : (kind === 'engines' && !item.openai_compatible)
            ? h('span', { class: 'muted' }, '不是 OpenAI 兼容接口')
            : h('button', { type: 'button', class: 'btn', onclick: () => register(item) }, '登记');
        const cells = kind === 'engines'
          ? [h('td', { class: 'tnum' }, String(item.port)), h('td', { class: 'tnum' }, item.run_as_user ?? (item.run_as_uid != null ? `uid ${item.run_as_uid}` : '—')),
            h('td', {}, engineLabel(item.framework)), h('td', {}, (item.models ?? []).join('、') || '—')]
          : [h('td', { class: 'mono', style: { 'font-size': '12px', 'word-break': 'break-all' } }, item.path), h('td', {}, item.format === 'hf' ? 'Hugging Face' : 'GGUF'),
            h('td', {}, item.architecture ?? '—'), h('td', { class: 'tnum' }, fmtBytes(item.size_bytes ?? 0))];
        return h('tr', {}, ...cells, h('td', { style: { 'text-align': 'right' } }, action));
      }))));
  }

  function draw() {
    while (holder.firstChild) holder.firstChild.remove();
    let content;
    if (st.busy) content = h('p', { class: 'muted' }, '正在扫描…');
    else if (!st.data && st.state) content = emptyState(st.state);
    else if (!st.data) content = h('p', { class: 'muted', style: { margin: 0 } }, intro);
    else if (!st.data.configured) content = quietEmpty('ready', { title: '本部署未开启本机发现', body: '仍可手动登记，或请管理员在启动时开启发现。' });
    else content = rows();
    put(holder, panel('', title, st.data ? intro : null,
      h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '12px' } },
        h('div', {}, h('button', { type: 'button', class: 'btn', disabled: st.busy, onclick: scan }, st.data ? '重新扫描' : '扫描本机')),
        st.msg ? h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left', margin: 0 } }, st.msg) : null,
        content,
        (st.data?.problems ?? []).length ? h('p', { class: 'muted', style: { margin: 0, 'font-size': '12px' } }, `提示：${st.data.problems.join('；')}`) : null)));
  }
  draw();
  // 这次会话里扫过一次，重绘（例如登记之后）就直接重新扫，不让结果凭空消失。
  if (scannedKinds.has(kind)) scan();
  return holder;
}

/* ============================== 模型库 ============================== */
export async function models(ctx) {
  let list; let state = 'ready';
  if (ctx.demo) list = [
    { id: 'm1', name: 'Qwen2.5-32B-Instruct', format: 'hf', params: '32B', owner: '张伟', self: true, weightPath: '/data/models/Qwen2.5-32B-Instruct', description: '对话' },
    { id: 'm2', name: 'bge-m3', format: 'hf', params: '568M', owner: '陈静', self: false, weightPath: null, description: '向量化' },
  ];
  else { const r = await loadOps(); state = r.state; list = r.state === 'ready' ? modelsFromOps(r.value) : null; }
  const root = h('div', { class: 'page' });
  const st = { open: false, msg: null, form: { name: '', weight_path: '', format: 'hf', parameter_count: '', description: '' } };

  async function submit() {
    const f = st.form;
    if (!f.name) { st.msg = '请填写模型名称。'; draw(); return; }
    if (ctx.demo) { list.push({ id: `m${list.length + 1}`, name: f.name, format: f.format, params: f.parameter_count || null, owner: '我', self: true, weightPath: f.weight_path || null, description: f.description || null }); st.open = false; draw(); return; }
    const body = Object.fromEntries(Object.entries(f).filter(([, v]) => v));
    const r = await send(PATHS.registryModels, body);
    if (!r.ok) { st.msg = r.status === 409 ? '你已有同名模型。' : (r.message ?? `登记失败（${r.reason}）`); draw(); return; }
    const fresh = await loadOps();
    if (fresh.state === 'ready') list = modelsFromOps(fresh.value);
    st.open = false; st.msg = null; st.form = { name: '', weight_path: '', format: 'hf', parameter_count: '', description: '' };
    draw();
  }

  function form() {
    const inp = (key, ph, width = '260px') => h('input', { value: st.form[key], placeholder: ph, style: { width, 'min-width': 0 }, oninput: (e) => { st.form[key] = e.target.value.trim(); } });
    return section('登记模型', h('div', { class: 'group' },
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '名称'), inp('name', 'qwen2.5-7b-sft-v3')),
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '权重位置'), inp('weight_path', '/data/models/qwen2.5-7b-sft-v3', '100%')),
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '格式'),
        h('select', { onchange: (e) => { st.form.format = e.target.value; } }, [['hf', 'Hugging Face'], ['gguf', 'GGUF'], ['other', '其他']].map(([v, l]) => h('option', { value: v, selected: st.form.format === v }, l)))),
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '参数量'), inp('parameter_count', '7B', '120px')),
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '说明'), inp('description', '对话 / 向量化 / 重排…', '100%')),
      h('div', { class: 'cell', style: { gap: '10px', 'justify-content': 'flex-end' } },
        h('button', { type: 'button', class: 'btn', onclick: () => { st.open = false; st.msg = null; draw(); } }, '取消'),
        h('button', { type: 'button', class: 'btn primary', onclick: submit }, '登记'))),
    '权重位置只是登记引用：平台不移动、不复制、不打开权重文件，其他成员也看不到这个路径。');
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    put(root, pageHead('模型库', '实验室登记的模型。模型和推理服务都是资源池里的资源：模型说明「是什么」，推理服务负责「运行它」。',
      h('button', { type: 'button', class: 'btn primary', onclick: () => { st.open = true; draw(); } }, '登记模型')));
    if (st.msg) put(root, h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));
    if (st.open) put(root, form());
    put(root, list
      ? (list.length
        ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, ['模型', '格式', '参数量', '所有者', '权重位置'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, list.map((m) => h('tr', {},
            h('td', {}, h('div', { style: { 'font-weight': 500 } }, m.name), m.description ? h('div', { class: 'muted', style: { 'font-size': '12px' } }, m.description) : null),
            h('td', {}, m.format === 'hf' ? 'Hugging Face' : m.format === 'gguf' ? 'GGUF' : (m.format ?? '—')),
            h('td', { class: 'tnum' }, m.params ?? '—'),
            h('td', {}, m.owner ?? '—', m.self ? h('span', { class: 'muted' }, '（我）') : null),
            h('td', { class: m.weightPath ? 'mono' : 'muted', style: { 'font-size': '12px', 'word-break': 'break-all' } }, m.weightPath ?? (m.self ? '—' : '仅所有者可见')))))))
        : h('div', { class: 'group' }, quietEmpty('ready', { title: '还没有登记模型', body: '点击「登记模型」手动添加，或用下方的本机发现从授权目录里挑选。' })))
      : emptyState(state));
    put(root, discoveryPanel(ctx, 'models'));
  }
  draw();
  return root;
}

/* ============================== 注册模型服务 ============================== */
const FRAMEWORKS = [['vllm', 'vLLM'], ['sglang', 'SGLang'], ['tei', 'Text Embeddings Inference'], ['ollama', 'Ollama'], ['llama.cpp', 'llama.cpp'], ['strata', 'Strata']];

export async function register(ctx) {
  const st = { step: 1, framework: 'vllm', port: '', name: '', scope: 'private', checks: null, error: null, cli: null, busy: false };
  const root = h('div', { class: 'page narrow' });

  const field = (label, input) => h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, label), input);

  const target = () => `http://127.0.0.1:${Number(st.port)}`;

  async function verify() {
    st.busy = true; st.error = null; st.cli = null; draw();
    const port = Number(st.port);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) { st.busy = false; st.error = '请输入 1024–65535 之间的端口号。'; draw(); return; }
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(st.name)) { st.busy = false; st.error = '服务名称只能包含字母、数字、点、下划线和连字符。'; draw(); return; }
    if (ctx.demo) {
      st.checks = preflightChecks({ all_ok: true, checks: {
        port_reachable: { ok: true, detail: 'connected' }, ownership: { ok: true, detail: 'verified' },
        openai_compat: { ok: true, detail: 'model_list_verified' }, name_unique: { ok: true, detail: 'available' } } });
      st.step = 2; st.busy = false; draw(); return;
    }
    // 只读预检：不登记、不写审计。
    const r = await send(PATHS.preflight, { name: st.name, target_url: target() });
    st.busy = false;
    if (!r.ok) { st.error = failText(r, '校验'); draw(); return; }
    st.checks = preflightChecks(r.data?.data ?? r.data);
    if (!st.checks) { st.error = '校验结果格式无法识别。'; draw(); return; }
    st.step = 2; draw();
  }

  async function publish() {
    if (ctx.demo) { st.step = 3; draw(); return; }
    st.busy = true; st.error = null; st.cli = null; draw();
    const r = await send(PATHS.registryEngines, {
      name: st.name,
      protocol: 'openai_compatible',
      target_url: target(),
      tags: [`framework:${st.framework}`, `scope:${st.scope}`],
    });
    st.busy = false;
    if (!r.ok) { st.error = failText(r, '发布'); draw(); return; }
    st.step = 3; draw();
  }

  function failText(r, verb) {
    // Registry 目前只接受 Portal 签发的 Bearer 令牌，网页会话尚未桥接；不要误报为会话失效。
    if (r.reason === 'registry.unauthenticated') {
      st.cli = `llmlab engines register --name ${st.name} --target-url ${target()} --tag framework:${st.framework}`;
      return '网页端注册尚未接入：注册接口目前只接受 Portal 签发的访问令牌。请先使用命令行注册。';
    }
    if (r.state === 'not_configured') return '本部署尚未启用推理服务登记。';
    if (r.state === 'signed_out') return '会话已失效，请重新登录。';
    if (r.status === 409) return '该地址或服务名称已被登记。';
    if (r.state === 'denied') return '当前账户没有登记推理服务的权限。';
    return r.message ?? `${verb}失败（${r.reason ?? r.status ?? '未知原因'}）`;
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    const labels = ['服务配置', '登记检查', '发布'];
    put(root, 
      h('header', {},
        h('a', { class: 'crumb', href: '#/services' }, '推理服务'),
        h('h1', { class: 'page-title', style: { 'margin-top': '6px' } }, '登记推理服务'),
        h('p', { class: 'page-sub' }, '先在你使用的 Linux 用户下启动推理服务，再在这里登记。系统会检测监听该端口的进程由哪个 Linux 用户运行并一并记录；同一个地址只能被一个账户登记。')),
      h('ol', { class: 'steps', 'aria-label': '注册步骤' }, labels.map((l, i) => {
        const n = i + 1;
        return h('li', { class: n < st.step ? 'done' : null, 'aria-current': n === st.step ? 'step' : null },
          h('span', { class: 'badge tnum', 'aria-hidden': 'true' }, n < st.step ? '✓' : String(n)), l);
      })));

    if (st.step === 1) {
      const sel = h('select', { onchange: (e) => { st.framework = e.target.value; } }, FRAMEWORKS.map(([v, l]) => h('option', { value: v, selected: v === st.framework }, l)));
      put(root, 
        section('服务配置', h('div', { class: 'group' },
          field('推理框架', sel),
          field('监听端口', h('input', { class: 'tnum', inputmode: 'numeric', placeholder: '8000', value: st.port, oninput: (e) => { st.port = e.target.value.trim(); } })),
          field('服务名称', h('input', { placeholder: 'qwen2.5-7b-sft-v3', value: st.name, oninput: (e) => { st.name = e.target.value.trim(); }, style: { width: '260px' } }))),
          '服务仅需监听 127.0.0.1，端口须在管理员允许的列表内（默认 8000、8001、8081、9000、9001）。服务名称即调用时的 model 参数，不能与你已有的服务重名。'),
        h('fieldset', { style: { border: 0, padding: 0, margin: 0 } },
          h('legend', { class: 'section-title', style: { 'margin-bottom': '8px' } }, '访问范围'),
          h('div', { class: 'group' }, [['private', '仅本人'], ['all', '全部成员'], ['selected', '指定成员']].map(([v, l]) =>
            h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, l),
              h('input', { type: 'radio', name: 'scope', value: v, checked: st.scope === v, onchange: () => { st.scope = v; }, style: { width: '18px', height: '18px' } }))))));
    } else if (st.step === 2) {
      put(root, section('校验结果', h('div', { class: 'group' }, st.checks.list.map((c) => h('div', { class: 'check' }, checkIcon(c.ok),
        h('div', {}, h('div', { style: { 'font-weight': 500 } }, c.title), h('div', { class: 'muted', style: { 'font-size': '13px' } }, c.detail)))))));
    } else {
      put(root, 
        h('div', { style: { display: 'flex', 'align-items': 'center', gap: '12px' } }, checkIcon(true, 28), h('h2', { style: { margin: 0, 'font-size': '22px', 'font-weight': 600 } }, '服务已发布')),
        h('p', { class: 'muted', style: { margin: 0 } }, `${st.name} 当前访问范围为「${{ private: '仅本人', all: '全部成员', selected: '指定成员' }[st.scope]}」，可在「推理服务」中查看运行状态。`),
        section('调用示例', h('div', { class: 'code' },
          `curl ${location.origin}/v1/chat/completions \\\n  -H "Authorization: Bearer $LLMLAB_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${st.name}", "messages": [{"role": "user", "content": "Hello"}]}'`)));
    }

    if (st.error) put(root, h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.error));
    if (st.error && st.cli) put(root, h('div', { class: 'code' }, st.cli));
    const blocked = !st.checks || st.checks.blocked;
    put(root, h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn', onclick: () => { if (st.step === 1) ctx.navigate('#/services'); else { st.step -= 1; st.error = null; draw(); } } }, st.step === 1 ? '取消' : '上一步'),
      st.step === 1 ? h('button', { type: 'button', class: 'btn primary', disabled: st.busy, onclick: verify }, st.busy ? '正在校验…' : '开始校验')
        : st.step === 2 ? h('button', { type: 'button', class: 'btn primary', disabled: blocked || st.busy, onclick: publish }, st.busy ? '正在发布…' : '发布')
          : h('a', { class: 'btn primary', href: '#/services' }, '完成')),
      h('p', { class: 'muted', style: { margin: 0, 'padding-top': '20px', 'border-top': '1px solid var(--hairline)', 'font-size': '13px' } },
        '管理员可以在「用户与权限」中为某个账户指定 Linux 用户，指定后该账户只能登记由这个 Linux 用户运行的服务。'));
  }
  draw();
  return root;
}

/* ============================== 我的账户 ============================== */
export async function account(ctx) {
  const me = ctx.me ?? {};
  const row = (label, value) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, label), h('span', { class: value ? null : 'muted' }, value ?? '—'));
  return h('div', { class: 'page narrow' },
    pageHead('我的账户', '平台账户、命令行与 Agent 令牌。调用模型用的 API 密钥在「网关 → API 密钥」。'),
    section('账户', h('div', { class: 'group' },
      row('用户名', me.username ?? me.subject),
      row('显示名称', me.display_name),
      row('角色', ctx.isAdmin() ? '管理员' : '成员'),
      row('常用 Linux 用户', me.linux_user)),
    '账户独立于 Linux 用户；登记推理服务时平台会记录它实际由哪个 Linux 用户运行。'),
    await agentTokens(ctx),
    section('接入', h('div', { class: 'group' },
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '模型调用（OpenAI 兼容）'), h('span', { class: 'mono' }, `${location.origin}/v1`)),
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '命令行工具'), h('span', { class: 'mono' }, 'llmlab --help')),
      h('a', { class: 'cell action', href: '#/gateway/keys' }, '管理 API 密钥')),
    '用上面创建的令牌配置 llmlab 后，可以执行 llmlab discover、engines、models、endpoints 等命令；加 --json 输出给 Agent 解析。'));
}

/* ---------- CLI / Agent 令牌 ---------- */
const TTL_CHOICES = [['7 天', 7], ['30 天', 30], ['90 天', 90], ['365 天', 365]];
const epochIso = (sec) => (typeof sec === 'number' ? new Date(sec * 1000).toISOString() : null);

async function agentTokens(ctx) {
  const holder = h('div', {});
  let list = []; let state = 'ready';
  if (ctx.demo) list = [{ token_id: 't1', label: 'claude-code', scopes: ['registry:read', 'registry:write', 'registry:endpoint:invoke', 'registry:endpoint:share'], created_at: Date.now() / 1000 - 86400, expires_at: Date.now() / 1000 + 29 * 86400, last_used_at: Date.now() / 1000 - 600, revoked: false }];
  else { const r = await loadTokens(); state = r.state; list = r.state === 'ready' ? r.value : null; }
  const st = { label: 'my-agent', days: 30, issued: null, msg: null };

  async function refresh() { if (ctx.demo) return; const r = await loadTokens(); if (r.state === 'ready') list = r.value; }
  async function mint() {
    if (ctx.demo) { st.issued = { token: 'llt_demo_xxxxxxxxxxxxxxxxxxxx', label: st.label }; draw(); return; }
    const r = await send(PATHS.tokens, { label: st.label || null, ttl_seconds: st.days * 86400 });
    if (!r.ok) { st.msg = r.message ?? `创建失败（${r.reason}）`; draw(); return; }
    st.issued = r.data; st.msg = null; await refresh(); draw();
  }
  async function revoke(id) {
    if (!globalThis.confirm?.('撤销后，使用这个令牌的命令行和 Agent 会立即失去访问权限。确定撤销？')) return;
    if (!ctx.demo) { const r = await send(PATHS.tokenRevoke, { token_id: id }); if (!r.ok) { st.msg = r.message ?? `撤销失败（${r.reason}）`; draw(); return; } await refresh(); }
    else list = list.map((t) => (t.token_id === id ? { ...t, revoked: true } : t));
    draw();
  }

  function draw() {
    while (holder.firstChild) holder.firstChild.remove();
    if (!list) { put(holder, section('命令行 / Agent 令牌', h('div', { class: 'group' }, quietEmpty(state, { title: '令牌服务未开启', body: '本部署尚未开启命令行令牌。' })))); return; }
    const live = list.filter((t) => !t.revoked);
    put(holder, section('命令行 / Agent 令牌', h('div', { class: 'group' },
      live.length ? live.map((t) => h('div', { class: 'cell' },
        h('div', { class: 'grow' }, h('div', { class: 'title' }, t.label ?? '未命名'),
          h('div', { class: 'sub' }, `${relTime(epochIso(t.last_used_at)) === '—' ? '尚未使用' : `最近使用 ${relTime(epochIso(t.last_used_at))}`} · ${new Date(t.expires_at * 1000).toLocaleDateString()} 到期`)),
        h('button', { type: 'button', class: 'linkbtn danger', onclick: () => revoke(t.token_id) }, '撤销')))
        : h('div', { class: 'cell' }, h('div', { class: 'grow sub' }, '还没有令牌。')),
      h('div', { class: 'cell', style: { gap: '10px', 'flex-wrap': 'wrap' } },
        h('input', { value: st.label, placeholder: '用途，例如 claude-code', style: { flex: '1 1 160px', 'min-width': 0 }, oninput: (e) => { st.label = e.target.value.trim(); } }),
        h('select', { onchange: (e) => { st.days = Number(e.target.value); } }, TTL_CHOICES.map(([l, d]) => h('option', { value: d, selected: d === st.days }, l))),
        h('button', { type: 'button', class: 'btn primary', onclick: mint }, '创建令牌')),
      st.issued ? h('div', { class: 'cell', style: { 'flex-direction': 'column', 'align-items': 'stretch', gap: '8px' } },
        h('div', { class: 'muted', style: { 'font-size': '13px' } }, '令牌只显示这一次。交给你的 Agent 后，它就能像你一样在命令行里发现、登记和使用资源：'),
        h('div', { class: 'code' }, `export LLMLAB_API_BASE=${location.origin}\nexport LLMLAB_TOKEN=${st.issued.token}\nllmlab whoami\nllmlab discover\nllmlab discover --register 8000`)) : null),
    '令牌拥有与你的账户相同的资源权限（管理员账户的令牌也能查看全实验室），账户停用或令牌撤销后立即失效。'));
    if (st.msg) put(holder, h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));
  }
  draw();
  return holder;
}

/* ============================== 资源监控 ============================== */
const fmtRate = (bps) => (bps == null ? '—' : bps >= 1024 ** 2 ? `${(bps / 1024 ** 2).toFixed(1)} MB/s` : bps >= 1024 ? `${(bps / 1024).toFixed(1)} KB/s` : `${Math.round(bps)} B/s`);
const fmtDisk = (b) => (b >= 1024 ** 4 ? `${(b / 1024 ** 4).toFixed(2)} TB` : `${(b / 1024 ** 3).toFixed(b >= 100 * 1024 ** 3 ? 0 : 1)} GB`);

function meterRow(label, ratio, right, tint) {
  const tone = ratio >= 0.9 ? 'var(--red)' : ratio >= 0.75 ? 'var(--orange)' : tint;
  return h('div', { class: 'hbar' },
    h('div', { class: 'hbar-top' }, h('span', {}, label), h('span', { class: 'muted tnum' }, right)),
    h('div', { class: 'hbar-track', role: 'img', 'aria-label': `${label} ${Math.round(ratio * 100)}%` }, h('div', { style: { width: `${(Math.min(1, ratio) * 100).toFixed(1)}%`, background: tone } })));
}

/** 主机读数（/api/infra/system）：CPU、内存、磁盘、网络、温度与按 Linux 用户的占用。 */
const USER_COLORS = ['#007AFF', '#34C759', '#FF9500', '#AF52DE', '#FF2D55', '#5AC8FA', '#FFCC00', '#5856D6'];

/** 「我的占用」：当前账户对应的 Linux 用户占了多少内存 / CPU / 显存，每个人都能看到自己的那一份。 */
function myShare(sys, me, gpuTotalMiB) {
  const row = (sys.per_user ?? []).find((u) => u.user === me);
  if (!row) return null;
  const memTotal = sys.memory?.total_mib;
  const cores = sys.cpu?.cores;
  const gauge = (label, ratio, value, tint) => h('div', { class: 'gauge' }, ringGauge(ratio, { tint }),
    h('div', {}, h('div', { class: 'gauge-label' }, label), h('div', { class: 'gauge-value' }, value)));
  return panel('', `我的占用 · ${me}`, '以你的 Linux 用户运行的全部进程，占整机的比例',
    h('div', { class: 'mine' },
      gauge('内存', memTotal ? row.memory_mib / memTotal : null, `${fmtGiB(row.memory_mib)} GB`, '#30B0C7'),
      gauge('CPU（整机）', row.cpu_ratio == null ? null : row.cpu_ratio / (cores || 1), row.cpu_ratio == null ? '—' : `${(row.cpu_ratio * 100).toFixed(1)}% 核`, '#34C759'),
      gauge('显存', row.vram_mib != null && gpuTotalMiB ? row.vram_mib / gpuTotalMiB : (gpuTotalMiB ? 0 : null), row.vram_mib == null ? '0 GB' : `${fmtGiB(row.vram_mib)} GB`, '#5856D6'),
      h('div', { class: 'gauge' }, h('div', { class: 'gauge-value', style: { 'font-size': '30px', 'min-width': '56px', 'text-align': 'center' } }, fmtInt(row.processes)),
        h('div', {}, h('div', { class: 'gauge-label' }, '进程数'), h('div', { class: 'muted', style: { 'font-size': '12.5px' } }, `${(sys.gpu_processes ?? []).filter((g) => g.user === me).length} 个在用 GPU`)))));
}

/** 按用户的环形图：内存 / CPU / 显存，各自带「我」高亮；系统账户合并成一块。 */
function userDonuts(sys, me, gpuTotalMiB) {
  const humans = (sys.per_user ?? []).filter((u) => u.account === 'human');
  const system = (sys.per_user ?? []).filter((u) => u.account !== 'human');
  const color = new Map(humans.map((u, i) => [u.user, USER_COLORS[i % USER_COLORS.length]]));
  const items = (key) => [
    ...humans.map((u) => ({ id: u.user, label: u.user, value: u[key] ?? 0, color: color.get(u.user), me: u.user === me })),
    ...(system.length ? [{ id: '__system', label: `系统账户（${system.length}）`, value: system.reduce((t, u) => t + (u[key] ?? 0), 0), color: '#8E8E93' }] : []),
  ];
  const memUsed = (sys.per_user ?? []).reduce((t, u) => t + (u.memory_mib ?? 0), 0);
  const vramUsed = humans.reduce((t, u) => t + (u.vram_mib ?? 0), 0);
  const gib = (v) => `${fmtGiB(v)} GB`;
  return panel('', '按用户看资源', '点扇区或图例聚焦；「我」默认高亮。内存只统计进程常驻内存，缓存与内核占用计入「其他 / 空闲」。',
    h('div', { class: 'donut-grid' },
      h('div', {}, h('div', { class: 'section-title', style: { 'margin-bottom': '8px' } }, '内存'),
        donutPanel(items('memory_mib'), { title: '进程内存', fmt: gib, rest: sys.memory ? { label: '其他 / 空闲', value: Math.max(0, sys.memory.total_mib - memUsed) } : null })),
      h('div', {}, h('div', { class: 'section-title', style: { 'margin-bottom': '8px' } }, 'CPU'),
        // cpu_ratio 以「核」计（1.0 = 跑满一个线程）；这里换成占整机的百分比。
        donutPanel(items('cpu_ratio').map((i) => ({ ...i, value: ((i.value ?? 0) / (sys.cpu?.cores || 1)) * 100 })), { title: 'CPU（整机）', fmt: (v) => `${v.toFixed(v < 1 ? 2 : 1)}%`, rest: { label: '空闲', value: Math.max(0, 100 - (sys.per_user ?? []).reduce((t, u) => t + ((u.cpu_ratio ?? 0) / (sys.cpu?.cores || 1)) * 100, 0)) } })),
      gpuTotalMiB ? h('div', {}, h('div', { class: 'section-title', style: { 'margin-bottom': '8px' } }, '显存'),
        donutPanel(items('vram_mib').filter((i) => i.id !== '__system'), { title: 'GPU 显存', fmt: gib, rest: { label: '空闲', value: Math.max(0, gpuTotalMiB - vramUsed) } })) : null));
}

function hostSystem(sys, me) {
  const humans = (sys.per_user ?? []).filter((u) => u.account === 'human');
  const folded = (sys.per_user ?? []).filter((u) => u.account !== 'human');
  const foldedMem = folded.reduce((t, u) => t + (u.memory_mib ?? 0), 0);
  const maxMem = Math.max(...humans.map((u) => u.memory_mib ?? 0), 1);
  const th = sys.thermal ?? {};
  return [
    h('div', { class: 'row' },
      panel('grow', '磁盘', null, h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
        (sys.disk?.usage ?? []).filter((d) => d.total_bytes > 4 * 1024 ** 3).map((d) => meterRow(d.mount, d.used_ratio, `${fmtDisk(d.used_bytes)} / ${fmtDisk(d.total_bytes)}`, '#30B0C7')),
        h('div', { class: 'muted tnum', style: { 'font-size': '12.5px' } }, `读 ${fmtRate(sys.disk?.read_bytes_per_s)} · 写 ${fmtRate(sys.disk?.write_bytes_per_s)}`))),
      panel('side', '网络与温度', null, h('div', { class: 'group' },
        h('div', { class: 'cell' }, h('span', { class: 'grow' }, '下行 / 上行'), h('span', { class: 'tnum' }, `${fmtRate(sys.network?.rx_bytes_per_s)} / ${fmtRate(sys.network?.tx_bytes_per_s)}`)),
        h('div', { class: 'cell' }, h('span', { class: 'grow' }, 'TCP 连接'), h('span', { class: 'tnum' }, sys.connections ? `${sys.connections.established} 已建立 · ${sys.connections.listening} 监听` : '—')),
        h('div', { class: 'cell' }, h('span', { class: 'grow' }, 'CPU / NVMe 温度'), h('span', { class: 'tnum' }, `${th.cpu_c ?? '—'} °C / ${th.nvme_c ?? '—'} °C`)),
        h('div', { class: 'cell' }, h('span', { class: 'grow' }, '风扇'), h('span', { class: 'tnum' }, th.fans_rpm?.length ? th.fans_rpm.map((r) => `${fmtInt(r)}`).join(' · ') + ' rpm' : '—'))))),
    panel('', '按 Linux 用户', `进程、内存、CPU 与显存按运行用户统计${folded.length ? ` · ${folded.length} 个系统账户已折叠（共 ${fmtGiB(foldedMem)} GB 内存）` : ''}`,
      humans.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['用户', '进程', '内存', '', 'CPU', '显存'].map((t, i) => h('th', { scope: 'col', class: i && i !== 3 ? 'num' : null }, t)))),
        h('tbody', {}, humans.map((u) => h('tr', { class: u.user === me ? 'me' : null },
          h('td', { style: { 'font-weight': 500 } }, u.user, u.user === me ? h('span', { class: 'me-tag' }, '我') : null),
          h('td', { class: 'num' }, fmtInt(u.processes)),
          h('td', { class: 'num' }, `${fmtGiB(u.memory_mib)} GB`),
          h('td', { style: { width: '28%' } }, h('div', { class: 'hbar-track' }, h('div', { style: { width: `${((u.memory_mib ?? 0) / maxMem * 100).toFixed(1)}%`, background: '#30B0C7' } }))),
          h('td', { class: 'num' }, u.cpu_ratio == null ? '—' : `${(u.cpu_ratio * 100).toFixed(1)}%`),
          h('td', { class: 'num' }, u.vram_mib == null ? '—' : `${fmtGiB(u.vram_mib)} GB`))))))
        : quietEmpty('ready', { title: '暂无按用户统计', body: '主机读数里没有用户进程信息。' })),
    (sys.gpu_processes ?? []).length ? panel('', 'GPU 进程', '谁的哪个进程占着显存', h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['进程', '用户', 'systemd 单元', '显存'].map((t, i) => h('th', { scope: 'col', class: i === 3 ? 'num' : null }, t)))),
      h('tbody', {}, sys.gpu_processes.map((g) => h('tr', {},
        h('td', { class: 'tnum' }, `PID ${g.pid}`), h('td', {}, g.user ?? `uid ${g.uid}`), h('td', { class: 'mono' }, g.unit ?? '—'),
        h('td', { class: 'num' }, `${fmtGiB(g.used_mib)} GB`))))))) : null,
  ];
}

export async function usage(ctx) {
  let model; let hostState = 'ready'; let opsState = 'ready'; let sys = null;
  if (ctx.demo) { model = demoOverview(); sys = SNAP_SYSTEM; }
  else {
    const [ops, host, system] = await Promise.all([loadOps(), loadHost(), loadSystem()]);
    hostState = host.state; opsState = ops.state;
    model = overviewModel({ ops: ops.value, host: host.value });
    sys = system.state === 'ready' && system.value.cpu ? system.value : null;
  }
  const hf = model.host;
  const me = ctx.me?.linux_user ?? ctx.me?.username ?? null;
  const withVram = (model.services ?? []).filter((x) => x.vramMiB != null);
  const mem = sys?.memory;
  const cards = [
    { label: 'GPU 显存', tint: '#5856D6', glyph: 'memory', value: hf?.vramUsedMiB == null ? '—' : fmtGiB(hf.vramUsedMiB), unit: hf?.vramTotalMiB ? `/ ${fmtGiB(hf.vramTotalMiB, 0)} GB` : null, note: hf?.vramUsedMiB != null && hf.vramTotalMiB ? `已用 ${Math.round(hf.vramUsedMiB / hf.vramTotalMiB * 100)}%` : '暂无 GPU 读数' },
    { label: 'GPU 利用率', tint: '#FF9500', glyph: 'usage', value: hf?.utilRatio == null ? '—' : `${Math.round(hf.utilRatio * 100)}%`, note: [hf?.tempC != null ? `${Math.round(hf.tempC)} °C` : null, hf?.powerW != null ? `${Math.round(hf.powerW)} W` : null].filter(Boolean).join(' · ') || '暂无 GPU 读数' },
    { label: 'CPU', tint: '#34C759', glyph: 'cpu', value: sys ? `${(sys.cpu.utilization_ratio * 100).toFixed(1)}%` : '—', note: sys ? `${sys.cpu.cores} 线程 · 负载 ${sys.cpu.load1} / ${sys.cpu.load5} / ${sys.cpu.load15}` : '暂无主机读数' },
    { label: '内存', tint: '#30B0C7', glyph: 'services', value: mem ? fmtGiB(mem.used_mib) : '—', unit: mem ? `/ ${fmtGiB(mem.total_mib, 0)} GB` : null, note: mem ? `Swap ${fmtGiB(mem.swap_used_mib)} / ${fmtGiB(mem.swap_total_mib, 0)} GB${mem.swap_used_ratio > 0.9 ? ' · 已满' : ''}` : '暂无主机读数' },
  ];
  return h('div', { class: 'page' },
    pageHead('资源监控', 'GPU、主机与各推理服务的资源占用。读数只读获取，取不到的一律显示「—」。'),
    metricCards(cards),
    h('div', { class: 'row' },
      panel('grow', '显存分配', model.vram ? `GPU 显存 ${fmtGiB(model.vram.totalMiB, 0)} GB · 每个方块代表 0.25 GB` : null,
        model.vram ? vramPanel(model.vram) : quietEmpty(hostState, { title: '暂无显存读数', body: '接入 Prometheus 后，这里会按推理服务显示显存分配。' })),
      panel('side', 'GPU 与服务', null, gpuFacts(hf, hostState))),
    sys ? myShare(sys, me, hf?.vramTotalMiB) : null,
    sys ? userDonuts(sys, me, hf?.vramTotalMiB) : null,
    sys ? hostSystem(sys, me) : null,
    panel('', '各推理服务资源占用', null, model.services
      ? (withVram.length ? servicesTable(withVram, { compact: true }) : quietEmpty('ready', { title: '暂无按服务统计的显存', body: '推理服务登记并上报进程指标后，这里会列出每个服务的显存占用。' }))
      : quietEmpty(opsState, SERVICES_EMPTY)),
    h('p', { class: 'muted', style: { margin: 0, 'font-size': '12.5px' } }, '调用量、Token 与延迟统计在「网关」一栏。'));
}

/* ============================== 模型评测 ============================== */
export async function evaluation(ctx) {
  let runs; let state = 'ready';
  if (ctx.demo) runs = demoEvaluation();
  else { const r = await loadOps(); state = r.state; runs = r.state === 'ready' ? r.value.evaluation?.runs ?? [] : null; }
  const STATUS = { done: { tone: 'ok', text: '已完成' }, running: { tone: 'warn', text: '进行中' }, queued: { tone: 'off', text: '排队中' }, failed: { tone: 'bad', text: '失败' }, unknown: { tone: 'off', text: '未知' } };
  const body = runs
    ? (runs.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['模型服务', '数据集', '得分', '状态', '提交时间'].map((t, i) => h('th', { scope: 'col', class: i === 2 ? 'num' : null }, t)))),
      h('tbody', {}, runs.map((r) => h('tr', {},
        h('td', {}, r.model_id), h('td', {}, r.dataset),
        h('td', { class: 'num' }, r.metric == null ? '—' : `${(r.metric * 100).toFixed(1)}`),
        h('td', {}, statusBadge(STATUS[r.status] ?? STATUS.unknown)), h('td', { class: 'muted' }, relTime(r.created_at)))))))
      : h('div', { class: 'group' }, h('div', { class: 'empty' }, h('strong', {}, '还没有评测记录'), '在「扩展」中启用 EvalScope 后即可对已注册的模型服务发起评测。')))
    : emptyState(state);
  return h('div', { class: 'page' }, pageHead('模型评测', '由 EvalScope 执行的基准评测结果。'), body);
}

/* ============================== 用户与权限 ============================== */
const USERS_SUBTITLE = '平台账户独立于 Linux 账户，登录不依赖主机账户。登记推理服务时会自动记录它由哪个 Linux 用户运行。';

function copyButton(text) {
  const btn = h('button', { type: 'button', class: 'linkbtn', onclick: async () => {
    try { await navigator.clipboard.writeText(text); btn.textContent = '已复制'; } catch { btn.textContent = '请手动复制'; }
  } }, '复制');
  return btn;
}

export async function users(ctx) {
  let list; let state = 'ready';
  if (ctx.demo) list = DEMO_USERS.map((u) => ({ ...u }));
  else { const r = await loadUsers(); state = r.state; list = r.state === 'ready' ? r.value : null; }
  const root = h('div', { class: 'page' });
  if (!list) {
    put(root, pageHead('用户与权限', USERS_SUBTITLE), emptyState(state));
    return root;
  }
  const st = { sel: list.find((u) => u.username !== ctx.me?.username)?.id ?? list[0]?.id, temp: null, creating: false, msg: null };

  async function act(path, body, after) {
    if (ctx.demo) { after?.({ temporary_password: 'vela-2741-ridge' }); draw(); return; }
    const r = await send(path, body);
    if (!r.ok) { st.msg = r.message ?? `操作未完成（${r.reason}）`; draw(); return; }
    st.msg = null;
    after?.(r.data ?? {});
    const fresh = await loadUsers();
    if (fresh.state === 'ready') list = fresh.value;
    draw();
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    const sel = list.find((u) => u.id === st.sel) ?? list[0];
    put(root, pageHead('用户与权限', USERS_SUBTITLE,
      h('button', { type: 'button', class: 'btn primary', onclick: () => { st.creating = true; st.temp = null; draw(); } }, '添加用户')));
    if (st.msg) put(root, h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));

    const table = h('div', { class: 'table-wrap', style: { flex: '999 1 560px', 'min-width': 0 } }, h('table', { class: 'data', style: { 'min-width': '560px' } },
      h('thead', {}, h('tr', {}, ['用户', '限定 Linux 用户', '角色', '状态', '最近登录'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, list.length === 0
        ? h('tr', {}, h('td', { colspan: 5, class: 'muted', style: { 'text-align': 'center', padding: '28px 12px' } }, '还没有平台账户。点击「添加用户」创建第一个账户。'))
        : list.map((u) => h('tr', {
        class: 'selectable', 'aria-selected': String(!st.creating && u.id === sel?.id), tabindex: 0,
        onclick: () => { st.sel = u.id; st.temp = null; st.creating = false; draw(); },
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); st.sel = u.id; st.temp = null; st.creating = false; draw(); } },
      },
      h('td', {}, h('div', { style: { 'font-weight': 500 } }, u.display_name || u.username), h('div', { class: 'muted', style: { 'font-size': '13px' } }, u.username)),
      h('td', { class: u.linux_user ? 'tnum' : 'muted' }, u.linux_user ?? '不限'),
      h('td', {}, u.role === 'admin' ? '管理员' : '成员'),
      h('td', {}, u.status === 'active' ? '正常' : '已停用'),
      h('td', { class: u.last_login_at ? null : 'muted' }, u.last_login_at ? relTime(u.last_login_at) : '尚未登录'))))));

    let side;
    if (st.creating) {
      const form = { username: '', display_name: '', linux_user: '', role: 'member' };
      const inp = (key, ph) => h('input', { placeholder: ph, oninput: (e) => { form[key] = e.target.value.trim(); }, style: { width: '160px' } });
      side = h('aside', { 'aria-label': '添加用户', style: { flex: '1 1 320px', 'min-width': 0, display: 'flex', 'flex-direction': 'column', gap: '22px' } },
        h('div', { style: { 'font-size': '19px', 'font-weight': 600 } }, '添加用户'),
        section('账户', h('div', { class: 'group' },
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '用户名'), inp('username', 'zhang')),
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '显示名称'), inp('display_name', '张伟')),
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '限定 Linux 用户（可选）'), inp('linux_user', '不限')),
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '管理员权限'), h('input', { type: 'checkbox', onchange: (e) => { form.role = e.target.checked ? 'admin' : 'member'; }, style: { width: '20px', height: '20px' } }))),
        '创建后系统将生成一次性临时密码，用户首次登录时须设置新密码。'),
        h('div', { class: 'actions' },
          h('button', { type: 'button', class: 'btn', onclick: () => { st.creating = false; draw(); } }, '取消'),
          h('button', { type: 'button', class: 'btn primary', onclick: () => act(PATHS.users, form, (d) => {
            st.creating = false; st.temp = d.temporary_password ?? null;
            if (ctx.demo) { const nu = { id: `u-${form.username || 'new'}`, ...form, status: 'active', last_login_at: null }; list.push(nu); st.sel = nu.id; }
            else if (d.user?.id) st.sel = d.user.id;
          }) }, '创建')));
    } else if (sel) {
      side = h('aside', { 'aria-label': `${sel.display_name || sel.username} 的账户设置`, style: { flex: '1 1 320px', 'min-width': 0, display: 'flex', 'flex-direction': 'column', gap: '22px' } },
        h('div', { style: { display: 'flex', 'align-items': 'center', gap: '14px' } },
          h('span', { class: 'avatar', style: { width: '48px', height: '48px', 'font-size': '18px' } }, (sel.display_name || sel.username).slice(0, 1)),
          h('div', {}, h('div', { style: { 'font-size': '19px', 'font-weight': 600 } }, sel.display_name || sel.username), h('div', { class: 'muted' }, sel.username))),
        section('账户', h('div', { class: 'group' },
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '限定 Linux 用户'),
            h('input', { class: 'tnum', value: sel.linux_user ?? '', placeholder: '不限', style: { width: '140px' }, onchange: (e) => act(PATHS.userLinux, { user_id: sel.id, linux_user: e.target.value.trim() }, () => { if (ctx.demo) sel.linux_user = e.target.value.trim(); }) })),
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '管理员权限'),
            h('input', { type: 'checkbox', checked: sel.role === 'admin', style: { width: '20px', height: '20px' }, onchange: (e) => act(PATHS.userRole, { user_id: sel.id, role: e.target.checked ? 'admin' : 'member' }, () => { if (ctx.demo) sel.role = e.target.checked ? 'admin' : 'member'; }) }))),
        '留空表示不限：该账户可以登记由任意 Linux 用户运行的服务，系统只做记录。填写后，只能登记由这个 Linux 用户运行的服务。这一项不影响平台登录。'),
        section('密码', h('div', { class: 'group' },
          h('button', { type: 'button', class: 'cell action', onclick: () => {
            if (!ctx.demo && !globalThis.confirm?.(`重置 ${sel.display_name || sel.username} 的密码？该用户的现有会话将全部注销。`)) return;
            act(PATHS.userReset, { user_id: sel.id }, (d) => { st.temp = d.temporary_password ?? null; });
          } }, '重置密码…'),
          st.temp ? h('div', { class: 'cell', style: { 'flex-direction': 'column', 'align-items': 'flex-start', gap: '4px' } },
            h('div', { class: 'muted', style: { 'font-size': '13px' } }, '临时密码'),
            h('div', { style: { display: 'flex', 'align-items': 'baseline', gap: '12px' } },
              h('div', { class: 'mono', style: { 'font-size': '19px', 'font-weight': 600 } }, st.temp), copyButton(st.temp)),
            h('div', { class: 'muted', style: { 'font-size': '12px' } }, '该密码仅显示一次。用户下次登录时须设置新密码，现有会话已全部注销。')) : null)),
        h('div', { class: 'group' },
          h('button', { type: 'button', class: 'cell action danger', onclick: () => {
            const disabling = sel.status === 'active';
            if (disabling && !ctx.demo && !globalThis.confirm?.(`停用 ${sel.display_name || sel.username}？该用户将立即退出登录，其 API 密钥一并失效。`)) return;
            act(PATHS.userStatus, { user_id: sel.id, status: disabling ? 'disabled' : 'active' }, () => { if (ctx.demo) sel.status = disabling ? 'disabled' : 'active'; });
          } },
            sel.status === 'active' ? '停用用户…' : '恢复用户')));
    }
    put(root, h('div', { class: 'row', style: { gap: '24px', 'align-items': 'flex-start' } }, table, side));
  }
  draw();
  return root;
}

/* ============================== 插件 ============================== */
// 推理引擎、监控、评测都以插件形式接入。自带网页界面的插件，在这里多一个跳转入口
// （新标签页打开，不嵌入）；没有界面的插件（例如只有 API 的 vLLM）就是一张普通卡片。
const EXTENSIONS = [
  { key: 'strata', name: 'Strata', glyph: 'S', tint: '#1D1D1F', kind: '推理框架 · 默认', desc: '实验室默认推理框架，提供 OpenAI 兼容接口。', contrib: '推理适配器 · 原生控制台', status: 'strata' },
  { key: 'vllm', name: 'vLLM', glyph: 'v', tint: '#5856D6', kind: '推理框架', desc: '成员自行启动的 vLLM 服务经此适配器完成登记、健康检查与指标采集。', contrib: '推理适配器 · /metrics 指标映射', status: null },
  { key: 'sglang', name: 'SGLang', glyph: 'G', tint: '#AF52DE', kind: '推理框架', desc: '支持 SGLang 服务的登记与指标采集。', contrib: '推理适配器', status: null },
  { key: 'gateway', name: 'Gateway', glyph: 'W', tint: '#007AFF', kind: '网关', desc: '统一入口：校验 API 密钥、转发请求并记录请求元数据。', contrib: '请求日志 · 调用量统计', status: 'gateway' },
  { key: 'prometheus', name: 'Prometheus', glyph: 'P', tint: '#FF9500', kind: '指标数据源', desc: '采集 GPU、主机与各推理服务的指标，供「资源监控」查询。', contrib: '数据源 · 资源监控图表', status: 'prometheus' },
  { key: 'grafana', name: 'Grafana', glyph: 'G', tint: '#FF6B2C', kind: '高级分析', desc: '深度排查与自定义看板；门户内图表覆盖日常查看。', contrib: '高级分析看板', status: 'grafana' },
  { key: 'evalscope', name: 'EvalScope', glyph: 'E', tint: '#FF2D55', kind: '评测框架', desc: '对已登记的推理服务发起基准评测，结果在「模型评测」中展示。', contrib: '模型评测页面 · 原生报告', status: 'evalscope' },
];

export async function extensions(ctx) {
  let services = {}; let state = 'ready';
  let links = new Map(); let linksState = 'ready';
  if (ctx.demo) {
    services = demoOverview().host.services;
    links = new Map([['strata', { has_ui: true, ui_url: '/strata/', visibility: 'all' }], ['grafana', { has_ui: true, ui_url: '/grafana/', visibility: 'admin' }]]);
  } else {
    const [host, integrations] = await Promise.all([loadHost(), loadIntegrations()]);
    state = host.state; services = host.state === 'ready' ? (hostFacts(host.value)?.services ?? {}) : {};
    linksState = integrations.state;
    if (integrations.state === 'ready') links = new Map(integrations.value.map((row) => [row.key, row]));
  }
  const admin = ctx.isAdmin?.() === true;
  const root = h('div', { class: 'page' });
  const st = { editing: null, draft: '', visibility: 'all', msg: null };

  const statusOf = (key) => {
    if (!key) return { tone: 'off', text: '按服务登记时检测' };
    const v = services[key];
    if (v === true) return { tone: 'ok', text: '已连接' };
    if (v === false) return { tone: 'bad', text: '未响应' };
    return { tone: 'off', text: state === 'ready' ? '状态未知' : '未检测' };
  };

  async function save(key, url, visibility) {
    if (ctx.demo) { links.set(key, { has_ui: Boolean(url), ui_url: url || null, visibility }); st.editing = null; draw(); return; }
    const r = await send(`${PATHS.integrations}/${encodeURIComponent(key)}`, { ui_url: url, visibility }, { method: 'PUT' });
    if (!r.ok) { st.msg = r.message ?? `保存失败（${r.reason}）`; draw(); return; }
    st.msg = null; st.editing = null;
    ctx.invalidateLaunch?.();
    const rows = r.data?.integrations;
    if (Array.isArray(rows)) links = new Map(rows.map((row) => [row.key, row]));
    draw();
  }

  function jump(e) {
    const link = links.get(e.key);
    const url = safeJumpUrl(link?.ui_url);
    if (url) {
      return h('a', { class: 'btn', href: url, target: '_blank', rel: 'noopener noreferrer', 'aria-label': `打开 ${e.name} 界面（新标签页）` }, '打开界面 ↗');
    }
    if (link?.has_ui) return h('span', { class: 'muted', style: { 'font-size': '13px' } }, '界面仅管理员可打开');
    return null;
  }

  function editor(e) {
    if (st.editing !== e.key) {
      const link = links.get(e.key);
      return h('button', { type: 'button', class: 'linkbtn', onclick: () => {
        st.editing = e.key; st.draft = link?.ui_url ?? ''; st.visibility = link?.visibility ?? 'all'; st.msg = null; draw();
      } }, link?.has_ui ? '修改跳转地址' : '设置跳转地址');
    }
    return h('div', { class: 'group', style: { 'margin-top': '4px' } },
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '跳转地址'),
        h('input', { value: st.draft, placeholder: 'https://… 或 /grafana/', style: { width: '100%', 'min-width': 0 }, oninput: (ev) => { st.draft = ev.target.value.trim(); } })),
      h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '仅管理员可见'),
        h('input', { type: 'checkbox', checked: st.visibility === 'admin', style: { width: '20px', height: '20px' }, onchange: (ev) => { st.visibility = ev.target.checked ? 'admin' : 'all'; } })),
      h('div', { class: 'cell', style: { gap: '10px', 'justify-content': 'flex-end' } },
        h('button', { type: 'button', class: 'btn', onclick: () => { st.editing = null; draw(); } }, '取消'),
        links.get(e.key)?.has_ui ? h('button', { type: 'button', class: 'btn', onclick: () => save(e.key, '', st.visibility) }, '清除') : null,
        h('button', { type: 'button', class: 'btn primary', onclick: () => save(e.key, st.draft, st.visibility) }, '保存')));
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    put(root, pageHead('插件', '推理框架、监控与评测以插件形式接入。自带网页界面的插件可以从这里跳转打开；没有界面的就是单纯的推理或数据插件，在平台内直接使用。'));
    if (st.msg) put(root, h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));
    if (linksState !== 'ready' && !ctx.demo) put(root, h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } }, '跳转地址暂时无法读取，插件卡片仍按已知信息显示。'));
    put(root, h('div', { class: 'ext-grid' }, EXTENSIONS.map((e) => h('article', { class: 'ext' },
      h('div', { class: 'ext-head' },
        h('span', { class: 'ext-glyph', style: { background: e.tint }, 'aria-hidden': 'true' }, e.glyph),
        h('div', { style: { flex: '1 1 auto', 'min-width': 0 } }, h('h2', { class: 'ext-name' }, e.name), h('div', { class: 'muted', style: { 'font-size': '13px' } }, e.kind)),
        statusBadge(statusOf(e.status))),
      h('p', { style: { margin: 0, 'font-size': '13px' } }, e.desc),
      h('div', { class: 'ext-contrib' }, h('div', { class: 'muted' }, '贡献点'), h('div', {}, e.contrib)),
      h('div', { style: { display: 'flex', 'align-items': 'center', gap: '12px', 'flex-wrap': 'wrap' } }, jump(e), admin ? editor(e) : null)))));
  }
  draw();
  return root;
}

export { modelColorMap };
