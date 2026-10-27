/* A session-owned, short-lived read cache. No localStorage and no secret responses. */
export function createReadCache(load, { ttlMs = 5000, now = Date.now, revision = () => 0 } = {}) {
  let cached = null, expires = 0, pending = null, epoch = revision();
  const aborted = () => ({ state: 'error', value: null, reason: 'aborted' });
  function clear() {
    cached = null; expires = 0;
    pending?.controller.abort(); pending = null;
    epoch = revision();
  }
  function sync() { if (epoch !== revision()) clear(); }
  function peek() { sync(); return cached; }
  function read({ signal, force = false } = {}) {
    sync();
    if (signal?.aborted) return Promise.resolve(aborted());
    if (!force && cached && now() < expires) return Promise.resolve(cached);
    if (!pending) {
      const entry = { controller: new AbortController(), users: new Set(), promise: null };
      pending = entry;
      entry.promise = Promise.resolve().then(() => load({ signal: entry.controller.signal })).then(result => {
        if (pending === entry && epoch === revision() && !entry.controller.signal.aborted) {
          if (result.state === 'ready') { cached = result; expires = now() + ttlMs; }
          else if (['signed_out', 'denied'].includes(result.state)) cached = null;
        }
        return epoch === revision() && !entry.controller.signal.aborted ? result : aborted();
      }, () => ({ state: 'error', value: null, reason: 'network' })).finally(() => { if (pending === entry) pending = null; });
    }
    const entry = pending;
    return new Promise(resolve => {
      const token = {}; entry.users.add(token);
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true; signal?.removeEventListener('abort', cancel); entry.users.delete(token); resolve(value);
      };
      const cancel = () => {
        finish(aborted());
        if (!entry.users.size) { entry.controller.abort(); if (pending === entry) pending = null; }
      };
      signal?.addEventListener('abort', cancel, { once: true });
      entry.promise.then(finish);
    });
  }
  return { read, peek, clear };
}
