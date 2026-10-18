"""V1-68 Wave-5：把 #114 的两条 absence 断言**在新增测试里变成正向**，并补严格门。

#114 用两条可执行断言记录了当时的两处装配缝：

1. 同源 BFF 没有 invoke 路由（``POST /api/v2/registry/...`` → 404）；
2. Registry 不接受 IAM 的 ``ScopedTokenService``（``resolve_requester`` 抛错）。

源补丁随后出现：BFF **#115 @ 7d29431**（+ 目录 #120 @ cece933）与 Registry **#116 @
769825b**。本文件的职责是：**不改那两条旧断言**（它们是历史事实，而且在新的门里仍然
成立），而是在新增测试里断言接缝**已经闭合**之后应当成立的正向行为。

# 合成边界

* 身份与 token：**真的** ``ScopedTokenService``（真实签发/撤销）、**真的**
  ``TrustedTokenIntrospector``、**真的** BFF 路由守卫；
* Gateway：回环上的 ``SyntheticGateway`` 替身 + 真实 ``TrustedGatewayAdapter``；
* Router：``ConfirmingRouterAdapter`` 只按快照回读；
* 语料：不涉及真实 usage.jsonl / prompt / key。**没有任何 Dell 现场事实**。

# 依赖状态（历史 → 当前）

其中若干用例最初是**预期红**：它们分别指向当时尚未落地的依赖，且从未用
``skip`` / ``expectedFailure`` 掩盖。在公开集成候选 ``5ea33c9``（wave14）上，
这些依赖都已闭合，因此本文件在该候选上 **45/45 全绿**：

| 曾待落地的依赖 | 当前 |
| --- | --- |
| Registry #76 P0：invoke 的 router-applied 硬门 | **已落地**（``_require_route_applied``）；pending / attested 两条负例现在**真的**被拒且零 Gateway 调用 |
| Registry #116 ``KNOWN_SCOPES`` 词表 | **已对齐**；真实 IAM token（含 portal scope）可被 ``TrustedTokenIntrospector`` 确认 |
| BFF #115 写前置对 Bearer 的 CSRF 要求 | **已收敛**；scoped token 可调用同源 invoke |

这些用例保留的是**负例语义**：断言拒绝与零 Gateway 调用，而不是「依赖缺位时恰好红」。
"""

from __future__ import annotations

import dataclasses
import unittest

from tests.bff import support as S
from tests.registry.test_trusted_gateway_and_router import ConfirmingRouterAdapter
from tests.v1_vertical.test_v1_68_offline_vertical import (
    A,
    B,
    C,
    UIDS,
    Vertical,
)

from backend.registry import errors as registry_errors
from backend.registry.identity import PrincipalDirectory, resolve_requester

# ``TokenAuthorityUnavailable`` / ``TrustedTokenIntrospector`` 由 **#116** 引入。
# 本文件 stack 在 **#114** 上，而 #116 尚未合入那里；把它们写成模块级 import 会让
# 「依赖缺失」表现成一次**收集失败**（整包 0 项），而不是几条指名到姓的失败。
# 局部 import 让单独运行本分支时每条用例各自报出「缺哪个符号」，而在 TEMP union
# 上行为不变。这不是 skip，也不是 expectedFailure：红就是红。

INVOKE_TEMPLATE = "/api/v2/ops/endpoints/{}/invoke"
LEGACY_REGISTRY_TEMPLATE = "/api/v2/registry/endpoints/{}/invoke"
#: 真实 IAM token 的 registry 子集：用来把「接缝本身是否工作」与
#: 「跨模块 scope 会不会把整条 token 拒掉」两件事分开。
REGISTRY_SCOPES = ("registry:read", "registry:endpoint:invoke")


class _InvokeBase(unittest.TestCase):
    trusted_gateway = True

    def setUp(self) -> None:
        self.v = Vertical(self, trusted_gateway=self.trusted_gateway)
        self.endpoint_id = self.v.ids["endpoint_id"]
        self.share_to_b()

    def share_to_b(self, deployment_id: str | None = None):
        target = deployment_id or self.v.ids["deployment_id"]
        response = S.share(
            self.v.browser(A), target, mode="selected", principals=["4003"]
        )
        self.assertEqual(response.status, 200, response.json())
        return response

    def publish(self):
        self.v.publish_engine()
        self.v.publish_snapshot()

    def invoke(self, client, *, payload=None, headers=None):
        return client.run(
            "POST",
            INVOKE_TEMPLATE.format(self.endpoint_id),
            payload={"messages": []} if payload is None else payload,
            headers=headers,
        )

    def browser_invoke(self, name: str, **kwargs):
        client = self.v.browser(name)
        return self.invoke(client, headers={"X-CSRF-Token": S.csrf(client)}, **kwargs)

    def gateway_calls(self) -> int:
        return len(self.v.gateway_double.calls)


# ---------------------------------------------------------------------------
# 正向：同源 invoke 路由存在，并经 Registry 的权威路径转发
# ---------------------------------------------------------------------------


class SameOriginInvokePositiveTest(_InvokeBase):
    def setUp(self) -> None:
        super().setUp()
        # 正向夹具：先让这条路由拥有一次 **Gateway 回读确认**（候选的 #122 门
        # 要求 applied + gateway_readback + 快照内）。负例（pending / attested /
        # stale）不经过这里，仍由各自用例的初始状态断言。
        self.v.h.acknowledge_route(self.endpoint_id)
        self.publish()

    def test_b_invokes_over_the_same_origin_route_with_session_and_csrf(self) -> None:
        """#114 的 absence 断言 → 正向：这条路现在存在，且真的走 Registry。"""
        response = self.browser_invoke(B)

        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        self.assertEqual(data["schema_version"], "portal-invoke/1")
        self.assertEqual(data["endpoint_id"], self.endpoint_id)
        self.assertEqual(data["gateway"]["adapter"], "trusted_http")
        self.assertEqual(data["gateway"]["authorization"], "server_side_enforced")
        self.assertTrue(data["result"]["forwarded"])
        self.assertTrue(data["result"]["synthetic"])
        # 真实的 request id 由 Gateway 路径产生，不是编出来的。
        self.assertTrue(data["result"]["request_id"].startswith("req_"))
        self.assertTrue(data["metering"]["recorded"])

    def test_the_gateway_saw_bs_key_not_as_key(self) -> None:
        self.browser_invoke(B)

        self.assertEqual(self.gateway_calls(), 1)
        call = self.v.gateway_double.calls[0]
        self.assertEqual(call["authorization"], "Bearer synthetic-bob-key")

    def test_the_sanctioned_route_is_under_ops_and_the_legacy_prefix_stays_unmounted(self) -> None:
        """两条路径分开断言：新入口在 ``/api/v2/ops/...``，旧的
        ``/api/v2/registry/...`` **仍然**不挂（#114 的旧断言继续成立）。"""
        self.assertEqual(self.browser_invoke(B).status, 200)

        issued = self.v.issue(B)
        legacy = self.v.token_client().run(
            "POST",
            LEGACY_REGISTRY_TEMPLATE.format(self.endpoint_id),
            headers={"Authorization": f"Bearer {issued.token}"},
            payload={"messages": []},
        )
        self.assertEqual(legacy.status, 404, legacy.json())

    def test_the_response_withholds_model_body_and_echo(self) -> None:
        """收窄是产品决定：元数据可以下发，正文/回显不行。"""
        data = self.browser_invoke(B).json()["data"]

        self.assertNotIn("body", data["result"])
        self.assertNotIn("echo", data["result"])

    def test_a_write_without_csrf_is_refused_before_the_gateway(self) -> None:
        client = self.v.browser(B)
        before = self.gateway_calls()
        response = self.invoke(client, payload={"messages": []})

        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(response.json()["error"]["code"], "csrf_rejected")
        self.assertEqual(self.gateway_calls(), before)

    def test_a_cross_origin_write_is_refused_before_the_gateway(self) -> None:
        client = self.v.browser(B)
        before = self.gateway_calls()
        response = self.invoke(
            client,
            headers={"X-CSRF-Token": S.csrf(client), "Origin": "https://evil.example"},
        )

        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(self.gateway_calls(), before)

    def test_an_oversized_payload_is_413_and_not_forwarded(self) -> None:
        before = self.gateway_calls()
        response = self.browser_invoke(
            B, payload={"messages": [{"role": "user", "content": "x" * 200_000}]}
        )

        self.assertEqual(response.status, 413, response.json())
        self.assertEqual(response.json()["error"]["code"], "payload_too_large")
        self.assertEqual(self.gateway_calls(), before)

    def test_forged_identity_headers_and_query_are_400(self) -> None:
        client = self.v.browser(B)
        before = self.gateway_calls()

        headers = {
            "X-CSRF-Token": S.csrf(client),
            "X-UID": "4001",
            "X-Owner": self.v.h.principal_of(A),
            "X-Role": "platform_admin",
        }
        self.assertEqual(self.invoke(client, headers=headers).status, 400)

        query = f"{INVOKE_TEMPLATE.format(self.endpoint_id)}?uid=4001&owner=4001"
        forged_query = client.run(
            "POST",
            query,
            payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client)},
        )
        self.assertEqual(forged_query.status, 400, forged_query.json())
        self.assertEqual(self.gateway_calls(), before)

    def test_c_cannot_enumerate_or_invoke(self) -> None:
        before = self.gateway_calls()
        response = self.browser_invoke(C)

        # 不可见与不存在同形：不区分「有这条端点但没共享给你」。
        self.assertEqual(response.status, 404, response.json())
        self.assertEqual(response.json()["error"]["code"], "registry.not_found")
        self.assertEqual(self.gateway_calls(), before)

    def test_revoking_the_grant_denies_the_next_call_and_the_counter_does_not_move(self) -> None:
        self.assertEqual(self.browser_invoke(B).status, 200)
        before = self.gateway_calls()

        revoked = self.v.share(self.v.browser(A), mode="private")
        self.assertEqual(revoked.status, 200, revoked.json())

        denied = self.browser_invoke(B)
        self.assertEqual(denied.status, 404, denied.json())
        # 被拒的调用**没有**再向 Gateway 发第二次请求。
        self.assertEqual(self.gateway_calls(), before)
        self.assertEqual(
            self.v.h.service.store.grantees(self.endpoint_id), ()
        )


# ---------------------------------------------------------------------------
# 严格 V1-03：applied 只能来自 Gateway 回读；attested 不是 applied
# ---------------------------------------------------------------------------


class StrictAppliedReadbackGateTest(_InvokeBase):
    """两条负例是 **DEPENDENCY_PENDING (#76 P0)**：当前 invoke 没有 applied 门。"""

    def _requester_b(self):
        return self.v.requester_from_token(self.v.issue(B))

    def _apply_via_gateway_readback(self) -> dict:
        self.v.h.service.router = ConfirmingRouterAdapter()
        self.v.publish_engine()
        sibling = self.v.ready_sibling_deployment()
        self.v.repoint(A, sibling)
        # repoint 会撤销既有授权，且端点现在指向**新的** deployment；授权要按
        # 新的 deployment 重新显式授予 —— 否则共享调用会以「该部署下没有你的端点」
        # 收场，而不是以「授权已恢复」收场。
        self.share_to_b(deployment_id=sibling)
        self.v.publish_snapshot()
        return self.v.h.service.get_endpoint(
            self.v.h.requester_for(A), self.endpoint_id
        )["data"]

    def test_invoke_succeeds_after_an_applied_gateway_readback(self) -> None:
        view = self._apply_via_gateway_readback()

        self.assertEqual(view["router_sync"]["state"], "router_sync_applied")
        self.assertTrue(view["router_sync"]["applied"])
        self.assertEqual(view["router_sync"]["evidence"], "gateway_readback")
        self.assertEqual(self.v.invoke_as(self._requester_b())["data"]["result"]["forwarded"], True)

    def test_invoke_is_refused_while_the_route_is_only_pending(self) -> None:
        """端点仍是 ``pending_router_sync`` 时，调用必须被拒 —— 否则「已授权」被当成了
        「已切流」。曾待 Registry #76 P0；在 ``5ea33c9`` 上已落地。"""
        self.v.publish_engine()
        self.v.publish_snapshot()
        view = self.v.h.service.get_endpoint(
            self.v.h.requester_for(A), self.endpoint_id
        )["data"]
        self.assertEqual(view["router_sync"]["state"], "pending_router_sync")

        before = self.gateway_calls()
        with self.assertRaises(registry_errors.RegistryError):
            self.v.invoke_as(self._requester_b())
        self.assertEqual(self.gateway_calls(), before)

    def test_an_attested_ack_is_not_applied_and_does_not_open_invoke(self) -> None:
        """运维 ``ack_routing_applied`` 只能挣到 ``router_sync_attested``；它**不**能让人
        调用。曾待 Registry #76 P0；在 ``5ea33c9`` 上已落地。"""
        self.v.publish_engine()
        self.v.publish_snapshot()
        admin = dataclasses.replace(
            self.v.h.requester_for(A),
            scopes=frozenset(self.v.h.requester_for(A).scopes)
            | frozenset({"registry:admin:manage"}),
        )
        acked = self.v.h.service.ack_routing_applied(
            admin, self.endpoint_id, {"generation": 1}
        )
        self.assertEqual(acked["meta"]["router_sync"], "router_sync_attested")
        self.assertFalse(acked["meta"]["router_applied"])

        before = self.gateway_calls()
        with self.assertRaises(registry_errors.RegistryError):
            self.v.invoke_as(self._requester_b())
        self.assertEqual(self.gateway_calls(), before)


# ---------------------------------------------------------------------------
# Registry token 权威：受信 adapter 可以，raw issuer 不行；IAM token 自己要能过
# ---------------------------------------------------------------------------


class RegistryTokenAuthorityTest(_InvokeBase):
    def _introspector(self):
        # #116 专有符号：在本分支上缺失时，这条用例报 ImportError（可读），
        # 而不是让整个模块收集失败。
        from backend.registry.identity import TrustedTokenIntrospector

        principals = self.v.h.principals
        self.assertIsInstance(principals, PrincipalDirectory)
        return TrustedTokenIntrospector(self.v.iam, principals=principals)

    def test_the_trusted_introspector_accepts_a_registry_scoped_iam_token(self) -> None:
        """隔离变量：scope **只含 registry** 时，#116 的接缝确实工作。"""
        issued = self.v.issue(B, scopes=REGISTRY_SCOPES)

        requester = resolve_requester(
            issued.token, self._introspector(), self.v.h.principals
        )

        self.assertEqual(requester.principal, self.v.h.principal_of(B))
        self.assertEqual(requester.uid, UIDS[B])
        self.assertEqual(requester.scopes, frozenset(REGISTRY_SCOPES))

    def test_the_trusted_introspector_confirms_the_iam_token(self) -> None:
        """真实 IAM token 携带 portal scope；曾因 ``KNOWN_SCOPES`` 只含 registry 六个而
        被整条拒绝。在 ``5ea33c9`` 上两条词表已对齐 —— 这不是「凭据不对」，
        是词表对齐问题。"""
        issued = self.v.issue(B)  # 默认 B_SCOPES：含 atlas / portal scope

        requester = resolve_requester(
            issued.token, self._introspector(), self.v.h.principals
        )
        self.assertEqual(requester.principal, self.v.h.principal_of(B))
        self.assertEqual(requester.uid, UIDS[B])

    def test_the_raw_iam_service_is_not_a_registry_authority(self) -> None:
        """**保留的负测**：#114 的原始断言继续成立，并升级为 503 语义 ——
        只有受信 adapter 可以当权威，raw issuer 对象不行。"""
        from backend.registry.identity import TokenAuthorityUnavailable

        issued = self.v.issue(B)

        with self.assertRaises(TokenAuthorityUnavailable) as raised:
            resolve_requester(issued.token, self.v.iam, self.v.h.principals)

        self.assertEqual(
            raised.exception.contract_code, "registry.token_authority_unavailable"
        )
        # 与 Unauthenticated 分开：这是「部署不知道问谁」，不是「这条凭据不对」。
        self.assertNotEqual(
            raised.exception.contract_code, registry_errors.Unauthenticated.contract_code
        )

    def test_b_invokes_with_their_own_scoped_token(self) -> None:
        """同源 invoke 走 ``_write_preflight``；曾对有 ``Authorization`` 而无 session
        cookie 的请求也要求 CSRF，于是 token 通道得到 403。在 ``5ea33c9`` 上已收敛，
        V1-07 的 CLI/token 入口可用。"""
        # 让「token 通道是否可用」成为这条用例**唯一**的变量：先把路由确认好，
        # 否则一个 409 会把 D4（写前置要求 session CSRF）掩盖掉。
        self.v.h.acknowledge_route(self.endpoint_id)
        self.v.publish_engine()
        self.v.publish_snapshot()
        issued = self.v.issue(B)

        response = self.v.token_client().run(
            "POST",
            INVOKE_TEMPLATE.format(self.endpoint_id),
            headers={"Authorization": f"Bearer {issued.token}"},
            payload={"messages": []},
        )

        self.assertEqual(response.status, 200, response.json())
        self.assertTrue(response.json()["data"]["result"]["request_id"].startswith("req_"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
