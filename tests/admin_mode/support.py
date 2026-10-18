"""Offline helpers for the shared-admin-mode suite.

Everything here is synthetic. The shared admin password is a fixture value that
exists only in this test package; no deployment password, and no user's real
Linux password, is read, guessed, copied or stored anywhere in this repository.

The credential is hashed with the **real** ``backend.auth.hashing`` at the
security floor, because the properties under test — constant-time comparison,
immediate effect of a rotation, the floor itself — are properties of that code,
not of a stub. It is cached per process only because scrypt at the floor costs
~30 ms and the suite builds many stores.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.auth import hashing
from backend.bff import admin_mode as am

#: Obviously synthetic. Never a real credential, in any environment.
SHARED_PASSWORD = "synthetic-shared-admin-mode-0003"
OTHER_SHARED_PASSWORD = "synthetic-other-shared-admin-mode-0004"

_CREDENTIAL_CACHE: dict[str, str] = {}


def credential_line(password: str = SHARED_PASSWORD, **params) -> str:
    key = f"{password}|{sorted(params.items())}"
    if key not in _CREDENTIAL_CACHE:
        _CREDENTIAL_CACHE[key] = hashing.hash_password(password, **params)
    return _CREDENTIAL_CACHE[key]


def write_credential(
    directory: str | Path,
    password: str = SHARED_PASSWORD,
    *,
    mode: int = 0o600,
    name: str = am.DEFAULT_CREDENTIAL_FILE_NAME,
    **params,
) -> Path:
    """Create a credential file the way the operator CLI would."""
    path = Path(directory) / name
    path.write_text(credential_line(password, **params) + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def make_service(
    directory: str | Path,
    *,
    clock,
    configured: bool = True,
    enabled: bool = True,
    password: str = SHARED_PASSWORD,
    ttl_seconds: int = am.DEFAULT_TTL_SECONDS,
    max_failures: int = 5,
    window_seconds: int = 300,
    lockout_seconds: int = 900,
    credential_path: str | Path | None = None,
    store_path: str | Path | None = None,
) -> am.AdminModeService:
    base = Path(directory)
    if configured and credential_path is None:
        write_credential(base, password)

    credentials = am.AdminCredentialStore(
        credential_path or (base / am.DEFAULT_CREDENTIAL_FILE_NAME)
    )
    store = am.AdminModeStore(store_path or (base / am.DEFAULT_STORE_FILE_NAME), clock=clock)
    store.initialize()
    # The *auth service's* limiter, given this store because it satisfies the
    # same three-method contract. Reused rather than reimplemented: a second
    # sliding-window implementation is a second set of off-by-one bugs.
    from backend.auth.ratelimit import RateLimiter

    return am.AdminModeService(
        credentials=credentials,
        store=store,
        ttl_seconds=ttl_seconds,
        enabled=enabled,
        clock=clock,
        limiter=RateLimiter(
            store,
            max_failures=max_failures,
            window_seconds=window_seconds,
            lockout_seconds=lockout_seconds,
            clock=clock,
        ),
    )


__all__ = [
    "OTHER_SHARED_PASSWORD",
    "SHARED_PASSWORD",
    "credential_line",
    "make_service",
    "write_credential",
]
