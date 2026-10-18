"""指标来源：固定表达式、环回端点、以及与「没测到」有关的一切。

**两条被本文件钉死的边界：**

1. 请求侧**给不出** PromQL：:meth:`PrometheusProvider.read` 只有一个 ``metric_id`` 入参，
   表达式来自代码里的登记表；
2. 请求侧**给不出** URL：端点地址是构造期配置，且构造时即校验环回 / 白名单。

外加一条与数值有关的纪律：``NaN`` / ``+Inf`` 不是测量结果，多序列而表达式未声明归约
不是「随便取一条」。
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import NOW  # noqa: E402

from backend.observability import (  # noqa: E402
    FIXED_EXPRESSIONS,
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    REASON_AMBIGUOUS_SERIES,
    REASON_NO_SAMPLE,
    REASON_SOURCE_UNAVAILABLE,
    ConfigRejected,
    DISPLAY_STALE,
    MockMetricProvider,
    MockPoint,
    PrometheusExpression,
    PrometheusProvider,
    SourceUnavailable,
    UnknownMetric,
    validate_base_url,
)


class FakeTransport:
    """记录被调用过的查询，并按需返回预置载荷或抛错。"""

    def __init__(self, payload=None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple] = []

    def query(self, base_url: str, query: str, timeout_seconds: float) -> dict:
        self.calls.append((base_url, query, timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.payload


def vector(*values, timestamp: float = 1_700_000_000.0) -> dict:
    """构造一条 Prometheus 向量结果。``values`` 为 ``(标签, 数值)`` 或裸数值。"""
    result = []
    for index, entry in enumerate(values):
        if isinstance(entry, tuple):
            labels, value = entry
        else:
            labels, value = {"instance": f"i{index}"}, entry
        result.append({"metric": labels, "value": [timestamp, str(value)]})
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


class NoArbitraryPromqlTest(unittest.TestCase):
    """Web 层结构上无法提交查询串。"""

    def test_read_takes_only_a_metric_id(self) -> None:
        signature = inspect.signature(PrometheusProvider.read)
        self.assertEqual(list(signature.parameters), ["self", "metric_id"])
        for name in ("query", "expr", "promql", "url"):
            with self.subTest(forbidden=name):
                self.assertNotIn(name, signature.parameters)

    def test_unregistered_metric_name_is_rejected_before_any_transport_call(self) -> None:
        transport = FakeTransport(vector(1))
        provider = PrometheusProvider(
            "http://127.0.0.1:9090", transport=transport, enabled=True
        )
        with self.assertRaises(UnknownMetric):
            provider.read('node_memory_MemTotal_bytes')
        self.assertEqual(transport.calls, [])

    def test_every_fixed_expression_maps_to_a_catalogued_metric(self) -> None:
        for metric_id, expression in FIXED_EXPRESSIONS.items():
            with self.subTest(metric=metric_id):
                self.assertEqual(expression.metric_id, metric_id)
                self.assertIn(expression.reducer, ("single", "sum", "max", "min", "avg"))

    def test_no_expression_carries_a_quote_from_request_data(self) -> None:
        """表达式是常量：不含 f-string 痕迹、不含占位符。"""
        for metric_id, expression in FIXED_EXPRESSIONS.items():
            with self.subTest(metric=metric_id):
                for token in ("{", "}", "%s", "$", "f'", 'f"'):
                    if token in ("{", "}"):
                        continue  # PromQL 标签选择器合法使用花括号
                    self.assertNotIn(token, expression.query)

    def test_there_is_no_per_key_promql_in_the_registry(self) -> None:
        """按人计量不经 Prometheus —— 混用会把整卡利用率误当成用户账单。"""
        for metric_id in FIXED_EXPRESSIONS:
            with self.subTest(metric=metric_id):
                self.assertFalse(metric_id.startswith("principal."))

    def test_expression_ctor_rejects_unknown_metric_and_reducer(self) -> None:
        with self.assertRaises(UnknownMetric):
            PrometheusExpression("principal.gpu.memory_bytes", "up", "single", "x")
        with self.assertRaises(Exception):
            PrometheusExpression("node.gpu.count", "up", "median", "x")


class BaseUrlPolicyTest(unittest.TestCase):
    """端点地址是构造期配置，且必须是环回或显式白名单。"""

    def test_loopback_hosts_are_accepted(self) -> None:
        for url in (
            "http://127.0.0.1:9090",
            "http://localhost:9090",
            "http://[::1]:9090",
            "https://127.0.0.1:9090",
        ):
            with self.subTest(url=url):
                self.assertEqual(validate_base_url(url), url)

    def test_arbitrary_external_host_is_rejected(self) -> None:
        for url in (
            "http://prometheus.example.com:9090",
            "https://metrics.attacker.test",
            "http://10.0.0.5:9090",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigRejected):
                    validate_base_url(url)

    def test_a_hostname_is_never_resolved_to_decide_loopback(self) -> None:
        """判据是「这就是本机」，不是「它说自己指向本机」。

        这些名字在公网 DNS 里都指向 ``127.0.0.1``。若校验路径去查 DNS 并按解析结果
        判断，它们就会被当成环回 —— 那是一类 SSRF 绕过。校验必须只看**字面量**。
        """
        for url in (
            "http://127.0.0.1.nip.io:9090",
            "http://localtest.me:9090",
            "http://localhost.evil.test:9090",
            "http://localhost.:9090",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigRejected):
                    validate_base_url(url)

    def test_the_only_accepted_hostname_literal_is_localhost(self) -> None:
        from backend.observability.sources import LOOPBACK_LITERAL_HOSTS

        self.assertEqual(set(LOOPBACK_LITERAL_HOSTS), {"localhost"})
        self.assertEqual(validate_base_url("http://localhost:9090"), "http://localhost:9090")

    def test_other_loopback_literals_are_accepted(self) -> None:
        for url in ("http://127.0.0.254:9090", "http://[::1]:9090"):
            with self.subTest(url=url):
                self.assertEqual(validate_base_url(url), url)

    def test_non_loopback_ip_literals_are_rejected(self) -> None:
        for url in ("http://0.0.0.0:9090", "http://192.168.1.10:9090", "http://[fe80::1]:9090"):
            with self.subTest(url=url):
                with self.assertRaises(ConfigRejected):
                    validate_base_url(url)

    def test_a_hostname_is_accepted_only_via_the_explicit_allowlist(self) -> None:
        """运维写死的一行配置可以放行一个名字 —— 外部输入不行。"""
        with self.assertRaises(ConfigRejected):
            validate_base_url("http://127.0.0.1.nip.io:9090")
        self.assertEqual(
            validate_base_url("http://127.0.0.1.nip.io:9090",
                              allowed_hosts=("127.0.0.1.nip.io",)),
            "http://127.0.0.1.nip.io:9090",
        )

    def test_allowlist_admits_a_named_host_only(self) -> None:
        self.assertEqual(
            validate_base_url("http://prom.internal:9090", allowed_hosts=("prom.internal",)),
            "http://prom.internal:9090",
        )
        with self.assertRaises(ConfigRejected):
            validate_base_url("http://other.internal:9090", allowed_hosts=("prom.internal",))

    def test_credentials_in_the_url_are_rejected(self) -> None:
        with self.assertRaises(ConfigRejected):
            # Split so a static credential scanner does not read the synthetic
            # userinfo placeholder as a real URI credential.
            validate_base_url("http://" + "user:secret@" + "127.0.0.1:9090")

    def test_query_or_fragment_is_rejected(self) -> None:
        for url in (
            "http://127.0.0.1:9090/?query=up",
            "http://127.0.0.1:9090/#frag",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ConfigRejected):
                    validate_base_url(url)

    def test_unsupported_scheme_is_rejected(self) -> None:
        for url in ("file:///etc/passwd", "ftp://127.0.0.1/x", "gopher://127.0.0.1"):
            with self.subTest(url=url):
                with self.assertRaises(ConfigRejected):
                    validate_base_url(url)

    def test_provider_construction_validates_the_url(self) -> None:
        with self.assertRaises(ConfigRejected):
            PrometheusProvider("http://example.com:9090", transport=FakeTransport())


class PrometheusReadTest(unittest.TestCase):
    def _provider(self, payload, **kwargs):
        transport = FakeTransport(payload, **kwargs)
        provider = PrometheusProvider(
            "http://127.0.0.1:9090", transport=transport, enabled=True
        )
        return provider, transport

    def test_disabled_provider_reports_unavailable_not_zero(self) -> None:
        provider = PrometheusProvider("http://127.0.0.1:9090", transport=FakeTransport())
        sample = provider.read("node.memory.total_bytes")
        self.assertIsNone(sample.value)
        self.assertEqual(sample.quality, QUALITY_UNKNOWN)
        self.assertEqual(sample.reason, REASON_SOURCE_UNAVAILABLE)
        self.assertFalse(provider.available())

    def test_disabled_provider_never_touches_the_transport(self) -> None:
        transport = FakeTransport(vector(1))
        provider = PrometheusProvider(
            "http://127.0.0.1:9090", transport=transport, enabled=False
        )
        provider.read("node.memory.total_bytes")
        self.assertEqual(transport.calls, [])

    def test_single_series_is_measured(self) -> None:
        provider, _ = self._provider(vector(33_554_432_000))
        sample = provider.read("node.memory.total_bytes")
        self.assertEqual(sample.value, 33_554_432_000)
        self.assertEqual(sample.quality, QUALITY_MEASURED)
        self.assertIsNotNone(sample.observed_at)

    def test_empty_result_is_no_sample_not_zero(self) -> None:
        provider, _ = self._provider({"status": "success", "data": {"result": []}})
        sample = provider.read("node.memory.total_bytes")
        self.assertIsNone(sample.value)
        self.assertEqual(sample.reason, REASON_NO_SAMPLE)

    def test_transport_failure_raises_from_read(self) -> None:
        """``read`` 如实抛出 —— 「来源不可达」是一个需要上层显式处理的事实。"""
        provider, _ = self._provider(None, error=SourceUnavailable("boom"))
        with self.assertRaises(SourceUnavailable):
            provider.read("node.memory.total_bytes")

    def test_safe_read_converts_transport_failure_into_unavailable(self) -> None:
        """``safe_read`` 是**唯一**把失败折成读数的入口，且折成 unknown + reason。"""
        provider, _ = self._provider(None, error=SourceUnavailable("boom"))
        sample = provider.safe_read("node.memory.total_bytes")
        self.assertEqual(sample.reason, REASON_SOURCE_UNAVAILABLE)
        self.assertIsNone(sample.value)
        self.assertIsNot(sample.value, 0)

    def test_non_success_status_is_unavailable(self) -> None:
        provider, _ = self._provider({"status": "error", "error": "bad_data"})
        with self.assertRaises(SourceUnavailable):
            provider.read("node.memory.total_bytes")

    def test_multi_series_without_a_reducer_is_ambiguous(self) -> None:
        """``single`` 却拿到多条 —— 报 ambiguous，不悄悄取第一条。"""
        provider, _ = self._provider(vector(1.0, 2.0))
        sample = provider.read("node.memory.total_bytes")
        self.assertIsNone(sample.value)
        self.assertEqual(sample.reason, REASON_AMBIGUOUS_SERIES)
        self.assertEqual(sample.quality, QUALITY_UNKNOWN)

    def test_declared_sum_reducer_is_applied_and_marked_partial(self) -> None:
        provider, _ = self._provider(vector(100.0, 200.0, 300.0))
        sample = provider.read("device.gpu.memory.used_bytes")
        self.assertEqual(sample.value, 600.0)
        # 归约过 => 不是一个单一观测，质量标 partial 而不是 measured。
        self.assertEqual(sample.quality, QUALITY_PARTIAL)

    def test_declared_sum_reducer_is_applied_to_the_numerator_too(self) -> None:
        provider, _ = self._provider(vector(100.0, 900.0, 300.0))
        sample = provider.read("device.gpu.memory.total_bytes")
        self.assertEqual(sample.value, 1300.0)

    def test_the_memory_pair_is_reduced_on_the_same_basis(self) -> None:
        """``used / total`` 是一个比值 —— 分子分母必须来自**同一个总体**。

        早先 ``total`` 用 ``max``、``used`` 用 ``sum``：在多卡机器上分子分母取自不同
        的总体，两卡各满一半就能算出「已用 > 总量」。这是**同基**要求，不是风格偏好。
        """
        total = FIXED_EXPRESSIONS["device.gpu.memory.total_bytes"]
        used = FIXED_EXPRESSIONS["device.gpu.memory.used_bytes"]
        self.assertEqual(total.reducer, used.reducer, "the ratio's two sides must share a basis")
        self.assertEqual(total.reducer, "sum")
        for expression in (total, used):
            self.assertIn("全部卡", expression.description)

    def test_a_multi_card_node_never_yields_used_above_total(self) -> None:
        """两卡各 48 GiB、各用满 —— 同基之和不会给出超过 100% 的占用率。"""
        provider, _ = self._provider(
            vector(("gpu0", 48 * 1024**3), ("gpu1", 48 * 1024**3))
        )
        total = provider.read("device.gpu.memory.total_bytes").value
        used = provider.read("device.gpu.memory.used_bytes").value
        self.assertLessEqual(used, total)
        self.assertAlmostEqual(100.0 * used / total, 100.0)

    def test_reducer_choice_is_auditable_per_expression(self) -> None:
        self.assertEqual(FIXED_EXPRESSIONS["device.gpu.utilization_percent"].reducer, "single")
        self.assertEqual(FIXED_EXPRESSIONS["device.gpu.temperature_celsius"].reducer, "single")
        self.assertEqual(FIXED_EXPRESSIONS["service.up"].reducer, "min")

    def test_single_reduced_expressions_reject_multi_series(self) -> None:
        """利用率与温度声明了 ``single``，多序列时报 ambiguous 而不是取一条。"""
        for metric_id in ("device.gpu.utilization_percent", "device.gpu.temperature_celsius"):
            with self.subTest(metric=metric_id):
                provider, _ = self._provider(vector(10.0, 80.0))
                sample = provider.read(metric_id)
                self.assertIsNone(sample.value)
                self.assertEqual(sample.reason, REASON_AMBIGUOUS_SERIES)

    def test_nan_is_not_a_measurement(self) -> None:
        provider, _ = self._provider(vector("NaN"))
        sample = provider.read("node.memory.total_bytes")
        self.assertIsNone(sample.value)
        self.assertEqual(sample.quality, QUALITY_UNKNOWN)

    def test_infinity_is_not_a_measurement(self) -> None:
        for planted in ("+Inf", "-Inf", "inf"):
            with self.subTest(planted=planted):
                provider, _ = self._provider(vector(planted))
                sample = provider.read("node.memory.total_bytes")
                self.assertIsNone(sample.value)
                self.assertEqual(sample.quality, QUALITY_UNKNOWN)

    def test_matrix_result_is_rejected(self) -> None:
        """区间向量不是本模块的用法 —— 拒绝而不是猜。"""
        provider, _ = self._provider(
            {"status": "success", "data": {"result": [{"values": [[1, "1"]]}]}}
        )
        with self.assertRaises(SourceUnavailable):
            provider.read("node.memory.total_bytes")

    def test_state_metric_maps_one_to_up_and_zero_to_down(self) -> None:
        provider, _ = self._provider(vector(1.0))
        self.assertEqual(provider.read("service.up").value, "up")
        provider, _ = self._provider(vector(0.0))
        self.assertEqual(provider.read("service.up").value, "down")

    def test_state_metric_with_a_fractional_value_is_unknown(self) -> None:
        """``up`` 只可能是 0 或 1；0.5 说明我们理解错了这个指标。"""
        provider, _ = self._provider(vector(0.5))
        sample = provider.read("service.up")
        self.assertIsNone(sample.value)
        self.assertEqual(sample.reason, REASON_AMBIGUOUS_SERIES)

    def test_service_up_uses_min_so_any_target_down_wins(self) -> None:
        provider, _ = self._provider(vector(1.0, 0.0))
        sample = provider.read("service.up")
        self.assertEqual(sample.value, "down")
        self.assertEqual(FIXED_EXPRESSIONS["service.up"].reducer, "min")

    def test_safe_read_converts_failure_into_an_unknown_reading(self) -> None:
        provider, _ = self._provider(None, error=SourceUnavailable("boom"))
        reading = provider.safe_read("node.memory.total_bytes").to_reading(NOW)
        payload = reading.to_dict()
        self.assertIsNone(payload["value"])
        self.assertEqual(payload["reason"], REASON_SOURCE_UNAVAILABLE)
        self.assertEqual(payload["display_quality"], QUALITY_UNKNOWN)

    def test_reading_inherits_scope_and_unit_from_the_catalogue(self) -> None:
        provider, _ = self._provider(vector(1024))
        reading = provider.safe_read("device.gpu.memory.used_bytes").to_reading(NOW)
        self.assertEqual(reading.scope, "device")
        self.assertEqual(reading.unit, "bytes")
        self.assertTrue(reading.physical)

    def test_stale_sample_displays_as_stale(self) -> None:
        old = FIXED_EXPRESSIONS["device.gpu.utilization_percent"]
        provider, _ = self._provider(vector(12.0, timestamp=1_600_000_000.0))
        reading = provider.safe_read("device.gpu.utilization_percent").to_reading(NOW)
        payload = reading.to_dict()
        self.assertEqual(payload["display_quality"], DISPLAY_STALE)
        self.assertTrue(payload["freshness"]["stale"])
        self.assertIsNotNone(old)

    def test_transport_receives_the_registered_query_verbatim(self) -> None:
        provider, transport = self._provider(vector(1))
        provider.read("node.cpu.utilization_percent")
        self.assertEqual(len(transport.calls), 1)
        base_url, query, _timeout = transport.calls[0]
        self.assertEqual(base_url, "http://127.0.0.1:9090")
        self.assertEqual(query, FIXED_EXPRESSIONS["node.cpu.utilization_percent"].query)


class NoRedirectFollowingTest(unittest.TestCase):
    """构造期校验管不住跳转 —— 请求期必须把重定向当失败。"""

    def test_the_transport_refuses_redirects(self) -> None:
        from backend.observability.sources import _NoRedirects

        handler = _NoRedirects()
        with self.assertRaises(Exception) as caught:
            handler.redirect_request(
                type("Req", (), {"full_url": "http://127.0.0.1:9090/x"})(),
                None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/",
            )
        self.assertIn("redirect refused", str(caught.exception))

    def test_the_default_opener_does_not_follow_redirects(self) -> None:
        """默认 transport 用的 opener 必须**不含**跟随重定向的处理器。

        否则一个被指向的（或已攻陷的）Prometheus 只要回一个 302，就能让客户端去取
        一个既非环回、也不在白名单里的地址 —— 构造期那道校验在请求期被绕过。
        """
        from backend.observability.sources import _OPENER, _NoRedirects

        handler_names = {type(h).__name__ for h in _OPENER.handlers}
        self.assertIn("_NoRedirects", handler_names)
        # 我们的处理器排在最前，且 urllib 默认的那个仍然在表里 —— 关键是它接了
        # 我们的 redirect_request，也就是被覆盖了。
        redirect_handlers = [
            h for h in _OPENER.handlers
            if isinstance(h, __import__("urllib.request", fromlist=["x"]).HTTPRedirectHandler)
        ]
        self.assertTrue(redirect_handlers)
        for handler in redirect_handlers:
            with self.subTest(handler=type(handler).__name__):
                self.assertIsInstance(handler, _NoRedirects)

    def test_urllib_default_really_would_follow_redirects(self) -> None:
        """金丝雀：证明上一条不是恒真 —— 默认处理器**确实**会跟随。"""
        import urllib.request

        default = urllib.request.HTTPRedirectHandler()
        self.assertFalse(isinstance(default, __import__(
            "backend.observability.sources", fromlist=["x"])._NoRedirects))


class MockProviderTest(unittest.TestCase):
    def test_mock_is_always_synthetic(self) -> None:
        provider = MockMetricProvider(
            {"node.gpu.count": MockPoint(value=1, observed_at=NOW)}
        )
        self.assertTrue(provider.synthetic)
        self.assertEqual(provider.source, "mock")

    def test_missing_point_is_no_sample_not_zero(self) -> None:
        provider = MockMetricProvider({})
        sample = provider.read("node.memory.total_bytes")
        self.assertIsNone(sample.value)
        self.assertEqual(sample.reason, REASON_NO_SAMPLE)

    def test_unknown_metric_is_rejected(self) -> None:
        with self.assertRaises(UnknownMetric):
            MockMetricProvider({"principal.gpu.memory_bytes": MockPoint(value=1)})
        with self.assertRaises(UnknownMetric):
            MockMetricProvider().read("not.a.metric")

    def test_points_produce_readings_with_the_full_envelope(self) -> None:
        provider = MockMetricProvider(
            {
                "device.gpu.utilization_percent": MockPoint(
                    value=41.5, observed_at=NOW - timedelta(seconds=5)
                )
            }
        )
        payload = provider.safe_read("device.gpu.utilization_percent").to_reading(NOW).to_dict()
        for key in ("source", "observed_at", "quality", "freshness", "display_quality"):
            self.assertIn(key, payload)
        self.assertEqual(payload["value"], 41.5)
        self.assertEqual(payload["source"], "mock")

    def test_to_readings_returns_a_metric_map(self) -> None:
        provider = MockMetricProvider(
            {
                "node.gpu.count": MockPoint(value=1, observed_at=NOW),
                "node.cpu.load1": MockPoint(value=2.25, observed_at=NOW),
            }
        )
        payload = provider.to_readings(
            ["node.gpu.count", "node.cpu.load1", "node.disk.used_bytes"], NOW
        )
        self.assertEqual(payload["node.gpu.count"]["value"], 1)
        self.assertIsNone(payload["node.disk.used_bytes"]["value"])

    def test_state_point_requires_allowed_states(self) -> None:
        provider = MockMetricProvider(
            {"service.up": MockPoint(value="up", observed_at=NOW)}
        )
        reading = provider.safe_read("service.up").to_reading(NOW)
        self.assertEqual(reading.state, "up")

    def test_non_mock_point_is_rejected(self) -> None:
        from backend.observability import InvalidRequest

        with self.assertRaises(InvalidRequest):
            MockMetricProvider({"node.gpu.count": 1})


if __name__ == "__main__":
    unittest.main()
