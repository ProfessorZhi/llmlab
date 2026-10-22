/*! 概览页「平台登记记录」那张紧凑表必须自报类型。
 *
 * 概览页把引擎、部署、端点合并成一张表。三者长得几乎一样：同名模型既是
 * 「部署」又会出现「引擎」行，读者第一眼会以为重复登记。表里必须有「类型」
 * 一列，把三件事分开 —— 这条用例钉住它，以及「类型列只在紧凑表出现」（服务页
 * 已经按类型分了三个 panel，再列一列就是噪音）。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// --- 极小的 DOM 替身：dom.js 的 h() 只用这四个入口 -----------------------
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) { super(); this.text = text; }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.style = { setProperty: (k, v) => { this.attrs[`style:${k}`] = v; } };
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  append(child) { this.children.push(child); }
}
globalThis.Node = FakeNode;
// pages.js 在模块顶层读 location.origin 拼对外端点；补一个即可，不为它改生产代码。
globalThis.location = { origin: 'http://127.0.0.1:8123', href: 'http://127.0.0.1:8123/#/overview' };
globalThis.window = globalThis;
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
};

const { servicesTable } = await import('../../web/console/js/pages.js');

const ROWS = [
  { id: 'eng_1', kind: 'engine', label: 'strata-qwen38-flash-next', engine: 'strata', owner: 'fjd', runAs: null, port: 8080, self: false, runtime: { tone: 'ok', text: '运行中' }, health: { tone: 'ok', text: '健康' }, share: '—', vramMiB: null },
  { id: 'dep_1', kind: 'deployment', label: 'qwen3.8-flash-next-iq2_xs', engine: null, owner: 'fjd', runAs: null, port: null, self: false, runtime: { tone: 'off', text: '状态未知' }, health: { tone: 'off', text: '未知' }, share: 'all', vramMiB: null },
];

/** 表头文字，按出现顺序。 */
function headers(table) {
  const thead = table.children[0];
  const tr = thead.children[0];
  return tr.children.map((th) => th.children.map((c) => c.text ?? '').join(''));
}

/** 第 index 行的单元格文字。 */
function cells(table, index) {
  const tr = table.children[1].children[index];
  return tr.children.map((td) => td.children.map((c) => c.text ?? '').join(''));
}

const findTable = (node) => {
  if (node.tag === 'table') return node;
  for (const child of node.children ?? []) {
    const hit = findTable(child);
    if (hit) return hit;
  }
  return null;
};

test('紧凑表带「类型」列，引擎与部署分得开', () => {
  const table = findTable(servicesTable(ROWS, { compact: true }));
  assert.ok(table, '没渲染出 table');
  const head = headers(table);
  assert.ok(head.includes('类型'), `表头缺「类型」：${head.join('/')}`);
  const kindCol = head.indexOf('类型');
  assert.equal(cells(table, 0)[kindCol], '引擎');
  assert.equal(cells(table, 1)[kindCol], '部署');
});

test('非紧凑表不加类型列（服务页已按类型分 panel）', () => {
  const table = findTable(servicesTable(ROWS));
  assert.ok(!headers(table).includes('类型'));
});

test('未知类型不编造，显示破折号', () => {
  const table = findTable(servicesTable([{ ...ROWS[0], kind: 'mystery' }], { compact: true }));
  const kindCol = headers(table).indexOf('类型');
  assert.equal(cells(table, 0)[kindCol], '—');
});
