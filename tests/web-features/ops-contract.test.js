/*! tests/web-features/ops-contract.test.js — what `portal-ops/1` must refuse.
 *
 *  The positive cases are one test. Everything else here is a *hostile shape*:
 *  a payload that would make a management screen confidently wrong. Each one is
 *  a real failure mode, not a hypothetical:
 *
 *    * a GPU figure with no NVML source behind it;
 *    * a colleague's deployment rendered under 我拥有的部署;
 *    * a key that arrives in a form that can be rendered;
 *    * a share that cannot be taken back;
 *    * a global eval score on a session that never got the deployment-wide read;
 *    * a `curl` example or a link that points off-origin or at the upstream port.
 *
 *  The fixtures are mutated in place from one base payload, so the difference
 *  between "valid" and "refused" is always exactly the one field under test.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_SHARE,
  OPS_SCHEMA,
  SHARE_MODES,
  effectiveShare,
  isMaskedPrefix,
  shareAllows,
  stateFor,
  validateOps,
} from '../../web/js/features/contract.js';
import { createMockHttp } from '../../web/js/features/mock-fixture.js';
import { SCOPES } from '../../web/js/features/capability.js';

/** A realistic starting point: the fixture's own payload, in the wider of the
 *  two views so that both `registry.owned` and `registry.shared` have a row for
 *  the hostile-shape tests to mutate. */
const base = () => JSON.parse(JSON.stringify(
  createMockHttp({ persona: 'admin', visibility: 'shared' }).ops(),
));
const clone = () => base();

const ok = (payload) => {
  const result = validateOps(payload);
  assert.deepEqual(result.errors, [], 'the base fixture must be valid');
  assert.equal(result.ok, true);
  return result.ops;
};

const refuses = (payload, pattern) => {
  const result = validateOps(payload);
  assert.equal(result.ok, false, `expected a refusal, got ${JSON.stringify(result.ops)}`);
  if (pattern) {
    assert.ok(
      result.errors.some((error) => pattern.test(error)),
      `expected an error matching ${pattern}, got ${JSON.stringify(result.errors)}`,
    );
  }
  return result.errors;
};

const withDeployment = (mutate, index = 0) => {
  const payload = clone();
  mutate(payload.registry.owned[index]);
  return payload;
};

// --- the positive case ------------------------------------------------------

test('a well-formed payload from every persona passes', () => {
  for (const persona of ['user', 'sharer', 'admin', 'ungranted']) {
    const result = validateOps(createMockHttp({ persona }).ops());
    assert.deepEqual(result.errors, [], `persona ${persona} produced an invalid payload`);
    assert.equal(result.ok, true);
  }
});

test('every no-GPU-number guarantee holds in the shipped fixture itself', () => {
  const payload = base();
  for (const deployment of payload.registry.owned) {
    for (const field of ['gpu_count', 'vram_total_mib', 'vram_used_mib']) {
      assert.equal(
        deployment.resources[field], null,
        `${deployment.deployment_id}.resources.${field} carries a number in the fixture`,
      );
    }
  }
  for (const metric of payload.metrics.items) {
    assert.equal(metric.value, null, `metric ${metric.name} carries a number in the fixture`);
  }
});

// --- schema and transport ---------------------------------------------------

test('a different schema version is refused', () => {
  const payload = clone();
  payload.schema_version = 'portal-ops/2';
  refuses(payload, /unsupported schema_version/);
});

test('a synthetic payload may not claim to be live', () => {
  const payload = clone();
  payload.transport.mode = 'live';
  payload.transport.gateway_attached = true;
  refuses(payload, /synthetic payload may not claim to be live/);
});

test('a live payload must state that the gateway is attached', () => {
  const payload = clone();
  payload.synthetic = false;
  payload.transport = { mode: 'live', gateway_attached: false, synthetic: false, label: 'LIVE' };
  refuses(payload, /must state that the gateway is attached/);
});

test('an empty notes list is refused: the view must state its own contract', () => {
  const payload = clone();
  payload.notes = [];
  refuses(payload, /notes must be a non-empty list/);
});

test('a payload that is not an object is refused rather than crashed on', () => {
  for (const value of [null, undefined, 42, 'x', []]) {
    const result = validateOps(value);
    assert.equal(result.ok, false);
    assert.equal(result.ops, null);
  }
});

// --- links may never leave the origin ---------------------------------------

test('a link that leaves the origin is refused', () => {
  for (const href of ['https://lab.example/gw', '//evil.example/x', 'http://127.0.0.1/x']) {
    const payload = clone();
    payload.registry.owned[0].links = [{ label: 'x', href, kind: 'native', requires: 'x' }];
    refuses(payload, /not a same-origin path/);
  }
});

test('a link at the upstream inference port is refused even as a same-origin path', () => {
  for (const href of ['/proxy/127.0.0.1:8080/v1', '/x:8080', '/v1:8080/chat']) {
    const payload = clone();
    payload.registry.owned[0].links = [{ label: 'x', href, kind: 'native', requires: 'x' }];
    refuses(payload, /not a same-origin path/);
  }
});

test('a blocked link must name what it requires and carries no address', () => {
  const good = clone();
  good.registry.owned[0].links = [{ label: '原生 Chat', href: null, kind: 'native', requires: 'portal:engines:native' }];
  ok(good);

  const bad = clone();
  bad.registry.owned[0].links = [{ label: '原生 Chat', href: null, kind: 'native' }];
  refuses(bad, /blocked but names no requirement/);
});

test('the playground endpoint may never name an upstream target', () => {
  const payload = clone();
  payload.playground = { authorized: false, backend: null, audited: false, endpoint: 'http://127.0.0.1:8080/v1', reason: 'no_authorized_backend' };
  refuses(payload, /playground.endpoint is not a same-origin path/);
});

// --- no GPU number without NVML ---------------------------------------------

test('a VRAM figure without the NVML source is refused', () => {
  const payload = withDeployment((dep) => {
    dep.resources = { gpu_count: null, vram_total_mib: 49140, vram_used_mib: null, source: 'prometheus-readonly', updated_at: '2026-10-15T00:00:00Z' };
  });
  refuses(payload, /carries a value without the nvml-verified source/);
});

test('a VRAM figure with no source at all is refused', () => {
  const payload = withDeployment((dep) => {
    dep.resources = { gpu_count: null, vram_total_mib: 49140, vram_used_mib: null, source: null, updated_at: null };
  });
  refuses(payload, /without the nvml-verified source/);
});

test('a VRAM figure from NVML but with no timestamp is refused', () => {
  const payload = withDeployment((dep) => {
    dep.resources = { gpu_count: null, vram_total_mib: 49140, vram_used_mib: null, source: 'nvml-verified', updated_at: null };
  });
  refuses(payload, /without an updated_at/);
});

test('a VRAM figure from NVML with a timestamp is accepted', () => {
  // The rule is "no number without NVML", not "no number ever": a deployment
  // that really attaches NVML must be able to show it. This build attaches
  // nothing, which is why the shipped fixture can never exercise this path.
  const payload = withDeployment((dep) => {
    dep.resources = { gpu_count: 1, vram_total_mib: 49140, vram_used_mib: 45469, source: 'nvml-verified', updated_at: '2026-10-15T00:00:00Z' };
  });
  const ops = ok(payload);
  assert.equal(ops.registry.owned[0].resources.vram_used_mib, 45469);
});

test('a boolean is not a measurement', () => {
  const payload = withDeployment((dep) => {
    dep.resources = { gpu_count: true, vram_total_mib: null, vram_used_mib: null, source: null, updated_at: null };
  });
  refuses(payload, /must be null or a finite number/);
});

test('a GPU metric with a value from a non-NVML source is refused', () => {
  const payload = clone();
  payload.metrics.items = [{
    name: 'gpu_vram_total_mib', label: 'GPU 显存总量', unit: 'MiB', kind: 'gpu',
    value: 49140, state: 'available', source: 'gateway-usage-export',
    updated_at: '2026-10-15T00:00:00Z', reason: null,
  }];
  refuses(payload, /may carry a value only from nvml-verified/);
});

// --- metrics say where they came from ---------------------------------------

test('a metric with a value must name its source', () => {
  const payload = clone();
  payload.metrics.items = [{
    name: 'gateway_requests_24h', label: 'x', unit: '次', kind: 'gateway',
    value: 1204, state: 'available', source: null, updated_at: '2026-10-15T00:00:00Z', reason: null,
  }];
  refuses(payload, /carries a value without a source/);
});

test('a metric with a value must carry the time it was taken', () => {
  const payload = clone();
  payload.metrics.items = [{
    name: 'gateway_requests_24h', label: 'x', unit: '次', kind: 'gateway',
    value: 1204, state: 'available', source: 'gateway-usage-export', updated_at: null, reason: null,
  }];
  refuses(payload, /carries a value without an updated_at/);
});

test('a metric with no value must be unknown and say why', () => {
  const payload = clone();
  payload.metrics.items = [{
    name: 'gateway_requests_24h', label: 'x', unit: '次', kind: 'gateway',
    value: null, state: 'available', source: null, updated_at: null, reason: null,
  }];
  const errors = refuses(payload);
  assert.ok(errors.some((e) => /must be unknown/.test(e)));
  assert.ok(errors.some((e) => /must say why/.test(e)));
});

test('an unverified metric source is refused', () => {
  const payload = clone();
  payload.metrics.items = [{
    name: 'gateway_requests_24h', label: 'x', unit: '次', kind: 'gateway',
    value: 12, state: 'available', source: 'vibes', updated_at: '2026-10-15T00:00:00Z', reason: null,
  }];
  refuses(payload, /unverified source/);
});

// --- whose deployment is it --------------------------------------------------

test('a deployment in "mine" that belongs to someone else is refused', () => {
  const payload = withDeployment((dep) => { dep.owner_uid = 'demo-user-b'; });
  refuses(payload, /owner_uid is not this session's subject/);
});

test('a deployment in "mine" that does not say owner_is_self is refused', () => {
  const payload = withDeployment((dep) => { dep.owner_is_self = false; });
  refuses(payload, /does not say owner_is_self/);
});

test('a shared endpoint that is actually my own is refused', () => {
  const payload = clone();
  payload.registry.shared[0].owner_uid = payload.scope.subject;
  refuses(payload, /this session's own subject/);
});

test('a shared endpoint carrying the private mode is refused', () => {
  const payload = clone();
  payload.registry.shared[0].share.mode = SHARE_MODES.PRIVATE;
  refuses(payload, /shared with me but carries the private mode/);
});

test('the private view carries no shared endpoints at all', () => {
  const payload = clone();
  payload.registry.shared = [];
  payload.scope.visibility = 'private';
  ok(payload);
});

// --- the deployment-wide view is admin-only ---------------------------------

test('deployment-wide rows without the admin scope are refused', () => {
  const payload = clone();
  payload.scope.scopes = [SCOPES.MODULES_READ];
  payload.admin = { available: false, reason: 'missing_required_scope', principals: [] };
  refuses(payload, /registry.all is present but this session lacks portal:admin:read/);
});

test('deployment-wide rows are accepted for an admin session', () => {
  const ops = ok(clone());
  assert.equal(ops.registry.all.length >= 2, true);
  // An admin view is allowed to hold other people's rows — that is its purpose.
  assert.ok(ops.registry.all.some((row) => row.owner_uid !== ops.scope.subject));
});

test('a deployment-wide row still has to be coherent', () => {
  const badHealth = clone();
  badHealth.registry.all[0].health = 'excellent';
  refuses(badHealth, /health is not supported/);

  const badShare = clone();
  badShare.registry.all[0].share = { mode: 'all', principals: [], revocable: false };
  refuses(badShare, /must be revocable/);

  const unexpected = clone();
  unexpected.registry.all[0].prompt = 'x';
  refuses(unexpected, /must never show/);
});

// --- share scopes ------------------------------------------------------------

test('a deployment with no share block is private by default', () => {
  const payload = withDeployment((dep) => { delete dep.share; });
  const ops = ok(payload);
  assert.deepEqual(effectiveShare(ops.registry.owned[0]), { ...DEFAULT_SHARE, principals: [] });
});

test('"selected" with no principals is refused', () => {
  const payload = withDeployment((dep) => { dep.share = { mode: 'selected', principals: [], revocable: true }; });
  refuses(payload, /names no principals/);
});

test('a share that is not revocable is refused', () => {
  for (const mode of ['selected', 'all']) {
    const payload = withDeployment((dep) => {
      dep.share = { mode, principals: mode === 'selected' ? ['demo-user-b'] : [], revocable: false };
    });
    refuses(payload, /must be revocable/);
  }
});

test('an unsupported share mode is refused', () => {
  const payload = withDeployment((dep) => { dep.share = { mode: 'everyone', principals: [], revocable: true }; });
  refuses(payload, /share.mode is not supported/);
});

test('shareAllows answers for all three modes, and defaults to nobody', () => {
  assert.equal(shareAllows({}, 'demo-user-b'), false);
  assert.equal(shareAllows({ share: { mode: 'private' } }, 'demo-user-b'), false);
  assert.equal(shareAllows({ share: { mode: 'all', revocable: true } }, 'demo-user-b'), true);
  assert.equal(shareAllows({ share: { mode: 'selected', principals: ['demo-user-b'] } }, 'demo-user-b'), true);
  assert.equal(shareAllows({ share: { mode: 'selected', principals: ['demo-user-c'] } }, 'demo-user-b'), false);
});

// --- the deployment itself ---------------------------------------------------

test('a deployment claiming healthy while not running is refused', () => {
  const payload = withDeployment((dep) => { dep.health = 'healthy'; dep.runtime_status = 'stopped'; });
  refuses(payload, /claims healthy while not running/);
});

test('an unknown health must state a reason', () => {
  const payload = withDeployment((dep) => { dep.health = 'unknown'; dep.health_reason = null; });
  refuses(payload, /health is unknown but states no reason/);
});

test('an unexpected field on a deployment is refused', () => {
  const payload = withDeployment((dep) => { dep.notes = 'extra'; });
  refuses(payload, /unexpected field notes/);
});

test('a prompt or completion riding along is refused outright', () => {
  for (const field of ['prompt', 'completion', 'messages', 'text']) {
    const payload = withDeployment((dep) => { dep[field] = 'secret text'; });
    const errors = refuses(payload);
    assert.ok(
      errors.some((e) => /must never show/.test(e)),
      `${field} was not refused as content: ${JSON.stringify(errors)}`,
    );
  }
});

// --- keys --------------------------------------------------------------------

test('a key carrying a secret field is refused', () => {
  for (const field of ['secret', 'key', 'value', 'plaintext', 'token']) {
    const payload = clone();
    payload.keys[0][field] = 'llk_live_abcdef';
    const errors = refuses(payload);
    assert.ok(
      errors.some((e) => /must never leave the server/.test(e)),
      `${field} was not refused: ${JSON.stringify(errors)}`,
    );
  }
});

test('an unmasked key prefix is refused', () => {
  const payload = clone();
  payload.keys[0].prefix = 'llk_live_abcdef8m';
  refuses(payload, /is not masked/);
});

test('isMaskedPrefix accepts the shape the UI actually shows', () => {
  assert.equal(isMaskedPrefix('llk_••••8m'), true);
  assert.equal(isMaskedPrefix('llk_****2a'), true);
  assert.equal(isMaskedPrefix('llk_live_abcdef8m'), false);
  assert.equal(isMaskedPrefix(''), false);
  assert.equal(isMaskedPrefix('a'.repeat(40) + '•'), false);
});

// --- evaluation --------------------------------------------------------------

test('a global eval row without the admin scope is refused', () => {
  const payload = clone();
  payload.scope.scopes = [SCOPES.MODULES_READ];
  refuses(payload, /global eval run but this session lacks/);
});

test('an eval score on a run that has not finished is refused', () => {
  const payload = clone();
  const run = payload.evaluation.runs.find((r) => r.status === 'queued');
  run.metric = 0.9;
  refuses(payload, /carries a metric while its status is queued/);
});

test('a not-ready eval submit must state a reason', () => {
  const payload = clone();
  payload.evaluation.submit = { ready: false, reason: null };
  refuses(payload, /not-ready eval submit must state a reason/);
});

// --- administration ----------------------------------------------------------

test('an available admin view without the admin scope is refused', () => {
  const payload = clone();
  payload.scope.scopes = [SCOPES.MODULES_READ];
  refuses(payload, /admin.available is true but this session lacks/);
});

test('an unavailable admin view must state a reason and list nobody', () => {
  const missingReason = clone();
  missingReason.admin = { available: false, reason: null, principals: [] };
  refuses(missingReason, /must state a reason/);

  const leaked = clone();
  leaked.admin = { available: false, reason: 'missing_required_scope', principals: [{ uid: 'demo-user-a' }] };
  refuses(leaked, /must not list principals/);
});

test('the admin view is never a name: the same payload passes on the scope alone', () => {
  const withScope = clone();
  withScope.scope.roles = [];
  ok(withScope); // roles emptied, scope kept — still valid
});

// --- trends ------------------------------------------------------------------

test('a synthetic trend series may not ride on a non-synthetic payload', () => {
  const payload = clone();
  payload.synthetic = false;
  payload.transport = { mode: 'demo', gateway_attached: false, synthetic: false, label: 'x' };
  refuses(payload, /synthetic trend series may not ride on a non-synthetic payload/);
});

test('a non-synthetic trend series needs a verified source and a timestamp', () => {
  const noSource = clone();
  noSource.trends.synthetic = false;
  refuses(noSource, /unverified source/);

  const withSource = clone();
  withSource.trends.synthetic = false;
  withSource.trends.source = 'gateway-usage-export';
  withSource.trends.updated_at = null;
  refuses(withSource, /must carry an updated_at/);

  const complete = clone();
  complete.trends.synthetic = false;
  complete.trends.source = 'gateway-usage-export';
  complete.trends.updated_at = '2026-10-15T00:00:00Z';
  complete.trends.reason = null;
  ok(complete);
});

test('a global trend series without the admin scope is refused', () => {
  const payload = clone();
  payload.scope.scopes = [SCOPES.MODULES_READ];
  payload.evaluation.runs = payload.evaluation.runs.filter((run) => run.scope === 'mine');
  payload.admin = { available: false, reason: 'missing_required_scope', principals: [] };
  refuses(payload, /global series but this session lacks/);
});

test('a gap in a series is allowed and stays a gap', () => {
  const payload = clone();
  payload.trends.series[0].points[3] = null;
  const ops = ok(payload);
  assert.equal(ops.trends.series[0].points[3], null);
});

test('a non-numeric, non-null point is refused', () => {
  const payload = clone();
  payload.trends.series[0].points[3] = '12';
  refuses(payload, /must be null or a finite number/);
});

// --- state derivation --------------------------------------------------------

test('a declared page state wins over an empty payload', () => {
  assert.equal(stateFor(null), 'unknown');
  assert.equal(stateFor({ state: 'signed_out' }), 'signed_out');
  assert.equal(stateFor({ state: 'denied' }), 'denied');
  assert.equal(stateFor({ state: 'ready' }), 'ready');
  // A 403 must never be relabelled as "no deployments".
  assert.equal(stateFor({ state: 'denied', registry: { owned: [] } }), 'denied');
});

test('the schema constant is the one this client claims', () => {
  assert.equal(OPS_SCHEMA, 'portal-ops/1');
  assert.equal(base().schema_version, OPS_SCHEMA);
});
