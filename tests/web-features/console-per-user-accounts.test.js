/*! 「按用户」表必须把「人」和「系统服务账号」分开。
 *
 * 这台机器上 17 个账号里 13 个是发行版建的守护进程账号（polkitd、messagebus、
 * avahi、_chrony…），混在一张表里会让单人开发机看起来像有十几个人在用。这条用例
 * 钉住的是**渲染结果**，不是文案：默认只出现人类账户的行，点开才追加系统账号，
 * 再点收起。数据一行都没丢，只是默认不抢注意力。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

// --- 极小的 DOM 替身 ------------------------------------------------------
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
    this.listeners = {};
    this.style = { setProperty: (k, v) => { this.attrs[`style:${k}`] = v; } };
    this.#text = '';
  }
  #text;
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  append(...kids) { for (const kid of kids) this.children.push(kid); }
  get firstChild() { return this.children[0] ?? null; }
  removeChild(child) { this.children = this.children.filter((x) => x !== child); }
  remove() {}
  getContext() { return new Proxy({}, { get: () => () => {} }); }
  getBoundingClientRect() { return { width: 600, height: 300, top: 0, left: 0 }; }
  set textContent(v) { this.#text = v; }
  get textContent() { return this.#text; }
  focus() {}
}
globalThis.Node = FakeNode;
globalThis.location = { origin: 'http://127.0.0.1:8123', href: 'http://127.0.0.1:8123/#/usage' };
globalThis.window = globalThis;
globalThis.document = {
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
  body: new FakeElement('body'),
};

const HUMAN = [
  { uid: 1002, user: 'fjd', account: 'human', processes: 114, memory_mib: 9800, cpu_ratio: 0.1, vram_mib: 41800 },
  { uid: 1003, user: 'kaidong', account: 'human', processes: 40, memory_mib: 1300, cpu_ratio: 0.1, vram_mib: null },
];
const SYSTEM = [
  { uid: 0, user: 'root', account: 'system', processes: 316, memory_mib: 200, cpu_ratio: 0.0, vram_mib: null },
  { uid: 113, user: 'messagebus', account: 'system', processes: 1, memory_mib: 3, cpu_ratio: 0.0, vram_mib: null },
];

// usage() 会渲染整页（CPU、内存、磁盘、网卡、温度…），所以桩数据要是**形状完整**的，
// 不能只给 per_user —— 缺一个 cpu 字段整页就抛异常，测的就不是折叠这件事了。
function systemPayload(rows) {
  return {
    source: 'proc-verified',
    window_seconds: 30,
    cpu: { cores: 20, load1: 1.0, load5: 1.0, load15: 1.0, utilization_ratio: 0.1, iowait_ratio: 0.0 },
    memory: {
      used_mib: 40000, total_mib: 257000, used_ratio: 0.16, available_mib: 200000,
      swap_used_mib: 1000, swap_total_mib: 8000, swap_used_ratio: 0.12,
    },
    disk: { read_bytes_per_s: 0, write_bytes_per_s: 0, devices: [], usage: [] },
    thermal: { cpu_c: 40, nvme_c: 35, fans_rpm: [] },
    connections: { total: 10, listening: 5, established: 4, time_wait: 1 },
    engine: { reachable: false },
    network: { rx_bytes_per_s: 0, tx_bytes_per_s: 0, interfaces: [] },
    gpu_processes: [],
    per_user: rows,
  };
}

function respond(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function stubFetch(rows) {
  globalThis.fetch = async (path) => {
    if (path === '/api/infra/system') return respond(200, { data: systemPayload(rows) });
    // ops 与 host 在这里不重要，503 让它们走空态。
    return respond(503, { error: { code: 'not_attached', message: 'not attached' } });
  };
}

const { usage } = await import('../../web/console/js/pages.js');

// --- 树遍历 ---------------------------------------------------------------
function walk(node, visit) {
  if (!(node instanceof FakeNode)) return;
  visit(node);
  if (node.children) for (const child of node.children) walk(child, visit);
}

function texts(node) {
  const out = [];
  walk(node, (n) => { if (n instanceof FakeText) out.push(n.text); });
  return out;
}

function elements(page) {
  const out = [];
  walk(page, (n) => { if (n instanceof FakeElement) out.push(n); });
  return out;
}

// 页面上有磁盘、容量、网卡等好几张表，按表头认出「按用户」那一张。
function userTable(page) {
  const found = elements(page).filter((el) => el.tag === 'table')
    .find((el) => { const t = texts(el); return t.includes('账户') && t.includes('进程数'); });
  assert.ok(found, '应该有一张「按用户」的表');
  return found;
}

function bodyRows(page) {
  const body = userTable(page).children.find((c) => c.tag === 'tbody');
  assert.ok(body, '「按用户」表里应该有一个 tbody');
  return body.children.filter((c) => c.tag === 'tr');
}

// 初始状态下按钮的文字是子文本节点，点过一次之后才被 textContent 覆盖 —— 两种都算。
function label(el) { return el.textContent + texts(el).join(''); }

function toggleOf(page) {
  return elements(page).find((el) => el.tag === 'button' && /系统服务账号/.test(label(el))) ?? null;
}

function click(el) {
  const handler = el.listeners.click?.[0];
  assert.ok(handler, '按钮应该挂了 click 处理器');
  handler({ currentTarget: el });
}

// --- 用例 -----------------------------------------------------------------
test('默认只渲染人类账户，系统服务账号不占行', async () => {
  stubFetch(HUMAN.concat(SYSTEM));
  const page = await usage({ demo: false });
  const rows = bodyRows(page);
  assert.equal(rows.length, 2, '默认只应出现 2 个人类账户');
  assert.deepEqual(texts(rows[0]).slice(0, 1), ['fjd']);
  const all = texts(page);
  assert.equal(all.includes('messagebus'), false, '系统账号默认不出现');
  assert.equal(all.includes('root'), false, 'root 是系统账号，默认也不出现');
});

test('摘要说清楚「几个人类账户 · 几个系统服务账号」', async () => {
  stubFetch(HUMAN.concat(SYSTEM));
  const page = await usage({ demo: false });
  const all = texts(page).join(' ');
  assert.match(all, /2 个人类账户/);
  assert.match(all, /2 个系统服务账号/);
  assert.match(all, /显示系统服务账号（2 个）/);
});

test('点开追加系统账号，再点收起 —— 是增删行，不是藏一个分区', async () => {
  stubFetch(HUMAN.concat(SYSTEM));
  const page = await usage({ demo: false });
  const toggle = toggleOf(page);
  assert.ok(toggle);

  click(toggle);
  assert.equal(bodyRows(page).length, 4);
  assert.equal(texts(page).includes('messagebus'), true);
  assert.equal(toggle.textContent, '收起系统服务账号');

  click(toggle);
  assert.equal(bodyRows(page).length, 2);
  assert.equal(texts(page).includes('messagebus'), false);
  assert.equal(toggle.textContent, '显示系统服务账号（2 个）');
});

test('旧后端没有 account 字段时按 UID 判，不按用户名猜', async () => {
  const legacy = HUMAN.concat(SYSTEM).map(({ account, ...rest }) => rest);
  legacy.push({ uid: 65534, user: 'nobody', processes: 1, memory_mib: 0, cpu_ratio: 0.0, vram_mib: null });
  stubFetch(legacy);
  const page = await usage({ demo: false });
  assert.equal(bodyRows(page).length, 2, 'fjd / kaidong 的 uid ≥ 1000，仍算人类账户');
  assert.equal(texts(page).includes('nobody'), false, '65534 归系统账号，即使没有 account 字段');
});

test('没有人类账户时不做折叠，否则整块会空掉', async () => {
  stubFetch([SYSTEM[0]]);
  const page = await usage({ demo: false });
  assert.equal(bodyRows(page).length, 1, '只有系统账号时原样列出，不能被折没');
  assert.equal(toggleOf(page), null, '没有可折叠的对象就不该出现按钮');
});
