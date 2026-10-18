/*! tests/web-features/live-mode-gating.test.js — the default-live gate, whole.
 *
 *  Issue #44's core complaint is that live mode was only *mostly* live: the KPI
 *  row was gated, but the overview's synthetic 24-point curve, its fixed "active
 *  route" card and its model roster were still painted on a bare URL. This file
 *  pins the two halves of the fix:
 *
 *    1. **Liveness.** Every selector in `SAMPLE_REGIONS` must match something in
 *       the shipped `web/index.html`. Two of them named tables that did not
 *       exist, which is why the overview leaked: a gate rule that matches
 *       nothing reads like coverage and delivers none.
 *    2. **Behaviour.** `applyDataMode` marks the sample regions, inserts exactly
 *       one stand-in per region, collapses a grid that lost a column, and in demo
 *       mode removes every stand-in again.
 *
 *  There is no jsdom here either. The document double below implements the
 *  handful of selectors this gate actually uses — id, class, attribute,
 *  descendant, `:scope >` and a comma list — and nothing else. It is a witness,
 *  not a browser: the responsive and screenshot claims belong to the Chrome
 *  suite, which cannot run on a host without Chrome.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  COLLAPSED_CLASS,
  DATA_MODES,
  SAMPLE_REGIONS,
  applyDataMode,
  resolveDataMode,
} from '../../web/js/demo-mode.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const WEB = readFileSync(join(REPO, 'web', 'index.html'), 'utf8').replace(/\r\n/g, '\n');

// --- 1. liveness, against the real generated HTML ---------------------------

/** Slice one `#view-<id>` section out of the shipped markup, counting nested
 *  `<section>` tags so a view that contains sub-sections closes where it really
 *  closes. Mirrors the walk in `prototypes/v6/derive-from-v5.mjs`. */
function sectionOf(html, viewId) {
  const needle = `id="view-${viewId}"`;
  const idAt = html.indexOf(needle);
  if (idAt < 0) return null;
  let cursor = html.lastIndexOf('<section', idAt);
  let depth = 0;
  while (cursor >= 0 && cursor < html.length) {
    const nextOpen = html.indexOf('<section', cursor);
    const nextClose = html.indexOf('</section>', cursor);
    if (nextClose < 0) return null;
    if (nextOpen >= 0 && nextOpen < nextClose) { depth += 1; cursor = nextOpen + '<section'.length; continue; }
    depth -= 1;
    if (depth === 0) return html.slice(html.lastIndexOf('<section', idAt), nextClose);
    cursor = nextClose + '</section>'.length;
  }
  return null;
}

/** `#view-overview .explorer .side-side` -> `{ view: 'overview', classes: [...] }` */
function parseRegionSelector(sel) {
  const parts = sel.trim().split(/\s+/);
  const view = parts[0].startsWith('#view-') ? parts[0].slice('#view-'.length) : null;
  return { view, classes: parts.slice(1).map((p) => p.replace(/^\./, '')) };
}

const hasClassToken = (markup, token) =>
  new RegExp(`class="[^"]*\\b${token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b[^"]*"`).test(markup);

test('every gated region names something that exists in the shipped shell', () => {
  for (const { sel } of SAMPLE_REGIONS) {
    const { view, classes } = parseRegionSelector(sel);
    assert.ok(view, `region selector must start with a #view- id: ${sel}`);
    const section = sectionOf(WEB, view);
    assert.ok(section, `region selector names a view the shell does not have: ${sel}`);
    for (const token of classes) {
      assert.ok(hasClassToken(section, token),
        `"${sel}" is a dead rule: #view-${view} contains no .${token}`);
    }
  }
});

test('the regions the audit named are actually gated now', () => {
  const selectors = SAMPLE_REGIONS.map((region) => region.sel);
  // The overview's synthetic curve and its fixed "active route" card…
  assert.ok(selectors.includes('#view-overview .explorer .side-side'),
    'the overview active-route card must be a sample region');
  assert.ok(selectors.includes('#view-overview .tablewrap'),
    'the overview model roster must be a sample region');
  // …and the API page's placeholder-text SDK block, which lives inside
  // `.api-keys` beside the key table — one rule covers both.
  assert.ok(selectors.includes('#view-api .api-keys'),
    'the key table and its placeholder curl example must be a sample region');
  const apiKeysMarkup = sectionOf(WEB, 'api');
  assert.ok(/lab\.example/.test(apiKeysMarkup),
    'sanity: the placeholder endpoint really is on the API page');
  // The two selectors that used to be dead must be gone.
  assert.ok(!selectors.includes('#view-models .tablewrap'), 'dead selector is still listed');
  assert.ok(!selectors.includes('#view-evaluation .tablewrap'), 'dead selector is still listed');
});

test('the mode resolver needs the QA build as well as the exact opt-in', () => {
  for (const search of ['', '?demo=0', '?demo=', '?demo=yes', '?source=demo']) {
    assert.equal(resolveDataMode(search, { qa: true }), DATA_MODES.LIVE, `"${search}" must stay live`);
  }
  assert.equal(resolveDataMode('?demo=1', { qa: true }), DATA_MODES.DEMO);

  // The query only asks. Without the QA artifact the answer is live, every time —
  // this is the half that makes the production gate a property of the build
  // rather than of a URL.
  for (const search of ['?demo=1', '?demo=true']) {
    assert.equal(resolveDataMode(search), DATA_MODES.LIVE, `"${search}" must stay live on production`);
    assert.equal(resolveDataMode(search, { qa: false }), DATA_MODES.LIVE);
  }
});

// --- 2. behaviour, against a document double --------------------------------

/** Does `node` match one comma-free selector chain? Supports the subset the gate
 *  uses: `#id`, `.class`, `tag`, `[attr]`, `[attr="v"]`, `:scope`, a descendant
 *  chain, and the `>` direct-child combinator. */
function matchesChain(node, chain, scope) {
  const tokens = chain.trim().split(/\s+/);
  // Walk the chain right-to-left into steps that apply to the node first.
  const steps = [];
  let direct = false;
  for (let i = tokens.length - 1; i >= 0; i--) {
    if (tokens[i] === '>') { direct = true; continue; }
    steps.push({ part: tokens[i], direct });
    direct = false;
  }
  if (!matchesPart(node, steps[0].part, scope)) return false;
  let current = node;
  for (let s = 1; s < steps.length; s++) {
    if (steps[s].direct) {
      current = current.parentElement;
      if (!current || !matchesPart(current, steps[s].part, scope)) return false;
    } else {
      current = ancestorMatching(current, steps[s].part, scope);
      if (!current) return false;
    }
  }
  return true;
}

function ancestorMatching(node, part, scope) {
  let current = node.parentElement;
  while (current) {
    if (matchesPart(current, part, scope)) return current;
    current = current.parentElement;
  }
  return null;
}

function matchesPart(node, part, scope) {
  if (!node) return false;
  if (part === ':scope') return node === scope;
  const attr = /^\[([\w-]+)(?:=["']?([^"']*)["']?)?\]$/.exec(part);
  if (attr) {
    if (!node.hasAttribute(attr[1])) return false;
    return attr[2] === undefined || String(node.getAttribute(attr[1])) === attr[2];
  }
  const cls = /^\.([\w-]+)$/.exec(part);
  if (cls) return node.classList.contains(cls[1]);
  const id = /^#([\w-]+)$/.exec(part);
  if (id) return node.id === id[1];
  if (/^[\w-]+$/.test(part)) return node.tagName === part.toLowerCase();
  return false;
}

const camelToKebab = (name) => String(name).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);

function makeNode(tag, { id = null, classes = [], attrs = {} } = {}) {
  const node = {
    tagName: tag.toLowerCase(),
    id,
    children: [],
    parentElement: null,
    _classes: new Set(classes),
    _attrs: { ...attrs },
    // `dataset` is a view over the `data-*` attributes, exactly as in the DOM.
    // Modelling it as a stand-alone object was the bug that made the gate look
    // non-idempotent: `setAttribute('data-gated', …)` and `dataset.gated` would
    // have drifted apart, so the "have I already placed a stand-in here?" check
    // could never see the one it placed.
    get dataset() {
      return new Proxy({}, {
        get: (_, prop) => (typeof prop === 'string' ? node._attrs[`data-${camelToKebab(prop)}`] : undefined),
        set: (_, prop, value) => {
          node._attrs[`data-${camelToKebab(prop)}`] = String(value);
          return true;
        },
        has: (_, prop) => typeof prop === 'string' && `data-${camelToKebab(prop)}` in node._attrs,
      });
    },
    classList: {
      contains: (name) => node._classes.has(name),
      toggle(name, on) { if (on) node._classes.add(name); else node._classes.delete(name); },
      add: (name) => node._classes.add(name),
    },
    get className() { return [...node._classes].join(' '); },
    set className(value) { node._classes = new Set(String(value).split(/\s+/).filter(Boolean)); },
    hasAttribute(name) { return name in node._attrs; },
    getAttribute(name) { return node._attrs[name] ?? null; },
    setAttribute(name, value) { node._attrs[name] = String(value); },
    removeAttribute(name) { delete node._attrs[name]; },
    appendChild(child) { child.parentElement = node; node.children.push(child); return child; },
    insertBefore(child, before) {
      child.parentElement = node;
      const at = node.children.indexOf(before);
      if (at < 0) node.children.push(child); else node.children.splice(at, 0, child);
      return child;
    },
    after(sibling) {
      const parent = node.parentElement;
      parent.insertBefore(sibling, node.children[node.children.indexOf(node) + 1] ?? null);
    },
    remove() {
      const parent = node.parentElement;
      if (!parent) return;
      parent.children.splice(parent.children.indexOf(node), 1);
      node.parentElement = null;
    },
    prepend(child) { return node.insertBefore(child, node.children[0] ?? null); },
    matches(selector) {
      return String(selector).split(',').some((chain) => matchesChain(node, chain, null));
    },
    // `:scope` resolves to the node the selector was called on, exactly as the
    // real API does — the gate relies on it to find a view's own direct children.
    querySelectorAll(selector) { return selectNodes(descendants(node), selector, node); },
    querySelector(selector) { return node.querySelectorAll(selector)[0] ?? null; },
    get previousElementSibling() {
      const parent = node.parentElement;
      if (!parent) return null;
      return parent.children[parent.children.indexOf(node) - 1] ?? null;
    },
    addEventListener() {},
    innerHTML: '',
    textContent: '',
  };
  return node;
}

function descendants(node) {
  const out = [];
  for (const child of node.children) { out.push(child, ...descendants(child)); }
  return out;
}

function selectNodes(nodes, selector, scope = null) {
  return nodes.filter((node) => String(selector).split(',').some((chain) => matchesChain(node, chain, scope)));
}

/** A document containing the parts of the shell the gate touches.
 *  `profile` mirrors the `data-build-profile` the generator writes onto `<html>`;
 *  it defaults to the deployed artifact. */
function gateDoc(profile = 'production') {
  const doc = makeNode('#document');
  doc.documentElement = makeNode('html');
  doc.documentElement.dataset.buildProfile = profile;
  doc.createElement = (tag) => makeNode(tag);
  doc.getElementById = (id) => descendants(doc).find((node) => node.id === id) ?? null;
  doc.querySelectorAll = (sel) => selectNodes(descendants(doc), sel);
  doc.querySelector = (sel) => doc.querySelectorAll(sel)[0] ?? null;
  doc._listeners = [];
  doc.addEventListener = (type) => doc._listeners.push(type);

  const view = (id, ...children) => {
    const node = makeNode('section', { id: `view-${id}`, classes: ['view'] });
    doc.appendChild(node);
    for (const child of children) node.appendChild(child);
    return node;
  };
  const el = (tag, opts) => makeNode(tag, opts);

  const first = el('p', { attrs: { 'data-demo-value': '1,248' } });
  first.textContent = '—';
  const note = el('small', { attrs: { 'data-stat-note': '' } });

  const plotSide = el('div', { classes: ['plot-side'] });
  const sideSide = el('div', { classes: ['side-side'] });
  const explorer = el('div', { classes: ['explorer'] });
  explorer.appendChild(plotSide);
  explorer.appendChild(sideSide);
  view('overview', el('div', { classes: ['breadcrumb'] }), el('div', { classes: ['hero'] }),
    el('div', { classes: ['stats'] }), explorer, el('div', { classes: ['tablewrap'] }),
    first, note);

  view('models', el('div', { classes: ['columns'] }));

  // Mirrors the real order in `#view-api`: stats, the Endpoints table, then the
  // `.columns.api-keys` block that holds both the key table and the SDK aside.
  const apiKeys = el('div', { classes: ['columns', 'api-keys'] });
  apiKeys.appendChild(el('table', { classes: ['keytable'] }));
  view('api', el('div', { classes: ['stats'] }), el('div', { classes: ['tablewrap'] }), apiKeys);

  const atlasLive = el('div', { attrs: { 'data-live-only': '' } });
  view('atlas', el('div', { classes: ['breadcrumb'] }), el('div', { classes: ['hero'] }),
    el('div', { classes: ['atlas-metrics'] }), el('div', { classes: ['atlas-stage-grid'] }), atlasLive);

  return doc;
}

const notices = (doc) => doc.querySelectorAll('.gated-inline');

test('live mode marks every sample region and leaves a stand-in with a reason', () => {
  const doc = gateDoc();
  const mode = applyDataMode({ doc, search: '' });

  assert.equal(mode, DATA_MODES.LIVE);
  assert.equal(doc.documentElement.dataset.mode, 'live');
  for (const { sel } of SAMPLE_REGIONS) {
    for (const node of doc.querySelectorAll(sel)) {
      assert.equal(node.dataset.demoOnly, '1', `${sel} survived live mode unmarked`);
    }
  }
  // One stand-in per region family — not one per matched node.
  assert.ok(notices(doc).length >= SAMPLE_REGIONS.length - 1,
    'each gated region family should be led by a stand-in');
  for (const notice of notices(doc)) {
    assert.match(notice.innerHTML, /待接线/);
    assert.doesNotMatch(notice.innerHTML, /data-demo-switch/,
      'the production artifact must not build a way into the samples');
  }
});

test('only the QA artifact builds the sample-mode entry into a stand-in', () => {
  const qa = gateDoc('qa');
  assert.equal(applyDataMode({ doc: qa, search: '' }), DATA_MODES.LIVE,
    'offerring the entry is not the same as painting the samples');
  const found = notices(qa);
  assert.ok(found.length > 0, 'the QA build still shows the stand-ins');
  for (const notice of found) {
    assert.match(notice.innerHTML, /data-demo-switch/, 'the QA stand-in offers the sample mode');
  }

  // The same markup, built as production, offers none of it — and the entry is
  // absent, not hidden: there is no node for a stylesheet to reveal.
  const prod = gateDoc('production');
  applyDataMode({ doc: prod, search: '' });
  assert.ok(notices(prod).length > 0, 'production still shows the honest stand-ins');
  for (const notice of notices(prod)) {
    assert.doesNotMatch(notice.innerHTML, /data-demo-switch/);
  }
});

test('the parked sample values are not painted on a live URL', () => {
  const doc = gateDoc();
  applyDataMode({ doc, search: '' });
  const parked = doc.querySelector('[data-demo-value]');
  assert.equal(parked.textContent, '—', 'a live first paint must not show the sample number');
  assert.equal(doc.querySelector('[data-stat-note]').textContent, '待授权');
});

test('demo mode paints the samples and removes every stand-in', () => {
  const doc = gateDoc('qa');
  applyDataMode({ doc, search: '?demo=1' });
  assert.equal(doc.documentElement.dataset.mode, 'demo');
  assert.equal(doc.querySelector('[data-demo-value]').textContent, '1,248');
  assert.equal(notices(doc).length, 0, 'no 待接线 stand-in may sit beside a visible sample');

  // The same URL, on the artifact that ships, changes nothing at all.
  const prod = gateDoc('production');
  assert.equal(applyDataMode({ doc: prod, search: '?demo=1' }), DATA_MODES.LIVE);
  assert.equal(prod.querySelector('[data-demo-value]').textContent, '—',
    'production must not paint the sample number, whatever the URL says');
});

test('a grid that lost its sample column collapses instead of leaving it blank', () => {
  const live = gateDoc();
  applyDataMode({ doc: live, search: '' });
  assert.equal(live.querySelector('#view-overview .explorer').classList.contains(COLLAPSED_CLASS), true);

  const demo = gateDoc('qa');
  applyDataMode({ doc: demo, search: '?demo=1' });
  assert.equal(demo.querySelector('#view-overview .explorer').classList.contains(COLLAPSED_CLASS), false);
});

test('the Atlas body keeps its hero and a declared live panel, and gates the rest', () => {
  const doc = gateDoc();
  applyDataMode({ doc, search: '' });
  assert.equal(doc.querySelector('#view-atlas .breadcrumb').dataset.demoOnly, undefined);
  assert.equal(doc.querySelector('#view-atlas .hero').dataset.demoOnly, undefined);
  assert.equal(doc.querySelector('#view-atlas [data-live-only]').dataset.demoOnly, undefined,
    'a panel that declares itself measured must survive the gate');
  assert.equal(doc.querySelector('#view-atlas .atlas-stage-grid').dataset.demoOnly, '1');
});

test('applying the gate twice does not stack a second stand-in', () => {
  const doc = gateDoc();
  applyDataMode({ doc, search: '' });
  const first = notices(doc).length;
  applyDataMode({ doc, search: '' });
  assert.equal(notices(doc).length, first, 'the gate must be idempotent');
});
