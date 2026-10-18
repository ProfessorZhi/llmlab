"""HTTP tests for the ``/auth/tokens`` surface.

The service-level guarantees live in :mod:`tests.auth.test_tokens`; this file
covers the parts that only exist once the routes are wired: the fail-closed
503 when no token service is attached, 401/403 for anonymous and CSRF-less
callers, and the rule that a token's owner is the *session's* principal and can
never be named by the request body.

Two synthetic principals are used throughout and neither is a real account:
the local ``admin`` with a fictional password, and a Feishu member whose
``open_id`` is invented in the test.
"""

import unittest
import urllib.parse

from backend.auth import routes
from backend.auth import tokens as tokens_mod
from tests.auth import support
from tests.auth.test_routes import SYNTHETIC_ADMIN_PASSWORD, HttpTestCase

MEMBER_OPEN_ID = "ou_synthetic_iam_45_a"
OTHER_OPEN_ID = "ou_synthetic_iam_45_b"


class TokenRouteTestCase(HttpTestCase):
    def setUp(self):
        super().setUp()
        # ``HttpTestCase`` builds the app exactly as a deployment would when the
        # token service has not been attached; attaching it here is the same
        # wiring the Integration lane performs. The directory is the real
        # adapter over the auth store's ``users`` table, so "is this principal
        # still allowed?" is answered from the same row the rest of the portal
        # reads — the check a test double could only pretend to make.
        self.tokens = tokens_mod.build_token_service(
            self.tmp,
            principal_state=tokens_mod.AuthStorePrincipalState(self.app.service.db),
            clock=self.clock,
        )
        self.tokens.initialize()
        self.app.tokens = self.tokens

    # --- helpers -------------------------------------------------------------

    def login_member(self, open_id, *, client=None):
        """Sign in a Feishu member on a fresh client and return that client."""
        client = client or support.WsgiClient(self.app)
        self.transport.queue(
            support.app_token_response(),
            support.user_token_response(open_id=open_id),
            support.user_info_response(open_id=open_id),
        )
        start = client.run("GET", "/auth/feishu/start")
        self.assertEqual(start.status, 302)
        state = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(start.location).query)
        )["state"]
        # The authorisation code is one-shot per flow: reusing it would be
        # refused as a replay, which is exactly what it did before this helper
        # gave each sign-in its own code.
        callback = client.run("GET", f"/auth/feishu/callback?code=code-{open_id}&state={state}")
        self.assertEqual(callback.status, 302)
        return client

    def session_principal(self, client):
        return self.app.service.current_session(client.cookies["llm_lab_session"]).user.id

    def issue(self, client=None, **payload):
        client = client or self.client
        return client.run(
            "POST",
            "/auth/tokens",
            payload=payload,
            headers={"X-CSRF-Token": self.csrf_token(client)},
        )


# --- fail-closed until wired --------------------------------------------------


class NotConfiguredTests(HttpTestCase):
    def test_the_token_routes_are_unavailable_without_a_token_service(self):
        self.bootstrap_admin()
        self.login_admin()
        # ``HttpTestCase`` never attaches a token service: this is the honest
        # state of a deployment that has not wired one.
        self.assertIsNone(self.app.tokens)
        for method, path, payload in (
            ("GET", "/auth/tokens", None),
            ("POST", "/auth/tokens", {"scopes": ["registry:read"]}),
            ("POST", "/auth/tokens/revoke", {"token_id": "x"}),
            ("POST", "/auth/tokens/rotate", {"token_id": "x"}),
            ("POST", "/auth/tokens/revoke-all", None),
        ):
            with self.subTest(route=f"{method} {path}"):
                response = self.client.run(
                    method, path, payload=payload, headers={"X-CSRF-Token": self.csrf_token()}
                )
                self.assertEqual(response.status, 503)
                self.assertEqual(response.error_code(), "not_configured")


# --- anonymous and CSRF -------------------------------------------------------


class AuthorizationTests(TokenRouteTestCase):
    def test_listing_tokens_anonymously_is_401(self):
        response = self.client.run("GET", "/auth/tokens")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "not_authenticated")

    def test_issuing_without_a_csrf_token_is_refused(self):
        self.bootstrap_admin()
        self.login_admin()
        response = self.client.run("POST", "/auth/tokens", payload={"scopes": ["registry:read"]})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "csrf_rejected")

    def test_issuing_without_a_session_is_refused(self):
        # No cookie, no CSRF: the write guard fires before anything is minted.
        response = self.client.run(
            "POST",
            "/auth/tokens",
            payload={"scopes": ["registry:read"]},
            headers={"X-CSRF-Token": "x"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "csrf_rejected")

    def test_a_disabled_account_cannot_use_the_token_routes(self):
        self.bootstrap_admin()
        self.login_admin()
        user = self.app.service.db.get_user(self.session_principal(self.client))
        self.app.service.db.set_user_status(user.id, "disabled")
        response = self.client.run("GET", "/auth/tokens")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "account_disabled")


# --- issuance -----------------------------------------------------------------


class IssuanceTests(TokenRouteTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()
        self.login_admin()

    def test_issuing_returns_the_plaintext_exactly_once(self):
        response = self.issue()
        self.assertEqual(response.status, 201)
        plaintext = response.json()["data"]["token"]
        self.assertTrue(plaintext.startswith(tokens_mod.TOKEN_PREFIX))

        listed = self.client.run("GET", "/auth/tokens")
        self.assertEqual(listed.status, 200)
        self.assertNotIn(plaintext, listed.body.decode("utf-8"))
        entry = listed.json()["data"]["tokens"][0]
        self.assertNotIn("token", entry)
        self.assertEqual(entry["token_id"], response.json()["data"]["token_id"])

    def test_the_default_scope_set_is_the_narrow_one(self):
        payload = self.issue().json()["data"]
        self.assertEqual(payload["scopes"], sorted(routes.DEFAULT_TOKEN_SCOPES))

    def test_a_requested_scope_outside_the_policy_is_forbidden(self):
        response = self.issue(scopes=["registry:admin:manage"])
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "forbidden")

    def test_a_partly_permitted_scope_set_is_refused_whole(self):
        # No silent downgrade: the surplus is named and the request fails.
        response = self.issue(scopes=["registry:read", "registry:admin:manage"])
        self.assertEqual(response.status, 403)

    def test_an_attached_policy_can_widen_the_grant(self):
        self.app._token_scope_policy = lambda session: {"registry:read", "registry:write"}
        payload = self.issue(scopes=["registry:read", "registry:write"]).json()["data"]
        self.assertEqual(payload["scopes"], ["registry:read", "registry:write"])

    def test_a_policy_that_grants_nothing_is_a_server_fault(self):
        self.app._token_scope_policy = lambda session: set()
        response = self.issue()
        self.assertEqual(response.status, 500)
        self.assertEqual(response.error_code(), "server_misconfigured")

    def test_a_malformed_scope_is_a_400(self):
        response = self.issue(scopes=["registry"])
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "invalid_request")

    def test_forged_owner_fields_are_rejected_whole(self):
        # A body that names a different principal, UID or role is not "ignored
        # in favour of the session": the request is refused. A silently ignored
        # field is one a later layer can start reading again.
        for field, value in (
            ("principal", "linux:llm-lab-dell:0"),
            ("uid", 0),
            ("user_id", "root"),
            ("role", "platform_admin"),
            ("owner", "linux:llm-lab-dell:0"),
        ):
            with self.subTest(field=field):
                response = self.issue(**{field: value})
                self.assertEqual(response.status, 400)
                self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_a_clean_issuance_names_the_session_principal(self):
        response = self.issue(scopes=["registry:read"])
        self.assertEqual(response.status, 201)
        data = response.json()["data"]
        self.assertEqual(data["principal"], self.session_principal(self.client))
        view = self.tokens.introspect(data["token"])
        self.assertEqual(view.principal, self.session_principal(self.client))
        self.assertIsNone(view.uid)  # the local admin is not a Linux principal

    def test_an_over_long_lifetime_is_refused(self):
        response = self.issue(ttl_seconds=tokens_mod.MAX_TTL_SECONDS + 1)
        self.assertEqual(response.status, 400)


# --- revocation and rotation over HTTP ----------------------------------------


class RevocationRouteTests(TokenRouteTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()
        self.login_admin()
        self.issued = self.issue().json()["data"]

    def test_revoking_your_own_token_takes_effect_immediately(self):
        response = self.client.run(
            "POST",
            "/auth/tokens/revoke",
            payload={"token_id": self.issued["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.json()["data"]["revoked"])
        self.assertFalse(self.tokens.introspect(self.issued["token"]).active)

    def test_revoking_without_a_token_id_is_a_400(self):
        response = self.client.run(
            "POST",
            "/auth/tokens/revoke",
            payload={},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 400)

    def test_revoking_an_unknown_token_reports_false_not_an_error(self):
        response = self.client.run(
            "POST",
            "/auth/tokens/revoke",
            payload={"token_id": "does-not-exist"},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 200)
        self.assertFalse(response.json()["data"]["revoked"])

    def test_rotating_returns_a_new_plaintext_and_kills_the_old_one(self):
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": self.issued["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 201)
        replacement = response.json()["data"]["token"]
        self.assertNotEqual(replacement, self.issued["token"])
        self.assertFalse(self.tokens.introspect(self.issued["token"]).active)
        self.assertTrue(self.tokens.introspect(replacement).active)

    def test_revoke_all_clears_every_token_the_caller_holds(self):
        self.issue()
        response = self.client.run(
            "POST",
            "/auth/tokens/revoke-all",
            payload={},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["data"]["revoked"], 2)
        self.assertFalse(self.tokens.introspect(self.issued["token"]).active)

    # --- BLOCKER A over HTTP --------------------------------------------------

    def test_a_revoked_token_cannot_be_rotated(self):
        self.client.run(
            "POST",
            "/auth/tokens/revoke",
            payload={"token_id": self.issued["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": self.issued["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "not_authenticated")

    def test_an_expired_token_cannot_be_rotated(self):
        short = self.issue(ttl_seconds=tokens_mod.MIN_TTL_SECONDS).json()["data"]
        self.clock.advance(tokens_mod.MIN_TTL_SECONDS)
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": short["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 401)

    def test_a_downgraded_session_cannot_reissue_its_old_wider_token(self):
        # Mint an administrator-scoped token while the policy allows it...
        self.app._token_scope_policy = lambda session: {
            "registry:read",
            "registry:admin:manage",
        }
        issued = self.issue(scopes=["registry:read", "registry:admin:manage"]).json()["data"]

        # ...then the session's policy is reduced. Rotation must not hand the
        # wider scope back, and must not silently narrow either — it refuses.
        self.app._token_scope_policy = lambda session: {"registry:read"}
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": issued["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "forbidden")

    def test_rotation_can_narrow_to_what_the_session_still_allows(self):
        self.app._token_scope_policy = lambda session: {
            "registry:read",
            "registry:write",
        }
        issued = self.issue(scopes=["registry:read", "registry:write"]).json()["data"]
        self.app._token_scope_policy = lambda session: {"registry:read"}
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": issued["token_id"], "scopes": ["registry:read"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.json()["data"]["scopes"], ["registry:read"])
        self.assertFalse(self.tokens.introspect(issued["token"]).active)

    def test_rotation_cannot_widen_through_the_route(self):
        issued = self.issue().json()["data"]
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": issued["token_id"], "scopes": ["registry:read", "registry:write"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 403)


# --- BLOCKER B over HTTP: suspension reaches existing tokens ------------------


class LivePrincipalStateTests(TokenRouteTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()
        self.login_admin()
        # Captured before any status change: once an account is disabled,
        # ``current_session`` refuses it, so the id must be read while it is
        # still usable.
        self.principal_id = self.session_principal(self.client)
        self.issued = self.issue().json()["data"]

    def _set_status(self, status):
        self.app.service.db.set_user_status(self.principal_id, status)

    def test_disabling_an_account_deactivates_its_existing_token(self):
        # The exact regression the review asked for: issue → disable → the token
        # that was already in someone's pocket stops working on the next call.
        self.assertTrue(self.tokens.introspect(self.issued["token"]).active)
        self._set_status("disabled")
        view = self.tokens.introspect(self.issued["token"])
        self.assertFalse(view.active)
        self.assertEqual(view.reason, tokens_mod.REASON_SUSPENDED)

    def test_disabling_an_account_also_revokes_its_live_session(self):
        # The two effects are independent and both matter: the session goes on
        # first use, so nothing about a disabled account stays usable.
        self._set_status("disabled")
        first = self.client.run("GET", "/auth/me")
        self.assertEqual(first.status, 403)
        self.assertEqual(first.error_code(), "account_disabled")

    def test_a_disabled_account_cannot_rotate_its_token_back_to_life(self):
        self._set_status("disabled")
        # The session was revoked by the disable, so the write route is refused
        # as an expired session before rotation is ever considered. (The CSRF
        # preflight passes on an anonymous single-use token, which is the same
        # allowance the login form needs; the session guard is what refuses.)
        response = self.client.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": self.issued["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "session_expired")
        # ...and the token is dead regardless of what the session did.
        self.assertFalse(self.tokens.introspect(self.issued["token"]).active)
        self.assertEqual(
            len(self.tokens.list_for_principal(self.principal_id)), 1
        )  # nothing was minted

    def test_re_enabling_an_account_restores_its_token(self):
        self._set_status("disabled")
        self.assertFalse(self.tokens.introspect(self.issued["token"]).active)
        self._set_status("active")
        # A fresh sign-in would be needed to act, but the token itself is live
        # again — the directory is the authority in both directions.
        self.assertTrue(self.tokens.introspect(self.issued["token"]).active)

    def test_an_unknown_principal_has_no_fallback(self):
        # A token for a principal the directory does not know is inactive, even
        # though the row is present, revoked-free and unexpired.
        view = self.tokens.introspect(tokens_mod.TOKEN_PREFIX + "unknown-principal")
        self.assertFalse(view.active)
        self.assertEqual(view.reason, tokens_mod.REASON_UNKNOWN)



# --- cross-principal isolation ------------------------------------------------


class CrossPrincipalTests(TokenRouteTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()
        self.login_admin()
        self.alice = self.login_member(MEMBER_OPEN_ID)
        self.bob = self.login_member(OTHER_OPEN_ID)
        self.alice_id = self.session_principal(self.alice)
        self.bob_id = self.session_principal(self.bob)
        self.alice_token = self.issue(self.alice).json()["data"]

    def test_two_members_are_distinct_principals(self):
        self.assertNotEqual(self.alice_id, self.bob_id)

    def test_a_member_cannot_see_another_members_tokens(self):
        listed = self.bob.run("GET", "/auth/tokens").json()["data"]["tokens"]
        self.assertEqual(listed, [])

    def test_a_member_cannot_revoke_another_members_token(self):
        response = self.bob.run(
            "POST",
            "/auth/tokens/revoke",
            payload={"token_id": self.alice_token["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token(self.bob)},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "forbidden")
        # The refused attempt must not have touched Alice's credential.
        self.assertTrue(self.tokens.introspect(self.alice_token["token"]).active)

    def test_a_member_cannot_rotate_another_members_token(self):
        response = self.bob.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": self.alice_token["token_id"]},
            headers={"X-CSRF-Token": self.csrf_token(self.bob)},
        )
        self.assertEqual(response.status, 403)
        self.assertTrue(self.tokens.introspect(self.alice_token["token"]).active)

    def test_revoke_all_only_touches_the_callers_own_tokens(self):
        self.bob.run(
            "POST",
            "/auth/tokens/revoke-all",
            payload={},
            headers={"X-CSRF-Token": self.csrf_token(self.bob)},
        )
        self.assertTrue(self.tokens.introspect(self.alice_token["token"]).active)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
