/*! tests/browser/platform-hub.browser.test.js — the Linux/PAM rebrand and the
 *  platform/source hub, in a real browser over CDP.
 *
 *  What needs a DOM here: that no 飞书 wording survives in what is actually
 *  rendered, that the account control opens an honest Linux/PAM explainer and
 *  never asks for a password, that each platform card's button really switches
 *  the view (by mouse *and* by keyboard), that nothing leaves loopback, and that
 *  the new hub does not overflow at the four breakpoints the design targets.
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
const CARD_TARGETS = [
  ['strata', 'models'],
  ['gateway', 'api'],
  ['metrics', 'observability'],
  ['evalscope', 'evaluation'],
];

let session = null;
let booting = null;
let page = null;

async function ready() {
  if (!booting) {
    booting = (async () => {
      const server = await startServer({ root: REPO_ROOT, port: 0 });
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

const clickCard = (platform) => page.evaluate(
  `document.querySelector('[data-platform="${platform}"] .secondary').click(); true`,
);

const isActive = (view) => page.waitFor(
  `document.querySelector('#view-${view}')?.classList.contains('active') === true`,
  { timeoutMs: 20000 },
);

t('the rendered shell carries no 飞书 wording and throws nothing', async () => {
  await page.goto(url(BUILD));
  const seen = await page.evaluate(`(() => ({
    text: document.body.innerText,
    html: document.documentElement.outerHTML,
    meta: document.querySelector('meta[name="description"]')?.content ?? '',
    title: document.title,
  }))()`);
  for (const [field, value] of Object.entries(seen)) {
    assert.doesNotMatch(value, /飞书|Feishu|OAuth/, `${field} still shows the retired sign-in wording`);
  }
  assert.deepEqual(page.consoleErrors, []);
  assert.deepEqual(page.exceptions, []);
});

t('the account control opens an honest Linux/PAM explainer, never a password prompt', async () => {
  await page.goto(url(BUILD));
  const label = await page.evaluate(`document.querySelector('#accountButton .account-label').textContent`);
  assert.match(label, /预览访问/, 'the header must label this as preview access');
  assert.match(label, /Linux/, 'the header must name the Linux session that is missing');
  assert.match(label, /未建立/, 'the header must not read as a signed-in account');

  await page.evaluate(`document.querySelector('#accountButton').click(); true`);
  const opened = await page.waitFor(`document.querySelector('#explainDialog')?.open === true`);
  assert.equal(opened, true, 'the account control must open the explainer');

  const dialog = await page.evaluate(`(() => ({
    title: document.querySelector('#modalTitle').textContent,
    body: document.querySelector('#modalBody').textContent,
    passwordFields: document.querySelectorAll('#explainDialog input[type="password"]').length,
  }))()`);
  assert.match(dialog.title, /预览访问|Linux/);
  assert.match(dialog.body, /PAM/, 'the explainer must name PAM');
  assert.match(dialog.body, /Basic Auth/, 'the explainer must admit this is a Basic-Auth preview');
  assert.match(dialog.body, /个人[^。]{0,12}不可访问|不可访问/, 'the explainer must say personal data is unavailable');
  assert.match(dialog.body, /不索取|不会.{0,6}密码|不向 \/auth\/linux\/login/, 'the explainer must promise not to ask for a Linux password');
  assert.equal(dialog.passwordFields, 0, 'the preview must never render a password field');
  await page.evaluate(`document.querySelector('#explainDialog').close(); true`);
});

for (const [platform, target] of CARD_TARGETS) {
  t(`the ${platform} card switches to the ${target} view`, async () => {
    await page.goto(url(BUILD));
    await page.evaluate(`document.querySelector('[data-page="overview"]').click(); true`);
    await clickCard(platform);
    assert.equal(await isActive(target), true, `${platform} did not activate #view-${target}`);
  });
}

t('the platform controls are real, focusable buttons operable by keyboard', async () => {
  await page.goto(url(BUILD));
  await page.evaluate(`document.querySelector('[data-page="overview"]').click(); true`);
  const shape = await page.evaluate(`(() => {
    const b = document.querySelector('[data-platform="metrics"] .secondary');
    return { tag: b.tagName, focusable: (b.focus(), document.activeElement === b) };
  })()`);
  assert.equal(shape.tag, 'BUTTON', 'a platform control must be a real button, not a div');
  assert.equal(shape.focusable, true, 'a platform control must be focusable');

  // A genuine Enter keydown on the focused control must activate it. The
  // `text` field is what makes Chrome apply the button's default action.
  const { cdp } = session.browser;
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyDown', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r',
    windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
  }, page.sessionId);
  await cdp.send('Input.dispatchKeyEvent', {
    type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
  }, page.sessionId);
  assert.equal(await isActive('observability'), true, 'Enter on the focused control must switch the view');
});

t('the hub never requests anything outside loopback', async () => {
  await page.goto(url(BUILD));
  await sleep(300);
  assert.ok(page.requests.length > 0, 'sanity: the page did load its own files');
  assert.deepEqual(page.externalRequests(), [], 'no request may leave loopback');
  assert.deepEqual(page.consoleErrors, []);
  assert.deepEqual(page.exceptions, []);
});

t('the hub does not overflow at phone, tablet or desktop widths', async () => {
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewport(width, width < 500 ? 844 : 950);
    await page.goto(url(BUILD));
    await page.evaluate(`document.querySelector('[data-page="overview"]').click(); true`);
    const box = await page.evaluate(`(() => ({
      doc: document.documentElement.scrollWidth,
      inner: window.innerWidth,
      cards: document.querySelectorAll('[data-platform]').length,
    }))()`);
    assert.ok(box.doc <= box.inner + 1, `${width}px overflows horizontally (${box.doc} > ${box.inner})`);
    assert.equal(box.cards, 6, 'all six platform cards must be present at every width');
  }
});

t('a dark theme repaint keeps the hub legible and in the box', async () => {
  await page.setViewport(1440, 950);
  await page.goto(url(BUILD));
  await page.evaluate(`document.querySelector('[data-page="overview"]').click(); true`);
  await page.evaluate(`document.querySelector('#themeButton').click(); true`);
  await page.waitFor(`document.documentElement.dataset.theme === 'dark'`);
  const box = await page.evaluate(`(() => {
    const b = document.querySelector('[data-platform="strata"] .secondary');
    const r = b.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), doc: document.documentElement.scrollWidth, inner: window.innerWidth };
  })()`);
  assert.ok(box.w > 0 && box.h > 0, 'the card button must keep a layout box in dark theme');
  assert.ok(box.doc <= box.inner + 1, 'dark theme must not introduce horizontal overflow');
});
