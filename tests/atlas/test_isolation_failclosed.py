"""Lane #77 Wave-5 的隔离与 fail-closed 判据（V1-08 / V1-09）。

四条要求各自对应一组用例：

1. **同时存在的 A/B principal 与撤销 key** —— 同一份语料、同一次构建，A 的 DTO 与
   B 的 DTO 必须各自只含本人行；A 的撤销 key 计数对 B **不可见**（跨 UID 侧信道）。
2. **owner 过滤之后的 p95 / coverage** —— 加进外来行（含会主导 p95 的极端延迟、
   以及 null token/TTFT）后，可见 viewer 的分位数、样本数、覆盖率必须逐项不变。
3. **来源 / 目录不可用 -> fail closed** —— 不是「空列表 200」。目录对象缺失必须拒绝；
   语料在装配后消失必须抛错；不可用的 ``AtlasSource`` 不提供任何可调用的 source。
4. **伪造 UID / owner / token 查询不可能生效** —— 请求 dataclass 上根本没有这些字段；
   行里的自报身份字段零影响；``requested_subject`` 伪造被拒。

语料全部是本文件现场构造的**合成**数据，不含任何真实 usage.jsonl / prompt / key。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import datetime

from tests.atlas import support

from backend.atlas import (
    SCOPE_USAGE_READ,
    AtlasRequest,
    Provenance,
    build_atlas_dto,
)
from backend.atlas.calls import SelfCallsRequest, build_self_calls_dto
from backend.atlas.corpus import build_atlas_source
from backend.atlas.errors import AccessDenied, InvalidRequest, SourceUnavailable
from backend.atlas.identity import (
    SCOPE_CALLS_SELF_READ,
    GatewayKeyBinding,
    KeyDirectory,
    RequesterContext,
)

WINDOW_FROM = datetime.fromisoformat("2026-10-14T00:00:00+00:00")
WINDOW_TO = datetime.fromisoformat("2026-10-14T04:00:00+00:00")
PROVENANCE = Provenance(source="synthetic-test", synthetic=True)


def _row(
    ts: str,
    key_id: str,
    *,
    total_ms: int | None = 100,
    usage: dict | None = None,
    ttft_ms: int | None = 50,
    request_id: str | None = None,
    **extra,
) -> str:
    row = {
        "ts": ts,
        "key_id": key_id,
        "request_id": request_id,
        "model": "m-a",
        "path": "/v1/chat/completions",
        "method": "POST",
        "status": 200,
        "outcome": "success",
        "stream": True,
        "usage": usage,
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
    }
    row.update(extra)
    return json.dumps(row, sort_keys=True)


def _text(rows: list[str]) -> str:
    return "\n".join(rows) + "\n"


#: A = u-1001；B = u-4001；两人在同一项目 proj-a 里各有 key，A 还有一把已吊销的 key。
#: ``key-b-other`` 属于 proj-b，用来验证「项目白名单之外的行」在聚合里被静默丢弃。
DIRECTORY = KeyDirectory(
    [
        GatewayKeyBinding("key-a-live", "u-1001", "proj-a", "resume-parse"),
        GatewayKeyBinding("key-a-retired", "u-1001", "proj-a", "resume-parse", revoked=True),
        GatewayKeyBinding("key-b-live", "u-4001", "proj-a", "code-assist"),
        GatewayKeyBinding("key-b-other", "u-4001", "proj-b", "eval-suite"),
    ]
)

CORPUS = _text(
    [
        _row("2026-10-14T00:10:00Z", "key-a-live", request_id="req-a", usage={"input_tokens": 1, "output_tokens": 2}),
        _row("2026-10-14T00:20:00Z", "key-a-retired", request_id="req-retired", usage={"input_tokens": 3, "output_tokens": 4}),
        _row("2026-10-14T00:30:00Z", "key-b-live", request_id="req-b", usage={"input_tokens": 5, "output_tokens": 6}),
    ]
)


def _self_requester(subject: str) -> RequesterContext:
    return RequesterContext(
        subject=subject,
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_CALLS_SELF_READ}),
        authorized_projects=frozenset({"proj-a"}),
    )


def _calls_dto(subject: str, text: str = CORPUS, **kwargs) -> dict:
    request = SelfCallsRequest(
        requester=_self_requester(subject),
        window_from=WINDOW_FROM,
        window_to=WINDOW_TO,
        limit=50,
        **kwargs,
    )
    return build_self_calls_dto(request, text, DIRECTORY, PROVENANCE)


class SimultaneousPrincipalsTest(unittest.TestCase):
    """A 与 B 同时存在；撤销 key 只对**本人**可见。"""

    def test_each_principal_sees_only_their_own_rows(self):
        a = _calls_dto("u-1001")
        b = _calls_dto("u-4001")

        self.assertEqual({row["key_id"] for row in a["calls"]}, {"key-a-live"})
        self.assertEqual({row["key_id"] for row in b["calls"]}, {"key-b-live"})

    def test_the_revoked_key_count_is_not_a_cross_uid_side_channel(self):
        """A 有一行已吊销 key 的调用；B **不能**通过任何计数器看到它。

        ``revoked_key_rows`` 在归属判定**之后**累加（principal != subject 的行静默丢弃
        且不计数），所以 B 看到的必须是 0 —— 不是「A 有几行」。
        """
        a = _calls_dto("u-1001")
        b = _calls_dto("u-4001")

        self.assertEqual(a["counts"]["revoked_key_rows"], 1)
        self.assertEqual(b["counts"]["revoked_key_rows"], 0)
        self.assertEqual(b["scope"]["revoked_keys"], [])

    def test_neither_principal_learns_the_other_key_id(self):
        a = _calls_dto("u-1001")
        b = _calls_dto("u-4001")

        b_blob = json.dumps(b, ensure_ascii=False)
        for foreign in ("key-a-live", "key-a-retired", "u-1001", "req-a", "req-retired"):
            with self.subTest(foreign=foreign):
                self.assertNotIn(foreign, b_blob)

        a_blob = json.dumps(a, ensure_ascii=False)
        for foreign in ("key-b-live", "u-4001", "req-b"):
            with self.subTest(foreign=foreign):
                self.assertNotIn(foreign, a_blob)

    def test_a_cannot_request_bs_key_and_b_cannot_request_as_key(self):
        """同一拒绝：不被允许的 key 不因「属于别人」而多出任何信息。"""
        with self.assertRaises(AccessDenied):
            _calls_dto("u-1001", requested_key="key-b-live")
        with self.assertRaises(AccessDenied):
            _calls_dto("u-4001", requested_key="key-a-live")


class OwnerFilteredMetricsTest(unittest.TestCase):
    """p95 / coverage 必须在**授权过滤之后**计算。"""

    OWNER_ROWS = [
        _row("2026-10-14T00:10:00Z", "key-a-live", total_ms=1000, usage={"input_tokens": 10, "output_tokens": 20}),
        _row("2026-10-14T00:20:00Z", "key-a-live", total_ms=1200, usage={"input_tokens": 10, "output_tokens": 20}),
        _row("2026-10-14T00:30:00Z", "key-a-live", total_ms=800, usage={"input_tokens": 10, "output_tokens": 20}),
    ]

    #: 外来行：**项目白名单之外 / 结构上不可归属**，因此不改变任何可见计数。
    #: （同项目里他人的 key 会被项目口径正当计入 —— 那是 §10.1 的有意行为，不能拿来
    #: 当「泄漏」；用它做本测试会得到一条本来就应该变化的 DTO。）
    FOREIGN_ROWS = [
        _row("2026-10-14T00:15:00Z", "key-b-other", total_ms=99999, usage=None, ttft_ms=None),
        _row("2026-10-14T00:25:00Z", "unknown-key-zzz", total_ms=88888, usage=None, ttft_ms=None),
    ]

    def _aggregate(self, rows):
        request = AtlasRequest(
            requester=RequesterContext(
                subject="u-1001",
                roles=frozenset({"user"}),
                scopes=frozenset({SCOPE_USAGE_READ}),
                authorized_projects=frozenset({"proj-a"}),
            ),
            requested_subject=None,
            requested_project="proj-a",
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
            bucket="1h",
        )
        return build_atlas_dto(request, _text(rows), DIRECTORY, PROVENANCE)

    def test_p95_and_sample_count_ignore_foreign_extremes(self):
        clean = self._aggregate(self.OWNER_ROWS)
        polluted = self._aggregate(self.OWNER_ROWS + self.FOREIGN_ROWS)

        self.assertEqual(clean["latency"], polluted["latency"])
        self.assertEqual(clean["latency"]["samples"], 3)
        # 牙齿：若外来行泄漏，p95 会被 99999ms 拉走。
        self.assertLess(clean["latency"]["p95_ms"], 5000)

    def test_coverage_buckets_ignore_foreign_rows(self):
        clean = self._aggregate(self.OWNER_ROWS)
        polluted = self._aggregate(self.OWNER_ROWS + self.FOREIGN_ROWS)

        self.assertEqual(clean["buckets"]["observed_count"], polluted["buckets"]["observed_count"])
        self.assertEqual(clean["quality"], polluted["quality"])
        self.assertEqual(clean["unknown"], polluted["unknown"])

    def test_the_whole_dto_is_byte_identical_after_pollution(self):
        clean = self._aggregate(self.OWNER_ROWS)
        polluted = self._aggregate(self.OWNER_ROWS + self.FOREIGN_ROWS)

        self.assertEqual(
            json.dumps(clean, sort_keys=True),
            json.dumps(polluted, sort_keys=True),
        )


class NullIsNotZeroTest(unittest.TestCase):
    """未测得的指标是 ``null``，不是 ``0``；覆盖率只数实际返回的行。"""

    def test_unmeasured_tokens_and_ttft_stay_null(self):
        text = _text(
            [
                _row("2026-10-14T00:10:00Z", "key-a-live", usage=None, ttft_ms=None, total_ms=None),
            ]
        )
        dto = _calls_dto("u-1001", text=text)

        row = dto["calls"][0]
        self.assertIsNone(row["tokens"]["input"])
        self.assertIsNone(row["tokens"]["output"])
        self.assertIsNone(row["tokens"]["total"])
        self.assertIsNone(row["ttft_ms"])
        self.assertIsNone(row["queue_ms"])
        self.assertIsNone(row["total_ms"])
        self.assertEqual(dto["coverage"]["tokens_unknown_rows"], 1)
        self.assertEqual(dto["coverage"]["ttft_unknown_rows"], 1)

    def test_foreign_rows_do_not_change_the_visible_coverage(self):
        owner = _row("2026-10-14T00:10:00Z", "key-a-live", usage={"input_tokens": 1, "output_tokens": 2})
        foreign = _row("2026-10-14T00:20:00Z", "key-b-live", usage=None, ttft_ms=None, request_id=None)

        alone = _calls_dto("u-1001", text=_text([owner]))
        polluted = _calls_dto("u-1001", text=_text([owner, foreign]))

        self.assertEqual(alone["coverage"], polluted["coverage"])
        self.assertEqual(alone["counts"], polluted["counts"])


class FailClosedSourceTest(unittest.TestCase):
    """来源不可用 = fail closed，不是「空列表 200」。"""

    def test_a_missing_directory_object_is_rejected_not_treated_as_empty(self):
        request = SelfCallsRequest(
            requester=_self_requester("u-1001"),
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
        )
        with self.assertRaises(InvalidRequest):
            build_self_calls_dto(request, CORPUS, None, PROVENANCE)

    def test_a_missing_directory_object_is_rejected_by_the_aggregate_too(self):
        request = AtlasRequest(
            requester=RequesterContext(
                subject="u-1001",
                roles=frozenset({"user"}),
                scopes=frozenset({SCOPE_USAGE_READ}),
                authorized_projects=frozenset({"proj-a"}),
            ),
            requested_subject=None,
            requested_project="proj-a",
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
        )
        with self.assertRaises(InvalidRequest):
            build_atlas_dto(request, CORPUS, None, PROVENANCE)

    def test_a_corpus_that_disappears_after_assembly_raises(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        support.write_file(root / "directory.json", support.dumps(support.directory_document()))
        corpus = support.write_file(root / "usage.jsonl", CORPUS)

        source = build_atlas_source(
            directory_path=root / "directory.json",
            directory_root=root,
            corpus_path=corpus,
            corpus_root=root,
            provenance=PROVENANCE,
        )
        self.assertTrue(source.available)

        corpus.unlink()

        # 装配时的探活已通过；请求时来源消失必须是**抛错**，不是空串。
        with self.assertRaises(SourceUnavailable):
            source.gateway_text(None)
        self.assertFalse(source.gateway_text.read().available)

    def test_an_unavailable_source_exposes_no_callable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        support.write_file(root / "directory.json", support.dumps(support.directory_document()))

        source = build_atlas_source(
            directory_path=root / "directory.json",
            directory_root=root,
            corpus_path=root / "missing.jsonl",
            corpus_root=root,
            provenance=PROVENANCE,
        )

        self.assertFalse(source.available)
        self.assertIsNone(source.gateway_text)
        self.assertIsNone(source.directory)


class ForgedIdentitySurfaceTest(unittest.TestCase):
    """伪造 UID / owner / token 查询必须在**接口形状**上就不可能。"""

    FORBIDDEN = {"uid", "owner", "token", "token_id", "principal", "subject", "user", "role"}

    def test_the_calls_request_dataclass_has_no_identity_query_fields(self):
        from dataclasses import fields

        names = {field.name for field in fields(SelfCallsRequest)}
        self.assertEqual(names & self.FORBIDDEN, set())

    def test_the_aggregate_request_dataclass_has_no_identity_query_fields(self):
        from dataclasses import fields

        names = {field.name for field in fields(AtlasRequest)}
        # ``requested_subject`` 是**收窄**参数，且必须与会话主体相等；它不是身份来源。
        self.assertEqual(names & self.FORBIDDEN, set())

    def test_a_forged_requested_subject_is_denied(self):
        request = AtlasRequest(
            requester=RequesterContext(
                subject="u-1001",
                roles=frozenset({"user"}),
                scopes=frozenset({SCOPE_USAGE_READ}),
                authorized_projects=frozenset({"proj-a"}),
            ),
            requested_subject="u-4001",
            requested_project="proj-a",
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
        )
        with self.assertRaises(AccessDenied):
            build_atlas_dto(request, CORPUS, DIRECTORY, PROVENANCE)

    def test_self_reported_identity_fields_in_the_corpus_have_zero_effect(self):
        clean = _row("2026-10-14T00:10:00Z", "key-a-live", request_id="req-a")
        forged = _row(
            "2026-10-14T00:10:00Z",
            "key-a-live",
            request_id="req-a",
            uid="u-4001",
            owner="u-4001",
            token="forged-token",
            principal="u-4001",
            x_principal="u-4001",
            **{"X-Forwarded-User": "u-4001"},
        )

        clean_dto = _calls_dto("u-1001", text=_text([clean]))
        forged_dto = _calls_dto("u-1001", text=_text([forged]))

        self.assertEqual(
            json.dumps(clean_dto, sort_keys=True),
            json.dumps(forged_dto, sort_keys=True),
        )
        self.assertNotIn("forged-token", json.dumps(forged_dto))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
