/*! tests/browser/operations-first.browser.test.js — live by default, in a real
 *  browser over CDP.
 *
 *  The claim under test is negative and easy to lose: on a bare URL (and on a
 *  bare URL carrying a tracking parameter) no fabricated KPI is on screen, and
 *  the sample values only appear when the reader asks for them.
 */
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { startServer } from '../../web/tools/serve.mjs';
import { findChrome, launchBrowser, openPage, sleep } from './harness.mjs';

const REPO_ROOT = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const CHROME = findChrome();
const SKIP = !CHROME && 'no Chrome/Chromium found — set CHROME_PATH to run browser checks';

const BUILD = '/web/index.html';
// The isolated QA preview — the only artifact where `?demo=1` reaches the
// samples. See web/js/build-profile.js.
const QA = '/web/qa.html';
const FABRICATED = ['1,248', '99.4%', '1.42 s', '482k', '8,412'];

let session = null;
let booting = null;
let page = null;

async function ready() {
  if (!booting) {
    booting = (async () => {
      const server = await startServer({ root: REPO_ROOT, port: 0, env: {} });
      const browser = await launchBrowser({ chromePath: CHROME });
      page = await openPage(browser.cdp, { width: 1440, height: 950 });
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

const t = (name, fn) => test(name, { skip: SKIP }, async () => {
  await ready();
  return fn();
});

const url = (p) => `${session.server.origin}${p}`;

let nav = 0;
const visit = async (search, base = BUILD) => {
  nav += 1;
  await page.goto(url(`${base}?qa=${nav}${search}`));
  await page.waitFor('!!window.__ATELIER__');
  await sleep(300);
};

const overview = () => page.evaluate(`(() => ({
  mode: document.documentElement.dataset.mode,
  text: document.querySelector('#view-overview').innerText,
  demoDisplay: getComputedStyle(document.querySelector('[data-demo-only]')).display,
  gatedVisible: !!document.querySelector('[data-live-only]') && getComputedStyle(document.querySelector('[data-live-only]')).display !== 'none',
}))()`);

t('the deployed artifact builds no sample-mode entry at all', async () => {
  await visit('');
  // Absent, not hidden. There is no node for a stylesheet to reveal and no
  // handler that could act on one — so the deployed page cannot be talked into
  // a demo by any means.
  assert.equal(await page.evaluate(`document.querySelectorAll('[data-demo-switch]').length`), 0,
    'the deployed page must not build a sample-mode entry');
  assert.ok(await page.evaluate(`document.querySelectorAll('[data-gated]').length > 0`),
    'sanity: the honest stand-ins are still there, just without an exit');
});

t('?demo=1 is inert on the deployed artifact', async () => {
  await visit('&demo=1'); // deliberately the production artifact, not QA
  const seen = await overview();
  assert.equal(seen.mode, 'live', 'the query asks; the build answers, and production says no');
  for (const value of FABRICATED) {
    assert.doesNotMatch(seen.text, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')),
      `${value} must not be visible even with ?demo=1`);
  }
  assert.equal(await page.evaluate(`window.__OPS__.mode`), 'unavailable',
    'and the ops source stays unavailable rather than reaching the fixture');
  assert.equal(await page.evaluate(`document.querySelectorAll('[data-demo-switch]').length`), 0);
});

t('a bare URL is live: no fabricated KPI is on screen', async () => {
  await visit('');
  const seen = await overview();
  assert.equal(seen.mode, 'live', 'live must be the default mode');
  for (const value of FABRICATED) {
    assert.doesNotMatch(seen.text, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), `${value} must not be visible`);
  }
  assert.equal(seen.demoDisplay, 'none', 'the sample block must be hidden');
  assert.equal(seen.gatedVisible, true, 'the gated stand-in must be shown instead');
});

t('a tracking parameter does not open the sample mode', async () => {
  await visit('&utm_source=chatgpt.com');
  const seen = await overview();
  assert.equal(seen.mode, 'live');
  for (const value of FABRICATED) assert.doesNotMatch(seen.text, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

t('?demo=1 alone shows the samples, clearly marked', async () => {
  await visit('&demo=1', QA);
  const seen = await overview();
  assert.equal(seen.mode, 'demo', 'the opt-in must set the demo mode');
  assert.match(seen.text, /1,248/, 'the samples are visible when asked for');
  const note = await page.evaluate(`document.querySelector('[data-stat-note]')?.textContent ?? ''`);
  assert.match(note, /设计样例|非实测/, 'a sample number must say it is a sample');
});

t('the account control reads as preview access, not a session', async () => {
  await visit('');
  const label = await page.evaluate(`document.querySelector('#accountButton .account-label').textContent`);
  assert.match(label, /预览访问/);
  assert.match(label, /未建立 Linux 会话/);
  await page.evaluate(`document.querySelector('#accountButton').click(); true`);
  await page.waitFor(`document.querySelector('#explainDialog')?.open === true`);
  const dialog = await page.evaluate(`(() => ({
    body: document.querySelector('#modalBody').textContent,
    passwordFields: document.querySelectorAll('#explainDialog input[type="password"]').length,
  }))()`);
  assert.match(dialog.body, /PAM/);
  assert.match(dialog.body, /不可访问/);
  assert.equal(dialog.passwordFields, 0, 'the preview must never render a password field');
  await page.evaluate(`document.querySelector('#explainDialog').close(); true`);
});

/* --------------------------------------------------------------- the Atlas
 *
 *  The Atlas is a whole design made of authored numbers, so it is gated as a
 *  body rather than region by region. The parity comparison against the frozen
 *  prototypes lives in `portal.browser.test.js` and puts the build into design
 *  mode explicitly; the two cases below are the other half of that pair — the
 *  default must show none of it.
 */

/** Bring the Atlas view on screen and wait for whatever it is going to paint. */
async function showAtlasView() {
  await page.evaluate(`document.querySelector('[data-page="atlas"]').click(); true`);
  await page.waitFor(`document.querySelector('#view-atlas').classList.contains('active')`);
  await sleep(400); // the charts draw on requestAnimationFrame
}

/** What the Atlas is currently showing, by rendered box rather than by text. */
const atlasState = () => page.evaluate(`(() => {
  const v = document.getElementById('view-atlas');
  const rendered = (el) => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const body = [...v.children].filter((c) => c.hasAttribute('data-demo-only'));
  const notice = [...v.children].find((c) => c.hasAttribute('data-live-only'));
  return {
    text: v.innerText,
    bodyCount: body.length,
    bodyRendered: body.filter(rendered).length,
    noticeShown: rendered(notice),
    metricsShown: rendered(v.querySelector('.atlas-metrics')),
    controlbarShown: rendered(v.querySelector('.atlas-controlbar')),
    stageShown: rendered(v.querySelector('.atlas-stage-grid')),
    compareShown: rendered(v.querySelector('.atlas-compare')),
    heroShown: rendered(v.querySelector('.hero')),
    breadcrumbShown: rendered(v.querySelector('.breadcrumb')),
  };
})()`);

/** The eight direct children the Atlas design is made of. */
const ATLAS_SAMPLE_BLOCKS = 8;

for (const [what, query] of [['a bare URL', ''], ['a tracking parameter', '&utm_source=chatgpt.com']]) {
  t(`${what} shows no Atlas sample: metrics, charts, distributions and controls all hidden`, async () => {
    await visit(query);
    await showAtlasView();
    const seen = await atlasState();

    for (const value of FABRICATED) {
      assert.doesNotMatch(seen.text, new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')),
        `${value} must not be visible in the Atlas on ${what}`);
    }
    assert.ok(seen.bodyCount >= ATLAS_SAMPLE_BLOCKS,
      `the Atlas sample body must still be marked (saw ${seen.bodyCount})`);
    assert.equal(seen.bodyRendered, 0, 'no part of the Atlas sample body may be rendered by default');
    assert.equal(seen.metricsShown, false, 'the metrics rail must be hidden by default');
    assert.equal(seen.controlbarShown, false, 'the sample control bar must be hidden by default');
    assert.equal(seen.stageShown, false, 'the chart/distribution stage must be hidden by default');
    assert.equal(seen.compareShown, false, 'the compare block must be hidden by default');
    assert.equal(seen.noticeShown, true, 'exactly one gated notice must stand in for the body');
    assert.equal(seen.heroShown, true, 'the Atlas keeps its hero — it is not a measurement');
    assert.equal(seen.breadcrumbShown, true, 'the Atlas keeps its breadcrumb');
  });
}

t('?demo=1 restores the whole Atlas design, and one notice never doubles up', async () => {
  await visit('&demo=1', QA);
  await showAtlasView();
  const seen = await atlasState();

  assert.match(seen.text, /1,248/, 'the authored fixture is back when asked for');
  // `.atlas-compare` starts collapsed in the design itself (the prototype ships
  // it hidden), so it is the one block that stays out of the layout in both
  // modes. Everything else the gate marked must be back on screen.
  assert.equal(seen.bodyRendered, seen.bodyCount - 1,
    `all but the collapsed compare block must be on screen in design mode (saw ${seen.bodyRendered}/${seen.bodyCount})`);
  assert.equal(seen.metricsShown, true);
  assert.equal(seen.controlbarShown, true);
  assert.equal(seen.stageShown, true);
  assert.equal(seen.noticeShown, false, 'the gated notice must give way to the design');

  const notices = await page.evaluate(
    `document.querySelectorAll('#view-atlas > [data-gated="1"]').length`);
  assert.equal(notices <= 1, true, `the Atlas must never carry more than one sample notice (saw ${notices})`);
});

/** Objects the ops fixture draws. They must not reach the DOM at all by default
 *  — `textContent` is used so a hidden one is still caught. */
const FIXTURE_OBJECTS = ['Demo LLM A', 'demo-user-a', 'run-demo-1'];
/** A static, parked sample key. It may remain in the DOM, but only inside a
 *  sample region that is not displayed. */
const PARKED_KEY = 'llk_';

t('a bare URL renders no demo portal object at all', async () => {
  await visit('');
  assert.equal(await page.evaluate(`window.__OPS__.mode`), 'unavailable',
    'the ops layer must not default to the fixture');
  const text = await page.evaluate(`document.body.textContent`);
  for (const needle of FIXTURE_OBJECTS) {
    assert.ok(!text.includes(needle), `"${needle}" must not be in the document on a bare URL`);
  }
  const seen = await page.evaluate(`(() => ({
    visible: document.body.innerText,
    keyHidden: [...document.querySelectorAll('#view-api .api-keys')].every((n) => getComputedStyle(n).display === 'none'),
  }))()`);
  assert.ok(!seen.visible.includes(PARKED_KEY), 'no sample key may be visible');
  assert.equal(seen.keyHidden, true, 'the parked keys must sit inside a hidden sample region');
});

t('a tracking parameter does not reveal the demo portal', async () => {
  await visit('&utm_source=chatgpt.com');
  assert.equal(await page.evaluate(`window.__OPS__.mode`), 'unavailable');
  const text = await page.evaluate(`document.body.textContent`);
  for (const needle of FIXTURE_OBJECTS) {
    assert.ok(!text.includes(needle), `"${needle}" must not appear beside a tracking parameter`);
  }
});

t('?demo=1 is the one switch that reveals the samples', async () => {
  await visit('&demo=1', QA);
  await sleep(400);
  assert.equal(await page.evaluate(`window.__OPS__.mode`), 'mock', 'the opt-in must reach the fixture');
  const text = await page.evaluate(`document.body.textContent`);
  assert.ok(FIXTURE_OBJECTS.some((n) => text.includes(n)), 'the samples must be reachable on request');
  assert.equal(await page.evaluate(`document.documentElement.dataset.mode`), 'demo',
    'one flag drives both the shell mode and the ops source');
  const shown = await page.evaluate(`getComputedStyle(document.querySelector('#view-api .api-keys')).display`);
  assert.notEqual(shown, 'none', 'and the sample regions are displayed again');
});

t('the sample-only regions replace the fake chat, not just hide the numbers', async () => {
  await visit('');
  const seen = await page.evaluate(`(() => {
    const gated = [...document.querySelectorAll('[data-gated]')].map((n) => n.textContent);
    return { count: gated.length, text: gated.join(' | ') };
  })()`);
  assert.ok(seen.count >= 3, 'each sample region must lead with a gated notice');
  assert.match(seen.text, /Linux PAM|Gateway|EvalScope/, 'the notices must name what the data needs');
  const chat = await page.evaluate(`(() => {
    const n = document.querySelector('#view-models .columns');
    return n ? getComputedStyle(n).display : 'missing';
  })()`);
  assert.equal(chat, 'none', 'the sample chat UI must not be displayed in live mode');
});

t('live mode never reaches past its own origin', async () => {
  await visit('');
  assert.deepEqual(page.externalRequests(), [], 'no request may leave loopback');
  assert.deepEqual(page.consoleErrors, []);
  assert.deepEqual(page.exceptions, []);
});

t('the gated first screen does not overflow at phone, tablet or desktop widths', async () => {
  for (const width of [320, 390, 768, 1024, 1440]) {
    await page.setViewport(width, width < 500 ? 844 : 950);
    await visit('');
    const box = await page.evaluate(`(() => ({
      doc: document.documentElement.scrollWidth,
      inner: window.innerWidth,
    }))()`);
    assert.ok(box.doc <= box.inner + 1, `${width}px overflows horizontally (${box.doc} > ${box.inner})`);
  }
});
