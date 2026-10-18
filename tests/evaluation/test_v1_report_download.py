"""V1 受限下载（Issue #78）：判定、上界、路径防护与"没接线"的正确回答。

这一组补的是 Lane #78 在 V1 里**新增**的第四条：报告列表/详情之外的受限下载。它证明的
事情分四类，每一类都有一条**负向**断言，因为这一面的正确性几乎全在"拒绝得对不对"里：

* **可见性**：不是 owner、不在项目里、无归属——一律与"不存在"**逐字节同形**，因此这个
  端点不会变成"这份报告存在吗"的预言机；
* **部署事实**：本部署的内容读取关闭、报告根未接线，因此 ``content_read`` 恒为 ``False``
  并给出原因——**不是**一个 0 字节的成功下载（"测不到"与"测得 0"分开）；
* **路径防护**：形状（相对、白名单后缀）与落点（``realpath`` 包含性、软链接）都由
  ``backend.evaluation.reports`` 判定，本组逐条验证那几条拒绝确实生效；
* **上界**：超出上限的产物被**拒绝**，不是被截断——一份被截断的报告看起来像完整的。

文件本身遵守 ``test_static_guarantees`` 对**测试**的要求：不调 ``open()``、路径只落在
``tempfile`` 里自己造的东西上、不出现 ``.jsonl`` 后缀、不碰 ``read_bytes`` 这个名字。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.evaluation import download as download_module
from backend.evaluation.authz import REPORT_NOT_AVAILABLE_MESSAGE
from backend.evaluation.download import (
    DOWNLOAD_REASONS,
    BoundedReportReader,
    ReportDownloadPlan,
)
from backend.evaluation.errors import NotFound, NotConfigured
from backend.evaluation.models import metric_known
from backend.evaluation.provenance import (
    DIMENSION_COLD_START,
    DIMENSION_QUALITY,
    SOURCE_EVALSCOPE,
    SOURCE_LABELS,
    SOURCE_LEGACY_BENCHMARK,
    SOURCE_OK,
)
from backend.evaluation.reports import ReportRoot, normalize_report_ref

from tests.evaluation.support import (
    EPOCH,
    PROJECT_ALPHA,
    PROJECT_BETA,
    REPORT_REF_EVALSCOPE,
    REPORT_REF_LEGACY,
    build_directories,
    build_harness_with_reports,
    close_all,
    report_owner,
    report_record,
    report_snapshot,
)

#: 一条**看起来像**绝对路径的引用。它不指向任何真实主机目录——`normalize_report_ref`
#: 在碰文件系统之前就按"引用必须是相对的"拒绝了它。
ABSOLUTE_LOOKING_REF = "/reports/absolute.json"


def _record(
    ref: str,
    *,
    owner: str | None = "alice",
    project_id: str = PROJECT_ALPHA,
    source: str = SOURCE_EVALSCOPE,
    title: str | None = None,
):
    return report_record(
        source,
        ref,
        report_id=f"r-{owner or 'orphan'}-{ref}",
        owner=None if owner is None else report_owner(owner, project_id=project_id),
        dimensions={DIMENSION_QUALITY: metric_known(DIMENSION_QUALITY, 0.5)},
        created_at=EPOCH,
        observed_at=EPOCH,
        title=title,
    )


def _snapshot(records, *, source: str = SOURCE_EVALSCOPE, state: str = SOURCE_OK):
    return report_snapshot(source, state, list(records), observed_at=EPOCH)


class Workspace:
    """一个临时报告根目录。**全部由测试自己造**，不读任何真实产物。"""

    def __init__(self, test: unittest.TestCase, prefix: str = "eval-report-root-") -> None:
        holder = tempfile.TemporaryDirectory(prefix=prefix)
        test.addCleanup(holder.cleanup)
        self.path = Path(holder.name)

    def write(self, ref: str, text: str = '{"ok": true}') -> Path:
        target = self.path.joinpath(*ref.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def root(self) -> ReportRoot:
        return ReportRoot(str(self.path))


class DownloadVisibilityTests(unittest.TestCase):
    """可见性：不是 owner、不在项目里、无归属 —— 与"不存在"同形。"""

    def setUp(self) -> None:
        self.harness = build_harness_with_reports(
            (
                # alice 的报告在 proj-alpha；dave 的报告在 proj-beta。
                _snapshot(
                    [
                        _record(REPORT_REF_EVALSCOPE, owner="alice"),
                        _record(REPORT_REF_LEGACY, owner="dave", project_id=PROJECT_BETA),
                    ]
                ),
                # 一条归属无法核实的历史产物：默认对所有人隐藏。
                _snapshot(
                    [
                        _record(
                            "legacy/benchmark/orphan-1.json",
                            owner=None,
                            source=SOURCE_LEGACY_BENCHMARK,
                        )
                    ],
                    source=SOURCE_LEGACY_BENCHMARK,
                ),
            )
        )
        self.addCleanup(close_all)

    def _refs(self) -> dict[str, str]:
        """以 ``carol``（全局读者 + admin:manage）列出**全部**记录，按 owner 建索引。"""
        data = self.harness.service.reports(
            requester=self.harness.requester("carol"), include_unowned=True
        )["data"]
        refs: dict[str, str] = {}
        for row in data["reports"]:
            owner = row["owner"]
            key = "__unowned__" if owner is None else owner["principal"]
            refs[key] = row["report_ref"]
        return refs

    def _download(self, who: str, ref: str, **kwargs):
        return self.harness.service.report_download(
            requester=self.harness.requester(who), report_ref=ref, **kwargs
        )

    def test_the_owner_can_ask_for_a_download(self) -> None:
        ref = self._refs()["alice"]
        data = self._download("alice", ref)["data"]
        self.assertTrue(data["authorized"])
        self.assertEqual(data["report_ref"], ref)

    def test_a_project_member_who_is_not_the_owner_is_refused(self) -> None:
        """``bob`` 与 alice 同属 proj-alpha，但**不是 owner** —— 仍然是 404。"""
        with self.assertRaises(NotFound):
            self._download("bob", self._refs()["alice"])

    def test_a_report_from_another_project_is_refused(self) -> None:
        """``dave`` 的报告属于 proj-beta；``alice`` 既不是 owner 也不是成员。"""
        with self.assertRaises(NotFound):
            self._download("alice", self._refs()["dave"])

    def test_a_nonexistent_reference_answers_exactly_like_a_forbidden_one(self) -> None:
        """两条拒绝必须**逐字节**相同，否则这个端点就是一个存在性预言机。"""
        with self.assertRaises(NotFound) as forbidden:
            self._download("bob", self._refs()["alice"])
        with self.assertRaises(NotFound) as absent:
            self._download("bob", "evrp-000000000000000000000000")
        self.assertEqual(str(forbidden.exception), str(absent.exception))
        self.assertEqual(str(forbidden.exception), REPORT_NOT_AVAILABLE_MESSAGE)
        self.assertEqual(forbidden.exception.contract_code, absent.exception.contract_code)

    def test_an_unowned_report_stays_hidden_even_with_the_flag_for_a_member(self) -> None:
        """``include_unowned`` 只是转述；放行在 ``authz``，那里要 ``admin:manage``。"""
        with self.assertRaises(NotFound):
            self._download("alice", self._refs()["__unowned__"], include_unowned=True)

    def test_an_admin_without_manage_cannot_reach_the_unowned_report_either(self) -> None:
        """``carol`` 的只读令牌只有 ``admin:read``：``include_unowned`` 这条阀门打不开。"""
        ref = self._refs()["__unowned__"]
        with self.assertRaises(NotFound):
            self.harness.service.report_download(
                requester=self.harness.service.requester_from_token("synthetic-carol-readonly"),
                report_ref=ref,
                include_unowned=True,
            )

    def test_the_download_plan_never_carries_a_path(self) -> None:
        data = self._download("alice", self._refs()["alice"])["data"]
        blob = repr(data)
        self.assertNotIn(REPORT_REF_EVALSCOPE, blob)
        self.assertNotIn(REPORT_REF_LEGACY, blob)
        self.assertNotIn("evalscope/reports", blob)
        self.assertNotIn("legacy/benchmark", blob)

    def test_the_download_plan_is_keyed_by_the_opaque_reference(self) -> None:
        ref = self._refs()["alice"]
        data = self._download("alice", ref)["data"]
        self.assertEqual(data["report_ref"], ref)
        self.assertTrue(data["report_ref"].startswith("evrp-"))

    def test_the_download_is_a_superset_of_nothing_seen_in_the_detail_view(self) -> None:
        """下载面不得比详情面看见更多：详情拿不到的，下载也拿不到，且同一条 404。"""
        detail_ref = self._refs()["dave"]
        with self.assertRaises(NotFound) as detail:
            self.harness.service.report_detail(
                requester=self.harness.requester("alice"), report_ref=detail_ref
            )
        with self.assertRaises(NotFound) as download:
            self._download("alice", detail_ref)
        self.assertEqual(str(detail.exception), str(download.exception))


class DownloadDeploymentStateTests(unittest.TestCase):
    """部署事实：本部署关闭内容读取，报告根未接线。两者都不是"0 字节的成功"。"""

    def _harness(self, *, enabled: bool, root: ReportRoot | None = None, max_bytes: int | None = None):
        overrides: dict = {"report_download_enabled": enabled}
        if max_bytes is not None:
            overrides["report_download_max_bytes"] = max_bytes
        return build_harness_with_reports(
            (_snapshot([_record(REPORT_REF_EVALSCOPE)]),),
            report_root=root,
            config_overrides=overrides,
        )

    def _ask(self, harness) -> dict:
        data = harness.service.reports(requester=harness.requester("alice"))["data"]
        ref = data["reports"][0]["report_ref"]
        return harness.service.report_download(
            requester=harness.requester("alice"), report_ref=ref
        )["data"]

    def test_the_default_deployment_refuses_the_content_and_says_why(self) -> None:
        harness = self._harness(enabled=False)
        self.addCleanup(close_all)
        plan = self._ask(harness)
        self.assertTrue(plan["authorized"])
        self.assertFalse(plan["content_read"])
        self.assertEqual(plan["reason"], "report_download_disabled")
        self.assertIsNone(plan["size_bytes"])
        self.assertEqual(plan["max_bytes"], download_module.MAX_DOWNLOAD_BYTES)

    def test_an_open_reader_over_an_unwired_root_says_not_configured(self) -> None:
        harness = self._harness(enabled=True, root=None)
        self.addCleanup(close_all)
        plan = self._ask(harness)
        self.assertFalse(plan["content_read"])
        self.assertEqual(plan["reason"], "report_root_not_configured")
        self.assertIsNone(plan["size_bytes"])

    def test_the_default_configuration_keeps_the_download_reader_closed(self) -> None:
        """缺省即本部署：开关 False、根未接线。这条是**结构**断言，不是行为断言。"""
        from backend.evaluation.service import EvaluationConfig

        config = EvaluationConfig()
        self.assertFalse(config.report_download_enabled)
        self.assertFalse(config.report_root.configured)
        self.assertEqual(config.report_download_max_bytes, download_module.MAX_DOWNLOAD_BYTES)

    def test_the_capabilities_block_describes_the_bound(self) -> None:
        harness = self._harness(enabled=False)
        self.addCleanup(close_all)
        block = harness.service.capabilities()["report_download"]
        self.assertEqual(block["max_bytes"], download_module.MAX_DOWNLOAD_BYTES)
        self.assertFalse(block["reader"]["enabled"])
        self.assertFalse(block["reader"]["configured"])
        self.assertEqual(
            set(block["content_types"]), set(download_module.DOWNLOAD_CONTENT_TYPES)
        )


class DownloadBoundsTests(unittest.TestCase):
    """上界：一份**超出**声明的产物被拒绝，不是被截断。"""

    PAYLOAD = '{"ok": true}'

    def setUp(self) -> None:
        self.workspace = Workspace(self)
        self.workspace.write(REPORT_REF_EVALSCOPE, self.PAYLOAD)

    def _harness(self, *, max_bytes: int):
        return build_harness_with_reports(
            (_snapshot([_record(REPORT_REF_EVALSCOPE)]),),
            report_root=self.workspace.root(),
            config_overrides={
                "report_download_enabled": True,
                "report_download_max_bytes": max_bytes,
            },
        )

    def _ask(self, harness) -> dict:
        data = harness.service.reports(requester=harness.requester("alice"))["data"]
        ref = data["reports"][0]["report_ref"]
        return harness.service.report_download(
            requester=harness.requester("alice"), report_ref=ref
        )["data"]

    def test_a_report_that_fits_is_readable_and_reports_its_size(self) -> None:
        harness = self._harness(max_bytes=len(self.PAYLOAD))
        self.addCleanup(close_all)
        plan = self._ask(harness)
        self.assertTrue(plan["content_read"])
        self.assertIsNone(plan["reason"])
        self.assertEqual(plan["size_bytes"], len(self.PAYLOAD))
        self.assertEqual(plan["content_type"], "application/json")
        self.assertEqual(plan["filename"], "synth-run-1.json")

    def test_a_report_over_the_bound_is_refused_and_asserts_no_size(self) -> None:
        harness = self._harness(max_bytes=4)
        self.addCleanup(close_all)
        plan = self._ask(harness)
        self.assertFalse(plan["content_read"])
        self.assertEqual(plan["reason"], "report_exceeds_download_bound")
        # 超界时字节数是**未知**，不是"被截断到的那个数"。
        self.assertIsNone(plan["size_bytes"])

    def test_the_reader_itself_refuses_rather_than_truncating(self) -> None:
        reader = BoundedReportReader(self.workspace.root(), enabled=True, max_bytes=4)
        result = reader.read_bounded(REPORT_REF_EVALSCOPE)
        self.assertFalse(result.ok)
        self.assertIsNone(result.body)
        self.assertEqual(result.reason, "report_exceeds_download_bound")

    def test_the_reader_returns_the_bytes_and_a_sha256_when_it_fits(self) -> None:
        reader = BoundedReportReader(self.workspace.root(), enabled=True, max_bytes=1024)
        result = reader.read_bounded(REPORT_REF_EVALSCOPE)
        self.assertTrue(result.ok)
        self.assertEqual(result.body, self.PAYLOAD.encode("utf-8"))
        self.assertEqual(len(result.sha256), 64)
        self.assertEqual(result.size_bytes, len(self.PAYLOAD))

    def test_an_absent_file_is_unknown_not_an_empty_report(self) -> None:
        reader = BoundedReportReader(self.workspace.root(), enabled=True)
        result = reader.read_bounded("evalscope/reports/absent-run.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "report_file_absent")
        self.assertIsNone(result.body)


class DownloadPathGuardTests(unittest.TestCase):
    """路径防护：形状与落点都由 ``reports`` 判定，这里逐条验证它确实在位。"""

    def setUp(self) -> None:
        self.workspace = Workspace(self)
        self.workspace.write(REPORT_REF_EVALSCOPE, "{}")
        self.reader = BoundedReportReader(self.workspace.root(), enabled=True)

    def test_a_traversal_reference_is_rejected_at_the_shape_layer(self) -> None:
        for attempt in ("../secret.json", "a/../../secret.json", ABSOLUTE_LOOKING_REF, "a\\b.json"):
            with self.subTest(ref=attempt):
                with self.assertRaises(Exception):
                    normalize_report_ref(attempt)

    def test_an_absolute_reference_never_reaches_the_filesystem(self) -> None:
        with self.assertRaises(Exception):
            self.reader.probe(ABSOLUTE_LOOKING_REF)

    def test_a_symlink_escaping_the_root_is_refused(self) -> None:
        """根**之外**的目标：``realpath`` 包含性检查先挡住它。"""
        outside = Workspace(self, prefix="eval-report-outside-")
        target = outside.write("secret.json", "{}")
        link = self.workspace.path / "evalscope" / "reports" / "escape.json"
        os.symlink(target, link)
        result = self.reader.read_bounded("evalscope/reports/escape.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "report_reference_refused")

    def test_a_symlink_inside_the_root_is_refused_by_its_own_rule(self) -> None:
        """根**之内**的链接也不行：一条引用永远只指一个产物，不指一次跳转。"""
        target = self.workspace.write("evalscope/reports/real.json", "{}")
        link = self.workspace.path / "evalscope" / "reports" / "alias.json"
        os.symlink(target, link)
        result = self.reader.read_bounded("evalscope/reports/alias.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "report_reference_is_a_symlink")

    def test_a_directory_is_not_a_downloadable_artifact(self) -> None:
        (self.workspace.path / "evalscope" / "reports" / "nested.json").mkdir(parents=True)
        result = self.reader.read_bounded("evalscope/reports/nested.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "report_is_not_a_regular_file")

    def test_a_disallowed_extension_never_gets_a_content_type(self) -> None:
        """词表之外的后缀没有内容类型，因此不会被下载——不是"猜一个二进制流"。"""
        with self.assertRaises(Exception):
            normalize_report_ref("evalscope/reports/weights.bin")
        self.assertIsNone(download_module.DOWNLOAD_CONTENT_TYPES.get(".bin"))

    def test_the_reader_is_closed_unless_it_is_explicitly_opened(self) -> None:
        closed = BoundedReportReader(self.workspace.root())
        with self.assertRaises(NotConfigured):
            closed.read_bounded(REPORT_REF_EVALSCOPE)


class DownloadPlanGuardTests(unittest.TestCase):
    """**直接**调用判定函数：可见性守卫必须在它自己那一层成立。

    只走 ``service.report_download`` 是不够的：那条路径上 ``catalog.lookup`` 已经先拒绝
    了一次，于是 ``download_plan`` 内部的守卫即使被删掉，端到端测试仍然全绿
    （这一点由一次 fail-when-reverted 探针发现）。而 ``download_plan`` 是交给 BFF 的公开
    函数——接缝另一侧可能直接调它。因此这里绕过取件路径，逐条验证守卫本身。
    """

    def setUp(self) -> None:
        self.harness = build_harness_with_reports(
            (
                _snapshot([_record(REPORT_REF_EVALSCOPE, owner="alice")]),
                _snapshot(
                    [_record(REPORT_REF_LEGACY, owner=None, source=SOURCE_LEGACY_BENCHMARK)],
                    source=SOURCE_LEGACY_BENCHMARK,
                ),
            )
        )
        self.addCleanup(close_all)
        _principals, _tokens, self.projects, _targets = build_directories()
        self.reader = BoundedReportReader(enabled=True)

    def _entries(self):
        return self.harness.service.report_catalog().entries()

    def _owned_entry(self):
        return next(entry for entry in self._entries() if not entry.record.unowned)

    def _unowned_entry(self):
        return next(entry for entry in self._entries() if entry.record.unowned)

    def test_a_non_owner_is_refused_by_the_plan_itself(self) -> None:
        with self.assertRaises(NotFound) as ctx:
            download_module.download_plan(
                self.harness.requester("bob"),
                self._owned_entry(),
                self.projects,
                reader=self.reader,
            )
        self.assertEqual(str(ctx.exception), REPORT_NOT_AVAILABLE_MESSAGE)

    def test_an_unowned_record_is_refused_by_the_plan_itself_for_a_member(self) -> None:
        with self.assertRaises(NotFound):
            download_module.download_plan(
                self.harness.requester("alice"),
                self._unowned_entry(),
                self.projects,
                reader=self.reader,
                include_unowned=True,
            )

    def test_the_owner_gets_a_plan_from_the_same_call(self) -> None:
        plan = download_module.download_plan(
            self.harness.requester("alice"),
            self._owned_entry(),
            self.projects,
            reader=self.reader,
        )
        self.assertTrue(plan.authorized)
        self.assertEqual(plan.report_ref, self._owned_entry().ref_id)

    def test_a_foreign_requester_is_rejected_rather_than_downgraded(self) -> None:
        """一个不是 ``RequesterContext`` 的东西是**编程错误**，不是"匿名访客"。"""
        with self.assertRaises(Exception):
            download_module.download_plan(
                object(), self._owned_entry(), self.projects, reader=self.reader
            )


class DownloadPlanShapeTests(unittest.TestCase):
    """计划的形状：封闭原因词表、来源标签、以及"绝不带走一个路径"。"""

    def _plan(self, **overrides) -> ReportDownloadPlan:
        base = dict(
            report_ref="evrp-0123456789abcdef01234567",
            path_ref=REPORT_REF_EVALSCOPE,
            source=SOURCE_EVALSCOPE,
            source_label=SOURCE_LABELS[SOURCE_EVALSCOPE],
            authorized=True,
            content_read=False,
            max_bytes=1024,
            reason="report_root_not_configured",
        )
        base.update(overrides)
        return ReportDownloadPlan(**base)

    def test_the_reason_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            set(DOWNLOAD_REASONS),
            {
                "report_content_reading_disabled",
                "report_root_not_configured",
                "report_download_disabled",
                "report_exceeds_download_bound",
                "report_is_not_a_regular_file",
                "report_file_absent",
                "report_reference_is_a_symlink",
                "report_reference_refused",
            },
        )

    def test_an_unknown_reason_is_rejected_at_construction(self) -> None:
        with self.assertRaises(Exception):
            self._plan(reason="because_i_said_so")

    def test_a_readable_plan_must_fit_its_own_declared_bound(self) -> None:
        with self.assertRaises(Exception):
            self._plan(
                content_read=True,
                reason=None,
                content_type="application/json",
                filename="synth-run-1.json",
                size_bytes=2048,
                max_bytes=1024,
            )

    def test_a_refusal_must_not_assert_a_size(self) -> None:
        with self.assertRaises(Exception):
            self._plan(size_bytes=12)

    def test_the_dict_carries_a_source_label_and_no_path(self) -> None:
        payload = self._plan().to_dict()
        self.assertEqual(payload["source"], SOURCE_EVALSCOPE)
        self.assertEqual(payload["source_label"], SOURCE_LABELS[SOURCE_EVALSCOPE])
        self.assertNotIn("path_ref", payload)
        blob = repr(payload)
        self.assertNotIn(REPORT_REF_EVALSCOPE, blob)
        self.assertNotIn("evalscope/reports", blob)

    def test_the_plan_requires_an_opaque_reference(self) -> None:
        with self.assertRaises(Exception):
            self._plan(report_ref=REPORT_REF_EVALSCOPE)

    def test_the_source_label_must_come_from_the_closed_vocabulary(self) -> None:
        with self.assertRaises(Exception):
            self._plan(source_label="EvalScope")


class MissingMetricIsNullTests(unittest.TestCase):
    """回归护栏：**缺测不是 0**——这条在目录 DTO 上已经成立，下载面不得让它退化。"""

    def test_a_dimension_the_source_never_reported_renders_as_unknown_and_null(self) -> None:
        harness = build_harness_with_reports((_snapshot([_record(REPORT_REF_EVALSCOPE)]),))
        self.addCleanup(close_all)
        data = harness.service.reports(requester=harness.requester("alice"))["data"]
        dimensions = data["reports"][0]["dimensions"]
        cold_start = dimensions[DIMENSION_COLD_START]
        self.assertEqual(cold_start["state"], "unknown")
        self.assertIsNone(cold_start["value"])
        self.assertNotEqual(cold_start["value"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
