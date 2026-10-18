"""报告来源（provenance）：**EvalScope 报告**与**历史 Benchmark 镜像 run** 不是同一种东西。

为什么这一层必须存在
--------------------

[Issue #34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34) 的现场证据是：
EvalScope 的 ``/api/v1/reports`` 真实返回 ``total=0``，而旧 Portal 的
``/api/evaluation`` 返回**一条**历史 Benchmark run。这两条数据长得都像"一次评测的
结果"，但它们的来源、可信度与归属完全不是一回事。把旧 run 标成 EvalScope 报告——
哪怕只是省略掉来源字段——就是本工单要消灭的那个缺陷。

因此本模块把「来源」做成一个**封闭词表**：

* :data:`SOURCE_EVALSCOPE` —— EvalScope 自己产出的报告；
* :data:`SOURCE_LEGACY_BENCHMARK` —— 旧 Portal 镜像下来的 Benchmark 产物。

一段外部负载**不能**决定自己的来源。来源是**产出它的那个适配器**的身份
（见 ``report_readers.py``），负载里就算写着 ``"source": "evalscope"``，只要它是从
legacy 适配器出来的，它就是 ``legacy-benchmark``。:class:`ReportSourceSnapshot` 在
构造时强制 ``record.source == snapshot.source``：一个来源里混进另一来源的记录是
构造期错误，而不是一条"看起来还行的数据"。

测不到 vs 真的是 0
------------------

来源自身的状态也是封闭词表（:data:`SOURCE_STATES`）。``empty`` 是**问过了、真的没有**；
``unconfigured`` / ``unreachable`` 是**没问到**。二者在 DTO 里的区别是 ``0`` 与
``None``，而且这个区别由 :class:`ReportSourceSnapshot` 的构造器强制（``empty`` 必须零条，
``ok`` 必须至少一条），不靠渲染层自觉。

内容永不外发
------------

本模块只产出**引用**与**元数据**。报告正文既不在 DTO 里，也不在这个面出去：预览/下载
的授权结论是一份策略结果（``authorized`` 布尔 + 原因），而不是字节。这与
``reports.py`` 里 ``read_bytes`` 一律 ``NotConfigured`` 是同一个决定。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from .errors import ReportSourceMalformed
from .models import METRIC_UNKNOWN, MetricSample, isoformat
from .reports import normalize_report_ref

# ---------------------------------------------------------------------------
# 来源词表
# ---------------------------------------------------------------------------

#: EvalScope 产出的报告。
SOURCE_EVALSCOPE = "evalscope"

#: 旧 Portal 镜像下来的历史 Benchmark run。**不是** EvalScope 报告。
SOURCE_LEGACY_BENCHMARK = "legacy-benchmark"

#: 本契约认识的来源。拒绝未知来源进入任何目录，避免拼写错误静默失效。
KNOWN_REPORT_SOURCES: tuple[str, ...] = (SOURCE_EVALSCOPE, SOURCE_LEGACY_BENCHMARK)

#: 展示标签。UI 直接渲染，不在前端拼字符串。
SOURCE_LABELS = {
    SOURCE_EVALSCOPE: "EvalScope 报告",
    SOURCE_LEGACY_BENCHMARK: "历史 Benchmark 镜像",
}

# ---------------------------------------------------------------------------
# 来源状态词表
# ---------------------------------------------------------------------------

#: 问到了，且拿到了内容。
SOURCE_OK = "ok"
#: 问到了，真的是 0 份。**这是一个测量结果**，不是"没问到"。
SOURCE_EMPTY = "empty"
#: 本部署没有接上这条来源。
SOURCE_UNCONFIGURED = "unconfigured"
#: 接上了，但这次没读到（上游故障 / 负载畸形）。
SOURCE_UNREACHABLE = "unreachable"
#: 读到过，但最后一次成功读取已经超出新鲜度窗口。
SOURCE_STALE = "stale"

SOURCE_STATES: tuple[str, ...] = (
    SOURCE_OK,
    SOURCE_EMPTY,
    SOURCE_UNCONFIGURED,
    SOURCE_UNREACHABLE,
    SOURCE_STALE,
)

#: 携带内容的来源状态：只有它们可以有记录。其余三态**必须**零条——一个没问到的来源
#: 不可能"有 3 份报告"。
SOURCE_STATES_WITH_CONTENT = frozenset({SOURCE_OK, SOURCE_STALE})

# 稳定的原因字符串，供审计与 UI 展示，不参与判定。
REASON_SOURCE_UNCONFIGURED = "report_source_not_configured"
REASON_SOURCE_UNREACHABLE = "report_source_unreachable"
REASON_SOURCE_MALFORMED = "report_source_payload_malformed"
REASON_SOURCE_STALE = "report_source_last_read_is_stale"
REASON_SOURCE_EMPTY = "report_source_returned_zero_reports"
#: 本主体在这个来源里**没有可见报告**。与 :data:`REASON_SOURCE_EMPTY` 刻意区分：
#: ``empty`` 是"这个来源真的没有报告"，而这一条对主体可见视图而言是唯一安全的说法
#: ——它**不**声称来源里有没有别人的报告（那是一个跨租户存在性侧信道）。
REASON_NO_VISIBLE_REPORTS = "no_visible_reports_for_this_principal"
#: 上游给的时间戳落在未来。有界的时钟偏斜可以容忍，超出的按陈旧处理（fail closed）。
REASON_SOURCE_TIMESTAMP_IN_FUTURE = "report_source_timestamp_is_in_the_future"
REASON_DIMENSION_NOT_REPORTED = "dimension_not_reported_by_source"
#: 来源**提到了**这个维度，但它这次没有取值。与"根本没提"分开，是因为两者对排障的
#: 意义不同——但两者都**不是 0**。
REASON_DIMENSION_REPORTED_UNKNOWN = "dimension_reported_without_a_value"
REASON_OWNER_UNVERIFIED = "report_owner_not_verified"
REASON_CONTENT_DISABLED = "report_content_reading_disabled"
REASON_REFERENCE_NOT_AUTHORIZED = "report_reference_not_authorized"

# ---------------------------------------------------------------------------
# 指标维度词表
# ---------------------------------------------------------------------------

#: 报告应当覆盖的维度（Issue #48：吞吐 / 首 token / 质量 / 冷启动 / 并发 / 失败）。
#: 缺哪个就有哪条 ``missing_dimension``，且**缺测不是 0**。
DIMENSION_QUALITY = "quality"
DIMENSION_THROUGHPUT = "throughput_rps"
DIMENSION_FIRST_TOKEN = "first_token_ms"
DIMENSION_COLD_START = "cold_start_ms"
DIMENSION_CONCURRENCY = "concurrency"
DIMENSION_FAILURE = "failure_rate"

REPORT_DIMENSIONS: tuple[str, ...] = (
    DIMENSION_QUALITY,
    DIMENSION_THROUGHPUT,
    DIMENSION_FIRST_TOKEN,
    DIMENSION_COLD_START,
    DIMENSION_CONCURRENCY,
    DIMENSION_FAILURE,
)

# ---------------------------------------------------------------------------
# 不透明引用 & 文本消毒
# ---------------------------------------------------------------------------

#: 不透明报告引用的前缀。它**不是**路径：由 (source, 上游 id, 相对引用) 摘要而来，
#: 因此前端手里的 id 无法被改写成一条别的路径。
OPAQUE_REPORT_PREFIX = "evrp-"

#: 标签长度上限。上游字段再长也不会变成一段可被滥用的正文。
MAX_LABEL_LENGTH = 200

#: 会被**删除**（而不是转义）的字符：任何可以开始一段标记的写法。
_UNSAFE_TEXT_CHARS = frozenset({"<", ">"})


def validate_source(raw: object) -> str:
    """来源必须是封闭词表里的一个。未知来源一律拒绝。"""
    if not isinstance(raw, str) or raw not in KNOWN_REPORT_SOURCES:
        raise ReportSourceMalformed(f"unknown report source: {raw!r}")
    return raw


def opaque_report_ref(source: str, source_report_id: str, ref: str) -> str:
    """由来源 + 上游 id + 相对引用推出稳定的**不透明**引用。

    摘要里含来源：同一份引用字符串在两个来源下不会撞成同一个 id，因此一个 id 永远
    指向一个来源。
    """
    validate_source(source)
    digest = hashlib.sha256(
        "\x1f".join((source, str(source_report_id), str(ref))).encode("utf-8")
    ).hexdigest()
    return OPAQUE_REPORT_PREFIX + digest[:24]


def safe_text(raw: object, *, limit: int = MAX_LABEL_LENGTH) -> str | None:
    """把一段上游文本消毒成可以安全下发的标签。

    删除 ``<`` / ``>`` 与控制字符、折叠空白、截断长度。**不做转义**：转义保留的仍是
    一段标记（``&lt;script&gt;`` 在某些渲染路径里会变回标签），而这里要的是"这段文本
    不可能成为标记"。空结果返回 ``None``，让 UI 走"无标题"分支而不是渲染一个空串。
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ReportSourceMalformed("a report label must be a string or null")
    cleaned = "".join(
        ch for ch in raw if ch not in _UNSAFE_TEXT_CHARS and ord(ch) >= 0x20 and ord(ch) != 0x7F
    )
    collapsed = " ".join(cleaned.split())
    if not collapsed:
        return None
    return collapsed[:limit]


# ---------------------------------------------------------------------------
# 记录与快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportOwner:
    """一份报告在**服务端**的归属。审核过的映射才配得出这个对象。"""

    principal: str
    uid: int
    project_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise ReportSourceMalformed("a report owner needs a principal")
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise ReportSourceMalformed("a report owner needs a non-negative uid")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ReportSourceMalformed("a report owner needs a project")

    def to_dict(self) -> dict:
        return {"principal": self.principal, "uid": self.uid, "project_id": self.project_id}


@dataclass(frozen=True)
class ReportRecord:
    """来源适配器产出的一条**只读**报告记录。

    ``owner`` 为 ``None`` 表示**归属无法核实**——旧镜像里没有经过 UID 绑定的历史产物
    就长这样。这种记录默认**对所有人隐藏**（见 ``authz.report_visibility``），而不是
    "默认所有人可见、等管理员来收"。
    """

    source: str
    source_report_id: str
    ref: str
    synthetic: bool
    owner: ReportOwner | None = None
    title: str | None = None
    observed_at: datetime | None = None
    created_at: datetime | None = None
    dimensions: Mapping[str, MetricSample] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_source(self.source)
        if not isinstance(self.source_report_id, str) or not self.source_report_id.strip():
            raise ReportSourceMalformed("a report record needs an upstream id")
        # 引用在构造期就被规范化：畸形引用进入不了目录，因此"伪造路径"在最早的一步
        # 就被挡住，而不是等到有人去解析它。
        object.__setattr__(self, "ref", normalize_report_ref(self.ref))
        if not isinstance(self.synthetic, bool):
            raise ReportSourceMalformed("a report record must declare synthetic explicitly")
        if self.owner is not None and not isinstance(self.owner, ReportOwner):
            raise ReportSourceMalformed("a report owner must be a ReportOwner or null")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ReportSourceMalformed("observed_at must be timezone-aware")
        if self.created_at is not None and self.created_at.tzinfo is None:
            raise ReportSourceMalformed("created_at must be timezone-aware")
        if not isinstance(self.dimensions, Mapping):
            raise ReportSourceMalformed("dimensions must be a mapping")
        unknown = sorted(set(self.dimensions) - set(REPORT_DIMENSIONS))
        if unknown:
            # 上游多送一个维度名 = 一次需要被看见的漂移，而不是一个悄悄进入 DTO 的新键。
            raise ReportSourceMalformed(f"unsupported dimension name(s): {', '.join(unknown)}")
        for name, sample in self.dimensions.items():
            if not isinstance(sample, MetricSample):
                raise ReportSourceMalformed(f"dimension {name!r} is not a MetricSample")
            if sample.name != name:
                raise ReportSourceMalformed(
                    f"dimension key {name!r} disagrees with its sample name"
                )

    @property
    def ref_id(self) -> str:
        return opaque_report_ref(self.source, self.source_report_id, self.ref)

    @property
    def unowned(self) -> bool:
        return self.owner is None

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        """应该有、但这次**没有测到**的维度（已知的记分不算缺失）。"""
        return tuple(
            name for name in REPORT_DIMENSIONS if not self._known(name)
        )

    def _known(self, name: str) -> bool:
        sample = self.dimensions.get(name)
        return bool(sample is not None and sample.known)

    def dimension(self, name: str) -> MetricSample:
        """取一个维度；缺失时返回一条**显式 unknown**，绝不是 0。"""
        if name not in REPORT_DIMENSIONS:
            raise ReportSourceMalformed(f"unknown dimension: {name!r}")
        sample = self.dimensions.get(name)
        if sample is not None:
            return sample
        return MetricSample(
            name=name, state=METRIC_UNKNOWN, value=None, note=REASON_DIMENSION_NOT_REPORTED
        )


@dataclass(frozen=True)
class ReportSourceSnapshot:
    """一条来源在**某一次读取**后的样子。

    适配器负责产出它；本对象负责保证它自洽：状态与记录数必须一致，且来源里不能混进
    另一来源的记录。
    """

    source: str
    state: str
    synthetic: bool
    observed_at: datetime | None = None
    records: tuple[ReportRecord, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        validate_source(self.source)
        if self.state not in SOURCE_STATES:
            raise ReportSourceMalformed(f"unknown source state: {self.state!r}")
        if not isinstance(self.synthetic, bool):
            raise ReportSourceMalformed("a source snapshot must declare synthetic explicitly")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ReportSourceMalformed("observed_at must be timezone-aware")
        records = tuple(self.records)
        for record in records:
            if not isinstance(record, ReportRecord):
                raise ReportSourceMalformed("a snapshot may only carry ReportRecord")
            if record.source != self.source:
                # 来源隔离的**唯一**强制性检查：旧 Benchmark 记录永远进不了 evalscope 快照。
                raise ReportSourceMalformed(
                    f"a {self.source!r} snapshot cannot carry a {record.source!r} record"
                )
        object.__setattr__(self, "records", records)

        if self.state in SOURCE_STATES_WITH_CONTENT:
            if self.state == SOURCE_OK and not records:
                # 有内容的状态却零条 = 调用方该用 ``empty``。二者不能互相冒充。
                raise ReportSourceMalformed(
                    "a snapshot in state 'ok' must carry at least one record; "
                    "a genuinely empty source is state 'empty'"
                )
        elif records:
            raise ReportSourceMalformed(
                f"a snapshot in state {self.state!r} must not carry records"
            )

    @property
    def count(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "source_label": SOURCE_LABELS[self.source],
            "state": self.state,
            "synthetic": self.synthetic,
            "count": self.count,
            "observed_at": isoformat(self.observed_at),
            "detail": self.detail,
        }


def unconfigured_snapshot(source: str, *, detail: str | None = None) -> ReportSourceSnapshot:
    """本部署里"这条来源没接线"的标准形状。零条、``unconfigured``。"""
    return ReportSourceSnapshot(
        source=source,
        state=SOURCE_UNCONFIGURED,
        synthetic=False,
        observed_at=None,
        records=(),
        detail=detail or REASON_SOURCE_UNCONFIGURED,
    )


__all__ = [
    "DIMENSION_COLD_START",
    "DIMENSION_CONCURRENCY",
    "DIMENSION_FAILURE",
    "DIMENSION_FIRST_TOKEN",
    "DIMENSION_QUALITY",
    "DIMENSION_THROUGHPUT",
    "KNOWN_REPORT_SOURCES",
    "MAX_LABEL_LENGTH",
    "OPAQUE_REPORT_PREFIX",
    "REASON_CONTENT_DISABLED",
    "REASON_DIMENSION_NOT_REPORTED",
    "REASON_DIMENSION_REPORTED_UNKNOWN",
    "REASON_OWNER_UNVERIFIED",
    "REASON_REFERENCE_NOT_AUTHORIZED",
    "REASON_NO_VISIBLE_REPORTS",
    "REASON_SOURCE_EMPTY",
    "REASON_SOURCE_MALFORMED",
    "REASON_SOURCE_STALE",
    "REASON_SOURCE_TIMESTAMP_IN_FUTURE",
    "REASON_SOURCE_UNCONFIGURED",
    "REASON_SOURCE_UNREACHABLE",
    "REPORT_DIMENSIONS",
    "SOURCE_EMPTY",
    "SOURCE_EVALSCOPE",
    "SOURCE_LABELS",
    "SOURCE_LEGACY_BENCHMARK",
    "SOURCE_OK",
    "SOURCE_STALE",
    "SOURCE_STATES",
    "SOURCE_STATES_WITH_CONTENT",
    "SOURCE_UNCONFIGURED",
    "SOURCE_UNREACHABLE",
    "ReportOwner",
    "ReportRecord",
    "ReportSourceSnapshot",
    "opaque_report_ref",
    "safe_text",
    "unconfigured_snapshot",
    "validate_source",
]
