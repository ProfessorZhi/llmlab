"""Traffic governance: concurrency, a bounded queue, and a timeout.

WHY THIS IS NOT DECORATION.  Strata serves one request at a time -- the engine
runs with `parallel 1` and one RTX A6000, and a long-context call holds the
whole box.  Without a gate in front, "a few colleagues can try the model"
degrades into "the last person to ask wins, and everybody else's client
silently times out".  The failure is not a slowdown, it is that a second caller
makes the *first* caller's request slower than anybody's timeout, so the
symptom gets reported against the model.

So there are two limits and a queue between them:

  * a **global** limit -- the number of requests the engine can actually run at
    once.  It is a property of the deployment, not of a user, and it is the
    number that must match `parallel`.
  * a **per-user** limit -- so that one person cannot occupy every slot.  With
    a global limit of 1 this is redundant; with a global limit of 4 and no
    per-user limit, one script with a thread pool is the whole queue.
  * a **bounded** queue, because an unbounded one converts load into latency
    rather than into a refusal, and a client waiting 20 minutes for a 429
    would rather have had the 429.

WHY THE TIMEOUT IS A REFUSAL AND NOT A WAIT FOREVER.  A caller that waits past
its own client-side timeout has already failed; holding its slot in the queue
just spends capacity on a request whose answer nobody will read.  `Retry-After`
tells a well-behaved client when to come back instead of it guessing.

Everything here counts, and the counts are per user, because "who is using the
box" is the question the gateway exists to answer.
"""

from __future__ import annotations

import threading
import time


class TooMany(Exception):
    """The request was refused before reaching the model.

    Carries `retry_after` so the HTTP layer does not have to invent one, and
    `scope` so the message can say which limit was hit -- "the service is busy"
    and "you already have a request running" are the same number in the log and
    different instructions to the person reading it.
    """

    def __init__(self, scope: str, detail: str, retry_after: float):
        super().__init__(detail)
        self.scope = scope
        self.detail = detail
        self.retry_after = retry_after


class Gate:
    """A counting gate over one condition variable.

    One lock and one condition, rather than a semaphore per limit: the two
    limits and the queue bound have to be evaluated as a single predicate, and
    three independent primitives would produce a state where a thread holds the
    per-user semaphore while waiting for the global one -- which is a deadlock
    waiting for the right interleaving, and a slow leak of slots the rest of
    the time.
    """

    def __init__(self, global_limit: int = 1, per_user_limit: int = 1,
                 queue_limit: int = 8, wait_timeout_s: float = 120.0,
                 retry_after_s: float = 5.0):
        if global_limit < 1:
            raise ValueError("global_limit must be at least 1")
        if per_user_limit < 1:
            raise ValueError("per_user_limit must be at least 1")
        if queue_limit < 0:
            raise ValueError("queue_limit cannot be negative")
        self.global_limit = int(global_limit)
        self.per_user_limit = int(per_user_limit)
        self.queue_limit = int(queue_limit)
        self.wait_timeout_s = float(wait_timeout_s)
        self.retry_after_s = float(retry_after_s)

        self._cv = threading.Condition()
        self._active = 0
        self._per_user: dict[str, int] = {}
        self._waiting = 0
        self._accepted: dict[str, int] = {}
        self._refused: dict[str, int] = {}
        # Wall-clock peak, so a status reader can see whether the gate ever
        # actually saturated -- a limit that has never been reached is a limit
        # whose value has not been tested by anything.
        self._peak_active = 0
        self._peak_waiting = 0

    # -- admission -------------------------------------------------------
    def _can_take(self, user: str) -> bool:
        return (self._active < self.global_limit
                and self._per_user.get(user, 0) < self.per_user_limit)

    def _take(self, user: str) -> None:
        self._active += 1
        self._per_user[user] = self._per_user.get(user, 0) + 1
        self._accepted[user] = self._accepted.get(user, 0) + 1
        self._peak_active = max(self._peak_active, self._active)

    def acquire(self, user: str) -> float:
        """Block until a slot is free, or raise `TooMany`.

        Raises at once when the queue is full, and after `wait_timeout_s` when
        it is not: the first is "come back later, we are not even holding
        you", the second is "we held you and it did not free up".

        Returns how long the caller waited, in seconds.  It is returned rather
        than left for the caller to measure because the caller measures from
        before the call and this measures from inside the lock -- so the number
        that reaches the record is the time actually spent queueing, not the
        time spent queueing plus however long the thread took to be scheduled.
        """
        user = user or "?"
        entered = time.monotonic()
        deadline = entered + self.wait_timeout_s
        with self._cv:
            while not self._can_take(user):
                if self._waiting >= self.queue_limit:
                    self._refused[user] = self._refused.get(user, 0) + 1
                    raise TooMany(
                        "queue", "the queue is full (%d waiting)" % self._waiting,
                        self.retry_after_s)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._refused[user] = self._refused.get(user, 0) + 1
                    raise TooMany(
                        "timeout",
                        "waited %.0fs for a slot and none came free"
                        % self.wait_timeout_s, self.retry_after_s)
                self._waiting += 1
                self._peak_waiting = max(self._peak_waiting, self._waiting)
                try:
                    self._cv.wait(min(remaining, 5.0))
                finally:
                    self._waiting -= 1
            self._take(user)
        return time.monotonic() - entered

    def release(self, user: str) -> None:
        """Return a slot.  Safe to call exactly once per successful acquire.

        The caller must call it on every path out of a request, including a
        client that hung up mid-stream: a slot leaked on disconnect is a slot
        the box never gets back, and the symptom is "it works, then after a
        while every request 429s" with nothing in the log explaining why.
        """
        user = user or "?"
        with self._cv:
            self._active = max(0, self._active - 1)
            left = self._per_user.get(user, 0) - 1
            if left > 0:
                self._per_user[user] = left
            else:
                self._per_user.pop(user, None)
            self._cv.notify_all()

    # -- observation -----------------------------------------------------
    def stats(self) -> dict:
        with self._cv:
            return {
                "global_limit": self.global_limit,
                "per_user_limit": self.per_user_limit,
                "queue_limit": self.queue_limit,
                "wait_timeout_s": self.wait_timeout_s,
                "active": self._active,
                "waiting": self._waiting,
                "peak_active": self._peak_active,
                "peak_waiting": self._peak_waiting,
                "accepted_by_user": dict(self._accepted),
                "refused_by_user": dict(self._refused),
            }
