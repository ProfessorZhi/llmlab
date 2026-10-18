"""UNKNOWN != 0，以及小时/日桶：没有可用小时日志时必须空态，不得伪造点阵。"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import SYNTHETIC, atlas_request, load_fixture, requester, trusted_directory  # noqa: E402

from backend.atlas import (  # noqa: E402
    SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
    SCOPE_USAGE_READ,
    build_atlas_dto,
    expected_bucket_starts,
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class BucketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = load_fixture("gateway_sample.jsonl")
        cls.directory = trusted_directory()

    def _build(self, ctx=None, **kwargs):
        return build_atlas_dto(
            atlas_request(ctx or requester(), **kwargs), self.text, self.directory, SYNTHETIC
        )

    # ---- expected_bucket_starts ----------------------------------------

    def test_expected_hourly_starts_cover_the_window(self) -> None:
        starts = expected_bucket_starts(
            _utc("2026-10-14T00:00:00Z"), _utc("2026-10-14T04:00:00Z"), "1h"
        )
        self.assertEqual([s.isoformat() for s in starts], [
            "2026-10-14T00:00:00+00:00",
            "2026-10-14T01:00:00+00:00",
            "2026-10-14T02:00:00+00:00",
            "2026-10-14T03:00:00+00:00",
        ])

    def test_expected_daily_starts_cover_the_window(self) -> None:
        starts = expected_bucket_starts(
            _utc("2026-10-14T00:00:00Z"), _utc("2026-10-16T00:00:00Z"), "1d"
        )
        self.assertEqual([s.date().isoformat() for s in starts], ["2026-10-14", "2026-10-15"])

    # ---- 有数据的窗口 ---------------------------------------------------

    def test_hourly_buckets_only_contain_observed_visible_hours(self) -> None:
        buckets = self._build()["buckets"]
        self.assertEqual([b["start"] for b in buckets["hourly"]], [
            "2026-10-14T00:00:00Z",
            "2026-10-14T01:00:00Z",
            "2026-10-14T02:00:00Z",
        ])
        self.assertEqual([b["calls"] for b in buckets["hourly"]], [4, 2, 1])

    def test_partial_coverage_is_declared_not_padded_with_zeros(self) -> None:
        buckets = self._build()["buckets"]
        self.assertEqual(buckets["expected_count"], 4)
        self.assertEqual(buckets["observed_count"], 3)
        self.assertEqual(self._build()["quality"]["hourly"], "partial")

    def test_daily_bucket_aggregates_the_day(self) -> None:
        daily = self._build()["buckets"]["daily"]
        self.assertEqual([b["start"] for b in daily], ["2026-10-14"])
        self.assertEqual(daily[0]["calls"], 7)

    def test_buckets_respect_the_authorized_scope(self) -> None:
        """只属于 proj-b 的时段不得在 proj-a 视图里出现任何桶。"""
        text = (
            '{"ts":"2026-10-14T06:10:00Z","key_id":"key-gamma","status":200,"outcome":"success",'
            '"stream":false,"usage":{"input_tokens":1,"output_tokens":1},"total_ms":10}\n'
        )
        dto = build_atlas_dto(
            atlas_request(requester(), window_from="2026-10-14T06:00:00Z", window_to="2026-10-14T07:00:00Z"),
            text,
            trusted_directory(),
            SYNTHETIC,
        )
        self.assertEqual(dto["buckets"]["hourly"], [])
        self.assertEqual(dto["buckets"]["observed_count"], 0)
        self.assertNotIn("proj-b", json.dumps(dto, sort_keys=True))

    def test_bucket_p95_uses_raw_samples_of_that_bucket(self) -> None:
        hourly = self._build()["buckets"]["hourly"]
        first = hourly[0]
        # 00:00 桶可测样本 = 1000 / 1200 / 800 → p50 1000，p95 1180
        self.assertEqual(first["latency"]["p50_ms"], 1000.0)
        self.assertEqual(first["latency"]["p95_ms"], 1180.0)

    # ---- 空窗口：UNKNOWN，不是 0 ----------------------------------------

    def test_empty_window_returns_unknown_not_zero(self) -> None:
        dto = self._build(window_from="2026-10-15T00:00:00Z", window_to="2026-10-15T03:00:00Z")

        self.assertEqual(dto["totals"]["calls"], 0)  # 真正测得 0 行
        self.assertEqual(dto["totals"]["success"], 0)

        # 但分位数与 tokens 是「无从测得」—— 必须是 None，不是 0
        self.assertIsNone(dto["latency"]["p50_ms"])
        self.assertIsNone(dto["latency"]["p95_ms"])
        self.assertIsNone(dto["ttft"]["p50_ms"])
        self.assertIsNone(dto["ttft"]["p95_ms"])
        self.assertIsNone(dto["tokens"]["input_total"])
        self.assertIsNone(dto["tokens"]["output_total"])
        self.assertNotEqual(dto["latency"]["p50_ms"], 0)
        self.assertEqual(dto["latency"]["samples"], 0)

        self.assertEqual(dto["groups"], [])
        self.assertEqual(dto["quality"]["latency"], "unknown")
        self.assertEqual(dto["quality"]["ttft"], "unknown")
        self.assertEqual(dto["quality"]["tokens"], "unknown")

    def test_empty_window_has_no_heatmap_grid(self) -> None:
        dto = self._build(window_from="2026-10-15T00:00:00Z", window_to="2026-10-15T03:00:00Z")
        self.assertEqual(dto["buckets"]["hourly"], [])
        self.assertEqual(dto["buckets"]["daily"], [])
        self.assertEqual(dto["buckets"]["expected_count"], 3)
        self.assertEqual(dto["buckets"]["observed_count"], 0)
        self.assertEqual(dto["quality"]["hourly"], "unknown")
        self.assertTrue(
            any("unknown" in hint or "empty" in hint for hint in dto["display_hints"]),
            dto["display_hints"],
        )

    def test_empty_input_file_is_also_unknown(self) -> None:
        dto = build_atlas_dto(
            atlas_request(requester()),
            load_fixture("gateway_empty.jsonl"),
            trusted_directory(),
            SYNTHETIC,
        )
        self.assertEqual(dto["totals"]["calls"], 0)
        self.assertIsNone(dto["latency"]["p95_ms"])
        # P0 收口：`malformed_lines` 是源文件级（不可归属）诊断，普通 viewer 看到的是
        # null（无从测得），不是 0。「这个文件确实没有坏行」只在显式持有
        # atlas:usage:global-diagnostics 的视图里可见 —— 见下一段。
        self.assertIsNone(dto["unknown"]["malformed_lines"])

    def test_empty_input_file_has_zero_malformed_lines_for_the_trusted_viewer(self) -> None:
        """同一个空文件：有全局诊断授权时才把「0 行坏行」这个**测得**的 0 说出来。"""
        ctx = requester(
            scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_GLOBAL_DIAGNOSTICS),
        )
        dto = build_atlas_dto(
            atlas_request(ctx),
            load_fixture("gateway_empty.jsonl"),
            trusted_directory(),
            SYNTHETIC,
        )
        self.assertEqual(dto["unknown"]["malformed_lines"], 0)
        self.assertEqual(dto["unknown"]["content_field_rejected"], 0)
        self.assertEqual(dto["quality"]["calls"], "measured")


if __name__ == "__main__":
    unittest.main()
