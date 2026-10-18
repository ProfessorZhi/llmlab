/*! web/tools/serve.mjs — a static server for local preview and browser tests.
 *
 *  Why this exists: `type="module"` is blocked by CORS on `file://`, so the
 *  front-end can only be opened from an origin. This serves the `web/` folder
 *  on loopback and nothing else.
 *
 *  Deliberate limits:
 *    - binds 127.0.0.1 only; never 0.0.0.0
 *    - path traversal outside the web root is refused
 *    - no directory listing, no writes, no directory writes
 *    - no network egress of its own; it never proxies anywhere
 */
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { INFRA_PATH, infraEnabled, collectInfraSummary, offlineSummary } from './verified-infra.mjs';

export const WEB_ROOT = resolve(fileURLToPath(new URL('../', import.meta.url)));

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.map': 'application/json; charset=utf-8',
};

/** Resolve a request path to an absolute file inside the web root, or null. */
export function resolveRequestPath(urlPath, root = WEB_ROOT) {
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(urlPath, 'http://127.0.0.1').pathname);
  } catch {
    return null;
  }
  if (pathname === '/' || pathname === '') pathname = '/index.html';
  const rel = normalize(pathname).replace(/^([/\\])+/, '');

  // Normalise the root first: callers may pass 'D:/x/y' while `resolve()` hands
  // back 'D:\x\y', and a raw string prefix test would then reject everything.
  const base = resolve(root);
  const abs = resolve(base, rel);

  // Containment check: never serve anything above the root. Windows paths are
  // case-insensitive, so compare case-folded there.
  const fold = (p) => (process.platform === 'win32' ? p.toLowerCase() : p);
  if (fold(abs) !== fold(base) && !fold(abs).startsWith(fold(base) + sep)) return null;
  return abs;
}

export function createStaticServer({
  root = WEB_ROOT,
  env = process.env,
  infraFetch = globalThis.fetch,
} = {}) {
  return createServer(async (req, res) => {
    // The one non-static route. It is dark unless the operator turned it on, and
    // even then it is GET-only, query-less and same-origin: the browser cannot
    // use it to reach anything but this process's own projection.
    let pathname = null;
    try { pathname = new URL(req.url ?? '/', 'http://127.0.0.1').pathname; } catch { pathname = null; }
    if (pathname === INFRA_PATH) {
      if (req.method !== 'GET') {
        res.writeHead(405, { 'content-type': 'application/json; charset=utf-8' });
        res.end('{"error":"method not allowed"}');
        return;
      }
      if ((req.url ?? '').includes('?')) {
        res.writeHead(400, { 'content-type': 'application/json; charset=utf-8' });
        res.end('{"error":"query not supported"}');
        return;
      }
      if (!infraEnabled(env)) {
        res.writeHead(404, { 'content-type': 'application/json; charset=utf-8' });
        res.end('{"error":"not found"}');
        return;
      }
      let summary;
      try {
        summary = await collectInfraSummary({ fetchImpl: infraFetch });
      } catch {
        // Never 200-with-a-raw-error: a failure is a complete UNKNOWN answer.
        summary = offlineSummary();
      }
      const body = Buffer.from(JSON.stringify(summary), 'utf8');
      res.writeHead(200, {
        'content-type': 'application/json; charset=utf-8',
        'content-length': body.length,
        'cache-control': 'no-store',
        'x-content-type-options': 'nosniff',
      });
      res.end(body);
      return;
    }

    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8' });
      res.end('method not allowed');
      return;
    }

    let abs = resolveRequestPath(req.url ?? '/', root);
    if (!abs) {
      res.writeHead(403, { 'content-type': 'text/plain; charset=utf-8' });
      res.end('forbidden');
      return;
    }

    let info = await stat(abs).catch(() => null);
    if (info?.isDirectory()) {
      abs = join(abs, 'index.html');
      info = await stat(abs).catch(() => null);
    }
    if (!info?.isFile()) {
      res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
      res.end('not found');
      return;
    }

    const body = await readFile(abs).catch(() => null);
    if (!body) {
      res.writeHead(500, { 'content-type': 'text/plain; charset=utf-8' });
      res.end('read failed');
      return;
    }

    res.writeHead(200, {
      'content-type': TYPES[extname(abs).toLowerCase()] ?? 'application/octet-stream',
      'content-length': body.length,
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    });
    if (req.method === 'HEAD') res.end();
    else res.end(body);
  });
}

/** @returns {Promise<{origin:string, port:number, close:()=>Promise<void>}>} */
export function startServer({ root = WEB_ROOT, port = 0, env = process.env, infraFetch = globalThis.fetch } = {}) {
  const server = createStaticServer({ root, env, infraFetch });
  return new Promise((ok, fail) => {
    server.once('error', fail);
    server.listen(port, '127.0.0.1', () => {
      const { port: bound } = server.address();
      ok({
        origin: `http://127.0.0.1:${bound}`,
        port: bound,
        close: () => new Promise((done) => server.close(() => done())),
      });
    });
  });
}

const invokedDirectly = process.argv[1]
  && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url));

if (invokedDirectly) {
  const port = Number(process.env.PORT ?? 8123);
  const s = await startServer({ port });
  // eslint-disable-next-line no-console
  console.log(`llm-lab portal web → ${s.origin}/index.html  (root: ${WEB_ROOT})`);
  for (const sig of ['SIGINT', 'SIGTERM']) {
    process.on(sig, () => { s.close().then(() => process.exit(0)); });
  }
}
