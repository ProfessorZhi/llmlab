"""「我的调用」逐条视图的授权、口径与不变量回归。

这个套件证明的核心只有三句话：

1. **归属来自服务端目录**：只有 ``principal == 会话 subject`` 的 key 会出现在明细里；
   同一个项目里别人的 key、没见过的 key、自己已吊销的 key 一律不出现；
2. **正文永不出现**：``content.included`` 恒为 ``False``，三种 consent 状态下都如此；
3. **绝不假称 live**：合成数据 + ``gateway_attached=True`` 仍然只能是 ``demo``。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from atlas_support import (  # noqa: E402
    SYNTHETIC,
    WINDOW_FROM,
    WINDOW_TO,
    calls_directory,
    calls_request,
    load_fixture,
    requester,
    self_requester,
)

from backend.atlas import (  # noqa: E402
    CALLS_SCHEMA_VERSION,
    CALLS_DEFAULT_LIMIT,
    CALLS_MAX_LIMIT,
    CONTENT_CONSENT_ABSENT,
    CONTENT_CONSENT_GRANTED,
    CONTENT_CONSENT_WITHDRAWN,
    SCOPE_CALLS_SELF_READ,
    SCOPE_USAGE_READ,
    AccessDenied,
    ContractError,
    InvalidRequest,
    Provenance,
    build_atlas_dto,
    build_self_calls_dto,
    validate_dto,
    validate_self_calls_dto,
)
from backend.atlas import calls as calls_contract  # noqa: E402

FIXTURE = "gateway_calls_sample.jsonl"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.text = load_fixture(FIXTURE)
        self.directory = calls_directory()

    def build(self, ctx=None, **kwargs) -> dict:
        request = calls_request(ctx, **kwargs) if ctx is not None else calls_request(**kwargs)
        return build_self_calls_dto(request, self.text, self.directory, SYNTHETIC)

    def keys_in(self, dto: dict) -> list[str]:
        return [row["key_id"] for row in dto["calls"]]

    @staticmethod
    def _scrub_identity_fields(line: str) -> str:
        """删掉一行里的自报身份字段；不可解析的行原样留下（它是坏行样本）。"""
        try:
            row = json.loads(line)
        except ValueError:
            return line
        return json.dumps(
            {
                key: value
                for key, value in row.items()
                if key not in ("x_project", "x_principal", "X-Forwarded-User")
            },
            ensure_ascii=False,
        )


class AuthorizationTest(_Base):
    """授权负测：失败必须抛 AccessDenied，且失败理由不区分「谁的 key」。"""

    def test_the_calls_scope_is_required(self) -> None:
        ctx = self_requester(scopes=())
        with self.assertRaises(AccessDenied):
            self.build(ctx)

    def test_the_aggregate_scope_is_not_enough(self) -> None:
        """``atlas:usage:read`` 不自动换来逐条明细 —— 两条口径各自授权。"""
        ctx = self_requester(scopes=(SCOPE_USAGE_READ,))
        with self.assertRaises(AccessDenied):
            self.build(ctx)

    def test_blank_subject_is_denied(self) -> None:
        for subject in ("", "   "):
            with self.subTest(subject=repr(subject)):
                with self.assertRaises(AccessDenied):
                    self.build(self_requester(subject))

    def test_admin_role_is_not_a_fallback(self) -> None:
        ctx = self_requester(roles=("admin",), scopes=(), projects=())
        with self.assertRaises(AccessDenied):
            self.build(ctx)

    def test_empty_project_whitelist_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester(projects=()))

    def test_a_subject_owning_no_key_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester("u-9999"))

    def test_someone_elses_key_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester(), requested_key="key-beta")

    def test_a_key_in_another_project_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester(), requested_key="key-gamma")

    def test_an_unregistered_key_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester(), requested_key="unknown-key-zzz")

    def test_own_revoked_key_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester(), requested_key="key-retired")

    def test_own_key_outside_the_project_whitelist_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            self.build(self_requester(), requested_key="key-outside")

    def test_every_refusal_is_the_same_refusal(self) -> None:
        """未注册 / 他人 / 已吊销 / 越项目 —— 四种成因必须是**不可区分**的同一拒绝。

        否则端点就变成一个可以枚举他人 ``key_id`` 存在性的预言机：调用方靠
        「404 还是 403」「消息不同」就能问出哪把 key 真实存在。
        """
        outcomes = []
        for key in ("key-beta", "key-gamma", "unknown-key-zzz", "key-retired", "key-outside"):
            try:
                self.build(self_requester(), requested_key=key)
            except AccessDenied as exc:
                outcomes.append((type(exc).__name__, str(exc)))
        self.assertEqual(len(outcomes), 5)
        self.assertEqual(len(set(outcomes)), 1, outcomes)

    def test_a_forged_requester_object_is_rejected(self) -> None:
        """一个字符串冒充 ``RequesterContext`` 必须在解析阶段就被挡下。"""
        request = calls_request()
        object.__setattr__(request, "requester", "u-1001")
        with self.assertRaises(InvalidRequest):
            build_self_calls_dto(request, self.text, self.directory, SYNTHETIC)

    def test_an_unknown_consent_state_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.build(content_consent="definitely-not-a-state")


class OwnerOnlyVisibilityTest(_Base):
    """可见性：明细里只能有自己的 key。"""

    def test_only_own_keys_appear(self) -> None:
        dto = self.build(self_requester())
        self.assertEqual(sorted(set(self.keys_in(dto))), ["key-alpha", "key-orphan"])

    def test_a_colleague_key_in_the_same_project_never_appears(self) -> None:
        """同一项目下他人的 key —— 这正是只按项目聚合时会串进来的那一种。"""
        dto = self.build(self_requester())
        for forbidden in ("key-beta", "key-gamma", "key-outside", "key-retired"):
            self.assertNotIn(forbidden, self.keys_in(dto))
        self.assertNotIn("key-beta", json.dumps(dto, ensure_ascii=False))

    def test_own_revoked_key_rows_are_counted_but_not_listed(self) -> None:
        dto = self.build(self_requester())
        self.assertNotIn("key-retired", self.keys_in(dto))
        self.assertEqual(dto["counts"]["revoked_key_rows"], 1)
        self.assertEqual(dto["scope"]["revoked_keys"], ["key-retired"])

    def test_an_unregistered_key_is_not_even_counted(self) -> None:
        """未注册的 key 结构上不可归属 —— 连「有几条」都不能说（侧信道）。"""
        dto = self.build(self_requester())
        self.assertNotIn("unknown-key-zzz", json.dumps(dto, ensure_ascii=False))
        self.assertEqual(dto["counts"]["window_excluded"], 1)
        self.assertEqual(dto["counts"]["revoked_key_rows"], 1)
        # 除了这两个可归属计数器，没有任何「被挡掉了多少条」的数字。
        self.assertEqual(
            sorted(dto["counts"]),
            [
                "available",
                "next_before",
                "returned",
                "revoked_key_rows",
                "truncated",
                "unattributed_rows",
                "window_excluded",
            ],
        )

    def test_forged_identity_fields_in_the_row_have_zero_effect(self) -> None:
        """把 fixture 里的 x_principal / X-Forwarded-User 抹掉，DTO 逐字节相同。"""
        scrubbed = "\n".join(
            self._scrub_identity_fields(line) for line in self.text.splitlines() if line.strip()
        )
        baseline = self.build(self_requester())
        scrubbed_dto = build_self_calls_dto(
            calls_request(), scrubbed, self.directory, SYNTHETIC
        )
        self.assertEqual(
            json.dumps(baseline, sort_keys=True, ensure_ascii=False),
            json.dumps(scrubbed_dto, sort_keys=True, ensure_ascii=False),
        )

    def test_the_row_never_carries_the_client_supplied_label(self) -> None:
        """``label`` 是客户端可自报的 —— 逐条视图也不给它第二个身份来源。"""
        dto = self.build(self_requester())
        for row in dto["calls"]:
            self.assertNotIn("label", row)
        self.assertNotIn("spoof", json.dumps(dto, ensure_ascii=False))

    def test_narrowing_to_one_own_key_is_allowed(self) -> None:
        dto = self.build(self_requester(), requested_key="key-orphan")
        self.assertEqual(dto["scope"]["keys"], ["key-orphan"])
        self.assertEqual(set(self.keys_in(dto)), {"key-orphan"})

    def test_a_multi_project_owner_still_only_sees_within_the_whitelist(self) -> None:
        ctx = self_requester(projects=("proj-a", "proj-b"))
        dto = self.build(ctx)
        # key-outside 属于本人且在 proj-b 内 —— 一旦白名单放开，它就该出现。
        self.assertIn("key-outside", self.keys_in(dto))
        self.assertEqual(dto["scope"]["cross_key"], True)


class ContentNeverLeavesTest(_Base):
    """正文可见性：三种 consent 状态下 ``content.included`` 都是 ``False``。"""

    def test_content_is_never_included_in_any_consent_state(self) -> None:
        for consent in (CONTENT_CONSENT_ABSENT, CONTENT_CONSENT_GRANTED, CONTENT_CONSENT_WITHDRAWN):
            with self.subTest(consent=consent):
                dto = self.build(content_consent=consent)
                self.assertIs(dto["content"]["included"], False)
                self.assertEqual(dto["content"]["consent"], consent)

    def test_granting_consent_does_not_change_the_rows_at_all(self) -> None:
        absent = self.build(content_consent=CONTENT_CONSENT_ABSENT)
        granted = self.build(content_consent=CONTENT_CONSENT_GRANTED)
        self.assertEqual(absent["calls"], granted["calls"])

    def test_the_reason_distinguishes_not_asked_from_not_collected(self) -> None:
        absent = self.build(content_consent=CONTENT_CONSENT_ABSENT)
        granted = self.build(content_consent=CONTENT_CONSENT_GRANTED)
        self.assertEqual(
            absent["content"]["reason"], calls_contract.CONTENT_REASON_CONSENT_ABSENT
        )
        self.assertEqual(
            granted["content"]["reason"], calls_contract.CONTENT_REASON_NOT_COLLECTED
        )

    def test_the_prompt_canary_row_is_absent(self) -> None:
        dto = self.build(self_requester())
        self.assertNotIn("SYNTHETIC-CANARY", json.dumps(dto, ensure_ascii=False))
        # 03:20 那一行被 ingest 拒绝，因此不计入任何明细；窗口内可归属的只有 5 条。
        self.assertEqual(dto["counts"]["available"], 5)

    def test_no_row_key_looks_like_content(self) -> None:
        dto = self.build(self_requester())
        for row in dto["calls"]:
            for field in row:
                self.assertNotIn(field.lower(), calls_contract.CALL_ROW_FORBIDDEN_FIELDS)


class TransportHonestyTest(_Base):
    """合成数据永远不能自称 live。"""

    def test_synthetic_data_is_demo_even_when_the_gateway_is_claimed_attached(self) -> None:
        dto = self.build(gateway_attached=True)
        self.assertTrue(dto["transport"]["gateway_attached"])
        self.assertEqual(dto["transport"]["mode"], "demo")
        self.assertEqual(dto["display_hints"][1].count("未接线真实 Gateway"), 1)

    def test_synthetic_data_without_the_claim_is_demo(self) -> None:
        self.assertEqual(self.build()["transport"]["mode"], "demo")

    def test_live_requires_both_conditions(self) -> None:
        real = Provenance(source="gateway-export", synthetic=False)
        dto = build_self_calls_dto(
            calls_request(gateway_attached=True), self.text, self.directory, real
        )
        self.assertEqual(dto["transport"]["mode"], "live")
        # 同一份「真实」来源，但不声明已接线 -> 仍然是 demo。
        timid = build_self_calls_dto(
            calls_request(gateway_attached=False), self.text, self.directory, real
        )
        self.assertEqual(timid["transport"]["mode"], "demo")

    def test_the_hints_always_say_this_is_synthetic(self) -> None:
        dto = self.build()
        self.assertTrue(any("SYNTHETIC" in hint for hint in dto["display_hints"]))


class MissingValuesTest(_Base):
    """缺失值一律 ``None``，不用 ``0`` 顶替。"""

    def test_a_row_without_usage_has_null_tokens_not_zero(self) -> None:
        dto = self.build(self_requester())
        failing = [row for row in dto["calls"] if row["outcome"] == "failed"]
        self.assertTrue(failing)
        for row in failing:
            self.assertIsNone(row["tokens"]["input"])
            self.assertIsNone(row["tokens"]["output"])
            self.assertIsNone(row["tokens"]["total"])

    def test_a_present_usage_is_summed(self) -> None:
        dto = self.build(self_requester(), requested_key="key-orphan")
        row = dto["calls"][0]
        self.assertEqual(row["tokens"], {"input": 20, "output": 30, "total": 50})

    def test_missing_latency_stays_null_and_is_counted(self) -> None:
        """``ttft_ms`` 缺失就是 ``null``；覆盖率只数「没记」的行数，不填 0。"""
        dto = self.build(self_requester())
        unknown = [row for row in dto["calls"] if row["ttft_ms"] is None]
        self.assertTrue(unknown)
        self.assertEqual(len(unknown), dto["coverage"]["ttft_unknown_rows"])
        self.assertEqual(
            len([row for row in dto["calls"] if row["queue_ms"] is None]),
            dto["coverage"]["queue_unknown_rows"],
        )
        for row in dto["calls"]:
            self.assertFalse(row["ttft_ms"] == 0)

    def test_a_request_id_is_reported_when_the_upstream_gave_one(self) -> None:
        dto = self.build(self_requester())
        by_id = {row["request_id"]: row["status"] for row in dto["calls"]}
        self.assertIn("req-SYN-0001", by_id)
        self.assertIn("req-SYN-0004", by_id)

    def test_a_missing_request_id_is_null_and_never_invented(self) -> None:
        dto = self.build(self_requester(), requested_key="key-orphan")
        self.assertIsNone(dto["calls"][0]["request_id"])
        self.assertEqual(dto["coverage"]["request_id_absent_rows"], 1)

    def test_call_id_is_derived_and_declared_as_such(self) -> None:
        dto = self.build(self_requester())
        for row in dto["calls"]:
            self.assertTrue(row["call_id"].startswith("call_"))
            self.assertEqual(row["call_id_basis"], calls_contract.CALL_ID_BASIS)

    def test_call_ids_are_stable_across_builds(self) -> None:
        first = self.build(self_requester())
        second = self.build(self_requester())
        self.assertEqual(
            [row["call_id"] for row in first["calls"]],
            [row["call_id"] for row in second["calls"]],
        )


class FilterAndPagingTest(_Base):
    def test_rows_are_newest_first(self) -> None:
        dto = self.build(self_requester())
        stamps = [row["ts"] for row in dto["calls"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_outcome_filter_narrows(self) -> None:
        dto = self.build(self_requester(), outcome="failed")
        self.assertTrue(dto["calls"])
        self.assertEqual({row["outcome"] for row in dto["calls"]}, {"failed"})

    def test_an_unsupported_outcome_filter_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.build(self_requester(), outcome="exploded")

    def test_model_filter_narrows(self) -> None:
        dto = self.build(self_requester(), model="llm-b")
        self.assertEqual(dto["calls"], [])

    def test_a_window_excludes_rows_and_counts_them(self) -> None:
        # 00:10 起：两条 00:05 落到窗口**之前**，04:30 那条落到 to 之后，共 3 条。
        dto = self.build(self_requester(), window_from="2026-10-14T00:10:00Z")
        self.assertEqual(dto["counts"]["window_excluded"], 3)
        self.assertEqual(dto["counts"]["available"], 3)

    def test_the_window_is_half_open(self) -> None:
        # 03:40 那一行正好在 to 之前；to 递减到 03:40 时它就该被排除。
        inclusive_to = self.build(self_requester(), window_to="2026-10-14T03:41:00Z")
        exclusive_to = self.build(self_requester(), window_to="2026-10-14T03:40:00Z")
        self.assertEqual(inclusive_to["counts"]["available"], 5)
        self.assertEqual(exclusive_to["counts"]["available"], 4)

    def test_a_truncated_page_says_so(self) -> None:
        dto = self.build(self_requester(), limit=2)
        self.assertEqual(dto["counts"]["returned"], 2)
        self.assertEqual(dto["counts"]["available"], 5)
        self.assertTrue(dto["counts"]["truncated"])
        self.assertEqual(
            dto["counts"]["next_before"]["call_id"], dto["calls"][-1]["call_id"]
        )

    def test_an_untruncated_page_carries_no_cursor(self) -> None:
        dto = self.build(self_requester(), limit=CALLS_MAX_LIMIT)
        self.assertFalse(dto["counts"]["truncated"])
        self.assertIsNone(dto["counts"]["next_before"])

    def test_paging_visits_every_row_exactly_once(self) -> None:
        """游标式翻页**不漏不重** —— 包括两行时间戳完全相同的那一对。

        这正是「用时间戳当游标」会出错的地方：``00:05`` 上并排着两条记录，
        以 ``ts`` 为界会把它们一起跳过或一起重复。
        """
        seen: list[str] = []
        cursor: dict | None = None
        for _ in range(10):
            kwargs = {"limit": 2}
            if cursor is not None:
                kwargs["before_ts"] = cursor["ts"]
                kwargs["before_call_id"] = cursor["call_id"]
            dto = self.build(self_requester(), **kwargs)
            seen.extend(row["call_id"] for row in dto["calls"])
            if not dto["counts"]["truncated"]:
                break
            cursor = dto["counts"]["next_before"]
        self.assertEqual(len(seen), 5, seen)
        self.assertEqual(len(set(seen)), 5, seen)

    def test_a_half_supplied_cursor_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.build(self_requester(), before_ts="2026-10-14T03:00:00Z")
        with self.assertRaises(InvalidRequest):
            self.build(self_requester(), before_call_id="call_deadbeef")

    def test_limit_bounds_are_enforced(self) -> None:
        for bad in (0, -1, CALLS_MAX_LIMIT + 1, True, 1.5, "10"):
            with self.subTest(limit=repr(bad)):
                with self.assertRaises(InvalidRequest):
                    self.build(self_requester(), limit=bad)

    def test_the_default_limit_is_the_documented_one(self) -> None:
        self.assertEqual(calls_request().limit, CALLS_DEFAULT_LIMIT)


class SeparateContractTest(_Base):
    """「我的调用」与项目汇总必须仍是两条互不影响的口径。"""

    def test_the_two_schema_versions_differ(self) -> None:
        from backend.atlas.dto import SCHEMA_VERSION

        self.assertNotEqual(CALLS_SCHEMA_VERSION, SCHEMA_VERSION)

    def test_the_aggregate_dto_gains_nothing_from_this_feature(self) -> None:
        """汇总 DTO 里不得出现 request_id / call_id —— 新字段没有渗进旧口径。"""
        from atlas_support import atlas_request, trusted_directory

        aggregate = build_atlas_dto(
            atlas_request(requester()), self.text, trusted_directory(), SYNTHETIC
        )
        validate_dto(aggregate)
        blob = json.dumps(aggregate, ensure_ascii=False)
        self.assertNotIn("request_id", blob)
        self.assertNotIn("call_id", blob)

    def test_the_aggregate_scope_does_not_open_the_self_view(self) -> None:
        ctx = self_requester(scopes=(SCOPE_USAGE_READ,))
        with self.assertRaises(AccessDenied):
            self.build(ctx)

    def test_the_self_scope_does_not_open_the_aggregate(self) -> None:
        from atlas_support import atlas_request, trusted_directory

        ctx = requester(scopes=(SCOPE_CALLS_SELF_READ,))
        with self.assertRaises(AccessDenied):
            build_atlas_dto(
                atlas_request(ctx), self.text, trusted_directory(), SYNTHETIC
            )

    def test_the_self_view_counts_more_rows_than_the_project_aggregate(self) -> None:
        """同一个 fixture：汇总会把同项目他人的 key 计入，逐条视图不会。

        这个差值就是「另一条口径」的具体含义，而不是一个可以顺手抹平的不一致。
        """
        from atlas_support import atlas_request, trusted_directory

        aggregate = build_atlas_dto(
            atlas_request(requester()), self.text, trusted_directory(), SYNTHETIC
        )
        own = self.build(self_requester())
        self.assertGreater(aggregate["totals"]["calls"], own["counts"]["available"])


class SelfCheckTest(_Base):
    """出口自检必须真的会失败 —— 一个不会失败的守卫只是装饰。"""

    def setUp(self) -> None:
        super().setUp()
        self.dto = self.build(self_requester())

    def _tampered(self, mutate) -> dict:
        dto = copy.deepcopy(self.dto)
        mutate(dto)
        return dto

    def _assert_rejected(self, mutate) -> None:
        with self.assertRaises(ContractError):
            validate_self_calls_dto(self._tampered(mutate))

    def test_the_baseline_passes(self) -> None:
        validate_self_calls_dto(self.dto)

    def test_content_included_true_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["content"].__setitem__("included", True))

    def test_a_foreign_key_id_in_a_row_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["calls"][0].__setitem__("key_id", "key-beta"))

    def test_a_project_outside_the_scope_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["calls"][0].__setitem__("project", "proj-b"))

    def test_an_extra_row_field_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["calls"][0].__setitem__("debug", "x"))

    def test_a_prompt_field_in_a_row_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["calls"][0].__setitem__("prompt", "hi"))

    def test_a_label_field_in_a_row_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["calls"][0].__setitem__("label", "x"))

    def test_out_of_order_rows_are_rejected(self) -> None:
        def swap(dto):
            dto["calls"][0], dto["calls"][1] = dto["calls"][1], dto["calls"][0]

        self._assert_rejected(swap)

    def test_a_token_total_with_a_null_side_is_rejected(self) -> None:
        def mutate(dto):
            dto["calls"][0]["tokens"] = {"input": None, "output": 5, "total": 5}

        self._assert_rejected(mutate)

    def test_an_inconsistent_returned_count_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["counts"].__setitem__("returned", 99))

    def test_a_truncated_page_without_a_cursor_is_rejected(self) -> None:
        def mutate(dto):
            dto["counts"]["truncated"] = True
            dto["counts"]["next_before"] = None

        self._assert_rejected(mutate)

    def test_claiming_live_on_synthetic_data_is_rejected(self) -> None:
        self._assert_rejected(
            lambda dto: dto["transport"].__setitem__("mode", "live")
        )

    def test_an_empty_scope_key_set_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto["scope"].__setitem__("keys", []))

    def test_dropping_the_notes_is_rejected(self) -> None:
        self._assert_rejected(lambda dto: dto.__setitem__("notes", []))

    def test_the_dto_is_json_serialisable(self) -> None:
        blob = json.dumps(self.dto, sort_keys=True, ensure_ascii=False)
        self.assertEqual(json.loads(blob), self.dto)


if __name__ == "__main__":
    unittest.main()
