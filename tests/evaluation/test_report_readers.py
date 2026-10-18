"""只读来源适配器：fail-closed 缺省、来源盖章、归属核实、畸形负载、伪造路径、HTML。

这一组用**注入的 transport**喂合成负载，不发任何网络请求。要证明的几件事：

* 没接线的来源是 ``unconfigured``，**不是** 0 份；
* 负载自报的 ``source`` 不被读取——来源由适配器盖章（"误导性来源标记"的负向）；
* 旧镜像里自报的 owner 不被信任，只有审核过的 ``uid -> owner`` 映射才算数；
* 畸形负载（含伪造路径、非法 HTML、非数值指标）把整条来源折成 ``unreachable``，
  不抛给调用方、也**不伪造**一条记录。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.evaluation.errors import ReportSourceMalformed
from backend.evaluation.provenance import (
    DIMENSION_QUALITY,
    DIMENSION_THROUGHPUT,
    SOURCE_EMPTY,
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    SOURCE_OK,
    SOURCE_UNCONFIGURED,
    SOURCE_UNREACHABLE,
    ReportOwner,
    safe_text,
)
from backend.evaluation.report_readers import (
    EVALSCOPE_REPORTS_PATH,
    LEGACY_RUNS_PATH,
    EvalScopeReportsReader,
    LegacyBenchmarkReader,
    records_from_evalscope_payload,
    records_from_legacy_payload,
    unconfigured_source_snapshots,
)

EPOCH = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
VINCE_OWNER = ReportOwner(principal="alice", uid=4001, project_id="proj-alpha")
BASE = "https://evalscope.internal"


def clock() -> datetime:
    return EPOCH


def evalscope_payload(*reports: dict) -> dict:
    return {"total": len(reports), "reports": list(reports)}


def legacy_payload(*runs: dict) -> dict:
    return {"runs": list(runs)}


def evalscope_reader(transport, *, single_tenant_owner=VINCE_OWNER, owners=None, base_url=BASE, synthetic=False):
    return EvalScopeReportsReader(
        single_tenant_owner=single_tenant_owner,
        owners=owners,
        clock=clock,
        base_url=base_url,
        transport=transport,
        synthetic=synthetic,
    )


def legacy_reader(transport, *, audited_owners=None, base_url=BASE, synthetic=False):
    return LegacyBenchmarkReader(
        audited_owners=(
            audited_owners if audited_owners is not None else {"r-1": VINCE_OWNER}
        ),
        clock=clock,
        base_url=base_url,
        transport=transport,
        synthetic=synthetic,
    )


class UnconfiguredTests(unittest.TestCase):
    def test_a_reader_without_a_transport_is_unconfigured(self) -> None:
        reader = evalscope_reader(None)
        self.assertFalse(reader.configured)
        snapshot = reader.snapshot()
        self.assertEqual(snapshot.state, SOURCE_UNCONFIGURED)
        self.assertEqual(snapshot.count, 0)

    def test_a_reader_without_a_base_url_is_unconfigured(self) -> None:
        reader = evalscope_reader(lambda path: evalscope_payload(), base_url=None)
        self.assertFalse(reader.configured)
        self.assertEqual(reader.snapshot().state, SOURCE_UNCONFIGURED)

    def test_the_deployment_default_reports_both_sources_unconfigured(self) -> None:
        snapshots = unconfigured_source_snapshots()
        self.assertEqual(
            sorted(s.source for s in snapshots),
            sorted([SOURCE_EVALSCOPE, SOURCE_LEGACY_BENCHMARK]),
        )
        for snapshot in snapshots:
            self.assertEqual(snapshot.state, SOURCE_UNCONFIGURED)
            self.assertEqual(snapshot.count, 0)

    def test_the_description_never_echoes_the_base_url(self) -> None:
        described = evalscope_reader(lambda path: {}).describe()
        self.assertNotIn("base_url", {k.lower() for k in described})
        self.assertNotIn(BASE, repr(described))
        self.assertTrue(described["read_only"])


class EvalScopeMappingTests(unittest.TestCase):
    def test_a_configured_reader_produces_evalscope_records(self) -> None:
        payload = evalscope_payload(
            {
                "id": "r-1",
                "artifact": "evalscope/reports/r-1.json",
                "created_at": "2026-10-15T11:00:00Z",
                "metrics": {DIMENSION_QUALITY: 0.8, DIMENSION_THROUGHPUT: {"value": 12, "unit": "rps"}},
                "title": "smoke",
            }
        )
        snapshot = evalscope_reader(lambda path: payload).snapshot()
        self.assertEqual(snapshot.state, SOURCE_OK)
        self.assertEqual(snapshot.count, 1)
        record = snapshot.records[0]
        self.assertEqual(record.source, SOURCE_EVALSCOPE)
        self.assertEqual(record.owner, VINCE_OWNER)
        self.assertEqual(record.dimension(DIMENSION_QUALITY).value, 0.8)
        self.assertEqual(record.dimension(DIMENSION_THROUGHPUT).unit, "rps")
        self.assertEqual(snapshot.observed_at, EPOCH)

    def test_the_reader_calls_the_fixed_path_only(self) -> None:
        seen: list[str] = []

        def transport(path: str) -> object:
            seen.append(path)
            return evalscope_payload()

        evalscope_reader(transport).snapshot()
        self.assertEqual(seen, [EVALSCOPE_REPORTS_PATH])

    def test_an_empty_result_is_a_counted_zero(self) -> None:
        snapshot = evalscope_reader(lambda path: evalscope_payload()).snapshot()
        self.assertEqual(snapshot.state, SOURCE_EMPTY)
        self.assertEqual(snapshot.count, 0)

    def test_the_payload_source_flag_is_ignored(self) -> None:
        """**误导性来源标记**：负载自称 legacy，适配器仍然盖 evalscope。"""
        payload = {
            "source": SOURCE_LEGACY_BENCHMARK,
            "kind": "legacy-benchmark",
            "reports": [{"id": "r-1", "artifact": "evalscope/reports/r-1.json"}],
        }
        snapshot = evalscope_reader(lambda path: payload).snapshot()
        self.assertEqual(snapshot.records[0].source, SOURCE_EVALSCOPE)


class LegacyMappingTests(unittest.TestCase):
    def test_a_legacy_reader_always_stamps_legacy_benchmark(self) -> None:
        """**误导性来源标记**：镜像自称 evalscope，适配器仍然盖 legacy-benchmark。"""
        payload = {
            "source": SOURCE_EVALSCOPE,
            "reports": [{"id": "r-1", "artifact": "legacy/benchmark/r-1.json"}],
            "runs": [{"run_id": "r-1", "artifact": "legacy/benchmark/r-1.json", "owner_uid": 4001}],
        }
        snapshot = legacy_reader(lambda path: payload).snapshot()
        self.assertEqual(snapshot.records[0].source, SOURCE_LEGACY_BENCHMARK)

    def test_only_a_per_run_audited_map_confers_ownership(self) -> None:
        payload = legacy_payload(
            {"run_id": "r-1", "artifact": "legacy/benchmark/r-1.json"},
            {"run_id": "r-2", "artifact": "legacy/benchmark/r-2.json"},
            {"run_id": "r-3", "artifact": "legacy/benchmark/r-3.json"},
        )
        # 只有 r-1 经过逐 run 审核。
        reader = legacy_reader(lambda path: payload, audited_owners={"r-1": VINCE_OWNER})
        by_id = {record.source_report_id: record for record in reader.snapshot().records}
        self.assertEqual(by_id["r-1"].owner, VINCE_OWNER)
        self.assertTrue(by_id["r-2"].unowned)
        self.assertTrue(by_id["r-3"].unowned)

    def test_a_forged_owner_uid_does_not_grant_access(self) -> None:
        """P0-B：伪造 ``owner_uid`` 为一个**已被批准**的 UID 也不能认领历史 run。"""
        # UID 4001 确实在一份可信名单上，但这条 run_id 从未被审核过。
        payload = legacy_payload(
            {
                "run_id": "r-forged",
                "artifact": "legacy/benchmark/r-forged.json",
                "owner_uid": 4001,
                "owner": "alice",
                "owner_principal": "alice",
                "source": SOURCE_EVALSCOPE,
            }
        )
        reader = legacy_reader(lambda path: payload, audited_owners={"r-1": VINCE_OWNER})
        record = reader.snapshot().records[0]
        self.assertTrue(record.unowned)
        self.assertIsNone(record.owner)
        self.assertEqual(record.source, SOURCE_LEGACY_BENCHMARK)

    def test_an_audited_run_is_owned_even_if_it_declares_a_different_uid(self) -> None:
        """审核映射是权威；payload 自报的 uid 与它冲突也不改写归属。"""
        payload = legacy_payload(
            {"run_id": "r-1", "artifact": "legacy/benchmark/r-1.json", "owner_uid": 9999}
        )
        record = legacy_reader(lambda path: payload).snapshot().records[0]
        self.assertEqual(record.owner, VINCE_OWNER)

    def test_the_legacy_reader_calls_the_fixed_path_only(self) -> None:
        seen: list[str] = []
        legacy_reader(lambda path: (seen.append(path), legacy_payload())[1]).snapshot()
        self.assertEqual(seen, [LEGACY_RUNS_PATH])


class MalformedPayloadTests(unittest.TestCase):
    def assert_unreachable(self, reader) -> None:
        snapshot = reader.snapshot()
        self.assertEqual(snapshot.state, SOURCE_UNREACHABLE)
        self.assertEqual(snapshot.count, 0)

    def test_a_transport_failure_is_unreachable(self) -> None:
        def boom(path: str) -> object:
            raise OSError("connection refused")

        self.assert_unreachable(evalscope_reader(boom))

    def test_a_non_mapping_payload_is_unreachable(self) -> None:
        self.assert_unreachable(evalscope_reader(lambda path: ["not", "a", "mapping"]))

    def test_a_payload_without_reports_is_unreachable(self) -> None:
        self.assert_unreachable(evalscope_reader(lambda path: {"total": 0}))

    def test_a_reports_value_that_is_not_a_list_is_unreachable(self) -> None:
        self.assert_unreachable(evalscope_reader(lambda path: {"reports": "nope"}))

    def test_a_non_numeric_metric_is_unreachable(self) -> None:
        payload = evalscope_payload(
            {"id": "r-1", "artifact": "evalscope/reports/r-1.json", "metrics": {DIMENSION_QUALITY: "high"}}
        )
        self.assert_unreachable(evalscope_reader(lambda path: payload))

    def test_an_unknown_dimension_name_is_unreachable(self) -> None:
        payload = evalscope_payload(
            {"id": "r-1", "artifact": "evalscope/reports/r-1.json", "metrics": {"mystery": 1}}
        )
        self.assert_unreachable(evalscope_reader(lambda path: payload))

    def test_a_forged_path_is_unreachable_and_not_a_400(self) -> None:
        """伪造路径是**来源**的问题：整条来源报成没读到，而不是把 400 甩给读列表的人。"""
        payload = evalscope_payload(
            {"id": "r-1", "artifact": "/etc/passwd.json"}
        )
        self.assert_unreachable(evalscope_reader(lambda path: payload))

    def test_a_traversal_path_is_unreachable(self) -> None:
        payload = evalscope_payload(
            {"id": "r-1", "artifact": "../../secret.json"}
        )
        self.assert_unreachable(evalscope_reader(lambda path: payload))

    def test_a_non_integer_legacy_owner_uid_is_ignored_not_fatal(self) -> None:
        """``owner_uid`` 根本不参与判定，因此它的类型也不该让整条来源失败。"""
        payload = legacy_payload(
            {"run_id": "r-1", "artifact": "legacy/benchmark/r-1.json", "owner_uid": "4001"}
        )
        snapshot = legacy_reader(lambda path: payload).snapshot()
        self.assertEqual(snapshot.state, SOURCE_OK)
        self.assertEqual(snapshot.records[0].owner, VINCE_OWNER)

    def test_a_bad_timestamp_is_unreachable(self) -> None:
        payload = evalscope_payload(
            {"id": "r-1", "artifact": "evalscope/reports/r-1.json", "created_at": "yesterday"}
        )
        self.assert_unreachable(evalscope_reader(lambda path: payload))

    def test_an_unreachable_snapshot_carries_no_records(self) -> None:
        snapshot = evalscope_reader(lambda path: ["nope"]).snapshot()
        self.assertEqual(snapshot.records, ())
        self.assertEqual(snapshot.state, SOURCE_UNREACHABLE)


class HtmlSafetyTests(unittest.TestCase):
    def test_a_markup_title_is_sanitized_on_the_way_in(self) -> None:
        payload = evalscope_payload(
            {
                "id": "r-1",
                "artifact": "evalscope/reports/r-1.json",
                "title": "<img src=x onerror=alert(1)>Smoke",
            }
        )
        record = evalscope_reader(lambda path: payload).snapshot().records[0]
        self.assertIsNotNone(record.title)
        self.assertNotIn("<", record.title)
        self.assertNotIn(">", record.title)
        self.assertEqual(record.title, safe_text("<img src=x onerror=alert(1)>Smoke"))


class BaseUrlValidationTests(unittest.TestCase):
    def test_an_embedded_credential_is_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            evalscope_reader(lambda path: {}, base_url="https://" + "user:pass@" + "host")

    def test_a_query_or_fragment_is_refused(self) -> None:
        for bad in ("https://host/?x=1", "https://host/#frag"):
            with self.subTest(url=bad):
                with self.assertRaises(ReportSourceMalformed):
                    evalscope_reader(lambda path: {}, base_url=bad)

    def test_a_non_http_scheme_is_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            evalscope_reader(lambda path: {}, base_url="ftp://host")

    def test_a_relative_url_is_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            evalscope_reader(lambda path: {}, base_url="evalscope.internal")


class EvalScopeOwnershipTests(unittest.TestCase):
    """P1-C：全局列表不能被整份盖上一个 owner。"""

    def two_owner_payload(self) -> dict:
        return evalscope_payload(
            {"id": "r-1", "artifact": "evalscope/reports/r-1.json"},
            {"id": "r-2", "artifact": "evalscope/reports/r-2.json"},
        )

    def test_without_any_ownership_proof_every_report_is_unowned(self) -> None:
        records = records_from_evalscope_payload(self.two_owner_payload(), synthetic=False)
        self.assertTrue(all(record.unowned for record in records))

    def test_a_per_report_map_only_owns_the_ids_it_names(self) -> None:
        records = records_from_evalscope_payload(
            self.two_owner_payload(), owners={"r-1": VINCE_OWNER}, synthetic=False
        )
        by_id = {record.source_report_id: record for record in records}
        self.assertEqual(by_id["r-1"].owner, VINCE_OWNER)
        # 未逐条审核的那一条**不会**因为配了别人就被顺手认领。
        self.assertTrue(by_id["r-2"].unowned)

    def test_a_single_tenant_declaration_assigns_the_whole_list(self) -> None:
        records = records_from_evalscope_payload(
            self.two_owner_payload(), single_tenant_owner=VINCE_OWNER, synthetic=False
        )
        self.assertTrue(all(record.owner == VINCE_OWNER for record in records))

    def test_declaring_both_ownership_modes_is_refused(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            records_from_evalscope_payload(
                self.two_owner_payload(),
                owners={"r-1": VINCE_OWNER},
                single_tenant_owner=VINCE_OWNER,
                synthetic=False,
            )

    def test_a_reader_refuses_both_ownership_modes(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            EvalScopeReportsReader(
                single_tenant_owner=VINCE_OWNER,
                owners={"r-1": VINCE_OWNER},
                clock=clock,
                base_url=BASE,
                transport=lambda path: evalscope_payload(),
            )


class MapperDirectTests(unittest.TestCase):
    def test_the_evalscope_mapper_stamps_its_source(self) -> None:
        records = records_from_evalscope_payload(
            evalscope_payload({"id": "r-1", "artifact": "evalscope/reports/r-1.json"}),
            synthetic=True,
        )
        self.assertEqual(records[0].source, SOURCE_EVALSCOPE)
        self.assertTrue(records[0].synthetic)

    def test_the_legacy_mapper_stamps_its_source(self) -> None:
        records = records_from_legacy_payload(
            legacy_payload({"run_id": "r-1", "artifact": "legacy/benchmark/r-1.json"}),
            audited_owners={},
            synthetic=False,
        )
        self.assertEqual(records[0].source, SOURCE_LEGACY_BENCHMARK)

    def test_the_evalscope_mapper_does_not_read_a_source_field(self) -> None:
        with self.assertRaises(ReportSourceMalformed):
            # 未知维度名导致整条记录不合法（不是因为 source）。
            records_from_evalscope_payload(
                {"reports": [{"id": "r", "artifact": "a/b.json", "metrics": {"nope": 1}}]},
                synthetic=False,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
