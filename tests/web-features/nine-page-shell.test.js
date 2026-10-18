/*! tests/web-features/nine-page-shell.test.js — the nine logical pages.
 *
 *  Issue #44 is framed as "four of nine pages are a wall of 待接线". Whatever
 *  content each page still owes (that part depends on the BFF contracts owned by
 *  #45–#49), the *shell* has to be complete and honest first. This file pins the
 *  part of that which a browserless host can actually check:
 *
 *    * all nine routes exist and resolve, including the legacy `#/compute`
 *      spelling of the Compute subpage;
 *    * every top-level page is reachable from both navigation rails, and every
 *      page carries the heading and breadcrumb that give it its place;
 *    * every page has a content anchor — something other than the stand-in;
 *    * every stand-in on screen offers the way out of itself, because a gate
 *      with no door is just a dead end with an explanation.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  ALL_ROUTES,
  PAGES,
  PAGE_NAMES,
  SUBPAGES,
  pageFromHash,
  pageParent,
} from '../../web/js/router.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const WEB = readFileSync(join(REPO, 'web', 'index.html'), 'utf8').replace(/\r\n/g, '\n');

/** Slice one `#view-<id>` section out of the shipped markup. */
function sectionOf(html, viewId) {
  const idAt = html.indexOf(`id="view-${viewId}"`);
  if (idAt < 0) return null;
  const start = html.lastIndexOf('<section', idAt);
  let cursor = start;
  let depth = 0;
  while (cursor >= 0 && cursor < html.length) {
    const nextOpen = html.indexOf('<section', cursor);
    const nextClose = html.indexOf('</section>', cursor);
    if (nextClose < 0) return null;
    if (nextOpen >= 0 && nextOpen < nextClose) { depth += 1; cursor = nextOpen + '<section'.length; continue; }
    depth -= 1;
    if (depth === 0) return html.slice(start, nextClose);
    cursor = nextClose + '</section>'.length;
  }
  return null;
}

test('the nine routes are declared, named and resolvable', () => {
  assert.equal(PAGES.length, 8, 'eight top-level tabs');
  assert.deepEqual(Object.keys(SUBPAGES), ['compute'], 'Compute is the ninth page, as a subpage');
  assert.equal(ALL_ROUTES.length, 9);
  for (const route of ALL_ROUTES) {
    assert.ok(route in PAGE_NAMES, `${route} has no human name`);
    assert.equal(pageFromHash(`#${route}`), route, `#${route} must resolve to ${route}`);
    // The legacy Portal spelling has to keep working: old links are the reason
    // Compute is a subpage rather than a ninth tab.
    assert.equal(pageFromHash(`#/${route}`), route, `#/${route} must keep resolving`);
  }
  assert.equal(pageFromHash('#nonsense'), 'overview', 'an unknown hash falls back, never blanks');
  assert.equal(pageFromHash(''), 'overview');
  assert.equal(pageParent('compute'), 'overview', 'Compute highlights the Overview tab');
  assert.equal(pageParent('atlas'), 'atlas', 'a top-level page is its own parent');
});

test('every top-level page is a real view in the shipped shell', () => {
  for (const page of PAGES) {
    assert.ok(sectionOf(WEB, page), `#view-${page} is missing from web/index.html`);
  }
});

test('every page carries a heading and a breadcrumb', () => {
  for (const [page, name] of Object.entries(PAGE_NAMES)) {
    const section = sectionOf(WEB, pageParent(page));
    assert.ok(section, `#view-${pageParent(page)} is missing`);
    assert.match(section, /<h1\b/, `${page} has no h1`);
    assert.match(section, /class="breadcrumb"/, `${page} has no breadcrumb`);
    assert.ok(section.includes(name) || page === 'compute',
      `${page} never names itself in its own page ("${name}")`);
  }
});

test('every page is reachable from both navigation rails', () => {
  for (const page of PAGES) {
    const hits = WEB.match(new RegExp(`data-page="${page}"`, 'g')) ?? [];
    assert.ok(hits.length >= 2,
      `${page} is reachable from ${hits.length} control(s); the desktop and mobile rails both need it`);
  }
});

/** Something on the page that is not a stand-in: the reader should land on
 *  content, with the gates inside it, not on a gate as the whole page. */
test('no page is only a stand-in', () => {
  const ANCHORS = ['tablewrap', 'service-grid', 'stats', 'module-panel', 'explorer', 'surface',
    'flatpanel', 'columns', 'subnav', 'atlas-stage-grid', 'editorial-index'];
  for (const page of PAGES) {
    const section = sectionOf(WEB, page);
    assert.ok(ANCHORS.some((token) => new RegExp(`class="[^"]*\\b${token}\\b`).test(section)),
      `#view-${page} has no content anchor at all`);
  }
});

test('the static stand-in carries no way into the samples on the deployed artifact', () => {
  // The deployed page shows an honest "待接线" and stops there: there is nothing
  // behind it to open. The QA artifact builds the switch into the DOM at runtime
  // (see web/js/demo-mode.js), so the shipped HTML must not carry one — a
  // stand-in with a live control is exactly the dead end this replaced.
  const blocks = WEB.match(/<div class="gated-inline"[\s\S]*?<\/p>/g) ?? [];
  assert.ok(blocks.length >= 1, 'sanity: the shell ships at least one stand-in');
  for (const block of blocks) {
    assert.doesNotMatch(block, /data-demo-switch/,
      'the deployed artifact must not carry a sample-mode entry');
    assert.match(block, /class="gated-why"/, 'and must say why it is empty');
  }
});

test('the static stand-in uses the marker the gate looks for', () => {
  // `demo-mode.js` decides "have I already placed a stand-in here?" by the
  // *presence* of `data-gated`. The static one must match that shape, or the two
  // halves of the gate disagree about what a stand-in looks like. It used to be
  // written as `data-gated=""` while every guard tested `data-gated="1"`, which
  // is why none of the idempotency checks ever matched.
  assert.match(WEB, /class="gated-inline"[^>]*data-live-only[^>]*data-gated[\s>]/);
  assert.doesNotMatch(WEB, /data-gated="1"/, 'the marker carries no value; guards test presence');
});
