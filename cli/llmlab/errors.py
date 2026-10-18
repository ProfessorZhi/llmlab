"""CLI 的退出码与错误类型。

退出码是**契约的一部分**：脚本与 CC Agent 靠它判断下一步，而不是靠解析人类文本。
码值刻意区分「你没有权限」与「上游不可达」—— 前者该去找管理员，后者该重试。

    0  OK
    2  用法/配置错误（含**没有凭据**：fail closed，一个请求都不发）
    3  认证/授权失败（401 / 403）
    4  对象不存在**或**无权知道其存在（服务端刻意不区分）
    5  冲突（重复登记、幂等键被复用、未验证不可共享）
    6  上游不可达 / 未接线（503）
    7  传输层失败（连不上、超时、响应不是 JSON）
    8  服务端内部错误（5xx）
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_UPSTREAM = 6
EXIT_TRANSPORT = 7
EXIT_SERVER = 8


class CliError(Exception):
    """CLI 层错误：带退出码，并且**永不**携带凭据。"""

    def __init__(self, exit_code: int, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message
        self.hint = hint


def exit_code_for_status(status: int) -> int:
    """HTTP 状态 -> 退出码。"""
    if status in (401, 403):
        return EXIT_AUTH
    if status == 404:
        return EXIT_NOT_FOUND
    if status in (409, 411, 413, 415):
        return EXIT_CONFLICT
    if status == 503:
        return EXIT_UPSTREAM
    if 400 <= status < 500:
        return EXIT_USAGE
    return EXIT_SERVER
