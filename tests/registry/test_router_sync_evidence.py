"""路由同步的**证据类别**：谁有权说「已切流」，以及理由不许凭空断言。

本文件存在的理由是一条被同一份响应自我否定的 DTO：

* ``meta.router_sync_report.reason`` 说 ``gateway_revision_mismatch``
  （adapter 挂着、Gateway 生效的代数落后），
* 而 ``data.router_sync.reason`` 说 ``no_router_adapter_in_this_deployment``
  （本部署没有 adapter）。

两句都写在一份响应里，只能有一句是真的。根因是 ``Endpoint.router_sync_block()``
是一张**行级**视图，却断言了一件**部署级**事实 —— 一行数据库记录不知道本部署
装没装 adapter。

第二条：``docs/architecture.md`` §5.2 规则 3 写得很清楚 ——
「路由应用必须有 Gateway 发布的 generation/revision 与 snapshot SHA 回执」，
状态图也只认「回读 revision + snapshot SHA 完全一致」这一条边。

但运维路径 ``POST /endpoints/{id}/routing-applied {"generation": N}``
只带一个整数：没有 SHA、没有回读，却产出与 Gateway 回读**同一个**
``router_sync_applied``。一个整数不该买到「已切流」这四个字。

所以状态取值扩成三个，且 ``router_sync_applied`` 只由**回读证据**产生：

    pending_router_sync   回执代数落后于当前 revision
    router_sync_attested  运维声明已切流，无外部证据 —— 不是「已切流」
    router_sync_applied   Gateway 回读的 revision 与 snapshot SHA 逐位相等

``applied`` 布尔量只对第三种为真。前端/CLI 读到的「已切流」因此第一次有了
证据来源，而不仅仅是一个整数相等。
"""

from __future__ import annotations

import unittest

from tests.registry.support import Harness
from tests.registry.test_trusted_gateway_and_router import (
    ConfirmingRouterAdapter,
    StaleRouterAdapter,
)

from backend.registry.api import PREFIX
from backend.registry.models import (
    REASON_GENERATION_LAGS_REVISION,
    REASON_NO_ROUTER_ADAPTER,
    ROUTER_EVIDENCE_GATEWAY_READBACK,
    ROUTER_EVIDENCE_OPERATOR_ATTESTATION,
    ROUTER_EVIDENCE_KINDS,
    ROUTER_SYNC_APPLIED,
    ROUTER_SYNC_ATTESTED,
    ROUTER_SYNC_PENDING,
    ROUTER_SYNC_STATES,
)


class EvidenceVocabularyTest(unittest.TestCase):
    """状态与证据都是**封闭**取值：新增一个必须同时想清楚它在前端画成什么。"""

    def test_the_state_vocabulary_is_closed_and_ordered(self) -> None:
        self.assertEqual(
            ROUTER_SYNC_STATES,
            (ROUTER_SYNC_PENDING, ROUTER_SYNC_ATTESTED, ROUTER_SYNC_APPLIED),
        )

    def test_applied_is_not_attested(self) -> None:
        # 这两种「代数已经相等」的情形必须是两个词，否则一个整数就能买到「已切流」。
        self.assertNotEqual(ROUTER_SYNC_APPLIED, ROUTER_SYNC_ATTESTED)

    def test_the_evidence_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            set(ROUTER_EVIDENCE_KINDS),
            {ROUTER_EVIDENCE_GATEWAY_READBACK, ROUTER_EVIDENCE_OPERATOR_ATTESTATION},
        )


class TheReasonMustNotInventADeploymentFactTest(unittest.TestCase):
    """一张行级视图不许断言部署级事实。"""

    def test_a_stale_adapter_is_not_reported_as_no_adapter(self) -> None:
        h = Harness(router=StaleRouterAdapter())
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        target = h.make_ready_deployment("alice", "second")
        _status, _headers, payload = h.repoint("alice", endpoint, target)

        # 部署级事实（service 知道 adapter）：落后的代数是真实原因。
        self.assertEqual(
            payload["meta"]["router_sync_report"]["reason"], "gateway_revision_mismatch"
        )
        # 行级视图不得反过来宣称「本部署没有 adapter」——它不知道这件事。
        self.assertNotEqual(payload["data"]["router_sync"]["reason"], REASON_NO_ROUTER_ADAPTER)
        self.assertNotIn("没有任何 Gateway 适配器", payload["data"]["router_sync"]["note"])

    def test_a_detached_deployment_may_say_there_is_no_adapter(self) -> None:
        h = Harness()  # 缺省：真的没有 adapter —— 这时那句话才是真的
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        target = h.make_ready_deployment("alice", "second")
        _status, _headers, payload = h.repoint("alice", endpoint, target)
        self.assertEqual(payload["data"]["router_sync"]["reason"], REASON_NO_ROUTER_ADAPTER)

    def test_a_plain_read_also_carries_the_deployment_level_reason(self) -> None:
        # repoint 之外的读路径（GET 详情）同样要有这句，否则前后端各读一份。
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        _status, _headers, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint}", principal="alice"
        )
        self.assertEqual(payload["data"]["router_sync"]["reason"], REASON_NO_ROUTER_ADAPTER)

    def test_the_row_level_reason_is_the_generation_lag(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        _status, _headers, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint}", principal="alice"
        )
        self.assertEqual(payload["data"]["router_sync"]["current_generation"], 1)
        self.assertEqual(payload["data"]["router_sync"]["applied_generation"], 0)


class OnlyAReadbackMaySayAppliedTest(unittest.TestCase):
    def test_an_operator_declaration_is_attested_never_applied(self) -> None:
        h = Harness()  # 没有 adapter：没有任何东西能回读
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        target = h.make_ready_deployment("alice", "second")
        h.repoint("alice", endpoint, target)
        revision = h.endpoint_revision("alice", endpoint)

        status, _headers, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/routing-applied",
            principal="carol",
            body={"generation": revision},
        )
        self.assertEqual(status, 200, payload)
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], ROUTER_SYNC_ATTESTED)
        self.assertFalse(block["applied"])
        self.assertEqual(block["evidence"], ROUTER_EVIDENCE_OPERATOR_ATTESTATION)
        self.assertNotEqual(block["state"], ROUTER_SYNC_APPLIED)
        self.assertFalse(payload["meta"]["router_applied"])

    def test_a_gateway_readback_is_what_says_applied(self) -> None:
        h = Harness(router=ConfirmingRouterAdapter())
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        # 快照必须真的包含这条路由，否则「回读确认」确认的是一个空快照，
        # 这条断言就什么也没证明（见 test_router_ack_binding.py）。
        h.publish_engine()
        target = h.make_sibling_deployment("alice", endpoint, "second")
        _status, _headers, payload = h.repoint("alice", endpoint, target)

        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], ROUTER_SYNC_APPLIED)
        self.assertTrue(block["applied"])
        self.assertEqual(block["evidence"], ROUTER_EVIDENCE_GATEWAY_READBACK)
        self.assertTrue(payload["meta"]["router_applied"])

    def test_the_attested_state_survives_a_later_plain_read(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        target = h.make_ready_deployment("alice", "second")
        h.repoint("alice", endpoint, target)
        revision = h.endpoint_revision("alice", endpoint)
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint}/routing-applied",
            principal="carol",
            body={"generation": revision},
        )
        _status, _headers, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint}", principal="alice"
        )
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], ROUTER_SYNC_ATTESTED)
        self.assertFalse(block["applied"])
        self.assertFalse(payload["data"]["pending_router_sync"])

    def test_a_lagging_generation_is_never_applied(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        target = h.make_ready_deployment("alice", "second")
        h.repoint("alice", endpoint, target)
        _status, _headers, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint}", principal="alice"
        )
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], ROUTER_SYNC_PENDING)
        self.assertFalse(block["applied"])
        self.assertIsNone(block["evidence"])


class LegacyRowsAreNotPromotedTest(unittest.TestCase):
    """迁移前落库的行没有证据记录。缺证据一律按**未验证**读，不按已切流读。"""

    def test_a_row_whose_generation_caught_up_without_evidence_is_attested(self) -> None:
        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        with h.service.store.transaction() as connection:
            connection.execute(
                "UPDATE endpoints SET router_generation = revision WHERE id = ?",
                (endpoint,),
            )
        _status, _headers, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint}", principal="alice"
        )
        block = payload["data"]["router_sync"]
        self.assertFalse(block["pending"])
        self.assertEqual(block["state"], ROUTER_SYNC_ATTESTED)
        self.assertFalse(block["applied"])
        self.assertIsNone(block["evidence"])

    def test_the_store_refuses_an_unknown_evidence_token(self) -> None:
        from backend.registry.errors import InvalidRequest

        h = Harness()
        self.addCleanup(h.close)
        endpoint = h.make_ready_endpoint("alice")
        with self.assertRaises(InvalidRequest):
            h.service.store.ack_router_generation(
                endpoint,
                generation=1,
                actor_principal="system:router-sync",
                actor_uid=0,
                at=h.service._now(),
                evidence="trust-me",
            )


if __name__ == "__main__":
    unittest.main()
