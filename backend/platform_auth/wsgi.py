"""WSGI-facing guard for platform sessions.

This adapter is small on purpose. It answers three questions and nothing else:

1. **Who is this?** — resolved from a session cookie through
   :meth:`PlatformAuthService.resolve_session`. Identity is never taken from a
   header, a query parameter, an environment variable, a Linux UID, or the
   login name of the request.
2. **May they do this?** — an explicit platform scope check (401 before 403).
3. **Is this request authorised by the page?** — double-submit CSRF token plus
   an ``Origin`` allow-list for state-changing methods.

It is not wired into the production BFF. The existing BFF IAM is Linux-shaped
and must not be bent to accept these principals; integration is a separate,
reviewable change.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable, Mapping

from .errors import CsrfRejected, Forbidden, NotAuthenticated, PlatformAuthError
from .service import PlatformAuthService, PlatformPrincipal

COOKIE_NAME = "llmlab_platform"
CSRF_HEADER = "HTTP_X_PLATFORM_CSRF"
ORIGIN_HEADER = "HTTP_ORIGIN"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Headers an attacker might use to claim an identity. They are listed so a
#: reviewer can see they are ignored, and so tests can prove it.
IDENTITY_HEADERS = (
    "HTTP_X_PLATFORM_USER",
    "HTTP_X_PLATFORM_USER_ID",
    "HTTP_X_USER_ID",
    "HTTP_X_OWNER",
    "HTTP_X_LINUX_UID",
    "HTTP_X_REMOTE_USER",
    "HTTP_X_FORWARDED_USER",
)


def parse_cookies(raw: object) -> dict[str, str]:
    jar: dict[str, str] = {}
    if not isinstance(raw, str):
        return jar
    for part in raw.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name, _, value = piece.partition("=")
        name = name.strip()
        if name and name not in jar:
            jar[name] = value.strip()
    return jar


def session_cookie(token: str, *, max_age: int, secure: bool = True,
                   name: str = COOKIE_NAME) -> str:
    """``Secure`` by default: a platform session over cleartext is a bug."""
    attrs = [f"{name}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
             f"Max-Age={int(max_age)}"]
    if secure:
        attrs.append("Secure")
    return "; ".join(attrs)


def clear_cookie(*, name: str = COOKIE_NAME) -> str:
    return f"{name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Secure"


class PlatformGuard:
    """Resolve, authorise, and describe failures as JSON responses."""

    def __init__(
        self,
        service: PlatformAuthService,
        *,
        allowed_origins: Iterable[str] = (),
        cookie_name: str = COOKIE_NAME,
    ) -> None:
        self.service = service
        self.allowed_origins = frozenset(allowed_origins)
        self.cookie_name = cookie_name

    def principal(self, environ: Mapping[str, object]) -> PlatformPrincipal:
        token = parse_cookies(environ.get("HTTP_COOKIE")).get(self.cookie_name)
        return self.service.resolve_session(token)

    def require(self, environ: Mapping[str, object], scope: str | None = None) -> PlatformPrincipal:
        principal = self.principal(environ)
        if scope is not None and not principal.has_scope(scope):
            raise Forbidden()
        if str(environ.get("REQUEST_METHOD", "GET")).upper() in MUTATING_METHODS:
            self._check_csrf(principal, environ)
        return principal

    def _check_csrf(self, principal: PlatformPrincipal, environ: Mapping[str, object]) -> None:
        origin = environ.get(ORIGIN_HEADER)
        if self.allowed_origins:
            if not isinstance(origin, str) or origin not in self.allowed_origins:
                raise CsrfRejected()
        elif origin is not None:
            # Without an allow-list we cannot judge an Origin, so refuse rather
            # than accept. A caller that wants browser writes must configure one.
            raise CsrfRejected()
        self.service.verify_csrf(principal, environ.get(CSRF_HEADER))


def error_response(exc: PlatformAuthError, start_response: Callable) -> list[bytes]:
    body = json.dumps(exc.to_dict()).encode("utf-8")
    start_response(
        f"{exc.status} {_reason(exc.status)}",
        [("Content-Type", "application/json; charset=utf-8"),
         ("Content-Length", str(len(body))),
         ("Cache-Control", "no-store")],
    )
    return [body]


def _reason(status: int) -> str:
    return {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
            429: "Too Many Requests", 500: "Internal Server Error"}.get(status, "Error")


__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER",
    "IDENTITY_HEADERS",
    "MUTATING_METHODS",
    "ORIGIN_HEADER",
    "PlatformGuard",
    "clear_cookie",
    "error_response",
    "parse_cookies",
    "session_cookie",
]
