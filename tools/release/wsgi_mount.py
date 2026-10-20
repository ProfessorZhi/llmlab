"""Offline WSGI skeleton for the production release mount plan.

It is a **test double for topology**, not a server to deploy:

* static UI and the two API surfaces live on one origin, so the browser's
  same-origin rule is satisfied without a proxy;
* ``/auth/platform/*`` and ``/api/*`` answer ``503 NOT_READY`` until the real
  handlers are supplied. Nothing here authenticates anybody;
* ``/auth/linux/*`` and ``/auth/admin/*`` are answered ``404``: they are not
  part of this release, and mounting them would make PAM-shaped login look like
  the platform sign-in path;
* ``/readyz`` reports the manifest gates and is ``503`` unless every gate is
  proven.

It performs no network I/O and writes nothing.
"""

from __future__ import annotations

import json
import os
import posixpath
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote

from .contract import ContractError, ReleaseManifest

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}
DEFAULT_MIME = "application/octet-stream"

STATIC_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

NOT_READY_CODE = "not_ready"


def _json(status: int, payload: Mapping[str, Any], start_response: Callable) -> list[bytes]:
    body = json.dumps(payload).encode("utf-8")
    reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed",
              421: "Misdirected Request", 503: "Service Unavailable"}.get(status, "Error")
    start_response(f"{status} {reason}", [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ])
    return [body]


class ReleaseMountApp:
    def __init__(
        self,
        manifest: ReleaseManifest,
        *,
        static_root: str | None = None,
        platform_app: Callable | None = None,
        api_app: Callable | None = None,
    ) -> None:
        self.manifest = manifest
        self.static_root = os.path.realpath(static_root or manifest.static_root)
        if not os.path.isdir(self.static_root):
            raise ContractError(f"static root is not a directory: {self.static_root!r}")
        self.platform_app = platform_app
        self.api_app = api_app

    # --- WSGI -----------------------------------------------------------

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> Iterable[bytes]:
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD", "GET")

        if path == "/readyz":
            return self._readyz(start_response)

        plan = self.manifest.mount_plan
        target = plan.resolve(path)
        if target == "refuse":
            return _json(404, {"error": {"code": "not_found"}}, start_response)
        if target == "platform":
            return self._delegate(self.platform_app, "platform_auth_not_installed",
                                  environ, start_response)
        if target == "api":
            return self._delegate(self.api_app, "api_not_installed",
                                  environ, start_response)

        if method not in ("GET", "HEAD"):
            return _json(405, {"error": {"code": "method_not_allowed"}}, start_response)
        return self._static(path, start_response, method)

    # --- parts ------------------------------------------------------------

    def _delegate(self, app, not_installed: str, environ, start_response):
        if app is None:
            # Fail closed and say which dependency is missing. A placeholder 200
            # here would be the exact "looks deployed" failure this release
            # contract exists to prevent.
            return _json(503, {
                "error": {"code": NOT_READY_CODE, "detail": not_installed},
                "state": "NOT_READY",
            }, start_response)
        return app(environ, start_response)

    def _readyz(self, start_response):
        report = self.manifest.readiness()
        return _json(200 if report["state"] == "READY" else 503, report, start_response)

    def _static(self, path: str, start_response, method: str):
        rel = _safe_relpath(path)
        if rel is None:
            return _json(404, {"error": {"code": "not_found"}}, start_response)
        candidate = os.path.join(self.static_root, rel)
        resolved = os.path.realpath(candidate)
        if resolved != self.static_root and not resolved.startswith(self.static_root + os.sep):
            return _json(404, {"error": {"code": "not_found"}}, start_response)
        if os.path.islink(candidate) or not os.path.isfile(resolved):
            return _json(404, {"error": {"code": "not_found"}}, start_response)
        try:
            with open(resolved, "rb") as handle:
                body = handle.read()
        except OSError:
            return _json(404, {"error": {"code": "not_found"}}, start_response)
        ext = posixpath.splitext(rel.lower())[1]
        start_response("200 OK", [
            ("Content-Type", MIME.get(ext, DEFAULT_MIME)),
            ("Content-Length", str(len(body))),
            *STATIC_HEADERS,
        ])
        return [b"" if method == "HEAD" else body]


def _safe_relpath(path: str) -> str | None:
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    if "\x00" in path or "\\" in path:
        return None
    decoded = unquote(path)
    if "\x00" in decoded or "\\" in decoded:
        return None
    if decoded in ("", "/"):
        return "index.html"
    parts = decoded.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        return None
    rel = posixpath.normpath("/".join(parts))
    if rel.startswith("..") or rel.startswith("/"):
        return None
    return rel


__all__ = ["DEFAULT_MIME", "MIME", "NOT_READY_CODE", "ReleaseMountApp", "STATIC_HEADERS"]
