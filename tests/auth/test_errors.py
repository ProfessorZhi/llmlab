"""Tests for backend.auth.errors."""

import unittest

from backend.auth import errors


class AuthErrorTests(unittest.TestCase):
    def test_envelope_shape_is_stable(self):
        err = errors.InvalidCredentials()
        self.assertEqual(
            err.to_dict(),
            {
                "ok": False,
                "error": {
                    "code": "invalid_credentials",
                    "message": errors.InvalidCredentials.message,
                },
            },
        )

    def test_status_codes(self):
        cases = [
            (errors.InvalidRequest(), 400),
            (errors.NotAuthenticated(), 401),
            (errors.Forbidden(), 403),
            (errors.CsrfRejected(), 403),
            (errors.InvalidState(), 400),
            (errors.ProviderUnavailable(), 502),
            (errors.NotConfigured(), 503),
            (errors.RateLimited(), 429),
        ]
        for err, expected in cases:
            with self.subTest(err=type(err).__name__):
                self.assertEqual(err.http_status, expected)

    def test_oauth_error_mapping(self):
        self.assertEqual(errors.InvalidRequest().oauth_error, "invalid_request")
        self.assertEqual(errors.AccessDenied().oauth_error, "access_denied")
        self.assertEqual(errors.ProviderUnavailable().oauth_error, "temporarily_unavailable")
        self.assertEqual(errors.NotConfigured().oauth_error, "temporarily_unavailable")

    def test_messages_are_generic(self):
        """No user-supplied or secret value may be echoed in the public message."""
        err = errors.InvalidCredentials()
        dump = repr(err.to_dict())
        for leak in ("password", "hash", "scrypt", "Bearer"):
            self.assertNotIn(leak, dump)

    def test_public_message_is_never_empty(self):
        for cls in (errors.InvalidCredentials, errors.NotConfigured, errors.InvalidState):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(cls().message)


if __name__ == "__main__":
    unittest.main()
