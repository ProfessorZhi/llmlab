"""定期复查本机已登记的推理服务：让「状态」这两个字在表里站得住。

设计选择（不是纯技术问题），2026-10-27 定下：

* **默认开。** 一张只在登记那一刻探过的表，会把昨天停掉的服务一直显示成「运行中」。
  用户第一次读到它就会追问「是不是有 bug」，而这本来可以不必发生。
* **按周期探，写回状态与健康。** 代价是审计账本按固定节奏多出 ``action=reverify``
  的行，而且**故意停掉的服务会被自动标成「无法访问」**。后者不是副作用，是目的：
  平台不知道「你是有意停的」，它只知道「探不通」。
* **只探回环目标。** 见 :meth:`~backend.registry.registry.RegistryService.reverify_local_engines`。

周期与「多旧算旧」是两个数：间隔 300 s、超过 240 s 没**看见**就探。差值留给
上一轮的探测与写库，保证每轮都有活干，又不会因为差一毫秒就在同一轮里重探一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只在类型检查时引入，避免与 registry 互相 import。
    from .registry import RegistryService

#: 复查周期。300 s = 5 min：比健康结论的展示窗口（30 min）短得多，足够让表看起来
#: 是活的；又比发现循环（30 s）长一个数量级，不至于让审计账本被复查淹没。
REVERIFY_INTERVAL_SECONDS = 300.0

#: 最后**看见**它超过这么久，就该重探一次。必须小于 :data:`REVERIFY_INTERVAL_SECONDS`，
#: 否则服务可能整整一轮都轮不到。
REVERIFY_STALE_AFTER_SECONDS = 240.0


@dataclass
class EngineReverifier:
    """给 :class:`~backend.registry.ingest_scheduler.IngestScheduler` 用的一轮。

    调度器只需要一个 ``tick()``；本类把「一轮复查」定义在这里，而不是让 BFF 的
    启动代码自己拼一个 lambda —— 那样测试就得从 BFF 里捞逻辑。
    """

    service: "RegistryService"
    stale_after_seconds: float = REVERIFY_STALE_AFTER_SECONDS

    def tick(self) -> dict:
        return self.service.reverify_local_engines(
            stale_after_seconds=self.stale_after_seconds
        )
