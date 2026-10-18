/*! web/js/data/remote-client.js
 *  The ONLY module in web/ allowed to touch the network — and only when the
 *  caller has explicitly opted in via `?source=remote` AND an endpoint was
 *  injected. `load()` is never called from the demo boot path.
 *
 *  No credentials live here. The client sends the browser session cookie
 *  (same-origin) and expects the Portal BFF to have already applied RBAC; the
 *  aggregation contract is enforced by validateAtlasBundle, not by trust.
 */
import { validateAtlasBundle, unknownState, emptyState } from './contract.js';

/**
 * @param {object} opts
 * @param {string} opts.endpoint  absolute or same-origin URL of the aggregate DTO
 * @param {typeof fetch} [opts.fetchImpl]  injectable for tests (keeps this module offline-testable)
 * @param {number} [opts.timeoutMs]
 */
export function createRemoteClient({ endpoint, fetchImpl, timeoutMs = 8000 } = {}) {
  if (typeof endpoint !== 'string' || !endpoint) {
    throw new Error('createRemoteClient requires an endpoint');
  }
  // Never inherit an ambient global fetch at import time — resolve lazily so a
  // test can run this file in Node with a stub.
  const doFetch = fetchImpl ?? (typeof fetch === 'function' ? fetch.bind(globalThis) : null);

  return {
    endpoint,

    /** @returns {Promise<{state:object, ok:boolean, errors:string[]}>} */
    async load() {
      if (!doFetch) return { state: unknownState('remote:no-fetch-available'), ok: false, errors: ['fetch unavailable'] };

      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      const timer = controller && setTimeout(() => controller.abort(), timeoutMs);
      let res;
      try {
        res = await doFetch(endpoint, {
          method: 'GET',
          headers: { Accept: 'application/json' },
          credentials: 'same-origin',
          signal: controller ? controller.signal : undefined,
        });
      } catch (err) {
        if (timer) clearTimeout(timer);
        return { state: unknownState('remote:request-failed'), ok: false, errors: [String(err && err.message || err)] };
      }
      if (timer) clearTimeout(timer);

      if (!res.ok) {
        return { state: unknownState(`remote:http-${res.status}`), ok: false, errors: [`HTTP ${res.status}`] };
      }
      let payload;
      try {
        payload = await res.json();
      } catch {
        return { state: unknownState('remote:not-json'), ok: false, errors: ['response was not JSON'] };
      }
      const verdict = validateAtlasBundle(payload);
      if (!verdict.ok) return { state: verdict.state, ok: false, errors: verdict.errors };
      if (!verdict.state.workloads.length) {
        return { state: emptyState('remote:no-workloads'), ok: true, errors: [] };
      }
      return { state: verdict.state, ok: true, errors: [] };
    },
  };
}
