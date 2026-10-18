"""监控 DTO 的形状定义：读数（reading）、新鲜度（freshness）、指标目录（catalogue）。

三条贯穿全模块的硬规则：

1. **``UNKNOWN`` 不等于 ``0``。** 没测到就是没测到，``value`` 为 ``None`` 并带一个
   稳定的 ``reason``；绝不允许用 ``0`` 顶替。``0`` 只在「确实数过，结果是零」时出现。
2. **读数由目录（:data:`CATALOGUE`）构造，不由调用方拼装。** :meth:`MetricReading.from_metric`
   从目录取 ``scope`` / ``unit`` / ``physical``，所以「某个用户在这个共享实例上占了多少
    GPU 显存」这种读数**结构上无法被表达** —— 见 :data:`PHYSICAL_SCOPES`。
3. **每个读数都自带来源与时间。** ``source`` / ``observed_at`` / ``quality`` / ``freshness``
   四件套缺一不可，好让 UI 如实显示「这是谁在什么时候测的、还新不新」，而不是把
   一份过期快照画成当前值。

DTO 本身是**纯 JSON 可序列化的嵌套 dict**，便于用 ``json.dumps(..., sort_keys=True)``
做逐字节回归比对。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping

from .errors import ContractError, InvalidRequest

#: DTO 模式版本。字段增删必须同步 docs/observability-contract.md。
SCHEMA_VERSION = "observability/1"

#: 读数质量取值域。
#:
#: ``stale`` **不是** ``quality`` 的取值，它是 ``freshness.stale`` 推导出的
#: ``display_quality``。这样「测到过、但已经过期」与「根本没测到」在数据层不混淆，
#: 而在展示层又能有一个字段直接决定 UI 怎么画。
QUALITY_MEASURED = "measured"
QUALITY_PARTIAL = "partial"
QUALITY_UNKNOWN = "unknown"

#: :meth:`MetricReading.from_metric` 允许的 ``quality``。
_RAW_QUALITIES = (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN)

#: 展示层质量：在原始 quality 之外多一个 :data:`DISPLAY_STALE`。
DISPLAY_STALE = "stale"

#: 读数作用域。``device`` / ``node`` 是**物理**读数，永远**不**按用户切分。
SCOPE_NODE = "node"
SCOPE_DEVICE = "device"
SCOPE_SERVICE = "service"
SCOPE_ENGINE = "engine"
SCOPE_PRINCIPAL = "principal"

#: 物理作用域：这些读数描述的是整机 / 整卡，**不是**任何人的用量。
#:
#: 这条常量是「共享推理实例 GPU 不得虚构分用户显存」的机器可检形式：
#: :meth:`MetricReading.from_metric` 拒绝对物理作用域的读数附加 ``uid``，而
#: :class:`MetricSpec` 里也没有任何 ``principal.*`` 指标的单位是字节的显存。
PHYSICAL_SCOPES = frozenset({SCOPE_NODE, SCOPE_DEVICE})

#: 未知读数的原因码。**稳定字符串**，会原样进入 DTO 供 UI 显示与回归断言。
REASON_NO_SAMPLE = "no_sample"
"""窗口内没有任何样本。"""

REASON_SOURCE_UNAVAILABLE = "source_unavailable"
"""来源不可达（Prometheus 掉线、NVML 打不开、主机采集未启用）。"""

REASON_NOT_COLLECTED = "not_collected"
"""本部署形态不采集该指标（例如生产不启用主机采集）。"""

REASON_NOT_AUTHORIZED = "not_authorized"
"""该读数存在，但本主体无权看到 —— UI 应显示「未授权」，不是 0。"""

REASON_UNSUPPORTED = "unsupported"
"""宿主环境不提供该指标（例如没有 NVIDIA 设备时的驱动状态）。"""

REASON_CLOCK_SKEW = "clock_skew"
"""来源时间戳落在未来 —— 宁可标为不可信，也不当成「极新」。"""

REASON_AMBIGUOUS_SERIES = "ambiguous_series"
"""一条固定表达式返回了多条序列，而该表达式没有声明如何归约 —— 不做隐含猜测。"""

ALL_REASONS = frozenset(
    {
        REASON_NO_SAMPLE,
        REASON_SOURCE_UNAVAILABLE,
        REASON_NOT_COLLECTED,
        REASON_NOT_AUTHORIZED,
        REASON_UNSUPPORTED,
        REASON_CLOCK_SKEW,
        REASON_AMBIGUOUS_SERIES,
    }
)

#: 观测时间落在「未来」超过这个容差就判为时钟偏斜。
CLOCK_SKEW_TOLERANCE = timedelta(seconds=30)


#: 服务健康探测的取值域。**没有** ``unknown``：测不到由 ``quality`` 表达，不是一种状态。
SERVICE_STATES = ("up", "down")

#: 引擎状态。``degraded`` 是「探测通但有坏副本」，与 ``down`` 不同。
ENGINE_STATES = ("up", "degraded", "down")

#: GPU 驱动 / NVML 状态。
#:
#: ``version_mismatch`` 是刻意单列的一种：内核模块与用户态库版本不一致时，NVML 往往
#: **仍然能打开**并报出设备，但读数不可信 —— 这不是 ``down``，而是「看得到、不能信」。
DRIVER_STATES = ("ok", "version_mismatch", "no_device", "permission_denied")


@dataclass(frozen=True)
class MetricSpec:
    """目录中的一条指标定义。

    ``physical`` 是**派生**的（``scope in PHYSICAL_SCOPES``），不单独存字段，免得两处
    不一致。
    """

    metric_id: str
    scope: str
    unit: str
    description: str
    #: 超过这个秒数没有新样本即判为 stale。``None`` = 该指标不参与过期判定
    #: （例如「驱动是否存在」这类结构性事实）。
    stale_after_seconds: int | None = 60
    #: ``unit == "state"`` 的指标必须在这里声明取值域 —— 取值域是**指标自身的属性**，
    #: 不是某个 provider 的意见。放在目录里，`state` 读数的合法集合就只有一处定义。
    allowed_states: tuple[str, ...] | None = None

    @property
    def physical(self) -> bool:
        return self.scope in PHYSICAL_SCOPES

    @property
    def per_principal(self) -> bool:
        return self.scope == SCOPE_PRINCIPAL


def _spec(
    metric_id: str,
    scope: str,
    unit: str,
    description: str,
    stale_after_seconds: int | None = 60,
    allowed_states: tuple[str, ...] | None = None,
) -> MetricSpec:
    spec = MetricSpec(
        metric_id=metric_id,
        scope=scope,
        unit=unit,
        description=description,
        stale_after_seconds=stale_after_seconds,
        allowed_states=allowed_states,
    )
    if unit == "state" and not allowed_states:
        raise InvalidRequest(f"{metric_id}: a state metric must declare allowed_states")
    if unit != "state" and allowed_states:
        raise InvalidRequest(f"{metric_id}: allowed_states is only meaningful for state metrics")
    return spec


#: 固定指标目录。**只有**这里列出的 ``metric_id`` 能被构造出来。
#:
#: 没有 ``principal.gpu.memory_bytes`` 这一条，且加不进来是有意的：共享推理实例上
#: 显存是整卡共享的，按用户切分只能是编造。按用户的计量口径是 Gateway 已验证 key
#: 的请求 / token 计数（``principal.*``），口径见 docs/observability-contract.md §6。
CATALOGUE: Mapping[str, MetricSpec] = MappingProxyType(
    {
        # --- 整机（node）-----------------------------------------------
        "node.cpu.utilization_percent": _spec(
            "node.cpu.utilization_percent", SCOPE_NODE, "percent", "整机 CPU 使用率"
        ),
        "node.cpu.load1": _spec("node.cpu.load1", SCOPE_NODE, "load", "1 分钟平均负载"),
        "node.memory.total_bytes": _spec(
            "node.memory.total_bytes", SCOPE_NODE, "bytes", "物理内存总量"
        ),
        "node.memory.available_bytes": _spec(
            "node.memory.available_bytes", SCOPE_NODE, "bytes", "可用物理内存"
        ),
        "node.memory.used_bytes": _spec(
            "node.memory.used_bytes", SCOPE_NODE, "bytes", "已用物理内存"
        ),
        "node.disk.total_bytes": _spec(
            "node.disk.total_bytes", SCOPE_NODE, "bytes", "根文件系统容量"
        ),
        "node.disk.used_bytes": _spec(
            "node.disk.used_bytes", SCOPE_NODE, "bytes", "根文件系统已用"
        ),
        "node.gpu.count": _spec("node.gpu.count", SCOPE_NODE, "count", "可见 GPU 数量"),
        "node.gpu.driver.status": _spec(
            "node.gpu.driver.status",
            SCOPE_NODE,
            "state",
            "GPU 驱动 / NVML 可用性状态",
            stale_after_seconds=None,
            allowed_states=DRIVER_STATES,
        ),
        "node.gpu.driver.kernel_module_version": _spec(
            "node.gpu.driver.kernel_module_version",
            SCOPE_NODE,
            "text",
            "内核模块自报版本（与用户态不一致即 mismatch）",
            stale_after_seconds=None,
        ),
        "node.gpu.driver.userspace_version": _spec(
            "node.gpu.driver.userspace_version",
            SCOPE_NODE,
            "text",
            "NVML 用户态库自报版本",
            stale_after_seconds=None,
        ),
        "node.gpu.driver.compiler_version": _spec(
            "node.gpu.driver.compiler_version",
            SCOPE_NODE,
            "text",
            "构建内核模块所用的编译器版本（驱动报告里的第二行，用于交叉印证）",
            stale_after_seconds=None,
        ),
        # --- 单卡（device）---------------------------------------------
        "device.gpu.memory.total_bytes": _spec(
            "device.gpu.memory.total_bytes", SCOPE_DEVICE, "bytes", "整卡显存总量"
        ),
        "device.gpu.memory.used_bytes": _spec(
            "device.gpu.memory.used_bytes", SCOPE_DEVICE, "bytes", "整卡已用显存（共享，非按人）"
        ),
        "device.gpu.utilization_percent": _spec(
            "device.gpu.utilization_percent", SCOPE_DEVICE, "percent", "整卡利用率"
        ),
        "device.gpu.temperature_celsius": _spec(
            "device.gpu.temperature_celsius", SCOPE_DEVICE, "celsius", "整卡温度"
        ),
        # --- 服务（service）--------------------------------------------
        "service.up": _spec(
            "service.up",
            SCOPE_SERVICE,
            "state",
            "只读健康探测是否可达",
            stale_after_seconds=120,
            allowed_states=SERVICE_STATES,
        ),
        # --- 引擎 / 部署（engine）--------------------------------------
        "engine.up": _spec(
            "engine.up",
            SCOPE_ENGINE,
            "state",
            "引擎健康探测是否可达",
            stale_after_seconds=120,
            allowed_states=ENGINE_STATES,
        ),
        "engine.replicas": _spec(
            "engine.replicas", SCOPE_ENGINE, "count", "登记在册的副本数"
        ),
        "engine.latency_p95_ms": _spec(
            "engine.latency_p95_ms", SCOPE_ENGINE, "milliseconds", "引擎自身 p95 延迟"
        ),
        # --- 按人用量（principal）--------------------------------------
        # 这些**只**来自 Gateway 已验证 key 的元数据；计量基准永远是 ``verified-key``。
        "principal.requests": _spec(
            "principal.requests", SCOPE_PRINCIPAL, "count", "本人已验证 key 的请求数"
        ),
        "principal.errors": _spec(
            "principal.errors", SCOPE_PRINCIPAL, "count", "本人已验证 key 的失败请求数"
        ),
        "principal.throttled_429": _spec(
            "principal.throttled_429", SCOPE_PRINCIPAL, "count", "本人被限流的请求数"
        ),
        "principal.tokens.input": _spec(
            "principal.tokens.input", SCOPE_PRINCIPAL, "tokens", "本人输入 token 数"
        ),
        "principal.tokens.output": _spec(
            "principal.tokens.output", SCOPE_PRINCIPAL, "tokens", "本人输出 token 数"
        ),
        "principal.queue_p50_ms": _spec(
            "principal.queue_p50_ms", SCOPE_PRINCIPAL, "milliseconds", "本人排队 p50"
        ),
        "principal.queue_p95_ms": _spec(
            "principal.queue_p95_ms", SCOPE_PRINCIPAL, "milliseconds", "本人排队 p95"
        ),
        "principal.latency_p50_ms": _spec(
            "principal.latency_p50_ms", SCOPE_PRINCIPAL, "milliseconds", "本人总延迟 p50"
        ),
        "principal.latency_p95_ms": _spec(
            "principal.latency_p95_ms", SCOPE_PRINCIPAL, "milliseconds", "本人总延迟 p95"
        ),
    }
)


@dataclass(frozen=True)
class Provenance:
    """数据来源声明。**没有默认值** —— 调用方必须显式声明，避免把合成数据说成生产。"""

    source: str
    synthetic: bool

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidRequest("provenance.source must be a non-empty string")
        if not isinstance(self.synthetic, bool):
            raise InvalidRequest("provenance.synthetic must be a boolean")

    @property
    def source_class(self) -> str:
        """来源分类：``verified`` / ``synthetic`` / ``unverified``。见 ``trust``。"""
        from .trust import classify_source

        return classify_source(self.source)

    @property
    def live_eligible(self) -> bool:
        """这份数据是否可按 LIVE（经核验的真实读数）呈现。

        唯一的真源是 :func:`backend.observability.trust.may_present_as_live`：
        非合成 **且** 来源在受信白名单里。刻意不看数据的形状 —— 形状可以被伪造，
        来源声明不能（它来自受信配置，不来自请求）。
        """
        from .trust import may_present_as_live

        return may_present_as_live(self.source, synthetic=self.synthetic)

    def to_dict(self) -> dict:
        return {"source": self.source, "synthetic": self.synthetic}


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidRequest(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRequest(f"{name} must carry an explicit UTC offset")
    return value.astimezone(timezone.utc)


def freshness_block(
    observed_at: datetime | None,
    now: datetime,
    threshold_seconds: int | None,
) -> dict:
    """算新鲜度。**三种状态**，不是两种：

    * 从未观测（``observed_at is None``）-> ``age_seconds=None``、``stale=None``。
      注意 ``stale`` 是 ``None`` 而**不是** ``False``：``False`` 会断言「它是新的」，
      而我们其实无从谈起。
    * 观测时间在未来（超过 :data:`CLOCK_SKEW_TOLERANCE`）-> ``clock_skew=True``、
      ``stale=None``。一个未来时间戳是数据质量问题，不是「非常新」。
    * 正常 -> ``age_seconds`` 与 ``stale`` 布尔值。
    """
    now_utc = _require_utc(now, "now")
    if observed_at is None:
        return {
            "age_seconds": None,
            "stale": None,
            "clock_skew": None,
            "threshold_seconds": threshold_seconds,
        }

    observed_utc = _require_utc(observed_at, "observed_at")
    delta = now_utc - observed_utc
    if delta < -CLOCK_SKEW_TOLERANCE:
        return {
            "age_seconds": None,
            "stale": None,
            "clock_skew": True,
            "threshold_seconds": threshold_seconds,
        }

    age = max(0.0, delta.total_seconds())
    if threshold_seconds is None:
        stale: bool | None = False
    else:
        stale = age > threshold_seconds
    return {
        "age_seconds": round(age, 3),
        "stale": stale,
        "clock_skew": False,
        "threshold_seconds": threshold_seconds,
    }


def _check_finite(value: object, metric_id: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRequest(f"{metric_id}: value must be a real number")
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidRequest(f"{metric_id}: value must be finite")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class MetricReading:
    """一条数值读数。**只能**经 :meth:`from_metric` 构造（``__post_init__`` 再自检一遍）。

    注意这里**没有** ``uid`` / ``principal`` 字段 —— 读数不知道自己属于谁。要表达
    「某人的用量」，用的是 :data:`SCOPE_PRINCIPAL` 的读数并**在外层**标注归属；
    而物理读数（``node`` / ``device``）永远不会走到那一步。
    """

    metric_id: str
    scope: str
    unit: str
    physical: bool
    value: float | int | None
    quality: str
    source: str
    observed_at: datetime | None
    reason: str | None
    freshness: dict

    @classmethod
    def from_metric(
        cls,
        metric_id: str,
        *,
        value: float | int | None,
        source: str,
        quality: str = QUALITY_MEASURED,
        observed_at: datetime | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> "MetricReading":
        """按目录构造读数。指标名不在目录里 -> :class:`UnknownMetric`。

        ``scope`` / ``unit`` / ``physical`` **一律取自目录**，调用方无从覆写 ——
        这就是「不能手工造一条『用户 X 的显存』读数」的执行点。
        """
        from .errors import UnknownMetric

        spec = CATALOGUE.get(metric_id)
        if spec is None:
            raise UnknownMetric(f"metric not in catalogue: {metric_id!r}")
        if not isinstance(source, str) or not source.strip():
            raise InvalidRequest(f"{metric_id}: source must be a non-empty string")

        moment = now if now is not None else datetime.now(timezone.utc)
        observed = observed_at
        if observed is not None:
            observed = _require_utc(observed, "observed_at")

        if quality not in _RAW_QUALITIES:
            raise InvalidRequest(f"{metric_id}: unsupported quality {quality!r}")

        if quality == QUALITY_UNKNOWN:
            if value is not None:
                raise InvalidRequest(
                    f"{metric_id}: an unknown reading must carry value=None "
                    f"(got {value!r}) — UNKNOWN is not 0"
                )
            if reason is None:
                raise InvalidRequest(f"{metric_id}: an unknown reading needs a reason code")
            if reason not in ALL_REASONS:
                raise InvalidRequest(f"{metric_id}: unknown reason {reason!r}")
        else:
            if value is None:
                raise InvalidRequest(
                    f"{metric_id}: quality={quality} but value is None — "
                    f"use quality=unknown with a reason instead"
                )
            value = _check_finite(value, metric_id)
            if observed is None:
                raise InvalidRequest(f"{metric_id}: a measured reading needs observed_at")
            if reason is not None:
                raise InvalidRequest(
                    f"{metric_id}: reason is only meaningful for unknown readings"
                )

        return cls(
            metric_id=metric_id,
            scope=spec.scope,
            unit=spec.unit,
            physical=spec.physical,
            value=value,
            quality=quality,
            source=source.strip(),
            observed_at=observed,
            reason=reason,
            freshness=freshness_block(observed, moment, spec.stale_after_seconds),
        )

    @classmethod
    def unknown(
        cls,
        metric_id: str,
        *,
        source: str,
        reason: str,
        now: datetime | None = None,
    ) -> "MetricReading":
        """便捷构造：一条「没测到」的读数。``value`` 恒为 ``None``。"""
        return cls.from_metric(
            metric_id,
            value=None,
            source=source,
            quality=QUALITY_UNKNOWN,
            observed_at=None,
            reason=reason,
            now=now,
        )

    @property
    def stale(self) -> bool | None:
        return self.freshness.get("stale")

    @property
    def display_quality(self) -> str:
        """UI 直接用的一个字段。

        * ``unknown`` 优先 —— 从没测到就谈不上「过期」；
        * 时钟偏斜 -> ``unknown``（这份数据不可信，不是「新」）；
        * 测到过但过期 -> :data:`DISPLAY_STALE`；
        * 其余 -> 原始 ``quality``。
        """
        if self.quality == QUALITY_UNKNOWN:
            return QUALITY_UNKNOWN
        if self.freshness.get("clock_skew") is True:
            return QUALITY_UNKNOWN
        if self.freshness.get("stale") is True:
            return DISPLAY_STALE
        return self.quality

    def to_dict(self) -> dict:
        return {
            "metric_id": self.metric_id,
            "scope": self.scope,
            "unit": self.unit,
            "physical": self.physical,
            "value": self.value,
            "quality": self.quality,
            "display_quality": self.display_quality,
            "source": self.source,
            "observed_at": _iso_z(self.observed_at) if self.observed_at else None,
            "reason": self.reason,
            "freshness": dict(self.freshness),
        }


@dataclass(frozen=True)
class StateReading:
    """一条**枚举**读数（驱动状态、引擎健康……）。值不是数字，所以 ``UNKNOWN`` 尤其不能被 0 顶替。"""

    metric_id: str
    scope: str
    unit: str
    physical: bool
    state: str | None
    allowed_states: tuple[str, ...]
    quality: str
    source: str
    observed_at: datetime | None
    reason: str | None
    freshness: dict

    @classmethod
    def from_metric(
        cls,
        metric_id: str,
        *,
        state: str | None,
        allowed_states: tuple[str, ...],
        source: str,
        quality: str = QUALITY_MEASURED,
        observed_at: datetime | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> "StateReading":
        from .errors import UnknownMetric

        spec = CATALOGUE.get(metric_id)
        if spec is None:
            raise UnknownMetric(f"metric not in catalogue: {metric_id!r}")
        if not allowed_states:
            raise InvalidRequest(f"{metric_id}: allowed_states must not be empty")
        if spec.allowed_states is not None and tuple(allowed_states) != spec.allowed_states:
            # 目录里的取值域是权威；调用方另说一套会让同一指标出现两种合法集合。
            raise InvalidRequest(
                f"{metric_id}: allowed_states disagrees with the catalogue "
                f"({tuple(allowed_states)!r} != {spec.allowed_states!r})"
            )
        if not isinstance(source, str) or not source.strip():
            raise InvalidRequest(f"{metric_id}: source must be a non-empty string")

        moment = now if now is not None else datetime.now(timezone.utc)
        observed = observed_at
        if observed is not None:
            observed = _require_utc(observed, "observed_at")

        if quality not in _RAW_QUALITIES:
            raise InvalidRequest(f"{metric_id}: unsupported quality {quality!r}")

        if quality == QUALITY_UNKNOWN:
            if state is not None:
                raise InvalidRequest(
                    f"{metric_id}: an unknown reading must carry state=None (got {state!r})"
                )
            if reason not in ALL_REASONS:
                raise InvalidRequest(f"{metric_id}: unknown reason {reason!r}")
        else:
            if state not in allowed_states:
                raise InvalidRequest(
                    f"{metric_id}: state {state!r} is not one of {allowed_states!r}"
                )
            if observed is None:
                raise InvalidRequest(f"{metric_id}: a measured reading needs observed_at")
            if reason is not None:
                raise InvalidRequest(
                    f"{metric_id}: reason is only meaningful for unknown readings"
                )

        return cls(
            metric_id=metric_id,
            scope=spec.scope,
            unit=spec.unit,
            physical=spec.physical,
            state=state,
            allowed_states=tuple(allowed_states),
            quality=quality,
            source=source.strip(),
            observed_at=observed,
            reason=reason,
            freshness=freshness_block(observed, moment, spec.stale_after_seconds),
        )

    @property
    def display_quality(self) -> str:
        if self.quality == QUALITY_UNKNOWN:
            return QUALITY_UNKNOWN
        if self.freshness.get("clock_skew") is True:
            return QUALITY_UNKNOWN
        if self.freshness.get("stale") is True:
            return DISPLAY_STALE
        return self.quality

    def to_dict(self) -> dict:
        return {
            "metric_id": self.metric_id,
            "scope": self.scope,
            "unit": self.unit,
            "physical": self.physical,
            "state": self.state,
            "allowed_states": list(self.allowed_states),
            "quality": self.quality,
            "display_quality": self.display_quality,
            "source": self.source,
            "observed_at": _iso_z(self.observed_at) if self.observed_at else None,
            "reason": self.reason,
            "freshness": dict(self.freshness),
        }


@dataclass(frozen=True)
class TextReading:
    """一条**文本**读数（驱动版本号、引擎构建 id……）。

    存在的理由：这些值必须和别的读数走同一套 ``source/observed_at/quality/freshness``
    纪律。若把它们塞进一个裸字符串字段，就会成为本模块里唯一一个「不知道是从哪来的」
    数据，而恰恰驱动版本是判断「读数能不能信」的依据。
    """

    metric_id: str
    scope: str
    unit: str
    physical: bool
    text: str | None
    quality: str
    source: str
    observed_at: datetime | None
    reason: str | None
    freshness: dict

    @classmethod
    def from_metric(
        cls,
        metric_id: str,
        *,
        text: str | None,
        source: str,
        quality: str = QUALITY_MEASURED,
        observed_at: datetime | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> "TextReading":
        from .errors import UnknownMetric

        spec = CATALOGUE.get(metric_id)
        if spec is None:
            raise UnknownMetric(f"metric not in catalogue: {metric_id!r}")
        if not isinstance(source, str) or not source.strip():
            raise InvalidRequest(f"{metric_id}: source must be a non-empty string")

        moment = now if now is not None else datetime.now(timezone.utc)
        observed = observed_at
        if observed is not None:
            observed = _require_utc(observed, "observed_at")

        if quality not in _RAW_QUALITIES:
            raise InvalidRequest(f"{metric_id}: unsupported quality {quality!r}")

        if quality == QUALITY_UNKNOWN:
            if text is not None:
                raise InvalidRequest(
                    f"{metric_id}: an unknown reading must carry text=None (got {text!r})"
                )
            if reason not in ALL_REASONS:
                raise InvalidRequest(f"{metric_id}: unknown reason {reason!r}")
        else:
            if not isinstance(text, str) or not text.strip():
                raise InvalidRequest(
                    f"{metric_id}: quality={quality} but text is empty — "
                    f"use quality=unknown with a reason instead"
                )
            if observed is None:
                raise InvalidRequest(f"{metric_id}: a measured reading needs observed_at")
            if reason is not None:
                raise InvalidRequest(
                    f"{metric_id}: reason is only meaningful for unknown readings"
                )

        return cls(
            metric_id=metric_id,
            scope=spec.scope,
            unit=spec.unit,
            physical=spec.physical,
            text=text.strip() if isinstance(text, str) else None,
            quality=quality,
            source=source.strip(),
            observed_at=observed,
            reason=reason,
            freshness=freshness_block(observed, moment, spec.stale_after_seconds),
        )

    @property
    def display_quality(self) -> str:
        if self.quality == QUALITY_UNKNOWN:
            return QUALITY_UNKNOWN
        if self.freshness.get("clock_skew") is True:
            return QUALITY_UNKNOWN
        if self.freshness.get("stale") is True:
            return DISPLAY_STALE
        return self.quality

    def to_dict(self) -> dict:
        return {
            "metric_id": self.metric_id,
            "scope": self.scope,
            "unit": self.unit,
            "physical": self.physical,
            "text": self.text,
            "quality": self.quality,
            "display_quality": self.display_quality,
            "source": self.source,
            "observed_at": _iso_z(self.observed_at) if self.observed_at else None,
            "reason": self.reason,
            "freshness": dict(self.freshness),
        }


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def iso_z(moment: datetime) -> str:
    """UTC ISO-8601，秒精度，``Z`` 结尾。公开版本给 service 层复用。"""
    return _iso_z(moment)


def readings_map(readings) -> dict:
    """把一串读数按 ``metric_id`` 收成 dict，便于 DTO 直接内嵌。"""
    result: dict[str, dict] = {}
    for reading in readings:
        if isinstance(reading, (MetricReading, StateReading, TextReading)):
            result[reading.metric_id] = reading.to_dict()
        else:
            raise InvalidRequest("readings_map only accepts readings")
    return result


__all__ = [
    "ALL_REASONS",
    "CATALOGUE",
    "CLOCK_SKEW_TOLERANCE",
    "DISPLAY_STALE",
    "DRIVER_STATES",
    "ENGINE_STATES",
    "MetricReading",
    "MetricSpec",
    "PHYSICAL_SCOPES",
    "Provenance",
    "QUALITY_MEASURED",
    "QUALITY_PARTIAL",
    "QUALITY_UNKNOWN",
    "REASON_AMBIGUOUS_SERIES",
    "REASON_CLOCK_SKEW",
    "REASON_NOT_AUTHORIZED",
    "REASON_NOT_COLLECTED",
    "REASON_NO_SAMPLE",
    "REASON_SOURCE_UNAVAILABLE",
    "REASON_UNSUPPORTED",
    "SCHEMA_VERSION",
    "SCOPE_DEVICE",
    "SCOPE_ENGINE",
    "SCOPE_NODE",
    "SCOPE_PRINCIPAL",
    "SCOPE_SERVICE",
    "SERVICE_STATES",
    "StateReading",
    "TextReading",
    "ContractError",
    "freshness_block",
    "iso_z",
    "readings_map",
]
