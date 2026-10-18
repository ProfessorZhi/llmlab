"""Atlas 真实聚合契约（``atlas-aggregate/1``）：分组、模型分布、7×24 热度、延迟、趋势。

它与 :mod:`backend.atlas.service` 的 ``atlas-usage/1`` 是**两份契约**，不是两次实现：
两者共用同一套授权判定（:func:`service.collect_authorized_records`），但切法不同 ——

* ``atlas-usage/1`` 回答「被授权项目这一段时间用了多少」；
* ``atlas-aggregate/1`` 回答「用量的形状长什么样」：按项目/工作负载的构成、按模型的
  分布、按星期几×小时的 7×24 热度、延迟分位与时间趋势。

为什么不把它们塞进同一份 DTO：字段多一份，消费方就多一份可以各自理解错的地方；
而这两份契约的口径问题（尤其是「哪些 0 是数过的」）需要分别回答。新增一份契约比
改一份在用的契约安全 —— 后者会让已经接好的消费方在没有改代码的情况下改变含义。

三条纪律
--------

1. **唯一输入是已授权记录。** 每一条进入聚合的行都必须先过 §3.5 的判定顺序。
   本模块自己**不**做任何授权判断，也无从绕过 —— 它只消费
   :func:`service.collect_authorized_records` 的产物。
2. **默认不接线。** :class:`AggregateSource` 的 ``attached`` 默认 ``False``：
   一个没有声明「我接上了」的来源，产出的是 ``state: unknown`` 与空数组，
   而不是一组看起来像实测结果的 0。失败是封闭的。
3. **热度图不补零。** 只输出**真正观测到**的 ``(星期几, 小时)`` 格；缺失的格子
   不出现，只体现在 ``cells_observed`` 与 ``cells_expected`` 的差上。一个把空格子
   画成 0 的热力图，会把「那段时间没有数据」显示成「那段时间没有请求」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .dto import (
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    SCOPED_UNKNOWN_COUNTER_FIELDS,
    UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS,
)
from .errors import ContractError, InvalidRequest, UnverifiedSource
from .identity import KeyDirectory
from .ingest import IngestLimits
from .service import (
    UNATTRIBUTED_WORKLOAD,
    AtlasRequest,
    _bucket_entry,
    _day_floor,
    _iso_z,
    _stats_entry,
    collect_authorized_records,
    expected_bucket_starts,
)
from .stats import PERCENTILE_METHOD, StatsAccumulator

#: 受信来源白名单与判定。**跨模块复用而不是抄一份**：这份集合在仓库里已经有过
#: 三份手抄副本（见 ``backend/observability/trust.py``），而手抄的副本迟早会不一样。
#: Atlas 非合成聚合要求来源名通过这份白名单 —— 数据产地与数据的可信判据不该分家。
from ..observability.trust import VERIFIED_SOURCES, is_verified_source  # noqa: E402

#: 字段增删必须同步 ``docs/atlas-data-contract.md``。
SCHEMA_VERSION = "atlas-aggregate/1"

#: 总体状态。``measured`` = 来源已接线（0 是**测得**的 0）；``unknown`` = 未接线。
STATE_MEASURED = "measured"
STATE_UNKNOWN = "unknown"

#: 模型分布的最多条目数。超出后其余并入 ``<other>``，并把质量标为 ``partial``。
#: 上界的存在与窗口上界同理：让**返回体积有界**，否则一个客户端只要换着模型名
#: 发请求，就能把响应撑到任意大。
MAX_MODELS = 50

#: 模型名不可用/未上报时的桶名。所有未上报的调用**合并**成一个桶，而不是逐条列出。
MODEL_UNREPORTED = "<unreported>"
MODEL_OTHER = "<other>"

#: 模型标签的长度上界。模型名是**客户端可控**的字符串，直接回显等于把渲染层
#: 暴露给一个任意长度的输入。
MAX_MODEL_LABEL_LEN = 64

#: 转义前缀。一个**真实**模型名若等于哨兵值、或以本前缀开头，就会被加上它。
#: 这样真实标签永远不可能等于哨兵值，哨兵也永远不可能被冒充（见 ``model_label``）。
MODEL_LABEL_ESCAPE = "\\"

#: 本模块自己保留的哨兵桶名。真实模型名可以与它们**字面相同**，但经过
#: :func:`model_label` 之后不会与它们**碰撞**。
_RESERVED_MODEL_LABELS = frozenset({MODEL_OTHER, MODEL_UNREPORTED})

#: 热度图的轴定义。星期几一律以**周一为 0**（与 ``datetime.weekday()`` 一致），
#: 小时一律 UTC —— 与仓库其余部分对时间的处理保持一致，不让「哪个小时」取决于时区。
WEEKDAY_ORIGIN = "monday"
WEEKDAY_COUNT = 7
HOUR_COUNT = 24
HEATMAP_CELLS = WEEKDAY_COUNT * HOUR_COUNT

REQUIRED_TOP_LEVEL_FIELDS = (
    "source",
    "synthetic",
    "schema_version",
    "attached",
    "state",
    "scope",
    "window",
    "quality",
    "totals",
    "latency",
    "groups",
    "models",
    "heatmap",
    "trend",
    "unknown",
    "display_hints",
    "notes",
)


@dataclass(frozen=True)
class AggregateSource:
    """聚合来源声明。

    ``attached`` **没有默认 True**：不做显式声明时，产出的是 UNKNOWN。
    这正是不该被省略的那一步 —— 「我接上了真实导出口」是一个人必须显式说出口的
    断言，而不是一个可以被省略后默认成立的假设。

    **声明非合成时，来源名必须在受信白名单里。** ``source`` 与 ``synthetic`` 也是
    调用方传进来的，如果只把它们原样抄进 DTO，那「这是已验证来源」就退化成了一个人
    自己说的一句话。所以非合成来源必须通过
    :func:`backend.observability.trust.is_verified_source` —— 默认拒绝，新增一个可信
    来源必须有人显式改那份白名单。合成来源仍可用任意名字（它自报合成，不冒充）。

    **仍未由本层验证的**：``attached`` 本身。仓库里还没有真实的 Gateway 导出口适配器
    （见 ``docs/atlas-data-contract.md`` §8），所以「我接上了」目前仍是一个调用方断言。
    本层能保证的是：一个**没**声明接线的来源绝不会产出实测数字，且一个声明接线的来源
    必须自称一个**受信**的来源名。
    """

    source: str
    synthetic: bool
    attached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidRequest("source.source must be a non-empty string")
        if not isinstance(self.synthetic, bool):
            raise InvalidRequest("source.synthetic must be a boolean")
        if not isinstance(self.attached, bool):
            raise InvalidRequest("source.attached must be a boolean")
        if not self.synthetic and not is_verified_source(self.source):
            # 一个自称「非合成」却不在白名单里的来源，是「来路不明的真实数据」——
            # 那正是本仓库一路拒绝的东西。
            raise UnverifiedSource(
                f"{self.source!r} is not a verified source; a non-synthetic "
                f"aggregate must name one of {sorted(VERIFIED_SOURCES)}"
            )


def model_label(model: object) -> str:
    """把一个客户端可控的模型名归成一个**有界、可打印、且与哨兵值互不碰撞**的标签。

    模型名来自上游日志，因而不可信：可能有任意长度、可能带控制字符、也可能缺失。
    这里做三件事 —— 去控制字符、截断、以及**转义**。

    为什么要转义：``<other>`` 与 ``<unreported>`` 是**本模块自己**的哨兵桶名
    （分别是「被截断的尾巴」与「上游没给模型名」）。模型名是客户端可控的，所以一个
    客户端完全可以把模型名写成字面的 ``<other>``。若不隔离，那个真实模型会与哨兵
    共用一个桶，并让 ``reported`` / ``bucket`` 两个标志位反过来描述它 —— 于是一个
    **确实上报了**的模型被标成「没上报」，一个**真实存在的**模型被标成「被折叠的尾巴」。

    做法与 URL 里 ``%`` 的转义同理：以转义前缀开头、或本身就等于哨兵值的标签，
    一律加一个前缀。于是映射是单射 —— 不同的输入永不会得到相同的标签，
    而真实标签也永远不会等于哨兵值。

    ``MAX_MODEL_LABEL_LEN`` 是**转义前**的上界；转义至多多出一个前缀字符。
    """
    if model is None:
        return MODEL_UNREPORTED
    text = "".join(ch for ch in str(model) if ch.isprintable()).strip()
    text = text[:MAX_MODEL_LABEL_LEN].strip()
    if not text:
        return MODEL_UNREPORTED
    if text in _RESERVED_MODEL_LABELS or text.startswith(MODEL_LABEL_ESCAPE):
        return MODEL_LABEL_ESCAPE + text
    return text


def is_sentinel_model_label(label: object) -> bool:
    """这个标签是不是**本模块**的哨兵桶（而不是某个真实模型的（转义后）名字）。

    有了转义，这个判断就是可靠的：真实标签永远不可能等于哨兵值。
    """
    return label in _RESERVED_MODEL_LABELS


def _quality_for_calls(records, overall: StatsAccumulator) -> str:
    """与 ``atlas-usage/1`` 同一条规则：看不到源文件级诊断就不能报 measured。"""
    if records.scope.global_diagnostics_visible:
        untrusted = records.ingest.malformed_lines + records.ingest.content_field_rejected
        return QUALITY_MEASURED if untrusted == 0 else QUALITY_PARTIAL
    return QUALITY_UNKNOWN


def build_atlas_aggregate(
    request: AtlasRequest,
    text: str,
    directory: KeyDirectory,
    source: AggregateSource,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """构建 ``atlas-aggregate/1`` DTO。纯函数：同输入必得同输出。

    **本函数只对已接线的来源有效。** 它产出的 DTO 恒为
    ``attached: true, state: "measured"`` —— 因此它必须拒绝 ``attached=False`` 的
    来源，否则一个直接调用本函数的调用方（绕过 :func:`build_atlas_aggregate_dto`
    的分派）会拿到一份自称「已接线、已测得」的 DTO：那正是 fail-closed 的反面。

    两个 builder 各自只对自己的接线状态负责，互不代劳：
    未接线走 :func:`build_unattached_atlas_aggregate`（它同样拒绝 ``attached=True``），
    要自动分派走 :func:`build_atlas_aggregate_dto`。
    """
    if not isinstance(request, AtlasRequest):
        raise InvalidRequest("request must be an AtlasRequest")
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")
    if not isinstance(source, AggregateSource):
        raise InvalidRequest("source must be an AggregateSource")
    if not source.attached:
        # 刻意**不**在这里降级成 UNKNOWN：静默降级会让「我调错了函数」看起来像
        # 「这份数据是空的」。接线状态不同就是两条路径，不是同一个函数的两种输出。
        raise InvalidRequest(
            "build_atlas_aggregate requires source.attached=True; "
            "use build_unattached_atlas_aggregate() or build_atlas_aggregate_dto()"
        )

    # 授权先于一切：未授权的调用**必须**在这里被拒，与来源接没接无关。
    records = collect_authorized_records(request, text, directory, limits)

    overall = StatsAccumulator()
    by_project: dict[str, StatsAccumulator] = {}
    by_workload: dict[tuple[str, str], StatsAccumulator] = {}
    by_model: dict[str, StatsAccumulator] = {}
    by_cell: dict[tuple[int, int], StatsAccumulator] = {}
    by_hour: dict[datetime, StatsAccumulator] = {}
    model_counts: dict[str, int] = {}

    for event, binding in records.visible:
        workload_id = binding.workload or UNATTRIBUTED_WORKLOAD
        label = model_label(event.model)

        overall.add(event)
        by_project.setdefault(binding.project, StatsAccumulator()).add(event)
        by_workload.setdefault((binding.project, workload_id), StatsAccumulator()).add(event)
        by_cell.setdefault((event.ts.weekday(), event.ts.hour), StatsAccumulator()).add(event)
        by_hour.setdefault(
            event.ts.replace(minute=0, second=0, microsecond=0), StatsAccumulator()
        ).add(event)
        model_counts[label] = model_counts.get(label, 0) + 1

    # 模型分布先定「哪些标签留在榜上」，再累加：这样 ``<other>`` 是一个**可复算**的
    # 确定集合，而不是「遍历顺序碰巧剩下的那些」。
    kept = _bounded_model_labels(model_counts)
    for event, _binding in records.visible:
        label = model_label(event.model)
        bucket = label if label in kept else MODEL_OTHER
        by_model.setdefault(bucket, StatsAccumulator()).add(event)

    start, end = records.start, records.end
    expected_starts = expected_bucket_starts(start, end, "1h")
    expected_cells = {(moment.weekday(), moment.hour) for moment in expected_starts}

    observed_cells = sorted(by_cell)
    heatmap_quality = _coverage_quality(len(observed_cells), len(expected_cells))

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

    models = []
    for label in sorted(by_model):
        entry = _stats_entry(label, by_model[label])
        entry["model"] = label
        entry["reported"] = label != MODEL_UNREPORTED
        entry["bucket"] = label != MODEL_OTHER
        models.append(entry)

    model_quality = QUALITY_MEASURED
    if MODEL_OTHER in by_model:
        model_quality = QUALITY_PARTIAL
    if not by_model:
        model_quality = QUALITY_UNKNOWN

    hourly = [_bucket_entry(hour, by_hour[hour], "1h") for hour in sorted(by_hour)]

    return {
        "source": source.source,
        "synthetic": source.synthetic,
        "schema_version": SCHEMA_VERSION,
        "attached": True,
        "state": STATE_MEASURED,
        "scope": records.scope.to_dict(),
        "window": _window_block(start, end),
        "quality": {
            "calls": _quality_for_calls(records, overall),
            "latency": overall.quality_latency(),
            "heatmap": heatmap_quality,
            "models": model_quality,
        },
        "totals": overall.counter_block(),
        "latency": overall.latency_block(),
        "groups": groups,
        "models": models,
        "heatmap": {
            "weekday_origin": WEEKDAY_ORIGIN,
            "timezone": "UTC",
            "cells_expected": len(expected_cells),
            "cells_observed": len(observed_cells),
            "cells": [
                _heatmap_cell(weekday, hour, by_cell[(weekday, hour)])
                for (weekday, hour) in observed_cells
            ],
        },
        "trend": {"unit": "1h", "points": hourly},
        "unknown": _unknown_block(records, overall),
        "display_hints": _display_hints(source, records, heatmap_quality, model_quality),
        "notes": [
            "本视图的每一行都先过服务端授权判定；授权范围外的行既不计数也不展示。",
            "模型名来自上游日志、客户端可控，只作**描述性**维度，不参与授权与归属。",
            f"热度图的轴为 UTC，星期一并为 0；只输出观测到的格，缺失的格不以 0 补齐"
            f"（{HEATMAP_CELLS} 格上限）。",
            "分位数由筛选后成功且测得正值的每条原始请求样本计算"
            f"（{PERCENTILE_METHOD}），不对各组分位数求平均。",
            "缺测一律为 null（UNKNOWN），不以 0 代替。",
        ],
    }


def build_unattached_atlas_aggregate(
    request: AtlasRequest,
    text: str,
    directory: KeyDirectory,
    source: AggregateSource,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """未接线时的**封闭**答法：``state: unknown`` + 空数组 + 空分位。

    注意它仍会先做授权判定 —— 「你没权限」与「来源没接」是不同的失败，前者必须先
    拒绝。一份未授权却因为来源没接而返回 200 空壳的实现，会把 403 藏起来。
    """
    if not isinstance(source, AggregateSource) or source.attached:
        raise InvalidRequest("build_unattached_atlas_aggregate requires attached=False")
    records = collect_authorized_records(request, text, directory, limits)
    start, end = records.start, records.end
    empty = StatsAccumulator()
    return {
        "source": source.source,
        "synthetic": source.synthetic,
        "schema_version": SCHEMA_VERSION,
        "attached": False,
        "state": STATE_UNKNOWN,
        "scope": records.scope.to_dict(),
        "window": _window_block(start, end),
        "quality": {
            "calls": QUALITY_UNKNOWN,
            "latency": QUALITY_UNKNOWN,
            "heatmap": QUALITY_UNKNOWN,
            "models": QUALITY_UNKNOWN,
        },
        "totals": empty.counter_block(),
        "latency": empty.latency_block(),
        "groups": [],
        "models": [],
        "heatmap": {
            "weekday_origin": WEEKDAY_ORIGIN,
            "timezone": "UTC",
            "cells_expected": 0,
            "cells_observed": 0,
            "cells": [],
        },
        "trend": {"unit": "1h", "points": []},
        "unknown": _unknown_block(records, empty),
        "display_hints": [
            "来源未接线（attached=false）：当前没有可信的 Atlas 聚合可展示。",
            "这里的 0 是「没有接线」，不是「测到 0 次请求」；在接线之前不得用于任何决策。",
        ],
        "notes": [
            "本视图的每一行都先过服务端授权判定。",
            "缺测一律为 null（UNKNOWN），不以 0 代替。",
        ],
    }


def build_atlas_aggregate_dto(
    request: AtlasRequest,
    text: str,
    directory: KeyDirectory,
    source: AggregateSource,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """按 ``source.attached`` 选择已接线 / 未接线的两条路径，并自检。"""
    if not isinstance(source, AggregateSource):
        # 先看类型再看属性：一个 dict 参数报 AttributeError 会把「参数用错了」
        # 伪装成「代码坏了」。
        raise InvalidRequest("source must be an AggregateSource")
    if source.attached:
        dto = build_atlas_aggregate(request, text, directory, source, limits)
    else:
        dto = build_unattached_atlas_aggregate(request, text, directory, source, limits)
    validate_atlas_aggregate(dto)
    return dto


# --- 内部 -------------------------------------------------------------------


def _bounded_model_labels(counts: dict[str, int]) -> set[str]:
    """按调用量取前 ``MAX_MODELS`` 个模型标签，平票按标签排序（确定性）。"""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {label for label, _ in ranked[:MAX_MODELS]}


def _coverage_quality(observed: int, expected: int) -> str:
    if observed == 0:
        return QUALITY_UNKNOWN
    if expected and observed < expected:
        return QUALITY_PARTIAL
    return QUALITY_MEASURED


def _window_block(start: datetime, end: datetime) -> dict:
    return {
        "from": _iso_z(start),
        "to": _iso_z(end),
        "bucket": "1h",
        "timezone": "UTC",
        "hours": int((end - start).total_seconds() // 3600),
    }


def _heatmap_cell(weekday: int, hour: int, accumulator: StatsAccumulator) -> dict:
    cell = {"weekday": weekday, "hour": hour}
    cell.update(accumulator.counter_block())
    cell["latency"] = accumulator.latency_block()
    return cell


def _unknown_block(records, overall: StatsAccumulator) -> dict:
    """与 ``atlas-usage/1`` 同一条两族规则：可归属的是整数，不可归属的无授权即 null。"""
    unknown = dict(records.unknown)
    unknown["outcome_status_mismatch"] = overall.outcome_status_mismatch
    unknown["latency_missing"] = overall.latency_missing
    unknown["ttft_missing"] = overall.ttft_missing
    scope = records.scope
    unknown["unknown_key"] = (
        records.unattributable["unknown_key"] if scope.unattributed_visible else None
    )
    unknown["malformed_lines"] = (
        records.unattributable["malformed_lines"]
        if scope.global_diagnostics_visible
        else None
    )
    unknown["content_field_rejected"] = (
        records.unattributable["content_field_rejected"]
        if scope.global_diagnostics_visible
        else None
    )
    return unknown


def _display_hints(source, records, heatmap_quality: str, model_quality: str) -> list[str]:
    hints: list[str] = []
    if source.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：不得作为生产监控或容量决策依据。")
    if heatmap_quality == QUALITY_UNKNOWN:
        hints.append("热度图 unknown：窗口内没有可用日志，保持空态，不渲染 7×24 网格。")
    elif heatmap_quality == QUALITY_PARTIAL:
        hints.append("热度图 partial：只渲染观测到的格子，缺失的格不以 0 补齐。")
    if model_quality == QUALITY_PARTIAL:
        hints.append(
            f"模型分布 partial：超过 {MAX_MODELS} 个模型，其余并入 {MODEL_OTHER}。"
        )
    if records.scope.global_diagnostics_visible:
        if records.ingest.malformed_lines:
            hints.append(f"输入含 {records.ingest.malformed_lines} 行损坏元数据，已排除并计数。")
    else:
        hints.append(
            "源文件级诊断（quality.calls 等）未获授权，当前为 unknown；"
            "需要时请申请 atlas:usage:global-diagnostics。"
        )
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
            raise ContractError(f"{name}.{key} must be null when samples == 0")
        if samples > 0 and value is None:
            raise ContractError(f"{name}.{key} must not be null when samples > 0")


def validate_atlas_aggregate(dto: dict) -> None:
    """结构自检。**UNKNOWN 不得被 0 顶替**这条贯穿全篇。"""
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
    if not isinstance(dto["attached"], bool):
        raise ContractError("DTO attached must be a boolean")
    if dto["state"] not in (STATE_MEASURED, STATE_UNKNOWN):
        raise ContractError("DTO state is not supported")

    _validate_stat_block(dto["latency"], "latency")

    quality = dto["quality"]
    for key in ("calls", "latency", "heatmap", "models"):
        if quality.get(key) not in (QUALITY_MEASURED, QUALITY_PARTIAL, QUALITY_UNKNOWN):
            raise ContractError(f"quality.{key} is not a supported value")

    if not dto["attached"]:
        # 未接线的答法必须是**封闭**的：没有任何一个数字可以看起来像一次测量。
        if dto["state"] != STATE_UNKNOWN:
            raise ContractError("an unattached source must report state=unknown")
        if quality["calls"] != QUALITY_UNKNOWN or quality["latency"] != QUALITY_UNKNOWN:
            raise ContractError("an unattached source must not report measured quality")
        if dto["groups"] or dto["models"] or dto["heatmap"]["cells"] or dto["trend"]["points"]:
            raise ContractError("an unattached source must not carry aggregate rows")
        if dto["latency"]["p50_ms"] is not None or dto["latency"]["p95_ms"] is not None:
            raise ContractError("an unattached source must not carry percentiles")
        return

    if dto["state"] != STATE_MEASURED:
        raise ContractError("an attached source must report state=measured")

    unknown = dto["unknown"]
    for key in SCOPED_UNKNOWN_COUNTER_FIELDS:
        value = unknown.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"unknown.{key} must be a measured non-negative integer")
    for key in UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS:
        value = unknown.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"unknown.{key} must be null or a non-negative integer")
    if not dto["scope"]["unattributed_visible"] and unknown.get("unknown_key") is not None:
        raise ContractError("unknown.unknown_key requires atlas:usage:unattributed")

    heatmap = dto["heatmap"]
    if heatmap.get("weekday_origin") != WEEKDAY_ORIGIN or heatmap.get("timezone") != "UTC":
        raise ContractError("the heatmap axes must be monday-based and UTC")
    seen: set[tuple[int, int]] = set()
    for cell in heatmap["cells"]:
        weekday, hour = cell.get("weekday"), cell.get("hour")
        if not isinstance(weekday, int) or not 0 <= weekday < WEEKDAY_COUNT:
            raise ContractError("heatmap weekday out of range")
        if not isinstance(hour, int) or not 0 <= hour < HOUR_COUNT:
            raise ContractError("heatmap hour out of range")
        if (weekday, hour) in seen:
            raise ContractError("heatmap cells must be unique")
        seen.add((weekday, hour))
        if isinstance(cell.get("calls"), bool) or cell.get("calls", 0) < 1:
            # 没有观测的格子不该存在：0 请求与「没有数据」在这里必须长得不一样。
            raise ContractError("a heatmap cell must represent at least one observed call")
    if heatmap["cells_observed"] != len(heatmap["cells"]):
        raise ContractError("heatmap cells_observed disagrees with the cell list")
    if heatmap["cells_observed"] > heatmap["cells_expected"]:
        raise ContractError("heatmap cells_observed must not exceed cells_expected")

    if len(dto["models"]) > MAX_MODELS + 1:
        raise ContractError(f"the model distribution must stay within {MAX_MODELS + 1} rows")
    labels = [row.get("model") for row in dto["models"]]
    if len(set(labels)) != len(labels):
        raise ContractError("model distribution rows must be unique")

    ids = [group.get("id") for group in dto["groups"]]
    if len(set(ids)) != len(ids):
        raise ContractError("group ids must be unique")
    expected_points = len(dto["trend"]["points"])
    if dto["trend"]["unit"] != "1h":
        raise ContractError("the trend unit must be 1h")
    if expected_points and dto["quality"]["heatmap"] == QUALITY_UNKNOWN:
        raise ContractError("a non-empty trend must not report an unknown heatmap quality")
