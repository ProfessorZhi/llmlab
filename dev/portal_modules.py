"""回环预览用的 Portal 模块目录端点：受信 Session -> 模块状态 DTO。

这是 ``dev/preview_auth.py`` 的**开发专用**附件，与 :mod:`dev.atlas_calls`、
:mod:`dev.atlas_preview` 平行。它只做三件事：

1. 一张**服务端受信**的 grant 表 ``subject -> scopes``（由 CLII / 测试在进程内注入）；
2. 一份**离线合成**的 Endpoint 目录（``principal`` 绑定到被授权的那个主体）；
3. 把 ``(会话用户, grant)`` 喂给 :func:`backend.portal.build_modules_dto` 的纯函数。

**为什么部署事实是写死的空值。** 预览里没有 Gateway、没有 NVML、没有 Grafana，
所以 ``DeploymentFacts()`` 就是**如实的描述**，不是「还没配」。这让每一个模块都
落到它真实的状态上：

* ``engines`` → ``unknown``（引擎未知）
* ``playground`` → ``disabled``（本构建没有、也不会有真实推理后端）
* ``evaluation`` → ``unavailable``（缺 scope / 未配置）
* ``administration`` / ``observability`` → 可用，但指标全是 UNKNOWN 并说明没有来源

没有任何运行参数能把这里变成 live。想让它变真，必须先真的接上那些东西，并改这
一行 —— 这正是「不显示假 GPU 数字」「不假装已接线」的落点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.atlas.dto import Provenance
from backend.atlas.identity import RequesterContext
from backend.portal import DeploymentFacts, EndpointRecord, ModuleRequest, build_modules_dto
from backend.portal.modules import SCOPE_MODULES_READ

#: 预览里 Portal 模块端点的路径。**只**由 ``dev/preview_auth.py`` 提供。
MODULES_PATH = "/api/atlas/modules"

#: 与另外两个端点用同一个来源标记：三者都是合成预览。
SYNTHETIC_SOURCE = "synthetic-preview"

#: 预览的部署事实。**空值即事实**：这台机器上确实没有接这些东西。
PREVIEW_FACTS = DeploymentFacts()

PROVENANCE = Provenance(source=SYNTHETIC_SOURCE, synthetic=True)

#: 允许出现在查询串里的参数。其余一律忽略 —— 尤其是 ``subject``、``role``、
#: ``X-Forwarded-User``：它们看着像身份，实则与授权无关。
QUERY_PARAMS = ("visibility",)

#: 一条 grant 默认给出的 scope。
#:
#: 只有**读目录**这一个。它是「告诉我有哪些模块、每个为什么不能用」的最小权限，
#: 不打开任何入口。多给一个 scope 就是多打开一个原生界面，那该由下这个决定的人
#: 显式写出，而不是由一个 `--portal-modules-grant` 顺手带上。
DEFAULT_GRANT_SCOPES = (SCOPE_MODULES_READ,)

#: 预览的合成 Endpoint 目录。
#:
#: ``principal`` 在构建时绑定到被授权的主体，所以「我的 Endpoint」与「同事的
#: Endpoint」在演示里是真的可区分的，而不是靠前端过滤出来的。
DEMO_OWN_ENDPOINTS = (
    ("ep-demo-1", "my-chat", "OpenAI Chat", "llm-a", False),
    ("ep-demo-2", "team-vision", "Images / Chat", "llm-a", True),
)

#: 同事的 Endpoint。它们存在的意义是让「默认 Private 看不到别人」在预览里可复现。
DEMO_FOREIGN_ENDPOINTS = (
    ("ep-demo-9", "colleague-chat", "OpenAI Chat", "llm-b", False, "u-demo-colleague", "proj-a"),
    ("ep-demo-8", "other-project", "OpenAI Chat", "llm-c", True, "u-demo-colleague", "proj-b"),
)


@dataclass(frozen=True)
class PortalModuleGrant:
    """一个账号在预览里被显式授予的 Portal 范围。"""

    subject: str
    scopes: tuple[str, ...] = DEFAULT_GRANT_SCOPES
    projects: tuple[str, ...] = ("proj-a",)

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("a portal grant needs a non-empty subject")
        if not self.scopes:
            raise ValueError("a portal grant needs at least one scope")
        if not self.projects:
            raise ValueError("a portal grant needs at least one project")


class PortalModuleGrants:
    """``subject -> grant`` 的进程内表。**按 subject 查，不按 role。**

    与 :class:`dev.atlas_preview.AtlasPreviewGrants` 同构：只有构造与 :meth:`set`
    两个入口，两者都是**进程内**动作，HTTP 面上没有任何路径能到达它。
    """

    def __init__(self, grants=()) -> None:
        self._by_subject: dict[str, PortalModuleGrant] = {}
        for grant in grants:
            self.set(grant)

    def __len__(self) -> int:
        return len(self._by_subject)

    def for_subject(self, subject: str) -> PortalModuleGrant | None:
        return self._by_subject.get(subject)

    def set(self, grant: PortalModuleGrant) -> None:
        if not isinstance(grant, PortalModuleGrant):
            raise TypeError("grants must be PortalModuleGrant instances")
        self._by_subject[grant.subject] = grant


def build_endpoints(subject: str) -> tuple[EndpointRecord, ...]:
    """为**一个**演示主体构建合成 Endpoint 目录。

    ``subject`` 是配置期确定的主体，不是请求里的调用者 —— 按每个请求的调用者
    生成目录，会让每个获授账号都「拥有」同一批演示 Endpoint。
    """
    principal = (subject or "").strip()
    if not principal:
        raise ValueError("an endpoint directory needs a non-empty subject")

    records = [
        EndpointRecord(
            endpoint_id=endpoint_id,
            name=name,
            principal=principal,
            project="proj-a",
            protocol=protocol,
            model=model,
            shared=shared,
        )
        for endpoint_id, name, protocol, model, shared in DEMO_OWN_ENDPOINTS
    ]
    records.extend(
        EndpointRecord(
            endpoint_id=endpoint_id,
            name=name,
            principal=owner,
            project=project,
            protocol=protocol,
            model=model,
            shared=shared,
        )
        for endpoint_id, name, protocol, model, shared, owner, project in DEMO_FOREIGN_ENDPOINTS
    )
    return tuple(records)


def requester_context(user, grant: PortalModuleGrant) -> RequesterContext:
    """会话用户 + 服务端 grant -> 模块目录要求的请求者上下文。

    ``user.role`` 只进 ``roles``（展示/审计用，不产生权限）。
    """
    return RequesterContext(
        subject=grant.subject,
        roles=frozenset({user.role}),
        scopes=frozenset(grant.scopes),
        authorized_projects=frozenset(grant.projects),
    )


def build_dto(*, user, grant: PortalModuleGrant, visibility: str = "private") -> dict:
    """构建演示 DTO。缺 scope 由 ``backend.portal`` 抛 ``AccessDenied``。"""
    return build_modules_dto(
        ModuleRequest(
            requester=requester_context(user, grant),
            visibility=visibility,
            endpoints=build_endpoints(grant.subject),
        ),
        PREVIEW_FACTS,
        PROVENANCE,
    )


def build_payload(*, user, grant: PortalModuleGrant, observed_at: float, visibility: str = "private") -> dict:
    """端点 ``data`` 块：DTO 本体 + 这份响应**是在什么时候**被构建的。"""
    return {
        "observed_at": _iso_z(observed_at),
        "portal": build_dto(user=user, grant=grant, visibility=visibility),
    }


def parse_visibility(raw: str) -> str:
    """解析 ``visibility``。取值域**精确**是两个小写词，别的都拒绝。

    刻意不做 ``strip()`` / ``lower()``：本仓库在别处（``parse_limit``）同样拒绝
    「宽容解析」。可见性虽然不是授权输入，但它是**契约**输入 —— 一个能被两种写法
    拼出来的值，会让「服务端到底收到了什么」变成一个需要猜的问题。
    """
    from backend.portal.modules import VISIBILITY_MODES

    if not isinstance(raw, str) or raw not in VISIBILITY_MODES:
        raise ValueError(f"visibility must be exactly one of {VISIBILITY_MODES}")
    return raw


def _iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DEFAULT_GRANT_SCOPES",
    "DEMO_FOREIGN_ENDPOINTS",
    "DEMO_OWN_ENDPOINTS",
    "MODULES_PATH",
    "PREVIEW_FACTS",
    "PROVENANCE",
    "PortalModuleGrant",
    "PortalModuleGrants",
    "SYNTHETIC_SOURCE",
    "build_dto",
    "build_endpoints",
    "build_payload",
    "parse_visibility",
    "requester_context",
]
