"""Negative-first contract tests for independent platform authentication.

Nothing here touches a Linux account, PAM, SSH, a real password, or a service.
Every check is either a unit assertion or a pure WSGI-environ call.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from backend.platform_auth import passwords
from backend.platform_auth import service as svc
from backend.platform_auth import wsgi
from backend.platform_auth.errors import (
    AccountDisabled,
    CsrfRejected,
    Forbidden,
    InvalidCredentials,
    NotAuthenticated,
    RateLimited,
    StorageSecurityError,
    ValidationError,
)
from backend.platform_auth.store import Store

PACKAGE_DIR = pathlib.Path(svc.__file__).parent


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_service(tmp, **kwargs):
    clock = kwargs.pop("clock", Clock())
    store = Store(tmp)
    service = svc.PlatformAuthService(store=store, clock=clock, **kwargs)
    return service, clock


class BaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.service, self.clock = make_service(self.dir)
        self.addCleanup(self.service.store.close)
        self.alice = self.service.create_user(
            "alice", "correct-horse-battery", (svc.SCOPE_READ,), display_name="Alice"
        )
        self.bob = self.service.create_user(
            "bob", "another-long-secret", (svc.SCOPE_READ,), display_name="Bob"
        )

    def login(self, login, password="correct-horse-battery", **kwargs):
        return self.service.login(login, password, **kwargs)


class IdentityIsIndependentTest(BaseTest):
    def test_platform_ids_are_unguessable_and_distinct(self):
        self.assertNotEqual(self.alice["platform_user_id"], self.bob["platform_user_id"])
        for user in (self.alice, self.bob):
            self.assertTrue(user["platform_user_id"].startswith("pu_"))
            self.assertGreaterEqual(len(user["platform_user_id"]), 24)

    def test_principal_carries_no_linux_identity(self):
        result = self.login("alice")
        principal = result.principal
        self.assertFalse(hasattr(principal, "uid"))
        self.assertFalse(hasattr(principal, "linux_user"))
        self.assertTrue(principal.platform_user_id.startswith("pu_"))
        self.assertNotIn("linux", principal.platform_user_id.lower())

    def test_the_package_never_reads_linux_accounts(self):
        # Code-level markers only: the docstrings *say* this package never reads
        # Linux accounts, so scanning for the word "shadow" would flag the claim
        # itself. These are the imports and calls that would actually do it.
        banned = ("import pwd", "from pwd", "import spwd", "from spwd",
                  "subprocess", "os.getlogin", "getpwnam", "getspnam", "pam_")
        for path in PACKAGE_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                with self.subTest(file=path.name, needle=needle):
                    self.assertNotIn(needle, text)

    def test_login_name_is_not_an_os_account(self):
        # Creating a platform account with an arbitrary login must not require or
        # consult a Linux account of that name.
        user = self.service.create_user("no-such-linux-user-xyz", "long-enough-secret",
                                        (svc.SCOPE_READ,))
        self.assertTrue(user["platform_user_id"].startswith("pu_"))


class PasswordTest(BaseTest):
    def test_correct_password_authenticates(self):
        result = self.login("alice")
        self.assertTrue(result.principal.platform_user_id.startswith("pu_"))

    def test_wrong_password_and_unknown_login_are_indistinguishable(self):
        wrong = None
        unknown = None
        try:
            self.service.login("alice", "wrong-password-here")
        except InvalidCredentials as exc:
            wrong = exc.code
        try:
            self.service.login("nobody-here", "wrong-password-here")
        except InvalidCredentials as exc:
            unknown = exc.code
        self.assertEqual(wrong, unknown)
        self.assertEqual(wrong, "invalid_credentials")

    def test_an_unknown_login_still_pays_for_a_verification(self):
        with mock.patch.object(passwords, "burn_verification") as burn:
            with self.assertRaises(InvalidCredentials):
                self.service.login("ghost", "whatever-long")
        burn.assert_called_once()

    def test_empty_and_short_passwords_are_refused(self):
        for value in ("", "short", "        "):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.service.create_user("carol", value, (svc.SCOPE_READ,))

    def test_hashes_are_salted_and_self_describing(self):
        first = passwords.hash_password("identical-secret-value")
        second = passwords.hash_password("identical-secret-value")
        self.assertNotEqual(first, second, "a shared salt would be a defect")
        n, r, p, salt, digest = passwords.parse_hash(first)
        self.assertEqual((n, r, p), (passwords.SCRYPT_N, passwords.SCRYPT_R, passwords.SCRYPT_P))
        self.assertEqual(len(salt), passwords.SALT_BYTES)
        self.assertEqual(len(digest), 32)
        self.assertTrue(passwords.verify_password(first, "identical-secret-value"))
        self.assertFalse(passwords.verify_password(first, "identical-secret-valuE"))

    def test_the_plaintext_is_never_stored(self):
        row = self.service.store.user_by_login("alice")
        self.assertNotIn("correct-horse-battery", row["password_hash"])
        self.assertTrue(row["password_hash"].startswith("scrypt$"))


class ThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service, self.clock = make_service(self._tmp.name, max_failures=3, lockout=60)
        self.addCleanup(self.service.store.close)
        self.service.create_user("alice", "correct-horse-battery", (svc.SCOPE_READ,))

    def test_repeated_failures_lock_the_login(self):
        for _ in range(3):
            with self.assertRaises(InvalidCredentials):
                self.service.login("alice", "wrong-password")
        with self.assertRaises(RateLimited):
            self.service.login("alice", "correct-horse-battery")
        self.clock.advance(61)
        self.service.login("alice", "correct-horse-battery")

    def test_a_successful_login_clears_the_counter(self):
        with self.assertRaises(InvalidCredentials):
            self.service.login("alice", "wrong-password")
        self.service.login("alice", "correct-horse-battery")
        for _ in range(2):
            with self.assertRaises(InvalidCredentials):
                self.service.login("alice", "wrong-password")
        self.service.login("alice", "correct-horse-battery")


class SessionTest(BaseTest):
    def test_token_is_returned_once_and_only_its_digest_is_stored(self):
        result = self.login("alice")
        row = self.service.store.session_by_digest(hashlib.sha256(
            result.token.encode()).hexdigest())
        self.assertIsNotNone(row)
        self.assertNotEqual(row["token_digest"], result.token)
        self.assertGreaterEqual(len(result.token), 60)
        # The raw token must not appear anywhere in the database file.
        with open(self.service.store.path, "rb") as handle:
            self.assertNotIn(result.token.encode(), handle.read())

    def test_resolution_returns_the_same_platform_principal(self):
        result = self.login("alice")
        principal = self.service.resolve_session(result.token)
        self.assertEqual(principal.platform_user_id, self.alice["platform_user_id"])
        self.assertEqual(principal.session_id, hashlib.sha256(
            result.token.encode()).hexdigest())

    def test_expired_session_is_rejected(self):
        result = self.login("alice")
        self.clock.advance(svc.SESSION_TTL_SECONDS + 1)
        with self.assertRaises(NotAuthenticated):
            self.service.resolve_session(result.token)

    def test_idle_timeout_is_rejected(self):
        result = self.login("alice")
        self.clock.advance(svc.IDLE_TTL_SECONDS + 1)
        with self.assertRaises(NotAuthenticated):
            self.service.resolve_session(result.token)

    def test_a_clock_that_runs_backwards_invalidates_the_session(self):
        result = self.login("alice")
        self.clock.advance(-3600)
        with self.assertRaises(NotAuthenticated):
            self.service.resolve_session(result.token)

    def test_revocation_is_immediate(self):
        result = self.login("alice")
        self.service.revoke_session(result.token)
        with self.assertRaises(NotAuthenticated):
            self.service.resolve_session(result.token)

    def test_disabling_an_account_kills_live_sessions_and_blocks_login(self):
        result = self.login("alice")
        self.service.set_disabled(self.alice["platform_user_id"], True)
        with self.assertRaises(NotAuthenticated):
            self.service.resolve_session(result.token)
        with self.assertRaises(AccountDisabled):
            self.service.login("alice", "correct-horse-battery")

    def test_password_rotation_revokes_sessions_and_retires_the_old_secret(self):
        result = self.login("alice")
        self.service.set_password(self.alice["platform_user_id"], "a-brand-new-secret")
        with self.assertRaises(NotAuthenticated):
            self.service.resolve_session(result.token)
        with self.assertRaises(InvalidCredentials):
            self.service.login("alice", "correct-horse-battery")
        self.service.login("alice", "a-brand-new-secret")

    def test_sessions_are_bound_to_one_platform_user(self):
        a = self.login("alice")
        b = self.login("bob", "another-long-secret")
        pa = self.service.resolve_session(a.token)
        pb = self.service.resolve_session(b.token)
        self.assertNotEqual(pa.platform_user_id, pb.platform_user_id)
        self.assertNotEqual(pa.session_id, pb.session_id)


class WsgiGuardTest(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.service.store.set_disabled(self.bob["platform_user_id"], False)
        self.admin = self.service.create_user(
            "rootadmin", "admin-long-secret", (svc.SCOPE_READ, svc.SCOPE_ADMIN)
        )
        self.guard = wsgi.PlatformGuard(
            self.service, allowed_origins=("http://127.0.0.1:8000",)
        )

    def env(self, token=None, *, method="GET", csrf=None, origin=None, extra=None):
        environ = {"REQUEST_METHOD": method}
        if token:
            environ["HTTP_COOKIE"] = f"{wsgi.COOKIE_NAME}={token}"
        if csrf:
            environ["HTTP_X_PLATFORM_CSRF"] = csrf
        if origin:
            environ["HTTP_ORIGIN"] = origin
        if extra:
            environ.update(extra)
        return environ

    def test_no_cookie_is_401(self):
        with self.assertRaises(NotAuthenticated) as caught:
            self.guard.require(self.env())
        self.assertEqual(caught.exception.status, 401)

    def test_wrong_scope_is_403_not_401(self):
        result = self.login("alice")
        with self.assertRaises(Forbidden) as caught:
            self.guard.require(self.env(result.token), scope=svc.SCOPE_ADMIN)
        self.assertEqual(caught.exception.status, 403)

    def test_correct_scope_passes(self):
        result = self.login("rootadmin", "admin-long-secret")
        principal = self.guard.require(self.env(result.token), scope=svc.SCOPE_ADMIN)
        self.assertEqual(principal.platform_user_id, self.admin["platform_user_id"])

    def test_mutation_without_csrf_is_rejected(self):
        result = self.login("rootadmin", "admin-long-secret")
        with self.assertRaises(CsrfRejected):
            self.guard.require(self.env(result.token, method="POST",
                                        origin="http://127.0.0.1:8000"))

    def test_mutation_with_a_foreign_origin_is_rejected(self):
        result = self.login("rootadmin", "admin-long-secret")
        with self.assertRaises(CsrfRejected):
            self.guard.require(self.env(result.token, method="POST",
                                        csrf=result.csrf_token,
                                        origin="https://evil.example"))

    def test_mutation_with_matching_csrf_and_origin_passes(self):
        result = self.login("rootadmin", "admin-long-secret")
        principal = self.guard.require(self.env(
            result.token, method="POST", csrf=result.csrf_token,
            origin="http://127.0.0.1:8000",
        ))
        self.assertEqual(principal.platform_user_id, self.admin["platform_user_id"])

    def test_a_wrong_csrf_token_is_rejected(self):
        result = self.login("rootadmin", "admin-long-secret")
        with self.assertRaises(CsrfRejected):
            self.guard.require(self.env(result.token, method="POST",
                                        csrf="not-the-token",
                                        origin="http://127.0.0.1:8000"))

    def test_self_reported_identity_headers_are_ignored(self):
        result = self.login("alice")
        spoofed = self.env(result.token, extra={
            "HTTP_X_PLATFORM_USER": "rootadmin",
            "HTTP_X_PLATFORM_USER_ID": self.admin["platform_user_id"],
            "HTTP_X_USER_ID": "1",
            "HTTP_X_OWNER": "rootadmin",
            "HTTP_X_LINUX_UID": "0",
            "HTTP_X_REMOTE_USER": "root",
        })
        principal = self.guard.require(spoofed)
        self.assertEqual(principal.platform_user_id, self.alice["platform_user_id"])
        self.assertNotIn(svc.SCOPE_ADMIN, principal.scopes)
        with self.assertRaises(Forbidden):
            self.guard.require(spoofed, scope=svc.SCOPE_ADMIN)

    def test_an_admin_scope_cannot_be_obtained_by_uid_or_login_name(self):
        result = self.login("alice")
        principal = self.guard.require(self.env(result.token))
        self.assertEqual(principal.login_name, "alice")
        self.assertFalse(principal.has_scope(svc.SCOPE_ADMIN))

    def test_cookie_attributes_are_secure(self):
        cookie = wsgi.session_cookie("tok", max_age=60)
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            self.assertIn(attr, cookie)
        cleared = wsgi.clear_cookie()
        self.assertIn("Max-Age=0", cleared)


class StorageTest(unittest.TestCase):
    def test_database_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            os.makedirs(state, mode=0o700)
            store = Store(state)
            mode = stat.S_IMODE(os.stat(store.path).st_mode)
            self.assertEqual(mode, 0o600)
            store.close()

    def test_a_world_writable_state_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            os.makedirs(state, mode=0o700)
            os.chmod(state, 0o777)
            with self.assertRaises(StorageSecurityError):
                Store(state)

    def test_a_symlinked_state_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            real = os.path.join(root, "real")
            os.makedirs(real, mode=0o700)
            link = os.path.join(root, "link")
            os.symlink(real, link)
            with self.assertRaises(StorageSecurityError):
                Store(link)

    def test_a_symlinked_database_file_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            os.makedirs(state, mode=0o700)
            target = os.path.join(root, "elsewhere.sqlite3")
            open(target, "w").close()
            os.symlink(target, os.path.join(state, "platform_auth.sqlite3"))
            with self.assertRaises(StorageSecurityError):
                Store(state)

    def test_a_group_readable_database_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            os.makedirs(state, mode=0o700)
            db = os.path.join(state, "platform_auth.sqlite3")
            open(db, "w").close()
            os.chmod(db, 0o644)
            with self.assertRaises(StorageSecurityError):
                Store(state)


class AuditTest(BaseTest):
    def test_audit_holds_no_secret_and_no_login_failure_text(self):
        self.login("alice")
        try:
            self.service.login("alice", "wrong-password")
        except InvalidCredentials:
            pass
        with sqlite3.connect(self.service.store.path) as conn:
            rows = list(conn.execute("SELECT event, subject, detail FROM platform_audit"))
        rendered = json.dumps([list(row) for row in rows])
        self.assertNotIn("correct-horse-battery", rendered)
        self.assertNotIn("wrong-password", rendered)
        self.assertIn("platform_login_succeeded", rendered)
        self.assertIn("platform_login_failed", rendered)


class CliSafetyTest(unittest.TestCase):
    def test_the_cli_has_no_password_option(self):
        from backend.platform_auth import cli

        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--state-dir", "/tmp/x", "init-admin",
                               "--login", "a", "--password", "secret"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["--state-dir", "/tmp/x", "set-password",
                               "--user-id", "pu_x", "--password", "secret"])

    def test_the_cli_never_prints_a_password(self):
        from backend.platform_auth import cli

        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, "state")
            os.makedirs(state, mode=0o700)
            with mock.patch.object(cli.getpass, "getpass", side_effect=[
                "a-very-long-secret", "a-very-long-secret",
            ]):
                with mock.patch("sys.stdout") as out:
                    code = cli.main(["--state-dir", state, "init-admin", "--login", "admin"])
            self.assertEqual(code, 0)
            printed = "".join(str(call) for call in out.write.call_args_list)
            self.assertNotIn("a-very-long-secret", printed)


if __name__ == "__main__":
    unittest.main()
