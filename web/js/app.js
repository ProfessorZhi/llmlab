/*! web/js/app.js — boot.
 *
 *  Order matters and is deliberate:
 *    1. Shell first (router, theme, timeline, dialog, chat), without any
 *       synthetic readings or model inventory in the regular production view.
 *    2. Then resolve the data source: default LIVE-or-UNCONFIGURED. Atlas may
 *       query only a reviewed same-origin BFF path from host config; without
 *       one it renders UNKNOWN, never demo. Only the isolated QA artifact
 *       (web/qa.html, `data-build-profile="qa"`) plus an explicit `?demo=1`
 *       gives an in-process synthetic fixture, visibly labelled as such.
 *    3. Finally build the Atlas with whatever state we ended up with. A source
 *       that is unconfigured or unavailable is a first-class, rendered state —
 *       not an exception and not a zero.
 *
 *  The two authenticated views — the call log and the Portal module catalogue —
 *  are kicked off after the Atlas, and deliberately not awaited: each renders
 *  its own signed-out / denied / unknown state, and a slow or refused backend
 *  must not delay the shell. They run regardless of which route is on screen, so
 *  switching to `calls` or `models` does not start a second fetch.
 *
 *  No credentials are read here. The only host configuration the front-end
 *  understands is an endpoint URL (`globalThis.__LLM_LAB_ATLAS__.endpoint`).
 */
import { initTheme } from './theme.js';
import { createRouter } from './router.js';
import { initTimeline } from './timeline.js';
import { createDialog } from './dialog.js';
import { initChat } from './chat.js';
import { createAtlas } from './atlas/controller.js';
import { initCalls } from './calls/controller.js';
import { initModules } from './modules/controller.js';
import { initOps } from './features/controller.js';
import { mountHostStatus } from './infra/host-status.js';
import { applyDataMode } from './demo-mode.js';
import { mountComputeView } from './compute/view.js';
import { DEMO_WORKLOADS, DEMO_GROUPS } from './data/demo-fixture.js';
import {
  resolveSource, usesDemoFixture, demoState, unknownState, SOURCE_MODES,
} from './data/contract.js';
import { createRemoteClient } from './data/remote-client.js';
import { isQaBuild } from './build-profile.js';
import { mountReadiness } from './workspace/readiness.js';
import { mountTokens } from './workspace/cli-access.js';
import { mountLifecycle } from './workspace/lifecycle.js';
import { mountRoutes } from './workspace/routes.js';

export const VERSION = '6.0.0';

/**
 * Decide the Atlas source state. Exported so the demo/remote rule is exercised
 * in tests without booting a document.
 */
export async function loadSource({ search = '', host = {}, fetchImpl, doc } = {}) {
  const resolved = resolveSource({ search, host, allowDemo: isQaBuild(doc) });
  if (usesDemoFixture(resolved)) {
    const state = demoState(DEMO_WORKLOADS, DEMO_GROUPS);
    // Keep the decision that produced this state, so the UI and its hooks can
    // always answer "why are these numbers on screen?".
    state.reason = resolved.reason;
    return { resolved, state };
  }
  if (resolved.mode === SOURCE_MODES.REMOTE) {
    const client = createRemoteClient({ endpoint: resolved.endpoint, fetchImpl });
    const { state } = await client.load();
    return { resolved, state };
  }
  // No approved same-origin BFF route is configured: show UNKNOWN, not a fixture.
  return { resolved, state: unknownState(resolved.reason) };
}

/** Compact, serialisable view of where the numbers came from. */
export function describeSource(state) {
  return {
    state: state.state,
    synthetic: !!state.synthetic,
    reason: state.reason,
    notes: [...(state.notes ?? [])],
    rows: (state.workloads ?? []).length,
  };
}

async function boot({ doc = document } = {}) {
  // First thing: decide the data mode. Live is the default, so no authored
  // sample number is on screen unless the reader asked for it. The resolved
  // mode is then handed to every layer that owns an authored sample of its own
  // — the overview chart, the chat composer — because a mode decided here and
  // not passed on is a decision those layers cannot honour.
  const mode = applyDataMode({ doc });
  // Add the real Compute view and nav before the hash router binds handlers.
  // The generated V5/V6 visual authority remains untouched.
  const compute = mountComputeView({ doc });
  const router = createRouter({ doc });
  const theme = initTheme({ doc });
  const timeline = initTimeline({ doc, mode });
  const dialog = createDialog({ doc });
  const chat = initChat({ doc, mode });

  const { state: source } = await loadSource({
    search: globalThis.location?.search ?? '',
    host: globalThis.__LLM_LAB_ATLAS__ ?? {},
    doc,
  });

  // Inert markers on <html>: no CSS targets them, so they cannot alter the
  // approved look, but they make the provenance of every pixel testable.
  doc.documentElement.dataset.dataSource = source.state;
  doc.documentElement.dataset.synthetic = String(!!source.synthetic);

  const atlas = createAtlas({ source, doc });

  // Both views read from the authenticated BFF and fail closed on their own; see
  // web/js/calls/controller.js and web/js/modules/controller.js.
  const calls = initCalls({ doc, dialog });
  const modules = initModules({ doc });

  // The ops panels use a reviewed same-origin BFF when configured, otherwise
  // show an honest unavailable state. Only explicit `?demo=1` activates
  // synthetic examples. There is no fallback to fake owners or deployments;
  // its loading remains non-blocking for the shell.
  const ops = initOps({
    doc,
    dialog,
    onStateChange: ({ ops: snapshot, state, mode }) => {
      compute?.update({ ops: snapshot, state, mode });
    },
  });

  // The verified host read-out. It reads exactly one same-origin path and shows
  // real numbers only when the server projected them from the verified
  // Prometheus facts; otherwise the panel says UNKNOWN · NO SOURCE. Like the
  // layers above it is not awaited — a refused or slow source must not hold up
  // the shell.
  const host = mountHostStatus({
    doc,
    // The verified host reading feeds the Compute hero as well: it is the one
    // source allowed to turn those four cells into real numbers.
    onChange: (summary) => compute?.updateHost(summary),
  });

  // The V6.1 workspace layer: runtime-mounted panels built from the shell's own
  // classes, so the generated HTML stays a pure derivation. The API panels go in
  // first so they sit directly under the hero, ahead of the gated V5 blocks.
  const tokens = mountTokens({ doc, demo: mode === 'demo' });
  const lifecycle = mountLifecycle({ doc, dialog });
  const routes = mountRoutes({ doc });
  const readiness = mountReadiness({
    doc,
    read: () => ({
      dataMode: mode,
      host: { state: host?.state, freshness: host?.freshness, reason: host?.reason },
      ops: { mode: ops.controller.mode, state: ops.controller.state, reason: ops.controller.lastReason },
      calls: {
        state: calls.controller.state,
        reason: calls.controller.reason,
        transport: calls.controller.log?.transport?.mode ?? null,
      },
      atlas: { state: source.state, synthetic: !!source.synthetic },
    }),
  });

  globalThis.__WORKSPACE__ = {
    get readiness() { return readiness?.current ?? null; },
    get tokens() { return { state: tokens?.state, count: tokens?.tokens?.length ?? 0, reason: tokens?.reason }; },
    get sdkTab() { return lifecycle?.active ?? null; },
    refresh: () => readiness?.update(),
    version: VERSION,
  };

  globalThis.__ATELIER__ = {
    get page() { return router.page; },
    // The resolved data mode, read back from the document the gate already
    // stamped, so a test can assert live-vs-demo without re-deriving it.
    get dataMode() { return doc.documentElement.dataset.mode; },
    get chartMetric() { return timeline.metric; },
    get theme() { return theme.current; },
    get reducedMotion() { return matchMedia('(prefers-reduced-motion: reduce)').matches; },
    get source() { return describeSource(source); },
    version: VERSION,
  };

  globalThis.__CALLS__ = {
    get state() { return calls.controller.state; },
    get log() { return calls.controller.log; },
    reload: () => calls.controller.load(),
    get reason() { return calls.controller.reason; },
    get lastAction() { return calls.controller.lastAction; },
    openDetail: (id) => calls.controller.openDetail(id),
    exportAs: (kind) => calls.controller.exportAs(kind),
    applyPreset: (p) => calls.controller.applyPreset(p),
    version: VERSION,
  };

  globalThis.__MODULES__ = {
    get state() { return modules.controller.state; },
    get portal() { return modules.controller.portal; },
    get visibility() { return modules.controller.visibility; },
    reload: () => modules.controller.load(),
    version: VERSION,
  };

  // The ops surface's test handle. `mode` is the load-bearing one: a browser
  // test asserts it is `mock` and that no request left the origin.
  globalThis.__OPS__ = {
    get state() { return ops.controller.state; },
    get mode() { return ops.controller.mode; },
    get ops() { return ops.controller.ops; },
    get capabilities() { return ops.controller.capabilities; },
    get visibility() { return ops.controller.visibility; },
    get trendScope() { return ops.controller.trendScope; },
    get writable() { return ops.controller.writable; },
    get lastAction() { return ops.controller.lastAction; },
    openDrawer: (id) => ops.controller.openDrawer(id),
    applyShare: (id, mode, principals) => ops.controller.applyShare(id, mode, principals),
    reload: () => ops.controller.load(),
    version: VERSION,
  };

  // The verified host read-out's test handle. `state` is the load-bearing one:
  // 'live' only when a real projection arrived, 'unknown' for everything else.
  // `freshness` is the client-side clock: a copy that outlived its sample is
  // 'stale' and its numbers are cleared, so `state === 'live'` alone is not
  // enough to call what is on screen current.
  globalThis.__HOST__ = {
    get state() { return host?.state ?? 'unknown'; },
    get freshness() { return host?.freshness ?? 'unknown'; },
    get ageMs() { return host?.ageMs ?? null; },
    get summary() { return host?.summary ?? null; },
    reload: () => host?.refresh(),
    version: VERSION,
  };

  globalThis.__ATLAS__ = {
    get state() { return atlas.snapshot(); },
    get totals() { return atlas.totals(); },
    get rows() { return atlas.ctx.visible(); },
    select: atlas.select,
    setGroup: atlas.setGroup,
    reset: atlas.reset,
    render: atlas.render,
    version: VERSION,
  };

  return { router, theme, timeline, dialog, chat, atlas, calls, modules, ops, source, readiness, tokens, lifecycle, routes };
}

if (globalThis.document?.querySelector('#view-atlas')) {
  boot().catch((err) => {
    // A boot failure must not leave a blank page pretending to have data.
    globalThis.__ATELIER_BOOT_ERROR__ = String(err && err.stack || err);
    // eslint-disable-next-line no-console
    console.error('[llm-lab] front-end boot failed', err);
  });
}
