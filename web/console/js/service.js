/*! web/console/js/service.js — 单个推理服务的详情：运行参数、实时负载、网关发布。
 *  参数因引擎而异，表单完全由后端返回的参数表（schema）生成。 */
import { h, toast, put, clear } from './dom.js';
import { fmtInt, fmtGiB } from './charts.js';
import {
  PATHS, loadData, loadEngineLive, send, engineParams, engineParamsUpdate, engineServe, engineVerify, agentTaskPath,
} from './data.js';
import { pageScope, readOps } from './page-data.js';
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
      toast('已保存目标参数，服务没有重启');
      return;
    }
    const r = await send(engineParamsUpdate(data.engine_id), { values });
    if (ctx.signal?.aborted) return;
    st.busy = false;
    if (!r.ok) { st.msg = r.message ?? (r.state === 'denied' ? '只有服务所有者可以修改参数。' : `保存失败（${r.reason}）`); draw(); return; }
    toast('已保存目标参数，服务没有重启');
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
      data.can_edit ? h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn primary', disabled: st.busy, onclick: save }, st.busy ? '正在保存…' : '保存目标参数'),
        // 保存只写目标值。一句话写在按钮旁边，比事后解释为什么参数没生效有用。
        h('span', { class: 'muted', style: { 'font-size': '13px' } }, '只记录目标值，不会重启服务；重启后才按新参数运行。'))
        : h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } }, '只有服务所有者可以修改参数。'),
      data.command ? section('启动参数参考', h('div', { class: 'code' }, data.command),
        '将这些参数合并到原有启动配置，保留模型路径等必需项，由本人重启服务。刷新后核对实测值。') : null);
    drawHint();
  }
  draw();
  return holder;
}

/* ---------------- 服务控制：把任务下发给本人的代理 ---------------- */

/** 任务状态 -> 用户该看到的那句话。
 *
 *  这里最要紧的一条：**「已下发」不是「已启动」**。平台能把任务排进代理的队列，
 *  但进程到底起没起来只有代理回报才算数。所以 done 之前一律不说「已启动」，
 *  failed 也照实说失败——一个停在 queued 的任务，说明代理没在跑，不是服务起好了。 */
const TASK_STATE = {
  queued: { tone: 'warn', text: '已下发给你的代理，等待领取' },
  running: { tone: 'warn', text: '代理正在执行' },
  done: { tone: 'ok', text: '代理已执行' },
  failed: { tone: 'bad', text: '代理执行失败' },
};

const CONTROL_ACTIONS = [['start', '启动'], ['stop', '停止'], ['restart', '重启']];

/** 代理回报的 result 里挑一句人话；挑了就没有，不硬编。 */
function resultText(result) {
  if (!result || typeof result !== 'object') return typeof result === 'string' ? result : '';
  return [result.detail, result.message, result.reason].find((v) => typeof v === 'string' && v) ?? '';
}

const sleep = (ms) => new Promise((settle) => setTimeout(settle, ms));

function controlView(ctx, engineId, onSettled) {
  const holder = h('div', {});
  // generation 是每一次下发的代号：上一次的轮询醒来时发现自己过期就退出，
  // 否则连点两次按钮时，旧任务的回报会盖掉新任务的状态。
  const st = { busy: false, task: null, msg: null, note: null, generation: 0, probing: false, probe: null };

  function draw() {
    const badge = st.task ? (TASK_STATE[st.task.state] ?? { tone: 'off', text: String(st.task.state) }) : null;
    const detail = st.task ? resultText(st.task.result) : '';
    // put 只追加不清空：每次重画都必须先清，否则状态会一层层叠上去。
    // section 只收 (标题, 内容, 脚注) —— 状态、报错、说明都得放进内容里，多传的参数会被丢掉。
    put(clear(holder), section('服务控制', h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '10px' } },
      h('div', { class: 'actions', style: { 'flex-wrap': 'wrap' } },
        CONTROL_ACTIONS.map(([kind, label]) => h('button', {
          type: 'button',
          class: 'btn',
          disabled: st.busy,
          onclick: () => dispatch(kind),
        }, label))),
      badge ? h('div', {}, statusBadge(badge),
        detail ? h('span', { class: 'muted', style: { 'margin-left': '8px', 'font-size': '13px' } }, detail) : null) : null,
      st.msg ? h('p', { class: 'form-error', role: 'alert', style: { margin: 0, 'text-align': 'left' } }, st.msg) : null,
      st.note ? h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } }, st.note) : null,
      h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } },
        '平台只把任务下发给你的代理，不代你执行：进程由你本机运行的 llmlab agent --watch 以你自己的 Linux 身份启动。'),
      // 探测与上面三个按钮不是一回事：那几个是「请代理去做」，这个是「平台现在测一次」。
      // 列表每 15 秒重读一次，但登记的探测结论不会自己变新——要新结论就得真探一次。
      h('div', { class: 'actions', style: { 'flex-wrap': 'wrap', 'align-items': 'center' } },
        h('button', { type: 'button', class: 'btn', disabled: st.probing || st.busy, onclick: probe }, st.probing ? '正在探测…' : '现在探测一次'),
        st.probe ? statusBadge(st.probe) : null,
        h('span', { class: 'muted', style: { 'font-size': '13px' } },
          '由平台从本机立刻探一次这个端口，几秒内返回并写回登记；只更新状态与健康，不会启动服务。')))));
  }

  /** 现在探测一次：同步结果，回来后重读一次概览，让页头与列表跟上。 */
  async function probe() {
    if (st.probing) return;
    const generation = ++st.generation;
    st.probing = true; st.probe = null; st.msg = null; st.note = null; draw();
    if (ctx.demo) {
      st.probing = false;
      st.probe = { tone: 'off', text: '演示数据：不会真的探测' };
      draw();
      return;
    }
    const r = await send(engineVerify(engineId), {});
    if (ctx.signal?.aborted || generation !== st.generation) return;
    st.probing = false;
    if (!r.ok) {
      st.msg = r.message ?? (r.state === 'denied'
        ? '只有服务所有者可以触发探测。'
        : `探测没有执行（${r.reason}）`);
      draw();
      return;
    }
    const data = r.data ?? {};
    // Registry 的 verification.status 是 VERIFIED / FAILED / NOT_CONFIGURED 三种大写取值，
    // 顶层的 status 是小写。统一成大写再比，免得大小写把「没测」读成「测通了」。
    const verdict = String(data.verification?.status ?? data.status ?? '').toUpperCase();
    const notes = Array.isArray(data.notes) ? data.notes.filter((n) => typeof n === 'string' && n) : [];
    st.probe = verdict === 'NOT_CONFIGURED'
      // 没有观测源时 verify_engine 不改状态：「没测」不能说成「测过、没问题」。
      ? { tone: 'off', text: '平台没有配置探测源，这次没有测' }
      : { tone: verdict === 'VERIFIED' ? 'ok' : 'bad', text: verdict === 'VERIFIED' ? '刚探测过：有了新的结论' : '刚探测过：没探通' };
    st.note = notes.join(' ') || null;
    draw();
    onSettled?.();
  }

  async function dispatch(kind) {
    if (st.busy) return;
    const generation = ++st.generation;
    st.busy = true; st.msg = null; st.task = null; draw();
    if (ctx.demo) {
      st.busy = false;
      st.msg = '演示数据：这里不会真的下发任务。';
      draw();
      return;
    }
    const r = await send(engineServe(engineId), { kind });
    if (ctx.signal?.aborted || generation !== st.generation) return;
    st.busy = false;
    if (!r.ok) {
      st.msg = r.message ?? (r.state === 'denied'
        ? '这个服务没有可用的本人代理：平台没有记录它运行在哪个 Linux 用户下，或者你不是它的所有者。'
        : `下发失败（${r.reason}）`);
      draw();
      return;
    }
    st.task = r.data; draw();
    poll(generation, r.data.id);
  }

  async function poll(generation, id) {
    for (let i = 0; i < 30; i += 1) {
      await sleep(2000);
      if (ctx.signal?.aborted || generation !== st.generation) return;
      const r = await loadData(agentTaskPath(id), { signal: ctx.signal });
      if (ctx.signal?.aborted || generation !== st.generation) return;
      if (r.state !== 'ready') continue;
      st.task = r.value; draw();
      if (st.task.state === 'done' || st.task.state === 'failed') {
        onSettled?.();
        return;
      }
    }
    // 一分钟还没有结果：多半是代理没在跑。平台这边没有任何办法替它执行。
    st.msg = '还没有收到执行结果。平台只负责下发，执行由你本机的代理完成——确认 llmlab agent --watch 正在运行。';
    draw();
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
      { label: '运行中', tint: '#34C759', glyph: 'services', value: fmtInt(L.running), unit: `/ ${slots.length || live.engine?.parallel || '—'} 路`, note: L.queued == null ? '排队数未上报' : `排队 ${L.queued}` },
      { label: '生成速度', tint: '#007AFF', glyph: 'usage', value: live.throughput?.tok_s == null ? '—' : live.throughput.tok_s.toFixed(1), unit: 'tok/s', note: live.recent?.length ? `最近一次 ${live.recent[0].decode_tok_s} tok/s` : '暂无请求' },
      { label: '累计请求', tint: '#AF52DE', glyph: 'requests', value: fmtInt(live.totals?.requests), note: live.totals?.since ? `自 ${relTime(live.totals.since)}` : ' ' },
      { label: '累计 Token', tint: '#FF9500', glyph: 'token', value: live.totals?.prompt_tokens == null || live.totals?.output_tokens == null ? '—' : fmtInt(live.totals.prompt_tokens + live.totals.output_tokens), note: `输出 ${fmtInt(live.totals?.output_tokens)}` },
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
  ctx = pageScope(ctx, root);
  let svc = null, params = null, opsDone = false, live = null, route = null;
  let tab = 'params', liveBusy = false, gatewayLoaded = false;
  // 只建一次：重新绘制参数区时把它挪回去，轮询状态才不会丢。
  let control = null;
  const title = h('h1', { class: 'page-title' }, '推理服务');
  const facts = h('p', { class: 'page-sub' }, '正在读取…');
  const badge = h('div', {}), tabsSlot = h('div', {});
  const panes = Object.fromEntries(['params', 'live', 'gateway'].map(key => [key, h('div', { hidden: key !== tab }, h('p', { class: 'muted' }, '正在读取…'))]));
  put(root, h('header', {}, h('a', { class: 'crumb', href: '#/services' }, '推理服务'),
    h('div', { class: 'page-head', style: { 'margin-top': '6px' } }, h('div', {}, title, facts), badge)), tabsSlot, ...Object.values(panes));
  const framework = () => params?.value?.framework ?? svc?.engine;
  let tabsKey = null;
  function drawHeader() {
    if (ctx.signal.aborted) return;
    title.textContent = svc?.label ?? params?.value?.engine_name ?? '推理服务';
    facts.textContent = [engineLabel(framework()), svc?.port ? `端口 ${svc.port}` : null, svc?.owner ? `所有者 ${svc.owner}` : null,
      svc?.runAs ? `运行用户 ${svc.runAs}` : null, svc?.vramMiB != null ? `显存 ${fmtGiB(svc.vramMiB)} GB` : null].filter(Boolean).join(' · ');
    clear(badge); if (svc) put(badge, statusBadge(svc.runtime));
    const tabs = [['运行参数', 'params'], ...(['strata', 'vllm', 'sglang'].includes(framework()) ? [['实时负载', 'live']] : []), ['API 接入', 'gateway']];
    const key = JSON.stringify(tabs);
    if (tabsKey !== key) {
      tabsKey = key;
      const seg = segmented('详情分区', tabs.map(([label]) => label), tabs.find(([, id]) => id === tab)?.[0] ?? '运行参数', label => {
        tab = tabs.find(([name]) => name === label)[1];
        seg.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.textContent === label)));
        for (const [id, pane] of Object.entries(panes)) pane.hidden = id !== tab;
        if (tab === 'live' && !live) refreshLive();
        if (tab === 'gateway' && !gatewayLoaded) refreshGateway();
      });
      put(clear(tabsSlot), seg);
    }
  }
  /** 任务执行完之后再读一次运行概览，让页头的状态跟上代理回报的结果。 */
  function readService(force = false) {
    readOps(ctx, force ? { force: true } : {}).then(r => {
      if (ctx.signal.aborted) return;
      opsDone = true;
      svc = r.state === 'ready' ? (servicesFromOps(r.value) ?? []).find(s => s.id === engineId) : null;
      drawHeader();
      if (params && !params.value?.engine_name) drawParams();
    });
  }
  /** 引擎行在，才谈得上控制：它同时证明了这条服务存在、归属，以及运行在哪个 uid 下。 */
  function controlPane() {
    if (!control) control = controlView(ctx, engineId, () => readService(true));
    return control;
  }
  function drawParams() {
    if (ctx.signal.aborted || !params) return;
    const pane = clear(panes.params);
    if (!svc && !params.value?.engine_name && opsDone) put(pane, emptyState(params.state === 'ready' ? 'error' : params.state, { title: '找不到这个推理服务', body: '它可能已被删除，或不属于当前账户。' }));
    else if (params.state !== 'ready') put(pane, h('div', { class: 'group' }, quietEmpty(params.state, { title: '参数暂不可用', body: '本次未能取得运行参数，请稍后重新进入。' })));
    else put(pane, controlPane(),
      params.value.supported
        ? paramsView(ctx, params.value, next => { params = { state: 'ready', value: next }; drawParams(); })
        : h('div', { class: 'group' }, quietEmpty('ready', { title: `${engineLabel(framework())} 暂无参数表`, body: '这个推理引擎还没有声明可调参数。' })));
  }
  async function refreshLive() {
    if (liveBusy || ctx.signal.aborted) return;
    liveBusy = true;
    const pane = panes.live;
    const button = pane.querySelector?.('button'); if (button) button.disabled = true;
    const r = ctx.demo ? { state: 'ready', value: SNAP_ENGINE_LIVE } : await loadEngineLive({ engineId, signal: ctx.signal });
    if (ctx.signal.aborted) return;
    liveBusy = false; live = r.value ?? { reachable: false, reason: r.reason };
    const reasons = { unauthorized: '引擎要求认证，请配置该服务的指标凭据。', metrics_not_enabled: '引擎尚未开放 /metrics，请在引擎配置中启用指标。', service_offline: '该服务已离线。', unreachable: '暂时无法连接该服务。', bad_payload: '引擎返回的指标格式无法识别。' };
    put(clear(pane), live.reachable ? liveView(live) : quietEmpty(r.state, { title: '暂无服务指标', body: reasons[live.reason] || '这次未能取得指标，稍后可以刷新。' }),
      h('button', { type: 'button', class: 'btn', onclick: refreshLive }, '刷新指标'));
  }
  async function refreshGateway() {
    gatewayLoaded = true;
    const r = ctx.demo ? { state: 'ready', value: demoGateway() } : await loadData(PATHS.gatewayModels, { signal: ctx.signal });
    if (ctx.signal.aborted) return;
    route = r.value?.routes?.find(item => item.engine_id === engineId) ?? null;
    const pane = clear(panes.gateway);
    if (r.state !== 'ready') {
      gatewayLoaded = false;
      put(pane, emptyState(r.state), h('button', { type: 'button', class: 'btn', onclick: refreshGateway }, '重试'));
    } else put(pane, route ? h('div', { class: 'group' },
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '对外模型名'), h('code', { class: 'mono' }, route.model)),
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '使用范围'), '仅本人'),
      h('div', { class: 'cell' }, h('span', { class: 'grow' }, '状态'), statusBadge(route.enabled ? { tone: 'ok', text: '可调用' } : { tone: 'off', text: '已暂停' })),
      h('a', { class: 'cell action', href: '#/gateway/models' }, '管理 API 接入'), h('a', { class: 'cell action', href: '#/gateway/playground' }, '测试 API'))
      : h('div', { class: 'group' }, quietEmpty('ready', { title: '尚未创建 API 接入', body: '为自己的服务设置 API 名称，随后可以测试并查看调用记录。' }), h('a', { class: 'cell action', href: '#/gateway/models' }, '创建 API 接入')));
  }
  drawHeader();
  if (ctx.demo) {
    svc = demoServices().find(s => s.id === engineId) ?? null;
    params = { state: 'ready', value: demoParams() }; opsDone = true; drawHeader(); drawParams();
  } else {
    const cached = ctx.opsCache?.peek();
    if (cached) { svc = (servicesFromOps(cached.value) ?? []).find(s => s.id === engineId); drawHeader(); }
    readService();
    loadData(engineParams(engineId), { signal: ctx.signal }).then(r => { if (ctx.signal.aborted) return; params = r; drawHeader(); drawParams(); });
  }
  return root;
}
