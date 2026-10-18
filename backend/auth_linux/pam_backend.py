"""The seam where PAM is actually called, on the privileged side.

A :class:`PamBackend` performs exactly two PAM operations and no others:

``pam_authenticate``
    Does the supplied password belong to this account?
``pam_acct_mgmt``
    Is the account presently usable — not locked, not expired, no forced
    password change pending?

It is not a session, it does not open one, and it never runs a command. The
distinction matters because PAM stacks are configured ``auth``/``account`` first
and ``session`` last, and reaching into the ``session`` phase of a stack that
contains ``pam_unix`` is how a password check turns into a login shell.

**The default backend is :class:`UnavailablePamBackend`, and it refuses
everything.** That is the honest state of this deployment: PAM has not been
wired up, no broker has been installed, and no Linux account's password has ever
been checked against this code. When the broker is installed the operator
substitutes a real backend; until then every request fails closed and the UI
says verification is unavailable.

A note on test doubles: the suite runs against a synthetic
:class:`PamBackend` supplied by the tests themselves. Such a double proves the
*surrounding* machinery — socket handling, peer checks, allowlist enforcement,
status mapping, refusal paths — and proves nothing whatsoever about PAM. A fake
that says "authenticated" is not evidence that any real password would be
accepted, and this module does not contain one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import errors


class PamOutcome:
    """What the PAM conversation concluded."""

    AUTHENTICATED = "authenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_EXPIRED = "account_expired"
    PASSWORD_EXPIRED = "password_expired"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_CHECK_FAILED = "account_check_failed"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


ALL_OUTCOMES = frozenset(
    value
    for name, value in vars(PamOutcome).items()
    if not name.startswith("_") and isinstance(value, str)
)


@dataclass(frozen=True)
class PamResult:
    outcome: str
    # Server-side diagnostic only. It stays inside the broker and is never put on
    # the wire, because PAM's own error text can name the module that rejected
    # the attempt and that is more than a client needs to know.
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ALL_OUTCOMES:
            raise ValueError(f"unknown PAM outcome: {self.outcome!r}")


class PamBackend(Protocol):
    def verify(self, *, user: str, password: str) -> PamResult: ...


class UnavailablePamBackend:
    """Refuses every attempt. The default, and the only one shipped enabled.

    This exists so that "PAM is not configured" is a first-class, tested state
    rather than a missing code path. An implementation that is absent is an
    implementation nobody audited; an implementation that says ``UNAVAILABLE``
    every time is one whose behaviour we can state exactly.
    """

    def __init__(self, detail: str = "no PAM backend is configured") -> None:
        self._detail = detail

    def verify(self, *, user: str, password: str) -> PamResult:
        # Neither argument is inspected, and the password is not retained.
        return PamResult(outcome=PamOutcome.UNAVAILABLE, detail=self._detail)


# The name a deployment writes into its broker configuration to select a
# backend. Only the refusing one exists here; see docs/linux-pam-login.md for
# what an installer must implement and validate before adding another.
BACKEND_UNAVAILABLE = "unavailable"


def build_pam_backend(name: str) -> PamBackend:
    """Resolve a configured backend name, refusing anything we do not ship.

    Unknown names raise rather than falling back, so a typo in the broker's
    configuration stops the broker instead of quietly leaving it with a backend
    nobody intended.
    """
    if name == BACKEND_UNAVAILABLE:
        return UnavailablePamBackend()
    raise errors.ConfigError(detail=f"unknown PAM backend: {name!r}")


__all__ = [
    "ALL_OUTCOMES",
    "BACKEND_UNAVAILABLE",
    "PamBackend",
    "PamOutcome",
    "PamResult",
    "UnavailablePamBackend",
    "build_pam_backend",
]
