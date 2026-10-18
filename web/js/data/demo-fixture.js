/*! web/js/data/demo-fixture.js
 *  SYNTHETIC demo fixture — the only data the Atlas shows by default.
 *
 *  Every record here is invented for interaction design. It is NOT Gateway
 *  usage, NOT EvalScope output, NOT Prometheus. Nine workloads, 1,248 calls and
 *  482k output tokens over the 24h window, matching prototypes/v5.
 *
 *  `p95` is per-workload only. There is deliberately no "overall p95": you
 *  cannot average percentiles across different request sets, and the prototype
 *  shows "—" rather than a fabricated aggregate. Do not add one.
 */

/** @typedef {{id:string,g:string,name:string,calls:number,p95:number,tokens:number,desc:string}} Workload */

/** @type {ReadonlyArray<Workload>} */
export const DEMO_WORKLOADS = Object.freeze([
  { id: 'chat', g: 'conversation', name: '日常对话', calls: 320, p95: 680, tokens: 115, desc: '常规聊天与知识问答的示意流量' },
  { id: 'resume', g: 'conversation', name: '简历解析', calls: 210, p95: 1420, tokens: 88, desc: '长文本与多页文档处理示意' },
  { id: 'vision', g: 'conversation', name: '视觉问答', calls: 160, p95: 1180, tokens: 71, desc: '多模态业务类型，仅作演示，不代表真实能力验收' },
  { id: 'code', g: 'developer', name: '编码协作', calls: 142, p95: 920, tokens: 69, desc: '代码补全与长轮次协作示意' },
  { id: 'tools', g: 'developer', name: '工具调用', calls: 126, p95: 820, tokens: 49, desc: '结构化工具调用的演示流量' },
  { id: 'ci', g: 'developer', name: 'CI 助手', calls: 108, p95: 1560, tokens: 33, desc: '持续集成工作流的演示流量' },
  { id: 'eval', g: 'evaluation', name: '离线测评', calls: 74, p95: 2070, tokens: 24, desc: '合成的评测请求，并非真实 EvalScope Job' },
  { id: 'regression', g: 'evaluation', name: '回归验收', calls: 60, p95: 1230, tokens: 20, desc: '稳定性回归模拟负载' },
  { id: 'stress', g: 'evaluation', name: '压力测试', calls: 48, p95: 2380, tokens: 13, desc: '仅是界面模拟数据，没有执行真实压测' },
]);

/** Business-direction groupings. `color` names a CSS custom property. */
export const DEMO_GROUPS = Object.freeze([
  { id: 'conversation', name: '交互与内容', color: '--atlas-a' },
  { id: 'developer', name: '开发自动化', color: '--atlas-b' },
  { id: 'evaluation', name: '实验评测', color: '--atlas-c' },
]);

/**
 * Static multipliers that turn the 24h fixture into wider windows. They are a
 * display transform of the SAME synthetic sample — not history. Labelled as a
 * synthetic window everywhere they surface.
 */
export const RANGE_SCALES = Object.freeze({ '24h': 1, '7d': 5.3, '30d': 22.7 });
export const RANGE_NAMES = Object.freeze({ '24h': '24 小时', '7d': '7 天', '30d': '30 天' });

/** The provenance stamp carried into every export and the source banner. */
export const DEMO_SOURCE = Object.freeze({
  kind: 'synthetic-ui-fixture',
  status: 'DEMO_ONLY',
  note: 'Per-workload p95 only — never aggregate percentiles by averaging.',
});
