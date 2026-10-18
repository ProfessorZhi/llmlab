"""Gateway 元数据行的解析：只认允许的字段，正文拒绝，自报身份**静默忽略**。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import load_fixture, row, text_of  # noqa: E402

from backend.observability import (  # noqa: E402
    IngestLimits,
    InputLimitExceeded,
    InvalidRequest,
    MAX_REQUEST_ID_LEN,
    SELF_REPORTED_IDENTITY_FIELDS,
    UsageAccumulator,
    event_endpoint,
    ingest_jsonl,
)


class HappyPathTest(unittest.TestCase):
    def test_a_clean_row_is_accepted(self) -> None:
        result = ingest_jsonl(text_of([row("2026-10-15T08:05:00Z", "key-alpha")]))
        self.assertEqual(result.total_lines, 1)
        self.assertEqual(result.malformed_lines, 0)
        self.assertEqual(result.content_field_rejected, 0)
        event = result.events[0]
        self.assertEqual(event.key_id, "key-alpha")
        self.assertEqual(event.status, 200)
        self.assertEqual(event.input_tokens, 100)
        self.assertEqual(event.total_ms, 1000.0)

    def test_blank_lines_are_not_counted_as_rows(self) -> None:
        result = ingest_jsonl("\n\n" + row("2026-10-15T08:05:00Z", "key-alpha") + "\n\n")
        self.assertEqual(result.total_lines, 1)

    def test_empty_input_yields_nothing(self) -> None:
        result = ingest_jsonl("")
        self.assertEqual(result.events, ())
        self.assertEqual(result.total_lines, 0)

    def test_endpoint_falls_back_to_model(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha", endpoint=None,
                         extra={"model": "model-x"})])
        )
        self.assertEqual(event_endpoint(result.events[0]), "model-x")

    def test_endpoint_is_none_when_neither_field_is_present(self) -> None:
        payload = json.dumps(
            {
                "ts": "2026-10-15T08:05:00Z",
                "key_id": "key-alpha",
                "status": 200,
                "outcome": "success",
                "stream": False,
            },
            sort_keys=True,
        )
        result = ingest_jsonl(text_of([payload]))
        self.assertIsNone(event_endpoint(result.events[0]))

    def test_explicit_endpoint_wins_over_model(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"endpoint": "ep-1", "model": "model-x"})])
        )
        self.assertEqual(event_endpoint(result.events[0]), "ep-1")


class RejectionTest(unittest.TestCase):
    def test_rows_with_content_fields_are_rejected_but_counted(self) -> None:
        for field in ("prompt", "completion", "messages", "content", "text"):
            with self.subTest(field=field):
                result = ingest_jsonl(
                    text_of([row("2026-10-15T08:05:00Z", "key-alpha", extra={field: "x"})])
                )
                self.assertEqual(result.content_field_rejected, 1)
                self.assertEqual(result.events, ())

    def test_content_field_detection_is_case_insensitive(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha", extra={"PROMPT": "x"})])
        )
        self.assertEqual(result.content_field_rejected, 1)

    def test_content_fields_are_found_when_nested(self) -> None:
        """只查顶层会放过 ``{"usage": {"prompt": …}}`` —— 正文没进事件，但

        「正文类字段 → 拒绝整行」这条不变量与它的计数会被悄悄绕过，而那个计数正是
        运维用来确认「上游有没有往元数据里塞正文」的依据。
        """
        nested = (
            json.dumps({
                "ts": "2026-10-15T08:05:00Z", "key_id": "key-alpha", "status": 200,
                "outcome": "success", "stream": False,
                "usage": {"input_tokens": 1, "output_tokens": 2, "prompt": "SECRET"},
            }, sort_keys=True),
            json.dumps({
                "ts": "2026-10-15T08:06:00Z", "key_id": "key-alpha", "status": 200,
                "outcome": "success", "stream": False,
                "meta": {"messages": [{"role": "user", "content": "SECRET"}]},
            }, sort_keys=True),
            json.dumps({
                "ts": "2026-10-15T08:07:00Z", "key_id": "key-alpha", "status": 200,
                "outcome": "success", "stream": False, "extra": [{"completion": "SECRET"}],
            }, sort_keys=True),
        )
        result = ingest_jsonl(text_of(nested))
        self.assertEqual(result.content_field_rejected, 3, result)
        self.assertEqual(result.events, ())

    def test_a_deeply_nested_structure_is_not_treated_as_clean(self) -> None:
        """深到看不清形状的结构不该被当作「我检查过了，是干净的」。"""
        from backend.observability import MAX_CONTENT_SCAN_DEPTH

        payload: dict = {"ts": "2026-10-15T08:05:00Z", "key_id": "key-alpha",
                         "status": 200, "outcome": "success", "stream": False}
        node: dict = payload
        for _ in range(MAX_CONTENT_SCAN_DEPTH + 3):
            child: dict = {}
            node["nest"] = child
            node = child
        result = ingest_jsonl(text_of([json.dumps(payload, sort_keys=True)]))
        self.assertEqual(result.content_field_rejected, 1)

    def test_ordinary_nesting_without_content_fields_is_still_accepted(self) -> None:
        """递归检测不能把正常行误杀。"""
        payload = json.dumps({
            "ts": "2026-10-15T08:05:00Z", "key_id": "key-alpha", "status": 200,
            "outcome": "success", "stream": False,
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "meta": {"retries": 0, "tags": ["a", "b"]},
        }, sort_keys=True)
        result = ingest_jsonl(text_of([payload]))
        self.assertEqual(result.content_field_rejected, 0)
        self.assertEqual(len(result.events), 1)

    def test_malformed_json_is_counted_not_raised(self) -> None:
        result = ingest_jsonl('{"ts":"2026-10-15T08:05:00Z","key_id":"key-alpha",\n')
        self.assertEqual(result.malformed_lines, 1)
        self.assertEqual(result.events, ())

    def test_json_scalars_are_malformed(self) -> None:
        result = ingest_jsonl(text_of(["42", '"a string"', "[1,2,3]"]))
        self.assertEqual(result.malformed_lines, 3)

    def test_bare_nan_and_infinity_are_malformed(self) -> None:
        """裸 ``NaN`` 不是合法 JSON —— 判坏行而不是猜一个数出来。"""
        for raw in (
            '{"ts":"2026-10-15T08:05:00Z","key_id":"k","status":NaN,"outcome":"success","stream":false}',
            '{"ts":"2026-10-15T08:05:00Z","key_id":"k","status":200,"outcome":"success","stream":false,"total_ms":Infinity}',
        ):
            with self.subTest(raw=raw):
                result = ingest_jsonl(text_of([raw]))
                self.assertEqual(result.malformed_lines, 1)
                self.assertEqual(result.events, ())

    def test_bad_field_values_are_malformed(self) -> None:
        cases = (
            {"ts": "not-a-time"},
            {"ts": "2026-10-15T08:05:00"},          # 无偏移
            {"key_id": ""},
            {"key_id": None},
            {"status": "200"},
            {"status": 99},
            {"status": 600},
            {"outcome": "maybe"},
            {"stream": "yes"},
            {"total_ms": -1},
            {"total_ms": "fast"},
            {"queue_ms": True},
            {"bytes_out": -5},
            {"usage": []},
            {"usage": {"input_tokens": -1}},
            {"usage": {"input_tokens": "many"}},
        )
        for planted in cases:
            with self.subTest(planted=planted):
                payload = {
                    "ts": "2026-10-15T08:05:00Z",
                    "key_id": "key-alpha",
                    "status": 200,
                    "outcome": "success",
                    "stream": False,
                }
                payload.update(planted)
                result = ingest_jsonl(text_of([json.dumps(payload, sort_keys=True)]))
                self.assertEqual(result.malformed_lines, 1, result)

    def test_offsets_are_normalised_to_utc(self) -> None:
        result = ingest_jsonl(text_of([row("2026-10-15T16:05:00+08:00", "key-alpha")]))
        self.assertEqual(result.total_lines, 1)
        self.assertEqual(result.events[0].ts.hour, 8)

    def test_null_optional_fields_are_accepted(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha", queue_ms=None,
                         total_ms=None, input_tokens=None, output_tokens=None,
                         extra={"label": None, "path": None})])
        )
        self.assertEqual(result.total_lines, 1)
        event = result.events[0]
        self.assertIsNone(event.queue_ms)
        self.assertIsNone(event.label)


class SelfReportedIdentityIsIgnoredTest(unittest.TestCase):
    """客户端自报的身份字段**永不**参与归属或分组。"""

    def test_identity_fields_never_reach_the_event(self) -> None:
        result = ingest_jsonl(
            text_of(
                [
                    row(
                        "2026-10-15T08:05:00Z",
                        "key-alpha",
                        extra={
                            "uid": "u-impostor",
                            "principal": "u-impostor",
                            "project": "proj-other",
                            "x_project": "proj-other",
                            "x_principal": "u-impostor",
                            "X-Forwarded-User": "u-impostor",
                            "owner": "u-impostor",
                            "endpoint_owner": "u-impostor",
                        },
                    )
                ]
            )
        )
        event = result.events[0]
        # 行仍然被接受（身份字段不是「正文」，不该判坏行），但字段本身没进事件。
        self.assertEqual(event.key_id, "key-alpha")
        for name in SELF_REPORTED_IDENTITY_FIELDS:
            with self.subTest(field=name):
                self.assertFalse(hasattr(event, name))

    def test_identity_fields_are_recorded_as_seen_for_diagnostics(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"x_principal": "u-impostor", "uid": "u-impostor"})])
        )
        seen = set(result.self_reported_fields_seen)
        self.assertIn("x_principal", seen)
        self.assertIn("uid", seen)

    def test_hyphenated_header_spellings_are_recognised_too(self) -> None:
        """``X-Forwarded-User`` 与 ``x_forwarded_user`` 是同一个东西。

        HTTP 头习惯写连字符、行内键习惯写下划线。少了归一化，连字符写法**仍然**不会被
        采信（未知字段一律忽略），但「我们见过这个字段」这句话会变成假话 ——
        而那句诊断正是运维用来确认「上游又加了什么头」的依据。
        回归：``test_report_and_docs.ContractDocumentTest`` 曾因此红过。
        """
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"X-Forwarded-User": "u-impostor",
                                "X-Auth-Request-User": "u-impostor"})])
        )
        seen = set(result.self_reported_fields_seen)
        self.assertIn("x_forwarded_user", seen)
        self.assertIn("x_auth_request_user", seen)
        self.assertEqual(result.malformed_lines, 0)
        self.assertEqual(len(result.events), 1)

    def test_normalisation_folds_case_hyphens_and_surrounding_space(self) -> None:
        from backend.observability import normalise_field_name

        for spelling in ("X-Forwarded-User", "x_forwarded_user", "X_FORWARDED_USER",
                         "  x-forwarded-user  "):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalise_field_name(spelling), "x_forwarded_user")
                self.assertIn(
                    normalise_field_name(spelling), SELF_REPORTED_IDENTITY_FIELDS
                )

    def test_every_declared_field_name_is_already_normalised(self) -> None:
        """集合本身必须是归一化形式，否则比对永远失配。"""
        from backend.observability import normalise_field_name

        for name in SELF_REPORTED_IDENTITY_FIELDS:
            with self.subTest(name=name):
                self.assertEqual(normalise_field_name(name), name)

    def test_identity_fields_do_not_make_a_row_malformed(self) -> None:
        """静默忽略，不是报错 —— 报错等于告诉上游「你写这个字段我们看见了」。"""
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"x_principal": "u-impostor"})])
        )
        self.assertEqual(result.malformed_lines, 0)
        self.assertEqual(len(result.events), 1)

    def test_unknown_non_identity_fields_are_tolerated(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"tenant_hint": "x", "retries": 3})])
        )
        self.assertEqual(result.malformed_lines, 0)
        self.assertEqual(len(result.events), 1)

    def test_the_fixture_no_longer_carries_a_trusted_identity(self) -> None:
        """fixture 里那行自报 ``u-1001`` 的未注册 key —— 它仍然无法归属。"""
        result = ingest_jsonl(load_fixture("gateway_sample.jsonl"))
        self.assertIn("x_principal", result.self_reported_fields_seen)
        zzz = [event for event in result.events if event.key_id == "unknown-key-zzz"]
        self.assertEqual(len(zzz), 1)


class RequestIdTest(unittest.TestCase):
    def test_a_reasonable_request_id_is_kept(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"request_id": "req-SYN-0001"})])
        )
        self.assertEqual(result.events[0].request_id, "req-SYN-0001")

    def test_blank_request_id_becomes_none(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha", extra={"request_id": "  "})])
        )
        self.assertIsNone(result.events[0].request_id)

    def test_overlong_request_id_is_malformed(self) -> None:
        """上界的存在是为了挡住「把正文塞进 request_id 当元数据」。"""
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                         extra={"request_id": "x" * (MAX_REQUEST_ID_LEN + 1)})])
        )
        self.assertEqual(result.malformed_lines, 1)

    def test_control_characters_in_request_id_are_malformed(self) -> None:
        for planted in ("a\nb", "a\tb", "a\x00b"):
            with self.subTest(planted=repr(planted)):
                result = ingest_jsonl(
                    text_of([row("2026-10-15T08:05:00Z", "key-alpha",
                                 extra={"request_id": planted})])
                )
                self.assertEqual(result.malformed_lines, 1)

    def test_missing_request_id_is_none_not_invented(self) -> None:
        result = ingest_jsonl(text_of([row("2026-10-15T08:05:00Z", "key-alpha")]))
        self.assertIsNone(result.events[0].request_id)


class LimitsTest(unittest.TestCase):
    def test_max_bytes_is_enforced(self) -> None:
        with self.assertRaises(InputLimitExceeded):
            ingest_jsonl("x" * 100, IngestLimits(max_bytes=10))

    def test_max_lines_is_enforced(self) -> None:
        text = text_of([row("2026-10-15T08:05:00Z", "key-alpha") for _ in range(5)])
        with self.assertRaises(InputLimitExceeded):
            ingest_jsonl(text, IngestLimits(max_lines=3))

    def test_overlong_lines_are_malformed(self) -> None:
        result = ingest_jsonl(
            text_of([row("2026-10-15T08:05:00Z", "key-alpha")]),
            IngestLimits(max_line_bytes=10),
        )
        self.assertEqual(result.malformed_lines, 1)

    def test_non_string_input_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            ingest_jsonl(b"bytes")


class AccumulatorTest(unittest.TestCase):
    def _accumulate(self, payloads):
        result = ingest_jsonl(text_of(payloads))
        accumulator = UsageAccumulator()
        for event in result.events:
            accumulator.add(event)
        return accumulator

    def test_success_latency_and_tokens(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", total_ms=1000)]
        )
        self.assertEqual(accumulator.calls, 1)
        self.assertEqual(accumulator.success, 1)
        self.assertEqual(accumulator.latency_samples, [1000.0])
        self.assertEqual(accumulator.input_tokens_total, 100)

    def test_throttled_requests_are_not_counted_as_failed_or_as_latency(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", status=429,
                 outcome="failed", total_ms=None)]
        )
        self.assertEqual(accumulator.throttled_429, 1)
        self.assertEqual(accumulator.failed, 0)
        self.assertEqual(accumulator.success, 0)
        self.assertEqual(accumulator.latency_samples, [])

    def test_outcome_status_mismatch_is_counted(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", status=500, outcome="success")]
        )
        self.assertEqual(accumulator.outcome_status_mismatch, 1)
        self.assertEqual(accumulator.success, 0)

    def test_non_stream_ttft_is_excluded_not_zeroed(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", stream=False, ttft_ms=None)]
        )
        self.assertEqual(accumulator.ttft_samples, [])
        self.assertEqual(accumulator.ttft_excluded_non_stream, 1)
        self.assertEqual(accumulator.ttft_missing, 0)

    def test_stream_ttft_missing_is_counted(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", stream=True, ttft_ms=None)]
        )
        self.assertEqual(accumulator.ttft_missing, 1)
        self.assertEqual(accumulator.ttft_samples, [])

    def test_zero_total_ms_is_missing_not_a_sample(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", total_ms=0)]
        )
        self.assertEqual(accumulator.latency_samples, [])
        self.assertEqual(accumulator.latency_missing, 1)

    def test_queue_is_collected_even_for_failures(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", status=500,
                 outcome="failed", queue_ms=42, total_ms=None)]
        )
        self.assertEqual(accumulator.queue_samples, [42.0])

    def test_percentile_blocks_are_empty_when_there_are_no_samples(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", status=429,
                 outcome="failed", total_ms=None, ttft_ms=None, queue_ms=None)]
        )
        block = accumulator.latency_block()
        self.assertIsNone(block["p95_ms"])
        self.assertIsNot(block["p95_ms"], 0)
        self.assertEqual(block["samples"], 0)

    def test_first_and_last_timestamps_track_the_window(self) -> None:
        accumulator = self._accumulate(
            [
                row("2026-10-15T09:05:00Z", "key-alpha"),
                row("2026-10-15T08:05:00Z", "key-alpha"),
            ]
        )
        self.assertEqual(accumulator.first_ts.hour, 8)
        self.assertEqual(accumulator.last_ts.hour, 9)


class SingleQualityRuleTest(unittest.TestCase):
    """`quality.calls` 的规则**只有一处实现**。

    早先这里有一个 `quality(observed_lines=…)` 版本、`service.py` 另有一个按 `calls`
    判定的副本，其中一个没人调用。契约规则有两个实现就迟早会漂移，而漂移会让
    「同一份数据在不同视图里质量不同」这种最难查的问题出现。
    """

    def _accumulate(self, payloads) -> UsageAccumulator:
        result = ingest_jsonl(text_of(payloads))
        accumulator = UsageAccumulator()
        for event in result.events:
            accumulator.add(event)
        return accumulator

    def test_the_service_rule_is_a_delegation_not_a_copy(self) -> None:
        import inspect

        from backend.observability import service

        source = inspect.getsource(service._calls_quality)
        self.assertIn("accumulator.quality()", source)
        # 规则本身不能在这里再写一遍。
        self.assertNotIn("QUALITY_UNKNOWN", source)
        self.assertNotIn("QUALITY_PARTIAL", source)

    def test_empty_accumulator_is_unknown(self) -> None:
        """空样本 -> unknown。空日志与没有接线在数据上无法区分。"""
        self.assertEqual(UsageAccumulator().quality(), "unknown")

    def test_clean_sample_is_measured(self) -> None:
        accumulator = self._accumulate([row("2026-10-15T08:05:00Z", "key-alpha")])
        self.assertEqual(accumulator.quality(), "measured")

    def test_missing_durations_make_it_partial(self) -> None:
        accumulator = self._accumulate(
            [row("2026-10-15T08:05:00Z", "key-alpha", queue_ms=None)]
        )
        self.assertEqual(accumulator.quality(), "partial")

    def test_the_rule_takes_no_arguments_so_it_cannot_be_called_wrong(self) -> None:
        import inspect

        parameters = list(inspect.signature(UsageAccumulator.quality).parameters)
        self.assertEqual(parameters, ["self"])


if __name__ == "__main__":
    unittest.main()
