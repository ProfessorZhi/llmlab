"""Registry 的领域对象：Engine / Model / Deployment / Endpoint (+ ACL)。

三组状态刻意分开，因为平台简化设计把它们分成了三件事：

* **注册（registered）** —— 元数据进了库。仅此而已。
* **验证（verified / ready）** —— 一次真实的、同主机 loopback 的探测成功过。
* **共享（sharing）** —— owner 明确授权给某些人或全部已审批用户。

「注册 != 启动 != 共享」在这一层就是三个不同的字段与三条不同的判定，
不是一个 ``status`` 字符串的三种取值。**未验证的 Endpoint 不可共享、不可调用**：
``NOT_VERIFIED`` 不是警告，是拒绝。

权重路径以**引用**形式存在（``weight_path``），从不被打开 —— 见 ``weights.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .errors import InvalidRequest

# ---------------------------------------------------------------------------
# 状态取值域
# ---------------------------------------------------------------------------

#: Engine 状态。
ENGINE_REGISTERED = "registered"
ENGINE_VERIFIED = "verified"
ENGINE_UNAVAILABLE = "unavailable"
ENGINE_STATES = (ENGINE_REGISTERED, ENGINE_VERIFIED, ENGINE_UNAVAILABLE)

#: Model 状态。模型**没有**探测路径（权重是本地文件引用，Registry 不打开它），
#: 所以它只有「登记过」这一个状态 —— 这一点是设计，不是遗漏。
MODEL_REGISTERED = "registered"
MODEL_STATES = (MODEL_REGISTERED,)

#: Deployment 状态。
DEPLOYMENT_REGISTERED = "registered"
DEPLOYMENT_READY = "ready"
DEPLOYMENT_UNAVAILABLE = "unavailable"
DEPLOYMENT_STATES = (DEPLOYMENT_REGISTERED, DEPLOYMENT_READY, DEPLOYMENT_UNAVAILABLE)

#: Endpoint 状态。
ENDPOINT_REGISTERED = "registered"
ENDPOINT_READY = "ready"
ENDPOINT_UNAVAILABLE = "unavailable"
ENDPOINT_STATES = (ENDPOINT_REGISTERED, ENDPOINT_READY, ENDPOINT_UNAVAILABLE)

#: Endpoint 共享模式。默认**永远**是 ``private``。
VISIBILITY_PRIVATE = "private"
VISIBILITY_USERS = "users"
VISIBILITY_ALL_APPROVED = "all_approved_platform_users"
VISIBILITY_MODES = (VISIBILITY_PRIVATE, VISIBILITY_USERS, VISIBILITY_ALL_APPROVED)

#: 登记来源。写进每一行，让「这条元数据从哪来」可审计。
SOURCE_CLIENT_REGISTERED = "client_registered"
SOURCE_ADMIN_IMPORTED = "admin_imported"
SOURCE_FIXTURE = "synthetic_fixture"
#: 由运维策略登记进来的**既有**服务（例如那台已经在 8080 上跑着的 Strata）。
SOURCE_OPERATOR_EXISTING = "operator_existing_service"
#: 守护进程每 30 s 观测到的回环服务（§4.1）。它没有经历任何人的登记动作，
#: 所以「是谁把它放进来的」必须一眼可辨：这是 auto，不是 client_registered。
SOURCE_AUTO = "auto"

#: Engine 的**运行时**状态（``engines.state``，v4 新增）。
#:
#: 它与上面的 ``ENGINE_STATES`` 是**两条正交的轴**：``status`` 回答「登记过没有、
#: 验证过没有」，``state`` 回答「这个监听端口此刻处于发现状态机的哪一步」。
#: 刻意不复用 ``status``：把「手工登记且已验证」和「自动发现但还没探测成功」
#: 塞进同一个字符串，上层就只能靠猜。迁移回填老行为 ``attached``，是因为
#: 它们当初就是经人工确认过的运行中服务 —— 留 NULL 会让上层多出一种「未知」
#: 状态去判断，而那个状态没有任何业务含义。
ENGINE_RUNTIME_SEEN = "seen"
ENGINE_RUNTIME_HEALTHY = "healthy"
ENGINE_RUNTIME_ATTACHED = "attached"
ENGINE_RUNTIME_OFFLINE = "offline"
ENGINE_RUNTIME_STATES = (
    ENGINE_RUNTIME_SEEN,
    ENGINE_RUNTIME_HEALTHY,
    ENGINE_RUNTIME_ATTACHED,
    ENGINE_RUNTIME_OFFLINE,
)

#: 模型文件位置的状态。位置是**多对多**的：同一个模型可以在 SSD 与 HDD 上各有一份。
LOCATION_PRESENT = "present"
LOCATION_MISSING = "missing"
LOCATION_STATES = (LOCATION_PRESENT, LOCATION_MISSING)

#: 引擎 build 的存在状态。build 文件不在 ≠ build 从未登记过。
RUNTIME_BUILD_PRESENT = "present"
RUNTIME_BUILD_MISSING = "missing"
RUNTIME_BUILD_STATES = (RUNTIME_BUILD_PRESENT, RUNTIME_BUILD_MISSING)

#: 派给用户代理的任务状态。``queued`` 是**唯一**可被领取的状态，且领取是一次
#: 原子状态迁移 —— 见 ``Store.claim_task``。
AGENT_TASK_QUEUED = "queued"
AGENT_TASK_RUNNING = "running"
AGENT_TASK_DONE = "done"
AGENT_TASK_FAILED = "failed"
AGENT_TASK_STATES = (AGENT_TASK_QUEUED, AGENT_TASK_RUNNING, AGENT_TASK_DONE, AGENT_TASK_FAILED)

#: 实验室级设置。只放**跨用户**的策略，不放任何用户私有数据。
SETTING_DEFAULT_VISIBILITY = "default_visibility"
DEFAULT_VISIBILITY_PRIVATE = "private"
DEFAULT_VISIBILITY_ALL = "all"

#: Engine 的登记模式。
#:
#: * ``user`` —— 用户登记自己账户下的服务。走端口 allowlist，默认 retain
#:   ``ownership_unverified``。
#: * ``system_existing`` —— 登记一个**既有**服务（不由本平台启动）。必须命中一条
#:   运维策略 entry，且需要管理员 scope。这是 8080 唯一的合法通路。
ENGINE_MODE_USER = "user"
ENGINE_MODE_SYSTEM_EXISTING = "system_existing"
ENGINE_MODES = (ENGINE_MODE_USER, ENGINE_MODE_SYSTEM_EXISTING)

#: Endpoint 的路由同步状态。
#:
#: 一个 Endpoint 被改写（repoint）之后，**Registry 这边改了，Gateway 那边还没改**。
#: 这里不给「切换成功」这种说法：只要 router 还没按新的 generation 取过配置，
#: 就是 ``pending_router_sync``。
ROUTER_SYNC_PENDING = "pending_router_sync"
#: 运维**声明**已经切流，但没有任何外部证据：没有 Gateway 回读的 revision，
#: 也没有 snapshot SHA。它必须是**另一个词**，因为 ``docs/architecture.md`` §5.2
#: 规则 3 要求「路由应用必须有 Gateway 发布的 generation/revision 与 snapshot SHA
#: 回执」。用同一个 ``router_sync_applied`` 表示这两件事，等于让一个整数买到
#: 「已切流」四个字 —— 那正是本仓库一路拒绝的谎报。
ROUTER_SYNC_ATTESTED = "router_sync_attested"
#: 唯一带**外部证据**的「已切流」：Gateway 回读的 revision 与 snapshot SHA
#: 与本次导出的快照逐位相等。
ROUTER_SYNC_APPLIED = "router_sync_applied"
ROUTER_SYNC_STATES = (ROUTER_SYNC_PENDING, ROUTER_SYNC_ATTESTED, ROUTER_SYNC_APPLIED)
REASON_NO_ROUTER_ADAPTER = "no_router_adapter_in_this_deployment"
#: 行级事实：回执代数落后于当前 revision。一张数据库行**只知道**这件事；
#: 「本部署有没有 adapter」是部署事实，由 service 层补（见 ``router_sync_block``）。
REASON_GENERATION_LAGS_REVISION = "router_generation_lags_revision"

#: 一次 ``router_generation`` 回执的**证据类别**。缺证据的行一律按「未验证」读，
#: 不按「已切流」读 —— 迁移前落库的行没有这一列，正是靠这条规则不被提拔。
ROUTER_EVIDENCE_GATEWAY_READBACK = "gateway_readback"
ROUTER_EVIDENCE_OPERATOR_ATTESTATION = "operator_attestation"
ROUTER_EVIDENCE_KINDS = (
    ROUTER_EVIDENCE_GATEWAY_READBACK,
    ROUTER_EVIDENCE_OPERATOR_ATTESTATION,
)

#: Engine 对外的协议形态（仅作元数据，真正的调用走 Gateway）。
PROTOCOL_OPENAI_COMPAT = "openai_compatible"
PROTOCOL_HTTP_JSON = "http_json"
SUPPORTED_PROTOCOLS = (PROTOCOL_OPENAI_COMPAT, PROTOCOL_HTTP_JSON)

MAX_NAME_LENGTH = 128
MAX_TEXT_LENGTH = 512
MAX_TAGS = 16


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        raise InvalidRequest("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        moment = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise InvalidRequest(f"unparseable stored timestamp: {raw!r}") from exc
    return moment.replace(tzinfo=timezone.utc)


def validate_name(raw: object, *, what: str = "name") -> str:
    """名称校验：非空、长度受限、无控制字符、**无路径分隔符**。

    名称会出现在 URL 路径与路由配置里，所以它不能被用作目录穿越或 header 注入的载体。
    """
    if not isinstance(raw, str):
        raise InvalidRequest(f"{what} must be a string")
    candidate = raw.strip()
    if not candidate:
        raise InvalidRequest(f"{what} must not be empty")
    if len(candidate) > MAX_NAME_LENGTH:
        raise InvalidRequest(f"{what} exceeds {MAX_NAME_LENGTH} characters")
    if any(ch in candidate for ch in "/\\\x00") or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate
    ):
        raise InvalidRequest(f"{what} contains illegal characters")
    if candidate.startswith("-"):
        raise InvalidRequest(f"{what} must not start with '-'")
    return candidate


def validate_optional_text(raw: object, *, what: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidRequest(f"{what} must be a string or null")
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > MAX_TEXT_LENGTH:
        raise InvalidRequest(f"{what} exceeds {MAX_TEXT_LENGTH} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise InvalidRequest(f"{what} contains control characters")
    return candidate


def validate_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise InvalidRequest("tags must be a list of strings")
    if len(raw) > MAX_TAGS:
        raise InvalidRequest(f"at most {MAX_TAGS} tags are accepted")
    cleaned = []
    for item in raw:
        if not isinstance(item, str):
            raise InvalidRequest("tags must be strings")
        value = item.strip()
        if not value or len(value) > 32:
            raise InvalidRequest("each tag must be 1..32 characters")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise InvalidRequest("tags must not contain control characters")
        cleaned.append(value)
    return tuple(sorted(set(cleaned)))


# ---------------------------------------------------------------------------
# 资源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Engine:
    """用户自己账户下运行的一个推理服务进程的**登记**。"""

    id: str
    owner_principal: str
    owner_uid: int
    name: str
    target: dict  # NormalizedTarget.to_dict()
    protocol: str
    status: str
    health: dict  # HealthObservation.to_dict()
    source: str
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    #: 登记模式（``user`` / ``system_existing``）。既有服务与用户自建走的是两条路。
    mode: str = ENGINE_MODE_USER
    #: 所有权证明（``ownership.OwnershipProof.to_dict()``）。
    ownership: dict = field(default_factory=dict)
    #: 两根验证轴：``{"transport": ..., "protocol": ..., "verified": ...}``。
    verification: dict = field(default_factory=dict)
    #: 运行时状态（``seen`` / ``healthy`` / ``attached`` / ``offline``）。
    #: ``None`` 只应出现在「刚登记、还没被观察过」的行上，由发现循环补写。
    state: str | None = None
    pid: int | None = None
    #: 代理拉起时用的 systemd 单元名；手工登记的服务没有。
    unit: str | None = None
    #: 该服务此刻加载的模型与 build。二者都是**引用**，不是拷贝。
    model_id: str | None = None
    runtime_build_id: str | None = None
    #: 启动参数快照（守护进程生成、代理执行的那一份）。留档是为了让「这个服务
    #: 当时是用什么参数起来的」在重启后仍可回答，而不是只剩当前值。
    launch: dict = field(default_factory=dict)

    def verification_block(self) -> dict:
        """两根轴 + 一个**兼容用的**总状态 token。

        ``status`` 是「登记/可达性」层面的总判定（历史字段，DTO 里一直有）；
        ``transport`` / ``protocol`` 是它的两根**独立**轴，见 ``health.py``。
        两者放在同一个对象里，是为了让「单个查」与「列表」用同一份形状 ——
        同一个对象两种形状，是上一轮已经修过一次的毛病。
        """
        block = dict(self.verification or {})
        if self.status == ENGINE_VERIFIED:
            block["status"] = "VERIFIED"
        elif self.status == ENGINE_REGISTERED:
            block["status"] = "UNVERIFIED"
        else:
            block["status"] = "FAILED"
        return block

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "name": self.name,
            "target": dict(self.target),
            "protocol": self.protocol,
            "status": self.status,
            "registered": True,
            "verified": self.status == ENGINE_VERIFIED,
            # 「注册 != 验证 != 运行」：这三个布尔各自独立，且**没有**一个叫 running。
            "verified_transport": bool((self.verification or {}).get("transport_verified")),
            "verified_protocol": bool((self.verification or {}).get("protocol_verified")),
            "running": None,  # 由平台外的 owner-agent 负责；这里恒为未观测
            # 观测到的运行时状态，与上面的 ``running`` 是两件事：``running`` 说的是
            # 「平台管不管得着这个进程」（管不着，恒为 None），``state`` 说的是
            # 「发现循环最近看到它在不在」（attached / offline / None=还没看过）。
            "state": self.state,
            "mode": self.mode,
            "ownership": dict(self.ownership or {}),
            "verification": self.verification_block(),
            "health": dict(self.health),
            "source": self.source,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "last_seen_at": isoformat(self.last_seen_at),
            "description": self.description,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class Model:
    """一个模型权重的**受限登记引用**。

    ``weight_path`` 只在 owner 的视图里原样出现；非 owner 视图走
    ``weights.redact_weight_path``。``weight_present`` **永远**是 ``None``：
    本模块不 stat、不读取，所以「文件在不在」是**未观测**，不是 ``False``。
    """

    id: str
    owner_principal: str
    owner_uid: int
    name: str
    weight_path: str | None
    weight_path_sha256: str | None
    format: str | None
    parameter_count: str | None
    status: str
    health: dict
    source: str
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    #: 文件形态：``gguf`` / ``hf`` / ``diffusers``。``None`` = 尚未识别。
    kind: str | None = None
    #: 用途推断：chat / vision / decision / embedding / rerank / image / video / unknown。
    task: str | None = None
    #: 从文件头读出的元数据（上下文上限、层数、KV 头数……）。只是**观测结果**，
    #: 不参与授权判断，所以可以安全地随模型视图一起返回。
    facts: dict = field(default_factory=dict)
    size_bytes: int | None = None

    def to_dict(self, *, reveal_weight_path: bool) -> dict:
        from .weights import redact_weight_path, reveal_weight_path as reveal

        weight = (
            reveal(self.weight_path) if reveal_weight_path else redact_weight_path(self.weight_path)
        )
        payload = {
            "id": self.id,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "name": self.name,
            "format": self.format,
            "parameter_count": self.parameter_count,
            "status": self.status,
            "registered": True,
            "health": dict(self.health),
            "source": self.source,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "last_seen_at": isoformat(self.last_seen_at),
            "description": self.description,
            "tags": list(self.tags),
            # 本模块从不打开权重文件，「是否已加载/可加载」无从测得。
            "weight_present": None,
            "weight_path_observed": False,
            # 这段声明放在**模型自身**的 to_dict 里，而不是拼装视图的那一层：
            # 否则「单个查」有、列表里没有，同一个对象两种形状。
            "weight_reference": {
                "kind": "registered_reference_only",
                "registry_reads_weights": False,
                "note": (
                    "权重路径只是登记引用：Registry 从不 stat / 打开 / 复制权重文件，"
                    "因此 weight_present 永远是 null（未观测），不是 false。"
                ),
            },
        }
        payload.update(weight)
        return payload


@dataclass(frozen=True)
class Deployment:
    """把某个 Engine 与某个 Model **绑在一起**的一次部署登记。"""

    id: str
    owner_principal: str
    owner_uid: int
    name: str
    engine_id: str
    model_id: str
    status: str
    health: dict
    source: str
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None
    last_seen_at: datetime | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "name": self.name,
            "engine_id": self.engine_id,
            "model_id": self.model_id,
            "status": self.status,
            "registered": True,
            "ready": self.status == DEPLOYMENT_READY,
            "verified_at": isoformat(self.verified_at),
            "health": dict(self.health),
            "source": self.source,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "last_seen_at": isoformat(self.last_seen_at),
            "description": self.description,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class Endpoint:
    """对 Gateway 暴露的一个**共享单元**。默认 private。

    ``route_path`` 是**稳定 alias**：它在创建时定下，之后**永不改变** —— 换模型
    换的是 ``deployment_id``，不是 alias。这正是「换小模型而 API 路径不动」的落点。

    ``revision`` 是乐观并发控制：每次重指向 +1，调用方必须带上它读到的值，
    于是「读-改-写」之间的竞争会变成一次明确的 ``revision_conflict``，
    而不是一次静默的覆盖。
    """

    id: str
    owner_principal: str
    owner_uid: int
    name: str
    deployment_id: str
    route_path: str
    visibility: str
    status: str
    health: dict
    source: str
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None
    last_seen_at: datetime | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    #: 乐观并发的版本号。创建时为 1，每次重指向 +1。
    revision: int = 1
    #: 运维/Gateway 已经**按哪个 revision** 取过路由配置（0 = 从未取过）。
    router_generation: int = 0
    #: 上一次把 ``router_generation`` 推到当前 revision 的**证据类别**。
    #: ``None`` = 没有任何证据记录（从未回执，或迁移前落库的行）。
    router_ack_evidence: str | None = None

    @property
    def shareable(self) -> bool:
        return self.status == ENDPOINT_READY

    @property
    def router_sync_pending(self) -> bool:
        """Registry 改了、router 还没跟上。诚实的默认是「还没生效」。"""
        return self.router_generation < self.revision

    def router_sync_state(self) -> str:
        """状态只由**两个事实**推出：代数是否落后，以及回执带的是哪种证据。

        刻意不在这里判「有没有 adapter」：一张行级视图不知道本部署装了什么，
        而它一旦猜错，就会在同一份响应里和 ``meta.router_sync_report`` 打架。
        """
        if self.router_sync_pending:
            return ROUTER_SYNC_PENDING
        if self.router_ack_evidence == ROUTER_EVIDENCE_GATEWAY_READBACK:
            return ROUTER_SYNC_APPLIED
        # 代数追平了，但证据不是回读 —— 可能是运维声明，也可能是没有记录。
        # 两种都**不是**「已切流」。
        return ROUTER_SYNC_ATTESTED

    def router_sync_block(self) -> dict:
        state = self.router_sync_state()
        return {
            "state": state,
            "pending": self.router_sync_pending,
            "applied": state == ROUTER_SYNC_APPLIED,
            "applied_generation": self.router_generation,
            "current_generation": self.revision,
            "evidence": self.router_ack_evidence,
            # 行级理由：只说自己知道的那件事。部署级理由（有没有 adapter、
            # Gateway 回读为什么失败）由 service 层的视图补，见 registry._endpoint_view。
            "reason": REASON_GENERATION_LAGS_REVISION if self.router_sync_pending else None,
            "note": (
                "Registry 这边的改动要等 Router 按新 generation 取一次路由配置才生效。"
                "只有 Gateway 回读的 revision 与 snapshot SHA 与本次快照逐位相等，"
                "才叫「已切流」；运维声明的回执是 router_sync_attested，不是已切流。"
            ),
        }

    def to_dict(self, *, grantees: tuple[str, ...] = (), include_acl: bool = False) -> dict:
        payload = {
            "id": self.id,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "name": self.name,
            "deployment_id": self.deployment_id,
            "route_path": self.route_path,
            "visibility": self.visibility,
            "status": self.status,
            "registered": True,
            "ready": self.status == ENDPOINT_READY,
            "shareable": self.shareable,
            "verified_at": isoformat(self.verified_at),
            "revision": self.revision,
            "router_generation": self.router_generation,
            "router_ack_evidence": self.router_ack_evidence,
            "pending_router_sync": self.router_sync_pending,
            "router_sync": self.router_sync_block(),
            "health": dict(self.health),
            "source": self.source,
            "created_at": isoformat(self.created_at),
            "updated_at": isoformat(self.updated_at),
            "last_seen_at": isoformat(self.last_seen_at),
            "description": self.description,
            "tags": list(self.tags),
        }
        if include_acl:
            payload["acl"] = {
                "mode": self.visibility,
                "grantees": list(grantees),
                "explicit_user_grants": self.visibility == VISIBILITY_USERS,
            }
        else:
            # 非 owner 默认**不**看到名单：共享模式的形状本身就是别人的授权信息。
            payload["acl"] = None
        return payload


@dataclass(frozen=True)
class EndpointRouting:
    """一条**只追加**的路由历史：某个 revision 上的 Endpoint 指向了哪个 Deployment/Model。

    这张表回答的是「这个 alias 以前指向什么、什么时候换的、谁换的」。
    旧行**永不删除** —— 换模型不该抹掉历史，调用记录仍然指着旧模型。
    """

    id: int
    endpoint_id: str
    revision: int
    deployment_id: str
    model_id: str | None
    engine_id: str | None
    previous_deployment_id: str | None
    previous_model_id: str | None
    activated_at: datetime
    actor_principal: str
    actor_uid: int
    router_generation: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "endpoint_id": self.endpoint_id,
            "revision": self.revision,
            "deployment_id": self.deployment_id,
            "model_id": self.model_id,
            "engine_id": self.engine_id,
            "previous_deployment_id": self.previous_deployment_id,
            "previous_model_id": self.previous_model_id,
            "activated_at": isoformat(self.activated_at),
            "actor": self.actor_principal,
            "actor_uid": self.actor_uid,
            "router_generation": self.router_generation,
        }


@dataclass(frozen=True)
class AclGrant:
    """一条显式的「把某 Endpoint 给某主体」的授权。撤销即删除（并留审计）。"""

    endpoint_id: str
    grantee_principal: str
    granted_by: str
    granted_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: datetime
    actor_principal: str
    actor_uid: int
    action: str
    object_type: str
    object_id: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "at": isoformat(self.at),
            "actor": self.actor_principal,
            "actor_uid": self.actor_uid,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class ModelLocation:
    """模型文件在某台机器上的**一个**落点（model_locations）。

    单独成表而不是给 ``models.weight_path`` 加字段，是因为位置本来就是多值的：
    同盘移动、跨盘移动、复制成两份，都是同一个模型的多个 location。主键用
    ``(model_id, path)`` —— 路径是位置的天然身份，而 inode 会随复制/重写变化。
    """

    model_id: str
    path: str
    uid: int
    device: int | None = None
    inode: int | None = None
    mtime: float | None = None
    #: ``ssd`` / ``hdd``，按挂载点判定；只用于提示冷启动快慢，不参与授权。
    tier: str | None = None
    state: str = LOCATION_PRESENT
    last_seen_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "path": self.path,
            "uid": self.uid,
            "device": self.device,
            "inode": self.inode,
            "mtime": self.mtime,
            "tier": self.tier,
            "state": self.state,
            "last_seen_at": isoformat(self.last_seen_at),
        }


@dataclass(frozen=True)
class RuntimeBuild:
    """一个可执行的引擎 build（runtime_builds）。

    与 ``engines`` 的区别是刻意的：build 是**文件**，engines 是**进程**。同一个
    build 可以起多个服务；一个服务也可能在重启前换了 build。``fingerprint``
    标识二进制身份，``flags`` / ``formats`` 是 ``--help`` 解析的结果，供推荐参数
    时求交。

    代理上报的 build 没有 inode / mtime 可给（扫描器只读目录，不读 ``/proc``），
    所以那边的指纹按 ``framework + binary_path + version`` 算，见
    :mod:`reported_builds`。同一份表同时容纳两种来源，只是为了不把「磁盘上有个
    llama.cpp」这件事分两个地方说。
    """

    id: str
    owner_principal: str
    owner_uid: int
    framework: str
    binary_path: str
    version: str | None = None
    git_commit: str | None = None
    fingerprint: str = ""
    #: 该 build 实际支持的参数：``{"--ctx-size": {"type": "int", "default": 4096}}``。
    flags: dict = field(default_factory=dict)
    #: 能加载的模型格式：``("gguf",)`` / ``("hf",)`` / ``("ollama",)``。
    formats: tuple[str, ...] = ()
    #: 代理是怎么认出它的：``executable`` / ``python_package`` / ``source_tree``。
    #: 这三者对使用者的含义不同（能直接跑 / 要先进 venv / 还得编译），值得存下来。
    found_as: str | None = None
    #: ``python_package`` 所在的那个虚拟环境；其余识别方式没有这个字段。
    env: str | None = None
    state: str = RUNTIME_BUILD_PRESENT
    last_seen_at: datetime | None = None

    def to_dict(self, *, reveal_binary_path: bool) -> dict:
        """``binary_path`` 与模型权重路径同一个待遇：只给 owner 原样看。

        可执行文件路径同样是家目录结构（``/home/<user>/...``），而「实验室里有
        几个 build」这件事本身不需要那份结构。非 owner 拿到的是指纹。
        """
        from .weights import path_fingerprint

        return {
            "id": self.id,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "framework": self.framework,
            "binary_path": self.binary_path if reveal_binary_path else None,
            "binary_path_sha256": path_fingerprint(self.binary_path),
            "binary_path_redacted": not reveal_binary_path,
            "version": self.version,
            "git_commit": self.git_commit,
            "fingerprint": self.fingerprint,
            "flags": dict(self.flags),
            "formats": list(self.formats),
            "found_as": self.found_as,
            "env": self.env if reveal_binary_path else None,
            "state": self.state,
            # 「最后一次见到它」是这台 build 的保鲜期，页面据此判断该不该信。
            "last_seen_at": isoformat(self.last_seen_at),
        }


@dataclass(frozen=True)
class AgentTask:
    """派给某个用户代理去做的一件事（agent_tasks）。

    任务表是**按 uid 分桶**的，因为代理只以本人身份运行：守护进程永远不会
    替一个 uid 领另一个 uid 的任务。``queued`` → ``running`` 的迁移必须原子，
    否则两个轮询会把同一条 ``start`` 执行两次（见 ``Store.claim_task``）。
    """

    id: str
    uid: int
    kind: str
    payload: dict = field(default_factory=dict)
    requested_by: str = ""
    created_at: datetime | None = None
    state: str = AGENT_TASK_QUEUED
    result: dict | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "uid": self.uid,
            "kind": self.kind,
            "payload": dict(self.payload),
            "requested_by": self.requested_by,
            "created_at": isoformat(self.created_at),
            "state": self.state,
            "result": dict(self.result) if self.result is not None else None,
            "updated_at": isoformat(self.updated_at),
        }


#: 供服务层注入时钟，便于测试确定性。
Clock = Callable[[], datetime]


def validate_status(raw: object, allowed: tuple[str, ...], *, what: str) -> str:
    if not isinstance(raw, str) or raw not in allowed:
        raise InvalidRequest(f"unsupported {what}: {raw!r}")
    return raw


def validate_visibility(raw: object) -> str:
    if raw is None:
        return VISIBILITY_PRIVATE
    return validate_status(raw, VISIBILITY_MODES, what="visibility mode")


def validate_protocol(raw: object) -> str:
    if raw is None:
        return PROTOCOL_OPENAI_COMPAT
    return validate_status(raw, SUPPORTED_PROTOCOLS, what="protocol")
