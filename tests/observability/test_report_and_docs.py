"""把「报告说自己没问题」与「文档说契约是什么」都变成会红的回归。

报告是一份**可执行**的证据：本文件跑它、断言它全过。文档则必须与代码里的常量一致 ——
契约文档漂移过一次，四个消费方就会各按各的理解接线。
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dev.observability_report import build_all, main, self_check  # noqa: E402

from backend.observability import (  # noqa: E402
    ALL_KINDS,
    DRIVER_STATES,
    KIND_USAGE_SELF,
    MAX_LIMIT,
    MAX_POINTS,
    MAX_WINDOW_HOURS,
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    REASON_AMBIGUOUS_SERIES,
    REASON_CLOCK_SKEW,
    REASON_NOT_AUTHORIZED,
    REASON_NOT_COLLECTED,
    REASON_NO_SAMPLE,
    REASON_SOURCE_UNAVAILABLE,
    REASON_UNSUPPORTED,
    SCHEMA_VERSION,
    SCOPE_ADMIN_READ,
    SCOPE_HOST_READ,
    SCOPE_SELF_READ,
    SELF_REPORTED_IDENTITY_FIELDS,
    SUPPORTED_BUCKETS,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONTRACT_DOC = REPO_ROOT / "docs" / "observability-contract.md"


def _doc_text() -> str:
    return CONTRACT_DOC.read_text(encoding="utf-8")


class SelfCheckReportTest(unittest.TestCase):
    def test_every_invariant_in_the_report_passes(self) -> None:
        failures = [item for item in self_check(build_all()) if not item["ok"]]
        self.assertEqual(failures, [], failures)

    def test_the_report_covers_every_dto_kind(self) -> None:
        dtos = build_all()
        self.assertEqual(set(dtos), set(ALL_KINDS))

    def test_the_report_exits_zero_when_everything_passes(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--quiet"])
        self.assertEqual(code, 0, buffer.getvalue())

    def test_the_report_json_mode_is_machine_readable(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--json"])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(set(payload["dtos"]), set(ALL_KINDS))

    def test_the_report_can_emit_a_single_kind(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main(["--kind", KIND_USAGE_SELF])
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["kind"], KIND_USAGE_SELF)

    def test_the_report_declares_itself_synthetic(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main([])
        self.assertIn("SYNTHETIC", buffer.getvalue())


class ContractDocumentTest(unittest.TestCase):
    """文档必须把代码里的常量**逐字**写清楚。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _doc_text()

    def test_the_doc_exists_and_is_substantial(self) -> None:
        self.assertTrue(CONTRACT_DOC.is_file())
        self.assertGreater(len(self.text), 8000)

    def test_the_doc_states_the_schema_version(self) -> None:
        self.assertIn(SCHEMA_VERSION, self.text)

    def test_the_doc_lists_every_scope(self) -> None:
        for scope in (SCOPE_SELF_READ, SCOPE_HOST_READ, SCOPE_ADMIN_READ):
            with self.subTest(scope=scope):
                self.assertIn(scope, self.text)

    def test_the_doc_states_that_roles_grant_nothing(self) -> None:
        self.assertIn("`roles` 不产生任何权限", self.text)
        self.assertIn("role == admin", self.text)

    def test_the_doc_states_the_unattributable_counter_contract(self) -> None:
        self.assertIn("不可归属", self.text)
        self.assertIn("unknown_key", self.text)
        self.assertIn("malformed_lines", self.text)
        self.assertIn("content_field_rejected", self.text)
        self.assertIn("无从测得", self.text)

    def test_the_doc_states_the_reason_codes(self) -> None:
        for reason in (
            REASON_NO_SAMPLE,
            REASON_SOURCE_UNAVAILABLE,
            REASON_NOT_COLLECTED,
            REASON_NOT_AUTHORIZED,
            REASON_UNSUPPORTED,
            REASON_CLOCK_SKEW,
            REASON_AMBIGUOUS_SERIES,
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, self.text)

    def test_the_doc_states_the_quality_vocabulary(self) -> None:
        for quality in (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN, "stale"):
            with self.subTest(quality=quality):
                self.assertIn(quality, self.text)

    def test_the_doc_states_every_dto_kind(self) -> None:
        for kind in ALL_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, self.text)

    def test_the_doc_states_the_window_bounds(self) -> None:
        for bucket in SUPPORTED_BUCKETS:
            with self.subTest(bucket=bucket):
                self.assertIn(bucket, self.text)
        for hours in MAX_WINDOW_HOURS.values():
            with self.subTest(hours=hours):
                self.assertIn(str(hours), self.text)
        self.assertIn(str(MAX_POINTS), self.text)
        self.assertIn(str(MAX_LIMIT), self.text)

    def test_the_doc_states_the_driver_state_vocabulary(self) -> None:
        for state in DRIVER_STATES:
            with self.subTest(state=state):
                self.assertIn(state, self.text)

    def test_the_doc_states_the_self_reported_identity_policy(self) -> None:
        self.assertIn("自报身份", self.text)
        self.assertIn("x_principal", self.text)
        self.assertIn("X-Forwarded-User", self.text)
        self.assertIn("SELF_REPORTED_IDENTITY_FIELDS", self.text)

    def test_the_doc_states_that_promql_is_unreachable_from_the_web_tier(self) -> None:
        self.assertIn("Web 层提供不了 PromQL", self.text)
        self.assertIn("FIXED_EXPRESSIONS", self.text)

    def test_the_doc_states_the_url_policy(self) -> None:
        self.assertIn("环回", self.text)
        self.assertIn("validate_base_url", self.text)
        self.assertIn("ConfigRejected", self.text)

    def test_the_doc_states_that_loopback_detection_never_resolves_dns(self) -> None:
        self.assertIn("不做任何名字解析", self.text)
        self.assertIn("SSRF", self.text)
        self.assertIn("LOOPBACK_LITERAL_HOSTS", self.text)

    def test_the_doc_states_the_billing_basis(self) -> None:
        self.assertIn("verified-key", self.text)
        self.assertIn("billing_basis", self.text)

    def test_the_doc_states_that_per_user_gpu_memory_is_unrepresentable(self) -> None:
        self.assertIn("按用户切分只能是编造", self.text)
        self.assertIn("结构性保证", self.text)

    def test_the_doc_states_the_iframe_policy(self) -> None:
        self.assertIn("不直接开放任意 iframe", self.text)
        self.assertIn("iframe_allowed", self.text)
        self.assertIn("external_url", self.text)
        self.assertIn("same-origin", self.text)

    def test_the_doc_states_the_host_collector_is_off_by_default(self) -> None:
        self.assertIn("enabled=False", self.text)
        self.assertIn("生产不启用", self.text)

    def test_the_doc_states_that_the_module_does_not_import_pynvml(self) -> None:
        self.assertIn("pynvml", self.text)
        self.assertIn("注入", self.text)

    def test_the_doc_points_at_the_canaries_and_says_what_they_mean(self) -> None:
        self.assertIn("test_canaries.py", self.text)
        self.assertIn("金丝雀", self.text)
        self.assertIn("不得声称 READY", self.text)

    def test_the_doc_lists_the_deliberate_gaps(self) -> None:
        self.assertIn("已知缺口", self.text)
        self.assertIn("故意不做", self.text)

    def test_the_doc_says_unknown_never_becomes_zero(self) -> None:
        self.assertIn("绝不以 `0` 代替", self.text)
        self.assertIn("UNKNOWN", self.text)

    def test_the_doc_runs_the_tests_with_gpu_disabled(self) -> None:
        self.assertIn("CUDA_VISIBLE_DEVICES=''", self.text)

    def test_the_doc_names_the_non_goals(self) -> None:
        """原文捕获与访问不在本任务范围 —— 文档必须这么写，免得后面有人往里加。"""
        self.assertIn("不读取任何真实日志", self.text)


class DocQuotesTheCodeTest(unittest.TestCase):
    """文档里引用的常量不能凭空出现 —— 拿代码验一遍。"""

    def test_self_reported_field_list_is_quoted_consistently(self) -> None:
        """集合内部按归一化形式存，文档里可以写成原始大小写 —— 两边都要能对上。"""
        from backend.observability import normalise_field_name

        text = _doc_text()
        for field in ("x_project", "x_principal", "X-Forwarded-User", "principal", "uid"):
            with self.subTest(field=field):
                self.assertIn(normalise_field_name(field), SELF_REPORTED_IDENTITY_FIELDS)
                self.assertIn(field, text)

    def test_bucket_limits_in_the_doc_match_the_code(self) -> None:
        text = _doc_text()
        self.assertIn("| `5m` | 24 小时 | 24 | 288 |", text)
        self.assertIn("| `1h` | 7 天 | 168 | 168 |", text)
        self.assertIn("| `1d` | 32 天 | 768 | 32 |", text)
        self.assertEqual(MAX_WINDOW_HOURS["5m"], 24)
        self.assertEqual(MAX_WINDOW_HOURS["1h"], 24 * 7)
        self.assertEqual(MAX_WINDOW_HOURS["1d"], 24 * 32)


if __name__ == "__main__":
    unittest.main()
