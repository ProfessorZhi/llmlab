"""Client-facing errors for the Linux sign-in path.

The envelope is deliberately byte-identical in shape to the one the existing
auth service already emits::

    {"ok": false, "error": {"code": "<snake_case>", "message": "<generic>"}}

so the V5 UI keeps a single error-rendering path regardless of which sign-in
method produced the failure.

Two rules govern this module, and both are tested:

1. **Nothing here is derived from what the caller supplied.** No message
   mentions a password, a hash, a shadow entry, a UID, a shell, an allowlist
   path, or whether a particular Linux account exists. Messages describe what
   happened, never what was given.
2. **Only these errors cross the boundary.** Everything the policy and provider
   layers learn internally travels as a *reason* (see
   :mod:`backend.auth_linux.reasons`) and is collapsed onto this small set
   before it can reach a client. That collapse is what stops the login form
   from becoming a user-enumeration oracle.
"""

from __future__ import annotations

from typing import Any, ClassVar


class LinuxAuthError(Exception):
    """Base class for every failure this package reports to a caller."""

    code: ClassVar[str] = "server_error"
    message: ClassVar[str] = "Authentication request failed."
    http_status: ClassVar[int] = 500

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        resolved = message or type(self).message
        super().__init__(resolved)
        # Instance attribute shadows the class default so a caller can refine the
        # public wording without inventing a new class.
        self.message = resolved
        # Server-side only. Never serialised, never returned to a client, never
        # logged with request material attached.
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message}}

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"{type(self).__name__}(code={self.code!r})"


class InvalidRequest(LinuxAuthError):
    code = "invalid_request"
    message = "The request is malformed."
    http_status = 400


class InvalidCredentials(LinuxAuthError):
    """The single public outcome of every credential *and* account refusal.

    A wrong password, an unknown account, a non-allowlisted account, a locked
    account, an expired password and an account that fails its PAM account
    checks are all this same error, with this same wording and — as far as the
    handler is concerned — the same timing. A caller learns that the attempt
    failed and nothing else.
    """

    code = "invalid_credentials"
    message = "Authentication failed."
    http_status = 401


class RateLimited(LinuxAuthError):
    code = "rate_limited"
    message = "Too many attempts. Please try again later."
    http_status = 429

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotConfigured(LinuxAuthError):
    """The sign-in method is switched off, or switched on incompletely."""

    code = "not_configured"
    message = "This sign-in method is not configured on this server."
    http_status = 503


class PamUnavailable(LinuxAuthError):
    """The check could not be performed, so it did not happen.

    Raised for an absent broker, an unreachable or timing-out broker, a
    malformed reply, an oversized frame, a refused peer, or an internal broker
    fault. This is a *refusal*, not a pass: callers must render it as "try
    again later", never as "signed in".
    """

    code = "pam_unavailable"
    # "Sign-in", not "password verification": the wording is asserted against a
    # forbidden-word list, and keeping the guarantee absolute (no message this
    # module can produce mentions a credential, even benignly) is worth more
    # than the slightly more precise phrasing.
    message = "Sign-in is temporarily unavailable. Please try again later."
    http_status = 503


class ConfigError(LinuxAuthError):
    """Raised while loading configuration. Fail closed; refuse to run."""

    code = "server_misconfigured"
    message = "Server authentication configuration is invalid."
    http_status = 500


__all__ = [
    "ConfigError",
    "InvalidCredentials",
    "InvalidRequest",
    "LinuxAuthError",
    "NotConfigured",
    "PamUnavailable",
    "RateLimited",
]
