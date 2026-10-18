"""报告来源（provenance）的契约：来源怎么被盖章、归属怎么被核实、引用怎么被约束。

这一组测的是**构造期**的防线——一份记录/快照要么自洽，要么在进入目录之前就炸掉：

* 引用在构造期规范化，伪造路径进不了目录；
* 来源是**记录自带的字段**，而快照强制它与自己一致 → 一条 legacy 记录永远进不了
  evalscope 快照；
* 状态与条数必须自洽（``ok`` 至少一条、``empty`` 必须零条）→ "0" 与"未知"不能互相冒充；
* 上游文本经 :func:`safe_text` 消毒，标记类字符不会活到 DTO。

本模块不读任何真实报告、不联网、不起模型；所有对象都是合成的。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.evaluation.errors import ReportPathRejected, ReportSourceMalformed
from backend.evaluation.models import metric_known, metric_unknown
from backend.evaluation.provenance import (
    DIMENSION_QUALITY,
    KNOWN_REPORT_SOURCES,
    OPAQUE_REPORT_PREFIX,
    REPORT_DIMENSIONS,
    SOURCE_EMPTY,
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    SOURCE_OK,
    SOURCE_STATES,
    SOURCE_UNREACHABLE,
    ReportOwner,
    ReportRecord,
    ReportSourceSnapshot,
    opaque_report_ref,
    safe_text,
    unconfigured_snapshot,
    validate_source,
)

from tests.evaluation.support import (
    REPORT_REF_EVALSCOPE,
    REPORT_REF_LEGACY,
    report_owner,
    report_record,
    report_snapshot,
)


class SourceVocabularyTests(unittest.TestCase):
    def test_the_two_sources_are_distinct_and_known(self) -> None:
        self.assertEqual(KNOWN_REPORT_SOURCES, (SOURCE_EVALSCOPE, SOURCE_LEGACY_BENCHMARK))
        self.assertNotEqual(SOURCE_EVALSCOPE, SOURCE_LEGACY_BENCHMARK)

    def test_an_unknown_source_is_refused(self) -> None:
        for bad in ("evalscope ", "EVALSCOPE", "benchmark", "", None, 7):
            with self.subTest(source=bad):
                with self.assertRaises(ReportSourceMalformed):
                    validate_source(bad)

    def test_a_record_refuses_an_unknown_source(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_record("mystery-source", REPORT_REF_EVALSCOPE)

    def test_the_state_vocabulary_is_closed(self) -> None:
        self.assertIn(SOURCE_OK, SOURCE_STATES)
        self.assertIn(SOURCE_EMPTY, SOURCE_STATES)
        with self.assertRaises(ReportSourceMalformed):
            report_snapshot(SOURCE_EVALSCOPE, "healthy", observed_at=None)


class ReportReferenceTests(unittest.TestCase):
    def test_a_reference_is_normalized_at_construction(self) -> None:
        record = report_record(SOURCE_EVALSCOPE, "  evalscope/reports/a.json  ")
        self.assertEqual(record.ref, "evalscope/reports/a.json")

    def test_forged_references_are_refused_at_construction(self) -> None:
        for bad in (
            "/etc/passwd.json",
            "../../secret.json",
            "a/b/../../../c.json",
            "http://evil.example/x.json",
            "a\\b.json",
            "C:/x.json",
            "a/../b.json",
            "a//b.json",
            "a/b.exe",
            "",
        ):
            with self.subTest(ref=bad):
                with self.assertRaises((ReportPathRejected, ReportSourceMalformed)):
                    report_record(SOURCE_EVALSCOPE, bad)

    def test_the_opaque_reference_is_not_the_path(self) -> None:
        record = report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE)
        ref_id = record.ref_id
        self.assertTrue(ref_id.startswith(OPAQUE_REPORT_PREFIX))
        self.assertNotIn(REPORT_REF_EVALSCOPE, ref_id)
        self.assertNotIn("/", ref_id)

    def test_the_opaque_reference_is_stable_and_source_scoped(self) -> None:
        a1 = report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE, report_id="r-1")
        a2 = report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE, report_id="r-1")
        b = report_record(SOURCE_LEGACY_BENCHMARK, REPORT_REF_EVALSCOPE, report_id="r-1")
        self.assertEqual(a1.ref_id, a2.ref_id)
        # 同样的上游 id 与引用，在两个来源下必须是两个 id。
        self.assertNotEqual(a1.ref_id, b.ref_id)
        self.assertEqual(
            opaque_report_ref(SOURCE_EVALSCOPE, "r-1", REPORT_REF_EVALSCOPE), a1.ref_id
        )


class DimensionTests(unittest.TestCase):
    def test_unknown_dimension_names_are_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_record(
                SOURCE_EVALSCOPE,
                REPORT_REF_EVALSCOPE,
                dimensions={"mystery_metric": metric_known("mystery_metric", 1.0)},
            )

    def test_a_dimension_key_must_agree_with_its_sample(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_record(
                SOURCE_EVALSCOPE,
                REPORT_REF_EVALSCOPE,
                dimensions={DIMENSION_QUALITY: metric_known("something_else", 1.0)},
            )

    def test_missing_dimensions_are_reported_and_are_never_zero(self) -> None:
        record = report_record(
            SOURCE_EVALSCOPE,
            REPORT_REF_EVALSCOPE,
            dimensions={DIMENSION_QUALITY: metric_known(DIMENSION_QUALITY, 0.5)},
        )
        missing = record.missing_dimensions
        self.assertNotIn(DIMENSION_QUALITY, missing)
        self.assertEqual(set(missing), set(REPORT_DIMENSIONS) - {DIMENSION_QUALITY})
        absent = record.dimension("throughput_rps")
        self.assertEqual(absent.state, "unknown")
        self.assertIsNone(absent.value)

    def test_a_dimension_marked_unknown_still_counts_as_missing_but_is_never_zero(self) -> None:
        record = report_record(
            SOURCE_EVALSCOPE,
            REPORT_REF_EVALSCOPE,
            dimensions={DIMENSION_QUALITY: metric_unknown(DIMENSION_QUALITY)},
        )
        # "提到了但这次没取值"与"根本没提"都是**没有已知取值**，因此都算缺失——
        # 但它们的取值都是 None，**不是 0**。区别由 note 承载。
        self.assertIn(DIMENSION_QUALITY, record.missing_dimensions)
        sample = record.dimension(DIMENSION_QUALITY)
        self.assertEqual(sample.state, "unknown")
        self.assertIsNone(sample.value)


class SnapshotCoherenceTests(unittest.TestCase):
    def test_an_ok_snapshot_must_carry_at_least_one_record(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, records=())

    def test_an_empty_snapshot_must_carry_no_records(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_snapshot(
                SOURCE_EVALSCOPE,
                SOURCE_EMPTY,
                records=[report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE)],
            )

    def test_an_unconfigured_snapshot_must_carry_no_records(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_snapshot(
                SOURCE_EVALSCOPE,
                "unconfigured",
                records=[report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE)],
            )

    def test_a_snapshot_refuses_a_record_from_another_source(self) -> None:
        """**来源隔离**的核心断言：legacy 记录进不了 evalscope 快照。"""
        legacy_record = report_record(SOURCE_LEGACY_BENCHMARK, REPORT_REF_LEGACY)
        with self.assertRaises(ReportSourceMalformed):
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, records=[legacy_record])

    def test_an_unreachable_snapshot_carries_the_reason(self) -> None:
        snapshot = report_snapshot(
            SOURCE_LEGACY_BENCHMARK, SOURCE_UNREACHABLE, observed_at=None, detail="boom"
        )
        self.assertEqual(snapshot.count, 0)
        self.assertEqual(snapshot.detail, "boom")
        self.assertIsNone(snapshot.to_dict()["observed_at"])

    def test_unconfigured_snapshot_is_not_a_counted_zero(self) -> None:
        snapshot = unconfigured_snapshot(SOURCE_EVALSCOPE)
        self.assertEqual(snapshot.state, "unconfigured")
        self.assertEqual(snapshot.count, 0)
        self.assertNotEqual(snapshot.state, SOURCE_EMPTY)


class OwnerTests(unittest.TestCase):
    def test_an_owner_needs_a_principal_uid_and_project(self) -> None:
        for kwargs in (
            {"principal": "", "uid": 4000, "project_id": "p"},
            {"principal": "alice", "uid": -1, "project_id": "p"},
            {"principal": "alice", "uid": 4001, "project_id": ""},
            {"principal": "alice", "uid": True, "project_id": "p"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ReportSourceMalformed):
                    ReportOwner(**kwargs)

    def test_an_unowned_record_is_allowed(self) -> None:
        record = ReportRecord(
            source=SOURCE_LEGACY_BENCHMARK,
            source_report_id="r-1",
            ref=REPORT_REF_LEGACY,
            synthetic=False,
            owner=None,
        )
        self.assertTrue(record.unowned)


class SafeTextTests(unittest.TestCase):
    def test_markup_characters_do_not_survive(self) -> None:
        self.assertEqual(safe_text("<script>alert(1)</script>"), "scriptalert(1)/script")

    def test_control_characters_are_dropped_and_whitespace_collapsed(self) -> None:
        self.assertEqual(safe_text("a\x00b\t c\n"), "ab c")

    def test_length_is_bounded(self) -> None:
        self.assertEqual(len(safe_text("x" * 5000)), 200)

    def test_empty_and_none(self) -> None:
        self.assertIsNone(safe_text(None))
        self.assertIsNone(safe_text("   "))
        self.assertIsNone(safe_text("<>"))

    def test_a_non_string_label_is_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            safe_text(7)


class ExplicitSyntheticTests(unittest.TestCase):
    def test_synthetic_has_no_default_on_a_record(self) -> None:
        with self.assertRaises(TypeError):
            ReportRecord(  # type: ignore[call-arg]
                source=SOURCE_EVALSCOPE, source_report_id="r", ref=REPORT_REF_EVALSCOPE
            )

    def test_synthetic_has_no_default_on_a_snapshot(self) -> None:
        with self.assertRaises(TypeError):
            ReportSourceSnapshot(source=SOURCE_EVALSCOPE, state=SOURCE_EMPTY)  # type: ignore[call-arg]

    def test_a_non_boolean_synthetic_is_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, synthetic="yes")  # type: ignore[arg-type]


class TimestampTests(unittest.TestCase):
    def test_naive_timestamps_are_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            report_record(
                SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE, observed_at=datetime(2026, 10, 15, 12, 0)
            )

    def test_aware_timestamps_round_trip(self) -> None:
        moment = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
        record = report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE, created_at=moment)
        self.assertEqual(record.created_at, moment)

    def test_the_default_owner_helper_matches_the_host_directory(self) -> None:
        owner = report_owner("alice")
        self.assertEqual(owner.uid, 4001)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
