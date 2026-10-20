"""Same-origin staging composition: one loopback origin serving UI **and** API.

Why this exists
---------------
The preview today is a static server on one port; the BFF on another. The
browser can therefore never reach ``/api/v2/ops`` on its own origin, so every
data region stays "not wired". This module composes the *reviewed* pieces into
a single WSGI application so that the question "can the real UI and the real
BFF live on one origin, fail closed, and leak nothing between users?" can be
answered in tests instead of in prose.

What this is **not**
--------------------
* It is **not** production. There is no real Linux authentication here: the
  PAM broker is not installed and ``backend.auth_linux.pam_backend`` ships
  ``UnavailablePamBackend``, so a login attempt ends in ``503 pam_unavailable``.
  A synthetic identity is never substituted for a real one.
* It is **not** a gateway wiring. ``GatewayAdapter`` is ``detached``: the Ops
  payload says synthetic/demo transport and carries no fabricated measurement.
* It is **not** the legacy deployment template. ``deploy/templates/**`` is
  reference material for the retired ``llm-lab`` stack and is not used here.

What it does guarantee (pinned by ``tests/bff/test_staging_same_origin.py``)
--------------------------------------------------------------------------
* one origin: static files and ``/api/**`` / ``/auth/**`` answered by the same
  process, so the browser's same-origin rule is satisfied without any proxy;
* authority guard before anything else: a ``Host`` that is not loopback and not
  an explicitly reviewed authority gets ``421`` and never reaches the registry;
* no traversal: ``..``, backslashes, NUL, symlinks and directories are refused;
* no directory listing, no writes, no arbitrary proxy, no ``0.0.0.0`` bind;
* unauthenticated ``/api/v2/ops`` is a ``401`` JSON error — never a fixture;
* static responses carry no cookies and no ``Set-Cookie``; API responses are
  ``no-store``, so one UID's data cannot be served from a shared cache;
* ``web/index.html`` (production profile) and ``web/qa.html`` (QA profile) are
  served byte-for-byte as built; the server injects no demo mode.

Run it (loopback only)::

    python3 -m backend.bff.staging \
        --state-dir /tmp/llmlab-staging-XXXX \
        --web-root web

It prints a STAGING banner on start; the banner is deliberate so nobody
mistakes this process for a production service.
"""

from __future__ import annotations

import argparse
import http.client
import os
import posixpath
import socketserver
import sys
import wsgiref.simple_server
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote

from ..auth import config as auth_config
from ..auth.routes import Application as AuthApplication
from ..auth_linux import accounts as linux_accounts
from ..registry.identity import PrincipalDirectory, TokenDirectory
from ..registry.registry import RegistryConfig, RegistryService
from ..registry.store import Store
from . import origin as bff_origin
from .app import OpsApplication
from .ops import OpsConfig
from .devserver import ALLOWED_BIND_HOSTS, DEFAULT_HOST_ID, build_linux_service

#: Same-as-`web/tools/serve.mjs` MIME table; kept small on purpose.
TYPES = {
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

DEFAULT_TYPE = "application/octet-stream"

#: Paths answered by the BFF. Everything else is static.
API_PREFIXES = ("/api/", "/auth/")

STATIC_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


class StagingConfigError(ValueError):
    """The process refuses to start rather than run in a guessed posture."""


def validate_bind_host(host: str) -> str:
    if host not in ALLOWED_BIND_HOSTS:
        raise StagingConfigError(
            f"refusing to bind {host!r}: staging is loopback-only "
            f"({', '.join(ALLOWED_BIND_HOSTS)}). A public bind is a deployment "
            "decision, not a staging convenience."
        )
    return host


def validate_bind_port(port: int) -> int:
    value = int(port)
    if value < 0 or value > 65535:
        raise StagingConfigError(f"refusing to bind port {port}: not a TCP port")
    if value == 8080:
        raise StagingConfigError(
            "refusing to bind port 8080: it is reserved for the upstream engine"
        )
    return value


def _is_api_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in API_PREFIXES) or path in {
        "/api/v2/ops",
        "/auth/linux/login",
        "/auth/linux/status",
        "/auth/logout",
    }


def _safe_relpath(path: str) -> str | None:
    """Return a repository-relative, canonical path or ``None`` (refuse)."""
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


def _mime_for(relpath: str) -> str:
    _, ext = posixpath.splitext(relpath.lower())
    return TYPES.get(ext, DEFAULT_TYPE)


def _emit(response, start_response) -> Iterable[bytes]:
    reason = http.client.responses.get(response.status, "")
    start_response(f"{response.status} {reason}".strip(), list(response.headers))
    return [response.body]


def _plain(status: int, message: str, start_response, *, method: str = "GET"):
    body = b"" if method == "HEAD" else message.encode("utf-8")
    headers = [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(message.encode("utf-8")))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    reason = http.client.responses.get(status, "")
    start_response(f"{status} {reason}".strip(), headers)
    return [body]


class StagingApp:
    """One origin: static UI + the existing fail-closed BFF."""

    def __init__(self, *, bff: OpsApplication, web_root: str,
                 authority_policy: bff_origin.AuthorityPolicy | None = None) -> None:
        self.bff = bff
        self.web_root = os.path.realpath(web_root)
        if not os.path.isdir(self.web_root):
            raise StagingConfigError(f"web root is not a directory: {web_root!r}")
        self.authority_policy = authority_policy or bff_origin.AuthorityPolicy()

    # --- WSGI ---------------------------------------------------------------

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO") or "/"

        # The authority guard runs before static file lookup as well: a request
        # addressed to someone else's hostname is not answered at all.
        refusal = self.bff.check_authority(
            host_header=environ.get("HTTP_HOST"),
            server_port=environ.get("SERVER_PORT"),
        )
        if refusal is not None:
            return _emit(refusal, start_response)

        if _is_api_path(path):
            return self.bff(environ, start_response)

        if method not in ("GET", "HEAD"):
            return _plain(405, "method not allowed", start_response, method=method)

        return self._static(path, environ, start_response, method)

    def _static(self, path, environ, start_response, method):
        rel = _safe_relpath(path)
        if rel is None:
            return _plain(404, "not found", start_response, method=method)

        abs_path = os.path.realpath(os.path.join(self.web_root, rel))
        if abs_path != self.web_root and not abs_path.startswith(self.web_root + os.sep):
            return _plain(404, "not found", start_response, method=method)
        if os.path.islink(os.path.join(self.web_root, rel)):
            return _plain(404, "not found", start_response, method=method)
        if not os.path.isfile(abs_path):
            return _plain(404, "not found", start_response, method=method)

        try:
            with open(abs_path, "rb") as handle:
                body = handle.read()
        except OSError:
            return _plain(404, "not found", start_response, method=method)

        headers = [
            ("Content-Type", _mime_for(rel)),
            ("Content-Length", str(len(body))),
            *STATIC_HEADERS,
        ]
        start_response("200 OK", headers)
        return [b"" if method == "HEAD" else body]


def build_staging_application(
    *,
    state_dir: str,
    web_root: str,
    database: str | None = None,
    host_id: str = DEFAULT_HOST_ID,
    trusted_authorities: Iterable[str] = (),
    accounts: object | None = None,
) -> StagingApp:
    """Compose an **empty, live-mode** registry with the fail-closed BFF.

    Deliberately unlike ``devserver.build_application``:

    * no ``MockGatewayAdapter`` — ``DetachedGatewayAdapter`` is used, so no
      fabricated measurement can appear;
    * no synthetic observability corpus — the adapters are left detached, so
      every metric is ``UNKNOWN`` with a reason;
    * no synthetic identities in the allowlist — login is unavailable anyway.
    """
    if not state_dir:
        raise StagingConfigError("--state-dir is required: state must be explicit")
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    database = database or os.path.join(state_dir, "registry.sqlite")

    authority_policy = bff_origin.AuthorityPolicy.build(tuple(trusted_authorities))
    accounts = accounts if accounts is not None else linux_accounts.SystemPwdSource()

    service = RegistryService(
        store=Store(database),
        tokens=TokenDirectory(()),
        principals=PrincipalDirectory([]),
        config=RegistryConfig(mode="live"),
    )

    auth_app = AuthApplication(auth_config.AuthConfig.from_env({}, state_dir=state_dir))
    auth_app.initialize()
    linux_service = build_linux_service(
        host_id=host_id,
        admin_names=(),
        accounts=accounts,
        allowed_logins=(),
    )

    bff = OpsApplication(
        auth_app=auth_app,
        service=service,
        accounts=accounts,
        linux_service=linux_service,
        observability=None,
        evaluation=None,
        ops_config=OpsConfig(
            registry_mode="live",
            gateway_adapter="detached",
            gateway_attached=False,
        ),
        authority_policy=authority_policy,
    )
    return StagingApp(bff=bff, web_root=web_root, authority_policy=authority_policy)


class _ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.bff.staging",
        description=(
            "Same-origin STAGING composition (UI + BFF on one loopback port). "
            "Not production: Linux auth is unavailable and no gateway is attached."
        ),
    )
    parser.add_argument("--host", default=ALLOWED_BIND_HOSTS[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--web-root", required=True)
    parser.add_argument("--database", default=None)
    parser.add_argument("--host-id", default=DEFAULT_HOST_ID)
    parser.add_argument("--trusted-authority", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host = validate_bind_host(args.host)
    port = validate_bind_port(args.port)

    app = build_staging_application(
        state_dir=args.state_dir,
        web_root=args.web_root,
        database=args.database,
        host_id=args.host_id,
        trusted_authorities=tuple(args.trusted_authority),
    )

    server = wsgiref.simple_server.make_server(
        host, port, app, server_class=_ThreadingWSGIServer
    )
    bound = server.server_address[1]
    print("STAGING composition — NOT production:", file=sys.stderr)
    print("  Linux authentication: unavailable (no PAM broker in this build)",
          file=sys.stderr)
    print("  Gateway: detached (all measurements UNKNOWN, no fixture)", file=sys.stderr)
    print(f"  origin: http://{host}:{bound}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


__all__ = [
    "API_PREFIXES",
    "STATIC_HEADERS",
    "StagingApp",
    "StagingConfigError",
    "build_staging_application",
    "main",
    "validate_bind_host",
    "validate_bind_port",
]


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
