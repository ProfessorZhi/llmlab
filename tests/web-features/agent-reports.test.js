/*! tests/web-features/agent-reports.test.js —— 代理上报的表格行由什么决定。
 *
 *  ``flattenAgentReports`` 是这一页唯一的纯函数：它把 ``/discovery`` 信封里的
 *  ``agent_reports`` 摊平成一行一件事。这里钉的是它**不许**骗人的几处：
 *
 *    1. 归属字段（用户、uid、主机、代理、时间）来自报告本身，不从条目里猜；
 *    2. 「可登记」是**服务端的判定**。代理报上来的端口不在允许列表里时，
 *       页面必须照实写「端口不在允许列表」，不能替用户写成可点；
 *    3. 过期与不完整是两件不同的事，各挂在自己的标志上，不互相顶替；
 *    4. 形状不对（不是数组、条目不是对象）时给空表，不抛；
 *    5. 不在允许列表里的端口**折叠成一行**（数量 + 端口照列），不逐条占位 ——
 *       它们仍然可见，只是不再把真正可登记的推理服务挤出视野。
 *
 *  这里不画 DOM，也不碰网络：``agentReportsPanel`` 的接线另有
 *  ``console-loaders.test.js`` 那一类测试看着。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { flattenAgentReports } from '../../web/console/js/pages.js';

/** 一份最小但完整的报告：每处用例只改自己关心的字段。 */
function report(overrides = {}) {
  return {
    principal: 'linux:lab-01:1001',
    agent_id: 'lab-01',
    host_id: 'lab-01',
    linux_user: 'li',
    uid: 1001,
    received_at: '2026-10-26T09:00:00+00:00',
    age_seconds: 240,
    stale: false,
    full: true,
    engines: [],
    installed_engines: [],
    models: [],
    problems: [],
    rejected: {},
    ...overrides,
  };
}

function envelope(...reports) {
  return { agent_reports: reports, agent_reports_scope: 'all' };
}

test('一条引擎：归属来自报告，细节是框架加模型名', () => {
  const rows = flattenAgentReports(envelope(report({
    engines: [{ port: 8001, framework: 'vllm', models: ['Qwen2.5-14B-Instruct'], registrable: true, registered: false }],
  })));

  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0], {
    user: 'li', uid: 1001, host: 'lab-01', agent: 'lab-01',
    receivedAt: '2026-10-26T09:00:00+00:00', stale: false, full: true,
    kind: 'engine',
    object: '端口 8001',
    detail: 'vllm · Qwen2.5-14B-Instruct',
    state: '可登记',
  });
});

test('端口不在允许列表里就照实说，不装成可登记', () => {
  const [row] = flattenAgentReports(envelope(report({
    engines: [{ port: 9999, framework: 'llama.cpp', models: [], registrable: false }],
  })));

  assert.equal(row.state, '端口不在允许列表');
});

test('已登记的引擎压过「不可登记」：登记过就是登记过', () => {
  const [row] = flattenAgentReports(envelope(report({
    engines: [{ port: 8001, framework: 'vllm', models: [], registrable: false, registered: true }],
  })));

  assert.equal(row.state, '已登记');
});

test('非允许端口折叠成一行：数量与端口都还在，只是不逐条占位', () => {
  const rows = flattenAgentReports(envelope(report({
    engines: [
      { port: 8000, framework: 'llama.cpp', registrable: true },
      { port: 8081, framework: null, registrable: false },
      { port: 9000, framework: null, registrable: false },
      { port: 22, framework: null, registrable: false },
    ],
  })));

  assert.deepEqual(rows.map((row) => row.kind), ['engine', 'folded']);
  assert.equal(rows[0].object, '端口 8000');
  assert.equal(rows[1].object, '另有 3 个监听端口（不在允许列表）');
  assert.equal(rows[1].detail, '8081、9000、22');
  assert.equal(rows[1].state, '端口不在允许列表');
  // 折叠行仍然带着这份报告的归属：它不属于任何别的报告。
  assert.equal(rows[1].user, 'li');
  assert.equal(rows[1].agent, 'lab-01');
});

test('折叠行最多列 6 个端口，再多就只给数量', () => {
  const engines = Array.from({ length: 9 }, (_, index) => ({
    port: 9000 + index, framework: null, registrable: false,
  }));
  const [row] = flattenAgentReports(envelope(report({ engines })));

  assert.equal(row.object, '另有 9 个监听端口（不在允许列表）');
  assert.equal(row.detail, '9000、9001、9002、9003、9004、9005…');
});

test('全是允许列表里的端口时没有折叠行', () => {
  const rows = flattenAgentReports(envelope(report({
    engines: [{ port: 8000, registrable: true }, { port: 8001, registrable: true, registered: true }],
  })));

  assert.deepEqual(rows.map((row) => row.kind), ['engine', 'engine']);
});

test('没有模型名时不编一个出来', () => {
  const [row] = flattenAgentReports(envelope(report({
    engines: [{ port: 8001, framework: null, models: [], registered: false }],
  })));

  assert.equal(row.detail, '—');
});

test('已安装引擎：状态是发现方式的中文说法，细节是版本与路径', () => {
  const [row] = flattenAgentReports(envelope(report({
    installed_engines: [{
      engine: 'llama.cpp', name: 'llama.cpp', found_as: 'executable',
      version: 'b4120', path: '/usr/local/bin/llama-server', env: null,
    }],
  })));

  assert.equal(row.kind, 'installed');
  assert.equal(row.object, 'llama.cpp');
  assert.equal(row.detail, 'b4120 · /usr/local/bin/llama-server');
  assert.equal(row.state, '可执行文件');
});

test('认不出的发现方式原样出现，不硬翻成中文', () => {
  const [row] = flattenAgentReports(envelope(report({
    installed_engines: [{ engine: 'x', name: 'x', found_as: 'flatpak', path: '/x' }],
  })));

  assert.equal(row.state, 'flatpak');
});

test('模型：路径是对象本身，细节区分 GGUF 与 Hugging Face', () => {
  const [gguf, hf] = flattenAgentReports(envelope(report({
    models: [
      { path: '/data/models/m.gguf', format: 'gguf', size_bytes: 4 * 1024 ** 3, architecture: 'LlamaForCausalLM', registered: false },
      { path: '/opt/models/bge-m3', format: 'hf', size_bytes: 2.2 * 1024 ** 3, architecture: 'XLMRobertaModel', registered: true },
    ],
  })));

  assert.equal(gguf.kind, 'model');
  assert.equal(gguf.object, '/data/models/m.gguf');
  assert.equal(gguf.detail, 'GGUF · 4.0 GB · LlamaForCausalLM');
  assert.equal(gguf.state, '可登记');

  assert.equal(hf.detail, 'Hugging Face · 2.2 GB · XLMRobertaModel');
  assert.equal(hf.state, '已登记');
});

test('一份报告的三类事实按引擎 → 已安装 → 模型的顺序摊开', () => {
  const rows = flattenAgentReports(envelope(report({
    engines: [{ port: 8001 }],
    installed_engines: [{ name: 'vllm' }],
    models: [{ path: '/m' }],
  })));

  assert.deepEqual(rows.map((row) => row.kind), ['engine', 'installed', 'model']);
  // 每一行都带着同一份归属，表格里才不用回头找报告。
  for (const row of rows) {
    assert.equal(row.user, 'li');
    assert.equal(row.uid, 1001);
    assert.equal(row.host, 'lab-01');
  }
});

test('过期与不完整各挂各的：过期不掩盖不完整', () => {
  const [row] = flattenAgentReports(envelope(report({
    stale: true, full: false, models: [{ path: '/m', format: 'hf' }],
  })));

  assert.equal(row.stale, true);
  assert.equal(row.full, false);
});

test('没有 full 字段时按完整处理 —— 缺省不该被读成「不完整」', () => {
  const data = envelope(report({ models: [{ path: '/m' }] }));
  delete data.agent_reports[0].full;

  const [row] = flattenAgentReports(data);

  assert.equal(row.full, true);
});

test('两份报告各摊各的，不合并成一行', () => {
  const rows = flattenAgentReports(envelope(
    report({ uid: 1001, linux_user: 'li', engines: [{ port: 8001 }] }),
    report({ uid: 1002, linux_user: 'wang', agent_id: 'lab-02', models: [{ path: '/m' }] }),
  ));

  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => [row.user, row.uid, row.agent]), [
    ['li', 1001, 'lab-01'],
    ['wang', 1002, 'lab-02'],
  ]);
});

test('缺归属字段时留空位，不拿 0 或空串顶替', () => {
  const [row] = flattenAgentReports(envelope(report({
    linux_user: null, host_id: null, agent_id: null, received_at: null, engines: [{ port: 8000 }],
  })));

  assert.equal(row.user, '—');
  assert.equal(row.host, '—');
  assert.equal(row.agent, '—');
  assert.equal(row.receivedAt, null);
});

test('不知道有没有东西可报时给空表，不抛', () => {
  assert.deepEqual(flattenAgentReports(undefined), []);
  assert.deepEqual(flattenAgentReports(null), []);
  assert.deepEqual(flattenAgentReports({}), []);
  assert.deepEqual(flattenAgentReports({ agent_reports: null }), []);
  assert.deepEqual(flattenAgentReports({ agent_reports: 'oops' }), []);
});

test('报告里没有清单字段时它只是不贡献行', () => {
  const bare = report();
  delete bare.engines;
  delete bare.installed_engines;
  delete bare.models;

  assert.deepEqual(flattenAgentReports(envelope(bare)), []);
});
