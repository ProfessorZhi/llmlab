"""每用户代理：把「我这台机器上跑着什么、装了什么」报给平台。

它**不持有任何凭据**，这是设计要点而不是省事：令牌能证明「某个主体签过一条
凭据」，证明不了「这条连接来自以那个 uid 运行的进程」。平台的 unix socket 用
``SO_PEERCRED`` 从内核取对端 uid，所以这个命令不需要密码、不需要发牌、不写
任何密钥文件，可以配成一个普通的用户级 systemd 单元。

它只报**自己的**东西：``LocalDiscovery`` 以 ``restrict_uid=os.getuid()`` 运行，
``/proc/net/tcp`` 里别人的监听一律不看，``stat`` 不属主的路径一律跳过。

上报的条目里**没有** ``uid`` / ``run_as_user`` / ``target_url`` / ``registered``
这类字段：服务端不读它们，带的条目会被整条丢弃（那是伪造归属的形状）。代理只
说「端口 8000 开着、跑的是 vllm」，归属由内核与服务端表定。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path

from backend.registry.agent_reports import MAX_PROBLEMS, REPORT_VERSION
from backend.registry.local_discovery import LocalDiscovery
from backend.registry.models import utc_now

from .errors import CliError, EXIT_AUTH, EXIT_CONFLICT, EXIT_TRANSPORT, EXIT_USAGE

#: 与 ``backend.bff.devserver.DEFAULT_AGENT_SOCKET`` 同一个位置。
DEFAULT_SOCKET = "/run/llmlab/agent.sock"

#: 一条消息（含收尾换行）的上限，与服务端一致。
MAX_MESSAGE_BYTES = 1024 * 1024

#: 连不上 / 没有回执的等待上限。socket 在本机，正常是毫秒级。
DEFAULT_TIMEOUT = 15.0

#: 扫描被截断的标志前缀。出现任意一个，``full`` 就是 ``False``。
_TRUNCATION_PREFIXES = ("model_walk_truncated:", "env_walk_truncated:", "model_candidates_capped:")

_AGENT_ID = re.compile(r"[^A-Za-z0-9._-]+")


def default_agent_id() -> str:
    """主机名去域、去怪字符。**同一个人在不同机器上应该是不同的 agent_id。**"""

    host = socket.gethostname().split(".")[0]
    cleaned = _AGENT_ID.sub("-", host).strip("-._")[:64]
    return cleaned or "agent"


def default_roots() -> tuple[str, ...]:
    """只看自家目录：``~``，加上 ``/data/home/<me>``（存在才算）。

    ``/data``、``/srv`` 这类共享根**不**默认扫：以用户身份跑的时候，扫共享盘等于
    把别人的模型也算进「我的」。真要报共享盘上的权重，用 ``--root`` 显式点名。
    """

    roots = [str(Path.home())]
    try:
        import pwd

        name = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):  # pragma: no cover - 没有 NSS 的容器
        name = os.environ.get("USER", "")
    if name:
        tail = Path("/data/home") / name
        if tail.is_dir():
            roots.append(str(tail))
    return tuple(roots)


def collect_report(
    *,
    agent_id: str | None = None,
    roots: tuple[str, ...] | None = None,
    discovery: object | None = None,
    scanned_at: datetime | None = None,
) -> dict:
    """扫一遍本机，得到一份可以直接发出去的 payload。不写任何东西。"""

    scanner = (
        discovery
        if discovery is not None
        else LocalDiscovery(
            # 代理不受登记端口允许列表限制：它只报**自己**的监听，而「这个端口能
            # 不能登记」由服务端在回执里说（registrable）。
            allowed_ports=None,
            model_roots=roots if roots is not None else default_roots(),
            restrict_uid=os.getuid(),
        )
    )

    engines, engine_problem = scanner.engines(registered_targets={})
    models, model_problems = scanner.models(registered_paths={})
    installed, installed_problems = scanner.installed_engines()

    problems: list[str] = []
    if engine_problem:
        problems.append(engine_problem)
    problems.extend(model_problems)
    problems.extend(installed_problems)
    full = not any(problem.startswith(_TRUNCATION_PREFIXES) for problem in problems)

    return {
        "report_version": REPORT_VERSION,
        "agent_id": agent_id or default_agent_id(),
        "scanned_at": (scanned_at or utc_now()).isoformat(),
        "full": full,
        # 只发「是什么」，不发「归谁」：后者由内核与服务端写死。
        "engines": [
            {
                "port": item.port,
                "framework": item.framework,
                "models": list(item.models),
                "suggested_name": item.suggested_name,
            }
            for item in engines
        ],
        "installed_engines": [
            {
                "engine": item.engine,
                "name": item.name,
                "found_as": item.found_as,
                "version": item.version,
                "path": item.path,
                "env": item.env,
            }
            for item in installed
        ],
        "models": [
            {
                "path": item.path,
                "format": item.format,
                "size_bytes": item.size_bytes,
                "architecture": item.architecture,
                "model_type": item.model_type,
                "suggested_name": item.suggested_name,
            }
            for item in models
        ],
        "problems": problems[:MAX_PROBLEMS],
    }


# -- 传输 -------------------------------------------------------------------


def _encode(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _read_reply(reader) -> dict:
    line = reader.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        raise CliError(EXIT_TRANSPORT, "the agent channel closed without a reply")
    if len(line) > MAX_MESSAGE_BYTES:
        raise CliError(EXIT_TRANSPORT, "the agent channel sent an oversized reply")
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CliError(EXIT_TRANSPORT, "the agent channel replied with something that is not JSON")
    if not isinstance(reply, dict):
        raise CliError(EXIT_TRANSPORT, "the agent channel replied with something that is not an object")
    return reply


def send_report(
    payload: dict, *, socket_path: str = DEFAULT_SOCKET, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """把一份 payload 发出去，返回服务端回执里的 ``data``。

    协议：一行 hello，一行 report。每行一个 JSON 对象，回复同理。
    """

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        try:
            connection.connect(socket_path)
        except FileNotFoundError:
            raise CliError(
                EXIT_TRANSPORT,
                f"no agent socket at {socket_path}",
                hint=(
                    "平台侧还没开这条通道：需要运维执行装机脚本建立 /run/llmlab "
                    "并重启服务；然后用 --socket 指向实际路径"
                ),
            ) from None
        except (ConnectionRefusedError, PermissionError, OSError) as exc:
            raise CliError(
                EXIT_TRANSPORT,
                f"cannot connect to the agent socket at {socket_path}: {exc.strerror or exc}",
            ) from None

        reader = connection.makefile("rb")
        try:
            connection.sendall(_encode({"type": "hello", "protocol_version": 1}))
            hello = _read_reply(reader)
            if not hello.get("ok"):
                raise _server_error(hello)
            connection.sendall(_encode({"type": "report", **payload}))
            reply = _read_reply(reader)
        except socket.timeout:
            raise CliError(EXIT_TRANSPORT, "the agent channel did not answer in time") from None
        finally:
            reader.close()
    finally:
        connection.close()

    if not reply.get("ok"):
        raise _server_error(reply)
    data = reply.get("data")
    if not isinstance(data, dict):
        raise CliError(EXIT_TRANSPORT, "the agent channel replied ok but without a receipt")
    return data


def _server_error(reply: dict) -> CliError:
    error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
    code = error.get("code") or "agent.error"
    message = error.get("message") or "the agent channel refused the report"
    if code == "agent.uid_not_mapped":
        hint = (
            "这个 Linux 账户在平台的主体目录里没有对应的身份：需要管理员先把账户"
            "开通/批准，内核给的是 uid，平台只认它映射得到的人"
        )
        return CliError(EXIT_AUTH, f"{message} ({code})", hint=hint)
    if code.startswith("agent."):
        return CliError(EXIT_TRANSPORT, f"{message} ({code})")
    # 其余是 Registry 的契约错误码：按 HTTP 那条路的同一套语义退出。
    if code in ("registry.access_denied", "registry.unauthenticated"):
        return CliError(EXIT_AUTH, f"{message} ({code})")
    if code == "registry.conflict":
        return CliError(EXIT_CONFLICT, f"{message} ({code})")
    return CliError(EXIT_USAGE, f"{message} ({code})")


def report_once(
    *,
    socket_path: str = DEFAULT_SOCKET,
    agent_id: str | None = None,
    roots: tuple[str, ...] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[dict, dict]:
    """扫一次、报一次。返回 ``(payload, receipt)`` —— 两者都适合直接打印。"""

    payload = collect_report(agent_id=agent_id, roots=roots)
    return payload, send_report(payload, socket_path=socket_path, timeout=timeout)


def human_summary(payload: dict, receipt: dict) -> str:
    """一段给人看的话。字段全部来自服务端回执或本地扫描，不含路径以外的事实。"""

    accepted = receipt.get("accepted") or {}
    lines = [
        f"agent {receipt.get('agent_id')} -> host {receipt.get('host_id')} "
        f"(uid {os.getuid()}) at {receipt.get('received_at')}",
        f"不再上报的有 {len(payload.get('problems') or [])} 条扫描问题",
    ]
    if payload.get("full") is False:
        lines.append("本次扫描**不完整**（目录条目预算用完），已如实标记 full=false")
    kept = (
        f"收下：engines {accepted.get('engines', 0)} / "
        f"installed {accepted.get('installed_engines', 0)} / models {accepted.get('models', 0)}"
    )
    lines.append(kept)
    rejected = receipt.get("rejected") or {}
    if rejected:
        detail = ", ".join(
            f"{bucket}: {reason} x{count}"
            for bucket, reasons in sorted(rejected.items())
            for reason, count in sorted(reasons.items())
        )
        lines.append(f"丢弃：{detail}")
    return "\n".join(lines)


def describe(payload: dict) -> None:
    """``--dry-run``：只打印将要发送的内容，不连 socket。"""

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def warn(message: str) -> None:
    print(f"llmlab agent: {message}", file=sys.stderr)


__all__ = [
    "DEFAULT_SOCKET",
    "MAX_MESSAGE_BYTES",
    "collect_report",
    "default_agent_id",
    "default_roots",
    "human_summary",
    "report_once",
    "send_report",
]
