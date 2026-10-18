"""评测中心的领域对象与状态机：EvalJob / EvalResult / MetricSample。

状态与迁移
----------
``queued -> running -> completed | failed | canceled``

迁移表是**显式**的（:data:`ALLOWED_TRANSITIONS`），不是"任何状态都能改成任何状态"。
非法迁移抛 :class:`~backend.evaluation.errors.Conflict`：一个已完成的评测不能"取消"，
一个运行中的评测不能"重试"——这些不是可以顺其自然的事，而是必须拒绝的事。

``canceled`` / ``completed`` / ``failed`` 是终态，终态**永不**离开。取消是幂等的：
对一个已经取消的任务再取消一次，返回同一个状态并标记 ``already_canceled=True``，
而不是报错，也不产生第二次状态迁移。

synthetic 没有默认值
--------------------
:class:`EvalJob` 与 :class:`EvalResult` 的 ``synthetic`` 字段**必须显式给出**。
本部署里 mock runner 是唯一能产出结果的 runner，因此它产出的每一个结果都是
``synthetic=True``。刻意不给默认值，是为了让"忘了声明"变成一个 TypeError，
而不是一个默认 ``False`` ——那会让一份合成样本在 DTO 里冒充真实评测。

测不到就是 None，不是 0
-----------------------
:class:`MetricSample` 的 ``state`` 有三个取值：``known`` / ``unknown`` /
``not_configured``。后两者 ``value`` **必须**是 ``None``。一个缺失的指标如果被写成 0，
UI 上就会显示"得分 0 分"，而真实含义是"这次评测没有产出这个指标"——
这两件事在评测语境里差别极大。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping

from .datasets import EvalSpec
from .errors import Conflict, InvalidRequest

# ---------------------------------------------------------------------------
# 状态取值域
# ---------------------------------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

JOB_STATUSES = (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELED,
)

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED})
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})

#: 显式状态机。终态是空集合——终态永不离开。
ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELED}),
    STATUS_RUNNING: frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED}),
    STATUS_COMPLETED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELED: frozenset(),
}

# 派发状态。与 job 状态**分开**：任务可以因为策略原因一直停在 queued，
# 而派发状态说明"到底有没有真的送去执行过"。
DISPATCH_NOT_DISPATCHED = "not_dispatched"
DISPATCH_DISPATCHED = "dispatched"
DISPATCH_REFUSED = "refused"
DISPATCH_STATES = (DISPATCH_NOT_DISPATCHED, DISPATCH_DISPATCHED, DISPATCH_REFUSED)

# 结果指标的状态。后两者 value 必须是 None。
METRIC_KNOWN = "known"
METRIC_UNKNOWN = "unknown"
METRIC_NOT_CONFIGURED = "not_configured"
METRIC_STATES = (METRIC_KNOWN, METRIC_UNKNOWN, METRIC_NOT_CONFIGURED)

#: 供服务层注入时钟，便于测试确定性。
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        raise InvalidRequest("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        moment = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise InvalidRequest(f"unparseable stored timestamp: {raw!r}") from exc
    return moment.replace(tzinfo=timezone.utc)


def can_transition(current: str, target: str) -> bool:
    """``current`` 能否迁移到 ``target``。未知状态一律 ``False``（fail closed）。"""
    if current not in ALLOWED_TRANSITIONS:
        return False
    return target in ALLOWED_TRANSITIONS[current]


def validate_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise Conflict(f"cannot move a job from {current!r} to {target!r}")


def validate_status(raw: object) -> str:
    if not isinstance(raw, str) or raw not in JOB_STATUSES:
        raise InvalidRequest(f"unsupported job status: {raw!r}")
    return raw


# ---------------------------------------------------------------------------
# 标识符
# ---------------------------------------------------------------------------

MAX_IDEMPOTENCY_KEY = 128

#: 前缀让 id 在日志与 URL 里一眼可辨，也便于测试断言"这不是别人的 id"。
JOB_ID_PREFIX = "evj-"
RESULT_ID_PREFIX = "evr-"


def validate_idempotency_key(raw: object) -> str | None:
    """幂等键：不透明字符串，长度受限、无控制字符。

    它只用于「同一个人、同一个项目、同一个键 -> 同一个任务」，不参与任何授权判定。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidRequest("idempotency_key must be a string")
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > MAX_IDEMPOTENCY_KEY:
        raise InvalidRequest(f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise InvalidRequest("idempotency_key contains control characters")
    return candidate


def deterministic_job_id(*, owner_principal: str, project_id: str, idempotency_key: str) -> str:
    """由 (owner, project, key) 推出稳定的 job id。

    确定性让重复提交可被识别为**同一个**任务，而不是"两个长得像的任务"。
    摘要里包含 owner 与 project，所以一个键在两个项目里不会撞成一个任务。
    """
    digest = hashlib.sha256(
        "\x1f".join((owner_principal, project_id, idempotency_key)).encode("utf-8")
    ).hexdigest()
    return JOB_ID_PREFIX + digest[:24]


def new_job_id() -> str:
    return JOB_ID_PREFIX + secrets.token_hex(12)


def new_result_id() -> str:
    return RESULT_ID_PREFIX + secrets.token_hex(12)


# ---------------------------------------------------------------------------
# 结果指标
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSample:
    """一个指标取值。

    ``state != known`` 时 ``value`` 必须是 ``None``：这是"测不到"与"等于 0"之间的
    那道防线，由构造器强制，而不是由渲染层自觉。
    """

    name: str
    state: str
    value: float | int | None = None
    unit: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise InvalidRequest("metric name must be a non-empty string")
        if self.state not in METRIC_STATES:
            raise InvalidRequest(f"unknown metric state: {self.state!r}")
        if self.state == METRIC_KNOWN:
            if self.value is None:
                raise InvalidRequest(f"metric {self.name!r} is known but has no value")
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise InvalidRequest(f"metric {self.name!r} value must be numeric")
        elif self.value is not None:
            raise InvalidRequest(
                f"metric {self.name!r} is {self.state} and must not carry a value; "
                "an unmeasured metric is not zero"
            )

    @property
    def known(self) -> bool:
        return self.state == METRIC_KNOWN

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "value": self.value,
            "unit": self.unit,
            "note": self.note,
        }


def metric_known(name: str, value: float | int, *, unit: str | None = None) -> MetricSample:
    return MetricSample(name=name, state=METRIC_KNOWN, value=value, unit=unit)


def metric_unknown(name: str, *, note: str | None = None) -> MetricSample:
    return MetricSample(name=name, state=METRIC_UNKNOWN, value=None, note=note)


def metric_not_configured(name: str, *, note: str | None = None) -> MetricSample:
    return MetricSample(name=name, state=METRIC_NOT_CONFIGURED, value=None, note=note)


# ---------------------------------------------------------------------------
# 任务与结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalTarget:
    """任务所指向的目标在**提交那一刻**的授权快照。

    刻意存快照而不是每次去目录里重查：评测报告的可复现性取决于"当时用的是谁的目标、
    以什么权限"。共享关系后来被撤回，不应改写一份已经出了结果的报告。
    """

    deployment_id: str
    project_id: str
    owner_principal: str
    shared: bool
    gpu_stress_allowed: bool
    engine: str | None = None

    def to_dict(self) -> dict:
        return {
            "deployment_id": self.deployment_id,
            "project_id": self.project_id,
            "owner": self.owner_principal,
            "shared": self.shared,
            "gpu_stress_allowed": self.gpu_stress_allowed,
            "engine": self.engine,
        }


@dataclass(frozen=True)
class EvalJob:
    """一次评测任务。"""

    job_id: str
    owner_principal: str
    owner_uid: int
    project_id: str
    spec: EvalSpec
    target: EvalTarget
    status: str
    #: mock 产出必为 True，真实 runner 产出为 False。**没有默认值**。
    synthetic: bool
    runner_kind: str
    dispatch_state: str
    created_at: datetime
    updated_at: datetime
    idempotency_key: str | None = None
    dispatch_reason: str | None = None
    cancel_requested: bool = False
    attempt: int = 1
    retry_of: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_code: str | None = None
    failure_detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.startswith(JOB_ID_PREFIX):
            raise InvalidRequest("job_id must be a job identifier")
        if not isinstance(self.synthetic, bool):
            raise InvalidRequest("synthetic must be an explicit boolean")
        if self.status not in JOB_STATUSES:
            raise InvalidRequest(f"unsupported job status: {self.status!r}")
        if self.dispatch_state not in DISPATCH_STATES:
            raise InvalidRequest(f"unsupported dispatch state: {self.dispatch_state!r}")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise InvalidRequest("attempt must be a positive integer")
        if self.retry_of is not None and not str(self.retry_of).startswith(JOB_ID_PREFIX):
            raise InvalidRequest("retry_of must be a job identifier")

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def cancellable(self) -> bool:
        """只有活跃任务可以被取消。终态没有"取消"这回事（`canceled` 已是终态）。"""
        return self.status in ACTIVE_STATUSES

    @property
    def retryable(self) -> bool:
        """终态的失败/取消可以重试；运行中与排队中的不能（那只是重复提交）。"""
        return self.status in (STATUS_FAILED, STATUS_CANCELED)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "project_id": self.project_id,
            "spec": self.spec.to_dict(),
            "target": self.target.to_dict(),
            "status": self.status,
            "terminal": self.terminal,
            "synthetic": self.synthetic,
            "runner_kind": self.runner_kind,
            "dispatch_state": self.dispatch_state,
            "dispatch_reason": self.dispatch_reason,
            "cancel_requested": self.cancel_requested,
            "attempt": self.attempt,
            "retry_of": self.retry_of,
            "idempotency_key": self.idempotency_key,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "started_at": isoformat(self.started_at),
            "finished_at": isoformat(self.finished_at),
            "failure_code": self.failure_code,
            "failure_detail": self.failure_detail,
        }


@dataclass(frozen=True)
class EvalResult:
    """一次任务的**一个版本**的结果。

    结果有版本（``version`` 从 1 递增）而不是就地覆盖：重跑产生的是新版本，
    已经发给别人的第 1 版不会被静默改写。
    """

    result_id: str
    job_id: str
    version: int
    synthetic: bool
    metrics: Mapping[str, MetricSample] = field(default_factory=dict)
    report_ref: str | None = None
    summary: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.result_id, str) or not self.result_id.startswith(RESULT_ID_PREFIX):
            raise InvalidRequest("result_id must be a result identifier")
        if not isinstance(self.job_id, str) or not self.job_id.startswith(JOB_ID_PREFIX):
            raise InvalidRequest("job_id must be a job identifier")
        if not isinstance(self.synthetic, bool):
            raise InvalidRequest("synthetic must be an explicit boolean")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise InvalidRequest("version must be a positive integer")
        for key, sample in self.metrics.items():
            if not isinstance(sample, MetricSample):
                raise InvalidRequest(f"metric {key!r} is not a MetricSample")
            if sample.name != key:
                raise InvalidRequest(f"metric key {key!r} disagrees with its sample name")

    @property
    def known_metrics(self) -> dict:
        return {k: v for k, v in self.metrics.items() if v.known}

    def to_dict(self) -> dict:
        return {
            "result_id": self.result_id,
            "job_id": self.job_id,
            "version": self.version,
            "synthetic": self.synthetic,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "report_ref": self.report_ref,
            "summary": self.summary,
            "created_at": isoformat(self.created_at),
        }


#: 结果里**允许**出现的指标名。不在这个集合里的指标一律拒绝——
#: 让"上游多送了一个字段"变成一个显式错误，而不是一个悄悄进入 DTO 的新键。
ALLOWED_METRIC_NAMES = frozenset(
    {
        "accuracy",
        "samples",
        "samples_failed",
        "throughput_rps",
        "latency_p50_ms",
        "latency_p99_ms",
        "refusal_rate",
    }
)


def build_metrics(raw: Mapping[str, object]) -> dict:
    """由原始映射构造指标集，拒绝未知指标名与非 MetricSample 值。"""
    if not isinstance(raw, Mapping):
        raise InvalidRequest("metrics must be a mapping")
    unknown = sorted(set(raw) - ALLOWED_METRIC_NAMES)
    if unknown:
        raise InvalidRequest(f"unsupported metric name(s): {', '.join(unknown)}")
    metrics = {}
    for name, sample in raw.items():
        if not isinstance(sample, MetricSample):
            raise InvalidRequest(f"metric {name!r} must be a MetricSample")
        metrics[name] = sample
    return metrics


__all__ = [
    "ACTIVE_STATUSES",
    "ALLOWED_METRIC_NAMES",
    "ALLOWED_TRANSITIONS",
    "DISPATCH_DISPATCHED",
    "DISPATCH_NOT_DISPATCHED",
    "DISPATCH_REFUSED",
    "DISPATCH_STATES",
    "JOB_ID_PREFIX",
    "JOB_STATUSES",
    "MAX_IDEMPOTENCY_KEY",
    "METRIC_KNOWN",
    "METRIC_NOT_CONFIGURED",
    "METRIC_STATES",
    "METRIC_UNKNOWN",
    "RESULT_ID_PREFIX",
    "STATUS_CANCELED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "TERMINAL_STATUSES",
    "Clock",
    "EvalJob",
    "EvalResult",
    "EvalTarget",
    "MetricSample",
    "build_metrics",
    "can_transition",
    "deterministic_job_id",
    "isoformat",
    "metric_known",
    "metric_not_configured",
    "metric_unknown",
    "new_job_id",
    "new_result_id",
    "parse_timestamp",
    "utc_now",
    "validate_idempotency_key",
    "validate_status",
    "validate_transition",
]
