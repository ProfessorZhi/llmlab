#!/usr/bin/env node
/*! tools/release/smoke_static_bundle.mjs — smoke a built bundle in a real browser.
 *
 *  The static checks in `build_static_bundle.mjs` prove what is *in* the bundle.
 *  They cannot prove what the bundle *does*. This does: it serves the staged
 *  directory on loopback, opens it in Chrome over CDP, and asserts the three
 *  things that would turn a packaging mistake into a production incident:
 *
 *    1. the artifact declares `production`, so `?demo=1` is inert;
 *    2. no URL — `?demo=1`, `?source=mock`, a tracking parameter — can put a
 *       synthetic number on screen, even though the fixture modules are shipped
 *       (they are imported by the application and must not be deleted blindly);
 *    3. the login page stays fail-closed while no PAM backend is connected, and
 *       no request carrying a password is ever issued.
 *
 *  It reuses the repository's own CDP driver (`tests/browser/harness.mjs`)
 *  instead of growing a second one. That file lives outside `web/`, so it is
 *  never part of the published bundle.
 *
 *  Usage: node tools/release/smoke_static_bundle.mjs <bundleDir> [--json]
 *  Exit:  0 all assertions held; 1 one failed; 2 the smoke could not run.
 */

import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { startServer } from '../../web/tools/serve.mjs';
import { findChrome, launchBrowser, openPage, sleep } from '../../tests/browser/harness.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '..', '..');

/** Numbers that only exist in the design fixture. */
const SYNTHETIC = ['1,248', '99.4%', '1.42 s', '482k', '8,412'];
/** Objects that only exist in the mock portal fixture. */
const FIXTURE_OBJECTS = ['Demo LLM A', 'demo-user-a', 'run-demo-1'];

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  if (!ok) process.stderr.write(`FAIL  ${name}${detail ? `  [${detail}]` : ''}\n`);
}

function forbidden(text) {
  return [...SYNTHETIC, ...FIXTURE_OBJECTS].filter((t) => text.includes(t));
}

async function main() {
  const args = process.argv.slice(2).filter((a) => a !== '--json');
  const json = process.argv.includes('--json');
  const bundle = args[0] ? resolve(args[0]) : null;
  if (!bundle || !existsSync(join(bundle, 'index.html'))) {
    process.stderr.write('usage: node tools/release/smoke_static_bundle.mjs <bundleDir> [--json]\n');
    return 2;
  }
  if (!findChrome()) {
    process.stderr.write('smoke: no Chrome/Chromium found; set CHROME_PATH\n');
    return 2;
  }

  // `env: {}` keeps the verified-infra endpoint unconfigured, so the smoke never
  // reaches a real host service.
  const server = await startServer({ root: bundle, port: 0, env: {} });
  const browser = await launchBrowser({ chromePath: findChrome() });
  let page = null;
  try {
    page = await openPage(browser.cdp, { width: 1440, height: 900 });

    // --- the production artifact, on URLs that are supposed to be inert ------
    const probes = [
      ['', 'bare URL'],
      ['?demo=1', '?demo=1'],
      ['?source=mock', '?source=mock'],
      ['?demo=1&source=mock', '?demo=1&source=mock'],
      ['?utm_source=chatgpt.com', 'a tracking parameter'],
    ];
    for (const [query, label] of probes) {
      await page.goto(`${server.origin}/index.html${query}`);
      await sleep(350);
      const state = await page.evaluate(`(() => ({
        profile: document.documentElement.dataset.buildProfile,
        mode: document.documentElement.dataset.mode,
        text: document.body.innerText,
        html: document.documentElement.outerHTML.slice(0, 4000),
      }))()`);

      check(`${label}: the artifact declares production`,
        state.profile === 'production', String(state.profile));
      check(`${label}: the data mode is live`, state.mode === 'live', String(state.mode));

      const hits = forbidden(state.text);
      check(`${label}: no synthetic value is rendered`, hits.length === 0, hits.join(','));

      // The fixture strings may still be *shipped* (imported modules); they must
      // not be *rendered*. Check the live DOM text, not the source.
      const objects = FIXTURE_OBJECTS.filter((o) => state.text.includes(o));
      check(`${label}: no demo portal object is rendered`, objects.length === 0, objects.join(','));
    }

    // --- the login page, with no PAM backend behind it ----------------------
    const authPage = await openPage(browser.cdp, { width: 1440, height: 900 });
    try {
      await authPage.goto(`${server.origin}/auth/index.html`, {
        waitFor: 'document.documentElement.dataset.authReady === "true"',
        timeoutMs: 15000,
      });
      const gate = await authPage.evaluate(`(() => ({
        gate: document.getElementById('linux-gate')?.dataset.gate,
        fields: document.getElementById('linux-fields')?.disabled,
        submit: document.getElementById('linux-submit')?.disabled,
        text: document.getElementById('linux-gate')?.textContent?.trim() ?? '',
      }))()`);
      check('login page: the PAM gate is closed', gate.gate === 'hold', String(gate.gate));
      check('login page: the form is disabled', gate.fields === true && gate.submit === true,
        `fields=${gate.fields} submit=${gate.submit}`);
      check('login page: it says PAM is not open', /PAM/.test(gate.text) && /BasicAuth/.test(gate.text),
        gate.text.slice(0, 90));

      const before = authPage.requests.length;
      await authPage.evaluate(`document.getElementById('linux-form')?.requestSubmit(); true`);
      await sleep(500);
      const posts = authPage.requests.slice(before).filter((u) => /\/auth\/linux\/login/.test(u));
      check('login page: no password request is issued', posts.length === 0, posts.join(','));
    } finally {
      await browser.cdp.send('Target.closeTarget', { targetId: authPage.targetId }).catch(() => {});
    }

    // --- no console errors, no off-origin traffic --------------------------
    check('no JavaScript console errors', page.consoleErrors.length === 0,
      page.consoleErrors.slice(0, 2).join(' | '));
    check('no uncaught exceptions', page.exceptions.length === 0,
      JSON.stringify(page.exceptions.slice(0, 2)).slice(0, 160));
    const external = page.externalRequests();
    check('no off-origin request', external.length === 0, external.slice(0, 3).join(', '));
  } finally {
    await browser.close().catch(() => {});
    await server.close().catch(() => {});
  }

  const failed = results.filter((r) => !r.ok);
  if (json) {
    process.stdout.write(JSON.stringify({ bundle, results, failed: failed.length }, null, 2) + '\n');
  } else {
    process.stdout.write(`smoke: ${results.length - failed.length}/${results.length} checks held\n`);
  }
  return failed.length === 0 ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((err) => {
  process.stderr.write(`smoke could not run: ${err && err.stack ? err.stack : err}\n`);
  process.exit(2);
});
