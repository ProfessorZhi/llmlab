"""聚合契约：跨项目泄漏 0、label 冒领 0、整体 p95 不能由各组 p95 推出。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import (  # noqa: E402
    SYNTHETIC,
    atlas_request,
    load_fixture,
    requester,
    trusted_directory,
)

from backend.atlas import (  # noqa: E402
    SCOPE_USAGE_READ,
    SCOPE_USAGE_UNATTRIBUTED,
    build_atlas_dto,
)

#: 这些字符串只属于 proj-b / 未归属行；proj-a 视图里出现任何一个都算泄漏。
FORBIDDEN_IN_PROJ_A_VIEW = ("proj-b", "eval-suite", "key-gamma", "u-2001", "llm-c")


class AggregationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = load_fixture("gateway_sample.jsonl")
        cls.directory = trusted_directory()
        cls.dto = build_atlas_dto(
            atlas_request(requester()), cls.text, cls.directory, SYNTHETIC
        )

    def _workloads(self, group_id: str = "proj-a") -> dict[str, dict]:
        groups = [g for g in self.dto["groups"] if g["id"] == group_id]
        self.assertEqual(len(groups), 1, f"expected exactly one group {group_id!r}")
        return {w["id"]: w for w in groups[0]["workloads"]}

    # ---- 泄漏 -----------------------------------------------------------

    def test_cross_project_leak_is_zero(self) -> None:
        blob = json.dumps(self.dto, sort_keys=True)
        for token in FORBIDDEN_IN_PROJ_A_VIEW:
            with self.subTest(token=token):
                self.assertNotIn(token, blob)

    def test_only_the_authorized_project_has_a_group(self) -> None:
        self.assertEqual([g["id"] for g in self.dto["groups"]], ["proj-a"])

    def test_unattributed_project_calls_are_hidden_without_scope(self) -> None:
        self.assertEqual(self.dto["unattributed"]["project_calls"], None)
        # P0 收口（fix/atlas-usage-privacy-boundaries-20261014）：
        # `unknown_key` 数的是「key 不在受信目录」的行 —— 结构上无法归属到任何项目，
        # 因此它对普通 viewer 是 null（无从测得），不是 1。旧断言 `== 1` 记录的是
        # 一个可被前后 DTO 差值读出的全局侧信道。
        self.assertIsNone(self.dto["unknown"]["unknown_key"])
        self.assertIsNot(self.dto["unknown"]["unknown_key"], 0)
        # `revoked_key` 相反：目录里有绑定，能归属到本次授权的 proj-a，所以是**测得**的 1。
        self.assertEqual(self.dto["unknown"]["revoked_key"], 1)

    def test_forged_x_project_header_cannot_create_a_foreign_group(self) -> None:
        """fixture 第 8 行带 x_project=proj-b / X-Forwarded-User —— 必须无效。"""
        self.assertNotIn("proj-b", json.dumps(self.dto, sort_keys=True))
        # 这一行用的是未注册 key，所以它既没进 proj-b，也没进 proj-a 的统计。
        self.assertEqual(self.dto["totals"]["calls"], 7)
        # 「它被记成了未归属」这件事只在持有 atlas:usage:unattributed 的视图里可见。
        scoped = build_atlas_dto(
            atlas_request(
                requester(scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_UNATTRIBUTED))
            ),
            self.text,
            self.directory,
            SYNTHETIC,
        )
        self.assertEqual(scoped["unknown"]["unknown_key"], 1)
        self.assertIsNone(self.dto["unknown"]["unknown_key"])

    # ---- label 冒领 -----------------------------------------------------

    def test_label_spoofing_has_zero_effect(self) -> None:
        """把所有 label 抹掉（含 spoof-admin / ../../etc/passwd），DTO 必须逐字节相同。"""
        stripped = []
        for raw in self.text.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                stripped.append(raw)
                continue
            if isinstance(row, dict):
                row.pop("label", None)
                stripped.append(json.dumps(row))
            else:
                stripped.append(raw)

        spoiled = self.dto
        clean = build_atlas_dto(
            atlas_request(requester()),
            "\n".join(stripped) + "\n",
            self.directory,
            SYNTHETIC,
        )
        self.assertEqual(
            json.dumps(spoiled, sort_keys=True),
            json.dumps(clean, sort_keys=True),
        )

    def test_workload_ids_never_come_from_labels(self) -> None:
        ids = set(self._workloads())
        self.assertEqual(ids, {"resume-parse", "code-assist", "unattributed"})
        for spoof in ("spoof-admin", "../../etc/passwd", "misc"):
            self.assertNotIn(spoof, ids)

    # ---- 分位数 ---------------------------------------------------------

    def test_percentiles_come_from_raw_request_samples(self) -> None:
        latency = self.dto["latency"]
        self.assertEqual(latency["samples"], 5)
        self.assertEqual(latency["p50_ms"], 1000.0)
        self.assertEqual(latency["p95_ms"], 2640.0)
        self.assertEqual(latency["basis"], "raw-request-samples")

    def test_overall_p95_is_not_the_mean_of_group_p95(self) -> None:
        workloads = self._workloads()
        group_p95 = [w["latency"]["p95_ms"] for w in workloads.values()]
        self.assertEqual(group_p95, [800.0, 2820.0, 700.0])
        mean_of_p95 = sum(group_p95) / len(group_p95)
        self.assertEqual(mean_of_p95, 1440.0)
        self.assertNotEqual(self.dto["latency"]["p95_ms"], mean_of_p95)

    def test_group_p95_ordering_is_by_id_for_stability(self) -> None:
        self.assertEqual(list(self._workloads()), ["code-assist", "resume-parse", "unattributed"])

    def test_single_sample_group_p50_equals_p95(self) -> None:
        workloads = self._workloads()
        self.assertEqual(workloads["code-assist"]["latency"]["p50_ms"], 800.0)
        self.assertEqual(workloads["code-assist"]["latency"]["p95_ms"], 800.0)

    # ---- TTFT -----------------------------------------------------------

    def test_ttft_only_counts_successful_streaming_positive_values(self) -> None:
        ttft = self.dto["ttft"]
        self.assertEqual(ttft["samples"], 3)
        self.assertEqual(ttft["p50_ms"], 150.0)
        self.assertEqual(ttft["p95_ms"], 195.0)
        self.assertEqual(ttft["excluded_non_stream"], 2)

    def test_ttft_unknown_for_non_streaming_workload(self) -> None:
        self.assertIsNone(self._workloads()["code-assist"]["ttft"]["p50_ms"])
        self.assertIsNone(self._workloads()["code-assist"]["ttft"]["p95_ms"])

    # ---- 429 / 失败 -----------------------------------------------------

    def test_429_is_excluded_from_latency_and_counted_separately(self) -> None:
        totals = self.dto["totals"]
        self.assertEqual(totals["throttled_429"], 1)
        self.assertEqual(totals["failed"], 1)
        self.assertEqual(totals["success"], 5)
        self.assertEqual(self._workloads()["code-assist"]["throttled_429"], 1)

    def test_429_row_contributes_no_latency_sample(self) -> None:
        """code-assist 只有一条 200 样本；429 那条 total_ms 是 null，不得算进去。"""
        self.assertEqual(self._workloads()["code-assist"]["latency"]["samples"], 1)

    # ---- Tokens ---------------------------------------------------------

    def test_token_totals(self) -> None:
        tokens = self.dto["tokens"]
        self.assertEqual(tokens["input_total"], 370)
        self.assertEqual(tokens["output_total"], 780)
        self.assertEqual(self.dto["quality"]["tokens"], "measured")

    def test_requests_equals_success_count(self) -> None:
        self.assertEqual(self.dto["totals"]["requests"], 5)


if __name__ == "__main__":
    unittest.main()
