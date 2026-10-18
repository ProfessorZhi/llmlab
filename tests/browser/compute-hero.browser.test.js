/*! tests/browser/compute-hero.browser.test.js — the Compute hero on first paint.
 *
 *  The host route is intercepted over CDP so a *known* summary and a 404 can both
 *  be exercised without any real Prometheus: the point is that the four cells on
 *  the first screen track the verified host source, and say UNKNOWN when there
 *  is none.
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
// Generate a fresh but measured sample for each test, never loosen the 90s LIVE gate.
// Fixed 03:57 UTC values made valid LIVE tests fail as soon as the wall clock advanced.
const freshTimes = () => {
  const now = Date.now();
  return { sampleAt: new Date(now - 9_000).toISOString(), observedAt: new Date(now).toISOString() };
};

const liveSummary = () => {
  const { sampleAt, observedAt } = freshTimes();
  return ({
  version: 1,
  source: 'prometheus-readonly',
  synthetic: false,
  readonly: true,
  state: 'known',
  observed_at: observedAt,
  metrics: {
    gpu_utilization_ratio: { state: 'known', value: 0, unit: 'ratio', source: 'prometheus-readonly', metric: 'llmlab_strata_gpu_utilization_ratio', series: 1, age_s: 9, sample_at: sampleAt },
    gpu_memory_used_mib: { state: 'known', value: 48884.3, unit: 'mib', source: 'prometheus-readonly', metric: 'llmlab_strata_gpu_memory_used_bytes', series: 1, age_s: 9, sample_at: sampleAt },
    gpu_memory_total_mib: { state: 'known', value: 49140, unit: 'mib', source: 'prometheus-readonly', metric: 'llmlab_strata_gpu_memory_total_bytes', series: 1, age_s: 9, sample_at: sampleAt },
    gpu_temperature_celsius: { state: 'known', value: 33, unit: 'celsius', source: 'prometheus-readonly', metric: 'llmlab_strata_gpu_temperature_celsius', series: 1, age_s: 9, sample_at: sampleAt },
    gpu_power_watts: { state: 'known', value: 21, unit: 'watts', source: 'prometheus-readonly', metric: 'llmlab_strata_gpu_power_watts', series: 1, age_s: 9, sample_at: sampleAt },
    strata_up: { state: 'known', value: 1, unit: 'bool', source: 'prometheus-readonly', metric: 'llmlab_strata_up', series: 1, age_s: 9, sample_at: sampleAt },
  },
  services: {
    strata: { state: 'known', up: true, reason: null },
    gateway: { state: 'known', up: true, reason: null },
    prometheus: { state: 'known', up: true, reason: null },
    grafana: { state: 'known', up: true, reason: null },
    evalscope: { state: 'known', up: true, reason: null },
  },
  });
};

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

/** Fulfil every request for the host route with `payload` (or a 404) for `fn`. */
async function withHostRoute({ status, body }, fn) {
  const { cdp } = session.browser;
  const { sessionId } = page;
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*api/infra/summary*' }] }, sessionId);
  const off = cdp.on((msg) => {
    if (msg.sessionId !== sessionId || msg.method !== 'Fetch.requestPaused') return;
    cdp.send('Fetch.fulfillRequest', {
      requestId: msg.params.requestId,
      responseCode: status,
      responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
      body: Buffer.from(JSON.stringify(body)).toString('base64'),
    }, sessionId).catch(() => {});
  });
  try {
    return await fn();
  } finally {
    off();
    await cdp.send('Fetch.disable', {}, sessionId).catch(() => {});
  }
}

const hero = () => page.evaluate(`(() => ({
  badge: document.querySelector('[data-compute-provenance]')?.textContent ?? null,
  util: document.querySelector('[data-compute-metric="gpu_utilization_pct"]')?.textContent ?? null,
  used: document.querySelector('[data-compute-metric="gpu_vram_used_mib"]')?.textContent ?? null,
  total: document.querySelector('[data-compute-metric="gpu_vram_total_mib"]')?.textContent ?? null,
  observed: document.querySelector('[data-compute-observed]')?.textContent ?? null,
}))()`);

// A distinct query per navigation: navigating to the identical URL is a
// same-document navigation, so the page would keep the previous host summary
// instead of re-reading the intercepted route.
let nav = 0;
const openCompute = async () => {
  nav += 1;
  await page.goto(url(`${BUILD}?qa=${nav}#compute`));
  await page.waitFor('!!window.__HOST__');
  await page.waitFor(`document.querySelector('#view-compute')?.classList.contains('active') === true`);
  await page.waitFor(`window.__HOST__.state !== 'unknown' || window.__HOST__.summary === null`);
};

t('a verified host summary drives the four first-screen cells with real values', async () => {
  const injected = liveSummary();
  await withHostRoute({ status: 200, body: injected }, async () => {
    await openCompute();
    await page.waitFor(`document.querySelector('[data-compute-metric="gpu_vram_used_mib"]').textContent.includes('MiB')`);
    const seen = await hero();
    assert.equal(seen.util, '0%', 'a measured zero renders as 0%, not UNKNOWN');
    assert.equal(seen.used, '48884.3 MiB');
    assert.equal(seen.total, '49140 MiB');
    assert.match(seen.badge, /LIVE · Prometheus/, 'the provenance must say LIVE · Prometheus');
    assert.ok(seen.observed?.includes(injected.metrics.gpu_memory_used_mib.sample_at.slice(0, 19)),
      'the sample time comes from the metric, not from a stale hard-coded clock');
  });
});

t('the live host narrative replaces the "all data synthetic" strip', async () => {
  await withHostRoute({ status: 200, body: liveSummary() }, async () => {
    await openCompute();
    await page.waitFor(`document.querySelector('[data-demo-state]')?.textContent.includes('LIVE')`);
    const text = await page.evaluate(`(() => ({
      strip: document.querySelector('[data-demo-state]').textContent,
      source: document.querySelector('[data-source-badge]')?.textContent ?? '',
    }))()`);
    assert.match(text.strip, /主机遥测 LIVE/, 'the strip must name the live host telemetry');
    assert.doesNotMatch(text.strip, /尚未连接 Dell 生产服务/, 'the retired blanket claim must be gone');
    assert.match(text.source, /待授权/, 'the call/usage numbers stay marked as not authorized');
  });
});

t('a 404 host route leaves every cell UNKNOWN with a why', async () => {
  await withHostRoute({ status: 404, body: { error: 'not found' } }, async () => {
    await openCompute();
    await sleep(250);
    const seen = await hero();
    for (const key of ['util', 'used', 'total']) {
      assert.equal(seen[key], 'UNKNOWN', `${key} must stay UNKNOWN without a source`);
    }
    assert.equal(seen.observed, 'UNKNOWN');
    assert.doesNotMatch(seen.badge, /LIVE/);
  });
});

t('the hero never reaches past its own origin', async () => {
  await withHostRoute({ status: 200, body: liveSummary() }, async () => {
    await openCompute();
    await sleep(250);
    assert.deepEqual(page.externalRequests(), [], 'no request may leave loopback');
    for (const u of page.requests) {
      assert.doesNotMatch(u, /:9090|:8200|:9000|:8080|:3000\b/, `the browser must not reach a host port: ${u}`);
    }
    assert.deepEqual(page.consoleErrors, []);
    assert.deepEqual(page.exceptions, []);
  });
});

t('the live hero does not overflow at phone, tablet or desktop widths', async () => {
  await withHostRoute({ status: 200, body: liveSummary() }, async () => {
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewport(width, width < 500 ? 844 : 950);
      await openCompute();
      await sleep(150);
      const box = await page.evaluate(`(() => ({
        doc: document.documentElement.scrollWidth,
        inner: window.innerWidth,
        cells: document.querySelectorAll('#view-compute [data-compute-metric]').length,
      }))()`);
      assert.ok(box.doc <= box.inner + 1, `${width}px overflows horizontally (${box.doc} > ${box.inner})`);
      assert.equal(box.cells, 3, 'the hero cells must survive every width');
    }
  });
});
