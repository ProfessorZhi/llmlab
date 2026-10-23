"""服务自动接入的状态机（设计文档 §4.1）。

这里是**纯函数**：给定上一轮跟踪到的服务、这一轮看到的回环监听端口和探测结果，
算出这一轮该做什么，并返回下一轮的跟踪表。它不读 ``/proc``、不发请求、不写
账本——那些都是调用方的事。这样状态机的每条分支都能在测试里精确复现，而不必
真的起停一个推理进程。

为什么要一个显式状态机而不是「每轮重新登记」：端口的出现与消失是抖动的事实，
而账本里的服务是稳定的身份。连续两轮不见才下 ``offline``、连续三次探测失败才
丢弃，都是为了不让一次 GC 停顿或一次慢启动把服务从用户的界面上抹掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import AbstractSet, Iterable, Mapping, Sequence

#: 端口连续缺席这么多轮 ⇒ ``offline``。30 s 一轮，即 60 s。
OFFLINE_AFTER_MISSES = 2

#: ``seen`` 状态连续探测失败这么多次 ⇒ 判断它不是推理服务，丢弃。
DROP_AFTER_PROBE_FAILURES = 3

#: 跟踪状态。``seen`` = 端口在监听、还没被认出是 OpenAI 兼容服务。
SEEN = "seen"
ATTACHED = "attached"
OFFLINE = "offline"

#: 这一轮要执行的动作。
ATTACH = "attach"      # 新服务接入（写账本 + 可能挂路由）
RECOVER = "recover"    # offline 的服务回来了，路由恢复
OFFLINE_ACTION = "offline"  # 服务不在了，路由保留、调用 503
DROP = "drop"          # 从未接入的端口消失，或连续探测失败：不入账本
OBSERVE = "observe"    # 端口还在，但这一轮探测没成功：保持原状态、继续计数
IGNORE = "ignore"      # uid 不在平台账户映射里


def service_key(uid: int, port: int) -> str:
    """服务的身份就是 (uid, port)。

    不用 pid：进程重启会换 pid，端口和所属用户才是用户眼里「这个服务」。
    """

    return f"{uid}:{port}"


@dataclass(frozen=True)
class SeenPort:
    """``/proc/net/tcp{,6}`` 里一个处于 LISTEN 的回环端口。"""

    uid: int
    port: int
    host: str = "127.0.0.1"


@dataclass(frozen=True)
class ProbeOutcome:
    """对一个端口做的只读探测（§4.1 第 3 步）。

    ``ok=False`` 表示探测失败或超时；``model_ids`` 为空但 ``ok=True`` 也算成功
    ——一个还没加载模型、但确实是 OpenAI 兼容的服务同样值得接入。
    """

    ok: bool
    framework: str | None = None
    model_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class TrackedService:
    """跨轮次保留的跟踪状态。"""

    uid: int
    port: int
    state: str
    framework: str | None = None
    model_ids: tuple[str, ...] = ()
    #: 连续缺席轮数（只有 attached 关心它）。
    misses: int = 0
    #: 连续探测失败次数（只有 seen 关心它）。
    probe_failures: int = 0
    #: 第一次被看到的时间，仅供展示。
    first_seen_at: str | None = None

    @property
    def key(self) -> str:
        return service_key(self.uid, self.port)


@dataclass(frozen=True)
class Action:
    """一条要由调用方执行的动作。"""

    kind: str
    service: TrackedService
    reason: str = ""
    #: ``attach`` / ``recover`` 时用于建路由的模型 id（已规范化前的原值）。
    model_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "uid": self.service.uid,
            "port": self.service.port,
            "state_from": self.service.state,
            "reason": self.reason,
            "model_ids": list(self.model_ids),
        }


@dataclass
class IngestRound:
    """一轮的结果：要执行的动作 + 下一轮的跟踪表。"""

    actions: list[Action] = field(default_factory=list)
    tracked: dict[str, TrackedService] = field(default_factory=dict)

    def of_kind(self, kind: str) -> list[Action]:
        return [action for action in self.actions if action.kind == kind]


def ingest_round(
    tracked: Mapping[str, TrackedService],
    seen: Iterable[SeenPort],
    probes: Mapping[str, ProbeOutcome],
    *,
    known_uids: AbstractSet[int],
    now: str,
) -> IngestRound:
    """推进一轮状态机。

    ``probes`` 用 ``service_key`` 索引；没给探测结果的端口按「这一轮没探测」处理，
    既不推进也不回退——慢一拍的探测不该被当成失败。
    """

    seen_by_key = {service_key(item.uid, item.port): item for item in seen}
    round_result = IngestRound()

    # 1. 先处理已在跟踪的服务：缺席、探测失败、恢复。
    for key, service in tracked.items():
        observed = seen_by_key.get(key)
        probe = probes.get(key)

        if service.state == ATTACHED:
            if observed is None:
                misses = service.misses + 1
                if misses >= OFFLINE_AFTER_MISSES:
                    offline = replace(service, state=OFFLINE, misses=0)
                    round_result.tracked[key] = offline
                    round_result.actions.append(
                        Action(OFFLINE_ACTION, offline, reason="端口连续缺席")
                    )
                else:
                    round_result.tracked[key] = replace(service, misses=misses)
                    round_result.actions.append(
                        Action(OBSERVE, service, reason="本轮未见端口，尚未达到离线阈值")
                    )
                continue

            # 端口还在。探测失败不代表服务挂了——健康检查在 health.py 里，
            # 这里只关心「还在不在」。模型清单以本轮探测到的为准。
            model_ids = probe.model_ids if probe and probe.ok else service.model_ids
            framework = (probe.framework if probe and probe.ok else None) or service.framework
            updated = replace(service, misses=0, model_ids=model_ids, framework=framework)
            round_result.tracked[key] = updated
            continue

        if service.state == OFFLINE:
            if observed is None:
                round_result.tracked[key] = service
                round_result.actions.append(Action(OBSERVE, service, reason="仍未出现"))
                continue
            if probe is not None and probe.ok:
                same_model = (not service.model_ids) or (probe.model_ids == service.model_ids)
                if same_model:
                    recovered = replace(
                        service,
                        state=ATTACHED,
                        misses=0,
                        probe_failures=0,
                        framework=probe.framework or service.framework,
                        model_ids=probe.model_ids or service.model_ids,
                    )
                    round_result.tracked[key] = recovered
                    round_result.actions.append(
                        Action(RECOVER, recovered, reason="同端口同模型重新出现", model_ids=recovered.model_ids)
                    )
                    continue
                # 换了模型的同一个端口：旧身份作废，按新接入处理。
                round_result.tracked.pop(key, None)
                fresh = TrackedService(
                    uid=service.uid,
                    port=service.port,
                    state=ATTACHED,
                    framework=probe.framework,
                    model_ids=probe.model_ids,
                    first_seen_at=service.first_seen_at,
                )
                round_result.tracked[key] = fresh
                round_result.actions.append(
                    Action(ATTACH, fresh, reason="端口复用但模型已更换", model_ids=probe.model_ids)
                )
                continue
            round_result.tracked[key] = service
            round_result.actions.append(Action(OBSERVE, service, reason="出现了但探测未成功"))
            continue

        # state == SEEN
        if observed is None:
            # 从来没接进去过的端口消失了：直接忘记，不留一条空记录。
            round_result.actions.append(Action(DROP, service, reason="未接入的端口消失"))
            continue
        if probe is None:
            round_result.tracked[key] = service
            round_result.actions.append(Action(OBSERVE, service, reason="本轮无探测结果"))
            continue
        if probe.ok:
            attached = replace(
                service,
                state=ATTACHED,
                probe_failures=0,
                framework=probe.framework,
                model_ids=probe.model_ids,
            )
            round_result.tracked[key] = attached
            round_result.actions.append(
                Action(ATTACH, attached, reason="探测成功", model_ids=probe.model_ids)
            )
            continue
        failures = service.probe_failures + 1
        if failures >= DROP_AFTER_PROBE_FAILURES:
            round_result.actions.append(
                Action(DROP, replace(service, probe_failures=failures), reason="连续探测失败")
            )
            continue
        round_result.tracked[key] = replace(service, probe_failures=failures)
        round_result.actions.append(
            Action(OBSERVE, replace(service, probe_failures=failures), reason=probe.detail or "探测失败")
        )

    # 2. 再处理这一轮新出现的端口。
    for key, observed in seen_by_key.items():
        if key in tracked:
            continue
        if observed.uid not in known_uids:
            round_result.actions.append(
                Action(
                    IGNORE,
                    TrackedService(uid=observed.uid, port=observed.port, state=SEEN),
                    reason="uid 不在平台账户映射里",
                )
            )
            continue
        probe = probes.get(key)
        if probe is not None and probe.ok:
            service = TrackedService(
                uid=observed.uid,
                port=observed.port,
                state=ATTACHED,
                framework=probe.framework,
                model_ids=probe.model_ids,
                first_seen_at=now,
            )
            round_result.tracked[key] = service
            round_result.actions.append(
                Action(ATTACH, service, reason="新端口探测成功", model_ids=probe.model_ids)
            )
            continue
        service = TrackedService(
            uid=observed.uid,
            port=observed.port,
            state=SEEN,
            probe_failures=0 if probe is None else 1,
            first_seen_at=now,
        )
        round_result.tracked[key] = service
        round_result.actions.append(
            Action(
                OBSERVE,
                service,
                reason="新端口，等待探测" if probe is None else (probe.detail or "探测失败"),
            )
        )

    return round_result


def normalize_model_id(raw: str) -> str | None:
    """§4.2 的路由名规范化。

    只保留 ``[A-Za-z0-9._:/-]``；规范化后为空则返回 ``None``（这个模型 id 不能
    单独构成路由名，调用方需要追加 ``@<用户名>`` 或改用其他名字）。
    """

    cleaned = "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch in "._:/-"))
    cleaned = cleaned.strip("/").strip()
    return cleaned or None


def route_name_for(model_id: str, owner: str, taken: AbstractSet[str]) -> str:
    """给一个模型挑路由名：优先用模型 id，冲突时追加 ``@<owner>``。

    ``taken`` 是已被占用的路由名（其他服务正在用的）。同一 owner 的同一模型重复
    调用会稳定地得到同一个名字——自动接入不能每轮换一个路由。
    """

    base = normalize_model_id(model_id) or "model"
    if base not in taken:
        return base
    candidate = f"{base}@{owner}"
    if candidate not in taken:
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in taken:
        suffix += 1
    return f"{candidate}-{suffix}"


def merge_route_names(existing: Sequence[str], wanted: Sequence[str]) -> list[str]:
    """本轮应有的路由名，保持已存在者的顺序与大小，只补新增的。"""

    result = list(existing)
    for name in wanted:
        if name not in result:
            result.append(name)
    return result
