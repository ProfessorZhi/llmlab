"""DTO 形状自检、序列化稳定性、来源声明与窗口校验。"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import (  # noqa: E402
    SCHEMA_VERSION,
    SYNTHETIC,
    atlas_request,
    load_fixture,
    requester,
    trusted_directory,
)

from backend.atlas import (  # noqa: E402
    MAX_WINDOW_HOURS,
    ContractError,
    InvalidBucket,
    InvalidWindow,
    Provenance,
    build_atlas_dto,
    validate_dto,
    validate_window,
)

REQUIRED_TOP_LEVEL = (
    "source",
    "synthetic",
    "schema_version",
    "scope",
    "window",
    "quality",
    "totals",
    "latency",
    "ttft",
    "tokens",
    "groups",
    "buckets",
    "unknown",
    "unattributed",
    "display_hints",
    "notes",
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class DtoContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = load_fixture("gateway_sample.jsonl")

    def _build(self, provenance=SYNTHETIC):
        return build_atlas_dto(
            atlas_request(requester()), self.text, trusted_directory(), provenance
        )

    def test_dto_has_every_documented_top_level_field(self) -> None:
        dto = self._build()
        for key in REQUIRED_TOP_LEVEL:
            with self.subTest(key=key):
                self.assertIn(key, dto)
        self.assertEqual(dto["schema_version"], SCHEMA_VERSION)

    def test_validate_dto_accepts_a_real_dto(self) -> None:
        validate_dto(self._build())

    def test_dto_is_json_serialisable_without_custom_encoders(self) -> None:
        json.dumps(self._build(), sort_keys=True, allow_nan=False)

    def test_dto_is_deterministic_across_runs(self) -> None:
        first = json.dumps(self._build(), sort_keys=True)
        second = json.dumps(self._build(), sort_keys=True)
        self.assertEqual(first, second)

    def test_provenance_has_no_default_and_must_be_explicit(self) -> None:
        with self.assertRaises(TypeError):
            Provenance(source="gateway")  # type: ignore[call-arg]

    def test_synthetic_data_is_labelled_in_display_hints(self) -> None:
        dto = self._build(Provenance(source="gateway", synthetic=True))
        self.assertTrue(dto["synthetic"])
        self.assertTrue(
            any("SYNTHETIC" in hint or "DEMO" in hint for hint in dto["display_hints"]),
            dto["display_hints"],
        )

    def test_non_synthetic_does_not_get_a_demo_marker(self) -> None:
        dto = self._build(Provenance(source="gateway", synthetic=False))
        self.assertFalse(dto["synthetic"])
        self.assertFalse(any("DEMO" in hint for hint in dto["display_hints"]))

    def test_window_block_is_utc_and_declares_expected_buckets(self) -> None:
        window = self._build()["window"]
        self.assertEqual(window["from"], "2026-10-14T00:00:00Z")
        self.assertEqual(window["to"], "2026-10-14T04:00:00Z")
        self.assertEqual(window["bucket"], "1h")
        self.assertEqual(window["timezone"], "UTC")
        self.assertEqual(window["hours"], 4)

    def test_scope_block_records_the_authorisation_basis(self) -> None:
        scope = self._build()["scope"]
        self.assertEqual(scope["subject"], "u-1001")
        self.assertEqual(scope["projects"], ["proj-a"])
        self.assertEqual(scope["scopes"], ["atlas:usage:read"])
        self.assertFalse(scope["cross_project"])
        self.assertFalse(scope["unattributed_visible"])

    def test_notes_record_the_percentile_basis(self) -> None:
        notes = " ".join(self._build()["notes"])
        self.assertIn("p95", notes)

    # ---- validate_dto 得真有牙齿 ----------------------------------------

    def test_validate_dto_rejects_missing_field(self) -> None:
        dto = self._build()
        del dto["buckets"]
        with self.assertRaises(ContractError):
            validate_dto(dto)

    def test_validate_dto_rejects_zero_substituted_for_unknown(self) -> None:
        dto = copy.deepcopy(self._build())
        dto["latency"]["samples"] = 0
        dto["latency"]["p50_ms"] = 0.0
        with self.assertRaises(ContractError):
            validate_dto(dto)

    def test_validate_dto_rejects_negative_counters(self) -> None:
        dto = copy.deepcopy(self._build())
        dto["unknown"]["malformed_lines"] = -1
        with self.assertRaises(ContractError):
            validate_dto(dto)


class WindowValidationTest(unittest.TestCase):
    def test_naive_datetimes_are_rejected(self) -> None:
        with self.assertRaises(InvalidWindow):
            validate_window(
                datetime(2026, 10, 14, 0, 0), datetime(2026, 10, 14, 4, 0), "1h"
            )

    def test_inverted_or_empty_window_is_rejected(self) -> None:
        for start, end in (
            ("2026-10-14T04:00:00Z", "2026-10-14T00:00:00Z"),
            ("2026-10-14T00:00:00Z", "2026-10-14T00:00:00Z"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(InvalidWindow):
                    validate_window(_utc(start), _utc(end), "1h")

    def test_oversized_window_is_rejected(self) -> None:
        start = _utc("2026-10-01T00:00:00Z")
        end = start + timedelta(hours=MAX_WINDOW_HOURS + 1)
        with self.assertRaises(InvalidWindow):
            validate_window(start, end, "1h")

    def test_unsupported_bucket_is_rejected(self) -> None:
        for bucket in ("15m", "1w", "", "hour"):
            with self.subTest(bucket=bucket):
                with self.assertRaises(InvalidBucket):
                    validate_window(
                        _utc("2026-10-14T00:00:00Z"), _utc("2026-10-14T04:00:00Z"), bucket
                    )

    def test_non_utc_input_is_normalised_to_utc(self) -> None:
        start, end = validate_window(
            datetime(2026, 10, 14, 8, 0, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 10, 14, 12, 0, tzinfo=timezone(timedelta(hours=8))),
            "1h",
        )
        self.assertEqual(start, _utc("2026-10-14T00:00:00Z"))
        self.assertEqual(end, _utc("2026-10-14T04:00:00Z"))
        self.assertEqual(start.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
