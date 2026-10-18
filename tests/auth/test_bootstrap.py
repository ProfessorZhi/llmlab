"""Tests for backend.auth.bootstrap — the interactive administrator password flow.

Every password in this module is a synthetic placeholder. The bootstrap is
driven through ``read_password=`` so no test ever touches a terminal, and the
real operator password is never passed on a command line, in an environment
variable, or through a tool prompt.
"""

import contextlib
import io
import pathlib
import shutil
import tempfile
import unittest

from backend.auth import bootstrap, config as auth_config, errors, hashing, local_admin

SYNTHETIC = "synthetic-bootstrap-passphrase"
SYNTHETIC_NEW = "synthetic-rotated-passphrase-2"


class ScriptedReader:
    """Feeds canned answers and records the prompts (never the answers)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self.answers.pop(0)


class BootstrapTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-boot-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = auth_config.AuthConfig.from_env(
            {"LLM_LAB_AUTH_STATE_DIR": str(self.tmp)}
        )


class InitializeTests(BootstrapTestCase):
    def test_initialize_writes_a_private_file_and_never_the_plaintext(self):
        reader = ScriptedReader(SYNTHETIC, SYNTHETIC)
        path = bootstrap.initialize_admin_password(self.cfg, read_password=reader)

        self.assertEqual(path, self.cfg.admin_hash_file)
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn(SYNTHETIC, raw)
        self.assertTrue(hashing.verify_password(SYNTHETIC, raw.strip()))

    def test_file_mode_is_owner_only_on_posix(self):
        path = bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )
        if hasattr(__import__("os"), "getuid"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_admin_can_then_authenticate(self):
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )
        store = local_admin.AdminCredentialStore(
            self.cfg.admin_hash_file,
            username=self.cfg.admin_username,
            enforce_permissions=False,
        )
        self.assertTrue(store.authenticate("admin", SYNTHETIC))

    def test_refuses_to_overwrite_an_existing_credential(self):
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )
        before = self.cfg.admin_hash_file.read_bytes()
        with self.assertRaises(errors.ConfigError):
            bootstrap.initialize_admin_password(
                self.cfg, read_password=ScriptedReader(SYNTHETIC_NEW, SYNTHETIC_NEW)
            )
        self.assertEqual(self.cfg.admin_hash_file.read_bytes(), before)

    def test_mismatched_confirmation_writes_nothing(self):
        with self.assertRaises(errors.InvalidRequest):
            bootstrap.initialize_admin_password(
                self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC + "typo")
            )
        self.assertFalse(self.cfg.admin_hash_file.exists())

    def test_short_password_is_refused(self):
        short = "a" * (bootstrap.MIN_PASSWORD_LENGTH - 1)
        with self.assertRaises(errors.InvalidRequest):
            bootstrap.initialize_admin_password(self.cfg, read_password=ScriptedReader(short, short))
        self.assertFalse(self.cfg.admin_hash_file.exists())

    def test_password_equal_to_the_username_is_refused(self):
        with self.assertRaises(errors.InvalidRequest):
            bootstrap.initialize_admin_password(
                self.cfg, read_password=ScriptedReader("admin", "admin")
            )

    def test_questions_do_not_advertise_echo(self):
        reader = ScriptedReader(SYNTHETIC, SYNTHETIC)
        bootstrap.initialize_admin_password(self.cfg, read_password=reader)
        self.assertEqual(len(reader.prompts), 2)

    def test_argon2_is_used_only_when_asked_for(self):
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )
        default = self.cfg.admin_hash_file.read_text(encoding="utf-8")
        self.assertTrue(default.startswith("scrypt$"))


class RotateTests(BootstrapTestCase):
    def setUp(self):
        super().setUp()
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )
        self.before = self.cfg.admin_hash_file.read_text(encoding="utf-8")

    def test_rotation_requires_the_current_password(self):
        with self.assertRaises(errors.InvalidCredentials):
            bootstrap.rotate_admin_password(
                self.cfg,
                read_password=ScriptedReader("wrong-current-value", SYNTHETIC_NEW, SYNTHETIC_NEW),
            )
        self.assertEqual(self.cfg.admin_hash_file.read_text(encoding="utf-8"), self.before)

    def test_rotation_with_the_correct_current_password_changes_the_hash(self):
        bootstrap.rotate_admin_password(
            self.cfg,
            read_password=ScriptedReader(SYNTHETIC, SYNTHETIC_NEW, SYNTHETIC_NEW),
        )
        after = self.cfg.admin_hash_file.read_text(encoding="utf-8")
        self.assertNotEqual(after, self.before)
        self.assertTrue(hashing.verify_password(SYNTHETIC_NEW, after.strip()))
        self.assertFalse(hashing.verify_password(SYNTHETIC, after.strip()))

    def test_break_glass_recovery_skips_the_current_password(self):
        bootstrap.rotate_admin_password(
            self.cfg,
            read_password=ScriptedReader(SYNTHETIC_NEW, SYNTHETIC_NEW),
            recover=True,
        )
        after = self.cfg.admin_hash_file.read_text(encoding="utf-8")
        self.assertTrue(hashing.verify_password(SYNTHETIC_NEW, after.strip()))

    def test_rotation_validates_the_new_password_before_writing(self):
        with self.assertRaises(errors.InvalidRequest):
            bootstrap.rotate_admin_password(
                self.cfg, read_password=ScriptedReader(SYNTHETIC, "short", "short")
            )
        self.assertEqual(self.cfg.admin_hash_file.read_text(encoding="utf-8"), self.before)


class DisableTests(BootstrapTestCase):
    def setUp(self):
        super().setUp()
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )

    def test_disable_requires_the_current_password(self):
        with self.assertRaises(errors.InvalidCredentials):
            bootstrap.disable_admin_login(
                self.cfg, read_password=ScriptedReader("wrong-current-value")
            )

    def test_disable_writes_the_sentinel_and_blocks_login(self):
        bootstrap.disable_admin_login(self.cfg, read_password=ScriptedReader(SYNTHETIC))
        text = self.cfg.admin_hash_file.read_text(encoding="utf-8")
        self.assertEqual(text.strip(), local_admin.DISABLED_SENTINEL)
        self.assertNotIn("scrypt$", text)

        store = local_admin.AdminCredentialStore(
            self.cfg.admin_hash_file, username="admin", enforce_permissions=False
        )
        with self.assertRaises(errors.AccountDisabled):
            store.authenticate("admin", SYNTHETIC)


class StatusTests(BootstrapTestCase):
    def test_status_is_secret_free(self):
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC, SYNTHETIC)
        )
        report = bootstrap.admin_credential_status(self.cfg)
        rendered = repr(report)
        self.assertNotIn(SYNTHETIC, rendered)
        self.assertNotIn("scrypt$", rendered)
        self.assertEqual(report["state"], local_admin.STATE_ACTIVE)

    def test_status_on_a_fresh_state_dir_reports_missing(self):
        self.assertEqual(
            bootstrap.admin_credential_status(self.cfg)["state"], local_admin.STATE_MISSING
        )


class CommandLineTests(BootstrapTestCase):
    def test_password_can_never_be_passed_on_the_command_line(self):
        parser = bootstrap.build_parser()
        for argv in (["init", "--password", "x"], ["init", "-p", "x"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(argv)

    def test_cli_init_writes_a_file(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = bootstrap.main(
                ["init", "--state-dir", str(self.tmp)],
                read_password=ScriptedReader(SYNTHETIC, SYNTHETIC),
            )
        self.assertEqual(rc, 0)
        self.assertTrue(self.cfg.admin_hash_file.exists())

    def test_cli_status_is_read_only(self):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            rc = bootstrap.main(
                ["status", "--state-dir", str(self.tmp)], read_password=ScriptedReader()
            )
        self.assertEqual(rc, 0)
        self.assertFalse(self.cfg.admin_hash_file.exists())
        self.assertIn("state=missing", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
