"""真实 Gateway 日志 ⇒ 观测契约的翻译层。

这里钉的是**翻译**，不是数字：一行 Gateway 记录经 ``normalise_row`` 之后必须能被
``ingest_jsonl`` 收下（``malformed == 0``），字段是**改名**而不是改值。同时钉住三件
「不许悄悄发生」的事：

* 翻译不了的行要**计数**（``unmapped``），不能被当成「日志里没有」；
* 文件读不到时 ``text`` 是空串且 ``summary`` 说 ``readable: False``，而不是 0 次调用；
* 窗口内没有调用（``in_window == 0``）与「日志是空的」（``events == 0``）是两回事。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.bff.gateway_usage import (
    GatewayUsageFeed,
    load_key_owners,
    normalise_row,
    read_tail,
)
from backend.observability.gateway import ingest_jsonl

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)


def gateway_row(**overrides):
    """一条形状与生产 ``usage.jsonl`` 一致的记录（默认 200/ok/非流式）。"""
    row = {
        "ts": "2026-10-15T01:37:43Z",
        "key_id": "k_7a0684a0",
        "label": "zhengji-system",
        "method": "POST",
        "path": "/v1/chat/completions",
        "model": "qwen3.8-flash-next-iq2_xs",
        "status": 200,
        "outcome": "ok",
        "stream": None,
        "usage": {"prompt_tokens": 89, "completion_tokens": 16, "total_tokens": 105},
        "bytes_out": 220,
        "queue_ms": 0.0,
        "ttft_ms": 2.0,
        "upstream_ms": 40.5,
        "total_ms": 44.0,
    }
    row.update(overrides)
    return row


class NormaliseIsRenamingNotRewriting(unittest.TestCase):
    def test_ok_becomes_success(self):
        self.assertEqual(normalise_row(gateway_row())["outcome"], "success")

    def test_rate_limited_becomes_failed(self):
        row = normalise_row(gateway_row(status=429, outcome="rate_limited"))
        self.assertEqual(row["outcome"], "failed")
        # 429 的形状留在 status 上：观测层靠它单独数 throttled_429。
        self.assertEqual(row["status"], 429)

    def test_null_stream_becomes_false(self):
        self.assertIs(normalise_row(gateway_row())["stream"], False)
        self.assertIs(normalise_row(gateway_row(stream=True))["stream"], True)

    def test_tokens_are_renamed_not_recomputed(self):
        usage = normalise_row(gateway_row())["usage"]
        self.assertEqual(usage, {"input_tokens": 89, "output_tokens": 16})

    def test_numbers_are_carried_over_untouched(self):
        row = normalise_row(gateway_row())
        self.assertEqual(row["total_ms"], 44.0)
        self.assertEqual(row["bytes_out"], 220)

    def test_an_unknown_outcome_is_not_translated(self):
        self.assertIsNone(normalise_row(gateway_row(outcome="weird")))

    def test_a_row_without_a_timestamp_is_not_translated(self):
        row = gateway_row()
        row.pop("ts")
        self.assertIsNone(normalise_row(row))

    def test_a_non_http_status_is_not_translated(self):
        self.assertIsNone(normalise_row(gateway_row(status=0)))

    def test_endpoint_is_filled_only_from_the_given_table(self):
        self.assertNotIn("endpoint", normalise_row(gateway_row()))
        row = normalise_row(
            gateway_row(), endpoint_for={"qwen3.8-flash-next-iq2_xs": "ep_32b400432bd1c6c1"}
        )
        self.assertEqual(row["endpoint"], "ep_32b400432bd1c6c1")


class ReadTailDropsTheCutRow(unittest.TestCase):
    def test_a_truncated_first_line_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for index in range(20):
                    handle.write(json.dumps(gateway_row(label=f"row-{index}")) + "\n")
            size = os.path.getsize(path)
            text, read = read_tail(path, max_bytes=size // 2)
            self.assertLessEqual(read, size // 2)
            for line in text.splitlines():
                self.assertIsInstance(json.loads(line), dict)

    def test_a_whole_file_is_returned_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(gateway_row()) + "\n")
            text, _read = read_tail(path, max_bytes=1024)
            self.assertEqual(len(text.splitlines()), 1)


class TheFeedSatisfiesTheIngestContract(unittest.TestCase):
    def _feed(self, rows):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "usage.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        self.addCleanup(lambda: os.remove(path))
        return GatewayUsageFeed(path, now=lambda: NOW)

    def test_every_real_shaped_row_survives_ingestion(self):
        feed = self._feed(
            [
                gateway_row(),
                gateway_row(status=429, outcome="rate_limited"),
                gateway_row(status=400, outcome="failed", stream=True),
                gateway_row(outcome="cancelled"),
            ]
        )
        result = ingest_jsonl(feed.text(object()))
        self.assertEqual(result.malformed_lines, 0)
        self.assertEqual(result.content_field_rejected, 0)
        self.assertEqual(len(result.events), 4)

    def test_untranslatable_rows_are_counted_not_forgotten(self):
        feed = self._feed([gateway_row(), gateway_row(outcome="weird")])
        feed.text(object())
        summary = feed.summary()
        self.assertEqual(summary["lines"], 2)
        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["unmapped"], 1)

    def test_rows_inside_the_window_are_counted(self):
        feed = self._feed([gateway_row()])  # 10-15 01:37，距离 NOW 只有 23 分钟
        text = feed.text(object())
        ingest_jsonl(text)
        self.assertEqual(feed.summary()["in_window"], 1)
        self.assertEqual(feed.summary()["last_event_ts"], "2026-10-15T01:37:43Z")

    def test_old_rows_stay_out_of_the_window(self):
        old = (NOW - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        feed = self._feed([gateway_row(ts=old)])
        self.assertEqual(feed.summary()["events"], 1)
        self.assertEqual(feed.summary()["in_window"], 0)

    def test_a_missing_file_is_empty_text_and_says_so(self):
        feed = GatewayUsageFeed("/nonexistent/usage.jsonl", now=lambda: NOW)
        self.assertEqual(feed.text(object()), "")
        summary = feed.summary()
        self.assertFalse(summary["readable"])
        self.assertEqual(summary["events"], 0)


class OwnersAreDeclaredNotGuessed(unittest.TestCase):
    def _write(self, document):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "gateway-key-owners.json")
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(document, str):
                handle.write(document)
            else:
                json.dump(document, handle)
        self.addCleanup(lambda: os.remove(path))
        return path

    def test_no_file_means_no_bindings(self):
        self.assertEqual(load_key_owners("/nonexistent/owners.json"), {})
        self.assertEqual(load_key_owners(None), {})

    def test_a_broken_file_yields_no_bindings(self):
        self.assertEqual(load_key_owners(self._write("{not json")), {})

    def test_entries_may_be_objects_or_bare_names(self):
        path = self._write(
            {
                "schema": "gateway-key-owners/1",
                "keys": {
                    "k_a": {"owner": "linux:dell-test-01:1003"},
                    "k_b": "account:319ff153f9792ed5d8bd353c3260b4e5",
                    "k_c": {"evidence": "no owner here"},
                },
            }
        )
        self.assertEqual(
            load_key_owners(path),
            {
                "k_a": "linux:dell-test-01:1003",
                "k_b": "account:319ff153f9792ed5d8bd353c3260b4e5",
            },
        )


if __name__ == "__main__":
    unittest.main()
