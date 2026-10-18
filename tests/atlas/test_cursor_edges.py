"""Keyset 游标的两个边界：**亚秒时间戳**与**重复 id**。

Lane #77 Wave-5 要求的第一条就是「keyset cursor under duplicate IDs」。既有用例
（``tests/backend/test_self_calls.py::test_paging_visits_every_row_exactly_once``）只覆盖
「同一秒、不同 id」，没有覆盖：

1. **同一秒内的亚秒次序**。``next_before.ts`` 若只写到秒，游标就会落在最后一行的
   **前面**，下一页把「同一秒里更早的那些行」一起跳过 —— 这是真的丢数据，不是理论问题：
   ``ingest`` 接受带小数秒的 ISO-8601，而 ``_iso_z`` 只格式化到秒。
2. **逐字段相同的重复行**。它们的派生 id 需要唯一后缀；游标必须用同一个（后补号的）
   值来过滤，否则翻页会漏行或重复。

本文件锁定的判据只有一个，和既有翻页用例同构：**每一行恰好访问一次**。

**failing-before 证据（本分支实测）。** 修复前
``SubSecondCursorTest`` 的两个用例都以
``ContractError: calls must be strictly ordered newest-first`` 告败 —— 不是翻页测试
断言失败，而是**第一页就整条 500**：排序用真实时刻、出口自检用截断到秒的 ``ts``，
同一秒内的两行只要派生 id 的字典序是升的，自检就认为「没有严格递减」。

修复 = ``calls.py::_iso_z`` 保留小数秒（秒精度输入输出不变）+ 游标与排序共用真实时刻。
把 ``_iso_z`` 改回 ``strftime("%Y-%m-%dT%H:%M:%SZ")`` 可让本文件重新变红。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from tests.atlas import support

from backend.atlas.calls import SelfCallsRequest, build_self_calls_dto
from backend.atlas.identity import SCOPE_CALLS_SELF_READ, RequesterContext

WINDOW_FROM = datetime.fromisoformat("2026-10-14T00:00:00+00:00")
WINDOW_TO = datetime.fromisoformat("2026-10-14T04:00:00+00:00")


def _row(ts: str, *, request_id: str | None = None, total_ms: int = 100) -> str:
    return json.dumps(
        {
            "ts": ts,
            "key_id": "key-alpha",
            "request_id": request_id,
            "model": "m-a",
            "status": 200,
            "outcome": "success",
            "stream": True,
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "total_ms": total_ms,
        },
        sort_keys=True,
    )


class _Pager:
    def __init__(self) -> None:
        self.directory = support.directory_from_document()
        self.requester = RequesterContext(
            subject="u-1001",
            roles=frozenset({"user"}),
            scopes=frozenset({SCOPE_CALLS_SELF_READ}),
            authorized_projects=frozenset({"proj-a"}),
        )

    def page(self, text: str, *, limit: int, cursor: dict | None = None) -> dict:
        request = SelfCallsRequest(
            requester=self.requester,
            window_from=WINDOW_FROM,
            window_to=WINDOW_TO,
            limit=limit,
            before_ts=(
                datetime.fromisoformat(cursor["ts"].replace("Z", "+00:00"))
                if cursor
                else None
            ),
            before_call_id=cursor["call_id"] if cursor else None,
        )
        return build_self_calls_dto(
            request, text, self.directory, support.SYNTHETIC_PROVENANCE
        )

    def walk(self, text: str, *, limit: int) -> list[str]:
        seen: list[str] = []
        cursor: dict | None = None
        for _ in range(20):
            dto = self.page(text, limit=limit, cursor=cursor)
            seen.extend(row["call_id"] for row in dto["calls"])
            if not dto["counts"]["truncated"]:
                return seen
            cursor = dto["counts"]["next_before"]
        raise AssertionError("pagination did not terminate")


class SubSecondCursorTest(unittest.TestCase):
    """游标必须保留亚秒精度，否则同一秒里的行会被整段跳过。"""

    def setUp(self) -> None:
        self.pager = _Pager()
        # 全部落在同一秒内，按亚秒降序：.900 > .500 > .100 > .000（.000 在前一秒）
        self.lines = [
            _row("2026-10-14T00:00:02.900000Z", request_id="req-a"),
            _row("2026-10-14T00:00:02.500000Z", request_id="req-b"),
            _row("2026-10-14T00:00:02.100000Z", request_id="req-c"),
            _row("2026-10-14T00:00:01.000000Z", request_id="req-d"),
        ]
        self.text = "\n".join(self.lines) + "\n"

    def test_a_page_boundary_inside_a_second_loses_no_row(self):
        seen = self.pager.walk(self.text, limit=2)

        self.assertEqual(len(seen), 4, f"lost rows: {seen}")
        self.assertEqual(len(set(seen)), 4, f"duplicated rows: {seen}")

    def test_the_cursor_round_trips_the_exact_boundary(self):
        first = self.pager.page(self.text, limit=2)
        boundary = first["counts"]["next_before"]["ts"]

        # 游标必须能表达最后一行的真实时刻；截断到秒会让下一页从错误的位置继续。
        self.assertEqual(
            datetime.fromisoformat(boundary.replace("Z", "+00:00")),
            datetime.fromisoformat("2026-10-14T00:00:02.500000+00:00"),
        )


class DuplicateIdCursorTest(unittest.TestCase):
    """逐字段相同的重复行：唯一后缀 + 同一个游标值，翻页仍然不漏不重。"""

    def setUp(self) -> None:
        self.pager = _Pager()
        duplicate = _row("2026-10-14T00:00:03.000000Z", request_id="req-dup")
        self.lines = [
            _row("2026-10-14T00:00:05.000000Z", request_id="req-top"),
            duplicate,
            duplicate,
            duplicate,
            _row("2026-10-14T00:00:01.000000Z", request_id="req-bottom"),
        ]
        self.text = "\n".join(self.lines) + "\n"

    def test_every_duplicate_is_visited_exactly_once(self):
        seen = self.pager.walk(self.text, limit=2)

        self.assertEqual(len(seen), 5, f"lost rows: {seen}")
        self.assertEqual(len(set(seen)), 5, f"duplicated rows: {seen}")

    def test_duplicates_are_not_collapsed(self):
        dto = self.pager.page(self.text, limit=50)

        self.assertEqual(dto["counts"]["returned"], 5)
        self.assertEqual(
            len([row for row in dto["calls"] if row["request_id"] == "req-dup"]), 3
        )

    def test_call_ids_are_unique_and_ordered_newest_first(self):
        dto = self.pager.page(self.text, limit=50)

        ids = [row["call_id"] for row in dto["calls"]]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(
            [(row["ts"], row["call_id"]) for row in dto["calls"]],
            sorted(((row["ts"], row["call_id"]) for row in dto["calls"]), reverse=True),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
