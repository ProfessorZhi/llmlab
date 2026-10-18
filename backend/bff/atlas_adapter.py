"""``backend/atlas`` ⇒ ``/api/atlas/calls`` 的接线层（Calls / Atlas Lane #77）。

**这一层只做三件事，而且刻意只做三件事：**

1. 把 BFF 已经解析好的**受信主体**（:class:`backend.bff.identity.Principal`）
   翻成 Atlas 自己的 ``RequesterContext``；
2. 把服务端装配好的**语料**（``gateway_text``）与**可信 key 目录**交给 Atlas
   自己的 builder；
3. 把它返回的 DTO 原样交给路由。

``/api/atlas/calls`` 这条路由以前不存在，所以 ``backend/atlas`` 是一个
**module-only** 模块：它的代码在，浏览器调不到。接上它需要一条同源路由，而那条
路由的守卫归本 Lane —— **被接线模块的行为仍归 Calls/Atlas Lane**，本文件一行都
不改它的判定。

三条不能在这里重做的决定
------------------------

**主体不是参数。** ``subject`` 只来自会话。Atlas 的
``resolve_own_call_scope`` 会把「别人的 key / 不存在的 key / 我自己的已吊销 key」
塌成同一个拒绝，正是为了不做成一个能枚举他人 ``key_id`` 的预言机；本层若允许
``?key=`` 之类的自报参数并自己先判一遍，那层防护就被绕过了。所以这里**没有**
任何来自查询串的身份字段。

**正文同意状态不从浏览器来。** ``content_consent`` 在 Atlas 里是一份**服务端
记录在案的事实**。本层恒传 ``CONTENT_CONSENT_ABSENT``：那是保守的那一端，而且
与「本模块结构上就不返回正文」这件事一致 —— 让浏览器声明「我同意看正文」，
只会制造一条没有正文可给的通道，以及一个将来有人会去填的洞。

**语料由装配决定，不由请求决定。** ``gateway_text`` 是注入的 callable，与
观测适配器同一个口径：默认返回空串，空串的诚实结局是「没有样本」，
不是 0、更不是一条编出来的调用记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..atlas import calls as atlas_calls
from ..atlas.dto import Provenance as AtlasProvenance
from ..atlas.identity import (
    CONTENT_CONSENT_ABSENT,
    SCOPE_CALLS_SELF_READ,
    KeyDirectory,
    RequesterContext,
)
from ..atlas.service import MAX_WINDOW_HOURS

#: 「我的调用」默认窗口与前端页面的默认窗口一致。
DEFAULT_WINDOW_HOURS = 24

#: 一个来源都没有装配时，``attached=False`` 的诚实替身。它回答的是「本部署没有
#: 这条来源」，而不是「这条来源回了空」—— 对前端那是两件事。
REASON_ATLAS_NOT_ATTACHED = "atlas_adapter_not_attached"


@dataclass(frozen=True)
class AtlasStack:
    """接线 Calls 面所需的一切，全部由调用方注入。

    ``gateway_text(identity)`` 返回原文 Gateway JSONL。**本层不读文件、不开
    socket**：生产里这个 callable 会去读真正的导出口 / 已落地的语料，而默认实现
    返回空串 —— 空串的诚实结局是「没有样本」。它收 ``identity`` 是因为语料按会话
    授权端点生成，与观测那一条同源。

    ``project_scope(identity)`` 回答「这个主体被授权看哪些项目」。它**不是**从
    请求里读的：权威在项目目录（``backend.bff.stacks.project_directory`` 那一份，
    评测面用的是同一个），本层只是把它传下去。
    """

    directory: object
    provenance: AtlasProvenance
    gateway_text: Callable[[object], str] = lambda _identity: ""
    project_scope: Callable[[object], frozenset[str]] = lambda _identity: frozenset()
    limits: object | None = None
    #: 装配方**显式**声明「这份语料确实来自已接线的真实 Gateway」。
    #: 默认 ``False``：不声明就是演示态——Atlas 自己的口径，本层照抄，
    #: 不替它把默认翻过来。
    gateway_attached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.directory, KeyDirectory):
            raise ValueError("directory must be an atlas KeyDirectory")
        if not isinstance(self.provenance, AtlasProvenance):
            raise ValueError("provenance must be an atlas Provenance")
        if not callable(self.gateway_text):
            raise ValueError("gateway_text must be callable")
        if not callable(self.project_scope):
            raise ValueError("project_scope must be callable")


class ModuleAtlasAdapter:
    """把 ``backend/atlas`` 接成 BFF 的 Calls 来源。"""

    name = "atlas"
    attached = True

    def __init__(
        self,
        stack: AtlasStack,
        *,
        clock: Callable[[], datetime] | None = None,
        window_hours: int = DEFAULT_WINDOW_HOURS,
    ) -> None:
        if not 1 <= window_hours <= MAX_WINDOW_HOURS:
            raise ValueError(f"window_hours must be within 1..{MAX_WINDOW_HOURS}")
        self.stack = stack
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.window_hours = window_hours

    # --- 主体桥接 ---------------------------------------------------------

    def requester_for(self, identity) -> RequesterContext:
        """受信主体 ⇒ Atlas 的主体类型。scope 来自角色，项目来自受信目录。"""
        return RequesterContext(
            subject=identity.principal,
            # roles 在 Atlas 里只用于展示与审计，不产生任何权限（见 atlas.identity）。
            roles=frozenset(identity.requester.roles),
            # 两套 scope 名在这里是**同一个词**（``atlas:calls:self:read``），
            # 所以直接取交集，而不是在两边各写一遍字符串。
            scopes=frozenset(identity.portal_scopes) & frozenset({SCOPE_CALLS_SELF_READ}),
            authorized_projects=frozenset(self.stack.project_scope(identity)),
        )

    # --- 可用性 -----------------------------------------------------------

    def availability(self, *, identity) -> tuple[bool, str | None]:
        """这个会话能不能读自己的调用。**由 scope 判定**，不靠试探调用。"""
        if SCOPE_CALLS_SELF_READ in identity.portal_scopes:
            return True, None
        return False, "missing_required_scope"

    # --- 逐条调用 ---------------------------------------------------------

    def self_calls(
        self,
        *,
        identity,
        outcome: str = "all",
        model: str | None = None,
        limit: int | None = None,
        before_ts: datetime | None = None,
        before_call_id: str | None = None,
    ) -> dict:
        """「我的调用」DTO。窗口由**服务端**的时钟与时长决定，不由请求挑。"""
        now = self._clock()
        request = atlas_calls.SelfCallsRequest(
            requester=self.requester_for(identity),
            window_from=now - timedelta(hours=self.window_hours),
            window_to=now,
            requested_key=None,
            outcome=outcome,
            model=model,
            limit=limit if limit is not None else atlas_calls.DEFAULT_LIMIT,
            before_ts=before_ts,
            before_call_id=before_call_id,
            # 见模块说明：正文同意状态不从浏览器来。
            content_consent=CONTENT_CONSENT_ABSENT,
            # 声明本次的数据是不是来自**已接线的真实 Gateway**。取装配方显式
            # 写下的那个取值，不让本层替它把默认翻过来。
            gateway_attached=bool(self.stack.gateway_attached),
        )
        return atlas_calls.build_self_calls_dto(
            request,
            self._text(identity),
            self.stack.directory,
            self.stack.provenance,
            **self._limits_kwargs(),
        )

    def _limits_kwargs(self) -> dict:
        return {"limits": self.stack.limits} if self.stack.limits is not None else {}

    def _text(self, identity) -> str:
        text = self.stack.gateway_text(identity)
        return text if isinstance(text, str) else ""


class DetachedAtlasAdapter:
    """没有 Calls 来源时的诚实答复：路由以 503 收场，而不是回一份空列表。

    空列表在这里是个**有意义的测量结果**（「这个窗口里你确实没有调用」），
    而「本部署没有这条来源」不是。用同一个 200 表达两者，等于把「没接」说成
    「没调用」。

    ``reason`` 可以按实例给：Calls Lane 的 ``AtlasSource`` 在装配失败时会带一个
    更具体的稳定码（``source.file_missing`` / ``source.outside_root`` /
    ``source.too_large`` ……）。那些码回答的是「去查哪一件事」，比笼统的
    「adapter 没接」有用得多，而且它们**不泄露内容**——只说明哪一道有界检查没过。
    不传时退回本层自己的 ``atlas_adapter_not_attached``。
    """

    name = "detached"
    attached = False
    reason = REASON_ATLAS_NOT_ATTACHED

    def __init__(self, *, reason: str | None = None) -> None:
        if reason:
            self.reason = str(reason)

    def availability(self, *, identity) -> tuple[bool, str | None]:
        del identity
        return False, self.reason


__all__ = [
    "DEFAULT_WINDOW_HOURS",
    "REASON_ATLAS_NOT_ATTACHED",
    "AtlasStack",
    "DetachedAtlasAdapter",
    "ModuleAtlasAdapter",
]
