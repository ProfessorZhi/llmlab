"""Entering, leaving and losing admin mode.

The properties here are the ones a reviewer should be able to check by reading
the test names: a shared password promotes exactly one session, for a bounded
time, and every one of logout, expiry, rotation and disable takes it back.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.bff import admin_mode as am
from tests.auth_linux.support import FakeClock

from . import support

PRINCIPAL = "linux:dell-test-01:4001"
UID = 4001
LINUX_USER = "alice"

SESSION_A = "session-aaa"
SESSION_B = "session-bbb"


class ServiceHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.service = support.make_service(self._tmp.name, clock=self.clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def enter(self, session=SESSION_A, password=support.SHARED_PASSWORD, **overrides):
        payload = {
            "session_id": session,
            "principal": PRINCIPAL,
            "uid": UID,
            "linux_user": LINUX_USER,
            "password": password,
            "client_key": "203.0.113.9",
        }
        payload.update(overrides)
        return self.service.enter(**payload)

    def is_elevated(self, session=SESSION_A) -> bool:
        return self.service.is_elevated(session, principal=PRINCIPAL, uid=UID)


class EnterTests(ServiceHarness):
    def test_the_right_password_promotes_the_session(self) -> None:
        state = self.enter()
        self.assertTrue(state.elevated)
        self.assertTrue(self.is_elevated())

    def test_the_elevation_is_time_boxed(self) -> None:
        state = self.enter()
        self.assertEqual(self.service.ttl_seconds, am.DEFAULT_TTL_SECONDS)
        self.assertEqual(state.expires_at, self.clock.now + am.DEFAULT_TTL_SECONDS)

    def test_a_wrong_password_does_not_promote(self) -> None:
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.OTHER_SHARED_PASSWORD)
        self.assertFalse(self.is_elevated())

    def test_an_empty_password_does_not_promote(self) -> None:
        for password in ("", None, 42, b"x"):
            with self.subTest(password=password):
                with self.assertRaises(am.InvalidCredentials):
                    self.enter(password=password)
        self.assertFalse(self.is_elevated())

    def test_the_password_is_never_echoed_back(self) -> None:
        state = self.enter()
        rendered = repr(state) + str(state.to_dict())
        self.assertNotIn(support.SHARED_PASSWORD, rendered)

    def test_re_entering_extends_from_now(self) -> None:
        first = self.enter()
        self.clock.advance(600)
        second = self.enter()
        self.assertEqual(second.expires_at, first.expires_at + 600)


class NotConfiguredTests(unittest.TestCase):
    """The state the deployment ships in: nobody can be promoted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_without_a_credential_nobody_can_enter(self) -> None:
        service = support.make_service(self._tmp.name, clock=self.clock, configured=False)
        self.assertFalse(service.configured())
        with self.assertRaises(am.NotConfigured):
            service.enter(
                session_id=SESSION_A, principal=PRINCIPAL, uid=UID, linux_user=LINUX_USER,
                password=support.SHARED_PASSWORD, client_key="k",
            )
        self.assertFalse(service.is_elevated(SESSION_A))

    def test_a_disabled_credential_is_the_same_refusal(self) -> None:
        support.write_credential(self._tmp.name)
        path = Path(self._tmp.name) / am.DEFAULT_CREDENTIAL_FILE_NAME
        service = support.make_service(self._tmp.name, clock=self.clock, configured=False)
        path.write_text(am.DISABLED_SENTINEL + "\n", encoding="utf-8")
        self.assertFalse(service.configured())
        with self.assertRaises(am.NotConfigured):
            service.enter(
                session_id=SESSION_A, principal=PRINCIPAL, uid=UID, linux_user=LINUX_USER,
                password=support.SHARED_PASSWORD, client_key="k",
            )

    def test_a_switched_off_feature_cannot_promote_even_with_the_right_password(self) -> None:
        service = support.make_service(self._tmp.name, clock=self.clock, enabled=False)
        self.assertFalse(service.configured())
        with self.assertRaises(am.NotConfigured):
            service.enter(
                session_id=SESSION_A, principal=PRINCIPAL, uid=UID, linux_user=LINUX_USER,
                password=support.SHARED_PASSWORD, client_key="k",
            )

    def test_an_unconfigured_status_is_still_answered(self) -> None:
        service = support.make_service(self._tmp.name, clock=self.clock, configured=False)
        state = service.status_for(SESSION_A)
        self.assertFalse(state.configured)
        self.assertFalse(state.elevated)


class SessionBindingTests(ServiceHarness):
    def test_a_second_session_of_the_same_person_is_not_promoted(self) -> None:
        """Two sessions, one person: elevation belongs to one of them.

        Nothing about the *user* changed, and that is the point — this is the
        isolation the model promises, and it holds even for the same account.
        """
        self.enter(session=SESSION_A)
        self.assertTrue(self.service.is_elevated(SESSION_A))
        self.assertFalse(self.service.is_elevated(SESSION_B))
        self.assertFalse(
            self.service.status_for(SESSION_B).elevated,
            "a sibling session inherited an elevation",
        )

    def test_an_elevation_named_for_another_principal_is_not_mine(self) -> None:
        self.enter()
        self.assertFalse(self.service.is_elevated(SESSION_A, principal="linux:other:1999"))
        self.assertFalse(self.service.is_elevated(SESSION_A, uid=1999))

    def test_an_arbitrary_cookie_value_is_not_elevated(self) -> None:
        self.enter()
        self.assertFalse(self.service.is_elevated("a-made-up-cookie"))
        self.assertFalse(self.service.is_elevated(""))
        self.assertFalse(self.service.is_elevated(None))

    def test_status_of_an_unknown_session_is_configured_but_not_elevated(self) -> None:
        state = self.service.status_for("not-a-session")
        self.assertTrue(state.configured)
        self.assertFalse(state.elevated)
        self.assertIsNone(state.expires_at)


class ExpiryTests(ServiceHarness):
    def test_the_promotion_lapses_on_its_own(self) -> None:
        self.enter()
        self.clock.advance(am.DEFAULT_TTL_SECONDS)
        self.assertFalse(self.is_elevated())
        self.assertFalse(self.service.status_for(SESSION_A).elevated)

    def test_it_is_still_live_one_second_before_it_lapses(self) -> None:
        self.enter()
        self.clock.advance(am.DEFAULT_TTL_SECONDS - 1)
        self.assertTrue(self.is_elevated())

    def test_a_lapsed_row_is_cleared_rather_than_left_behind(self) -> None:
        self.enter()
        self.clock.advance(am.DEFAULT_TTL_SECONDS + 1)
        self.assertFalse(self.is_elevated())
        self.assertEqual(self.service.store.count_elevations(), 0)

    def test_the_hard_cap_cannot_be_configured_away(self) -> None:
        service = support.make_service(self._tmp.name, clock=self.clock, ttl_seconds=100_000)
        self.assertEqual(service.ttl_seconds, am.MAX_TTL_SECONDS)
        self.assertLessEqual(service.ttl_seconds, 900)

    def test_a_too_short_ttl_is_raised_to_the_floor(self) -> None:
        service = support.make_service(self._tmp.name, clock=self.clock, ttl_seconds=1)
        self.assertEqual(service.ttl_seconds, am.MIN_TTL_SECONDS)


class ExitTests(ServiceHarness):
    def test_exit_takes_the_promotion_away(self) -> None:
        self.enter()
        state = self.service.exit(session_id=SESSION_A, principal=PRINCIPAL, uid=UID, linux_user=LINUX_USER)
        self.assertFalse(state.elevated)
        self.assertFalse(self.is_elevated())

    def test_exit_is_idempotent(self) -> None:
        self.service.exit(session_id=SESSION_A)
        self.service.exit(session_id=SESSION_A)
        self.assertFalse(self.is_elevated())

    def test_exit_does_not_touch_a_sibling_session(self) -> None:
        self.enter(session=SESSION_A)
        self.enter(session=SESSION_B)
        self.service.exit(session_id=SESSION_A)
        self.assertFalse(self.service.is_elevated(SESSION_A))
        self.assertTrue(self.service.is_elevated(SESSION_B))


class LogoutTests(ServiceHarness):
    def test_forgetting_a_session_takes_the_promotion_with_it(self) -> None:
        self.enter()
        self.service.forget_session(SESSION_A)
        self.assertFalse(self.is_elevated())
        self.assertEqual(self.service.store.count_elevations(), 0)

    def test_forgetting_an_unknown_session_is_harmless(self) -> None:
        self.enter()
        self.service.forget_session("never-seen")
        self.assertTrue(self.is_elevated())


class RotationTests(ServiceHarness):
    def test_rotating_the_password_invalidates_every_promotion(self) -> None:
        """Otherwise the old password's sessions outlive the old password."""
        self.enter()
        self.assertTrue(self.is_elevated())
        support.write_credential(self._tmp.name, support.OTHER_SHARED_PASSWORD)
        self.assertFalse(self.is_elevated())
        self.assertFalse(self.service.status_for(SESSION_A).elevated)

    def test_the_new_password_is_the_one_that_works_afterwards(self) -> None:
        support.write_credential(self._tmp.name, support.OTHER_SHARED_PASSWORD)
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.SHARED_PASSWORD)
        self.assertTrue(self.enter(password=support.OTHER_SHARED_PASSWORD).elevated)

    def test_rotate_credentials_clears_the_rows_too(self) -> None:
        self.enter()
        self.enter(session=SESSION_B)
        self.assertEqual(self.service.rotate_credentials(), 2)
        self.assertEqual(self.service.store.count_elevations(), 0)

    def test_disabling_the_credential_invalidates_promotions(self) -> None:
        self.enter()
        path = Path(self._tmp.name) / am.DEFAULT_CREDENTIAL_FILE_NAME
        path.write_text(am.DISABLED_SENTINEL + "\n", encoding="utf-8")
        self.assertFalse(self.is_elevated())


class ThrottleTests(ServiceHarness):
    def setUp(self) -> None:
        super().setUp()
        self.service = support.make_service(
            self._tmp.name, clock=self.clock, max_failures=3, window_seconds=60, lockout_seconds=300
        )

    def test_repeated_wrong_passwords_are_throttled(self) -> None:
        for _ in range(3):
            with self.assertRaises(am.InvalidCredentials):
                self.enter(password=support.OTHER_SHARED_PASSWORD)
        with self.assertRaises(am.RateLimited) as caught:
            self.enter(password=support.OTHER_SHARED_PASSWORD)
        self.assertIsNotNone(caught.exception.retry_after)

    def test_the_right_password_is_also_held_off_while_locked(self) -> None:
        """A lockout that admits the correct password is not a lockout."""
        for _ in range(3):
            with self.assertRaises(am.InvalidCredentials):
                self.enter(password=support.OTHER_SHARED_PASSWORD)
        with self.assertRaises(am.RateLimited):
            self.enter()
        self.assertFalse(self.is_elevated())

    def test_a_success_clears_the_counter(self) -> None:
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.OTHER_SHARED_PASSWORD)
        self.enter()
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.OTHER_SHARED_PASSWORD)
        # Had the success not cleared it, this third failure would be a lockout.
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.OTHER_SHARED_PASSWORD)

    def test_the_lock_lapses_after_its_window(self) -> None:
        for _ in range(3):
            with self.assertRaises(am.InvalidCredentials):
                self.enter(password=support.OTHER_SHARED_PASSWORD)
        self.clock.advance(301)
        self.clock.advance(60)
        self.assertTrue(self.enter().elevated)

    def test_one_client_is_locked_out_without_locking_out_another(self) -> None:
        for _ in range(3):
            with self.assertRaises(am.InvalidCredentials):
                self.enter(password=support.OTHER_SHARED_PASSWORD, client_key="10.0.0.1")
        with self.assertRaises(am.RateLimited):
            self.enter(password=support.OTHER_SHARED_PASSWORD, client_key="10.0.0.1")
        self.assertTrue(self.enter(client_key="10.0.0.2").elevated)


class AuditTrailTests(ServiceHarness):
    def test_a_successful_promotion_is_recorded_with_the_linux_identity(self) -> None:
        self.enter()
        events = [row for row in self.service.store.list_audit() if row["event"] == "admin_mode_enter"]
        self.assertTrue(events)
        row = events[0]
        self.assertEqual(row["outcome"], "success")
        self.assertEqual(row["uid"], UID)
        self.assertEqual(row["linux_user"], LINUX_USER)
        self.assertEqual(row["actor"], LINUX_USER)
        self.assertIs(row["admin_mode"], True)

    def test_a_failed_attempt_is_recorded_as_not_elevated(self) -> None:
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.OTHER_SHARED_PASSWORD)
        row = self.service.store.list_audit()[0]
        self.assertEqual(row["event"], "admin_mode_enter")
        self.assertEqual(row["outcome"], "failure")
        self.assertIs(row["admin_mode"], False)

    def test_exit_is_recorded(self) -> None:
        self.enter()
        self.service.exit(session_id=SESSION_A, principal=PRINCIPAL, uid=UID, linux_user=LINUX_USER)
        events = {row["event"] for row in self.service.store.list_audit()}
        self.assertIn("admin_mode_exit", events)

    def test_no_audit_row_ever_contains_the_password(self) -> None:
        self.enter()
        with self.assertRaises(am.InvalidCredentials):
            self.enter(password=support.OTHER_SHARED_PASSWORD)
        blob = str(self.service.store.list_audit())
        self.assertNotIn(support.SHARED_PASSWORD, blob)
        self.assertNotIn(support.OTHER_SHARED_PASSWORD, blob)
        self.assertNotIn("$scrypt$", blob)


class PurgeTests(ServiceHarness):
    def test_purge_drops_lapsed_rows(self) -> None:
        self.enter()
        self.clock.advance(am.DEFAULT_TTL_SECONDS + 1)
        self.assertEqual(self.service.purge(), 1)
        self.assertEqual(self.service.store.count_elevations(), 0)


class ConfigTests(unittest.TestCase):
    def test_the_feature_is_off_unless_asked_for(self) -> None:
        config = am.from_env({}, state_dir="/tmp/does-not-matter")
        self.assertFalse(config.enabled)

    def test_a_typo_in_the_switch_is_loud(self) -> None:
        with self.assertRaises(am.Misconfigured):
            am.from_env({am.ENV_ENABLED: "tru"}, state_dir="/tmp/x")

    def test_the_ttl_cannot_exceed_the_cap(self) -> None:
        with self.assertRaises(am.Misconfigured):
            am.from_env({am.ENV_TTL_SECONDS: "86400"}, state_dir="/tmp/x")

    def test_the_paths_default_into_the_state_directory(self) -> None:
        config = am.from_env({}, state_dir="/var/lib/llm-lab")
        self.assertEqual(str(config.credential_path), "/var/lib/llm-lab/" + am.DEFAULT_CREDENTIAL_FILE_NAME)
        self.assertEqual(str(config.store_path), "/var/lib/llm-lab/" + am.DEFAULT_STORE_FILE_NAME)

    def test_settings_are_validated(self) -> None:
        for key, value in (
            (am.ENV_MAX_FAILURES, "0"),
            (am.ENV_WINDOW_SECONDS, "-1"),
            (am.ENV_LOCKOUT_SECONDS, "soon"),
            (am.ENV_TTL_SECONDS, "59"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(am.Misconfigured):
                    am.from_env({key: value}, state_dir="/tmp/x")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
