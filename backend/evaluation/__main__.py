"""``python -m backend.evaluation <command>`` 的入口。

这个入口只做一件事：把 CLI 接上**一个真实装配好的** :class:`~backend.evaluation.service.EvaluationService`。
而"真实装配"在本部署里意味着：主体目录、项目目录与目标目录都**没有**来源，
因此这里选择 **fail closed** —— 不启动一个"能跑但什么都没有配置"的服务。

要跑起来必须由调用方提供配置。生产装配由 Portal BFF 负责（把 ``backend.registry``
的 deployment 目录与 ``backend.auth_linux`` 的主体目录接进来）；开发时用
``dev/evaluation_preview.py``，它装配的是一份显式标注为合成的目录。

这样做的理由：一个"谁都没有配置就启动"的评测服务，会以"看起来能用"的样子运行，
然后对每一个提交回答 404。一个拒绝启动的进程，比一个只会说 404 的进程更诚实。
"""

from __future__ import annotations

import sys

MESSAGE = (
    "backend.evaluation 没有内置的服务装配。\n"
    "  * 生产：由 Portal BFF 装配 EvaluationService（主体/项目/目标目录来自服务端可信来源）。\n"
    "  * 开发：python -m dev.evaluation_preview --help\n"
)


def run(argv: list[str] | None = None) -> int:
    # ``argv`` 被刻意忽略：无论调用方想跑哪个子命令，没有装配就无法回答任何问题。
    del argv
    sys.stderr.write(MESSAGE)
    return 3


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(run(sys.argv[1:]))
