"""Tests for backend.auth.ratelimit — persistent, cross-process login throttling."""

import pathlib
import shutil
import tempfile
import unittest

from backend.auth import errors, ratelimit, store
from tests.auth.test_store import FakeClock


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-rl-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "auth.sqlite3"
        self.clock = FakeClock()
        self.db = store.Database(self.path, clock=self.clock)
        self.db.initialize()
        self.limiter = ratelimit.RateLimiter(
            self.db,
            max_failures=3,
            window_seconds=300,
            lockout_seconds=900,
            clock=self.clock,
        )

    def test_lockout_trips_exactly_at_the_threshold(self):
        self.limiter.record_failure("local_login", "admin")
        self.limiter.record_failure("local_login", "admin")
        self.limiter.check("local_login", "admin")  # still allowed
        self.limiter.record_failure("local_login", "admin")
        with self.assertRaises(errors.RateLimited):
            self.limiter.check("local_login", "admin")

    def test_two_failures_do_not_trip_the_lockout(self):
        self.limiter.record_failure("local_login", "admin")
        self.limiter.record_failure("local_login", "admin")
        self.limiter.check("local_login", "admin")  # must not raise

    def test_lockout_reports_a_positive_retry_after(self):
        for _ in range(3):
            self.limiter.record_failure("local_login", "admin")
        with self.assertRaises(errors.RateLimited) as ctx:
            self.limiter.check("local_login", "admin")
        self.assertIsNotNone(ctx.exception.retry_after)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_other_keys_are_unaffected(self):
        for _ in range(3):
            self.limiter.record_failure("local_login", "admin")
        self.limiter.check("local_login", "someone-else")
        self.limiter.check("feishu_start", "admin")

    def test_window_rollover_forgets_old_failures(self):
        self.limiter.record_failure("local_login", "admin")
        self.limiter.record_failure("local_login", "admin")
        self.clock.advance(301)
        self.limiter.record_failure("local_login", "admin")
        self.limiter.check("local_login", "admin")  # counter restarted at 1

    def test_lockout_expires(self):
        for _ in range(3):
            self.limiter.record_failure("local_login", "admin")
        with self.assertRaises(errors.RateLimited):
            self.limiter.check("local_login", "admin")
        self.clock.advance(901)
        self.limiter.check("local_login", "admin")  # must not raise

    def test_success_clears_the_counter(self):
        self.limiter.record_failure("local_login", "admin")
        self.limiter.record_failure("local_login", "admin")
        self.limiter.record_success("local_login", "admin")
        self.limiter.record_failure("local_login", "admin")
        self.limiter.check("local_login", "admin")  # only one failure since reset

    def test_lockout_survives_a_new_process(self):
        """The counter lives in the database, not in memory."""
        for _ in range(3):
            self.limiter.record_failure("local_login", "admin")
        fresh_db = store.Database(self.path, clock=self.clock)
        fresh_db.initialize()
        fresh = ratelimit.RateLimiter(
            fresh_db,
            max_failures=3,
            window_seconds=300,
            lockout_seconds=900,
            clock=self.clock,
        )
        with self.assertRaises(errors.RateLimited):
            fresh.check("local_login", "admin")


if __name__ == "__main__":
    unittest.main()
