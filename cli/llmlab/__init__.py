"""``llmlab`` —— Atelier Observatory 平台 Registry 的命令行客户端。

给 CC Agent 用的形态与给人类用的形态是同一份数据：``--json`` 输出服务端契约信封
原样，不加解释、不补默认值。**没有来源的数字不会出现在输出里** —— 未观测一律是
``null`` 与 ``unknown``，不是 ``0``。

安全姿势（见 ``client.py`` 与 ``docs/registry-cli-contract.md``）：

* 凭据只来自 Portal 签发的 token；没有就退出码 2，**不发请求**；
* 不读 Linux 账密、不调 ``sudo``、不开远端 shell；
* 明文 ``http://`` 只允许 loopback；
* token 从不进日志、异常消息或 ``--json`` 输出。

``invoke`` 只到**授权网关**：服务端先做行级 ACL，再由 Gateway adapter 转发；
CLI 不直连上游推理端口，也不持有 Gateway 的 key。
"""

from __future__ import annotations

__all__ = ["main", "run", "build_parser"]

from .entry import build_parser, main, run
