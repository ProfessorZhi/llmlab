/*! web/console/js/gateway.js — 「网关」一栏：同源 /v1 的 OpenAI 兼容网关。
 *  概览、模型路由、API 密钥、调用日志、在线体验。每人用自己的密钥、看自己的调用；
 *  服务所有者能看到谁调用了自己的服务（只有计数，没有内容）；管理员可切到全实验室。 */
import { h, toast, put } from './dom.js';
import { fmtInt, colorAt } from './charts.js';
import {
  PATHS, loadData, loadOps, send, gatewayKeyRevoke, gatewayKeyUpdate, gatewayModelUpdate, gatewayLogDetail,
} from './data.js';
import { servicesFromOps, engineLabel, modelColorMap } from './viewmodel.js';
import { demoGateway, demoServices } from './demo.js';
import {
  pageHead, emptyState, statusBadge, segmented, section, relTime, clock,
  sunburstPanel, hbarList, quietEmpty, metricCards, windowBars, donutPanel, heatmap,
} from './ui.js';

const panel = (cls, title, sub, ...body) => h('section', { class: `panel ${cls}` },
  h('div', { class: 'panel-head' }, h('div', {}, h('h2', { class: 'panel-title' }, title), sub ? h('p', { class: 'panel-sub' }, sub) : null)),
  ...body);

/* 演示模式下的可变状态：创建 / 撤销 / 发布在本页内生效，刷新即还原。 */
let demoState = null;
const demo = () => (demoState ??= (() => { const g = demoGateway(); return { ...g, keys: g.keys.map((k) => ({ ...k })), routes: g.routes.map((r) => ({ ...r })) }; })());

const baseUrl = (fromServer) => fromServer || `${location.origin}/v1`;
const WINDOWS = [['24 小时', '24h'], ['7 天', '7d'], ['30 天', '30d']];
const AXIS = {
  '24h': ['24 小时前', '18 小时前', '12 小时前', '6 小时前', '现在'],
  '7d': ['7 天前', '5 天前', '3 天前', '昨天', '今天'],
  '30d': ['30 天前', '3 周前', '2 周前', '1 周前', '今天'],
};
const ms = (v) => (v == null ? '—' : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
const pct = (v) => (v == null ? '—' : `${(v * 100).toFixed(v >= 0.995 || v === 0 ? 0 : 1)}%`);
const tokens = (n) => (n == null ? '—' : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}K` : fmtInt(n));

const NO_CALLS = { title: '还没有经过网关的调用', body: '创建一把 API 密钥，用 OpenAI SDK 指向本站的 /v1 即可开始；在线体验里的对话也会记在这里。' };

function viewToggle(ctx, current, onPick) {
  if (!ctx.isAdmin()) return null;
  const labels = { mine: '我的', all: '全实验室' };
  return segmented('查看范围', ['我的', '全实验室'], labels[current], (v) => onPick(v === '我的' ? 'mine' : 'all'));
}

function failText(r, verb) {
  if (r.state === 'signed_out') return '会话已失效，请重新登录。';
  if (r.state === 'not_configured') return '本部署尚未启用网关。';
  if (r.status === 409) return '这个模型名已经被发布了，换一个名字。';
  if (r.status === 429) return r.message ?? '数量已达上限。';
  if (r.state === 'denied' || r.status === 404) return r.message ?? '没有权限执行这个操作。';
  return r.message ?? `${verb}失败（${r.reason ?? r.status ?? '未知原因'}）`;
}

/* ============================== 网关概览 ============================== */
export async function overview(ctx) {
  const st = { window: '24h', view: 'mine' };
  const root = h('div', { class: 'page' });

  async function load() {
    if (ctx.demo) return { state: 'ready', value: demo().overview(st.window, st.view) };
    const q = new URLSearchParams({ window: st.window, ...(st.view === 'all' ? { view: 'all' } : {}) });
    return loadData(`${PATHS.gatewayOverview}?${q}`);
  }

  async function draw() {
    const r = await load();
    while (root.firstChild) root.firstChild.remove();
    const seg = segmented('时间范围', WINDOWS.map(([l]) => l), WINDOWS.find(([, v]) => v === st.window)[0], (l) => { st.window = WINDOWS.find(([x]) => x === l)[1]; draw(); });
    put(root, pageHead('网关', '实验室统一的 OpenAI 兼容入口。所有调用经网关转发到各推理服务，引擎自身的密钥只留在服务端。',
      viewToggle(ctx, st.view, (v) => { st.view = v; draw(); }), seg));
    if (r.state !== 'ready') { put(root, h('div', { class: 'group' }, quietEmpty(r.state, { title: '网关未启用', body: '本部署尚未接入网关。' }))); return; }
    const o = r.value;
    const c = o.calls;
    const totalTok = (c.prompt_tokens ?? 0) + (c.completion_tokens ?? 0);
    put(root, metricCards([
      { label: '请求数', tint: '#007AFF', glyph: 'requests', value: fmtInt(c.requests), note: o.view === 'all' ? '全实验室' : '我发起的' },
      { label: 'Tokens', tint: '#AF52DE', glyph: 'token', value: c.requests ? tokens(totalTok) : '—', note: c.requests ? `输入 ${tokens(c.prompt_tokens)} · 输出 ${tokens(c.completion_tokens)}` : '暂无调用' },
      { label: '成功率', tint: '#34C759', glyph: 'check', value: pct(c.success_rate), note: c.requests ? `${fmtInt(Math.round(c.requests * (1 - (c.success_rate ?? 1))))} 次失败` : '暂无调用' },
      { label: 'P95 延迟', tint: '#FF9500', glyph: 'timer', value: ms(c.latency_p95_ms), note: c.ttft_p50_ms != null ? `首字 P50 ${ms(c.ttft_p50_ms)}` : `P50 ${ms(c.latency_p50_ms)}` },
    ]));
    put(root, h('div', { class: 'gw-strip' },
      h('div', {}, h('span', { class: 'muted' }, '接入地址'), h('code', { class: 'mono' }, baseUrl(o.base_url))),
      h('div', {}, h('span', { class: 'muted' }, '已发布模型'), h('a', { href: '#/gateway/models' }, `${fmtInt(o.routes)} 个`)),
      h('div', {}, h('span', { class: 'muted' }, o.view === 'all' ? '有效密钥（全部）' : '我的有效密钥'), h('a', { href: '#/gateway/keys' }, `${fmtInt(o.keys_active)} 把`)),
      h('div', {}, h('span', { class: 'muted' }, '进行中'), h('span', { class: 'tnum' }, `${fmtInt(o.inflight)} 个请求`))));

    if (!c.requests) {
      put(root, h('div', { class: 'group' }, quietEmpty('ready', NO_CALLS)),
        h('div', { class: 'actions', style: { 'justify-content': 'flex-start' } },
          h('a', { class: 'btn primary', href: '#/gateway/keys' }, '创建 API 密钥'), h('a', { class: 'btn', href: '#/gateway/playground' }, '在线体验')));
    } else {
      const models = c.by_model.map((m) => m.model);
      const colors = modelColorMap(models);
      const groups = c.by_caller.map((x) => ({ id: x.caller, label: x.name, children: c.by_caller_model.filter((p) => p.caller === x.caller).map((p) => ({ id: p.model, label: p.model, value: p.requests })) }));
      put(root, 
        h('div', { class: 'row' },
          panel('grow', '请求趋势', `每格 ${st.window === '24h' ? '1 小时' : '1 天'} · 峰值高亮`, windowBars(c.series.requests, { labels: AXIS[st.window] })),
          panel('side', '各模型调用量', null, h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
            ...hbarList(c.by_model.map((m) => ({ id: m.model, label: m.model, value: m.requests })), colors)))),
        groups.length > 0 ? panel('', '谁在调用哪个模型', '内环为调用者，外环为其调用的模型；点图例聚焦', sunburstPanel({ innerLabel: '调用者', groups }, colors)) : null,
        h('div', { class: 'row' },
          c.by_key ? panel('grow', '按密钥', '每把 API 密钥（含在线体验）的请求占比', donutPanel(c.by_key.map((k, i) => ({
            id: k.key_id ?? `pg:${k.caller}`, label: o.view === 'all' ? `${k.label} · ${k.caller_name}` : k.label, value: k.requests, color: colorAt(i),
          })), { title: '请求' })) : null,
          c.outcomes ? panel('side', '结果', null, donutPanel([
            { id: 'success', label: '成功', value: c.outcomes.success ?? 0, color: 'var(--green)' },
            { id: 'failed', label: '失败', value: c.outcomes.failed ?? 0, color: 'var(--red)' },
            { id: 'cancelled', label: '客户端取消', value: c.outcomes.cancelled ?? 0, color: 'var(--orange)' },
          ], { title: '请求', size: 180 })) : null),
        h('div', { class: 'row' },
          panel('grow', 'Token 去向', '各模型消耗的 Token（输入 + 输出）', donutPanel(c.by_model.map((m) => ({ id: m.model, label: m.model, value: m.tokens, color: colors.get(m.model) ?? colorAt(0) })), { title: 'Tokens', fmt: tokens })),
          c.heatmap ? panel('side', '调用热度', '星期 × 小时（服务器时间）', heatmap(c.heatmap)) : null));
    }
    const mine = o.my_services;
    if (mine) {
      put(root, panel('', '我的服务被谁调用', '经网关调用你发布的模型的成员。只统计次数与 Token，不记录内容。',
        mine.by_caller.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', {}, h('tr', {}, ['调用者', '请求', 'Tokens', '占比'].map((t, i) => h('th', { scope: 'col', class: i ? 'num' : null }, t)))),
          h('tbody', {}, mine.by_caller.map((x, i) => h('tr', {},
            h('td', {}, h('span', { class: 'swatch', style: { background: colorAt(i), 'border-radius': '50%', display: 'inline-block', width: '8px', height: '8px', 'margin-right': '8px' } }), x.name),
            h('td', { class: 'num' }, fmtInt(x.requests)), h('td', { class: 'num' }, tokens(x.tokens)),
            h('td', { class: 'num muted' }, pct(x.requests / mine.requests)))))))
          : quietEmpty('ready', { title: '这段时间没有人调用你的服务', body: '模型发布并共享给全实验室后，成员的调用会显示在这里。' })));
    }
  }
  await draw();
  return root;
}

/* ============================== 模型路由 ============================== */
const VIS = { all: '全实验室', private: '仅自己' };

function snippets(base, model) {
  const m = model ?? '<模型名>';
  return {
    curl: `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer $LLMLAB_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "${m}", "messages": [{"role": "user", "content": "你好"}]}'`,
    python: `import os\nfrom openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key=os.environ["LLMLAB_API_KEY"])\nresp = client.chat.completions.create(\n    model="${m}",\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(resp.choices[0].message.content)`,
  };
}

function snippetPanel(base, model) {
  let lang = 'curl';
  const code = h('div', { class: 'code' });
  const set = () => { code.textContent = snippets(base, model)[lang]; };
  set();
  const seg = segmented('示例语言', ['curl', 'Python'], 'curl', (v) => { lang = v === 'curl' ? 'curl' : 'python'; seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b.textContent === v))); set(); });
  return panel('', '接入示例', `接入地址 ${base} · 与 OpenAI SDK 兼容`, h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '12px' } }, seg, code));
}

async function ownEngines(ctx) {
  if (ctx.demo) return demoServices().filter((s) => s.kind === 'engine');
  const r = await loadOps();
  if (r.state !== 'ready') return [];
  return (servicesFromOps(r.value) ?? []).filter((s) => s.kind === 'engine' && (s.self || ctx.isAdmin()));
}

export async function models(ctx) {
  const root = h('div', { class: 'page' });
  const st = { routes: null, base: null, state: 'ready', engines: await ownEngines(ctx), form: { engine_id: '', model: '', visibility: 'all' }, msg: null, busy: false };

  async function refresh() {
    if (ctx.demo) { st.routes = demo().routes; st.base = null; return; }
    const r = await loadData(PATHS.gatewayModels);
    st.state = r.state;
    st.routes = r.state === 'ready' ? r.value.routes : null;
    st.base = r.value?.base_url ?? null;
  }

  async function update(route, patch) {
    if (patch.delete && !globalThis.confirm?.(`下线「${route.model}」后，用这个名字的调用会立刻失败。确定下线？`)) return;
    if (ctx.demo) {
      const list = demo().routes;
      const i = list.findIndex((x) => x.route_id === route.route_id);
      if (patch.delete) list.splice(i, 1); else Object.assign(list[i], patch);
    } else {
      const r = await send(gatewayModelUpdate(route.route_id), patch);
      if (!r.ok) { toast(failText(r, '修改')); return; }
    }
    await refresh(); draw();
  }

  async function publish() {
    const f = st.form;
    if (!f.engine_id) { st.msg = '先选择要发布的推理服务。'; draw(); return; }
    if (f.model && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(f.model)) { st.msg = '模型名只能包含字母、数字和 . _ : / -'; draw(); return; }
    st.busy = true; st.msg = null; draw();
    if (ctx.demo) {
      const eng = st.engines.find((e) => e.id === f.engine_id);
      const alias = f.model || eng.label;
      if (demo().routes.some((r) => r.model === alias)) { st.busy = false; st.msg = '这个模型名已经被发布了，换一个名字。'; draw(); return; }
      demo().routes.push({ route_id: `rt_${Date.now()}`, model: alias, engine_id: eng.id, engine_name: eng.label, engine_status: 'verified', framework: eng.engine, upstream_model: null, owner: 'account:fjd', owner_name: eng.owner, owner_is_self: true, visibility: f.visibility, enabled: true, upstream_credential: false, created_at: new Date().toISOString() });
    } else {
      const r = await send(PATHS.gatewayPublish, { engine_id: f.engine_id, model: f.model || undefined, visibility: f.visibility });
      if (!r.ok) { st.busy = false; st.msg = failText(r, '发布'); draw(); return; }
    }
    st.busy = false; st.form = { engine_id: '', model: '', visibility: 'all' };
    toast('已发布到网关');
    await refresh(); draw();
  }

  function routesTable() {
    if (!st.routes) return h('div', { class: 'group' }, quietEmpty(st.state, { title: '网关未启用', body: '本部署尚未接入网关。' }));
    if (!st.routes.length) return h('div', { class: 'group' }, quietEmpty('ready', { title: '还没有发布的模型', body: '把自己登记的推理服务发布到网关，成员就能用自己的密钥通过统一地址调用它。' }));
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['模型名', '推理服务', '所有者', '共享范围', '状态', ''].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, st.routes.map((r) => {
        const mine = r.owner_is_self || ctx.isAdmin();
        return h('tr', {},
          h('td', {}, h('div', { class: 'mono', style: { 'font-weight': 600, 'font-size': '13px' } }, r.model),
            r.upstream_model && r.upstream_model !== r.model ? h('div', { class: 'muted', style: { 'font-size': '12px' } }, `→ ${r.upstream_model}`) : null),
          h('td', {}, r.engine_name ? h('a', { href: `#/services/${encodeURIComponent(r.engine_id)}` }, r.engine_name) : h('span', { class: 'muted' }, '服务已删除'),
            h('div', { class: 'muted', style: { 'font-size': '12px' } }, [engineLabel(r.framework), r.upstream_credential ? '引擎密钥由服务端注入' : null].filter(Boolean).join(' · '))),
          h('td', {}, r.owner_name ?? '—', r.owner_is_self ? h('span', { class: 'muted' }, '（我）') : null),
          h('td', {}, mine
            ? h('select', { 'aria-label': `${r.model} 的共享范围`, onchange: (e) => update(r, { visibility: e.target.value }) },
              Object.entries(VIS).map(([v, l]) => h('option', { value: v, selected: v === r.visibility }, l)))
            : VIS[r.visibility]),
          h('td', {}, statusBadge(!r.enabled ? { tone: 'off', text: '已暂停' } : r.engine_status === 'verified' ? { tone: 'ok', text: '可调用' } : r.engine_status === 'missing' ? { tone: 'bad', text: '服务缺失' } : { tone: 'warn', text: '服务未验证' })),
          h('td', { style: { 'white-space': 'nowrap', 'text-align': 'right' } }, mine ? [
            h('button', { type: 'button', class: 'linkbtn', onclick: () => update(r, { enabled: !r.enabled }) }, r.enabled ? '暂停' : '恢复'),
            '  ',
            h('button', { type: 'button', class: 'linkbtn danger', onclick: () => update(r, { delete: true }) }, '下线'),
          ] : null));
      }))));
  }

  function publishForm() {
    const published = new Set((st.routes ?? []).map((r) => r.engine_id));
    const choices = st.engines.filter((e) => !published.has(e.id));
    if (!st.engines.length) {
      return section('发布到网关', h('div', { class: 'group' }, quietEmpty('ready', { title: '你还没有可发布的推理服务', body: '先在「推理服务」里登记自己运行的引擎，再回来发布。' })));
    }
    const f = st.form;
    const field = (label, input) => h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, label), input);
    return section('发布到网关', h('div', { class: 'group' },
      field('推理服务', h('select', { onchange: (e) => { f.engine_id = e.target.value; } },
        h('option', { value: '' }, choices.length ? '选择一个服务…' : '你的服务都已发布'),
        choices.map((e) => h('option', { value: e.id, selected: e.id === f.engine_id }, `${e.label} · ${engineLabel(e.engine)}${e.port ? ` · :${e.port}` : ''}`)))),
      field('对外模型名', h('input', { placeholder: '留空则用服务名', value: f.model, style: { width: '240px' }, oninput: (e) => { f.model = e.target.value.trim(); } })),
      field('共享范围', h('select', { onchange: (e) => { f.visibility = e.target.value; } }, Object.entries(VIS).map(([v, l]) => h('option', { value: v, selected: v === f.visibility }, l)))),
      h('div', { class: 'cell', style: { 'justify-content': 'flex-end' } },
        h('button', { type: 'button', class: 'btn primary', disabled: st.busy || !choices.length, onclick: publish }, st.busy ? '正在发布…' : '发布'))),
    '共享的是正在运行的那一个服务实例：大家的请求排队进同一组并发槽位，不会重复占用显存。想独占就选「仅自己」。');
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    put(root, 
      pageHead('模型路由', '网关上可调用的模型名，以及它们落到哪个推理服务。服务所有者决定共享给谁、何时暂停。'),
      routesTable(),
      st.msg ? h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg) : null,
      publishForm(),
      snippetPanel(baseUrl(st.base), st.routes?.find((r) => r.enabled)?.model));
  }
  await refresh();
  draw();
  return root;
}

/* ============================== API 密钥 ============================== */
const KEY_STATE = { active: { tone: 'ok', text: '有效' }, revoked: { tone: 'off', text: '已撤销' }, expired: { tone: 'warn', text: '已过期' } };
const TTL = [['永不过期', null], ['7 天', 7], ['30 天', 30], ['90 天', 90], ['1 年', 365]];

export async function keys(ctx) {
  const root = h('div', { class: 'page' });
  const st = { view: 'mine', list: null, state: 'ready', routes: [], issued: null, msg: null, busy: false, showRevoked: false,
    form: { label: '', models: [], rpm: 60, concurrency: 2, ttl: 30, log_bodies: false } };

  async function refresh() {
    if (ctx.demo) {
      st.list = demo().keys.filter((k) => st.view === 'all' || k.owner === 'account:fjd');
      st.routes = demo().routes;
      return;
    }
    const [r, m] = await Promise.all([
      loadData(st.view === 'all' ? `${PATHS.gatewayKeys}?view=all` : PATHS.gatewayKeys),
      loadData(PATHS.gatewayModels),
    ]);
    st.state = r.state;
    st.list = r.state === 'ready' ? r.value.keys : null;
    st.routes = m.state === 'ready' ? m.value.routes : [];
  }

  async function create() {
    const f = st.form;
    st.busy = true; st.msg = null; draw();
    const body = { label: f.label || null, models: f.models.length ? f.models : null, rpm: Number(f.rpm), concurrency: Number(f.concurrency), ttl_days: f.ttl, log_bodies: f.log_bodies };
    if (ctx.demo) {
      const plain = `sk-lab-demo${Math.random().toString(36).slice(2, 12)}${Math.random().toString(36).slice(2, 12)}`;
      const k = { usage: { requests_24h: 0, requests_7d: 0, tokens_7d: 0, errors_7d: 0 }, log_bodies: body.log_bodies, key_id: `key_${Date.now()}`, prefix: plain.slice(0, 11), label: body.label, owner: 'account:fjd', owner_name: 'fjd', models: body.models, rpm: body.rpm, concurrency: body.concurrency, created_at: new Date().toISOString(), expires_at: f.ttl ? new Date(Date.now() + f.ttl * 86400_000).toISOString() : null, last_used_at: null, state: 'active' };
      demo().keys.unshift(k);
      st.issued = { ...k, key: plain };
    } else {
      const r = await send(PATHS.gatewayKeyCreate, body);
      if (!r.ok) { st.busy = false; st.msg = failText(r, '创建'); draw(); return; }
      st.issued = r.data.key;
    }
    st.busy = false; st.form = { label: '', models: [], rpm: 60, concurrency: 2, ttl: 30, log_bodies: false };
    await refresh(); draw();
  }

  async function toggleBodies(k) {
    const next = !k.log_bodies;
    if (next && !globalThis.confirm?.(`为「${k.label ?? k.prefix}」打开内容记录后，它之后的请求与响应正文会保存 7 天，你和管理员可以在调用日志里查看。确定打开？`)) return;
    if (ctx.demo) Object.assign(demo().keys.find((x) => x.key_id === k.key_id), { log_bodies: next });
    else { const r = await send(gatewayKeyUpdate(k.key_id), { log_bodies: next }); if (!r.ok) { toast(failText(r, '修改')); return; } }
    toast(next ? '已打开内容记录' : '已关闭内容记录');
    await refresh(); draw();
  }

  async function revoke(k) {
    if (!globalThis.confirm?.(`撤销「${k.label ?? k.prefix}」后，使用它的程序会立刻收到 401。确定撤销？`)) return;
    if (ctx.demo) { Object.assign(demo().keys.find((x) => x.key_id === k.key_id), { state: 'revoked' }); }
    else { const r = await send(gatewayKeyRevoke(k.key_id), {}); if (!r.ok) { toast(failText(r, '撤销')); return; } }
    await refresh(); draw();
  }

  function issuedCard() {
    const k = st.issued;
    const copy = h('button', { type: 'button', class: 'btn', onclick: async () => {
      try { await navigator.clipboard.writeText(k.key); copy.textContent = '已复制'; } catch { copy.textContent = '请手动复制'; }
    } }, '复制');
    return h('section', { class: 'reveal', role: 'status' },
      h('div', { class: 'reveal-head' }, h('strong', {}, `新密钥「${k.label ?? k.prefix}」已创建`), h('button', { type: 'button', class: 'linkbtn', onclick: () => { st.issued = null; draw(); } }, '我已保存')),
      h('p', { class: 'muted', style: { margin: 0, 'font-size': '13px' } }, '完整密钥只显示这一次，关闭后无法再次查看。平台只保存它的摘要。'),
      h('div', { class: 'reveal-key' }, h('code', { class: 'mono' }, k.key), copy),
      h('div', { class: 'code' }, `export OPENAI_BASE_URL=${location.origin}/v1\nexport OPENAI_API_KEY=${k.key}`));
  }

  function createForm() {
    const f = st.form;
    const field = (label, input, sub) => h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, label, sub ? h('div', { class: 'sub' }, sub) : null), input);
    const routes = st.routes.filter((r) => r.enabled);
    return section('创建密钥', h('div', { class: 'group' },
      field('用途', h('input', { placeholder: '例如 laptop、eval-pipeline', value: f.label, maxlength: 64, style: { width: '240px' }, oninput: (e) => { f.label = e.target.value; } })),
      h('div', { class: 'cell', style: { 'align-items': 'flex-start' } }, h('span', { class: 'fieldname' }, '可调用的模型', h('div', { class: 'sub' }, '都不勾选 = 我能用的全部模型')),
        h('div', { class: 'chips' }, routes.length ? routes.map((r) => h('label', { class: 'chip' },
          h('input', { type: 'checkbox', checked: f.models.includes(r.model), onchange: (e) => { f.models = e.target.checked ? [...f.models, r.model] : f.models.filter((m) => m !== r.model); } }),
          h('span', { class: 'mono' }, r.model))) : h('span', { class: 'muted', style: { 'font-size': '13px' } }, '暂无已发布模型'))),
      field('每分钟请求上限', h('input', { type: 'number', class: 'tnum', min: 1, max: 6000, value: f.rpm, style: { width: '96px' }, oninput: (e) => { f.rpm = e.target.value; } }), 'RPM'),
      field('同时进行的请求', h('input', { type: 'number', class: 'tnum', min: 1, max: 16, value: f.concurrency, style: { width: '96px' }, oninput: (e) => { f.concurrency = e.target.value; } }), '超出时返回 429，保护共享的并发槽位'),
      field('记录请求内容', h('label', { class: 'switch' }, h('input', { type: 'checkbox', role: 'switch', checked: f.log_bodies, onchange: (e) => { f.log_bodies = e.target.checked; } }), h('span', { 'aria-hidden': 'true' })), '默认关闭。打开后正文保留 7 天，便于排查；仅你和管理员可见'),
      field('有效期', h('select', { onchange: (e) => { f.ttl = e.target.value === '' ? null : Number(e.target.value); } }, TTL.map(([l, d]) => h('option', { value: d ?? '', selected: d === f.ttl }, l)))),
      h('div', { class: 'cell', style: { 'justify-content': 'flex-end' } },
        h('button', { type: 'button', class: 'btn primary', disabled: st.busy, onclick: create }, st.busy ? '正在创建…' : '创建密钥'))),
    '每个人用自己的密钥、看自己的用量；密钥不共享。对外交付也请单独建一把，便于限流与随时撤销。');
  }

  function table() {
    if (!st.list) return h('div', { class: 'group' }, quietEmpty(st.state, { title: '网关未启用', body: '本部署尚未接入网关。' }));
    const shown = st.list.filter((k) => st.showRevoked || k.state === 'active');
    const hidden = st.list.length - shown.length;
    const all = st.view === 'all';
    return h('div', { style: { display: 'flex', 'flex-direction': 'column', gap: '8px' } },
      shown.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', {}, h('tr', {}, ['密钥', all ? '所有者' : null, '可调用模型', '限额', '24 小时', '最近使用', '到期', '记录内容', '状态', ''].filter((x) => x !== null).map((t) => h('th', { scope: 'col', class: t === '24 小时' ? 'num' : null }, t)))),
        h('tbody', {}, shown.map((k) => h('tr', {},
          h('td', {}, h('div', { style: { 'font-weight': 500 } }, k.label ?? '未命名'), h('div', { class: 'mono muted', style: { 'font-size': '12px' } }, `${k.prefix}…`)),
          all ? h('td', {}, k.owner_name) : null,
          h('td', { class: k.models ? 'mono' : 'muted', style: { 'font-size': '12.5px' } }, k.models ? k.models.join(', ') : '全部'),
          h('td', { class: 'tnum muted', style: { 'font-size': '12.5px', 'white-space': 'nowrap' } }, `${k.rpm}/分 · ${k.concurrency} 并发`),
          h('td', { class: 'num' }, h('a', { href: `#/gateway/logs?key=${encodeURIComponent(k.key_id)}${all ? '&view=all' : ''}`, title: '查看这把密钥的调用日志' }, `${fmtInt(k.usage?.requests_24h ?? 0)} 次`)),
          h('td', { class: 'muted' }, k.last_used_at ? relTime(k.last_used_at) : '尚未使用'),
          h('td', { class: 'muted' }, k.expires_at ? new Date(k.expires_at).toLocaleDateString() : '永不'),
          h('td', {}, !all && k.state === 'active'
            ? h('label', { class: 'switch', title: '记录请求内容' }, h('input', { type: 'checkbox', role: 'switch', 'aria-label': `为 ${k.label ?? k.prefix} 记录请求内容`, checked: Boolean(k.log_bodies), onchange: () => toggleBodies(k) }), h('span', { 'aria-hidden': 'true' }))
            : h('span', { class: 'muted' }, k.log_bodies ? '开' : '关')),
          h('td', {}, statusBadge(KEY_STATE[k.state] ?? KEY_STATE.revoked)),
          h('td', { style: { 'text-align': 'right' } }, k.state === 'active' ? h('button', { type: 'button', class: 'linkbtn danger', onclick: () => revoke(k) }, '撤销') : null))))))
        : h('div', { class: 'group' }, quietEmpty('ready', { title: all ? '实验室里还没有有效密钥' : '你还没有 API 密钥', body: '在下方创建一把，就能用 OpenAI SDK 调用网关上的模型。' })),
      hidden > 0 || st.showRevoked ? h('button', { type: 'button', class: 'linkbtn', style: { 'align-self': 'flex-start' }, onclick: () => { st.showRevoked = !st.showRevoked; draw(); } },
        st.showRevoked ? '隐藏已撤销 / 已过期' : `显示已撤销 / 已过期（${hidden}）`) : null);
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    put(root, 
      pageHead('API 密钥', '调用网关用的虚拟密钥。每把密钥可以限定模型、限速、设有效期，随时撤销。',
        viewToggle(ctx, st.view, async (v) => { st.view = v; await refresh(); draw(); })),
      st.issued ? issuedCard() : null,
      table(),
      st.msg ? h('p', { class: 'form-error', role: 'alert', style: { 'text-align': 'left' } }, st.msg) : null,
      st.view === 'mine' ? createForm() : null);
  }
  await refresh();
  draw();
  return root;
}

/* ============================== 调用日志 ============================== */
/* 以密钥为单位：先选一把密钥（或全部），下面是 LiteLLM 式的简要日志；点任一行看这次请求的
 * 全量记录。「全量日志」是同一份数据的完整列视图，可导出 JSONL。正文只有在那把密钥打开了
 * 「记录请求内容」时才有。 */
const OUTCOMES = [['全部', ''], ['成功', 'success'], ['失败', 'failed']];
const PLAYGROUND_KEY = '__playground__';

function hashParams() {
  const q = location.hash.split('?')[1] ?? '';
  return new URLSearchParams(q);
}

function sourceLabel(r) {
  if (r.channel === 'playground') return '在线体验';
  return r.key_label ?? (r.key_prefix ? `${r.key_prefix}…` : r.key_id ?? '—');
}

async function loadDetail(ctx, id) {
  if (ctx.demo) { const d = demo().detail(id); return d ? { state: 'ready', value: d } : { state: 'error' }; }
  return loadData(gatewayLogDetail(id));
}

function pretty(v) {
  if (v == null) return '—';
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2);
}

/** 单次请求的全量记录：右侧抽屉。 */
async function openDetail(ctx, row) {
  const close = () => { sheet.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const body = h('div', { class: 'sheet-body' }, h('p', { class: 'muted' }, '正在加载…'));
  const sheet = h('div', { class: 'sheet-backdrop', onclick: (e) => { if (e.target === sheet) close(); } },
    h('aside', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-label': '请求详情' },
      h('div', { class: 'sheet-head' }, h('div', {}, h('div', { class: 'muted', style: { 'font-size': '12px' } }, '请求详情'), h('div', { class: 'mono', style: { 'font-weight': 600 } }, row.request_id)),
        h('button', { type: 'button', class: 'btn', onclick: close }, '关闭')),
      body));
  document.put(body, sheet);
  document.addEventListener('keydown', onKey);
  const r = await loadDetail(ctx, row.request_id);
  while (body.firstChild) body.firstChild.remove();
  if (r.state !== 'ready') { put(body, emptyState(r.state)); return; }
  const d = r.value;
  const kv = (k, v, mono) => h('div', { class: 'cell' }, h('span', { class: 'grow muted' }, k), h('span', { class: mono ? 'mono' : 'tnum', style: { 'text-align': 'right', 'word-break': 'break-all' } }, v ?? '—'));
  put(body, 
    h('div', { style: { display: 'flex', gap: '8px', 'align-items': 'center', 'flex-wrap': 'wrap' } },
      statusBadge(d.outcome === 'success' ? { tone: 'ok', text: `${d.status} 成功` } : { tone: 'bad', text: `${d.status ?? '—'} ${d.outcome === 'cancelled' ? '客户端取消' : '失败'}` }),
      d.stream ? h('span', { class: 'tag' }, '流式') : null, h('span', { class: 'muted', style: { 'font-size': '13px' } }, new Date(d.ts).toLocaleString())),
    d.error ? h('div', { class: 'callout bad' }, d.error) : null,
    section('请求', h('div', { class: 'group' },
      kv('调用者', d.caller_name), kv('来源', sourceLabel(d)), kv('接口', d.path, true),
      kv('模型', d.model, true), kv('上游模型', d.upstream_model ?? d.model, true), kv('推理服务', d.engine_name), kv('服务所有者', d.route_owner_name))),
    section('用量与耗时', h('div', { class: 'group' },
      kv('输入 Tokens', d.prompt_tokens == null ? null : fmtInt(d.prompt_tokens)), kv('输出 Tokens', d.completion_tokens == null ? null : fmtInt(d.completion_tokens)),
      kv('首字耗时', ms(d.ttft_ms)), kv('总耗时', ms(d.total_ms)),
      kv('生成速度', d.completion_tokens && d.total_ms ? `${(d.completion_tokens / ((d.total_ms - (d.ttft_ms ?? 0)) / 1000)).toFixed(1)} tok/s` : null))),
    d.body
      ? [section('请求内容', h('pre', { class: 'code wrap' }, pretty(d.body.request))),
        section('响应内容', h('pre', { class: 'code wrap' }, d.body.response_text != null ? d.body.response_text : pretty(d.body.response)),
          d.body.truncated ? '响应超过 64 KB，只保留了开头部分。' : '正文保留 7 天后自动删除。')]
      : h('div', { class: 'group' }, quietEmpty('ready', { title: '未记录请求内容', body: d.channel === 'playground' ? '在线体验的对话不记录正文。' : '这把密钥没有打开「记录请求内容」，只保留了上面的元数据。可以在「API 密钥」里为它打开。' })));
}

export async function logs(ctx, { full = false } = {}) {
  const root = h('div', { class: 'page' });
  const params = hashParams();
  const st = { view: params.get('view') === 'all' && ctx.isAdmin() ? 'all' : 'mine', outcome: '', model: '', key: params.get('key') ?? '', rows: null, keys: [], state: 'ready' };

  async function refresh() {
    if (ctx.demo) {
      st.keys = demo().keys.filter((k) => st.view === 'all' || k.owner === 'account:fjd');
      st.rows = demo().rows.filter((r) => (st.view === 'all' || r.caller === 'account:fjd') && (!st.outcome || r.outcome === st.outcome) && (!st.model || r.model === st.model)
        && (!st.key || (st.key === PLAYGROUND_KEY ? r.channel === 'playground' : r.key_id === st.key)));
      return;
    }
    const q = new URLSearchParams({ limit: full ? '500' : '100' });
    if (st.view === 'all') q.set('view', 'all');
    if (st.outcome) q.set('outcome', st.outcome);
    if (st.model) q.set('model', st.model);
    if (st.key && st.key !== PLAYGROUND_KEY) q.set('key_ref', st.key);
    const [r, k] = await Promise.all([loadData(`${PATHS.gatewayLogs}?${q}`), loadData(st.view === 'all' ? `${PATHS.gatewayKeys}?view=all` : PATHS.gatewayKeys)]);
    st.state = r.state;
    st.rows = r.state === 'ready' ? r.value.rows.filter((x) => st.key !== PLAYGROUND_KEY || x.channel === 'playground') : null;
    st.keys = k.state === 'ready' ? k.value.keys : [];
  }
  const redraw = async () => { await refresh(); draw(); };

  function keyRail() {
    const live = st.keys.filter((k) => k.state === 'active' || (k.usage?.requests_7d ?? 0) > 0);
    const card = (id, title, sub, stats, extra) => h('button', { type: 'button', class: 'keycard', 'aria-pressed': String(st.key === id), onclick: () => { st.key = st.key === id ? '' : id; redraw(); } },
      h('div', { class: 'keycard-title' }, title, extra), h('div', { class: 'keycard-sub mono' }, sub), h('div', { class: 'keycard-stats tnum' }, stats));
    const all = st.view === 'all';
    return h('div', { class: 'keyrail', role: 'group', 'aria-label': '按密钥查看' },
      card('', '全部密钥', all ? '全实验室' : '我的所有调用', `${fmtInt(st.keys.reduce((t, k) => t + (k.usage?.requests_24h ?? 0), 0))} 次 / 24 小时`),
      live.map((k) => card(k.key_id, k.label ?? '未命名', `${k.prefix}…${all ? ` · ${k.owner_name}` : ''}`,
        `${fmtInt(k.usage?.requests_24h ?? 0)} 次 / 24h · ${tokens(k.usage?.tokens_7d ?? 0)} tok / 7d${k.usage?.errors_7d ? ` · ${k.usage.errors_7d} 失败` : ''}`,
        k.log_bodies ? h('span', { class: 'tag', title: '这把密钥记录请求内容' }, '记录内容') : (k.state !== 'active' ? h('span', { class: 'tag' }, '已撤销') : null))),
      card(PLAYGROUND_KEY, '在线体验', '浏览器内对话', '会话调用'));
  }

  function briefTable(rows) {
    const all = st.view === 'all';
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', {}, h('tr', {}, ['时间', all ? '调用者' : null, '密钥', '模型', 'Tokens', '耗时', '状态'].filter((x) => x !== null).map((t) => h('th', { scope: 'col', class: t === 'Tokens' || t === '耗时' ? 'num' : null }, t)))),
      h('tbody', {}, rows.map((r) => h('tr', { class: 'selectable', tabindex: 0, onclick: () => openDetail(ctx, r), onkeydown: (e) => { if (e.key === 'Enter') openDetail(ctx, r); } },
        h('td', { class: 'tnum', title: r.ts }, clock(r.ts), h('span', { class: 'muted', style: { 'margin-left': '8px', 'font-size': '12px' } }, relTime(r.ts))),
        all ? h('td', {}, r.caller_name) : null,
        h('td', { class: 'muted' }, sourceLabel(r), r.has_body ? h('span', { class: 'dot-mark', title: '已记录内容' }) : null),
        h('td', { class: 'mono', style: { 'font-size': '12.5px' } }, r.model ?? '—'),
        h('td', { class: 'num' }, r.prompt_tokens == null && r.completion_tokens == null ? '—' : `${fmtInt(r.prompt_tokens ?? 0)} → ${fmtInt(r.completion_tokens ?? 0)}`),
        h('td', { class: 'num' }, ms(r.total_ms)),
        h('td', {}, statusBadge(r.outcome === 'success' ? { tone: 'ok', text: String(r.status ?? 200) } : { tone: 'bad', text: String(r.status ?? '失败') })))))));
  }

  function fullTable(rows) {
    const cols = [
      ['时间', (r) => r.ts], ['请求 ID', (r) => r.request_id], ['调用者', (r) => r.caller_name], ['来源', sourceLabel], ['模型', (r) => r.model],
      ['流式', (r) => (r.stream ? '是' : '否')], ['状态', (r) => r.status], ['结果', (r) => r.outcome], ['输入', (r) => r.prompt_tokens], ['输出', (r) => r.completion_tokens],
      ['首字 ms', (r) => (r.ttft_ms == null ? null : Math.round(r.ttft_ms))], ['总耗时 ms', (r) => (r.total_ms == null ? null : Math.round(r.total_ms))], ['错误', (r) => r.error], ['内容', (r) => (r.has_body ? '有' : '')],
    ];
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data dense' },
      h('thead', {}, h('tr', {}, cols.map(([t]) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, rows.map((r) => h('tr', { class: 'selectable', onclick: () => openDetail(ctx, r) }, cols.map(([, f], i) => h('td', { class: i <= 1 || i === 4 ? 'mono' : 'tnum' }, f(r) ?? '—')))))));
  }

  function exportJsonl() {
    const blob = new Blob([st.rows.map((r) => JSON.stringify(r)).join('\n') + '\n'], { type: 'application/x-ndjson' });
    const a = h('a', { href: URL.createObjectURL(blob), download: `llmlab-gateway-logs-${new Date().toISOString().slice(0, 10)}.jsonl` });
    document.put(body, a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  function draw() {
    while (root.firstChild) root.firstChild.remove();
    const seg = segmented('结果', OUTCOMES.map(([l]) => l), OUTCOMES.find(([, v]) => v === st.outcome)[0], (l) => { st.outcome = OUTCOMES.find(([x]) => x === l)[1]; redraw(); });
    const head = full
      ? h('header', {}, h('a', { class: 'crumb', href: '#/gateway/logs' }, '调用日志'),
        h('div', { class: 'page-head', style: { 'margin-top': '6px' } },
          h('div', {}, h('h1', { class: 'page-title' }, '全量日志'), h('p', { class: 'page-sub' }, '最近 30 天、最多 500 条的完整字段。点任一行看单次请求的全部记录；可导出为 JSONL。')),
          h('div', { style: { display: 'flex', gap: '10px', 'flex-wrap': 'wrap' } }, viewToggle(ctx, st.view, (v) => { st.view = v; redraw(); }), seg,
            h('button', { type: 'button', class: 'btn', disabled: !st.rows?.length, onclick: exportJsonl }, '导出 JSONL'))))
      : pageHead('调用日志', '按 API 密钥查看你的每一次调用：模型、Token、耗时与结果。点任一行查看完整记录。',
        viewToggle(ctx, st.view, (v) => { st.view = v; st.key = ''; redraw(); }), seg,
        h('a', { class: 'btn', href: `#/gateway/logs/full${st.view === 'all' ? '?view=all' : ''}` }, '全量日志'));
    put(root, head);
    if (!st.rows) { put(root, h('div', { class: 'group' }, quietEmpty(st.state, { title: '网关未启用', body: '本部署尚未接入网关。' }))); return; }
    if (!full) put(root, keyRail());
    const filters = [st.model ? h('span', { class: 'chip on' }, `模型 ${st.model}`, h('button', { type: 'button', class: 'linkbtn', 'aria-label': '清除模型筛选', onclick: () => { st.model = ''; redraw(); } }, '×')) : null].filter(Boolean);
    if (filters.length) put(root, h('div', { class: 'chips' }, filters));
    if (!st.rows.length) { put(root, h('div', { class: 'group' }, quietEmpty('ready', st.outcome || st.key || st.model ? { title: '没有符合条件的调用', body: '换一把密钥或筛选条件试试。' } : NO_CALLS))); return; }
    put(root, full ? fullTable(st.rows) : briefTable(st.rows),
      h('p', { class: 'muted', style: { margin: 0, 'font-size': '12.5px' } },
        `显示最近 ${fmtInt(st.rows.length)} 条。默认只记录元数据；需要排查内容时，在「API 密钥」里为某一把密钥打开「记录请求内容」（保留 7 天，仅本人与管理员可见）。`));
  }
  await refresh();
  draw();
  return root;
}

export const logsFull = (ctx) => logs(ctx, { full: true });

/* ============================== 在线体验 ============================== */
export async function playground(ctx) {
  const root = h('div', { class: 'page' });
  let routes = [];
  if (ctx.demo) routes = demo().routes;
  else { const r = await loadData(PATHS.gatewayModels); routes = r.state === 'ready' ? r.value.routes : null; }
  const usable = (routes ?? []).filter((r) => r.enabled);
  const st = { model: usable[0]?.model ?? '', system: '', temperature: 0.7, max_tokens: 512, messages: [], busy: false, last: null };

  const log = h('div', { class: 'chat-log', 'aria-live': 'polite' });
  const input = h('textarea', { rows: 3, placeholder: '输入消息，⌘/Ctrl + Enter 发送', onkeydown: (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendMsg(); } } });
  const meta = h('div', { class: 'muted tnum', style: { 'font-size': '12.5px' } });

  function drawLog() {
    while (log.firstChild) log.firstChild.remove();
    if (!st.messages.length) put(log, h('div', { class: 'chat-empty muted' }, '以你的账户经网关调用，和 API 调用一样计入你的用量。'));
    for (const m of st.messages) put(log, h('div', { class: `bubble ${m.role}${m.error ? ' error' : ''}` }, m.content));
    if (st.busy) put(log, h('div', { class: 'bubble assistant typing' }, h('i'), h('i'), h('i')));
    log.scrollTop = log.scrollHeight;
    meta.textContent = st.last ? `${st.last.model} · 输入 ${st.last.usage?.prompt_tokens ?? '—'} · 输出 ${st.last.usage?.completion_tokens ?? '—'} tokens · ${ms(st.last.ms)}` : '';
  }

  async function sendMsg() {
    const text = input.value.trim();
    if (!text || st.busy || !st.model) return;
    st.messages.push({ role: 'user', content: text }); input.value = ''; st.busy = true; drawLog();
    const messages = [...(st.system ? [{ role: 'system', content: st.system }] : []), ...st.messages.filter((m) => !m.error)];
    const t0 = performance.now();
    if (ctx.demo) {
      await new Promise((res) => setTimeout(res, 650));
      st.messages.push({ role: 'assistant', content: `（演示）这是 ${st.model} 的示例回复。真实部署中，这里会显示推理服务经网关返回的内容。` });
      st.last = { model: st.model, usage: { prompt_tokens: 18 + text.length, completion_tokens: 36 }, ms: performance.now() - t0 };
    } else {
      const r = await send(PATHS.gatewayPlayground, { model: st.model, messages, temperature: Number(st.temperature), max_tokens: Number(st.max_tokens) });
      if (!r.ok) st.messages.push({ role: 'assistant', content: failText(r, '调用'), error: true });
      else {
        st.messages.push({ role: 'assistant', content: r.data.message?.content ?? '（空回复）' });
        st.last = { model: r.data.model, usage: r.data.usage, ms: performance.now() - t0 };
      }
    }
    st.busy = false; drawLog();
  }

  if (!routes) { put(root, pageHead('在线体验', null), h('div', { class: 'group' }, emptyState('not_configured'))); return root; }
  const field = (label, el) => h('label', { class: 'cell' }, h('span', { class: 'fieldname' }, label), el);
  put(root, 
    pageHead('在线体验', '直接在浏览器里和网关上的模型对话，用来快速验证服务与参数。'),
    usable.length ? h('div', { class: 'row' },
      h('section', { class: 'panel grow chat' }, log,
        h('div', { class: 'chat-input' }, input,
          h('div', { style: { display: 'flex', 'justify-content': 'space-between', 'align-items': 'center', gap: '10px' } }, meta,
            h('div', { style: { display: 'flex', gap: '8px' } },
              h('button', { type: 'button', class: 'btn', onclick: () => { st.messages = []; st.last = null; drawLog(); } }, '清空'),
              h('button', { type: 'button', class: 'btn primary', onclick: sendMsg }, '发送'))))),
      h('div', { style: { flex: '0 1 300px', 'min-width': '260px' } }, section('设置', h('div', { class: 'group' },
        field('模型', h('select', { onchange: (e) => { st.model = e.target.value; } }, usable.map((r) => h('option', { value: r.model, selected: r.model === st.model }, r.model)))),
        field('温度', h('input', { type: 'number', class: 'tnum', min: 0, max: 2, step: 0.1, value: st.temperature, style: { width: '72px' }, oninput: (e) => { st.temperature = e.target.value; } })),
        field('最大输出', h('input', { type: 'number', class: 'tnum', min: 1, max: 1024, value: st.max_tokens, style: { width: '72px' }, oninput: (e) => { st.max_tokens = e.target.value; } })),
        h('label', { class: 'cell', style: { 'flex-direction': 'column', 'align-items': 'stretch', gap: '6px' } }, h('span', { class: 'fieldname' }, '系统提示词'),
          h('textarea', { rows: 4, placeholder: '可选', oninput: (e) => { st.system = e.target.value; } }))),
      '单次最多 1024 个输出 Token；需要更长输出请用 API。')))
      : h('div', { class: 'group' }, quietEmpty('ready', { title: '还没有可调用的模型', body: '在「模型路由」把推理服务发布到网关后，就能在这里对话。' })));
  drawLog();
  return root;
}
