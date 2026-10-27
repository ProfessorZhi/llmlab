/*! tests/web-features/service-control.test.js — 控制台「服务控制」：下发、轮询、说真话。
 *
 *  这一块的价值全在一条规则上：**「已下发」不是「已启动」**。平台只能把任务排进
 *  本人代理的队列（POST 返回 202 + queued），进程到底起没起来只有代理回报的终态
 *  才算数。所以这里的用例不是去读 service.js 的源码常量，而是真的把详情页渲染出来、
 *  点按钮、喂响应，再对**渲染结果**做断言：
 *
 *    * 点「重启」「启动」各下发一条 POST，带 CSRF 令牌和正确的 kind；
 *    * 202 + queued 只说「已下发给你的代理，等待领取」，页面上不许出现「已启动」；
 *    * running/done/failed 三拍的文案，以及终态之后不再轮询；
 *    * 403 且没有服务端说明时，落到「没有可用的本人代理」这句兜底；
 *    * 演示模式一条网络请求都不发；
 *    * 一分钟没结果时，文案必须指向 llmlab agent --watch（页脚本来就有这句话，
 *      所以要认准**超时那句**，否则会被页脚蒙混过关）。
 *
 *  controlView 是 service.js 的内部函数，不导出；这里按真实路径走 detail(ctx, engineId)，
 *  让它自己把「服务控制」画出来。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

/* ---------------- 假 DOM（只实现 dom.js / ui.js 用到的那部分） ---------------- */
class FakeNode {}
class FakeText extends FakeNode {
  constructor(text) { super(); this.text = text; this.parentNode = null; }
  get textContent() { return this.text; }
}
class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.tagName = tag;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.events = {};
    this.text = '';
    this.style = { setProperty() {} };
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.childNodes.push(node); } }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) { this.childNodes.splice(index, 1); node.parentNode = null; }
    return node;
  }
  remove() { this.parentNode?.removeChild(this); }
  get firstChild() { return this.childNodes[0] ?? null; }
  set textContent(value) { this.childNodes = []; this.text = String(value); }
  get textContent() { return `${this.text}${this.childNodes.map(node => node.textContent).join('')}`; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, fn) { this.events[type] = fn; }
  querySelectorAll() { return []; }
  contains(node) { return node === this || this.childNodes.some(child => child.contains?.(node)); }
  get isConnected() { return false; }
}

const documentRoot = new FakeElement('body');
globalThis.Node = FakeNode;
globalThis.document = {
  documentElement: documentRoot,
  body: documentRoot,
  visibilityState: 'visible',
  createElement: (tag) => new FakeElement(tag),
  createElementNS: (_ns, tag) => new FakeElement(tag),
  createTextNode: (text) => new FakeText(text),
};

const { detail } = await import('../../web/console/js/service.js');
const { rememberCsrf } = await import('../../web/console/js/data.js');

/* ---------------- 替身 ---------------- */
const ENGINE_ID = 'eng_test';
const DEMO_ENGINE_ID = 'eng_9c3071673a52286a'; // snapshot.js 里的引擎，演示页按这个 id 找服务
const SERVE_PATH = `/api/v2/registry/engines/${ENGINE_ID}/serve`;
const PARAMS_PATH = `/api/v2/registry/engines/${ENGINE_ID}/params`;
const taskPath = (id) => `/api/v2/registry/agent-tasks/${id}`;

const realFetch = globalThis.fetch;
const realTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };

const json = (status, body) => new Response(JSON.stringify(body), {
  status,
  headers: { 'content-type': 'application/json' },
});

const csrfOk = () => json(200, { data: { csrf_token: 'csrf-test-token' } });
// supported:false —— 本文件只关心「服务控制」，不需要参数表也画出来。
const paramsOk = () => json(200, { data: { engine_id: ENGINE_ID, engine_name: '测试服务', framework: 'test', supported: false } });
const queued202 = (id = 'task-1') => json(202, { data: { id, engine_id: ENGINE_ID, state: 'queued', result: null } });

/** 记录每一条请求；route(path, nth, record) 按该路径第几次调用返回 Response。 */
function stubFetch(route) {
  const calls = [];
  globalThis.fetch = async (path, options = {}) => {
    const record = { path, method: options.method ?? 'GET', headers: options.headers ?? {}, body: options.body ?? null };
    calls.push(record);
    const nth = calls.filter((call) => call.path === path).length;
    return route(path, nth, record);
  };
  return calls;
}

/** 默认路由：CSRF、参数表正常，其余（含 /api/v2/ops）按「未接入」处理。 */
function routes(extra = {}) {
  return (path, nth, record) => {
    if (extra[path]) return extra[path](nth, record);
    if (path === '/auth/csrf') return csrfOk();
    if (path === PARAMS_PATH) return paramsOk();
    return json(503, { error: { code: 'not_configured' } });
  };
}

/** 轮询的 sleep 是 setTimeout(2000)。'freeze' 只记录不触发（观察中间态），
 *  'micro' 立即续拍（跑到终态）。8000ms 的请求超时仍用真定时器，且请求一完成就被
 *  getJSON 自己 clearTimeout，所以不会被这里误 abort。 */
function installPollTimers(mode) {
  globalThis.setTimeout = (fn, ms) => (ms === 2000
    ? (mode === 'micro' ? queueMicrotask(fn) : undefined, 0)
    : realTimers.setTimeout(fn, ms));
  globalThis.clearTimeout = realTimers.clearTimeout;
}

function restore() {
  globalThis.fetch = realFetch;
  globalThis.setTimeout = realTimers.setTimeout;
  globalThis.clearTimeout = realTimers.clearTimeout;
  rememberCsrf(null);
}

/** 可控的轮询定时器：把 2000ms 的那一拍攒起来，由测试决定何时唤醒。
 *  8000ms 的请求超时仍走真定时器（请求一完成就被 getJSON 自己清掉），
 *  这样既能复现「A 还在等、B 已经下发」的连点，又不会把 A 的挂起请求误 abort。 */
function installQueuedPollTimers() {
  const beats = [];
  globalThis.setTimeout = (fn, ms) => {
    if (ms === 2000) { beats.push(fn); return 0; }
    return realTimers.setTimeout(fn, ms);
  };
  globalThis.clearTimeout = realTimers.clearTimeout;
  return beats;
}

async function fireBeat(beats) {
  const beat = beats.shift();
  assert.ok(beat, '没有待唤醒的轮询拍');
  beat();
  await nextTurn();
}

const nextTurn = () => new Promise(resolve => setImmediate(resolve));
async function until(check, message) {
  for (let i = 0; i < 2000; i += 1) {
    if (check()) return;
    await nextTurn();
  }
  assert.fail(message);
}

function allNodes(node, out = []) {
  out.push(node);
  for (const child of node.childNodes ?? []) allNodes(child, out);
  return out;
}
const findButton = (root, label) => allNodes(root)
  .find(node => node.tagName === 'button' && node.textContent === label) ?? null;

/** 走真实入口渲染详情页，等「服务控制」的三个按钮出现。 */
async function mountControl(ctx = { demo: false }, engineId = ENGINE_ID) {
  const page = await detail(ctx, engineId);
  await until(() => findButton(page, '启动'), '详情页没有画出「服务控制」的按钮');
  return page;
}

/* ---------------- 1. 按钮真的下发 ---------------- */
test('「重启」「启动」各下发一条 POST，带 CSRF 令牌和对应的 kind', async () => {
  rememberCsrf(null);
  installPollTimers('freeze');
  const calls = stubFetch(routes({ [SERVE_PATH]: () => queued202() }));
  let page;
  try {
    page = await mountControl();
    for (const label of ['启动', '停止', '重启']) {
      assert.ok(findButton(page, label), `服务控制缺少「${label}」按钮`);
    }

    await findButton(page, '重启').events.click();
    const afterRestart = calls.filter(call => call.method === 'POST');
    // 一次点击只代表一条任务：多发一条就等于让代理重复重启。
    assert.equal(afterRestart.length, 1, `点一次「重启」应恰好一条 POST，实际 ${afterRestart.length} 条`);
    assert.equal(afterRestart[0].path, SERVE_PATH);
    assert.equal(afterRestart[0].headers['X-CSRF-Token'], 'csrf-test-token', '写请求必须带 CSRF 令牌');
    assert.deepEqual(JSON.parse(afterRestart[0].body), { kind: 'restart' });

    await findButton(page, '启动').events.click();
    const afterStart = calls.filter(call => call.method === 'POST');
    assert.equal(afterStart.length, 2);
    assert.deepEqual(JSON.parse(afterStart[1].body), { kind: 'start' }, '点「启动」必须发 start，不能沿用上一个 kind');
  } finally { page?.dispose(); restore(); }
});

/* ---------------- 2. 已下发 ≠ 已启动 ---------------- */
test('202 + queued 只显示「已下发」，页面上不能出现「已启动」', async () => {
  rememberCsrf(null);
  installPollTimers('freeze');
  const calls = stubFetch(routes({ [SERVE_PATH]: () => queued202() }));
  let page;
  try {
    page = await mountControl();
    await findButton(page, '重启').events.click();

    const text = page.textContent;
    assert.match(text, /已下发给你的代理，等待领取/);
    // 这条是整个链路的底线：202 只说明任务进了队列，进程起没起来还未知。
    assert.doesNotMatch(text, /已启动/, '202/queued 阶段不能替代理宣称服务已启动');
    // 冻结了 2 秒的轮询拍，所以此时不该已经去查过任务 —— 文案只能来自 POST 响应本身。
    assert.equal(calls.filter(call => call.path === taskPath('task-1')).length, 0);
  } finally { page?.dispose(); restore(); }
});

/* ---------------- 3. 轮询到终态 ---------------- */
test('轮询：running 说「代理正在执行」，done 说「代理已执行」后不再轮询', async () => {
  rememberCsrf(null);
  installPollTimers('micro');
  const TASK = taskPath('task-1');
  let releaseDone;
  const doneGate = new Promise(resolve => { releaseDone = resolve; });
  const calls = stubFetch(routes({
    [SERVE_PATH]: () => queued202('task-1'),
    // 第一拍 running；第二拍卡住，等测试确认完中间态再放行 done。
    [TASK]: async (nth) => {
      if (nth === 1) return json(200, { data: { id: 'task-1', state: 'running', result: null } });
      await doneGate;
      return json(200, { data: { id: 'task-1', state: 'done', result: null } });
    },
  }));
  let page;
  try {
    page = await mountControl();
    await findButton(page, '重启').events.click();

    await until(() => /代理正在执行/.test(page.textContent), 'running 阶段没有说「代理正在执行」');
    releaseDone();
    await until(() => /代理已执行/.test(page.textContent), 'done 阶段没有说「代理已执行」');

    const polls = calls.filter(call => call.path === TASK).length;
    assert.equal(polls, 2, `终态前只该查两拍，实际 ${polls} 拍`);
    for (let i = 0; i < 5; i += 1) await nextTurn();
    // done 之后继续打任务接口，只会让代理和页面一起空转。
    assert.equal(calls.filter(call => call.path === TASK).length, polls, '终态之后不得再轮询任务状态');
  } finally { page?.dispose(); restore(); }
});

test('failed：说「代理执行失败」，并把代理回报的 detail 原样显示出来', async () => {
  rememberCsrf(null);
  installPollTimers('micro');
  const TASK = taskPath('task-1');
  const calls = stubFetch(routes({
    [SERVE_PATH]: () => queued202('task-1'),
    [TASK]: () => json(200, { data: { id: 'task-1', state: 'failed', result: { detail: 'systemctl exited 1' } } }),
  }));
  let page;
  try {
    page = await mountControl();
    await findButton(page, '重启').events.click();

    await until(() => /代理执行失败/.test(page.textContent), 'failed 阶段没有说失败');
    // resultText 从 detail/message/reason 里挑一句；挑不到就空着，不硬编。
    assert.match(page.textContent, /systemctl exited 1/, '代理回报的失败原因必须让人看得到');
    assert.equal(calls.filter(call => call.path === TASK).length, 1, 'failed 是终态，查一拍就够');
  } finally { page?.dispose(); restore(); }
});

/* ---------------- 3b. 连点两次：旧任务的回报不能盖掉新任务 ---------------- */
test('连点两次：A 的旧轮询回报不覆盖 B，也不多发请求、不多触发一次结算刷新', async () => {
  rememberCsrf(null);
  const beats = installQueuedPollTimers();
  const TASK_A = taskPath('task_a');
  const TASK_B = taskPath('task_b');
  let releaseA;
  const gateA = new Promise(resolve => { releaseA = resolve; });
  let served = 0;
  const calls = stubFetch(routes({
    [SERVE_PATH]: () => queued202(served++ === 0 ? 'task_a' : 'task_b'),
    // A 的第一拍请求挂住，模拟代理迟迟不回报。B 已经走完终态后才放行它，
    // 这样旧代码会把 A 的失败当成页面现状、并再触发一次结算刷新。
    [TASK_A]: async () => {
      await gateA;
      return json(200, { data: { id: 'task_a', state: 'failed', result: { detail: 'A-STALE' } } });
    },
    [TASK_B]: (nth) => json(200, {
      data: { id: 'task_b', state: nth === 1 ? 'running' : 'done', result: null },
    }),
  }));
  let page;
  try {
    page = await mountControl();
    const opsBefore = calls.filter(call => call.path === '/api/v2/ops').length;
    assert.equal(opsBefore, 1, '挂载时读一次运营数据');

    // A：下发成功，轮询第一拍发出去后一直挂着。
    await findButton(page, '重启').events.click();
    await fireBeat(beats);
    assert.equal(calls.filter(call => call.path === TASK_A).length, 1, 'A 的第一拍应当已经发出');

    // B：A 的那条请求还挂着时立刻再点一次，拿到新的 task_b。
    await findButton(page, '停止').events.click();
    await fireBeat(beats);
    await until(() => /代理正在执行/.test(page.textContent), 'B 的 running 状态没有显示');
    await fireBeat(beats);
    await until(() => /代理已执行/.test(page.textContent), 'B 的终态没有显示');

    // 现在才放行 A 的旧回报（failed + A-STALE）。
    releaseA();
    for (let i = 0; i < 10; i += 1) await nextTurn();

    const text = page.textContent;
    assert.match(text, /代理已执行/, '页面最终显示的必须是 B 的终态');
    assert.doesNotMatch(text, /A-STALE/, 'A 的旧回报不能覆盖 B 的状态');
    assert.doesNotMatch(text, /代理执行失败/, 'A 的旧 failed 不能变成页面当前的状态');
    // generation 过期后 A 的循环必须直接退出：既不再发第二拍，也不再改页面。
    assert.equal(calls.filter(call => call.path === TASK_A).length, 1, 'A 醒来后不得再发出针对 A 的 GET');
    assert.equal(calls.filter(call => call.path === TASK_B).length, 2, 'B 正常走 running → done 两拍');
    assert.equal(beats.length, 0, '终态后不该还留着待唤醒的轮询拍');
    // onSettled（readService）只能由 B 的终态触发一次；A 的旧回报若也触发，就会多读一次。
    assert.equal(
      calls.filter(call => call.path === '/api/v2/ops').length,
      opsBefore + 1,
      '任务结算后的重读只能由 B 的终态触发一次',
    );
  } finally { releaseA(); page?.dispose(); restore(); }
});

/* ---------------- 4. 没有可用代理 ---------------- */
test('403 且服务端没给话：落到 deny 分支的「没有可用的本人代理」', async () => {
  rememberCsrf(null);
  installPollTimers('freeze');
  stubFetch(routes({
    // message 为 null：服务端只给了错误码，没有给可转述的说明。
    [SERVE_PATH]: () => json(403, { error: { code: 'registry.access_denied', message: null } }),
  }));
  let page;
  try {
    page = await mountControl();
    await findButton(page, '重启').events.click();
    await until(() => /没有可用的本人代理/.test(page.textContent), '403 时没有说清「没有可用代理」');
  } finally { page?.dispose(); restore(); }
});

test('403 带服务端 message 时优先转述服务端的话（本地兜底句不再出现）', async () => {
  rememberCsrf(null);
  installPollTimers('freeze');
  stubFetch(routes({
    [SERVE_PATH]: () => json(403, {
      error: { code: 'registry.access_denied', message: '这个服务没有登记运行用户，暂不能下发。' },
    }),
  }));
  let page;
  try {
    page = await mountControl();
    await findButton(page, '重启').events.click();
    await until(() => /这个服务没有登记运行用户，暂不能下发。/.test(page.textContent), '没有显示服务端给出的拒绝原因');
    // 服务端已经解释过时不重复本地兜底句：两句话叠在一起只会让人以为出了两种问题。
    assert.doesNotMatch(page.textContent, /没有可用的本人代理/);
  } finally { page?.dispose(); restore(); }
});

/* ---------------- 5. 演示模式不发请求 ---------------- */
test('演示模式点按钮：一条网络请求都不发，并明说这是演示数据', async () => {
  rememberCsrf(null);
  installPollTimers('freeze');
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new Error('演示模式不该发请求'); };
  let page;
  try {
    page = await mountControl({ demo: true }, DEMO_ENGINE_ID);
    await findButton(page, '重启').events.click();
    await until(() => /演示数据/.test(page.textContent), '演示模式没有说明不会真的下发');
    // 演示数据是常量：真发一条任务出去，点一下就会动到某人的真实服务。
    assert.equal(requests, 0, `演示模式发出了 ${requests} 条网络请求`);
  } finally { page?.dispose(); restore(); }
});

/* ---------------- 6. 超时不撒谎 ---------------- */
test('30 拍都没有终态：文案指向 llmlab agent --watch，而不是谎称已执行', async () => {
  rememberCsrf(null);
  installPollTimers('micro');
  const TASK = taskPath('task-1');
  const calls = stubFetch(routes({
    [SERVE_PATH]: () => queued202('task-1'),
    [TASK]: () => json(200, { data: { id: 'task-1', state: 'running', result: null } }),
  }));
  let page;
  try {
    page = await mountControl();
    await findButton(page, '重启').events.click();

    await until(() => /还没有收到执行结果/.test(page.textContent), '轮询耗尽后没有给出超时说明');
    const text = page.textContent;
    // 页脚本来就写着 llmlab agent --watch，所以只认「超时那句」里的命令，
    // 否则页脚会把一条坏文案蒙混过去。
    assert.ok(
      text.includes('还没有收到执行结果。平台只负责下发，执行由你本机的代理完成——确认 llmlab agent --watch 正在运行。'),
      '超时文案必须让人知道去确认 llmlab agent --watch，而不是以为任务已经完成',
    );
    assert.doesNotMatch(text, /代理已执行/, '一直停在 running 不能说「已执行」');
    assert.equal(calls.filter(call => call.path === TASK).length, 30,
      '应当老老实实查满 30 拍再放弃，提前放弃会把慢代理误报成没在跑');
  } finally { page?.dispose(); restore(); }
});
