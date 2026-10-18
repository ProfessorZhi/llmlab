"""回环预览用的「我的调用」只读端点：受信 Session -> 合成调用日志 DTO。

这是 ``dev/preview_auth.py`` 的**开发专用**附件，职责与 :mod:`dev.atlas_preview`
平行、但**不共用**任何产出：

1. 一份**服务端受信**的演示 key 目录（``principal`` 在启动时绑定到本次演示主体）；
2. 一份**离线合成** fixture，每行都带 ``SYNTHETIC-PREVIEW`` 标注；
3. 一个把 ``(会话用户, 查询参数)`` 映射成 DTO 的纯函数。

它**不是**一条新的授权规则。真正的判定仍然是
:func:`backend.atlas.identity.resolve_own_call_scope`（缺 scope、非本人的 key 一律
``AccessDenied``）；这里只是把「谁是谁」从**会话**取出来喂进去。

**为什么目录要按主体生成。** 逐条视图的归属依据是 ``binding.principal == subject``，
所以演示目录里的 principal 必须是本次演示账号的真实 id —— 否则演示账号一把 key 都
不拥有，端点只会给出 403，看不出任何东西。

**为什么目录在启动期**只**构建一次。** 这是一个必须固定的东西：如果按**每个请求**
的调用者去生成目录，那么每一个获授账号都会「拥有」同一批演示 key —— 归属判定就
退化成「谁登录谁拥有」，跨用户边界的演示会**恰好反过来**变成它的反面。所以目录由
:class:`SelfCallsGrant` 在配置期绑定到一个主体，请求路径上只做比对，不生成。

**为什么只支持一个演示主体。** 一份 fixture 只覆盖一组 key。两个主体共享一份目录时
必然有一个人看到的是别人的数据；各自生成一份又会让归属判定失效（见上）。与其在两
个错误之间选一个，不如在配置期就拒绝，把这件事说清楚。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from backend.atlas import (
    CONTENT_CONSENT_ABSENT,
    CONTENT_CONSENT_GRANTED,
    CONTENT_CONSENT_WITHDRAWN,
    SCOPE_CALLS_SELF_READ,
    GatewayKeyBinding,
    KeyDirectory,
    Provenance,
    SelfCallsRequest,
    build_self_calls_dto,
)

#: 预览里「我的调用」端点的路径。**只**由 ``dev/preview_auth.py`` 提供。
CALLS_PATH = "/api/atlas/calls"

#: 写进 DTO ``source`` 的值，与 Atlas 端点刻意用同一个标记：两者都是合成预览。
SYNTHETIC_SOURCE = "synthetic-preview"

FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "atlas_calls_sample.jsonl"
)

#: 演示窗口。理由与 :mod:`dev.atlas_preview` 相同：窗口是**演示数据**的一部分，
#: 不是用服务器时钟推出来的。用 ``now - 1h`` 只会得到一个空视图，而空视图看起来
#: 像「你最近没有调用」—— 一个自己造出来的结论。
DEMO_WINDOW_FROM = "2026-10-14T00:00:00Z"
DEMO_WINDOW_TO = "2026-10-14T04:00:00Z"

#: 演示主体被授予的 scope。刻意**不**含 ``atlas:usage:read``：
#: 逐条视图与项目汇总是两条口径，演示也应该各开各的。
GRANT_SCOPES = (SCOPE_CALLS_SELF_READ,)

#: 演示主体被授权看到的项目。
DEMO_PROJECTS = ("proj-a",)

#: ``principal`` 是本次演示主体名下的 key。``revoked=True`` 的那把会出现在
#: ``scope.revoked_keys`` 与 ``counts.revoked_key_rows`` 里，但不出现在明细中 ——
#: 这正是演示要展示的边界之一。
DEMO_OWN_KEYS = (
    ("key-alpha", "proj-a", "resume-parse", False),
    ("key-orphan", "proj-a", None, False),
    ("key-retired", "proj-a", "resume-parse", True),
)

#: 演示库里**别人**的 key。它们的存在是为了让「跨用户 403」在预览里可复现：
#: 演示主体拿着自己的会话去请求 ``key-beta`` 必须被拒。
DEMO_FOREIGN_KEYS = (
    ("key-beta", "u-demo-colleague", "proj-a", "code-assist"),
    ("key-gamma", "u-demo-colleague", "proj-b", "eval-suite"),
)

#: 允许出现在「我的调用」查询串里的参数。其余一律忽略 —— 尤其是 ``subject``、
#: ``principal``、``role``、``x_principal``：它们看着像身份，实则与授权无关，
#: 本视图的身份**只有**会话一个来源。
QUERY_PARAMS = (
    "from",
    "to",
    "key",
    "outcome",
    "model",
    "limit",
    "before_ts",
    "before_call_id",
)

#: 每次响应都自称的来源。``Provenance`` 没有默认值，正是为了不让调用方默默把
#: 合成数据说成生产统计。
PROVENANCE = Provenance(source=SYNTHETIC_SOURCE, synthetic=True)


def demo_window() -> tuple[datetime, datetime]:
    return _parse_utc(DEMO_WINDOW_FROM), _parse_utc(DEMO_WINDOW_TO)


def read_fixture_text() -> str:
    """按 UTF-8 读取那唯一一份合成 fixture 原文。"""
    return FIXTURE_PATH.read_text(encoding="utf-8")


def build_directory(subject: str) -> KeyDirectory:
    """为**一个**演示主体构建受信 key 目录。

    ``subject`` 是**配置期**确定的主体（见 :class:`SelfCallsGrant`），不是请求里的
    调用者。把这两者搞混——按每个请求的调用者生成目录——会让每个获授账号都「拥有」
    同一批演示 key，跨用户边界的演示就变成了它的反面。
    """
    principal = (subject or "").strip()
    if not principal:
        raise ValueError("a self-calls directory needs a non-empty subject")

    bindings = [
        GatewayKeyBinding(key_id, principal, project, workload, revoked=revoked)
        for key_id, project, workload, revoked in DEMO_OWN_KEYS
    ]
    bindings.extend(
        GatewayKeyBinding(key_id, owner, project, workload)
        for key_id, owner, project, workload in DEMO_FOREIGN_KEYS
    )
    return KeyDirectory(bindings)


@dataclass(frozen=True)
class SelfCallsGrant:
    """配置期确定的**唯一**一份逐条视图授权：主体 + 该主体的受信 key 目录。

    两个字段绑在一起，是因为它们必须同时确定：目录里的 ``principal`` 就是这里的
    ``subject``，而 :meth:`PreviewApplication._calls` 只把请求的会话主体与
    ``subject`` 做**相等**比对。没有任何一条请求路径能生成或修改它。
    """

    subject: str
    directory: KeyDirectory

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("a self-calls grant needs a non-empty subject")
        if not isinstance(self.directory, KeyDirectory):
            raise ValueError("a self-calls grant needs a KeyDirectory")

    @classmethod
    def for_subject(cls, subject: str) -> "SelfCallsGrant":
        return cls(subject=subject.strip(), directory=build_directory(subject))


def requester_context(user, subject: str):
    """会话用户 + 服务端 grant -> 逐条视图要求的请求者上下文。

    ``user.role`` 只进 ``roles``（展示/审计用，不产生权限）；``subject`` 是
    **会话**给出的主体，不是请求里的任何一个字段。
    """
    from backend.atlas import RequesterContext

    return RequesterContext(
        subject=subject,
        roles=frozenset({user.role}),
        scopes=frozenset(GRANT_SCOPES),
        authorized_projects=frozenset(DEMO_PROJECTS),
    )


class ContentConsentRegistry:
    """``subject -> 正文 opt-in 状态`` 的进程内表。

    与 :class:`dev.atlas_preview.AtlasPreviewGrants` 同样只有两个入口：构造与
    :meth:`grant` / :meth:`withdraw`。两者都是**进程内**动作，HTTP 面上没有任何
    路径能到达它 —— 所以浏览器无法给自己开一个 opt-in。

    而且即便状态是 ``granted``，本端点也不会返回正文：元数据管道里没有正文
    （见 :mod:`backend.atlas.calls` 的模块文档）。这张表的作用仅仅是让前端能
    如实显示「未开启 / 已开启」这个**授权状态**。
    """

    def __init__(self, granted: Iterable[str] = ()) -> None:
        self._by_subject: dict[str, str] = {
            subject: CONTENT_CONSENT_GRANTED for subject in granted
        }

    def state_for(self, subject: str) -> str:
        return self._by_subject.get(subject, CONTENT_CONSENT_ABSENT)

    def grant(self, subject: str) -> None:
        self._by_subject[subject] = CONTENT_CONSENT_GRANTED

    def withdraw(self, subject: str) -> None:
        self._by_subject[subject] = CONTENT_CONSENT_WITHDRAWN

    def __len__(self) -> int:
        return len(self._by_subject)


def build_dto(
    *,
    user,
    grant: SelfCallsGrant,
    requested_key: str | None = None,
    outcome: str = "all",
    model: str | None = None,
    limit: int = 50,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    before_ts: datetime | None = None,
    before_call_id: str | None = None,
    content_consent: str = CONTENT_CONSENT_ABSENT,
) -> dict:
    """构建演示 DTO。非本人的 key / 缺 scope 由 ``backend.atlas`` 抛 ``AccessDenied``。

    ``grant`` 来自**配置期**，请求路径上不构造它 —— 目录与主体因此不可能被一次
    请求影响。

    窗口缺省即演示窗口；给了就更窄（范围校验是 ``backend.atlas`` 的既有实现：
    半开区间、必须带时区、跨度有上限）。窗口只能**收窄**查得到的范围，收窄到没有
    数据就是空列表 —— 那是一个诚实的「这段时间没有调用」。
    """
    default_from, default_to = demo_window()
    window_from = default_from if window_from is None else window_from
    window_to = default_to if window_to is None else window_to
    request = SelfCallsRequest(
        requester=requester_context(user, grant.subject),
        window_from=window_from,
        window_to=window_to,
        requested_key=requested_key,
        outcome=outcome,
        model=model,
        limit=limit,
        before_ts=before_ts,
        before_call_id=before_call_id,
        content_consent=content_consent,
        # 预览**没有**接线真实 Gateway，因此这里永远是 False。没有任何运行参数
        # 能把它变成 True —— 想让它变真，必须先真的接上 Gateway 并改这一行。
        gateway_attached=False,
    )
    return build_self_calls_dto(request, read_fixture_text(), grant.directory, PROVENANCE)


def build_payload(
    *,
    user,
    grant: SelfCallsGrant,
    observed_at: float,
    requested_key: str | None = None,
    outcome: str = "all",
    model: str | None = None,
    limit: int = 50,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    before_ts: datetime | None = None,
    before_call_id: str | None = None,
    content_consent: str = CONTENT_CONSENT_ABSENT,
) -> dict:
    """端点 ``data`` 块：DTO 本体 + 这份响应**是在什么时候**被构建的。

    字段名是 ``call_log`` 而不是 ``calls``：DTO 自己的 ``calls`` 是**行列表**，
    同名嵌套会让读者把两层含义读混。
    """
    return {
        "observed_at": _iso_z(observed_at),
        "call_log": build_dto(
            user=user,
            grant=grant,
            requested_key=requested_key,
            outcome=outcome,
            model=model,
            limit=limit,
            window_from=window_from,
            window_to=window_to,
            before_ts=before_ts,
            before_call_id=before_call_id,
            content_consent=content_consent,
        ),
    }


def parse_ts(raw: str, *, field: str) -> datetime:
    """把 ``from`` / ``to`` / ``before_ts`` 解析成带时区的 ``datetime``。

    失败抛 ``ValueError``（由调用方映射成 400）。要求显式时区：一个没有偏移的
    时间戳在「半开区间」语义下有两种截然不同的读法，猜一个就是在猜授权边界。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must carry an explicit UTC offset")
    return parsed


def parse_limit(raw: str) -> int:
    """解析 ``limit``。非十进制整数一律拒绝（不试着「宽容解析」）。"""
    text = (raw or "").strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError("limit must be a decimal integer")
    return int(text)


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


__all__ = [
    "CALLS_PATH",
    "ContentConsentRegistry",
    "DEMO_FOREIGN_KEYS",
    "DEMO_OWN_KEYS",
    "DEMO_PROJECTS",
    "DEMO_WINDOW_FROM",
    "DEMO_WINDOW_TO",
    "FIXTURE_PATH",
    "GRANT_SCOPES",
    "PROVENANCE",
    "QUERY_PARAMS",
    "SYNTHETIC_SOURCE",
    "SelfCallsGrant",
    "build_directory",
    "build_dto",
    "build_payload",
    "demo_window",
    "parse_limit",
    "parse_ts",
    "read_fixture_text",
    "requester_context",
    "CONTENT_CONSENT_WITHDRAWN",
]
