/*! tests/web-features/capability.test.js — who may do what, and on whose word.
 *
 *  The two properties this file exists to pin down:
 *
 *  1. **Scopes decide, roles do not.** `role: admin` must change nothing. The
 *     backend already refuses to treat a role as a fallback
 *     (`test_an_admin_role_is_not_a_fallback` in the modules suite); a front-end
 *     that quietly reintroduced the shortcut would silently undo it, and the
 *     undo would be invisible in every other test.
 *  2. **No account name is a permission.** The last test in this file reads the
 *     sources under `web/js/features/` and fails if an operator's login appears
 *     in any of them. That is a structural check, not a stylistic one: the
 *     product authenticates against Linux accounts, so a name compared in the
 *     browser is a name an attacker only has to learn.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  CAPABILITY_NAMES,
  CAPABILITY_RULES,
  SCOPES,
  capabilityReason,
  deriveCapabilities,
  heldScopes,
  isGranted,
  unknownScopes,
} from '../../web/js/features/capability.js';

const FEATURES_DIR = resolve(
  fileURLToPath(new URL('../../web/js/features/', import.meta.url)),
);

const grants = (...scopes) => deriveCapabilities({ scopes });

// --- the keys of the model --------------------------------------------------

test('every capability is off when the session holds no scopes', () => {
  const caps = grants();
  for (const name of CAPABILITY_NAMES) {
    assert.equal(caps[name].granted, false, `${name} was granted with no scopes`);
    assert.ok(caps[name].missing.length > 0, `${name} is denied but names nothing missing`);
  }
});

test('a capability is granted by exactly its required scopes', () => {
  for (const [name, required] of Object.entries(CAPABILITY_RULES)) {
    assert.equal(grants(...required)[name].granted, true, `${name} not granted by its own scopes`);
    // One scope short is not enough: every rule is conjunctive.
    if (required.length > 1) {
      assert.equal(grants(...required.slice(1))[name].granted, false);
    }
  }
});

test('an extra scope does not open anything else', () => {
  const caps = grants(SCOPES.CALLS_SELF_READ, SCOPES.OBSERVABILITY_NATIVE);
  assert.equal(caps.readCatalogue.granted, false);
  assert.equal(caps.shareEndpoints.granted, false);
  assert.equal(caps.viewAllPrincipals.granted, false);
});

// --- roles are not authority ------------------------------------------------

test('the admin role grants nothing on its own', () => {
  const byRole = deriveCapabilities({ scopes: [], roles: ['admin'] });
  const byNothing = deriveCapabilities({ scopes: [] });
  assert.deepEqual(byRole, byNothing, 'a role changed a capability decision');
  assert.equal(byRole.viewAllPrincipals.granted, false);
});

test('adding a role never changes any decision, for any role', () => {
  const scopes = [SCOPES.MODULES_READ];
  const baseline = deriveCapabilities({ scopes });
  for (const role of ['admin', 'operator', 'root', 'superuser', 'fleet-master']) {
    assert.deepEqual(
      deriveCapabilities({ scopes, roles: [role] }),
      baseline,
      `role ${role} changed a decision`,
    );
  }
});

test('the admin view is granted by its scope and by nothing else', () => {
  assert.equal(grants(SCOPES.ADMIN_READ).viewAllPrincipals.granted, true);
  assert.equal(
    deriveCapabilities({ scopes: [], roles: ['admin', 'operator'] }).viewAllPrincipals.granted,
    false,
  );
});

// --- malformed input fails closed -------------------------------------------

test('a missing or malformed scope list grants nothing', () => {
  for (const scopes of [undefined, null, 'portal:modules:read', {}, 42, [null], [''], [{}]]) {
    const caps = deriveCapabilities({ scopes });
    for (const name of CAPABILITY_NAMES) {
      assert.equal(caps[name].granted, false, `${name} granted for scopes=${JSON.stringify(scopes)}`);
    }
  }
});

test('an unknown capability is not granted, and says so', () => {
  const caps = grants(SCOPES.ADMIN_READ);
  assert.equal(isGranted(caps, 'deleteEverything'), false);
  assert.equal(isGranted(caps, 'toString'), false);
  assert.match(capabilityReason(caps, 'deleteEverything'), /本构建没有/);
});

test('isGranted is false for a capability object that is not granted', () => {
  assert.equal(isGranted({ x: { granted: 'true' } }, 'x'), false);
  assert.equal(isGranted(undefined, 'x'), false);
});

// --- reason strings name the thing an operator can change -------------------

test('a withheld capability names the missing scope', () => {
  const reason = capabilityReason(grants(SCOPES.MODULES_READ), 'shareEndpoints');
  assert.match(reason, /portal:endpoints:share/);
});

test('a granted capability has no reason text', () => {
  assert.equal(capabilityReason(grants(SCOPES.ENDPOINTS_SHARE), 'shareEndpoints'), '');
});

// --- scope bookkeeping ------------------------------------------------------

test('held and unknown scopes are separated', () => {
  const session = { scopes: [SCOPES.MODULES_READ, 'portal:something:new', 'portal:something:new'] };
  assert.deepEqual(heldScopes(session), [SCOPES.MODULES_READ]);
  assert.deepEqual(unknownScopes(session), ['portal:something:new']);
});

test('an unknown scope is reported but never becomes a capability', () => {
  const caps = grants('portal:everything:allow');
  for (const name of CAPABILITY_NAMES) assert.equal(caps[name].granted, false);
  assert.deepEqual(unknownScopes({ scopes: ['portal:everything:allow'] }), ['portal:everything:allow']);
});

// --- the structural guard ---------------------------------------------------

/** The people this workspace is actually provisioned for, plus the two generic
 *  privilege words a shortcut would reach for. Kept in the test, not in the
 *  sources, so the guard cannot satisfy itself. */
const ACCOUNT_NAMES = ['carol', 'alice', 'dave', 'bob'];

test('no operator account name appears anywhere in web/js/features/', () => {
  const offenders = [];
  for (const entry of readdirSync(FEATURES_DIR, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith('.js')) continue;
    const text = readFileSync(join(FEATURES_DIR, entry.name), 'utf8').toLowerCase();
    for (const name of ACCOUNT_NAMES) {
      // Word-boundary so `invoice` or `alicer` do not fire: the guard must be
      // able to fail for the right reason and stay quiet otherwise.
      if (new RegExp(`\\b${name}\\b`).test(text)) offenders.push(`${entry.name} → ${name}`);
    }
  }
  assert.deepEqual(offenders, [], `an account name is being used as authority:\n  ${offenders.join('\n  ')}`);
});

test('the guard would notice an account name if one were added', () => {
  // Negative control: the pattern above must be able to fire, or it is decoration.
  assert.match('if (uid === "carol") { allow() }'.toLowerCase(), /\bcarol\b/);
  assert.doesNotMatch('const invoice = 1'.toLowerCase(), /\bcarol\b/);
});
