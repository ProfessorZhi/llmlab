"""Lane Release/Legacy #50 — release-gate and legacy-parity test package.

Exists for the same reason as ``tests/auth/__init__.py``: ``python3 -m unittest
discover -s tests -t .`` walks this directory as a package path, so the release
gates are picked up by the repository-wide Python run without any extra
invocation.
"""

from __future__ import annotations
