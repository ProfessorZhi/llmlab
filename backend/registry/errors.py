"""Registry 契约的失败模式。

与 ``backend.atlas.errors`` 同纪律：本模块**不**决定传输层。每个错误只携带一个
稳定的、可审计的 ``contract_code``；把它映射成 HTTP 状态码是 ``api.py`` 的事
（映射表在 ``api.CONTRACT_STATUS``，契约层自己从不发 HTTP）。

``NotFound`` 的存在理由值得单独说明：它同时表示「不存在」与「存在但你无权知道它
存在」。二者**必须**共用一个错误 —— 一旦分开，端点就成了一个可以逐 id 枚举他人
资源存在性的预言机（同 ``backend.atlas.identity.resolve_own_call_scope`` 的取舍）。
"""

from __future__ import annotations


class RegistryError(Exception):
    """Registry 契约错误的基类。

    ``expose_message`` 决定这条错误的消息能否原样发给调用方。绝大多数契约错误
    可以（它们说的就是调用方做错了什么），但**存储层**不行：它的话术里会出现
    数据库路径、表名、约束名这些内部结构。那些进日志，不进响应。
    """

    contract_code = "registry.error"
    expose_message = True


class InvalidRequest(RegistryError):
    """请求参数类型/取值非法（非授权类）。"""

    contract_code = "registry.invalid_request"


class IdentityAssertionRejected(InvalidRequest):
    """请求试图**自报身份**（owner / uid / principal / role / header）。

    这不是「忽略了一个多余字段」，而是拒绝整个请求：身份只能由服务端可信目录从
    凭据推出。静默忽略会让一次伪造尝试在日志里消失，用户也无从知道自己用错了客户端。
    """

    contract_code = "registry.identity_assertion_rejected"


class Unauthenticated(RegistryError):
    """没有凭据，或凭据无法在受信目录里解析出主体。"""

    contract_code = "registry.unauthenticated"


class TokenAuthorityUnavailable(InvalidRequest):
    """本部署的 token 权威**没有被正确装配**，或它此刻无法回答。

    它与 ``Unauthenticated`` 是两件事：后者是「这条凭据不对」，换一条可能就好了；
    前者是「这台服务不知道该问谁」，换任何凭据都不会好。所以传输层把它映射成
    **503**，而不是 401 —— 让运维看见的是一个未配置/不可用的权威，而不是一次
    可疑的登录失败。

    继承 ``InvalidRequest`` 沿用本文件里 ``IdentityAssertionRejected`` 的既有写法：
    它仍然是契约层的「请求边界」错误，而线上判据是 ``contract_code``。
    消息里**永不**回显凭据。
    """

    contract_code = "registry.token_authority_unavailable"


class AccessDenied(RegistryError):
    """身份已确认，但这次操作不被允许，且调用者**知道**该资源的存在。"""

    contract_code = "registry.access_denied"


class NotFound(RegistryError):
    """不存在，或存在但调用者无权知道其存在（刻意不区分）。"""

    contract_code = "registry.not_found"


class Conflict(RegistryError):
    """与当前状态冲突：重复对象、幂等键被不同内容复用、非法状态迁移。"""

    contract_code = "registry.conflict"


class NotVerified(RegistryError):
    """端点/部署尚未通过验证，因而不处于可共享、可调用的状态。

    验证与「注册了 HTTP endpoint」是**强绑定**的：注册只说明登记过元数据，
    不说明它真的回应过。
    """

    contract_code = "registry.not_verified"


class RevisionConflict(Conflict):
    """乐观并发冲突：调用方手上的 ``revision`` 已经不是当前值。

    与普通 ``Conflict`` 分开，是因为它要求**客户端换一个动作**（重读再重试），
    而不是「换个名字」或「去找管理员」。CLI 靠这个码决定是否自动重试。
    """

    contract_code = "registry.revision_conflict"


class OwnershipUnverified(RegistryError):
    """引擎的所有权没有证明，因而不可共享/调用。

    「没有证明」与「证明失败」在这里是同一件事：两者都不足以授权。
    """

    contract_code = "registry.ownership_unverified"


class ProtocolUnverified(RegistryError):
    """传输可达，但**协议**没验证是这个部署声明的模型服务。

    2xx 只说明某个东西在监听。它不是「模型在这儿」的证据。
    """

    contract_code = "registry.protocol_unverified"


class UpstreamUnavailable(RegistryError):
    """上游服务不可达（调用路径）。**不**降级成 200 + 假数据。"""

    contract_code = "registry.upstream_unavailable"


class StorageError(RegistryError):
    """持久化层失败（迁移不匹配、约束冲突、数据库不可用）。

    刻意**不**把消息外发：这里面有库路径与 schema 细节，属于内部结构。
    """

    contract_code = "registry.storage_error"
    expose_message = False
