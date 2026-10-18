"""SEAM-1：同源 ``POST /api/v2/ops/endpoints/{id}/invoke``。

Issue #68 的离线纵切发现浏览器同源入口**没有**调用路径：``/api/v2/registry/*``
被 ``route_map`` 故意钉在 404，所以 V1-07 只能走 Registry 服务层。这一组验的是那条
新挂的同源入口，而且刻意**只**验边界 —— 授权本身仍然是 Registry 的：

* 身份只来自凭据（会话或 scoped token），没有任何参数能指定 uid/owner/key/上游；
* 写操作必须过 CSRF/同源；
* 不可见的 endpoint 与不存在的 endpoint **回答逐字相同**（存在性不泄露）；
* 撤权在**下一次**调用就生效，而且**在到达 Gateway 之前**就被拦下；
* 超界请求体在到达 Registry 之前就被拒；
* 答复里没有模型正文、没有 prompt 回显、没有 Gateway key。

每一条「拒绝」都配一次**副作用断言**（Gateway 调用计数）。只验状态码的测试看不见
「403 了但已经把调用转发出去了」这种最坏情况。
"""

from __future__ import annotations

import json
import tempfile
import unittest

from backend.bff import invoke as bff_invoke
from backend.bff import route_map as RM
from backend.registry.gateway import GatewayAdapter  # noqa: F401 - protocol shape
from tests.bff import support as S

INVOKE = "/api/v2/ops/endpoints/{endpoint_id}/invoke"


class CountingGateway:
    """一个**受信形状**的 Gateway 替身：记录每次转发，返回带 request_id 的结果。

    ``body`` / ``echo`` 是刻意放的：真实 Gateway 会回正文、测试替身会回显请求。
    这条路由必须把两者都扣下 —— 替身里带着它们，断言才有意义。
    """

    name = "fake-trusted"
    attached = True

    def __init__(self, *, status: int = 200, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._status = status
        self._fail = fail

    def forward(self, *, endpoint, deployment, engine, model, payload, requester=None):
        self.calls.append(
            {
                "endpoint_id": endpoint.id,
                "principal": getattr(requester, "principal", None),
                "payload": payload,
            }
        )
        if self._fail is not None:
            raise self._fail
        return {
            "request_id": "req_synthetic_0000000000000001",
            "synthetic": True,
            "gateway": self.name,
            "forwarded": True,
            "served_by": "gateway",
            "http_status": self._status,
            "model": endpoint.route_path,
            # 下面两个**不得**出现在答复里：
            "body": {"content": "SYNTHETIC-MODEL-BODY"},
            "echo": payload,
            "usage": {"total_tokens": 7, "nested": {"x": 1}},
            "tokens": 7,
            "client_latency_ms": 1.5,
        }


class _HarnessCase(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.gateway = CountingGateway()
        self.h.service.gateway = self.gateway
        self.ref = S.register_baseline(self.h, "alice")
        # Registry 的 P0 调用门（PR #122）要求一次 Gateway 回读确认过的路由；
        # 新建的 Endpoint 停在 ``pending_router_sync``。补上那条**回执成功时本来就会
        # 写下的行**之后，这些用例才是在验「已确认的路由如何被调用」，
        # 而不是在验「未确认的路由被拒绝」（后者有它自己的用例）。
        self.h.acknowledge_route(self.ref["endpoint_id"])

    @property
    def path(self) -> str:
        return INVOKE.format(endpoint_id=self.ref["endpoint_id"])

    def invoke(self, client, *, path=None, payload=None, csrf=True, headers=None):
        extra = dict(headers or {})
        if csrf:
            extra["X-CSRF-Token"] = S.csrf(client)
        return client.run(
            "POST",
            path or self.path,
            payload=payload if payload is not None else {"messages": [{"role": "user", "content": "hi"}]},
            headers=extra,
        )


class RouteShapeTests(unittest.TestCase):
    def test_the_route_is_declared_as_a_write_on_the_registry_source(self):
        spec = RM.ROUTES_BY_NAME["ops.invoke"]
        self.assertEqual(spec.path, "/api/v2/ops/endpoints/{endpoint_id}/invoke")
        self.assertEqual(spec.methods, ("POST",))
        self.assertEqual(spec.auth, RM.AUTH_SESSION)
        self.assertTrue(spec.is_write)
        self.assertTrue(spec.csrf)
        self.assertEqual(spec.source, RM.SOURCE_REGISTRY)
        self.assertEqual(spec.scope, "registry:endpoint:invoke")

    def test_the_pattern_matches_an_endpoint_id_and_not_a_nested_path(self):
        self.assertIs(RM.match("/api/v2/ops/endpoints/ep_1/invoke"), RM.ROUTES_BY_NAME["ops.invoke"])
        self.assertIsNone(RM.match("/api/v2/ops/endpoints/a/b/invoke"))
        # 旧的分享路由没有被这条新路由吃掉。
        self.assertEqual(
            RM.match("/api/v2/ops/deployments/dep_1/share").name, "ops.share"
        )


class IdentityTests(_HarnessCase):
    def test_anonymous_with_a_csrf_token_is_401(self):
        anon = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = self.invoke(anon)
        self.assertEqual(response.status, 401, response.json())
        self.assertEqual(response.error_code(), "no_authenticated_session")
        self.assertEqual(self.gateway.calls, [])

    def test_anonymous_without_csrf_hits_the_other_gate_first(self):
        """两道门都关着时先遇到哪一道，是既有约定，不是这条路由的新行为。

        ``/api/v2/ops/deployments/{id}/share`` 也一样：没有 CSRF 的匿名写请求得到
        403（``_preflight``），带上合法 CSRF 才走到身份那道门得到 401。测试两个都钉，
        免得将来有人「统一」成其中一个而不知道另一个被谁依赖。
        """
        anon = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = self.invoke(anon, csrf=False)
        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(self.gateway.calls, [])

    def test_a_forged_identity_header_is_400(self):
        client = self.h.client("alice")
        response = self.invoke(client, headers={"X-UID": "4001"})
        self.assertEqual(response.status, 400, response.json())
        self.assertEqual(response.error_code(), "identity_assertion_rejected")
        self.assertEqual(self.gateway.calls, [])

    def test_no_query_parameter_can_name_a_subject_or_a_target(self):
        client = self.h.client("alice")
        for query in (
            "uid=0",
            "owner=x",
            "principal=x",
            "key=other-key",
            "url=http://127.0.0.1:8080/v1/chat/completions",
            "path=../etc/passwd",
            "promql=up",
            "source=live",
        ):
            with self.subTest(query=query):
                response = self.invoke(client, path=self.path + "?" + query)
                self.assertEqual(response.status, 400, response.json())
                self.assertIn(
                    response.error_code(),
                    ("proxy_assertion_rejected", "source_assertion_rejected"),
                )
        self.assertEqual(self.gateway.calls, [])

    def test_a_chat_role_field_in_the_payload_is_not_an_identity_assertion(self):
        """``role`` 在模型载荷里是发言人角色，不是「我是个管理员」。"""
        client = self.h.client("alice")
        response = self.invoke(
            client, payload={"messages": [{"role": "system", "content": "be brief"}]}
        )
        self.assertEqual(response.status, 200, response.json())


class CsrfTests(_HarnessCase):
    def test_a_session_without_csrf_is_403_and_nothing_is_forwarded(self):
        client = self.h.client("alice")
        response = self.invoke(client, csrf=False)
        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(self.gateway.calls, [])

    def test_a_cross_origin_write_is_refused(self):
        client = self.h.client("alice")
        response = client.run(
            "POST",
            self.path,
            payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client), "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(self.gateway.calls, [])


class EndpointVisibilityTests(_HarnessCase):
    def test_an_invisible_endpoint_and_a_missing_one_answer_identically(self):
        """存在性不泄露：别人的私有 endpoint 与根本不存在的那一个，答复**同形**。

        两条答复里唯一的差别是**调用方自己发过去的那个 id** 被原样回显 —— 那是他
        已经知道的东西，不是关于对方的事实。所以这里把 id 归一化之后比较整份错误体：
        除输入之外，没有任何一处能区分「存在但你无权」与「根本不存在」。
        """
        outsider = self.h.client("bob")
        invisible = self.invoke(outsider)
        missing = self.invoke(
            outsider, path=INVOKE.format(endpoint_id="ep_does_not_exist")
        )
        self.assertEqual(invisible.status, 404, invisible.json())
        self.assertEqual(invisible.status, missing.status)
        self.assertEqual(invisible.error_code(), missing.error_code())
        self.assertEqual(
            invisible.json()["error"]["message"].replace(self.ref["endpoint_id"], "<id>"),
            missing.json()["error"]["message"].replace("ep_does_not_exist", "<id>"),
        )
        self.assertEqual(self.gateway.calls, [])

    def test_the_owner_can_invoke_and_the_trusted_principal_reaches_the_gateway(self):
        client = self.h.client("alice")
        response = self.invoke(client)
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(
            self.gateway.calls[0]["principal"], self.h.principal_of("alice")
        )


class ShareRevokeTests(_HarnessCase):
    def test_a_grantee_can_invoke_and_revocation_denies_the_very_next_call(self):
        owner = self.h.client("alice")
        grantee = self.h.client("bob")

        shared = S.share(
            owner,
            self.ref["deployment_id"],
            mode="selected",
            principals=[self.h.principal_of("bob")],
        )
        self.assertEqual(shared.status, 200, shared.json())

        first = self.invoke(grantee)
        self.assertEqual(first.status, 200, first.json())
        self.assertEqual(len(self.gateway.calls), 1)
        # 转发用的是 **B 自己的** 受信主体，不是请求里能写的任何东西。
        self.assertEqual(
            self.gateway.calls[0]["principal"], self.h.principal_of("bob")
        )

        revoked = S.share(owner, self.ref["deployment_id"], mode="private")
        self.assertEqual(revoked.status, 200, revoked.json())

        after = self.invoke(grantee)
        self.assertEqual(after.status, 404, after.json())
        self.assertEqual(
            len(self.gateway.calls), 1, "a revoked caller must be stopped BEFORE the gateway"
        )


class PayloadBoundTests(_HarnessCase):
    def test_an_oversized_body_is_413_before_the_registry_sees_it(self):
        client = self.h.client("alice")
        big = json.dumps(
            {"messages": [{"role": "user", "content": "x" * (self.h.app.cfg.max_request_bytes + 64)}]}
        )
        response = client.run(
            "POST",
            self.path,
            raw_body=big.encode(),
            headers={
                "X-CSRF-Token": S.csrf(client),
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(response.status, 413, response.json())
        self.assertEqual(self.gateway.calls, [])

    def test_the_effective_bound_is_the_smaller_of_two(self):
        """BFF 的整请求上界**小于** Registry 的 64 KiB，所以实际生效的是前者。"""
        self.assertLess(
            self.h.app.cfg.max_request_bytes, bff_invoke.REGISTRY_MAX_PAYLOAD_BYTES
        )

    def test_a_non_object_body_is_400(self):
        client = self.h.client("alice")
        for raw in (b"[]", b'"text"', b"not json"):
            with self.subTest(raw=raw):
                response = client.run(
                    "POST",
                    self.path,
                    raw_body=raw,
                    headers={
                        "X-CSRF-Token": S.csrf(client),
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(response.status, 400, response.json())
        self.assertEqual(self.gateway.calls, [])


class ReadinessTests(unittest.TestCase):
    def test_a_registered_but_unverified_endpoint_is_refused(self):
        """注册 != 可用：没通过探测的 endpoint 不可调用（409 ``registry.not_verified``）。"""
        h = S.build_harness(
            state_dir=tempfile.mkdtemp(), prober_healthy=False, mock_gateway=False
        )
        self.addCleanup(h.close)
        gateway = CountingGateway()
        h.service.gateway = gateway
        ref = S.register_baseline(h, "alice")
        client = h.client("alice")
        response = client.run(
            "POST",
            INVOKE.format(endpoint_id=ref["endpoint_id"]),
            payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client)},
        )
        self.assertEqual(response.status, 409, response.json())
        self.assertEqual(response.error_code(), "registry.not_verified")
        self.assertEqual(gateway.calls, [])

    def test_a_detached_gateway_is_503_not_a_fake_success(self):
        h = S.build_harness(
            state_dir=tempfile.mkdtemp(), prober_healthy=True, mock_gateway=False
        )
        self.addCleanup(h.close)
        ref = S.register_baseline(h, "alice")
        client = h.client("alice")
        response = client.run(
            "POST",
            INVOKE.format(endpoint_id=ref["endpoint_id"]),
            payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client)},
        )
        self.assertEqual(response.status, 503, response.json())
        self.assertEqual(response.error_code(), "registry.upstream_unavailable")


class ResponseRedactionTests(_HarnessCase):
    def _data(self):
        response = self.invoke(self.h.client("alice"))
        self.assertEqual(response.status, 200, response.json())
        return response, response.json()["data"]

    def test_the_model_body_and_the_prompt_echo_are_withheld(self):
        response, data = self._data()
        self.assertNotIn("body", data["result"])
        self.assertNotIn("echo", data["result"])
        blob = response.body.decode()
        self.assertNotIn("SYNTHETIC-MODEL-BODY", blob)
        self.assertNotIn('"content"', blob)

    def test_request_id_provenance_is_surfaced(self):
        _, data = self._data()
        self.assertEqual(data["result"]["request_id"], "req_synthetic_0000000000000001")
        self.assertEqual(data["result"]["served_by"], "gateway")
        self.assertIs(data["result"]["synthetic"], True)

    def test_usage_is_reduced_to_integers(self):
        _, data = self._data()
        self.assertEqual(data["result"]["usage"], {"total_tokens": 7})

    def test_no_credential_or_upstream_address_appears(self):
        response, _ = self._data()
        blob = response.body.decode()
        for needle in ("Bearer", "Authorization", "127.0.0.1", ":8080", "api_key", "sk-"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, blob)

    def test_every_result_value_is_a_scalar_or_a_shallow_int_map(self):
        _, data = self._data()
        for key, value in data["result"].items():
            with self.subTest(field=key):
                if key == "usage":
                    self.assertIsInstance(value, dict)
                    for nested in value.values():
                        self.assertIsInstance(nested, int)
                else:
                    self.assertNotIsInstance(value, (dict, list, tuple, set))


class RouterSyncProvenanceTests(_HarnessCase):
    def test_the_route_reports_the_router_state_as_a_registry_fact(self):
        """``router_sync`` 原样转述 Registry 的行级视图：``applied`` 只由回读产生。

        夹具里这条 Endpoint 已经有过一次 **Gateway 回读确认**
        （``Harness.acknowledge_route``），所以如实报 ``router_sync_applied``。
        没有这次确认时它报的是 ``pending_router_sync`` —— 那是下一组的主题。
        """
        _, data = self._data_after_invoke()
        sync = data["router_sync"]
        self.assertIsNotNone(sync)
        self.assertEqual(sync["state"], "router_sync_applied")
        self.assertIs(sync["pending"], False)
        self.assertIs(sync["applied"], True)
        self.assertEqual(sync["evidence"], "gateway_readback")

    def _data_after_invoke(self):
        response = self.invoke(self.h.client("alice"))
        self.assertEqual(response.status, 200, response.json())
        return response, response.json()["data"]


class PendingRouteTests(unittest.TestCase):
    """**没有被 Gateway 回读确认的路由，不许被说成已生效、也不许被转发。**

    这一类用例**故意**不调用 ``acknowledge_route``：它们要的正是
    ``pending_router_sync`` 那条 Endpoint。
    """

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.gateway = CountingGateway()
        self.h.service.gateway = self.gateway
        self.ref = S.register_baseline(self.h, "alice")

    @property
    def path(self) -> str:
        return INVOKE.format(endpoint_id=self.ref["endpoint_id"])

    def test_a_pending_route_is_never_reported_as_applied_and_never_forwarded(self):
        """这条替换了此前那条 ``@unittest.expectedFailure``。

        它不再是「期望红」，而是**两个层面都成立**的正向安全断言：

        * 若本树的 ``RegistryService.invoke`` 带**调用门**（Registry P0，PR #122）：
          ``pending_router_sync`` 的 Endpoint 必须在 **Gateway 之前**被 409 拒绝，
          且 ``gateway.calls == 0`` —— 这是真正的安全正向断言；
        * 若本树还没有那道门（本分支单独存在时就是这样）：BFF 至少必须保证
          **不谎报** —— ``router_sync.applied`` 仍是 ``false``，``state`` 仍是
          ``pending_router_sync``。声明永远不超前于证据。

        探针看的是那道门本身（``_require_route_applied``）。它是**跨 PR 依赖的
        能力探针**，不是安全判定：判定永远在 Registry 那边，BFF 不放宽也不代替。
        与 ``expectedFailure`` 的区别很重要 —— expectedFailure 会把「红」记成绿；
        这里两条分支都是真断言，任何一条不成立都真红。
        """
        gate_present = callable(getattr(self.h.service, "_require_route_applied", None))
        client = self.h.client("alice")
        response = client.run(
            "POST", self.path, payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client)},
        )

        if gate_present:
            self.assertEqual(response.status, 409, response.json())
            self.assertEqual(response.error_code(), "registry.not_verified")
            self.assertEqual(
                self.gateway.calls, [], "a pending route must be stopped before the Gateway"
            )
            return

        self.assertEqual(response.status, 200, response.json())
        sync = response.json()["data"]["router_sync"]
        self.assertEqual(sync["state"], "pending_router_sync")
        self.assertIs(sync["applied"], False)
        self.assertIs(sync["pending"], True)

    def test_an_attested_route_is_never_forwarded_and_never_reported_as_applied(self):
        """``router_sync_attested`` 说的是「代数追平了，证据是**运维说的**」。

        V1 的口径是：只有 Gateway 回读（revision **与** snapshot SHA 逐位相等）才叫
        已切流；一句运维回执不是。所以这条路由同样不许被转发，也**不许**在答复里被
        报成 ``applied``。

        与上一条同样的两分支写法：有调用门（#122）时必须 409 且一次都不打 Gateway；
        没有那道门时，BFF 至少不许谎报 —— ``state`` 仍是 attested、``applied`` 仍是
        false。
        """
        from backend.registry import models as registry_models

        endpoint = self.h.service.store.endpoint_by_id(self.ref["endpoint_id"])
        outcome = self.h.service.store.ack_router_generation(
            self.ref["endpoint_id"],
            generation=endpoint.revision,
            actor_principal="operator:synthetic",
            actor_uid=0,
            at=self.h.service._now(),
            evidence=registry_models.ROUTER_EVIDENCE_OPERATOR_ATTESTATION,
        )
        self.assertEqual(outcome.get("outcome"), "ok", outcome)
        # 一个只报同一代数的 Router，确保被拒的原因是**证据类别**而不是代数。
        self.h.service.router = S._FixedReadbackRouter(endpoint.revision)

        gate_present = callable(getattr(self.h.service, "_require_route_applied", None))
        client = self.h.client("alice")
        response = client.run(
            "POST", self.path, payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client)},
        )

        if gate_present:
            self.assertEqual(response.status, 409, response.json())
            self.assertEqual(response.error_code(), "registry.not_verified")
            self.assertEqual(
                self.gateway.calls, [], "an attested route must be stopped before the Gateway"
            )
            return

        self.assertEqual(response.status, 200, response.json())
        sync = response.json()["data"]["router_sync"]
        self.assertEqual(sync["state"], "router_sync_attested")
        self.assertIs(sync["applied"], False)

    def test_the_gate_probe_matches_the_authority_it_probes(self):
        """探针必须真的反映 Registry 的实现，而不是一个恒假的字符串检查。"""
        gate_present = callable(getattr(self.h.service, "_require_route_applied", None))
        source = __import__("inspect").getsource(type(self.h.service).invoke)
        self.assertEqual(gate_present, "_require_route_applied" in source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
