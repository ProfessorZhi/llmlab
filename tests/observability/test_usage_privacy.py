"""P0 隐私边界：一个普通主体不得从自己的 DTO 里读到任何关于他人的东西。

**验收口径不是「某个数字等于 X」，而是：**

> 对同一个 viewer，两批输入只要**可见样本相同**，整份 DTO 必须**逐字节相同** ——
> 无论第二批额外塞进多少其他主体的行 / 未注册 key / 损坏 JSON / 带正文行 /
> 自报身份行 / 窗口外行。

以及 ``null``（无从测得）与 ``0``（确实测得零）必须分开：能归属到可见主体的计数器
是真数过的整数（可以是 0）；结构上无法归属的原始诊断一律 ``None``。
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
    ENDPOINT_PRIVATE_C,
    ENDPOINT_RETIRED,
    ENDPOINT_SHARED_A,
    ENDPOINT_SHARED_B,
    FOREIGN_ROWS,
    NOW,
    SYNTHETIC,
    UID_ALICE,
    UID_BOB,
    UID_CAROL,
    admin_principal,
    load_fixture,
    office_principal,
    principal,
    registry,
    request,
    row,
    text_of,
    trusted_directory,
)

from backend.observability import (  # noqa: E402
    QUALITY_MEASURED,
    QUALITY_UNKNOWN,
    AccessDenied,
    ContractError,
    build_admin_overview_dto,
    build_self_usage_dto,
    build_timeline_dto,
    validate_usage_dto,
)

#: 结构上无法归属到任何主体的原始诊断 —— 普通主体一律 ``None``。
UNATTRIBUTABLE = ("unknown_key", "malformed_lines", "content_field_rejected")

#: 可归属到可见主体的计数器 —— 是真数过的整数，可以是 0。
SCOPED = (
    "out_of_window",
    "revoked_key",
    "filtered_out",
    "out_of_scope_endpoint",
    "endpoint_missing",
    "outcome_status_mismatch",
    "latency_missing",
    "ttft_missing",
)


class _Builder:
    """同一个 viewer，喂不同批次，便于做逐字节对比。"""

    def __init__(self) -> None:
        self.directory = trusted_directory()
        self.registry = registry()

    def own(self, rows, ctx=None, **request_kwargs):
        return build_self_usage_dto(
            request(ctx or principal(UID_ALICE), **request_kwargs),
            text_of(rows),
            self.directory,
            self.registry,
            SYNTHETIC,
            now=NOW,
        )

    def timeline(self, rows, ctx=None, **request_kwargs):
        return build_timeline_dto(
            request(ctx or principal(UID_ALICE), **request_kwargs),
            text_of(rows),
            self.directory,
            self.registry,
            SYNTHETIC,
            now=NOW,
        )

    def admin(self, rows, ctx=None, **request_kwargs):
        return build_admin_overview_dto(
            request(ctx or admin_principal(), **request_kwargs),
            text_of(rows),
            self.directory,
            self.registry,
            SYNTHETIC,
            now=NOW,
        )


def _canonical(dto) -> str:
    return json.dumps(dto, sort_keys=True)


class ByteInvarianceTest(unittest.TestCase):
    """额外塞进不可归属的数据，不得改变普通 viewer 的 DTO。"""

    def setUp(self) -> None:
        self.builder = _Builder()
        self.clean = self.builder.own(ALICE_ROWS)
        shuffled = (
            FOREIGN_ROWS[3],
            FOREIGN_ROWS[1],
            FOREIGN_ROWS[5],
            FOREIGN_ROWS[0],
            FOREIGN_ROWS[4],
            FOREIGN_ROWS[2],
        )
        self.polluted = self.builder.own(ALICE_ROWS + shuffled)

    def test_extra_foreign_and_corrupt_rows_do_not_change_the_dto_bytes(self) -> None:
        self.assertEqual(_canonical(self.clean), _canonical(self.polluted))

    def test_display_hints_are_unchanged(self) -> None:
        self.assertEqual(self.clean["display_hints"], self.polluted["display_hints"])

    def test_quality_block_is_unchanged(self) -> None:
        self.assertEqual(self.clean["quality"], self.polluted["quality"])

    def test_unknown_block_is_unchanged(self) -> None:
        self.assertEqual(self.clean["unknown"], self.polluted["unknown"])

    def test_buckets_are_unchanged(self) -> None:
        self.assertEqual(self.clean["buckets"], self.polluted["buckets"])

    def test_visible_sample_is_actually_measured_so_the_test_has_teeth(self) -> None:
        """若可见样本本身为空/不可测，上面的「相同」会退化成恒真。"""
        self.assertEqual(self.clean["counters"]["calls"], 3)
        self.assertEqual(self.clean["counters"]["success"], 3)
        self.assertEqual(self.clean["metrics"]["principal.requests"]["value"], 3)
        self.assertEqual(self.clean["quality"]["calls"], QUALITY_MEASURED)

    def test_timeline_is_also_byte_invariant(self) -> None:
        clean = self.builder.timeline(ALICE_ROWS)
        polluted = self.builder.timeline(ALICE_ROWS + FOREIGN_ROWS)
        self.assertEqual(_canonical(clean), _canonical(polluted))


class NoForeignNamesAnywhereTest(unittest.TestCase):
    def test_no_foreign_uid_endpoint_or_canary_survives(self) -> None:
        rows = ALICE_ROWS + FOREIGN_ROWS + (
            row(
                "2026-10-15T11:10:00Z",
                "key-alpha",
                endpoint=ENDPOINT_RETIRED,
                extra={"prompt": "<SYNTHETIC-CANARY: 正文永不出现>"},
            ),
        )
        blob = _canonical(_Builder().own(rows))
        for token in (
            UID_BOB,
            UID_CAROL,
            ENDPOINT_SHARED_B,
            ENDPOINT_PRIVATE_C,
            "unknown-key-zzz",
            "SYNTHETIC-CANARY",
            "key-gamma",
            "key-beta",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)

    def test_an_unauthorised_endpoint_is_counted_but_never_named(self) -> None:
        """本人自己的行引用了无权端点 —— 计入一个**不带名字**的计数。"""
        rows = ALICE_ROWS + (
            row("2026-10-15T11:10:00Z", "key-alpha", endpoint=ENDPOINT_RETIRED),
        )
        dto = _Builder().own(rows)
        self.assertEqual(dto["unknown"]["out_of_scope_endpoint"], 1)
        self.assertNotIn(ENDPOINT_RETIRED, _canonical(dto))

    def test_the_authorised_catalogue_is_exactly_the_visible_endpoints(self) -> None:
        dto = _Builder().own(ALICE_ROWS)
        self.assertEqual(dto["scope"]["endpoints"], [ENDPOINT_SHARED_A])
        self.assertEqual(
            {group["id"] for group in dto["groups"]}, {ENDPOINT_SHARED_A}
        )

    def test_a_model_the_user_cannot_use_does_not_appear_even_as_a_zero(self) -> None:
        """不是「显示为 0 次」，而是**不存在** —— 后者会变成一条可读的断言。"""
        dto = _Builder().own(ALICE_ROWS)
        ids = {group["id"] for group in dto["groups"]}
        self.assertNotIn(ENDPOINT_SHARED_B, ids)
        self.assertNotIn(ENDPOINT_PRIVATE_C, ids)
        self.assertEqual([group["counters"]["calls"] for group in dto["groups"]], [3])

    def test_the_fixture_does_not_leak_foreign_activity_to_a_single_user(self) -> None:
        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            load_fixture("gateway_sample.jsonl"),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        blob = _canonical(dto)
        for token in (UID_BOB, UID_CAROL, ENDPOINT_SHARED_B, "key-gamma", "key-beta"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)
        # 而本人的样本确实被数到了 —— 否则上面全是空断言。
        self.assertEqual(dto["counters"]["calls"], 5)
        self.assertEqual(dto["counters"]["success"], 3)


class FilterOracleTest(unittest.TestCase):
    """过滤器不得变成「这个端点存不存在」的预言机。"""

    def test_an_unauthorised_endpoint_filter_is_denied(self) -> None:
        for candidate in (ENDPOINT_SHARED_B, ENDPOINT_PRIVATE_C, "model-nope"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AccessDenied):
                    _Builder().own(ALICE_ROWS, requested_endpoint=candidate)

    def test_the_denial_is_identical_for_existing_and_nonexistent_endpoints(self) -> None:
        messages = set()
        for candidate in (ENDPOINT_SHARED_B, ENDPOINT_PRIVATE_C, "model-nope", ""):
            with self.subTest(candidate=candidate):
                with self.assertRaises(AccessDenied) as caught:
                    _Builder().own(ALICE_ROWS, requested_endpoint=candidate)
                messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1, messages)

    def test_filtering_by_an_authorised_endpoint_keeps_counts_self_scoped(self) -> None:
        dto = _Builder().own(
            ALICE_ROWS + FOREIGN_ROWS, requested_endpoint=ENDPOINT_SHARED_A
        )
        self.assertEqual(dto["counters"]["calls"], 3)
        self.assertEqual(dto["unknown"]["filtered_out"], 0)

    def test_filtering_out_own_rows_is_counted_but_the_name_is_authorised(self) -> None:
        """本人持两个端点的授权时，过滤掉其中一个 —— 计数可归属，名字也已授权。"""
        ctx = principal(UID_ALICE, endpoints=(ENDPOINT_SHARED_A, ENDPOINT_SHARED_B))
        rows = ALICE_ROWS + (
            row("2026-10-15T11:20:00Z", "key-alpha", endpoint=ENDPOINT_SHARED_A),
        )
        dto = _Builder().own(rows, ctx, requested_endpoint=ENDPOINT_SHARED_A)
        self.assertEqual(dto["counters"]["calls"], 4)
        self.assertEqual(dto["unknown"]["filtered_out"], 0)

    def test_no_filter_means_no_filtered_out_counter_movement(self) -> None:
        dto = _Builder().own(ALICE_ROWS + FOREIGN_ROWS)
        self.assertEqual(dto["unknown"]["filtered_out"], 0)


class NullVersusZeroTest(unittest.TestCase):
    """``null``（无从测得）与 ``0``（确实测得零）必须分开。"""

    def test_unattributable_counters_are_null_for_a_plain_user(self) -> None:
        dto = _Builder().own(ALICE_ROWS + FOREIGN_ROWS)
        for name in UNATTRIBUTABLE:
            with self.subTest(counter=name):
                self.assertIsNone(dto["unknown"][name])
                self.assertIsNot(dto["unknown"][name], 0)

    def test_unattributable_counters_are_null_even_when_the_sample_is_pristine(self) -> None:
        """干净的可见样本也不得把全局计数伪造成 0 —— 无从测得就是无从测得。"""
        dto = _Builder().own(ALICE_ROWS)
        for name in UNATTRIBUTABLE:
            with self.subTest(counter=name):
                self.assertIsNone(dto["unknown"][name])

    def test_scoped_counters_are_measured_zero_not_null(self) -> None:
        dto = _Builder().own(ALICE_ROWS)
        for name in SCOPED:
            with self.subTest(counter=name):
                self.assertIsInstance(dto["unknown"][name], int)
                self.assertNotIsInstance(dto["unknown"][name], bool)
                self.assertEqual(dto["unknown"][name], 0)

    def test_revoked_keys_of_another_user_are_not_counted_for_me(self) -> None:
        """``key-revoked`` 属 u-3001 —— 对本人在归属判定阶段就被丢掉了。"""
        dto = _Builder().own(ALICE_ROWS + FOREIGN_ROWS)
        self.assertEqual(dto["unknown"]["revoked_key"], 0)

    def test_an_empty_log_does_not_claim_zero_requests(self) -> None:
        dto = _Builder().own([])
        self.assertEqual(dto["quality"]["calls"], QUALITY_UNKNOWN)
        metric = dto["metrics"]["principal.requests"]
        self.assertIsNone(metric["value"])
        self.assertIsNot(metric["value"], 0)
        self.assertEqual(metric["reason"], "no_sample")

    def test_an_empty_fixture_behaves_the_same_way(self) -> None:
        dto = build_self_usage_dto(
            request(principal(UID_ALICE)),
            load_fixture("gateway_empty.jsonl"),
            trusted_directory(),
            registry(),
            SYNTHETIC,
            now=NOW,
        )
        self.assertEqual(dto["quality"]["calls"], QUALITY_UNKNOWN)
        requests = dto["metrics"]["principal.requests"]
        self.assertIsNone(requests["value"])
        self.assertIsNot(requests["value"], 0)
        self.assertEqual(dto["counters"]["calls"], 0)

    def test_no_latency_samples_means_null_percentiles(self) -> None:
        rows = [row("2026-10-15T08:05:00Z", "key-alpha", status=429,
                    outcome="failed", total_ms=None, ttft_ms=None, queue_ms=None)]
        dto = _Builder().own(rows)
        self.assertIsNone(dto["latency"]["p50_ms"])
        self.assertIsNone(dto["latency"]["p95_ms"])
        self.assertIsNot(dto["latency"]["p95_ms"], 0)
        self.assertEqual(dto["latency"]["samples"], 0)

    def test_no_token_samples_means_null_totals(self) -> None:
        rows = [row("2026-10-15T08:05:00Z", "key-alpha",
                    input_tokens=None, output_tokens=None)]
        dto = _Builder().own(rows)
        self.assertIsNone(dto["tokens"]["input_total"])
        self.assertIsNone(dto["tokens"]["output_total"])
        self.assertIsNone(dto["metrics"]["principal.tokens.input"]["value"])


class RoleDoesNotUnlockTest(unittest.TestCase):
    def test_a_role_admin_without_scope_gets_the_same_data_as_anyone(self) -> None:
        """role 不进任何判定 —— 两边的**数据**必须逐字节相同。

        比较时把 ``scope.roles`` / ``scope.scopes`` 摘掉：那是**审计记录**
        （本视图如实记下「你是谁、你持什么」），不是数据口径。若连它都要求相同，
        这条断言就变成恒假，而不是在证明「role 不开门」。
        """
        plain = _Builder().own(ALICE_ROWS + FOREIGN_ROWS)
        roley = _Builder().own(ALICE_ROWS + FOREIGN_ROWS, office_principal(UID_ALICE))

        for dto in (plain, roley):
            dto["scope"].pop("roles")
            dto["scope"].pop("scopes")
        self.assertEqual(_canonical(plain), _canonical(roley))
        for name in UNATTRIBUTABLE:
            with self.subTest(counter=name):
                self.assertIsNone(roley["unknown"][name])

    def test_behavioural_difference_between_role_admin_and_plain_user_is_nil(self) -> None:
        """除审计字段外，两个主体的每一次**行为**判定都一致。"""
        for kwargs in (
            {},
            {"requested_endpoint": ENDPOINT_SHARED_A},
        ):
            with self.subTest(kwargs=kwargs):
                plain = _Builder().own(ALICE_ROWS, principal(UID_ALICE), **kwargs)
                roley = _Builder().own(ALICE_ROWS, office_principal(UID_ALICE), **kwargs)
                self.assertEqual(plain["counters"], roley["counters"])
                self.assertEqual(plain["scope"]["endpoints"], roley["scope"]["endpoints"])

        for kwargs in ({"requested_uid": UID_BOB}, {"requested_endpoint": ENDPOINT_SHARED_B}):
            with self.subTest(kwargs=kwargs):
                for ctx in (principal(UID_ALICE), office_principal(UID_ALICE)):
                    with self.assertRaises(AccessDenied):
                        _Builder().own(ALICE_ROWS, ctx, **kwargs)

    def test_a_role_admin_cannot_view_another_uid(self) -> None:
        with self.assertRaises(AccessDenied):
            _Builder().own(ALICE_ROWS, office_principal(UID_ALICE), requested_uid=UID_BOB)

    def test_a_role_admin_cannot_open_the_admin_overview(self) -> None:
        with self.assertRaises(AccessDenied):
            _Builder().admin(ALICE_ROWS, office_principal(UID_ALICE))

    def test_admin_overview_requires_the_admin_scope(self) -> None:
        with self.assertRaises(AccessDenied):
            _Builder().admin(ALICE_ROWS, principal(UID_ALICE))


class AdminSeesAllTest(unittest.TestCase):
    """收紧普通主体不等于把管理视角也砍掉。"""

    def setUp(self) -> None:
        # 含 bob 的一行，好让「全体主体」这件事真的被覆盖到。
        self.rows = ALICE_ROWS + FOREIGN_ROWS + (
            row("2026-10-15T08:35:00Z", "key-beta", endpoint=ENDPOINT_SHARED_A),
        )
        self.admin = _Builder().admin(self.rows)

    def test_admin_sees_every_principal_with_usage(self) -> None:
        uids = {entry["uid"] for entry in self.admin["principals"]}
        self.assertEqual(uids, {UID_ALICE, UID_BOB, UID_CAROL})

    def test_a_principal_without_usage_is_absent_not_zeroed(self) -> None:
        """目录里有、窗口内没调用 —— 不列出，也不以 0 列出。"""
        rows = ALICE_ROWS + FOREIGN_ROWS  # 不含 bob
        admin = _Builder().admin(rows)
        uids = {entry["uid"] for entry in admin["principals"]}
        self.assertNotIn(UID_BOB, uids)
        self.assertIn(UID_ALICE, uids)
        self.assertTrue(any("有可归属调用" in note for note in admin["notes"]))

    def test_admin_sees_unattributable_diagnostics_as_numbers(self) -> None:
        for name in UNATTRIBUTABLE:
            with self.subTest(counter=name):
                self.assertIsInstance(self.admin["unknown"][name], int)
                self.assertNotIsInstance(self.admin["unknown"][name], bool)

    def test_admin_counts_match_the_fixture(self) -> None:
        self.assertEqual(self.admin["unknown"]["unknown_key"], 1)
        self.assertEqual(self.admin["unknown"]["malformed_lines"], 1)
        self.assertEqual(self.admin["unknown"]["content_field_rejected"], 1)

    def test_admin_can_see_all_registered_endpoints(self) -> None:
        self.assertEqual(
            self.admin["scope"]["endpoints"],
            sorted(registry().endpoints()),
        )

    def test_admin_viewing_a_specific_uid_gets_only_that_users_usage(self) -> None:
        dto = _Builder().admin(self.rows, requested_uid=UID_CAROL)
        self.assertEqual(dto["scope"]["viewing_uid"], UID_CAROL)
        # carol 在窗口内有两行可归属调用（第三行带正文，已被拒绝）。
        self.assertEqual(dto["counters"]["calls"], 2)
        self.assertEqual(
            {entry["uid"] for entry in dto["principals"]}, {UID_CAROL}
        )
        self.assertLess(dto["counters"]["calls"], self.admin["counters"]["calls"])

    def test_a_specific_uid_scope_declares_it_was_requested(self) -> None:
        dto = _Builder().admin(self.rows, requested_uid=UID_CAROL)
        self.assertEqual(dto["scope"]["requested_uid"], UID_CAROL)
        global_dto = _Builder().admin(self.rows)
        self.assertIsNone(global_dto["scope"]["requested_uid"])

    def test_admin_overview_is_still_validated(self) -> None:
        validate_usage_dto(self.admin)
        self.assertTrue(self.admin["scope"]["admin"])


class ScopeBlockTest(unittest.TestCase):
    def test_scope_declares_the_session_uid_and_the_viewing_uid(self) -> None:
        dto = _Builder().own(ALICE_ROWS)
        self.assertEqual(dto["scope"]["uid"], UID_ALICE)
        self.assertEqual(dto["scope"]["viewing_uid"], UID_ALICE)
        self.assertTrue(dto["scope"]["is_self_view"])

    def test_requested_uid_mismatch_is_denied(self) -> None:
        with self.assertRaises(AccessDenied):
            _Builder().own(ALICE_ROWS, requested_uid=UID_BOB)

    def test_admin_asking_for_itself_is_a_self_view(self) -> None:
        dto = _Builder().admin(ALICE_ROWS, requested_uid="u-9admin")
        self.assertTrue(dto["scope"]["is_self_view"])
        # 管理主体没拥有任何 key，所以它自己的用量是无从测得的。
        self.assertEqual(dto["quality"]["calls"], QUALITY_UNKNOWN)


class ValidateGatesBoundariesTest(unittest.TestCase):
    """门禁不是靠自觉：``validate_usage_dto`` 必须**拒绝**手工改坏的 DTO。"""

    def _dto(self, *scopes, ctx=None):
        return _Builder().own(ALICE_ROWS + FOREIGN_ROWS, ctx)

    def test_a_validated_dto_passes(self) -> None:
        validate_usage_dto(self._dto())

    def test_rejects_a_numeric_unattributable_counter_without_admin(self) -> None:
        for counter in UNATTRIBUTABLE:
            for planted in (0, 5):
                with self.subTest(counter=counter, planted=planted):
                    dto = self._dto()
                    dto["unknown"][counter] = planted
                    with self.assertRaises(ContractError):
                        validate_usage_dto(dto)

    def test_rejects_a_null_scoped_counter(self) -> None:
        for counter in SCOPED:
            with self.subTest(counter=counter):
                dto = self._dto()
                dto["unknown"][counter] = None
                with self.assertRaises(ContractError):
                    validate_usage_dto(dto)

    def test_rejects_an_unknown_reading_that_carries_a_zero(self) -> None:
        dto = self._dto()
        dto["metrics"]["principal.latency_p95_ms"] = {
            "metric_id": "principal.latency_p95_ms",
            "scope": "principal",
            "unit": "milliseconds",
            "physical": False,
            "value": 0,
            "quality": QUALITY_UNKNOWN,
            "display_quality": QUALITY_UNKNOWN,
            "source": "x",
            "observed_at": None,
            "reason": "no_sample",
            "freshness": {"age_seconds": None, "stale": None, "clock_skew": None,
                          "threshold_seconds": 60},
        }
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_a_percentile_that_is_zero_while_samples_is_zero(self) -> None:
        dto = self._dto()
        dto["latency"] = {"p50_ms": 0, "p95_ms": None, "samples": 0,
                          "missing": 0, "basis": "raw-request-samples"}
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_a_reading_whose_scope_disagrees_with_the_catalogue(self) -> None:
        dto = self._dto()
        dto["metrics"]["principal.requests"]["scope"] = "device"
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_a_physical_reading_that_carries_a_uid(self) -> None:
        dto = self._dto()
        dto["metrics"]["principal.requests"]["uid"] = UID_ALICE
        # principal 不是物理作用域，所以先换成一条物理读数来测这条规则。
        dto["host"] = {
            "source": "mock",
            "synthetic": True,
            "node": {},
            "driver": {},
            "devices": [
                {
                    "index": 0,
                    "name": "X",
                    "scope": "device",
                    "readings": {},
                    "uid": UID_ALICE,
                }
            ],
            "notes": [],
        }
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_an_external_url(self) -> None:
        dto = self._dto()
        dto["external_ui"]["entries"][0]["external_url"] = "https://example.com/grafana"
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_an_iframe_allowing_policy(self) -> None:
        dto = self._dto()
        dto["external_ui"]["iframe_allowed"] = True
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_a_hidden_entry_that_still_leaks_its_path(self) -> None:
        dto = self._dto()
        for entry in dto["external_ui"]["entries"]:
            if entry["id"] == "evalscope":
                entry["path"] = "/observability/ui/evalscope/"
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)

    def test_rejects_a_synthetic_dto_that_does_not_say_so(self) -> None:
        dto = self._dto()
        dto["display_hints"] = [hint for hint in dto["display_hints"] if "SYNTHETIC" not in hint]
        with self.assertRaises(ContractError):
            validate_usage_dto(dto)


if __name__ == "__main__":
    unittest.main()
