/*! web/console/js/service.js — 单个推理服务的详情：运行参数、实时负载、网关发布。
 *  参数因引擎而异，表单完全由后端返回的参数表（schema）生成。 */
import { h, toast, put } from './dom.js';
import { fmtInt, fmtGiB } from './charts.js';
import { PATHS, loadData, loadOps, send, engineParams, engineParamsUpdate } from './data.js';
import { servicesFromOps, engineLabel } from './viewmodel.js';
import { demoServices, demoParams, demoGateway } from './demo.js';
import { SNAP_ENGINE_LIVE } from './snapshot.js';
import { emptyState, statusBadge, segmented, section, relTime, quietEmpty, metricCards } from './ui.js';

const PARAM_STATE = {
  applied: { tone: 'ok', text: '已生效' },
  pending: { tone: 'warn', text: '待重启生效' },
  unverifiable: { tone: 'off', text: '无法实测核对' },
  none: { tone: 'off', text: '未设置目标值' },
};

const fmtVal = (p, v) => {
  if (v == null || v === '') return '—';
  if (p.kind === 'bool') return v ? '开启' : '关闭';
  if (p.kind === 'int' && typeof v === 'number') return `${fmtInt(v)}${p.unit && p.unit !== 'tokens' ? ` ${p.unit}` : ''}`;
  return String(v);
};

/* ---------------- 运行参数 ---------------- */
function paramsView(ctx, data, onSaved) {
  const holder = h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '22px' } });
  const draft = {};
  for (const p of data.schema) draft[p.key] = data.desired[p.key] ?? data.observed.values?.[p.key] ?? p.default;
  const st = { msg: null, busy: false };
  const obs = data.observed.values ?? {};

  async function save() {
    st.busy = true; st.msg = null; draw();
    const values = {};
    for (const p of data.schema) {
      let v = draft[p.key];
      if (v === '' || v == null) continue;
      if (p.kind === 'int') v = Number.parseInt(v, 10);
      if (p.kind === 'float') v = Number.parseFloat(v);
      if ((p.kind === 'int' || p.kind === 'float') && (!Number.isFinite(v) || (p.min != null && v < p.min) || (p.max != null && v > p.max))) {
        st.busy = false; st.msg = `「${p.label}」需在 ${p.min} – ${p.max} 之间。`; draw(); return;
      }
      values[p.key] = v;
    }
    if (ctx.demo) {
      const flags = data.schema.filter((p) => values[p.key] !== undefined && (p.kind !== 'bool' || values[p.key])).map((p) => (p.kind === 'bool' ? p.flag : `${p.flag} ${values[p.key]}`));
      const pending = data.schema.some((p) => p.observable && obs[p.key] != null && values[p.key] !== obs[p.key]);
      onSaved({ ...data, desired: values, state: pending ? 'pending' : 'applied', command: `strata serve ${flags.join(' ')} --port 8080`, desired_updated_at: new Date().toISOString() });
      toast('已保存目标参数');
      return;
    }
    const r = await send(engineParamsUpdate(data.engine_id), { values });
    st.busy = false;
    if (!r.ok) { st.msg = r.message ?? (r.state === 'denied' ? '只有服务所有者或管理员可以修改参数。' : `保存失败（${r.reason}）`); draw(); return; }
    toast('已保存目标参数');
    onSaved(r.data);
  }

  function input(p) {
    const dis = !data.can_edit;
    const on = (v) => { draft[p.key] = v; drawHint(); };
    if (p.kind === 'bool') return h('label', { class: 'switch' }, h('input', { type: 'checkbox', role: 'switch', checked: Boolean(draft[p.key]), disabled: dis, onchange: (e) => on(e.target.checked) }), h('span', { 'aria-hidden': 'true' }));
    if (p.kind === 'choice') return h('select', { disabled: dis, onchange: (e) => on(e.target.value) }, p.choices.map((c) => h('option', { value: c, selected: c === draft[p.key] }, c)));
    if (p.kind === 'int' || p.kind === 'float') {
      return h('span', { style: { display: 'inline-flex', 'align-items': 'center', gap: '6px' } },
        h('input', { type: 'number', class: 'tnum', min: p.min ?? undefined, max: p.max ?? undefined, step: p.kind === 'float' ? 0.01 : 1, value: draft[p.key] ?? '', disabled: dis, style: { width: '112px', 'text-align': 'right' }, oninput: (e) => on(e.target.value) }),
        p.unit ? h('span', { class: 'muted', style: { 'font-size': '13px', 'min-width': '44px' } }, p.unit) : null);
    }
    return h('input', { value: draft[p.key] ?? '', disabled: dis, style: { width: '220px' }, oninput: (e) => on(e.target.value) });
  }

  // 「每路上下文」是 llama.cpp 系最容易踩的坑：总上下文被并发路数平分。
  const hint = h('div', { class: 'muted tnum', style: { 'font-size': '13px' } });
  function drawHint() {
    const ctxKey = data.schema.find((p) => p.key === 'ctx_size') ? 'ctx_size' : null;
    if (!ctxKey || !data.schema.find((p) => p.key === 'parallel')) { hint.textContent = ''; return; }
    const total = Number(draft.ctx_size); const n = Number(draft.parallel);
    hint.textContent = total > 0 && n > 0 ? `按当前填写：每路可用上下文 ≈ ${fmtInt(Math.floor(total / n))} tokens（${fmtInt(total)} ÷ ${n} 路）` : '';
  }

  function draw() {
    while (holder.firstChild) holder.firstChild.remove();
    const groups = [...new Set(data.schema.map((p) => p.group))];
    const live = data.observed.reachable;
    put(holder, 
      h('div', { class: 'param-status' },
        statusBadge(PARAM_STATE[data.state] ?? PARAM_STATE.none),
        h('span', { class: 'muted', style: { 'font-size': '13px' } },
          live === true ? '实测值读自运行中的服务' : live === false ? '服务当前无法访问，实测值暂缺' : '该引擎不提供只读参数接口',
          data.desired_updated_at ? ` · 目标值更新于 ${relTime(data.desired_updated_at)}` : '')),
      obs.ctx_per_slot != null ? metricCards([
        { label: '总上下文', tint: '#5856D6', glyph: 'memory', value: fmtInt(obs.ctx_size), unit: 'tokens', note: '实测' },
        { label: '并发路数', tint: '#34C759', glyph: 'services', value: fmtInt(obs.parallel), unit: '路', note: '实测 · 槽位数' },
        { label: '每路上下文', tint: '#FF9500', glyph: 'requests', value: fmtInt(obs.ctx_per_slot), unit: 'tokens', note: '单个请求最长可用' },
      ]) : null,
      ...groups.map((g) => section(g, h('div', { class: 'group' }, data.schema.filter((p) => p.group === g).map((p) => h('div', { class: 'cell param' },
        h('div', { class: 'grow' },
          h('div', { class: 'title' }, p.label, h('span', { class: 'mono muted', style: { 'margin-left': '8px', 'font-size': '11.5px', 'font-weight': 400 } }, p.flag)),
          p.help ? h('div', { class: 'sub' }, p.help) : null),
        p.observable ? h('span', { class: `observed${data.desired[p.key] != null && obs[p.key] != null && data.desired[p.key] !== obs[p.key] ? ' diff' : ''}`, title: '运行中实测值' }, `实测 ${fmtVal(p, obs[p.key])}`) : null,
        input(p)))))),
      hint,
      st.msg ? h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg) : null,
      data.can_edit ? h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn primary', disabled: st.busy, onclick: save }, st.busy ? '正在保存…' : '保存目标参数'))
        : h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } }, '只有服务所有者或管理员可以修改参数。'),
      data.command ? section('生效方式', h('div', { class: 'code' }, data.command),
        '平台不会替你重启进程。在运行这个服务的 Linux 用户下用上面的命令重启后，实测值与目标一致，状态会自动变成「已生效」。') : null);
    drawHint();
  }
  draw();
  return holder;
}

/* ---------------- 实时负载 ---------------- */
const SLOT_STATE = { idle: { tone: 'off', text: '空闲' }, processing: { tone: 'ok', text: '生成中' }, busy: { tone: 'ok', text: '生成中' } };

function liveView(live) {
  const L = live.live ?? {};
  const slots = L.slots ?? [];
  const maxCtx = live.engine?.max_context && live.engine?.parallel ? live.engine.max_context / live.engine.parallel : null;
  return h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '22px' } },
    metricCards([
      { label: '运行中', tint: '#34C759', glyph: 'services', value: fmtInt(L.running ?? 0), unit: `/ ${slots.length || live.engine?.parallel || '—'} 路`, note: L.queued ? `排队 ${L.queued}` : '无排队' },
      { label: '生成速度', tint: '#007AFF', glyph: 'usage', value: live.throughput?.tok_s == null ? '—' : live.throughput.tok_s.toFixed(1), unit: 'tok/s', note: live.recent?.length ? `最近一次 ${live.recent[0].decode_tok_s} tok/s` : '暂无请求' },
      { label: '累计请求', tint: '#AF52DE', glyph: 'requests', value: fmtInt(live.totals?.requests), note: live.totals?.since ? `自 ${relTime(live.totals.since)}` : ' ' },
      { label: '累计 Token', tint: '#FF9500', glyph: 'token', value: fmtInt((live.totals?.prompt_tokens ?? 0) + (live.totals?.output_tokens ?? 0)), note: `输出 ${fmtInt(live.totals?.output_tokens)}` },
    ]),
    section('并发槽位', h('div', { class: 'slots' }, slots.map((s) => h('div', { class: `slot ${s.state}` },
      h('div', { class: 'slot-head' }, h('span', { class: 'tnum' }, `#${s.index}`), statusBadge(SLOT_STATE[s.state] ?? { tone: 'off', text: s.state })),
      h('div', { class: 'meter' }, h('div', { style: { width: `${maxCtx ? Math.min(100, (s.held_tokens / maxCtx) * 100).toFixed(1) : 0}%` } })),
      h('div', { class: 'muted tnum', style: { 'font-size': '12px' } }, `占用 ${fmtInt(s.held_tokens)}${maxCtx ? ` / ${fmtInt(maxCtx)}` : ''} tokens`)))),
    '每个槽位同时只服务一个请求；槽位全满时新请求排队。'),
    live.recent?.length ? section('最近请求', h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['时间', '输入', '输出', '耗时', '生成速度', '结束'].map((t, i) => h('th', { scope: 'col', class: i && i < 5 ? 'num' : null }, t)))),
      h('tbody', {}, live.recent.map((r) => h('tr', {},
        h('td', { class: 'muted' }, relTime(r.at)), h('td', { class: 'num' }, fmtInt(r.prompt_tokens)), h('td', { class: 'num' }, fmtInt(r.output_tokens)),
        h('td', { class: 'num' }, `${r.duration_s} s`), h('td', { class: 'num' }, `${r.decode_tok_s} tok/s`), h('td', { class: 'muted' }, r.finish))))))) : null);
}

/* ---------------- 页面 ---------------- */
export async function detail(ctx, engineId) {
  const root = h('div', { class: 'page' });
  let svc = null; let params; let live = null; let route = null;
  if (ctx.demo) {
    svc = demoServices().find((s) => s.id === engineId) ?? null;
    params = { state: 'ready', value: demoParams() };
    live = SNAP_ENGINE_LIVE;
    route = demoGateway().routes.find((r) => r.engine_id === engineId) ?? null;
  } else {
    const [ops, p, gw] = await Promise.all([loadOps(), loadData(engineParams(engineId)), loadData(PATHS.gatewayModels)]);
    svc = ops.state === 'ready' ? (servicesFromOps(ops.value) ?? []).find((s) => s.id === engineId) ?? null : null;
    params = p;
    route = gw.state === 'ready' ? gw.value.routes.find((r) => r.engine_id === engineId) ?? null : null;
    // 实时负载目前只有 Strata 的只读代理（部署里没有这条路由就是 404 ⇒ 不画）。
    const framework = p.value?.framework ?? svc?.engine;
    if (framework === 'strata') {
      const l = await loadData(PATHS.engineLive);
      if (l.state === 'ready' && l.value.reachable) live = l.value;
    }
  }
  const name = svc?.label ?? params.value?.engine_name;
  if (!name) {
    put(root, h('a', { class: 'crumb', href: '#/services' }, '推理服务'), emptyState(params.state === 'ready' ? 'error' : params.state, { title: '找不到这个推理服务', body: '它可能已被删除，或没有共享给你。' }));
    return root;
  }
  const framework = params.value?.framework ?? svc?.engine;
  const tabs = [['运行参数', 'params'], live ? ['实时负载', 'live'] : null, ['网关', 'gateway']].filter(Boolean);
  let tab = 'params';
  const body = h('div', {});
  const drawTab = () => {
    while (body.firstChild) body.firstChild.remove();
    if (tab === 'params') {
      if (params.state !== 'ready') put(body, h('div', { class: 'group' }, quietEmpty(params.state, { title: '参数接口未启用', body: '本部署尚未提供推理服务参数管理。' })));
      else if (!params.value.supported) put(body, h('div', { class: 'group' }, quietEmpty('ready', { title: `${engineLabel(framework)} 暂无参数表`, body: '这个推理框架还没有声明可调参数。在服务登记时打上 framework 标签（vllm / sglang / llama.cpp / strata / ollama）即可启用。' })));
      else put(body, paramsView(ctx, params.value, (next) => { params = { state: 'ready', value: next }; drawTab(); }));
    } else if (tab === 'live') put(body, liveView(live));
    else {
      put(body, route
        ? h('div', { class: 'group' },
          h('div', { class: 'cell' }, h('span', { class: 'grow' }, '对外模型名'), h('code', { class: 'mono' }, route.model)),
          h('div', { class: 'cell' }, h('span', { class: 'grow' }, '共享范围'), route.visibility === 'all' ? '全实验室' : '仅自己'),
          h('div', { class: 'cell' }, h('span', { class: 'grow' }, '状态'), statusBadge(route.enabled ? { tone: 'ok', text: '可调用' } : { tone: 'off', text: '已暂停' })),
          h('a', { class: 'cell action', href: '#/gateway/models' }, '在「模型路由」中管理'))
        : h('div', { class: 'group' }, quietEmpty('ready', { title: '尚未发布到网关', body: '发布后，成员可以用自己的 API 密钥按模型名调用这个服务。' }),
          h('a', { class: 'cell action', href: '#/gateway/models' }, '去发布')));
    }
  };
  const seg = segmented('详情分区', tabs.map(([l]) => l), '运行参数', (l) => {
    tab = tabs.find(([x]) => x === l)[1];
    seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.textContent === l)));
    drawTab();
  });
  const facts = [engineLabel(framework), svc?.port ? `端口 ${svc.port}` : null, svc?.owner ? `所有者 ${svc.owner}` : null, svc?.runAs ? `运行用户 ${svc.runAs}` : null, svc?.vramMiB != null ? `显存 ${fmtGiB(svc.vramMiB)} GB` : null].filter(Boolean);
  put(root, 
    h('header', {},
      h('a', { class: 'crumb', href: '#/services' }, '推理服务'),
      h('div', { class: 'page-head', style: { 'margin-top': '6px' } },
        h('div', {}, h('h1', { class: 'page-title' }, name), h('p', { class: 'page-sub' }, facts.join(' · '))),
        svc ? statusBadge(svc.runtime) : null)),
    seg, body);
  drawTab();
  return root;
}

