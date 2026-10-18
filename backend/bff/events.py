"""``GET /api/portal/events`` —— 操作员范围的有界审计时间线。

V1-11 要的是「fixed-scope operator services/logs/audit … bounded and redacted」。
在它之前，这条路由在契约里是 **PROPOSED and deliberately unmounted**，理由写得很
明确：它的权威（一个 **应用生成的结构化事件存储**）不存在，而当时能答的东西只有
三类契约禁止下发的来源 —— 原始日志代理、无界 journal、或者把固定错误列表换个名字
叫「审计」。

这个判断的前提在本轮被推翻了，而且是被**代码**推翻的：仓库里已经有两个真实的、
有界的、可归属的结构化审计账本：

``elevation``
    ``backend.bff.admin_mode`` 自己的 ``admin_audit`` 表（提权进入/退出/失败）。
    它属于本层，写入时已经刻意不带凭据材料。
``registry``
    Registry 的 ``audit_log`` 表，经 ``RegistryService.audit_log(requester, limit)``
    读出；那个方法**自己**就带授权语义（跨主体需要 admin read，普通主体只看自己）
    与 ``limit`` 上界。

所以本模块挂的是**这两条**，不是一份新的日志系统。任何一条没装，它如实报
``unconfigured``；两条都没装，路由以 503 收场。这不是「先开个口子以后再填」。

四条硬边界
----------

**只下发白名单字段。** 每条记录先过 :func:`_redact`：只有列在
``ELEVATION_FIELDS`` / ``REGISTRY_FIELDS`` 里的键会被留下，其余（包括两边的
``detail`` 列）**整体丢弃**。不是「过滤敏感词」，是「不在名单里的不存在」——
前一版设计里最容易腐化的就是一份「已知敏感字段」黑名单。

**操作员范围，不是「登录即可」。** 路由要求 ``portal:admin:read``；普通成员得到
403，而不是一份过滤过的别人活动。跨主体审计是这个范围**唯一**的用途，把
「你自己那份」也塞进来只会让「谁能看见谁」多一条需要论证的路径。

**有界。** ``limit`` 只接受 1..``MAX_LIMIT``，超界是 400 而不是静默截断；每次只取
一页，没有时间范围扫描、没有游标遍历。``truncated`` 说明这一页是否顶到了上界。

**没有可指向的东西。** 没有任何参数能改变读的是哪张表、哪个文件、哪条 PromQL：
来源是模块里写死的两条，路由上的 ``?url=`` / ``?path=`` / ``?promql=`` 在到达本
模块之前就已经被 400 拒绝（``backend.bff.identity.reject_proxy_assertions``）。

新鲜度
------

审计账本没有「过期」这回事：一台闲着的机器上，最新一条记录本来就可能很旧。
因此这里报两件**事实**而不是一个判断：``observed_at``（这次读的时间）与
``newest_at``（账本里最新一条的时间）。给审计账本编一个 stale 阈值，只会把
「最近没人做管理操作」说成「数据有问题」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..observability.dto import iso_z

SCHEMA_VERSION = "portal-events/1"

#: 一页的默认与上限。上限存在的理由与 Atlas 那边一样：一个能把整张表读进内存的
#: ``limit`` 不是分页，是拒绝服务。
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# --- 来源状态：与仓库既有的来源状态词表同一套取值 ---------------------------

#: 问到了，且拿到了内容。
STATE_OK = "ok"
#: 问到了，真的是 0 条。**这是一个测量结果**，不是「没配」。
STATE_EMPTY = "empty"
#: 本部署没有接上这条来源。
STATE_UNCONFIGURED = "unconfigured"
#: 接上了，但这次没读到。取值沿用仓库既有的 ``unreachable`` —— **不自造**
#: ``unavailable``：同一个概念有两个词，就是两份会漂移的规则。
STATE_UNREACHABLE = "unreachable"

#: 这组取值与 ``backend.evaluation.provenance.SOURCE_STATES`` 是同一套词
#: （``ok/empty/unconfigured/unreachable/stale``）。本模块只用到前四个：审计账本
#: 没有 ``stale`` 语义（见模块说明）。测试把它与那份词表钉在一起，防止两套词漂移。
STATES = (STATE_OK, STATE_EMPTY, STATE_UNCONFIGURED, STATE_UNREACHABLE)

REASON_NOT_CONFIGURED = "portal_events_source_not_configured"
REASON_READ_FAILED = "portal_events_source_read_failed"

#: 每条记录只允许这些键出现在下发内容里。**不在名单里的一律丢弃**。
ELEVATION_FIELDS = ("at", "event", "outcome", "actor", "principal", "uid", "linux_user", "admin_mode")
REGISTRY_FIELDS = ("at", "action", "actor", "actor_uid", "object_type", "object_id")

#: 单个文本字段的长度上界。审计账本里的字符串也可能来自用户输入（对象 id、
#: 动作名）；一条超长值不该成为一条渲染通道。
MAX_FIELD_CHARS = 120


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:MAX_FIELD_CHARS]


def _instant(value: object) -> str | None:
    """把一条记录的时间归一成 UTC ISO-8601。

    两个账本的时间格式不一样：Registry 存的是 ISO 字符串，``admin_mode`` 存的是
    epoch 浮点（``at REAL``）。下发两份格式不同的时间，等于让前端去猜哪一个是
    哪一个 —— 所以在这里归一，而不是让调用方记得。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return iso_z(datetime.fromtimestamp(float(value), tz=timezone.utc))
    return None


def _redact(item: object, allow: Iterable[str]) -> dict:
    """一条记录 ⇒ 白名单字段。不在 ``allow`` 里的键**不存在**于结果里。"""
    if not isinstance(item, dict):
        return {}
    out: dict = {}
    for key in allow:
        if key not in item:
            continue
        value = item[key]
        if key == "at":
            out[key] = _instant(value)
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int):
            out[key] = value
        elif value is None:
            out[key] = None
        else:
            out[key] = _text(value)
    return out


def _source_block(
    source: str,
    *,
    state: str,
    reason: str | None,
    items: list[dict],
    limit: int,
    now: datetime,
) -> dict:
    return {
        "source": source,
        "state": state,
        "reason": reason,
        "observed_at": iso_z(now),
        # 账本按 id DESC 取，第一条就是最新的那条；没有记录就是不存在的取值。
        "newest_at": items[0].get("at") if items else None,
        "count": len(items),
        "truncated": len(items) >= limit,
        "items": items,
    }


def elevation_events(admin_mode: object, *, limit: int, now: datetime) -> dict:
    """本层自己的提权审计。

    ``admin_mode`` 缺席 ⇒ ``unconfigured``；读失败 ⇒ ``unavailable`` 且**不带**
    异常文本（一条数据库错误里可能含有路径或 SQL）。
    """
    if admin_mode is None:
        return _source_block(
            "elevation",
            state=STATE_UNCONFIGURED,
            reason=REASON_NOT_CONFIGURED,
            items=[],
            limit=limit,
            now=now,
        )
    try:
        rows = admin_mode.store.list_audit(limit=limit)
    except Exception:  # noqa: BLE001 - 账本读不到只能是「这次没读到」
        return _source_block(
            "elevation",
            state=STATE_UNREACHABLE,
            reason=REASON_READ_FAILED,
            items=[],
            limit=limit,
            now=now,
        )
    items = [_redact(row, ELEVATION_FIELDS) for row in rows]
    return _source_block(
        "elevation",
        state=STATE_OK if items else STATE_EMPTY,
        reason=None,
        items=items,
        limit=limit,
        now=now,
    )


def registry_events(service: object, requester: object, *, limit: int, now: datetime) -> dict:
    """Registry 的审计账本。

    授权与跨主体可见性**由 Registry 自己判定**（``audit_log`` 里
    「普通主体只看自己的条目」那一条）。本层不重写一遍——重写就是第二套 ACL。
    """
    if service is None:
        return _source_block(
            "registry",
            state=STATE_UNCONFIGURED,
            reason=REASON_NOT_CONFIGURED,
            items=[],
            limit=limit,
            now=now,
        )
    try:
        envelope = service.audit_log(requester, limit=limit)
        raw = ((envelope or {}).get("data") or {}).get("items") or []
    except Exception:  # noqa: BLE001 - 同上：读不到就是读不到，不外泄异常文本
        return _source_block(
            "registry",
            state=STATE_UNREACHABLE,
            reason=REASON_READ_FAILED,
            items=[],
            limit=limit,
            now=now,
        )
    items = [_redact(item, REGISTRY_FIELDS) for item in raw]
    return _source_block(
        "registry",
        state=STATE_OK if items else STATE_EMPTY,
        reason=None,
        items=items,
        limit=limit,
        now=now,
    )


def build_events_dto(
    *,
    admin_mode: object,
    service: object,
    requester: object,
    limit: int = DEFAULT_LIMIT,
    now: datetime,
) -> dict:
    """两条来源 ⇒ 一份 ``portal-events/1``。纯函数（除了两个只读账本）。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= MAX_LIMIT):
        raise ValueError(f"limit must be an integer in [1, {MAX_LIMIT}]")
    sources = [
        elevation_events(admin_mode, limit=limit, now=now),
        registry_events(service, requester, limit=limit, now=now),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": iso_z(now),
        "limit": limit,
        "sources": sources,
        "notes": [
            "只列白名单字段；两个账本各自的私有列（记录元数据）不下发。",
            "审计账本没有 stale 语义：newest_at 是事实，不是新鲜度判断。",
            "每条来源的授权由它自己的模块判定，本层不复制第二套 ACL。",
        ],
    }


def all_sources_unconfigured(dto: dict) -> bool:
    """两条来源都没接上 ⇒ 路由应当 503，而不是回一份空时间线。

    「没接线」与「账本里确实一条都没有」必须能分开：前者是接线问题，后者是一个
    测量结果（``empty``）。
    """
    return all(item["state"] == STATE_UNCONFIGURED for item in dto["sources"])


__all__ = [
    "DEFAULT_LIMIT",
    "ELEVATION_FIELDS",
    "MAX_FIELD_CHARS",
    "MAX_LIMIT",
    "REASON_NOT_CONFIGURED",
    "REASON_READ_FAILED",
    "REGISTRY_FIELDS",
    "SCHEMA_VERSION",
    "STATES",
    "STATE_EMPTY",
    "STATE_OK",
    "STATE_UNREACHABLE",
    "STATE_UNCONFIGURED",
    "all_sources_unconfigured",
    "build_events_dto",
    "elevation_events",
    "registry_events",
]
