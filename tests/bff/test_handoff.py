"""The Linux sign-in handoff: a session is minted, a credential is not kept.

``backend/auth_linux`` deliberately stops at ``VerifiedIdentity`` and says the
session must be issued by ``backend/auth``. This module checks that the BFF is
the implementation that does exactly that — no second session store, no second
cookie format — and that the four rules the handoff documents actually hold.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest

from backend.auth import store as auth_store
from backend.auth_linux import handoff as linux_handoff
from tests.bff import support as S


def _imported_modules(package_dir) -> set:
    """Every module imported by the files in ``package_dir``.

    Parsed rather than grepped: the two packages deliberately mention each other
    in prose, and a substring search cannot tell a comment from an import.
    """
    import ast
    import pathlib

    found = set()
    for path in sorted(pathlib.Path(package_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found.add(node.module)
                    if node.level:
                        found.add("." * node.level + node.module)
    return found


class ConstantAgreementTests(unittest.TestCase):
    def test_the_auth_source_string_is_spelled_identically_on_both_sides(self):
        """The string is duplicated on purpose; this is what stops it drifting.

        ``backend/auth`` may not import the Linux package — that package
        documents the dependency as protocol-only, and a transitive import would
        let either side change the other by accident.
        """
        self.assertEqual(auth_store.SOURCE_LINUX, linux_handoff.AUTH_SOURCE_LINUX)

        import pathlib

        auth_dir = pathlib.Path(auth_store.__file__).parent
        offenders = {
            name
            for name in _imported_modules(auth_dir)
            if "auth_linux" in name
        }
        self.assertEqual(offenders, set(), "the dependency direction was broken")

    def test_the_linux_package_does_not_import_the_portal_either(self):
        import pathlib

        from backend import auth_linux

        linux_dir = pathlib.Path(auth_linux.__file__).parent
        offenders = {
            name
            for name in _imported_modules(linux_dir)
            if name in {"backend.auth", ".auth", "..auth"} or name.startswith("backend.auth.")
        }
        self.assertEqual(offenders, set(), "the protocol-only boundary was broken")

    def test_the_role_names_agree(self):
        from backend.auth_linux import policy as linux_policy

        self.assertEqual(auth_store.ROLE_ADMIN, linux_policy.ROLE_ADMIN)
        self.assertEqual(auth_store.ROLE_MEMBER, linux_policy.ROLE_MEMBER)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_the_handoff_satisfies_the_declared_protocol(self):
        """A missing or renamed ``start_session`` would be caught here."""
        handoff = self.h.app.handoff
        self.assertTrue(hasattr(handoff, "start_session"))
        self.assertIn("draft", inspect.signature(handoff.start_session).parameters)

    def test_a_draft_produces_a_provisioned_user_and_a_session(self):
        draft = linux_handoff.UserDraft(
            user_id=linux_handoff.linux_user_id(S.SYNTHETIC_HOST_ID, 4003),
            auth_source=linux_handoff.AUTH_SOURCE_LINUX,
            external_id=f"{S.SYNTHETIC_HOST_ID}:4003",
            display_name="bob",
            role=auth_store.ROLE_MEMBER,
            host_id=S.SYNTHETIC_HOST_ID,
            uid=4003,
            login_name="bob",
        )
        receipt = self.h.app.handoff.start_session(draft)
        self.assertTrue(receipt.session_id)
        self.assertEqual(receipt.user.id, draft.user_id)
        self.assertEqual(receipt.user.role, auth_store.ROLE_MEMBER)
        self.assertEqual(receipt.user.auth_source, linux_handoff.AUTH_SOURCE_LINUX)

        # The session is the *existing* manager's, so the existing endpoint
        # resolves it without knowing anything about Linux.
        session = self.h.auth_app.service.current_session(receipt.session_id)
        self.assertEqual(session.user.id, draft.user_id)

    def test_a_returning_sign_in_cannot_promote_itself(self):
        """Rule 1: role and status are written on insert, never on conflict."""
        draft = linux_handoff.UserDraft(
            user_id=linux_handoff.linux_user_id(S.SYNTHETIC_HOST_ID, 4003),
            auth_source=linux_handoff.AUTH_SOURCE_LINUX,
            external_id=f"{S.SYNTHETIC_HOST_ID}:4003",
            display_name="bob",
            role=auth_store.ROLE_MEMBER,
            host_id=S.SYNTHETIC_HOST_ID,
            uid=4003,
            login_name="bob",
        )
        self.h.app.handoff.start_session(draft)

        # Same person, now claiming to be an administrator.
        from dataclasses import replace

        promoted = replace(draft, role=auth_store.ROLE_ADMIN, display_name="bob2")
        self.h.app.handoff.start_session(promoted)

        user = self.h.auth_app.service.db.get_user(draft.user_id)
        self.assertEqual(user.role, auth_store.ROLE_MEMBER, "a sign-in promoted itself")
        self.assertEqual(user.display_name, "bob2", "the display name may refresh")
        self.assertEqual(user.auth_source, linux_handoff.AUTH_SOURCE_LINUX)

    def test_a_returning_sign_in_cannot_re_enable_itself(self):
        """Rule 1, the other half: an operator's disable must stick."""
        draft = linux_handoff.UserDraft(
            user_id=linux_handoff.linux_user_id(S.SYNTHETIC_HOST_ID, 4003),
            auth_source=linux_handoff.AUTH_SOURCE_LINUX,
            external_id=f"{S.SYNTHETIC_HOST_ID}:4003",
            display_name="bob",
            role=auth_store.ROLE_MEMBER,
            host_id=S.SYNTHETIC_HOST_ID,
            uid=4003,
            login_name="bob",
        )
        self.h.app.handoff.start_session(draft)
        self.h.auth_app.service.db.set_user_status(
            draft.user_id, auth_store.STATUS_DISABLED
        )
        self.h.app.handoff.start_session(draft)
        self.assertEqual(
            self.h.auth_app.service.db.get_user(draft.user_id).status,
            auth_store.STATUS_DISABLED,
            "a sign-in re-enabled a disabled account",
        )

    def test_the_handoff_refuses_anything_that_is_not_a_draft(self):
        for bad in ({"user_id": "x"}, None, "draft"):
            with self.assertRaises(ValueError):
                self.h.app.handoff.start_session(bad)

    def test_no_credential_survives_the_handoff(self):
        """Rule 4: nothing derived from the broker's reply is stored."""
        self.h.client("alice")
        user = self.h.auth_app.service.db.get_user(self.h.principal_of("alice"))
        fields = {f for f in dir(user) if not f.startswith("_")}
        for forbidden in ("password", "password_hash", "secret", "credential"):
            self.assertNotIn(forbidden, fields)
        for value in (user.external_id, user.display_name, user.id, user.role):
            self.assertNotIn(S.GOOD_PASSWORD, str(value))
            self.assertNotIn(S.WRONG_PASSWORD, str(value))

    def test_the_users_table_has_no_credential_column(self):
        """A column that does not exist cannot be filled in later by accident."""
        import pathlib
        import sqlite3

        database = pathlib.Path(self.h.state_dir) / "auth.sqlite3"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }
        finally:
            connection.close()
        self.assertIn("id", columns, "the schema was not found where it was expected")
        for forbidden in ("password", "password_hash", "pwhash", "secret", "salt"):
            self.assertNotIn(forbidden, columns)


class CredentialHandlingTests(unittest.TestCase):
    """A password must not reach a log, an audit record, or a response."""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_the_password_reaches_the_pam_double_and_is_not_kept(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        S.login(client, "alice", S.GOOD_PASSWORD)
        # The double records what it was asked to check — that is how a test can
        # prove the supplied password is the one that was verified.
        self.assertEqual(self.h.pam.calls[-1], ("alice", S.GOOD_PASSWORD))
        # But the module keeps no copy: no attribute holds it after the call.
        for name in dir(self.h.pam):
            value = getattr(self.h.pam, name, None)
            if isinstance(value, str):
                self.assertNotEqual(value, S.GOOD_PASSWORD)

    def test_no_login_module_imports_logging(self):
        """Nothing can write a credential to a log if nothing logs."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "backend" / "auth_linux"
        for path in sorted(root.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import logging", source, path.name)
            self.assertNotIn("print(", source, path.name)

    def test_the_broker_request_renders_its_password_redacted(self):
        from backend.auth_linux.protocol import BrokerRequest

        request = BrokerRequest(user="alice", password=S.GOOD_PASSWORD, request_id="r1")
        self.assertIn("<redacted>", repr(request))
        self.assertNotIn(S.GOOD_PASSWORD, repr(request))
        self.assertNotIn(S.GOOD_PASSWORD, str(request))

    def test_an_audit_event_refuses_a_credential_shaped_field(self):
        from backend.auth_linux.audit import AuditEvent

        for key in ("password", "passwd", "secret", "token", "hash", "credential"):
            with self.assertRaises(ValueError, msg=key):
                AuditEvent(event="linux_login", outcome="ok", fields={key: "x"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
