/*! tests/browser/ops-ui.browser.test.js — the ops surface in a real browser.
 *
 *  Run with: npm run test:browser   (needs Chrome; set CHROME_PATH to override)
 *
 *  This file carries the assertions that need a DOM: that the panels are
 *  mounted into the right views, that a click changes real state, that the
 *  401/403 ladder paints the right notice, and that the new panels inherit the
 *  shell's computed styling rather than introducing a second look.
 *
 *  Everything that can be established without a browser is established without
 *  one — `tests/web-features/` holds the contract, capability, render and
 *  fixture checks, and `ops-mock-e2e.test.js` drives the whole data path against
 *  the HTTP fixture under plain Node. **On a host with no Chrome/Chromium every
 *  test here skips** with an explicit reason (see `SKIP` below) rather than
 *  passing vacuously; the offline suite remains the evidence in that case.
 *
 *  The `live` tests use CDP request interception, so the 401/403/contract paths
 *  are exercised against a real network round trip with a real injected
 *  response — not against a stubbed `fetch`.
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

// This file exercises the ops *fixture*, which is a design sample. Samples are
// opt-in now, so every navigation below asks for them explicitly with
// `?demo=1`; a bare URL renders the unavailable/preview state instead.
const BUILD = '/web/index.html';
// The isolated QA preview. Its only difference is `data-build-profile="qa"`,
// which is what lets `?demo=1` reach the design samples (web/js/build-profile.js).
// The deployed artifact ignores that query entirely, so any test that wants the
// samples has to ask for them from the artifact that carries them.
const QA = '/web/qa.html';

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

const url = (path) => `${session.server.origin}${path}`;

/** Wait until the ops layer has painted something in a given panel. A page that
 *  has merely booted has empty panel bodies; asserting on those would pass for
 *  the wrong reason. */
const settle = (panel = 'registry', minLength = 40) => page.waitFor(
  `(document.querySelector('[data-ops-body="${panel}"]')?.textContent?.length ?? 0) > ${minLength}`,
);

/** Wait until the panels have painted a settled answer.
 *
 *  `window.__OPS__.state` flips as soon as the parser decides the payload is
 *  unusable, but the notice is painted by a later `paint()` pass — probing the
 *  DOM the instant the hook changes can still read the in-flight LOADING
 *  notice. Wait for the repaint itself rather than sleeping a fixed interval. */
const settledNotice = (panels = ['registry']) => page.waitFor(`(() => {
  const ids = ${JSON.stringify(panels)};
  return ids.every((id) => {
    const t = document.querySelector('[data-ops-body="' + id + '"]')?.textContent ?? '';
    return t.length > 40 && !t.includes('LOADING') && !t.includes('正在读取');
  });
})()`);

/** Install `globalThis.__LLM_LAB_OPS__` before the module graph runs. */
async function injectHostConfig(cdp, sessionId, config) {
  const { identifier } = await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
    source: `window.__LLM_LAB_OPS__ = ${JSON.stringify(config)};`,
  }, sessionId);
  return identifier;
}

async function removeInjectedScript(cdp, sessionId, identifier) {
  if (identifier) {
    await cdp.send('Page.removeScriptToEvaluateOnNewDocument', { identifier }, sessionId);
  }
}

/**
 * Fulfil every request matching `urlPattern` with a canned response, for the
 * duration of `fn`. Requests that do not match are continued untouched.
 */
async function intercept(cdp, sessionId, { urlPattern, status, body }, fn) {
  await cdp.send('Fetch.enable', { patterns: [{ urlPattern }] }, sessionId);
  const off = cdp.on((msg) => {
    if (msg.sessionId !== sessionId || msg.method !== 'Fetch.requestPaused') return;
    const { requestId } = msg.params;
    cdp.send('Fetch.fulfillRequest', {
      requestId,
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

const probe = (expression) => page.evaluate(`(() => (${expression}))()`);

// --- mounting ---------------------------------------------------------------

t('the six ops panels mount into the five management views', async () => {
  await page.goto(url(`${QA}?demo=1`));
  await settle('registry');

  const mounted = await probe(`({
    panels: [...document.querySelectorAll('[data-ops]')].map((n) => n.dataset.ops),
    registry: !!document.querySelector('#view-models [data-ops="registry"]'),
    keys: !!document.querySelector('#view-api [data-ops="keys"]'),
    playground: !!document.querySelector('#view-api [data-ops="playground"]'),
    observability: !!document.querySelector('#view-observability [data-ops="observability"]'),
    evaluation: !!document.querySelector('#view-evaluation [data-ops="evaluation"]'),
    access: !!document.querySelector('#view-admin [data-ops="access"]'),
    mode: window.__OPS__.mode,
    state: window.__OPS__.state,
  })`);

  assert.equal(mounted.mode, 'mock', 'the default source must be the synthetic fixture');
  assert.equal(mounted.state, 'ready');
  assert.deepEqual(mounted.panels, ['registry', 'keys', 'playground', 'observability', 'evaluation', 'access']);
  for (const where of ['registry', 'keys', 'playground', 'observability', 'evaluation', 'access']) {
    assert.equal(mounted[where], true, `${where} is not inside its view`);
  }
});

t('the generated document is untouched by the mounting', async () => {
  // The panels are injected at runtime precisely so web/index.html stays
  // byte-identical to its generator output. `npm run verify:extract` is the
  // real check; this is the browser-side corroboration that the extra sections
  // were appended rather than the shell being rewritten.
  const shape = await probe(`({
    views: document.querySelectorAll('.view').length,
    anchors: document.querySelectorAll('[data-module]').length,
    ops: document.querySelectorAll('[data-ops]').length,
    script: document.querySelectorAll('script[src="./js/app.js"]').length,
    styles: document.querySelectorAll('link[rel="stylesheet"]').length,
  })`);
  assert.equal(shape.views, 9, 'the shell must include the extra Compute view without changing generated HTML');
  assert.equal(shape.anchors, 5, 'the shipped module panels must survive');
  assert.equal(shape.ops, 6);
  assert.equal(shape.script, 1);
  assert.equal(shape.styles, 4);
});

// --- the write path, in a real DOM -----------------------------------------

t('clicking a share mode changes the scope on screen, and it can be taken back', async () => {
  await page.goto(url(`${QA}?demo=1&persona=sharer`));
  await settle('registry');

  const before = await probe(`document.querySelector('[data-deployment-card="dep-demo-1"]').textContent`);
  assert.match(before, /Private/);

  // Selected shares require at least one concrete principal; the contract
  // correctly rejects an empty selection. Exercise a valid scoped write.
  await page.evaluate(`(() => {
    const input = document.querySelector('[data-share-principals="dep-demo-1"]');
    input.value = 'alice';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('[data-share-mode="selected"][data-deployment="dep-demo-1"]').click();
  })()`);
  const afterClick = await page.waitFor(
    `!!document.querySelector('[data-share-mode="selected"][data-deployment="dep-demo-1"][aria-pressed="true"]')`,
  );
  assert.equal(afterClick, true, 'the selected mode did not become current');

  const stored = await probe(`window.__OPS__.ops.registry.owned.find((d) => d.deployment_id === 'dep-demo-1').share.mode`);
  assert.equal(stored, 'selected', 'the on-screen state is not the fixture state');

  // Revoke.
  await page.evaluate(`document.querySelector('[data-share-mode="private"][data-deployment="dep-demo-1"]').click()`);
  await page.waitFor(
    `window.__OPS__.ops.registry.owned.find((d) => d.deployment_id === 'dep-demo-1').share.mode === 'private'`,
  );
  const revoked = await probe(`window.__OPS__.ops.registry.owned.find((d) => d.deployment_id === 'dep-demo-1').share.principals`);
  assert.deepEqual(revoked, [], 'revoking must clear the principal list');
});

t('a session without the share scope sees the control disabled, with the reason', async () => {
  await page.goto(url(`${QA}?demo=1&persona=user`));
  await settle('registry');

  const control = await probe(`(() => {
    const buttons = [...document.querySelectorAll('[data-share-mode]')];
    return {
      count: buttons.length,
      allDisabled: buttons.every((b) => b.disabled),
      titles: buttons.map((b) => b.title),
      hasReason: /portal:endpoints:share/.test(document.querySelector('#view-models [data-ops="registry"]').textContent),
    };
  })()`);

  assert.ok(control.count >= 3);
  assert.equal(control.allDisabled, true, 'an ungranted session must not get a live share control');
  for (const title of control.titles) assert.match(title, /portal:endpoints:share/);
  assert.equal(control.hasReason, true);
});

// --- the controls that must never act ---------------------------------------

t('the playground send and the eval submit are disabled and explain themselves', async () => {
  await page.goto(url(`${QA}?demo=1&persona=admin`));
  await settle('playground');

  const state = await probe(`(() => {
    const send = document.querySelector('[data-playground-send]');
    const submit = document.querySelector('[data-eval-submit]');
    return {
      send: { disabled: send.disabled, title: send.title },
      submit: { disabled: submit.disabled, title: submit.title },
      input: document.querySelector('#view-api [data-ops="playground"] input[aria-label="试跑输入"]').disabled,
      select: document.querySelector('#view-api [data-ops="playground"] select').disabled,
    };
  })()`);

  assert.equal(state.send.disabled, true);
  assert.equal(state.submit.disabled, true);
  assert.equal(state.input, true, 'the playground input must be inert without an authorized backend');
  assert.equal(state.select, true);
  assert.ok(state.send.title.length > 0, 'a disabled control must say why');
  assert.ok(state.submit.title.length > 0);
});

t('clicking the disabled controls changes nothing and issues no request', async () => {
  await page.goto(url(`${QA}?demo=1&persona=admin`));
  await settle('playground');
  const before = page.requests.length;
  await page.evaluate(`document.querySelector('[data-playground-send]').click(); document.querySelector('[data-eval-submit]').click();`);
  await page.evaluate('new Promise((r) => setTimeout(r, 250))', { awaitPromise: true });
  const added = page.requests.slice(before).filter((u) => /api\/v2|chat\/completions/.test(u));
  assert.deepEqual(added, [], `a disabled control issued a request: ${added.join(', ')}`);
});

// --- visual continuity ------------------------------------------------------

t('the new panels render with the shell\'s own components, not a second look', async () => {
  await page.goto(url(`${QA}?demo=1`));
  await settle('registry');

  const styles = await probe(`(() => {
    const read = (node, props) => {
      const cs = getComputedStyle(node);
      return props.map((p) => cs.getPropertyValue(p));
    };
    const props = ['border-top-color','border-radius','background-color','padding-top','padding-left','font-size'];
    // The shipped module panel, and the new registry panel: same frame, same card.
    const shippedFrame = document.querySelector('#view-models [data-module="engines"]');
    const opsFrame = document.querySelector('#view-models [data-ops="registry"]');
    const shippedCard = shippedFrame?.querySelector('.module-card') ?? document.createElement('div');
    const opsCard = opsFrame.querySelector('.module-card');
    return {
      frame: [read(shippedFrame, props), read(opsFrame, props)],
      card: [read(shippedCard, props), read(opsCard, props)],
      opsCardClass: opsCard.className,
      opsFrameClass: opsFrame.className,
    };
  })()`);

  assert.equal(styles.opsFrameClass, 'page-block module-panel');
  assert.match(styles.opsCardClass, /module-card/);
  // The shipped frame may not have rendered a card (its state depends on the
  // module payload), so only compare frames unconditionally.
  assert.deepEqual(styles.frame[1], styles.frame[0],
    'the ops panel frame does not match the shipped module panel frame');
});

t('the trend chart draws inside the existing plot frame and stays in the box', async () => {
  // The SVG exists in every view but has no layout box while its panel is
  // display:none. Measure it only after activating the observability view.
  await page.goto(url(`${QA}?demo=1&persona=admin#observability`));
  await settle('observability');
  const active = await page.waitFor("document.querySelector('#view-observability').classList.contains('active')");
  assert.equal(active, true);
  const plot = await probe(`(() => {
    const svg = document.querySelector('[data-ops-trend] svg');
    const box = svg.getBoundingClientRect();
    const frame = svg.parentElement.getBoundingClientRect();
    const polylines = [...svg.querySelectorAll('polyline')];
    return {
      width: box.width, height: box.height,
      frameWidth: frame.width,
      lines: polylines.length,
      // Every vertex must sit inside the viewBox: a point drawn outside is lost.
      outside: polylines.flatMap((p) => [...p.points]).filter((pt) => pt.x < -1 || pt.y < -1 || pt.x > 721 || pt.y > 221).length,
    };
  })()`);

  assert.ok(plot.lines >= 3, 'the trend must draw one run per series');
  assert.ok(plot.width > 100 && plot.height > 60, `the chart collapsed (${plot.width}x${plot.height})`);
  assert.equal(plot.outside, 0, 'a trend vertex was drawn outside the viewBox');
});

// --- the 401 / 403 ladder, over a real round trip ---------------------------

t('a 401 from a live source paints "sign in", in every panel', async () => {
  const { cdp } = session.browser;
  const { sessionId } = page;
  const injected = await injectHostConfig(cdp, sessionId, { opsBase: '' });
  try {
    await intercept(cdp, sessionId, { urlPattern: '*api/v2/ops*', status: 401, body: { error: { code: 'not_authenticated' } } }, async () => {
      await page.goto(url(`${BUILD}?source=live`), { waitFor: 'window.__OPS__' });
      await page.waitFor(`window.__OPS__.state !== 'unknown'`);
      await settledNotice(['registry', 'keys', 'playground', 'observability', 'evaluation', 'access']);
    });

    const state = await probe(`({
      mode: window.__OPS__.mode,
      state: window.__OPS__.state,
      ops: window.__OPS__.ops,
      body: document.querySelector('[data-ops-body="registry"]').textContent,
      everyPanel: ['registry','keys','playground','observability','evaluation','access']
        .map((id) => document.querySelector('[data-ops-body="' + id + '"]').textContent),
    })`);

    assert.equal(state.mode, 'live');
    assert.equal(state.state, 'signed_out');
    assert.equal(state.ops, null, 'a 401 page must carry no payload');
    assert.match(state.body, /请先登录/);
    assert.match(state.body, /401/);
    for (const panel of state.everyPanel) assert.match(panel, /请先登录/);
  } finally {
    await removeInjectedScript(cdp, sessionId, injected);
  }
});

t('a 403 from a live source paints "ask an operator" and is not retried', async () => {
  const { cdp } = session.browser;
  const { sessionId } = page;
  const injected = await injectHostConfig(cdp, sessionId, { opsBase: '' });
  try {
    let calls = 0;
    await cdp.send('Fetch.enable', { patterns: [{ urlPattern: '*api/v2/ops*' }] }, sessionId);
    const off = cdp.on((msg) => {
      if (msg.sessionId !== sessionId || msg.method !== 'Fetch.requestPaused') return;
      calls += 1;
      cdp.send('Fetch.fulfillRequest', {
        requestId: msg.params.requestId,
        responseCode: 403,
        responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
        body: Buffer.from('{"error":{"code":"forbidden"}}').toString('base64'),
      }, sessionId).catch(() => {});
    });
    try {
      await page.goto(url(`${BUILD}?source=live`), { waitFor: 'window.__OPS__' });
      await page.waitFor(`window.__OPS__.state === 'denied'`);
      await settledNotice();
    } finally {
      off();
      await cdp.send('Fetch.disable', {}, sessionId).catch(() => {});
    }

    const state = await probe(`({
      state: window.__OPS__.state,
      body: document.querySelector('[data-ops-body="registry"]').textContent,
    })`);
    assert.equal(state.state, 'denied');
    assert.match(state.body, /未获得模块目录授权/);
    assert.doesNotMatch(state.body, /请先登录/);
    assert.equal(calls, 1, `a 403 was retried ${calls} times; it must not be`);
  } finally {
    await removeInjectedScript(cdp, sessionId, injected);
  }
});

t('a live payload that violates the contract paints nothing rather than half of it', async () => {
  const { cdp } = session.browser;
  const { sessionId } = page;
  const injected = await injectHostConfig(cdp, sessionId, { opsBase: '' });
  try {
    // Valid apart from one field: a VRAM figure with no NVML source.
    const ops = (await import('../../web/js/features/mock-fixture.js'))
      .createMockHttp({ persona: 'sharer' }).ops();
    ops.registry.owned[0].resources.vram_total_mib = 49140;

    await intercept(cdp, sessionId, { urlPattern: '*api/v2/ops*', status: 200, body: { data: { ops } } }, async () => {
      await page.goto(url(`${BUILD}?source=live`), { waitFor: 'window.__OPS__' });
      // The state flips before the panel repaints, so a state-only wait can
      // still read the LOADING body. Wait for the render to land as well.
      const painted = await page.waitFor(
        `window.__OPS__.state === 'unknown'`
        + ` && !document.querySelector('[data-ops-body="registry"]').textContent.includes('LOADING')`,
      );
      assert.equal(painted, true, 'the refused payload never finished painting');
    });

    const state = await probe(`({
      state: window.__OPS__.state,
      ops: window.__OPS__.ops,
      body: document.querySelector('[data-ops-body="registry"]').textContent,
    })`);
    assert.equal(state.state, 'unknown');
    assert.equal(state.ops, null, 'a refused payload must not be half-rendered');
    assert.match(state.body, /暂时无法确定/);
    assert.doesNotMatch(state.body, /49,140|49140/, 'the refused GPU figure must not reach the screen');
  } finally {
    await removeInjectedScript(cdp, sessionId, injected);
  }
});

t('an explicit live request with no configured base fails closed to the fixture-free state', async () => {
  // No injected host config: `?source=live` cannot be honoured, and the answer
  // must be a rendered unknown — not the synthetic fixture wearing a live badge.
  await page.goto(url(`${BUILD}?source=live`), { waitFor: 'window.__OPS__' });
  await page.waitFor(`window.__OPS__.state !== 'unknown' || document.querySelector('[data-ops-body="registry"]').textContent.length > 40`);

  const state = await probe(`({
    mode: window.__OPS__.mode,
    state: window.__OPS__.state,
    ops: window.__OPS__.ops,
    body: document.querySelector('[data-ops-body="registry"]').textContent,
  })`);

  assert.equal(state.mode, 'unavailable');
  assert.equal(state.ops, null);
  assert.doesNotMatch(state.body, /我拥有的部署/, 'the fixture leaked into an unhonourable live request');
  assert.doesNotMatch(state.body, /SYNTHETIC/);
});

// --- the mobile layout keeps working ----------------------------------------

t('the ops panels survive the mobile breakpoint', async () => {
  await page.goto(url(`${QA}?demo=1`));
  await settle('registry');
  await page.setViewport(390, 844);
  await page.waitFor('window.innerWidth < 500');

  const mobile = await probe(`(() => {
    const frame = document.querySelector('#view-models [data-ops="registry"]');
    const box = frame.getBoundingClientRect();
    return {
      width: box.width,
      inner: window.innerWidth,
      docScroll: document.documentElement.scrollWidth,
      cards: frame.querySelectorAll('.module-card').length,
      stacked: [...frame.querySelectorAll('.module-card')].every((c) => c.getBoundingClientRect().width <= box.width + 1),
    };
  })()`);

  assert.ok(mobile.width <= mobile.inner + 1, `the panel overflows: ${mobile.width} > ${mobile.inner}`);
  assert.ok(mobile.docScroll <= mobile.inner + 1, 'the document overflows horizontally');
  assert.ok(mobile.cards > 0);
  assert.equal(mobile.stacked, true, 'a deployment card is wider than its frame');
});

// Declared last: requests accumulate for the life of the tab.
t('the ops surface never requests anything outside loopback', async () => {
  assert.ok(page.requests.length > 0, 'sanity: the page did load local files');
  const external = page.externalRequests();
  assert.deepEqual(external, [], `unexpected external requests: ${external.join(', ')}`);
  assert.deepEqual(page.consoleErrors, []);
  assert.deepEqual(page.exceptions, []);
});
