"""S2 验收：假引擎起来 ⇒ 账本里出现；停掉 ⇒ 两轮后 offline 且行还在。

这一层用真的 Store（临时 SQLite 文件）与真的状态机，只把两件外部事物换掉：
``/proc/net/tcp`` 的表和 HTTP 探测。这样测到的是「观测 → 账本」整条路径，
而不是某一段的替身。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from backend.registry import api
from backend.registry.identity import Principal, PrincipalDirectory
from backend.registry.ingest_runner import IngestRunner
from backend.registry.lab_settings import ALL, PRIVATE, set_default_visibility
from backend.registry.models import (
    ENGINE_RUNTIME_ATTACHED,
    ENGINE_RUNTIME_OFFLINE,
    SOURCE_AUTO,
)
from backend.registry.observed_services import (
    auto_engine_id,
    observed_engines,
    parse_auto_engine_id,
)
from backend.registry.ownership import ListeningSocket
from backend.registry.store import Store

ALICE_UID = 1001


class FakeTable:
    def __init__(self) -> None:
        self.rows: list[ListeningSocket] = []

    def listeners(self):
        return list(self.rows)

    def listen(self, port: int, uid: int = ALICE_UID, ip: str = "127.0.0.1") -> None:
        self.rows.append(ListeningSocket(ip=ip, port=port, uid=uid, inode=port))

    def stop(self, port: int) -> None:
        self.rows = [row for row in self.rows if row.port != port]


class FakeFetch:
    """按端口模拟 OpenAI 兼容服务；``down`` 里的端口一律连不上。"""

    def __init__(self) -> None:
        self.models: dict[int, list[str]] = {}
        self.down: set[int] = set()

    def __call__(self, port: int, path: str):
        if port in self.down or port not in self.models:
            return None
        if path == "/v1/models":
            body = json.dumps({"data": [{"id": m} for m in self.models[port]]}).encode()
            return 200, body
        return 404, b""


class RunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "registry.sqlite3")
        self.addCleanup(self.store.close)

        self.directory = PrincipalDirectory(
            [Principal("alice", "alice", ALICE_UID), Principal("bob", "bob", 1002)]
        )
        self.table = FakeTable()
        self.fetch = FakeFetch()
        self.runner = IngestRunner(
            store=self.store,
            directory=self.directory,
            table=self.table,
            fetch=self.fetch,
        )

    def serve(self, port: int = 8013, models=("qwen3-4b",), uid: int = ALICE_UID) -> None:
        self.fetch.models[port] = list(models)
        self.table.listen(port, uid=uid)

    def engine(self, port: int = 8013, uid: int = ALICE_UID):
        return self.store.engine_by_id(auto_engine_id(uid, port))


class AttachTests(RunnerTestCase):
    def test_a_running_service_appears_in_the_ledger_after_one_round(self):
        self.serve()

        round_result = self.runner.tick()

        self.assertTrue(round_result.of_kind("attach"))
        engine = self.engine()
        self.assertIsNotNone(engine)
        self.assertEqual(engine.source, SOURCE_AUTO)
        self.assertEqual(engine.state, ENGINE_RUNTIME_ATTACHED)
        self.assertEqual(engine.owner_principal, "alice")
        self.assertEqual(engine.target["url"], "http://127.0.0.1:8013")
        self.assertEqual(engine.launch["model_ids"], ["qwen3-4b"])

    def test_the_generated_id_is_addressable_by_the_http_route(self):
        """列表里看得见、却打不开详情的资源，比看不见更糟。

        自动 id 必须满足 ``api._ID`` 的语法（``[A-Za-z0-9_-]{1,64}``）；原来的
        ``auto:<uid>:<port>`` 带冒号，``GET /engines/auto:1000:18123`` 直接 404。
        """

        pattern = re.compile(rf"^{api._ID}$")

        engine_id = auto_engine_id(ALICE_UID, 18123)

        self.assertRegex(engine_id, pattern)
        self.assertEqual(parse_auto_engine_id(engine_id), (ALICE_UID, 18123))
        self.assertIsNone(parse_auto_engine_id("eng_deadbeefdeadbeef"))

    def test_the_second_round_does_not_create_a_second_row(self):
        self.serve()
        self.runner.tick()
        first = self.engine()

        self.runner.tick()

        self.assertEqual(len(observed_engines(self.store)), 1)
        self.assertEqual(self.engine().id, first.id)

    def test_a_port_owned_by_an_unmapped_uid_never_reaches_the_ledger(self):
        self.serve(uid=4242)

        round_result = self.runner.tick()

        self.assertTrue(round_result.of_kind("ignore"))
        self.assertEqual(observed_engines(self.store), [])

    def test_a_service_that_never_looks_compatible_is_dropped_without_a_row(self):
        self.table.listen(9000)  # 没有模型、fetch 一律返回 None

        for _ in range(3):
            round_result = self.runner.tick()

        self.assertTrue(round_result.of_kind("drop"))
        self.assertIsNone(self.engine(9000))
        self.assertEqual(self.runner.tracked, {})


class LifecycleTests(RunnerTestCase):
    def test_stopping_the_service_takes_two_rounds_to_go_offline(self):
        self.serve()
        self.runner.tick()

        self.runner.tick()
        self.assertEqual(self.engine().state, ENGINE_RUNTIME_ATTACHED)

        self.table.stop(8013)
        self.runner.tick()
        self.assertEqual(self.engine().state, ENGINE_RUNTIME_ATTACHED, "一轮缺席不该下线")

        self.runner.tick()
        offline = self.engine()
        self.assertEqual(offline.state, ENGINE_RUNTIME_OFFLINE)
        self.assertEqual(offline.id, auto_engine_id(ALICE_UID, 8013), "行不能被删掉")

    def test_coming_back_restores_the_same_row(self):
        self.serve()
        self.runner.tick()
        self.table.stop(8013)
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(self.engine().state, ENGINE_RUNTIME_OFFLINE)

        self.table.listen(8013)
        round_result = self.runner.tick()

        self.assertTrue(round_result.of_kind("recover"))
        self.assertEqual(self.engine().state, ENGINE_RUNTIME_ATTACHED)
        self.assertEqual(len(observed_engines(self.store)), 1)

    def test_the_visibility_default_is_snapshotted_at_attach_time(self):
        """§4.2：默认值只影响之后接入的服务。

        管理员把默认值改成共享，不应该让昨天接入的服务也变成共享；反过来，
        接入之后再改默认值，也不该改写已经接入那条的快照。
        """

        set_default_visibility(self.store, PRIVATE, updated_by="admin")
        self.serve()
        self.runner.tick()
        self.assertEqual(self.engine().launch["visibility"], PRIVATE)

        set_default_visibility(self.store, ALL, updated_by="admin")
        self.serve(port=8014)
        self.runner.tick()

        self.assertEqual(self.engine(8013).launch["visibility"], PRIVATE)
        self.assertEqual(self.engine(8014).launch["visibility"], ALL)

    def test_a_daemon_restart_does_not_rewrite_an_old_snapshot(self):
        """重启后 tracked 清空，旧服务会再走一次 ATTACH；快照必须原样保留。"""

        set_default_visibility(self.store, PRIVATE, updated_by="admin")
        self.serve()
        self.runner.tick()
        set_default_visibility(self.store, ALL, updated_by="admin")

        restarted = IngestRunner(
            store=self.store,
            directory=self.directory,
            table=self.table,
            fetch=self.fetch,
        )
        round_result = restarted.tick()

        self.assertTrue(round_result.of_kind("attach"))
        self.assertEqual(self.engine().launch["visibility"], PRIVATE)
        self.assertEqual(len(observed_engines(self.store)), 1)


class CooldownTests(RunnerTestCase):
    def test_a_port_that_is_not_an_engine_is_not_probed_forever(self):
        """丢弃只是忘掉它；端口还在，下一轮就又被当成新端口探三次。

        开发机上这样的端口不少（API 自己、ssh 转发口、任何 HTTP 服务）。不冷却
        的话，探测会以「每轮三次」的节奏永远打下去。
        """

        self.table.listen(9000)
        probed: list[int] = []

        def counting_fetch(port: int, _path: str):
            probed.append(port)
            return None

        self.runner.fetch = counting_fetch
        for _ in range(3):
            self.runner.tick()
        self.assertIn(9000, probed)
        after_drop = len(probed)

        self.runner.tick()

        self.assertEqual(len(probed), after_drop, "冷却期内不该再敲这个端口")
        self.assertGreater(self.runner.cooldowns.get("1001:9000", 0), 0)

    def test_a_cooldown_ends_and_the_port_is_tried_again(self):
        """退避不是拉黑：判定错了（服务起得慢）还得能翻案。"""

        self.table.listen(9000)
        self.runner.fetch = lambda _port, _path: None
        for _ in range(3):
            self.runner.tick()

        for _ in range(5):
            self.runner.tick()
        self.runner.fetch = self.fetch
        self.serve(port=9000)

        for _ in range(4):
            self.runner.tick()

        self.assertIsNotNone(self.engine(9000))
        self.assertNotIn("1001:9000", self.runner.cooldowns)


class BudgetTests(RunnerTestCase):
    def test_the_probe_budget_defers_work_instead_of_stalling_the_round(self):
        for port in range(8000, 8004):
            self.table.listen(port)
        clock = {"now": 0.0}

        self.runner.monotonic = lambda: clock["now"]
        self.runner.probe_budget_seconds = 0
        round_result = self.runner.tick()

        self.assertGreater(self.runner.deferred_probes, 0)
        # 没探测 ≠ 探测失败：这些端口仍是 seen，失败次数为 0。
        self.assertEqual(round_result.of_kind("drop"), [])
        self.assertTrue(all(s.probe_failures == 0 for s in self.runner.tracked.values()))
        self.assertEqual(observed_engines(self.store), [])


if __name__ == "__main__":
    unittest.main()
