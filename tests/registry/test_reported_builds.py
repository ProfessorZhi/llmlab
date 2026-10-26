"""上报里的「装着的引擎」→ 账户名下的 ``runtime_builds`` 行。

这里钉的是落成规则（校验在 ``test_agent_reports.py``）：

* 所有者是账户（``account:<id>``，``owner_uid=-1``），不是 linux 主体；
* 身份是 ``(framework, 可执行文件路径)``：同一条路径换了版本**还是**同一行；
* 观测值随每轮刷新，但一轮没读到版本不能把上一轮的版本抹掉；
* 不标 missing —— 一个账户可能不止一个 agent 在报（见模块文档）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.registry.reported_builds import (
    BuildAdoption,
    adopt_reported_builds,
    build_fingerprint,
)
from backend.registry.reported_models import ACCOUNT_OWNER_UID
from backend.registry.store import Store

OWNER = "account:0123456789abcdef0123456789abcdef"
OTHER = "account:fedcba9876543210fedcba9876543210"
NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)

LLAMA = {
    "engine": "llama.cpp",
    "name": "llama.cpp",
    "found_as": "executable",
    "version": "b4120",
    "path": "/usr/local/bin/llama-server",
    "env": None,
}
VLLM = {
    "engine": "vllm",
    "name": "vLLM",
    "found_as": "python_package",
    "version": "0.6.3",
    "path": "/opt/vllm/.venv/lib/python3.12/site-packages/vllm-0.6.3.dist-info",
    "env": "/opt/vllm/.venv",
}


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def adopt(self, entries, *, owner=OWNER, now=NOW):
        return adopt_reported_builds(self.store, owner=owner, builds=entries, now=now)


class FingerprintTests(unittest.TestCase):
    def test_the_fingerprint_covers_framework_path_and_version(self) -> None:
        base = build_fingerprint("llama.cpp", "/usr/local/bin/llama-server", "b4120")

        self.assertEqual(base, build_fingerprint("llama.cpp", "/usr/local/bin/llama-server", "b4120"))
        self.assertNotEqual(base, build_fingerprint("llama.cpp", "/usr/local/bin/llama-server", "b4200"))
        self.assertNotEqual(base, build_fingerprint("llama.cpp", "/opt/bin/llama-server", "b4120"))
        self.assertEqual(len(base), 32)


class AdoptionTests(StoreFixture):
    def test_an_installed_engine_becomes_a_build_row_owned_by_the_account(self) -> None:
        adoption = self.adopt([LLAMA])

        (row,) = self.store.list_builds()
        self.assertEqual(row.owner_principal, OWNER)
        self.assertEqual(row.owner_uid, ACCOUNT_OWNER_UID)
        self.assertEqual(row.framework, "llama.cpp")
        self.assertEqual(row.binary_path, LLAMA["path"])
        self.assertEqual(row.version, "b4120")
        self.assertEqual(row.found_as, "executable")
        self.assertEqual(row.state, "present")
        self.assertEqual(row.last_seen_at, NOW)
        self.assertEqual(adoption.adopted, {LLAMA["path"]: row.id})
        self.assertEqual(adoption.created, (row.id,))

    def test_a_python_package_keeps_the_venv_it_was_found_in(self) -> None:
        self.adopt([VLLM])

        (row,) = self.store.list_builds()
        self.assertEqual(row.found_as, "python_package")
        self.assertEqual(row.env, "/opt/vllm/.venv")

    def test_the_same_report_twice_creates_one_row(self) -> None:
        first = self.adopt([LLAMA])
        second = self.adopt([LLAMA], now=LATER)

        self.assertEqual(len(self.store.list_builds()), 1)
        self.assertEqual(len(first.created), 1)
        self.assertEqual(second.created, ())
        self.assertTrue(second, "第二轮仍然认得这条路径")

    def test_a_new_version_at_the_same_path_is_the_same_build(self) -> None:
        """路径是身份：同一个文件换了版本，不是「你装了两份 llama.cpp」。"""

        self.adopt([LLAMA])
        self.adopt([{**LLAMA, "version": "b4200"}], now=LATER)

        (row,) = self.store.list_builds()
        self.assertEqual(row.version, "b4200")
        self.assertEqual(row.last_seen_at, LATER)
        self.assertEqual(
            row.fingerprint,
            build_fingerprint("llama.cpp", LLAMA["path"], "b4200"),
            "指纹跟着观测值走，否则「换了没换」这个问题它答不了",
        )

    def test_a_round_that_could_not_read_the_version_does_not_erase_it(self) -> None:
        """可执行文件的版本号探测不到是常态（扫描器不执行它找到的东西）。"""

        self.adopt([LLAMA])
        self.adopt([{**LLAMA, "version": None}], now=LATER)

        (row,) = self.store.list_builds()
        self.assertEqual(row.version, "b4120")

    def test_every_round_refreshes_last_seen_at_including_a_quiet_one(self) -> None:
        self.adopt([LLAMA])
        self.adopt([LLAMA], now=LATER)

        (row,) = self.store.list_builds()
        self.assertEqual(row.last_seen_at, LATER)

    def test_one_path_is_one_build_even_if_two_frameworks_claim_it(self) -> None:
        """一个文件就是一个 build：两种框架都说「这是我的」，是报告自相矛盾。

        取先报的那个，并且只入账一行 —— 与其让库里同一路径出现两行、谁也说不清
        哪行才是「装着的那个」，不如让矛盾在入账这一步就被裁掉。
        """

        adoption = self.adopt([LLAMA, {**LLAMA, "engine": "ollama"}])

        (row,) = self.store.list_builds()
        self.assertEqual(row.framework, "llama.cpp")
        self.assertEqual(len(adoption.adopted), 1, "报告里一条路径只对应一条入账结果")

    def test_another_account_never_borrows_my_build(self) -> None:
        self.adopt([LLAMA], owner=OTHER)
        mine = self.adopt([LLAMA])

        self.assertEqual(len(self.store.builds_for_owner(OTHER)), 1)
        self.assertEqual(len(self.store.builds_for_owner(OWNER)), 1)
        self.assertNotEqual(
            self.store.builds_for_owner(OWNER)[0].id,
            self.store.builds_for_owner(OTHER)[0].id,
        )
        self.assertEqual(len(mine.created), 1)

    def test_a_row_is_never_marked_missing_by_a_later_report(self) -> None:
        """甲机器没报某个 build，不证明它被卸了 —— 一台账户下可能不止一个 agent。"""

        self.adopt([LLAMA, VLLM])
        self.adopt([LLAMA], now=LATER)  # 这一轮里 vLLM 不见了

        states = {row.framework: row.state for row in self.store.list_builds()}
        self.assertEqual(states, {"llama.cpp": "present", "vllm": "present"})

    def test_a_malformed_entry_is_skipped_without_losing_the_rest(self) -> None:
        adoption = self.adopt([
            "nope",
            {"engine": "llama.cpp", "path": "relative/bin"},
            {"engine": None, "path": "/x"},
            {"engine": "llama.cpp", "path": "/x\x00y"},
            VLLM,
        ])

        self.assertEqual(len(adoption.created), 1)
        (row,) = self.store.list_builds()
        self.assertEqual(row.framework, "vllm")

    def test_an_unknown_found_as_is_dropped_rather_than_stored_as_is(self) -> None:
        self.adopt([{**LLAMA, "found_as": "flatpak"}])

        (row,) = self.store.list_builds()
        self.assertIsNone(row.found_as)

    def test_no_owner_means_no_rows(self) -> None:
        """linux 用户没有平台账户时，谁的名下都不该出现这几行。"""

        adoption = self.adopt([LLAMA], owner="")

        self.assertEqual(adoption, BuildAdoption())
        self.assertEqual(self.store.list_builds(), [])


class SchemaTests(unittest.TestCase):
    def test_the_two_observation_columns_survive_a_round_trip(self) -> None:
        store = Store(":memory:")
        self.addCleanup(store.close)
        adopt_reported_builds(store, owner=OWNER, builds=[VLLM], now=NOW)

        (row,) = store.builds_for_owner(OWNER)
        self.assertEqual((row.found_as, row.env), ("python_package", "/opt/vllm/.venv"))


if __name__ == "__main__":
    unittest.main()
