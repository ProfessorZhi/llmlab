"""授权负测：身份、scope、项目白名单。**失败必须抛 AccessDenied，不得回退 admin。**"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import SYNTHETIC, atlas_request, load_fixture, requester, trusted_directory  # noqa: E402

from backend.atlas import (  # noqa: E402
    SCOPE_USAGE_READ,
    SCOPE_USAGE_UNATTRIBUTED,
    AccessDenied,
    build_atlas_dto,
)


class AuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = load_fixture("gateway_sample.jsonl")
        self.directory = trusted_directory()

    def _build(self, ctx, **kwargs):
        request = atlas_request(ctx, **kwargs)
        return build_atlas_dto(request, self.text, self.directory, SYNTHETIC)

    # ---- 拒绝路径 -------------------------------------------------------

    def test_missing_read_scope_is_denied(self) -> None:
        ctx = requester(scopes=("atlas:usage:unattributed",))
        with self.assertRaises(AccessDenied):
            self._build(ctx)

    def test_empty_scopes_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self._build(requester(scopes=()))

    def test_blank_subject_is_denied(self) -> None:
        for subject in ("", "   "):
            with self.subTest(subject=repr(subject)):
                with self.assertRaises(AccessDenied):
                    self._build(requester(subject))

    def test_admin_role_is_not_a_fallback(self) -> None:
        """role=admin 但不持 scope、无项目白名单 —— 仍然拒绝。"""
        ctx = requester(roles=("admin",), scopes=(), projects=())
        with self.assertRaises(AccessDenied):
            self._build(ctx, requested_project=None)

    def test_empty_project_whitelist_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self._build(requester(projects=()))

    def test_wrong_requested_subject_is_denied_not_downgraded(self) -> None:
        """客户端自报 subject 与服务端不一致：拒绝，而不是改用别人的身份。"""
        ctx = requester("u-1001", projects=("proj-a",), scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_UNATTRIBUTED))
        with self.assertRaises(AccessDenied):
            self._build(ctx, requested_subject="u-2001")

    def test_cross_project_request_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self._build(requester(projects=("proj-a",)), requested_project="proj-b")

    def test_unattributed_scope_does_not_grant_foreign_project(self) -> None:
        """有 unattributed scope 也不等于能读别人的项目。"""
        ctx = requester(projects=("proj-a",), scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_UNATTRIBUTED))
        with self.assertRaises(AccessDenied):
            self._build(ctx, requested_project="proj-b")

    # ---- 通过路径 -------------------------------------------------------

    def test_matching_requested_subject_is_allowed(self) -> None:
        dto = self._build(requester("u-1001"), requested_subject="u-1001")
        self.assertEqual(dto["scope"]["subject"], "u-1001")

    def test_all_authorized_projects_when_project_not_specified(self) -> None:
        ctx = requester(projects=("proj-a", "proj-b"))
        dto = self._build(ctx, requested_project=None)
        self.assertEqual(dto["scope"]["projects"], ["proj-a", "proj-b"])
        self.assertTrue(dto["scope"]["cross_project"])

    def test_single_project_scope_is_not_cross_project(self) -> None:
        dto = self._build(requester(projects=("proj-a",)))
        self.assertFalse(dto["scope"]["cross_project"])

    def test_unattributed_visibility_follows_scope_only(self) -> None:
        without = self._build(requester(scopes=(SCOPE_USAGE_READ,)))
        with_scope = self._build(
            requester(scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_UNATTRIBUTED))
        )
        self.assertFalse(without["unattributed"]["visible"])
        self.assertIsNone(without["unattributed"]["project_calls"])
        self.assertTrue(with_scope["unattributed"]["visible"])
        self.assertEqual(with_scope["unattributed"]["project_calls"], 2)


if __name__ == "__main__":
    unittest.main()
