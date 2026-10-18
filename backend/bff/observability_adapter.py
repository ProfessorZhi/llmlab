"""``backend/observability`` ⇒ ``portal-ops/1`` 的 metrics / trends 段。

这个模块是**翻译层**，不是第二个观测实现：读数、口径、质量与原因全部来自
``backend/observability`` 的 builder（``build_self_usage_dto`` /
``build_admin_overview_dto`` / ``build_timeline_dto`` / ``build_engine_status_dto``），
本层只把它们摆进前端契约的形状。

三条必须讲清楚的规则
--------------------

1. **没有已验证来源，卡片就是 UNKNOWN。** 前端 ``contract.js`` 白名单
   ``VERIFIED_METRIC_SOURCES = {gateway-usage-export, nvml-verified, prometheus-readonly}``：
   一个**带数字**的指标必须报出它属于这三个来源之一。本部署的观测数据是合成的
   （``synthetic-fixture`` / ``mock``），**不在白名单里**——所以带数字的读数在这里
   一律降级成 ``value: null`` + ``state: unknown``，并说明原因。把合成数据标成
   ``gateway-usage-export`` 会让 UI 把一个没人量过的数字渲染成「已验证」，那正是
   本仓库一路拒绝的东西。这是一处**契约级限制**，不是本层的选择：
   指标卡槽位没有合成数的出口，曲线槽位有（见下）。
2. **曲线可以有合成数。** ``trends`` 有 ``synthetic`` 字段：为真时只要求
   ``payload.synthetic === true`` 且给出原因。所以本人/全局的时间线可以照画，
   标题上带着 ``SYNTHETIC · …``。缺测的桶不补 0——它们在序列里是 ``null``。
3. **整卡显存永远不是按人的。** ``device.*`` / ``node.*`` 属于 ``PHYSICAL_SCOPES``，
   observability 的 DTO 结构上就不带 uid；本层只在会话持有
   ``observability:host:read`` 时才请求主机读数，并且照原样透传（它本来就是
   host-wide 的共享量）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..observability import dto as obs_dto
from ..observability.errors import ContractError as ObservabilityError
from ..observability import engines as obs_engines
from ..observability import service as obs_service
from ..observability import windows as obs_windows
from ..observability.identity import (
    SCOPE_SELF_READ as obs_identity_scope,
    KeyDirectory,
    RequesterPrincipal,
)
from . import scopes as bff_scopes
from .ops import REASON_NO_VERIFIED_SOURCE

#: 前端契约认可的三个来源口径。**这个集合由前端定义**，本层只是照着读。
VERIFIED_METRIC_SOURCES = frozenset(
    {"gateway-usage-export", "nvml-verified", "prometheus-readonly"}
)

#: 曲线里的三种序列，与前端 ``TREND_KINDS`` 一致。
TREND_KINDS = ("requests", "tokens", "latency")

DEFAULT_WINDOW_HOURS = 24
DEFAULT_BUCKET = "1h"


@dataclass(frozen=True)
class ObservabilityStack:
    """接线一个观测数据面所需的一切，全部由调用方注入。

    ``gateway_text(identity)`` 返回原文 Gateway JSONL。**本层不读文件、不开
    socket**：生产里这个 callable 会去读真正的导出口，而默认实现返回空串——空串的
    诚实结局是「所有读数 unknown / no_sample」，不是 0。

    它收 ``identity`` 是因为**授权端点要按会话算**：观测模块只统计引用了「本次
    会话有权使用的 endpoint」的行（``_attribute`` 的 ``out_of_scope_endpoint``）。
    一个不含该会话端点的语料，诚实的结局就是「没有样本」——这正是不该被掩盖的
    事实。
    """

    directory: object
    #: 引擎表：可以是固定的 ``EngineRegistry``，也可以是 ``(identity) -> EngineRegistry``。
    #: 后者让本层可以从 **Registry 的可见引擎**动态建表，而不是维护第二份名单。
    engine_registry: object
    provenance: obs_dto.Provenance
    gateway_text: Callable[[object], str] = lambda _identity: ""
    engine_probe: object | None = None
    host_report: object | None = None
    limits: object | None = None

    def registry_for(self, identity) -> obs_engines.EngineRegistry:
        """本次请求用的引擎表。固定表直接返回，可调用表按主体现算。"""
        registry = self.engine_registry
        if isinstance(registry, obs_engines.EngineRegistry):
            return registry
        built = registry(identity)
        if not isinstance(built, obs_engines.EngineRegistry):
            raise ValueError("the engine_registry callable must return an EngineRegistry")
        return built

    def __post_init__(self) -> None:
        if not isinstance(self.directory, KeyDirectory):
            raise ValueError("directory must be a KeyDirectory")
        if not isinstance(self.engine_registry, obs_engines.EngineRegistry) and not callable(
            self.engine_registry
        ):
            raise ValueError(
                "engine_registry must be an EngineRegistry or a callable returning one"
            )
        if not isinstance(self.provenance, obs_dto.Provenance):
            raise ValueError("provenance must be a Provenance")
        if not callable(self.gateway_text):
            raise ValueError("gateway_text must be callable")


class ModuleObservabilityAdapter:
    """把观测模块接成 BFF 的 ``ObservabilityAdapter``。"""

    name = "observability"
    attached = True

    def __init__(
        self,
        stack: ObservabilityStack,
        *,
        endpoint_scope: Callable[[object], frozenset[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        bucket: str = DEFAULT_BUCKET,
    ) -> None:
        self.stack = stack
        #: 已授权可见的 Endpoint 集合。由 BFF 用 Registry 的 ACL 注入 —— 观测层
        #: 不自己判定「这个 endpoint 算不算你的」，它接受一个已经决定好的集合。
        self._endpoint_scope = endpoint_scope or (lambda _identity: frozenset())
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.window_hours = window_hours
        self.bucket = bucket

    # --- 主体桥接 ---------------------------------------------------------

    def requester_for(self, identity) -> RequesterPrincipal:
        """受信主体 ⇒ 观测模块的主体类型。scope 来自角色，不来自请求。"""
        return RequesterPrincipal(
            # 观测模块的 uid 是不透明字符串；这里用同一个主体名，使得 Gateway
            # key 目录（也按这个 uid 建）与 Registry 的 owner 是同一个标识。
            uid=identity.principal,
            roles=frozenset(identity.requester.roles),
            # ``effective_role``, not ``identity.user.role`` — see the note in
            # ``evaluation_adapter.requester_for``.
            scopes=bff_scopes.observability_scopes_for(identity.effective_role),
            authorized_endpoints=frozenset(self._endpoint_scope(identity)),
        )

    def _window(self) -> tuple[datetime, datetime]:
        now = self._clock()
        start = now - timedelta(hours=self.window_hours)
        # 窗口先过观测模块自己的校验：桶与跨度是它的口径，不是本层说了算。
        obs_windows.validate_window(start, now, self.bucket)
        return start, now

    def _request(self, identity, *, requested_uid: str | None = None):
        start, now = self._window()
        return (
            obs_service.ObservabilityRequest(
                principal=self.requester_for(identity),
                window_from=start,
                window_to=now,
                requested_uid=requested_uid,
                bucket=self.bucket,
            ),
            now,
        )

    # --- 可用性 -----------------------------------------------------------

    def availability(self, *, identity) -> tuple[bool, str | None]:
        """这个会话能不能读观测数据。**由 scope 判定**（角色推出），不靠试探调用。

        会话没有 ``observability:self:read`` 时整个观测段应当是空的并说明原因，
        而不是让整页 500 —— UI 分支的设计是「没有授权的已登录会话仍然是一张
        合法的、说清自己状态”的页面。
        """
        scopes = bff_scopes.observability_scopes_for(identity.effective_role)
        if obs_identity_scope in scopes:
            return True, None
        return False, "missing_required_scope"

    # --- metrics ----------------------------------------------------------

    def metrics(self, *, identity, is_admin: bool) -> list[dict]:
        """指标卡。见模块说明第 1 条：没有白名单来源，就一律 UNKNOWN。"""
        items: list[dict] = []
        seen: set[str] = set()

        try:
            readings = self._self_readings(identity, is_admin)
        except ObservabilityError:
            # 观测模块自己拒了（scope 不够等）。整段留空并让 payload 的 notes 说明。
            return []
        for reading in readings:
            metric_id = str(reading.get("metric_id") or "")
            if not metric_id or metric_id in seen:
                continue
            seen.add(metric_id)
            items.append(_metric_card(metric_id, reading))

        if is_admin and self.stack.host_report is not None:
            # 整机/整卡：只在持有 host scope 时出现，且照原样透传（host-wide）。
            for reading in self._host_readings():
                metric_id = str(reading.get("metric_id") or "")
                if not metric_id or metric_id in seen:
                    continue
                seen.add(metric_id)
                items.append(_metric_card(metric_id, reading))

        return items

    def _self_readings(self, identity, is_admin: bool) -> list[dict]:
        request, now = self._request(identity)
        text = self._text(identity)
        if is_admin:
            dto = obs_service.build_admin_overview_dto(
                request,
                text,
                self.stack.directory,
                self.stack.registry_for(identity),
                self.stack.provenance,
                now=now,
                host_report=self.stack.host_report,
                engine_probe=self.stack.engine_probe,
                **_limits_kwargs(self.stack.limits),
            )
        else:
            dto = obs_service.build_self_usage_dto(
                request,
                text,
                self.stack.directory,
                self.stack.registry_for(identity),
                self.stack.provenance,
                now=now,
                **_limits_kwargs(self.stack.limits),
            )
        metrics = dto.get("metrics") or {}
        return [value for value in metrics.values() if isinstance(value, dict)]

    def _host_readings(self) -> list[dict]:
        report = self.stack.host_report
        if report is None:
            return []
        payload = report.to_dict() if hasattr(report, "to_dict") else {}
        readings: list[dict] = []
        for group in ("node", "driver"):
            block = payload.get(group) or {}
            readings.extend(
                value for value in block.values() if isinstance(value, dict)
            )
        for device in payload.get("devices") or []:
            block = (device or {}).get("readings") or {}
            readings.extend(value for value in block.values() if isinstance(value, dict))
        return readings

    # --- trends -----------------------------------------------------------

    def trends(self, *, identity, is_admin: bool, window: str) -> dict | None:
        """时间线。合成来源时按契约带 ``synthetic: true`` + 原因。"""
        try:
            return self._trends(identity=identity, is_admin=is_admin, window=window)
        except ObservabilityError:
            return None

    def _trends(self, *, identity, is_admin: bool, window: str) -> dict | None:
        synthetic = bool(self.stack.provenance.synthetic)
        if not synthetic:
            # 非合成声明必须落在前端的白名单里，否则宁可**不给曲线**，
            # 也不把一条来路不明的线画成已测量。
            source = self.stack.provenance.source
            if source not in VERIFIED_METRIC_SOURCES:
                return None

        request, now = self._request(identity)
        text = self._text(identity)
        timeline = obs_service.build_timeline_dto(
            request,
            text,
            self.stack.directory,
            self.stack.registry_for(identity),
            self.stack.provenance,
            now=now,
            **_limits_kwargs(self.stack.limits),
        )
        start, end = request.window_from, request.window_to
        # ``build_timeline_dto`` 把桶放在 ``series`` 里（已分页），而
        # ``build_admin_overview_dto`` 把它们放在 ``buckets.items`` 里。
        series = _series_from_buckets(
            timeline.get("series") or [],
            start=start,
            end=end,
            bucket=self.bucket,
            scope="mine",
            prefix="我的",
            metrics=timeline.get("metrics") or {},
        )

        if is_admin:
            overview = obs_service.build_admin_overview_dto(
                request,
                text,
                self.stack.directory,
                self.stack.registry_for(identity),
                self.stack.provenance,
                now=now,
                host_report=self.stack.host_report,
                engine_probe=self.stack.engine_probe,
                **_limits_kwargs(self.stack.limits),
            )
            series.extend(
                _series_from_buckets(
                    (overview.get("buckets") or {}).get("items") or [],
                    start=start,
                    end=end,
                    bucket=self.bucket,
                    scope="global",
                    prefix="全局",
                    metrics={},
                )
            )

        if not series:
            return None

        block = {
            "window": window or f"{self.window_hours}h",
            "synthetic": synthetic,
            "series": series,
        }
        if synthetic:
            block["source"] = None
            block["updated_at"] = None
            block["reason"] = (
                f"本部署没有接线真实 Gateway 导出口：这条曲线来自 "
                f"{self.stack.provenance.source} 的合成样本，只用于验证渲染与交互。"
            )
        else:
            block["source"] = self.stack.provenance.source
            block["updated_at"] = obs_dto.iso_z(end)
            block["reason"] = None
        return block

    # --- 完整 DTO（供同源路由使用） ---------------------------------------

    def usage_dto(self, *, identity, scope: str = "self") -> dict:
        """完整用量 DTO。``scope='global'`` 只有管理员拿得到（模块自己会拒）。"""
        request, now = self._request(identity)
        text = self._text(identity)
        if scope == "global":
            return obs_service.build_admin_overview_dto(
                request,
                text,
                self.stack.directory,
                self.stack.registry_for(identity),
                self.stack.provenance,
                now=now,
                host_report=self.stack.host_report,
                engine_probe=self.stack.engine_probe,
                **_limits_kwargs(self.stack.limits),
            )
        return obs_service.build_self_usage_dto(
            request,
            text,
            self.stack.directory,
            self.stack.registry_for(identity),
            self.stack.provenance,
            now=now,
            **_limits_kwargs(self.stack.limits),
        )

    def engine_status_dto(self, *, identity) -> dict:
        """引擎状态 DTO。探测来源是注入的；没有探针时全部 unknown。"""
        request, now = self._request(identity)
        return obs_service.build_engine_status_dto(
            request,
            self.stack.registry_for(identity),
            self.stack.provenance,
            now=now,
            engine_probe=self.stack.engine_probe,
        )

    # --- 小工具 -----------------------------------------------------------

    def _text(self, identity) -> str:
        text = self.stack.gateway_text(identity)
        return text if isinstance(text, str) else ""


def _limits_kwargs(limits) -> dict:
    return {"limits": limits} if limits is not None else {}


def _metric_card(metric_id: str, reading: dict) -> dict:
    """一条观测读数 ⇒ 一张指标卡。见模块说明第 1 条。"""
    spec = obs_dto.CATALOGUE.get(metric_id)
    raw_value = reading.get("value")
    source = reading.get("source")
    reason = reading.get("reason")

    verified = (
        raw_value is not None
        and isinstance(source, str)
        and source in VERIFIED_METRIC_SOURCES
    )
    if verified:
        value = raw_value
        state = "available"
        updated_at = reading.get("observed_at")
        card_reason = None
    else:
        value = None
        state = "unknown"
        source = None
        updated_at = None
        # 读数本身 unknown 就报它自己的原因；有值但来源不在白名单，就说明是
        # 来源不可信——这两种「没有数字」是不同的，不能混成一句话。
        card_reason = (
            reason
            if raw_value is None
            else REASON_NO_VERIFIED_SOURCE
        ) or REASON_NO_VERIFIED_SOURCE

    unit = reading.get("unit") or (spec.unit if spec is not None else None)
    return {
        "name": metric_id,
        "label": spec.description if spec is not None else metric_id,
        "unit": unit,
        "kind": "observability",
        "value": value,
        "state": state,
        "source": source,
        "updated_at": updated_at,
        "reason": card_reason,
    }


def _series_from_buckets(
    items: list,
    *,
    start: datetime,
    end: datetime,
    bucket: str,
    scope: str,
    prefix: str,
    metrics: dict,
) -> list[dict]:
    """按观测模块的桶表折出三条序列，**缺测的桶在序列里是 null**。

    观测模块只返回「有观测的桶」；覆盖率（expected/observed）在 DTO 里单独给。
    画图需要的是等距的轴，所以这里用 ``expected_bucket_starts`` 重建轴，把没有
    观测的位置填成 ``null`` —— 那正是前端契约里「缺口就是缺口，不是 0」的语义。
    """
    observed = {
        str(item.get("start")): item for item in items if isinstance(item, dict)
    }
    axis = [
        _bucket_label(moment, bucket)
        for moment in obs_windows.expected_bucket_starts(start, end, bucket)
    ]
    if not axis:
        return []
    if not observed:
        # 一个桶都没有观测到：不是「全 0」，是「没有曲线」。
        return []

    requests: list[object] = []
    tokens: list[object] = []
    latency: list[object] = []
    for label in axis:
        item = observed.get(label)
        if item is None:
            requests.append(None)
            tokens.append(None)
            latency.append(None)
            continue
        counters = item.get("counters") or {}
        calls = counters.get("calls")
        requests.append(calls if isinstance(calls, (int, float)) and not isinstance(calls, bool) else None)
        token_block = item.get("tokens") or {}
        output = token_block.get("output_total")
        tokens.append(output if isinstance(output, (int, float)) and not isinstance(output, bool) else None)
        latency_block = item.get("latency") or {}
        p95 = latency_block.get("p95_ms")
        latency.append(p95 if isinstance(p95, (int, float)) and not isinstance(p95, bool) else None)

    p50 = _reading_value(metrics.get("principal.latency_p50_ms"))
    p95 = _reading_value(metrics.get("principal.latency_p95_ms"))
    built = [
        {"name": f"{prefix}请求", "kind": "requests", "scope": scope, "unit": "次", "points": requests},
        {"name": f"{prefix}输出 Tokens", "kind": "tokens", "scope": scope, "unit": "tokens", "points": tokens},
        {
            "name": f"{prefix} p95",
            "kind": "latency",
            "scope": scope,
            "unit": "ms",
            "points": latency,
            "p50": p50,
            "p95": p95,
        },
    ]
    # 一条**全为缺口**的序列不是「一条平线」，而是「没有这条序列」。丢掉它，
    # 而不是画一根空轴：前端对「没有测量」的既定措辞是不给图，不是给一张空的。
    # （这里会真实发生：``usage.admin`` 的桶不带 tokens，只有 ``timeline`` 带，
    #  所以全局 token 序列在模块层面就不存在。） ``_series_from_buckets`` 的
    # 调用方由此不会收到一条全 null 的曲线。
    return [item for item in built if any(point is not None for point in item["points"])]


def _reading_value(reading: object):
    if not isinstance(reading, dict):
        return None
    value = reading.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _bucket_label(moment: datetime, bucket: str) -> str:
    """与观测模块 ``_bucket_label`` 同样的口径，用于把轴对齐到桶表。"""
    return obs_dto.iso_z(moment) if bucket != "1d" else moment.date().isoformat()


def engine_registry_from_bff(*, service, identity) -> obs_engines.EngineRegistry:
    """用 **Registry 里这个会话看得见的** 引擎/端点建观测侧的引擎表。

    这是本轮唯一一处跨模块的数据桥：观测模块要一张 ``EngineRegistry``，而引擎与
    端点的权威在 Registry。映射规则刻意机械、可审查 —— 而且**以 endpoint 为键**：

    观测模块的可见性口径是「endpoint 在不在 ``EngineRegistry.endpoints()`` 里」
    （``resolve_observer_scope`` 会把它和 ``authorized_endpoints`` 取交集），所以
    每条记录都挂在一条**可见的 Endpoint** 上。早先按 engine 建键的版本会让非 owner
    的交集恒为空 —— 结果是「管理员的全局曲线一条都没有」，那不是安全，是接错线。

    * ``endpoint_id`` ：可见的 Endpoint 的 id；
    * ``engine_id`` ：该 Endpoint 的 deployment 指向的 Engine；
    * ``display_name`` ：Engine 的名字，取不到就退回 Endpoint 的名字；
    * ``visibility`` ：Endpoint 非 private ⇒ shared，否则 private；
    * ``owner_uid`` ：Endpoint 的 owner（主体名）；
    * ``replicas_declared`` ：恒为 ``None`` —— Registry 不声明副本数，本层不编。

    没有挂 Endpoint 的引擎（登记了但还没发布）仍然进表，只是对 owner 之外不可见，
    这样「引擎状态」视图不会因为缺少 Endpoint 而丢掉这台机器上真实存在的引擎。
    """
    from ..registry import errors as registry_errors

    try:
        policy = service.policy(identity.requester)
        endpoints = policy.visible_endpoints()
        deployments = {item.id: item for item in policy.visible_deployments()}
        engines = {engine.id: engine for engine in policy.visible_engines()}
    except registry_errors.RegistryError:
        # 会话连 Registry 都读不了：观测侧就没有可归属的端点，给空表。
        return obs_engines.EngineRegistry(())

    records = []
    covered: set[str] = set()
    for endpoint in endpoints:
        deployment = deployments.get(endpoint.deployment_id)
        engine = engines.get(deployment.engine_id) if deployment is not None else None
        if engine is not None:
            covered.add(engine.id)
        records.append(
            obs_engines.EngineRecord(
                engine_id=engine.id if engine is not None else endpoint.id,
                endpoint_id=endpoint.id,
                display_name=(engine.name if engine is not None else endpoint.name),
                visibility=(
                    obs_engines.VISIBILITY_SHARED
                    if endpoint.visibility != "private"
                    else obs_engines.VISIBILITY_PRIVATE
                ),
                owner_uid=endpoint.owner_principal,
                replicas_declared=None,
            )
        )
    for engine_id, engine in engines.items():
        if engine_id in covered:
            continue
        records.append(
            obs_engines.EngineRecord(
                engine_id=engine.id,
                endpoint_id=engine.id,
                display_name=engine.name,
                visibility=obs_engines.VISIBILITY_PRIVATE,
                owner_uid=engine.owner_principal,
                replicas_declared=None,
            )
        )
    return obs_engines.EngineRegistry(records)


__all__ = [
    "DEFAULT_BUCKET",
    "DEFAULT_WINDOW_HOURS",
    "TREND_KINDS",
    "VERIFIED_METRIC_SOURCES",
    "ModuleObservabilityAdapter",
    "ObservabilityStack",
    "engine_registry_from_bff",
]
