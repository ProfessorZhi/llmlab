"""受信会话 ⇒ Registry 主体。**这是本包唯一一处决定「你是谁」。**

三条硬规则，每一条都有对应的测试：

1. **身份只来自服务端会话记录。** 输入是一个 session cookie 值，输出由
   ``backend.auth`` 的会话表 + Linux 账号目录（``pwd``）决定。请求头与请求体
   里不接受任何身份声明：``X-UID`` / ``X-Admin`` / ``X-Owner`` 之类**抛错**，
   而不是「读到了但不用」——默默忽略意味着中间件、日志或某个将来的人可以把它
   重新捡起来用；拒绝才能让这条边界留在代码里。
2. **uid 来自账号目录，不来自用户 id 字符串。** ``external_id`` 里那个
   ``host:uid`` 只是索引键；真正用来填 ``RequesterContext.uid`` 与
   ``linux_user`` 的是 ``PwdSource.by_uid`` 的查询结果。目录里没有这个 uid，
   就没有主体——而不是「先信了再说」。
3. **只有 Linux(PAM) 会话能变成主体。** 基线里的 Feishu / local-admin 通道
   仍在，但它们**不**产出 Registry 主体：前者没有 uid，后者是演示用的本地
   管理员。这不是能力缺失，是边界——``reason`` 会说 ``linux_auth_not_enabled``。
4. **管理员能力来自提权，不来自用户表。** 主体解析出来的一律是 ``member``，
   除非那条会话**此刻**处于管理员模式（``backend.bff.admin_mode``，TTL ≤ 15 分钟）。
   因此"没有登录、或不是 Linux 登录的人不能提权"在这里就已经成立：拿不到主体就
   没有会话可提；Feishu 与 local-admin 会话拿不到主体，也就提不了权。第二个身份
   来源（演示用的 local admin、以及将来任何非 PAM 通道）因此不会成为绕过点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

from ..auth import errors as auth_errors
from ..auth import store as auth_store
from ..auth_linux import handoff as linux_handoff
from ..registry.identity import (
    FORBIDDEN_IDENTITY_FIELDS,
    FORBIDDEN_IDENTITY_HEADERS,
    RequesterContext,
)
from . import scopes as bff_scopes

#: 拒绝自报身份的响应码。Registry 用的是 400 ``registry.identity_assertion_rejected``；
#: 这里刻意保持一致，让两条入口对同一类攻击给出同一个答案。
REASON_IDENTITY_ASSERTED = "identity_assertion_rejected"

#: 会话有效，但不是 Linux(PAM) 签发的。
REASON_NOT_LINUX = "linux_auth_not_enabled"

#: 请求试图自己声明数据来源（``?source=`` / ``X-Source``）。
REASON_SOURCE_ASSERTED = "source_assertion_rejected"

#: 请求试图自己声明**去哪取数**（上游 URL / PromQL / 文件路径 / 命令）。
REASON_PROXY_ASSERTED = "proxy_assertion_rejected"

#: 请求同时带了 session cookie 与 ``Authorization``：两条凭据通道都在场。
REASON_CREDENTIAL_CONFUSION = "credential_confusion_rejected"

#: 本部署没有启用 scoped CLI token 通道。默认如此。
REASON_TOKEN_AUTH_NOT_ENABLED = "token_auth_not_enabled"

#: 查询参数里**永远不能**承载数据来源的名字。前端页面 URL 上的 ``?source=`` 是
#: **页面**的取数模式；它不该出现在 API 调用里，出现了就是一次断言。
SOURCE_QUERY_KEY = "source"

#: 请求里**永远不能**承载数据来源的头。命中即拒绝整个请求。
FORBIDDEN_SOURCE_HEADERS = frozenset(
    {
        "x-source",
        "x-data-source",
        "x-live-data",
        "x-synthetic",
    }
)

#: 请求里**永远不能**承载的**自选目标 / 自选主体**参数名。
#:
#: 与 ``FORBIDDEN_SOURCE_HEADERS`` 同一类，但针对的是另一件事：来源说的是
#: 「数据是谁给的」，这一组说的是「**去哪取、以谁的身份取**」。两件事的共同点是
#: 把服务端**已经决定**的东西交回给客户端挑 —— BFF 的每一条数据路由都有固定的
#: 来源（注入的适配器、固定的 key 目录、注入的投影读取器）和固定的主体（会话），
#: 一个能收 ``?url=`` 的处理器就是一个可以从浏览器打到内网的跳板；一个能收
#: ``?owner=`` 的处理器就是一个越权读数的入口。
#:
#: 下面是 Issue #79 逐条点名的几类（PromQL、上游 URL、命令、文件路径、owner/UID），
#: 再加上它们各自的常见拼法。命中即 400，**不是**「读了不用」—— 静默忽略意味着
#: 下游某一层可以再把它捡起来用，而那条路径一旦存在，边界就不在代码里了。
#:
#: 刻意**不含**路由真正用到的那些参数（``visibility`` / ``scope`` / ``outcome`` /
#: ``model`` / ``limit`` / ``before_ts`` / ``before_call_id`` / ``include_unowned``）：
#: 把合法参数一起拒掉，只会逼着下一个人把整条守卫拆掉。
FORBIDDEN_PROXY_QUERY_KEYS = frozenset(
    {
        # 上游地址与目标
        "url",
        "uri",
        "upstream",
        "target",
        "target_url",
        "base_url",
        "endpoint",
        "host",
        "port",
        # 任意查询表达式
        "promql",
        "query",
        "expr",
        # 任意命令 / 文件 / 日志读取
        "cmd",
        "command",
        "shell",
        "exec",
        "path",
        "file",
        "journal",
        "journalctl",
        # 自由转发
        "proxy",
        "forward",
        # 以谁的身份取 / 取谁的记录。``key`` 在 Atlas 里是一等概念
        # （``requested_key``），本层**没有**把它暴露出去；既然没有参数接它，
        # 就必须把「传了它」变成一个明确的 400，而不是一次静默的忽略。
        "uid",
        "user",
        "username",
        "owner",
        "principal",
        "subject",
        "role",
        "key",
        "key_id",
        "as_user",
        "login_name",
    }
)


class IdentityError(Exception):
    """解析主体失败。``reason`` 是给前端的稳定 token，``status`` 是 HTTP 码。"""

    status = 401
    reason = "no_authenticated_session"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.reason)
        self.detail = detail


class NoSession(IdentityError):
    """没有会话，或会话已失效 / 已撤销 / 已过期。"""

    status = 401
    reason = "no_authenticated_session"


class NotLinuxSession(IdentityError):
    """会话是真的，但不是 Linux 账号登录来的。"""

    status = 403
    reason = REASON_NOT_LINUX


class AccountDisabled(IdentityError):
    """账号已被停用。403 而不是 401：会话本身没问题，是人不该进来。"""

    status = 403
    reason = "account_disabled"


class IdentityAsserted(IdentityError):
    """请求试图自己声明身份。"""

    status = 400
    reason = REASON_IDENTITY_ASSERTED


class SourceAsserted(IdentityError):
    """请求试图自己声明**数据来源**。

    与自报身份同一类：``?source=live`` 与 ``X-Source: live`` 都不改变任何东西，
    但「读到了不用」意味着下游某一层可以再把它捡起来——而那正好是「来源可自选」
    这个缺陷的开端。所以这里是 400，不是静默忽略。
    """

    status = 400
    reason = REASON_SOURCE_ASSERTED


class CredentialConfusion(IdentityError):
    """请求同时带了两种凭据通道（session cookie **与** ``Authorization``）。

    ``resolve_credential`` 本来就把「谁负责这个请求」写成显式的一步：带
    ``Authorization`` 的请求归 token 通道，其余归会话通道。但**这条路由**的写前置
    必须知道该不该要 CSRF —— 而两种凭据同时出现时，任何选择都是一次隐式降级：

    * 先看 cookie 就要 CSRF，于是 CLI 的正常形态被拒；
    * 先看 token 就跳过 CSRF，于是一个「既有会话又有 token」的请求可以借 token
      的名义绕开浏览器侧的 CSRF 保护。

    两个都不选。同时出现即 400，让调用方明确自己用的是哪一条通道。
    """

    status = 400
    reason = REASON_CREDENTIAL_CONFUSION


class ProxyAsserted(IdentityError):
    """请求试图指定**去哪取数**（上游地址、查询表达式、文件路径、命令）。

    与 :class:`SourceAsserted` 并列：那一条拒绝「自己声明来源」，这一条拒绝
    「自己声明目标」。两者都会让某条路径变成客户端可指向的通道，只是指的方向
    不同 —— 一个指向「谁说的」，一个指向「去哪拿」。
    """

    status = 400
    reason = REASON_PROXY_ASSERTED


class TokenAuthNotEnabled(IdentityError):
    """请求带了 ``Authorization: Bearer``，但本部署没有启用 token 通道。

    401 而不是 403：``Authorization`` 头**不是**一条已认证的凭据，本部署根本没有
    接受它的通道。默认关闭是刻意的——scoped token 的签发/撤销/审计归 IAM Lane，
    在它落地之前，一个「看起来能用」的 token 通道就是一处没有审计的旁路。
    """

    status = 401
    reason = REASON_TOKEN_AUTH_NOT_ENABLED


@dataclass(frozen=True)
class Principal:
    """一个解析完成的主体：受信凭据 + 目录里的账号 + 由**提权状态**推出的 scope。

    ``effective_role`` 与 ``elevated`` 是同义的两面：普通 Linux 会话一律 ``member``，
    只有处于管理员模式的会话才在 TTL 内是 ``platform_admin``。``user.role`` 仍然
    留着（审计与展示要读它），但**没有任何判权路径读它**。

    ``user`` 是产生这条主体的 **Portal 会话行**，因此只有会话通道拿得到它：
    scoped CLI token 不是一次浏览器登录，没有会话行，也没有用户表里的角色，
    所以那条通道给的是 ``None``。没有一个判权路径读 ``user``——它只供审计与展示，
    而「没有」是一个诚实取值，不是一个待补的默认值。
    """

    user: auth_store.User | None
    principal: str
    uid: int
    linux_user: str
    portal_scopes: tuple[str, ...]
    requester: RequesterContext
    elevated: bool = False
    effective_role: str = auth_store.ROLE_MEMBER

    @property
    def is_platform_admin(self) -> bool:
        return bff_scopes.is_platform_admin(elevated=self.elevated)


def reject_identity_assertions(
    header_names: object = (),
    body: object = None,
) -> None:
    """请求里出现自报身份的痕迹就抛 :class:`IdentityAsserted`。

    比对用的是 Registry 自己的 ``FORBIDDEN_IDENTITY_HEADERS``，所以两条入口的
    拒绝清单不可能各自漂移。body 的字段级拒绝**故意**不在这里做：invoke 的
    payload 合法地含有 ``role``（聊天消息的角色），字段级规则属于各自的路由。
    """
    lowered = set()
    # ``dict_keys`` / generators are iterable but are not a list, tuple or set;
    # testing for one of those shapes silently turned this guard into a no-op,
    # which is exactly the failure mode it exists to prevent.
    if not isinstance(header_names, (str, bytes)) and isinstance(
        header_names, Iterable
    ):
        lowered = {str(name).strip().lower() for name in header_names}
    offending = sorted(lowered & FORBIDDEN_IDENTITY_HEADERS)
    if offending:
        raise IdentityAsserted(
            "identity must not be asserted by the client; "
            f"remove header(s): {', '.join(offending)}"
        )


def reject_identity_field_assertions(
    *,
    query_keys: object = (),
    body_fields: object = (),
) -> None:
    """请求试图用**字段**自报身份时抛 :class:`IdentityAsserted`（400）。

    与 :func:`reject_identity_assertions` 是同一类守卫的两个面：那个只看**请求头**
    （``X-UID`` / ``X-Role`` ……），这一个看**查询串与 JSON 体的字段名**。
    ``uid`` / ``role`` / ``owner`` / ``principal`` / ``is_admin`` 出现在这两处之一，
    都不改变任何东西 —— 但「读到了不用」意味着下游某一层可以把它重新捡起来，
    所以这里是拒绝，不是忽略。

    字段表直接取 ``backend.registry.identity.FORBIDDEN_IDENTITY_FIELDS``：三个入口
    （Registry 传输层、``backend.auth`` 的 write preflight、本层）必须对「什么算自报
    身份」给出同一个答案，各写一份就是三份会漂移的规则。
    （``backend.auth`` 因为处在更低层、不能依赖 Registry，另存了一份并由它自己的测试
    把两边钉在一起；本层没有那个约束，直接复用。）

    **本函数按路由调用，不是全局中间件。** 有些载荷里 ``role`` 是合法内容
    （例如聊天消息的发言人角色），一刀切会把它误伤成 400。因此「这条路由的字段策略」
    写在 ``app.py`` 的 ``IDENTITY_FIELD_GUARDED`` 里，是一个**声明出来的集合**，
    而不是每个处理函数的记性。
    """
    offending = sorted(
        (_lowered_names(query_keys) | _lowered_names(body_fields))
        & FORBIDDEN_IDENTITY_FIELDS
    )
    if offending:
        raise IdentityAsserted(
            "identity must not be asserted by the client; "
            f"remove: {', '.join(offending)}"
        )


def reject_source_assertions(
    header_names: object = (),
    query_keys: object = (),
) -> None:
    """请求里出现自报**数据来源**的痕迹就抛 :class:`SourceAsserted`。

    ``source`` 是服务端的属性，由装配它的人决定（``OpsConfig``、各模块 adapter），
    不是请求可以挑的东西。一个 ``?source=live`` 什么也不改变——除非某一层决定
    「读到了就用」，而那正是这条守卫要让它不可能发生的地方。

    ``query_keys`` 用 ``request.query_keys()`` 而不是已经取回的单个值：参数名是
    不区分大小写的等价项（``Source``、``SOURCE``），而这里是按**名字**拒绝整个
    请求，不是按值。
    """
    lowered_headers = _lowered_names(header_names)
    offending_headers = sorted(lowered_headers & FORBIDDEN_SOURCE_HEADERS)

    lowered_query = _lowered_names(query_keys)
    offending_query = sorted(lowered_query & {SOURCE_QUERY_KEY})

    if offending_headers or offending_query:
        offending = offending_headers + offending_query
        raise SourceAsserted(
            "the data source is decided by the server; "
            f"remove the client-declared source: {', '.join(offending)}"
        )


def reject_proxy_assertions(query_keys: object = ()) -> None:
    """请求里出现「去哪取数」的参数就抛 :class:`ProxyAsserted`。

    每一条同源数据路由的目标都在**服务端装配**里：一个注入的适配器、一份注入的
    语料、一个零参数的投影读取器。这些处理器结构上没有一根线可以把请求里的
    字符串接到一个上游地址上 —— 这条守卫把「没有那根线」变成**可以被断言的**
    事实，而不是靠每个新处理器作者的记性。

    ``query_keys`` 与 :func:`reject_source_assertions` 同样的理由用参数**名**的
    集合：``?URL=`` 与 ``?url=`` 是同一个参数。
    """
    offending = sorted(_lowered_names(query_keys) & FORBIDDEN_PROXY_QUERY_KEYS)
    if offending:
        raise ProxyAsserted(
            "the upstream target is decided by the server; "
            f"remove the client-declared target parameter(s): {', '.join(offending)}"
        )


def _lowered_names(names: object) -> set[str]:
    """头/参数名归一成小写集合。与 ``reject_identity_assertions`` 同样的理由：
    传进来的可能是 ``dict_keys``、generator 或集合——只认 list/tuple/set 会把守卫
    悄悄变成空操作。"""
    if isinstance(names, (str, bytes)) or not isinstance(names, Iterable):
        return set()
    return {str(name).strip().lower() for name in names}


def uid_from_external_id(external_id: object) -> int | None:
    """``"<host_id>:<uid>"`` ⇒ uid。取不到就返回 ``None``（不猜）。"""
    if not isinstance(external_id, str) or ":" not in external_id:
        return None
    _, _, raw = external_id.rpartition(":")
    if not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


def resolve_principal(
    *,
    session_id: object,
    auth_service,
    accounts,
    cookie_name: str | None = None,
    admin_mode=None,
) -> Principal:
    """会话 cookie 值 ⇒ :class:`Principal`。任何一步不成立都抛错。

    ``accounts`` 是 ``backend.auth_linux.accounts.PwdSource``：uid ⇒ 账号名的
    权威来源。测试注入假目录，生产用 ``SystemPwdSource``（真 ``pwd`` 查询）。

    ``admin_mode`` 是可选的 ``backend.bff.admin_mode.AdminModeService``。它**只**
    决定这一条会话此刻是否处于管理员模式；不传就是"没提权"，因此每个既有调用点
    在改完之前都只会拿到一个成员主体——这是刻意的失败方向。

    提权查询本身出错（库坏了、锁住了）会被吞掉并当作**未提权**：鉴权路径上唯一
    安全的缺省是"少给权限"。那次故障仍然看得出来——``/auth/admin/status`` 与
    服务端日志会如实报告，而不是靠一个静默提权来提示。
    """
    del cookie_name  # 由调用方负责从 cookie jar 里取值；这里只收值。

    if not isinstance(session_id, str) or not session_id:
        raise NoSession("no session cookie was presented")

    try:
        session = auth_service.current_session(session_id)
    except auth_errors.AccountDisabled as exc:
        # 会话有效但账号被停用：这是「不许进来」，不是「没登录」。
        raise AccountDisabled(str(exc)) from None
    except auth_errors.AuthError as exc:  # 未知 / 过期 / 撤销 / 畸形，一律「没会话」
        raise NoSession(str(exc)) from None
    if session is None:  # pragma: no cover - current_session 以上面的异常收场
        raise NoSession("the session is unknown, expired or revoked")

    user = session.user
    if not user.is_active:  # pragma: no cover - authenticate 已经拦下停用账号
        raise AccountDisabled("the account behind this session is disabled")

    if user.auth_source != linux_handoff.AUTH_SOURCE_LINUX:
        # 基线里的 Feishu / local-admin 会话是真的，但它们不是 Linux 身份；
        # 于是它们也拿不到 Registry 主体。诚实地说清楚是哪一条不满足。
        raise NotLinuxSession(
            f"session auth_source is {user.auth_source!r}; "
            f"only {linux_handoff.AUTH_SOURCE_LINUX!r} sessions map to a registry principal"
        )

    uid = uid_from_external_id(user.external_id)
    if uid is None:
        raise NoSession("the linux session carries no usable uid binding")

    account = accounts.by_uid(uid)
    if account is None:
        # 目录里查不到这个 uid —— 账号可能已被删除。没有主体，不给「大概是他」。
        raise NoSession(f"uid {uid} is not present in the account directory")

    elevated = _is_elevated(admin_mode, session_id=session_id, principal=user.id, uid=uid)
    role = bff_scopes.session_role(elevated=elevated)
    granted = bff_scopes.session_scopes(elevated=elevated)

    return Principal(
        user=user,
        principal=user.id,
        uid=uid,
        linux_user=account.name,
        portal_scopes=tuple(sorted(granted.portal)),
        elevated=elevated,
        effective_role=role,
        requester=RequesterContext(
            principal=user.id,
            uid=uid,
            linux_user=account.name,
            scopes=granted.registry,
            # roles 只用于展示与审计，不参与任何判定 —— 与 Registry 的约定一致。
            # 因此这里写的是**有效角色**，不是用户表里那个。
            roles=frozenset({role}),
            token_label=None,
        ),
    )


def _is_elevated(admin_mode, *, session_id: object, principal: str, uid: int) -> bool:
    """提权状态。没接 admin_mode 就是没提权；查不动也是没提权。"""
    if admin_mode is None:
        return False
    try:
        return bool(admin_mode.is_elevated(session_id, principal=principal, uid=uid))
    except Exception:  # noqa: BLE001 - 鉴权路径上的任何故障都只能少给权限
        return False


# --------------------------------------------------------------------------
# 凭据通道：浏览器会话 vs scoped CLI token
#
# 两条通道产出**同一种**主体（``Principal``），但它们的可信来源完全不同：
#
# * 会话   —— 一条服务端会话行（``backend.auth``），身份来自 PAM 登录；
# * token  —— Registry 的 ``TokenDirectory`` 里的一条绑定，身份来自签发那一刻。
#
# 因此这个接缝存在的意义不是「多支持一种凭据」，而是让**通道本身**可被声明、
# 可被关闭、可被断言：一个部署要么显式启用 token 通道，要么它对每一个
# ``Authorization`` 头都答 401。默认是后者。
#
# 本阶段（Phase A）只落契约与守卫：真正的 token 签发/到期/撤销/审计归 IAM Lane，
# 它落地之前把通道打开，等于开一条没有审计的旁路。
# --------------------------------------------------------------------------


class PrincipalSource(Protocol):
    """一条凭据通道。

    ``handles`` 决定这条通道**是否负责**这个请求，``resolve`` 只接受请求本身、
    不接受任何身份声明。把「谁负责」做成显式的一步，是为了让「同时带 cookie 与
    Authorization 的请求」有一个写下来的答案，而不是由 if 的先后顺序决定。
    """

    name: str

    @property
    def enabled(self) -> bool: ...

    def handles(self, request) -> bool: ...

    def resolve(self, request) -> Principal: ...


class SessionPrincipalSource:
    """浏览器会话通道：cookie → 服务端会话行 → 目录里的账号。

    这是 :func:`resolve_principal` 的薄包装，**没有**第二套实现：``resolve``
    只是把 cookie 值交给那一个函数。
    """

    name = "session"

    def __init__(self, resolve: Callable[..., Principal], *, cookie_name: str) -> None:
        self._resolve = resolve
        self._cookie_name = cookie_name

    @property
    def enabled(self) -> bool:
        # 会话通道是本构建**唯一**启用的通道，没有第二个开关。
        return True

    def handles(self, request) -> bool:
        """会话通道是**兜底**：每一个不由别的通道负责的请求都归它。

        它永远返回 ``True``，因此 :meth:`OpsApplication.resolve_credential` 的
        循环一定会落在某条通道上——「没有通道负责」因此不会变成一条被忘掉的路径。
        """
        del request
        return True

    def resolve(self, request) -> Principal:
        return self._resolve(request.cookies.get(self._cookie_name))


class TokenIntrospectionLike(Protocol):
    """``introspect(token)`` 的返回值形状。

    IAM Lane 的 ``backend/auth/tokens.py::TokenIntrospection`` 就是这个形状
    （字段名逐个相同），所以那个实现可以直接当 ``introspector`` 用。本层只读
    ``active`` / ``principal`` / ``uid`` / ``linux_user`` / ``scopes`` / ``roles`` /
    ``label``；多出来的字段（``token_id`` / ``expires_at`` / ``state`` …）不读、
    也不要求。
    """

    active: bool
    principal: str | None
    uid: int | None
    linux_user: str | None
    scopes: frozenset[str]
    roles: frozenset[str]
    label: str | None


@dataclass(frozen=True)
class TokenSubject:
    """本层自带的一份 :class:`TokenIntrospectionLike`。

    用途有二：测试里的 fixture，以及 :class:`RegistryTokenIntrospector` 的返回值。
    ``reason`` 只在服务端日志里有意义——面向客户端的答复一律塌成同一个 401。
    """

    active: bool
    reason: str = "unknown"
    principal: str | None = None
    uid: int | None = None
    linux_user: str | None = None
    scopes: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    label: str | None = None

    @classmethod
    def inactive(cls, reason: str = "unknown") -> "TokenSubject":
        return cls(active=False, reason=reason)


class RegistryTokenIntrospector:
    """把**既有的** Registry 凭据目录适配成 :class:`TokenIntrospectionLike`。

    这条桥存在的理由：在 IAM Lane 的 scoped token 服务合入之前，Registry 的
    ``TokenDirectory`` + ``PrincipalDirectory`` 已经是「token → 主体 + scope」的
    一个真实实现（它不认识 JWT、只做等值查找）。有这条桥，token 通道今天就能被
    端到端地测；等 ``backend.auth.tokens`` 合入，装配处换成那个服务即可，本文件
    不需要改。

    与 IAM 那条通道的差别是**有意**的：这个目录是内存的、没有到期时间、没有签发
    审计。所以它只配用来做契约测试，不是一个可以在生产打开的签发者——默认关闭的
    那半个开关正是为这件事存在的。
    """

    def __init__(self, tokens, principals) -> None:
        self._tokens = tokens
        self._principals = principals

    def introspect(self, token: str) -> TokenSubject:
        from ..registry.identity import resolve_requester

        try:
            requester = resolve_requester(token, self._tokens, self._principals)
        except Exception as exc:  # noqa: BLE001 - 未知/撤销/未审批一律 inactive
            return TokenSubject.inactive(str(exc) or "unknown credential")
        return TokenSubject(
            active=True,
            reason="active",
            principal=requester.principal,
            uid=requester.uid,
            linux_user=requester.linux_user,
            scopes=requester.scopes,
            roles=requester.roles,
            label=requester.token_label,
        )


class ScopedTokenPrincipalSource:
    """scoped CLI token 通道：``Authorization: Bearer <token>`` → 主体。

    **默认关闭。** 关闭时 ``resolve`` 对任何 ``Authorization`` 头都抛
    :class:`TokenAuthNotEnabled`（401），包括一个真的有效的 token——「本部署不接受
    这种凭据」与「这条凭据无效」是两个不同的答案，混起来会让人以为开个开关就能用。

    启用时，token 的解释**完全**交给一个 ``introspector``（见
    :class:`TokenIntrospectionLike`）。本层不做自己的解析、不查自己的表、不解析
    任何自签结构——它只把「这条凭据是谁」这个问题转给那条通道的权威实现，然后把
    答案翻成 :class:`Principal`。

    接缝的形状来自 IAM Lane 已经实现的
    ``backend/auth/tokens.py::TokenIntrospection``：它的字段
    （``active`` / ``principal`` / ``uid`` / ``linux_user`` / ``scopes`` /
    ``roles`` / ``label``）正是这里要读的那几个，而它自己的 ``to_registry_dict()``
    已经声明「这就是 ``backend.registry.identity`` 解析所依据的形状」。因此这个类
    **不 import** 那个模块（它会先合入别的分支），只需要一个提供
    ``introspect(token)`` 的对象。把 IAM 的服务接上来是一行装配，不是一次重写。

    ``accounts`` 可选：给了就用**会话通道同一条规则**再核一遍——``uid`` 必须在
    账号目录里，且目录里的登录名必须与 introspect 说的那个一致。这样两条通道对
    「这个 uid 是谁」不可能给出两个答案。
    """

    name = "scoped_token"

    def __init__(
        self,
        *,
        introspector,
        enabled: bool = False,
        accounts=None,
    ) -> None:
        self._introspector = introspector
        self._enabled = bool(enabled)
        self._accounts = accounts

    @property
    def enabled(self) -> bool:
        return self._enabled

    def handles(self, request) -> bool:
        """带 ``Authorization`` 头的请求归这条通道——包括**关闭**的时候。

        关掉时把它让给会话通道，等于让一个「本部署不接受 token」的请求退化成
        「cookie 说了算」；那样一次凭据混淆会被静默解决掉。拒绝要比猜测清楚。
        """
        return _header(request, "Authorization") is not None

    def resolve(self, request) -> Principal:
        from ..registry.identity import extract_bearer_token

        captured = extract_bearer_token(_header(request, "Authorization"))
        if captured is None:
            # 不是一条 Bearer 凭据：这不是 token 通道要回答的请求。
            raise NoSession("no scoped token was presented")
        if not self._enabled:
            raise TokenAuthNotEnabled(
                "this deployment does not accept scoped CLI tokens; "
                "use a browser session, or have the operator enable the token channel"
            )

        try:
            found = self._introspector.introspect(captured)
        except Exception as exc:  # noqa: BLE001 - 通道故障只能少给权限
            raise NoSession(str(exc) or "the token channel could not be consulted") from None

        # 只认 ``active``。它之外的一切理由都塌成同一个 401：一个面向公网的入口
        # 不该用「已撤销 / 已过期 / 未审批」把一个可用的试探变成一个预言机。
        if not getattr(found, "active", False):
            raise NoSession("the presented credential is not active")

        principal = found.principal
        uid = found.uid
        linux_user = found.linux_user
        if not isinstance(principal, str) or not principal:
            raise NoSession("the credential resolves to no principal")
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise NoSession("the credential carries no usable uid binding")

        if self._accounts is not None:
            account = self._accounts.by_uid(uid)
            if account is None:
                # 与 :func:`resolve_principal` 同一条规则：目录里没有这个 uid，
                # 就没有主体。
                raise NoSession(f"uid {uid} is not present in the account directory")
            if linux_user is not None and account.name != linux_user:
                raise NoSession(
                    f"uid {uid} is {account.name!r} in the account directory, "
                    f"not {linux_user!r}"
                )
            linux_user = account.name

        scopes = frozenset(getattr(found, "scopes", ()) or ())
        roles = frozenset(getattr(found, "roles", ()) or ())

        return Principal(
            user=None,
            principal=principal,
            uid=uid,
            linux_user=linux_user or principal,
            # token 的能力 = 签发时写下的那几条（∩ portal 词表）。
            portal_scopes=tuple(sorted(scopes & bff_scopes.PORTAL_KNOWN_SCOPES)),
            requester=RequesterContext(
                principal=principal,
                uid=uid,
                linux_user=linux_user or principal,
                scopes=scopes,
                # roles 只用于展示与审计，不参与判定——与 Registry 的约定一致。
                roles=roles,
                token_label=getattr(found, "label", None),
            ),
            # token 不是会话：没有会话行，也就没有提权。
            elevated=False,
            effective_role=bff_scopes.session_role(elevated=False),
        )


def authorization_header(request) -> object:
    """请求里的 ``Authorization`` 头；没有就是 ``None``。

    公开的薄封装：路由层需要**在解析主体之前**知道「这条请求走的是哪条凭据通道」，
    因为 CSRF 只对浏览器会话有意义。它只读头，不做任何判断。
    """
    return _header(request, "Authorization")


def _header(request, name: str) -> object:
    """WSGI 请求上的一个头值。取不到就是 ``None``。

    ``backend.auth.wsgi.build_request`` 把头收成一个**小写、连字符**键的普通
    ``dict``，所以这里先把名字规范化，而不是假设调用方记得这件事。
    """
    headers = getattr(request, "headers", None)
    if not isinstance(headers, dict):
        return None
    return headers.get(name.strip().lower())


__all__ = [
    "FORBIDDEN_IDENTITY_FIELDS",
    "FORBIDDEN_PROXY_QUERY_KEYS",
    "FORBIDDEN_SOURCE_HEADERS",
    "REASON_CREDENTIAL_CONFUSION",
    "REASON_IDENTITY_ASSERTED",
    "REASON_NOT_LINUX",
    "REASON_PROXY_ASSERTED",
    "REASON_SOURCE_ASSERTED",
    "REASON_TOKEN_AUTH_NOT_ENABLED",
    "SOURCE_QUERY_KEY",
    "AccountDisabled",
    "CredentialConfusion",
    "IdentityAsserted",
    "IdentityError",
    "NoSession",
    "NotLinuxSession",
    "Principal",
    "PrincipalSource",
    "ProxyAsserted",
    "RegistryTokenIntrospector",
    "ScopedTokenPrincipalSource",
    "SessionPrincipalSource",
    "SourceAsserted",
    "TokenAuthNotEnabled",
    "TokenIntrospectionLike",
    "TokenSubject",
    "authorization_header",
    "reject_identity_assertions",
    "reject_identity_field_assertions",
    "reject_proxy_assertions",
    "reject_source_assertions",
    "resolve_principal",
    "uid_from_external_id",
]
