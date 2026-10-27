/*! tests/web-features/services-running-only.test.js — 推理服务页默认只列**正在跑**的服务。
 *
 * 用户的原话：「服务 这里应该 只显示运行着的啊」。这条规则落在两处，缺一条就只是
 * 换了个说法：
 *
 *   * viewmodel 给每一行一个 ``running`` —— 此刻有没有证据说这个端口上有进程在监听；
 *   * 页面默认按它过滤，并把「另有几个没在跑」写在标题旁边，再给一个开关。
 *
 * 开关不是装饰：停掉的服务**还在登记里**，要能点进去重新探测、重新启动。直接从
 * 列表里删掉，等于让「不在列表里」和「不存在」变成同一件事。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

class Element {
  constructor(tag) { this.tagName = tag; this.childNodes = []; this.attributes = {}; this.events = {}; this.style = { setProperty() {} }; }
  append(...nodes) { for (const n of nodes) { n.parentNode = this; this.childNodes.push(n); } }
  removeChild(n) { this.childNodes.splice(this.childNodes.indexOf(n), 1); n.parentNode = null; }
  remove() { this.parentNode?.removeChild(this); }
  get children() { return this.childNodes.filter(n => n.tagName !== '#text'); }
  contains(n) { return n === this || this.childNodes.some(c => c.contains(n)); }
  replaceWith(n) { const siblings = this.parentNode.childNodes; siblings[siblings.indexOf(this)] = n; n.parentNode = this.parentNode; this.parentNode = null; }
  get firstChild() { return this.childNodes[0]; }
  set textContent(v) { this.childNodes = []; this.text = v; this._text = v; }
  get textContent() { return this._text !== undefined && !this.childNodes.length ? this._text : [this.text ?? '', ...this.childNodes.map(n => n.textContent)].join(''); }
  setAttribute(k, v) { this.attributes[k] = v; }
  addEventListener(k, fn) { this.events[k] = fn; }
  get isConnected() { return false; }
  querySelectorAll() { return []; }
}
globalThis.Node = Element;
globalThis.document = {
  createElement: t => new Element(t),
  createElementNS: (_, t) => new Element(t),
  createTextNode: t => { const e = new Element('#text'); e.text = t; return e; },
  visibilityState: 'visible',
};

const { services } = await import('../../web/console/js/pages.js');

const BASE = JSON.parse(readFileSync(new URL('./fixtures/bff-ops-payloads.json', import.meta.url))).admin_private;
const MINUTE = 60_000;
const ago = ms => new Date(Date.now() - ms).toISOString();

/** 一行引擎夹具：从真夹具那一行抄字段，只改决定「在不在跑」的那几个。 */
const TEMPLATE = BASE.registry.engines[0];
const engine = (id, name, extra = {}) => ({
  ...TEMPLATE,
  engine_id: id, name,
  status: 'verified', health: 'healthy', health_reason: null, health_observed_at: ago(MINUTE),
  last_seen_at: ago(MINUTE), state: 'attached', ...extra,
});

function opsWith(engines) {
  const data = structuredClone(BASE);
  data.synthetic = false;
  data.trends = null;
  data.registry.engines = engines;
  return data;
}

const ready = ops => new Response(JSON.stringify({ data: { ops } }));
const turn = () => new Promise(r => setImmediate(r));

/** 在页面里按 class 找那个开关（页头上唯一带 aria-pressed 的按钮）。 */
function findToggle(root) {
  const stack = [root];
  while (stack.length) {
    const node = stack.pop();
    if (node.tagName === 'button' && node.attributes['aria-pressed'] !== undefined) return node;
    stack.push(...node.childNodes);
  }
  return null;
}

test('默认只列在跑的服务，并把「另有几个没在跑」写在标题旁边', async () => {
  globalThis.fetch = async () => ready(opsWith([
    engine('eng_live', 'svc-live'),
    engine('eng_dead', 'svc-dead', { status: 'unavailable', health: 'unreachable', state: 'offline', last_seen_at: ago(3 * 24 * 60 * MINUTE) }),
  ]));
  const page = await services({ demo: false });
  await turn();
  try {
    assert.match(page.textContent, /svc-live/);
    assert.doesNotMatch(page.textContent, /svc-dead/, '停掉的服务不该混在「推理服务」里冒充一个服务');
    assert.match(page.textContent, /1 个在运行 · 另有 1 个未在运行/);
    const toggle = findToggle(page);
    assert.ok(toggle, '必须给一个能看见它们的开关');
    assert.equal(toggle.hidden, false);
    assert.match(toggle.textContent, /显示未运行的（1）/);
  } finally {
    page.dispose?.();
  }
});

test('打开开关后停掉的服务重新出现，按钮改口「只看运行中的」', async () => {
  globalThis.fetch = async () => ready(opsWith([
    engine('eng_live', 'svc-live'),
    engine('eng_dead', 'svc-dead', { status: 'unavailable', health: 'unreachable', state: 'offline', last_seen_at: ago(3 * 24 * 60 * MINUTE) }),
  ]));
  const page = await services({ demo: false });
  await turn();
  try {
    const toggle = findToggle(page);
    toggle.events.click();
    assert.match(page.textContent, /svc-dead/, '它没有消失，只是被折叠了');
    assert.match(page.textContent, /svc-live/);
    assert.equal(toggle.attributes['aria-pressed'], 'true');
    assert.match(toggle.textContent, /只看运行中的/);
    toggle.events.click();
    assert.doesNotMatch(page.textContent, /svc-dead/, '再点一次要能收回去');
  } finally {
    page.dispose?.();
  }
});

test('全部都没在跑时说清楚原因，而不是摆一句「还没有登记」', async () => {
  globalThis.fetch = async () => ready(opsWith([
    engine('eng_dead', 'svc-dead', { status: 'unavailable', health: 'unreachable', state: 'offline', last_seen_at: ago(3 * 24 * 60 * MINUTE) }),
  ]));
  const page = await services({ demo: false });
  await turn();
  try {
    assert.doesNotMatch(page.textContent, /还没有登记推理服务/, '登记还在，只是没在跑');
    assert.match(page.textContent, /1 个已登记的服务当前都没有在运行/);
  } finally {
    page.dispose?.();
  }
});

test('全部在跑时不摆一个点了也没东西的开关', async () => {
  globalThis.fetch = async () => ready(opsWith([engine('eng_live', 'svc-live')]));
  const page = await services({ demo: false });
  await turn();
  try {
    const toggle = findToggle(page);
    assert.equal(toggle.hidden, true);
    assert.match(page.textContent, /^[\s\S]*1 个在运行[\s\S]*$/);
    assert.doesNotMatch(page.textContent, /未在运行/);
  } finally {
    page.dispose?.();
  }
});
