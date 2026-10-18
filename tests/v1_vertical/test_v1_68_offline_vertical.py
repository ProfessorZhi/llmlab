"""Issue #68 的**离线**跨 Lane 纵切验收（CPU-only，进程内，合成）。

## 这是什么，不是什么

`docs/v1/V1_ACCEPTANCE.md` 的 V1-04..V1-09 要求一条真实的纵向链路：两个不同 UID
的主体、真实 scoped token、真实授权、真实撤权、真实计数。本文件交付的是它的
**离线那一半**：

* **真的**：`backend/auth/tokens` 的 `ScopedTokenService`（真实签发/撤销/轮换）、
  `ScopedTokenPrincipalSource`（BFF 真实 token 通道）、`OpsApplication` 的真实
  路由与守卫、Registry 的真实登记/验证/共享/撤销/路由状态机、
  `TrustedGatewayAdapter` 的真实转发与 key 解析、Atlas 的真实
  `build_self_calls_dto`。
* **假的（且只在测试里）**：PAM 后端（`tests/bff/support` 的合成 broker）、
  回环上的 `SyntheticGateway`（Gateway 替身）、`ConfirmingRouterAdapter`
  （只按快照回读的 Router 替身）、Atlas 的 Gateway 语料（合成 JSONL）。

**没有任何 Dell 现场事实**：唯一的网络是回环上的测试替身，没有出站连接。
每一条断言都不写 LIVE；`transport.synthetic is True` 与
`gateway_attached is False` 是被**断言**的，不是被假设的。

## 两处**阻断的装配缝**（本文件把它们变成失败信息，而不是悄悄绕过）

1. `backend/registry/identity.resolve_requester` 对 `tokens` 做
   `isinstance(TokenDirectory)` **硬校验**，所以 Registry 模块的 WSGI 应用无法接受
   `backend/auth/tokens.ScopedTokenService`。把 IAM token「搬」进一个
   `TokenDirectory` 就是**第二套 token 账本**，本文件不做。
2. BFF 的 `route_map` 不挂载 `/api/v2/registry/*`（`tests/bff/test_route_map.py`
   把它钉在 404），所以**没有同源 invoke 路由**。

因此 B 的调用在本文件里走 **Registry 服务层**，主体/scopes 来自**真实 IAM
introspection**，不是自造的身份。这不是完整的 V1-07；`PARTIAL` 见
`docs/v1/V1_68_OFFLINE_GATE.md`。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import tempfile
import unittest
from datetime import timedelta, timezone

from backend.auth import tokens as auth_tokens
from backend.bff import atlas_adapter as bff_atlas_adapter
from backend.bff import identity as bff_identity
from backend.bff import stacks as bff_stacks
from backend.registry import errors as registry_errors
from backend.registry.gateway import StaticGatewayKeyResolver, TrustedGatewayAdapter
from backend.registry.identity import InvalidRequest, RequesterContext, resolve_requester
from tests.bff import support as S
from tests.registry.test_trusted_gateway_and_router import (
    ConfirmingRouterAdapter,
    SyntheticGateway,
)

A = "alice"       # uid 4001 —— 登记与共享的 owner
B = "bob"     # uid 4003 —— 被共享者
C = "dave"     # uid 4002 —— 无关第三方

UIDS = {"alice": 4001, "bob": 4003, "dave": 4002}

#: B 的 token 只带普通成员 scope；没有 admin、没有 share。
B_SCOPES = (
    "registry:read",
    "registry:endpoint:invoke",
    "observability:self:read",
    "atlas:calls:self:read",
    "portal:modules:read",
)

SEAM_NO_SAME_ORIGIN_INVOKE = (
    "BFF route_map 未挂载 /api/v2/registry/*（tests/bff/test_route_map.py 钉在 404）。"
    "需要 BFF owner (#79) 决定是否提供同源 invoke；"
    "在此之前 B 的调用只能走 Registry 模块或服务层。"
)
SEAM_REGISTRY_TOKEN_AUTHORITY = (
    "backend/registry/identity.resolve_requester 对 tokens 做 isinstance(TokenDirectory) "
    "硬校验，Registry 模块无法接受 backend/auth/tokens.ScopedTokenService。"
    "需要 Registry owner (#76) 提供「受信 introspector」装配点；"
    "把 IAM token 复制进第二个 TokenDirectory 等于第二套 token 账本，本文件不做。"
)


class Vertical:
    """一个进程内的纵向装置：真实 IAM 签发 → BFF 真实路由 → 真实 Registry/Atlas。

    ``trusted_gateway=True`` 时再在回环上起一个 Gateway 替身，并用真实的
    `TrustedGatewayAdapter` 转发 —— 那条路径才会产生真实的 ``request_id``
    （``MockGatewayAdapter`` 不产生，它只是一个返回固定 JSON 的替身）。
    """

    def __init__(
        self,
        case: unittest.TestCase,
        *,
        mock_gateway: bool = True,
        trusted_gateway: bool = False,
        with_observability: bool = True,
    ) -> None:
        self.h = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            mock_gateway=mock_gateway,
            with_observability=with_observability,
        )
        case.addCleanup(self.h.close)
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="v1-68-iam-"))
        case.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        self.gateway_double = None
        if trusted_gateway:
            self.gateway_double = SyntheticGateway(
                keys={
                    "synthetic-alice-key": A,
                    "synthetic-bob-key": B,
                }
            )
            case.addCleanup(self.gateway_double.stop)
            # key 目录按**完整 principal** 索引：BFF harness 的 principal 是
            # ``linux:<host>:<uid>``，不是登录名。
            self.h.service.gateway = TrustedGatewayAdapter(
                base_url=self.gateway_double.base_url,
                keys=StaticGatewayKeyResolver(
                    {
                        self.h.principal_of(A): "synthetic-alice-key",
                        self.h.principal_of(B): "synthetic-bob-key",
                    }
                ),
            )

        self.iam = auth_tokens.ScopedTokenService(
            auth_tokens.TokenStore(self._tmp / "scoped-tokens.sqlite3"),
            principal_state=auth_tokens.MappingPrincipalState(
                {
                    self.h.principal_of(name): auth_tokens.STATE_APPROVED
                    for name in (A, B, C)
                }
            ),
        )
        self.iam.initialize()
        # IAM 的真服务接进 BFF 的真实 token 通道（一行装配，见该类的 docstring）。
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=self.iam,
            accounts=self.h.accounts,
            enabled=True,
        )
        self.ids = S.register_baseline(self.h, A)
        self._sibling_counter = 0

    # -- 身份 -------------------------------------------------------------

    def issue(self, name: str, scopes=B_SCOPES):
        return self.iam.issue(
            principal=self.h.principal_of(name),
            uid=UIDS[name],
            linux_user=name,
            scopes=scopes,
            roles=("member",),
            label=f"v1-68-{name}",
        )

    def requester_from_token(self, issued) -> RequesterContext:
        """把 IAM 的真实 introspection 结果翻成 Registry 的 ``RequesterContext``。

        这与 `backend/bff/identity.RegistryTokenIntrospector` 是**同一个方向**的
        转换：字段全部来自受信权威，测试不发明任何身份。它存在的原因见
        ``SEAM_REGISTRY_TOKEN_AUTHORITY``。
        """
        subject = self.iam.introspect(issued.token)
        assert subject.active, subject.reason
        return RequesterContext(
            principal=subject.principal,
            uid=subject.uid,
            linux_user=subject.linux_user,
            scopes=frozenset(subject.scopes),
            roles=frozenset(subject.roles),
            token_label=subject.label,
        )

    # -- HTTP -------------------------------------------------------------

    def token_client(self):
        return S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def browser(self, name: str):
        return self.h.client(name)

    def share(self, client, *, mode: str, principals=None):
        return S.share(
            client, self.ids["deployment_id"], mode=mode, principals=principals
        )

    # -- Registry ---------------------------------------------------------

    def publish_engine(self) -> str:
        """让 engine 进入可发布状态；否则快照为空，任何 ACK 都没有意义。"""
        engines = self.h.service.store.list_engines()
        assert engines, "publish_engine() 之前必须先登记一个 engine"
        refs = dict(self.h.service.config.engine_secret_refs)
        refs[engines[0].id] = {"file": "/etc/llmlab/engines.env", "var": "ENGINE_KEY"}
        self.h.service.config = dataclasses.replace(
            self.h.service.config, engine_secret_refs=refs
        )
        return engines[0].id

    def ready_sibling_deployment(self) -> str:
        """在同一 engine 上再建一个 ready deployment（repoint 的合法目标）。"""
        svc = self.h.service
        requester = self.h.requester_for(A)
        base = svc.store.deployment_by_id(self.ids["deployment_id"])
        self._sibling_counter += 1
        suffix = self._sibling_counter
        model, _ = svc.register_model(requester, {"name": f"model-sibling-{suffix}"})
        deployment, _ = svc.register_deployment(
            requester,
            {
                "name": f"deployment-sibling-{suffix}",
                "engine_id": base.engine_id,
                "model_id": model["data"]["id"],
            },
        )
        svc.verify_deployment(requester, deployment["data"]["id"])
        return deployment["data"]["id"]

    def repoint(self, principal: str, deployment_id: str):
        svc = self.h.service
        requester = self.h.requester_for(principal)
        revision = svc.store.endpoint_by_id(self.ids["endpoint_id"]).revision
        return svc.repoint_endpoint(
            requester,
            self.ids["endpoint_id"],
            {"target_deployment_id": deployment_id, "expected_revision": revision},
        )

    def invoke_as(self, requester: RequesterContext, payload: dict | None = None):
        return self.h.service.invoke(
            requester,
            self.ids["endpoint_id"],
            payload
            if payload is not None
            else {"model": f"model-{A}", "messages": [{"role": "user", "content": "hi"}]},
        )

    def publish_snapshot(self) -> dict:
        """把当前快照交给回环 Gateway 替身（模拟「Gateway 已加载」）。"""
        assert self.gateway_double is not None
        snapshot = self.h.service._routing_snapshot()
        assert snapshot["endpoints"], snapshot.get("excluded")
        self.gateway_double.set_snapshot(snapshot)
        return snapshot


# ---------------------------------------------------------------------------
# 0. 装配缝的真实形状：能组合的、不能组合的，各有一条可执行的断言
# ---------------------------------------------------------------------------


class SeamInventoryTest(unittest.TestCase):
    """别把「组合不起来」留成一句口头假设。"""

    def setUp(self) -> None:
        self.v = Vertical(self)

    def test_a_real_iam_token_authenticates_to_the_bff(self) -> None:
        issued = self.v.issue(B)
        response = self.v.token_client().run(
            "GET", "/api/v2/ops", headers={"Authorization": f"Bearer {issued.token}"}
        )
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(S.ops_of(response)["scope"]["subject"], self.v.h.principal_of(B))

    def test_the_same_token_reaches_the_atlas_calls_route(self) -> None:
        issued = self.v.issue(B)
        response = self.v.token_client().run(
            "GET", "/api/atlas/calls", headers={"Authorization": f"Bearer {issued.token}"}
        )
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(
            response.json()["data"]["scope"]["subject"], self.v.h.principal_of(B)
        )

    def test_forged_and_absent_credentials_are_401(self) -> None:
        client = self.v.token_client()
        forged = client.run(
            "GET", "/api/v2/ops", headers={"Authorization": "Bearer not-a-real-token"}
        )
        self.assertEqual(forged.status, 401, forged.json())
        absent = client.run("GET", "/api/v2/ops")
        self.assertEqual(absent.status, 401, absent.json())

    def test_the_same_origin_bff_has_no_invoke_route(self) -> None:
        """阻断缝 #1（可执行形式）：同源 BFF 不提供 Registry invoke。"""
        issued = self.v.issue(B)
        response = self.v.token_client().run(
            "POST",
            f"/api/v2/registry/endpoints/{self.v.ids['endpoint_id']}/invoke",
            headers={"Authorization": f"Bearer {issued.token}"},
            payload={"messages": []},
        )
        self.assertEqual(response.status, 404, response.json())
        self.assertIn("no route", response.json()["error"]["message"])
        self.assertIn("#79", SEAM_NO_SAME_ORIGIN_INVOKE)

    def test_the_registry_module_refuses_the_iam_token_service(self) -> None:
        """阻断缝 #2（可执行形式）：Registry 只认它自己的 ``TokenDirectory``。"""
        issued = self.v.issue(B)
        with self.assertRaises(InvalidRequest) as raised:
            resolve_requester(issued.token, self.v.iam, self.v.h.principals)
        self.assertIn("TokenDirectory", str(raised.exception))
        self.assertIn("#76", SEAM_REGISTRY_TOKEN_AUTHORITY)


# ---------------------------------------------------------------------------
# V1-04 身份与边界：两个 UID、伪造头、CSRF、origin、replay、revision
# ---------------------------------------------------------------------------


class V104IdentityBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Vertical(self)

    def test_anonymous_is_401_on_every_mounted_data_route(self) -> None:
        client = self.v.token_client()
        for path in (
            "/api/v2/ops",
            "/api/v2/ops/usage",
            "/api/atlas/calls",
            "/api/portal/capabilities",
            "/api/portal/services",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.run("GET", path).status, 401)

    def test_forged_identity_headers_are_refused_on_every_data_route(self) -> None:
        issued = self.v.issue(B)
        client = self.v.token_client()
        for path in ("/api/v2/ops", "/api/atlas/calls"):
            with self.subTest(path=path):
                response = client.run(
                    "GET",
                    path,
                    headers={
                        "Authorization": f"Bearer {issued.token}",
                        "X-UID": "4001",
                        "X-Owner": self.v.h.principal_of(A),
                        "X-Role": "platform_admin",
                    },
                )
                self.assertEqual(response.status, 400, response.json())

    def test_a_write_without_csrf_is_refused_and_changes_nothing(self) -> None:
        alice = self.v.browser(A)
        response = alice.run(
            "PUT",
            f"/api/v2/ops/deployments/{self.v.ids['deployment_id']}/share",
            payload={"mode": "selected", "principals": ["4003"]},
        )
        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(
            self.v.h.service.store.grantees(self.v.ids["endpoint_id"]), ()
        )

    def test_a_stale_origin_is_refused_before_anything_else(self) -> None:
        alice = self.v.browser(A)
        token = S.csrf(alice)
        alice.host = "stale-portal.internal"
        response = alice.run("GET", "/api/v2/ops", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status, 421, response.json())

    def test_an_idempotent_replay_returns_the_same_object(self) -> None:
        requester = self.v.h.requester_for(A)
        body = {
            "name": "engine-replay",
            "target_url": S.ENGINE_TARGET_URL,
            "protocol": "openai_compatible",
        }
        first, created = self.v.h.service.register_engine(
            requester, dict(body), idempotency_key="v1-68-replay"
        )
        second, replayed = self.v.h.service.register_engine(
            requester, dict(body), idempotency_key="v1-68-replay"
        )
        self.assertTrue(created)
        self.assertFalse(replayed)
        self.assertEqual(first["data"]["id"], second["data"]["id"])

    def test_a_revision_mismatch_on_repoint_is_409_and_changes_nothing(self) -> None:
        target = self.v.ready_sibling_deployment()
        before = self.v.h.service.store.endpoint_by_id(self.v.ids["endpoint_id"])
        with self.assertRaises(registry_errors.RevisionConflict):
            self.v.h.service.repoint_endpoint(
                self.v.h.requester_for(A),
                self.v.ids["endpoint_id"],
                {"target_deployment_id": target, "expected_revision": before.revision + 7},
            )
        after = self.v.h.service.store.endpoint_by_id(self.v.ids["endpoint_id"])
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.deployment_id, before.deployment_id)


# ---------------------------------------------------------------------------
# V1-05 路由就绪：Registry 状态机 + 真实 FakeRouter 回读
# ---------------------------------------------------------------------------


class V105RouteReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Vertical(self)

    def test_registration_readiness_and_routing_are_separate_axes(self) -> None:
        endpoint = self.v.h.service.store.endpoint_by_id(self.v.ids["endpoint_id"])
        view = self.v.h.service.get_endpoint(
            self.v.h.requester_for(A), self.v.ids["endpoint_id"]
        )["data"]
        self.assertTrue(view["registered"])
        self.assertTrue(view["ready"])
        self.assertTrue(view["pending_router_sync"])
        self.assertEqual(view["router_sync"]["state"], "pending_router_sync")
        self.assertFalse(view["router_sync"]["applied"])
        self.assertIsNone(view["router_sync"]["evidence"])
        self.assertEqual(endpoint.revision, 1)

    def test_router_readiness_requires_an_actual_ack_of_an_included_snapshot(self) -> None:
        self.v.h.service.router = ConfirmingRouterAdapter()
        self.v.publish_engine()
        target = self.v.ready_sibling_deployment()
        view = self.v.repoint(A, target)

        report = view["meta"]["router_sync_report"]
        self.assertTrue(report["published"])
        # 回执绑定的是**这次快照里真的发布了的那一条**。
        self.assertEqual(report["acked_endpoints"], [self.v.ids["endpoint_id"]])
        self.assertEqual(view["meta"]["router_sync"], "router_sync_applied")
        self.assertTrue(view["meta"]["router_applied"])
        self.assertEqual(view["data"]["router_sync"]["evidence"], "gateway_readback")

    def test_without_a_router_adapter_the_same_flow_stays_pending(self) -> None:
        """对照组：同样的 repoint，缺省 detached adapter 不许说已切流。"""
        target = self.v.ready_sibling_deployment()
        view = self.v.repoint(A, target)
        self.assertEqual(view["meta"]["router_sync"], "pending_router_sync")
        self.assertFalse(view["meta"]["router_applied"])
        self.assertEqual(
            view["meta"]["router_sync_report"]["reason"],
            "no_router_adapter_in_this_deployment",
        )

    def test_a_fake_router_never_implies_live(self) -> None:
        """替身回读成功 = 「替身说它加载了」，不是 LIVE。"""
        self.v.h.service.router = ConfirmingRouterAdapter()
        self.v.publish_engine()
        self.v.repoint(A, self.v.ready_sibling_deployment())

        issued = self.v.issue(A)
        client = self.v.token_client()
        ops = S.ops_of(
            client.run(
                "GET", "/api/v2/ops", headers={"Authorization": f"Bearer {issued.token}"}
            )
        )
        self.assertEqual(ops["scope"]["subject"], self.v.h.principal_of(A))
        self.assertNotIn("LIVE", json.dumps(ops))
        self.assertTrue(ops["registry"]["owned"] or ops["registry"]["shared"])

        calls = client.run(
            "GET", "/api/atlas/calls", headers={"Authorization": f"Bearer {issued.token}"}
        ).json()["data"]
        self.assertTrue(calls["synthetic"])
        self.assertFalse(calls["transport"]["gateway_attached"])
        self.assertEqual(calls["transport"]["mode"], "demo")


# ---------------------------------------------------------------------------
# V1-06/07 共享 → 调用 → 撤权（B 用真实 scoped token 的主体）
# ---------------------------------------------------------------------------


class V10607ShareInvokeRevokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Vertical(self, trusted_gateway=True)
        # 集成候选已含 Registry 的调用门（#122 P0）：新建 Endpoint 停在
        # ``pending_router_sync``，invoke 会被 409 拒绝。``acknowledge_route``
        # 写下的正是**一次成功 Gateway 回读本来就会写下的同一行**（revision 相等
        # + evidence=gateway_readback + 可导出快照），没有绕过回读逻辑。
        # 依赖该门的负例（pending / attested）用自己的夹具，不在这里被提前放行。
        self.v.h.acknowledge_route(self.v.ids["endpoint_id"])

    def _share_to_b(self):
        response = self.v.share(self.v.browser(A), mode="selected", principals=["4003"])
        self.assertEqual(response.status, 200, response.json())

    def test_b_owns_the_identity_and_gets_a_forwarded_synthetic_call(self) -> None:
        self._share_to_b()
        self.v.publish_engine()
        self.v.publish_snapshot()

        issued_b = self.v.issue(B)
        requester = self.v.requester_from_token(issued_b)
        # 身份来自 B 的真实 token，不是 A 的、也不是 admin 的。
        self.assertEqual(requester.principal, self.v.h.principal_of(B))
        self.assertEqual(requester.uid, 4003)
        self.assertNotIn("registry:admin:manage", requester.scopes)

        result = self.v.invoke_as(requester)
        data = result["data"]
        self.assertTrue(data["result"]["forwarded"])
        self.assertTrue(data["result"]["synthetic"])
        self.assertEqual(data["gateway"]["adapter"], "trusted_http")
        self.assertEqual(data["gateway"]["authorization"], "server_side_enforced")
        # 真实的 request id 由 Gateway 路径产生。
        self.assertTrue(data["result"]["request_id"].startswith("req_"))
        self.assertTrue(data["metering"]["recorded"])

        # 替身看到的是 **B 的 key** —— 身份没有串。
        self.assertEqual(len(self.v.gateway_double.calls), 1)
        call = self.v.gateway_double.calls[0]
        self.assertEqual(call["authorization"], "Bearer synthetic-bob-key")
        self.assertEqual(call["request_id"], data["result"]["request_id"])

    def test_a_revoked_grant_denies_b_on_the_very_next_call(self) -> None:
        self._share_to_b()
        self.v.publish_engine()
        self.v.publish_snapshot()
        requester = self.v.requester_from_token(self.v.issue(B))
        self.v.invoke_as(requester)
        calls_before = len(self.v.gateway_double.calls)

        response = self.v.share(self.v.browser(A), mode="private")
        self.assertEqual(response.status, 200, response.json())

        with self.assertRaises(registry_errors.NotFound):
            self.v.invoke_as(requester)
        # 被拒的调用**没有**再向 Gateway 发第二次请求。
        self.assertEqual(len(self.v.gateway_double.calls), calls_before)
        self.assertEqual(
            self.v.h.service.store.grantees(self.v.ids["endpoint_id"]), ()
        )

    def test_a_foreign_principal_never_sees_or_calls_it(self) -> None:
        self._share_to_b()
        self.v.publish_engine()
        self.v.publish_snapshot()
        issued_c = self.v.issue(C)
        client = self.v.token_client()
        ops = S.ops_of(
            client.run(
                "GET", "/api/v2/ops", headers={"Authorization": f"Bearer {issued_c.token}"}
            )
        )
        self.assertEqual(ops["registry"]["owned"], [])
        self.assertEqual(ops["registry"]["shared"], [])
        with self.assertRaises(registry_errors.NotFound):
            self.v.invoke_as(self.v.requester_from_token(issued_c))
        self.assertEqual(self.v.gateway_double.calls, [])

    def test_a_repoint_revokes_the_old_grant_and_requires_explicit_regrant(self) -> None:
        self._share_to_b()
        self.v.publish_engine()
        self.v.publish_snapshot()
        requester_b = self.v.requester_from_token(self.v.issue(B))
        self.v.invoke_as(requester_b)

        self.v.repoint(A, self.v.ready_sibling_deployment())
        self.assertEqual(
            self.v.h.service.store.grantees(self.v.ids["endpoint_id"]), ()
        )
        with self.assertRaises(registry_errors.NotFound):
            self.v.invoke_as(requester_b)


# ---------------------------------------------------------------------------
# V1-08/09 Calls 与 Atlas：只有自己的事件；未测得的就是 null
# ---------------------------------------------------------------------------


def _corpus(*, now) -> str:
    """三把 key、三种可观察差异；B 的那把含一条**未测量**的行。

    归属只来自受信目录（``key_id``），行里不带 uid、不带任何自报主体。
    时间戳相对 ``now`` 生成 —— Calls 的窗口是 24h，写死的历史日期会落在窗外，
    于是「B 看不到任何行」与「B 看到自己的行」就分不出来了。
    """

    def at(minutes_ago: int) -> str:
        moment = (now - timedelta(minutes=minutes_ago)).astimezone(timezone.utc)
        return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = [
        (at(60), "key-alice", {"input_tokens": 10, "output_tokens": 20}, 40),
        (at(55), "key-alice", {"input_tokens": 11, "output_tokens": 21}, 41),
        (at(50), "key-bob", {"input_tokens": 100, "output_tokens": 200}, 120),
        (at(45), "key-bob", {"input_tokens": 101, "output_tokens": 201}, 121),
        # B 的未测量行：usage / ttft / total 都没有
        (at(40), "key-bob", None, None),
        (at(35), "key-dave", {"input_tokens": 7, "output_tokens": 7}, 7),
    ]
    lines = []
    for index, (moment, key, usage, ttft) in enumerate(rows):
        lines.append(
            json.dumps(
                {
                    "ts": moment,
                    "key_id": key,
                    "request_id": f"req-v1-68-{index:03d}",
                    "label": "v1-68",
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "model": "model-v1-68",
                    "status": 200,
                    "outcome": "success",
                    "stream": False,
                    "usage": usage,
                    "queue_ms": 1 if usage else None,
                    "ttft_ms": ttft,
                    "upstream_ms": 500 if usage else None,
                    "total_ms": 600 if usage else None,
                    "bytes_out": 128,
                },
                sort_keys=True,
            )
        )
    return "\n".join(lines) + "\n"


class V10809CallsAtlasIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.v = Vertical(self)
        old_stack = self.v.h.app.atlas.stack
        self.v.h.app.atlas = bff_atlas_adapter.ModuleAtlasAdapter(
            bff_stacks.build_atlas_stack(
                directory=old_stack.directory,
                gateway_text=lambda _identity: _corpus(now=self.v.h.now),
                project_scope=old_stack.project_scope,
            ),
            clock=lambda: self.v.h.now,
        )

    def _calls(self, name: str) -> dict:
        issued = self.v.issue(name)
        response = self.v.token_client().run(
            "GET", "/api/atlas/calls", headers={"Authorization": f"Bearer {issued.token}"}
        )
        self.assertEqual(response.status, 200, response.json())
        return response.json()["data"]

    def test_b_sees_only_bs_own_calls(self) -> None:
        calls = self._calls(B)
        self.assertTrue(calls["calls"])
        self.assertEqual({row["key_id"] for row in calls["calls"]}, {"key-bob"})
        self.assertEqual({row["project"] for row in calls["calls"]}, {"proj-lab"})

    def test_a_sees_only_as_own_calls(self) -> None:
        calls = self._calls(A)
        self.assertTrue(calls["calls"])
        self.assertEqual({row["key_id"] for row in calls["calls"]}, {"key-alice"})

    def test_the_two_principals_never_share_a_row(self) -> None:
        ids_a = {row["request_id"] for row in self._calls(A)["calls"]}
        ids_b = {row["request_id"] for row in self._calls(B)["calls"]}
        self.assertTrue(ids_a)
        self.assertTrue(ids_b)
        self.assertEqual(ids_a & ids_b, set())

    def test_unmeasured_tokens_and_ttft_stay_null(self) -> None:
        calls = self._calls(B)
        unmeasured = [row for row in calls["calls"] if row["ttft_ms"] is None]
        self.assertEqual(len(unmeasured), 1)
        row = unmeasured[0]
        self.assertIsNone(row["tokens"]["input"])
        self.assertIsNone(row["tokens"]["output"])
        self.assertIsNone(row["tokens"]["total"])
        self.assertIsNone(row["queue_ms"])
        self.assertIsNone(row["total_ms"])
        # 0 与 null 是两件事：这里必须是 null，不是 0。
        self.assertNotEqual(row["tokens"]["total"], 0)
        self.assertGreaterEqual(calls["coverage"]["tokens_unknown_rows"], 1)
        self.assertGreaterEqual(calls["coverage"]["ttft_unknown_rows"], 1)

    def test_the_atlas_source_declares_itself_synthetic_demo(self) -> None:
        calls = self._calls(B)
        self.assertTrue(calls["synthetic"])
        self.assertFalse(calls["transport"]["gateway_attached"])
        self.assertEqual(calls["transport"]["mode"], "demo")
        self.assertEqual(calls["source"], "synthetic-fixture")


# ---------------------------------------------------------------------------
# 未配置的真实依赖：503 / blocked，而不是一份看起来正常的空数据
# ---------------------------------------------------------------------------


class FailClosedUnconfiguredTest(unittest.TestCase):
    def test_a_detached_gateway_makes_invocation_503_not_a_fabricated_result(self) -> None:
        v = Vertical(self, mock_gateway=False)
        response = v.share(v.browser(A), mode="selected", principals=["4003"])
        self.assertEqual(response.status, 200, response.json())
        with self.assertRaises(registry_errors.UpstreamUnavailable):
            v.invoke_as(v.requester_from_token(v.issue(B)))

    def test_the_usage_route_is_503_when_no_observability_source_is_attached(self) -> None:
        v = Vertical(self, with_observability=False)
        issued = v.issue(B)
        response = v.token_client().run(
            "GET",
            "/api/v2/ops/usage",
            headers={"Authorization": f"Bearer {issued.token}"},
        )
        self.assertEqual(response.status, 503, response.json())

    def test_a_deployment_without_linux_auth_answers_503_on_login(self) -> None:
        from backend.bff.app import OpsApplication

        v = Vertical(self)
        app = OpsApplication(
            auth_app=v.h.auth_app,
            service=v.h.service,
            accounts=v.h.accounts,
            linux_service=None,  # 本部署没有装 PAM/broker
            observability=v.h.observability,
            evaluation=v.h.evaluation,
            atlas=v.h.atlas,
            host_summary=v.h.host_summary,
            clock=lambda: v.h.now,
        )
        client = S.WsgiClient(app=app, host="127.0.0.1:8790", scheme="http")
        token = client.run("GET", "/auth/csrf").json()["data"]["csrf_token"]
        response = client.run(
            "POST",
            "/auth/linux/login",
            payload={"login_name": A, "password": "whatever"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status, 503, response.json())
        self.assertEqual(response.json()["error"]["code"], "linux_auth_not_configured")

    def test_a_missing_atlas_source_is_503_not_an_empty_list(self) -> None:
        v = Vertical(self)
        v.h.app.atlas = None
        issued = v.issue(B)
        response = v.token_client().run(
            "GET",
            "/api/atlas/calls",
            headers={"Authorization": f"Bearer {issued.token}"},
        )
        self.assertEqual(response.status, 503, response.json())
        self.assertIn("atlas", response.json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
