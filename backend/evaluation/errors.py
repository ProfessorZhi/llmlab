"""EvalScope 评测中心契约的失败模式。

沿用 ``backend.registry.errors`` / ``backend.atlas.errors`` 的纪律：本模块**不**决定
传输层。每个错误只携带一个稳定的、可审计的 ``contract_code``；把它映射成 HTTP 状态码
是 ``api.py`` 的事（映射表在 ``api.CONTRACT_STATUS``，契约层自己从不发 HTTP）。

三条值得单独说明的取舍：

``NotFound`` 同时表示「不存在」与「存在但你无权知道它存在」
    二者**必须**共用一个错误。一旦分开，``GET /jobs/{id}`` 就成了一个可以逐 id 枚举
    他人评测任务存在性的预言机。这与 ``backend.registry.errors.NotFound`` 的取舍一致。

``DispatchForbidden`` 与 ``RunnerUnavailable`` 是两件事
    前者是**策略**说「不允许派发」（GPU 满、管理员没开、不在时间窗内），后者是
    **能力**说「没有可用的真实 runner」。二者都不降级成 200 + 假数据：评测队列可以
    接受一个排队中的任务，但绝不据此声称跑过一次真实评测。

``ReportPathRejected`` 是拒绝而不是「截断成合法路径」
    报告引用来自数据库，但数据库里的字符串可能被上游写坏。一个越界的引用是**错误**，
    不是「尽力修正后继续」——修正路径正是目录穿越得以成立的那一步。
"""

from __future__ import annotations


class EvaluationError(Exception):
    """评测中心契约错误的基类。

    ``expose_message`` 决定这条错误的消息能否原样发给调用方。绝大多数契约错误可以
    （它们说的就是调用方做错了什么），但**存储层**不行：它的话术里会出现数据库路径、
    表名、约束名这些内部结构。那些进日志，不进响应。
    """

    contract_code = "evaluation.error"
    expose_message = True


class InvalidRequest(EvaluationError):
    """请求参数类型/取值非法（非授权类）。"""

    contract_code = "evaluation.invalid_request"


class IdentityAssertionRejected(InvalidRequest):
    """请求试图**自报身份**（owner / uid / role / is_admin / 相关 header）。

    这不是「忽略了一个多余字段」，而是拒绝整个请求：身份只能由服务端可信目录从凭据
    推出。静默忽略会让一次伪造尝试在日志里消失，用户也无从知道自己用错了客户端。
    """

    contract_code = "evaluation.identity_assertion_rejected"


class Unauthenticated(EvaluationError):
    """没有凭据，或凭据无法在受信目录里解析出主体。"""

    contract_code = "evaluation.unauthenticated"


class AccessDenied(EvaluationError):
    """身份已确认、对象也**看得见**，但这次操作不被允许。

    只用于「调用者已经知道这个对象存在」的情形（例如持有 ``evaluation:admin:read``
    因而看得到全部任务，但没有 ``evaluation:admin:manage``）。看不见的对象一律走
    :class:`NotFound`，以免把拒绝变成一个存在性预言机。
    """

    contract_code = "evaluation.access_denied"

    def __init__(self, detail: str = "", *, reason: str | None = None) -> None:
        super().__init__(detail)
        self.reason = reason


class NotFound(EvaluationError):
    """不存在，或存在但调用者无权知道其存在（刻意不区分）。"""

    contract_code = "evaluation.not_found"


class Conflict(EvaluationError):
    """与当前状态冲突：非法状态迁移、幂等键被不同内容复用、重复对象。"""

    contract_code = "evaluation.conflict"


class RateLimited(EvaluationError):
    """超出提交配额。``retry_after`` 是秒数，供客户端与 UI 展示。"""

    contract_code = "evaluation.rate_limited"

    def __init__(self, detail: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.retry_after = retry_after


class OutsideTimeWindow(EvaluationError):
    """当前时刻不在允许的时间窗内。"""

    contract_code = "evaluation.outside_time_window"


class TargetNotReady(EvaluationError):
    """评测目标（deployment / endpoint）尚未验证就绪，因而不处于可被评测的状态。

    「登记过」与「验证过」是两件事：注册只说明元数据进了库，不说明它真的回应过。
    """

    contract_code = "evaluation.target_not_ready"


class TargetNotAuthorized(EvaluationError):
    """调用者对该评测目标没有授权（不是 owner，也没有被共享给）。

    与 :class:`NotFound` 一样**不泄露**目标是否存在：未授权即「查无此目标」。
    """

    contract_code = "evaluation.target_not_authorized"


class SharedTargetGpuStressRefused(EvaluationError):
    """对**他人共享**的目标默认不做 GPU 压测（throughput / latency）。

    共享的语义是「你可以调用它」，不是「你可以拿它跑满显存」。没有 owner 或管理员的
    显式授予时，压测类评测一律拒绝——这是拒绝，不是降级成一个大得多的 correctness。
    """

    contract_code = "evaluation.shared_target_gpu_stress_refused"


class DispatchForbidden(EvaluationError):
    """策略层不允许派发到真实 runner（GPU 满 / 管理员未开 / 不在时间窗内）。"""

    contract_code = "evaluation.dispatch_forbidden"


class RunnerUnavailable(EvaluationError):
    """没有可用的真实 runner（未接线 / 能力探测未通过 / 版本未知）。"""

    contract_code = "evaluation.runner_unavailable"


class NotConfigured(EvaluationError):
    """本部署没有接上这条来源。

    「没接线」是一个**如实的状态**，不是 0，也不是空列表：DTO 里对应的取值是
    ``None`` + ``not_configured``，UI 据此显示「等待数据接入」而不是「得分为 0」。
    """

    contract_code = "evaluation.not_configured"


class ReportPathRejected(EvaluationError):
    """报告引用不是一个可接受的、只读安全的相对路径（越界 / 绝对 / 含穿越 / 后缀不允许）。"""

    contract_code = "evaluation.report_path_rejected"


class ReportSourceMalformed(EvaluationError):
    """一条来源快照或其中的记录不自洽（未知来源、来源混装、状态与条数矛盾、负载字段非法）。

    这是**上游/装配**的问题，不是调用方做错了什么，因此消息不外发（里面会出现上游的
    字段名）。适配器读到畸形负载时应当在内部把它转成一条 ``unreachable`` 快照，而不是
    让这个异常穿到 HTTP 面上变成 400。
    """

    contract_code = "evaluation.report_source_malformed"
    expose_message = False


class ReportDownloadRefused(EvaluationError):
    """一次报告下载被**有界**策略拒绝：文件超出声明上限、不是普通文件、或此刻不在。

    与 :class:`ReportPathRejected` 刻意分开：那个说的是"这个**引用**不是一个可接受的
    相对路径"，是调用方/配置的问题；这一条说的是"引用没问题，但这份产物**不能按这个
    上限交出去**"。把两者并成一个，会让"报告太大"看起来像"你写了个非法路径"。

    ``reason`` 是一个稳定 token，供 UI 与审计区分具体是哪一条上界。
    """

    contract_code = "evaluation.report_download_refused"

    def __init__(self, detail: str = "", *, reason: str | None = None) -> None:
        super().__init__(detail)
        self.reason = reason


class InternalError(EvaluationError):
    """我们自己的契约被违反了（例如一份 DTO 偏离了它自己的字段清单）。

    刻意与 :class:`InvalidRequest` 分开：调用方没有做错任何事，所以它**不能**是 400。
    把内部 bug 报成"你的请求有错"，既误导用户，又让一个真实缺陷在监控里看起来像噪声。
    消息不外发（里面有内部字段名）。
    """

    contract_code = "evaluation.internal_error"
    expose_message = False


class StorageError(EvaluationError):
    """持久化层失败（迁移不匹配、约束冲突、数据库不可用）。

    刻意**不**把消息外发：这里面有库路径与 schema 细节，属于内部结构。
    """

    contract_code = "evaluation.storage_error"
    expose_message = False


__all__ = [
    "AccessDenied",
    "Conflict",
    "DispatchForbidden",
    "EvaluationError",
    "IdentityAssertionRejected",
    "InternalError",
    "InvalidRequest",
    "NotFound",
    "NotConfigured",
    "OutsideTimeWindow",
    "RateLimited",
    "ReportDownloadRefused",
    "ReportPathRejected",
    "ReportSourceMalformed",
    "RunnerUnavailable",
    "SharedTargetGpuStressRefused",
    "StorageError",
    "TargetNotAuthorized",
    "TargetNotReady",
    "Unauthenticated",
]
