"""网关的持久状态：虚拟密钥、模型路由、调用元数据。一个 SQLite 文件（0600）。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

KEY_PREFIX = "sk-lab-"
VISIBILITY_ALL = "all"
VISIBILITY_PRIVATE = "private"
VISIBILITIES = (VISIBILITY_ALL, VISIBILITY_PRIVATE)

DEFAULT_RPM = 60
DEFAULT_CONCURRENCY = 2
MAX_RPM = 6000
MAX_CONCURRENCY = 16
MAX_KEYS_PER_PRINCIPAL = 50
BODY_LIMIT_BYTES = 64 * 1024
BODY_RETENTION_SECONDS = 7 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    id            TEXT PRIMARY KEY,
    key_hash      TEXT NOT NULL UNIQUE,
    prefix        TEXT NOT NULL,
    principal     TEXT NOT NULL,
    label         TEXT,
    models_json   TEXT,
    rpm           INTEGER NOT NULL,
    concurrency   INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL,
    revoked_at    REAL,
    last_used_at  REAL,
    log_bodies    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS keys_by_principal ON keys(principal);

CREATE TABLE IF NOT EXISTS routes (
    id              TEXT PRIMARY KEY,
    alias           TEXT NOT NULL UNIQUE,
    engine_id       TEXT NOT NULL,
    upstream_model  TEXT,
    owner           TEXT NOT NULL,
    visibility      TEXT NOT NULL,
    enabled         INTEGER NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    request_id        TEXT NOT NULL,
    key_id            TEXT,
    principal         TEXT NOT NULL,
    route_id          TEXT,
    model             TEXT,
    route_owner       TEXT,
    path              TEXT NOT NULL,
    status            INTEGER,
    outcome           TEXT NOT NULL,
    stream            INTEGER NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    ttft_ms           REAL,
    total_ms          REAL,
    channel           TEXT NOT NULL,
    error             TEXT,
    has_body          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_by_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS usage_by_principal ON usage(principal, ts);
CREATE INDEX IF NOT EXISTS usage_by_owner ON usage(route_owner, ts);
CREATE INDEX IF NOT EXISTS usage_by_key ON usage(key_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS usage_by_request ON usage(request_id);

-- 只有密钥所有者为某把密钥打开「记录请求内容」时才写这里；按 BODY_RETENTION_SECONDS 清理。
CREATE TABLE IF NOT EXISTS bodies (
    request_id  TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    request     TEXT,
    response    TEXT,
    truncated   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS bodies_by_ts ON bodies(ts);
"""


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_plaintext_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class KeyRecord:
    id: str
    prefix: str
    principal: str
    label: str | None
    models: tuple[str, ...] | None
    rpm: int
    concurrency: int
    created_at: float
    expires_at: float | None
    revoked_at: float | None
    last_used_at: float | None
    log_bodies: bool = False

    def live(self, now: float) -> bool:
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)


@dataclass(frozen=True)
class Route:
    id: str
    alias: str
    engine_id: str
    upstream_model: str | None
    owner: str
    visibility: str
    enabled: bool
    created_by: str
    created_at: float
    updated_at: float


class GatewayStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover
            pass

    def _rows(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, tuple(args)).fetchall()
        finally:
            conn.close()

    def _exec(self, sql: str, args: Iterable | dict = ()) -> int:
        conn = self._connect()
        try:
            params = args if isinstance(args, dict) else tuple(args)
            return conn.execute(sql, params).rowcount or 0
        finally:
            conn.close()

    # --- keys ----------------------------------------------------------------

    def create_key(
        self,
        *,
        principal: str,
        label: str | None,
        models: tuple[str, ...] | None,
        rpm: int,
        concurrency: int,
        expires_at: float | None,
        log_bodies: bool = False,
    ) -> tuple[KeyRecord, str]:
        plaintext = new_plaintext_key()
        key_id = "key_" + secrets.token_hex(6)
        now = self._clock()
        self._exec(
            "INSERT INTO keys (id, key_hash, prefix, principal, label, models_json, rpm, concurrency, "
            "created_at, expires_at, log_bodies) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key_id,
                digest(plaintext),
                plaintext[: len(KEY_PREFIX) + 4],
                principal,
                label,
                json.dumps(list(models)) if models is not None else None,
                rpm,
                concurrency,
                now,
                expires_at,
                1 if log_bodies else 0,
            ),
        )
        record = self.key_by_id(key_id)
        assert record is not None
        return record, plaintext

    def key_by_id(self, key_id: str) -> KeyRecord | None:
        rows = self._rows("SELECT * FROM keys WHERE id = ?", (key_id,))
        return _key(rows[0]) if rows else None

    def key_by_plaintext(self, plaintext: str) -> KeyRecord | None:
        rows = self._rows("SELECT * FROM keys WHERE key_hash = ?", (digest(plaintext),))
        return _key(rows[0]) if rows else None

    def keys_for(self, principal: str | None) -> list[KeyRecord]:
        if principal is None:
            rows = self._rows("SELECT * FROM keys ORDER BY created_at DESC")
        else:
            rows = self._rows("SELECT * FROM keys WHERE principal = ? ORDER BY created_at DESC", (principal,))
        return [_key(row) for row in rows]

    def count_live_keys(self, principal: str) -> int:
        now = self._clock()
        rows = self._rows(
            "SELECT COUNT(*) FROM keys WHERE principal = ? AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (principal, now),
        )
        return int(rows[0][0])

    def revoke_key(self, key_id: str) -> bool:
        return self._exec(
            "UPDATE keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (self._clock(), key_id)
        ) > 0

    def revoke_all_for(self, principal: str) -> int:
        return self._exec(
            "UPDATE keys SET revoked_at = ? WHERE principal = ? AND revoked_at IS NULL", (self._clock(), principal)
        )

    def update_key(self, key_id: str, *, label: str | None = None, log_bodies: bool | None = None) -> None:
        if label is not None:
            self._exec("UPDATE keys SET label = ? WHERE id = ?", (label or None, key_id))
        if log_bodies is not None:
            self._exec("UPDATE keys SET log_bodies = ? WHERE id = ?", (1 if log_bodies else 0, key_id))

    def touch_key(self, key_id: str) -> None:
        self._exec("UPDATE keys SET last_used_at = ? WHERE id = ?", (self._clock(), key_id))

    # --- routes --------------------------------------------------------------

    def create_route(
        self, *, alias: str, engine_id: str, upstream_model: str | None, owner: str, visibility: str, created_by: str
    ) -> Route:
        route_id = "rt_" + secrets.token_hex(6)
        now = self._clock()
        try:
            self._exec(
                "INSERT INTO routes (id, alias, engine_id, upstream_model, owner, visibility, enabled, created_by, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (route_id, alias, engine_id, upstream_model, owner, visibility, created_by, now, now),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"model name {alias!r} is already published") from None
        route = self.route_by_id(route_id)
        assert route is not None
        return route

    def route_by_id(self, route_id: str) -> Route | None:
        rows = self._rows("SELECT * FROM routes WHERE id = ?", (route_id,))
        return _route(rows[0]) if rows else None

    def route_by_alias(self, alias: str) -> Route | None:
        rows = self._rows("SELECT * FROM routes WHERE alias = ?", (alias,))
        return _route(rows[0]) if rows else None

    def routes(self) -> list[Route]:
        return [_route(row) for row in self._rows("SELECT * FROM routes ORDER BY created_at")]

    def update_route(self, route_id: str, *, visibility: str | None = None, enabled: bool | None = None,
                     upstream_model: str | None = None) -> None:
        sets, args = [], []
        if visibility is not None:
            sets.append("visibility = ?")
            args.append(visibility)
        if enabled is not None:
            sets.append("enabled = ?")
            args.append(1 if enabled else 0)
        if upstream_model is not None:
            sets.append("upstream_model = ?")
            args.append(upstream_model or None)
        if not sets:
            return
        sets.append("updated_at = ?")
        args.append(self._clock())
        args.append(route_id)
        self._exec(f"UPDATE routes SET {', '.join(sets)} WHERE id = ?", args)

    def delete_route(self, route_id: str) -> None:
        self._exec("DELETE FROM routes WHERE id = ?", (route_id,))

    # --- usage ---------------------------------------------------------------

    def record(self, row: dict) -> None:
        self._exec(
            "INSERT INTO usage (ts, request_id, key_id, principal, route_id, model, route_owner, path, status, "
            "outcome, stream, prompt_tokens, completion_tokens, ttft_ms, total_ms, channel, error, has_body) "
            "VALUES (:ts, :request_id, :key_id, :principal, :route_id, :model, :route_owner, :path, :status, "
            ":outcome, :stream, :prompt_tokens, :completion_tokens, :ttft_ms, :total_ms, :channel, :error, :has_body)",
            {"error": None, "has_body": 0, **row},
        )

    def record_body(self, request_id: str, *, request: str | None, response: str | None, truncated: bool) -> None:
        now = self._clock()
        self._exec("DELETE FROM bodies WHERE ts < ?", (now - BODY_RETENTION_SECONDS,))
        self._exec(
            "INSERT OR REPLACE INTO bodies (request_id, ts, request, response, truncated) VALUES (?, ?, ?, ?, ?)",
            (request_id, now, request, response, 1 if truncated else 0),
        )

    def body_for(self, request_id: str) -> sqlite3.Row | None:
        rows = self._rows("SELECT * FROM bodies WHERE request_id = ? AND ts >= ?",
                          (request_id, self._clock() - BODY_RETENTION_SECONDS))
        return rows[0] if rows else None

    def usage_by_request(self, request_id: str) -> sqlite3.Row | None:
        rows = self._rows("SELECT * FROM usage WHERE request_id = ?", (request_id,))
        return rows[0] if rows else None

    def usage_rows(
        self,
        *,
        since: float,
        principal: str | None = None,
        route_owner: str | None = None,
        model: str | None = None,
        outcome: str | None = None,
        key_id: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        where, args = ["ts >= ?"], [since]
        if principal is not None:
            where.append("principal = ?")
            args.append(principal)
        if route_owner is not None:
            where.append("route_owner = ?")
            args.append(route_owner)
        if model:
            where.append("model = ?")
            args.append(model)
        if outcome:
            where.append("outcome = ?")
            args.append(outcome)
        if key_id:
            where.append("key_id = ?")
            args.append(key_id)
        sql = f"SELECT * FROM usage WHERE {' AND '.join(where)} ORDER BY ts DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, args)


def _key(row: sqlite3.Row) -> KeyRecord:
    models = json.loads(row["models_json"]) if row["models_json"] else None
    return KeyRecord(
        id=row["id"],
        prefix=row["prefix"],
        principal=row["principal"],
        label=row["label"],
        models=tuple(models) if models is not None else None,
        rpm=int(row["rpm"]),
        concurrency=int(row["concurrency"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
        log_bodies=bool(row["log_bodies"]),
    )


def _route(row: sqlite3.Row) -> Route:
    return Route(
        id=row["id"],
        alias=row["alias"],
        engine_id=row["engine_id"],
        upstream_model=row["upstream_model"],
        owner=row["owner"],
        visibility=row["visibility"],
        enabled=bool(row["enabled"]),
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
