/* ==========================================================================
   api -- the only place the app talks to the server.

   Three things it exists to get right:

   1. **Every failure has a name.**  A dropped socket, a timeout, a 500 and a
      well-formed "ok:false" envelope are four different situations that need
      four different words on screen.  Collapsing them into "加载失败" is how a
      credentials problem gets mistaken for an outage.

   2. **A refresh does not start a second copy of a request already in
      flight.**  Several views poll on independent timers, and a user mashing
      refresh used to multiply that.  Identical concurrent GETs share one
      promise; the sharing is dropped the moment the response lands.

   3. **A response that outlives its view is discarded.**  Each view render
      takes a signal, so navigating away aborts what the old view was waiting
      for instead of letting it write into a container that no longer exists.
   ========================================================================== */

const JSON_HEADERS = { Accept: 'application/json' };

export class ApiError extends Error {
  constructor(message, { kind = 'error', status = 0, detail = '', source = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;        // offline | timeout | http | server | aborted
    this.status = status;
    this.detail = detail;
    this.source = source;
  }
  /** A short word for a status pill. */
  get label() {
    return { offline: '无法连接', timeout: '响应超时', http: '服务错误',
             server: '服务报告异常', aborted: '已取消', error: '失败' }[this.kind] || '失败';
  }
}

/** Requests sharing (method,url) while one is in flight. */
const inflight = new Map();

function timeoutError(ms) {
  return new ApiError(`请求超过 ${Math.round(ms / 1000)} 秒未返回`, { kind: 'timeout' });
}

/**
 * GET a JSON document.
 *
 * Returns the *unwrapped* payload: for the v2 envelopes that is `data`, for
 * plain responses it is the body itself.  Throws ApiError on anything that is
 * not usable data.  `opts.raw` returns the whole envelope instead, for callers
 * that need `partial` / `source` / `ts`.
 */
export async function get(path, opts = {}) {
  const { timeout = 20000, signal, raw = false, dedupe = true } = opts;
  const key = `GET ${path}`;
  if (dedupe && inflight.has(key)) return inflight.get(key);

  const run = (async () => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(timeoutError(timeout)), timeout);
    // An outer signal (view teardown) also aborts this request.
    const onAbort = () => ctl.abort(signal.reason);
    if (signal) {
      if (signal.aborted) { clearTimeout(timer); throw asAborted(signal); }
      signal.addEventListener('abort', onAbort, { once: true });
    }

    let res;
    try {
      res = await fetch(path, { headers: JSON_HEADERS, signal: ctl.signal, cache: 'no-store' });
    } catch (err) {
      const abortedByTimeout = ctl.signal.reason instanceof ApiError;
      if (abortedByTimeout) throw ctl.signal.reason;
      if (signal && signal.aborted) throw asAborted(signal);
      if (err && err.name === 'AbortError') throw new ApiError('已取消', { kind: 'aborted' });
      // fetch only rejects for network-level failures, so this genuinely is
      // "the server is not there" -- not "the server said no".
      throw new ApiError('无法连接到本地服务', { kind: 'offline', detail: String(err && err.message || err) });
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    }

    let body = null;
    const text = await res.text().catch(() => '');
    if (text) { try { body = JSON.parse(text); } catch { body = null; } }

    if (!res.ok) {
      const detail = (body && (body.error || body.detail)) || text.slice(0, 200);
      throw new ApiError(detail || `HTTP ${res.status}`, { kind: 'http', status: res.status, detail });
    }

    if (body && typeof body === 'object' && 'ok' in body && 'data' in body && 'ts' in body) {
      if (!body.ok) {
        throw new ApiError(body.error || '服务报告异常',
          { kind: 'server', detail: body.error || '', source: body.source || '' });
      }
      return raw ? body : body.data;
    }

    // A non-envelope endpoint.  A 200 whose body is not JSON at all is not a
    // success: this workstation runs an unrelated service on 18080 that answers
    // 200 to every path, and a status-code-only client happily renders it.
    if (body === null) {
      throw new ApiError('响应不是 JSON（可能是别的服务占据了该端口）', { kind: 'server', status: res.status });
    }
    return raw ? { ok: true, data: body, error: null, ts: null, source: '', partial: false } : body;
  })();

  if (dedupe) {
    inflight.set(key, run);
    // Map hygiene must not add a rejection of its own.  `run.finally(...)`
    // returns a *derived* promise, and that one has no caller -- so any failed
    // GET would surface as an unhandled rejection even though every real
    // caller catches it.  `then(done, done)` handles both arms and returns a
    // promise that is always fulfilled.
    const done = () => { if (inflight.get(key) === run) inflight.delete(key); };
    run.then(done, done);
  }
  return run;
}

/**
 * POST with no body.  Carries the header the server requires to accept a
 * state-changing call -- see docs/security.md.  Without it the server answers
 * 403, which is the intended outcome for a cross-origin caller.
 */
export async function post(path, opts = {}) {
  const { timeout = 40000, signal } = opts;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(timeoutError(timeout)), timeout);
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { ...JSON_HEADERS, 'X-Requested-With': 'llm-lab-portal' },
      signal: ctl.signal,
    });
    const text = await res.text().catch(() => '');
    let body = null;
    if (text) { try { body = JSON.parse(text); } catch { body = null; } }
    if (!res.ok) {
      throw new ApiError((body && (body.error || body.note)) || `HTTP ${res.status}`,
        { kind: 'http', status: res.status });
    }
    return body;
  } catch (err) {
    if (ctl.signal.reason instanceof ApiError) throw ctl.signal.reason;
    if (err instanceof ApiError) throw err;
    throw new ApiError('无法连接到本地服务', { kind: 'offline', detail: String(err && err.message || err) });
  } finally {
    clearTimeout(timer);
  }
}

function asAborted(signal) {
  const e = new ApiError('已取消', { kind: 'aborted' });
  if (signal && signal.reason instanceof ApiError) return signal.reason;
  return e;
}

/** True for the one error we expect on every navigation and never report. */
export const isAborted = (err) => err instanceof ApiError && err.kind === 'aborted';

/** A per-view AbortController factory: new render aborts the previous one. */
export function makeScope() {
  let current = new AbortController();
  return {
    get signal() { return current.signal; },
    renew() { current.abort(new ApiError('视图已切换', { kind: 'aborted' })); current = new AbortController(); return current.signal; },
    abort() { current.abort(new ApiError('视图已销毁', { kind: 'aborted' })); },
  };
}
