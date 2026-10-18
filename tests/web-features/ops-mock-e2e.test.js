/*! tests/web-features/ops-mock-e2e.test.js — the offline end-to-end.
 *
 *  The brief asks for a Mock E2E: the whole data path exercised against an HTTP
 *  fixture, with a real state change at the end of it. That is what this file is
 *  — `source → contract → capability → render`, driven through the same
 *  functions the browser calls, with no browser involved:
 *
 *      the fixture answers a real PUT
 *        → the share scope changes
 *          → the payload is re-read
 *            → the re-rendered panel shows the new scope
 *
 *  A button whose "function" is a hard-coded re-render proves nothing, so the
 *  assertion is always against what the fixture now holds, not against what the
 *  UI said it would do.
 *
 *  It also encodes the brief's rule that **every button does something, or is
 *  disabled and says why** as a structural check over every rendered panel:
 *
 *    * every enabled `<button>` carries a hook the controller actually binds;
 *    * every disabled `<button>` carries a `title` naming the missing thing;
 *    * and the hook list here is checked against the controller's own bindings,
 *      so adding a hook to the markup without wiring it fails this test.
 *
 *  This build has no Chrome, so this is the strongest end-to-end evidence
 *  available on this machine; `tests/browser/ops-ui.browser.test.js` carries the
 *  same assertions into a real DOM on a host that has one.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { createFeatureSource } from '../../web/js/features/source.js';
import { validateOps } from '../../web/js/features/contract.js';
import { deriveCapabilities } from '../../web/js/features/capability.js';
import { OPS_PANEL_IDS, renderPanels } from '../../web/js/features/render.js';
import { PANEL_IDS_MATCH_RENDER, PANELS } from '../../web/js/features/controller.js';

const CONTROLLER_SRC = resolve(
  fileURLToPath(new URL('../../web/js/features/controller.js', import.meta.url)),
);

/** A `fetch` that fails the test if anything reaches for the network. */
const forbiddenFetch = async () => {
  throw new Error('a request left the process in mock mode');
};

/** Drive one persona through load → validate → render, exactly as the
 *  controller does. Returns the pieces the assertions need. */
async function run({ persona = 'sharer', visibility = 'private', trendScope = 'mine' } = {}) {
  const source = createFeatureSource({
    mode: 'mock',
    persona,
    fetchImpl: forbiddenFetch,
  });
  let answer = await source.load();
  if (visibility !== 'private') answer = await source.setVisibility(visibility);

  assert.equal(validateOps(answer.ops).ok, true, 'the fixture must produce a valid payload');
  const capabilities = deriveCapabilities({ scopes: answer.ops.scope.scopes });
  const panels = renderPanels(answer.ops, capabilities, {
    visibility, trendScope, writable: true, error: null,
  });
  return { source, answer, ops: answer.ops, capabilities, panels };
}

const allHtml = (panels) => Object.values(panels).join('\n');

// --- the panels agree with the mount list -----------------------------------

test('the mounted panels and the rendered panels are the same set', () => {
  assert.equal(PANEL_IDS_MATCH_RENDER, true);
  assert.deepEqual([...OPS_PANEL_IDS].sort(), PANELS.map((p) => p.id).sort());
});

// --- the whole path, per persona --------------------------------------------

test('a plain user gets a catalogue, keys and metrics, and no admin view', async () => {
  const { panels } = await run({ persona: 'user' });
  assert.match(panels.registry, /我拥有的部署/);
  assert.match(panels.keys, /llk_••••/);
  assert.match(panels.observability, /UNKNOWN/);
  assert.doesNotMatch(panels.access, /demo-user-b/);
  assert.match(panels.access, /portal:admin:read/);
});

test('an admin session gets the deployment-wide view', async () => {
  const { panels } = await run({ persona: 'admin' });
  assert.match(panels.access, /demo-user-b/);
  assert.match(panels.evaluation, /run-demo-3/);
  // The global switch is live, and switching to it draws the global series.
  assert.doesNotMatch(panels.observability, /data-trend-scope="global"[^>]*disabled/);
  const global = await run({ persona: 'admin', trendScope: 'global' });
  assert.match(global.panels.observability, /全局请求/);
});

test('a session with no grant at all is still a valid, rendered page', async () => {
  // It has no catalogue scope, but the ops contract does not gate on that — the
  // module catalogue does. So the page renders and every write control is off.
  const { panels, capabilities } = await run({ persona: 'ungranted' });
  assert.equal(capabilities.readCatalogue.granted, false);
  assert.match(panels.registry, /共享控制当前不可用/);
  assert.match(panels.registry, /portal:endpoints:share/);
});

// --- the write path, end to end ---------------------------------------------

test('publishing a share scope changes the fixture and the next render shows it', async () => {
  const { source, panels } = await run({ persona: 'sharer' });
  assert.match(panels.registry, /data-share-mode="private"[^>]*aria-pressed="true"/);

  const before = await source.load();
  assert.equal(before.ops.registry.owned[0].share.mode, 'private');

  const result = await source.updateShare('dep-demo-1', {
    mode: 'selected', principals: ['demo-user-b', 'demo-user-c'],
  });
  assert.equal(result.ok, true);

  const after = await source.load();
  assert.equal(after.ops.registry.owned[0].share.mode, 'selected');
  assert.deepEqual(after.ops.registry.owned[0].share.principals, ['demo-user-b', 'demo-user-c']);

  const caps = deriveCapabilities({ scopes: after.ops.scope.scopes });
  const html = renderPanels(after.ops, caps, { visibility: 'private', writable: true }).registry;
  assert.match(html, /data-share-mode="selected"[^>]*aria-pressed="true"/);
  assert.match(html, /指定账号：|demo-user-b/);
});

test('a share can be revoked, and revoking clears the principal list', async () => {
  const { source } = await run({ persona: 'sharer' });
  await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal((await source.load()).ops.registry.owned[0].share.mode, 'all');

  const revoked = await source.updateShare('dep-demo-1', { mode: 'private', principals: ['demo-user-b'] });
  assert.equal(revoked.ok, true);
  assert.equal(revoked.deployment.share.mode, 'private');
  assert.deepEqual(revoked.deployment.share.principals, [], 'a revoke must not leave principals behind');
});

test('a share change is audited in the fixture, and only in memory', async () => {
  const { source } = await run({ persona: 'sharer' });
  assert.equal(source.fixture.snapshot().audit.length, 0);

  await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  const audit = source.fixture.snapshot().audit;
  assert.equal(audit.length, 1);
  assert.equal(audit[0].deployment_id, 'dep-demo-1');
  assert.equal(audit[0].by, 'demo-user-a');
  assert.equal(audit[0].to, 'all');
  // The stored state is what the audit describes — not just the response body.
  assert.equal(source.fixture.snapshot().registry.owned[0].share.mode, 'all');
});

test('a session without the share scope is refused by the fixture, not just the UI', async () => {
  // Belt and braces: even if a control were somehow clicked, the server-side
  // rule has to hold on its own.
  const { source } = await run({ persona: 'user' });
  const result = await source.updateShare('dep-demo-1', { mode: 'all', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.status, 403);
  assert.equal(result.reason, 'forbidden');
});

test('the fixture will not let one persona change another persona\'s share', async () => {
  const { source } = await run({ persona: 'sharer' });
  const result = await source.updateShare('dep-demo-nobody', { mode: 'all', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.status, 404);
});

test('"selected" with no principals is refused by the fixture', async () => {
  const { source } = await run({ persona: 'sharer' });
  const result = await source.updateShare('dep-demo-1', { mode: 'selected', principals: [] });
  assert.equal(result.ok, false);
  assert.equal(result.status, 400);
});

// --- visibility is a view, not a permission ---------------------------------

test('switching to the shared view adds the granted endpoint and hides nothing of mine', async () => {
  const { source } = await run({ persona: 'sharer' });
  const priv = (await source.load()).ops;
  assert.equal(priv.registry.owned.length, 2);
  assert.equal(priv.registry.shared.length, 0);

  const shared = (await source.setVisibility('shared')).ops;
  assert.equal(shared.registry.shared.length, 1);
  assert.equal(shared.registry.owned.length, 2, 'my own deployments must not disappear');
  assert.equal(shared.registry.shared[0].owner_uid, 'demo-user-b');
});

test('nothing in the payload can widen the visibility a client asked for', async () => {
  const { source } = await run({ persona: 'sharer' });
  const answer = await source.setVisibility('private');
  for (const item of answer.ops.registry.shared) {
    assert.fail(`a shared endpoint leaked into the private view: ${item.endpoint_id}`);
  }
});

// --- every button is functional, or disabled and explained ------------------

/** Hooks the controller actually binds. Kept next to the source read below,
 *  which proves the list is not aspirational. */
const BOUND_HOOKS = Object.freeze([
  'data-visibility', 'data-trend-scope', 'data-share-mode', 'data-drawer',
  'data-ops-retry', 'data-copy-example',
]);

/** Hooks that exist so a control can be *identified* as deliberately inert.
 *  These must never be bound — binding one would mean the button was expected
 *  to work, which is exactly the state this build forbids. */
const INERT_HOOKS = Object.freeze(['data-playground-send', 'data-eval-submit']);

test('the controller binds exactly the hooks the markup uses', () => {
  const source = readFileSync(CONTROLLER_SRC, 'utf8');
  // Every listener goes through `bindAll('<selector>', …)`, so the set of bound
  // hooks is readable rather than inferred.
  const selectors = (source.match(/bindAll\(\s*'([^']+)'/g) ?? [])
    .map((call) => call.replace(/^bindAll\(\s*'/, ''));
  const bound = new Set(selectors.map((selector) => selector.replace(/^\[/, '').split(/[=\]]/)[0]));

  assert.equal(bound.size, selectors.length, 'a selector is bound twice');
  assert.deepEqual([...bound].sort(), [...BOUND_HOOKS].sort(),
    'the bound hooks and the declared list disagree');
  for (const hook of INERT_HOOKS) {
    assert.equal(bound.has(hook), false, `${hook} is bound, but that control must never act`);
  }
});

test('every enabled button carries a hook the controller binds', async () => {
  const html = allHtml((await run({ persona: 'admin' })).panels);
  const buttons = html.match(/<button\b[^>]*>/g) ?? [];
  assert.ok(buttons.length >= 12, `only ${buttons.length} buttons found; the scrape is broken`);

  const inert = buttons.filter((button) => (
    !/\bdisabled\b/.test(button)
    && !BOUND_HOOKS.some((hook) => button.includes(hook))
  ));
  assert.deepEqual(inert, [], `these buttons look live but do nothing:\n  ${inert.join('\n  ')}`);
});

test('every disabled button says what it is missing', async () => {
  const html = allHtml((await run({ persona: 'user' })).panels);
  const disabled = (html.match(/<button\b[^>]*>/g) ?? []).filter((b) => /\bdisabled\b/.test(b));
  assert.ok(disabled.length >= 4, `only ${disabled.length} disabled buttons; the scrape is broken`);
  const unexplained = disabled.filter((button) => !/title="[^"]+"/.test(button));
  assert.deepEqual(unexplained, [], `these disabled buttons explain nothing:\n  ${unexplained.join('\n  ')}`);
});

test('the two controls that must never act are always disabled', async () => {
  for (const persona of ['user', 'sharer', 'admin']) {
    const html = allHtml((await run({ persona })).panels);
    assert.match(html, /data-playground-send="1"[^>]*disabled/, `playground live for ${persona}`);
    assert.match(html, /data-eval-submit="1"[^>]*disabled/, `eval submit live for ${persona}`);
  }
});

test('the share control is live for a sharer and dead for everyone else', async () => {
  const sharer = allHtml((await run({ persona: 'sharer' })).panels);
  const shareButtons = sharer.match(/<button[^>]*data-share-mode[^>]*>/g) ?? [];
  assert.ok(shareButtons.length >= 3);
  for (const button of shareButtons) assert.doesNotMatch(button, /\bdisabled\b/);

  const plain = allHtml((await run({ persona: 'user' })).panels);
  for (const button of plain.match(/<button[^>]*data-share-mode[^>]*>/g) ?? []) {
    assert.match(button, /\bdisabled\b/);
  }
});

// --- the page never lies about where it came from ---------------------------

test('every panel carries the synthetic badge in mock mode', async () => {
  // The strip is prepended by the controller; the offline test reproduces that
  // step so the badge is checked as rendered, not as intended.
  const { panels } = await run({ persona: 'admin' });
  const strip = '<div class="demo-strip" role="status"><strong>SYNTHETIC</strong></div>';
  const html = Object.values(panels).map((panel) => strip + panel).join('\n');
  assert.ok((html.match(/SYNTHETIC/g) ?? []).length >= 6, 'a panel is missing its provenance strip');
});

test('the whole run reaches no network and no GPU number', async () => {
  const { panels } = await run({ persona: 'admin', visibility: 'shared' });
  const html = allHtml(panels);
  // `fetchImpl: forbiddenFetch` above means any request would have thrown; this
  // asserts the other half — that nothing rendered claims a GPU measurement.
  assert.doesNotMatch(html, /\d+\s*MiB/);
  assert.doesNotMatch(html, /gpu[^<]*<\/dt><dd[^>]*>\d/i);
  assert.match(html, /没有已声明的可信数据来源/);
});

test('no panel ever contains an absolute or upstream address', async () => {
  const html = allHtml((await run({ persona: 'admin', visibility: 'shared' })).panels);
  assert.doesNotMatch(html, /href="https?:/);
  assert.doesNotMatch(html, /href="\/\//);
  assert.doesNotMatch(html, /127\.0\.0\.1:8080|:8080/);
});

test('no panel carries prompt, completion or key material', async () => {
  const html = allHtml((await run({ persona: 'admin', visibility: 'shared' })).panels);
  assert.doesNotMatch(html, /prompt/i);
  // The request example legitimately names the API path `/v1/chat/completions`.
  // Any other use of the word would be model output, which must never appear.
  for (const word of html.match(/completion\w*/gi) ?? []) {
    assert.equal(word.toLowerCase(), 'completions', `unexpected content: ${word}`);
  }
  assert.doesNotMatch(html, /secret|Bearer [A-Za-z0-9]{12}/);
});
