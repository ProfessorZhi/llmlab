"""V1 BFF 接缝交付（Issue #78）：清单、可注入接缝、以及"第四条路由由谁挂"。

BFF（Issue #79 / PR #85）把它的接缝写成 ``SOURCE_SEAMS``，并在 ``BFF_BATCH1_SEAMS.md``
里说：那张表"同时就是 DTO seam 声明"，别的 Lane 接的是表里的一个条目。本组验证**本侧**
交出的那一半：

* 四条报告路由（三条既有 + 一条**本 Lane 新增的受限下载**）被逐字声明；
* 三条既有路由与评测模块**自己的**路由表（``api.ROUTES``）逐字一致——两张表漂移过一次，
  前端就会对着一条不存在的路由写代码；
* 下载路由**不**在 ``api.ROUTES`` 里：本 Lane 只提供判定，挂载是 ``backend/bff`` 的事
  （本 Lane 的改动面不含 ``backend/bff/**`` 的 ``route_map.py``）；
* 没接线时该下发什么状态码，写在 :data:`SEAM_ERROR_STATUS` 里（BFF 面的 503），因为本仓库
  评测 API 自己对 ``NotConfigured`` 的映射是 501——两张映射各有各的读者。
"""

from __future__ import annotations

import unittest

from backend.evaluation import api as api_module
from backend.evaluation import report_seam
from backend.evaluation.errors import InvalidRequest
from backend.evaluation.identity import SCOPE_EVAL_READ
from backend.evaluation.provenance import SOURCE_EVALSCOPE, SOURCE_OK
from backend.evaluation.report_seam import (
    DOWNLOAD_ROUTE,
    REPORT_ROUTE_SEAMS,
    SEAM_ERROR_STATUS,
    EvaluationReportSeam,
    ReportRouteSeam,
    manifest,
    self_check,
)

from tests.evaluation import HOST_USERS
from tests.evaluation.support import (
    EPOCH,
    PROJECT_ALPHA,
    REPORT_REF_EVALSCOPE,
    build_harness_with_reports,
    close_all,
    report_owner,
    report_record,
    report_snapshot,
)

#: 本 Lane 交给 BFF 之前，BFF 那一轮已经挂上的三条。它们必须与本模块的路由表逐字一致。
PRE_EXISTING_REPORT_ROUTES = (
    "/api/evaluation/report-sources",
    "/api/evaluation/reports",
    "/api/evaluation/reports/{report_ref}",
)


def _harness():
    return build_harness_with_reports(
        (
            report_snapshot(
                SOURCE_EVALSCOPE,
                SOURCE_OK,
                [
                    report_record(
                        SOURCE_EVALSCOPE,
                        REPORT_REF_EVALSCOPE,
                        report_id="r-seam",
                        owner=report_owner("alice", project_id=PROJECT_ALPHA),
                        observed_at=EPOCH,
                        created_at=EPOCH,
                    )
                ],
                observed_at=EPOCH,
            ),
        )
    )


class SeamManifestTests(unittest.TestCase):
    def test_the_manifest_declares_the_four_report_routes(self) -> None:
        self.assertEqual(
            set(report_seam.ROUTES),
            set(PRE_EXISTING_REPORT_ROUTES) | {DOWNLOAD_ROUTE},
        )

    def test_the_download_route_is_the_fourth_and_is_declared(self) -> None:
        self.assertIn(DOWNLOAD_ROUTE, report_seam.ROUTES)
        self.assertEqual(len(REPORT_ROUTE_SEAMS), 4)

    def test_the_pre_existing_routes_match_the_modules_own_route_table(self) -> None:
        """清单里的路由必须真的存在于 ``api.ROUTES``——否则前端会对一条不存在的路由写代码。"""
        declared = {route.template for route in api_module.ROUTES}
        for route in PRE_EXISTING_REPORT_ROUTES:
            with self.subTest(route=route):
                self.assertIn(route, declared)

    def test_the_download_route_is_not_in_the_modules_own_route_table(self) -> None:
        """本 Lane 只交付判定；挂载这条路由是 ``backend/bff`` 的事，不在本 Lane 的改动面。"""
        declared = {route.template for route in api_module.ROUTES}
        self.assertNotIn(DOWNLOAD_ROUTE, declared)

    def test_every_seam_requires_the_read_scope(self) -> None:
        for seam in REPORT_ROUTE_SEAMS:
            with self.subTest(route=seam.route):
                self.assertEqual(seam.scope, SCOPE_EVAL_READ)

    def test_no_seam_puts_freshness_on_a_ttl(self) -> None:
        """逐来源的新鲜度由评测模块自己的 DTO 判定；在 BFF 层再判一次会得到两份答案。"""
        for seam in REPORT_ROUTE_SEAMS:
            with self.subTest(route=seam.route):
                self.assertIsNone(seam.ttl_seconds)

    def test_the_adapter_attribute_is_the_one_the_bff_shape_uses(self) -> None:
        self.assertEqual(report_seam.ADAPTER_ATTRIBUTE, "evaluation")
        for seam in REPORT_ROUTE_SEAMS:
            with self.subTest(route=seam.route):
                self.assertEqual(seam.adapter_attribute, report_seam.ADAPTER_ATTRIBUTE)
                self.assertEqual(seam.attachment, report_seam.ATTACHMENT_ADAPTER)

    def test_the_error_status_map_tells_the_bff_what_a_missing_source_answers(self) -> None:
        """本仓库评测 API 对 ``NotConfigured`` 的映射是 501；BFF 面按契约是 503。"""
        self.assertEqual(SEAM_ERROR_STATUS["evaluation.not_configured"], 503)
        self.assertEqual(SEAM_ERROR_STATUS["evaluation.not_found"], 404)
        self.assertEqual(SEAM_ERROR_STATUS["evaluation.report_download_refused"], 413)

    def test_self_check_finds_no_duplicate_route(self) -> None:
        self_check()

    def test_the_manifest_is_subject_free(self) -> None:
        """清单是一份**部署**描述：任何主体的名字都不该出现在里面。"""
        blob = repr(manifest())
        for principal, _uid in HOST_USERS:
            with self.subTest(principal=principal):
                self.assertNotIn(principal, blob)

    def test_the_manifest_declares_the_download_bound(self) -> None:
        payload = manifest()
        self.assertEqual(payload["owner"], "Evaluation#78")
        self.assertEqual(payload["adapter_attribute"], "evaluation")
        self.assertFalse(payload["download"]["reader"]["enabled"])
        self.assertFalse(payload["download"]["reader"]["configured"])
        self.assertEqual(payload["download_schema_version"], "eval-report-download/1")


class SeamShapeTests(unittest.TestCase):
    """构造期校验：一个写错的接缝应当在装配时炸掉，而不是在某条路由上表现成 500。"""

    def _seam(self, **overrides) -> ReportRouteSeam:
        base = dict(
            source="evaluation",
            route="/api/evaluation/reports",
            scope=SCOPE_EVAL_READ,
            ttl_seconds=None,
            adapter_attribute="evaluation",
            attachment="adapter",
        )
        base.update(overrides)
        return ReportRouteSeam(**base)

    def test_a_relative_route_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self._seam(route="api/evaluation/reports")

    def test_an_unknown_attachment_kind_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self._seam(attachment="maybe")

    def test_a_non_positive_ttl_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self._seam(ttl_seconds=0)

    def test_a_source_id_must_be_a_bare_token(self) -> None:
        with self.assertRaises(InvalidRequest):
            self._seam(source="some/path")

    def test_a_seam_must_name_the_attribute_that_carries_it(self) -> None:
        with self.assertRaises(InvalidRequest):
            self._seam(adapter_attribute="")

    def test_the_dict_shape_carries_every_field_the_bff_table_needs(self) -> None:
        payload = self._seam().to_dict()
        self.assertEqual(
            set(payload),
            {
                "source",
                "owner",
                "route",
                "scope",
                "ttl_seconds",
                "adapter_attribute",
                "attachment",
                "note",
            },
        )
        self.assertEqual(payload["owner"], "Evaluation#78")


class InjectableSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _harness()
        self.addCleanup(close_all)
        self.seam = EvaluationReportSeam(self.harness.service)

    def _ref(self) -> str:
        data = self.seam.reports(requester=self.harness.requester("alice"))
        return data["reports"][0]["report_ref"]

    def test_the_seam_returns_the_data_block_the_bff_consumes(self) -> None:
        data = self.seam.reports(requester=self.harness.requester("alice"))
        self.assertEqual(len(data["reports"]), 1)
        self.assertIn("sources", data)

    def test_the_seam_exposes_the_download_seam_the_bff_adapter_lacks(self) -> None:
        plan = self.seam.report_download(
            requester=self.harness.requester("alice"), report_ref=self._ref()
        )
        self.assertTrue(plan["authorized"])
        self.assertFalse(plan["content_read"])
        self.assertEqual(plan["reason"], "report_download_disabled")

    def test_the_seam_and_the_module_agree_on_which_reports_exist(self) -> None:
        """同一条 service，两种读法：接缝不得多看见或少看见一份报告。"""
        requester = self.harness.requester("alice")
        via_seam = {row["report_ref"] for row in self.seam.reports(requester=requester)["reports"]}
        via_module = {
            row["report_ref"]
            for row in self.harness.service.reports(requester=requester)["data"]["reports"]
        }
        self.assertEqual(via_seam, via_module)

    def test_the_seam_refuses_a_service_without_the_download_method(self) -> None:
        """第四条是本 Lane 新增的：一个还没接线下载的 service 不该被静默接上。"""

        class PartialService:
            def report_sources(self, **kwargs):  # pragma: no cover - 不该被调到
                return {"data": {}}

            def reports(self, **kwargs):  # pragma: no cover - 不该被调到
                return {"data": {}}

            def report_detail(self, **kwargs):  # pragma: no cover - 不该被调到
                return {"data": {}}

        with self.assertRaises(InvalidRequest) as ctx:
            EvaluationReportSeam(PartialService())
        self.assertIn("report_download", str(ctx.exception))

    def test_the_seam_reports_the_bound_it_would_apply(self) -> None:
        bounds = self.seam.bounds()
        self.assertEqual(bounds["schema_version"], "eval-report-download/1")
        self.assertFalse(bounds["reader"]["enabled"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
