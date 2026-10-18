/*! tests/web-features/ops-render.test.js — the panels, as strings.
 *
 *  This build has no browser, so the *content* of every management panel is
 *  asserted here. The browser suite (`tests/browser/ops-ui.browser.test.js`)
 *  only has to prove the wiring, and it can be skipped on a machine without
 *  Chrome — which is why this file carries the weight.
 *
 *  What it is really checking, beyond "the words appear":
 *
 *    * a missing value renders `—` **with its reason**, never `0`;
 *    * a value renders **with its source and its time**;
 *    * a control that cannot act is `disabled` and names the missing thing;
 *    * the trend geometry treats a `null` as a break, not a step to zero;
 *    * nothing user-supplied reaches the DOM unescaped.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  healthPill,
  renderAdmin,
  renderAdminDeploymentView,
  renderDeploymentCard,
  renderDeploymentDrawer,
  evaluationDisplayState,
  evaluationVisibleCounts,
  renderEvaluation,
  renderKeys,
  renderNotice,
  renderObservability,
  renderOpsStrip,
  renderPlayground,
  renderRegistry,
  renderRequestExample,
  renderShareControl,
  renderTrend,
  shareLabel,
} from '../../web/js/features/render.js';
import { coverage, seriesExtent, seriesSegments } from '../../web/js/features/trend.js';
import { deriveCapabilities, SCOPES } from '../../web/js/features/capability.js';
import { createMockHttp } from '../../web/js/features/mock-fixture.js';

const opsFor = (persona = 'sharer', visibility = 'shared') =>
  createMockHttp({ persona, visibility }).ops();

const capsFor = (ops) => deriveCapabilities({ scopes: ops.scope.scopes });

// --- provenance strip -------------------------------------------------------

test('a synthetic payload is badged as synthetic, at the top of the panel', () => {
  const html = renderOpsStrip(opsFor());
  assert.match(html, /SYNTHETIC/);
  assert.match(html, /不是<\/b> Dell 生产读数|不是/);
  assert.match(html, /FAIL-CLOSED/);
});

test('a live payload is badged LIVE and says nothing about fixtures', () => {
  const ops = opsFor();
  ops.transport = { mode: 'live', gateway_attached: true, synthetic: false, label: 'LIVE' };
  ops.synthetic = false;
  const html = renderOpsStrip(ops);
  assert.match(html, /LIVE/);
  assert.doesNotMatch(html, /SYNTHETIC/);
});

test('with no payload there is no strip at all', () => {
  assert.equal(renderOpsStrip(null), '');
});

// --- notices ----------------------------------------------------------------

test('401 says sign in, 403 says ask an operator, and they are different pages', () => {
  const signedOut = renderNotice('signed_out', 'http-401');
  const denied = renderNotice('denied', 'http-403');
  assert.match(signedOut, /请先登录/);
  assert.match(signedOut, /\/login/);
  assert.match(denied, /未获得模块目录授权/);
  assert.doesNotMatch(denied, /\/login/);
  assert.match(denied, /http-403/);
});

test('an unknown state offers a retry and shows no numbers', () => {
  const html = renderNotice('unknown', 'contract:bad');
  assert.match(html, /data-ops-retry/);
  assert.match(html, /contract:bad/);
  assert.match(html, /不显示任何状态或数字/);
});

// --- the registry: mine vs shared -------------------------------------------

test('owned deployments and endpoints shared to me are named differently', () => {
  const ops = opsFor();
  const html = renderRegistry(ops, capsFor(ops), { visibility: 'shared' });
  assert.match(html, /我拥有的部署/);
  assert.match(html, /别人共享给我的 Endpoint/);
  assert.match(html, /OWNED · 2/);
  assert.match(html, /SHARED TO ME · 1/);
});

test('the private view withholds the shared list, and says why', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderRegistry(ops, capsFor(ops), { visibility: 'private' });
  assert.doesNotMatch(html, /colleague-chat/);
  assert.match(html, /当前是 Private 视图/);
  assert.match(html, /SHARED TO ME · —/);
});

test('a shared endpoint names its owner and who granted it', () => {
  const ops = opsFor();
  const html = renderRegistry(ops, capsFor(ops), { visibility: 'shared' });
  assert.match(html, /colleague-chat/);
  assert.match(html, /ep-demo-shared-1/);
  assert.match(html, /demo-user-b/);
  assert.match(html, /Selected · 指定账号/);
});

test('an empty owned list is an empty state, not "everything is down"', () => {
  const ops = opsFor();
  ops.registry.owned = [];
  const html = renderRegistry(ops, capsFor(ops), {});
  assert.match(html, /没有登记/);
  // No state pill at all: an empty list is not a status claim about a model.
  assert.doesNotMatch(html, /state-pill/);
});

// --- one deployment card ----------------------------------------------------

test('a deployment card shows owner, engine type, health, runtime and resources', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderDeploymentCard(ops.registry.owned[0], capsFor(ops), { writable: true });
  assert.match(html, /所有者 UID/);
  assert.match(html, /demo-user-a/);
  assert.match(html, /Engine type/);
  assert.match(html, /strata/);
  assert.match(html, /运行状态/);
  assert.match(html, /UNKNOWN/); // health unknown ⇒ the pill says so
  assert.match(html, /GPU 卡数/);
});

test('a missing GPU figure renders — with a reason, never 0', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderDeploymentCard(ops.registry.owned[0], capsFor(ops), {});
  assert.doesNotMatch(html, />0 MiB</);
  assert.match(html, /没有已声明的可信数据来源|no_verified_metric_source/);
});

test('an engine type that is unknown renders as — rather than a guess', () => {
  const ops = opsFor('sharer', 'private');
  const dep = ops.registry.owned.find((d) => d.engine_type === null);
  const html = renderDeploymentCard(dep, capsFor(ops), {});
  assert.match(html, /Engine type<\/dt><dd>—/);
});

test('health pills are distinct per state', () => {
  assert.match(healthPill('healthy'), /state-available/);
  assert.match(healthPill('degraded'), /state-unavailable/);
  assert.match(healthPill('unreachable'), /state-unknown/);
  assert.match(healthPill('unknown'), /state-disabled/);
  assert.match(healthPill('nonsense'), /UNKNOWN/);
});

// --- the share control ------------------------------------------------------

test('without the share scope every mode button is disabled and names the scope', () => {
  const ops = opsFor('user', 'private');
  const capabilities = capsFor(ops);
  const html = renderShareControl(ops.registry.owned[0], capabilities, { writable: true });
  const buttons = html.match(/<button[^>]*data-share-mode[^>]*>/g) ?? [];
  assert.equal(buttons.length, 3);
  for (const button of buttons) assert.match(button, /disabled/);
  assert.match(html, /portal:endpoints:share/);
  assert.match(html, /共享控制当前不可用/);
});

test('with the share scope the buttons are live and a principal field appears', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderShareControl(ops.registry.owned[0], capsFor(ops), { writable: true });
  const buttons = html.match(/<button[^>]*data-share-mode[^>]*>/g) ?? [];
  assert.equal(buttons.length, 3);
  for (const button of buttons) assert.doesNotMatch(button, /disabled/);
  assert.match(html, /data-share-principals/);
});

test('a read-only source disables the control even when the scope is held', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderShareControl(ops.registry.owned[0], capsFor(ops), { writable: false });
  const buttons = html.match(/<button[^>]*data-share-mode[^>]*>/g) ?? [];
  for (const button of buttons) assert.match(button, /disabled/);
  assert.match(html, /只读/);
});

test('renderRegistry threads its writable flag down to the cards', () => {
  // Regression: the cards used to read `ops.writable`, a payload field that
  // does not exist, so every share control rendered live no matter what the
  // source could actually accept.
  const ops = opsFor('sharer', 'private');
  const capabilities = capsFor(ops);

  const live = renderRegistry(ops, capabilities, { writable: true });
  for (const button of live.match(/<button[^>]*data-share-mode[^>]*>/g) ?? []) {
    assert.doesNotMatch(button, /\bdisabled\b/);
  }

  const readonly = renderRegistry(ops, capabilities, { writable: false });
  for (const button of readonly.match(/<button[^>]*data-share-mode[^>]*>/g) ?? []) {
    assert.match(button, /\bdisabled\b/);
  }
});

test('renderRegistry defaults to read-only when nobody says otherwise', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderRegistry(ops, capsFor(ops), {});
  for (const button of html.match(/<button[^>]*data-share-mode[^>]*>/g) ?? []) {
    assert.match(button, /\bdisabled\b/);
  }
});

test('the current mode is marked pressed, and private is the default', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderShareControl(ops.registry.owned[0], capsFor(ops), {});
  assert.match(html, /data-share-mode="private"[^>]*aria-pressed="true"/);
  assert.match(html, /data-share-mode="all"[^>]*aria-pressed="false"/);
});

test('the current option of every toggle is visible, not only announced', () => {
  // Regression: the stylesheet has no `[aria-pressed]` rule, so a toggle marked
  // only with aria-pressed looks identical in every state. `.seg button.active`
  // is the shell's styled toggle and is what must carry the current option.
  const ops = opsFor('sharer');
  const capabilities = capsFor(ops);

  const registry = renderRegistry(ops, capabilities, { visibility: 'private', writable: true });
  assert.match(registry, /<div class="seg" role="group" aria-label="可见视图">/);
  assert.match(registry, /class="active"[^>]*data-visibility="private"/);

  const card = renderDeploymentCard(ops.registry.owned[0], capabilities, { writable: true });
  assert.match(card, /<div class="seg" role="group" aria-label="共享范围">/);
  assert.match(card, /class="active"[^>]*data-share-mode="private"/);

  const observability = renderObservability(ops, capabilities, { trendScope: 'mine' });
  assert.match(observability, /<div class="seg" role="group" aria-label="趋势范围">/);
  assert.match(observability, /class="active"[^>]*data-trend-scope="mine"/);
});

test('a disabled option is marked unavailable, not merely unselected', () => {
  const ops = opsFor('user');
  const capabilities = capsFor(ops);
  const card = renderDeploymentCard(ops.registry.owned[0], capabilities, { writable: true });
  const buttons = card.match(/<button[^>]*data-share-mode[^>]*>/g) ?? [];
  assert.equal(buttons.length, 3);

  for (const button of buttons) {
    assert.match(button, /disabled/, 'an ungranted share control must be disabled');
    assert.match(button, /title="[^"]+"/, 'a disabled option must say what is missing');
    if (!/class="active"/.test(button)) {
      // The current option keeps its active marking — that is the more useful
      // fact — but everything else must read as unavailable, not as merely
      // not-selected.
      assert.match(button, /class="unavailable"/);
    }
  }
  assert.equal(buttons.filter((b) => /class="active"/.test(b)).length, 1,
    'exactly one option is the current one');

  const observability = renderObservability(ops, capabilities, { trendScope: 'mine' });
  const global = (observability.match(/<button[^>]*data-trend-scope="global"[^>]*>/g) ?? [])[0];
  assert.match(global, /class="unavailable"/);
  assert.match(global, /disabled/);
});

test('share labels name the three scopes in the reader\'s language', () => {
  assert.match(shareLabel('private'), /仅自己/);
  assert.match(shareLabel('selected'), /指定账号/);
  assert.match(shareLabel('all'), /全部模型用户/);
  assert.match(shareLabel('nonsense'), /按 Private 处理/);
});

// --- the drawer -------------------------------------------------------------

test('the drawer shows the full record and admits the write path is not wired', () => {
  const ops = opsFor('sharer', 'private');
  const html = renderDeploymentDrawer(ops.registry.owned[0], capsFor(ops), { writable: true });
  assert.match(html, /Deployment ID/);
  assert.match(html, /dep-demo-1/);
  assert.match(html, /资源来源/);
  assert.match(html, /backend\/registry/);
  assert.match(html, /不提供|尚未接线/);
});

// --- keys -------------------------------------------------------------------

test('keys render as prefixes and never as secrets', () => {
  const html = renderKeys(opsFor());
  assert.match(html, /llk_••••8m/);
  assert.match(html, /llk_••••2a/);
  assert.doesNotMatch(html, /secret|Bearer/);
  assert.match(html, /明文只在签发那一刻出现一次/);
});

// --- playground and request example -----------------------------------------

test('with no authorized backend the send button is disabled and says why', () => {
  const html = renderPlayground(opsFor(), capsFor(opsFor()));
  assert.match(html, /data-playground-send="1" disabled/);
  assert.match(html, /没有经授权的(交互式)?后端|no_authorized_backend/);
  assert.match(html, /不提供/); // no upstream direct connect
});

test('an authorized playground turns the control on and names its backend', () => {
  const ops = opsFor();
  ops.playground = {
    authorized: true, backend: 'authorized-gateway', audited: true,
    endpoint: '/gw/v1/chat/completions', reason: null,
  };
  const html = renderPlayground(ops, capsFor(ops));
  assert.doesNotMatch(html, /data-playground-send="1" disabled/);
  assert.match(html, /已授权/);
});

test('the request example is a placeholder, and says so', () => {
  const html = renderRequestExample(opsFor());
  assert.match(html, /YOUR_SCOPED_KEY/);
  assert.match(html, /不可直接调用/);
  assert.doesNotMatch(html, /sk-|Bearer [A-Za-z0-9]{16}/);
});

// --- the admin perspective on share scopes ----------------------------------

test('the deployment-wide share view appears only for an admin session', () => {
  const admin = opsFor('admin', 'private');
  const adminHtml = renderRegistry(admin, capsFor(admin), { visibility: 'private' });
  assert.match(adminHtml, /部署范围（admin 视角）/);
  assert.match(adminHtml, /dep-demo-shared-1/);
  assert.match(adminHtml, /demo-user-b/);

  const user = opsFor('user', 'private');
  const userHtml = renderRegistry(user, capsFor(user), { visibility: 'private' });
  assert.doesNotMatch(userHtml, /部署范围（admin 视角）/);
  assert.doesNotMatch(userHtml, /demo-user-b/);
});

test('the admin deployment table marks which row is mine', () => {
  const ops = opsFor('admin', 'private');
  const html = renderAdminDeploymentView(ops, capsFor(ops));
  assert.match(html, /自己/);
  assert.match(html, /共享范围/);
  // It is a view of other people's scopes, not a control over them.
  assert.doesNotMatch(html, /data-share-mode/);
});

test('the admin deployment table renders nothing at all for a non-admin', () => {
  const ops = opsFor('user', 'private');
  assert.equal(renderAdminDeploymentView(ops, capsFor(ops)), '');
});

test('a non-admin payload carrying deployment-wide rows renders none of them', () => {
  // The contract already refuses this shape; the renderer must not be the weak
  // link if one ever reaches it.
  const ops = opsFor('user', 'private');
  ops.registry.all = [{ deployment_id: 'x', model_id: 'y', owner_uid: 'z', health: 'unknown', share: {} }];
  assert.equal(renderAdminDeploymentView(ops, capsFor(ops)), '');
});

// --- callable models --------------------------------------------------------

test('the playground lists models you own, and marks endpoints shared with you', () => {
  const private_ = opsFor('sharer', 'private');
  const privateOptions = renderPlayground(private_, capsFor(private_));
  assert.match(privateOptions, /dep-demo-1/);
  assert.doesNotMatch(privateOptions, /ep-demo-shared-1/);

  const shared = opsFor('sharer', 'shared');
  const sharedOptions = renderPlayground(shared, capsFor(shared));
  assert.match(sharedOptions, /ep-demo-shared-1/);
  assert.match(sharedOptions, /共享：demo-user-b/);
});

// --- observability ----------------------------------------------------------

test('every metric without a source renders UNKNOWN with its reason', () => {
  const ops = opsFor();
  const html = renderObservability(ops, capsFor(ops), { trendScope: 'mine' });
  assert.equal((html.match(/class="state-pill state-unknown"/g) ?? []).length, ops.metrics.items.length);
  assert.match(html, /没有已声明的可信数据来源|no_verified_metric_source/);
  assert.doesNotMatch(html, /AVAILABLE/);
});

test('a metric with a value shows its source and the time it was taken', () => {
  const ops = opsFor();
  ops.metrics.items = [{
    name: 'gateway_requests_24h', label: '24h 调用数', unit: '次', kind: 'gateway',
    value: 1204, state: 'available', source: 'gateway-usage-export',
    updated_at: '2026-10-15T04:00:00Z', reason: null,
  }];
  const html = renderObservability(ops, capsFor(ops), {});
  assert.match(html, /1,204 次/);
  assert.match(html, /gateway-usage-export/);
  assert.match(html, /2026-10-15T04:00:00Z/);
  assert.match(html, /state-available/);
});

test('the global trend switch is disabled without the admin scope, and says so', () => {
  const ops = opsFor('sharer');
  const html = renderObservability(ops, capsFor(ops), { trendScope: 'mine' });
  assert.match(html, /data-trend-scope="global"[^>]*disabled/);
  assert.match(html, /portal:admin:read/);
});

test('an admin session gets a working global switch and global series', () => {
  const ops = opsFor('admin');
  const html = renderObservability(ops, capsFor(ops), { trendScope: 'global' });
  assert.doesNotMatch(html, /data-trend-scope="global"[^>]*disabled/);
  assert.match(html, /data-trend-scope="global"/);
  assert.match(html, /全局请求/);
});

test('a global trend with no series is an empty state, not a flat line', () => {
  const ops = opsFor('sharer');
  ops.trends.series = ops.trends.series.filter((s) => s.scope === 'mine');
  const html = renderTrend(ops, { scope: 'global' });
  assert.match(html, /没有全局趋势序列/);
  assert.doesNotMatch(html, /<polyline/);
});

test('a synthetic trend is captioned as synthetic', () => {
  const ops = opsFor();
  const html = renderTrend(ops, { scope: 'mine' });
  assert.match(html, /SYNTHETIC/);
  assert.match(html, /不是任何真实计量|确定性合成序列/);
  assert.match(html, /个测量点/);
});

// --- trend geometry ---------------------------------------------------------

test('a gap splits the line instead of dropping it to zero', () => {
  const points = [10, 20, null, 40, 50];
  const segments = seriesSegments(points, { width: 100, height: 100, pad: 0, min: 0, max: 50 });
  assert.equal(segments.length, 2, 'the gap did not split the series');
  // A zero would have been placed on the bottom edge; nothing is.
  for (const segment of segments) {
    const ys = segment.split(' ').map((pair) => Number(pair.split(',')[1]));
    assert.ok(ys.every((y) => y <= 100 && y >= 0));
    assert.ok(ys.every((y) => y < 100), 'a point landed on the axis, which would read as zero');
  }
});

test('a leading or trailing gap is not drawn as a segment to the edge', () => {
  const segments = seriesSegments([null, 5, 6, null], { width: 100, height: 100, min: 0, max: 10 });
  assert.equal(segments.length, 1);
});

test('a lone measurement between gaps is still drawn', () => {
  const segments = seriesSegments([null, 5, null], { width: 100, height: 100, min: 0, max: 10 });
  assert.equal(segments.length, 1);
  assert.equal(segments[0].split(' ').length, 2);
});

test('an all-gap series draws nothing at all', () => {
  assert.deepEqual(seriesSegments([null, null, null], { width: 100, height: 10, min: 0, max: 1 }), []);
});

test('the extent ignores gaps, and is null when nothing was measured', () => {
  assert.deepEqual(seriesExtent([{ points: [3, null, 9] }]), { min: 3, max: 9, known: 2 });
  assert.equal(seriesExtent([{ points: [null, null] }]), null);
  assert.equal(seriesExtent([]), null);
  assert.equal(seriesExtent(undefined), null);
});

test('a flat series sits on the middle line, not on the floor', () => {
  const segments = seriesSegments([7, 7], { width: 100, height: 100, pad: 0, min: 7, max: 7 });
  const y = Number(segments[0].split(' ')[0].split(',')[1]);
  assert.ok(Math.abs(y - 50) < 0.01, `a flat series was drawn at y=${y}`);
});

test('coverage counts only the measured points', () => {
  assert.equal(coverage([1, null, 3, undefined]), 2);
  assert.equal(coverage([]), 0);
});

// --- evaluation -------------------------------------------------------------

test('the eval submit is disabled and names the missing backend', () => {
  const ops = opsFor('sharer');
  const html = renderEvaluation(ops, capsFor(ops));
  assert.match(html, /data-eval-submit="1" disabled/);
  assert.match(html, /评测后端尚未就绪|evaluation_backend_not_ready/);
});

test('a not-yet-finished run shows no score', () => {
  const ops = opsFor('sharer');
  const html = renderEvaluation(ops, capsFor(ops));
  assert.match(html, /run-demo-2/); // queued
  assert.doesNotMatch(html, /run-demo-3/); // the admin-only global row
});

test('an admin session sees the global rows, a user does not', () => {
  const admin = opsFor('admin');
  assert.match(renderEvaluation(admin, capsFor(admin)), /run-demo-3/);
  const user = opsFor('user');
  assert.doesNotMatch(renderEvaluation(user, capsFor(user)), /run-demo-3/);
});

test('a ready eval backend enables the submit button', () => {
  const ops = opsFor('admin');
  ops.evaluation.submit = { ready: true, reason: null };
  const html = renderEvaluation(ops, capsFor(ops));
  assert.doesNotMatch(html, /data-eval-submit="1" disabled/);
});

test('Evaluation makes synthetic provenance and per-session counts explicit', () => {
  const ops = opsFor('sharer');
  const html = renderEvaluation(ops, capsFor(ops));
  const counts = evaluationVisibleCounts(ops.evaluation.runs);
  assert.equal(evaluationDisplayState(ops).kind, 'synthetic');
  assert.match(html, /data-eval-provenance="synthetic"/);
  assert.match(html, /SYNTHETIC · 演示记录/);
  assert.match(html, /DEMO · 非实测/);
  assert.match(html, new RegExp('data-eval-count="visible">' + counts.visible));
  assert.match(html, /data-eval-explainer/);
  assert.match(html, /data-eval-source-note/);
  assert.match(html, /caption class="tiny"/);
});

test('Evaluation never equates an authenticated BFF response with verified EvalScope reports', () => {
  const ops = opsFor('user');
  ops.synthetic = false;
  ops.transport = { mode: 'live', gateway_attached: true, synthetic: false };
  const html = renderEvaluation(ops, capsFor(ops));
  assert.equal(evaluationDisplayState(ops).kind, 'bff');
  assert.match(html, /LIVE BFF · 报告待核验/);
  assert.match(html, /EvalScope 报告的来源与权限仍需服务端逐条核对/);
  assert.doesNotMatch(html, /SYNTHETIC · 演示记录/);
});

test('Empty scoped Evaluation is not presented as zero global reports or scores', () => {
  const ops = opsFor('user');
  ops.evaluation.runs = [];
  ops.synthetic = false;
  ops.transport = { mode: 'unavailable', gateway_attached: false, synthetic: false };
  const html = renderEvaluation(ops, capsFor(ops));
  assert.equal(evaluationDisplayState(ops).kind, 'unknown');
  assert.match(html, /data-eval-provenance="unknown"/);
  assert.match(html, /data-eval-empty="true"/);
  assert.match(html, /这不意味着全平台没有报告/);
  assert.match(html, /data-eval-count="visible">0/);
  assert.match(html, /data-eval-submit="1" disabled/);
});

test('Evaluation visible counts never infer unseen global rows', () => {
  const counts = evaluationVisibleCounts([
    { status: 'queued' }, { status: 'running' },
    { status: 'done' }, { status: 'failed' },
  ]);
  assert.deepEqual(counts, { visible: 4, active: 2, done: 1, failed: 1 });
  assert.deepEqual(evaluationVisibleCounts(null), {
    visible: 0, active: 0, done: 0, failed: 0,
  });
});

// --- administration ---------------------------------------------------------

test('the admin view lists principals only when the scope is held', () => {
  const user = opsFor('user');
  const userHtml = renderAdmin(user, capsFor(user), {});
  assert.doesNotMatch(userHtml, /demo-user-b/);
  assert.match(userHtml, /portal:admin:read/);
  assert.match(userHtml, /未授权/);

  const admin = opsFor('admin');
  const adminHtml = renderAdmin(admin, capsFor(admin), {});
  assert.match(adminHtml, /demo-user-b/);
  assert.match(adminHtml, /已授权/);
});

test('the admin view says that roles grant nothing and names are not identity', () => {
  const ops = opsFor('admin');
  const html = renderAdmin(ops, capsFor(ops), {});
  assert.match(html, /角色（不产生权限）/);
  assert.match(html, /不来自账号名|不是<\/b>看账号名|权限来自 Scope/);
  assert.match(html, /PAM|Linux 账号/);
});

test('every capability is listed with its grant state', () => {
  const ops = opsFor('user');
  const html = renderAdmin(ops, capsFor(ops), {});
  for (const name of ['readCatalogue', 'shareEndpoints', 'viewAllPrincipals']) {
    assert.match(html, new RegExp(name));
  }
});

// --- escaping ---------------------------------------------------------------

test('nothing from the payload reaches the DOM unescaped', () => {
  const ops = opsFor('sharer', 'private');
  const hostile = '<img src=x onerror="alert(1)">';
  ops.registry.owned[0].model_label = hostile;
  ops.registry.owned[0].owner_uid = hostile;
  ops.scope.subject = hostile;
  const html = renderDeploymentCard(ops.registry.owned[0], capsFor(ops), {})
    + renderAdmin(ops, capsFor(ops), {});
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x/);
  assert.doesNotMatch(html, /onerror="alert/);
});

test('a blocked link renders as a disabled affordance with no href', () => {
  const ops = opsFor('sharer', 'private');
  ops.registry.owned[0].links = [
    { label: '原生 Chat', href: null, kind: 'native', requires: SCOPES.ENGINES_NATIVE },
  ];
  const html = renderDeploymentCard(ops.registry.owned[0], capsFor(ops), {});
  assert.match(html, /aria-disabled="true"/);
  assert.doesNotMatch(html, /href=/);
});

test('an allowed link is a same-origin anchor', () => {
  const ops = opsFor('sharer', 'private');
  ops.registry.owned[0].links = [
    { label: '我的调用日志', href: '/atlas#calls', kind: 'internal', requires: 'atlas:calls:self:read' },
  ];
  const html = renderDeploymentCard(ops.registry.owned[0], capsFor(ops), {});
  assert.match(html, /href="\/atlas#calls"/);
});
