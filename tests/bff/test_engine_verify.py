"""「现在探测一次」：平台自己探，同步返回今天的结果。

这条路由与 ``/serve`` 是两件事，别混在一起验：

* ``/serve`` 下发一条任务给**本人代理**，202 + queued，代理没领取就什么都没发生；
* ``/verify`` 由平台从本机立刻探一次，**同步**返回，并把结论写回登记。

所以这里断言的是「探测真的跑了、结果照实报、状态真的被写回」，以及归属那道门
（只有所有者能触发，别人 403、匿名 401）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from tests.bff import support as S

VERIFY = "/api/v2/registry/engines/{}/verify"


class EngineVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llm-lab-bff-verify-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.harnesses = []

    def tearDown(self):
        for harness in self.harnesses:
            harness.close()

    def build(self, *, prober_healthy: bool = True):
        h = S.build_harness(state_dir=self.tmp, prober_healthy=prober_healthy)
        self.harnesses.append(h)
        return h

    def verify(self, client, engine_id):
        return client.run(
            "POST",
            VERIFY.format(engine_id),
            payload={},
            headers={"X-CSRF-Token": S.csrf(client)},
        )

    def test_an_anonymous_caller_gets_401(self):
        h = self.build()
        engine_id = S.register_baseline(h, "alice")["engine_id"]
        # 先把 CSRF 备齐：验的是身份那道门，不是 CSRF。
        self.assertEqual(self.verify(h.client("alice"), engine_id).status, 200)
        anonymous = S.WsgiClient(app=h.app, host="127.0.0.1:8790", scheme="http")
        self.assertEqual(self.verify(anonymous, engine_id).status, 401)

    def test_the_owner_gets_the_fresh_conclusion_synchronously(self):
        h = self.build()
        engine_id = S.register_baseline(h, "alice")["engine_id"]
        result = self.verify(h.client("alice"), engine_id)
        self.assertEqual(result.status, 200, result.body)
        data = result.json()["data"]
        # 同步：响应里直接带这次探测的结论，不是一个待查的任务 id。
        self.assertNotIn("requested_by", data, "这是引擎视图，不是一条代理任务")
        self.assertEqual(data["verification"]["status"], "VERIFIED", data)
        self.assertEqual(data["status"], "verified")
        self.assertEqual(data["verification"]["transport"]["state"], "healthy")
        # 结论是**刚才**取的，不是登记时那份（ops 里的 health_observed_at 就取这个值）。
        self.assertEqual(data["health"]["observed_at"], data["last_seen_at"])
        self.assertEqual(data["notes"], [])

    def test_a_probe_that_cannot_reach_the_service_is_written_back_as_unavailable(self):
        h = self.build(prober_healthy=False)
        engine_id = S.register_baseline(h, "alice")["engine_id"]
        result = self.verify(h.client("alice"), engine_id)
        self.assertEqual(result.status, 200, result.body)
        data = result.json()["data"]
        # 探不通也是今天的结论：状态如实变成 unavailable，而不是保持过去那个 verified。
        self.assertEqual(data["status"], "unavailable", data)
        self.assertFalse(data["verification"]["verified"])
        self.assertNotEqual(data["health"]["state"], "healthy")

    def test_another_principal_cannot_probe_someone_elses_service(self):
        h = self.build()
        engine_id = S.register_baseline(h, "alice")["engine_id"]
        result = self.verify(h.client("dave"), engine_id)
        # 别人的服务对 dave 根本不可见：不可见与不存在给同一个答案。
        self.assertEqual(result.status, 404, result.body)
        self.assertEqual(result.error_code(), "registry.not_found")


if __name__ == "__main__":
    unittest.main()
