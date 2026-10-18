"""数据来源是服务端的属性，不是请求可以挑的东西。

前端页面的 URL 上确实有 ``?source=live|mock``——那是**页面**的取数模式，由
``web/js/features/source.js`` 解析，决定页面去调哪个 base。它不该出现在 API 调用
里。一旦 API 接受它，「来源」就变成了调用方声明的字段，而一份可以把合成数据标成
live 的接口，和一份会说谎的接口没有区别。

所以这里断言两件事，方向相反：

* 客户端声明的来源被 **400 拒绝**（而不是「读到了但不用」——静默忽略意味着下游
  某一层可以再把它捡起来）；
* 服务端**自己**写进 payload 的来源是诚实的：mock 转发层下 ``transport.mode``
  是 ``demo``，不是 ``live``。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import identity as bff_identity
from backend.bff import ops as bff_ops
from backend.bff import route_map as RM
from tests.bff import support as S

#: 客户端可能用来声明来源的查询参数写法。
SOURCE_QUERIES = ("source=live", "source=mock", "source=demo", "source=1", "SOURCE=live")

#: 客户端可能用来声明来源的头。
SOURCE_HEADERS = ("X-Source", "X-Data-Source", "X-Synthetic", "X-Live-Data")


class SourceAssertionRejectionTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.client = self.h.client("alice")

    def test_a_declared_source_in_the_query_is_rejected_on_every_data_route(self):
        for spec in RM.ROUTES:
            if not spec.is_data_route or spec.handler is None:
                continue
            for query in SOURCE_QUERIES:
                with self.subTest(route=spec.name, query=query):
                    client = self.h.client("alice")
                    path = spec.path if spec.pattern is None else (
                        f"{spec.pattern[0]}dep_example{spec.pattern[1]}"
                    )
                    separator = "&" if "?" in path else "?"
                    headers = {}
                    if spec.is_write:
                        headers["X-CSRF-Token"] = S.csrf(client)
                    response = client.run(
                        spec.methods[0],
                        f"{path}{separator}{query}",
                        payload={} if spec.is_write else None,
                        headers=headers,
                    )
                    self.assertEqual(
                        response.status,
                        400,
                        f"{spec.name} accepted ?{query}: {response.json()!r}",
                    )
                    self.assertEqual(
                        response.error_code(), bff_identity.REASON_SOURCE_ASSERTED
                    )

    def test_a_declared_source_in_a_header_is_rejected(self):
        for header in SOURCE_HEADERS:
            with self.subTest(header=header):
                response = self.client.run(
                    "GET", "/api/v2/ops", headers={header: "live"}
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(
                    response.error_code(), bff_identity.REASON_SOURCE_ASSERTED
                )

    def test_the_rejection_names_the_offending_parameter(self):
        response = self.client.run("GET", "/api/v2/ops?source=live")
        message = response.json()["error"]["message"]
        self.assertIn("source", message)

    def test_an_empty_source_parameter_is_still_an_assertion(self):
        """``?source=`` 也是「我在声明来源」，只是声明成空的。"""
        response = self.client.run("GET", "/api/v2/ops?source=")
        self.assertEqual(response.status, 400)

    def test_rejecting_a_source_does_not_require_a_session_first(self):
        """先拒断言，再谈身份：两种都是 4xx，但拒绝的理由要准确。"""
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run("GET", "/api/v2/ops?source=live")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), bff_identity.REASON_SOURCE_ASSERTED)

    def test_the_other_query_parameters_still_work(self):
        """守卫只针对来源，不是「查询参数一律拒绝」。"""
        self.assertEqual(
            self.client.run("GET", "/api/v2/ops?visibility=shared").status, 200
        )
        self.assertEqual(
            self.client.run("GET", "/api/v2/ops/usage?scope=self").status, 200
        )

    def test_a_source_declaration_on_a_non_data_route_is_not_this_guard(self):
        """登录路径不代理数据，所以它不受这条守卫约束——但也不因此改变行为。"""
        response = self.client.run("GET", "/auth/linux/status?source=live")
        self.assertEqual(response.status, 200)


class ServerDerivedSourceTests(unittest.TestCase):
    """服务端写进 payload 的来源必须诚实。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_a_mock_forwarder_reports_demo_and_never_live(self):
        ops = S.ops_of(S.fetch_ops(self.h.client("alice")))
        self.assertEqual(ops["transport"]["mode"], "demo")
        self.assertTrue(ops["transport"]["synthetic"])
        self.assertTrue(ops["synthetic"])

    def test_the_source_is_taken_from_the_ops_config_not_the_request(self):
        """同一个会话，改了**装配**就是另一个来源；改请求什么也改不了。"""
        from backend.bff.app import OpsApplication
        from backend.bff.ops import OpsConfig

        live_app = OpsApplication(
            auth_app=self.h.auth_app,
            service=self.h.service,
            accounts=self.h.accounts,
            linux_service=self.h.linux_service,
            observability=self.h.observability,
            evaluation=self.h.evaluation,
            ops_config=OpsConfig(
                registry_mode="live", gateway_adapter="gateway", gateway_attached=True
            ),
            admin_mode=self.h.admin_mode,
        )
        # 同一个后端会话（同一份会话库），换一个装配。
        client = S.WsgiClient(app=live_app, host="127.0.0.1:8790", scheme="http")
        client.cookies[self.h.app.cfg.cookie_name] = self.h.session_id("alice")

        ops = S.ops_of(client.run("GET", "/api/v2/ops"))
        self.assertEqual(ops["transport"]["mode"], "live")

        # 而一个试图把来源「降级」或「升级」的请求仍然被拒：唯一改变来源的是
        # 装配，不是请求。
        self.assertEqual(client.run("GET", "/api/v2/ops?source=mock").status, 400)
        self.assertEqual(client.run("GET", "/api/v2/ops?source=live").status, 400)

    def test_metrics_carry_no_source_when_there_is_no_verified_one(self):
        ops = S.ops_of(S.fetch_ops(self.h.client("alice")))
        self.assertIsNone(ops["metrics"]["my_source"])
        self.assertIsNone(ops["metrics"]["global_source"])
        for item in ops["metrics"]["items"]:
            self.assertIsNone(item["source"], item["name"])
            self.assertEqual(item["state"], "unknown", item["name"])

    def test_the_transport_mode_rule_is_the_config_rule(self):
        """把「什么时候可以声称 live」这条规则本身钉住。"""
        self.assertEqual(bff_ops.OpsConfig().transport_mode, "demo")
        self.assertEqual(
            bff_ops.OpsConfig(
                registry_mode="live", gateway_adapter="mock", gateway_attached=True
            ).transport_mode,
            "demo",
            "a test double must never be labelled live",
        )
        self.assertEqual(
            bff_ops.OpsConfig(
                registry_mode="live", gateway_adapter="gateway", gateway_attached=True
            ).transport_mode,
            "live",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
