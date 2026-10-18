"""只读报告来源适配器：EvalScope ``/api/v1/reports`` 与旧 Benchmark 镜像。

三条边界
--------

**1. 上游是固定的，不由客户端决定。** 适配器在**构造**时拿到一个 base URL 与一个
注入的 ``transport``；HTTP 请求里的任何字段（URL、路径、query、header）都进不到这里。
因此"让服务端替我去读一个我指定的地址"这件事在结构上不存在——没有一个函数把它接线。
本部署 ``transport`` 是 ``None``，于是两条来源都如实报成 ``unconfigured``。

**2. 来源由适配器盖章，负载说了不算。** :func:`records_from_evalscope_payload` 产出的
记录永远是 ``evalscope``；:func:`records_from_legacy_payload` 产出的永远是
``legacy-benchmark``。负载里就算写着 ``"source": "evalscope"`` 也**不会**被读——这是
Issue #34 里"旧 run 被当成 EvalScope 报告"那条缺陷的正面防线。

**3. 归属必须被核实。** EvalScope 的归属来自服务端配置的
:class:`~backend.evaluation.provenance.ReportOwner`；旧镜像的归属必须命中一份**审核过的**
``uid -> ReportOwner`` 映射，否则那条 run 就是 ``unowned``，默认对所有人隐藏。

本模块**不发网络、不读磁盘**：``transport`` 是调用方注入的可调用对象。测试注入一个
返回合成负载的函数；本部署不注入任何东西。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from .errors import ReportPathRejected, ReportSourceMalformed
from .models import MetricSample, metric_known, metric_unknown
from .provenance import (
    REASON_SOURCE_EMPTY,
    REASON_SOURCE_MALFORMED,
    REASON_SOURCE_UNCONFIGURED,
    REASON_SOURCE_UNREACHABLE,
    REPORT_DIMENSIONS,
    SOURCE_EMPTY,
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    SOURCE_OK,
    SOURCE_UNREACHABLE,
    ReportOwner,
    ReportRecord,
    ReportSourceSnapshot,
    safe_text,
    unconfigured_snapshot,
)

#: EvalScope 的报告列表路径。**常量**：调用方无法从一个请求里把它改掉。
EVALSCOPE_REPORTS_PATH = "/api/v1/reports"

#: 旧 Benchmark 镜像的运行索引路径。同样是常量。
LEGACY_RUNS_PATH = "/api/legacy/benchmark/runs"

#: 注入的读取器：给定**固定**路径，返回已解析的负载（通常是 ``dict``）。
Transport = Callable[[str], object]

_REPORT_TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z")


def unreachable_snapshot(
    source: str, *, detail: str, synthetic: bool = False
) -> ReportSourceSnapshot:
    """一条"接上了但这次没读到"的快照。零条、``unreachable``。"""
    return ReportSourceSnapshot(
        source=source,
        state=SOURCE_UNREACHABLE,
        synthetic=synthetic,
        observed_at=None,
        records=(),
        detail=detail,
    )


def _parse_timestamp(raw: object, *, what: str) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ReportSourceMalformed(f"{what} must be an ISO-8601 string or null")
    text = raw.strip()
    for fmt in _REPORT_TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ReportSourceMalformed(f"{what} is not a recognized ISO-8601 timestamp")


def _dimensions_from(raw: object) -> dict[str, MetricSample]:
    """把上游的指标映射成维度样本。

    未知维度名是**错误**（``ReportSourceMalformed``），不是被丢掉的噪声：上游多送一个
    名字意味着我们和它的契约漂移了，那件事应当被看见，而不是静默消失。已知维度里
    ``null`` 表示"这次没测到"，是 ``unknown`` 而**不是** 0。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ReportSourceMalformed("report metrics must be a mapping or null")
    unknown = sorted(set(raw) - set(REPORT_DIMENSIONS))
    if unknown:
        raise ReportSourceMalformed(f"unsupported dimension name(s): {', '.join(unknown)}")
    samples: dict[str, MetricSample] = {}
    for name, value in raw.items():
        if value is None:
            samples[name] = metric_unknown(name)
            continue
        unit = None
        if isinstance(value, Mapping):
            unit = value.get("unit")
            value = value.get("value")
        if value is None:
            samples[name] = metric_unknown(name)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReportSourceMalformed(f"dimension {name!r} is not numeric")
        if not isinstance(unit, str) or not unit:
            unit = None
        samples[name] = metric_known(name, value, unit=unit)
    return samples


def _require_str(raw: object, *, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ReportSourceMalformed(f"{what} must be a non-empty string")
    return raw.strip()


def _owner_for(
    source_report_id: str,
    *,
    owners: Mapping[str, ReportOwner] | None,
    single_tenant_owner: ReportOwner | None,
) -> ReportOwner | None:
    """一条记录的归属。

    **只有两种配得出归属的方式**：明确声明该来源是**单租户隔离实例**（此时整份列表
    属于同一个配置 owner），或逐条命中一份审核过的 ``报告 id -> ReportOwner`` 映射。
    二者都没有 -> ``None``（归属无法核实 -> 默认隐藏）。**绝不**把整份全局列表盖上一个
    owner 当作"共享来源"的缺省。
    """
    if single_tenant_owner is not None:
        return single_tenant_owner
    if owners is None:
        return None
    return owners.get(source_report_id)


def records_from_evalscope_payload(
    payload: object,
    *,
    owners: Mapping[str, ReportOwner] | None = None,
    single_tenant_owner: ReportOwner | None = None,
    synthetic: bool,
) -> tuple[ReportRecord, ...]:
    """EvalScope ``/api/v1/reports`` 负载 -> 记录。

    只认 ``id`` / ``artifact``(或 ``ref``) / ``created_at`` / ``metrics`` / ``observed_at``。
    负载里的 ``source`` / ``origin`` / ``kind`` **一律不读**：来源是这个函数给的。

    归属：``single_tenant_owner`` 是一次**显式的单租户声明**（这份列表由受信配置证明是
    某个主体的隔离实例），此时整份列表归它；否则必须逐条命中 ``owners``，未命中即
    ``unowned``。给不出单租户证明、又拿不到逐条映射的**共享/全局列表**不会得到整份指派。
    """
    if single_tenant_owner is not None and owners:
        raise ReportSourceMalformed(
            "declare either a single-tenant owner or a per-report owner map, not both"
        )
    if not isinstance(payload, Mapping):
        raise ReportSourceMalformed("an EvalScope payload must be a mapping")
    items = payload.get("reports")
    if items is None:
        raise ReportSourceMalformed("an EvalScope payload must carry a 'reports' list")
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, Mapping)):
        raise ReportSourceMalformed("'reports' must be a list")
    records = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ReportSourceMalformed(f"report #{index} is not an object")
        source_report_id = _require_str(
            item.get("report_id", item.get("id")), what=f"reports[{index}].id"
        )
        ref = _require_str(
            item.get("artifact", item.get("ref")), what=f"reports[{index}].artifact"
        )
        records.append(
            ReportRecord(
                # 来源**写死**：负载里的任何"source"字段都不参与。
                source=SOURCE_EVALSCOPE,
                source_report_id=source_report_id,
                ref=ref,
                synthetic=synthetic,
                owner=_owner_for(
                    source_report_id, owners=owners, single_tenant_owner=single_tenant_owner
                ),
                title=safe_text(item.get("title")),
                observed_at=_parse_timestamp(
                    item.get("observed_at"), what=f"reports[{index}].observed_at"
                ),
                created_at=_parse_timestamp(
                    item.get("created_at"), what=f"reports[{index}].created_at"
                ),
                dimensions=_dimensions_from(item.get("metrics")),
            )
        )
    return tuple(records)


def records_from_legacy_payload(
    payload: object,
    *,
    audited_owners: Mapping[str, ReportOwner],
    synthetic: bool,
) -> tuple[ReportRecord, ...]:
    """旧 Benchmark 镜像索引 -> 记录（来源**恒为** ``legacy-benchmark``）。

    归属只来自 ``audited_owners``：一份**逐 run 审核过**的 ``run_id -> ReportOwner`` 映射。
    镜像里自报的 ``owner_uid`` / ``owner`` / ``owner_principal`` / ``source`` 字段**一律不读**
    ——只用 UID 作键只能证明"这个 UID 在可信名单上"，**不证明这一条 run 属于它**；一条
    自报 ``owner_uid`` 的记录会把未核实的历史产物指派给一个被允许的主体。

    ``run_id`` 未命中映射时，这条 run 就是 ``unowned``——默认对所有人隐藏。这正是
    Issue #34 要求的"无法核实 owner 的历史数据只能隐藏"。
    """
    if not isinstance(payload, Mapping):
        raise ReportSourceMalformed("a legacy payload must be a mapping")
    items = payload.get("runs")
    if items is None:
        raise ReportSourceMalformed("a legacy payload must carry a 'runs' list")
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, Mapping)):
        raise ReportSourceMalformed("'runs' must be a list")
    records = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ReportSourceMalformed(f"run #{index} is not an object")
        source_report_id = _require_str(
            item.get("run_id", item.get("id")), what=f"runs[{index}].id"
        )
        ref = _require_str(
            item.get("artifact", item.get("ref")), what=f"runs[{index}].artifact"
        )

        # 归属按**这一条 run** 审核过的映射来；payload 里的 owner_uid 不参与判定。
        owner = audited_owners.get(source_report_id)

        records.append(
            ReportRecord(
                source=SOURCE_LEGACY_BENCHMARK,
                source_report_id=source_report_id,
                ref=ref,
                synthetic=synthetic,
                owner=owner,
                title=safe_text(item.get("title")),
                observed_at=_parse_timestamp(
                    item.get("observed_at"), what=f"runs[{index}].observed_at"
                ),
                created_at=_parse_timestamp(
                    item.get("created_at"), what=f"runs[{index}].created_at"
                ),
                dimensions=_dimensions_from(item.get("metrics")),
            )
        )
    return tuple(records)


def _validate_base_url(raw: object) -> str:
    """上游基址的形状校验。拒绝内嵌凭据、query、fragment 与非 http(s) 方案。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ReportSourceMalformed("a source base URL must be a non-empty string")
    text = raw.strip()
    if "://" not in text:
        raise ReportSourceMalformed("a source base URL must be absolute")
    scheme, _, rest = text.partition("://")
    if scheme not in ("http", "https"):
        raise ReportSourceMalformed(f"unsupported source scheme: {scheme!r}")
    if "@" in rest.split("/", 1)[0]:
        raise ReportSourceMalformed("a source base URL must not embed credentials")
    if "?" in text or "#" in text:
        raise ReportSourceMalformed("a source base URL must not carry a query or fragment")
    return text.rstrip("/")


class _ReadOnlySourceReader:
    """两个适配器的共同骨:配置校验 + ``snapshot()`` 的 fail-closed 形状。"""

    name: str
    path: str

    def __init__(
        self,
        *,
        base_url: object = None,
        transport: Transport | None = None,
        clock: Callable[[], datetime],
        synthetic: bool = False,
    ) -> None:
        if not callable(clock):
            raise ReportSourceMalformed("a reader needs a clock")
        self._clock = clock
        self._synthetic = bool(synthetic)
        self._transport = transport if callable(transport) else None
        self._base_url = _validate_base_url(base_url) if base_url is not None else None

    @property
    def configured(self) -> bool:
        """**必须同时**有基址与注入的读取器。缺一个都是"没接线"。"""
        return self._base_url is not None and self._transport is not None

    def describe(self) -> dict:
        """给运维看的只读描述。**不回显** base URL（它属于接入细节，不属于契约）。"""
        return {
            "source": self.name,
            "configured": self.configured,
            "has_base_url": self._base_url is not None,
            "has_transport": self._transport is not None,
            "read_only": True,
            "path": self.path,
        }

    def _read(self) -> object:
        assert self._transport is not None  # noqa: S101 - guarded by ``configured``
        return self._transport(self.path)

    def snapshot(self) -> ReportSourceSnapshot:
        raise NotImplementedError  # pragma: no cover - 子类实现


class EvalScopeReportsReader(_ReadOnlySourceReader):
    """EvalScope ``GET /api/v1/reports``：只读、固定上游、固定路径。

    归属**必须**由调用方给出一种证明：``single_tenant_owner``（显式的单租户隔离声明）
    或 ``owners``（逐报告 id 的审核映射）。两者都不给时，每条记录都是 ``unowned``。
    """

    name = SOURCE_EVALSCOPE
    path = EVALSCOPE_REPORTS_PATH

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        owners: Mapping[str, ReportOwner] | None = None,
        single_tenant_owner: ReportOwner | None = None,
        base_url: object = None,
        transport: Transport | None = None,
        synthetic: bool = False,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport, clock=clock, synthetic=synthetic)
        if single_tenant_owner is not None and owners:
            raise ReportSourceMalformed(
                "declare either a single-tenant owner or a per-report owner map, not both"
            )
        if owners is not None and not isinstance(owners, Mapping):
            raise ReportSourceMalformed("evalscope report owners must be a mapping")
        if single_tenant_owner is not None and not isinstance(single_tenant_owner, ReportOwner):
            raise ReportSourceMalformed("single_tenant_owner must be a ReportOwner")
        self._owners = dict(owners) if owners else None
        self._single_tenant_owner = single_tenant_owner

    def snapshot(self) -> ReportSourceSnapshot:
        if not self.configured:
            return unconfigured_snapshot(self.name, detail=REASON_SOURCE_UNCONFIGURED)
        try:
            payload = self._read()
        except Exception:  # noqa: BLE001 - 上游任何失败都折成"这次没读到"
            return unreachable_snapshot(self.name, detail=REASON_SOURCE_UNREACHABLE, synthetic=self._synthetic)
        try:
            records = records_from_evalscope_payload(
                payload,
                owners=self._owners,
                single_tenant_owner=self._single_tenant_owner,
                synthetic=self._synthetic,
            )
        except (ReportSourceMalformed, ReportPathRejected):
            # 一条伪造/越界的引用是**来源**的问题，不是这次请求的问题：整条来源报成
            # "这次没读到"，而不是把 400 甩给一个只是来读列表的用户。
            return unreachable_snapshot(self.name, detail=REASON_SOURCE_MALFORMED, synthetic=self._synthetic)
        return ReportSourceSnapshot(
            source=self.name,
            state=SOURCE_OK if records else SOURCE_EMPTY,
            synthetic=self._synthetic,
            observed_at=self._clock(),
            records=records,
            detail=None if records else REASON_SOURCE_EMPTY,
        )


class LegacyBenchmarkReader(_ReadOnlySourceReader):
    """旧 Benchmark 镜像索引：只读，来源恒为 ``legacy-benchmark``。

    归属只来自 ``audited_owners``——一份逐 ``run_id`` 审核过的映射。镜像自报的
    ``owner_uid`` 不参与判定。
    """

    name = SOURCE_LEGACY_BENCHMARK
    path = LEGACY_RUNS_PATH

    def __init__(
        self,
        *,
        audited_owners: Mapping[str, ReportOwner],
        clock: Callable[[], datetime],
        base_url: object = None,
        transport: Transport | None = None,
        synthetic: bool = False,
    ) -> None:
        super().__init__(base_url=base_url, transport=transport, clock=clock, synthetic=synthetic)
        if not isinstance(audited_owners, Mapping):
            raise ReportSourceMalformed("legacy audited owners must be a mapping")
        self._audited_owners = dict(audited_owners)

    def snapshot(self) -> ReportSourceSnapshot:
        if not self.configured:
            return unconfigured_snapshot(self.name, detail=REASON_SOURCE_UNCONFIGURED)
        try:
            payload = self._read()
        except Exception:  # noqa: BLE001
            return unreachable_snapshot(self.name, detail=REASON_SOURCE_UNREACHABLE, synthetic=self._synthetic)
        try:
            records = records_from_legacy_payload(
                payload, audited_owners=self._audited_owners, synthetic=self._synthetic
            )
        except (ReportSourceMalformed, ReportPathRejected):
            return unreachable_snapshot(self.name, detail=REASON_SOURCE_MALFORMED, synthetic=self._synthetic)
        return ReportSourceSnapshot(
            source=self.name,
            state=SOURCE_OK if records else SOURCE_EMPTY,
            synthetic=self._synthetic,
            observed_at=self._clock(),
            records=records,
            detail=None if records else REASON_SOURCE_EMPTY,
        )


def unconfigured_source_snapshots(
    *,
    sources: Iterable[str] = (SOURCE_EVALSCOPE, SOURCE_LEGACY_BENCHMARK),
) -> tuple[ReportSourceSnapshot, ...]:
    """本部署的缺省：两条来源都如实报成未接线（而不是 0 份报告）。"""
    return tuple(unconfigured_snapshot(source) for source in sources)


__all__ = [
    "EVALSCOPE_REPORTS_PATH",
    "LEGACY_RUNS_PATH",
    "EvalScopeReportsReader",
    "LegacyBenchmarkReader",
    "Transport",
    "records_from_evalscope_payload",
    "records_from_legacy_payload",
    "unconfigured_source_snapshots",
    "unreachable_snapshot",
]
