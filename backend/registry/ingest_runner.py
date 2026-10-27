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

from .ingest import (
    ATTACH,
    DROP,
    OFFLINE_ACTION,
    RECOVER,
    IngestRound,
    TrackedService,
    ingest_round,
    service_key,
)
from .ingest_probe import probe_port, read_seen_ports
from .lab_settings import visibility_for_new_service
from .local_discovery import Fetch, loopback_fetch
from .models import utc_now
from .observed_services import attach_observed, mark_observed_offline, mark_observed_online
from .ownership import ProcNetListenerTable
from .store import Store

#: 一个端口被判「不是推理服务」之后，隔多少轮再看一眼。30 s 一轮，即 1 分钟。
DROP_COOLDOWN_ROUNDS = 2
#: 冷却上限：同一个端口反复失败时按倍数退避，最多半小时看一次。
MAX_COOLDOWN_ROUNDS = 60


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
    #: 被判「不是推理服务」的端口还能冷却几轮。状态机丢弃之后**忘记**它，下一轮
    #: 就又是一个新端口、又探三次——一个 API 服务、一个 ssh 转发口会这样被永远
    #: 反复敲。这里按指数退避记着，避免这种没有尽头的探测。
    cooldowns: dict[str, int] = field(default_factory=dict)

    def tick(self) -> IngestRound:
        ports, problem = read_seen_ports(self.table, include_wildcard=self.include_wildcard)
        self.last_problem = problem
        now = self.clock()

        known = self.directory.uids()
        probes = {}
        deferred = 0
        deadline = self.monotonic() + self.probe_budget_seconds
        for item in ports:
            # 先按 uid 过滤再探测：这些端口接下来会被 IGNORE，没必要为它们发
            # HTTP。这一条把「全机的回环监听」缩成「平台账户的监听」——在开发机
            # 上是几十个端口和两三个的区别。
            if item.uid not in known:
                continue
            if self.cooldowns.get(service_key(item.uid, item.port), 0) > 0:
                # 刚判定过「不是推理服务」。不探测 = 状态机这轮拿不到结果，端口
                # 保持 SEEN 不动，冷却结束后会再试一次。
                continue
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
            known_uids=known,
            now=now.isoformat(),
        )
        self.tracked = round_result.tracked
        self._update_cooldowns(round_result, probes)
        self._apply(round_result, now)
        return round_result

    def _update_cooldowns(self, round_result: IngestRound, probes: dict) -> None:
        """维护端口冷却：判定不是推理服务就退避，探测成功就清掉。"""

        for key in list(self.cooldowns):
            remaining = self.cooldowns[key] - 1
            if remaining > 0:
                self.cooldowns[key] = remaining
            else:
                del self.cooldowns[key]

        for action in round_result.actions:
            key = action.service.key
            if action.kind == DROP and action.reason == "连续探测失败":
                previous = self.cooldowns.get(key, 0)
                self.cooldowns[key] = min(max(previous * 2, DROP_COOLDOWN_ROUNDS), MAX_COOLDOWN_ROUNDS)
            elif probes.get(key) is not None and probes[key].ok:
                # 认出来了：之前的退避作废，别再让它拖着。
                self.cooldowns.pop(key, None)

    # -- 动作执行 ---------------------------------------------------------

    def _apply(self, round_result: IngestRound, now: datetime) -> None:
        visibility = visibility_for_new_service(self.store)

        for action in round_result.actions:
            service = action.service
            if action.kind == ATTACH:
                listener = self.directory.by_uid(service.uid)
                if listener is None:  # 目录在两轮之间变了；下一轮会 IGNORE 掉它
                    continue
                # 归属主体必须与代理上报路径（``registry.report_agent`` →
                # ``_account_for_linux_user``）给出同一个答案：有人认领这个 Linux
                # 用户的平台账户，就记在账户名下，否则才退回 Linux 主体。
                #
                # 这两条路径如果给出不同形状的主体，同一台机器上的服务就会因为
                # 「怎么被发现的」不同而分属两个名字。控制台判断「是不是我的」用的是
                # 主体字符串全等（``backend/bff/ops.py`` 的 ``owner_is_self``、
                # ``backend/registry/authz.py`` 的可见性），而扫描路径拿到的是
                # ``linux:<host>:<uid>``，会话主体是 ``account:<id>`` —— 永远不相等。
                # 结果是本人看不见自己的服务，更发布不出去。
                owner = self.directory.account_for_linux_user(listener.linux_user) or listener
                attach_observed(
                    self.store,
                    uid=service.uid,
                    principal=owner.principal,
                    owner_label=listener.linux_user,
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
