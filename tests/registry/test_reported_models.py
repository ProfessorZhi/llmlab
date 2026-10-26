"""上报里的模型条目 → 账户名下的 ``models`` 行。

这里测的是**落成规则**，不是上报的校验（那是 ``test_agent_reports.py`` 的事）：

* 所有者是账户（``account:<id>``，``owner_uid=-1``），不是 linux 主体；
* 行里带 ``weight_path`` / ``format`` / ``size_bytes``，但**只是引用**；
* 同名或同路径已存在时沿用那一行：自动登记只补空，不覆盖；
* 别的主体的行不会因为路径相同而被借走。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.registry.reported_models import (
    ACCOUNT_OWNER_UID,
    adopt_reported_models,
    reported_name,
)
from backend.registry.store import Store

OWNER = "account:0123456789abcdef0123456789abcdef"
OTHER = "account:fedcba9876543210fedcba9876543210"
NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)

GGUF = {
    "path": "/home/alice/models/gpt-oss-120b-Q4.gguf",
    "format": "gguf",
    "size_bytes": 63_400_000_000,
    "suggested_name": "gpt-oss-120b-Q4",
    "architecture": "gpt-oss",
    "model_type": None,
}


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def adopt(self, entries, *, owner=OWNER):
        return adopt_reported_models(self.store, owner=owner, models=entries, now=NOW)


class NamingTests(unittest.TestCase):
    def test_a_suggested_name_is_sanitized_even_though_the_validator_bounded_it(self) -> None:
        """校验器只截长度、去控制字符；名称会进 URL，分隔符必须在这里换掉。"""

        self.assertEqual(
            reported_name("/home/alice/m.gguf", "Qwen/Qwen3-4B"), "Qwen-Qwen3-4B"
        )

    def test_a_missing_suggested_name_falls_back_to_the_file_stem(self) -> None:
        self.assertEqual(reported_name("/home/alice/decider-v2.1-Q8_0.gguf", None), "decider-v2.1-Q8_0")

    def test_a_name_that_cannot_be_salvaged_is_refused_not_guessed(self) -> None:
        # 文件名只有扩展名之外的空壳：没有名字的行在库里搜不到、也打不开。
        self.assertIsNone(reported_name("/home/alice/.gguf", "///"))


class AdoptionTests(StoreFixture):
    def test_a_reported_file_becomes_a_model_row_owned_by_the_account(self) -> None:
        adoption = self.adopt([GGUF])

        rows = self.store.list_models()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.owner_principal, OWNER)
        self.assertEqual(row.owner_uid, ACCOUNT_OWNER_UID)
        self.assertEqual(row.name, "gpt-oss-120b-Q4")
        self.assertEqual(row.weight_path, GGUF["path"])
        self.assertEqual(row.format, "gguf")
        self.assertEqual(row.size_bytes, GGUF["size_bytes"])
        self.assertEqual(row.source, "auto")
        self.assertEqual(adoption.adopted, {GGUF["path"]: row.id})
        self.assertEqual(adoption.created, (row.id,))

    def test_the_row_says_it_is_a_reference_the_platform_never_opens(self) -> None:
        self.adopt([GGUF])
        row = self.store.list_models()[0]

        self.assertEqual(row.health["state"], "unknown")
        self.assertIn("never opened", row.health["reason"])
        self.assertIn("不打开", row.description or "")

    def test_the_same_report_adopts_nothing_new_the_second_time(self) -> None:
        first = self.adopt([GGUF])
        second = self.adopt([GGUF])

        self.assertEqual(len(self.store.list_models()), 1)
        self.assertEqual(second.adopted, first.adopted)
        self.assertEqual(second.created, ())

    def test_a_name_already_in_the_library_is_reused_not_duplicated(self) -> None:
        """库里有同名的一行（哪怕它没有路径）时，再建一行只是同一件事说两遍。"""

        once = self.adopt([GGUF])
        self.store.list_models()  # 触发一次读取，确认库里有且只有一行
        # 换一条路径、同一个建议名：新路径不会被单独登记。
        again = self.adopt([{**GGUF, "path": "/home/alice/models/copy.gguf"}])

        self.assertEqual(len(self.store.list_models()), 1)
        self.assertEqual(again.created, ())
        self.assertEqual(again.adopted["/home/alice/models/copy.gguf"], once.created[0])

    def test_another_owners_row_with_the_same_path_does_not_hide_the_file(self) -> None:
        self.adopt([GGUF], owner=OWNER)
        adoption = self.adopt([GGUF], owner=OTHER)

        self.assertEqual(len(self.store.list_models()), 2)
        self.assertEqual(len(adoption.created), 1)
        mine = self.store.model_by_owner_name(OTHER, "gpt-oss-120b-Q4")
        self.assertIsNotNone(mine)

    def test_entries_that_cannot_be_stored_are_skipped_one_by_one(self) -> None:
        adoption = self.adopt(
            [
                {"path": "relative/m.gguf", "suggested_name": "relative"},
                "not a dict",
                GGUF,
            ]
        )

        self.assertEqual(len(self.store.list_models()), 1)
        self.assertEqual(list(adoption.adopted), [GGUF["path"]])

    def test_a_size_that_is_not_a_count_is_left_unset(self) -> None:
        self.adopt(
            [
                {**GGUF, "size_bytes": True},
                {
                    **GGUF,
                    "path": "/home/alice/models/two.gguf",
                    "suggested_name": "two",
                    "size_bytes": -1,
                },
            ]
        )

        self.assertEqual([row.size_bytes for row in self.store.list_models()], [None, None])

    def test_no_owner_means_nothing_is_written(self) -> None:
        self.assertEqual(self.adopt([GGUF], owner="").adopted, {})
        self.assertEqual(self.store.list_models(), [])


if __name__ == "__main__":
    unittest.main()
