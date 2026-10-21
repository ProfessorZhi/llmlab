"""Registry DTO + 受信主体 ⇒ ``portal-ops/1``。

形状的权威在 ``web/js/features/contract.js``（前端已发布，逐字节校验它自己收到的
payload）。本模块**只**做两件事：从 Registry 取数、把数摆成那个形状。

取数一律走 Registry 自己的行级 ACL（``RegistryService.policy(requester)`` →
``AccessPolicy``），记录形状一律走 Registry 自己的 renderer（``dto.render_*``）。
本模块里**没有**一处自己判定「谁能看见什么」：``visible_*`` 的返回值就是答案，
这里只负责它变成前端字段。

几条刻意的不作为，都有测试兜着：

* **观测与评测是 adapter stub。** 没有观测源，就每个指标都是 ``value: null`` +
  ``state: unknown`` + 原因，``trends`` 是 ``null``。不画一条没人测过的曲线，
  也不给一个没人算过的分数。
* **试跑台对 mock Gateway 保持关闭。** adapter 是 ``mock`` 时 ``authorized``
  恒为 ``false``：测试替身不是「经授权的交互式后端」，把用户 prompt 转发给它
  再宣称「已审计」是假的。
* **``registry.all`` 只在 ``portal:admin:read`` 下非空。** 契约会把「没有该
  scope 却给了非空 all」判为违约，所以这里是真门，不是显示约定。
* **health / runtime 一律透传 Registry 的 reason。** 不认识的 reason 前端原样
  显示成 token，好过这里替它编一个更好看的词。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..observability import dto as observability_dto
from typing import Callable, Iterable, Protocol

from ..registry import dto as registry_dto
from ..registry import health as registry_health
from ..registry import models as registry_models
from . import scopes as bff_scopes
from .sharing import endpoint_share_block, narrowest_share

OPS_SCHEMA = "portal-ops/1"

VISIBILITY_PRIVATE_PORTAL = "private"
VISIBILITY_SHARED_PORTAL = "shared"
VISIBILITIES = (VISIBILITY_PRIVATE_PORTAL, VISIBILITY_SHARED_PORTAL)

HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNREACHABLE = "unreachable"
HEALTH_UNKNOWN = "unknown"

RUNTIME_RUNNING = "running"
RUNTIME_STOPPED = "stopped"
RUNTIME_UNKNOWN = "unknown"

#: 前端认识的 reason token。这里列出来的会在 UI 上显示成人话，其余原样显示。
REASON_REGISTRY_NOT_ATTACHED = "registry_adapter_not_attached"
REASON_OBSERVABILITY_NOT_ATTACHED = "observability_adapter_not_attached"
REASON_EVALUATION_NOT_READY = "evaluation_backend_not_ready"
REASON_NO_AUTHORIZED_BACKEND = "no_authorized_backend"
REASON_NO_VERIFIED_SOURCE = "no_verified_metric_source"
REASON_MISSING_SCOPE = "missing_required_scope"
REASON_NO_RUNTIME_MONITOR = "no_runtime_monitor"

#: 测试替身适配器的名字。``MockGatewayAdapter`` 可以被「接上」，接上的也确实是它——
#: 但它永远不是一条经授权的交互式后端。这个常量存在的理由就是让**两处**判定
#: （``transport_mode`` 的标签与 ``gateway_authorized`` 的试跑台）读同一个事实，
#: 而不是一处写了 ``!= "mock"``、另一处忘了写。
GATEWAY_ADAPTER_MOCK = "mock"

#: 指标骨架。名字/单位与前端 fixture 一致，好让面板有确定的行；**值全部为 null**。
METRIC_SKELETON = (
    ("gateway_requests_24h", "24h 调用数", "次", "gateway"),
    ("gateway_error_rate_pct", "错误率", "%", "gateway"),
    ("gateway_p95_ms", "p95 响应延迟", "ms", "gateway"),
    ("gpu_vram_total_mib", "GPU 显存总量", "MiB", "gpu"),
    ("gpu_vram_used_mib", "GPU 显存占用", "MiB", "gpu"),
    ("gpu_utilization_pct", "GPU 利用率", "%", "gpu"),
)

#: Registry 的 health.state ⇒ 前端的 (health, runtime_status)。
#: 「healthy 却说没在跑」是契约直接拒绝的组合，所以这两列必须一起映射。
_HEALTH_TO_PORTAL = {
    registry_health.STATE_HEALTHY: (HEALTH_HEALTHY, RUNTIME_RUNNING),
    registry_health.STATE_DEGRADED: (HEALTH_DEGRADED, RUNTIME_RUNNING),
    registry_health.STATE_UNREACHABLE: (HEALTH_UNREACHABLE, RUNTIME_STOPPED),
    registry_health.STATE_UNKNOWN: (HEALTH_UNKNOWN, RUNTIME_UNKNOWN),
}


# --------------------------------------------------------------------------
# 尚未接线的两块：接口先定下来，值一律 UNKNOWN
# --------------------------------------------------------------------------


class ObservabilityAdapter(Protocol):
    """观测来源。

    ``identity`` 是 :class:`backend.bff.identity.Principal` —— **已解析的受信
    主体**，不是客户端传来的任何东西。实现方自己把它翻成所属模块的主体类型；
    本层不做第二次身份判定。"""

    name: str
    attached: bool

    def metrics(self, *, identity, is_admin: bool) -> list[dict]: ...

    def trends(self, *, identity, is_admin: bool, window: str) -> dict | None: ...


class DetachedObservabilityAdapter:
    """没有观测源时的诚实答复：每个指标都是 UNKNOWN + 原因，没有曲线。"""

    name = "detached"
    attached = False
    reason = REASON_OBSERVABILITY_NOT_ATTACHED

    def metrics(self, *, identity, is_admin: bool) -> list[dict]:
        del identity, is_admin
        return [
            {
                "name": name,
                "label": label,
                "unit": unit,
                "kind": kind,
                "value": None,
                "state": "unknown",
                "source": None,
                "updated_at": None,
                "reason": self.reason,
            }
            for name, label, unit, kind in METRIC_SKELETON
        ]

    def trends(self, *, identity, is_admin: bool, window: str) -> dict | None:
        del identity, is_admin, window
        # 不是「空曲线」，是**没有曲线**：null 让前端连坐标轴都不画。
        return None


class EvaluationAdapter(Protocol):
    """评测后端。本部署没有实现，只有下面的 detached stub。"""

    name: str
    attached: bool

    def runs(self, *, identity, is_admin: bool) -> list[dict]: ...

    def submit(self, *, identity, is_admin: bool) -> dict: ...


class DetachedEvaluationAdapter:
    """没有评测后端：没有 run，提交按钮未就绪并说明原因。"""

    name = "detached"
    attached = False
    reason = REASON_EVALUATION_NOT_READY

    def runs(self, *, identity, is_admin: bool) -> list[dict]:
        del identity, is_admin
        return []

    def submit(self, *, identity, is_admin: bool) -> dict:
        del identity, is_admin
        return {"ready": False, "reason": self.reason}


@dataclass(frozen=True)
class OpsConfig:
    """BFF 侧的可调项。默认值全部是「本部署此刻诚实的取值」。"""

    #: Registry 的 ``RegistryConfig.mode``。``synthetic`` 是本仓库唯一诚实的取值。
    registry_mode: str = registry_dto.MODE_SYNTHETIC
    gateway_adapter: str = "detached"
    gateway_attached: bool = False
    window: str = "24h"

    @property
    def synthetic(self) -> bool:
        return self.registry_mode == registry_dto.MODE_SYNTHETIC

    @property
    def transport_mode(self) -> str:
        """``live`` 只在真的有 Gateway 接上、且不是合成形态、且不是测试替身时才可以声称。

        第三个条件是 2026-10-16 补上的：在它之前，一个 ``gateway_attached=True``
        的 ``MockGatewayAdapter`` 会让这一页顶栏写着「LIVE · 同源 BFF」——而同一个
        ``OpsConfig`` 在 ``build_ops`` 里是把试跑台关掉的（那里一直写着
        ``!= "mock"``）。同一份事实两处判定、只有一处写对，正是本 Lane 要收掉的那类
        不一致：**测试替身不是一条经授权的交互式后端**，它不能被标成 live。
        """
        if (
            self.synthetic
            or not self.gateway_attached
            or self.gateway_adapter == GATEWAY_ADAPTER_MOCK
        ):
            return "demo"
        return "live"


# --------------------------------------------------------------------------
# 记录映射
# --------------------------------------------------------------------------


def _health_columns(health: dict) -> tuple[str, str, str | None, str | None]:
    """Registry 的 health dict ⇒ (health, runtime_status, health_reason, runtime_reason)。"""
    state = str(health.get("state") or registry_health.STATE_UNKNOWN)
    portal_health, runtime = _HEALTH_TO_PORTAL.get(
        state, (HEALTH_UNKNOWN, RUNTIME_UNKNOWN)
    )
    reason = health.get("reason")
    health_reason = reason if isinstance(reason, str) and reason else None
    if portal_health == HEALTH_UNKNOWN and not health_reason:
        # 契约要求「unknown 必须说明为什么」，所以这里补一个最保守的默认值，
        # 而不是留空让它违约。
        health_reason = REASON_NO_VERIFIED_SOURCE
    runtime_reason = None
    if runtime == RUNTIME_UNKNOWN:
        runtime_reason = health_reason or REASON_NO_RUNTIME_MONITOR
    return portal_health, runtime, health_reason, runtime_reason


def _resources_block() -> dict:
    """GPU 数字的唯一诚实取值：没有 NVML 来源，就全是 null。

    前端契约对 ``gpu_*`` 有一条硬规则——带值必须带 ``nvml-verified`` 来源。
    本部署没有 NVML，所以这里连 ``source`` 都是 null。
    """
    return {
        "gpu_count": None,
        "vram_total_mib": None,
        "vram_used_mib": None,
        "vram_free_mib": None,
        "source": None,
        "updated_at": None,
    }


def _endpoints_of(deployment_id: str, endpoints) -> list:
    return [item for item in endpoints if item.deployment_id == deployment_id]


def deployment_record(
    *,
    deployment,
    model,
    engine,
    endpoints,
    grantees_by_endpoint: dict,
    subject: str,
    links: list[dict],
) -> dict:
    """一个 Deployment ⇒ 前端的一行。字段名照着 ``DEPLOYMENT_FIELDS`` 写。"""
    rendered = registry_dto.render_deployment(deployment)
    health, runtime, health_reason, runtime_reason = _health_columns(
        rendered.get("health") or {}
    )

    blocks = [
        endpoint_share_block(item, tuple(grantees_by_endpoint.get(item.id, ())))
        for item in endpoints
    ]
    share = narrowest_share(blocks)

    return {
        "deployment_id": deployment.id,
        "model_id": deployment.model_id,
        "model_label": (model.name if model is not None else None) or deployment.name,
        # Registry 里最接近「引擎类型」的封闭取值是 protocol。engine.name 是用户
        # 自己起的字符串，用它会让这一列既不稳定又不可比。
        "engine_type": engine.protocol if engine is not None else None,
        "owner_uid": deployment.owner_principal,
        "owner_is_self": deployment.owner_principal == subject,
        "health": health,
        "health_reason": health_reason,
        "runtime_status": runtime,
        "runtime_reason": runtime_reason,
        "registered": bool(rendered.get("registered", True)),
        "resources": _resources_block(),
        "share": share,
        "links": list(links),
        "updated_at": rendered.get("updated_at"),
    }


def principal_name(principals, principal: str) -> str:
    """主体 id ⇒ 给人看的名字（平台账户是显示名，Linux 主体是登录名）。查不到原样返回。"""
    record = principals.lookup(principal) if principals is not None else None
    if record is None:
        return principal
    return record.display_name or record.linux_user or principal


def engine_record(engine, *, subject: str, principals) -> dict:
    """一个已登记的推理服务（Registry engine）⇒ 前端的一行。

    ``run_as_user`` 是登记 / 重验时在本机观测到的监听进程用户（``/proc/net/tcp``
    的 uid 反查）；它是事实记录，不是授权依据。没观测到就是 ``None``。
    """
    ownership = dict(engine.ownership or {})
    framework = next(
        (tag.split(":", 1)[1] for tag in (engine.tags or ()) if tag.startswith("framework:")),
        None,
    )
    return {
        "engine_id": engine.id,
        "name": engine.name,
        "protocol": engine.protocol,
        "framework": framework,
        "port": (engine.target or {}).get("port"),
        "owner": engine.owner_principal,
        "owner_name": principal_name(principals, engine.owner_principal),
        "owner_is_self": engine.owner_principal == subject,
        "status": engine.status,
        "ownership_state": ownership.get("state"),
        "ownership_verified": bool(ownership.get("verified")),
        "run_as_user": ownership.get("linux_user"),
        "run_as_uid": ownership.get("uid"),
        "last_seen_at": registry_models.isoformat(engine.last_seen_at),
    }


def shared_endpoint_record(
    *,
    endpoint,
    model,
    engine,
    visible_grantees: tuple[str, ...],
    granted_by: str,
    links: list[dict],
) -> dict:
    """一个**共享给我**的 Endpoint ⇒ 前端的一行。

    ``visible_grantees`` 是**这一视角看得见**的那份名单，不是完整 ACL：
    Registry 只把完整受托人名单给 owner 与管理员（``authz`` 里
    ``include_acl`` 的判定）。非 owner 拿到的是「与我有关的那一条授权」，
    方向是**少报**，永远不会把别人的授权名单漏出去。
    """
    block = endpoint_share_block(endpoint, visible_grantees)
    if block["mode"] == "private":
        # 共享给我的东西不可能带着 private。真出现了，说明上游的可见性变了，
        # 这时宁可报成一条只指向我的授权，也不谎称它是公开的。
        block = {"mode": "selected", "principals": list(visible_grantees), "revocable": True}
    return {
        "endpoint_id": endpoint.id,
        "name": endpoint.name,
        "protocol": engine.protocol if engine is not None else None,
        "model": model.name if model is not None else None,
        "owner_uid": endpoint.owner_principal,
        "owner_is_self": False,
        "granted_by": granted_by,
        "share": block,
        "links": list(links),
    }


def _latest_grantor(store, endpoint_id: str, grantee: str, fallback: str) -> str:
    """「谁把这条共享给我的」——取最近一条未撤销的授权记录。"""
    try:
        history = store.grant_history(endpoint_id)
    except Exception:  # pragma: no cover - 存储层异常不该让整页 500
        return fallback
    for entry in reversed(list(history)):
        if entry.grantee_principal == grantee and entry.revoked_at is None:
            return entry.granted_by or fallback
    return fallback


def _key_record(binding) -> dict:
    """一条 Registry 凭据 ⇒ 前端的一行。**只有掩码前缀，永远没有凭据本身。**"""
    digest = hashlib.sha256(str(binding.token).encode("utf-8")).hexdigest()
    return {
        "key_id": f"tok_{digest[:12]}",
        "name": binding.label or "未命名凭据",
        "prefix": f"llk_••••{digest[:4]}",
        "scopes": sorted(binding.scopes),
        "status": "revoked" if binding.revoked else "active",
        # Registry 不记录签发与使用时间；null 比编一个时间戳诚实。
        "created_at": None,
        "last_used_at": None,
        "endpoint": "/api/v2/ops",
    }


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def _ops_metric_expiries(cards: list[dict]) -> dict[str, str]:
    """The browser's per-metric lease, derived from the *source catalogue* TTL.

    It is not a second, arbitrary freshness threshold: only a known catalogue
    metric with a trusted, timezone-aware observation and finite per-metric TTL
    receives a deadline. Missing proof means the browser cannot show a numeric
    LIVE card indefinitely. Synthetic/detached cards normally have no deadline.
    """
    expiries: dict[str, str] = {}
    for card in cards:
        if card.get("state") != "available" or card.get("value") is None:
            continue
        name = card.get("name")
        if not isinstance(name, str):
            continue
        spec = observability_dto.CATALOGUE.get(name)
        ttl = spec.stale_after_seconds if spec is not None else None
        if not isinstance(ttl, (float, int)) or isinstance(ttl, bool) or ttl <= 0:
            continue
        stamp = card.get("updated_at")
        if not isinstance(stamp, str):
            continue
        try:
            dt = datetime.fromisoformat(stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp)
        except ValueError:
            continue
        if dt.tzinfo is None or dt.utcoffset() is None:
            continue
        deadline = (dt + timedelta(seconds=ttl)).astimezone(timezone.utc)
        expiries[name] = deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return expiries


def build_ops(
    *,
    principal,
    service,
    visibility: str = VISIBILITY_PRIVATE_PORTAL,
    token_bindings: Iterable[object] = (),
    observability: ObservabilityAdapter | None = None,
    evaluation: EvaluationAdapter | None = None,
    role_lookup: Callable[[str], str | None] | None = None,
    links: list[dict] | None = None,
    config: OpsConfig | None = None,
) -> dict:
    """组装一份 ``portal-ops/1``。纯函数：只读，不写任何东西。"""
    if visibility not in VISIBILITIES:
        raise ValueError(f"unsupported visibility: {visibility!r}")

    observability = observability or DetachedObservabilityAdapter()
    evaluation = evaluation or DetachedEvaluationAdapter()
    config = config or OpsConfig()
    links = list(links or [])

    requester = principal.requester
    subject = requester.principal
    # 管理面（全局只读 + 全员名单）按 **portal** 词表判定。
    #
    # 这里原本读的是 ``requester.is_admin_read``，也就是 Registry 的
    # ``registry:admin:read``。对会话而言两者等价——``session_scopes`` 让这两条
    # admin read 恒同进同出（``tests/bff/test_scopes.py`` 断言了这件事）。但凭据不止
    # 会话一种：一条 scoped CLI token 的 scope 来自**签发时写下的那几条**，它可以
    # 带着 ``registry:admin:read``（那是 Registry 自己的全局只读）而并不处于任何
    # 会话的提权状态。用 Registry 词表决定 portal 的管理面，等于让一条只被授权读
    # 登记库的凭据看见全平台账号名单——两个词表本来就该各管各的面。
    is_admin = bff_scopes.PORTAL_ADMIN_READ in principal.portal_scopes

    policy = service.policy(requester)
    store = service.store

    endpoints = policy.visible_endpoints()
    deployments = policy.visible_deployments()

    grantees_by_endpoint = (
        {item.id: store.grantees(item.id) for item in endpoints} if is_admin else {}
    )
    for item in endpoints:
        if item.owner_principal == subject:
            grantees_by_endpoint[item.id] = store.grantees(item.id)

    def _model(model_id: str):
        return store.model_by_id(model_id)

    def _engine(engine_id: str):
        return store.engine_by_id(engine_id)

    mine = [item for item in deployments if item.owner_principal == subject]
    owned = [
        deployment_record(
            deployment=item,
            model=_model(item.model_id),
            engine=_engine(item.engine_id),
            endpoints=_endpoints_of(item.id, endpoints),
            grantees_by_endpoint=grantees_by_endpoint,
            subject=subject,
            links=links,
        )
        for item in mine
    ]

    for row in owned:
        row["owner_name"] = principal_name(service.principals, row["owner_uid"])

    engines = [
        engine_record(item, subject=subject, principals=service.principals)
        for item in policy.visible_engines()
    ]

    shared = []
    if visibility == VISIBILITY_SHARED_PORTAL:
        for item in endpoints:
            if item.owner_principal == subject:
                continue
            deployment = store.deployment_by_id(item.deployment_id)
            # 完整名单只给 owner 与管理员；其余视角只报「与我有关的那条授权」。
            visible_grantees = store.grantees(item.id) if is_admin else (subject,)
            shared.append(
                shared_endpoint_record(
                    endpoint=item,
                    model=_model(deployment.model_id) if deployment else None,
                    engine=_engine(deployment.engine_id) if deployment else None,
                    visible_grantees=visible_grantees,
                    granted_by=_latest_grantor(store, item.id, subject, item.owner_principal),
                    links=links,
                )
            )

    all_rows = []
    if is_admin:
        for item in deployments:
            model = _model(item.model_id)
            all_rows.append(
                {
                    "deployment_id": item.id,
                    "model_id": item.model_id,
                    "model_label": model.name if model is not None else None,
                    "owner_uid": item.owner_principal,
                    "owner_name": principal_name(service.principals, item.owner_principal),
                    "health": _health_columns(
                        registry_dto.render_deployment(item).get("health") or {}
                    )[0],
                    "share": narrowest_share(
                        [
                            endpoint_share_block(
                                endpoint,
                                tuple(grantees_by_endpoint.get(endpoint.id, ())),
                            )
                            for endpoint in _endpoints_of(item.id, endpoints)
                        ]
                    ),
                }
            )

    keys = [
        _key_record(binding)
        for binding in token_bindings
        if getattr(binding, "principal", None) == subject
    ]

    admin_principals = []
    if is_admin:
        for name in sorted(service.principals.approved_principals()):
            record = service.principals.lookup(name)
            if record is None:  # pragma: no cover
                continue
            row = {
                "uid": record.principal,
                "display_name": record.display_name or record.linux_user,
            }
            role = role_lookup(name) if role_lookup else None
            if role:
                # 角色与 scope 来自受信目录；目录里没有角色这一列，所以查不到就
                # **不写**，而不是给个默认值。
                #
                # 注意 ``role_lookup`` 必须返回**常驻**角色，不能返回用户表里的
                # 那个：提权是每条会话的状态，不是人的属性。名单上写
                # ``platform_admin`` 会宣称"这个人一直是管理员"，而新模型下他
                # 一登出就是普通成员。
                row["roles"] = [role]
                row["scopes"] = list(bff_scopes.portal_scopes_for(role))
            admin_principals.append(row)

    gateway_authorized = (
        config.gateway_attached and config.gateway_adapter != GATEWAY_ADAPTER_MOCK
    )

    # Read once. The deadline map contains *only* visible metric names and
    # source-catalogue timestamps; no private owner/UID information is added.
    metric_cards = observability.metrics(identity=principal, is_admin=is_admin)
    metric_expiries = _ops_metric_expiries(metric_cards)

    return {
        "schema_version": OPS_SCHEMA,
        "synthetic": config.synthetic,
        "state": "ready",
        "transport": {
            "mode": config.transport_mode,
            "gateway_attached": bool(config.gateway_attached),
            "synthetic": config.synthetic,
            "label": (
                "LIVE · 同源 BFF" if config.transport_mode == "live" else
                "SYNTHETIC / 本部署无生产 Gateway"
            ),
        },
        "scope": {
            "subject": subject,
            "roles": sorted(requester.roles),
            "scopes": list(principal.portal_scopes),
            "visibility": visibility,
        },
        "registry": {
            "adapter_attached": True,
            "reason": None,
            "owned": owned,
            "shared": shared,
            "all": all_rows,
            "engines": engines,
        },
        "keys": keys,
        "playground": (
            {
                "authorized": True,
                "backend": "/x/playground",
                "audited": True,
                "endpoint": "/x/playground",
                "reason": None,
            }
            if gateway_authorized
            else {
                "authorized": False,
                "backend": None,
                "audited": False,
                "endpoint": None,
                "reason": REASON_NO_AUTHORIZED_BACKEND,
            }
        ),
        "metrics": {
            "items": metric_cards,
            "my_source": None,
            "global_source": None,
            "window": config.window,
            **({"expires_at": metric_expiries} if metric_expiries else {}),
        },
        "trends": observability.trends(
            identity=principal, is_admin=is_admin, window=config.window
        ),
        "evaluation": {
            "runs": evaluation.runs(identity=principal, is_admin=is_admin),
            "submit": evaluation.submit(identity=principal, is_admin=is_admin),
        },
        "admin": (
            {"available": True, "reason": None, "principals": admin_principals}
            if is_admin
            else {
                "available": False,
                "reason": REASON_MISSING_SCOPE,
                "principals": [],
            }
        ),
        "links": links,
        "notes": _notes(
            is_admin=is_admin,
            visibility=visibility,
            observability_reason=_unavailable_reason(observability, principal),
            evaluation_reason=_unavailable_reason(evaluation, principal),
        ),
    }


def _unavailable_reason(adapter: object, identity) -> str | None:
    """adapter 是否对这个会话不可用；不可用时给出稳定原因。

    适配器**自己**说明可用性（通常是「会话缺这个模块的读 scope」）。可选的：
    detached stub 没有这个方法，那就当它一直「可用」（它本来就返回空值）。
    """
    probe = getattr(adapter, "availability", None)
    if not callable(probe):
        return None
    available, reason = probe(identity=identity)
    return None if available else (reason or "missing_required_scope")


def _notes(
    *,
    is_admin: bool,
    visibility: str,
    observability_reason: str | None = None,
    evaluation_reason: str | None = None,
) -> list[str]:
    """契约要求 notes 非空：这一页必须自己说明自己是从哪来的。"""
    notes = [
        "本页数据来自同源 BFF，数据源是 Registry 的登记库；转发层是 mock，不是生产 Gateway。",
        "权限只由服务端根据受信会话断言；请求头里的 UID / 角色一律被拒绝。",
        "GPU 指标没有已声明的 NVML 来源，因此一律 UNKNOWN，而不是 0。",
    ]
    if visibility == VISIBILITY_SHARED_PORTAL:
        notes.append("共享视图只列出**别人显式共享给我**的 Endpoint；我的部署不会消失。")
    if is_admin:
        notes.append("全局视图来自 portal:admin:read，不是名字，也不是客户端的开关。")
    else:
        notes.append("没有 portal:admin:read，因此本页不含全局视图与全员名单。")
    if observability_reason:
        notes.append(
            "观测段为空：这个会话缺少 observability:self:read（"
            f"{observability_reason}），因此服务端没有返回任何用量或曲线。"
        )
    else:
        notes.append(
            "观测数据来自合成样本；指标卡没有白名单来源（gateway-usage-export / "
            "nvml-verified / prometheus-readonly），因此一律 UNKNOWN，只有曲线带 "
            "SYNTHETIC 标记。"
        )
    if evaluation_reason:
        notes.append(
            "评测队列为空：这个会话缺少 evaluation:read（"
            f"{evaluation_reason}），因此服务端没有返回任何任务。"
        )
    else:
        notes.append("真实评测派发在本部署关闭：任务可入队，但不会有 runner 执行它。")
    return notes


__all__ = [
    "GATEWAY_ADAPTER_MOCK",
    "METRIC_SKELETON",
    "OPS_SCHEMA",
    "REASON_EVALUATION_NOT_READY",
    "REASON_MISSING_SCOPE",
    "REASON_NO_AUTHORIZED_BACKEND",
    "REASON_NO_RUNTIME_MONITOR",
    "REASON_NO_VERIFIED_SOURCE",
    "REASON_OBSERVABILITY_NOT_ATTACHED",
    "REASON_REGISTRY_NOT_ATTACHED",
    "VISIBILITIES",
    "VISIBILITY_PRIVATE_PORTAL",
    "VISIBILITY_SHARED_PORTAL",
    "DetachedEvaluationAdapter",
    "DetachedObservabilityAdapter",
    "EvaluationAdapter",
    "ObservabilityAdapter",
    "OpsConfig",
    "build_ops",
    "deployment_record",
    "shared_endpoint_record",
]
