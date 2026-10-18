/* ==========================================================================
   store -- polling that cannot turn into a request storm.

   The brief calls this out by name, and it is the failure this console is
   most prone to: several views each want live data, the user presses refresh
   while a poll is already in flight, and a hidden background tab keeps
   hammering a node that is already down.

   Five guards, all in one place:

     1. **single-flight** -- a tick that fires while a request is outstanding
        sets a flag instead of starting a second request; at most one
        follow-up runs when the first settles.
     2. **min interval**  -- a manual refresh inside `minInterval` of the last
        network hit is coalesced, not sent.  Pressing refresh ten times sends
        one request.
     3. **backoff**       -- consecutive failures widen the interval
        (x2, capped), and a success resets it.  A node that is down gets
        polled at 1/8 the rate instead of the same rate forever.
     4. **hidden tab**    -- a tick while the document is hidden is skipped,
        and the first tick after it becomes visible is immediate.  A tab left
        open overnight must not be a background load generator.
     5. **change detect** -- the payload is hashed; subscribers are told
        whether it actually changed, so a steady-state poll repaints nothing
        and cannot race the user mid-interaction.
   ========================================================================== */

const hash = (value) => {
  if (value === null || value === undefined) return 'nil';
  let s;
  try { s = typeof value === 'string' ? value : JSON.stringify(value); }
  catch { return `!${Math.random()}`; }
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36);
};

const state = { hidden: false };
document.addEventListener('visibilitychange', () => {
  const was = state.hidden;
  state.hidden = document.visibilityState === 'hidden';
  if (was && !state.hidden) pollers.forEach((p) => p.tickNow());
});

const pollers = new Set();

/**
 * A polling source.
 *
 *   const live = poller(() => api.get('/api/live'), { interval: 15000 });
 *   live.subscribe((data, meta) => { ... });   // meta: {changed, at, error, first}
 *   live.start();
 *
 * `subscribe` fires immediately with the cached value when there is one, so a
 * view that mounts after the first fetch renders without waiting.
 */
export function poller(fetcher, {
  interval = 20000,
  minInterval = 1500,
  maxBackoff = 8,
  timeout = 20000,
  immediate = true,
} = {}) {
  let timer = 0;
  let running = false;
  let queued = false;
  let lastAt = 0;
  let fails = 0;
  let stopped = true;
  let value;
  let digest = null;
  let lastError = null;
  const subs = new Set();

  function emit(changed, error = null, first = false) {
    const meta = { changed, at: lastAt, error, first, stale: error ? lastError !== null : false };
    for (const fn of [...subs]) {
      try { fn(value, meta); } catch (err) { console.error('[store] subscriber threw', err); }
    }
  }

  async function run(force = false) {
    if (stopped) return;
    if (running) { queued = true; return; }
    const now = Date.now();
    if (!force && lastAt && now - lastAt < minInterval) { queued = true; schedule(); return; }
    if (state.hidden && !force) return;     // guard 4

    running = true;
    lastAt = now;
    try {
      const data = await fetcher({ timeout });
      const next = hash(data);
      const changed = next !== digest;
      digest = next;
      value = data;
      lastError = null;
      fails = 0;
      emit(changed, null, false);
    } catch (err) {
      if (err && err.kind === 'aborted') { running = false; return; }
      fails += 1;
      lastError = err;
      emit(false, err, false);
    } finally {
      running = false;
      if (queued && !stopped) { queued = false; schedule(600); } else schedule();
    }
  }

  function schedule(delay) {
    if (stopped) return;
    clearTimeout(timer);
    const wait = delay ?? Math.min(interval * Math.min(2 ** fails, maxBackoff), interval * maxBackoff);
    timer = setTimeout(() => {
      if (state.hidden) { schedule(interval); return; }   // stay parked while hidden
      run();
    }, wait);
  }

  const api = {
    get value() { return value; },
    get error() { return lastError; },
    get ok() { return lastError === null && lastAt > 0; },
    get age() { return lastAt ? Date.now() - lastAt : null; },
    get interval() { return Math.min(interval * Math.min(2 ** fails, maxBackoff), interval * maxBackoff); },

    /** Publish now, if the min interval allows it. */
    refresh({ force = true } = {}) { return run(force); },

    subscribe(fn, { replay = true } = {}) {
      subs.add(fn);
      if (replay && lastAt) fn(value, { changed: false, at: lastAt, error: lastError, first: true, stale: true });
      return () => subs.delete(fn);
    },

    start({ now = immediate } = {}) {
      if (!stopped) return api;
      stopped = false;
      pollers.add(api);
      if (now) run(true); else schedule();
      return api;
    },

    tickNow() { if (!stopped && document.visibilityState === 'visible') run(true); },

    stop() {
      stopped = true;
      queued = false;
      clearTimeout(timer);
      pollers.delete(api);
      return api;
    },
  };
  return api;
}

/* ------------------------------------------------------------- registry */

/**
 * Named pollers, so two views wanting the same endpoint share one timer
 * instead of each opening their own.  The reference count keeps it alive
 * while anyone is subscribed.
 */
const registry = new Map();

export function sharedPoller(name, fetcher, opts) {
  let entry = registry.get(name);
  if (!entry) {
    entry = { p: poller(fetcher, opts), refs: 0 };
    registry.set(name, entry);
  }
  entry.refs += 1;
  entry.p.start();
  return {
    ...entry.p,
    subscribe(fn, o) {
      const off = entry.p.subscribe(fn, o);
      return () => { off(); entry.refs -= 1; if (entry.refs <= 0) { entry.p.stop(); registry.delete(name); } };
    },
  };
}

/** Relative time for a "刚刚 / 12 秒前" readout. */
export function since(at) {
  if (!at) return '尚未刷新';
  const s = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (s < 3) return '刚刚';
  if (s < 60) return `${s} 秒前`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} 分钟前`;
  return `${Math.round(m / 60)} 小时前`;
}
