"""schema v4：资源自动接入这一层的账本。

本文件只钉**存储层**，不碰发现循环 / CLI / 网关：

* 迁移 3 → 4 是纯增量（加列、加表），且老 ``engines`` 行被回填成 ``attached``；
* 新增的四个领域对象各自能往返一次；
* ``claim_task`` 的「一条任务只能被领一次」在并发下成立。

测试全部用临时目录里的临时库，不读写任何真实用户数据，也不联网。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.registry.errors import Conflict
from backend.registry.models import (
    AGENT_TASK_DONE,
    AGENT_TASK_FAILED,
    AGENT_TASK_QUEUED,
    ENGINE_RUNTIME_ATTACHED,
    ENGINE_RUNTIME_OFFLINE,
    ENGINE_VERIFIED,
    LOCATION_MISSING,
    LOCATION_PRESENT,
    MODEL_REGISTERED,
    PROTOCOL_OPENAI_COMPAT,
    RUNTIME_BUILD_MISSING,
    RUNTIME_BUILD_PRESENT,
    AgentTask,
    Engine,
    Model,
    ModelLocation,
    RuntimeBuild,
    utc_now,
)
from backend.registry.store import (
    MIGRATIONS,
    SCHEMA_VERSION,
    Store,
    _split_statements,
)

NOW = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc)


def _model(model_id: str = "m1", *, uid: int = 4001, owner: str = "alice") -> Model:
    return Model(
        id=model_id,
        owner_principal=owner,
        owner_uid=uid,
        name=f"model-{model_id}",
        weight_path=f"/data/home/{owner}/{model_id}.gguf",
        weight_path_sha256=None,
        format="gguf",
        parameter_count=None,
        status=MODEL_REGISTERED,
        health={},
        source="agent",
        created_at=NOW,
        updated_at=NOW,
        last_seen_at=NOW,
        kind="gguf",
        task="chat",
        facts={"context_length": 40960, "block_count": 36},
        size_bytes=4507123712,
    )


def _engine(
    engine_id: str = "e1",
    *,
    uid: int = 4001,
    owner: str = "alice",
    source: str = "agent",
) -> Engine:
    return Engine(
        id=engine_id,
        owner_principal=owner,
        owner_uid=uid,
        name=f"engine-{engine_id}",
        target={"scheme": "http", "host": "127.0.0.1", "port": 8013, "path": "/v1"},
        protocol=PROTOCOL_OPENAI_COMPAT,
        status=ENGINE_VERIFIED,
        health={},
        source=source,
        created_at=NOW,
        updated_at=NOW,
        last_seen_at=NOW,
        state=ENGINE_RUNTIME_ATTACHED,
        pid=4242,
        unit="llmlab-e1.service",
        model_id="m1",
        runtime_build_id="b1",
        launch={"ctx_size": 32768},
    )


def _build(build_id: str = "b1", *, fingerprint: str = "bin-fp-1") -> RuntimeBuild:
    return RuntimeBuild(
        id=build_id,
        owner_principal="alice",
        owner_uid=4001,
        framework="llama.cpp",
        binary_path="/home/alice/llama.cpp/build/bin/llama-server",
        version="b6512",
        git_commit="a1b2c3d",
        fingerprint=fingerprint,
        flags={"--ctx-size": {"type": "int", "default": 4096}},
        formats=("gguf",),
        state=RUNTIME_BUILD_PRESENT,
        last_seen_at=NOW,
    )


def _location(path: str, *, state: str = LOCATION_PRESENT) -> ModelLocation:
    return ModelLocation(
        model_id="m1",
        path=path,
        uid=4001,
        device=64768,
        inode=1234,
        mtime=1761200000.0,
        tier="ssd",
        state=state,
        last_seen_at=NOW,
    )


def _task(task_id: str, *, uid: int = 4001, kind: str = "start") -> AgentTask:
    return AgentTask(
        id=task_id,
        uid=uid,
        kind=kind,
        payload={"model_id": "m1"},
        requested_by="alice",
        created_at=NOW,
        state=AGENT_TASK_QUEUED,
        result=None,
        updated_at=None,
    )


class _StoreCase(unittest.TestCase):
    """每个测试一个临时目录里的独立库。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "registry.sqlite"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)


class SchemaV4Test(_StoreCase):
    def test_a_fresh_database_is_v4_and_has_every_new_table_and_column(self) -> None:
        self.assertEqual(self.store.schema_version(), 4)
        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertEqual(self.store.applied_migrations(), (1, 2, 3, 4))

        def columns(table: str) -> set[str]:
            return {
                row["name"]
                for row in self.store.connection.execute(f"PRAGMA table_info({table})")
            }

        self.assertTrue(
            {"kind", "task", "facts_json", "size_bytes"} <= columns("models"),
            columns("models"),
        )
        self.assertTrue(
            {"state", "pid", "unit", "model_id", "runtime_build_id", "launch_json"}
            <= columns("engines"),
            columns("engines"),
        )
        self.assertEqual(
            columns("model_locations"),
            {
                "model_id",
                "path",
                "uid",
                "device",
                "inode",
                "mtime",
                "tier",
                "state",
                "last_seen_at",
            },
        )
        self.assertEqual(
            columns("runtime_builds"),
            {
                "id",
                "owner_principal",
                "owner_uid",
                "framework",
                "binary_path",
                "version",
                "git_commit",
                "fingerprint",
                "flags_json",
                "formats_json",
                "state",
                "last_seen_at",
            },
        )
        self.assertEqual(
            columns("agent_tasks"),
            {
                "id",
                "uid",
                "kind",
                "payload_json",
                "requested_by",
                "created_at",
                "state",
                "result_json",
                "updated_at",
            },
        )
        self.assertEqual(
            columns("lab_settings"), {"key", "value", "updated_by", "updated_at"}
        )

        indexes = {
            row["name"]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertIn("model_locations_state", indexes)
        self.assertIn("agent_tasks_uid_state", indexes)
        self.assertIn("runtime_builds_fingerprint", indexes)

    def test_existing_engines_are_backfilled_to_attached(self) -> None:
        """手工建一个 v3 库（只跑前三条迁移），再让 Store 升到 4。"""
        # 必须是一条新路径：setUp 里的 Store 已经把 self.db 建到 v4 了。
        legacy_db = Path(self.tmp.name) / "legacy.sqlite"

        connection = sqlite3.connect(legacy_db, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                "CREATE TABLE schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for version, script in MIGRATIONS[:3]:
                for statement in _split_statements(script):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, "2026-10-01T00:00:00Z"),
                )
            connection.execute("PRAGMA user_version = 3")
            # 一条「那个时代」的 engine 行：只有 v1/v2 的列，没有任何 state。
            connection.execute(
                "INSERT INTO engines ("
                "id, owner_principal, owner_uid, name, target_json, protocol, status, "
                "health_json, source, description, tags_json, created_at, updated_at, "
                "last_seen_at, verified_at, idempotency_key, fingerprint, mode, "
                "ownership_json, verification_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "e_legacy",
                    "alice",
                    4001,
                    "legacy-svc",
                    '{"host":"127.0.0.1","port":8080}',
                    PROTOCOL_OPENAI_COMPAT,
                    ENGINE_VERIFIED,
                    "{}",
                    "operator_existing_service",
                    "8080 上那台手工登记的服务",
                    "[]",
                    "2026-10-01T00:00:00Z",
                    "2026-10-01T00:00:00Z",
                    "2026-10-01T00:00:00Z",
                    "2026-10-01T00:00:00Z",
                    None,
                    "fp-legacy",
                    "system_existing",
                    "{}",
                    "{}",
                ),
            )
            connection.execute(
                "INSERT INTO models ("
                "id, owner_principal, owner_uid, name, weight_path, weight_sha256, format, "
                "parameter_count, status, health_json, source, description, tags_json, "
                "created_at, updated_at, last_seen_at, idempotency_key, fingerprint"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "m_legacy",
                    "alice",
                    4001,
                    "legacy-model",
                    "/data/home/alice/legacy.gguf",
                    None,
                    "gguf",
                    None,
                    MODEL_REGISTERED,
                    "{}",
                    "manual",
                    None,
                    "[]",
                    "2026-10-01T00:00:00Z",
                    "2026-10-01T00:00:00Z",
                    "2026-10-01T00:00:00Z",
                    None,
                    "fp-legacy-model",
                ),
            )
        finally:
            connection.close()

        upgraded = Store(legacy_db)
        self.addCleanup(upgraded.close)

        self.assertEqual(upgraded.schema_version(), 4)
        self.assertEqual(upgraded.applied_migrations(), (1, 2, 3, 4))

        engine = upgraded.engine_by_id("e_legacy")
        self.assertIsNotNone(engine)
        # 回填：这些服务当初就是人工确认过的运行中服务，不该留 NULL。
        self.assertEqual(engine.state, ENGINE_RUNTIME_ATTACHED)
        # 老数据一个字都没丢。
        self.assertEqual(engine.name, "legacy-svc")
        self.assertEqual(engine.owner_principal, "alice")
        self.assertEqual(engine.target["port"], 8080)
        self.assertEqual(engine.mode, "system_existing")
        self.assertEqual(engine.source, "operator_existing_service")
        # v4 的新列在老行上是 NULL / 空，而不是被编出来的值。
        self.assertIsNone(engine.pid)
        self.assertIsNone(engine.model_id)
        self.assertEqual(engine.launch, {})

        model = upgraded.model_by_id("m_legacy")
        self.assertIsNotNone(model)
        self.assertEqual(model.name, "legacy-model")
        self.assertIsNone(model.kind)
        self.assertEqual(model.facts, {})

        # 回填之后旧行确实进入了「已接入」这个状态集合。
        self.assertEqual([item.id for item in upgraded.engines_in_state("attached")], ["e_legacy"])


class SettingsTest(_StoreCase):
    def test_get_set_upsert_and_all(self) -> None:
        self.assertIsNone(self.store.get_setting("default_visibility"))

        self.store.set_setting("default_visibility", "private", updated_by="carol")
        self.assertEqual(self.store.get_setting("default_visibility"), "private")

        self.store.set_setting("default_visibility", "all", updated_by="dave")
        self.assertEqual(self.store.get_setting("default_visibility"), "all")
        # upsert 不产生第二行。
        rows = self.store.connection.execute(
            "SELECT * FROM lab_settings WHERE key = ?", ("default_visibility",)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["updated_by"], "dave")
        self.assertIsNotNone(rows[0]["updated_at"])

        self.store.set_setting("other", "x", updated_by="carol")
        self.assertEqual(
            self.store.all_settings(), {"default_visibility": "all", "other": "x"}
        )


class LocationTest(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.insert_model(_model(), idempotency_key=None, fingerprint="fp-m1")

    def test_upsert_and_list(self) -> None:
        self.assertEqual(self.store.locations_for("m1"), [])

        self.store.upsert_location(_location("/ssd/m1.gguf"))
        self.store.upsert_location(_location("/hdd/m1.gguf"))

        locations = self.store.locations_for("m1")
        self.assertEqual([item.path for item in locations], ["/hdd/m1.gguf", "/ssd/m1.gguf"])
        self.assertEqual(locations[0].device, 64768)
        self.assertEqual(locations[0].tier, "ssd")
        self.assertEqual(locations[0].state, LOCATION_PRESENT)
        self.assertEqual(locations[0].last_seen_at, NOW)

        # 同主键再写一次是更新，不是第二行。
        self.store.upsert_location(
            ModelLocation(
                model_id="m1",
                path="/ssd/m1.gguf",
                uid=4001,
                device=1,
                inode=2,
                mtime=3.0,
                tier="hdd",
                state=LOCATION_MISSING,
                last_seen_at=NOW,
            )
        )
        locations = self.store.locations_for("m1")
        self.assertEqual(len(locations), 2)
        ssd = next(item for item in locations if item.path == "/ssd/m1.gguf")
        self.assertEqual(ssd.tier, "hdd")
        self.assertEqual(ssd.device, 1)
        self.assertEqual(ssd.state, LOCATION_MISSING)

    def test_upsert_for_unknown_model_is_a_conflict(self) -> None:
        with self.assertRaises(Conflict):
            self.store.upsert_location(
                ModelLocation(
                    model_id="nope",
                    path="/x",
                    uid=4001,
                    state=LOCATION_PRESENT,
                    last_seen_at=NOW,
                )
            )

    def test_mark_missing_by_seen_paths(self) -> None:
        self.store.upsert_location(_location("/ssd/m1.gguf"))
        self.store.upsert_location(_location("/hdd/m1.gguf"))

        changed = self.store.mark_locations_missing("m1", ["/ssd/m1.gguf"])
        self.assertEqual(changed, 1)
        self.assertEqual(
            [item.path for item in self.store.present_locations()], ["/ssd/m1.gguf"]
        )
        self.assertEqual(
            self.store.locations_for("m1")[0].state, LOCATION_MISSING
        )

        # 再次标记同一批不会重复计数（已经是 missing 的行不再改）。
        self.assertEqual(self.store.mark_locations_missing("m1", ["/ssd/m1.gguf"]), 0)

    def test_empty_seen_paths_means_the_whole_model_is_missing(self) -> None:
        self.store.upsert_location(_location("/ssd/m1.gguf"))
        self.store.upsert_location(_location("/hdd/m1.gguf"))

        self.assertEqual(self.store.mark_locations_missing("m1", []), 2)
        self.assertEqual(self.store.present_locations(), [])
        # 行不删除：位置回来时可以原地复活，历史也还在。
        self.assertEqual(len(self.store.locations_for("m1")), 2)


class BuildTest(_StoreCase):
    def test_upsert_owner_and_fingerprint_lookups(self) -> None:
        self.store.upsert_build(_build())
        self.assertEqual(self.store.build_by_fingerprint("bin-fp-1").version, "b6512")
        self.assertIsNone(self.store.build_by_fingerprint("nope"))

        # 按 id upsert：版本与 flags 跟着最新观测走，不新增一行。
        updated = RuntimeBuild(
            id="b1",
            owner_principal="alice",
            owner_uid=4001,
            framework="llama.cpp",
            binary_path="/home/alice/llama.cpp/build/bin/llama-server",
            version="b7000",
            git_commit=None,
            fingerprint="bin-fp-1",
            flags={"--parallel": {"type": "int", "default": 1}},
            formats=("gguf", "ollama"),
            state=RUNTIME_BUILD_PRESENT,
            last_seen_at=NOW,
        )
        self.store.upsert_build(updated)
        builds = self.store.builds_for_owner("alice")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0].version, "b7000")
        self.assertEqual(builds[0].flags, {"--parallel": {"type": "int", "default": 1}})
        self.assertEqual(builds[0].formats, ("gguf", "ollama"))

        self.store.upsert_build(_build("b2", fingerprint="bin-fp-2"))
        self.assertEqual([item.id for item in self.store.builds_for_owner("alice")], ["b1", "b2"])
        self.assertEqual(self.store.builds_for_owner("carol"), [])

    def test_mark_build_missing(self) -> None:
        self.store.upsert_build(_build("b1", fingerprint="f1"))
        self.store.upsert_build(_build("b2", fingerprint="f2"))

        # 空集合是无操作 —— 不能把它解释成「全部标缺失」。
        self.assertEqual(self.store.mark_build_missing([]), 0)
        self.assertEqual(
            len(self.store.builds_for_owner("alice")), 2
        )

        self.assertEqual(self.store.mark_build_missing(["b1"]), 1)
        states = {item.id: item.state for item in self.store.builds_for_owner("alice")}
        self.assertEqual(states, {"b1": RUNTIME_BUILD_MISSING, "b2": RUNTIME_BUILD_PRESENT})
        # 重复标记不重复计数。
        self.assertEqual(self.store.mark_build_missing(["b1"]), 0)


class TaskTest(_StoreCase):
    def test_enqueue_claim_finish_and_list(self) -> None:
        self.store.enqueue_task(_task("t1"))
        self.store.enqueue_task(_task("t2", kind="restart"))
        self.store.enqueue_task(_task("other", uid=4002))

        self.assertEqual([item.id for item in self.store.tasks_for(4001)], ["t1", "t2"])
        self.assertEqual([item.id for item in self.store.tasks_for(4002)], ["other"])

        # 只能领到**本人**的任务，且按入队顺序。
        first = self.store.claim_task(4001, now=utc_now())
        self.assertEqual(first.id, "t1")
        self.assertEqual(first.state, "running")
        self.assertEqual(first.payload, {"model_id": "m1"})
        self.assertIsNotNone(first.updated_at)

        second = self.store.claim_task(4001, now=utc_now())
        self.assertEqual(second.id, "t2")
        self.assertIsNone(self.store.claim_task(4001, now=utc_now()))

        # 别的 uid 的任务不受影响。
        other = self.store.claim_task(4002, now=utc_now())
        self.assertEqual(other.id, "other")

        self.assertTrue(
            self.store.finish_task(
                "t1", state=AGENT_TASK_DONE, result={"pid": 4242}, now=utc_now()
            )
        )
        self.assertFalse(
            self.store.finish_task(
                "missing", state=AGENT_TASK_FAILED, result=None, now=utc_now()
            )
        )
        t1 = next(item for item in self.store.tasks_for(4001) if item.id == "t1")
        self.assertEqual(t1.state, AGENT_TASK_DONE)
        self.assertEqual(t1.result, {"pid": 4242})

    def test_finish_without_result_stores_null(self) -> None:
        self.store.enqueue_task(_task("t1"))
        self.store.claim_task(4001, now=utc_now())
        self.store.finish_task("t1", state=AGENT_TASK_FAILED, result=None, now=utc_now())
        task = self.store.tasks_for(4001)[0]
        self.assertEqual(task.state, AGENT_TASK_FAILED)
        self.assertIsNone(task.result)

    def test_claim_task_in_parallel_only_succeeds_once(self) -> None:
        """同一条任务只能被领一次 —— 两个线程同时领，只有一个拿到。"""
        self.store.enqueue_task(_task("only"))

        barrier = threading.Barrier(2)
        results: list[AgentTask | None] = []
        errors: list[BaseException] = []

        def claim() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(self.store.claim_task(4001, now=utc_now()))
            except BaseException as exc:  # pragma: no cover - 失败时给出原因
                errors.append(exc)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        claimed = [item for item in results if item is not None]
        self.assertEqual(len(claimed), 1, results)
        self.assertEqual(claimed[0].id, "only")
        # 数据库里的状态也只有一个 running，没有第二次迁移。
        task = self.store.tasks_for(4001)[0]
        self.assertEqual(task.state, "running")

    def test_two_store_instances_cannot_claim_the_same_task(self) -> None:
        """这条保证不能只靠进程内的那把锁：两个 Store 各开一条连接时也必须成立。

        模拟守护进程重启后新旧实例短暂共存（或两个进程共用同一个库）：
        ``BEGIN IMMEDIATE`` 让第二个写事务排队，而 UPDATE 的 ``state = queued``
        条件保证即便排到队也改不动已领走的行。
        """
        self.store.enqueue_task(_task("only"))
        other = Store(self.db)
        self.addCleanup(other.close)

        barrier = threading.Barrier(2)
        results: list[AgentTask | None] = []
        errors: list[BaseException] = []

        def claim(store: Store) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(store.claim_task(4001, now=utc_now()))
            except BaseException as exc:  # pragma: no cover - 失败时给出原因
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=(self.store,)),
            threading.Thread(target=claim, args=(other,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len([item for item in results if item is not None]), 1, results)


class EngineRuntimeTest(_StoreCase):
    def test_insert_engine_round_trips_v4_columns(self) -> None:
        self.store.insert_engine(_engine(), idempotency_key=None, fingerprint="fp-e1")

        engine = self.store.engine_by_id("e1")
        self.assertEqual(engine.state, ENGINE_RUNTIME_ATTACHED)
        self.assertEqual(engine.pid, 4242)
        self.assertEqual(engine.unit, "llmlab-e1.service")
        self.assertEqual(engine.model_id, "m1")
        self.assertEqual(engine.runtime_build_id, "b1")
        self.assertEqual(engine.launch, {"ctx_size": 32768})
        # 老的构造与字段仍然完好。
        self.assertEqual(engine.status, ENGINE_VERIFIED)
        self.assertEqual(engine.target["port"], 8013)

    def test_update_engine_runtime_only_touches_passed_columns(self) -> None:
        self.store.insert_engine(_engine(), idempotency_key=None, fingerprint="fp-e1")

        self.store.update_engine_runtime("e1", state=ENGINE_RUNTIME_OFFLINE)
        self.assertEqual(self.store.engine_by_id("e1").state, ENGINE_RUNTIME_OFFLINE)

        # 只带 pid：state / unit / model_id 必须原样保留。
        self.store.update_engine_runtime("e1", pid=9999)
        engine = self.store.engine_by_id("e1")
        self.assertEqual(engine.pid, 9999)
        self.assertEqual(engine.state, ENGINE_RUNTIME_OFFLINE)
        self.assertEqual(engine.unit, "llmlab-e1.service")
        self.assertEqual(engine.model_id, "m1")
        self.assertEqual(engine.runtime_build_id, "b1")
        self.assertEqual(engine.launch, {"ctx_size": 32768})

    def test_successive_partial_updates_accumulate(self) -> None:
        self.store.insert_engine(_engine(), idempotency_key=None, fingerprint="fp-e1")
        self.store.update_engine_runtime("e1", state=ENGINE_RUNTIME_ATTACHED)
        self.store.update_engine_runtime("e1", model_id="m2")
        self.store.update_engine_runtime("e1", launch={"ctx_size": 8192})
        engine = self.store.engine_by_id("e1")
        self.assertEqual(engine.state, ENGINE_RUNTIME_ATTACHED)
        self.assertEqual(engine.model_id, "m2")
        self.assertEqual(engine.launch, {"ctx_size": 8192})

    def test_engines_in_state_and_by_source(self) -> None:
        self.store.insert_engine(_engine("e1"), idempotency_key=None, fingerprint="fp1")
        self.store.insert_engine(
            _engine("e2", source="manual"), idempotency_key=None, fingerprint="fp2"
        )
        self.store.update_engine_runtime("e1", state=ENGINE_RUNTIME_OFFLINE)

        self.assertEqual([item.id for item in self.store.engines_in_state("attached")], ["e2"])
        self.assertEqual([item.id for item in self.store.engines_in_state("offline")], ["e1"])
        self.assertEqual([item.id for item in self.store.engines_by_source("agent")], ["e1"])
        self.assertEqual([item.id for item in self.store.engines_by_source("manual")], ["e2"])

    def test_model_v4_columns_round_trip(self) -> None:
        self.store.insert_model(_model(), idempotency_key=None, fingerprint="fp-m1")
        model = self.store.model_by_id("m1")
        self.assertEqual(model.kind, "gguf")
        self.assertEqual(model.task, "chat")
        self.assertEqual(model.facts, {"context_length": 40960, "block_count": 36})
        self.assertEqual(model.size_bytes, 4507123712)


if __name__ == "__main__":
    unittest.main()
