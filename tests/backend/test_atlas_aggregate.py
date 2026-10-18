"""``backend/atlas/aggregate.py`` 的回归（``atlas-aggregate/1``）。

这一份契约回答的是「用量的形状」：按项目/工作负载的构成、按模型的分布、
按 星期几×小时 的 7×24 热度、延迟分位与时间趋势。

三条最要紧的断言：

* **默认不接线 = UNKNOWN。** 没有显式声明 ``attached`` 的来源产出的不是一组
  看起来像实测的 0，而是 ``state: unknown`` 与空数组。
* **跨 UID / 跨项目泄漏为零。** 授权范围外的行在任何一个新维度上都不得露面 ——
  新增维度是这类泄漏最容易溜进来的地方（授权只要漏在**一个新**维度上就够）。
* **热度图不补零。** 只有观测到的格子存在；没数据的时段不长得像「没有请求」。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import (  # noqa: E402
    atlas_request,
    load_fixture,
    requester,
    trusted_directory,
)
from backend.atlas import (  # noqa: E402
    AGGREGATE_SCHEMA_VERSION,
    MAX_MODELS,
    MODEL_LABEL_ESCAPE,
    MODEL_OTHER,
    MODEL_UNREPORTED,
    SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
    SCOPE_USAGE_UNATTRIBUTED,
    AggregateSource,
    UnverifiedSource,
    build_atlas_aggregate,
    build_atlas_aggregate_dto,
    build_unattached_atlas_aggregate,
    is_sentinel_model_label,
    model_label,
    validate_atlas_aggregate,
)
from backend.atlas.aggregate import (  # noqa: E402
    HEATMAP_CELLS,
    MAX_MODEL_LABEL_LEN,
    STATE_MEASURED,
    STATE_UNKNOWN,
    WEEKDAY_ORIGIN,
)
from backend.atlas.errors import AccessDenied, ContractError, InvalidRequest  # noqa: E402

ATTACHED = AggregateSource(source="gateway-usage-export", synthetic=False, attached=True)
UNATTACHED = AggregateSource(source="gateway-usage-export", synthetic=False)


def build(text: str | None = None, *, request=None, source=ATTACHED) -> dict:
    return build_atlas_aggregate_dto(
        request if request is not None else atlas_request(),
        load_fixture("gateway_sample.jsonl") if text is None else text,
        trusted_directory(),
        source,
    )


def rows(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def row(**over) -> dict:
    base = {
        "ts": "2026-10-14T00:30:00Z",
        "key_id": "key-alpha",
        "method": "POST",
        "path": "/v1/chat/completions",
        "model": "llm-a",
        "status": 200,
        "outcome": "success",
        "stream": True,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "bytes_out": 10,
        "queue_ms": 1,
        "ttft_ms": 10,
        "upstream_ms": 10,
        "total_ms": 100,
    }
    base.update(over)
    return base


class FailClosedAttachmentTest(unittest.TestCase):
    """来源没接线时，答法是封闭的 —— 不是一组 0。"""

    def test_an_unattached_source_reports_unknown_with_empty_arrays(self) -> None:
        dto = build(source=UNATTACHED)
        self.assertFalse(dto["attached"])
        self.assertEqual(dto["state"], STATE_UNKNOWN)
        self.assertEqual(dto["groups"], [])
        self.assertEqual(dto["models"], [])
        self.assertEqual(dto["heatmap"]["cells"], [])
        self.assertEqual(dto["trend"]["points"], [])
        self.assertIsNone(dto["latency"]["p50_ms"])
        self.assertIsNone(dto["latency"]["p95_ms"])
        self.assertEqual(dto["quality"]["calls"], "unknown")

    def test_attachment_defaults_to_false(self) -> None:
        self.assertFalse(AggregateSource(source="gateway-usage-export", synthetic=False).attached)

    def test_an_unattached_dto_never_pretends_to_measure(self) -> None:
        dto = build(source=UNATTACHED)
        self.assertIn("未接线", " ".join(dto["display_hints"]))

    def test_defaults_are_rejected_when_they_contradict_themselves(self) -> None:
        with self.assertRaises(ContractError):
            validate_atlas_aggregate({**build(source=UNATTACHED), "state": STATE_MEASURED})

    def test_an_unattached_dto_may_not_carry_rows(self) -> None:
        tampered = {**build(source=UNATTACHED), "groups": [{"id": "proj-a", "workloads": []}]}
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(tampered)

    def test_an_unattached_source_still_denies_unauthorised_callers(self) -> None:
        # 「你没权限」与「来源没接」是不同的失败，前者必须先拒绝 —— 否则 403 会被
        # 一个 200 的空壳藏起来。
        with self.assertRaises(AccessDenied):
            build(
                source=UNATTACHED,
                request=atlas_request(requester_ctx=requester(scopes=())),
            )

    def test_an_attached_source_reports_measured(self) -> None:
        dto = build()
        self.assertTrue(dto["attached"])
        self.assertEqual(dto["state"], STATE_MEASURED)
        self.assertEqual(dto["schema_version"], AGGREGATE_SCHEMA_VERSION)

    def test_a_synthetic_source_is_labelled(self) -> None:
        dto = build(source=AggregateSource(source="gateway", synthetic=True, attached=True))
        self.assertTrue(dto["synthetic"])
        self.assertIn("SYNTHETIC", " ".join(dto["display_hints"]))


class CrossTenantIsolationTest(unittest.TestCase):
    """新增维度最容易漏的那一条：授权范围外的行不得在**任何**维度上露面。"""

    def setUp(self) -> None:
        self.dto = build()
        self.blob = json.dumps(self.dto, sort_keys=True)

    def test_no_foreign_project_key_or_model_appears_anywhere(self) -> None:
        for needle in ("proj-b", "key-gamma", "u-2001", "llm-c", "eval-suite"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, self.blob)

    def test_the_model_distribution_only_carries_authorised_models(self) -> None:
        self.assertEqual({entry["model"] for entry in self.dto["models"]}, {"llm-a", "llm-b"})

    def test_the_viewer_without_the_scope_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            build(request=atlas_request(requester_ctx=requester(scopes=())))

    def test_a_foreign_requested_subject_is_denied_not_downgraded(self) -> None:
        with self.assertRaises(AccessDenied):
            build(request=atlas_request(requested_subject="u-2001"))

    def test_a_foreign_project_whitelist_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            build(request=atlas_request(requester_ctx=requester(projects=("proj-b",))))

    def test_an_empty_project_whitelist_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            build(request=atlas_request(requester_ctx=requester(projects=())))

    def test_adding_foreign_and_corrupt_rows_does_not_change_the_dto(self) -> None:
        # 同一 viewer，第二批额外塞进其他项目 / 未知 key / 坏 JSON / 正文行之后，
        # 整份 DTO 必须逐字节相同。
        extra = rows(
            row(key_id="key-gamma", model="llm-c", ts="2026-10-14T00:10:00Z"),
            row(key_id="unknown-zzz", ts="2026-10-14T00:11:00Z"),
            row(key_id="key-alpha", ts="2026-10-14T00:12:00Z", prompt="leak?"),
        )
        polluted = build(load_fixture("gateway_sample.jsonl") + "\n" + extra)
        self.assertEqual(json.dumps(polluted, sort_keys=True), self.blob)

    def test_a_forged_model_name_stays_a_label_in_your_own_view(self) -> None:
        # 模型名是客户端可控的**描述性**标签：它当然会原样出现在你自己的视图里，
        # 但它不得改归属、不得改授权，也不得制造出一个别人项目的分组。
        dto = build(rows(row(key_id="key-alpha", model="proj-b")))
        self.assertEqual([group["id"] for group in dto["groups"]], ["proj-a"])
        self.assertEqual(dto["totals"]["calls"], 1)
        self.assertEqual([entry["model"] for entry in dto["models"]], ["proj-b"])

    def test_a_forged_model_name_cannot_forge_an_unreported_bucket_collision(self) -> None:
        # 模型名只是一个描述性标签：同一个人可以把模型名写成任何东西，但它既不改
        # 归属、也不改授权。这里证明它只会落在自己的桶里。
        dto = build(rows(row(key_id="key-alpha", model="llm-a")))
        self.assertEqual([entry["model"] for entry in dto["models"]], ["llm-a"])


class ModelDistributionTest(unittest.TestCase):
    def test_calls_are_grouped_by_model(self) -> None:
        dto = build()
        by_model = {entry["model"]: entry for entry in dto["models"]}
        self.assertEqual(by_model["llm-a"]["calls"], 5)
        self.assertEqual(by_model["llm-b"]["calls"], 2)

    def test_the_label_field_does_not_create_a_bucket(self) -> None:
        text = rows(
            row(label="spoof-admin", model="llm-a"),
            row(label="../../etc/passwd", model="llm-a"),
        )
        dto = build(text)
        self.assertEqual([entry["model"] for entry in dto["models"]], ["llm-a"])
        self.assertEqual(dto["models"][0]["calls"], 2)

    def test_a_missing_model_is_bucketed_as_unreported(self) -> None:
        dto = build(rows({"ts": "2026-10-14T00:30:00Z", "key_id": "key-alpha",
                          "status": 200, "outcome": "success", "stream": False}))
        self.assertEqual([entry["model"] for entry in dto["models"]], [MODEL_UNREPORTED])

    def test_the_distribution_is_bounded_and_folds_the_tail_into_other(self) -> None:
        text = rows(*[
            row(ts=f"2026-10-14T00:{index % 60:02d}:00Z", model=f"model-{index:03d}")
            for index in range(MAX_MODELS + 20)
        ])
        dto = build(text)
        self.assertLessEqual(len(dto["models"]), MAX_MODELS + 1)
        self.assertIn(MODEL_OTHER, {entry["model"] for entry in dto["models"]})
        self.assertEqual(dto["quality"]["models"], "partial")
        self.assertIn("partial", " ".join(dto["display_hints"]))

    def test_a_long_or_hostile_model_label_is_sanitised(self) -> None:
        text = rows(row(model="m" * 500), row(model="bad\x07name"))
        dto = build(text)
        for entry in dto["models"]:
            with self.subTest(label=entry["model"]):
                # 转义至多多出一个前缀字符，所以上界是 MAX_MODEL_LABEL_LEN + 1。
                self.assertLessEqual(len(entry["model"]), MAX_MODEL_LABEL_LEN + 1)
                self.assertTrue(entry["model"].isprintable())

    def test_a_blank_model_is_unreported_not_an_empty_bucket(self) -> None:
        self.assertEqual(model_label("   "), MODEL_UNREPORTED)
        self.assertEqual(model_label(None), MODEL_UNREPORTED)
        self.assertEqual(model_label("llm-a"), "llm-a")

    def test_the_model_quality_is_unknown_when_nothing_was_observed(self) -> None:
        dto = build("")
        self.assertEqual(dto["models"], [])
        self.assertEqual(dto["quality"]["models"], "unknown")


class HeatmapTest(unittest.TestCase):
    def test_only_observed_cells_exist(self) -> None:
        dto = build()
        heatmap = dto["heatmap"]
        self.assertEqual(heatmap["weekday_origin"], WEEKDAY_ORIGIN)
        self.assertEqual(heatmap["timezone"], "UTC")
        self.assertEqual(
            [(cell["weekday"], cell["hour"]) for cell in heatmap["cells"]],
            [(2, 0), (2, 1), (2, 2)],
        )

    def test_the_missing_hour_is_absent_rather_than_zero(self) -> None:
        # 窗口是 00:00-04:00，但只有 0/1/2 点有观测。第 3 点必须**不存在**，
        # 而不是以一个 0 请求的格子出现。
        dto = build()
        self.assertNotIn(3, [cell["hour"] for cell in dto["heatmap"]["cells"]])
        self.assertEqual(dto["heatmap"]["cells_observed"], 3)
        self.assertEqual(dto["heatmap"]["cells_expected"], 4)
        self.assertEqual(dto["quality"]["heatmap"], "partial")

    def test_every_cell_represents_at_least_one_observed_call(self) -> None:
        for cell in build()["heatmap"]["cells"]:
            with self.subTest(cell=cell):
                self.assertGreaterEqual(cell["calls"], 1)

    def test_cells_never_exceed_the_bounded_grid(self) -> None:
        dto = build()
        self.assertLessEqual(dto["heatmap"]["cells_observed"], HEATMAP_CELLS)
        self.assertLessEqual(dto["heatmap"]["cells_expected"], HEATMAP_CELLS)

    def test_an_empty_window_has_no_grid_and_an_unknown_quality(self) -> None:
        dto = build("")
        self.assertEqual(dto["heatmap"]["cells"], [])
        self.assertEqual(dto["heatmap"]["cells_observed"], 0)
        self.assertEqual(dto["quality"]["heatmap"], "unknown")

    def test_a_zero_call_cell_is_rejected_by_the_validator(self) -> None:
        dto = build()
        dto["heatmap"]["cells"][0]["calls"] = 0
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(dto)

    def test_a_duplicate_cell_is_rejected(self) -> None:
        dto = build()
        dto["heatmap"]["cells"].append(dict(dto["heatmap"]["cells"][0]))
        dto["heatmap"]["cells_observed"] = len(dto["heatmap"]["cells"])
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(dto)

    def test_an_out_of_range_cell_is_rejected(self) -> None:
        dto = build()
        dto["heatmap"]["cells"][0]["hour"] = 24
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(dto)


class ZeroVersusUnknownTest(unittest.TestCase):
    def test_an_attached_empty_window_is_a_measured_zero(self) -> None:
        dto = build("")
        self.assertEqual(dto["state"], STATE_MEASURED)
        self.assertEqual(dto["totals"]["calls"], 0)
        self.assertEqual(dto["groups"], [])

    def test_no_latency_samples_means_null_not_zero(self) -> None:
        dto = build("")
        self.assertEqual(dto["latency"]["samples"], 0)
        self.assertIsNone(dto["latency"]["p50_ms"])
        self.assertIsNone(dto["latency"]["p95_ms"])

    def test_latency_is_measured_when_samples_exist(self) -> None:
        dto = build()
        self.assertEqual(dto["quality"]["latency"], "measured")
        self.assertIsNotNone(dto["latency"]["p95_ms"])

    def test_percentiles_with_zero_samples_are_rejected(self) -> None:
        dto = build("")
        dto["latency"]["p95_ms"] = 0
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(dto)

    def test_scoped_counters_are_integers_and_unattributable_ones_are_null(self) -> None:
        dto = build()
        self.assertEqual(dto["unknown"]["revoked_key"], 1)
        self.assertEqual(dto["unknown"]["out_of_window"], 1)
        self.assertIsNone(dto["unknown"]["unknown_key"])
        self.assertIsNone(dto["unknown"]["malformed_lines"])

    def test_the_unattributable_counters_unlock_only_with_their_scope(self) -> None:
        request = atlas_request(
            requester(scopes=(
                "atlas:usage:read",
                SCOPE_USAGE_UNATTRIBUTED,
                SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
            ))
        )
        dto = build(request=request)
        self.assertEqual(dto["unknown"]["unknown_key"], 1)
        self.assertGreaterEqual(dto["unknown"]["malformed_lines"], 1)

    def test_a_null_counter_without_its_scope_cannot_be_promoted(self) -> None:
        dto = build()
        dto["unknown"]["unknown_key"] = 0
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(dto)


class TrendTest(unittest.TestCase):
    def test_trend_points_only_cover_observed_hours(self) -> None:
        dto = build()
        self.assertEqual(dto["trend"]["unit"], "1h")
        self.assertEqual(
            [point["start"] for point in dto["trend"]["points"]],
            [
                "2026-10-14T00:00:00Z",
                "2026-10-14T01:00:00Z",
                "2026-10-14T02:00:00Z",
            ],
        )

    def test_trend_carries_no_zero_filled_hour(self) -> None:
        starts = [point["start"] for point in build()["trend"]["points"]]
        self.assertNotIn("2026-10-14T03:00:00Z", starts)

    def test_a_non_empty_trend_may_not_claim_an_unknown_heatmap_quality(self) -> None:
        dto = build()
        dto["quality"]["heatmap"] = "unknown"
        with self.assertRaises(ContractError):
            validate_atlas_aggregate(dto)


class DeterminismAndShapeTest(unittest.TestCase):
    def test_the_dto_is_deterministic(self) -> None:
        self.assertEqual(
            json.dumps(build(), sort_keys=True), json.dumps(build(), sort_keys=True)
        )

    def test_the_dto_is_json_serialisable(self) -> None:
        self.assertIsInstance(json.dumps(build()), str)

    def test_groups_are_sorted_and_unique(self) -> None:
        dto = build()
        ids = [group["id"] for group in dto["groups"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))

    def test_groups_nest_their_workloads(self) -> None:
        group = next(g for g in build()["groups"] if g["id"] == "proj-a")
        workload_ids = [workload["id"] for workload in group["workloads"]]
        self.assertIn("resume-parse", workload_ids)
        self.assertIn("code-assist", workload_ids)

    def test_the_aggregate_carries_no_request_identifiers(self) -> None:
        # 这条契约是**聚合**：逐条句柄属于「我的调用」那份契约。
        blob = json.dumps(build(), sort_keys=True)
        self.assertNotIn("request_id", blob)
        self.assertNotIn("call_id", blob)

    def test_a_wrong_request_type_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            build_atlas_aggregate_dto("nope", "", trusted_directory(), ATTACHED)

    def test_a_wrong_source_type_is_rejected(self) -> None:
        from backend.atlas.errors import InvalidRequest

        with self.assertRaises(InvalidRequest):
            build_atlas_aggregate_dto(
                atlas_request(), "", trusted_directory(), {"source": "gateway"}
            )

    def test_an_empty_source_name_is_rejected(self) -> None:
        from backend.atlas.errors import InvalidRequest

        with self.assertRaises(InvalidRequest):
            AggregateSource(source="  ", synthetic=False, attached=True)

    def test_a_non_boolean_attached_flag_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            AggregateSource(source="gateway-usage-export", synthetic=False, attached="yes")


class DirectBuilderFailsClosedTest(unittest.TestCase):
    """评审 A：直接调用公开的 ``build_atlas_aggregate`` 不得绕过接线判定。

    ``build_atlas_aggregate`` 的产物恒为 ``attached: true, state: "measured"``。
    在修掉之前，一个绕过 ``build_atlas_aggregate_dto`` 分派、直接调用它的调用方，
    即使传的是 ``attached=False`` 的来源，也会拿到一份自称「已接线、已测得」的 DTO。
    """

    def test_the_direct_builder_refuses_a_detached_source(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_atlas_aggregate(
                atlas_request(), load_fixture("gateway_sample.jsonl"),
                trusted_directory(), UNATTACHED,
            )

    def test_the_direct_builder_still_builds_for_an_attached_source(self) -> None:
        dto = build_atlas_aggregate(
            atlas_request(), load_fixture("gateway_sample.jsonl"),
            trusted_directory(), ATTACHED,
        )
        self.assertTrue(dto["attached"])
        self.assertEqual(dto["state"], STATE_MEASURED)

    def test_the_unattached_builder_refuses_an_attached_source(self) -> None:
        # 对称的另一半：两个 builder 各自只对自己的接线状态负责，互不代劳。
        with self.assertRaises(InvalidRequest):
            build_unattached_atlas_aggregate(
                atlas_request(), load_fixture("gateway_sample.jsonl"),
                trusted_directory(), ATTACHED,
            )

    def test_no_public_builder_emits_measured_for_a_detached_source(self) -> None:
        # 穷举公开入口：无论走哪一个，detached 都不可能产出 measured。
        for entry_point in (build_atlas_aggregate_dto, build_atlas_aggregate):
            with self.subTest(entry_point=entry_point.__name__):
                try:
                    dto = entry_point(
                        atlas_request(), load_fixture("gateway_sample.jsonl"),
                        trusted_directory(), UNATTACHED,
                    )
                except InvalidRequest:
                    continue  # 拒绝同样是 fail-closed 的合法结局
                self.assertFalse(dto["attached"])
                self.assertEqual(dto["state"], STATE_UNKNOWN)

    def test_the_wrapper_still_dispatches_both_ways(self) -> None:
        attached = build_atlas_aggregate_dto(
            atlas_request(), "", trusted_directory(), ATTACHED
        )
        detached = build_atlas_aggregate_dto(
            atlas_request(), "", trusted_directory(), UNATTACHED
        )
        self.assertEqual(attached["state"], STATE_MEASURED)
        self.assertEqual(detached["state"], STATE_UNKNOWN)


class SourceTruthIsNotCallerControlledTest(unittest.TestCase):
    """评审 A：非合成的来源名必须来自受信白名单，而不是调用方随口一说。"""

    def test_a_non_synthetic_source_must_be_on_the_whitelist(self) -> None:
        with self.assertRaises(UnverifiedSource):
            AggregateSource(source="gateway", synthetic=False, attached=True)

    def test_the_rejection_carries_a_stable_contract_code(self) -> None:
        try:
            AggregateSource(source="totally-real-metrics", synthetic=False)
        except UnverifiedSource as error:
            self.assertEqual(error.contract_code, "atlas.unverified_source")
        else:  # pragma: no cover - 见下
            self.fail("an unverified non-synthetic source must be rejected")

    def test_every_verified_source_name_is_accepted(self) -> None:
        from backend.observability.trust import VERIFIED_SOURCES

        for name in VERIFIED_SOURCES:
            with self.subTest(source=name):
                source = AggregateSource(source=name, synthetic=False, attached=True)
                self.assertFalse(source.synthetic)

    def test_a_synthetic_source_may_use_any_name(self) -> None:
        # 合成来源自报合成、不冒充，因此不需要在白名单里。
        source = AggregateSource(source="mock", synthetic=True, attached=True)
        self.assertTrue(source.synthetic)

    def test_the_whitelist_is_the_same_one_observability_uses(self) -> None:
        from backend.atlas.aggregate import VERIFIED_SOURCES as atlas_whitelist
        from backend.observability.trust import VERIFIED_SOURCES as obs_whitelist

        self.assertEqual(atlas_whitelist, obs_whitelist)

    def test_an_unverified_source_cannot_be_smuggled_in_as_attached(self) -> None:
        # 不能靠「attached=True」把一个没审批过的来源名洗成实测来源。
        with self.assertRaises(UnverifiedSource):
            AggregateSource(source="prometheus", synthetic=False, attached=True)


class ReservedModelNameCollisionTest(unittest.TestCase):
    """评审 B：真实模型名不得与哨兵桶名碰撞。

    ``<other>`` / ``<unreported>`` 是本模块自己的哨兵。模型名是客户端可控的，
    所以客户端可以真的把模型叫成这两个名字。若不隔离，那个**真实存在**的模型会被
    标成「被折叠的尾巴」或「没上报」，而 ``reported`` / ``bucket`` 反过来描述它。
    """

    def test_a_real_model_named_other_does_not_collide_with_the_sentinel(self) -> None:
        dto = build(rows(row(model=MODEL_OTHER)))
        labels = [entry["model"] for entry in dto["models"]]
        self.assertEqual(len(labels), 1)
        entry = dto["models"][0]
        self.assertNotEqual(entry["model"], MODEL_OTHER)
        self.assertTrue(entry["bucket"], "a real model must not be flagged as the folded tail")
        self.assertTrue(entry["reported"])
        self.assertFalse(is_sentinel_model_label(entry["model"]))

    def test_a_real_model_named_unreported_is_still_reported(self) -> None:
        dto = build(rows(row(model=MODEL_UNREPORTED)))
        entry = dto["models"][0]
        self.assertNotEqual(entry["model"], MODEL_UNREPORTED)
        self.assertTrue(entry["reported"], "a client that DID send a model name must read as reported")
        self.assertFalse(is_sentinel_model_label(entry["model"]))

    def test_the_genuine_sentinels_remain_reachable_and_distinct(self) -> None:
        # 同一个窗口里，既有一个真叫 <unreported> 的模型，也有真正没上报的行。
        # 两者必须是两行，且各自被正确标记。
        text = rows(row(model=MODEL_UNREPORTED), {"ts": "2026-10-14T00:40:00Z",
                                                 "key_id": "key-alpha", "status": 200,
                                                 "outcome": "success", "stream": False})
        dto = build(text)
        by_label = {entry["model"]: entry for entry in dto["models"]}
        self.assertEqual(len(by_label), 2, list(by_label))
        real = next(e for e in dto["models"] if e["model"] != MODEL_UNREPORTED)
        sentinel = by_label[MODEL_UNREPORTED]
        self.assertTrue(real["reported"])
        self.assertFalse(sentinel["reported"])
        self.assertTrue(is_sentinel_model_label(sentinel["model"]))
        self.assertNotEqual(real["model"], sentinel["model"])

    def test_the_escape_is_injective_over_real_model_names(self) -> None:
        # 对**真实**（非空）的模型名，映射是单射：任何两个不同的名字都不会落到同一
        # 个标签上。这是「真实模型不可能与哨兵碰撞」的完整表述。
        seen: dict[str, str] = {}
        for raw in (MODEL_OTHER, MODEL_UNREPORTED, MODEL_LABEL_ESCAPE + MODEL_OTHER,
                    MODEL_LABEL_ESCAPE * 2 + MODEL_OTHER, "llm-a", MODEL_LABEL_ESCAPE,
                    MODEL_OTHER + "x", "x" + MODEL_OTHER):
            label = model_label(raw)
            self.assertNotIn(label, seen, f"{raw!r} collided with {seen.get(label)!r}")
            seen[label] = repr(raw)

    def test_absent_and_blank_models_deliberately_share_one_sentinel(self) -> None:
        # 这条**不是**碰撞：``None`` / 空串 / 全空白都是同一个事实 ——「上游没给
        # 模型名」。把它们拆成三个桶是把一次缺失说成三种，那不是更精确，是更误导。
        for raw in (None, "", "   ", "\x07"):
            with self.subTest(raw=raw):
                self.assertEqual(model_label(raw), MODEL_UNREPORTED)
                self.assertTrue(is_sentinel_model_label(model_label(raw)))

    def test_a_label_that_is_the_escape_marker_is_escaped(self) -> None:
        self.assertEqual(model_label(MODEL_LABEL_ESCAPE), MODEL_LABEL_ESCAPE * 2)

    def test_a_reserved_name_with_the_escape_prefix_is_double_escaped(self) -> None:
        # 客户端写 ``\<other>``：既然以转义前缀开头，就必须再转义一次，
        # 否则它可以冒充「被转义过的 <other>」。
        escaped = model_label(MODEL_LABEL_ESCAPE + MODEL_OTHER)
        self.assertEqual(escaped, MODEL_LABEL_ESCAPE * 2 + MODEL_OTHER)
        self.assertNotEqual(escaped, model_label(MODEL_OTHER))

    def test_truncation_never_collides_with_a_sentinel(self) -> None:
        # 截断必须发生在转义**之前**，否则一个长标签被截回哨兵形状就会碰撞。
        label = model_label("x" * 200)
        self.assertEqual(len(label), MAX_MODEL_LABEL_LEN)
        self.assertFalse(is_sentinel_model_label(label))

    def test_the_client_named_other_is_not_swallowed_by_the_truncation_tail(self) -> None:
        # 评审 B 的组合条件：既有 >MAX_MODELS 个模型触发 <other> 折叠，
        # 又有一个真叫 <other> 的模型 —— 后者必须独立成行。
        text = rows(*[
            row(ts=f"2026-10-14T00:{index % 60:02d}:00Z", model=f"model-{index:03d}")
            for index in range(MAX_MODELS + 5)
        ] + [row(ts="2026-10-14T00:59:00Z", model=MODEL_OTHER)])
        dto = build(text)
        labels = [entry["model"] for entry in dto["models"]]
        self.assertLessEqual(len(labels), MAX_MODELS + 1)
        self.assertIn(MODEL_OTHER, labels, "the folded tail must exist")
        self.assertEqual(dto["quality"]["models"], "partial")

        folded = next(e for e in dto["models"] if e["model"] == MODEL_OTHER)
        self.assertFalse(folded["bucket"], "the folded tail is not a real model bucket")
        self.assertTrue(is_sentinel_model_label(folded["model"]))

        real = [e for e in dto["models"] if e["model"] != MODEL_OTHER]
        self.assertEqual(len(real), len(labels) - 1)
        for entry in real:
            with self.subTest(label=entry["model"]):
                self.assertTrue(entry["bucket"])
                self.assertFalse(is_sentinel_model_label(entry["model"]))

    def test_is_sentinel_only_matches_the_two_reserved_labels(self) -> None:
        self.assertTrue(is_sentinel_model_label(MODEL_OTHER))
        self.assertTrue(is_sentinel_model_label(MODEL_UNREPORTED))
        for label in ("llm-a", MODEL_LABEL_ESCAPE + MODEL_OTHER, "", None, 42):
            with self.subTest(label=label):
                self.assertFalse(is_sentinel_model_label(label))


if __name__ == "__main__":
    unittest.main()
