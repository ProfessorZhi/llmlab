/*! web/js/features/source.js — where the ops views get their data from.
 *
 *  Three modes, resolved before anything is drawn:
 *
 *      mock        in-process fixture only by explicit `?demo=1` / true, never
 *                  by an untrusted `?source=mock` legacy shortcut.
 *      live        same-origin `GET /api/v2/ops` when a reviewed BFF base has
 *                  been injected, including on the regular URL; backend still
 *                  checks the current authenticated subject and resource ACL.
 *      unavailable missing, untrusted or unsupported source; shown explicitly
 *                  rather than constructing synthetic model rows.
 *
 *  The rule this file exists to enforce is the same one `web/js/data/contract.js`
 *  applies to the Atlas, and the same one `backend/portal/modules.py` applies to
 *  links: **the browser is never handed a way off the origin.** So:
 *
 *   * the base must be the empty string (same origin) or a root-relative path;
 *     `https://…`, `//host/…` and anything containing `:8080` are refused before
 *     a request is built, using `isInternalHref` — one implementation, the
 *     reviewed one;
 *   * an unrecognised `?source=` value resolves to `unavailable`, because a typo
 *     is not a licence to serve the fixture;
 *   * there is no silent production fallback, and no ambient `fetch` is captured
 *     at import time.
 *
 *  This is the ops layer's **single** network site. `render.js` is pure,
 *  `contract.js` and `capability.js` hold no IO, and the fixture answers
 *  in-process. `web/README.md` §7.1 records the two pre-existing exceptions
 *  (`calls/`, `modules/`); this is the third, and it is one `fetch` in one
 *  function — see `docs/v6-ui-flows.md` §"网络边界".
 */

import { isInternalHref } from '../modules/contract.js';
import {
  OPS_ENDPOINT,
  createMockHttp,
  sharePath,
} from './mock-fixture.js';
import {
  OPS_STATES,
  STATE_CHANGING_STATUSES,
  emptyOps,
  validateOps,
} from './contract.js';
import {
  EVENTS_DEFAULT_LIMIT,
  EVENTS_MAX_LIMIT,
  eventsOverallState,
  validateEventsDto,
} from './events.js';

export const SOURCE_MODES = Object.freeze({
  MOCK: 'mock',
  LIVE: 'live',
  UNAVAILABLE: 'unavailable',
});

/** Fallback when `?persona=` is absent. Named here so the demo default is one
 *  visible constant instead of a string buried in a call site. */
export const DEFAULT_PERSONA = 'user';

/** Is this a path the browser may issue a request to? The empty string means
 *  "this origin", which is allowed; everything else must pass the same-origin
 *  test the module links use. */
export const isSameOriginBase = (base) =>
  base === '' || base === undefined || base === null || isInternalHref(base);

/**
 * Decide where ops data should come from.
 *
 * @param {{search?: string, host?: {opsBase?: string}, allowDemo?: boolean}} [input]
 *   `search` — `window.location.search`. `host` — an injected config object
 *   (normally `globalThis.__LLM_LAB_OPS__`). Only a base path; this repo carries
 *   no credentials. `allowDemo` — true only on the isolated QA artifact; absent
 *   or false means the samples are not reachable here.
 * @returns {{mode: string, requested: string|null, base: string|null, persona: string, reason: string}}
 */
export function resolveFeatureSource(input = {}) {
  const search = input.search ?? '';
  const host = input.host ?? {};
  const allowDemo = input.allowDemo === true;
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  const requested = params.get('source');
  const persona = params.get('persona') || DEFAULT_PERSONA;
  const demo = params.get('demo');

  // All synthetic management rows require the same two gates as the shell,
  // Calls and Atlas: the isolated QA artifact, and then an explicit opt-in. A
  // production URL cannot reach them, and a legacy ?source=mock cannot bypass
  // either gate.
  if (allowDemo && (demo === '1' || demo === 'true')) {
    return { mode: SOURCE_MODES.MOCK, requested, base: null, persona, reason: 'design-samples-opt-in' };
  }
  if (requested !== null && requested !== SOURCE_MODES.LIVE) {
    return { mode: SOURCE_MODES.UNAVAILABLE, requested, base: null, persona, reason: 'unknown-source-value' };
  }

  // Real browser requests are allowed only with an operator-configured
  // same-origin BFF path. Presence of the path does not grant any permission:
  // the backend rechecks current Session, scopes, Owner and ACL for each call.
  const base = host.opsBase;
  if (base === undefined || base === null) {
    return {
      mode: SOURCE_MODES.UNAVAILABLE,
      requested,
      base: null,
      persona,
      reason: requested === SOURCE_MODES.LIVE ? 'no-endpoint-configured' : 'auth-bff-not-connected',
    };
  }
  if (!isSameOriginBase(base)) {
    return { mode: SOURCE_MODES.UNAVAILABLE, requested, base: null, persona, reason: 'endpoint-is-not-same-origin' };
  }
  return {
    mode: SOURCE_MODES.LIVE,
    requested,
    base,
    persona,
    reason: requested === SOURCE_MODES.LIVE ? 'explicit-live' : 'configured-same-origin',
  };
}

/** The one audit path the UI may call. Fixed on purpose: no caller-supplied
 *  `path`, `url`, `promql`, `source`, `uid` or `owner` can reach it, and the
 *  only variable is a limit bounded by the contract (`backend/bff/events.py`). */
export const EVENTS_ENDPOINT = '/api/portal/events';

/** Build the URL for a same-origin endpoint. Refuses anything else outright
 *  rather than trusting the caller to have checked. */
export function endpointUrl(base, path) {
  const prefix = base === '' || base === undefined || base === null ? '' : base;
  const url = `${prefix}${path}`;
  if (!isInternalHref(url)) throw new Error(`refusing a non-same-origin endpoint: ${url}`);
  return url;
}

/**
 * Create the ops source.
 *
 * @param {object} opts
 * @param {'mock'|'live'|'unavailable'} opts.mode
 * @param {string} [opts.base]        same-origin base for `live`
 * @param {typeof fetch} [opts.fetchImpl]  injected; in `mock` mode the fixture
 *        answers instead and this is never called — which a test asserts rather
 *        than assumes.
 * @param {string} [opts.persona]     fixture persona, `mock` mode only
 */
export function createFeatureSource({
  mode = SOURCE_MODES.UNAVAILABLE,
  base = '',
  fetchImpl,
  persona = DEFAULT_PERSONA,
} = {}) {
  const mock = mode === SOURCE_MODES.MOCK ? createMockHttp({ persona }) : null;
  // Held in the closure so a visibility switch survives the reload it triggers,
  // in both modes, without the caller having to thread it back in.
  let visibility = 'private';

  const config = { mode, base, fetchImpl, mock };

  return {
    mode,
    persona,
    synthetic: mode === SOURCE_MODES.MOCK,
    get visibility() { return visibility; },
    /** The fixture behind a mock source, so a test can assert the *stored*
     *  result of a write rather than the response that claimed it. `null` in
     *  every other mode — there is no server state to inspect. */
    get fixture() { return mock; },
    get transport() {
      return mode === SOURCE_MODES.MOCK
        ? mock.fetch
        : transportFor(fetchImpl);
    },
    load: () => load({ ...config, visibility }),
    /** The operator audit timeline. Same transport, same status ladder as
     *  `load` — and the same rule that only a live source opens a socket. */
    loadEvents: ({ limit = EVENTS_DEFAULT_LIMIT } = {}) => loadEvents({
      mode, base, fetchImpl, limit,
    }),
    setVisibility: (next) => {
      if (next !== 'private' && next !== 'shared') {
        return Promise.resolve({ ok: false, status: 0, reason: 'unsupported-visibility' });
      }
      visibility = next;
      return load({ ...config, visibility });
    },
    updateShare: (id, change) => updateShare({ ...config, id, change }),
  };
}

/** Resolve the transport lazily so a Node test can run this file with a stub
 *  and an injected `fetchImpl` always wins over the ambient one. */
const transportFor = (fetchImpl) =>
  fetchImpl ?? (typeof globalThis.fetch === 'function' ? globalThis.fetch.bind(globalThis) : null);

function visibilityQuery(visibility) {
  return visibility && visibility !== 'private'
    ? `?visibility=${encodeURIComponent(visibility)}`
    : '';
}

/** Shared response handling: the same 401/403/!ok/!json/contract ladder the
 *  call-log and module controllers use, so the three views cannot disagree
 *  about what a status code means. */
async function interpret(response) {
  if (STATE_CHANGING_STATUSES.includes(response.status)) {
    const state = response.status === 401 ? OPS_STATES.SIGNED_OUT : OPS_STATES.DENIED;
    return { state, ops: emptyOps(state, `http-${response.status}`), reason: `http-${response.status}`, errors: [] };
  }
  if (!response.ok) {
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, `http-${response.status}`),
      reason: `http-${response.status}`,
      errors: [],
    };
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, 'not-json'),
      reason: 'not-json',
      errors: [],
    };
  }
  const checked = validateOps(payload?.data?.ops);
  if (!checked.ok) {
    // Nothing is painted from a payload that fails its own contract: half a
    // management view looks authoritative while being wrong.
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, `contract:${checked.errors[0]}`),
      reason: `contract:${checked.errors[0]}`,
      errors: checked.errors,
    };
  }
  return { state: OPS_STATES.READY, ops: checked.ops, reason: null, errors: [] };
}

async function load({ mode, base, fetchImpl, mock, visibility = 'private' }) {
  if (mode === SOURCE_MODES.MOCK) {
    // In-process: no socket is opened, so there is no way for this path to
    // reach anything, anywhere.
    const response = await mock.fetch(`${OPS_ENDPOINT}${visibilityQuery(visibility)}`);
    return interpret(response);
  }
  if (mode !== SOURCE_MODES.LIVE) {
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, 'unconfigured-source'),
      reason: 'unconfigured-source',
      errors: [],
    };
  }

  const doFetch = transportFor(fetchImpl);
  if (!doFetch) {
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, 'no-fetch-available'),
      reason: 'no-fetch-available',
      errors: [],
    };
  }
  let url;
  try {
    url = endpointUrl(base, `${OPS_ENDPOINT}${visibilityQuery(visibility)}`);
  } catch (error) {
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, 'endpoint-rejected'),
      reason: 'endpoint-rejected',
      errors: [String(error && error.message || error)],
    };
  }

  let response;
  try {
    response = await doFetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
    return {
      state: OPS_STATES.UNKNOWN,
      ops: emptyOps(OPS_STATES.UNKNOWN, 'request-failed'),
      reason: 'request-failed',
      errors: [],
    };
  }
  return interpret(response);
}

/**
 * Read the operator audit timeline.
 *
 * Same shape as `load`, and deliberately the same status ladder: 401 is
 * "sign in", 403 is "ask an operator", 404/501/503 mean this deployment does
 * not provide the route (503 is the BFF's `portal.events_not_configured`), and
 * an invalid payload is `unknown` rather than a half-rendered table.
 *
 * The path is fixed and the limit is clamped by the contract. Neither the
 * caller nor the URL can select a ledger, a user, a file or a PromQL.
 *
 * @returns {Promise<{state: string, reason: string|null, dto: object|null}>}
 */
async function loadEvents({ mode, base, fetchImpl, limit = EVENTS_DEFAULT_LIMIT }) {
  if (mode !== SOURCE_MODES.LIVE) {
    // Mock mode is the design sample: it has no audit ledger, and inventing one
    // would be exactly the fabricated audit row the contract forbids.
    return { state: 'unconfigured', reason: 'source-unavailable', dto: null };
  }
  const bounded = Number.isInteger(limit) && limit >= 1 && limit <= EVENTS_MAX_LIMIT
    ? limit
    : EVENTS_DEFAULT_LIMIT;
  const doFetch = transportFor(fetchImpl);
  if (!doFetch) return { state: 'unknown', reason: 'no-fetch-available', dto: null };

  let url;
  try {
    url = endpointUrl(base, `${EVENTS_ENDPOINT}?limit=${bounded}`);
  } catch {
    return { state: 'unknown', reason: 'endpoint-rejected', dto: null };
  }

  let response;
  try {
    response = await doFetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
    return { state: 'unknown', reason: 'request-failed', dto: null };
  }

  if (STATE_CHANGING_STATUSES.includes(response.status)) {
    return {
      state: response.status === 401 ? 'signed_out' : 'denied',
      reason: `http-${response.status}`,
      dto: null,
    };
  }
  if ([404, 501, 503].includes(response.status)) {
    return { state: 'not_configured', reason: `http-${response.status}`, dto: null };
  }
  if (!response.ok) {
    return { state: 'unknown', reason: `http-${response.status}`, dto: null };
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    return { state: 'unknown', reason: 'not-json', dto: null };
  }
  const checked = validateEventsDto(payload?.data);
  if (!checked.ok) {
    return { state: 'unknown', reason: `contract:${checked.errors[0]}`, dto: null };
  }
  return { state: eventsOverallState(checked.dto), reason: null, dto: checked.dto };
}

/**
 * Change a deployment's share scope.
 *
 * The brief's requirement is that every button does something real, and that
 * "does something" is verifiable. So this is a real request against the fixture
 * (mock) or the BFF (live), and the caller re-reads afterwards. In `live` mode
 * this build has no server to answer, and the capability gate in the controller
 * has already disabled the control — this function is the second lock.
 *
 * @returns {Promise<{ok: boolean, status: number, reason: string|null, deployment?: object}>}
 */
async function updateShare({ mode, base, fetchImpl, mock, id, change }) {
  if (mode === SOURCE_MODES.UNAVAILABLE) {
    return { ok: false, status: 0, reason: 'source-unavailable' };
  }
  if (!id) return { ok: false, status: 0, reason: 'missing-deployment-id' };

  const doFetch = mode === SOURCE_MODES.MOCK ? mock.fetch : transportFor(fetchImpl);
  if (!doFetch) return { ok: false, status: 0, reason: 'no-fetch-available' };

  let url;
  try {
    url = mode === SOURCE_MODES.MOCK ? sharePath(id) : endpointUrl(base, sharePath(id));
  } catch (error) {
    return { ok: false, status: 0, reason: 'endpoint-rejected', errors: [String(error?.message ?? error)] };
  }

  let response;
  try {
    response = await doFetch(url, {
      method: 'PUT',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(change),
    });
  } catch {
    return { ok: false, status: 0, reason: 'request-failed' };
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const code = payload?.error?.code ?? `http-${response.status}`;
    return { ok: false, status: response.status, reason: code };
  }
  const deployment = payload?.data?.deployment;
  if (!deployment) return { ok: false, status: response.status, reason: 'contract:no-deployment' };
  return { ok: true, status: response.status, reason: null, deployment };
}

// --------------------------------------------------------------------------
// 共享管理员模式的两条同源调用
//
// 放在这个文件里，而不是新开一个网络点：``endpointUrl`` 的同源守卫是**已经
// 审查过的那一份**，再抄一遍只会有两份会漂移的校验。这里唯一的写入是
// ``POST /auth/admin/enter|exit``，而且
//
//   * ``password`` 只出现在这一次请求体里，不落 localStorage / sessionStorage，
//     不写进 URL，不回显，不需要时立刻丢；
//   * 除了既有的 ``X-CSRF-Token``，不额外设置任何头 —— 尤其没有 ``X-Admin``。
//     服务端的提权状态只由服务端会话决定，客户端能加的头一个都不该有。
// --------------------------------------------------------------------------

export const ADMIN_MODE_PATHS = Object.freeze({
  status: '/auth/admin/status',
  enter: '/auth/admin/enter',
  exit: '/auth/admin/exit',
});

/** 服务端承诺的时长上限，与 ``backend/bff/admin_mode.py`` 的硬上限一致。
 *  前端拿它当**显示**上限：服务端说 3600，界面也不会照抄成 3600。 */
export const ADMIN_MODE_TTL_CAP_SECONDS = 900;

/** The existing auth service issues a single-use CSRF token bound to the
 * current cookie. Keep this separate from ADMIN_MODE_PATHS: the latter is
 * precisely the three admin routes and is a tested public contract. */
export const ADMIN_MODE_CSRF_PATH = '/auth/csrf';

/** Get a new single-use token for each admin-mode write. No synthetic token
 * is ever generated, and an anonymous CSRF response is not usable. */
export async function adminModeCsrf({ mode, base, fetchImpl } = {}) {
  if (mode !== SOURCE_MODES.LIVE) {
    return { ok: false, status: 0, reason: 'no-server-session' };
  }
  const doFetch = transportFor(fetchImpl);
  if (!doFetch) return { ok: false, status: 0, reason: 'no-fetch-available' };
  let url;
  try {
    url = endpointUrl(base, ADMIN_MODE_CSRF_PATH);
  } catch {
    return { ok: false, status: 0, reason: 'endpoint-rejected' };
  }

  let response;
  try {
    response = await doFetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
    return { ok: false, status: 0, reason: 'request-failed' };
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    return { ok: false, status: response.status, reason: 'contract:invalid-json' };
  }
  if (!response.ok) {
    return { ok: false, status: response.status,
      reason: payload?.error?.code ?? `http-${response.status}` };
  }
  const csrfToken = payload?.data?.csrf_token;
  if (payload?.ok !== true || payload?.data?.authenticated !== true
    || typeof csrfToken !== 'string' || !csrfToken) {
    return { ok: false, status: response.status, reason: 'csrf-unbound' };
  }
  return { ok: true, status: response.status, reason: null, csrfToken };
}


const adminTransport = ({ mode, base, fetchImpl }) => {
  if (mode !== SOURCE_MODES.LIVE) {
    // 预览（mock）里没有服务端会话，因此没有可提权的对象。如实说，而不是
    // 造一个看起来能按的按钮。
    return { ok: false, status: 0, reason: 'no-server-session' };
  }
  const doFetch = transportFor(fetchImpl);
  if (!doFetch) return { ok: false, status: 0, reason: 'no-fetch-available' };
  let url;
  try {
    url = endpointUrl(base, ADMIN_MODE_PATHS.status);
  } catch (error) {
    return { ok: false, status: 0, reason: 'endpoint-rejected', errors: [String(error?.message ?? error)] };
  }
  return { doFetch, url };
};

/**
 * 读当前会话的管理员模式状态。
 * @returns {Promise<{ok: boolean, status: number, reason: string|null, adminMode?: object}>}
 */
export async function adminModeStatus({ mode, base, fetchImpl } = {}) {
  const ready = adminTransport({ mode, base, fetchImpl });
  if (!ready.doFetch) return ready;

  let response;
  try {
    response = await ready.doFetch(ready.url, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
  } catch {
    return { ok: false, status: 0, reason: 'request-failed' };
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    return { ok: false, status: response.status, reason: payload?.error?.code ?? `http-${response.status}` };
  }
  const adminMode = payload?.data?.admin_mode;
  if (!adminMode || typeof adminMode.configured !== 'boolean') {
    // 形状不对就当"未知"，绝不当"已提权"。
    return { ok: false, status: response.status, reason: 'contract:no-admin-mode' };
  }
  return { ok: true, status: response.status, reason: null, adminMode };
}

/**
 * 进入或退出管理员模式。
 *
 * ``password`` 是本函数唯一的秘密参数：它被放进请求体，然后这个函数就再也
 * 不持有它。调用方负责在拿到结果后清空输入框。
 */
export async function adminModeAction({ mode, base, fetchImpl, action, password, csrfToken } = {}) {
  if (action !== 'enter' && action !== 'exit') {
    return { ok: false, status: 0, reason: 'unsupported-action' };
  }
  const ready = adminTransport({ mode, base, fetchImpl });
  if (!ready.doFetch) return ready;

  let url;
  try {
    url = endpointUrl(base, ADMIN_MODE_PATHS[action]);
  } catch (error) {
    return { ok: false, status: 0, reason: 'endpoint-rejected', errors: [String(error?.message ?? error)] };
  }
  if (typeof csrfToken !== 'string' || !csrfToken) {
    return { ok: false, status: 0, reason: 'missing-csrf-token' };
  }

  const headers = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'X-CSRF-Token': csrfToken,
  };
  const body = action === 'enter' ? { password: String(password ?? '') } : {};

  let response;
  try {
    response = await ready.doFetch(url, {
      method: 'POST',
      headers,
      credentials: 'same-origin',
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, reason: 'request-failed' };
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    return { ok: false, status: response.status, reason: payload?.error?.code ?? `http-${response.status}` };
  }
  const adminMode = payload?.data?.admin_mode;
  if (!adminMode || typeof adminMode.configured !== 'boolean') {
    return { ok: false, status: response.status, reason: 'contract:no-admin-mode' };
  }
  return { ok: true, status: response.status, reason: null, adminMode };
}
