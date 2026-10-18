"""测试共用脚手架：路径引导、受信目录、请求构造、期望常量。

**这里的每一个 ``key_id`` 都是虚构的**，``GatewayKeyBinding`` 就是那份
"服务端可信目录" 的测试替身；真实目录将来由 BFF 从受信配置加载。
"""

from __future__ import annotations

import os
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.atlas import (  # noqa: E402
    SCOPE_CALLS_SELF_READ,
    SCOPE_USAGE_READ,
    SCOPE_USAGE_UNATTRIBUTED,
    AtlasRequest,
    GatewayKeyBinding,
    KeyDirectory,
    Provenance,
    RequesterContext,
)
from backend.atlas.dto import SCHEMA_VERSION  # noqa: E402

FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"

#: 测试窗口：2026-10-14T00:00Z 起 4 小时（左闭右开）。
WINDOW_FROM = "2026-10-14T00:00:00Z"
WINDOW_TO = "2026-10-14T04:00:00Z"


def load_fixture(name: str) -> str:
    """按 UTF-8 读取静态 fixture 原文。"""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def trusted_directory() -> KeyDirectory:
    """测试用受信 key 目录（虚构数据）。"""
    return KeyDirectory(
        [
            GatewayKeyBinding("key-alpha", "u-1001", "proj-a", "resume-parse"),
            GatewayKeyBinding("key-beta", "u-4001", "proj-a", "code-assist"),
            GatewayKeyBinding("key-orphan", "u-1001", "proj-a", None),
            GatewayKeyBinding("key-gamma", "u-2001", "proj-b", "eval-suite"),
            GatewayKeyBinding("key-revoked", "u-3001", "proj-a", "resume-parse", revoked=True),
        ]
    )


def requester(
    subject: str = "u-1001",
    *,
    roles: tuple[str, ...] = ("user",),
    scopes: tuple[str, ...] = (SCOPE_USAGE_READ,),
    projects: tuple[str, ...] = ("proj-a",),
) -> RequesterContext:
    return RequesterContext(
        subject=subject,
        roles=frozenset(roles),
        scopes=frozenset(scopes),
        authorized_projects=frozenset(projects),
    )


def atlas_request(
    requester_ctx: RequesterContext | None = None,
    *,
    requested_project: str | None = "proj-a",
    window_from: str = WINDOW_FROM,
    window_to: str = WINDOW_TO,
    bucket: str = "1h",
    requested_subject: str | None = None,
    model_filter: str | None = None,
) -> AtlasRequest:
    from datetime import datetime

    def _parse(value: str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    ctx = requester_ctx if requester_ctx is not None else requester()
    return AtlasRequest(
        requester=ctx,
        requested_subject=requested_subject,
        requested_project=requested_project,
        window_from=_parse(window_from),
        window_to=_parse(window_to),
        bucket=bucket,
        model_filter=model_filter,
    )


#: 合成数据必须显式声明来源，避免被当成生产数据。
SYNTHETIC = Provenance(source="gateway", synthetic=True)


# --- 「我的调用」视图的脚手架 --------------------------------------------------
#
# 目录刻意比 trusted_directory() 多两把 key，因为逐条视图要区分的是
# **归属**而不是项目：同一项目里的他人 key（key-beta）与本人但越出项目白名单的
# key（key-outside）都必须被挡住，而这两种情形在只按项目聚合的目录里看不出差别。


def calls_directory() -> KeyDirectory:
    """逐条视图的测试目录：覆盖归属 / 项目 / 吊销三种边界的组合。"""
    return KeyDirectory(
        [
            # 本人（u-1001）名下、在授权项目内 —— 本视图的可见集合。
            GatewayKeyBinding("key-alpha", "u-1001", "proj-a", "resume-parse"),
            GatewayKeyBinding("key-orphan", "u-1001", "proj-a", None),
            # 本人名下但已吊销：归属仍是本人，明细里不出现，只留计数。
            GatewayKeyBinding("key-retired", "u-1001", "proj-a", "resume-parse", revoked=True),
            # 本人名下但项目不在白名单 —— 项目仍是最外层边界。
            GatewayKeyBinding("key-outside", "u-1001", "proj-b", "eval-suite"),
            # 同一个项目里的**他人** key：只按项目聚合时会被一并计量，逐条视图必须挡住。
            GatewayKeyBinding("key-beta", "u-4001", "proj-a", "code-assist"),
            # 另一个项目的他人 key。
            GatewayKeyBinding("key-gamma", "u-2001", "proj-b", "eval-suite"),
        ]
    )


def calls_request(
    requester_ctx: RequesterContext | None = None,
    *,
    requested_key: str | None = None,
    window_from: str = WINDOW_FROM,
    window_to: str = WINDOW_TO,
    outcome: str = "all",
    model: str | None = None,
    limit: int = 50,
    before_ts: str | None = None,
    before_call_id: str | None = None,
    content_consent: str = "absent",
    gateway_attached: bool = False,
):
    """构造一条 :class:`backend.atlas.calls.SelfCallsRequest`。"""
    from datetime import datetime

    from backend.atlas import SelfCallsRequest

    def _parse(value: str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    ctx = requester_ctx if requester_ctx is not None else self_requester()
    return SelfCallsRequest(
        requester=ctx,
        window_from=_parse(window_from),
        window_to=_parse(window_to),
        requested_key=requested_key,
        outcome=outcome,
        model=model,
        limit=limit,
        before_ts=_parse(before_ts) if before_ts else None,
        before_call_id=before_call_id,
        content_consent=content_consent,
        gateway_attached=gateway_attached,
    )


def self_requester(
    subject: str = "u-1001",
    *,
    roles: tuple[str, ...] = ("user",),
    scopes: tuple[str, ...] = (SCOPE_CALLS_SELF_READ,),
    projects: tuple[str, ...] = ("proj-a",),
) -> RequesterContext:
    """默认持有 ``atlas:calls:self:read``（**不**含 ``atlas:usage:read``）。"""
    return RequesterContext(
        subject=subject,
        roles=frozenset(roles),
        scopes=frozenset(scopes),
        authorized_projects=frozenset(projects),
    )


__all__ = [
    "FIXTURES_DIR",
    "SCHEMA_VERSION",
    "SYNTHETIC",
    "WINDOW_FROM",
    "WINDOW_TO",
    "atlas_request",
    "calls_directory",
    "calls_request",
    "load_fixture",
    "requester",
    "self_requester",
    "trusted_directory",
]
