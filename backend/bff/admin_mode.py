"""共享管理员模式：一个全平台共享的口令，换一段限时的管理员会话。

Atelier V6 的权限模型只有一句话：**登录决定你是谁，管理员模式决定你此刻能不能
看全局。** 两者是两件事——

* 任何获准的成员用自己的 Ubuntu 账号通过 PAM 登录后，**默认都是 `member`**；
  ``carol`` 即使在 Dell 上是基础设施管理员、即使 root-owned 的授权文件把他标成
  ``platform_admin``，他在 Portal 的普通会话里**仍然是 `member`**。
* 只有在**自己的会话里**输入那个全平台共享的独立管理口令，才能进入管理员模式，
  并且**限时**（默认 900 秒，硬上限 900 秒 = 15 分钟）。退出、过期、登出、口令
  轮换都会立刻回到 `member`。

要说清楚的三件事
----------------

**这不是一个独立主体。** 管理员模式只是既有会话上的一个标记，不产生新的身份、
不产生新的 UID。审计里的 actor 永远是原来的 Linux uid / 登录名，外加一个
``admin_mode`` 布尔位——把一个临时开关写成"另一个账号"，正是让事后审计说不清
「这件事到底是谁做的」的原因。

**默认 NOT_CONFIGURED，而且这就是"无人可提升"。** 口令散列只由运维在 owner 侧
交互式写入 ``0600`` 的私有 state（见本模块的 CLI）。仓库里没有、也不允许有默认
口令或演示口令；文件缺失不是"用默认值"，是**没有人能进入管理员模式**。

**状态按会话绑定。** 提权行以 ``session_hash`` 为主键——库里存的是会话 cookie 的
摘要，不是 cookie 本身（与 ``backend.auth.sessions`` 同一纪律）。因此同一个人的
两条会话彼此隔离：一条提权，另一条仍然是 member；登出删掉那一行，重复使用旧
cookie 也不会重新拿到管理员视图。

口令的处理
----------
口令只在这一次请求的内存里存在：不写日志、不进审计、不进异常、不回显（``repr``
里没有它）。校验复用 ``backend.auth.hashing``（scrypt/argon2id、每口令独立盐、
``hmac.compare_digest`` 恒定时间比较），并额外要求**散列参数不低于安全下限**——
一个被换成 ``n=2`` 的口令文件是拒绝加载的，而不是"照验不误"。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import os
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..auth import hashing
from ..auth.errors import RateLimited as AuthRateLimited
from ..auth.ratelimit import RateLimiter

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 提权时长。默认与硬上限**都是** 900 秒：15 分钟是需求写死的天花板，
#: 允许配置更长就等于把这个天花板变成一个可以自己拆掉的装饰。
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 900
MIN_TTL_SECONDS = 60

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_LOCKOUT_SECONDS = 900

#: 与 ``backend.auth.local_admin`` 同名的状态词，语义也一致。
STATE_MISSING = "missing"
STATE_DISABLED = "disabled"
STATE_ACTIVE = "active"
STATE_BROKEN = "broken"

DISABLED_SENTINEL = "disabled"
MAX_CREDENTIAL_FILE_BYTES = 1024

DEFAULT_CREDENTIAL_FILE_NAME = "admin_mode.hash"
DEFAULT_STORE_FILE_NAME = "admin_mode.sqlite3"

#: 失败计数的 scope。与 local_login / feishu_* 分开，互不消耗。
SCOPE_ADMIN_ENTER = "admin_mode_enter"

#: 散列参数的安全下限。低于此值的口令文件被判定为 broken（拒绝加载），
#: 而不是"能验就行"——一个被悄悄削弱的散列，比没有散列更难发现。
MIN_SCRYPT_N = hashing.SCRYPT_N  # 16384
MIN_SCRYPT_R = hashing.SCRYPT_R  # 8
MIN_SCRYPT_P = hashing.SCRYPT_P  # 1
MIN_ARGON2_MEMORY_KIB = 65536
MIN_ARGON2_TIME_COST = 3
MIN_ARGON2_PARALLELISM = 4

ENV_ENABLED = "LLM_LAB_ADMIN_MODE_ENABLED"
ENV_HASH_FILE = "LLM_LAB_ADMIN_MODE_HASH_FILE"
ENV_STATE_DB = "LLM_LAB_ADMIN_MODE_STATE_DB"
ENV_TTL_SECONDS = "LLM_LAB_ADMIN_MODE_TTL_SECONDS"
ENV_MAX_FAILURES = "LLM_LAB_ADMIN_MODE_MAX_FAILURES"
ENV_WINDOW_SECONDS = "LLM_LAB_ADMIN_MODE_WINDOW_SECONDS"
ENV_LOCKOUT_SECONDS = "LLM_LAB_ADMIN_MODE_LOCKOUT_SECONDS"


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------


class AdminModeError(Exception):
    """管理员模式的失败。``code`` 是对前端的稳定 token，``message`` 面向人。"""

    status = 500
    code = "admin_mode_error"
    message = "Admin mode is unavailable."

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(self.message)
        # 仅供服务端日志。绝不序列化，绝不带上请求材料。
        self.detail = detail


class NotConfigured(AdminModeError):
    """没有管理口令（或它被停用了）。**这是默认状态**，含义是"无人可提升"。"""

    status = 503
    code = "admin_mode_not_configured"
    message = "Admin mode is not configured on this server."


class InvalidCredentials(AdminModeError):
    """口令不对。与"未配置"刻意分开，但文案对探测者没有任何信息量。"""

    status = 401
    code = "invalid_credentials"
    message = "Authentication failed."


class InvalidRequest(AdminModeError):
    status = 400
    code = "invalid_request"
    message = "The request is malformed."


class RateLimited(AdminModeError):
    status = 429
    code = "rate_limited"
    message = "Too many attempts. Please try again later."

    def __init__(self, detail: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class Misconfigured(AdminModeError):
    status = 500
    code = "admin_mode_misconfigured"
    message = "Admin mode configuration is invalid."


# --------------------------------------------------------------------------
# 口令文件的读取（与 backend.auth.local_admin 同一套硬化）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileFacts:
    exists: bool
    is_regular: bool
    mode: int
    size: int
    uid: int | None
    is_symlink: bool = False


def default_fs_probe(path: Path) -> FileFacts:
    """Facts about the credential file, **without following a symlink**.

    ``lstat`` rather than ``stat``: this is deliberately stricter than
    ``backend.auth.local_admin``, which follows links. A symlink here would mean
    the file whose mode and owner we just checked is not the file whose bytes we
    are about to trust, and "the path is operator-supplied" is not a reason to
    accept that — the path is supplied once, at configuration time, and read on
    every attempt afterwards.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return FileFacts(exists=False, is_regular=False, mode=0, size=0, uid=None)
    except OSError as exc:
        raise Misconfigured(detail=f"cannot stat the credential file: {exc}") from None
    return FileFacts(
        exists=True,
        is_regular=stat.S_ISREG(info.st_mode),
        mode=stat.S_IMODE(info.st_mode),
        size=info.st_size,
        uid=getattr(info, "st_uid", None),
        is_symlink=stat.S_ISLNK(info.st_mode),
    )


def _process_uid() -> int | None:
    getuid = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - Windows
        return None
    try:
        return int(getuid())
    except OSError:  # pragma: no cover - defensive
        return None


def scrypt_params(encoded: object) -> tuple[int, int, int] | None:
    """``scrypt$n=..,r=..,p=..$salt$digest`` ⇒ ``(n, r, p)``，取不到给 ``None``。

    刻意自己解析而不是调用 ``hashing`` 的私有 ``_parse_scrypt``：跨模块依赖一个
    下划线函数，等于把对方的内部实现变成自己的接口。
    """
    if not isinstance(encoded, str):
        return None
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != "scrypt":
        return None
    params: dict[str, int] = {}
    for chunk in parts[1].split(","):
        key, _, value = chunk.partition("=")
        if key not in ("n", "r", "p") or not value.isdigit():
            return None
        params[key] = int(value)
    if set(params) != {"n", "r", "p"}:
        return None
    return params["n"], params["r"], params["p"]


def argon2_params(encoded: object) -> tuple[int, int, int] | None:
    """``$argon2id$v=19$m=..,t=..,p=..$salt$digest`` ⇒ ``(m, t, p)``。"""
    if not isinstance(encoded, str) or not encoded.startswith("$argon2id$"):
        return None
    parts = encoded.split("$")
    if len(parts) < 5:
        return None
    params: dict[str, int] = {}
    for chunk in parts[3].split(","):
        key, _, value = chunk.partition("=")
        if key not in ("m", "t", "p") or not value.isdigit():
            return None
        params[key] = int(value)
    if set(params) != {"m", "t", "p"}:
        return None
    return params["m"], params["t"], params["p"]


def hash_meets_floor(encoded: object) -> bool:
    """散列参数是否达到安全下限。达不到就是"这份凭据不可用"。"""
    if not isinstance(encoded, str) or not encoded:
        return False
    if encoded.startswith("$argon2id$"):
        parsed = argon2_params(encoded)
        if parsed is None:
            return False
        memory, time_cost, parallelism = parsed
        return (
            memory >= MIN_ARGON2_MEMORY_KIB
            and time_cost >= MIN_ARGON2_TIME_COST
            and parallelism >= MIN_ARGON2_PARALLELISM
        )
    parsed_scrypt = scrypt_params(encoded)
    if parsed_scrypt is None:
        return False
    n, r, p = parsed_scrypt
    return n >= MIN_SCRYPT_N and r >= MIN_SCRYPT_R and p >= MIN_SCRYPT_P


class AdminCredentialStore:
    """读取并校验那个全平台共享的管理口令。

    文件每次校验都重新读取，因此**轮换立即生效**，不需要重启进程；同时
    :meth:`credential_version` 让所有已发出的提权行立刻作废。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        enforce_permissions: bool | None = None,
        fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
        process_uid: int | None = None,
    ) -> None:
        self.path = Path(path)
        self._probe = fs_probe
        self._enforce_permissions = os.name == "posix" if enforce_permissions is None else enforce_permissions
        self._uid = _process_uid() if process_uid is None else process_uid

    # --- inspection ---------------------------------------------------------

    def is_configured(self) -> bool:
        try:
            return self.status()["state"] == STATE_ACTIVE
        except AdminModeError:
            return False

    def status(self) -> dict[str, object]:
        """不含任何秘密的状态描述，给运维与 ``/status`` 用。"""
        state, encoded = self._load()
        report: dict[str, object] = {"state": state, "path": str(self.path)}
        if state == STATE_ACTIVE and encoded is not None:
            report["algorithm"] = "argon2id" if encoded.startswith("$argon2id$") else "scrypt"
            report["meets_floor"] = hash_meets_floor(encoded)
            report["needs_rehash"] = hashing.hash_needs_rehash(encoded)
        else:
            report["algorithm"] = None
            report["meets_floor"] = False
            report["needs_rehash"] = False
        return report

    def credential_version(self) -> str:
        """当前凭据的版本 token（``"none"`` 表示不可用）。

        用于让**已发出的提权行**在轮换或停用后立即失效。取的是散列的摘要，不是
        口令本身；这个值只在库里与该模块内部比较，从不出现在任何响应里。
        """
        state, encoded = self._load()
        if state != STATE_ACTIVE or encoded is None:
            return "none"
        return hashlib.sha256(b"admin-mode-credential\x00" + encoded.encode("utf-8")).hexdigest()[:32]

    # --- verification -------------------------------------------------------

    def verify(self, password: str) -> bool:
        """校验口令。不可用/被停用/损坏一律抛错，绝不静默返回"通过"。"""
        state, encoded = self._load()
        if state == STATE_MISSING:
            raise NotConfigured(detail="no shared admin credential file")
        if state == STATE_DISABLED:
            raise NotConfigured(detail="admin mode has been explicitly disabled")
        if state == STATE_BROKEN or encoded is None:
            raise Misconfigured(detail="the shared admin credential file is not usable")

        # 空口令不进入散列比较：空口令不是口令，而某些实现会把空串当成退化为
        # "无校验"。这里显式拒绝，并且与"口令不对"走同一条返回路径。
        if not isinstance(password, str) or password == "":
            return False
        return hashing.verify_password(password, encoded)

    # --- internals ----------------------------------------------------------

    def _load(self) -> tuple[str, str | None]:
        facts = self._probe(self.path)
        if not facts.exists:
            return STATE_MISSING, None

        self._check_permissions(facts)
        if facts.is_symlink:
            raise Misconfigured(detail="the shared admin credential must not be a symlink")
        if not facts.is_regular:
            raise Misconfigured(detail="the shared admin credential must be a regular file")
        if facts.size > MAX_CREDENTIAL_FILE_BYTES:
            raise Misconfigured(detail="the shared admin credential file is implausibly large")

        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise Misconfigured(detail=f"cannot read the credential file: {exc}") from None

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            # 空文件是写坏了，不是"刻意停用"——两者必须能分辨。
            return STATE_BROKEN, None
        if DISABLED_SENTINEL in lines:
            return STATE_DISABLED, None
        if len(lines) != 1:
            return STATE_BROKEN, None
        if not hashing.is_well_formed(lines[0]):
            return STATE_BROKEN, None
        if not hash_meets_floor(lines[0]):
            # 参数低于安全下限：能验也不接受。
            return STATE_BROKEN, None
        return STATE_ACTIVE, lines[0]

    def _check_permissions(self, facts: FileFacts) -> None:
        if not self._enforce_permissions:
            return
        if facts.mode & 0o077:
            raise Misconfigured(
                detail=(
                    "the shared admin credential file is readable or writable by group/other "
                    f"(mode {facts.mode:04o}); expected 0600"
                )
            )
        if facts.uid is not None and self._uid is not None and facts.uid != self._uid:
            raise Misconfigured(detail="the credential file is not owned by the service account")


# --------------------------------------------------------------------------
# 提权状态的存储
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Elevation:
    session_hash: str
    principal: str
    uid: int
    linux_user: str
    entered_at: float
    expires_at: float
    credential_version: str

    def is_live(self, now: float, credential_version: str) -> bool:
        return now < self.expires_at and hmac.compare_digest(
            self.credential_version, credential_version
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_elevations (
    session_hash       TEXT PRIMARY KEY,
    principal          TEXT NOT NULL,
    uid                INTEGER NOT NULL,
    linux_user         TEXT NOT NULL,
    entered_at         REAL NOT NULL,
    expires_at         REAL NOT NULL,
    credential_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_attempts (
    scope             TEXT NOT NULL,
    key               TEXT NOT NULL,
    window_started_at REAL NOT NULL,
    failures          INTEGER NOT NULL,
    locked_until      REAL NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL NOT NULL,
    event      TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    actor      TEXT,
    principal  TEXT,
    uid        INTEGER,
    linux_user TEXT,
    admin_mode INTEGER NOT NULL,
    detail     TEXT
);
"""


def admin_session_hash(session_id: str) -> str:
    """会话 cookie 值 ⇒ 入库用的摘要。库里永远没有可用的 cookie。"""
    return hashlib.sha256(b"admin-mode-session\x00" + str(session_id).encode("utf-8")).hexdigest()


class AdminModeStore:
    """提权行 + 失败计数 + 审计。**独立于** ``backend/auth/store.py``。

    刻意用自己的库文件：管理员模式是一个可整体删除的叠加层，把它写进会话表会让
    "关掉这个功能"变成一次会话表的迁移。
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        # ``sqlite3.connect(":memory:")`` hands back a *fresh*, empty database
        # every time. The whole class opens a connection per call, so an
        # in-memory store would silently lose every elevation it wrote — the
        # worst possible failure for this particular table. One connection is
        # therefore kept alive for that case.
        self._shared: sqlite3.Connection | None = None

    def _now(self) -> float:
        return float(self._clock())

    def _connect(self) -> sqlite3.Connection:
        if str(self.path) == ":memory:":
            if self._shared is None:
                self._shared = sqlite3.connect(":memory:", timeout=10.0)
                self._shared.row_factory = sqlite3.Row
            return self._shared
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            self._release(conn)
        self._harden()

    def _release(self, conn: sqlite3.Connection) -> None:
        """Close a per-call connection; leave the shared in-memory one open.

        Every method below opens a connection and must give it back. The
        in-memory case is the exception — closing it would discard the database
        the whole store is built on.
        """
        if conn is not self._shared:
            conn.close()

    def close(self) -> None:
        """Release the shared in-memory handle, if one was opened."""
        if self._shared is not None:
            try:
                self._shared.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass
            self._shared = None

    def _harden(self) -> None:
        """库文件与本模块写的任何东西一样，只有服务账号能读。"""
        if str(self.path) == ":memory:" or os.name != "posix":
            return
        for candidate, mode in ((self.path, 0o600), (self.path.parent, 0o700)):
            try:
                os.chmod(candidate, mode)
            except (OSError, NotImplementedError):  # pragma: no cover - odd filesystems
                continue

    # --- elevations ---------------------------------------------------------

    def insert_elevation(self, elevation: Elevation) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO admin_elevations
                    (session_hash, principal, uid, linux_user, entered_at, expires_at, credential_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_hash) DO UPDATE SET
                    principal          = excluded.principal,
                    uid                = excluded.uid,
                    linux_user         = excluded.linux_user,
                    entered_at         = excluded.entered_at,
                    expires_at         = excluded.expires_at,
                    credential_version = excluded.credential_version
                """,
                (
                    elevation.session_hash, elevation.principal, int(elevation.uid),
                    elevation.linux_user, float(elevation.entered_at),
                    float(elevation.expires_at), elevation.credential_version,
                ),
            )
            conn.commit()
        finally:
            self._release(conn)

    def get_elevation(self, session_hash: str) -> Elevation | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM admin_elevations WHERE session_hash = ?", (session_hash,)
            ).fetchone()
        finally:
            self._release(conn)
        if row is None:
            return None
        return Elevation(
            session_hash=row["session_hash"],
            principal=row["principal"],
            uid=int(row["uid"]),
            linux_user=row["linux_user"],
            entered_at=row["entered_at"],
            expires_at=row["expires_at"],
            credential_version=row["credential_version"],
        )

    def delete_elevation(self, session_hash: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM admin_elevations WHERE session_hash = ?", (session_hash,))
            conn.commit()
        finally:
            self._release(conn)

    def delete_all_elevations(self) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM admin_elevations")
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            self._release(conn)

    def purge_expired(self, now: float) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute("DELETE FROM admin_elevations WHERE expires_at <= ?", (float(now),))
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            self._release(conn)

    def count_elevations(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM admin_elevations").fetchone()
        finally:
            self._release(conn)
        return int(row["n"])

    # --- failed-attempt counters（``RateLimiter`` 的三个方法） ---------------

    def read_attempt(self, scope: str, key: str) -> tuple[float, int, float] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT window_started_at, failures, locked_until FROM admin_attempts "
                "WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        finally:
            self._release(conn)
        if row is None:
            return None
        return (row["window_started_at"], int(row["failures"]), row["locked_until"])

    def write_attempt(
        self, scope: str, key: str, window_started_at: float, failures: int, locked_until: float
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO admin_attempts (scope, key, window_started_at, failures, locked_until)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET
                    window_started_at = excluded.window_started_at,
                    failures          = excluded.failures,
                    locked_until      = excluded.locked_until
                """,
                (scope, key, float(window_started_at), int(failures), float(locked_until)),
            )
            conn.commit()
        finally:
            self._release(conn)

    def clear_attempt(self, scope: str, key: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM admin_attempts WHERE scope = ? AND key = ?", (scope, key))
            conn.commit()
        finally:
            self._release(conn)

    # --- audit --------------------------------------------------------------

    def record_audit(
        self,
        *,
        event: str,
        outcome: str,
        actor: str | None = None,
        principal: str | None = None,
        uid: int | None = None,
        linux_user: str | None = None,
        admin_mode: bool = False,
        detail: str | None = None,
    ) -> None:
        """写一条审计。``detail`` 不得含任何凭据材料（调用方负责）。

        ``actor`` 是原来的 Linux uid / 登录名，``admin_mode`` 只是那一刻的标记：
        提权不是另一个主体，因此这里**不**生成替代身份。
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO admin_audit
                    (at, event, outcome, actor, principal, uid, linux_user, admin_mode, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._now(), event, outcome, actor, principal,
                    int(uid) if uid is not None else None, linux_user,
                    1 if admin_mode else 0, detail,
                ),
            )
            conn.commit()
        finally:
            self._release(conn)

    def list_audit(self, limit: int = 100) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT at, event, outcome, actor, principal, uid, linux_user, admin_mode, detail "
                "FROM admin_audit ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            self._release(conn)
        return [
            {
                "at": row["at"],
                "event": row["event"],
                "outcome": row["outcome"],
                "actor": row["actor"],
                "principal": row["principal"],
                "uid": row["uid"],
                "linux_user": row["linux_user"],
                "admin_mode": bool(row["admin_mode"]),
                "detail": row["detail"],
            }
            for row in rows
        ]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminModeConfig:
    enabled: bool = False
    credential_path: Path | None = None
    store_path: Path | None = None
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    max_failures: int = DEFAULT_MAX_FAILURES
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS


def from_env(environ: Mapping[str, str], *, state_dir: str | Path) -> AdminModeConfig:
    """从环境变量读配置。默认**关闭**（并且没有口令文件时也无人可提升）。"""
    raw_enabled = str(environ.get(ENV_ENABLED, "")).strip().lower()
    enabled = raw_enabled in {"1", "true", "yes", "on"}
    if raw_enabled and raw_enabled not in {"1", "true", "yes", "on", "0", "false", "no", "off", ""}:
        # 拼错的开关不当作"关闭"——那会让一次打错的部署看起来像"功能没开"。
        raise Misconfigured(detail=f"{ENV_ENABLED} is not a recognised boolean")

    base = Path(state_dir)
    credential = environ.get(ENV_HASH_FILE) or str(base / DEFAULT_CREDENTIAL_FILE_NAME)
    store = environ.get(ENV_STATE_DB) or str(base / DEFAULT_STORE_FILE_NAME)

    ttl = _int_env(environ, ENV_TTL_SECONDS, DEFAULT_TTL_SECONDS, MIN_TTL_SECONDS, MAX_TTL_SECONDS)
    return AdminModeConfig(
        enabled=enabled,
        credential_path=Path(credential),
        store_path=Path(store),
        ttl_seconds=ttl,
        max_failures=_int_env(environ, ENV_MAX_FAILURES, DEFAULT_MAX_FAILURES, 1, 1000),
        window_seconds=_int_env(environ, ENV_WINDOW_SECONDS, DEFAULT_WINDOW_SECONDS, 1, 86400),
        lockout_seconds=_int_env(environ, ENV_LOCKOUT_SECONDS, DEFAULT_LOCKOUT_SECONDS, 1, 86400),
    )


def _int_env(environ: Mapping[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    raw = environ.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip()
    if not text.isdigit():
        raise Misconfigured(detail=f"{key} must be a non-negative integer")
    value = int(text)
    if not minimum <= value <= maximum:
        raise Misconfigured(detail=f"{key} is outside the permitted range")
    return value


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminModeState:
    """``GET /auth/admin/status`` 的形状，也是 UI 唯一需要的状态。"""

    configured: bool
    elevated: bool
    ttl_seconds: int
    expires_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "elevated": self.elevated,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
        }


class AdminModeService:
    """进入 / 退出 / 查询管理员模式。

    这里**没有**会话解析：调用方必须先把 cookie 解成一个受信主体（
    ``backend.bff.identity.resolve_principal``，它只接受 ``linux_pam`` 会话），
    再把主体交给本服务。因此"没有登录、或不是 Linux 登录的人不能提权"是**由构造
    决定的**，而不是靠本模块再检查一遍。
    """

    def __init__(
        self,
        *,
        credentials: AdminCredentialStore,
        store: AdminModeStore,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        enabled: bool = True,
        clock: Callable[[], float] = time.time,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.credentials = credentials
        self.store = store
        self.enabled = bool(enabled)
        self.ttl_seconds = max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, int(ttl_seconds)))
        self._clock = clock
        self._limiter = limiter if limiter is not None else RateLimiter(
            store,
            max_failures=DEFAULT_MAX_FAILURES,
            window_seconds=DEFAULT_WINDOW_SECONDS,
            lockout_seconds=DEFAULT_LOCKOUT_SECONDS,
            clock=clock,
        )

    # --- 状态 ---------------------------------------------------------------

    def configured(self) -> bool:
        return bool(self.enabled) and self.credentials.is_configured()

    def status_for(self, session_id: object) -> AdminModeState:
        """给 UI 的状态。不抛错：未登录时也是一个合法的"未提权"。"""
        configured = self.configured()
        if not isinstance(session_id, str) or not session_id:
            return AdminModeState(configured=configured, elevated=False, ttl_seconds=self.ttl_seconds)
        elevation = self._live_elevation(session_id)
        return AdminModeState(
            configured=configured,
            elevated=elevation is not None,
            ttl_seconds=self.ttl_seconds,
            expires_at=elevation.expires_at if elevation is not None else None,
        )

    def is_elevated(self, session_id: object, *, principal: str | None = None, uid: int | None = None) -> bool:
        """这条会话此刻是否处于管理员模式。

        ``principal`` / ``uid`` 传进来时会一并核对：提权行绑定的是**当时**那个
        主体，会话与主体对不上就不算提权。两者都省略时只按会话判断（调用方已经
        从会话推出主体，这里不重复推）。
        """
        elevation = self._live_elevation(session_id)
        if elevation is None:
            return False
        if principal is not None and elevation.principal != principal:
            return False
        if uid is not None and int(elevation.uid) != int(uid):
            return False
        return True

    # --- 进入 / 退出 --------------------------------------------------------

    def enter(
        self,
        *,
        session_id: object,
        principal: str,
        uid: int,
        linux_user: str,
        password: object,
        client_key: str,
    ) -> AdminModeState:
        """用共享口令把**当前会话**提权。"""
        if not self.configured():
            self.store.record_audit(
                event="admin_mode_enter",
                outcome="not_configured",
                actor=linux_user,
                principal=principal,
                uid=uid,
                linux_user=linux_user,
                admin_mode=False,
            )
            raise NotConfigured()

        if not isinstance(session_id, str) or not session_id:
            raise InvalidRequest(detail="no session to elevate")

        key = f"{client_key}|{principal}"
        try:
            self._limiter.check(SCOPE_ADMIN_ENTER, key)
        except RateLimited:
            raise
        except AuthRateLimited as exc:
            # The limiter is the auth service's, so it raises the auth service's
            # error. Translate at the boundary: everything this module raises is
            # an ``AdminModeError``, and the HTTP layer maps exactly one family.
            self.store.record_audit(
                event="admin_mode_enter",
                outcome="throttled",
                actor=linux_user,
                principal=principal,
                uid=uid,
                linux_user=linux_user,
                admin_mode=False,
            )
            raise RateLimited(retry_after=getattr(exc, "retry_after", None)) from None

        try:
            accepted = self.credentials.verify(password if isinstance(password, str) else "")
        except AdminModeError:
            self.store.record_audit(
                event="admin_mode_enter",
                outcome="misconfigured",
                actor=linux_user,
                principal=principal,
                uid=uid,
                linux_user=linux_user,
                admin_mode=False,
            )
            raise

        if not accepted:
            self._limiter.record_failure(SCOPE_ADMIN_ENTER, key)
            self.store.record_audit(
                event="admin_mode_enter",
                outcome="failure",
                actor=linux_user,
                principal=principal,
                uid=uid,
                linux_user=linux_user,
                admin_mode=False,
            )
            raise InvalidCredentials()

        now = self._clock()
        elevation = Elevation(
            session_hash=admin_session_hash(session_id),
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            entered_at=now,
            expires_at=now + self.ttl_seconds,
            credential_version=self.credentials.credential_version(),
        )
        self.store.insert_elevation(elevation)
        self._limiter.record_success(SCOPE_ADMIN_ENTER, key)
        self.store.record_audit(
            event="admin_mode_enter",
            outcome="success",
            actor=linux_user,
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            admin_mode=True,
        )
        return AdminModeState(
            configured=True, elevated=True, ttl_seconds=self.ttl_seconds, expires_at=elevation.expires_at
        )

    def exit(
        self,
        *,
        session_id: object,
        principal: str | None = None,
        linux_user: str | None = None,
        uid: int | None = None,
    ) -> AdminModeState:
        """退出管理员模式。幂等：本来就没提权也返回成功。"""
        if isinstance(session_id, str) and session_id:
            self.store.delete_elevation(admin_session_hash(session_id))
        self.store.record_audit(
            event="admin_mode_exit",
            outcome="success",
            actor=linux_user,
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            admin_mode=False,
        )
        return AdminModeState(configured=self.configured(), elevated=False, ttl_seconds=self.ttl_seconds)

    # --- 失效 ---------------------------------------------------------------

    def forget_session(self, session_id: object) -> None:
        """登出钩子：会话一撤销，它的提权立刻消失。"""
        if isinstance(session_id, str) and session_id:
            self.store.delete_elevation(admin_session_hash(session_id))

    def rotate_credentials(self) -> int:
        """口令轮换后调用：所有已发出的提权立即作废。

        即使不调用这个方法，``credential_version`` 的比对也已经让旧提权失效；这里
        额外删掉，是为了让"正在提权中"的那些行不留在库里。
        """
        removed = self.store.delete_all_elevations()
        self.store.record_audit(
            event="admin_mode_credential_rotation", outcome="success", admin_mode=False,
            detail=f"elevations_revoked={removed}",
        )
        return removed

    def purge(self) -> int:
        return self.store.purge_expired(self._clock())

    # --- internals ----------------------------------------------------------

    def _live_elevation(self, session_id: object) -> Elevation | None:
        if not isinstance(session_id, str) or not session_id:
            return None
        elevation = self.store.get_elevation(admin_session_hash(session_id))
        if elevation is None:
            return None
        if not elevation.is_live(self._clock(), self.credentials.credential_version()):
            # 过期或凭据已轮换：顺手清掉，别让一行死记录留在库里。
            self.store.delete_elevation(elevation.session_hash)
            return None
        return elevation


def build_service(config: AdminModeConfig, *, clock: Callable[[], float] = time.time) -> AdminModeService:
    """按配置装配。**不做任何隐式启用**：没有文件就是没人能提升。"""
    if config.credential_path is None or config.store_path is None:  # pragma: no cover - 配置由 from_env 保证
        raise Misconfigured(detail="admin mode paths are not configured")
    store = AdminModeStore(config.store_path, clock=clock)
    store.initialize()
    credentials = AdminCredentialStore(config.credential_path)
    limiter = RateLimiter(
        store,
        max_failures=config.max_failures,
        window_seconds=config.window_seconds,
        lockout_seconds=config.lockout_seconds,
        clock=clock,
    )
    return AdminModeService(
        credentials=credentials,
        store=store,
        ttl_seconds=config.ttl_seconds,
        enabled=config.enabled,
        clock=clock,
        limiter=limiter,
    )


# --------------------------------------------------------------------------
# 运维侧 CLI：口令只从交互式提示读入
# --------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 16

NEW_PROMPT = "New shared admin password (input is not echoed): "
CONFIRM_PROMPT = "Repeat the new shared admin password: "
CURRENT_PROMPT = "Current shared admin password (input is not echoed): "

ReadPassword = Callable[[str], str]


def _interactive_reader(prompt: str) -> str:
    return getpass.getpass(prompt)


def validate_new_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidRequest(detail=f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if not password.strip():
        raise InvalidRequest(detail="password must not be blank")


def _write_private(path: Path, content: str) -> None:
    """原子地以 ``0600`` 写入：崩溃不会留下半个文件。"""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows / odd FS
        pass

    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".admin-mode-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - best effort
            pass
        raise


def _read_confirmed(read_password: ReadPassword) -> str:
    first = read_password(NEW_PROMPT)
    second = read_password(CONFIRM_PROMPT)
    if first != second:
        raise InvalidRequest(detail="the two entries did not match")
    return first


def main(
    argv: Sequence[str] | None = None,
    *,
    read_password: ReadPassword = _interactive_reader,
    clock: Callable[[], float] = time.time,
) -> int:
    """``python -m backend.bff.admin_mode init|status|rotate|disable``。

    **没有 ``--password`` 参数、没有环境变量、没有 stdin 管道**：任何可能落进
    shell history、``ps`` 输出或 CI 日志的通道都被排除。这不是洁癖——那个口令
    一旦泄漏，任何人都能把自己的会话提成管理员。
    """
    parser = argparse.ArgumentParser(prog="python -m backend.bff.admin_mode")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("init", "create the shared admin credential"),
        ("status", "report the credential state without revealing anything"),
        ("rotate", "replace the shared admin credential"),
        ("disable", "refuse every admin-mode attempt until re-initialised"),
    ):
        child = subparsers.add_parser(name, help=help_text)
        child.add_argument("--state-dir", required=True, help="private state directory")
        if name in ("init", "rotate"):
            child.add_argument(
                "--force", action="store_true", help="overwrite an existing credential"
            )
            child.add_argument(
                "--recover",
                action="store_true",
                help="break-glass: rotate without proving the current password",
            )

    args = parser.parse_args(list(argv) if argv is not None else None)
    state_dir = Path(args.state_dir)
    config = from_env({}, state_dir=state_dir)
    store = AdminCredentialStore(config.credential_path)

    try:
        if args.command == "status":
            report = store.status()
            print(f"state:          {report['state']}")
            print(f"algorithm:      {report['algorithm']}")
            print(f"meets_floor:    {report['meets_floor']}")
            print(f"needs_rehash:   {report['needs_rehash']}")
            print(f"path:           {report['path']}")
            return 0

        if args.command == "disable":
            if store.status()["state"] == STATE_MISSING:
                print("no credential to disable")
                return 1
            _write_private(config.credential_path, DISABLED_SENTINEL + "\n")
            _revoke_elevations(config, clock)
            print("admin mode disabled: every attempt now reports not_configured")
            return 0

        current_state = store.status()["state"]
        if current_state != STATE_MISSING and not args.force and not getattr(args, "recover", False):
            if args.command == "init":
                print("a credential already exists; use --force to overwrite or `rotate`")
                return 1
            supplied = read_password(CURRENT_PROMPT)
            try:
                if not store.verify(supplied):
                    print("the current password did not match")
                    return 1
            except AdminModeError as exc:
                print(f"the current credential is unusable: {exc.code}")
                return 1

        password = _read_confirmed(read_password)
        validate_new_password(password)
        encoded = hashing.hash_password(password)
        if not hash_meets_floor(encoded):  # pragma: no cover - 由 hashing 的默认值保证
            raise Misconfigured(detail="refusing to write a hash below the security floor")
        _write_private(config.credential_path, encoded + "\n")
        revoked = _revoke_elevations(config, clock)
        print(f"wrote a {store.status()['algorithm']} credential; revoked {revoked} elevation(s)")
        return 0
    except AdminModeError as exc:
        print(f"error: {exc.code}")
        return 1


def _revoke_elevations(config: AdminModeConfig, clock: Callable[[], float]) -> int:
    store = AdminModeStore(config.store_path, clock=clock)
    store.initialize()
    removed = store.delete_all_elevations()
    store.record_audit(
        event="admin_mode_credential_rotation",
        outcome="success",
        admin_mode=False,
        detail=f"elevations_revoked={removed}",
    )
    return removed


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "DISABLED_SENTINEL",
    "MAX_TTL_SECONDS",
    "MIN_ARGON2_MEMORY_KIB",
    "MIN_ARGON2_PARALLELISM",
    "MIN_ARGON2_TIME_COST",
    "MIN_SCRYPT_N",
    "MIN_SCRYPT_P",
    "MIN_SCRYPT_R",
    "MIN_TTL_SECONDS",
    "SCOPE_ADMIN_ENTER",
    "STATE_ACTIVE",
    "STATE_BROKEN",
    "STATE_DISABLED",
    "STATE_MISSING",
    "AdminCredentialStore",
    "AdminModeConfig",
    "AdminModeError",
    "AdminModeService",
    "AdminModeState",
    "AdminModeStore",
    "Elevation",
    "FileFacts",
    "InvalidCredentials",
    "InvalidRequest",
    "Misconfigured",
    "NotConfigured",
    "RateLimited",
    "admin_session_hash",
    "argon2_params",
    "build_service",
    "default_fs_probe",
    "from_env",
    "hash_meets_floor",
    "main",
    "scrypt_params",
    "validate_new_password",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
