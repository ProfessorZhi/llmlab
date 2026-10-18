"""重复 ``request_id``（Issue #77 必测清单）。

上游签发的 ``request_id`` 是**不透明句柄**，它不保证唯一 —— 代理重试、双写、
导出被读两遍都会让同一个 id 出现在多行上。本文件的用例锁定的是「遇到重复时
逐条视图怎么答」，而答案是：**每一条观测仍然是独立的一行**。

刻意**不**做的事：按 ``request_id`` 去重。去重听起来像是「清理」，实际是把
「上游交付了两次」这条事实删掉 —— 而那正是运维需要看见的东西。``call_id``
（派生值）负责让 DOM key 保持唯一，``request_id``（上游原值）负责对账。

**failing-before 证据。** 本文件不是特性固化，它抓到了一个真实缺陷：

``test_field_identical_observations_are_both_kept_with_unique_call_ids`` 在修复前
以 ``ContractError: calls must be strictly ordered newest-first`` 告败 —— 也就是说
**语料里只要出现两行逐字段相同的观测，``GET /api/atlas/calls`` 就整条 500**。

根因是两个键打架：排序用「基础摘要」，而出口自检用「补过序号的 id」，补序号只会把
id 变大，于是同一时刻的两行让自检读到一串**递增**的游标。修复见 ``calls.py`` 中
「先给每条观测定下一个唯一 id，再拿这个 id 当排序键」那一段。同一处还修掉了游标
不自洽（``next_before.call_id`` 是补过号的、过滤却用未补号的），即翻页会漏行/重行。
"""

from __future__ import annotations

import unittest

from tests.atlas import support

from backend.atlas.calls import SelfCallsRequest, build_self_calls_dto

WINDOW_FROM = "2026-10-14T00:00:00Z"
WINDOW_TO = "2026-10-14T04:00:00Z"


def _row(ts: str, request_id: str | None, *, model: str = "m-a", status: int = 200) -> str:
    import json

    return json.dumps(
        {
            "ts": ts,
            "key_id": "key-alpha",
            "request_id": request_id,
            "model": model,
            "status": status,
            "outcome": "success" if status < 400 else "failed",
            "stream": True,
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "total_ms": 100,
        },
        sort_keys=True,
    )


class DuplicateRequestIdTest(unittest.TestCase):
    def setUp(self):
        from datetime import datetime

        from backend.atlas.identity import SCOPE_CALLS_SELF_READ, RequesterContext

        self._parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.directory = support.directory_from_document()
        self.requester = RequesterContext(
            subject="u-1001",
            roles=frozenset({"user"}),
            scopes=frozenset({SCOPE_CALLS_SELF_READ}),
            authorized_projects=frozenset({"proj-a"}),
        )

    def _dto(self, lines: list[str]) -> dict:
        request = SelfCallsRequest(
            requester=self.requester,
            window_from=self._parse(WINDOW_FROM),
            window_to=self._parse(WINDOW_TO),
            limit=50,
        )
        return build_self_calls_dto(
            request,
            "\n".join(lines) + "\n",
            self.directory,
            support.SYNTHETIC_PROVENANCE,
        )

    def test_the_same_request_id_on_different_observations_yields_two_rows(self):
        dto = self._dto(
            [_row("2026-10-14T00:10:00Z", "req-dup"), _row("2026-10-14T00:20:00Z", "req-dup")]
        )

        self.assertEqual(dto["counts"]["returned"], 2)
        self.assertEqual([row["request_id"] for row in dto["calls"]], ["req-dup", "req-dup"])
        # 两条观测 -> 两个不同的派生 id，前端才拿得到唯一的 DOM key。
        self.assertEqual(len({row["call_id"] for row in dto["calls"]}), 2)

    def test_field_identical_observations_are_both_kept_with_unique_call_ids(self):
        """逐字段完全相同的两行：仍然两行，``call_id`` 靠后缀区分。

        去重会把「上游把同一份数据交付了两次」抹掉；这里选择如实保留。
        """
        identical = _row("2026-10-14T00:40:00Z", "req-same")

        dto = self._dto([identical, identical])

        self.assertEqual(dto["counts"]["returned"], 2)
        call_ids = [row["call_id"] for row in dto["calls"]]
        self.assertEqual(len(set(call_ids)), 2)

    def test_a_duplicate_request_id_never_rewrites_the_upstream_value(self):
        """派生值绝不能覆盖上游原值 —— 对账时两边必须对得上。"""
        dto = self._dto(
            [_row("2026-10-14T00:10:00Z", "req-dup"), _row("2026-10-14T00:20:00Z", "req-dup")]
        )

        for row in dto["calls"]:
            self.assertEqual(row["request_id"], "req-dup")
            self.assertEqual(row["call_id_basis"], "derived-from-metadata-fields")
            self.assertNotEqual(row["call_id"], row["request_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
