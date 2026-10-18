"""P0 隐私边界回归：授权范围外的**原始诊断计数**不得出现在普通 viewer 的 DTO 里。

背景（Issue #3 第二轮审查，基线 commit ``73f2d15``）：``build_atlas_dto`` 先对整个输入做
``ingest_jsonl``，再把全局的 ``malformed_lines`` / ``content_field_rejected`` 无条件写进
``unknown`` / ``quality`` / ``display_hints``；遍历事件时又在**授权项目判断之前**累加
``out_of_window`` / ``unknown_key`` / ``revoked_key``。于是一个只持有单项目
``atlas:usage:read`` 的 viewer，只要前后两次调用喂进不同的日志，就能从 DTO 数字变化
反推出其他租户的活动量与源文件损坏情况。

**本文件的验收口径不是「某个数字等于 X」，而是：**

> 对同一个 viewer，两批输入只要**可见样本相同**，整个 DTO 必须**逐字节相同** ——
> 无论第二批额外塞进多少其他项目行 / 未知 key / 损坏 JSON / 带正文行 / 窗口外请求。

以及 ``null``（无从测得）与 ``0``（确实测得零）必须分开：
- 归属不明的计数（全局诊断）→ ``None``；
- 能归属到可见项目的计数（真的数过）→ 整数，可以是 ``0``。
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
    atlas_request,
    load_fixture,
    requester,
    trusted_directory,
)

from backend.atlas import (  # noqa: E402
    SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
    SCOPE_USAGE_READ,
    SCOPE_USAGE_UNATTRIBUTED,
    ContractError,
    build_atlas_dto,
    validate_dto,
)

#: 目录里 key-alpha / key-beta / key-orphan / key-revoked 属于 proj-a，key-gamma 属于 proj-b。
OWN_PROJECT = "proj-a"
FOREIGN_PROJECT = "proj-b"

#: 归属不明（结构上无法定位到任何项目）的诊断计数器 —— 普通 viewer 一律 ``None``。
GLOBAL_DIAGNOSTIC_COUNTERS = ("unknown_key", "malformed_lines", "content_field_rejected")

WINDOW = ("2026-10-14T00:00:00Z", "2026-10-14T04:00:00Z")


def _row(ts: str, key_id: str, *, total_ms: int, stream: bool = True, ttft_ms: int | None = 100) -> str:
    return json.dumps(
        {
            "ts": ts,
            "key_id": key_id,
            "status": 200,
            "outcome": "success",
            "stream": stream,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "total_ms": total_ms,
            "ttft_ms": ttft_ms if stream else None,
        },
        sort_keys=True,
    )


#: 第一批：**只含可见项目（proj-a）且完全干净**的样本，窗口内。
BASE_ROWS = (
    _row("2026-10-14T00:05:00Z", "key-alpha", total_ms=1000),
    _row("2026-10-14T01:05:00Z", "key-alpha", total_ms=1200),
    _row("2026-10-14T02:05:00Z", "key-beta", total_ms=800, stream=False, ttft_ms=None),
)

#: 第二批额外注入的**不可归属**内容：其他项目、未知 key、损坏 JSON、带正文行、窗口外请求。
#: 注意：窗口外的两行故意用「其他项目 / 未知 key」——它们本来就不该被数。
#: 可见项目自己的窗口外行另有单独用例（那一条是**可归属**的，数它是正确行为）。
FOREIGN_AND_CORRUPT_ROWS = (
    # 其他项目，窗口内
    _row("2026-10-14T00:10:00Z", "key-gamma", total_ms=9999),
    # 未知 key，窗口内
    _row("2026-10-14T00:11:00Z", "unknown-key-zzz", total_ms=8888),
    # 损坏 JSON
    '{"ts":"2026-10-14T00:12:00Z","key_id":"key-gamma","broken":',
    # 带正文类字段（正文拒绝计数在结构上无法归属到项目）
    json.dumps(
        {
            "ts": "2026-10-14T00:13:00Z",
            "key_id": "key-gamma",
            "status": 200,
            "outcome": "success",
            "stream": True,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_ms": 10,
            "prompt": "<SYNTHETIC-CANARY: must never surface>",
        },
        sort_keys=True,
    ),
    # 窗口外 + 其他项目
    _row("2026-10-14T09:30:00Z", "key-gamma", total_ms=7777),
    # 窗口外 + 未知 key
    _row("2026-10-14T09:31:00Z", "unknown-key-zzz", total_ms=6666),
)


def _text(rows) -> str:
    return "\n".join(rows) + "\n"


class _Builder:
    """小工具：同一个 viewer，喂不同批次，便于做逐字节对比。"""

    def __init__(self) -> None:
        self.directory = trusted_directory()

    def build(self, rows, ctx=None, **kwargs):
        kwargs.setdefault("window_from", WINDOW[0])
        kwargs.setdefault("window_to", WINDOW[1])
        request = atlas_request(ctx or requester(projects=(OWN_PROJECT,)), **kwargs)
        return build_atlas_dto(request, _text(rows), self.directory, SYNTHETIC)


class DtoInvarianceTest(unittest.TestCase):
    """P0 的核心验收：额外的不可归属数据不得改变普通 viewer 的 DTO。"""

    def setUp(self) -> None:
        self.builder = _Builder()
        self.clean = self.builder.build(BASE_ROWS)
        # 第二批 = 同一批可见样本 + 一堆不可归属内容（顺序打散，避免靠位置蒙对）
        shuffled = (FOREIGN_AND_CORRUPT_ROWS[0], FOREIGN_AND_CORRUPT_ROWS[3],
                    FOREIGN_AND_CORRUPT_ROWS[1], FOREIGN_AND_CORRUPT_ROWS[5],
                    FOREIGN_AND_CORRUPT_ROWS[2], FOREIGN_AND_CORRUPT_ROWS[4])
        self.polluted = self.builder.build(BASE_ROWS + shuffled)

    def test_extra_foreign_and_corrupt_rows_do_not_change_the_dto_bytes(self) -> None:
        """整份 DTO 逐字节相同 —— notes / hints / quality / unknown 一个都不能变。"""
        self.assertEqual(
            json.dumps(self.clean, sort_keys=True),
            json.dumps(self.polluted, sort_keys=True),
        )

    def test_display_hints_are_unchanged_by_foreign_corruption(self) -> None:
        self.assertEqual(self.clean["display_hints"], self.polluted["display_hints"])

    def test_notes_are_unchanged_by_foreign_corruption(self) -> None:
        self.assertEqual(self.clean["notes"], self.polluted["notes"])

    def test_quality_block_is_unchanged_by_foreign_corruption(self) -> None:
        self.assertEqual(self.clean["quality"], self.polluted["quality"])

    def test_visible_sample_is_actually_measured_so_the_test_has_teeth(self) -> None:
        """若可见样本本身为空/不可测，上面那些『相同』会退化成恒真。"""
        self.assertEqual(self.clean["totals"]["calls"], 3)
        self.assertEqual(self.clean["totals"]["success"], 3)
        self.assertEqual(self.clean["latency"]["samples"], 3)
        self.assertEqual(self.clean["groups"][0]["id"], OWN_PROJECT)

    def test_no_foreign_project_or_canary_string_survives_anywhere(self) -> None:
        blob = json.dumps(self.polluted, sort_keys=True)
        for token in (FOREIGN_PROJECT, "key-gamma", "unknown-key-zzz", "SYNTHETIC-CANARY"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)


class GlobalDiagnosticsAreNullNotZeroTest(unittest.TestCase):
    """``null``（无从测得）与 ``0``（确实测得零）必须分开，且不得伪造 0。"""

    def setUp(self) -> None:
        self.builder = _Builder()

    def test_ordinary_viewer_gets_null_for_every_unattributable_counter(self) -> None:
        dto = self.builder.build(BASE_ROWS + FOREIGN_AND_CORRUPT_ROWS)
        for name in GLOBAL_DIAGNOSTIC_COUNTERS:
            with self.subTest(counter=name):
                self.assertIsNone(dto["unknown"][name], dto["unknown"])
                self.assertIsNot(dto["unknown"][name], 0)

    def test_ordinary_viewer_gets_null_not_zero_in_the_quality_block(self) -> None:
        dto = self.builder.build(BASE_ROWS + FOREIGN_AND_CORRUPT_ROWS)
        self.assertIsNone(dto["quality"]["malformed_lines"])
        self.assertIsNone(dto["quality"]["content_field_rejected"])
        self.assertIsNot(dto["quality"]["malformed_lines"], 0)
        self.assertIsNot(dto["quality"]["content_field_rejected"], 0)

    def test_null_is_reported_even_when_the_visible_sample_is_pristine(self) -> None:
        """干净的可见样本也不得把全局计数伪造成 0 —— 无从测得就是无从测得。"""
        dto = self.builder.build(BASE_ROWS)
        for name in GLOBAL_DIAGNOSTIC_COUNTERS:
            with self.subTest(counter=name):
                self.assertIsNone(dto["unknown"][name], dto["unknown"])
        self.assertIsNone(dto["quality"]["malformed_lines"])
        self.assertIsNone(dto["quality"]["content_field_rejected"])

    def test_scoped_counters_are_measured_zero_not_null(self) -> None:
        """能归属到可见项目的计数器是真数过的：0 就是测得零，不是 None。"""
        dto = self.builder.build(BASE_ROWS)
        for name in ("out_of_window", "revoked_key", "filtered_out",
                     "outcome_status_mismatch", "latency_missing", "ttft_missing"):
            with self.subTest(counter=name):
                self.assertIsInstance(dto["unknown"][name], int)
                self.assertNotIsInstance(dto["unknown"][name], bool)
                self.assertEqual(dto["unknown"][name], 0)

    def test_quality_calls_never_claims_measured_when_integrity_is_unverifiable(self) -> None:
        """全局坏行被隐藏后，再报 ``measured`` 就等于断言「源文件没有坏行」——即伪造 0。"""
        dto = self.builder.build(BASE_ROWS)
        self.assertEqual(dto["quality"]["calls"], "unknown")
        with_corruption = self.builder.build(BASE_ROWS + FOREIGN_AND_CORRUPT_ROWS)
        self.assertEqual(with_corruption["quality"]["calls"], "unknown")

    def test_quality_calls_does_not_track_global_corruption(self) -> None:
        """同一 viewer：加不加坏行，``quality.calls`` 必须一样（否则是侧信道）。"""
        self.assertEqual(
            self.builder.build(BASE_ROWS)["quality"]["calls"],
            self.builder.build(BASE_ROWS + FOREIGN_AND_CORRUPT_ROWS)["quality"]["calls"],
        )

    def test_hidden_corruption_leaves_no_trace_in_hints(self) -> None:
        dto = self.builder.build(BASE_ROWS + FOREIGN_AND_CORRUPT_ROWS)
        blob = " ".join(dto["display_hints"])
        for token in ("损坏", "malformed", "正文", "prompt/completion"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)


class ScopedDiagnosticsStillWorkTest(unittest.TestCase):
    """收紧边界不等于把有用的、**可归属**的统计也一起砍掉。"""

    def setUp(self) -> None:
        self.builder = _Builder()

    def test_out_of_window_rows_of_the_visible_project_are_counted(self) -> None:
        rows = (_row("2026-10-14T00:05:00Z", "key-alpha", total_ms=1000),
                _row("2026-10-14T09:05:00Z", "key-alpha", total_ms=1000))
        dto = self.builder.build(rows)
        self.assertEqual(dto["unknown"]["out_of_window"], 1)
        self.assertEqual(dto["totals"]["calls"], 1)

    def test_out_of_window_rows_of_foreign_or_unknown_keys_are_not_counted(self) -> None:
        rows = (_row("2026-10-14T00:05:00Z", "key-alpha", total_ms=1000),
                _row("2026-10-14T09:05:00Z", "key-gamma", total_ms=1000),
                _row("2026-10-14T09:06:00Z", "unknown-key-zzz", total_ms=1000))
        dto = self.builder.build(rows)
        self.assertEqual(dto["unknown"]["out_of_window"], 0)
        self.assertEqual(dto["unknown"]["unknown_key"], None)

    def test_revoked_key_of_the_visible_project_is_counted(self) -> None:
        rows = (_row("2026-10-14T00:05:00Z", "key-alpha", total_ms=1000),
                _row("2026-10-14T00:06:00Z", "key-revoked", total_ms=1000))
        dto = self.builder.build(rows)
        self.assertEqual(dto["unknown"]["revoked_key"], 1)
        self.assertEqual(dto["totals"]["calls"], 1)

    def test_model_filter_counter_stays_scoped(self) -> None:
        rows = (_row("2026-10-14T00:05:00Z", "key-alpha", total_ms=1000),)
        dto = self.builder.build(rows, model_filter="llm-does-not-exist")
        self.assertEqual(dto["unknown"]["filtered_out"], 1)
        self.assertEqual(dto["totals"]["calls"], 0)


class ScopeGatingTest(unittest.TestCase):
    """全局诊断只认**显式 scope**；role=admin 不是通行证。"""

    def setUp(self) -> None:
        self.builder = _Builder()
        self.rows = BASE_ROWS + FOREIGN_AND_CORRUPT_ROWS

    def test_admin_role_without_the_scope_still_sees_null(self) -> None:
        ctx = requester(roles=("admin",), scopes=(SCOPE_USAGE_READ,), projects=(OWN_PROJECT,))
        dto = self.builder.build(self.rows, ctx)
        for name in GLOBAL_DIAGNOSTIC_COUNTERS:
            with self.subTest(counter=name):
                self.assertIsNone(dto["unknown"][name], dto["unknown"])
        self.assertIsNone(dto["quality"]["malformed_lines"])
        self.assertFalse(dto["scope"]["global_diagnostics_visible"])

    def test_unattributed_scope_alone_does_not_unlock_source_diagnostics(self) -> None:
        """`atlas:usage:unattributed` 只授权「未归属**请求**」，不授权「源文件损坏」。"""
        ctx = requester(
            scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_UNATTRIBUTED), projects=(OWN_PROJECT,)
        )
        dto = self.builder.build(self.rows, ctx)
        # 两行用的是未注册 key（一行窗口内、一行窗口外），归属判定先于窗口判定，
        # 所以两个都落进 unknown_key —— 这正是「不可归属」与「窗口外」不混为一谈。
        self.assertEqual(dto["unknown"]["unknown_key"], 2)
        self.assertIsNone(dto["unknown"]["malformed_lines"])
        self.assertIsNone(dto["unknown"]["content_field_rejected"])
        self.assertIsNone(dto["quality"]["malformed_lines"])
        self.assertEqual(dto["quality"]["calls"], "unknown")
        self.assertFalse(dto["scope"]["global_diagnostics_visible"])

    def test_global_diagnostics_scope_alone_does_not_unlock_unattributed_request_counts(self) -> None:
        ctx = requester(
            scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_GLOBAL_DIAGNOSTICS), projects=(OWN_PROJECT,)
        )
        dto = self.builder.build(self.rows, ctx)
        self.assertIsNone(dto["unknown"]["unknown_key"])
        self.assertIsNone(dto["unattributed"]["project_calls"])
        self.assertFalse(dto["unattributed"]["visible"])

    def test_global_diagnostics_scope_reveals_source_diagnostics(self) -> None:
        ctx = requester(
            scopes=(SCOPE_USAGE_READ, SCOPE_USAGE_GLOBAL_DIAGNOSTICS), projects=(OWN_PROJECT,)
        )
        dto = self.builder.build(self.rows, ctx)
        self.assertTrue(dto["scope"]["global_diagnostics_visible"])
        self.assertEqual(dto["unknown"]["malformed_lines"], 1)
        self.assertEqual(dto["unknown"]["content_field_rejected"], 1)
        self.assertEqual(dto["quality"]["malformed_lines"], 1)
        self.assertEqual(dto["quality"]["content_field_rejected"], 1)
        self.assertEqual(dto["quality"]["calls"], "partial")
        self.assertTrue(any("损坏" in h for h in dto["display_hints"]))

    def test_scope_block_declares_every_visibility_flag(self) -> None:
        dto = self.builder.build(self.rows)
        self.assertIn("global_diagnostics_visible", dto["scope"])
        self.assertIn("unattributed_visible", dto["scope"])
        self.assertFalse(dto["scope"]["global_diagnostics_visible"])

    def test_multi_project_viewer_is_still_denied_foreign_raw_diagnostics(self) -> None:
        """拿到两个项目的汇总授权，也不等于拿到全局日志。"""
        ctx = requester(projects=(OWN_PROJECT, FOREIGN_PROJECT))
        dto = self.builder.build(self.rows, ctx, requested_project=None)
        self.assertEqual(dto["scope"]["projects"], [OWN_PROJECT, FOREIGN_PROJECT])
        self.assertTrue(dto["scope"]["cross_project"])
        for name in GLOBAL_DIAGNOSTIC_COUNTERS:
            with self.subTest(counter=name):
                self.assertIsNone(dto["unknown"][name], dto["unknown"])

    def test_real_fixture_no_longer_leaks_to_the_single_project_viewer(self) -> None:
        """基线 fixture 自带坏行 / 未知 key / 已吊销 key —— 普通 viewer 不得看见计数。"""
        dto = build_atlas_dto(
            atlas_request(requester()),
            load_fixture("gateway_sample.jsonl"),
            trusted_directory(),
            SYNTHETIC,
        )
        self.assertIsNone(dto["unknown"]["unknown_key"])
        self.assertIsNone(dto["unknown"]["malformed_lines"])
        self.assertIsNone(dto["unknown"]["content_field_rejected"])
        self.assertIsNone(dto["quality"]["malformed_lines"])
        self.assertEqual(dto["unknown"]["revoked_key"], 1)
        self.assertEqual(dto["unknown"]["out_of_window"], 1)


class AggregationSemanticsBoundaryTest(unittest.TestCase):
    """P1 待办：本文档记下**当前**的汇总语义，不改授权、不扩大也不缩小可见范围。"""

    def setUp(self) -> None:
        self.builder = _Builder()

    def test_view_is_an_authorised_project_aggregate_not_a_personal_view(self) -> None:
        """当前视图 = 「被授权项目汇总」：同项目、其他 principal 的 key 也会被一起计量。"""
        dto = self.builder.build(BASE_ROWS)  # key-alpha -> u-1001, key-beta -> u-4001
        self.assertEqual(dto["scope"]["subject"], "u-1001")
        self.assertEqual(dto["totals"]["calls"], 3)
        group = dto["groups"][0]
        workload_ids = {w["id"] for w in group["workloads"]}
        self.assertEqual(workload_ids, {"resume-parse", "code-assist"})

    def test_notes_state_the_aggregation_semantics_explicitly(self) -> None:
        notes = " ".join(self.builder.build(BASE_ROWS)["notes"])
        self.assertIn("项目汇总", notes)

    def test_personal_view_is_not_implemented_by_principal_matching(self) -> None:
        """没有『我的用量』这种口径 —— 不得靠 principal 悄悄过滤充数。"""
        mine_only = self.builder.build(BASE_ROWS[0:1] + BASE_ROWS[2:3])
        everything = self.builder.build(BASE_ROWS)
        self.assertEqual(mine_only["totals"]["calls"], 2)
        self.assertEqual(everything["totals"]["calls"], 3)


class RequestsFieldSemanticsTest(unittest.TestCase):
    """P2：``requests`` 的历史命名有歧义，字段保留但口径必须钉死并写进文档。"""

    def setUp(self) -> None:
        self.builder = _Builder()

    def test_requests_is_the_success_count_not_the_request_total(self) -> None:
        rows = BASE_ROWS + (
            json.dumps(
                {
                    "ts": "2026-10-14T03:05:00Z",
                    "key_id": "key-alpha",
                    "status": 500,
                    "outcome": "failed",
                    "stream": False,
                    "usage": None,
                    "total_ms": None,
                },
                sort_keys=True,
            ),
        )
        totals = self.builder.build(rows)["totals"]
        self.assertEqual(totals["calls"], 4)
        self.assertEqual(totals["success"], 3)
        self.assertEqual(totals["requests"], totals["success"])
        self.assertNotEqual(totals["requests"], totals["calls"])

    def test_contract_documents_the_requests_field(self) -> None:
        """字段名保留，但契约必须把「requests == success，不是总量」写清楚。"""
        import pathlib

        doc = pathlib.Path(__file__).resolve().parents[2] / "docs" / "atlas-data-contract.md"
        text = doc.read_text(encoding="utf-8")
        self.assertIn("### 5.2", text)
        self.assertIn("**等于同一行的 `success`**", text)
        self.assertIn("**不是**请求总量", text)

    def test_contract_documents_the_new_scope_and_its_separation(self) -> None:
        import pathlib

        doc = pathlib.Path(__file__).resolve().parents[2] / "docs" / "atlas-data-contract.md"
        text = doc.read_text(encoding="utf-8")
        self.assertIn(SCOPE_USAGE_GLOBAL_DIAGNOSTICS, text)
        self.assertIn("**不得**为了「顺手」而调整", text)
        self.assertIn("为什么是两个 scope 而不是一个", text)


class ValidateDtoEnforcesBoundariesTest(unittest.TestCase):
    """门禁不是靠自觉：``validate_dto`` 必须**拒绝**手工构造出来的越界 DTO。

    这些用例直接对「构建完再改坏」的 DTO 断言，覆盖的是每次 ``build_atlas_dto``
    都会跑的那道自检 —— 也就是防止将来有人把边界改回去的最后一层。
    """

    def _dto(self, *scopes):
        ctx = requester(scopes=(SCOPE_USAGE_READ,) + scopes, projects=(OWN_PROJECT,))
        return _Builder().build(BASE_ROWS, ctx)

    def test_authorised_dto_passes(self) -> None:
        for scopes in (
            (),
            (SCOPE_USAGE_UNATTRIBUTED,),
            (SCOPE_USAGE_GLOBAL_DIAGNOSTICS,),
            (SCOPE_USAGE_UNATTRIBUTED, SCOPE_USAGE_GLOBAL_DIAGNOSTICS),
        ):
            with self.subTest(scopes=scopes):
                validate_dto(self._dto(*scopes))

    def test_rejects_numeric_unknown_key_without_the_scope(self) -> None:
        for planted in (0, 5):
            with self.subTest(planted=planted):
                dto = self._dto()
                dto["unknown"]["unknown_key"] = planted
                with self.assertRaises(ContractError):
                    validate_dto(dto)

    def test_rejects_numeric_source_diagnostics_without_the_scope(self) -> None:
        for block in ("unknown", "quality"):
            for counter in ("malformed_lines", "content_field_rejected"):
                with self.subTest(block=block, counter=counter):
                    dto = self._dto()
                    dto[block][counter] = 0
                    with self.assertRaises(ContractError):
                        validate_dto(dto)

    def test_rejects_measured_calls_quality_without_the_scope(self) -> None:
        for planted in ("measured", "partial"):
            with self.subTest(planted=planted):
                dto = self._dto()
                dto["quality"]["calls"] = planted
                with self.assertRaises(ContractError):
                    validate_dto(dto)

    def test_rejects_null_scoped_counter(self) -> None:
        for counter in ("out_of_window", "revoked_key", "filtered_out",
                        "outcome_status_mismatch", "latency_missing", "ttft_missing"):
            with self.subTest(counter=counter):
                dto = self._dto()
                dto["unknown"][counter] = None
                with self.assertRaises(ContractError):
                    validate_dto(dto)

    def test_rejects_quality_that_does_not_mirror_unknown(self) -> None:
        dto = self._dto(SCOPE_USAGE_GLOBAL_DIAGNOSTICS)
        dto["unknown"]["malformed_lines"] = 7
        with self.assertRaises(ContractError):
            validate_dto(dto)

    def test_rejects_unattributed_visible_that_contradicts_the_scope(self) -> None:
        dto = self._dto()
        dto["unattributed"]["visible"] = True
        with self.assertRaises(ContractError):
            validate_dto(dto)

    def test_rejects_null_project_calls_while_claiming_visibility(self) -> None:
        dto = self._dto(SCOPE_USAGE_UNATTRIBUTED)
        dto["unattributed"]["project_calls"] = None
        with self.assertRaises(ContractError):
            validate_dto(dto)


if __name__ == "__main__":
    unittest.main()
