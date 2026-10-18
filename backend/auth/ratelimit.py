"""Login throttling.

Counters live in the shared database rather than in process memory so that the
limit holds across WSGI workers, across restarts, and across however many
processes a future deployment decides to run.

The shape is: failures are counted inside a sliding *window*; reaching
``max_failures`` sets a ``locked_until`` stamp. A successful authentication
clears the counter. Both the window and the lockout are evaluated against an
injected clock, which is what makes the expiry behaviour testable offline.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from . import errors
from .store import Database

SCOPE_LOCAL_LOGIN = "local_login"
SCOPE_FEISHU_START = "feishu_start"
SCOPE_FEISHU_CALLBACK = "feishu_callback"


class RateLimiter:
    def __init__(
        self,
        db: Database,
        *,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._max_failures = max(1, int(max_failures))
        self._window = max(1, int(window_seconds))
        self._lockout = max(1, int(lockout_seconds))
        self._clock = clock

    def check(self, scope: str, key: str) -> None:
        """Raise :class:`errors.RateLimited` when ``key`` is currently locked out."""
        row = self._db.read_attempt(scope, key)
        if row is None:
            return
        window_started_at, failures, locked_until = row
        now = self._clock()

        if locked_until > now:
            raise errors.RateLimited(retry_after=max(1, int(math.ceil(locked_until - now))))

        if failures >= self._max_failures and (now - window_started_at) < self._window:
            # The lockout lapsed but the failure window has not: stay refused,
            # and do not extend the lock beyond one lockout period.
            raise errors.RateLimited(retry_after=self._lockout)

    def record_failure(self, scope: str, key: str) -> None:
        now = self._clock()
        row = self._db.read_attempt(scope, key)
        if row is None or (now - row[0]) >= self._window:
            window_started_at, failures, locked_until = now, 0, 0.0
        else:
            window_started_at, failures, locked_until = row

        failures += 1
        if failures >= self._max_failures:
            failures = self._max_failures
            locked_until = now + self._lockout
        self._db.write_attempt(scope, key, window_started_at, failures, locked_until)

    def record_success(self, scope: str, key: str) -> None:
        self._db.clear_attempt(scope, key)


__all__ = ["RateLimiter", "SCOPE_FEISHU_CALLBACK", "SCOPE_FEISHU_START", "SCOPE_LOCAL_LOGIN"]
