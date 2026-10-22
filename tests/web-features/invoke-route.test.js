/*! tests/web-features/invoke-route.test.js — `ops.invoke` is a boundary, not a button.
 *
 *  BFF SEAM-1 (#115) declares `POST /api/v2/ops/endpoints/{endpoint_id}/invoke`
 *  with `scope: registry:endpoint:invoke`. The authority for it lives in
 *  Registry (`RegistryService.invoke`: row ACL, readiness, payload bound,
 *  audit-before-forward); the BFF only wires it. The Router-ACK gate is
 *  *reported*, not enforced, at this layer.
 *
 *  So the only honest thing this front-end may do with the route today is
 *  **declare it**: the matrix row carries the method, the dynamic template, the
 *  session requirement, the scope and the CSRF boundary; the rendered matrix
 *  shows a write boundary with no control; the write gate still ships an empty
 *  verified-contract list; and no module in `web/js` calls the path.
 *
 *  Two checkouts must both be green here:
 *
 *    stacked  this branch alone: `route_map.py` has 19 rows (#115 not merged),
 *             so `ops.invoke` is the one pending row.
 *    union    #115/#120 merged: `route_map.py` has 20 rows and the pending set
 *             is empty.
 *
 *  The previous version asserted the literal `19`, which made a correct union
 *  checkout red (`actual: 20, expected: 19`). The dependency is now a
 *  **computed set difference** measured against the mirror: it can never go
 *  stale, and any unexpected divergence still fails in either direction.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { resolve, join } from 'node:path';

import { BFF_ROUTES, BFF_PENDING_ROWS, renderRoutes } from '../../web/js/workspace/routes.js';
import {
  VERIFIED_WRITE_CONTRACTS,
  WRITE_CONTROL_CONTRACTS,
  createOps,
} from '../../web/js/features/controller.js';

const REPO = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const read = (p) => readFileSync(join(REPO, p), 'utf8').replace(/\r\n/g, '\n');
const lfHash = (p) => createHash('sha256').update(read(p), 'utf8').digest('hex');

const INVOKE_PATH = '/api/v2/ops/endpoints/{endpoint_id}/invoke';

/** The frozen V5 design authority. Pinned in both provenance tools; asserted
 *  here as well so a UI-only change cannot quietly move it. */
const V5_SHA256 = '04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf';

/** The scope expressions in `route_map.py` mapped to the literals the mirror
 *  carries. The Python table names symbols, not strings; this is the one place
 *  the two vocabularies are joined, so a renamed constant fails here. */
const SCOPE_SYMBOLS = Object.freeze({
  None: null,
  'observability_identity.SCOPE_SELF_READ': 'observability:self:read',
  'observability_identity.SCOPE_HOST_READ': 'observability:host:read',
  'bff_scopes.PORTAL_ENDPOINTS_SHARE': 'portal:endpoints:share',
  'bff_scopes.PORTAL_ADMIN_READ': 'portal:admin:read',
  atlas_calls_scope: 'atlas:calls:self:read',
  eval_read_scope: 'evaluation:read',
  registry_invoke_scope: 'registry:endpoint:invoke',
  registry_write_scope: 'registry:write',
  registry_read_scope: 'registry:read',
});

/** Parse the Python `ROUTES` table into the fields both sides must agree on. */
function localRoutes() {
  const py = read('backend/bff/route_map.py');
  const block = py.slice(py.indexOf('ROUTES: tuple[RouteSpec, ...] = ('), py.indexOf('ROUTES_BY_NAME'));
  const field = (chunk, re) => chunk.match(re)?.[1] ?? null;
  const rows = block.split('RouteSpec(').slice(1).map((chunk) => {
    const scopeExpr = field(chunk, /^\s*scope=([^\n,]+)/m)?.trim() ?? 'None';
    return {
      name: field(chunk, /name="([^"]+)"/),
      method: field(chunk, /methods=\("([A-Z]+)",?\)/),
      path: field(chunk, /path="([^"]+)"/),
      auth: field(chunk, /auth=AUTH_([A-Z]+)/)?.toLowerCase(),
      kind: field(chunk, /kind=KIND_([A-Z]+)/)?.toLowerCase(),
      csrf: field(chunk, /csrf=(True|False)/) === 'True',
      scopeExpr,
    };
  });
  return rows;
}

const invokeRow = () => BFF_ROUTES.find((row) => row.name === 'ops.invoke');

// --- 1. the route the mirror declares --------------------------------------

test('ops.invoke carries its method, dynamic template, session and scope', () => {
  const row = invokeRow();
  assert.ok(row, 'the mirror carries the SEAM-1 route');
  assert.equal(row.method, 'POST');
  assert.equal(row.path, INVOKE_PATH);
  assert.equal(row.auth, 'session', 'identity comes from the session, never a query parameter');
  assert.equal(row.kind, 'write');
  assert.equal(row.scope, 'registry:endpoint:invoke');
  assert.equal(row.owner, 'Integration#49');
  // The endpoint id is a path template, not something a caller appends.
  assert.match(row.path, /\{endpoint_id\}/);
});

test('the matrix renders ops.invoke as a write boundary, never as a control', () => {
  const html = renderRoutes();
  const row = html.slice(html.indexOf('data-route="ops.invoke"'));
  const rowHtml = row.slice(0, row.indexOf('</tr>'));
  assert.ok(rowHtml.length > 0);
  assert.match(rowHtml, /POST \/api\/v2\/ops\/endpoints\/\{endpoint_id\}\/invoke/);
  assert.match(rowHtml, /写 · CSRF/, 'a write is labelled as a boundary');
  assert.match(rowHtml, /registry:endpoint:invoke/);
  assert.doesNotMatch(rowHtml, /<button|<input|<select|<a\s/, 'no control is rendered for the route');
  assert.doesNotMatch(rowHtml, /\bLIVE\b/);
  // No session-derived value may be baked into the declaration.
  assert.doesNotMatch(rowHtml, /\buid\b|principal|@|token/i);
});

test('the write gate still has no verified contract, and no invoke entry was added', () => {
  assert.deepEqual([...VERIFIED_WRITE_CONTRACTS], [],
    'declaring a route must not verify a write contract');
  assert.deepEqual(Object.keys(WRITE_CONTROL_CONTRACTS).sort(),
    ['[data-eval-submit]', '[data-playground-send]'],
    'the gate lists the same two controls as before; ops.invoke is not one of them');
  assert.equal(WRITE_CONTROL_CONTRACTS['[data-playground-send]'], 'playground.submit');
});

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (entry.endsWith('.js')) out.push(full);
  }
  return out;
}

test('no front-end module calls the invoke path', () => {
  const offenders = walk(join(REPO, 'web', 'js'))
    .filter((file) => readFileSync(file, 'utf8').includes('/invoke'))
    .map((file) => file.slice(REPO.length + 1))
    .filter((rel) => rel !== 'web/js/workspace/routes.js');
  assert.deepEqual(offenders, [],
    'the route is declared in the matrix and called nowhere; no control, no fetch');
});

// --- 2. the stacked / union dependency, computed ---------------------------

test('the mirror and the local table agree under both stacked and union SHAs', () => {
  const local = localRoutes();
  const localNames = new Set(local.map((row) => row.name));

  // Two known checkouts: 25 rows (#115 not merged) and 26 (union with
  // #115/#120); both include the web-registration and plugin-address routes. Any other count
  // is a table this test cannot reason about, and guessing would be worse than failing.
  assert.ok([25, 26, 28, 29, 30].includes(local.length),
    `local route table has ${local.length} rows; expected 25 (pre-#115), 26 (union), 28 (+ 整机读数/模型服务密钥) or 30 (+ 引擎实时读数)`);

  // The dependency is the REAL difference, measured — not a literal that goes
  // stale the moment the BFF branch is merged.
  const pending = BFF_ROUTES
    .filter((row) => !localNames.has(row.name))
    .map((row) => row.name)
    .sort();
  const expectedPending = localNames.has('ops.invoke') ? [] : [...BFF_PENDING_ROWS].sort();
  assert.deepEqual(pending, expectedPending,
    'the real difference between the mirror and the local table equals the declared pending set');

  // Both directions are asserted explicitly, so neither state can rot.
  if (localNames.has('ops.invoke')) {
    assert.deepEqual(pending, [], 'a union checkout has no pending route');
    assert.ok(localNames.has('ops.invoke'), 'the union carries the SEAM-1 route');
  } else {
    assert.deepEqual(pending, ['ops.invoke'], 'the stacked checkout is missing exactly ops.invoke');
    assert.equal(localNames.has('ops.invoke'), false);
  }

  // Every declared pending row must still exist in the mirror — an empty
  // vocabulary entry would silently widen the tolerance.
  for (const name of BFF_PENDING_ROWS) {
    assert.ok(BFF_ROUTES.some((row) => row.name === name), `${name} is not a mirror row`);
  }
});

test('every locally-declared row agrees with the mirror on method, session, CSRF and scope', () => {
  // Runs on both SHAs: under the union all 26 rows are checked; stacked, 25 are.
  const mirrorByName = new Map(BFF_ROUTES.map((row) => [row.name, row]));
  for (const spec of localRoutes()) {
    const row = mirrorByName.get(spec.name);
    assert.ok(row, `${spec.name} is declared by the BFF but missing from the mirror`);
    assert.equal(row.method, spec.method, `${spec.name}: method`);
    assert.equal(row.path, spec.path, `${spec.name}: path`);
    assert.equal(row.auth, spec.auth, `${spec.name}: session/anonymous`);
    assert.equal(row.kind, spec.kind, `${spec.name}: read/write`);
    // The backend encodes the boundary as a boolean `csrf`; the mirror encodes
    // it as `kind: write`. They must move together.
    assert.equal(spec.csrf, row.kind === 'write', `${spec.name}: CSRF and kind disagree`);
    const expectedScope = SCOPE_SYMBOLS[spec.scopeExpr];
    assert.notEqual(expectedScope, undefined, `${spec.name}: unknown scope expression ${spec.scopeExpr}`);
    assert.equal(row.scope, expectedScope, `${spec.name}: scope`);
  }
});

// --- 3. frozen design authority and session-scoped leakage -----------------

test('the V5 design authority is frozen', () => {
  assert.equal(lfHash('prototypes/v5/index.html'), V5_SHA256,
    'V5 is the design authority; a UI change must not move it');
});

test('a session change drops the privileged payload instead of leaving it on screen', async () => {
  // The shared mechanism every ops panel (including the route matrix's host)
  // reads through: `invalidate()` must clear the session-scoped payload, so an
  // expired elevation or a logout cannot keep rendering the previous session.
  const emptyDoc = { querySelector: () => null, querySelectorAll: () => [] };
  const controller = createOps({
    doc: emptyDoc,
    source: {
      mode: 'live',
      load: async () => ({ state: 'ready', ops: { scope: { scopes: ['portal:admin:read'] } }, reason: null, errors: [] }),
      loadEvents: async () => ({ state: 'unknown', reason: 'not-configured', dto: null }),
    },
  });
  await controller.load();
  assert.ok(controller.ops, 'sanity: the first load carries a payload');
  controller.invalidate();
  assert.equal(controller.ops, null, 'a scope change must not leave the previous payload behind');
  assert.equal(controller.capabilities.viewAllPrincipals.granted, false,
    'and it must not keep a capability granted by the previous session');
});
