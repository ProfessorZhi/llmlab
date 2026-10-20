/*! V1 M1: real-browser matrix for the live/production UI.
 * No production producer or external network is contacted. Existing Chrome
 * on the CI runner is required; the test cannot pass by simulating a DOM.
 */
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startServer } from '../../web/tools/serve.mjs';
import { findChrome, launchBrowser, openPage } from './harness.mjs';
import { ALL_ROUTES } from '../../web/js/router.js';

const ROOT = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const CHROME = findChrome();
const SKIP = !CHROME && 'Chrome/Chromium required for real browser acceptance';
const VIEWPORTS = [320, 390, 768, 1024, 1440];
const THEMES = ['light', 'dark'];
const FIXTURES = ['1,248', '482k', '8,412', 'Demo LLM A', 'demo-user-a', 'run-demo-1'];
let session = null;

async function ready() {
  if (session) return session;
  const server = await startServer({ root: ROOT, port: 0, env: {} });
  try {
    const browser = await launchBrowser({ chromePath: CHROME });
    const page = await openPage(browser.cdp, { width: 1440, height: 950 });
    session = { server, browser, page };
    return session;
  } catch (error) {
    await server.close();
    throw error;
  }
}

after(async () => {
  if (!session) return;
  await session.browser.close();
  await session.server.close();
});

test('V1 M1: nine live views, five widths, two themes and zero fabricated production values',
  { skip: SKIP }, async () => {
    assert.equal(ALL_ROUTES.length, 9, 'eight top-level views plus Compute');
    assert.equal(new Set(ALL_ROUTES).size, 9, 'routes must be unique');

    const { server, page } = await ready();
    let sequence = 0;
    for (const theme of THEMES) {
      for (const width of VIEWPORTS) {
        await page.setViewport(width, width < 500 ? 844 : 950);
        // Deliberately open the production artifact with an attempted QA opt-in.
        await page.goto(server.origin + '/web/index.html?demo=1&matrix=' + (++sequence) + '#overview');
        assert.ok(await page.waitFor('!!window.__ATELIER__ && !!window.__OPS__'));
        if (theme === 'dark') {
          await page.evaluate("document.getElementById('themeButton').click(); true");
          assert.ok(await page.waitFor("document.documentElement.dataset.theme === 'dark'"));
        }

        for (const route of ALL_ROUTES) {
          await page.evaluate('location.hash = ' + JSON.stringify('#' + route) + '; true');
          const id = 'view-' + route;
          const activated = await page.waitFor(
            'window.__ATELIER__.page === ' + JSON.stringify(route)
            + ' && document.getElementById(' + JSON.stringify(id)
            + ')?.classList.contains("active") === true',
            { timeoutMs: 15000 },
          );
          assert.ok(activated, width + 'px ' + theme + ' ' + route + ': not active');

          const state = await page.evaluate('(() => {'
            + 'const element = document.getElementById(' + JSON.stringify(id) + ');'
            + 'const rect = element.getBoundingClientRect();'
            + 'return {'
            + 'page: window.__ATELIER__.page,'
            + 'theme: document.documentElement.dataset.theme,'
            + 'mode: document.documentElement.dataset.mode,'
            + 'synthetic: document.documentElement.dataset.synthetic,'
            + 'activeCount: document.querySelectorAll(".view.active").length,'
            + 'hidden: element.getAttribute("aria-hidden"),'
            + 'width: rect.width, height: rect.height,'
            + 'viewport: window.innerWidth,'
            + 'scrollWidth: document.documentElement.scrollWidth,'
            + 'demoSwitches: document.querySelectorAll("[data-demo-switch]").length,'
            + 'text: element.innerText'
            + '}; })()');
          const context = width + 'px ' + theme + ' ' + route;
          assert.equal(state.page, route, context + ': incorrect route');
          assert.equal(state.theme, theme, context + ': wrong theme');
          assert.equal(state.mode, 'live', context + ': production entered QA mode');
          assert.equal(state.synthetic, 'false', context + ': synthetic provenance');
          assert.equal(state.activeCount, 1, context + ': multiple active views');
          assert.equal(state.hidden, 'false', context + ': active view aria-hidden');
          assert.ok(state.width > 0 && state.height > 0, context + ': invisible view');
          assert.equal(state.demoSwitches, 0, context + ': QA switch exposed');
          assert.ok(state.scrollWidth <= state.viewport + 1,
            context + ': horizontal overflow ' + state.scrollWidth + ' > ' + state.viewport);
          for (const value of FIXTURES) {
            assert.ok(!state.text.includes(value),
              context + ': demo fixture visible: ' + value);
          }
        }
      }
    }

    assert.deepEqual(page.externalRequests(), [], 'the live preview must not request external origins');
    assert.deepEqual(page.exceptions, [], 'unhandled browser exception');
    assert.deepEqual(page.consoleErrors, [], 'browser console error');
  });
