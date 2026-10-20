"""Platform accounts, sessions and scopes — independent of Linux.

Identity model
--------------
A platform account has a stable, unguessable ``platform_user_id`` (``pu_...``)
and a separate ``login_name`` used only for signing in. Neither is a Linux
account, a UID, a PAM name, or an SSH identity. There is no ``linux_uid`` field
anywhere in this package, and no code path derives a platform principal from a
Linux fact. If a future feature needs an OS identity it must map one explicitly,
under its own authorization; a missing OS account must never break platform
login.

Authentication
--------------
``login()`` verifies a platform password against a salted scrypt hash and issues
a session token that is returned **once**. The database stores only a SHA-256
digest of the token and of the CSRF token. Wrong password, unknown login, empty
password and a malformed hash all produce the same ``invalid_credentials``
answer, and an unknown login still pays for one scrypt verification so timing
does not enumerate accounts. Failures are throttled per login key (itself
hashed, so the throttle table holds no account name).

Sessions
--------
Absolute TTL and idle TTL are both enforced. A clock that reads earlier than the
session's creation is treated as invalid rather than "very fresh". Disabling an
account or changing its password revokes every session immediately (the session
carries the password epoch and is checked on every resolution).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import passwords
from .errors import (
    AccountDisabled,
    InvalidCredentials,
    NotAuthenticated,
    RateLimited,
    ValidationError,
)
from .store import Store

#: Scopes are platform concepts. They are *not* Linux groups or PAM roles.
SCOPE_READ = "platform:read"
SCOPE_ADMIN = "platform:admin:manage"
SCOPE_INVOKE = "platform:invoke"
KNOWN_SCOPES = frozenset({SCOPE_READ, SCOPE_ADMIN, SCOPE_INVOKE})

SESSION_TTL_SECONDS = 8 * 60 * 60
IDLE_TTL_SECONDS = 30 * 60
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 5 * 60
LOCKOUT_SECONDS = 15 * 60

USER_ID_PREFIX = "pu_"
USER_ID_BYTES = 24
SESSION_TOKEN_BYTES = 48
CSRF_TOKEN_BYTES = 32


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_platform_user_id() -> str:
    return USER_ID_PREFIX + secrets.token_urlsafe(USER_ID_BYTES)


def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def normalise_login(login_name: object) -> str:
    if not isinstance(login_name, str):
        raise ValidationError("login name must be text")
    name = login_name.strip()
    if not name or len(name) > 64:
        raise ValidationError("login name must be 1..64 characters")
    if any(ch.isspace() for ch in name):
        raise ValidationError("login name must not contain whitespace")
    return name.lower()


def validate_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    values = tuple(sorted(set(scopes)))
    unknown = [scope for scope in values if scope not in KNOWN_SCOPES]
    if unknown:
        raise ValidationError("unknown platform scope")
    if not values:
        raise ValidationError("at least one scope is required")
    return values


@dataclass(frozen=True)
class PlatformPrincipal:
    """An authenticated platform subject.

    Deliberately has no ``uid``/``linux_user``: those belong to the host, and a
    platform session never implies one.
    """

    platform_user_id: str
    login_name: str
    display_name: str | None
    scopes: tuple[str, ...]
    session_id: str

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def to_dict(self) -> dict:
        return {
            "platform_user_id": self.platform_user_id,
            "login_name": self.login_name,
            "display_name": self.display_name,
            "scopes": list(self.scopes),
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class LoginResult:
    principal: PlatformPrincipal
    token: str          # returned exactly once; never stored in this form
    csrf_token: str     # returned exactly once; never stored in this form
    expires_at: float

    def to_dict(self) -> dict:
        return {
            "principal": self.principal.to_dict(),
            "token": self.token,
            "csrf_token": self.csrf_token,
            "expires_at": self.expires_at,
        }


@dataclass
class PlatformAuthService:
    store: Store
    clock: Callable[[], float] = time.time
    session_ttl: int = SESSION_TTL_SECONDS
    idle_ttl: int = IDLE_TTL_SECONDS
    max_failures: int = MAX_FAILURES
    failure_window: int = FAILURE_WINDOW_SECONDS
    lockout: int = LOCKOUT_SECONDS

    # --- accounts -------------------------------------------------------

    def create_user(
        self,
        login_name: object,
        password: object,
        scopes: Iterable[str],
        *,
        display_name: str | None = None,
    ) -> dict:
        login = normalise_login(login_name)
        if self.store.user_by_login(login) is not None:
            # A duplicate is a real conflict for an operator-driven CLI, not an
            # unauthenticated endpoint, so naming it does not enumerate anything.
            raise ValidationError("login name already exists")
        hashed = passwords.hash_password(password)
        moment = self.clock()
        user_id = new_platform_user_id()
        self.store.insert_user(
            {
                "platform_user_id": user_id,
                "login_name": login,
                "display_name": display_name,
                "password_hash": hashed,
                "scopes": ",".join(validate_scopes(scopes)),
                "disabled": 0,
                "created_at": moment,
                "password_changed_at": moment,
            }
        )
        self.store.audit("platform_user_created", subject=user_id)
        return self._public_user(self.store.user_by_id(user_id))

    def set_password(self, platform_user_id: str, password: object) -> None:
        user = self._require_user(platform_user_id)
        hashed = passwords.hash_password(password)
        moment = self.clock()
        self.store.update_secret(user["platform_user_id"], hashed, moment)
        self.store.revoke_all_sessions(user["platform_user_id"])
        self.store.audit("platform_password_changed", subject=user["platform_user_id"])

    def set_disabled(self, platform_user_id: str, disabled: bool) -> None:
        user = self._require_user(platform_user_id)
        self.store.set_disabled(user["platform_user_id"], disabled)
        if disabled:
            self.store.revoke_all_sessions(user["platform_user_id"])
        self.store.audit(
            "platform_user_disabled" if disabled else "platform_user_enabled",
            subject=user["platform_user_id"],
        )

    def list_users(self) -> list[dict]:
        return [self._public_user(row) for row in self.store.list_users()]

    # --- login / sessions -----------------------------------------------

    def login(
        self,
        login_name: object,
        password: object,
        *,
        remote_key: str | None = None,
    ) -> LoginResult:
        try:
            login = normalise_login(login_name)
        except ValidationError:
            # An unparseable login is still one indistinguishable failure.
            passwords.burn_verification(password)
            raise InvalidCredentials()

        key = self._throttle_key(login, remote_key)
        now = self.clock()
        self._check_throttle(key, now)

        user = self.store.user_by_login(login)
        if user is None:
            passwords.burn_verification(password)
            self._record_failure(key, now)
            self.store.audit("platform_login_failed", subject=None)
            raise InvalidCredentials()

        if not passwords.verify_password(user["password_hash"], password):
            self._record_failure(key, now)
            self.store.audit("platform_login_failed", subject=user["platform_user_id"])
            raise InvalidCredentials()

        if int(user["disabled"]):
            self.store.audit("platform_login_disabled", subject=user["platform_user_id"])
            raise AccountDisabled()

        self.store.clear_throttle(key)
        token = new_session_token()
        csrf = new_csrf_token()
        expires_at = now + self.session_ttl
        self.store.insert_session(
            {
                "token_digest": _digest(token),
                "platform_user_id": user["platform_user_id"],
                "created_at": now,
                "expires_at": expires_at,
                "idle_expires_at": now + self.idle_ttl,
                "csrf_digest": _digest(csrf),
                "user_epoch": float(user["password_changed_at"]),
            }
        )
        self.store.audit("platform_login_succeeded", subject=user["platform_user_id"])
        principal = self._principal(user, _digest(token))
        return LoginResult(principal=principal, token=token, csrf_token=csrf,
                           expires_at=expires_at)

    def resolve_session(self, token: object) -> PlatformPrincipal:
        """Every authenticated request goes through here."""
        if not isinstance(token, str) or not token:
            raise NotAuthenticated()
        digest = _digest(token)
        row = self.store.session_by_digest(digest)
        if row is None:
            raise NotAuthenticated()
        if int(row["revoked"]):
            raise NotAuthenticated()

        now = self.clock()
        # A clock behind the session's creation is a disagreement, not freshness.
        if now < float(row["created_at"]):
            raise NotAuthenticated()
        if now >= float(row["expires_at"]) or now >= float(row["idle_expires_at"]):
            raise NotAuthenticated()

        user = self.store.user_by_id(row["platform_user_id"])
        if user is None:
            raise NotAuthenticated()
        if int(user["disabled"]):
            raise NotAuthenticated()
        if float(user["password_changed_at"]) != float(row["user_epoch"]):
            raise NotAuthenticated()

        self.store.touch_session(digest, now + self.idle_ttl)
        return self._principal(user, digest)

    def verify_csrf(self, principal: PlatformPrincipal, csrf_token: object) -> None:
        from .errors import CsrfRejected

        if not isinstance(csrf_token, str) or not csrf_token:
            raise CsrfRejected()
        row = self.store.session_by_digest(principal.session_id)
        if row is None:
            raise NotAuthenticated()
        if not hmac.compare_digest(_digest(csrf_token), row["csrf_digest"]):
            raise CsrfRejected()

    def revoke_session(self, token: object) -> None:
        if not isinstance(token, str) or not token:
            raise NotAuthenticated()
        digest = _digest(token)
        if self.store.session_by_digest(digest) is None:
            raise NotAuthenticated()
        self.store.revoke_session(digest)
        self.store.audit("platform_session_revoked", subject=digest[:12])

    def revoke_all_for(self, platform_user_id: str) -> int:
        user = self._require_user(platform_user_id)
        return self.store.revoke_all_sessions(user["platform_user_id"])

    # --- internals ------------------------------------------------------

    def _require_user(self, platform_user_id: str):
        user = self.store.user_by_id(platform_user_id)
        if user is None:
            raise ValidationError("unknown platform user")
        return user

    @staticmethod
    def _public_user(row) -> dict:
        return {
            "platform_user_id": row["platform_user_id"],
            "login_name": row["login_name"],
            "display_name": row["display_name"],
            "scopes": [s for s in str(row["scopes"]).split(",") if s],
            "disabled": bool(row["disabled"]),
            "created_at": float(row["created_at"]),
            "password_changed_at": float(row["password_changed_at"]),
        }

    @staticmethod
    def _principal(row, session_id: str) -> PlatformPrincipal:
        return PlatformPrincipal(
            platform_user_id=row["platform_user_id"],
            login_name=row["login_name"],
            display_name=row["display_name"],
            scopes=tuple(s for s in str(row["scopes"]).split(",") if s),
            session_id=session_id,
        )

    @staticmethod
    def _throttle_key(login: str, remote_key: str | None) -> str:
        # The throttle table holds a digest, never an account name.
        remote = remote_key or "local"
        return hashlib.sha256(f"{login}|{remote}".encode("utf-8")).hexdigest()

    def _check_throttle(self, key: str, now: float) -> None:
        row = self.store.throttle(key)
        if row is None:
            return
        if float(row["locked_until"]) > now:
            raise RateLimited()

    def _record_failure(self, key: str, now: float) -> None:
        row = self.store.throttle(key)
        if row is None or now - float(row["window_start"]) > self.failure_window:
            failures = 1
            window_start = now
        else:
            failures = int(row["failures"]) + 1
            window_start = float(row["window_start"])
        locked_until = now + self.lockout if failures >= self.max_failures else 0.0
        self.store.put_throttle(key, failures, window_start, locked_until)
        if locked_until:
            self.store.audit("platform_login_locked", subject=None)


__all__ = [
    "CSRF_TOKEN_BYTES",
    "IDLE_TTL_SECONDS",
    "KNOWN_SCOPES",
    "LOCKOUT_SECONDS",
    "MAX_FAILURES",
    "SCOPE_ADMIN",
    "SCOPE_INVOKE",
    "SCOPE_READ",
    "SESSION_TOKEN_BYTES",
    "SESSION_TTL_SECONDS",
    "LoginResult",
    "PlatformAuthService",
    "PlatformPrincipal",
    "new_csrf_token",
    "new_platform_user_id",
    "new_session_token",
    "normalise_login",
    "validate_scopes",
]
