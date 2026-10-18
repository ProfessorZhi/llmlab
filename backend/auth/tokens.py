"""Scoped server-side tokens for the ``llmlab`` CLI and machine clients.

What a token is, and what it deliberately is not
------------------------------------------------
A scoped token is a *bearer credential that the portal issues to a principal
that has already proved who it is through a session*. It is what lets the CLI
and automated agents call the same Registry/Gateway surface the web uses
**without ever handling a Linux password**. The Linux password is used once, at
sign-in, over the PAM broker; everything afterwards is a token.

Consequences that are baked into this module rather than left to a caller:

* **The plaintext exists exactly once.** :meth:`ScopedTokenService.issue`
  returns it and nothing else ever does — not :meth:`list_for_principal`, not
  :meth:`introspect`, not the audit trail, not ``repr``. At rest the database
  holds only ``sha256(token)``, the same discipline
  :mod:`backend.auth.sessions` applies to session identifiers. A copy of the
  token database is therefore not a set of usable credentials.
* **A token carries scopes, and a scope is a capability, not a role.** Nothing
  here infers a scope from ``role``. The caller (the BFF, at issuance time)
  decides which scopes a session may hand out; this module only *validates* the
  set it is given and stores it verbatim.
* **Identity is never taken from the request.** :meth:`introspect` takes a
  bearer string and answers with the principal the *server* recorded. There is
  no field a client can set to claim a different owner, a different UID or a
  different scope set.
* **Every failure is a refusal.** Unknown, malformed, expired, revoked and
  suspended-principal tokens all resolve to an inactive introspection result.
  There is no "looks close enough" branch and no anonymous fallback.
* **A token never outlives its owner's access.** Issuance records the
  principal's state for audit, but the *authority* on every call is a trusted
  :class:`PrincipalStateDirectory` consulted live. An account disabled after
  its token was minted makes that token inactive on the next introspection,
  with no revocation step anyone can forget to wire. The directory is a
  **required** collaborator with no permissive default: a service that cannot
  answer "is this principal still allowed?" is not constructible.

What the consuming services still must do
-----------------------------------------
This module answers *"is this bearer credential still valid, and for which
principal?"*. It does **not** answer *"is this principal still entitled to the
specific operation being attempted?"*, and it cannot: the scope vocabulary and
the resource ownership rules belong to the Registry/Gateway.

So the contract for those lanes is two checks, not one:

1. resolve the credential here — ``introspect()``, and treat any inactive
   result as a single opaque refusal (never branch the response on the reason);
2. authorise the operation there — against **their own current policy**, not
   against the scope set frozen into the token at issuance.

(2) matters because a scope reduction on an existing principal is invisible to
this store: the token still carries the scopes it was minted with until it
expires or is revoked. Only the consuming service knows the current rule, so
only the consuming service can enforce it. A Gateway that trusts the token's
scope list alone is a Gateway that cannot revoke a capability.

Why introspection is in-process rather than a static directory
--------------------------------------------------------------
The Registry/Gateway side resolves a bearer token to
``{principal, uid, linux_user, state, scopes, roles, label}``. It could do that
from a static ``TokenDirectory`` snapshot — but this module cannot build one,
because it never retains the plaintext the directory would need to key on. That
is the point: revocation takes effect on the **next request**, not at the next
directory rebuild. So the contract offered to the other lanes is a live call::

    view = tokens.introspect(raw_bearer_token)     # in-process, trusted caller
    if view.active:
        payload = view.to_registry_dict()          # exactly the directory shape

and the ``Bearer`` header is unwrapped with
``backend.registry.identity.extract_bearer_token`` on that side, so both ends
agree on what "no credential" means.

Storage
-------
The token database is a separate SQLite file from the session store. Tokens and
sessions have different lifetimes, different blast radii and different purge
schedules; keeping them apart means a token can be revoked, rotated or dumped
for audit without touching live sessions. Pure standard library, so the suite
runs offline (``python -m unittest discover -s tests/auth -t .``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from . import errors

# --- constants ---------------------------------------------------------------

#: Prefix on every plaintext token. Not a security boundary (the entropy is),
#: but it makes a token recognisable in a log or a leaked string, and it lets a
#: secret scanner flag one. A token presented without the prefix is refused
#: before any lookup happens.
TOKEN_PREFIX = "llmlab_"

#: 32 bytes = 256 bits, matching the session-identifier floor.
TOKEN_BYTES = 32

#: Issue-time default and hard ceiling on a token's lifetime.
DEFAULT_TTL_SECONDS = 30 * 24 * 3600
MAX_TTL_SECONDS = 365 * 24 * 3600
MIN_TTL_SECONDS = 60

#: How many live tokens one principal may hold at once, and how many may be
#: minted inside one window. Both are refusals, never evictions: silently
#: dropping someone else's token to make room would be a denial of service with
#: a friendly face.
MAX_ACTIVE_PER_PRINCIPAL = 20
ISSUANCE_WINDOW_SECONDS = 3600
MAX_ISSUANCE_PER_WINDOW = 30

#: Label hygiene. A label is operator-facing text that ends up in list views
#: and audit rows, so it is bounded and stripped of control characters.
MAX_LABEL_LENGTH = 64
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: A scope is ``namespace:segment[:segment...]`` in lower case. The shape is
#: checked here; the *vocabulary* is owned by the consuming subsystem (for the
#: registry, ``backend.registry.identity.KNOWN_SCOPES``). This module therefore
#: rejects a malformed scope — a typo that would otherwise be stored and then
#: silently never match anything — without inventing a second, drifting list of
#: what the platform supports.
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}(?::[a-z0-9_]{1,32}){1,4}$")

# --- principal state ---------------------------------------------------------

STATE_APPROVED = "approved"
STATE_SUSPENDED = "suspended"
PRINCIPAL_STATES = (STATE_APPROVED, STATE_SUSPENDED)

# --- introspection verdicts --------------------------------------------------

REASON_ACTIVE = "active"
REASON_UNKNOWN = "unknown"
REASON_REVOKED = "revoked"
REASON_EXPIRED = "expired"
REASON_SUSPENDED = "suspended"
#: The token is well-formed and stored, but the trusted directory no longer
#: knows the principal at all. Distinct from ``REASON_SUSPENDED`` only so
#: operators can tell "deleted account" from "disabled account"; both refuse.
REASON_UNKNOWN_PRINCIPAL = "unknown_principal"


# --- scope helpers -----------------------------------------------------------


def normalize_scopes(scopes: Iterable[str]) -> frozenset[str]:
    """Validate a scope set, refusing anything that is not a well-formed scope.

    Empty sets are refused: a token with no scope can do nothing, and issuing
    one is far more likely to be a bug in the caller than an intentional
    least-privilege choice.
    """
    if isinstance(scopes, str):  # a bare string is a very common bug
        raise errors.InvalidRequest(detail="scopes must be a collection, not a string")
    try:
        items = list(scopes)
    except TypeError:
        raise errors.InvalidRequest(detail="scopes must be a collection of strings") from None
    if not items:
        raise errors.InvalidRequest(detail="a token must carry at least one scope")
    seen: set[str] = set()
    for scope in items:
        if not isinstance(scope, str):
            raise errors.InvalidRequest(detail="every scope must be a string")
        candidate = scope.strip()
        if not _SCOPE_RE.match(candidate):
            raise errors.InvalidRequest(detail=f"malformed scope: {candidate!r}")
        seen.add(candidate)
    return frozenset(seen)


def normalize_roles(roles: Iterable[str]) -> frozenset[str]:
    """Roles are display/audit metadata. They are kept verbatim, but bounded.

    Nothing in this module grants a capability from a role, so the only checks
    that matter are "is a string" and "is not absurd", which keeps a role from
    being used as a smuggling channel into a log line.
    """
    if isinstance(roles, str):
        raise errors.InvalidRequest(detail="roles must be a collection, not a string")
    if roles is None:
        return frozenset()
    out: set[str] = set()
    for role in roles:
        if not isinstance(role, str):
            raise errors.InvalidRequest(detail="every role must be a string")
        candidate = role.strip()
        if not candidate or len(candidate) > 64 or _CONTROL_CHARS.search(candidate):
            raise errors.InvalidRequest(detail="malformed role")
        out.add(candidate)
    return frozenset(out)


def normalize_label(label: object) -> str | None:
    """Bound and clean an operator-facing label, or ``None`` for "no label"."""
    if label is None:
        return None
    if not isinstance(label, str):
        raise errors.InvalidRequest(detail="label must be a string")
    cleaned = _CONTROL_CHARS.sub("", label).strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_LABEL_LENGTH:
        raise errors.InvalidRequest(detail=f"label must be at most {MAX_LABEL_LENGTH} characters")
    return cleaned


def resolve_ttl(ttl_seconds: object, *, default: int = DEFAULT_TTL_SECONDS) -> int:
    """Clamp a requested lifetime into ``[MIN_TTL_SECONDS, MAX_TTL_SECONDS]``.

    Out-of-range values are refused rather than clamped: a caller that asked for
    a one-second token has a bug, and handing it a token that lives for a day
    instead is the kind of silent widening that ends up in a postmortem.
    """
    if ttl_seconds is None:
        return default
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise errors.InvalidRequest(detail="ttl_seconds must be an integer")
    if ttl_seconds < MIN_TTL_SECONDS:
        raise errors.InvalidRequest(detail=f"ttl_seconds must be at least {MIN_TTL_SECONDS}")
    if ttl_seconds > MAX_TTL_SECONDS:
        raise errors.InvalidRequest(detail=f"ttl_seconds must be at most {MAX_TTL_SECONDS}")
    return int(ttl_seconds)


def digest_token(raw: str) -> str:
    """The digest under which a plaintext token is stored. Never the token."""
    return sha256(raw.encode("utf-8")).hexdigest()


def new_token_id() -> str:
    """A non-secret public identifier used for management (list/revoke/rotate)."""
    return secrets.token_urlsafe(12)


def _new_token_plaintext() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


# --- live principal state ----------------------------------------------------
#
# A token row records the principal's state *at issuance*. That snapshot is not
# authority: an account can be disabled after the token was minted, and a token
# that outlives its owner's access is precisely the credential the platform must
# not keep honouring. So the service consults a trusted directory on **every**
# issuance, **every** rotation and **every** introspection, and treats the
# directory — never the row — as the answer.
#
# The directory is a required collaborator, not an optional one. There is no
# default that says "assume approved": a service that cannot answer "is this
# principal still allowed?" must not be constructible, because the failure mode
# of a forgotten collaborator would be silent, permanent privilege.


class PrincipalStateDirectory(Protocol):
    """The trusted answer to "is this principal allowed right now?".

    Returns :data:`STATE_APPROVED`, :data:`STATE_SUSPENDED`, or ``None`` when
    the principal is not in the directory at all. ``None`` is **not** a neutral
    value: an unknown principal is refused everywhere. There is deliberately no
    "assume approved" and no "assume anonymous" branch for a caller to reach for.
    """

    def current_state(self, principal: str) -> str | None: ...


class MappingPrincipalState:
    """A directory backed by a caller-owned mapping.

    Small on purpose, and honest about what it is: it holds whatever its owner
    puts in it and nothing else. It exists so a test — or an embedding that
    genuinely has no other source of truth — states its principal set
    explicitly, instead of a permissive default standing in for a directory
    nobody wired.
    """

    def __init__(self, states: Mapping[str, str] | None = None) -> None:
        self._states: dict[str, str] = dict(states or {})

    def current_state(self, principal: str) -> str | None:
        return self._states.get(principal)

    # --- management, for the owner of the directory --------------------------

    def approve(self, principal: str) -> None:
        self._states[principal] = STATE_APPROVED

    def suspend(self, principal: str) -> None:
        self._states[principal] = STATE_SUSPENDED

    def forget(self, principal: str) -> None:
        """Remove a principal entirely; it becomes unknown, and unknown refuses."""
        self._states.pop(principal, None)

    def __len__(self) -> int:
        return len(self._states)


class AuthStorePrincipalState:
    """Live state from the auth store's ``users`` table.

    Duck-typed on ``get_user(user_id) -> User | None`` and ``User.is_active``,
    so this module does not import the store. That matters because the Linux
    principal id this service keys on (``linux:<host>:<uid>``) is *exactly* the
    id ``Database.ensure_linux_user`` writes, which is what makes the check a
    real one: ``set_user_status(..., "disabled")`` flips ``is_active``, and the
    next introspection of that principal's tokens answers inactive.

    A user row that is missing, or that lacks the attribute, is refused rather
    than assumed active.
    """

    def __init__(self, users: object) -> None:
        self._users = users

    def current_state(self, principal: str) -> str | None:
        get_user = getattr(self._users, "get_user", None)
        if not callable(get_user):  # pragma: no cover - miswiring, refused loudly
            raise errors.ConfigError(detail="the principal directory has no get_user()")
        user = get_user(principal)
        if user is None:
            return None
        return STATE_APPROVED if getattr(user, "is_active", False) else STATE_SUSPENDED


# --- value objects -----------------------------------------------------------


@dataclass(frozen=True)
class IssuedToken:
    """The single moment of plaintext.

    ``token`` is the only copy that will ever exist in the clear. It is not
    written to the store, and the ``repr`` below omits it so that an accidental
    ``print(issued)`` or a traceback frame cannot leak a live credential.
    """

    token: str
    token_id: str
    principal: str
    scopes: frozenset[str]
    roles: frozenset[str]
    label: str | None
    issued_at: float
    expires_at: float

    def to_public_dict(self) -> dict[str, object]:
        """The one-and-only-one response an issuance endpoint may return."""
        return {
            "token": self.token,
            "token_id": self.token_id,
            "principal": self.principal,
            "scopes": sorted(self.scopes),
            "roles": sorted(self.roles),
            "label": self.label,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive, not on a hot path
        return (
            f"IssuedToken(token_id={self.token_id!r}, principal={self.principal!r}, "
            f"scopes={sorted(self.scopes)}, token=<redacted>)"
        )


@dataclass(frozen=True)
class TokenRecord:
    """A stored token: everything about it **except** the plaintext."""

    token_id: str
    principal: str
    uid: int | None
    linux_user: str | None
    display_name: str | None
    state: str
    scopes: frozenset[str]
    roles: frozenset[str]
    label: str | None
    created_at: float
    expires_at: float
    revoked_at: float | None
    rotated_at: float | None
    rotation_of: str | None
    last_used_at: float | None

    def is_live(self, now: float) -> bool:
        return self.revoked_at is None and now < self.expires_at

    def to_public_dict(self) -> dict[str, object]:
        """What a management endpoint may show. There is no token to redact
        because this object never held one."""
        return {
            "token_id": self.token_id,
            "principal": self.principal,
            "scopes": sorted(self.scopes),
            "roles": sorted(self.roles),
            "label": self.label,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "rotated_at": self.rotated_at,
            "rotation_of": self.rotation_of,
            "last_used_at": self.last_used_at,
            "revoked": self.revoked_at is not None,
        }


@dataclass(frozen=True)
class TokenIntrospection:
    """The trusted answer to "who is this bearer token?".

    ``active`` is the only thing an authorisation decision may branch on. When
    it is ``False`` the remaining fields are empty and ``reason`` is a stable
    machine token for server-side logging — it is deliberately *not* specific
    enough to be a useful oracle for an unauthenticated caller, and callers that
    face the public internet must collapse every inactive reason into one 401.
    """

    active: bool
    reason: str
    principal: str | None = None
    uid: int | None = None
    linux_user: str | None = None
    display_name: str | None = None
    state: str | None = None
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    label: str | None = None
    token_id: str | None = None
    expires_at: float | None = None

    def to_registry_dict(self) -> dict[str, object]:
        """The exact shape ``backend.registry.identity`` resolves against.

        Kept in one place, spelled out, so a rename on either side is a visible
        diff rather than a silent ``KeyError`` at runtime.
        """
        return {
            "principal": self.principal,
            "uid": self.uid,
            "linux_user": self.linux_user,
            "display_name": self.display_name,
            "state": self.state,
            "approved": self.state == STATE_APPROVED,
            "scopes": sorted(self.scopes),
            "roles": sorted(self.roles),
            "label": self.label,
            "token_id": self.token_id,
            "expires_at": self.expires_at,
        }

    @classmethod
    def inactive(cls, reason: str) -> "TokenIntrospection":
        return cls(active=False, reason=reason)


# --- storage -----------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoped_tokens (
    token_hash   TEXT PRIMARY KEY,
    token_id     TEXT NOT NULL UNIQUE,
    principal    TEXT NOT NULL,
    uid          INTEGER,
    linux_user   TEXT,
    display_name TEXT,
    state        TEXT NOT NULL,
    scopes       TEXT NOT NULL,
    roles        TEXT NOT NULL,
    label        TEXT,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    revoked_at   REAL,
    rotated_at   REAL,
    rotation_of  TEXT,
    last_used_at REAL
);

CREATE INDEX IF NOT EXISTS scoped_tokens_by_principal ON scoped_tokens(principal);
"""


class TokenStore:
    """A small SQLite store for scoped tokens, opened per operation.

    Separate from :class:`backend.auth.store.Database` on purpose (see the
    module docstring). The connection discipline — WAL, a busy timeout, a
    connection per call — is copied deliberately so the two behave the same
    under a multi-worker WSGI deployment.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _harden(self.path.parent, 0o700)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()
        _harden(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # --- writes --------------------------------------------------------------

    def insert(
        self,
        *,
        token_hash: str,
        token_id: str,
        principal: str,
        uid: int | None,
        linux_user: str | None,
        display_name: str | None,
        state: str,
        scopes: frozenset[str],
        roles: frozenset[str],
        label: str | None,
        created_at: float,
        expires_at: float,
        rotation_of: str | None = None,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO scoped_tokens (token_hash, token_id, principal, uid, linux_user,
                                           display_name, state, scopes, roles, label,
                                           created_at, expires_at, revoked_at, rotated_at,
                                           rotation_of, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
                """,
                (
                    token_hash, token_id, principal, uid, linux_user, display_name, state,
                    json.dumps(sorted(scopes)), json.dumps(sorted(roles)), label,
                    created_at, expires_at, rotation_of,
                ),
            )
        finally:
            conn.close()

    def mark_revoked(self, token_id: str, *, now: float, rotated: bool = False) -> bool:
        """Stamp a token revoked, and optionally record that a rotation did it."""
        conn = self._connect()
        try:
            if rotated:
                cursor = conn.execute(
                    "UPDATE scoped_tokens SET revoked_at = COALESCE(revoked_at, ?), "
                    "rotated_at = COALESCE(rotated_at, ?) WHERE token_id = ?",
                    (now, now, token_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE scoped_tokens SET revoked_at = COALESCE(revoked_at, ?) "
                    "WHERE token_id = ?",
                    (now, token_id),
                )
            return (cursor.rowcount or 0) > 0
        finally:
            conn.close()

    def revoke_all_for_principal(self, principal: str, *, now: float) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE scoped_tokens SET revoked_at = ? "
                "WHERE principal = ? AND revoked_at IS NULL",
                (now, principal),
            )
            return cursor.rowcount or 0
        finally:
            conn.close()

    def purge(self, *, before: float, revoked_before: float) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM scoped_tokens WHERE expires_at <= ? "
                "OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
                (before, revoked_before),
            )
            return cursor.rowcount or 0
        finally:
            conn.close()

    # --- reads ---------------------------------------------------------------

    def get_by_hash(self, token_hash: str) -> TokenRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM scoped_tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_record(row) if row else None

    def get_by_id(self, token_id: str) -> TokenRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM scoped_tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_record(row) if row else None

    def list_for_principal(self, principal: str) -> list[TokenRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM scoped_tokens WHERE principal = ? ORDER BY created_at DESC, token_id",
                (principal,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_record(row) for row in rows]

    def count_live(self, principal: str, *, now: float) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM scoped_tokens "
                "WHERE principal = ? AND revoked_at IS NULL AND expires_at > ?",
                (principal, now),
            ).fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def count_issued_since(self, principal: str, *, since: float) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM scoped_tokens WHERE principal = ? AND created_at >= ?",
                (principal, since),
            ).fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def touch_used(self, token_id: str, *, now: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE scoped_tokens SET last_used_at = ? WHERE token_id = ?", (now, token_id)
            )
        finally:
            conn.close()


def _row_to_record(row: sqlite3.Row) -> TokenRecord:
    return TokenRecord(
        token_id=str(row["token_id"]),
        principal=str(row["principal"]),
        uid=row["uid"],
        linux_user=row["linux_user"],
        display_name=row["display_name"],
        state=str(row["state"]),
        scopes=frozenset(json.loads(row["scopes"])),
        roles=frozenset(json.loads(row["roles"])),
        label=row["label"],
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        revoked_at=row["revoked_at"],
        rotated_at=row["rotated_at"],
        rotation_of=row["rotation_of"],
        last_used_at=row["last_used_at"],
    )


def _harden(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows / odd FS
        pass


def _rate_limited(detail: str, *, retry_after: int | None = None) -> errors.RateLimited:
    """Build a :class:`errors.RateLimited` carrying server-side detail.

    ``RateLimited`` defines its own ``__init__`` (it needs ``retry_after``), so
    it does not take the base class's ``detail`` keyword. The detail is
    diagnostic only — it is never serialised — so attaching it after
    construction is equivalent and keeps the message the client sees generic.
    """
    exc = errors.RateLimited(retry_after=retry_after)
    exc.detail = detail
    return exc


# --- the service -------------------------------------------------------------


class ScopedTokenService:
    """Issue, introspect, revoke, rotate and expire scoped bearer tokens."""

    def __init__(
        self,
        store: TokenStore,
        *,
        principal_state: PrincipalStateDirectory,
        clock: Callable[[], float] = time.time,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_ttl_seconds: int = MAX_TTL_SECONDS,
        max_active_per_principal: int = MAX_ACTIVE_PER_PRINCIPAL,
        max_issuance_per_window: int = MAX_ISSUANCE_PER_WINDOW,
        issuance_window_seconds: int = ISSUANCE_WINDOW_SECONDS,
    ) -> None:
        # ``principal_state`` is keyword-only and has no default: see the
        # "live principal state" section above. A token service without a
        # trusted answer to "is this principal still allowed?" is not a
        # constructible object.
        if principal_state is None or not callable(
            getattr(principal_state, "current_state", None)
        ):
            raise errors.ConfigError(
                detail="a trusted principal state directory is required"
            )
        self._store = store
        self._principal_state = principal_state
        self._clock = clock
        self._default_ttl = int(default_ttl_seconds)
        self._max_ttl = int(max_ttl_seconds)
        self._max_active = int(max_active_per_principal)
        self._max_issuance = int(max_issuance_per_window)
        self._issuance_window = int(issuance_window_seconds)

    # --- live state ----------------------------------------------------------

    def current_state(self, principal: str) -> str | None:
        """What the trusted directory says about ``principal`` right now.

        Public so the wiring layer and the consuming services can ask the same
        question against the same source of truth instead of each keeping a
        copy that can drift.
        """
        return self._principal_state.current_state(principal)

    def _require_approved(self, principal: str, *, context: str) -> None:
        """Refuse unless the directory says ``approved`` — no fallback path."""
        state = self._principal_state.current_state(principal)
        if state == STATE_APPROVED:
            return
        if state is None:
            raise errors.Forbidden(
                detail=f"{context}: principal {principal!r} is not in the trusted directory"
            )
        raise errors.Forbidden(detail=f"{context}: principal state is {state!r}")

    # --- lifecycle -----------------------------------------------------------

    def initialize(self) -> None:
        self._store.initialize()

    def issue(
        self,
        *,
        principal: str,
        uid: int | None,
        linux_user: str | None,
        scopes: Iterable[str],
        roles: Iterable[str] = (),
        label: object = None,
        display_name: str | None = None,
        ttl_seconds: int | None = None,
        rotation_of: str | None = None,
    ) -> IssuedToken:
        """Mint a token for a principal the **caller** has already authenticated.

        The principal, the UID and the Linux name are arguments, never request
        fields: this method has no way to read them from anywhere else, which is
        what makes "the client said it was someone else" unrepresentable.

        Approval is **not** an argument. It is read from the trusted directory
        at this moment, so a caller cannot mint for a disabled account by
        passing ``approved`` (there is no such parameter) and cannot mint
        without the directory agreeing. The state is recorded on the row for
        audit, but the row is never the authority again — see
        :meth:`introspect`.
        """
        principal = self._valid_principal(principal)
        self._require_approved(principal, context="issue")
        state = STATE_APPROVED

        resolved_scopes = normalize_scopes(scopes)
        resolved_roles = normalize_roles(roles)
        resolved_label = normalize_label(label)
        ttl = resolve_ttl(ttl_seconds, default=self._default_ttl)
        if ttl > self._max_ttl:
            raise errors.InvalidRequest(detail=f"ttl_seconds must be at most {self._max_ttl}")

        now = self._clock()
        self._enforce_issuance_limits(principal, now)

        plaintext = _new_token_plaintext()
        token_id = new_token_id()
        expires_at = now + ttl
        self._store.insert(
            token_hash=digest_token(plaintext),
            token_id=token_id,
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            display_name=display_name,
            state=state,
            scopes=resolved_scopes,
            roles=resolved_roles,
            label=resolved_label,
            created_at=now,
            expires_at=expires_at,
            rotation_of=rotation_of,
        )
        return IssuedToken(
            token=plaintext,
            token_id=token_id,
            principal=principal,
            scopes=resolved_scopes,
            roles=resolved_roles,
            label=resolved_label,
            issued_at=now,
            expires_at=expires_at,
        )

    def _enforce_issuance_limits(self, principal: str, now: float) -> None:
        live = self._store.count_live(principal, now=now)
        if live >= self._max_active:
            raise _rate_limited(
                f"principal already holds {live} live tokens "
                f"(limit {self._max_active}); revoke one before issuing another"
            )
        recent = self._store.count_issued_since(principal, since=now - self._issuance_window)
        if recent >= self._max_issuance:
            raise _rate_limited(
                f"issuance limit reached ({self._max_issuance} per "
                f"{self._issuance_window}s)",
                retry_after=self._issuance_window,
            )

    @staticmethod
    def _valid_principal(principal: object) -> str:
        if not isinstance(principal, str) or not principal.strip():
            raise errors.InvalidRequest(detail="principal must be a non-empty string")
        if _CONTROL_CHARS.search(principal):
            raise errors.InvalidRequest(detail="principal must not contain control characters")
        return principal.strip()

    # --- introspection -------------------------------------------------------

    def introspect(self, raw_token: object) -> TokenIntrospection:
        """Resolve a bearer token. Never raises; every negative path is inactive.

        This is the server-side trust boundary. The answer depends on the stored
        record **and on the trusted directory's view of the principal right
        now** — never on anything the caller asserts alongside the token.

        The live check is the point. A token row records the principal's state
        as of issuance, and a token that keeps working after its owner is
        disabled is exactly the credential that must not survive. So the row's
        ``state`` is treated as history: a principal who has since been
        suspended, or removed from the directory altogether, makes the token
        inactive on the **next** call, with no revocation step required and
        nothing to forget to wire.

        ``approved`` on the result is the *live* verdict, which is what the
        Registry/Gateway consume.
        """
        if not isinstance(raw_token, str) or not raw_token:
            return TokenIntrospection.inactive(REASON_UNKNOWN)
        if not raw_token.startswith(TOKEN_PREFIX):
            # A string that is not a token at all is refused before we touch the
            # database, so junk cannot be used to probe lookup behaviour.
            return TokenIntrospection.inactive(REASON_UNKNOWN)

        record = self._store.get_by_hash(digest_token(raw_token))
        if record is None:
            return TokenIntrospection.inactive(REASON_UNKNOWN)

        now = self._clock()
        if record.revoked_at is not None:
            return TokenIntrospection.inactive(REASON_REVOKED)
        if now >= record.expires_at:
            return TokenIntrospection.inactive(REASON_EXPIRED)

        live_state = self._principal_state.current_state(record.principal)
        if live_state is None:
            return TokenIntrospection.inactive(REASON_UNKNOWN_PRINCIPAL)
        if live_state != STATE_APPROVED:
            return TokenIntrospection.inactive(REASON_SUSPENDED)

        # Best-effort usage stamp. A failure here must not turn a valid token
        # into a rejection, so it is deliberately not allowed to propagate.
        try:
            self._store.touch_used(record.token_id, now=now)
        except sqlite3.Error:  # pragma: no cover - defensive
            pass

        return TokenIntrospection(
            active=True,
            reason=REASON_ACTIVE,
            principal=record.principal,
            uid=record.uid,
            linux_user=record.linux_user,
            display_name=record.display_name,
            # The live verdict, not the row's snapshot: this is what
            # ``to_registry_dict()["approved"]`` exposes downstream.
            state=live_state,
            scopes=record.scopes,
            roles=record.roles,
            label=record.label,
            token_id=record.token_id,
            expires_at=record.expires_at,
        )

    # --- management ----------------------------------------------------------

    def list_for_principal(self, principal: str) -> list[TokenRecord]:
        """Every token a principal holds, live or not. Never a plaintext."""
        return self._store.list_for_principal(self._valid_principal(principal))

    def revoke(self, raw_token: object) -> bool:
        """Revoke by presenting the token itself. Unknown tokens are not an error."""
        if not isinstance(raw_token, str) or not raw_token.startswith(TOKEN_PREFIX):
            return False
        record = self._store.get_by_hash(digest_token(raw_token))
        if record is None:
            return False
        return self._store.mark_revoked(record.token_id, now=self._clock())

    def revoke_for_principal(self, principal: str, token_id: str) -> bool:
        """Revoke a token **only** if the requesting principal owns it.

        A mismatched owner raises :class:`Forbidden` rather than returning
        ``False``: a cross-principal revocation attempt is an authorisation
        failure worth a distinguishable refusal, not a "not found".
        """
        owner = self._valid_principal(principal)
        record = self._store.get_by_id(token_id)
        if record is None:
            return False
        if record.principal != owner:
            raise errors.Forbidden(detail="that token belongs to another principal")
        return self._store.mark_revoked(record.token_id, now=self._clock())

    def revoke_all_for_principal(self, principal: str) -> int:
        return self._store.revoke_all_for_principal(
            self._valid_principal(principal), now=self._clock()
        )

    def rotate_for_principal(
        self,
        principal: str,
        token_id: str,
        *,
        allowed_scopes: Iterable[str],
        scopes: Iterable[str] | None = None,
        roles: Iterable[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> IssuedToken:
        """Replace a **live** token with a fresh one, without re-widening anything.

        Rotation is not a way to mint authority, so it re-checks everything the
        original issuance checked, and one thing more:

        1. the old token must belong to ``principal`` (else ``Forbidden``);
        2. the old token must be **live** — not revoked, not expired. A dead
           credential is not a ticket to a new one; it is a dead credential
           (``NotAuthenticated``). Without this check, "revoke this token" would
           be undone by the holder of its id, which is the whole point of
           revocation;
        3. the principal must be **approved by the trusted directory right now**
           (``Forbidden`` otherwise). The old row's ``state`` is history and is
           never consulted as authority;
        4. the replacement's scopes must be a subset of both the **current
           session policy** (``allowed_scopes``, supplied by the caller from the
           live session) and the **old token's scopes**. Anything else is
           refused by name rather than silently narrowed or silently carried
           over — a downgraded administrator must not be able to re-issue their
           old privilege by rotating a token they still hold.

        ``allowed_scopes`` is keyword-only and required. Omitting it is not a
        default that happens to be permissive; it is a ``TypeError``, because
        the only way to rotate safely is to say what is currently permitted.

        The new token is minted **first**; only once it exists is the old one
        revoked. The reverse order has a window in which the principal holds no
        usable credential, and a crash inside that window would leave them
        locked out. The cost of this order is that a crash between the two steps
        leaves two live tokens — visible in ``list_for_principal`` and revocable
        — which is the failure direction we prefer.
        """
        owner = self._valid_principal(principal)
        record = self._store.get_by_id(token_id)
        if record is None:
            raise errors.InvalidRequest(detail="unknown token")
        if record.principal != owner:
            raise errors.Forbidden(detail="that token belongs to another principal")

        now = self._clock()
        if record.revoked_at is not None:
            raise errors.NotAuthenticated(
                detail="that token has been revoked and cannot be rotated"
            )
        if now >= record.expires_at:
            raise errors.NotAuthenticated(
                detail="that token has expired and cannot be rotated"
            )

        # Live approval, from the directory — not ``record.state``.
        self._require_approved(owner, context="rotate")

        permitted = normalize_scopes(allowed_scopes)
        if scopes is None:
            replacement_scopes = record.scopes
            surplus = replacement_scopes - permitted
            if surplus:
                raise errors.Forbidden(
                    detail="the current session may no longer hold "
                    f"{sorted(surplus)}; revoke the old token and issue a new one"
                )
        else:
            replacement_scopes = normalize_scopes(scopes)
            surplus = replacement_scopes - permitted
            if surplus:
                raise errors.Forbidden(
                    detail=f"scopes not permitted by the current session: {sorted(surplus)}"
                )
            widened = replacement_scopes - record.scopes
            if widened:
                # Rotation narrows or preserves; it never adds. Adding is what
                # ``issue`` is for, where the policy is applied from scratch.
                raise errors.Forbidden(
                    detail=f"rotation cannot add scopes: {sorted(widened)}"
                )

        issued = self.issue(
            principal=owner,
            uid=record.uid,
            linux_user=record.linux_user,
            display_name=record.display_name,
            scopes=replacement_scopes,
            roles=record.roles if roles is None else roles,
            label=record.label,
            ttl_seconds=ttl_seconds,
            rotation_of=record.token_id,
        )
        self._store.mark_revoked(record.token_id, now=now, rotated=True)
        return issued

    def purge_expired(self, *, grace_seconds: int = 86400) -> int:
        now = self._clock()
        return self._store.purge(before=now, revoked_before=now - grace_seconds)


def _parse_linux_uid(principal: str) -> int | None:
    """Recover the UID from a ``linux:<host>:<uid>`` user id, or ``None``.

    ``backend.auth_linux.handoff.linux_user_id`` builds exactly that string. A
    principal that is not a Linux one (a Feishu identity, say) has no UID, and
    ``None`` is returned so the Registry side refuses to treat it as a host
    account rather than inventing a number.
    """
    if not principal.startswith("linux:"):
        return None
    remainder = principal[len("linux:") :]
    host, separator, uid_text = remainder.rpartition(":")
    if not separator or not host or not uid_text.isdigit():
        return None
    return int(uid_text)


def principal_ref(user: object) -> tuple[str, int | None, str | None]:
    """``(principal, uid, linux_user)`` for a signed-in auth-store user.

    A convenience for the wiring layer, so the coupling to the shape of the
    Linux user id lives next to the token code that consumes it rather than
    being re-derived at every call site.

    ``linux_user`` is the account name, and only a Linux principal has one: for
    a Feishu or local ``admin`` identity both the UID and the Linux name are
    ``None``. That is what lets the Registry tell "a person on a host" apart
    from "some other kind of principal" instead of fabricating an account for
    them.
    """
    principal_id = str(getattr(user, "id"))
    uid = _parse_linux_uid(principal_id)
    linux_user = str(getattr(user, "display_name", "")) or None
    if uid is None:
        linux_user = None
    return principal_id, uid, linux_user


def build_token_store(state_dir: str | Path, *, clock: Callable[[], float] = time.time) -> TokenStore:
    """The store for a deployment's state directory."""
    return TokenStore(Path(state_dir) / "scoped-tokens.sqlite3", clock=clock)


def build_token_service(
    state_dir: str | Path,
    *,
    principal_state: PrincipalStateDirectory,
    clock: Callable[[], float] = time.time,
    environ: Mapping[str, str] | None = None,
) -> ScopedTokenService:
    """Assemble the service from a state directory, a directory and optional env.

    ``principal_state`` is required for the same reason it is required on
    :class:`ScopedTokenService`: there is no permissive stand-in. The usual
    choice is :class:`AuthStorePrincipalState` over the auth service's
    ``Database``, which makes "was this account disabled?" a live question
    against the same row the rest of the platform reads.

    Recognised environment overrides (all optional, all bounded by the module
    constants): ``LLM_LAB_TOKEN_TTL_SECONDS``, ``LLM_LAB_TOKEN_MAX_ACTIVE``,
    ``LLM_LAB_TOKEN_MAX_PER_WINDOW``.
    """
    source = os.environ if environ is None else environ

    def _int(name: str, default: int) -> int:
        raw = source.get(name)
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            raise errors.ConfigError(detail=f"{name} must be an integer") from None

    return ScopedTokenService(
        build_token_store(state_dir, clock=clock),
        principal_state=principal_state,
        clock=clock,
        default_ttl_seconds=_int("LLM_LAB_TOKEN_TTL_SECONDS", DEFAULT_TTL_SECONDS),
        max_active_per_principal=_int("LLM_LAB_TOKEN_MAX_ACTIVE", MAX_ACTIVE_PER_PRINCIPAL),
        max_issuance_per_window=_int(
            "LLM_LAB_TOKEN_MAX_PER_WINDOW", MAX_ISSUANCE_PER_WINDOW
        ),
    )


__all__ = [
    "AuthStorePrincipalState",
    "DEFAULT_TTL_SECONDS",
    "ISSUANCE_WINDOW_SECONDS",
    "IssuedToken",
    "MAX_ACTIVE_PER_PRINCIPAL",
    "MAX_ISSUANCE_PER_WINDOW",
    "MAX_LABEL_LENGTH",
    "MAX_TTL_SECONDS",
    "MIN_TTL_SECONDS",
    "MappingPrincipalState",
    "PRINCIPAL_STATES",
    "PrincipalStateDirectory",
    "REASON_ACTIVE",
    "REASON_EXPIRED",
    "REASON_REVOKED",
    "REASON_SUSPENDED",
    "REASON_UNKNOWN",
    "REASON_UNKNOWN_PRINCIPAL",
    "STATE_APPROVED",
    "STATE_SUSPENDED",
    "ScopedTokenService",
    "TOKEN_BYTES",
    "TOKEN_PREFIX",
    "TokenIntrospection",
    "TokenRecord",
    "TokenStore",
    "build_token_service",
    "build_token_store",
    "digest_token",
    "new_token_id",
    "normalize_label",
    "normalize_roles",
    "normalize_scopes",
    "principal_ref",
    "resolve_ttl",
]
