"""The lockout / rate-limit hook.

This module deliberately defines a *hook* rather than a limiter. The auth
service already owns a database-backed limiter whose counters survive restarts
and are shared by every WSGI worker, and a second, independent limiter living in
this package would be a weaker duplicate that also forgets everything on
restart. So the interface below is written to be satisfied by that limiter
unchanged: it is the same ``check(scope, key)`` / ``record_failure`` /
``record_success`` triple, with the scope namespaced so Linux sign-ins get their
own counters instead of sharing the local-admin ones.

An in-memory implementation is provided for two cases — a deployment with no
database, and tests — and is documented as the weaker option rather than
presented as equivalent.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol

from . import errors

# Namespaced so these counters can never be confused with, or spent by, the
# local-administrator or Feishu flows.
SCOPE_LINUX_LOGIN = "linux_pam_login"


class ThrottleHook(Protocol):
    """Satisfied by :class:`backend.auth.ratelimit.RateLimiter` as it already is.

    Duplicating the signature on purpose: no adapter layer, no wrapper that can
    drift, and no import of the auth package from this one.
    """

    def check(self, scope: str, key: str) -> None: ...

    def record_failure(self, scope: str, key: str) -> None: ...

    def record_success(self, scope: str, key: str) -> None: ...


class InMemoryThrottle:
    """A sliding-window failure counter with a lockout, held in process memory.

    Semantics match the auth service's limiter: failures are counted inside a
    window, reaching the maximum stamps a lockout, and a success clears the
    counter. Everything is evaluated against an injected clock so expiry is
    testable without sleeping.

    The limitation is the point of the docstring: counters live in one process
    and vanish on restart, so an attacker who can make the process restart gets
    a fresh budget. Prefer the shared, database-backed limiter.
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_seconds: int = 300,
        lockout_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_failures = max(1, int(max_failures))
        self._window = max(1, int(window_seconds))
        self._lockout = max(1, int(lockout_seconds))
        self._clock = clock
        self._attempts: dict[tuple[str, str], tuple[float, int, float]] = {}

    def check(self, scope: str, key: str) -> None:
        row = self._attempts.get((scope, key))
        if row is None:
            return
        window_started_at, failures, locked_until = row
        now = self._clock()

        if locked_until > now:
            raise errors.RateLimited(retry_after=max(1, int(math.ceil(locked_until - now))))

        if failures >= self._max_failures and (now - window_started_at) < self._window:
            raise errors.RateLimited(retry_after=self._lockout)

    def record_failure(self, scope: str, key: str) -> None:
        now = self._clock()
        row = self._attempts.get((scope, key))
        if row is None or (now - row[0]) >= self._window:
            window_started_at, failures, locked_until = now, 0, 0.0
        else:
            window_started_at, failures, locked_until = row

        failures += 1
        if failures >= self._max_failures:
            failures = self._max_failures
            locked_until = now + self._lockout
        self._attempts[(scope, key)] = (window_started_at, failures, locked_until)

    def record_success(self, scope: str, key: str) -> None:
        self._attempts.pop((scope, key), None)


def login_throttle_key(*, client_key: str, login_name: str) -> str:
    """The key a Linux sign-in attempt is counted against.

    Composed of the client and the submitted name, and used for every attempt —
    including ones naming accounts that do not exist. Counting only real
    accounts would make a ``429`` a confirmation that the account exists, which
    is precisely the enumeration the rest of this package works to prevent.
    """
    return f"{client_key}|{login_name}"


__all__ = [
    "SCOPE_LINUX_LOGIN",
    "InMemoryThrottle",
    "ThrottleHook",
    "login_throttle_key",
]
