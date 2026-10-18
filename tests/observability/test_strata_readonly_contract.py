"""Contract tests for the Strata read-only JSON adapter.

All fixtures are synthetic. No socket is opened, no key is read, and no host
address or private artifact is used.
"""

from __future__ import annotations

import inspect
import json
import unittest

from backend.observability import strata_readonly as S


class FakeTransport:
    def __init__(self, responses, *, key="synthetic-key-value", fingerprint="abc123def456"):
        self.responses = dict(responses)
        self.calls = []
        self._key = key
        self._fingerprint = fingerprint

    @property
    def key_present(self):
        return bool(self._key)

    @property
    def key_fingerprint(self):
        return self._fingerprint

    def get_json(self, path, *, auth, timeout_s):
        self.calls.append((path, auth, timeout_s))
        return self.responses.get(
            path, S.TransportResult(False, None, 0, "not-configured")
        )


def ok(payload, status=200):
    return S.TransportResult(True, payload, status)


def fail(status=0, error=None):
    return S.TransportResult(False, None, status, error)


HEALTH = {"api_key": True, "loaded": True, "status": "ok"}
MODELS = {
    "data": [
        {"id": "m1", "status": {"value": "ready"}, "meta": {"n_ctx": 8192}},
        {"id": "m2", "status": "loading"},
    ]
}
STATUS = {
    "machine": {
        "gpu": {
            "name": "A6000",
            "used_mib": 1024,
            "total_mib": 49152,
            "util_pct": 37,
            "temp_c": 51,
            "power_w": 120,
        }
    }
}
METRICS = {
    "hardware": {
        "gpu_util": 35,
        "gpu_mem_used": 1048576 * 1000,
        "gpu_mem_total": 1048576 * 49152,
        "gpu_temp": 50,
        "gpu_power": 118,
        "gpu_power_limit": 300,
        "gpu_pcie_gen": 3,
        "gpu_pcie_gen_max": 4,
        "gpu_pcie_width": 16,
        "gpu_pcie_rx_mb": 12,
        "gpu_pcie_tx_mb": 3,
        "cpu": 22,
        "ram_used": 1048576 * 8000,
        "ram_total": 1048576 * 32768,
        "disk_read_mb": 5,
        "disk_write_mb": 2,
    },
    "hardware_static": {"gpu_name": "A6000", "cpu_name": "Xeon", "cores": 16, "threads": 32},
    "live": {"state": "idle", "queued": 0, "tok_s": 0},
    "totals": {"requests": 7, "prompt_tokens": 100, "output_tokens": 50},
    "engine": {"model": "qwen", "version": "1.2", "max_context": 32768},
}
ALL_OK = {
    "/health": ok(HEALTH),
    "/v1/models": ok(MODELS),
    "/v1/status": ok(STATUS),
    "/metrics": ok(METRICS),
}


class PathAndAuthContractTests(unittest.TestCase):
    def test_paths_are_fixed_read_only_and_have_no_billing_endpoint(self):
        self.assertEqual(
            dict(S.STRATA_PATHS),
            {
                "health": "/health",
                "models": "/v1/models",
                "status": "/v1/status",
                "metrics": "/metrics",
            },
        )
        self.assertEqual(S.READ_ONLY_PATHS, frozenset(S.STRATA_PATHS.values()))
        for path in S.STRATA_PATHS.values():
            self.assertTrue(path.startswith("/"))
            self.assertNotIn("://", path)
            self.assertNotIn("chat", path)
            self.assertNotIn("completions", path)
        adapter = S.StrataReadonly(FakeTransport({}))
        self.assertFalse(hasattr(adapter, "chat"))

    def test_auth_contract_health_is_anonymous_others_are_bearer(self):
        self.assertEqual(dict(S.STRATA_AUTH), {
            "health": "anonymous",
            "models": "bearer",
            "status": "bearer",
            "metrics": "bearer",
        })

    def test_each_fixed_endpoint_is_called_once_with_its_auth(self):
        transport = FakeTransport(ALL_OK)
        S.StrataReadonly(transport, timeout_s=2.5).snapshot()
        self.assertEqual([p for p, _, _ in transport.calls], [
            "/health", "/v1/models", "/v1/status", "/metrics"
        ])
        self.assertEqual(dict((p, a) for p, a, _ in transport.calls), {
            "/health": "anonymous",
            "/v1/models": "bearer",
            "/v1/status": "bearer",
            "/metrics": "bearer",
        })
        self.assertTrue(all(t == 2.5 for _, _, t in transport.calls))

    def test_adapter_cannot_accept_an_arbitrary_url(self):
        with self.assertRaises(TypeError):
            S.StrataReadonly(FakeTransport({}), url="arbitrary-target")
        self.assertEqual(list(inspect.signature(S.StrataReadonly.snapshot).parameters), ["self"])


class ModelApiTests(unittest.TestCase):
    def test_health_and_models_project_the_model_api(self):
        api = S.StrataReadonly(FakeTransport(ALL_OK)).snapshot()["model_api"]
        self.assertTrue(api["ok"])
        self.assertTrue(api["reachable"])
        self.assertTrue(api["auth_required"])
        self.assertTrue(api["loaded"])
        self.assertEqual(api["model_list"], [
            {"id": "m1", "status": "ready", "context": 8192},
            {"id": "m2", "status": "loading", "context": None},
        ])

    def test_models_401_is_a_credential_problem_not_an_outage(self):
        responses = dict(ALL_OK)
        responses["/v1/models"] = fail(401)
        api = S.StrataReadonly(FakeTransport(responses)).snapshot()["model_api"]
        self.assertTrue(api["reachable"])
        self.assertFalse(api["ok"])
        self.assertEqual(api["error_kind"], "credential")
        self.assertEqual(api["models_http_status"], 401)

    def test_both_health_and_models_unreachable_is_an_outage(self):
        responses = {"/health": fail(0), "/v1/models": fail(0)}
        api = S.StrataReadonly(FakeTransport(responses)).snapshot()["model_api"]
        self.assertFalse(api["reachable"])
        self.assertFalse(api["ok"])
        self.assertEqual(api["error_kind"], "unreachable")

    def test_both_503_is_unavailable_not_a_network_outage(self):
        responses = {"/health": fail(503), "/v1/models": fail(503)}
        snap = S.StrataReadonly(FakeTransport(responses)).snapshot()
        self.assertFalse(snap["model_api"]["reachable"])
        self.assertEqual(snap["model_api"]["error_kind"], "unavailable")
        self.assertEqual(snap["sources"]["health"]["error_kind"], "unavailable")
        self.assertEqual(snap["sources"]["models"]["error_kind"], "unavailable")

    def test_health_200_with_models_503_is_partial_and_names_unavailable(self):
        responses = dict(ALL_OK)
        responses["/v1/models"] = fail(503)
        snap = S.StrataReadonly(FakeTransport(responses)).snapshot()
        self.assertTrue(snap["model_api"]["reachable"])
        self.assertFalse(snap["model_api"]["ok"])
        self.assertEqual(snap["model_api"]["error_kind"], "partial")
        self.assertEqual(snap["sources"]["models"]["error_kind"], "unavailable")

    def test_malformed_payload_is_unavailable_without_raising(self):
        responses = dict(ALL_OK)
        responses["/health"] = ok("not an object")
        snap = S.StrataReadonly(FakeTransport(responses)).snapshot()
        self.assertEqual(snap["sources"]["health"]["error_kind"], "contract")
        self.assertFalse(snap["model_api"]["ok"])


class GpuTelemetryTests(unittest.TestCase):
    def test_metrics_is_preferred_and_bytes_are_scaled(self):
        gpu = S.StrataReadonly(FakeTransport(ALL_OK)).snapshot()["gpu"]
        self.assertTrue(gpu["source_present"])
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["source"], "Strata /metrics hardware")
        self.assertEqual(gpu["name"], "A6000")
        self.assertEqual(gpu["used_mib"], 1000.0)
        self.assertEqual(gpu["total_mib"], 49152.0)
        self.assertEqual(gpu["pcie_rx_mb"], 12)
        self.assertEqual(gpu["cross_check"]["delta_pct"], 2.34)

    def test_status_is_the_fallback_when_metrics_is_absent(self):
        responses = dict(ALL_OK)
        responses["/metrics"] = fail(404)
        gpu = S.StrataReadonly(FakeTransport(responses)).snapshot()["gpu"]
        self.assertTrue(gpu["source_present"])
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["source"], "Strata /v1/status machine.gpu (fallback: /metrics unavailable)")
        self.assertEqual(gpu["used_mib"], 1024.0)
        self.assertIsNone(gpu["pcie_rx_mb"])
        self.assertIsNone(gpu["cross_check"])

    def test_metrics_and_status_failures_are_distinct_in_the_reason(self):
        responses = {
            "/health": ok(HEALTH),
            "/v1/models": ok(MODELS),
            "/v1/status": fail(0),
            "/metrics": fail(401),
        }
        snap = S.StrataReadonly(FakeTransport(responses)).snapshot()
        gpu = snap["gpu"]
        self.assertFalse(gpu["source_present"])
        self.assertFalse(gpu["available"])
        self.assertIn("metrics HTTP 401", gpu["reason"])
        self.assertIn("status HTTP unreachable", gpu["reason"])
        self.assertEqual(snap["sources"]["metrics"]["error_kind"], "credential")
        self.assertEqual(snap["sources"]["status"]["error_kind"], "unreachable")

    def test_missing_values_are_none_not_zero(self):
        responses = dict(ALL_OK)
        responses["/metrics"] = ok({"hardware": {"gpu_util": 35}})
        snap = S.StrataReadonly(FakeTransport(responses)).snapshot()
        gpu = snap["gpu"]
        self.assertTrue(gpu["source_present"])
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["util_pct"], 35)
        self.assertIsNone(gpu["used_mib"])
        self.assertIsNone(gpu["total_mib"])
        self.assertIsNone(gpu["temp_c"])
        self.assertIsNone(snap["host"]["ram_used_mib"])
        self.assertIsNone(snap["engine"]["requests"])

    def test_source_present_without_a_trustworthy_gauge_is_not_available(self):
        responses = dict(ALL_OK)
        responses["/metrics"] = ok({"hardware": {}})
        gpu = S.StrataReadonly(FakeTransport(responses)).snapshot()["gpu"]
        self.assertTrue(gpu["source_present"])
        self.assertFalse(gpu["available"])
        self.assertEqual(gpu["reason"], "telemetry source present but no trustworthy numeric gauge")

    def test_a_real_zero_gauge_is_preserved_and_counts_as_available(self):
        responses = dict(ALL_OK)
        responses["/metrics"] = ok({
            "hardware": {
                "gpu_util": 0,
                "gpu_mem_used": 0,
                "gpu_mem_total": 0,
                "gpu_temp": 0,
                "gpu_power": 0,
            }
        })
        gpu = S.StrataReadonly(FakeTransport(responses)).snapshot()["gpu"]
        self.assertTrue(gpu["available"])
        self.assertEqual(gpu["util_pct"], 0)
        self.assertEqual(gpu["used_mib"], 0.0)
        self.assertEqual(gpu["temp_c"], 0)

    def test_raw_gauges_are_type_and_range_sanitised(self):
        responses = dict(ALL_OK)
        responses["/metrics"] = ok({
            "hardware": {
                "gpu_util": "35",
                "gpu_temp": float("nan"),
                "gpu_power": float("inf"),
                "gpu_mem_used": True,
                "gpu_mem_total": -1,
                "gpu_pcie_width": 999,
            }
        })
        gpu = S.StrataReadonly(FakeTransport(responses)).snapshot()["gpu"]
        self.assertTrue(gpu["source_present"])
        self.assertFalse(gpu["available"])
        self.assertIsNone(gpu["util_pct"])
        self.assertIsNone(gpu["temp_c"])
        self.assertIsNone(gpu["power_w"])
        self.assertIsNone(gpu["used_mib"])
        self.assertIsNone(gpu["total_mib"])
        self.assertIsNone(gpu["pcie_width"])

    def test_json_metrics_is_not_rendered_as_prometheus_text(self):
        snap = S.StrataReadonly(FakeTransport(ALL_OK)).snapshot()
        dumped = json.dumps(snap)
        self.assertNotIn("# HELP", dumped)
        self.assertNotIn("llmlab_strata_", dumped)
        self.assertIsInstance(snap["engine"], dict)
        self.assertIsInstance(snap["host"], dict)


class CredentialAndClockTests(unittest.TestCase):
    def test_key_is_never_rendered(self):
        snap = S.StrataReadonly(FakeTransport(ALL_OK, key="synthetic-key-value")).snapshot()
        dumped = json.dumps(snap)
        self.assertNotIn("synthetic-key-value", dumped)
        self.assertTrue(snap["key_present"])
        self.assertEqual(snap["key_fingerprint"], "abc123def456")

    def test_fingerprint_is_bounded_and_display_safe(self):
        long = S.StrataReadonly(
            FakeTransport(ALL_OK, fingerprint="x" * 100)
        ).snapshot()["key_fingerprint"]
        self.assertIsNone(long)
        html = S.StrataReadonly(
            FakeTransport(ALL_OK, fingerprint="<script>alert(1)</script>")
        ).snapshot()["key_fingerprint"]
        self.assertIsNone(html)
        non_string = S.StrataReadonly(
            FakeTransport(ALL_OK, fingerprint=12345)
        ).snapshot()["key_fingerprint"]
        self.assertIsNone(non_string)
        safe = S.StrataReadonly(
            FakeTransport(ALL_OK, fingerprint="abc123def456")
        ).snapshot()["key_fingerprint"]
        self.assertEqual(safe, "abc123def456")

    def test_transport_exception_is_redacted(self):
        class Boom:
            key_present = False
            key_fingerprint = ""

            def get_json(self, path, *, auth, timeout_s):
                raise RuntimeError("SECRET-LEAK-CANARY")

        snap = S.StrataReadonly(Boom()).snapshot()
        self.assertEqual(snap["sources"]["health"]["error_kind"], "transport")
        dumped = json.dumps(snap)
        self.assertNotIn("SECRET-LEAK-CANARY", dumped)
        self.assertNotIn("RuntimeError", dumped)

    def test_invalid_payload_shapes_do_not_raise(self):
        for payload in ([1, 2, 3], "text", None, 42):
            responses = dict(ALL_OK)
            responses["/metrics"] = ok(payload)
            responses["/v1/status"] = fail(0)
            snap = S.StrataReadonly(FakeTransport(responses)).snapshot()
            self.assertFalse(snap["gpu"]["source_present"])
            self.assertFalse(snap["gpu"]["available"])

    def test_observed_at_is_utc_from_the_injected_clock(self):
        snap = S.StrataReadonly(FakeTransport(ALL_OK), clock=lambda: 0).snapshot()
        self.assertEqual(snap["observed_at"], "1970-01-01T00:00:00Z")
        snap = S.StrataReadonly(FakeTransport(ALL_OK), clock=lambda: 1700000000).snapshot()
        self.assertEqual(snap["observed_at"], "2023-11-14T22:13:20Z")


if __name__ == "__main__":
    unittest.main()
