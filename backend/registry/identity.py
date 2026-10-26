"""受信身份：从**凭据**推出主体，绝不从**请求**推出主体。

与 ``backend.atlas.identity`` 同一套纪律，只是换了一层：

``token -> principal/uid/linux_user/is_admin`` 只能来自服务端可信目录
（:class:`TokenDirectory`，由运维/Portal 在服务启动时注入）。客户端能自报的一切
—— ``X-Owner``、``X-Uid``、``X-Role``、body 里的 ``owner`` / ``uid`` / ``principal``
—— **都不参与授权**，而且这里选择**直接拒绝**而不是静默忽略：一次伪造尝试
应当留下痕迹并让调用方知道自己的客户端错了。

``roles`` 只用于展示与审计，**不产生任何权限**：没有一条规则是「因为 role == admin
所以放行」。管理员的全局只读与可授权管理，来自显式授予的 ``registry:admin:*``
scope（见 :class:`RequesterContext` 与 ``authz.py``）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable, Protocol

from .errors import (
    IdentityAssertionRejected,
    InvalidRequest,
    TokenAuthorityUnavailable,
    Unauthenticated,
)

#: 读取**自己**名下 engine/model/deployment/endpoint 所必需的 scope。
SCOPE_REGISTRY_READ = "registry:read"

#: 登记/修改/删除自己名下资源所必需的 scope。
SCOPE_REGISTRY_WRITE = "registry:write"

#: 共享（share/unshare）自己名下的 Endpoint 所必需的 scope。
#:
#: 刻意与 ``registry:write`` 分开：写自己的元数据与**把服务开放给别人**是两种不同
#: 的授权决定，后者影响的是别人能不能用，不该由前者顺带获得。
SCOPE_ENDPOINT_SHARE = "registry:endpoint:share"

#: 经授权网关调用一个**别人共享出来**的 Endpoint 所必需的 scope。
SCOPE_ENDPOINT_INVOKE = "registry:endpoint:invoke"

#: 全局只读（跨 owner 看全部资源）。管理员 carol 的可授权只读能力。
SCOPE_ADMIN_READ = "registry:admin:read"

#: 可授权管理：代替 owner 授予/收回共享、以及查看全量 ACL 与审计。
#:
#: 与 ``SCOPE_ADMIN_READ`` 分开：能看全局不等于能改全局。两者都**不**因为
#: ``role == admin`` 自动获得。
SCOPE_ADMIN_MANAGE = "registry:admin:manage"

#: 经本机 unix socket（``SO_PEERCRED``）上报**自己**的扫描快照。
#:
#: 只由内核证明的 uid 授予：``backend.registry.agent_channel`` 用对端 uid 在受信
#: 主体目录里查到主体，构造的 ``RequesterContext`` 只带这一条 scope。网页会话的
#: 策略（``backend.bff.scopes``）刻意**不**发它 —— 于是它既申请不到、也声明不了。
#: 它换来的全部能力是「写一条属于自己的报告」：不产生登记、共享，也不带来读别人
#: 数据的权力。
SCOPE_AGENT_REPORT = "registry:agent:report"

#: 本部署认识的全部 scope —— Registry **唯一**的授权词表，不因 introspection 扩容。
#:
#: ``TokenDirectory`` 在构造期拒绝表外的 scope（拼写错误不会静默失效）；
#: ``TrustedTokenIntrospector`` 收到别的命名空间时丢弃、收到本命名空间的未知值
#: 时整条拒绝（见该类 docstring）。两条路径共用这一份词表。
KNOWN_SCOPES = frozenset(
    {
        SCOPE_REGISTRY_READ,
        SCOPE_REGISTRY_WRITE,
        SCOPE_ENDPOINT_SHARE,
        SCOPE_ENDPOINT_INVOKE,
        SCOPE_ADMIN_READ,
        SCOPE_ADMIN_MANAGE,
        SCOPE_AGENT_REPORT,
    }
)

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
    }
)

#: 请求体里**永远不能**承载身份的字段。命中即拒绝整个请求。
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
    }
)

#: 主体在平台内的状态。只有 ``approved`` 的主体才属于 "all-approved-platform-users"。
PRINCIPAL_APPROVED = "approved"
PRINCIPAL_SUSPENDED = "suspended"
PRINCIPAL_STATES = (PRINCIPAL_APPROVED, PRINCIPAL_SUSPENDED)

#: 主体种类。``linux`` 是历史上的「一个主机账户 = 一个主体」；``account`` 是
#: 与 Linux 无关的平台账户（``backend.auth.accounts``，主体 id 为 ``account:<hex>``）。
PRINCIPAL_KIND_LINUX = "linux"
PRINCIPAL_KIND_ACCOUNT = "account"
PRINCIPAL_KINDS = (PRINCIPAL_KIND_LINUX, PRINCIPAL_KIND_ACCOUNT)
ACCOUNT_PRINCIPAL_PREFIX = "account:"

#: 平台账户没有「自己的」uid。它名下的行在 ``owner_uid`` 列里写这个值；服务实际
#: 以哪个 Linux 用户运行，记录在 engine 的 ``ownership.uid`` 里（登记时观测到的）。
UID_UNMAPPED = -1


def is_account_principal(principal: object) -> bool:
    return isinstance(principal, str) and principal.startswith(ACCOUNT_PRINCIPAL_PREFIX)


#: Linux 主体的命名形状（``backend.auth_linux.handoff.linux_user_id`` 的产物）。
_LINUX_PRINCIPAL_PREFIX = "linux:"


def principal_host_id(principal: object) -> str | None:
    """从 ``linux:<host>:<uid>`` 里取出 ``<host>``；不是这个形状就 ``None``。

    代理上报的 ``host_id`` **必须**由服务端从这里推出。客户端自称的主机名不能
    用：主体名里的 ``<host>`` 是部署时 ``--host-id`` 定下的那一份，采信客户端的
    写法，同一台机器就会在库里变成两台（这也是 ``--host-id`` 缺省取真实主机名的
    理由：代理就在本机跑，两边的答案本来就该是同一个）。

    只认最后一段是十进制 uid 的形状；主机名里即便有冒号也仍然解得出来（按最右
    一个冒号切）。
    """

    if not isinstance(principal, str) or not principal.startswith(_LINUX_PRINCIPAL_PREFIX):
        return None
    rest = principal[len(_LINUX_PRINCIPAL_PREFIX):]
    head, separator, tail = rest.rpartition(":")
    if not separator or not head:
        return None
    if not tail.isdigit():
        return None
    return head


@dataclass(frozen=True)
class Principal:
    """一个平台主体的服务端事实：**owner 的最终依据**。

    ``kind == "account"`` 时：``linux_user`` 是账户用户名（展示与路由标签，不是主机
    账户），``uid`` 为 ``None``；``run_as`` 若非空，则这个账户只能登记以该 Linux
    用户运行的服务（管理员设定的可选限制）。
    """

    principal: str
    linux_user: str
    uid: int | None
    state: str = PRINCIPAL_APPROVED
    display_name: str | None = None
    kind: str = PRINCIPAL_KIND_LINUX
    run_as: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise InvalidRequest("principal must be a non-empty string")
        if not isinstance(self.linux_user, str) or not self.linux_user.strip():
            raise InvalidRequest("linux_user must be a non-empty string")
        if self.kind not in PRINCIPAL_KINDS:
            raise InvalidRequest(f"unknown principal kind: {self.kind!r}")
        if self.kind == PRINCIPAL_KIND_ACCOUNT:
            if not is_account_principal(self.principal):
                raise InvalidRequest("account principals must be named account:<id>")
            if self.uid is not None:
                raise InvalidRequest("account principals carry no uid")
        else:
            if is_account_principal(self.principal):
                raise InvalidRequest("account:<id> principals must be of kind 'account'")
            if self.run_as is not None:
                raise InvalidRequest("run_as only applies to account principals")
            if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
                raise InvalidRequest("uid must be a non-negative integer")
        if self.state not in PRINCIPAL_STATES:
            raise InvalidRequest(f"unknown principal state: {self.state!r}")

    @property
    def is_account(self) -> bool:
        return self.kind == PRINCIPAL_KIND_ACCOUNT

    @property
    def ownership_user(self) -> str | None:
        """所有权校验要比对的 Linux 用户名；``None`` 表示「只观测、不比对」。"""
        return self.run_as if self.is_account else self.linux_user

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
            "kind": self.kind,
            "run_as": self.run_as,
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
            if not binding.token:
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
            if principal.uid is not None and principal.uid in seen_uids:
                # 一个 UID 只能属于一个主体：否则「owner 是谁」会有两个答案。
                raise InvalidRequest(
                    f"uid {principal.uid} already bound to {seen_uids[principal.uid]!r}"
                )
            by_principal[principal.principal] = principal
            if principal.uid is not None:
                seen_uids[principal.uid] = principal.principal
        self._by_principal = by_principal
        self._by_uid = dict(seen_uids)
        self._approved = frozenset(
            p.principal for p in by_principal.values() if p.approved
        )

    def lookup(self, principal: str) -> Principal | None:
        return self._by_principal.get(principal)

    def by_uid(self, uid: int) -> Principal | None:
        """``uid -> Principal``。服务自动接入用它把监听端口归到账户（§4.1）。

        只有目录里**显式绑定**了 uid 的主体才会命中；``account:<id>`` 这类不带
        uid 的账户永远查不到——它们没有本机进程可归属。
        """

        principal = self._by_uid.get(uid)
        return self._by_principal.get(principal) if principal else None

    def uids(self) -> frozenset[int]:
        """目录里所有被绑定的 uid。自动接入拿它决定哪些端口值得探测。"""

        return frozenset(self._by_uid)

    def require(self, principal: str) -> Principal:
        found = self.lookup(principal)
        if found is None:
            raise Unauthenticated(f"principal {principal!r} is not in the trusted directory")
        return found

    def account_for_linux_user(self, linux_user: str) -> Principal | None:
        """这个 Linux 用户对应的**平台账户**主体，没有就 ``None``。

        静态目录只装 Linux 主体，账户由 :class:`AccountAwareDirectory` 接进来；
        所以基类永远回答「没有」——它没有别的答案可给，而不是「查不到就叫账户」。
        """

        return None

    def approved_principals(self) -> frozenset[str]:
        """``all-approved-platform-users`` 的**唯一**成员来源。"""
        return self._approved

    def __len__(self) -> int:
        return len(self._by_principal)


@dataclass(frozen=True)
class RequesterContext:
    """一次请求的受信身份。**只能**由凭据 + 受信目录构造。"""

    principal: str
    uid: int | None
    linux_user: str
    scopes: frozenset[str]
    roles: frozenset[str] = frozenset()
    token_label: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @property
    def is_admin_read(self) -> bool:
        return SCOPE_ADMIN_READ in self.scopes

    @property
    def is_admin_manage(self) -> bool:
        return SCOPE_ADMIN_MANAGE in self.scopes

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


class ScopedTokenIntrospector(Protocol):
    """一个**服务端**权威，回答「这条 bearer 凭据是谁」。

    ``backend/auth/tokens.ScopedTokenService`` 已经实现了它。Registry 从不自己
    解析 token：没有 JWT 解码、没有自签校验、没有第二条查表路径。它只把这个
    问题转给签发方，然后校验**回答**。
    """

    def introspect(self, raw_token: object) -> object:  # pragma: no cover - protocol
        ...


#: scope 的**形状**规则。
#:
#: 与签发方 ``backend/auth/tokens._SCOPE_RE`` 逐字相同。这里刻意**不 import**
#: ``backend.auth``：Registry 不依赖 IAM 包，而一个比签发方更窄的形状规则会
#: 重新制造本函数要修的那个缺陷（合法 token 被整条拒）。两份规则的一致性由
#: ``tests/registry/test_iam_scope_vocabulary.py`` 用同一份语料对照钉住。
_SCOPE_SHAPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}(?::[a-z0-9_]{1,32}){1,4}$")


def _valid_scope_shape(scope: object) -> bool:
    """这条 scope 的**形状**是否合法（与它属于哪个 namespace 无关）。"""
    return isinstance(scope, str) and bool(_SCOPE_SHAPE_RE.match(scope))


def _scope_namespace(scope: str) -> str:
    return scope.split(":", 1)[0]


@dataclass(frozen=True)
class _IntrospectionAnswer:
    """一次被本层**校验过**的 introspect 回答。

    ``scopes`` 只含**本部署认识的 registry scope**：外模块的合法 scope 在校验时
    被丢弃，因为它们不产生任何 Registry 权限。保留它们会让 ``RequesterContext``
    看起来像持有本模块不理解的授权，也让审计日志里出现一堆与 Registry 无关的词。
    """

    principal: str
    uid: int
    linux_user: str
    scopes: frozenset[str]
    roles: frozenset[str]
    label: str | None
    token_id: str | None
    expires_at: float | None


def _string_set(raw: object) -> frozenset[str] | None:
    if isinstance(raw, (str, bytes)) or raw is None:
        return None
    try:
        values = frozenset(raw)  # type: ignore[arg-type]
    except TypeError:
        return None
    if not all(isinstance(item, str) for item in values):
        return None
    return values


def _answer_mapping(answer: object) -> dict | None:
    """把 IAM 的回答读成字段映射。

    优先用 ``to_registry_dict()``（IAM 已经把它声明为「registry 解析所依据的形状」），
    否则读同名字段。两种形状都要经过同一套校验，所以这里只负责取字段。
    """
    to_mapping = getattr(answer, "to_registry_dict", None)
    if callable(to_mapping):
        try:
            mapping = to_mapping()
        except Exception:  # noqa: BLE001 - 形状不对就是「不是一条可用的回答」
            return None
        return mapping if isinstance(mapping, dict) else None
    if not hasattr(answer, "principal"):
        return None
    return {
        name: getattr(answer, name, None)
        for name in (
            "principal",
            "uid",
            "linux_user",
            "state",
            "approved",
            "scopes",
            "roles",
            "label",
            "token_id",
            "expires_at",
        )
    }


class TrustedTokenIntrospector:
    """把 IAM 的受信 introspector 接成 Registry 的 token 权威。

    **接缝为什么长这样。** Registry 的 ``TokenDirectory`` 是一个内存等值表；
    IAM 的 ``ScopedTokenService`` 是另一个服务，有自己的存储、到期、撤销与主体
    状态。把 IAM token 复制进第二个 ``TokenDirectory`` 能接上，但那是**第二套
    token 账本**：撤销/到期/停用必须在两边各同步一次，而漏掉任何一次都表现为
    「已撤销的凭据仍然有效」。所以这里不复制数据，只**问**签发方。

    **它不接受任意对象。** 包装的对象是启动时由部署注入的、实现了
    ``introspect(token)`` 的服务端对象；``resolve_requester`` 只接受本类实例，
    而不是任何有同名方法的鸭子。客户端提供的任何东西都不会走到这里。

    **它只读回答，不读 token。** 一个长得像 JSON claims 的字符串对这条路径而言
    只是一个未知凭据 —— 「用一个字符串给自己签发管理员」在这条路径上不可表示。

    校验面（任何一项不满足都返回 ``None``，由调用方转成 401）：

    * ``active`` 必须为真；
    * ``principal`` / ``linux_user`` 是非空字符串，``uid`` 是非负整数（拒绝 bool）；
    * 主体状态必须是 approved；
    * ``scopes`` / ``roles`` 是字符串集合，且每条 scope 的形状合法；
    * scope 按 **namespace** 分流：``registry:`` 命名空间里本部署不认识的 scope
      让整条 token 失败关闭（不猜、也不扩权）；其它命名空间的合法 scope
      （``atlas:`` / ``observability:`` / ``portal:`` …）**丢弃但保留 token** ——
      Registry 只消费自己的六个，外模块的 scope 本来就不产生 Registry 权限；
    * 若回答带 ``expires_at``，按本层的时钟再判一次（签发方已经判过；这是纵深防御）。

    如果签发方**无法回答**（抛异常），它抛 :class:`TokenAuthorityUnavailable`
    （503），而不是把「权威离线」伪装成「你的凭据不对」。
    """

    name = "trusted_token_introspector"

    def __init__(
        self,
        introspector: object,
        *,
        principals: PrincipalDirectory,
        clock=time.time,
    ) -> None:
        if not callable(getattr(introspector, "introspect", None)):
            raise InvalidRequest("an introspector must implement introspect(token)")
        if not isinstance(principals, PrincipalDirectory):
            raise InvalidRequest("principals must be a PrincipalDirectory")
        if not callable(clock):
            raise InvalidRequest("clock must be callable")
        self._introspector = introspector
        self._principals = principals
        self._clock = clock

    def introspect(self, raw_token: object) -> _IntrospectionAnswer | None:
        try:
            answer = self._introspector.introspect(raw_token)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - 权威不可用是 503，不是 401
            raise TokenAuthorityUnavailable(
                f"the configured token authority could not answer ({type(exc).__name__})"
            ) from exc
        return self._validate(answer)

    def _validate(self, answer: object) -> _IntrospectionAnswer | None:
        if answer is None or getattr(answer, "active", None) is not True:
            return None
        mapping = _answer_mapping(answer)
        if mapping is None:
            return None

        principal = mapping.get("principal")
        uid = mapping.get("uid")
        linux_user = mapping.get("linux_user")
        if not isinstance(principal, str) or not principal.strip():
            return None
        if is_account_principal(principal):
            # 平台账户没有 uid：回答里必须**没有**，有了反而说明签发方搞混了主体。
            if uid is not None:
                return None
        elif not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
            return None
        if not isinstance(linux_user, str) or not linux_user.strip():
            return None

        state = mapping.get("state")
        if state is not None and state != PRINCIPAL_APPROVED:
            return None
        if mapping.get("approved") is False:
            return None

        scopes = _string_set(mapping.get("scopes"))
        roles = _string_set(mapping.get("roles"))
        if scopes is None or roles is None:
            return None

        # 形状先判：一条畸形字符串不可能是任何模块的合法 scope。
        if not all(_valid_scope_shape(scope) for scope in scopes):
            return None

        # Registry 只消费**自己的**六个 scope。IAM 允许合法的多 namespace，
        # 一条真 token 会同时带 atlas/observability/portal 的 scope；因为
        # 这些词不在 KNOWN_SCOPES 里就把**整条 token** 拒掉，是把「Registry
        # 不认识它」误判成「这个凭据无效」。外模块的 scope 一律**丢弃**：
        # 它们不产生任何 Registry 权限，丢掉只会收窄。
        registry_scopes = frozenset(scope for scope in scopes if scope in KNOWN_SCOPES)

        # 但**我们自己命名空间里**不认识的 scope 是另一回事：那是一个本部署
        # 无法解释的 registry 权限（拼写错误、或一份更新的词表）。丢弃它会让
        # 一次权限漂移变成一次静默降级；接受它则等于凭空扩权。整条 token
        # 失败关闭 —— 不猜它是什么意思，也不扩容授权词表。
        unknown_registry = {
            scope
            for scope in scopes
            if _scope_namespace(scope) == "registry" and scope not in KNOWN_SCOPES
        }
        if unknown_registry:
            return None

        expires_at = mapping.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
                return None
            if float(self._clock()) >= float(expires_at):
                return None

        label = mapping.get("label")
        if label is not None and not isinstance(label, str):
            return None
        token_id = mapping.get("token_id")
        if token_id is not None and not isinstance(token_id, str):
            return None

        return _IntrospectionAnswer(
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            scopes=registry_scopes,
            roles=roles,
            label=label,
            token_id=token_id,
            expires_at=float(expires_at) if expires_at is not None else None,
        )


def resolve_requester(
    token: object,
    tokens: TokenDirectory | TrustedTokenIntrospector,
    principals: PrincipalDirectory,
) -> RequesterContext:
    """凭据 -> :class:`RequesterContext`。任何缺口都抛 :class:`Unauthenticated`。

    这里**没有**「拿不到就当成匿名/管理员」的分支：目录里没有就是没有。

    两种权威：

    * :class:`TokenDirectory` —— 既有的内存等值表路径，逐字未变；
    * :class:`TrustedTokenIntrospector` —— 由签发方回答，本层校验回答。

    其它任何对象都抛 :class:`TokenAuthorityUnavailable`：把「部署装配错了」伪装成
    「这条凭据不对」，会让运维去查一个不存在的登录问题。
    """
    if not isinstance(principals, PrincipalDirectory):
        raise InvalidRequest("principals must be a PrincipalDirectory")

    if isinstance(tokens, TokenDirectory):
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
            uid=principal.uid if principal.uid is not None else UID_UNMAPPED,
            linux_user=principal.linux_user,
            scopes=frozenset(binding.scopes),
            roles=frozenset(binding.roles),
            token_label=binding.label,
        )

    if isinstance(tokens, TrustedTokenIntrospector):
        answer = tokens.introspect(token)
        if answer is None:
            raise Unauthenticated("unknown or missing credential")
        # 与目录路径**同一套**主体判定：一个 uid 只能属于一个主体，两条通道
        # 对「这个 uid 是谁」不可能给出两个答案。
        principal = principals.lookup(answer.principal)
        if principal is None:
            raise Unauthenticated("credential resolves to an unknown principal")
        if not principal.approved:
            raise Unauthenticated("principal is not approved")
        if principal.uid != answer.uid:
            raise Unauthenticated("credential uid does not match the trusted directory")
        if principal.linux_user != answer.linux_user:
            raise Unauthenticated(
                "credential linux user does not match the trusted directory"
            )
        return RequesterContext(
            principal=principal.principal,
            uid=principal.uid if principal.uid is not None else UID_UNMAPPED,
            linux_user=principal.linux_user,
            scopes=answer.scopes,
            roles=answer.roles,
            token_label=answer.label,
        )

    raise TokenAuthorityUnavailable(
        "tokens must be a TokenDirectory or a TrustedTokenIntrospector; "
        "a raw issuer object is not an authority this layer can trust"
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

    只接受 ``Bearer <token>``。返回 ``None`` 意味着「没有可用凭据」，
    由调用方转成 401 —— 这里不抛异常，是为了不把「缺头」与「头畸形」
    变成两个可区分的响应。
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


#: 凭据形态与角色的**展示用**缺省值。刻意留空：本模块不为任何主体预设权限。
EMPTY_SCOPES: frozenset[str] = frozenset()
