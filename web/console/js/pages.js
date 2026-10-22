/*! web/console/js/pages.js — 控制台各页面。
 *  每个页面是 async (ctx) => Element。ctx.demo 为真时只使用 demo.js 的示例数据。 */
import { h, clear } from './dom.js';
import { fmtInt, fmtGiB } from './charts.js';
import {
  PATHS, loadOps, loadHost, loadSystem, loadEngineLive, loadModelKeys, createModelKey, loadCalls, loadUsers, loadIntegrations, loadDiscovery, safeJumpUrl, send,
} from './data.js';
import {
  overviewModel, servicesFromOps, modelsFromOps, keysFromOps, callsFacts, hostFacts, engineLabel, modelColorMap, preflightChecks,
} from './viewmodel.js';
import {
  demoOverview, demoServices, demoKeys, demoCalls, demoEvaluation, DEMO_USERS,
} from './demo.js';
import {
  pageHead, emptyState, statusBadge, segmented, section, relTime, clock,
  kpiBar, sunburstPanel, vramPanel, hourlyChart, hbarList, checkIcon, quietEmpty,
} from './ui.js';

const panel = (cls, title, sub, ...body) => h('section', { class: `panel ${cls}` },
  h('div', { class: 'panel-head' }, h('div', {}, h('h2', { class: 'panel-title' }, title), sub ? h('p', { class: 'panel-sub' }, sub) : null)),
  ...body);

/* ============================== 概览 ============================== */
// 平台的核心是资源监控与推理服务；API 调用统计是次要信息，没配置、没调用都属正常。
const CALLS_EMPTY = { title: '暂无调用记录', body: '这里只统计经网关转发的调用；直连推理服务的请求不经过网关，不会出现在这里。' };
const SERVICES_EMPTY = { title: '还没有登记推理服务', body: '在自己的 Linux 用户下启动推理引擎，再用平台账户登记，这里会显示它的运行状态与显存占用。' };

const runtimeStates = Object.freeze({
  healthy: { tone: 'ok', text: '健康' },
  degraded: { tone: 'warn', text: '响应异常' },
  unreachable: { tone: 'bad', text: '无法访问' },
  loaded: { tone: 'ok', text: '已加载' },
  unloaded: { tone: 'warn', text: '未加载' },
  applied: { tone: 'ok', text: '配置已发布' },
  not_applied: { tone: 'warn', text: '未发布' },
  unknown: { tone: 'off', text: '未知' },
});

function runtimePanel(runtime) {
  const state = (key) => statusBadge(runtimeStates[key] ?? runtimeStates.unknown);
  const row = (label, value, note = null) => h('div', { class: 'cell' },
    h('span', { class: 'grow' }, label), value, note ? h('span', { class: 'muted mono', style: { 'font-size': '12px' } }, note) : null);
  const modelName = runtime?.model?.id ?? '—';
  const modelState = runtime?.model?.state ?? 'unknown';
  const routeNote = runtime?.route?.targetModel ? `→ ${runtime.route.targetModel}` : '目标模型未知';
  return h('div', { class: 'group' },
    row('推理引擎 · Strata', state(runtime?.engine?.state), '本机 /health'),
    row('引擎当前加载的模型', state(modelState), modelName),
    row('Gateway 进程', state(runtime?.gateway?.state), '健康检查'),
    row(`API 别名 · ${runtime?.route?.alias ?? 'strata-vision'}`, state(runtime?.route?.state), routeNote),
    h('p', { class: 'muted', style: { margin: '4px 0 0', 'font-size': '12px' } }, '引擎健康、模型已加载、Gateway 在线和 API 路由已发布是四项独立检查；路由发布本身不代表 API 调用已验证。'));
}

/* ---------- 引擎实时负载：读数由 BFF 代取，前端不见密钥 ---------- */
// 读不到时分两种说法，绝不混成一句「暂无数据」：
// 「平台还没配凭据」「引擎没应答」是两种完全不同的运维动作，
// 而且**都不是**「负载为 0」。
const ENGINE_LIVE_DOWN = Object.freeze({
  key_not_configured: { title: '平台尚未配置引擎凭据', body: 'BFF 需要引擎自己的那把 key 才能代取读数；配好之后这里会自动显示。' },
  unauthorized: { title: '引擎拒绝了平台凭据', body: '凭据可能已在引擎侧轮换。这是「被拒绝」，不是「没有负载」。' },
  unreachable: { title: '引擎没有响应', body: '本机 127.0.0.1:8080 上没有应答 —— 这是「不知道」，不是「负载为 0」。' },
  http_error: { title: '引擎返回了错误', body: '引擎在线，但这次读数没取到；详见服务端日志。' },
  bad_payload: { title: '读数格式不符合约定', body: '引擎回了内容，但不是这份投影认得的结构，已停止显示以免误导。' },
});

const ENGINE_LIVE_EMPTY = { title: '引擎实时读数未接入', body: '本部署没有提供 /api/v2/engine/live；接入后这里会显示槽位、排队与吞吐。' };

const liveInt = (n) => (typeof n === 'number' && Number.isFinite(n) ? fmtInt(Math.round(n)) : '—');
const liveRate = (n) => (typeof n === 'number' && Number.isFinite(n) ? `${n.toFixed(1)} tok/s` : '—');
const liveClock = (iso) => (typeof iso === 'string' && iso.length >= 19 ? `${iso.slice(11, 19)}Z` : '—');

function engineRecentTable(recent) {
  if (!Array.isArray(recent) || !recent.length) {
    return h('p', { class: 'muted', style: { margin: '4px 0 0', 'font-size': '12px' } }, '本次启动以来还没有请求。');
  }
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', {}, h('tr', {}, ['时间 (UTC)', '用时', '输入', '输出', '解码', '结束'].map((t) => h('th', { scope: 'col' }, t)))),
    h('tbody', {}, recent.map((e) => h('tr', {},
      h('td', { class: 'mono' }, liveClock(e?.at)),
      h('td', { class: 'mono' }, typeof e?.duration_s === 'number' ? `${e.duration_s.toFixed(2)}s` : '—'),
      h('td', { class: 'mono' }, liveInt(e?.prompt_tokens)),
      h('td', { class: 'mono' }, liveInt(e?.output_tokens)),
      h('td', { class: 'mono' }, liveRate(e?.decode_tok_s)),
      h('td', { class: 'mono' }, e?.finish ?? '—'))))));
}

function engineLivePanel(live, demo) {
  if (demo) {
    return quietEmpty('ready', { title: '示例模式不显示引擎读数', body: '这一块只显示真实引擎；示例数字会让人误以为推理正在跑。' });
  }
  if (live.state !== 'ready' || !live.value) return quietEmpty(live.state, ENGINE_LIVE_EMPTY);
  const d = live.value;
  const observed = h('div', { class: 'cell' },
    h('span', { class: 'grow' }, '观测时间 (UTC)'),
    h('span', { class: 'muted mono', style: { 'font-size': '12px' } }, liveClock(d.observed_at)));
  if (d.reachable !== true) {
    const why = ENGINE_LIVE_DOWN[d.reason] ?? {
      title: '引擎读数不可用',
      body: `原因：${d.reason ?? '未报告'}。数值一律留空，不当作 0。`,
    };
    return h('div', { class: 'group' }, observed, quietEmpty('not_configured', why));
  }
  const row = (label, value, note = null) => h('div', { class: 'cell' },
    h('span', { class: 'grow' }, label), value,
    note ? h('span', { class: 'muted mono', style: { 'font-size': '12px' } }, note) : null);
  const slots = Array.isArray(d.live?.slots) ? d.live.slots : [];
  const busy = slots.filter((s) => s && s.state && s.state !== 'idle').length;
  const stateText = d.live?.state === 'idle' ? '空闲' : (d.live?.state ?? '未知');
  return h('div', { class: 'group' },
    observed,
    row('引擎状态', h('span', { class: 'mono' }, stateText), `模型 ${d.engine?.model ?? '—'} · v${d.engine?.version ?? '—'}`),
    row('并发槽位', h('span', { class: 'mono' }, `${busy} 在用 / 共 ${liveInt(d.live?.parallel)} 路`), slots.length ? `${slots.length} 个槽位` : null),
    row('排队中', h('span', { class: 'mono' }, liveInt(d.live?.queued)), '等待空槽的请求数；0 就是真的没人排队'),
    row('生成吞吐', h('span', { class: 'mono' }, liveRate(d.throughput?.tok_s)), '当前解码速率；空闲时为 —，不是 0'),
    row('本次启动累计', h('span', { class: 'mono' },
      `${liveInt(d.totals?.requests)} 次 · 入 ${liveInt(d.totals?.prompt_tokens)} / 出 ${liveInt(d.totals?.output_tokens)} tokens`),
    `自 ${liveClock(d.totals?.since)} 起`),
    engineRecentTable(d.recent),
    h('p', { class: 'muted', style: { margin: '4px 0 0', 'font-size': '12px' } },
      '读数由平台服务端代取，浏览器不接触引擎密钥；只有时延与 token 数，不含提示词与输出正文。'));
}

function loadedModelPanel(runtime) {
  const model = runtime?.model ?? {};
  const state = statusBadge(runtimeStates[model.state] ?? runtimeStates.unknown);
  const vision = model.vision === true ? '支持图像输入' : model.vision === false ? '不支持图像输入' : '能力未报告';
  return panel('grow', '当前已加载模型', '由推理引擎健康接口报告；这不是模型目录登记记录',
    h('div', { class: 'group' },
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, model.id ?? '暂无已加载模型'), state),
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '视觉能力'), h('span', {}, vision))));
}

function serviceRegistrySections(list) {
  const groups = [
    ['已登记推理引擎', list.filter((item) => item.kind === 'engine')],
    ['模型部署', list.filter((item) => item.kind === 'deployment')],
    ['对外 API 端点', list.filter((item) => item.kind === 'endpoint')],
  ];
  return h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '16px' } },
    ...groups.filter(([, rows]) => rows.length).map(([title, rows]) => panel('', title, null, servicesTable(rows))));
}

//: 三类登记在库里是三种东西：引擎（进程/端口）、部署（模型挂在某引擎上）、端点（对外
//: API）。概览页把它们合并成一张紧凑表时，必须显式标出类型 —— 否则一行引擎和一行
//: 部署长得一模一样，同一个模型看起来像「重复登记」。
const KIND_LABEL = Object.freeze({ engine: '引擎', deployment: '部署', endpoint: '端点' });

export function servicesTable(list, { compact = false } = {}) {
  const cols = compact ? ['推理服务', '类型', '推理框架', '所有者', '运行用户', '显存', '状态'] : ['推理服务', '推理框架', '所有者', '运行用户', '访问范围', '显存', '健康', '状态'];
  const vramCol = cols.indexOf('显存');
  return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
    h('thead', {}, h('tr', {}, cols.map((t, i) => h('th', { scope: 'col', class: i === vramCol ? 'num' : null }, t)))),
    h('tbody', {}, list.map((x) => h('tr', {},
      h('td', {}, h('div', { style: { 'font-weight': 500 } }, x.label),
        x.sharedToMe ? h('div', { class: 'muted', style: { 'font-size': '12px' } }, '共享给我')
          : x.port ? h('div', { class: 'muted tnum', style: { 'font-size': '12px' } }, `端口 ${x.port}`) : null),
      compact ? h('td', { class: 'muted' }, KIND_LABEL[x.kind] ?? '—') : null,
      h('td', {}, engineLabel(x.engine)),
      h('td', {}, x.owner ?? '—', x.self ? h('span', { class: 'muted' }, '（我）') : null),
      h('td', { class: x.runAs ? 'tnum' : 'muted' }, x.runAs ?? '—'),
      compact ? null : h('td', {}, x.share),
      h('td', { class: 'num' }, x.vramMiB == null ? '—' : `${fmtGiB(x.vramMiB)} GB`),
      compact ? null : h('td', {}, statusBadge(x.health)),
      h('td', {}, statusBadge(x.runtime)))))));
}

function gpuFacts(hf, hostState) {
  if (!hf) return quietEmpty(hostState, { title: '暂无 GPU 读数', body: '主机读数接入后，这里显示 GPU 利用率、温度与功耗。' });
  const row = (label, value) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, label), h('span', { class: 'tnum' }, value ?? '—'));
  const SERVICES = [['prometheus', 'Prometheus'], ['strata', 'Strata 引擎'], ['strata_model', 'Strata 模型'], ['gateway', 'Gateway'], ['evalscope', 'EvalScope']];
  const svc = (v, key) => (v === true
    ? { tone: 'ok', text: key === 'strata_model' ? '已加载' : '在线' }
    : v === false ? { tone: 'bad', text: key === 'strata_model' ? '未加载' : '未响应' }
      : { tone: 'off', text: '未检测' });
  return h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
    h('div', { class: 'group' },
      row('利用率', hf.utilRatio == null ? null : `${Math.round(hf.utilRatio * 100)}%`),
      row('温度', hf.tempC == null ? null : `${Math.round(hf.tempC)} °C`),
      row('功耗', hf.powerW == null ? null : `${Math.round(hf.powerW)} W`),
      row('数据来源', hf.source === 'nvml-verified' ? '本机 NVML' : hf.source ?? '—')),
    h('div', { class: 'group' }, SERVICES.map(([key, name]) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, name), statusBadge(svc(hf.services?.[key], key))))));
}

export async function overview(ctx) {
  let model;
  let states = {}; let registrySynthetic = false;
  if (ctx.demo) {
    model = demoOverview();
  } else {
    const [ops, host, calls] = await Promise.all([loadOps(), loadHost(), loadCalls()]);
    states = { ops: ops.state, host: host.state, calls: calls.state };
    registrySynthetic = ops.value?.synthetic === true;
    model = overviewModel({ ops: ops.value, host: host.value, log: calls.value });
  }
  const now = new Date();
  const sampleTime = model.host?.observedAt ? new Date(model.host.observedAt) : now;
  const stamp = `${String(sampleTime.getHours()).padStart(2, '0')}:${String(sampleTime.getMinutes()).padStart(2, '0')}`;
  const head = pageHead('概览', `RTX A6000 工作站 · 数据截至 ${stamp}`,
    registrySynthetic && !ctx.demo ? h('span', { class: 'btn', 'aria-disabled': 'true' }, 'Registry 演示模式')
      : h('a', { class: 'btn primary', href: '#/services/register' }, '登记推理服务'));

  const vramBody = model.vram
    ? vramPanel(model.vram)
    : quietEmpty(states.host ?? 'not_configured', { title: '暂无显存读数', body: '主机读数接入后，这里显示 GPU 全局显存占用。' });

  const services = model.services;
  const servicesBody = services
    ? (services.length ? servicesTable(services, { compact: true }) : quietEmpty('ready', { title: '当前没有真实登记记录', body: '本机实际引擎与加载模型显示在下方；演示登记记录已隐藏。' }))
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
      panel('grow', 'GPU 显存实测', model.vram
        ? `${fmtGiB(model.vram.totalMiB, 0)} GB 总量 · 已用 ${fmtGiB(model.vram.usedMiB)} GB · 保留 ${fmtGiB(model.vram.reservedMiB)} GB · 可用 ${fmtGiB(model.vram.freeMiB)} GB`
        : null, vramBody),
      panel('side', 'GPU 与服务', null, gpuFacts(model.host, states.host ?? 'not_configured'))),
    panel('', '实际运行状态', '引擎、加载模型、Gateway 与 API 路由分别探测', runtimePanel(model.host?.runtime)),
    panel('', '平台登记记录', services?.length ? `${services.length} 条可信登记 · 引擎 / 部署 / 端点分类型列出` : null, servicesBody),
    h('section', { style: { display: 'flex', 'flex-direction': 'column', gap: '16px' }, 'aria-labelledby': 'calls-title' },
      h('div', { class: 'page-head' },
        h('h2', { id: 'calls-title', style: { margin: 0, 'font-size': '19px', 'font-weight': 600 } }, 'API 调用'),
        h('a', { class: 'linkbtn', href: '#/requests' }, '查看请求日志')),
      hasCalls
        ? h('div', { class: 'row' },
          panel('grow', '调用分布', `内环为${model.distribution?.innerLabel ?? 'API 密钥'}，外环为其调用的推理服务`, dist),
          panel('side', '各服务调用量', null, usage))
        : h('div', { class: 'group' }, quietEmpty(states.calls ?? 'not_configured', CALLS_EMPTY))));
}

/* ============================== 推理服务 ============================== */
export async function services(ctx) {
  let list; let state = 'ready'; let runtime = null; let registrySynthetic = false;
  let engineLive = { state: 'not_configured', value: null };
  if (ctx.demo) list = demoServices();
  else {
    const [r, host, live] = await Promise.all([loadOps(), loadHost(), loadEngineLive()]);
    state = r.state; runtime = host.value?.runtime ?? null; registrySynthetic = r.value?.synthetic === true;
    list = r.state === 'ready' ? servicesFromOps(r.value) : null;
    engineLive = { state: live.state, value: live.value };
  }
  const body = list
    ? (list.length ? serviceRegistrySections(list) : h('div', { class: 'group' }, quietEmpty('ready', { title: '没有可信的登记记录', body: '本机真实引擎状态见上方；演示登记行已隐藏。' })))
    : emptyState(state);
  return h('div', { class: 'page' },
    pageHead('推理服务', '先看实际运行状态，再看平台登记与 API 发布状态。引擎健康、模型加载和端点可调用是不同的事实。',
      registrySynthetic && !ctx.demo ? h('span', { class: 'btn', 'aria-disabled': 'true' }, 'Registry 演示模式')
        : h('a', { class: 'btn primary', href: '#/services/register' }, '手动登记')),
    panel('', '本机实时运行状态', '来自 Strata /health、Gateway 健康检查与 Gateway 配置读回', runtimePanel(runtime)),
    panel('', '引擎实时负载', 'BFF 用服务端凭据代取 Strata /metrics 的槽位、排队与吞吐；浏览器不接触那把密钥', engineLivePanel(engineLive, ctx.demo)),
    panel('', '登记资源与对外端点', null, body),
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
    holder.append(panel('', title, st.data ? intro : null,
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
  let list; let state = 'ready'; let runtime = null; let registrySynthetic = false;
  if (ctx.demo) list = [
    { id: 'm1', name: 'Qwen2.5-32B-Instruct', format: 'hf', params: '32B', owner: '张伟', self: true, weightPath: '/data/models/Qwen2.5-32B-Instruct', description: '对话' },
    { id: 'm2', name: 'bge-m3', format: 'hf', params: '568M', owner: '陈静', self: false, weightPath: null, description: '向量化' },
  ];
  else {
    const [r, host] = await Promise.all([loadOps(), loadHost()]);
    state = r.state; registrySynthetic = r.value?.synthetic === true; runtime = host.value?.runtime ?? null;
    list = r.state === 'ready' ? modelsFromOps(r.value) : null;
  }
  const root = h('div', { class: 'page' });
  const st = { open: false, msg: null, form: { name: '', weight_path: '', format: 'hf', parameter_count: '', description: '' } };

  async function submit() {
    const f = st.form;
    if (!f.name) { st.msg = '请填写模型名称。'; draw(); return; }
    if (ctx.demo) { list.push({ id: `m${list.length + 1}`, name: f.name, format: f.format, params: f.parameter_count || null, owner: '我', self: true, weightPath: f.weight_path || null, description: f.description || null }); st.open = false; draw(); return; }
    if (registrySynthetic) { st.msg = '模型目录当前仍是演示配置，尚不能把登记记录写入真实 Registry。此页显示的是引擎实际加载状态。'; draw(); return; }
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
    root.append(pageHead('模型库', '模型登记描述模型资产；模型是否已加载由推理引擎运行状态单独确认。',
      registrySynthetic && !ctx.demo ? h('span', { class: 'btn', 'aria-disabled': 'true' }, 'Registry 演示模式')
        : h('button', { type: 'button', class: 'btn primary', onclick: () => { st.open = true; draw(); } }, '登记模型')));
    if (st.msg) root.append(h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));
    if (st.open) root.append(form());
    if (!ctx.demo) root.append(loadedModelPanel(runtime));
    root.append(list
      ? (list.length
        ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, ['模型', '格式', '参数量', '所有者', '权重位置'].map((t) => h('th', { scope: 'col' }, t)))),
          h('tbody', {}, list.map((m) => h('tr', {},
            h('td', {}, h('div', { style: { 'font-weight': 500 } }, m.name), m.description ? h('div', { class: 'muted', style: { 'font-size': '12px' } }, m.description) : null),
            h('td', {}, m.format === 'hf' ? 'Hugging Face' : m.format === 'gguf' ? 'GGUF' : (m.format ?? '—')),
            h('td', { class: 'tnum' }, m.params ?? '—'),
            h('td', {}, m.owner ?? '—', m.self ? h('span', { class: 'muted' }, '（我）') : null),
            h('td', { class: m.weightPath ? 'mono' : 'muted', style: { 'font-size': '12px', 'word-break': 'break-all' } }, m.weightPath ?? (m.self ? '—' : '仅所有者可见')))))))
        : h('div', { class: 'group' }, quietEmpty('ready', registrySynthetic && !ctx.demo
          ? { title: '真实模型目录尚未接入', body: '上方显示推理引擎当前实际加载的模型；演示登记行已隐藏。' }
          : { title: '还没有登记模型', body: '点击「登记模型」手动添加，或用下方的本机发现从授权目录里挑选。' })))
      : emptyState(state));
    root.append(discoveryPanel(ctx, 'models'));
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
    root.append(
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
      root.append(
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
      root.append(section('校验结果', h('div', { class: 'group' }, st.checks.list.map((c) => h('div', { class: 'check' }, checkIcon(c.ok),
        h('div', {}, h('div', { style: { 'font-weight': 500 } }, c.title), h('div', { class: 'muted', style: { 'font-size': '13px' } }, c.detail)))))));
    } else {
      root.append(
        h('div', { style: { display: 'flex', 'align-items': 'center', gap: '12px' } }, checkIcon(true, 28), h('h2', { style: { margin: 0, 'font-size': '22px', 'font-weight': 600 } }, '服务已发布')),
        h('p', { class: 'muted', style: { margin: 0 } }, `${st.name} 当前访问范围为「${{ private: '仅本人', all: '全部成员', selected: '指定成员' }[st.scope]}」，可在「推理服务」中查看运行状态。`),
        section('调用示例', h('div', { class: 'code' },
          `curl ${location.origin}/v1/chat/completions \\\n  -H "Authorization: Bearer $LLMLAB_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${st.name}", "messages": [{"role": "user", "content": "Hello"}]}'`)));
    }

    if (st.error) root.append(h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.error));
    if (st.error && st.cli) root.append(h('div', { class: 'code' }, st.cli));
    const blocked = !st.checks || st.checks.blocked;
    root.append(h('div', { class: 'actions' },
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

/* ---------- 模型推理密钥（平台签发的引擎密钥） ---------- */
// 这一页只管「调用本机推理引擎的密钥」。命令行与 Agent 的凭据走它们自己的
// 账户鉴权，不在这里签发 —— 两套凭据混在一页上，读者分不清哪把能干什么。
const maskKey = (k) => (typeof k === 'string' && k.length > 12
  ? `${k.slice(0, 8)}${'•'.repeat(10)}${k.slice(-4)}` : '—');
const DEMO_MODEL_KEY = {
  id: 'msk_demo', label: '演示密钥', key: 'sk-qwen-demo0000000000000000000000',
  model: 'qwen3.8-flash-next-iq2_xs', concurrency: 2,
  public_endpoint: `${location.origin}/v1/chat/completions`,
  created_at: new Date().toISOString(),
};

async function modelServiceKeys(ctx) {
  const holder = h('div', {});
  let list = null; let state = 'ready'; let msg = null; let issued = null;
  const revealed = new Set();
  const form = { label: '', busy: false };

  async function reload() {
    if (ctx.demo) { list = [DEMO_MODEL_KEY]; state = 'ready'; return; }
    const r = await loadModelKeys();
    state = r.state;
    list = r.state === 'ready' ? r.value : null;
  }

  async function mint() {
    if (form.busy) return;
    if (!ctx.demo && !globalThis.confirm?.(
      '新建密钥会把它写进推理引擎的环境文件，并让引擎重载才生效（约 1 分钟，期间推理不可用）。继续？')) return;
    form.busy = true; msg = null; draw();
    if (ctx.demo) {
      issued = { ...DEMO_MODEL_KEY, label: form.label || '演示密钥' };
      revealed.add(issued.key); form.busy = false; form.label = ''; draw(); return;
    }
    const r = await createModelKey(form.label || null);
    form.busy = false;
    if (!r.ok) { msg = r.message ?? `签发失败（${r.reason}）`; draw(); return; }
    issued = r.data;
    if (issued?.key) revealed.add(issued.key);
    form.label = '';
    await reload();
    draw();
  }

  /**
   * 一把密钥的「交付文本」。对方拿到这一块就能直接调，不需要和我们在同一个
   * tailnet：地址是 funnel 露出来的公网 HTTPS，唯一的门就是这把 key。
   */
  function handoffText(k) {
    const url = k.external_endpoint ?? k.public_endpoint ?? `${location.origin}/v1/chat/completions`;
    const lan = k.lan_endpoint ?? null;
    const lines = [
      `模型：${k.model ?? ''}`,
      `公网地址：${url}（任何地方都能用，不需要 tailscale）`,
    ];
    if (lan) lines.push(`内网地址：${lan}（同一局域网，不需要 tailscale）`);
    lines.push(
      `密钥：${k.key}`,
      '说明：不需要安装任何客户端，也不需要加入我们的内网。',
      '注意：这是推理模型，max_tokens 给小了 content 会为空，建议 >= 512。',
      '',
      `curl ${url} \\`,
      `  -H "Authorization: Bearer ${k.key}" \\`,
      '  -H "Content-Type: application/json" \\',
      `  -d '{"model":"${k.model ?? ''}","messages":[{"role":"user","content":"你好"}],"max_tokens":512}'`,
    );
    return lines.join('\n');
  }

  function copyHandoff(k) {
    Promise.resolve(navigator.clipboard?.writeText(handoffText(k))).then(
      () => { msg = '交付文本已复制，直接发给对方即可。'; draw(); },
      () => { msg = '浏览器拒绝了剪贴板写入，请点「查看」后手动复制。'; draw(); });
  }

  function keyRow(k) {
    const open = revealed.has(k.key);
    return h('div', { class: 'cell', style: { 'flex-direction': 'column', 'align-items': 'stretch', gap: '8px' } },
      h('div', { style: { display: 'flex', 'align-items': 'center', gap: '10px', 'flex-wrap': 'wrap' } },
        h('div', { class: 'grow' },
          h('div', { class: 'title' }, k.label ?? '未命名'),
          h('div', { class: 'sub' }, `${k.model ?? '—'} · ${k.concurrency ?? '—'} 路 · ${
            k.created_at ? new Date(k.created_at).toLocaleDateString() : '—'} 签发`)),
        h('span', { class: 'mono' }, open ? k.key : maskKey(k.key)),
        h('button', { type: 'button', class: 'linkbtn', onclick: () => {
          if (open) revealed.delete(k.key); else revealed.add(k.key); draw();
        } }, open ? '隐藏' : '查看'),
        h('button', { type: 'button', class: 'linkbtn', onclick: () => {
          Promise.resolve(navigator.clipboard?.writeText(k.key)).then(
            () => { msg = '已复制到剪贴板。'; draw(); },
            () => { msg = '浏览器拒绝了剪贴板写入，请点「查看」后手动复制。'; draw(); });
        } }, '复制'),
        h('button', { type: 'button', class: 'linkbtn', onclick: () => copyHandoff(k) }, '复制交付信息')),
      open ? h('div', { class: 'code' },
        `curl ${k.external_endpoint ?? k.public_endpoint ?? `${location.origin}/v1/chat/completions`} \\\n`
        + `  -H "Authorization: Bearer ${k.key}" \\\n`
        + '  -H "Content-Type: application/json" \\\n'
        + `  -d '{"model":"${k.model ?? ''}","messages":[{"role":"user","content":"你好"}]}'`) : null);
  }

  function draw() {
    while (holder.firstChild) holder.firstChild.remove();
    const body = [];
    if (!list) {
      body.push(quietEmpty(state, { title: '取不到密钥清单', body: '密钥来源没有接上或这次没读到。' }));
    } else if (!list.length) {
      body.push(quietEmpty('ready', { title: '还没有模型推理密钥', body: '用下面的「新建密钥」签发第一把。' }));
    } else {
      body.push(...list.map(keyRow));
    }
    body.push(h('div', { class: 'cell', style: { gap: '10px', 'flex-wrap': 'wrap' } },
      h('input', {
        value: form.label, placeholder: '用途，例如：调度器 A',
        style: { flex: '1 1 180px', 'min-width': 0 },
        oninput: (e) => { form.label = e.target.value.trim(); },
      }),
      h('button', { type: 'button', class: `btn primary${form.busy ? ' is-busy' : ''}`, onclick: mint },
        form.busy ? '正在签发…' : '新建密钥')));
    if (issued) {
      body.push(h('div', { class: 'cell', style: { 'flex-direction': 'column', 'align-items': 'stretch', gap: '6px' } },
        h('div', { class: 'title' }, '新密钥已签发'),
        h('div', { class: 'mono' }, issued.key),
        h('button', { type: 'button', class: 'linkbtn', style: { 'align-self': 'flex-start' }, onclick: () => copyHandoff(issued) }, '复制交付信息'),
        h('div', { class: 'sub' }, issued.restart?.triggered
          ? (issued.restart.detail ?? '引擎正在重载，新密钥稍后生效。')
          : `密钥已写入，但引擎没有自动重载（${issued.restart?.detail ?? '原因不明'}）—— 在引擎下次启动前它不会生效。`)));
    }
    if (msg) body.push(h('p', { class: 'sub', style: { 'text-align': 'left' } }, msg));
    holder.append(section('模型推理密钥', h('div', { class: 'group' }, body),
      '这些密钥只用于调用本机推理引擎，不具备平台管理权限。新建密钥要写进引擎的环境文件并让引擎重载，约 1 分钟后才生效。'));
  }

  await reload();
  draw();
  return holder;
}

/* ============================== API 密钥 ============================== */
export async function keys(ctx) {
  return h('div', { class: 'page narrow' },
    pageHead('API 密钥', '调用本机推理引擎时使用的密钥。'),
    await modelServiceKeys(ctx));
}

/* ============================== 请求日志 ============================== */
export async function requests(ctx) {
  let rows; let state = 'ready';
  if (ctx.demo) rows = demoCalls();
  else { const r = await loadCalls(); state = r.state; rows = r.state === 'ready' ? (callsFacts(r.value)?.recent ?? []) : null; }
  if (!rows) return h('div', { class: 'page' }, pageHead('请求日志', '经网关转发的请求元数据。不记录提示词与输出正文。'), h('div', { class: 'group' }, quietEmpty(state, CALLS_EMPTY)));
  let filter = '全部';
  const holder = h('div', {});
  const draw = () => {
    while (holder.firstChild) holder.firstChild.remove();
    const view = rows.filter((r) => filter === '全部' || (filter === '失败' ? !r.ok : r.ok));
    if (!view.length) { holder.append(h('div', { class: 'group' }, quietEmpty('ready', rows.length ? { title: '没有符合条件的请求', body: '换一个筛选条件试试。' } : CALLS_EMPTY))); return; }
    holder.append(h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['时间', '模型服务', '密钥', 'Token', '延迟', '状态'].map((t, i) => h('th', { scope: 'col', class: i === 3 || i === 4 ? 'num' : null }, t)))),
      h('tbody', {}, view.map((r) => h('tr', {},
        h('td', { class: 'tnum' }, clock(r.ts)), h('td', {}, r.model ?? '—'), h('td', { class: 'muted' }, r.key ?? '—'),
        h('td', { class: 'num' }, fmtInt(r.tokens)), h('td', { class: 'num' }, r.totalMs == null ? '—' : `${(r.totalMs / 1000).toFixed(2)} s`),
        h('td', { style: { color: r.ok ? 'var(--green-text)' : 'var(--red-text)' } }, `${r.status ?? '—'} ${r.ok ? '成功' : '失败'}`)))))));
  };
  draw();
  const seg = segmented('筛选结果', ['全部', '成功', '失败'], filter, (v) => { filter = v; seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.textContent === v))); draw(); });
  return h('div', { class: 'page' }, pageHead('请求日志', '经网关转发的请求元数据。不记录提示词与输出正文。', seg), holder);
}

/* ============================== 资源监控 ============================== */

/** 字节速率 → 人读。null 保持 null：没量到不能显示成 0 B/s。 */
function fmtRate(bps) {
  if (bps == null || !Number.isFinite(bps)) return null;
  const units = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
  let v = bps; let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function fmtPct(ratio, digits = 1) {
  return ratio == null ? null : `${(ratio * 100).toFixed(digits)}%`;
}

export async function usage(ctx) {
  let model; let hostState = 'ready'; let opsState = 'ready';
  let sys = null; let sysState = 'ready';
  if (ctx.demo) model = demoOverview();
  else {
    const [ops, host, system] = await Promise.all([loadOps(), loadHost(), loadSystem()]);
    hostState = host.state; opsState = ops.state; sysState = system.state;
    model = overviewModel({ ops: ops.value, host: host.value });
    sys = system.state === 'ready' ? system.value : null;
  }
  const hf = model.host;
  // GPU 全局占用：ops 契约里的按服务显存通常为空，但驱动自己上报了按进程
  // 的用量。有这份清单就按它切分，否则保留「已分配」这一整块 ——
  // 「没归因」与「归因到具体服务」是两件事，不能都画成同一块灰。
  if (model.vram && sys && Array.isArray(sys.gpu_processes) && sys.gpu_processes.length) {
    const labels = sys.gpu_processes.map((g) => g.unit ?? `pid ${g.pid}`);
    const colors = modelColorMap(labels);
    const parts = sys.gpu_processes.map((g, i) => ({
      id: `gpu-${g.pid}`, label: labels[i], owner: g.user ?? null, engine: null,
      mib: g.used_mib, color: colors.get(labels[i]) ?? '#8E8E93',
    }));
    const attributed = parts.reduce((t, p) => t + p.mib, 0);
    if (model.vram.usedMiB != null && model.vram.usedMiB - attributed > 512) {
      parts.push({ id: '__other', label: '其他进程', owner: null, engine: null, mib: model.vram.usedMiB - attributed, color: '#8E8E93' });
    }
    if (model.vram.reservedMiB > 0) {
      parts.push({ id: '__reserved', label: '驱动保留', owner: null, engine: null, mib: model.vram.reservedMiB, color: '#B8B8BD' });
    }
    model.vram = { ...model.vram, parts };
  }
  const metric = (label, value, unit) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, label), h('span', { class: 'tnum' }, value == null ? '—' : `${value}${unit}`));
  const withVram = (model.services ?? []).filter((x) => x.vramMiB != null);

  // 系统读数。sys 为 null 时按各自的 state 显示空态，绝不画一排 0。
  const sysFoot = sys ? `数据来自 BFF 直读 /proc（本机没有 node_exporter）；采样窗口 ${sys.window_seconds} 秒。` : null;
  const cpuPanel = panel('', 'CPU', sys ? `${sys.cpu.cores} 个逻辑核 · 负载 ${sys.cpu.load1} / ${sys.cpu.load5} / ${sys.cpu.load15}` : null,
    sys ? h('div', { class: 'group' },
      metric('利用率', fmtPct(sys.cpu.utilization_ratio), ''),
      metric('iowait', fmtPct(sys.cpu.iowait_ratio), ''),
      metric('1 / 5 / 15 分钟负载', `${sys.cpu.load1} / ${sys.cpu.load5} / ${sys.cpu.load15}`, ''),
      metric('逻辑核', sys.cpu.cores, ' 个')) : quietEmpty(sysState, { title: '暂无 CPU 读数', body: '主机读数接入后显示。' }),
    sys ? '利用率是整机口径；1 分钟负载在 20 核上可超过 1，它衡量的是排队长度而不是百分比。' : null);

  const mem = sys?.memory;
  const swapFull = mem && mem.swap_used_ratio != null && mem.swap_used_ratio > 0.9;
  const memPanel = panel('', '内存与交换区',
    mem && swapFull ? '交换区接近用满；只要可用内存仍充足，这通常是历史冷页残留，不等于当下缺内存。' : null,
    mem ? h('div', { class: 'group' },
      metric('已用 / 总量', `${fmtGiB(mem.used_mib)} / ${fmtGiB(mem.total_mib, 0)}`, ' GB'),
      metric('已用比例', fmtPct(mem.used_ratio), ''),
      metric('可用', fmtGiB(mem.available_mib), ' GB'),
      metric('交换区已用 / 总量', swapFull
        ? `${fmtGiB(mem.swap_used_mib)} / ${fmtGiB(mem.swap_total_mib, 0)}（${fmtPct(mem.swap_used_ratio)}，接近用满）`
        : `${fmtGiB(mem.swap_used_mib)} / ${fmtGiB(mem.swap_total_mib, 0)}`, ' GB')) : quietEmpty(sysState, { title: '暂无内存读数', body: '主机读数接入后显示。' }),
    sys ? sysFoot : null);

  const disk = sys?.disk;
  const diskPanel = panel('', '磁盘读写', disk ? `全机 ${fmtRate(disk.read_bytes_per_s) ?? '—'} 读 · ${fmtRate(disk.write_bytes_per_s) ?? '—'} 写` : null,
    disk && disk.devices.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['设备', '读', '写', '繁忙'].map((t, i) => h('th', { scope: 'col', class: i ? 'num' : null }, t)))),
        h('tbody', {}, disk.devices.map((d) => h('tr', {},
          h('td', { class: 'tnum' }, d.name),
          h('td', { class: 'num' }, fmtRate(d.read_bytes_per_s) ?? '—'),
          h('td', { class: 'num' }, fmtRate(d.write_bytes_per_s) ?? '—'),
          h('td', { class: 'num' }, fmtPct(d.busy_ratio) ?? '—'))))))
      : quietEmpty(sysState, { title: '暂无磁盘读数', body: '主机读数接入后显示各块盘的吞吐。' }),
    disk ? '「繁忙」是设备有 I/O 在进行的时间占比；多块盘同时忙时全机合计会超过 100%。' : null);

  const usage = sys?.disk?.usage;
  const usagePanel = panel('', '磁盘容量',
    usage && usage.length ? `${usage.length} 个文件系统` : null,
    usage && usage.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['挂载点', '已用 / 总量', '可用', '比例'].map((t, i) => h('th', { scope: 'col', class: i ? 'num' : null }, t)))),
        h('tbody', {}, usage.map((u) => h('tr', {},
          h('td', { class: 'tnum' }, u.mount),
          h('td', { class: 'num' }, `${fmtGiB(u.used_bytes / 1048576, 0)} / ${fmtGiB(u.total_bytes / 1048576, 0)} GB`),
          h('td', { class: 'num' }, `${fmtGiB(u.free_bytes / 1048576, 0)} GB`),
          h('td', { class: 'num' }, fmtPct(u.used_ratio) ?? '—'))))))
      : quietEmpty(sysState, { title: '暂无容量读数', body: '主机读数接入后显示各挂载点的剩余空间。' }),
    usage && usage.some((u) => u.used_ratio > 0.9)
      ? '有文件系统已用超过 90%，需要清理；这一列算的是文件系统，不是物理设备。'
      : '这一列算的是文件系统，不是物理设备：LVM 与分区会让两者对不上；已排除 tmpfs 等伪文件系统。');

  const thermal = sys?.thermal;
  const thermalPanel = panel('', '温度与风扇',
    thermal && thermal.fans_rpm.length ? `${thermal.fans_rpm.length} 个风扇` : null,
    thermal ? h('div', { class: 'group' },
      metric('CPU 最高核温', thermal.cpu_c == null ? null : thermal.cpu_c, ' °C'),
      metric('NVMe', thermal.nvme_c == null ? null : thermal.nvme_c, ' °C'),
      metric('风扇', thermal.fans_rpm.length ? `${Math.min(...thermal.fans_rpm)}–${Math.max(...thermal.fans_rpm)}` : null, ' RPM'))
      : quietEmpty(sysState, { title: '暂无温度读数', body: '主机读数接入后显示。' }),
    thermal ? 'CPU 取所有核心里的最高值，不取平均：平均值会把一颗过热的核心摊平掉。' : null);

  const conn = sys?.connections;
  const connPanel = panel('', '网络连接',
    conn ? `共 ${conn.total} 条 TCP 连接` : null,
    conn ? h('div', { class: 'group' },
      metric('监听中', conn.listening, ' 个端口'),
      metric('已建立', conn.established, ' 条'),
      metric('TIME_WAIT', conn.time_wait, ' 条'))
      : quietEmpty(sysState, { title: '暂无连接读数', body: '主机读数接入后显示。' }),
    conn ? '这里没有端口明细；监听 0 个通常说明服务没起来，已建立暴涨说明有并发在打。' : null);

  const eng = sys?.engine;
  const engValues = eng?.values ?? {};
  const engNum = (k) => (engValues[k] == null ? null : engValues[k]);
  const specOffered = engNum('speculative_drafts_offered_total');
  const specAccepted = engNum('speculative_drafts_accepted_total');
  const specRatio = specOffered ? specAccepted / specOffered : null;
  const cacheTotal = engNum('prompt_tokens_total');
  const cacheReused = engNum('cached_prompt_tokens_total');
  const enginePanel = panel('', '推理引擎吞吐',
    eng?.reachable ? '来自引擎自己的 exporter（127.0.0.1:9101）' : null,
    eng?.reachable ? h('div', { class: 'group' },
      metric('累计请求', engNum('requests_total'), ' 次'),
      metric('输出 / 输入 token', `${engNum('output_tokens_total') ?? '—'} / ${engNum('prompt_tokens_total') ?? '—'}`, ''),
      metric('当前出词速度', engNum('tokens_per_second'), ' tok/s'),
      metric('KV cache 复用', cacheTotal ? `${fmtPct(cacheReused / cacheTotal) ?? '—'}（${cacheReused} / ${cacheTotal}）` : null, ''),
      metric('投机解码命中', specRatio == null ? null : `${fmtPct(specRatio) ?? '—'}（${specAccepted} / ${specOffered}）`, ''))
      : quietEmpty('not_configured', { title: '引擎指标未接入', body: '引擎 exporter 没有回应；引擎可能在重启或未启动。' }),
    eng?.reachable
      ? `累计值自引擎上次启动起算（${eng.info?.gpu_name ?? 'GPU 未知'}，${eng.info?.cores ?? '?'} 核 ${eng.info?.threads ?? '?'} 线程）。出词速度是瞬时值，停止生成后会回落到 0。`
      : null);

  const net = sys?.network;
  const netPanel = panel('', '网络带宽', net ? `全机 ${fmtRate(net.rx_bytes_per_s) ?? '—'} 收 · ${fmtRate(net.tx_bytes_per_s) ?? '—'} 发` : null,
    net && net.interfaces.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['网卡', '接收', '发送'].map((t, i) => h('th', { scope: 'col', class: i ? 'num' : null }, t)))),
        h('tbody', {}, net.interfaces.map((n) => h('tr', {},
          h('td', { class: 'tnum' }, n.name),
          h('td', { class: 'num' }, fmtRate(n.rx_bytes_per_s) ?? '—'),
          h('td', { class: 'num' }, fmtRate(n.tx_bytes_per_s) ?? '—'))))))
      : quietEmpty(sysState, { title: '暂无网络读数', body: '主机读数接入后显示各网卡带宽。' }),
    net ? '不含 lo（回环流量不占物理带宽）；这里是瞬时速率，不是计费流量。' : null);

  // 「按用户」归并的是**进程的所属账号**，不是「谁在用机器」：polkitd、messagebus、
  // avahi、_chrony 这些发行版建的系统服务账号各占一行，一整屏看下来像是机器上有
  // 十几个人。所以默认只列人类账户（uid ≥ 1000），系统账号按需展开 —— 一行数据都
  // 没少，只是不让守护进程冒充使用者。
  const perUser = sys?.per_user;
  const isSystemAccount = (u) => (u?.account === 'system' || u?.account === 'human'
    ? u.account === 'system'
    : typeof u?.uid === 'number' && (u.uid < 1000 || u.uid === 65534));
  const humanUsers = (perUser ?? []).filter((u) => !isSystemAccount(u));
  const systemUsers = (perUser ?? []).filter(isSystemAccount);
  const systemProcesses = systemUsers.reduce((t, u) => t + (u.processes ?? 0), 0);
  const systemMemory = systemUsers.reduce((t, u) => t + (u.memory_mib ?? 0), 0);
  const userRow = (u) => h('tr', {},
    h('td', {}, u.user ?? `uid ${u.uid}`),
    h('td', { class: 'num' }, u.processes),
    h('td', { class: 'num' }, u.memory_mib == null ? '—' : `${fmtGiB(u.memory_mib)} GB`),
    h('td', { class: 'num' }, u.vram_mib == null ? '—' : `${fmtGiB(u.vram_mib)} GB`),
    h('td', { class: 'num' }, u.cpu_ratio == null ? '—' : u.cpu_ratio.toFixed(1)));
  // 只有系统账号、没有人类账户时（容器、专用机）不做折叠：否则整块会空掉。
  const collapse = humanUsers.length > 0 && systemUsers.length > 0;
  // 折叠是**真的增删行**，不是给 tbody 挂 display:none。thead/tbody 的显示由浏览器
  // 按表格布局自己算，藏一个分区并不可靠；行在不在，比一个样式属性诚实。
  const userBody = h('tbody', {}, (collapse ? humanUsers : (perUser ?? [])).map(userRow));
  let systemShown = false;
  const systemToggle = collapse
    ? h('button', { type: 'button', class: 'linkbtn', onclick: (event) => {
      systemShown = !systemShown;
      clear(userBody);
      const rows = systemShown ? humanUsers.concat(systemUsers) : humanUsers;
      for (const u of rows) userBody.append(userRow(u));
      event.currentTarget.textContent = systemShown
        ? '收起系统服务账号'
        : `显示系统服务账号（${systemUsers.length} 个）`;
    } }, `显示系统服务账号（${systemUsers.length} 个）`)
    : null;
  const usersPanel = panel('', '按用户',
    perUser
      ? (collapse
        ? `按进程的所属账号归并：${humanUsers.length} 个人类账户；另有 ${systemUsers.length} 个系统服务账号（${systemProcesses} 个进程 · ${fmtGiB(systemMemory)} GB）`
        : `按进程的所属账号归并，共 ${perUser.length} 个账户`)
      : null,
    perUser && perUser.length
      ? h('div', {},
        h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, ['账户', '进程数', '常驻内存', '显存', 'CPU（核）'].map((t, i) => h('th', { scope: 'col', class: i ? 'num' : null }, t)))),
          userBody)),
        systemToggle ? h('p', { style: { margin: '8px 0 0' } }, systemToggle) : null)
      : quietEmpty(sysState, { title: '暂无按账户统计', body: '主机读数接入后显示。' }),
    perUser ? '系统服务账号指 uid < 1000 或 65534 的账号（root、polkitd、messagebus、avahi 等），由发行版或软件包创建，不是人；它们默认折叠，数据仍完整。显存列来自驱动按进程上报的用量，同一进程的显存只算给它的属主。CPU 按「占满多少个逻辑核」计：一个账户的多进程可以在多核上并行，所以这一列可以大于 1，上限是逻辑核数；整机口径见上方 CPU 面板。按账户的磁盘读写需要 root 读 /proc/<pid>/io，本页取不到，因此没有这一列。' : null);

  return h('div', { class: 'page' },
    pageHead('资源监控', 'GPU、主机与各用户的资源占用。'),
    h('div', { class: 'row' },
      h('div', { style: { flex: '1 1 320px', 'min-width': 0 } }, section('GPU', hf ? h('div', { class: 'group' },
        metric('利用率', hf.utilRatio == null ? null : Math.round(hf.utilRatio * 100), '%'),
        metric('显存', hf.vramUsedMiB == null ? null : `${fmtGiB(hf.vramUsedMiB)} / ${fmtGiB(hf.vramTotalMiB, 0)}`, ' GB'),
        metric('驱动保留', hf.vramReservedMiB == null ? null : fmtGiB(hf.vramReservedMiB), ' GB'),
        metric('可用显存', hf.vramFreeMiB == null ? null : fmtGiB(hf.vramFreeMiB), ' GB'),
        metric('温度', hf.tempC == null ? null : Math.round(hf.tempC), ' °C'),
        metric('功耗', hf.powerW == null ? null : Math.round(hf.powerW), ' W')) : quietEmpty(hostState, { title: '暂无 GPU 读数', body: '主机读数接入后显示。' }),
      `${hf?.source === 'nvml-verified' ? '数据来自本机 NVML' : 'GPU 数据源待确认'}；超过 90 秒的读数不予显示。`)),
      h('div', { style: { flex: '2 1 520px', 'min-width': 0 } }, panel('', 'GPU 全局显存占用', model.vram ? '全卡占用；按进程归因，明细见下方「各推理服务资源占用」' : null,
        model.vram ? vramPanel(model.vram) : quietEmpty(hostState, { title: '暂无显存读数', body: '主机读数接入后显示 GPU 全局占用。' })))),
    h('div', { class: 'row' }, h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, cpuPanel),
      h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, memPanel)),
    h('div', { class: 'row' }, h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, diskPanel),
      h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, usagePanel)),
    h('div', { class: 'row' }, h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, netPanel),
      h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, connPanel)),
    h('div', { class: 'row' }, h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, thermalPanel),
      h('div', { style: { flex: '1 1 360px', 'min-width': 0 } }, enginePanel)),
    usersPanel,
    panel('', '各推理服务资源占用',
      sys && Array.isArray(sys.gpu_processes) && sys.gpu_processes.length
        ? `驱动按进程上报；共 ${sys.gpu_processes.length} 个计算进程` : null,
      sys && Array.isArray(sys.gpu_processes) && sys.gpu_processes.length
        ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, ['服务 / 进程', '用户', 'PID', '显存'].map((t, i) => h('th', { scope: 'col', class: i > 1 ? 'num' : null }, t)))),
          h('tbody', {}, sys.gpu_processes.map((g) => h('tr', {},
            h('td', { class: 'tnum' }, g.unit ?? '（未知单元）'),
            h('td', {}, g.user ?? (g.uid == null ? '—' : `uid ${g.uid}`)),
            h('td', { class: 'num' }, g.pid),
            h('td', { class: 'num' }, `${fmtGiB(g.used_mib)} GB`))))))
        : model.services && withVram.length
          ? servicesTable(withVram, { compact: true })
          : quietEmpty(opsState, { title: '暂无按服务统计的显存', body: '推理服务登记并上报进程指标后，这里会列出每个服务的显存占用。' })),
    h('div', { class: 'row' },
      panel('grow', '每小时请求数', null, model.hourly ? hourlyChart(model.hourly.points, { title: false }) : quietEmpty('not_configured', CALLS_EMPTY)),
      panel('side', '调用热度', '按星期与小时统计', quietEmpty('not_configured', { title: '暂无小时级统计', body: '网关按小时汇总调用后，这里将显示星期 × 小时的调用热力图。' }))));
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
    root.append(pageHead('用户与权限', USERS_SUBTITLE), emptyState(state));
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
    root.append(pageHead('用户与权限', USERS_SUBTITLE,
      h('button', { type: 'button', class: 'btn primary', onclick: () => { st.creating = true; st.temp = null; draw(); } }, '添加用户')));
    if (st.msg) root.append(h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));

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
    root.append(h('div', { class: 'row', style: { gap: '24px', 'align-items': 'flex-start' } }, table, side));
  }
  draw();
  return root;
}

/* ============================== 插件 ============================== */
// 推理引擎、监控、评测都以插件形式接入。自带网页界面的插件，在这里多一个跳转入口
// （新标签页打开，不嵌入）；没有界面的插件（例如只有 API 的 vLLM）就是一张普通卡片。
const EXTENSIONS = [
  // 2026-10-22：引擎读数改由 BFF 代取（推理服务 · 引擎实时负载），所以这里不再
  // 暗示「去 Strata 自己的控制台填一把 key 才能看见数据」—— 那条路只是管理员可选。
  { key: 'strata', name: 'Strata', glyph: 'S', tint: '#1D1D1F', kind: '推理框架 · 默认', desc: '实验室默认推理框架，提供 OpenAI 兼容接口。引擎的槽位、排队与吞吐已在「推理服务 · 引擎实时负载」里由平台代取，不需要在 Strata 设置里填密钥。', contrib: '推理适配器 · 原生控制台（管理员可选）', status: 'strata' },
  { key: 'vllm', name: 'vLLM', glyph: 'v', tint: '#5856D6', kind: '推理框架', desc: '成员自行启动的 vLLM 服务经此适配器完成登记、健康检查与指标采集。', contrib: '推理适配器 · /metrics 指标映射', status: null },
  { key: 'sglang', name: 'SGLang', glyph: 'G', tint: '#AF52DE', kind: '推理框架', desc: '支持 SGLang 服务的登记与指标采集。', contrib: '推理适配器', status: null },
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
    root.append(pageHead('插件', '推理框架、监控与评测以插件形式接入。自带网页界面的插件可以从这里跳转打开；没有界面的就是单纯的推理或数据插件，在平台内直接使用。'));
    if (st.msg) root.append(h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));
    if (linksState !== 'ready' && !ctx.demo) root.append(h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } }, '跳转地址暂时无法读取，插件卡片仍按已知信息显示。'));
    root.append(h('div', { class: 'ext-grid' }, EXTENSIONS.map((e) => h('article', { class: 'ext' },
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
