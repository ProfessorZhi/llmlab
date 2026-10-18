"""输入校验负测：坏行必须被拒绝并计数，绝不能混进统计。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import load_fixture  # noqa: E402

from backend.atlas import (  # noqa: E402
    MAX_REQUEST_ID_LEN,
    IngestLimits,
    InputLimitExceeded,
    ingest_jsonl,
)

GOOD = {
    "ts": "2026-10-14T00:05:00Z",
    "key_id": "key-alpha",
    "label": "whatever",
    "method": "POST",
    "path": "/v1/chat/completions",
    "model": "llm-a",
    "status": 200,
    "outcome": "success",
    "stream": True,
    "usage": {"input_tokens": 10, "output_tokens": 20},
    "bytes_out": 100,
    "queue_ms": 1,
    "ttft_ms": 5,
    "upstream_ms": 50,
    "total_ms": 100,
}


def line(**overrides) -> str:
    row = dict(GOOD)
    row.update(overrides)
    return json.dumps(row)


class IngestValidationTest(unittest.TestCase):
    def _count(self, *lines: str, limits: IngestLimits = IngestLimits()):
        return ingest_jsonl("\n".join(lines) + "\n", limits)

    def test_valid_line_is_accepted(self) -> None:
        result = self._count(line())
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.malformed_lines, 0)
        self.assertEqual(result.content_field_rejected, 0)

    def test_broken_json_is_rejected(self) -> None:
        result = self._count('{"ts":"2026-10-14T00:05:00Z","key_id":"key-alpha"')
        self.assertEqual(len(result.events), 0)
        self.assertEqual(result.malformed_lines, 1)

    def test_non_object_line_is_rejected(self) -> None:
        result = self._count("[1,2,3]", '"just a string"', "42")
        self.assertEqual(len(result.events), 0)
        self.assertEqual(result.malformed_lines, 3)

    def test_naive_timestamp_is_rejected(self) -> None:
        """没有时区的时间戳不可信 —— 无法确定它属于哪个小时桶。"""
        result = self._count(line(ts="2026-10-14T00:05:00"))
        self.assertEqual(result.malformed_lines, 1)
        self.assertEqual(len(result.events), 0)

    def test_bad_timestamp_string_is_rejected(self) -> None:
        result = self._count(line(ts="yesterday at noon"))
        self.assertEqual(result.malformed_lines, 1)

    def test_missing_and_blank_key_id_are_rejected(self) -> None:
        row = dict(GOOD)
        del row["key_id"]
        self.assertEqual(self._count(json.dumps(row)).malformed_lines, 1)
        self.assertEqual(self._count(line(key_id="   ")).malformed_lines, 1)

    def test_status_outside_http_range_is_rejected(self) -> None:
        for status in (0, 99, 600, -200, "200"):
            with self.subTest(status=status):
                self.assertEqual(self._count(line(status=status)).malformed_lines, 1)

    def test_unknown_outcome_is_rejected(self) -> None:
        for outcome in ("SUCCESS", "ok", "", None, 1):
            with self.subTest(outcome=outcome):
                self.assertEqual(self._count(line(outcome=outcome)).malformed_lines, 1)

    def test_negative_duration_is_rejected(self) -> None:
        for field in ("queue_ms", "ttft_ms", "upstream_ms", "total_ms"):
            with self.subTest(field=field):
                self.assertEqual(self._count(line(**{field: -1})).malformed_lines, 1)

    def test_nan_and_infinite_durations_are_rejected(self) -> None:
        """JSON 里裸 NaN / Infinity 不是合法 JSON，必须被挡住。"""
        raw = '{"ts":"2026-10-14T00:05:00Z","key_id":"key-alpha","status":200,"outcome":"success","stream":true,"total_ms":NaN}'
        self.assertEqual(self._count(raw).malformed_lines, 1)
        raw_inf = raw.replace("NaN", "Infinity")
        self.assertEqual(self._count(raw_inf).malformed_lines, 1)

    def test_float_durations_are_accepted(self) -> None:
        result = self._count(line(total_ms=12.5, ttft_ms=1.25))
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].total_ms, 12.5)

    def test_non_boolean_stream_is_rejected(self) -> None:
        for value in ("true", 1, None):
            with self.subTest(value=value):
                self.assertEqual(self._count(line(stream=value)).malformed_lines, 1)

    def test_negative_or_non_integer_tokens_are_rejected(self) -> None:
        for usage in (
            {"input_tokens": -1},
            {"output_tokens": -5},
            {"input_tokens": "10"},
            {"output_tokens": 1.5},
        ):
            with self.subTest(usage=usage):
                self.assertEqual(self._count(line(usage=usage)).malformed_lines, 1)

    def test_usage_must_be_object_when_present(self) -> None:
        self.assertEqual(self._count(line(usage=[1, 2])).malformed_lines, 1)
        self.assertEqual(self._count(line(usage="10 tokens")).malformed_lines, 1)

    def test_missing_tokens_are_null_not_zero(self) -> None:
        result = self._count(line(usage={}))
        self.assertEqual(len(result.events), 1)
        self.assertIsNone(result.events[0].input_tokens)
        self.assertIsNone(result.events[0].output_tokens)

    def test_content_fields_are_rejected_and_counted_separately(self) -> None:
        for field in ("prompt", "completion", "messages", "content"):
            with self.subTest(field=field):
                result = self._count(line(**{field: "<SYNTHETIC-CANARY>"}))
                self.assertEqual(len(result.events), 0)
                self.assertEqual(result.malformed_lines, 0)
                self.assertEqual(result.content_field_rejected, 1)

    def test_forged_client_headers_are_ignored_not_honoured(self) -> None:
        """伪造的 X-* / x_project 字段只是被忽略的未知字段，不进 RawEvent。"""
        result = self._count(line(**{"x_project": "proj-b", "X-Forwarded-User": "u-2001"}))
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertFalse(hasattr(event, "x_project"))
        self.assertEqual(event.key_id, "key-alpha")

    def test_unknown_key_is_ingested_not_malformed(self) -> None:
        """未知 key 是「无法归属」，不是「坏行」——两者必须分开计数。"""
        result = self._count(line(key_id="never-registered"))
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.malformed_lines, 0)

    # ---- 滥用上限 -------------------------------------------------------

    def test_byte_limit_is_enforced(self) -> None:
        limits = IngestLimits(max_bytes=64)
        with self.assertRaises(InputLimitExceeded):
            self._count(line(), limits=limits)

    def test_line_count_limit_is_enforced(self) -> None:
        limits = IngestLimits(max_lines=2)
        with self.assertRaises(InputLimitExceeded):
            self._count(line(), line(), line(), limits=limits)

    def test_single_oversized_line_is_rejected(self) -> None:
        limits = IngestLimits(max_line_bytes=32)
        result = self._count(line(), limits=limits)
        self.assertEqual(len(result.events), 0)
        self.assertEqual(result.malformed_lines, 1)

    def test_blank_lines_are_skipped_not_counted_as_malformed(self) -> None:
        result = ingest_jsonl(line() + "\n\n\n", IngestLimits())
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.malformed_lines, 0)

    def test_fixture_accounting_matches_expectations(self) -> None:
        result = ingest_jsonl(load_fixture("gateway_sample.jsonl"), IngestLimits())
        self.assertEqual(result.total_lines, 15)
        # 窗口过滤发生在聚合阶段：ingest 阶段 15 行里有 11 行结构可信（含 1 行窗口外）。
        self.assertEqual(len(result.events), 11)
        self.assertEqual(result.malformed_lines, 3)
        self.assertEqual(result.content_field_rejected, 1)


class RequestIdTest(unittest.TestCase):
    """可选字段 ``request_id``：它是不透明句柄，不是第二条内容通道。"""

    def _one(self, **overrides):
        result = ingest_jsonl(line(**overrides), IngestLimits())
        self.assertEqual(len(result.events), 1, "expected the row to be trusted")
        return result.events[0]

    def test_absent_is_none(self) -> None:
        self.assertIsNone(self._one().request_id)

    def test_a_plain_string_is_kept_verbatim(self) -> None:
        self.assertEqual(self._one(request_id="req-SYN-0001").request_id, "req-SYN-0001")

    def test_surrounding_whitespace_is_stripped(self) -> None:
        self.assertEqual(self._one(request_id="  req-a  ").request_id, "req-a")

    def test_blank_is_none_not_an_empty_string(self) -> None:
        for blank in ("", "   ", "\t"):
            with self.subTest(value=repr(blank)):
                # ``\t`` 被 strip 掉之后为空 —— 但控制字符判定在 strip 之前就该挡住它，
                # 所以这里只断言「不会留下一个空的 id」。
                row = json.loads(line(request_id=blank))
                row["request_id"] = blank.replace("\t", " ")
                result = ingest_jsonl(json.dumps(row), IngestLimits())
                self.assertIsNone(result.events[0].request_id)

    def test_a_non_string_is_a_bad_row(self) -> None:
        for value in (123, 12.5, True, ["a"], {"a": 1}):
            with self.subTest(value=repr(value)):
                result = ingest_jsonl(line(request_id=value), IngestLimits())
                self.assertEqual(result.events, ())
                self.assertEqual(result.malformed_lines, 1)

    def test_an_over_long_id_is_a_bad_row(self) -> None:
        """正文塞进 ``request_id`` 的绕行必须被挡住。"""
        result = ingest_jsonl(line(request_id="x" * (MAX_REQUEST_ID_LEN + 1)), IngestLimits())
        self.assertEqual(result.events, ())
        self.assertEqual(result.malformed_lines, 1)

    def test_the_length_boundary_is_exact(self) -> None:
        keep = ingest_jsonl(line(request_id="x" * MAX_REQUEST_ID_LEN), IngestLimits())
        self.assertEqual(len(keep.events), 1)
        self.assertEqual(len(keep.events[0].request_id), MAX_REQUEST_ID_LEN)

    def test_control_characters_are_a_bad_row(self) -> None:
        """一个带换行的 id 能让下游把一行 JSONL 读成两行。"""
        for value in ("req\na", "req\ta", "req\x00a", "req\x7fa", "req\x9ba"):
            with self.subTest(value=repr(value)):
                result = ingest_jsonl(line(request_id=value), IngestLimits())
                self.assertEqual(result.events, ())
                self.assertEqual(result.malformed_lines, 1)

    def test_non_ascii_ids_are_fine(self) -> None:
        self.assertEqual(self._one(request_id="请求-0001").request_id, "请求-0001")

    def test_the_calls_fixture_parses_every_request_id(self) -> None:
        """ingest 层不做授权 —— 它把 fixture 里**所有**行可信的 id 都解析出来。

        其中 ``req-SYN-0100`` / ``req-SYN-0200`` 属于他人 key：挡下它们是逐条视图
        的职责（见 ``test_self_calls.py``），不是 ingest 的。
        """
        result = ingest_jsonl(load_fixture("gateway_calls_sample.jsonl"), IngestLimits())
        present = {event.request_id for event in result.events if event.request_id}
        self.assertEqual(
            present,
            {
                "req-SYN-0001",
                "req-SYN-0002",
                "req-SYN-0003",
                "req-SYN-0004",
                "req-SYN-0005",
                "req-SYN-0100",
                "req-SYN-0200",
            },
        )

    def test_the_aggregate_fixture_has_no_request_id_at_all(self) -> None:
        """``request_id`` 是纯增量：既有 fixture 一个都没有，行为因此不变。"""
        result = ingest_jsonl(load_fixture("gateway_sample.jsonl"), IngestLimits())
        self.assertTrue(all(event.request_id is None for event in result.events))


if __name__ == "__main__":
    unittest.main()
