"""Platform accounts: username + password logins that do not depend on Linux.

Issue #17 asks for LLM Lab accounts that are **independent** from the host's
Linux accounts. A platform username and a Linux username may happen to be the
same string, and an operator may choose the same initial password for both, but
nothing here reads PAM, ``/etc/shadow``, ``getpwnam`` or SSH, and nothing here
copies a password from one side to the other.

Rules this module enforces, so no route can forget them:

* **The principal id is random, not derived from the username.** A user row is
  keyed ``account:<128-bit hex>``. Renaming, or creating a second account whose
  display name equals a Linux name, can never collide with or inherit another
  principal's grants.
* **Passwords exist only as salted scrypt hashes** (:mod:`backend.auth.hashing`).
  An administrator never chooses a member's password: creation and reset return
  a generated one-time password, shown once, and the account must set its own
  password before the session is good for anything else.
* **Login failures are generic.** Unknown user, wrong password and disabled
  account all answer ``invalid_credentials``, and an unknown user still pays
  for one scrypt verification so the timing does not tell them apart either.
* **Password change and reset revoke every session**; disabling an account
  revokes its sessions and (when wired) its scoped tokens.
* **``linux_user`` is a label, not a credential.** It is an optional, explicit,
  administrator-set mapping used later for host-scoped ownership checks. It is
  never looked up in the OS here and a missing or wrong mapping never blocks a
  platform login.
* **An administrator cannot demote or disable themself**, so the last admin
  cannot lock the platform out by accident.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import errors, hashing
from .ratelimit import RateLimiter
from .sessions import IssuedSession, SessionManager
from .store import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    Database,
    User,
)

SOURCE_PLATFORM = "platform"
SCOPE_ACCOUNT_LOGIN = "account_login"
SCOPE_ACCOUNT_PASSWORD = "account_password"

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
#: POSIX portable login names; the same shape ``useradd`` accepts by default.
LINUX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
DISPLAY_NAME_MAX = 64

PASSWORD_MIN = 10
PASSWORD_MAX = 256

#: Unambiguous alphabet (no 0/O/1/l/I) for one-time passwords read off a screen.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_TEMP_GROUPS = 4
_TEMP_GROUP_LEN = 4

#: API-facing role names. The store keeps the established ``platform_admin``.
API_ROLE_ADMIN = "admin"
API_ROLE_MEMBER = "member"
_ROLE_TO_STORE = {API_ROLE_ADMIN: ROLE_ADMIN, API_ROLE_MEMBER: ROLE_MEMBER}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS platform_accounts (
    user_id              TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    username             TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,
    must_change_password INTEGER NOT NULL,
    password_changed_at  REAL NOT NULL,
    linux_user           TEXT,
    created_by           TEXT
);
"""


@dataclass(frozen=True)
class Account:
    user: User
    username: str
    must_change_password: bool
    password_changed_at: float
    linux_user: str | None

    def to_public_dict(self) -> dict[str, object]:
        """What the admin list and ``/auth/me`` may show. No hash, ever."""
        return {
            "id": self.user.id,
            "username": self.username,
            "display_name": self.user.display_name,
            "linux_user": self.linux_user,
            "role": API_ROLE_ADMIN if self.user.is_admin else API_ROLE_MEMBER,
            "status": self.user.status,
            "must_change_password": self.must_change_password,
            "created_at": _iso(self.user.created_at),
            "last_login_at": _iso(self.user.last_login_at),
        }


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_temporary_password() -> str:
    """``xxxx-xxxx-xxxx-xxxx`` from a 31-symbol alphabet: ~79 bits."""
    groups = (
        "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(_TEMP_GROUP_LEN))
        for _ in range(_TEMP_GROUPS)
    )
    return "-".join(groups)


def normalize_username(value: object) -> str:
    if not isinstance(value, str):
        raise errors.InvalidRequest(detail="username must be a string")
    name = value.strip().lower()
    if not USERNAME_RE.match(name):
        raise errors.InvalidRequest(
            detail="username must be 2-32 characters: a-z, 0-9, '.', '_' or '-', starting with a letter"
        )
    return name


def normalize_linux_user(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise errors.InvalidRequest(detail="linux_user must be a string")
    name = value.strip()
    if not name:
        return None
    if not LINUX_USER_RE.match(name):
        raise errors.InvalidRequest(detail="linux_user is not a valid Linux login name")
    return name


def normalize_display_name(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise errors.InvalidRequest(detail="display_name must be a string")
    name = " ".join(value.split())
    if len(name) > DISPLAY_NAME_MAX:
        raise errors.InvalidRequest(detail=f"display_name is longer than {DISPLAY_NAME_MAX} characters")
    if any(ord(ch) < 0x20 for ch in name):
        raise errors.InvalidRequest(detail="display_name contains control characters")
    return name or fallback


def normalize_role(value: object) -> str:
    if value not in _ROLE_TO_STORE:
        raise errors.InvalidRequest(detail="role must be 'admin' or 'member'")
    return _ROLE_TO_STORE[value]


def check_new_password(password: object) -> str:
    if not isinstance(password, str):
        raise errors.InvalidRequest(detail="new_password must be a string")
    if len(password) < PASSWORD_MIN:
        raise errors.InvalidRequest(detail=f"new_password must be at least {PASSWORD_MIN} characters")
    if len(password) > PASSWORD_MAX:
        raise errors.InvalidRequest(detail=f"new_password must be at most {PASSWORD_MAX} characters")
    return password


class AccountStore:
    """SQLite persistence for platform accounts, in the auth database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def initialize(self) -> None:
        conn = self.db._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    _SELECT = (
        "SELECT u.*, a.username AS a_username, a.must_change_password AS a_must, "
        "a.password_changed_at AS a_changed, a.linux_user AS a_linux, a.password_hash AS a_hash "
        "FROM platform_accounts a JOIN users u ON u.id = a.user_id"
    )

    def _one(self, where: str, args: tuple) -> tuple[Account, str] | None:
        conn = self.db._connect()
        try:
            row = conn.execute(f"{self._SELECT} WHERE {where}", args).fetchone()
        finally:
            conn.close()
        return (_row_to_account(row), row["a_hash"]) if row else None

    def by_username(self, username: str) -> tuple[Account, str] | None:
        return self._one("a.username = ?", (username,))

    def by_id(self, user_id: str) -> tuple[Account, str] | None:
        return self._one("a.user_id = ?", (user_id,))

    def list(self) -> list[Account]:
        conn = self.db._connect()
        try:
            rows = conn.execute(f"{self._SELECT} ORDER BY u.created_at, a.username").fetchall()
        finally:
            conn.close()
        return [_row_to_account(row) for row in rows]

    def create(
        self,
        *,
        username: str,
        display_name: str,
        role: str,
        linux_user: str | None,
        password_hash: str,
        created_by: str,
        now: float,
    ) -> Account:
        user_id = "account:" + secrets.token_hex(16)
        conn = self.db._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO users (id, auth_source, external_id, app_id, union_id, tenant_key, "
                    "display_name, role, status, created_at) VALUES (?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)",
                    (user_id, SOURCE_PLATFORM, display_name, role, STATUS_ACTIVE, now),
                )
                conn.execute(
                    "INSERT INTO platform_accounts (user_id, username, password_hash, must_change_password, "
                    "password_changed_at, linux_user, created_by) VALUES (?, ?, ?, 1, ?, ?, ?)",
                    (user_id, username, password_hash, now, linux_user, created_by),
                )
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise errors.InvalidRequest(detail="that username is already taken") from None
            conn.execute("COMMIT")
        finally:
            conn.close()
        found = self.by_id(user_id)
        assert found is not None
        return found[0]

    def set_password(self, user_id: str, password_hash: str, *, must_change: bool, now: float) -> None:
        conn = self.db._connect()
        try:
            conn.execute(
                "UPDATE platform_accounts SET password_hash = ?, must_change_password = ?, "
                "password_changed_at = ? WHERE user_id = ?",
                (password_hash, 1 if must_change else 0, now, user_id),
            )
        finally:
            conn.close()

    def set_linux_user(self, user_id: str, linux_user: str | None) -> None:
        conn = self.db._connect()
        try:
            conn.execute(
                "UPDATE platform_accounts SET linux_user = ? WHERE user_id = ?", (linux_user, user_id)
            )
        finally:
            conn.close()

    def touch_login(self, user_id: str, now: float) -> None:
        conn = self.db._connect()
        try:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))
        finally:
            conn.close()


def _row_to_account(row: sqlite3.Row) -> Account:
    user = User(
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
    return Account(
        user=user,
        username=row["a_username"],
        must_change_password=bool(row["a_must"]),
        password_changed_at=row["a_changed"],
        linux_user=row["a_linux"],
    )


class AccountService:
    """Account use cases. HTTP lives in :mod:`backend.auth.routes`."""

    def __init__(
        self,
        db: Database,
        sessions: SessionManager,
        limiter: RateLimiter,
        *,
        clock: Callable[[], float] = time.time,
        revoke_tokens: Callable[[str], object] | None = None,
    ) -> None:
        self.db = db
        self.store = AccountStore(db)
        self.sessions = sessions
        self.limiter = limiter
        self._clock = clock
        self.revoke_tokens = revoke_tokens
        # Verified against when the username is unknown, so a miss costs the
        # same scrypt work as a hit. Generated per process; never persisted.
        self._dummy_hash = hashing.hash_password(secrets.token_urlsafe(24))

    def initialize(self) -> None:
        self.store.initialize()

    # --- lookups -------------------------------------------------------------

    def account_for(self, user: User) -> Account | None:
        if user.auth_source != SOURCE_PLATFORM:
            return None
        found = self.store.by_id(user.id)
        return found[0] if found else None

    def list_accounts(self) -> list[Account]:
        return self.store.list()

    def _require(self, user_id: object) -> Account:
        if not isinstance(user_id, str) or not user_id:
            raise errors.InvalidRequest(detail="user_id is required")
        found = self.store.by_id(user_id)
        if found is None:
            raise errors.InvalidRequest(detail="no such user")
        return found[0]

    # --- self service --------------------------------------------------------

    def login(self, username: object, password: object, *, client_key: str) -> tuple[Account, IssuedSession]:
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            raise errors.InvalidRequest(detail="username and password are required")
        name = username.strip().lower()
        key = f"{client_key}|{name}"
        self.limiter.check(SCOPE_ACCOUNT_LOGIN, key)

        found = self.store.by_username(name) if USERNAME_RE.match(name) else None
        if found is None:
            hashing.verify_password(password, self._dummy_hash)
            accepted = False
        else:
            accepted = hashing.verify_password(password, found[1])

        if not accepted or not found[0].user.is_active:
            self.limiter.record_failure(SCOPE_ACCOUNT_LOGIN, key)
            self.db.record_audit("account_login", "failure", actor=found[0].user.id if found else None)
            raise errors.InvalidCredentials()

        account = found[0]
        self.limiter.record_success(SCOPE_ACCOUNT_LOGIN, key)
        now = self._clock()
        self.store.touch_login(account.user.id, now)
        self.db.record_audit("account_login", "success", actor=account.user.id)
        issued = self.sessions.create(account.user.id)
        refreshed = self.store.by_id(account.user.id)
        return (refreshed[0] if refreshed else account), issued

    def change_password(
        self, user: User, current_password: object, new_password: object
    ) -> tuple[Account, IssuedSession]:
        account = self.account_for(user)
        if account is None:
            raise errors.Forbidden(detail="this session is not a platform account")
        key = account.user.id
        self.limiter.check(SCOPE_ACCOUNT_PASSWORD, key)
        if not isinstance(current_password, str) or not current_password:
            raise errors.InvalidRequest(detail="current_password is required")
        new = check_new_password(new_password)

        found = self.store.by_id(account.user.id)
        if found is None or not hashing.verify_password(current_password, found[1]):
            self.limiter.record_failure(SCOPE_ACCOUNT_PASSWORD, key)
            self.db.record_audit("account_password", "failure", actor=account.user.id)
            raise errors.InvalidCredentials()
        if hashing.verify_password(new, found[1]):
            raise errors.InvalidRequest(detail="new_password must differ from the current password")

        self.limiter.record_success(SCOPE_ACCOUNT_PASSWORD, key)
        self.store.set_password(account.user.id, hashing.hash_password(new), must_change=False, now=self._clock())
        # Every existing session — this one included — dies with the old
        # password; the caller gets a fresh session in its place.
        self.sessions.revoke_all_for_user(account.user.id)
        self.db.record_audit("account_password", "success", actor=account.user.id)
        issued = self.sessions.create(account.user.id)
        refreshed = self.store.by_id(account.user.id)
        return (refreshed[0] if refreshed else account), issued

    # --- administration ------------------------------------------------------

    def create(self, actor: User, body: dict) -> tuple[Account, str]:
        username = normalize_username(body.get("username"))
        display_name = normalize_display_name(body.get("display_name"), username)
        role = normalize_role(body.get("role", API_ROLE_MEMBER))
        linux_user = normalize_linux_user(body.get("linux_user"))
        temporary = generate_temporary_password()
        account = self.store.create(
            username=username,
            display_name=display_name,
            role=role,
            linux_user=linux_user,
            password_hash=hashing.hash_password(temporary),
            created_by=actor.id,
            now=self._clock(),
        )
        self.db.record_audit("account_create", "success", actor=actor.id, detail=account.user.id)
        return account, temporary

    def reset_password(self, actor: User, user_id: object) -> tuple[Account, str]:
        account = self._require(user_id)
        temporary = generate_temporary_password()
        self.store.set_password(
            account.user.id, hashing.hash_password(temporary), must_change=True, now=self._clock()
        )
        self.sessions.revoke_all_for_user(account.user.id)
        self.db.record_audit("account_reset", "success", actor=actor.id, detail=account.user.id)
        return self._require(account.user.id), temporary

    def set_role(self, actor: User, user_id: object, role: object) -> Account:
        account = self._require(user_id)
        store_role = normalize_role(role)
        if account.user.id == actor.id and store_role != ROLE_ADMIN:
            raise errors.Forbidden(detail="administrators cannot remove their own admin role")
        self.db.set_user_role(account.user.id, store_role)
        self.db.record_audit("account_role", store_role, actor=actor.id, detail=account.user.id)
        return self._require(account.user.id)

    def set_linux_user(self, actor: User, user_id: object, linux_user: object) -> Account:
        account = self._require(user_id)
        self.store.set_linux_user(account.user.id, normalize_linux_user(linux_user))
        self.db.record_audit("account_linux", "success", actor=actor.id, detail=account.user.id)
        return self._require(account.user.id)

    def set_status(self, actor: User, user_id: object, status: object) -> Account:
        account = self._require(user_id)
        if status not in (STATUS_ACTIVE, STATUS_DISABLED):
            raise errors.InvalidRequest(detail="status must be 'active' or 'disabled'")
        if account.user.id == actor.id and status != STATUS_ACTIVE:
            raise errors.Forbidden(detail="administrators cannot disable their own account")
        self.db.set_user_status(account.user.id, status)
        if status == STATUS_DISABLED:
            self.sessions.revoke_all_for_user(account.user.id)
            if self.revoke_tokens is not None:
                self.revoke_tokens(account.user.id)
        self.db.record_audit("account_status", status, actor=actor.id, detail=account.user.id)
        return self._require(account.user.id)


__all__ = [
    "API_ROLE_ADMIN",
    "API_ROLE_MEMBER",
    "Account",
    "AccountService",
    "AccountStore",
    "PASSWORD_MIN",
    "SCOPE_ACCOUNT_LOGIN",
    "SOURCE_PLATFORM",
    "generate_temporary_password",
]
