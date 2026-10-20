"""Per-metric Ops display lease derives from the authoritative metric catalogue.

No GPU, browser, production host, identity, network or environment access.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.bff import ops as bff_ops
from backend.bff.ops import _ops_metric_expiries
from tests.bff import support as S
from backend.observability.dto import CATALOGUE


class OpsMetricExpiryTests(unittest.TestCase):
    NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def card(self, **overrides):
        record = {
            "name": "principal.requests",
            "state": "available",
            "value": 42,
            "source": "gateway-usage-export",
            "updated_at": "2026-10-20T11:59:55Z",
        }
        record.update(overrides)
        return record

    def test_source_catalogue_deadline_for_verified_metric(self):
        ttl = CATALOGUE["principal.requests"].stale_after_seconds
        self.assertIsNotNone(ttl)
        expiry = self.NOW - timedelta(seconds=5) + timedelta(seconds=ttl)
        expected = expiry.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(
            _ops_metric_expiries([self.card()]),
            {"principal.requests": expected},
        )

    def test_null_or_unknown_readings_have_no_deadline(self):
        for card in [
            self.card(state="unknown", value=None),
            self.card(value=None),
            self.card(state="denied"),
        ]:
            with self.subTest(card=card):
                self.assertEqual(_ops_metric_expiries([card]), {})

    def test_bad_or_absent_time_cannot_create_a_lease(self):
        for stamp in [None, "", "not-a-time", "2026-10-20T11:59:55",
                      "2026-10-20T00:00:00+99:99"]:
            with self.subTest(stamp=stamp):
                self.assertEqual(
                    _ops_metric_expiries([self.card(updated_at=stamp)]), {},
                )

    def test_unrecognised_metric_is_not_granted_a_made_up_ttl(self):
        self.assertEqual(_ops_metric_expiries([
            self.card(name="new.metric.no.catalogue"),
        ]), {})

    def test_mixed_metrics_expose_only_visible_deadlines(self):
        cards = [self.card(), self.card(name="missing.metric", value=9),
                 self.card(name="principal.no.sample", state="unknown", value=None)]
        leases = _ops_metric_expiries(cards)
        self.assertEqual(list(leases), ["principal.requests"])
        self.assertNotIn("subject", str(leases))
        self.assertNotIn("uid", str(leases))



class VisibleOpsMetricIntegrationTests(unittest.TestCase):
    """The actual BFF aggregate must preserve source-derived per-metric leases."""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        S.register_baseline(self.h, "alice")

    def test_build_ops_adds_leases_only_for_visible_numeric_metrics(self):
        class FixtureAdapter:
            def metrics(self, *, identity, is_admin):
                del identity, is_admin
                return [
                    {
                        "name": "principal.requests",
                        "label": "Requests",
                        "kind": "observability",
                        "unit": "次",
                        "value": 42,
                        "state": "available",
                        "source": "gateway-usage-export",
                        "updated_at": "2026-10-20T11:59:55Z",
                        "reason": None,
                    },
                    {
                        "name": "principal.no_sample",
                        "label": "No sample",
                        "kind": "observability",
                        "unit": "次",
                        "value": None,
                        "state": "unknown",
                        "source": None,
                        "updated_at": None,
                        "reason": "no_sample",
                    },
                ]

            def trends(self, *, identity, is_admin, window):
                del identity, is_admin, window
                return None

        principal = self.h.app.resolve_session(self.h.session_id("alice"))
        payload = bff_ops.build_ops(
            principal=principal, service=self.h.service,
            observability=FixtureAdapter(),
        )
        items = payload["metrics"]["items"]
        names = {item["name"] for item in items}
        expires = payload["metrics"]["expires_at"]
        self.assertEqual(len(items), 2)
        self.assertEqual(set(expires), {"principal.requests"})
        self.assertTrue(set(expires).issubset(names))
        self.assertEqual(
            expires["principal.requests"],
            _ops_metric_expiries(items)["principal.requests"],
        )
        self.assertEqual(items[1]["state"], "unknown")
        self.assertIsNone(items[1]["value"])
        self.assertNotIn("uid", str(expires))
        self.assertNotIn("subject", str(expires))

    def test_detached_adapter_does_not_invent_expiry_metadata(self):
        principal = self.h.app.resolve_session(self.h.session_id("alice"))
        payload = bff_ops.build_ops(
            principal=principal, service=self.h.service,
            observability=bff_ops.DetachedObservabilityAdapter(),
        )
        self.assertNotIn("expires_at", payload["metrics"])
        self.assertTrue(all(c["value"] is None for c in payload["metrics"]["items"]))




if __name__ == "__main__":
    unittest.main()
