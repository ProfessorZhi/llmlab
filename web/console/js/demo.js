/*! web/console/js/demo.js — 演示数据（仅在 qa.html?demo=1 时使用）。
 *  以 2026-10-22 Dell 真实快照（snapshot.js）为原型：真实的引擎、模型、GPU 与主机读数、
 *  真实的用户名单。网关调用记录是在这些真实对象上**确定性合成**的示例（真实网关在快照
 *  窗口内为 0 次），页面顶部始终显示「演示数据」提示。 */
import { RUNTIME_LABEL, HEALTH_LABEL, modelColorMap } from './viewmodel.js';
import { SNAP_SYSTEM, SNAP_ENGINE_LIVE, SNAP_REGISTRY } from './snapshot.js';

const ENGINE = SNAP_REGISTRY.engines[0];
const MODEL = SNAP_REGISTRY.models[0];
export const DEMO_MODEL_NAME = MODEL.name;

export const DEMO_USERS = Object.freeze([
  { id: 'account:fjd', username: 'fjd', display_name: 'fjd', linux_user: 'fjd', role: 'admin', status: 'active', last_login_at: '2026-10-22T08:20:00Z', created_at: '2026-10-21T10:00:00Z' },
  { id: 'account:vince', username: 'vince', display_name: 'vince', linux_user: null, role: 'member', status: 'active', last_login_at: '2026-10-22T08:28:00Z', created_at: '2026-10-21T12:00:00Z' },
  { id: 'account:kaidong', username: 'kaidong', display_name: 'kaidong', linux_user: 'kaidong', role: 'member', status: 'active', last_login_at: '2026-10-22T03:12:00Z', created_at: '2026-10-21T12:05:00Z' },
  { id: 'account:llmlab', username: 'llmlab', display_name: 'llmlab（平台服务）', linux_user: 'llmlab', role: 'member', status: 'active', last_login_at: null, created_at: '2026-10-21T12:10:00Z' },
]);

export const DEMO_ME = Object.freeze({ id: 'account:fjd', username: 'fjd', display_name: 'fjd', linux_user: 'fjd', role: 'admin', is_platform_admin: true, must_change_password: false });

function seeded(seed) {
  let state = seed >>> 0;
  return () => { state = (state * 1664525 + 1013904223) >>> 0; return state / 0x100000000; };
}

const GPU_PROC = SNAP_SYSTEM.gpu_processes[0];

export function demoServices() {
  return [{
    id: ENGINE.engine_id, kind: 'engine', label: ENGINE.name, engine: ENGINE.framework, owner: ENGINE.owner_name,
    runAs: GPU_PROC.user, self: true, port: ENGINE.port, runtime: RUNTIME_LABEL.running, health: HEALTH_LABEL.healthy,
    share: '全部成员', vramMiB: GPU_PROC.used_mib, sharedToMe: false, running: true,
  }];
}

export function demoModels() {
  return [{
    id: MODEL.model_id, name: MODEL.name, format: MODEL.format, params: MODEL.parameter_count, description: MODEL.description,
    owner: MODEL.owner_name, self: true, weightPath: '/data/models/qwen3.8-flash-next/IQ2_XS.gguf', createdAt: MODEL.created_at,
  }];
}

/* ---------- 网关 ---------- */

const NOW = Date.parse('2026-10-22T08:29:00Z');
const ROUTE = { route_id: 'rt_qwen38', model: 'qwen3.8-flash-next', engine_id: ENGINE.engine_id, engine_name: ENGINE.name, engine_status: 'verified', framework: 'strata', upstream_model: MODEL.name, owner: 'account:fjd', owner_name: 'fjd', owner_is_self: true, visibility: 'all', enabled: true, upstream_credential: true, created_at: '2026-10-22T06:00:00Z' };

const KEYS = [
  { key_id: 'key_vince1', prefix: 'sk-lab-Qm4x', label: 'laptop', owner: 'account:vince', owner_name: 'vince', models: null, rpm: 60, concurrency: 2, created_at: '2026-10-22T05:10:00Z', expires_at: '2026-11-21T05:10:00Z', last_used_at: '2026-10-22T08:24:00Z', state: 'active', log_bodies: true },
  { key_id: 'key_kd01', prefix: 'sk-lab-9cTe', label: 'eval-pipeline', owner: 'account:kaidong', owner_name: 'kaidong', models: ['qwen3.8-flash-next'], rpm: 120, concurrency: 2, created_at: '2026-10-22T02:30:00Z', expires_at: null, last_used_at: '2026-10-22T07:58:00Z', state: 'active' },
  { key_id: 'key_fjd_ext', prefix: 'sk-lab-Xa2P', label: '对外 · 公网交付', owner: 'account:fjd', owner_name: 'fjd', models: ['qwen3.8-flash-next'], rpm: 30, concurrency: 2, created_at: '2026-10-22T06:50:00Z', expires_at: null, last_used_at: '2026-10-22T08:25:00Z', state: 'active' },
  { key_id: 'key_vince0', prefix: 'sk-lab-b7Lk', label: 'old-notebook', owner: 'account:vince', owner_name: 'vince', models: null, rpm: 60, concurrency: 2, created_at: '2026-10-21T13:00:00Z', expires_at: null, last_used_at: '2026-10-21T14:00:00Z', state: 'revoked' },
];

function demoLogRows() {
  const rand = seeded(11);
  const callers = [['account:vince', 'vince', 'key_vince1', 'laptop'], ['account:kaidong', 'kaidong', 'key_kd01', 'eval-pipeline'], ['account:fjd', 'fjd', 'key_fjd_ext', '对外 · 公网交付'], ['account:fjd', 'fjd', null, '在线体验']];
  const rows = [];
  let t = NOW;
  for (let i = 0; i < 96; i += 1) {
    const hour = new Date(t).getUTCHours();
    const busy = hour >= 1 && hour <= 11; // 北京时间白天
    t -= Math.round((busy ? 6 : 28) * 60_000 * (0.4 + rand()));
    const [caller, name, keyId, label] = callers[Math.floor(rand() * (rand() > 0.85 ? 4 : 3))];
    const ok = rand() > 0.04;
    const stream = rand() > 0.35;
    const prompt = Math.round(40 + rand() * 900);
    const completion = Math.round(20 + rand() * 360);
    const total = Math.round(300 + completion * (8 + rand() * 6));
    rows.push({
      request_id: `req_${(100000 + i).toString(16)}`, ts: new Date(t).toISOString(), model: ROUTE.model,
      key_id: keyId, key_label: label, key_prefix: KEYS.find((k) => k.key_id === keyId)?.prefix ?? null, channel: keyId ? 'api_key' : 'playground', caller, caller_name: name,
      status: ok ? 200 : (rand() > 0.5 ? 429 : 502), outcome: ok ? 'success' : 'failed', stream,
      prompt_tokens: ok ? prompt : null, completion_tokens: ok ? completion : null,
      ttft_ms: ok && stream ? Math.round(180 + rand() * 520) : null, total_ms: ok ? total : null,
      error: ok ? null : 'upstream answered 502', has_body: ok && keyId === 'key_vince1',
    });
  }
  return rows;
}

const pct = (xs, p) => {
  const v = xs.filter((x) => typeof x === 'number').sort((a, b) => a - b);
  if (!v.length) return null;
  const i = (v.length - 1) * p; const lo = Math.floor(i); const hi = Math.ceil(i);
  return v[lo] + (v[hi] - v[lo]) * (i - lo);
};

/** 与后端 ``gateway.service._aggregate`` 同形的聚合（只用于演示）。 */
export function aggregateRows(rows, { now = NOW, span = 86400_000 } = {}) {
  const inWin = rows.filter((r) => now - Date.parse(r.ts) < span);
  const ok = inWin.filter((r) => r.outcome === 'success');
  const buckets = span <= 86400_000 ? 24 : (span <= 7 * 86400_000 ? 7 : 30);
  const step = span / buckets;
  const series = Array(buckets).fill(0);
  const byModel = new Map(); const byCaller = new Map(); const pairs = new Map(); const byKey = new Map();
  const outcomes = { success: 0, failed: 0, cancelled: 0 };
  const heat = Array.from({ length: 7 }, () => Array(24).fill(0));
  for (const r of inWin) {
    const d = new Date(r.ts);
    heat[(d.getDay() + 6) % 7][d.getHours()] += 1;
    outcomes[r.outcome] = (outcomes[r.outcome] ?? 0) + 1;
    const kid = r.key_id ?? `${r.channel}:${r.caller}`;
    const k = byKey.get(kid) ?? { key_id: r.key_id, channel: r.channel, caller: r.caller, caller_name: r.caller_name, label: r.key_id ? r.key_label : '在线体验', requests: 0, tokens: 0, errors: 0 };
    k.requests += 1; k.tokens += (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0); k.errors += r.outcome === 'success' ? 0 : 1; byKey.set(kid, k);
    const idx = Math.floor((Date.parse(r.ts) - (now - span)) / step);
    if (idx >= 0 && idx < buckets) series[idx] += 1;
    const tok = (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0);
    const m = byModel.get(r.model) ?? { model: r.model, requests: 0, tokens: 0, errors: 0 };
    m.requests += 1; m.tokens += tok; m.errors += r.outcome === 'success' ? 0 : 1; byModel.set(r.model, m);
    const c = byCaller.get(r.caller) ?? { caller: r.caller, name: r.caller_name, requests: 0, tokens: 0 };
    c.requests += 1; c.tokens += tok; byCaller.set(r.caller, c);
    const key = `${r.caller}\u0000${r.model}`;
    const pr = pairs.get(key) ?? { caller: r.caller, name: r.caller_name, model: r.model, requests: 0 };
    pr.requests += 1; pairs.set(key, pr);
  }
  return {
    requests: inWin.length,
    success_rate: inWin.length ? ok.length / inWin.length : null,
    prompt_tokens: inWin.reduce((t, r) => t + (r.prompt_tokens ?? 0), 0),
    completion_tokens: inWin.reduce((t, r) => t + (r.completion_tokens ?? 0), 0),
    latency_p50_ms: pct(ok.map((r) => r.total_ms), 0.5),
    latency_p95_ms: pct(ok.map((r) => r.total_ms), 0.95),
    ttft_p50_ms: pct(ok.filter((r) => r.stream).map((r) => r.ttft_ms), 0.5),
    series: { buckets, step_seconds: step / 1000, requests: series },
    by_model: [...byModel.values()].sort((a, b) => b.requests - a.requests),
    by_caller: [...byCaller.values()].sort((a, b) => b.requests - a.requests),
    by_key: [...byKey.values()].sort((a, b) => b.requests - a.requests),
    outcomes,
    heatmap: heat,
    by_caller_model: [...pairs.values()].sort((a, b) => b.requests - a.requests),
  };
}

export function demoGateway() {
  const rows = demoLogRows();
  const now = NOW;
  const keys = KEYS.map((k) => {
    const mine = rows.filter((r) => r.key_id === k.key_id);
    const day = mine.filter((r) => now - Date.parse(r.ts) < 86400_000);
    return { log_bodies: false, ...k, usage: { requests_24h: day.length, requests_7d: mine.length, tokens_7d: mine.reduce((t, r) => t + (r.prompt_tokens ?? 0) + (r.completion_tokens ?? 0), 0), errors_7d: mine.filter((r) => r.outcome !== 'success').length } };
  });
  return {
    routes: [ROUTE],
    keys,
    rows,
    detail(requestId) {
      const r = rows.find((x) => x.request_id === requestId);
      if (!r) return null;
      return { ...r, path: '/v1/chat/completions', route_owner: ROUTE.owner, route_owner_name: ROUTE.owner_name, engine_name: ROUTE.engine_name, upstream_model: ROUTE.upstream_model,
        body: r.has_body ? { request: { model: r.model, stream: r.stream, messages: [{ role: 'user', content: '（演示）帮我把这段日志整理成表格。' }] }, response: r.stream ? 'data: {…}\n\ndata: [DONE]' : { choices: [{ message: { role: 'assistant', content: '（演示）好的，下面是整理后的表格……' } }] }, response_text: r.stream ? '（演示）好的，下面是整理后的表格……' : null, truncated: false } : null };
    },
    overview(window = '24h', view = 'mine') {
      const span = { '24h': 86400_000, '7d': 7 * 86400_000, '30d': 30 * 86400_000 }[window] ?? 86400_000;
      const everyone = view === 'all';
      const calls = aggregateRows(everyone ? rows : rows.filter((r) => r.caller === DEMO_ME.id), { span });
      // fjd 是唯一发布了模型的人：「我的服务被谁调用」= 全部调用。
      const served = everyone ? null : aggregateRows(rows, { span });
      return { window, view: everyone ? 'all' : 'mine', calls, my_services: served, keys_active: KEYS.filter((k) => k.state === 'active' && (everyone || k.owner === DEMO_ME.id)).length, routes: 1, inflight: 0, base_url: null };
    },
  };
}

export function demoParams() {
  return {
    engine_id: ENGINE.engine_id, engine_name: ENGINE.name, framework: 'strata', supported: true,
    schema: [
      { key: 'ctx_size', label: '上下文长度', kind: 'int', flag: '--ctx-size', group: '资源与并发', default: 8192, min: 512, max: 262144, unit: 'tokens', help: '所有并发槽位共享这个总长度；每路可用上下文 ≈ 上下文长度 ÷ 并发路数。', observable: true, choices: [] },
      { key: 'parallel', label: '并发路数', kind: 'int', flag: '--parallel', group: '资源与并发', default: 4, min: 1, max: 64, unit: '路', help: '同时处理的请求数（槽位数）。越多吞吐越高，每路可用上下文越短。', observable: true, choices: [] },
      { key: 'n_gpu_layers', label: 'GPU 层数', kind: 'int', flag: '--n-gpu-layers', group: '资源与并发', default: 999, min: 0, max: 999, unit: null, help: '放进显存的层数；999 表示全部。', observable: false, choices: [] },
      { key: 'batch_size', label: '批大小', kind: 'int', flag: '--batch-size', group: '性能', default: 2048, min: 32, max: 16384, unit: null, help: '', observable: false, choices: [] },
      { key: 'flash_attn', label: 'Flash Attention', kind: 'bool', flag: '--flash-attn', group: '性能', default: true, min: null, max: null, unit: null, help: '', observable: false, choices: [] },
      { key: 'cache_type_k', label: 'KV 缓存精度', kind: 'choice', flag: '--cache-type-k', group: '性能', default: 'f16', min: null, max: null, unit: null, help: '降低精度可以省显存、换更长上下文。', observable: false, choices: ['f16', 'q8_0', 'q4_0'] },
    ],
    observed: { reachable: true, values: { ctx_size: SNAP_ENGINE_LIVE.engine.max_context, parallel: SNAP_ENGINE_LIVE.engine.parallel, ctx_per_slot: SNAP_ENGINE_LIVE.engine.max_context / SNAP_ENGINE_LIVE.engine.parallel } },
    desired: { ctx_size: 32768, parallel: 4, flash_attn: true },
    state: 'applied',
    command: 'strata serve --ctx-size 32768 --parallel 4 --flash-attn --port 8080',
    can_edit: true,
    apply: 'restart_by_owner',
  };
}

export function demoOverview() {
  const services = demoServices();
  const gw = demoGateway().overview('24h', 'all').calls;
  const colors = modelColorMap([ROUTE.model]);
  const groups = gw.by_caller.map((c) => ({ id: c.caller, label: c.name, children: gw.by_caller_model.filter((p) => p.caller === c.caller).map((p) => ({ id: p.model, label: p.model, value: p.requests })) }));
  const h = SNAP_ENGINE_LIVE;
  return {
    source: 'demo',
    kpis: {
      requests: gw.requests, requestsScope: '近 24 小时',
      servicesOnline: 1, servicesTotal: 1,
      vramUsedMiB: 41958, vramTotalMiB: 49140,
      utilRatio: 0, tempC: 34, powerW: 21.5,
      successRate: gw.success_rate, p95ms: gw.latency_p95_ms,
    },
    distribution: { innerLabel: '调用者', groups },
    modelUsage: gw.by_model.map((m) => ({ id: m.model, label: m.model, value: m.requests })),
    modelColors: colors,
    hourly: { points: gw.series.requests, scope: 'lab' },
    vram: { totalMiB: 49140, usedMiB: 41958, parts: [{ id: ENGINE.engine_id, label: ENGINE.name, owner: 'fjd', engine: 'strata', mib: GPU_PROC.used_mib, color: '#5856D6' }] },
    services,
    keys: [],
    recent: [],
    host: { vramUsedMiB: 41958, vramTotalMiB: 49140, utilRatio: 0, tempC: 34, powerW: 21.5, services: { prometheus: true, strata: true, gateway: true, evalscope: true, grafana: true } },
    engineLive: h,
  };
}

export function demoEvaluation() {
  return [
    { run_id: 'run-101', model_id: ROUTE.model, dataset: 'C-Eval', status: 'done', metric: 0.712, metric_unit: 'acc', created_at: '2026-10-22T02:00:00Z' },
    { run_id: 'run-102', model_id: ROUTE.model, dataset: 'GSM8K', status: 'running', metric: null, metric_unit: 'acc', created_at: '2026-10-22T07:40:00Z' },
  ];
}
