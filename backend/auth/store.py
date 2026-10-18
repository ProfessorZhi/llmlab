"""Persistent state for the authentication backend.

One SQLite database holds every piece of durable authentication state: users and
their project grants, sessions, in-flight login states, spent provider codes,
rate-limit counters and the audit trail. SQLite is used because it is in the
standard library, needs no daemon, and gives us *cross-process* atomicity for the
one place where it really matters (one-time consumption of ``state`` and
``code``).

Two rules are baked into the schema rather than into the callers:

1. **Opaque identifiers are stored as digests.** The cookie holds a raw session
   id; the database only ever sees ``sha256(id)``. A leaked database file
   therefore does not hand over live sessions. The OAuth browser-binding nonce
   is treated the same way: only its digest reaches ``login_states``.
2. **Roles are never assigned by the upsert path.** Re-login may refresh a
   display name, but it can neither change a role nor re-enable a disabled
   account, so no authentication event can be an escalation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SOURCE_LOCAL = "local"
SOURCE_FEISHU = "feishu"
#: Kept byte-identical to ``backend.auth_linux.handoff.AUTH_SOURCE_LINUX``. The
#: duplicate is deliberate: ``backend.auth`` must not import the Linux package
#: (that package documents the dependency direction as protocol-only), so the
#: string is spelled twice and ``tests/bff/test_handoff.py`` fails if the two
#: ever drift apart.
SOURCE_LINUX = "linux_pam"

ROLE_ADMIN = "platform_admin"
ROLE_MEMBER = "member"

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"

# 32 bytes = 256 bits, per the requirement that session identifiers carry at
# least 256 bits of entropy.
SECRET_ID_BYTES = 32

# The identity index carries ``tenant_key`` as well as ``app_id``: a Feishu
# ``open_id`` is issued per application *and* per tenant, so an index without it
# would refuse the second tenant's identity outright — or, worse, allow it only
# as long as nobody had noticed the collision.
_USERS_IDENTITY_INDEX = "users_external_identity"
_USERS_IDENTITY_COLUMNS = ("auth_source", "app_id", "tenant_key", "external_id")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             TEXT PRIMARY KEY,
    auth_source    TEXT NOT NULL,
    external_id    TEXT,
    app_id         TEXT,
    union_id       TEXT,
    tenant_key     TEXT,
    display_name   TEXT NOT NULL,
    role           TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    last_login_at  REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS users_external_identity
    ON users(auth_source, app_id, tenant_key, external_id);

CREATE TABLE IF NOT EXISTS project_grants (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    granted_at REAL NOT NULL,
    PRIMARY KEY (user_id, project_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash        TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    csrf_token          TEXT NOT NULL,
    created_at          REAL NOT NULL,
    last_seen_at        REAL NOT NULL,
    absolute_expires_at REAL NOT NULL,
    revoked_at          REAL
);

CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS login_states (
    state_hash    TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    code_verifier TEXT,
    next_path     TEXT,
    binding_hash  TEXT
);

CREATE TABLE IF NOT EXISTS provider_codes (
    code_hash  TEXT PRIMARY KEY,
    claimed_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_attempts (
    scope             TEXT NOT NULL,
    key               TEXT NOT NULL,
    window_started_at REAL NOT NULL,
    failures          INTEGER NOT NULL,
    locked_until      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    event   TEXT NOT NULL,
    outcome TEXT NOT NULL,
    actor   TEXT,
    detail  TEXT
);
"""


def new_secret_id(nbytes: int = SECRET_ID_BYTES) -> str:
    """A fresh URL-safe random identifier (>= 256 bits at the default size)."""
    return secrets.token_urlsafe(nbytes)


def session_hash(raw: str) -> str:
    """Digest used for opaque identifiers at rest (sessions, state, codes, CSRF)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add ``column`` to ``table`` if an older schema predates it.

    ``table``, ``column`` and ``decl`` are module literals, never user input.
    """
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _ensure_index(
    conn: sqlite3.Connection, name: str, columns: tuple[str, ...], create_sql: str
) -> None:
    """Rebuild ``name`` when an older schema defined it over different columns.

    ``CREATE UNIQUE INDEX IF NOT EXISTS`` silently keeps the old definition, so
    a database written before ``tenant_key`` joined the identity key would go on
    enforcing the narrower rule. Rebuilding is safe here because the new column
    set is a superset of the old one, so any data that satisfied the old index
    satisfies the new one as well.
    """
    rows = conn.execute(f"PRAGMA index_info({name})").fetchall()
    if rows and tuple(str(row["name"]) for row in rows) != columns:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
        rows = []
    if not rows:
        conn.execute(create_sql)


@dataclass(frozen=True)
class User:
    id: str
    auth_source: str
    external_id: str | None
    app_id: str | None
    union_id: str | None
    tenant_key: str | None
    display_name: str
    role: str
    status: str
    created_at: float
    last_login_at: float | None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE


@dataclass(frozen=True)
class Grant:
    user_id: str
    project_id: str
    role: str


@dataclass(frozen=True)
class SessionRecord:
    session_hash: str
    user_id: str
    # Stored verbatim rather than digested: unlike the session id it is not a
    # bearer credential (it is useless without the session cookie, whose id
    # *is* stored only as a digest) and the server must be able to hand it
    # back to the same-origin /auth/csrf endpoint.
    csrf_token: str
    created_at: float
    last_seen_at: float
    absolute_expires_at: float
    revoked_at: float | None


@dataclass(frozen=True)
class LoginState:
    state_hash: str
    created_at: float
    expires_at: float
    code_verifier: str | None
    next_path: str | None
    # Digest of the browser nonce that started this flow, or ``None`` for states
    # that are not browser-bound (the anonymous CSRF tokens minted by
    # ``service.issue_csrf``). Never the nonce itself.
    binding_hash: str | None = None


def _local_user_id(username: str) -> str:
    return "local:" + hashlib.sha256(username.encode("utf-8")).hexdigest()[:32]


def _feishu_user_id(app_id: str, tenant_key: str, open_id: str) -> str:
    """The local identity of a Feishu person.

    All three parts are load-bearing. ``open_id`` is scoped to the application
    *and* the tenant it was issued in, so keying on ``(app_id, open_id)`` alone
    would let one allowlisted tenant's identity collapse onto another's — and
    with it that person's project grants. ``tenant_key`` therefore forms part of
    the key, matching the joint ``(app_id, tenant_key, open_id)`` ownership the
    review requires.
    """
    raw = f"{app_id}\x00{tenant_key}\x00{open_id}".encode("utf-8")
    return "feishu:" + hashlib.sha256(raw).hexdigest()[:32]


class Database:
    """Thin, dependency-free SQLite wrapper.

    A connection is opened per operation. That is cheap at authentication
    traffic levels and avoids sharing a connection between WSGI worker threads,
    which SQLite does not allow by default.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _now(self) -> float:
        return self._clock()

    # --- connection / schema -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        _harden(parent, 0o700, is_dir=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            # ``CREATE TABLE IF NOT EXISTS`` leaves an existing database alone,
            # so a deployment that ran an earlier build keeps the old column
            # set. Bring it forward explicitly rather than failing on the first
            # insert of a browser-bound state.
            _ensure_column(conn, "login_states", "binding_hash", "TEXT")
            _ensure_index(
                conn,
                _USERS_IDENTITY_INDEX,
                _USERS_IDENTITY_COLUMNS,
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_USERS_IDENTITY_INDEX} "
                f"ON users({', '.join(_USERS_IDENTITY_COLUMNS)})",
            )
        finally:
            conn.close()
        _harden(self.path, 0o600, is_dir=False)

    # --- users ---------------------------------------------------------------

    def ensure_local_user(self, username: str) -> User:
        user_id = _local_user_id(username)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO users (id, auth_source, external_id, app_id, union_id,
                                   tenant_key, display_name, role, status, created_at)
                VALUES (?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (user_id, SOURCE_LOCAL, username, ROLE_ADMIN, STATUS_ACTIVE, self._now()),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            conn.execute("COMMIT")
        finally:
            conn.close()
        return _row_to_user(row)

    def upsert_feishu_user(
        self,
        *,
        app_id: str,
        open_id: str,
        union_id: str | None,
        tenant_key: str,
        display_name: str,
    ) -> User:
        """Create or refresh the local shadow of a Feishu identity.

        Note what is *not* in the UPDATE clause: ``role`` and ``status``. A
        returning user keeps whatever the platform decided, so signing in can
        never re-enable a disabled account or promote anyone.
        """
        user_id = _feishu_user_id(app_id, tenant_key, open_id)
        now = self._now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO users (id, auth_source, external_id, app_id, union_id,
                                   tenant_key, display_name, role, status, created_at,
                                   last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name  = excluded.display_name,
                    union_id      = excluded.union_id,
                    tenant_key    = excluded.tenant_key,
                    last_login_at = excluded.last_login_at
                """,
                (
                    user_id, SOURCE_FEISHU, open_id, app_id, union_id, tenant_key,
                    display_name, ROLE_MEMBER, STATUS_ACTIVE, now, now,
                ),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            conn.execute("COMMIT")
        finally:
            conn.close()
        return _row_to_user(row)

    def ensure_linux_user(
        self,
        *,
        user_id: str,
        external_id: str,
        display_name: str,
        role: str,
    ) -> User:
        """Create or refresh the local shadow of a Linux (PAM) identity.

        Exactly the discipline :meth:`upsert_feishu_user` follows, and for the
        same reason: ``role`` and ``status`` appear in the INSERT and **never**
        in the UPDATE clause. A returning sign-in therefore cannot promote
        itself or re-enable a disabled account — the role the PAM policy
        computed is written once, when the platform user is first created, and
        afterwards only an out-of-band :meth:`set_user_role` may change it.

        ``user_id`` is the ``linux:<host>:<uid>`` identifier built by
        ``backend.auth_linux.handoff.linux_user_id``; it is keyed on host and
        UID rather than the login name, so renaming an account keeps the same
        platform user. ``last_login_at`` is the only field a returning sign-in
        moves.
        """
        now = self._now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO users (id, auth_source, external_id, app_id, union_id,
                                   tenant_key, display_name, role, status, created_at,
                                   last_login_at)
                VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name  = excluded.display_name,
                    last_login_at = excluded.last_login_at
                """,
                (
                    user_id, SOURCE_LINUX, external_id,
                    display_name, role, STATUS_ACTIVE, now, now,
                ),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            conn.execute("COMMIT")
        finally:
            conn.close()
        return _row_to_user(row)

    def get_user(self, user_id: str) -> User | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        return _row_to_user(row) if row else None

    def find_user_by_open_id(self, app_id: str, open_id: str) -> User | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE auth_source = ? AND app_id = ? AND external_id = ?",
                (SOURCE_FEISHU, app_id, open_id),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_user(row) if row else None

    def set_user_status(self, user_id: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        finally:
            conn.close()

    def set_user_role(self, user_id: str, role: str) -> None:
        """Administrative helper. Not reachable from any HTTP route."""
        conn = self._connect()
        try:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        finally:
            conn.close()

    def list_project_grants(self, user_id: str) -> list[Grant]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT user_id, project_id, role FROM project_grants "
                "WHERE user_id = ? ORDER BY project_id",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
        return [Grant(r["user_id"], r["project_id"], r["role"]) for r in rows]

    def grant_project(self, user_id: str, project_id: str, role: str) -> None:
        """Administrative helper. Not reachable from any HTTP route."""
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO project_grants (user_id, project_id, role, granted_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, project_id) DO UPDATE SET role = excluded.role
                """,
                (user_id, project_id, role, self._now()),
            )
        finally:
            conn.close()

    def revoke_project(self, user_id: str, project_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM project_grants WHERE user_id = ? AND project_id = ?",
                (user_id, project_id),
            )
        finally:
            conn.close()

    # --- sessions ------------------------------------------------------------

    def insert_session(
        self,
        *,
        session_hash: str,
        user_id: str,
        csrf_token: str,
        absolute_expires_at: float,
    ) -> None:
        now = self._now()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO sessions (session_hash, user_id, csrf_token, created_at,
                                      last_seen_at, absolute_expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (session_hash, user_id, csrf_token, now, now, absolute_expires_at),
            )
        finally:
            conn.close()

    def get_session(self, session_hash: str) -> SessionRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_hash = ?", (session_hash,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_session(row) if row else None

    def touch_session(self, session_hash: str, now: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_hash = ?",
                (now, session_hash),
            )
        finally:
            conn.close()

    def revoke_session(self, session_hash: str, *, now: float | None = None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE session_hash = ?",
                (self._now() if now is None else now, session_hash),
            )
        finally:
            conn.close()

    def revoke_all_for_user(self, user_id: str) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (self._now(), user_id),
            )
            return cursor.rowcount or 0
        finally:
            conn.close()

    def purge_expired(self, now: float) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM sessions WHERE absolute_expires_at <= ? "
                "OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
                (now, now - 86400),
            )
            conn.execute("DELETE FROM login_states WHERE expires_at <= ?", (now,))
            conn.execute("DELETE FROM provider_codes WHERE expires_at <= ?", (now,))
            conn.execute(
                "DELETE FROM auth_attempts WHERE window_started_at <= ? AND locked_until <= ?",
                (now - 86400, now),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    # --- login states --------------------------------------------------------

    def insert_login_state(
        self,
        *,
        state_hash: str,
        expires_at: float,
        code_verifier: str | None = None,
        next_path: str | None = None,
        binding_hash: str | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO login_states (state_hash, created_at, expires_at,
                                          code_verifier, next_path, binding_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (state_hash, self._now(), expires_at, code_verifier, next_path, binding_hash),
            )
        finally:
            conn.close()

    def consume_login_state_bound(self, state_hash: str, binding_hash: str) -> LoginState | None:
        """Read, verify and delete a browser-bound login state in one transaction.

        Returns the record only when the digest the caller presented matches the
        one stored at ``start`` — compared with :func:`hmac.compare_digest` over
        fixed-length digests, so the check itself leaks no timing signal.

        A mismatch does **not** consume the row. That is deliberate: if merely
        presenting a state was enough to destroy it, anyone who saw the state
        (a shoulder-surfed URL, a shared proxy log) could deny the real browser
        its sign-in. Guessing the nonce is infeasible, so leaving the row alive
        costs nothing and keeps the failure mode a refusal rather than a
        denial. The row is removed on success, so a replay still loses.
        """
        now = self._now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM login_states WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            if row["expires_at"] <= now:
                conn.execute("DELETE FROM login_states WHERE state_hash = ?", (state_hash,))
                conn.execute("COMMIT")
                return None

            expected = row["binding_hash"]
            if not isinstance(expected, str) or not hmac.compare_digest(
                expected.encode("utf-8"), binding_hash.encode("utf-8")
            ):
                conn.execute("COMMIT")
                return None

            conn.execute("DELETE FROM login_states WHERE state_hash = ?", (state_hash,))
            conn.execute("COMMIT")
        finally:
            conn.close()
        return LoginState(
            state_hash=row["state_hash"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            code_verifier=row["code_verifier"],
            next_path=row["next_path"],
            binding_hash=row["binding_hash"],
        )

    def consume_login_state(self, state_hash: str) -> LoginState | None:
        """Atomically read and delete a login state.

        Returns ``None`` when the state is unknown or expired. Because the row
        is deleted in the same transaction, a replayed ``state`` can never be
        consumed twice — not even by two concurrent requests.
        """
        now = self._now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM login_states WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            if row is None or row["expires_at"] <= now:
                if row is not None:
                    conn.execute("DELETE FROM login_states WHERE state_hash = ?", (state_hash,))
                conn.execute("COMMIT")
                return None
            conn.execute("DELETE FROM login_states WHERE state_hash = ?", (state_hash,))
            conn.execute("COMMIT")
        finally:
            conn.close()
        return LoginState(
            state_hash=row["state_hash"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            code_verifier=row["code_verifier"],
            next_path=row["next_path"],
            binding_hash=row["binding_hash"],
        )

    def count_login_states(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) AS n FROM login_states").fetchone()["n"])
        finally:
            conn.close()

    # --- provider codes ------------------------------------------------------

    def claim_provider_code(self, code_hash: str, expires_at: float) -> bool:
        """One-shot claim of an authorisation code. False means 'already spent'."""
        now = self._now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM provider_codes WHERE expires_at <= ?", (now,))
            cursor = conn.execute(
                "INSERT OR IGNORE INTO provider_codes (code_hash, claimed_at, expires_at) "
                "VALUES (?, ?, ?)",
                (code_hash, now, expires_at),
            )
            claimed = cursor.rowcount == 1
            conn.execute("COMMIT")
        finally:
            conn.close()
        return claimed

    # --- rate limiting -------------------------------------------------------

    def read_attempt(self, scope: str, key: str) -> tuple[float, int, float] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT window_started_at, failures, locked_until FROM auth_attempts "
                "WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        finally:
            conn.close()
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
                INSERT INTO auth_attempts (scope, key, window_started_at, failures, locked_until)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET
                    window_started_at = excluded.window_started_at,
                    failures          = excluded.failures,
                    locked_until      = excluded.locked_until
                """,
                (scope, key, window_started_at, failures, locked_until),
            )
        finally:
            conn.close()

    def clear_attempt(self, scope: str, key: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM auth_attempts WHERE scope = ? AND key = ?", (scope, key))
        finally:
            conn.close()

    # --- audit ---------------------------------------------------------------

    def record_audit(
        self, event: str, outcome: str, *, actor: str | None = None, detail: str | None = None
    ) -> None:
        """Append an audit row. ``detail`` must never contain credential material."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO audit_events (at, event, outcome, actor, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._now(), event, outcome, actor, detail),
            )
        finally:
            conn.close()

    def list_audit_events(self, limit: int = 100) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT at, event, outcome, actor, detail FROM audit_events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        auth_source=row["auth_source"],
        external_id=row["external_id"],
        app_id=row["app_id"],
        union_id=row["union_id"],
        tenant_key=row["tenant_key"],
        display_name=row["display_name"],
        role=row["role"],
        status=row["status"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
    )


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        session_hash=row["session_hash"],
        user_id=row["user_id"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
        absolute_expires_at=row["absolute_expires_at"],
        revoked_at=row["revoked_at"],
    )


def _harden(path: Path, mode: int, *, is_dir: bool) -> None:
    """Best-effort restrictive permissions. No-op where the OS has no mode bits."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows / odd FS
        pass


__all__ = [
    "Database",
    "Grant",
    "LoginState",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "SOURCE_FEISHU",
    "SOURCE_LINUX",
    "SOURCE_LOCAL",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "SessionRecord",
    "User",
    "new_secret_id",
    "session_hash",
]
