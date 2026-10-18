"""The admin-mode routes, over the real WSGI application.

Same callable the browser talks to, real ``POST /auth/linux/login`` sessions,
real CSRF and same-origin preflight, real elevation store. The only synthetic
parts are the PAM backend (there is no real one in this build) and the shared
admin password, which is a fixture value.

What this file is here to prove, in one line: **the promotion comes from
server-side session state, and from nothing the caller sends.**
"""

from __future__ import annotations

import json
import tempfile
import unittest

from backend.auth import store as auth_store
from backend.bff.app import ADMIN_ENTER_PATH, ADMIN_EXIT_PATH, ADMIN_STATUS_PATH
from tests.auth.support import WsgiClient
from tests.auth_linux.support import FakeClock
from tests.bff import support as S

#: uid, name — the four Linux accounts this deployment has.
MEMBERS = (
    ("carol", 4000),
    ("alice", 4001),
    ("dave", 4002),
    ("bob", 4003),
)

HOST = "127.0.0.1:8790"


class RouteHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(now=5_000_000.0)
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), admin_clock=self.clock)
        self.addCleanup(self.h.close)
        self.client = self.h.client("alice")

    def enter(self, client=None, password=S.ADMIN_MODE_PASSWORD):
        return S.enter_admin_mode(client or self.client, password)

    def status(self, client=None):
        return S.admin_mode_of(S.admin_status(client or self.client))

    def fresh(self, name="alice") -> WsgiClient:
        """A brand-new session for ``name`` (a second sign-in)."""
        client = WsgiClient(app=self.h.app, host=HOST, scheme="http")
        signed_in = S.login(client, name, S.GOOD_PASSWORD)
        assert signed_in.status == 200, signed_in.body
        return client


class EveryMemberTests(RouteHarness):
    """All four Linux accounts, through the same path."""

    def test_each_account_signs_in_as_a_member_first(self) -> None:
        for name, uid in MEMBERS:
            with self.subTest(name=name):
                principal = self.h.app.resolve_session(self.h.session_id(name))
                self.assertFalse(principal.is_platform_admin)
                self.assertFalse(principal.elevated)
                self.assertEqual(principal.uid, uid)
                self.assertNotIn("portal:admin:read", principal.portal_scopes)

    def test_each_account_can_promote_a_session_with_the_shared_password(self) -> None:
        for name, _uid in MEMBERS:
            with self.subTest(name=name):
                client = self.fresh(name)
                self.assertFalse(self.status(client)["elevated"])
                response = self.enter(client)
                self.assertEqual(response.status, 200, response.body)
                state = S.admin_mode_of(response)
                self.assertTrue(state["elevated"])
                self.assertTrue(
                    self.h.app.resolve_session(client.cookies[self.h.app.cfg.cookie_name]).elevated
                )

    def test_each_account_is_refused_the_same_way_with_a_wrong_password(self) -> None:
        for name, _uid in MEMBERS:
            with self.subTest(name=name):
                client = self.fresh(name)
                response = self.enter(client, S.OTHER_ADMIN_MODE_PASSWORD)
                self.assertEqual(response.status, 401)
                self.assertEqual(response.error_code(), "invalid_credentials")
                self.assertFalse(self.status(client)["elevated"])

    def test_the_wrong_password_leaves_the_session_a_working_member(self) -> None:
        self.assertEqual(self.enter(self.client, S.OTHER_ADMIN_MODE_PASSWORD).status, 401)
        self.assertEqual(S.fetch_ops(self.client).status, 200)
        self.assertFalse(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])


class PromotionIsServerStateTests(RouteHarness):
    def test_the_promoted_session_sees_globally_and_a_sibling_does_not(self) -> None:
        """Same person, two sessions: one promoted, one not."""
        elevated = self.fresh("alice")
        self.assertEqual(self.enter(elevated).status, 200)

        self.assertTrue(S.ops_of(S.fetch_ops(elevated))["admin"]["available"])
        self.assertFalse(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])
        self.assertEqual(S.ops_of(S.fetch_ops(self.client))["registry"]["all"], [])

    def test_a_second_account_is_unaffected_by_anothers_promotion(self) -> None:
        self.assertEqual(self.enter(self.client).status, 200)
        other = self.fresh("bob")
        self.assertFalse(self.status(other)["elevated"])
        self.assertFalse(S.ops_of(S.fetch_ops(other))["admin"]["available"])

    def test_exiting_takes_the_global_view_away_immediately(self) -> None:
        self.assertEqual(self.enter().status, 200)
        self.assertTrue(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])

        exited = S.exit_admin_mode(self.client)
        self.assertEqual(exited.status, 200)
        self.assertFalse(S.admin_mode_of(exited)["elevated"])
        self.assertFalse(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])

    def test_the_promotion_lapses_with_the_clock(self) -> None:
        self.assertEqual(self.enter().status, 200)
        self.clock.advance(901)
        self.assertFalse(self.status()["elevated"])
        self.assertFalse(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])

    def test_the_ttl_cannot_exceed_fifteen_minutes(self) -> None:
        self.assertEqual(self.enter().status, 200)
        self.assertLessEqual(self.status()["ttl_seconds"], 900)


class CookiesAndCsrfTests(RouteHarness):
    def test_entering_without_a_csrf_token_is_refused(self) -> None:
        response = self.client.run("POST", ADMIN_ENTER_PATH, payload={"password": S.ADMIN_MODE_PASSWORD})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "csrf_rejected")
        self.assertFalse(self.status()["elevated"])

    def test_entering_from_another_origin_is_refused(self) -> None:
        response = self.client.run(
            "POST",
            ADMIN_ENTER_PATH,
            payload={"password": S.ADMIN_MODE_PASSWORD},
            headers={"X-CSRF-Token": S.csrf(self.client), "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "csrf_rejected")
        self.assertFalse(self.status()["elevated"])

    def test_exiting_without_a_csrf_token_is_refused(self) -> None:
        self.assertEqual(self.enter().status, 200)
        response = self.client.run("POST", ADMIN_EXIT_PATH, payload={})
        self.assertEqual(response.status, 403)
        # Still elevated: the write never happened.
        self.assertTrue(self.status()["elevated"])

    def test_the_session_cookie_stays_out_of_reach_of_script(self) -> None:
        """`HttpOnly` on the session cookie is what keeps elevation unstealable.

        If page script could read the cookie it could mint the elevation itself,
        and the promotion being server-side would not matter.
        """
        client = WsgiClient(app=self.h.app, host=HOST, scheme="http")
        issued = S.login(client, "alice", S.GOOD_PASSWORD)
        raw = issued.set_cookie(self.h.app.cfg.cookie_name)
        self.assertIsNotNone(raw)
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Lax", raw)

    def test_promoting_a_session_does_not_reissue_the_cookie(self) -> None:
        """The promotion is state, not a new credential in the browser."""
        response = self.enter()
        self.assertEqual(response.set_cookies, [])

    def test_the_status_route_is_a_get_and_needs_no_csrf(self) -> None:
        self.assertEqual(S.admin_status(self.client).status, 200)

    def test_wrong_methods_are_405(self) -> None:
        self.assertEqual(self.client.run("GET", ADMIN_ENTER_PATH).status, 405)
        self.assertEqual(self.client.run("POST", ADMIN_STATUS_PATH).status, 405)
        self.assertEqual(self.client.run("GET", ADMIN_EXIT_PATH).status, 405)


class AssertedIdentityTests(RouteHarness):
    def test_forging_an_admin_header_does_not_promote(self) -> None:
        response = self.client.run(
            "GET", "/api/v2/ops", headers={"X-Admin": "true"}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_forging_a_role_header_on_the_enter_route_is_refused(self) -> None:
        response = self.client.run(
            "POST",
            ADMIN_ENTER_PATH,
            payload={"password": S.ADMIN_MODE_PASSWORD},
            headers={"X-CSRF-Token": S.csrf(self.client), "X-Role": "platform_admin"},
        )
        self.assertEqual(response.status, 400)
        self.assertFalse(self.status()["elevated"])

    def test_a_body_claiming_elevation_is_refused_not_ignored(self) -> None:
        """A payload may not name a role; it is refused whole, before the password.

        **Superseded policy.** This used to assert ``401`` under the rule "the
        payload may carry anything; only the password is read". IAM #100 /
        BFF #79 identified that as the gap: "read but not used" is one
        refactor away from used. A request that names an identity is now
        ``400 identity_assertion_rejected``.

        **Why the field guard runs before the credential check:** doing it
        afterwards would make ``400 vs 401`` a password oracle — an attacker
        could send a forged field with a guessed password and learn from the
        status code alone whether the guess was right. Rejecting the malformed
        request first leaks nothing.

        The elevation assertion still holds: nothing was promoted.
        """
        response = self.client.run(
            "POST",
            ADMIN_ENTER_PATH,
            payload={"password": S.OTHER_ADMIN_MODE_PASSWORD, "elevated": True, "role": "platform_admin"},
            headers={"X-CSRF-Token": S.csrf(self.client)},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")
        self.assertFalse(self.status()["elevated"])


class NoSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.client = WsgiClient(app=self.h.app, host=HOST, scheme="http")

    def test_entering_without_a_session_is_401(self) -> None:
        response = S.enter_admin_mode(self.client)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_status_without_a_session_says_only_whether_it_is_configured(self) -> None:
        state = S.admin_mode_of(S.admin_status(self.client))
        self.assertTrue(state["configured"])
        self.assertFalse(state["elevated"])
        self.assertIsNone(state["expires_at"])


class OtherIdentitySourcesTests(unittest.TestCase):
    """The other two sign-in channels must not reach this door at all."""

    def setUp(self) -> None:
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def _client_for(self, user_id: str) -> WsgiClient:
        client = WsgiClient(app=self.h.app, host=HOST, scheme="http")
        issued = self.h.auth_app.service.sessions.create(user_id)
        client.cookies[self.h.app.cfg.cookie_name] = issued.session_id
        return client

    def test_a_feishu_session_cannot_enter_admin_mode(self) -> None:
        user = self.h.auth_app.service.db.upsert_feishu_user(
            app_id="cli_synthetic", open_id="ou_synthetic", union_id=None,
            tenant_key="t_synthetic", display_name="Feishu Person",
        )
        client = self._client_for(user.id)
        response = S.enter_admin_mode(client)
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "linux_auth_not_enabled")
        self.assertFalse(S.admin_mode_of(S.admin_status(client))["elevated"])

    def test_the_local_demo_administrator_cannot_enter_admin_mode(self) -> None:
        """The demo local-admin identity is not a way into the platform admin view."""
        user = self.h.auth_app.service.db.ensure_local_user("demo-admin")
        self.assertEqual(user.role, auth_store.ROLE_ADMIN)
        client = self._client_for(user.id)
        response = S.enter_admin_mode(client)
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "linux_auth_not_enabled")
        self.assertFalse(S.admin_mode_of(S.admin_status(client))["elevated"])

    def test_a_feishu_session_still_gets_no_ops_principal(self) -> None:
        user = self.h.auth_app.service.db.upsert_feishu_user(
            app_id="cli_synthetic", open_id="ou_synthetic", union_id=None,
            tenant_key="t_synthetic", display_name="Feishu Person",
        )
        self.assertEqual(S.fetch_ops(self._client_for(user.id)).status, 403)


class LogoutTests(RouteHarness):
    def test_logging_out_takes_the_promotion_with_it(self) -> None:
        self.assertEqual(self.enter().status, 200)
        cookie = self.client.cookies[self.h.app.cfg.cookie_name]

        response = self.client.run(
            "POST", "/auth/logout", payload={}, headers={"X-CSRF-Token": S.csrf(self.client)}
        )
        self.assertEqual(response.status, 200)
        self.assertFalse(self.h.admin_mode.is_elevated(cookie))
        self.assertEqual(self.h.admin_mode.store.count_elevations(), 0)

    def test_replaying_the_old_cookie_does_not_restore_the_promotion(self) -> None:
        self.assertEqual(self.enter().status, 200)
        cookie = self.client.cookies[self.h.app.cfg.cookie_name]
        self.client.run(
            "POST", "/auth/logout", payload={}, headers={"X-CSRF-Token": S.csrf(self.client)}
        )

        replay = WsgiClient(app=self.h.app, host=HOST, scheme="http")
        replay.cookies[self.h.app.cfg.cookie_name] = cookie
        self.assertEqual(S.fetch_ops(replay).status, 401)
        self.assertFalse(self.h.admin_mode.is_elevated(cookie))


class RotationTests(RouteHarness):
    def test_rotating_the_password_ends_the_promotion(self) -> None:
        self.assertEqual(self.enter().status, 200)
        self.assertTrue(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])

        S.write_admin_credential(self.h.state_dir, S.OTHER_ADMIN_MODE_PASSWORD)

        self.assertFalse(S.admin_mode_of(S.admin_status(self.client))["elevated"])
        self.assertFalse(S.ops_of(S.fetch_ops(self.client))["admin"]["available"])
        # The session itself is untouched and still usable as a member.
        self.assertEqual(S.fetch_ops(self.client).status, 200)


class ThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(now=5_000_000.0)
        self.h = S.build_harness(
            state_dir=tempfile.mkdtemp(), admin_clock=self.clock
        )
        self.addCleanup(self.h.close)
        self.client = self.h.client("alice")

    def test_repeated_wrong_passwords_are_rate_limited(self) -> None:
        for _ in range(5):
            self.assertEqual(
                S.enter_admin_mode(self.client, S.OTHER_ADMIN_MODE_PASSWORD).status, 401
            )
        response = S.enter_admin_mode(self.client, S.OTHER_ADMIN_MODE_PASSWORD)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.error_code(), "rate_limited")

    def test_the_lockout_also_holds_off_the_correct_password(self) -> None:
        for _ in range(5):
            S.enter_admin_mode(self.client, S.OTHER_ADMIN_MODE_PASSWORD)
        self.assertEqual(S.enter_admin_mode(self.client).status, 429)

    def test_the_lockout_lapses(self) -> None:
        for _ in range(5):
            S.enter_admin_mode(self.client, S.OTHER_ADMIN_MODE_PASSWORD)
        self.clock.advance(901)
        self.assertEqual(S.enter_admin_mode(self.client).status, 200)


class NotConfiguredTests(unittest.TestCase):
    """The default deployment: the control exists and cannot be used."""

    def setUp(self) -> None:
        self.h = S.build_harness(
            state_dir=tempfile.mkdtemp(), admin_mode_configured=False
        )
        self.addCleanup(self.h.close)
        self.client = self.h.client("alice")

    def test_status_reports_that_it_is_not_configured(self) -> None:
        state = S.admin_mode_of(S.admin_status(self.client))
        self.assertFalse(state["configured"])
        self.assertFalse(state["elevated"])

    def test_every_attempt_is_refused_with_a_stated_reason(self) -> None:
        for password in (S.ADMIN_MODE_PASSWORD, "", "anything-at-all"):
            with self.subTest(password=password[:8]):
                response = S.enter_admin_mode(self.client, password)
                self.assertEqual(response.status, 503)
                self.assertEqual(response.error_code(), "admin_mode_not_configured")

    def test_the_session_still_works_as_a_member(self) -> None:
        self.assertEqual(S.fetch_ops(self.client).status, 200)


class DisclosureTests(RouteHarness):
    def test_no_response_ever_carries_the_password(self) -> None:
        attempts = [
            self.enter(self.client, S.ADMIN_MODE_PASSWORD),
            self.enter(self.client, S.OTHER_ADMIN_MODE_PASSWORD),
            S.admin_status(self.client),
            S.exit_admin_mode(self.client),
        ]
        for response in attempts:
            blob = (response.body or b"").decode("utf-8", "replace")
            with self.subTest(status=response.status):
                self.assertNotIn(S.ADMIN_MODE_PASSWORD, blob)
                self.assertNotIn(S.OTHER_ADMIN_MODE_PASSWORD, blob)
                self.assertNotIn("scrypt", blob)

    def test_the_status_payload_has_no_hash_or_path_in_it(self) -> None:
        blob = json.dumps(self.status())
        for forbidden in ("$", "scrypt", "/", self.h.state_dir):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_the_status_payload_is_exactly_the_agreed_shape(self) -> None:
        self.assertEqual(
            set(self.status()),
            {"configured", "elevated", "ttl_seconds", "expires_at"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
