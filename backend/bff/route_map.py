"""同源 BFF 的路由表：一张**声明式**的表，说清每条路由的边界。

在这张表出现之前，「这条路径谁能调、是不是写操作、数据从哪来」散在
``app.py`` 的一串 ``if`` 里：路由存在与否可以读出来，但**没写下来的那些**
（哪条允许匿名、哪条必须带 CSRF、哪条的数据源是服务端派的）只能靠逐行读实现。
Issue #49 要的第一件事就是把它变成可断言的事实。

表里每一行是一条 :class:`RouteSpec`，字段刻意都是**封闭取值或稳定常量**：

``auth``
    匿名还是必须有会话。**只有**登录入口与「这条路径此刻状态如何」的两条只读
    状态路由是匿名的；其余一律 ``session``。测试逐条断言匿名调用得到 401。
``kind`` / ``csrf``
    读还是写；写必须过既有的 CSRF/同源 ``_preflight``（复用 ``backend.auth``，
    不在这里重写一份 Origin 比较）。
``source``
    这条路由的数据**从哪来**。``session`` 表示它只回答「你是谁 / 这条登录路径的
    状态」；其余取值表示数据的权威在某个服务端模块。客户端**不能**声明这个值——
    见 :func:`backend.bff.identity.reject_source_assertions`，以及 ``?source=live``
    被 400 拒绝的那组测试。前端页面 URL 上的 ``?source=`` 是**页面**的取数模式，
    不是发给这些 API 的参数；把它发到这里来，是把「来源」当成可自选的东西。
``owner``
    这条路由的功能归属哪条 Lane。集成 Lane 拥有的是**接线与守卫**；被接线模块的
    行为仍归各自 Lane，本文件只声明它的存在，不改它的实现。

这张表**不是**第二套 ACL。行级权限（谁能看见哪个 deployment/endpoint）始终只在
Registry 的 ``AccessPolicy`` 里判定，模块内的 scope 也只由各模块自己的 adapter
判定；``scope`` 字段记录的是那条路由**主要的**那道门，好让测试知道该去验什么。
把「谁能看什么」抄一份到这里，正是这个仓库反复拒绝的那种「两份会漂移的规则」。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..atlas.identity import SCOPE_CALLS_SELF_READ as atlas_calls_scope
from ..evaluation.identity import SCOPE_EVAL_READ as eval_read_scope
from ..registry.identity import SCOPE_ENDPOINT_INVOKE as registry_invoke_scope
from ..registry.identity import SCOPE_REGISTRY_READ as registry_read_scope
from ..registry.identity import SCOPE_REGISTRY_WRITE as registry_write_scope
from ..observability import identity as observability_identity
from . import scopes as bff_scopes

# --- 身份要求 ---------------------------------------------------------------

AUTH_ANONYMOUS = "anonymous"
AUTH_SESSION = "session"

AUTH_LEVELS = (AUTH_ANONYMOUS, AUTH_SESSION)

# --- 读写 -------------------------------------------------------------------

KIND_READ = "read"
KIND_WRITE = "write"

KINDS = (KIND_READ, KIND_WRITE)

# --- 数据来源 ---------------------------------------------------------------

#: 这条路由只回答会话/登录路径本身的状况，不代理任何数据模块。
SOURCE_SESSION = "session"
#: 数据的权威在 Registry 的登记库与 ``AccessPolicy``。
SOURCE_REGISTRY = "registry"
#: 数据的权威在观测模块（``backend/observability``，经 Data Lane 的 adapter）。
SOURCE_OBSERVABILITY = "observability"
#: 固定来源的主机投影（Prometheus 或 NVML），服务端按
#: ``backend.observability.host_projection`` 严格校验后才下发。
SOURCE_HOST_SUMMARY = "host_summary"
#: 数据的权威在 Calls/Atlas 模块（``backend/atlas``）的 Gateway 调用元数据。
SOURCE_ATLAS = "atlas"
#: 数据的权威在评测模块（``backend/evaluation``）的报告目录。
SOURCE_EVALUATION = "evaluation"
#: 这条路由不下发任何业务数据：它回答的是**装配本身**的状态
#: （哪条来源此刻对你能说什么）。归到 ``session`` 之外单独一支，是因为它的
#: 权威既不是某个数据模块、也不只是会话 —— 它是 BFF 自己的接线事实。
SOURCE_PORTAL = "portal"
#: 数据的权威在两个**应用生成的结构化审计账本**：本层的提权审计与 Registry 的
#: ``audit_log``。它既不是原始日志文件，也不是 journal —— 那两类都由契约禁止下发。
SOURCE_AUDIT = "audit"
#: 数据的权威在集成网关（``backend/gateway``）：虚拟密钥、模型路由与它自己记的调用元数据。
SOURCE_GATEWAY = "gateway"
#: 数据的权威是「平台签发、给调用方用的模型服务凭据」。与其它来源分开，
#: 因为它是唯一会原样下发密钥的一支，读代码的人应当一眼看出来。
SOURCE_MODEL_SERVICE = "model_service"
#: 数据的权威是**正在运行的推理引擎自己**报出来的运行时状态（Strata 的
#: ``GET /metrics``）。与 ``host_summary`` 分开：那份是主机侧的投影（Prometheus /
#: NVML），这一份是引擎进程内部的槽位、排队与吞吐，只有引擎自己知道。
SOURCE_ENGINE = "engine"

SOURCES = (
    SOURCE_SESSION,
    SOURCE_REGISTRY,
    SOURCE_OBSERVABILITY,
    SOURCE_HOST_SUMMARY,
    SOURCE_ATLAS,
    SOURCE_EVALUATION,
    SOURCE_PORTAL,
    SOURCE_AUDIT,
    SOURCE_GATEWAY,
    SOURCE_MODEL_SERVICE,
    SOURCE_ENGINE,
)

# --- 归属 -------------------------------------------------------------------

LANE_IAM = "IAM#45"
LANE_DATA = "Data#47"
LANE_INTEGRATION = "Integration#49"

# --- 委派 -------------------------------------------------------------------

#: ``/auth/`` 下的其余路径原样交给既有的 ``backend.auth.routes.Application``：
#: 它是那条路径的唯一实现者，这里不复制它的路由表。
DELEGATED_AUTH_PREFIX = "/auth/"


@dataclass(frozen=True)
class RouteSpec:
    """一条同源路由的边界声明。

    ``handler`` 是 ``OpsApplication`` 上的方法名；``None`` 表示这条路径由既有
    auth 应用自己处理（``/auth/logout``）。``pattern`` 只在路径**有变量**时给出，
    形如 ``("/prefix/", "/suffix")``，此时 ``path`` 仍然写着人类可读的形状。
    """

    name: str
    methods: tuple[str, ...]
    path: str
    auth: str
    kind: str
    csrf: bool
    source: str
    owner: str
    handler: str | None
    scope: str | None = None
    pattern: tuple[str, str] | None = None
    note: str = ""

    @property
    def is_write(self) -> bool:
        return self.kind == KIND_WRITE

    @property
    def is_anonymous(self) -> bool:
        return self.auth == AUTH_ANONYMOUS

    @property
    def is_data_route(self) -> bool:
        """是不是一条**代理数据**的路由（前端不能自选来源的那一类）。"""
        return self.source != SOURCE_SESSION

    def path_matches(self, path: str) -> bool:
        if self.pattern is None:
            return path == self.path
        prefix, suffix = self.pattern
        if not path.startswith(prefix) or not path.endswith(suffix):
            return False
        middle = path[len(prefix) : len(path) - len(suffix)]
        return bool(middle) and "/" not in middle


ROUTES: tuple[RouteSpec, ...] = (
    RouteSpec(
        name="ops.read",
        methods=("GET",),
        path="/api/v2/ops",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_ops",
        # 行级 ACL 在 Registry 的 AccessPolicy 里：这条路由本身不设 scope 门槛，
        # 任何已登录成员都能读**自己**可见的那一份。
        scope=None,
        note="可见性由 Registry 判定；本层不复制行级 ACL。",
    ),
    RouteSpec(
        name="ops.usage",
        methods=("GET",),
        path="/api/v2/ops/usage",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_OBSERVABILITY,
        owner=LANE_DATA,
        handler="_usage",
        # 词的归属随那道门住在哪个模块：观测的读门槛是它自己的 scope，不是
        # portal:* 词表里的那一个。成员本来就持有这一条，所以这条路由对成员可用。
        scope=observability_identity.SCOPE_SELF_READ,
        note="完整用量 DTO；未接线时 503 observability_adapter_not_attached。",
    ),
    RouteSpec(
        name="ops.engines",
        methods=("GET",),
        path="/api/v2/ops/engines",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_OBSERVABILITY,
        owner=LANE_DATA,
        handler="_engines",
        scope=observability_identity.SCOPE_SELF_READ,
        note="引擎状态 DTO；未接线时 503 observability_adapter_not_attached。",
    ),
    RouteSpec(
        name="ops.share",
        methods=("PUT",),
        path="/api/v2/ops/deployments/{deployment_id}/share",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_share",
        scope=bff_scopes.PORTAL_ENDPOINTS_SHARE,
        pattern=("/api/v2/ops/deployments/", "/share"),
        note="共享范围变更；CSRF/同源复用既有 auth 应用的 _preflight。",
    ),
    RouteSpec(
        name="registry.engines.preflight",
        methods=("POST",),
        path="/api/v2/registry/engines/preflight",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_registry_preflight",
        scope=registry_write_scope,
        note=(
            "网页登记的只读预检（不写库）。POST 只因为它带一份表单体；"
            "身份只来自会话，所有权由 Registry 依据本机端口表判定。"
        ),
    ),
    RouteSpec(
        name="registry.engines.register",
        methods=("POST",),
        path="/api/v2/registry/engines",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_registry_register",
        scope=registry_write_scope,
        note="网页登记推理服务。owner 是会话主体（平台账户或 Linux 主体），不来自请求体。",
    ),
    RouteSpec(
        name="registry.discovery",
        methods=("GET",),
        path="/api/v2/registry/discovery",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_registry_discovery",
        scope=registry_read_scope,
        note="本机候选：回环上监听的推理服务（/proc/net/tcp + 只读 /v1/models）与授权目录里的模型。只读。",
    ),
    RouteSpec(
        name="registry.models.register",
        methods=("POST",),
        path="/api/v2/registry/models",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_registry_model_register",
        scope=registry_write_scope,
        note="网页登记模型。owner 只来自会话；权重路径只是登记引用，Registry 不打开它。",
    ),
    RouteSpec(
        name="gateway.overview",
        methods=("GET",),
        path="/api/gateway/overview",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_overview",
        scope=None,
        note="网关用量概览：本人（或管理员 view=all 全部），以及本人发布的服务被调用的情况。",
    ),
    RouteSpec(
        name="gateway.keys",
        methods=("GET",),
        path="/api/gateway/keys",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_keys",
        scope=None,
        note="本人的虚拟密钥（管理员 view=all 看全部）；只有前缀，从不下发明文。",
    ),
    RouteSpec(
        name="gateway.keys.create",
        methods=("POST",),
        path="/api/gateway/keys/create",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_key_create",
        scope=None,
        note="创建本人的虚拟密钥；明文只在这一次响应里出现。",
    ),
    RouteSpec(
        name="gateway.keys.revoke",
        methods=("POST",),
        path="/api/gateway/keys/{key_id}/revoke",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_key_revoke",
        scope=None,
        pattern=('/api/gateway/keys/', '/revoke'),
        note="吊销密钥（本人的，或管理员任意），立即生效。",
    ),
    RouteSpec(
        name="gateway.keys.update",
        methods=("POST",),
        path="/api/gateway/keys/{key_id}/update",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_key_update",
        scope=None,
        pattern=('/api/gateway/keys/', '/update'),
        note="改本人密钥的用途，或开关「记录请求内容」（只有密钥所有者本人能改）。",
    ),
    RouteSpec(
        name="gateway.models",
        methods=("GET",),
        path="/api/gateway/models",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_models",
        scope=None,
        note="网关对外发布的模型路由（共享的 + 本人的；管理员全部）。",
    ),
    RouteSpec(
        name="gateway.models.publish",
        methods=("POST",),
        path="/api/gateway/models/publish",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_model_publish",
        scope=None,
        note="把一个已登记的推理服务发布到网关（服务所有者或管理员）。",
    ),
    RouteSpec(
        name="gateway.models.update",
        methods=("POST",),
        path="/api/gateway/models/{route_id}/update",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_model_update",
        scope=None,
        pattern=('/api/gateway/models/', '/update'),
        note="修改路由：共享范围、启停、上游模型名，或删除。",
    ),
    RouteSpec(
        name="gateway.logs",
        methods=("GET",),
        path="/api/gateway/logs",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_logs",
        scope=None,
        note="本人的调用元数据，可按密钥 / 模型 / 结果筛选（管理员 view=all 全部）；不含正文。",
    ),
    RouteSpec(
        name="gateway.logs.detail",
        methods=("GET",),
        path="/api/gateway/logs/{request_id}",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_log_detail",
        scope=None,
        pattern=('/api/gateway/logs/', ''),
        note="单条请求的全量记录（本人或管理员）；正文只在该密钥打开了内容记录时才有，保留 7 天。",
    ),
    RouteSpec(
        name="gateway.playground",
        methods=("POST",),
        path="/api/gateway/playground",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_GATEWAY,
        owner=LANE_INTEGRATION,
        handler="_gateway_playground",
        scope=None,
        note="在线体验：以会话身份经网关调一次（非流式、有上限），照常记账。",
    ),
    RouteSpec(
        name="registry.engines.params",
        methods=("GET",),
        path="/api/v2/registry/engines/{engine_id}/params",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_engine_params",
        scope=registry_read_scope,
        pattern=('/api/v2/registry/engines/', '/params'),
        note="推理服务参数：按框架的参数表、实测当前值、目标配置与生效状态。",
    ),
    RouteSpec(
        name="registry.engines.params.update",
        methods=("POST",),
        path="/api/v2/registry/engines/{engine_id}/params/update",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_engine_params_update",
        scope=registry_write_scope,
        pattern=('/api/v2/registry/engines/', '/params/update'),
        note="保存目标参数（服务所有者或管理员）；返回对应的启动命令。",
    ),
    RouteSpec(
        name="ops.invoke",
        methods=("POST",),
        path="/api/v2/ops/endpoints/{endpoint_id}/invoke",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        # 权威在 Registry：行级 ACL、就绪、载荷上界、审计先于转发、Gateway 自己的
        # key/ACL 判定全在 ``RegistryService.invoke`` 里。本层只做接线。
        source=SOURCE_REGISTRY,
        owner=LANE_INTEGRATION,
        handler="_invoke_endpoint",
        # 记录**该去验什么**：真正的判定是 Registry 的 ``require_invoke_scope``。
        scope=registry_invoke_scope,
        pattern=("/api/v2/ops/endpoints/", "/invoke"),
        note=(
            "同源调用入口（SEAM-1）。身份只来自会话/scoped token；endpoint 只在路径里；"
            "载荷是有界 JSON 对象。只下发调用元数据，不下发模型正文。"
        ),
    ),
    RouteSpec(
        name="portal.infra_summary",
        methods=("GET",),
        path="/api/infra/summary",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_HOST_SUMMARY,
        owner=LANE_DATA,
        handler="_infra_summary",
        # 整机读数是共享量、有自己的一道门；那道门由适配器自己判（它知道自己的
        # scope 词表），这里记录的是「该去验什么」。
        scope=observability_identity.SCOPE_HOST_READ,
        note="固定 Prometheus/NVML 只读投影；未装配时 503，不是一份全 UNKNOWN 的 200。",
    ),
    RouteSpec(
        name="portal.model_service_key_create",
        methods=("POST",),
        path="/api/model-service/keys",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_MODEL_SERVICE,
        owner=LANE_DATA,
        handler="_model_service_key_create",
        note="签发一把新的模型推理密钥；写入引擎环境文件并触发引擎重载，因此响应里带 restart。",
    ),
    RouteSpec(
        name="portal.system_resources",
        methods=("GET",),
        path="/api/infra/system",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_HOST_SUMMARY,
        owner=LANE_DATA,
        handler="_system_resources",
        # 与 /api/infra/summary 同一道门：整机读数是共享量，不因为「换了个
        # 采集方式」就放宽。
        scope=observability_identity.SCOPE_HOST_READ,
        note="BFF 直读 /proc 的整机读数（CPU/内存/交换/磁盘/网络/按用户）；未装配时 503。",
    ),
    RouteSpec(
        name="engine.live",
        methods=("GET",),
        path="/api/v2/engine/live",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_ENGINE,
        owner=LANE_DATA,
        handler="_engine_live",
        # 引擎读数和整机读数一样是**共享量**：所有人看到的是同一份，用它不需要
        # 额外授权。这里再借一次 ``SCOPE_HOST_READ``，而不是为一处用法造一个新的
        # scope —— 多一个词条就多一处会漂移的权限声明。
        scope=observability_identity.SCOPE_HOST_READ,
        note=(
            "BFF 代取引擎 /metrics 的窄投影（槽位/排队/吞吐）。引擎凭据只在服务端，"
            "响应体里没有 key；读不到时是 200 + reachable:false，不是 503。"
        ),
    ),
    RouteSpec(
        name="portal.model_service_key",
        methods=("GET",),
        path="/api/model-service/key",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_MODEL_SERVICE,
        owner=LANE_DATA,
        handler="_model_service_key",
        note="下发给平台管理员的模型服务密钥（完整明文）；未签发时 200 + 空清单，不是 404。",
    ),
    RouteSpec(
        name="atlas.calls",
        methods=("GET",),
        path="/api/atlas/calls",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_ATLAS,
        owner=LANE_DATA,
        handler="_atlas_calls",
        scope=atlas_calls_scope,
        note="只报**本人**的调用元数据；未装配时 503 atlas_adapter_not_attached。",
    ),
    RouteSpec(
        name="evaluation.report_sources",
        methods=("GET",),
        path="/api/evaluation/report-sources",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_EVALUATION,
        owner=LANE_DATA,
        handler="_eval_report_sources",
        scope=eval_read_scope,
        note="两条来源各自的可用状态；来源隔离由评测模块自己盖章，本层不映射第二遍。",
    ),
    RouteSpec(
        name="evaluation.reports",
        methods=("GET",),
        path="/api/evaluation/reports",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_EVALUATION,
        owner=LANE_DATA,
        handler="_eval_reports",
        scope=eval_read_scope,
        note="报告目录；未知归属的记录由评测模块自己隐藏。",
    ),
    RouteSpec(
        name="evaluation.report_download",
        methods=("GET",),
        path="/api/evaluation/reports/{report_ref}/download",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_EVALUATION,
        owner=LANE_DATA,
        handler="_eval_report_download",
        scope=eval_read_scope,
        # **必须排在 ``report_detail`` 之前**：两条都是 ``/api/evaluation/reports/``
        # 前缀的模式路由。``match`` 按表序取第一个命中，而 ``report_detail`` 的
        # 后缀是空串 —— 它自己会把带 ``/`` 的中间段拒掉，今天不会吞掉这一条，
        # 但「靠另一条模式的内部检查来保证顺序」是一根会断的线。显式排前面。
        pattern=("/api/evaluation/reports/", "/download"),
        note=(
            "**第四条报告路由**（评测 Lane #78 / PR #91 交付判定，本 Lane 挂载）："
            "受限下载的**判定**，不是字节。本部署内容读取关闭 ⇒ content_read=false，"
            "不是一个 0 字节的成功下载。"
        ),
    ),
    RouteSpec(
        name="evaluation.report_detail",
        methods=("GET",),
        path="/api/evaluation/reports/{report_ref}",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_EVALUATION,
        owner=LANE_DATA,
        handler="_eval_report_detail",
        scope=eval_read_scope,
        pattern=("/api/evaluation/reports/", ""),
        note="单份报告；未授权与不存在由评测模块塌成同一个答复。",
    ),
    RouteSpec(
        name="portal.services",
        methods=("GET",),
        path="/api/portal/services",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        # 与 ``/api/infra/summary`` 同一条来源的**另一个切面**：那份投影里本来就
        # 带一段 ``services``（固定几个进程各自 up/down + 原因）。这条路由只把它
        # 单独取出来，好让前端的「服务健康」那一栏有一个稳定的地址，
        # 而不是从一份主机读数里挖字段。
        source=SOURCE_HOST_SUMMARY,
        owner=LANE_DATA,
        handler="_portal_services",
        scope=observability_identity.SCOPE_HOST_READ,
        note="固定服务就绪证据；未装配时 503，与主机投影同一条门。",
    ),
    RouteSpec(
        name="portal.events",
        methods=("GET",),
        path="/api/portal/events",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_AUDIT,
        owner=LANE_INTEGRATION,
        handler="_portal_events",
        # **操作员范围**，不是「登录即可」：跨主体审计是这个范围唯一的用途。
        # 普通成员得到 403，而不是一份过滤过的别人活动。
        scope=bff_scopes.PORTAL_ADMIN_READ,
        note=(
            "有界、脱敏的审计时间线；两条真实来源（本层提权审计 + Registry audit_log）。"
            "只下发白名单字段，两个账本的 detail 列整体丢弃。"
        ),
    ),
    RouteSpec(
        name="portal.capabilities",
        methods=("GET",),
        path="/api/portal/capabilities",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_PORTAL,
        owner=LANE_INTEGRATION,
        handler="_capabilities",
        # 它不下发业务数据，所以没有模块 scope 门槛；但每条来源**自己**的 scope
        # 决定那条来源对这条会话是 ready 还是 auth_required —— 这正是这条路由
        # 存在的理由。
        scope=None,
        note="装配状态清单。它是**拟议路由**的落地实现，不是「已上线」的声明。",
    ),
    RouteSpec(
        name="portal.integrations",
        methods=("GET",),
        path="/api/portal/integrations",
        auth=AUTH_SESSION,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_PORTAL,
        owner=LANE_INTEGRATION,
        handler="_integrations",
        scope=None,
        note="插件目录与各插件的原生界面跳转地址；看不到的地址下发 null。",
    ),
    RouteSpec(
        name="portal.integrations.update",
        methods=("PUT",),
        path="/api/portal/integrations/{key}",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_PORTAL,
        owner=LANE_INTEGRATION,
        handler="_integration_update",
        scope=bff_scopes.PORTAL_ADMIN_READ,
        pattern=("/api/portal/integrations/", ""),
        note="管理员设置 / 清除某个插件的跳转地址与可见范围。",
    ),
    RouteSpec(
        name="auth.linux.login",
        methods=("POST",),
        path="/auth/linux/login",
        # 这是登录入口：此刻**还没有**会话，所以它必须允许匿名调用——但它是写
        # 操作，因此同样要过 CSRF/同源。本构建里 PAM 后端未实现，它以 503
        # pam_unavailable 收场，而不是伪造一次成功登录。
        auth=AUTH_ANONYMOUS,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_SESSION,
        owner=LANE_IAM,
        handler="_linux_login",
        note="没有真实 PAM 后端；未实现时以 503 收场，不伪造会话。",
    ),
    RouteSpec(
        name="auth.linux.status",
        methods=("GET",),
        path="/auth/linux/status",
        auth=AUTH_ANONYMOUS,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_SESSION,
        owner=LANE_IAM,
        handler="_linux_status",
        note="公开说明：这条登录路径此刻的真实状态，不是任何人的秘密。",
    ),
    RouteSpec(
        name="auth.admin.status",
        methods=("GET",),
        path="/auth/admin/status",
        auth=AUTH_ANONYMOUS,
        kind=KIND_READ,
        csrf=False,
        source=SOURCE_SESSION,
        owner=LANE_IAM,
        handler="_admin_status",
        note="没有会话也是一个合法答案（elevated: false）。",
    ),
    RouteSpec(
        name="auth.admin.enter",
        methods=("POST",),
        path="/auth/admin/enter",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_SESSION,
        owner=LANE_IAM,
        handler="_admin_enter",
        note="提权是**每条会话**的状态；口令错误不泄露会话是否存在。",
    ),
    RouteSpec(
        name="auth.admin.exit",
        methods=("POST",),
        path="/auth/admin/exit",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_SESSION,
        owner=LANE_IAM,
        handler="_admin_exit",
        note="幂等；不要求会话仍处于提权状态。",
    ),
    RouteSpec(
        name="auth.logout",
        methods=("POST",),
        path="/auth/logout",
        auth=AUTH_SESSION,
        kind=KIND_WRITE,
        csrf=True,
        source=SOURCE_SESSION,
        owner=LANE_IAM,
        handler=None,
        note="登出由既有 auth 应用执行；成功后才清提权，顺序见 app.py。",
    ),
)

ROUTES_BY_NAME: dict[str, RouteSpec] = {spec.name: spec for spec in ROUTES}


def match(path: str) -> RouteSpec | None:
    """路径 → 路由声明。**不含方法**：方法不匹配要回 405，而不是 404。"""
    for spec in ROUTES:
        if spec.path_matches(path):
            return spec
    return None


def is_delegated(path: str) -> bool:
    """这条路径是否交给既有 auth 应用。"""
    return path.startswith(DELEGATED_AUTH_PREFIX)


def handlers() -> dict[str, str]:
    """``handler 方法名 → 路由名``。app.py 用它把表与实现绑在一起。"""
    return {spec.handler: spec.name for spec in ROUTES if spec.handler is not None}


__all__ = [
    "AUTH_ANONYMOUS",
    "AUTH_LEVELS",
    "AUTH_SESSION",
    "DELEGATED_AUTH_PREFIX",
    "KIND_READ",
    "KIND_WRITE",
    "KINDS",
    "LANE_DATA",
    "LANE_IAM",
    "LANE_INTEGRATION",
    "ROUTES",
    "ROUTES_BY_NAME",
    "SOURCES",
    "SOURCE_ATLAS",
    "SOURCE_AUDIT",
    "SOURCE_EVALUATION",
    "SOURCE_HOST_SUMMARY",
    "SOURCE_OBSERVABILITY",
    "SOURCE_PORTAL",
    "SOURCE_REGISTRY",
    "SOURCE_SESSION",
    "RouteSpec",
    "handlers",
    "is_delegated",
    "match",
]
