"""身份与授权：role 不产生权限；拒绝是**统一**的，不留存在性预言机。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import (  # noqa: E402
    ENDPOINT_PRIVATE_C,
    ENDPOINT_RETIRED,
    ENDPOINT_SHARED_A,
    ENDPOINT_SHARED_B,
    UID_ALICE,
    UID_BOB,
    UID_CAROL,
    admin_principal,
    host_only_principal,
    office_principal,
    principal,
    registry,
    trusted_directory,
)

from backend.observability import (  # noqa: E402
    SCOPE_SELF_READ,
    AccessDenied,
    GatewayKeyBinding,
    InvalidRequest,
    KeyDirectory,
    RequesterPrincipal,
    attributed_keys,
    resolve_observer_scope,
    scope_allows_uid,
)


class KeyDirectoryTest(unittest.TestCase):
    def test_ownership_comes_only_from_the_directory(self) -> None:
        directory = trusted_directory()
        self.assertEqual(
            directory.owned_by(UID_ALICE), ("key-alpha", "key-orphan")
        )
        self.assertEqual(directory.owned_by(UID_CAROL), ("key-gamma",))
        self.assertEqual(directory.owned_by("u-nobody"), ())

    def test_lookup_is_exact_and_unknown_keys_are_none(self) -> None:
        directory = trusted_directory()
        self.assertIsNotNone(directory.lookup("key-alpha"))
        self.assertIsNone(directory.lookup("key-alpha "))  # 不做清洗/模糊匹配
        self.assertIsNone(directory.lookup("unknown-key-zzz"))

    def test_duplicate_key_ids_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            KeyDirectory(
                [
                    GatewayKeyBinding("key-a", UID_ALICE, "proj-a"),
                    GatewayKeyBinding("key-a", UID_BOB, "proj-a"),
                ]
            )

    def test_bindings_require_a_uid(self) -> None:
        with self.assertRaises(InvalidRequest):
            KeyDirectory([GatewayKeyBinding("key-a", "", "proj-a")])

    def test_empty_directory_is_allowed_and_has_no_uids(self) -> None:
        directory = KeyDirectory([])
        self.assertEqual(len(directory), 0)
        self.assertEqual(directory.uids(), ())


class ScopeResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoints = registry().endpoints()

    def _scope(self, ctx, **kwargs):
        return resolve_observer_scope(ctx, available_endpoints=self.endpoints, **kwargs)

    def test_self_scope_is_the_floor(self) -> None:
        ctx = RequesterPrincipal(UID_ALICE, frozenset(), frozenset(), frozenset())
        with self.assertRaises(AccessDenied):
            self._scope(ctx)

    def test_empty_uid_cannot_even_be_constructed(self) -> None:
        """没有 uid 的主体根本表示不出来 —— 构造期就拒绝，不给下游留「空身份」状态。"""
        for bad in ("", "   "):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(InvalidRequest):
                    RequesterPrincipal(bad, frozenset(), frozenset({SCOPE_SELF_READ}), frozenset())

    def test_resolver_still_guards_a_blank_uid_defensively(self) -> None:
        """即便有人绕过构造期检查（例如 ``object.__new__``），解析器也必须拒绝。"""
        forged = object.__new__(RequesterPrincipal)
        object.__setattr__(forged, "uid", "   ")
        object.__setattr__(forged, "roles", frozenset())
        object.__setattr__(forged, "scopes", frozenset({SCOPE_SELF_READ}))
        object.__setattr__(forged, "authorized_endpoints", frozenset())
        with self.assertRaises(AccessDenied):
            self._scope(forged)

    def test_role_admin_grants_nothing_by_itself(self) -> None:
        """``roles`` 里有 admin / owner / superuser，但没有管理 scope —— 一律不放行。"""
        ctx = office_principal()
        scope = self._scope(ctx)
        self.assertFalse(scope.admin)
        self.assertFalse(scope.host_visible)
        self.assertEqual(scope.endpoints, (ENDPOINT_SHARED_A,))
        with self.assertRaises(AccessDenied):
            self._scope(ctx, requested_uid=UID_BOB)

    def test_authorized_endpoints_are_intersected_with_the_registry(self) -> None:
        """授权白名单里那些**登记表里不存在**的端点不会出现。

        「被授权」不等于「存在于当前登记表」：前者是审批记录，后者是现实。
        取交集，免得一个已下架的名字靠白名单残留继续出现在响应里。
        """
        ctx = principal(
            endpoints=(ENDPOINT_SHARED_A, "model-never-registered", "model-also-missing")
        )
        scope = self._scope(ctx)
        self.assertEqual(scope.endpoints, (ENDPOINT_SHARED_A,))

    def test_a_registered_but_unauthorized_endpoint_is_not_visible(self) -> None:
        """登记表里有、但本人没被授权（``model-retired``）—— 不可见。"""
        ctx = principal(endpoints=(ENDPOINT_SHARED_A,))
        scope = self._scope(ctx)
        self.assertNotIn(ENDPOINT_RETIRED, scope.endpoints)

    def test_requested_uid_mismatch_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self._scope(principal(UID_ALICE), requested_uid=UID_BOB)

    def test_requested_uid_matching_self_is_fine(self) -> None:
        scope = self._scope(principal(UID_ALICE), requested_uid=UID_ALICE)
        self.assertTrue(scope.is_self_view)

    def test_denial_does_not_distinguish_existing_from_nonexistent_uids(self) -> None:
        """同一个拒绝兜住两种成因 —— 否则端点就是可枚举他人 uid 的预言机。"""
        ctx = principal(UID_ALICE)
        messages = set()
        for candidate in (UID_BOB, UID_CAROL, "u-does-not-exist", ""):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AccessDenied) as caught:
                    self._scope(ctx, requested_uid=candidate)
                messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1, messages)

    def test_admin_can_view_another_uid(self) -> None:
        scope = self._scope(admin_principal(), requested_uid=UID_CAROL)
        self.assertTrue(scope.admin)
        self.assertEqual(scope.viewing_uid, UID_CAROL)
        self.assertFalse(scope.is_self_view)

    def test_admin_sees_the_whole_registry(self) -> None:
        scope = self._scope(admin_principal())
        self.assertEqual(
            scope.endpoints,
            (ENDPOINT_PRIVATE_C, ENDPOINT_RETIRED, ENDPOINT_SHARED_A, ENDPOINT_SHARED_B),
        )

    def test_host_scope_alone_does_not_grant_admin(self) -> None:
        scope = self._scope(host_only_principal())
        self.assertFalse(scope.admin)
        self.assertTrue(scope.host_visible)
        with self.assertRaises(AccessDenied):
            self._scope(host_only_principal(), requested_uid=UID_BOB)

    def test_requested_endpoint_must_be_visible(self) -> None:
        ctx = principal(endpoints=(ENDPOINT_SHARED_A,))
        with self.assertRaises(AccessDenied):
            self._scope(ctx, requested_endpoint=ENDPOINT_SHARED_B)

    def test_endpoint_denial_does_not_distinguish_registered_from_unregistered(self) -> None:
        ctx = principal(endpoints=(ENDPOINT_SHARED_A,))
        messages = set()
        for candidate in (ENDPOINT_SHARED_B, ENDPOINT_PRIVATE_C, "model-nope"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AccessDenied) as caught:
                    self._scope(ctx, requested_endpoint=candidate)
                messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1, messages)

    def test_requested_endpoint_narrows_the_selected_set(self) -> None:
        ctx = principal(endpoints=(ENDPOINT_SHARED_A, ENDPOINT_SHARED_B))
        scope = self._scope(ctx, requested_endpoint=ENDPOINT_SHARED_B)
        self.assertEqual(scope.endpoints, (ENDPOINT_SHARED_B,))
        self.assertEqual(scope.requested_endpoint, ENDPOINT_SHARED_B)

    def test_scope_block_is_json_ready(self) -> None:
        import json

        payload = self._scope(principal()).to_dict()
        json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["uid"], UID_ALICE)
        self.assertEqual(payload["viewing_uid"], UID_ALICE)
        self.assertTrue(payload["is_self_view"])

    def test_non_requester_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            resolve_observer_scope("not-a-principal", available_endpoints=self.endpoints)


class AttributedKeysTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = trusted_directory()
        self.endpoints = registry().endpoints()

    def test_keys_are_owned_only_by_the_viewing_uid(self) -> None:
        scope = resolve_observer_scope(
            principal(UID_ALICE), available_endpoints=self.endpoints
        )
        self.assertEqual(attributed_keys(scope, self.directory), ("key-alpha", "key-orphan"))

    def test_revoked_keys_are_excluded_by_default_but_reported_on_request(self) -> None:
        scope = resolve_observer_scope(
            admin_principal(), available_endpoints=self.endpoints, requested_uid="u-3001"
        )
        self.assertEqual(attributed_keys(scope, self.directory), ())
        self.assertEqual(
            attributed_keys(scope, self.directory, include_revoked=True), ("key-revoked",)
        )

    def test_unknown_uid_yields_no_keys(self) -> None:
        scope = resolve_observer_scope(
            admin_principal(), available_endpoints=self.endpoints, requested_uid="u-nobody"
        )
        self.assertEqual(attributed_keys(scope, self.directory), ())


class ScopeAllowsUidTest(unittest.TestCase):
    def test_self_always_allowed(self) -> None:
        self.assertTrue(scope_allows_uid(principal(UID_ALICE), UID_ALICE))

    def test_other_uid_needs_admin(self) -> None:
        self.assertFalse(scope_allows_uid(principal(UID_ALICE), UID_BOB))
        self.assertTrue(scope_allows_uid(admin_principal(), UID_BOB))

    def test_role_admin_does_not_allow_others(self) -> None:
        self.assertFalse(scope_allows_uid(office_principal(), UID_BOB))


if __name__ == "__main__":
    unittest.main()
