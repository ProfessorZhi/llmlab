"""Per-metric Ops display lease derives from the authoritative metric catalogue.

No GPU, browser, production host, identity, network or environment access.
"""
import unittest
from datetime import datetime, timedelta, timezone

from backend.bff.ops import _ops_metric_expiries
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


if __name__ == "__main__":
    unittest.main()
