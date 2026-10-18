"""Runtime configuration, loaded from the process environment.

Rules:

* **Fail closed.** Anything ambiguous or half-configured raises
  :class:`~backend.auth.errors.ConfigError` and the process refuses to serve.
  A partially configured Feishu block is *never* treated as "disabled" — it is
  treated as broken, because silently degrading to a local-only login on a
  typo'd environment variable is exactly the kind of downgrade this backend is
  built to prevent.
* **Secrets are injected, never committed.** ``feishu_app_secret`` is marked
  ``repr=False`` so it cannot leak through a traceback that prints the config.
* **Navigation targets are local paths.** ``post_login_path`` and
  ``login_error_path`` are validated as same-origin absolute paths so a typo
  cannot turn the callback into an open redirect.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from . import errors

ENV_PREFIX = "LLM_LAB_"

# The product has exactly one local administrator, and its name is not a
# deployment choice: it is baked into the bootstrap prompts, the docs and the
# operator runbook. Renaming it through the environment would create a *second*
# local administrative identity beside the documented one, so the override is
# refused outright rather than honoured.
LOCAL_ADMIN_USERNAME = "admin"

DEFAULTS = {
    "SESSION_IDLE_SECONDS": 1800,
    "SESSION_ABSOLUTE_SECONDS": 43200,
    "COOKIE_NAME": "llm_lab_session",
    "CSRF_COOKIE_NAME": "llm_lab_csrf",
    "OAUTH_BINDING_COOKIE_NAME": "llm_lab_oauth_binding",
    "MAX_REQUEST_BYTES": 8192,
    "LOGIN_MAX_FAILURES": 5,
    "LOGIN_WINDOW_SECONDS": 300,
    "LOCKOUT_SECONDS": 900,
    "POST_LOGIN_PATH": "/",
    "LOGIN_ERROR_PATH": "/login",
}

_INSECURE_REDIRECT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _env(environ: Mapping[str, str], name: str) -> str | None:
    raw = environ.get(ENV_PREFIX + name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _int_env(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = _env(environ, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise errors.ConfigError(detail=f"{ENV_PREFIX}{name} must be an integer") from None
    if value <= 0:
        raise errors.ConfigError(detail=f"{ENV_PREFIX}{name} must be a positive integer")
    return value


def _bool_env(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _env(environ, name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _csv_env(environ: Mapping[str, str], name: str) -> frozenset[str]:
    raw = environ.get(ENV_PREFIX + name) or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_local_path(value: object) -> bool:
    """Same-origin absolute path, e.g. ``/`` or ``/dashboard``.

    Rejects ``//host`` (protocol-relative), any scheme (``javascript:``),
    backslashes and control characters. Used both for configuration values and
    for any operator- or attacker-supplied redirect target.
    """
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return False
    if "\\" in value or any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        return False
    split = urlsplit(value)
    return not split.scheme and not split.netloc


# Retained for readability at the call sites inside this module.
_is_local_path = is_local_path


@dataclass(frozen=True)
class AuthConfig:
    """Immutable view of the authentication configuration."""

    state_dir: Path
    admin_hash_file: Path
    admin_username: str = LOCAL_ADMIN_USERNAME

    session_idle_seconds: int = DEFAULTS["SESSION_IDLE_SECONDS"]
    session_absolute_seconds: int = DEFAULTS["SESSION_ABSOLUTE_SECONDS"]

    cookie_name: str = DEFAULTS["COOKIE_NAME"]
    csrf_cookie_name: str = DEFAULTS["CSRF_COOKIE_NAME"]
    oauth_binding_cookie_name: str = DEFAULTS["OAUTH_BINDING_COOKIE_NAME"]
    cookie_secure: bool = True
    cookie_httponly: bool = True
    cookie_samesite: str = "Lax"

    max_request_bytes: int = DEFAULTS["MAX_REQUEST_BYTES"]
    login_max_failures: int = DEFAULTS["LOGIN_MAX_FAILURES"]
    login_window_seconds: int = DEFAULTS["LOGIN_WINDOW_SECONDS"]
    lockout_seconds: int = DEFAULTS["LOCKOUT_SECONDS"]

    post_login_path: str = DEFAULTS["POST_LOGIN_PATH"]
    login_error_path: str = DEFAULTS["LOGIN_ERROR_PATH"]
    trusted_origins: frozenset[str] = frozenset()
    allow_insecure_redirect_uri: bool = False

    feishu_app_id: str | None = None
    feishu_app_secret: str | None = field(default=None, repr=False)
    feishu_redirect_uri: str | None = None
    feishu_tenant_allowlist: frozenset[str] = frozenset()
    feishu_pkce_enabled: bool = False

    @property
    def feishu_configured(self) -> bool:
        """All three pieces of the app identity are present."""
        return bool(self.feishu_app_id and self.feishu_app_secret and self.feishu_redirect_uri)

    @property
    def state_db_path(self) -> Path:
        return self.state_dir / "auth.sqlite3"

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        *,
        state_dir: str | Path | None = None,
    ) -> "AuthConfig":
        raw_state_dir = state_dir if state_dir is not None else _env(environ, "AUTH_STATE_DIR")
        if not raw_state_dir:
            raise errors.ConfigError(
                detail=f"{ENV_PREFIX}AUTH_STATE_DIR is required (or pass state_dir=)"
            )

        state_path = Path(raw_state_dir)
        raw_hash_file = _env(environ, "ADMIN_HASH_FILE")
        hash_path = Path(raw_hash_file) if raw_hash_file else state_path / "admin_password.hash"

        cfg = cls(
            state_dir=state_path,
            admin_hash_file=hash_path,
            admin_username=_env(environ, "ADMIN_USERNAME") or LOCAL_ADMIN_USERNAME,
            session_idle_seconds=_int_env(
                environ, "SESSION_IDLE_SECONDS", DEFAULTS["SESSION_IDLE_SECONDS"]
            ),
            session_absolute_seconds=_int_env(
                environ, "SESSION_ABSOLUTE_SECONDS", DEFAULTS["SESSION_ABSOLUTE_SECONDS"]
            ),
            cookie_name=_env(environ, "COOKIE_NAME") or DEFAULTS["COOKIE_NAME"],
            csrf_cookie_name=_env(environ, "CSRF_COOKIE_NAME") or DEFAULTS["CSRF_COOKIE_NAME"],
            oauth_binding_cookie_name=_env(environ, "OAUTH_BINDING_COOKIE_NAME")
            or DEFAULTS["OAUTH_BINDING_COOKIE_NAME"],
            cookie_secure=_bool_env(environ, "COOKIE_SECURE", True),
            cookie_httponly=True,  # not configurable: the session cookie is never readable by JS
            cookie_samesite="Lax",  # not configurable: needed for the OAuth top-level redirect
            max_request_bytes=_int_env(
                environ, "MAX_REQUEST_BYTES", DEFAULTS["MAX_REQUEST_BYTES"]
            ),
            login_max_failures=_int_env(
                environ, "LOGIN_MAX_FAILURES", DEFAULTS["LOGIN_MAX_FAILURES"]
            ),
            login_window_seconds=_int_env(
                environ, "LOGIN_WINDOW_SECONDS", DEFAULTS["LOGIN_WINDOW_SECONDS"]
            ),
            lockout_seconds=_int_env(environ, "LOCKOUT_SECONDS", DEFAULTS["LOCKOUT_SECONDS"]),
            post_login_path=_env(environ, "POST_LOGIN_PATH") or DEFAULTS["POST_LOGIN_PATH"],
            login_error_path=_env(environ, "LOGIN_ERROR_PATH") or DEFAULTS["LOGIN_ERROR_PATH"],
            trusted_origins=_csv_env(environ, "TRUSTED_ORIGINS"),
            allow_insecure_redirect_uri=_bool_env(environ, "ALLOW_INSECURE_REDIRECT_URI", False),
            feishu_app_id=_env(environ, "FEISHU_APP_ID"),
            feishu_app_secret=_env(environ, "FEISHU_APP_SECRET"),
            feishu_redirect_uri=_env(environ, "FEISHU_REDIRECT_URI"),
            feishu_tenant_allowlist=_csv_env(environ, "FEISHU_TENANT_ALLOWLIST"),
            feishu_pkce_enabled=_bool_env(environ, "FEISHU_PKCE", False),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Raise :class:`ConfigError` unless the whole configuration is coherent."""
        if not self.state_dir.is_absolute():
            raise errors.ConfigError(detail="AUTH_STATE_DIR must be an absolute path")
        if self.admin_username != LOCAL_ADMIN_USERNAME:
            # Fail closed. The product ships exactly one local administrator and
            # it is named ``admin``; honouring the override would silently mint a
            # second administrative identity outside the documented one. Checked
            # here rather than in ``from_env`` so ``replace()`` and direct
            # construction cannot slip past it either.
            raise errors.ConfigError(
                detail=f"ADMIN_USERNAME is fixed at {LOCAL_ADMIN_USERNAME!r}; "
                f"{self.admin_username!r} is refused"
            )

        if self.session_idle_seconds > self.session_absolute_seconds:
            raise errors.ConfigError(
                detail="SESSION_IDLE_SECONDS must not exceed SESSION_ABSOLUTE_SECONDS"
            )

        if not _is_local_path(self.post_login_path):
            raise errors.ConfigError(detail="POST_LOGIN_PATH must be a same-origin absolute path")
        if not _is_local_path(self.login_error_path):
            raise errors.ConfigError(detail="LOGIN_ERROR_PATH must be a same-origin absolute path")

        if not self.cookie_name or not self.csrf_cookie_name or not self.oauth_binding_cookie_name:
            raise errors.ConfigError(detail="cookie names must not be empty")

        self._validate_feishu()

    def _validate_feishu(self) -> None:
        provided = [self.feishu_app_id, self.feishu_app_secret, self.feishu_redirect_uri]
        if any(provided) and not all(provided):
            raise errors.ConfigError(
                detail="FEISHU_APP_ID, FEISHU_APP_SECRET and FEISHU_REDIRECT_URI "
                "must be set together"
            )
        if not self.feishu_configured:
            return

        if not self.feishu_tenant_allowlist:
            raise errors.ConfigError(
                detail="FEISHU_TENANT_ALLOWLIST must list at least one tenant_key; "
                "an empty allowlist is refused rather than accepting every tenant"
            )

        split = urlsplit(self.feishu_redirect_uri or "")
        if not split.scheme or not split.netloc:
            raise errors.ConfigError(detail="FEISHU_REDIRECT_URI must be an absolute URL")
        if split.fragment:
            raise errors.ConfigError(detail="FEISHU_REDIRECT_URI must not contain a fragment")
        if split.scheme != "https":
            if not (
                self.allow_insecure_redirect_uri
                and (split.hostname or "") in _INSECURE_REDIRECT_HOSTS
            ):
                raise errors.ConfigError(
                    detail="FEISHU_REDIRECT_URI must use https "
                    "(http is only allowed for loopback hosts with "
                    "LLM_LAB_ALLOW_INSECURE_REDIRECT_URI=1)"
                )

    def replace(self, **changes: object) -> "AuthConfig":
        """Copy with overrides, re-validated. Handy for tests and bootstrap."""
        candidate = dataclasses.replace(self, **changes)  # type: ignore[arg-type]
        candidate.validate()
        return candidate


__all__ = [
    "AuthConfig",
    "DEFAULTS",
    "ENV_PREFIX",
    "LOCAL_ADMIN_USERNAME",
    "is_local_path",
]
