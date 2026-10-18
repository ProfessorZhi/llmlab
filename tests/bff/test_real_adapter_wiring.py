"""真实跨 Lane 注入缝的装配，以及评测第四条路由的安全边界。

这一组与 ``test_portal_routes.py`` 的分工很清楚：那一组验**路由存在时**的行为，
这一组验**别的 Lane 把东西交过来（或者没交）时** BFF 怎么接。

三条必须能被区分开的结局，测试逐条钉住：

* **另一个 Lane 还没合入这个分支** —— 装配函数返回 ``(None, 稳定 reason)``，
  路由 503。它不能表现成 ImportError 崩溃，也不能表现成一份看起来正常的空数据。
* **来源交给了我们但它自己是坏的**（文件缺失 / 越界 / 超限）—— 503，**并且**把
  来源自己的诊断码带出去（``source.file_missing`` 之类），而不是笼统的
  「adapter 没接」。运维据此知道该去查哪一件事。
* **来源是好的，但这次读不到** —— 仍然是 503，而不是「你最近没有调用」。

评测下载那部分用的是**接缝替身**（``tests/`` 内），因为 ``report_download`` 由
Evaluation Lane #78 / PR #91 交付、不在本分支的树上。替身验的是**本 Lane 的接线
契约**：身份只来自会话、路径不是参数、超界拒绝而不截断、读不到时不报字节数。
它不冒充 #91 的实现——那一条由 #91 自己的 59 条测试验。
"""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from backend.atlas.dto import Provenance as AtlasProvenance
from backend.atlas.identity import GatewayKeyBinding, KeyDirectory
from backend.evaluation.errors import EvaluationError as EvaluationErrorBase
from backend.bff import stacks as bff_stacks
from backend.bff.atlas_adapter import DetachedAtlasAdapter
from backend.bff.evaluation_adapter import EvaluationStack, ModuleEvaluationAdapter
from tests.bff import support as S

#: #91 交付的下载 DTO 形状（``ReportDownloadPlan.to_dict()``）。替身照它返回，
#: 好让本 Lane 的断言对的是**契约**而不是某一版实现。
DOWNLOAD_PLAN = {
    "schema_version": "eval-report-download/1",
    "report_ref": "evrp-0000000000000000000000000000000000000000",
    "source": "evalscope",
    "source_label": "EvalScope 报告",
    "authorized": True,
    "content_read": False,
    "content_type": None,
    "filename": None,
    "size_bytes": None,
    "max_bytes": 4 * 1024 * 1024,
    "reason": "report_content_reading_disabled",
    "note": "本部署不提供正文读取。",
}


class FakeReportService:
    """评测 service 的接缝替身：只实现 BFF 会调的那几个方法。

    ``report_download`` 可按用例换成抛错或换返回值；``seen`` 记下每一次调用收到的
    ``requester`` —— 身份「只来自会话」这件事只有这样才验得了。
    """

    def __init__(self, *, download=None, error=None, with_download=True):
        self.seen: list = []
        self._download = download if download is not None else dict(DOWNLOAD_PLAN)
        self._error = error
        if with_download:
            self.report_download = self._report_download  # type: ignore[assignment]

    # ModuleEvaluationAdapter 的构造期检查要求这个
    def jobs(self, *, requester, limit=None):
        self.seen.append(("jobs", requester))
        return {"data": {"jobs": []}}

    def report_sources(self, *, requester):
        self.seen.append(("report_sources", requester))
        return {"data": {"sources": []}}

    def reports(self, *, requester, include_unowned=False):
        self.seen.append(("reports", requester))
        return {"data": {"reports": []}}

    def report_detail(self, *, requester, report_ref, include_unowned=False):
        self.seen.append(("report_detail", requester))
        return {"data": {"report_ref": report_ref}}

    def _report_download(self, *, requester, report_ref, include_unowned=False):
        self.seen.append(("report_download", requester))
        if self._error is not None:
            raise self._error
        return {"data": dict(self._download)}


class FakeEvaluationError(EvaluationErrorBase):
    """一个只带 ``contract_code`` 的评测错误。

    **必须真的继承模块的基类**：#91 的 ``ReportDownloadRefused`` 是
    ``EvaluationError`` 的子类，因此 ``OpsApplication.handle`` 的
    ``except evaluation_errors.EvaluationError`` 会接住它、再按本层的
    ``EVALUATION_STATUS`` 映射成 413。一个只长得像的替身会从那个 ``except`` 旁边
    滑过去、落到兜底的 500 —— 替身失真会把一次接线错误伪装成一次服务崩溃。
    """

    def __init__(self, code, detail="synthetic"):
        super().__init__(detail)
        self.contract_code = code


class SliceOfSource:
    """``backend.atlas.corpus.AtlasSource`` 的最小鸭子替身。

    按字段读、不 import 那个模块：本 Lane 的分支不假设 #90 已经合入。
    """

    def __init__(self, **fields):
        self.available = fields.pop("available")
        self.reason = fields.pop("reason", None)
        for key, value in fields.items():
            setattr(self, key, value)


def _atlas_directory() -> KeyDirectory:
    return KeyDirectory(
        [GatewayKeyBinding("key-alice", "linux:dell-test-01:4001", "proj-lab", "w")]
    )


def _atlas_provenance() -> AtlasProvenance:
    return AtlasProvenance(source="synthetic-fixture", synthetic=True)


class HostSummarySourceWiringTests(unittest.TestCase):
    """Host Lane #74 的零参读取器 ⇒ ``HostSummaryStack``。"""

    def test_a_missing_lane_seam_is_a_stable_reason_not_an_import_crash(self):
        """#88 还没合入时，装配函数说一条稳定 reason。

        用 ``_load_host_summary_reader`` 这个 indirection 模拟「树里没有它」，
        因为「本机当前装了哪个 Lane」不是一个测试应该去猜的环境事实。
        """
        with mock.patch.object(
            bff_stacks, "_load_host_summary_reader", side_effect=ImportError("absent")
        ):
            stack, reason = bff_stacks.build_host_summary_stack_from_observability()
        self.assertIsNone(stack)
        self.assertEqual(reason, bff_stacks.REASON_HOST_SOURCE_NOT_DELIVERED)

    def test_when_the_seam_is_present_the_reader_is_wired_straight_through(self):
        """``host_summary_reader(**kwargs)`` 的返回值就是 ``read``，不加一层。"""
        seen = {}

        def fake_reader(*, enabled=False, **kwargs):
            seen["enabled"] = enabled
            seen["kwargs"] = kwargs
            return lambda: {"synthetic": False}

        with mock.patch.object(
            bff_stacks, "_load_host_summary_reader", return_value=fake_reader
        ):
            stack, reason = bff_stacks.build_host_summary_stack_from_observability(
                enabled=True
            )

        self.assertIsNone(reason)
        self.assertIsNotNone(stack)
        self.assertTrue(seen["enabled"])
        self.assertEqual(stack.read(), {"synthetic": False})

    def test_an_enabled_false_reader_still_yields_none_so_the_route_says_503(self):
        """Host Lane 的默认 ``enabled=False`` 回 ``None`` —— 那是「没接线」，不是「空」."""
        with mock.patch.object(
            bff_stacks,
            "_load_host_summary_reader",
            return_value=lambda **kwargs: (lambda: None),
        ):
            stack, reason = bff_stacks.build_host_summary_stack_from_observability()

        self.assertIsNone(reason)
        self.assertIsNone(stack.read())
        self.assertEqual(stack.max_sample_age_s, 90)

    def test_this_tree_actually_has_the_host_seam_or_reports_that_it_does_not(self):
        """把「本分支的树里到底有没有 #88」写成一次**可核对**的观察。

        它不断言某一个取值（那会让测试随合入顺序变色），它断言两种结局都是合法且
        各自自洽的：要么接上，要么带着那条稳定 reason 说没接上。PR 里报告的是这条
        观察在提交那一刻的实际结果。
        """
        stack, reason = bff_stacks.build_host_summary_stack_from_observability()
        self.assertTrue(
            (stack is None) != (reason is None),
            "exactly one of stack/reason must be set",
        )
        if stack is None:
            self.assertEqual(reason, bff_stacks.REASON_HOST_SOURCE_NOT_DELIVERED)
        else:
            self.assertEqual(stack.max_sample_age_s, 90)


class AtlasSourceWiringTests(unittest.TestCase):
    """Calls Lane #77 的 ``AtlasSource`` ⇒ ``AtlasStack``。"""

    def test_a_missing_source_is_named_not_silently_empty(self):
        stack, reason = bff_stacks.build_atlas_stack_from_source(
            None, project_scope=lambda _i: frozenset()
        )
        self.assertIsNone(stack)
        self.assertEqual(reason, bff_stacks.REASON_CALLS_SOURCE_NOT_DELIVERED)

    def test_an_unavailable_source_carries_its_own_diagnostic_code(self):
        """来源自己说「文件缺失」时，503 就说 ``source.file_missing``。

        把这条压成笼统的 ``atlas_adapter_not_attached``，等于把一个可排查的接线
        问题变成一句「没接」；而那个码只说明哪一道有界检查没过，不含任何内容。
        """
        source = SliceOfSource(available=False, reason="source.file_missing")
        stack, reason = bff_stacks.build_atlas_stack_from_source(
            source, project_scope=lambda _i: frozenset()
        )
        self.assertIsNone(stack)
        self.assertEqual(reason, "source.file_missing")

    def test_an_available_source_is_wired_whole(self):
        """可用的来源**整份**接上：目录、语料、provenance、声明，一样不缺。"""
        directory = _atlas_directory()
        provenance = _atlas_provenance()
        reader = lambda _identity: ""  # noqa: E731 - 替身而已
        source = SliceOfSource(
            available=True,
            reason=None,
            directory=directory,
            gateway_text=reader,
            provenance=provenance,
            gateway_attached=False,
            directory_revision="rev-1",
        )
        stack, reason = bff_stacks.build_atlas_stack_from_source(
            source, project_scope=lambda _i: frozenset({"proj-lab"})
        )
        self.assertIsNone(reason)
        self.assertIs(stack.directory, directory)
        self.assertIs(stack.gateway_text, reader)
        self.assertIs(stack.provenance, provenance)
        self.assertFalse(stack.gateway_attached)

    def test_a_detached_adapter_can_carry_the_source_reason(self):
        adapter = DetachedAtlasAdapter(reason="source.outside_root")
        self.assertFalse(adapter.attached)
        self.assertEqual(adapter.reason, "source.outside_root")
        self.assertEqual(adapter.availability(identity=object()), (False, "source.outside_root"))

    def test_the_default_detached_reason_is_unchanged(self):
        self.assertEqual(DetachedAtlasAdapter().reason, "atlas_adapter_not_attached")


class DownloadRouteShapeTests(unittest.TestCase):
    """那条路由在表里，而且**没有**被 ``report_detail`` 抢走。"""

    def test_the_download_route_is_its_own_declared_route(self):
        from backend.bff import route_map as RM

        spec = RM.match("/api/evaluation/reports/evrp_abc/download")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "evaluation.report_download")
        self.assertEqual(spec.source, RM.SOURCE_EVALUATION)
        self.assertEqual(spec.scope, "evaluation:read")
        self.assertFalse(spec.is_write)

    def test_the_detail_route_does_not_swallow_the_download_route(self):
        """两条模式路由共享前缀；``report_detail`` 的后缀是空串。

        它今天靠「中间段不含 ``/``」自己避开，但本表把 downnload 显式排在前面的
        理由正是：**顺序不该由另一条路径的内部检查来保证**。
        """
        from backend.bff import route_map as RM

        names = [spec.name for spec in RM.ROUTES]
        self.assertLess(
            names.index("evaluation.report_download"),
            names.index("evaluation.report_detail"),
        )
        self.assertIs(RM.match("/api/evaluation/reports/ref"), RM.ROUTES_BY_NAME["evaluation.report_detail"])
        self.assertIs(
            RM.match("/api/evaluation/reports/ref/download"),
            RM.ROUTES_BY_NAME["evaluation.report_download"],
        )

    def test_a_nested_path_is_not_a_report_reference(self):
        from backend.bff import route_map as RM

        self.assertIsNone(RM.match("/api/evaluation/reports/a/b/download"))


class DownloadRouteBehaviourTests(unittest.TestCase):
    """第四条路由的负向面与 fail-closed 面。"""

    PATH = "/api/evaluation/reports/evrp_abc/download"

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())

    def tearDown(self):
        self.h.close()

    def test_anonymous_is_401(self):
        response = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http").run(
            "GET", self.PATH
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_a_forged_identity_header_is_400(self):
        response = self.h.client("alice").run("GET", self.PATH, headers={"X-UID": "4001"})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_a_client_declared_path_is_refused_before_the_module_sees_it(self):
        """``?path=`` 是这条路由最该拒绝的参数：下载面的整个风险就在路径上。"""
        response = self.h.client("alice").run("GET", self.PATH + "?path=../../etc/passwd")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "proxy_assertion_rejected")

    def test_an_absent_adapter_is_503_not_a_guess(self):
        """评测适配器**缺席**时这条路由说 503。

        显式把适配器摘掉，而不是依赖 harness 默认怎么装配它 —— 后者会随
        「评测 Lane 合没合入」变色，而这不是这条断言要验的东西。
        （合入之后，一个**存在**的 service 对一个未知引用会答 404；那是另一条语义，
        由 ``DownloadSeamTests`` 里那条 404 用例覆盖。）
        """
        self.h.app.evaluation = None
        try:
            response = self.h.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 503, response.json())
            self.assertEqual(response.error_code(), "evaluation_backend_not_ready")
        finally:
            # 这个 harness 是 setUp 建的、tearDown 关的；适配器要在本用例内恢复，
            # 免得后面的用例继承一个被摘空的 app。
            self.h.app.evaluation = self.h.evaluation


class DownloadSeamTests(unittest.TestCase):
    """有接缝替身时，第四条路由的接线契约。"""

    PATH = "/api/evaluation/reports/evrp_abc/download"

    def _harness(self, service):
        harness = S.build_harness(state_dir=tempfile.mkdtemp())
        harness.app.evaluation = ModuleEvaluationAdapter(EvaluationStack(service=service))
        return harness

    def test_a_service_without_the_method_is_503_not_500(self):
        """评测 Lane 还没交付 ``report_download``：本部署没有这条功能 ⇒ 503。"""
        service = FakeReportService(with_download=False)
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 503, response.json())
            self.assertEqual(response.error_code(), "evaluation_backend_not_ready")
        finally:
            harness.close()

    def test_a_non_readable_plan_is_200_without_bytes_or_paths(self):
        """正确的 200 是一份**计划**：没有字节、没有相对路径、没有绝对路径。"""
        service = FakeReportService()
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 200, response.json())
            data = response.json()["data"]
            self.assertFalse(data["content_read"])
            self.assertTrue(data["reason"])
            self.assertIsNone(data["size_bytes"], "an unreadable plan must not assert a size")
            self.assertNotIn("path_ref", data)
            self.assertNotIn("relative_path", data)
            blob = response.body.decode()
            for needle in ("/home/", "../", "etc/passwd"):
                with self.subTest(needle=needle):
                    self.assertNotIn(needle, blob)
        finally:
            harness.close()

    def test_the_identity_handed_to_the_module_is_the_session_not_the_request(self):
        """``requester.principal`` 必须等于会话主体。

        这是整条下载面唯一一处「谁在取」的判断点：本层给什么，模块就按什么判 ACL。
        """
        service = FakeReportService()
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 200)
            calls = [entry for entry in service.seen if entry[0] == "report_download"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1].principal, harness.principal_of("alice"))

            # 请求里塞一个主体字段，不改变任何东西（而且会被守卫先拒掉）。
            forged = harness.client("alice").run(
                "GET", self.PATH + "?principal=" + harness.principal_of(S.OTHER_MEMBER_NAME)
            )
            self.assertEqual(forged.status, 400)
            self.assertEqual(len(service.seen), 1, "the module was not called again")
        finally:
            harness.close()

    def test_not_configured_is_503_on_the_browser_face(self):
        """模块自己的 HTTP 面把 ``NotConfigured`` 讲成 501；浏览器面必须是 503。

        两张映射各有各的读者：501 说的是「本服务器的这个资源没实现」，而前端的
        取数状态机把 503 读成「这条来源此刻不可用」。这里钉住 BFF 的那一份。
        """
        from backend.evaluation.errors import NotConfigured

        service = FakeReportService(error=NotConfigured("synthetic"))
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 503, response.json())
            self.assertEqual(response.error_code(), "evaluation.not_configured")
        finally:
            harness.close()

    def test_a_path_rejection_is_400(self):
        from backend.evaluation.errors import ReportPathRejected

        service = FakeReportService(error=ReportPathRejected("synthetic"))
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 400, response.json())
            self.assertEqual(response.error_code(), "evaluation.report_path_rejected")
        finally:
            harness.close()

    def test_an_over_bound_download_is_413_not_a_truncated_file(self):
        """超界**拒绝**而不截断；那个 413 由 #91 的 ``ReportDownloadRefused`` 带出。"""
        service = FakeReportService(error=FakeEvaluationError("evaluation.report_download_refused"))
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 413, response.json())
            self.assertEqual(response.error_code(), "evaluation.report_download_refused")
        finally:
            harness.close()

    def test_a_missing_or_invisible_report_is_404_with_no_existence_hint(self):
        from backend.evaluation.errors import NotFound

        service = FakeReportService(error=NotFound("synthetic"))
        harness = self._harness(service)
        try:
            response = harness.client("alice").run("GET", self.PATH)
            self.assertEqual(response.status, 404)
            self.assertEqual(response.error_code(), "evaluation.not_found")
        finally:
            harness.close()


class RegistryRouterAckIsInvisibleToTheBFFTests(unittest.TestCase):
    """Registry #89 换了 ``router_sync`` 的语义；本层**不该**看得见它。

    ``/api/v2/ops`` 的 deployment 行由 ``registry_dto.render_deployment`` 产出，
    而它等于 ``Deployment.to_dict()`` —— 里面没有 ``router_sync`` 块（那是 **endpoint**
    的视图）。所以 #89 的取值变化对本层是**不可达**的，而不是「碰巧没测到」。
    这条断言把「不可达」写成事实：将来谁想把那个块加进 ops DTO，会先在这里被问到
    「你确定要把一个策略判据下发给浏览器吗」。
    """

    def test_the_ops_deployment_row_carries_no_router_sync_block(self):
        h = S.build_harness(state_dir=tempfile.mkdtemp())
        try:
            S.register_baseline(h)
            data = h.client("alice").run("GET", "/api/v2/ops").json()["data"]["ops"]
            rows = data["registry"]["owned"]
            self.assertTrue(rows, "the fixture must produce at least one deployment row")
            for row in rows:
                with self.subTest(deployment=row["deployment_id"]):
                    self.assertNotIn("router_sync", row)
                    self.assertNotIn("pending_router_sync", row)
                    self.assertNotIn("router_ack_evidence", row)
        finally:
            h.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
