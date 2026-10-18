"""The operator CLI — where the shared password is created.

The password reaches this program through one channel only: an interactive
prompt. These tests drive it with an injected reader, which is also the proof
that the channel is a parameter rather than something the program fishes out of
the environment or ``argv``.
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from backend.auth import hashing
from backend.bff import admin_mode as am
from tests.auth_linux.support import FakeClock

from . import support

NEW = support.SHARED_PASSWORD
OTHER = support.OTHER_SHARED_PASSWORD

#: The CLI writes to the terminal it was run at, which is correct for an
#: operator tool and noise in a test run. Captured for the whole module; the
#: individual tests that assert on output redirect again into their own buffer.
_REAL_STDOUT = None
_SWALLOWED = None


def setUpModule() -> None:
    global _REAL_STDOUT, _SWALLOWED
    _REAL_STDOUT = sys.stdout
    _SWALLOWED = io.StringIO()
    sys.stdout = _SWALLOWED


def tearDownModule() -> None:
    sys.stdout = _REAL_STDOUT
    assert _SWALLOWED is not None  # noqa: S101 - symmetry check


def _capture():
    return contextlib.redirect_stdout(io.StringIO())


class Reader:
    """A stand-in for the interactive prompt, recording what was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self.answers.pop(0)


def credential_path(directory: str) -> Path:
    return Path(directory) / am.DEFAULT_CREDENTIAL_FILE_NAME


class InitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_writes_a_0600_credential_that_verifies(self) -> None:
        reader = Reader(NEW, NEW)
        self.assertEqual(am.main(["init", "--state-dir", self.dir], read_password=reader), 0)

        path = credential_path(self.dir)
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(reader.prompts, [am.NEW_PROMPT, am.CONFIRM_PROMPT])

        store = am.AdminCredentialStore(path)
        self.assertTrue(store.is_configured())
        self.assertTrue(store.verify(NEW))
        self.assertFalse(store.verify(OTHER))

    def test_the_written_hash_is_at_or_above_the_security_floor(self) -> None:
        am.main(["init", "--state-dir", self.dir], read_password=Reader(NEW, NEW))
        encoded = credential_path(self.dir).read_text(encoding="utf-8").strip()
        self.assertTrue(am.hash_meets_floor(encoded))
        self.assertFalse(hashing.hash_needs_rehash(encoded))

    def test_mismatched_confirmations_are_refused_and_nothing_is_written(self) -> None:
        code = am.main(["init", "--state-dir", self.dir], read_password=Reader(NEW, OTHER))
        self.assertEqual(code, 1)
        self.assertFalse(credential_path(self.dir).exists())

    def test_a_short_password_is_refused(self) -> None:
        for short in ("", "x", "short", "a" * (am.MIN_PASSWORD_LENGTH - 1)):
            with self.subTest(length=len(short)):
                with tempfile.TemporaryDirectory() as tmp:
                    code = am.main(["init", "--state-dir", tmp], read_password=Reader(short, short))
                    self.assertEqual(code, 1)
                    self.assertFalse(credential_path(tmp).exists())

    def test_a_blank_password_is_refused_even_when_long_enough(self) -> None:
        blank = " " * (am.MIN_PASSWORD_LENGTH + 4)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                am.main(["init", "--state-dir", tmp], read_password=Reader(blank, blank)), 1
            )
            self.assertFalse(credential_path(tmp).exists())

    def test_init_refuses_to_overwrite_an_existing_credential(self) -> None:
        am.main(["init", "--state-dir", self.dir], read_password=Reader(NEW, NEW))
        before = credential_path(self.dir).read_text(encoding="utf-8")
        self.assertEqual(
            am.main(["init", "--state-dir", self.dir], read_password=Reader(OTHER, OTHER)), 1
        )
        self.assertEqual(credential_path(self.dir).read_text(encoding="utf-8"), before)

    def test_force_overwrites(self) -> None:
        am.main(["init", "--state-dir", self.dir], read_password=Reader(NEW, NEW))
        self.assertEqual(
            am.main(
                ["init", "--state-dir", self.dir, "--force"], read_password=Reader(OTHER, OTHER)
            ),
            0,
        )
        store = am.AdminCredentialStore(credential_path(self.dir))
        self.assertTrue(store.verify(OTHER))
        self.assertFalse(store.verify(NEW))


class StatusTests(unittest.TestCase):
    def test_status_reports_state_without_the_hash(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            am.main(["init", "--state-dir", tmp], read_password=Reader(NEW, NEW))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = am.main(["status", "--state-dir", tmp], read_password=Reader())
            printed = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("active", printed)
        self.assertNotIn("$scrypt$", printed)
        self.assertNotIn(NEW, printed)

    def test_an_absent_credential_reports_missing(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = am.main(["status", "--state-dir", tmp], read_password=Reader())
            self.assertEqual(code, 0)
            self.assertIn("missing", buffer.getvalue())


class RotateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        am.main(["init", "--state-dir", self.dir], read_password=Reader(NEW, NEW))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rotate_requires_the_current_password(self) -> None:
        code = am.main(
            ["rotate", "--state-dir", self.dir], read_password=Reader(OTHER, OTHER, OTHER)
        )
        self.assertEqual(code, 1)
        self.assertTrue(am.AdminCredentialStore(credential_path(self.dir)).verify(NEW))

    def test_rotate_with_the_current_password_replaces_it(self) -> None:
        code = am.main(["rotate", "--state-dir", self.dir], read_password=Reader(NEW, OTHER, OTHER))
        self.assertEqual(code, 0)
        store = am.AdminCredentialStore(credential_path(self.dir))
        self.assertTrue(store.verify(OTHER))
        self.assertFalse(store.verify(NEW))

    def test_recover_is_the_documented_break_glass(self) -> None:
        code = am.main(
            ["rotate", "--state-dir", self.dir, "--recover"], read_password=Reader(OTHER, OTHER)
        )
        self.assertEqual(code, 0)
        self.assertTrue(am.AdminCredentialStore(credential_path(self.dir)).verify(OTHER))

    def test_rotation_revokes_outstanding_elevations(self) -> None:
        """A rotation that left sessions promoted would leave the old password in force."""
        clock = FakeClock()
        service = support.make_service(self.dir, clock=clock, max_failures=50)
        service.enter(
            session_id="s", principal="linux:h:4001", uid=4001, linux_user="alice",
            password=NEW, client_key="k",
        )
        self.assertTrue(service.is_elevated("s"))

        am.main(["rotate", "--state-dir", self.dir], read_password=Reader(NEW, OTHER, OTHER))

        self.assertEqual(service.store.count_elevations(), 0)
        self.assertFalse(service.is_elevated("s"))


class DisableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        am.main(["init", "--state-dir", self.dir], read_password=Reader(NEW, NEW))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_disable_writes_the_sentinel_and_revokes(self) -> None:
        clock = FakeClock()
        service = support.make_service(self.dir, clock=clock)
        service.enter(
            session_id="s", principal="linux:h:4001", uid=4001, linux_user="alice",
            password=NEW, client_key="k",
        )
        self.assertEqual(am.main(["disable", "--state-dir", self.dir], read_password=Reader()), 0)

        self.assertEqual(
            am.AdminCredentialStore(credential_path(self.dir)).status()["state"], am.STATE_DISABLED
        )
        self.assertEqual(service.store.count_elevations(), 0)
        self.assertFalse(service.is_elevated("s"))

    def test_disable_on_a_missing_credential_is_a_no_op_with_a_nonzero_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(am.main(["disable", "--state-dir", tmp], read_password=Reader()), 1)


class NoAmbientChannelTests(unittest.TestCase):
    """There is no way to supply the password other than the prompt."""

    def test_there_is_no_password_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (
                ["init", "--state-dir", tmp, "--password", NEW],
                ["rotate", "--state-dir", tmp, "--password", NEW],
                ["init", "--state-dir", tmp, "-p", NEW],
            ):
                with self.subTest(argv=argv):
                    with self.assertRaises(SystemExit):
                        am.main(argv, read_password=Reader(NEW, NEW))

    def test_the_environment_is_not_consulted_for_a_password(self) -> None:
        """A password in the environment would land in ``ps``-adjacent places."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LLM_LAB_ADMIN_MODE_PASSWORD"] = NEW
            try:
                # Nothing reads it: init still prompts, and refuses when the
                # prompt is not answered.
                with self.assertRaises(AssertionError):
                    am.main(["init", "--state-dir", tmp], read_password=Reader())
            finally:
                del os.environ["LLM_LAB_ADMIN_MODE_PASSWORD"]
            self.assertFalse(credential_path(tmp).exists())

    def test_the_reader_is_only_ever_asked_for_the_password_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reader = Reader(NEW, NEW)
            am.main(["init", "--state-dir", tmp], read_password=reader)
        self.assertEqual(set(reader.prompts), {am.NEW_PROMPT, am.CONFIRM_PROMPT})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
