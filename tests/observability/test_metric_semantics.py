"""口径分离：整机/整卡读数与按人用量**不得互相冒充**。

本文件把任务书里那句「共享推理实例 GPU 不能虚构分用户显存，按 caller 已验证 key 计量」
落成可执行的断言：

* 按人计量只走 Gateway 已验证 key（``billing_basis == "verified-key"``），
  且其指标全是计数 / token / 毫秒；
* 整机 / 整卡读数只出现在 ``host`` 块里，设备块**没有** ``uid`` 字段；
* 两块之间不共享任何 ``metric_id`` —— 于是「把整卡利用率当成用户账单」在结构上
  无法被表达，而不仅仅是「没人这么写」。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from observability_support import (  # noqa: E402
    ALICE_ROWS,
    FOREIGN_ROWS,
    NOW,
    SYNTHETIC,
    UID_ALICE,
    admin_principal,
    principal,
    registry,
    request,
    text_of,
    trusted_directory,
)

from backend.observability import (  # noqa: E402
    CATALOGUE,
    MockHostCollector,
    PHYSICAL_SCOPES,
    SCOPE_DEVICE,
    SCOPE_NODE,
    SCOPE_PRINCIPAL,
    QUALITY_UNKNOWN,
    ContractError,
    HostReport,
    HOST_SOURCE,
    MetricReading,
    MockHostCollector,
    ProcHostCollector,
    validate_admin_overview_dto,
    UnknownMetric,
    build_admin_overview_dto,
    build_self_usage_dto,
    build_timeline_dto,
    validate_usage_dto,
)

BILLING_BASIS = "verified-key"


def _admin_with_host(rows=ALICE_ROWS + FOREIGN_ROWS, **kwargs):
    return build_admin_overview_dto(
        request(admin_principal(), **kwargs),
        text_of(rows),
        trusted_directory(),
        registry(),
        SYNTHETIC,
        now=NOW,
        host_report=MockHostCollector().collect(NOW),
    )


class BillingBasisTest(unittest.TestCase):
    def test_usage_views_declare_the_billing_basis(self) -> None:
        own = build_self_usage_dto(
            request(principal(UID_ALICE)),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertEqual(own["billing_basis"], BILLING_BASIS)

        timeline = build_timeline_dto(
            request(principal(UID_ALICE)),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertEqual(timeline["billing_basis"], BILLING_BASIS)

    def test_admin_usage_view_also_declares_it(self) -> None:
        self.assertEqual(_admin_with_host()["billing_basis"], BILLING_BASIS)

    def test_the_basis_is_pinned_by_the_validator(self) -> None:
        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        dto["billing_basis"] = "device-share"
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)


class PrincipalMetricsAreNotDeviceMetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.admin = _admin_with_host()

    def test_the_two_blocks_share_no_metric_id(self) -> None:
        billing = set(self.admin["metrics"])
        physical: set[str] = set()
        for section in ("node", "driver"):
            physical |= set(self.admin["host"][section])
        for device in self.admin["host"]["devices"]:
            physical |= set(device["readings"])
        self.assertEqual(billing & physical, set(), billing & physical)

    def test_every_billing_metric_is_principal_scoped(self) -> None:
        for metric_id, reading in self.admin["metrics"].items():
            with self.subTest(metric=metric_id):
                self.assertEqual(reading["scope"], SCOPE_PRINCIPAL)
                self.assertFalse(reading["physical"])

    def test_every_host_metric_is_node_or_device_scoped(self) -> None:
        for section in ("node", "driver"):
            for metric_id, reading in self.admin["host"][section].items():
                with self.subTest(metric=metric_id):
                    self.assertEqual(reading["scope"], SCOPE_NODE)
                    self.assertTrue(reading["physical"])
        for device in self.admin["host"]["devices"]:
            for metric_id, reading in device["readings"].items():
                with self.subTest(metric=metric_id):
                    self.assertEqual(reading["scope"], SCOPE_DEVICE)
                    self.assertTrue(reading["physical"])

    def test_the_device_block_has_no_uid_and_no_per_user_fields(self) -> None:
        for device in self.admin["host"]["devices"]:
            with self.subTest(index=device["index"]):
                self.assertNotIn("uid", device)
                self.assertNotIn("principal", device)
                blob = json.dumps(device, sort_keys=True)
                for token in ("uid", "principal", "per_user", "per-user"):
                    self.assertNotIn(token, blob)

    def test_the_device_reading_set_is_exactly_memory_utilisation_temperature(self) -> None:
        device = self.admin["host"]["devices"][0]
        self.assertEqual(
            set(device["readings"]),
            {
                "device.gpu.memory.total_bytes",
                "device.gpu.memory.used_bytes",
                "device.gpu.utilization_percent",
                "device.gpu.temperature_celsius",
            },
        )

    def test_total_card_memory_is_labelled_as_shared_in_the_catalogue(self) -> None:
        description = CATALOGUE["device.gpu.memory.used_bytes"].description
        self.assertIn("共享", description)
        self.assertIn("非按人", description)

    def test_the_admin_hints_say_the_two_views_are_not_interchangeable(self) -> None:
        hints = " ".join(self.admin["display_hints"])
        self.assertIn("不得互相换算", hints)

    def test_host_and_billing_quality_are_tracked_separately(self) -> None:
        quality = self.admin["quality"]
        self.assertIn("host", quality)
        self.assertIn("calls", quality)

    def test_an_admin_without_a_host_report_gets_null_host_not_empty_numbers(self) -> None:
        dto = build_admin_overview_dto(
            request(admin_principal()),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertIsNone(dto["host"])
        self.assertEqual(dto["quality"]["host"], "unknown")
        self.assertTrue(any("不可得" in hint for hint in dto["display_hints"]))

    def test_a_present_but_empty_host_report_is_unknown_not_measured(self) -> None:
        """本任务采集器未启用时的常态：``host`` 对象在，但里面**一条都没测到**。

        按「有对象就是 measured」会把它画成一屏可信读数 —— 正是「UNKNOWN 被当成 0」
        的展示层版本，也是本模块最不该犯的错。
        """
        dto = build_admin_overview_dto(
            request(admin_principal()),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
            host_report=ProcHostCollector(enabled=False).collect(NOW),
        )
        self.assertIsNotNone(dto["host"])
        # 面板里每条都是 unknown……
        for metric_id, reading in dto["host"]["node"].items():
            with self.subTest(metric=metric_id):
                self.assertEqual(reading["quality"], QUALITY_UNKNOWN)
        # ……所以整体质量也必须是 unknown。
        self.assertEqual(dto["quality"]["host"], QUALITY_UNKNOWN)
        self.assertTrue(
            any("全部未测得" in hint for hint in dto["display_hints"]),
            dto["display_hints"],
        )

    def test_partially_measured_host_readings_are_partial(self) -> None:
        report = MockHostCollector().collect(NOW)
        partial = HostReport(
            source=HOST_SOURCE,
            synthetic=True,
            node_readings=report.node_readings + (
                MetricReading.unknown(
                    "node.disk.used_bytes", source=HOST_SOURCE, reason="source_unavailable",
                    now=NOW,
                ),
            ),
            driver_readings=report.driver_readings,
            devices=report.devices,
            notes=(),
        )
        dto = build_admin_overview_dto(
            request(admin_principal()),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
            host_report=partial,
        )
        self.assertEqual(dto["quality"]["host"], "partial")

    def test_the_validator_refuses_a_measured_host_over_unknown_readings(self) -> None:
        """门禁钉住这条：手工把 quality.host 改成 measured 必须被拒。"""
        dto = build_admin_overview_dto(
            request(admin_principal()),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
            host_report=ProcHostCollector(enabled=False).collect(NOW),
        )
        dto["quality"]["host"] = "measured"
        with self.assertRaises(ContractError):
            validate_admin_overview_dto(dto)


class NoPerUserGpuMemoryTest(unittest.TestCase):
    """「某用户在这张卡上占了多少」在本模块**无法表达**。"""

    def test_the_catalogue_has_no_per_principal_byte_metric(self) -> None:
        offenders = [
            metric_id
            for metric_id, spec in CATALOGUE.items()
            if spec.scope == SCOPE_PRINCIPAL and spec.unit in ("bytes", "celsius", "percent")
        ]
        self.assertEqual(offenders, [])

    def test_no_metric_id_pairs_a_principal_with_a_gpu_resource(self) -> None:
        for metric_id in CATALOGUE:
            with self.subTest(metric=metric_id):
                if metric_id.startswith("principal."):
                    self.assertNotIn("gpu", metric_id)
                    self.assertNotIn("memory", metric_id)

    def test_constructing_a_user_scoped_gpu_memory_reading_is_impossible(self) -> None:
        for metric_id in (
            "principal.gpu.memory_bytes",
            "principal.device.gpu.memory.used_bytes",
            "principal.gpu.utilization_percent",
        ):
            with self.subTest(metric=metric_id):
                with self.assertRaises(UnknownMetric):
                    MetricReading.from_metric(
                        metric_id, value=1, source="x", observed_at=NOW, now=NOW
                    )

    def test_physical_scopes_are_exactly_node_and_device(self) -> None:
        self.assertEqual(PHYSICAL_SCOPES, frozenset({SCOPE_NODE, SCOPE_DEVICE}))

    def test_the_usage_dto_carries_no_device_readings_at_all(self) -> None:
        """本人用量视图里连 ``host`` 块都没有 —— 那是管理视角的东西。"""
        own = build_self_usage_dto(
            request(principal(UID_ALICE)),
            text_of(ALICE_ROWS),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertNotIn("host", own)
        blob = json.dumps(own, sort_keys=True)
        for token in ("device.gpu", "node.gpu", "node.memory", "node.disk"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)


class PrometheusPhysicalSourceTest(unittest.TestCase):
    """Prometheus 只登记物理 / 服务类固定表达式 —— 里面没有一条按人的。"""

    def test_no_fixed_expression_targets_a_principal_metric(self) -> None:
        from backend.observability import FIXED_EXPRESSIONS

        for metric_id in FIXED_EXPRESSIONS:
            with self.subTest(metric=metric_id):
                self.assertNotEqual(CATALOGUE[metric_id].scope, SCOPE_PRINCIPAL)

    def test_every_fixed_expression_targets_a_physical_or_service_metric(self) -> None:
        from backend.observability import FIXED_EXPRESSIONS

        for metric_id in FIXED_EXPRESSIONS:
            with self.subTest(metric=metric_id):
                self.assertIn(CATALOGUE[metric_id].scope, (SCOPE_NODE, SCOPE_DEVICE, "service"))


if __name__ == "__main__":
    unittest.main()
