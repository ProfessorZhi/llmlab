"""Tests for backend.auth.local_admin — the local administrator credential file.

The password used here is a synthetic test value. The real administrator
password is produced only by the interactive bootstrap on the machine that owns
it, and never appears in this repository.
"""

import os
import pathlib
import shutil
import tempfile
import unittest

from backend.auth import errors, hashing, local_admin

SYNTHETIC = "synthetic-admin-passphrase-1"
USERNAME = "admin"


class Probe:
    """Overrides the filesystem facts so permission rules are testable on Windows."""

    def __init__(self, *, mode=0o600, uid=None, is_regular=True, exists=None):
        self.mode = mode
        self.uid = uid
        self.is_regular = is_regular
        # None means "ask the real filesystem"; pass a bool to fake it.
        self.exists = exists

    def __call__(self, path: pathlib.Path) -> local_admin.FileFacts:
        really_there = path.exists()
        exists = really_there if self.exists is None else self.exists
        size = path.stat().st_size if really_there else 0
        return local_admin.FileFacts(
            exists=exists,
            is_regular=self.is_regular,
            mode=self.mode,
            size=size if really_there else 0,
            uid=self.uid,
        )


class LocalAdminTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-admin-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hash_file = self.tmp / "admin_password.hash"

    def store(self, probe=None, **kwargs):
        return local_admin.AdminCredentialStore(
            self.hash_file,
            username=USERNAME,
            enforce_permissions=True,
            fs_probe=probe or Probe(),
            **kwargs,
        )

    def write(self, content: str) -> None:
        self.hash_file.write_text(content, encoding="utf-8")


class UnsetAdminTests(LocalAdminTestCase):
    def test_missing_file_is_not_configured(self):
        store = self.store()
        self.assertFalse(store.is_configured())
        self.assertEqual(store.status()["state"], local_admin.STATE_MISSING)

    def test_missing_file_refuses_login_rather_than_allowing_a_blank_password(self):
        store = self.store()
        with self.assertRaises(errors.NotConfigured):
            store.authenticate(USERNAME, SYNTHETIC)

    def test_blank_password_never_authenticates(self):
        self.write(hashing.hash_password(SYNTHETIC) + "\n")
        store = self.store()
        for blank in ("", " "):
            with self.subTest(blank=repr(blank)):
                self.assertFalse(store.authenticate(USERNAME, blank))


class ActiveAdminTests(LocalAdminTestCase):
    def setUp(self):
        super().setUp()
        self.write(hashing.hash_password(SYNTHETIC) + "\n")

    def test_correct_password_authenticates(self):
        self.assertTrue(self.store().authenticate(USERNAME, SYNTHETIC))

    def test_wrong_password_returns_false(self):
        self.assertFalse(self.store().authenticate(USERNAME, SYNTHETIC + "x"))

    def test_unknown_username_returns_false_even_with_the_right_password(self):
        self.assertFalse(self.store().authenticate("root", SYNTHETIC))

    def test_username_comparison_is_constant_time(self):
        store = self.store()
        calls = []
        real = local_admin.hmac.compare_digest

        def spy(left, right):
            calls.append((left, right))
            return real(left, right)

        local_admin.hmac.compare_digest = spy
        try:
            store.authenticate(USERNAME, SYNTHETIC)
        finally:
            local_admin.hmac.compare_digest = real
        self.assertGreaterEqual(len(calls), 2, "username must be compared in constant time too")

    def test_trailing_whitespace_and_comments_are_tolerated(self):
        self.write("# managed by the bootstrap CLI\n\n" + hashing.hash_password(SYNTHETIC) + "  \n")
        self.assertTrue(self.store().authenticate(USERNAME, SYNTHETIC))

    def test_status_reports_algorithm_and_needs_no_secret(self):
        status = self.store().status()
        self.assertEqual(status["state"], local_admin.STATE_ACTIVE)
        self.assertEqual(status["algorithm"], "scrypt")
        self.assertFalse(status["needs_rehash"])
        self.assertNotIn("hash", " ".join(k for k in status if k != "needs_rehash"))


class DisabledAdminTests(LocalAdminTestCase):
    def test_sentinel_is_recognised(self):
        self.write(local_admin.DISABLED_SENTINEL + "\n")
        store = self.store()
        self.assertFalse(store.is_configured())
        self.assertEqual(store.status()["state"], local_admin.STATE_DISABLED)

    def test_disable_beats_a_leftover_hash(self):
        self.write(hashing.hash_password(SYNTHETIC) + "\n" + local_admin.DISABLED_SENTINEL + "\n")
        store = self.store()
        self.assertEqual(store.status()["state"], local_admin.STATE_DISABLED)
        with self.assertRaises(errors.AccountDisabled):
            store.authenticate(USERNAME, SYNTHETIC)


class PermissionTests(LocalAdminTestCase):
    def setUp(self):
        super().setUp()
        self.write(hashing.hash_password(SYNTHETIC) + "\n")

    def test_group_or_other_readable_file_is_refused(self):
        for mode in (0o644, 0o640, 0o604, 0o666, 0o777):
            with self.subTest(mode=oct(mode)):
                store = self.store(probe=Probe(mode=mode))
                with self.assertRaises(errors.ConfigError):
                    store.authenticate(USERNAME, SYNTHETIC)

    def test_owner_only_mode_is_accepted(self):
        store = self.store(probe=Probe(mode=0o600))
        self.assertTrue(store.authenticate(USERNAME, SYNTHETIC))

    def test_foreign_owner_is_refused(self):
        store = self.store(probe=Probe(mode=0o600, uid=(os.getuid() if hasattr(os, "getuid") else 0) + 12345), process_uid=0)
        with self.assertRaises(errors.ConfigError):
            store.authenticate(USERNAME, SYNTHETIC)

    def test_non_regular_file_is_refused(self):
        store = self.store(probe=Probe(is_regular=False))
        with self.assertRaises(errors.ConfigError):
            store.authenticate(USERNAME, SYNTHETIC)

    def test_oversized_file_is_refused(self):
        store = self.store(probe=Probe())
        self.hash_file.write_text("x" * 5000, encoding="utf-8")
        with self.assertRaises(errors.ConfigError):
            store.authenticate(USERNAME, SYNTHETIC)

    def test_permission_enforcement_can_be_relaxed_explicitly(self):
        store = local_admin.AdminCredentialStore(
            self.hash_file,
            username=USERNAME,
            enforce_permissions=False,
            fs_probe=Probe(mode=0o644),
        )
        self.assertTrue(store.authenticate(USERNAME, SYNTHETIC))


class BrokenFileTests(LocalAdminTestCase):
    def test_unparseable_content_is_a_hard_failure(self):
        for content in ("garbage\n", "scrypt$n=1,r=1,p=1$!!$!!\n", "a\nb\n"):
            with self.subTest(content=content):
                self.write(content)
                store = self.store()
                self.assertEqual(store.status()["state"], local_admin.STATE_BROKEN)
                with self.assertRaises(errors.ConfigError):
                    store.authenticate(USERNAME, SYNTHETIC)

    def test_weak_parameters_are_flagged_for_rotation(self):
        self.write(hashing.hash_password(SYNTHETIC, n=2 ** 10) + "\n")
        self.assertTrue(self.store().status()["needs_rehash"])


if __name__ == "__main__":
    unittest.main()
