/*! web/js/modules/controller.js — one fetch, then every module panel on the page.
 *
 *  The module catalogue is fetched once per visibility change and painted into
 *  every `[data-module]` panel the shell provides, so `models`, `api`,
 *  `observability`, `evaluation` and `admin` all read from a single, consistent
 *  answer instead of five independent ones that could disagree with each other.
 *
 *  The page state machine mirrors `../calls/controller.js` exactly, because the
 *  server's answers are the same shape:
 *
 *     200 → ready | the panels render, each with its own state and reason
 *     401 → signed_out   ("sign in")
 *     403 → denied       ("ask an operator") — never retried automatically
 *     anything else / unparsable / contract violation → unknown
 *
 *  NOTE for reviewers: like the call log, this is the module layer's single
 *  `fetch`. `web/README.md` §7.1 records why the network call lives under
 *  `web/js/modules/` rather than `web/js/data/`.
 */

import { $, $$ } from '../dom.js';
import {
  PORTAL_STATES,
  STATE_CHANGING_STATUSES,
  emptyPortal,
  validatePortal,
} from './contract.js';
import {
  panelStateText,
  renderEndpoints,
  renderModulePanel,
  renderNotice,
  renderPlaygroundNote,
  renderTransportStrip,
  stateFor as renderStateFor,
} from './render.js';

/** The path the preview serves. A production BFF would serve it too. */
export const MODULES_ENDPOINT = '/api/atlas/modules';

/** Panels whose body is not just the module card: the playground owns the
 *  interactive-backend caveat, and the endpoint visibility table belongs with
 *  the API view. */
const PANEL_EXTRAS = Object.freeze({
  playground: renderPlaygroundNote,
});

export function createModules({ endpoint = MODULES_ENDPOINT, fetchImpl, doc = globalThis.document } = {}) {
  const panels = $$('[data-module]', doc);
  const hosts = new Map();
  for (const panel of panels) {
    hosts.set(panel.dataset.module, {
      panel,
      body: $(`#module-${panel.dataset.module}-body`, doc) ?? panel,
      state: $(`[data-module-state="${panel.dataset.module}"]`, doc),
    });
  }

  let portal = null;
  let state = PORTAL_STATES.UNKNOWN;
  let visibility = 'private';
  // Why the last load produced no payload; a null portal cannot carry it.
  let lastReason = null;

  const resolveFetch = () =>
    fetchImpl ?? (typeof globalThis.fetch === 'function' ? globalThis.fetch.bind(globalThis) : null);

  function paint() {
    const strip = $('#modulesTransport', doc);
    if (strip) strip.innerHTML = renderTransportStrip(portal);

    const stateText = (moduleId) =>
      portal ? panelStateText(portal, moduleId) : '等待会话';

    for (const [moduleId, host] of hosts) {
      if (!portal) {
        host.body.innerHTML = renderNotice(state, portal?.reason ?? lastReason);
        if (host.state) host.state.textContent = '等待会话';
        continue;
      }
      const extra = PANEL_EXTRAS[moduleId]?.(portal) ?? '';
      const endpoints = moduleId === 'playground' ? renderEndpoints(portal) : '';
      host.body.innerHTML = renderModulePanel(portal, moduleId) + extra + endpoints;
      if (host.state) host.state.textContent = stateText(moduleId);
    }

    const liveHost = $('#modulesHost', doc);
    if (liveHost && !panels.length) liveHost.innerHTML = renderNotice(state, portal?.reason ?? lastReason);

    bindPanelControls();
  }

  /** Controls inside freshly-rendered panel bodies.
   *
   *  Panel bodies are replaced wholesale on every paint, so the listener cannot
   *  be attached once at construction. `data-bound` keeps a paint from stacking
   *  a second handler on a node that survived. */
  function bindPanelControls() {
    for (const button of $$('[data-visibility]', doc)) {
      if (button.dataset.bound === '1') continue;
      button.dataset.bound = '1';
      button.addEventListener('click', () => {
        const next = button.dataset.visibility;
        if (next === visibility) return;
        visibility = next;
        return load();
      });
    }
    for (const button of $$('[data-modules-retry]', doc)) {
      if (button.dataset.bound === '1') continue;
      button.dataset.bound = '1';
      button.addEventListener('click', () => load());
    }
  }

  async function load() {
    const doFetch = resolveFetch();
    if (!doFetch) {
      state = PORTAL_STATES.UNKNOWN;
      portal = null;
      paint();
      return { state, reason: 'no-fetch-available' };
    }
    let response;
    try {
      const query = visibility === 'private' ? '' : `?visibility=${encodeURIComponent(visibility)}`;
      response = await doFetch(`${endpoint}${query}`, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });
    } catch (error) {
      state = PORTAL_STATES.UNKNOWN;
      portal = null;
      paint();
      return { state, reason: 'request-failed' };
    }

    if (STATE_CHANGING_STATUSES.includes(response.status)) {
      state = response.status === 401 ? PORTAL_STATES.SIGNED_OUT : PORTAL_STATES.DENIED;
      portal = null;
      paint();
      return { state, reason: `http-${response.status}` };
    }
    if (!response.ok) {
      // 503 is `*_adapter_not_attached`: the route exists, the module behind it
      // was not wired in this deployment. That is a deployment fact, not an
      // unknown answer — read it the way Calls and the token panel do.
      state = [404, 501, 503].includes(response.status) ? PORTAL_STATES.NOT_CONFIGURED : PORTAL_STATES.UNKNOWN;
      portal = null;
      lastReason = `http-${response.status}`;
      paint();
      return { state, reason: lastReason };
    }

    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      state = PORTAL_STATES.UNKNOWN;
      portal = null;
      paint();
      return { state, reason: 'not-json' };
    }

    const checked = validatePortal(payload?.data?.portal);
    if (!checked.ok) {
      // Nothing is painted from a payload that fails its own contract — a
      // half-shown module list would look authoritative while being wrong.
      state = PORTAL_STATES.UNKNOWN;
      portal = emptyPortal(state, `contract:${checked.errors[0]}`);
      paint();
      return { state, reason: `contract:${checked.errors[0]}`, errors: checked.errors };
    }

    portal = checked.portal;
    lastReason = null;
    state = renderStateFor(portal);
    paint();
    return { state, reason: null, portal };
  }

  return {
    load,
    paint,
    get state() {
      return state;
    },
    get portal() {
      return portal;
    },
    get visibility() {
      return visibility;
    },
  };
}

/**
 * Wire the module panels inside the V5 shell.
 *
 * No self-boot: the panels are part of the shell, so `app.js` owns the lifecycle.
 */
export function initModules({ doc = globalThis.document, endpoint = MODULES_ENDPOINT, fetchImpl } = {}) {
  const controller = createModules({ endpoint, fetchImpl, doc });
  const first = controller.load().catch(() => {
    /* `load` maps every failure into a page state; nothing else to do. */
  });
  return { controller, first };
}
