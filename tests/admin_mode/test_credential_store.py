"""The shared admin credential: what it is, and what it refuses to be.

The state that matters most is the one nobody writes a test for by instinct:
**absent**. A deployment with no credential file must be one where nobody can
enter admin mode — not one where a default, a demo value, or an empty password
lets someone in.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.auth import hashing
from backend.bff import admin_mode as am

from . import support


def store_in(directory: str | Path, **kwargs) -> am.AdminCredentialStore:
    return am.AdminCredentialStore(
        Path(directory) / am.DEFAULT_CREDENTIAL_FILE_NAME, **kwargs
    )


class AbsentCredentialTests(unittest.TestCase):
    """The default state of this feature, and it is a refusal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_file_means_not_configured(self) -> None:
        store = store_in(self.dir)
        self.assertFalse(store.is_configured())
        self.assertEqual(store.status()["state"], am.STATE_MISSING)
        with self.assertRaises(am.NotConfigured):
            store.verify(support.SHARED_PASSWORD)

    def test_no_file_means_there_is_no_credential_version_to_match(self) -> None:
        self.assertEqual(store_in(self.dir).credential_version(), "none")

    def test_an_empty_password_is_refused_even_with_a_live_credential(self) -> None:
        support.write_credential(self.dir)
        store = store_in(self.dir)
        for password in ("", None, 123, b"x"):
            with self.subTest(password=password):
                self.assertFalse(store.verify(password))  # type: ignore[arg-type]

    def test_an_empty_file_is_broken_rather_than_open(self) -> None:
        path = Path(self.dir) / am.DEFAULT_CREDENTIAL_FILE_NAME
        path.write_text("", encoding="utf-8")
        os.chmod(path, 0o600)
        self.assertEqual(store_in(self.dir).status()["state"], am.STATE_BROKEN)
        with self.assertRaises(am.Misconfigured):
            store_in(self.dir).verify(support.SHARED_PASSWORD)

    def test_there_is_no_default_demo_password(self) -> None:
        """Nothing in the module supplies a fallback credential.

        Asserted against a handful of the values a "demo default" would
        plausibly be, so that adding one later fails here rather than shipping.
        """
        store = store_in(self.dir)
        for candidate in ("admin", "admin123", "changeme", "password", "llm-lab", "demo"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(am.NotConfigured):
                    store.verify(candidate)


class LiveCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        support.write_credential(self.dir)
        self.store = store_in(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_configured_and_active(self) -> None:
        self.assertTrue(self.store.is_configured())
        self.assertEqual(self.store.status()["state"], am.STATE_ACTIVE)
        self.assertEqual(self.store.status()["algorithm"], "scrypt")

    def test_the_right_password_verifies(self) -> None:
        self.assertTrue(self.store.verify(support.SHARED_PASSWORD))

    def test_a_wrong_password_does_not(self) -> None:
        self.assertFalse(self.store.verify(support.OTHER_SHARED_PASSWORD))
        self.assertFalse(self.store.verify(support.SHARED_PASSWORD + "x"))
        self.assertFalse(self.store.verify(support.SHARED_PASSWORD[:-1]))

    def test_status_never_leaks_the_hash(self) -> None:
        report = self.store.status()
        rendered = repr(report)
        encoded = support.credential_line()
        self.assertNotIn(encoded, rendered)
        self.assertNotIn(encoded.split("$")[3], rendered)
        self.assertNotIn("$scrypt$", rendered)

    def test_the_version_token_is_stable_and_reveals_nothing(self) -> None:
        first = self.store.credential_version()
        second = self.store.credential_version()
        self.assertEqual(first, second)
        self.assertNotIn(support.credential_line()[:16], first)

    def test_a_rotation_changes_the_version_token(self) -> None:
        before = self.store.credential_version()
        support.write_credential(self.dir, support.OTHER_SHARED_PASSWORD)
        after = self.store.credential_version()
        self.assertNotEqual(before, after)

    def test_a_disable_changes_the_version_token_to_the_absent_one(self) -> None:
        path = Path(self.dir) / am.DEFAULT_CREDENTIAL_FILE_NAME
        path.write_text(am.DISABLED_SENTINEL + "\n", encoding="utf-8")
        self.assertEqual(store_in(self.dir).credential_version(), "none")


class DisabledCredentialTests(unittest.TestCase):
    def test_the_sentinel_disables_rather_than_opens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / am.DEFAULT_CREDENTIAL_FILE_NAME
            path.write_text(am.DISABLED_SENTINEL + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            store = store_in(tmp)
            self.assertEqual(store.status()["state"], am.STATE_DISABLED)
            self.assertFalse(store.is_configured())
            with self.assertRaises(am.NotConfigured):
                store.verify(support.SHARED_PASSWORD)


class FileHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_group_or_world_readable_file_is_refused(self) -> None:
        for mode in (0o644, 0o640, 0o604, 0o666, 0o660):
            with self.subTest(mode=oct(mode)):
                support.write_credential(self.dir, mode=mode)
                with self.assertRaises(am.Misconfigured):
                    store_in(self.dir).verify(support.SHARED_PASSWORD)

    def test_a_file_owned_by_another_account_is_refused(self) -> None:
        support.write_credential(self.dir)
        store = store_in(self.dir, process_uid=os.getuid() + 1)
        with self.assertRaises(am.Misconfigured):
            store.verify(support.SHARED_PASSWORD)

    def test_a_non_regular_file_is_refused(self) -> None:
        target = Path(self.dir) / "real.hash"
        support.write_credential(self.dir, name="real.hash")
        link = Path(self.dir) / am.DEFAULT_CREDENTIAL_FILE_NAME
        link.symlink_to(target)
        with self.assertRaises(am.Misconfigured):
            store_in(self.dir).verify(support.SHARED_PASSWORD)

    def test_an_oversized_file_is_refused(self) -> None:
        path = Path(self.dir) / am.DEFAULT_CREDENTIAL_FILE_NAME
        path.write_text("x" * (am.MAX_CREDENTIAL_FILE_BYTES + 1), encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaises(am.Misconfigured):
            store_in(self.dir).verify(support.SHARED_PASSWORD)

    def test_a_multi_line_file_is_broken(self) -> None:
        path = Path(self.dir) / am.DEFAULT_CREDENTIAL_FILE_NAME
        path.write_text(
            support.credential_line() + "\n" + support.credential_line(support.OTHER_SHARED_PASSWORD) + "\n",
            encoding="utf-8",
        )
        os.chmod(path, 0o600)
        self.assertEqual(store_in(self.dir).status()["state"], am.STATE_BROKEN)


class SecurityFloorTests(unittest.TestCase):
    """A weakened hash is refused, not accepted because it still verifies."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_below_floor_scrypt_hash_is_refused(self) -> None:
        support.write_credential(self.dir, n=1024)
        store = store_in(self.dir)
        self.assertEqual(store.status()["state"], am.STATE_BROKEN)
        self.assertFalse(store.status()["meets_floor"])
        with self.assertRaises(am.Misconfigured):
            store.verify(support.SHARED_PASSWORD)

    def test_a_below_floor_cost_factor_is_refused(self) -> None:
        support.write_credential(self.dir, n=1 << 13)
        self.assertEqual(store_in(self.dir).status()["state"], am.STATE_BROKEN)

    def test_the_shipped_defaults_are_at_or_above_the_floor(self) -> None:
        support.write_credential(self.dir)
        self.assertTrue(store_in(self.dir).status()["meets_floor"])

    def test_a_lower_parallelism_is_refused(self) -> None:
        support.write_credential(self.dir, p=1, r=4)
        self.assertEqual(store_in(self.dir).status()["state"], am.STATE_BROKEN)


class HashFloorUnitTests(unittest.TestCase):
    def test_the_floor_accepts_the_house_parameters(self) -> None:
        self.assertTrue(am.hash_meets_floor(hashing.hash_password("synthetic-value-0001")))

    def test_the_floor_refuses_weak_scrypt_parameters(self) -> None:
        weak = hashing.hash_password("synthetic-value-0001", n=1024)
        self.assertFalse(am.hash_meets_floor(weak))

    def test_the_floor_refuses_junk(self) -> None:
        for value in ("", None, 42, "not-a-hash", "$scrypt$n=x,r=8,p=1$a$b", "plaintext"):
            with self.subTest(value=value):
                self.assertFalse(am.hash_meets_floor(value))

    def test_scrypt_parameters_are_parsed_without_the_private_helper(self) -> None:
        encoded = hashing.hash_password("synthetic-value-0001", n=1 << 15, r=8, p=2)
        self.assertEqual(am.scrypt_params(encoded), (1 << 15, 8, 2))

    def test_argon2_parameters_are_parsed_when_present(self) -> None:
        if not hashing.argon2_available():  # pragma: no cover - host dependent
            self.skipTest("argon2-cffi is not installed")
        encoded = hashing.hash_password("synthetic-value-0001", algorithm="argon2id")
        parsed = am.argon2_params(encoded)
        self.assertIsNotNone(parsed)
        self.assertTrue(am.hash_meets_floor(encoded))

    def test_an_argon2id_hash_below_the_floor_is_refused(self) -> None:
        cheap = "$argon2id$v=19$m=1024,t=1,p=1$c2FsdHNhbHRzYWx0c2FsdA$" + "A" * 43
        self.assertFalse(am.hash_meets_floor(cheap))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
