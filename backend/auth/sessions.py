"""Server-side sessions.

Properties this module is responsible for, and the reason each one exists:

* **The cookie carries an opaque 256-bit id; the database carries only its
  digest.** A dump of the state directory therefore yields no usable session.
* **Idle *and* absolute expiry.** Idle expiry bounds a stolen cookie that is
  used (or not) later; absolute expiry bounds a session that is used
  continuously, so a compromised client cannot stay logged in forever.
* **Rotation on sign-in.** A pre-login identifier is revoked the moment a user
  authenticates, which is the session-fixation defence.
* **Revocation on sign-out**, plus revocation when an account is disabled.
* **A CSRF token bound to the session**, compared in constant time. It is
  delivered only by a same-origin endpoint and never placed in local storage.

No provider credential (a Feishu ``user_access_token`` in particular) is ever
written into a cookie or into the session row. The session is the *only* thing
the browser holds.
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from typing import Callable

from . import errors, store
from .store import Database, SessionRecord, User

CSRF_ID_BYTES = 24


@dataclass(frozen=True)
class IssuedSession:
    session_id: str
    csrf_token: str
    created_at: float
    absolute_expires_at: float


@dataclass(frozen=True)
class AuthenticatedSession:
    user: User
    session_id: str
    csrf_token: str
    record: SessionRecord


class SessionManager:
    def __init__(
        self,
        db: Database,
        *,
        idle_seconds: int,
        absolute_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._idle = int(idle_seconds)
        self._absolute = int(absolute_seconds)
        self._clock = clock

    # --- lifecycle -----------------------------------------------------------

    def create(self, user_id: str) -> IssuedSession:
        session_id = store.new_secret_id()
        csrf_token = store.new_secret_id(CSRF_ID_BYTES)
        now = self._clock()
        absolute_expires_at = now + self._absolute
        self._db.insert_session(
            session_hash=store.session_hash(session_id),
            user_id=user_id,
            csrf_token=csrf_token,
            absolute_expires_at=absolute_expires_at,
        )
        return IssuedSession(
            session_id=session_id,
            csrf_token=csrf_token,
            created_at=now,
            absolute_expires_at=absolute_expires_at,
        )

    def authenticate(self, session_id: str) -> AuthenticatedSession:
        """Resolve a cookie value to a live session and active user."""
        if not isinstance(session_id, str) or not session_id:
            raise errors.NotAuthenticated()

        digest = store.session_hash(session_id)
        record = self._db.get_session(digest)
        if record is None:
            raise errors.NotAuthenticated()

        now = self._clock()
        if record.revoked_at is not None:
            raise errors.SessionExpired(detail="session was revoked")
        if now >= record.absolute_expires_at:
            self._db.revoke_session(digest, now=now)
            raise errors.SessionExpired(detail="absolute lifetime reached")
        if now - record.last_seen_at >= self._idle:
            self._db.revoke_session(digest, now=now)
            raise errors.SessionExpired(detail="idle timeout reached")

        # Sliding window: the request itself counts as activity.
        self._db.touch_session(digest, now)

        user = self._db.get_user(record.user_id)
        if user is None:
            self._db.revoke_session(digest, now=now)
            raise errors.SessionExpired(detail="session owner no longer exists")
        if not user.is_active:
            self._db.revoke_session(digest, now=now)
            raise errors.AccountDisabled()

        return AuthenticatedSession(
            user=user, session_id=session_id, csrf_token=record.csrf_token, record=record
        )

    def rotate(self, session_id: str) -> IssuedSession:
        """Revoke ``session_id`` and issue a fresh one for the same user."""
        digest = store.session_hash(session_id)
        record = self._db.get_session(digest)
        if record is None:
            raise errors.NotAuthenticated()

        now = self._clock()
        if record.revoked_at is not None or now >= record.absolute_expires_at:
            raise errors.SessionExpired()

        user = self._db.get_user(record.user_id)
        if user is None:
            raise errors.SessionExpired()
        if not user.is_active:
            raise errors.AccountDisabled()

        self._db.revoke_session(digest, now=now)
        return self.create(record.user_id)

    def revoke(self, session_id: str) -> None:
        if isinstance(session_id, str) and session_id:
            self._db.revoke_session(store.session_hash(session_id))

    def revoke_all_for_user(self, user_id: str) -> int:
        return self._db.revoke_all_for_user(user_id)

    def purge(self) -> None:
        self._db.purge_expired(self._clock())

    # --- CSRF ----------------------------------------------------------------

    def verify_csrf(self, session_id: str, token: str) -> bool:
        if not isinstance(token, str) or not token:
            return False
        record = self._db.get_session(store.session_hash(session_id or ""))
        if record is None or record.revoked_at is not None:
            return False
        if self._clock() >= record.absolute_expires_at:
            return False
        return hmac.compare_digest(record.csrf_token.encode("utf-8"), token.encode("utf-8"))

    # --- cookie helpers ------------------------------------------------------

    def cookie_header(self, session_id: str, *, name: str, secure: bool) -> str:
        """Session cookie attributes.

        No ``Max-Age``: this is a browser-session cookie. The authoritative
        lifetime lives server-side, and an idle timeout has no meaningful
        browser-side equivalent anyway.
        """
        parts = [f"{name}={session_id}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self, *, name: str, secure: bool) -> str:
        parts = [f"{name}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)


__all__ = ["AuthenticatedSession", "IssuedSession", "SessionManager"]
