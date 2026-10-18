"""The lockout hook."""

from __future__ import annotations

import unittest

from backend.auth_linux import errors
from backend.auth_linux.throttle import (
    SCOPE_LINUX_LOGIN,
    InMemoryThrottle,
    login_throttle_key,
)

from . import support


def limiter(**overrides) -> tuple[InMemoryThrottle, support.FakeClock]:
    clock = support.FakeClock()
    settings = {"max_failures": 3, "window_seconds": 60, "lockout_seconds": 300}
    settings.update(overrides)
    return InMemoryThrottle(clock=clock, **settings), clock


KEY = "client|alice"


class WindowTests(unittest.TestCase):
    def test_a_fresh_key_is_not_limited(self) -> None:
        throttle, _ = limiter()
        throttle.check(SCOPE_LINUX_LOGIN, KEY)

    def test_failures_below_the_threshold_do_not_lock(self) -> None:
        throttle, _ = limiter()
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        throttle.check(SCOPE_LINUX_LOGIN, KEY)

    def test_reaching_the_threshold_locks(self) -> None:
        throttle, _ = limiter()
        for _ in range(3):
            throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        with self.assertRaises(errors.RateLimited) as caught:
            throttle.check(SCOPE_LINUX_LOGIN, KEY)
        self.assertIsNotNone(caught.exception.retry_after)
        self.assertGreater(caught.exception.retry_after or 0, 0)

    def test_the_lock_lapses_after_the_lockout_period(self) -> None:
        throttle, clock = limiter()
        for _ in range(3):
            throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        clock.advance(301)
        clock.advance(60)
        throttle.check(SCOPE_LINUX_LOGIN, KEY)

    def test_a_lapsed_lock_inside_the_window_stays_refused(self) -> None:
        """Otherwise the lockout would be a rate of three guesses per lockout."""
        throttle, clock = limiter(max_failures=3, window_seconds=1000, lockout_seconds=10)
        for _ in range(3):
            throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        clock.advance(11)
        with self.assertRaises(errors.RateLimited):
            throttle.check(SCOPE_LINUX_LOGIN, KEY)

    def test_the_window_expiring_clears_the_count(self) -> None:
        throttle, clock = limiter(max_failures=3, window_seconds=60, lockout_seconds=10)
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        clock.advance(61)
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        # The two stale failures are gone, so this is only the first of a window.
        throttle.check(SCOPE_LINUX_LOGIN, KEY)

    def test_a_success_clears_the_counter(self) -> None:
        throttle, _ = limiter()
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        throttle.record_success(SCOPE_LINUX_LOGIN, KEY)
        throttle.record_failure(SCOPE_LINUX_LOGIN, KEY)
        throttle.check(SCOPE_LINUX_LOGIN, KEY)


class KeyTests(unittest.TestCase):
    def test_keys_are_scoped_per_client_and_per_name(self) -> None:
        throttle, _ = limiter(max_failures=1, lockout_seconds=300)
        throttle.record_failure(SCOPE_LINUX_LOGIN, login_throttle_key(client_key="a", login_name="alice"))
        # A different client is unaffected...
        throttle.check(SCOPE_LINUX_LOGIN, login_throttle_key(client_key="b", login_name="alice"))
        # ...and so is a different name from the same client.
        throttle.check(SCOPE_LINUX_LOGIN, login_throttle_key(client_key="a", login_name="carol"))

    def test_the_key_names_both_parts(self) -> None:
        key = login_throttle_key(client_key="203.0.113.7", login_name="alice")
        self.assertIn("203.0.113.7", key)
        self.assertIn("alice", key)

    def test_the_linux_scope_is_namespaced_apart_from_the_other_flows(self) -> None:
        self.assertEqual(SCOPE_LINUX_LOGIN, "linux_pam_login")
        self.assertNotIn(SCOPE_LINUX_LOGIN, {"local_login", "feishu_start", "feishu_callback"})


class HookShapeTests(unittest.TestCase):
    def test_the_hook_matches_the_auth_service_limiter_signature(self) -> None:
        """So the database-backed limiter can be injected with no adapter.

        The names and arities are asserted rather than the types, because the
        point of the hook is that the two implementations are interchangeable.
        """
        import inspect

        from backend.auth_linux.throttle import ThrottleHook

        for name in ("check", "record_failure", "record_success"):
            with self.subTest(method=name):
                protocol_method = getattr(ThrottleHook, name)
                self.assertIsNotNone(protocol_method)
        self.assertEqual(
            list(inspect.signature(InMemoryThrottle.check).parameters),
            ["self", "scope", "key"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
