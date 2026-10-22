"""``/api/infra/summary`` 投影契约的**服务端严格校验**。

背景
----

Dell 主机上的**只读** Prometheus 或 NVIDIA NVML 事实投影成一个
固定形状的小契约；浏览器端 ``web/js/infra/host-status.js`` 只在三条同时成立时，才把
数字画成 LIVE：

    source in {"prometheus-readonly", "nvml-verified"} && version === 1 && synthetic === false

那是一条**客户端**防线。一个被中间人改写、由旧版本工具生成、或者干脆伪造的响应，
只要能过浏览器那一行 ``if``，就会被画成 LIVE。服务端在把投影下发给浏览器之前必须
自己再判一次 —— 而且判得比前端更严。

本模块把那三条「信任门槛」从一段写在渲染函数里的 ``if``，变成一个**有名字、可测试、
可被 BFF 复用**的校验器。

不变式（每一条都有回归）
------------------------

* **``synthetic`` 必须是 JSON ``false``** —— 不是 ``0``、不是 ``"false"``、不是缺省。
  合成数据永远不得进入 LIVE 分支：一个合成数字被画成实测值，正是本仓库一路拒绝的事。
* **``source`` 必须逐字匹配受信白名单**，``readonly`` 必须为 ``true``，
  ``version`` 必须为 ``1``。任何一条不成立，整份响应的**每一个**指标都降级为 UNKNOWN
  —— 不是「只降级出错的那个字段」：一份来路不明信封里的其它字段同样不可信。
* **``sample_at`` 是信任的充要条件。** ``state == "known"`` 的指标必须带**带时区**的
  ISO-8601 ``sample_at``，且不得晚于「现在 + 容差」（未来时间是时钟偏斜，不是「很新」），
  不得早于「现在 - 阈值」（过期样本不许冒充当前读数）。缺失 / naive / 未来 / 过期四种
  情形一律 UNKNOWN，并各自给出稳定原因码。**缺测不是 0**：校验器从不把「没有样本」
  渲染成数值。
* **``unit`` 必须与该键的定义匹配。** ``gpu_temperature_celsius`` 报 ``ratio`` 是数据
  错位，不是风格问题 —— 一个把摄氏度印成百分比的面板比一个空面板更危险。

失败是**封闭**的（fail-closed）：校验器只接受它明确认识且逐条通过的东西。任何异常形状、
任何未知 ``state``、任何非有限数，结局都是 ``live=False`` + 原因码，绝不「放行并当作
已知」。

本模块**不做 IO、不联网**：输入就是一份已经解析好的 JSON 对象。它给 BFF、给报告自检、
给 UI Lane 的 JS 测试提供**同一套边界条件**（见 ``docs/observability-contract.md`` §16）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

__all__ = [
    "DEFAULT_FUTURE_TOLERANCE_S",
    "DEFAULT_MAX_SAMPLE_AGE_S",
    "HOST_METRIC_UNITS",
    "HOST_METRIC_OPTIONAL_UNITS",
    "PROJECTION_PATH",
    "PROJECTION_SOURCE",
    "PROJECTION_SOURCES",
    "PROJECTION_VERSION",
    "REJECT_BAD_OBSERVED_AT",
    "REJECT_BAD_SAMPLE_AT",
    "REJECT_BAD_SHAPE",
    "REJECT_BAD_STATE_VALUE",
    "REJECT_FUTURE_OBSERVED_AT",
    "REJECT_FUTURE_SAMPLE_AT",
    "REJECT_MISSING_SAMPLE_AT",
    "REJECT_NON_FINITE_VALUE",
    "REJECT_NOT_KNOWN_STATE",
    "REJECT_NOT_READONLY",
    "REJECT_STALE_SAMPLE_AT",
    "REJECT_SYNTHETIC_SOURCE",
    "REJECT_UNKNOWN_METRIC_KEY",
    "REJECT_UNTRUSTED_SOURCE",
    "REJECT_WRONG_VERSION",
    "STATE_LIVE",
    "STATE_UNKNOWN",
    "UPSTREAM_REASONS",
    "MetricVerdict",
    "ServiceVerdict",
    "HostSummaryVerdict",
    "parse_instant",
    "validate_host_summary",
]

#: 投影端点。校验器不认识别的路径 —— 契约只有一个。
PROJECTION_PATH = "/api/infra/summary"

#: 受信来源逐字比较，不做大小写/空白归一化。
PROJECTION_SOURCE = "prometheus-readonly"
PROJECTION_SOURCES = frozenset({PROJECTION_SOURCE, "nvml-verified"})

#: 唯一被信任的契约版本。
PROJECTION_VERSION = 1

#: 样本最大年龄（秒）。与 ``verified-infra.mjs`` 的 ``MAX_SAMPLE_AGE_S`` 同值；
#: 它同时也是服务端 ``HostReport`` 的 stale 阈值口径。**服务端不放松这个数**。
DEFAULT_MAX_SAMPLE_AGE_S = 90

#: 允许样本时间领先「现在」的容差（秒）。Prometheus 的时间戳可以略微领先采集点，
#: 但那是有界的：超过它就是时钟偏斜，而不是「更新的一笔」。
DEFAULT_FUTURE_TOLERANCE_S = 15

#: 键 -> 单位。这是**唯一**真源：投影端（``verified-infra.mjs`` 的 ``METRICS``）
#: 与浏览器端（``host-status.js`` 的 ``UNITS``）都必须与它一致。
HOST_METRIC_UNITS: Mapping[str, str] = MappingProxyType(
    {
        "gpu_utilization_ratio": "ratio",
        "gpu_temperature_celsius": "celsius",
        "gpu_power_watts": "watts",
        "strata_up": "bool",
        "gpu_memory_used_mib": "mib",
        "gpu_memory_total_mib": "mib",
    }
)

# Optional gauges are emitted by direct NVML readers. Their absence does not
# invalidate older Prometheus projections, but a present value is validated
# under the same freshness and unit rules as the required host metrics.
HOST_METRIC_OPTIONAL_UNITS: Mapping[str, str] = MappingProxyType(
    {
        "gpu_memory_free_mib": "mib",
        "gpu_memory_reserved_mib": "mib",
    }
)

#: 投影端会随 UNKNOWN 一起给出的上游原因码。校验器**只透传这些**，别的一律
#: 归到 ``unknown`` —— 一个自由文本「原因」会变成一条不受控的渲染通道。
UPSTREAM_REASONS = frozenset(
    {
        "no-sample",
        "stale",
        "non-finite-sample",
        "prometheus-error",
        "timeout",
        "unreachable",
        "offline",
        "metric-name-unconfirmed",
        "invalid_response",
    }
)

STATE_LIVE = "live"
STATE_UNKNOWN = "unknown"

# --- 拒绝原因码（稳定字符串，供 BFF 映射与前端展示）---------------------------
REJECT_BAD_SHAPE = "bad-shape"
REJECT_UNTRUSTED_SOURCE = "untrusted-source"
REJECT_SYNTHETIC_SOURCE = "synthetic-source"
REJECT_NOT_READONLY = "not-readonly"
REJECT_WRONG_VERSION = "wrong-version"
REJECT_BAD_OBSERVED_AT = "bad-observed-at"
REJECT_FUTURE_OBSERVED_AT = "future-observed-at"
REJECT_UNKNOWN_METRIC_KEY = "unknown-metric-key"
REJECT_NOT_KNOWN_STATE = "unknown-state"
REJECT_NON_FINITE_VALUE = "non-finite-value"
REJECT_UNIT_MISMATCH = "unit-mismatch"
REJECT_BAD_STATE_VALUE = "bad-state-value"
REJECT_MISSING_SAMPLE_AT = "missing-sample-at"
REJECT_BAD_SAMPLE_AT = "bad-sample-at"
REJECT_FUTURE_SAMPLE_AT = "future-sample-at"
REJECT_STALE_SAMPLE_AT = "stale-sample-at"

#: 上游原因码之外，投影端在「整段离线」时使用的兜底原因。它不属于单个指标，但
#: 浏览器端会显示它，所以必须被认识（否则会退化成一个没有理由的 UNKNOWN）。
REASON_UNKNOWN = "unknown"


def parse_instant(value: Any) -> datetime | None:
    """解析一个**必须带时区**的 ISO-8601 瞬时；否则 ``None``。

    刻意不接受 naive 时间戳：无从判定它属于哪个时区，因而无从判定它有没有过期。
    一个 naive 的「样本时间」正是「它到底新不新」这个问题的答案被悄悄丢掉的地方。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment.astimezone(timezone.utc)


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


@dataclass(frozen=True)
class MetricVerdict:
    """一个主机指标的判定结果。

    ``value`` **只**在 ``live=True`` 时非 ``None`` —— 这是本模块存在的全部理由：
    一个不是 LIVE 的指标没有数字可渲染，连「0」都没有。
    """

    key: str
    live: bool
    value: float | None
    unit: str | None
    sample_at: str | None
    age_seconds: float | None
    reason: str | None
    source: str

    def __post_init__(self) -> None:
        if self.live and self.value is None:
            raise ValueError("a live metric verdict must carry a value")
        if self.live and self.reason is not None:
            raise ValueError("a live metric verdict must not carry a reason")
        if not self.live and self.value is not None:
            raise ValueError("a non-live metric verdict must not carry a value")

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "state": STATE_LIVE if self.live else STATE_UNKNOWN,
            "value": self.value,
            "unit": self.unit,
            "sample_at": self.sample_at,
            "age_seconds": self.age_seconds,
            "reason": self.reason,
            "source": self.source,
        }


@dataclass(frozen=True)
class ServiceVerdict:
    """一个服务可达性探测的判定结果。``up`` 只在 ``live=True`` 时为布尔。"""

    key: str
    live: bool
    up: bool | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.live and self.up is None:
            raise ValueError("a live service verdict must carry an up boolean")
        if not self.live and self.up is not None:
            raise ValueError("a non-live service verdict must not carry an up boolean")

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "state": STATE_LIVE if self.live else STATE_UNKNOWN,
            "up": self.up,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HostSummaryVerdict:
    """整份 ``/api/infra/summary`` 的判定结果。

    ``accepted`` 说的是**信封**是否可信。信封不可信时，``metrics`` 里每一项都是
    UNKNOWN 且共享同一个信封级原因 —— 逐字段降级是不够的，因为「这份响应的来源
    已经不可信」是关于整份响应的判断。
    """

    accepted: bool
    reason: str | None
    source: str | None
    synthetic: bool | None
    readonly: bool | None
    version: int | None
    observed_at: str | None
    metrics: dict[str, MetricVerdict] = field(default_factory=dict)
    services: dict[str, ServiceVerdict] = field(default_factory=dict)

    @property
    def live_metric_keys(self) -> tuple[str, ...]:
        """本次判定为 LIVE 的指标键（排序，便于回归逐字节比较）。"""
        return tuple(sorted(key for key, item in self.metrics.items() if item.live))

    @property
    def any_live(self) -> bool:
        return any(item.live for item in self.metrics.values())

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "source": self.source,
            "synthetic": self.synthetic,
            "readonly": self.readonly,
            "version": self.version,
            "observed_at": self.observed_at,
            "metrics": {key: item.to_dict() for key, item in self.metrics.items()},
            "services": {key: item.to_dict() for key, item in self.services.items()},
        }


def _envelope_rejection(payload: Any) -> tuple[str, str | None, int | None, bool | None]:
    """返回 ``(reason, source, version, readonly)``；``reason`` 为空串表示信封通过。"""
    if not isinstance(payload, Mapping):
        return REJECT_BAD_SHAPE, None, None, None

    source = payload.get("source")
    version = payload.get("version")
    readonly = payload.get("readonly")
    synthetic = payload.get("synthetic")

    # 先判「这是不是一份投影」再判「它说了什么」。一份 404 的 ``{"error": "..."}``
    # 之类的东西，四个信封字段一个都没有 —— 那不是「自称合成」，那根本不是投影。
    # 把这两件事混成一句，会让运维去查一个不存在的数据源问题。
    for required in ("source", "synthetic", "readonly", "version"):
        if required not in payload:
            return REJECT_BAD_SHAPE, source, version, readonly

    # synthetic 必须是真正的 JSON false。``0`` / ``"false"`` / 缺省都不算 ——
    # 一个用 0 表示「非合成」的上游，和一个用 0 表示「未知」的上游，无法区分。
    if synthetic is not False:
        return REJECT_SYNTHETIC_SOURCE, source, version, readonly
    if source not in PROJECTION_SOURCES:
        return REJECT_UNTRUSTED_SOURCE, source, version, readonly
    if readonly is not True:
        return REJECT_NOT_READONLY, source, version, readonly
    # ``version`` 必须是整数 1，且 ``True`` 不算（bool 是 int 的子类）。
    if isinstance(version, bool) or version != PROJECTION_VERSION:
        return REJECT_WRONG_VERSION, source, version, readonly
    return "", source, version, readonly


def _unknown_metric(
    key: str, unit: str | None, reason: str, *, source: str | None = None
) -> MetricVerdict:
    return MetricVerdict(
        key=key,
        live=False,
        value=None,
        unit=unit if unit in set(HOST_METRIC_UNITS.values()) else (
            HOST_METRIC_UNITS.get(key) or HOST_METRIC_OPTIONAL_UNITS.get(key)
        ),
        sample_at=None,
        age_seconds=None,
        reason=reason,
        source=source if source in PROJECTION_SOURCES else PROJECTION_SOURCE,
    )


def _metric_verdict(
    key: str,
    entry: Any,
    *,
    now: datetime,
    max_sample_age_s: int,
    future_tolerance_s: int,
    source: str | None = None,
) -> MetricVerdict:
    expected_unit = HOST_METRIC_UNITS.get(key) or HOST_METRIC_OPTIONAL_UNITS[key]

    if not isinstance(entry, Mapping):
        return _unknown_metric(key, expected_unit, REJECT_BAD_SHAPE, source=source)

    if entry.get("state") != "known":
        upstream = entry.get("reason")
        reason = upstream if upstream in UPSTREAM_REASONS else REASON_UNKNOWN
        return _unknown_metric(key, entry.get("unit"), reason, source=source)

    value = entry.get("value")
    if not _is_finite_number(value):
        return _unknown_metric(key, entry.get("unit"), REJECT_NON_FINITE_VALUE, source=source)

    unit = entry.get("unit")
    if unit != expected_unit:
        return _unknown_metric(key, expected_unit, REJECT_UNIT_MISMATCH, source=source)

    # 状态量只可能是 0/1。别的值不算「未知」，是**数据错位**：一个 0.4 的
    # ``strata_up`` 既不是 up 也不是 down，把它按阈值解释就是在编。
    if expected_unit == "bool" and value not in (0, 1):
        return _unknown_metric(key, expected_unit, REJECT_BAD_STATE_VALUE, source=source)

    raw_sample_at = entry.get("sample_at")
    # 空白字符串等同于「没有给出时间戳」：一个 ``"   "`` 的样本时间既不新也不旧，
    # 它只是没有回答「这是什么时候测的」这个问题。
    if raw_sample_at is None or (
        isinstance(raw_sample_at, str) and not raw_sample_at.strip()
    ):
        return _unknown_metric(key, expected_unit, REJECT_MISSING_SAMPLE_AT, source=source)
    sample_at = parse_instant(raw_sample_at)
    if sample_at is None:
        return _unknown_metric(key, expected_unit, REJECT_BAD_SAMPLE_AT, source=source)

    age = (now - sample_at).total_seconds()
    if age < -future_tolerance_s:
        # 未来时间戳是数据质量问题，不是「非常新」。
        return _unknown_metric(key, expected_unit, REJECT_FUTURE_SAMPLE_AT, source=source)
    if age > max_sample_age_s:
        verdict = _unknown_metric(key, expected_unit, REJECT_STALE_SAMPLE_AT, source=source)
        return MetricVerdict(
            key=verdict.key,
            live=False,
            value=None,
            unit=verdict.unit,
            sample_at=raw_sample_at,
            age_seconds=age,
            reason=REJECT_STALE_SAMPLE_AT,
            source=source if source in PROJECTION_SOURCES else PROJECTION_SOURCE,
        )

    return MetricVerdict(
        key=key,
        live=True,
        value=float(value),
        unit=expected_unit,
        sample_at=raw_sample_at,
        age_seconds=max(0.0, age),
        reason=None,
        source=source if source in PROJECTION_SOURCES else PROJECTION_SOURCE,
    )


def _service_verdict(key: str, entry: Any) -> ServiceVerdict:
    if not isinstance(entry, Mapping):
        return ServiceVerdict(key=key, live=False, up=None, reason=REJECT_BAD_SHAPE)
    if entry.get("state") != "known":
        upstream = entry.get("reason")
        reason = upstream if upstream in UPSTREAM_REASONS else REASON_UNKNOWN
        return ServiceVerdict(key=key, live=False, up=None, reason=reason)
    # ``up`` 必须是真布尔。``"true"`` / ``1`` / ``null`` 都不算 —— 一个字符串
    # "false" 是 truthy 的，把它当可达性会静默地把「挂了」读成「在」。
    up = entry.get("up")
    if not isinstance(up, bool):
        return ServiceVerdict(key=key, live=False, up=None, reason=REJECT_BAD_SHAPE)
    return ServiceVerdict(key=key, live=True, up=up, reason=None)


def validate_host_summary(
    payload: Any,
    *,
    now: datetime,
    max_sample_age_s: int = DEFAULT_MAX_SAMPLE_AGE_S,
    future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S,
) -> HostSummaryVerdict:
    """把一份 ``/api/infra/summary`` 响应判成「可信的实测」或「不可信的未知」。

    ``now`` 必须显式传入（带时区）—— 「过期」是相对一个明确的当下说的，模块不自造
    当下，否则同一次判定就有了两个时间源。

    失败是封闭的：**任何**信封级问题都会让全部指标变成 UNKNOWN。
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)

    reason, source, version, readonly = _envelope_rejection(payload)
    envelope_ok = reason == ""

    observed_at: str | None = None
    if envelope_ok:
        raw_observed = payload.get("observed_at")
        observed = parse_instant(raw_observed)
        if observed is None:
            reason, envelope_ok = REJECT_BAD_OBSERVED_AT, False
        elif (observed - now).total_seconds() > future_tolerance_s:
            # 整份响应自称的时间在将来：这与单个指标的未来样本同样不可信。
            reason, envelope_ok = REJECT_FUTURE_OBSERVED_AT, False
        else:
            observed_at = raw_observed

    metrics: dict[str, MetricVerdict] = {}
    services: dict[str, ServiceVerdict] = {}

    raw_metrics = payload.get("metrics") if isinstance(payload, Mapping) else None
    if not isinstance(raw_metrics, Mapping):
        rejected_reason = reason if not envelope_ok else REJECT_BAD_SHAPE
        return HostSummaryVerdict(
            accepted=False,
            reason=rejected_reason,
            source=source,
            synthetic=payload.get("synthetic") if isinstance(payload, Mapping) else None,
            readonly=readonly,
            version=version,
            observed_at=observed_at,
            metrics={
                key: _unknown_metric(key, unit, rejected_reason, source=source)
                for key, unit in HOST_METRIC_UNITS.items()
            },
            services={},
        )

    for key, expected_unit in HOST_METRIC_UNITS.items():
        entry = raw_metrics.get(key)
        if entry is None:
            # 契约要求「始终是完整形状」：一个**缺键**的响应是残缺的，不是「这项
            # 没数据」。缺键意味着我们根本不知道投影端跑的是哪个版本。
            metrics[key] = _unknown_metric(
                key, expected_unit, REJECT_UNKNOWN_METRIC_KEY, source=source
            )
            continue
        if not envelope_ok:
            metrics[key] = _unknown_metric(key, expected_unit, reason, source=source)
            continue
        metrics[key] = _metric_verdict(
            key,
            entry,
            now=now,
            max_sample_age_s=max_sample_age_s,
            future_tolerance_s=future_tolerance_s,
            source=source,
        )

    for key, expected_unit in HOST_METRIC_OPTIONAL_UNITS.items():
        if key not in raw_metrics:
            continue
        if not envelope_ok:
            metrics[key] = _unknown_metric(key, expected_unit, reason, source=source)
            continue
        metrics[key] = _metric_verdict(
            key,
            raw_metrics[key],
            now=now,
            max_sample_age_s=max_sample_age_s,
            future_tolerance_s=future_tolerance_s,
            source=source,
        )

    raw_services = payload.get("services")
    if isinstance(raw_services, Mapping):
        for key in ("prometheus", "gateway", "grafana", "evalscope", "strata", "strata_model"):
            entry = raw_services.get(key)
            if entry is None:
                continue
            if not envelope_ok:
                services[key] = ServiceVerdict(key=key, live=False, up=None, reason=reason)
                continue
            services[key] = _service_verdict(key, entry)

    return HostSummaryVerdict(
        accepted=envelope_ok,
        reason=None if envelope_ok else reason,
        source=source,
        synthetic=payload.get("synthetic") if isinstance(payload, Mapping) else None,
        readonly=readonly,
        version=version,
        observed_at=observed_at,
        metrics=metrics,
        services=services,
    )
