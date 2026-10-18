"""Who may sign in to the portal, and what they are once they have.

This is the module that answers "is this Linux account allowed to be a portal
identity at all", before any password is looked at, and "what platform role
does that identity carry", after it has been.

The rules, and why each one exists:

``root`` never signs in
    Not "is not in the allowlist by default". Never, under any configuration.
    A check that lives in a config file is a check someone can undo by editing
    the config file, and the account it would undo is the one with no
    constraints left.

Service and system accounts never sign in
    Below a human UID floor, or carrying a non-login shell, an account is
    infrastructure. Refusing it here means the broker is never even asked.

Membership is explicit
    The allowlist is a root-owned file. Absent, unreadable or malformed means
    nobody is allowed, because "the policy file did not load" must never look
    like "everyone is allowed".

Roles are granted, never inferred
    A Linux sign-in produces ``member``. ``platform_admin`` appears only from an
    explicit, root-owned grant matched on ``(host_id, uid, name)``. Nothing in
    the submitted login name, and nothing about how similar that name is to an
    administrator's, participates in the decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import errors
from .accounts import LinuxAccount, canonical_login_name
from .reasons import DenialReason
from .securefile import FileFacts, default_fs_probe, read_policy_text

ROLE_ADMIN = "platform_admin"
ROLE_MEMBER = "member"

# Accounts below this UID are the system's, not a person's. 1000 is the Debian
# and Ubuntu convention for the first human account; it is configurable because
# a host may reasonably start higher.
DEFAULT_MIN_HUMAN_UID = 1000

# Shells that mean "this account exists for file ownership or service
# execution, not for interactive sign-in". An empty shell is included: it is how
# ``/etc/passwd`` entries for daemons written by hand often end up.
NON_LOGIN_SHELLS = frozenset(
    {
        "",
        "/bin/false",
        "/usr/bin/false",
        "/sbin/false",
        "/usr/sbin/false",
        "/bin/nologin",
        "/sbin/nologin",
        "/usr/bin/nologin",
        "/usr/sbin/nologin",
        "/bin/true",
        "/usr/bin/true",
    }
)

# Names that are refused even if someone adds them to the allowlist. The UID
# floor already covers most of these on a normal host; this list is the part
# that does not depend on the host's UID layout being conventional.
ALWAYS_DENIED_NAMES = frozenset(
    {
        "root",
        "toor",
        "nobody",
        "nogroup",
        "daemon",
        "bin",
        "sys",
        "sync",
        "games",
        "man",
        "lp",
        "mail",
        "news",
        "uucp",
        "proxy",
        "www-data",
        "backup",
        "list",
        "irc",
        "gnats",
        "messagebus",
        "sshd",
        "cron",
        "ntp",
        "apt",
        "_apt",
        "polkitd",
        "systemd-network",
        "systemd-resolve",
        "systemd-timesync",
    }
)

ALLOWLIST_VERSION = 1
ADMIN_GRANT_VERSION = 1


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str | None
    role: str | None


@dataclass(frozen=True)
class Allowlist:
    """The accounts permitted to hold a portal identity on this host."""

    allowed_users: frozenset[str]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self.allowed_users

    def sorted_users(self) -> list[str]:
        """Deterministic ordering, for status output and tests."""
        return sorted(self.allowed_users)


def parse_allowlist(text: str) -> Allowlist:
    """Parse the allowlist document, rejecting anything we did not write.

    Unknown keys are an error rather than being ignored. That is what makes the
    absence of an alias mechanism enforceable: a document that tries to say
    ``{"aliases": {"dana": "bob"}}`` does not silently lose the aliases and
    carry on — it fails to load, and the operator is told their policy is
    invalid. Selective ignoring is how a policy file quietly stops meaning what
    its author thought it meant.
    """
    document = _load_json_object(text, what="allowlist")

    unknown = set(document) - {"version", "allowed_users"}
    if unknown:
        raise errors.ConfigError(detail=f"allowlist has unsupported keys: {sorted(unknown)}")

    version = document.get("version")
    if version != ALLOWLIST_VERSION:
        raise errors.ConfigError(detail="allowlist has an unsupported version")

    raw_users = document.get("allowed_users")
    if not isinstance(raw_users, list):
        raise errors.ConfigError(detail="allowlist allowed_users must be a list")

    names: set[str] = set()
    for entry in raw_users:
        name = canonical_login_name(entry)
        if name is None:
            raise errors.ConfigError(detail="allowlist contains a name that is not a login name")
        names.add(name)

    if not names:
        # An empty list is far more likely to be a half-finished edit than a
        # deliberate "nobody may sign in", and the two behave identically at
        # runtime, so the ambiguity is resolved by refusing to start.
        raise errors.ConfigError(detail="allowlist is empty")

    return Allowlist(allowed_users=frozenset(names))


def load_allowlist(
    path: str | Path,
    *,
    required_owner_uid: int | None = 0,
    required_parent_owner_uid: int | None = 0,
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> Allowlist:
    text = read_policy_text(
        path,
        required_owner_uid=required_owner_uid,
        required_parent_owner_uid=required_parent_owner_uid,
        fs_probe=fs_probe,
    )
    return parse_allowlist(text)


@dataclass(frozen=True)
class AdminGrant:
    """Explicit platform-administrator grants, bound to this host.

    A grant names a UID *and* the login name that must still belong to it. Both
    must match at sign-in time, so a grant cannot survive the UID being recycled
    to a different person, and it cannot be satisfied by a name that merely
    looks right.
    """

    host_id: str
    entries: frozenset[tuple[int, str]]

    def grants_admin(self, *, uid: int, name: str) -> bool:
        return (uid, name) in self.entries

    def sorted_entries(self) -> list[tuple[int, str]]:
        return sorted(self.entries)


def parse_admin_grant(text: str, *, host_id: str) -> AdminGrant:
    document = _load_json_object(text, what="administrator grant")

    unknown = set(document) - {"version", "host_id", "platform_admins"}
    if unknown:
        raise errors.ConfigError(detail=f"administrator grant has unsupported keys: {sorted(unknown)}")

    if document.get("version") != ADMIN_GRANT_VERSION:
        raise errors.ConfigError(detail="administrator grant has an unsupported version")

    file_host = document.get("host_id")
    if not isinstance(file_host, str) or not file_host:
        raise errors.ConfigError(detail="administrator grant must name a host")
    if file_host != host_id:
        # A grant is a statement about one machine's accounts. Copying the file
        # to a second host must not carry the promotion with it.
        raise errors.ConfigError(detail="administrator grant is for a different host")

    raw_entries = document.get("platform_admins")
    if not isinstance(raw_entries, list):
        raise errors.ConfigError(detail="administrator grant platform_admins must be a list")

    entries: set[tuple[int, str]] = set()
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise errors.ConfigError(detail="administrator grant entry must be an object")
        extra = set(entry) - {"uid", "name"}
        if extra:
            raise errors.ConfigError(detail=f"administrator grant entry has unsupported keys: {sorted(extra)}")
        uid = entry.get("uid")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            raise errors.ConfigError(detail="administrator grant entry needs a numeric uid")
        name = canonical_login_name(entry.get("name"))
        if name is None:
            raise errors.ConfigError(detail="administrator grant entry needs a login name")
        entries.add((uid, name))

    return AdminGrant(host_id=file_host, entries=frozenset(entries))


def load_admin_grant(
    path: str | Path,
    *,
    host_id: str,
    required_owner_uid: int | None = 0,
    required_parent_owner_uid: int | None = 0,
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> AdminGrant:
    text = read_policy_text(
        path,
        required_owner_uid=required_owner_uid,
        required_parent_owner_uid=required_parent_owner_uid,
        fs_probe=fs_probe,
    )
    return parse_admin_grant(text, host_id=host_id)


def no_admin_grant(host_id: str) -> AdminGrant:
    """The grant used when no administrator file exists.

    Absence of the file is "nobody is an administrator through this path", not
    a missing prerequisite that blocks start-up: a deployment may reasonably
    never want a Linux-derived administrator.
    """
    return AdminGrant(host_id=host_id, entries=frozenset())


class AccountPolicy:
    """Decides whether an account may sign in, and with which role."""

    def __init__(
        self,
        *,
        allowlist: Allowlist,
        host_id: str,
        admin_grant: AdminGrant | None = None,
        min_human_uid: int = DEFAULT_MIN_HUMAN_UID,
        deny_shells: Iterable[str] = NON_LOGIN_SHELLS,
        denied_names: Iterable[str] = ALWAYS_DENIED_NAMES,
    ) -> None:
        self._allowlist = allowlist
        self._host_id = host_id
        self._admin_grant = admin_grant if admin_grant is not None else no_admin_grant(host_id)
        self._min_human_uid = int(min_human_uid)
        self._deny_shells = frozenset(deny_shells)
        self._denied_names = frozenset(denied_names)

    @property
    def host_id(self) -> str:
        return self._host_id

    def evaluate(self, account: LinuxAccount) -> PolicyDecision:
        """Classify ``account``.

        Order is deliberate and asserted by tests: the checks that protect the
        machine come before the check that reflects an operator's list, so an
        allowlist that names ``root`` or a daemon still cannot make it a portal
        identity.
        """
        name = account.name

        if name in self._denied_names:
            reason = DenialReason.ROOT_FORBIDDEN if name == "root" else DenialReason.EXPLICITLY_DENIED
            return PolicyDecision(allowed=False, reason=reason, role=None)

        if account.uid < self._min_human_uid:
            return PolicyDecision(allowed=False, reason=DenialReason.SYSTEM_ACCOUNT, role=None)

        if account.shell in self._deny_shells:
            return PolicyDecision(allowed=False, reason=DenialReason.NON_LOGIN_SHELL, role=None)

        if name not in self._allowlist:
            return PolicyDecision(allowed=False, reason=DenialReason.NOT_ALLOWLISTED, role=None)

        role = (
            ROLE_ADMIN
            if self._admin_grant.grants_admin(uid=account.uid, name=name)
            else ROLE_MEMBER
        )
        return PolicyDecision(allowed=True, reason=None, role=role)


def _load_json_object(text: str, *, what: str) -> dict:
    try:
        document = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise errors.ConfigError(detail=f"cannot parse {what}: {exc}") from None
    if not isinstance(document, dict):
        raise errors.ConfigError(detail=f"{what} must be a JSON object")
    return document


__all__ = [
    "ADMIN_GRANT_VERSION",
    "ALLOWLIST_VERSION",
    "ALWAYS_DENIED_NAMES",
    "AccountPolicy",
    "AdminGrant",
    "Allowlist",
    "DEFAULT_MIN_HUMAN_UID",
    "NON_LOGIN_SHELLS",
    "PolicyDecision",
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "load_admin_grant",
    "load_allowlist",
    "no_admin_grant",
    "parse_admin_grant",
    "parse_allowlist",
]
