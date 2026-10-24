"""§4.1：``offline`` ⇒ 调用返回 503 ``service_offline``，路由保留。

两件事必须同时成立，缺一条这个功能就是假的：

1. 发现循环看到服务不在了，**调用不再被转发**（不是转发了再失败）；
2. **路由没有被删掉** —— 服务回来时还是同一个对象、同一条路由。

所以这里的每一条拒绝都断言 ``gateway.calls`` 没有增加。
"""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from tests.registry.support import Harness

from backend.registry.api import PREFIX
from backend.registry.models import ENGINE_RUNTIME_ATTACHED, ENGINE_RUNTIME_OFFLINE

OWNER = "alice"


class ServiceOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.endpoint = self.h.make_ready_endpoint(OWNER, name="svc")
        self.h.confirm_route(self.endpoint)
        row = self.h.store.endpoint_by_id(self.endpoint)
        self.deployment = self.h.store.deployment_by_id(row.deployment_id)
        self.engine_id = self.deployment.engine_id

    def set_state(self, state: str | None) -> None:
        self.h.store.update_engine_runtime(
            self.engine_id, state=state, last_seen_at=datetime.now(timezone.utc)
        )

    def invoke(self, principal: str = OWNER):
        return self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/invoke",
            principal=principal,
            body={"messages": []},
        )

    def test_an_offline_service_is_503_service_offline_and_never_reaches_the_gateway(self):
        self.set_state(ENGINE_RUNTIME_OFFLINE)
        calls_before = len(self.h.gateway.calls)

        status, _headers, payload = self.invoke()

        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], "registry.service_offline")
        self.assertIn("service_offline", payload["error"]["message"])
        self.assertEqual(
            len(self.h.gateway.calls), calls_before, "拒绝必须发生在转发之前"
        )

    def test_the_route_survives_and_the_same_endpoint_works_when_the_service_returns(self):
        self.set_state(ENGINE_RUNTIME_OFFLINE)
        status, _headers, _payload = self.invoke()
        self.assertEqual(status, 503)

        self.set_state(ENGINE_RUNTIME_ATTACHED)
        status, _headers, payload = self.invoke()

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["endpoint_id"], self.endpoint)
        self.assertEqual(self.h.store.endpoint_by_id(self.endpoint).id, self.endpoint)

    def test_never_observed_is_not_offline(self):
        """没有观测源（``state is None``）时不能凭空拒绝：不知道不等于不在。"""

        self.set_state(None)
        status, _headers, payload = self.invoke()

        self.assertEqual(status, 200, payload)

    def test_the_offline_gate_also_holds_for_other_granted_users(self):
        """offline 是**服务**的事实，不是调用者的事实：换人来调一样 503。"""

        status, _headers, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint}/share",
            principal=OWNER,
            body={"mode": "users", "grantees": ["bob"]},
        )
        self.assertEqual(status, 200, payload)
        self.set_state(ENGINE_RUNTIME_OFFLINE)

        status, _headers, payload = self.invoke(principal="bob")

        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["code"], "registry.service_offline")


if __name__ == "__main__":
    unittest.main()
