"""The seam between "who is this" and "keep them signed in".

This module exists to make one thing hard: starting a session.

The portal already has a session implementation — an opaque 256-bit cookie
identifier, only its digest at rest, idle and absolute expiry, rotation on
sign-in, a CSRF token bound to the session. That implementation is the only
thing in the system that should ever mint a session, and a second sign-in path
is exactly the sort of change that ends up with a parallel, weaker one.

So the Linux path does not get one. It produces a :class:`VerifiedIdentity`, the
auth service turns that into a :class:`UserDraft`, and the auth service's own
session manager is what issues the cookie. Nothing in this package creates a
session, sets a cookie, or issues or validates a CSRF token.

Why a protocol rather than an import
------------------------------------
``backend.auth`` does not exist on the branch this module was built against, and
even when it does, this package should not depend on its internals. The handoff
is therefore expressed as a protocol the auth service implements: the coupling
is a documented shape, reviewed on both sides, rather than a transitive import
that would let either side change the other by accident.

The rules the implementer must hold
-----------------------------------
1. **Role and status are written on insert, never on conflict.** A returning
   sign-in must not be able to promote itself or un-disable itself. This is the
   same discipline the Feishu path already follows, and it is the reason the
   Linux path can be allowed to hand over a role at all.
2. **The role in the draft comes from policy, never from the request.** The only
   way a Linux identity carries ``platform_admin`` is a matching entry in the
   root-owned grant file, matched on host, UID and name.
3. **Start a fresh session and revoke any pre-login one** — the session-fixation
   defence the existing manager already implements.
4. **Store no credential.** The draft carries no password, no hash, and nothing
   derived from the broker's reply except the account's own identity.

The Principal link
------------------
A platform Principal may be associated with a Linux account by the pair
``(host_id, uid)``. That pair is what :func:`linux_user_id` builds the local
user id from, and it is deliberately *not* built from the login name: a rename
changes the name a person types, not the person. The login name is recorded
alongside, for operators to read, but it is not what identifies the account. A
consequence worth knowing: because the administrator grant is matched on the
name as well as the UID, renaming an administrator's account drops their
administrator role until the grant is updated. That is the intended failure
direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

AUTH_SOURCE_LINUX = "linux_pam"


def linux_user_id(host_id: str, uid: int) -> str:
    """The stable local user identifier for a Linux account on a host.

    Keyed on the host and the UID rather than the login name, so a rename maps
    to the same person and the same platform user.
    """
    if not isinstance(host_id, str) or not host_id:
        raise ValueError("host_id must be a non-empty string")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise ValueError("uid must be a non-negative integer")
    return f"linux:{host_id}:{uid}"


@dataclass(frozen=True)
class UserDraft:
    """What the auth service needs in order to provision and sign someone in."""

    user_id: str
    auth_source: str
    external_id: str
    display_name: str
    role: str
    host_id: str
    uid: int
    login_name: str

    def to_public_identity(self) -> dict[str, object]:
        """The subset a UI may see.

        Mirrors the shape the auth service already returns from ``/auth/me`` and
        ``/auth/csrf`` so the V5 front end needs no special case. It contains no
        UID, no host identifier, no login name and nothing derived from the
        broker: the platform user id and the role are enough to render, and the
        fewer places an account's Linux identity is echoed, the fewer places it
        can leak from.
        """
        return {
            "id": self.user_id,
            "display_name": self.display_name,
            "role": self.role,
            "auth_source": self.auth_source,
            "is_platform_admin": self.role == "platform_admin",
        }


class SessionHandoff(Protocol):
    """Implemented by the auth service; called by the Linux sign-in path.

    The return value is deliberately opaque. This package has no opinion about
    what a session is, and giving it one — even a peek at the issued token —
    would invite it to start caring.
    """

    def start_session(self, draft: UserDraft) -> object: ...


def draft_from_identity(identity: object) -> UserDraft:
    """Build the draft for a verified identity.

    Takes the identity structurally (``login_name``, ``uid``, ``host_id``,
    ``role``, ``display_name``) so this module does not have to import the
    provider, and a test can hand it a stand-in.
    """
    login_name = str(getattr(identity, "login_name"))
    uid = int(getattr(identity, "uid"))
    host_id = str(getattr(identity, "host_id"))
    role = str(getattr(identity, "role"))
    display_name = str(getattr(identity, "display_name"))

    return UserDraft(
        user_id=linux_user_id(host_id, uid),
        auth_source=AUTH_SOURCE_LINUX,
        external_id=f"{host_id}:{uid}",
        display_name=display_name,
        role=role,
        host_id=host_id,
        uid=uid,
        login_name=login_name,
    )


__all__ = [
    "AUTH_SOURCE_LINUX",
    "SessionHandoff",
    "UserDraft",
    "draft_from_identity",
    "linux_user_id",
]
