"""A Linux session's scoped CLI token, over the real BFF HTTP surface.

Two properties are only observable end-to-end:

* **Elevation is per-session browser state, not a property of the person.** A
  member who enters temporary admin mode still mints a ``member`` token with the
  narrow scope set. The token route reads the auth-store session row, which
  elevation never rewrites.
* **A token belongs to one Linux UID.** A second signed-in account cannot list,
  revoke or rotate the first account's token.

PAM is the in-process double; the session, CSRF and token stores are the real
implementations.
"""

from __future__ import annotations

import tempfile
import unittest

from backend.auth import tokens as tokens_mod
from tests.auth.support import WsgiClient
from tests.bff import support as S


class LinuxTokenIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

        # The deployment wiring the Integration lane performs. Without it the
        # token routes correctly answer 503 and this file would prove nothing.
        self.tokens = tokens_mod.build_token_service(
            self.h.state_dir,
            principal_state=tokens_mod.AuthStorePrincipalState(self.h.auth_app.service.db),
        )
        self.tokens.initialize()
        self.h.auth_app.tokens = self.tokens

        self.member = self._client()
        self.other = self._client()
        self._sign_in(self.member, S.MEMBER_NAME)
        self._sign_in(self.other, S.REAL_ACCOUNT_NAME)

    # --- helpers -------------------------------------------------------------

    def _client(self) -> WsgiClient:
        return WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def _sign_in(self, client: WsgiClient, name: str) -> None:
        response = S.login(client, name, S.GOOD_PASSWORD)
        self.assertEqual(response.status, 200, response.body)

    def _issue(self, client: WsgiClient, **payload):
        return client.run(
            "POST",
            "/auth/tokens",
            payload=payload,
            headers={"X-CSRF-Token": S.csrf(client)},
        )

    # --- temporary admin must not upgrade a CLI token ------------------------

    def test_an_elevated_member_session_still_mints_a_member_token(self):
        plain = self._issue(self.member)
        self.assertEqual(plain.status, 201, plain.body)
        plain_view = self.tokens.introspect(plain.json()["data"]["token"])
        self.assertEqual(plain_view.roles, frozenset({"member"}))

        entered = S.enter_admin_mode(self.member)
        self.assertEqual(entered.status, 200, entered.body)
        principal = self.h.app.resolve_session(
            self.member.cookies[self.h.app.cfg.cookie_name]
        )
        self.assertTrue(principal.elevated)

        elevated = self._issue(self.member)
        self.assertEqual(elevated.status, 201, elevated.body)
        view = self.tokens.introspect(elevated.json()["data"]["token"])

        self.assertEqual(view.roles, frozenset({"member"}))
        self.assertEqual(view.scopes, frozenset({"registry:read"}))
        self.assertNotIn("portal:admin:read", view.scopes)

    # --- cross-UID isolation -------------------------------------------------

    def test_a_second_uid_cannot_see_or_touch_the_first_uids_token(self):
        issued = self._issue(self.member).json()["data"]
        token_id = issued["token_id"]

        listed = self.other.run("GET", "/auth/tokens").json()["data"]["tokens"]
        self.assertEqual(listed, [])

        revoked = self.other.run(
            "POST",
            "/auth/tokens/revoke",
            payload={"token_id": token_id},
            headers={"X-CSRF-Token": S.csrf(self.other)},
        )
        self.assertEqual(revoked.status, 403)
        self.assertEqual(revoked.error_code(), "forbidden")
        self.assertTrue(self.tokens.introspect(issued["token"]).active)

        rotated = self.other.run(
            "POST",
            "/auth/tokens/rotate",
            payload={"token_id": token_id},
            headers={"X-CSRF-Token": S.csrf(self.other)},
        )
        self.assertEqual(rotated.status, 403)
        self.assertTrue(self.tokens.introspect(issued["token"]).active)
