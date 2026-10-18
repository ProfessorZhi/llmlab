/*! tests/browser/harness.mjs
 *  A zero-dependency Chrome DevTools Protocol driver.
 *
 *  Why not Playwright: the brief allows "existing Node/Chromium" only, and the
 *  repo has no node_modules. Node 24 ships a global WebSocket and Chrome ships
 *  the debugging protocol, which is all a headless check needs. Nothing here
 *  installs anything or reaches the network beyond 127.0.0.1.
 *
 *  Two guarantees this harness is built to make checkable:
 *    - every request the *page* makes is recorded, so "zero external requests"
 *      is an observation, not a promise;
 *    - `--host-resolver-rules` resolves every host except loopback to NOTFOUND,
 *      so a leak fails loudly instead of quietly succeeding on a dev machine.
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdtemp, readdir, rm, stat } from 'node:fs/promises';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env['ProgramFiles'] && join(process.env['ProgramFiles'], 'Google/Chrome/Application/chrome.exe'),
    process.env['ProgramFiles(x86)'] && join(process.env['ProgramFiles(x86)'], 'Google/Chrome/Application/chrome.exe'),
    process.env['LOCALAPPDATA'] && join(process.env['LOCALAPPDATA'], 'Google/Chrome/Application/chrome.exe'),
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    // Pre-installed Playwright Chromium on cloud CI containers.
    '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((p) => existsSync(p)) ?? null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((ok, fail) => {
    const srv = createServer();
    srv.once('error', fail);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => ok(port));
    });
  });
}

/** Minimal CDP client over the browser-level WebSocket, with flat sessions. */
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    ws.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`));
        else resolve(msg.result);
        return;
      }
      if (msg.method) for (const fn of this.listeners) fn(msg);
    });
    ws.addEventListener('close', () => {
      for (const { reject } of this.pending.values()) reject(new Error('CDP socket closed'));
      this.pending.clear();
    });
  }

  send(method, params = {}, sessionId, { timeoutMs = 30_000 } = {}) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      // A browser killed mid-request can leave the socket half-open on Windows;
      // without a deadline that request, and the whole test run with it, would
      // wait forever. A timeout turns a dead browser into a clear failure.
      const timer = setTimeout(() => {
        if (!this.pending.delete(id)) return;
        reject(new Error(`CDP ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (err) => { clearTimeout(timer); reject(err); },
      });
      this.ws.send(JSON.stringify(payload));
    });
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
}

async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((ok, fail) => {
    ws.addEventListener('open', () => ok(), { once: true });
    ws.addEventListener('error', () => fail(new Error(`cannot open CDP socket at ${url}`)), { once: true });
  });
  return new CDP(ws);
}

// ------------------------------------------------------------------ browser

/** Remove browser profiles left behind by a run that was killed mid-flight.
 *  Only our own prefix, and only when it is old enough that no sibling run
 *  could still be using it. Best-effort: a locked file is not worth failing. */
async function sweepStaleProfiles({ maxAgeMs = 3600_000 } = {}) {
  const root = tmpdir();
  let entries = [];
  try { entries = await readdir(root, { withFileTypes: true }); } catch { return; }
  await Promise.all(entries
    .filter((e) => e.isDirectory() && e.name.startsWith('llmlab-cdp-'))
    .map(async (e) => {
      const path = join(root, e.name);
      try {
        const info = await stat(path);
        if (Date.now() - info.mtimeMs < maxAgeMs) return;
        await rm(path, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
      } catch { /* in use, or already gone */ }
    }));
}

export async function launchBrowser({ chromePath = findChrome() } = {}) {
  if (!chromePath) throw new Error('no Chrome/Chromium found; set CHROME_PATH');

  await sweepStaleProfiles();
  const port = await freePort();
  const profile = await mkdtemp(join(tmpdir(), 'llmlab-cdp-'));
  const args = [
    '--headless=new',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-background-networking',
    // Headless Chromium treats an unfocused/occluded window as a backgrounded
    // renderer and throttles timers, rAF and view transitions — on a loaded
    // runner that turned sub-second paints into multi-second waits and made
    // timers/transitions look hung. These exempt the one inspected page from
    // that throttling; they change nothing about what is rendered.
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-ipc-flooding-protection',
    '--disable-component-update',
    '--disable-domain-reliability',
    '--disable-client-side-phishing-detection',
    '--safebrowsing-disable-auto-update',
    '--metrics-recording-only',
    '--disable-breakpad',
    '--no-pings',
    '--mute-audio',
    // Any host other than loopback is unresolvable: an accidental external
    // request fails here instead of succeeding unnoticed.
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--disable-features=Translate,OptimizationHints,MediaRouter,InterestFeedContentSuggestions',
    // Chrome refuses to start as root with its sandbox on (e.g. CI containers);
    // only then is the sandbox switched off. Loopback-only resolution above
    // still applies, so nothing can leave the machine either way.
    ...(process.getuid?.() === 0 ? ['--no-sandbox'] : []),
    'about:blank',
  ];

  const child = spawn(chromePath, args, { stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });

  let version = null;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) { version = await res.json(); break; }
    } catch { /* not up yet */ }
    await sleep(120);
  }
  if (!version) {
    child.kill();
    throw new Error('Chrome did not expose a debugging endpoint in time');
  }

  const cdp = await connect(version.webSocketDebuggerUrl);

  async function close() {
    try { cdp.ws.close(); } catch { /* already gone */ }
    if (process.platform === 'win32') {
      await new Promise((done) => {
        const k = spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
        k.on('exit', done);
        k.on('error', done);
      });
    } else child.kill('SIGKILL');
    // Chrome releases file handles a moment after the process dies, so a single
    // rm attempt can fail with EBUSY and strand ~2MB of profile per run.
    await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }).catch(() => {});
  }

  return { cdp, version, close, profile };
}

// --------------------------------------------------------------------- page

/**
 * A single inspected page. Records every request, console message, and
 * uncaught exception it produces.
 */
export async function openPage(cdp, { width = 1280, height = 900 } = {}) {
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });

  const requests = [];
  const consoleErrors = [];
  const exceptions = [];

  cdp.on((msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === 'Network.requestWillBeSent') requests.push(msg.params.request.url);
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails ?? {};
      const desc = d.exception?.description ?? '';
      exceptions.push({
        text: d.text ?? 'exception',
        where: desc.split('\n').slice(0, 2).join(' | '),
        url: d.url ?? null,
      });
    }
    if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? '').join(' '));
    }
  });

  const send = (method, params) => cdp.send(method, params, sessionId);
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Network.setCacheDisabled', { cacheDisabled: true });
  await setViewport(width, height);

  async function setViewport(w, h = height) {
    await send('Emulation.setDeviceMetricsOverride', {
      width: w, height: h, deviceScaleFactor: 1, mobile: false,
    });
  }

  async function evaluate(expression, { awaitPromise = false } = {}) {
    const res = await send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise, userGesture: true,
    });
    if (res.exceptionDetails) {
      throw new Error(`page evaluate failed: ${res.exceptionDetails.text} — ${expression.slice(0, 120)}`);
    }
    return res.result.value;
  }

  async function goto(url, { waitFor = 'window.__ATELIER__ && window.__ATLAS__', timeoutMs = 30000 } = {}) {
    await send('Page.navigate', { url });
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const ok = await evaluate(`!!(${waitFor})`).catch(() => false);
      if (ok) return;
      await sleep(50);
    }
    // Fail with the page's own complaints attached — a bare timeout sends you
    // hunting for a network problem that is usually a missing module.
    const detail = [
      `page never satisfied ${waitFor} at ${url}`,
      `console errors: ${JSON.stringify(consoleErrors)}`,
      `exceptions: ${JSON.stringify(exceptions)}`,
      `requests: ${JSON.stringify(requests)}`,
    ].join('\n  ');
    throw new Error(detail);
  }

  async function waitFor(expression, { timeoutMs = 15000 } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await evaluate(`!!(${expression})`).catch(() => false)) return true;
      await sleep(40);
    }
    return false;
  }

  /** Requests that are neither loopback nor a non-network scheme. */
  const externalRequests = () => requests.filter((u) => (
    !u.startsWith('http://127.0.0.1:')
    && !u.startsWith('http://localhost:')
    && !/^(data|blob|about|devtools):/.test(u)
  ));

  return {
    sessionId, targetId, evaluate, goto, waitFor, setViewport,
    requests, externalRequests, consoleErrors, exceptions,
  };
}

export { sleep };
