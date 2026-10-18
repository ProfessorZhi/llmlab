"""Error taxonomy for the authentication backend.

Every failure that reaches an HTTP client is rendered through :class:`AuthError`
so that the response envelope stays stable and, more importantly, so that no
failure path can leak a credential, an internal identifier or an upstream body.

Envelope (identical shape for every JSON failure)::

    {"ok": false, "error": {"code": "<snake_case>", "message": "<generic text>"}}

``oauth_error`` carries the OIDC-style token used when we have to bounce the
browser back to the login page with ``?error=``. ``detail`` is diagnostic
material for server-side logs only and is deliberately *not* serialised.

``message`` strings must stay generic: they describe what happened, never what
was supplied. In particular nothing in this module mentions passwords, hashes
or upstream tokens.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AuthError(Exception):
    """Base class for every authentication failure."""

    code: ClassVar[str] = "server_error"
    message: ClassVar[str] = "Authentication request failed."
    http_status: ClassVar[int] = 500
    oauth_error: ClassVar[str] = "server_error"

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        resolved = message or type(self).message
        super().__init__(resolved)
        # Instance attribute shadows the class default so callers may refine the
        # public wording without inventing a new error class.
        self.message = resolved
        # Server-side only. Never reaches to_dict() / the wire.
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message}}

    def redirect_query(self) -> dict[str, str]:
        """Query parameters appended to the fixed local error path."""
        return {"error": self.oauth_error, "reason": self.code}

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --- client errors -----------------------------------------------------------


class InvalidRequest(AuthError):
    code = "invalid_request"
    message = "The request is malformed."
    http_status = 400
    oauth_error = "invalid_request"


class NotAuthenticated(AuthError):
    code = "not_authenticated"
    message = "Authentication required."
    http_status = 401
    oauth_error = "invalid_token"


class SessionExpired(NotAuthenticated):
    code = "session_expired"
    message = "Your session has expired. Please sign in again."


class Forbidden(AuthError):
    code = "forbidden"
    message = "You do not have access to this resource."
    http_status = 403
    oauth_error = "insufficient_scope"


class AccountDisabled(Forbidden):
    code = "account_disabled"
    message = "This account is disabled."
    oauth_error = "access_denied"


class CsrfRejected(Forbidden):
    code = "csrf_rejected"
    message = "Request rejected."


class InvalidCredentials(NotAuthenticated):
    code = "invalid_credentials"
    # Deliberately identical for "no such user", "wrong password" and
    # "rate limited out of the comparison": the client must not be able to
    # tell these apart.
    message = "Authentication failed."
    oauth_error = "invalid_grant"


class IdentityAsserted(InvalidRequest):
    """The request tried to name a principal, UID, role or owner itself.

    Same status and code the BFF and Registry use for the same class of
    request, so one forged field has one answer across the HTTP surface. The
    field is refused rather than ignored: an ignored assertion is one a later
    layer can pick up again.
    """

    code = "identity_assertion_rejected"
    message = "Identity must not be asserted by the client."
    oauth_error = "invalid_request"


class InvalidState(InvalidRequest):
    code = "invalid_state"
    message = "The sign-in request is invalid or has expired."
    # Kept distinct from a generic bad request so a bounced callback can say
    # "your sign-in attempt expired" rather than "the request was malformed".
    oauth_error = "invalid_state"


class AccessDenied(AuthError):
    code = "access_denied"
    message = "The sign-in request was denied."
    http_status = 403
    oauth_error = "access_denied"


class RateLimited(AuthError):
    code = "rate_limited"
    message = "Too many attempts. Please try again later."
    http_status = 429
    oauth_error = "temporarily_unavailable"

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PayloadTooLarge(InvalidRequest):
    code = "payload_too_large"
    message = "The request is too large."
    http_status = 413


# --- server / upstream errors ------------------------------------------------


class NotConfigured(AuthError):
    code = "not_configured"
    message = "This sign-in method is not configured on this server."
    http_status = 503
    oauth_error = "temporarily_unavailable"


class ProviderUnavailable(AuthError):
    code = "provider_unavailable"
    message = "The identity provider is temporarily unavailable."
    http_status = 502
    oauth_error = "temporarily_unavailable"


class ConfigError(AuthError):
    """Raised while loading configuration. Fail closed, refuse to start."""

    code = "server_misconfigured"
    message = "Server authentication configuration is invalid."
    http_status = 500
    oauth_error = "server_error"


__all__ = [
    "AccessDenied",
    "AccountDisabled",
    "AuthError",
    "ConfigError",
    "CsrfRejected",
    "Forbidden",
    "IdentityAsserted",
    "InvalidCredentials",
    "InvalidRequest",
    "InvalidState",
    "NotAuthenticated",
    "NotConfigured",
    "PayloadTooLarge",
    "ProviderUnavailable",
    "RateLimited",
    "SessionExpired",
]
