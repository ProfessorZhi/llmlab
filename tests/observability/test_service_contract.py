"""四种 DTO 的形状契约：信封齐备、分页有界、外部 UI 授权入口、以及 self-check 有牙齿。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import (  # noqa: E402
    ALICE_ROWS,
    ENDPOINT_SHARED_A,
    ENDPOINT_SHARED_B,
    FOREIGN_ROWS,
    NOW,
    SYNTHETIC,
    UID_ALICE,
    admin_principal,
    host_only_principal,
    principal,
    registry,
    request,
    row,
    text_of,
    trusted_directory,
)

from backend.observability import (  # noqa: E402
    KIND_ENGINE_STATUS,
    KIND_TIMELINE,
    KIND_USAGE_ADMIN,
    KIND_USAGE_SELF,
    MAX_LIMIT,
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    SCHEMA_VERSION,
    ContractError,
    InvalidRequest,
    InvalidWindow,
    MockEngineProbe,
    EngineProbeResult,
    EngineRegistry,
    build_admin_overview_dto,
    build_engine_status_dto,
    build_self_usage_dto,
    build_timeline_dto,
    validate_engine_status_dto,
    validate_timeline_dto,
    validate_usage_dto,
)

DIRECTORY = trusted_directory()


def _own(rows=ALICE_ROWS, ctx=None, **kwargs):
    return build_self_usage_dto(
        request(ctx or principal(UID_ALICE), **kwargs),
        text_of(rows),
        DIRECTORY,
        registry(),
        SYNTHETIC,
        now=NOW,
    )


def _timeline(rows=ALICE_ROWS, ctx=None, **kwargs):
    return build_timeline_dto(
        request(ctx or principal(UID_ALICE), **kwargs),
        text_of(rows),
        DIRECTORY,
        registry(),
        SYNTHETIC,
        now=NOW,
    )


def _admin(rows=ALICE_ROWS + FOREIGN_ROWS, ctx=None, **kwargs):
    return build_admin_overview_dto(
        request(ctx or admin_principal(), **kwargs),
        text_of(rows),
        DIRECTORY,
        registry(),
        SYNTHETIC,
        now=NOW,
    )


def _engines(ctx=None, **kwargs):
    return build_engine_status_dto(
        request(ctx or principal(UID_ALICE), **kwargs),
        registry(),
        SYNTHETIC,
        now=NOW,
    )


class EnvelopeTest(unittest.TestCase):
    def test_every_kind_carries_the_shared_envelope(self) -> None:
        cases = (
            (_own(), KIND_USAGE_SELF),
            (_admin(), KIND_USAGE_ADMIN),
            (_engines(), KIND_ENGINE_STATUS),
            (_timeline(), KIND_TIMELINE),
        )
        for dto, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(dto["kind"], kind)
                self.assertEqual(dto["schema_version"], SCHEMA_VERSION)
                self.assertIs(dto["synthetic"], True)
                self.assertIsInstance(dto["source"], str)
                for key in ("scope", "window", "page", "display_hints", "notes"):
                    self.assertIn(key, dto)
                json.dumps(dto, sort_keys=True)

    def test_every_window_declares_utc_and_a_finite_span(self) -> None:
        for dto in (_own(), _admin(), _engines(), _timeline()):
            with self.subTest(kind=dto["kind"]):
                window = dto["window"]
                self.assertEqual(window["timezone"], "UTC")
                self.assertEqual(window["bucket"], "1h")
                self.assertTrue(window["from"].endswith("Z"))
                self.assertTrue(window["to"].endswith("Z"))
                self.assertEqual(window["hours"], 4)

    def test_synthetic_data_must_announce_itself(self) -> None:
        for dto in (_own(), _admin(), _engines(), _timeline()):
            with self.subTest(kind=dto["kind"]):
                self.assertTrue(
                    any("SYNTHETIC" in hint for hint in dto["display_hints"]),
                    dto["display_hints"],
                )

    def test_non_synthetic_views_still_validate(self) -> None:
        from observability_support import LIVE_LIKE

        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            text_of(ALICE_ROWS),
            DIRECTORY,
            registry(),
            LIVE_LIKE,
            now=NOW,
        )
        self.assertIs(dto["synthetic"], False)
        validate_usage_dto(dto)

    def test_scope_block_declares_every_visibility_flag(self) -> None:
        scope = _own()["scope"]
        for flag in ("admin", "host_visible", "cross_endpoint", "is_self_view"):
            with self.subTest(flag=flag):
                self.assertIsInstance(scope[flag], bool)

    def test_a_synthetic_dto_without_the_hint_is_rejected(self) -> None:
        dto = _own()
        dto["display_hints"] = ["nothing to see"]
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)


class PaginationTest(unittest.TestCase):
    def test_default_page_is_the_whole_small_set(self) -> None:
        dto = _own()
        self.assertEqual(dto["page"]["limit"], 50)
        self.assertEqual(dto["page"]["offset"], 0)
        self.assertEqual(dto["page"]["returned"], dto["page"]["total"])
        self.assertFalse(dto["page"]["has_more"])

    def test_limit_and_offset_slice_the_groups(self) -> None:
        ctx = principal(UID_ALICE, endpoints=(ENDPOINT_SHARED_A, ENDPOINT_SHARED_B))
        rows = ALICE_ROWS + (
            row("2026-10-15T11:20:00Z", "key-alpha", endpoint=ENDPOINT_SHARED_B),
        )
        full = _own(rows, ctx)
        self.assertEqual(full["page"]["total"], 2)

        page = _own(rows, ctx, limit=1, offset=0)
        self.assertEqual(page["page"]["returned"], 1)
        self.assertTrue(page["page"]["has_more"])
        self.assertEqual(page["groups"][0]["id"], ENDPOINT_SHARED_A)

        second = _own(rows, ctx, limit=1, offset=1)
        self.assertEqual(second["groups"][0]["id"], ENDPOINT_SHARED_B)
        self.assertFalse(second["page"]["has_more"])

    def test_offset_past_the_end_returns_an_empty_page(self) -> None:
        dto = _own(limit=10, offset=99)
        self.assertEqual(dto["groups"], [])
        self.assertEqual(dto["page"]["returned"], 0)
        self.assertFalse(dto["page"]["has_more"])

    def test_limit_above_the_cap_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            _own(limit=MAX_LIMIT + 1)

    def test_non_positive_limit_is_rejected(self) -> None:
        for bad in (0, -1):
            with self.subTest(limit=bad):
                with self.assertRaises(InvalidRequest):
                    _own(limit=bad)

    def test_negative_offset_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            _own(offset=-1)

    def test_non_integer_paging_is_rejected(self) -> None:
        for bad in ("50", 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRequest):
                    _own(limit=bad)

    def test_admin_principals_are_paginated(self) -> None:
        rows = ALICE_ROWS + FOREIGN_ROWS + (
            row("2026-10-15T08:35:00Z", "key-beta", endpoint=ENDPOINT_SHARED_A),
        )
        dto = _admin(rows, limit=1)
        self.assertGreaterEqual(dto["page"]["total"], 2)
        self.assertEqual(dto["page"]["returned"], 1)
        self.assertTrue(dto["page"]["has_more"])

    def test_page_block_is_validated(self) -> None:
        dto = _own()
        dto["page"]["has_more"] = True
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)


class WindowBoundaryInDtoTest(unittest.TestCase):
    def test_a_row_at_the_exclusive_end_is_not_counted(self) -> None:
        rows = ALICE_ROWS + (
            row("2026-10-15T12:00:00Z", "key-alpha"),  # 恰好是 to
        )
        dto = _own(rows)
        self.assertEqual(dto["counters"]["calls"], 3)
        self.assertEqual(dto["unknown"]["out_of_window"], 1)

    def test_a_row_at_the_inclusive_start_is_counted(self) -> None:
        rows = (row("2026-10-15T08:00:00Z", "key-alpha"),)
        dto = _own(rows)
        self.assertEqual(dto["counters"]["calls"], 1)
        self.assertEqual(dto["unknown"]["out_of_window"], 0)

    def test_a_window_that_is_too_long_is_rejected(self) -> None:
        with self.assertRaises(InvalidWindow):
            _own(window_from="2026-01-01T00:00:00Z", window_to="2026-12-31T00:00:00Z")

    def test_a_five_minute_window_is_accepted(self) -> None:
        dto = _own(
            bucket="5m",
            window_from="2026-10-15T08:00:00Z",
            window_to="2026-10-15T08:30:00Z",
        )
        self.assertEqual(dto["window"]["bucket"], "5m")
        self.assertEqual(dto["buckets"]["expected_count"], 6)

    def test_a_daily_window_is_accepted(self) -> None:
        dto = _own(
            bucket="1d",
            window_from="2026-10-14T00:00:00Z",
            window_to="2026-10-16T00:00:00Z",
        )
        self.assertEqual(dto["window"]["bucket"], "1d")
        self.assertEqual(dto["buckets"]["expected_count"], 2)

    def test_the_dto_window_matches_the_request(self) -> None:
        dto = _own()
        self.assertEqual(dto["window"]["from"], "2026-10-15T08:00:00Z")
        self.assertEqual(dto["window"]["to"], "2026-10-15T12:00:00Z")


class BucketCoverageTest(unittest.TestCase):
    def test_full_coverage_is_measured(self) -> None:
        rows = ALICE_ROWS + (
            row("2026-10-15T11:05:00Z", "key-alpha"),
        )
        dto = _own(rows)
        self.assertEqual(dto["buckets"]["expected_count"], 4)
        self.assertEqual(dto["buckets"]["observed_count"], 4)
        self.assertEqual(dto["quality"]["buckets"], QUALITY_MEASURED)

    def test_partial_coverage_is_partial(self) -> None:
        dto = _own(ALICE_ROWS)  # 08 / 09 / 10 各一个桶，11 缺
        self.assertEqual(dto["buckets"]["observed_count"], 3)
        self.assertEqual(dto["buckets"]["expected_count"], 4)
        self.assertEqual(dto["quality"]["buckets"], QUALITY_PARTIAL)

    def test_no_coverage_is_unknown_and_the_list_is_empty(self) -> None:
        dto = _own([])
        self.assertEqual(dto["buckets"]["observed_count"], 0)
        self.assertEqual(dto["buckets"]["items"], [])
        self.assertEqual(dto["quality"]["buckets"], QUALITY_UNKNOWN)

    def test_missing_buckets_are_absent_not_zero_filled(self) -> None:
        dto = _own(ALICE_ROWS)
        starts = [item["start"] for item in dto["buckets"]["items"]]
        self.assertEqual(starts, ["2026-10-15T08:00:00Z", "2026-10-15T09:00:00Z",
                                  "2026-10-15T10:00:00Z"])
        self.assertNotIn("2026-10-15T11:00:00Z", starts)

    def test_timeline_coverage_reports_the_shortfall_explicitly(self) -> None:
        dto = _timeline(ALICE_ROWS)
        coverage = dto["coverage"]
        self.assertEqual(coverage["expected_count"], 4)
        self.assertEqual(coverage["observed_count"], 3)
        self.assertEqual(coverage["missing_count"], 1)
        self.assertEqual(coverage["basis"], "expected-buckets-within-window")
        self.assertTrue(any("不补 0" in hint for hint in dto["display_hints"]))

    def test_coverage_block_is_validated(self) -> None:
        dto = _timeline(ALICE_ROWS)
        dto["coverage"]["missing_count"] = 0
        with self.assertRaises(ContractError):
            validate_timeline_dto(dto)


class EngineStatusDtoTest(unittest.TestCase):
    def test_a_plain_user_sees_only_authorised_engines(self) -> None:
        dto = _engines()
        self.assertEqual(dto["counts"]["total"], 1)
        self.assertEqual(dto["engines"][0]["endpoint_id"], ENDPOINT_SHARED_A)

    def test_without_a_probe_every_engine_is_unknown(self) -> None:
        """本任务未接线探测 —— 默认形态是一整屏 unknown，不是一整屏绿色。"""
        dto = _engines()
        self.assertEqual(dto["counts"]["unknown"], 1)
        self.assertEqual(dto["counts"]["up"], 0)
        self.assertEqual(dto["counts"]["down"], 0)
        self.assertEqual(dto["quality"]["engines"], QUALITY_UNKNOWN)
        self.assertTrue(any("UNKNOWN" in hint for hint in dto["display_hints"]))

    def test_counts_partition_the_total(self) -> None:
        probe = MockEngineProbe(
            {
                "engine-shared-a": EngineProbeResult(
                    up=True, observed_at=NOW, replicas_up=2
                ),
                "engine-shared-b": EngineProbeResult(up=False, observed_at=NOW),
                "engine-private-c": EngineProbeResult(
                    up=None, reason="source_unavailable"
                ),
                "engine-retired": EngineProbeResult(
                    up=True, observed_at=NOW, replicas_up=1
                ),
            }
        )
        dto = build_engine_status_dto(
            request(admin_principal()), registry(), SYNTHETIC, now=NOW, engine_probe=probe
        )
        counts = dto["counts"]
        self.assertEqual(counts["total"], 4)
        self.assertEqual(
            counts["up"] + counts["degraded"] + counts["down"] + counts["unknown"], 4
        )
        validate_engine_status_dto(dto)

    def test_a_degraded_engine_is_counted_separately_from_down(self) -> None:
        probe = MockEngineProbe(
            {
                "engine-shared-a": EngineProbeResult(
                    up=True, observed_at=NOW, replicas_up=1
                )
            }
        )
        dto = build_engine_status_dto(
            request(principal(UID_ALICE)), registry(), SYNTHETIC, now=NOW, engine_probe=probe
        )
        self.assertEqual(dto["counts"]["degraded"], 1)
        self.assertEqual(dto["counts"]["down"], 0)

    def test_no_engines_visible_is_unknown_quality_not_a_clean_bill(self) -> None:
        ctx = principal(UID_ALICE, endpoints=("model-not-registered",))
        dto = _engines(ctx)
        self.assertEqual(dto["counts"]["total"], 0)
        self.assertEqual(dto["quality"]["engines"], QUALITY_UNKNOWN)

    def test_an_empty_catalogue_does_not_crash(self) -> None:
        dto = build_engine_status_dto(
            request(admin_principal()),
            EngineRegistry([]),
            SYNTHETIC,
            now=NOW,
        )
        self.assertEqual(dto["counts"]["total"], 0)
        self.assertEqual(dto["engines"], [])

    def test_counts_must_partition_and_the_validator_enforces_it(self) -> None:
        dto = _engines()
        dto["counts"]["up"] = 1
        with self.assertRaises(ContractError):
            validate_engine_status_dto(dto)

    def test_engine_status_dto_has_no_usage_unknown_family(self) -> None:
        """引擎视图不处理 Gateway 行，因此不带那一族计数器。"""
        dto = _engines()
        self.assertNotIn("unknown", dto)
        self.assertTrue(any("没有 unknown 计数器族" in note for note in dto["notes"]))


class ExternalUiTest(unittest.TestCase):
    """原生 UI 保留授权入口，但**不**开放任意 iframe，也**不**下发外部 URL。"""

    def test_an_admin_sees_all_three_entries(self) -> None:
        entries = {entry["id"]: entry for entry in _admin()["external_ui"]["entries"]}
        self.assertEqual({"grafana", "prometheus", "evalscope"}, set(entries))
        for entry in entries.values():
            with self.subTest(entry=entry["id"]):
                self.assertTrue(entry["visible"])
                self.assertTrue(entry["path"].startswith("/observability/ui/"))

    def test_a_plain_user_sees_no_native_ui_entry(self) -> None:
        entries = _own()["external_ui"]["entries"]
        for entry in entries:
            with self.subTest(entry=entry["id"]):
                self.assertFalse(entry["visible"])
                self.assertIsNone(entry["path"])

    def test_a_host_scope_reader_does_not_get_the_native_dashboards(self) -> None:
        # Host readings are shared with every member (portal charts), but the
        # native Grafana / Prometheus / EvalScope UIs stay admin-only.
        entries = {
            entry["id"]: entry
            for entry in _own(ALICE_ROWS, host_only_principal(UID_ALICE))["external_ui"][
                "entries"
            ]
        }
        self.assertFalse(entries["grafana"]["visible"])
        self.assertFalse(entries["prometheus"]["visible"])
        self.assertEqual(entries["grafana"]["requires"], "observability:admin:read")
        self.assertFalse(entries["evalscope"]["visible"])
        self.assertEqual(entries["evalscope"]["requires"], "observability:admin:read")

    def test_iframes_are_forbidden_with_a_reason(self) -> None:
        block = _admin()["external_ui"]
        self.assertIs(block["iframe_allowed"], False)
        self.assertTrue(block["iframe_reason"])
        self.assertIn("iframe", block["iframe_reason"])

    def test_external_url_is_always_null(self) -> None:
        for dto in (_own(), _admin()):
            for entry in dto["external_ui"]["entries"]:
                with self.subTest(entry=entry["id"]):
                    self.assertIsNone(entry["external_url"])

    def test_the_policy_is_named(self) -> None:
        self.assertEqual(
            _admin()["external_ui"]["policy"], "same-origin-proxy-with-session-auth"
        )


class RequestValidationTest(unittest.TestCase):
    def test_a_non_request_object_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_self_usage_dto("nope", "", DIRECTORY, registry(), SYNTHETIC, now=NOW)

    def test_a_non_provenance_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_self_usage_dto(
                request(principal(UID_ALICE)), "", DIRECTORY, registry(), "synthetic", now=NOW
            )

    def test_self_usage_requires_a_directory(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_self_usage_dto(
                request(principal(UID_ALICE)), "", "not-a-directory", registry(),
                SYNTHETIC, now=NOW,
            )

    def test_self_usage_requires_a_registry(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_self_usage_dto(
                request(principal(UID_ALICE)), "", DIRECTORY, "not-a-registry",
                SYNTHETIC, now=NOW,
            )

    def test_engine_status_requires_a_registry(self) -> None:
        with self.assertRaises(InvalidRequest):
            build_engine_status_dto(
                request(principal(UID_ALICE)), "not-a-registry", SYNTHETIC, now=NOW
            )

    def test_a_provenance_with_an_empty_source_is_rejected(self) -> None:
        from backend.observability import Provenance

        with self.assertRaises(InvalidRequest):
            Provenance("", True)

    def test_input_limits_are_honoured(self) -> None:
        from backend.observability import IngestLimits, InputLimitExceeded

        with self.assertRaises(InputLimitExceeded):
            build_self_usage_dto(
                request(principal(UID_ALICE)),
                text_of(ALICE_ROWS),
                DIRECTORY,
                registry(),
                SYNTHETIC,
                now=NOW,
                limits=IngestLimits(max_lines=1),
            )


if __name__ == "__main__":
    unittest.main()
