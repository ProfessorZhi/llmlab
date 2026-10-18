"""Internal denial reasons, and the total mapping onto client-visible errors.

This module is the whole anti-enumeration story, kept in one auditable place.

The policy and provider layers are *allowed* to know precisely why an attempt
failed — that precision is what makes the audit trail worth reading, and what
lets an operator answer "why can't I sign in?" without guessing. None of that
precision may reach the client. Each reason therefore passes through
:func:`client_error_for`, which is **total**: it maps every known reason, and it
maps anything unrecognised (a future reason someone forgot to classify, a
corrupted value) to the same generic refusal as a wrong password. Forgetting to
classify a new reason can make the audit trail vaguer; it can never widen what
the client learns, and it can never turn a refusal into a success.

Only three families are visible to the client, and each is visible for a
reason that does not depend on the caller's input:

``rate_limited``
    Throttling is applied on a key composed of the client and the submitted
    name, and is charged identically whether or not the account exists, so a
    ``429`` does not answer "does this account exist?".
``pam_unavailable`` / ``not_configured``
    A statement about the *server's* state, identical for every caller.
``invalid_credentials``
    Everything else. This is the answer to every question about a specific
    account.
"""

from __future__ import annotations

from . import errors

# --- reasons the layers below are allowed to distinguish ----------------------


class DenialReason:
    """String constants. Not an ``Enum`` so audit records stay plain JSON."""

    # The request itself, before any account is considered.
    INVALID_NAME = "invalid_name"
    EMPTY_PASSWORD = "empty_password"
    NOT_CONFIGURED = "not_configured"

    # Local, read-only facts about the account.
    UNKNOWN_USER = "unknown_user"
    NOT_ALLOWLISTED = "not_allowlisted"
    ROOT_FORBIDDEN = "root_forbidden"
    SYSTEM_ACCOUNT = "system_account"
    NON_LOGIN_SHELL = "non_login_shell"
    EXPLICITLY_DENIED = "explicitly_denied"

    # Throttling.
    RATE_LIMITED = "rate_limited"

    # Outcomes reported by the broker after PAM ran.
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_EXPIRED = "account_expired"
    PASSWORD_EXPIRED = "password_expired"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_CHECK_FAILED = "account_check_failed"

    # Disagreement between the broker's answer and our own read-only view.
    UID_MISMATCH = "uid_mismatch"

    # The broker could not be used at all.
    BROKER_UNAVAILABLE = "broker_unavailable"
    BROKER_PROTOCOL_ERROR = "broker_protocol_error"


ALL_REASONS = frozenset(
    value
    for name, value in vars(DenialReason).items()
    if not name.startswith("_") and isinstance(value, str)
)

# The reasons that keep their own client-visible error.
#
# Everything absent from this table is collapsed onto InvalidCredentials by
# `client_error_for`. Adding a reason here is therefore a deliberate act that
# widens the client's view and should be justified in review.
_CLIENT_VISIBLE = {
    DenialReason.RATE_LIMITED: errors.RateLimited,
    DenialReason.BROKER_UNAVAILABLE: errors.PamUnavailable,
    DenialReason.BROKER_PROTOCOL_ERROR: errors.PamUnavailable,
    DenialReason.NOT_CONFIGURED: errors.NotConfigured,
}


def client_error_for(reason: str, *, retry_after: int | None = None) -> errors.LinuxAuthError:
    """Map an internal reason onto the error a client is allowed to see.

    Total by construction: an unrecognised reason is treated as an ordinary
    credential refusal rather than being passed through, so no future reason can
    leak by omission.
    """
    error_class = _CLIENT_VISIBLE.get(reason)
    if error_class is None:
        return errors.InvalidCredentials()
    if error_class is errors.RateLimited:
        return errors.RateLimited(retry_after=retry_after)
    return error_class()


def is_client_visible(reason: str) -> bool:
    """True only for reasons that intentionally keep a distinct public error."""
    return reason in _CLIENT_VISIBLE


__all__ = [
    "ALL_REASONS",
    "DenialReason",
    "client_error_for",
    "is_client_visible",
]
