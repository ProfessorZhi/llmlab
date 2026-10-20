"""Independent platform authentication.

Platform credentials are separate from Linux credentials by construction: this
package never reads PAM, ``/etc/passwd``, ``/etc/shadow``, an SSH identity, or a
UID, and no platform principal carries a Linux identity. A platform account may
exist with no Linux account at all.

Public surface:

* :class:`~backend.platform_auth.service.PlatformAuthService` — accounts,
  login, sessions, scopes, password rotation, disable, audit;
* :class:`~backend.platform_auth.service.PlatformPrincipal` — the authenticated
  subject (``platform_user_id`` + scopes, no UID);
* :class:`~backend.platform_auth.wsgi.PlatformGuard` — WSGI session/CSRF guard
  for a future same-origin BFF integration;
* :mod:`backend.platform_auth.cli` — local operator CLI.

This is a reviewed core, not a deployed login: no service is started here, and
the production BFF has not been wired to it.
"""

from .errors import (
    AccountDisabled,
    CsrfRejected,
    Forbidden,
    InvalidCredentials,
    NotAuthenticated,
    PlatformAuthError,
    RateLimited,
    StorageSecurityError,
    ValidationError,
)
from .service import (
    KNOWN_SCOPES,
    SCOPE_ADMIN,
    SCOPE_INVOKE,
    SCOPE_READ,
    LoginResult,
    PlatformAuthService,
    PlatformPrincipal,
)
from .store import Store, prepare_state_dir

__all__ = [
    "AccountDisabled",
    "CsrfRejected",
    "Forbidden",
    "InvalidCredentials",
    "KNOWN_SCOPES",
    "LoginResult",
    "NotAuthenticated",
    "PlatformAuthError",
    "PlatformAuthService",
    "PlatformPrincipal",
    "RateLimited",
    "SCOPE_ADMIN",
    "SCOPE_INVOKE",
    "SCOPE_READ",
    "StorageSecurityError",
    "Store",
    "ValidationError",
    "prepare_state_dir",
]
