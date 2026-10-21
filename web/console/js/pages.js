/*! web/console/js/pages.js — 控制台各页面。
 *  每个页面是 async (ctx) => Element。ctx.demo 为真时只使用 demo.js 的示例数据。 */
import { h } from './dom.js';
import { fmtInt, fmtGiB } from './charts.js';
import {
  PATHS, loadOps, loadHost, loadCalls, loadUsers, send,
} from './data.js';
import {
  overviewModel, servicesFromOps, keysFromOps, callsFacts, hostFacts, engineLabel, modelColorMap,
} from './viewmodel.js';
import {
  demoOverview, demoServices, demoKeys, demoCalls, demoEvaluation, DEMO_USERS,
} from './demo.js';
import {
  pageHead, emptyState, statusBadge, segmented, section, relTime, clock,
  kpiBar, sunburstPanel, vramPanel, hourlyChart, hbarList, checkIcon,
} from './ui.js';

const panel = (cls, title, sub, ...body) => h('section', { class: `panel ${cls}` },
  h('div', { class: 'panel-head' }, h('div', {}, h('h2', { class: 'panel-title' }, title), sub ? h('p', { class: 'panel-sub' }, sub) : null)),
  ...body);

/* ============================== 概览 ============================== */
export async function overview(ctx) {
  let model;
  let states = {};
  if (ctx.demo) {
    model = demoOverview();
  } else {
    const [ops, host, calls] = await Promise.all([loadOps(), loadHost(), loadCalls()]);
    states = { ops: ops.state, host: host.state, calls: calls.state };
    model = overviewModel({ ops: ops.value, host: host.value, log: calls.value });
  }
  let range = '今日';
  const now = new Date();
  const stamp = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const head = pageHead('概览', `数据截至今天 ${stamp}`,
    segmented('统计周期', ['今日', '近 7 日', '近 30 日'], range, () => {}));

  const vramBody = model.vram
    ? vramPanel(model.vram)
    : emptyState(states.host ?? 'not_configured', { title: 'GPU 读数不可用', body: '尚未读取到经过校验的显存数据。Prometheus 接入后，这里会按模型服务显示显存分配。' });

  const dist = model.distribution
    ? sunburstPanel(model.distribution, model.modelColors)
    : emptyState(states.calls ?? 'not_configured');

  const usage = h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
    ...(model.modelUsage ? hbarList(model.modelUsage, model.modelColors) : [emptyState(states.calls ?? 'not_configured')]));

  const hourly = model.hourly
    ? h('div', { style: { 'border-top': '1px solid var(--hairline)', 'padding-top': '14px', 'margin-top': '4px' } },
      hourlyChart(model.hourly.points))
    : null;

  const myServices = (model.services ?? []).filter((x) => x.self);
  const services = section('模型服务',
    myServices.length
      ? h('div', { class: 'group' }, myServices.map((x) => h('div', { class: 'cell' },
        h('div', { class: 'grow' }, h('div', { class: 'title' }, x.label), h('div', { class: 'sub' }, `${engineLabel(x.engine)}${x.port ? ` · 端口 ${x.port}` : ''} · ${x.share}`)),
        statusBadge(x.runtime))))
      : h('div', { class: 'group' }, h('div', { class: 'cell' }, h('div', { class: 'grow sub' }, model.services ? '尚未注册模型服务。' : '注册中心未接入。'))));

  const keys = section('API 密钥',
    h('div', { class: 'group' },
      (model.keys ?? []).filter((k) => k.active).slice(0, 3).map((k) => h('div', { class: 'cell' },
        h('div', { class: 'grow' }, h('div', { class: 'title' }, k.name), h('div', { class: 'sub tnum' }, k.prefix)),
        h('span', { class: 'muted', style: { 'font-size': '13px' } }, relTime(k.lastUsedAt)))),
      (model.keys ?? []).length ? null : h('div', { class: 'cell' }, h('div', { class: 'grow sub' }, '暂无密钥。')),
      h('div', { class: 'cell' }, h('span', { class: 'grow muted' }, '接入端点'), h('span', { class: 'mono' }, `${location.origin}/v1`))));

  const recent = model.recent
    ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['时间', '模型服务', '密钥', 'Token', '延迟', '状态'].map((t, i) => h('th', { scope: 'col', class: i === 3 || i === 4 ? 'num' : null }, t)))),
      h('tbody', {}, model.recent.map((r) => h('tr', {},
        h('td', { class: 'tnum' }, clock(r.ts)), h('td', {}, r.model ?? '—'), h('td', { class: 'muted' }, r.key ?? '—'),
        h('td', { class: 'num' }, fmtInt(r.tokens)), h('td', { class: 'num' }, r.totalMs == null ? '—' : `${(r.totalMs / 1000).toFixed(2)} s`),
        h('td', { style: { color: r.ok ? 'var(--green-text)' : 'var(--red-text)' } }, `${r.status ?? '—'} ${r.ok ? '成功' : '失败'}`))))))
    : emptyState(states.calls ?? 'not_configured');

  return h('div', { class: 'page' },
    head,
    kpiBar(model.kpis),
    panel('', '显存分配', model.vram ? `GPU 显存 ${fmtGiB(model.vram.totalMiB, 0)} GB · 每个方块代表 0.25 GB` : null, vramBody),
    h('div', { class: 'row' },
      panel('grow', '调用分布', model.distribution ? `内环为${model.distribution.innerLabel}，外环为其调用的模型服务` : null, dist),
      panel('side', '模型服务调用量', null, usage, hourly)),
    h('section', { style: { display: 'flex', 'flex-direction': 'column', gap: '16px' }, 'aria-labelledby': 'ws-title' },
      h('div', { class: 'page-head' }, h('h2', { id: 'ws-title', style: { margin: 0, 'font-size': '22px', 'font-weight': 700 } }, '我的工作区'),
        h('a', { class: 'btn primary', href: '#/services/register' }, '注册模型服务')),
      h('div', { class: 'row' }, h('div', { style: { flex: '1 1 420px', 'min-width': 0 } }, services), h('div', { style: { flex: '1 1 420px', 'min-width': 0 } }, keys)),
      section('最近请求', recent)));
}

/* ============================== 模型服务 ============================== */
export async function services(ctx) {
  let list; let state = 'ready';
  if (ctx.demo) list = demoServices();
  else {
    const r = await loadOps();
    state = r.state;
    list = r.state === 'ready' ? servicesFromOps(r.value) : null;
  }
  const body = list
    ? (list.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['模型服务', '推理框架', '所有者', '访问范围', '显存', '健康', '状态'].map((t, i) => h('th', { scope: 'col', class: i === 4 ? 'num' : null }, t)))),
        h('tbody', {}, list.map((x) => h('tr', {},
          h('td', {}, h('div', { style: { 'font-weight': 500 } }, x.label), x.sharedToMe ? h('div', { class: 'muted', style: { 'font-size': '12px' } }, '共享给我') : null),
          h('td', {}, engineLabel(x.engine)), h('td', { class: 'tnum' }, x.owner ?? '—', x.self ? h('span', { class: 'muted' }, '（我）') : null),
          h('td', {}, x.share), h('td', { class: 'num' }, x.vramMiB == null ? '—' : `${fmtGiB(x.vramMiB)} GB`),
          h('td', {}, statusBadge(x.health)), h('td', {}, statusBadge(x.runtime)))))))
      : h('div', { class: 'group' }, h('div', { class: 'empty' }, h('strong', {}, '还没有模型服务'), '先用本人的 Linux 账户启动推理服务，再在这里注册。')))
    : emptyState(state);
  return h('div', { class: 'page' },
    pageHead('模型服务', '实验室内已注册的推理服务。成员自行启动服务后注册，系统校验进程归属后发布。',
      h('a', { class: 'btn primary', href: '#/services/register' }, '注册模型服务')),
    body);
}

/* ============================== 注册模型服务 ============================== */
const FRAMEWORKS = [['vllm', 'vLLM'], ['sglang', 'SGLang'], ['tei', 'Text Embeddings Inference'], ['ollama', 'Ollama'], ['llama.cpp', 'llama.cpp'], ['strata', 'Strata']];

export async function register(ctx) {
  const st = { step: 1, framework: 'vllm', port: '', name: '', scope: 'private', checks: null, error: null, busy: false };
  const root = h('div', { class: 'page narrow' });

  const field = (label, input) => h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, label), input);

  async function verifyAndRegister() {
    st.busy = true; st.error = null; draw();
    const port = Number(st.port);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) { st.busy = false; st.error = '请输入 1024–65535 之间的端口号。'; draw(); return; }
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(st.name)) { st.busy = false; st.error = '服务名称只能包含字母、数字、点、下划线和连字符。'; draw(); return; }
    if (ctx.demo) {
      st.checks = [
        { ok: true, title: '端口可达', detail: `127.0.0.1:${port} 已响应，耗时 32 ms` },
        { ok: true, title: '进程归属', detail: `监听该端口的进程运行账户为 ${ctx.me?.linux_user ?? '当前用户'}，与账户绑定一致` },
        { ok: true, title: '接口兼容性', detail: `GET /v1/models 返回 ${st.name}` },
        { ok: true, title: '名称唯一性', detail: '实验室内不存在同名服务' },
      ];
      st.step = 2; st.busy = false; draw(); return;
    }
    const r = await send(PATHS.registryEngines, {
      name: st.name,
      protocol: 'openai_compatible',
      target_url: `http://127.0.0.1:${port}`,
      tags: [`framework:${st.framework}`, `scope:${st.scope}`],
    });
    st.busy = false;
    if (!r.ok) {
      st.error = r.state === 'not_configured' ? '本部署尚未启用注册接口。' : r.state === 'signed_out' ? '会话已失效，请重新登录。' : (r.message ?? `注册失败（${r.reason}）`);
      draw(); return;
    }
    const eng = r.data?.engine ?? r.data ?? {};
    const own = eng.ownership ?? {};
    const ver = eng.verification ?? {};
    const ownedOk = own.state === 'uid_verified' || own.state === 'operator_verified';
    st.checks = [
      { ok: eng.status !== 'unavailable', title: '端口可达', detail: eng.status === 'unavailable' ? '端口未响应或返回异常' : `127.0.0.1:${port} 已响应` },
      { ok: ownedOk, title: '进程归属', detail: ownedOk ? '监听进程的运行账户与当前用户绑定的 Linux 账户一致' : `未能证明进程归属（${own.reason ?? own.state ?? '未校验'}）。服务已登记，但在证明归属前不能共享或被调用。` },
      { ok: ver.verified === true, title: '接口兼容性', detail: ver.verified === true ? 'OpenAI 兼容接口校验通过' : '接口校验未通过' },
    ];
    st.step = 2; draw();
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    const labels = ['服务配置', '归属校验', '发布'];
    root.append(
      h('header', {},
        h('a', { class: 'crumb', href: '#/services' }, '模型服务'),
        h('h1', { class: 'page-title', style: { 'margin-top': '6px' } }, '注册模型服务'),
        h('p', { class: 'page-sub' }, '请先使用本人的 Linux 账户启动推理服务。注册时，系统将校验该端口的监听进程是否归属于您的账户，校验通过后服务方可发布。')),
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
          field('监听端口', h('input', { class: 'tnum', inputmode: 'numeric', placeholder: '18009', value: st.port, oninput: (e) => { st.port = e.target.value.trim(); } })),
          field('服务名称', h('input', { placeholder: 'qwen2.5-7b-sft-v3', value: st.name, oninput: (e) => { st.name = e.target.value.trim(); }, style: { width: '260px' } }))),
          '服务仅需监听 127.0.0.1，由网关在本机转发。服务名称即调用时的 model 参数，须在全实验室范围内唯一。'),
        h('fieldset', { style: { border: 0, padding: 0, margin: 0 } },
          h('legend', { class: 'section-title', style: { 'margin-bottom': '8px' } }, '访问范围'),
          h('div', { class: 'group' }, [['private', '仅本人'], ['all', '全部成员'], ['selected', '指定成员']].map(([v, l]) =>
            h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, l),
              h('input', { type: 'radio', name: 'scope', value: v, checked: st.scope === v, onchange: () => { st.scope = v; }, style: { width: '18px', height: '18px' } }))))));
    } else if (st.step === 2) {
      root.append(section('校验结果', h('div', { class: 'group' }, st.checks.map((c) => h('div', { class: 'check' }, checkIcon(c.ok),
        h('div', {}, h('div', { style: { 'font-weight': 500 } }, c.title), h('div', { class: 'muted', style: { 'font-size': '13px' } }, c.detail)))))));
    } else {
      root.append(
        h('div', { style: { display: 'flex', 'align-items': 'center', gap: '12px' } }, checkIcon(true, 28), h('h2', { style: { margin: 0, 'font-size': '22px', 'font-weight': 600 } }, '服务已发布')),
        h('p', { class: 'muted', style: { margin: 0 } }, `${st.name} 当前访问范围为「${{ private: '仅本人', all: '全部成员', selected: '指定成员' }[st.scope]}」，可在「模型服务」中调整或停用。`),
        section('调用示例', h('div', { class: 'code' },
          `curl ${location.origin}/v1/chat/completions \\\n  -H "Authorization: Bearer $LLMLAB_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${st.name}", "messages": [{"role": "user", "content": "Hello"}]}'`)));
    }

    if (st.error) root.append(h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.error));
    const allOk = st.checks?.every((c) => c.ok);
    root.append(h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn', onclick: () => { if (st.step === 1) ctx.navigate('#/services'); else { st.step -= 1; st.error = null; draw(); } } }, st.step === 1 ? '取消' : '上一步'),
      st.step === 1 ? h('button', { type: 'button', class: 'btn primary', disabled: st.busy, onclick: verifyAndRegister }, st.busy ? '正在校验…' : '开始校验')
        : st.step === 2 ? h('button', { type: 'button', class: 'btn primary', disabled: !allOk, onclick: () => { st.step = 3; draw(); } }, '发布')
          : h('a', { class: 'btn primary', href: '#/services' }, '完成')),
      h('p', { class: 'muted', style: { margin: 0, 'padding-top': '20px', 'border-top': '1px solid var(--hairline)', 'font-size': '13px' } },
        '管理员可在「用户与权限」中为任意成员代为注册模型服务，归属校验规则保持一致。'));
  }
  draw();
  return root;
}

/* ============================== API 密钥 ============================== */
export async function keys(ctx) {
  let list; let state = 'ready';
  if (ctx.demo) list = demoKeys();
  else { const r = await loadOps(); state = r.state; list = r.state === 'ready' ? keysFromOps(r.value) : null; }
  const body = list
    ? section(null, h('div', { class: 'group' },
      list.length ? list.map((k) => h('div', { class: 'cell' },
        h('div', { class: 'grow' }, h('div', { class: 'title' }, k.name), h('div', { class: 'sub tnum' }, `${k.prefix} · ${k.scopes.join('、') || '无权限范围'}`)),
        h('span', { class: 'muted', style: { 'font-size': '13px' } }, k.active ? `最近使用 ${relTime(k.lastUsedAt)}` : '已撤销')))
        : h('div', { class: 'cell' }, h('div', { class: 'grow sub' }, '暂无密钥。'))),
    '密钥只显示前缀，完整内容仅在创建时显示一次。')
    : emptyState(state);
  return h('div', { class: 'page narrow' },
    pageHead('API 密钥', '调用模型服务时使用的凭据。'),
    body,
    section('接入', h('div', { class: 'group' },
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '接入端点'), h('span', { class: 'mono' }, `${location.origin}/v1`)),
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '接口协议'), h('span', {}, 'OpenAI 兼容')),
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '命令行工具'), h('span', { class: 'mono' }, 'llmlab --help'))),
    '在命令行中执行 llmlab keys 系列命令可创建、轮换或撤销密钥。'));
}

/* ============================== 请求日志 ============================== */
export async function requests(ctx) {
  let rows; let state = 'ready';
  if (ctx.demo) rows = demoCalls();
  else { const r = await loadCalls(); state = r.state; rows = r.state === 'ready' ? (callsFacts(r.value)?.recent ?? []) : null; }
  if (!rows) return h('div', { class: 'page' }, pageHead('请求日志', '经网关转发的请求元数据。不记录提示词与输出正文。'), emptyState(state));
  let filter = '全部';
  const holder = h('div', {});
  const draw = () => {
    while (holder.firstChild) holder.firstChild.remove();
    const view = rows.filter((r) => filter === '全部' || (filter === '失败' ? !r.ok : r.ok));
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

/* ============================== 资源与用量 ============================== */
export async function usage(ctx) {
  let model; let hostState = 'ready';
  if (ctx.demo) model = demoOverview();
  else {
    const [ops, host] = await Promise.all([loadOps(), loadHost()]);
    hostState = host.state;
    model = overviewModel({ ops: ops.value, host: host.value });
  }
  const hf = model.host;
  const metric = (label, value, unit) => h('div', { class: 'cell' }, h('span', { class: 'grow' }, label), h('span', { class: 'tnum' }, value == null ? '—' : `${value}${unit}`));
  return h('div', { class: 'page' },
    pageHead('资源与用量', 'GPU、主机与各模型服务的资源占用。'),
    h('div', { class: 'row' },
      h('div', { style: { flex: '1 1 320px', 'min-width': 0 } }, section('GPU', hf ? h('div', { class: 'group' },
        metric('利用率', hf.utilRatio == null ? null : Math.round(hf.utilRatio * 100), '%'),
        metric('显存', hf.vramUsedMiB == null ? null : `${fmtGiB(hf.vramUsedMiB)} / ${fmtGiB(hf.vramTotalMiB, 0)}`, ' GB'),
        metric('温度', hf.tempC == null ? null : Math.round(hf.tempC), ' °C'),
        metric('功耗', hf.powerW == null ? null : Math.round(hf.powerW), ' W')) : emptyState(hostState), '数据来自 Prometheus 只读查询，超过 90 秒未更新的读数不予显示。')),
      h('div', { style: { flex: '2 1 520px', 'min-width': 0 } }, panel('', '显存分配', model.vram ? '每个方块代表 0.25 GB' : null, model.vram ? vramPanel(model.vram) : emptyState(hostState)))),
    h('div', { class: 'row' },
      panel('grow', '每小时请求数', null, model.hourly ? hourlyChart(model.hourly.points, { title: false }) : emptyState('not_configured')),
      panel('side', '调用热度', '按星期与小时统计', emptyState('not_configured', { title: '网关尚未提供小时级分布', body: '网关目前只记录累计调用量。按小时汇总后，这里将显示星期 × 小时的调用热力图。' }))));
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
export async function users(ctx) {
  let list; let state = 'ready';
  if (ctx.demo) list = DEMO_USERS.map((u) => ({ ...u }));
  else { const r = await loadUsers(); state = r.state; list = r.state === 'ready' ? r.value : null; }
  const root = h('div', { class: 'page' });
  if (!list) {
    root.append(pageHead('用户与权限', '每个用户绑定一个 Linux 账户，模型服务的归属以该账户为准。'), emptyState(state));
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
    root.append(pageHead('用户与权限', '每个用户绑定一个 Linux 账户，模型服务的归属以该账户为准。',
      h('button', { type: 'button', class: 'btn primary', onclick: () => { st.creating = true; st.temp = null; draw(); } }, '添加用户')));
    if (st.msg) root.append(h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg));

    const table = h('div', { class: 'table-wrap', style: { flex: '999 1 560px', 'min-width': 0 } }, h('table', { class: 'data', style: { 'min-width': '560px' } },
      h('thead', {}, h('tr', {}, ['用户', 'Linux 账户', '角色', '状态', '最近登录'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, list.map((u) => h('tr', {
        class: 'selectable', 'aria-selected': String(!st.creating && u.id === sel?.id), tabindex: 0,
        onclick: () => { st.sel = u.id; st.temp = null; st.creating = false; draw(); },
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); st.sel = u.id; st.temp = null; st.creating = false; draw(); } },
      },
      h('td', {}, h('div', { style: { 'font-weight': 500 } }, u.display_name || u.username), h('div', { class: 'muted', style: { 'font-size': '13px' } }, u.username)),
      h('td', { class: 'tnum' }, u.linux_user ?? '—'),
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
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, 'Linux 账户'), inp('linux_user', 'zhang')),
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
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, 'Linux 账户'),
            h('input', { class: 'tnum', value: sel.linux_user ?? '', style: { width: '140px' }, onchange: (e) => act(PATHS.userLinux, { user_id: sel.id, linux_user: e.target.value.trim() }, () => { if (ctx.demo) sel.linux_user = e.target.value.trim(); }) })),
          h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, '管理员权限'),
            h('input', { type: 'checkbox', checked: sel.role === 'admin', style: { width: '20px', height: '20px' }, onchange: (e) => act(PATHS.userRole, { user_id: sel.id, role: e.target.checked ? 'admin' : 'member' }, () => { if (ctx.demo) sel.role = e.target.checked ? 'admin' : 'member'; }) }))),
        '管理员可添加用户、重置密码，并为任意成员代为注册模型服务。变更 Linux 账户后，该用户名下的模型服务将重新进行归属校验。'),
        section('密码', h('div', { class: 'group' },
          h('button', { type: 'button', class: 'cell action', onclick: () => act(PATHS.userReset, { user_id: sel.id }, (d) => { st.temp = d.temporary_password ?? null; }) }, '重置密码…'),
          st.temp ? h('div', { class: 'cell', style: { 'flex-direction': 'column', 'align-items': 'flex-start', gap: '4px' } },
            h('div', { class: 'muted', style: { 'font-size': '13px' } }, '临时密码'),
            h('div', { class: 'mono', style: { 'font-size': '19px', 'font-weight': 600 } }, st.temp),
            h('div', { class: 'muted', style: { 'font-size': '12px' } }, '该密码仅显示一次。用户下次登录时须设置新密码，现有会话已全部注销。')) : null)),
        h('div', { class: 'group' },
          h('button', { type: 'button', class: 'cell action danger', onclick: () => act(PATHS.userStatus, { user_id: sel.id, status: sel.status === 'active' ? 'disabled' : 'active' }, () => { if (ctx.demo) sel.status = sel.status === 'active' ? 'disabled' : 'active'; }) },
            sel.status === 'active' ? '停用用户…' : '恢复用户')));
    }
    root.append(h('div', { class: 'row', style: { gap: '24px', 'align-items': 'flex-start' } }, table, side));
  }
  draw();
  return root;
}

/* ============================== 扩展 ============================== */
const EXTENSIONS = [
  { key: 'strata', name: 'Strata', glyph: 'S', tint: '#1D1D1F', kind: '推理框架 · 默认', desc: '实验室默认推理框架，提供 OpenAI 兼容接口。', contrib: '推理适配器 · 模型服务详情面板 · 原生控制台' },
  { key: null, name: 'vLLM', glyph: 'v', tint: '#5856D6', kind: '推理框架', desc: '成员自行启动的 vLLM 服务经此适配器完成注册、健康检查与指标采集。', contrib: '推理适配器 · /metrics 指标映射' },
  { key: null, name: 'SGLang', glyph: 'G', tint: '#AF52DE', kind: '推理框架', desc: '支持 SGLang 服务的注册与指标采集。', contrib: '推理适配器' },
  { key: 'gateway', name: 'Gateway', glyph: 'W', tint: '#007AFF', kind: '网关', desc: '统一入口：校验 API 密钥、转发请求并记录请求元数据。', contrib: '请求日志 · 调用量统计' },
  { key: 'prometheus', name: 'Prometheus', glyph: 'P', tint: '#FF9500', kind: '指标数据源', desc: '采集 GPU、主机与各推理服务的指标，供「资源与用量」查询。', contrib: '数据源 · 资源与用量图表' },
  { key: 'grafana', name: 'Grafana', glyph: 'G', tint: '#FF6B2C', kind: '仪表盘 · 仅管理员', desc: '面向管理员的深度排查；门户内图表覆盖日常查看。', contrib: '「在 Grafana 中查看」链接' },
  { key: 'evalscope', name: 'EvalScope', glyph: 'E', tint: '#FF2D55', kind: '评测框架', desc: '对已注册的模型服务发起基准评测，结果在「模型评测」中展示。', contrib: '模型评测页面' },
];

export async function extensions(ctx) {
  let services = {}; let state = 'ready';
  if (ctx.demo) services = demoOverview().host.services;
  else { const r = await loadHost(); state = r.state; services = r.state === 'ready' ? (hostFacts(r.value)?.services ?? {}) : {}; }
  const statusOf = (key) => {
    if (!key) return { tone: 'off', text: '按服务注册时检测' };
    const v = services[key];
    if (v === true) return { tone: 'ok', text: '已连接' };
    if (v === false) return { tone: 'bad', text: '未响应' };
    return { tone: 'off', text: state === 'ready' ? '状态未知' : '未检测' };
  };
  return h('div', { class: 'page' },
    pageHead('扩展', '推理框架、监控与评测以扩展形式接入。每个扩展声明后端适配器与前端贡献点，启用后对应的页面与面板出现在门户中。'),
    h('div', { class: 'ext-grid' }, EXTENSIONS.map((e) => h('article', { class: 'ext' },
      h('div', { class: 'ext-head' },
        h('span', { class: 'ext-glyph', style: { background: e.tint }, 'aria-hidden': 'true' }, e.glyph),
        h('div', { style: { flex: '1 1 auto', 'min-width': 0 } }, h('h2', { class: 'ext-name' }, e.name), h('div', { class: 'muted', style: { 'font-size': '13px' } }, e.kind)),
        statusBadge(statusOf(e.key))),
      h('p', { style: { margin: 0, 'font-size': '13px' } }, e.desc),
      h('div', { class: 'ext-contrib' }, h('div', { class: 'muted' }, '贡献点'), h('div', {}, e.contrib))))),
    h('p', { class: 'muted', style: { margin: 0, 'padding-top': '20px', 'border-top': '1px solid var(--hairline)', 'font-size': '13px' } },
      '扩展的原生界面（如推理框架自带的控制台、Grafana 仪表盘、EvalScope 报告）经网关以同源方式代理，沿用门户的登录状态打开。'));
}

export { modelColorMap };
