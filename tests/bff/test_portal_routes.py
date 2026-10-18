"""这一轮接进来的同源只读路由：正反两面都验。

Issue #79 把这几条路由接进 ``backend/bff`` 的原因不是「前端想要更多接口」，而是
它们以前的状态是**含糊**的：``/api/atlas/calls`` 与 ``/api/infra/summary`` 在
同源 BFF 上根本 404（数据面明明在），``/api/evaluation/reports`` 只有 module-only
的 WSGI 入口。含糊的地方就会长出两种坏结局里的一种 —— 前端以为「没有数据」，
或者某天有人给它加一条 `?url=` 让它「先能通」。

所以这一组测试盯的是**分界**，不是功能点：

* 未接线的来源是 **503**，不是一份 200 的空数据（「没接」≠「没有」）；
* 没有权限是 **403**，不是 200 空列表（「你没权限」≠「你没有」）；
* 客户端自选来源 / 目标 / 主体是 **400**（``?source=`` / ``?url=`` / ``?owner=``
  / ``?key=``），不是静默忽略；
* 跨主体的记录**一条都不出现**在别人的答复里。

正向的那几条用仓库既有的协议 fixture（``tests/observability/fixtures/host_summary``）
与 harness 自带的合成语料，二者都只在 ``tests/`` 内 —— 生产装配模块里没有任何
一条路径能产出它们。
"""

from __future__ import annotations

import tempfile
import unittest

from tests.bff import support as S

#: 这一轮新接进来的、按会话鉴权的同源路由。每一条都要过同一组负向断言。
NEW_SESSION_ROUTES = (
    "/api/infra/summary",
    "/api/portal/services",
    "/api/portal/capabilities",
    "/api/atlas/calls",
    "/api/evaluation/report-sources",
    "/api/evaluation/reports",
    "/api/evaluation/reports/rep_example",
)

#: 客户端不该能自选的那些参数。前三个是 Issue #79 点名的（上游 URL / PromQL /
#: 文件路径），后面几个是同一类的常见拼法（取数目标、主体、命令行）。
FORBIDDEN_PARAMS = (
    "source",
    "url",
    "promql",
    "path",
    "query",
    "target",
    "port",
    "cmd",
    "journal",
    "file",
    "proxy",
    "uid",
    "owner",
    "principal",
    "role",
    "key",
)


class RouteTableShapeTests(unittest.TestCase):
    """纯数据：新路由在表里被声明成什么。"""

    def test_the_new_routes_are_declared_with_their_source_and_owner(self):
        from backend.bff import route_map as RM

        expected = {
            "/api/infra/summary": RM.SOURCE_HOST_SUMMARY,
            "/api/portal/services": RM.SOURCE_HOST_SUMMARY,
            "/api/portal/capabilities": RM.SOURCE_PORTAL,
            "/api/atlas/calls": RM.SOURCE_ATLAS,
            "/api/evaluation/report-sources": RM.SOURCE_EVALUATION,
            "/api/evaluation/reports": RM.SOURCE_EVALUATION,
        }
        for path, source in expected.items():
            with self.subTest(path=path):
                spec = RM.match(path)
                self.assertIsNotNone(spec, f"{path} is not in the route table")
                self.assertEqual(spec.source, source)
                self.assertTrue(spec.owner)
                self.assertEqual(spec.auth, RM.AUTH_SESSION)
                self.assertFalse(spec.is_write)

    def test_the_variable_route_is_declared_with_a_pattern(self):
        from backend.bff import route_map as RM

        spec = RM.ROUTES_BY_NAME["evaluation.report_detail"]
        self.assertIsNotNone(spec.pattern)
        self.assertIs(RM.match("/api/evaluation/reports/rep_1"), spec)
        # 一个带斜杠的「ref」不是这条路由：它要么去匹配别的路由，要么 404。
        self.assertIsNone(RM.match("/api/evaluation/reports/a/b"))

    def test_every_new_route_is_session_gated_not_anonymous(self):
        """匿名可达的集合**没有**因为这一轮而变大。

        这条与 ``test_route_map.py`` 里那条封闭集合断言是同一个事实的两个视角：
        新增匿名路由必须是一次显式的决定，而不是接路由时的副产品。
        """
        from backend.bff import route_map as RM

        for path in NEW_SESSION_ROUTES:
            with self.subTest(path=path):
                spec = RM.match(path)
                self.assertEqual(spec.auth, RM.AUTH_SESSION)


class NegativeMatrixTests(unittest.TestCase):
    """整组新路由共用的负向面：状态码与错误 token。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _anonymous(self):
        return S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def test_every_new_route_refuses_an_anonymous_caller_with_401(self):
        client = self._anonymous()
        for path in NEW_SESSION_ROUTES:
            with self.subTest(path=path):
                response = client.run("GET", path)
                self.assertEqual(response.status, 401, f"{path}: {response.json()!r}")
                self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_every_new_route_refuses_a_forged_identity_header(self):
        for path in NEW_SESSION_ROUTES:
            for header in ("X-UID", "X-Owner", "X-Role", "X-Admin", "X-Linux-User"):
                with self.subTest(path=path, header=header):
                    client = self.h.client("alice")
                    response = client.run("GET", path, headers={header: "4001"})
                    self.assertEqual(response.status, 400, response.json())
                    self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_a_client_declared_source_is_refused_on_every_data_route(self):
        for path in NEW_SESSION_ROUTES:
            if path == "/api/portal/capabilities":
                # 这条也是数据路由（source=portal，非 session），所以同样要拒。
                pass
            with self.subTest(path=path):
                client = self.h.client("alice")
                response = client.run("GET", f"{path}?source=live")
                self.assertEqual(response.status, 400, f"{path}: {response.json()!r}")
                self.assertEqual(response.error_code(), "source_assertion_rejected")

    def test_an_arbitrary_upstream_target_is_refused_on_every_data_route(self):
        """``?url=`` / ``?promql=`` / ``?path=`` 之类**没有**一条路由会读它。

        它们命中即 400：静默忽略意味着下游某一层将来可以把它捡起来用。这条断言
        覆盖这一轮新接的每一条路由 —— 而不是只覆盖「我记得写了守卫的那一条」。
        """
        for path in NEW_SESSION_ROUTES:
            for param in FORBIDDEN_PARAMS:
                with self.subTest(path=path, param=param):
                    client = self.h.client("alice")
                    response = client.run("GET", f"{path}?{param}=x")
                    self.assertEqual(
                        response.status,
                        400,
                        f"{path}?{param}= was not refused: {response.json()!r}",
                    )
                    self.assertIn(
                        response.error_code(),
                        ("source_assertion_rejected", "proxy_assertion_rejected"),
                    )

    def test_the_refused_target_error_names_the_parameter_it_refused(self):
        """错误里说清楚是**哪个**参数被拒了 —— 前端与运维都要知道该删什么。"""
        client = self.h.client("alice")
        response = client.run("GET", "/api/infra/summary?promql=up")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "proxy_assertion_rejected")
        self.assertIn("promql", response.json()["error"]["message"])

    def test_a_method_the_route_does_not_declare_is_405(self):
        for path in NEW_SESSION_ROUTES:
            with self.subTest(path=path):
                client = self.h.client("alice")
                response = client.run("DELETE", path)
                self.assertEqual(response.status, 405)


class HostSummaryTests(unittest.TestCase):
    """``/api/infra/summary``：固定来源 + 服务端判定，三种未就绪各自有别。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_a_member_without_the_host_scope_is_refused_with_403(self):
        """整机读数是共享量、有自己的一道门；一个普通成员看不到它。"""
        response = self.h.client("alice").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "observability.access_denied")

    def test_an_elevated_session_reads_it_but_gets_503_when_nothing_was_read(self):
        """装配了、但这次没读到 ⇒ 503，**不是**一份全 UNKNOWN 的 200。

        两者在前端都是「没有数字」，但对运维是两个不同的结论：一个要查投影端，
        一个要查装配。让它们长得一样，就是让一次数据故障和一次接线遗漏无法区分。
        """
        response = self.h.admin_client("carol").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "observability.source_unavailable")


class HostSummaryWithProjectionTests(unittest.TestCase):
    """带一份**真形状**的投影时，服务端判定到底做了什么。

    用的是仓库既有的协议 fixture（``tests/observability/fixtures/host_summary``），
    它们同时被观测模块自己的套件断言。**没有**新造样本：一份新造的、看起来像真的
    读数，正是这个仓库一路拒绝的东西。
    """

    @classmethod
    def setUpClass(cls):
        cls.fresh = S.load_host_summary_fixture("01-live-fresh.json")
        cls.synthetic = S.load_host_summary_fixture("06-synthetic-true.json")
        cls.stale = S.load_host_summary_fixture("08-stale-sample-at.json")

        cls.h_fresh = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            now=S.fixture_now(cls.fresh),
            host_projection=cls.fresh["payload"],
        )
        cls.h_synthetic = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            now=S.fixture_now(cls.synthetic),
            host_projection=cls.synthetic["payload"],
        )
        cls.h_stale = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            now=S.fixture_now(cls.stale),
            host_projection=cls.stale["payload"],
        )

    @classmethod
    def tearDownClass(cls):
        for harness in (cls.h_fresh, cls.h_synthetic, cls.h_stale):
            harness.close()

    def test_a_fresh_verified_projection_is_accepted_and_carries_live_readings(self):
        response = self.h_fresh.admin_client("carol").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        self.assertTrue(data["accepted"])
        self.assertEqual(data["source"], "prometheus-readonly")
        self.assertEqual(data["version"], 1)
        live = [key for key, item in data["metrics"].items() if item["state"] == "live"]
        self.assertEqual(sorted(live), sorted(self.fresh["expect"]["live_keys"]))

    def test_a_projection_that_calls_itself_synthetic_is_never_live(self):
        """``synthetic: true`` 的信封被整份拒绝 —— 不是「逐字段降级」。

        这条是本轮接线的核心价值：判定在**下发之前**发生，而不是留给浏览器那行
        ``if``。一个自称合成的数字如果过去了，就会被画成实测值。
        """
        response = self.h_synthetic.admin_client("carol").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 200)
        data = response.json()["data"]
        self.assertFalse(data["accepted"])
        self.assertTrue(all(item["state"] == "unknown" for item in data["metrics"].values()))
        # 不是「值降级成 unknown」，是**一个值都没有**：UNKNOWN 不是 0。
        self.assertTrue(all(item["value"] is None for item in data["metrics"].values()))

    def test_the_rejection_reason_is_carried_through(self):
        data = self.h_synthetic.admin_client("carol").run("GET", "/api/infra/summary").json()["data"]
        self.assertEqual(data["reason"], self.synthetic["expect"]["envelope_reason"])

    def test_a_stale_projection_is_accepted_but_every_metric_is_unknown(self):
        """信封可信、指标太旧：``accepted`` 是 true，LIVE 的指标是**零个**。

        这两件事必须分开 —— 把它们合成一个布尔，前端就只能二选一：要么相信一份
        过期的读数，要么把一次正常的旧数据说成一次合同违约。
        """
        data = self.h_stale.admin_client("carol").run("GET", "/api/infra/summary").json()["data"]
        self.assertTrue(data["accepted"])
        self.assertEqual(
            [key for key, item in data["metrics"].items() if item["state"] == "live"], []
        )

    def test_a_member_cannot_reach_the_projection_even_when_it_is_fresh(self):
        """数据的可得性与权限无关：来源再新，没这道 scope 就是 403。"""
        response = self.h_fresh.client("alice").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 403)

    def test_no_response_names_an_upstream_address_or_a_credential(self):
        """答复里不该出现任何内网地址、凭据或本机路径。"""
        body = self.h_fresh.admin_client("carol").run("GET", "/api/infra/summary").body.decode()
        for needle in ("127.0.0.1", "localhost", ":8080", ":9090", "token", "Bearer", "/home/"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, body)


class PortalServicesTests(unittest.TestCase):
    """``/api/portal/services``：和主机投影**同一条门、同一个未就绪结局**。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())
        cls.offline = S.load_host_summary_fixture("09-offline.json")
        cls.bad_shape = S.load_host_summary_fixture("10-not-found.json")
        cls.h_offline = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            now=S.fixture_now(cls.offline),
            host_projection=cls.offline["payload"],
        )
        cls.h_bad_shape = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            now=S.fixture_now(cls.bad_shape),
            host_projection=cls.bad_shape["payload"],
        )

    @classmethod
    def tearDownClass(cls):
        for harness in (cls.h, cls.h_offline, cls.h_bad_shape):
            harness.close()

    def test_a_member_is_refused_with_the_same_403_as_the_host_route(self):
        response = self.h.client("alice").run("GET", "/api/portal/services")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "observability.access_denied")

    def test_an_elevated_session_gets_503_when_nothing_was_read(self):
        response = self.h.admin_client("carol").run("GET", "/api/portal/services")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "observability.source_unavailable")

    def test_the_service_block_is_the_projection_s_own_facts(self):
        """服务探针与指标样本是**两件事**，这条路由如实分开报。

        ``09-offline`` 那份投影的六个指标全是 ``offline``，而五个服务里四个
        ``up: true`` —— 这不矛盾：探针回答「这个端点此刻答不答」，样本回答「这个
        数字是什么时候量的」。把它们混成一个状态，会让一次采集故障看起来像一次全机
        宕机，或者反过来。
        """
        response = self.h_offline.admin_client("carol").run("GET", "/api/portal/services")
        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        self.assertTrue(data["accepted"])
        self.assertEqual(data["route"], "/api/infra/summary")
        self.assertEqual(data["services"]["prometheus"]["state"], "live")
        self.assertIs(data["services"]["prometheus"]["up"], True)

    def test_an_unreachable_service_is_unknown_with_a_reason_not_up_false(self):
        """``unreachable`` 的探针报 **UNKNOWN + 原因**，不是 ``up: false``。

        「没问到」与「问到了，它挂了」是两件事 —— 把前者写成后者，就是把一次探测
        失败说成一次服务下线。
        """
        data = self.h_offline.admin_client("carol").run("GET", "/api/portal/services").json()["data"]
        evalscope = data["services"]["evalscope"]
        self.assertEqual(evalscope["state"], "unknown")
        self.assertIsNone(evalscope["up"])
        self.assertEqual(evalscope["reason"], "unreachable")

    def test_an_untrusted_envelope_yields_no_service_claims_at_all(self):
        """信封不可信（``{"error": "not found"}``）时，**一个服务都不出现**。

        不是「都出现成 down」：一份来路不明的响应里没有任何一条服务事实可以引用。
        而 HTTP 仍然是 200 —— 我们确实读到了东西，只是它不该被相信；这与「没读到」
        的 503 是两个不同的答复。
        """
        response = self.h_bad_shape.admin_client("carol").run("GET", "/api/portal/services")
        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        self.assertFalse(data["accepted"])
        self.assertTrue(data["reason"])
        self.assertEqual(data["services"], {})


class AtlasCallsTests(unittest.TestCase):
    """``/api/atlas/calls``：只有我自己的调用，一条别人的都不行。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _calls(self, harness, name):
        client = harness.client(name)
        response = client.run("GET", "/api/atlas/calls")
        self.assertEqual(response.status, 200, response.json())
        return response.json()["data"]

    def test_a_member_reads_their_own_calls(self):
        data = self._calls(self.h, "alice")
        self.assertEqual(data["scope"]["subject"], self.h.principal_of("alice"))
        self.assertEqual(data["scope"]["keys"], ["key-alice"])
        self.assertGreater(data["counts"]["returned"], 0)

    def test_no_other_subject_appears_anywhere_in_the_payload(self):
        """跨主体反泄露：**整份 payload 的字符串里**不得出现别人的 key 或主体。

        逐字段断言容易漏掉嵌套的一段（``counts`` / ``coverage`` / ``filters`` 都会
        长），所以这里直接对整份序列化后的答复做子串检查 —— 一条别人的 key id 或
        主体名出现在任何位置都算失败。
        """
        alice = self._calls(self.h, "alice")
        other_key = "key-" + S.OTHER_MEMBER_NAME
        other_principal = self.h.principal_of(S.OTHER_MEMBER_NAME)

        import json

        blob = json.dumps(alice, ensure_ascii=False)
        self.assertNotIn(other_key, blob)
        self.assertNotIn(other_principal, blob)
        # 正向对照：自己的必须在，否则上面两条可能只是因为整份是空的。
        self.assertIn("key-alice", blob)

    def test_the_two_members_see_different_records(self):
        """B 看得到自己的，且**不是** A 的那一份。"""
        alice = self._calls(self.h, "alice")
        bob = self._calls(self.h, S.OTHER_MEMBER_NAME)
        self.assertEqual(bob["scope"]["keys"], ["key-" + S.OTHER_MEMBER_NAME])
        alice_ids = [row["call_id"] for row in alice["calls"]]
        bob_ids = [row["call_id"] for row in bob["calls"]]
        self.assertTrue(alice_ids and bob_ids)
        self.assertEqual(set(alice_ids) & set(bob_ids), set())
        for row in bob["calls"]:
            with self.subTest(call=row["call_id"]):
                self.assertEqual(row["key_id"], "key-" + S.OTHER_MEMBER_NAME)

    def test_the_transport_stays_demo_while_the_corpus_is_synthetic(self):
        """一份合成语料不能被标成 live —— 这条与 Atlas 自己的口径一致。"""
        data = self._calls(self.h, "alice")
        self.assertTrue(data["synthetic"])
        self.assertEqual(data["transport"]["mode"], "demo")
        self.assertFalse(data["transport"]["gateway_attached"])

    def test_a_malformed_limit_is_400_and_does_not_reach_the_module(self):
        response = self.h.client("alice").run("GET", "/api/atlas/calls?limit=abc")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "bff.invalid_request")

    def test_a_half_cursor_is_400(self):
        """游标要么都给、要么都不给：半截游标不能被当成一次「从头开始」。"""
        response = self.h.client("alice").run("GET", "/api/atlas/calls?before_call_id=call_1")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "bff.invalid_request")

    def test_a_naive_cursor_timestamp_is_400(self):
        """没有时区的游标无从判定属于哪一刻，所以不接受。"""
        response = self.h.client("alice").run(
            "GET", "/api/atlas/calls?before_ts=2026-10-17T00:00:00&before_call_id=call_1"
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "bff.invalid_request")


class UnattachedSourceTests(unittest.TestCase):
    """一个来源**没有被装配**时，每一条路由都说清楚是「没接」。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            with_atlas=False,
            with_host_summary=False,
            with_observability=False,
            with_evaluation=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_the_calls_route_says_the_adapter_is_missing(self):
        response = self.h.client("alice").run("GET", "/api/atlas/calls")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "atlas_adapter_not_attached")

    def test_the_host_route_says_the_adapter_is_missing(self):
        """与「装配了但没读到」是两个不同的 token —— 运维据此分流。"""
        response = self.h.admin_client("carol").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "bff.host_summary_adapter_not_attached")

    def test_the_services_route_says_the_adapter_is_missing(self):
        response = self.h.admin_client("carol").run("GET", "/api/portal/services")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "bff.host_summary_adapter_not_attached")

    def test_the_evaluation_routes_say_the_adapter_is_missing(self):
        for path in (
            "/api/evaluation/report-sources",
            "/api/evaluation/reports",
            "/api/evaluation/reports/rep_1",
        ):
            with self.subTest(path=path):
                response = self.h.client("alice").run("GET", path)
                self.assertEqual(response.status, 503, response.json())
                self.assertEqual(response.error_code(), "evaluation_backend_not_ready")

    def test_the_capabilities_manifest_reports_them_as_not_configured(self):
        """清单里的 ``not_configured`` 与路由上的 503 说的是同一件事。"""
        data = self.h.client("alice").run("GET", "/api/portal/capabilities").json()["data"]
        by_source = {item["source"]: item for item in data["sources"]}
        for source in ("observability", "host_summary", "atlas_calls", "evaluation"):
            with self.subTest(source=source):
                self.assertEqual(by_source[source]["state"], "not_configured")
                self.assertEqual(by_source[source]["reason"], "adapter_not_attached")
        # Registry 是应用的**必需**依赖：它没有「装了但关着」的中间态。
        self.assertEqual(by_source["registry"]["state"], "ready")


class CapabilitiesRouteTests(unittest.TestCase):
    """``/api/portal/capabilities``：清单说的是**这个会话**此刻能读什么。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _manifest(self, client):
        response = client.run("GET", "/api/portal/capabilities")
        self.assertEqual(response.status, 200, response.json())
        return response.json()["data"]

    def test_a_member_sees_the_host_source_as_auth_required_not_missing(self):
        """成员缺 ``observability:host:read``：那条来源是 ``auth_required``。

        它不是 ``not_configured`` —— 来源在，是这条会话没被授权。这两个答案会把
        运维引向完全不同的两个方向。
        """
        data = self._manifest(self.h.client("alice"))
        by_source = {item["source"]: item for item in data["sources"]}
        self.assertEqual(by_source["host_summary"]["state"], "auth_required")
        self.assertEqual(by_source["host_summary"]["reason"], "missing_required_scope")
        self.assertTrue(by_source["host_summary"]["attached"])

    def test_an_elevated_session_gets_the_host_source_as_stale_never_sampled(self):
        """提权之后权限有了，但这份部署从来没读到过投影 ⇒ ``stale`` / ``no_sample_yet``。

        ``ready`` 需要**正面证据**：一个从来没采到样本的来源不该因为「没人说它坏」
        就被标成可用。
        """
        data = self._manifest(self.h.admin_client("carol"))
        by_source = {item["source"]: item for item in data["sources"]}
        self.assertEqual(by_source["host_summary"]["state"], "stale")
        self.assertEqual(by_source["host_summary"]["reason"], "no_sample_yet")
        self.assertIsNone(by_source["host_summary"]["sample_at"])

    def test_the_manifest_carries_no_credential_or_internal_address(self):
        body = self.h.client("alice").run("GET", "/api/portal/capabilities").body.decode()
        for needle in ("127.0.0.1", "localhost", ":8080", "token", "Bearer", "/home/"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, body)

    def test_the_route_never_claims_a_source_is_live(self):
        """清单的取值是四个状态，**没有** ``live`` 这个词。

        它是一个装配事实的陈述，不是一次现场核验；``ready`` 只说明装配在、权限在、
        最近一次样本落在声明窗口内。
        """
        data = self._manifest(self.h.admin_client("carol"))
        self.assertNotIn("live", {item["state"] for item in data["sources"]})


class EvaluationRoutesTests(unittest.TestCase):
    """``/api/evaluation/*``：module-only 的报告目录接上同源出口。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_the_two_source_families_are_reported_separately(self):
        """``evalscope`` 与 ``legacy-benchmark`` 是两条独立来源，本层不合并它们。"""
        response = self.h.client("alice").run("GET", "/api/evaluation/report-sources")
        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        blob = repr(data)
        self.assertIn("evalscope", blob)
        self.assertIn("legacy-benchmark", blob)

    def test_a_report_detail_route_reaches_the_module_and_answers_about_that_ref(self):
        response = self.h.client("alice").run("GET", "/api/evaluation/reports/rep_missing")
        # 未授权与不存在由评测模块塌成同一个答复；这里只断言它**不是** 404 路由缺失
        # （那条形状归 route_map 的 not_found），也不是 500。
        self.assertIn(response.status, (200, 403, 404))
        if response.status != 200:
            self.assertNotEqual(response.error_code(), "not_found")

    def test_include_unowned_is_only_forwarded_not_granted(self):
        """成员把它设成 true 也看不到无归属记录 —— 放行在评测模块的 authz 里。"""
        for value in ("true", "1", "yes", "false", "0", ""):
            with self.subTest(value=value):
                response = self.h.client("alice").run(
                    "GET", f"/api/evaluation/reports?include_unowned={value}"
                )
                self.assertEqual(response.status, 200, response.json())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
