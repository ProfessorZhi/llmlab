"""金丝雀：证明上面那些负测**真的会失败**。

只写「应该拒绝」的断言是不够的 —— 断言本身可能是空的（写错了、恒真、或者根本没覆盖
到那个分支）。这里显式写出几种「错误但看似合理」的实现，并断言它们**确实违反**我们
声明的不变量。如果有人把负测弱化成恒真，这一组会立刻红。

如果哪天这些金丝雀不再违反不变量，说明不变量本身失效了 —— 此时**不得**声称 READY。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import SYNTHETIC, atlas_request, load_fixture, requester, trusted_directory  # noqa: E402

from backend.atlas import IngestLimits, build_atlas_dto, ingest_jsonl  # noqa: E402
from backend.atlas.stats import percentile  # noqa: E402


# --------------------------------------------------------------------------
# 故意写坏的实现（只存在于测试里，绝不进 backend/）
# --------------------------------------------------------------------------

def canary_workloads_from_label(text: str) -> set[str]:
    """错误做法一：把客户端 label 当工作负载。"""
    ids: set[str] = set()
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("label") is not None:
            ids.add(str(row["label"]))
    return ids


def canary_workloads_from_client_headers(text: str) -> set[str]:
    """错误做法二：相信请求体里自报的 x_project。"""
    ids: set[str] = set()
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and "x_project" in row:
            ids.add(str(row["x_project"]))
    return ids


def canary_mean_of_p95(group_p95: list[float]) -> float:
    """错误做法三：把各组 p95 求平均当整体 p95。"""
    return sum(group_p95) / len(group_p95)


def canary_ttft_including_non_stream(events) -> list[float]:
    """错误做法四：把非流式成功请求的缺失 TTFT 当作 0 一并计入。"""
    return [e.ttft_ms if e.ttft_ms is not None else 0.0 for e in events]


def canary_duration_of_every_row(events) -> list[float]:
    """错误做法五：只要行里有耗时就算样本 —— 无视 429 / 失败 / 跨项目。"""
    return [e.total_ms for e in events if e.total_ms is not None]


def canary_unknown_as_zero(_events) -> float:
    """错误做法六：没有样本时返回 0 而不是 None。"""
    return 0.0


#: fixture 里被伪造成「看起来像身份/路径」的 label。
SPOOFED_LABELS = {"spoof-admin", "../../etc/passwd", "x", "misc"}
#: 其中足够独特、可以安全地做「不该出现在 DTO 里」字符串断言的那些。
DISTINCTIVE_SPOOFED_LABELS = {"spoof-admin", "../../etc/passwd", "misc"}
THROTTLED_WITH_DURATION = (
    '{"ts":"2026-10-14T00:35:00Z","key_id":"key-beta","status":429,"outcome":"failed",'
    '"stream":false,"usage":null,"total_ms":9999}\n'
)


# --------------------------------------------------------------------------


class CanaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = load_fixture("gateway_sample.jsonl")
        cls.directory = trusted_directory()
        cls.dto = build_atlas_dto(
            atlas_request(requester()), cls.text, cls.directory, SYNTHETIC
        )
        cls.events = ingest_jsonl(cls.text, IngestLimits()).events

    def _real_workload_ids(self) -> set[str]:
        return {w["id"] for g in self.dto["groups"] for w in g["workloads"]}

    def test_label_grouping_canary_is_caught(self) -> None:
        spoofed = canary_workloads_from_label(self.text)
        self.assertTrue({"spoof-admin", "../../etc/passwd"} <= spoofed)
        # 真实实现里这些字符串不可能成为工作负载 id
        self.assertFalse(SPOOFED_LABELS & self._real_workload_ids())
        blob = json.dumps(self.dto, sort_keys=True)
        for token in DISTINCTIVE_SPOOFED_LABELS:
            self.assertNotIn(token, blob)

    def test_client_header_grouping_canary_is_caught(self) -> None:
        spoofed = canary_workloads_from_client_headers(self.text)
        self.assertEqual(spoofed, {"proj-b"})
        self.assertNotIn("proj-b", json.dumps(self.dto, sort_keys=True))

    def test_mean_of_p95_canary_is_caught(self) -> None:
        group_p95 = [w["latency"]["p95_ms"] for w in self.dto["groups"][0]["workloads"]]
        canary = canary_mean_of_p95(group_p95)
        self.assertEqual(canary, 1440.0)
        self.assertNotEqual(canary, self.dto["latency"]["p95_ms"])

    def test_ttft_non_stream_canary_is_caught(self) -> None:
        canary_samples = canary_ttft_including_non_stream(self.events)
        canary_p50 = percentile(sorted(canary_samples), 0.5)
        # 金丝雀把 11 条结构可信行全部计入（含非流式、失败、窗口外），样本数被灌水到 11。
        self.assertEqual(len(canary_samples), 11)
        self.assertEqual(canary_p50, 0.0)
        self.assertNotEqual(canary_p50, self.dto["ttft"]["p50_ms"])
        self.assertNotEqual(len(canary_samples), self.dto["ttft"]["samples"])

    def test_429_with_duration_canary_is_caught(self) -> None:
        """429 即便带着耗时也不得进入延迟样本 —— 金丝雀会把它算进去。"""
        canary = canary_duration_of_every_row(
            ingest_jsonl(THROTTLED_WITH_DURATION, IngestLimits()).events
        )
        self.assertEqual(canary, [9999])

        dto = build_atlas_dto(
            atlas_request(requester()), THROTTLED_WITH_DURATION, self.directory, SYNTHETIC
        )
        self.assertEqual(dto["latency"]["samples"], 0)
        self.assertIsNone(dto["latency"]["p95_ms"])
        self.assertEqual(dto["totals"]["throttled_429"], 1)
        self.assertEqual(dto["totals"]["success"], 0)

    def test_cross_project_canary_is_caught(self) -> None:
        """不分项目白名单的实现会把 proj-b 的样本算进来。"""
        canary_samples = canary_duration_of_every_row(self.events)
        self.assertEqual(len(canary_samples), 10)
        self.assertEqual(self.dto["latency"]["samples"], 5)
        self.assertNotEqual(len(canary_samples), self.dto["latency"]["samples"])

    def test_unknown_as_zero_canary_is_caught(self) -> None:
        empty_dto = build_atlas_dto(atlas_request(requester()), "", self.directory, SYNTHETIC)
        self.assertEqual(canary_unknown_as_zero([]), 0.0)
        self.assertIsNone(empty_dto["latency"]["p50_ms"])
        self.assertIsNot(canary_unknown_as_zero([]), empty_dto["latency"]["p50_ms"])


if __name__ == "__main__":
    unittest.main()
