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
        self.assertEqual(snapshot.candidates[0].status, discovery.STATUS_SOURCE_UNAVAILABLE)
        self.assertEqual(snapshot.candidates[1].status, discovery.STATUS_DETECTED_UNVERIFIED)

    def test_a_raising_source_does_not_break_the_snapshot(self):
        class Exploding:
            name = "broken"
            kind = "descriptor"

            def read(self, now):
                raise RuntimeError("boom")

        snapshot = discovery.discover([Exploding()], now=NOW)
        self.assertEqual(snapshot.unavailable_sources, ("broken",))
        self.assertEqual(snapshot.candidates[0].status, discovery.STATUS_SOURCE_UNAVAILABLE)


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

    def test_naive_time_and_bad_limits_are_refused(self):
        with self.assertRaises(discovery.DiscoveryError):
            discovery.discover([], now=datetime(2026, 10, 20, 15, 0))
        with self.assertRaises(discovery.DiscoveryError):
            discovery.discover([], now=NOW, ttl_seconds=0)
        with self.assertRaises(discovery.DiscoveryError):
            discovery.discover([], now=NOW, max_candidates=0)


class BindingTest(unittest.TestCase):
    def test_binding_requires_evidence_and_never_grants_rights(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        candidate = snapshot.candidates[0]
        bound = discovery.bind_candidate(
            candidate,
            platform_principal="platform:alice",
            evidence_kind="launch_receipt",
            evidence_id="receipt-42",
        )
        self.assertEqual(bound.owner_binding, "platform:alice")
        self.assertFalse(bound.registered)
        self.assertFalse(bound.shareable)
        self.assertFalse(bound.invokable)
        self.assertEqual(bound.candidate_id, candidate.candidate_id)

    def test_binding_rejects_unreviewed_evidence_kinds(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        candidate = snapshot.candidates[0]
        for kind in ("same_username", "matching_password", "guessed_process", ""):
            with self.subTest(kind=kind):
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.bind_candidate(
                        candidate,
                        platform_principal="platform:alice",
                        evidence_kind=kind,
                        evidence_id="x",
                    )

    def test_binding_needs_a_principal_and_an_evidence_id(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        candidate = snapshot.candidates[0]
        with self.assertRaises(discovery.DiscoveryError):
            discovery.bind_candidate(
                candidate, platform_principal="", evidence_kind="operator_policy",
                evidence_id="x",
            )
        with self.assertRaises(discovery.DiscoveryError):
            discovery.bind_candidate(
                candidate, platform_principal="platform:alice",
                evidence_kind="operator_policy", evidence_id="",
            )

    def test_discovery_alone_never_binds(self):
        snapshot = discovery.discover([source(descriptors=[descriptor()])], now=NOW)
        for candidate in snapshot.candidates:
            self.assertIsNone(candidate.owner_binding)


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
