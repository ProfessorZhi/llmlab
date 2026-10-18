"""监控数据契约的失败模式。

与 ``backend/atlas/errors.py`` 同构：本模块**不**绑定 HTTP 状态码，只携带一个稳定的
``contract_code``；将来接线 BFF 时由 BFF 自己把它们映射成传输层状态。

这里额外区分两类「拒绝」，因为它们的含义**不同**、上层处理也不同：

* :class:`AccessDenied` —— 身份缺失 / scope 不足 / 请求了未授权对象。**fail closed**，
  永不回退到 admin，永不用「返回空结果」代替拒绝。
* :class:`UnknownMetric` —— 指标名不在**固定表达式登记表**里。这不是授权问题，而是
  「你要的东西本模块结构上不提供」。单独一类是为了让「没有这个指标」与「你没权限看」
  不会在日志里混成一条 —— 混起来等于给了个枚举他人指标的预言机。
"""

from __future__ import annotations


class ContractError(Exception):
    """监控数据契约错误的基类。"""

    contract_code = "observability.contract_error"


class AccessDenied(ContractError):
    """身份缺失、scope 不足、或请求了未授权的对象（用户 / 引擎 / 过滤器取值）。

    任何情况下都**不**回退到 admin —— 拒绝就是拒绝。
    """

    contract_code = "observability.access_denied"


class UnknownMetric(ContractError):
    """指标名不在固定登记表内。"""

    contract_code = "observability.unknown_metric"


class InputLimitExceeded(ContractError):
    """输入超过行数 / 字节数上限（滥用拒绝）。"""

    contract_code = "observability.input_limit_exceeded"


class InvalidWindow(ContractError):
    """时间窗口非法：缺时区、``to <= from``、或超出最大跨度。"""

    contract_code = "observability.invalid_window"


class InvalidBucket(ContractError):
    """不支持的桶粒度。"""

    contract_code = "observability.invalid_bucket"


class InvalidRequest(ContractError):
    """请求参数类型/取值非法（非授权类）。"""

    contract_code = "observability.invalid_request"


class SourceUnavailable(ContractError):
    """指标来源（Prometheus / NVML / 主机采集）不可达。

    注意：**不可达不等于 0**。捕获此错误的调用方必须产出 ``quality=unknown`` 且带
    ``reason`` 的读数，绝不允许把它折算成一个数字。
    """

    contract_code = "observability.source_unavailable"


class ConfigRejected(ContractError):
    """配置被拒绝（例如 provider 的 base URL 不是环回 / 不在允许列表内）。

    这类错误在**构造期**抛出 —— 一个指向任意外网地址的 provider 根本不该被造出来。
    """

    contract_code = "observability.config_rejected"
