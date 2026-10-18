"""「我的调用」视图的金丝雀：证明上面那组负测**真的会失败**。

与 ``test_canaries.py`` 同一套思路，但针对逐条视图特有的三种错误形态：

* 把**客户端自报**的 ``x_principal`` / ``X-Forwarded-User`` 当成归属依据；
* 沿用**项目汇总**的归属规则（只按项目）来喂逐条明细；
* 让缺失的 Token / 延迟以 ``0`` 的面目出现。

每一条金丝雀都断言「这个错误实现确实会把不该出现的东西放出来」。如果哪天它们不再
违反不变量，说明对应的负测已经变成恒真 —— 那时**不得**声称这一层是安全的。
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import (  # noqa: E402
    SYNTHETIC,
    calls_directory,
    calls_request,
    load_fixture,
    self_requester,
)

from backend.atlas import (  # noqa: E402
    CALLS_SCHEMA_VERSION,
    ContractError,
    build_self_calls_dto,
    ingest_jsonl,
    validate_self_calls_dto,
)

FIXTURE = "gateway_calls_sample.jsonl"


# --------------------------------------------------------------------------
# 故意写坏的实现（只存在于测试里，绝不进 backend/）
# --------------------------------------------------------------------------


def canary_owner_from_client_fields(text: str, subject: str) -> list[str]:
    """错误做法一：相信请求行里自报的 ``x_principal`` / ``X-Forwarded-User``。"""
    keys: list[str] = []
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        claimed = row.get("x_principal") or row.get("X-Forwarded-User")
        if claimed == subject:
            keys.append(str(row.get("key_id")))
    return keys


def canary_attribute_by_project_only(text: str, directory, projects) -> list[str]:
    """错误做法二：沿用项目汇总的归属规则 —— 项目对就算，不问 principal。

    这正是把两条口径混在一起的后果：同一项目里同事的 key 会一起出现在
    「**我的**调用日志」里。
    """
    keys: list[str] = []
    for event in ingest_jsonl(text).events:
        binding = directory.lookup(event.key_id)
        if binding is None:
            continue
        if binding.project in projects and not binding.revoked:
            keys.append(event.key_id)
    return keys


def canary_tokens_with_zero_fallback(events) -> list[int]:
    """错误做法三：缺失的 Token 用 0 顶替 —— 把「没测到」说成「测到了 0」。"""
    return [
        (event.input_tokens or 0) + (event.output_tokens or 0) for event in events
    ]


class SelfCallsCanariesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = load_fixture(FIXTURE)
        self.directory = calls_directory()
        self.dto = build_self_calls_dto(
            calls_request(self_requester()), self.text, self.directory, SYNTHETIC
        )

    # ---- 金丝雀一：自报身份 ----------------------------------------------

    def test_canary_client_declared_ownership_does_leak(self) -> None:
        leaked = canary_owner_from_client_fields(self.text, "u-1001")
        # fixture 里那条伪造了 x_principal=u-1001 的未注册 key 会被它放进来。
        self.assertIn("unknown-key-zzz", leaked)

    def test_the_real_view_does_not_leak_it(self) -> None:
        blob = json.dumps(self.dto, ensure_ascii=False)
        self.assertNotIn("unknown-key-zzz", blob)
        self.assertNotIn("x_principal", blob)
        self.assertNotIn("X-Forwarded-User", blob)

    # ---- 金丝雀二：只按项目归属 ------------------------------------------

    def test_canary_project_only_attribution_does_leak_a_colleague(self) -> None:
        leaked = canary_attribute_by_project_only(self.text, self.directory, {"proj-a"})
        # 汇总口径下 u-4001 的 key-beta 会被算进同一个项目的视图。
        self.assertIn("key-beta", leaked)

    def test_the_real_view_excludes_the_colleague(self) -> None:
        self.assertNotIn("key-beta", [row["key_id"] for row in self.dto["calls"]])

    def test_the_two_rules_differ_by_exactly_the_colleague_rows(self) -> None:
        """两条口径的差集必须是「同一个项目里的他人 key」，不能是别的什么。"""
        aggregate_style = set(canary_attribute_by_project_only(self.text, self.directory, {"proj-a"}))
        own_style = {row["key_id"] for row in self.dto["calls"]}
        self.assertEqual(aggregate_style - own_style, {"key-beta"})

    # ---- 金丝雀三：用 0 顶替缺失值 ---------------------------------------

    def test_canary_zero_fallback_does_produce_fake_zeros(self) -> None:
        events = [e for e in ingest_jsonl(self.text).events if e.outcome == "failed"]
        self.assertTrue(events)
        totals = canary_tokens_with_zero_fallback(events)
        self.assertTrue(all(total == 0 for total in totals))

    def test_the_real_view_reports_null_instead(self) -> None:
        for row in self.dto["calls"]:
            if row["outcome"] == "failed":
                self.assertIsNone(row["tokens"]["total"])

    # ---- 金丝雀四：自检必须挡得住被篡改的 DTO ----------------------------

    def test_canary_a_content_bearing_dto_is_refused(self) -> None:
        """一条「顺手把 prompt 带出去」的实现——自检必须拒绝它。"""
        tampered = json.loads(json.dumps(self.dto))
        tampered["content"]["included"] = True
        tampered["calls"][0]["prompt"] = "<SYNTHETIC-CANARY>"
        with self.assertRaises(ContractError):
            validate_self_calls_dto(tampered)

    def test_canary_a_live_claim_on_synthetic_data_is_refused(self) -> None:
        tampered = json.loads(json.dumps(self.dto))
        tampered["transport"]["mode"] = "live"
        with self.assertRaises(ContractError):
            validate_self_calls_dto(tampered)

    def test_the_baseline_dto_is_untouched_by_these_canaries(self) -> None:
        self.assertEqual(self.dto["schema_version"], CALLS_SCHEMA_VERSION)
        validate_self_calls_dto(self.dto)


if __name__ == "__main__":
    unittest.main()
