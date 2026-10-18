"""受信身份：从**凭据**推出主体，绝不从**请求**推出主体。

与 ``backend.registry.identity`` / ``backend.atlas.identity`` 同一套纪律：

``token -> principal/uid/linux_user/scopes`` 只能来自服务端可信目录
（:class:`TokenDirectory` + :class:`PrincipalDirectory`，由运维/Portal 在服务启动时注入）。
客户端能自报的一切 —— ``X-Owner``、``X-Uid``、``X-Role``、body 里的 ``owner`` /
``uid`` / ``role`` / ``is_admin`` —— **都不参与授权**，而且这里选择**直接拒绝**而不是
静默忽略：一次伪造尝试应当留下痕迹并让调用方知道自己的客户端错了。

``roles`` 只用于展示与审计，**不产生任何权限**：没有一条规则是「因为 role == admin
所以放行」。全局可见来自显式授予的 ``evaluation:admin:read``；代替他人操作来自
``evaluation:admin:manage``。二者都不因 ``role`` 自动获得。

为什么本包自带一份而不是 import ``backend.registry.identity``
------------------------------------------------------------
本包的上游基线是 ``main``（一个只有文档与原型的干净基线），此刻 ``backend/registry``
并不存在于本分支。跨包的 import 会让「registry 还没合并进来」变成「评测中心无法启动」，
而我们真正需要的只是**同一个契约的形状**。因此这里照抄那份契约——同样的字段、同样的
拒绝语义、同样的无兜底立场——并在 :meth:`RequesterContext.from_foreign` 提供一个鸭子类型
适配器：等 BFF 把两层接起来时，``backend.registry`` 的 ``RequesterContext`` 可以原样传进来，
不需要任何转换层。这条缝在 ``docs/evalscope-center.md`` 里写明为**待合并项**。

scope 命名空间
--------------
本包用 ``evaluation:*``。``portal:evaluation:native``（由 ``portal-modules`` 契约定义，
含义是「让我进 EvalScope 原生界面」）**不是**本包的 scope，本包既不消费也不产出它：
「能不能读我的评测任务」与「能不能进原生 UI」是两次不同的授权决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import IdentityAssertionRejected, InvalidRequest, Unauthenticated

# ---------------------------------------------------------------------------
# scope：唯一的授权依据（role 不产生权限）
# ---------------------------------------------------------------------------

#: 读取**自己**名下的评测任务与结果所必需。
SCOPE_EVAL_READ = "evaluation:read"

#: 提交评测任务所必需。
SCOPE_EVAL_SUBMIT = "evaluation:submit"

#: 取消**自己**名下的评测任务所必需。
SCOPE_EVAL_CANCEL = "evaluation:cancel"

#: 重试**自己**名下的评测任务所必需。
SCOPE_EVAL_RETRY = "evaluation:retry"

#: 读取**自己**任务的报告引用所必需。
#:
#: 刻意与 ``evaluation:read`` 分开：知道「有一个结果」与「拿到指向报告文件的引用」
#: 是两次不同的决定，后者影响的是一个文件路径。
SCOPE_EVAL_REPORT_READ = "evaluation:report:read"

#: 全局只读：跨 owner / 跨项目看全部评测任务与结果。carol 的可授权只读能力。
SCOPE_EVAL_ADMIN_READ = "evaluation:admin:read"

#: 可授权管理：代替 owner 取消/重试任何任务、开关真实派发。
#:
#: 与 ``SCOPE_EVAL_ADMIN_READ`` 分开：能看全局不等于能改全局。两者都**不**因为
#: ``role == admin`` 自动获得。
SCOPE_EVAL_ADMIN_MANAGE = "evaluation:admin:manage"

#: 允许把任务派发到**真实** runner。
#:
#: 单独一个 scope，且**没有**任何一条规则能从别的 scope 推出它：真实派发会占用 GPU，
#: 是一个比「提交一个排队任务」重得多的决定。本部署不授予任何主体这个 scope——GPU 已满。
SCOPE_EVAL_DISPATCH_REAL = "evaluation:dispatch:real"

#: 本部署认识的全部 scope。拒绝未知 scope 进入受信目录，避免拼写错误静默失效。
KNOWN_SCOPES = frozenset(
    {
        SCOPE_EVAL_READ,
        SCOPE_EVAL_SUBMIT,
        SCOPE_EVAL_CANCEL,
        SCOPE_EVAL_RETRY,
        SCOPE_EVAL_REPORT_READ,
        SCOPE_EVAL_ADMIN_READ,
        SCOPE_EVAL_ADMIN_MANAGE,
        SCOPE_EVAL_DISPATCH_REAL,
    }
)

#: 展示用角色名。与 ``backend.auth_linux.policy`` 的取值保持一致，但**不参与判定**。
ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_MEMBER = "member"

#: 主体在平台内的状态。只有 ``approved`` 的主体属于 "all-approved-platform-users"。
PRINCIPAL_APPROVED = "approved"
PRINCIPAL_SUSPENDED = "suspended"
PRINCIPAL_STATES = (PRINCIPAL_APPROVED, PRINCIPAL_SUSPENDED)

#: 共享可见性的取值域。``all_approved`` 是文案里的 "all-approved-platform-users"。
VISIBILITY_PRIVATE = "private"
VISIBILITY_USERS = "users"
VISIBILITY_ALL_APPROVED = "all_approved"
VISIBILITY_MODES = (VISIBILITY_PRIVATE, VISIBILITY_USERS, VISIBILITY_ALL_APPROVED)

# ---------------------------------------------------------------------------
# 自报身份：命中即拒绝整个请求
# ---------------------------------------------------------------------------

#: 请求里**永远不能**承载身份的头。命中即拒绝整个请求。
FORBIDDEN_IDENTITY_HEADERS = frozenset(
    {
        "x-owner",
        "x-owner-principal",
        "x-principal",
        "x-subject",
        "x-uid",
        "x-user-id",
        "x-username",
        "x-linux-user",
        "x-role",
        "x-roles",
        "x-forwarded-user",
        "x-remote-user",
        "x-auth-request-user",
        "x-admin",
        "x-is-admin",
        "x-project",
        "x-project-id",
    }
)

#: 请求体里**永远不能**承载身份的字段。命中即拒绝整个请求。
#:
#: ``project`` 在列表里是刻意的：项目归属决定可见范围，因此它是一个**授权输入**，
#: 只能由服务端主体目录决定，不能由调用者在 body 里挑一个。
FORBIDDEN_IDENTITY_FIELDS = frozenset(
    {
        "owner",
        "owner_principal",
        "owner_uid",
        "uid",
        "user_id",
        "principal",
        "subject",
        "username",
        "linux_user",
        "role",
        "roles",
        "is_admin",
        "admin",
        "project",
        "project_id",
        "scopes",
    }
)

#: 凭据形态与角色的**展示用**缺省值。刻意留空：本模块不为任何主体预设权限。
EMPTY_SCOPES: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# 受信目录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """一个平台主体的服务端事实：**owner 的最终依据**。"""

    principal: str
    linux_user: str
    uid: int
    state: str = PRINCIPAL_APPROVED
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise InvalidRequest("principal must be a non-empty string")
        if not isinstance(self.linux_user, str) or not self.linux_user.strip():
            raise InvalidRequest("linux_user must be a non-empty string")
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise InvalidRequest("uid must be a non-negative integer")
        if self.state not in PRINCIPAL_STATES:
            raise InvalidRequest(f"unknown principal state: {self.state!r}")

    @property
    def approved(self) -> bool:
        return self.state == PRINCIPAL_APPROVED

    def to_dict(self) -> dict:
        return {
            "principal": self.principal,
            "linux_user": self.linux_user,
            "uid": self.uid,
            "state": self.state,
            "approved": self.approved,
            "display_name": self.display_name,
        }


@dataclass(frozen=True)
class TokenBinding:
    """一条受信的 ``token -> principal + scopes`` 绑定。

    ``token`` 本身是**不透明**的：本模块从不解析它（没有 JWT 解码、没有自签校验），
    只做等值查找。凭证的形状是 Portal 的事，这里只认「目录里有这一条」。
    """

    token: str
    principal: str
    scopes: frozenset[str]
    roles: frozenset[str] = frozenset()
    revoked: bool = False
    label: str | None = None


class TokenDirectory:
    """服务端可信 token 目录（纯内存、无 IO、无解析）。

    目录之外的 token 一律解析不出主体 —— 没有兜底、没有匿名缺省。
    """

    def __init__(self, bindings: Iterable[TokenBinding]) -> None:
        by_token: dict[str, TokenBinding] = {}
        for binding in bindings:
            if not isinstance(binding, TokenBinding):
                raise InvalidRequest("directory entries must be TokenBinding")
            if not isinstance(binding.token, str) or not binding.token:
                raise InvalidRequest("binding token must not be empty")
            if binding.token in by_token:
                raise InvalidRequest("duplicate token in directory")
            unknown = set(binding.scopes) - KNOWN_SCOPES
            if unknown:
                raise InvalidRequest(f"binding carries unknown scopes: {sorted(unknown)}")
            by_token[binding.token] = binding
        self._by_token = by_token

    def resolve(self, token: object) -> TokenBinding | None:
        """命中返回绑定；未知 token 返回 ``None``（由调用方转成 401）。"""
        if not isinstance(token, str) or not token:
            return None
        return self._by_token.get(token)

    def __len__(self) -> int:
        return len(self._by_token)


class PrincipalDirectory:
    """服务端可信主体目录：``principal -> Principal``（含 UID 与审批状态）。"""

    def __init__(self, principals: Iterable[Principal]) -> None:
        by_principal: dict[str, Principal] = {}
        seen_uids: dict[int, str] = {}
        for principal in principals:
            if not isinstance(principal, Principal):
                raise InvalidRequest("directory entries must be Principal")
            if principal.principal in by_principal:
                raise InvalidRequest(f"duplicate principal: {principal.principal!r}")
            if principal.uid in seen_uids:
                # 一个 UID 只能属于一个主体：否则「owner 是谁」会有两个答案。
                raise InvalidRequest(
                    f"uid {principal.uid} already bound to {seen_uids[principal.uid]!r}"
                )
            by_principal[principal.principal] = principal
            seen_uids[principal.uid] = principal.principal
        self._by_principal = by_principal
        self._approved = frozenset(p.principal for p in by_principal.values() if p.approved)

    def lookup(self, principal: str) -> Principal | None:
        return self._by_principal.get(principal)

    def require(self, principal: str) -> Principal:
        found = self.lookup(principal)
        if found is None:
            raise Unauthenticated(f"principal {principal!r} is not in the trusted directory")
        return found

    def approved_principals(self) -> frozenset[str]:
        """``all_approved`` 可见性的**唯一**成员来源。"""
        return self._approved

    def __len__(self) -> int:
        return len(self._by_principal)


@dataclass(frozen=True)
class RequesterContext:
    """一次请求的受信身份。**只能**由凭据 + 受信目录构造。"""

    principal: str
    uid: int
    linux_user: str
    scopes: frozenset[str]
    roles: frozenset[str] = frozenset()
    token_label: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_all(self, *scopes: str) -> bool:
        return all(scope in self.scopes for scope in scopes)

    @property
    def is_admin_read(self) -> bool:
        return SCOPE_EVAL_ADMIN_READ in self.scopes

    @property
    def is_admin_manage(self) -> bool:
        return SCOPE_EVAL_ADMIN_MANAGE in self.scopes

    def to_dict(self) -> dict:
        return {
            "principal": self.principal,
            "uid": self.uid,
            "linux_user": self.linux_user,
            "scopes": sorted(self.scopes),
            "roles": sorted(self.roles),
            "token_label": self.token_label,
            "is_admin_read": self.is_admin_read,
            "is_admin_manage": self.is_admin_manage,
        }

    @classmethod
    def from_foreign(cls, obj: object) -> "RequesterContext":
        """把**另一个契约**的受信上下文鸭子类型地接进来。

        只读属性、只接受已知形状：``backend.registry`` 的 ``RequesterContext`` 有
        ``principal`` / ``uid`` / ``linux_user`` / ``scopes`` / ``roles``，可以直接传入。
        缺任何一个必需属性就抛 :class:`InvalidRequest` —— 「拿不到就当成匿名/管理员」
        这种兜底在这里不存在。
        """
        if isinstance(obj, cls):
            return obj

        def _need(name: str) -> object:
            if not hasattr(obj, name):
                raise InvalidRequest(f"foreign requester has no {name!r}")
            return getattr(obj, name)

        principal = _need("principal")
        uid = _need("uid")
        linux_user = _need("linux_user")
        try:
            scopes = frozenset(_need("scopes"))  # type: ignore[arg-type]
            roles = frozenset(getattr(obj, "roles", ()) or ())
        except TypeError as exc:
            raise InvalidRequest(f"foreign requester scopes/roles are not iterable: {exc}") from None
        return cls(
            principal=principal,  # type: ignore[arg-type]
            uid=uid,  # type: ignore[arg-type]
            linux_user=linux_user,  # type: ignore[arg-type]
            scopes=scopes,
            roles=roles,
            token_label=getattr(obj, "token_label", None),
        )


def resolve_requester(
    token: object,
    tokens: TokenDirectory,
    principals: PrincipalDirectory,
) -> RequesterContext:
    """凭据 -> :class:`RequesterContext`。任何缺口都抛 :class:`Unauthenticated`。

    这里**没有**「拿不到就当成匿名/管理员」的分支：目录里没有就是没有。
    """
    if not isinstance(tokens, TokenDirectory):
        raise InvalidRequest("tokens must be a TokenDirectory")
    if not isinstance(principals, PrincipalDirectory):
        raise InvalidRequest("principals must be a PrincipalDirectory")

    binding = tokens.resolve(token)
    if binding is None:
        raise Unauthenticated("unknown or missing credential")
    if binding.revoked:
        raise Unauthenticated("credential has been revoked")

    principal = principals.lookup(binding.principal)
    if principal is None:
        raise Unauthenticated("credential resolves to an unknown principal")
    if not principal.approved:
        raise Unauthenticated("principal is not approved")

    return RequesterContext(
        principal=principal.principal,
        uid=principal.uid,
        linux_user=principal.linux_user,
        scopes=frozenset(binding.scopes),
        roles=frozenset(binding.roles),
        token_label=binding.label,
    )


def reject_identity_assertions(header_names: Iterable[str], body: object) -> None:
    """请求里若出现任何自报身份的痕迹，抛 :class:`IdentityAssertionRejected`。

    ``header_names`` 由传输层给出（不区分大小写地比对）；``body`` 是已解析的 JSON
    （dict 才检查，其余交给字段级校验）。
    """
    lowered = {str(name).strip().lower() for name in header_names}
    offending_headers = sorted(lowered & FORBIDDEN_IDENTITY_HEADERS)
    if offending_headers:
        raise IdentityAssertionRejected(
            "identity must not be asserted by the client; "
            f"remove header(s): {', '.join(offending_headers)}"
        )

    if isinstance(body, dict):
        offending_fields = sorted(set(body.keys()) & FORBIDDEN_IDENTITY_FIELDS)
        if offending_fields:
            raise IdentityAssertionRejected(
                "identity must not be asserted by the client; "
                f"remove field(s): {', '.join(offending_fields)}"
            )


def extract_bearer_token(authorization: object) -> str | None:
    """从 ``Authorization`` 头取出不透明 token；形状不对返回 ``None``。

    只接受 ``Bearer <token>``。返回 ``None`` 意味着「没有可用凭据」，由调用方转成
    401 —— 这里不抛异常，是为了不把「缺头」与「头畸形」变成两个可区分的响应。
    """
    if not isinstance(authorization, str):
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts[0], parts[1].strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


__all__ = [
    "EMPTY_SCOPES",
    "FORBIDDEN_IDENTITY_FIELDS",
    "FORBIDDEN_IDENTITY_HEADERS",
    "KNOWN_SCOPES",
    "PRINCIPAL_APPROVED",
    "PRINCIPAL_STATES",
    "PRINCIPAL_SUSPENDED",
    "ROLE_MEMBER",
    "ROLE_PLATFORM_ADMIN",
    "SCOPE_EVAL_ADMIN_MANAGE",
    "SCOPE_EVAL_ADMIN_READ",
    "SCOPE_EVAL_CANCEL",
    "SCOPE_EVAL_DISPATCH_REAL",
    "SCOPE_EVAL_READ",
    "SCOPE_EVAL_REPORT_READ",
    "SCOPE_EVAL_RETRY",
    "SCOPE_EVAL_SUBMIT",
    "VISIBILITY_ALL_APPROVED",
    "VISIBILITY_MODES",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_USERS",
    "Principal",
    "PrincipalDirectory",
    "RequesterContext",
    "TokenBinding",
    "TokenDirectory",
    "extract_bearer_token",
    "reject_identity_assertions",
    "resolve_requester",
]
