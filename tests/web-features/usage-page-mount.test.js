/*! tests/web-features/usage-page-mount.test.js — 资源监控页真的画得出来。
 *
 *  这一支补的是「只读源码」测不到的那一半：页面把三份读数拼起来的时候会不会踩空。
 *  最有价值的两条：
 *
 *    * 三份读数**全部**取不到时，页面仍然画出标题、刷新频率和「没取到 …」，
 *      而不是白屏或者一个假读数；
 *    * 只坏一份时，其余照常显示，状态行点名坏的那一份 —— 「这台机器没有 GPU」
 *      与「GPU 读数这次没取到」必须能分开读。
 *
 *  为了在没有浏览器的环境里跑，这里用一个几百字节的假 DOM（只实现 dom.js 用到的
 *  那些方法）。它不是浏览器，也不假装是：它证明的是「这条渲染路径不抛错」，
 *  版式与样式不在这里的价值范围内。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

/* ---------------- 假 DOM ---------------- */
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) { super(); this.text = text; this.parentNode = null; }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tagName = tag;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this._text = '';
    this.style = { setProperty: (key, value) => { this.attributes[`style:${key}`] = value; } };
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() { /* 测试里不派发事件 */ }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? new FakeText(node) : node;
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) { this.childNodes.splice(index, 1); node.parentNode = null; }
    return node;
  }
  get firstChild() { return this.childNodes[0] ?? null; }
  set textContent(value) { this.childNodes = []; this._text = value; }
  get textContent() { return this._text; }
  get isConnected() {
    let node = this;
    while (node) { if (node === document.documentElement) return true; node = node.parentNode; }
    return false;
  }
}

const root = new FakeElement('body');
globalThis.Node = FakeNode;
globalThis.document = {
  documentElement: root,
  visibilityState: 'visible',
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
};

function textOf(node, out = []) {
  if (node instanceof FakeText) out.push(node.text);
  if (node._text) out.push(node._text);
  for (const child of node.childNodes ?? []) textOf(child, out);
  return out.join(' ');
}

const { usage } = await import('../../web/console/js/pages.js');
const { SNAP_SYSTEM } = await import('../../web/console/js/snapshot.js');
const OPS = JSON.parse(readFileSync(join(here, 'fixtures', 'bff-ops-payloads.json'), 'utf8')).admin_private;

/** 「暂停」：这一支不测定时器（那在 live-refresh.test.js），所以不让它排拍。 */
globalThis.localStorage = { getItem: () => '0', setItem: () => {} };

const reachable = new Set();
globalThis.fetch = async (path) => (reachable.has(path)
  ? new Response(JSON.stringify(
    path === '/api/v2/ops' ? { data: { ops: OPS } } : { data: SNAP_SYSTEM },
  ), { status: 200 })
  : new Response(JSON.stringify({ error: { code: 'not_configured' } }), { status: 503 }));

const ctx = { demo: false, me: { linux_user: 'fjd', username: 'fjd' } };

test('三份读数全取不到时，页面照样画出来并逐条点名', async () => {
  reachable.clear();
  const page = await usage(ctx);

  const text = textOf(page);
  assert.match(text, /资源监控/);
  assert.match(text, /已暂停/, '状态行说清现在没有自动刷新');
  assert.match(text, /这次没取到 服务清单（未接入，这一块还是空的）、GPU 读数（未接入，这一块还是空的）、主机读数（未接入，这一块还是空的）/);
  assert.doesNotMatch(text, /最后更新/, '一份都没成功过，就不能写「最后更新」');
  assert.match(text, /暂无 GPU 读数/, '取不到就是「暂无」，不是 0');
});

test('只坏一份时其余照常显示，状态行只点名坏的那一份', async () => {
  reachable.clear();
  reachable.add('/api/v2/ops');
  reachable.add('/api/infra/system');
  const page = await usage(ctx);

  const text = textOf(page);
  assert.match(text, /这次没取到 GPU 读数（未接入，这一块还是空的）/);
  assert.doesNotMatch(text, /服务清单（未接入/, '服务清单取到了，不该被一起报成失败');
  assert.match(text, /最后更新 \d\d:\d\d:\d\d/, '另外两块刚刚更新过，状态行要说出来');
  for (const block of ['显存分配', '按用户看资源', '磁盘', '各推理服务资源占用']) {
    assert.ok(text.includes(block), `缺了「${block}」这一块`);
  }
});

test('没存过偏好的浏览器：页面自己按 1 秒排拍，状态行写「每秒」', async () => {
  const delays = [];
  const realSet = globalThis.setTimeout;
  // 把定时器换成只记录不执行：既能断言「排了一拍 1 秒」，又不会让测试进程挂着。
  globalThis.setTimeout = (fn, ms) => { delays.push(ms); return 0; };
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  try {
    const page = await usage(ctx);
    assert.ok(delays.includes(1000), `页面没有按 1 秒排拍，实际排了 ${JSON.stringify(delays)}`);
    assert.match(textOf(page), /自动刷新：每秒/);
  } finally {
    globalThis.setTimeout = realSet;
    globalThis.localStorage = { getItem: () => '0', setItem: () => {} };
  }
});

test('演示数据不挂定时器，也不出现刷新频率控件', async () => {
  const page = await usage({ ...ctx, demo: true });

  const text = textOf(page);
  assert.match(text, /资源监控/);
  assert.doesNotMatch(text, /自动刷新|已暂停/, '演示数据是常量，不该有刷新节奏');
});
