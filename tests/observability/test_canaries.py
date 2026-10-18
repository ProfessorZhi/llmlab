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

from observability_support import (  # noqa: E402
    ALICE_ROWS,
    FOREIGN_ROWS,
    NOW,
    SYNTHETIC,
    UID_ALICE,
    principal,
    registry,
    request,
    row,
    text_of,
    trusted_directory,
)

from backend.observability import (  # noqa: E402
    CATALOGUE,
    FIXED_EXPRESSIONS,
    MAX_POINTS,
    MAX_WINDOW_HOURS,
    QUALITY_UNKNOWN,
    ContractError,
    EngineProbeResult,
    EngineRecord,
    InvalidWindow,
    MetricReading,
    PrometheusProvider,
    UnknownMetric,
    build_engine_status_dto,
    build_self_usage_dto,
    expected_bucket_starts,
    percentile,
    status_from_probe,
    validate_usage_dto,
    validate_window,
)

# --------------------------------------------------------------------------
# 故意写坏的实现（只存在于测试里，绝不进 backend/）
# --------------------------------------------------------------------------


def canary_groups_by_label(text: str) -> set[str]:
    """错误做法一：把客户端自报的 ``label`` 当分组依据。"""
    ids: set[str] = set()
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("label") is not None:
            ids.add(str(payload["label"]))
    return ids


def canary_trusts_forwarded_identity(text: str, uid: str) -> int:
    """错误做法二：相信请求体里自报的 ``x_principal`` / ``X-Forwarded-User``。"""
    count = 0
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        claimed = payload.get("x_principal") or payload.get("X-Forwarded-User")
        if claimed == uid:
            count += 1
    return count


def canary_mean_of_group_p95(groups: list[list[float]]) -> float:
    """错误做法三：先算每组 p95，再对 p95 求平均当整体 p95。"""
    values = [percentile(sorted(samples), 0.95) for samples in groups]
    return sum(values) / len(values)


def canary_per_user_gpu_bytes(device_used_bytes: int, my_calls: int, total_calls: int) -> int:
    """错误做法四：把整卡已用显存按调用数占比切给个人。

    这条算术本身看起来「公平」，但它有一个致命性质：**别人多跑一点，我的显存就变少**。
    显存是整卡共享的，这个数字不描述任何真实资源。
    """
    if total_calls == 0:
        return 0
    return int(device_used_bytes * my_calls / total_calls)


def canary_unknown_is_down(probe_up) -> str:
    """错误做法五：把「没问到」当成 ``down``。"""
    return "up" if probe_up else "down"


#: fixture 里被伪造成「看起来像身份」的 label。
SPOOFED_LABELS = {"spoof-admin", "../../etc/passwd"}


class AttributionCanaryTest(unittest.TestCase):
    """归属必须来自受信目录 —— 金丝雀证明「不这么做的实现会泄漏」。"""

    def setUp(self) -> None:
        self.directory = trusted_directory()
        self.registry = registry()

    def _real_groups(self, rows):
        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            text_of(rows),
            self.directory,
            self.registry,
            SYNTHETIC,
            now=NOW,
        )
        return {group["id"] for group in dto["groups"]}, dto

    def test_labelling_canary_leaks_a_foreign_group_that_the_real_code_does_not(self) -> None:
        rows = ALICE_ROWS + FOREIGN_ROWS
        real_ids, real_dto = self._real_groups(rows)
        canary_ids = canary_groups_by_label(text_of(rows))

        # 金丝雀确实把别人的 label 变成了分组 —— 它「违反」了不变量。
        self.assertIn("eval-suite", canary_ids)
        self.assertIn("misc", canary_ids)
        # 而真实实现里没有这些名字。
        real_blob = json.dumps(real_dto, sort_keys=True)
        for leaked in ("eval-suite", "misc", "code-assist"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, real_ids)
                self.assertNotIn(leaked, real_blob)

    def test_label_canary_survives_even_when_the_label_spoofs_an_identity(self) -> None:
        rows = (row("2026-10-15T08:05:00Z", "key-alpha",
                    extra={"label": "spoof-admin"}),)
        canary_ids = canary_groups_by_label(text_of(rows))
        self.assertIn("spoof-admin", canary_ids)
        self.assertTrue(SPOOFED_LABELS & canary_ids)
        real_ids, dto = self._real_groups(rows)
        self.assertNotIn("spoof-admin", real_ids)
        self.assertNotIn("spoof-admin", json.dumps(dto, sort_keys=True))

    def test_forwarded_identity_canary_over_counts_and_the_real_code_does_not(self) -> None:
        rows = ALICE_ROWS + FOREIGN_ROWS
        canary_count = canary_trusts_forwarded_identity(text_of(rows), UID_ALICE)
        self.assertEqual(canary_count, 1, "金丝雀应当被自报字段骗到一次")

        _, dto = self._real_groups(rows)
        self.assertEqual(dto["counters"]["calls"], 3, "真实实现只数自己的 3 行")

    def test_forwarded_identity_is_invisible_to_the_real_implementation(self) -> None:
        """真实实现里，一行带着 ``x_principal: 我`` 的**他人**行不会进入我的计数。"""
        rows = ALICE_ROWS + (
            row("2026-10-15T09:30:00Z", "key-gamma",
                endpoint="model-shared-b", extra={"x_principal": UID_ALICE}),
        )
        _, dto = self._real_groups(rows)
        self.assertEqual(dto["counters"]["calls"], 3)
        self.assertNotIn("model-shared-b", json.dumps(dto, sort_keys=True))


class UnknownVersusZeroCanaryTest(unittest.TestCase):
    def test_zeroing_an_unattributable_counter_is_rejected_by_the_validator(self) -> None:
        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            text_of(ALICE_ROWS + FOREIGN_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertIsNone(dto["unknown"]["unknown_key"])

        # 金丝雀：像「贴心的默认值」那样把它写成 0。
        canary = json.loads(json.dumps(dto))
        canary["unknown"]["unknown_key"] = 0
        with self.assertRaises(ContractError):
            validate_usage_dto(canary)

    def test_the_zero_reading_shape_is_rejected_at_construction(self) -> None:
        """连「造一条 value=0 的 unknown 读数」都过不去 —— 这是最内层的一道闸。"""
        with self.assertRaises(Exception):
            MetricReading.from_metric(
                "principal.latency_p95_ms",
                value=0,
                source="gateway-metadata",
                quality=QUALITY_UNKNOWN,
                reason="no_sample",
                now=NOW,
            )

    def test_a_null_empty_sample_is_the_real_behaviour(self) -> None:
        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            "",
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertIsNone(dto["metrics"]["principal.latency_p95_ms"]["value"])
        self.assertEqual(dto["quality"]["calls"], QUALITY_UNKNOWN)


class PercentileCanaryTest(unittest.TestCase):
    def test_mean_of_group_p95_differs_from_the_true_p95(self) -> None:
        groups = [[10.0, 20.0], [1000.0, 2000.0]]
        flattened = sorted(value for group in groups for value in group)

        canary = canary_mean_of_group_p95(groups)
        truth = percentile(flattened, 0.95)

        self.assertNotAlmostEqual(canary, truth)
        self.assertLess(canary, truth)
        # 金丝雀确实更「好看」—— 这正是它会悄悄低估尾部延迟的原因。
        self.assertLess(canary, 1000.0)


class PerUserGpuMemoryCanaryTest(unittest.TestCase):
    """绝不能把整卡显存按调用占比切给个人。"""

    DEVICE_USED = 47_647_023_104

    def test_the_canary_split_moves_when_someone_else_runs_more(self) -> None:
        """我一行都没变，但「我的显存」变了 —— 这个数字不描述任何真实资源。"""
        mine_before = canary_per_user_gpu_bytes(self.DEVICE_USED, my_calls=100, total_calls=200)
        mine_after = canary_per_user_gpu_bytes(self.DEVICE_USED, my_calls=100, total_calls=400)
        self.assertEqual(mine_before, self.DEVICE_USED // 2)
        self.assertNotEqual(mine_before, mine_after)
        self.assertEqual(mine_after, self.DEVICE_USED // 4)

    def test_the_canary_invents_a_number_when_there_is_no_traffic_at_all(self) -> None:
        """总调用为 0 时它会返回一个 0 —— 一个「我数过，是零」的假结论。"""
        self.assertEqual(canary_per_user_gpu_bytes(self.DEVICE_USED, 0, 0), 0)

    def test_the_real_module_cannot_express_such_a_metric(self) -> None:
        for metric_id in (
            "principal.gpu.memory_used_bytes",
            "principal.gpu_memory_share",
            "principal.device.gpu.memory.used_bytes",
        ):
            with self.subTest(metric=metric_id):
                self.assertNotIn(metric_id, CATALOGUE)
                with self.assertRaises(UnknownMetric):
                    MetricReading.from_metric(
                        metric_id, value=1, source="x", observed_at=NOW, now=NOW
                    )

    def test_the_real_module_keeps_the_shared_figure_global(self) -> None:
        """整卡已用显存照实报为**整卡**读数，并在目录里标明是共享量。"""
        spec = CATALOGUE["device.gpu.memory.used_bytes"]
        self.assertEqual(spec.scope, "device")
        self.assertTrue(spec.physical)
        self.assertIn("共享", spec.description)


class EngineStateCanaryTest(unittest.TestCase):
    def test_the_canary_calls_an_unreached_probe_down(self) -> None:
        self.assertEqual(canary_unknown_is_down(None), "down")

    def test_the_real_code_reports_unknown_instead(self) -> None:
        record = EngineRecord("e", "ep", "E")
        status, reason = status_from_probe(EngineProbeResult(up=None, reason="source_unavailable"), record)
        self.assertIsNone(status)
        self.assertEqual(reason, "source_unavailable")
        self.assertNotEqual(status, "down")

        dto = build_engine_status_dto(
            request(principal(UID_ALICE)),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertEqual(dto["counts"]["down"], 0)
        self.assertEqual(dto["counts"]["unknown"], 1)
        # 金丝雀会把这一整屏渲染成 down=1。
        self.assertNotEqual(dto["counts"]["down"], 1)


class UnboundedWindowCanaryTest(unittest.TestCase):
    def test_the_window_guard_prevents_a_response_this_large(self) -> None:
        """金丝雀：越过上限直接算桶起点，点数会远远超过硬上限。"""
        from observability_support import parse_moment

        start = parse_moment("2026-01-01T00:00:00Z")
        end = parse_moment("2026-12-31T00:00:00Z")
        canary_points = len(expected_bucket_starts(start, end, "5m"))
        self.assertGreater(canary_points, MAX_POINTS)

    def test_the_real_code_refuses_that_window(self) -> None:
        from observability_support import parse_moment

        start = parse_moment("2026-01-01T00:00:00Z")
        end = parse_moment("2026-12-31T00:00:00Z")
        with self.assertRaises(InvalidWindow):
            validate_window(start, end, "5m")

    def test_every_bucket_has_a_span_limit_so_no_window_is_unbounded(self) -> None:
        for bucket in MAX_WINDOW_HOURS:
            with self.subTest(bucket=bucket):
                self.assertGreater(MAX_WINDOW_HOURS[bucket], 0)


class ArbitraryPromqlCanaryTest(unittest.TestCase):
    def test_the_registry_admits_only_a_fixed_set_of_queries(self) -> None:
        registered = {expression.query for expression in FIXED_EXPRESSIONS.values()}
        for sneaky in (
            'up{instance="attacker"}',
            "node_memory_MemTotal_bytes * 0",
            'rate(node_cpu_seconds_total[5m]) or vector(0)',
        ):
            with self.subTest(query=sneaky):
                self.assertNotIn(sneaky, registered)

    def test_an_unregistered_metric_name_cannot_be_read(self) -> None:
        class Recording:
            def __init__(self):
                self.calls = []

            def query(self, base_url, query, timeout_seconds):
                self.calls.append(query)
                return {"status": "success", "data": {"result": []}}

        transport = Recording()
        provider = PrometheusProvider(
            "http://127.0.0.1:9090", transport=transport, enabled=True
        )
        with self.assertRaises(UnknownMetric):
            provider.read('up{instance="attacker"}')
        self.assertEqual(transport.calls, [])


class FreshnessCanaryTest(unittest.TestCase):
    def test_a_stale_reading_is_not_rendered_as_current(self) -> None:
        from datetime import timedelta

        reading = MetricReading.from_metric(
            "device.gpu.utilization_percent",
            value=12.0,
            source="nvml",
            observed_at=NOW - timedelta(hours=3),
            now=NOW,
        )
        payload = reading.to_dict()
        self.assertEqual(payload["quality"], "measured")
        self.assertEqual(payload["display_quality"], "stale")
        self.assertNotEqual(payload["display_quality"], payload["quality"])


if __name__ == "__main__":
    unittest.main()
