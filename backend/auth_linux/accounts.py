"""Read-only Linux account facts.

Everything here is a *lookup*: ``getpwnam`` / ``getpwuid`` and nothing else.
This module never opens ``/etc/shadow``, never calls PAM, never changes an
account, and never suppresses the fact that the web process is unprivileged.

Two consequences worth stating plainly, because they shape the whole design:

* ``getpwnam`` resolves through NSS, which on a joined host can mean SSSD or
  LDAP. It is therefore **not** instant and **not** guaranteed to succeed. A
  name that does not resolve is a refusal, never an invitation to guess.
* Nothing here proves a password. These facts are used to decide whether an
  account is one we are willing to ask the broker about, and afterwards to
  cross-check the broker's answer against our own view of the same account.

The resolution functions are wrapped behind :class:`PwdSource` so the policy and
provider tests can run against a synthetic account table instead of the
accounts that happen to exist on the machine running the tests.
"""

from __future__ import annotations

import pwd
import re
from dataclasses import dataclass
from typing import Protocol

# Debian-style ``NAME_REGEX``, tightened: lower-case, no leading digit or dash,
# at most 32 characters. Deliberately narrower than what a hand-edited
# ``/etc/passwd`` can contain, because this string is about to be handed to a
# privileged broker and to NSS, and a strict allowlist of characters is a
# cheaper control than reasoning about every parser in that path.
#
# ``\A``/``\Z`` rather than ``^``/``$``: in Python, ``$`` also matches just
# before a trailing newline, so ``"root\n"`` would have been accepted as the
# name ``root``. The anchor has to be end-of-string, not end-of-line.
_LOGIN_NAME_PATTERN = re.compile(r"\A[a-z_][a-z0-9_-]{0,31}\Z")

MAX_LOGIN_NAME_LENGTH = 32


def canonical_login_name(name: object) -> str | None:
    """Return ``name`` if it is a well-formed Linux login name, else ``None``.

    This is the *only* normalisation the module performs. There is no case
    folding, no alias table, no prefix or suffix stripping: ``dana`` stays
    ``dana``. If ``dana`` is not a real account, the answer is that no such
    account exists.
    """
    if not isinstance(name, str):
        return None
    if len(name) > MAX_LOGIN_NAME_LENGTH:
        return None
    if not _LOGIN_NAME_PATTERN.match(name):
        return None
    return name


@dataclass(frozen=True)
class LinuxAccount:
    name: str
    uid: int
    gid: int
    shell: str
    home: str

    def __post_init__(self) -> None:
        # Cheap sanity, so a synthetic or corrupted record cannot masquerade as
        # a human account by carrying a sentinel UID.
        if not isinstance(self.uid, int) or self.uid < 0:
            raise ValueError("uid must be a non-negative int")


class PwdSource(Protocol):
    """The read-only account lookups this package depends on."""

    def by_name(self, name: str) -> LinuxAccount | None: ...

    def by_uid(self, uid: int) -> LinuxAccount | None: ...


class SystemPwdSource:
    """The real thing: NSS through the stdlib ``pwd`` module.

    Every failure is reported as "no such account" rather than propagated,
    because an NSS outage, a malformed NSS record and a genuinely absent account
    must all lead to the same refusal. The distinction, where it matters, is
    recorded by the broker on the privileged side.
    """

    def by_name(self, name: str) -> LinuxAccount | None:
        if not isinstance(name, str) or not name:
            return None
        try:
            record = pwd.getpwnam(name)
        except (KeyError, OSError, TypeError, ValueError):
            return None
        return _from_record(record)

    def by_uid(self, uid: int) -> LinuxAccount | None:
        if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            return None
        try:
            record = pwd.getpwuid(uid)
        except (KeyError, OSError, TypeError, ValueError):
            return None
        return _from_record(record)


def _from_record(record: object) -> LinuxAccount | None:
    try:
        return LinuxAccount(
            name=str(record.pw_name),  # type: ignore[attr-defined]
            uid=int(record.pw_uid),  # type: ignore[attr-defined]
            gid=int(record.pw_gid),  # type: ignore[attr-defined]
            shell=str(record.pw_shell),  # type: ignore[attr-defined]
            home=str(record.pw_dir),  # type: ignore[attr-defined]
        )
    except (AttributeError, TypeError, ValueError):
        return None


def uid_matches(account: LinuxAccount, *, uid: object, name: object) -> bool:
    """Do the broker's ``(uid, name)`` agree with our own read-only view?

    Both have to agree. Comparing only the UID would accept a reply that names a
    different account holding the same UID; comparing only the name would accept
    a reply that re-points a familiar name at a privileged UID. Requiring both
    means the broker cannot hand back an identity that our own ``getpwnam``
    would not have produced.
    """
    if not isinstance(uid, int) or isinstance(uid, bool):
        return False
    if not isinstance(name, str):
        return False
    return account.uid == uid and account.name == name


__all__ = [
    "MAX_LOGIN_NAME_LENGTH",
    "LinuxAccount",
    "PwdSource",
    "SystemPwdSource",
    "canonical_login_name",
    "uid_matches",
]
