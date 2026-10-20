"""SQLite storage for platform accounts, sessions, throttle state and audit.

Storage rules (enforced, not documented-and-hoped):

* the state directory must be absolute, a real directory, **not** a symlink,
  and not group/world writable;
* the database file is created ``0600`` with ``O_NOFOLLOW`` and is refused if it
  is a symlink or is group/world accessible;
* nothing here stores a plaintext password, a raw session token, or a Linux
  account name.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time

from .errors import StorageSecurityError

DB_FILENAME = "platform_auth.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_user_id TEXT NOT NULL UNIQUE,
    login_name TEXT NOT NULL UNIQUE,
    display_name TEXT,
    password_hash TEXT NOT NULL,
    scopes TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    password_changed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_sessions (
    token_digest TEXT PRIMARY KEY,
    platform_user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    idle_expires_at REAL NOT NULL,
    csrf_digest TEXT NOT NULL,
    user_epoch REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS platform_login_throttle (
    login_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    window_start REAL NOT NULL,
    locked_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    event TEXT NOT NULL,
    subject TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS platform_sessions_by_user
    ON platform_sessions (platform_user_id);
"""


def _reject(reason: str) -> None:
    raise StorageSecurityError(reason)


def prepare_state_dir(path: str) -> str:
    """Validate (and create, if missing) the owner-private state directory."""
    if not isinstance(path, str) or not path:
        _reject("state directory is required")
    if not os.path.isabs(path):
        _reject("state directory must be an absolute path")
    if os.path.islink(path):
        _reject("state directory must not be a symlink")
    if not os.path.exists(path):
        os.makedirs(path, mode=0o700, exist_ok=False)
        os.chmod(path, 0o700)
    if os.path.realpath(path) != os.path.abspath(path):
        _reject("state directory resolves outside itself")
    info = os.stat(path)
    if not stat.S_ISDIR(info.st_mode):
        _reject("state directory is not a directory")
    if info.st_mode & 0o002:
        _reject("state directory is world-writable")
    if info.st_mode & 0o020:
        _reject("state directory is group-writable")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        _reject("state directory is not owned by this user")
    return path


def _open_database(path: str) -> None:
    """Create the database with 0600 and refuse an unsafe pre-existing file."""
    if os.path.islink(path):
        _reject("database file must not be a symlink")
    if not os.path.exists(path):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        os.close(fd)
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        _reject("database path is not a regular file")
    if info.st_mode & 0o077:
        _reject("database file is accessible to group or others")
    os.chmod(path, 0o600)


class Store:
    """Thin SQLite wrapper. One connection, explicit transactions, no ORM."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = prepare_state_dir(state_dir)
        self.path = os.path.join(self.state_dir, DB_FILENAME)
        _open_database(self.path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- users ----------------------------------------------------------

    def insert_user(self, row: dict) -> None:
        self._conn.execute(
            "INSERT INTO platform_users "
            "(platform_user_id, login_name, display_name, password_hash, scopes,"
            " disabled, created_at, password_changed_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                row["platform_user_id"], row["login_name"], row.get("display_name"),
                row["password_hash"], row["scopes"], int(row.get("disabled", 0)),
                row["created_at"], row["password_changed_at"],
            ),
        )

    def user_by_login(self, login_name: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM platform_users WHERE login_name = ?", (login_name,)
        ).fetchone()

    def user_by_id(self, platform_user_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM platform_users WHERE platform_user_id = ?",
            (platform_user_id,),
        ).fetchone()

    def count_users(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM platform_users").fetchone()["n"])

    def update_secret(self, platform_user_id: str, password_hash: str, epoch: float) -> None:
        self._conn.execute(
            "UPDATE platform_users SET password_hash = ?, password_changed_at = ? "
            "WHERE platform_user_id = ?",
            (password_hash, epoch, platform_user_id),
        )

    def set_disabled(self, platform_user_id: str, disabled: bool) -> None:
        self._conn.execute(
            "UPDATE platform_users SET disabled = ? WHERE platform_user_id = ?",
            (1 if disabled else 0, platform_user_id),
        )

    def list_users(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT platform_user_id, login_name, display_name, scopes, disabled,"
                " created_at, password_changed_at FROM platform_users"
            ).fetchall()
        )

    # --- sessions -------------------------------------------------------

    def insert_session(self, row: dict) -> None:
        self._conn.execute(
            "INSERT INTO platform_sessions "
            "(token_digest, platform_user_id, created_at, expires_at, idle_expires_at,"
            " csrf_digest, user_epoch, revoked) VALUES (?,?,?,?,?,?,?,0)",
            (
                row["token_digest"], row["platform_user_id"], row["created_at"],
                row["expires_at"], row["idle_expires_at"], row["csrf_digest"],
                row["user_epoch"],
            ),
        )

    def session_by_digest(self, token_digest: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM platform_sessions WHERE token_digest = ?", (token_digest,)
        ).fetchone()

    def touch_session(self, token_digest: str, idle_expires_at: float) -> None:
        self._conn.execute(
            "UPDATE platform_sessions SET idle_expires_at = ? WHERE token_digest = ?",
            (idle_expires_at, token_digest),
        )

    def revoke_session(self, token_digest: str) -> None:
        self._conn.execute(
            "UPDATE platform_sessions SET revoked = 1 WHERE token_digest = ?",
            (token_digest,),
        )

    def revoke_all_sessions(self, platform_user_id: str) -> int:
        cursor = self._conn.execute(
            "UPDATE platform_sessions SET revoked = 1 WHERE platform_user_id = ? AND revoked = 0",
            (platform_user_id,),
        )
        return int(cursor.rowcount or 0)

    def active_session_count(self, platform_user_id: str) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) AS n FROM platform_sessions "
                "WHERE platform_user_id = ? AND revoked = 0",
                (platform_user_id,),
            ).fetchone()["n"]
        )

    # --- throttle -------------------------------------------------------

    def throttle(self, login_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM platform_login_throttle WHERE login_key = ?", (login_key,)
        ).fetchone()

    def put_throttle(self, login_key: str, failures: int, window_start: float,
                     locked_until: float) -> None:
        self._conn.execute(
            "INSERT INTO platform_login_throttle (login_key, failures, window_start, locked_until)"
            " VALUES (?,?,?,?) ON CONFLICT(login_key) DO UPDATE SET"
            " failures = excluded.failures, window_start = excluded.window_start,"
            " locked_until = excluded.locked_until",
            (login_key, failures, window_start, locked_until),
        )

    def clear_throttle(self, login_key: str) -> None:
        self._conn.execute(
            "DELETE FROM platform_login_throttle WHERE login_key = ?", (login_key,)
        )

    # --- audit ----------------------------------------------------------

    def audit(self, event: str, subject: str | None = None, detail: str | None = None) -> None:
        """Append an audit row. Callers must never pass a secret here."""
        self._conn.execute(
            "INSERT INTO platform_audit (at, event, subject, detail) VALUES (?,?,?,?)",
            (time.time(), event, subject, detail),
        )

    def audit_rows(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT at, event, subject, detail FROM platform_audit ORDER BY id"
            ).fetchall()
        )


__all__ = ["DB_FILENAME", "SCHEMA", "Store", "prepare_state_dir"]
