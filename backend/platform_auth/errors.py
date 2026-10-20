"""Typed errors for platform authentication.

Every error carries a *stable, non-enumerating* code. Codes are the only thing
that reaches a client; details stay inside for the audit log and the tests, and
never include a password, a token, a hash, or a Linux account name.
"""

from __future__ import annotations


class PlatformAuthError(Exception):
    """Base class. ``code`` is safe to return; ``detail`` is not."""

    code = "platform_auth_error"
    status = 400

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.code)
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": {"code": self.code}}


class ValidationError(PlatformAuthError):
    code = "invalid_request"
    status = 400


class InvalidCredentials(PlatformAuthError):
    """Wrong password, unknown login, or empty secret — one indistinguishable answer."""

    code = "invalid_credentials"
    status = 401


class NotAuthenticated(PlatformAuthError):
    code = "not_authenticated"
    status = 401


class Forbidden(PlatformAuthError):
    code = "forbidden"
    status = 403


class AccountDisabled(PlatformAuthError):
    """Only raised *after* a correct password, so it cannot enumerate accounts."""

    code = "account_disabled"
    status = 403


class RateLimited(PlatformAuthError):
    code = "rate_limited"
    status = 429


class CsrfRejected(PlatformAuthError):
    code = "csrf_rejected"
    status = 403


class StorageSecurityError(PlatformAuthError):
    """The state directory or database file cannot be trusted; refuse to start."""

    code = "unsafe_state_storage"
    status = 500


__all__ = [
    "AccountDisabled",
    "CsrfRejected",
    "Forbidden",
    "InvalidCredentials",
    "NotAuthenticated",
    "PlatformAuthError",
    "RateLimited",
    "StorageSecurityError",
    "ValidationError",
]
