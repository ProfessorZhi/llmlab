/*! web/js/features/controller.js — the ops layer's one fetch, one paint, one set of handlers.
 *
 *  Where the panels live. `web/index.html` is a **generated** file: it is
 *  produced by `web/tools/extract-from-prototype.mjs` from Atelier 06, which is
 *  itself derived from the frozen 05 design authority. Editing it by hand would
 *  break `npm run verify:extract`, and regenerating it would mean editing the
 *  05 authority — the one thing the design brief forbids. So this layer does
 *  **not** add markup to the document. It mounts its panels at runtime, into the
 *  five views the shell already has, using the shell's own component classes
 *  (`.page-block.module-panel`, `.editorial-index`, `.module-body`). The
 *  generated artefacts stay byte-identical and the new surface still looks like
 *  it was always there.
 *
 *  Where the data comes from. `./source.js` — a configured, reviewed
 *  same-origin BFF in live mode, or an explicit unavailable state. Only
 *  `?demo=1` activates the synthetic fixture; no hidden production fallback.
 *  This module performs no network IO itself.
 *
 *  The page state machine mirrors `../calls/controller.js` and
 *  `../modules/controller.js` exactly, because the server's answers are the same
 *  shape:
 *
 *      200 → ready | every panel renders, each with its own state and reason
 *      401 → signed_out  ("sign in")
 *      403 → denied      ("ask an operator") — never retried automatically
 *      anything else / unparsable / contract violation → unknown
 *
 *  **Every button here does something, or it is disabled and says why.** The
 *  share control issues a real request and re-reads the result; the visibility
 *  and trend-scope switches issue a real reload; the drawer opens for real. The
 *  controls that cannot act in this build — the playground send, the eval
 *  submit, and (in `live` mode) the share writes, whose server path does not
 *  exist yet — are rendered `disabled` with the missing precondition named,
 *  because a button that looks live and does nothing is the exact failure this
 *  whole layer is written to avoid.
 *
 *  The playground send and the eval submit are disabled **twice**: once by the
 *  renderer, which believes the payload, and once by `applyWriteGate`, which
 *  does not. Only the second gate is load-bearing — a payload that started
 *  claiming `authorized: true` would otherwise be enough to produce a dead
 *  button, and paying for that with a server-side promise is not a gate at all.
 */

import { $, $$ } from '../dom.js';
import { EXPLAIN_COPIED, EXPLAIN_COPY_BLOCKED } from '../dialog.js';
import { deriveCapabilities, isGranted } from './capability.js';
import { mountAdminModeControl } from './admin-mode.js';
import { OPS_STATES, emptyOps } from './contract.js';
import { renderEventsSection } from './events.js';
import {
  OPS_PANEL_IDS,
  renderDeploymentDrawer,
  renderNotice,
  renderOpsStrip,
  renderPanels,
} from './render.js';
import { SOURCE_MODES, adminModeCsrf, createFeatureSource, resolveFeatureSource } from './source.js';
import { isQaBuild } from '../build-profile.js';

/** Source modes whose write path has been verified end-to-end. Only the fixture
 *  qualifies: no live BFF exists yet, so a live write would be a request against
 *  a route nobody implemented. Disabling it is the fail-closed answer, and the
 *  reason says so rather than pretending the control works. */
export const WRITES_VERIFIED_MODES = Object.freeze([SOURCE_MODES.MOCK]);

/**
 * The write routes this front-end can actually call: same-origin, CSRF-bound,
 * with cancellation, a timeout and a response contract it has been tested
 * against. The list is empty because no such route exists yet.
 *
 * This is the difference between a gate and a coat of paint. `data-playground-send`
 * and `data-eval-submit` are disabled today only because the payload says the
 * backend is not ready — so a server that started reporting `authorized: true`
 * would silently turn both into enabled buttons wired to nothing, which is
 * precisely the failure Issue #44 was filed about. The gate below reads this
 * list instead of the payload, so enabling a control requires editing an
 * explicit constant *and* shipping the transport that honours it.
 */
export const VERIFIED_WRITE_CONTRACTS = Object.freeze([]);

/** Write controls, and the contract each one needs before it may be enabled. */
export const WRITE_CONTROL_CONTRACTS = Object.freeze({
  '[data-playground-send]': 'playground.submit',
  '[data-eval-submit]': 'evaluation.submit',
});

/** The reason stamped on a control whose contract is missing. */
export const NO_WRITE_CONTRACT_REASON =
  '本构建没有已验证的同源写契约（CSRF、取消/超时、结果回执）；后端就绪标记不会自动启用它。';

/** The panels this layer mounts, and the view each one belongs to.
 *  `index` continues the shell's own editorial numbering (the last shipped
 *  panel is 10), and every `id` must appear in `render.renderPanels`. */
export const PANELS = Object.freeze([
  { id: 'registry', view: 'models', index: '11', title: '模型与引擎 · 我拥有 / 共享给我', en: 'REGISTRY & SHARE SCOPE' },
  { id: 'keys', view: 'api', index: '12', title: '个人 API Key', en: 'SCOPED CREDENTIALS' },
  { id: 'playground', view: 'api', index: '13', title: '受限试跑台与请求示例', en: 'PLAYGROUND · GATED' },
  { id: 'observability', view: 'observability', index: '14', title: '我的 / 全局指标与趋势', en: 'TELEMETRY · MY / GLOBAL' },
  { id: 'evaluation', view: 'evaluation', index: '15', title: '评测队列与结果', en: 'EVALS · QUEUE & RESULTS' },
  { id: 'access', view: 'admin', index: '16', title: '系统管理 · 按 scope 判定', en: 'ADMINISTRATION · SCOPES ONLY' },
]);

/** The two lists must agree; a mismatch is a blank rectangle on screen. Kept as
 *  a module-level check so it fails loudly in development rather than quietly in
 *  production, and re-asserted in the test suite. */
export const PANEL_IDS_MATCH_RENDER = PANELS.every((panel) => OPS_PANEL_IDS.includes(panel.id))
  && OPS_PANEL_IDS.length === PANELS.length;

/** Mount the ops panels into the shell's views. Idempotent: a second call finds
 *  the hosts it already made instead of stacking a duplicate set. */
export function mountPanels({ doc = globalThis.document } = {}) {
  const hosts = new Map();
  for (const panel of PANELS) {
    const existing = $(`[data-ops="${panel.id}"]`, doc);
    if (existing) {
      hosts.set(panel.id, existing);
      continue;
    }
    const view = $(`#view-${panel.view}`, doc);
    if (!view) continue; // a shell without this view renders no panel for it
    const section = doc.createElement('section');
    section.className = 'page-block module-panel';
    section.dataset.ops = panel.id;
    section.innerHTML =
      `<div class="editorial-index"><b>${panel.index}</b><span>${panel.en}</span>` +
      `<span class="index-rule"></span>` +
      `<span class="mono" data-ops-state="${panel.id}">等待会话</span></div>` +
      `<div class="group-title"><h2 class="section-h">${panel.title}</h2>` +
      `<span class="smallmeta">FAIL-CLOSED · 无来源即 UNKNOWN</span></div>` +
      `<div class="module-body" data-ops-body="${panel.id}"></div>`;
    view.appendChild(section);
    hosts.set(panel.id, section);
  }
  return hosts;
}

/**
 * @param {object} opts
 * @param {Document} [opts.doc]
 * @param {object} [opts.source]  a source from `createFeatureSource`; resolved
 *        from the URL when omitted.
 * @param {{explain: Function}} [opts.dialog]  the shell dialog, used for the
 *        detail drawer so the page grows no second modal.
 */
export function createOps({ doc = globalThis.document, source, dialog = null, onStateChange = null } = {}) {
  const resolved = resolveFeatureSource({
    search: globalThis.location?.search ?? '',
    host: globalThis.__LLM_LAB_OPS__ ?? {},
    allowDemo: isQaBuild(doc),
  });
  const opsSource = source ?? createFeatureSource({
    mode: resolved.mode,
    base: resolved.base ?? '',
    persona: resolved.persona,
  });

  const hosts = mountPanels({ doc });
  const bodies = new Map();
  for (const [id, host] of hosts) {
    bodies.set(id, $(`[data-ops-body="${id}"]`, doc) ?? host);
  }

  let ops = null;
  let state = OPS_STATES.UNKNOWN;
  let visibility = 'private';
  let trendScope = 'mine';
  let lastError = null;
  let lastAction = null;
  // The reason the *last load* gave, kept separately from `ops.reason`: a page
  // state that carries no payload has nowhere else to record why, and a notice
  // without its reason is a dead end for whoever has to fix it.
  let lastReason = null;
  let capabilities = deriveCapabilities({ scopes: [] });
  let loadGeneration = 0;
  let pending = false;
  // The operator audit timeline is a second, independent read. It is never
  // derived from the ops payload, and it is cleared whenever the session scope
  // changes — an elevated read must not outlive the elevation.
  let events = { state: 'unknown', dto: null, reason: null, pending: false };
  let eventsGeneration = 0;

  const writable = WRITES_VERIFIED_MODES.includes(opsSource.mode);

  // Notify passive read-only projections (e.g. Compute) whenever the effective
  // session scope is reloaded or invalidated. They do not own any credentials.
  const notify = () => {
    if (typeof onStateChange !== 'function') return;
    try { onStateChange({ ops, state, mode: opsSource.mode }); } catch {
      // A secondary view may never block the primary scoped panel repaint.
    }
  };

  function setBody(id, html) {
    const body = bodies.get(id);
    if (body) body.innerHTML = html;
  }

  function paint() {
    for (const panel of PANELS) {
      const stateEl = $(`[data-ops-state="${panel.id}"]`, doc);
      if (stateEl) stateEl.textContent = ops ? stateLabel() : '等待会话';
    }

    if (!ops) {
      // A first load with nothing on screen yet says "reading", not "no
      // answer": the page has asked, it just has not been told. Only when there
      // is no payload is the loading notice used, so a reload keeps the panels
      // that already have data instead of blanking them mid-flight.
      const notice = pending
        ? renderNotice(OPS_STATES.LOADING, null)
        : renderNotice(state, lastReason);
      for (const panel of PANELS) {
        // The audit section is independent of the ops payload, and "this
        // deployment has no audit route" must stay visible even when the ops
        // read itself failed — otherwise the honest NotConfigured/UNKNOWN is
        // buried under one generic notice.
        setBody(panel.id, notice + (panel.id === 'access' ? renderEventsSection(events) : ''));
      }
      // The notice carries a retry control; binding it here — not only on the
      // success path — is what keeps it from being a button that does nothing.
      bindControls();
      notify();
      return;
    }

    const strip = renderOpsStrip(ops);
    const panels = renderPanels(ops, capabilities, {
      visibility,
      trendScope,
      writable,
      error: lastError,
      events,
    });
    for (const [id, html] of Object.entries(panels)) setBody(id, strip + html);

    applyWriteGate();
    bindControls();
    notify();
  }

  /** Force every write control without a verified contract back to disabled.
   *  Runs on every paint, after the renderer has had its say, so the payload
   *  can never be the only thing standing between a reader and a dead button. */
  function applyWriteGate() {
    for (const [selector, contract] of Object.entries(WRITE_CONTROL_CONTRACTS)) {
      if (VERIFIED_WRITE_CONTRACTS.includes(contract)) continue;
      for (const node of $$(selector, doc)) {
        node.disabled = true;
        node.setAttribute('aria-disabled', 'true');
        node.title = NO_WRITE_CONTRACT_REASON;
        node.dataset.writeGate = contract;
      }
    }
  }

  /** Refuse a write whose transport does not exist. Bound to both controls even
   *  though the gate disables them: a control that is one CSS regression away
   *  from being clickable must still answer with a reason, not with silence. */
  function rejectUnverifiedWrite(contract) {
    lastAction = { kind: 'write-rejected', contract };
    lastError = `no-verified-write-contract:${contract}`;
    paint();
    return { ok: false, reason: lastError };
  }

  function stateLabel() {
    if (!ops) return '等待会话';
    return `就绪 · 可见 ${visibility === 'private' ? 'PRIVATE' : 'SHARED'}`;
  }

  /** Panel bodies are replaced wholesale on every paint, so handlers are bound
   *  to the freshly rendered nodes each time. `data-bound` keeps one paint from
   *  stacking a second listener on a node that survived. */
  function bindControls() {
    /** Bind once per node. `data-bound` keeps one paint from stacking a second
     *  listener on a node that survived the previous one. */
    const bindAll = (selector, handler) => {
      for (const node of $$(selector, doc)) {
        if (node.dataset.bound === '1') continue;
        node.dataset.bound = '1';
        node.addEventListener('click', handler);
      }
    };

    bindAll('[data-visibility]', (event) => {
      const next = event.currentTarget.dataset.visibility;
      if (next === visibility) return undefined;
      visibility = next;
      return load();
    });

    bindAll('[data-trend-scope]', (event) => {
      const next = event.currentTarget.dataset.trendScope;
      if (next === trendScope) return undefined;
      trendScope = next;
      paint();
      return undefined;
    });

    bindAll('[data-share-mode]', (event) => {
      const { deployment: deploymentId, shareMode: mode } = event.currentTarget.dataset;
      const input = $(`[data-share-principals="${deploymentId}"]`, doc);
      const principals = (input?.value ?? '')
        .split(',').map((value) => value.trim()).filter(Boolean);
      return applyShare(deploymentId, mode, principals);
    });

    bindAll('[data-drawer="deployment"]', (event) => openDrawer(event.currentTarget.dataset.deployment));

    bindAll('[data-ops-retry]', () => load());

    // The two write controls have a handler before they have a transport: the
    // handler says which contract is missing. Nothing here is allowed to be a
    // button that looks live and does nothing.
    for (const [selector, contract] of Object.entries(WRITE_CONTROL_CONTRACTS)) {
      bindAll(selector, () => rejectUnverifiedWrite(contract));
    }

    // The request example is the one control that touches the clipboard. It
    // copies a placeholder, never a credential, and it says so either way —
    // reusing the shell's own dialog copy so there is one wording, not two.
    bindAll('[data-copy-example]', async (event) => {
      lastAction = { kind: 'copy-example' };
      const panel = event.currentTarget.closest('.flatpanel');
      const text = panel?.querySelector('.codearea')?.textContent ?? '';
      try {
        const clipboard = globalThis.navigator?.clipboard;
        if (!clipboard?.writeText) throw new Error('clipboard unavailable');
        await clipboard.writeText(text);
        dialog?.explain?.(EXPLAIN_COPIED);
      } catch {
        dialog?.explain?.(EXPLAIN_COPY_BLOCKED);
      }
    });
  }

  /** Open the detail drawer in the shell's existing `<dialog>`. Falls back to a
   *  no-op when no dialog was injected, so the function is safe to call from a
   *  test that only cares about state. */
  function openDrawer(deploymentId) {
    lastAction = { kind: 'drawer', deployment_id: deploymentId };
    const deployment = (ops?.registry?.owned ?? [])
      .find((dep) => dep.deployment_id === deploymentId);
    if (!deployment || !dialog?.explain) return { ok: false, reason: 'no-drawer-host' };
    dialog.explain(
      `模型详情 / 登记管理 · ${deployment.model_id}`,
      renderDeploymentDrawer(deployment, capabilities, { writable }),
    );
    return { ok: true };
  }

  /** Apply a share change through the source, then re-read. The re-read is the
   *  point: the UI shows the server's answer, not an optimistic local guess. */
  async function applyShare(deploymentId, mode, principals) {
    lastAction = { kind: 'share', deployment_id: deploymentId, mode, principals };
    if (!writable) {
      lastError = 'live-write-unverified';
      paint();
      return { ok: false, reason: 'live-write-unverified' };
    }
    const result = await opsSource.updateShare(deploymentId, { mode, principals });
    if (result.ok) {
      lastError = null;
      await load();
    } else {
      lastError = result.reason ?? 'share-failed';
      paint();
    }
    return result;
  }

  async function load() {
    const generation = ++loadGeneration;
    pending = true;
    // Paint the loading state *before* awaiting, so the reader is told the page
    // has asked. `paint` only draws it when there is no payload to keep.
    if (!ops) paint();
    const answer = await opsSource.load();
    // Scope changes invalidate every in-flight read. Older responses must not
    // repaint the page with a snapshot fetched under a previous session role.
    if (generation !== loadGeneration) {
      return { state: OPS_STATES.UNKNOWN, reason: 'superseded', errors: [] };
    }
    pending = false;
    state = answer.state;
    lastReason = answer.reason ?? null;
    ops = answer.ops ?? null;

    if (ops && state === OPS_STATES.READY) {
      capabilities = deriveCapabilities({ scopes: ops.scope?.scopes ?? [] });
    } else {
      capabilities = deriveCapabilities({ scopes: [] });
    }
    // A page state that carries no data is painted as a notice, and the payload
    // is dropped so no stale panel survives a failure.
    if (state !== OPS_STATES.READY) ops = null;

    paint();
    // The audit timeline is read only after the session's scopes are known. A
    // failed ops read grants nothing, so it must not be mistaken for "asked and
    // denied" — that would claim a decision the server never made.
    if (state === OPS_STATES.READY) {
      await loadEvents();
    } else {
      events = { state: 'unknown', dto: null, reason: 'session-unknown', pending: false };
      paint();
    }
    return { state, reason: answer.reason ?? null, errors: answer.errors ?? [] };
  }

  /**
   * Read the operator audit timeline.
   *
   * Only a session whose server-asserted scopes include `portal:admin:read`
   * asks at all. An ordinary member gets a denial with **no request and no
   * rows** — not a filtered view of someone else's activity.
   */
  async function loadEvents() {
    const generation = ++eventsGeneration;
    if (!isGranted(capabilities, 'viewAllPrincipals')) {
      events = { state: 'denied', dto: null, reason: 'missing-scope:portal:admin:read', pending: false };
      paint();
      return events;
    }
    events = { ...events, pending: true };
    paint();
    const answer = await opsSource.loadEvents();
    // A scope change (elevation expiry, logout, revoke) bumps the generation;
    // the answer that was already on the wire must not repaint the panel.
    if (generation !== eventsGeneration) {
      return { state: 'superseded', dto: null, reason: 'superseded', pending: false };
    }
    events = {
      state: answer.state,
      dto: answer.dto ?? null,
      reason: answer.reason ?? null,
      pending: false,
    };
    paint();
    return events;
  }

  function invalidate() {
    ++loadGeneration;
    ++eventsGeneration; // drop any in-flight audit read as well
    pending = false;
    ops = null;
    state = OPS_STATES.UNKNOWN;
    lastReason = 'session-scope-changed';
    lastError = null;
    capabilities = deriveCapabilities({ scopes: [] });
    events = { state: 'unknown', dto: null, reason: 'session-scope-changed', pending: false };
    paint();
  }

  return {
    load,
    invalidate,
    paint,
    openDrawer,
    applyShare,
    rejectUnverifiedWrite,
    applyWriteGate,
    panels: PANELS.map((panel) => panel.id),
    get state() { return state; },
    get ops() { return ops; },
    get capabilities() { return capabilities; },
    get visibility() { return visibility; },
    get trendScope() { return trendScope; },
    get lastError() { return lastError; },
    get lastAction() { return lastAction; },
    get lastReason() { return lastReason; },
    get pending() { return pending; },
    get events() { return events; },
    loadEvents,
    invalidateEvents: () => {
      ++eventsGeneration;
      events = { state: 'unknown', dto: null, reason: 'session-scope-changed', pending: false };
      paint();
    },
    get mode() { return opsSource.mode; },
    get writable() { return writable; },
    get source() { return opsSource; },
  };
}

/** Wire the ops layer into the shell. No self-boot: `app.js` owns the lifecycle,
 *  and the load is deliberately not awaited, so a slow or refused source cannot
 *  delay the shell's first paint. */
/** Mount the isolated admin-mode control into the V6 admin view.
 * The generated HTML stays unchanged. Each write gets a new CSRF token bound
 * to the current server-side cookie, never a synthetic preview token. */
export function attachAdminMode({
  doc = globalThis.document,
  controller,
  transport = null,
  search = globalThis.location?.search ?? '',
  host = globalThis.__LLM_LAB_OPS__ ?? {},
} = {}) {
  if (!doc || !controller) return null;
  const root = $('#view-admin', doc);
  if (!root) return null;

  const resolved = resolveFeatureSource({ search, host, allowDemo: isQaBuild(doc) });
  const wire = transport ?? { mode: controller.mode, base: resolved.base ?? '' };
  let previousElevation = null;

  return mountAdminModeControl({
    doc,
    root,
    transport: wire,
    getCsrfToken: async () => {
      const token = await adminModeCsrf(wire);
      return token.ok ? token.csrfToken : null;
    },
    onChange: (view) => {
      const elevated = view.state === 'elevated';
      if (previousElevation !== elevated) {
        const changed = previousElevation !== null || elevated;
        previousElevation = elevated;
        if (changed) {
          // Drop privileged data/capabilities immediately before async reload.
          controller.invalidate();
          controller.load().catch(() => {});
        }
      }
    },
  });
}

/** Wire ops into the shell; mock mode cannot create a privileged session. */
export function initOps(options = {}) {
  const controller = createOps(options);
  const first = controller.load().catch(() => {
    /* `load` maps every failure into a page state; nothing else to do. */
  });
  const adminControl = attachAdminMode({
    doc: options.doc ?? globalThis.document,
    controller,
    transport: options.adminTransport ?? null,
  });
  return { controller, first, adminControl };
}

export { emptyOps };
