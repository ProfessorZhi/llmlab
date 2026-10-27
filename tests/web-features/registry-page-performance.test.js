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
  set textContent(v) { this.childNodes = []; this.text = v; }
  get textContent() { return [this.text ?? '', ...this.childNodes.map(n => n.textContent)].join(''); }
  setAttribute(k, v) { this.attributes[k] = v; }
  addEventListener(k, fn) { this.events[k] = fn; }
  get isConnected() { return false; }
  querySelectorAll() { return []; }
}
globalThis.Node = Element;
globalThis.document = { createElement: t => new Element(t), createElementNS: (_, t) => new Element(t), createTextNode: t => { const e = new Element('#text'); e.text = t; return e; }, visibilityState: 'visible' };
const { models, engines, services } = await import('../../web/console/js/pages.js');
const { detail } = await import('../../web/console/js/service.js');
const ops = JSON.parse(readFileSync(new URL('./fixtures/bff-ops-payloads.json', import.meta.url))).admin_private;
const ready = data => new Response(JSON.stringify({ data }));
const turn = () => new Promise(r => setImmediate(r));
const nodes = (root, tag) => [root, ...root.childNodes.flatMap(n => nodes(n, tag))].filter(n => n.tagName === tag);

for (const [label, make] of [['models', models], ['engines', engines], ['services', services]]) {
  test(`${label}: mounts without waiting for registry, and cancels when disposed`, async () => {
    let aborted = false;
    globalThis.fetch = (_, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('cancelled', 'AbortError')); }, { once: true }));
    const controller = new AbortController();
    let page;
    const pending = make({ demo: false, signal: controller.signal }).then(p => { page = p; });
    await turn();
    if (!page) controller.abort();
    assert.ok(page, 'list shell must be available while its data is pending');
    page.dispose(); await pending; await turn();
    assert.equal(aborted, true);
  });
}
test('model list bounds DOM size with thousands of records', async () => {
  const data = structuredClone(ops); data.synthetic = false; data.trends = null;
  const base = data.registry.models[0];
  data.registry.models = Array.from({ length: 3000 }, (_, i) => ({ ...base, model_id: 'mdl_' + i, name: 'Model ' + i }));
  globalThis.fetch = async () => ready({ ops: data });
  const page = await models({ demo: false }); await turn();
  try { const rows = nodes(page, 'tr'); assert.ok(rows.length > 1); assert.ok(rows.length <= 51, 'only the current page should be rendered'); }
  finally { page.dispose?.(); }
});
test('service detail mounts immediately and loads metrics/API only on demand', async () => {
  const requests = [];
  globalThis.fetch = async path => { requests.push(path); return path === '/api/v2/ops' ? ready({ ops }) : ready({ framework: 'strata', engine_name: 'Test', supported: false, routes: [] }); };
  const page = await detail({ demo: false }, 'eng_test'); await turn();
  try { assert.equal(requests.some(p => p.includes('/live') || p === '/api/gateway/models'), false); assert.match(page.textContent, /运行参数/); }
  finally { page.dispose?.(); }
});


test('pagination focus does not stop scheduled registry refresh', async () => {
  const { registryList } = await import('../../web/console/js/registry-list.js');
  const originalTimer = globalThis.setTimeout;
  let tick, reads = 0; const cleanups = [];
  globalThis.setTimeout = (fn, ms) => ms === 15000 ? (tick = fn, 0) : originalTimer(fn, ms);
  const ctx = { demo: false, opsCache: { peek: () => null, read: async () => ({ state: 'ready', value: ++reads }) }, onDispose: fn => cleanups.push(fn) };
  const list = registryList(ctx, { project: () => Array.from({ length: 70 }, (_, i) => i), render: rows => new Element('table'), empty: () => new Element('p') });
  Object.defineProperty(list.root, 'isConnected', { value: true });
  try {
    await turn();
    const next = nodes(list.root, 'button').find(n => n.textContent === '下一页');
    document.activeElement = next; next.events.click();
    await tick(); assert.equal(reads, 2, 'next-page button focus must not pause polling');
  } finally { for (const cleanup of cleanups) cleanup(); globalThis.setTimeout = originalTimer; document.activeElement = null; }
});

test('overview reattributes GPU memory when ops arrives after host', async () => {
  const { overview } = await import('../../web/console/js/pages.js');
  const { SNAP_HOST_SUMMARY } = await import('../../web/console/js/snapshot.js');
  let finishOps;
  const host = structuredClone(SNAP_HOST_SUMMARY); host.observed_at = new Date().toISOString();
  for (const metric of Object.values(host.metrics)) metric.sample_at = host.observed_at;
  globalThis.fetch = async path => path === '/api/infra/summary' ? ready(host) : new Response('{}', { status: 503 });
  const page = await overview({ demo: false, opsCache: { read: () => new Promise(resolve => { finishOps = resolve; }) } });
  try {
    await turn();
    assert.match(page.children[2].textContent, /进程未归因/);
    finishOps({ state: 'ready', value: { synthetic: false, registry: { adapter_attached: true, owned: [{ deployment_id: 'late', model_label: 'Late service', resources: { vram_used_mib: 1024 } }] } } });
    await turn();
    assert.match(page.children[2].textContent, /Late service/, 'late registry data must update GPU attribution');
  } finally { page.dispose(); }
});
