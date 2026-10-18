"""P0：`invoke` 必须先证明**路由已经真的生效**，再碰 Gateway。

## 缺陷（修复前实测）

`RegistryService.invoke` 只检查 ``status == ready``，然后直接
``self.gateway.forward(...)``。于是下面这些端点**都会被转发**：

* 刚创建、从未被路由过的（``router_generation=0 < revision=1``）；
* 刚 repoint、Router 还没回读的（``pending_router_sync``）；
* 运维用 ``{"generation": N}`` 声明过、但**没有任何 Gateway 回读**的
  （``router_sync_attested``）；
* 被 ``build_routing_snapshot`` 排除、Gateway 从未收到过的；
* 本部署根本没配 RouterAdapter 的；
* DB 说已生效、但 Router **此刻**报的是另一代的。

``docs/architecture.md`` §5.2 规则 3 要求「路由应用必须有 Gateway 发布的
generation/revision 与 snapshot SHA 回执」，V1-03 同理。人工声明不能冒充 applied。

## 本文件的口径

每一条拒绝都断言 **``gateway.calls`` 没有增加** —— 拒绝必须发生在任何 Gateway
调用**之前**，而不是调用完再判断。只有真正 ``router_sync_applied`` 且 Router
此刻回读一致的端点才允许转发一次有界 synthetic 调用。
"""

from __future__ import annotations

import dataclasses
import unittest

from tests.registry.support import Harness

from backend.registry.api import PREFIX
from backend.registry.errors import (
    AccessDenied,
    NotFound,
    NotVerified,
    Unauthenticated,
)
from backend.registry.router import (
    DetachedRouterAdapter,
    RouterPublishResult,
    ROUTER_SYNC_APPLIED,
    ROUTER_SYNC_PENDING,
    snapshot_sha256,
)
from tests.registry.test_trusted_gateway_and_router import ConfirmingRouterAdapter

OWNER = "alice"
GRANTEE = "bob"
FOREIGN = "dave"
ADMIN = "carol"


class _StaleReadbackRouter:
    """一个回读**落后**的 Router：publish 说成功，但 applied_revision 是上一代。"""

    name = "stale_readback_double"
    attached = True

    def __init__(self) -> None:
        self._published: int | None = None

    def publish(self, snapshot: dict) -> RouterPublishResult:
        self._published = int(snapshot["revision"])
        return RouterPublishResult(
            published=True,
            applied_revision=self._published,
            snapshot_sha256=snapshot_sha256(snapshot),
            state=ROUTER_SYNC_APPLIED,
            detail="test double confirms at publish time",
        )

    def applied_revision(self) -> int | None:
        # 网关后来回退/未跟上：此刻报的是上一代。
        return max(0, (self._published or 1) - 1)


class _ReadbackRouter(ConfirmingRouterAdapter):
    """在 `ConfirmingRouterAdapter` 之上补一个**真实的** applied_revision。"""

    def __init__(self) -> None:
        self._applied: int | None = None

    def publish(self, snapshot: dict) -> RouterPublishResult:
        result = super().publish(snapshot)
        self._applied = result.applied_revision
        return result

    def applied_revision(self) -> int | None:
        return self._applied


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.endpoint = self.h.make_ready_endpoint(OWNER, name="svc")
        self.sibling = self.h.make_sibling_deployment(OWNER, self.endpoint, "next")
        self.revision = self.h.endpoint_revision(OWNER, self.endpoint)

    # -- helpers ----------------------------------------------------------

    def calls(self) -> int:
        return len(self.h.gateway.calls)

    def invoke(self, principal: str = OWNER, payload: dict | None = None):
        return self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/invoke",
            principal=principal,
            body=payload if payload is not None else {"messages": []},
        )

    def repoint(self, principal: str = OWNER):
        return self.h.repoint(principal, self.endpoint, self.sibling)


class PendingNeverReachesTheGatewayTest(_Base):
    def test_a_fresh_endpoint_is_pending_and_is_rejected_before_forward(self) -> None:
        with self.assertRaises(NotVerified) as raised:
            self.h.service.invoke(
                self.h.requester(OWNER), self.endpoint, {"messages": []}
            )
        self.assertEqual(raised.exception.contract_code, "registry.not_verified")
        self.assertEqual(self.calls(), 0)

    def test_the_http_route_answers_409_and_does_not_forward(self) -> None:
        status, _headers, payload = self.invoke()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_verified")
        self.assertEqual(self.calls(), 0)

    def test_a_repoint_without_a_router_confirmation_is_rejected(self) -> None:
        # 缺省 DetachedRouterAdapter：repoint 之后仍然 pending。
        status, _headers, payload = self.repoint()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["router_sync"], ROUTER_SYNC_PENDING)
        status, _headers, payload = self.invoke()
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.calls(), 0)

    def test_an_operator_attestation_is_not_enough(self) -> None:
        self.repoint()
        revision = self.h.endpoint_revision(OWNER, self.endpoint)
        status, _headers, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/routing-applied",
            principal=ADMIN,
            body={"generation": revision},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            payload["data"]["router_sync"]["state"], "router_sync_attested"
        )
        # 人工声明过 ≠ 已切流：仍然不许转发。
        status, _headers, payload = self.invoke()
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.calls(), 0)


class UnpublishedOrUnconfirmedNeverReachesTheGatewayTest(_Base):
    def test_an_endpoint_excluded_from_the_snapshot_is_rejected(self) -> None:
        # 装一个会确认的 Router，但**不让** engine 可发布：快照为空，
        # 这个端点从未被 Gateway 收到过。
        self.h.service.router = _ReadbackRouter()
        self.assertEqual(self.h.service._routing_snapshot()["endpoints"], [])
        self.repoint()
        status, _headers, payload = self.invoke()
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.calls(), 0)

    def test_a_router_that_reports_a_different_generation_is_rejected(self) -> None:
        # DB 侧会记成 applied，但 Router **此刻**报的是上一代。
        self.h.publish_engine()
        self.h.service.router = _StaleReadbackRouter()
        self.repoint()
        status, _headers, payload = self.invoke()
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.calls(), 0)

    def test_a_detached_router_blocks_even_a_row_that_was_acked(self) -> None:
        # 模拟「上一次部署里被回执过」的行，然后本部署没有 Router。
        self.h.publish_engine()
        self.repoint()  # revision 2
        revision = self.h.endpoint_revision(OWNER, self.endpoint)
        self.h.service.store.ack_router_generation(
            self.endpoint,
            generation=revision,
            actor_principal="system:router-sync",
            actor_uid=0,
            at=self.h.service._now(),
            evidence="gateway_readback",
        )
        self.h.service.router = DetachedRouterAdapter()
        status, _headers, payload = self.invoke()
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.calls(), 0)


class AConfirmedRouteIsForwardedExactlyOnceTest(_Base):
    def test_the_full_confirmed_path_forwards_one_bounded_call(self) -> None:
        self.h.publish_engine()
        self.h.service.router = _ReadbackRouter()
        status, _headers, payload = self.repoint()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["router_sync"], ROUTER_SYNC_APPLIED)
        self.assertEqual(
            payload["meta"]["router_sync_report"]["acked_endpoints"], [self.endpoint]
        )

        status, _headers, payload = self.invoke(OWNER, {"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["data"]["result"]["forwarded"])
        self.assertTrue(payload["data"]["result"]["synthetic"])
        self.assertEqual(self.calls(), 1)

    def test_a_confirmed_but_unshared_foreign_uid_gets_404_and_no_call(self) -> None:
        self.h.publish_engine()
        self.h.service.router = _ReadbackRouter()
        self.repoint()
        status, _headers, payload = self.invoke(FOREIGN)
        self.assertEqual(status, 404, payload)
        self.assertEqual(self.calls(), 0)

    def test_revocation_denies_the_next_call_without_a_second_gateway_request(self) -> None:
        self.h.publish_engine()
        self.h.service.router = _ReadbackRouter()
        self.repoint()
        status, _headers, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/share",
            principal=OWNER,
            body={"mode": "users", "grantees": [GRANTEE]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.invoke(GRANTEE)[0], 200)
        calls_before = self.calls()

        status, _headers, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/unshare",
            principal=OWNER,
            body={},
        )
        self.assertEqual(status, 200, payload)
        status, _headers, payload = self.invoke(GRANTEE)
        self.assertEqual(status, 404, payload)
        self.assertEqual(self.calls(), calls_before)

    def test_an_unauthenticated_request_never_reaches_the_gateway(self) -> None:
        status, _headers, payload = self.h.request(
            "POST", f"{PREFIX}/endpoints/{self.endpoint}/invoke", token="forged-token",
            body={"messages": []},
        )
        self.assertEqual(status, 401, payload)
        self.assertEqual(self.calls(), 0)


class TheGateDoesNotChangeTheAuthBoundaryTest(_Base):
    def test_a_grantee_without_invoke_scope_is_refused_before_forward(self) -> None:
        # 一个看得见但**没有** invoke scope 的主体：403，且没有转发。
        self.h.publish_engine()
        self.h.service.router = _ReadbackRouter()
        self.repoint()
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/share",
            principal=OWNER,
            body={"mode": "users", "grantees": [GRANTEE]},
        )
        self.h.with_scopes(GRANTEE, frozenset({"registry:read"}))
        status, _headers, payload = self.invoke(GRANTEE)
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.calls(), 0)


if __name__ == "__main__":
    unittest.main()
