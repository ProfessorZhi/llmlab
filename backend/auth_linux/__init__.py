"""Linux username + Linux password sign-in for the LLM Lab Portal.

Scope
-----
This package decides **one** thing: whether a person who knows a Linux
account's password on a known host may be treated as that account's owner.
It does not mint sessions, set cookies, issue CSRF tokens, render pages or
talk HTTP. Identity is handed back to the existing auth service through the
narrow contract in :mod:`backend.auth_linux.handoff`, which is the *only*
place allowed to start a session.

Why a broker exists
-------------------
The Portal web process runs as an ordinary, unprivileged account (``alice``).
That process cannot read ``/etc/shadow``, and Linux PAM's ``pam_unix`` module
verifies through the ``unix_chkpwd`` helper, which is deliberately limited to
the *calling* user. So "just call PAM from the web process" would only ever be
able to verify the account the web server already runs as — it can never
verify ``carol`` or ``bob``.

Raising the web process's privileges (setuid helpers, a shell-out to ``su`` or
``login``, a world-writable socket) trades one small problem for a large one.
Instead the password check happens in a **separate, restricted, auditable
broker** that runs with whatever privilege PAM needs, and the web process
reaches it over a root-owned Unix domain socket whose callers are restricted by
:manpage:`SO_PEERCRED(7)`. The broker can do exactly three things for exactly
the allowlisted accounts: ``pam_authenticate``, ``pam_acct_mgmt``, and a
read-only ``getpwnam``. It returns a UID, a name and a validity flag. It never
returns a password, a hash, or a shadow entry, and it is not a general-purpose
"run this as root" facility.

Invariants this package is built to hold
----------------------------------------
* **No default allow.** Every unverifiable state — broker absent, unreachable,
  slow, malformed, ambiguous — resolves to ``PAM_UNAVAILABLE``, and
  ``PAM_UNAVAILABLE`` never authenticates anyone.
* **Machine accounts cannot sign in to the web.** ``root`` never can, no matter
  what any configuration file says. Accounts below the human UID floor, or with
  a non-login shell, are refused before PAM is consulted at all.
* **Roles are never inferred.** A Linux login yields ``member``. The single
  exception is an explicit grant in a root-owned file keyed on ``(host_id,
  uid)``, which is the platform administrator grant the deployment asked for.
  A username, a request field, a similar-looking name or a missing file can
  never produce ``platform_admin``.
* **No aliasing.** ``dana`` is not ``bob``. The allowlist schema has no
  alias field, unknown keys are rejected outright, and a login name is only
  ever looked up as itself. Binding a person to a real account is a deliberate
  configuration act, not a convenience built into the lookup.
* **The password exists only in memory, only briefly.** It is never written to
  a file, a log, an exception, a URL, an audit record or a command line.

Everything here is pure Python standard library, so the suite runs offline
(``python -m unittest discover -s tests/auth_linux -t .``).
"""

from __future__ import annotations

__all__ = [
    "accounts",
    "audit",
    "broker_client",
    "broker_server",
    "config",
    "errors",
    "handoff",
    "pam_backend",
    "policy",
    "protocol",
    "provider",
    "reasons",
    "service",
    "throttle",
]
