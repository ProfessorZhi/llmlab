"""Dual-entry authentication backend: local ``admin`` + Feishu OAuth SSO.

Design rules enforced by this package (see ``docs/auth-implementation.md``):

* Exactly one local platform administrator (username ``admin`` by default).
  The password hash is never committed; it is produced by an interactive
  bootstrap and loaded from a private ``0600`` file at runtime.
* Every other human identity comes from Feishu OAuth. Feishu only answers
  *who you are*; authorisation is decided here, and a Feishu identity never
  grants administrator rights or project grants implicitly.
* Secrets live in the process environment, never in the repository, never in
  logs, never in HTTP responses, never in cookies.

Everything in this package is pure Python standard library so that it can be
tested offline (``python -m unittest discover -s tests/auth -t .``).
"""

__all__ = [
    "config",
    "errors",
    "hashing",
]
