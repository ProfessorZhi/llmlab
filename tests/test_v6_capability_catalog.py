"""Source-only validation of the V6.2 capability catalogue (no Dell or mocked lab data).

This is NOT a production readiness probe. It checks documentation invariants so
Claude Code can build against contracts without access to the A6000 workstation.
Run: python3 -m unittest tests.test_v6_capability_catalog -v
"""

from __future__ import annotations

import json
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
CATALOG = REPO / "docs/integrations/portal-capabilities.v1.json"


class CapabilityCatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(CATALOG.read_text(encoding="utf-8"))

    def test_does_not_claim_live_production(self):
        root = self.registry
        self.assertEqual(root["baseline"]["runtime_assessment"], "not_attested")
        self.assertEqual(root["baseline"]["inspection"], "repository source only")
        for element in root["providers"] + root["routes"]:
            self.assertEqual(element["host_runtime_verification"], "not_run")
        self.assertNotIn("deployed", root["status_vocabulary"])
        self.assertNotIn("ready", root["status_vocabulary"])

    def test_contains_no_operational_measurements_or_fake_resource_rows(self):
        policy = self.registry["no_fabrication_policy"]
        for field in ("user_or_model_records_in_file", "measurement_values_in_file",
                      "public_credentials_in_file"):
            self.assertEqual(policy[field], 0, field)
        self.assertTrue(policy["empty_requires_queried_authorized_source"])
        self.assertTrue(policy["measured_zero_requires_valid_source"])
        for metric in self.registry["metrics"]:
            self.assertNotIn("value", metric)
            self.assertNotIn("observed_at", metric)
            self.assertNotIn("sample_at", metric)
        for route in self.registry["routes"]:
            self.assertNotIn("example_payload", route)
            self.assertNotIn("sample_data", route)

    def test_sources_routes_and_metric_references_are_nonempty_and_unique(self):
        all_statuses = set(self.registry["status_vocabulary"])
        providers = self.registry["providers"]
        provider_ids = [p["id"] for p in providers]
        self.assertEqual(len(provider_ids), len(set(provider_ids)))
        for p in providers:
            self.assertTrue(p["evidence"]["path"])
            self.assertIn(p["implementation_status"], all_statuses)
        seen_routes = set()
        for route in self.registry["routes"]:
            self.assertIn(route["provider"], provider_ids)
            self.assertIn(route["implementation_status"], all_statuses)
            self.assertTrue(route["path"].startswith("/"))
            key = (route["method"], route["path"])
            self.assertNotIn(key, seen_routes)
            seen_routes.add(key)
        seen_metrics = set()
        for metric in self.registry["metrics"]:
            self.assertIn(metric["provider"], provider_ids)
            self.assertIn(metric["implementation_status"], all_statuses)
            self.assertNotIn(metric["id"], seen_metrics)
            seen_metrics.add(metric["id"])

    def test_current_bff_routes_are_declared_in_its_code(self):
        from backend.bff.route_map import ROUTES
        patterns = {(method, route.path) for route in ROUTES for method in route.methods}
        declared = {
            (route["method"], route["path"])
            for route in self.registry["routes"]
            if route["implementation_status"] == "bff_declared"
        }
        self.assertLessEqual(declared, patterns)

    def test_observed_gpu_prometheus_names_come_from_allowlisted_source(self):
        producer = (REPO / "web/tools/verified-infra.mjs").read_text(encoding="utf-8")
        observed = [
            m for m in self.registry["metrics"]
            if m["provider"] == "prometheus" and m["implementation_status"] == "repo"
        ]
        self.assertGreaterEqual(len(observed), 6)
        for m in observed:
            self.assertIn(m["source_metric"], producer, m["id"])

    def test_all_planned_endpoints_are_explicitly_not_bff_declared(self):
        from backend.bff.route_map import ROUTES
        current = {route.path for route in ROUTES}
        for route in self.registry["routes"]:
            if route["implementation_status"] == "planned":
                self.assertNotIn(route["path"], current, route["path"])


if __name__ == "__main__":
    unittest.main()
