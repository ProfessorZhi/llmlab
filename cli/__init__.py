"""CLI 包根。

存在理由只有一个：让 ``python3 -m cli.llmlab`` 可用（无需安装、无需网络）。
它是本任务允许的最小打包文件之一；除本文件与 ``cli/llmlab/**`` 之外，
``cli/`` 下不应出现别的内容。
"""

from __future__ import annotations
