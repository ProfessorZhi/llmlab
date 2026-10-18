"""``backend/observability/trust.py`` 的回归 —— 受信来源白名单不许漂移。

这份白名单在这之前有**三份手抄副本**（portal 模块视图、BFF 观测 adapter、前端
``contract.js``），而**数据产地**一份都没有。三份拷贝的意义就在于它们迟早会不一样；
本文件把「它们必须一样」变成一条会红的断言，并把「产地自己也要能回答谁可信」
变成可执行的契约。
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.observability import trust  # noqa: E402
from backend.observability.dto import Provenance  # noqa: E402
from backend.observability.sources import MockMetricProvider, PrometheusProvider  # noqa: E402
from backend.bff import observability_adapter as bff_adapter  # noqa: E402
from backend.portal import modules as portal_modules  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND_CONTRACT = REPO_ROOT / "web" / "js" / "features" / "contract.js"


class ClassificationTest(unittest.TestCase):
    def test_the_three_whitelisted_names_are_verified(self) -> None:
        for name in (
            trust.SOURCE_GATEWAY_USAGE_EXPORT,
            trust.SOURCE_NVML_VERIFIED,
            trust.SOURCE_PROMETHEUS_READONLY,
        ):
            with self.subTest(name=name):
                self.assertEqual(trust.classify_source(name), trust.CLASS_VERIFIED)
                self.assertTrue(trust.is_verified_source(name))

    def test_an_unknown_name_is_unverified_by_default(self) -> None:
        for name in ("gateway", "prometheus", "inflated-numbers", "prod"):
            with self.subTest(name=name):
                self.assertEqual(trust.classify_source(name), trust.CLASS_UNVERIFIED)
                self.assertFalse(trust.is_verified_source(name))

    def test_prometheus_is_not_prometheus_readonly(self) -> None:
        # 这条是「名字对不上 → 接对了却不显示」那个真实缺陷的护栏。
        self.assertNotEqual(
            trust.SOURCE_PROMETHEUS_READONLY, "prometheus"
        )
        self.assertFalse(trust.is_verified_source("prometheus"))

    def test_synthetic_names_are_classified_as_synthetic_not_unverified(self) -> None:
        # 合成数据是**诚实**的：它有独立分类，用于把「假数据」与「来路不明」分开。
        for name in ("mock", "synthetic-fixture", "demo"):
            with self.subTest(name=name):
                self.assertEqual(trust.classify_source(name), trust.CLASS_SYNTHETIC)
                self.assertFalse(trust.is_verified_source(name))

    def test_non_strings_and_blanks_are_unverified(self) -> None:
        for value in (None, 42, "", "   ", b"mock"):
            with self.subTest(value=value):
                self.assertEqual(trust.classify_source(value), trust.CLASS_UNVERIFIED)

    def test_surrounding_whitespace_does_not_change_the_class(self) -> None:
        self.assertEqual(
            trust.classify_source("  prometheus-readonly  "), trust.CLASS_VERIFIED
        )

    def test_every_class_is_in_the_declared_vocabulary(self) -> None:
        for value in (
            trust.SOURCE_PROMETHEUS_READONLY,
            trust.SOURCE_MOCK,
            "whatever",
            None,
        ):
            with self.subTest(value=value):
                self.assertIn(trust.classify_source(value), trust.SOURCE_CLASSES)


class MayPresentAsLiveTest(unittest.TestCase):
    def test_a_verified_non_synthetic_source_may_be_live(self) -> None:
        self.assertTrue(
            trust.may_present_as_live(
                trust.SOURCE_PROMETHEUS_READONLY, synthetic=False
            )
        )

    def test_a_verified_source_declared_synthetic_may_not_be_live(self) -> None:
        # 来源可信 ≠ 这批数据可信。两者都要成立。
        self.assertFalse(
            trust.may_present_as_live(
                trust.SOURCE_PROMETHEUS_READONLY, synthetic=True
            )
        )

    def test_an_unverified_source_may_never_be_live(self) -> None:
        self.assertFalse(trust.may_present_as_live("gateway", synthetic=False))

    def test_only_an_explicit_false_counts_as_non_synthetic(self) -> None:
        # None / 0 / "" 都不构成「我声明这是真的」。
        for value in (None, 0, "", "false", 1, True):
            with self.subTest(synthetic=value):
                self.assertFalse(
                    trust.may_present_as_live(
                        trust.SOURCE_PROMETHEUS_READONLY, synthetic=value
                    )
                )


class ProvenanceBridgeTest(unittest.TestCase):
    def test_provenance_exposes_its_source_class(self) -> None:
        verified = Provenance(source=trust.SOURCE_NVML_VERIFIED, synthetic=False)
        self.assertEqual(verified.source_class, trust.CLASS_VERIFIED)
        self.assertTrue(verified.live_eligible)

    def test_a_synthetic_provenance_is_never_live_eligible(self) -> None:
        synthetic = Provenance(source=trust.SOURCE_MOCK, synthetic=True)
        self.assertEqual(synthetic.source_class, trust.CLASS_SYNTHETIC)
        self.assertFalse(synthetic.live_eligible)

    def test_an_unverified_provenance_is_never_live_eligible(self) -> None:
        odd = Provenance(source="gateway", synthetic=False)
        self.assertEqual(odd.source_class, trust.CLASS_UNVERIFIED)
        self.assertFalse(odd.live_eligible)

    def test_provenance_still_accepts_unverified_names(self) -> None:
        # 构造不许变严：合成/未知来源必须仍可被构造出来（否则无法如实描述它们）。
        Provenance(source="something-else", synthetic=False)


class ProviderSourceParityTest(unittest.TestCase):
    """产地的 provider 必须自称一个**白名单里存在的**名字。"""

    def test_the_prometheus_provider_uses_a_verified_source_name(self) -> None:
        self.assertEqual(PrometheusProvider.source, trust.SOURCE_PROMETHEUS_READONLY)
        self.assertTrue(trust.is_verified_source(PrometheusProvider.source))
        self.assertFalse(PrometheusProvider.synthetic)

    def test_the_mock_provider_self_reports_synthetic_and_is_not_verified(self) -> None:
        self.assertEqual(MockMetricProvider.source, trust.SOURCE_MOCK)
        self.assertTrue(MockMetricProvider.synthetic)
        self.assertFalse(trust.is_verified_source(MockMetricProvider.source))


class WhitelistParityTest(unittest.TestCase):
    """三份副本 + 前端必须逐字一致。"""

    def test_the_portal_module_whitelist_matches(self) -> None:
        self.assertEqual(
            frozenset(portal_modules.VERIFIED_METRIC_SOURCES), trust.VERIFIED_SOURCES
        )

    def test_the_bff_adapter_whitelist_matches(self) -> None:
        self.assertEqual(
            frozenset(bff_adapter.VERIFIED_METRIC_SOURCES), trust.VERIFIED_SOURCES
        )

    def test_the_frontend_whitelist_matches(self) -> None:
        text = FRONTEND_CONTRACT.read_text(encoding="utf-8")
        start = text.index("export const VERIFIED_METRIC_SOURCES")
        end = text.index("]);", start)
        block = text[start:end]
        found = set(re.findall(r"'([a-z0-9-]+)'", block))
        self.assertEqual(found, set(trust.VERIFIED_SOURCES))

    def test_the_frontend_still_gates_live_on_non_synthetic(self) -> None:
        text = FRONTEND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("synthetic", text)


if __name__ == "__main__":
    unittest.main()
