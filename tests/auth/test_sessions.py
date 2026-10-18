"""Tests for backend.auth.sessions — issuing, validating, rotating and revoking."""

import pathlib
import shutil
import tempfile
import unittest

from backend.auth import errors, sessions, store
from tests.auth.test_store import FakeClock


class SessionTestCase(unittest.TestCase):
    IDLE = 600
    ABSOLUTE = 3600

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-sess-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.db = store.Database(self.tmp / "auth.sqlite3", clock=self.clock)
        self.db.initialize()
        self.manager = sessions.SessionManager(
            self.db,
            idle_seconds=self.IDLE,
            absolute_seconds=self.ABSOLUTE,
            clock=self.clock,
        )
        self.user = self.db.ensure_local_user("admin")

    def issue(self):
        return self.manager.create(self.user.id)


class IssuingTests(SessionTestCase):
    def test_session_identifier_carries_at_least_256_bits(self):
        issued = self.issue()
        self.assertGreaterEqual(len(issued.session_id), 43)

    def test_identifiers_are_unique_across_sessions(self):
        ids = {self.issue().session_id for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_csrf_token_is_not_the_session_id(self):
        issued = self.issue()
        self.assertNotEqual(issued.csrf_token, issued.session_id)

    def test_expiry_timestamps_are_returned(self):
        issued = self.issue()
        self.assertEqual(issued.absolute_expires_at, self.clock() + self.ABSOLUTE)


class ValidationTests(SessionTestCase):
    def test_fresh_session_authenticates(self):
        issued = self.issue()
        result = self.manager.authenticate(issued.session_id)
        self.assertEqual(result.user.id, self.user.id)

    def test_unknown_session_id_is_rejected(self):
        with self.assertRaises(errors.NotAuthenticated):
            self.manager.authenticate(store.new_secret_id())

    def test_empty_session_id_is_rejected(self):
        with self.assertRaises(errors.NotAuthenticated):
            self.manager.authenticate("")

    def test_activity_extends_the_idle_deadline(self):
        issued = self.issue()
        self.clock.advance(self.IDLE - 10)
        self.manager.authenticate(issued.session_id)
        self.clock.advance(self.IDLE - 10)
        self.manager.authenticate(issued.session_id)  # still alive

    def test_idle_session_expires(self):
        issued = self.issue()
        self.clock.advance(self.IDLE + 1)
        with self.assertRaises(errors.SessionExpired):
            self.manager.authenticate(issued.session_id)

    def test_absolute_lifetime_caps_idle_extension(self):
        """Keep using the session below the idle timeout; it must still die."""
        issued = self.issue()
        elapsed = 0
        while elapsed <= self.ABSOLUTE:
            self.clock.advance(self.IDLE - 10)
            elapsed += self.IDLE - 10
            try:
                self.manager.authenticate(issued.session_id)
            except errors.SessionExpired:
                return
        self.fail("absolute expiry was never enforced")

    def test_absolute_lifetime_expires_a_continuously_used_session(self):
        issued = self.issue()
        self.clock.advance(self.ABSOLUTE + 1)
        with self.assertRaises(errors.SessionExpired):
            self.manager.authenticate(issued.session_id)

    def test_disabled_user_cannot_use_a_live_session(self):
        issued = self.issue()
        self.db.set_user_status(self.user.id, store.STATUS_DISABLED)
        with self.assertRaises(errors.AccountDisabled):
            self.manager.authenticate(issued.session_id)

    def test_expired_session_is_revoked_in_storage(self):
        issued = self.issue()
        self.clock.advance(self.IDLE + 1)
        with self.assertRaises(errors.SessionExpired):
            self.manager.authenticate(issued.session_id)
        record = self.db.get_session(store.session_hash(issued.session_id))
        self.assertIsNotNone(record.revoked_at)


class CsrfTests(SessionTestCase):
    def test_matching_token_is_accepted(self):
        issued = self.issue()
        self.assertTrue(self.manager.verify_csrf(issued.session_id, issued.csrf_token))

    def test_wrong_token_is_refused(self):
        issued = self.issue()
        self.assertFalse(self.manager.verify_csrf(issued.session_id, store.new_secret_id()))

    def test_another_sessions_token_is_refused(self):
        first = self.issue()
        second = self.issue()
        self.assertFalse(self.manager.verify_csrf(first.session_id, second.csrf_token))

    def test_empty_token_is_refused(self):
        issued = self.issue()
        self.assertFalse(self.manager.verify_csrf(issued.session_id, ""))

    def test_token_is_bound_to_a_live_session(self):
        issued = self.issue()
        self.manager.revoke(issued.session_id)
        self.assertFalse(self.manager.verify_csrf(issued.session_id, issued.csrf_token))

    def test_comparison_is_constant_time(self):
        issued = self.issue()
        calls = []
        real = sessions.hmac.compare_digest

        def spy(left, right):
            calls.append((left, right))
            return real(left, right)

        sessions.hmac.compare_digest = spy
        try:
            self.manager.verify_csrf(issued.session_id, issued.csrf_token)
        finally:
            sessions.hmac.compare_digest = real
        self.assertTrue(calls, "CSRF verification did not use hmac.compare_digest")


class RevocationTests(SessionTestCase):
    def test_revoke_invalidates_the_session(self):
        issued = self.issue()
        self.manager.revoke(issued.session_id)
        with self.assertRaises(errors.SessionExpired):
            self.manager.authenticate(issued.session_id)

    def test_revoke_all_invalidates_every_session(self):
        ids = [self.issue().session_id for _ in range(3)]
        self.manager.revoke_all_for_user(self.user.id)
        for session_id in ids:
            with self.subTest(session_id=session_id[:8]):
                with self.assertRaises(errors.SessionExpired):
                    self.manager.authenticate(session_id)

    def test_revoke_is_idempotent(self):
        issued = self.issue()
        self.manager.revoke(issued.session_id)
        self.manager.revoke(issued.session_id)


class RotationTests(SessionTestCase):
    def test_rotation_issues_a_new_identifier(self):
        issued = self.issue()
        rotated = self.manager.rotate(issued.session_id)
        self.assertNotEqual(rotated.session_id, issued.session_id)

    def test_rotation_invalidates_the_previous_identifier(self):
        """Session fixation defence: the pre-login id must not survive sign-in."""
        issued = self.issue()
        rotated = self.manager.rotate(issued.session_id)
        with self.assertRaises(errors.SessionExpired):
            self.manager.authenticate(issued.session_id)
        self.assertEqual(self.manager.authenticate(rotated.session_id).user.id, self.user.id)

    def test_rotation_issues_a_new_csrf_token(self):
        issued = self.issue()
        rotated = self.manager.rotate(issued.session_id)
        self.assertNotEqual(rotated.csrf_token, issued.csrf_token)
        self.assertFalse(self.manager.verify_csrf(rotated.session_id, issued.csrf_token))
        self.assertTrue(self.manager.verify_csrf(rotated.session_id, rotated.csrf_token))

    def test_rotation_restarts_the_idle_clock(self):
        issued = self.issue()
        self.clock.advance(self.IDLE - 10)
        rotated = self.manager.rotate(issued.session_id)
        self.clock.advance(self.IDLE - 10)
        self.manager.authenticate(rotated.session_id)


class PurgeTests(SessionTestCase):
    def test_purge_removes_expired_sessions_only(self):
        stale = self.issue()
        live = self.issue()
        self.clock.advance(self.ABSOLUTE + 1)
        self.manager.purge()
        self.assertIsNone(self.db.get_session(store.session_hash(stale.session_id)))
        self.assertIsNone(self.db.get_session(store.session_hash(live.session_id)))


class CookieShapeTests(SessionTestCase):
    def test_session_cookie_is_httponly_secure_and_lax(self):
        header = self.manager.cookie_header(
            self.issue().session_id, secure=True, name="llm_lab_session"
        )
        self.assertIn("HttpOnly", header)
        self.assertIn("Secure", header)
        self.assertIn("SameSite=Lax", header)

    def test_clearing_cookie_expires_immediately(self):
        header = self.manager.clear_cookie_header(name="llm_lab_session", secure=True)
        self.assertIn("Max-Age=0", header)

    def test_cookie_never_carries_a_provider_credential(self):
        header = self.manager.cookie_header(
            self.issue().session_id, secure=True, name="llm_lab_session"
        )
        self.assertNotIn("Bearer", header)
        self.assertNotIn("access_token", header)


if __name__ == "__main__":
    unittest.main()
