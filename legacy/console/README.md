# Legacy console (`legacy/console`)

The retired read-only operations console from the private `llm-lab` repository.

* `server.py` — stdlib `ThreadingHTTPServer` application. It serves the static
  files in `web/` and exposes read-only endpoints that query the inference node
  through the SSH probe and scrape Prometheus.
* `web/` — the browser client: `index.html`, `app.js`, `core/`, `views/`,
  `styles/`.

## Status: reference only

**This is not the runtime entry point of `llm-lab-portal`.** It is kept so the
new portal can reuse the view structure, the design tokens and the API shape
without re-reading the old repository. Do not wire it into a service unit.

The import was sanitized: the node address, account names, absolute paths and
the SSH identity paths are placeholders. The server will not start against a real
node unless the `{{PLACEHOLDER}}` values are filled in and the SSH identity files
(not included, see `config/ssh/*` exclusion in `legacy/README.md`) are supplied.

## Known limitation

The console was strictly read-only: it never wrote to the node and had no
authentication layer of its own. That model is why the console must not be
republished as an externally reachable service.
