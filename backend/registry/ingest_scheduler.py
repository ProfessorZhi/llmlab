"""§4.1 的那句「每 30 s」本身。

:class:`~backend.registry.ingest_runner.IngestRunner` 是一轮；这里负责按周期
反复调用它。分开写是因为两者的失败方式不同：

* 一轮出错（磁盘满、表读不了、某条 SQL 卡住）**不能**让整个循环退出——那会
  让「自动接入」在无人察觉的情况下永久停摆。所以每轮都捕获异常、记下来、
  下一轮照常。
* 循环要能立刻停。``threading.Event`` 而不是 ``time.sleep``：Ctrl-C 或测试结束
  时不用干等最多 30 s。

这个类**不自建线程**，除非调用方显式调用 :meth:`start`。测试与前台进程都可以
直接 ``run_once``，不引入后台执行。
"""

from __future__ import annotations

import sys
import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable

from .ingest import IngestRound
from .ingest_runner import IngestRunner

#: 设计文档 §4.1 的周期。不是可调参数——「30 s 内出现」是验收条件。
DEFAULT_INTERVAL_SECONDS = 30.0


def _report_to_stderr(detail: str) -> None:
    """缺省的错误出口：整段堆栈进 stderr。

    刻意**不**静默：一个每 30 s 失败一次、却什么都不说的守护进程，看起来和
    一个正常运行的一模一样。
    """

    print(detail, file=sys.stderr, end="" if detail.endswith("\n") else "\n")


@dataclass
class IngestScheduler:
    runner: IngestRunner
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    stop: threading.Event = field(default_factory=threading.Event)
    #: 上一轮的结果，给管理接口/日志读。
    last_round: IngestRound | None = None
    #: 最近一次失败。成功一轮**不清空**它：一个反复失败又偶尔成功的循环，
    #: 比一个一直失败的更需要被看见。
    last_error: str | None = None
    rounds: int = 0
    failures: int = 0
    thread: threading.Thread | None = None
    #: 出错时的出口。测试里传一个空函数，生产里用缺省值。
    on_error: Callable[[str], None] = _report_to_stderr

    def run_once(self) -> IngestRound | None:
        """跑一轮。异常不外抛，但一定记在 :attr:`last_error` 里。"""

        try:
            round_result = self.runner.tick()
        except Exception as exc:  # noqa: BLE001 - 循环必须活下去
            self.failures += 1
            # 完整堆栈给日志；``last_error`` 只留一行给接口。
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.on_error(traceback.format_exc())
            return None
        self.rounds += 1
        self.last_round = round_result
        return round_result

    def run_forever(self) -> None:
        """前台循环，直到 :attr:`stop` 被置位。

        先跑一轮再等：守护进程刚起来时不该先静默 30 s。
        """

        while not self.stop.is_set():
            self.run_once()
            if self.stop.wait(self.interval_seconds):
                return

    def start(self, *, name: str = "llmlab-ingest", daemon: bool = True) -> threading.Thread:
        """在一个线程里跑 :meth:`run_forever`。线程标记为 daemon，进程退出不等它。"""

        if self.thread is not None and self.thread.is_alive():
            return self.thread
        self.stop.clear()
        self.thread = threading.Thread(target=self.run_forever, name=name, daemon=daemon)
        self.thread.start()
        return self.thread

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """置位停止标志并等线程退出。可以重复调用。"""

        self.stop.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self.thread = None
