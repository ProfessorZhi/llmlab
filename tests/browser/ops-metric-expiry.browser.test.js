/*! Real Chrome regression: an already-rendered Ops metric must expire in place.
 * Source is a deterministic same-origin CDP fixture. No production API is read.
 */
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { startServer } from '../../web/tools/serve.mjs';
import { findChrome, launchBrowser, openPage } from './harness.mjs';
import { createMockHttp } from '../../web/js/features/mock-fixture.js';
import { validateOps } from '../../web/js/features/contract.js';

const ROOT = resolve(fileURLToPath(new URL('../../', import.meta.url)));
const CHROME = findChrome();
const SKIP = !CHROME && 'actual Chrome/Chromium is required';
let active = null;

after(async () => {
  if (!active) return;
  await active.browser.close();
  await active.server.close();
});

test('an idle real Chrome tab hides a verified Ops value at its source deadline',
  { skip: SKIP }, async () => {
    const server = await startServer({ root: ROOT, port: 0, env: {} });
    const browser = await launchBrowser({ chromePath: CHROME });
    active = { server, browser };
    const page = await openPage(browser.cdp, { width: 1440, height: 900 });
    const payload = createMockHttp({ persona: 'sharer', visibility: 'private' }).ops();
    payload.transport = { mode: 'live', gateway_attached: true, synthetic: false, label: 'LIVE' };
    payload.synthetic = false;
    payload.trends = null;
    payload.metrics.items = [{
      name: 'principal.requests', kind: 'observability',
      label: 'Requests', unit: '次', value: 987654, state: 'available',
      source: 'gateway-usage-export', updated_at: new Date().toISOString(), reason: null,
    }];
    // A future lease calculated by the BFF from source-specific catalogue TTL.
    payload.metrics.expires_at = { 'principal.requests': new Date(Date.now() + 2400).toISOString() };
    assert.equal(validateOps(payload).ok, true, 'the intercepted DTO must really satisfy the contract');

    const { identifier } = await browser.cdp.send('Page.addScriptToEvaluateOnNewDocument', {
      source: "window.__LLM_LAB_OPS__ = { opsBase: '' };",
    }, page.sessionId);
    await browser.cdp.send('Fetch.enable',
      { patterns: [{ urlPattern: '*api/v2/ops*' }] }, page.sessionId);
    const off = browser.cdp.on((msg) => {
      if (msg.sessionId !== page.sessionId || msg.method !== 'Fetch.requestPaused') return;
      browser.cdp.send('Fetch.fulfillRequest', {
        requestId: msg.params.requestId,
        responseCode: 200,
        responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
        body: Buffer.from(JSON.stringify({ data: { ops: payload } })).toString('base64'),
      }, page.sessionId).catch(() => {});
    });
    try {
      await page.goto(server.origin + '/web/index.html');
      assert.ok(await page.waitFor('window.__OPS__?.ops?.metrics?.items?.[0]?.value === 987654'));
      const early = await page.evaluate('document.querySelector("[data-ops-body=observability]")?.textContent ?? ""');
      assert.match(early, /LIVE/, 'a fresh verified response first paints as LIVE');
      // The exposed getter consults the clock immediately; the DOM is updated
      // by an independent expiry timer. Wait for that real repaint, not merely
      // for a time-computed getter to turn null ahead of the scheduled paint.
      assert.ok(await page.waitFor(
        `(document.querySelector('[data-ops-body="observability"]')?.textContent ?? '').includes('STALE')`,
        { timeoutMs: 8000 }), 'the idle browser must repaint STALE on the lease deadline');
      assert.equal(await page.evaluate('window.__OPS__?.ops?.metrics?.items?.[0]?.value'), null);
      const late = await page.evaluate('document.querySelector("[data-ops-body=observability]")?.textContent ?? ""');
      assert.match(late, /STALE/, 'the expired view must visibly report staleness');
      assert.match(late, /已过期/, 'reason must explain disappearance of the number');
      assert.deepEqual(page.externalRequests(), [], 'no off-origin requests are permitted');
    } finally {
      off();
      await browser.cdp.send('Fetch.disable', {}, page.sessionId).catch(() => {});
      await browser.cdp.send('Page.removeScriptToEvaluateOnNewDocument', { identifier }, page.sessionId).catch(() => {});
    }
  });
