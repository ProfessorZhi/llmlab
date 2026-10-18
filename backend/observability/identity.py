"""受信身份、授权范围、以及 key 归属目录。

**三条不可跨越的边界：**

1. ``key_id -> uid/project/workload`` **只能**来自服务端可信目录（:class:`KeyDirectory`，
   由运维经审批写入）。客户端可自报的 ``label``、HTTP header、body 字段**永不**参与
   归属或分组 —— 现有 Gateway 元数据里那个 ``x_principal`` / ``X-Forwarded-User``
   一律当噪声丢掉（回归见 ``tests/observability/test_gateway_ingest.py``）。
2. 请求者身份只能是 BFF 会话给出的 ``uid``；客户端若传 ``requested_uid`` 且与服务端
   不一致，直接拒绝，**不**回退到 admin。
3. ``roles`` 只用于展示与审计，**不产生任何权限**：没有任何一条规则是
   「因为 role == admin 所以放行」。管理视角靠的是显式 scope。

第 3 条在这里尤其重要：本模块会暴露**全局物理 GPU** 与**全体用户**的用量摘要，
一旦 role 能开门，任何一次角色误配就是把全机观测面交出去。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import AccessDenied, InvalidRequest

#: 读取**自己**的用量与**自己被授权端点**的状态所必需的 scope。
SCOPE_SELF_READ = "observability:self:read"

#: 读取全局视图（全体用户用量摘要、全部引擎、服务健康）所必需的 scope。
SCOPE_ADMIN_READ = "observability:admin:read"

#: 读取**物理**整机 / 整卡读数（CPU、内存、磁盘、GPU 显存与温度）所必需的 scope。
#:
#: 刻意与 :data:`SCOPE_ADMIN_READ` **分开**：物理读数不含任何人的身份，因此可以在
#: 不给「全体用户用量摘要」的前提下单独授予（例如给一个只做容量告警的看板账号）。
#: 反过来，管理摘要也不该顺带把整机读数塞给一个只想知道用户用量的人。
#: 两个 scope 都**不**因为 ``role == admin`` 自动获得。
SCOPE_HOST_READ = "observability:host:read"

#: 本模块认识的全部 scope。
ALL_SCOPES = frozenset({SCOPE_SELF_READ, SCOPE_ADMIN_READ, SCOPE_HOST_READ})


@dataclass(frozen=True)
class GatewayKeyBinding:
    """一条经审批的 ``key_id -> uid/项目/工作负载`` 绑定。"""

    key_id: str
    uid: str
    project: str
    workload: str | None = None
    revoked: bool = False


class KeyDirectory:
    """服务端可信 key 目录（纯内存、无 IO）。

    这是**唯一**的归属来源。任何「从日志行里读 label / header 来推断是谁」的做法
    都在结构上被绕开了：归属判定的入参只有 ``key_id``，而 ``key_id`` 只能在这里查到。
    """

    def __init__(self, bindings: Iterable[GatewayKeyBinding]) -> None:
        by_key: dict[str, GatewayKeyBinding] = {}
        for binding in bindings:
            if not isinstance(binding, GatewayKeyBinding):
                raise InvalidRequest("directory entries must be GatewayKeyBinding")
            if not binding.key_id:
                raise InvalidRequest("binding key_id must not be empty")
            if not binding.uid:
                raise InvalidRequest("binding uid must not be empty")
            if binding.key_id in by_key:
                raise InvalidRequest(f"duplicate key_id in directory: {binding.key_id!r}")
            by_key[binding.key_id] = binding
        self._by_key = by_key

    def lookup(self, key_id: str) -> GatewayKeyBinding | None:
        """命中返回绑定；未注册返回 ``None``（调用方按「无法归属」处理）。"""
        return self._by_key.get(key_id)

    def owned_by(self, uid: str) -> tuple[str, ...]:
        """``uid`` 名下的全部 ``key_id``（含已吊销），按字典序。

        这是**服务端目录**给出的归属事实，与请求里任何自报字段无关。
        """
        if not isinstance(uid, str) or not uid:
            return ()
        return tuple(sorted(k for k, b in self._by_key.items() if b.uid == uid))

    def uids(self) -> tuple[str, ...]:
        """目录里出现过的全部 ``uid``（按字典序）。仅管理视角使用。"""
        return tuple(sorted({b.uid for b in self._by_key.values()}))

    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, key_id: object) -> bool:
        return key_id in self._by_key


@dataclass(frozen=True)
class RequesterPrincipal:
    """BFF 会话解析出的请求者身份（受信通道，不来自请求体）。

    ``authorized_endpoints`` 是**服务端**给出的「此人可用哪些端点」白名单，
    它同时充当模型/引擎目录的可见性边界：未列出的端点在本人的任何响应里
    **不存在** —— 不是「显示为 0」，而是根本不出现。
    """

    uid: str
    roles: frozenset[str]
    scopes: frozenset[str]
    authorized_endpoints: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.uid, str) or not self.uid.strip():
            raise InvalidRequest("principal uid must be a non-empty string")


@dataclass(frozen=True)
class ObserverScope:
    """授权解析结果，会原样写进 DTO 的 ``scope`` 字段以便审计。"""

    uid: str
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    endpoints: tuple[str, ...]
    viewing_uid: str
    admin: bool
    host_visible: bool
    requested_endpoint: str | None
    cross_endpoint: bool
    #: 请求里**显式**指定的 uid（``None`` = 没指定）。
    #:
    #: 与 ``viewing_uid`` 不同：``viewing_uid`` 总是有值（没指定时就是会话本人），
    #: 而管理视图要靠「有没有指定」来决定是「全体摘要」还是「某人的视图」。
    #: 两者合成一个字段会让「管理员查自己」与「管理员不指定」无法区分。
    requested_uid: str | None = None

    @property
    def is_self_view(self) -> bool:
        return self.viewing_uid == self.uid

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "viewing_uid": self.viewing_uid,
            "requested_uid": self.requested_uid,
            "is_self_view": self.is_self_view,
            "roles": list(self.roles),
            "scopes": list(self.scopes),
            "endpoints": list(self.endpoints),
            "admin": self.admin,
            "host_visible": self.host_visible,
            "requested_endpoint": self.requested_endpoint,
            "cross_endpoint": self.cross_endpoint,
        }


def resolve_observer_scope(
    principal: RequesterPrincipal,
    *,
    available_endpoints: Iterable[str],
    requested_uid: str | None = None,
    requested_endpoint: str | None = None,
) -> ObserverScope:
    """校验身份/scope/端点白名单并返回可见范围。失败一律抛 :class:`AccessDenied`。

    判定顺序（顺序本身是安全边界）：

    1. ``principal`` 类型正确、``uid`` 非空；
    2. 持有 :data:`SCOPE_SELF_READ`（任何监控视图的最低门槛）；
    3. ``requested_uid`` 与本会话 ``uid`` 一致，**或**持有 :data:`SCOPE_ADMIN_READ`；
    4. 算可见端点：admin -> 全量；否则 ``authorized_endpoints ∩ available``；
    5. ``requested_endpoint`` 必须落在第 4 步的集合里。

    第 3 步对非 admin 只有**一个**拒绝，不区分「这个 uid 存在但不是你」「这个 uid
    不存在」：一旦区分，端点就变成一个可枚举他人 ``uid`` 存在性的预言机。
    第 5 步同理 —— 不区分「这个端点不存在」与「你不能用这个端点」。
    """
    if not isinstance(principal, RequesterPrincipal):
        raise InvalidRequest("principal must be a RequesterPrincipal")

    uid = (principal.uid or "").strip()
    if not uid:
        raise AccessDenied("no authenticated uid in the BFF session")

    if SCOPE_SELF_READ not in principal.scopes:
        raise AccessDenied(f"missing required scope {SCOPE_SELF_READ}")

    admin = SCOPE_ADMIN_READ in principal.scopes
    host_visible = admin or (SCOPE_HOST_READ in principal.scopes)

    if requested_uid is not None:
        if not isinstance(requested_uid, str):
            raise InvalidRequest("requested_uid must be a string or None")
        candidate = requested_uid.strip()
        if candidate != uid and not admin:
            # 同一个拒绝兜住「别人的 uid」「不存在的 uid」两种成因。
            raise AccessDenied("requested uid is not the session uid")
        viewing_uid = candidate or uid
    else:
        viewing_uid = uid

    available = frozenset(str(endpoint) for endpoint in available_endpoints)
    if admin:
        # 管理视角看全量目录 —— 这就是「Admin 可见 all」。
        visible = available
    else:
        visible = frozenset(principal.authorized_endpoints) & available

    if requested_endpoint is not None:
        if not isinstance(requested_endpoint, str):
            raise InvalidRequest("requested_endpoint must be a string or None")
        candidate = requested_endpoint.strip()
        if candidate not in visible:
            raise AccessDenied("requested endpoint is not in the visible set")
        selected: tuple[str, ...] = (candidate,)
    else:
        selected = tuple(sorted(visible))

    return ObserverScope(
        uid=uid,
        roles=tuple(sorted(principal.roles)),
        scopes=tuple(sorted(principal.scopes)),
        endpoints=selected,
        viewing_uid=viewing_uid,
        admin=admin,
        host_visible=host_visible,
        requested_endpoint=requested_endpoint.strip() if requested_endpoint else None,
        cross_endpoint=len(selected) > 1,
        requested_uid=requested_uid.strip() if requested_uid else None,
    )


def attributed_keys(
    scope: ObserverScope, directory: KeyDirectory, *, include_revoked: bool = False
) -> tuple[str, ...]:
    """本次可见的 ``key_id`` 集合。

    归属**只**来自 :class:`KeyDirectory`：取 ``uid == scope.viewing_uid`` 的 key。
    没有任何请求字段参与。
    """
    if not isinstance(scope, ObserverScope):
        raise InvalidRequest("scope must be an ObserverScope")
    if not isinstance(directory, KeyDirectory):
        raise InvalidRequest("directory must be a KeyDirectory")

    owned = directory.owned_by(scope.viewing_uid)
    if include_revoked:
        return owned
    kept = []
    for key_id in owned:
        binding = directory.lookup(key_id)
        if binding is not None and not binding.revoked:
            kept.append(key_id)
    return tuple(kept)


def endpoint_visibility(principal: RequesterPrincipal, available_endpoints: Iterable[str]) -> frozenset[str]:
    """便捷函数：给一个主体，算出它能看到哪些端点 id（不抛错，不要求 scope）。

    只用于在**组装请求之前**决定「要不要把某个端点放进候选集」这类展示性问题；
    真正的授权判定仍然只有 :func:`resolve_observer_scope` 一处。
    """
    if not isinstance(principal, RequesterPrincipal):
        raise InvalidRequest("principal must be a RequesterPrincipal")
    available = frozenset(str(e) for e in available_endpoints)
    if SCOPE_ADMIN_READ in principal.scopes:
        return available
    return frozenset(principal.authorized_endpoints) & available


def scope_allows_uid(principal: RequesterPrincipal, uid: str) -> bool:
    """``principal`` 能否查看 ``uid`` 的用量（自己，或持管理 scope 的任意人）。"""
    if not isinstance(principal, RequesterPrincipal):
        raise InvalidRequest("principal must be a RequesterPrincipal")
    if SCOPE_ADMIN_READ in principal.scopes:
        return True
    return principal.uid == uid


__all__ = [
    "ALL_SCOPES",
    "GatewayKeyBinding",
    "KeyDirectory",
    "ObserverScope",
    "RequesterPrincipal",
    "SCOPE_ADMIN_READ",
    "SCOPE_HOST_READ",
    "SCOPE_SELF_READ",
    "attributed_keys",
    "endpoint_visibility",
    "resolve_observer_scope",
    "scope_allows_uid",
]
