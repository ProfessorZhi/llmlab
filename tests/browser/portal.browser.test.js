/*! tests/browser/portal.browser.test.js
 *  Real-browser acceptance for the extracted front-end, driven over CDP.
 *
 *  The load-bearing test here is `matches the frozen prototype`: it renders the
 *  original single-file prototype and the extracted web/ build in the same
 *  browser at the same viewport and compares resolved computed styles plus
 *  Atlas structure per view. That is what turns "the refactor is faithful" from
 *  a claim into a measurement.
 *
 *  Run with: npm run test:browser   (needs Chrome; set CHROME_PATH to override)
 */
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { startServer } from '../../web/tools/serve.mjs';
import { findChrome, launchBrowser, openPage } from './harness.mjs';

const REPO_ROOT = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const CHROME = findChrome();
const SKIP = !CHROME && 'no Chrome/Chromium found — set CHROME_PATH to run browser checks';

let session = null;
let booting = null;
let page = null;

/**
 * Start the shared browser lazily and tear it down on failure. Doing this in a
 * `before` hook would leave a live Chrome and an open socket behind if setup
 * threw, which hangs the whole run instead of reporting one clear error.
 */
async function ready() {
  if (!booting) {
    booting = (async () => {
      // Serve the repo root so the prototype and the extracted build are both
      // reachable from one origin. Loopback only.
      const server = await startServer({ root: REPO_ROOT, port: 0 });
      const browser = await launchBrowser({ chromePath: CHROME });
      page = await openPage(browser.cdp, { width: 1440, height: 900 });
      session = { server, browser, page };
      return session;
    })();
    booting.catch(() => shutdown());
  }
  return booting;
}

async function shutdown() {
  const s = session;
  session = null;
  if (!s) return;
  await s.browser?.close().catch(() => {});
  await s.server?.close().catch(() => {});
}

after(shutdown);

/** Every check needs the shared browser; boot once, reuse the tab. */
const t = (name, fn) => test(name, { skip: SKIP }, async () => {
  await ready();
  return fn();
});

const url = (path) => `${session.server.origin}${path}`;
// The frozen visual authority, and the versioned successor the build is
// actually generated from. Both are compared, for different reasons:
//   * v5 is the design language that must not drift;
//   * v6 is v5 + the authored Portal additions, and is what `web/` slices.
// Comparing the build only against v5 would fail on the things 06 deliberately
// adds (an eighth view, the document title); comparing only against v6 would
// stop guarding the 05 visual language. So: does the successor keep 05, and does
// the build match its successor?
const PROTOTYPE = '/prototypes/v5/index.html';
const SUCCESSOR = '/prototypes/v6/index.html';
const BUILD = '/web/index.html';
// The isolated QA preview — the only artifact where `?demo=1` reaches the
// samples. See web/js/build-profile.js.
const QA = '/web/qa.html';

// --------------------------------------------------------------- page probes

const STYLE_SELECTORS = [
  'body', '.brandname', '.hero h1', '.demo-strip', '.badge', '#themeButton',
  '#atlasMainChart', '#atlasDotCanvas', '#atlasDetail', '#atlasRankRows',
  '#atlasPareto', '#atlasHeatmap', '.atlas-metric strong', '.atlas-rail',
];

/** Computed styles of a fixed selector list, plus every :root custom property. */
const STYLE_PROBE = `(() => {
  const props = ['background-color','color','font-size','font-family','font-weight',
    'letter-spacing','border-bottom-color','border-top-color','border-left-color',
    'border-radius','padding','margin','fill','stroke'];
  const root = getComputedStyle(document.documentElement);
  const vars = [];
  for (const name of root) if (name.startsWith('--')) vars.push(name + '=' + root.getPropertyValue(name).trim());
  const styles = {};
  for (const sel of ${JSON.stringify(STYLE_SELECTORS)}) {
    const node = document.querySelector(sel);
    if (!node) { styles[sel] = null; continue; }
    const cs = getComputedStyle(node);
    const out = {};
    for (const p of props) { const v = cs.getPropertyValue(p); if (v) out[p] = v; }
    styles[sel] = out;
  }
  return { vars: vars.sort(), styles, title: document.title };
})()`;

/** Structure of whichever Atlas view is currently selected. */
const VIEW_PROBE = `(() => {
  const svg = document.querySelector('#atlasMainChart');
  const canvas = document.querySelector('#atlasDotCanvas');
  const paint = (nodes) => [...nodes].map((n) => n.getAttribute('fill')).join(',');
  const out = {
    // SVGElement has no .hidden property — it is an attribute here.
    svgHidden: svg.hasAttribute('hidden'),
    canvasHidden: canvas.hidden,
    svgPaths: svg.querySelectorAll('path').length,
    svgRects: svg.querySelectorAll('rect').length,
    svgHitAreas: svg.querySelectorAll('.atlas-hit').length,
    svgText: svg.querySelectorAll('text').length,
    donutSegments: document.querySelectorAll('#atlasDonut path').length,
    legendItems: document.querySelectorAll('#atlasLegend > *').length,
    rankRows: document.querySelectorAll('#atlasRankRows > *').length,
    heatCells: document.querySelectorAll('#atlasHeatmap rect[data-heat-day]').length,
    compareHidden: document.querySelector('#atlasCompare').hidden,
    pressed: [...document.querySelectorAll('[data-atlas-view]')]
      .map((b) => b.dataset.atlasView + ':' + b.getAttribute('aria-pressed')).join(' '),
    kpis: ['atlasCalls','atlasActive','atlasTokens','atlasP95','atlasCallsNote','atlasScope','atlasBasis']
      .map((id) => (document.getElementById(id) ? document.getElementById(id).textContent : '')).join('|'),
    fills: paint(svg.querySelectorAll('path,rect,circle,text')),
    canvas: null,
  };
  if (!canvas.hidden && canvas.width) {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
    let painted = 0;
    for (let i = 3; i < data.length; i += 4) if (data[i] > 0) painted++;
    out.canvas = { w: canvas.width, h: canvas.height, painted };
  }
  return out;
})()`;

/** The Atlas only has a layout box while its view is the active one.
 *  Both conditions are waited for (never slept on) because the router commits
 *  the page inside a view transition, which a loaded headless runner may run
 *  several frames late. */
async function showAtlas() {
  await page.evaluate(`document.querySelector('[data-page="atlas"]').click(); true`);
  const active = await page.waitFor(`document.querySelector('#view-atlas').classList.contains('active')`);
  const sized = await page.waitFor(`document.querySelector('#atlasMainChart').getBoundingClientRect().width > 0`);
  if (!active || !sized) {
    throw new Error(`could not bring the Atlas view on screen (active=${active}, sized=${sized})`);
  }
}

/**
 * Wait until the selected Atlas view has actually drawn, instead of sleeping a
 * fixed number of milliseconds and hoping a frame landed.
 *
 * The dot matrix is painted on `requestAnimationFrame`, and under a loaded CDP
 * runner that frame can be arbitrarily late — the canvas then still reports its
 * 300×150 default and zero painted pixels. The condition asserts the real
 * outcome: the canvas is sized to its layout box and has ink on it. The SVG
 * views draw synchronously, but they are waited on too so the measurement never
 * races a renderer.
 */
async function waitForViewPainted(view) {
  const ready = view === 'dots'
    ? `(() => {
        const c = document.querySelector('#atlasDotCanvas');
        if (!c || c.hidden || !c.width || !c.height) return false;
        const box = c.getBoundingClientRect();
        if (c.width !== Math.max(1, Math.round(box.width))) return false;
        const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        for (let i = 3; i < data.length; i += 4) if (data[i] > 0) return true;
        return false;
      })()`
    : `document.querySelector('#atlasMainChart ${view === 'sunburst' ? 'path' : 'rect'}') !== null`;
  const painted = await page.waitFor(ready);
  if (!painted) throw new Error(`the ${view} view never finished drawing`);
}

/**
 * Fully measure one build: styles once, then structure for each Atlas view.
 *
 * `query` exists because the Atlas is a *design* made of authored numbers. The
 * frozen prototypes render it unconditionally — that is what they are — while
 * the product build shows it only behind the explicit `?demo=1` opt-in. The
 * parity comparison therefore has to put the build in the same design mode the
 * prototypes are always in; the live default is asserted negatively instead, in
 * `operations-first.browser.test.js`.
 */
async function measure(path, query = '') {
  await page.goto(url(path + query));
  const style = await page.evaluate(STYLE_PROBE);
  await showAtlas();
  const views = {};
  for (const view of ['sunburst', 'treemap', 'dots']) {
    await page.evaluate(`document.querySelector('[data-atlas-view="${view}"]').click(); true`);
    await page.waitFor(`document.querySelector('[data-atlas-view="${view}"]').getAttribute('aria-pressed') === 'true'`);
    await waitForViewPainted(view);
    views[view] = await page.evaluate(VIEW_PROBE);
  }
  await page.evaluate(`document.querySelector('[data-atlas-view="sunburst"]').click(); true`);
  return { style, views };
}

// ------------------------------------------------------------------- asserts

t('boots from local files only and exposes the prototype test hooks', async () => {
  await page.goto(url(BUILD));
  const info = await page.evaluate(`(() => ({
    page: window.__ATELIER__.page,
    version: window.__ATELIER__.version,
    theme: window.__ATELIER__.theme,
    reducedMotion: typeof window.__ATELIER__.reducedMotion,
    source: window.__ATELIER__.source,
    dataSource: document.documentElement.dataset.dataSource,
    synthetic: document.documentElement.dataset.synthetic,
    views: document.querySelectorAll('.view').length,
    primaryNav: document.querySelectorAll('.topnav [data-page]').length,
    hasComputePrimaryNav: !!document.querySelector('.topnav [data-page="compute"]'),
    atlasRows: window.__ATLAS__.rows.length,
    totals: window.__ATLAS__.totals,
    bootError: window.__ATELIER_BOOT_ERROR__ || null,
  }))()`);

  assert.equal(info.bootError, null, 'boot must not have failed');
  assert.equal(info.page, 'overview');
  assert.equal(info.version, '6.0.0');
  assert.equal(info.theme, 'light', 'the page opens light, as in the prototype');
  assert.equal(info.views, 9, 'eight generated views plus a runtime-mounted Compute subpage');
  assert.equal(info.primaryNav, 8, 'Compute is a nested view, not a ninth primary navigation item');
  assert.equal(info.hasComputePrimaryNav, false);
  assert.equal(info.dataSource, 'unknown', 'plain URL must not select synthetic Atlas data');
  assert.deepEqual(info.source, {
    state: 'unknown',
    synthetic: false,
    reason: 'no-endpoint-configured',
    notes: [],
    rows: 0,
  });
  assert.equal(info.synthetic, 'false');
  assert.equal(info.atlasRows, 0, 'no synthetic workloads may exist in default live memory');
  assert.deepEqual(page.exceptions, []);
});

t('the Atlas renders the same numbers, structure and styles as the frozen prototype', async () => {
  // The prototypes are the design and always draw it; the build draws it only
  // when asked. Compare like with like by opting the build into design mode, so
  // this stays a parity assertion over the same numerical fixture rather than a
  // comparison against an empty frame.
  const proto = await measure(PROTOTYPE);
  const successor = await measure(SUCCESSOR);
  const build = await measure(QA, '?demo=1');

  // Every :root custom property, resolved, against the build's own source.
  // Catches any cascade-order slip introduced by splitting one <style> block
  // into three files.
  //
  // Until V6.2 this also required v6 to resolve to exactly the 05 tokens. The
  // product owner then asked for an Apple-style look (2026-10-16), which v6
  // declares as one CSS layer in prototypes/v6/additions.css. So the parity is
  // now build == v6, and v6 must carry that declared layer — not whatever
  // tokens happen to resolve. The Atlas *structure* parity with 05 below is
  // unchanged: the redesign recolours the charts, it does not redraw them.
  assert.deepEqual(build.style.vars, successor.style.vars, 'resolved :root custom properties differ from prototypes/v6');
  assert.deepEqual(build.style.styles, successor.style.styles, 'computed styles differ from prototypes/v6');
  const declared = new Set(successor.style.vars);
  for (const token of ['--bg=#f5f5f7', '--accent=#0071e3', '--atlas-a=#0071e3']) {
    assert.ok(declared.has(token), `v6 must declare the Apple layer token ${token}`);
  }
  assert.notDeepEqual(successor.style.vars, proto.style.vars, 'the Apple layer is expected to differ from 05');
  // …and the build matches its own source, including the version it names.
  assert.equal(build.style.title, successor.style.title, 'document title differs from prototypes/v6');

  // Per-view structure, including canvas pixel coverage for the dot renderer.
  // Structure (paths, rects, hit areas, text, KPIs, pixel coverage) must match
  // 05 exactly; fill colours must match v6, which recolours the same drawing.
  const shape = ({ fills, ...rest }) => rest;
  for (const view of ['sunburst', 'treemap', 'dots']) {
    assert.deepEqual(shape(build.views[view]), shape(proto.views[view]), `Atlas "${view}" structure differs from the 05 prototype`);
    assert.deepEqual(build.views[view], successor.views[view], `Atlas "${view}" view differs from prototypes/v6`);
  }

  assert.ok(proto.views.sunburst.svgPaths > 0, 'prototype sunburst should draw');
  assert.ok(proto.views.treemap.svgRects > 0, 'prototype treemap should draw');
  assert.ok(proto.views.dots.canvas.painted > 0, 'prototype dot renderer should paint');
});

t('exactly one Atlas layer is visible at a time', async () => {
  // The layers are part of the Atlas design, so this runs in design mode; the
  // live default is asserted in operations-first.browser.test.js instead.
  await page.goto(url(`${QA}?demo=1`));
  await showAtlas();
  for (const view of ['sunburst', 'treemap', 'dots']) {
    await page.evaluate(`document.querySelector('[data-atlas-view="${view}"]').click(); true`);
    const s = await page.evaluate(VIEW_PROBE);
    assert.notEqual(s.svgHidden, s.canvasHidden, `view ${view}: svg/canvas visibility must be mutually exclusive`);
    if (view === 'dots') {
      assert.equal(s.svgHidden, true, 'the dot view hides the svg');
      assert.equal(s.canvasHidden, false);
    } else {
      assert.equal(s.svgHidden, false, `${view} draws in the svg`);
      assert.equal(s.canvasHidden, true, `${view} hides the dot canvas`);
    }
  }
});

t('the atlas controls drive the same state the prototype hook reports', async () => {
  // This exercises the Atlas widget's own semantics (group filter, range scaling,
  // search, reset), which need rows to act on. A bare production URL is
  // deliberately unconfigured and holds none, so the widget is driven on the
  // sanctioned synthetic fixture — the same `?demo=1` opt-in its siblings use.
  await page.goto(url(`${QA}?demo=1`));
  await showAtlas();

  await page.evaluate(`document.querySelector('[data-atlas-group="developer"]').click(); true`);
  let snap = await page.evaluate('window.__ATLAS__.state');
  assert.equal(snap.group, 'developer');
  assert.equal((await page.evaluate('window.__ATLAS__.totals')).rows, 3, 'the developer group holds three workloads');

  await page.evaluate(`document.querySelector('[data-atlas-range="7d"]').click(); true`);
  snap = await page.evaluate('window.__ATLAS__.state');
  assert.equal(snap.range, '7d');
  const scaled = await page.evaluate('window.__ATLAS__.totals.calls');
  assert.ok(scaled > 1248, 'the 7-day range scales the counts up');

  await page.evaluate(`window.__ATLAS__.setGroup('all'); true`);
  assert.equal((await page.evaluate('window.__ATLAS__.totals')).rows, 9);

  await page.evaluate(`document.querySelector('#atlasSearch').value = 'zzz-no-match'; document.querySelector('#atlasSearch').dispatchEvent(new Event('input')); true`);
  assert.match(await page.evaluate(`document.getElementById('atlasRankRows').textContent`), /没有匹配/);

  await page.evaluate(`window.__ATLAS__.reset(); true`);
  const after = await page.evaluate(`({ rows: window.__ATLAS__.totals.rows, ranked: document.querySelectorAll('#atlasRankRows > *').length })`);
  assert.deepEqual(after, { rows: 9, ranked: 9 });
});

t('switching theme repaints the charts from the dark palette', async () => {
  await page.goto(url(`${QA}?demo=1`)); // the charts exist in design mode
  await showAtlas();

  const snapshot = `(() => {
    const svg = document.querySelector('#atlasMainChart');
    const attrs = (sel) => [...svg.querySelectorAll(sel)].map((n) => n.getAttribute('fill') + '/' + n.getAttribute('stroke')).join(',');
    return {
      theme: document.documentElement.dataset.theme,
      bg: getComputedStyle(document.body).backgroundColor,
      ink: getComputedStyle(document.documentElement).getPropertyValue('--ink').trim(),
      textPaint: attrs('text'),
      shapes: attrs('path,rect,circle'),
    };
  })()`;

  const light = await page.evaluate(snapshot);
  assert.equal(light.theme, 'light');

  await page.evaluate(`document.querySelector('#themeButton').click(); true`);
  await page.waitFor(`document.documentElement.dataset.theme === 'dark'`);
  // Atlas paints SVG attributes in the next frame; a fixed 250ms sleep is
  // unreliable under loaded CDP runners. Wait for the *actual* repaint instead —
  // and for the body background too, which animates over `transition:background
  // .22s` and would otherwise still read the light value the instant the SVG
  // flips, making this assertion fail on a correct theme.
  await page.waitFor(`(() => {
    const svg = document.querySelector('#atlasMainChart');
    if (!svg) return false;
    const attrs = (sel) => [...svg.querySelectorAll(sel)]
      .map((n) => n.getAttribute('fill') + '/' + n.getAttribute('stroke')).join(',');
    return attrs('text') !== ${JSON.stringify(light.textPaint)}
      && attrs('path,rect,circle') !== ${JSON.stringify(light.shapes)}
      && getComputedStyle(document.body).backgroundColor !== ${JSON.stringify(light.bg)};
  })()`, { timeoutMs: 8000 });

  const dark = await page.evaluate(snapshot);
  assert.equal(dark.theme, 'dark');
  assert.notEqual(dark.bg, light.bg, 'body background should follow the theme token');
  assert.notEqual(dark.ink, light.ink, 'the --ink token itself should change');
  assert.notEqual(dark.textPaint, light.textPaint,
    'labels are painted from :root values, so the chart must be redrawn on a theme change');
  assert.notEqual(dark.shapes, light.shapes, 'shape paint must follow the theme too');
});

t('every view commits, one at a time', async () => {
  await page.goto(url(BUILD));
  const names = ['overview', 'models', 'api', 'calls', 'observability', 'atlas', 'evaluation', 'admin'];
  for (const name of names) {
    await page.evaluate(`document.querySelector('[data-page="${name}"]').click(); true`);
    await page.waitFor(`document.querySelector('#view-${name}').classList.contains('active')`);
    const s = await page.evaluate(`(() => ({
      active: [...document.querySelectorAll('.view.active')].map((v) => v.id),
      current: [...document.querySelectorAll('[data-page][aria-current="page"]')].map((b) => b.dataset.page),
      title: document.title,
    }))()`);
    assert.deepEqual(s.active, [`view-${name}`], `only #view-${name} should be active`);
    assert.ok(s.current.includes(name), `${name} should be marked current`);
    assert.ok(s.title.length > 0);
  }
});

t('the legacy /#/compute deep link activates the real Compute surface', async () => {
  await page.goto(url(BUILD + '#/compute'));
  const active = await page.waitFor("document.querySelector('#view-compute')?.classList.contains('active') === true");
  assert.equal(active, true);
  const evidence = await page.evaluate("({ title: document.title, mode: document.querySelector('[data-compute-provenance]')?.textContent, metric: document.querySelector('[data-compute-metric=\"gpu_vram_used_mib\"]')?.textContent })");
  assert.match(evidence.title, /算力中心/);
  assert.match(evidence.mode, /UNKNOWN/, 'with no source configured the provenance must read UNKNOWN');
  assert.doesNotMatch(evidence.mode, /LIVE/, 'and must never claim a live reading');
  assert.equal(evidence.metric, 'UNKNOWN');
  const nav = await page.evaluate('([...document.querySelectorAll(\"[data-page][aria-current=page]\")].map(n=>n.dataset.page))');
  assert.ok(nav.includes('overview'), 'Compute must keep Overview primary tab active');
  assert.equal(nav.includes('compute'), false);
  await page.evaluate('document.querySelector(\"#view-compute [data-go=overview]\").click(); true');
  const back = await page.waitFor('document.querySelector(\"#view-overview\").classList.contains(\"active\")');
  assert.equal(back, true);
});

t('responsive: the nav swaps at 1050px and the atlas still lays out at every width', async () => {
  const widths = [320, 390, 768, 1024, 1440];
  const seen = {};
  for (const w of widths) {
    await page.setViewport(w, 900);
    await page.goto(url(`${QA}?demo=1`)); // lay the design out, not the empty frame
    await showAtlas();
    seen[w] = await page.evaluate(`(() => {
      const top = document.querySelector('.topnav');
      const mobile = document.querySelector('.mobile-nav');
      const frame = document.querySelector('#atlasChartFrame').getBoundingClientRect();
      const chart = document.querySelector('#atlasMainChart').getBoundingClientRect();
      return {
        topnav: getComputedStyle(top).display,
        mobileNav: getComputedStyle(mobile).display,
        stripTail: getComputedStyle(document.querySelector('.demo-strip span:last-child')).display,
        frameWidth: Math.round(frame.width),
        chartWidth: Math.round(chart.width),
        chartHeight: Math.round(chart.height),
        docScroll: document.documentElement.scrollWidth,
        inner: window.innerWidth,
      };
    })()`);
  }

  for (const w of [320, 390, 768, 1024]) {
    assert.equal(seen[w].topnav, 'none', `${w}px: the desktop nav is hidden`);
    assert.equal(seen[w].mobileNav, 'flex', `${w}px: the mobile nav is shown`);
  }
  assert.notEqual(seen[1440].topnav, 'none', '1440px: the desktop nav is shown');
  assert.equal(seen[1440].mobileNav, 'none', '1440px: the mobile nav is hidden');

  assert.equal(seen[390].stripTail, 'none', 'the demo strip drops its tail on phones');

  for (const w of widths) {
    assert.ok(seen[w].frameWidth > 0, `${w}px: the atlas frame has width`);
    assert.ok(seen[w].chartWidth > 0 && seen[w].chartHeight > 0, `${w}px: the chart has a box`);
    assert.ok(
      seen[w].docScroll <= seen[w].inner + 1,
      `${w}px: the document overflows horizontally (${seen[w].docScroll} > ${seen[w].inner})`,
    );
  }
});

t('?source=remote with no endpoint fails closed instead of showing the fixture', async () => {
  const before = page.requests.length;
  await page.goto(url(`${BUILD}?source=remote`));
  // `goto` only guarantees the shell globals exist. Source resolution settles a
  // tick later, so reading the DOM straight away can catch it mid-boot. Wait for
  // the documented fail-closed state itself — polling a condition, not a fixed
  // delay — then assert against that settled state.
  const settled = await page.waitFor(
    `window.__ATELIER__?.source?.state === 'unknown'`
    + ` && window.__ATELIER__.source.reason === 'no-endpoint-configured'`
    + ` && document.documentElement.dataset.dataSource === 'unknown'`,
  );
  assert.equal(settled, true, 'the unconfigured Atlas source never reached its settled state');

  const info = await page.evaluate(`(() => ({
    source: window.__ATELIER__.source,
    dataSource: document.documentElement.dataset.dataSource,
    rows: window.__ATLAS__.rows.length,
    kpis: ['atlasCalls','atlasActive','atlasTokens','atlasP95'].map((id) => document.getElementById(id).textContent),
    scope: document.getElementById('atlasScope').textContent,
    note: document.getElementById('atlasCallsNote').textContent,
    detail: document.getElementById('atlasDetail').textContent,
    chartText: document.getElementById('atlasMainChart').textContent,
    rankText: document.getElementById('atlasRankRows').textContent,
    breadcrumb: document.getElementById('atlasBreadcrumb').textContent,
  }))()`);

  assert.equal(info.source.state, 'unknown');
  assert.equal(info.source.synthetic, false);
  assert.equal(info.source.reason, 'no-endpoint-configured');
  assert.equal(info.dataSource, 'unknown');
  assert.equal(info.rows, 0, 'the synthetic fixture must not leak into a remote request');
  assert.deepEqual(info.kpis, ['—', '—', '—', '—'], 'missing metrics render as "—", never as 0');
  assert.match(info.note, /no-endpoint-configured/);
  assert.match(info.detail, /UNKNOWN/);
  assert.match(info.chartText, /UNKNOWN/, 'the main chart must say why it is empty');
  assert.match(info.rankText, /UNKNOWN/, 'the ranking must say why it is empty');
  assert.ok(info.breadcrumb.length > 0);

  // This query controls Atlas alone: calls/modules are independently scoped
  // same-origin views and legitimately attempt their own authenticated GET.
  // Keep the hard boundary: zero external egress, no unconfigured Atlas fetch,
  // and only the shell's documented same-origin endpoints.
  const added = page.requests.slice(before);
  const allowed = (value) => {
    if (value.startsWith('data:image/svg+xml')) return true; // Chrome favicon
    let requested;
    try { requested = new URL(value); } catch { return false; }
    if (requested.origin !== session.server.origin) return false;
    const path = requested.pathname;
    return path === '/web/index.html'
      || path.startsWith('/web/js/')
      || path.startsWith('/web/css/')
      || path === '/favicon.ico'
      || path === '/api/atlas/calls'
      || path === '/api/atlas/modules'
      || path === '/api/infra/summary' // the verified host read-out (404 by default)
      // The API page's read-only scoped-token list (backend/auth tokens_list,
      // delegated through the BFF's /auth/ prefix). GET only; never a write.
      || path === '/auth/tokens';
  };
  assert.ok(added.length > 0, 'sanity: the page did reload its own files');
  assert.deepEqual(
    added.filter((u) => !allowed(u)), [],
    'unconfigured Atlas must not trigger external or unsupported data requests',
  );
  assert.equal(added.some((u) => u.includes('/api/atlas/usage')), false,
    'the unconfigured Atlas usage request must never be issued');
});

t('a remote payload that admits to being synthetic is rejected', async () => {
  // Serve a synthetic-flagged payload from the same origin and point the page
  // at it through the documented host hook. This is the exact failure mode the
  // contract exists to stop: fixtures dressed up as production numbers.
  const { origin } = session.server;
  await page.goto(url(BUILD));
  const verdict = await page.evaluate(`(async () => {
    const mod = await import('${origin}/web/js/data/contract.js');
    const real = mod.validateAtlasBundle({ synthetic: true, groups: [{ id: 'g', calls: 5 }] });
    const bad = mod.validateAtlasBundle({ synthetic: false, groups: [{ id: 'g', calls: 'lots' }] });
    const good = mod.validateAtlasBundle({ synthetic: false, groups: [{ id: 'g', calls: 5, workloads: [{ id: 'w', calls: 5, p95: null, tokens: null }] }] });
    return {
      realOk: real.ok, realState: real.state.state,
      badOk: bad.ok, badState: bad.state.state,
      goodOk: good.ok, goodState: good.state.state, goodSynthetic: good.state.synthetic,
      goodP95: good.state.workloads[0].p95,
    };
  })()`, { awaitPromise: true });

  assert.equal(verdict.realOk, false, 'a payload flagged synthetic=true must be refused as remote data');
  assert.equal(verdict.realState, 'unknown');
  assert.equal(verdict.badOk, false);
  assert.equal(verdict.badState, 'unknown', 'a non-numeric metric invalidates the payload');
  assert.equal(verdict.goodOk, true);
  assert.equal(verdict.goodState, 'live');
  assert.equal(verdict.goodSynthetic, false);
  assert.equal(verdict.goodP95, null, 'an absent metric stays null, never 0');
});

// Declared last on purpose: requests accumulate for the life of the tab, so
// this assertion covers every navigation the file performed.
t('the page never requests anything outside loopback', async () => {
  assert.ok(page.requests.length > 0, 'sanity: the page did load local files');
  const external = page.externalRequests();
  assert.deepEqual(external, [], `unexpected external requests: ${external.join(', ')}`);
  assert.deepEqual(page.consoleErrors, []);
  assert.deepEqual(page.exceptions, []);
});
