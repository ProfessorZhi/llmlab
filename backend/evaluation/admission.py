"""GPU 准入：真实派发之前的最后一道闸门。

本部署的 GPU 已满，因此**这一轮的答案永远是否定的**。这个模块存在的意义不是"将来会
打开"（那由运维决定），而是让"此刻为什么不能跑"变成一个**可以展示、可以测试、可以审计**
的事实，而不是一句"暂无数据"。

准入是**合取**，不是任一
----------------------

以下每一条都必须成立；任何一条不成立就拒绝派发：

1. ``policy.dispatch_enabled`` —— 管理员在服务端显式打开。**不是**请求里的字段，
   也不是某个 scope 能换来的东西。
2. ``facts.real_runner_available`` —— 真的存在一个可用的 EvalScope runner。
3. ``facts.adapter_version`` 不是 ``None`` —— 版本探测通过。版本未知时**不猜**：
   一个"大概是 1.x"的 runner 产出的报告没有可复现性可言。
4. 当前时刻在 ``policy.window`` 内 —— 运维决定的时间窗。
5. ``gpu.observed`` —— GPU 事实**被观测过**。没观测过就是 ``not_configured``，
   不是"显存充足"。
6. ``gpu.free_mib >= policy.min_free_mib`` —— 真的有富余显存。

为什么把「没观测到 GPU」与「显存不足」分开
------------------------------------------
二者都是拒绝，但原因不同、修复动作也不同：前者是"把只读探测接上"，后者是"等 GPU 空出来"。
把 ``未观测`` 当成 ``0`` 或当成 ``充足`` 都会让运维看到一句错误的解释。``GpuFacts`` 在
构造时就强制：``observed=False`` 时 ``free_mib`` / ``total_mib`` 必须是 ``None``。

synthetic_only
--------------
:class:`AdmissionDecision` 带一个 ``synthetic_only``：当真实派发不被允许时，本部署
**只能**产出合成结果。这个布尔量是 DTO 里 ``synthetic`` 的上游依据，也是 UI 上那句
"这不是评测失败，也不代表已有模型得分为 0"的来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .errors import InvalidRequest
from .limits import TimeWindow

# 拒绝原因。稳定字符串，供审计/UI/测试断言，不参与判定。
REASON_ADMIN_DISPATCH_DISABLED = "admin_dispatch_disabled"
REASON_DISPATCH_SCOPE_NOT_GRANTED = "dispatch_scope_not_granted"
REASON_REAL_RUNNER_NOT_AVAILABLE = "real_runner_not_available"
REASON_ADAPTER_VERSION_UNKNOWN = "adapter_version_unknown"
REASON_OUTSIDE_TIME_WINDOW = "outside_time_window"
REASON_GPU_FACTS_NOT_OBSERVED = "gpu_facts_not_observed"
REASON_INSUFFICIENT_GPU_HEADROOM = "insufficient_gpu_headroom"

#: GPU 事实的状态取值域。``not_configured`` 是"没接上只读探测"，``unknown`` 是
#: "探测过但拿不到数"。两者都不是 0。
GPU_KNOWN = "known"
GPU_UNKNOWN = "unknown"
GPU_NOT_CONFIGURED = "not_configured"
GPU_STATES = (GPU_KNOWN, GPU_UNKNOWN, GPU_NOT_CONFIGURED)

#: 派发要求的最低空闲显存。默认 8 GiB：一个评测 runner 的入场券。
DEFAULT_MIN_FREE_MIB = 8192


@dataclass(frozen=True)
class GpuFacts:
    """一次**只读**观测的结果。

    本模块**不**去读 GPU：没有 NVML 调用、没有 ``nvidia-smi``、没有子进程。事实由
    调用方（运维接线或测试替身）注入。这样"评测中心不碰 GPU"是一条结构性事实，
    而不是一条纪律。
    """

    observed: bool
    free_mib: int | None = None
    total_mib: int | None = None
    source: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observed, bool):
            raise InvalidRequest("observed must be a boolean")
        for name, value in (("free_mib", self.free_mib), ("total_mib", self.total_mib)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidRequest(f"{name} must be a non-negative integer or None")
        if not self.observed and (self.free_mib is not None or self.total_mib is not None):
            # 未观测却带着数字，是"把没测到的东西写成 0"的第一步。堵死在这里。
            raise InvalidRequest("unobserved GPU facts must not carry numbers")
        if self.observed and (self.free_mib is None or self.total_mib is None):
            # ``observed=True`` 却**没有**数字是自相矛盾的事实。留着它会开一个洞：
            # 准入里"显存够不够"那一条会因为 ``free_mib is None`` 而被跳过，
            # 于是一份什么都没测到的观测反而能让 GPU 闸门放行。
            raise InvalidRequest("observed GPU facts must carry both free and total memory")

    @property
    def state(self) -> str:
        if self.observed:
            return GPU_KNOWN
        if self.source is None:
            return GPU_NOT_CONFIGURED
        return GPU_UNKNOWN

    @classmethod
    def not_configured(cls, *, note: str | None = None) -> "GpuFacts":
        """本部署的默认事实：没有接上只读探测。"""
        return cls(
            observed=False,
            source=None,
            note=note or "本部署没有接线到只读 GPU 观测来源；显存占用未知，不是 0。",
        )

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "observed": self.observed,
            "free_mib": self.free_mib,
            "total_mib": self.total_mib,
            "source": self.source,
            "note": self.note,
        }


@dataclass(frozen=True)
class RunnerFacts:
    """真实 runner 的可用性事实（由 ``runner.py`` 的能力探测产出）。"""

    real_runner_available: bool
    adapter_kind: str | None = None
    adapter_version: str | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.real_runner_available, bool):
            raise InvalidRequest("real_runner_available must be a boolean")
        if self.adapter_version is not None and not isinstance(self.adapter_version, str):
            raise InvalidRequest("adapter_version must be a string or None")

    @classmethod
    def unavailable(cls, *, reason: str) -> "RunnerFacts":
        return cls(real_runner_available=False, reasons=(reason,))

    def to_dict(self) -> dict:
        return {
            "real_runner_available": self.real_runner_available,
            "adapter_kind": self.adapter_kind,
            "adapter_version": self.adapter_version,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class AdmissionPolicy:
    """准入策略。全部来自服务端配置，**没有**一项来自请求。"""

    dispatch_enabled: bool = False
    require_real_runner: bool = True
    min_free_mib: int = DEFAULT_MIN_FREE_MIB
    window: TimeWindow = TimeWindow(start_minute=0, end_minute=0)
    label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dispatch_enabled, bool):
            raise InvalidRequest("dispatch_enabled must be a boolean")
        if not isinstance(self.require_real_runner, bool):
            raise InvalidRequest("require_real_runner must be a boolean")
        if isinstance(self.min_free_mib, bool) or not isinstance(self.min_free_mib, int):
            raise InvalidRequest("min_free_mib must be an integer")
        if self.min_free_mib < 0:
            raise InvalidRequest("min_free_mib must not be negative")
        if not isinstance(self.window, TimeWindow):
            raise InvalidRequest("window must be a TimeWindow")

    @classmethod
    def closed(cls, *, label: str | None = None) -> "AdmissionPolicy":
        """本部署的默认策略：管理员未开、时间窗每天关闭。

        两个独立的闸门都关着，是刻意的：将来运维想临时开一次派发，必须先想清楚
        "我是要打开管理员开关，还是要打开时间窗"，而不是发现"居然有一个开关一开就通了"。
        """
        return cls(
            dispatch_enabled=False,
            require_real_runner=True,
            window=TimeWindow.closed(),
            label=label or "本部署不派发真实评测（GPU 已满）",
        )

    def to_dict(self) -> dict:
        return {
            "dispatch_enabled": self.dispatch_enabled,
            "require_real_runner": self.require_real_runner,
            "min_free_mib": self.min_free_mib,
            "window": self.window.to_dict(),
            "label": self.label,
        }


@dataclass(frozen=True)
class AdmissionDecision:
    """一次准入判定的完整结果。

    ``reasons`` 列出**全部**不成立的条件（而不是只有第一个）：运维需要的是
    "要让它跑起来，得改哪几件事"，不是一个逐个试错的清单。
    """

    allowed: bool
    reason: str | None
    reasons: tuple[str, ...]
    gpu: GpuFacts
    runner: RunnerFacts
    window_open: bool
    window: TimeWindow
    synthetic_only: bool

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "gpu": self.gpu.to_dict(),
            "runner": self.runner.to_dict(),
            "window_open": self.window_open,
            "window": self.window.to_dict(),
            "synthetic_only": self.synthetic_only,
        }


def evaluate_admission(
    policy: AdmissionPolicy,
    *,
    runner: RunnerFacts,
    gpu: GpuFacts,
    now: datetime,
    dispatch_scope_granted: bool = False,
) -> AdmissionDecision:
    """判定此刻能否派发到真实 runner。

    全部条件都必须成立。``reason`` 是第一个不成立的条件（给 UI 一句话），
    ``reasons`` 是全部（给运维一张清单）。

    ``dispatch_scope_granted`` 是**主体级**的那一道：真实派发会占用 GPU，是一个比
    "提交一个排队任务"重得多的决定，因此它需要一个单独的
    ``evaluation:dispatch:real``。默认为 ``False`` —— 一个没声明自己持有它的调用者
    不应该被当作持有它。
    """
    if not isinstance(policy, AdmissionPolicy):
        raise InvalidRequest("policy must be an AdmissionPolicy")
    if not isinstance(runner, RunnerFacts):
        raise InvalidRequest("runner must be RunnerFacts")
    if not isinstance(gpu, GpuFacts):
        raise InvalidRequest("gpu must be GpuFacts")
    if not isinstance(dispatch_scope_granted, bool):
        raise InvalidRequest("dispatch_scope_granted must be a boolean")

    window_open = policy.window.contains(now)

    failures: list[str] = []
    if not policy.dispatch_enabled:
        failures.append(REASON_ADMIN_DISPATCH_DISABLED)
    if not dispatch_scope_granted:
        failures.append(REASON_DISPATCH_SCOPE_NOT_GRANTED)
    if policy.require_real_runner and not runner.real_runner_available:
        failures.append(REASON_REAL_RUNNER_NOT_AVAILABLE)
    if runner.adapter_version is None:
        failures.append(REASON_ADAPTER_VERSION_UNKNOWN)
    if not window_open:
        failures.append(REASON_OUTSIDE_TIME_WINDOW)
    if not gpu.observed or gpu.free_mib is None:
        # ``GpuFacts`` 已经保证 observed 时一定有数字；这里**再**判一次是有意的：
        # 准入是最后一道闸门，"显存够不够"这一条不能因为一个 None 而被跳过。
        failures.append(REASON_GPU_FACTS_NOT_OBSERVED)
    elif gpu.free_mib < policy.min_free_mib:
        failures.append(REASON_INSUFFICIENT_GPU_HEADROOM)

    allowed = not failures
    return AdmissionDecision(
        allowed=allowed,
        reason=failures[0] if failures else None,
        reasons=tuple(failures),
        gpu=gpu,
        runner=runner,
        window_open=window_open,
        window=policy.window,
        # 真实派发不被允许时，本部署只能产出合成结果。
        synthetic_only=not allowed,
    )


def all_reasons() -> tuple[str, ...]:
    """全部拒绝原因，供自检与文档一致性测试。"""
    return (
        REASON_ADMIN_DISPATCH_DISABLED,
        REASON_DISPATCH_SCOPE_NOT_GRANTED,
        REASON_REAL_RUNNER_NOT_AVAILABLE,
        REASON_ADAPTER_VERSION_UNKNOWN,
        REASON_OUTSIDE_TIME_WINDOW,
        REASON_GPU_FACTS_NOT_OBSERVED,
        REASON_INSUFFICIENT_GPU_HEADROOM,
    )


def reason_is_known(reason: str) -> bool:
    return reason in all_reasons()


def summarize_reasons(reasons: Iterable[str]) -> list[str]:
    """把拒绝原因按固定顺序排好，便于逐字节比对与 UI 稳定展示。"""
    order = {reason: index for index, reason in enumerate(all_reasons())}
    return sorted(set(reasons), key=lambda r: order.get(r, len(order)))


__all__ = [
    "DEFAULT_MIN_FREE_MIB",
    "GPU_KNOWN",
    "GPU_NOT_CONFIGURED",
    "GPU_STATES",
    "GPU_UNKNOWN",
    "REASON_ADAPTER_VERSION_UNKNOWN",
    "REASON_ADMIN_DISPATCH_DISABLED",
    "REASON_DISPATCH_SCOPE_NOT_GRANTED",
    "REASON_GPU_FACTS_NOT_OBSERVED",
    "REASON_INSUFFICIENT_GPU_HEADROOM",
    "REASON_OUTSIDE_TIME_WINDOW",
    "REASON_REAL_RUNNER_NOT_AVAILABLE",
    "AdmissionDecision",
    "AdmissionPolicy",
    "GpuFacts",
    "RunnerFacts",
    "all_reasons",
    "evaluate_admission",
    "reason_is_known",
    "summarize_reasons",
]
