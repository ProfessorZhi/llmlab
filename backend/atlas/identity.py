"""受信身份与授权。

**两条不可跨越的边界：**

1. ``key_id -> principal/project/workload`` 只能来自服务端可信目录
   （:class:`KeyDirectory`，由运维经审批写入）。客户端可自报的 ``label``、
   HTTP header、body 字段**永不**参与授权或分组。
2. 请求者身份只能是 BFF 会话给出的 ``subject``；客户端若传 ``requested_subject``
   且与服务端不一致，直接拒绝，**不**回退到 admin。

``roles`` 只用于展示与审计，**不产生任何权限**：没有任何一条规则是
「因为 role == admin 所以放行」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import AccessDenied, InvalidRequest

#: 读取被授权项目的用量聚合所必需的 scope。
SCOPE_USAGE_READ = "atlas:usage:read"
#: 额外允许看到「没有可信项目归属」的**调用**计数（``unknown_key`` 等）。普通用户默认没有。
SCOPE_USAGE_UNATTRIBUTED = "atlas:usage:unattributed"
#: 额外允许看到**源文件级**诊断（损坏行 / 正文行计数）。
#:
#: 刻意与 ``SCOPE_USAGE_UNATTRIBUTED`` **分开**：前者是「哪些请求归不了属」，
#: 后者是「上游把什么不该来的东西写进了日志」。二者是不同的数据类别，
#: 合并成一个 scope 会让「未归属请求」与「源文件损坏」互相背书，
#: 也会让只看未归属量的主体顺带拿到全局损坏计数。
#: 两个 scope 都**不**因为 ``role == admin`` 自动获得。
SCOPE_USAGE_GLOBAL_DIAGNOSTICS = "atlas:usage:global-diagnostics"

#: 读取**自己**的逐条调用日志所必需的 scope。
#:
#: 刻意与 ``SCOPE_USAGE_READ`` **分开**，而不是「有 usage:read 就顺带能看自己的调用」：
#:
#: * 两者回答的是不同问题 —— 前者是「被授权项目的汇总」，后者是「我自己的调用」。
#:   同一份数据、两种口径，各自需要一次明确的授权决定；
#: * 合并会让「项目汇总」的授权悄悄附带一份逐条明细。明细比汇总更接近原始观测
#:   （逐条的时间戳本身就是行为指纹），不该由一份为别的目的授予的 scope 背书；
#: * 且 ``roles`` 依旧不产生任何权限：``role == admin`` 也不自动获得它。
#:
#: 这个 scope 与项目汇总的**关系**是「另一条独立口径」，**不是**后者的一个过滤器 ——
#: 见 ``docs/portal-call-log-contract.md`` 与 ``docs/atlas-data-contract.md`` §10.1。
SCOPE_CALLS_SELF_READ = "atlas:calls:self:read"

#: 逐条调用日志的**正文**可见性状态。取值即服务端记录在案的事实。
#:
#: 这是一份**授权状态**，不是内容本身。即便它是 ``GRANTED``，本模块也**永远**
#: 不返回 prompt / completion 原文 —— 元数据管道结构上就没有正文（``ingest`` 拒绝
#: 一切正文类字段）。该状态存在的意义是让前端能如实显示「你还没有开启/你已经开启」，
#: 而不是让任何一条路径把原文送到浏览器。
CONTENT_CONSENT_ABSENT = "absent"
CONTENT_CONSENT_GRANTED = "granted"
CONTENT_CONSENT_WITHDRAWN = "withdrawn"

CONTENT_CONSENT_STATES = (
    CONTENT_CONSENT_ABSENT,
    CONTENT_CONSENT_GRANTED,
    CONTENT_CONSENT_WITHDRAWN,
)


@dataclass(frozen=True)
class GatewayKeyBinding:
    """一条经审批的 ``key_id -> 主体/项目/工作负载`` 绑定。"""

    key_id: str
    principal: str
    project: str
    workload: str | None = None
    revoked: bool = False


class KeyDirectory:
    """服务端可信 key 目录（纯内存、无 IO）。"""

    def __init__(self, bindings: Iterable[GatewayKeyBinding]) -> None:
        by_key: dict[str, GatewayKeyBinding] = {}
        for binding in bindings:
            if not isinstance(binding, GatewayKeyBinding):
                raise InvalidRequest("directory entries must be GatewayKeyBinding")
            if not binding.key_id:
                raise InvalidRequest("binding key_id must not be empty")
            if binding.key_id in by_key:
                raise InvalidRequest(f"duplicate key_id in directory: {binding.key_id!r}")
            by_key[binding.key_id] = binding
        self._by_key = by_key

    def lookup(self, key_id: str) -> GatewayKeyBinding | None:
        """命中返回绑定；未注册返回 None（调用方按「无法归属」处理）。"""
        return self._by_key.get(key_id)

    def owned_by(self, principal: str) -> tuple[str, ...]:
        """``principal`` 名下的全部 ``key_id``（含已吊销），按字典序。

        这是**服务端目录**给出的归属事实，与请求里任何自报字段无关。是否把已吊销的
        那把也算进「可见」由调用方决定（本模块的逐条视图选择排除，见
        :func:`resolve_own_call_scope`），但「谁拥有它」不因此改变 —— 所以这个
        方法返回全部，把取舍留给唯一的上层决定。
        """
        if not isinstance(principal, str) or not principal:
            return ()
        return tuple(sorted(k for k, b in self._by_key.items() if b.principal == principal))

    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, key_id: object) -> bool:
        return key_id in self._by_key


@dataclass(frozen=True)
class RequesterContext:
    """BFF 会话解析出的请求者身份（受信通道，不来自请求体）。"""

    subject: str
    roles: frozenset[str]
    scopes: frozenset[str]
    authorized_projects: frozenset[str]


@dataclass(frozen=True)
class AuthorizedScope:
    """授权解析结果，会原样写进 DTO 的 ``scope`` 字段以便审计。"""

    subject: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    projects: tuple[str, ...]
    unattributed_visible: bool
    global_diagnostics_visible: bool
    cross_project: bool

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "projects": list(self.projects),
            "unattributed_visible": self.unattributed_visible,
            "global_diagnostics_visible": self.global_diagnostics_visible,
            "cross_project": self.cross_project,
        }


def resolve_authorized_scope(
    requester: RequesterContext,
    requested_subject: str | None,
    requested_project: str | None,
) -> AuthorizedScope:
    """校验身份/scope/项目白名单并返回授权范围。失败抛 AccessDenied。"""
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a RequesterContext")

    subject = (requester.subject or "").strip()
    if not subject:
        raise AccessDenied("no authenticated subject in the BFF session")

    if requested_subject is not None and requested_subject.strip() != subject:
        # 关键：不「改用别人的身份」，也不因为别处是 admin 就放行。
        raise AccessDenied("requested subject does not match the session subject")

    if SCOPE_USAGE_READ not in requester.scopes:
        raise AccessDenied(f"missing required scope {SCOPE_USAGE_READ}")

    authorized = frozenset(requester.authorized_projects)
    if not authorized:
        raise AccessDenied("no authorized projects for this subject")

    if requested_project is not None:
        if requested_project not in authorized:
            raise AccessDenied("requested project is not in the authorized set")
        selected = (requested_project,)
    else:
        selected = tuple(sorted(authorized))

    return AuthorizedScope(
        subject=subject,
        roles=tuple(sorted(requester.roles)),
        scopes=tuple(sorted(requester.scopes)),
        projects=selected,
        unattributed_visible=SCOPE_USAGE_UNATTRIBUTED in requester.scopes,
        global_diagnostics_visible=SCOPE_USAGE_GLOBAL_DIAGNOSTICS in requester.scopes,
        cross_project=len(selected) > 1,
    )


@dataclass(frozen=True)
class OwnCallScope:
    """「我自己的调用」视图的授权解析结果。

    ``keys`` 是本次可以看到的 ``key_id`` 集合，由**服务端目录**的
    ``principal == subject`` 推出 —— 不是任何请求字段。
    """

    subject: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    projects: tuple[str, ...]
    keys: tuple[str, ...]
    requested_key: str | None
    revoked_keys: tuple[str, ...]
    content_consent: str

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "projects": list(self.projects),
            "keys": list(self.keys),
            "requested_key": self.requested_key,
            "revoked_keys": list(self.revoked_keys),
            "cross_key": len(self.keys) > 1,
            "content_consent": self.content_consent,
        }


def resolve_own_call_scope(
    requester: RequesterContext,
    requested_key: str | None,
    directory: KeyDirectory,
    content_consent: str = CONTENT_CONSENT_ABSENT,
) -> OwnCallScope:
    """解析「我的调用」视图范围。失败一律抛 :class:`AccessDenied`。

    判定顺序（顺序本身是安全边界）：

    1. ``requester`` 类型正确、会话 subject 非空；
    2. 持有 :data:`SCOPE_CALLS_SELF_READ`；
    3. ``authorized_projects`` 非空；
    4. 从**可信目录**取出 ``principal == subject`` 的 key；
    5. 丢掉不在 ``authorized_projects`` 内的 key（项目仍是最外层边界）；
    6. 丢掉已吊销的 key，但把它们单独记进 ``revoked_keys``（属于本人，可告知）；
    7. 若指定了 ``requested_key``，它必须在第 4~6 步得到的集合里，否则拒绝。

    第 7 步是**同一个**拒绝，不区分「别人的 key」「不存在的 key」「我自己的已吊销
    key」三种成因：一旦区分，端点就变成一个可枚举他人 ``key_id`` 存在性的预言机。
    """
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a RequesterContext")

    subject = (requester.subject or "").strip()
    if not subject:
        raise AccessDenied("no authenticated subject in the BFF session")

    if SCOPE_CALLS_SELF_READ not in requester.scopes:
        raise AccessDenied(f"missing required scope {SCOPE_CALLS_SELF_READ}")

    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")

    authorized = frozenset(requester.authorized_projects)
    if not authorized:
        raise AccessDenied("no authorized projects for this subject")

    if content_consent not in CONTENT_CONSENT_STATES:
        raise InvalidRequest(f"unknown content_consent state: {content_consent!r}")

    owned = directory.owned_by(subject)
    # 项目白名单先于吊销：与聚合路径同序（见 docs/atlas-data-contract.md §3.5），
    # 免得「这把 key 属不属于别的项目」从拒绝理由里漏出去。
    in_project = tuple(k for k in owned if directory.lookup(k).project in authorized)
    revoked = tuple(k for k in in_project if directory.lookup(k).revoked)
    visible = tuple(k for k in in_project if not directory.lookup(k).revoked)

    if not visible:
        # 一把都看不到 —— 和聚合路径「authorized_projects 为空即拒绝」同构。
        # 这里**不**返回一个空列表的 200：那会把「你没有 key」与「你不被允许」
        # 合成一个可区分的状态，而对调用方来说，两者的正确响应都是「去找管理员」。
        raise AccessDenied("no call-log keys are owned by this subject")

    if requested_key is not None:
        if not isinstance(requested_key, str) or requested_key.strip() not in visible:
            raise AccessDenied("requested key is not one of the caller's own keys")
        selected: tuple[str, ...] = (requested_key.strip(),)
    else:
        selected = visible

    return OwnCallScope(
        subject=subject,
        roles=tuple(sorted(requester.roles)),
        scopes=tuple(sorted(requester.scopes)),
        projects=tuple(sorted(authorized)),
        keys=selected,
        requested_key=requested_key.strip() if requested_key else None,
        revoked_keys=revoked,
        content_consent=content_consent,
    )
