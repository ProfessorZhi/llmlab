"""Configuration for the Linux sign-in path, loaded fail-closed.

The rule the rest of the auth service already follows applies here: an
incomplete or self-contradictory configuration stops the process rather than
degrading into a sign-in method that merely looks like it works. A missing
allowlist path, a relative socket path, a timeout that is not a number — each of
those raises :class:`~backend.auth_linux.errors.ConfigError`.

The default is **off**. ``LLM_LAB_LINUX_AUTH_ENABLED`` has to be set
deliberately, so adding this package to a deployment changes nothing until an
operator asks for it, and nothing about the existing local-admin or Feishu paths
moves.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import errors
from .policy import DEFAULT_MIN_HUMAN_UID

ENV_ENABLED = "LLM_LAB_LINUX_AUTH_ENABLED"
ENV_HOST_ID = "LLM_LAB_LINUX_HOST_ID"
ENV_BROKER_SOCKET = "LLM_LAB_PAM_BROKER_SOCKET"
ENV_ALLOWLIST_FILE = "LLM_LAB_LINUX_ALLOWLIST_FILE"
ENV_ADMIN_GRANT_FILE = "LLM_LAB_LINUX_ADMIN_GRANT_FILE"
ENV_BROKER_TIMEOUT = "LLM_LAB_LINUX_BROKER_TIMEOUT_SECONDS"
ENV_MIN_UID = "LLM_LAB_LINUX_MIN_UID"
ENV_POLICY_OWNER_UID = "LLM_LAB_LINUX_POLICY_OWNER_UID"
ENV_MAX_FAILURES = "LLM_LAB_LINUX_MAX_FAILURES"
ENV_WINDOW_SECONDS = "LLM_LAB_LINUX_WINDOW_SECONDS"
ENV_LOCKOUT_SECONDS = "LLM_LAB_LINUX_LOCKOUT_SECONDS"

DEFAULT_BROKER_TIMEOUT_SECONDS = 5.0
MAX_BROKER_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 300
DEFAULT_LOCKOUT_SECONDS = 900

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

# ``\A``/``\Z``, not ``^``/``$``: ``$`` would also accept a trailing newline.
_HOST_ID_PATTERN = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")


@dataclass(frozen=True)
class LinuxAuthConfig:
    enabled: bool
    host_id: str | None = None
    broker_socket_path: Path | None = None
    allowlist_path: Path | None = None
    admin_grant_path: Path | None = None
    broker_timeout_seconds: float = DEFAULT_BROKER_TIMEOUT_SECONDS
    min_human_uid: int = DEFAULT_MIN_HUMAN_UID
    policy_owner_uid: int = 0
    policy_parent_owner_uid: int = 0
    max_failures: int = DEFAULT_MAX_FAILURES
    window_seconds: int = DEFAULT_WINDOW_SECONDS
    lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS

    def require_enabled(self) -> "LinuxAuthConfig":
        if not self.enabled:
            raise errors.NotConfigured(detail="the Linux sign-in path is not enabled")
        return self

    @property
    def host(self) -> str:
        if self.host_id is None:  # pragma: no cover - guarded by validation
            raise errors.ConfigError(detail="host_id is not configured")
        return self.host_id


def from_environ(environ: Mapping[str, str] | None = None) -> LinuxAuthConfig:
    """Read configuration from the process environment.

    Returns a disabled configuration when ``LLM_LAB_LINUX_AUTH_ENABLED`` is
    unset, and validates nothing else in that case — a deployment that has not
    enabled the feature should not be forced to supply its paths.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ

    enabled = _bool_value(source, ENV_ENABLED, default=False)
    if not enabled:
        return LinuxAuthConfig(enabled=False)

    host_id = _required_text(source, ENV_HOST_ID)
    if not _HOST_ID_PATTERN.match(host_id):
        raise errors.ConfigError(detail=f"{ENV_HOST_ID} is not a valid host identifier")

    socket_path = _absolute_path(source, ENV_BROKER_SOCKET)
    allowlist_path = _absolute_path(source, ENV_ALLOWLIST_FILE)

    grant_value = source.get(ENV_ADMIN_GRANT_FILE)
    admin_grant_path: Path | None = None
    if grant_value is not None and grant_value.strip() != "":
        admin_grant_path = _absolute_path(source, ENV_ADMIN_GRANT_FILE)

    owner_uid = _int_value(
        source, ENV_POLICY_OWNER_UID, default=0, minimum=0, maximum=2**31 - 1
    )

    return LinuxAuthConfig(
        enabled=True,
        host_id=host_id,
        broker_socket_path=socket_path,
        allowlist_path=allowlist_path,
        admin_grant_path=admin_grant_path,
        broker_timeout_seconds=_float_value(
            source,
            ENV_BROKER_TIMEOUT,
            default=DEFAULT_BROKER_TIMEOUT_SECONDS,
            minimum=0.1,
            maximum=MAX_BROKER_TIMEOUT_SECONDS,
        ),
        min_human_uid=_int_value(
            source, ENV_MIN_UID, default=DEFAULT_MIN_HUMAN_UID, minimum=0, maximum=2**31 - 1
        ),
        # The policy files are root-owned in a deployment. The parent directory
        # is held to the same owner, because a root-owned file inside a
        # user-writable directory can be swapped by that user.
        policy_owner_uid=owner_uid,
        policy_parent_owner_uid=owner_uid,
        max_failures=_int_value(source, ENV_MAX_FAILURES, default=DEFAULT_MAX_FAILURES, minimum=1, maximum=1000),
        window_seconds=_int_value(
            source, ENV_WINDOW_SECONDS, default=DEFAULT_WINDOW_SECONDS, minimum=1, maximum=86400
        ),
        lockout_seconds=_int_value(
            source, ENV_LOCKOUT_SECONDS, default=DEFAULT_LOCKOUT_SECONDS, minimum=1, maximum=86400
        ),
    )


# --- parsing helpers ---------------------------------------------------------


def _bool_value(source: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = source.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    # Not "treat as false": a typo in a security switch should be loud.
    raise errors.ConfigError(detail=f"{key} is not a recognised boolean")


def _required_text(source: Mapping[str, str], key: str) -> str:
    raw = source.get(key)
    if raw is None or raw.strip() == "":
        raise errors.ConfigError(detail=f"{key} is required when the Linux sign-in path is enabled")
    return raw.strip()


def _absolute_path(source: Mapping[str, str], key: str) -> Path:
    raw = _required_text(source, key)
    path = Path(raw)
    if not path.is_absolute():
        # A relative path would resolve against whatever working directory the
        # service happens to be started from, which is not a property any
        # policy file should depend on.
        raise errors.ConfigError(detail=f"{key} must be an absolute path")
    return path


def _int_value(
    source: Mapping[str, str], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = source.get(key)
    if raw is None or raw.strip() == "":
        return default
    text = raw.strip()
    if not text.isdigit():
        raise errors.ConfigError(detail=f"{key} must be a non-negative integer")
    value = int(text)
    if not (minimum <= value <= maximum):
        raise errors.ConfigError(detail=f"{key} is outside the permitted range")
    return value


def _float_value(
    source: Mapping[str, str], key: str, *, default: float, minimum: float, maximum: float
) -> float:
    raw = source.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise errors.ConfigError(detail=f"{key} must be a number") from None
    if not (minimum <= value <= maximum):
        raise errors.ConfigError(detail=f"{key} is outside the permitted range")
    return value


__all__ = [
    "DEFAULT_BROKER_TIMEOUT_SECONDS",
    "DEFAULT_LOCKOUT_SECONDS",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_WINDOW_SECONDS",
    "ENV_ADMIN_GRANT_FILE",
    "ENV_ALLOWLIST_FILE",
    "ENV_BROKER_SOCKET",
    "ENV_BROKER_TIMEOUT",
    "ENV_ENABLED",
    "ENV_HOST_ID",
    "ENV_LOCKOUT_SECONDS",
    "ENV_MAX_FAILURES",
    "ENV_MIN_UID",
    "ENV_POLICY_OWNER_UID",
    "ENV_WINDOW_SECONDS",
    "LinuxAuthConfig",
    "from_environ",
]
