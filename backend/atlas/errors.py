"""Atlas 数据契约的失败模式。

这里刻意**不**绑定 HTTP 状态码：本模块不是 HTTP 服务，将来接线 BFF 时由
BFF 自己把这些契约错误映射成传输层状态（见 docs/atlas-data-contract.md）。
每个错误只携带一个稳定的、可审计的 ``contract_code`` 字符串。
"""

from __future__ import annotations


class ContractError(Exception):
    """Atlas 数据契约错误的基类。"""

    contract_code = "atlas.contract_error"


class AccessDenied(ContractError):
    """身份缺失、scope 不足、或请求了未授权项目。

    任何情况下都**不**回退到 admin —— 拒绝就是拒绝。
    """

    contract_code = "atlas.access_denied"


class InputLimitExceeded(ContractError):
    """输入超过行数 / 字节数 / 窗口长度上限（滥用拒绝）。"""

    contract_code = "atlas.input_limit_exceeded"


class InvalidWindow(ContractError):
    """时间窗口非法：缺时区、``to <= from``、或超出最大跨度。"""

    contract_code = "atlas.invalid_window"


class InvalidBucket(ContractError):
    """不支持的桶粒度。"""

    contract_code = "atlas.invalid_bucket"


class InvalidRequest(ContractError):
    """请求参数类型/取值非法（非授权类）。"""

    contract_code = "atlas.invalid_request"


class UnverifiedSource(ContractError):
    """声明为**非合成**的来源名不在受信白名单里。

    与 :class:`InvalidRequest` 分开，是因为这两件事的排查方向不同：「参数写错了」
    与「有人往系统里接了一个没人审批过的数据源」。后者是一条安全边界，不是一次笔误。
    """

    contract_code = "atlas.unverified_source"


class SourceUnavailable(ContractError):
    """服务端受信来源（Key Directory / Gateway 语料）读不出来，或读出来的不可信。

    **这是 fail closed 的信号。** 它回答的是「本部署没有这条来源」，而不是
    「这条来源回了空」：两者在数据上无法区分，但结论完全相反 —— 前者是接线问题，
    后者是一个测量结果。上层必须据此把端口置为 detach / 503，**不能**把它降级成
    一份空语料再走正常路径，否则「没接线」会被渲染成「你最近没有调用」。

    与 :class:`UnverifiedSource` 的分工：那一条是「来源没人审批过」，这一条是
    「来源已经指定，但读不到 / 有界检查没过 / 内容不可信」。

    ``reason`` 是稳定的机器可读码；与 ``contract_code`` 分开，是因为前者用来区分
    「缺文件」与「越界」这类排查方向，后者只回答「这是哪一类错误」。
    """

    contract_code = "atlas.source_unavailable"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
