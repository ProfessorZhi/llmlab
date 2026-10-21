"""监控视图入口：受信参数 + 来源 -> 稳定 JSON DTO（纯函数）。

四种 DTO（``kind`` 字段区分）：

* ``usage.self``     —— 本人的用量（按已验证 key 归属）
* ``usage.admin``    —— 管理全局摘要（整机 / 全卡 / 全体用户 / 服务健康）
* ``engine.status``  —— 可见引擎的健康
* ``timeline``       —— 按桶的时间线

**纯函数**：同输入必得同输出。所有时间都从 ``now`` 入参取，不读系统时钟 ——
否则「过期」与「覆盖率」这两件事就没法做回归了。

**授权范围之外的行既不计数也不展示**，顺序即边界：先判归属，再判窗口，最后才计数。
否则「他人有多少行落在窗口外 / 用了多少把已吊销的 key」会变成仅凭前后 DTO 差值
就能读出来的侧信道（同 ``backend/atlas/service.py`` 的既有结论）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .dto import (
    CATALOGUE,
    PHYSICAL_SCOPES,
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    SCHEMA_VERSION,
    Provenance,
    iso_z,
)
from .engines import (
    EngineProbe,
    EngineRegistry,
    UnavailableEngineProbe,
    probe_engine,
)
from .errors import AccessDenied, ContractError, InvalidRequest
from .gateway import (
    IngestLimits,
    UsageAccumulator,
    event_endpoint,
    ingest_jsonl,
)
from .identity import (
    SCOPE_ADMIN_READ,
    KeyDirectory,
    ObserverScope,
    RequesterPrincipal,
    resolve_observer_scope,
)
from .percentiles import PERCENTILE_METHOD
from .windows import (
    SUPPORTED_BUCKETS,
    bucket_start_of,
    expected_bucket_starts,
    validate_window,
    within_window,
)

#: DTO ``kind`` 取值。
KIND_USAGE_SELF = "usage.self"
KIND_USAGE_ADMIN = "usage.admin"
KIND_ENGINE_STATUS = "engine.status"
KIND_TIMELINE = "timeline"

ALL_KINDS = (KIND_USAGE_SELF, KIND_USAGE_ADMIN, KIND_ENGINE_STATUS, KIND_TIMELINE)

#: 分页上下限。上限的存在是为了让「返回体积」有界，与窗口上界是同一类闸门。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: 按人聚合视图里，可归属到**可见主体**的 unknown 计数器。
#: 这些只由「服务端目录判定属于本次可见主体」的行累加，0 是**确实数过的零**。
SCOPED_UNKNOWN_COUNTER_FIELDS = (
    "out_of_window",
    "revoked_key",
    "filtered_out",
    "out_of_scope_endpoint",
    "endpoint_missing",
    "outcome_status_mismatch",
    "latency_missing",
    "ttft_missing",
)

#: 结构上**无法归属到任何主体**的原始诊断。普通主体一律 ``None``（无从测得），
#: **绝不写 0 冒充** —— 否则前后两次 DTO 的差值就能读出他人活动量或源文件损坏情况。
UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS = (
    "unknown_key",
    "malformed_lines",
    "content_field_rejected",
)

UNKNOWN_COUNTER_FIELDS = (
    SCOPED_UNKNOWN_COUNTER_FIELDS + UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS
)

#: usage DTO 的顶层必需字段。
USAGE_REQUIRED_FIELDS = (
    "kind",
    "source",
    "synthetic",
    "schema_version",
    "scope",
    "window",
    "page",
    "quality",
    "billing_basis",
    "counters",
    "metrics",
    "latency",
    "queue",
    "ttft",
    "tokens",
    "groups",
    "buckets",
    "unknown",
    "external_ui",
    "display_hints",
    "notes",
)


@dataclass(frozen=True)
class ObservabilityRequest:
    """一次监控请求。身份字段来自 BFF 会话，其余是展示 / 过滤参数。"""

    principal: RequesterPrincipal
    window_from: datetime
    window_to: datetime
    requested_uid: str | None = None
    requested_endpoint: str | None = None
    bucket: str = "1h"
    limit: int = DEFAULT_LIMIT
    offset: int = 0


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _validate_paging(limit: int, offset: int) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidRequest("limit must be an integer")
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidRequest("offset must be an integer")
    if limit < 1:
        raise InvalidRequest("limit must be >= 1")
    if limit > MAX_LIMIT:
        raise InvalidRequest(f"limit must be <= {MAX_LIMIT}")
    if offset < 0:
        raise InvalidRequest("offset must be >= 0")
    return limit, offset


def _page(items: list, limit: int, offset: int) -> tuple[list, dict]:
    total = len(items)
    window = items[offset : offset + limit]
    return window, {
        "limit": limit,
        "offset": offset,
        "total": total,
        "returned": len(window),
        "has_more": offset + len(window) < total,
    }


def _require_request(request: object) -> ObservabilityRequest:
    if not isinstance(request, ObservabilityRequest):
        raise InvalidRequest("request must be an ObservabilityRequest")
    return request


def _require_provenance(provenance: object) -> Provenance:
    if not isinstance(provenance, Provenance):
        raise InvalidRequest("provenance must be a Provenance")
    return provenance


def _bucket_label(moment: datetime, bucket: str) -> str:
    return iso_z(moment) if bucket != "1d" else moment.date().isoformat()


def _bucket_quality(observed_count: int, expected_count: int) -> str:
    if observed_count == 0:
        return QUALITY_UNKNOWN
    if observed_count < expected_count:
        return QUALITY_PARTIAL
    return QUALITY_MEASURED


def _block_quality(samples: list, missing: int) -> str:
    if not samples:
        return QUALITY_UNKNOWN
    return QUALITY_PARTIAL if missing else QUALITY_MEASURED


def _host_quality(host_report) -> str:
    """主机读数的整体质量。

    **不能**只看「有没有传 host_report」：未启用采集器时 `host_report` 是一个**内容
    全是 `not_collected`** 的对象，按「传了就是 measured」会渲染成一屏可信读数 ——
    正是本模块最不该犯的错。判据是读数本身。
    """
    if host_report is None:
        return QUALITY_UNKNOWN
    qualities: list[str] = []
    for reading in tuple(getattr(host_report, "node_readings", ())) + tuple(
        getattr(host_report, "driver_readings", ())
    ):
        qualities.append(reading.quality)
    for device in getattr(host_report, "devices", ()):
        for reading in getattr(device, "readings", ()):
            qualities.append(reading.quality)
    if not qualities:
        return QUALITY_UNKNOWN
    if all(quality == QUALITY_UNKNOWN for quality in qualities):
        return QUALITY_UNKNOWN
    if any(quality != QUALITY_MEASURED for quality in qualities):
        return QUALITY_PARTIAL
    return QUALITY_MEASURED


def _calls_quality(accumulator: UsageAccumulator) -> str:
    """聚合质量 —— 委派给 :meth:`UsageAccumulator.quality`，本模块只有那一处实现。"""
    return accumulator.quality()


# ---------------------------------------------------------------------------
# 归属 / 过滤（唯一一处计数入口）
# ---------------------------------------------------------------------------


@dataclass
class _Attribution:
    """一次归属扫描的产物。**所有**计数都在 :func:`_attribute` 里发生。"""

    visible: UsageAccumulator
    by_endpoint: dict
    by_uid: dict
    by_bucket: dict
    unknown: dict
    unattributable: dict
    foreign_skipped: int


def _attribute(
    ingest,
    *,
    scope: ObserverScope,
    directory: KeyDirectory,
    start: datetime,
    end: datetime,
    bucket: str,
    requested_endpoint: str | None,
    restrict_uid: str | None,
) -> _Attribution:
    """顺序即边界：归属 -> 窗口 -> 吊销 -> 端点 -> 计数。

    ``restrict_uid`` 是**唯一**决定「哪些行能被计数」的东西：

    * 本人 / 时间线视图传 ``scope.viewing_uid`` —— 凡不属于它的行在**任何计数器之前**
      就被丢弃，所以不可归属的行不会在响应里留下任何痕迹，响应也就对「日志里还有
      多少别人的行」逐字节不敏感；
    * 管理全局摘要传 ``None``（不限主体，但仍按 uid 归集）；
    * 管理摘要若**显式**指定了 uid，则传那个 uid —— 于是「查某人」得到的是某人的用量，
      而不是一个看起来像某人、内容却是全体的摘要。

    入参是**整个** :class:`IngestResult`（而不是 ``events``）：坏行与正文行计数发生在
    解析期，本函数必须把它们接过来，否则管理视角会显示 ``malformed_lines == 0`` ——
    一个「我数过，是零」的假结论。
    """
    unknown = {name: 0 for name in SCOPED_UNKNOWN_COUNTER_FIELDS}
    unattributable = {name: 0 for name in UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS}
    unattributable["malformed_lines"] = ingest.malformed_lines
    unattributable["content_field_rejected"] = ingest.content_field_rejected

    visible = UsageAccumulator()
    by_endpoint: dict[str, UsageAccumulator] = {}
    by_uid: dict[str, UsageAccumulator] = {}
    by_bucket: dict[datetime, UsageAccumulator] = {}
    foreign_skipped = 0

    visible_endpoints = frozenset(scope.endpoints)

    for event in ingest.events:
        binding = directory.lookup(event.key_id)
        if binding is None:
            # 键不在受信目录 —— 结构上没有主体可言，只能算全局原始诊断。
            unattributable["unknown_key"] += 1
            continue

        if restrict_uid is not None and binding.uid != restrict_uid:
            # 授权范围外：静默丢弃，不计数、不展示。
            foreign_skipped += 1
            continue

        if not within_window(event.ts, start, end):
            unknown["out_of_window"] += 1
            continue
        if binding.revoked:
            unknown["revoked_key"] += 1
            continue

        endpoint = event_endpoint(event)
        if endpoint is None:
            unknown["endpoint_missing"] += 1
            continue
        if requested_endpoint is not None and endpoint != requested_endpoint:
            unknown["filtered_out"] += 1
            continue
        if endpoint not in visible_endpoints:
            # 本人的行，但引用了本人当前无权使用的端点。计入一个**不带名字**的计数：
            # 既如实告知「有一部分没算进来」，又不会把未授权端点的 id 泄漏进响应。
            unknown["out_of_scope_endpoint"] += 1
            continue

        visible.add(event)
        by_endpoint.setdefault(endpoint, UsageAccumulator()).add(event)
        by_uid.setdefault(binding.uid, UsageAccumulator()).add(event)
        by_bucket.setdefault(bucket_start_of(event.ts, bucket), UsageAccumulator()).add(event)

    unknown["outcome_status_mismatch"] = visible.outcome_status_mismatch
    unknown["latency_missing"] = visible.latency_missing
    unknown["ttft_missing"] = visible.ttft_missing

    return _Attribution(
        visible=visible,
        by_endpoint=by_endpoint,
        by_uid=by_uid,
        by_bucket=by_bucket,
        unknown=unknown,
        unattributable=unattributable,
        foreign_skipped=foreign_skipped,
    )


def _apply_visibility_gates(unknown: dict, unattributable: dict, scope: ObserverScope) -> None:
    """不可归属的原始诊断：只在**管理 scope** 下露面，否则一律 ``None``。

    ``None`` 是「无从测得」的唯一合法表达；写 0 等于宣称「我数过，是零」。
    """
    unknown["unknown_key"] = unattributable["unknown_key"] if scope.admin else None
    unknown["malformed_lines"] = unattributable["malformed_lines"] if scope.admin else None
    unknown["content_field_rejected"] = (
        unattributable["content_field_rejected"] if scope.admin else None
    )


def _bucket_block(attribution: _Attribution, bucket: str, start, end, *, with_tokens: bool) -> dict:
    expected_count = len(expected_bucket_starts(start, end, bucket))
    items = []
    for moment in sorted(attribution.by_bucket):
        accumulator = attribution.by_bucket[moment]
        item = {
            "start": _bucket_label(moment, bucket),
            "counters": accumulator.counter_block(),
            "latency": accumulator.latency_block(),
            "queue": accumulator.queue_block(),
        }
        if with_tokens:
            item["tokens"] = accumulator.token_block()
        items.append(item)
    observed_count = len(items)
    return {
        "unit": bucket,
        "expected_count": expected_count,
        "observed_count": observed_count,
        "items": items,
        "_quality": _bucket_quality(observed_count, expected_count),
    }


def _envelope(
    *,
    kind: str,
    provenance: Provenance,
    scope: ObserverScope,
    start: datetime,
    end: datetime,
    bucket: str,
    page: dict,
) -> dict:
    return {
        "kind": kind,
        "source": provenance.source,
        "synthetic": provenance.synthetic,
        "schema_version": SCHEMA_VERSION,
        "scope": scope.to_dict(),
        "window": {
            "from": iso_z(start),
            "to": iso_z(end),
            "bucket": bucket,
            "timezone": "UTC",
            "hours": int((end - start).total_seconds() // 3600),
        },
        "page": page,
    }


def _external_ui(scope: ObserverScope) -> dict:
    """原生 UI（Grafana / Prometheus / EvalScope）的**授权入口**说明。

    **不直接开放任意 iframe。** 给出的是同源代理路径（``path``），必须经过 Portal 自己的
    会话授权；``external_url`` 恒为 ``null`` —— 平台**不**向浏览器下发上游地址，否则
    「谁能看哪块看板」就退化成一条可分享的裸链接。
    """
    catalog = (
        # 原生看板是管理面：整机读数对成员开放（门户内图表），原生 UI 不随之开放。
        ("grafana", "Grafana", "/observability/ui/grafana/", SCOPE_ADMIN_READ),
        ("prometheus", "Prometheus", "/observability/ui/prometheus/", SCOPE_ADMIN_READ),
        ("evalscope", "EvalScope", "/observability/ui/evalscope/", SCOPE_ADMIN_READ),
    )
    entries = []
    for entry_id, label, path, required in catalog:
        visible = scope.admin or required in scope.scopes
        entries.append(
            {
                "id": entry_id,
                "label": label,
                "path": path if visible else None,
                "requires": required,
                "visible": visible,
                "external_url": None,
            }
        )
    return {
        "policy": "same-origin-proxy-with-session-auth",
        "iframe_allowed": False,
        "iframe_reason": (
            "上游原生 UI 不以 iframe 嵌入：跨源 iframe 无法由本服务施加授权，"
            "且会把上游会话/Cookie 暴露在平台页面的源下。改为同源反向代理路径，"
            "由 Portal 会话授权后再转发。"
        ),
        "entries": entries,
        "notes": [
            "external_url 恒为 null：平台不下发任意外部地址，避免授权退化为可分享链接。",
            "Grafana 自身的匿名/组织权限仍由 Grafana 侧配置，本模块只负责入口授权。",
        ],
    }


# ---------------------------------------------------------------------------
# 视图：本人用量
# ---------------------------------------------------------------------------


def _resolve_and_validate(request, registry, *, require_admin: bool = False) -> tuple:
    scope = resolve_observer_scope(
        request.principal,
        available_endpoints=registry.endpoints(),
        requested_uid=request.requested_uid,
        requested_endpoint=request.requested_endpoint,
    )
    if require_admin and not scope.admin:
        # 管理视图**只**认显式 scope；role=admin 不是通行证。
        raise AccessDenied(f"this view requires {SCOPE_ADMIN_READ}")
    start, end = validate_window(request.window_from, request.window_to, request.bucket)
    limit, offset = _validate_paging(request.limit, request.offset)
    return scope, start, end, limit, offset


def build_self_usage_dto(
    request: ObservabilityRequest,
    text: str,
    directory: KeyDirectory,
    registry: EngineRegistry,
    provenance: Provenance,
    *,
    now: datetime,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """构建 ``usage.self`` DTO。"""
    _require_request(request)
    _require_provenance(provenance)
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")
    if not isinstance(registry, EngineRegistry):
        raise InvalidRequest("registry must be an EngineRegistry")

    scope, start, end, limit, offset = _resolve_and_validate(request, registry)

    ingest = ingest_jsonl(text, limits)
    attribution = _attribute(
        ingest,
        scope=scope,
        directory=directory,
        start=start,
        end=end,
        bucket=request.bucket,
        requested_endpoint=scope.requested_endpoint,
        restrict_uid=scope.viewing_uid,
    )
    unknown = attribution.unknown
    _apply_visibility_gates(unknown, attribution.unattributable, scope)

    visible = attribution.visible
    calls_quality = _calls_quality(visible)
    buckets = _bucket_block(attribution, request.bucket, start, end, with_tokens=False)
    bucket_quality = buckets.pop("_quality", QUALITY_UNKNOWN)

    groups_all = [
        {
            "id": endpoint,
            "display_name": _display_name_for(registry, endpoint),
            "counters": attribution.by_endpoint[endpoint].counter_block(),
            "latency": attribution.by_endpoint[endpoint].latency_block(),
            "queue": attribution.by_endpoint[endpoint].queue_block(),
        }
        for endpoint in sorted(attribution.by_endpoint)
    ]
    groups, page = _page(groups_all, limit, offset)

    dto = _envelope(
        kind=KIND_USAGE_SELF,
        provenance=provenance,
        scope=scope,
        start=start,
        end=end,
        bucket=request.bucket,
        page=page,
    )
    dto.update(
        {
            "quality": {
                "calls": calls_quality,
                "latency": _block_quality(visible.latency_samples, visible.latency_missing),
                "queue": _block_quality(visible.queue_samples, visible.queue_missing),
                "ttft": _block_quality(visible.ttft_samples, visible.ttft_missing),
                "buckets": bucket_quality,
                "truncated": False,
            },
            "billing_basis": "verified-key",
            "counters": visible.counter_block(),
            "metrics": visible.readings(source=provenance.source, now=now, quality=calls_quality),
            "latency": visible.latency_block(),
            "queue": visible.queue_block(),
            "ttft": visible.ttft_block(),
            "tokens": visible.token_block(),
            "groups": groups,
            "buckets": buckets,
            "unknown": unknown,
            "external_ui": _external_ui(scope),
            "display_hints": _usage_hints(provenance, scope, calls_quality, unknown),
            "notes": _usage_notes(),
        }
    )
    validate_usage_dto(dto)
    return dto


def _display_name_for(registry: EngineRegistry, endpoint_id: str) -> str | None:
    record = registry.by_endpoint(endpoint_id)
    return record.display_name if record is not None else None


def _usage_hints(
    provenance: Provenance, scope: ObserverScope, calls_quality: str, unknown: dict
) -> list:
    hints: list[str] = []
    if provenance.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：不得作为生产监控、计费或容量决策依据。")
    if calls_quality == QUALITY_UNKNOWN:
        hints.append(
            "本人用量在本窗口内无可用样本：显示为 UNKNOWN（不是 0）。"
            "空日志与未接线在数据上无法区分，因此不给出「0 次请求」这个结论。"
        )
    if unknown.get("out_of_scope_endpoint"):
        hints.append(
            f"有 {unknown['out_of_scope_endpoint']} 条本人调用引用了当前未授权的端点，"
            "已计入计数但**不列出端点名**。"
        )
    if unknown.get("revoked_key"):
        hints.append(f"有 {unknown['revoked_key']} 条调用使用了已吊销的 key，已排除。")
    if not scope.is_self_view:
        hints.append(
            f"当前为管理视角，查看对象是 {scope.viewing_uid}（不是会话本人 {scope.uid}）。"
        )
    hints.append(
        "本人用量按 **Gateway 已验证 key** 归属（billing_basis=verified-key）；"
        "与整卡 GPU 利用率/显存是两套口径。"
    )
    return hints


def _usage_notes() -> list:
    return [
        "归属只来自服务端可信 key 目录；label / 请求头 / 请求体里的自报字段一律忽略。",
        f"p50/p95 由筛选后每条原始请求样本计算（{PERCENTILE_METHOD}），不对各组 p95 求平均。",
        "缺测一律为 null（UNKNOWN），不以 0 代替。",
        "共享推理实例上不虚构分用户显存：按人计量只有已验证 key 的请求/token 口径。",
        "授权范围外的行既不计数也不展示；不可归属的原始诊断普通主体为 null。",
    ]


# ---------------------------------------------------------------------------
# 视图：管理全局摘要
# ---------------------------------------------------------------------------


def build_admin_overview_dto(
    request: ObservabilityRequest,
    text: str,
    directory: KeyDirectory,
    registry: EngineRegistry,
    provenance: Provenance,
    *,
    now: datetime,
    host_report=None,
    engine_probe: EngineProbe | None = None,
    service_metrics: dict | None = None,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """构建 ``usage.admin`` DTO。需要 ``observability:admin:read``。"""
    _require_request(request)
    _require_provenance(provenance)
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")
    if not isinstance(registry, EngineRegistry):
        raise InvalidRequest("registry must be an EngineRegistry")

    scope, start, end, limit, offset = _resolve_and_validate(
        request, registry, require_admin=True
    )

    ingest = ingest_jsonl(text, limits)
    attribution = _attribute(
        ingest,
        scope=scope,
        directory=directory,
        start=start,
        end=end,
        bucket=request.bucket,
        requested_endpoint=scope.requested_endpoint,
        # 没指定 uid -> 全体摘要；指定了 -> 只看那个人。
        restrict_uid=scope.requested_uid,
    )
    unknown = attribution.unknown
    # 管理视角：不可归属的诊断**可见**（这就是「Admin 可见 all」）。
    _apply_visibility_gates(unknown, attribution.unattributable, scope)

    overall = attribution.visible
    calls_quality = _calls_quality(overall)
    buckets = _bucket_block(attribution, request.bucket, start, end, with_tokens=False)
    bucket_quality = buckets.pop("_quality", QUALITY_UNKNOWN)

    principals_all = [
        {
            "uid": uid,
            "counters": attribution.by_uid[uid].counter_block(),
            "latency": attribution.by_uid[uid].latency_block(),
            "queue": attribution.by_uid[uid].queue_block(),
            "tokens": attribution.by_uid[uid].token_block(),
        }
        for uid in sorted(attribution.by_uid)
    ]
    principals, page = _page(principals_all, limit, offset)

    engines_all = _engine_status_list(scope, registry, engine_probe, provenance, now)
    engines, _engine_page = _page(engines_all, limit, offset)

    dto = _envelope(
        kind=KIND_USAGE_ADMIN,
        provenance=provenance,
        scope=scope,
        start=start,
        end=end,
        bucket=request.bucket,
        page=page,
    )
    dto.update(
        {
            "quality": {
                "calls": calls_quality,
                "buckets": bucket_quality,
                "host": _host_quality(host_report),
                "truncated": False,
            },
            "billing_basis": "verified-key",
            "counters": overall.counter_block(),
            "metrics": overall.readings(
                source=provenance.source, now=now, quality=calls_quality
            ),
            "latency": overall.latency_block(),
            "queue": overall.queue_block(),
            "ttft": overall.ttft_block(),
            "tokens": overall.token_block(),
            "principals": principals,
            "groups": [
                {
                    "id": endpoint,
                    "display_name": _display_name_for(registry, endpoint),
                    "counters": attribution.by_endpoint[endpoint].counter_block(),
                    "latency": attribution.by_endpoint[endpoint].latency_block(),
                }
                for endpoint in sorted(attribution.by_endpoint)
            ],
            "buckets": buckets,
            "engines": engines,
            "engines_total": len(engines_all),
            "host": host_report.to_dict() if host_report is not None else None,
            "services": dict(service_metrics) if service_metrics is not None else {},
            "unknown": unknown,
            "external_ui": _external_ui(scope),
            "display_hints": _admin_hints(
                provenance,
                host_report,
                quality=_host_quality(host_report),
                calls_quality=calls_quality,
                unattributable=attribution.unattributable,
            ),
            "notes": _admin_notes(),
        }
    )
    validate_admin_overview_dto(dto)
    return dto


def _admin_hints(
    provenance: Provenance,
    host_report,
    *,
    quality: str,
    calls_quality: str,
    unattributable: dict,
) -> list:
    hints: list[str] = []
    if provenance.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：不得作为生产监控、计费或容量决策依据。")
    if host_report is None:
        hints.append(
            "本次未提供主机读数：host 为 null。缺失不以 0 代替，前端应显示「不可得」。"
        )
    elif quality == QUALITY_UNKNOWN:
        # 有 host 对象但里面一条都没测到（本任务采集器未启用时的常态）——
        # 放任它被画成一屏正常读数，正是「UNKNOWN 被当成 0」的展示层版本。
        hints.append(
            "主机读数全部未测得（例如采集器未启用）：quality.host = UNKNOWN，"
            "前端应显示空态而不是 0 或绿色。"
        )
    if calls_quality == QUALITY_UNKNOWN:
        hints.append("本窗口内全体用量无可用样本：显示为 UNKNOWN（不是 0）。")
    if unattributable["unknown_key"]:
        hints.append(
            f"{unattributable['unknown_key']} 条调用使用了未注册 key，已计入 unknown_key。"
        )
    if unattributable["malformed_lines"]:
        hints.append(f"{unattributable['malformed_lines']} 行元数据损坏，已排除并计数。")
    if unattributable["content_field_rejected"]:
        hints.append(
            f"{unattributable['content_field_rejected']} 行含正文类字段，已拒绝"
            "（正文捕获与访问不在本任务范围）。"
        )
    hints.append(
        "整机 / 整卡读数（host）与按人用量（principals）是两套来源与两套口径，"
        "不得互相换算：共享实例的 GPU 显存无法按用户切分。"
    )
    return hints


def _admin_notes() -> list:
    return [
        "管理摘要可见全部主体与全部引擎；普通主体只可见自己被授权的端点。",
        "principals 只列出窗口内**有可归属调用**的主体；目录里存在但本窗口无调用的主体"
        "不出现，也不以 0 出现 —— 「没有用量」与「没有数据」在数据上无法区分。",
        "主机读数取自受限采集器（本任务生产不启用）；未接线时如实为 not_collected。",
        "服务健康（Prometheus/Grafana）只报可达性，不代理其 UI。",
        "所有时间窗有界，桶数有硬上限；Web 层不能提交任意 PromQL 或任意外部 URL。",
    ]


def _engine_status_list(scope, registry, engine_probe, provenance, now) -> list:
    probe = engine_probe if engine_probe is not None else UnavailableEngineProbe()
    return [
        probe_engine(record, probe, source=provenance.source, now=now).to_dict()
        for record in registry.visible_to(scope)
    ]


# ---------------------------------------------------------------------------
# 视图：引擎状态
# ---------------------------------------------------------------------------


def build_engine_status_dto(
    request: ObservabilityRequest,
    registry: EngineRegistry,
    provenance: Provenance,
    *,
    now: datetime,
    engine_probe: EngineProbe | None = None,
) -> dict:
    """构建 ``engine.status`` DTO。非管理主体只看得到自己被授权的引擎。"""
    _require_request(request)
    _require_provenance(provenance)
    if not isinstance(registry, EngineRegistry):
        raise InvalidRequest("registry must be an EngineRegistry")

    scope, start, end, limit, offset = _resolve_and_validate(request, registry)

    reports = _engine_status_list(scope, registry, engine_probe, provenance, now)
    items, page = _page(reports, limit, offset)
    statuses = [item["status"] for item in reports]

    if not statuses or all(status is None for status in statuses):
        # 一个都没测到（或没有可见引擎）-> unknown。报 partial 会暗示「至少有一部分
        # 是准的」，而这里一条结论都没有。
        quality = QUALITY_UNKNOWN
    elif any(status is None for status in statuses):
        quality = QUALITY_PARTIAL
    else:
        quality = QUALITY_MEASURED

    dto = _envelope(
        kind=KIND_ENGINE_STATUS,
        provenance=provenance,
        scope=scope,
        start=start,
        end=end,
        bucket=request.bucket,
        page=page,
    )
    dto.update(
        {
            "quality": {"engines": quality, "host": QUALITY_UNKNOWN, "truncated": False},
            "engines": items,
            "counts": {
                "total": len(reports),
                "up": sum(1 for status in statuses if status == "up"),
                "degraded": sum(1 for status in statuses if status == "degraded"),
                "down": sum(1 for status in statuses if status == "down"),
                # unknown 单列：把「没问到」折进 down 或 up 都是撒谎。
                "unknown": sum(1 for status in statuses if status is None),
            },
            "host": None,
            "external_ui": _external_ui(scope),
            "display_hints": _engine_hints(provenance, statuses),
            "notes": [
                "引擎可见性由服务端端点白名单决定；看不到的引擎在响应里不存在。",
                "探测无结论时状态为 unknown，**不**等同于 down。",
                "本视图不处理 Gateway 用量，因此没有 unknown 计数器族。",
            ],
        }
    )
    validate_engine_status_dto(dto)
    return dto


def _engine_hints(provenance: Provenance, statuses: list) -> list:
    hints: list[str] = []
    if provenance.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：引擎状态为合成值。")
    unknown_count = sum(1 for status in statuses if status is None)
    if unknown_count:
        hints.append(
            f"{unknown_count} 个引擎的探测无结论，状态显示为 UNKNOWN。"
            "这不表示它们不可用，只表示本次没问到。"
        )
    return hints


# ---------------------------------------------------------------------------
# 视图：时间线
# ---------------------------------------------------------------------------


def build_timeline_dto(
    request: ObservabilityRequest,
    text: str,
    directory: KeyDirectory,
    registry: EngineRegistry,
    provenance: Provenance,
    *,
    now: datetime,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """构建 ``timeline`` DTO：按桶的本人（或管理指定主体）用量序列。"""
    _require_request(request)
    _require_provenance(provenance)
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")
    if not isinstance(registry, EngineRegistry):
        raise InvalidRequest("registry must be an EngineRegistry")

    scope, start, end, limit, offset = _resolve_and_validate(request, registry)

    ingest = ingest_jsonl(text, limits)
    attribution = _attribute(
        ingest,
        scope=scope,
        directory=directory,
        start=start,
        end=end,
        bucket=request.bucket,
        requested_endpoint=scope.requested_endpoint,
        restrict_uid=scope.viewing_uid,
    )
    unknown = attribution.unknown
    _apply_visibility_gates(unknown, attribution.unattributable, scope)

    buckets = _bucket_block(attribution, request.bucket, start, end, with_tokens=True)
    bucket_quality = buckets.pop("_quality", QUALITY_UNKNOWN)
    series_all = buckets["items"]
    series, page = _page(series_all, limit, offset)

    dto = _envelope(
        kind=KIND_TIMELINE,
        provenance=provenance,
        scope=scope,
        start=start,
        end=end,
        bucket=request.bucket,
        page=page,
    )
    dto.update(
        {
            "quality": {"buckets": bucket_quality, "truncated": False},
            "billing_basis": "verified-key",
            "series": series,
            "coverage": {
                "unit": request.bucket,
                "expected_count": buckets["expected_count"],
                "observed_count": buckets["observed_count"],
                "missing_count": max(
                    0, buckets["expected_count"] - buckets["observed_count"]
                ),
                "basis": "expected-buckets-within-window",
            },
            "metrics": attribution.visible.readings(
                source=provenance.source,
                now=now,
                quality=_calls_quality(attribution.visible),
            ),
            "unknown": unknown,
            "display_hints": [
                "缺失的桶**不补 0**：它们根本不出现，只让 quality.buckets 变成 partial。",
            ]
            + (["SYNTHETIC / DEMO 数据。"] if provenance.synthetic else []),
            "notes": [
                "桶起点在 UTC 下取整；覆盖率分母是窗口内应有的桶数。",
                "缺测不补零，也不线性插值。",
            ],
        }
    )
    validate_timeline_dto(dto)
    return dto


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _fail(message: str) -> None:
    raise ContractError(message)


def _validate_reading(node: object, name: str) -> None:
    """逐条读数自检：信封齐备、UNKNOWN 不带值、物理读数不带主体。"""
    if not isinstance(node, dict):
        _fail(f"{name} must be an object")
    for key in (
        "metric_id",
        "scope",
        "unit",
        "physical",
        "quality",
        "display_quality",
        "source",
        "observed_at",
        "reason",
        "freshness",
    ):
        if key not in node:
            _fail(f"{name} is missing required field {key!r}")

    metric_id = node["metric_id"]
    spec = CATALOGUE.get(metric_id)
    if spec is None:
        _fail(f"{name}.metric_id {metric_id!r} is not in the catalogue")
    if node["scope"] != spec.scope:
        _fail(f"{name}.scope disagrees with the catalogue for {metric_id!r}")
    if node["unit"] != spec.unit:
        _fail(f"{name}.unit disagrees with the catalogue for {metric_id!r}")
    if not isinstance(node["physical"], bool) or node["physical"] != spec.physical:
        _fail(f"{name}.physical disagrees with the catalogue for {metric_id!r}")

    quality = node["quality"]
    if quality not in (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN):
        _fail(f"{name}.quality {quality!r} is not supported")

    present = [key for key in ("value", "state", "text") if key in node]
    if len(present) != 1:
        _fail(f"{name} must carry exactly one of value/state/text")

    held = node[present[0]]
    if quality == QUALITY_UNKNOWN:
        if held is not None:
            _fail(f"{name} is unknown but carries a non-null {present[0]} — UNKNOWN is not 0")
        if node["reason"] is None:
            _fail(f"{name} is unknown but carries no reason code")
        if node["display_quality"] != QUALITY_UNKNOWN:
            _fail(f"{name}.display_quality must be unknown when quality is unknown")
    else:
        if held is None:
            _fail(f"{name} claims {quality} but carries a null {present[0]}")
        if node["observed_at"] is None:
            _fail(f"{name} claims {quality} but carries no observed_at")
        if node["reason"] is not None:
            _fail(f"{name}.reason is only meaningful for unknown readings")

    if spec.scope in PHYSICAL_SCOPES and "uid" in node:
        # 物理读数**永远**不属于某个人。这条断言是「共享 GPU 不虚构分用户显存」的机器条件。
        _fail(f"{name}: a physical reading must not carry a uid")


def _validate_reading_map(node: object, name: str) -> None:
    if not isinstance(node, dict):
        _fail(f"{name} must be an object")
    for metric_id, reading in node.items():
        _validate_reading(reading, f"{name}.{metric_id}")
        if reading["metric_id"] != metric_id:
            _fail(f"{name}.{metric_id} key disagrees with its metric_id")


def _validate_scope_block(scope: object) -> None:
    if not isinstance(scope, dict):
        _fail("scope must be an object")
    for flag in ("admin", "host_visible", "cross_endpoint", "is_self_view"):
        if not isinstance(scope.get(flag), bool):
            _fail(f"scope.{flag} must be a boolean")
    if scope["admin"] and not scope["host_visible"]:
        _fail("scope.admin implies scope.host_visible")


def _validate_window_block(window: object) -> None:
    if not isinstance(window, dict):
        _fail("window must be an object")
    for key in ("from", "to", "bucket", "timezone", "hours"):
        if key not in window:
            _fail(f"window is missing {key!r}")
    if window["bucket"] not in SUPPORTED_BUCKETS:
        _fail(f"window.bucket {window['bucket']!r} is not supported")
    if window["timezone"] != "UTC":
        _fail("window.timezone must be UTC")
    if isinstance(window["hours"], bool) or not isinstance(window["hours"], int):
        _fail("window.hours must be an integer")
    if window["hours"] < 0:
        _fail("window.hours must not be negative")


def _validate_page_block(page: object) -> None:
    if not isinstance(page, dict):
        _fail("page must be an object")
    for key in ("limit", "offset", "total", "returned", "has_more"):
        if key not in page:
            _fail(f"page is missing {key!r}")
    for key in ("limit", "offset", "total", "returned"):
        value = page[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"page.{key} must be a non-negative integer")
    if page["limit"] > MAX_LIMIT:
        _fail(f"page.limit must not exceed {MAX_LIMIT}")
    # 唯一正确的切片不变式。注意 ``offset`` **可以**超过 ``total`` —— 那是一次合法的
    # 「翻到末尾之后」，结果是空页；把它当错误会逼调用方自己算边界。
    expected_returned = min(page["limit"], max(0, page["total"] - page["offset"]))
    if page["returned"] != expected_returned:
        _fail("page.returned is not the slice of [offset, offset+limit) over total")
    expected_has_more = page["offset"] + page["returned"] < page["total"]
    if bool(page["has_more"]) != expected_has_more:
        _fail("page.has_more disagrees with offset/returned/total")


def _validate_unknown_block(unknown: object, scope: dict) -> None:
    if not isinstance(unknown, dict):
        _fail("unknown must be an object")
    for key in SCOPED_UNKNOWN_COUNTER_FIELDS:
        value = unknown.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(
                f"unknown.{key} is scoped to the visible principal and must be a "
                f"measured non-negative integer (0 is fine, null is not)"
            )
    for key in UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS:
        value = unknown.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"unknown.{key} must be null (no visibility) or a non-negative integer")
    if not scope["admin"]:
        for key in UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS:
            if unknown.get(key) is not None:
                _fail(f"unknown.{key} must be null without {SCOPE_ADMIN_READ}")


def _validate_metric_semantics(dto: dict) -> None:
    """物理读数与按人读数不得互相冒充。"""
    metrics = dto.get("metrics")
    if isinstance(metrics, dict):
        _validate_reading_map(metrics, "metrics")
    if dto.get("billing_basis") is not None and dto["billing_basis"] != "verified-key":
        _fail("billing_basis must be 'verified-key' when present")
    host = dto.get("host")
    if host is None:
        return
    if not isinstance(host, dict):
        _fail("host must be an object or null")
    for section in ("node", "driver"):
        _validate_reading_map(host.get(section, {}), f"host.{section}")
    devices = host.get("devices")
    if not isinstance(devices, list):
        _fail("host.devices must be a list")
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            _fail(f"host.devices[{index}] must be an object")
        if device.get("scope") != "device":
            _fail(f"host.devices[{index}].scope must be 'device'")
        if "uid" in device:
            _fail(f"host.devices[{index}]: a device block must not carry a uid")
        _validate_reading_map(device.get("readings", {}), f"host.devices[{index}].readings")


def _validate_envelope(dto: dict, kind: str) -> None:
    if not isinstance(dto, dict):
        _fail("DTO must be an object")
    if dto.get("schema_version") != SCHEMA_VERSION:
        _fail("DTO schema_version mismatch")
    if dto.get("kind") != kind:
        _fail(f"DTO kind must be {kind!r}")
    if not isinstance(dto.get("synthetic"), bool):
        _fail("DTO.synthetic must be a boolean")
    if not isinstance(dto.get("source"), str) or not dto["source"]:
        _fail("DTO.source must be a non-empty string")
    _validate_scope_block(dto.get("scope"))
    _validate_window_block(dto.get("window"))
    _validate_page_block(dto.get("page"))
    for key in ("display_hints", "notes"):
        value = dto.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            _fail(f"{key} must be a list of strings")
    if dto.get("synthetic") and not any(
        "SYNTHETIC" in hint for hint in dto.get("display_hints", [])
    ):
        # 合成数据必须在展示层明说，否则前端无从判断「这是不是真的」。
        _fail("a synthetic DTO must say so in display_hints")


def validate_usage_dto(dto: dict) -> None:
    """``usage.self`` / ``usage.admin`` 的结构自检。"""
    kind = dto.get("kind")
    if kind not in (KIND_USAGE_SELF, KIND_USAGE_ADMIN):
        _fail("validate_usage_dto expects a usage DTO")
    _validate_envelope(dto, kind)
    for key in USAGE_REQUIRED_FIELDS:
        if key not in dto:
            _fail(f"DTO is missing required field {key!r}")
    _validate_unknown_block(dto["unknown"], dto["scope"])
    _validate_metric_semantics(dto)

    quality = dto.get("quality")
    if not isinstance(quality, dict) or "calls" not in quality:
        _fail("quality.calls is required")
    if quality["calls"] not in (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN):
        _fail("quality.calls is not a supported value")
    _validate_stat_block(dto.get("latency"), "latency")
    _validate_stat_block(dto.get("queue"), "queue")
    _validate_stat_block(dto.get("ttft"), "ttft")
    _validate_bucket_block(dto.get("buckets"))
    for key in ("counters", "tokens"):
        if not isinstance(dto.get(key), dict):
            _fail(f"{key} must be an object")
    if not isinstance(dto.get("groups"), list):
        _fail("groups must be a list")
    if kind == KIND_USAGE_ADMIN:
        for key in ("principals", "engines"):
            if not isinstance(dto.get(key), list):
                _fail(f"an admin DTO must carry a {key} list")
    _validate_external_ui(dto.get("external_ui"), dto["scope"])


def validate_admin_overview_dto(dto: dict) -> None:
    if dto.get("kind") != KIND_USAGE_ADMIN:
        _fail("validate_admin_overview_dto expects kind=usage.admin")
    validate_usage_dto(dto)
    if not dto["scope"]["admin"]:
        _fail("an admin overview DTO must carry scope.admin")
    if dto.get("host") is not None and not dto["scope"]["host_visible"]:
        _fail("a host block requires scope.host_visible")
    _validate_host_quality(dto)


def _validate_host_quality(dto: dict) -> None:
    """``quality.host`` 必须与 ``host`` 里的读数一致 —— 不能「有对象就是 measured」。"""
    host = dto.get("host")
    reported = dto["quality"].get("host")
    if reported not in (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN):
        _fail("quality.host is not a supported value")
    if host is None:
        if reported != QUALITY_UNKNOWN:
            _fail("quality.host must be unknown when there is no host block")
        return

    qualities: list[str] = []
    for section in ("node", "driver"):
        for reading in host.get(section, {}).values():
            qualities.append(reading["quality"])
    for device in host.get("devices", []):
        for reading in device.get("readings", {}).values():
            qualities.append(reading["quality"])

    if not qualities:
        expected = QUALITY_UNKNOWN
    elif all(quality == QUALITY_UNKNOWN for quality in qualities):
        expected = QUALITY_UNKNOWN
    elif any(quality != QUALITY_MEASURED for quality in qualities):
        expected = QUALITY_PARTIAL
    else:
        expected = QUALITY_MEASURED
    if reported != expected:
        _fail(
            f"quality.host is {reported!r} but the host readings imply {expected!r} — "
            "a panel of UNKNOWN readings must never be presented as measured"
        )


def validate_engine_status_dto(dto: dict) -> None:
    _validate_envelope(dto, KIND_ENGINE_STATUS)
    for key in ("engines", "counts", "quality", "external_ui"):
        if key not in dto:
            _fail(f"DTO is missing required field {key!r}")
    if not isinstance(dto["engines"], list):
        _fail("engines must be a list")
    counts = dto["counts"]
    if not isinstance(counts, dict):
        _fail("counts must be an object")
    for key in ("total", "up", "degraded", "down", "unknown"):
        value = counts.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"counts.{key} must be a non-negative integer")
    if counts["up"] + counts["degraded"] + counts["down"] + counts["unknown"] != counts["total"]:
        _fail("counts must partition the total")
    _validate_external_ui(dto.get("external_ui"), dto["scope"])


def validate_timeline_dto(dto: dict) -> None:
    _validate_envelope(dto, KIND_TIMELINE)
    for key in ("series", "coverage", "metrics", "unknown"):
        if key not in dto:
            _fail(f"DTO is missing required field {key!r}")
    if not isinstance(dto["series"], list):
        _fail("series must be a list")
    _validate_reading_map(dto["metrics"], "metrics")
    _validate_unknown_block(dto["unknown"], dto["scope"])

    coverage = dto["coverage"]
    if not isinstance(coverage, dict):
        _fail("coverage must be an object")
    for key in ("expected_count", "observed_count", "missing_count"):
        value = coverage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"coverage.{key} must be a non-negative integer")
    if coverage["observed_count"] > coverage["expected_count"]:
        _fail("coverage.observed_count must not exceed expected_count")
    if coverage["observed_count"] < len(dto["series"]):
        _fail("coverage.observed_count disagrees with the series length")
    if coverage["missing_count"] != max(
        0, coverage["expected_count"] - coverage["observed_count"]
    ):
        _fail("coverage.missing_count must equal expected - observed (clamped at 0)")


def _validate_stat_block(block: object, name: str) -> None:
    if not isinstance(block, dict):
        _fail(f"{name} must be an object")
    samples = block.get("samples")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 0:
        _fail(f"{name}.samples must be a non-negative integer")
    for key in ("p50_ms", "p95_ms"):
        value = block.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            _fail(f"{name}.{key} must be a number or null")
        if samples == 0 and value is not None:
            # 没有样本就必须是 UNKNOWN —— 绝不允许用 0 顶替。
            _fail(f"{name}.{key} must be null when samples == 0")
        if samples > 0 and value is None:
            _fail(f"{name}.{key} must not be null when samples > 0")


def _validate_bucket_block(block: object) -> None:
    if not isinstance(block, dict):
        _fail("buckets must be an object")
    if block.get("unit") not in SUPPORTED_BUCKETS:
        _fail("buckets.unit is not supported")
    for key in ("expected_count", "observed_count"):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"buckets.{key} must be a non-negative integer")
    if block["observed_count"] > block["expected_count"]:
        _fail("buckets.observed_count must not exceed expected_count")
    if not isinstance(block.get("items"), list):
        _fail("buckets.items must be a list")
    if len(block["items"]) != block["observed_count"]:
        _fail("buckets.observed_count disagrees with the items list")


def _validate_external_ui(block: object, scope: dict) -> None:
    if not isinstance(block, dict):
        _fail("external_ui must be an object")
    if block.get("iframe_allowed") is not False:
        # 结构上禁止嵌入任意 iframe：一旦允许，授权就由上游页面而非本服务决定。
        _fail("external_ui.iframe_allowed must be False")
    if not isinstance(block.get("policy"), str) or not block["policy"]:
        _fail("external_ui.policy must be a non-empty string")
    entries = block.get("entries")
    if not isinstance(entries, list):
        _fail("external_ui.entries must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("each external_ui entry must be an object")
        if entry.get("external_url") is not None:
            _fail("external_ui entries must never carry an external_url")
        if entry.get("visible") and not isinstance(entry.get("path"), str):
            _fail("a visible external_ui entry must carry a same-origin path")
        if not entry.get("visible") and entry.get("path") is not None:
            _fail("an invisible external_ui entry must not leak its path")
        if entry.get("id") == "evalscope" and entry.get("visible") and not scope.get("admin"):
            _fail("evalscope is admin-only")


__all__ = [
    "ALL_KINDS",
    "DEFAULT_LIMIT",
    "KIND_ENGINE_STATUS",
    "KIND_TIMELINE",
    "KIND_USAGE_ADMIN",
    "KIND_USAGE_SELF",
    "MAX_LIMIT",
    "ObservabilityRequest",
    "SCOPED_UNKNOWN_COUNTER_FIELDS",
    "UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS",
    "UNKNOWN_COUNTER_FIELDS",
    "USAGE_REQUIRED_FIELDS",
    "build_admin_overview_dto",
    "build_engine_status_dto",
    "build_self_usage_dto",
    "build_timeline_dto",
    "validate_admin_overview_dto",
    "validate_engine_status_dto",
    "validate_timeline_dto",
    "validate_usage_dto",
]
