"""健康与就绪：**没有观测就是 UNKNOWN**，注册与就绪是两件事。

这一组测试盯的是「不许编」这条纪律的每一处落点：不可达不许带延迟、未观测不许写 0、
缺观测源不许变成 verified、不同上游各自保留自己的健康事实。
"""

from __future__ import annotations

import unittest

from backend.registry.errors import InvalidRequest
from backend.registry.gateway import DetachedGatewayAdapter
from backend.registry.health import (
    REASON_NEVER_PROBED,
    REASON_NOT_CONFIGURED,
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_UNKNOWN,
    STATE_UNREACHABLE,
    HealthObservation,
    ProbeResult,
    aggregate_health,
    metric,
    observation_from_probe,
    unknown,
)
from tests.registry.support import (
    DEGRADED_PORT,
    HEALTHY_PORT,
    UNREACHABLE_PORT,
    Harness,
    StaticProber,
    close_all,
)

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


class ProbeResultInvariantTest(unittest.TestCase):
    def test_an_unreachable_probe_must_not_report_a_latency(self) -> None:
        # 这正是要防的伪造：连不上却给出一个延迟数字。
        with self.assertRaises(InvalidRequest):
            ProbeResult(reachable=False, latency_ms=3.0)

    def test_a_negative_latency_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            ProbeResult(reachable=True, latency_ms=-1.0)

    def test_reachable_must_be_a_bool(self) -> None:
        with self.assertRaises(InvalidRequest):
            ProbeResult(reachable="yes")  # type: ignore[arg-type]


class HealthObservationInvariantTest(unittest.TestCase):
    def test_an_unknown_observation_must_not_carry_a_latency(self) -> None:
        with self.assertRaises(InvalidRequest):
            HealthObservation(
                state=STATE_UNKNOWN,
                source="none",
                observed_at=None,
                latency_ms=1.0,
                reason=REASON_NOT_CONFIGURED,
                detail=None,
                verified=False,
            )

    def test_an_unreachable_observation_must_not_carry_a_latency(self) -> None:
        with self.assertRaises(InvalidRequest):
            HealthObservation(
                state=STATE_UNREACHABLE,
                source="loopback_probe",
                observed_at="2026-10-15T00:00:00Z",
                latency_ms=2.0,
                reason="probe_failed",
                detail=None,
                verified=False,
            )

    def test_an_unknown_state_is_not_a_health_state(self) -> None:
        with self.assertRaises(InvalidRequest):
            HealthObservation(
                state="probably-fine",
                source="none",
                observed_at=None,
                latency_ms=None,
                reason=None,
                detail=None,
                verified=False,
            )


class ObservationFromProbeTest(unittest.TestCase):
    def test_a_reachable_probe_becomes_healthy_and_verified(self) -> None:
        observation = observation_from_probe(
            ProbeResult(reachable=True, latency_ms=2.5), observed_at="2026-10-15T00:00:00Z"
        )
        self.assertEqual(observation.state, STATE_HEALTHY)
        self.assertTrue(observation.verified)
        self.assertEqual(observation.latency_ms, 2.5)

    def test_a_degraded_probe_is_verified_but_degraded(self) -> None:
        observation = observation_from_probe(
            ProbeResult(reachable=True, latency_ms=9.0, degraded=True),
            observed_at="2026-10-15T00:00:00Z",
        )
        self.assertEqual(observation.state, STATE_DEGRADED)
        # 「探测到了它」与「它一切正常」是两件事：前者由 verified 承载，
        # 后者由 state 承载。二者不可互相顶替。
        self.assertTrue(observation.verified)

    def test_a_failed_probe_is_unreachable_with_no_latency(self) -> None:
        observation = observation_from_probe(
            ProbeResult(reachable=False, detail="connection refused"),
            observed_at="2026-10-15T00:00:00Z",
        )
        self.assertEqual(observation.state, STATE_UNREACHABLE)
        self.assertFalse(observation.verified)
        self.assertIsNone(observation.latency_ms)
        self.assertEqual(observation.detail, "connection refused")


class AggregateHealthTest(unittest.TestCase):
    def test_no_observations_at_all_is_unknown(self) -> None:
        result = aggregate_health([])
        self.assertEqual(result["state"], STATE_UNKNOWN)
        self.assertIsNone(result["latency_ms"])

    def test_all_unknown_is_unknown_not_healthy(self) -> None:
        observations = [
            observation_from_probe(ProbeResult(reachable=False), observed_at="x")
        ]
        # 手工造两条 unknown（不是 unreachable）。
        unknowns = [
            HealthObservation(STATE_UNKNOWN, "none", None, None, REASON_NOT_CONFIGURED, None, False),
            HealthObservation(STATE_UNKNOWN, "none", None, None, REASON_NEVER_PROBED, None, False),
        ]
        result = aggregate_health(unknowns)
        self.assertEqual(result["state"], STATE_UNKNOWN)
        self.assertEqual(result["observed_sources"], 0)
        self.assertEqual(len(observations), 1)

    def test_one_healthy_and_one_unreachable_is_degraded(self) -> None:
        observations = [
            observation_from_probe(ProbeResult(reachable=True, latency_ms=1.0), observed_at="a"),
            observation_from_probe(ProbeResult(reachable=False, detail="refused"), observed_at="b"),
        ]
        result = aggregate_health(observations)
        self.assertEqual(result["state"], STATE_DEGRADED)

    def test_all_unreachable_is_unreachable(self) -> None:
        observations = [
            observation_from_probe(ProbeResult(reachable=False, detail="refused"), observed_at="a"),
            observation_from_probe(ProbeResult(reachable=False, detail="refused"), observed_at="b"),
        ]
        result = aggregate_health(observations)
        self.assertEqual(result["state"], STATE_UNREACHABLE)

    def test_all_healthy_is_healthy(self) -> None:
        observations = [
            observation_from_probe(ProbeResult(reachable=True, latency_ms=1.0), observed_at="a"),
            observation_from_probe(ProbeResult(reachable=True, latency_ms=2.0), observed_at="b"),
        ]
        result = aggregate_health(observations)
        self.assertEqual(result["state"], STATE_HEALTHY)
        self.assertEqual(result["latency_ms"], 2.0)

    def test_unknown_does_not_drag_a_healthy_set_down(self) -> None:
        observations = [
            observation_from_probe(ProbeResult(reachable=True, latency_ms=1.0), observed_at="a"),
            HealthObservation(STATE_UNKNOWN, "none", None, None, REASON_NOT_CONFIGURED, None, False),
        ]
        result = aggregate_health(observations)
        self.assertEqual(result["state"], STATE_HEALTHY)

    def test_non_observation_input_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            aggregate_health("not-a-list")  # type: ignore[arg-type]
        with self.assertRaises(InvalidRequest):
            aggregate_health([{"state": "healthy"}])  # type: ignore[list-item]


class MetricHelperTest(unittest.TestCase):
    def test_no_value_yields_an_honest_unknown(self) -> None:
        block = metric(None, unit="tokens")
        self.assertIsNone(block["value"])
        self.assertEqual(block["state"], STATE_UNKNOWN)
        self.assertIsNone(block["source"])

    def test_a_measured_value_requires_a_source(self) -> None:
        with self.assertRaises(InvalidRequest):
            metric(5, state=STATE_HEALTHY, source=None)

    def test_unknown_with_a_value_is_self_contradictory(self) -> None:
        with self.assertRaises(InvalidRequest):
            metric(5, state=STATE_UNKNOWN, source="x")

    def test_a_measured_value_carries_its_source(self) -> None:
        block = metric(5, source="registry_audit", unit="calls")
        self.assertEqual(block["value"], 5)
        self.assertEqual(block["source"], "registry_audit")


class HarnessHealthTest(unittest.TestCase):
    """通过 HTTP 观察健康语义。"""

    def test_health_of_each_port_variant(self) -> None:
        h = Harness()
        cases = {
            HEALTHY_PORT: ("verified", STATE_HEALTHY),
            DEGRADED_PORT: ("verified", STATE_DEGRADED),
            UNREACHABLE_PORT: ("unavailable", STATE_UNREACHABLE),
        }
        for port, (status, health_state) in cases.items():
            with self.subTest(port=port):
                code, _h, payload = h.request(
                    "POST",
                    f"{PREFIX}/engines",
                    principal="alice",
                    body={"name": f"e-{port}", "target_url": f"http://127.0.0.1:{port}/v1"},
                )
                self.assertEqual(code, 200, payload)
                self.assertEqual(payload["data"]["status"], status)
                self.assertEqual(payload["data"]["health"]["state"], health_state)

    def test_an_unreachable_engine_still_reports_no_latency_over_http(self) -> None:
        h = Harness()
        _c, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "dead", "target_url": "http://127.0.0.1:8081/v1"},
        )
        self.assertIsNone(payload["data"]["health"]["latency_ms"])
        self.assertEqual(payload["data"]["health"]["reason"], "probe_failed")

    def test_without_a_prober_every_engine_is_unknown(self) -> None:
        h = Harness(prober=None)
        _c, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(payload["data"]["health"]["state"], STATE_UNKNOWN)
        self.assertEqual(payload["data"]["health"]["reason"], REASON_NOT_CONFIGURED)
        self.assertIsNone(payload["data"]["health"]["latency_ms"])
        self.assertIsNone(payload["data"]["health"]["observed_at"])

    def test_a_freshly_registered_deployment_is_never_probed_yet(self) -> None:
        h = Harness()
        engine_id = h.make_engine("alice", "e")
        model_id = h.make_model("alice", "m")
        _c, _h, payload = h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )
        self.assertEqual(payload["data"]["status"], "registered")
        self.assertEqual(payload["data"]["health"]["state"], STATE_UNKNOWN)
        self.assertEqual(payload["data"]["health"]["reason"], REASON_NEVER_PROBED)

    def test_a_model_health_is_unknown_because_weights_are_never_read(self) -> None:
        h = Harness()
        _c, _h, payload = h.request(
            "POST", f"{PREFIX}/models", principal="alice", body={"name": "m", "weight_path": "/home/x/y"}
        )
        self.assertEqual(payload["data"]["health"]["state"], STATE_UNKNOWN)
        self.assertIn("reference", payload["data"]["health"]["reason"])

    def test_different_upstreams_keep_their_own_health(self) -> None:
        # 「不同服务健康降级」：一个健康、一个不可达，各自的 DTO 必须保留各自的事实。
        prober = StaticProber(
            {
                HEALTHY_PORT: ProbeResult(reachable=True, latency_ms=3.0),
                UNREACHABLE_PORT: ProbeResult(reachable=False, detail="refused"),
            }
        )
        h = Harness(prober=prober)
        h.make_engine("alice", "healthy-one", port=HEALTHY_PORT)
        h.make_engine("alice", "dead-one", port=UNREACHABLE_PORT)
        _c, _h, payload = h.request("GET", f"{PREFIX}/engines", principal="alice")
        by_name = {item["name"]: item for item in payload["data"]["items"]}
        self.assertEqual(by_name["healthy-one"]["health"]["state"], STATE_HEALTHY)
        self.assertEqual(by_name["dead-one"]["health"]["state"], STATE_UNREACHABLE)
        self.assertEqual(by_name["healthy-one"]["health"]["latency_ms"], 3.0)
        self.assertIsNone(by_name["dead-one"]["health"]["latency_ms"])

    def test_re_verification_updates_the_state(self) -> None:
        prober = StaticProber({HEALTHY_PORT: ProbeResult(reachable=True, latency_ms=1.0)})
        h = Harness(prober=prober)
        engine_id = h.make_engine("alice", "e", port=HEALTHY_PORT)
        self.assertEqual(
            h.request("GET", f"{PREFIX}/engines/{engine_id}", principal="alice")[2]["data"]["status"],
            "verified",
        )
        # 上游垮了：再验证一次必须反映为不可达，而不是沿用上一次的「verified」。
        prober.set(HEALTHY_PORT, ProbeResult(reachable=False, detail="refused"))
        _c, _h, payload = h.request(
            "POST", f"{PREFIX}/engines/{engine_id}/verify", principal="alice"
        )
        self.assertEqual(payload["data"]["status"], "unavailable")
        self.assertEqual(payload["data"]["health"]["state"], STATE_UNREACHABLE)
        self.assertIsNone(payload["data"]["health"]["latency_ms"])

    def test_unknown_helper_shape(self) -> None:
        block = unknown()
        self.assertEqual(block["state"], STATE_UNKNOWN)
        self.assertEqual(block["reason"], REASON_NOT_CONFIGURED)
        self.assertIsNone(block["latency_ms"])
        self.assertFalse(block["verified"])


class ServiceStatusTest(unittest.TestCase):
    def test_status_reports_each_source_honestly(self) -> None:
        h = Harness(prober=None)
        _c, _h, payload = h.request("GET", f"{PREFIX}/status", principal="alice")
        data = payload["data"]
        self.assertFalse(data["observation"]["prober_attached"])
        self.assertEqual(data["observation"]["state"], STATE_UNKNOWN)
        self.assertEqual(data["observation"]["reason"], REASON_NOT_CONFIGURED)
        # 测试装置挂的是 mock adapter，所以这里如实报 attached + mock。
        self.assertTrue(data["gateway"]["attached"])
        self.assertEqual(data["gateway"]["adapter"], "mock")
        self.assertEqual(data["gateway"]["state"], "available")
        # 用量管道未接入。
        self.assertEqual(data["usage_pipeline"]["state"], STATE_UNKNOWN)
        self.assertEqual(data["usage_pipeline"]["reason"], REASON_NOT_CONFIGURED)

    def test_a_deployment_without_a_gateway_says_so_instead_of_pretending(self) -> None:
        # 这是**默认**部署形态（DetachedGatewayAdapter）：没接网关就必须说没接。
        h = Harness(gateway=DetachedGatewayAdapter())
        _c, _h, payload = h.request("GET", f"{PREFIX}/status", principal="alice")
        data = payload["data"]
        self.assertFalse(data["gateway"]["attached"])
        self.assertEqual(data["gateway"]["state"], "unavailable")
        self.assertEqual(data["gateway"]["reason"], "gateway_not_attached")

    def test_invoke_without_a_gateway_is_503_not_a_fabricated_result(self) -> None:
        h = Harness(gateway=DetachedGatewayAdapter())
        endpoint_id = h.make_ready_endpoint("alice")
        code, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/invoke",
            principal="alice",
            body={"prompt": "hi"},
        )
        self.assertEqual(code, 503)
        self.assertEqual(payload["error"]["code"], "registry.upstream_unavailable")
        self.assertIn("gateway_not_attached", payload["error"]["message"])

    def test_status_is_not_an_unauthenticated_endpoint(self) -> None:
        h = Harness()
        code, _h, payload = h.request("GET", f"{PREFIX}/status")
        self.assertEqual(code, 401)

    def test_the_liveness_probe_carries_no_tenant_data(self) -> None:
        h = Harness()
        code, _h, payload = h.request("GET", f"{PREFIX}/healthz")
        self.assertEqual(code, 200)
        text = str(payload)
        self.assertNotIn("alice", text)
        self.assertNotIn("eng_", text)
        self.assertNotIn("ep_", text)
        self.assertTrue(payload["data"]["synthetic"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
