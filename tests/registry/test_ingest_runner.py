"""S2 验收：假引擎起来 ⇒ 账本里出现；停掉 ⇒ 两轮后 offline 且行还在。

这一层用真的 Store（临时 SQLite 文件）与真的状态机，只把两件外部事物换掉：
``/proc/net/tcp`` 的表和 HTTP 探测。这样测到的是「观测 → 账本」整条路径，
而不是某一段的替身。
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    MODEL_REGISTERED,
    SOURCE_AUTO,
    SOURCE_CLIENT_REGISTERED,
    Model,
)
from backend.registry.observed_services import (
    auto_engine_id,
    observed_engines,
    parse_auto_engine_id,
)
from backend.registry.ownership import ListeningSocket
from backend.registry.store import Store

ALICE_UID = 1001
BOB_UID = 1002


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
            [Principal("alice", "alice", ALICE_UID), Principal("bob", "bob", BOB_UID)]
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


class RouteNameTests(RunnerTestCase):
    """§4.2：路由名 = 模型 id 规范化，冲突时追加 ``@<用户名>``。"""

    def test_a_private_service_does_not_take_a_route_name(self):
        self.serve()
        self.runner.tick()

        route = self.engine().launch["route"]
        self.assertIsNone(route["name"], "private 的服务不需要网关路由")
        self.assertFalse(route["shared"])

    def test_a_shared_service_gets_the_normalized_model_id(self):
        set_default_visibility(self.store, ALL, updated_by="admin")
        self.serve()

        self.runner.tick()

        route = self.engine().launch["route"]
        self.assertEqual(route["name"], "qwen3-4b")
        self.assertTrue(route["shared"])

    def test_two_users_serving_the_same_model_get_distinct_route_names(self):
        set_default_visibility(self.store, ALL, updated_by="admin")
        self.serve(port=8013, uid=ALICE_UID)
        self.serve(port=8014, uid=BOB_UID)

        self.runner.tick()

        self.assertEqual(self.engine(8013).launch["route"]["name"], "qwen3-4b")
        self.assertEqual(self.engine(8014, BOB_UID).launch["route"]["name"], "qwen3-4b@bob")

    def test_a_restart_does_not_rename_an_existing_route(self):
        set_default_visibility(self.store, ALL, updated_by="admin")
        self.serve(port=8013)
        self.runner.tick()
        first = self.engine().launch["route"]["name"]
        # 换一个占用了同一个名字的服务，再重启守护进程
        self.serve(port=8014, uid=BOB_UID)

        restarted = IngestRunner(
            store=self.store, directory=self.directory, table=self.table, fetch=self.fetch
        )
        restarted.tick()

        self.assertEqual(first, "qwen3-4b")
        self.assertEqual(self.engine(8013).launch["route"]["name"], "qwen3-4b")


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


class AutoModelTests(RunnerTestCase):
    """自动发现的是「引擎 + 模型」两样：模型也要进账本，不能只留在 launch_json 里。"""

    def models_of(self, principal: str = "alice"):
        return [m for m in self.store.list_models() if m.owner_principal == principal]

    def test_an_attached_engine_registers_the_models_it_reports(self):
        self.serve(models=("qwen3-4b", "qwen3-4b-embed"))

        self.runner.tick()

        rows = self.models_of()
        self.assertEqual(sorted(row.name for row in rows), ["qwen3-4b", "qwen3-4b-embed"])
        for row in rows:
            self.assertEqual(row.source, SOURCE_AUTO)
            self.assertEqual(row.owner_uid, ALICE_UID)
            # 观测到名字 ≠ 观测到权重：路径必须留空，猜出来的路径比空着更坏。
            self.assertIsNone(row.weight_path)
            self.assertIsNone(row.format)

    def test_the_engine_payload_links_its_models_back_to_the_ledger_rows(self):
        self.serve(models=("qwen3-4b",))

        self.runner.tick()

        engine = self.engine()
        model = self.models_of()[0]
        self.assertEqual(engine.launch["model_rows"], {"qwen3-4b": model.id})

    def test_a_restart_does_not_duplicate_the_model_rows(self):
        self.serve()
        self.runner.tick()
        first = [row.id for row in self.models_of()]

        self.runner.tick()
        self.runner.tracked.clear()  # 守护进程重启：tracked 空了，会再 ATTACH 一次
        self.runner.tick()

        self.assertEqual([row.id for row in self.models_of()], first)

    def test_two_ports_serving_the_same_model_share_one_row(self):
        self.serve(port=8013, models=("qwen3-4b",))
        self.serve(port=8014, models=("qwen3-4b",))

        self.runner.tick()

        self.assertEqual(len(self.models_of()), 1)
        self.assertEqual(
            len({row.id for row in self.models_of()}), 1, "同一 owner 下的同名模型只该有一行"
        )

    def test_an_owner_prefixed_serving_id_becomes_a_legal_name(self):
        """``Qwen/Qwen3-4B`` 含斜杠，而 Registry 名称不允许路径分隔符。"""

        self.serve(models=("Qwen/Qwen3-4B",))

        self.runner.tick()

        names = [row.name for row in self.models_of()]
        self.assertEqual(names, ["Qwen-Qwen3-4B"])
        self.assertNotIn("/", names[0])

    def test_an_existing_manual_model_is_reused_and_never_rewritten(self):
        """自动发现只补空、不覆盖：手工登记的权重路径不能被观测值抹掉。"""

        now = datetime.now(timezone.utc)
        self.store.insert_model(
            Model(
                id="mdl_manual0000000000",
                owner_principal="alice",
                owner_uid=ALICE_UID,
                name="qwen3-4b",
                weight_path="/srv/models/qwen3-4b",
                weight_path_sha256=None,
                format="hf",
                parameter_count=None,
                status=MODEL_REGISTERED,
                health={},
                source=SOURCE_CLIENT_REGISTERED,
                created_at=now,
                updated_at=now,
            ),
            idempotency_key=None,
            fingerprint="manual0000000000000000000000000",
        )
        self.serve(models=("qwen3-4b",))

        self.runner.tick()

        rows = self.models_of()
        self.assertEqual(len(rows), 1, "不得为同名模型再建一行")
        self.assertEqual(rows[0].id, "mdl_manual0000000000")
        self.assertEqual(rows[0].weight_path, "/srv/models/qwen3-4b")
        self.assertEqual(
            self.engine().launch["model_rows"], {"qwen3-4b": "mdl_manual0000000000"}
        )

    def test_going_offline_does_not_delete_the_model_rows(self):
        self.serve()
        self.runner.tick()
        before = [row.id for row in self.models_of()]

        self.table.stop(8013)
        for _ in range(2):
            self.runner.tick()

        self.assertEqual(self.engine().state, ENGINE_RUNTIME_OFFLINE)
        self.assertEqual([row.id for row in self.models_of()], before)


class ClaimingDirectory(PrincipalDirectory):
    """静态目录 + 一条「这个 Linux 用户被哪个平台账户认领」。

    只覆盖扫描路径用到的那一个方法。``AccountAwareDirectory`` 的真实实现（唯一
    匹配、停用不认领、两人争抢同一个名字时返回 ``None``）另有覆盖：
    ``tests/bff/test_account_directory_by_uid.py``。
    """

    def __init__(self, principals, claims: dict[str, str | None]) -> None:
        super().__init__(principals)
        self._claims = claims

    def account_for_linux_user(self, linux_user: str):
        account = self._claims.get(linux_user)
        if account is None:
            return None
        return Principal(principal=account, linux_user=linux_user, uid=None, kind="account")


class AttachOwnershipTests(RunnerTestCase):
    """扫描接入的服务必须归到「认领了这个 Linux 用户」的账户名下。

    两条自动接入路径（端口扫描、代理上报）如果给出不同形状的归属主体，同一台
    机器上的服务就会因为「怎么被发现的」不同而分属两个名字，而控制台判断
    「是不是我的」是主体字符串全等。
    """

    def runner_claiming(self, claims: dict[str, str | None]) -> IngestRunner:
        return IngestRunner(
            store=self.store,
            directory=ClaimingDirectory(
                [Principal("alice", "alice", ALICE_UID), Principal("bob", "bob", BOB_UID)],
                claims,
            ),
            table=self.table,
            fetch=self.fetch,
        )

    def test_the_owner_is_the_account_that_claims_the_linux_user(self):
        runner = self.runner_claiming({"alice": "account:319ff153f9792ed5"})
        self.serve()

        runner.tick()

        engine = self.engine()
        self.assertEqual(engine.owner_principal, "account:319ff153f9792ed5")
        # uid 仍然是事实记录：谁在跑这个进程，与谁拥有这一行，是两件事。
        self.assertEqual(engine.owner_uid, ALICE_UID)

    def test_an_unclaimed_linux_user_stays_with_the_linux_principal(self):
        runner = self.runner_claiming({})
        self.serve()

        runner.tick()

        self.assertEqual(self.engine().owner_principal, "alice")

    def test_a_collided_route_name_uses_the_linux_user_not_the_account_id(self):
        """路由名会被拼进对外契约里，``qwen3-4b@account:319f…`` 没法给人看。"""

        runner = self.runner_claiming({"alice": "account:319ff153f9792ed5"})
        set_default_visibility(self.store, ALL)
        self.serve(port=8013)
        self.serve(port=8014)

        runner.tick()

        names = sorted(
            engine.launch["route"]["name"] for engine in observed_engines(self.store)
        )
        self.assertEqual(names, ["qwen3-4b", "qwen3-4b@alice"])


if __name__ == "__main__":
    unittest.main()
