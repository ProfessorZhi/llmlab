"""Read-only account lookup: name shape, and the UID cross-check."""

from __future__ import annotations

import unittest

from backend.auth_linux.accounts import (
    MAX_LOGIN_NAME_LENGTH,
    LinuxAccount,
    SystemPwdSource,
    canonical_login_name,
    uid_matches,
)

from . import support


class CanonicalNameTests(unittest.TestCase):
    def test_ordinary_names_pass_through_unchanged(self) -> None:
        for name in ("carol", "alice", "dave", "bob", "a", "_svc", "user-1", "u_2"):
            with self.subTest(name=name):
                # Returned as-is. No case folding, no trimming, no aliasing.
                self.assertEqual(canonical_login_name(name), name)

    def test_names_that_are_not_login_names_are_refused(self) -> None:
        rejected = [
            "",
            "Root",  # upper case
            "ROOT",
            "carol ",  # trailing space
            " carol",
            "carol\n",
            "carol\tx",
            "-carol",  # leading dash: would read as an option to a CLI
            "1carol",  # leading digit
            "fj d",
            "carol/../root",
            "carol:0:0",
            "root\x00",
            "josé",  # non-ASCII
            "a" * (MAX_LOGIN_NAME_LENGTH + 1),
            None,
            123,
            b"carol",
            ["carol"],
        ]
        for name in rejected:
            with self.subTest(name=name):
                self.assertIsNone(canonical_login_name(name))

    def test_the_length_boundary_is_exact(self) -> None:
        allowed = "a" * MAX_LOGIN_NAME_LENGTH
        self.assertEqual(canonical_login_name(allowed), allowed)
        self.assertIsNone(canonical_login_name("a" * (MAX_LOGIN_NAME_LENGTH + 1)))


class UidMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.account = support.make_account(support.MEMBER_NAME, support.MEMBER_UID)

    def test_both_uid_and_name_must_agree(self) -> None:
        self.assertTrue(
            uid_matches(self.account, uid=support.MEMBER_UID, name=support.MEMBER_NAME)
        )

    def test_a_different_name_for_the_same_uid_is_refused(self) -> None:
        self.assertFalse(
            uid_matches(self.account, uid=support.MEMBER_UID, name=support.ADMIN_NAME)
        )

    def test_a_different_uid_for_the_same_name_is_refused(self) -> None:
        # The interesting attack: keep the familiar name, point it at UID 0.
        self.assertFalse(uid_matches(self.account, uid=0, name=support.MEMBER_NAME))

    def test_non_integer_uid_is_refused(self) -> None:
        for bad in ("4001", 4001.0, True, False, None, [4001]):
            with self.subTest(uid=bad):
                self.assertFalse(uid_matches(self.account, uid=bad, name=support.MEMBER_NAME))

    def test_non_string_name_is_refused(self) -> None:
        for bad in (None, 4001, b"alice"):
            with self.subTest(name=bad):
                self.assertFalse(uid_matches(self.account, uid=support.MEMBER_UID, name=bad))


class LinuxAccountTests(unittest.TestCase):
    def test_negative_uid_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            LinuxAccount(name="x", uid=-1, gid=0, shell="/bin/bash", home="/home/x")

    def test_non_integer_uid_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            LinuxAccount(name="x", uid="0", gid=0, shell="/bin/bash", home="/home/x")  # type: ignore[arg-type]


class SystemPwdSourceTests(unittest.TestCase):
    """Only the guard paths and a read-only sanity lookup.

    No password is involved anywhere in ``pwd``; this source cannot verify one
    and the module never asks it to.
    """

    def setUp(self) -> None:
        self.source = SystemPwdSource()

    def test_malformed_input_never_reaches_the_lookup(self) -> None:
        for bad in ("", None, 123, b"root", []):
            with self.subTest(name=bad):
                self.assertIsNone(self.source.by_name(bad))  # type: ignore[arg-type]

    def test_malformed_uid_never_reaches_the_lookup(self) -> None:
        for bad in (-1, "0", None, True, 1.5):
            with self.subTest(uid=bad):
                self.assertIsNone(self.source.by_uid(bad))  # type: ignore[arg-type]

    def test_an_account_that_does_not_exist_is_none_not_an_exception(self) -> None:
        self.assertIsNone(self.source.by_name("no-such-account-zz9-plural-z"))

    def test_the_lookup_agrees_with_the_system_for_root(self) -> None:
        # A read-only sanity check that the real source is wired to real NSS.
        # Skipped rather than failed if this machine has no root entry, which
        # would be strange but is not this module's business.
        account = self.source.by_name("root")
        if account is None:  # pragma: no cover - defensive
            self.skipTest("no root entry on this machine")
        self.assertEqual(account.uid, 0)
        self.assertEqual(self.source.by_uid(0).name, "root")  # type: ignore[union-attr]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
