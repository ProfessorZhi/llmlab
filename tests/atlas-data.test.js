/*! tests/atlas-data.test.js
 *  Unit tests for the Atlas data layer: source isolation, the remote contract,
 *  numeric derivations, export contents and layer exclusivity.
 *  `node --test tests/` — no browser, no network.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { DEMO_WORKLOADS, DEMO_GROUPS, RANGE_SCALES } from '../web/js/data/demo-fixture.js';
import {
  resolveSource, validateAtlasBundle, SOURCE_MODES, DATA_STATES,
  usesDemoFixture, emptyState, unknownState, demoState,
} from '../web/js/data/contract.js';
import { createRemoteClient } from '../web/js/data/remote-client.js';
import {
  visibleRows, allRows, sum, sortedRows, filteredRows, paretoCutoff,
  toggleCompare, aggregateP95, groupName, MAX_COMPARE, ATLAS_DEFAULTS,
} from '../web/js/atlas/selectors.js';
import { squarify } from '../web/js/atlas/squarify.js';
import { syntheticHour, syntheticLatency, hexMix, HEAT_CELLS, heatMatrix, heatCell } from '../web/js/atlas/heat.js';
import { buildCsv, buildJson, CSV_FILENAME, JSON_FILENAME, heatReadout } from '../web/js/atlas/exporters.js';
import { structureVisibility, isAtlasView, ATLAS_VIEWS } from '../web/js/atlas/structure.js';
import { segments, arc, point } from '../web/js/atlas/geometry.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..');
const state = (over = {}) => ({ ...ATLAS_DEFAULTS, ...over });

// ---------------------------------------------------------------- fixture

test('demo fixture keeps the documented shape', () => {
  assert.equal(DEMO_WORKLOADS.length, 9);
  assert.equal(DEMO_GROUPS.length, 3);
  assert.equal(sum(DEMO_WORKLOADS, 'calls'), 1248, '24h calls total is 1,248');
  assert.equal(sum(DEMO_WORKLOADS, 'tokens'), 482, '24h output tokens is 482k');
  for (const w of DEMO_WORKLOADS) {
    assert.ok(DEMO_GROUPS.some((g) => g.id === w.g), `${w.id} maps to a known direction`);
    assert.ok(w.p95 > 0 && w.calls > 0);
  }
  assert.equal(new Set(DEMO_WORKLOADS.map((w) => w.id)).size, 9, 'workload ids are unique');
});

// ------------------------------------------------------------ source modes

test('regular URL never loads an implicit Atlas demo fixture', () => {
  for (const search of ['', '?demo=0', '?demo=yes', '?utm_source=chatgpt.com']) {
    const r = resolveSource({ search });
    assert.equal(r.mode, SOURCE_MODES.UNCONFIGURED);
    assert.equal(usesDemoFixture(r), false);
    assert.equal(r.reason, 'no-endpoint-configured');
  }
  assert.equal(resolveSource({ search: '?source=demo' }).mode, SOURCE_MODES.UNCONFIGURED);
});

test('the in-process sample needs the QA build as well as demo=1/true', () => {
  for (const search of ['?demo=1', '?demo=true', '?source=remote&demo=1']) {
    const r = resolveSource({ search, allowDemo: true });
    assert.equal(r.mode, SOURCE_MODES.DEMO, `${search} on the QA build`);
    assert.equal(r.reason, 'explicit-demo');
    assert.equal(usesDemoFixture(r), true);
    // The same query on the artifact that ships is inert — it resolves exactly
    // as if `?demo` were not there at all.
    assert.equal(resolveSource({ search }).mode, SOURCE_MODES.UNCONFIGURED, `${search} on production`);
  }
});

test('an unrecognised source value never silently serves the fixture as real', () => {
  const r = resolveSource({ search: '?source=prod' });
  assert.equal(r.mode, SOURCE_MODES.UNCONFIGURED);
  assert.equal(usesDemoFixture(r), false);
  assert.equal(r.reason, 'unknown-source-value');
});

test('remote without a configured endpoint resolves to unconfigured, not demo', () => {
  const r = resolveSource({ search: '?source=remote' });
  assert.equal(r.mode, SOURCE_MODES.UNCONFIGURED);
  assert.equal(r.reason, 'no-endpoint-configured');
  assert.equal(usesDemoFixture(r), false);
});

test('a configured reviewed same-origin Atlas route is used on the normal URL', () => {
  for (const search of ['', '?utm_source=chatgpt.com', '?source=remote']) {
    const r = resolveSource({ search, host: { endpoint: '/api/atlas/usage' } });
    assert.equal(r.mode, SOURCE_MODES.REMOTE);
    assert.equal(r.endpoint, '/api/atlas/usage');
    assert.equal(usesDemoFixture(r), false);
  }
});

test('foreign, upstream or ambiguous Atlas endpoint configuration fails closed', () => {
  for (const endpoint of [
    'https://example.org/api/atlas/usage',
    '//evil.example/api/atlas/usage',
    'http://127.0.0.1:8200/api/atlas/usage',
    '/api/v2/ops',
    '/api/atlas/../auth',
    '/api/atlas/%2e%2e/auth',
    '/api/atlas/usage?owner=someone-else',
    '/api/atlas/usage#fragment',
  ]) {
    const r = resolveSource({ search: '', host: { endpoint } });
    assert.equal(r.mode, SOURCE_MODES.UNCONFIGURED, endpoint);
    assert.equal(r.reason, 'endpoint-not-approved', endpoint);
  }
});

test('controlled empty/unknown states carry no fabricated numbers', () => {
  for (const s of [emptyState('x'), unknownState('y')]) {
    assert.deepEqual(s.workloads, []);
    assert.deepEqual(s.groups, []);
    assert.equal(s.synthetic, false);
    assert.equal(s.state === DATA_STATES.EMPTY || s.state === DATA_STATES.UNKNOWN, true);
  }
  const demo = demoState(DEMO_WORKLOADS, DEMO_GROUPS);
  assert.equal(demo.synthetic, true);
  assert.equal(demo.state, DATA_STATES.DEMO);
});

// --------------------------------------------------------- remote contract

const validBundle = () => ({
  source: 'gateway',
  synthetic: false,
  scope: 'authorized-projects',
  window: { from: 'a', to: 'b', bucket: '1h' },
  quality: { calls: 'measured', latency: 'partial', ttft: 'unknown' },
  groups: [{ id: 'proj-a', name: 'Project A', calls: 10, workloads: [{ id: 'w1', name: 'W1', calls: 10, p95: 900, tokens: 3 }] }],
  series: [],
  notes: ['No data values fabricated'],
});

test('a well-formed remote bundle validates and stays non-synthetic', () => {
  const v = validateAtlasBundle(validBundle());
  assert.equal(v.ok, true, v.errors.join('; '));
  assert.equal(v.state.state, DATA_STATES.LIVE);
  assert.equal(v.state.synthetic, false);
  assert.equal(v.state.workloads.length, 1);
  assert.equal(v.state.workloads[0].g, 'proj-a', 'workload is attributed to its group');
});

test('a payload that is synthetic, unlabelled or absent is rejected', () => {
  for (const bad of [true, undefined, 'false', 0]) {
    const v = validateAtlasBundle({ ...validBundle(), synthetic: bad });
    assert.equal(v.ok, false, `synthetic=${String(bad)} must be rejected`);
    assert.equal(v.state.state, DATA_STATES.UNKNOWN);
  }
});

test('non-finite or negative counts are rejected rather than rendered', () => {
  const cases = [
    { calls: NaN }, { calls: Infinity }, { calls: -1 }, { calls: '12' },
  ];
  for (const bad of cases) {
    const b = validBundle();
    b.groups[0].workloads[0] = { ...b.groups[0].workloads[0], ...bad };
    const v = validateAtlasBundle(b);
    assert.equal(v.ok, false, `calls=${String(bad.calls)} must be rejected`);
  }
});

test('a missing metric stays null (UNKNOWN), never 0', () => {
  const b = validBundle();
  b.groups[0].workloads[0] = { id: 'w1', name: 'W1', calls: 5, p95: null, tokens: null };
  const v = validateAtlasBundle(b);
  assert.equal(v.ok, true, v.errors.join('; '));
  assert.equal(v.state.workloads[0].p95, null);
  assert.equal(v.state.workloads[0].tokens, null);
});

test('non-object payloads fail closed', () => {
  for (const bad of [null, 42, 'x', []]) {
    const v = validateAtlasBundle(bad);
    assert.equal(v.ok, false);
    assert.equal(v.state.state, DATA_STATES.UNKNOWN);
  }
});

// -------------------------------------------------------- remote client I/O

test('the remote client performs no fetch unless explicitly constructed', async () => {
  let called = 0;
  const client = createRemoteClient({
    endpoint: '/api/atlas/usage',
    fetchImpl: async () => { called++; return { ok: true, json: async () => validBundle() }; },
  });
  const r = await client.load();
  assert.equal(called, 1);
  assert.equal(r.ok, true);
  assert.equal(r.state.state, DATA_STATES.LIVE);
});

test('a failing transport becomes an unknown state, not an exception', async () => {
  const client = createRemoteClient({
    endpoint: '/x',
    fetchImpl: async () => { throw new Error('boom'); },
  });
  const r = await client.load();
  assert.equal(r.ok, false);
  assert.equal(r.state.state, DATA_STATES.UNKNOWN);
  assert.deepEqual(r.state.workloads, []);
});

test('an HTTP error or a bad payload becomes a controlled state', async () => {
  const http500 = createRemoteClient({ endpoint: '/x', fetchImpl: async () => ({ ok: false, status: 500 }) });
  assert.equal((await http500.load()).state.state, DATA_STATES.UNKNOWN);

  const synthetic = createRemoteClient({
    endpoint: '/x',
    fetchImpl: async () => ({ ok: true, json: async () => ({ ...validBundle(), synthetic: true }) }),
  });
  assert.equal((await synthetic.load()).ok, false, 'a synthetic payload must not pass as live');

  const notJson = createRemoteClient({ endpoint: '/x', fetchImpl: async () => ({ ok: true, json: async () => { throw new Error('nope'); } }) });
  assert.equal((await notJson.load()).state.reason, 'remote:not-json');
});

test('creating a client without an endpoint is a programming error', () => {
  assert.throws(() => createRemoteClient({}), /endpoint/);
});

// ------------------------------------------------------------- derivations

test('window scaling matches the documented demo values', () => {
  assert.equal(sum(scaleOf('24h'), 'calls'), 1248);
  assert.equal(sum(scaleOf('7d'), 'calls'), 6614);
  assert.deepEqual(
    visibleRows(DEMO_WORKLOADS, state({ group: 'developer' })).map((d) => d.calls).reduce((a, b) => a + b),
    376,
    '开发自动化 · 24h → 376 次 (matches the prototype QA record)',
  );
  assert.equal(
    sum(visibleRows(DEMO_WORKLOADS, state({ group: 'developer', range: '7d' })), 'calls'),
    1993,
    '开发自动化 · 7d → 1,993 次 (matches the prototype QA record)',
  );
});
const scaleOf = (range) => allRows(DEMO_WORKLOADS, state({ range }));

test('the window multiplier never touches latency percentiles', () => {
  const rows7d = allRows(DEMO_WORKLOADS, state({ range: '7d' }));
  assert.deepEqual(rows7d.map((d) => d.p95), DEMO_WORKLOADS.map((d) => d.p95));
});

test('there is no aggregate p95 by construction', () => {
  assert.equal(aggregateP95(), null);
  assert.equal(RANGE_SCALES['24h'], 1);
});

test('direction filter narrows rows and recomputes the denominator', () => {
  const all = visibleRows(DEMO_WORKLOADS, state());
  const conv = visibleRows(DEMO_WORKLOADS, state({ group: 'conversation' }));
  assert.equal(all.length, 9);
  assert.equal(conv.length, 3);
  assert.equal(sum(conv, 'calls'), 690);
  assert.equal(groupName(DEMO_GROUPS, 'conversation'), '交互与内容');
  assert.equal(groupName(DEMO_GROUPS, 'nope'), '全部工作负载');
});

test('ranking sorts by the chosen metric, descending, with a zh-CN tiebreak', () => {
  const byP95 = sortedRows(visibleRows(DEMO_WORKLOADS, state()), 'p95');
  assert.equal(byP95[0].id, 'stress', '压力测试 has the highest p95');
  assert.equal(byP95.at(-1).id, 'chat');
  const byCalls = sortedRows(visibleRows(DEMO_WORKLOADS, state()), 'calls');
  assert.equal(byCalls[0].id, 'chat');
});

test('search matches workload name and direction, then re-sorts', () => {
  const hits = filteredRows(DEMO_WORKLOADS, DEMO_GROUPS, state({ search: '视觉' }));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].id, 'vision');
  const byDirection = filteredRows(DEMO_WORKLOADS, DEMO_GROUPS, state({ search: '开发自动化' }));
  assert.equal(byDirection.length, 3, 'search matches the direction label too');
  assert.equal(filteredRows(DEMO_WORKLOADS, DEMO_GROUPS, state({ search: '不存在' })).length, 0);
});

test('pareto cut reports the workload count carrying >=80% of calls', () => {
  const { cutoff, n, share } = paretoCutoff(visibleRows(DEMO_WORKLOADS, state()));
  assert.equal(n, 9);
  assert.equal(cutoff, 6);
  assert.ok(share >= 0.8 && share < 0.9);
  assert.deepEqual(paretoCutoff([]), { cutoff: 0, n: 0, share: 0 });
});

test('the comparison basket holds at most four and reports refusals', () => {
  let basket = [];
  for (const id of ['a', 'b', 'c', 'd']) basket = toggleCompare(basket, id).compare;
  assert.equal(basket.length, MAX_COMPARE);
  const refused = toggleCompare(basket, 'e');
  assert.equal(refused.rejected, true);
  assert.deepEqual(refused.compare, basket, 'a refused add leaves the basket unchanged');
  assert.deepEqual(toggleCompare(basket, 'a').compare, ['b', 'c', 'd'], 'toggling an existing id removes it');
});

test('group segments tile the full circle', () => {
  const segs = segments(DEMO_GROUPS.map((g) => ({ ...g, value: 1 })), 'value');
  assert.equal(segs[0].start, 0);
  assert.equal(segs.at(-1).end, 360);
  assert.equal(segs.reduce((n, s) => n + s.span, 0), 360);
});

test('polar helpers are consistent', () => {
  const [x0, y0] = point(100, 100, 50, 0); // 12 o'clock
  assert.ok(Math.abs(x0 - 100) < 1e-9 && Math.abs(y0 - 50) < 1e-9);
  assert.match(arc(100, 100, 10, 20, 0, 90), /^M[\d.]+ [\d.]+ A20 20 0 0 1 /);
  assert.match(arc(100, 100, 10, 20, 0, 360), /0 1 /, 'a full circle still uses the large-arc flag path');
});

// ----------------------------------------------------------------- exports

test('CSV export is BOM-prefixed, quoted, and labelled DEMO on every row', () => {
  const rows = visibleRows(DEMO_WORKLOADS, state({ group: 'evaluation' }));
  const csv = buildCsv(rows, DEMO_GROUPS, state({ group: 'evaluation' }));
  assert.equal(csv.charCodeAt(0), 0xfeff, 'starts with a UTF-8 BOM');
  const lines = csv.slice(1).split('\r\n');
  assert.equal(lines.length, rows.length + 1);
  assert.match(lines[0], /^"业务方向","工作负载","调用请求_DEMO","p95毫秒_DEMO","输出Tokens千_DEMO","统计窗口","来源"$/);
  assert.equal((csv.match(/synthetic-ui-fixture/g) || []).length, rows.length);
  assert.match(csv, /"实验评测","压力测试","48","2380","13","24h"/);
  assert.equal(CSV_FILENAME.includes('DEMO'), true);
});

test('CSV quoting survives a quote in a label', () => {
  const rows = [{ id: 'x', g: 'conversation', name: 'a"b', calls: 1, p95: 2, tokens: 3 }];
  assert.match(buildCsv(rows, DEMO_GROUPS, state()), /"a""b"/);
});

test('JSON export carries provenance and the percentile caveat', () => {
  const rows = visibleRows(DEMO_WORKLOADS, state());
  const payload = JSON.parse(buildJson(rows, state()));
  assert.equal(payload.meta.status, 'DEMO_ONLY');
  assert.equal(payload.meta.source, 'synthetic-ui-fixture');
  assert.match(payload.meta.note, /percentile/i);
  assert.equal(payload.rows.length, 9);
  assert.equal(JSON_FILENAME.includes('DEMO'), true);
});

// -------------------------------------------------------------------- heat

test('the activity matrix is a complete 7x24 grid, deterministic per cell', () => {
  assert.equal(HEAT_CELLS, 168);
  const m = heatMatrix('volume');
  assert.equal(m.length, 168);
  assert.equal(new Set(m.map((c) => `${c.day}-${c.hour}`)).size, 168);
  // deterministic: recomputing a cell gives the same value
  const first = m[0];
  assert.equal(syntheticHour(first.day, first.hour), first.value);
  assert.notEqual(syntheticHour(0, 3), syntheticHour(3, 3), 'weekday and weekend differ');
  assert.ok(syntheticLatency(0, 14) > 0);
});

test('heat colour mixing is a correct midpoint blend', () => {
  assert.equal(hexMix('#000000', '#ffffff', 0.5), '#808080');
  assert.equal(hexMix('#000000', '#ffffff', 0), '#000000');
  assert.equal(hexMix('#000000', '#ffffff', 1), '#ffffff');
  const cell = heatCell(0, 14, 'volume', '#ffffff', '#000000');
  assert.match(cell.fill, /^#[0-9a-f]{6}$/);
  assert.ok(cell.value > 0);
});

test('heat readout names the metric and denies being live monitoring', () => {
  assert.match(heatReadout('周一', 3, 'volume', 120), /周一 · 03:00 — 合成请求量 120 次/);
  assert.match(heatReadout('周一', 3, 'latency', 900), /延迟压力 900 ms/);
  assert.match(heatReadout('周一', 3, 'volume', 1), /不是实时监控/);
});

// --------------------------------------------------------- chart layering

test('exactly one structure layer is visible at a time', () => {
  assert.deepEqual(ATLAS_VIEWS, ['sunburst', 'treemap', 'dots']);
  for (const view of ATLAS_VIEWS) {
    const v = structureVisibility(view);
    assert.equal(v.svgHidden && v.canvasHidden, false, `${view}: both layers hidden`);
    assert.equal(v.svgHidden !== v.canvasHidden, true, `${view}: layers are mutually exclusive`);
  }
  assert.deepEqual(structureVisibility('dots'), { svgHidden: true, canvasHidden: false });
  assert.deepEqual(structureVisibility('sunburst'), { svgHidden: false, canvasHidden: true });
  assert.equal(isAtlasView('bogus'), false);
});

// ------------------------------------------------------------- treemap

test('treemap rectangles fill the frame and stay inside it', () => {
  const rect = { x: 11, y: 15, w: 738, h: 510 };
  const blocks = squarify(DEMO_GROUPS.map((g) => ({ ...g, value: sum(DEMO_WORKLOADS.filter((d) => d.g === g.id), 'calls') })), rect);
  assert.equal(blocks.length, 3);
  const area = blocks.reduce((n, b) => n + b.w * b.h, 0);
  assert.ok(Math.abs(area - rect.w * rect.h) < 1, `areas tile the frame (got ${area})`);
  for (const b of blocks) {
    assert.ok(b.x >= rect.x - 1e-6 && b.y >= rect.y - 1e-6);
    assert.ok(b.x + b.w <= rect.x + rect.w + 1e-6 && b.y + b.h <= rect.y + rect.h + 1e-6);
  }
  // deterministic
  assert.deepEqual(squarify([{ value: 3 }, { value: 1 }], rect), squarify([{ value: 3 }, { value: 1 }], rect));
  assert.deepEqual(squarify([], rect), []);
});

// ------------------------------------------------- frozen source / generated

test('prototypes/v5/index.html is byte-identical to the frozen source', () => {
  const p = join(REPO, 'prototypes', 'v5', 'index.html');
  // Normalise to LF before hashing. On Windows `core.autocrlf=true` checks the
  // prototype out with CRLF even though the committed blob is LF, so hashing
  // the worktree bytes verbatim fails on a perfectly good fresh clone — the
  // pinned digest is the blob's. What is frozen is the content; the checkout
  // style is a local git setting and is not.
  const text = readFileSync(p, 'utf8').replace(/\r\n/g, '\n');
  const sha = createHash('sha256').update(text, 'utf8').digest('hex');
  assert.equal(sha, '04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf',
    'the prototype is the frozen source of record and must not be edited');
});

/** The `<section>` that opens (directly or transitively) at `id="view-<name>"`,
 *  found by counting nested `<section>` tags — every `</section>` in the file is
 *  identical, so a plain search for the closing tag would land in a neighbour. */
function viewSection(html, name) {
  const needle = `id="view-${name}"`;
  const idAt = html.indexOf(needle);
  assert.ok(idAt > 0, `#view-${name} must exist`);
  let cursor = html.lastIndexOf('<section', idAt);
  let depth = 0;
  while (cursor < html.length) {
    const nextOpen = html.indexOf('<section', cursor);
    const nextClose = html.indexOf('</section>', cursor);
    if (nextClose < 0) throw new Error(`unbalanced <section> around #view-${name}`);
    if (nextOpen >= 0 && nextOpen < nextClose) { depth += 1; cursor = nextOpen + 8; continue; }
    depth -= 1;
    const end = nextClose + '</section>'.length;
    if (depth === 0) return html.slice(html.lastIndexOf('<section', idAt), end);
    cursor = end;
  }
  throw new Error(`unbalanced <section> around #view-${name}`);
}

const SEVEN_ORIGINAL_VIEWS = ['overview', 'models', 'api', 'observability', 'atlas', 'evaluation', 'admin'];

test('v6 appends module panels to the seven original views and edits nothing else', async () => {
  // "Keep the existing routes and visual parity" is only a fact if it is
  // checked. v6 is v5 + the authored additions + the *declared* copy patches, so
  // each original view must equal its patched v5 form (minus the closing tag)
  // with only a module panel appended. A hand-edit — a renamed class, a
  // reordered row, a deleted paragraph — still fails here, and the copy patches
  // themselves are enumerated in derive-from-v5.mjs rather than waved through.
  const { readFragments, copyPatches, replaceUnique } = await import('../prototypes/v6/derive-from-v5.mjs');
  const frags = readFragments(readFileSync(join(REPO, 'prototypes', 'v6', 'additions.html'), 'utf8'));
  const need = (n) => {
    const v = frags.get(n);
    if (!v) throw new Error(`missing fragment: ${n}`);
    return v;
  };
  const v5 = readFileSync(join(REPO, 'prototypes', 'v5', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
  let patched = v5;
  for (const [from, to, label] of copyPatches(need)) patched = replaceUnique(patched, from, to, label);
  const v6 = readFileSync(join(REPO, 'prototypes', 'v6', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
  const close = '</section>';
  for (const name of SEVEN_ORIGINAL_VIEWS) {
    const before = viewSection(patched, name);
    const after = viewSection(v6, name);
    const prefix = before.slice(0, before.lastIndexOf(close));
    assert.ok(after.startsWith(prefix), `#view-${name} was edited beyond the declared patches`);
    assert.ok(after.endsWith(close), `#view-${name} must still close with ${close}`);
    if (after === before) continue;
    const inserted = after.slice(prefix.length, after.length - close.length);
    assert.match(inserted, /module-panel/, `#view-${name} gained something other than a module panel`);
  }
  // The declared patches only touch copy: the patched shell still carries no
  // module panel of its own, so a patch could never smuggle structure into v5.
  assert.equal(
    (patched.match(/module-panel/g) || []).length,
    (v5.match(/module-panel/g) || []).length,
    'a copy patch must not add or remove a module panel',
  );
});

test('v6 keeps the original navigation and adds exactly one entry', () => {
  const navOf = (html) => {
    const open = html.indexOf('<nav class="topnav"');
    return html.slice(open, html.indexOf('</nav>', open));
  };
  const v5 = readFileSync(join(REPO, 'prototypes', 'v5', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
  const v6 = readFileSync(join(REPO, 'prototypes', 'v6', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
  const entries = (nav) => [...nav.matchAll(/data-page="([a-z]+)"(?:[^>]*)>([^<]*)</g)].map((m) => [m[1], m[2]]);
  const before = entries(navOf(v5));
  const after = entries(navOf(v6));
  assert.equal(after.length, before.length + 1, 'exactly one navigation entry should be added');
  const added = after.filter(([page]) => !before.some(([p]) => p === page));
  assert.deepEqual(added, [['calls', '我的调用']]);
  // The original seven, unchanged in name, label and relative order.
  assert.deepEqual(after.filter(([page]) => page !== 'calls'), before);
});

test('prototypes/v6/index.html is exactly v5 plus the authored additions', async () => {
  // The chain is v5 -> v6 -> web/. This checks the first hop the same way the
  // next test checks the second: by recomputing it, not by trusting the file.
  // A hand-edit to v6 fails here, and the v5 hash assertion above proves the
  // frozen authority source was not touched to make it pass.
  const { derive } = await import('../prototypes/v6/derive-from-v5.mjs');
  const { html } = derive();
  const onDisk = readFileSync(join(REPO, 'prototypes', 'v6', 'index.html'), 'utf8')
    .replace(/\r\n/g, '\n');
  assert.equal(onDisk, html, 'prototypes/v6 must be what v5 + additions produce');
});

test('the extractor pins the v6 file it actually reads', async () => {
  // Two places could pin the source: the generator, and this suite. If they
  // drift apart, one of them is checking a file the build no longer uses.
  const { EXPECTED_SOURCE_SHA256, V5_SHA256 } = await import('../web/tools/extract-from-prototype.mjs');
  const v6 = readFileSync(join(REPO, 'prototypes', 'v6', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
  const v6Sha = createHash('sha256').update(v6, 'utf8').digest('hex');
  assert.equal(v6Sha, EXPECTED_SOURCE_SHA256, 'the generator must pin the v6 file it reads');
  const v5 = readFileSync(join(REPO, 'prototypes', 'v5', 'index.html'), 'utf8').replace(/\r\n/g, '\n');
  const v5Sha = createHash('sha256').update(v5, 'utf8').digest('hex');
  assert.equal(v5Sha, V5_SHA256, 'the generator must pin the frozen v5 authority source');
});

test('web/ still matches what the extractor would generate', async () => {
  const { generate } = await import('../web/tools/extract-from-prototype.mjs');
  const out = generate();
  const files = {
    [join(REPO, 'web', 'index.html')]: out.indexHtml,
    [join(REPO, 'web', 'css', 'tokens.css')]: out.tokensCss,
    [join(REPO, 'web', 'css', 'base.css')]: out.baseCss,
    [join(REPO, 'web', 'css', 'atlas.css')]: out.atlasCss,
    [join(REPO, 'web', 'css', 'portal.css')]: out.portalCss,
  };
  for (const [path, expected] of Object.entries(files)) {
    assert.ok(existsSync(path), `${path} exists`);
    // Same caveat as the frozen-source check above: on a core.autocrlf=true
    // checkout the worktree copy is CRLF while the generator emits LF. Compare
    // content, not the local checkout style.
    const actual = readFileSync(path, 'utf8').replace(/\r\n/g, '\n');
    assert.equal(actual, expected, `${path} drifted from the generator`);
  }
});
