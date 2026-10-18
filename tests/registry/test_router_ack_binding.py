"""ACK 必须绑定到**已发布快照里的那条路由**，不是一个 revision 算术。

`_attempt_router_sync` 从前只做一件事：RouterAdapter 说 ``published=True`` 之后，
把所有 ``revision <= applied_revision`` 且仍 pending 的 Endpoint 一起回执。

它没有问一个更基本的问题：**这条 Endpoint 在这次快照里吗？**

`build_routing_snapshot` 会诚实地排除它发不出去的行，并把理由写进 ``excluded``：
engine 没有 ``secret_ref``（``no_secret_ref``）、engine 没被发布
（``engine_not_published``）、deployment/model 缺失、owner 不在受信目录、
未知 visibility……被排除的 Endpoint **Gateway 从来没收到过**。给它记一笔
``router_generation = revision``，等于凭一个整数说「这条路由已经生效」——
和本仓库刚在 PR #89 里拆掉的谎报是同一类，只是换了一条路径。

实测（修复前）：快照 ``endpoints == []``，一次 repoint 仍把 2 条 Endpoint 记进
``acked_endpoints``，其中一条读出 ``state=router_sync_applied``、
``evidence=gateway_readback``，``applied=true``。

本文件把这些情形钉住：

* 被快照排除的 Endpoint 永远不被回执，哪怕同一轮里有别的 Endpoint 成功回执；
* 真正在快照里的 Endpoint 才被回执，且 ``acked_endpoints`` 只包含它；
* 迟到的（落后于当前 revision 的）回执被拒，且不把状态往回推；
* revision 与 router_generation 单调：前者只增，后者永不超过前者；
* 回执是 CAS：``generation`` 必须与当前 revision 精确相等。
"""

from __future__ import annotations

import unittest

from tests.registry.support import Harness
from tests.registry.test_trusted_gateway_and_router import ConfirmingRouterAdapter

from backend.registry.api import PREFIX


def _engine_of(h: Harness, endpoint_id: str) -> str:
    endpoint = h.service.store.endpoint_by_id(endpoint_id)
    assert endpoint is not None
    deployment = h.service.store.deployment_by_id(endpoint.deployment_id)
    assert deployment is not None
    return deployment.engine_id


class SnapshotMembershipTest(unittest.TestCase):
    def test_the_snapshot_really_excludes_the_second_engine(self) -> None:
        """先证明夹具本身是对的：一条进快照，一条不进。"""
        h = Harness()
        self.addCleanup(h.close)
        routable = h.make_ready_endpoint("alice", name="routable")
        excluded = h.make_ready_endpoint("alice", name="excluded")
        # 只让 routable 的 engine 可发布；excluded 的 engine 留在快照之外。
        h.publish_engine(_engine_of(h, routable))

        snapshot = h.service._routing_snapshot()
        ids = {entry["endpoint_id"] for entry in snapshot["endpoints"]}
        self.assertIn(routable, ids)
        self.assertNotIn(excluded, ids)
        reasons = {
            entry.get("endpoint_id"): entry.get("reason") for entry in snapshot["excluded"]
        }
        self.assertEqual(reasons.get(excluded), "engine_not_published")


class AnExcludedEndpointIsNeverAckedTest(unittest.TestCase):
    """被排除的行不能靠别人的成功回执沾光。"""

    def _harness(self) -> tuple[Harness, str, str]:
        h = Harness(router=ConfirmingRouterAdapter())
        self.addCleanup(h.close)
        routable = h.make_ready_endpoint("alice", name="routable")
        excluded = h.make_ready_endpoint("alice", name="excluded")
        h.publish_engine(_engine_of(h, routable))
        return h, routable, excluded

    def test_only_the_published_endpoint_is_acked(self) -> None:
        h, routable, excluded = self._harness()
        # repoint 到**同一个 engine** 上的兄弟 deployment：换的是路由，不是上游。
        target = h.make_sibling_deployment("alice", routable, "second")
        status, _headers, payload = h.repoint("alice", routable, target)
        self.assertEqual(status, 200, payload)

        acked = payload["meta"]["router_sync_report"]["acked_endpoints"]
        self.assertIn(routable, acked)
        self.assertNotIn(excluded, acked)

    def test_the_excluded_endpoint_stays_pending_and_carries_no_evidence(self) -> None:
        h, routable, excluded = self._harness()
        target = h.make_sibling_deployment("alice", routable, "second")
        h.repoint("alice", routable, target)

        _status, _headers, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{excluded}", principal="alice"
        )
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], "pending_router_sync")
        self.assertFalse(block["applied"])
        self.assertIsNone(block["evidence"])
        self.assertEqual(payload["data"]["router_generation"], 0)

    def test_an_empty_snapshot_acks_nothing(self) -> None:
        """快照为空时，一次「确认成功」的回执不能给任何人记账。"""
        h = Harness(router=ConfirmingRouterAdapter())
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice", name="only")
        target = h.make_ready_deployment("alice", "second")

        # 刻意**不**补 engine_secret_refs：本部署发布不出任何路由。
        self.assertEqual(h.service._routing_snapshot()["endpoints"], [])
        status, _headers, payload = h.repoint("alice", endpoint, target)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["router_sync_report"]["acked_endpoints"], [])
        self.assertEqual(payload["meta"]["router_sync"], "pending_router_sync")
        self.assertFalse(payload["meta"]["router_applied"])


class LateAckNeverPushesStateBackwardsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.endpoint = self.h.make_ready_endpoint("alice", name="svc")
        self.a = self.h.make_ready_deployment("alice", "a")
        self.b = self.h.make_ready_deployment("alice", "b")

    def _revision(self) -> int:
        return self.h.endpoint_revision("alice", self.endpoint)

    def _ack(self, generation: int, principal: str = "carol"):
        return self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/routing-applied",
            principal=principal,
            body={"generation": generation},
        )

    def test_an_ack_for_a_superseded_revision_is_refused_409(self) -> None:
        # revision 2
        self.h.repoint("alice", self.endpoint, self.a)
        stale = self._revision()
        # revision 3 —— 上面那个回执来晚了
        self.h.repoint("alice", self.endpoint, self.b)
        self.assertGreater(self._revision(), stale)

        status, _headers, payload = self._ack(stale)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.revision_conflict")

        # 状态没有被推回去，也没有被记成已切流。
        _s, _h, after = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint}", principal="alice"
        )
        self.assertTrue(after["data"]["pending_router_sync"])
        self.assertEqual(after["data"]["router_sync"]["state"], "pending_router_sync")
        self.assertFalse(after["data"]["router_sync"]["applied"])

    def test_a_late_ack_cannot_lower_an_already_recorded_generation(self) -> None:
        self.h.repoint("alice", self.endpoint, self.a)
        current = self._revision()
        status, _headers, _payload = self._ack(current)
        self.assertEqual(status, 200)
        recorded = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint}", principal="alice"
        )[2]["data"]["router_generation"]
        self.assertEqual(recorded, current)

        # 再推进一次 revision；随后用旧代数回执，必须被拒且不改变已记录值。
        self.h.repoint("alice", self.endpoint, self.b)
        status, _headers, _payload = self._ack(recorded)
        self.assertEqual(status, 409)
        after = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint}", principal="alice"
        )[2]["data"]["router_generation"]
        self.assertEqual(after, recorded)


class GenerationAndRevisionAreMonotonicTest(unittest.TestCase):
    def test_revision_only_increases_and_generation_never_exceeds_it(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice", name="svc")
        first = h.make_ready_deployment("alice", "first")
        second = h.make_ready_deployment("alice", "second")

        seen_revisions: list[int] = []
        seen_generations: list[int] = []

        def observe() -> dict:
            data = h.request(
                "GET", f"{PREFIX}/endpoints/{endpoint}", principal="alice"
            )[2]["data"]
            seen_revisions.append(data["revision"])
            seen_generations.append(data["router_generation"])
            return data

        observe()
        for target in (first, second, first, second):
            status, _headers, payload = h.repoint("alice", endpoint, target)
            self.assertEqual(status, 200, payload)
            observe()
        status, _headers, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/rollback",
            principal="alice",
            body={"expected_revision": payload["data"]["revision"]},
        )
        self.assertEqual(status, 200, payload)
        observe()

        self.assertEqual(seen_revisions, sorted(seen_revisions))
        self.assertEqual(len(set(seen_revisions)), len(seen_revisions), seen_revisions)
        for generation, revision in zip(seen_generations, seen_revisions):
            self.assertLessEqual(generation, revision)
        self.assertEqual(seen_generations, sorted(seen_generations))


class TheAckIsACompareAndSwapTest(unittest.TestCase):
    def test_generation_must_equal_the_current_revision_exactly(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice", name="svc")
        other = h.make_ready_deployment("alice", "other")
        h.repoint("alice", endpoint, other)
        revision = h.endpoint_revision("alice", endpoint)

        for wrong in (revision - 1, revision + 1, 0, -1):
            with self.subTest(generation=wrong):
                status, _headers, payload = h.request(
                    "POST",
                    f"{PREFIX}/endpoints/{endpoint}/routing-applied",
                    principal="carol",
                    body={"generation": wrong},
                )
                # 0 / -1 在输入校验就被拒；其余是 CAS 冲突。
                self.assertIn(status, (400, 409), payload)


if __name__ == "__main__":
    unittest.main()
