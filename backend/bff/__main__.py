"""``python3 -m backend.bff`` —— 环回地址上的本地 BFF 进程。"""

from __future__ import annotations

from .devserver import main

raise SystemExit(main())
