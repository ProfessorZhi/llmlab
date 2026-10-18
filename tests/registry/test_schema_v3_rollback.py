"""schema v2 → v3：迁移长什么样，以及**回滚**为什么不是 `git revert`。

v3 只加了一件事：``endpoints.router_ack_evidence``，用来记住「代数追平」是
Gateway 回读挣来的，还是运维一句话声明出来的。它同时带来一个运维事实：

**旧代码读不了新库。** ``Store._migrate`` 在 ``user_version > SCHEMA_VERSION``
时直接拒绝打开（"refusing to run against an unknown schema"），而不是半读。
所以把 Registry 回滚到 v3 之前的代码，**必须连同数据库一起回滚**；只回滚代码
会得到一个启动即拒绝的进程 —— 那正是我们想要的方向（fail closed），但它必须是
一次**有准备**的回滚，不能是现场发现。

本文件钉两件事：

1. 一个真的 v2 库升上来之后，旧行的 ``router_ack_evidence`` 是 NULL，读出来是
   ``router_sync_attested``，**不**被提拔成 ``router_sync_applied``；
2. 一个 v3 库被 v2 时代的代码打开时**被拒**，并且错误信息说清楚了原因。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import backend.registry.store as store_module

from backend.registry.models import (
    ENDPOINT_READY,
    VISIBILITY_PRIVATE,
    Endpoint,
    utc_now,
)
from backend.registry.store import Store


def _legacy_endpoint(endpoint_id: str = "ep_legacy") -> Endpoint:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return Endpoint(
        id=endpoint_id,
        owner_principal="alice",
        owner_uid=4001,
        name="legacy-svc",
        deployment_id="dep_legacy",
        route_path="/v1/chat/legacy",
        visibility=VISIBILITY_PRIVATE,
        status=ENDPOINT_READY,
        health={},
        source="registry_store",
        created_at=now,
        updated_at=now,
    )


class _SchemaPin:
    """临时把模块的 schema 钉到某个历史版本，模拟「那个时代的代码」。"""

    def __init__(self, version: int) -> None:
        self.version = version

    def __enter__(self):
        self._schema = store_module.SCHEMA_VERSION
        self._migrations = store_module.MIGRATIONS
        store_module.SCHEMA_VERSION = self.version
        store_module.MIGRATIONS = tuple(
            item for item in self._migrations if item[0] <= self.version
        )
        return self

    def __exit__(self, *exc) -> None:
        store_module.SCHEMA_VERSION = self._schema
        store_module.MIGRATIONS = self._migrations
        return False


class V2ToV3MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "registry.sqlite"

    def _make_v2_database_with_a_caught_up_row(self) -> None:
        """造一个 v2 库，并留下一条「代数追平但没有证据」的行。"""
        with _SchemaPin(2):
            store = Store(self.db)
            self.assertEqual(store.schema_version(), 2)
            store.insert_endpoint(
                _legacy_endpoint(), idempotency_key=None, fingerprint="fp_legacy"
            )
            # v2 只有一个整数：当时的运维回执照样把 generation 推到 revision。
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE endpoints SET router_generation = revision WHERE id = ?",
                    ("ep_legacy",),
                )
            store.close()

    def test_the_column_arrives_null_on_existing_rows(self) -> None:
        self._make_v2_database_with_a_caught_up_row()

        upgraded = Store(self.db)
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.schema_version(), store_module.SCHEMA_VERSION)
        columns = {
            row["name"]
            for row in upgraded.connection.execute("PRAGMA table_info(endpoints)")
        }
        self.assertIn("router_ack_evidence", columns)
        row = upgraded.connection.execute(
            "SELECT router_generation, router_ack_evidence FROM endpoints WHERE id = ?",
            ("ep_legacy",),
        ).fetchone()
        self.assertEqual(int(row["router_generation"]), 1)
        self.assertIsNone(row["router_ack_evidence"])

    def test_a_caught_up_legacy_row_reads_as_attested_not_applied(self) -> None:
        self._make_v2_database_with_a_caught_up_row()

        upgraded = Store(self.db)
        self.addCleanup(upgraded.close)
        endpoint = upgraded.endpoint_by_id("ep_legacy")
        self.assertIsNotNone(endpoint)
        self.assertFalse(endpoint.router_sync_pending)
        self.assertEqual(endpoint.router_sync_state(), "router_sync_attested")
        block = endpoint.router_sync_block()
        self.assertFalse(block["applied"])
        self.assertIsNone(block["evidence"])

    def test_an_operator_ack_after_the_upgrade_records_its_evidence(self) -> None:
        self._make_v2_database_with_a_caught_up_row()

        upgraded = Store(self.db)
        self.addCleanup(upgraded.close)
        outcome = upgraded.ack_router_generation(
            "ep_legacy",
            generation=1,
            actor_principal="carol",
            actor_uid=4000,
            at=utc_now(),
            evidence="operator_attestation",
        )
        self.assertEqual(outcome["outcome"], "ok")
        endpoint = upgraded.endpoint_by_id("ep_legacy")
        self.assertEqual(endpoint.router_ack_evidence, "operator_attestation")
        self.assertEqual(endpoint.router_sync_state(), "router_sync_attested")


class RollbackIsADatabaseRestoreTest(unittest.TestCase):
    """v3 库 + v2 代码 = 拒绝启动，而不是半读。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "registry.sqlite"
        store = Store(self.db)
        store.close()

    def test_a_v2_era_reader_refuses_a_v3_database(self) -> None:
        from backend.registry.errors import StorageError

        with _SchemaPin(2):
            with self.assertRaises(StorageError) as raised:
                Store(self.db)
        message = str(raised.exception)
        self.assertIn("newer than this code", message)
        self.assertIn("refusing to run against an unknown schema", message)

    def test_the_database_is_untouched_by_the_refusal(self) -> None:
        from backend.registry.errors import StorageError

        with _SchemaPin(2):
            with self.assertRaises(StorageError):
                Store(self.db)
        # 拒绝之后仍然是那个 v3 库：没有降级、没有半迁移。
        reopened = Store(self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version(), store_module.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
