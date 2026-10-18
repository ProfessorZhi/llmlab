"""Credentials for the single local platform administrator.

The administrator password is never stored in this repository, never passed on
a command line, and never kept in an environment variable. It exists on disk
only as a hash, in a file that must be readable by nobody but its owner, and it
is produced by :mod:`backend.auth.bootstrap` through an interactive prompt on
the machine that will serve the portal.

**Default state is "no password".** A missing file means the local
administrator cannot log in at all — it does not mean "any password". The only
supported ways to change that are the bootstrap CLI and an explicit deploy
step; there is no fallback, no default value, and no empty-hash shortcut.

File format (strict, one meaningful line)::

    # optional comments
    scrypt$n=16384,r=8,p=1$<b64 salt>$<b64 digest>

or the single word ``disabled``, which keeps the file present — so operators can
tell "deliberately switched off" from "never initialised" — while refusing every
local login.
"""

from __future__ import annotations

import hmac
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import errors, hashing

STATE_MISSING = "missing"
STATE_DISABLED = "disabled"
STATE_ACTIVE = "active"
STATE_BROKEN = "broken"

DISABLED_SENTINEL = "disabled"

MAX_FILE_BYTES = 1024


@dataclass(frozen=True)
class FileFacts:
    exists: bool
    is_regular: bool
    mode: int
    size: int
    uid: int | None


def default_fs_probe(path: Path) -> FileFacts:
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return FileFacts(exists=False, is_regular=False, mode=0, size=0, uid=None)
    except OSError as exc:
        raise errors.ConfigError(detail=f"cannot stat the credential file: {exc}") from None
    return FileFacts(
        exists=True,
        is_regular=stat.S_ISREG(info.st_mode),
        mode=stat.S_IMODE(info.st_mode),
        size=info.st_size,
        uid=getattr(info, "st_uid", None),
    )


def _process_uid() -> int | None:
    getuid = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - Windows
        return None
    try:
        return int(getuid())
    except OSError:  # pragma: no cover - defensive
        return None


class AdminCredentialStore:
    """Reads and verifies the local administrator credential file.

    The file is re-read on every verification, so a rotation or a disable takes
    effect immediately without restarting the process (one small ``stat`` plus a
    sub-kilobyte read per login attempt — negligible, and it removes a whole
    class of "forgot to restart after rotating" incidents).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        username: str = "admin",
        enforce_permissions: bool | None = None,
        fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
        process_uid: int | None = None,
    ) -> None:
        self.path = Path(path)
        self._username = username
        self._probe = fs_probe
        # POSIX has real mode bits; Windows does not, so enforcement there would
        # only ever produce false alarms. Deployments on POSIX get the check.
        self._enforce_permissions = os.name == "posix" if enforce_permissions is None else enforce_permissions
        self._uid = _process_uid() if process_uid is None else process_uid

    # --- inspection ----------------------------------------------------------

    def is_configured(self) -> bool:
        return self.status()["state"] == STATE_ACTIVE

    def status(self) -> dict[str, object]:
        """Secret-free description of the credential file, for operators."""
        state, encoded = self._load()
        report: dict[str, object] = {
            "state": state,
            "path": str(self.path),
            "username": self._username,
        }
        if state == STATE_ACTIVE and encoded is not None:
            report["algorithm"] = (
                "argon2id" if encoded.startswith("$argon2id$") else "scrypt"
            )
            report["needs_rehash"] = hashing.hash_needs_rehash(encoded)
        else:
            report["algorithm"] = None
            report["needs_rehash"] = False
        return report

    # --- verification --------------------------------------------------------

    def authenticate(self, username: str, password: str) -> bool:
        """Return True only for the configured administrator and its password.

        Raises :class:`errors.NotConfigured`, :class:`errors.AccountDisabled` or
        :class:`errors.ConfigError` when the local login path is unavailable —
        never silently falls back to accepting anything.
        """
        state, encoded = self._load()
        if state == STATE_MISSING:
            raise errors.NotConfigured(detail="no administrator credential file")
        if state == STATE_DISABLED:
            raise errors.AccountDisabled(detail="administrator login is disabled")
        if state == STATE_BROKEN or encoded is None:
            raise errors.ConfigError(detail="administrator credential file is not usable")

        supplied = username if isinstance(username, str) else ""
        # Bytes, not str: compare_digest rejects non-ASCII str inputs, and we do
        # not want a Unicode username to raise instead of simply failing.
        username_ok = hmac.compare_digest(supplied.encode("utf-8"), self._username.encode("utf-8"))
        # Always run the (expensive) password verification, even when the
        # username is wrong, so the two outcomes cost the same.
        password_ok = hashing.verify_password(password, encoded)
        return bool(username_ok and password_ok)

    # --- internals -----------------------------------------------------------

    def _load(self) -> tuple[str, str | None]:
        facts = self._probe(self.path)
        if not facts.exists:
            return STATE_MISSING, None

        self._check_permissions(facts)
        if not facts.is_regular:
            raise errors.ConfigError(detail="administrator credential must be a regular file")
        if facts.size > MAX_FILE_BYTES:
            raise errors.ConfigError(detail="administrator credential file is implausibly large")

        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise errors.ConfigError(
                detail=f"cannot read the administrator credential file: {exc}"
            ) from None

        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not lines:
            # An empty file is a broken write, not a deliberate disable.
            return STATE_BROKEN, None
        if DISABLED_SENTINEL in lines:
            return STATE_DISABLED, None
        if len(lines) != 1:
            return STATE_BROKEN, None
        if not hashing.is_well_formed(lines[0]):
            return STATE_BROKEN, None
        return STATE_ACTIVE, lines[0]

    def _check_permissions(self, facts: FileFacts) -> None:
        if not self._enforce_permissions:
            return
        if facts.mode & 0o077:
            raise errors.ConfigError(
                detail=(
                    "administrator credential file is readable or writable by group/other "
                    f"(mode {facts.mode:04o}); expected 0600"
                )
            )
        if facts.uid is not None and self._uid is not None and facts.uid != self._uid:
            raise errors.ConfigError(
                detail="administrator credential file is not owned by the service account"
            )


__all__ = [
    "AdminCredentialStore",
    "DISABLED_SENTINEL",
    "FileFacts",
    "MAX_FILE_BYTES",
    "STATE_ACTIVE",
    "STATE_BROKEN",
    "STATE_DISABLED",
    "STATE_MISSING",
    "default_fs_probe",
]
