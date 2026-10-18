"""The Linux sign-in path over the real BFF HTTP route.

The PAM backend is still the in-process double: this file proves the machinery
around it, not PAM itself. But the route is what a caller actually reaches, and
three properties only exist there:

* ``root`` is refused before the broker, with the same answer as any other
  account-level refusal;
* an anonymous ``GET /auth/linux/status`` reports counts, never the policy's
  account names;
* a request cannot name an identity (``uid``, ``role``, ``owner`` ...) in its
  body or query string. The BFF already refuses those as headers; the write
  preflight it reuses from ``backend.auth`` now refuses them as fields too.
"""

from __future__ import annotations

import tempfile
import unittest

from tests.auth.support import WsgiClient
from tests.bff import support as S


class LinuxHttpRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.client = WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def _csrf(self):
        return S.csrf(self.client)

    def _login(self, *, path: str = "/auth/linux/login", **overrides):
        payload = {"login_name": S.MEMBER_NAME, "password": S.GOOD_PASSWORD}
        payload.update(overrides)
        return self.client.run(
            "POST", path, payload=payload, headers={"X-CSRF-Token": self._csrf()}
        )

    def test_root_is_refused_on_the_http_route(self):
        response = self._login(login_name="root")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "invalid_credentials")
        self.assertIsNone(response.set_cookie(self.h.app.cfg.cookie_name))
        self.assertEqual(self.client.run("GET", "/auth/me").status, 401)

    def test_root_is_indistinguishable_from_an_unknown_account(self):
        root = self._login(login_name="root")
        unknown = self._login(login_name="not-an-account")
        self.assertEqual(root.status, unknown.status)
        self.assertEqual(root.json(), unknown.json())

    def test_identity_fields_in_the_login_body_are_refused(self):
        for field, value in (
            ("uid", 0),
            ("role", "platform_admin"),
            ("owner", "linux:synthetic-host:0"),
            ("principal", "linux:synthetic-host:0"),
        ):
            with self.subTest(field=field):
                response = self._login(**{field: value})
                self.assertEqual(response.status, 400)
                self.assertEqual(response.error_code(), "identity_assertion_rejected")
                self.assertIsNone(response.set_cookie(self.h.app.cfg.cookie_name))

    def test_identity_fields_in_the_login_query_are_refused(self):
        response = self._login(path="/auth/linux/login?uid=0")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_the_status_endpoint_is_anonymous_and_names_no_account(self):
        anonymous = WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = anonymous.run("GET", "/auth/linux/status")
        self.assertEqual(response.status, 200)

        data = response.json()["data"]
        self.assertEqual(data["allowlist_size"], 4)
        self.assertNotIn("allowed_users", data)
        self.assertNotIn("platform_admins", data)
        for field, value in data.items():
            with self.subTest(field=field):
                self.assertNotIsInstance(value, (list, tuple, set, frozenset))

        rendered = response.body.decode("utf-8")
        for name in (
            S.ADMIN_NAME,
            S.MEMBER_NAME,
            S.OTHER_MEMBER_NAME,
            S.REAL_ACCOUNT_NAME,
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, rendered)
