/*! web/console/js/live-refresh.js —— 页面上「每隔一会儿重取一次」的那点骨架。
 *
 *  一条定时的 ``setInterval`` 写起来只有一行，但监控页面真正会踩的坑在别处，
 *  所以这里把它们写成可测的四条：
 *
 *    1. **不叠加。** 上一拍还没回来就跳过这一拍，不再排一个并行的请求。
 *       取数慢的时候（引擎探活要等超时）排队比并发更诚实：并发只会让每一拍
 *       都更慢，然后看起来像「刷新失效」。
 *    2. **看不见的标签页不取。** 人不在这个标签页时没有必要每秒扫一遍 ``/proc``。
 *       隐藏期间的拍被跳过，切回来时最多等一个周期。
 *    3. **页面被换掉就停。** 路由重绘会把整棵 DOM 换掉；定时器不会自己知道，
 *       不主动停就会一直取数，而且往一个已经不在文档里的节点上画。
 *    4. **抛错不终止。** 一次失败是一次数不到，不是「以后都不刷新了」。
 *       失败怎么显示由 ``run`` 自己决定（保留上一次读数 + 说明时间）。
 *
 *  定时器与可见性都从外面注入，所以这一支能在没有浏览器的 node 里测。
 */

/** 刷新节奏的可选值。``0`` = 不自动刷新，只留进入页面时的那一次。 */
export const REFRESH_CHOICES = Object.freeze([
  ['每秒', 1000], ['2 秒', 2000], ['5 秒', 5000], ['10 秒', 10000], ['暂停', 0],
]);

const REFRESH_KEY = 'llmlab.usage.refresh';
/** 取数失败时，状态行用短词说明是「哪一种失败」，不把状态码当成原因。 */
export const STATE_SHORT = Object.freeze({
  signed_out: '会话失效', denied: '没有权限', not_configured: '未接入', error: '读取失败',
});

const clockOf = (d = new Date()) => [d.getHours(), d.getMinutes(), d.getSeconds()]
  .map((n) => String(n).padStart(2, '0')).join(':');

/** 上一次选中的节奏；没有、读不了、或者不在选项里，都回到「每秒」。 */
export function savedRefreshMs() {
  try {
    const ms = Number(localStorage.getItem(REFRESH_KEY));
    return REFRESH_CHOICES.some(([, value]) => value === ms) ? ms : 1000;
  } catch { return 1000; }
}

export function rememberRefreshMs(ms) {
  try { localStorage.setItem(REFRESH_KEY, String(ms)); } catch { /* 无存储时只在本次页面有效 */ }
}

export const refreshLabel = (ms) => REFRESH_CHOICES.find(([, value]) => value === ms)?.[0] ?? '—';

/**
 * 状态行。它要说清三件事：多久取一次、上一次成功是什么时候、以及**这次没取到时
 * 屏幕上那些数字是什么时候的** —— 一个停住的读数如果只写着「每秒刷新」，
 * 读起来就像「刚刚量的」。
 *
 * 时间是**按块各算各的**：GPU 读数一直取不到，不该让「最后更新」跟着一起停住，
 * 也不该反过来，用另外两块的更新时间为一块停住的读数背书。所以失败项自带
 * ``at``（那一块上一次拿到的时间）；没有就是「这一块还是空的」。
 */
export function usageStatus({ everyMs, at, failed = [] }) {
  const label = everyMs === 0 ? '已暂停' : `自动刷新：${refreshLabel(everyMs)}`;
  const head = at ? `${label} · 最后更新 ${clockOf(at)}` : label;
  if (!failed.length) return at ? head : `${label} · 正在取数…`;
  const parts = failed.map(({ label: name, state, at: seen }) => {
    const how = STATE_SHORT[state] ?? state;
    return seen ? `${name}（${how}，仍是 ${clockOf(seen)} 的读数）` : `${name}（${how}，这一块还是空的）`;
  });
  return `${head} · 这次没取到 ${parts.join('、')}`;
}

/**
 * @param {object} options
 * @param {number} options.intervalMs 间隔；``0`` 表示不自动刷新
 * @param {() => unknown | Promise<unknown>} options.run 每一拍做的事
 * @param {() => boolean} [options.isHidden] 这一拍该不该跳过（标签页不可见）
 * @param {() => boolean} [options.isAttached] 页面还在文档里吗；``false`` 就停
 */
export function createRefresher({
  intervalMs,
  run,
  setTimeoutImpl = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutImpl = (id) => clearTimeout(id),
  isHidden = () => document.visibilityState === 'hidden',
  isAttached = () => true,
}) {
  let everyMs = Math.max(0, Number(intervalMs) || 0);
  let timer = null;
  let inFlight = false;
  let stopped = true;
  //: 页面是「先建好节点、再由路由挂上去」的：第一拍可能在挂上之前就到。
  //: 未挂上的那一拍只算「来早了」，不算「页面已经换掉」，所以不能就此停掉。
  let seenAttached = false;

  const schedule = () => {
    if (stopped || everyMs <= 0) return;
    timer = setTimeoutImpl(tick, everyMs);
  };

  const tick = async () => {
    timer = null;
    if (stopped) return;
    if (isAttached()) seenAttached = true;
    else if (seenAttached) { stopped = true; return; }
    else { schedule(); return; }
    if (isHidden() || inFlight) { schedule(); return; }
    inFlight = true;
    try {
      await run();
    } catch {
      // 取不到是这一拍的结果，不是循环的终点；状态由 run 自己讲。
    } finally {
      inFlight = false;
    }
    schedule();
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      schedule();
    },
    stop() {
      stopped = true;
      if (timer !== null) clearTimeoutImpl(timer);
      timer = null;
    },
    /** 换节奏。``0`` = 停下来（但页面还在，只是不再自动取数）。 */
    every(ms) {
      everyMs = Math.max(0, Number(ms) || 0);
      if (timer !== null) clearTimeoutImpl(timer);
      timer = null;
      if (stopped) return;
      schedule();
    },
    /** 不等下一拍，现在就取一次。 */
    async now() {
      if (stopped) return;
      if (timer !== null) { clearTimeoutImpl(timer); timer = null; }
      await tick();
    },
    get running() { return !stopped && everyMs > 0; },
    get intervalMs() { return everyMs; },
  };
}
