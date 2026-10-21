"""Who the request is, and how it is *not* allowed to be decided.

The property under test throughout: identity comes from a server-side session
record plus the account directory, and from nowhere else. Every way a client
could try to name itself is refused rather than ignored.
"""

from __future__ import annotations

import tempfile
import unittest

from backend.auth import store as auth_store
from backend.auth_linux.handoff import linux_user_id
from backend.bff import identity as bff_identity
from tests.bff import support as S


def harness(**kwargs) -> S.Harness:
    return S.build_harness(state_dir=tempfile.mkdtemp(), **kwargs)


class SessionResolutionTests(unittest.TestCase):
    def setUp(self):
        self.h = harness()

    def test_no_session_is_401(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = S.fetch_ops(client)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_a_forged_cookie_is_401_not_someone_elses_data(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        client.cookies[self.h.app.cfg.cookie_name] = "not-a-real-session"
        response = S.fetch_ops(client)
        self.assertEqual(response.status, 401)

    def test_a_real_session_resolves_to_the_linux_principals(self):
        client = self.h.client("alice")
        principal = self.h.app.resolve_session(self.h.session_id("alice"))
        self.assertEqual(principal.principal, linux_user_id(S.SYNTHETIC_HOST_ID, 4001))
        self.assertEqual(principal.uid, 4001)
        self.assertEqual(principal.linux_user, "alice")
        self.assertEqual(principal.user.role, auth_store.ROLE_MEMBER)
        self.assertEqual(S.fetch_ops(client).status, 200)

    def test_the_two_users_resolve_to_different_principals(self):
        a = self.h.app.resolve_session(self.h.session_id("alice"))
        b = self.h.app.resolve_session(self.h.session_id("bob"))
        self.assertNotEqual(a.principal, b.principal)
        self.assertNotEqual(a.uid, b.uid)

    def test_a_plain_session_is_a_member_even_for_a_granted_administrator(self):
        """The V6 rule: signing in decides *who you are*, not what you may see.

        `carol` is named by the root-owned Linux grant, so the user row really does
        say `platform_admin` — and that is exactly why this test asserts the user
        row is *not* what the session is judged on. A plain PAM sign-in is a
        member; the global view is opened only by entering admin mode, which is a
        separate, time-boxed act.
        """
        principal = self.h.app.resolve_session(self.h.session_id("carol"))
        self.assertEqual(principal.user.role, auth_store.ROLE_ADMIN)
        self.assertFalse(principal.is_platform_admin)
        self.assertFalse(principal.elevated)
        self.assertEqual(principal.effective_role, auth_store.ROLE_MEMBER)
        self.assertNotIn("portal:admin:read", principal.portal_scopes)

    def test_entering_admin_mode_is_what_promotes_the_session(self):
        """The same person, one session later, with the global view."""
        elevated = self.h.app.resolve_session(self.h.session_id_for("carol", admin=True))
        self.assertTrue(elevated.is_platform_admin)
        self.assertTrue(elevated.elevated)
        self.assertEqual(elevated.effective_role, auth_store.ROLE_ADMIN)
        self.assertIn("portal:admin:read", elevated.portal_scopes)

    def test_the_two_sessions_of_one_person_are_judged_separately(self):
        """Elevation is a property of a session, not of a person."""
        plain = self.h.app.resolve_session(self.h.session_id("carol"))
        elevated = self.h.app.resolve_session(self.h.session_id_for("carol", admin=True))
        self.assertEqual(plain.principal, elevated.principal)
        self.assertFalse(plain.elevated)
        self.assertTrue(elevated.elevated)

    def test_a_session_whose_uid_left_the_directory_is_refused(self):
        client = self.h.client("alice")
        self.h.accounts.remove("alice")
        response = S.fetch_ops(client)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_a_disabled_account_is_403_not_a_silent_downgrade(self):
        client = self.h.client("alice")
        self.h.auth_app.service.db.set_user_status(
            linux_user_id(S.SYNTHETIC_HOST_ID, 4001), auth_store.STATUS_DISABLED
        )
        response = S.fetch_ops(client)
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "account_disabled")


class NonLinuxSessionTests(unittest.TestCase):
    """Feishu maps to no principal; the bootstrap local admin gets the admin view only."""

    def setUp(self):
        self.h = harness()

    def _session_for(self, user_id: str) -> str:
        issued = self.h.auth_app.service.sessions.create(user_id)
        return issued.session_id

    def test_a_feishu_session_gets_no_registry_principal(self):
        user = self.h.auth_app.service.db.upsert_feishu_user(
            app_id="cli_synthetic",
            open_id="ou_synthetic",
            union_id=None,
            tenant_key="t_synthetic",
            display_name="Feishu Person",
        )
        with self.assertRaises(bff_identity.NotLinuxSession) as caught:
            self.h.app.resolve_session(self._session_for(user.id))
        self.assertEqual(caught.exception.reason, "linux_auth_not_enabled")
        self.assertEqual(caught.exception.status, 403)

    def test_the_local_admin_channel_gets_an_admin_view_but_no_registry_identity(self):
        # The bootstrap ``admin`` manages accounts; it reads the whole lab but is
        # no Linux user and not in the principal directory, so nothing it
        # registers could prove ownership.
        user = self.h.auth_app.service.db.ensure_local_user("demo-admin")
        principal = self.h.app.resolve_session(self._session_for(user.id))
        self.assertTrue(principal.elevated)
        self.assertEqual(principal.uid, -1)
        self.assertIsNone(self.h.app.service.principals.lookup(principal.principal))

    def test_a_disabled_local_admin_is_still_refused(self):
        user = self.h.auth_app.service.db.ensure_local_user("demo-admin")
        session = self._session_for(user.id)
        self.h.auth_app.service.db.set_user_status(user.id, "disabled")
        with self.assertRaises(bff_identity.IdentityError):
            self.h.app.resolve_session(session)


class AssertedIdentityTests(unittest.TestCase):
    """A client that names itself is refused, not humoured."""

    def setUp(self):
        self.h = harness()
        self.client = self.h.client("alice")

    def test_the_forbidden_header_list_is_the_registrys_own(self):
        from backend.registry.identity import FORBIDDEN_IDENTITY_HEADERS

        for header in ("x-uid", "x-admin", "x-role", "x-owner", "x-principal"):
            self.assertIn(header, FORBIDDEN_IDENTITY_HEADERS)

    def test_every_forbidden_header_is_rejected_with_400(self):
        from backend.registry.identity import FORBIDDEN_IDENTITY_HEADERS

        for header in sorted(FORBIDDEN_IDENTITY_HEADERS):
            response = self.client.run("GET", "/api/v2/ops", headers={header: "4000"})
            self.assertEqual(
                response.status,
                400,
                f"{header} was not refused",
            )
            self.assertEqual(
                response.error_code(), bff_identity.REASON_IDENTITY_ASSERTED
            )

    def test_a_forged_admin_header_does_not_widen_anything(self):
        thin = self.client.run("GET", "/api/v2/ops", headers={"X-Admin": "true"})
        self.assertEqual(thin.status, 400)
        # And the honest request still shows a member's view.
        ops = S.ops_of(S.fetch_ops(self.client))
        self.assertEqual(ops["admin"]["available"], False)
        self.assertEqual(ops["registry"]["all"], [])

    def test_header_rejection_is_case_insensitive(self):
        for header in ("X-UID", "x-Uid", "X-uId"):
            response = self.client.run("GET", "/api/v2/ops", headers={header: "0"})
            self.assertEqual(response.status, 400, header)

    def test_uid_parsing_only_accepts_a_plain_decimal(self):
        self.assertEqual(bff_identity.uid_from_external_id("host:4003"), 4003)
        for bad in ("4003", "host:", "host:1x04", "host:-1", "host:1_0", None, 4003):
            self.assertIsNone(bff_identity.uid_from_external_id(bad), repr(bad))


class NoAliasTests(unittest.TestCase):
    """`dana` is not `bob`, and nothing in this layer makes it so."""

    def test_an_unknown_login_name_cannot_sign_in(self):
        h = harness()
        client = S.WsgiClient(app=h.app, host="127.0.0.1:8790", scheme="http")
        response = S.login(client, S.UNBOUND_NAME, S.GOOD_PASSWORD)
        # `dana` is not an allowlisted account, so the login is refused and —
        # crucially — no session is handed out for `bob`.
        self.assertNotEqual(response.status, 200)
        self.assertNotIn(h.app.cfg.cookie_name, client.cookies)

    def test_a_successful_login_pins_the_uid_not_the_name(self):
        h = harness()
        h.client(S.REAL_ACCOUNT_NAME)
        principal = h.app.resolve_session(h.session_id(S.REAL_ACCOUNT_NAME))
        self.assertEqual(principal.uid, S.REAL_ACCOUNT_UID)
        self.assertEqual(principal.principal, linux_user_id(S.SYNTHETIC_HOST_ID, 4003))
        self.assertEqual(principal.linux_user, S.REAL_ACCOUNT_NAME)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
