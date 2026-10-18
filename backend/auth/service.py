"""Authentication use cases.

This is the layer that decides *policy*: when to count a failure, when a Feishu
identity becomes a member, what a redirect target may be, and what the browser
is allowed to learn. HTTP concerns live in :mod:`backend.auth.routes`; storage
lives in :mod:`backend.auth.store`; the provider protocol lives in
:mod:`backend.auth.feishu`.

Invariants enforced here, in one place, so no route can forget them:

* Only the local administrator can hold ``platform_admin``. A Feishu login calls
  ``upsert_feishu_user``, which can only ever produce a ``member`` with zero
  project grants — regardless of the person's name, e-mail or open_id.
* One-time values (login ``state``, authorisation ``code``, anonymous CSRF
  token) are claimed through the database, so a replay loses even under
  concurrent requests.
* A Feishu sign-in is bound to the browser that started it. ``state`` proves the
  request is one we minted; it does **not** prove the request came from the
  browser we minted it for. A second browser handed the same callback URL —
  the classic login-CSRF / session-confusion move, and trivial on a shared NAT —
  must therefore be refused, which is what the per-flow browser nonce is for.
* Anything a caller supplies is re-validated before it influences a redirect.
"""

from __future__ import annotations

import time
from typing import Callable

from . import errors, feishu, ratelimit, store
from .config import AuthConfig, is_local_path
from .feishu import FeishuProvider
from .local_admin import AdminCredentialStore
from .ratelimit import RateLimiter
from .sessions import IssuedSession, SessionManager
from .store import Database, User

# Short lifetimes: these values only have to survive one user interaction.
CSRF_TTL_SECONDS = 600
LOGIN_STATE_TTL_SECONDS = 600
PROVIDER_CODE_TTL_SECONDS = 900
# The browser-binding cookie lives exactly as long as the flow it belongs to,
# so a browser never holds a stale nonce and the server never accepts one.
OAUTH_BINDING_TTL_SECONDS = LOGIN_STATE_TTL_SECONDS


def _csrf_key(token: str) -> str:
    # Purpose-scoped digests keep the shared one-time-token table from letting a
    # login `state` be spent as a CSRF token or vice versa.
    return store.session_hash("csrf\x00" + token)


def _state_key(state: str) -> str:
    return store.session_hash("state\x00" + state)


def _code_key(code: str) -> str:
    return store.session_hash("code\x00" + code)


def _binding_key(nonce: str) -> str:
    # Purpose-scoped like the other one-time values, so a binding digest can
    # never be spent as a login state or a CSRF token.
    return store.session_hash("oauth-binding\x00" + nonce)


class AuthService:
    def __init__(
        self,
        cfg: AuthConfig,
        *,
        transport: feishu.Transport | None = None,
        provider: FeishuProvider | None = None,
        db: Database | None = None,
        admin_store: AdminCredentialStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self._clock = clock
        self.db = db if db is not None else Database(cfg.state_db_path, clock=clock)
        self.sessions = SessionManager(
            self.db,
            idle_seconds=cfg.session_idle_seconds,
            absolute_seconds=cfg.session_absolute_seconds,
            clock=clock,
        )
        self.limiter = RateLimiter(
            self.db,
            max_failures=cfg.login_max_failures,
            window_seconds=cfg.login_window_seconds,
            lockout_seconds=cfg.lockout_seconds,
            clock=clock,
        )
        self.admin = admin_store if admin_store is not None else AdminCredentialStore(
            cfg.admin_hash_file, username=cfg.admin_username
        )
        self.provider = (
            provider if provider is not None else feishu.provider_from_config(cfg, transport=transport)
        )

    def initialize(self) -> None:
        self.db.initialize()

    # --- discovery -----------------------------------------------------------

    @property
    def feishu_configured(self) -> bool:
        return self.provider is not None

    def providers(self) -> dict[str, bool]:
        """Which sign-in methods the UI may offer. Never raises."""
        return {"local_admin": self.local_admin_available(), "feishu": self.provider is not None}

    def local_admin_available(self) -> bool:
        try:
            return self.admin.is_configured()
        except errors.AuthError:
            # A broken or over-permissive credential file is not "available".
            return False

    # --- CSRF ----------------------------------------------------------------

    def issue_csrf(self, session_id: str | None = None) -> tuple[str, bool]:
        """Return ``(token, session_bound)``.

        With a live session the token is the stable, session-bound one; without
        one, a short-lived single-use token is minted so that a *login* cannot
        be forged cross-site either.
        """
        if session_id:
            try:
                session = self.sessions.authenticate(session_id)
            except errors.AuthError:
                session = None
            if session is not None:
                return session.csrf_token, True

        token = store.new_secret_id()
        self.db.insert_login_state(
            state_hash=_csrf_key(token), expires_at=self._clock() + CSRF_TTL_SECONDS
        )
        return token, False

    def verify_write_csrf(self, session_id: str | None, token: str | None) -> bool:
        if not isinstance(token, str) or not token:
            return False
        if session_id and self.sessions.verify_csrf(session_id, token):
            return True
        # Anonymous path: the token is single-use, so a replay loses.
        return self.db.consume_login_state(_csrf_key(token)) is not None

    # --- local administrator -------------------------------------------------

    def login_local(
        self, username: str, password: str, *, client_key: str
    ) -> tuple[User, IssuedSession]:
        key = f"{client_key}|{username}"
        self.limiter.check(ratelimit.SCOPE_LOCAL_LOGIN, key)

        try:
            accepted = self.admin.authenticate(username, password)
        except errors.AuthError as exc:
            self.db.record_audit("local_login", exc.code, actor=username)
            raise

        if not accepted:
            self.limiter.record_failure(ratelimit.SCOPE_LOCAL_LOGIN, key)
            self.db.record_audit("local_login", "failure", actor=username)
            raise errors.InvalidCredentials()

        self.limiter.record_success(ratelimit.SCOPE_LOCAL_LOGIN, key)
        user = self.db.ensure_local_user(self.cfg.admin_username)
        if not user.is_active:
            raise errors.AccountDisabled()
        self.db.record_audit("local_login", "success", actor=user.id)
        return user, self.sessions.create(user.id)

    # --- Feishu --------------------------------------------------------------

    def start_feishu(
        self, *, client_key: str, next_path: str | None = None
    ) -> tuple[str, str, str]:
        """Begin a Feishu sign-in.

        Returns ``(state, authorization_url, browser_nonce)``. The nonce is a
        fresh 256-bit value, independent of ``state``: ``state`` travels through
        the provider and back, whereas the nonce goes to the initiating browser
        alone and never appears in a URL, so a callback can only be completed by
        the browser that asked for it. Only its digest is persisted.
        """
        provider = self.provider
        if provider is None:
            raise errors.NotConfigured()

        self.limiter.check(ratelimit.SCOPE_FEISHU_START, client_key)
        state = store.new_secret_id()
        browser_nonce = store.new_secret_id()
        safe_next = next_path if is_local_path(next_path) else None
        self.db.insert_login_state(
            state_hash=_state_key(state),
            expires_at=self._clock() + LOGIN_STATE_TTL_SECONDS,
            next_path=safe_next,
            binding_hash=_binding_key(browser_nonce),
        )
        return state, provider.authorization_url(state=state), browser_nonce

    def complete_feishu(
        self, *, state: str, code: str, client_key: str, binding: str | None
    ) -> tuple[User, IssuedSession, str]:
        """Finish a Feishu sign-in, for the originating browser only.

        ``binding`` is the value of the flow cookie the browser presented.
        Order matters: the browser is authenticated *before* the state is spent
        and, crucially, before the authorisation code is sent to Feishu, so a
        callback that cannot prove it came from the right browser never causes
        an upstream token exchange.
        """
        provider = self.provider
        if provider is None:
            raise errors.NotConfigured()

        self.limiter.check(ratelimit.SCOPE_FEISHU_CALLBACK, client_key)

        if not isinstance(binding, str) or not binding:
            self.db.record_audit("feishu_callback", "rejected", detail="no browser binding cookie")
            raise errors.InvalidState(detail="the callback carried no browser binding cookie")

        record = self.db.consume_login_state_bound(_state_key(state or ""), _binding_key(binding))
        if record is None:
            # One case covers unknown, expired, already-spent and
            # wrong-browser alike: the client learns only that the sign-in
            # attempt is not usable, never which of those it was.
            self.db.record_audit("feishu_callback", "rejected", detail="binding check failed")
            raise errors.InvalidState(
                detail="unknown, expired, already used, or not this browser's state"
            )

        if not self.db.claim_provider_code(
            _code_key(code), self._clock() + PROVIDER_CODE_TTL_SECONDS
        ):
            raise errors.InvalidState(detail="the authorisation code was already used")

        identity = provider.exchange_code(code=code)

        app_id = self.cfg.feishu_app_id
        if not app_id:
            # The provider only exists when the app id does, so this is a
            # belt-and-braces refusal: keying an identity under an empty app id
            # would silently merge unrelated tenants into one local user.
            raise errors.NotConfigured(detail="the Feishu app id is not configured")

        user = self.db.upsert_feishu_user(
            app_id=app_id,
            open_id=identity.open_id,
            union_id=identity.union_id,
            tenant_key=identity.tenant_key,
            display_name=identity.display_name,
        )
        if not user.is_active:
            self.db.record_audit("feishu_login", "disabled", actor=user.id)
            raise errors.AccountDisabled()

        issued = self.sessions.create(user.id)
        self.db.record_audit("feishu_login", "success", actor=user.id)
        target = record.next_path if is_local_path(record.next_path) else self.cfg.post_login_path
        return user, issued, target

    # --- session -------------------------------------------------------------

    def current_session(self, session_id: str | None):
        if not session_id:
            raise errors.NotAuthenticated()
        return self.sessions.authenticate(session_id)

    def logout(self, session_id: str) -> None:
        self.sessions.revoke(session_id)

    # --- presentation --------------------------------------------------------

    def public_identity(self, user: User) -> dict[str, object]:
        """The minimum a UI needs. No upstream identifiers, no credential material."""
        return {
            "id": user.id,
            "display_name": user.display_name,
            "role": user.role,
            "auth_source": user.auth_source,
            "is_platform_admin": user.is_admin,
        }

    def projects_for(self, user: User) -> list[dict[str, str]]:
        return [
            {"project_id": grant.project_id, "role": grant.role}
            for grant in self.db.list_project_grants(user.id)
        ]


__all__ = [
    "AuthService",
    "CSRF_TTL_SECONDS",
    "LOGIN_STATE_TTL_SECONDS",
    "OAUTH_BINDING_TTL_SECONDS",
    "PROVIDER_CODE_TTL_SECONDS",
]
