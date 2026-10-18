"""只读报告目录：ACL、0 vs 未知、新鲜度、缺失维度、删除线。

这一组只碰**目录本身**（无 HTTP、无持久层）：给定快照与一个受信主体，目录给出的
可见集合是否与规则一致。ACL 的三条硬约束在这里被逐条断言：

* 跨 UID 不可见；
* 归属无法核实的历史记录默认对所有人隐藏（连 ``admin:read`` 都看不到）；
* 只有显式带 flag 的 ``admin:manage`` 才能把它们纳入。

另外断言"0 vs 未知"：``empty`` 是数过的 0，``unconfigured``/``unreachable`` 是未知，
目录**不**把后者折成前者。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.evaluation.authz import REPORT_NOT_AVAILABLE_MESSAGE
from backend.evaluation.errors import NotFound, ReportSourceMalformed
from backend.evaluation.identity import (
    RequesterContext,
    SCOPE_EVAL_ADMIN_MANAGE,
    SCOPE_EVAL_ADMIN_READ,
    SCOPE_EVAL_READ,
    SCOPE_EVAL_REPORT_READ,
)
from backend.evaluation.models import metric_known
from backend.evaluation.provenance import (
    DIMENSION_QUALITY,
    DIMENSION_THROUGHPUT,
    REPORT_DIMENSIONS,
    SOURCE_EMPTY,
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    SOURCE_OK,
    SOURCE_STALE,
    SOURCE_UNCONFIGURED,
    SOURCE_UNREACHABLE,
)
from backend.evaluation.report_catalog import ReportCatalog, empty_catalog

from tests.evaluation import HOST_USERS
from tests.evaluation.support import (
    EPOCH,
    PROJECT_BETA,
    REPORT_REF_EVALSCOPE,
    REPORT_REF_LEGACY,
    build_directories,
    report_owner,
    report_record,
    report_snapshot,
)

_PRINCIPALS, _TOKENS, PROJECTS, _TARGETS = build_directories()

MEMBER_SCOPES = frozenset({SCOPE_EVAL_READ, SCOPE_EVAL_REPORT_READ})
ADMIN_READ_SCOPES = frozenset({SCOPE_EVAL_READ, SCOPE_EVAL_ADMIN_READ})
ADMIN_MANAGE_SCOPES = frozenset(
    {SCOPE_EVAL_READ, SCOPE_EVAL_ADMIN_READ, SCOPE_EVAL_ADMIN_MANAGE}
)


def requester(principal: str, scopes=MEMBER_SCOPES, *, uid: int = -1) -> RequesterContext:
    resolved = dict(HOST_USERS)[principal] if uid == -1 else uid
    return RequesterContext(
        principal=principal,
        uid=resolved,
        linux_user=principal,
        scopes=frozenset(scopes),
        roles=frozenset({"member"}),
    )


def alice_report(ref: str = REPORT_REF_EVALSCOPE):
    return report_record(SOURCE_EVALSCOPE, ref, report_id="r-alice", owner=report_owner("alice"))


def dave_report(ref: str = REPORT_REF_LEGACY):
    return report_record(
        SOURCE_LEGACY_BENCHMARK,
        ref,
        report_id="r-dave",
        owner=report_owner("dave", project_id=PROJECT_BETA),
    )


def dave_evalscope_report(ref: str = "evalscope/reports/dave-1.json"):
    return report_record(
        SOURCE_EVALSCOPE,
        ref,
        report_id="r-dave-ev",
        owner=report_owner("dave", project_id=PROJECT_BETA),
    )


def catalog_with(*snapshots, now=EPOCH, staleness_seconds: int = 3600) -> ReportCatalog:
    return ReportCatalog(tuple(snapshots), now=now, staleness_seconds=staleness_seconds)


def one_ok(source: str, record) -> ReportCatalog:
    return catalog_with(report_snapshot(source, SOURCE_OK, [record]))


class VisibilityTests(unittest.TestCase):
    def test_the_owner_in_the_same_project_sees_their_report(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        visible = catalog.visible(requester("alice"), PROJECTS)
        self.assertEqual(len(visible), 1)
        entry, reason = visible[0]
        self.assertEqual(reason, "owner_and_project_member")
        self.assertEqual(entry.ref_id, alice_report().ref_id)

    def test_a_plain_member_does_not_see_another_users_report(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        self.assertEqual(catalog.visible(requester("dave"), PROJECTS), ())

    def test_a_matching_principal_without_membership_is_refused(self) -> None:
        # 归属指向一个该项目里没有这个主体的 project：归属映射写错了就不该放行。
        record = report_record(
            SOURCE_EVALSCOPE,
            REPORT_REF_EVALSCOPE,
            report_id="r-x",
            owner=report_owner("alice", project_id=PROJECT_BETA),
        )
        catalog = one_ok(SOURCE_EVALSCOPE, record)
        self.assertEqual(catalog.visible(requester("alice"), PROJECTS), ())

    def test_a_global_reader_sees_a_owned_report(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        visible = catalog.visible(requester("carol", ADMIN_READ_SCOPES), PROJECTS)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0][1], "global_reader")

    def test_a_requester_with_a_different_uid_is_refused(self) -> None:
        # 同一个 principal 但 uid 对不上：归属不一致，拒绝。
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        # 构造一个 principal 对、uid 错的上下文（用另一个真实入目录的 uid）。
        forged = requester("alice", uid=dict(HOST_USERS)["bob"])
        self.assertEqual(catalog.visible(forged, PROJECTS), ())


class UnownedTests(unittest.TestCase):
    def unowned_catalog(self) -> ReportCatalog:
        orphan = report_record(
            SOURCE_LEGACY_BENCHMARK, REPORT_REF_LEGACY, report_id="r-orphan", owner=None
        )
        return one_ok(SOURCE_LEGACY_BENCHMARK, orphan)

    def test_an_unowned_report_is_hidden_from_a_member(self) -> None:
        self.assertEqual(self.unowned_catalog().visible(requester("alice"), PROJECTS), ())

    def test_an_unowned_report_is_hidden_from_a_readonly_admin(self) -> None:
        catalog = self.unowned_catalog()
        self.assertEqual(catalog.visible(requester("carol", ADMIN_READ_SCOPES), PROJECTS), ())

    def test_an_unowned_report_is_hidden_from_admin_manage_without_the_flag(self) -> None:
        catalog = self.unowned_catalog()
        self.assertEqual(catalog.visible(requester("carol", ADMIN_MANAGE_SCOPES), PROJECTS), ())

    def test_only_admin_manage_with_the_flag_gets_the_unowned_report(self) -> None:
        catalog = self.unowned_catalog()
        visible = catalog.visible(
            requester("carol", ADMIN_MANAGE_SCOPES), PROJECTS, include_unowned=True
        )
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0][1], "admin_managed_unowned")

    def test_a_member_passing_the_flag_still_gets_nothing(self) -> None:
        catalog = self.unowned_catalog()
        self.assertEqual(
            catalog.visible(requester("alice"), PROJECTS, include_unowned=True), ()
        )

    def test_a_readonly_admin_passing_the_flag_still_gets_nothing(self) -> None:
        catalog = self.unowned_catalog()
        self.assertEqual(
            catalog.visible(requester("carol", ADMIN_READ_SCOPES), PROJECTS, include_unowned=True),
            (),
        )


class LookupTests(unittest.TestCase):
    def test_lookup_returns_the_visible_entry(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        entry, _reason = catalog.lookup(
            requester("alice"), PROJECTS, alice_report().ref_id
        )
        self.assertEqual(entry.ref_id, alice_report().ref_id)

    def test_lookup_of_an_unknown_reference_is_a_404(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        with self.assertRaises(NotFound) as caught:
            catalog.lookup(requester("alice"), PROJECTS, "evrp-" + "0" * 24)
        self.assertEqual(str(caught.exception), REPORT_NOT_AVAILABLE_MESSAGE)

    def test_lookup_of_an_invisible_reference_is_byte_identical_to_unknown(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        with self.assertRaises(NotFound) as unknown:
            catalog.lookup(requester("dave"), PROJECTS, "evrp-" + "0" * 24)
        with self.assertRaises(NotFound) as invisible:
            catalog.lookup(requester("dave"), PROJECTS, alice_report().ref_id)
        self.assertEqual(str(unknown.exception), str(invisible.exception))

    def test_lookup_rejects_a_blank_reference_like_an_unknown_one(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        for bad in ("", "   ", None, 7):
            with self.subTest(ref=bad):
                with self.assertRaises(NotFound):
                    catalog.lookup(requester("alice"), PROJECTS, bad)


class SourceStateTests(unittest.TestCase):
    def test_an_empty_source_is_a_counted_zero_not_unknown(self) -> None:
        catalog = catalog_with(report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, observed_at=EPOCH))
        row = catalog.pipeline_status()[0]
        self.assertEqual(row["state"], SOURCE_EMPTY)
        self.assertFalse(row["stale"])

    def test_an_unconfigured_source_is_not_a_counted_zero(self) -> None:
        catalog = catalog_with(
            report_snapshot(SOURCE_LEGACY_BENCHMARK, SOURCE_UNCONFIGURED, observed_at=None)
        )
        row = catalog.pipeline_status()[0]
        self.assertEqual(row["state"], SOURCE_UNCONFIGURED)
        self.assertNotEqual(row["state"], SOURCE_EMPTY)

    def test_an_unreachable_source_is_not_a_counted_zero(self) -> None:
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_UNREACHABLE, observed_at=None)
        )
        self.assertEqual(catalog.pipeline_status()[0]["state"], SOURCE_UNREACHABLE)

    def test_both_sources_are_reported_separately(self) -> None:
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, observed_at=EPOCH),
            report_snapshot(SOURCE_LEGACY_BENCHMARK, SOURCE_UNCONFIGURED, observed_at=None),
        )
        rows = {row["source"]: row["state"] for row in catalog.pipeline_status()}
        self.assertEqual(rows[SOURCE_EVALSCOPE], SOURCE_EMPTY)
        self.assertEqual(rows[SOURCE_LEGACY_BENCHMARK], SOURCE_UNCONFIGURED)


class FreshnessTests(unittest.TestCase):
    def test_a_fresh_ok_snapshot_is_not_stale(self) -> None:
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [alice_report()], observed_at=EPOCH)
        )
        self.assertFalse(catalog.pipeline_status()[0]["stale"])
        self.assertFalse(catalog.entries()[0].stale)

    def test_an_old_ok_snapshot_is_stale(self) -> None:
        old = EPOCH - timedelta(hours=5)
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [alice_report()], observed_at=old)
        )
        self.assertEqual(catalog.pipeline_status()[0]["state"], SOURCE_STALE)
        self.assertTrue(catalog.entries()[0].stale)
        # 陈旧不隐藏记录：用户看到的是"最后一次已知的样子"，被标成陈旧。
        self.assertEqual(len(catalog.visible(requester("alice"), PROJECTS)), 1)

    def test_an_ok_snapshot_without_a_timestamp_is_treated_as_stale(self) -> None:
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [alice_report()], observed_at=None)
        )
        self.assertTrue(catalog.entries()[0].stale)
        self.assertEqual(catalog.pipeline_status()[0]["state"], SOURCE_STALE)

    def test_a_far_future_timestamp_is_stale_not_fresh(self) -> None:
        """远在未来的 ``observed_at`` 会算出负 age；没有上界它就永远新鲜。"""
        future = EPOCH + timedelta(days=365)
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [alice_report()], observed_at=future)
        )
        row = catalog.pipeline_status()[0]
        self.assertEqual(row["state"], SOURCE_STALE)
        self.assertTrue(row["stale"])

    def test_a_small_clock_skew_is_tolerated(self) -> None:
        slightly_ahead = EPOCH + timedelta(seconds=5)
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [alice_report()], observed_at=slightly_ahead)
        )
        self.assertEqual(catalog.pipeline_status()[0]["state"], SOURCE_OK)


class ScopedSourceStatusTests(unittest.TestCase):
    """P0-A：下发给普通成员的来源状态**不能**泄露别人的报告是否存在。"""

    def foreign_only_source(self) -> ReportCatalog:
        # 来源里有一条别人的记录；alice 看不到它。
        return catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [dave_evalscope_report()], observed_at=EPOCH)
        )

    def test_a_member_sees_no_ok_for_a_source_holding_only_foreign_reports(self) -> None:
        catalog = self.foreign_only_source()
        row = catalog.scoped_source_status(requester("alice"), PROJECTS)[0]
        self.assertEqual(row["state"], SOURCE_EMPTY)
        self.assertEqual(row["visible_count"], 0)
        self.assertIsNone(row["observed_at"])

    def test_the_owner_sees_their_own_source_as_ok(self) -> None:
        catalog = one_ok(SOURCE_EVALSCOPE, alice_report())
        row = catalog.scoped_source_status(requester("alice"), PROJECTS)[0]
        self.assertEqual(row["state"], SOURCE_OK)
        self.assertEqual(row["visible_count"], 1)

    def test_an_unwired_source_stays_unconfigured_for_a_member(self) -> None:
        catalog = catalog_with(
            report_snapshot(SOURCE_LEGACY_BENCHMARK, SOURCE_UNCONFIGURED, observed_at=None)
        )
        row = catalog.scoped_source_status(requester("alice"), PROJECTS)[0]
        self.assertEqual(row["state"], SOURCE_UNCONFIGURED)

    def test_the_scoped_view_is_identical_whether_the_source_is_empty_or_foreign(self) -> None:
        empty_source = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, observed_at=EPOCH)
        )
        foreign = self.foreign_only_source()
        for include_unowned in (False, True):
            with self.subTest(include_unowned=include_unowned):
                a = empty_source.scoped_source_status(
                    requester("alice"), PROJECTS, include_unowned=include_unowned
                )
                b = foreign.scoped_source_status(
                    requester("alice"), PROJECTS, include_unowned=include_unowned
                )
                self.assertEqual(a, b)


class DimensionRenderingTests(unittest.TestCase):
    def test_missing_dimensions_are_filled_with_unknown_never_zero(self) -> None:
        record = report_record(
            SOURCE_EVALSCOPE,
            REPORT_REF_EVALSCOPE,
            report_id="r-dims",
            owner=report_owner("alice"),
            dimensions={DIMENSION_QUALITY: metric_known(DIMENSION_QUALITY, 0.75, unit="ratio")},
        )
        entry = one_ok(SOURCE_EVALSCOPE, record).entries()[0]
        dims = entry.dimensions
        self.assertEqual(set(dims), set(REPORT_DIMENSIONS))
        self.assertEqual(dims[DIMENSION_QUALITY].value, 0.75)
        self.assertEqual(dims[DIMENSION_THROUGHPUT].state, "unknown")
        self.assertIsNone(dims[DIMENSION_THROUGHPUT].value)
        self.assertIn(DIMENSION_THROUGHPUT, entry.missing_dimensions)
        self.assertNotIn(DIMENSION_QUALITY, entry.missing_dimensions)


class StructuralTests(unittest.TestCase):
    def test_an_empty_catalog_has_no_entries_and_no_sources(self) -> None:
        catalog = empty_catalog(now=EPOCH)
        self.assertEqual(catalog.entries(), ())
        self.assertEqual(catalog.pipeline_status(), ())

    def test_duplicate_snapshots_for_one_source_are_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            catalog_with(
                report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, observed_at=EPOCH),
                report_snapshot(SOURCE_EVALSCOPE, SOURCE_EMPTY, observed_at=EPOCH),
            )

    def test_a_duplicate_report_reference_is_refused(self) -> None:
        first = report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE, report_id="r-1")
        second = report_record(SOURCE_EVALSCOPE, REPORT_REF_EVALSCOPE, report_id="r-1")
        with self.assertRaises(ReportSourceMalformed):
            catalog_with(report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [first, second]))

    def test_the_catalog_refuses_a_naive_now(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            ReportCatalog((), now=datetime(2026, 10, 15, 12, 0))

    def test_visible_by_source_counts_per_source(self) -> None:
        mine = alice_report()
        theirs = dave_report()
        catalog = catalog_with(
            report_snapshot(SOURCE_EVALSCOPE, SOURCE_OK, [mine]),
            report_snapshot(SOURCE_LEGACY_BENCHMARK, SOURCE_OK, [theirs]),
        )
        counts = catalog.visible_by_source(requester("alice"), PROJECTS)
        self.assertEqual(counts[SOURCE_EVALSCOPE], 1)
        self.assertEqual(counts[SOURCE_LEGACY_BENCHMARK], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
