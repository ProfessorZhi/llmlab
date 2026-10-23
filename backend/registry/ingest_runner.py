"""把 §4.1 的三块拼起来：读表 → 探测 → 状态机 → 账本。

守护进程每 30 s 调一次 :meth:`IngestRunner.tick`。这里唯一有状态的东西是
``tracked``（跨轮次的跟踪表），它只活在内存里：守护进程重启后第一轮会把所有
在监听的端口重新认一遍，最坏情况是多花 30 s，而不是从账本里丢东西。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .ingest import ATTACH, OFFLINE_ACTION, RECOVER, IngestRound, TrackedService, ingest_round, service_key
from .ingest_probe import probe_port, read_seen_ports
from .lab_settings import visibility_for_new_service
from .local_discovery import Fetch, loopback_fetch
from .models import utc_now
from .observed_services import attach_observed, mark_observed_offline, mark_observed_online
from .ownership import ProcNetListenerTable
from .store import Store


@dataclass
class IngestRunner:
    """一轮自动接入所需的全部依赖，都能替换（测试里就是假表 + 假 fetch）。"""

    store: Store
    directory: object  # PrincipalDirectory；这里只用到 by_uid / uids
    table: object = field(default_factory=ProcNetListenerTable)
    fetch: Fetch = field(default_factory=lambda: loopback_fetch(1.5))
    #: 一轮最多花多少秒在探测上。端口多、又都连不上时，宁可这一轮少探几个，
    #: 也不能让 30 s 的周期变成一个跑不完的长任务。
    probe_budget_seconds: float = 20.0
    include_wildcard: bool = False
    clock: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    tracked: dict[str, TrackedService] = field(default_factory=dict)
    #: 本轮的读表问题（``proc_net_unreadable`` 等），上报用。
    last_problem: str | None = None
    #: 本轮因为预算没轮到的端口数，下一轮优先（顺序由端口号决定，天然公平）。
    deferred_probes: int = 0

    def tick(self) -> IngestRound:
        ports, problem = read_seen_ports(self.table, include_wildcard=self.include_wildcard)
        self.last_problem = problem
        now = self.clock()

        probes = {}
        deferred = 0
        deadline = self.monotonic() + self.probe_budget_seconds
        for item in ports:
            if self.monotonic() >= deadline:
                # 预算用完：这一轮不探。状态机把「没有探测结果」当作「没探测」，
                # 不会因此累计失败次数。
                deferred += 1
                continue
            probes[service_key(item.uid, item.port)] = probe_port(self.fetch, item.port)
        self.deferred_probes = deferred

        round_result = ingest_round(
            self.tracked,
            ports,
            probes,
            known_uids=self.directory.uids(),
            now=now.isoformat(),
        )
        self.tracked = round_result.tracked
        self._apply(round_result, now)
        return round_result

    # -- 动作执行 ---------------------------------------------------------

    def _apply(self, round_result: IngestRound, now: datetime) -> None:
        visibility = visibility_for_new_service(self.store)

        for action in round_result.actions:
            service = action.service
            if action.kind == ATTACH:
                principal = self.directory.by_uid(service.uid)
                if principal is None:  # 目录在两轮之间变了；下一轮会 IGNORE 掉它
                    continue
                attach_observed(
                    self.store,
                    uid=service.uid,
                    principal=principal.principal,
                    port=service.port,
                    framework=service.framework,
                    model_ids=service.model_ids,
                    visibility=visibility,
                    now=now,
                )
            elif action.kind == RECOVER:
                mark_observed_online(self.store, uid=service.uid, port=service.port, now=now)
            elif action.kind == OFFLINE_ACTION:
                mark_observed_offline(self.store, uid=service.uid, port=service.port, now=now)
            # OBSERVE / DROP / IGNORE 不写账本：DROP 说的是「它从来就没进过账本」。
