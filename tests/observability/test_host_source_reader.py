"""主机投影的**生产采样 Reader**（Lane #74）。

背景：``/api/infra/summary`` 这份投影此前只有 Node 侧的产生方
（``web/tools/verified-infra.mjs``），Python 服务端只有**校验方**
（``host_projection.py``）。PR #85 的 ``HostSummaryStack(read=...)`` 需要一个
**零参数**的 ``read()`` 来注入真实来源，而仓库里还没有那个来源 —— 本文件钉住的
就是它的契约。

被钉死的边界（每条都有对应的负向用例）：

* **请求侧给不出 PromQL、也给不出 URL。** ``read()`` 无参数；查询名来自代码里的
  ``STRATA_HOST_METRICS``；端点构造期即校验环回 / 白名单。
* **真实指标名。** 六个 ``llmlab_strata_*`` 名字与 ``verified-infra.mjs`` **逐字**
  一致 —— 两套产生方一旦漂移，就会出现「同一台机器两个真相」。
* **多卡不求和。** 归约一律 ``max``（``sum`` 在构造期即被拒绝），且 ``max`` 基下
  ``used <= total`` 恒成立。
* **不看标签。** 序列标签里的 ``gpu`` / ``uid`` 一概不读、不透传 —— 不拼造归属。
* **null ≠ 0。** 无样本 / 非有限 / 过期 / 未来 一律 ``unknown`` + ``value=None``；
  只有**确实量到的 0** 才是 ``0``。
* **服务 TCP ≠ ready。** 可达性取 **HTTP 状态**，不取「连得上」；Strata 的状态取
  自 ``llmlab_strata_up`` 这条指标，**从不**去取它那个带 ``api_key`` 的 ``/health``。
* **产物必须过服务端校验器。** 本 Reader 的输出直接喂给
  ``validate_host_summary``，且它必须被接受。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.observability import (  # noqa: E402
    ConfigRejected,
    InvalidRequest,
    SourceUnavailable,
)
from backend.observability.host_projection import (  # noqa: E402
    DEFAULT_FUTURE_TOLERANCE_S,
    DEFAULT_MAX_SAMPLE_AGE_S,
    HOST_METRIC_UNITS,
    PROJECTION_PATH,
    PROJECTION_SOURCE,
    PROJECTION_VERSION,
    UPSTREAM_REASONS,
    validate_host_summary,
)

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = NOW.timestamp() * 1000.0

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIED_INFRA_MJS = REPO_ROOT / "web" / "tools" / "verified-infra.mjs"


def vector(*values, timestamp: float | None = None, labels: dict | None = None) -> dict:
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
    """按指标名返回预置载荷，并记录被问过的查询。"""

    def __init__(self, responses=None, *, default=None, error: Exception | None = None) -> None:
        self.responses = responses or {}
        self.default = default
        self.error = error
        self.calls: list[tuple] = []

    def query(self, base_url: str, query: str, timeout_seconds: float) -> dict:
        self.calls.append((base_url, query, timeout_seconds))
        if self.error is not None:
            raise self.error
        if query in self.responses:
            return self.responses[query]
        if self.default is not None:
            return self.default
        return {"status": "success", "data": {"resultType": "vector", "result": []}}


class FakeHealthProbe:
    """固定回答的服务探针。记录被问过的 URL。"""

    def __init__(self, answers=None, *, default: bool | None = True) -> None:
        self.answers = answers or {}
        self.default = default
        self.asked: list[str] = []

    def probe(self, url: str, timeout_seconds: float) -> bool | None:
        self.asked.append(url)
        return self.answers.get(url, self.default)


def _full_transport(**overrides) -> FakeTransport:
    """一份「六个指标都有、都是新鲜单卡」的传输。"""
    responses = {
        "llmlab_strata_gpu_utilization_ratio": vector(0.42),
        "llmlab_strata_gpu_temperature_celsius": vector(61.0),
        "llmlab_strata_gpu_power_watts": vector(212.0),
        "llmlab_strata_up": vector(1.0),
        "llmlab_strata_gpu_memory_used_bytes": vector(6 * 1048576.0),
        "llmlab_strata_gpu_memory_total_bytes": vector(48 * 1048576.0),
    }
    responses.update(overrides)
    return FakeTransport(responses)


def _reader(**kwargs):
    from backend.observability.sources import build_host_projection_reader

    kwargs.setdefault("transport", _full_transport())
    kwargs.setdefault("health_probe", FakeHealthProbe())
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("clock", lambda: NOW)
    return build_host_projection_reader(**kwargs)


class RegistryParityTest(unittest.TestCase):
    """登记表与共享契约、以及 Node 产生方的一致性。"""

    def test_the_registry_covers_exactly_the_projection_keys(self) -> None:
        from backend.observability.sources import STRATA_HOST_METRICS

        self.assertEqual(set(STRATA_HOST_METRICS), set(HOST_METRIC_UNITS))

    def test_the_registry_uses_the_metric_names_pinned_in_the_node_projector(self) -> None:
        """两个产生方必须问**同一批**指标名。

        这条断言读的是 ``verified-infra.mjs`` 的源文本，而不是另一份手抄常量 ——
        手抄的那份正是会漂移的那份。
        """
        from backend.observability.sources import STRATA_HOST_METRICS

        source = VERIFIED_INFRA_MJS.read_text(encoding="utf-8")
        pattern = re.compile(
            r"\{\s*key:\s*'([a-z_]+)',\s*metric:\s*'([a-z0-9_]+)',\s*"
            r"unit:\s*'([a-z]+)',\s*scale:\s*([^,]+),\s*agg:\s*'([a-z]+)'\s*\}"
        )
        pinned = {match.group(1): match.group(2) for match in pattern.finditer(source)}
        self.assertTrue(pinned, "could not parse METRICS out of verified-infra.mjs")
        self.assertEqual(
            {key: spec.metric for key, spec in STRATA_HOST_METRICS.items()}, pinned
        )

    def test_units_agree_with_the_shared_host_metric_contract(self) -> None:
        from backend.observability.sources import STRATA_HOST_METRICS

        for key, spec in STRATA_HOST_METRICS.items():
            with self.subTest(key=key):
                self.assertEqual(spec.unit, HOST_METRIC_UNITS[key])

    def test_every_reduction_is_max_so_gpus_are_never_summed(self) -> None:
        from backend.observability.sources import STRATA_HOST_METRICS

        for key, spec in STRATA_HOST_METRICS.items():
            with self.subTest(key=key):
                self.assertEqual(spec.aggregation, "max")

    def test_a_sum_reduction_is_refused_at_construction(self) -> None:
        """求和会把「最高那张卡」变成一个**没有任何一张卡**有过的机器级数字。"""
        from backend.observability.sources import HostMetricSource

        with self.assertRaises(InvalidRequest):
            HostMetricSource(
                key="gpu_memory_used_mib",
                metric="llmlab_strata_gpu_memory_used_bytes",
                unit="mib",
                scale=1 / 1048576,
                aggregation="sum",
            )

    def test_a_unit_that_disagrees_with_the_contract_is_refused(self) -> None:
        from backend.observability.sources import HostMetricSource

        with self.assertRaises(InvalidRequest):
            HostMetricSource(
                key="gpu_temperature_celsius",
                metric="llmlab_strata_gpu_temperature_celsius",
                unit="ratio",
                scale=1.0,
                aggregation="max",
            )

    def test_an_unknown_projection_key_is_refused(self) -> None:
        from backend.observability.sources import HostMetricSource

        with self.assertRaises(InvalidRequest):
            HostMetricSource(
                key="gpu_power_microwatts",
                metric="llmlab_strata_gpu_power_watts",
                unit="watts",
                scale=1.0,
                aggregation="max",
            )

    def test_every_emitted_reason_is_in_the_validators_vocabulary(self) -> None:
        from backend.observability import sources

        emitted = {
            value
            for name, value in vars(sources).items()
            if name.startswith("PROJECTION_REASON_")
        }
        self.assertTrue(emitted)
        self.assertTrue(
            emitted <= set(UPSTREAM_REASONS),
            f"reasons outside the contract: {sorted(emitted - set(UPSTREAM_REASONS))}",
        )


class NoClientQueryTest(unittest.TestCase):
    """请求侧结构上给不出查询串、也给不出 URL。"""

    def test_read_takes_no_parameters(self) -> None:
        from backend.observability.sources import HostProjectionReader

        signature = inspect.signature(HostProjectionReader.read)
        self.assertEqual(list(signature.parameters), ["self"])

    def test_the_seam_callable_takes_no_parameters(self) -> None:
        from backend.observability.sources import host_summary_reader

        seam = host_summary_reader(
            transport=_full_transport(), health_probe=FakeHealthProbe(), enabled=True,
            clock=lambda: NOW,
        )
        self.assertTrue(callable(seam))
        self.assertEqual(list(inspect.signature(seam).parameters), [])

    def test_the_transport_only_ever_sees_registered_metric_names(self) -> None:
        from backend.observability.sources import STRATA_HOST_METRICS

        transport = _full_transport()
        reader = _reader(transport=transport)
        reader.read()
        asked = {query for _base, query, _timeout in transport.calls}
        self.assertTrue(asked)
        self.assertEqual(asked, {spec.metric for spec in STRATA_HOST_METRICS.values()})

    def test_a_non_loopback_endpoint_cannot_even_be_constructed(self) -> None:
        from backend.observability.sources import build_host_projection_reader

        for base_url in (
            "http://prometheus.example.com:9090",
            "http://10.0.0.5:9090",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ConfigRejected):
                    build_host_projection_reader(base_url=base_url)

    def test_the_default_endpoint_is_loopback(self) -> None:
        from backend.observability.sources import PROMETHEUS_LOOPBACK_ORIGIN

        self.assertEqual(PROMETHEUS_LOOPBACK_ORIGIN, "http://127.0.0.1:9090")


class ProjectionEnvelopeTest(unittest.TestCase):
    """信封形状，以及「产物必须过服务端校验器」。"""

    def test_a_healthy_reader_produces_a_projection_the_validator_accepts(self) -> None:
        payload = _reader().read()
        verdict = validate_host_summary(payload, now=NOW)
        self.assertTrue(verdict.accepted, verdict.reason)
        self.assertEqual(verdict.source, PROJECTION_SOURCE)
        self.assertEqual(verdict.version, PROJECTION_VERSION)
        self.assertEqual(payload["source"], PROJECTION_SOURCE)
        self.assertIs(payload["synthetic"], False)
        self.assertIs(payload["readonly"], True)
        self.assertEqual(payload["version"], PROJECTION_VERSION)
        self.assertEqual(payload["state"], "known")

    def test_every_contract_key_is_present_even_when_nothing_was_measured(self) -> None:
        reader = _reader(transport=FakeTransport())
        payload = reader.read()
        self.assertEqual(set(payload["metrics"]), set(HOST_METRIC_UNITS))
        self.assertEqual(payload["state"], "unknown")

    def test_the_envelope_carries_the_projection_path_and_timestamps(self) -> None:
        payload = _reader().read()
        self.assertIn("observed_at", payload)
        self.assertEqual(payload["metrics"]["gpu_power_watts"]["source"], PROJECTION_SOURCE)
        self.assertIn("sample_at", payload["metrics"]["gpu_power_watts"])
        self.assertIn("age_s", payload["metrics"]["gpu_power_watts"])
        self.assertEqual(PROJECTION_PATH, "/api/infra/summary")

    def test_a_disabled_reader_reads_nothing_so_the_bff_reports_a_detached_source(self) -> None:
        """没装配来源就回 ``None``（BFF ⇒ 503「未接线」），而**不是**一份全 UNKNOWN 的 200。

        全 UNKNOWN 的 200 与「本部署没有这条来源」形状几乎一样，但对运维是两个完全
        不同的结论：前者要去查投影端，后者要去查装配。让它们长得一样，是让一次接线
        遗漏伪装成一次数据故障。
        """
        transport = _full_transport()
        reader = _reader(transport=transport, enabled=False)
        self.assertIsNone(reader.read())
        self.assertEqual(transport.calls, [], "a disabled reader must not touch the network")
        self.assertEqual(transport.calls, [])

    def test_read_never_raises_on_an_unreachable_upstream(self) -> None:
        reader = _reader(transport=FakeTransport(error=SourceUnavailable("boom")))
        payload = reader.read()
        self.assertEqual(payload["state"], "unknown")
        self.assertEqual(payload["metrics"]["gpu_utilization_ratio"]["reason"], "unreachable")
        verdict = validate_host_summary(payload, now=NOW)
        self.assertTrue(verdict.accepted, verdict.reason)
        self.assertFalse(verdict.any_live)

    def test_a_timeout_is_reported_as_timeout_not_unreachable(self) -> None:
        """`urllib` 把超时包成 ``SourceUnavailable``；它的 cause 才认得出这是超时。

        两者对运维的指向不同（配额/慢查询 vs 进程不在），所以不合并成一个原因码。
        """
        error = SourceUnavailable("prometheus query failed: timed out")
        error.__cause__ = TimeoutError("timed out")
        reader = _reader(transport=FakeTransport(error=error))
        payload = reader.read()
        self.assertEqual(payload["metrics"]["gpu_utilization_ratio"]["reason"], "timeout")

    def test_a_malformed_payload_is_a_prometheus_error_not_a_value(self) -> None:
        bad = {"status": "error", "error": "bad_data"}
        reader = _reader(transport=FakeTransport(default=bad))
        payload = reader.read()
        entry = payload["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["state"], "unknown")
        self.assertEqual(entry["reason"], "prometheus-error")
        self.assertIsNone(entry["value"])


class ValueSemanticsTest(unittest.TestCase):
    """单位、换算，以及「null 不是 0」。"""

    def test_bytes_are_scaled_into_mib(self) -> None:
        reader = _reader(
            transport=_full_transport(
                llmlab_strata_gpu_memory_used_bytes=vector(3 * 1048576.0 * 2),
            )
        )
        entry = reader.read()["metrics"]["gpu_memory_used_mib"]
        self.assertEqual(entry["state"], "known")
        self.assertEqual(entry["unit"], "mib")
        self.assertAlmostEqual(entry["value"], 6.0)

    def test_a_measured_zero_is_a_live_zero_not_unknown(self) -> None:
        """确实量到的 0 是合法读数 —— 与「没量到」必须长得不一样。"""
        reader = _reader(
            transport=_full_transport(llmlab_strata_gpu_utilization_ratio=vector(0.0))
        )
        entry = reader.read()["metrics"]["gpu_utilization_ratio"]
        self.assertEqual(entry["state"], "known")
        self.assertEqual(entry["value"], 0.0)

    def test_no_sample_is_unknown_and_carries_no_number(self) -> None:
        reader = _reader(
            transport=_full_transport(
                llmlab_strata_gpu_power_watts={"status": "success",
                                               "data": {"resultType": "vector", "result": []}}
            )
        )
        entry = reader.read()["metrics"]["gpu_power_watts"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["value"])
        self.assertEqual(entry["reason"], "no-sample")
        self.assertIsNot(entry["value"], 0)

    def test_nan_and_infinity_are_not_measurements(self) -> None:
        for planted in ("NaN", "+Inf", "-Inf", "inf"):
            with self.subTest(planted=planted):
                reader = _reader(
                    transport=_full_transport(
                        llmlab_strata_gpu_temperature_celsius=vector(planted)
                    )
                )
                entry = reader.read()["metrics"]["gpu_temperature_celsius"]
                self.assertEqual(entry["state"], "unknown")
                self.assertIsNone(entry["value"])
                self.assertEqual(entry["reason"], "non-finite-sample")

    def test_a_stale_sample_is_unknown_with_a_stale_reason(self) -> None:
        old = (NOW - timedelta(seconds=DEFAULT_MAX_SAMPLE_AGE_S + 30)).timestamp()
        reader = _reader(
            transport=_full_transport(llmlab_strata_gpu_temperature_celsius=vector(70.0, timestamp=old))
        )
        entry = reader.read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["value"])
        self.assertEqual(entry["reason"], "stale")
        self.assertGreaterEqual(entry["age_s"], DEFAULT_MAX_SAMPLE_AGE_S)

    def test_a_sample_exactly_at_the_age_limit_is_still_live(self) -> None:
        edge = (NOW - timedelta(seconds=DEFAULT_MAX_SAMPLE_AGE_S)).timestamp()
        reader = _reader(
            transport=_full_transport(llmlab_strata_gpu_temperature_celsius=vector(70.0, timestamp=edge))
        )
        self.assertEqual(
            reader.read()["metrics"]["gpu_temperature_celsius"]["state"], "known"
        )

    def test_a_sample_beyond_the_future_tolerance_is_not_live(self) -> None:
        ahead = (NOW + timedelta(seconds=DEFAULT_FUTURE_TOLERANCE_S + 30)).timestamp()
        reader = _reader(
            transport=_full_transport(llmlab_strata_gpu_temperature_celsius=vector(70.0, timestamp=ahead))
        )
        entry = reader.read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["value"])

    def test_a_sample_within_the_future_tolerance_is_live_but_not_negative_valued(self) -> None:
        ahead = (NOW + timedelta(seconds=DEFAULT_FUTURE_TOLERANCE_S - 5)).timestamp()
        reader = _reader(
            transport=_full_transport(llmlab_strata_gpu_temperature_celsius=vector(70.0, timestamp=ahead))
        )
        entry = reader.read()["metrics"]["gpu_temperature_celsius"]
        self.assertEqual(entry["state"], "known")
        self.assertGreaterEqual(entry["age_s"], 0)

    def test_a_negative_sample_is_dropped_rather_than_reported(self) -> None:
        reader = _reader(
            transport=_full_transport(llmlab_strata_gpu_power_watts=vector(-5.0))
        )
        entry = reader.read()["metrics"]["gpu_power_watts"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["value"])


class MultiGpuSemanticsTest(unittest.TestCase):
    """多卡：取最大而不是求和，且不拼造归属。"""

    def test_several_gpus_collapse_by_max_and_report_the_series_count(self) -> None:
        reader = _reader(
            transport=_full_transport(
                llmlab_strata_gpu_utilization_ratio=vector(("gpu0", 0.10), ("gpu1", 0.90))
            )
        )
        entry = reader.read()["metrics"]["gpu_utilization_ratio"]
        self.assertEqual(entry["state"], "known")
        self.assertAlmostEqual(entry["value"], 0.90)
        self.assertEqual(entry["series"], 2)

    def test_max_basis_never_yields_used_above_total(self) -> None:
        reader = _reader(
            transport=_full_transport(
                llmlab_strata_gpu_memory_used_bytes=vector(
                    ("g0", 40 * 1048576.0), ("g1", 2 * 1048576.0)
                ),
                llmlab_strata_gpu_memory_total_bytes=vector(
                    ("g0", 48 * 1048576.0), ("g1", 24 * 1048576.0)
                ),
            )
        )
        metrics = reader.read()["metrics"]
        used = metrics["gpu_memory_used_mib"]["value"]
        total = metrics["gpu_memory_total_mib"]["value"]
        self.assertEqual(used, 40.0)
        self.assertEqual(total, 48.0)
        self.assertLessEqual(used, total)

    def test_series_labels_are_never_read_so_no_attribution_is_fabricated(self) -> None:
        """标签里可以有任何标识（用户、卡号、模型）—— 它们不得出现在产物里。"""
        reader = _reader(
            transport=_full_transport(
                llmlab_strata_gpu_memory_used_bytes=vector(
                    ({"gpu": "3"}, 6 * 1048576.0),
                    labels={"uid": "u-1001", "model": "A6000", "host": "dell"},
                )
            )
        )
        entry = reader.read()["metrics"]["gpu_memory_used_mib"]
        blob = json.dumps(entry)
        for leaked in ("u-1001", "A6000", "dell"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, blob)
        self.assertNotIn("gpu", entry)


class ServiceReadinessTest(unittest.TestCase):
    """服务健康：固定来源，且「连得上」不等于「就绪」。"""

    def test_strata_state_comes_from_the_metric_never_from_its_health_body(self) -> None:
        from backend.observability.sources import HOST_HEALTH_TARGETS

        self.assertNotIn("strata", HOST_HEALTH_TARGETS)
        probe = FakeHealthProbe(default=False)
        reader = _reader(
            transport=_full_transport(llmlab_strata_up=vector(1.0)), health_probe=probe
        )
        payload = reader.read()
        self.assertEqual(payload["services"]["strata"]["up"], True)
        self.assertNotIn("strata", " ".join(probe.asked))

    def test_strata_down_is_reported_when_the_metric_is_zero(self) -> None:
        reader = _reader(transport=_full_transport(llmlab_strata_up=vector(0.0)))
        self.assertEqual(reader.read()["services"]["strata"]["up"], False)

    def test_strata_unknown_when_the_metric_has_no_sample(self) -> None:
        reader = _reader(
            transport=_full_transport(
                llmlab_strata_up={"status": "success",
                                  "data": {"resultType": "vector", "result": []}}
            )
        )
        self.assertEqual(reader.read()["services"]["strata"]["state"], "unknown")

    def test_a_service_that_answers_http_500_is_not_up(self) -> None:
        """TCP 上端口在听，但 /healthz 回 500 —— 那是**没就绪**，不是可达。"""
        from backend.observability.sources import HOST_HEALTH_TARGETS

        gateway = HOST_HEALTH_TARGETS["gateway"]
        answers = {url: True for url in HOST_HEALTH_TARGETS.values()}
        answers[gateway] = False
        probe = FakeHealthProbe(answers)
        reader = _reader(health_probe=probe)
        payload = reader.read()
        self.assertEqual(payload["services"]["gateway"], {"state": "known", "up": False, "reason": None})
        self.assertEqual(payload["services"]["prometheus"]["up"], True)

    def test_an_unreachable_probe_is_unknown_not_down(self) -> None:
        from backend.observability.sources import HOST_HEALTH_TARGETS

        probe = FakeHealthProbe({url: None for url in HOST_HEALTH_TARGETS.values()})
        reader = _reader(health_probe=probe)
        payload = reader.read()
        entry = payload["services"]["gateway"]
        self.assertEqual(entry["state"], "unknown")
        self.assertIsNone(entry["up"])
        self.assertEqual(entry["reason"], "unreachable")

    def test_the_default_prober_decides_on_http_status_not_on_connect(self) -> None:
        """金丝雀：连接成功但状态 500 必须是 False，而不是「连上了就算在」。"""
        from backend.observability.sources import UrllibHealthProbe

        class _Response:
            status = 500

            def read(self, *_args) -> bytes:
                return b'{"status":"degraded"}'

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        class _Opener:
            def __init__(self, response):
                self._response = response

            def open(self, request, timeout=None):
                return self._response

        self.assertIs(UrllibHealthProbe(opener=_Opener(_Response())).probe("http://127.0.0.1:8200/healthz", 1.0), False)

        class _Ok(_Response):
            status = 200

        self.assertIs(UrllibHealthProbe(opener=_Opener(_Ok())).probe("http://127.0.0.1:8200/healthz", 1.0), True)

        class _Boom:
            def open(self, request, timeout=None):
                raise OSError("connection refused")

        self.assertIsNone(UrllibHealthProbe(opener=_Boom()).probe("http://127.0.0.1:8200/healthz", 1.0))

    def test_the_default_prober_treats_a_real_http_error_status_as_not_ready(self) -> None:
        """真实的 ``urllib`` 对 5xx 抛 ``HTTPError``（也是 ``URLError`` 的子类）。

        若把它归到「不可达」，一次回 503 的服务就会显示成 UNKNOWN 而不是 DOWN。
        """
        import urllib.error

        from backend.observability.sources import UrllibHealthProbe

        class _RaisesHttpError:
            def open(self, request, timeout=None):
                raise urllib.error.HTTPError(
                    "http://127.0.0.1:8200/healthz", 503, "Service Unavailable", {}, None
                )

        self.assertIs(
            UrllibHealthProbe(opener=_RaisesHttpError()).probe(
                "http://127.0.0.1:8200/healthz", 1.0
            ),
            False,
        )

    def test_the_probe_targets_are_fixed_and_loopback_only(self) -> None:
        from backend.observability.sources import HOST_HEALTH_TARGETS

        self.assertEqual(
            set(HOST_HEALTH_TARGETS), {"prometheus", "gateway", "grafana", "evalscope"}
        )
        for key, url in HOST_HEALTH_TARGETS.items():
            with self.subTest(key=key):
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)


class InjectionSeamTest(unittest.TestCase):
    """`HostSummaryStack(read=...)` 需要的正是这个形状。"""

    def test_the_seam_is_a_zero_argument_callable_returning_a_parsed_projection(self) -> None:
        from backend.observability.sources import host_summary_reader

        @dataclass(frozen=True)
        class SeamStack:
            """PR #85 ``HostSummaryStack`` 注入面的等价形状（本基线不含 bff）。"""

            read: Callable[[], object] = lambda: None

            def payload(self):
                return self.read()

        stack = SeamStack(
            read=host_summary_reader(
                transport=_full_transport(), health_probe=FakeHealthProbe(),
                enabled=True, clock=lambda: NOW,
            )
        )
        payload = stack.payload()
        verdict = validate_host_summary(payload, now=NOW)
        self.assertTrue(verdict.accepted, verdict.reason)
        self.assertTrue(verdict.any_live)
        self.assertIn("gpu_power_watts", verdict.live_metric_keys)

    def test_the_default_seam_reads_nothing_rather_than_inventing_a_host(self) -> None:
        """默认 `enabled=False`：注入面回 `None`（BFF 据此 503），不是一份全 0。

        这是「没接线」与「接线了但没数据」的分界；把两者折叠成一个空投影，会让一次
        接线遗漏伪装成一次上游故障。
        """
        from backend.observability.sources import host_summary_reader

        seam = host_summary_reader()
        self.assertTrue(callable(seam))
        self.assertIsNone(seam())


if __name__ == "__main__":
    unittest.main()
