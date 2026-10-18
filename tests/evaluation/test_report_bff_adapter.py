"""BFF 适配器的报告接缝：主体桥接、scope 不放宽、来源不被二次改写。

``backend/bff/evaluation_adapter.py`` 属于本 Lane 的文件家族。这一组证明三条：

* 适配器只用**受信主体**里那几个字段（角色/principal/uid/linux_user/roles），接进来的
  评测 scope 由 ``backend.bff.scopes`` 决定——因此"BFF 里的成员"与"评测模块里的成员"
  可以是不同的 scope 集合，而适配器不会替它们抹平；
* ``source``（EvalScope vs legacy-benchmark）由模块盖章后原样透传，本层不重映射；
* 报告面的 ``submit``/准入结论不受影响（报告是只读的，不放开任何派发）。

身份用一个**鸭子类型**的小对象：这正是适配器宣称的最小契约。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.auth.store import ROLE_ADMIN, ROLE_MEMBER
from backend.bff.evaluation_adapter import EvaluationStack, ModuleEvaluationAdapter
from backend.evaluation.models import metric_known
from backend.evaluation.provenance import DIMENSION_QUALITY, SOURCE_EVALSCOPE, SOURCE_OK

from tests.evaluation.support import (
    EPOCH,
    REPORT_REF_EVALSCOPE,
    REPORT_REF_LEGACY,
    build_harness_with_reports,
    close_all,
    report_owner,
    report_record,
    report_snapshot,
)


def identity(principal: str, role: str) -> SimpleNamespace:
    """适配器读取的最小主体形状：这正是它声明的鸭子类型契约。"""
    from tests.evaluation import HOST_USERS

    uid = dict(HOST_USERS)[principal]
    return SimpleNamespace(
        principal=principal,
        uid=uid,
        linux_user=principal,
        effective_role=role,
        requester=SimpleNamespace(roles=frozenset({role})),
    )


def adapter_for(harness) -> ModuleEvaluationAdapter:
    return ModuleEvaluationAdapter(EvaluationStack(service=harness.service))


class ReportAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = build_harness_with_reports(
            (
                report_snapshot(
                    SOURCE_EVALSCOPE,
                    SOURCE_OK,
                    [
                        report_record(
                            SOURCE_EVALSCOPE,
                            REPORT_REF_EVALSCOPE,
                            report_id="r-alice",
                            owner=report_owner("alice"),
                            dimensions={
                                DIMENSION_QUALITY: metric_known(DIMENSION_QUALITY, 0.9)
                            },
                        )
                    ],
                    observed_at=EPOCH,
                ),
            )
        )
        self.adapter = adapter_for(self.harness)

    def tearDown(self) -> None:
        close_all()

    def test_a_member_lists_reports_but_cannot_read_the_reference(self) -> None:
        data = self.adapter.reports(identity=identity("alice", ROLE_MEMBER))
        self.assertEqual(len(data["reports"]), 1)
        row = data["reports"][0]
        # BFF 的成员 scope 里**没有** evaluation:report:read，因此引用读取未授权——
        # 适配器没有把这条要求抹掉。
        self.assertFalse(row["preview"]["authorized"])
        self.assertFalse(row["preview"]["content_read"])

    def test_the_source_is_passed_through_unchanged(self) -> None:
        data = self.adapter.reports(identity=identity("alice", ROLE_MEMBER))
        self.assertEqual(data["reports"][0]["source"], SOURCE_EVALSCOPE)

    def test_report_sources_exposes_health(self) -> None:
        data = self.adapter.report_sources(identity=identity("alice", ROLE_MEMBER))
        self.assertEqual(data["sources"][0]["source"], SOURCE_EVALSCOPE)

    def test_the_adapter_uses_the_module_submit_gate(self) -> None:
        # 报告接缝不改变派发结论：submit 仍然恒为不可用。
        decision = self.adapter.submit(identity=identity("alice", ROLE_MEMBER), is_admin=False)
        self.assertFalse(decision["ready"])

    def test_an_unowned_report_stays_hidden_even_from_a_bff_admin_with_the_flag(self) -> None:
        """BFF 的角色只发 ``admin:read``，从不发 ``admin:manage``。

        因此 ``include_unowned`` 这条安全阀在 BFF 面**打不开**：归属无法核实的历史记录
        永远不通过"角色是管理员"这条路显形。要让它可见，正确做法是走 Issue #34 的
        **逐 run 审核过的迁移映射**（``LegacyBenchmarkReader(audited_owners={run_id: owner})``），
        把 run 变成有归属。
        """
        harness = build_harness_with_reports(
            (
                report_snapshot(
                    SOURCE_EVALSCOPE,
                    SOURCE_OK,
                    [
                        report_record(
                            SOURCE_EVALSCOPE, REPORT_REF_LEGACY, report_id="r-orphan", owner=None
                        )
                    ],
                    observed_at=EPOCH,
                ),
            )
        )
        self.addCleanup(close_all)
        adapter = adapter_for(harness)
        data = adapter.reports(identity=identity("carol", ROLE_ADMIN))
        self.assertEqual(data["reports"], [])
        with_flag = adapter.reports(
            identity=identity("carol", ROLE_ADMIN), include_unowned=True
        )
        self.assertEqual(with_flag["reports"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
