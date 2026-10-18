"""The elevation store: rows, expiry, counters and audit."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from backend.bff import admin_mode as am
from tests.auth_linux.support import FakeClock

from . import support


def elevation(**overrides) -> am.Elevation:
    base = {
        "session_hash": am.admin_session_hash("session-a"),
        "principal": "linux:dell-test-01:4001",
        "uid": 4001,
        "linux_user": "alice",
        "entered_at": 1_000_000.0,
        "expires_at": 1_000_900.0,
        "credential_version": "version-1",
    }
    base.update(overrides)
    return am.Elevation(**base)


class ElevationRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = am.AdminModeStore(
            Path(self._tmp.name) / am.DEFAULT_STORE_FILE_NAME, clock=self.clock
        )
        self.store.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_written_elevation_reads_back_intact(self) -> None:
        self.store.insert_elevation(elevation())
        found = self.store.get_elevation(am.admin_session_hash("session-a"))
        self.assertIsNotNone(found)
        self.assertEqual(found.uid, 4001)
        self.assertEqual(found.linux_user, "alice")
        self.assertEqual(found.credential_version, "version-1")

    def test_an_unknown_session_has_no_elevation(self) -> None:
        self.assertIsNone(self.store.get_elevation(am.admin_session_hash("nope")))

    def test_re_elevating_the_same_session_replaces_the_row(self) -> None:
        self.store.insert_elevation(elevation())
        self.store.insert_elevation(elevation(expires_at=1_000_500.0))
        self.assertEqual(self.store.count_elevations(), 1)
        self.assertEqual(
            self.store.get_elevation(am.admin_session_hash("session-a")).expires_at,
            1_000_500.0,
        )

    def test_delete_removes_only_that_session(self) -> None:
        self.store.insert_elevation(elevation())
        self.store.insert_elevation(elevation(session_hash=am.admin_session_hash("session-b")))
        self.store.delete_elevation(am.admin_session_hash("session-a"))
        self.assertIsNone(self.store.get_elevation(am.admin_session_hash("session-a")))
        self.assertIsNotNone(self.store.get_elevation(am.admin_session_hash("session-b")))

    def test_delete_all_clears_every_session(self) -> None:
        self.store.insert_elevation(elevation())
        self.store.insert_elevation(elevation(session_hash=am.admin_session_hash("session-b")))
        self.assertEqual(self.store.delete_all_elevations(), 2)
        self.assertEqual(self.store.count_elevations(), 0)

    def test_purge_removes_only_expired_rows(self) -> None:
        self.store.insert_elevation(elevation(expires_at=1_000_100.0))
        self.store.insert_elevation(
            elevation(session_hash=am.admin_session_hash("session-b"), expires_at=9_999_999.0)
        )
        self.assertEqual(self.store.purge_expired(1_000_200.0), 1)
        self.assertEqual(self.store.count_elevations(), 1)

    def test_the_session_cookie_is_never_stored(self) -> None:
        raw = "the-actual-cookie-value"
        self.store.insert_elevation(elevation(session_hash=am.admin_session_hash(raw)))
        blob = Path(self.store.path).read_bytes()
        self.assertNotIn(raw.encode(), blob)


class InMemoryStoreTests(unittest.TestCase):
    def test_an_in_memory_store_actually_keeps_what_it_is_given(self) -> None:
        """``sqlite3.connect(":memory:")`` returns a *new* database every call.

        Building the class on a connection-per-call and then offering an
        in-memory path would mean every elevation wrote into a database that was
        thrown away — silently, and in the one table where losing a row means
        "not elevated", which looks exactly like "never elevated".
        """
        store = am.AdminModeStore(":memory:", clock=FakeClock())
        store.initialize()
        try:
            store.insert_elevation(elevation())
            self.assertEqual(store.count_elevations(), 1)
            self.assertIsNotNone(store.get_elevation(am.admin_session_hash("session-a")))
            store.write_attempt("s", "k", 1.0, 2, 3.0)
            self.assertEqual(store.read_attempt("s", "k"), (1.0, 2, 3.0))
        finally:
            store.close()


class AttemptCounterTests(unittest.TestCase):
    """The three methods ``backend.auth.ratelimit.RateLimiter`` needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = am.AdminModeStore(
            Path(self._tmp.name) / am.DEFAULT_STORE_FILE_NAME, clock=FakeClock()
        )
        self.store.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_write_clear_round_trip(self) -> None:
        self.assertIsNone(self.store.read_attempt("scope", "key"))
        self.store.write_attempt("scope", "key", 10.0, 1, 0.0)
        self.assertEqual(self.store.read_attempt("scope", "key"), (10.0, 1, 0.0))
        self.store.write_attempt("scope", "key", 10.0, 2, 30.0)
        self.assertEqual(self.store.read_attempt("scope", "key"), (10.0, 2, 30.0))
        self.store.clear_attempt("scope", "key")
        self.assertIsNone(self.store.read_attempt("scope", "key"))

    def test_scopes_and_keys_do_not_collide(self) -> None:
        self.store.write_attempt("a", "k", 1.0, 1, 0.0)
        self.assertIsNone(self.store.read_attempt("b", "k"))
        self.assertIsNone(self.store.read_attempt("a", "j"))

    def test_the_limiter_accepts_this_store_unchanged(self) -> None:
        from backend.auth.ratelimit import RateLimiter

        clock = FakeClock()
        limiter = RateLimiter(
            self.store, max_failures=2, window_seconds=60, lockout_seconds=300, clock=clock
        )
        limiter.record_failure("s", "k")
        limiter.record_failure("s", "k")
        from backend.auth import errors as auth_errors

        with self.assertRaises(auth_errors.RateLimited):
            limiter.check("s", "k")


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = am.AdminModeStore(
            Path(self._tmp.name) / am.DEFAULT_STORE_FILE_NAME, clock=FakeClock()
        )
        self.store.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_an_audit_row_names_the_linux_identity_and_the_mode(self) -> None:
        """The actor is the original Linux account, plus a flag.

        Admin mode is not a second principal, so the record must not invent one:
        it carries the same uid and login the session already had, and a boolean
        saying whether the action happened while elevated.
        """
        self.store.record_audit(
            event="admin_mode_enter",
            outcome="success",
            actor="alice",
            principal="linux:dell-test-01:4001",
            uid=4001,
            linux_user="alice",
            admin_mode=True,
        )
        (row,) = self.store.list_audit()
        self.assertEqual(row["actor"], "alice")
        self.assertEqual(row["uid"], 4001)
        self.assertEqual(row["linux_user"], "alice")
        self.assertIs(row["admin_mode"], True)
        self.assertEqual(row["principal"], "linux:dell-test-01:4001")

    def test_the_audit_table_has_no_column_for_a_credential(self) -> None:
        import sqlite3

        conn = sqlite3.connect(str(self.store.path))
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(admin_audit)").fetchall()
            }
        finally:
            conn.close()
        for forbidden in ("password", "hash", "secret", "token", "credential"):
            with self.subTest(column=forbidden):
                self.assertNotIn(forbidden, columns)

    def test_the_newest_row_comes_first(self) -> None:
        self.store.record_audit(event="first", outcome="success")
        self.store.record_audit(event="second", outcome="success")
        self.assertEqual([row["event"] for row in self.store.list_audit()], ["second", "first"])


class StoreHardeningTests(unittest.TestCase):
    def test_the_database_file_is_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / am.DEFAULT_STORE_FILE_NAME
            store = am.AdminModeStore(path, clock=FakeClock())
            store.initialize()
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_the_state_directory_is_tightened_to_0700(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "state"
            nested.mkdir(mode=0o755)
            store = am.AdminModeStore(nested / am.DEFAULT_STORE_FILE_NAME, clock=FakeClock())
            store.initialize()
            self.assertEqual(stat.S_IMODE(os.stat(nested).st_mode), 0o700)


class ElevationLivenessTests(unittest.TestCase):
    def test_a_row_is_live_only_before_its_expiry(self) -> None:
        row = am.Elevation(
            session_hash="h", principal="p", uid=1, linux_user="u",
            entered_at=0.0, expires_at=100.0, credential_version="v",
        )
        self.assertTrue(row.is_live(99.0, "v"))
        self.assertFalse(row.is_live(100.0, "v"))
        self.assertFalse(row.is_live(101.0, "v"))

    def test_a_row_dies_with_the_credential_that_issued_it(self) -> None:
        row = am.Elevation(
            session_hash="h", principal="p", uid=1, linux_user="u",
            entered_at=0.0, expires_at=100.0, credential_version="v1",
        )
        self.assertTrue(row.is_live(50.0, "v1"))
        self.assertFalse(row.is_live(50.0, "v2"))
        self.assertFalse(row.is_live(50.0, "none"))


class SessionHashTests(unittest.TestCase):
    def test_the_hash_is_deterministic_and_not_the_cookie(self) -> None:
        raw = "some-cookie-value"
        digest = am.admin_session_hash(raw)
        self.assertEqual(digest, am.admin_session_hash(raw))
        self.assertNotIn(raw, digest)
        self.assertEqual(len(digest), 64)

    def test_different_cookies_hash_differently(self) -> None:
        self.assertNotEqual(am.admin_session_hash("a"), am.admin_session_hash("b"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
