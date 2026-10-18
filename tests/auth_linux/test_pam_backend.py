"""The PAM seam — which today refuses everything, on purpose."""

from __future__ import annotations

import unittest

from backend.auth_linux import errors
from backend.auth_linux.pam_backend import (
    ALL_OUTCOMES,
    BACKEND_UNAVAILABLE,
    PamOutcome,
    PamResult,
    UnavailablePamBackend,
    build_pam_backend,
)

from . import support


class UnavailableBackendTests(unittest.TestCase):
    """The shipped default. Its job is to never authenticate anyone."""

    def test_it_refuses(self) -> None:
        backend = UnavailablePamBackend()
        result = backend.verify(user=support.MEMBER_NAME, password=support.GOOD_PASSWORD)
        self.assertEqual(result.outcome, PamOutcome.UNAVAILABLE)

    def test_it_refuses_regardless_of_what_it_is_given(self) -> None:
        backend = UnavailablePamBackend()
        for user, password in (
            ("root", ""),
            ("", ""),
            (support.MEMBER_NAME, support.GOOD_PASSWORD),
            ("a" * 40, "x" * 900),
        ):
            with self.subTest(user=user):
                self.assertEqual(backend.verify(user=user, password=password).outcome, PamOutcome.UNAVAILABLE)

    def test_it_does_not_authenticate_even_for_an_allowlisted_account(self) -> None:
        """Being in the allowlist is an authorisation fact, not a credential."""
        result = UnavailablePamBackend().verify(
            user=support.ADMIN_NAME, password=support.GOOD_PASSWORD
        )
        self.assertNotEqual(result.outcome, PamOutcome.AUTHENTICATED)


class BackendSelectionTests(unittest.TestCase):
    def test_only_the_refusing_backend_is_selectable(self) -> None:
        backend = build_pam_backend(BACKEND_UNAVAILABLE)
        self.assertIsInstance(backend, UnavailablePamBackend)

    def test_an_unknown_backend_name_is_refused_not_ignored(self) -> None:
        """A typo must stop the broker, not silently leave it on a default."""
        for name in ("pam_unix", "ctypes", "", "unavailable ", "None"):
            with self.subTest(name=name):
                with self.assertRaises(errors.ConfigError):
                    build_pam_backend(name)

    def test_there_is_no_shell_out_backend(self) -> None:
        for name in ("su", "login", "subprocess", "unix_chkpwd"):
            with self.subTest(name=name):
                with self.assertRaises(errors.ConfigError):
                    build_pam_backend(name)


class PamResultTests(unittest.TestCase):
    def test_an_unknown_outcome_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            PamResult(outcome="definitely_authenticated")

    def test_every_declared_outcome_is_accepted(self) -> None:
        for outcome in sorted(ALL_OUTCOMES):
            with self.subTest(outcome=outcome):
                self.assertEqual(PamResult(outcome=outcome).outcome, outcome)

    def test_only_one_outcome_means_success(self) -> None:
        """A guard against a future outcome being read as "authenticated"."""
        successes = {o for o in ALL_OUTCOMES if "authenticated" in o and o != "unauthenticated"}
        self.assertEqual(successes, {PamOutcome.AUTHENTICATED})
        self.assertEqual(PamOutcome.AUTHENTICATED, "authenticated")

    def test_the_result_carries_a_detail_only_for_the_server_side(self) -> None:
        result = PamResult(outcome=PamOutcome.UNAVAILABLE, detail="pam_unix not loadable")
        # The detail exists for the broker's own logs and is never encoded onto
        # the wire, which the protocol tests assert from the other direction.
        self.assertEqual(result.detail, "pam_unix not loadable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
