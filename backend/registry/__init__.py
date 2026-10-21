"""Registry：Engine / Model / Deployment / Endpoint 的登记、验证与跨用户共享。

平台简化设计的落点（见 ``docs/registry-cli-contract.md``）：

* 注册、启动、共享是**三件事**：三个状态字段、三条判定，不是同一个字符串的三种取值；
* owner 只能由服务端受信目录从**凭据**推出，请求里的 ``owner``/``uid``/``role``
  一律拒绝；
* 权重路径是**受限登记引用**，Registry 从不打开它；
* engine 目标 URL 只接受**同主机 loopback + 端口 allowlist**（SSRF 防线）；
* 默认私有，共享必须显式；撤销立即生效；
* 共享只传递「可以调用」和必要的元数据，不传递 owner 的家目录结构与本机端口表。

与既有子系统的关系：本包**不**改动 ``backend.portal.modules`` 的
``EndpointRecord`` / ``portal:endpoints:share`` 语义，而是使用独立的 schema 版本
（``registry/1``）与 scope 命名空间（``registry:*``）。两者折叠需要一次显式的
集成决定，不属于本包的范围。

本包**不**：启动常驻服务、读生产 API key、写生产 Gateway、触碰 GPU、
打开权重文件、执行任意 shell、下载未受信模型。
"""

from __future__ import annotations

from .errors import (
    AccessDenied,
    Conflict,
    IdentityAssertionRejected,
    InvalidRequest,
    NotFound,
    NotVerified,
    OwnershipUnverified,
    ProtocolUnverified,
    RegistryError,
    RevisionConflict,
    StorageError,
    Unauthenticated,
    UpstreamUnavailable,
)
from .health import (
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_UNKNOWN,
    STATE_UNREACHABLE,
    HEALTH_STATES,
    PROTOCOL_UNKNOWN,
    PROTOCOL_UNVERIFIED,
    PROTOCOL_VERIFIED,
    LoopbackHttpProber,
    LoopbackProtocolProber,
    NoProtocolProber,
    ProbeResult,
    ProtocolObservation,
    StaticProber,
    StaticProtocolProber,
    build_verification,
    parse_model_list,
)
from .ownership import (
    EMPTY_POLICY,
    OWNERSHIP_OPERATOR_VERIFIED,
    OWNERSHIP_POLICIES,
    OWNERSHIP_POLICY_ADVISORY,
    OWNERSHIP_POLICY_ENFORCED,
    OWNERSHIP_PROVER_KINDS,
    OWNERSHIP_UID_VERIFIED,
    OWNERSHIP_UNVERIFIED,
    ListeningSocket,
    NoOwnershipProver,
    OperatorPolicy,
    OperatorTarget,
    OwnershipProof,
    ProcNetListenerTable,
    ProcNetOwnershipProver,
    StaticOwnershipProver,
    load_operator_policy,
    operator_targets_for_fixture,
)
from .identity import (
    SCOPE_ADMIN_MANAGE,
    SCOPE_ADMIN_READ,
    SCOPE_ENDPOINT_INVOKE,
    SCOPE_ENDPOINT_SHARE,
    SCOPE_REGISTRY_READ,
    SCOPE_REGISTRY_WRITE,
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TokenBinding,
    TokenDirectory,
    resolve_requester,
)
from .registry import RegistryConfig, RegistryService, build_service, new_id
from .store import SCHEMA_VERSION, Store

__all__ = [
    "AccessDenied",
    "Conflict",
    "EMPTY_POLICY",
    "HEALTH_STATES",
    "IdentityAssertionRejected",
    "InvalidRequest",
    "ListeningSocket",
    "LoopbackHttpProber",
    "LoopbackProtocolProber",
    "NoOwnershipProver",
    "NoProtocolProber",
    "NotFound",
    "NotVerified",
    "OWNERSHIP_OPERATOR_VERIFIED",
    "OWNERSHIP_POLICIES",
    "OWNERSHIP_POLICY_ADVISORY",
    "OWNERSHIP_POLICY_ENFORCED",
    "OWNERSHIP_PROVER_KINDS",
    "OWNERSHIP_UID_VERIFIED",
    "OWNERSHIP_UNVERIFIED",
    "OperatorPolicy",
    "OperatorTarget",
    "OwnershipProof",
    "OwnershipUnverified",
    "PROTOCOL_UNKNOWN",
    "ProcNetListenerTable",
    "ProcNetOwnershipProver",
    "PROTOCOL_UNVERIFIED",
    "PROTOCOL_VERIFIED",
    "Principal",
    "PrincipalDirectory",
    "ProbeResult",
    "ProtocolObservation",
    "ProtocolUnverified",
    "RegistryConfig",
    "RegistryError",
    "RegistryService",
    "RequesterContext",
    "RevisionConflict",
    "SCHEMA_VERSION",
    "SCOPE_ADMIN_MANAGE",
    "SCOPE_ADMIN_READ",
    "SCOPE_ENDPOINT_INVOKE",
    "SCOPE_ENDPOINT_SHARE",
    "SCOPE_REGISTRY_READ",
    "SCOPE_REGISTRY_WRITE",
    "STATE_DEGRADED",
    "STATE_HEALTHY",
    "STATE_UNKNOWN",
    "STATE_UNREACHABLE",
    "StaticOwnershipProver",
    "StaticProber",
    "StaticProtocolProber",
    "StorageError",
    "Store",
    "TokenBinding",
    "TokenDirectory",
    "Unauthenticated",
    "UpstreamUnavailable",
    "build_service",
    "build_verification",
    "load_operator_policy",
    "new_id",
    "operator_targets_for_fixture",
    "parse_model_list",
    "resolve_requester",
]
