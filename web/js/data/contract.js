/*! web/js/data/contract.js
 *  Source isolation + the remote DTO contract.
 *
 *  Rules this module exists to enforce (from prototypes/v5/DATA-ATLAS-NOTES.md):
 *    1. The Atlas is LIVE-or-UNCONFIGURED by default. A demo needs the isolated
 *       QA build *and* an explicit ?demo=1; on production the query is inert.
 *    2. A same-origin authorized endpoint is required; missing configuration is
 *       an honest unconfigured state. It must never fall back to a synthetic
 *       fixture or accept a caller-provided external upstream URL.
 *    3. A payload claiming `synthetic:false` is only accepted if it actually
 *       validates; anything else becomes an `unknown` state, not zeros.
 *    4. Missing metrics stay `null` (rendered as "—"), never 0.
 *
 *  No DOM, no network here — `remote-client.js` does the fetching so this file
 *  stays unit-testable and cannot itself reach the network.
 */

export const SOURCE_MODES = Object.freeze({
  DEMO: 'demo',
  REMOTE: 'remote',
  UNCONFIGURED: 'unconfigured',
});

/** UI-facing state for the atlas data banner. */
export const DATA_STATES = Object.freeze({
  DEMO: 'demo',        // opt-in synthetic fixture, labelled as such
  LIVE: 'live',        // validated, scoped same-origin BFF payload
  EMPTY: 'empty',      // authorized BFF returned no matching rows
  UNKNOWN: 'unknown',  // no route, denied, invalid payload or network error
});

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const isStr = (v) => typeof v === 'string' && v.length > 0;
const isCount = (v) => v === null || (typeof v === 'number' && Number.isFinite(v) && v >= 0);

/** Only a fixed same-origin BFF path, never a client-selectable upstream URL. */
export const isTrustedAtlasPath = (path) => (
  typeof path === 'string' && /^\/api\/atlas\/[a-zA-Z0-9_/-]+$/.test(path)
  && !path.includes('..') && !path.includes('//')
);

/**
 * Select a source without ever promoting an in-process sample to a live fact.
 *
 * The regular page is LIVE-or-UNCONFIGURED by default; a reviewed same-origin
 * BFF path must be injected by the hosting environment before any network call
 * is permitted. Its absence means "not configured", not "there were 0 calls".
 *
 * Demo mode needs BOTH the isolated QA build (`allowDemo`, false everywhere
 * else) and the explicit ?demo=1/true opt-in — never implicit, and never a
 * legacy ?source=demo alias. Browser selection never supplies authentication,
 * source trust or row-level ACL — those are enforced by the BFF.
 *
 * @param {{search?: string, host?: {endpoint?: string}, allowDemo?: boolean}} [input]
 * @returns {{mode:string, requested:string|null, endpoint:string|null, reason:string}}
 */
export function resolveSource(input = {}) {
  const search = input.search ?? '';
  const host = input.host ?? {};
  const allowDemo = input.allowDemo === true;
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  const requested = params.get('source');
  const demo = params.get('demo');

  // The opt-in only asks; the build answers. `allowDemo` is only ever true on
  // the isolated QA artifact (see web/js/build-profile.js), so on production a
  // `?demo=1` in the URL falls through to UNCONFIGURED like any other URL.
  if (allowDemo && (demo === '1' || demo === 'true')) {
    return { mode: SOURCE_MODES.DEMO, requested, endpoint: null, reason: 'explicit-demo' };
  }
  if (requested !== null && requested !== SOURCE_MODES.REMOTE) {
    return { mode: SOURCE_MODES.UNCONFIGURED, requested, endpoint: null, reason: 'unknown-source-value' };
  }
  const endpoint = isStr(host.endpoint) ? host.endpoint : null;
  if (!endpoint) {
    return { mode: SOURCE_MODES.UNCONFIGURED, requested, endpoint: null, reason: 'no-endpoint-configured' };
  }
  if (!isTrustedAtlasPath(endpoint)) {
    return { mode: SOURCE_MODES.UNCONFIGURED, requested, endpoint: null, reason: 'endpoint-not-approved' };
  }
  return {
    mode: SOURCE_MODES.REMOTE,
    requested,
    endpoint,
    reason: requested === SOURCE_MODES.REMOTE ? 'explicit-remote' : 'configured-same-origin',
  };
}

/** A shape the UI can always render, whatever went wrong upstream. */
export function makeState(state, detail = {}) {
  return {
    state,
    synthetic: state === DATA_STATES.DEMO,
    groups: [],
    workloads: [],
    notes: detail.notes ? [...detail.notes] : [],
    reason: detail.reason ?? null,
    window: detail.window ?? null,
    scope: detail.scope ?? null,
    quality: detail.quality ?? null,
  };
}

export const demoState = (workloads, groups) => ({
  ...makeState(DATA_STATES.DEMO, { notes: ['Synthetic UI fixture — no production service was queried.'] }),
  synthetic: true,
  groups: groups.map((g) => ({ ...g })),
  workloads: workloads.map((w) => ({ ...w })),
});

export const emptyState = (reason) => makeState(DATA_STATES.EMPTY, { reason });
export const unknownState = (reason) => makeState(DATA_STATES.UNKNOWN, { reason });

/**
 * Validate a remote DTO. Returns `{ok, errors, state}` — never throws, so a bad
 * payload turns into a renderable unknown state instead of a blank page.
 */
export function validateAtlasBundle(raw) {
  const errors = [];
  if (!isObj(raw)) return { ok: false, errors: ['payload is not an object'], state: unknownState('contract:not-an-object') };

  if (raw.synthetic !== false) {
    // A remote payload that admits to being synthetic, or omits the flag, is
    // rejected outright: mixing fixtures into a "real" surface is the exact
    // failure this contract exists to prevent.
    errors.push('synthetic must be exactly false for a remote payload');
  }
  if (!Array.isArray(raw.groups)) errors.push('groups must be an array');

  const groups = [];
  const workloads = [];
  if (Array.isArray(raw.groups)) {
    raw.groups.forEach((g, gi) => {
      if (!isObj(g)) { errors.push(`groups[${gi}] is not an object`); return; }
      if (!isStr(g.id)) errors.push(`groups[${gi}].id must be a non-empty string`);
      if (!isCount(g.calls)) errors.push(`groups[${gi}].calls must be null or a finite number >= 0`);
      const gid = isStr(g.id) ? g.id : `group-${gi}`;
      groups.push({ id: gid, name: isStr(g.name) ? g.name : gid, calls: g.calls ?? null });
      if (!Array.isArray(g.workloads)) { if (g.workloads !== undefined) errors.push(`groups[${gi}].workloads must be an array`); return; }
      g.workloads.forEach((w, wi) => {
        if (!isObj(w)) { errors.push(`groups[${gi}].workloads[${wi}] is not an object`); return; }
        if (!isStr(w.id)) errors.push(`groups[${gi}].workloads[${wi}].id must be a non-empty string`);
        if (!isCount(w.calls)) errors.push(`groups[${gi}].workloads[${wi}].calls must be null or a finite number >= 0`);
        if (!isCount(w.p95)) errors.push(`groups[${gi}].workloads[${wi}].p95 must be null or a finite number >= 0`);
        if (!isCount(w.tokens)) errors.push(`groups[${gi}].workloads[${wi}].tokens must be null or a finite number >= 0`);
        workloads.push({
          id: isStr(w.id) ? w.id : `${gid}-${wi}`,
          g: gid,
          name: isStr(w.name) ? w.name : (isStr(w.id) ? w.id : `${gid}-${wi}`),
          calls: w.calls ?? null,
          p95: w.p95 ?? null,
          tokens: w.tokens ?? null,
          desc: isStr(w.desc) ? w.desc : '',
        });
      });
    });
  }

  if (errors.length) return { ok: false, errors, state: unknownState('contract:invalid-payload') };

  const state = {
    ...makeState(workloads.length ? DATA_STATES.LIVE : DATA_STATES.EMPTY, {
      notes: Array.isArray(raw.notes) ? raw.notes.filter(isStr) : [],
      window: isObj(raw.window) ? raw.window : null,
      scope: isStr(raw.scope) ? raw.scope : null,
      quality: isObj(raw.quality) ? raw.quality : null,
    }),
    synthetic: false,
    groups,
    workloads,
  };
  return { ok: true, errors: [], state };
}

/**
 * Should the demo fixture be shown? Only when the resolved mode is DEMO.
 * Exported so the rule is asserted in tests rather than re-derived in the UI.
 */
export const usesDemoFixture = (resolved) => resolved.mode === SOURCE_MODES.DEMO;
