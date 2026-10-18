"""回环预览用的 Atlas 只读端点：受信 Session + 离线合成 fixture -> ``AtlasDTO``。

这个模块是 ``dev/preview_auth.py`` 的**开发专用**附件，只有三件事：

1. 一份**服务端受信**的演示 key 目录，加一份**离线合成** fixture（每行都带
   ``SYNTHETIC`` 标注）—— 两者合起来就是演示数据的**全部**来源；
2. 一张**服务端受信**的 grant 表 ``subject -> 项目/scope``：由运维 / CLI 在
   进程内注入，**不**来自请求，**不**看 role；
3. 一个把 ``(会话用户, grant, 查询参数)`` 映射成 ``AtlasDTO`` 的纯函数。

它**不是**一条新的授权规则。真正的判定仍然是 ``backend.atlas`` 的
``resolve_authorized_scope``（缺 scope、越权项目一律 ``AccessDenied``）；这里
只是把「谁是谁、能看哪些项目」从**会话 + 服务端配置**里取出来喂进去。

它**不**读真实 ``usage.jsonl``、不联网、不读环境变量、不监听端口。整个模块
不做任何 IO，除了读那一个 fixture 文件。

**为什么 grant 表不认 role**：``role == platform_admin`` 是「这个人能管理这台
部署」，不是「这个人能看用量」。把两者绑起来，等于让每一个管理员账号自动获得
全部项目的用量视图 —— 那正是 ``backend.atlas`` 明确拒绝的事（见
``docs/atlas-data-contract.md``）。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from backend.atlas import (
    SCOPE_USAGE_READ,
    AtlasRequest,
    GatewayKeyBinding,
    KeyDirectory,
    Provenance,
    RequesterContext,
    build_atlas_dto,
)

#: 预览里 Atlas 只读端点的路径。**只**由 ``dev/preview_auth.py`` 提供。
ATLAS_USAGE_PATH = "/api/atlas/usage"

#: 写进 DTO ``source`` 的值。它必须一眼看出「这不是 Gateway 生产统计」，
#: 前端任何按 ``source`` 分流的逻辑都不该把它当 live 数据。
SYNTHETIC_SOURCE = "synthetic-preview"

FIXTURE_PATH = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "atlas_preview_sample.jsonl"
)

#: 演示窗口。**不是**「最近 N 小时」：合成数据只有这一段，用服务器时钟去推一个
#: 窗口只会得到一个空 DTO，而空 DTO 看起来像「没有流量」——一个自己造出来的结论。
#: 窗口是演示数据的一部分，所以它和被演示的数据放在同一个地方声明。
DEMO_WINDOW_FROM = "2026-10-14T00:00:00Z"
DEMO_WINDOW_TO = "2026-10-14T04:00:00Z"

DEFAULT_BUCKET = "1h"

#: 演示 fixture 覆盖的合成项目。CLI 不写项目时就是这一组。
DEMO_PROJECTS = ("proj-a", "proj-b")

#: 一条 grant 默认给出的 scope。``unattributed`` / ``global-diagnostics`` 刻意
#: **不**默认包含：它们要显式追加，和产品里的规矩一致。
DEFAULT_GRANT_SCOPES = (SCOPE_USAGE_READ,)

#: 允许出现在查询串里的参数。其余一律忽略 —— 尤其是任何形如 ``role``、
#: ``key_id``、``label`` 的字段：它们看着像身份，实则和授权毫无关系。
QUERY_PARAMS = ("project", "subject", "bucket")

#: 演示 key 目录：``key_id -> principal/project/workload`` 的**服务端**权威映射。
#: 生产里这张表由运维经审批写入；预览里它就是下面这几行虚构数据。
#: 客户端可自报的 ``label`` 与任何 ``X-`` 头**永远**不参与这张表。
DEMO_BINDINGS = (
    GatewayKeyBinding("key-alpha", "u-1001", "proj-a", "resume-parse"),
    GatewayKeyBinding("key-beta", "u-1002", "proj-a", "code-assist"),
    GatewayKeyBinding("key-orphan", "u-1001", "proj-a", None),
    GatewayKeyBinding("key-gamma", "u-2001", "proj-b", "eval-suite"),
    GatewayKeyBinding("key-revoked", "u-3001", "proj-a", "resume-parse", revoked=True),
)

DEMO_DIRECTORY = KeyDirectory(DEMO_BINDINGS)

#: 每次响应都自称的来源。``Provenance`` 没有默认值，正是为了不让调用方
#: 默默把合成数据说成生产统计。
PROVENANCE = Provenance(source=SYNTHETIC_SOURCE, synthetic=True)


# --- 服务端受信的 grant 表 ----------------------------------------------------


@dataclass(frozen=True)
class AtlasPreviewGrant:
    """一个账号在预览里被**显式**授予的 Atlas 范围。"""

    subject: str
    projects: tuple[str, ...]
    scopes: tuple[str, ...] = DEFAULT_GRANT_SCOPES

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("an atlas grant needs a non-empty subject")
        if not self.projects:
            # 空项目集永远走不到数据，只会安静地 403 —— 与其让人对着一个
            # 「明明配了却什么都没有」的现象排查，不如在配置期就喊出来。
            raise ValueError("an atlas grant needs at least one project")
        if not self.scopes:
            raise ValueError("an atlas grant needs at least one scope")


class AtlasPreviewGrants:
    """``subject -> grant`` 的进程内表。**按 subject 查，不按 role。**

    这张表只有两个入口：构造（静态配置）和 :meth:`set`（配置期覆盖）。两者都
    是**进程内**动作 —— HTTP 面上没有任何路径能到达它，所以浏览器既不能给自己
    发一个 scope，也不能靠改 role 拿到一个。
    """

    def __init__(self, grants: Iterable[AtlasPreviewGrant] = ()) -> None:
        self._by_subject: dict[str, AtlasPreviewGrant] = {}
        for grant in grants:
            self._check(grant)
            if grant.subject in self._by_subject:
                # 静态配置里同一个 subject 写两遍是笔误，不是「后者覆盖前者」。
                raise ValueError(f"duplicate atlas grant for subject {grant.subject!r}")
            self._by_subject[grant.subject] = grant

    @staticmethod
    def _check(grant: AtlasPreviewGrant) -> None:
        if not isinstance(grant, AtlasPreviewGrant):
            raise TypeError("grants must be AtlasPreviewGrant instances")

    def __len__(self) -> int:
        return len(self._by_subject)

    def for_subject(self, subject: str) -> AtlasPreviewGrant | None:
        """命中返回 grant；未登记返回 ``None``（调用方按 403 处理）。"""
        return self._by_subject.get(subject)

    def set(self, grant: AtlasPreviewGrant) -> None:
        """登记或覆盖一个 subject 的 grant。配置期动作，非 HTTP 可达。"""
        self._check(grant)
        self._by_subject[grant.subject] = grant


# --- 纯函数 ------------------------------------------------------------------


def read_fixture_text() -> str:
    """按 UTF-8 读取那唯一一份合成 fixture 原文。"""
    return FIXTURE_PATH.read_text(encoding="utf-8")


def demo_window() -> tuple[datetime, datetime]:
    return _parse_utc(DEMO_WINDOW_FROM), _parse_utc(DEMO_WINDOW_TO)


def requester_context(user, grant: AtlasPreviewGrant) -> RequesterContext:
    """会话用户 + 服务端 grant -> Atlas 要求的请求者上下文。

    ``user.id`` 是**会话**给出的主体，不是请求里的任何一个字段；``user.role``
    只进 ``roles``（展示/审计用，不产生权限）。
    """
    return RequesterContext(
        subject=user.id,
        roles=frozenset({user.role}),
        scopes=frozenset(grant.scopes),
        authorized_projects=frozenset(grant.projects),
    )


def build_dto(
    *,
    user,
    grant: AtlasPreviewGrant,
    requested_project: str | None = None,
    requested_subject: str | None = None,
    bucket: str = DEFAULT_BUCKET,
) -> dict:
    """构建演示 DTO。越权项目 / 缺 scope 由 ``backend.atlas`` 抛 ``AccessDenied``。"""
    window_from, window_to = demo_window()
    request = AtlasRequest(
        requester=requester_context(user, grant),
        requested_subject=requested_subject,
        requested_project=requested_project,
        window_from=window_from,
        window_to=window_to,
        bucket=bucket,
    )
    return build_atlas_dto(
        request, read_fixture_text(), DEMO_DIRECTORY, PROVENANCE
    )


def build_payload(
    *,
    user,
    grant: AtlasPreviewGrant,
    observed_at: float,
    requested_project: str | None = None,
    requested_subject: str | None = None,
    bucket: str = DEFAULT_BUCKET,
) -> dict:
    """端点 ``data`` 块：DTO 本体 + 这份响应**是在什么时候**被构建的。

    ``observed_at`` 是服务器的时钟，不是数据的时间 —— 合成数据的时间范围写在
    ``atlas.window`` 里，两者刻意分开，免得读者把「刚生成的响应」当成「刚采集的
    观测」。
    """
    return {
        "observed_at": _iso_z(observed_at),
        "atlas": build_dto(
            user=user,
            grant=grant,
            requested_project=requested_project,
            requested_subject=requested_subject,
            bucket=bucket,
        ),
    }


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_z(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


__all__ = [
    "ATLAS_USAGE_PATH",
    "AtlasPreviewGrant",
    "AtlasPreviewGrants",
    "DEFAULT_BUCKET",
    "DEFAULT_GRANT_SCOPES",
    "DEMO_BINDINGS",
    "DEMO_DIRECTORY",
    "DEMO_PROJECTS",
    "DEMO_WINDOW_FROM",
    "DEMO_WINDOW_TO",
    "FIXTURE_PATH",
    "PROVENANCE",
    "QUERY_PARAMS",
    "SYNTHETIC_SOURCE",
    "build_dto",
    "build_payload",
    "demo_window",
    "read_fixture_text",
    "requester_context",
]
