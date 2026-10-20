"""``backend/observability`` 接进 BFF 之后的行为。

这一层最重要的性质不是「有数据」，而是**哪些数字允许出现**：

* 指标卡里的数字必须有前端白名单来源（gateway-usage-export / nvml-verified /
  prometheus-readonly）。本部署是合成来源，因此卡片一律 ``value: null`` +
  ``unknown`` + 原因 —— 把合成数据标成白名单来源会让 UI 把一个没人量过的数字
  渲染成「已验证」。
* 曲线可以有合成数：``trends.synthetic`` 是契约给的出口，代价是必须自报合成。
* 缺测的桶在序列里是 ``null``，不是 0。
* 整卡显存**结构上**不可能按人给：观测模块的 device 读数不带 uid，本层也没有
  任何把 host 读数按主体切分的代码。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from backend.observability import dto as obs_dto
from backend.bff import observability_adapter as obs_adapter
from tests.bff import support as S


class MetricCardTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def cards(self, name, *, admin=False):
        client = self.h.admin_client(name) if admin else self.h.client(name)
        return S.ops_of(S.fetch_ops(client))["metrics"]["items"]

    def test_every_card_is_null_with_a_stated_reason(self):
        for card in self.cards("alice"):
            self.assertIsNone(card["value"], card["name"])
            self.assertEqual(card["state"], "unknown")
            self.assertIsNone(card["source"])
            self.assertIsNone(card["updated_at"])
            self.assertTrue(card["reason"], card["name"])

    def test_a_measured_reading_is_withheld_because_its_source_is_not_trusted(self):
        """The reading *has* a value; the contract refuses to show it as one."""
        cards = {card["name"]: card for card in self.cards("alice")}
        requests = cards["principal.requests"]
        self.assertIsNone(requests["value"])
        # Not "no data" — the module measured it, and the reason says why the
        # number is not on screen.
        self.assertEqual(requests["reason"], "no_verified_metric_source")

    def test_a_genuinely_missing_reading_reports_the_modules_own_reason(self):
        cards = {card["name"]: card for card in self.cards("alice")}
        # No queue samples were produced, so the module says `no_sample`.
        queue_cards = [c for c in cards.values() if "queue" in c["name"]]
        self.assertTrue(queue_cards)
        for card in queue_cards:
            self.assertIsNone(card["value"])
            self.assertIn(card["reason"], {"no_sample", "no_verified_metric_source"})

    def test_the_card_set_comes_from_the_observability_catalogue(self):
        for card in self.cards("alice"):
            self.assertIn(card["name"], obs_dto.CATALOGUE)
            self.assertEqual(card["kind"], "observability")

    def test_no_card_ever_carries_a_per_user_gpu_figure(self):
        """There is no such metric in the catalogue, and none can appear here."""
        for name in ("alice", "carol"):
            for card in self.cards(name):
                self.assertNotIn("principal.gpu", card["name"])
                if card["name"].startswith(("device.", "node.")):
                    self.assertIsNone(card["value"])

    def test_host_readings_are_only_requested_for_a_session_that_may_see_them(self):
        member = {card["name"] for card in self.cards("alice")}
        admin = {card["name"] for card in self.cards("carol", admin=True)}
        self.assertFalse(
            {name for name in member if name.startswith(("device.", "node."))},
            "a member was handed host-wide readings",
        )
        self.assertTrue(
            {name for name in admin if name.startswith("device.gpu")},
            "the host-scoped session got no device readings",
        )


class MetricFreshnessMappingTests(unittest.TestCase):
    """A time-expired verified source must never become an available card."""

    NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def reading(self, seconds_old=5, *, source="gateway-usage-export"):
        return obs_dto.MetricReading.from_metric(
            "principal.requests",
            value=42,
            source=source,
            observed_at=self.NOW - timedelta(seconds=seconds_old),
            now=self.NOW,
        ).to_dict()

    def test_fresh_verified_reading_remains_available(self):
        reading = self.reading()
        self.assertEqual(reading["freshness"]["stale"], False)
        card = obs_adapter._metric_card("principal.requests", reading)
        self.assertEqual(card["state"], "available")
        self.assertEqual(card["value"], 42)
        self.assertEqual(card["source"], "gateway-usage-export")
        self.assertIsNone(card["reason"])

    def test_stale_verified_reading_withholds_old_number(self):
        reading = self.reading(seconds_old=600)
        self.assertEqual(reading["display_quality"], obs_dto.DISPLAY_STALE)
        self.assertIs(reading["freshness"]["stale"], True)
        card = obs_adapter._metric_card("principal.requests", reading)
        self.assertEqual(card["state"], "unknown")
        self.assertIsNone(card["value"])
        self.assertIsNone(card["source"])
        self.assertIsNone(card["updated_at"])
        self.assertEqual(card["reason"], "stale_sample")

    def test_future_clock_skew_is_not_considered_fresh(self):
        reading = self.reading(seconds_old=-600)
        self.assertIs(reading["freshness"]["clock_skew"], True)
        card = obs_adapter._metric_card("principal.requests", reading)
        self.assertEqual(card["state"], "unknown")
        self.assertIsNone(card["value"])
        self.assertEqual(card["reason"], "unverified_freshness")

    def test_missing_freshness_is_fail_closed(self):
        reading = self.reading()
        reading.pop("freshness")
        card = obs_adapter._metric_card("principal.requests", reading)
        self.assertEqual(card["state"], "unknown")
        self.assertIsNone(card["value"])
        self.assertEqual(card["reason"], "unverified_freshness")

    def test_synthetic_source_stays_unknown_even_when_recent(self):
        reading = self.reading(source="synthetic-fixture")
        card = obs_adapter._metric_card("principal.requests", reading)
        self.assertEqual(card["state"], "unknown")
        self.assertIsNone(card["value"])
        self.assertEqual(card["reason"], "no_verified_metric_source")


class TrendTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def trends(self, name, *, admin=False):
        client = self.h.admin_client(name) if admin else self.h.client(name)
        return S.ops_of(S.fetch_ops(client))["trends"]

    def test_the_curve_self_declares_as_synthetic(self):
        trends = self.trends("alice")
        self.assertIsNotNone(trends)
        self.assertTrue(trends["synthetic"])
        self.assertTrue(trends["reason"], "a synthetic series must say so")
        self.assertIsNone(trends["source"])
        self.assertEqual(trends["window"], "24h")

    def test_missing_buckets_are_gaps_not_zeroes(self):
        (series,) = [s for s in self.trends("alice")["series"] if s["kind"] == "requests"]
        self.assertEqual(len(series["points"]), 24)
        self.assertTrue(
            any(point is None for point in series["points"]),
            "the fixture should leave gaps so this property is actually tested",
        )
        self.assertTrue(any(point is not None for point in series["points"]))
        for point in series["points"]:
            if point is not None:
                self.assertNotEqual(point, 0, "a gap must never be rendered as 0")

    def test_the_three_contract_kinds_are_the_ones_emitted(self):
        kinds = {series["kind"] for series in self.trends("alice")["series"]}
        self.assertTrue(kinds)
        self.assertTrue(kinds <= set(obs_adapter.TREND_KINDS))

    def test_the_global_series_exists_only_for_an_admin(self):
        member_scopes = {s["scope"] for s in self.trends("alice")["series"]}
        self.assertEqual(member_scopes, {"mine"})
        admin_scopes = {s["scope"] for s in self.trends("carol", admin=True)["series"]}
        self.assertEqual(admin_scopes, {"mine", "global"})

    def test_a_session_with_no_visible_endpoint_gets_no_curve_at_all(self):
        """No authorised endpoint ⇒ the module counts nothing ⇒ no series.

        Not a flat zero line: the honest rendering of "nothing was measured" is
        an empty state, and that is what the payload produces.
        """
        self.assertIsNone(self.trends("bob"))

    def test_a_series_with_no_measurements_is_omitted_rather_than_drawn_empty(self):
        for trends in (self.trends("alice"), self.trends("carol", admin=True)):
            for series in trends["series"]:
                self.assertTrue(
                    any(point is not None for point in series["points"]),
                    f"{series['name']} was emitted with no measurements at all",
                )


class IsolationTests(unittest.TestCase):
    """`B` can see `A`'s usage only if the rows are B's own."""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def usage(self, name, scope="self", *, admin=False):
        return self.h.observability.usage_dto(
            identity=self.h.trusted_principal(name, admin=admin), scope=scope
        )

    def test_a_member_cannot_see_anothers_usage(self):
        S.share(self.h.client("alice"), self.ids["deployment_id"], mode="all")
        bob = self.usage("bob")
        alice = self.usage("alice")
        # Both see the same endpoint now, but each counts only its own key's
        # rows. The fixture gives each key a different row count so "only mine"
        # is observable instead of accidentally equal.
        self.assertNotEqual(bob["counters"]["calls"], alice["counters"]["calls"])
        self.assertEqual(bob["scope"]["uid"], self.h.principal_of("bob"))
        self.assertNotEqual(bob["scope"]["uid"], alice["scope"]["uid"])

    def test_a_member_with_no_authorised_endpoint_counts_nothing(self):
        """No visible endpoint ⇒ no corpus for that session ⇒ nothing to count.

        The rows of other subjects are not "filtered out" and then counted — they
        are never generated for this session's view, and the module drops any
        that are anyway. Both halves land on zero.
        """
        bob = self.usage("bob")
        self.assertEqual(bob["counters"]["calls"], 0)
        self.assertEqual(bob["buckets"]["observed_count"], 0)
        self.assertEqual(bob["metrics"]["principal.requests"]["value"], None)

    def test_the_admin_overview_covers_several_subjects_but_is_metadata_only(self):
        admin = self.usage("carol", scope="global", admin=True)
        self.assertEqual(admin["kind"], "usage.admin")
        self.assertTrue(admin["scope"]["admin"])
        # A subject summary, never per-call content.
        blob = str(admin)
        for field in ("prompt", "completion", "messages", "request_id"):
            self.assertNotIn(f"'{field}'", blob)

    def test_a_member_cannot_ask_for_the_global_overview(self):
        from backend.observability.errors import AccessDenied

        with self.assertRaises(AccessDenied):
            self.usage("alice", scope="global")

    def test_the_session_uid_is_the_one_attribution_uses(self):
        """The uid comes from the trusted directory, not from the request."""
        principal = self.h.trusted_principal("bob")
        requester = self.h.observability.requester_for(principal)
        self.assertEqual(requester.uid, principal.principal)
        self.assertNotIn("observability", "".join(sorted(requester.roles)))


class DegradationTests(unittest.TestCase):
    """A session with no scopes is refused, not served a page of blanks.

    This class used to manufacture that session by writing an unknown role into
    the user row. Scopes no longer come from that column — they come from the
    session's elevation — so the condition is no longer reachable through the
    database, which is the point of the change. What is still worth asserting is
    that the fail-closed path *works*, so it is exercised with a scope-less
    identity built directly, plus the counterpart that keeps this class from
    quietly becoming vacuous.
    """

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.h.client("dave")

    def _scopeless_identity(self):
        """A real principal with every scope stripped, as an unknown role gives."""
        from dataclasses import replace

        principal = self.h.app.resolve_session(self.h.session_id("dave"))
        return replace(
            principal,
            effective_role="no_such_role",
            portal_scopes=(),
            requester=replace(principal.requester, scopes=frozenset()),
        )

    def test_a_session_with_no_registry_scope_is_denied_not_served_a_half_page(self):
        """The registry ACL is the authority for the catalogue, so it refuses.

        The front end has a `denied` state with a retry control for exactly this;
        answering with a page whose every panel is empty-but-200 would suggest
        "there is nothing here" instead of "you may not look".
        """
        from backend.registry import errors as registry_errors

        with self.assertRaises(registry_errors.AccessDenied):
            self.h.service.policy(self._scopeless_identity().requester).visible_endpoints()

    def test_the_adapter_reports_its_own_unavailability(self):
        available, reason = self.h.observability.availability(
            identity=self._scopeless_identity()
        )
        self.assertFalse(available)
        self.assertEqual(reason, "missing_required_scope")

    def test_every_portal_session_does_carry_the_self_read_scope(self):
        """The counterpart: a signed-in member is not a scope-less session.

        Without this, the two tests above could pass while the module had become
        unreachable for everybody.
        """
        for name in ("dave", "alice", "carol"):
            with self.subTest(name=name):
                available, reason = self.h.observability.availability(
                    identity=self.h.trusted_principal(name)
                )
                self.assertTrue(available, reason)

    def test_a_member_still_cannot_reach_the_global_overview(self):
        """Self-read is not admin-read; the degradation is a real distinction."""
        member = self.h.trusted_principal("carol")
        with self.assertRaises(Exception):
            self.h.observability.usage_dto(identity=member, scope="global")


class EngineStatusTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def test_the_engine_table_is_built_from_the_registrys_visible_engines(self):
        registry = obs_adapter.engine_registry_from_bff(
            service=self.h.service, identity=self.h.trusted_principal("alice")
        )
        self.assertEqual([record.engine_id for record in registry.all_records()], [self.ids["engine_id"]])

    def test_engine_status_is_a_test_double_and_says_so(self):
        dto = self.h.observability.engine_status_dto(identity=self.h.trusted_principal("alice"))
        self.assertTrue(dto["synthetic"])
        self.assertTrue(any("SYNTHETIC" in hint for hint in dto["display_hints"]))
        self.assertIn(dto["source"], {"synthetic-fixture", "mock-engine-probe"})

    def test_an_unprobed_engine_is_unknown_rather_than_down(self):
        """The default probe (no probe at all) never reports `down`."""
        from backend.observability.engines import UnavailableEngineProbe
        from backend.bff import stacks

        stack = stacks.build_observability_stack(
            directory=self.h.key_directory,
            engine_registry=lambda identity: obs_adapter.engine_registry_from_bff(
                service=self.h.service, identity=identity
            ),
            engine_probe=UnavailableEngineProbe(),
            provenance=stacks.synthetic_provenance(),
        )
        adapter = obs_adapter.ModuleObservabilityAdapter(stack)
        dto = adapter.engine_status_dto(identity=self.h.trusted_principal("alice"))
        for engine in dto["engines"]:
            self.assertIsNone(engine["status"], engine["engine_id"])
            self.assertEqual(engine["quality"], "unknown")


class DispatchTests(unittest.TestCase):
    def test_the_synthetic_corpus_is_marked_and_keyed_for_attribution(self):
        """Rows carry no uid; attribution is `key_id -> uid` and nothing else."""
        import json

        h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(h.close)
        corpus = S.bff_stacks.demo_gateway_corpus(
            h.key_directory, now=h.now, endpoints=["ep-demo"]
        )
        self.assertTrue(corpus.strip())
        for line in corpus.strip().splitlines():
            row = json.loads(line)
            self.assertEqual(row["fixture"], "SYNTHETIC-BFF")
            self.assertNotIn("uid", row)
            self.assertNotIn("principal", row)
            self.assertNotIn("x_principal", row)
            self.assertTrue(h.key_directory.lookup(row["key_id"]) is not None)

    def test_no_corpus_without_a_visible_endpoint(self):
        h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(h.close)
        self.assertEqual(
            S.bff_stacks.demo_gateway_corpus(h.key_directory, now=h.now, endpoints=[]), ""
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
