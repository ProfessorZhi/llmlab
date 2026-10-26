"""每用户代理的 unix socket 通道：**内核**说是谁，平台才认是谁。

为什么不是 HTTP + token：这条通道要回答的正是「服务端读不到用户的目录，怎么
知道他的机器上有什么」。令牌能证明「某个主体签过一条凭据」，但证明不了「这条
连接来自他以那个 uid 运行的进程」。unix socket 的 ``SO_PEERCRED`` 由内核在
``connect()`` 时填好，客户端既不能伪造、也不用保管——用户不需要任何密码或
长期令牌就能让自己的机器被看见（设计规格 D9）。

边界，逐条写死：

* **未映射的 uid 立即断开。** 主体目录里没有这个 uid（系统账户、root、一个
  没有平台身份的人）就到此为止，连一句 hello 都不接。
* **身份不从消息里读。** 协议里没有 ``uid`` / ``owner`` / ``principal`` 字段；
  带了的条目会被 ``agent_reports`` 当成伪造整条丢弃。
* **一条连接上只做少量事。** 消息上限 1 MiB、每连接最多 8 条、空闲 30 秒断开、
  同时最多 8 条连接。代理的正常行为是「连上 → hello → report → 断开」，
  这些上限只用来挡住一个坏掉的客户端。
* **socket 文件权限 0666。** 本机上任何用户都能 ``connect``，但只有受信目录里
  的 uid 能通过第一步。这比依赖目录权限更清楚：入口是一个明确的判定，而不是
  一个可能被继承错的模式位。

本模块只负责传输与身份映射；校验与入库在 ``agent_reports`` 与
``RegistryService.report_agent``。socket 绑不上时它**不**让服务起不来：把原因
记进状态对象，``/discovery`` 如实展示「代理通道没开、为什么」。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .errors import RegistryError
from .identity import SCOPE_AGENT_REPORT, RequesterContext
from .models import utc_now

#: 协议版本。与报告本身的 ``report_version`` 分开：帧格式与内容各算各的。
PROTOCOL_VERSION = 1

#: 一条消息（一行 JSON）的上限。典型报告远小于它；超过就是客户端坏了。
MAX_MESSAGE_BYTES = 1024 * 1024

#: 一条连接上最多处理多少条消息。持久连接留给将来的任务轮询，现在不需要。
MAX_MESSAGES_PER_CONNECTION = 8

#: 空闲超时：一个连上就不说话的客户端不该占着一个线程。
IDLE_TIMEOUT_SECONDS = 30.0

#: 同时处理的连接数上限。
MAX_CONNECTIONS = 8

_ACCEPT_BACKLOG = 16

#: 监听 socket 的轮询间隔。``accept()`` 在一个线程里阻塞着，而**关闭另一个线程
#: 正阻塞其上的 socket** 并不保证叫醒它；给它一个短超时就能让循环自己回头看
#: ``_stop``，于是 ``stop()`` 立刻返回，而不是每次都等满 2 秒的 join 超时。
_ACCEPT_POLL_SECONDS = 0.25

#: socket 文件权限：任何本机用户都可以 ``connect``，但只有受信 uid 能过身份这一步。
_SOCKET_MODE = 0o666

#: 关连接前最多花多久把对端还在路上的字节读掉。它只影响「被拒的客户端」，
#: 正常客户端在收到 FIN 后立刻关闭，这里几乎不会真的等。
_LINGER_SECONDS = 0.5


@dataclass
class AgentChannelStatus:
    """通道的对外事实。``RegistryService.discover`` 把它原样放进信封。"""

    path: str
    listening: bool = False
    problem: str | None = None
    connections: int = 0
    reports: int = 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "listening": self.listening,
            "problem": self.problem,
            "connections": self.connections,
            "reports": self.reports,
        }


def _peer_uid(connection: socket.socket) -> int | None:
    """``SO_PEERCRED`` 给出的对端 uid。取不到就 ``None``（调用方 fail closed）。"""

    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:  # pragma: no cover - 非 Linux 平台
        return None
    raw = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return int(uid)


def _encode(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


@dataclass
class AgentChannel:
    """一个 AF_UNIX 监听器 + 身份映射。``service`` 只用到 ``report_agent``。"""

    path: str
    service: object
    principals: object
    status: AgentChannelStatus = field(init=False)
    peer_uid: Callable[[socket.socket], int | None] = _peer_uid
    clock: Callable[[], datetime] = utc_now
    max_connections: int = MAX_CONNECTIONS

    def __post_init__(self) -> None:
        self.status = AgentChannelStatus(path=self.path)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active = 0

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> AgentChannelStatus:
        """绑定并开始接受连接。**任何失败都只进状态，不抛。**"""

        directory = os.path.dirname(self.path) or "."
        if not os.path.isdir(directory):
            self.status.problem = f"agent_socket_dir_missing:{directory}"
            return self.status

        try:
            self._unlink_stale()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(self.path)
            os.chmod(self.path, _SOCKET_MODE)
            listener.listen(_ACCEPT_BACKLOG)
            listener.settimeout(_ACCEPT_POLL_SECONDS)
        except OSError as exc:
            self.status.problem = f"agent_socket_bind_failed:{exc.errno or 0}"
            try:
                listener.close()  # type: ignore[possibly-undefined]
            except (OSError, UnboundLocalError):
                pass
            return self.status

        self._listener = listener
        self.status.listening = True
        self.status.problem = None
        self._thread = threading.Thread(
            target=self._accept_loop, name="llmlab-agent-channel", daemon=True
        )
        self._thread.start()
        return self.status

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:  # pragma: no cover - 关闭失败无关紧要
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self.status.listening = False
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:  # pragma: no cover
            pass

    def _unlink_stale(self) -> None:
        """上次进程留下的 socket 文件挡路时清掉它；**有人真的在听**就不动。"""

        if not os.path.exists(self.path):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(self.path)
        except OSError:
            os.unlink(self.path)
        else:  # pragma: no cover - 需要同一路径上真的有两个实例
            raise OSError("agent socket is already served by another process")
        finally:
            probe.close()

    # -- 接受连接 ---------------------------------------------------------

    def _accept_loop(self) -> None:  # pragma: no cover - 线程体，逻辑在 _serve 里
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                if self._active >= self.max_connections or self._stop.is_set():
                    try:
                        connection.close()
                    except OSError:
                        pass
                    continue
                self._active += 1
            threading.Thread(
                target=self._serve, args=(connection,), name="llmlab-agent-conn", daemon=True
            ).start()

    def _serve(self, connection: socket.socket) -> None:
        """一条连接：先认人，再收消息。"""

        reader = None
        try:
            connection.settimeout(IDLE_TIMEOUT_SECONDS)
            try:
                uid = self.peer_uid(connection)
            except OSError:
                # 内核都不肯说这条连接是谁 —— 没有身份，也就没有话说。直接关。
                return
            principal = self.principals.by_uid(uid) if uid is not None else None
            if principal is None or not principal.approved:
                _send(connection, _error(
                    "agent.uid_not_mapped",
                    "this uid has no approved platform identity on this host",
                ))
                return
            with self._lock:
                self.status.connections += 1

            requester = RequesterContext(
                principal=principal.principal,
                uid=uid,
                linux_user=principal.linux_user,
                # 这一条 scope 只存在于此：网页会话的签发策略不给它，HTTP 里也没有
                # 任何一条路由接受它。它是「内核证明的 uid」的产物。
                scopes=frozenset({SCOPE_AGENT_REPORT}),
                roles=frozenset(),
                token_label="unix-socket",
            )

            reader = connection.makefile("rb")
            greeted = False
            for _ in range(MAX_MESSAGES_PER_CONNECTION):
                line = reader.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_MESSAGE_BYTES:
                    _send(connection, _error(
                        "agent.message_too_large",
                        f"a message must stay under {MAX_MESSAGE_BYTES} bytes",
                    ))
                    return
                message = _decode(line, connection)
                if message is None:
                    return
                kind = message.get("type")
                if kind == "hello":
                    greeted = True
                    _send(connection, {
                        "ok": True,
                        "data": {
                            "protocol_version": PROTOCOL_VERSION,
                            "principal": requester.principal,
                            "uid": requester.uid,
                            "linux_user": requester.linux_user,
                        },
                    })
                    continue
                if kind != "report":
                    _send(connection, _error(
                        "agent.protocol_error", f"unknown message type: {kind!r}"
                    ))
                    return
                if not greeted:
                    _send(connection, _error(
                        "agent.hello_required", "send hello first"
                    ))
                    return
                self._handle_report(connection, requester, message)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
            if reader is not None:
                try:
                    reader.close()
                except OSError:  # pragma: no cover
                    pass
            _close(connection)

    def _handle_report(
        self, connection: socket.socket, requester: RequesterContext, message: dict
    ) -> None:
        payload = {key: value for key, value in message.items() if key != "type"}
        try:
            report = self.service.report_agent(requester, payload)
        except RegistryError as exc:
            message_text = exc.args[0] if exc.args else exc.contract_code
            # 存储层的话术里有路径与表名，不发回客户端（与 HTTP 那条路一致）。
            if not getattr(exc, "expose_message", True):
                message_text = "the report could not be stored"
            _send(connection, _error(exc.contract_code, str(message_text)))
            return
        with self._lock:
            self.status.reports += 1
        _send(connection, {"ok": True, "data": report.response()})


def _send(connection: socket.socket, message: dict) -> None:
    try:
        connection.sendall(_encode(message))
    except OSError:  # pragma: no cover - 对端提前断开
        pass


def _close(connection: socket.socket) -> None:
    """先发 FIN，再把对端还在路上的字节读掉，最后才 ``close()``。

    直接关一个还有未读入站数据的 socket，内核会用 **RST 代替 FIN**。对端于是读到
    ``ECONNRESET``，而那条「你的 uid 没有映射」的说明可能连同缓冲区一起被丢掉 ——
    被拒的人看到的是「连接被重置」，而不是为什么被拒。这条通道唯一的拒绝理由就是
    身份，所以这句话不能丢。

    顺序固定的原因：``shutdown(SHUT_WR)`` 让对端立刻读到 EOF（不必等我们读完），
    之后的 ``recv`` 只负责把已经写进来的字节收干净，收不到就在 ``_LINGER_SECONDS``
    后放弃 —— 一个不肯松手的客户端最多占住一个线程半秒。
    """

    try:
        connection.shutdown(socket.SHUT_WR)
    except OSError:  # pragma: no cover - 对端已经先走了
        pass
    deadline = time.monotonic() + _LINGER_SECONDS
    try:
        connection.settimeout(_LINGER_SECONDS)
        # 用 deadline 而不是「读到 EOF」：一个不停写的客户端不该把线程永远留在收尾里。
        while time.monotonic() < deadline and connection.recv(65536):
            pass
    except OSError:
        pass
    try:
        connection.close()
    except OSError:  # pragma: no cover
        pass


def _decode(line: bytes, connection: socket.socket) -> dict | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _send(connection, _error("agent.protocol_error", "each line must be one JSON object"))
        return None
    if not isinstance(value, dict):
        _send(connection, _error("agent.protocol_error", "a message must be a JSON object"))
        return None
    return value


__all__ = [
    "AgentChannel",
    "AgentChannelStatus",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
]
