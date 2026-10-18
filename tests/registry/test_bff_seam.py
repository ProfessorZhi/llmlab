"""BFF seam：同源 BFF 从 Registry 拿走的东西，就在这份文件里被钉住。

`backend/bff/ops.py` 与 `backend/bff/app.py` 把 ``RegistryService`` 直接注入
``OpsApplication(service=...)``，然后只调用很少的几个入口：

    service.policy(requester)      -> AccessPolicy；可见性**全部**由它决定
    policy.visible_endpoints() / visible_deployments()
    service.store.grantees(endpoint_id)
    service.principals.approved_principals() / lookup(name)
    service.share_endpoint(...) / unshare_endpoint(...)

BFF 侧对这份 seam 的声明是「它只读 ``visible_*``，不重新判定任何一行 ACL」。
本文件存在的理由是：那条声明是关于 **Registry** 的性质，所以必须有一条测试
**站在 Registry 这边**证明它 —— 否则哪天 `visible_endpoints()` 开始返回更宽的
集合，BFF 会照它说的渲染，而没有任何东西会失败。

这里刻意**不** import `backend/bff`：这条 seam 的契约是可调用的形状与行级 ACL
语义，不是某个具体调用点的实现。BFF 侧另有自己的 route_map / ops 测试。
"""

from __future__ import annotations

import unittest

from tests.registry.support import Harness

from backend.registry.api import PREFIX
from backend.registry.authz import AccessPolicy
from backend.registry.identity import RequesterContext


class TheInjectedObjectIsWhatTheBffCallsTest(unittest.TestCase):
    """``OpsApplication(service=...)`` 那个槽位要求的形状。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_the_service_exposes_every_entry_point_the_bff_uses(self) -> None:
        service = self.h.service
        for name in ("policy", "store", "principals", "share_endpoint", "unshare_endpoint"):
            with self.subTest(attribute=name):
                self.assertTrue(hasattr(service, name), name)

    def test_policy_takes_the_registrys_own_requester_type(self) -> None:
        # BFF 用的就是同一个类（backend/bff/identity.py 直接 import 它）。
        # 两个同名的 RequesterContext 是这条 seam 最容易悄悄漂移的地方。
        requester = self.h.requester("alice")
        self.assertIsInstance(requester, RequesterContext)
        self.assertIsInstance(self.h.service.policy(requester), AccessPolicy)

    def test_the_policy_answers_the_four_visible_collections(self) -> None:
        policy = self.h.service.policy(self.h.requester("alice"))
        for name in (
            "visible_endpoints",
            "visible_deployments",
            "visible_engines",
            "visible_models",
        ):
            with self.subTest(method=name):
                self.assertIsInstance(getattr(policy, name)(), list)

    def test_the_principal_directory_answers_what_the_bff_reads(self) -> None:
        principals = self.h.service.principals
        self.assertIsInstance(principals.approved_principals(), frozenset)
        self.assertIsNotNone(principals.lookup("alice"))

    def test_grantees_reads_a_tuple_for_a_plain_endpoint(self) -> None:
        endpoint = self.h.make_ready_endpoint("alice")
        self.assertIsInstance(self.h.service.store.grantees(endpoint), tuple)


class VisibilityIsDecidedByRegistryAndOnlyRegistryTest(unittest.TestCase):
    """BFF 只读 ``visible_*``；那条声明的真假由这里决定。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.endpoint = self.h.make_ready_endpoint("alice")
        self.target = self.h.make_ready_deployment("alice", "shared")

    def _visible_endpoint_ids(self, principal: str) -> list[str]:
        policy = self.h.service.policy(self.h.requester(principal))
        return [item.id for item in policy.visible_endpoints()]

    def test_a_private_endpoint_is_not_in_another_principals_visible_set(self) -> None:
        self.assertIn(self.endpoint, self._visible_endpoint_ids("alice"))
        self.assertNotIn(self.endpoint, self._visible_endpoint_ids("bob"))

    def test_a_shared_endpoint_enters_the_grantees_visible_set(self) -> None:
        status, _headers, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["bob"]},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn(self.endpoint, self._visible_endpoint_ids("bob"))
        self.assertIn("bob", self.h.service.store.grantees(self.endpoint))

    def test_a_revoked_grantee_leaves_the_visible_set_on_the_next_read(self) -> None:
        # 契约 §2.2 规则 5：撤权必须在**下一次请求**生效，不是在客户端缓存里过期。
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["bob"]},
        )
        self.assertIn(self.endpoint, self._visible_endpoint_ids("bob"))
        status, _headers, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/unshare",
            principal="alice",
            body={"grantee": "bob"},
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn(self.endpoint, self._visible_endpoint_ids("bob"))
        self.assertEqual(self.h.service.store.grantees(self.endpoint), ())

    def test_the_visible_set_never_carries_another_owners_private_endpoint(self) -> None:
        other = self.h.make_ready_endpoint("dave", name="dave-svc")
        self.assertNotIn(other, self._visible_endpoint_ids("alice"))
        self.assertNotIn(other, self._visible_endpoint_ids("bob"))


class ARefusalAtTheSeamDoesNotLeakExistenceTest(unittest.TestCase):
    """安全拒绝不泄漏他人 Endpoint 的存在 —— 在 seam 这一层再钉一次。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)

    def test_reading_a_forbidden_endpoint_and_a_missing_one_are_indistinguishable(self) -> None:
        private = self.h.make_ready_endpoint("alice")
        forbidden = self.h.request(
            "GET", f"{PREFIX}/endpoints/{private}", principal="bob"
        )
        missing = self.h.request(
            "GET", f"{PREFIX}/endpoints/ep_does_not_exist", principal="bob"
        )
        self.assertEqual(forbidden[0], missing[0])
        self.assertEqual(forbidden[2]["error"]["code"], missing[2]["error"]["code"])

    def test_the_refusal_never_names_the_owner_or_the_route(self) -> None:
        private = self.h.make_ready_endpoint("alice")
        _status, _headers, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{private}", principal="bob"
        )
        rendered = repr(payload)
        self.assertNotIn("alice", rendered)
        self.assertNotIn("svc", rendered)


if __name__ == "__main__":
    unittest.main()
