/*! tests/browser/host-status.browser.test.js — the verified host read-out, in a
 *  real browser over CDP.
 *
 *  The server here runs with ATELIER_INFRA_READONLY unset, which is the shipped
 *  default, so the route is a 404 and the panel must say UNKNOWN · NO SOURCE. No
 *  Dell host is contacted: the point is that an unwired preview is honest, that
 *  the panel never reaches past its own origin, and that it does not blur into
 *  the synthetic demo numbers beside it.
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

const BUILD = '/web/index.html';

let session = null;
let booting = null;
let page = null;

async function ready() {
  if (!booting) {
    booting = (async () => {
      // Explicitly flag-off: the shipped default.
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

t('the panel mounts into the overview and the compute subpage', async () => {
  await page.goto(url(BUILD));
  await page.waitFor('!!window.__HOST__');
  const shape = await page.evaluate(`(() => ({
    overview: !!document.querySelector('#view-overview [data-host-status="overview"]'),
    compute: !!document.querySelector('#view-compute [data-host-status="compute"]'),
    labelled: !!document.querySelector('[data-host-status="overview"] .section-h'),
  }))()`);
  assert.equal(shape.overview, true, 'overview must carry the host panel');
  assert.equal(shape.compute, true, 'the compute subpage must carry it too');
  assert.equal(shape.labelled, true, 'the panel must carry a heading');
});

t('an unwired preview reads UNKNOWN · NO SOURCE, never a number', async () => {
  await page.goto(url(BUILD));
  await page.waitFor('!!window.__HOST__');
  // The panel mounts before its first read resolves; wait for the settled
  // notice instead of sleeping past the fetch.
  await page.waitFor(`(document.querySelector('#view-overview [data-host-status-body]')?.textContent ?? '').includes('UNKNOWN · NO SOURCE')`);
  const state = await page.evaluate(`window.__HOST__.state`);
  assert.equal(state, 'unknown', 'a 404 projection must resolve to unknown');
  const body = await page.evaluate(`document.querySelector('#view-overview [data-host-status-body]').textContent`);
  assert.match(body, /UNKNOWN · NO SOURCE/, 'the badge must say there is no source');
  assert.doesNotMatch(body, /LIVE · Prometheus verified/, 'it must not claim a live source');
  const summary = await page.evaluate(`window.__HOST__.summary`);
  assert.equal(summary, null, 'no summary may be fabricated when the route is off');
});

t('the real host panel is visibly distinct from the synthetic demo numbers', async () => {
  await page.goto(url(BUILD));
  await page.waitFor('!!window.__HOST__');
  // `__HOST__` appears when the layer is built, not when it has painted: wait
  // for the provenance badge itself rather than reading an empty body.
  await page.waitFor(`(document.querySelector('#view-overview [data-host-status-body] .badge')?.textContent?.trim().length ?? 0) > 0`);
  const marks = await page.evaluate(`(() => ({
    demoBadge: !!document.querySelector('[data-source-badge="gated"]'),
    demoText: document.querySelector('[data-source-badge="gated"]')?.textContent ?? '',
    hostBadge: document.querySelector('#view-overview [data-host-status-body] .badge')?.textContent ?? '',
  }))()`);
  assert.match(marks.demoBadge && marks.demoText, /待授权/, 'the gated numbers carry a not-authorized badge');
  assert.match(marks.hostBadge, /UNKNOWN|LIVE/, 'the host panel carries its own provenance badge');
  assert.notEqual(marks.demoText, marks.hostBadge, 'the two must not read the same');
});

t('the host panel touches nothing but its own origin', async () => {
  await page.goto(url(BUILD));
  await page.waitFor('!!window.__HOST__');
  // The one same-origin read must have completed before the request list is
  // judged; the settled notice is that signal.
  await page.waitFor(`(document.querySelector('#view-overview [data-host-status-body]')?.textContent ?? '').includes('UNKNOWN · NO SOURCE')`);
  assert.deepEqual(page.externalRequests(), [], 'no request may leave loopback');
  const hostCalls = page.requests.filter((u) => u.includes('/api/infra/summary'));
  assert.ok(hostCalls.length >= 1, 'the panel must read its one same-origin path');
  for (const u of hostCalls) {
    assert.equal(new URL(u).origin, session.server.origin, 'the read-out path must be same-origin');
  }
  // It must never reach a host port directly.
  for (const u of page.requests) {
    assert.doesNotMatch(u, /:9090|:8200|:9000|:8080|:3000\b/, `the browser must not reach a host port: ${u}`);
  }
  assert.deepEqual(page.consoleErrors, []);
  assert.deepEqual(page.exceptions, []);
});

t('the host panel introduces no overflow at phone, tablet or desktop widths', async () => {
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewport(width, width < 500 ? 844 : 950);
    await page.goto(url(BUILD));
    await page.waitFor('!!window.__HOST__');
    await page.evaluate(`document.querySelector('[data-page="overview"]').click(); true`);
    // The tiles are painted when the first read settles, not when the layer is
    // built; measure only after they exist.
    await page.waitFor(`document.querySelectorAll('#view-overview [data-host-status="overview"] .statusitem').length >= 6`);
    const box = await page.evaluate(`(() => ({
      doc: document.documentElement.scrollWidth,
      inner: window.innerWidth,
      tiles: document.querySelectorAll('#view-overview [data-host-status="overview"] .statusitem').length,
    }))()`);
    assert.ok(box.doc <= box.inner + 1, `${width}px overflows horizontally (${box.doc} > ${box.inner})`);
    assert.ok(box.tiles >= 6, 'the metric tiles must be present at every width');
  }
});

t('the host panel survives the dark theme', async () => {
  await page.setViewport(1440, 950);
  await page.goto(url(BUILD));
  await page.waitFor('!!window.__HOST__');
  await page.evaluate(`document.querySelector('#themeButton').click(); true`);
  await page.waitFor(`document.documentElement.dataset.theme === 'dark'`);
  const box = await page.evaluate(`(() => {
    const s = document.querySelector('#view-overview [data-host-status="overview"]');
    const r = s.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), doc: document.documentElement.scrollWidth, inner: window.innerWidth };
  })()`);
  assert.ok(box.w > 0 && box.h > 0, 'the panel must keep a layout box in dark theme');
  assert.ok(box.doc <= box.inner + 1, 'dark theme must not introduce horizontal overflow');
});
