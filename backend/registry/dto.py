"""对外 DTO 的形状：纯 JSON 可序列化的嵌套 dict。

沿用仓库既有做法（``backend.atlas.dto`` / ``backend.portal.modules``）：

* 输出是**纯 dict**，便于 ``json.dumps(..., sort_keys=True)`` 做逐字节回归；
* 顶层带 ``schema_version`` / ``source`` / ``synthetic``，且 ``synthetic`` **没有**
  默认值 —— 调用方必须显式声明，避免把合成数据说成生产数据；
* 测不到的东西是 ``None`` + ``unknown``，不是 ``0``。

本模块**只负责形状**，不查库、不判权。红不红码由调用方以显式布尔量传入，
这样「谁看得见什么」就只有 ``authz.py`` 一个地方说了算。
"""

from __future__ import annotations

from typing import Iterable

from .health import (
    REASON_NOT_CONFIGURED,
    STATE_UNKNOWN,
    unknown,
)
from .models import Deployment, Endpoint, Engine, Model, RuntimeBuild

#: DTO 模式版本。字段增删必须同步 ``docs/registry-cli-contract.md``。
SCHEMA_VERSION = "registry/1"

#: 部署形态。``synthetic`` 是本仓库此刻唯一诚实的取值：没有生产部署。
MODE_SYNTHETIC = "synthetic"
MODE_LIVE = "live"
MODES = (MODE_SYNTHETIC, MODE_LIVE)

#: 用量/日志数据源的状态。源没接上就是 NOT_CONFIGURED，不是「0 次调用」。
USAGE_OK = "OK"
USAGE_NOT_CONFIGURED = "NOT_CONFIGURED"
USAGE_UNKNOWN = "UNKNOWN"


def envelope(
    data: object,
    *,
    mode: str,
    source: str,
    notes: Iterable[str] | None = None,
    display_hints: Iterable[str] | None = None,
    scope: dict | None = None,
    extra_meta: dict | None = None,
) -> dict:
    """标准响应体：``data`` + 契约元信息。"""
    if mode not in MODES:
        raise ValueError(f"unknown registry mode: {mode!r}")
    if not source:
        raise ValueError("a DTO must declare its source")
    synthetic = mode == MODE_SYNTHETIC
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "source": source,
        "synthetic": synthetic,
        "data": data,
        "scope": scope,
        "notes": list(notes or ()),
        "display_hints": list(display_hints or ()),
    }
    if extra_meta:
        payload["meta"] = dict(extra_meta)
    if synthetic:
        # 合成形态必须在**每一份**响应里自我声明，而不是只在文档里写一句。
        payload["display_hints"] = [
            "SYNTHETIC: 本部署非生产，数据不代表真实运行状态。",
            *payload["display_hints"],
        ]
    return payload


def not_configured_block(*, what: str, reason: str = REASON_NOT_CONFIGURED) -> dict:
    """一个诚实的「这条来源本部署没有」。"""
    return {
        "status": USAGE_NOT_CONFIGURED,
        "reason": reason,
        "what": what,
        "value": None,
        "state": STATE_UNKNOWN,
        "source": None,
    }


def render_engine(engine: Engine, *, reveal_target: bool) -> dict:
    """Engine 视图。非 owner 看不到 loopback 目标地址（见 ``authz`` 的说明）。"""
    payload = engine.to_dict()
    if not reveal_target:
        target = payload.get("target") or {}
        payload["target"] = {
            "scheme": target.get("scheme"),
            "host": None,
            "port": None,
            "path": None,
            "query": None,
            "url": None,
            "redacted": True,
        }
        payload["target_redacted"] = True
    else:
        payload["target_redacted"] = False
    return payload


def render_model(model: Model, *, reveal_weight_path: bool) -> dict:
    return model.to_dict(reveal_weight_path=reveal_weight_path)


def render_build(build: RuntimeBuild, *, reveal_binary_path: bool) -> dict:
    return build.to_dict(reveal_binary_path=reveal_binary_path)


def render_deployment(deployment: Deployment) -> dict:
    return deployment.to_dict()


def render_endpoint(
    endpoint: Endpoint, *, grantees: tuple[str, ...] = (), include_acl: bool
) -> dict:
    return endpoint.to_dict(grantees=grantees, include_acl=include_acl)


def render_audit(entries: Iterable[object]) -> list[dict]:
    return [entry.to_dict() for entry in entries]  # type: ignore[attr-defined]


def scope_block(
    *,
    principal: str,
    uid: int,
    scopes: Iterable[str],
    roles: Iterable[str],
    is_admin_read: bool,
    is_admin_manage: bool,
    elevated_read: bool = False,
) -> dict:
    """审计用的授权快照。``roles`` 只展示，不参与判定（同 atlas/portal 约定）。"""
    return {
        "principal": principal,
        "uid": uid,
        "scopes": sorted(scopes),
        "roles": sorted(roles),
        "is_admin_read": is_admin_read,
        "is_admin_manage": is_admin_manage,
        "elevated_read": elevated_read,
    }


def unknown_health() -> dict:
    return unknown()
