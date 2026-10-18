"""Tests for backend.auth.hashing.

All passwords here are synthetic test values. No real credential material is
ever used in this suite.
"""

import unittest

from backend.auth import hashing

SYNTHETIC = "synthetic-test-passphrase-not-real-1"


class HashPasswordTests(unittest.TestCase):
    def test_default_algorithm_is_scrypt(self):
        encoded = hashing.hash_password(SYNTHETIC)
        self.assertTrue(encoded.startswith("scrypt$"), encoded)
        self.assertEqual(len(encoded.split("$")), 4)

    def test_empty_password_is_refused(self):
        with self.assertRaises(ValueError):
            hashing.hash_password("")

    def test_salt_is_random_per_call(self):
        first = hashing.hash_password(SYNTHETIC)
        second = hashing.hash_password(SYNTHETIC)
        self.assertNotEqual(first, second)
        self.assertEqual(first.split("$")[1], second.split("$")[1])

    def test_parameters_are_encoded_so_future_verification_can_rehash(self):
        encoded = hashing.hash_password(SYNTHETIC, n=1 << 12)
        params = encoded.split("$")[1]
        self.assertIn("n=4096", params)

    def test_hash_material_is_not_the_plaintext(self):
        encoded = hashing.hash_password(SYNTHETIC)
        self.assertNotIn(SYNTHETIC, encoded)


class VerifyPasswordTests(unittest.TestCase):
    def test_correct_password_verifies(self):
        encoded = hashing.hash_password(SYNTHETIC)
        self.assertTrue(hashing.verify_password(SYNTHETIC, encoded))

    def test_wrong_password_does_not_verify(self):
        encoded = hashing.hash_password(SYNTHETIC)
        self.assertFalse(hashing.verify_password(SYNTHETIC + "x", encoded))

    def test_tampered_digest_does_not_verify(self):
        encoded = hashing.hash_password(SYNTHETIC)
        head, params, salt, digest = encoded.split("$")
        flipped = "A" if digest[0] != "A" else "B"
        tampered = "$".join([head, params, salt, flipped + digest[1:]])
        self.assertFalse(hashing.verify_password(SYNTHETIC, tampered))

    def test_malformed_encoding_returns_false_without_raising(self):
        for bad in ("", "not-a-hash", "scrypt$n=16384,r=8,p=1", "scrypt$a=b$!!$??"):
            with self.subTest(bad=bad):
                self.assertFalse(hashing.verify_password(SYNTHETIC, bad))

    def test_empty_candidate_does_not_verify(self):
        encoded = hashing.hash_password(SYNTHETIC)
        self.assertFalse(hashing.verify_password("", encoded))

    def test_verify_uses_constant_time_comparison(self):
        """compare_digest must be used, not '==' on the digest."""
        encoded = hashing.hash_password(SYNTHETIC)
        calls = []
        real = hashing.hmac.compare_digest

        def spy(left, right):
            calls.append((left, right))
            return real(left, right)

        hashing.hmac.compare_digest = spy
        try:
            hashing.verify_password(SYNTHETIC, encoded)
        finally:
            hashing.hmac.compare_digest = real
        self.assertTrue(calls, "verify_password did not use hmac.compare_digest")


class HashShapeTests(unittest.TestCase):
    def test_is_well_formed_accepts_generated_hash(self):
        self.assertTrue(hashing.is_well_formed(hashing.hash_password(SYNTHETIC)))

    def test_is_well_formed_rejects_junk(self):
        self.assertFalse(hashing.is_well_formed(""))
        self.assertFalse(hashing.is_well_formed("scrypt$$$"))

    def test_needs_rehash_true_for_weaker_params(self):
        weak = hashing.hash_password(SYNTHETIC, n=2 ** 10)
        self.assertTrue(hashing.hash_needs_rehash(weak))

    def test_needs_rehash_false_for_current_params(self):
        current = hashing.hash_password(SYNTHETIC)
        self.assertFalse(hashing.hash_needs_rehash(current))


class Argon2Tests(unittest.TestCase):
    @unittest.skipUnless(hashing.argon2_available(), "argon2-cffi not installed")
    def test_argon2id_round_trip(self):
        encoded = hashing.hash_password(SYNTHETIC, algorithm="argon2id")
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(hashing.verify_password(SYNTHETIC, encoded))
        self.assertFalse(hashing.verify_password(SYNTHETIC + "x", encoded))

    def test_unknown_algorithm_is_refused(self):
        with self.assertRaises(ValueError):
            hashing.hash_password(SYNTHETIC, algorithm="md5")


if __name__ == "__main__":
    unittest.main()
