import { pageScope } from './page-data.js';
import { h, put, clear } from './dom.js';
import { PATHS, loadData, send } from './data.js';
import { pageHead, section, statusBadge, quietEmpty, emptyState, relTime } from './ui.js';
import { createRefresher } from './live-refresh.js';
import { demoGateway } from './demo.js';

const API = '/api/evaluation/benchmarks';
const STATUS = { stopped: { tone: 'muted', text: '已停止' }, reconciled: { tone: 'muted', text: '已核对' }, running: { tone: 'warn', text: '评测中' }, completed: { tone: 'ok', text: '已完成' }, failed: { tone: 'bad', text: '失败' }, unknown: { tone: 'warn', text: '待核对' } };
const REASONS = { owner_confirmed_stopped: '所有者已确认 EvalScope 任务结束；平台没有获得结果摘要。', runner_completion_unknown: '与评测服务的连接中断，任务可能仍在执行。', platform_restarted: '平台重启，需在 EvalScope 核对执行结果。', runner_failed: '执行失败，请检查 EvalScope 与模型服务。', numeric_summary_unavailable: '执行已完成，但未返回可识别的数值摘要。' };

export async function evaluation(ctx) {
  const root = h('div', { class: 'page' });
  ctx = pageScope(ctx, root);
  let pending = null;
  const st = { runs: [], routes: [], runner: {}, state: 'ready', msg: null, busy: false, details: null,
    form: { model: '', kind: 'perf', number: 10, parallel: 1, max_tokens: 128, dataset: 'openqa' }, key: crypto.randomUUID() };
  function refresh() {
    if (ctx.signal.aborted) return Promise.resolve();
    if (!pending) pending = read().finally(() => { pending = null; });
    return pending;
  }
  async function read() {
    if (ctx.demo) { st.routes = demoGateway().routes.filter(r => r.owner_is_self); st.runner = { connected: true, perf: true, eval: true }; st.form.model ||= st.routes[0]?.model ?? ''; return; }
    const [runs, routes] = await Promise.all([loadData(API, { signal: ctx.signal }), loadData(PATHS.gatewayModels, { signal: ctx.signal })]);
    if (ctx.signal.aborted) return;
    st.state = runs.state;
    if (runs.state === 'ready') { st.runs = runs.value.runs ?? []; st.runner = runs.value.runner ?? {}; }
    if (routes.state === 'ready') st.routes = (routes.value.routes ?? []).filter(r => r.owner_is_self && r.enabled && !['offline', 'stale'].includes(r.engine_state));
    if (!st.form.model && st.routes.length) st.form.model = st.routes[0].model;
  }
  async function submit(event) {
    event.preventDefault();
    if (ctx.demo) { st.msg = '演示页面不执行评测。请登录后选择自己的服务。'; draw(); return; }
    st.busy = true; st.msg = null; draw();
    const r = await send(API + '/submit', { ...st.form, idempotency_key: st.key });
    st.busy = false;
    if (!r.ok) st.msg = r.reason === 'evaluation.runner_unavailable' ? 'EvalScope 执行接口尚未就绪。' : r.reason === 'evaluation.busy' ? '已有评测在执行，或有待核对的任务。' : r.reason === 'evaluation.service_offline' ? '服务已离线，请启动后重试。' : '提交失败，请检查服务状态后重试。';
    else { st.key = crypto.randomUUID(); await refresh(); }
    draw();
  }
  function form() {
    const f = st.form;
    const field = (label, input) => h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, label), input);
    const number = (key, max) => h('input', { type: 'number', min: 1, max, required: true, value: f[key], oninput: e => { f[key] = Number(e.target.value); }, style: { width: '100px' } });
    const ready = st.state === 'ready' && st.runner[f.kind] && st.routes.length;
    return h('form', { onsubmit: submit }, section('新建评测', h('div', { class: 'group' },
      field('我的服务', h('select', { required: true, value: f.model, onchange: e => { f.model = e.target.value; } },
        h('option', { value: '' }, '选择 API 服务'), st.routes.map(r => h('option', { value: r.model, selected: r.model === f.model }, r.model)))),
      field('评测类型', h('select', { onchange: e => { f.kind = e.target.value; f.dataset = f.kind === 'perf' ? 'openqa' : 'gsm8k'; draw(); } },
        h('option', { value: 'perf', selected: f.kind === 'perf' }, '性能 · 延迟与吞吐'), h('option', { value: 'eval', selected: f.kind === 'eval' }, '能力 · 数据集评分'))),
      field('数据集', h('select', { onchange: e => { f.dataset = e.target.value; } }, (f.kind === 'perf' ? ['openqa'] : ['gsm8k']).map(d => h('option', { value: d, selected: d === f.dataset }, d)))),
      field('样本数', number('number', 100)), field('并发数', number('parallel', 4)), field('最大输出 Token', number('max_tokens', 1024)),
      h('div', { class: 'cell', style: { 'justify-content': 'flex-end' } }, h('button', { type: 'submit', class: 'btn primary', disabled: st.busy || !ready }, st.busy ? '正在提交…' : '开始评测'))),
      '评测会向选中的服务发送请求并占用其推理资源。结果仅本人可见。'));
  }
  async function stop(run) {
    const r = await send(API + '/stop', { id: run.id });
    st.msg = r.ok ? null : '尚未确认停止，任务可能已经结束。请刷新后核对。';
    await refresh(); draw();
  }
  async function reconcile(run) {
    const r = await send(API + '/reconcile', { id: run.id, confirmed_stopped: true });
    st.msg = r.ok ? null : '核对失败，请刷新记录后重试。';
    await refresh(); draw();
  }
  function draw() {
    if (ctx.signal.aborted) return;
    put(clear(root), pageHead('模型评测', '测试本人服务的性能与能力，查看 EvalScope 真实结果。'),
      st.state !== 'ready' ? emptyState(st.state) : !st.runner[st.form.kind] ? h('div', { class: 'banner', role: 'status' }, h('strong', {}, '执行接口未就绪'),
        st.runner.connected ? 'EvalScope 可以连接，但没有开放所选评测接口。请在插件页查看接入状态。' : '暂时无法连接 EvalScope，请检查插件服务。', h('a', { href: '#/plugins' }, '查看插件')) : null,
      !st.routes.length ? quietEmpty('ready', { title: '还没有可评测的服务', body: '先在「API 接入」中添加自己的运行中服务。' }) : null,
      form(), st.msg ? h('p', { class: 'form-error', role: 'alert' }, st.msg) : null,
      section('评测记录', st.runs.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['服务', '类型 / 数据集', '样本 / 并发', '状态', '开始时间', '结果'].map(t => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, st.runs.map(run => h('tr', {},
          h('td', {}, run.spec.model), h('td', {}, `${run.spec.kind === 'perf' ? '性能' : '能力'} · ${run.spec.dataset}`),
          h('td', { class: 'tnum' }, `${run.spec.number} / ${run.spec.parallel}`),
          h('td', {}, statusBadge(STATUS[run.status] ?? STATUS.unknown), ['running', 'unknown'].includes(run.status) && st.runner.protocols?.[run.spec.kind] === 'invoke' ? h('button', { type: 'button', class: 'linkbtn danger', onclick: () => stop(run), 'aria-label': `停止 ${run.spec.model} 的评测` }, '停止') : null), h('td', {}, relTime(new Date(run.created_at * 1000).toISOString())),
          h('td', {}, h('button', { type: 'button', class: 'linkbtn', onclick: () => { st.details = st.details === run.id ? null : run.id; draw(); } }, '查看')))))))
        : h('div', { class: 'group' }, quietEmpty(st.state, { title: '还没有评测记录', body: '选择自己的服务，创建第一次评测。' }))),
      h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn', onclick: async () => { await refresh(); draw(); } }, '刷新记录')));
    const run = st.runs.find(r => r.id === st.details);
    if (run) put(root, section('结果详情', h('div', { class: 'group' },
      run.reason ? h('div', { class: 'cell' }, REASONS[run.reason] || '暂无结果摘要。') : null,
      run.status === 'unknown' ? h('div', { class: 'cell' }, h('button', { type: 'button', class: 'btn', onclick: () => reconcile(run) }, '已在 EvalScope 核对任务结束')) : null,
      Object.entries(run.metrics).map(([label, value]) => h('div', { class: 'cell' }, h('span', { class: 'grow mono' }, label), h('span', { class: 'tnum' }, Number.isInteger(value) ? value : Number(value.toFixed(4))))))));
  }
  put(root, pageHead('模型评测', '测试本人服务的性能与能力。'), h('p', { class: 'muted' }, '正在读取…'));
  refresh().then(draw);
  const ticker = createRefresher({ intervalMs: 5000, isAttached: () => root.isConnected, run: async () => {
    if (st.runs.some(r => r.status === 'running') && !st.busy && !root.querySelector('form')?.contains(document.activeElement)) { await refresh(); draw(); }
  } });
  if (!ctx.demo) ticker.start();
  ctx.onDispose(() => ticker.stop());
  return root;
}
