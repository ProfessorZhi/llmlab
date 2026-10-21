/*! web/console/js/demo.js — 演示数据（仅在 ?demo=1 时使用）。
 *  所有数值都是确定性合成的示例，页面顶部始终显示「演示数据」提示；
 *  任何真实部署的默认路径都不会加载本模块的数据。 */
import { RUNTIME_LABEL, HEALTH_LABEL, modelColorMap } from './viewmodel.js';

const MODELS = ['Qwen2.5-32B-Instruct', 'Llama-3.1-8B-Instruct', 'DeepSeek-R1-Distill-14B', 'bge-m3', 'qwen2.5-7b-sft-v3'];

export const DEMO_USERS = Object.freeze([
  { id: 'u-admin', username: 'admin', display_name: '系统管理员', linux_user: 'fjd', role: 'admin', status: 'active', last_login_at: '2026-10-21T06:30:00Z', created_at: '2026-10-01T00:00:00Z' },
  { id: 'u-zhang', username: 'zhang', display_name: '张伟', linux_user: 'zhang', role: 'member', status: 'active', last_login_at: '2026-10-21T06:18:00Z', created_at: '2026-10-02T00:00:00Z' },
  { id: 'u-li', username: 'li', display_name: '李娜', linux_user: 'li', role: 'member', status: 'active', last_login_at: '2026-10-20T13:40:00Z', created_at: '2026-10-02T00:00:00Z' },
  { id: 'u-wang', username: 'wang', display_name: '王磊', linux_user: 'wang', role: 'member', status: 'active', last_login_at: '2026-10-18T09:00:00Z', created_at: '2026-10-03T00:00:00Z' },
  { id: 'u-chen', username: 'chen', display_name: '陈静', linux_user: 'chen', role: 'member', status: 'active', last_login_at: null, created_at: '2026-10-19T00:00:00Z' },
]);

export const DEMO_ME = Object.freeze({ id: 'u-zhang', username: 'zhang', display_name: '张伟', linux_user: 'zhang', role: 'admin', must_change_password: false });

function seeded(seed) {
  let state = seed >>> 0;
  return () => { state = (state * 1664525 + 1013904223) >>> 0; return state / 0x100000000; };
}

export function demoServices() {
  return [
    { id: 'dep-q32', label: MODELS[0], engine: 'strata', owner: 'zhang', self: true, runtime: RUNTIME_LABEL.running, health: HEALTH_LABEL.healthy, share: '全部成员', vramMiB: 18432, port: 18001 },
    { id: 'dep-l8', label: MODELS[1], engine: 'vllm', owner: 'li', self: false, runtime: RUNTIME_LABEL.running, health: HEALTH_LABEL.healthy, share: '全部成员', vramMiB: 6554, port: 18002 },
    { id: 'dep-d14', label: MODELS[2], engine: 'vllm', owner: 'wang', self: false, runtime: RUNTIME_LABEL.running, health: HEALTH_LABEL.degraded, share: '指定成员', vramMiB: 5325, port: 18003 },
    { id: 'dep-bge', label: MODELS[3], engine: 'tei', owner: 'chen', self: false, runtime: RUNTIME_LABEL.stopped, health: HEALTH_LABEL.unknown, share: '全部成员', vramMiB: null, port: 18004 },
    { id: 'dep-sft', label: MODELS[4], engine: 'vllm', owner: 'zhang', self: true, runtime: RUNTIME_LABEL.starting, health: HEALTH_LABEL.unknown, share: '仅本人', vramMiB: 1843, port: 18009 },
  ];
}

export function demoKeys() {
  return [
    { id: 'tok_3f9a', name: 'paper-experiments', prefix: 'llk_••••3f9a', scopes: ['models.invoke'], active: true, lastUsedAt: '2026-10-21T06:29:00Z', createdAt: '2026-10-05T00:00:00Z' },
    { id: 'tok_81c2', name: 'eval-pipeline', prefix: 'llk_••••81c2', scopes: ['models.invoke', 'evaluation.submit'], active: true, lastUsedAt: '2026-10-20T11:02:00Z', createdAt: '2026-10-08T00:00:00Z' },
    { id: 'tok_0b77', name: 'old-notebook', prefix: 'llk_••••0b77', scopes: ['models.invoke'], active: false, lastUsedAt: '2026-09-30T08:00:00Z', createdAt: '2026-09-01T00:00:00Z' },
  ];
}

export function demoCalls(now = Date.parse('2026-10-21T06:32:00Z')) {
  const rand = seeded(7);
  const keys = ['paper-experiments', 'eval-pipeline'];
  const rows = [];
  for (let i = 0; i < 40; i += 1) {
    const model = MODELS[Math.floor(rand() * 3)];
    const ok = rand() > 0.06;
    const total = Math.round(300 + rand() * 2200);
    rows.push({
      ts: new Date(now - i * 97_000).toISOString(),
      model: ok ? model : MODELS[3],
      key: keys[Math.floor(rand() * 2)],
      tokens: ok ? Math.round(400 + rand() * 3000) : null,
      totalMs: ok ? total : null,
      status: ok ? 200 : 503,
      ok,
    });
  }
  return rows;
}

export function demoOverview() {
  const services = demoServices();
  const colors = modelColorMap(MODELS);
  const users = [
    { id: 'zhang', label: '张伟', parts: [[MODELS[0], 5200], [MODELS[3], 2200]] },
    { id: 'li', label: '李娜', parts: [[MODELS[0], 1800], [MODELS[1], 3000]] },
    { id: 'wang', label: '王磊', parts: [[MODELS[2], 3600]] },
    { id: 'chen', label: '陈静', parts: [[MODELS[1], 1400], [MODELS[3], 1040]] },
  ];
  const groups = users.map((u) => ({ id: u.id, label: u.label, children: u.parts.map(([m, v]) => ({ id: m, label: m, value: v })) }));
  const usage = new Map();
  for (const g of groups) for (const ch of g.children) usage.set(ch.id, (usage.get(ch.id) ?? 0) + ch.value);
  const total = [...usage.values()].reduce((a, b) => a + b, 0);
  const vramParts = services.filter((x) => x.vramMiB).map((x) => ({ id: x.id, label: x.label, owner: x.owner, engine: x.engine, mib: x.vramMiB, color: colors.get(x.label) }));
  return {
    source: 'demo',
    kpis: {
      requests: total, requestsScope: '今日', requestsDelta: 0.124,
      servicesOnline: services.filter((x) => x.runtime === RUNTIME_LABEL.running).length,
      servicesTotal: services.length,
      vramUsedMiB: vramParts.reduce((t, p) => t + p.mib, 0), vramTotalMiB: 49152,
      utilRatio: 0.63, tempC: 61, powerW: 212,
      successRate: 0.993, p95ms: 1620,
    },
    distribution: { innerLabel: '用户', groups },
    modelUsage: [...usage.entries()].map(([id, value]) => ({ id, label: id, value })).sort((a, b) => b.value - a.value),
    modelColors: colors,
    hourly: { points: [120, 80, 60, 40, 30, 40, 90, 260, 520, 810, 940, 1010, 880, 960, 1180, 1320, 1240, 1100, 900, 760, 640, 520, 410, 300], synthetic: true, scope: 'lab' },
    vram: { totalMiB: 49152, usedMiB: vramParts.reduce((t, p) => t + p.mib, 0), parts: vramParts },
    services,
    keys: demoKeys(),
    recent: demoCalls().slice(0, 6),
    host: { vramUsedMiB: 32154, vramTotalMiB: 49152, utilRatio: 0.63, tempC: 61, powerW: 212, services: { prometheus: true, gateway: true, grafana: true, evalscope: true, strata: true } },
  };
}

export function demoEvaluation() {
  return [
    { run_id: 'run-101', model_id: MODELS[0], dataset: 'C-Eval', status: 'done', metric: 0.812, metric_unit: 'acc', created_at: '2026-10-20T02:00:00Z' },
    { run_id: 'run-102', model_id: MODELS[1], dataset: 'C-Eval', status: 'done', metric: 0.574, metric_unit: 'acc', created_at: '2026-10-20T02:10:00Z' },
    { run_id: 'run-103', model_id: MODELS[2], dataset: 'GSM8K', status: 'running', metric: null, metric_unit: 'acc', created_at: '2026-10-21T05:40:00Z' },
    { run_id: 'run-104', model_id: MODELS[0], dataset: 'GSM8K', status: 'done', metric: 0.901, metric_unit: 'acc', created_at: '2026-10-19T08:00:00Z' },
  ];
}
