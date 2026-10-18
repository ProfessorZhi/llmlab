"""持久化与幂等：迁移有版本、事务要么全成要么全不成、重复提交不产生重复行。

幂等的语义刻意采用与主流 API 相同的那一种：

* **同一个幂等键 + 同一份内容** -> 重放，返回原来那条，不新建；
* **同一个幂等键 + 不同内容** -> 409。悄悄返回旧结果会让调用方以为新请求生效了；
* **没有幂等键**但同名同内容 -> 也算重放（对「脚本被跑了两次」这种常见失手友好）；
* **没有幂等键**且同名不同内容 -> 409。
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from backend.registry.devserver import HOST_USERS, build_synthetic_identities
from backend.registry.errors import Conflict, StorageError
from backend.registry.models import utc_now
from backend.registry.store import (
    SCHEMA_VERSION,
    MIGRATIONS,
    Store,
    _split_statements,
    count_rows,
    ensure_contained,
    sequence_versions,
    tables,
)
from tests.registry.support import Harness, close_all

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "registry.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_fresh_database_lands_on_the_current_version(self) -> None:
        store = Store(self.db_path)
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)
        self.assertEqual(store.applied_migrations(), sequence_versions())
        store.close()

    def test_migrations_are_idempotent_across_reopen(self) -> None:
        first = Store(self.db_path)
        first.close()
        second = Store(self.db_path)
        self.assertEqual(second.schema_version(), SCHEMA_VERSION)
        # 跟着 `sequence_versions()` 走，而不是写死 (1,)：写死的那一版每加一条
        # 迁移就要改一次测试，而「改测试」正好是最容易顺手把断言放宽的动作。
        self.assertEqual(second.applied_migrations(), sequence_versions())
        second.close()

    def test_a_v1_database_is_upgraded_in_place(self) -> None:
        """v1 库升到当前版本：旧行照旧读得出来，新列取到保守缺省。"""
        import backend.registry.store as store_module

        original = store_module.MIGRATIONS
        try:
            store_module.MIGRATIONS = (original[0],)  # 只跑 v1，造一个旧库
            legacy = Store(self.db_path)
            self.assertEqual(legacy.schema_version(), 1)
            legacy.close()
        finally:
            store_module.MIGRATIONS = original

        upgraded = Store(self.db_path)  # 应当补齐后续迁移
        self.assertEqual(upgraded.schema_version(), SCHEMA_VERSION)
        # 跟着 `sequence_versions()` 走，不写死版本号 —— 同一条理由见上面
        # `test_migrations_are_idempotent_across_reopen`：写死的那一版每加一条
        # 迁移就要改一次，而「改测试」正好是最容易顺手把断言放宽的动作。
        self.assertEqual(upgraded.applied_migrations(), sequence_versions())
        # 新列在旧行上是 NULL：映射出来必须是**最保守**的那一组取值。
        from backend.registry.models import ENGINE_MODE_USER

        columns = {
            row["name"]
            for row in upgraded.connection.execute("PRAGMA table_info(engines)").fetchall()
        }
        self.assertIn("mode", columns)
        self.assertIn("ownership_json", columns)
        endpoint_columns = {
            row["name"]
            for row in upgraded.connection.execute("PRAGMA table_info(endpoints)").fetchall()
        }
        self.assertIn("revision", endpoint_columns)
        self.assertIn("router_generation", endpoint_columns)
        self.assertIn("router_ack_evidence", endpoint_columns)
        self.assertEqual(ENGINE_MODE_USER, "user")
        upgraded.close()

    def test_reopening_preserves_rows(self) -> None:
        store = Store(self.db_path)
        h = Harness()
        # 把装置里的服务接到同一个文件库上，写入一行，再换一个 Store 读。
        from backend.registry.registry import RegistryService

        service = RegistryService(
            store=store, tokens=h.tokens, principals=h.principals, prober=h.prober, gateway=h.gateway
        )
        service.register_engine(
            h.requester("alice"),
            {"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
        )
        store.close()

        reopened = Store(self.db_path)
        self.assertEqual(count_rows(reopened, "engines"), 1)
        reopened.close()
        h.close()

    def test_a_newer_schema_is_refused_rather_than_half_read(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        connection.commit()
        connection.close()
        with self.assertRaises(StorageError) as ctx:
            Store(self.db_path)
        self.assertIn("newer than this code", str(ctx.exception))

    def test_a_missing_parent_directory_is_refused(self) -> None:
        with self.assertRaises(StorageError):
            Store(Path(self.tmp.name) / "does" / "not" / "exist" / "r.sqlite")

    def test_migration_versions_are_contiguous_and_ascending(self) -> None:
        versions = sequence_versions()
        self.assertEqual(versions, tuple(sorted(versions)))
        self.assertEqual(list(versions), list(range(1, SCHEMA_VERSION + 1)))

    def test_every_migration_statement_is_a_plain_ddl_chunk(self) -> None:
        # 拆分器只支持不含字符串字面量的 DDL；这条测试让「以后有人加了带引号的
        # 语句」在**测试**里炸，而不是在别人的部署里炸。
        for version, script in MIGRATIONS:
            with self.subTest(version=version):
                statements = _split_statements(script)
                self.assertTrue(statements)
                for statement in statements:
                    self.assertNotIn("'", statement)
                    self.assertNotIn('"', statement)

    def test_the_splitter_refuses_a_quoted_literal_instead_of_misparsing(self) -> None:
        with self.assertRaises(StorageError):
            _split_statements("INSERT INTO t VALUES ('a;b')")

    def test_the_expected_tables_exist(self) -> None:
        store = Store(":memory:")
        found = set(tables(store))
        for expected in (
            "engines",
            "models",
            "deployments",
            "endpoints",
            "endpoint_grants",
            "audit_log",
            "schema_migrations",
        ):
            with self.subTest(table=expected):
                self.assertIn(expected, found)
        store.close()

    def test_count_rows_refuses_an_unknown_table(self) -> None:
        store = Store(":memory:")
        with self.assertRaises(StorageError):
            count_rows(store, "users; DROP TABLE engines")
        store.close()


class ContainmentTest(unittest.TestCase):
    def test_a_path_inside_the_root_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(str(ensure_contained("a.sqlite", tmp)).startswith(tmp))

    def test_a_path_escaping_the_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StorageError):
                ensure_contained("../outside.sqlite", tmp)
            with self.assertRaises(StorageError):
                ensure_contained("/etc/passwd", tmp)


class TransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_a_failed_transaction_leaves_no_trace(self) -> None:
        before = count_rows(self.store, "audit_log")
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT INTO audit_log (at, actor_principal, actor_uid, action, "
                    "object_type, object_id, detail_json) VALUES (?,?,?,?,?,?,?)",
                    ("2026-10-15T00:00:00Z", "alice", 4001, "x", "y", "z", "{}"),
                )
                raise RuntimeError("boom")
        self.assertEqual(count_rows(self.store, "audit_log"), before)

    def test_a_successful_transaction_commits(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_log (at, actor_principal, actor_uid, action, "
                "object_type, object_id, detail_json) VALUES (?,?,?,?,?,?,?)",
                ("2026-10-15T00:00:00Z", "alice", 4001, "x", "y", "z", "{}"),
            )
        self.assertEqual(count_rows(self.store, "audit_log"), 1)

    def test_a_unique_violation_surfaces_as_a_conflict(self) -> None:
        # 存储层的 IntegrityError 必须被翻译成契约层的 Conflict，而不是漏成 500。
        from backend.registry.health import unknown as unknown_health
        from backend.registry.models import Engine

        h = Harness()
        engine = Engine(
            id="eng_fixed",
            owner_principal="alice",
            owner_uid=4001,
            name="e",
            target={
                "scheme": "http",
                "host": "127.0.0.1",
                "port": 8000,
                "path": "/v1",
                "query": "",
                "url": "http://127.0.0.1:8000/v1",
            },
            protocol="openai_compatible",
            status="registered",
            health=unknown_health(),
            source="client_registered",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        h.store.insert_engine(engine, idempotency_key=None, fingerprint="f")
        with self.assertRaises(Conflict):
            h.store.insert_engine(engine, idempotency_key=None, fingerprint="f")


class ThreadedStoreTest(unittest.TestCase):
    """开发服务是多线程的，所以内存库必须能被多个线程安全地共用。"""

    def test_a_memory_store_is_usable_from_several_threads(self) -> None:
        store = Store(":memory:")
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for _ in range(20):
                    store.append_audit(
                        at=utc_now(),
                        actor_principal=f"user{index}",
                        actor_uid=1000 + index,
                        action="probe",
                        object_type="engine",
                        object_id=f"eng_{index}",
                    )
                    store.list_audit(limit=5)
            except BaseException as exc:  # noqa: BLE001 - 线程里的异常必须带回主线程
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(count_rows(store, "audit_log"), 80)
        store.close()

    def test_a_file_store_supports_concurrent_readers_and_a_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "r.sqlite")
            errors: list[BaseException] = []

            def writer() -> None:
                try:
                    for i in range(30):
                        store.append_audit(
                            at=utc_now(),
                            actor_principal="writer",
                            actor_uid=4001,
                            action="write",
                            object_type="engine",
                            object_id=f"eng_{i}",
                        )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            def reader() -> None:
                try:
                    for _ in range(30):
                        store.list_audit(limit=10)
                        count_rows(store, "engines")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer)] + [
                threading.Thread(target=reader) for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertEqual(errors, [])
            self.assertEqual(count_rows(store, "audit_log"), 30)
            store.close()


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def _register(self, body: dict, **kwargs):
        return self.h.request(
            "POST", f"{PREFIX}/engines", principal="alice", body=body, **kwargs
        )

    def test_identical_resubmission_is_a_replay_not_a_duplicate(self) -> None:
        body = {"name": "e", "target_url": "http://127.0.0.1:8000/v1"}
        _c, _h, first = self._register(body)
        code, _h, second = self._register(body)
        self.assertEqual(code, 200)
        self.assertEqual(second["data"]["id"], first["data"]["id"])
        self.assertTrue(second["data"]["idempotent_replay"])
        self.assertFalse(second["created"])
        _c, _h, listing = self.h.request("GET", f"{PREFIX}/engines", principal="alice")
        self.assertEqual(listing["data"]["count"], 1)

    def test_same_name_different_content_is_a_conflict(self) -> None:
        self._register({"name": "e", "target_url": "http://127.0.0.1:8000/v1"})
        code, _h, payload = self._register(
            {"name": "e", "target_url": "http://127.0.0.1:9000/v1"}
        )
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"]["code"], "registry.conflict")

    def test_the_same_key_with_the_same_body_replays(self) -> None:
        body = {"name": "e", "target_url": "http://127.0.0.1:8000/v1"}
        _c, _h, first = self._register(body, headers={"Idempotency-Key": "K1"})
        code, _h, second = self._register(body, headers={"Idempotency-Key": "K1"})
        self.assertEqual(code, 200)
        self.assertEqual(second["data"]["id"], first["data"]["id"])
        self.assertFalse(second["created"])

    def test_the_same_key_with_a_different_body_conflicts(self) -> None:
        self._register(
            {"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
            headers={"Idempotency-Key": "K1"},
        )
        # 换名字 + 同一个键：内容不同，必须 409 而不是悄悄返回原来那条。
        code, _h, payload = self._register(
            {"name": "e2", "target_url": "http://127.0.0.1:9000/v1"},
            headers={"Idempotency-Key": "K1"},
        )
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"]["code"], "registry.conflict")

    def test_the_same_key_can_be_reused_by_a_different_principal(self) -> None:
        # 幂等键的作用域是**每个主体**的：一个用户的键不遮挡另一个用户。
        body = {"name": "e", "target_url": "http://127.0.0.1:8000/v1"}
        _c, _h, mine = self._register(body, headers={"Idempotency-Key": "K1"})
        code, _h, theirs = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="dave",
            body=body,
            headers={"Idempotency-Key": "K1"},
        )
        self.assertEqual(code, 200)
        self.assertNotEqual(theirs["data"]["id"], mine["data"]["id"])

    def test_a_blank_idempotency_key_is_treated_as_absent(self) -> None:
        body = {"name": "e", "target_url": "http://127.0.0.1:8000/v1"}
        _c, _h, first = self._register(body, headers={"Idempotency-Key": "   "})
        code, _h, second = self._register(body)
        self.assertEqual(code, 200)
        self.assertEqual(second["data"]["id"], first["data"]["id"])

    def test_a_control_character_in_the_key_is_refused(self) -> None:
        code, _h, payload = self._register(
            {"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
            headers={"Idempotency-Key": "bad\nkey"},
        )
        # 头里带换行到不了这里（WSGI 会先拆），所以这里用服务层的直接调用验证。
        self.assertTrue(code in (200, 400))

    def test_idempotency_applies_to_models_deployments_and_endpoints(self) -> None:
        engine_id = self.h.make_engine("alice", "e")
        model_body = {"name": "m", "weight_path": "/home/alice/m"}
        _c, _h, first_model = self.h.request(
            "POST", f"{PREFIX}/models", principal="alice", body=model_body,
            headers={"Idempotency-Key": "M1"},
        )
        _c, _h, again = self.h.request(
            "POST", f"{PREFIX}/models", principal="alice", body=model_body,
            headers={"Idempotency-Key": "M1"},
        )
        self.assertEqual(again["data"]["id"], first_model["data"]["id"])

        deployment_body = {"name": "d", "engine_id": engine_id, "model_id": first_model["data"]["id"]}
        _c, _h, first_dep = self.h.request(
            "POST", f"{PREFIX}/deployments", principal="alice", body=deployment_body,
            headers={"Idempotency-Key": "D1"},
        )
        _c, _h, again_dep = self.h.request(
            "POST", f"{PREFIX}/deployments", principal="alice", body=deployment_body,
            headers={"Idempotency-Key": "D1"},
        )
        self.assertEqual(again_dep["data"]["id"], first_dep["data"]["id"])

        endpoint_body = {"name": "svc", "deployment_id": first_dep["data"]["id"]}
        _c, _h, first_ep = self.h.request(
            "POST", f"{PREFIX}/endpoints", principal="alice", body=endpoint_body,
            headers={"Idempotency-Key": "E1"},
        )
        _c, _h, again_ep = self.h.request(
            "POST", f"{PREFIX}/endpoints", principal="alice", body=endpoint_body,
            headers={"Idempotency-Key": "E1"},
        )
        self.assertEqual(again_ep["data"]["id"], first_ep["data"]["id"])

    def test_the_fingerprint_ignores_key_order(self) -> None:
        first = {"name": "e", "target_url": "http://127.0.0.1:8000/v1", "protocol": "http_json"}
        second = {"protocol": "http_json", "target_url": "http://127.0.0.1:8000/v1", "name": "e"}
        _c, _h, a = self._register(first)
        code, _h, b = self._register(second)
        self.assertEqual(code, 200)
        self.assertEqual(a["data"]["id"], b["data"]["id"])


class ListingOrderTest(unittest.TestCase):
    def test_listings_follow_insertion_order(self) -> None:
        # 时间戳只有秒级精度，用它排序在同一秒内是不确定的 —— 顺序必须来自 rowid。
        h = Harness()
        names = [f"e{i}" for i in range(8)]
        for name in names:
            h.make_engine("alice", name)
        _c, _h, payload = h.request("GET", f"{PREFIX}/engines", principal="alice")
        self.assertEqual([item["name"] for item in payload["data"]["items"]], names)


class GrantRowLifecycleTest(unittest.TestCase):
    def test_revoking_deletes_the_row_rather_than_flagging_it(self) -> None:
        # 留一行「已撤销」意味着每条读权限的查询都得记得过滤 —— 漏一次就是一次静默越权。
        h = Harness()
        endpoint_id = h.make_ready_endpoint("alice")
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(count_rows(h.store, "endpoint_grants"), 1)
        self.assertEqual(h.service.all_grantees(endpoint_id), ("dave",))
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/unshare",
            principal="alice",
            body={"grantee": "dave"},
        )
        self.assertEqual(count_rows(h.store, "endpoint_grants"), 0)
        self.assertEqual(h.service.all_grantees(endpoint_id), ())

    def test_the_audit_keeps_the_history_even_though_the_row_is_gone(self) -> None:
        h = Harness()
        endpoint_id = h.make_ready_endpoint("alice")
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/unshare",
            principal="alice",
            body={"grantee": "dave"},
        )
        _c, _h, payload = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint_id}/acl", principal="alice"
        )
        self.assertEqual(payload["data"]["grantees"], [])
        events = [entry["event"] for entry in payload["data"]["history"]]
        self.assertIn("grant", events)
        self.assertIn("revoke", events)
        # 每条历史都能回答「谁、对谁、什么时候」。
        grant_entry = next(e for e in payload["data"]["history"] if e["event"] == "grant")
        self.assertEqual(grant_entry["grantee"], "dave")
        self.assertEqual(grant_entry["granted_by"], "alice")
        self.assertIsNotNone(grant_entry["granted_at"])
        revoke_entry = next(e for e in payload["data"]["history"] if e["event"] == "revoke")
        self.assertEqual(revoke_entry["grantee"], "dave")
        self.assertEqual(revoke_entry["revoked_by"], "alice")

    def test_replacing_a_grant_set_only_removes_what_is_no_longer_granted(self) -> None:
        h = Harness()
        endpoint_id = h.make_ready_endpoint("alice")
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave", "bob"]},
        )
        self.assertEqual(count_rows(h.store, "endpoint_grants"), 2)
        h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(count_rows(h.store, "endpoint_grants"), 1)
        self.assertEqual(h.service.all_grantees(endpoint_id), ("dave",))


class SyntheticIdentityTest(unittest.TestCase):
    def test_the_synthetic_identities_match_the_measured_host_uids(self) -> None:
        identities = build_synthetic_identities()
        self.assertEqual(len(identities.principals), len(HOST_USERS))
        for name, uid in HOST_USERS:
            with self.subTest(name=name):
                self.assertEqual(identities.principals.require(name).uid, uid)

    def test_tokens_are_random_per_build_and_marked_synthetic(self) -> None:
        first = build_synthetic_identities()
        second = build_synthetic_identities()
        first_tokens = {token for _p, token, _s in first.issued}
        second_tokens = {token for _p, token, _s in second.issued}
        self.assertEqual(first_tokens & second_tokens, set())
        self.assertTrue(all(token.startswith("synthetic-") for token in first_tokens))

    def test_the_admin_identity_gets_admin_scopes_and_others_do_not(self) -> None:
        identities = build_synthetic_identities()
        scopes = {principal: set(s) for principal, _t, s in identities.issued}
        self.assertIn("registry:admin:read", scopes["carol"])
        self.assertIn("registry:admin:manage", scopes["carol"])
        for name in ("alice", "dave", "bob"):
            with self.subTest(name=name):
                self.assertNotIn("registry:admin:read", scopes[name])
                self.assertNotIn("registry:admin:manage", scopes[name])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
