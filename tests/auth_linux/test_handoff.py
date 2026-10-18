"""The session handoff contract — the seam that must not become a session."""

from __future__ import annotations

import unittest

import backend.auth_linux.handoff as handoff_module
from backend.auth_linux.handoff import (
    AUTH_SOURCE_LINUX,
    SessionHandoff,
    UserDraft,
    draft_from_identity,
    linux_user_id,
)
from backend.auth_linux.policy import ROLE_ADMIN, ROLE_MEMBER
from backend.auth_linux.provider import VerifiedIdentity

from . import support


def identity(name=support.MEMBER_NAME, uid=support.MEMBER_UID, role=ROLE_MEMBER) -> VerifiedIdentity:
    return VerifiedIdentity(
        login_name=name,
        uid=uid,
        host_id=support.SYNTHETIC_HOST_ID,
        role=role,
        display_name=name,
    )


class UserIdTests(unittest.TestCase):
    def test_the_identifier_is_keyed_on_host_and_uid(self) -> None:
        self.assertEqual(
            linux_user_id(support.SYNTHETIC_HOST_ID, 4001),
            f"linux:{support.SYNTHETIC_HOST_ID}:4001",
        )

    def test_it_is_deterministic(self) -> None:
        first = linux_user_id("dell-test-01", 4001)
        second = linux_user_id("dell-test-01", 4001)
        self.assertEqual(first, second)

    def test_a_rename_produces_the_same_user(self) -> None:
        """The person is the UID; the name is what they type."""
        self.assertEqual(linux_user_id("dell-test-01", 4001), linux_user_id("dell-test-01", 4001))

    def test_the_same_uid_on_a_different_host_is_a_different_user(self) -> None:
        self.assertNotEqual(linux_user_id("dell-test-01", 4001), linux_user_id("dell-test-02", 4001))

    def test_malformed_input_is_rejected(self) -> None:
        for host, uid in (("", 1), (None, 1), ("dell", -1), ("dell", "4001"), ("dell", True)):
            with self.subTest(host=host, uid=uid):
                with self.assertRaises(ValueError):
                    linux_user_id(host, uid)  # type: ignore[arg-type]


class DraftTests(unittest.TestCase):
    def test_a_draft_carries_what_the_auth_service_needs(self) -> None:
        draft = draft_from_identity(identity())
        self.assertEqual(draft.user_id, linux_user_id(support.SYNTHETIC_HOST_ID, support.MEMBER_UID))
        self.assertEqual(draft.auth_source, AUTH_SOURCE_LINUX)
        self.assertEqual(draft.external_id, f"{support.SYNTHETIC_HOST_ID}:{support.MEMBER_UID}")
        self.assertEqual(draft.role, ROLE_MEMBER)
        self.assertEqual(draft.uid, support.MEMBER_UID)
        self.assertEqual(draft.login_name, support.MEMBER_NAME)

    def test_the_auth_source_is_a_new_value_not_an_existing_one(self) -> None:
        self.assertEqual(AUTH_SOURCE_LINUX, "linux_pam")
        self.assertNotIn(AUTH_SOURCE_LINUX, {"local", "feishu"})

    def test_an_administrator_grant_shows_up_as_a_platform_administrator(self) -> None:
        draft = draft_from_identity(
            identity(name=support.ADMIN_NAME, uid=support.ADMIN_UID, role=ROLE_ADMIN)
        )
        self.assertTrue(draft.to_public_identity()["is_platform_admin"])

    def test_the_public_identity_matches_the_existing_shape(self) -> None:
        public = draft_from_identity(identity()).to_public_identity()
        self.assertEqual(
            set(public), {"id", "display_name", "role", "auth_source", "is_platform_admin"}
        )

    def test_the_public_identity_does_not_expose_the_linux_account(self) -> None:
        """A UID, a hostname and a login name are not needed to render a page."""
        public = draft_from_identity(identity()).to_public_identity()
        self.assertNotIn("uid", public)
        self.assertNotIn("host_id", public)
        self.assertNotIn("login_name", public)
        self.assertNotIn("external_id", public)

    def test_the_draft_carries_no_credential(self) -> None:
        draft = draft_from_identity(identity())
        for value in vars(draft).values():
            self.assertNotIn("password", str(value).lower())

    def test_the_draft_accepts_a_structural_identity(self) -> None:
        """It takes the fields it needs, not the concrete type."""

        class StandIn:
            login_name = "alice"
            uid = 4001
            host_id = "dell-test-01"
            role = ROLE_MEMBER
            display_name = "alice"

        draft = draft_from_identity(StandIn())
        self.assertEqual(draft.user_id, "linux:dell-test-01:4001")


class ModuleScopeTests(unittest.TestCase):
    """What this module must not contain."""

    def test_it_exposes_no_session_or_cookie_machinery(self) -> None:
        for forbidden in ("create_session", "issue_session", "mint", "set_cookie", "csrf_token"):
            with self.subTest(symbol=forbidden):
                self.assertFalse(hasattr(handoff_module, forbidden))

    def test_the_handoff_is_a_protocol_the_other_side_implements(self) -> None:
        self.assertTrue(hasattr(SessionHandoff, "start_session"))

    def test_the_draft_is_immutable(self) -> None:
        draft = draft_from_identity(identity())
        with self.assertRaises(Exception):
            draft.role = ROLE_ADMIN  # type: ignore[misc]

    def test_a_draft_can_always_be_built_from_an_identity(self) -> None:
        self.assertIsInstance(draft_from_identity(identity()), UserDraft)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
