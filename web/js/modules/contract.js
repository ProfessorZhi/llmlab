/*! web/js/modules/contract.js — the `GET /api/atlas/modules` payload, checked before it is shown.
 *
 *  This is the browser-side half of `backend/portal/modules.py`. It holds no DOM
 *  and no network, so it can be unit-tested under plain Node.
 *
 *  Why validate again, when the server already ran `validate_modules_dto`?
 *  Because the page must not be the one place where a claim goes unchecked, and
 *  because these particular claims are the ones a reader would act on. Four
 *  things are refused here rather than rendered:
 *
 *  1. **A link that leaves the origin**, or that names an upstream port. The
 *     whole point of the server-side rule is that the browser is never handed a
 *     way around the Gateway; a payload that contains one is refused outright
 *     rather than rendered with the address stripped, so the violation is
 *     visible instead of silently repaired.
 *  2. **A GPU number.** No NVML source, no number — re-checked here so a
 *     fabricated value cannot be displayed even if it reached the wire.
 *  3. **An `available` module with nothing to show**, or one whose scopes the
 *     session does not hold. `available` has to mean something.
 *  4. **A live claim on synthetic data.**
 *
 *  Missing values stay `null` all the way to the panel (rendered as `—`), never
 *  `0`: "not measured" and "measured zero" are different facts.
 */

/** The only schema this client understands. */
export const PORTAL_SCHEMA = 'portal-modules/1';

export const MODULE_STATES = Object.freeze({
  AVAILABLE: 'available',
  UNAVAILABLE: 'unavailable',
  DISABLED: 'disabled',
  UNKNOWN: 'unknown',
});

export const VISIBILITY = Object.freeze({ PRIVATE: 'private', SHARED: 'shared' });

export const TRANSPORT_MODES = Object.freeze({ DEMO: 'demo', LIVE: 'live' });

/** Page states, mapped 1:1 onto what the server answered. */
export const PORTAL_STATES = Object.freeze({
  READY: 'ready',
  UNKNOWN: 'unknown',
  DENIED: 'denied',
  SIGNED_OUT: 'signed_out',
  // The route is absent from this deployment (404/501): not an answer about data.
  NOT_CONFIGURED: 'not_configured',
});

/** Transport statuses that are answers about *access*, not about data. */
export const STATE_CHANGING_STATUSES = Object.freeze([401, 403]);

export const UPSTREAM_PORT_TOKEN = ':8080';

/** Reasons the server gives, in the reader's language. A reason this build does
 *  not know is still shown — as the raw token — never swallowed. */
const REASON_LABELS = Object.freeze({
  no_authenticated_session: '会话无效或缺失',
  missing_required_scope: '缺少所需权限（scope）',
  engine_is_not_strata: '当前引擎不是 Strata',
  gateway_not_attached: 'Gateway 未接线',
  not_configured_in_this_deployment: '本次部署未配置该项',
  no_authorized_backend: '没有经授权的后端',
  no_verified_metric_source: '没有已声明的可信数据来源',
  preview_build_has_no_real_backend: '预览构建没有真实推理后端（合成输出不等于试跑）',
});

/** Preconditions the server evaluates, in the reader's language. */
const FACT_LABELS = Object.freeze({
  engine_is_strata: '引擎为 Strata',
  gateway_attached: 'Gateway 已接线',
  grafana_configured: 'Grafana 已配置',
  prometheus_configured: 'Prometheus 已配置',
  evalscope_configured: 'EvalScope 已配置',
  has_gpu_source: '有已声明的 NVML 来源',
  playground_backend_authorized: '有经授权的交互式推理后端',
});

export const reasonLabel = (reason) => (reason ? REASON_LABELS[reason] ?? reason : '');
export const factLabel = (name) => FACT_LABELS[name] ?? name;

/** A link the server declined to open: it carries the reason, never an address. */
export const isBlockedLink = (link) => !link?.href;

const isPlainObject = (value) =>
  !!value && typeof value === 'object' && !Array.isArray(value);

/** Is this href a same-origin path with no upstream port anywhere in it? */
export function isInternalHref(href) {
  if (typeof href !== 'string' || !href.startsWith('/')) return false;
  if (href.startsWith('//')) return false;
  if (href.includes('://')) return false;
  if (href.includes(UPSTREAM_PORT_TOKEN)) return false;
  return true;
}

const finiteOrNull = (value) =>
  value === null || value === undefined ? null : (Number.isFinite(value) ? value : null);

function checkModule(module, scopes, errors, index) {
  const id = module?.id ?? `#${index}`;
  if (!isPlainObject(module)) {
    errors.push(`modules[${index}] must be an object`);
    return;
  }
  if (!Object.values(MODULE_STATES).includes(module.state)) {
    errors.push(`module ${id} has an unsupported state: ${String(module.state)}`);
  }
  if (module.state === MODULE_STATES.AVAILABLE) {
    const declared = Array.isArray(module.requires_scopes) ? module.requires_scopes : [];
    const missing = declared.filter((scope) => !scopes.has(scope));
    if (missing.length) {
      errors.push(`module ${id} claims available without ${missing.join(', ')}`);
    }
    const hasSomething =
      (Array.isArray(module.metrics) && module.metrics.length > 0) ||
      (Array.isArray(module.links) && module.links.length > 0) ||
      (isPlainObject(module.data) && Object.keys(module.data).length > 0);
    if (!hasSomething) errors.push(`module ${id} claims available with nothing to show`);
  } else if (!module.reason) {
    errors.push(`module ${id} is not available but states no reason`);
  }

  for (const metric of module.metrics ?? []) {
    const value = finiteOrNull(metric.value);
    if (value === null) {
      if (metric.state !== MODULE_STATES.UNKNOWN) {
        errors.push(`metric ${metric.name} has no value and must be unknown`);
      }
    } else if (metric.state !== MODULE_STATES.AVAILABLE || !metric.source) {
      errors.push(`metric ${metric.name} has a value without state/source`);
    }
    if (String(metric.name ?? '').startsWith('gpu_') && value !== null) {
      errors.push(`GPU metric ${metric.name} carries a value, which this build never has`);
    }
  }

  for (const link of module.links ?? []) {
    if (isBlockedLink(link)) {
      if (!link.requires) errors.push(`a blocked link in ${id} names no requirement`);
      continue;
    }
    if (!isInternalHref(link.href)) {
      errors.push(`link ${link.href} is not a same-origin path`);
    }
  }
}

/**
 * Check a decoded `data.portal` object.
 *
 * @param {unknown} raw
 * @returns {{ok: boolean, errors: string[], portal: object|null}}
 */
export function validatePortal(raw) {
  const errors = [];
  if (!isPlainObject(raw)) {
    return { ok: false, errors: ['the payload is not an object'], portal: null };
  }
  if (raw.schema_version !== PORTAL_SCHEMA) {
    errors.push(`unsupported schema_version: ${String(raw.schema_version)}`);
  }

  const transport = raw.transport;
  if (!isPlainObject(transport)) {
    errors.push('transport must be an object');
  } else if (!Object.values(TRANSPORT_MODES).includes(transport.mode)) {
    errors.push(`transport.mode is not supported: ${String(transport.mode)}`);
  } else if (transport.mode === TRANSPORT_MODES.LIVE && raw.synthetic === true) {
    errors.push('a synthetic payload may not claim to be live');
  } else if (transport.mode === TRANSPORT_MODES.LIVE && transport.gateway_attached !== true) {
    errors.push('a live payload must state that the gateway is attached');
  }

  const scope = raw.scope;
  const scopes = new Set();
  if (!isPlainObject(scope) || !Array.isArray(scope.scopes) || !scope.subject) {
    errors.push('scope must name a subject and its scopes');
  } else {
    scope.scopes.forEach((value) => scopes.add(value));
    if (!Object.values(VISIBILITY).includes(scope.visibility)) {
      errors.push('scope.visibility is not supported');
    }
  }

  if (!Array.isArray(raw.modules) || !raw.modules.length) {
    errors.push('modules must be a non-empty list');
  } else {
    raw.modules.forEach((module, index) => checkModule(module, scopes, errors, index));
  }

  const endpoints = raw.endpoints;
  if (!isPlainObject(endpoints)) {
    errors.push('endpoints must be an object');
  } else {
    if (!Object.values(VISIBILITY).includes(endpoints.visibility)) {
      errors.push('endpoints.visibility is not supported');
    }
    if (typeof endpoints.can_publish_shared !== 'boolean') {
      errors.push('endpoints.can_publish_shared must be a boolean');
    }
    for (const item of endpoints.items ?? []) {
      if (
        item.scope === VISIBILITY.SHARED &&
        endpoints.visibility !== VISIBILITY.SHARED
      ) {
        errors.push('a shared endpoint leaked into a private view');
      }
    }
  }

  const ok = errors.length === 0;
  return { ok, errors, portal: ok ? raw : null };
}

/** Find one module by id. */
export const moduleById = (portal, moduleId) =>
  (portal?.modules ?? []).find((module) => module.id === moduleId) ?? null;

/** A page state that carries no data. The module list is empty by construction. */
export function emptyPortal(state, reason) {
  return { state, reason: reason ?? null, transport: null, scope: null, modules: [], endpoints: null };
}

/** Which page state a validated payload represents. A declared state wins: a
 *  403 page must not be relabelled as "no modules". */
export function stateFor(portal) {
  if (!portal) return PORTAL_STATES.UNKNOWN;
  const declared = Object.values(PORTAL_STATES).includes(portal.state) ? portal.state : null;
  return declared && declared !== PORTAL_STATES.READY ? declared : PORTAL_STATES.READY;
}

/** The readiness checklist for one module, as `{name, label, met}` rows. */
export function requirements(module, deployment, scopes) {
  const facts = deployment?.facts ?? {};
  const rows = [];
  for (const scope of module?.requires_scopes ?? []) {
    rows.push({ kind: 'scope', name: scope, label: scope, met: scopes.has(scope) });
  }
  for (const name of module?.requires ?? []) {
    rows.push({ kind: 'fact', name, label: factLabel(name), met: facts[name] === true });
  }
  return rows;
}

export const isDemo = (portal) => portal?.transport?.mode === TRANSPORT_MODES.DEMO;
