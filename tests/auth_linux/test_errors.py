"""The client-facing error envelope."""

from __future__ import annotations

import unittest

from backend.auth_linux import errors

# Words that must never appear in anything a client can read. Kept as a list so
# the failure message names the offending word.
FORBIDDEN_WORDS = [
    "password",
    "passwd",
    "hash",
    "shadow",
    "salt",
    "secret",
    "token",
    "credential",
    "uid",
    "gid",
    "allowlist",
    "pam_unix",
    "broker",
]

ALL_ERRORS = [
    errors.InvalidRequest,
    errors.InvalidCredentials,
    errors.RateLimited,
    errors.NotConfigured,
    errors.PamUnavailable,
    errors.ConfigError,
]


class EnvelopeTests(unittest.TestCase):
    def test_envelope_matches_the_existing_auth_service_shape(self) -> None:
        payload = errors.InvalidCredentials().to_dict()
        self.assertEqual(set(payload), {"ok", "error"})
        self.assertIs(payload["ok"], False)
        self.assertEqual(set(payload["error"]), {"code", "message"})

    def test_every_error_renders_with_a_code_and_a_message(self) -> None:
        for error_class in ALL_ERRORS:
            with self.subTest(error=error_class.__name__):
                payload = error_class().to_dict()
                self.assertTrue(payload["error"]["code"])
                self.assertTrue(payload["error"]["message"])

    def test_codes_are_snake_case(self) -> None:
        for error_class in ALL_ERRORS:
            with self.subTest(error=error_class.__name__):
                code = error_class.code
                self.assertRegex(code, r"^[a-z][a-z0-9_]*$")

    def test_http_statuses_are_sensible(self) -> None:
        self.assertEqual(errors.InvalidRequest().http_status, 400)
        self.assertEqual(errors.InvalidCredentials().http_status, 401)
        self.assertEqual(errors.RateLimited().http_status, 429)
        self.assertEqual(errors.PamUnavailable().http_status, 503)
        self.assertEqual(errors.NotConfigured().http_status, 503)
        self.assertEqual(errors.ConfigError().http_status, 500)


class NonDisclosureTests(unittest.TestCase):
    def test_no_public_message_mentions_a_credential_or_an_internal(self) -> None:
        for error_class in ALL_ERRORS:
            with self.subTest(error=error_class.__name__):
                message = error_class().to_dict()["error"]["message"].lower()
                for word in FORBIDDEN_WORDS:
                    self.assertNotIn(word, message)

    def test_detail_is_not_serialised(self) -> None:
        error = errors.ConfigError(detail="allowlist at /etc/llm-lab/allow.json is world-writable")
        payload = error.to_dict()
        self.assertNotIn("detail", payload["error"])
        self.assertNotIn("world-writable", str(payload))

    def test_repr_does_not_carry_the_detail(self) -> None:
        error = errors.ConfigError(detail="a real path that should stay internal")
        self.assertNotIn("a real path", repr(error))

    def test_custom_message_overrides_but_still_has_a_code(self) -> None:
        error = errors.PamUnavailable("Verification is down.")
        payload = error.to_dict()
        self.assertEqual(payload["error"]["message"], "Verification is down.")
        self.assertEqual(payload["error"]["code"], "pam_unavailable")


class RateLimitTests(unittest.TestCase):
    def test_retry_after_is_optional_and_carried(self) -> None:
        self.assertIsNone(errors.RateLimited().retry_after)
        self.assertEqual(errors.RateLimited(retry_after=42).retry_after, 42)


class UnavailableIsARefusalTests(unittest.TestCase):
    def test_every_error_class_is_an_exception_a_caller_must_handle(self) -> None:
        # There is no error that means "carry on": every one of these is raised
        # on a path where the answer is "do not sign this person in".
        for error_class in ALL_ERRORS:
            with self.subTest(error=error_class.__name__):
                self.assertTrue(issubclass(error_class, errors.LinuxAuthError))
                self.assertTrue(issubclass(error_class, Exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
