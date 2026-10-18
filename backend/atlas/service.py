"""Atlas 聚合入口：受信参数 + 元数据文本 -> AtlasDTO（纯函数）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .dto import (
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    REQUIRED_TOP_LEVEL_FIELDS,
    SCHEMA_VERSION,
    SCOPED_UNKNOWN_COUNTER_FIELDS,
    UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS,
    Provenance,
)
from .errors import ContractError, InvalidBucket, InvalidRequest, InvalidWindow
from .identity import (
    AuthorizedScope,
    GatewayKeyBinding,
    KeyDirectory,
    resolve_authorized_scope,
)
from .ingest import IngestLimits, IngestResult, RawEvent, ingest_jsonl
from .stats import PERCENTILE_METHOD, StatsAccumulator

#: 窗口最大跨度（小时）。32 天 > 31 天，留一天余量给「月度视图 + 时区边界」。
MAX_WINDOW_HOURS = 24 * 32

#: 受信项目下「没有经审批的工作负载映射」时使用的分组 id。
UNATTRIBUTED_WORKLOAD = "unattributed"

SUPPORTED_BUCKETS = ("1h", "1d")

_BUCKET_STEP = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True)
class AtlasRequest:
    """一次聚合请求。身份字段来自 BFF 会话，其余是展示/过滤参数。"""

    requester: RequesterContext
    requested_subject: str | None
    requested_project: str | None
    window_from: datetime
    window_to: datetime
    bucket: str = "1h"
    model_filter: str | None = None


def validate_window(
    window_from: datetime,
    window_to: datetime,
    bucket: str,
    max_window_hours: int = MAX_WINDOW_HOURS,
) -> tuple[datetime, datetime]:
    """校验窗口/粒度并归一化到 UTC。失败抛 InvalidWindow / InvalidBucket。"""
    for value, name in ((window_from, "from"), (window_to, "to")):
        if not isinstance(value, datetime):
            raise InvalidWindow(f"window {name} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidWindow(f"window {name} must carry an explicit UTC offset")

    if bucket not in SUPPORTED_BUCKETS:
        raise InvalidBucket(f"unsupported bucket: {bucket!r}")

    start = window_from.astimezone(timezone.utc)
    end = window_to.astimezone(timezone.utc)
    if end <= start:
        raise InvalidWindow("window must be a non-empty half-open interval [from, to)")

    hours = (end - start).total_seconds() / 3600
    if hours > max_window_hours:
        raise InvalidWindow(f"window exceeds max_window_hours={max_window_hours}")
    return start, end


def expected_bucket_starts(
    window_from: datetime, window_to: datetime, bucket: str
) -> list[datetime]:
    """窗口内应有的桶起点列表（UTC）。用于算出覆盖率，而不是伪造零值点。"""
    if bucket not in SUPPORTED_BUCKETS:
        raise InvalidBucket(f"unsupported bucket: {bucket!r}")
    step = _BUCKET_STEP[bucket]
    current = window_from.astimezone(timezone.utc)
    if bucket == "1h":
        current = current.replace(minute=0, second=0, microsecond=0)
    else:
        current = current.replace(hour=0, minute=0, second=0, microsecond=0)

    starts: list[datetime] = []
    while current < window_to:
        starts.append(current)
        current = current + step
    return starts


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_floor(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _stats_entry(node_id: str, accumulator: StatsAccumulator) -> dict:
    entry = {"id": node_id}
    entry.update(accumulator.counter_block())
    entry["latency"] = accumulator.latency_block()
    entry["ttft"] = accumulator.ttft_block()
    entry["tokens"] = accumulator.token_block()
    return entry


def _bucket_entry(start: datetime, accumulator: StatsAccumulator, bucket: str) -> dict:
    entry = {"start": _iso_z(start) if bucket == "1h" else start.date().isoformat()}
    entry.update(accumulator.counter_block())
    entry["latency"] = accumulator.latency_block()
    entry["ttft"] = accumulator.ttft_block()
    return entry


@dataclass(frozen=True)
class VisibleRecords:
    """一次请求里**已通过授权判定**的那部分记录（§3.5 的产物）。

    ``visible`` 里的每一对都满足：``key_id`` 在受信目录里、``project`` 在本次
    授权白名单里、落在窗口内、未被吊销、且没被 ``model_filter`` 排除。
    换言之 —— 这些是**唯一**允许进入任何聚合的行。
    """

    scope: AuthorizedScope
    start: datetime
    end: datetime
    visible: tuple[tuple[RawEvent, GatewayKeyBinding], ...]
    #: 可归属到可见项目的计数器（``0`` 是**数过**的零）。
    unknown: dict[str, int]
    #: 无法归属到任何项目的原始诊断（未授权时为 ``null``）。
    unattributable: dict[str, int]
    ingest: IngestResult


def collect_authorized_records(
    request: AtlasRequest,
    text: str,
    directory: KeyDirectory,
    limits: IngestLimits = IngestLimits(),
) -> VisibleRecords:
    """执行契约 §3.5 的判定顺序，返回本次授权可见的行。

    **这个顺序是安全边界，不是风格问题。** 「归属 -> 窗口 -> 吊销 -> 端点 -> 计数」
    之中，归属判定必须早于窗口/吊销判定：否则 ``out_of_window`` / ``revoked_key``
    就变成了全局计数，一个只有单项目授权的 viewer 仅凭前后两次 DTO 的差值就能读出
    其他租户的活动量（基线 ``73f2d15`` 的 P0 缺陷形态）。

    之所以把它抽成一个函数：聚合视图不止一种（项目汇总、模型分布、7×24 热力图），
    而**授权判定的实现只允许有一处**。两份拷贝迟早会漂移，而这里漂移的代价是一条
    跨租户侧信道。
    """
    scope = resolve_authorized_scope(
        request.requester, request.requested_subject, request.requested_project
    )
    start, end = validate_window(request.window_from, request.window_to, request.bucket)
    ingest = ingest_jsonl(text, limits)

    authorized_projects = frozenset(scope.projects)
    unknown = {name: 0 for name in SCOPED_UNKNOWN_COUNTER_FIELDS}
    unattributable = {name: 0 for name in UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS}
    unattributable["malformed_lines"] = ingest.malformed_lines
    unattributable["content_field_rejected"] = ingest.content_field_rejected

    visible: list[tuple[RawEvent, GatewayKeyBinding]] = []
    for event in ingest.events:
        binding = directory.lookup(event.key_id)
        if binding is None:
            # 键不在受信目录 —— 结构上没有项目可言，只能算全局原始诊断。
            unattributable["unknown_key"] += 1
            continue
        if binding.project not in authorized_projects:
            continue  # 授权范围外：静默丢弃，不计数、不展示
        if not (start <= event.ts < end):
            unknown["out_of_window"] += 1
            continue
        if binding.revoked:
            unknown["revoked_key"] += 1
            continue
        if request.model_filter is not None and event.model != request.model_filter:
            unknown["filtered_out"] += 1
            continue
        visible.append((event, binding))

    return VisibleRecords(
        scope=scope,
        start=start,
        end=end,
        visible=tuple(visible),
        unknown=unknown,
        unattributable=unattributable,
        ingest=ingest,
    )


def build_atlas_dto(
    request: AtlasRequest,
    text: str,
    directory: KeyDirectory,
    provenance: Provenance,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """构建 AtlasDTO。纯函数：同输入必得同输出。

    授权范围之外的行**既不计数也不展示** —— 避免用计数侧信道推断他人流量。
    """
    if not isinstance(request, AtlasRequest):
        raise InvalidRequest("request must be an AtlasRequest")
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")
    if not isinstance(provenance, Provenance):
        raise InvalidRequest("provenance must be a Provenance")
    if not provenance.source.strip():
        raise InvalidRequest("provenance.source must not be empty")

    records = collect_authorized_records(request, text, directory, limits)
    scope, start, end = records.scope, records.start, records.end
    ingest = records.ingest
    global_diag = scope.global_diagnostics_visible

    # 先只累加「可归属」的那一半；不可归属的原始诊断在最后按 scope 决定露面还是为 null。
    unknown = dict(records.unknown)
    unattributable = records.unattributable

    visible = StatsAccumulator()
    by_project: dict[str, StatsAccumulator] = {}
    by_workload: dict[tuple[str, str], StatsAccumulator] = {}
    by_hour: dict[datetime, StatsAccumulator] = {}
    by_day: dict[datetime, StatsAccumulator] = {}

    for event, binding in records.visible:
        workload_id = binding.workload or UNATTRIBUTED_WORKLOAD

        visible.add(event)
        by_project.setdefault(binding.project, StatsAccumulator()).add(event)
        by_workload.setdefault((binding.project, workload_id), StatsAccumulator()).add(event)
        by_hour.setdefault(
            event.ts.replace(minute=0, second=0, microsecond=0), StatsAccumulator()
        ).add(event)
        by_day.setdefault(_day_floor(event.ts), StatsAccumulator()).add(event)

    unknown["outcome_status_mismatch"] = visible.outcome_status_mismatch
    unknown["latency_missing"] = visible.latency_missing
    unknown["ttft_missing"] = visible.ttft_missing

    # 不可归属的原始诊断：只在**显式**授权下露面，否则一律 None（不是 0）。
    #
    # 两类分开授权，因为它们回答的是不同的问题：
    # - 「哪些请求归不了属」（unknown_key）→ atlas:usage:unattributed
    # - 「上游把什么不该来的东西写进了日志」（损坏行 / 正文行）→ atlas:usage:global-diagnostics
    unknown["unknown_key"] = (
        unattributable["unknown_key"] if scope.unattributed_visible else None
    )
    unknown["malformed_lines"] = (
        unattributable["malformed_lines"] if global_diag else None
    )
    unknown["content_field_rejected"] = (
        unattributable["content_field_rejected"] if global_diag else None
    )

    groups = []
    for project_id in sorted(by_project):
        group = _stats_entry(project_id, by_project[project_id])
        group["workloads"] = [
            _stats_entry(workload_id, by_workload[(project_id, workload_id)])
            for workload_id in sorted(
                wl for (proj, wl) in by_workload if proj == project_id
            )
        ]
        groups.append(group)

    expected_starts = expected_bucket_starts(start, end, request.bucket)
    hourly = [_bucket_entry(hour, by_hour[hour], "1h") for hour in sorted(by_hour)]
    daily = [_bucket_entry(day, by_day[day], "1d") for day in sorted(by_day)]

    if request.bucket == "1h":
        expected_count, observed_count = len(expected_starts), len(hourly)
    else:
        expected_count, observed_count = len(expected_starts), len(daily)

    if observed_count == 0:
        hourly_quality = QUALITY_UNKNOWN
    elif observed_count < expected_count:
        hourly_quality = QUALITY_PARTIAL
    else:
        hourly_quality = QUALITY_MEASURED

    untrusted_lines = ingest.malformed_lines + ingest.content_field_rejected

    if global_diag:
        calls_quality = QUALITY_MEASURED if untrusted_lines == 0 else QUALITY_PARTIAL
    else:
        # 看不到源文件级诊断时，**不能**报 measured —— 那等于断言「源文件没有坏行」，
        # 也就是把一个自己无从测得的 0 伪造成结论。宁可 unknown。
        calls_quality = QUALITY_UNKNOWN

    dto = {
        "source": provenance.source,
        "synthetic": provenance.synthetic,
        "schema_version": SCHEMA_VERSION,
        "scope": scope.to_dict(),
        "window": {
            "from": _iso_z(start),
            "to": _iso_z(end),
            "bucket": request.bucket,
            "timezone": "UTC",
            "hours": int((end - start).total_seconds() // 3600),
        },
        "quality": {
            "calls": calls_quality,
            "latency": visible.quality_latency(),
            "ttft": visible.quality_ttft(),
            "tokens": visible.quality_tokens(),
            "hourly": hourly_quality,
            # 源文件级计数与 `unknown` 里的同名项同门同锁：无授权即 None。
            "malformed_lines": unknown["malformed_lines"],
            "content_field_rejected": unknown["content_field_rejected"],
            "truncated": False,
        },
        "totals": visible.counter_block(),
        "latency": visible.latency_block(),
        "ttft": visible.ttft_block(),
        "tokens": visible.token_block(),
        "groups": groups,
        "buckets": {
            "unit": request.bucket,
            "expected_count": expected_count,
            "observed_count": observed_count,
            "hourly": hourly,
            "daily": daily,
        },
        "unknown": unknown,
        "unattributed": {
            "visible": scope.unattributed_visible,
            "workload_calls": sum(
                accumulator.calls
                for (project, workload), accumulator in by_workload.items()
                if workload == UNATTRIBUTED_WORKLOAD
            ),
            # 「未归属项目」的调用数 = 未注册 key + 已吊销 key。
            # 未注册 key 是全局量，所以这项整体只对持有 atlas:usage:unattributed 的主体可见；
            # 没有该 scope 时不输出（None），不输出一个「只看得到一半」的数字。
            "project_calls": (
                unattributable["unknown_key"] + unknown["revoked_key"]
                if scope.unattributed_visible
                else None
            ),
        },
        "display_hints": _display_hints(
            provenance, ingest, hourly_quality, observed_count, expected_count, scope
        ),
        "notes": [
            "p50/p95 由筛选后成功且测得正值的每条原始请求样本计算，"
            f"不对各组 p95 求平均（{PERCENTILE_METHOD}）。",
            "label / 请求体自报字段不参与授权与分组；工作负载只来自服务端经审批映射。",
            "缺测一律为 null（UNKNOWN），不以 0 代替。",
            "本视图是「被授权项目汇总」：同一项目下其他 principal 持有的 key 也一并计量；"
            "当前**没有**「我的调用」这种按 principal 过滤的口径。",
            "无法归属到可见项目的原始诊断计数（未注册 key / 损坏行 / 正文行）"
            "默认不输出（null），需显式 scope；它们不以 0 代替。",
        ],
    }
    validate_dto(dto)
    return dto


def _display_hints(
    provenance: Provenance,
    ingest,
    hourly_quality: str,
    observed_count: int,
    expected_count: int,
    scope,
) -> list[str]:
    hints: list[str] = []
    if provenance.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：不得作为生产监控或容量决策依据。")
    if hourly_quality == QUALITY_UNKNOWN:
        hints.append(
            "hourly: unknown —— 窗口内没有可用的按小时日志，保持空态，不渲染 7×24 热力图。"
        )
    elif hourly_quality == QUALITY_PARTIAL:
        hints.append(
            f"hourly 覆盖率 partial：观察到 {observed_count} / 应有 {expected_count} 个桶。"
        )
    if scope.global_diagnostics_visible:
        if ingest.malformed_lines:
            hints.append(f"输入含 {ingest.malformed_lines} 行损坏元数据，已排除并计数。")
        if ingest.content_field_rejected:
            hints.append(
                f"输入含 {ingest.content_field_rejected} 行正文类字段（prompt/completion），已拒绝。"
            )
    else:
        # 未授权时连「有没有坏行」都不能暗示 —— 只说「这项测不了」，不报任何数字。
        hints.append(
            "源文件级诊断（quality.calls 等）未获授权，当前为 unknown；"
            "需要时请申请 atlas:usage:global-diagnostics。"
        )
    if scope.unattributed_visible:
        hints.append("unattributed（无可信项目归属）计数对本主体可见。")
    else:
        hints.append("unattributed（无可信项目归属）计数已按权限隐藏。")
    return hints


def _validate_stat_block(block: dict, name: str) -> None:
    if not isinstance(block, dict):
        raise ContractError(f"{name} must be an object")
    samples = block.get("samples")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 0:
        raise ContractError(f"{name}.samples must be a non-negative integer")
    for key in ("p50_ms", "p95_ms"):
        value = block.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ContractError(f"{name}.{key} must be a number or null")
        if samples == 0 and value is not None:
            # 没有样本就必须是 UNKNOWN —— 绝不允许用 0 顶替。
            raise ContractError(f"{name}.{key} must be null when samples == 0")
        if samples > 0 and value is None:
            raise ContractError(f"{name}.{key} must not be null when samples > 0")


def validate_dto(dto: dict) -> None:
    """结构自检：字段齐备、类型正确、UNKNOWN 不得被 0 顶替。"""
    if not isinstance(dto, dict):
        raise ContractError("DTO must be an object")
    for key in REQUIRED_TOP_LEVEL_FIELDS:
        if key not in dto:
            raise ContractError(f"DTO is missing required field {key!r}")
    if dto["schema_version"] != SCHEMA_VERSION:
        raise ContractError("DTO schema_version mismatch")
    if not isinstance(dto["synthetic"], bool):
        raise ContractError("DTO synthetic must be a boolean")
    if not isinstance(dto["source"], str) or not dto["source"]:
        raise ContractError("DTO source must be a non-empty string")

    _validate_stat_block(dto["latency"], "latency")
    _validate_stat_block(dto["ttft"], "ttft")

    scope_block = dto["scope"]
    if not isinstance(scope_block, dict):
        raise ContractError("scope must be an object")
    for flag in ("unattributed_visible", "global_diagnostics_visible", "cross_project"):
        if not isinstance(scope_block.get(flag), bool):
            raise ContractError(f"scope.{flag} must be a boolean")

    unknown = dto["unknown"]
    if not isinstance(unknown, dict):
        raise ContractError("unknown must be an object")
    for key in SCOPED_UNKNOWN_COUNTER_FIELDS:
        value = unknown.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(
                f"unknown.{key} is scoped to the visible projects and must be a "
                f"measured non-negative integer (0 is fine, null is not)"
            )
    for key in UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS:
        value = unknown.get(key)
        if value is None:
            continue  # 无授权 —— 唯一合法的「不知道」表达
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(
                f"unknown.{key} must be null (no visibility) or a non-negative integer"
            )
    # 门禁一致性：没有对应 scope 就**必须**是 null，绝不能悄悄漏出一个全局数字。
    if not scope_block["unattributed_visible"] and unknown.get("unknown_key") is not None:
        raise ContractError(
            "unknown.unknown_key must be null without atlas:usage:unattributed"
        )
    if not scope_block["global_diagnostics_visible"]:
        for key in ("malformed_lines", "content_field_rejected"):
            if unknown.get(key) is not None:
                raise ContractError(
                    f"unknown.{key} must be null without atlas:usage:global-diagnostics"
                )

    quality = dto["quality"]
    if not isinstance(quality, dict):
        raise ContractError("quality must be an object")
    if quality.get("calls") not in (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN):
        raise ContractError("quality.calls is not a supported value")
    for key in ("malformed_lines", "content_field_rejected"):
        if quality.get(key) != unknown.get(key):
            raise ContractError(f"quality.{key} must mirror unknown.{key}")
    if scope_block["global_diagnostics_visible"]:
        if quality.get("calls") == QUALITY_UNKNOWN:
            raise ContractError(
                "quality.calls must be measured/partial with atlas:usage:global-diagnostics"
            )
    elif quality.get("calls") != QUALITY_UNKNOWN:
        # 看不到源文件级诊断却报 measured —— 等于把无从测得的 0 说成结论。
        raise ContractError(
            "quality.calls must be unknown without atlas:usage:global-diagnostics"
        )

    buckets = dto["buckets"]
    if not isinstance(buckets, dict):
        raise ContractError("buckets must be an object")
    for key in ("hourly", "daily"):
        if not isinstance(buckets.get(key), list):
            raise ContractError(f"buckets.{key} must be a list")
    if buckets.get("unit") not in SUPPORTED_BUCKETS:
        raise ContractError("buckets.unit is not supported")
    for key in ("expected_count", "observed_count"):
        value = buckets.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"buckets.{key} must be a non-negative integer")
    if buckets["observed_count"] > buckets["expected_count"]:
        raise ContractError("buckets.observed_count must not exceed expected_count")
    observed_list = buckets["hourly"] if buckets["unit"] == "1h" else buckets["daily"]
    if len(observed_list) != buckets["observed_count"]:
        raise ContractError("buckets.observed_count disagrees with the bucket list")

    groups = dto["groups"]
    if not isinstance(groups, list):
        raise ContractError("groups must be a list")
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str):
            raise ContractError("each group needs a string id")
        workloads = group.get("workloads")
        if not isinstance(workloads, list):
            raise ContractError("each group needs a workloads list")
        for workload in workloads:
            if not isinstance(workload, dict) or not isinstance(workload.get("id"), str):
                raise ContractError("each workload needs a string id")

    unattributed = dto["unattributed"]
    if not isinstance(unattributed, dict) or not isinstance(
        unattributed.get("visible"), bool
    ):
        raise ContractError("unattributed.visible must be a boolean")
    if unattributed["visible"] != scope_block["unattributed_visible"]:
        raise ContractError("unattributed.visible must mirror scope.unattributed_visible")
    if not unattributed["visible"] and unattributed.get("project_calls") is not None:
        raise ContractError("hidden unattributed counts must be null, not a number")
    if unattributed["visible"] and unattributed.get("project_calls") is None:
        raise ContractError("visible unattributed counts must not be null")

    for key in ("display_hints", "notes"):
        value = dto[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ContractError(f"{key} must be a list of strings")
