/*! web/console/js/viewmodel.js — 把已校验的接口数据整理成页面使用的视图模型（纯函数）。
 *
 *  输入都是**已经过契约校验**的对象：
 *    - ops   ← validateOps(GET /api/v2/ops 的 data.ops)
 *    - host  ← validateHostSummary(GET /api/infra/summary)
 *    - log   ← validateCallLog(GET /api/atlas/calls 的 data.call_log)
 *  规则：没测到就是 null，页面显示「—」或「暂无数据源」，绝不补 0。
 */
import { colorAt } from './charts.js';

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

export const RUNTIME_LABEL = Object.freeze({
  running: { tone: 'ok', text: '运行中' },
  starting: { tone: 'warn', text: '启动中' },
  stopped: { tone: 'off', text: '已停止' },
  unknown: { tone: 'off', text: '状态未知' },
});

export const HEALTH_LABEL = Object.freeze({
  healthy: { tone: 'ok', text: '健康' },
  degraded: { tone: 'warn', text: '性能下降' },
  unreachable: { tone: 'bad', text: '无法访问' },
  unknown: { tone: 'off', text: '未知' },
});

export const SHARE_LABEL = Object.freeze({
  private: '仅本人',
  all: '全部成员',
  selected: '指定成员',
});

export const ENGINE_LABEL = Object.freeze({
  strata: 'Strata',
  vllm: 'vLLM',
  sglang: 'SGLang',
  ollama: 'Ollama',
  tei: 'Text Embeddings Inference',
  'llama.cpp': 'llama.cpp',
  openai: 'OpenAI 兼容',
  'openai-compatible': 'OpenAI 兼容',
});

export const engineLabel = (t) => (t ? ENGINE_LABEL[t] ?? t : '—');

/** 分位数（线性插值）。空数组 → null。 */
export function percentile(values, p) {
  const xs = values.filter(finite).sort((a, b) => a - b);
  if (!xs.length) return null;
  const idx = (xs.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  return xs[lo] + (xs[hi] - xs[lo]) * (idx - lo);
}

function metricValue(ops, name) {
  const item = (ops?.metrics?.items ?? []).find((m) => m.name === name);
  if (!item || item.state !== 'available' || !finite(item.value)) return null;
  return item.value;
}

const ENGINE_STATUS = Object.freeze({
  verified: { tone: 'ok', text: '运行中' },
  unavailable: { tone: 'bad', text: '无法访问' },
  registered: { tone: 'off', text: '未验证' },
});

/** 推理服务列表：已登记的推理引擎在前，其上的模型部署在后（管理员看全部，成员看自己的与共享给自己的）。 */
export function servicesFromOps(ops) {
  const reg = ops?.registry;
  if (!reg || reg.adapter_attached === false) return null;
  // Registry demo rows are fixtures, not deployed engines or routes.
  if (ops.synthetic === true) return [];
  const list = (reg.engines ?? []).map((e) => ({
    id: e.engine_id,
    kind: 'engine',
    label: e.name,
    engine: e.framework ?? e.protocol ?? null,
    owner: e.owner_name ?? e.owner ?? null,
    runAs: e.run_as_user ?? (Number.isInteger(e.run_as_uid) ? `uid ${e.run_as_uid}` : null),
    port: e.port ?? null,
    self: e.owner_is_self === true,
    runtime: ENGINE_STATUS[e.status] ?? RUNTIME_LABEL.unknown,
    health: e.status === 'verified' ? HEALTH_LABEL.healthy : e.status === 'unavailable' ? HEALTH_LABEL.unreachable : HEALTH_LABEL.unknown,
    share: '—',
    vramMiB: null,
    updatedAt: e.last_seen_at ?? null,
    sharedToMe: false,
  }));
  const rows = (reg.all && reg.all.length ? reg.all : [...(reg.owned ?? [])]);
  const seen = new Set(rows.map((r) => r.deployment_id));
  for (const r of rows) {
    list.push({
      id: r.deployment_id,
      kind: 'deployment',
      label: r.model_label ?? r.model_id ?? r.deployment_id,
      engine: r.engine_type ?? null,
      owner: r.owner_name ?? r.owner_uid ?? null,
      runAs: null,
      port: null,
      self: r.owner_is_self === true,
      runtime: RUNTIME_LABEL[r.runtime_status] ?? RUNTIME_LABEL.unknown,
      health: HEALTH_LABEL[r.health] ?? HEALTH_LABEL.unknown,
      share: SHARE_LABEL[r.share?.mode] ?? '—',
      vramMiB: finite(r.resources?.vram_used_mib) ? r.resources.vram_used_mib : null,
      updatedAt: r.updated_at ?? null,
      sharedToMe: false,
    });
  }
  for (const e of reg.shared ?? []) {
    if (seen.has(e.endpoint_id)) continue;
    list.push({
      id: e.endpoint_id,
      kind: 'endpoint',
      label: e.model ?? e.name,
      engine: e.protocol ?? null,
      owner: e.owner_name ?? e.owner_uid ?? null,
      runAs: null,
      port: null,
      self: false,
      runtime: RUNTIME_LABEL.unknown,
      health: HEALTH_LABEL.unknown,
      share: SHARE_LABEL[e.share?.mode] ?? '—',
      vramMiB: null,
      updatedAt: null,
      sharedToMe: true,
    });
  }
  return list;
}

/** 已登记的模型（模型库）。权重路径只有 owner 才拿得到。 */
export function modelsFromOps(ops) {
  const reg = ops?.registry;
  if (!reg || reg.adapter_attached === false) return null;
  // Do not present fixture catalog entries as models installed on this host.
  if (ops.synthetic === true) return [];
  return (reg.models ?? []).map((m) => ({
    id: m.model_id,
    name: m.name,
    format: m.format ?? null,
    params: m.parameter_count ?? null,
    description: m.description ?? null,
    owner: m.owner_name ?? m.owner ?? null,
    self: m.owner_is_self === true,
    weightPath: m.weight_path ?? null,
    createdAt: m.created_at ?? null,
  }));
}

export function keysFromOps(ops) {
  return (ops?.keys ?? []).map((k) => ({
    id: k.key_id,
    name: k.name,
    prefix: k.prefix,
    scopes: k.scopes ?? [],
    active: k.status === 'active',
    lastUsedAt: k.last_used_at ?? null,
    createdAt: k.created_at ?? null,
  }));
}

/** 每小时请求数序列（24 点，可能含 null）。合成序列不是计量，一律视为未接入。 */
export function hourlyFromOps(ops) {
  if (ops?.synthetic === true || ops?.trends?.synthetic === true) return null;
  const series = (ops?.trends?.series ?? []).find((x) => x.kind === 'requests');
  if (!series || !Array.isArray(series.points)) return null;
  return { points: series.points, scope: series.scope ?? null };
}

/** 主机读数：显存、利用率、各集成服务是否在线。 */
export function hostFacts(host) {
  if (!host) return null;
  const m = host.metrics ?? {};
  const val = (k) => (m[k]?.live && finite(m[k].value) ? m[k].value : null);
  const services = {};
  for (const [key, v] of Object.entries(host.services ?? {})) services[key] = v.live ? v.up === true : null;
  return {
    vramUsedMiB: val('gpu_memory_used_mib'),
    vramTotalMiB: val('gpu_memory_total_mib'),
    vramFreeMiB: val('gpu_memory_free_mib'),
    vramReservedMiB: val('gpu_memory_reserved_mib'),
    utilRatio: val('gpu_utilization_ratio'),
    tempC: val('gpu_temperature_celsius'),
    powerW: val('gpu_power_watts'),
    services,
    observedAt: host.observedAt ?? null,
    source: host.source ?? null,
    runtime: host.runtime ?? null,
  };
}

/** 请求日志 → 表格行、分布、成功率与 P95。 */
export function callsFacts(log) {
  if (!log) return null;
  const rows = log.calls ?? [];
  const byKey = new Map();
  const byModel = new Map();
  let ok = 0;
  for (const r of rows) {
    if (r.outcome === 'success') ok += 1;
    const key = r.key_id ?? '—';
    const model = r.model ?? '—';
    if (!byKey.has(key)) byKey.set(key, new Map());
    const m = byKey.get(key);
    m.set(model, (m.get(model) ?? 0) + 1);
    byModel.set(model, (byModel.get(model) ?? 0) + 1);
  }
  const groups = [...byKey.entries()].map(([key, models]) => ({
    id: key,
    label: key,
    children: [...models.entries()].map(([model, value]) => ({ id: model, label: model, value })),
  }));
  return {
    count: rows.length,
    successRate: rows.length ? ok / rows.length : null,
    p95ms: percentile(rows.map((r) => r.total_ms), 0.95),
    distribution: { innerLabel: 'API 密钥', groups },
    models: [...byModel.entries()].map(([id, value]) => ({ id, label: id, value })).sort((a, b) => b.value - a.value),
    recent: [...rows].sort((a, b) => String(b.ts).localeCompare(String(a.ts))).slice(0, 8).map((r) => ({
      ts: r.ts,
      model: r.model,
      key: r.key_id,
      tokens: finite(r.tokens?.total) ? r.tokens.total : null,
      totalMs: finite(r.total_ms) ? r.total_ms : null,
      status: r.status,
      ok: r.outcome === 'success',
    })),
  };
}

/** 稳定的模型配色：按名称首次出现的顺序分配。 */
export function modelColorMap(names) {
  const map = new Map();
  for (const n of names) if (!map.has(n)) map.set(n, colorAt(map.size));
  return map;
}

/**
 * 概览页视图模型。
 * @param {{ops?:object|null, host?:object|null, log?:object|null, me?:object|null, demo?:object|null}} src
 */
export function overviewModel({ ops = null, host = null, log = null, demo = null } = {}) {
  if (demo) return demo;
  const hf = hostFacts(host);
  const cf = callsFacts(log);
  const services = servicesFromOps(ops);
  // 名称以 BFF 契约（portal-ops/1 metrics.items）为准；principal.* 是本人已验证密钥的计量。
  const reqs = metricValue(ops, 'principal.requests');
  const errs = metricValue(ops, 'principal.errors');
  const p95 = metricValue(ops, 'principal.latency_p95_ms');
  const toMiB = (b) => (finite(b) ? b / 1048576 : null);
  const vramTotal = hf?.vramTotalMiB ?? toMiB(metricValue(ops, 'device.gpu.memory.total_bytes'));
  const vramUsed = hf?.vramUsedMiB ?? toMiB(metricValue(ops, 'device.gpu.memory.used_bytes'));
  const window = ops?.metrics?.window === '24h' ? '近 24 小时' : null;
  // 调用源本身：路径 / 行数 / 最后一条 / 窗口内条数。没有这一段就是「没接线」。
  const feed = ops?.gateway_feed ?? null;
  const requestsFeed = feed
    ? {
      readable: feed.readable !== false,
      events: finite(feed.events) ? feed.events : null,
      inWindow: finite(feed.in_window) ? feed.in_window : null,
      lastTs: typeof feed.last_event_ts === 'string' ? feed.last_event_ts : null,
      unmapped: finite(feed.unmapped) ? feed.unmapped : null,
      keysBound: finite(feed.keys_bound) ? feed.keys_bound : null,
      keysTotal: finite(feed.keys_total) ? feed.keys_total : null,
    }
    : null;
  const engines = services?.filter((x) => x.kind === 'engine') ?? null;
  const online = engines ? engines.filter((x) => x.runtime?.tone === 'ok').length : null;

  const modelNames = [
    ...(cf?.models ?? []).map((m) => m.id),
    ...(services ?? []).map((x) => x.label),
  ];
  const colors = modelColorMap(modelNames);

  // 显存：按部署可见的占用画格，其余已用部分归为「其他进程」。
  let vram = null;
  if (finite(vramTotal)) {
    const parts = (services ?? [])
      .filter((x) => finite(x.vramMiB) && x.vramMiB > 0)
      .map((x) => ({ id: x.id, label: x.label, owner: x.owner, engine: x.engine, mib: x.vramMiB, color: colors.get(x.label) ?? '#8E8E93' }));
    const attributed = parts.reduce((t, p) => t + p.mib, 0);
    if (finite(vramUsed) && vramUsed - attributed > 1) {
      parts.push({ id: '__other', label: parts.length ? '其他进程' : '已分配（进程未归因）', owner: null, engine: null, mib: vramUsed - attributed, color: '#8E8E93' });
    }
    if (finite(hf?.vramReservedMiB) && hf.vramReservedMiB > 0) {
      parts.push({ id: '__reserved', label: '驱动保留', owner: null, engine: null, mib: hf.vramReservedMiB, color: '#B8B8BD' });
    }
    vram = {
      totalMiB: vramTotal,
      usedMiB: finite(vramUsed) ? vramUsed : null,
      freeMiB: finite(hf?.vramFreeMiB) ? hf.vramFreeMiB : null,
      reservedMiB: finite(hf?.vramReservedMiB) ? hf.vramReservedMiB : null,
      parts,
    };
  }

  return {
    source: ops?.synthetic ? 'synthetic' : 'live',
    kpis: {
      requests: finite(reqs) ? reqs : cf ? cf.count : null,
      requestsScope: finite(reqs) ? (window ? `本人 · ${window}` : '本人') : cf ? '本人 · 最近记录' : null,
      requestsFeed,
      servicesOnline: online,
      servicesTotal: engines ? engines.length : null,
      vramUsedMiB: finite(vramUsed) ? vramUsed : null,
      vramTotalMiB: finite(vramTotal) ? vramTotal : null,
      utilRatio: hf?.utilRatio ?? null,
      tempC: hf?.tempC ?? null,
      powerW: hf?.powerW ?? null,
      successRate: finite(reqs) && reqs > 0 && finite(errs) ? 1 - errs / reqs : cf ? cf.successRate : null,
      p95ms: finite(p95) ? p95 : cf ? cf.p95ms : null,
    },
    distribution: cf && cf.distribution.groups.length ? cf.distribution : null,
    modelUsage: cf && cf.models.length ? cf.models : null,
    modelColors: colors,
    hourly: hourlyFromOps(ops),
    vram,
    services,
    keys: ops ? keysFromOps(ops) : null,
    recent: cf ? cf.recent : null,
    host: hf,
  };
}

/* 注册预检（POST /api/v2/registry/engines/preflight，registry-engine-preflight/1）。
 * ok 三态：true 通过 / false 校验失败 / null 本部署没有这条来源（不是失败）。 */
const PREFLIGHT_DETAIL = Object.freeze({
  connected: '端口已响应',
  connection_refused: '连接被拒绝，端口上没有服务在监听',
  timeout: '连接超时',
  verified: '监听进程由管理员为此账户指定的 Linux 用户运行',
  runtime_observed: '已记录监听该端口的 Linux 用户',
  target_registered_by_another_account: '该地址已被其他账户登记',
  not_listening: '该端口没有监听进程',
  ambiguous: '该端口存在多个不同账户的监听进程，无法确定归属',
  unknown_user: '管理员为此账户指定的 Linux 用户在本机不存在',
  target_not_loopback: '服务地址必须为 127.0.0.1',
  proc_net_unreadable: '无法读取本机端口表',
  proc_net_unparseable: '本机端口表格式无法识别',
  model_list_verified: 'GET /v1/models 校验通过',
  transport_unreachable: '接口不可达',
  available: '名称可用',
  engine_name_taken: '您已有同名服务',
  not_configured_in_this_deployment: '本部署未接入此项校验',
  no_ownership_source_in_this_deployment: '本部署未启用归属校验',
});
const PREFLIGHT_ORDER = [['port_reachable', '端口可达'], ['ownership', '运行用户'], ['openai_compat', '接口兼容性'], ['name_unique', '名称唯一性']];

function preflightDetail(raw, evidence) {
  if (typeof raw !== 'string' || !raw) return '—';
  if (raw.startsWith('owner_mismatch:')) return `监听进程不是由管理员为此账户指定的 Linux 用户运行（实际 uid ${raw.slice('owner_mismatch:'.length)}）`;
  if (raw === 'runtime_observed') {
    const who = evidence?.linux_user ?? (Number.isInteger(evidence?.uid) ? `uid ${evidence.uid}` : null);
    return who ? `服务由 Linux 用户 ${who} 运行，已记录` : PREFLIGHT_DETAIL.runtime_observed;
  }
  return PREFLIGHT_DETAIL[raw] ?? raw;
}

export function preflightChecks(data) {
  const checks = data?.checks;
  if (!checks || typeof checks !== 'object') return null;
  const list = PREFLIGHT_ORDER.map(([key, title]) => {
    const c = checks[key];
    const ok = c?.ok === true ? true : c?.ok === false ? false : null;
    return { key, title, ok, detail: c ? preflightDetail(c.detail, c.evidence) : '本部署未接入此项校验' };
  });
  return { list, allOk: data.all_ok === true, blocked: list.some((c) => c.ok === false) };
}
