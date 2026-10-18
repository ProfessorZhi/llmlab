"""WSGI entry point.

This module only *builds* an application object from the environment. Wiring it
to a real server, port or systemd unit is a deployment decision and is
explicitly out of scope for this repository — see ``docs/access-boundaries.md``.
Nothing here opens a socket or starts a listener.

Example (from a deployment-owned unit, never from this repository)::

    gunicorn 'backend.auth_entry:create_app_from_env()'

Required environment::

    LLM_LAB_AUTH_STATE_DIR=<absolute path, contains auth.sqlite3>

Optional (Feishu sign-in is simply unavailable until all three are set)::

    LLM_LAB_FEISHU_APP_ID / LLM_LAB_FEISHU_APP_SECRET / LLM_LAB_FEISHU_REDIRECT_URI
    LLM_LAB_FEISHU_TENANT_ALLOWLIST=<comma separated tenant_key values, required>

See ``docs/auth-implementation.md`` for the full list.
"""

from __future__ import annotations

import os
from typing import Mapping

from backend.auth import feishu
from backend.auth.config import AuthConfig
from backend.auth.routes import Application, create_app


def create_app_from_env(environ: Mapping[str, str] | None = None) -> Application:
    """Build (and initialise) the auth application from the process environment.

    Raises :class:`backend.auth.errors.ConfigError` when the configuration is
    incomplete or incoherent: the process is meant to refuse to start rather
    than serve a half-configured login.
    """
    source = os.environ if environ is None else environ
    cfg = AuthConfig.from_env(source)
    app = create_app(cfg, transport=feishu.UrllibTransport(), clock=_clock())
    app.initialize()
    return app


def _clock():
    import time

    return time.time


__all__ = ["create_app_from_env"]
