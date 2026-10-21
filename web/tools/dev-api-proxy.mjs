/*! web/tools/dev-api-proxy.mjs — opt-in same-origin bridge to a loopback BFF.
 *
 *  The console under web/console/ talks only to same-origin `/auth/*` and
 *  `/api/*`. In development the static server and the Python BFF devserver
 *  (`python3 -m backend.bff --state-dir …`) listen on different ports, so
 *  without this bridge the console can never reach a real backend locally.
 *
 *  Off unless LLMLAB_DEV_API names a loopback http:// origin. The target is
 *  validated once; anything that is not 127.0.0.1 / localhost / [::1] is
 *  refused, so this can never become an egress path.
 *
 *  The BFF only answers to the authority it listens on (anti-rebinding), so,
 *  like a reverse proxy, the bridge presents itself as that authority: Host is
 *  set to the target, and Origin / Referer are translated **only** when they are
 *  exactly this static server's own origin. Any other Origin is forwarded
 *  verbatim, so the BFF's same-origin and CSRF checks still refuse it.
 */
import { request } from 'node:http';

export const DEV_API_ENV = 'LLMLAB_DEV_API';
export const PROXIED_PREFIXES = Object.freeze(['/auth/', '/api/']);
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]']);
const MAX_BODY_BYTES = 1 << 20;

/** The validated target origin, or null when the bridge is off. Throws on a non-loopback target. */
export function devApiTarget(env = process.env) {
  const raw = env?.[DEV_API_ENV];
  if (!raw) return null;
  let url;
  try { url = new URL(raw); } catch { throw new Error(`${DEV_API_ENV} is not a URL`); }
  if (url.protocol !== 'http:' || !LOOPBACK_HOSTS.has(url.hostname) || url.pathname !== '/' || url.search || url.username) {
    throw new Error(`${DEV_API_ENV} must be a bare http:// loopback origin, e.g. http://127.0.0.1:8124`);
  }
  return url;
}

export function isProxiedPath(pathname) {
  return typeof pathname === 'string' && PROXIED_PREFIXES.some((p) => pathname.startsWith(p));
}

/** Request headers as the BFF should see them. Pure; exported for tests. */
export function forwardedHeaders(incoming, target) {
  const headers = { ...incoming };
  const self = incoming.host ? `http://${incoming.host}` : null;
  headers.host = target.host;
  if (self && headers.origin === self) headers.origin = target.origin;
  if (self && typeof headers.referer === 'string' && headers.referer.startsWith(`${self}/`)) {
    headers.referer = target.origin + headers.referer.slice(self.length);
  }
  return headers;
}

/** Forward one request to `target`; never throws, answers 502 on any failure. */
export function proxyRequest(req, res, target) {
  const headers = forwardedHeaders(req.headers, target);
  delete headers.connection;
  delete headers['keep-alive'];
  delete headers['transfer-encoding'];
  const chunks = [];
  let size = 0;
  let refused = false;
  req.on('data', (c) => {
    size += c.length;
    if (size > MAX_BODY_BYTES && !refused) {
      refused = true;
      res.writeHead(413, { 'content-type': 'application/json; charset=utf-8' });
      res.end('{"ok":false,"error":{"code":"payload_too_large"}}');
      req.destroy();
      return;
    }
    if (!refused) chunks.push(c);
  });
  req.on('end', () => {
    if (refused) return;
    const body = Buffer.concat(chunks);
    headers['content-length'] = String(body.length);
    const upstream = request({
      host: target.hostname.replace(/^\[|\]$/g, ''),
      port: target.port || 80,
      method: req.method,
      path: req.url,
      headers,
    }, (up) => {
      res.writeHead(up.statusCode ?? 502, up.headers);
      up.pipe(res);
    });
    upstream.on('error', () => {
      if (res.headersSent) { res.destroy(); return; }
      res.writeHead(502, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
      res.end('{"ok":false,"error":{"code":"dev_api_unreachable"}}');
    });
    upstream.end(body);
  });
}
