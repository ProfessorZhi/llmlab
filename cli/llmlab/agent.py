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
import subprocess
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

#: 与 ``backend.registry.registry`` 同一条白名单：单元名里不允许路径分隔符或空白。
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

#: 平台能下发的动作。代理只把这三个词映射到 ``systemctl --user``。
TASK_KINDS = ("start", "stop", "restart")

#: 一轮最多领多少条任务，与服务端默认值一致。
DEFAULT_TASK_LIMIT = 8

#: 阻塞命令的最长执行时间。超过就回报 failed，而不是让 watch 循环卡住。
DEFAULT_TASK_TIMEOUT = 120.0

#: 回传的 stdout/stderr 截断长度。任务是「控制」不是「取日志」，不该搬走整份输出。
MAX_TASK_OUTPUT = 4096


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
    models, model_problems = scanner.models(registered_paths={}) if discovery is not None else ([], [])
    installed, installed_problems = scanner.installed_engines()

    scanned_roots = roots if roots is not None else default_roots()
    model_payloads = None
    extra_builds = []
    if discovery is None:
        from .inventory import scan_models
        model_payloads, extra_builds, model_problems = scan_models(scanned_roots)

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
        "scan_roots": [str(Path(p).absolute()) for p in scanned_roots],
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
        ] + extra_builds,
        "models": model_payloads if model_payloads is not None else [
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


def _connect(socket_path: str, timeout: float) -> socket.socket:
    """连上本机代理通道。错误话术与 ``send_report`` 一直以来的那几句相同。"""

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(socket_path)
    except FileNotFoundError:
        connection.close()
        raise CliError(
            EXIT_TRANSPORT,
            f"no agent socket at {socket_path}",
            hint=(
                "平台侧还没开这条通道：需要运维执行装机脚本建立 /run/llmlab "
                "并重启服务；然后用 --socket 指向实际路径"
            ),
        ) from None
    except (ConnectionRefusedError, PermissionError, OSError) as exc:
        connection.close()
        raise CliError(
            EXIT_TRANSPORT,
            f"cannot connect to the agent socket at {socket_path}: {exc.strerror or exc}",
        ) from None
    return connection


def _exchange(
    messages: list[dict], *, socket_path: str, timeout: float
) -> dict:
    """hello 之后依次发消息并读回执，返回**最后一条**回执里的 ``data``。

    每一行都是一个 JSON 对象；错误按 :func:`_server_error` 的同一套语义转成
    ``CliError``。这条通道没有凭据 —— 身份来自内核。
    """

    connection = _connect(socket_path, timeout)
    reader = connection.makefile("rb")
    try:
        connection.sendall(_encode({"type": "hello", "protocol_version": 1}))
        hello = _read_reply(reader)
        if not hello.get("ok"):
            raise _server_error(hello)
        reply: dict = {}
        for message in messages:
            connection.sendall(_encode(message))
            reply = _read_reply(reader)
    except socket.timeout:
        raise CliError(EXIT_TRANSPORT, "the agent channel did not answer in time") from None
    finally:
        reader.close()
        connection.close()

    if not reply.get("ok"):
        raise _server_error(reply)
    data = reply.get("data")
    if not isinstance(data, dict):
        raise CliError(EXIT_TRANSPORT, "the agent channel replied ok but without a receipt")
    return data


def send_report(
    payload: dict, *, socket_path: str = DEFAULT_SOCKET, timeout: float = DEFAULT_TIMEOUT
) -> dict:
    """把一份 payload 发出去，返回服务端回执里的 ``data``。

    协议：一行 hello，一行 report。每行一个 JSON 对象，回复同理。
    """

    return _exchange([{"type": "report", **payload}], socket_path=socket_path, timeout=timeout)


def claim_tasks(
    *,
    socket_path: str = DEFAULT_SOCKET,
    limit: int = DEFAULT_TASK_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """领一轮任务。返回的条目就是服务端的 ``AgentTask.to_dict()``。

    服务端按内核给出的 uid 领取，所以这里**没有**任何 uid/owner 字段可填。
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise CliError(EXIT_USAGE, "task limit must be a positive integer")
    data = _exchange(
        [{"type": "tasks", "limit": int(limit)}],
        socket_path=socket_path,
        timeout=timeout,
    )
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise CliError(EXIT_TRANSPORT, "the agent channel replied without a task list")
    return [task for task in tasks if isinstance(task, dict)]


def send_task_result(
    task_id: str,
    state: str,
    result: dict | None,
    *,
    socket_path: str = DEFAULT_SOCKET,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """回报一条自己领过的任务。身份仍是 socket 的对端 uid。"""

    if not isinstance(task_id, str) or not task_id:
        raise CliError(EXIT_USAGE, "task_id must be a non-empty string")
    if state not in ("done", "failed"):
        raise CliError(EXIT_USAGE, "task state must be done or failed")
    message: dict = {"type": "task_result", "task_id": task_id, "state": state}
    if result is not None:
        message["result"] = result
    return _exchange([message], socket_path=socket_path, timeout=timeout)


# -- 任务执行 ---------------------------------------------------------------


def valid_unit_name(value: object) -> bool:
    """systemd 单元名白名单：只允许 ``[A-Za-z0-9_.@-]``。

    路径分隔符与空格在这里被拒绝，所以一个 unit 永远不可能变成第二个参数、
    一条路径，或一段 shell。
    """

    return (
        isinstance(value, str)
        and bool(_UNIT_RE.fullmatch(value))
        # ``systemctl --user start -x`` 会把 ``-x`` 当 option；白名单允许 ``-`` 开头，
        # 所以在这里再挡一层。
        and not value.startswith("-")
    )


def _task_payload(task: object) -> dict:
    """任务可能是 ``AgentTask``，也可能是它 ``to_dict()`` 之后的 JSON。"""

    payload = getattr(task, "payload", None)
    if payload is None and isinstance(task, dict):
        payload = task.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _task_kind(task: object, payload: dict) -> object:
    kind = getattr(task, "kind", None)
    if kind is None and isinstance(task, dict):
        kind = task.get("kind")
    return kind if kind is not None else payload.get("kind")


def _validated_argv(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("argv 必须是字符串列表")
    if not value:
        raise ValueError("argv 不能为空")
    argv: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("argv 的每一项都必须是非空字符串")
        if "\x00" in item:
            raise ValueError("argv 不能包含 NUL 字节")
        argv.append(item)
    return argv


def _validate_default_command(argv: list[str]) -> None:
    """生产执行器只认两种形状；其它一律拒绝，**不**执行。

    * ``systemctl --user start|stop|restart <unit>``
    * ``systemd-run --user --unit <name> ...``

    这里检查的是形状，不是内容：``shell=False`` 保证参数不会被再解释一次。
    """

    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("命令必须是 argv 字符串列表")
    if argv[0] == "systemctl":
        if (
            len(argv) != 4
            or argv[1] != "--user"
            or argv[2] not in TASK_KINDS
            or not valid_unit_name(argv[3])
        ):
            raise ValueError(
                "systemctl 只允许 `systemctl --user start|stop|restart <unit>`"
            )
        return
    if argv[0] == "systemd-run":
        if (
            len(argv) < 5
            or argv[1] != "--user"
            or argv[2] != "--unit"
            or not valid_unit_name(argv[3])
        ):
            raise ValueError("systemd-run 只允许 `systemd-run --user --unit <unit> ...`")
        return
    raise ValueError("只允许 systemctl --user 或 systemd-run --user 两种形状")


def _truncate(text: object) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= MAX_TASK_OUTPUT:
        return text
    return text[:MAX_TASK_OUTPUT] + "\n…[truncated]"


def default_runner(argv: list[str]) -> dict:
    """真实的本地执行器：``subprocess.run(argv, shell=False)``。

    只接受 :func:`_validate_default_command` 认可的两组形状。测试里应注入
    假 runner，而不是让这段代码真的去调用 ``systemctl``。
    """

    command = list(argv)
    _validate_default_command(command)
    completed = subprocess.run(
        command,
        shell=False,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TASK_TIMEOUT,
    )
    return {
        "argv": command,
        "returncode": completed.returncode,
        "stdout": _truncate(completed.stdout),
        "stderr": _truncate(completed.stderr),
    }


def _outcome(command: list[str], raw: object) -> dict:
    """把 runner 的返回值归一成 ``{"state", "result"}``。"""

    if isinstance(raw, dict):
        result = dict(raw)
        returncode = result.get("returncode", 0)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        returncode = raw
        result = {}
    else:
        returncode = getattr(raw, "returncode", None)
        result = {
            "stdout": _truncate(getattr(raw, "stdout", None)),
            "stderr": _truncate(getattr(raw, "stderr", None)),
        }
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise TypeError("runner 必须返回 returncode（或带 returncode 的对象）")
    result.setdefault("argv", list(command))
    result["returncode"] = returncode
    return {"state": "done" if returncode == 0 else "failed", "result": result}


def _failed(message: str) -> dict:
    return {"state": "failed", "result": {"error": message}}


def run_task(task: object, *, runner=None) -> dict:
    """执行一条任务，返回可直接回报的 ``{"state", "result"}``。

    诚实优先：payload 里既没有 ``unit`` 也没有 ``argv`` 时不猜、不假装成功，
    直接回报 ``failed: 这一行没有可执行入口``。单元名不合法同样拒绝执行。

    ``runner`` 可注入：签名是 ``runner(argv: list[str]) -> mapping | int``。
    生产缺省是 :func:`default_runner`，只接受 systemctl/systemd-run 两种形状。
    """

    payload = _task_payload(task)
    kind = _task_kind(task, payload)
    unit = payload.get("unit")
    argv = payload.get("argv")

    if argv is None and unit is None:
        return _failed("这一行没有可执行入口")
    if unit is not None and not valid_unit_name(unit):
        return _failed(f"unit 名不合法: {unit!r}")

    if argv is not None:
        try:
            command = _validated_argv(argv)
        except ValueError as exc:
            return _failed(str(exc))
    else:
        if kind not in TASK_KINDS:
            return _failed(f"未知任务类型: {kind!r}")
        command = ["systemctl", "--user", kind, unit]

    execute = runner if runner is not None else default_runner
    try:
        raw = execute(command)
    except subprocess.TimeoutExpired:
        return _failed(f"命令超时（超过 {DEFAULT_TASK_TIMEOUT:g} 秒）")
    except ValueError as exc:
        # 生产执行器的形状校验失败：拒绝执行，并把原因如实回报。
        return _failed(str(exc))
    except OSError as exc:
        return _failed(f"无法执行: {exc}")
    except Exception as exc:  # noqa: BLE001 - 执行器的意外失败也要回报而不是静默
        return _failed(f"执行失败: {type(exc).__name__}")

    try:
        return _outcome(command, raw)
    except TypeError as exc:
        return _failed(str(exc))


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
    "DEFAULT_TASK_LIMIT",
    "DEFAULT_TASK_TIMEOUT",
    "MAX_MESSAGE_BYTES",
    "claim_tasks",
    "collect_report",
    "default_agent_id",
    "default_roots",
    "default_runner",
    "human_summary",
    "report_once",
    "run_task",
    "send_report",
    "send_task_result",
    "valid_unit_name",
]
