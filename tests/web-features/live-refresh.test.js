/*! tests/web-features/live-refresh.test.js — 定时重取的四条规矩，以及状态行说了什么。
 *
 *  这一支值得单独钉住，是因为它的每一条规矩对应的都是一个「看起来在刷新，
 *  其实在骗人」的失败：
 *
 *    * 上一拍没回来却照排下一拍 ⇒ 越刷新越慢，最后像坏了；
 *    * 隐藏的标签页照样每秒扫 ``/proc`` ⇒ 没人在看，机器在忙；
 *    * 页面换掉了定时器还在跑 ⇒ 一路取数到关标签页；
 *    * 一次失败就停 ⇒ 网闪一下，这页就再也不更新了；
 *    * 停住的读数只写「每秒刷新」⇒ 看不出这些数字是十分钟前的。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  createRefresher, REFRESH_CHOICES, refreshLabel, rememberRefreshMs, savedRefreshMs, usageStatus,
} from '../../web/console/js/live-refresh.js';

const here = dirname(fileURLToPath(import.meta.url));

/** 手摇的定时器：谁排了什么、什么时候跑，全部由测试决定。 */
function fakeTimers() {
  let seq = 0;
  const pending = new Map();
  return {
    set: (fn, ms) => { const id = ++seq; pending.set(id, { fn, ms }); return id; },
    clear: (id) => pending.delete(id),
    get size() { return pending.size; },
    intervals: () => [...pending.values()].map((t) => t.ms),
    async fire() {
      const [id, timer] = [...pending.entries()][0] ?? [];
      if (!timer) throw new Error('没有排着的定时器');
      pending.delete(id);
      await timer.fn();
      return timer.ms;
    },
  };
}

function refresher({ everyMs = 1000, run = () => {}, hidden = () => false, attached = () => true } = {}) {
  const timers = fakeTimers();
  const r = createRefresher({
    intervalMs: everyMs, run,
    setTimeoutImpl: timers.set, clearTimeoutImpl: timers.clear,
    isHidden: hidden, isAttached: attached,
  });
  return { r, timers };
}

test('按节奏一拍一拍地取，不提前、不叠加', async () => {
  let runs = 0;
  const { r, timers } = refresher({ run: () => { runs += 1; } });

  r.start();
  assert.deepEqual(timers.intervals(), [1000], 'start 只排一拍');
  assert.equal(runs, 0, 'start 自己不取数');

  await timers.fire();
  assert.equal(runs, 1);
  assert.deepEqual(timers.intervals(), [1000], '跑完再排下一拍');
});

test('上一拍还没回来时跳过这一拍，而不是并行再来一次', async () => {
  let runs = 0;
  let release = null;
  const { r } = refresher({
    run: () => { runs += 1; return new Promise((resolve) => { release = resolve; }); },
  });

  r.start();
  const first = r.now();            // now() 立即取，但 run 卡住不返回
  await Promise.resolve();
  assert.equal(runs, 1);

  await r.now();
  assert.equal(runs, 1, '在途时 now() 不另起一次');
  release();
  await first;
});

test('看不见的标签页不取，但节奏还在', async () => {
  let hidden = true;
  let runs = 0;
  const { r, timers } = refresher({ run: () => { runs += 1; }, hidden: () => hidden });

  r.start();
  await timers.fire();
  assert.equal(runs, 0, '隐藏时取数是白扫 /proc');
  assert.deepEqual(timers.intervals(), [1000], '回来时最多等一个周期');

  hidden = false;
  await timers.fire();
  assert.equal(runs, 1);
});

test('页面被换掉（节点离开文档）就停，不再取数', async () => {
  let attached = true;
  let runs = 0;
  const { r, timers } = refresher({ run: () => { runs += 1; }, attached: () => attached });

  r.start();
  await timers.fire();
  assert.equal(runs, 1);

  attached = false;
  await timers.fire();
  assert.equal(runs, 1);
  assert.equal(timers.size, 0, '离开后不留定时器');
});

test('还没挂上文档的第一拍只是来早了，不能当成「页面已换掉」', async () => {
  let runs = 0;
  const { r, timers } = refresher({ run: () => { runs += 1; }, attached: () => false });

  r.start();
  await timers.fire();
  assert.equal(runs, 0);
  assert.equal(timers.size, 1, '继续排，等它挂上去');
});

test('一次抛错不终止循环', async () => {
  let runs = 0;
  const { r, timers } = refresher({ run: () => { runs += 1; throw new Error('boom'); } });

  r.start();
  await timers.fire();
  assert.equal(runs, 1);
  assert.equal(timers.size, 1, '下一次还是排着的');

  await timers.fire();
  assert.equal(runs, 2);
});

test('stop() 之后既不再取数，也不再排拍', async () => {
  let runs = 0;
  const { r, timers } = refresher({ run: () => { runs += 1; } });

  r.start();
  r.stop();
  assert.equal(timers.size, 0);
  assert.equal(r.running, false);
  await r.now();
  assert.equal(runs, 0);
});

test('换节奏会立刻按新节奏重排；暂停就是 0', async () => {
  const { r, timers } = refresher();

  r.start();
  r.every(2000);
  assert.deepEqual(timers.intervals(), [2000], '换节奏后不留旧的那一拍');

  r.every(0);
  assert.equal(timers.size, 0);
  assert.equal(r.running, false, '暂停不算在跑');
});

test('状态行：多久取一次 + 上一次成功的时间', () => {
  const at = new Date(2026, 9, 26, 18, 20, 31);
  assert.equal(usageStatus({ everyMs: 1000, at }), '自动刷新：每秒 · 最后更新 18:20:31');
  assert.equal(usageStatus({ everyMs: 0, at }), '已暂停 · 最后更新 18:20:31');
  assert.equal(usageStatus({ everyMs: 1000, at: null }), '自动刷新：每秒 · 正在取数…');
});

test('状态行：这次没取到时，说清停住的是哪一块、它停在什么时候', () => {
  const at = new Date(2026, 9, 26, 18, 20, 31);
  const text = usageStatus({ everyMs: 1000, at, failed: [{ label: '主机读数', state: 'error' }] });

  assert.equal(text,
    '自动刷新：每秒 · 最后更新 18:20:31 · 这次没取到 主机读数（读取失败，这一块还是空的）');
});

test('状态行：一块停住时，写的是那一块自己的时间，不借别的时间', () => {
  // 其余两块每分钟都在更新，但服务清单停在 15 分钟前 —— 拿「最后更新」给它背书就是撒谎。
  const page = new Date(2026, 9, 26, 18, 20, 31);
  const older = new Date(2026, 9, 26, 18, 5, 0);
  const text = usageStatus({
    everyMs: 2000, at: page, failed: [{ label: '服务清单', state: 'error', at: older }],
  });

  assert.equal(text,
    '自动刷新：2 秒 · 最后更新 18:20:31 · 这次没取到 服务清单（读取失败，仍是 18:05:00 的读数）');
});

test('状态行：一份都没成功过时不给「最后更新」', () => {
  const text = usageStatus({ everyMs: 2000, at: null, failed: [{ label: '服务清单', state: 'not_configured' }] });

  assert.equal(text, '自动刷新：2 秒 · 这次没取到 服务清单（未接入，这一块还是空的）');
});

test('认不出的失败状态照原样写出来，不编一个中文理由', () => {
  assert.match(usageStatus({ everyMs: 5000, at: null, failed: [{ label: 'GPU 读数', state: 'teapot' }] }),
    /GPU 读数（teapot，/);
});

test('刷新频率的选项与默认值：默认每秒，且只有列出来的那几个', () => {
  assert.equal(refreshLabel(1000), '每秒');
  assert.deepEqual(REFRESH_CHOICES.map(([label]) => label), ['每秒', '2 秒', '5 秒', '10 秒', '暂停']);
  assert.equal(savedRefreshMs(), 1000, '没有存储时回到每秒');
});

test('存过的节奏能用；存进去的值不在选项里就回到每秒', () => {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, value),
  };
  try {
    rememberRefreshMs(5000);
    assert.equal(savedRefreshMs(), 5000);

    rememberRefreshMs(250);
    assert.equal(savedRefreshMs(), 1000, '250 毫秒不是选项，别让它悄悄生效');
  } finally {
    delete globalThis.localStorage;
  }
});

test('新用户（浏览器里从没存过）拿到的是每秒，不是「暂停」', () => {
  // 真实的 localStorage 在没有这个键时返回 null；Number(null) 是 0，而 0 就是「暂停」。
  // 这一条防的就是「默认节奏变成不刷新、页面看起来正常但数字不动」。
  const seen = [];
  globalThis.localStorage = {
    getItem: (key) => { seen.push(key); return null; },
    setItem: () => {},
  };
  try {
    assert.equal(savedRefreshMs(), 1000);
    assert.equal(savedRefreshMs(), 1000, '每次都重新读，不会记住上一次的结果');
  } finally {
    delete globalThis.localStorage;
  }
  assert.ok(seen.length >= 2);
});

test('浏览器里空字符串也不当成「暂停」', () => {
  globalThis.localStorage = { getItem: () => '', setItem: () => {} };
  try {
    assert.equal(savedRefreshMs(), 1000);
  } finally {
    delete globalThis.localStorage;
  }
});

test('显式选了「暂停」才是不刷新', () => {
  globalThis.localStorage = { getItem: () => '0', setItem: () => {} };
  try {
    assert.equal(savedRefreshMs(), 0, '存过 0 就是用户真的按了暂停');
  } finally {
    delete globalThis.localStorage;
  }
});

test('资源监控页真的接上了这支定时器，而不是自己另写一个 setInterval', () => {
  const source = readFileSync(join(here, '../../web/console/js/pages.js'), 'utf8');

  assert.match(source, /from '\.\/live-refresh\.js'/, '刷新节奏只有一处定义');
  assert.match(source, /createRefresher\(\{/, '页面用这支骨架');
  assert.match(source, /isAttached:\s*\(\)\s*=>\s*page\.isConnected/,
    '页面被换掉时要停：isAttached 必须绑在页面的连接状态上');
  assert.doesNotMatch(source, /\bsetInterval\(/, '不要再散一个自己的定时器');

  // 演示数据是常量：那一支必须在建定时器之前返回，不能给它挂一个每秒重画的循环。
  // 现在的页面用的是一组 refreshers（复数），启动点写作 refreshers[key].start()，
  // 所以这里锚在「usage 函数、demo 分支的 return、创建/启动刷新器」三处，而不是旧
  // 的单个 refresher.start()，也不钉行号。
  const usageAt = source.indexOf('export async function usage');
  assert.ok(usageAt >= 0, '找不到 usage 页面函数，接线测试不能静默通过');

  const demoAt = source.indexOf('if (ctx.demo) {', usageAt);
  assert.ok(demoAt > usageAt, 'usage 里找不到 demo 分支');

  const demoReturnAt = source.indexOf('return', demoAt);
  const refresherAt = source.indexOf('createRefresher({', usageAt);
  const startAt = source.indexOf('.start()', usageAt);
  assert.ok(demoReturnAt > demoAt, 'demo 分支没有返回');
  assert.ok(refresherAt > usageAt, 'usage 里找不到 createRefresher(');
  assert.ok(startAt > usageAt, 'usage 里找不到刷新器的启动点');
  assert.ok(demoReturnAt < refresherAt, 'demo 分支必须在创建刷新器之前返回');
  assert.ok(demoReturnAt < startAt, 'demo 分支必须在启动刷新之前返回');
});
