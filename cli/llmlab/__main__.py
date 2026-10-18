"""``python3 -m cli.llmlab`` 的入口。

实现放在 ``entry.py``：这样 ``__init__`` 与 ``-m`` 执行的是**同一个**模块对象，
不会出现「包导入一次、-m 再执行一次」的双份状态。
"""

from __future__ import annotations

from .entry import main

if __name__ == "__main__":
    raise SystemExit(main())
