"""Identity is never accepted from a request field.

A ``uid``, ``role`` or ``owner`` in a query string or JSON body is not a hint
the server may quietly ignore. The whole request is refused, because "read but
not used" is how a forged field comes back into use later — a middleware, a
log processor, or a well-meaning refactor.

The server-side session is the only identity source; the local-admin login
still accepts ``username`` because that is the credential being authenticated,
not a principal assertion.
"""

import unittest

from backend.auth import routes
from backend.registry.identity import FORBIDDEN_IDENTITY_FIELDS
from tests.auth.test_routes import SYNTHETIC_ADMIN_PASSWORD, HttpTestCase


class IdentityFieldSetTests(unittest.TestCase):
    def test_the_auth_guard_uses_the_same_field_names_as_the_registry(self):
        """One forged field must have one answer across the HTTP surface."""
        self.assertEqual(routes.ASSERTED_IDENTITY_FIELDS, FORBIDDEN_IDENTITY_FIELDS)


class LocalLoginIdentityAssertionTests(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()

    def _login(self, *, path="/auth/local/login", **extra):
        payload = {"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD}
        payload.update(extra)
        return self.client.run(
            "POST", path, payload=payload, headers={"X-CSRF-Token": self.csrf_token()}
        )

    def test_identity_fields_in_the_login_body_are_refused(self):
        for field, value in (
            ("uid", 0),
            ("role", "platform_admin"),
            ("owner", "linux:synthetic-host:0"),
            ("principal", "linux:synthetic-host:0"),
            ("user_id", "root"),
            ("linux_user", "root"),
            ("is_admin", True),
        ):
            with self.subTest(field=field):
                response = self._login(**{field: value})
                self.assertEqual(response.status, 400)
                self.assertEqual(response.error_code(), "identity_assertion_rejected")
                self.assertIsNone(response.set_cookie("llm_lab_session"))

    def test_identity_fields_in_the_query_are_refused(self):
        response = self._login(path="/auth/local/login?uid=0")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_the_credential_username_is_still_accepted(self):
        response = self._login()
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(response.set_cookie("llm_lab_session"))
