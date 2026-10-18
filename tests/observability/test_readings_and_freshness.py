"""读数信封：``source`` / ``observed_at`` / ``quality`` / ``freshness`` 与 UNKNOWN ≠ 0。

本文件的验收口径：

> ``null``（无从测得）与 ``0``（确实测得零）必须在**类型层面**就分开，
> 且任何一条读数都必须能回答「谁在什么时候测的、还新不新」。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import NOW  # noqa: E402

from backend.observability import (  # noqa: E402
    CATALOGUE,
    DISPLAY_STALE,
    DRIVER_STATES,
    PHYSICAL_SCOPES,
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    REASON_NO_SAMPLE,
    REASON_UNSUPPORTED,
    SCOPE_DEVICE,
    SCOPE_PRINCIPAL,
    InvalidRequest,
    MetricReading,
    StateReading,
    TextReading,
    UnknownMetric,
    freshness_block,
)

from datetime import timedelta, timezone


class UnknownIsNotNullTest(unittest.TestCase):
    """UNKNOWN 必须表达为 ``value=None`` + ``reason``，绝不能是 0。"""

    def test_unknown_reading_carries_none_and_a_reason(self) -> None:
        reading = MetricReading.unknown(
            "device.gpu.temperature_celsius", source="nvml", reason=REASON_UNSUPPORTED, now=NOW
        )
        payload = reading.to_dict()
        self.assertIsNone(payload["value"])
        self.assertIsNot(payload["value"], 0)
        self.assertEqual(payload["quality"], QUALITY_UNKNOWN)
        self.assertEqual(payload["display_quality"], QUALITY_UNKNOWN)
        self.assertEqual(payload["reason"], REASON_UNSUPPORTED)
        self.assertIsNone(payload["observed_at"])

    def test_unknown_reading_rejects_a_numeric_value(self) -> None:
        for planted in (0, 1, 0.0):
            with self.subTest(planted=planted):
                with self.assertRaises(InvalidRequest):
                    MetricReading.from_metric(
                        "node.gpu.count",
                        value=planted,
                        source="host-proc",
                        quality=QUALITY_UNKNOWN,
                        reason=REASON_NO_SAMPLE,
                        now=NOW,
                    )

    def test_unknown_reading_requires_a_known_reason_code(self) -> None:
        with self.assertRaises(InvalidRequest):
            MetricReading.from_metric(
                "node.gpu.count",
                value=None,
                source="host-proc",
                quality=QUALITY_UNKNOWN,
                reason="because",
                now=NOW,
            )

    def test_measured_reading_rejects_a_null_value(self) -> None:
        with self.assertRaises(InvalidRequest):
            MetricReading.from_metric(
                "node.gpu.count",
                value=None,
                source="host-proc",
                quality=QUALITY_MEASURED,
                observed_at=NOW,
                now=NOW,
            )

    def test_measured_reading_rejects_a_reason(self) -> None:
        """``reason`` 只对 unknown 有意义；给 measured 挂 reason 会让 UI 两套提示并存。"""
        with self.assertRaises(InvalidRequest):
            MetricReading.from_metric(
                "node.gpu.count",
                value=1,
                source="host-proc",
                observed_at=NOW,
                reason=REASON_NO_SAMPLE,
                now=NOW,
            )

    def test_measured_reading_requires_observed_at(self) -> None:
        with self.assertRaises(InvalidRequest):
            MetricReading.from_metric(
                "node.gpu.count", value=1, source="host-proc", now=NOW
            )

    def test_zero_is_a_legitimate_measured_value(self) -> None:
        """确实测得零 —— 那是一个整数 0，与 None 不同类。"""
        reading = MetricReading.from_metric(
            "device.gpu.utilization_percent",
            value=0,
            source="nvml",
            observed_at=NOW,
            now=NOW,
        )
        payload = reading.to_dict()
        self.assertEqual(payload["value"], 0)
        self.assertIsNotNone(payload["value"])
        self.assertEqual(payload["quality"], QUALITY_MEASURED)


class CatalogueIsTheOnlySourceOfShapeTest(unittest.TestCase):
    """scope / unit / physical 一律取自目录 —— 调用方无从覆写。"""

    def test_unknown_metric_name_is_rejected(self) -> None:
        with self.assertRaises(UnknownMetric):
            MetricReading.from_metric(
                "principal.gpu.memory_bytes", value=1, source="x", observed_at=NOW, now=NOW
            )

    def test_no_per_principal_gpu_memory_metric_exists_anywhere(self) -> None:
        """「共享实例上某用户占了多少显存」在本模块**无法表达**。"""
        offending = [
            metric_id
            for metric_id, spec in CATALOGUE.items()
            if spec.scope == SCOPE_PRINCIPAL and spec.unit == "bytes"
        ]
        self.assertEqual(offending, [])

    def test_every_principal_metric_is_a_count_or_a_duration(self) -> None:
        allowed_units = {"count", "tokens", "milliseconds"}
        for metric_id, spec in CATALOGUE.items():
            if spec.scope == SCOPE_PRINCIPAL:
                with self.subTest(metric=metric_id):
                    self.assertIn(spec.unit, allowed_units)

    def test_physical_flag_is_derived_from_scope(self) -> None:
        for metric_id, spec in CATALOGUE.items():
            with self.subTest(metric=metric_id):
                self.assertEqual(spec.physical, spec.scope in PHYSICAL_SCOPES)

    def test_reading_shape_follows_the_catalogue(self) -> None:
        reading = MetricReading.from_metric(
            "device.gpu.memory.used_bytes",
            value=1024,
            source="nvml",
            observed_at=NOW,
            now=NOW,
        )
        self.assertEqual(reading.scope, SCOPE_DEVICE)
        self.assertEqual(reading.unit, "bytes")
        self.assertTrue(reading.physical)

    def test_a_reading_has_no_uid_field_at_all(self) -> None:
        """物理读数连「挂一个 uid」的字段都没有 —— 这是结构性的，不是校验出来的。"""
        reading = MetricReading.from_metric(
            "device.gpu.memory.used_bytes", value=1, source="nvml", observed_at=NOW, now=NOW
        )
        self.assertNotIn("uid", reading.to_dict())
        self.assertFalse(hasattr(reading, "uid"))


class FreshnessTest(unittest.TestCase):
    """新鲜度三态：新 / 过期 / 无从测得（``stale=None``，不是 ``False``）。"""

    def test_never_observed_yields_null_staleness_not_false(self) -> None:
        block = freshness_block(None, NOW, 60)
        self.assertIsNone(block["age_seconds"])
        self.assertIsNone(block["stale"])
        self.assertIsNot(block["stale"], False)
        self.assertEqual(block["threshold_seconds"], 60)

    def test_fresh_sample_is_not_stale(self) -> None:
        block = freshness_block(NOW - timedelta(seconds=10), NOW, 60)
        self.assertEqual(block["stale"], False)
        self.assertEqual(block["age_seconds"], 10.0)

    def test_old_sample_is_stale_and_displays_as_stale(self) -> None:
        reading = MetricReading.from_metric(
            "device.gpu.utilization_percent",
            value=42.0,
            source="nvml",
            observed_at=NOW - timedelta(seconds=600),
            now=NOW,
        )
        payload = reading.to_dict()
        self.assertEqual(payload["quality"], QUALITY_MEASURED)
        self.assertTrue(payload["freshness"]["stale"])
        self.assertEqual(payload["display_quality"], DISPLAY_STALE)

    def test_unknown_dominates_staleness(self) -> None:
        """从没测到就谈不上「过期」—— display_quality 必须是 unknown。"""
        reading = MetricReading.unknown(
            "device.gpu.utilization_percent", source="nvml", reason=REASON_NO_SAMPLE, now=NOW
        )
        self.assertEqual(reading.display_quality, QUALITY_UNKNOWN)

    def test_future_timestamp_is_clock_skew_not_freshness(self) -> None:
        reading = MetricReading.from_metric(
            "device.gpu.utilization_percent",
            value=42.0,
            source="nvml",
            observed_at=NOW + timedelta(minutes=10),
            now=NOW,
        )
        payload = reading.to_dict()
        self.assertTrue(payload["freshness"]["clock_skew"])
        self.assertIsNone(payload["freshness"]["stale"])
        # 一个未来时间戳是数据质量问题，不是「非常新」。
        self.assertEqual(payload["display_quality"], QUALITY_UNKNOWN)

    def test_metric_without_a_staleness_policy_is_never_stale(self) -> None:
        """驱动版本号是**结构性事实**，不带过期策略 —— 它不会「变旧」。"""
        reading = TextReading.from_metric(
            "node.gpu.driver.kernel_module_version",
            text="595.58.03",
            source="host-proc",
            observed_at=NOW - timedelta(days=30),
            now=NOW,
        )
        # ``threshold_seconds=None`` 是「本指标没有过期概念」的标记；
        # ``stale`` 因此是 False（不被标记），而不是 None —— 否则 display_quality
        # 会退化成 unknown，把一个**已知**的版本号说成「测不出来」。
        self.assertIsNone(reading.freshness["threshold_seconds"])
        self.assertFalse(reading.freshness["stale"])
        self.assertEqual(reading.display_quality, QUALITY_MEASURED)

    def test_threshold_is_per_metric(self) -> None:
        self.assertEqual(CATALOGUE["device.gpu.utilization_percent"].stale_after_seconds, 60)
        self.assertEqual(CATALOGUE["service.up"].stale_after_seconds, 120)
        self.assertIsNone(
            CATALOGUE["node.gpu.driver.kernel_module_version"].stale_after_seconds
        )

    def test_naive_datetime_is_rejected(self) -> None:
        from datetime import datetime

        with self.assertRaises(InvalidRequest):
            freshness_block(datetime(2026, 10, 15, 12, 0, 0), NOW, 60)

    def test_utc_normalisation(self) -> None:
        from datetime import datetime

        offset = timezone(timedelta(hours=8))
        block = freshness_block(datetime(2026, 10, 15, 19, 59, 30, tzinfo=offset), NOW, 60)
        self.assertEqual(block["age_seconds"], 30.0)


class StateAndTextReadingTest(unittest.TestCase):
    def test_state_reading_unknown_is_none_not_a_state(self) -> None:
        reading = StateReading.from_metric(
            "node.gpu.driver.status",
            state=None,
            allowed_states=DRIVER_STATES,
            source="host-proc",
            quality=QUALITY_UNKNOWN,
            reason=REASON_NO_SAMPLE,
            now=NOW,
        )
        payload = reading.to_dict()
        self.assertIsNone(payload["state"])
        self.assertEqual(payload["allowed_states"], list(DRIVER_STATES))

    def test_state_reading_rejects_an_unlisted_state(self) -> None:
        with self.assertRaises(InvalidRequest):
            StateReading.from_metric(
                "node.gpu.driver.status",
                state="probably_fine",
                allowed_states=DRIVER_STATES,
                source="host-proc",
                observed_at=NOW,
                now=NOW,
            )

    def test_measured_state_rejects_null(self) -> None:
        with self.assertRaises(InvalidRequest):
            StateReading.from_metric(
                "node.gpu.driver.status",
                state=None,
                allowed_states=DRIVER_STATES,
                source="host-proc",
                observed_at=NOW,
                now=NOW,
            )

    def test_text_reading_unknown_is_none(self) -> None:
        reading = TextReading.from_metric(
            "node.gpu.driver.userspace_version",
            text=None,
            source="nvml",
            quality=QUALITY_UNKNOWN,
            reason=REASON_UNSUPPORTED,
            now=NOW,
        )
        payload = reading.to_dict()
        self.assertIsNone(payload["text"])
        self.assertEqual(payload["display_quality"], QUALITY_UNKNOWN)

    def test_text_reading_rejects_blank_text_when_measured(self) -> None:
        with self.assertRaises(InvalidRequest):
            TextReading.from_metric(
                "node.gpu.driver.userspace_version",
                text="   ",
                source="nvml",
                observed_at=NOW,
                now=NOW,
            )


class QualityVocabularyTest(unittest.TestCase):
    def test_partial_is_representable(self) -> None:
        reading = MetricReading.from_metric(
            "device.gpu.utilization_percent",
            value=1.0,
            source="prometheus",
            quality=QUALITY_PARTIAL,
            observed_at=NOW,
            now=NOW,
        )
        self.assertEqual(reading.display_quality, QUALITY_PARTIAL)

    def test_stale_is_not_a_raw_quality_value(self) -> None:
        """``stale`` 只能是 display_quality 的取值，不能是 ``quality`` 的取值。"""
        with self.assertRaises(InvalidRequest):
            MetricReading.from_metric(
                "device.gpu.utilization_percent",
                value=1.0,
                source="prometheus",
                quality=DISPLAY_STALE,
                observed_at=NOW,
                now=NOW,
            )

    def test_non_finite_values_are_rejected(self) -> None:
        for planted in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(planted=planted):
                with self.assertRaises(InvalidRequest):
                    MetricReading.from_metric(
                        "device.gpu.utilization_percent",
                        value=planted,
                        source="prometheus",
                        observed_at=NOW,
                        now=NOW,
                    )

    def test_booleans_are_not_numbers(self) -> None:
        with self.assertRaises(InvalidRequest):
            MetricReading.from_metric(
                "node.gpu.count", value=True, source="host-proc", observed_at=NOW, now=NOW
            )


if __name__ == "__main__":
    unittest.main()
