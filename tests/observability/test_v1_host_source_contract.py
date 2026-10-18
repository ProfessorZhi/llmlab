"""V1-02 / #74 Wave-5：主机投影消费者与失败态的**确定性回归**。

这份文件不引入新行为。它把 Wave-4 已实现、但只被“能用”证明过的几条**口径**
逐条钉死，好让以后任何一次“顺手优化”都会撞上它：

* **基数 ≠ 数值。** 多卡机器上 ``series`` 是「这条查询命中了几张卡」，不是权重；
  它必须原样报出，且**设备序号/标签一个都不许进入产物**。
* **单位是数量级。** ``bytes → mib`` 是 ``1/1048576``，不是 ``1/1000``；一个漏乘
  或错乘会产出一个“看起来合理”的数字 —— 这是最难靠视觉发现的一类错误。
* **``0`` 与 ``null`` 是两件事。** 量到 0 与没量到在同一个键上必须可区分。
* **可达 ≠ 就绪。** 导出器在报数、HTTP 探针回 200，都不构成“模型 ready”。
* **来源是结构性的。** 上游标签改不了产物来源；被篡改的信封会被校验器整份拒绝。
* **请求侧给不出 PromQL / URL。** ``read()`` 无参数，登记表在运行时只读。

全部用例 CPU-only、无网络、无 GPU、无模型。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.observability import (  # noqa: E402
    ConfigRejected,
    SourceUnavailable,
    is_verified_source,
    may_present_as_live,
)
from backend.observability.host_projection import (  # noqa: E402
    DEFAULT_FUTURE_TOLERANCE_S,
    DEFAULT_MAX_SAMPLE_AGE_S,
    HOST_METRIC_UNITS,
    PROJECTION_SOURCE,
    UPSTREAM_REASONS,
    validate_host_summary,
)
from backend.observability.sources import (  # noqa: E402
    STRATA_HOST_METRICS,
    build_host_projection_reader,
)

NOW = datetime(2026, 10, 17, 18, 0, 0, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_SPEC = REPO_ROOT / "docs" / "v1" / "HOST_READONLY_EVIDENCE_CAPTURE.md"

#: 六个真实指标名。这里再钉一次，好让“有人把名字改了”在本文件也失败。
EXPECTED_METRICS = {
    "gpu_utilization_ratio": "llmlab_strata_gpu_utilization_ratio",
    "gpu_temperature_celsius": "llmlab_strata_gpu_temperature_celsius",
    "gpu_power_watts": "llmlab_strata_gpu_power_watts",
    "strata_up": "llmlab_strata_up",
    "gpu_memory_used_mib": "llmlab_strata_gpu_memory_used_bytes",
    "gpu_memory_total_mib": "llmlab_strata_gpu_memory_total_bytes",
}

MIB = 1048576.0


def vector(*values, timestamp=None, labels=None) -> dict:
    """一条 Prometheus 即时向量。``values`` 为裸数值或 ``(标签, 数值)``。"""
    stamp = NOW.timestamp() if timestamp is None else timestamp
    result = []
    for index, entry in enumerate(values):
        if isinstance(entry, tuple):
            series_labels, value = entry
        else:
            series_labels, value = {"instance": f"i{index}"}, entry
        if labels:
            series_labels = {**series_labels, **labels}
        result.append({"metric": series_labels, "value": [stamp, str(value)]})
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


class FakeTransport:
    def __init__(self, responses=None, *, default=None, error=None):
        self.responses = responses or {}
        self.default = default
        self.error = error
        self.calls = []

    def query(self, base_url, query, timeout_seconds):
        self.calls.append((base_url, query, timeout_seconds))
        if self.error is not None:
            raise self.error
        if query in self.responses:
            return self.responses[query]
        if self.default is not None:
            return self.default
        return {"status": "success", "data": {"resultType": "vector", "result": []}}


class FakeHealthProbe:
    def __init__(self, answers=None, *, default=True):
        self.answers = answers or {}
        self.default = default
        self.asked = []

    def probe(self, url, timeout_seconds):
        self.asked.append(url)
        return self.answers.get(url, self.default)


def healthy_transport(**overrides) -> FakeTransport:
    responses = {
        "llmlab_strata_gpu_utilization_ratio": vector(0.42),
        "llmlab_strata_gpu_temperature_celsius": vector(61.0),
        "llmlab_strata_gpu_power_watts": vector(212.0),
        "llmlab_strata_up": vector(1.0),
        "llmlab_strata_gpu_memory_used_bytes": vector(6 * MIB),
        "llmlab_strata_gpu_memory_total_bytes": vector(48 * MIB),
    }
    responses.update(overrides)
    return FakeTransport(responses)


def reader(**kwargs):
    kwargs.setdefault("transport", healthy_transport())
    kwargs.setdefault("health_probe", FakeHealthProbe())
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("clock", lambda: NOW)
    return build_host_projection_reader(**kwargs)


class MetricIdentityTest(unittest.TestCase):
    """六个允许的指标名，逐字。"""

    def test_the_registry_is_exactly_the_six_allowlisted_names(self) -> None:
        self.assertEqual(
            {key: spec.metric for key, spec in STRATA_HOST_METRICS.items()},
            EXPECTED_METRICS,
        )

    def test_every_name_is_a_bare_prometheus_identifier(self) -> None:
        """查询是**裸指标名**，不是表达式 —— 没有选择器、没有运算符、没有函数。"""
        pattern = re.compile(r"^llmlab_strata_[a-z0-9_]+$")
        for key, spec in STRATA_HOST_METRICS.items():
            with self.subTest(key=key):
                self.assertRegex(spec.metric, pattern)
                for forbidden in ("{", "}", "(", ")", "+", "-", "*", "/", " by ", " rate("):
                    self.assertNotIn(forbidden, spec.metric)

    def test_the_allowlist_is_read_only_at_runtime(self) -> None:
        """一个能在运行时被改写的 allowlist 不是 allowlist。"""
        projection = reader().metrics
        with self.assertRaises(TypeError):
            projection["gpu_power_watts"] = None  # type: ignore[index]

    def test_a_caller_cannot_supply_a_query_or_a_url(self) -> None:
        import inspect

        from backend.observability.sources import HostProjectionReader

        self.assertEqual(list(inspect.signature(HostProjectionReader.read).parameters), ["self"])
        for base_url in ("http://10.0.0.5:9090", "http://prometheus.example.com:9090"):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ConfigRejected):
                    build_host_projection_reader(base_url=base_url)


class SampleCardinalityTest(unittest.TestCase):
    """基数（命中几张卡）与数值（取哪一个）是两件事。"""

    def test_series_count_reports_one_two_and_eight_devices(self) -> None:
        for count in (1, 2, 8):
            with self.subTest(devices=count):
                series = [{"gpu": str(i)} for i in range(count)]
                payload = healthy_transport(
                    llmlab_strata_gpu_utilization_ratio=vector(
                        *[(labels, 0.10 + i * 0.05) for i, labels in enumerate(series)]
                    )
                )
                entry = reader(transport=payload).read()["metrics"]["gpu_utilization_ratio"]
                self.assertEqual(entry["state"], "known")
                self.assertEqual(entry["series"], count)
                # max，不是均值、不是求和：第 count 张卡的值最高。
                self.assertAlmostEqual(entry["value"], 0.10 + (count - 1) * 0.05)

    def test_duplicate_values_still_count_as_distinct_series(self) -> None:
        """两张卡恰好同值仍是两张卡 —— 基数不能被“去重”抹掉。"""
        payload = healthy_transport(
            llmlab_strata_gpu_temperature_celsius=vector(("g0", 60.0), ("g1", 60.0))
        )
        entry = reader(transport=payload).read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["series"], 2)
        self.assertEqual(entry["value"], 60.0)

    def test_a_malformed_series_does_not_poison_a_valid_one(self) -> None:
        """一坏一好：好的那条仍然可信，坏的那条只是不计入基数。"""
        payload = healthy_transport(
            llmlab_strata_gpu_power_watts=vector(("bad", "NaN"), ("good", 220.0))
        )
        entry = reader(transport=payload).read()["metrics"]["gpu_power_watts"]
        self.assertEqual(entry["state"], "known")
        self.assertEqual(entry["value"], 220.0)
        self.assertEqual(entry["series"], 1)

    def test_a_stale_series_does_not_contaminate_a_fresh_one(self) -> None:
        """过期样本不许把一份新鲜聚合“抬高/拉低”，也不许计入基数。"""
        old = (NOW - timedelta(seconds=DEFAULT_MAX_SAMPLE_AGE_S + 60)).timestamp()
        payload = healthy_transport()
        payload.responses["llmlab_strata_gpu_temperature_celsius"] = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"gpu": "old"}, "value": [old, "99.0"]},
                    {"metric": {"gpu": "fresh"}, "value": [NOW.timestamp(), "61.0"]},
                ],
            },
        }
        entry = reader(transport=payload).read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["value"], 61.0)
        self.assertEqual(entry["series"], 1)
        self.assertLess(entry["age_s"], DEFAULT_MAX_SAMPLE_AGE_S)

    def test_per_device_cardinality_is_independent_per_metric(self) -> None:
        """used 与 total 各报各的基数 —— 我们**不**按设备配成对。"""
        payload = healthy_transport(
            llmlab_strata_gpu_memory_used_bytes=vector(("g0", 20 * MIB), ("g1", 10 * MIB)),
            llmlab_strata_gpu_memory_total_bytes=vector(
                ("g0", 48 * MIB), ("g1", 24 * MIB), ("g2", 24 * MIB), ("g3", 24 * MIB)
            ),
        )
        metrics = reader(transport=payload).read()["metrics"]
        self.assertEqual(metrics["gpu_memory_used_mib"]["series"], 2)
        self.assertEqual(metrics["gpu_memory_total_mib"]["series"], 4)
        # 各取各的最大值，不配对、不求比值。
        self.assertEqual(metrics["gpu_memory_used_mib"]["value"], 20.0)
        self.assertEqual(metrics["gpu_memory_total_mib"]["value"], 48.0)

    def test_gpu_index_and_device_labels_are_never_emitted(self) -> None:
        """序号/设备名/主机名都是归属线索，产物里一个都不许有。"""
        payload = healthy_transport(
            llmlab_strata_gpu_memory_used_bytes=vector(
                ({"gpu": "3", "device": "nvidia3", "host": "lab-test-host", "uid": "u-1001"}, 6 * MIB)
            )
        )
        entry = reader(transport=payload).read()["metrics"]["gpu_memory_used_mib"]
        blob = json.dumps(entry, sort_keys=True)
        for leaked in ("nvidia3", "lab-test-host", "u-1001", '"gpu"', '"device"', '"host"', '"uid"'):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, blob)
        self.assertEqual(
            set(entry), {"state", "value", "unit", "source", "metric", "series", "age_s", "sample_at"}
        )


class QuantityTest(unittest.TestCase):
    """数量级与 ``0`` / ``null`` 的分界。"""

    def test_bytes_to_mib_is_exact(self) -> None:
        payload = healthy_transport(
            llmlab_strata_gpu_memory_used_bytes=vector(6 * MIB),
            llmlab_strata_gpu_memory_total_bytes=vector(48 * MIB),
        )
        metrics = reader(transport=payload).read()["metrics"]
        self.assertEqual(metrics["gpu_memory_used_mib"]["value"], 6.0)
        self.assertEqual(metrics["gpu_memory_total_mib"]["value"], 48.0)
        self.assertEqual(metrics["gpu_memory_used_mib"]["unit"], "mib")

    def test_a_zero_reading_and_a_missing_reading_are_different_objects(self) -> None:
        """同一个键：``0`` 必须是 ``known`` + ``0``；缺样本必须是 ``unknown`` + ``None``。"""
        zero = reader(
            transport=healthy_transport(llmlab_strata_gpu_power_watts=vector(0.0))
        ).read()["metrics"]["gpu_power_watts"]
        missing = reader(
            transport=healthy_transport(
                llmlab_strata_gpu_power_watts={
                    "status": "success", "data": {"resultType": "vector", "result": []}
                }
            )
        ).read()["metrics"]["gpu_power_watts"]

        self.assertEqual(zero["state"], "known")
        self.assertEqual(zero["value"], 0.0)
        self.assertIsNotNone(zero["value"])

        self.assertEqual(missing["state"], "unknown")
        self.assertIsNone(missing["value"])
        self.assertEqual(missing["reason"], "no-sample")
        self.assertNotEqual(zero, missing)


class FreshnessBoundaryTest(unittest.TestCase):
    """90s TTL 与 15s 未来容差的**边界**。"""

    def test_ninety_seconds_plus_a_hair_is_stale(self) -> None:
        stamp = (NOW - timedelta(seconds=DEFAULT_MAX_SAMPLE_AGE_S + 0.5)).timestamp()
        entry = reader(
            transport=healthy_transport(
                llmlab_strata_gpu_temperature_celsius=vector(70.0, timestamp=stamp)
            )
        ).read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["state"], "unknown")
        self.assertEqual(entry["reason"], "stale")
        self.assertGreaterEqual(entry["age_s"], DEFAULT_MAX_SAMPLE_AGE_S)

    def test_fifteen_seconds_plus_a_hair_in_the_future_is_dropped(self) -> None:
        stamp = (NOW + timedelta(seconds=DEFAULT_FUTURE_TOLERANCE_S + 0.5)).timestamp()
        entry = reader(
            transport=healthy_transport(
                llmlab_strata_gpu_temperature_celsius=vector(70.0, timestamp=stamp)
            )
        ).read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["value"])

    def test_all_samples_stale_degrades_the_whole_envelope(self) -> None:
        old = (NOW - timedelta(seconds=DEFAULT_MAX_SAMPLE_AGE_S + 120)).timestamp()
        transport = FakeTransport(
            {spec.metric: vector(1.0, timestamp=old) for spec in STRATA_HOST_METRICS.values()}
        )
        payload = reader(transport=transport).read()
        self.assertEqual(payload["state"], "unknown")
        for key, entry in payload["metrics"].items():
            with self.subTest(key=key):
                self.assertEqual(entry["state"], "unknown")
                self.assertEqual(entry["reason"], "stale")
                self.assertIsNone(entry["value"])
        self.assertEqual(payload["services"]["strata"]["state"], "unknown")
        verdict = validate_host_summary(payload, now=NOW)
        self.assertTrue(verdict.accepted, verdict.reason)
        self.assertFalse(verdict.any_live)


class FailureStateTest(unittest.TestCase):
    """上游的五种坏法各有独立后果。"""

    def test_a_prometheus_http_error_is_unreachable_not_zero(self) -> None:
        """``urllib`` 把 5xx 折成 ``SourceUnavailable``；与 Node 参考实现一致。"""
        entry = reader(
            transport=FakeTransport(error=SourceUnavailable("http 500"))
        ).read()["metrics"]["gpu_utilization_ratio"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["value"])
        self.assertEqual(entry["reason"], "unreachable")

    def test_a_timeout_is_reported_separately_from_unreachable(self) -> None:
        error = SourceUnavailable("prometheus query failed: timed out")
        error.__cause__ = TimeoutError("timed out")
        entry = reader(transport=FakeTransport(error=error)).read()["metrics"][
            "gpu_utilization_ratio"
        ]
        self.assertEqual(entry["reason"], "timeout")

    def test_nan_infinity_and_non_numeric_are_non_finite(self) -> None:
        for planted in ("NaN", "+Inf", "-Inf", "not-a-number"):
            with self.subTest(planted=planted):
                entry = reader(
                    transport=healthy_transport(
                        llmlab_strata_gpu_temperature_celsius=vector(planted)
                    )
                ).read()["metrics"]["gpu_temperature_celsius"]
                self.assertEqual(entry["state"], "unknown")
                self.assertEqual(entry["reason"], "non-finite-sample")
                self.assertIsNone(entry["value"])

    def test_a_matrix_or_missing_data_is_prometheus_error(self) -> None:
        shapes = (
            {"status": "success", "data": {"result": [{"values": [[1, "1"]]}]}},
            {"status": "success"},
            {"status": "error", "error": "bad_data"},
            "not-json",
        )
        for shape in shapes:
            with self.subTest(shape=str(shape)[:40]):
                entry = reader(transport=FakeTransport(default=shape)).read()["metrics"][
                    "gpu_power_watts"
                ]
                self.assertEqual(entry["state"], "unknown")
                self.assertIsNone(entry["value"])
                self.assertEqual(entry["reason"], "prometheus-error")

    def test_every_reason_code_stays_inside_the_contract_vocabulary(self) -> None:
        import backend.observability.sources as sources

        emitted = {
            value for name, value in vars(sources).items() if name.startswith("PROJECTION_REASON_")
        }
        self.assertTrue(emitted)
        self.assertTrue(emitted <= set(UPSTREAM_REASONS), sorted(emitted - set(UPSTREAM_REASONS)))


class ReachabilityIsNotReadinessTest(unittest.TestCase):
    """导出器在报数、端口回 200 —— 都不是“模型 ready”。"""

    def test_the_projection_has_no_readiness_fact_beyond_the_allowlist(self) -> None:
        payload = reader().read()
        self.assertEqual(set(payload["metrics"]), set(HOST_METRIC_UNITS))
        self.assertEqual(
            set(payload["services"]),
            {"prometheus", "gateway", "grafana", "evalscope", "strata"},
        )
        for key, entry in payload["services"].items():
            with self.subTest(key=key):
                self.assertEqual(set(entry), {"state", "up", "reason"})
        blob = json.dumps(payload).lower()
        for fabricated in ('"ready"', '"model_ready"', '"served_models"', '"inference_ready"'):
            with self.subTest(fabricated=fabricated):
                self.assertNotIn(fabricated, blob)

    def test_exporter_up_and_http_200_still_do_not_claim_model_readiness(self) -> None:
        """``strata_up=1`` 只说“导出器在报数”；200 只说“这个端点答了”。"""
        payload = reader(
            transport=healthy_transport(llmlab_strata_up=vector(1.0)),
            health_probe=FakeHealthProbe(default=True),
        ).read()
        self.assertEqual(payload["services"]["strata"]["up"], True)
        self.assertEqual(payload["services"]["gateway"]["up"], True)
        # 仍然没有任何一个字段把这两件事升级成“模型可调用”。
        self.assertNotIn("ready", json.dumps(payload).lower())

    def test_a_service_answering_500_while_the_port_listens_is_down(self) -> None:
        from backend.observability.sources import HOST_HEALTH_TARGETS

        gateway = HOST_HEALTH_TARGETS["gateway"]
        answers = {url: True for url in HOST_HEALTH_TARGETS.values()}
        answers[gateway] = False
        payload = reader(health_probe=FakeHealthProbe(answers)).read()
        self.assertEqual(payload["services"]["gateway"]["state"], "known")
        self.assertEqual(payload["services"]["gateway"]["up"], False)

    def test_strata_state_comes_only_from_its_metric(self) -> None:
        from backend.observability.sources import HOST_HEALTH_TARGETS

        self.assertNotIn("strata", HOST_HEALTH_TARGETS)
        probe = FakeHealthProbe(default=True)
        payload = reader(
            transport=healthy_transport(llmlab_strata_up=vector(0.0)), health_probe=probe
        ).read()
        self.assertEqual(payload["services"]["strata"]["up"], False)
        self.assertNotIn("strata", " ".join(probe.asked))


class ProvenanceTest(unittest.TestCase):
    """来源是结构性的，不是上游标签或调用方说了算。"""

    def test_the_projection_source_is_verified_and_non_synthetic(self) -> None:
        self.assertTrue(is_verified_source(PROJECTION_SOURCE))
        self.assertTrue(may_present_as_live(PROJECTION_SOURCE, synthetic=False))
        payload = reader().read()
        self.assertEqual(payload["source"], PROJECTION_SOURCE)
        self.assertIs(payload["synthetic"], False)
        self.assertIs(payload["readonly"], True)

    def test_upstream_labels_cannot_launder_the_source(self) -> None:
        payload = healthy_transport(
            llmlab_strata_gpu_power_watts=vector(
                212.0, labels={"source": "gateway-usage-export", "synthetic": "false"}
            )
        )
        entry = reader(transport=payload).read()["metrics"]["gpu_power_watts"]
        self.assertEqual(entry["source"], PROJECTION_SOURCE)
        self.assertNotIn("gateway-usage-export", json.dumps(entry))

    def test_a_tampered_envelope_is_rejected_as_a_whole(self) -> None:
        payload = reader().read()
        tampered = dict(payload)
        tampered["source"] = "prometheus"  # 少一个字符的来源名也不可信
        verdict = validate_host_summary(tampered, now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertFalse(verdict.any_live)
        for key, item in verdict.metrics.items():
            with self.subTest(key=key):
                self.assertFalse(item.live)
                self.assertIsNone(item.value)

    def test_an_empty_payload_is_unknown_not_healthy(self) -> None:
        verdict = validate_host_summary({}, now=NOW)
        self.assertFalse(verdict.accepted)
        self.assertFalse(verdict.any_live)


class OperatorSpecParityTest(unittest.TestCase):
    """操作单与实现必须逐字一致 —— 一份会漂移的采集单比没有更危险。"""

    def setUp(self) -> None:
        self.doc = OPERATOR_SPEC.read_text(encoding="utf-8")

    def test_the_operator_spec_lists_exactly_the_registry_metrics_and_units(self) -> None:
        pattern = re.compile(
            r"\|\s*`([a-z_]+)`\s*\|\s*`(llmlab_strata_[a-z0-9_]+)`\s*\|\s*`([a-z]+)`"
        )
        pinned = {
            match.group(1): (match.group(2), match.group(3))
            for match in pattern.finditer(self.doc)
        }
        expected = {
            key: (spec.metric, spec.unit) for key, spec in STRATA_HOST_METRICS.items()
        }
        self.assertEqual(pinned, expected)

    def test_the_operator_spec_pins_every_health_endpoint_verbatim(self) -> None:
        from backend.observability.sources import (
            HOST_HEALTH_TARGETS,
            PROMETHEUS_LOOPBACK_ORIGIN,
        )

        self.assertIn(PROMETHEUS_LOOPBACK_ORIGIN, self.doc)
        for key, url in HOST_HEALTH_TARGETS.items():
            with self.subTest(key=key):
                self.assertIn(url, self.doc)

    def test_the_operator_spec_forbids_the_strata_health_body(self) -> None:
        """Strata 的 /health 体里有 api_key —— 采集单必须明说它不在允许集合里。"""
        self.assertIn("api_key", self.doc)
        self.assertIn("不在允许集合里", self.doc)
        self.assertIn("llmlab_strata_up", self.doc)

    def test_the_operator_spec_denies_secret_shaped_fields(self) -> None:
        for forbidden in (
            "api_key",
            "Authorization",
            "Cookie",
            "token",
            "password",
            "secret",
            "private_key",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, self.doc)

    def test_the_operator_spec_is_read_only_and_refuses_to_follow_redirects(self) -> None:
        for phrase in ("只读 GET", "不跟随重定向", "不传 `Authorization`", "不加 `-L`"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.doc)

    def test_the_operator_spec_declares_the_blocked_state_and_is_not_live(self) -> None:
        for phrase in ("BLOCKED_ACCESS", "HOST_PENDING", "NOT YET EXECUTED"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.doc)


if __name__ == "__main__":
    unittest.main()
