"""The anti-enumeration collapse: many internal reasons, few public answers."""

from __future__ import annotations

import unittest

from backend.auth_linux import errors, reasons
from backend.auth_linux.reasons import DenialReason, client_error_for, is_client_visible


class TotalityTests(unittest.TestCase):
    def test_the_reason_set_is_not_empty(self) -> None:
        self.assertGreater(len(reasons.ALL_REASONS), 10)

    def test_every_known_reason_maps_to_a_client_error(self) -> None:
        for reason in sorted(reasons.ALL_REASONS):
            with self.subTest(reason=reason):
                error = client_error_for(reason)
                self.assertIsInstance(error, errors.LinuxAuthError)

    def test_an_unclassified_reason_cannot_leak(self) -> None:
        """A reason someone adds later and forgets to classify must still refuse.

        This is the property that makes the mapping safe to extend: the failure
        mode of forgetting is a vaguer error message, not a wider one.
        """
        for unknown in ("", "some_future_reason", None, "invalid_credentials "):
            with self.subTest(reason=unknown):
                error = client_error_for(unknown)  # type: ignore[arg-type]
                self.assertIsInstance(error, errors.InvalidCredentials)
                self.assertEqual(error.code, "invalid_credentials")

    def test_no_reason_maps_to_a_500_or_a_success(self) -> None:
        for reason in sorted(reasons.ALL_REASONS):
            with self.subTest(reason=reason):
                self.assertNotEqual(client_error_for(reason).http_status, 200)


class CollapseTests(unittest.TestCase):
    """Every account-specific reason has to look identical from outside."""

    ACCOUNT_SPECIFIC = [
        DenialReason.UNKNOWN_USER,
        DenialReason.NOT_ALLOWLISTED,
        DenialReason.ROOT_FORBIDDEN,
        DenialReason.SYSTEM_ACCOUNT,
        DenialReason.NON_LOGIN_SHELL,
        DenialReason.EXPLICITLY_DENIED,
        DenialReason.INVALID_CREDENTIALS,
        DenialReason.ACCOUNT_LOCKED,
        DenialReason.ACCOUNT_EXPIRED,
        DenialReason.PASSWORD_EXPIRED,
        DenialReason.ACCOUNT_DISABLED,
        DenialReason.ACCOUNT_CHECK_FAILED,
        DenialReason.UID_MISMATCH,
        DenialReason.EMPTY_PASSWORD,
        DenialReason.INVALID_NAME,
    ]

    def test_account_reasons_are_indistinguishable_from_outside(self) -> None:
        rendered = {
            reason: client_error_for(reason).to_dict() for reason in self.ACCOUNT_SPECIFIC
        }
        first = rendered[self.ACCOUNT_SPECIFIC[0]]
        for reason, payload in rendered.items():
            with self.subTest(reason=reason):
                self.assertEqual(payload, first)
        # And in particular none of them says "the service is broken", which
        # would tell an attacker their probe reached something real.
        self.assertEqual(first["error"]["code"], "invalid_credentials")

    def test_only_the_intended_reasons_are_client_visible(self) -> None:
        visible = {reason for reason in reasons.ALL_REASONS if is_client_visible(reason)}
        self.assertEqual(
            visible,
            {
                DenialReason.RATE_LIMITED,
                DenialReason.BROKER_UNAVAILABLE,
                DenialReason.BROKER_PROTOCOL_ERROR,
                DenialReason.NOT_CONFIGURED,
            },
        )

    def test_broker_trouble_is_never_reported_as_bad_credentials(self) -> None:
        """The distinction that matters for the fail-closed promise.

        "We could not check" and "the password is wrong" must not be the same
        answer, or an outage would be indistinguishable from a rejection and an
        operator would never learn the broker was down.
        """
        for reason in (DenialReason.BROKER_UNAVAILABLE, DenialReason.BROKER_PROTOCOL_ERROR):
            with self.subTest(reason=reason):
                error = client_error_for(reason)
                self.assertIsInstance(error, errors.PamUnavailable)
                self.assertEqual(error.http_status, 503)

    def test_rate_limited_keeps_its_retry_hint(self) -> None:
        error = client_error_for(DenialReason.RATE_LIMITED, retry_after=30)
        self.assertIsInstance(error, errors.RateLimited)
        self.assertEqual(error.retry_after, 30)

    def test_the_public_surface_is_small(self) -> None:
        codes = {client_error_for(reason).code for reason in reasons.ALL_REASONS}
        self.assertLessEqual(
            codes,
            {"invalid_credentials", "pam_unavailable", "rate_limited", "not_configured"},
        )


class ReasonHygieneTests(unittest.TestCase):
    def test_reasons_are_plain_lower_snake_case_strings(self) -> None:
        for reason in sorted(reasons.ALL_REASONS):
            with self.subTest(reason=reason):
                self.assertRegex(reason, r"^[a-z][a-z0-9_]*$")

    def test_no_reason_name_describes_a_credential_value(self) -> None:
        for reason in sorted(reasons.ALL_REASONS):
            with self.subTest(reason=reason):
                self.assertNotIn("password_was", reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
