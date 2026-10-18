"""Reading root-owned configuration files without trusting their contents.

Both the portal allowlist and the platform-administrator grant are files that
an operator places with root privileges. They are *security policy in text
form*, so a file that is missing, oversized, world-writable, owned by the wrong
account or sitting in a directory anyone can rewrite is treated as "no policy",
not as "some policy": every failure path here raises
:class:`backend.auth_linux.errors.ConfigError`, which is a refusal.

The file-system probe is injectable for the same reason the auth service makes
its own probe injectable — so the permission logic can be exercised offline, on
a machine where the test runner is not root and cannot create root-owned files.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import errors

# Policy files are a short list of names. Anything past this is not a policy
# file we wrote, and reading it would only waste memory.
MAX_POLICY_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileFacts:
    exists: bool
    is_regular: bool
    mode: int
    size: int
    uid: int | None
    gid: int | None


def default_fs_probe(path: Path) -> FileFacts:
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return FileFacts(exists=False, is_regular=False, mode=0, size=0, uid=None, gid=None)
    except OSError as exc:
        raise errors.ConfigError(detail=f"cannot stat the policy file: {exc}") from None
    return FileFacts(
        exists=True,
        is_regular=stat.S_ISREG(info.st_mode),
        mode=stat.S_IMODE(info.st_mode),
        size=info.st_size,
        uid=getattr(info, "st_uid", None),
        gid=getattr(info, "st_gid", None),
    )


def read_policy_text(
    path: str | Path,
    *,
    max_bytes: int = MAX_POLICY_FILE_BYTES,
    required_owner_uid: int | None = 0,
    required_parent_owner_uid: int | None = 0,
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> str:
    """Return the text of a root-owned policy file, or refuse.

    ``required_owner_uid`` and ``required_parent_owner_uid`` default to ``0``
    (root) because that is the deployment requirement. Tests pass the running
    user instead; a deployment must not.

    The parent-directory check exists because a root-owned file inside a
    user-writable directory can be replaced by that user at any moment, which
    would make every other check here decorative.
    """
    target = Path(path)

    if required_parent_owner_uid is not None:
        _check_parent_directory(target, required_parent_owner_uid, fs_probe)

    facts = fs_probe(target)
    if not facts.exists:
        raise errors.ConfigError(detail="policy file is missing")
    if not facts.is_regular:
        raise errors.ConfigError(detail="policy file is not a regular file")
    if facts.size > max_bytes:
        raise errors.ConfigError(detail="policy file is implausibly large")

    if required_owner_uid is not None:
        if facts.uid is None:
            raise errors.ConfigError(detail="cannot determine policy file owner")
        if facts.uid != required_owner_uid:
            raise errors.ConfigError(detail="policy file is not owned by the required account")
        # A root-owned file that other accounts may write is not root-owned in
        # any meaningful sense.
        if facts.mode & 0o022:
            raise errors.ConfigError(
                detail=f"policy file is writable by group/other (mode {facts.mode:04o})"
            )

    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.ConfigError(detail=f"cannot read policy file: {exc}") from None


def _check_parent_directory(
    target: Path,
    required_owner_uid: int,
    fs_probe: Callable[[Path], FileFacts],
) -> None:
    parent = target.parent
    facts = fs_probe(parent)
    if not facts.exists:
        raise errors.ConfigError(detail="policy file directory is missing")
    if facts.uid is None:
        raise errors.ConfigError(detail="cannot determine policy file directory owner")
    if facts.uid != required_owner_uid:
        raise errors.ConfigError(detail="policy file directory is not owned by the required account")
    if facts.mode & 0o022:
        raise errors.ConfigError(
            detail=f"policy file directory is writable by group/other (mode {facts.mode:04o})"
        )


__all__ = [
    "FileFacts",
    "MAX_POLICY_FILE_BYTES",
    "default_fs_probe",
    "read_policy_text",
]
