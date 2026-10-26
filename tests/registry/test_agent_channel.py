"""代理通道：真的 unix socket + 内核给的身份。

这里跑的是**真的** AF_UNIX 监听器（临时目录里的一个 socket 文件），只有两件事
被替换：``peer_uid``（否则测试进程只能以一个 uid 出现）和 ``report_agent``
（那是服务层，另有测试）。协议、超时、上限、错误形状都是真的。

最后两条用例反过来：它们不替换 ``peer_uid``，让内核的 ``SO_PEERCRED`` 真的回答
一次 —— 这是整条通道唯一的凭据来源，值得有一条用例证明它在当前内核上可用。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.registry.agent_channel import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    AgentChannel,
    _peer_uid,
)
from backend.registry.errors import AccessDenied, StorageError
from backend.registry.identity import (
    SCOPE_AGENT_REPORT,
    PRINCIPAL_SUSPENDED,
    Principal,
    PrincipalDirectory,
)

ALICE_UID = 4001
BOB_UID = 4302


class _Service:
    """``report_agent`` 的替身：记下调用，按需报错。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, dict]] = []
        self.error: Exception | None = None
        self.receipt: dict = {"agent_id": "lab-01", "accepted": {"engines": 0}}

    def report_agent(self, requester, payload):
        self.calls.append((requester, payload))
        if self.error is not None:
            raise self.error
        receipt = self.receipt
        return SimpleNamespace(response=lambda: dict(receipt))


class _Client:
    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.reader = self.sock.makefile("rb")

    def send(self, message: dict) -> None:
        try:
            self.sock.sendall(json.dumps(message).encode("utf-8") + b"\n")
        except BrokenPipeError:
            # 服务端已经关掉这条连接 —— 对被拒的客户端来说，这就是它该看到的。
            pass

    def send_raw(self, payload: bytes) -> None:
        self.sock.sendall(payload)

    def read(self) -> dict | None:
        line = self.reader.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return None
        return json.loads(line.decode("utf-8"))

    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.sock.close()


class ChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "agent.sock")
        self.service = _Service()
        self.principals = PrincipalDirectory(
            [
                Principal("linux:lab-01:4001", "alice", ALICE_UID),
                Principal(
                    "linux:lab-01:4302", "bob", BOB_UID, state=PRINCIPAL_SUSPENDED
                ),
            ]
        )
        self.channel = AgentChannel(
            path=self.path,
            service=self.service,
            principals=self.principals,
            peer_uid=lambda connection: ALICE_UID,
        )
        self.addCleanup(self.channel.stop)
        self.channel.start()

    def client(self) -> _Client:
        client = _Client(self.path)
        self.addCleanup(client.close)
        return client

    # -- 正常路径 ----------------------------------------------------------

    def test_hello_names_the_kernel_attested_identity(self) -> None:
        connection = self.client()
        connection.send({"type": "hello", "protocol_version": PROTOCOL_VERSION})
        reply = connection.read()

        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["uid"], ALICE_UID)
        self.assertEqual(reply["data"]["linux_user"], "alice")
        self.assertEqual(reply["data"]["principal"], "linux:lab-01:4001")
        self.assertEqual(reply["data"]["protocol_version"], PROTOCOL_VERSION)

    def test_a_report_after_hello_reaches_the_service_with_socket_scopes(self) -> None:
        connection = self.client()
        connection.send({"type": "hello"})
        connection.read()
        connection.send({"type": "report", "agent_id": "lab-01", "engines": [{"port": 8000}]})
        reply = connection.read()

        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["agent_id"], "lab-01")
        requester, payload = self.service.calls[0]
        self.assertEqual(requester.principal, "linux:lab-01:4001")
        self.assertEqual(requester.uid, ALICE_UID)
        self.assertEqual(requester.scopes, frozenset({SCOPE_AGENT_REPORT}))
        self.assertEqual(requester.token_label, "unix-socket")
        # ``type`` 是协议字段，不该出现在报告载荷里。
        self.assertNotIn("type", payload)
        self.assertEqual(payload["engines"], [{"port": 8000}])

        self.assertEqual(self.channel.status.reports, 1)
        self.assertEqual(self.channel.status.connections, 1)

    # -- 身份 --------------------------------------------------------------

    def test_an_unmapped_uid_is_disconnected_before_any_hello(self) -> None:
        self.channel.peer_uid = lambda connection: 4999
        connection = self.client()

        self.assertEqual(connection.read()["error"]["code"], "agent.uid_not_mapped")
        connection.send({"type": "hello"})
        self.assertIsNone(connection.read())
        self.assertEqual(self.service.calls, [])

    def test_a_peer_credential_that_cannot_be_read_closes_silently(self) -> None:
        """``getsockopt`` 都失败时，连「你是谁」都无从说起 —— 那就什么都不说。"""

        def refuse(connection):
            raise OSError("cannot read peer credentials")

        self.channel.peer_uid = refuse
        connection = self.client()

        self.assertIsNone(connection.read())
        self.assertEqual(self.service.calls, [])

    def test_a_suspended_principal_is_refused_like_an_unknown_uid(self) -> None:
        self.channel.peer_uid = lambda connection: BOB_UID
        connection = self.client()

        self.assertEqual(connection.read()["error"]["code"], "agent.uid_not_mapped")
        self.assertEqual(self.service.calls, [])

    def test_a_report_without_hello_is_refused(self) -> None:
        connection = self.client()
        connection.send({"type": "report", "agent_id": "lab-01"})

        self.assertEqual(connection.read()["error"]["code"], "agent.hello_required")
        self.assertEqual(self.service.calls, [])

    def test_a_rejection_reaches_a_client_that_is_still_writing(self) -> None:
        """被拒时客户端可能已经又写了一条。它要读到**原因**，再读到干净的 EOF。

        这条用例来自一次真实的 flake：服务端发完错误就 ``close()``，此时入站缓冲区
        里还有客户端刚写的 ``hello``，内核便用 RST 代替 FIN —— 客户端读到的不是
        「为什么被拒」，而是 ``ECONNRESET``。原因可见与不可见，是两条不同的体验。
        """

        self.channel.peer_uid = lambda connection: 4999
        connection = self.client()
        connection.send({"type": "hello"})
        connection.send({"type": "report", "agent_id": "lab-01"})

        self.assertEqual(connection.read()["error"]["code"], "agent.uid_not_mapped")
        self.assertIsNone(connection.read())
        self.assertEqual(self.service.calls, [])

    # -- 协议 --------------------------------------------------------------

    def test_an_unknown_message_type_is_a_protocol_error(self) -> None:
        connection = self.client()
        connection.send({"type": "start-engine"})

        self.assertEqual(connection.read()["error"]["code"], "agent.protocol_error")

    def test_a_line_that_is_not_json_is_a_protocol_error(self) -> None:
        connection = self.client()
        connection.send_raw(b"not json\n")

        self.assertEqual(connection.read()["error"]["code"], "agent.protocol_error")

    def test_an_oversized_line_is_refused_without_buffering_it_all(self) -> None:
        connection = self.client()
        connection.send_raw(b"x" * (MAX_MESSAGE_BYTES + 16) + b"\n")

        self.assertEqual(connection.read()["error"]["code"], "agent.message_too_large")

    def test_a_connection_is_capped_at_a_handful_of_messages(self) -> None:
        connection = self.client()
        for _ in range(8):
            connection.send({"type": "hello"})
            self.assertTrue(connection.read()["ok"])

        # 第九条不会再被处理：通道读完这一批就关。
        connection.send({"type": "hello"})
        self.assertIsNone(connection.read())

    # -- 错误透传 ----------------------------------------------------------

    def test_a_registry_error_is_returned_with_its_contract_code(self) -> None:
        self.service.error = AccessDenied("missing required scope")
        connection = self.client()
        connection.send({"type": "hello"})
        connection.read()
        connection.send({"type": "report", "agent_id": "lab-01"})
        reply = connection.read()

        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "registry.access_denied")

    def test_a_storage_error_does_not_leak_its_message(self) -> None:
        self.service.error = StorageError("cannot write /home/alice/.llmlab-state/registry.db")
        connection = self.client()
        connection.send({"type": "hello"})
        connection.read()
        connection.send({"type": "report", "agent_id": "lab-01"})
        reply = connection.read()

        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "registry.storage_error")
        self.assertNotIn("/home/alice", reply["error"]["message"])

    # -- 生命周期 ----------------------------------------------------------

    def test_a_missing_socket_directory_is_reported_not_raised(self) -> None:
        channel = AgentChannel(
            path=str(Path(self.tmp.name) / "absent" / "agent.sock"),
            service=self.service,
            principals=self.principals,
        )
        status = channel.start()

        self.assertFalse(status.listening)
        self.assertTrue(status.problem.startswith("agent_socket_dir_missing:"))
        channel.stop()
        self.assertFalse(channel.status.listening)

    def test_stop_unlinks_the_socket_and_leaves_a_stale_one_replaceable(self) -> None:
        self.assertTrue(Path(self.path).exists())
        self.channel.stop()
        self.assertFalse(Path(self.path).exists())

        # 上次进程留下的文件挡路：新的实例要能把它清掉。
        Path(self.path).touch()
        channel = AgentChannel(
            path=self.path,
            service=self.service,
            principals=self.principals,
            peer_uid=lambda connection: ALICE_UID,
        )
        self.addCleanup(channel.stop)
        status = channel.start()
        self.assertTrue(status.listening, status.problem)

    def test_status_is_serializable_for_the_discovery_envelope(self) -> None:
        status = self.channel.status.to_dict()
        self.assertEqual(status["path"], self.path)
        self.assertTrue(status["listening"])
        self.assertIsNone(status["problem"])
        self.assertEqual(set(status), {"path", "listening", "problem", "connections", "reports"})


class PeerCredentialTests(unittest.TestCase):
    """真的让内核回答一次：这条 socket 的 uid 从哪来。"""

    def setUp(self) -> None:
        if not hasattr(socket, "SO_PEERCRED"):  # pragma: no cover - 非 Linux
            self.skipTest("SO_PEERCRED is Linux-only")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "cred.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(1)
        self.addCleanup(self.listener.close)

    def test_the_kernel_reports_the_connecting_uids_own_uid(self) -> None:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        try:
            client.connect(self.path)
            connection, _address = self.listener.accept()
        finally:
            client.close()
        self.addCleanup(connection.close)

        self.assertEqual(_peer_uid(connection), os.getuid())

    def test_a_channel_started_on_a_tmp_dir_accepts_this_process(self) -> None:
        """端到端：不替换 ``peer_uid``，用本进程的真实 uid 走完 hello。"""

        directory = Path(self.tmp.name) / "run"
        directory.mkdir()
        path = str(directory / "agent.sock")
        uid = os.getuid()
        principals = PrincipalDirectory(
            [Principal(f"linux:lab-01:{uid}", "me", uid)]
        )
        service = _Service()
        channel = AgentChannel(path=path, service=service, principals=principals)
        self.addCleanup(channel.stop)
        self.assertTrue(channel.start().listening)

        connection = _Client(path)
        self.addCleanup(connection.close)
        connection.send({"type": "hello"})
        reply = connection.read()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["uid"], uid)


if __name__ == "__main__":
    unittest.main()
