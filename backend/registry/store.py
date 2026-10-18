"""SQLite 持久化：迁移有版本、写入有事务、查询不带策略。

分成三层是刻意的：

* ``store.py``（本模块）—— 只做**存取**，不做授权判断；
* ``authz.py`` —— 只做**授权判断**，不知道 SQL；
* ``registry.py`` —— 把两者接起来，是唯一同时认识「行」与「谁能看这行」的地方。

把授权判断塞进 SQL 的 ``WHERE`` 里（例如「顺手加个 owner = ?」）很容易在某一条
新查询上漏掉，从而静默地泄漏行级数据。因此可见性判定**只有一处**，且它在 Python
里、对每一行都跑。

迁移用 ``PRAGMA user_version`` 作为权威版本号，另用 ``schema_migrations`` 表留
审计痕迹。打开一个版本**高于**本代码的库会直接失败 —— 拒绝在未知 schema 上跑，
比「读一半字段然后当作默认值」安全得多。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .errors import Conflict, InvalidRequest, StorageError
from .models import (
    DEPLOYMENT_READY,
    ENDPOINT_READY,
    ENGINE_MODE_USER,
    ENGINE_VERIFIED,
    ROUTER_EVIDENCE_KINDS,
    AclGrant,
    AuditEntry,
    Deployment,
    Endpoint,
    EndpointRouting,
    Engine,
    Model,
    isoformat,
    parse_timestamp,
    utc_now,
)

#: 当前 schema 版本。**每次改动表结构都要 +1 并追加一条迁移**。
SCHEMA_VERSION = 3

#: 有序迁移：(版本, SQL)。只追加，永不修改已发布的条目。
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS engines (
            id               TEXT PRIMARY KEY,
            owner_principal  TEXT NOT NULL,
            owner_uid        INTEGER NOT NULL,
            name             TEXT NOT NULL,
            target_json      TEXT NOT NULL,
            protocol         TEXT NOT NULL,
            status           TEXT NOT NULL,
            health_json      TEXT NOT NULL,
            source           TEXT NOT NULL,
            description      TEXT,
            tags_json        TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            last_seen_at     TEXT,
            verified_at      TEXT,
            idempotency_key  TEXT,
            fingerprint      TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS engines_owner_name
            ON engines (owner_principal, name);
        CREATE UNIQUE INDEX IF NOT EXISTS engines_owner_idem
            ON engines (owner_principal, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS engines_owner
            ON engines (owner_principal);

        CREATE TABLE IF NOT EXISTS models (
            id               TEXT PRIMARY KEY,
            owner_principal  TEXT NOT NULL,
            owner_uid        INTEGER NOT NULL,
            name             TEXT NOT NULL,
            weight_path      TEXT,
            weight_sha256    TEXT,
            format           TEXT,
            parameter_count  TEXT,
            status           TEXT NOT NULL,
            health_json      TEXT NOT NULL,
            source           TEXT NOT NULL,
            description      TEXT,
            tags_json        TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            last_seen_at     TEXT,
            idempotency_key  TEXT,
            fingerprint      TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS models_owner_name
            ON models (owner_principal, name);
        CREATE UNIQUE INDEX IF NOT EXISTS models_owner_idem
            ON models (owner_principal, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS models_owner
            ON models (owner_principal);

        CREATE TABLE IF NOT EXISTS deployments (
            id               TEXT PRIMARY KEY,
            owner_principal  TEXT NOT NULL,
            owner_uid        INTEGER NOT NULL,
            name             TEXT NOT NULL,
            engine_id        TEXT NOT NULL,
            model_id         TEXT NOT NULL,
            status           TEXT NOT NULL,
            health_json      TEXT NOT NULL,
            source           TEXT NOT NULL,
            description      TEXT,
            tags_json        TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            last_seen_at     TEXT,
            verified_at      TEXT,
            idempotency_key  TEXT,
            fingerprint      TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS deployments_owner_name
            ON deployments (owner_principal, name);
        CREATE UNIQUE INDEX IF NOT EXISTS deployments_owner_idem
            ON deployments (owner_principal, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS deployments_owner
            ON deployments (owner_principal);

        CREATE TABLE IF NOT EXISTS endpoints (
            id               TEXT PRIMARY KEY,
            owner_principal  TEXT NOT NULL,
            owner_uid        INTEGER NOT NULL,
            name             TEXT NOT NULL,
            deployment_id    TEXT NOT NULL,
            route_path       TEXT NOT NULL,
            visibility       TEXT NOT NULL,
            status           TEXT NOT NULL,
            health_json      TEXT NOT NULL,
            source           TEXT NOT NULL,
            description      TEXT,
            tags_json        TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            last_seen_at     TEXT,
            verified_at      TEXT,
            idempotency_key  TEXT,
            fingerprint      TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS endpoints_owner_name
            ON endpoints (owner_principal, name);
        CREATE UNIQUE INDEX IF NOT EXISTS endpoints_owner_idem
            ON endpoints (owner_principal, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS endpoints_route
            ON endpoints (route_path);
        CREATE INDEX IF NOT EXISTS endpoints_owner
            ON endpoints (owner_principal);

        CREATE TABLE IF NOT EXISTS endpoint_grants (
            endpoint_id       TEXT NOT NULL,
            grantee_principal TEXT NOT NULL,
            granted_by        TEXT NOT NULL,
            granted_at        TEXT NOT NULL,
            PRIMARY KEY (endpoint_id, grantee_principal)
        );

        CREATE INDEX IF NOT EXISTS endpoint_grants_grantee
            ON endpoint_grants (grantee_principal);

        CREATE TABLE IF NOT EXISTS audit_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            at              TEXT NOT NULL,
            actor_principal TEXT NOT NULL,
            actor_uid       INTEGER NOT NULL,
            action          TEXT NOT NULL,
            object_type     TEXT NOT NULL,
            object_id       TEXT NOT NULL,
            detail_json     TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS audit_log_object
            ON audit_log (object_type, object_id);
        CREATE INDEX IF NOT EXISTS audit_log_actor
            ON audit_log (actor_principal);
        """,
    ),
    (
        2,
        # v2：Endpoint 重指向（repoint）。
        #
        # ``revision`` 是乐观并发控制 —— 调用方必须带对它，于是「读-改-写」
        # 之间的竞争变成一次明确的 revision_conflict 而不是静默覆盖。
        # ``router_generation`` 记录运维/Gateway 已经按哪个 revision 取过配置；
        # 它 < revision 时状态就是 pending_router_sync（见 models.router_sync_block）。
        #
        # ``endpoint_routings`` 是**只追加**的：换模型不抹历史，调用记录仍指着旧模型。
        #
        # 字符串默认值刻意不写进 DDL（用 NULL + 代码侧缺省），这样既有的
        # ``_split_statements``（它拒绝带引号的语句）不必放宽。
        """
        ALTER TABLE engines ADD COLUMN mode TEXT;
        ALTER TABLE engines ADD COLUMN ownership_json TEXT;
        ALTER TABLE engines ADD COLUMN verification_json TEXT;

        ALTER TABLE endpoints ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE endpoints ADD COLUMN router_generation INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE IF NOT EXISTS endpoint_routings (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint_id           TEXT NOT NULL,
            revision              INTEGER NOT NULL,
            deployment_id         TEXT NOT NULL,
            model_id              TEXT,
            engine_id             TEXT,
            previous_deployment_id TEXT,
            previous_model_id     TEXT,
            activated_at          TEXT NOT NULL,
            actor_principal       TEXT NOT NULL,
            actor_uid             INTEGER NOT NULL,
            router_generation     INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS endpoint_routings_endpoint
            ON endpoint_routings (endpoint_id, revision);
        """,
    ),
    (
        3,
        # v3：把「代数追上了」和「有证据证明已切流」拆开。
        #
        # v2 只有一个整数 ``router_generation``。代数与 revision 相等时，
        # models.router_sync_block() 一律报 ``router_sync_applied`` —— 无论这个
        # 相等是 Gateway 回读 revision+snapshot SHA 挣来的，还是一个运维
        # POST {"generation": N} 声明出来的。前者有外部证据，后者只有一句话，
        # 两者却共用同一个词（与 docs/architecture.md §5.2 规则 3 相悖）。
        #
        # 新列记录那次回执的**证据类别**。刻意不给 DEFAULT：迁移前的行是 NULL，
        # 而 NULL 一律按「未验证」读（router_sync_attested），不按已切流读 ——
        # 历史行不被提拔，是因为没有人替它们证明过。
        """
        ALTER TABLE endpoints ADD COLUMN router_ack_evidence TEXT;
        """,
    ),
)


@dataclass(frozen=True)
class IdempotencyOutcome:
    """写入的幂等结果：是新行，还是一次重放。"""

    record: object
    created: bool


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(raw: str) -> object:
    return json.loads(raw)


def _loads_or_empty(raw: object) -> dict:
    """读一个 JSON 对象列；NULL / 非对象一律当空 dict。

    v2 新增的列在旧行上是 NULL（迁移里刻意不写字符串默认值），所以这条路径
    必须能吃 NULL，而不是抛。
    """
    if raw is None:
        return {}
    try:
        value = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _split_statements(script: str) -> tuple[str, ...]:
    """把一段 DDL 拆成单条语句。

    为什么不用 ``Connection.executescript``：它在执行前会**隐式提交**已打开的事务，
    于是「一次迁移要么全成要么全不成」这个保证就没了。自己拆、自己放进显式事务里
    逐条执行，才有真正的原子性。

    只支持本文件里那种不含分号字面量的纯 DDL —— 遇到带引号分号会显式报错，
    而不是静默拆错。
    """
    statements = []
    for chunk in script.split(";"):
        stripped = chunk.strip()
        if not stripped:
            continue
        if "'" in stripped or '"' in stripped:
            raise StorageError(
                "migration statement contains a string literal; split it manually "
                "rather than relying on naive statement splitting"
            )
        statements.append(stripped)
    return tuple(statements)


def _serialized(method: Callable) -> Callable:
    """在**内存库**上把一次调用整个包进写锁。

    为什么读也要锁：内存库只有一条连接，而 sqlite3 的连接**不是**并发安全的
    —— 一个线程的 ``SELECT`` 撞进另一个线程刚打开的写事务里，会得到
    ``sqlite3.InterfaceError: bad parameter or other API misuse``（不是
    ``OperationalError``，重试也没用）。文件库不受影响：那种情况下每个线程有
    自己的连接，本就不共享。

    锁是 ``RLock``，所以 ``transaction()`` 里已经持有的锁不会自锁。
    """

    @functools.wraps(method)
    def wrapper(self: "Store", *args: object, **kwargs: object) -> object:
        if self._is_memory:
            with self._lock:
                return method(self, *args, **kwargs)
        return method(self, *args, **kwargs)

    return wrapper


def _serialize_public_methods(cls: type) -> type:
    """给每个公开方法套上 :func:`_serialized`。

    刻意用「自动扫一遍」而不是手工逐条标注：漏标的后果是一个只在并发下才出现、
    无法稳定复现的 InterfaceError，而那种 bug 很难在评审里被看出来。
    """
    for name, attribute in list(vars(cls).items()):
        if name.startswith("_") or isinstance(attribute, (property, staticmethod, classmethod)):
            continue
        if not callable(attribute):
            continue
        setattr(cls, name, _serialized(attribute))
    return cls


@_serialize_public_methods
class Store:
    """一个 SQLite 库上的存取层。线程安全靠一把写锁 + 每线程连接。

    ``:memory:`` 是**唯一**的例外：内存库的每条连接都是一个**独立**的库，
    所以那种情况下共用同一个连接，否则「建了表」的连接与「查表」的连接不是同一个。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._is_memory = self.path == ":memory:"
        self._shared: sqlite3.Connection | None = None
        self._connections: list[sqlite3.Connection] = []
        if not self._is_memory:
            target = Path(self.path)
            if target.parent and not target.parent.exists():
                raise StorageError(f"database directory does not exist: {target.parent}")
        try:
            self._migrate()
        except BaseException:
            # 构造失败的 Store 不该留下一个已经打开的句柄 —— 否则调用方即使
            # 正确地不持有这个对象，文件描述符也已经被泄漏了。
            self.close()
            raise

    # -- 连接管理 ---------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        # ``check_same_thread=False``：``:memory:`` 那条路径上**一个**连接会被多个
        # 线程共用（内存库的每条连接都是独立的库，不能按线程各开一条）。
        # 并发安全由 :func:`_serialized` 提供的串行化保证 —— 不依赖 sqlite
        # 自身的线程模式，因为那保护的是数据竞争，不是「两个线程共用一个事务状态」。
        connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not self._is_memory:
            # WAL：读写不互相阻塞，且崩溃后仍是一致的。
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        with self._lock:
            # 记下来，好让 close() 能关上**所有**线程开的连接。
            # 只关当前线程那一条会让别的线程的连接在 GC 时冒 ResourceWarning。
            self._connections.append(connection)
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        if self._is_memory:
            with self._lock:
                if self._shared is None:
                    self._shared = self._new_connection()
                return self._shared
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._new_connection()
            self._local.connection = connection
        return connection

    def close(self) -> None:
        with self._lock:
            remaining = list(self._connections)
            self._connections.clear()
            self._shared = None
            self._local.connection = None
        for connection in remaining:
            try:
                connection.close()
            except sqlite3.Error:  # pragma: no cover - 关闭失败不该掩盖真正的错误
                pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """一个显式事务。异常即回滚 —— 部分写入不留痕。"""
        with self._lock:
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    # -- 迁移 -------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            connection = self.connection
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > SCHEMA_VERSION:
                raise StorageError(
                    f"database schema version {current} is newer than this code "
                    f"({SCHEMA_VERSION}); refusing to run against an unknown schema"
                )
            if current == SCHEMA_VERSION:
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version    INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                for version, script in MIGRATIONS:
                    if version <= current:
                        continue
                    for statement in _split_statements(script):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_migrations (version, applied_at) "
                        "VALUES (?, ?)",
                        (version, isoformat(utc_now())),
                    )
                    # PRAGMA 不接受占位符；int() 保证这里不可能被注入。
                    connection.execute(f"PRAGMA user_version = {int(version)}")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def applied_migrations(self) -> tuple[int, ...]:
        rows = self.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        return tuple(int(row["version"]) for row in rows)

    # -- 行映射 -----------------------------------------------------------

    @staticmethod
    def _engine(row: sqlite3.Row) -> Engine:
        return Engine(
            id=row["id"],
            owner_principal=row["owner_principal"],
            owner_uid=int(row["owner_uid"]),
            name=row["name"],
            target=_loads(row["target_json"]),
            protocol=row["protocol"],
            status=row["status"],
            health=_loads(row["health_json"]),
            source=row["source"],
            created_at=parse_timestamp(row["created_at"]),
            updated_at=parse_timestamp(row["updated_at"]),
            last_seen_at=parse_timestamp(row["last_seen_at"]),
            description=row["description"],
            tags=tuple(_loads(row["tags_json"])),
            # v2 之前写下的行这些列是 NULL：缺省成「用户登记 + 未证明 + 未验证」，
            # 也就是**最保守**的那一组取值。
            mode=(row["mode"] or ENGINE_MODE_USER),
            ownership=_loads_or_empty(row["ownership_json"]),
            verification=_loads_or_empty(row["verification_json"]),
        )

    @staticmethod
    def _model(row: sqlite3.Row) -> Model:
        return Model(
            id=row["id"],
            owner_principal=row["owner_principal"],
            owner_uid=int(row["owner_uid"]),
            name=row["name"],
            weight_path=row["weight_path"],
            weight_path_sha256=row["weight_sha256"],
            format=row["format"],
            parameter_count=row["parameter_count"],
            status=row["status"],
            health=_loads(row["health_json"]),
            source=row["source"],
            created_at=parse_timestamp(row["created_at"]),
            updated_at=parse_timestamp(row["updated_at"]),
            last_seen_at=parse_timestamp(row["last_seen_at"]),
            description=row["description"],
            tags=tuple(_loads(row["tags_json"])),
        )

    @staticmethod
    def _deployment(row: sqlite3.Row) -> Deployment:
        return Deployment(
            id=row["id"],
            owner_principal=row["owner_principal"],
            owner_uid=int(row["owner_uid"]),
            name=row["name"],
            engine_id=row["engine_id"],
            model_id=row["model_id"],
            status=row["status"],
            health=_loads(row["health_json"]),
            source=row["source"],
            created_at=parse_timestamp(row["created_at"]),
            updated_at=parse_timestamp(row["updated_at"]),
            verified_at=parse_timestamp(row["verified_at"]),
            last_seen_at=parse_timestamp(row["last_seen_at"]),
            description=row["description"],
            tags=tuple(_loads(row["tags_json"])),
        )

    @staticmethod
    def _endpoint(row: sqlite3.Row) -> Endpoint:
        return Endpoint(
            id=row["id"],
            owner_principal=row["owner_principal"],
            owner_uid=int(row["owner_uid"]),
            name=row["name"],
            deployment_id=row["deployment_id"],
            route_path=row["route_path"],
            visibility=row["visibility"],
            status=row["status"],
            health=_loads(row["health_json"]),
            source=row["source"],
            created_at=parse_timestamp(row["created_at"]),
            updated_at=parse_timestamp(row["updated_at"]),
            verified_at=parse_timestamp(row["verified_at"]),
            last_seen_at=parse_timestamp(row["last_seen_at"]),
            description=row["description"],
            tags=tuple(_loads(row["tags_json"])),
            revision=int(row["revision"]),
            router_generation=int(row["router_generation"]),
            router_ack_evidence=row["router_ack_evidence"],
        )

    # -- Engine -----------------------------------------------------------

    def insert_engine(self, engine: Engine, *, idempotency_key: str | None, fingerprint: str) -> None:
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO engines (
                        id, owner_principal, owner_uid, name, target_json, protocol, status,
                        health_json, source, description, tags_json, created_at, updated_at,
                        last_seen_at, verified_at, idempotency_key, fingerprint,
                        mode, ownership_json, verification_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        engine.id,
                        engine.owner_principal,
                        engine.owner_uid,
                        engine.name,
                        _dumps(engine.target),
                        engine.protocol,
                        engine.status,
                        _dumps(engine.health),
                        engine.source,
                        engine.description,
                        _dumps(list(engine.tags)),
                        isoformat(engine.created_at),
                        isoformat(engine.updated_at),
                        isoformat(engine.last_seen_at),
                        None,
                        idempotency_key,
                        fingerprint,
                        engine.mode,
                        _dumps(engine.ownership or {}),
                        _dumps(engine.verification or {}),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"engine conflicts with an existing row: {exc}") from exc

    def engine_by_id(self, engine_id: str) -> Engine | None:
        row = self.connection.execute("SELECT * FROM engines WHERE id = ?", (engine_id,)).fetchone()
        return self._engine(row) if row else None

    def engine_by_owner_name(self, owner: str, name: str) -> Engine | None:
        row = self.connection.execute(
            "SELECT * FROM engines WHERE owner_principal = ? AND name = ?", (owner, name)
        ).fetchone()
        return self._engine(row) if row else None

    def engine_fingerprint(self, engine_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT fingerprint FROM engines WHERE id = ?", (engine_id,)
        ).fetchone()
        return row["fingerprint"] if row else None

    def engine_by_idempotency_key(self, owner: str, key: str) -> Engine | None:
        row = self.connection.execute(
            "SELECT * FROM engines WHERE owner_principal = ? AND idempotency_key = ?",
            (owner, key),
        ).fetchone()
        return self._engine(row) if row else None

    def list_engines(self) -> list[Engine]:
        rows = self.connection.execute("SELECT * FROM engines ORDER BY rowid").fetchall()
        return [self._engine(row) for row in rows]

    def update_engine_status(
        self,
        engine_id: str,
        *,
        status: str,
        health: dict,
        last_seen_at: object,
        updated_at: object,
        ownership: dict | None = None,
        verification: dict | None = None,
        mode: str | None = None,
        source: str | None = None,
    ) -> None:
        # 列名都是本模块写死的字面量，参数一律走占位符 —— 可选片段只影响 SET 列表。
        assignments = [
            "status = ?",
            "health_json = ?",
            "last_seen_at = ?",
            "updated_at = ?",
            "verified_at = CASE WHEN ? = ? THEN ? ELSE verified_at END",
        ]
        params: list[object] = [
            status,
            _dumps(health),
            isoformat(last_seen_at),
            isoformat(updated_at),
            status,
            ENGINE_VERIFIED,
            isoformat(updated_at) if status == ENGINE_VERIFIED else None,
        ]
        if ownership is not None:
            assignments.append("ownership_json = ?")
            params.append(_dumps(ownership))
        if verification is not None:
            assignments.append("verification_json = ?")
            params.append(_dumps(verification))
        if mode is not None:
            assignments.append("mode = ?")
            params.append(mode)
        if source is not None:
            assignments.append("source = ?")
            params.append(source)
        params.append(engine_id)
        with self.transaction() as connection:
            connection.execute(
                f"UPDATE engines SET {', '.join(assignments)} WHERE id = ?", tuple(params)
            )

    # -- Model -----------------------------------------------------------

    def insert_model(self, model: Model, *, idempotency_key: str | None, fingerprint: str) -> None:
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO models (
                        id, owner_principal, owner_uid, name, weight_path, weight_sha256, format,
                        parameter_count, status, health_json, source, description, tags_json,
                        created_at, updated_at, last_seen_at, idempotency_key, fingerprint
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        model.id,
                        model.owner_principal,
                        model.owner_uid,
                        model.name,
                        model.weight_path,
                        model.weight_path_sha256,
                        model.format,
                        model.parameter_count,
                        model.status,
                        _dumps(model.health),
                        model.source,
                        model.description,
                        _dumps(list(model.tags)),
                        isoformat(model.created_at),
                        isoformat(model.updated_at),
                        isoformat(model.last_seen_at),
                        idempotency_key,
                        fingerprint,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"model conflicts with an existing row: {exc}") from exc

    def model_by_id(self, model_id: str) -> Model | None:
        row = self.connection.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        return self._model(row) if row else None

    def model_by_owner_name(self, owner: str, name: str) -> Model | None:
        row = self.connection.execute(
            "SELECT * FROM models WHERE owner_principal = ? AND name = ?", (owner, name)
        ).fetchone()
        return self._model(row) if row else None

    def model_fingerprint(self, model_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT fingerprint FROM models WHERE id = ?", (model_id,)
        ).fetchone()
        return row["fingerprint"] if row else None

    def model_by_idempotency_key(self, owner: str, key: str) -> Model | None:
        row = self.connection.execute(
            "SELECT * FROM models WHERE owner_principal = ? AND idempotency_key = ?",
            (owner, key),
        ).fetchone()
        return self._model(row) if row else None

    def list_models(self) -> list[Model]:
        rows = self.connection.execute("SELECT * FROM models ORDER BY rowid").fetchall()
        return [self._model(row) for row in rows]

    # -- Deployment -------------------------------------------------------

    def insert_deployment(
        self, deployment: Deployment, *, idempotency_key: str | None, fingerprint: str
    ) -> None:
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO deployments (
                        id, owner_principal, owner_uid, name, engine_id, model_id, status,
                        health_json, source, description, tags_json, created_at, updated_at,
                        last_seen_at, verified_at, idempotency_key, fingerprint
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        deployment.id,
                        deployment.owner_principal,
                        deployment.owner_uid,
                        deployment.name,
                        deployment.engine_id,
                        deployment.model_id,
                        deployment.status,
                        _dumps(deployment.health),
                        deployment.source,
                        deployment.description,
                        _dumps(list(deployment.tags)),
                        isoformat(deployment.created_at),
                        isoformat(deployment.updated_at),
                        isoformat(deployment.last_seen_at),
                        isoformat(deployment.verified_at),
                        idempotency_key,
                        fingerprint,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"deployment conflicts with an existing row: {exc}") from exc

    def deployment_by_id(self, deployment_id: str) -> Deployment | None:
        row = self.connection.execute(
            "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
        return self._deployment(row) if row else None

    def deployment_by_owner_name(self, owner: str, name: str) -> Deployment | None:
        row = self.connection.execute(
            "SELECT * FROM deployments WHERE owner_principal = ? AND name = ?", (owner, name)
        ).fetchone()
        return self._deployment(row) if row else None

    def deployment_fingerprint(self, deployment_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT fingerprint FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
        return row["fingerprint"] if row else None

    def deployment_by_idempotency_key(self, owner: str, key: str) -> Deployment | None:
        row = self.connection.execute(
            "SELECT * FROM deployments WHERE owner_principal = ? AND idempotency_key = ?",
            (owner, key),
        ).fetchone()
        return self._deployment(row) if row else None

    def list_deployments(self) -> list[Deployment]:
        rows = self.connection.execute(
            "SELECT * FROM deployments ORDER BY rowid"
        ).fetchall()
        return [self._deployment(row) for row in rows]

    def update_deployment_status(
        self,
        deployment_id: str,
        *,
        status: str,
        health: dict,
        verified_at: object,
        last_seen_at: object,
        updated_at: object,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE deployments SET status = ?, health_json = ?, verified_at = ?, "
                "last_seen_at = ?, updated_at = ? WHERE id = ?",
                (
                    status,
                    _dumps(health),
                    isoformat(verified_at),
                    isoformat(last_seen_at),
                    isoformat(updated_at),
                    deployment_id,
                ),
            )

    # -- Endpoint ---------------------------------------------------------

    def insert_endpoint(
        self, endpoint: Endpoint, *, idempotency_key: str | None, fingerprint: str
    ) -> None:
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO endpoints (
                        id, owner_principal, owner_uid, name, deployment_id, route_path,
                        visibility, status, health_json, source, description, tags_json,
                        created_at, updated_at, last_seen_at, verified_at, idempotency_key,
                        fingerprint
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        endpoint.id,
                        endpoint.owner_principal,
                        endpoint.owner_uid,
                        endpoint.name,
                        endpoint.deployment_id,
                        endpoint.route_path,
                        endpoint.visibility,
                        endpoint.status,
                        _dumps(endpoint.health),
                        endpoint.source,
                        endpoint.description,
                        _dumps(list(endpoint.tags)),
                        isoformat(endpoint.created_at),
                        isoformat(endpoint.updated_at),
                        isoformat(endpoint.last_seen_at),
                        isoformat(endpoint.verified_at),
                        idempotency_key,
                        fingerprint,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"endpoint conflicts with an existing row: {exc}") from exc

            # 初始路由也要落一行。否则「历史」只从第一次换模型开始记，
            # 于是「最初指向哪个模型」既查不到、也回滚不回去 ——
            # 而「回到最初那个」正是最常见的回滚诉求。
            deployment = connection.execute(
                "SELECT model_id, engine_id FROM deployments WHERE id = ?",
                (endpoint.deployment_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO endpoint_routings ("
                "endpoint_id, revision, deployment_id, model_id, engine_id, "
                "previous_deployment_id, previous_model_id, activated_at, "
                "actor_principal, actor_uid, router_generation"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    endpoint.id,
                    int(endpoint.revision),
                    endpoint.deployment_id,
                    deployment["model_id"] if deployment else None,
                    deployment["engine_id"] if deployment else None,
                    None,
                    None,
                    isoformat(endpoint.created_at),
                    endpoint.owner_principal,
                    int(endpoint.owner_uid),
                    int(endpoint.router_generation),
                ),
            )

    def endpoint_by_id(self, endpoint_id: str) -> Endpoint | None:
        row = self.connection.execute(
            "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
        ).fetchone()
        return self._endpoint(row) if row else None

    def endpoint_by_owner_name(self, owner: str, name: str) -> Endpoint | None:
        row = self.connection.execute(
            "SELECT * FROM endpoints WHERE owner_principal = ? AND name = ?", (owner, name)
        ).fetchone()
        return self._endpoint(row) if row else None

    def endpoint_fingerprint(self, endpoint_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT fingerprint FROM endpoints WHERE id = ?", (endpoint_id,)
        ).fetchone()
        return row["fingerprint"] if row else None

    def endpoint_by_idempotency_key(self, owner: str, key: str) -> Endpoint | None:
        row = self.connection.execute(
            "SELECT * FROM endpoints WHERE owner_principal = ? AND idempotency_key = ?",
            (owner, key),
        ).fetchone()
        return self._endpoint(row) if row else None

    def list_endpoints(self) -> list[Endpoint]:
        rows = self.connection.execute("SELECT * FROM endpoints ORDER BY rowid").fetchall()
        return [self._endpoint(row) for row in rows]

    def update_endpoint(
        self,
        endpoint_id: str,
        *,
        visibility: str,
        status: str,
        health: dict,
        verified_at: object,
        last_seen_at: object,
        updated_at: object,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE endpoints SET visibility = ?, status = ?, health_json = ?, "
                "verified_at = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
                (
                    visibility,
                    status,
                    _dumps(health),
                    isoformat(verified_at),
                    isoformat(last_seen_at),
                    isoformat(updated_at),
                    endpoint_id,
                ),
            )

    # -- Endpoint 重指向（repoint）-----------------------------------------

    def repoint_endpoint_atomic(
        self,
        *,
        endpoint_id: str,
        expected_revision: int,
        new_deployment_id: str,
        require_same_owner: bool,
        require_ownership: bool,
        require_protocol: bool,
        actor_principal: str,
        actor_uid: int,
        at: object,
        reason: str | None,
        desired_visibility: str,
        desired_grantees: tuple[str, ...],
    ) -> dict:
        """把 Endpoint 的 ``deployment_id`` 换掉，**全部在一个事务里**。

        为什么这一整段必须在**同一个** BEGIN IMMEDIATE 里：

        * 校验用的「目标 Deployment 现在是 ready」「引擎现在归属已证明」如果发生在
          事务之外，那么在读与写之间目标就可能变了 —— 典型的 TOCTOU；
        * 授权集合（ACL）的重置必须与路由切换**同生共死**。分两次写意味着存在一个
          窗口，新模型正在服务、而旧的共享名单还挂着 —— 那正是「静默把 ACL 带到
          新模型」的实现方式。

        返回一个 ``outcome`` 字典；调用方（服务层）负责把它翻成契约错误。
        **本方法不做授权判断**（谁有权改）—— 那是 ``authz`` 的事，在进来之前就做完了；
        这里做的是「这个改动现在还成立吗」。
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            if row is None:
                return {"outcome": "endpoint_missing"}

            current_revision = int(row["revision"])
            if current_revision != expected_revision:
                return {
                    "outcome": "revision_mismatch",
                    "current_revision": current_revision,
                    "expected_revision": expected_revision,
                }

            owner_principal = row["owner_principal"]
            previous_deployment_id = row["deployment_id"]

            target = connection.execute(
                "SELECT * FROM deployments WHERE id = ?", (new_deployment_id,)
            ).fetchone()
            if target is None:
                return {"outcome": "target_missing"}
            if require_same_owner and target["owner_principal"] != owner_principal:
                # 一个 Endpoint 只能指向**同一个 owner** 的 Deployment。
                # 跨 owner 会变成一种「冒领」：把别人的算力挂到自己的对外路径上。
                return {
                    "outcome": "target_cross_owner",
                    "target_owner": target["owner_principal"],
                    "endpoint_owner": owner_principal,
                }
            if target["status"] != DEPLOYMENT_READY:
                return {"outcome": "target_not_ready", "target_status": target["status"]}

            engine = connection.execute(
                "SELECT * FROM engines WHERE id = ?", (target["engine_id"],)
            ).fetchone()
            if engine is None:
                return {"outcome": "engine_missing"}
            if engine["status"] != ENGINE_VERIFIED:
                return {"outcome": "engine_not_verified", "engine_status": engine["status"]}

            ownership = _loads_or_empty(engine["ownership_json"])
            verification = _loads_or_empty(engine["verification_json"])
            if require_ownership and not ownership.get("verified"):
                return {"outcome": "ownership_unverified", "ownership": ownership}
            if require_protocol and not verification.get("protocol_verified"):
                return {"outcome": "protocol_unverified", "verification": verification}

            previous_model_id: str | None = None
            if previous_deployment_id:
                previous = connection.execute(
                    "SELECT model_id FROM deployments WHERE id = ?", (previous_deployment_id,)
                ).fetchone()
                previous_model_id = previous["model_id"] if previous else None

            previous_grantees = tuple(
                item["grantee_principal"]
                for item in connection.execute(
                    "SELECT grantee_principal FROM endpoint_grants WHERE endpoint_id = ? "
                    "ORDER BY grantee_principal",
                    (endpoint_id,),
                ).fetchall()
            )

            new_revision = current_revision + 1
            updated_at = isoformat(at)
            cursor = connection.execute(
                "UPDATE endpoints SET deployment_id = ?, visibility = ?, revision = ?, "
                "status = ?, health_json = ?, verified_at = ?, last_seen_at = ?, "
                "updated_at = ? WHERE id = ? AND revision = ?",
                (
                    new_deployment_id,
                    desired_visibility,
                    new_revision,
                    ENDPOINT_READY,
                    target["health_json"],
                    updated_at,
                    updated_at,
                    updated_at,
                    endpoint_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - 同一事务内理论不可达
                return {
                    "outcome": "revision_mismatch",
                    "current_revision": current_revision,
                    "expected_revision": expected_revision,
                }

            # ACL 整体重置：默认回到 private，或在**同一事务**里换上调用方显式
            # 重新授权的那一组。绝不让旧的共享名单顺延到新模型上。
            connection.execute("DELETE FROM endpoint_grants WHERE endpoint_id = ?", (endpoint_id,))
            for grantee in desired_grantees:
                connection.execute(
                    "INSERT OR REPLACE INTO endpoint_grants "
                    "(endpoint_id, grantee_principal, granted_by, granted_at) VALUES (?,?,?,?)",
                    (endpoint_id, grantee, actor_principal, updated_at),
                )

            connection.execute(
                "INSERT INTO endpoint_routings ("
                "endpoint_id, revision, deployment_id, model_id, engine_id, "
                "previous_deployment_id, previous_model_id, activated_at, "
                "actor_principal, actor_uid, router_generation"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    endpoint_id,
                    new_revision,
                    new_deployment_id,
                    target["model_id"],
                    target["engine_id"],
                    previous_deployment_id,
                    previous_model_id,
                    updated_at,
                    actor_principal,
                    int(actor_uid),
                    int(row["router_generation"]),
                ),
            )

            granted = tuple(sorted(set(desired_grantees) - set(previous_grantees)))
            revoked = tuple(sorted(set(previous_grantees) - set(desired_grantees)))
            reestablished = tuple(sorted(set(previous_grantees) & set(desired_grantees)))

            def audit(action: str, detail: dict) -> None:
                connection.execute(
                    "INSERT INTO audit_log (at, actor_principal, actor_uid, action, "
                    "object_type, object_id, detail_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        updated_at,
                        actor_principal,
                        int(actor_uid),
                        action,
                        "endpoint",
                        endpoint_id,
                        _dumps(detail),
                    ),
                )

            # 一条 repoint 记录**旧/新**两侧的 deployment 与 model：这是「换了什么」。
            audit(
                "repoint",
                {
                    "reason": reason,
                    "revision_from": current_revision,
                    "revision_to": new_revision,
                    "previous_deployment_id": previous_deployment_id,
                    "previous_model_id": previous_model_id,
                    "deployment_id": new_deployment_id,
                    "model_id": target["model_id"],
                    "engine_id": target["engine_id"],
                    "visibility_after": desired_visibility,
                    "grantees_after": list(desired_grantees),
                    "acl_carried_over": False,
                },
            )
            # 授权集合被**整体重建**了（DELETE + INSERT），所以每一个保留下来的人
            # 也在这件事里被重新授权了一次。只记增量会让审计看起来「ACL 没动过」——
            # 而它其实被拆掉重建了，授权依据从旧模型换成了新模型。
            for grantee in desired_grantees:
                audit("grant", {"grantee": grantee, "reason": "repoint_reauthorization"})
            for grantee in revoked:
                audit(
                    "revoke",
                    {"grantee": grantee, "reason": "repoint_acl_reset"},
                )

            refreshed = connection.execute(
                "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            return {
                "outcome": "ok",
                "endpoint": self._endpoint(refreshed),
                "previous_deployment_id": previous_deployment_id,
                "previous_model_id": previous_model_id,
                "deployment_id": new_deployment_id,
                "model_id": target["model_id"],
                "engine_id": target["engine_id"],
                "revision_from": current_revision,
                "revision_to": new_revision,
                "granted": list(granted),
                "revoked": list(revoked),
                "reestablished": list(reestablished),
                "visibility": desired_visibility,
                "operator_blessed": bool((_loads_or_empty(engine["target_json"]) or {}).get("operator_blessed")),
            }

    def endpoint_routings(self, endpoint_id: str, *, limit: int = 50) -> list[EndpointRouting]:
        """只追加的路由历史，最新在前。旧行**永不删除**。"""
        rows = self.connection.execute(
            "SELECT * FROM endpoint_routings WHERE endpoint_id = ? "
            "ORDER BY revision DESC, id DESC LIMIT ?",
            (endpoint_id, int(limit)),
        ).fetchall()
        return [
            EndpointRouting(
                id=int(row["id"]),
                endpoint_id=row["endpoint_id"],
                revision=int(row["revision"]),
                deployment_id=row["deployment_id"],
                model_id=row["model_id"],
                engine_id=row["engine_id"],
                previous_deployment_id=row["previous_deployment_id"],
                previous_model_id=row["previous_model_id"],
                activated_at=parse_timestamp(row["activated_at"]),
                actor_principal=row["actor_principal"],
                actor_uid=int(row["actor_uid"]),
                router_generation=int(row["router_generation"]),
            )
            for row in rows
        ]

    def previous_distinct_deployment(self, endpoint_id: str, current_deployment_id: str) -> str | None:
        """回滚用：历史上最近一个**与当前不同**的 deployment。

        回滚不是特权操作，只是「再重指向一次」—— 所以这里只回答「上一个是哪个」，
        至于它现在还 ready 不 ready，由 ``repoint_endpoint_atomic`` 用**同一套**检查
        重新判定。历史里那个目标此刻不可用，回滚就该失败。
        """
        row = self.connection.execute(
            "SELECT deployment_id FROM endpoint_routings "
            "WHERE endpoint_id = ? AND deployment_id != ? "
            "ORDER BY revision DESC, id DESC LIMIT 1",
            (endpoint_id, current_deployment_id),
        ).fetchone()
        return row["deployment_id"] if row else None

    def ack_router_generation(
        self,
        endpoint_id: str,
        *,
        generation: int,
        actor_principal: str,
        actor_uid: int,
        at: object,
        evidence: str,
    ) -> dict:
        """记录一次「已按第 generation 号取过配置」的回执，连同它的**证据类别**。

        刻意要求 ``generation`` 与当前 revision **精确相等**：一个落后的 ack 会让
        系统误以为已经生效，从而在运维不知情的情况下又回到「谎报已切换」。

        ``evidence`` 是必填的，且必须是封闭词表里的一个：调用方必须当场说清楚
        这次回执**凭什么**。代数和 revision 相等只说明「代数追平了」，不说明
        「线上在跑新模型」—— 只有 ``gateway_readback`` 才意味着 Gateway 自己
        发布的 revision 与 snapshot SHA 与本次快照逐位相等。
        """
        if evidence not in ROUTER_EVIDENCE_KINDS:
            raise InvalidRequest(
                f"evidence must be one of {sorted(ROUTER_EVIDENCE_KINDS)}, got {evidence!r}"
            )
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            if row is None:
                return {"outcome": "endpoint_missing"}
            current = int(row["revision"])
            if int(generation) != current:
                return {
                    "outcome": "generation_mismatch",
                    "current_revision": current,
                    "requested_generation": int(generation),
                }
            connection.execute(
                "UPDATE endpoints SET router_generation = ?, router_ack_evidence = ? "
                "WHERE id = ?",
                (int(generation), evidence, endpoint_id),
            )
            connection.execute(
                "INSERT INTO audit_log (at, actor_principal, actor_uid, action, "
                "object_type, object_id, detail_json) VALUES (?,?,?,?,?,?,?)",
                (
                    isoformat(at),
                    actor_principal,
                    int(actor_uid),
                    "router_sync_ack",
                    "endpoint",
                    endpoint_id,
                    _dumps({"generation": int(generation), "evidence": evidence}),
                ),
            )
            refreshed = connection.execute(
                "SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)
            ).fetchone()
            return {"outcome": "ok", "endpoint": self._endpoint(refreshed)}

    # -- ACL --------------------------------------------------------------

    def grantees(self, endpoint_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT grantee_principal FROM endpoint_grants WHERE endpoint_id = ? "
            "ORDER BY grantee_principal",
            (endpoint_id,),
        ).fetchall()
        return tuple(row["grantee_principal"] for row in rows)

    def endpoint_ids_granted_to(self, grantee: str) -> frozenset[str]:
        rows = self.connection.execute(
            "SELECT endpoint_id FROM endpoint_grants WHERE grantee_principal = ?", (grantee,)
        ).fetchall()
        return frozenset(row["endpoint_id"] for row in rows)

    def grant(self, endpoint_id: str, grantee: str, *, granted_by: str, granted_at: object) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO endpoint_grants "
                "(endpoint_id, grantee_principal, granted_by, granted_at) VALUES (?,?,?,?)",
                (endpoint_id, grantee, granted_by, isoformat(granted_at)),
            )

    def revoke(self, endpoint_id: str, grantee: str) -> bool:
        """删除授权行。返回是否真的删掉了一条。

        刻意用 **DELETE** 而不是 ``revoked_at`` 打标：留一行「已撤销」意味着每次
        读权限的查询都必须记得过滤它 —— 漏掉一次就是一次静默的越权。没有行就没有
        权限，这是结构上的保证。撤销这件事由 ``audit_log`` 留痕。
        """
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM endpoint_grants WHERE endpoint_id = ? AND grantee_principal = ?",
                (endpoint_id, grantee),
            )
            return cursor.rowcount > 0

    def revoke_all(self, endpoint_id: str) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM endpoint_grants WHERE endpoint_id = ?", (endpoint_id,)
            )
            return cursor.rowcount

    def grant_history(self, endpoint_id: str) -> tuple[AclGrant, ...]:
        """从审计里重建曾经的授权（含已撤销），供管理员查看。"""
        rows = self.connection.execute(
            # ``action`` 必须在 SELECT 里：下面要用它区分 grant 与 revoke，
            # 漏选会在运行时才炸成 IndexError（sqlite3.Row 缺列就是 IndexError）。
            "SELECT at, action, actor_principal, detail_json FROM audit_log "
            "WHERE object_type = 'endpoint' AND object_id = ? AND action IN ('grant','revoke') "
            "ORDER BY id",
            (endpoint_id,),
        ).fetchall()
        history: list[AclGrant] = []
        for row in rows:
            detail = _loads(row["detail_json"])
            if not isinstance(detail, dict) or "grantee" not in detail:
                continue
            is_revoke = row["action"] == "revoke"
            history.append(
                AclGrant(
                    endpoint_id=endpoint_id,
                    grantee_principal=str(detail["grantee"]),
                    granted_by=row["actor_principal"],
                    granted_at=parse_timestamp(row["at"]),
                    revoked_at=parse_timestamp(row["at"]) if is_revoke else None,
                    revoked_by=row["actor_principal"] if is_revoke else None,
                )
            )
        return tuple(history)

    # -- 审计 -------------------------------------------------------------

    def append_audit(
        self,
        *,
        at: object,
        actor_principal: str,
        actor_uid: int,
        action: str,
        object_type: str,
        object_id: str,
        detail: dict | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_log "
                "(at, actor_principal, actor_uid, action, object_type, object_id, detail_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    isoformat(at),
                    actor_principal,
                    int(actor_uid),
                    action,
                    object_type,
                    object_id,
                    _dumps(detail or {}),
                ),
            )

    def list_audit(
        self, *, object_type: str | None = None, object_id: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        query = "SELECT * FROM audit_log"
        clauses: list[str] = []
        params: list[object] = []
        if object_type is not None:
            clauses.append("object_type = ?")
            params.append(object_type)
        if object_id is not None:
            clauses.append("object_id = ?")
            params.append(object_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.connection.execute(query, tuple(params)).fetchall()
        return [
            AuditEntry(
                id=int(row["id"]),
                at=parse_timestamp(row["at"]),
                actor_principal=row["actor_principal"],
                actor_uid=int(row["actor_uid"]),
                action=row["action"],
                object_type=row["object_type"],
                object_id=row["object_id"],
                detail=_loads(row["detail_json"]),
            )
            for row in rows
        ]


def ensure_contained(path: str | Path, root: str | Path) -> Path:
    """把 ``path`` 解析到 ``root`` 之内，否则抛 ``StorageError``。

    用于任何「数据库路径来自配置/请求」的场景，避免写到意料之外的位置。
    """
    resolved_root = Path(root).resolve()
    resolved = (resolved_root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise StorageError(f"path escapes the allowed root: {resolved}")
    return resolved


def count_rows(store: Store, table: str) -> int:
    """只允许白名单表名（表名不能用参数化占位符，所以必须校验）。"""
    allowed = {"engines", "models", "deployments", "endpoints", "endpoint_grants", "audit_log"}
    if table not in allowed:
        raise StorageError(f"unknown table: {table!r}")
    return int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def sequence_versions() -> Sequence[int]:
    """已发布迁移的版本序列（供测试断言连续性）。"""
    return tuple(version for version, _ in MIGRATIONS)


def tables(store: Store) -> Iterable[str]:
    rows = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return tuple(row["name"] for row in rows)
