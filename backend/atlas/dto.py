"""AtlasDTO 的形状定义。

DTO 本身是**纯 JSON 可序列化的嵌套 dict**（便于用 ``json.dumps(..., sort_keys=True)``
做逐字节回归比对），字段清单与语义见 ``docs/atlas-data-contract.md``。
"""

from __future__ import annotations

from dataclasses import dataclass

#: DTO 模式版本。字段增删必须同步 docs/atlas-data-contract.md。
SCHEMA_VERSION = "atlas-usage/1"

#: quality 字段的取值域。
QUALITY_MEASURED = "measured"
QUALITY_PARTIAL = "partial"
QUALITY_UNKNOWN = "unknown"

#: DTO 顶层必需字段（validate_dto 与文档共用同一份清单）。
REQUIRED_TOP_LEVEL_FIELDS = (
    "source",
    "synthetic",
    "schema_version",
    "scope",
    "window",
    "quality",
    "totals",
    "latency",
    "ttft",
    "tokens",
    "groups",
    "buckets",
    "unknown",
    "unattributed",
    "display_hints",
    "notes",
)

#: unknown 计数器中**可归属到可见项目**的那一半。
#:
#: 这些计数器只由「服务端可信目录判定属于本次授权项目」的行累加，任何持有
#: ``atlas:usage:read`` 的主体都能安全看到；它们的 ``0`` 是**确实数过的零**。
SCOPED_UNKNOWN_COUNTER_FIELDS = (
    "out_of_window",
    "revoked_key",
    "filtered_out",
    "outcome_status_mismatch",
    "latency_missing",
    "ttft_missing",
)

#: unknown 计数器中**结构上无法归属到任何项目**的那一半（原始诊断）。
#:
#: - ``malformed_lines`` / ``content_field_rejected``：行不可解析，或在身份解析**之前**
#:   就因正文类字段被拒绝 —— 根本没有项目可言；
#: - ``unknown_key``：``key_id`` 不在受信目录，同样没有项目可言。
#:
#: 普通 viewer 一律返回 ``None``（无从测得），**绝不写 0 冒充**；否则仅凭前后两次
#: DTO 的差值就能推断其他租户的活动量或源文件损坏情况。
#: 需要它们必须显式持有对应 scope（见 ``identity.SCOPE_USAGE_*``），且 ``role`` 不参与。
UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS = (
    "unknown_key",
    "malformed_lines",
    "content_field_rejected",
)

#: unknown 计数器的完整字段清单（有权限时可见 + 无权限时为 null 的都在内）。
UNKNOWN_COUNTER_FIELDS = (
    SCOPED_UNKNOWN_COUNTER_FIELDS + UNATTRIBUTABLE_UNKNOWN_COUNTER_FIELDS
)


@dataclass(frozen=True)
class Provenance:
    """数据来源声明。**没有默认值** —— 调用方必须显式声明，避免把合成数据说成生产。"""

    source: str
    synthetic: bool
