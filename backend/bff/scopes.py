"""两套 scope 词表，一个来源：受信角色。

V6 前端与 Registry 用的是**两套** scope 名，这不是笔误：

* 前端 ``web/js/features/capability.js`` 用 ``portal:*``（外加
  ``atlas:calls:self:read``）——它只读 ``scope.scopes``，且**刻意忽略角色**。
* ``backend/registry/identity.py`` 用 ``registry:*``——行级 ACL 只认这套。

集成层不能把其中一套删掉（前端已发布、Registry 已合入），也不能让两条判定
各自为政。所以两个词表都从这里、由**同一个受信角色**推出，两个结论互为推论而
非各写一遍：``tests/bff/test_scopes.py`` 断言 ``portal:admin:read`` 与
``registry:admin:read`` 恒同时出现、恒同时缺席。

**有效角色来自"这条会话是否处于管理员模式"，不来自用户表里的 ``role`` 列。**

这一条是 2026-10-15 改的，改的就是这里：在那之前，``ROLE_SCOPES`` 直接按用户表里
的角色查表，于是 ``carol`` —— 一个在 Dell 上确实是基础设施管理员、并且被 root-owned
授权文件标成 ``platform_admin`` 的人 —— **只要用 PAM 普通登录进来就自动拿到全局
只读**。那和"登录决定你是谁、管理员模式决定你此刻能看什么"是两回事，实测后果是
一个普通会话能读到全平台视图。

现在：

* ``role_scopes(role)`` 仍然是一张**词表**（角色 ⇒ 四组 scope），它没有被删掉，
  因为两套 scope 的一致性断言还需要它；
* 但**没有任何调用方可以直接拿用户表的角色去查这张表**。判断一个会话能看什么，
  一律走 :func:`session_role` / :func:`session_scopes`，它们只把 ``elevated``
  当作输入；
* 于是 ``carol`` 的普通会话是 ``member``，只有他**在自己的会话里输入共享管理口令**
  之后，那条会话才在 TTL 内变成 ``platform_admin``。

``backend.auth_linux.policy`` 的 root-owned 授权文件仍然决定"谁是基础设施管理员"，
但它**不再**单独决定 Portal 里任何一条会话的可见范围。未知角色 fail closed——
不给任何 scope。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..auth import store as auth_store
from ..evaluation import identity as evaluation_identity
from ..observability import identity as observability_identity
from ..registry import identity as registry_identity

# --------------------------------------------------------------------------
# 前端词表。逐个抄自 web/js/features/capability.js 的 SCOPES，抄错即测试失败。
# --------------------------------------------------------------------------

PORTAL_MODULES_READ = "portal:modules:read"
PORTAL_ENGINES_NATIVE = "portal:engines:native"
PORTAL_EVALUATION_NATIVE = "portal:evaluation:native"
PORTAL_ADMIN_READ = "portal:admin:read"
PORTAL_ENDPOINTS_SHARE = "portal:endpoints:share"
PORTAL_OBSERVABILITY_NATIVE = "portal:observability:native"
PORTAL_CALLS_SELF_READ = "atlas:calls:self:read"

PORTAL_KNOWN_SCOPES = frozenset(
    {
        PORTAL_MODULES_READ,
        PORTAL_ENGINES_NATIVE,
        PORTAL_EVALUATION_NATIVE,
        PORTAL_ADMIN_READ,
        PORTAL_ENDPOINTS_SHARE,
        PORTAL_OBSERVABILITY_NATIVE,
        PORTAL_CALLS_SELF_READ,
    }
)

# --------------------------------------------------------------------------
# Registry 词表。直接引用 Registry 自己的常量，不重抄字符串。
# --------------------------------------------------------------------------

REGISTRY_KNOWN_SCOPES = registry_identity.KNOWN_SCOPES

# --------------------------------------------------------------------------
# 观测与评测词表。同样直接引用各自模块的常量。
# --------------------------------------------------------------------------

OBSERVABILITY_KNOWN_SCOPES = observability_identity.ALL_SCOPES
EVALUATION_KNOWN_SCOPES = evaluation_identity.KNOWN_SCOPES

#: ``portal:X`` ⇒ 它蕴含的其它模块 scope。**只用于断言两侧一致**，不用于判定：
#: 判定用的是下面 ``RoleScopes`` 里按词表分组写好的几组，避免出现「映射表是权限
#: 来源」这种一句话就能绕过的设计。这里断言的是：**任何一个** admin read 出现，
#: 另外三个也必然出现（四套词表的「全局只读」是同一个决定）。
PORTAL_IMPLIES_ADMIN_READ = {
    PORTAL_ADMIN_READ: (
        registry_identity.SCOPE_ADMIN_READ,
        observability_identity.SCOPE_ADMIN_READ,
        evaluation_identity.SCOPE_EVAL_ADMIN_READ,
    ),
}

#: ``portal:X`` ⇒ 它蕴含的 Registry scope。
PORTAL_IMPLIES_REGISTRY = {
    PORTAL_ADMIN_READ: registry_identity.SCOPE_ADMIN_READ,
    PORTAL_ENDPOINTS_SHARE: registry_identity.SCOPE_ENDPOINT_SHARE,
}


@dataclass(frozen=True)
class RoleScopes:
    """一个受信角色对应的四组 scope（前端、Registry、观测、评测）。"""

    portal: frozenset[str]
    registry: frozenset[str]
    observability: frozenset[str]
    evaluation: frozenset[str]


#: 成员：能读目录、能登记元数据、能共享自己的 Endpoint、能调用被授权的
#: Endpoint、能读自己的调用日志、能看**自己**的用量与评测队列，以及整机/整卡读数
#: （``observability:host:read``）。**没有**任何 admin 能力。
#:
#: 整机读数原先只给管理员；平台的核心用途就是让共用这台 GPU 工作站的成员看到
#: 资源占用（2026-10-21 由 Owner 确认），所以成员也能读。它仍是一个独立 scope，
#: Grafana/Prometheus 原生界面等其它管理面不受影响。
_MEMBER = RoleScopes(
    portal=frozenset(
        {
            PORTAL_MODULES_READ,
            PORTAL_CALLS_SELF_READ,
            PORTAL_ENDPOINTS_SHARE,
        }
    ),
    registry=frozenset(
        {
            registry_identity.SCOPE_REGISTRY_READ,
            registry_identity.SCOPE_REGISTRY_WRITE,
            registry_identity.SCOPE_ENDPOINT_SHARE,
            registry_identity.SCOPE_ENDPOINT_INVOKE,
        }
    ),
    observability=frozenset(
        {observability_identity.SCOPE_SELF_READ, observability_identity.SCOPE_HOST_READ}
    ),
    evaluation=frozenset(
        {
            evaluation_identity.SCOPE_EVAL_READ,
            evaluation_identity.SCOPE_EVAL_SUBMIT,
        }
    ),
)

#: 平台管理员 = 成员 + 全局只读（四套词表同时打开）。**刻意不含**：
#:
#: * ``registry:admin:manage`` —— 管理视图是「看见全部」，不是「能改别人的东西」；
#: * ``evaluation:dispatch:real`` —— 真实评测派发在本部署**永远**关闭。把它放进
#:   角色里就等于说「有个开关能打开它」，而实际的门是准入策略 + 未接线的 adapter
#:   （两道锁）。少发一个 scope，是第三道。
_ADMIN = RoleScopes(
    portal=_MEMBER.portal | {PORTAL_ADMIN_READ},
    registry=_MEMBER.registry | {registry_identity.SCOPE_ADMIN_READ},
    # 整机/整卡读数与全局用量摘要同属管理面；host 读数单独一个 scope，因此这里
    # 显式写两条，而不是让 admin 顺带拿到它。
    observability=_MEMBER.observability | {observability_identity.SCOPE_ADMIN_READ},
    evaluation=_MEMBER.evaluation | {evaluation_identity.SCOPE_EVAL_ADMIN_READ},
)

ROLE_SCOPES: dict[str, RoleScopes] = {
    auth_store.ROLE_MEMBER: _MEMBER,
    auth_store.ROLE_ADMIN: _ADMIN,
}

def operator_scopes() -> RoleScopes:
    """运维词表 = 管理员词表 + ``registry:admin:manage``。

    为什么单独开一档，而不是把它并进 ``_ADMIN``：``registry:admin:manage`` 是
    **登记一台既有系统服务**的准入（``RegistryService._engine_target_for``），它必须
    与 ``operator-policy.json`` 这份运维承认文件配套——而那份文件写在本机、由运维
    维护。平台管理员的职责是「看见全部」，不是「把任意端口上的服务收进目录」，
    所以那档词表保持原样。

    唯一使用这份词表的是 :func:`_local_admin_principal`：bootstrap 的本机 admin
    是由磁盘上的管理口令造出来的运维身份，它没有 Linux 账号、也不在主体目录里，
    因此它的所有权证明不来自 Linux uid，而来自运维策略那一条 entry
    （``operator_verified``）。
    """
    return RoleScopes(
        portal=_ADMIN.portal,
        registry=_ADMIN.registry | {registry_identity.SCOPE_ADMIN_MANAGE},
        observability=_ADMIN.observability,
        evaluation=_ADMIN.evaluation,
    )


#: 未知 / 缺失角色得到的一组。显式写出来，好让「fail closed」是可以被断言的
#: 事实，而不是「大概不会给吧」。
NO_SCOPES = RoleScopes(
    portal=frozenset(),
    registry=frozenset(),
    observability=frozenset(),
    evaluation=frozenset(),
)


def session_role(*, elevated: bool) -> str:
    """这条会话**此刻**的有效角色。

    唯一的输入是提权状态。刻意不接受用户表里的 ``role``：那个值可以被 root-owned
    授权文件写成 ``platform_admin``，而"被标成平台管理员"与"此刻正在管理员模式里"
    是两件事——把它们混起来，就是一个普通的 PAM 登录直接看到全平台。
    """
    return auth_store.ROLE_ADMIN if elevated else auth_store.ROLE_MEMBER


def role_scopes(role: object) -> RoleScopes:
    """角色 ⇒ 四组 scope。未知角色 fail closed。

    **这是一张词表，不是判定的入口。** 调用方要判断一条会话能看什么，请用
    :func:`session_scopes`；直接把这个函数用在用户表的角色上，正是 2026-10-15
    修掉的那个缺陷。
    """
    if not isinstance(role, str):
        return NO_SCOPES
    return ROLE_SCOPES.get(role, NO_SCOPES)


def session_scopes(*, elevated: bool) -> RoleScopes:
    """会话事实 ⇒ 四组 scope。**所有鉴权路径的唯一入口。**"""
    return role_scopes(session_role(elevated=elevated))


def is_platform_admin(role: object = None, *, elevated: bool = False) -> bool:
    """是否持有平台管理员能力。

    位置参数 ``role`` 保留只为兼容既有调用点，**不参与判定**——只传角色就永远
    得到 ``False``，因为角色本身不再是提权的来源。
    """
    del role
    return elevated


def portal_scopes_for(role: object) -> tuple[str, ...]:
    return tuple(sorted(role_scopes(role).portal))


def registry_scopes_for(role: object) -> frozenset[str]:
    return role_scopes(role).registry


def observability_scopes_for(role: object) -> frozenset[str]:
    return role_scopes(role).observability


def evaluation_scopes_for(role: object) -> frozenset[str]:
    return role_scopes(role).evaluation


def is_global_reader(role: object) -> bool:
    """四个词表里的「全局只读」是否同时成立。见 ``PORTAL_IMPLIES_ADMIN_READ``。"""
    granted = role_scopes(role)
    return (
        PORTAL_ADMIN_READ in granted.portal
        and registry_identity.SCOPE_ADMIN_READ in granted.registry
        and observability_identity.SCOPE_ADMIN_READ in granted.observability
        and evaluation_identity.SCOPE_EVAL_ADMIN_READ in granted.evaluation
    )


__all__ = [
    "EVALUATION_KNOWN_SCOPES",
    "NO_SCOPES",
    "OBSERVABILITY_KNOWN_SCOPES",
    "PORTAL_ADMIN_READ",
    "PORTAL_CALLS_SELF_READ",
    "PORTAL_ENDPOINTS_SHARE",
    "PORTAL_ENGINES_NATIVE",
    "PORTAL_EVALUATION_NATIVE",
    "PORTAL_IMPLIES_ADMIN_READ",
    "PORTAL_IMPLIES_REGISTRY",
    "PORTAL_KNOWN_SCOPES",
    "PORTAL_MODULES_READ",
    "PORTAL_OBSERVABILITY_NATIVE",
    "REGISTRY_KNOWN_SCOPES",
    "ROLE_SCOPES",
    "RoleScopes",
    "evaluation_scopes_for",
    "is_global_reader",
    "is_platform_admin",
    "observability_scopes_for",
    "portal_scopes_for",
    "registry_scopes_for",
    "role_scopes",
    "session_role",
    "session_scopes",
]
