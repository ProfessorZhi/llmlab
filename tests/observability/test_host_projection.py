"""``backend/observability/host_projection.py`` 的回归。

这个文件把「主机 LIVE 面板凭什么相信一份响应」写成了会红的断言。它同时是一份
**给 UI Lane 的边界条件清单**：``web/js/infra/host-status.js`` 的刷新循环落地时，
可以照着这里的用例逐条对齐（浏览器端无法 import 本模块，但契约必须相同）。

三条纪律贯穿全篇：

1. **信封一坏，全体降级。** 一份 ``synthetic:true`` 或来源不对的响应，里面的
   每一个指标都必须是 UNKNOWN —— 不是「只降级那个字段」。
2. **缺测不是 0。** 任何没有通过 ``sample_at`` 校验的指标，``value`` 必须是
   ``None``；而**确实测得的 0** 必须原样保留为 ``0``。这两件事在数据上必须能分开。
3. **未来时间戳不是「很新」。** 它是时钟偏斜，必须降级。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.observability.host_projection import (  # noqa: E402
    DEFAULT_FUTURE_TOLERANCE_S,
    DEFAULT_MAX_SAMPLE_AGE_S,
    HOST_METRIC_UNITS,
    PROJECTION_SOURCE,
    PROJECTION_VERSION,
    REJECT_BAD_OBSERVED_AT,
    REJECT_BAD_SAMPLE_AT,
    REJECT_BAD_SHAPE,
    REJECT_BAD_STATE_VALUE,
    REJECT_FUTURE_OBSERVED_AT,
    REJECT_FUTURE_SAMPLE_AT,
    REJECT_MISSING_SAMPLE_AT,
    REJECT_NON_FINITE_VALUE,
    REJECT_NOT_READONLY,
    REJECT_STALE_SAMPLE_AT,
    REJECT_SYNTHETIC_SOURCE,
    REJECT_UNIT_MISMATCH,
    REJECT_UNKNOWN_METRIC_KEY,
    REJECT_UNTRUSTED_SOURCE,
    REJECT_WRONG_VERSION,
    STATE_LIVE,
    STATE_UNKNOWN,
    MetricVerdict,
    parse_instant,
    validate_host_summary,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VERIFIED_INFRA_MJS = REPO_ROOT / "web" / "tools" / "verified-infra.mjs"
HOST_STATUS_JS = REPO_ROOT / "web" / "js" / "infra" / "host-status.js"

#: 判定用的固定「当下」。固定时钟让「过期」与「未来」两个概念在测试里可复现。
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def metric_known(
    *,
    value=42.0,
    unit="ratio",
    sample_at: datetime | None = None,
    age_s: int = 5,
    key: str = "gpu_utilization_ratio",
) -> dict:
    """一条形状正确的 ``known`` 指标。``sample_at`` 缺省为「现在 - age_s」秒。"""
    moment = sample_at if sample_at is not None else NOW - timedelta(seconds=age_s)
    return {
        "state": "known",
        "value": value,
        "unit": unit,
        "source": PROJECTION_SOURCE,
        "metric": "llmlab_strata_" + key,
        "series": 1,
        "age_s": age_s,
        "sample_at": _iso(moment),
    }


def valid_metrics() -> dict:
    return {
        "gpu_utilization_ratio": metric_known(value=0.5, unit="ratio"),
        "gpu_temperature_celsius": metric_known(value=61.0, unit="celsius"),
        "gpu_power_watts": metric_known(value=212.0, unit="watts"),
        "strata_up": metric_known(value=1, unit="bool"),
        "gpu_memory_used_mib": metric_known(value=8192.0, unit="mib"),
        "gpu_memory_total_mib": metric_known(value=24576.0, unit="mib"),
    }


def valid_summary(**overrides) -> dict:
    payload = {
        "version": PROJECTION_VERSION,
        "source": PROJECTION_SOURCE,
        "synthetic": False,
        "readonly": True,
        "state": "known",
        "observed_at": _iso(NOW - timedelta(seconds=4)),
        "metrics": valid_metrics(),
        "services": {
            "prometheus": {"state": "known", "up": True, "reason": None},
            "gateway": {"state": "known", "up": False, "reason": None},
            "grafana": {"state": "unknown", "up": None, "reason": "unreachable"},
        },
    }
    payload.update(overrides)
    return payload


class EnvelopeIsTheOnlyDoorTest(unittest.TestCase):
    """信封级信任门槛。任一条不过，整份响应都不可信。"""

    def test_a_clean_summary_is_accepted(self) -> None:
        verdict = validate_host_summary(valid_summary(), now=NOW)
        self.assertTrue(verdict.accepted)
        self.assertIsNone(verdict.reason)
        self.assertEqual(verdict.source, PROJECTION_SOURCE)
        self.assertEqual(verdict.version, PROJECTION_VERSION)

    def test_synthetic_true_poisons_every_metric(self) -> None:
        verdict = validate_host_summary(
            valid_summary(synthetic=True), now=NOW
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_SYNTHETIC_SOURCE)
        self.assertEqual(verdict.live_metric_keys, ())
        for key, item in verdict.metrics.items():
            with self.subTest(key=key):
                self.assertEqual(item.reason, REJECT_SYNTHETIC_SOURCE)
                self.assertIsNone(item.value)

    def test_synthetic_zero_is_not_false(self) -> None:
        # 一个用 0 表示「非合成」的上游，和一个用 0 表示「未知」的上游无法区分。
        verdict = validate_host_summary(valid_summary(synthetic=0), now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_SYNTHETIC_SOURCE)

    def test_synthetic_string_false_is_not_false(self) -> None:
        verdict = validate_host_summary(valid_summary(synthetic="false"), now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_SYNTHETIC_SOURCE)

    def test_a_missing_synthetic_flag_is_a_bad_shape(self) -> None:
        # 缺字段是「这不像一份投影」，不是「它自称合成」—— 两者的排查方向不同。
        payload = valid_summary()
        del payload["synthetic"]
        verdict = validate_host_summary(payload, now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_BAD_SHAPE)

    def test_a_postgres_style_source_is_not_trusted(self) -> None:
        verdict = validate_host_summary(
            valid_summary(source="gateway-usage-export"), now=NOW
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_UNTRUSTED_SOURCE)

    def test_readonly_false_is_rejected(self) -> None:
        verdict = validate_host_summary(valid_summary(readonly=False), now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_NOT_READONLY)

    def test_a_missing_readonly_flag_is_a_bad_shape(self) -> None:
        payload = valid_summary()
        del payload["readonly"]
        verdict = validate_host_summary(payload, now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_BAD_SHAPE)

    def test_a_wrong_contract_version_is_rejected(self) -> None:
        verdict = validate_host_summary(valid_summary(version=2), now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_WRONG_VERSION)

    def test_a_boolean_version_is_not_version_one(self) -> None:
        # ``True == 1`` 在 Python 里成立，但 ``version: true`` 不是 version 1。
        verdict = validate_host_summary(valid_summary(version=True), now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_WRONG_VERSION)

    def test_a_non_mapping_payload_is_bad_shape(self) -> None:
        for payload in ("not json", 42, None, []):
            with self.subTest(payload=payload):
                verdict = validate_host_summary(payload, now=NOW)
                self.assertFalse(verdict.accepted)
                self.assertEqual(verdict.reason, REJECT_BAD_SHAPE)

    def test_the_verdict_never_carries_a_value_when_the_envelope_failed(self) -> None:
        verdict = validate_host_summary(valid_summary(synthetic=True), now=NOW)
        self.assertEqual(
            [item.value for item in verdict.metrics.values() if item.value is not None],
            [],
        )


class ObservedAtTest(unittest.TestCase):
    """整份响应自称的时间也必须可信。"""

    def test_a_missing_observed_at_is_rejected(self) -> None:
        payload = valid_summary()
        del payload["observed_at"]
        verdict = validate_host_summary(payload, now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_BAD_OBSERVED_AT)

    def test_a_naive_observed_at_is_rejected(self) -> None:
        verdict = validate_host_summary(
            valid_summary(observed_at="2026-10-16T11:59:56"), now=NOW
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_BAD_OBSERVED_AT)

    def test_an_observed_at_in_the_future_is_rejected(self) -> None:
        verdict = validate_host_summary(
            valid_summary(observed_at=_iso(NOW + timedelta(minutes=5))), now=NOW
        )
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason, REJECT_FUTURE_OBSERVED_AT)

    def test_a_small_lead_is_tolerated(self) -> None:
        verdict = validate_host_summary(
            valid_summary(observed_at=_iso(NOW + timedelta(seconds=1))), now=NOW
        )
        self.assertTrue(verdict.accepted)


class SampleAtIsThePriceOfLiveTest(unittest.TestCase):
    """``sample_at`` 是 LIVE 的充要条件 —— 四种坏法各自降级。"""

    def _one(self, entry: dict):
        payload = valid_summary(metrics={"gpu_utilization_ratio": entry})
        return validate_host_summary(payload, now=NOW).metrics["gpu_utilization_ratio"]

    def test_a_fresh_sample_is_live_with_its_age(self) -> None:
        item = self._one(metric_known(value=0.42, age_s=9))
        self.assertTrue(item.live)
        self.assertAlmostEqual(item.value, 0.42)
        self.assertAlmostEqual(item.age_seconds, 9.0)
        self.assertEqual(item.unit, "ratio")
        self.assertIsNone(item.reason)

    def test_a_measured_zero_is_live_and_stays_zero(self) -> None:
        # 这条是整个契约的要点：**确实测得的 0** 与「没测到」不是一回事。
        item = self._one(metric_known(value=0, unit="ratio"))
        self.assertTrue(item.live)
        self.assertEqual(item.value, 0.0)

    def test_a_missing_sample_at_is_unknown_not_live(self) -> None:
        entry = metric_known()
        del entry["sample_at"]
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertIsNone(item.value)
        self.assertEqual(item.reason, REJECT_MISSING_SAMPLE_AT)

    def test_a_blank_sample_at_is_treated_as_missing(self) -> None:
        entry = metric_known()
        entry["sample_at"] = "   "
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_MISSING_SAMPLE_AT)

    def test_a_naive_sample_at_is_rejected(self) -> None:
        entry = metric_known()
        entry["sample_at"] = "2026-10-16T11:59:56"
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_BAD_SAMPLE_AT)

    def test_an_unparseable_sample_at_is_rejected(self) -> None:
        entry = metric_known()
        entry["sample_at"] = "yesterday"
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_BAD_SAMPLE_AT)

    def test_a_future_sample_at_is_clock_skew_not_freshness(self) -> None:
        entry = metric_known(sample_at=NOW + timedelta(seconds=600))
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertIsNone(item.value)
        self.assertEqual(item.reason, REJECT_FUTURE_SAMPLE_AT)

    def test_a_sample_leading_within_tolerance_is_live(self) -> None:
        entry = metric_known(sample_at=NOW + timedelta(seconds=DEFAULT_FUTURE_TOLERANCE_S - 1))
        item = self._one(entry)
        self.assertTrue(item.live)

    def test_an_expired_sample_is_unknown_and_reports_its_age(self) -> None:
        entry = metric_known(age_s=DEFAULT_MAX_SAMPLE_AGE_S + 30)
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertIsNone(item.value)
        self.assertEqual(item.reason, REJECT_STALE_SAMPLE_AT)
        self.assertGreater(item.age_seconds, DEFAULT_MAX_SAMPLE_AGE_S)

    def test_a_sample_at_the_age_boundary_is_still_live(self) -> None:
        entry = metric_known(age_s=DEFAULT_MAX_SAMPLE_AGE_S - 1)
        item = self._one(entry)
        self.assertTrue(item.live)

    def test_the_age_threshold_is_configurable(self) -> None:
        entry = metric_known(age_s=30)
        verdict = validate_host_summary(
            valid_summary(metrics={"gpu_utilization_ratio": entry}),
            now=NOW,
            max_sample_age_s=10,
        )
        self.assertFalse(verdict.metrics["gpu_utilization_ratio"].live)


class ValueAndUnitDisciplineTest(unittest.TestCase):
    def _one(self, entry: dict, key: str = "gpu_utilization_ratio"):
        payload = valid_summary(metrics={key: entry})
        return validate_host_summary(payload, now=NOW).metrics[key]

    def test_a_null_value_on_a_known_metric_is_not_live(self) -> None:
        entry = metric_known()
        entry["value"] = None
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_NON_FINITE_VALUE)

    def test_a_nan_value_is_not_live(self) -> None:
        entry = metric_known()
        entry["value"] = float("nan")
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_NON_FINITE_VALUE)

    def test_an_infinite_value_is_not_live(self) -> None:
        entry = metric_known()
        entry["value"] = float("inf")
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_NON_FINITE_VALUE)

    def test_a_boolean_value_is_not_a_number(self) -> None:
        entry = metric_known()
        entry["value"] = True
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_NON_FINITE_VALUE)

    def test_a_numeric_string_is_not_a_number(self) -> None:
        entry = metric_known()
        entry["value"] = "42"
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_NON_FINITE_VALUE)

    def test_a_unit_mismatch_is_a_data_fault_not_a_style_choice(self) -> None:
        entry = metric_known(value=61.0, unit="ratio")  # 摄氏度被印成百分比
        item = self._one(entry, key="gpu_temperature_celsius")
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_UNIT_MISMATCH)
        self.assertEqual(item.unit, "celsius")

    def test_an_absent_unit_is_a_mismatch(self) -> None:
        entry = metric_known()
        del entry["unit"]
        item = self._one(entry)
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_UNIT_MISMATCH)

    def test_a_fractional_boolean_state_is_not_interpreted(self) -> None:
        # 0.4 的 ``strata_up`` 既不是 up 也不是 down —— 按阈值解释就是在编。
        entry = metric_known(value=0.4, unit="bool")
        item = self._one(entry, key="strata_up")
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_BAD_STATE_VALUE)

    def test_a_boolean_state_must_be_zero_or_one(self) -> None:
        for value in (0, 1):
            with self.subTest(value=value):
                item = self._one(metric_known(value=value, unit="bool"), key="strata_up")
                self.assertTrue(item.live)
                self.assertEqual(item.value, float(value))


class UnknownEntriesTest(unittest.TestCase):
    def _one(self, entry: dict, key: str = "gpu_utilization_ratio"):
        payload = valid_summary(metrics={key: entry})
        return validate_host_summary(payload, now=NOW).metrics[key]

    def test_an_upstream_unknown_passes_its_reason_through(self) -> None:
        item = self._one({"state": "unknown", "value": None, "unit": "ratio", "reason": "no-sample"})
        self.assertFalse(item.live)
        self.assertIsNone(item.value)
        self.assertEqual(item.reason, "no-sample")

    def test_an_unrecognised_reason_collapses_to_unknown(self) -> None:
        # 一个自由文本「原因」会变成一条不受控的渲染通道。
        item = self._one(
            {"state": "unknown", "value": None, "unit": "ratio", "reason": "<img src=x>"}
        )
        self.assertFalse(item.live)
        self.assertEqual(item.reason, "unknown")

    def test_an_unknown_entry_carrying_a_number_is_still_not_live(self) -> None:
        item = self._one(
            {"state": "unknown", "value": 123.0, "unit": "ratio", "reason": "no-sample"}
        )
        self.assertFalse(item.live)
        self.assertIsNone(item.value)

    def test_a_missing_metric_key_is_unknown_not_absent(self) -> None:
        # 契约要求「始终是完整形状」：缺键意味着我们不知道投影端跑的是哪个版本。
        payload = valid_summary(metrics={})
        verdict = validate_host_summary(payload, now=NOW)
        self.assertEqual(set(verdict.metrics), set(HOST_METRIC_UNITS))
        self.assertEqual(verdict.live_metric_keys, ())
        self.assertEqual(
            verdict.metrics["gpu_utilization_ratio"].reason, REJECT_UNKNOWN_METRIC_KEY
        )

    def test_a_non_mapping_metric_entry_is_bad_shape(self) -> None:
        item = self._one("nope")
        self.assertFalse(item.live)
        self.assertEqual(item.reason, REJECT_BAD_SHAPE)

    def test_an_absent_metrics_block_is_bad_shape(self) -> None:
        payload = valid_summary()
        del payload["metrics"]
        verdict = validate_host_summary(payload, now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertEqual(set(verdict.metrics), set(HOST_METRIC_UNITS))
        self.assertEqual(verdict.live_metric_keys, ())


class ServiceVerdictTest(unittest.TestCase):
    def test_a_known_up_service_is_live(self) -> None:
        verdict = validate_host_summary(valid_summary(), now=NOW)
        self.assertTrue(verdict.services["prometheus"].live)
        self.assertTrue(verdict.services["prometheus"].up)

    def test_a_known_down_service_is_still_live(self) -> None:
        # 「服务确实挂了」是一个**测得**的事实，不是 UNKNOWN。
        verdict = validate_host_summary(valid_summary(), now=NOW)
        self.assertTrue(verdict.services["gateway"].live)
        self.assertFalse(verdict.services["gateway"].up)

    def test_a_string_up_is_not_a_boolean(self) -> None:
        # 字符串 "false" 是 truthy 的：把它当可达性会把「挂了」静默读成「在」。
        payload = valid_summary(
            services={"gateway": {"state": "known", "up": "false", "reason": None}}
        )
        verdict = validate_host_summary(payload, now=NOW)
        self.assertFalse(verdict.services["gateway"].live)
        self.assertIsNone(verdict.services["gateway"].up)

    def test_an_unreachable_service_is_unknown(self) -> None:
        verdict = validate_host_summary(valid_summary(), now=NOW)
        self.assertFalse(verdict.services["grafana"].live)
        self.assertEqual(verdict.services["grafana"].reason, "unreachable")


class VerdictInvariantTest(unittest.TestCase):
    """数据类自身不许表达「LIVE 却没有数字」这种自相矛盾。"""

    def test_a_live_verdict_without_a_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MetricVerdict(
                key="gpu_utilization_ratio",
                live=True,
                value=None,
                unit="ratio",
                sample_at=None,
                age_seconds=None,
                reason=None,
                source=PROJECTION_SOURCE,
            )

    def test_a_non_live_verdict_with_a_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MetricVerdict(
                key="gpu_utilization_ratio",
                live=False,
                value=1.0,
                unit="ratio",
                sample_at=None,
                age_seconds=None,
                reason="no-sample",
                source=PROJECTION_SOURCE,
            )

    def test_parse_instant_rejects_naive_and_accepts_aware(self) -> None:
        self.assertIsNone(parse_instant("2026-10-16T12:00:00"))
        self.assertIsNotNone(parse_instant("2026-10-16T12:00:00Z"))
        self.assertIsNotNone(parse_instant("2026-10-16T20:00:00+08:00"))
        self.assertIsNone(parse_instant(None))
        self.assertIsNone(parse_instant(""))

    def test_a_naive_now_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_host_summary(valid_summary(), now=datetime(2026, 10, 16, 12, 0, 0))

    def test_the_verdict_is_json_serialisable(self) -> None:
        verdict = validate_host_summary(valid_summary(), now=NOW)
        self.assertIsInstance(json.dumps(verdict.to_dict()), str)


class ProjectionConstantParityTest(unittest.TestCase):
    """投影端、浏览器端与本模块必须说同一件事。

    这三处任意一处单独漂移，都会得到一个「看起来在跑、其实在撒谎」的面板。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.infra = VERIFIED_INFRA_MJS.read_text(encoding="utf-8")
        cls.host_status = HOST_STATUS_JS.read_text(encoding="utf-8")

    def test_the_metric_keys_match_the_node_projection(self) -> None:
        # 只从 ``METRICS`` 数组里抠键名 —— ``HEALTH_TARGETS`` 也长得一样（``{ key: … }``），
        # 所以先把数组切出来，否则断言会把服务探测目标误当成指标。
        block = self._metrics_block()
        keys = set(re.findall(r"\{\s*key:\s*'([a-z0-9_]+)'", block))
        self.assertEqual(keys, set(HOST_METRIC_UNITS))

    def test_the_units_match_the_node_projection(self) -> None:
        block = self._metrics_block()
        pairs = re.findall(
            r"\{\s*key:\s*'([a-z0-9_]+)'[^}]*?unit:\s*'([a-z]+)'", block
        )
        found = {key: unit for key, unit in pairs}
        self.assertEqual(found, dict(HOST_METRIC_UNITS))

    @classmethod
    def _metrics_block(cls) -> str:
        start = cls.infra.index("export const METRICS = Object.freeze([")
        end = cls.infra.index("]);", start)
        return cls.infra[start:end]

    def test_the_node_projection_declares_the_same_source_and_version(self) -> None:
        self.assertIn(f"SOURCE = '{PROJECTION_SOURCE}'", self.infra)
        self.assertIn(f"CONTRACT_VERSION = {PROJECTION_VERSION}", self.infra)

    def test_the_browser_gate_requires_the_same_source_and_version(self) -> None:
        # The browser now uses a named version constant and rejects mismatches
        # strictly. A literal "version === 1" would miss the actual guard and
        # incorrectly fail on the correct "version !== PROJECTION_VERSION" shape.
        self.assertIn(
            f"export const PROJECTION_SOURCE = '{PROJECTION_SOURCE}';",
            self.host_status,
        )
        self.assertIn(
            f"export const PROJECTION_VERSION = {PROJECTION_VERSION};",
            self.host_status,
        )
        self.assertRegex(self.host_status, r"\bsource\s*!==\s*PROJECTION_SOURCE\b")
        self.assertRegex(self.host_status, r"\bversion\s*!==\s*PROJECTION_VERSION\b")
        self.assertRegex(self.host_status, r"\bsynthetic\s*!==\s*false\b")

    def test_the_sample_age_threshold_matches_the_node_projection(self) -> None:
        self.assertIn(
            f"MAX_SAMPLE_AGE_S = {DEFAULT_MAX_SAMPLE_AGE_S}", self.infra
        )

    def test_the_state_vocabulary_is_shared(self) -> None:
        self.assertEqual(STATE_LIVE, "live")
        self.assertEqual(STATE_UNKNOWN, "unknown")


if __name__ == "__main__":
    unittest.main()
