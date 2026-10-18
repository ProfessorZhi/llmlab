"""``python3 -m backend.registry`` 的入口（临时 loopback 开发服务）。"""

from __future__ import annotations

from .devserver import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
