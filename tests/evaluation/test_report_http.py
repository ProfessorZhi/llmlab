"""报告目录的 HTTP 与服务面：匿名、跨 UID、不透明引用、空态、无绝对路径。

这一组跑**真的传输层**（进程内 WSGI + 一次真实回环 socket），证明的是契约而不是
目录规则：状态码、来源字段、引用形状、以及"响应里永远没有报告路径"。

负向用例（这是本工单的重点）：

* 匿名 -> 401；缺 ``evaluation:read`` -> 403；
* 跨 UID 的报告在列表里不出现，按引用取 -> 404，且与"引用不存在"的响应**逐字节同形**；
* ``include_unowned`` 的非法取值 -> 400；成员传 true 仍然看不到无归属记录；
* 响应体里不出现任何文档路径（引用是不透明 id，不是路径）。
"""

from __future__ import annotations

import unittest

from backend.evaluation.dto import validate_dto
from backend.evaluation.errors import AccessDenied
from backend.evaluation.identity import RequesterContext
from backend.evaluation.models import metric_known
from backend.evaluation.provenance import (
    DIMENSION_QUALITY,
    SOURCE_EMPTY,
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    SOURCE_OK,
    SOURCE_UNCONFIGURED,
)

from tests.evaluation.support import (
    EPOCH,
    REPORT_REF_EVALSCOPE,
    REPORT_REF_LEGACY,
    Clock,
    build_harness_with_reports,
    close_all,
    http_request,
    report_owner,
    report_record,
    report_snapshot,
    serve,
)

#: 任何主机文件系统路径都不该出现在报告响应里。
FORBIDDEN_PATH_MARKERS = ("/home/", "/srv/", "/opt/", "/tmp/", "/etc/", "/var/")


def alice_evalscope_record():
    return report_record(
        SOURCE_EVALSCOPE,
        REPORT_REF_EVALSCOPE,
        report_id="r-alice",
        owner=report_owner("alice"),
        title="<b>smoke</b>",
        dimensions={DIMENSION_QUALITY: metric_known(DIMENSION_QUALITY, 0.9)},
    )


def dave_legacy_record():
    return report_record(
        SOURCE_LEGACY_BENCHMARK,
        REPORT_REF_LEGACY,
        report_id="r-dave",
        owner=report_owner("dave", project_id="proj-beta"),
    )


def unowned_legacy_record():
    return report_record(
        SOURCE_LEGACY_BENCHMARK,
        "legacy/benchmark/orphan-1.json",
        report_id="r-orphan",
        owner=None,
    )


def populated_harness(*, clock: Clock | None = None):
    snapshots = (
        report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [alice_evalscope_record()], observed_at=EPOCH),
        report_snapshot(
            SOURCE_LEGACY_BENCHMARK,
            SOURCE_OK,
            [dave_legacy_record(), unowned_legacy_record()],
            observed_at=EPOCH,
        ),
    )
    return build_harness_with_reports(snapshots, clock=clock or Clock())


def assert_no_paths(test: unittest.TestCase, payload: object) -> None:
    text = repr(payload)
    for marker in FORBIDDEN_PATH_MARKERS:
        test.assertNotIn(marker, text)


class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = populated_harness()

    def tearDown(self) -> None:
        close_all()

    def test_anonymous_is_rejected(self) -> None:
        for path in ("/api/evaluation/reports", "/api/evaluation/report-sources"):
            with self.subTest(path=path):
                status, _headers, body = self.harness.body("GET", path)
                self.assertEqual(status, 401)
                self.assertFalse(body["ok"])

    def test_a_member_can_list_reports(self) -> None:
        status, _headers, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="alice"
        )
        self.assertEqual(status, 200)
        validate_dto(body)
        refs = [row["report_ref"] for row in body["data"]["reports"]]
        self.assertIn(alice_evalscope_record().ref_id, refs)

    def test_missing_evaluation_read_is_a_403(self) -> None:
        requester = RequesterContext(
            principal="alice", uid=4001, linux_user="alice", scopes=frozenset()
        )
        with self.assertRaises(AccessDenied):
            self.harness.service.reports(requester=requester)


class SourceSeparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = populated_harness()

    def tearDown(self) -> None:
        close_all()

    def test_each_row_declares_its_source(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="alice"
        )
        row = body["data"]["reports"][0]
        self.assertEqual(row["source"], SOURCE_EVALSCOPE)
        self.assertEqual(row["source_label"], "EvalScope 报告")

    def test_a_legacy_run_is_labelled_legacy_not_evalscope(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="dave"
        )
        rows = body["data"]["reports"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], SOURCE_LEGACY_BENCHMARK)
        self.assertNotEqual(rows[0]["source"], SOURCE_EVALSCOPE)

    def test_the_sources_endpoint_is_scoped_to_the_principal(self) -> None:
        """``pipeline_status`` 的全局 ``ok`` 会泄露"别人的报告在不在"，因此不能下发。

        alice 有一份自己的 EvalScope 报告 -> ``ok``；legacy 里只有 dave 的与一条
        无归属记录，alice 一条都看不见 -> ``empty``（**不是** ``ok``）。
        """
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/report-sources", as_principal="alice"
        )
        states = {row["source"]: row["state"] for row in body["data"]["sources"]}
        self.assertEqual(states[SOURCE_EVALSCOPE], SOURCE_OK)
        self.assertEqual(states[SOURCE_LEGACY_BENCHMARK], SOURCE_EMPTY)


class CrossUidTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = populated_harness()

    def tearDown(self) -> None:
        close_all()

    def test_a_member_never_sees_another_users_report(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="dave"
        )
        refs = {row["report_ref"] for row in body["data"]["reports"]}
        self.assertNotIn(alice_evalscope_record().ref_id, refs)

    def test_an_unowned_report_is_invisible_to_a_member(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="dave"
        )
        refs = {row["report_ref"] for row in body["data"]["reports"]}
        self.assertNotIn(unowned_legacy_record().ref_id, refs)

    def test_a_member_cannot_fetch_another_users_report_by_reference(self) -> None:
        status, _headers, body = self.harness.request(
            "GET",
            f"/api/evaluation/reports/{alice_evalscope_record().ref_id}",
            as_principal="dave",
        )
        self.assertEqual(status, 404)

    def test_an_unknown_reference_and_a_forbidden_one_are_byte_identical(self) -> None:
        _s1, _h1, unknown = self.harness.request(
            "GET", "/api/evaluation/reports/evrp-" + "0" * 24, as_principal="alice"
        )
        status, _h2, forbidden = self.harness.request(
            "GET",
            f"/api/evaluation/reports/{dave_legacy_record().ref_id}",
            as_principal="alice",
        )
        self.assertEqual(status, 404)
        self.assertEqual(unknown, forbidden)


class OwnerResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = populated_harness()

    def tearDown(self) -> None:
        close_all()

    def test_the_owner_can_fetch_their_own_report(self) -> None:
        status, _headers, body = self.harness.request(
            "GET",
            f"/api/evaluation/reports/{alice_evalscope_record().ref_id}",
            as_principal="alice",
        )
        self.assertEqual(status, 200)
        entry = body["data"]["report"]
        self.assertTrue(entry["preview"]["authorized"])
        self.assertFalse(entry["preview"]["content_read"])
        self.assertFalse(entry["download"]["content_read"])

    def test_an_admin_manage_with_the_flag_sees_the_unowned_report(self) -> None:
        status, _headers, body = self.harness.request(
            "GET",
            "/api/evaluation/reports",
            as_principal="carol",
            query="include_unowned=true",
        )
        self.assertEqual(status, 200)
        rows = {row["report_ref"]: row for row in body["data"]["reports"]}
        orphan = rows[unowned_legacy_record().ref_id]
        self.assertEqual(orphan["owner_state"], "unowned")
        self.assertIsNone(orphan["owner"])

    def test_an_admin_without_the_flag_does_not_see_the_unowned_report(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="carol"
        )
        refs = {row["report_ref"] for row in body["data"]["reports"]}
        self.assertNotIn(unowned_legacy_record().ref_id, refs)

    def test_a_member_cannot_include_unowned_reports(self) -> None:
        _s, _h, body = self.harness.request(
            "GET",
            "/api/evaluation/reports",
            as_principal="alice",
            query="include_unowned=true",
        )
        refs = {row["report_ref"] for row in body["data"]["reports"]}
        self.assertNotIn(unowned_legacy_record().ref_id, refs)

    def test_a_malformed_include_unowned_is_a_400(self) -> None:
        status, _headers, body = self.harness.request(
            "GET",
            "/api/evaluation/reports",
            as_principal="alice",
            query="include_unowned=yes",
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class EmptyStateTests(unittest.TestCase):
    def tearDown(self) -> None:
        close_all()

    def test_a_reachable_empty_source_is_a_counted_zero(self) -> None:
        harness = build_harness_with_reports(
            (
                report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, observed_at=EPOCH),
                report_snapshot(SOURCE_LEGACY_BENCHMARK, SOURCE_UNCONFIGURED, observed_at=None),
            )
        )
        _s, _h, body = harness.request("GET", "/api/evaluation/reports", as_principal="alice")
        empty = body["data"]["empty_state"]
        self.assertEqual(empty["value"], 0)
        self.assertEqual(empty["state"], "empty")

    def test_when_nothing_is_wired_the_empty_state_is_unknown_not_zero(self) -> None:
        harness = build_harness_with_reports(())
        _s, _h, body = harness.request("GET", "/api/evaluation/reports", as_principal="alice")
        empty = body["data"]["empty_state"]
        self.assertIsNone(empty["value"])
        self.assertEqual(empty["state"], "unknown")

    def test_a_reachable_source_with_invisible_records_is_a_scoped_zero(self) -> None:
        """来源里有记录，但一条都不属于我 -> 对我而言就是 0 份（一个主体范围的计数）。"""
        harness = build_harness_with_reports(
            (
                report_snapshot(
                    SOURCE_LEGACY_BENCHMARK,
                    SOURCE_OK,
                    [dave_legacy_record()],
                    observed_at=EPOCH,
                ),
            )
        )
        _s, _h, body = harness.request("GET", "/api/evaluation/reports", as_principal="alice")
        empty = body["data"]["empty_state"]
        self.assertEqual(empty["value"], 0)
        self.assertEqual(empty["label"], "0 份报告")


class ForeignReportInvarianceTests(unittest.TestCase):
    """P0-A：别人新增一份报告，**不能**改变我的任何一条响应。

    这是"来源状态是跨租户存在性侧信道"的正面回归：若 ``state``/``observed_at``/``reason``
    或空态里出现任何依赖他人数据的字节，这组断言会逐字节失败。
    """

    def _harness(self, extra_evalscope: bool):
        records = [alice_evalscope_record()]
        if extra_evalscope:
            records.append(
                report_record(
                    SOURCE_EVALSCOPE,
                    "evalscope/reports/dave-1.json",
                    report_id="r-dave-ev",
                    owner=report_owner("dave", project_id="proj-beta"),
                )
            )
        return build_harness_with_reports(
            (
                report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, records, observed_at=EPOCH),
                report_snapshot(SOURCE_LEGACY_BENCHMARK, SOURCE_EMPTY, observed_at=EPOCH),
            )
        )

    def tearDown(self) -> None:
        close_all()

    def test_reports_response_is_byte_identical(self) -> None:
        before = self._harness(extra_evalscope=False)
        after = self._harness(extra_evalscope=True)
        for principal in ("alice", "bob"):
            for query in ("", "include_unowned=true"):
                with self.subTest(principal=principal, query=query):
                    _s1, _h1, a = before.request(
                        "GET", "/api/evaluation/reports", as_principal=principal, query=query
                    )
                    _s2, _h2, b = after.request(
                        "GET", "/api/evaluation/reports", as_principal=principal, query=query
                    )
                    self.assertEqual(a, b)

    def test_report_sources_response_is_byte_identical(self) -> None:
        # 只对**普通成员**断言不变性。全局读者（admin:read）本来就被授权看见别人的报告，
        # 因此他的可见集合"应该"随新报告变化——那不是泄漏，那是权限。
        before = self._harness(extra_evalscope=False)
        after = self._harness(extra_evalscope=True)
        for principal in ("alice", "bob"):
            with self.subTest(principal=principal):
                _s1, _h1, a = before.request(
                    "GET", "/api/evaluation/report-sources", as_principal=principal
                )
                _s2, _h2, b = after.request(
                    "GET", "/api/evaluation/report-sources", as_principal=principal
                )
                self.assertEqual(a, b)

    def test_a_global_reader_does_see_the_new_report(self) -> None:
        before = self._harness(extra_evalscope=False)
        after = self._harness(extra_evalscope=True)
        _s, _h, a = before.request(
            "GET", "/api/evaluation/reports", as_principal="carol"
        )
        _s, _h, b = after.request(
            "GET", "/api/evaluation/reports", as_principal="carol"
        )
        self.assertEqual(len(a["data"]["reports"]), 1)
        self.assertEqual(len(b["data"]["reports"]), 2)

    def test_a_principal_with_no_reports_cannot_tell_the_source_has_any(self) -> None:
        """bob 一条报告都没有：加不加那份别人的报告，他的来源状态完全一样。"""
        before = self._harness(extra_evalscope=False)
        after = self._harness(extra_evalscope=True)
        _s, _h, body = after.request(
            "GET", "/api/evaluation/report-sources", as_principal="bob"
        )
        row = {r["source"]: r for r in body["data"]["sources"]}[SOURCE_EVALSCOPE]
        self.assertEqual(row["state"], SOURCE_EMPTY)
        self.assertIsNone(row["observed_at"])
        self.assertEqual(row["visible_count"], 0)
        _s, _h, body_before = before.request(
            "GET", "/api/evaluation/report-sources", as_principal="bob"
        )
        self.assertEqual(body, body_before)


class ResponseHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = populated_harness()

    def tearDown(self) -> None:
        close_all()

    def test_no_report_response_carries_a_document_path(self) -> None:
        for principal in ("alice", "dave", "carol"):
            with self.subTest(principal=principal):
                for path in (
                    "/api/evaluation/reports",
                    "/api/evaluation/reports",
                    "/api/evaluation/report-sources",
                ):
                    _s, _h, body = self.harness.request("GET", path, as_principal=principal)
                    assert_no_paths(self, body)

    def test_the_reference_is_opaque_not_the_relative_path(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="alice"
        )
        row = body["data"]["reports"][0]
        self.assertTrue(row["report_ref"].startswith("evrp-"))
        self.assertNotIn(REPORT_REF_EVALSCOPE, repr(body))

    def test_a_markup_title_is_neutralized_in_the_response(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="alice"
        )
        title = body["data"]["reports"][0]["title"]
        self.assertNotIn("<", title)
        self.assertNotIn(">", title)

    def test_missing_dimensions_are_null_not_zero(self) -> None:
        _s, _h, body = self.harness.request(
            "GET", "/api/evaluation/reports", as_principal="alice"
        )
        row = body["data"]["reports"][0]
        absent = row["dimensions"]["throughput_rps"]
        self.assertEqual(absent["state"], "unknown")
        self.assertIsNone(absent["value"])
        names = {item["name"] for item in row["missing_dimensions"]}
        self.assertIn("throughput_rps", names)


class LoopbackHttpTests(unittest.TestCase):
    """一次**真实 socket** 的契约检查：状态码、header 与不可缓存。"""

    def setUp(self) -> None:
        self.harness = populated_harness()

    def tearDown(self) -> None:
        close_all()

    def test_reports_over_tcp(self) -> None:
        with serve(self.harness) as (host, port):
            status, headers, body = http_request(
                host,
                port,
                "GET",
                "/api/evaluation/reports",
                token=self.harness.token_for("alice"),
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(body["schema_version"], "evaluation/1")
        self.assertIn("reports", body["data"])

    def test_an_anonymous_tcp_request_is_401(self) -> None:
        with serve(self.harness) as (host, port):
            status, _headers, _body = http_request(host, port, "GET", "/api/evaluation/reports")
        self.assertEqual(status, 401)

    def test_the_report_endpoints_reject_writes(self) -> None:
        with serve(self.harness) as (host, port):
            status, headers, _body = http_request(
                host,
                port,
                "POST",
                "/api/evaluation/reports",
                token=self.harness.token_for("alice"),
                payload={},
            )
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
