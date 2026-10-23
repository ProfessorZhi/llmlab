"""身份与行级 ACL：注册、共享、撤销、管理员、以及「不可见 = 404」的整套边界。

这一组测试是本次交付的核心安全证据。它们**全部**走真实的 WSGI 路径
（``Harness.request`` 驱动 ``create_app`` 的产物），因此断言的是「端到端行为」，
而不是某个内部函数的返回值。
"""

from __future__ import annotations

import unittest

from backend.registry.identity import (
    Principal,
    PrincipalDirectory,
    TokenBinding,
    TokenDirectory,
    resolve_requester,
)
from backend.registry.models import (
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
)
from tests.registry.support import (
    ADMIN_READ_ONLY_SCOPES,
    HOST_USERS,
    NON_ADMIN_SCOPES,
    TOKEN_PREFIX,
    Harness,
    close_all,
)

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


class OwnerIsDerivedFromCredentialTest(unittest.TestCase):
    """owner 只能由服务端受信目录从**凭据**推出。"""

    def setUp(self) -> None:
        self.h = Harness()

    def test_owner_comes_from_the_credential_not_the_payload(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e1", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["owner"], "alice")
        self.assertEqual(payload["data"]["owner_uid"], 4001)

    def test_a_forged_owner_field_is_rejected_outright(self) -> None:
        # 不是「忽略这个字段」：静默忽略会让一次伪造尝试在日志里消失。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={
                "name": "e1",
                "target_url": "http://127.0.0.1:8000/v1",
                "owner": "carol",
            },
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "registry.identity_assertion_rejected")

    def test_every_identity_field_is_rejected(self) -> None:
        for field in ("owner", "uid", "principal", "role", "is_admin", "linux_user"):
            with self.subTest(field=field):
                status, _h, payload = self.h.request(
                    "POST",
                    f"{PREFIX}/models",
                    principal="alice",
                    body={"name": "m", field: "carol"},
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    payload["error"]["code"], "registry.identity_assertion_rejected"
                )

    def test_every_identity_header_is_rejected(self) -> None:
        for header in ("X-Owner", "X-Uid", "X-Role", "X-Principal", "X-Forwarded-User"):
            with self.subTest(header=header):
                status, _h, payload = self.h.request(
                    "GET", f"{PREFIX}/whoami", principal="alice", headers={header: "carol"}
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    payload["error"]["code"], "registry.identity_assertion_rejected"
                )

    def test_two_principals_may_use_the_same_name_without_colliding(self) -> None:
        self.h.make_engine("alice", "shared-name")
        self.h.make_engine("dave", "shared-name")
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/engines", principal="alice"
        )
        self.assertEqual(payload["data"]["count"], 1)
        self.assertEqual(payload["data"]["items"][0]["owner"], "alice")


class AuthenticationTest(unittest.TestCase):
    """没有凭据、凭据无效、凭据被撤销 —— 一律 401，且**不回退**到匿名。"""

    def setUp(self) -> None:
        self.h = Harness()

    def test_no_credential_is_401(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/whoami")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "registry.unauthenticated")

    def test_unknown_credential_is_401(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/whoami", token=f"{TOKEN_PREFIX}nobody"
        )
        self.assertEqual(status, 401)

    def test_malformed_authorization_header_is_401(self) -> None:
        for header in ("", "Bearer", "Basic dXNlcjpwYXNz", "Token abc", "Bearer "):
            with self.subTest(header=header):
                status, _h, _p = self.h.request(
                    "GET", f"{PREFIX}/whoami", extra_environ={"HTTP_AUTHORIZATION": header}
                )
                self.assertEqual(status, 401)

    def test_revoked_credential_is_401(self) -> None:
        bindings = [
            TokenBinding(f"{TOKEN_PREFIX}alice", "alice", NON_ADMIN_SCOPES, revoked=True)
        ]
        for name, uid in HOST_USERS:
            if name != "alice":
                bindings.append(TokenBinding(f"{TOKEN_PREFIX}{name}", name, NON_ADMIN_SCOPES))
        h = Harness(bindings=bindings)
        status, _h, _p = h.request("GET", f"{PREFIX}/whoami", principal="alice")
        self.assertEqual(status, 401)

    def test_suspended_principal_is_401(self) -> None:
        principals = PrincipalDirectory(
            [
                Principal("alice", "alice", 4001, state="suspended"),
                Principal("carol", "carol", 4000),
            ]
        )
        tokens = TokenDirectory([TokenBinding(f"{TOKEN_PREFIX}alice", "alice", NON_ADMIN_SCOPES)])
        with self.assertRaises(Exception) as ctx:
            resolve_requester(f"{TOKEN_PREFIX}alice", tokens, principals)
        self.assertEqual(getattr(ctx.exception, "contract_code", None), "registry.unauthenticated")

    def test_credential_resolving_to_unknown_principal_is_401(self) -> None:
        principals = PrincipalDirectory([Principal("carol", "carol", 4000)])
        tokens = TokenDirectory(
            [TokenBinding(f"{TOKEN_PREFIX}alice", "alice", NON_ADMIN_SCOPES)]
        )
        with self.assertRaises(Exception) as ctx:
            resolve_requester(f"{TOKEN_PREFIX}alice", tokens, principals)
        self.assertEqual(getattr(ctx.exception, "contract_code", None), "registry.unauthenticated")

    def test_a_duplicate_uid_in_the_directory_is_refused(self) -> None:
        with self.assertRaises(Exception):
            PrincipalDirectory(
                [Principal("a", "a", 4001), Principal("b", "b", 4001)]
            )

    def test_the_directory_can_answer_which_principal_owns_a_uid(self) -> None:
        """§4.1：自动接入必须能把一个监听端口归到某个账户。

        反向查找只认目录里**显式绑定**的 uid；没有绑定 uid 的账户（``account:<id>``
        这类跨机身份）永远查不到，它们没有本机进程可归属。
        """

        directory = PrincipalDirectory(
            [
                Principal("alice", "alice", 4001),
                Principal("bob", "bob", 4002),
                Principal("account:42", "team", None, kind="account"),
            ]
        )

        self.assertEqual(directory.by_uid(4001).principal, "alice")
        self.assertIsNone(directory.by_uid(9999))
        self.assertEqual(directory.uids(), frozenset({4001, 4002}))


class DefaultPrivateTest(unittest.TestCase):
    """默认私有：未共享的东西**连存在性**都不泄漏。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")

    def test_a_new_endpoint_is_private(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="alice"
        )
        self.assertEqual(payload["data"]["visibility"], VISIBILITY_PRIVATE)

    def test_creation_cannot_self_declare_a_visibility(self) -> None:
        """开放范围是一件**独立**的事：走 share/unshare，不走创建。

        这条拒绝路径此前没有任何测试 —— 而它是「新 Endpoint 不允许在创建请求里
        同时自报开放共享」这条不变量唯一被强制执行的地方。连 ``private`` 也拒：
        被拒的是这个**键**的存在，不是它的取值，否则「显式写 private」与「没写」
        会走上两条不同的代码路径，而只有一条被测试过。
        """
        deployment = self.h.make_ready_deployment("alice", "d")
        for value in (VISIBILITY_PRIVATE, VISIBILITY_USERS, VISIBILITY_ALL_APPROVED):
            with self.subTest(visibility=value):
                status, _h, payload = self.h.request(
                    "POST",
                    f"{PREFIX}/endpoints",
                    principal="alice",
                    body={
                        "name": "svc",
                        "deployment_id": deployment,
                        "visibility": value,
                    },
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["code"], "registry.invalid_request")
        # 同一份请求把 ``visibility`` 拿掉就能通过 —— 证明被拒的是那个键本身。
        # 换个名字：setUp 已经用 "svc" 建过一个，同名会被幂等/冲突检查拦下，
        # 那样这条对照就不再是「关于 visibility 的」了。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints",
            principal="alice",
            body={"name": "svc2", "deployment_id": deployment},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["visibility"], VISIBILITY_PRIVATE)

    def test_other_users_cannot_see_it(self) -> None:
        for who in ("dave", "bob"):
            with self.subTest(who=who):
                self.assertEqual(self.h.endpoints_visible_to(who), [])

    def test_the_backing_engine_model_and_deployment_are_invisible_too(self) -> None:
        for collection in ("engines", "models", "deployments"):
            with self.subTest(collection=collection):
                status, _h, payload = self.h.request(
                    "GET", f"{PREFIX}/{collection}", principal="dave"
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["count"], 0)

    def test_a_forbidden_object_and_a_missing_object_are_the_same_refusal(self) -> None:
        # 「他人未共享的」与「根本不存在的」必须给出同一个回答，
        # 否则端点就成了一个逐 id 枚举他人资源存在性的预言机。
        forbidden_status, _h, forbidden = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="dave"
        )
        missing_status, _h, missing = self.h.request(
            "GET", f"{PREFIX}/endpoints/ep_0000000000000000", principal="dave"
        )
        self.assertEqual(forbidden_status, missing_status)
        self.assertEqual(forbidden_status, 404)
        self.assertEqual(forbidden["error"]["code"], missing["error"]["code"])
        # 消息里会回显调用方**自己传进来的** id（否则没人能调试），
        # 但两个回答的**形状**必须一致：同为 registry.not_found + 同一句式。
        self.assertTrue(
            forbidden["error"]["message"].startswith("endpoint '")
            and missing["error"]["message"].startswith("endpoint '")
        )

    def test_the_refusal_does_not_reveal_which_of_several_ids_exist(self) -> None:
        # 逐个探：存在的（但不可见）与不存在的，回答必须逐字节同形，
        # 把 id 换成通配符之后必须完全一致。
        import re as _re

        shapes = []
        for candidate in (self.endpoint_id, "ep_0000000000000000", "ep_ffffffffffffffff"):
            status, _h, payload = self.h.request(
                "GET", f"{PREFIX}/endpoints/{candidate}", principal="dave"
            )
            self.assertEqual(status, 404)
            shapes.append(
                _re.sub(r"'[^']*'", "'<id>'", payload["error"]["message"])
            )
        self.assertEqual(len(set(shapes)), 1, shapes)

    def test_admin_global_read_does_see_it(self) -> None:
        self.assertEqual(len(self.h.endpoints_visible_to("carol")), 1)


class SharingTest(unittest.TestCase):
    """共享：显式、可撤销、立即生效、不传递、不越权。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")

    def _share(self, body: dict, principal: str = "alice"):
        return self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal=principal,
            body=body,
        )

    def test_sharing_to_one_user_reaches_only_that_user(self) -> None:
        status, _h, _p = self._share({"mode": "users", "grantees": ["dave"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)
        self.assertEqual(self.h.endpoints_visible_to("bob"), [])

    def test_sharing_to_all_approved_reaches_every_approved_principal(self) -> None:
        status, _h, _p = self._share({"mode": "all_approved_platform_users"})
        self.assertEqual(status, 200)
        for who in ("dave", "bob"):
            with self.subTest(who=who):
                self.assertEqual(len(self.h.endpoints_visible_to(who)), 1)

    def test_all_approved_excludes_suspended_principals(self) -> None:
        principals = PrincipalDirectory(
            [
                Principal("alice", "alice", 4001),
                Principal("dave", "dave", 4002),
                Principal("bob", "bob", 4003, state="suspended"),
                Principal("carol", "carol", 4000),
            ]
        )
        # 「已审批成员」的唯一来源是受信目录。
        self.assertEqual(principals.approved_principals(), frozenset({"alice", "dave", "carol"}))

        h = Harness(principals=principals)
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, _p = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "all_approved_platform_users"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(h.endpoints_visible_to("dave")), 1)
        # 被停用的主体比「不在名单里」更早被挡住：它连认证都过不了。
        # 这比「能登录但看不到」更强 —— 停用是一个身份层面的状态。
        status, _h, payload = h.request("GET", f"{PREFIX}/whoami", principal="bob")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "registry.unauthenticated")

    def test_a_suspended_principal_cannot_be_named_as_a_grantee(self) -> None:
        principals = PrincipalDirectory(
            [
                Principal("alice", "alice", 4001),
                Principal("bob", "bob", 4003, state="suspended"),
                Principal("carol", "carol", 4000),
            ]
        )
        h = Harness(principals=principals)
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["bob"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "registry.invalid_request")

    def test_revoking_is_immediate(self) -> None:
        self._share({"mode": "users", "grantees": ["dave"]})
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)
        status, _h, _p = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/unshare",
            principal="alice",
            body={"grantee": "dave"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.h.endpoints_visible_to("dave"), [])
        # 且取回一个**已在内存里缓存过**的视图也一样不行 —— 服务端每次都重新判定。
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="dave"
        )
        self.assertEqual(status, 404)

    def test_revoking_one_of_two_leaves_the_other(self) -> None:
        self._share({"mode": "users", "grantees": ["dave", "bob"]})
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/unshare",
            principal="alice",
            body={"grantee": "dave"},
        )
        self.assertEqual(self.h.endpoints_visible_to("dave"), [])
        self.assertEqual(len(self.h.endpoints_visible_to("bob")), 1)
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="alice"
        )
        self.assertEqual(payload["data"]["visibility"], "users")

    def test_removing_the_last_grantee_returns_to_private(self) -> None:
        self._share({"mode": "users", "grantees": ["dave"]})
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/unshare",
            principal="alice",
            body={"grantee": "dave"},
        )
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="alice"
        )
        self.assertEqual(payload["data"]["visibility"], VISIBILITY_PRIVATE)

    def test_a_grantee_cannot_re_share(self) -> None:
        self._share({"mode": "users", "grantees": ["dave"]})
        status, _h, payload = self._share(
            {"mode": "users", "grantees": ["bob"]}, principal="dave"
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")
        self.assertEqual(self.h.endpoints_visible_to("bob"), [])

    def test_a_grantee_cannot_see_the_grant_list(self) -> None:
        self._share({"mode": "users", "grantees": ["dave"]})
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}/acl", principal="dave"
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_sharing_to_an_unknown_principal_is_refused(self) -> None:
        status, _h, payload = self._share(
            {"mode": "users", "grantees": ["nonexistent-user"]}
        )
        self.assertEqual(status, 400)

    def test_sharing_to_yourself_is_refused(self) -> None:
        status, _h, payload = self._share({"mode": "users", "grantees": ["alice"]})
        self.assertEqual(status, 400)

    def test_users_mode_requires_at_least_one_grantee(self) -> None:
        status, _h, payload = self._share({"mode": "users", "grantees": []})
        self.assertEqual(status, 400)

    def test_visibility_mode_must_be_one_of_the_three(self) -> None:
        status, _h, payload = self._share({"mode": "public"})
        self.assertEqual(status, 400)

    def test_an_unverified_endpoint_cannot_be_shared(self) -> None:
        # 注册 != 可用：观测源缺席时 endpoint 停在 registered，共享必须被拒。
        h = Harness(prober=None)
        engine_id = h.make_engine("alice", "e")
        model_id = h.make_model("alice", "m")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )
        deployment_id = payload["data"]["id"]
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints",
            principal="alice",
            body={"name": "svc", "deployment_id": deployment_id},
        )
        endpoint_id = payload["data"]["id"]
        self.assertEqual(payload["data"]["status"], "registered")
        self.assertFalse(payload["data"]["shareable"])
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "all_approved_platform_users"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "registry.not_verified")

    def test_sharing_can_be_replaced_atomically(self) -> None:
        self._share({"mode": "users", "grantees": ["dave", "bob"]})
        self._share({"mode": "users", "grantees": ["dave"]})
        # 上一轮的授权集合被整体替换：bob 不再可见。
        self.assertEqual(self.h.endpoints_visible_to("bob"), [])
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)


class AdminTest(unittest.TestCase):
    """管理员：全局只读与可授权管理**都是 scope**，都不是角色。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")

    def test_admin_read_sees_everything_across_owners(self) -> None:
        self.assertEqual(len(self.h.endpoints_visible_to("carol")), 1)
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/engines", principal="carol"
        )
        self.assertEqual(payload["data"]["count"], 1)

    def test_admin_read_sees_raw_targets(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/engines", principal="carol"
        )
        self.assertIsNotNone(payload["data"]["items"][0]["target"]["host"])

    def test_admin_manage_can_share_another_owners_endpoint(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="carol",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)

    def test_admin_read_alone_cannot_manage(self) -> None:
        h = Harness()
        h.with_scopes("carol", ADMIN_READ_ONLY_SCOPES)
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="carol",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_a_role_of_admin_grants_nothing_by_itself(self) -> None:
        h = Harness()
        # 只有读取权，但 roles 里写着 admin。
        h.with_scopes(
            "alice", frozenset({"registry:read"}), roles=frozenset({"admin", "superuser"})
        )
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(status, 403, payload)
        status, _h, payload = h.request("GET", f"{PREFIX}/whoami", principal="alice")
        self.assertEqual(payload["data"]["authorized"]["roles"], ["admin", "superuser"])
        self.assertFalse(payload["data"]["authorized"]["is_admin_read"])

    def test_non_admin_cannot_read_the_routing_config(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/routing-config", principal="alice"
        )
        self.assertEqual(status, 403)

    def test_admin_audit_sees_other_actors_but_a_user_does_not(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request("GET", f"{PREFIX}/audit", principal="carol")
        actors = {entry["actor"] for entry in payload["data"]["items"]}
        self.assertIn("alice", actors)
        status, _h, payload = self.h.request("GET", f"{PREFIX}/audit", principal="dave")
        self.assertTrue(
            all(entry["actor"] == "dave" for entry in payload["data"]["items"])
        )

    def test_the_share_is_attributed_to_whoever_actually_did_it(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="carol",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request("GET", f"{PREFIX}/audit", principal="carol")
        shares = [e for e in payload["data"]["items"] if e["action"] == "share"]
        self.assertTrue(shares)
        self.assertEqual(shares[0]["actor"], "carol")
        self.assertEqual(shares[0]["actor_uid"], 4000)


class ScopeRequirementTest(unittest.TestCase):
    """每个动作都要求**它自己**的 scope，互不代偿。"""

    def setUp(self) -> None:
        self.h = Harness()

    def test_read_scope_is_required_to_list(self) -> None:
        h = Harness()
        h.with_scopes("alice", frozenset({"registry:write"}))
        status, _h, payload = h.request("GET", f"{PREFIX}/engines", principal="alice")
        self.assertEqual(status, 403)

    def test_write_scope_is_required_to_register(self) -> None:
        h = Harness()
        h.with_scopes("alice", frozenset({"registry:read"}))
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_share_scope_is_required_to_share(self) -> None:
        h = Harness()
        endpoint_id = h.make_ready_endpoint("alice")
        # 有读、有写，但**没有** share scope。
        h.with_scopes("alice", frozenset({"registry:read", "registry:write"}))
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_invoke_scope_is_required_to_invoke(self) -> None:
        h = Harness()
        endpoint_id = h.make_ready_endpoint("alice")
        h.with_scopes("alice", frozenset({"registry:read"}))
        status, _h, _p = h.request(
            "POST", f"{PREFIX}/endpoints/{endpoint_id}/invoke", principal="alice", body={}
        )
        self.assertEqual(status, 403)

    def test_an_unknown_scope_in_the_directory_is_refused(self) -> None:
        with self.assertRaises(Exception):
            TokenDirectory(
                [TokenBinding("t", "alice", frozenset({"registry:everything"}))]
            )


class WeightAndTargetRedactionTest(unittest.TestCase):
    """共享传递的是**服务**，不是 owner 的家目录与本机端口表。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )

    def test_the_grantee_sees_the_model_but_not_the_weight_path(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/models", principal="dave")
        self.assertEqual(payload["data"]["count"], 1)
        model = payload["data"]["items"][0]
        self.assertIsNone(model["weight_path"])
        self.assertTrue(model["weight_path_redacted"])
        self.assertTrue(model["weight_path_sha256"])

    def test_the_owner_still_sees_the_real_path(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/models", principal="alice")
        self.assertIn("/home/alice", payload["data"]["items"][0]["weight_path"])

    def test_the_grantee_cannot_see_the_engines_loopback_target(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/engines", principal="dave")
        self.assertEqual(payload["data"]["count"], 1)
        target = payload["data"]["items"][0]["target"]
        self.assertIsNone(target["host"])
        self.assertIsNone(target["port"])
        self.assertTrue(target["redacted"])

    def test_weight_presence_is_unobserved_not_false(self) -> None:
        # Registry 从不 stat 权重文件，「在不在」是**未观测**，写成 False 就是在编。
        status, _h, payload = self.h.request("GET", f"{PREFIX}/models", principal="alice")
        model = payload["data"]["items"][0]
        self.assertIsNone(model["weight_present"])
        self.assertFalse(model["weight_path_observed"])


class InvokeAuthorizationTest(unittest.TestCase):
    """``invoke`` 的每一步都先过服务端授权，且未验证/未共享都不可调用。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")
        # invoke 现在要求路由被 Gateway 回读确认过（PR #118 的 P0 门禁）。
        # 这个夹具显式完成一次确认，而不是依赖「无 ACK 也 200」的旧行为。
        self.h.confirm_route(self.endpoint_id)

    def test_a_non_grantee_cannot_invoke(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="bob",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 404, payload)
        self.assertEqual(self.h.gateway.calls, [])

    def test_a_grantee_can_invoke_and_the_call_is_recorded(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="dave",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["data"]["result"]["synthetic"])
        self.assertEqual(len(self.h.gateway.calls), 1)
        # mock 没有量过 token/延迟，所以是 null 而不是编出来的数字。
        self.assertIsNone(payload["data"]["metering"]["tokens"])
        self.assertIsNone(payload["data"]["metering"]["latency_ms"])

    def test_revocation_takes_effect_on_the_very_next_invoke(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="dave",
            body={"prompt": "ok"},
        )
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/unshare",
            principal="alice",
            body={"grantee": "dave"},
        )
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="dave",
            body={"prompt": "again"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(len(self.h.gateway.calls), 1)  # 第二次**没有**到达 gateway

    def test_invoke_payload_size_is_capped(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="alice",
            body={"prompt": "x" * 70000},
        )
        self.assertEqual(status, 400)


class RegistrationChainTest(unittest.TestCase):
    """注册 != 就绪：每一级都要上一级真的验证过。"""

    def setUp(self) -> None:
        self.h = Harness()

    def test_a_fresh_engine_is_registered_not_verified(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8081/v1"},
        )
        self.assertEqual(payload["data"]["status"], "unavailable")
        self.assertFalse(payload["data"]["health"]["verified"])

    def test_a_verified_engine_reports_verified(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(payload["data"]["status"], "verified")
        self.assertTrue(payload["data"]["health"]["verified"])
        self.assertEqual(payload["data"]["verification"]["status"], "VERIFIED")

    def test_a_deployment_on_an_unverified_engine_cannot_become_ready(self) -> None:
        engine_id = self.h.make_engine("alice", "e-dead", port=8081)
        model_id = self.h.make_model("alice", "m")
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )
        deployment_id = payload["data"]["id"]
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/deployments/{deployment_id}/verify", principal="alice"
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "registry.not_verified")

    def test_a_deployment_cannot_reference_an_invisible_engine(self) -> None:
        engine_id = self.h.make_engine("alice", "e")
        model_id = self.h.make_model("alice", "m")
        # dave 看不到 alice 的 engine：这里必须与「不存在」同答。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="dave",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )
        self.assertEqual(status, 404)

    def test_without_an_observation_source_nothing_becomes_verified(self) -> None:
        h = Harness(prober=None)
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(payload["data"]["status"], "registered")
        self.assertEqual(payload["data"]["health"]["state"], "unknown")
        self.assertIsNone(payload["data"]["health"]["latency_ms"])
        self.assertEqual(payload["data"]["verification"]["status"], "UNVERIFIED")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines/{payload['data']['id']}/verify",
            principal="alice",
        )
        self.assertEqual(payload["data"]["verification"]["status"], "NOT_CONFIGURED")
        self.assertEqual(payload["data"]["status"], "registered")


class OwnerOnlyMutationTest(unittest.TestCase):
    """非 owner 改别人的东西：可见 -> 403；不可见 -> 404。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.endpoint_id = self.h.make_ready_endpoint("alice")

    def test_a_non_owner_cannot_verify_someone_elses_endpoint(self) -> None:
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/endpoints/{self.endpoint_id}/verify", principal="dave"
        )
        self.assertEqual(status, 404)

    def test_a_grantee_cannot_verify_the_endpoint_they_can_use(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/endpoints/{self.endpoint_id}/verify", principal="dave"
        )
        # 能看见（所以不是 404），但不能改（所以是 403）。
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_a_non_owner_cannot_unshare(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/unshare",
            principal="dave",
            body={},
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
