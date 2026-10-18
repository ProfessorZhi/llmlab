"""「我的调用」逐条日志契约（纯函数，与项目汇总完全分离）。

**这是第二条口径，不是汇总路径上的一个过滤器。**

``backend.atlas.service`` 回答的是「我被授权项目的用量汇总」—— 同一项目下其他
``principal`` 的 key 会被一并计量（那是有意的，见
``docs/atlas-data-contract.md`` §10.1）。本模块回答的是另一个问题：
「**我自己**的 key 发出去的每一条调用是什么样」。

两者共用同一份 ``KeyDirectory``、同一份 ``RequesterContext``、同一个 ``ingest``，
但产出**不同的 DTO、不同的 schema_version、不同的 scope**，互不派生：

* 汇总 DTO 的字段不因本模块而改变含义；
* 本模块的 ``scope.keys`` **只**来自服务端目录的 ``principal == subject``；
* 持 ``atlas:usage:read`` 不自动获得本视图，反之亦然。

刻意**不**做的事：

1. **绝不返回正文。** ``content.included`` 恒为 ``False`` —— 不是因为「这个用户没
   同意」，而是因为元数据管道里**根本没有**正文（``ingest`` 在身份解析之前就拒绝
   一切正文类字段）。``content.consent`` 只如实反映服务端记录在案的授权状态，
   让前端能区分「未开启」与「已开启但本管道不采集」，而不是把两者显示成同一句
   「暂无内容」。
2. **绝不把合成数据说成 live。** ``transport.mode`` 只有在
   ``provenance.synthetic is False`` **且**调用方显式声明 ``gateway_attached=True``
   时才是 ``"live"``；本模块没有任何默认路径能得出 ``live``。
3. **绝不为缺失的字段编造数值。** 上游没记 ``ttft_ms`` / ``usage`` 就是 ``None``，
   不写 ``0``：``0`` 是一个**测得**的结论，``None`` 才是「没测到」。
4. **绝不编造 ``request_id``。** 缺省即 ``None``；``call_id`` 是本模块自己算出的
   稳定去重键，它会**明确标注**为派生值，不会被冒充成上游签发的句柄。
5. **不回答「别人的 key 存不存在」。** 未见过的 key、别人的 key、自己已吊销的 key
   在授权阶段是**同一个**拒绝（见 :func:`backend.atlas.identity.resolve_own_call_scope`）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .dto import QUALITY_MEASURED, Provenance
from .errors import ContractError, InvalidRequest
from .identity import (
    CONTENT_CONSENT_ABSENT,
    CONTENT_CONSENT_GRANTED,
    CONTENT_CONSENT_STATES,
    CONTENT_CONSENT_WITHDRAWN,
    KeyDirectory,
    OwnCallScope,
    RequesterContext,
    resolve_own_call_scope,
)
from .ingest import (
    ALLOWED_OUTCOMES,
    CONTENT_FIELD_NAMES,
    IngestLimits,
    RawEvent,
    ingest_jsonl,
)
from .service import UNATTRIBUTED_WORKLOAD, validate_window

#: 本 DTO 的模式版本。它与 :data:`backend.atlas.dto.SCHEMA_VERSION` **不同**，
#: 且必须不同：两者字段含义不可互换，共用一个版本号会让下游按汇总 schema 去解析
#: 逐条日志而看不出错。
CALLS_SCHEMA_VERSION = "atlas-calls/1"

#: 一页最多多少条。分页是**跳页式**（keyset），不是「截断后假装完整」：
#: 超出部分通过 ``counts.truncated`` + ``counts.next_before`` 如实告知。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: ``outcome`` 过滤器的取值域：``all`` 加 ``ingest.ALLOWED_OUTCOMES``。
OUTCOME_FILTERS = ("all",) + tuple(sorted(ALLOWED_OUTCOMES))

#: 传输层模式。``live`` 需要**两个**条件同时成立，缺一不可（见 build_* 的注释）。
TRANSPORT_MODE_DEMO = "demo"
TRANSPORT_MODE_LIVE = "live"

#: ``content.reason`` 的取值域。它解释的是「为什么这里没有正文」，而**不是**
#: 「这个用户做错了什么」。
CONTENT_REASON_CONSENT_ABSENT = "consent_absent"
CONTENT_REASON_CONSENT_WITHDRAWN = "consent_withdrawn"
CONTENT_REASON_NOT_COLLECTED = "not_collected_by_metadata_pipeline"

CONTENT_REASONS = {
    CONTENT_CONSENT_ABSENT: CONTENT_REASON_CONSENT_ABSENT,
    CONTENT_CONSENT_WITHDRAWN: CONTENT_REASON_CONSENT_WITHDRAWN,
    CONTENT_CONSENT_GRANTED: CONTENT_REASON_NOT_COLLECTED,
}

#: DTO 顶层必需字段（``validate_self_calls_dto`` 与文档共用同一份清单）。
REQUIRED_TOP_LEVEL_FIELDS = (
    "source",
    "synthetic",
    "schema_version",
    "transport",
    "scope",
    "window",
    "filters",
    "coverage",
    "counts",
    "content",
    "calls",
    "display_hints",
    "notes",
)

#: 一条调用记录**恰好**包含这些字段。任何多余字段都是缺陷：白名单而非黑名单，
#: 免得将来有人在 ``calls`` 里塞一个 ``debug`` 字段顺手带上别的东西。
CALL_ROW_FIELDS = (
    "call_id",
    "call_id_basis",
    "request_id",
    "ts",
    "key_id",
    "project",
    "workload",
    "model",
    "path",
    "method",
    "stream",
    "status",
    "outcome",
    "tokens",
    "queue_ms",
    "ttft_ms",
    "upstream_ms",
    "total_ms",
)

#: 一行里**禁止**出现的字段名（大小写不敏感）。
#:
#: 前九个来自 ``ingest.CONTENT_FIELD_NAMES``（正文类），在这里二次设防：即便将来
#: ``ingest`` 的契约被改动，本契约的自检也要求在出口再次挡住正文。
#: ``label`` 是**客户端可自报**的字段，汇总契约明确不让它参与分组（见
#: ``docs/atlas-data-contract.md`` §3.1）；逐条视图若把它显示出来，就等于凭空
#: 造出第二个更弱、且由调用方自己书写的身份来源，因此这里也不带它。
CALL_ROW_FORBIDDEN_FIELDS = frozenset(CONTENT_FIELD_NAMES | {"label"})

#: ``call_id_basis`` 的声明值：告诉下游这个 id 是**派生**的，不是上游签发的。
CALL_ID_BASIS = "derived-from-metadata-fields"


@dataclass(frozen=True)
class SelfCallsRequest:
    """一次「我的调用」查询。

    身份字段只来自 BFF 会话（``requester``）与服务端配置
    （``requested_key`` 的**合法性**由服务端目录判定）；其余是展示/过滤参数。
    """

    requester: RequesterContext
    window_from: datetime
    window_to: datetime
    requested_key: str | None = None
    outcome: str = "all"
    model: str | None = None
    limit: int = DEFAULT_LIMIT
    before_ts: datetime | None = None
    before_call_id: str | None = None
    content_consent: str = CONTENT_CONSENT_ABSENT
    #: 调用方**显式**声明「本次的数据确实来自已接线的真实 Gateway」。
    #: 默认 ``False``：不声明就是演示态，没有「默认 live」这条路。
    gateway_attached: bool = False


def validate_limit(limit: int) -> int:
    """校验分页大小。``bool`` 是 ``int`` 的子类，因此显式排除。"""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidRequest("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def validate_outcome_filter(outcome: str) -> str:
    if not isinstance(outcome, str) or outcome not in OUTCOME_FILTERS:
        raise InvalidRequest(f"unsupported outcome filter: {outcome!r}")
    return outcome


def validate_cursor(
    before_ts: datetime | None, before_call_id: str | None
) -> tuple[datetime | None, str | None]:
    """校验翻页游标：**要么都给，要么都不给**。

    只给一半意味着调用方对「上一页停在哪」的理解与本模块不同 —— 与其猜一个
    （比如悄悄忽略游标、从第一页重来，那会**重复**返回已经看过的行），不如拒绝。
    """
    if (before_ts is None) != (before_call_id is None):
        raise InvalidRequest("before_ts and before_call_id must be supplied together")
    if before_ts is None:
        return None, None
    if not isinstance(before_ts, datetime):
        raise InvalidRequest("before_ts must be a datetime")
    if before_ts.tzinfo is None or before_ts.utcoffset() is None:
        raise InvalidRequest("before_ts must carry an explicit UTC offset")
    if not isinstance(before_call_id, str) or not before_call_id.strip():
        raise InvalidRequest("before_call_id must be a non-empty string")
    return before_ts.astimezone(timezone.utc), before_call_id.strip()


def call_id_for(event: RawEvent) -> str:
    """由**元数据字段**派生一个稳定的调用标识。

    为什么要有它：逐条视图需要一个可以当 DOM key、可以进 URL 的稳定句柄，而上游
    并不保证提供 ``request_id``。派生值必须满足三条：

    * **稳定**：同一条观测在任何一次构建里都得到同一个 id；
    * **不泄漏**：它是单向摘要，不把 ``key_id`` 等内容原样拼回字符串；
    * **不冒充**：DTO 里同时给出 ``call_id_basis``，声明这是派生值。

    它**不是**授权依据，也不参与任何归属判定 —— 只用于去重与前端 key。
    """
    digest = hashlib.sha256()
    parts = (
        event.ts.astimezone(timezone.utc).isoformat(),
        event.key_id,
        str(event.status),
        event.outcome,
        event.model or "",
        event.path or "",
        event.method or "",
        event.request_id or "",
    )
    digest.update("\x00".join(parts).encode("utf-8"))
    return "call_" + digest.hexdigest()[:16]


def _token_block(event: RawEvent) -> dict:
    """Tokens 三件套。**任一侧缺失即整块 ``None``**，不用 0 补齐。"""
    input_tokens = event.input_tokens
    output_tokens = event.output_tokens
    if input_tokens is None or output_tokens is None:
        total = None
    else:
        total = input_tokens + output_tokens
    return {"input": input_tokens, "output": output_tokens, "total": total}


def _call_row(event: RawEvent, call_id: str, project: str, workload: str) -> dict:
    return {
        "call_id": call_id,
        "call_id_basis": CALL_ID_BASIS,
        "request_id": event.request_id,
        "ts": _iso_z(event.ts),
        "key_id": event.key_id,
        "project": project,
        "workload": workload,
        "model": event.model,
        "path": event.path,
        "method": event.method,
        "stream": event.stream,
        "status": event.status,
        "outcome": event.outcome,
        "tokens": _token_block(event),
        "queue_ms": event.queue_ms,
        "ttft_ms": event.ttft_ms,
        "upstream_ms": event.upstream_ms,
        "total_ms": event.total_ms,
    }


def build_self_calls_dto(
    request: SelfCallsRequest,
    text: str,
    directory: KeyDirectory,
    provenance: Provenance,
    limits: IngestLimits = IngestLimits(),
) -> dict:
    """构建「我的调用」DTO。纯函数：同输入必得同输出。"""
    if not isinstance(request, SelfCallsRequest):
        raise InvalidRequest("request must be a SelfCallsRequest")
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")
    if not isinstance(provenance, Provenance):
        raise InvalidRequest("provenance must be a Provenance")
    if not provenance.source.strip():
        raise InvalidRequest("provenance.source must not be empty")

    limit = validate_limit(request.limit)
    outcome_filter = validate_outcome_filter(request.outcome)
    before_ts, before_call_id = validate_cursor(request.before_ts, request.before_call_id)

    scope = resolve_own_call_scope(
        request.requester,
        request.requested_key,
        directory,
        request.content_consent,
    )
    # 列表视图没有桶的概念，但窗口上限与 UTC 归一化要复用同一份**已测**实现，
    # 免得这里长出一套平行的、更宽松的窗口规则。
    start, end = validate_window(request.window_from, request.window_to, "1h")

    ingest = ingest_jsonl(text, limits)

    own_keys = frozenset(scope.keys)
    own_revoked_keys = frozenset(scope.revoked_keys)
    authorized_projects = frozenset(scope.projects)

    matched: list[tuple[RawEvent, str, str]] = []
    window_excluded = 0
    revoked_key_rows = 0

    # **顺序即边界**（与 docs/atlas-data-contract.md §3.5 同构，但归属依据换成
    # principal）：
    #   1. key 不在受信目录        -> 结构上不可归属：不计数、不展示
    #   2. principal != subject    -> 别人的 key：静默丢弃，连计数都没有
    #   3. project 不在授权集       -> 静默丢弃
    #   4. 已吊销（本人）           -> 计入 revoked_key_rows，不出现在明细里
    #   5. 被 requested_key 收窄掉  -> 静默丢弃
    #   6. 窗口外                   -> window_excluded（本人数据，可如实告知）
    #   7. outcome / model 过滤     -> filtered_out
    # 第 2 步必须在计数之前：否则「你有 N 行别人的调用被过滤了」本身就是一个
    # 关于他人活动量的侧信道。
    for event in ingest.events:
        binding = directory.lookup(event.key_id)
        if binding is None:
            continue
        if binding.principal != scope.subject:
            continue
        if binding.project not in authorized_projects:
            continue
        if binding.revoked:
            # 已吊销的 key **不在** ``scope.keys`` 里（那是可见集合），所以这里
            # 必须查 ``own_revoked_keys``：否则这把 key 的行会被当成「不是我的」
            # 静默丢掉，连计数都没有 —— 一条本该如实告知的事实凭空消失。
            if binding.key_id in own_revoked_keys and start <= event.ts < end:
                revoked_key_rows += 1
            continue
        if event.key_id not in own_keys:
            continue
        if not (start <= event.ts < end):
            window_excluded += 1
            continue
        if outcome_filter != "all" and event.outcome != outcome_filter:
            continue
        if request.model is not None and event.model != request.model:
            continue
        matched.append(
            (event, binding.project, binding.workload or UNATTRIBUTED_WORKLOAD)
        )

    # **先给每条观测定下一个唯一 id，再拿这个 id 当排序键。**
    #
    # 反过来做（先按基础摘要排序、翻页时才补序号）会让排序键与出口自检用的键
    # 打架：补序号只会把 id **变大**，于是「同一秒、同一 key、同一模型、同一
    # request_id」的两条观测——上游重试或双写留下的重复行——会让自检读到一串
    # **递增**的游标，整条请求以 ContractError 收场（端点 500）。语料里出现重复行
    # 不是异常，是常态，所以这不是边界情形。
    #
    # 唯一 id 在**摄取顺序**上分配（``matched`` 由输入文本决定，与字典/集合的
    # 迭代顺序无关），因此同一次输入必然得到同一组 id，翻页与重复构建都稳定。
    decorated: list[tuple[RawEvent, str, str, str]] = []
    seen_base: dict[str, int] = {}
    for event, project, workload in matched:
        base = call_id_for(event)
        occurrence = seen_base.get(base, 0) + 1
        seen_base[base] = occurrence
        unique_id = base if occurrence == 1 else f"{base}-{occurrence}"
        decorated.append((event, project, workload, unique_id))

    # 排序：时间倒序，同一时刻用**唯一** id 兜底 —— 这个二元组正是出口自检
    # （``validate_self_calls_dto``）检查的那个键，所以「按它排序」与「它严格递减」
    # 不可能互相矛盾。游标也用它，因此 ``next_before`` 交出去的 id 与下一次过滤
    # 用的 id 是同一个值，翻页既不漏也不重。
    decorated.sort(key=lambda item: (item[0].ts, item[3]), reverse=True)

    if before_ts is not None and before_call_id is not None:
        decorated = [
            item for item in decorated if (item[0].ts, item[3]) < (before_ts, before_call_id)
        ]

    available = len(decorated)
    page = decorated[:limit]
    truncated = available > len(page)

    rows = [
        _call_row(event, call_id, project, workload)
        for event, project, workload, call_id in page
    ]

    next_before = None
    if truncated and page:
        last_event = page[-1][0]
        next_before = {"ts": _iso_z(last_event.ts), "call_id": rows[-1]["call_id"]}

    unattributed_rows = sum(1 for row in rows if row["workload"] == UNATTRIBUTED_WORKLOAD)

    dto = {
        "source": provenance.source,
        "synthetic": provenance.synthetic,
        "schema_version": CALLS_SCHEMA_VERSION,
        "transport": _transport_block(request, provenance),
        "scope": scope.to_dict(),
        "window": {"from": _iso_z(start), "to": _iso_z(end)},
        "filters": {
            "outcome": outcome_filter,
            "model": request.model,
            "limit": limit,
            "before": _cursor_block(before_ts, before_call_id),
        },
        "coverage": _coverage_block(rows),
        "counts": {
            "returned": len(rows),
            "available": available,
            "truncated": truncated,
            "next_before": next_before,
            "window_excluded": window_excluded,
            "revoked_key_rows": revoked_key_rows,
            "unattributed_rows": unattributed_rows,
        },
        "content": _content_block(scope),
        "calls": rows,
        "display_hints": _display_hints(request, provenance, scope),
        "notes": _notes(scope),
    }
    validate_self_calls_dto(dto)
    return dto


def _transport_block(request: SelfCallsRequest, provenance: Provenance) -> dict:
    """传输层自述。``live`` 需要两个条件同时成立。

    ``synthetic`` 为真时无论 ``gateway_attached`` 写什么都只能是 ``demo`` ——
    这样「把合成 fixture 接上，然后声明 live」这条最省事的路走不通。
    """
    live = bool(request.gateway_attached) and not provenance.synthetic
    return {
        "mode": TRANSPORT_MODE_LIVE if live else TRANSPORT_MODE_DEMO,
        "gateway_attached": bool(request.gateway_attached),
        "synthetic": provenance.synthetic,
        "label": "DEMO / 合成数据" if not live else "LIVE / 已接线",
    }


def _content_block(scope: OwnCallScope) -> dict:
    return {
        # 恒为 False：正文类字段在 ingest 就被拒绝，本管道**没有**正文可给。
        "included": False,
        "consent": scope.content_consent,
        "reason": CONTENT_REASONS[scope.content_consent],
        "opt_in_available": True,
        "note": (
            "调用日志只包含元数据（时间、状态、Token、排队与耗时）。"
            "prompt / completion 原文不在本管道的采集范围内；"
            "即便 opt-in 已开启，此处也不会返回正文。"
        ),
    }


def _coverage_block(rows: list[dict]) -> dict:
    """逐条的**可测性**统计：有多少行压根没记这个字段。

    与汇总 DTO 的 ``quality`` 刻意用不同的名字 —— 两者口径不同，共用一个词会让
    读者以为可以互相比较。
    """
    tokens_unknown = sum(1 for row in rows if row["tokens"]["total"] is None)
    ttft_unknown = sum(1 for row in rows if row["ttft_ms"] is None)
    queue_unknown = sum(1 for row in rows if row["queue_ms"] is None)
    request_id_absent = sum(1 for row in rows if row["request_id"] is None)
    return {
        "rows": QUALITY_MEASURED,
        "tokens_unknown_rows": tokens_unknown,
        "ttft_unknown_rows": ttft_unknown,
        "queue_unknown_rows": queue_unknown,
        "request_id_absent_rows": request_id_absent,
    }


def _cursor_block(before_ts: datetime | None, before_call_id: str | None) -> dict | None:
    if before_ts is None or before_call_id is None:
        return None
    return {"ts": _iso_z(before_ts), "call_id": before_call_id}


def _display_hints(
    request: SelfCallsRequest,
    provenance: Provenance,
    scope: OwnCallScope,
) -> list[str]:
    hints: list[str] = []
    if provenance.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：这不是真实 Gateway 的调用记录，不得用于对账。")
    if not (request.gateway_attached and not provenance.synthetic):
        hints.append("当前未接线真实 Gateway：本视图为 DEMO 态，transport.mode = demo。")
    hints.append(f"本视图只包含你名下 {len(scope.keys)} 把 key 的调用，按 principal 精确匹配。")
    if scope.revoked_keys:
        hints.append(
            f"你有 {len(scope.revoked_keys)} 把 key 已吊销，其调用已从明细中排除，只保留总数。"
        )
    if request.model is not None:
        hints.append(f"当前按 model = {request.model} 过滤。")
    if request.outcome != "all":
        hints.append(f"当前按 outcome = {request.outcome} 过滤。")
    hints.append("Token / 延迟为 null 表示上游没有记录该字段，不是 0。")
    return hints


def _notes(scope: OwnCallScope) -> list[str]:
    notes = [
        "「我的调用」与被授权项目的用量汇总是两条独立口径：前者按 principal 精确匹配，"
        "后者按项目汇总。两者互不派生，字段含义不可互换。",
        "只有你自己的 key 会被列出；未注册的 key、他人的 key 与已吊销的 key 在这里"
        "既不计数也不展示。",
    ]
    if scope.content_consent == CONTENT_CONSENT_GRANTED:
        notes.append(
            "content.consent = granted：服务端已记录你的 opt-in。这**不**改变 "
            "content.included —— 元数据管道不采集正文。"
        )
    elif scope.content_consent == CONTENT_CONSENT_WITHDRAWN:
        notes.append("content.consent = withdrawn：你的正文 opt-in 已撤回。")
    else:
        notes.append("content.consent = absent：你尚未就「正文访问」做出任何授权。")
    # 源文件级诊断**不**在这里露面：它属于 atlas:usage:global-diagnostics，
    # 且与本人无关。这里只声明「本页数字是逐条观测的汇总」，不暗示源文件状态。
    notes.append(
        "本视图的计数只来自逐条观测；源文件级诊断（坏行、正文行）需要"
        " atlas:usage:global-diagnostics，不在此处展示。"
    )
    return notes


def _iso_z(moment: datetime) -> str:
    """UTC ISO-8601。**有亚秒就写亚秒，不截断。**

    截断到秒不是「格式化偏好」，它会同时破坏两件事：

    * **出口自检**用行里的 ``ts`` 重建排序键，而排序用的是**真实**时刻。同一秒内的
      两行被截断后时间相等，自检退化成「按派生 id 比较」；只要 id 的字典序是升的，
      它就会以 ``calls must be strictly ordered newest-first`` 告败 —— 端点 500。
    * **翻页游标**会把最后一行的时刻写早，下一页从错误的位置继续，把同一秒里更早的
      行整段跳过。

    秒精度输入（现有 Gateway 导出）的输出**逐字节不变**：只有 ``microsecond`` 非零时
    才追加小数部分。
    """
    moment = moment.astimezone(timezone.utc)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def validate_self_calls_dto(dto: dict) -> None:
    """出口自检。**每一条都对应一个具体的泄漏或误报形态。**

    这个函数的存在意义不是「防御性编程」，而是让 :func:`build_self_calls_dto`
    里任何一次将来的改动都必须先满足这里的不变量才能返回。
    """
    if not isinstance(dto, dict):
        raise ContractError("self-calls DTO must be an object")
    for key in REQUIRED_TOP_LEVEL_FIELDS:
        if key not in dto:
            raise ContractError(f"self-calls DTO is missing required field {key!r}")
    if dto["schema_version"] != CALLS_SCHEMA_VERSION:
        raise ContractError("self-calls DTO schema_version mismatch")
    if not isinstance(dto["synthetic"], bool):
        raise ContractError("synthetic must be a boolean")

    transport = dto["transport"]
    if not isinstance(transport, dict):
        raise ContractError("transport must be an object")
    if transport.get("mode") not in (TRANSPORT_MODE_DEMO, TRANSPORT_MODE_LIVE):
        raise ContractError("transport.mode is not a supported value")
    if transport["mode"] == TRANSPORT_MODE_LIVE:
        # 唯一能自称 live 的组合。合成数据永远不能是 live。
        if dto["synthetic"] or not transport.get("gateway_attached"):
            raise ContractError(
                "transport.mode may be 'live' only when the payload is not "
                "synthetic and the gateway is attached"
            )

    scope_block = dto["scope"]
    if not isinstance(scope_block, dict):
        raise ContractError("scope must be an object")
    keys = scope_block.get("keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
        raise ContractError("scope.keys must be a non-empty list of key ids")
    if len(set(keys)) != len(keys):
        raise ContractError("scope.keys must not contain duplicates")
    projects = scope_block.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ContractError("scope.projects must be a non-empty list")
    if scope_block.get("content_consent") not in CONTENT_CONSENT_STATES:
        raise ContractError("scope.content_consent is not a supported value")

    content = dto["content"]
    if not isinstance(content, dict):
        raise ContractError("content must be an object")
    if content.get("included") is not False:
        # 唯一绝对不允许被改的不变量。
        raise ContractError(
            "content.included must be False: the metadata pipeline never carries "
            "prompt/completion text"
        )
    if content.get("consent") != scope_block.get("content_consent"):
        raise ContractError("content.consent must mirror scope.content_consent")

    calls = dto["calls"]
    if not isinstance(calls, list):
        raise ContractError("calls must be a list")

    authorize_keys = frozenset(keys)
    authorize_projects = frozenset(projects)
    seen: set[str] = set()
    previous: tuple[datetime, str] | None = None
    for row in calls:
        if not isinstance(row, dict):
            raise ContractError("each call must be an object")
        extra = set(row) - set(CALL_ROW_FIELDS)
        if extra:
            raise ContractError(f"call carries unexpected fields: {sorted(extra)}")
        for field in CALL_ROW_FIELDS:
            if field not in row:
                raise ContractError(f"call is missing field {field!r}")
        for field in row:
            if field.lower() in CALL_ROW_FORBIDDEN_FIELDS:
                raise ContractError(f"call must not carry field {field!r}")
        if row["key_id"] not in authorize_keys:
            raise ContractError("call.key_id is outside the authorised key set")
        if row["project"] not in authorize_projects:
            raise ContractError("call.project is outside the authorised project set")
        if row["call_id_basis"] != CALL_ID_BASIS:
            raise ContractError("call.call_id_basis must declare the id as derived")
        if row["call_id"] in seen:
            raise ContractError("call.call_id must be unique within a page")
        seen.add(row["call_id"])
        cursor = (_parse_iso_z(row["ts"]), row["call_id"])
        if previous is not None and cursor >= previous:
            raise ContractError("calls must be strictly ordered newest-first")
        previous = cursor
        tokens = row["tokens"]
        if not isinstance(tokens, dict):
            raise ContractError("call.tokens must be an object")
        for side in ("input", "output", "total"):
            value = tokens.get(side)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ContractError(f"call.tokens.{side} must be a non-negative integer or null")
        if (tokens["input"] is None or tokens["output"] is None) and tokens["total"] is not None:
            # 缺一侧却给出总数 —— 等于用 0 顶替未知，正是本契约禁止的事。
            raise ContractError(
                "call.tokens.total must be null whenever input or output is null"
            )
        for field in ("queue_ms", "ttft_ms", "upstream_ms", "total_ms"):
            value = row[field]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise ContractError(f"call.{field} must be a number or null")

    counts = dto["counts"]
    if not isinstance(counts, dict):
        raise ContractError("counts must be an object")
    for field in ("returned", "available", "window_excluded", "revoked_key_rows", "unattributed_rows"):
        value = counts.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"counts.{field} must be a measured non-negative integer")
    if counts["returned"] != len(calls):
        raise ContractError("counts.returned disagrees with the calls list")
    if counts["available"] < counts["returned"]:
        raise ContractError("counts.available must not be smaller than counts.returned")
    if not isinstance(counts.get("truncated"), bool):
        raise ContractError("counts.truncated must be a boolean")
    if counts["truncated"] != (counts["available"] > counts["returned"]):
        raise ContractError("counts.truncated disagrees with available/returned")
    if counts["truncated"]:
        if not isinstance(counts.get("next_before"), dict):
            raise ContractError("a truncated page must carry counts.next_before")
        if not isinstance(counts["next_before"].get("call_id"), str):
            raise ContractError("counts.next_before.call_id must be a string")
        if calls and counts["next_before"]["call_id"] != calls[-1]["call_id"]:
            raise ContractError("counts.next_before must point at the last returned call")
    elif counts.get("next_before") is not None:
        raise ContractError("an untruncated page must not carry counts.next_before")

    for key in ("display_hints", "notes"):
        value = dto[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ContractError(f"{key} must be a list of strings")
    if not dto["notes"]:
        raise ContractError("notes must not be empty: the view must state its own contract")


def _parse_iso_z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


__all__ = [
    "CALLS_SCHEMA_VERSION",
    "CALL_ID_BASIS",
    "CALL_ROW_FIELDS",
    "CALL_ROW_FORBIDDEN_FIELDS",
    "CONTENT_REASON_CONSENT_ABSENT",
    "CONTENT_REASON_CONSENT_WITHDRAWN",
    "CONTENT_REASON_NOT_COLLECTED",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "OUTCOME_FILTERS",
    "REQUIRED_TOP_LEVEL_FIELDS",
    "SelfCallsRequest",
    "TRANSPORT_MODE_DEMO",
    "TRANSPORT_MODE_LIVE",
    "build_self_calls_dto",
    "call_id_for",
    "validate_cursor",
    "validate_limit",
    "validate_outcome_filter",
    "validate_self_calls_dto",
]
