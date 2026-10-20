"""Contract tests for read-only engine candidate discovery.

The properties pinned here are the ones that keep discovery from becoming a
silent auto-registration or an ownership guess:

* a candidate is never registered, shareable, invokable, or owned;
* only loopback targets are described, and only over http(s) without embedded
  credentials, paths, queries or fragments;
* duplicates collapse deterministically and the count is capped;
* a source that cannot answer is reported, never rendered as "nothing running";
* a stale snapshot withholds everything instead of showing old inventory;
* the JSON shape is stable and carries no credential-shaped key.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from backend.registry import discovery

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def source(name="launch", kind="launch_receipt", descriptors=()):
    return discovery.StaticDescriptorSource(
        name=name, kind=kind, descriptors=tuple(descriptors)
    )


def descriptor(name="engine-a", url="http://127.0.0.1:8000"):
    return {"name": name, "target_url": url}


class CandidateShapeTest(unittest.TestCase):
    def test_a_loopback_descriptor_becomes_an_unverified_candidate(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        self.assertEqual(len(snapshot.candidates), 1)
        candidate = snapshot.candidates[0]
        self.assertEqual(candidate.status, discovery.STATUS_DETECTED_UNVERIFIED)
        self.assertEqual((candidate.host, candidate.port, candidate.scheme),
                         ("127.0.0.1", 8000, "http"))
        self.assertEqual(candidate.source_kind, "launch_receipt")
        self.assertIsNone(candidate.owner_binding)
        self.assertFalse(candidate.registered)
        self.assertFalse(candidate.shareable)
        self.assertFalse(candidate.invokable)

    def test_candidate_id_is_stable_across_runs(self):
        first = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        second = discovery.discover(
            [source(descriptors=[descriptor()])], now=NOW + timedelta(seconds=5)
        )
        self.assertEqual(first.candidates[0].candidate_id, second.candidates[0].candidate_id)

    def test_candidates_are_immutable(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        with self.assertRaises(Exception):
            snapshot.candidates[0].status = "registered"  # type: ignore[misc]
        with self.assertRaises(Exception):
            snapshot.candidates = ()  # type: ignore[misc]


class TargetValidationTest(unittest.TestCase):
    def test_remote_targets_are_not_described(self):
        for url in ("http://10.0.0.5:8000", "http://example.com:8000",
                    "http://192.168.1.10:9000", "https://8.8.8.8"):
            with self.subTest(url=url):
                snapshot = discovery.discover(
                    [source(descriptors=[descriptor(url=url)])], now=NOW
                )
                self.assertEqual(snapshot.candidates, ())

    def test_credentials_paths_queries_and_bad_ports_are_refused(self):
        for url in (
            "http://user:pass@127.0.0.1:8000",
            "http://127.0.0.1:8000/admin",
            "http://127.0.0.1:8000/?token=x",
            "ftp://127.0.0.1:8000",
            "http://127.0.0.1:0",
            "not a url",
        ):
            with self.subTest(url=url):
                snapshot = discovery.discover(
                    [source(descriptors=[descriptor(url=url)])], now=NOW
                )
                self.assertEqual(snapshot.candidates, ())

    def test_ipv6_loopback_is_canonicalised(self):
        snapshot = discovery.discover(
            [source(descriptors=[descriptor(url="http://[::1]:8000")])], now=NOW
        )
        self.assertEqual(snapshot.candidates[0].host, "::1")

    def test_a_nameless_descriptor_is_ignored(self):
        snapshot = discovery.discover(
            [source(descriptors=[{"target_url": "http://127.0.0.1:8000"}])], now=NOW
        )
        self.assertEqual(snapshot.candidates, ())


class DedupeAndCapTest(unittest.TestCase):
    def test_duplicate_targets_collapse_to_the_first_source(self):
        snapshot = discovery.discover(
            [
                source(name="receipt", descriptors=[descriptor(name="from-receipt")]),
                source(name="descriptor", descriptors=[descriptor(name="from-descriptor")]),
            ],
            now=NOW,
        )
        self.assertEqual(len(snapshot.candidates), 1)
        self.assertEqual(snapshot.candidates[0].source, "receipt")
        self.assertEqual(snapshot.candidates[0].display_name, "from-receipt")

    def test_candidate_count_is_capped(self):
        descriptors = [
            descriptor(name=f"engine-{index}", url=f"http://127.0.0.1:{9000 + index}")
            for index in range(10)
        ]
        snapshot = discovery.discover(
            [source(descriptors=descriptors)], now=NOW, max_candidates=4
        )
        self.assertEqual(len(snapshot.candidates), 4)
        self.assertTrue(snapshot.truncated)
        self.assertEqual([c.port for c in snapshot.candidates], [9000, 9001, 9002, 9003])


class SourceFailureTest(unittest.TestCase):
    def test_an_unavailable_source_is_reported_not_silently_empty(self):
        snapshot = discovery.discover(
            [
                discovery.UnavailableSource(name="host-proc", reason="no permission"),
                source(descriptors=[descriptor()]),
            ],
            now=NOW,
        )
        self.assertIn("host-proc", snapshot.unavailable_sources)
        # Only the source that answered produces an endpoint.
        self.assertEqual(len(snapshot.candidates), 1)
        self.assertEqual(snapshot.candidates[0].status, discovery.STATUS_DETECTED_UNVERIFIED)
        payload = snapshot.for_display(NOW)
        self.assertEqual(payload["counts"]["unavailable_sources"], 1)
        self.assertEqual(payload["counts"]["total"], 1)

    def test_a_raising_source_does_not_break_the_snapshot(self):
        class Exploding:
            name = "broken"
            kind = "descriptor"

            def read(self, now):
                raise RuntimeError("boom")

        snapshot = discovery.discover([Exploding()], now=NOW)
        self.assertEqual(snapshot.unavailable_sources, ("broken",))
        # No placeholder endpoint is invented for a source that could not answer.
        self.assertEqual(snapshot.candidates, ())
        payload = snapshot.for_display(NOW)
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["unavailable_sources"], ["broken"])

    def test_the_exception_message_is_never_surfaced(self):
        class Leaky:
            name = "leaky"
            kind = "descriptor"

            def read(self, now):
                raise RuntimeError("secret=/etc/shadow token=abc123")

        snapshot = discovery.discover([Leaky()], now=NOW)
        rendered = json.dumps(snapshot.for_display(NOW)).lower()
        self.assertNotIn("secret", rendered)
        self.assertNotIn("abc123", rendered)
        self.assertNotIn("shadow", rendered)


class FreshnessTest(unittest.TestCase):
    def test_a_fresh_snapshot_renders_candidates(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        payload = snapshot.for_display(NOW + timedelta(seconds=5))
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["counts"]["total"], 1)
        self.assertEqual(len(payload["candidates"]), 1)

    def test_a_stale_snapshot_withholds_every_candidate(self):
        snapshot = discovery.discover(
            [source(descriptors=[descriptor()])], now=NOW, ttl_seconds=10
        )
        payload = snapshot.for_display(NOW + timedelta(seconds=11))
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["reason"], "snapshot_stale")
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["counts"]["total"], 0)

    def test_a_future_stamped_snapshot_is_withheld_as_clock_skew(self):
        # generated_at comes from the caller's clock. If that clock ran ahead,
        # the snapshot must not stay displayable until real time catches up.
        future = NOW + timedelta(hours=2)
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=future)
        payload = snapshot.for_display(NOW)
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["reason"], "clock_skew")
        self.assertEqual(payload["candidates"], [])

    def test_small_clock_jitter_is_tolerated(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        payload = snapshot.for_display(NOW - timedelta(seconds=1))
        self.assertFalse(payload["stale"])
        self.assertEqual(len(payload["candidates"]), 1)

    def test_naive_time_and_bad_limits_are_refused(self):
        with self.assertRaises(discovery.DiscoveryError):
            discovery.discover([], now=datetime(2026, 10, 20, 15, 0))
        with self.assertRaises(discovery.DiscoveryError):
            discovery.discover([], now=NOW, ttl_seconds=0)
        with self.assertRaises(discovery.DiscoveryError):
            discovery.discover([], now=NOW, max_candidates=0)
        snapshot = discovery.discover([], now=NOW)
        with self.assertRaises(discovery.DiscoveryError):
            snapshot.for_display(datetime(2026, 10, 20, 15, 0))
        with self.assertRaises(discovery.DiscoveryError):
            snapshot.is_stale(datetime(2026, 10, 20, 15, 0))


class OwnershipBoundaryTest(unittest.TestCase):
    """Discovery must have no way to attribute a candidate to anyone.

    A previous draft exposed ``bind_candidate(candidate, evidence_kind=...,
    evidence_id=...)``: three caller-supplied strings, checked only for
    membership and non-emptiness. Any caller could tag another user's engine as
    its own. The API was removed rather than tightened, because a string tag
    cannot be made into proof by validating its spelling.
    """

    def test_there_is_no_owner_binding_api_to_forge(self):
        for name in ("bind_candidate", "bind_owner", "claim_owner", "set_owner"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(discovery, name), name)
        self.assertNotIn("bind_candidate", discovery.__all__)

    def test_every_candidate_is_unowned_and_rightless(self):
        snapshot = discovery.discover(
            [
                source(descriptors=[descriptor()]),
                discovery.UnavailableSource(name="host-proc", reason="no permission"),
            ],
            now=NOW,
        )
        self.assertTrue(snapshot.candidates)
        for candidate in snapshot.candidates:
            with self.subTest(candidate=candidate.candidate_id):
                self.assertIsNone(candidate.owner_binding)
                self.assertFalse(candidate.registered)
                self.assertFalse(candidate.shareable)
                self.assertFalse(candidate.invokable)

    def test_json_never_invents_an_owner(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        payload = snapshot.for_display(NOW)
        for item in payload["candidates"]:
            self.assertIsNone(item["owner_binding"])
            self.assertFalse(item["registered"] or item["shareable"] or item["invokable"])


class JsonShapeTest(unittest.TestCase):
    def test_json_is_stable_and_carries_no_credential_keys(self):
        snapshot = discovery.discover(
            [
                discovery.UnavailableSource(name="host-proc", reason="no permission"),
                source(descriptors=[descriptor()]),
            ],
            now=NOW,
        )
        payload = snapshot.for_display(NOW)
        encoded = json.dumps(payload, sort_keys=True)
        self.assertEqual(
            sorted(payload),
            ["candidates", "counts", "expires_at", "generated_at", "reason",
             "stale", "truncated", "unavailable_sources"],
        )
        for forbidden in ("token", "secret", "password", "authorization", "cookie",
                          "uid", "owner_uid", "env", "cmdline"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, encoded.lower())


if __name__ == "__main__":
    unittest.main()
