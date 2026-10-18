"""引擎登记与健康：可见性是目录性的；``down`` 必须有正面证据。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import (  # noqa: E402
    ENDPOINT_PRIVATE_C,
    ENDPOINT_RETIRED,
    ENDPOINT_SHARED_A,
    ENDPOINT_SHARED_B,
    ENGINE_A,
    ENGINE_B,
    ENGINE_C,
    ENGINE_RETIRED,
    NOW,
    UID_ALICE,
    UID_CAROL,
    admin_principal,
    principal,
    registry,
)

from backend.observability import (  # noqa: E402
    ENGINE_STATES,
    QUALITY_UNKNOWN,
    REASON_NO_SAMPLE,
    REASON_SOURCE_UNAVAILABLE,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    AccessDenied,
    EngineProbeResult,
    EngineRecord,
    EngineRegistry,
    InvalidRequest,
    MockEngineProbe,
    UnavailableEngineProbe,
    probe_engine,
    resolve_observer_scope,
    status_from_probe,
)


def _scope(ctx, registry_obj=None):
    target = registry_obj if registry_obj is not None else registry()
    return resolve_observer_scope(ctx, available_endpoints=target.endpoints())


class EngineRecordTest(unittest.TestCase):
    def test_a_private_engine_must_declare_an_owner(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineRecord("e1", "ep1", "Private", visibility=VISIBILITY_PRIVATE)

    def test_unknown_visibility_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineRecord("e1", "ep1", "X", visibility="secret")

    def test_blank_ids_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineRecord("", "ep1", "X")
        with self.assertRaises(InvalidRequest):
            EngineRecord("e1", "   ", "X")

    def test_negative_replica_counts_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineRecord("e1", "ep1", "X", replicas_declared=-1)

    def test_non_integer_replica_counts_are_rejected(self) -> None:
        """副本数是**计数**。半个副本没有意义，而 2.5 会让「2/2 副本在线」判成 degraded。

        ``EngineProbeResult`` 对同类字段做了类型检查，这两半是同一个比较的两端，
        校验强度必须一致。
        """
        for planted in (2.5, "2", True):
            with self.subTest(planted=planted):
                with self.assertRaises(InvalidRequest):
                    EngineRecord("e1", "ep1", "X", replicas_declared=planted)

    def test_a_float_replica_count_cannot_silently_flip_health(self) -> None:
        """守住上一条的实际后果：合法的整数副本数不会把健康的引擎说成 degraded。"""
        record = EngineRecord("e1", "ep1", "X", replicas_declared=2)
        status, _ = status_from_probe(
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=2), record
        )
        self.assertEqual(status, "up")


class RegistryTest(unittest.TestCase):
    def test_duplicate_engine_ids_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineRegistry(
                [EngineRecord("e1", "ep1", "A"), EngineRecord("e1", "ep2", "B")]
            )

    def test_duplicate_endpoint_ids_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineRegistry(
                [EngineRecord("e1", "ep1", "A"), EngineRecord("e2", "ep1", "B")]
            )

    def test_endpoints_are_the_authorisation_universe(self) -> None:
        self.assertEqual(
            registry().endpoints(),
            {ENDPOINT_SHARED_A, ENDPOINT_SHARED_B, ENDPOINT_PRIVATE_C, ENDPOINT_RETIRED},
        )

    def test_lookup_by_endpoint_and_id(self) -> None:
        target = registry()
        self.assertEqual(target.by_endpoint(ENDPOINT_SHARED_A).engine_id, ENGINE_A)
        self.assertEqual(target.by_id(ENGINE_A).endpoint_id, ENDPOINT_SHARED_A)
        self.assertIsNone(target.by_endpoint("nope"))
        self.assertIsNone(target.by_id("nope"))


class VisibilityTest(unittest.TestCase):
    def test_a_plain_user_sees_only_their_authorised_shared_endpoint(self) -> None:
        records = registry().visible_to(_scope(principal(UID_ALICE)))
        self.assertEqual([record.engine_id for record in records], [ENGINE_A])

    def test_another_users_private_engine_is_absent_not_zeroed(self) -> None:
        """carol 的私有引擎在 alice 的可见集合里**不存在** —— 不出现，不是 0 副本。"""
        records = registry().visible_to(_scope(principal(UID_ALICE)))
        self.assertNotIn(ENGINE_C, {record.engine_id for record in records})
        self.assertNotIn(ENDPOINT_PRIVATE_C, {record.endpoint_id for record in records})

    def test_the_owner_sees_their_own_private_engine(self) -> None:
        ctx = principal(UID_CAROL, endpoints=(ENDPOINT_SHARED_B, ENDPOINT_PRIVATE_C))
        records = registry().visible_to(_scope(ctx))
        self.assertIn(ENGINE_C, {record.engine_id for record in records})

    def test_being_authorised_for_an_endpoint_does_not_require_owning_it(self) -> None:
        ctx = principal(UID_ALICE, endpoints=(ENDPOINT_SHARED_A, ENDPOINT_SHARED_B))
        records = registry().visible_to(_scope(ctx))
        self.assertEqual(
            {record.engine_id for record in records}, {ENGINE_A, ENGINE_B}
        )

    def test_admin_sees_everything_including_private_engines(self) -> None:
        records = registry().visible_to(_scope(admin_principal()))
        self.assertEqual(
            {record.engine_id for record in records},
            {ENGINE_A, ENGINE_B, ENGINE_C, ENGINE_RETIRED},
        )

    def test_require_visible_denies_absent_and_unauthorised_identically(self) -> None:
        scope = _scope(principal(UID_ALICE))
        messages = set()
        for candidate in (ENDPOINT_PRIVATE_C, ENDPOINT_RETIRED, "model-nope"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AccessDenied) as caught:
                    registry().require_visible(scope, candidate)
                messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1, messages)

    def test_require_visible_returns_the_record_when_allowed(self) -> None:
        scope = _scope(principal(UID_ALICE))
        record = registry().require_visible(scope, ENDPOINT_SHARED_A)
        self.assertEqual(record.engine_id, ENGINE_A)

    def test_non_scope_argument_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            registry().visible_to("not-a-scope")

    def test_admin_viewing_a_foreign_user_still_sees_the_admin_catalogue(self) -> None:
        """``requested_uid`` 只改变**用量**的归属视角；引擎可见性仍按管理 scope 给全量。"""
        scope = resolve_observer_scope(
            admin_principal(),
            available_endpoints=registry().endpoints(),
            requested_uid=UID_CAROL,
        )
        records = registry().visible_to(scope)
        self.assertIn(ENGINE_C, {record.engine_id for record in records})
        self.assertEqual(scope.viewing_uid, UID_CAROL)


class StatusFromProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.record = EngineRecord(
            ENGINE_A, ENDPOINT_SHARED_A, "A", replicas_declared=2
        )

    def test_an_inconclusive_probe_is_unknown_not_down(self) -> None:
        status, reason = status_from_probe(
            EngineProbeResult(up=None, reason=REASON_SOURCE_UNAVAILABLE), self.record
        )
        self.assertIsNone(status)
        self.assertEqual(reason, REASON_SOURCE_UNAVAILABLE)

    def test_a_positive_failure_is_down(self) -> None:
        status, reason = status_from_probe(
            EngineProbeResult(up=False, observed_at=NOW), self.record
        )
        self.assertEqual(status, "down")
        self.assertIsNone(reason)

    def test_all_replicas_up_is_up(self) -> None:
        status, _ = status_from_probe(
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=2), self.record
        )
        self.assertEqual(status, "up")

    def test_a_missing_replica_is_degraded(self) -> None:
        status, _ = status_from_probe(
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=1), self.record
        )
        self.assertEqual(status, "degraded")

    def test_replica_shortfall_without_a_declared_count_is_not_degraded(self) -> None:
        record = EngineRecord("e", "ep", "X")
        status, _ = status_from_probe(
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=1), record
        )
        self.assertEqual(status, "up")

    def test_up_without_replica_information_is_up(self) -> None:
        status, _ = status_from_probe(
            EngineProbeResult(up=True, observed_at=NOW), self.record
        )
        self.assertEqual(status, "up")

    def test_status_is_either_a_declared_state_or_unknown(self) -> None:
        """``none`` **不**是 ``ENGINE_STATES`` 的成员 —— 枚举里没有 ``unknown``，
        「测不到」由 ``None`` + ``quality`` 表达，而不是一个叫 unknown 的状态值。"""
        for probe in (
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=2),
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=0),
            EngineProbeResult(up=False, observed_at=NOW),
        ):
            with self.subTest(probe=probe):
                status, reason = status_from_probe(probe, self.record)
                self.assertIn(status, ENGINE_STATES)
                self.assertIsNone(reason)
        self.assertNotIn("unknown", ENGINE_STATES)

        status, reason = status_from_probe(
            EngineProbeResult(up=None, reason=REASON_NO_SAMPLE), self.record
        )
        self.assertIsNone(status)
        self.assertEqual(reason, REASON_NO_SAMPLE)


class ProbeResultValidationTest(unittest.TestCase):
    def test_inconclusive_probe_requires_a_reason(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineProbeResult(up=None)

    def test_conclusive_probe_requires_observed_at(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineProbeResult(up=True)

    def test_negative_replica_counts_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineProbeResult(up=True, observed_at=NOW, replicas_up=-1)

    def test_non_boolean_up_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            EngineProbeResult(up="yes", observed_at=NOW)


class ProbeEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.record = EngineRecord(
            ENGINE_A, ENDPOINT_SHARED_A, "A", replicas_declared=2
        )

    def test_unavailable_probe_yields_unknown_readings(self) -> None:
        report = probe_engine(
            self.record, UnavailableEngineProbe(), source="probe", now=NOW
        )
        self.assertIsNone(report.status)
        payload = report.to_dict()
        readings = payload["readings"]
        self.assertIsNone(readings["engine.up"]["state"])
        self.assertEqual(
            readings["engine.up"]["reason"], REASON_SOURCE_UNAVAILABLE
        )
        self.assertEqual(readings["engine.replicas"]["quality"], QUALITY_UNKNOWN)
        self.assertIsNone(readings["engine.latency_p95_ms"]["value"])

    def test_probe_exceptions_are_treated_as_inconclusive(self) -> None:
        def exploding(record):
            raise RuntimeError("probe crashed")

        report = probe_engine(self.record, exploding, source="probe", now=NOW)
        self.assertIsNone(report.status)

    def test_a_non_probe_result_is_treated_as_inconclusive(self) -> None:
        report = probe_engine(
            self.record, lambda record: "up", source="probe", now=NOW
        )
        self.assertIsNone(report.status)

    def test_a_healthy_engine_reports_replicas_and_latency(self) -> None:
        probe = MockEngineProbe(
            {
                ENGINE_A: EngineProbeResult(
                    up=True, observed_at=NOW, replicas_up=2, latency_p95_ms=880.5
                )
            }
        )
        report = probe_engine(self.record, probe, source="probe", now=NOW)
        self.assertEqual(report.status, "up")
        readings = report.to_dict()["readings"]
        self.assertEqual(readings["engine.replicas"]["value"], 2)
        self.assertEqual(readings["engine.latency_p95_ms"]["value"], 880.5)

    def test_engine_readings_carry_the_full_envelope(self) -> None:
        probe = MockEngineProbe(
            {ENGINE_A: EngineProbeResult(up=True, observed_at=NOW, replicas_up=2)}
        )
        readings = probe_engine(self.record, probe, source="probe", now=NOW).to_dict()[
            "readings"
        ]
        for metric_id, reading in readings.items():
            with self.subTest(metric=metric_id):
                for key in ("source", "observed_at", "quality", "display_quality", "freshness"):
                    self.assertIn(key, reading)

    def test_engine_readings_are_never_physical(self) -> None:
        probe = MockEngineProbe(
            {ENGINE_A: EngineProbeResult(up=True, observed_at=NOW, replicas_up=2)}
        )
        readings = probe_engine(self.record, probe, source="probe", now=NOW).to_dict()[
            "readings"
        ]
        for reading in readings.values():
            self.assertFalse(reading["physical"])
            self.assertEqual(reading["scope"], "engine")

    def test_unknown_status_is_explained_in_the_notes(self) -> None:
        report = probe_engine(
            self.record, UnavailableEngineProbe(), source="probe", now=NOW
        )
        self.assertTrue(any("down" in note for note in report.notes))

    def test_report_is_json_serialisable(self) -> None:
        import json

        probe = MockEngineProbe()
        payload = probe_engine(self.record, probe, source="probe", now=NOW).to_dict()
        json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["engine_id"], ENGINE_A)
        self.assertEqual(payload["visibility"], VISIBILITY_SHARED)


if __name__ == "__main__":
    unittest.main()
