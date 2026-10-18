/*! tests/browser/workspace.browser.test.js — the V6.1 workspace layer in a real
 *  browser over CDP.
 *
 *  Each check drives a control the way a reader would (a click, a key) and
 *  asserts both what changed on screen and that nothing left the origin.
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

const t = (name, fn) => test(name, { skip: SKIP }, async () => { await ready(); return fn(); });
let nav = 0;
const visit = async (search, hash = '', base = BUILD) => {
  nav += 1;
  await page.goto(`${session.server.origin}${base}?qa=${nav}${search}${hash}`);
  assert.ok(await page.waitFor('!!window.__ATELIER__ && !!window.__WORKSPACE__'));
  await sleep(250);
};

t('demo calls: rows load from the in-page fixture, filter, open a detail drawer and enable export', async () => {
  await visit('&demo=1', '#calls', QA);
  assert.ok(await page.waitFor("window.__CALLS__.state === 'ready'"));
  const first = await page.evaluate(`(() => ({
    rows: document.querySelectorAll('#callsTable tbody tr[data-call-id]').length,
    badge: document.querySelector('#callsSourceBadge').textContent,
    csv: document.querySelector('#callsExportCsv').disabled,
    insights: !!document.querySelector('[data-calls-insights]'),
  }))()`);
  assert.ok(first.rows > 0);
  assert.match(first.badge, /^DEMO/);
  assert.equal(first.csv, false, 'export is enabled once rows are on screen');
  assert.ok(first.insights);

  await page.evaluate(`(() => { document.querySelector('#callsOutcome').value = 'failed'; document.querySelector('#callsApply').click(); })()`);
  assert.ok(await page.waitFor(`[...document.querySelectorAll('#callsTable tbody tr[data-call-id]')].every((r) => r.children[2].textContent === 'failed')`));

  await page.evaluate(`document.querySelector('#callsTable tbody tr[data-call-id]').click()`);
  assert.ok(await page.waitFor(`document.querySelector('#explainDialog').open`));
  const dlg = await page.evaluate(`document.querySelector('#modalBody').innerText`);
  assert.match(dlg, /SYNTHETIC \/ DEMO/);
  assert.match(dlg, /request_id/);
  await page.evaluate(`document.querySelector('#closeDialog').click()`);

  await page.evaluate(`window.__CALLS__.applyPreset('7d')`);
  assert.ok(await page.waitFor(`document.querySelector('[data-calls-preset="7d"]').getAttribute('aria-pressed') === 'true'`));
  assert.deepEqual(page.externalRequests(), [], 'demo mode issued no off-origin request');
});

t('live calls on a server without the route say NOT CONFIGURED, never zero', async () => {
  await visit('', '#calls');
  assert.ok(await page.waitFor("window.__CALLS__.state === 'not_configured'"));
  const seen = await page.evaluate(`(() => ({
    state: window.__CALLS__.state,
    text: document.querySelector('#callsHost').innerText,
    csv: document.querySelector('#callsExportCsv').disabled,
  }))()`);
  assert.equal(seen.state, 'not_configured');
  assert.match(seen.text, /http-404/);
  assert.equal(seen.csv, true);
});

t('overview readiness board lists nine views and calls none of them READY on a bare preview', async () => {
  await visit('', '#overview');
  assert.ok(await page.waitFor(`document.querySelectorAll('[data-ws-row]').length === 9`));
  const states = await page.evaluate(`[...document.querySelectorAll('[data-ws-row]')].map((r) => r.dataset.state)`);
  assert.equal(states.length, 9);
  assert.ok(!states.includes('ready'));
  await page.evaluate(`document.querySelector('[data-ws-go="calls"]').click()`);
  assert.ok(await page.waitFor(`window.__ATELIER__.page === 'calls'`));
});

t('API: SDK tabs switch by click and arrow key; token writes are disabled', async () => {
  await visit('&demo=1', '#api', QA);
  await page.evaluate(`document.querySelector('[data-sdk-tab="python"]').click()`);
  assert.equal(await page.evaluate('window.__WORKSPACE__.sdkTab'), 'python');
  await page.evaluate(`(() => { const t = document.querySelector('[data-sdk-tab="python"]'); t.focus(); t.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); })()`);
  assert.equal(await page.evaluate('window.__WORKSPACE__.sdkTab'), 'javascript');
  const visible = await page.evaluate(`document.querySelector('[data-sdk-panel="javascript"]').hidden`);
  assert.equal(visible, false);
  const tokens = await page.evaluate(`(() => ({
    state: window.__WORKSPACE__.tokens.state,
    enabled: [...document.querySelectorAll('[data-ws="tokens"] button')].filter((b) => !b.disabled && !b.hasAttribute('data-token-retry')).length,
  }))()`);
  assert.equal(tokens.state, 'demo');
  assert.equal(tokens.enabled, 0, 'no token write control is clickable');
});

t('admin: the route matrix is mounted and names no upstream port', async () => {
  await visit('', '#admin');
  const html = await page.evaluate(`[...document.querySelectorAll('[data-ws="routes"] table')].map((t) => t.outerHTML).join('')`);
  assert.match(html, /\/api\/v2\/ops\/deployments\/\{deployment_id\}\/share/);
  assert.doesNotMatch(html, /:8080|:8200|:9000/);
});
