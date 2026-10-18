"""来源能力清单：四个状态、五个原因，以及「什么才算 ready」的那条线。

这一组**不需要 harness**：``backend.bff.capabilities`` 是纯函数，输入是「已装配
事实」+ 这个会话的 scope + 一个当下。这正是它能在没有 Dell、没有 PAM、没有
Gateway 的机器上被逐条验收的原因 —— 判定与装配是分开的两件事，测试也分开验。

断言的重点不是「四种状态都出现过」，而是**它们之间的分界在哪里**：

* ``not_configured`` 与 ``auth_required`` 不能混 —— 一个说部署没接，一个说
  这条会话没权限。混起来会让运维去查一个根本不存在的接线问题；
* ``stale`` 与 ``ready`` 的分界是**声明的 TTL**，而过期只按 ``sample_at`` 判；
* **没权限的会话不该知道数据新不新** —— 判定顺序本身是一条安全边界。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.bff import capabilities as C

NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)

#: 声明了 TTL 的那条 seam（主机投影，90 秒）。
HOST_SEAM = C.SEAMS_BY_SOURCE["host_summary"]
#: 没声明 TTL 的那条（实时查询型）。
REGISTRY_SEAM = C.SEAMS_BY_SOURCE["registry"]


def _cap(seam, evidence, scopes=frozenset(), now=NOW):
    return C.judge(seam, evidence, granted_scopes=frozenset(scopes), now=now)


class VocabularyTests(unittest.TestCase):
    def test_the_state_vocabulary_is_exactly_the_four_frontend_branches(self):
        """前端只认这四个。新增一个取值必须同时改这条断言 —— 那正是要被逼着想的。"""
        self.assertEqual(
            C.STATES, ("ready", "stale", "auth_required", "not_configured")
        )

    def test_every_reason_is_reachable_by_construction(self):
        self.assertEqual(len(C.REASONS), len(set(C.REASONS)))
        for reason in C.REASONS:
            with self.subTest(reason=reason):
                self.assertTrue(reason)

    def test_every_seam_declares_closed_values(self):
        seen = set()
        for seam in C.SOURCE_SEAMS:
            with self.subTest(source=seam.source):
                self.assertNotIn(seam.source, seen)
                seen.add(seam.source)
                self.assertIn(seam.attachment, C.ATTACHMENTS)
                self.assertTrue(seam.owner)
                self.assertTrue(seam.route.startswith("/"))
                self.assertNotIn(":8080", seam.route)
                if seam.ttl_seconds is not None:
                    self.assertGreater(seam.ttl_seconds, 0)

    def test_the_sources_are_the_stable_names_other_lanes_hand_over(self):
        self.assertEqual(
            C.SOURCES,
            ("registry", "observability", "host_summary", "atlas_calls", "evaluation"),
        )

    def test_only_the_sampling_source_declares_a_ttl(self):
        """声明了阈值的必须真的是**采样型**来源，且那个数从上游常量来。

        给一条实时查询型来源编一根 TTL，等于替它宣称一个它没说过的新鲜度口径；
        而 ``host_summary`` 的 90 秒是 ``host_projection`` 自己写的那个数 ——
        这里断言它逐字相等，好让改上游的人在这里被拦住。
        """
        from backend.observability.host_projection import DEFAULT_MAX_SAMPLE_AGE_S

        with_ttl = {seam.source: seam.ttl_seconds for seam in C.SOURCE_SEAMS if seam.ttl_seconds}
        self.assertEqual(with_ttl, {"host_summary": DEFAULT_MAX_SAMPLE_AGE_S})

    def test_the_future_tolerance_is_the_host_projection_one(self):
        from backend.observability.host_projection import DEFAULT_FUTURE_TOLERANCE_S

        self.assertEqual(C.DEFAULT_FUTURE_TOLERANCE_S, DEFAULT_FUTURE_TOLERANCE_S)


class JudgeTests(unittest.TestCase):
    def test_no_adapter_is_not_configured_and_says_so(self):
        verdict = _cap(HOST_SEAM, C.SourceEvidence(attached=False))
        self.assertEqual(verdict.state, C.STATE_NOT_CONFIGURED)
        self.assertEqual(verdict.reason, C.REASON_ADAPTER_NOT_ATTACHED)
        self.assertFalse(verdict.attached)

    def test_attached_but_unscoped_is_auth_required_not_stale(self):
        """没权限的会话拿到的是 ``auth_required``，**不是**「数据过期了」。

        后者会泄露一条只有有权限的人才该知道的事实，还会把运维引到一个错误的
        方向上去。判定顺序里这一条排在新鲜度之前，就是为了这件事。
        """
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(attached=True, sample_at=NOW - timedelta(hours=9)),
            scopes=(),
        )
        self.assertEqual(verdict.state, C.STATE_AUTH_REQUIRED)
        self.assertEqual(verdict.reason, C.REASON_MISSING_SCOPE)
        self.assertIsNone(verdict.sample_at, "an unauthorised session learns no sample time")
        self.assertIsNone(verdict.age_seconds)

    def test_attached_scoped_and_never_sampled_is_stale_not_configured(self):
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(attached=True),
            scopes=(HOST_SEAM.scope,),
        )
        self.assertEqual(verdict.state, C.STATE_STALE)
        self.assertEqual(verdict.reason, C.REASON_NO_SAMPLE_YET)
        self.assertIsNone(verdict.sample_at)

    def test_a_fresh_sample_is_ready(self):
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(attached=True, sample_at=NOW - timedelta(seconds=20)),
            scopes=(HOST_SEAM.scope,),
        )
        self.assertEqual(verdict.state, C.STATE_READY)
        self.assertIsNone(verdict.reason)
        self.assertAlmostEqual(verdict.age_seconds, 20.0, places=3)

    def test_a_sample_older_than_the_declared_ttl_is_stale(self):
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(
                attached=True, sample_at=NOW - timedelta(seconds=HOST_SEAM.ttl_seconds + 1)
            ),
            scopes=(HOST_SEAM.scope,),
        )
        self.assertEqual(verdict.state, C.STATE_STALE)
        self.assertEqual(verdict.reason, C.REASON_SAMPLE_AGED_OUT)
        self.assertIsNotNone(verdict.sample_at, "we did sample; we just did it too long ago")

    def test_exactly_at_the_ttl_is_still_ready(self):
        """边界算在**里面**：``age > ttl`` 才过期，``age == ttl`` 还在窗口内。"""
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(
                attached=True, sample_at=NOW - timedelta(seconds=HOST_SEAM.ttl_seconds)
            ),
            scopes=(HOST_SEAM.scope,),
        )
        self.assertEqual(verdict.state, C.STATE_READY)

    def test_a_future_sample_is_clock_skew_not_freshness(self):
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(attached=True, sample_at=NOW + timedelta(seconds=600)),
            scopes=(HOST_SEAM.scope,),
        )
        self.assertEqual(verdict.state, C.STATE_STALE)
        self.assertEqual(verdict.reason, C.REASON_SAMPLE_IN_FUTURE)

    def test_a_live_query_source_is_ready_however_old_its_read_looks(self):
        """``ttl_seconds=None`` 的 seam 是实时查询：没有 TTL 可以判过期。

        这条同时钉住**另一半**：不能因为「没有 TTL 就永远 ready」就顺手给所有
        seam 都填一个 TTL —— 那些数只在上游真的写过时才存在。
        """
        verdict = _cap(
            REGISTRY_SEAM,
            C.SourceEvidence(attached=True, sample_at=NOW - timedelta(days=30)),
            scopes=(),
        )
        self.assertEqual(verdict.state, C.STATE_READY)
        self.assertIsNone(verdict.ttl_seconds)

    def test_an_evidence_supplied_ttl_overrides_the_declared_one(self):
        """来源自己报的阈值优先：它比 seam 更知道自己的口径。"""
        verdict = _cap(
            HOST_SEAM,
            C.SourceEvidence(
                attached=True,
                sample_at=NOW - timedelta(seconds=30),
                ttl_seconds=10,
            ),
            scopes=(HOST_SEAM.scope,),
        )
        self.assertEqual(verdict.state, C.STATE_STALE)
        self.assertEqual(verdict.ttl_seconds, 10)

    def test_a_naive_now_is_refused(self):
        with self.assertRaises(ValueError):
            _cap(HOST_SEAM, C.SourceEvidence(attached=True), now=datetime(2026, 10, 17))


class EvidenceInvariantTests(unittest.TestCase):
    def test_a_ready_verdict_may_not_be_constructed_by_hand_without_attachment(self):
        with self.assertRaises(ValueError):
            C.SourceCapability(
                source="x", owner="y", route="/z", scope=None,
                state=C.STATE_READY, reason=None, attached=False,
                sample_at=None, observed_at=None, age_seconds=None, ttl_seconds=None,
            )

    def test_a_non_ready_verdict_must_carry_a_reason(self):
        with self.assertRaises(ValueError):
            C.SourceCapability(
                source="x", owner="y", route="/z", scope=None,
                state=C.STATE_STALE, reason=None, attached=True,
                sample_at=None, observed_at=None, age_seconds=None, ttl_seconds=None,
            )

    def test_adapter_not_attached_may_not_claim_attachment(self):
        with self.assertRaises(ValueError):
            C.SourceCapability(
                source="x", owner="y", route="/z", scope=None,
                state=C.STATE_NOT_CONFIGURED, reason=C.REASON_ADAPTER_NOT_ATTACHED,
                attached=True, sample_at=None, observed_at=None,
                age_seconds=None, ttl_seconds=None,
            )

    def test_source_evidence_refuses_a_naive_timestamp(self):
        with self.assertRaises(ValueError):
            C.SourceEvidence(attached=True, sample_at=datetime(2026, 10, 17))


class EvidencePlumbingTests(unittest.TestCase):
    class _Adapter:
        attached = True

    class _Detached:
        attached = False

    class _Sampling:
        attached = True

        def __init__(self, fact):
            self._fact = fact

        def source_fact(self):
            return self._fact

    def test_a_missing_adapter_is_not_configured(self):
        evidence = C.evidence_from(None, now=NOW)
        self.assertFalse(evidence.attached)

    def test_a_detached_adapter_is_not_configured_even_though_it_exists(self):
        self.assertFalse(C.evidence_from(self._Detached(), now=NOW).attached)

    def test_a_live_query_adapter_has_no_source_fact_and_uses_now(self):
        """实时查询型来源**没有** ``source_fact()``：这一次读到了就是证据。

        给它编一个「采样时间」,正是这份清单要避免的那种假证据。
        """
        evidence = C.evidence_from(self._Adapter(), now=NOW)
        self.assertTrue(evidence.attached)
        self.assertEqual(evidence.sample_at, NOW)

    def test_a_sampling_adapter_reports_its_own_fact(self):
        observed = NOW - timedelta(seconds=5)
        adapter = self._Sampling({"sample_at": NOW - timedelta(seconds=5), "observed_at": observed})
        evidence = C.evidence_from(adapter, now=NOW)
        self.assertEqual(evidence.sample_at, NOW - timedelta(seconds=5))
        self.assertEqual(evidence.observed_at, observed)

    def test_a_sampling_adapter_that_never_read_anything_says_so(self):
        adapter = self._Sampling({"sample_at": None, "observed_at": None, "ttl_seconds": None})
        evidence = C.evidence_from(adapter, now=NOW)
        self.assertTrue(evidence.attached)
        self.assertIsNone(evidence.sample_at)

    def test_a_required_dependency_counts_as_attached_when_present(self):
        seam = REGISTRY_SEAM
        self.assertEqual(seam.attachment, C.ATTACHMENT_REQUIRED)
        self.assertTrue(C.evidence_for(seam, object(), now=NOW).attached)
        # 而它**缺了**仍然如实报没装配 —— 一个构造期本该拦住它的人今天没拦住，
        # 不该被这里悄悄补上。
        self.assertFalse(C.evidence_for(seam, None, now=NOW).attached)


class BuildTests(unittest.TestCase):
    def test_a_source_with_no_evidence_is_reported_as_not_configured(self):
        """少给一条来源的证据，结局是它说自己 ``not_configured``，而不是它消失。"""
        payload = C.build_capabilities(
            subject="linux:host:4001", granted_scopes=frozenset(), evidence={}, now=NOW
        )
        self.assertEqual(
            [item["source"] for item in payload["sources"]], list(C.SOURCES)
        )
        self.assertTrue(all(item["state"] == C.STATE_NOT_CONFIGURED for item in payload["sources"]))

    def test_the_summary_counts_match_the_source_rows(self):
        payload = C.build_capabilities(
            subject="linux:host:4001",
            granted_scopes=frozenset(seam.scope for seam in C.SOURCE_SEAMS if seam.scope),
            evidence={seam.source: C.SourceEvidence(attached=True, sample_at=NOW)
                      for seam in C.SOURCE_SEAMS},
            now=NOW,
        )
        self.assertEqual(sum(payload["summary"].values()), len(payload["sources"]))
        self.assertEqual(
            sum(payload["summary"].values()),
            sum(1 for item in payload["sources"] if item["state"] in C.STATES),
        )

    def test_the_payload_carries_no_identity_the_session_did_not_have(self):
        """清单里只有服务端已知的东西：主体、它自己的 scope、装配事实。

        没有 header 里的任何取值、没有别人、没有凭据 —— 这份 payload 将来会被
        画到页面上，塞进去什么就会被看到什么。
        """
        payload = C.build_capabilities(
            subject="linux:host:4001",
            granted_scopes=frozenset({"atlas:calls:self:read"}),
            evidence={},
            now=NOW,
        )
        self.assertEqual(payload["scope"], {
            "subject": "linux:host:4001",
            "scopes": ["atlas:calls:self:read"],
        })
        self.assertEqual(payload["schema_version"], C.SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
