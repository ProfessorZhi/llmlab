"""Portal 模块目录契约（``portal-modules/1``，纯函数，无 IO）。

这个模块回答一个问题：**在当前这次已认证的会话下，每个模块能对这个主体说什么？**

它的答案有三种成分，缺一不可，而且三者都是**服务端判定**的：

1. **状态** —— ``available`` / ``unavailable`` / ``disabled`` / ``unknown``。
   一个模块要成为 ``available``，它在 :data:`MODULE_CATALOG` 里声明的**每一项**
   前置条件都必须成立：所需的 scope 在会话里、所需的部署事实在受信配置里。
2. **数据** —— 只包含**有已声明可信来源**的数值。没有来源的指标一律
   ``value: null`` + ``state: unknown``，绝不用 ``0`` 或一个「看起来正常」的数顶替。
3. **链接** —— 只包含此刻真的可以打开的入口。链接的前置条件与模块的前置条件是
   **两套**判定：模块 ``available`` 不代表它的每个入口都开放。

三条硬性不变量（由 :func:`validate_modules_dto` 在出口强制，见该函数）：

* **任何链接都必须是同源内部路径**（以 ``/`` 开头），且**不得**指向上游推理端口。
  这两条一起构成「不绕过 Gateway/Proxy」：页面永远不会给出一个直连上游的地址，
  因此也就不存在一条绕过鉴权与计量的路径。
* **没有可信来源的数值必须是 ``null``。** GPU 相关指标尤其如此 —— 本构建没有
  NVML 观测源，所以 ``gpu_*`` 永远是 unknown，而不是一个假的健康值。
* **``available`` 必须名副其实**：一个既没有数据也没有链接的 ``available``
  模块是自相矛盾的，出口会拒绝它。

本模块不联网、不读环境变量、不读真实日志、不启动监听。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from backend.atlas.dto import Provenance
from backend.atlas.errors import AccessDenied, ContractError, InvalidRequest
from backend.atlas.identity import SCOPE_CALLS_SELF_READ, RequesterContext

#: DTO 模式版本。与 Atlas 的两个版本都不同，字段含义不可互换。
MODULES_SCHEMA_VERSION = "portal-modules/1"

# --- scope（全部显式，role 不产生任何权限） -----------------------------------

#: 读取本模块目录的**前置条件**。它本身不打开任何入口。
SCOPE_MODULES_READ = "portal:modules:read"
#: 打开引擎原生控制台（Strata Chat / Monitor）。
#:
#: 刻意与 ``SCOPE_MODULES_READ`` **分开**：前者是「告诉我有这么个模块」，
#: 后者是「让我进到那个界面里」。两者是两次不同的授权决定。
SCOPE_ENGINES_NATIVE = "portal:engines:native"
#: 打开 EvalScope 原生界面。
SCOPE_EVALUATION_NATIVE = "portal:evaluation:native"
#: 查看整个部署范围的用户/RBAC 视图（而不是只看自己的）。
SCOPE_ADMIN_READ = "portal:admin:read"
#: 允许把 Endpoint **发布**为 Shared。缺省即 Private。
SCOPE_ENDPOINTS_SHARE = "portal:endpoints:share"
#: 打开 Grafana / Prometheus 的原生只读大盘。
SCOPE_OBSERVABILITY_NATIVE = "portal:observability:native"

#: 所有已知 scope，供自检与配置期校验。
#:
#: 包含**另一个契约的** scope（``atlas:calls:self:read``）：本 DTO 会把会话持有的
#: scope 原样回显给前端做审计，而一个真实的会话可能同时持有两个契约的 scope。
#: 自检在这里要拦的是「拼错的 scope」，不是「不属于本包的 scope」。
KNOWN_SCOPES = frozenset(
    {
        SCOPE_MODULES_READ,
        SCOPE_ENGINES_NATIVE,
        SCOPE_EVALUATION_NATIVE,
        SCOPE_ADMIN_READ,
        SCOPE_ENDPOINTS_SHARE,
        SCOPE_OBSERVABILITY_NATIVE,
        SCOPE_CALLS_SELF_READ,
    }
)

# --- 数值来源白名单 -----------------------------------------------------------

#: 一个指标数值被允许出现在页面上，当且仅当它的 ``source`` 在这个集合里
#: **且**该来源在 :class:`DeploymentFacts.attached_sources` 里被声明为已接线。
#:
#: 这是一份白名单而不是黑名单：新增一个观测源必须改这里，也就是说必须有人
#: 显式地声明「这个东西现在可信」。默认拒绝。
VERIFIED_METRIC_SOURCES = frozenset(
    {
        "gateway-usage-export",  # Gateway 导出的调用元数据
        "nvml-verified",         # 经确认可读的 NVML 观测
        "prometheus-readonly",   # 只读 Prometheus 查询
    }
)

#: GPU 指标名前缀。
GPU_METRIC_PREFIX = "gpu_"

#: 唯一被允许为 GPU 指标背书的观测来源。本构建没有接线它，所以本构建里
#: 每一个 ``gpu_*`` 都是 ``null`` —— 这条「不显示假 GPU 数字」的保证因此是一个
#: 可被测试断言的事实，而不是一句注释。
GPU_METRIC_SOURCE = "nvml-verified"

TRANSPORT_MODE_DEMO = "demo"
TRANSPORT_MODE_LIVE = "live"

VISIBILITY_PRIVATE = "private"
VISIBILITY_SHARED = "shared"
VISIBILITY_MODES = (VISIBILITY_PRIVATE, VISIBILITY_SHARED)

#: 模块状态取值。
STATE_AVAILABLE = "available"
STATE_UNAVAILABLE = "unavailable"
STATE_DISABLED = "disabled"
STATE_UNKNOWN = "unknown"
MODULE_STATES = (STATE_AVAILABLE, STATE_UNAVAILABLE, STATE_DISABLED, STATE_UNKNOWN)

#: 上游推理端口。任何链接都不得指向它 —— 那是绕过 Gateway 的直连路径。
UPSTREAM_PORTS = frozenset({"8080"})

#: 拒绝原因的稳定字符串。它们是**契约**，不是日志文案：测试断言它们，
#: 前端按它们分流。
REASON_NO_SESSION = "no_authenticated_session"
REASON_MISSING_SCOPE = "missing_required_scope"
REASON_ENGINE_NOT_STRATA = "engine_is_not_strata"
REASON_GATEWAY_NOT_ATTACHED = "gateway_not_attached"
REASON_NOT_CONFIGURED = "not_configured_in_this_deployment"
REASON_NO_AUTHORIZED_BACKEND = "no_authorized_backend"
REASON_NO_VERIFIED_SOURCE = "no_verified_metric_source"
REASON_PREVIEW_ONLY = "preview_build_has_no_real_backend"


# --- 受信输入 ----------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentFacts:
    """这个部署**实际**是什么样 —— 由运维/CLI 注入，请求里改不了。

    刻意没有任何默认值是「已接线」：``gateway_attached`` 默认 ``False``，
    ``attached_sources`` 默认空集。想让它变成 live 必须有人显式地写下来。
    """

    #: 推理引擎类别，例如 ``"strata"`` / ``"vllm"``；``None`` 表示未知。
    engine: str | None = None
    #: Gateway 是否真的接线（而不是只有个地址）。
    gateway_attached: bool = False
    #: Gateway 的**同源内部**路径前缀，例如 ``"/gw"``。不是绝对 URL。
    gateway_path: str | None = None
    #: 只读大盘的内部路径。
    grafana_path: str | None = None
    prometheus_path: str | None = None
    evalscope_path: str | None = None
    #: 是否已接线一条**经授权且受审计**的交互式推理路径。
    #:
    #: 刻意与 ``gateway_attached`` 分开：交互式试跑会被计费、会被配额与审计约束，
    #: 是**另一次**授权决定。把两者绑在一起，等于「Gateway 装了」就自动获得一个
    #: 面向所有人的交互入口 —— 那不是接线，那是放行。
    playground_backend_authorized: bool = False
    #: 已声明为**已接线**的观测来源（必须同时属于 VERIFIED_METRIC_SOURCES）。
    attached_sources: frozenset[str] = frozenset()
    #: 从已接线来源读到的指标：``name -> (value, source)``。
    metrics: Mapping[str, tuple[float, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        unknown = set(self.attached_sources) - VERIFIED_METRIC_SOURCES
        if unknown:
            raise InvalidRequest(
                f"unknown metric source(s) declared attached: {sorted(unknown)}"
            )
        for name, entry in self.metrics.items():
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise InvalidRequest(f"metric {name!r} must be a (value, source) pair")
            value, source = entry
            if source not in VERIFIED_METRIC_SOURCES:
                raise InvalidRequest(
                    f"metric {name!r} cites an unverified source {source!r}"
                )
            if source not in self.attached_sources:
                raise InvalidRequest(
                    f"metric {name!r} cites {source!r}, which is not declared attached"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidRequest(f"metric {name!r} must be a number")

    @property
    def live(self) -> bool:
        return self.gateway_attached

    def is_strata(self) -> bool:
        return (self.engine or "").strip().lower() == "strata"


@dataclass(frozen=True)
class EndpointRecord:
    """受信目录里的一条 Endpoint。``shared`` 是运维写下的，不是客户端说的。"""

    endpoint_id: str
    name: str
    principal: str
    project: str
    protocol: str
    model: str
    shared: bool = False

    def __post_init__(self) -> None:
        for value, label in (
            (self.endpoint_id, "endpoint_id"),
            (self.name, "name"),
            (self.principal, "principal"),
            (self.project, "project"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise InvalidRequest(f"an endpoint needs a non-empty {label}")


@dataclass(frozen=True)
class ModuleRequest:
    """一次模块目录查询。身份来自会话，可见性是展示口径。"""

    requester: RequesterContext
    visibility: str = VISIBILITY_PRIVATE
    endpoints: tuple[EndpointRecord, ...] = ()


# --- 模块目录（静态声明，前置条件是数据而不是分支） ---------------------------


@dataclass(frozen=True)
class ModuleSpec:
    """一个模块及其**前置条件**。

    ``requires_scopes`` 与 ``requires`` 是两套判定：前者是会话的授权，
    后者是部署的事实。两者都必须成立，模块才可能是 ``available``。
    """

    module_id: str
    title: str
    detail: str
    requires_scopes: frozenset[str] = frozenset()
    #: 需要的部署事实，取值是 :class:`DeploymentFacts` 上的谓词名，
    #: 由 :data:`FACT_CHECKS` 解释。用名字而不是 lambda 是为了让目录可被
    #: 静态检查与自检遍历。
    requires: tuple[str, ...] = ()
    #: 本构建是否**永久**没有真实后端。``True`` 的模块只能是 ``disabled`` ——
    #: 这与「配置没写对」是两件事，所以状态与原因都不同。
    preview_only: bool = False
    #: 缺失原因（未满足条件时）的默认值。
    missing_reason: str = REASON_NOT_CONFIGURED


#: 事实谓词。名字 -> 该事实是否成立。
FACT_CHECKS = {
    "gateway_attached": lambda facts: facts.gateway_attached,
    "engine_is_strata": lambda facts: facts.is_strata(),
    "grafana_configured": lambda facts: bool(facts.grafana_path),
    "prometheus_configured": lambda facts: bool(facts.prometheus_path),
    "evalscope_configured": lambda facts: bool(facts.evalscope_path),
    "has_gpu_source": lambda facts: "nvml-verified" in facts.attached_sources,
    "playground_backend_authorized": lambda facts: facts.playground_backend_authorized,
}

#: 模块目录。**新增模块必须在这里加一行**，否则前端拿不到它，
#: 而且 `validate_modules_dto` 会拒绝一个含未知模块 id 的 DTO。
MODULE_CATALOG: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        module_id="engines",
        title="引擎与部署",
        detail="推理引擎类别、部署状态与原生控制台入口。",
        requires_scopes=frozenset(),
        requires=("engine_is_strata", "gateway_attached"),
        missing_reason=REASON_ENGINE_NOT_STRATA,
    ),
    ModuleSpec(
        module_id="playground",
        title="Playground",
        detail="面向已授权模型的对话试跑台。",
        requires_scopes=frozenset(),
        requires=("playground_backend_authorized",),
        # 预览构建没有、也不会有真实推理后端：合成输出不是「试跑」，
        # 把一个预置的假回复说成「模型回答」才是这个模块最坏的失败形态。
        preview_only=True,
        missing_reason=REASON_PREVIEW_ONLY,
    ),
    ModuleSpec(
        module_id="observability",
        title="监控与日志",
        detail="只读指标与调用溯源，来源与新鲜度优先。",
        requires_scopes=frozenset(),
        requires=(),
    ),
    ModuleSpec(
        module_id="evaluation",
        title="EvalScope",
        detail="授权后进入评测任务的原始界面与报告。",
        requires_scopes=frozenset({SCOPE_EVALUATION_NATIVE}),
        requires=("evalscope_configured",),
        missing_reason=REASON_NOT_CONFIGURED,
    ),
    ModuleSpec(
        module_id="administration",
        title="视图过滤与权限边界",
        detail="服务端按会话主体算出的可见范围。",
        requires_scopes=frozenset(),
        requires=(),
    ),
)


MODULE_SPECS: dict[str, ModuleSpec] = {spec.module_id: spec for spec in MODULE_CATALOG}


# --- 授权 --------------------------------------------------------------------


def resolve_module_scope(request: ModuleRequest) -> RequesterContext:
    """校验会话与 scope，返回**已校验**的请求者上下文。

    与 Atlas 的解析器同构：类型、非空 subject、读 scope、项目白名单、可见性取值，
    任何一项不成立都抛 :class:`AccessDenied`。``role`` 依旧不产生任何权限。
    """
    if not isinstance(request, ModuleRequest):
        raise InvalidRequest("request must be a ModuleRequest")
    requester = request.requester
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a RequesterContext")

    subject = (requester.subject or "").strip()
    if not subject:
        raise AccessDenied(REASON_NO_SESSION)

    if SCOPE_MODULES_READ not in requester.scopes:
        raise AccessDenied(f"missing required scope {SCOPE_MODULES_READ}")

    if request.visibility not in VISIBILITY_MODES:
        raise InvalidRequest(f"unknown visibility mode: {request.visibility!r}")

    return requester


# --- 链接 --------------------------------------------------------------------


def _internal_link(href: str, label: str, kind: str, requires: str) -> dict:
    """构造一个内部链接。**在这里**拒绝绝对 URL 与上游端口。

    这是「不绕过 Gateway/Proxy」这条要求的落点：页面只放同源路径，所以浏览器
    不可能被引导到 127.0.0.1:8080 那类上游推理端口上去，也就没有一条绕过鉴权、
    计量与审计的通道。
    """
    if not isinstance(href, str) or not href.startswith("/"):
        raise InvalidRequest(f"a portal link must be a same-origin path: {href!r}")
    if href.startswith("//"):
        # 协议相对 URL 是绝对 URL 的另一种写法。
        raise InvalidRequest(f"a portal link must not be protocol-relative: {href!r}")
    # Deliberately a substring denylist rather than "parse the authority and
    # compare its port": a verbatim forwarding proxy spells the upstream as a
    # *path* segment (`/proxy/127.0.0.1:8080/v1`), and telling "is this port the
    # authority or part of the path?" apart is precisely where that slips
    # through. Refusing the token outright has no such edge.
    for port in UPSTREAM_PORTS:
        if f":{port}" in href:
            raise InvalidRequest(
                f"a portal link must not name an upstream inference port: {href!r}"
            )
    return {"href": href, "label": label, "kind": kind, "requires": requires}


def _port_of(href: str) -> str | None:
    authority = href.split("/", 2)[1] if href.startswith("/") else ""
    if ":" not in authority:
        return None
    return authority.rsplit(":", 1)[1]


# --- 数值 --------------------------------------------------------------------


def _metric(name: str, facts: DeploymentFacts, label: str, unit: str) -> dict:
    """一个有来源的指标，或一个诚实的 ``unknown``。

    ``gpu_*`` 走的是同一条路：本构建没有 NVML 来源，所以只会得到 unknown。
    这里没有第二条分支可以让它变成一个数字。
    """
    entry = facts.metrics.get(name)
    has_source = bool(facts.attached_sources) and entry is not None
    if name.startswith(GPU_METRIC_PREFIX) and "nvml-verified" not in facts.attached_sources:
        has_source = False
    if not has_source:
        return {
            "name": name,
            "label": label,
            "unit": unit,
            "value": None,
            "state": STATE_UNKNOWN,
            "source": None,
            "reason": REASON_NO_VERIFIED_SOURCE,
        }
    value, source = entry
    return {
        "name": name,
        "label": label,
        "unit": unit,
        "value": float(value),
        "state": STATE_AVAILABLE,
        "source": source,
        "reason": None,
    }


# --- 组装 --------------------------------------------------------------------


def _module_state(spec: ModuleSpec, requester: RequesterContext, facts: DeploymentFacts):
    """返回 ``(state, reason)``。前置条件是**全部**成立才 ``available``。"""
    missing_scopes = sorted(spec.requires_scopes - set(requester.scopes))
    if missing_scopes:
        return STATE_UNAVAILABLE, REASON_MISSING_SCOPE
    unmet = [name for name in spec.requires if not FACT_CHECKS[name](facts)]
    if unmet:
        # 「本构建永远没有真实后端」和「这次部署没配」是两件事：前者的按钮
        # 不该看起来像「配一下就能用」，所以状态是 disabled 而不是 unavailable。
        if spec.preview_only:
            return STATE_DISABLED, REASON_PREVIEW_ONLY
        if "engine_is_strata" in unmet and not facts.is_strata():
            # 引擎「未知」与引擎「不是 Strata」是两件事，不该共用一个原因。
            if not (facts.engine or "").strip():
                return STATE_UNKNOWN, REASON_NOT_CONFIGURED
            return STATE_UNAVAILABLE, REASON_ENGINE_NOT_STRATA
        if "gateway_attached" in unmet:
            return STATE_UNAVAILABLE, REASON_GATEWAY_NOT_ATTACHED
        return STATE_UNAVAILABLE, spec.missing_reason
    return STATE_AVAILABLE, None


def _module_links(spec: ModuleSpec, requester: RequesterContext, facts: DeploymentFacts) -> list[dict]:
    """只发出此刻真的能打开的入口。

    每个入口的判定是**独立**的：模块 ``available`` 并不意味着它的原生界面也开放。
    例如 Strata Chat 需要引擎是 Strata **且** Gateway 已接线 **且** 持有
    ``SCOPE_ENGINES_NATIVE``；三者缺一，这个链接就不出现。
    """
    links: list[dict] = []
    scopes = set(requester.scopes)

    if spec.module_id == "engines" and facts.is_strata() and facts.gateway_attached:
        if SCOPE_ENGINES_NATIVE not in scopes:
            links.append(
                {
                    "href": None,
                    "label": "原生 Chat / Monitor",
                    "kind": "native",
                    "requires": SCOPE_ENGINES_NATIVE,
                    "blocked": REASON_MISSING_SCOPE,
                }
            )
        elif facts.gateway_path:
            links.append(
                _internal_link(
                    f"{facts.gateway_path}/strata/chat",
                    "原生 Chat",
                    "native",
                    "engine=strata + gateway=attached + portal:engines:native",
                )
            )
            links.append(
                _internal_link(
                    f"{facts.gateway_path}/strata/monitor",
                    "原生 Monitor",
                    "native",
                    "engine=strata + gateway=attached + portal:engines:native",
                )
            )

    if spec.module_id == "observability":
        if facts.grafana_path:
            links.append(
                _internal_link(
                    facts.grafana_path, "Grafana 只读大盘", "native",
                    "portal:observability:native + grafana=configured",
                )
            )
        if facts.prometheus_path:
            links.append(
                _internal_link(
                    facts.prometheus_path, "Prometheus 查询", "native",
                    "portal:observability:native + prometheus=configured",
                )
            )

    if spec.module_id == "evaluation" and facts.evalscope_path:
        links.append(
            _internal_link(
                facts.evalscope_path, "EvalScope 原生界面", "native",
                "portal:evaluation:native + evalscope=configured",
            )
        )

    return links


def _module_data(spec: ModuleSpec, requester: RequesterContext, facts: DeploymentFacts) -> dict:
    """模块自己声明的事实块。

    有些模块的内容既不是一个外部指标、也不是一个入口，而是**服务端刚刚做出的
    判定本身** —— 例如「你这个会话能看到什么」。把它放进 ``data`` 而不是硬塞进
    ``metrics``：``metrics`` 的每一项都必须有可信观测来源，而会话授权不是观测。
    """
    if spec.module_id == "administration":
        return {
            "roles": sorted(requester.roles),
            "scopes": sorted(requester.scopes),
            "projects": sorted(requester.authorized_projects),
            "subject": requester.subject.strip(),
        }
    if spec.module_id == "engines":
        return {
            "engine": facts.engine,
            "gateway_attached": facts.gateway_attached,
            "native_console_ready": facts.is_strata() and facts.gateway_attached,
        }
    if spec.module_id == "observability":
        return {"attached_sources": sorted(facts.attached_sources)}
    if spec.module_id == "playground":
        # Only ever populated in the state in which the module is usable.
        return (
            {"backend": "authorized-gateway", "audited": True}
            if facts.playground_backend_authorized
            else {}
        )
    return {}


def _module_metrics(spec: ModuleSpec, facts: DeploymentFacts) -> list[dict]:
    if spec.module_id == "engines":
        return [
            _metric("gpu_vram_used_mib", facts, "GPU 显存占用", "MiB"),
            _metric("gpu_vram_total_mib", facts, "GPU 显存总量", "MiB"),
            _metric("gpu_utilization_pct", facts, "GPU 利用率", "%"),
        ]
    if spec.module_id == "observability":
        return [
            _metric("gateway_requests_24h", facts, "24h 调用数", "次"),
            _metric("gateway_error_rate_pct", facts, "错误率", "%"),
            _metric("gpu_vram_used_mib", facts, "GPU 显存占用", "MiB"),
        ]
    return []


def _endpoint_block(
    subject: str, visibility: str, endpoints: Iterable[EndpointRecord], requester: RequesterContext
) -> dict:
    """按会话主体算出可见 Endpoint。**默认私有。**

    ``private`` 只含 ``principal == subject`` 的记录；``shared`` 额外含**已授权项目
    内**被运维标记为 shared 的记录。客户端只能在服务端已经算好的这两个子集之间
    切换，不能做别的 —— 所以这个开关不是一个可以越权的开关。
    """
    authorized = frozenset(requester.authorized_projects)
    items = []
    for record in sorted(endpoints, key=lambda r: (r.project, r.name, r.endpoint_id)):
        mine = record.principal == subject
        shared_visible = (
            visibility == VISIBILITY_SHARED
            and record.shared
            and record.project in authorized
        )
        if not (mine or shared_visible):
            continue
        items.append(
            {
                "endpoint_id": record.endpoint_id,
                "name": record.name,
                "project": record.project,
                "protocol": record.protocol,
                "model": record.model,
                "owner": "self" if mine else "shared",
                "scope": VISIBILITY_PRIVATE if mine else VISIBILITY_SHARED,
            }
        )
    return {
        "visibility": visibility,
        "can_publish_shared": SCOPE_ENDPOINTS_SHARE in requester.scopes,
        "items": items,
    }


def build_modules_dto(
    request: ModuleRequest,
    facts: DeploymentFacts,
    provenance: Provenance,
) -> dict:
    """构建模块目录 DTO。纯函数：同输入必得同输出。"""
    if not isinstance(facts, DeploymentFacts):
        raise InvalidRequest("facts must be a DeploymentFacts")
    if not isinstance(provenance, Provenance):
        raise InvalidRequest("provenance must be a Provenance")
    if not provenance.source.strip():
        raise InvalidRequest("provenance.source must not be empty")

    requester = resolve_module_scope(request)
    subject = requester.subject.strip()
    specs = MODULE_SPECS

    modules = []
    for spec in specs.values():
        state, reason = _module_state(spec, requester, facts)
        modules.append(
            {
                "id": spec.module_id,
                "title": spec.title,
                "detail": spec.detail,
                "state": state,
                "reason": reason,
                "requires_scopes": sorted(spec.requires_scopes),
                "requires": list(spec.requires),
                "data": _module_data(spec, requester, facts),
                "metrics": _module_metrics(spec, facts),
                "links": _module_links(spec, requester, facts),
            }
        )

    modules.append(
        {
            "id": "self_calls",
            "title": "我的调用日志",
            "detail": "按 principal 精确匹配的逐条调用；见 GET /api/atlas/calls。",
            "state": STATE_AVAILABLE if SCOPE_CALLS_SELF_READ in requester.scopes else STATE_UNAVAILABLE,
            "reason": None if SCOPE_CALLS_SELF_READ in requester.scopes else REASON_MISSING_SCOPE,
            "requires_scopes": [SCOPE_CALLS_SELF_READ],
            "requires": [],
            "data": {"endpoint": "/api/atlas/calls", "view": "calls"},
            "metrics": [],
            "links": [
                _internal_link(
                    "/atlas#calls", "打开我的调用日志", "internal",
                    "atlas:calls:self:read",
                )
            ],
        }
    )
    modules.sort(key=lambda m: m["id"])

    dto = {
        "source": provenance.source,
        "synthetic": provenance.synthetic,
        "schema_version": MODULES_SCHEMA_VERSION,
        "transport": _transport_block(facts, provenance),
        "scope": {
            "subject": subject,
            "roles": sorted(requester.roles),
            "scopes": sorted(requester.scopes),
            "projects": sorted(requester.authorized_projects),
            "visibility": request.visibility,
            "visible_modules": sorted(
                module["id"] for module in modules if module["state"] == STATE_AVAILABLE
            ),
        },
        "deployment": {
            "engine": facts.engine,
            "gateway_attached": facts.gateway_attached,
            "attached_sources": sorted(facts.attached_sources),
            "native_console_ready": facts.is_strata() and facts.gateway_attached,
            # The truth value of every named precondition, so the UI can render
            # an exact readiness checklist instead of guessing from prose. It is
            # computed here rather than in the browser because "is this module
            # ready?" is a server-side judgement, not a display choice.
            "facts": {name: bool(check(facts)) for name, check in FACT_CHECKS.items()},
        },
        "endpoints": _endpoint_block(subject, request.visibility, request.endpoints, requester),
        "modules": modules,
        "display_hints": _display_hints(facts, provenance),
        "notes": _notes(),
    }
    validate_modules_dto(dto)
    return dto


def _transport_block(facts: DeploymentFacts, provenance: Provenance) -> dict:
    live = facts.live and not provenance.synthetic
    return {
        "mode": TRANSPORT_MODE_LIVE if live else TRANSPORT_MODE_DEMO,
        "gateway_attached": facts.gateway_attached,
        "synthetic": provenance.synthetic,
        "label": "LIVE / 已接线" if live else "DEMO / 合成数据",
    }


def _display_hints(facts: DeploymentFacts, provenance: Provenance) -> list[str]:
    hints = []
    if not (facts.live and not provenance.synthetic):
        hints.append("当前未接线真实 Gateway：模块状态与入口都来自演示配置，不是生产实况。")
    if provenance.synthetic:
        hints.append("SYNTHETIC / DEMO 数据：不得作为生产监控或容量决策依据。")
    if not facts.attached_sources:
        hints.append("没有已声明的可信指标来源：所有指标显示 UNKNOWN，而不是 0。")
    hints.append("GPU 显存与利用率只在已声明 NVML 来源时才显示数值；本构建没有该来源。")
    hints.append("所有入口都是同源内部路径，不提供直连上游推理端口的地址。")
    return hints


def _notes() -> list[str]:
    return [
        "模块状态由服务端判定：所需 scope 与所需部署事实**全部**成立才是 available。",
        "链接与模块分别判定：模块可用不代表它的原生界面也对你开放。",
        "Endpoint 可见性默认 Private；Shared 只在已授权项目内、且由运维标记后可见。",
        "本页不接受客户端改变可见性的请求 —— 开关只是在两个服务端算好的子集之间切换。",
    ]


def validate_modules_dto(dto: dict) -> None:
    """出口自检。每一条都对应一个具体的「看起来正常但其实在撒谎」的形态。"""
    if not isinstance(dto, dict):
        raise ContractError("modules DTO must be an object")
    for key in (
        "source", "synthetic", "schema_version", "transport", "scope", "deployment",
        "endpoints", "modules", "display_hints", "notes",
    ):
        if key not in dto:
            raise ContractError(f"modules DTO is missing required field {key!r}")
    if dto["schema_version"] != MODULES_SCHEMA_VERSION:
        raise ContractError("modules DTO schema_version mismatch")

    transport = dto["transport"]
    if transport.get("mode") not in (TRANSPORT_MODE_DEMO, TRANSPORT_MODE_LIVE):
        raise ContractError("transport.mode is not supported")
    if transport["mode"] == TRANSPORT_MODE_LIVE and (
        dto["synthetic"] or not transport.get("gateway_attached")
    ):
        raise ContractError("a synthetic or unattached deployment may not claim to be live")

    scope_block = dto["scope"]
    scopes = frozenset(scope_block.get("scopes") or [])
    subject = scope_block.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ContractError("scope.subject must be a non-empty string")
    if scope_block.get("visibility") not in VISIBILITY_MODES:
        raise ContractError("scope.visibility is not supported")
    unknown_scopes = scopes - KNOWN_SCOPES
    if unknown_scopes:
        raise ContractError(f"scope carries unknown scopes: {sorted(unknown_scopes)}")

    specs = MODULE_SPECS
    modules = dto["modules"]
    if not isinstance(modules, list) or not modules:
        raise ContractError("modules must be a non-empty list")
    seen: set[str] = set()
    for module in modules:
        module_id = module.get("id")
        if module_id in seen:
            raise ContractError(f"duplicate module id: {module_id!r}")
        seen.add(module_id)
        if module_id not in specs and module_id != "self_calls":
            raise ContractError(f"unknown module id: {module_id!r}")
        if module.get("state") not in MODULE_STATES:
            raise ContractError(f"module {module_id!r} has an unsupported state")
        declared = frozenset(module.get("requires_scopes") or [])
        missing = sorted(declared - scopes)
        if module.get("state") == STATE_AVAILABLE:
            # `available` 必须名副其实：所需 scope 已持有，且至少有一项产出。
            # 一个既无数据也无链接的 available 模块是在对读者撒谎。
            if missing:
                raise ContractError(
                    f"module {module_id!r} claims available without {missing}"
                )
            if not module["metrics"] and not module["links"] and not module.get("data"):
                raise ContractError(
                    f"module {module_id!r} claims available with nothing to show"
                )
        elif not module.get("reason"):
            raise ContractError(
                f"module {module_id!r} is not available but states no reason"
            )
        for metric in module["metrics"]:
            value = metric.get("value")
            source = metric.get("source")
            if value is None:
                if metric.get("state") != STATE_UNKNOWN:
                    raise ContractError(
                        f"metric {metric.get('name')!r} has no value and must be unknown"
                    )
                if metric.get("reason") != REASON_NO_VERIFIED_SOURCE:
                    raise ContractError(
                        f"metric {metric.get('name')!r} must say why it is unknown"
                    )
            else:
                if source not in VERIFIED_METRIC_SOURCES:
                    raise ContractError(
                        f"metric {metric.get('name')!r} cites an unverified source"
                    )
                if metric.get("state") != STATE_AVAILABLE:
                    raise ContractError(
                        f"metric {metric.get('name')!r} has a value and must be available"
                    )
            if (
                str(metric.get("name", "")).startswith(GPU_METRIC_PREFIX)
                and value is not None
                and source != GPU_METRIC_SOURCE
            ):
                # The rule is "no GPU number without the NVML source", not "no GPU
                # number ever": a deployment that really does attach NVML must be
                # able to show it, and this build (which attaches nothing) still
                # cannot. Enforcing it here rather than by construction means the
                # guarantee survives a future edit to `_metric`.
                raise ContractError(
                    f"GPU metric {metric.get('name')!r} may carry a value only when its "
                    f"source is {GPU_METRIC_SOURCE!r}"
                )
        for link in module["links"]:
            href = link.get("href")
            if href is None:
                # 「被挡住」的入口必须说明挡住它的 scope，且不得带地址。
                if not link.get("requires"):
                    raise ContractError("a blocked link must name what it requires")
                continue
            if not isinstance(href, str) or not href.startswith("/") or href.startswith("//"):
                raise ContractError(f"link {href!r} must be a same-origin path")
            if "://" in href:
                raise ContractError(f"link {href!r} must not carry a scheme")
            for port in UPSTREAM_PORTS:
                if f":{port}" in href:
                    raise ContractError(
                        f"link {href!r} names an upstream inference port"
                    )

    visible = sorted(m["id"] for m in modules if m["state"] == STATE_AVAILABLE)
    if scope_block.get("visible_modules") != visible:
        raise ContractError("scope.visible_modules disagrees with the module states")

    endpoints = dto["endpoints"]
    if endpoints.get("visibility") not in VISIBILITY_MODES:
        raise ContractError("endpoints.visibility is not supported")
    for item in endpoints["items"]:
        if item.get("scope") == VISIBILITY_SHARED and endpoints["visibility"] != VISIBILITY_SHARED:
            raise ContractError("a shared endpoint leaked into a private view")
        if item.get("scope") not in (VISIBILITY_PRIVATE, VISIBILITY_SHARED):
            raise ContractError("an endpoint needs a scope")
    if not isinstance(endpoints.get("can_publish_shared"), bool):
        raise ContractError("endpoints.can_publish_shared must be a boolean")

    for key in ("display_hints", "notes"):
        value = dto[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ContractError(f"{key} must be a list of strings")
    if not dto["notes"]:
        raise ContractError("notes must not be empty: the view must state its own contract")


__all__ = [
    "GPU_METRIC_PREFIX",
    "GPU_METRIC_SOURCE",
    "KNOWN_SCOPES",
    "MODULES_SCHEMA_VERSION",
    "MODULE_CATALOG",
    "MODULE_STATES",
    "REASON_MISSING_SCOPE",
    "REASON_NO_SESSION",
    "REASON_NO_VERIFIED_SOURCE",
    "REASON_PREVIEW_ONLY",
    "SCOPE_ADMIN_READ",
    "SCOPE_ENDPOINTS_SHARE",
    "SCOPE_ENGINES_NATIVE",
    "SCOPE_EVALUATION_NATIVE",
    "SCOPE_MODULES_READ",
    "SCOPE_OBSERVABILITY_NATIVE",
    "STATE_AVAILABLE",
    "STATE_DISABLED",
    "STATE_UNAVAILABLE",
    "STATE_UNKNOWN",
    "UPSTREAM_PORTS",
    "VERIFIED_METRIC_SOURCES",
    "VISIBILITY_MODES",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_SHARED",
    "DeploymentFacts",
    "EndpointRecord",
    "ModuleRequest",
    "ModuleSpec",
    "build_modules_dto",
    "resolve_module_scope",
    "validate_modules_dto",
]
