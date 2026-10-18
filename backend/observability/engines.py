"""引擎 / 部署登记与健康判定。

**可见性是目录性的，不是计数性的。** 一个非管理主体看不到的引擎，在本人的任何响应里
**根本不存在** —— 不是「显示为 0% 可用」，也不是「名字出现但副本数为 0」。后者会让
「B 有一个私有引擎」变成一条可读的断言，而前者不会。

**``down`` 必须有正面证据。** 探测不可达时状态是 ``unknown``，不是 ``down``：
「我没能问到」与「它确实挂了」是两件事，把它们合并会让一次采集故障看起来像一次
线上事故。同理，``degraded`` 需要「探测通 **且** 副本数不足」两个条件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from .dto import (
    ENGINE_STATES,
    QUALITY_MEASURED,
    QUALITY_UNKNOWN,
    REASON_NO_SAMPLE,
    REASON_SOURCE_UNAVAILABLE,
    MetricReading,
    StateReading,
)
from .errors import AccessDenied, InvalidRequest
from .identity import ObserverScope

#: 引擎可见性类别。
VISIBILITY_SHARED = "shared"
VISIBILITY_PRIVATE = "private"

VISIBILITY_KINDS = (VISIBILITY_SHARED, VISIBILITY_PRIVATE)


@dataclass(frozen=True)
class EngineRecord:
    """一条引擎 / 部署登记。

    ``owner_uid`` 为 ``None`` 表示平台共享引擎。**属主不迁移**：这里只记录现状，
    本模块不提供任何改写属主的入口。
    """

    engine_id: str
    endpoint_id: str
    display_name: str
    visibility: str = VISIBILITY_SHARED
    owner_uid: str | None = None
    replicas_declared: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.engine_id, str) or not self.engine_id.strip():
            raise InvalidRequest("engine_id must be a non-empty string")
        if not isinstance(self.endpoint_id, str) or not self.endpoint_id.strip():
            raise InvalidRequest("endpoint_id must be a non-empty string")
        if self.visibility not in VISIBILITY_KINDS:
            raise InvalidRequest(f"unsupported visibility {self.visibility!r}")
        if self.visibility == VISIBILITY_PRIVATE and not self.owner_uid:
            raise InvalidRequest("a private engine must declare owner_uid")
        if self.replicas_declared is not None:
            if isinstance(self.replicas_declared, bool) or not isinstance(
                self.replicas_declared, int
            ):
                raise InvalidRequest("replicas_declared must be an integer or None")
            if self.replicas_declared < 0:
                raise InvalidRequest("replicas_declared must not be negative")


class EngineRegistry:
    """引擎登记表（纯内存、无 IO）。"""

    def __init__(self, records: Iterable[EngineRecord]) -> None:
        by_engine: dict[str, EngineRecord] = {}
        by_endpoint: dict[str, str] = {}
        for record in records:
            if not isinstance(record, EngineRecord):
                raise InvalidRequest("registry entries must be EngineRecord")
            if record.engine_id in by_engine:
                raise InvalidRequest(f"duplicate engine_id: {record.engine_id!r}")
            if record.endpoint_id in by_endpoint:
                raise InvalidRequest(f"duplicate endpoint_id: {record.endpoint_id!r}")
            by_engine[record.engine_id] = record
            by_endpoint[record.endpoint_id] = record.engine_id
        self._by_engine = by_engine
        self._by_endpoint = by_endpoint

    def endpoints(self) -> frozenset[str]:
        """全部已登记端点 id。授权解析的 ``available_endpoints`` 就取这个。"""
        return frozenset(self._by_endpoint)

    def by_endpoint(self, endpoint_id: str) -> EngineRecord | None:
        engine_id = self._by_endpoint.get(endpoint_id)
        return None if engine_id is None else self._by_engine[engine_id]

    def by_id(self, engine_id: str) -> EngineRecord | None:
        return self._by_engine.get(engine_id)

    def all_records(self) -> tuple[EngineRecord, ...]:
        return tuple(self._by_engine[key] for key in sorted(self._by_engine))

    def visible_to(self, scope: ObserverScope) -> tuple[EngineRecord, ...]:
        """``scope`` 能看到哪些引擎。

        * 管理 scope -> 全部；
        * 私有引擎 -> 只有属主本人（``scope.uid == owner_uid``）；
        * 共享引擎 -> 需要 ``endpoint_id`` 落在 ``scope.endpoints`` 内。

        注意这里**只**用 ``scope.endpoints``（已由 :func:`resolve_observer_scope`
        按白名单算好），不看请求里的任何字段。
        """
        if not isinstance(scope, ObserverScope):
            raise InvalidRequest("scope must be an ObserverScope")
        if scope.admin:
            return self.all_records()

        visible_endpoints = frozenset(scope.endpoints)
        kept: list[EngineRecord] = []
        for record in self.all_records():
            if record.visibility == VISIBILITY_PRIVATE:
                if record.owner_uid == scope.uid:
                    kept.append(record)
                continue
            if record.endpoint_id in visible_endpoints:
                kept.append(record)
        return tuple(kept)

    def require_visible(self, scope: ObserverScope, endpoint_id: str) -> EngineRecord:
        """取一个端点对应的引擎，并要求它对 ``scope`` 可见。否则抛 :class:`AccessDenied`。

        「不存在」与「你看不到」是**同一个**拒绝：分开就变成枚举他人私有端点存在性的预言机。
        """
        record = self.by_endpoint(endpoint_id)
        if record is None:
            raise AccessDenied("requested endpoint is not visible to this principal")
        visible = {item.endpoint_id for item in self.visible_to(scope)}
        if endpoint_id not in visible:
            raise AccessDenied("requested endpoint is not visible to this principal")
        return record

    def __len__(self) -> int:
        return len(self._by_engine)


# ---------------------------------------------------------------------------
# 健康探测
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineProbeResult:
    """一次引擎健康探测的原始结果。``up=None`` 表示**没问到**（不是「挂了」）。"""

    up: bool | None
    observed_at: datetime | None = None
    replicas_up: int | None = None
    latency_p95_ms: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.up is not None and not isinstance(self.up, bool):
            raise InvalidRequest("probe up must be a bool or None")
        if self.up is None and self.reason is None:
            raise InvalidRequest("an inconclusive probe must carry a reason code")
        if self.up is not None and self.observed_at is None:
            raise InvalidRequest("a conclusive probe must carry observed_at")
        for name, value in (("replicas_up", self.replicas_up), ("latency_p95_ms", self.latency_p95_ms)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidRequest(f"probe {name} must be numeric or None")
            if value < 0:
                raise InvalidRequest(f"probe {name} must not be negative")


def status_from_probe(
    probe: EngineProbeResult, record: EngineRecord
) -> tuple[str | None, str | None]:
    """把探测结果折成 ``(status, reason)``。

    * ``up is None`` -> ``(None, reason)``：没问到，状态**未知**；
    * ``up is False`` -> ``("down", None)``：有正面证据；
    * ``up is True`` 且声明了副本数且 ``replicas_up < declared`` -> ``("degraded", None)``；
    * 其余 -> ``("up", None)``。
    """
    if probe.up is None:
        return None, probe.reason or REASON_SOURCE_UNAVAILABLE
    if probe.up is False:
        return "down", None
    declared = record.replicas_declared
    if declared is not None and probe.replicas_up is not None:
        if probe.replicas_up < declared:
            return "degraded", None
    return "up", None


EngineProbe = Callable[[EngineRecord], EngineProbeResult]


class MockEngineProbe:
    """按 ``engine_id`` 给固定结果的探测替身。合成数据，永远 ``synthetic``。"""

    source = "mock-engine-probe"
    synthetic = True

    def __init__(self, results: Mapping[str, EngineProbeResult] | None = None, *, default_up: bool | None = True) -> None:
        self._results = MappingProxyType(dict(results or {}))
        self._default_up = default_up

    def __call__(self, record: EngineRecord) -> EngineProbeResult:
        existing = self._results.get(record.engine_id)
        if existing is not None:
            return existing
        if self._default_up is None:
            return EngineProbeResult(up=None, reason=REASON_NO_SAMPLE)
        return EngineProbeResult(up=self._default_up, observed_at=_now())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UnavailableEngineProbe:
    """默认探测：**永远问不到**。

    这是本任务交付时的真实形态 —— 探测端点尚未接线，所以全部引擎状态如实为
    ``unknown``。它存在的意义正是让「未接线」不会被某个乐观默认值渲染成一片绿色。
    """

    source = "engine-probe-absent"
    synthetic = False

    def __call__(self, record: EngineRecord) -> EngineProbeResult:
        return EngineProbeResult(up=None, reason=REASON_SOURCE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# 健康报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineHealthReport:
    """一个引擎的健康快照。"""

    engine_id: str
    endpoint_id: str
    display_name: str
    visibility: str
    owner_uid: str | None
    readings: tuple
    notes: tuple[str, ...] = ()

    @property
    def status(self) -> str | None:
        for reading in self.readings:
            if isinstance(reading, StateReading) and reading.metric_id == "engine.up":
                return reading.state
        return None

    @property
    def quality(self) -> str:
        for reading in self.readings:
            if isinstance(reading, StateReading) and reading.metric_id == "engine.up":
                return reading.quality
        return QUALITY_UNKNOWN

    def to_dict(self) -> dict:
        return {
            "engine_id": self.engine_id,
            "endpoint_id": self.endpoint_id,
            "display_name": self.display_name,
            "visibility": self.visibility,
            "owner_uid": self.owner_uid,
            "status": self.status,
            "quality": self.quality,
            "readings": {r.metric_id: r.to_dict() for r in self.readings},
            "notes": list(self.notes),
        }


def probe_engine(
    record: EngineRecord,
    probe: EngineProbe,
    *,
    source: str,
    now: datetime,
) -> EngineHealthReport:
    """探测一个引擎并折成报告。``probe`` 抛异常按「问不到」处理，绝不按 down。"""
    try:
        result = probe(record)
    except Exception:  # noqa: BLE001 - 探测实现是外部注入的，任何异常都只是「问不到」
        result = EngineProbeResult(up=None, reason=REASON_SOURCE_UNAVAILABLE)

    if not isinstance(result, EngineProbeResult):
        result = EngineProbeResult(up=None, reason=REASON_SOURCE_UNAVAILABLE)

    status, reason = status_from_probe(result, record)
    quality = QUALITY_UNKNOWN if status is None else QUALITY_MEASURED

    readings: list = [
        StateReading.from_metric(
            "engine.up",
            state=status,
            allowed_states=ENGINE_STATES,
            source=source,
            quality=quality,
            observed_at=result.observed_at if status is not None else None,
            reason=reason if status is None else None,
            now=now,
        )
    ]

    if result.replicas_up is None:
        readings.append(
            MetricReading.unknown(
                "engine.replicas", source=source, reason=REASON_NO_SAMPLE, now=now
            )
        )
    else:
        readings.append(
            MetricReading.from_metric(
                "engine.replicas",
                value=result.replicas_up,
                source=source,
                observed_at=result.observed_at or now,
                now=now,
            )
        )

    if result.latency_p95_ms is None:
        readings.append(
            MetricReading.unknown(
                "engine.latency_p95_ms",
                source=source,
                reason=REASON_NO_SAMPLE,
                now=now,
            )
        )
    else:
        readings.append(
            MetricReading.from_metric(
                "engine.latency_p95_ms",
                value=result.latency_p95_ms,
                source=source,
                observed_at=result.observed_at or now,
                now=now,
            )
        )

    notes: list[str] = []
    if status is None:
        notes.append(
            "探测无结论（reason="
            f"{reason}）：状态为 unknown，**不**等同于 down。"
        )
    if record.replicas_declared is not None and result.replicas_up is not None:
        notes.append(
            f"副本 {result.replicas_up} / 声明 {record.replicas_declared}。"
        )

    return EngineHealthReport(
        engine_id=record.engine_id,
        endpoint_id=record.endpoint_id,
        display_name=record.display_name,
        visibility=record.visibility,
        owner_uid=record.owner_uid,
        readings=tuple(readings),
        notes=tuple(notes),
    )


__all__ = [
    "EngineHealthReport",
    "EngineProbe",
    "EngineProbeResult",
    "EngineRecord",
    "EngineRegistry",
    "MockEngineProbe",
    "UnavailableEngineProbe",
    "VISIBILITY_KINDS",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_SHARED",
    "probe_engine",
    "status_from_probe",
]
