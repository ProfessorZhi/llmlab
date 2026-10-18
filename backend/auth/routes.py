"""The HTTP endpoints, expressed independently of any web framework.

Contract (agreed with the parallel frontend/Atlas work):

===============================================  ==========================================
``GET  /auth/csrf``                              200 — ``{csrf_token, authenticated, providers}``
``POST /auth/local/login``                       200 — ``{user, csrf_token, session_expires_at}`` + session cookie
``GET  /auth/feishu/start``                      302 — to the Feishu authorisation endpoint
``GET  /auth/feishu/callback``                   302 — to a fixed local path
``GET  /auth/me``                                200 — ``{authenticated, user, projects}`` or 401
``POST /auth/logout``                            200 — clears the cookie, revokes the session
``GET  /auth/tokens``                            200 — the caller's own scoped tokens, masked
``POST /auth/tokens``                            201 — mint a scoped token; the plaintext is returned here **once**
``POST /auth/tokens/revoke``                     200 — revoke one of the caller's own tokens
``POST /auth/tokens/rotate``                     201 — replace a **live** token of the caller's; plaintext returned once
``POST /auth/tokens/revoke-all``                 200 — revoke every token the caller holds
===============================================  ==========================================

The five ``/auth/tokens`` routes are **fail-closed**: until the deploying
application injects a :class:`~backend.auth.tokens.ScopedTokenService` they
answer ``503 not_configured``, exactly like the Feishu routes do before an
OAuth provider is attached. There is deliberately **no introspection route** —
resolving a bearer token to a principal is a trusted, in-process call the
Registry/Gateway makes, not an endpoint that would turn a bad token into an
oracle for anyone who can reach the portal.

Success:  ``{"ok": true,  "data": {...}}``
Failure:  ``{"ok": false, "error": {"code": "<snake_case>", "message": "..."}}``

Rules applied to every request:

* anything unauthenticated that needs a session gets **401**;
* write requests (``POST``/``PUT``/``PATCH``/``DELETE``) must carry a matching
  ``Origin`` (or ``Referer``) **and** a valid CSRF token;
* bodies larger than ``max_request_bytes`` are rejected with **413** before they
  are parsed;
* redirect targets are re-validated as same-origin paths, so no query
  parameter can turn this into an open redirect;
* the Feishu flow is bound to the browser that started it, by a short-lived
  ``HttpOnly; Secure; SameSite=Lax`` cookie whose value never appears in a URL
  (see :meth:`Application._flow_cookie` and ``AuthService.complete_feishu``);
* unexpected exceptions become a generic 500 — internals never reach the client.
"""

from __future__ import annotations

import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import errors
from .config import AuthConfig, is_local_path
from .feishu import Transport
from .service import OAUTH_BINDING_TTL_SECONDS, AuthService
from .tokens import ScopedTokenService, normalize_scopes, principal_ref

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: The scopes a session may hand to a CLI token when the deploying application
#: has not supplied a policy of its own.
#:
#: This is deliberately the *narrowest useful* set — being able to read the
#: registry — and deliberately **not** derived from ``role``. A ``platform_admin``
#: session does not get ``registry:admin:*`` merely by being an administrator;
#: those are capabilities the deployment must grant explicitly, because the
#: Registry's own rule is that no rule says "role == admin therefore allow".
#: Without this restraint, minting a token would be a silent escalation path.
DEFAULT_TOKEN_SCOPES: frozenset[str] = frozenset({"registry:read"})

#: Query/body names that assert an identity. Same set the BFF and Registry
#: refuse (``backend.registry.identity.FORBIDDEN_IDENTITY_FIELDS``), repeated
#: here because ``backend.auth`` is the lower layer and must not depend on
#: Registry. A test pins the two sets together so they cannot drift.
#:
#: ``username`` is on the list because it is an assertion on every route except
#: the local-admin login, where it names the credential being verified.
ASSERTED_IDENTITY_FIELDS: frozenset[str] = frozenset(
    {
        "owner",
        "owner_principal",
        "owner_uid",
        "uid",
        "user_id",
        "principal",
        "subject",
        "username",
        "linux_user",
        "role",
        "roles",
        "is_admin",
        "admin",
    }
)

DEFAULT_PORTS = {"http": "80", "https": "443"}


def _normalise_authority(scheme: str, authority: str) -> str:
    """Lower-case an authority and drop the port when it is the scheme default.

    ``Host: portal.example.com:443`` and ``Origin: https://portal.example.com``
    name the same origin; a proxy that rewrites one and not the other must not
    turn an ordinary request into a rejection, nor make two different origins
    compare equal.
    """
    scheme = scheme.lower()
    authority = authority.lower()
    host, separator, port = authority.rpartition(":")
    if separator and host and port.isdigit() and port == DEFAULT_PORTS.get(scheme):
        authority = host
    return f"{scheme}://{authority}"


def _normalise_origin(value: str) -> str | None:
    """``scheme://host[:port]`` from an Origin/Referer value, or ``None``.

    ``None`` means "not an origin we can compare": an opaque ``null`` Origin,
    a scheme-relative ``//host``, a bare host, or any non-HTTP scheme.
    """
    split = urllib.parse.urlsplit(value)
    if split.scheme not in DEFAULT_PORTS or not split.netloc:
        return None
    return _normalise_authority(split.scheme, split.netloc)

REASON_PHRASES = {
    200: "OK",
    201: "Created",
    302: "Found",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/auth/csrf"): "csrf",
    ("POST", "/auth/local/login"): "local_login",
    ("GET", "/auth/feishu/start"): "feishu_start",
    ("GET", "/auth/feishu/callback"): "feishu_callback",
    ("GET", "/auth/me"): "me",
    ("POST", "/auth/logout"): "logout",
    ("GET", "/auth/tokens"): "tokens_list",
    ("POST", "/auth/tokens"): "tokens_issue",
    ("POST", "/auth/tokens/revoke"): "tokens_revoke",
    ("POST", "/auth/tokens/rotate"): "tokens_rotate",
    ("POST", "/auth/tokens/revoke-all"): "tokens_revoke_all",
}

#: Paths whose field-level identity policy this application owns. The matrix
#: routes are its own surface; ``/auth/linux/login`` is owned by IAM in the BFF
#: route map and reuses this preflight for CSRF/Origin. BFF-only routes keep
#: their own field policy and are deliberately absent.
IDENTITY_GUARDED_PATHS: frozenset[str] = frozenset(
    {path for _method, path in ROUTES} | {"/auth/linux/login"}
)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: Mapping[str, list[str]] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    client_ip: str = ""
    scheme: str = "https"
    host: str = ""

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def query_one(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None

    def json_body(self) -> dict[str, Any]:
        media_type = (self.header("content-type") or "").split(";")[0].strip().lower()
        if media_type and media_type != "application/json":
            raise errors.InvalidRequest(detail="expected an application/json body")
        if not self.body:
            raise errors.InvalidRequest(detail="a JSON body is required")
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise errors.InvalidRequest(detail="the body is not valid JSON") from None
        if not isinstance(payload, dict):
            raise errors.InvalidRequest(detail="the body must be a JSON object")
        return payload


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def json(cls, status: int, payload: Any, *, headers: list[tuple[str, str]] | None = None) -> "Response":
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return cls(
            status=status,
            body=body,
            headers=[("Content-Type", "application/json; charset=utf-8"), *(headers or [])],
        )

    @classmethod
    def redirect(cls, location: str, *, headers: list[tuple[str, str]] | None = None) -> "Response":
        return cls(status=302, body=b"", headers=[("Location", location), *(headers or [])])


class Application:
    """Route table plus the cross-cutting guards, callable as a WSGI app."""

    def __init__(
        self,
        cfg: AuthConfig,
        *,
        service: AuthService | None = None,
        transport: Transport | None = None,
        provider: Any = None,
        clock: Callable[[], float] = time.time,
        tokens: ScopedTokenService | None = None,
        token_scope_policy: Callable[[Any], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self.service = service if service is not None else AuthService(
            cfg, transport=transport, provider=provider, clock=clock
        )
        # ``None`` is a supported, tested state: the token routes 503 rather
        # than exist in a half-wired form.
        self.tokens = tokens
        self._token_scope_policy = token_scope_policy

    # --- wiring --------------------------------------------------------------

    def initialize(self) -> None:
        self.service.initialize()

    @property
    def provider(self) -> Any:
        return self.service.provider

    @provider.setter
    def provider(self, value: Any) -> None:
        self.service.provider = value

    # --- authorisation -------------------------------------------------------

    def authorize(self, session, *, require_admin: bool = False):
        """Guard reused by every future protected route.

        There is deliberately no route that grants a role or a project: role
        assignment is an out-of-band administrative action, so authentication
        can never be an escalation path.
        """
        if not session.user.is_active:
            raise errors.AccountDisabled()
        if require_admin and not session.user.is_admin:
            raise errors.Forbidden()
        return session

    # --- dispatch ------------------------------------------------------------

    def handle(self, request: Request) -> Response:
        handler_name = ROUTES.get((request.method, request.path))
        if handler_name is None:
            if any(path == request.path for _method, path in ROUTES):
                return self._finish(self._envelope(405, "method_not_allowed", "Method not allowed."))
            return self._finish(self._envelope(404, "not_found", "Not found."))

        try:
            self._preflight(request)
            response = getattr(self, f"_route_{handler_name}")(request)
        except errors.AuthError as exc:
            if handler_name == "feishu_callback":
                response = self._callback_redirect(exc)
            else:
                response = self._error_response(exc)
        except Exception:  # noqa: BLE001 - never let internals reach the client
            self._record_internal_error()
            response = self._error_response(errors.AuthError())

        if handler_name == "feishu_callback":
            # Success or failure alike: the flow cookie has served its purpose
            # and is removed, so a stale nonce can never be presented again.
            response = self._clear_flow_cookie(response)
        return self._finish(response)

    @staticmethod
    def _identity_guard_owns(path: str) -> bool:
        return path in IDENTITY_GUARDED_PATHS

    def _reject_asserted_identity(self, request: Request) -> None:
        """Refuse a query/body field that names an identity or a role.

        Identity comes from the server-side session, never from the request.
        A forged field is refused whole rather than ignored: "read but not
        used" is one refactor, middleware or log processor away from used.

        ``username`` is the credential identifier on ``/auth/local/login``, so
        that one route may carry it in its body. It remains an assertion in a
        query string and on every other route.
        """
        if not self._identity_guard_owns(request.path):
            # BFF reuses this preflight for CSRF/Origin. Its own routes keep
            # their own field policy (``/auth/admin/enter`` deliberately ignores
            # a body claim); the route map's IAM-owned Linux login is included.
            return

        query_fields = {str(name).strip().lower() for name in request.query}
        body_fields: set[str] = set()
        if request.body:
            payload = request.json_body()
            if isinstance(payload, dict):
                body_fields = {str(name).strip().lower() for name in payload}

        if request.path == "/auth/local/login":
            body_fields.discard("username")

        offending = sorted((query_fields | body_fields) & ASSERTED_IDENTITY_FIELDS)
        if offending:
            raise errors.IdentityAsserted(
                detail=(
                    "identity must not be asserted by the client; "
                    f"remove: {', '.join(offending)}"
                )
            )

    def _preflight(self, request: Request) -> None:
        if len(request.body) > self.cfg.max_request_bytes:
            raise errors.PayloadTooLarge()

        self._reject_asserted_identity(request)

        if request.method in WRITE_METHODS:
            if not self._origin_ok(request):
                raise errors.CsrfRejected(detail="Origin/Referer was missing or cross-origin")
            session_id = request.cookies.get(self.cfg.cookie_name)
            if not self.service.verify_write_csrf(session_id, request.header("x-csrf-token")):
                raise errors.CsrfRejected(detail="the CSRF token was missing, wrong or already used")

    def _origin_ok(self, request: Request) -> bool:
        """Same-origin check for state-changing requests.

        An origin is the *tuple* ``scheme://host[:port]``. Comparing only the
        authority, as this used to, accepted ``Origin: http://<same host>`` on
        an HTTPS request — an origin an attacker's plain-HTTP page can set, and
        which the browser will attach to a genuine cross-origin request. The
        whole tuple is compared here.

        The scheme on our side comes from the server's own view of the
        connection (``wsgi.url_scheme``, set by the WSGI container). It must
        never come from ``X-Forwarded-Proto`` or ``Forwarded``: those are
        request headers a client controls, and trusting them would let a
        plain-HTTP request promote itself to HTTPS for this comparison.

        An explicit ``trusted_origins`` allowlist is still honoured, for
        deployments whose public origin differs from ``Host``; it is empty by
        default. ``Origin: null``, scheme-relative and scheme-less values are
        refused, as is an unrecognised request scheme.
        """
        candidate = request.header("origin") or request.header("referer")
        if not candidate:
            return False

        origin = _normalise_origin(candidate)
        if origin is None:
            return False

        trusted = {
            normalised
            for normalised in (_normalise_origin(entry) for entry in self.cfg.trusted_origins)
            if normalised is not None
        }
        if origin in trusted:
            return True

        scheme = (request.scheme or "").lower()
        if scheme not in DEFAULT_PORTS:
            return False

        host = (request.header("host") or request.host or "").strip()
        if not host:
            return False

        return hmac.compare_digest(origin, _normalise_authority(scheme, host))

    # --- endpoints -----------------------------------------------------------

    def _route_csrf(self, request: Request) -> Response:
        token, bound = self.service.issue_csrf(request.cookies.get(self.cfg.cookie_name))
        return Response.json(
            200,
            {
                "ok": True,
                "data": {
                    "csrf_token": token,
                    "authenticated": bound,
                    "providers": self.service.providers(),
                },
            },
        )

    def _route_local_login(self, request: Request) -> Response:
        body = request.json_body()
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            raise errors.InvalidRequest(detail="username and password are required")

        user, issued = self.service.login_local(
            username, password, client_key=request.client_ip
        )
        return Response.json(
            200,
            {
                "ok": True,
                "data": {
                    "user": self.service.public_identity(user),
                    "csrf_token": issued.csrf_token,
                    "session_expires_at": issued.absolute_expires_at,
                },
            },
            headers=[("Set-Cookie", self._session_cookie(issued.session_id))],
        )

    def _route_feishu_start(self, request: Request) -> Response:
        _state, url, browser_nonce = self.service.start_feishu(
            client_key=request.client_ip, next_path=request.query_one("next")
        )
        # The nonce is delivered here and nowhere else: not in the redirect, not
        # in the page, not in a log line. Only this browser ends up holding it.
        return Response.redirect(url, headers=[("Set-Cookie", self._flow_cookie(browser_nonce))])

    def _route_feishu_callback(self, request: Request) -> Response:
        if self.service.provider is None:
            raise errors.NotConfigured()
        if request.query_one("error"):
            # The provider said no (user cancelled, or the app was refused).
            raise errors.AccessDenied()

        state = request.query_one("state")
        code = request.query_one("code")
        if not state or not code:
            raise errors.InvalidRequest(detail="state and code are both required")

        _user, issued, target = self.service.complete_feishu(
            state=state,
            code=code,
            client_key=request.client_ip,
            binding=request.cookies.get(self.cfg.oauth_binding_cookie_name),
        )
        return Response.redirect(
            self._safe_target(target),
            headers=[("Set-Cookie", self._session_cookie(issued.session_id))],
        )

    def _route_me(self, request: Request) -> Response:
        session = self.authorize(
            self.service.current_session(request.cookies.get(self.cfg.cookie_name))
        )
        return Response.json(
            200,
            {
                "ok": True,
                "data": {
                    "authenticated": True,
                    "user": self.service.public_identity(session.user),
                    "projects": self.service.projects_for(session.user),
                    "session_expires_at": session.record.absolute_expires_at,
                },
            },
        )

    def _route_logout(self, request: Request) -> Response:
        session_id = request.cookies.get(self.cfg.cookie_name)
        if not session_id:
            raise errors.NotAuthenticated()
        try:
            session = self.authorize(self.service.current_session(session_id))
        except (errors.NotAuthenticated, errors.AccountDisabled):
            # The credential is already unusable: expired, revoked, or the
            # account is disabled. Logout is still how the browser discards
            # it, and revocation is idempotent. Refusing here would leave the
            # browser holding a cookie it cannot get rid of, while the server
            # already denies every protected request anyway.
            self.service.logout(session_id)
        else:
            self.service.logout(session.session_id)
        return Response.json(
            200,
            {"ok": True, "data": {"logged_out": True}},
            headers=[("Set-Cookie", self.service.sessions.clear_cookie_header(
                name=self.cfg.cookie_name, secure=self.cfg.cookie_secure
            ))],
        )

    # --- scoped tokens -------------------------------------------------------

    def _token_service(self) -> ScopedTokenService:
        if self.tokens is None:
            raise errors.NotConfigured(detail="scoped tokens are not configured")
        return self.tokens

    def _session(self, request: Request):
        """The caller's own live session, or a refusal.

        ``authorize`` rejects a disabled account; the token routes then use
        ``session.user`` as the *only* source of principal. Nothing in the
        request body can name a different owner.
        """
        return self.authorize(
            self.service.current_session(request.cookies.get(self.cfg.cookie_name))
        )

    def _allowed_token_scopes(self, session) -> frozenset[str]:
        """The scopes this session may hand out, or a server fault.

        A policy that is empty, malformed or not a collection is a deployment
        bug, not a client error: it is reported as ``500 server_misconfigured``
        so an operator sees it immediately, instead of surfacing as a puzzling
        ``400`` on every issuance attempt.
        """
        policy = self._token_scope_policy
        granted = DEFAULT_TOKEN_SCOPES if policy is None else policy(session)
        try:
            items = tuple(granted)
        except TypeError:
            raise errors.ConfigError(
                detail="the token scope policy is not a collection of scopes"
            ) from None
        if not items:
            raise errors.ConfigError(detail="the token scope policy granted no scopes")
        try:
            return normalize_scopes(items)
        except errors.InvalidRequest as exc:
            raise errors.ConfigError(
                detail=f"the token scope policy is invalid: {exc.detail}"
            ) from None

    def _route_tokens_list(self, request: Request) -> Response:
        tokens = self._token_service()
        session = self._session(request)
        records = tokens.list_for_principal(session.user.id)
        return Response.json(
            200,
            {"ok": True, "data": {"tokens": [record.to_public_dict() for record in records]}},
        )

    def _route_tokens_issue(self, request: Request) -> Response:
        tokens = self._token_service()
        session = self._session(request)
        body = request.json_body() if request.body else {}

        allowed = self._allowed_token_scopes(session)
        requested = body.get("scopes")
        if requested is None:
            scopes = allowed
        else:
            if not isinstance(requested, list):
                raise errors.InvalidRequest(detail="scopes must be a list of strings")
            wanted = normalize_scopes(requested)
            surplus = wanted - allowed
            if surplus:
                # A member asking for an administrator scope is an attempt at
                # escalation, and it is refused by name rather than filtered
                # down: a token silently missing a scope it was asked for is a
                # support ticket, a refused one is a signal.
                raise errors.Forbidden(detail=f"scopes not permitted: {sorted(surplus)}")
            scopes = wanted

        principal, uid, linux_user = principal_ref(session.user)
        issued = tokens.issue(
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            scopes=scopes,
            roles=(session.user.role,),
            label=body.get("label"),
            display_name=session.user.display_name,
            ttl_seconds=body.get("ttl_seconds"),
        )
        return Response.json(201, {"ok": True, "data": issued.to_public_dict()})

    def _route_tokens_revoke(self, request: Request) -> Response:
        tokens = self._token_service()
        session = self._session(request)
        token_id = self._require_token_id(request)
        revoked = tokens.revoke_for_principal(session.user.id, token_id)
        return Response.json(200, {"ok": True, "data": {"revoked": revoked}})

    def _route_tokens_rotate(self, request: Request) -> Response:
        tokens = self._token_service()
        session = self._session(request)
        body = request.json_body() if request.body else {}
        token_id = self._require_token_id(request)

        # The rotation is bounded by the scopes the **current session** may hand
        # out right now — not by what the old token happened to carry. Without
        # this, a principal whose privileges were reduced could re-issue their
        # old, wider credential by rotating a token they still hold.
        requested = body.get("scopes")
        replacement: list[str] | None
        if requested is None:
            replacement = None
        else:
            if not isinstance(requested, list):
                raise errors.InvalidRequest(detail="scopes must be a list of strings")
            replacement = requested

        issued = tokens.rotate_for_principal(
            session.user.id,
            token_id,
            allowed_scopes=self._allowed_token_scopes(session),
            scopes=replacement,
            ttl_seconds=body.get("ttl_seconds"),
        )
        return Response.json(201, {"ok": True, "data": issued.to_public_dict()})

    def _route_tokens_revoke_all(self, request: Request) -> Response:
        tokens = self._token_service()
        session = self._session(request)
        count = tokens.revoke_all_for_principal(session.user.id)
        return Response.json(200, {"ok": True, "data": {"revoked": count}})

    @staticmethod
    def _require_token_id(request: Request) -> str:
        body = request.json_body()
        token_id = body.get("token_id")
        if not isinstance(token_id, str) or not token_id.strip():
            raise errors.InvalidRequest(detail="token_id is required")
        return token_id.strip()

    # --- helpers -------------------------------------------------------------

    def _session_cookie(self, session_id: str) -> str:
        return self.service.sessions.cookie_header(
            session_id, name=self.cfg.cookie_name, secure=self.cfg.cookie_secure
        )

    def _flow_cookie(self, browser_nonce: str | None) -> str:
        """The OAuth flow cookie, or its removal when ``browser_nonce`` is ``None``.

        ``SameSite=Lax`` is not a preference: the callback arrives as a
        top-level *cross-site* GET navigation from the provider, which
        ``Strict`` would strip the cookie from, while anything looser than
        ``Lax`` would let the nonce ride along on cross-site subresource
        requests. ``HttpOnly`` keeps it out of reach of page script and
        ``Secure`` keeps it off plain HTTP. No ``Domain`` attribute, so it is
        host-only. The lifetime is the flow's own.
        """
        parts = [f"{self.cfg.oauth_binding_cookie_name}={browser_nonce or ''}", "Path=/"]
        parts.append(
            "Max-Age=0" if browser_nonce is None else f"Max-Age={OAUTH_BINDING_TTL_SECONDS}"
        )
        parts += ["HttpOnly", "SameSite=Lax"]
        if self.cfg.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _clear_flow_cookie(self, response: Response) -> Response:
        response.headers.append(("Set-Cookie", self._flow_cookie(None)))
        return response

    def _safe_target(self, target: str | None) -> str:
        return target if is_local_path(target) else self.cfg.post_login_path

    def _callback_redirect(self, exc: errors.AuthError) -> Response:
        query = urllib.parse.urlencode(exc.redirect_query())
        return Response.redirect(f"{self.cfg.login_error_path}?{query}")

    def _error_response(self, exc: errors.AuthError) -> Response:
        headers: list[tuple[str, str]] = []
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            headers.append(("Retry-After", str(int(retry_after))))
        return Response.json(exc.http_status, exc.to_dict(), headers=headers)

    def _envelope(self, status: int, code: str, message: str) -> Response:
        return Response.json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _finish(self, response: Response) -> Response:
        response.headers.extend(
            [
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
            ]
        )
        return response

    def _record_internal_error(self) -> None:
        try:
            self.service.db.record_audit("route", "error", detail="unhandled exception")
        except Exception:  # noqa: BLE001 - auditing must not mask the original failure
            pass

    # --- WSGI ----------------------------------------------------------------

    def __call__(self, environ, start_response):
        from . import wsgi  # imported lazily to keep the module graph acyclic

        return wsgi.serve(self, environ, start_response)


def create_app(cfg: AuthConfig, **kwargs: Any) -> Application:
    return Application(cfg, **kwargs)


__all__ = [
    "ASSERTED_IDENTITY_FIELDS",
    "Application",
    "DEFAULT_TOKEN_SCOPES",
    "REASON_PHRASES",
    "ROUTES",
    "Request",
    "Response",
    "create_app",
]
