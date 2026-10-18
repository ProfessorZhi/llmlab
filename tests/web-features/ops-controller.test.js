/*! tests/web-features/ops-controller.test.js — the controller's own logic.
 *
 *  `createOps` is the one piece of this layer that touches a document, and the
 *  browser suite that would otherwise cover it **cannot run on a host without
 *  Chrome**. So the parts that do not actually need a DOM are exercised here,
 *  against a deliberately empty document stub: the panel mount finds no views
 *  and mounts nothing, which is itself a supported state ("a shell without this
 *  view renders no panel for it"), and everything downstream — the source, the
 *  state machine, the capability derivation, the write path, the drawer — runs
 *  for real.
 *
 *  What this file is for, concretely: proving that a write against a source that
 *  has not been verified **never reaches the wire**, and that the drawer refuses
 *  to open when the shell gave it no host, rather than throwing.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PANELS,
  PANEL_IDS_MATCH_RENDER,
  VERIFIED_WRITE_CONTRACTS,
  WRITE_CONTROL_CONTRACTS,
  WRITES_VERIFIED_MODES,
  createOps,
} from '../../web/js/features/controller.js';
import { createFeatureSource, SOURCE_MODES } from '../../web/js/features/source.js';
import { OPS_PANEL_IDS } from '../../web/js/features/render.js';

/** A document that contains none of the shell's views. Enough for everything
 *  except actually painting, which the browser suite covers. */
const emptyDoc = () => ({
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => {
    throw new Error('nothing should be created against an empty document');
  },
});

const explodingFetch = async () => {
  throw new Error('the network was used where it must not be');
};

const mockSource = (persona = 'sharer') => createFeatureSource({
  mode: SOURCE_MODES.MOCK,
  persona,
  fetchImpl: explodingFetch,
});

// --- wiring -----------------------------------------------------------------

test('the mounted panels and the rendered panels agree', () => {
  assert.equal(PANEL_IDS_MATCH_RENDER, true);
  assert.deepEqual([...OPS_PANEL_IDS].sort(), PANELS.map((panel) => panel.id).sort());
  assert.equal(new Set(PANELS.map((panel) => panel.id)).size, PANELS.length);
});

test('every panel names a view the shell actually has', () => {
  const views = ['models', 'api', 'observability', 'evaluation', 'admin'];
  for (const panel of PANELS) {
    assert.ok(views.includes(panel.view), `${panel.id} targets unknown view ${panel.view}`);
    assert.match(panel.index, /^\d{2}$/, `${panel.id} has a loose editorial index`);
  }
});

test('only verified source modes may write', () => {
  assert.deepEqual([...WRITES_VERIFIED_MODES], [SOURCE_MODES.MOCK]);
  assert.equal(WRITES_VERIFIED_MODES.includes(SOURCE_MODES.LIVE), false);
  assert.equal(WRITES_VERIFIED_MODES.includes(SOURCE_MODES.UNAVAILABLE), false);
});

// --- state ------------------------------------------------------------------

test('a mock controller reaches ready and derives capabilities from the payload', async () => {
  const controller = createOps({ doc: emptyDoc(), source: mockSource('sharer') });
  const result = await controller.load();

  assert.equal(result.state, 'ready');
  assert.equal(controller.mode, 'mock');
  assert.equal(controller.writable, true);
  assert.equal(controller.capabilities.shareEndpoints.granted, true);
  assert.equal(controller.capabilities.viewAllPrincipals.granted, false);
  assert.equal(controller.ops.registry.owned.length, 2);
});

test('a live controller is not writable, because no live write path is verified', async () => {
  const controller = createOps({
    doc: emptyDoc(),
    source: createFeatureSource({ mode: SOURCE_MODES.LIVE, base: '/gw', fetchImpl: explodingFetch }),
  });
  await controller.load();
  assert.equal(controller.writable, false);
  assert.equal(controller.state, 'unknown'); // the exploding fetch was never reached
});

test('a failed load drops the payload, so no stale panel can survive it', async () => {
  const source = mockSource('sharer');
  const controller = createOps({ doc: emptyDoc(), source });
  await controller.load();
  assert.ok(controller.ops, 'sanity: the first load succeeded');

  // Point the same source at a path the fixture does not answer.
  const broken = createFeatureSource({ mode: SOURCE_MODES.UNAVAILABLE, fetchImpl: explodingFetch });
  const second = createOps({ doc: emptyDoc(), source: broken });
  await second.load();
  assert.equal(second.ops, null);
  assert.equal(second.state, 'unknown');
  for (const capability of Object.values(second.capabilities)) {
    assert.equal(capability.granted, false, 'a failed load must leave no capabilities granted');
  }
});

test('a session scope change discards privileged data and supersedes in-flight loads', async () => {
  let completeRead;
  const source = {
    mode: SOURCE_MODES.LIVE,
    load: () => new Promise((resolve) => { completeRead = resolve; }),
  };
  const controller = createOps({ doc: emptyDoc(), source });
  const first = controller.load();
  controller.invalidate();
  assert.equal(controller.ops, null);
  for (const capability of Object.values(controller.capabilities)) {
    assert.equal(capability.granted, false, 'invalidated view still grants privileges');
  }

  completeRead({ state: 'ready', ops: {
    scope: { scopes: ['portal:admin:read'] },
  } });
  const outcome = await first;
  assert.equal(outcome.reason, 'superseded');
  assert.equal(controller.ops, null, 'stale privileged response repainted the view');
  assert.equal(controller.state, 'unknown');
});

test("an in-flight read cannot write one account's rows into another's view", async () => {
  // Switching accounts is asynchronous in both directions: the previous reader's
  // request is already on the wire when the session changes, and its reply lands
  // afterwards. A payload fetched under one Linux UID must never be cached under
  // the next — the marker below is deliberately one no fixture can contain.
  const STALE = 'STALE-UID-A-ROW';
  let completeA;
  const sourceA = {
    mode: SOURCE_MODES.LIVE,
    load: () => new Promise((resolve) => { completeA = resolve; }),
  };
  const controller = createOps({ doc: emptyDoc(), source: sourceA });
  const inFlight = controller.load();

  controller.invalidate(); // the session moves from UID A to UID B

  completeA({ state: 'ready', ops: { scope: { scopes: ['portal:admin:read'] }, rows: [{ key_id: STALE }] } });
  assert.equal((await inFlight).reason, 'superseded', "UID A's read must report as superseded");
  assert.equal(controller.ops, null, "UID A's rows must not be cached under UID B");
  assert.doesNotMatch(JSON.stringify(controller.ops ?? {}), new RegExp(STALE),
    'a superseded payload must leave no trace in the view it no longer belongs to');

  // The session that owns the view then loads its own rows — and only its own.
  const controllerB = createOps({ doc: emptyDoc(), source: mockSource('user') });
  await controllerB.load();
  assert.ok(controllerB.ops, 'sanity: UID B loads its own payload');
  assert.doesNotMatch(JSON.stringify(controllerB.ops), new RegExp(STALE),
    "another account's rows must never appear in this one's view");
});

// --- the write path ---------------------------------------------------------

test('a share change on a mock source writes, reloads, and is recorded', async () => {
  const controller = createOps({ doc: emptyDoc(), source: mockSource('sharer') });
  await controller.load();

  const result = await controller.applyShare('dep-demo-1', 'selected', ['demo-user-b']);
  assert.equal(result.ok, true);
  assert.equal(controller.lastError, null);
  assert.deepEqual(controller.lastAction, {
    kind: 'share', deployment_id: 'dep-demo-1', mode: 'selected', principals: ['demo-user-b'],
  });
  // The re-read is the point: controller state is the fixture's answer.
  assert.equal(controller.ops.registry.owned[0].share.mode, 'selected');
  assert.deepEqual(controller.ops.registry.owned[0].share.principals, ['demo-user-b']);
});

test('a refused share change leaves the previous state on screen and names the error', async () => {
  const controller = createOps({ doc: emptyDoc(), source: mockSource('sharer') });
  await controller.load();

  const result = await controller.applyShare('dep-demo-1', 'selected', []);
  assert.equal(result.ok, false);
  assert.equal(result.status, 400);
  assert.equal(controller.lastError, 'invalid_request');
  assert.equal(controller.ops.registry.owned[0].share.mode, 'private',
    'a refused write must not optimistically change the view');
});

test('a share change against an unverified write path never reaches the wire', async () => {
  const controller = createOps({
    doc: emptyDoc(),
    source: createFeatureSource({ mode: SOURCE_MODES.LIVE, base: '/gw', fetchImpl: explodingFetch }),
  });
  await controller.load();

  const result = await controller.applyShare('dep-demo-1', 'all', []);
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'live-write-unverified');
  assert.equal(controller.lastError, 'live-write-unverified');
});

test('a session without the share scope is refused by the source, not only by the button', async () => {
  const controller = createOps({ doc: emptyDoc(), source: mockSource('user') });
  await controller.load();
  assert.equal(controller.capabilities.shareEndpoints.granted, false);

  const result = await controller.applyShare('dep-demo-1', 'all', []);
  assert.equal(result.ok, false);
  assert.equal(result.status, 403);
});

// --- the drawer -------------------------------------------------------------

test('the drawer reports that it had no host rather than throwing', async () => {
  const controller = createOps({ doc: emptyDoc(), source: mockSource('sharer'), dialog: null });
  await controller.load();
  const result = controller.openDrawer('dep-demo-1');
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'no-drawer-host');
  assert.equal(controller.lastAction.kind, 'drawer');
});

test('the drawer renders the record through the shell dialog', async () => {
  const opened = [];
  const controller = createOps({
    doc: emptyDoc(),
    source: mockSource('sharer'),
    dialog: { explain: (title, html) => opened.push({ title, html }) },
  });
  await controller.load();

  assert.equal(controller.openDrawer('dep-demo-1').ok, true);
  assert.equal(opened.length, 1);
  assert.match(opened[0].title, /demo-llm-a/);
  assert.match(opened[0].html, /dep-demo-1/);
  assert.match(opened[0].html, /所有者 UID/);
  // The drawer never offers a live write for something it cannot write.
  assert.match(opened[0].html, /backend\/registry/);
});

test('the drawer for an unknown deployment opens nothing', async () => {
  let calls = 0;
  const controller = createOps({
    doc: emptyDoc(),
    source: mockSource('sharer'),
    dialog: { explain: () => { calls += 1; } },
  });
  await controller.load();
  const result = controller.openDrawer('dep-demo-does-not-exist');
  assert.equal(result.ok, false);
  assert.equal(calls, 0);
});

// --- painting, against a document stand-in ----------------------------------
//
// Not a browser. Just enough DOM to make `mountPanels` and `paint` run so the
// two things a browser would otherwise be the only witness to are checkable
// here: that a page state with no payload still states *why*, and that its
// retry control is actually bound (an unbound button is exactly the failure
// this layer is written to avoid).

function recordingDoc() {
  const bound = [];
  const views = new Map();
  for (const view of ['models', 'api', 'observability', 'evaluation', 'admin']) {
    views.set(`#view-${view}`, {
      children: [],
      appendChild(node) { this.children.push(node); return node; },
    });
  }
  const created = [];
  const nodes = new Map();
  return {
    bound,
    created,
    views,
    nodes,
    documentElement: { dataset: {} },
    querySelector(selector) {
      if (selector.startsWith('[data-ops=')) return null; // nothing mounted yet
      if (selector.startsWith('#view-')) return views.get(selector) ?? null;
      return null; // data-ops-body / data-ops-state fall back to the host
    },
    querySelectorAll(selector) {
      // One node per selector, handed out on every call. It has to be the same
      // object each time: `applyWriteGate` runs on every paint and is read back
      // afterwards, so a double that minted a fresh element per call would let
      // the gate "pass" while leaving the element the reader sees untouched.
      //
      // The properties below are the ones the controller actually touches —
      // disabling a control sets `disabled`, `title` and `aria-disabled` — so a
      // double without them would report a crash where production sets a field.
      if (!nodes.has(selector)) {
        nodes.set(selector, {
          dataset: {},
          disabled: false,
          title: '',
          attributes: {},
          setAttribute(name, value) { this.attributes[name] = String(value); },
          addEventListener(type, handler) { bound.push({ selector, type, handler }); },
        });
      }
      return [nodes.get(selector)];
    },
    createElement() {
      const node = {
        dataset: {},
        className: '',
        innerHTML: '',
        children: [],
        appendChild(child) { this.children.push(child); return child; },
      };
      created.push(node);
      return node;
    },
  };
}

const panelHtml = (doc, panelId) => {
  for (const view of doc.views.values()) {
    for (const section of view.children) {
      if (section.dataset.ops === panelId) return section.innerHTML;
    }
  }
  return null;
};

test('the panels are mounted into their views and painted', async () => {
  const doc = recordingDoc();
  const controller = createOps({ doc, source: mockSource('sharer') });
  await controller.load();

  const expected = {
    registry: [/我拥有的部署/],
    keys: [/明文只在签发那一刻/],
    playground: [/受限试跑台/],
    observability: [/指标卡/],
    evaluation: [/评测证据与运行记录/],
    access: [/本次会话的可见范围/],
  };
  const viewsOf = {
    registry: 'models', keys: 'api', playground: 'api',
    observability: 'observability', evaluation: 'evaluation', access: 'admin',
  };
  for (const [id, view] of Object.entries(viewsOf)) {
    const section = doc.views.get(`#view-${view}`).children.find((node) => node.dataset.ops === id);
    assert.ok(section, `${id} was not mounted into #view-${view}`);
    assert.equal(section.className, 'page-block module-panel');
    for (const pattern of expected[id]) {
      assert.match(section.innerHTML, pattern, `${id} did not paint its own content`);
    }
  }
  assert.match(panelHtml(doc, 'registry'), /SYNTHETIC/);
});

test('a first load says it is reading, not that there is no answer', async () => {
  // Regression: with no payload the panel painted the *unknown* notice, which
  // says "没有取到可用的管理数据" — a failure report for a request that was still
  // in flight.
  const doc = recordingDoc();
  const inner = mockSource('sharer');
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const controller = createOps({
    doc,
    source: { mode: SOURCE_MODES.MOCK, load: async () => { await gate; return inner.load(); } },
  });

  const loading = controller.load();
  assert.equal(controller.pending, true);
  assert.match(panelHtml(doc, 'registry'), /正在读取/);
  assert.doesNotMatch(panelHtml(doc, 'registry'), /暂时无法确定/);

  release();
  await loading;
  assert.equal(controller.pending, false);
  assert.match(panelHtml(doc, 'registry'), /我拥有的部署/);
});

test('a reload keeps the panels it already has instead of blanking them', async () => {
  const doc = recordingDoc();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const inner = mockSource('sharer');
  let first = true;
  const controller = createOps({
    doc,
    source: {
      mode: SOURCE_MODES.MOCK,
      load: async () => {
        if (!first) await gate;
        first = false;
        return inner.load();
      },
    },
  });
  await controller.load();
  assert.match(panelHtml(doc, 'registry'), /我拥有的部署/);

  const reload = controller.load();
  assert.equal(controller.pending, true);
  assert.match(panelHtml(doc, 'registry'), /我拥有的部署/,
    'a panel with data must not be replaced by a loading notice mid-flight');
  release();
  await reload;
});

test('a failed load paints the reason, not just the state', async () => {
  // Regression: the notice used to be rendered from `ops?.reason`, which is
  // null exactly when the notice is shown — so the reason was never displayed.
  const doc = recordingDoc();
  const controller = createOps({
    doc,
    source: createFeatureSource({ mode: SOURCE_MODES.LIVE, base: '/gw', fetchImpl: explodingFetch }),
  });
  await controller.load();

  assert.equal(controller.state, 'unknown');
  assert.equal(controller.lastReason, 'request-failed');
  const notice = panelHtml(doc, 'registry');
  assert.match(notice, /暂时无法确定/);
  assert.match(notice, /request-failed/, 'the notice must say why it has no data');
});

test('the retry control is bound even when there is no payload to paint', async () => {
  // Regression: `bindControls` used to run only on the success path, so the
  // retry button in the failure notice was rendered live and did nothing.
  const doc = recordingDoc();
  const controller = createOps({
    doc,
    source: createFeatureSource({ mode: SOURCE_MODES.UNAVAILABLE, fetchImpl: explodingFetch }),
  });
  await controller.load();

  assert.equal(panelHtml(doc, 'registry')?.includes('data-ops-retry'), true,
    'the notice should offer a retry');
  assert.ok(
    doc.bound.some((entry) => entry.selector === '[data-ops-retry]' && entry.type === 'click'),
    'the retry control was rendered but never bound',
  );
});

test('a successful paint binds every control the panels render', async () => {
  const doc = recordingDoc();
  const controller = createOps({ doc, source: mockSource('sharer') });
  await controller.load();

  const selectors = new Set(doc.bound.filter((entry) => entry.type === 'click').map((e) => e.selector));
  for (const selector of ['[data-visibility]', '[data-trend-scope]', '[data-share-mode]',
    '[data-drawer="deployment"]', '[data-copy-example]']) {
    assert.ok(selectors.has(selector), `${selector} was never bound`);
  }
});

// --- the write gate: a payload cannot enable a dead button -------------------

test('no write contract is verified in this build', () => {
  assert.deepEqual([...VERIFIED_WRITE_CONTRACTS], []);
  assert.deepEqual(Object.keys(WRITE_CONTROL_CONTRACTS).sort(),
    ['[data-eval-submit]', '[data-playground-send]']);
});

test('a payload that claims readiness still cannot enable a write control', async () => {
  // The renderer believes the payload; `applyWriteGate` does not. This pins the
  // half that matters, by hand-enabling the control the way a *ready* payload
  // would and then re-running the gate.
  const doc = recordingDoc();
  const controller = createOps({ doc, source: mockSource('sharer') });
  await controller.load();

  const send = doc.nodes.get('[data-playground-send]');
  const submit = doc.nodes.get('[data-eval-submit]');
  assert.ok(send && submit, 'the panels should render both write controls');

  for (const node of [send, submit]) node.disabled = false; // "the backend says it is ready"
  controller.applyWriteGate();

  for (const node of [send, submit]) {
    assert.equal(node.disabled, true, 'the gate must re-disable it');
    assert.equal(node.attributes['aria-disabled'], 'true');
    assert.match(node.title, /没有已验证的同源写契约/);
  }
  assert.equal(send.dataset.writeGate, 'playground.submit');
  assert.equal(submit.dataset.writeGate, 'evaluation.submit');
});

test('both write controls are bound, so neither can be a button that does nothing', async () => {
  const doc = recordingDoc();
  const controller = createOps({ doc, source: mockSource('sharer') });
  await controller.load();

  const selectors = new Set(doc.bound.filter((entry) => entry.type === 'click').map((e) => e.selector));
  for (const selector of Object.keys(WRITE_CONTROL_CONTRACTS)) {
    assert.ok(selectors.has(selector), `${selector} was rendered but never bound`);
  }
});

test('an unverified write is refused with a reason instead of silently doing nothing', async () => {
  const doc = recordingDoc();
  const controller = createOps({ doc, source: mockSource('sharer') });
  await controller.load();

  const result = controller.rejectUnverifiedWrite('playground.submit');
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'no-verified-write-contract:playground.submit');
  assert.equal(controller.lastError, 'no-verified-write-contract:playground.submit');
  assert.deepEqual(controller.lastAction, { kind: 'write-rejected', contract: 'playground.submit' });
});

// --- painting against a shell with no panels --------------------------------

test('painting against a document with none of the views is a no-op, not a crash', async () => {
  const controller = createOps({ doc: emptyDoc(), source: mockSource('admin') });
  await controller.load();
  assert.doesNotThrow(() => controller.paint());
  assert.doesNotThrow(() => controller.openDrawer('dep-demo-1'));
  assert.equal(controller.state, 'ready');
});
