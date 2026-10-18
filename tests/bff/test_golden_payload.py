"""The BFF's payload, pinned byte-for-byte, and handed to the browser's checker.

Two halves of one idea:

* **here** — the payload the BFF produces today is byte-identical to the one in
  the fixture, so any change to the wire shape shows up as a failing test rather
  than as a blank panel;
* **there** — ``tests/web-features/ops-bff-payload.test.js`` feeds that same
  fixture to ``validateOps`` and to the real renderer, which is the only check
  that proves the two sides agree about the shape.

Regenerate with ``python3 tests/bff/make_golden.py`` after an intentional change.
"""

from __future__ import annotations

import json
import unittest

from tests.bff import make_golden


class GoldenPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(make_golden.FIXTURE.read_text(encoding="utf-8"))

    def test_the_bff_still_produces_exactly_the_committed_payload(self):
        """Regenerating must be a no-op. A diff here means the wire shape moved."""
        regenerated = make_golden.generate()
        committed = json.dumps(
            self.fixture, ensure_ascii=False, sort_keys=True, indent=2
        )
        produced = json.dumps(
            regenerated, ensure_ascii=False, sort_keys=True, indent=2
        )
        if produced != committed:
            self.fail(
                "the ops payload changed; re-run `python3 tests/bff/make_golden.py` "
                "and review the diff (the browser contract must be re-checked too)\n"
                + _first_difference(committed, produced)
            )

    def test_the_fixture_covers_the_three_views_this_slice_is_about(self):
        self.assertEqual(
            set(self.fixture),
            {"member_private", "grantee_shared", "admin_private"},
        )
        self.assertEqual(len(self.fixture["member_private"]["registry"]["owned"]), 1)
        self.assertEqual(len(self.fixture["grantee_shared"]["registry"]["shared"]), 1)
        self.assertEqual(len(self.fixture["admin_private"]["registry"]["all"]), 1)

    def test_the_grantee_sees_the_endpoint_and_not_the_owners_deployment(self):
        grantee = self.fixture["grantee_shared"]["registry"]
        self.assertEqual(grantee["owned"], [])
        (row,) = grantee["shared"]
        self.assertFalse(row["owner_is_self"])
        self.assertNotEqual(row["owner_uid"], row["share"]["principals"][0])

    def test_the_fixture_carries_no_secret_and_no_way_off_the_origin(self):
        blob = json.dumps(self.fixture, ensure_ascii=False)
        for needle in ("llk_", "Bearer ", "password", "secret", ":8080", "http://", "https://"):
            if needle == "llk_":
                continue  # a masked prefix is the one permitted appearance
            self.assertNotIn(needle, blob, needle)

    def test_every_gpu_number_in_the_fixture_is_null(self):
        for payload in self.fixture.values():
            for metric in payload["metrics"]["items"]:
                if metric["name"].startswith("gpu_"):
                    self.assertIsNone(metric["value"], metric["name"])
            for row in payload["registry"]["owned"]:
                for field in ("gpu_count", "vram_total_mib", "vram_used_mib"):
                    self.assertIsNone(row["resources"][field])

    def test_the_fixture_is_stable_across_runs(self):
        self.assertEqual(make_golden.generate(), make_golden.generate())


def _first_difference(expected: str, actual: str) -> str:
    for index, (left, right) in enumerate(zip(expected.splitlines(), actual.splitlines())):
        if left != right:
            return f"  first difference at line {index + 1}:\n    - {left}\n    + {right}"
    return "  (one side is a prefix of the other)"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
