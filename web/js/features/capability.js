/*! web/js/features/capability.js — what this session may do, and nothing else.
 *
 *  The ops surface is the first place in this build where the *buttons* matter:
 *  a share control that publishes, an eval submit, an admin-only view. So the
 *  question "may this session do X?" has to be answered in exactly one place,
 *  from exactly one kind of input.
 *
 *  That input is the **scope list the server asserted for this session** — the
 *  same vocabulary `backend/portal/modules.py` already uses, re-declared here so
 *  a typo cannot silently match. Two things are deliberately *not* inputs:
 *
 *  1. **Roles.** `role: admin` grants nothing. A role is a label an operator
 *     typed into a table; a scope is a decision the server made about this
 *     request. Only one of those is safe to branch on, and it is not the role.
 *     The backend already takes this position (`resolve_module_scope`: "role 不
 *     产生任何权限"), and a front-end that quietly reintroduced the role
 *     shortcut would undo it — so `deriveCapabilities` accepts `roles` and
 *     provably ignores it (see `tests/web-features/capability.test.js`).
 *  2. **Names.** No operator's account name appears anywhere in this directory,
 *     and `tests/web-features/capability.test.js` reads every file here and
 *     fails if one does. A Linux account name is an *identifier*, not a
 *     permission: the product will authenticate against Linux accounts + a
 *     restricted PAM path, and the authorization decision that follows is a
 *     scope, not a string comparison against someone's login. (The names
 *     themselves are listed as needles in that test rather than repeated here —
 *     otherwise this comment would trip its own guard, which is exactly the
 *     false-positive `tests/lib/static-refs.mjs` documents.)
 *
 *  Missing input fails closed: an absent or unparseable scope list grants
 *  nothing, because "we could not find out" must never render as "allowed".
 *
 *  No DOM, no network — pure functions, unit-testable under plain Node.
 */

/** The scope vocabulary. Mirrors `KNOWN_SCOPES` in `backend/portal/modules.py`;
 *  a scope this file does not know is not trusted for anything. */
export const SCOPES = Object.freeze({
  /** See that the modules exist and why each is or is not usable. */
  MODULES_READ: 'portal:modules:read',
  /** Open the engine's own console (Strata Chat / Monitor). */
  ENGINES_NATIVE: 'portal:engines:native',
  /** Open the EvalScope native UI. */
  EVALUATION_NATIVE: 'portal:evaluation:native',
  /** See the whole deployment's principals, not just your own rows. */
  ADMIN_READ: 'portal:admin:read',
  /** Publish an endpoint as Shared. Absent ⇒ everything stays Private. */
  ENDPOINTS_SHARE: 'portal:endpoints:share',
  /** Open the read-only Grafana / Prometheus dashboards. */
  OBSERVABILITY_NATIVE: 'portal:observability:native',
  /** Read your own per-call log (the other contract's scope). */
  CALLS_SELF_READ: 'atlas:calls:self:read',
});

/** Every capability this surface can grant, and the scopes each one needs.
 *
 *  All of them are **conjunctive**: every listed scope must be held. That is the
 *  same "all must hold" rule the backend applies to module availability, kept
 *  identical on purpose so the two cannot drift into disagreeing about what a
 *  session can see. */
export const CAPABILITY_RULES = Object.freeze({
  /** Read owned deployments and the endpoints shared with you. */
  readCatalogue: Object.freeze([SCOPES.MODULES_READ]),
  /** Change a deployment's share scope (private / selected / all). */
  shareEndpoints: Object.freeze([SCOPES.ENDPOINTS_SHARE]),
  /** See every principal and every deployment, not just your own rows. */
  viewAllPrincipals: Object.freeze([SCOPES.ADMIN_READ]),
  /** Follow the native engine console link. */
  openEnginesNative: Object.freeze([SCOPES.ENGINES_NATIVE]),
  /** Follow the read-only dashboard links. */
  openObservabilityNative: Object.freeze([SCOPES.OBSERVABILITY_NATIVE]),
  /** Follow the EvalScope native link. */
  openEvaluationNative: Object.freeze([SCOPES.EVALUATION_NATIVE]),
});

export const CAPABILITY_NAMES = Object.freeze(Object.keys(CAPABILITY_RULES));

/**
 * Turn a session's asserted scopes into capability decisions.
 *
 * @param {{scopes?: unknown, roles?: unknown}} [session]
 *   `scopes` — the server-asserted scope list for this session. Anything that
 *              is not an array of strings grants nothing.
 *   `roles`  — accepted **only** so that the "roles change nothing" property is
 *              testable against a real argument. It is never read.
 * @returns {Record<string, {granted: boolean, requires: string[], missing: string[]}>}
 */
export function deriveCapabilities(session = {}) {
  const scopes = new Set(
    (Array.isArray(session?.scopes) ? session.scopes : [])
      .filter((scope) => typeof scope === 'string' && scope.length > 0),
  );

  const capabilities = {};
  for (const [name, required] of Object.entries(CAPABILITY_RULES)) {
    const missing = required.filter((scope) => !scopes.has(scope));
    capabilities[name] = {
      granted: missing.length === 0,
      requires: [...required],
      missing,
    };
  }
  return Object.freeze(capabilities);
}

/** `true` only when the capability exists and is granted. An unknown capability
 *  name is not granted — asking about something this build cannot grant must
 *  not read as permission. */
export const isGranted = (capabilities, name) => capabilities?.[name]?.granted === true;

/**
 * Why a capability is withheld, for the disabled button's `title` and for the
 * inline reason text. Deliberately names the *scope*, since that is the thing
 * an operator can actually change.
 */
export function capabilityReason(capabilities, name) {
  const entry = capabilities?.[name];
  if (!entry) return `本构建没有名为 ${name} 的能力，因此不授权`;
  if (entry.granted) return '';
  return `需要 ${entry.missing.join(' + ')}`;
}

/** The scopes a session actually holds, for the "what can I see" panel. */
export function heldScopes(session = {}) {
  const known = new Set(Object.values(SCOPES));
  return [...new Set(Array.isArray(session?.scopes) ? session.scopes : [])]
    .filter((scope) => typeof scope === 'string' && known.has(scope))
    .sort();
}

/** Scopes the session carries that this build does not know about. Shown, never
 *  acted on: a scope we have not reviewed must not become a capability. */
export function unknownScopes(session = {}) {
  const known = new Set(Object.values(SCOPES));
  return [...new Set(Array.isArray(session?.scopes) ? session.scopes : [])]
    .filter((scope) => typeof scope === 'string' && scope.length > 0 && !known.has(scope))
    .sort();
}
