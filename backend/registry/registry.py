"""Registry 服务层：把存储、授权、健康、网关接起来的那一处。

这一层是**唯一**同时认识「一行数据」与「谁能看这行」的地方，因此所有对外视图
都从这里产出（``api.py`` 只负责转发与包封套）。这样做的好处是可测：安全测试可以
直接调本模块，不必为了验证一条 ACL 规则而先启一个 HTTP 服务。

串起来的三条链：

* **注册链** —— ``register_*``：写元数据。未验证就是未验证。
* **验证链** —— ``verify_*``：engine 探测通过 -> deployment ready -> endpoint ready。
  每一级都要求上一级已经真的验证过（``NotVerified``），不靠用户声明。
* **共享链** —— ``share/unshare``：只在 endpoint ``ready`` 时允许，且受托人必须
  来自受信目录；``unshare`` 立即生效（删行，不是打标）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import dto, gateway as gateway_mod
from . import health as health_mod
from . import ownership as ownership_mod
from . import preflight as preflight_mod
from . import router as router_mod
from .authz import AccessPolicy, assert_no_default_private_trap
from .errors import (
    AccessDenied,
    Conflict,
    InvalidRequest,
    NotFound,
    NotVerified,
    OwnershipUnverified,
    ProtocolUnverified,
    RevisionConflict,
    UpstreamUnavailable,
)
from .gateway import REASON_GATEWAY_NOT_ATTACHED
from .health import (
    REASON_NEVER_PROBED,
    REASON_NOT_CONFIGURED,
    SOURCE_PROBE,
    HealthObservation,
    NoProtocolProber,
    ProbeResult,
    ProtocolObservation,
    build_verification,
    observation_from_probe,
    unknown,
)
from .identity import (
    FORBIDDEN_IDENTITY_FIELDS,
    SCOPE_ADMIN_MANAGE,
    PrincipalDirectory,
    RequesterContext,
    TokenDirectory,
    TrustedTokenIntrospector,
    is_account_principal,
)
from .models import (
    DEPLOYMENT_READY,
    DEPLOYMENT_REGISTERED,
    DEPLOYMENT_UNAVAILABLE,
    ENDPOINT_READY,
    ENDPOINT_REGISTERED,
    ENDPOINT_UNAVAILABLE,
    ENGINE_MODE_SYSTEM_EXISTING,
    ENGINE_MODE_USER,
    ENGINE_MODES,
    ENGINE_REGISTERED,
    ENGINE_UNAVAILABLE,
    ENGINE_VERIFIED,
    MODEL_REGISTERED,
    PROTOCOL_OPENAI_COMPAT,
    REASON_NO_ROUTER_ADAPTER,
    ROUTER_EVIDENCE_GATEWAY_READBACK,
    ROUTER_EVIDENCE_OPERATOR_ATTESTATION,
    ROUTER_SYNC_APPLIED,
    ROUTER_SYNC_ATTESTED,
    ROUTER_SYNC_PENDING,
    SOURCE_CLIENT_REGISTERED,
    SOURCE_OPERATOR_EXISTING,
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
    Clock,
    Deployment,
    Endpoint,
    Engine,
    Model,
    isoformat,
    utc_now,
    validate_name,
    validate_optional_text,
    validate_protocol,
    validate_tags,
    validate_visibility,
)
from .ownership import (
    EMPTY_POLICY,
    OWNERSHIP_OPERATOR_VERIFIED,
    OWNERSHIP_POLICIES,
    OWNERSHIP_POLICY_ADVISORY,
    OWNERSHIP_POLICY_ENFORCED,
    OWNERSHIP_PROVER_KINDS,
    OWNERSHIP_PROVER_NONE,
    REASON_OPERATOR_POLICY,
    OperatorPolicy,
    OwnershipProof,
    build_ownership_prover,
)
from .store import Store
from .urls import (
    DEFAULT_ALLOWED_PORTS,
    NormalizedTarget,
    OperatorTarget,
    validate_engine_target_url,
)
from .weights import validate_weight_path_reference, weight_path_fingerprint

#: 单次 invoke 允许的最大请求体（序列化后字节数）。这是拒绝滥用，不是限流。
MAX_INVOKE_PAYLOAD_BYTES = 64 * 1024

#: 自动路由同步回执的审计主体。它**不是** Linux 账号，也不是 ``registry:admin:manage``
#: 的持有者：它代表「RouterAdapter 读到了 Gateway 自己发布的、与本次快照逐位相等的
#: 代数与 sha256」这一条**外部证据**。``actor_uid`` 0 在这里只作「非人类主体」的占位，
#: 不是 root —— ``usage_mine`` 只按 principal 过滤，不会把这条记到任何用户名下。
SYSTEM_ROUTER_PRINCIPAL = "system:router-sync"
SYSTEM_ROUTER_UID = 0

#: 登记来源标签的契约 token。
SOURCE_REGISTRY_API = "registry_api"


@dataclass(frozen=True)
class RegistryConfig:
    """部署形态与安全参数。**没有隐含的开放默认值**。

    ``mode`` 默认 ``synthetic``：本仓库此刻没有生产部署，默认值必须是最保守、
    最诚实的那一个。

    两个**默认关闭**的安全开关各自都有一个具体理由，且都写在
    ``docs/registry-cli-contract.md`` 里：

    * ``ownership_policy`` 默认 ``advisory``：``backend/bff`` 用默认配置构造服务
      并调用 ``share_endpoint``，而本模块不允许改它。把缺省改成 ``enforced``
      会静默改掉一个不属于本模块的对外路径。
    * ``require_protocol_verification`` 默认 ``False``：同理，且历史行为是
      「传输可达即 ready」。

    生产使用 :meth:`production_posture`，它把两档都打开。
    """

    mode: str = dto.MODE_SYNTHETIC
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS
    allow_https_targets: bool = False
    allow_localhost_name: bool = True
    source: str = "registry_store"
    probe_timeout_seconds: float = 1.5
    #: 运维策略：哪些**既有**服务可以被登记（见 ``ownership.OperatorPolicy``）。
    operator_policy: OperatorPolicy = EMPTY_POLICY
    #: 所有权强制：``advisory``（记录）/ ``enforced``（未证明即拒绝）。
    ownership_policy: str = OWNERSHIP_POLICY_ADVISORY
    #: 是否把「协议验证」当作 ready 的必要条件（另一根轴见 ``health``）。
    require_protocol_verification: bool = False
    #: 用哪个所有权 prover（未显式注入 ``ownership_prover`` 时生效）：
    #: ``none``（**缺省**，fail-closed，永远 ``ownership_unverified``）或
    #: ``proc_net``（读全员可读的 ``/proc/net/tcp(6)`` 的 uid 列，见
    #: ``ownership.ProcNetOwnershipProver``；只经 fixture 单测验证，真实主机验证待做）。
    #: 缺省不变，是为了让既有部署与测试的行为**逐字节**不变 —— 这是一个显式的选择。
    ownership_prover_kind: str = OWNERSHIP_PROVER_NONE
    #: ``engine_id -> {"file": ..., "var": ...}``：Gateway 快照里每个 engine 引用
    #: 上游凭据的**位置**（只点名文件与变量，绝不携带值）。缺省空 —— 于是缺省
    #: 下没有任何 engine 会被发布，这正是 fail-closed 该有的样子：不配置就不发。
    engine_secret_refs: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in dto.MODES:
            raise InvalidRequest(f"unknown registry mode: {self.mode!r}")
        if not self.allowed_ports:
            raise InvalidRequest("allowed_ports must not be empty")
        for port in self.allowed_ports:
            if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
                raise InvalidRequest(f"invalid port in allowlist: {port!r}")
        if self.ownership_policy not in OWNERSHIP_POLICIES:
            raise InvalidRequest(f"unknown ownership policy: {self.ownership_policy!r}")
        if self.ownership_prover_kind not in OWNERSHIP_PROVER_KINDS:
            raise InvalidRequest(
                f"unknown ownership prover kind: {self.ownership_prover_kind!r}"
            )
        if not isinstance(self.operator_policy, OperatorPolicy):
            raise InvalidRequest("operator_policy must be an OperatorPolicy")
        if not isinstance(self.engine_secret_refs, dict):
            raise InvalidRequest("engine_secret_refs must be a mapping")

    @property
    def synthetic(self) -> bool:
        return self.mode == dto.MODE_SYNTHETIC

    @property
    def ownership_enforced(self) -> bool:
        return self.ownership_policy == OWNERSHIP_POLICY_ENFORCED

    @classmethod
    def production_posture(cls, **overrides: object) -> RegistryConfig:
        """生产姿态：两根轴都要**证明过**才叫 ready。

        ``advisory`` 与 ``require_protocol_verification=False`` 是为了不破坏
        既有调用方而留的兼容缺省；生产不该用它们。这个类方法把「该打开的都打开」
        收敛成一处，免得部署时漏掉其中一条。
        """
        base: dict = {
            "ownership_policy": OWNERSHIP_POLICY_ENFORCED,
            "require_protocol_verification": True,
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _fingerprint(payload: dict) -> str:
    """请求内容的稳定指纹：用于区分「重放」与「真冲突」。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _idempotency_key(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidRequest("idempotency key must be a string")
    key = raw.strip()
    if not key:
        return None
    if len(key) > 200:
        raise InvalidRequest("idempotency key is too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise InvalidRequest("idempotency key contains control characters")
    return key


def _reject_identity_fields(payload: dict) -> None:
    """登记类请求里出现身份字段 -> 直接拒绝（同 ``identity.reject_identity_assertions``）。

    这个 helper 只在**服务层**的登记/写动作上调用；它**不是** invoke 的保护层。
    invoke 的保护在传输层：``api._dispatch`` 在认证之后对所有路由（含 invoke）
    调用 ``identity.reject_identity_assertions``，它对顶层字段与禁用头命中即 400。
    所以「invoke 可以带顶层 ``role``」的说法并不成立 —— 经 HTTP 的 invoke 一样会被
    拒。这里曾经写着相反的意思；保留它只会让下一个人以为那条缝隙存在。
    """
    offending = sorted(set(payload.keys()) & FORBIDDEN_IDENTITY_FIELDS)
    if offending:
        raise InvalidRequest(
            "identity must not be asserted by the client; "
            f"remove field(s): {', '.join(offending)}"
        )


class RegistryService:
    """平台 Registry 的用例层。"""

    def __init__(
        self,
        *,
        store: Store,
        tokens: TokenDirectory | TrustedTokenIntrospector,
        principals: PrincipalDirectory,
        config: RegistryConfig | None = None,
        prober: object | None = None,
        gateway: object | None = None,
        protocol_prober: object | None = None,
        ownership_prover: object | None = None,
        router: object | None = None,
        port_checker: Callable[[NormalizedTarget], tuple[bool, str]] | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.store = store
        self.tokens = tokens
        self.principals = principals
        self.config = config or RegistryConfig()
        #: ``None`` 表示**本部署没有任何观测源** —— 于是没有东西能变成 verified。
        self.prober = prober
        #: 协议轴。缺省是「说不出来」，不是「大概是」。
        self.protocol_prober = protocol_prober if protocol_prober is not None else NoProtocolProber()
        #: 所有权证明。显式注入的优先；否则按 ``config.ownership_prover_kind`` 选，
        #: 其缺省 ``none`` **永远**给 ``ownership_unverified``。
        self.ownership_prover = (
            ownership_prover
            if ownership_prover is not None
            else build_ownership_prover(self.config.ownership_prover_kind)
        )
        #: 登记前检查（``preflight_engine``）的「端口能不能连上」。显式注入的优先；
        #: 否则**只有**本部署挂了传输 prober（即允许本机回环探测）时才用真实的
        #: TCP connect —— 没有观测源的部署不会因为 preflight 而开出 socket。
        if port_checker is not None:
            self.port_checker: Callable[[NormalizedTarget], tuple[bool, str]] | None = port_checker
        elif self.prober is not None:
            timeout = self.config.probe_timeout_seconds
            self.port_checker = lambda target: preflight_mod.tcp_connect_check(
                target, timeout_seconds=timeout
            )
        else:
            self.port_checker = None
        self.gateway = gateway if gateway is not None else gateway_mod.DetachedGatewayAdapter()
        #: 路由同步。缺省 ``DetachedRouterAdapter``：不写任何 Gateway，repoint 之后
        #: 状态诚实停在 ``pending_router_sync`` —— 与「本仓库不写 Gateway」一字不差。
        self.router = router if router is not None else router_mod.DetachedRouterAdapter()
        self._clock = clock

    # -- 运维策略与两根轴 --------------------------------------------------

    @property
    def operator_policy(self) -> OperatorPolicy:
        return self.config.operator_policy

    def _operator_entry_for(self, target: NormalizedTarget) -> OperatorTarget | None:
        return self.operator_policy.match(target)

    def _prove_ownership(
        self,
        target: NormalizedTarget,
        *,
        entry: OperatorTarget | None,
        owner_linux_user: str | None = None,
    ) -> OwnershipProof:
        """所有权结论的**唯一**来源，按可信度排序。

        运维承认的既有服务**优先**：它是运维写下的记录，比任何本机启发式都硬。
        否则交给注入的 prover —— 缺省那个永远说不出来。

        ``owner_linux_user`` 只来自受信目录（登记者的 ``RequesterContext`` 或
        engine owner 在 ``PrincipalDirectory`` 里的那一行），从不来自请求体。
        """
        if entry is not None:
            return OwnershipProof(
                state=OWNERSHIP_OPERATOR_VERIFIED,
                method=REASON_OPERATOR_POLICY,
                detail=f"admitted by operator policy entry {entry.label!r}",
                source=entry.source,
            )
        proof = self.ownership_prover.prove(target, owner_linux_user=owner_linux_user)
        if not isinstance(proof, OwnershipProof):
            raise InvalidRequest("ownership_prover must return an OwnershipProof")
        return proof

    def _owner_linux_user(self, principal: str) -> str | None:
        """engine owner 的 Linux 用户名：**只**从受信目录查，查不到就是 ``None``。

        平台账户返回管理员设定的 ``run_as``（可能为 ``None``，即「只观测」）。
        """
        found = self.principals.lookup(principal)
        return found.ownership_user if found is not None else None

    def _ownership_for_owner(
        self,
        principal: str,
        target: NormalizedTarget,
        *,
        entry: OperatorTarget | None,
        fallback_linux_user: str | None = None,
    ) -> OwnershipProof:
        """按 owner 的种类选证明方式，并施加「一个目标只归一个主体」。

        * Linux 主体：监听 uid 必须是这个主体的 uid（历史行为）；
        * 平台账户、管理员限定了 ``run_as``：监听 uid 必须是那个 Linux 用户；
        * 平台账户、未限定：只记录观测到的那一个运行 uid（``runtime_observed``）。

        无论哪种，同一个回环目标已被**另一个**主体登记时一律不算证明过
        （``target_registered_by_another_account``）：账户不绑定 Linux 之后，这条
        才是防止「把别人跑着的服务登记到自己名下」的那道门。
        """
        if not is_account_principal(principal):
            return self._prove_ownership(
                target,
                entry=entry,
                # 登记/预检用请求者自己的受信用户名（历史行为）；重验用目录里 owner 那一行。
                owner_linux_user=fallback_linux_user or self._owner_linux_user(principal),
            )
        taken_by = self._target_owner_other_than(principal, target)
        if taken_by is not None:
            return OwnershipProof(
                state=ownership_mod.OWNERSHIP_UNVERIFIED,
                method=ownership_mod.REASON_TARGET_TAKEN,
                detail=f"{target.host}:{target.port} is already registered by another principal",
                reason=ownership_mod.REASON_TARGET_TAKEN,
            )
        found = self.principals.lookup(principal)
        if entry is not None or (found is not None and found.run_as):
            return self._prove_ownership(
                target, entry=entry, owner_linux_user=found.run_as if found is not None else None
            )
        observe = getattr(self.ownership_prover, "observe", None)
        if observe is None:
            return ownership_mod.unverified_proof()
        proof = observe(target)
        if not isinstance(proof, OwnershipProof):
            raise InvalidRequest("ownership_prover.observe must return an OwnershipProof")
        return proof

    def _target_owner_other_than(self, principal: str, target: NormalizedTarget) -> str | None:
        for engine in self.store.list_engines():
            other = engine.target or {}
            if engine.owner_principal != principal and (
                other.get("host"), other.get("port")
            ) == (target.host, target.port):
                return engine.owner_principal
        return None

    def _observe_protocol(self, target: NormalizedTarget, protocol: str) -> ProtocolObservation:
        observation = self.protocol_prober.probe(target, protocol)
        if not isinstance(observation, ProtocolObservation):
            raise InvalidRequest("protocol_prober must return a ProtocolObservation")
        return observation

    def _verification(
        self, target: NormalizedTarget, protocol: str
    ) -> tuple[HealthObservation | None, ProtocolObservation, dict]:
        """跑两根轴，返回 ``(传输观测, 协议观测, 合并结论)``。

        协议轴**始终**跑（这样 DTO 里永远有话说），但是否参与 ``verified``
        由 ``require_protocol_verification`` 决定。
        """
        transport = self._observe(target)
        protocol_obs = self._observe_protocol(target, protocol)
        merged = build_verification(
            transport=transport,
            protocol=protocol_obs,
            require_protocol=self.config.require_protocol_verification,
        )
        return transport, protocol_obs, merged

    def _ownership_gate(self, engine: Engine, *, action: str) -> None:
        """``enforced`` 档下：没有所有权证明就不许共享/调用。"""
        if not self.config.ownership_enforced:
            return
        if (engine.ownership or {}).get("verified"):
            return
        raise OwnershipUnverified(
            f"engine {engine.id!r} is "
            f"{(engine.ownership or {}).get('state', ownership_mod.OWNERSHIP_UNVERIFIED)!r}; "
            f"{action} requires a verified engine owner (ownership_policy=enforced)"
        )

    def _protocol_gate(self, engine: Engine, *, action: str) -> None:
        """要求协议已验证时才拦 —— 否则只如实报告，不升级成拒绝。"""
        if not self.config.require_protocol_verification:
            return
        if (engine.verification or {}).get("protocol_verified"):
            return
        raise ProtocolUnverified(
            f"engine {engine.id!r} transport is reachable but its protocol is not verified; "
            f"{action} requires protocol verification"
        )

    def _require_route_applied(self, endpoint: Endpoint) -> None:
        """调用前的**唯一权威**路由门：只有真实回读确认过的路由才可转发。

        ``docs/architecture.md`` §5.2 规则 3 把「路由应用」的必要条件写成
        「Gateway 发布的 generation/revision 与 snapshot SHA 回执」。一条
        ``status == ready`` 只说明**Endpoint 验证过**，不说明**路由生效了**；
        运维的一句 ``{"generation": N}`` 也只说明**运维说**它生效了。两者都
        不能成为把用户请求送到 Gateway 的依据。

        逐条拒绝（全部在 ``gateway.forward`` **之前**）：

        * ``pending_router_sync`` —— 代数落后（从未回执、刚 repoint、迟到）；
        * ``router_sync_attested`` —— 人工声明，没有 Gateway 回读；
        * 证据不是 ``gateway_readback`` —— 纵深防御，防止一行被别的路径写坏；
        * 本部署没有装配 RouterAdapter —— 没有东西能确认；
        * Router **此刻**回读的代数与这条 Endpoint 的 revision 不一致
          （``applied_revision()``）—— DB 说是第 2 代、Gateway 说是第 1 代时，
          线上跑的不是这条路由；
        * 这条 Endpoint 不在**当前可导出的快照**里 —— ``build_routing_snapshot``
          排除了它（engine 没有 secret_ref、engine/ deployment / model 缺失、
          owner 不在受信目录），Gateway 从来没有收到过它。

        拒绝用 :class:`NotVerified`（``409 registry.not_verified``），与「未验证的
        Endpoint 不可调用」同一个契约错误族：它不是权限问题，也不是「不存在」，
        而是「现在还不能用」。消息只含状态词，不含任何凭据或他人信息。
        """
        state = endpoint.router_sync_state()
        if state != ROUTER_SYNC_APPLIED:
            raise NotVerified(
                f"endpoint {endpoint.id!r} route is not applied (state={state!r}); "
                "invoke requires a Gateway readback-confirmed revision"
            )
        if endpoint.router_ack_evidence != ROUTER_EVIDENCE_GATEWAY_READBACK:
            raise NotVerified(
                f"endpoint {endpoint.id!r} route has no Gateway readback evidence"
            )

        adapter = self.router
        if not bool(getattr(adapter, "attached", False)):
            raise NotVerified(
                "this deployment has no attached RouterAdapter; "
                "no route can be confirmed as applied"
            )
        applied = getattr(adapter, "applied_revision", lambda: None)()
        if not isinstance(applied, int) or isinstance(applied, bool) or applied != endpoint.revision:
            raise NotVerified(
                f"endpoint {endpoint.id!r} is at revision {endpoint.revision}, but the "
                f"router currently reports {applied!r}; refusing to forward a stale route"
            )

        published = {
            entry["endpoint_id"] for entry in self._routing_snapshot()["endpoints"]
        }
        if endpoint.id not in published:
            raise NotVerified(
                f"endpoint {endpoint.id!r} is not in the currently publishable routing "
                "snapshot; the Gateway was never told about this route"
            )

    def _gate_endpoint_backend(self, policy: AccessPolicy, endpoint: Endpoint, *, action: str) -> None:
        """共享/调用一个 Endpoint 之前，对它的**后端引擎**过两根轴。

        只在策略打开时拦（``advisory`` / ``require_protocol=False`` 是兼容缺省）。
        两个开关都关着时这里**一个查询都不做** —— 既有路径的开销与行为都不变。
        """
        if not (self.config.ownership_enforced or self.config.require_protocol_verification):
            return
        deployment = policy.require_deployment_read(endpoint.deployment_id)
        engine = policy.require_engine_read(deployment.engine_id)
        self._ownership_gate(engine, action=action)
        self._protocol_gate(engine, action=action)

    # -- 内部工具 ---------------------------------------------------------

    def _now(self):
        moment = self._clock()
        if moment.tzinfo is None:
            raise InvalidRequest("clock must return a timezone-aware datetime")
        return moment

    def policy(self, requester: RequesterContext) -> AccessPolicy:
        if not isinstance(requester, RequesterContext):
            raise InvalidRequest("requester must be a RequesterContext")
        return AccessPolicy(requester, self.store, self.principals)

    def _scope_block(self, requester: RequesterContext) -> dict:
        return dto.scope_block(
            principal=requester.principal,
            uid=requester.uid,
            scopes=requester.scopes,
            roles=requester.roles,
            is_admin_read=requester.is_admin_read,
            is_admin_manage=requester.is_admin_manage,
        )

    def _envelope(self, data: object, requester: RequesterContext, *, notes=(), meta=None) -> dict:
        return dto.envelope(
            data,
            mode=self.config.mode,
            source=self.config.source,
            notes=notes,
            scope=self._scope_block(requester),
            extra_meta=meta,
        )

    def _audit(
        self,
        requester: RequesterContext,
        *,
        action: str,
        object_type: str,
        object_id: str,
        detail: dict | None = None,
    ) -> None:
        self.store.append_audit(
            at=self._now(),
            actor_principal=requester.principal,
            actor_uid=requester.uid,
            action=action,
            object_type=object_type,
            object_id=object_id,
            detail=detail or {},
        )

    def _target(
        self, raw: object, *, operator_allowlist: tuple[OperatorTarget, ...] = ()
    ) -> NormalizedTarget:
        return validate_engine_target_url(
            raw,
            allowed_ports=self.config.allowed_ports,
            allow_https=self.config.allow_https_targets,
            allow_localhost_name=self.config.allow_localhost_name,
            operator_allowlist=operator_allowlist,
        )

    def _unobserved_health(self) -> dict:
        """新建资源时的健康：**永远是 UNKNOWN**，并说明是哪一种 UNKNOWN。"""
        reason = REASON_NEVER_PROBED if self.prober is not None else REASON_NOT_CONFIGURED
        return unknown(reason)

    def _observe(self, target: NormalizedTarget) -> HealthObservation | None:
        """探测一次。没有 prober 时返回 ``None``（表示**没有观测过**）。"""
        if self.prober is None:
            return None
        result = self.prober.probe(target)
        if not isinstance(result, ProbeResult):
            raise InvalidRequest("prober must return a ProbeResult")
        return observation_from_probe(
            result, observed_at=isoformat(self._now()), source=SOURCE_PROBE
        )

    # -- whoami -----------------------------------------------------------

    def whoami(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        counts = {
            "engines": len(policy.visible_engines()),
            "models": len(policy.visible_models()),
            "deployments": len(policy.visible_deployments()),
            "endpoints": len(policy.visible_endpoints()),
        }
        data = {
            "identity": requester.to_dict(),
            "authorized": {
                "scopes": sorted(requester.scopes),
                "roles": sorted(requester.roles),
                "is_admin_read": requester.is_admin_read,
                "is_admin_manage": requester.is_admin_manage,
            },
            "visible_counts": counts,
            "capabilities": {
                "can_write": "registry:write" in requester.scopes,
                "can_share": (
                    "registry:endpoint:share" in requester.scopes
                    or requester.is_admin_manage
                ),
                "can_invoke": "registry:endpoint:invoke" in requester.scopes,
                "can_read_all": requester.is_admin_read,
            },
            "deployment": {
                "mode": self.config.mode,
                "synthetic": self.config.synthetic,
                "prober_attached": self.prober is not None,
                "gateway_attached": bool(getattr(self.gateway, "attached", False)),
                "gateway_adapter": getattr(self.gateway, "name", "unknown"),
            },
        }
        return self._envelope(
            data,
            requester,
            notes=[
                "身份由服务端受信目录从凭据推出；请求里的 owner/uid/role 字段一律被拒绝。",
            ],
        )

    # -- Engine -----------------------------------------------------------

    def _engine_payload(self, engine: Engine, policy: AccessPolicy) -> dict:
        """Engine 的**唯一**视图。单个查与列表都用它 —— 免得同一个对象两种形状。"""
        data = dto.render_engine(engine, reveal_target=policy.can_see_engine_raw_target(engine))
        block = data["verification"]
        # 服务级事实（不属于这一行，属于这个部署）。
        block["prober_attached"] = self.prober is not None
        block["protocol_prober"] = getattr(self.protocol_prober, "name", "unknown")
        block["ownership_policy"] = self.config.ownership_policy
        block["require_protocol_verification"] = bool(self.config.require_protocol_verification)
        # 「注册 != 验证 != 运行」：把还差什么明明白白列出来。
        blockers: list[str] = []
        if not block.get("transport_verified"):
            blockers.append("transport_unreachable")
        if not block.get("protocol_verified"):
            blockers.append("protocol_unverified")
        if not (engine.ownership or {}).get("verified"):
            blockers.append("ownership_unverified")
        data["blockers"] = blockers
        data["capabilities"] = {
            # 能不能被共享/调用，按**当前策略**算出来，而不是让前端自己猜。
            "shareable": engine.status == ENGINE_VERIFIED
            and (not self.config.ownership_enforced
                 or bool((engine.ownership or {}).get("verified")))
            and (not self.config.require_protocol_verification
                 or bool(block.get("protocol_verified"))),
            "shareable_caveat": (
                None
                if engine.status == ENGINE_VERIFIED
                else "engine is not transport-verified"
            ),
            "can_be_started_by_platform": False,
            "note": (
                "本平台**不启动**引擎：start/stop/restart 与 CUDA/GPU 准入属于独立的"
                "受限 owner-agent，不在这条路径上。这里也没有 running 这个事实。"
            ),
        }
        return data

    def _engine_target_for(
        self, requester: RequesterContext, payload: dict, mode: str
    ) -> tuple[NormalizedTarget, OperatorTarget | None]:
        """按登记模式解析目标，返回 ``(target, 运维 entry 或 None)``。

        ``register_engine`` 与 ``preflight_engine`` 共用这一处 —— 「预检说能登记、
        登记却被拒」这种两份规则漂移的结局，只有一份规则才能杜绝。
        """
        # **登记模式决定能用哪个 allowlist**，这是本函数最关键的一处：
        #
        #   user 模式       -> operator_allowlist=()  运维的承认**不**对普通用户生效，
        #                                              于是 8080 依旧进不来。
        #   system_existing -> 传入运维策略           只有走这条路，8080 才可能被登记。
        #
        # 顺序也重要：先判模式与权限，再解析 URL —— 一个没有管理员权限的请求
        # 不该因为「它恰好命中了一条策略」而得到不同的错误信息。
        if mode == ENGINE_MODE_SYSTEM_EXISTING:
            if SCOPE_ADMIN_MANAGE not in requester.scopes:
                raise AccessDenied(
                    "registering an existing system service requires registry:admin:manage"
                )
            target = self._target(
                payload.get("target_url"), operator_allowlist=self.operator_policy.targets
            )
            entry = self._operator_entry_for(target)
            if entry is None:
                # 允许走 allowlist 进来说明不了问题：system_existing 意味着
                # 「运维明确承认过这台既有服务」，所以必须**命中一条 entry**。
                raise AccessDenied(
                    "registering an existing system service requires an operator-owned "
                    "allowlist entry for that exact target"
                )
        else:
            target = self._target(payload.get("target_url"))
            entry = None
        return target, entry

    def register_engine(
        self, requester: RequesterContext, payload: dict, *, idempotency_key: object = None
    ) -> tuple[dict, bool]:
        policy = self.policy(requester)
        policy.require_write_scope()
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        name = validate_name(payload.get("name"), what="engine name")
        protocol = validate_protocol(payload.get("protocol"))
        mode = payload.get("engine_mode") or ENGINE_MODE_USER
        if mode not in ENGINE_MODES:
            raise InvalidRequest(f"unsupported engine_mode: {mode!r}")
        target, entry = self._engine_target_for(requester, payload, mode)

        description = validate_optional_text(payload.get("description"), what="description")
        tags = validate_tags(payload.get("tags"))
        key = _idempotency_key(idempotency_key)

        fingerprint = _fingerprint(
            {
                "kind": "engine",
                "mode": mode,
                "name": name,
                "target_url": target.url,
                "protocol": protocol,
                "description": description,
                "tags": list(tags),
            }
        )

        if key is not None:
            prior = self.store.engine_by_idempotency_key(requester.principal, key)
            if prior is not None:
                if self.store.engine_fingerprint(prior.id) == fingerprint:
                    return self._engine_view(prior, policy, requester, replay=True), False
                raise Conflict("idempotency key was already used with different content")

        existing = self.store.engine_by_owner_name(requester.principal, name)
        if existing is not None:
            if self.store.engine_fingerprint(existing.id) == fingerprint:
                # 同一份内容再提交一次：不是错误，是一次重放。
                return self._engine_view(existing, policy, requester, replay=True), False
            raise Conflict(f"engine {name!r} already exists with different content")

        now = self._now()
        ownership = self._ownership_for_owner(
            requester.principal, target, entry=entry, fallback_linux_user=requester.linux_user
        )
        if ownership.reason == ownership_mod.REASON_TARGET_TAKEN:
            raise Conflict(
                f"{target.host}:{target.port} is already registered by another account"
            )
        observation, protocol_obs, merged = self._verification(target, protocol)

        status = ENGINE_REGISTERED
        health = self._unobserved_health()
        last_seen = None
        if observation is not None:
            health = observation.to_dict()
            last_seen = now
            status = ENGINE_VERIFIED if merged["verified"] else ENGINE_UNAVAILABLE

        engine = Engine(
            id=new_id("eng"),
            owner_principal=requester.principal,
            # 平台账户没有自己的 uid：engine 这一行记下观测到的运行 uid。
            owner_uid=(
                ownership.uid
                if is_account_principal(requester.principal) and ownership.uid is not None
                else requester.uid
            ),
            name=name,
            target=target.to_dict(),
            protocol=protocol,
            status=status,
            health=health,
            source=(
                SOURCE_OPERATOR_EXISTING if entry is not None else SOURCE_CLIENT_REGISTERED
            ),
            created_at=now,
            updated_at=now,
            last_seen_at=last_seen,
            description=description,
            tags=tags,
            mode=mode,
            ownership=ownership.to_dict(),
            verification=merged,
        )
        self.store.insert_engine(engine, idempotency_key=key, fingerprint=fingerprint)
        self._audit(
            requester,
            action="register",
            object_type="engine",
            object_id=engine.id,
            detail={
                "name": name,
                "mode": mode,
                "status": status,
                "target_host": target.host,
                "target_port": target.port,
                # 一次「靠运维策略放行」的登记必须在审计里看得见。
                "operator_blessed": target.operator_blessed,
                "operator_label": target.operator_label,
                "ownership_state": ownership.state,
                "transport_verified": merged["transport_verified"],
                "protocol_verified": merged["protocol_verified"],
                "blockers": merged["blockers"],
            },
        )
        return self._engine_view(engine, policy, requester), True

    def preflight_engine(self, requester: RequesterContext, payload: dict) -> dict:
        """「登记模型服务」之前的**只读**预检：不写库、不写审计、不登记任何东西。

        回答前端登记表单的四个问题，每个都是 ``{ok, detail}``（``ok`` 为 ``None``
        表示本部署**没有**这条来源，不是失败）：

        * ``port_reachable`` —— 回环 TCP connect（短超时；只有挂了传输 prober 的部署
          才真的连）；
        * ``ownership`` —— 与登记走同一个 prover，登记者用户名来自受信目录；
        * ``openai_compat`` —— 与登记走同一个协议 prober（只读 ``/v1/models``）；
        * ``name_unique`` —— 在调用者自己的 engine 名字空间里是否已被占用。

        目标解析与 ``register_engine`` **共用**同一个 helper：预检能过的，登记的
        目标校验也一定能过。需要 ``registry:write`` —— 它只服务于登记流程。
        """
        policy = self.policy(requester)
        policy.require_write_scope()
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        name = validate_name(payload.get("name"), what="engine name")
        mode = payload.get("engine_mode") or ENGINE_MODE_USER
        if mode not in ENGINE_MODES:
            raise InvalidRequest(f"unsupported engine_mode: {mode!r}")
        target, entry = self._engine_target_for(requester, payload, mode)

        existing = self.store.engine_by_owner_name(requester.principal, name)
        ownership = self._ownership_for_owner(
            requester.principal, target, entry=entry, fallback_linux_user=requester.linux_user
        )
        protocol_obs = self._observe_protocol(target, PROTOCOL_OPENAI_COMPAT)
        data = preflight_mod.build_engine_preflight(
            target=target,
            name=name,
            existing_engine_id=existing.id if existing is not None else None,
            ownership=ownership,
            port_checker=self.port_checker,
            protocol=protocol_obs,
        )
        return self._envelope(
            data,
            requester,
            notes=[
                "只读预检：不登记、不写审计；ok=null 表示本部署没有这条来源，不是失败。",
                "所有权的用户名来自受信目录；请求里的 uid/owner 字段一律被拒绝。",
            ],
        )

    def _engine_view(
        self, engine: Engine, policy: AccessPolicy, requester: RequesterContext, *, replay: bool = False
    ) -> dict:
        data = self._engine_payload(engine, policy)
        data["idempotent_replay"] = replay
        return self._envelope(data, requester)

    def list_engines(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        engines = policy.visible_engines()
        items = [self._engine_payload(item, policy) for item in engines]
        return self._envelope(
            {"items": items, "count": len(items)},
            requester,
            notes=["仅列出调用者有权知道其存在的 engine；他人的未共享 engine 不会出现在这里。"],
        )

    def get_engine(self, requester: RequesterContext, engine_id: str) -> dict:
        policy = self.policy(requester)
        engine = policy.require_engine_read(engine_id)
        return self._engine_view(engine, policy, requester)

    def verify_engine(self, requester: RequesterContext, engine_id: str) -> dict:
        policy = self.policy(requester)
        engine = policy.require_engine_read(engine_id)
        policy.require_owner_for(
            engine.owner_principal, object_type="engine", object_id=engine.id, action="verify"
        )
        if self.prober is None:
            # 没有观测源 -> 不能假装验证过。状态原样保留。
            self._audit(
                requester,
                action="verify_skipped",
                object_type="engine",
                object_id=engine.id,
                detail={"reason": REASON_NOT_CONFIGURED},
            )
            view = self._engine_view(engine, policy, requester)
            view["data"]["verification"]["status"] = "NOT_CONFIGURED"
            view["notes"].append(
                "本部署未配置任何观测源：验证无法进行，状态保持未验证而不是当作通过。"
            )
            return view

        observation = self._observe(NormalizedTarget(**engine.target))
        assert observation is not None
        now = self._now()
        target = NormalizedTarget(**engine.target)
        # 所有权**重新**证明一次：进程可能已经换了个 uid 在跑，旧结论不该被沿用。
        entry = self._operator_entry_for(target) if engine.mode == ENGINE_MODE_SYSTEM_EXISTING else None
        ownership = self._ownership_for_owner(engine.owner_principal, target, entry=entry)
        protocol_obs = self._observe_protocol(target, engine.protocol)
        merged = build_verification(
            transport=observation,
            protocol=protocol_obs,
            require_protocol=self.config.require_protocol_verification,
        )
        status = ENGINE_VERIFIED if merged["verified"] else ENGINE_UNAVAILABLE
        self.store.update_engine_status(
            engine.id,
            status=status,
            health=observation.to_dict(),
            last_seen_at=now,
            updated_at=now,
            ownership=ownership.to_dict(),
            verification=merged,
        )
        self._audit(
            requester,
            action="verify",
            object_type="engine",
            object_id=engine.id,
            detail={
                "status": status,
                "health": observation.state,
                "ownership_state": ownership.state,
                "transport_verified": merged["transport_verified"],
                "protocol_verified": merged["protocol_verified"],
                "blockers": merged["blockers"],
            },
        )
        refreshed = self.store.engine_by_id(engine.id)
        return self._engine_view(refreshed, policy, requester)  # type: ignore[arg-type]

    # -- Model ------------------------------------------------------------

    def register_model(
        self, requester: RequesterContext, payload: dict, *, idempotency_key: object = None
    ) -> tuple[dict, bool]:
        policy = self.policy(requester)
        policy.require_write_scope()
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        name = validate_name(payload.get("name"), what="model name")
        raw_weight = payload.get("weight_path")
        weight_path = (
            validate_weight_path_reference(raw_weight) if raw_weight is not None else None
        )
        weight_sha = weight_path_fingerprint(weight_path) if weight_path else None
        model_format = validate_optional_text(payload.get("format"), what="format")
        parameter_count = validate_optional_text(
            payload.get("parameter_count"), what="parameter_count"
        )
        description = validate_optional_text(payload.get("description"), what="description")
        tags = validate_tags(payload.get("tags"))
        key = _idempotency_key(idempotency_key)

        fingerprint = _fingerprint(
            {
                "kind": "model",
                "name": name,
                "weight_path": weight_path,
                "format": model_format,
                "parameter_count": parameter_count,
                "description": description,
                "tags": list(tags),
            }
        )

        if key is not None:
            prior = self.store.model_by_idempotency_key(requester.principal, key)
            if prior is not None:
                if self.store.model_fingerprint(prior.id) == fingerprint:
                    return self._model_view(prior, policy, requester, replay=True), False
                raise Conflict("idempotency key was already used with different content")

        existing = self.store.model_by_owner_name(requester.principal, name)
        if existing is not None:
            if self.store.model_fingerprint(existing.id) == fingerprint:
                return self._model_view(existing, policy, requester, replay=True), False
            raise Conflict(f"model {name!r} already exists with different content")

        now = self._now()
        model = Model(
            id=new_id("mdl"),
            owner_principal=requester.principal,
            owner_uid=requester.uid,
            name=name,
            weight_path=weight_path,
            weight_path_sha256=weight_sha,
            format=model_format,
            parameter_count=parameter_count,
            status=MODEL_REGISTERED,
            # 模型没有探测路径：Registry 不打开权重文件，所以这里**只能**是 UNKNOWN。
            health=unknown("weight_file_is_a_reference_never_opened_by_registry"),
            source=SOURCE_CLIENT_REGISTERED,
            created_at=now,
            updated_at=now,
            description=description,
            tags=tags,
        )
        self.store.insert_model(model, idempotency_key=key, fingerprint=fingerprint)
        self._audit(
            requester,
            action="register",
            object_type="model",
            object_id=model.id,
            detail={
                "name": name,
                "weight_path_registered": weight_path is not None,
                "weight_path_sha256": weight_sha,
            },
        )
        return self._model_view(model, policy, requester), True

    def _model_view(
        self, model: Model, policy: AccessPolicy, requester: RequesterContext, *, replay: bool = False
    ) -> dict:
        data = dto.render_model(model, reveal_weight_path=policy.can_see_weight_path(model))
        data["idempotent_replay"] = replay
        return self._envelope(data, requester)

    def list_models(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        models = policy.visible_models()
        items = [
            dto.render_model(item, reveal_weight_path=policy.can_see_weight_path(item))
            for item in models
        ]
        return self._envelope(
            {"items": items, "count": len(items)},
            requester,
            notes=[
                "非 owner 看到的是权重路径的哈希指纹，不是路径本身 —— 共享的是服务，不是家目录结构。",
            ],
        )

    def get_model(self, requester: RequesterContext, model_id: str) -> dict:
        policy = self.policy(requester)
        model = policy.require_model_read(model_id)
        return self._model_view(model, policy, requester)

    # -- Deployment -------------------------------------------------------

    def register_deployment(
        self, requester: RequesterContext, payload: dict, *, idempotency_key: object = None
    ) -> tuple[dict, bool]:
        policy = self.policy(requester)
        policy.require_write_scope()
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        name = validate_name(payload.get("name"), what="deployment name")
        engine_id = payload.get("engine_id")
        model_id = payload.get("model_id")
        if not isinstance(engine_id, str) or not engine_id.strip():
            raise InvalidRequest("engine_id is required")
        if not isinstance(model_id, str) or not model_id.strip():
            raise InvalidRequest("model_id is required")
        description = validate_optional_text(payload.get("description"), what="description")
        tags = validate_tags(payload.get("tags"))
        key = _idempotency_key(idempotency_key)

        # 两个引用都必须落在调用者**可见**的范围内：不可见与不存在给同一个答案。
        policy.require_engine_read(engine_id)
        policy.require_model_read(model_id)

        fingerprint = _fingerprint(
            {
                "kind": "deployment",
                "name": name,
                "engine_id": engine_id,
                "model_id": model_id,
                "description": description,
                "tags": list(tags),
            }
        )

        if key is not None:
            prior = self.store.deployment_by_idempotency_key(requester.principal, key)
            if prior is not None:
                if self.store.deployment_fingerprint(prior.id) == fingerprint:
                    return self._deployment_view(prior, policy, requester, replay=True), False
                raise Conflict("idempotency key was already used with different content")

        existing = self.store.deployment_by_owner_name(requester.principal, name)
        if existing is not None:
            if self.store.deployment_fingerprint(existing.id) == fingerprint:
                return self._deployment_view(existing, policy, requester, replay=True), False
            raise Conflict(f"deployment {name!r} already exists with different content")

        now = self._now()
        deployment = Deployment(
            id=new_id("dep"),
            owner_principal=requester.principal,
            owner_uid=requester.uid,
            name=name,
            engine_id=engine_id,
            model_id=model_id,
            status=DEPLOYMENT_REGISTERED,
            health=self._unobserved_health(),
            source=SOURCE_CLIENT_REGISTERED,
            created_at=now,
            updated_at=now,
            description=description,
            tags=tags,
        )
        self.store.insert_deployment(deployment, idempotency_key=key, fingerprint=fingerprint)
        self._audit(
            requester,
            action="register",
            object_type="deployment",
            object_id=deployment.id,
            detail={"name": name, "engine_id": engine_id, "model_id": model_id},
        )
        return self._deployment_view(deployment, policy, requester), True

    def _deployment_view(
        self,
        deployment: Deployment,
        policy: AccessPolicy,
        requester: RequesterContext,
        *,
        replay: bool = False,
    ) -> dict:
        data = dto.render_deployment(deployment)
        data["idempotent_replay"] = replay
        data["readiness"] = {
            "registered": True,
            "ready": deployment.status == DEPLOYMENT_READY,
            "note": (
                "registered 与 ready 是两件事：注册只说明登记过元数据，"
                "ready 需要一次真实的 engine 验证。"
            ),
        }
        return self._envelope(data, requester)

    def list_deployments(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        deployments = policy.visible_deployments()
        items = [dto.render_deployment(item) for item in deployments]
        return self._envelope({"items": items, "count": len(items)}, requester)

    def get_deployment(self, requester: RequesterContext, deployment_id: str) -> dict:
        policy = self.policy(requester)
        deployment = policy.require_deployment_read(deployment_id)
        return self._deployment_view(deployment, policy, requester)

    def verify_deployment(self, requester: RequesterContext, deployment_id: str) -> dict:
        policy = self.policy(requester)
        deployment = policy.require_deployment_read(deployment_id)
        policy.require_owner_for(
            deployment.owner_principal,
            object_type="deployment",
            object_id=deployment.id,
            action="verify",
        )
        engine = policy.require_engine_read(deployment.engine_id)
        if engine.status != ENGINE_VERIFIED:
            # 上一级没有验证过 -> 拒绝，而不是顺手替它验证一次。
            raise NotVerified(
                f"engine {engine.id!r} is {engine.status!r}; verify the engine first"
            )

        observation = self._observe(NormalizedTarget(**engine.target))
        now = self._now()
        if observation is None:
            # 没有观测源：保持 registered，并把原因如实放回 DTO。
            self._audit(
                requester,
                action="verify_skipped",
                object_type="deployment",
                object_id=deployment.id,
                detail={"reason": REASON_NOT_CONFIGURED},
            )
            view = self._deployment_view(deployment, policy, requester)
            view["data"]["readiness"]["ready"] = False
            view["data"]["readiness"]["reason"] = REASON_NOT_CONFIGURED
            view["notes"].append("本部署未配置观测源：无法验证，状态保持 registered。")
            return view

        status = (
            DEPLOYMENT_READY
            if observation.state in ("healthy", "degraded")
            else DEPLOYMENT_UNAVAILABLE
        )
        self.store.update_deployment_status(
            deployment.id,
            status=status,
            health=observation.to_dict(),
            verified_at=now if status == DEPLOYMENT_READY else None,
            last_seen_at=now,
            updated_at=now,
        )
        self._audit(
            requester,
            action="verify",
            object_type="deployment",
            object_id=deployment.id,
            detail={"status": status, "health": observation.state},
        )
        refreshed = self.store.deployment_by_id(deployment.id)
        return self._deployment_view(refreshed, policy, requester)  # type: ignore[arg-type]

    # -- Endpoint ---------------------------------------------------------

    def register_endpoint(
        self, requester: RequesterContext, payload: dict, *, idempotency_key: object = None
    ) -> tuple[dict, bool]:
        policy = self.policy(requester)
        policy.require_write_scope()
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        name = validate_name(payload.get("name"), what="endpoint name")
        deployment_id = payload.get("deployment_id")
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            raise InvalidRequest("deployment_id is required")
        description = validate_optional_text(payload.get("description"), what="description")
        tags = validate_tags(payload.get("tags"))
        # 新建永远是 private。这里**不**接受 payload 里的 visibility：
        # 开放范围是一件独立的事，走 share/unshare，不走创建。
        if "visibility" in payload:
            raise InvalidRequest(
                "visibility is not settable at creation; endpoints start private and "
                "are shared explicitly via share/unshare"
            )
        key = _idempotency_key(idempotency_key)

        deployment = policy.require_deployment_read(deployment_id)

        fingerprint = _fingerprint(
            {
                "kind": "endpoint",
                "name": name,
                "deployment_id": deployment_id,
                "description": description,
                "tags": list(tags),
            }
        )

        if key is not None:
            prior = self.store.endpoint_by_idempotency_key(requester.principal, key)
            if prior is not None:
                if self.store.endpoint_fingerprint(prior.id) == fingerprint:
                    return self._endpoint_view(prior, policy, requester, replay=True), False
                raise Conflict("idempotency key was already used with different content")

        existing = self.store.endpoint_by_owner_name(requester.principal, name)
        if existing is not None:
            if self.store.endpoint_fingerprint(existing.id) == fingerprint:
                return self._endpoint_view(existing, policy, requester, replay=True), False
            raise Conflict(f"endpoint {name!r} already exists with different content")

        now = self._now()
        route_path = f"/x/{requester.principal}/{name}"
        endpoint = Endpoint(
            id=new_id("ep"),
            owner_principal=requester.principal,
            owner_uid=requester.uid,
            name=name,
            deployment_id=deployment_id,
            route_path=route_path,
            visibility=VISIBILITY_PRIVATE,
            status=ENDPOINT_REGISTERED,
            health=self._unobserved_health(),
            source=SOURCE_CLIENT_REGISTERED,
            created_at=now,
            updated_at=now,
            description=description,
            tags=tags,
        )
        assert_no_default_private_trap(endpoint)

        # 注册即尝试验证（有观测源时）：注册与验证强绑定。
        engine = policy.require_engine_read(deployment.engine_id)
        observation = self._observe(NormalizedTarget(**engine.target))
        status = ENDPOINT_REGISTERED
        health = endpoint.health
        verified_at = None
        if observation is not None:
            health = observation.to_dict()
            verified_at = now if observation.state in ("healthy", "degraded") else None
            status = ENDPOINT_READY if verified_at is not None else ENDPOINT_UNAVAILABLE
        endpoint = Endpoint(
            id=endpoint.id,
            owner_principal=endpoint.owner_principal,
            owner_uid=endpoint.owner_uid,
            name=endpoint.name,
            deployment_id=endpoint.deployment_id,
            route_path=endpoint.route_path,
            visibility=endpoint.visibility,
            status=status,
            health=health,
            source=endpoint.source,
            created_at=endpoint.created_at,
            updated_at=now,
            verified_at=verified_at,
            last_seen_at=now if observation is not None else None,
            description=endpoint.description,
            tags=endpoint.tags,
        )
        self.store.insert_endpoint(endpoint, idempotency_key=key, fingerprint=fingerprint)
        self._audit(
            requester,
            action="register",
            object_type="endpoint",
            object_id=endpoint.id,
            detail={
                "name": name,
                "deployment_id": deployment_id,
                "status": status,
                "visibility": VISIBILITY_PRIVATE,
            },
        )
        return self._endpoint_view(endpoint, policy, requester), True

    def _decorate_router_sync(self, block: dict | None) -> dict | None:
        """把**部署级**事实补进行级的 ``router_sync`` 视图（就地修改）。

        一张数据库行只知道「回执代数落后于 revision」；「本部署装没装 RouterAdapter」
        只有 service 知道。从前 ``models.router_sync_block()`` 替部署回答了这个
        问题，于是同一份响应里 ``data.router_sync.reason`` 说「没有 adapter」而
        ``meta.router_sync_report.reason`` 说「Gateway 代数落后」—— 两句都写着，
        只有一句是真的。现在行级视图只说行级事实，部署级事实在这里补。
        """
        if not isinstance(block, dict) or not block.get("pending"):
            return block
        name = getattr(self.router, "name", "unknown")
        if not bool(getattr(self.router, "attached", False)):
            block["reason"] = REASON_NO_ROUTER_ADAPTER
            block["note"] = (
                "本部署没有配置 RouterAdapter：Registry 这边的改动要等运维/Gateway "
                "按新 generation 取一次路由配置才生效。在那之前这里是 pending，"
                "不是「已切换」。"
            )
        else:
            block["adapter"] = name
            block["note"] = (
                f"RouterAdapter（{name}）已装配，但路由**尚未确认生效**：回执代数仍"
                "落后于当前 revision，或 Gateway 的回读对不上本次快照。repoint / "
                "rollback 的响应里 meta.router_sync_report 有本次的精确原因。"
            )
        return block

    def _endpoint_view(
        self,
        endpoint: Endpoint,
        policy: AccessPolicy,
        requester: RequesterContext,
        *,
        replay: bool = False,
    ) -> dict:
        owner_view = endpoint.owner_principal == requester.principal or requester.is_admin_read
        grantees = self.store.grantees(endpoint.id) if owner_view else ()
        data = dto.render_endpoint(endpoint, grantees=grantees, include_acl=owner_view)
        data["idempotent_replay"] = replay
        data["shared_with_me"] = (
            endpoint.owner_principal != requester.principal
            and endpoint.id in policy.visibility().endpoints
        )
        # 后端事实：这个 alias 现在指向哪个 deployment/model，以及它的两根轴。
        # 「ready」是 Endpoint 自己的可达性判定；能不能共享/调用还要看策略
        # （``_gate_endpoint_backend``）。两件事分开说，免得 ready 被读成「可以共享」。
        deployment = self.store.deployment_by_id(endpoint.deployment_id)
        engine = self.store.engine_by_id(deployment.engine_id) if deployment else None
        data["backend"] = {
            "deployment_id": endpoint.deployment_id,
            "model_id": deployment.model_id if deployment else None,
            "engine_id": deployment.engine_id if deployment else None,
            "deployment_status": deployment.status if deployment else None,
            "engine_status": engine.status if engine else None,
            "transport_verified": bool((engine.verification or {}).get("transport_verified"))
            if engine
            else False,
            "protocol_verified": bool((engine.verification or {}).get("protocol_verified"))
            if engine
            else False,
            "ownership_state": (engine.ownership or {}).get("state") if engine else None,
        }
        data["policy"] = {
            "ownership_policy": self.config.ownership_policy,
            "require_protocol_verification": bool(self.config.require_protocol_verification),
            "shareable_caveat": (
                None
                if endpoint.status == ENDPOINT_READY
                else "endpoint is not ready; see status"
            ),
        }
        self._decorate_router_sync(data.get("router_sync"))
        return self._envelope(data, requester)

    def list_endpoints(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        endpoints = policy.visible_endpoints()
        items = []
        for item in endpoints:
            owner_view = item.owner_principal == requester.principal or requester.is_admin_read
            rendered = dto.render_endpoint(
                item,
                grantees=self.store.grantees(item.id) if owner_view else (),
                include_acl=owner_view,
            )
            # 与详情路径同一句话：列表与详情不该对同一件事给出两个理由。
            self._decorate_router_sync(rendered.get("router_sync"))
            items.append(rendered)
        return self._envelope(
            {"items": items, "count": len(items)},
            requester,
            notes=[
                "默认私有：只列出自己名下的与**已被显式共享给**自己的 Endpoint。",
                "他人未共享的 Endpoint 不在这里，也不会以任何其他形式暴露其存在。",
            ],
        )

    def get_endpoint(self, requester: RequesterContext, endpoint_id: str) -> dict:
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        return self._endpoint_view(endpoint, policy, requester)

    def verify_endpoint(self, requester: RequesterContext, endpoint_id: str) -> dict:
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        policy.require_owner_for(
            endpoint.owner_principal,
            object_type="endpoint",
            object_id=endpoint.id,
            action="verify",
        )
        deployment = policy.require_deployment_read(endpoint.deployment_id)
        engine = policy.require_engine_read(deployment.engine_id)
        if engine.status != ENGINE_VERIFIED:
            raise NotVerified(
                f"engine {engine.id!r} is {engine.status!r}; verify the engine first"
            )

        observation = self._observe(NormalizedTarget(**engine.target))
        now = self._now()
        if observation is None:
            self._audit(
                requester,
                action="verify_skipped",
                object_type="endpoint",
                object_id=endpoint.id,
                detail={"reason": REASON_NOT_CONFIGURED},
            )
            view = self._endpoint_view(endpoint, policy, requester)
            view["notes"].append("本部署未配置观测源：无法验证，Endpoint 保持不可共享。")
            return view

        status = (
            ENDPOINT_READY
            if observation.state in ("healthy", "degraded")
            else ENDPOINT_UNAVAILABLE
        )
        self.store.update_endpoint(
            endpoint.id,
            visibility=endpoint.visibility,
            status=status,
            health=observation.to_dict(),
            verified_at=now if status == ENDPOINT_READY else None,
            last_seen_at=now,
            updated_at=now,
        )
        self._audit(
            requester,
            action="verify",
            object_type="endpoint",
            object_id=endpoint.id,
            detail={"status": status, "health": observation.state},
        )
        refreshed = self.store.endpoint_by_id(endpoint.id)
        return self._endpoint_view(refreshed, policy, requester)  # type: ignore[arg-type]

    def share_endpoint(self, requester: RequesterContext, endpoint_id: str, payload: dict) -> dict:
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        policy.require_endpoint_owner_or_admin(endpoint, action="share")
        policy.require_share_scope()
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        # 两根轴的门（enforced 档才拦）：共享的是「这个 alias 后面的服务」，
        # 所以后端得先真的是个被证明过的模型服务。
        self._gate_endpoint_backend(policy, endpoint, action="share")

        mode = validate_visibility(payload.get("mode"))
        raw_grantees = payload.get("grantees") or []
        if mode == VISIBILITY_USERS:
            grantees = policy.validate_grantees(list(raw_grantees))
            if not grantees:
                raise InvalidRequest("mode 'users' requires at least one grantee")
            # 未验证的 Endpoint 不可共享：注册 != 可用。
            policy.require_endpoint_ready(endpoint, action="share")
        else:
            if mode == VISIBILITY_ALL_APPROVED:
                policy.require_endpoint_ready(endpoint, action="share")
            grantees = ()

        previous = set(self.store.grantees(endpoint.id))
        desired = set(grantees)
        removed = self.store.revoke_all(endpoint.id)
        for grantee in sorted(desired):
            self.store.grant(
                endpoint.id, grantee, granted_by=requester.principal, granted_at=self._now()
            )

        # 逐条 grant/revoke 的审计，而不是只留一条汇总的 share：
        # 「谁在什么时候把哪个 Endpoint 给了谁」要能单独查出来（见 grant_history），
        # 一条汇总事件在多人共享时无法回答这个问题。
        for grantee in sorted(desired - previous):
            self._audit(
                requester,
                action="grant",
                object_type="endpoint",
                object_id=endpoint.id,
                detail={"grantee": grantee, "mode": mode},
            )
        for grantee in sorted(previous - desired):
            self._audit(
                requester,
                action="revoke",
                object_type="endpoint",
                object_id=endpoint.id,
                detail={"grantee": grantee, "mode": mode},
            )

        now = self._now()
        self.store.update_endpoint(
            endpoint.id,
            visibility=mode,
            status=endpoint.status,
            health=endpoint.health,
            verified_at=endpoint.verified_at,
            last_seen_at=endpoint.last_seen_at,
            updated_at=now,
        )
        self._audit(
            requester,
            action="share",
            object_type="endpoint",
            object_id=endpoint.id,
            detail={"mode": mode, "grantees": list(grantees), "revoked_previously": removed},
        )
        refreshed = self.store.endpoint_by_id(endpoint.id)
        view = self._endpoint_view(refreshed, policy, requester)  # type: ignore[arg-type]
        view["meta"] = {
            "mode": mode,
            "grantees": list(grantees),
            "revoked_previously": removed,
            "granted": sorted(desired - previous),
            "revoked": sorted(previous - desired),
        }
        return view

    def unshare_endpoint(
        self, requester: RequesterContext, endpoint_id: str, payload: dict | None = None
    ) -> dict:
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        policy.require_endpoint_owner_or_admin(endpoint, action="unshare")
        policy.require_share_scope()
        payload = payload or {}
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        target = payload.get("grantee")
        if target is None:
            removed_grantees = list(self.store.grantees(endpoint.id))
            removed = self.store.revoke_all(endpoint.id)
            mode = VISIBILITY_PRIVATE
            detail = {"mode": VISIBILITY_PRIVATE, "removed": removed}
        else:
            if not isinstance(target, str) or not target.strip():
                raise InvalidRequest("grantee must be a non-empty principal string")
            grantee = target.strip()
            removed = 1 if self.store.revoke(endpoint.id, grantee) else 0
            removed_grantees = [grantee] if removed else []
            remaining = self.store.grantees(endpoint.id)
            mode = endpoint.visibility
            if endpoint.visibility == VISIBILITY_USERS and not remaining:
                # 最后一个受托人被移除 -> 回到私有，避免留下一个「users 模式但没人」的空壳。
                mode = VISIBILITY_PRIVATE
            detail = {"grantee": grantee, "removed": removed, "mode": mode}

        for grantee in sorted(removed_grantees):
            self._audit(
                requester,
                action="revoke",
                object_type="endpoint",
                object_id=endpoint.id,
                detail={"grantee": grantee, "mode": mode},
            )

        now = self._now()
        self.store.update_endpoint(
            endpoint.id,
            visibility=mode,
            status=endpoint.status,
            health=endpoint.health,
            verified_at=endpoint.verified_at,
            last_seen_at=endpoint.last_seen_at,
            updated_at=now,
        )
        self._audit(
            requester,
            action="unshare",
            object_type="endpoint",
            object_id=endpoint.id,
            detail=detail,
        )
        refreshed = self.store.endpoint_by_id(endpoint.id)
        view = self._endpoint_view(refreshed, policy, requester)  # type: ignore[arg-type]
        view["meta"] = detail
        view["notes"].append("撤销是立即的：授权行被删除，下一次请求就已经不可见。")
        return view

    def endpoint_acl(self, requester: RequesterContext, endpoint_id: str) -> dict:
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        if not (endpoint.owner_principal == requester.principal or requester.is_admin_read):
            # 能调一个端点不等于能看到它的授权名单。
            raise AccessDenied("the ACL of an endpoint is visible to its owner and admins only")
        grantees = self.store.grantees(endpoint.id)
        history = [
            {
                "event": "revoke" if grant.revoked_at is not None else "grant",
                "grantee": grant.grantee_principal,
                "granted_by": grant.granted_by,
                "granted_at": isoformat(grant.granted_at),
                "revoked_at": isoformat(grant.revoked_at),
                "revoked_by": grant.revoked_by,
            }
            for grant in self.store.grant_history(endpoint.id)
        ]
        data = {
            "endpoint_id": endpoint.id,
            "mode": endpoint.visibility,
            "grantees": list(grantees),
            "effective_visibility": (
                "owner_only"
                if endpoint.visibility == VISIBILITY_PRIVATE
                else endpoint.visibility
            ),
            "all_approved_members_source": (
                sorted(self.principals.approved_principals())
                if endpoint.visibility == VISIBILITY_ALL_APPROVED
                else None
            ),
            "history": history,
            "ready": endpoint.shareable,
        }
        return self._envelope(data, requester)

    # -- 重指向（repoint / rollback）----------------------------------------

    def _repoint_visibility(
        self, requester: RequesterContext, endpoint: Endpoint, payload: dict
    ) -> tuple[str, tuple[str, ...]]:
        """重指向时的授权决定。**默认把共享收回成 private。**

        这不是保守主义，是一条安全要求：Endpoint 可能已经被共享出去了，而共享的
        对象是「这个 alias 后面的服务」。换个后端却把名单留着，等于把一份为
        **旧模型**签发的授权静默转给了新模型 —— 收件人从没同意过这件事。

        要保留共享，必须由调用方**显式**重新授权（``reauthorize``），而且那一步
        要 ``registry:endpoint:share`` 这个**它自己的** scope，不是顺手借用
        repoint 的权限。重新授权与路由切换在**同一个事务**里生效，中间没有窗口。
        """
        reauthorize = payload.get("reauthorize")
        if reauthorize is None:
            return VISIBILITY_PRIVATE, ()
        if not isinstance(reauthorize, dict):
            raise InvalidRequest("reauthorize must be an object")
        policy = self.policy(requester)
        policy.require_share_scope()
        mode = validate_visibility(reauthorize.get("mode"))
        if mode == VISIBILITY_PRIVATE:
            return VISIBILITY_PRIVATE, ()
        if mode == VISIBILITY_USERS:
            grantees = policy.validate_grantees(list(reauthorize.get("grantees") or []))
            if not grantees:
                raise InvalidRequest("mode 'users' requires at least one grantee")
            return VISIBILITY_USERS, grantees
        return VISIBILITY_ALL_APPROVED, ()

    def _require_repoint_target(
        self, policy: AccessPolicy, endpoint: Endpoint, target_deployment_id: str
    ) -> Deployment:
        """目标 Deployment 的可见性与归属。

        * 不可见 -> 404（与不存在同形，不做存在性预言机）；
        * 可见但属于别的 owner -> 403。刻意**不**共用 404：调用者已经知道它存在
          （它被共享给他了），这时说「不存在」是撒谎，而 403 讲的是真话 ——
          「你确实看得见它，但它不是你的，所以不能把它挂到你的对外路径上」。
        """
        target = policy.require_deployment_read(target_deployment_id)
        if target.owner_principal != endpoint.owner_principal:
            raise AccessDenied(
                f"deployment {target.id!r} belongs to another owner; an endpoint may only "
                "be repointed at a deployment owned by the same principal"
            )
        return target

    def _repoint_result(
        self, policy: AccessPolicy, requester: RequesterContext, outcome: dict, *, operation: str
    ) -> dict:
        """把 store 的 outcome 翻成契约错误，或在成功时拼出**诚实的**结果。"""
        kind = outcome.get("outcome")
        if kind == "endpoint_missing":
            raise NotFound("endpoint not found")
        if kind == "revision_mismatch":
            raise RevisionConflict(
                f"endpoint is at revision {outcome.get('current_revision')}, not "
                f"{outcome.get('expected_revision')}; re-read the endpoint and retry"
            )
        if kind == "target_missing":
            raise NotFound("target deployment not found")
        if kind == "target_cross_owner":
            raise AccessDenied(
                "an endpoint may only be repointed at a deployment owned by the same principal"
            )
        if kind == "target_not_ready":
            raise NotVerified(
                f"target deployment is {outcome.get('target_status')!r}; "
                "repoint requires a ready deployment"
            )
        if kind == "engine_missing":
            raise NotFound("target deployment has no engine")
        if kind == "engine_not_verified":
            raise NotVerified(
                f"target engine is {outcome.get('engine_status')!r}; repoint requires a "
                "verified engine"
            )
        if kind == "ownership_unverified":
            raise OwnershipUnverified(
                "target engine ownership is not verified (ownership_policy=enforced)"
            )
        if kind == "protocol_unverified":
            raise ProtocolUnverified(
                "target engine transport is reachable but its protocol is not verified"
            )
        if kind != "ok":  # pragma: no cover - store 只产出上面这些
            raise InvalidRequest(f"unexpected repoint outcome: {kind!r}")

        endpoint = outcome["endpoint"]
        view = self._endpoint_view(endpoint, policy, requester)
        view["meta"] = {
            "operation": operation,
            "revision_from": outcome["revision_from"],
            "revision_to": outcome["revision_to"],
            "previous_deployment_id": outcome["previous_deployment_id"],
            "previous_model_id": outcome["previous_model_id"],
            "deployment_id": outcome["deployment_id"],
            "model_id": outcome["model_id"],
            "engine_id": outcome["engine_id"],
            "visibility": outcome["visibility"],
            "granted": outcome["granted"],
            "revoked": outcome["revoked"],
            # 明确重新授权时，这些人被**重新**授权了一次（旧授权已随切换失效）。
            "reestablished": outcome.get("reestablished", []),
            # 三个必须被说出来的事实，免得被读成「切换成功」。
            "acl_reset": True,
            "router_sync": ROUTER_SYNC_PENDING,
            "router_applied": False,
            "pending_router_sync": True,
        }
        view["notes"].append(
            "路由**尚未生效**：Registry 这边改了 deployment 指向，但没有任何 Gateway "
            "适配器被写过。状态是 pending_router_sync —— 要等运维/Gateway 按新的 "
            "generation 取一次路由配置（并回执 ack）才算生效。旧 alias 不变。"
        )
        if outcome["revoked"]:
            view["notes"].append(
                f"共享已随切换收回：{', '.join(outcome['revoked'])} 不再可见。"
                "为旧模型签发的授权不会被静默带给新模型。"
            )
        return view

    def repoint_endpoint(
        self, requester: RequesterContext, endpoint_id: str, payload: dict
    ) -> dict:
        """把稳定 alias 指向**另一个**已就绪的 Deployment。

        ``route_path`` 不变 —— 这就是「换小模型而 API 路径不动」。变的是
        ``deployment_id``，而这件事在一个事务里连同 ACL 重置与审计一起完成。
        """
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        policy.require_endpoint_owner_or_admin(endpoint, action="repoint")
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        target_deployment_id = payload.get("target_deployment_id")
        if not isinstance(target_deployment_id, str) or not target_deployment_id.strip():
            raise InvalidRequest("target_deployment_id is required")
        target_deployment_id = target_deployment_id.strip()

        expected_revision = payload.get("expected_revision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            # 不给默认值：默认值会让「读-改-写」的竞争悄悄过关，而这道 CAS 的
            # 全部价值就在于强迫调用方先读一次。
            raise InvalidRequest("expected_revision is required (integer >= 1)")

        reason = validate_optional_text(payload.get("reason"), what="reason")
        self._require_repoint_target(policy, endpoint, target_deployment_id)
        visibility, grantees = self._repoint_visibility(requester, endpoint, payload)

        outcome = self.store.repoint_endpoint_atomic(
            endpoint_id=endpoint.id,
            expected_revision=expected_revision,
            new_deployment_id=target_deployment_id,
            require_same_owner=True,
            require_ownership=self.config.ownership_enforced,
            require_protocol=self.config.require_protocol_verification,
            actor_principal=requester.principal,
            actor_uid=requester.uid,
            at=self._now(),
            reason=reason,
            desired_visibility=visibility,
            desired_grantees=grantees,
        )
        view = self._repoint_result(policy, requester, outcome, operation="repoint")
        self._apply_router_sync_to_view(view, endpoint.id)
        return view

    def rollback_endpoint(
        self, requester: RequesterContext, endpoint_id: str, payload: dict | None = None
    ) -> dict:
        """回到上一个 deployment。**不是特权操作** —— 走的是同一套检查。

        回滚会重新判定目标此刻是不是 ready / 已证明：历史上那个目标如果现在不可用，
        回滚就该失败，而不是因为「它以前行过」就放行。
        """
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        policy.require_endpoint_owner_or_admin(endpoint, action="rollback")
        payload = payload or {}
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)

        expected_revision = payload.get("expected_revision")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise InvalidRequest("expected_revision is required (integer >= 1)")

        previous = self.store.previous_distinct_deployment(endpoint.id, endpoint.deployment_id)
        if previous is None:
            raise NotFound("this endpoint has no previous routing to roll back to")
        self._require_repoint_target(policy, endpoint, previous)
        visibility, grantees = self._repoint_visibility(requester, endpoint, payload)

        outcome = self.store.repoint_endpoint_atomic(
            endpoint_id=endpoint.id,
            expected_revision=expected_revision,
            new_deployment_id=previous,
            require_same_owner=True,
            require_ownership=self.config.ownership_enforced,
            require_protocol=self.config.require_protocol_verification,
            actor_principal=requester.principal,
            actor_uid=requester.uid,
            at=self._now(),
            reason=validate_optional_text(payload.get("reason"), what="reason") or "rollback",
            desired_visibility=visibility,
            desired_grantees=grantees,
        )
        view = self._repoint_result(policy, requester, outcome, operation="rollback")
        self._apply_router_sync_to_view(view, endpoint.id)
        return view

    def endpoint_routings(self, requester: RequesterContext, endpoint_id: str) -> dict:
        """只追加的路由历史：这个 alias 以前指向什么、谁换的、什么时候。"""
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        if not (
            endpoint.owner_principal == requester.principal or requester.is_admin_read
        ):
            raise AccessDenied(
                "the routing history of an endpoint is visible to its owner and admins only"
            )
        rows = self.store.endpoint_routings(endpoint.id)
        # 把 model id 翻成名字：这张表是给人看「从哪个模型换到哪个模型」的，
        # 只给一串 ``mdl_...`` 等于把解释工作推回给读者。
        names = {item.id: item.name for item in self.store.list_models()}
        items = []
        for row in rows:
            payload = row.to_dict()
            payload["model_name"] = names.get(row.model_id) if row.model_id else None
            payload["previous_model_name"] = (
                names.get(row.previous_model_id) if row.previous_model_id else None
            )
            items.append(payload)
        return self._envelope(
            {
                "endpoint_id": endpoint.id,
                "route_path": endpoint.route_path,
                "current_revision": endpoint.revision,
                "current_deployment_id": endpoint.deployment_id,
                "router_generation": endpoint.router_generation,
                "pending_router_sync": endpoint.router_sync_pending,
                "items": items,
                "count": len(items),
            },
            requester,
            notes=[
                "历史只追加：换模型不会删除旧行，旧模型的数据与审计都留着。",
                "第一行是 Endpoint 创建时的初始路由 —— 所以「回到最初那个」也是可回滚的。",
            ],
        )

    def ack_routing_applied(
        self, requester: RequesterContext, endpoint_id: str, payload: dict
    ) -> dict:
        """运维声明「路由器已经按第 N 代配置生效」。

        这是 ``pending_router_sync`` 变成 ``router_sync_attested`` 的途径 —— 注意
        **不是** ``router_sync_applied``。这次请求里只有一个整数：没有 snapshot
        SHA，也没有 Gateway 自己发布的回读。``docs/architecture.md`` §5.2 规则 3
        要求「路由应用必须有 Gateway 发布的 generation/revision 与 snapshot SHA
        回执」，所以运维的一句话只能算**声明**，不能买下「已切流」四个字。

        要求 ``generation`` 与当前 revision **精确相等**：一个落后的回执会让系统
        以为已经生效，从而又回到「谎报已切换」。
        """
        policy = self.policy(requester)
        endpoint = policy.require_endpoint_read(endpoint_id)
        if SCOPE_ADMIN_MANAGE not in requester.scopes:
            raise AccessDenied(
                "acknowledging router sync is an operator action (registry:admin:manage)"
            )
        if not isinstance(payload, dict):
            raise InvalidRequest("payload must be an object")
        _reject_identity_fields(payload)
        generation = payload.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise InvalidRequest("generation is required (integer >= 1)")

        outcome = self.store.ack_router_generation(
            endpoint.id,
            generation=generation,
            actor_principal=requester.principal,
            actor_uid=requester.uid,
            at=self._now(),
            evidence=ROUTER_EVIDENCE_OPERATOR_ATTESTATION,
        )
        if outcome.get("outcome") == "endpoint_missing":
            raise NotFound("endpoint not found")
        if outcome.get("outcome") == "generation_mismatch":
            raise RevisionConflict(
                f"endpoint is at revision {outcome.get('current_revision')}; refusing to "
                f"acknowledge generation {outcome.get('requested_generation')}"
            )
        refreshed = outcome["endpoint"]
        view = self._endpoint_view(refreshed, policy, requester)
        state = refreshed.router_sync_state()
        view["meta"] = {
            "operation": "router_sync_ack",
            "generation": generation,
            # 回执**不**等于已切流：这里如实报出这次回执挣到的状态。持有
            # admin:manage 的人声明一句，不该让前端把 pending 画成 applied。
            "router_sync": state,
            "router_applied": state == ROUTER_SYNC_APPLIED,
            "evidence": ROUTER_EVIDENCE_OPERATOR_ATTESTATION,
        }
        view["notes"].append(
            "已记录运维回执：代数与当前 revision 相等，状态是 "
            f"{ROUTER_SYNC_ATTESTED}（**不是** {ROUTER_SYNC_APPLIED}）。这只说明"
            "**运维说**配置已生效，不说明本仓库验证过它；要变成 "
            f"{ROUTER_SYNC_APPLIED}，需要 RouterAdapter 读回 Gateway 自己发布的、"
            "与本次快照逐位相等的 revision 与 snapshot SHA。"
        )
        return view

    # -- 调用 -------------------------------------------------------------

    def invoke(
        self, requester: RequesterContext, endpoint_id: str, payload: dict
    ) -> dict:
        policy = self.policy(requester)
        policy.require_invoke_scope()
        # 不可见就是 404，与「不存在」同形。
        endpoint = policy.require_endpoint_read(endpoint_id)
        # 注册 != 可用：未验证的 Endpoint 不可调用。
        policy.require_endpoint_ready(endpoint, action="invoke")
        # 本部署压根没接 Gateway：这是**部署**事实（503），不是「这条路由没生效」
        # （409）。先判它，既保留了「no fake results」的既有契约，也不让一个
        # 未接线的部署把问题伪装成路由状态。
        if not bool(getattr(self.gateway, "attached", False)):
            raise UpstreamUnavailable(
                f"{REASON_GATEWAY_NOT_ATTACHED}: no gateway adapter is wired in this "
                "deployment; invoke cannot be served"
            )
        # 所有权/协议两根轴先判（它们描述的是「这个后端能不能用」）。
        # 顺序刻意保持在路由门之前：一个既未证明、路由也没生效的后端，应当
        # 得到 `ownership_unverified` / `protocol_unverified`，而不是被路由状态
        # 抢先报成一个看起来无关的错误。
        self._gate_endpoint_backend(policy, endpoint, action="invoke")
        # 可用 != 已路由：只有 Gateway 回读确认过的路由才可转发。
        # 这一步必须**先于**计量与任何 Gateway 调用 —— 被拒的调用不该被记成
        # 一次「已授权的调用」，更不该碰上游。
        self._require_route_applied(endpoint)
        if not isinstance(payload, dict):
            raise InvalidRequest("invoke payload must be an object")
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_INVOKE_PAYLOAD_BYTES:
            raise InvalidRequest(
                f"invoke payload exceeds {MAX_INVOKE_PAYLOAD_BYTES} bytes"
            )

        deployment = policy.require_deployment_read(endpoint.deployment_id)
        engine = policy.require_engine_read(deployment.engine_id)
        model = policy.require_model_read(deployment.model_id)

        # 计量先于转发：授权通过就算一次「被 Registry 记录的调用」，
        # 无论上游随后是否可达。这样计量不会因为上游抖动而丢事件。
        self._audit(
            requester,
            action="invoke",
            object_type="endpoint",
            object_id=endpoint.id,
            detail={
                "deployment_id": deployment.id,
                "model": model.name,
                "payload_bytes": len(encoded),
                "gateway_adapter": getattr(self.gateway, "name", "unknown"),
            },
        )

        result = self.gateway.forward(
            endpoint=endpoint,
            deployment=deployment,
            engine=engine,
            model=model,
            payload=payload,
            # 受信身份（由凭据推出）交给 adapter：只有需要「调用者自己的 Gateway
            # key」的实现会用它，替身忽略即可。**不是**客户端能自报的东西。
            requester=requester,
        )
        data = {
            "endpoint_id": endpoint.id,
            "route_path": endpoint.route_path,
            "model": model.name,
            "deployment_id": deployment.id,
            "gateway": {
                "adapter": getattr(self.gateway, "name", "unknown"),
                "attached": bool(getattr(self.gateway, "attached", False)),
                "authorization": "server_side_enforced",
            },
            "result": result,
            "metering": {
                "recorded": True,
                "source": "registry_audit",
                "tokens": None,
                "latency_ms": None,
                "note": (
                    "Registry 记录的是「经授权的调用次数」；token/延迟属于 Gateway 的"
                    "计量管道，本部署未接入，因此为 null 而不是 0。"
                ),
            },
        }
        return self._envelope(
            data,
            requester,
            notes=["调用经后端授权后交由 Gateway adapter 转发；浏览器不直连上游端口。"],
        )

    # -- 用量 / 日志 / 审计 ------------------------------------------------

    def usage_mine(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        policy.require_read_scope()
        entries = self.store.list_audit(limit=10000)
        mine = [
            entry
            for entry in entries
            if entry.object_type == "endpoint"
            and entry.action == "invoke"
            and entry.actor_principal == requester.principal
        ]
        # 这条计数是**有来源的**：来自本 Registry 自己的审计表。
        metrics = {
            "registry_recorded_invocations": {
                "value": len(mine),
                "state": "healthy",
                "source": "registry_audit",
                "unit": "calls",
                "label": "经授权并通过 Registry 转发的调用次数",
            },
            "tokens": {
                "value": None,
                "state": "unknown",
                "source": None,
                "unit": "tokens",
                "label": "token 用量",
            },
            "latency_ms": {
                "value": None,
                "state": "unknown",
                "source": None,
                "unit": "ms",
                "label": "端到端延迟",
            },
            "ttft_ms": {
                "value": None,
                "state": "unknown",
                "source": None,
                "unit": "ms",
                "label": "首 token 延迟",
            },
        }
        data = {
            "status": dto.USAGE_NOT_CONFIGURED,
            "reason": REASON_NOT_CONFIGURED,
            "pipeline": dto.not_configured_block(what="usage_metrics_pipeline"),
            "metrics": metrics,
            "attribution": {
                "principal": requester.principal,
                "uid": requester.uid,
                "source": "registry_audit_only",
            },
            "notes": [
                "用量管道（token/延迟/TTFT）在本部署**未接入**：这些指标是 null + unknown，不是 0。",
                "唯一有来源的是 Registry 自己记录的调用次数 —— 它不等于 Gateway 的全部流量。",
            ],
        }
        return self._envelope(data, requester)

    def audit_log(self, requester: RequesterContext, *, limit: int = 200) -> dict:
        policy = self.policy(requester)
        policy.require_read_scope()
        if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 1000):
            raise InvalidRequest("limit must be an integer in [1, 1000]")
        entries = self.store.list_audit(limit=limit)
        if not requester.is_admin_read:
            # 普通用户看自己的活动；跨主体审计需要 registry:admin:read。
            entries = [entry for entry in entries if entry.actor_principal == requester.principal]
        return self._envelope(
            {"items": dto.render_audit(entries), "count": len(entries)},
            requester,
            notes=["跨主体审计需要 registry:admin:read；普通主体只看到自己的条目。"],
        )

    def routing_config(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        if not requester.is_admin_read:
            raise AccessDenied("routing config requires registry:admin:read")
        endpoints = self.store.list_endpoints()
        deployments = {item.id: item for item in self.store.list_deployments()}
        engines = {item.id: item for item in self.store.list_engines()}
        models = {item.id: item for item in self.store.list_models()}
        counts = {item.id: len(self.store.grantees(item.id)) for item in endpoints}
        config = gateway_mod.build_routing_config(
            endpoints, deployments, engines, models, counts
        )
        # 「配置发出去了」与「路由器已经按它生效了」是两件事：把还欠回执的那些
        # endpoint 明确点名，这样「有没有生效」不靠猜。
        pending = [
            {
                "endpoint_id": item.id,
                "route_path": item.route_path,
                "current_generation": item.revision,
                "applied_generation": item.router_generation,
            }
            for item in endpoints
            if item.router_sync_pending
        ]
        config["pending_router_sync"] = {
            "count": len(pending),
            "items": pending,
            "note": (
                "这些 Endpoint 的改动还没被任何路由器取走。本仓库不写 Gateway，"
                "所以它们此刻**尚未生效** —— 需要运维消费本契约并回执。"
            ),
        }
        return self._envelope(
            config,
            requester,
            notes=["只读契约：本仓库不写入任何生产 Gateway，也不签发 per-user key。"],
        )

    def service_status(self, requester: RequesterContext) -> dict:
        policy = self.policy(requester)
        data = {
            "registry": {
                "state": "available",
                "schema_version": self.store.schema_version(),
                "mode": self.config.mode,
                "synthetic": self.config.synthetic,
                "dto_schema_version": dto.SCHEMA_VERSION,
            },
            "observation": {
                "prober_attached": self.prober is not None,
                "state": "available" if self.prober is not None else "unknown",
                "reason": None if self.prober is not None else REASON_NOT_CONFIGURED,
            },
            "protocol_observation": {
                "prober": getattr(self.protocol_prober, "name", "unknown"),
                "required_for_ready": bool(self.config.require_protocol_verification),
                "model_identity_paths": list(
                    health_mod.PROTOCOL_MODEL_IDENTITY_PATHS.get(PROTOCOL_OPENAI_COMPAT, ())
                ),
                "health_paths_are_not_identity": list(health_mod.ALLOWED_HEALTH_PATHS),
            },
            "ownership": {
                "prover": getattr(self.ownership_prover, "name", "unknown"),
                "policy": self.config.ownership_policy,
                "enforced": self.config.ownership_enforced,
                "operator_policy_targets": len(self.operator_policy.targets),
                "operator_policy_provenance": self.operator_policy.provenance,
            },
            "gateway": {
                "attached": bool(getattr(self.gateway, "attached", False)),
                "adapter": getattr(self.gateway, "name", "unknown"),
                "state": (
                    "available"
                    if getattr(self.gateway, "attached", False)
                    else "unavailable"
                ),
                "reason": (
                    None
                    if getattr(self.gateway, "attached", False)
                    else gateway_mod.REASON_GATEWAY_NOT_ATTACHED
                ),
            },
            "router": {
                "attached": bool(getattr(self.router, "attached", False)),
                "adapter": getattr(self.router, "name", "unknown"),
                "state": (
                    "available"
                    if getattr(self.router, "attached", False)
                    else "unavailable"
                ),
                "reason": (
                    None
                    if getattr(self.router, "attached", False)
                    else REASON_NO_ROUTER_ADAPTER
                ),
                "snapshot_schema": router_mod.ROUTING_SNAPSHOT_SCHEMA,
            },
            "usage_pipeline": {
                "state": "unknown",
                "reason": REASON_NOT_CONFIGURED,
                "source": None,
            },
            "counts": {
                "engines": len(self.store.list_engines()),
                "models": len(self.store.list_models()),
                "deployments": len(self.store.list_deployments()),
                "endpoints": len(self.store.list_endpoints()),
            },
        }
        return self._envelope(data, requester)

    # -- 供测试/其他层复用的只读助手 ---------------------------------------

    def all_grantees(self, endpoint_id: str) -> tuple[str, ...]:
        return self.store.grantees(endpoint_id)

    def find_endpoint(self, endpoint_id: str) -> Endpoint | None:
        return self.store.endpoint_by_id(endpoint_id)

    def describe_gateway(self) -> dict:
        return {
            "adapter": getattr(self.gateway, "name", "unknown"),
            "attached": bool(getattr(self.gateway, "attached", False)),
        }

    # -- 路由同步（Gateway 快照 + 代数回执）--------------------------------

    def _routing_snapshot(self) -> dict:
        """把当前状态导出成 Gateway 认识的 ``llmlab.endpoint-routing/1`` 快照。"""
        endpoints = self.store.list_endpoints()
        deployments = {item.id: item for item in self.store.list_deployments()}
        engines = {item.id: item for item in self.store.list_engines()}
        models = {item.id: item for item in self.store.list_models()}
        grantees = {item.id: self.store.grantees(item.id) for item in endpoints}

        # 只查真正被引用到的 principal —— 目录是受信的，没查到就是不映射（该行会被排除）。
        referenced: set[str] = set()
        for endpoint in endpoints:
            referenced.add(endpoint.owner_principal)
            referenced.update(grantees[endpoint.id])
        linux_user_by_principal: dict[str, str] = {}
        for principal in referenced:
            found = self.principals.lookup(principal)
            if found is not None:
                linux_user_by_principal[principal] = found.linux_user

        return router_mod.build_routing_snapshot(
            endpoints=endpoints,
            deployments=deployments,
            engines=engines,
            models=models,
            grantees=grantees,
            secret_refs=self.config.engine_secret_refs,
            linux_user_by_principal=linux_user_by_principal,
            generated_at=self._now(),
            source=f"llm-lab-portal registry ({self.config.mode})",
        )

    def _attempt_router_sync(self, endpoint_id: str) -> dict:
        """把当前快照交给 RouterAdapter，并在**有外部证据**时回执。

        返回值是一块诚实的报告：``applied`` 只有在 RouterAdapter 读回 Gateway 自己
        发布、且与本次快照逐位相等的 ``revision`` 与 ``sha256`` 时才为真。任何一环
        缺失（没有 adapter、Gateway 没加载、代数/sha 对不上）都停在 pending。

        回执**只发给这次快照里真的发布了的路由**：一条 Endpoint 即使在代数上已经
        落后得够久，只要它被 ``build_routing_snapshot`` 排除在外，Gateway 就没有
        收到过它，任何「已切流」的记账都是凭空的。
        """
        block: dict = {
            "adapter": getattr(self.router, "name", "unknown"),
            "attached": bool(getattr(self.router, "attached", False)),
            "state": ROUTER_SYNC_PENDING,
            "applied": False,
            "reason": None,
            "detail": None,
            "endpoint_id": endpoint_id,
            "acked_endpoints": [],
        }
        try:
            snapshot = self._routing_snapshot()
        except router_mod.RoutingSnapshotError as exc:
            block["reason"] = router_mod.REASON_SNAPSHOT_INVALID
            block["detail"] = str(exc)
            return block

        result = self.router.publish(snapshot)
        block["published"] = bool(result.published)
        block["snapshot_revision"] = int(snapshot["revision"])
        block["snapshot_sha256"] = result.snapshot_sha256
        block["applied_revision"] = result.applied_revision
        block["reason"] = result.reason
        block["detail"] = result.detail

        if not result.published or not isinstance(result.applied_revision, int):
            return block

        # 有证据了：只回执**这次快照里真的发布了**的 Endpoint，并逐个复核成功率。
        #
        # 仅凭 ``revision <= applied_revision`` 是不够的。``build_routing_snapshot``
        # 会诚实地排除它发不出去的行（engine 没有 secret_ref、engine 未发布、
        # deployment/model 缺失、owner 不在受信目录……），这些 Endpoint **Gateway
        # 从来没收到过**。给它们记一笔 ``router_generation = revision``，等于凭一个
        # 整数说「这条路由已生效」—— 与 PR #89 拆掉的谎报是同一类，只是换了一条路径。
        # 实测（修复前）：快照 ``endpoints == []`` 时，一次 repoint 仍能回执 2 条
        # Endpoint，其中一条读出 ``applied=true`` / ``evidence=gateway_readback``。
        published_endpoint_ids = {entry["endpoint_id"] for entry in snapshot["endpoints"]}
        for endpoint in self.store.list_endpoints():
            if endpoint.id not in published_endpoint_ids:
                continue
            if not endpoint.router_sync_pending or endpoint.revision > result.applied_revision:
                continue
            outcome = self.store.ack_router_generation(
                endpoint.id,
                generation=endpoint.revision,
                actor_principal=SYSTEM_ROUTER_PRINCIPAL,
                actor_uid=SYSTEM_ROUTER_UID,
                at=self._now(),
                # 这次回执的**证据**：RouterAdapter 读回 Gateway 自己发布的
                # revision 与 snapshot SHA，且与本次快照逐位相等。这是本仓库
                # 唯一能说「已切流」的凭据。
                evidence=ROUTER_EVIDENCE_GATEWAY_READBACK,
            )
            if outcome.get("outcome") == "ok":
                block["acked_endpoints"].append(endpoint.id)

        refreshed = self.store.endpoint_by_id(endpoint_id)
        if refreshed is not None:
            block["evidence"] = refreshed.router_ack_evidence
            state = refreshed.router_sync_state()
            block["state"] = state
            block["applied"] = state == ROUTER_SYNC_APPLIED
        return block

    def _apply_router_sync_to_view(self, view: dict, endpoint_id: str) -> None:
        """把一次 repoint/rollback 后的真实同步状态写进返回视图（就地修改）。

        默认（没有 RouterAdapter）下，这一步与旧行为**逐字相同**：状态是
        ``pending_router_sync``，理由是 ``no_router_adapter_in_this_deployment``。
        只有配置了 adapter 且 Gateway 确认过，才会变成 ``router_sync_applied``。
        """
        sync = self._attempt_router_sync(endpoint_id)
        meta = view.setdefault("meta", {})
        meta["router_sync"] = sync["state"]
        meta["router_applied"] = sync["applied"]
        meta["pending_router_sync"] = not sync["applied"]
        meta["router_sync_report"] = sync
        # 回执会改 store 里的 router_generation，而 view 是在回执**之前**渲染的。
        # 不刷新的话，同一个响应里会一边说 router_applied=true、一边 data 里还写着
        # pending_router_sync=true —— 自相矛盾的 DTO 比不返回更糟。
        refreshed = self.store.endpoint_by_id(endpoint_id)
        inner = view.get("data")
        if refreshed is not None and isinstance(inner, dict):
            inner["router_generation"] = refreshed.router_generation
            inner["router_ack_evidence"] = refreshed.router_ack_evidence
            inner["pending_router_sync"] = refreshed.router_sync_pending
            inner["router_sync"] = self._decorate_router_sync(refreshed.router_sync_block())
        if sync["applied"]:
            view.setdefault("notes", []).append(
                "路由已生效：RouterAdapter 读回了 Gateway 自己发布的、与本次快照逐位相等"
                f"的第 {sync.get('applied_revision')} 代与 sha256。"
            )
        else:
            reason = sync.get("reason") or router_mod.REASON_SNAPSHOT_INVALID
            view.setdefault("notes", []).append(
                f"路由**尚未生效**：{reason}。Registry 里改了不算线上改了 —— 要等 "
                "RouterAdapter 从 Gateway 读回确认的代数与 sha256，才会回执生效。"
            )

    def routing_snapshot(self, requester: RequesterContext) -> dict:
        """导出给 Gateway 的 ``llmlab.endpoint-routing/1`` 快照（需要全局只读）。"""
        policy = self.policy(requester)
        if not requester.is_admin_read:
            raise AccessDenied("routing snapshot requires registry:admin:read")
        del policy
        snapshot = self._routing_snapshot()
        snapshot = dict(snapshot)
        snapshot["router"] = {
            "adapter": getattr(self.router, "name", "unknown"),
            "attached": bool(getattr(self.router, "attached", False)),
        }
        return self._envelope(
            snapshot,
            requester,
            notes=[
                "这是写给 Gateway 的快照（只读）；本仓库不写任何生产 Gateway。"
                "不含任何凭据：secret_ref 只点名文件与变量名。"
            ],
        )


def build_service(
    *,
    database: str,
    tokens: Iterable[object] = (),
    principals: Iterable[object] = (),
    config: RegistryConfig | None = None,
    prober: object | None = None,
    gateway: object | None = None,
    protocol_prober: object | None = None,
    ownership_prover: object | None = None,
    router: object | None = None,
    port_checker: Callable[[NormalizedTarget], tuple[bool, str]] | None = None,
    clock: Callable[[], object] = utc_now,
) -> RegistryService:
    """构造一个服务实例。**只建对象、不开端口、不起进程。**"""
    store = Store(database)
    return RegistryService(
        store=store,
        tokens=TokenDirectory(tokens),  # type: ignore[arg-type]
        principals=PrincipalDirectory(principals),  # type: ignore[arg-type]
        config=config,
        prober=prober,
        gateway=gateway,
        protocol_prober=protocol_prober,
        ownership_prover=ownership_prover,
        router=router,
        port_checker=port_checker,
        clock=clock,  # type: ignore[arg-type]
    )
