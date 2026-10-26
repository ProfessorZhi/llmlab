/*! web/js/features/contract.js — the `portal-ops/1` payload, checked before it is shown.
 *
 *  `portal-ops/1` is the **front-end-side** aggregate for the five management
 *  views (models & engines, api & playground, observability, evaluation, admin).
 *  It is not a production contract yet: the services that would produce it
 *  (`backend/registry`, `backend/observability`, `backend/evaluation`,
 *  `backend/auth_linux`) are being built independently and none of them is
 *  merged. So this file does two jobs and is honest about which is which:
 *
 *    * it **pins the shape the browser will accept**, so the eventual adapter
 *      has something concrete to satisfy, and
 *    * it **refuses the failure modes that would make a management UI lie**.
 *
 *  The refusals are the point. Each one below is a specific way this screen
 *  could be confidently wrong, not a general "be careful":
 *
 *  1. **A GPU number without NVML.** `resources.vram_*`, `gpu_count` and every
 *     `gpu_*` metric may be a number only when its `source` is `nvml-verified`.
 *     This build attaches no NVML source, so in practice every GPU cell is
 *     UNKNOWN. A metric with a value must also name its `source` **and** its
 *     `updated_at` — "12 GB" with no provenance is worse than "—".
 *  2. **A link that leaves the origin or names the upstream port.** Delegated to
 *     `isInternalHref` from `../modules/contract.js`, so the reviewed rule has
 *     exactly one implementation: the browser is never handed a path around the
 *     Gateway, and `/…:8080…` is refused as a substring, not by parsing.
 *  3. **My rows containing someone else's deployment.** `registry.owned` must be
 *     `owner_is_self` *and* `owner_uid === scope.subject`; `registry.shared`
 *     must be the opposite. This is the same defence the call log applies to
 *     `key_id`, and it is what stops "我拥有的部署" from quietly listing a
 *     colleague's model.
 *  4. **A `global` eval row on a session without `portal:admin:read`.** Same
 *     class as "a shared endpoint leaked into a private view".
 *  5. **A key prefix that is a key.** `secret`/`token`/`value`/`plaintext` are
 *     forbidden fields and the `prefix` must actually be masked.
 *  6. **A share mode that is neither revocable nor coherent.** `private` is the
 *     default when the block is absent; `selected` must name principals; any
 *     non-private mode must say it is revocable.
 *  7. **`synthetic` data claiming to be `live`**, or a live claim without an
 *     attached gateway — identical to the modules and call-log contracts.
 *
 *  Missing numbers stay `null` all the way to the DOM (`—`), never `0`.
 *
 *  No DOM, no network: pure functions, so this is unit-testable under plain Node.
 */

import { isInternalHref } from '../modules/contract.js';

/** The only schema this client understands. */
export const OPS_SCHEMA = 'portal-ops/1';

export const TRANSPORT_MODES = Object.freeze({ DEMO: 'demo', LIVE: 'live' });

/** Page states, mapped 1:1 onto what the server answered.
 *
 *  `LOADING` is the one state that is not a server answer: it is the page saying
 *  it has asked and has not been told yet. Without it the first paint showed the
 *  *unknown* notice — "没有取到可用的管理数据，也没有得到明确答复" — for a request
 *  that was still in flight, which reports a failure that has not happened. It is
 *  only ever shown when there is no payload to keep on screen, so a reload never
 *  blanks a panel that already has data. */
export const OPS_STATES = Object.freeze({
  READY: 'ready',
  LOADING: 'loading',
  UNKNOWN: 'unknown',
  DENIED: 'denied',
  SIGNED_OUT: 'signed_out',
});

/** Transport statuses that are answers about *access*, not about data. */
export const STATE_CHANGING_STATUSES = Object.freeze([401, 403]);

export const SHARE_MODES = Object.freeze({
  PRIVATE: 'private',
  SELECTED: 'selected',
  ALL: 'all',
});

export const VISIBILITY = Object.freeze({ PRIVATE: 'private', SHARED: 'shared' });

export const HEALTH = Object.freeze({
  HEALTHY: 'healthy',
  DEGRADED: 'degraded',
  UNREACHABLE: 'unreachable',
  UNKNOWN: 'unknown',
});

export const RUNTIME_STATUS = Object.freeze({
  RUNNING: 'running',
  STOPPED: 'stopped',
  STARTING: 'starting',
  UNKNOWN: 'unknown',
});

export const EVAL_STATUS = Object.freeze({
  QUEUED: 'queued',
  RUNNING: 'running',
  DONE: 'done',
  FAILED: 'failed',
  UNKNOWN: 'unknown',
});

export const METRIC_STATES = Object.freeze({ AVAILABLE: 'available', UNKNOWN: 'unknown' });

/** The only observation source allowed to back a GPU number. Mirrors
 *  `GPU_METRIC_SOURCE` in `backend/portal/modules.py`; kept here as well because
 *  the browser must not depend on the server having remembered. */
export const GPU_METRIC_SOURCE = 'nvml-verified';

/** Sources this build treats as *verified* for any metric, not just GPU. */
export const VERIFIED_METRIC_SOURCES = Object.freeze([
  'gateway-usage-export',
  'nvml-verified',
  'prometheus-readonly',
]);

/** Metric names that describe the GPU, and therefore fall under the NVML rule. */
export const GPU_METRIC_PREFIX = 'gpu_';

/** Fields a key record must never carry. A management page that can be made to
 *  render a live secret is a credential-disclosure bug, not a display bug. */
const FORBIDDEN_KEY_FIELDS = Object.freeze([
  'secret', 'key', 'value', 'plaintext', 'token', 'key_material', 'credential',
]);

/** Fields an endpoint/deployment record must never carry: this surface shows
 *  metadata, and a prompt or completion riding along would make it a content
 *  view without any of the consent machinery the call log has. */
const FORBIDDEN_CONTENT_FIELDS = Object.freeze([
  'prompt', 'prompts', 'completion', 'completions', 'messages', 'content',
  'text', 'input_text', 'output_text', 'body', 'payload',
]);

const isPlainObject = (value) =>
  !!value && typeof value === 'object' && !Array.isArray(value);

const isNonEmptyString = (value) => typeof value === 'string' && value.trim().length > 0;

/** A finite number, `null`, or `undefined`. Booleans are rejected: `true` is not
 *  a measurement, and `Number.isFinite(true)` would have accepted it. */
const numberOrNull = (value) => {
  if (value === null || value === undefined) return null;
  if (typeof value === 'boolean') return undefined;
  return Number.isFinite(value) ? value : undefined;
};

/** Reasons the server may give, in the reader's language. An unknown reason is
 *  still shown — as the raw token — never swallowed. */
const REASON_LABELS = Object.freeze({
  no_authenticated_session: '会话无效或缺失',
  missing_required_scope: '缺少所需权限（scope）',
  registry_adapter_not_attached: 'Registry 适配器尚未接线',
  observability_adapter_not_attached: '观测适配器尚未接线',
  evaluation_backend_not_ready: '评测后端尚未就绪',
  no_authorized_backend: '没有经授权的交互式后端',
  no_verified_metric_source: '没有已声明的可信数据来源',
  stale_sample: '观测值已过期；等待重新读取',
  unverified_freshness: '观测值缺少可信的新鲜度证据',
  linux_auth_not_enabled: 'Linux 账号 / PAM 认证尚未启用',
  preview_build_has_no_real_backend: '预览构建没有真实后端（合成输出不等于试跑）',
});

export const reasonLabel = (reason) => (reason ? REASON_LABELS[reason] ?? reason : '');

/** The default share scope when a deployment states none. Least privilege, and
 *  the same default the server applies. */
export const DEFAULT_SHARE = Object.freeze({
  mode: SHARE_MODES.PRIVATE,
  principals: Object.freeze([]),
  revocable: true,
});

/**
 * Check a decoded `portal-ops/1` object.
 *
 * @param {unknown} raw
 * @returns {{ok: boolean, errors: string[], ops: object|null}}
 */
export function validateOps(raw) {
  const errors = [];
  if (!isPlainObject(raw)) {
    return { ok: false, errors: ['the payload is not an object'], ops: null };
  }

  if (raw.schema_version !== OPS_SCHEMA) {
    errors.push(`unsupported schema_version: ${String(raw.schema_version)}`);
  }

  checkTransport(raw, errors);

  const scope = checkScope(raw.scope, errors);
  const subject = scope?.subject ?? null;
  const scopes = new Set(scope?.scopes ?? []);

  checkRegistry(raw.registry, subject, scopes, errors);
  checkKeys(raw.keys, errors);
  checkPlayground(raw.playground, errors);
  checkMetrics(raw.metrics, errors);
  checkTrends(raw.trends, raw.synthetic, scopes, errors);
  checkEvaluation(raw.evaluation, scopes, errors);
  checkAdmin(raw.admin, scopes, errors);
  checkLinks(raw.links, errors);

  if (!Array.isArray(raw.notes) || !raw.notes.length) {
    // The view must state its own contract. An ops payload with no notes is one
    // that has stopped explaining itself, which is how a demo starts looking
    // like production.
    errors.push('notes must be a non-empty list');
  }

  const ok = errors.length === 0;
  return { ok, errors, ops: ok ? normalise(raw) : null };
}

function checkTransport(raw, errors) {
  const transport = raw.transport;
  if (!isPlainObject(transport)) {
    errors.push('transport must be an object');
    return;
  }
  if (!Object.values(TRANSPORT_MODES).includes(transport.mode)) {
    errors.push(`transport.mode is not supported: ${String(transport.mode)}`);
    return;
  }
  if (transport.mode === TRANSPORT_MODES.LIVE && raw.synthetic === true) {
    errors.push('a synthetic payload may not claim to be live');
  }
  if (transport.mode === TRANSPORT_MODES.LIVE && transport.gateway_attached !== true) {
    errors.push('a live payload must state that the gateway is attached');
  }
}

function checkScope(scope, errors) {
  if (!isPlainObject(scope)) {
    errors.push('scope must be an object');
    return null;
  }
  if (!isNonEmptyString(scope.subject)) errors.push('scope.subject must be a non-empty string');
  if (!Array.isArray(scope.scopes) || !scope.scopes.every(isNonEmptyString)) {
    errors.push('scope.scopes must be a list of non-empty strings');
  }
  if (scope.roles !== undefined && !Array.isArray(scope.roles)) {
    errors.push('scope.roles must be a list when present');
  }
  if (!Object.values(VISIBILITY).includes(scope.visibility)) {
    errors.push('scope.visibility is not supported');
  }
  return scope;
}

// --- registry: owned deployments and endpoints shared *to* me ---------------

function checkShare(share, where, errors) {
  if (share === undefined || share === null) return; // absent ⇒ private, by default
  if (!isPlainObject(share)) {
    errors.push(`${where}.share must be an object`);
    return;
  }
  if (!Object.values(SHARE_MODES).includes(share.mode)) {
    errors.push(`${where}.share.mode is not supported: ${String(share.mode)}`);
    return;
  }
  if (share.principals !== undefined && !Array.isArray(share.principals)) {
    errors.push(`${where}.share.principals must be a list when present`);
    return;
  }
  if (share.mode === SHARE_MODES.SELECTED && !(share.principals ?? []).length) {
    errors.push(`${where}.share.mode is "selected" but names no principals`);
  }
  if (share.mode !== SHARE_MODES.PRIVATE && share.revocable !== true) {
    // A share that cannot be taken back is not a share, it is a leak.
    errors.push(`${where}.share.mode "${share.mode}" must be revocable`);
  }
  for (const principal of share.principals ?? []) {
    if (!isNonEmptyString(principal)) errors.push(`${where}.share.principals has an empty entry`);
  }
}

function checkResources(resources, where, errors) {
  if (resources === undefined || resources === null) return;
  if (!isPlainObject(resources)) {
    errors.push(`${where}.resources must be an object`);
    return;
  }
  const source = resources.source ?? null;
  if (source !== null && !isNonEmptyString(source)) {
    errors.push(`${where}.resources.source must be a non-empty string or null`);
  }
  const gpuNames = ['gpu_count', 'vram_total_mib', 'vram_used_mib', 'vram_free_mib'];
  for (const name of gpuNames) {
    if (!(name in resources)) continue;
    const value = numberOrNull(resources[name]);
    if (value === undefined) {
      errors.push(`${where}.resources.${name} must be null or a finite number`);
      continue;
    }
    if (value === null) continue;
    if (source !== GPU_METRIC_SOURCE) {
      // The one rule that exists so a plausible-looking VRAM figure can never
      // reach the screen: no NVML source, no GPU number.
      errors.push(
        `${where}.resources.${name} carries a value without the ${GPU_METRIC_SOURCE} source`,
      );
      continue;
    }
    if (!isNonEmptyString(resources.updated_at)) {
      errors.push(`${where}.resources.${name} carries a value without an updated_at`);
    }
  }
  if (resources.updated_at !== undefined && resources.updated_at !== null
      && !isNonEmptyString(resources.updated_at)) {
    errors.push(`${where}.resources.updated_at must be a non-empty string or null`);
  }
}

function checkForbidden(record, allowed, where, errors) {
  for (const field of Object.keys(record)) {
    if (!allowed.includes(field)) {
      errors.push(`${where} has an unexpected field ${field}`);
    }
    if (FORBIDDEN_CONTENT_FIELDS.includes(String(field).toLowerCase())) {
      errors.push(`${where} carries content this surface must never show: ${field}`);
    }
  }
}

const DEPLOYMENT_FIELDS = Object.freeze([
  'deployment_id', 'model_id', 'model_label', 'engine_type', 'owner_uid', 'owner_is_self',
  'health', 'runtime_status', 'registered', 'resources', 'share', 'links', 'updated_at',
  'health_reason', 'runtime_reason', 'owner_name',
]);

function checkDeployment(dep, index, subject, errors) {
  const where = `registry.owned[${index}]`;
  if (!isPlainObject(dep)) {
    errors.push(`${where} must be an object`);
    return;
  }
  checkForbidden(dep, DEPLOYMENT_FIELDS, where, errors);
  for (const field of ['deployment_id', 'model_id', 'owner_uid']) {
    if (!isNonEmptyString(dep[field])) errors.push(`${where}.${field} must be a non-empty string`);
  }
  if (dep.owner_is_self !== true) {
    errors.push(`${where} is in "我拥有的部署" but does not say owner_is_self`);
  }
  if (subject !== null && dep.owner_uid !== subject) {
    // The "mine" list is defined by principal equality with the session subject
    // — the same basis the call log uses. A row that disagrees is not my row.
    errors.push(`${where}.owner_uid is not this session's subject`);
  }
  if (!Object.values(HEALTH).includes(dep.health)) {
    errors.push(`${where}.health is not supported: ${String(dep.health)}`);
  }
  if (!Object.values(RUNTIME_STATUS).includes(dep.runtime_status)) {
    errors.push(`${where}.runtime_status is not supported: ${String(dep.runtime_status)}`);
  }
  if (dep.health === HEALTH.HEALTHY && dep.runtime_status !== RUNTIME_STATUS.RUNNING) {
    // "healthy" and "not running" cannot both be true. One of them is invented.
    errors.push(`${where} claims healthy while not running`);
  }
  if (dep.health === HEALTH.UNKNOWN && !isNonEmptyString(dep.health_reason)) {
    errors.push(`${where}.health is unknown but states no reason`);
  }
  if (dep.engine_type !== undefined && dep.engine_type !== null && !isNonEmptyString(dep.engine_type)) {
    errors.push(`${where}.engine_type must be a non-empty string or null`);
  }
  checkResources(dep.resources, where, errors);
  checkShare(dep.share, where, errors);
  checkLinkList(dep.links, `${where}.links`, errors);
}

const SHARED_FIELDS = Object.freeze([
  'endpoint_id', 'name', 'protocol', 'model', 'project', 'owner_uid', 'owner_is_self',
  'granted_by', 'share', 'links',
]);

function checkSharedEndpoint(item, index, subject, errors) {
  const where = `registry.shared[${index}]`;
  if (!isPlainObject(item)) {
    errors.push(`${where} must be an object`);
    return;
  }
  checkForbidden(item, SHARED_FIELDS, where, errors);
  for (const field of ['endpoint_id', 'name', 'owner_uid']) {
    if (!isNonEmptyString(item[field])) errors.push(`${where}.${field} must be a non-empty string`);
  }
  if (item.owner_is_self === true) {
    errors.push(`${where} is in "共享给我的 Endpoint" but claims to be mine`);
  }
  if (subject !== null && item.owner_uid === subject) {
    errors.push(`${where}.owner_uid is this session's own subject; it is not a shared endpoint`);
  }
  const share = item.share;
  if (!isPlainObject(share) || !Object.values(SHARE_MODES).includes(share.mode)) {
    errors.push(`${where}.share.mode is missing or unsupported`);
  } else if (share.mode === SHARE_MODES.PRIVATE) {
    errors.push(`${where} is shared with me but carries the private mode`);
  } else if (!isNonEmptyString(item.granted_by)) {
    errors.push(`${where}.granted_by must name who granted it`);
  }
  checkLinkList(item.links, `${where}.links`, errors);
}

/** A row in the deployment-wide view: `admin.principals` says who exists, this
 *  says what they have and how it is shared. Owner is deliberately *not*
 *  constrained to the session subject here — that is the whole point of the
 *  admin view — but the scope check below is what keeps it admin-only. */
const ALL_DEPLOYMENT_FIELDS = Object.freeze([
  'deployment_id', 'model_id', 'model_label', 'owner_uid', 'owner_name', 'health', 'share',
]);

function checkAllDeployment(row, index, errors) {
  const where = `registry.all[${index}]`;
  if (!isPlainObject(row)) {
    errors.push(`${where} must be an object`);
    return;
  }
  for (const field of Object.keys(row)) {
    if (!ALL_DEPLOYMENT_FIELDS.includes(field)) errors.push(`${where} has an unexpected field ${field}`);
    if (FORBIDDEN_CONTENT_FIELDS.includes(String(field).toLowerCase())) {
      errors.push(`${where} carries content this surface must never show: ${field}`);
    }
  }
  for (const field of ['deployment_id', 'model_id', 'owner_uid']) {
    if (!isNonEmptyString(row[field])) errors.push(`${where}.${field} must be a non-empty string`);
  }
  if (!Object.values(HEALTH).includes(row.health)) {
    errors.push(`${where}.health is not supported: ${String(row.health)}`);
  }
  checkShare(row.share, where, errors);
}

/** A registered inference service (Registry engine). `run_as_user` is the
 *  Linux user observed serving the port at registration — a recorded fact,
 *  never an authorisation input — and may be null when nothing was observed. */
const ENGINE_FIELDS = Object.freeze([
  'engine_id', 'name', 'protocol', 'framework', 'port', 'owner', 'owner_name', 'owner_is_self',
  'status', 'ownership_state', 'ownership_verified', 'run_as_user', 'run_as_uid',
  'runtime_build_id', 'last_seen_at',
]);
const ENGINE_STATUSES = Object.freeze(['registered', 'verified', 'unavailable']);

function checkEngine(row, index, subject, errors) {
  const where = `registry.engines[${index}]`;
  if (!isPlainObject(row)) {
    errors.push(`${where} must be an object`);
    return;
  }
  checkForbidden(row, ENGINE_FIELDS, where, errors);
  for (const field of ['engine_id', 'name', 'owner']) {
    if (!isNonEmptyString(row[field])) errors.push(`${where}.${field} must be a non-empty string`);
  }
  if (!ENGINE_STATUSES.includes(row.status)) {
    errors.push(`${where}.status is not supported: ${String(row.status)}`);
  }
  if (subject !== null && (row.owner_is_self === true) !== (row.owner === subject)) {
    errors.push(`${where}.owner_is_self disagrees with the session subject`);
  }
  if (row.run_as_user !== undefined && row.run_as_user !== null && !isNonEmptyString(row.run_as_user)) {
    errors.push(`${where}.run_as_user must be a non-empty string or null`);
  }
}

const MODEL_FIELDS = Object.freeze([
  'model_id', 'name', 'format', 'parameter_count', 'description', 'owner', 'owner_name',
  'owner_is_self', 'status', 'weight_path', 'created_at',
]);

function checkModel(row, index, subject, errors) {
  const where = `registry.models[${index}]`;
  if (!isPlainObject(row)) {
    errors.push(`${where} must be an object`);
    return;
  }
  checkForbidden(row, MODEL_FIELDS, where, errors);
  for (const field of ['model_id', 'name', 'owner']) {
    if (!isNonEmptyString(row[field])) errors.push(`${where}.${field} must be a non-empty string`);
  }
  const mine = subject !== null && row.owner === subject;
  if (subject !== null && (row.owner_is_self === true) !== mine) {
    errors.push(`${where}.owner_is_self disagrees with the session subject`);
  }
  if (!mine && row.weight_path !== undefined && row.weight_path !== null) {
    // A weight path names the owner's directories; only the owner may see it.
    errors.push(`${where} carries another principal's weight path`);
  }
}

/** A runtime build — the engine library. A build is a **file** (an executable,
 *  a Python distribution or a source tree); a service is a process. The path
 *  and the venv name the owner's directory structure, so a non-owner row may
 *  carry neither: it gets `binary_path: null` plus a fingerprint instead, and
 *  `binary_path_redacted` must say so rather than letting "no path" read as
 *  "this build has no file". */
const BUILD_FIELDS = Object.freeze([
  'build_id', 'framework', 'version', 'found_as', 'binary_path', 'binary_path_sha256',
  'binary_path_redacted', 'env', 'formats', 'owner', 'owner_name', 'owner_is_self',
  'state', 'last_seen_at',
]);
const BUILD_FOUND_AS = Object.freeze(['executable', 'python_package', 'source_tree']);
const BUILD_STATES = Object.freeze(['present', 'missing']);

function checkBuild(row, index, subject, errors) {
  const where = `registry.builds[${index}]`;
  if (!isPlainObject(row)) {
    errors.push(`${where} must be an object`);
    return;
  }
  checkForbidden(row, BUILD_FIELDS, where, errors);
  for (const field of ['build_id', 'framework', 'owner']) {
    if (!isNonEmptyString(row[field])) errors.push(`${where}.${field} must be a non-empty string`);
  }
  const mine = subject !== null && row.owner === subject;
  if (subject !== null && (row.owner_is_self === true) !== mine) {
    errors.push(`${where}.owner_is_self disagrees with the session subject`);
  }
  if (!mine) {
    if (row.binary_path !== undefined && row.binary_path !== null) {
      errors.push(`${where} carries another principal's binary path`);
    }
    if (row.env !== undefined && row.env !== null) {
      errors.push(`${where} names another principal's virtual environment`);
    }
  }
  if (row.found_as !== undefined && row.found_as !== null && !BUILD_FOUND_AS.includes(row.found_as)) {
    errors.push(`${where}.found_as is not supported: ${String(row.found_as)}`);
  }
  if (row.state !== undefined && row.state !== null && !BUILD_STATES.includes(row.state)) {
    errors.push(`${where}.state is not supported: ${String(row.state)}`);
  }
}

function checkRegistry(registry, subject, scopes, errors) {
  if (!isPlainObject(registry)) {
    errors.push('registry must be an object');
    return;
  }
  if (!Array.isArray(registry.owned)) {
    errors.push('registry.owned must be a list');
  } else {
    registry.owned.forEach((dep, index) => checkDeployment(dep, index, subject, errors));
  }
  if (!Array.isArray(registry.shared)) {
    errors.push('registry.shared must be a list');
  } else {
    registry.shared.forEach((item, index) => checkSharedEndpoint(item, index, subject, errors));
  }
  if (registry.all !== undefined && registry.all !== null) {
    if (!Array.isArray(registry.all)) {
      errors.push('registry.all must be a list');
    } else {
      if (registry.all.length && !scopes.has('portal:admin:read')) {
        // The deployment-wide view is a scope, not a client choice. A payload
        // that offers it to a session without the scope is refused rather than
        // rendered and then regretted.
        errors.push('registry.all is present but this session lacks portal:admin:read');
      }
      registry.all.forEach((row, index) => checkAllDeployment(row, index, errors));
    }
  }
  if (registry.models !== undefined && registry.models !== null) {
    if (!Array.isArray(registry.models)) errors.push('registry.models must be a list');
    else registry.models.forEach((row, index) => checkModel(row, index, subject, errors));
  }
  if (registry.engines !== undefined && registry.engines !== null) {
    if (!Array.isArray(registry.engines)) errors.push('registry.engines must be a list');
    else registry.engines.forEach((row, index) => checkEngine(row, index, subject, errors));
  }
  if (registry.builds !== undefined && registry.builds !== null) {
    if (!Array.isArray(registry.builds)) errors.push('registry.builds must be a list');
    else registry.builds.forEach((row, index) => checkBuild(row, index, subject, errors));
  }
  if (registry.adapter_attached !== undefined && typeof registry.adapter_attached !== 'boolean') {
    errors.push('registry.adapter_attached must be a boolean when present');
  }
  if (registry.adapter_attached === false && !isNonEmptyString(registry.reason)) {
    errors.push('registry with no attached adapter must state a reason');
  }
}

// --- keys -------------------------------------------------------------------

const KEY_FIELDS = Object.freeze([
  'key_id', 'name', 'prefix', 'scopes', 'status', 'created_at', 'last_used_at', 'endpoint',
]);

/** A masked prefix, e.g. `llk_••••8m`. Rejecting anything else is how "the page
 *  shows a key prefix" stays true without the page ever being able to show a
 *  key: a full key has no mask character in it. */
export const isMaskedPrefix = (prefix) =>
  typeof prefix === 'string'
  && prefix.length > 0
  && prefix.length <= 24
  && /[•*·]/.test(prefix);

function checkKeys(keys, errors) {
  if (!Array.isArray(keys)) {
    errors.push('keys must be a list');
    return;
  }
  keys.forEach((key, index) => {
    const where = `keys[${index}]`;
    if (!isPlainObject(key)) {
      errors.push(`${where} must be an object`);
      return;
    }
    for (const field of Object.keys(key)) {
      if (!KEY_FIELDS.includes(field)) errors.push(`${where} has an unexpected field ${field}`);
      if (FORBIDDEN_KEY_FIELDS.includes(String(field).toLowerCase())) {
        errors.push(`${where} carries ${field}, which must never leave the server`);
      }
    }
    for (const field of ['key_id', 'name', 'prefix']) {
      if (!isNonEmptyString(key[field])) errors.push(`${where}.${field} must be a non-empty string`);
    }
    if (isNonEmptyString(key.prefix) && !isMaskedPrefix(key.prefix)) {
      errors.push(`${where}.prefix is not masked; a key prefix must contain a mask character`);
    }
    if (!Array.isArray(key.scopes) || !key.scopes.every(isNonEmptyString)) {
      errors.push(`${where}.scopes must be a list of non-empty strings`);
    }
    if (!['active', 'revoked', 'expired'].includes(key.status)) {
      errors.push(`${where}.status is not supported: ${String(key.status)}`);
    }
  });
}

// --- playground -------------------------------------------------------------

function checkPlayground(playground, errors) {
  if (!isPlainObject(playground)) {
    errors.push('playground must be an object');
    return;
  }
  if (typeof playground.authorized !== 'boolean') {
    errors.push('playground.authorized must be a boolean');
    return;
  }
  if (playground.authorized) {
    if (!isNonEmptyString(playground.backend)) {
      errors.push('an authorized playground must name its backend');
    }
    if (playground.audited !== true) {
      errors.push('an authorized playground must be audited');
    }
  } else {
    if (playground.backend !== null && playground.backend !== undefined) {
      errors.push('an unauthorized playground must not name a backend');
    }
    if (!isNonEmptyString(playground.reason)) {
      errors.push('an unauthorized playground must state a reason');
    }
  }
  // Never a direct upstream target, whatever else the payload says.
  if (playground.endpoint !== null && playground.endpoint !== undefined
      && !isInternalHref(playground.endpoint)) {
    errors.push(`playground.endpoint is not a same-origin path: ${String(playground.endpoint)}`);
  }
}

// --- metrics ----------------------------------------------------------------

// This is an additive, optional portal-ops/1 metrics-level extension. The BFF
// derives each deadline from its authoritative metric catalogue, not a browser
// copy of those TTLs. An unleased LIVE number must fail closed.
const ISO_WITH_ZONE = /(?:Z|[+-]\d{2}:\d{2})$/;
export function parseOpsExpiry(value) {
  if (typeof value !== 'string' || !ISO_WITH_ZONE.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

export function opsMetricsForDisplay(ops, nowMs = Date.now(), expiredNames = new Set()) {
  if (!ops || ops.transport?.mode !== TRANSPORT_MODES.LIVE
    || !Array.isArray(ops.metrics?.items)) return ops;
  const leases = ops.metrics.expires_at ?? {};
  let changed = false;
  const items = ops.metrics.items.map((metric) => {
    if (metric.value === null || metric.value === undefined) return metric;
    const expiry = Object.prototype.hasOwnProperty.call(leases, metric.name)
      ? parseOpsExpiry(leases[metric.name]) : null;
    if (expiry !== null && expiry > nowMs && !expiredNames.has(metric.name)
      && Number.isFinite(nowMs)) return metric;
    changed = true;
    expiredNames.add(metric.name);
    return { ...metric, value: null, state: METRIC_STATES.UNKNOWN,
      source: null, updated_at: null, reason: 'stale_sample' };
  });
  return changed ? { ...ops, metrics: { ...ops.metrics, items } } : ops;
}

/** Only future deadlines; expired metrics have already been masked on paint. */
export function nextOpsMetricExpiry(ops, nowMs = Date.now(), expiredNames = new Set()) {
  if (!ops || ops.transport?.mode !== TRANSPORT_MODES.LIVE
    || !Array.isArray(ops.metrics?.items)) return null;
  const leases = ops.metrics.expires_at ?? {};
  let soonest = Infinity;
  for (const metric of ops.metrics.items) {
    if (metric.value === null || metric.value === undefined || expiredNames.has(metric.name)) continue;
    const expiry = Object.prototype.hasOwnProperty.call(leases, metric.name)
      ? parseOpsExpiry(leases[metric.name]) : null;
    if (expiry !== null && expiry > nowMs && expiry < soonest) soonest = expiry;
  }
  return Number.isFinite(soonest) ? soonest : null;
}

const METRIC_FIELDS = Object.freeze([
  'name', 'label', 'unit', 'value', 'state', 'source', 'reason', 'updated_at', 'kind',
]);

export const isGpuMetric = (name) => String(name ?? '').startsWith(GPU_METRIC_PREFIX);

function checkMetric(metric, index, errors) {
  const where = `metrics.items[${index}]`;
  if (!isPlainObject(metric)) {
    errors.push(`${where} must be an object`);
    return;
  }
  for (const field of Object.keys(metric)) {
    if (!METRIC_FIELDS.includes(field)) errors.push(`${where} has an unexpected field ${field}`);
  }
  if (!isNonEmptyString(metric.name)) errors.push(`${where}.name must be a non-empty string`);
  const value = numberOrNull(metric.value);
  if (value === undefined) {
    errors.push(`${where}.value must be null or a finite number`);
    return;
  }
  if (value === null) {
    if (metric.state !== METRIC_STATES.UNKNOWN) {
      errors.push(`metric ${metric.name} has no value and must be unknown`);
    }
    if (!isNonEmptyString(metric.reason)) {
      errors.push(`metric ${metric.name} has no value and must say why`);
    }
    return;
  }
  // A number has to be defensible: a declared state, a source, and a time.
  if (metric.state !== METRIC_STATES.AVAILABLE) {
    errors.push(`metric ${metric.name} has a value and must be available`);
  }
  if (!isNonEmptyString(metric.source)) {
    errors.push(`metric ${metric.name} carries a value without a source`);
  } else if (!VERIFIED_METRIC_SOURCES.includes(metric.source)) {
    errors.push(`metric ${metric.name} cites an unverified source ${metric.source}`);
  }
  if (!isNonEmptyString(metric.updated_at)) {
    errors.push(`metric ${metric.name} carries a value without an updated_at`);
  }
  if (isGpuMetric(metric.name) && metric.source !== GPU_METRIC_SOURCE) {
    errors.push(`GPU metric ${metric.name} may carry a value only from ${GPU_METRIC_SOURCE}`);
  }
}

function checkMetrics(metrics, errors) {
  if (!isPlainObject(metrics)) {
    errors.push('metrics must be an object');
    return;
  }
  if (!Array.isArray(metrics.items)) {
    errors.push('metrics.items must be a list');
    return;
  }
  metrics.items.forEach((metric, index) => checkMetric(metric, index, errors));
  // Public contract addendum: optional map of metric name -> authoritative
  // expiry instant. Missing map is accepted for DEMO/legacy shape, but the
  // *renderer* withholds any unleased numeric LIVE metric.
  if (metrics.expires_at !== undefined) {
    if (!isPlainObject(metrics.expires_at)) {
      errors.push('metrics.expires_at must be a name-to-ISO-time object');
    } else {
      const names = new Set(metrics.items.filter(isPlainObject).map((m) => m.name));
      for (const [name, stamp] of Object.entries(metrics.expires_at)) {
        if (!names.has(name)) errors.push('metrics.expires_at has an unknown metric');
        if (parseOpsExpiry(stamp) === null) {
          errors.push('metrics.expires_at has an invalid timezone-aware timestamp');
        }
      }
    }
  }
  for (const field of ['my_source', 'global_source', 'window']) {
    const value = metrics[field];
    if (value !== undefined && value !== null && !isNonEmptyString(value)) {
      errors.push(`metrics.${field} must be a non-empty string or null`);
    }
  }
}

// --- trend series -----------------------------------------------------------

/** Series kinds the observability view knows how to draw. */
export const TREND_KINDS = Object.freeze(['requests', 'tokens', 'latency']);

const TREND_SERIES_FIELDS = Object.freeze([
  'name', 'kind', 'scope', 'unit', 'points', 'p50', 'p95',
]);

const TRENDS_FIELDS = Object.freeze([
  'window', 'synthetic', 'source', 'updated_at', 'series', 'reason',
]);

function checkTrends(trends, payloadSynthetic, scopes, errors) {
  if (trends === undefined || trends === null) return;
  if (!isPlainObject(trends)) {
    errors.push('trends must be an object');
    return;
  }
  for (const field of Object.keys(trends)) {
    if (!TRENDS_FIELDS.includes(field)) errors.push(`trends has an unexpected field ${field}`);
  }
  if (typeof trends.synthetic !== 'boolean') {
    errors.push('trends.synthetic must be a boolean');
    return;
  }
  if (trends.synthetic) {
    // The narrow rule that keeps a drawn curve from becoming a claim: a series
    // this build made up may only exist on a payload that already admits to
    // being synthetic. There is no path from "fixture" to "looks like telemetry".
    if (payloadSynthetic !== true) {
      errors.push('a synthetic trend series may not ride on a non-synthetic payload');
    }
    if (!isNonEmptyString(trends.reason)) {
      errors.push('a synthetic trend series must state that it is synthetic');
    }
  } else {
    if (!VERIFIED_METRIC_SOURCES.includes(trends.source)) {
      errors.push(`trends cite an unverified source ${String(trends.source)}`);
    }
    if (!isNonEmptyString(trends.updated_at)) {
      errors.push('a non-synthetic trend series must carry an updated_at');
    }
  }
  if (!isNonEmptyString(trends.window)) errors.push('trends.window must be a non-empty string');

  if (!Array.isArray(trends.series)) {
    errors.push('trends.series must be a list');
    return;
  }
  trends.series.forEach((series, index) => {
    const where = `trends.series[${index}]`;
    if (!isPlainObject(series)) {
      errors.push(`${where} must be an object`);
      return;
    }
    for (const field of Object.keys(series)) {
      if (!TREND_SERIES_FIELDS.includes(field)) errors.push(`${where} has an unexpected field ${field}`);
    }
    if (!isNonEmptyString(series.name)) errors.push(`${where}.name must be a non-empty string`);
    if (!TREND_KINDS.includes(series.kind)) {
      errors.push(`${where}.kind is not supported: ${String(series.kind)}`);
    }
    if (!['mine', 'global'].includes(series.scope)) {
      errors.push(`${where}.scope must be "mine" or "global"`);
    }
    if (series.scope === 'global' && !scopes.has('portal:admin:read')) {
      errors.push(`${where} is a global series but this session lacks portal:admin:read`);
    }
    if (!Array.isArray(series.points)) {
      errors.push(`${where}.points must be a list`);
      return;
    }
    series.points.forEach((point, pi) => {
      // `null` is a gap in the record, not a zero. A chart that drew it as 0
      // would invent a quiet period that never happened.
      if (point === null) return;
      if (numberOrNull(point) === undefined) {
        errors.push(`${where}.points[${pi}] must be null or a finite number`);
      }
    });
  });
}

// --- evaluation -------------------------------------------------------------

const EVAL_RUN_FIELDS = Object.freeze([
  'run_id', 'scope', 'model_id', 'dataset', 'status', 'metric', 'metric_unit',
  'created_at', 'updated_at', 'reason',
]);

function checkEvalRun(run, index, scopes, errors) {
  const where = `evaluation.runs[${index}]`;
  if (!isPlainObject(run)) {
    errors.push(`${where} must be an object`);
    return;
  }
  for (const field of Object.keys(run)) {
    if (!EVAL_RUN_FIELDS.includes(field)) errors.push(`${where} has an unexpected field ${field}`);
  }
  if (!isNonEmptyString(run.run_id)) errors.push(`${where}.run_id must be a non-empty string`);
  if (!['mine', 'global'].includes(run.scope)) {
    errors.push(`${where}.scope must be "mine" or "global"`);
  }
  if (run.scope === 'global' && !scopes.has('portal:admin:read')) {
    // A deployment-wide row on a session that cannot read the deployment view.
    errors.push(`${where} is a global eval run but this session lacks portal:admin:read`);
  }
  if (!Object.values(EVAL_STATUS).includes(run.status)) {
    errors.push(`${where}.status is not supported: ${String(run.status)}`);
  }
  const metric = numberOrNull(run.metric);
  if (metric === undefined) {
    errors.push(`${where}.metric must be null or a finite number`);
  } else if (run.status !== EVAL_STATUS.DONE && metric !== null) {
    // A score for a run that has not finished is a number someone made up.
    errors.push(`${where} carries a metric while its status is ${run.status}`);
  }
}

function checkEvaluation(evaluation, scopes, errors) {
  if (!isPlainObject(evaluation)) {
    errors.push('evaluation must be an object');
    return;
  }
  if (!Array.isArray(evaluation.runs)) {
    errors.push('evaluation.runs must be a list');
  } else {
    evaluation.runs.forEach((run, index) => checkEvalRun(run, index, scopes, errors));
  }
  const submit = evaluation.submit;
  if (!isPlainObject(submit)) {
    errors.push('evaluation.submit must be an object');
    return;
  }
  if (typeof submit.ready !== 'boolean') {
    errors.push('evaluation.submit.ready must be a boolean');
    return;
  }
  if (!submit.ready && !isNonEmptyString(submit.reason)) {
    errors.push('a not-ready eval submit must state a reason');
  }
}

// --- admin ------------------------------------------------------------------

const PRINCIPAL_FIELDS = Object.freeze(['uid', 'display_name', 'roles', 'scopes']);

function checkAdmin(admin, scopes, errors) {
  if (!isPlainObject(admin)) {
    errors.push('admin must be an object');
    return;
  }
  if (typeof admin.available !== 'boolean') {
    errors.push('admin.available must be a boolean');
    return;
  }
  if (admin.available && !scopes.has('portal:admin:read')) {
    // The whole point of the admin view: it is a scope, not a name and not a
    // client-side choice. A payload that offers it without the scope is refused
    // rather than rendered and then regretted.
    errors.push('admin.available is true but this session lacks portal:admin:read');
  }
  if (!admin.available && !isNonEmptyString(admin.reason)) {
    errors.push('an unavailable admin view must state a reason');
  }
  if (!Array.isArray(admin.principals)) {
    errors.push('admin.principals must be a list');
    return;
  }
  admin.principals.forEach((principal, index) => {
    const where = `admin.principals[${index}]`;
    if (!isPlainObject(principal)) {
      errors.push(`${where} must be an object`);
      return;
    }
    for (const field of Object.keys(principal)) {
      if (!PRINCIPAL_FIELDS.includes(field)) errors.push(`${where} has an unexpected field ${field}`);
    }
    if (!isNonEmptyString(principal.uid)) errors.push(`${where}.uid must be a non-empty string`);
    if (principal.roles !== undefined && !Array.isArray(principal.roles)) {
      errors.push(`${where}.roles must be a list when present`);
    }
    if (principal.scopes !== undefined && !Array.isArray(principal.scopes)) {
      errors.push(`${where}.scopes must be a list when present`);
    }
  });
  if (!admin.available && admin.principals.length) {
    errors.push('an unavailable admin view must not list principals');
  }
}

// --- links ------------------------------------------------------------------

function checkLink(link, where, errors) {
  if (!isPlainObject(link)) {
    errors.push(`${where} must be an object`);
    return;
  }
  if (!isNonEmptyString(link.label)) errors.push(`${where}.label must be a non-empty string`);
  if (link.href === null || link.href === undefined) {
    // A blocked entry has no address on purpose: it explains, it does not fail.
    if (!isNonEmptyString(link.requires)) {
      errors.push(`${where} is blocked but names no requirement`);
    }
    return;
  }
  if (!isInternalHref(link.href)) {
    errors.push(`${where}.href is not a same-origin path: ${String(link.href)}`);
  }
}

function checkLinkList(links, where, errors) {
  if (links === undefined || links === null) return;
  if (!Array.isArray(links)) {
    errors.push(`${where} must be a list`);
    return;
  }
  links.forEach((link, index) => checkLink(link, `${where}[${index}]`, errors));
}

function checkLinks(links, errors) {
  checkLinkList(links, 'links', errors);
}

/**
 * Fill in what the check treats as defaulted but the renderer would otherwise
 * have to re-derive: an absent share block is the private default, and an
 * absent link list is empty.
 */
function normalise(raw) {
  const share = (block) =>
    isPlainObject(block) && Object.values(SHARE_MODES).includes(block.mode)
      ? {
        mode: block.mode,
        principals: [...(block.principals ?? [])],
        revocable: block.revocable === true,
      }
      : { ...DEFAULT_SHARE, principals: [] };

  return {
    ...raw,
    registry: {
      ...raw.registry,
      owned: (raw.registry.owned ?? []).map((dep) => ({
        ...dep,
        links: [...(dep.links ?? [])],
        share: share(dep.share),
      })),
      shared: (raw.registry.shared ?? []).map((item) => ({
        ...item,
        links: [...(item.links ?? [])],
        share: share(item.share),
      })),
      all: (raw.registry.all ?? []).map((row) => ({ ...row, share: share(row.share) })),
      engines: (raw.registry.engines ?? []).map((row) => ({ ...row })),
      models: (raw.registry.models ?? []).map((row) => ({ ...row })),
    },
    links: [...(raw.links ?? [])],
    trends: raw.trends
      ? { ...raw.trends, series: (raw.trends.series ?? []).map((s) => ({ ...s, points: [...s.points] })) }
      : null,
  };
}

/** A page state that carries no data — used for `unknown`, `denied` and
 *  `signed_out`. Nothing is fabricated to fill it in. */
export function emptyOps(state, reason) {
  return {
    state,
    reason: reason ?? null,
    transport: null,
    scope: null,
    registry: null,
    keys: [],
    playground: null,
    metrics: null,
    trends: null,
    evaluation: null,
    admin: null,
    links: [],
    notes: [],
  };
}

/** Which page state a validated payload represents. A declared state wins: a
 *  403 page must not be relabelled as "no deployments". */
export function stateFor(ops) {
  if (!ops) return OPS_STATES.UNKNOWN;
  const declared = Object.values(OPS_STATES).includes(ops.state) ? ops.state : null;
  return declared && declared !== OPS_STATES.READY ? declared : OPS_STATES.READY;
}

export const isDemo = (ops) => ops?.transport?.mode === TRANSPORT_MODES.DEMO;

/** The share scope a deployment is actually in, with the private default
 *  applied. Exported so the renderer and the tests agree on one answer. */
export const effectiveShare = (record) => {
  const block = record?.share;
  if (!block || !Object.values(SHARE_MODES).includes(block.mode)) {
    return { ...DEFAULT_SHARE, principals: [] };
  }
  return {
    mode: block.mode,
    principals: [...(block.principals ?? [])],
    revocable: block.revocable === true,
  };
};

/** `true` when the share block would let `principal` reach the deployment. */
export function shareAllows(record, principal) {
  const block = effectiveShare(record);
  if (block.mode === SHARE_MODES.ALL) return true;
  if (block.mode === SHARE_MODES.SELECTED) return block.principals.includes(principal);
  return false;
}
