"""V1-06 / V1-07 的 Registry 侧验收：CPU-only、合成、**不声称 REAL**。

`docs/v1/V1_ACCEPTANCE.md` 把这两条写成：

* **V1-06**：Owner 能登记/验证自己的 Engine/Model/Deployment/Endpoint，并看到
  **准确的路由状态**。证据要求「Registry 状态机 + 真实的 Engine/Router ACK」。
* **V1-07**：A 把授权的 Endpoint 分享给 B，B 经 Gateway 调用，A 撤权，B 的下一次
  请求被拒。证据要求「真实的 bounded invoke、request ID、route revision、
  授权审计」。

本文件提供的是**离线那一半**：状态机、按 principal 的独立 scoped token、
回环上的 FakeRouter 替身、有界载荷、下一次请求即拒。**另一半（真实 Dell Gateway）
不在这里，也不被声称** —— 每一个用例都跑在 `SyntheticGateway` 上，它复刻
PR #20 的 ACL 判定，但只认合成 key。

判断 REAL 与否只看一件事：`result.synthetic is True` 且
`gateway.adapter == "trusted_http"` 指向的是替身。任何一条用例都不写 LIVE。
"""

from __future__ import annotations

import unittest

from tests.registry.support import Harness
from tests.registry.test_trusted_gateway_and_router import (
    ConfirmingRouterAdapter,
    DecoyUpstream,
    SyntheticGateway,
)

from backend.registry.api import PREFIX
from backend.registry.gateway import StaticGatewayKeyResolver, TrustedGatewayAdapter
from backend.registry.health import (
    PROTOCOL_VERIFIED,
    REASON_MODEL_LIST_VERIFIED,
    ProbeResult,
    ProtocolObservation,
    StaticProber,
    StaticProtocolProber,
)
from backend.registry.registry import RegistryConfig

LOOPBACK = "127.0.0.1"

#: 两个**互不相同**的 scoped token。B 用它自己的，不是 A 的，也不是 admin 的。
A = "alice"
B = "bob"
C = "dave"  # 完全无关的第三方


class _SyntheticChain:
    """一个 A→FakeRouter 的装置；`router` 决定路由回执走哪条路径。"""

    def __init__(self, testcase: unittest.TestCase, router=None) -> None:
        self.decoy = DecoyUpstream()
        testcase.addCleanup(self.decoy.stop)
        self.gateway = SyntheticGateway(
            keys={"synthetic-alice-key": A, "synthetic-bob-key": B}
        )
        testcase.addCleanup(self.gateway.stop)

        adapter = TrustedGatewayAdapter(
            base_url=self.gateway.base_url,
            keys=StaticGatewayKeyResolver(
                {A: "synthetic-alice-key", B: "synthetic-bob-key"}
            ),
        )
        self.h = Harness(
            gateway=adapter,
            router=router,
            config=RegistryConfig(
                mode="synthetic", allowed_ports=frozenset({self.decoy.port})
            ),
            prober=StaticProber(
                {self.decoy.port: ProbeResult(reachable=True, latency_ms=1.0)}
            ),
            protocol_prober=StaticProtocolProber(
                {
                    (LOOPBACK, self.decoy.port): ProtocolObservation(
                        state=PROTOCOL_VERIFIED,
                        reason=REASON_MODEL_LIST_VERIFIED,
                        protocol="openai_compatible",
                        checked_path="/v1/models",
                        http_status=200,
                        model_ids=1,
                        source="static_test_double",
                    )
                }
            ),
        )
        testcase.addCleanup(self.h.close)
        self.endpoint = self.h.make_ready_endpoint(A, name="chat", port=self.decoy.port)
        # 快照必须非空：没有 secret_ref 的 engine 会被整个排除。
        self.h.publish_engine()

    # -- 便捷入口 ---------------------------------------------------------

    def request(self, method: str, path: str, principal: str, body: dict | None = None):
        return self.h.request(method, path, principal=principal, body=body)

    def share_to_b(self):
        return self.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/share",
            A,
            {"mode": "users", "grantees": [B]},
        )

    def publish(self) -> dict:
        snapshot = self.h.service._routing_snapshot()
        assert snapshot["endpoints"], snapshot.get("excluded")
        self.gateway.set_snapshot(snapshot)
        return snapshot

    def confirm_route(self) -> int:
        """让 `invoke` 的路由门通过：一次真实的 Gateway 回读确认（PR #118）。"""
        return self.h.acknowledge_route(self.endpoint)

    def invoke(self, principal: str, body: dict | None = None):
        return self.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/invoke",
            principal,
            body if body is not None else {"messages": [{"role": "user", "content": "hi"}]},
        )

    def view(self, principal: str = A) -> dict:
        status, _headers, payload = self.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint}", principal
        )
        assert status == 200, payload
        return payload["data"]


class V106RouteStatusIsAccurateTest(unittest.TestCase):
    """V1-06：登记/验证/就绪/路由已生效是**四个**状态，不许互相顶替。"""

    def setUp(self) -> None:
        self.chain = _SyntheticChain(self)

    def test_a_sees_registered_ready_and_routing_as_separate_axes(self) -> None:
        data = self.chain.view(A)
        self.assertTrue(data["registered"])
        self.assertTrue(data["ready"])
        self.assertEqual(data["status"], "ready")
        # 刚验证完、尚未回执：路由**没有**生效。
        self.assertTrue(data["pending_router_sync"])
        block = data["router_sync"]
        self.assertEqual(block["state"], "pending_router_sync")
        self.assertFalse(block["applied"])
        self.assertIsNone(block["evidence"])
        self.assertEqual(block["applied_generation"], 0)
        self.assertEqual(block["current_generation"], data["revision"])

    def test_the_private_endpoint_is_private_in_the_published_snapshot(self) -> None:
        snapshot = self.chain.publish()
        entry = next(
            item for item in snapshot["endpoints"] if item["endpoint_id"] == self.chain.endpoint
        )
        self.assertEqual(entry["acl"], {"kind": "private"})
        self.assertEqual(entry["state"], "active")
        # 快照点名的是 Linux 用户，不是 principal —— 但也绝不内联任何密钥。
        self.assertTrue(entry["owner_uid"])
        self.assertNotIn("key", entry)
        for engine in snapshot["engines"]:
            self.assertNotIn("value", engine["secret_ref"])

    def test_an_unrelated_principal_gets_the_same_answer_as_a_missing_endpoint(self) -> None:
        forbidden = self.chain.request(
            "GET", f"{PREFIX}/endpoints/{self.chain.endpoint}", C
        )
        missing = self.chain.request("GET", f"{PREFIX}/endpoints/ep_absent", C)
        self.assertEqual(forbidden[0], missing[0])
        self.assertEqual(forbidden[2]["error"]["code"], missing[2]["error"]["code"])
        self.assertNotIn(A, repr(forbidden[2]))
        self.assertNotIn("chat", repr(forbidden[2]))


class V107ShareInvokeRevokeTest(unittest.TestCase):
    """V1-07：A share → B 用**自己的** scoped token 调用 → A 撤权 → B 下一次被拒。"""

    def setUp(self) -> None:
        self.chain = _SyntheticChain(self)

    def test_b_calls_with_its_own_token_and_the_router_sees_b_key(self) -> None:
        status, _h, payload = self.chain.share_to_b()
        self.assertEqual(status, 200, payload)
        self.chain.publish()
        self.chain.confirm_route()

        status, headers, payload = self.chain.invoke(B)
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        # 服务端声明这是经服务端授权的 Gateway 转发，且结果自称 synthetic。
        self.assertEqual(data["gateway"]["adapter"], "trusted_http")
        self.assertEqual(data["gateway"]["authorization"], "server_side_enforced")
        self.assertEqual(data["result"]["served_by"], "gateway")
        self.assertTrue(data["result"]["synthetic"])
        self.assertTrue(data["metering"]["recorded"])
        # request ID 由服务端生成，出现在响应头上。
        rid = dict(headers).get("X-Request-Id")
        self.assertTrue(rid)

        # 替身 Gateway 看到的是 **B 的 key**，不是 A 的 —— 身份没有串。
        self.assertEqual(len(self.chain.gateway.calls), 1)
        call = self.chain.gateway.calls[0]
        self.assertEqual(call["authorization"], "Bearer synthetic-bob-key")
        self.assertTrue(call["request_id"])
        # 诱饵上游一次都没被碰过。
        self.assertEqual(self.chain.decoy.hits, 0)

    def test_b_without_a_grant_cannot_invoke_and_the_router_is_never_called(self) -> None:
        self.chain.publish()
        self.chain.confirm_route()
        status, _h, payload = self.chain.invoke(C)
        self.assertEqual(status, 404, payload)
        self.assertEqual(self.chain.gateway.calls, [])

    def test_revocation_denies_b_on_the_next_invoke_without_a_second_router_call(self) -> None:
        self.chain.share_to_b()
        self.chain.publish()
        self.chain.confirm_route()
        self.assertEqual(self.chain.invoke(B)[0], 200)
        calls_before = len(self.chain.gateway.calls)

        status, _h, payload = self.chain.request(
            "POST", f"{PREFIX}/endpoints/{self.chain.endpoint}/unshare", A, {}
        )
        self.assertEqual(status, 200, payload)

        status, _h, payload = self.chain.invoke(B)
        self.assertEqual(status, 404, payload)
        self.assertEqual(len(self.chain.gateway.calls), calls_before)

    def test_a_bounded_payload_is_enforced_before_the_router_is_called(self) -> None:
        self.chain.share_to_b()
        self.chain.publish()
        self.chain.confirm_route()
        status, _h, payload = self.chain.invoke(
            B, {"messages": [], "padding": "x" * (64 * 1024 + 1)}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.chain.gateway.calls, [])


class V107RepointRevokesTheOldGrantTest(unittest.TestCase):
    """旧授权不随 repoint 静默带到新后端。"""

    def setUp(self) -> None:
        self.chain = _SyntheticChain(self)

    def test_repoint_without_reauthorize_revokes_b_and_the_new_snapshot_has_no_grantee(self) -> None:
        self.chain.share_to_b()
        self.chain.publish()
        self.chain.confirm_route()
        self.assertEqual(self.chain.invoke(B)[0], 200)

        target = self.chain.h.make_sibling_deployment(A, self.chain.endpoint, "next")
        status, _h, payload = self.chain.h.repoint(A, self.chain.endpoint, target)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["visibility"], "private")
        self.assertEqual(self.chain.h.service.store.grantees(self.chain.endpoint), ())

        snapshot = self.chain.h.service._routing_snapshot()
        entry = next(
            item for item in snapshot["endpoints"] if item["endpoint_id"] == self.chain.endpoint
        )
        self.assertEqual(entry["acl"], {"kind": "private"})
        self.assertNotIn("principals", entry["acl"])

        # B 的下一次调用：Registry 侧就 404，且不再打 Gateway。
        calls_before = len(self.chain.gateway.calls)
        status, _h, _payload = self.chain.invoke(B)
        self.assertEqual(status, 404)
        self.assertEqual(len(self.chain.gateway.calls), calls_before)


class V107RouteRevisionAndAckCasTest(unittest.TestCase):
    """route revision / ACK CAS：落后的回执不许生效，也不许把状态往回推。"""

    def setUp(self) -> None:
        self.chain = _SyntheticChain(self)  # 缺省 DetachedRouterAdapter

    def test_a_repoint_leaves_routing_pending_and_the_ack_is_a_cas(self) -> None:
        before = self.chain.view(A)["revision"]
        target = self.chain.h.make_sibling_deployment(A, self.chain.endpoint, "next")
        status, _h, payload = self.chain.h.repoint(A, self.chain.endpoint, target)
        self.assertEqual(status, 200, payload)

        after = self.chain.view(A)
        self.assertEqual(after["revision"], before + 1)
        self.assertTrue(after["pending_router_sync"])
        self.assertEqual(after["router_sync"]["state"], "pending_router_sync")

        # 落后 / 超前 / 非法的 generation 都不被接受。
        for wrong in (before, after["revision"] + 1, 0):
            with self.subTest(generation=wrong):
                status, _h, payload = self.chain.request(
                    "POST",
                    f"{PREFIX}/endpoints/{self.chain.endpoint}/routing-applied",
                    "carol",
                    {"generation": wrong},
                )
                self.assertIn(status, (400, 409), payload)

        # 精确相等才被记下；但它只是**运维声明**，不是已切流。
        status, _h, payload = self.chain.request(
            "POST",
            f"{PREFIX}/endpoints/{self.chain.endpoint}/routing-applied",
            "carol",
            {"generation": after["revision"]},
        )
        self.assertEqual(status, 200, payload)
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], "router_sync_attested")
        self.assertFalse(block["applied"])
        self.assertEqual(block["evidence"], "operator_attestation")
        self.assertFalse(payload["meta"]["router_applied"])

    def test_only_a_readback_produces_applied(self) -> None:
        chain = _SyntheticChain(self, router=ConfirmingRouterAdapter())
        target = chain.h.make_sibling_deployment(A, chain.endpoint, "next")
        status, _h, payload = chain.h.repoint(A, chain.endpoint, target)
        self.assertEqual(status, 200, payload)
        report = payload["meta"]["router_sync_report"]
        self.assertEqual(report["acked_endpoints"], [chain.endpoint])
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], "router_sync_applied")
        self.assertTrue(block["applied"])
        self.assertEqual(block["evidence"], "gateway_readback")


if __name__ == "__main__":
    unittest.main()
