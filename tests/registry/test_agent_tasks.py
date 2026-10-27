"""代理任务链路：排队 -> 领取 -> 执行 -> 回报。

这里有两层：

* **服务层**：主人校验、原子领取、状态迁移（``queued`` -> ``running`` -> ``done``）；
* **代理层**：领任务的协议帧，以及 ``run_task`` 在不安全/不可执行时诚实拒绝。

执行器在测试里一律是假的 —— 这些用例**不会**调用 ``systemctl``，也不会创建或
修改任何 systemd 单元。
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from backend.registry.agent_channel import MAX_MESSAGE_BYTES, AgentChannel
from backend.registry.errors import AccessDenied, InvalidRequest, NotFound
from backend.registry.identity import (
    SCOPE_AGENT_REPORT,
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TokenBinding,
)
from backend.registry.models import (
    AGENT_TASK_DONE,
    AGENT_TASK_FAILED,
    AGENT_TASK_QUEUED,
    AGENT_TASK_RUNNING,
)
from cli.llmlab import agent as agent_mod
from tests.registry.support import NON_ADMIN_SCOPES, TOKEN_PREFIX, Harness, close_all

ALICE_UID = 4001
BOB_UID = 4003


def tearDownModule() -> None:
    close_all()


def _agent_requester(uid: int, principal: str = "alice", linux_user: str = "alice") -> RequesterContext:
    """代理通道会构造的那种身份：只有内核证明的 uid 与一条 report scope。"""

    return RequesterContext(
        principal=principal,
        uid=uid,
        linux_user=linux_user,
        scopes=frozenset({SCOPE_AGENT_REPORT}),
        roles=frozenset(),
        token_label="unix-socket",
    )


class _Client:
    """最小 unix socket 客户端：每行一个 JSON 帧。"""

    def __init__(self, path: str, timeout: float = 5.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.reader = self.sock.makefile("rb")

    def send(self, message: dict) -> None:
        self.sock.sendall(json.dumps(message).encode("utf-8") + b"\n")

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


class AgentTaskServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.service = self.harness.service
        self.owner = self.harness.requester("alice")
        self.engine_id = self.harness.make_engine("alice", "service")
        self.agent = _agent_requester(ALICE_UID)

    def test_enqueue_claim_finish_round_trip(self) -> None:
        task = self.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )

        # 「已下发」只是 queued，不是 running，更不是 done。
        self.assertEqual(task.state, AGENT_TASK_QUEUED)
        self.assertEqual(task.uid, ALICE_UID)
        self.assertEqual(task.requested_by, "alice")
        self.assertEqual(
            set(task.payload), {"kind", "engine_id", "unit"}
        )
        self.assertEqual(task.payload["kind"], "start")
        self.assertEqual(task.payload["engine_id"], self.engine_id)

        claimed = self.service.claim_agent_tasks(self.agent, limit=4)
        self.assertEqual([item.id for item in claimed], [task.id])
        self.assertEqual(claimed[0].state, AGENT_TASK_RUNNING)

        # 领取之后、收尾之前：显示的是 running，不能提前显示 done。
        self.assertEqual(
            self.service.agent_task(self.owner, task.id).state, AGENT_TASK_RUNNING
        )

        finished = self.service.finish_agent_task(
            self.agent, task.id, state=AGENT_TASK_DONE, result={"returncode": 0}
        )
        self.assertEqual(finished.state, AGENT_TASK_DONE)
        self.assertEqual(finished.result, {"returncode": 0})
        self.assertEqual(
            self.service.agent_task(self.owner, task.id).state, AGENT_TASK_DONE
        )

    def test_a_failed_result_is_a_result_not_a_claim(self) -> None:
        task = self.service.enqueue_agent_task(
            self.owner, self.engine_id, "stop", unit="llmlab-service.service"
        )
        self.service.claim_agent_tasks(self.agent)

        finished = self.service.finish_agent_task(
            self.agent, task.id, state=AGENT_TASK_FAILED, result={"error": "boom"}
        )
        self.assertEqual(finished.state, AGENT_TASK_FAILED)
        self.assertEqual(finished.result, {"error": "boom"})

    def test_two_claimers_never_get_the_same_task(self) -> None:
        task = self.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )
        barrier = threading.Barrier(2)
        claimed: list[list] = []
        errors: list[BaseException] = []

        def poll() -> None:
            try:
                barrier.wait(timeout=5)
                claimed.append(self.service.claim_agent_tasks(self.agent, limit=1))
            except BaseException as exc:  # pragma: no cover - 失败时给出原因
                errors.append(exc)

        threads = [threading.Thread(target=poll) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(sum(len(items) for items in claimed), 1, claimed)
        ids = [item.id for items in claimed for item in items]
        self.assertEqual(ids, [task.id])
        # 数据库里也只有一条 running，没有第二次迁移。
        self.assertEqual(
            self.service.agent_task(self.owner, task.id).state, AGENT_TASK_RUNNING
        )

    def test_a_non_owner_cannot_enqueue_for_that_engine(self) -> None:
        bob = self.harness.requester("bob")

        with self.assertRaises(AccessDenied):
            self.service.enqueue_agent_task(
                bob, self.engine_id, "start", unit="llmlab-service.service"
            )

        self.assertEqual(self.service.store.tasks_for(ALICE_UID), [])
        self.assertEqual(self.service.store.tasks_for(BOB_UID), [])

    def test_an_agent_cannot_claim_another_uids_tasks(self) -> None:
        self.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )

        other = _agent_requester(BOB_UID, principal="bob", linux_user="bob")
        self.assertEqual(self.service.claim_agent_tasks(other), [])
        self.assertEqual(
            self.service.store.tasks_for(ALICE_UID)[0].state, AGENT_TASK_QUEUED
        )

    def test_claim_and_finish_require_the_agent_report_scope(self) -> None:
        self.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )

        with self.assertRaises(AccessDenied):
            self.service.claim_agent_tasks(self.owner)  # HTTP 身份没有这条 scope

    def test_a_queued_task_cannot_be_finished_without_being_claimed(self) -> None:
        task = self.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )

        with self.assertRaises(Exception) as caught:
            self.service.finish_agent_task(
                self.agent, task.id, state=AGENT_TASK_DONE, result=None
            )
        self.assertEqual(getattr(caught.exception, "contract_code", ""), "registry.conflict")
        self.assertEqual(
            self.service.agent_task(self.owner, task.id).state, AGENT_TASK_QUEUED
        )

    def test_unit_and_argv_together_are_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.service.enqueue_agent_task(
                self.owner,
                self.engine_id,
                "start",
                unit="llmlab-service.service",
                argv=["systemctl", "--user", "start", "llmlab-service.service"],
            )

    def test_an_invalid_unit_is_rejected_before_it_reaches_the_agent(self) -> None:
        for bad in ("../../etc/passwd", "bad unit", "a/b", "-evil.service"):
            with self.subTest(unit=bad):
                with self.assertRaises(InvalidRequest):
                    self.service.enqueue_agent_task(
                        self.owner, self.engine_id, "start", unit=bad
                    )


class AgentTaskProtocolTests(unittest.TestCase):
    """真的 unix socket：hello -> tasks -> task_result。只有 peer_uid 是替身。"""

    def setUp(self) -> None:
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        self.owner = self.harness.requester("alice")
        self.engine_id = self.harness.make_engine("alice", "service")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "agent.sock")
        self.channel = AgentChannel(
            path=self.path,
            service=self.harness.service,
            principals=self.harness.principals,
            peer_uid=lambda connection: ALICE_UID,
        )
        self.addCleanup(self.channel.stop)
        self.assertTrue(self.channel.start().listening)
        self.client = _Client(self.path)
        self.addCleanup(self.client.close)

    def _hello(self) -> None:
        self.client.send({"type": "hello"})
        self.assertTrue(self.client.read()["ok"])

    def test_a_task_is_claimed_and_reported_over_the_channel(self) -> None:
        task = self.harness.service.enqueue_agent_task(
            self.owner, self.engine_id, "restart", unit="llmlab-service.service"
        )
        self._hello()

        self.client.send({"type": "tasks", "limit": 4})
        reply = self.client.read()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["count"], 1)
        delivered = reply["data"]["tasks"][0]
        self.assertEqual(delivered["id"], task.id)
        self.assertEqual(delivered["state"], AGENT_TASK_RUNNING)
        # 身份字段不在任务里：一个代理只能领到自己桶里的东西。
        self.assertEqual(delivered["uid"], ALICE_UID)

        self.client.send({
            "type": "task_result",
            "task_id": task.id,
            "state": "done",
            "result": {"returncode": 0},
        })
        reply = self.client.read()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["data"]["task"]["state"], AGENT_TASK_DONE)
        self.assertEqual(
            self.harness.service.agent_task(self.owner, task.id).state, AGENT_TASK_DONE
        )

    def test_the_agent_client_can_claim_run_and_report(self) -> None:
        """``--watch`` 每一轮的三个动作，用真实 socket 串起来。"""

        task = self.harness.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )

        claimed = agent_mod.claim_tasks(socket_path=self.path, limit=4)
        self.assertEqual([item["id"] for item in claimed], [task.id])
        outcome = agent_mod.run_task(
            claimed[0], runner=lambda argv: {"returncode": 0, "stdout": "ok"}
        )
        self.assertEqual(outcome["state"], "done")

        receipt = agent_mod.send_task_result(
            claimed[0]["id"],
            outcome["state"],
            outcome["result"],
            socket_path=self.path,
        )
        self.assertEqual(receipt["task"]["state"], AGENT_TASK_DONE)
        self.assertEqual(
            self.harness.service.agent_task(self.owner, task.id).state, AGENT_TASK_DONE
        )

    def test_a_result_without_claiming_is_refused(self) -> None:
        task = self.harness.service.enqueue_agent_task(
            self.owner, self.engine_id, "start", unit="llmlab-service.service"
        )
        self._hello()

        self.client.send({
            "type": "task_result",
            "task_id": task.id,
            "state": "done",
            "result": None,
        })
        reply = self.client.read()
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "registry.conflict")
        self.assertEqual(
            self.harness.service.agent_task(self.owner, task.id).state, AGENT_TASK_QUEUED
        )


class RunTaskTests(unittest.TestCase):
    """``run_task``：不猜、不拼 shell、没有入口就诚实失败。"""

    def test_without_unit_or_argv_it_fails_honestly_and_calls_no_runner(self) -> None:
        calls: list[list[str]] = []

        def runner(argv):  # pragma: no cover - 不该被调用
            calls.append(list(argv))
            return {"returncode": 0}

        outcome = agent_mod.run_task(
            {"id": "t1", "kind": "start", "payload": {"kind": "start", "engine_id": "e"}},
            runner=runner,
        )

        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["result"]["error"], "这一行没有可执行入口")
        self.assertEqual(calls, [])

    def test_an_invalid_unit_is_refused_without_running_anything(self) -> None:
        calls: list[list[str]] = []

        def runner(argv):  # pragma: no cover - 不该被调用
            calls.append(list(argv))
            return {"returncode": 0}

        for bad in ("../../etc/passwd", "bad unit", "a/b", "-evil.service"):
            with self.subTest(unit=bad):
                outcome = agent_mod.run_task(
                    {"kind": "start", "payload": {"kind": "start", "unit": bad}},
                    runner=runner,
                )
                self.assertEqual(outcome["state"], "failed")
                self.assertIn("unit 名不合法", outcome["result"]["error"])
        self.assertEqual(calls, [])

    def test_a_unit_becomes_an_argv_list_not_a_shell_string(self) -> None:
        calls: list[list[str]] = []

        def runner(argv):
            calls.append(list(argv))
            return {"returncode": 0, "stdout": "started"}

        outcome = agent_mod.run_task(
            {"kind": "restart", "payload": {"kind": "restart", "unit": "svc.service"}},
            runner=runner,
        )

        self.assertEqual(outcome["state"], "done")
        self.assertEqual(calls, [["systemctl", "--user", "restart", "svc.service"]])
        self.assertEqual(outcome["result"]["argv"], calls[0])
        self.assertEqual(outcome["result"]["stdout"], "started")

    def test_a_nonzero_return_code_is_failed_not_done(self) -> None:
        outcome = agent_mod.run_task(
            {"kind": "stop", "payload": {"kind": "stop", "unit": "svc.service"}},
            runner=lambda argv: {"returncode": 3, "stderr": "no such unit"},
        )

        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["result"]["returncode"], 3)

    def test_the_production_runner_only_accepts_the_two_known_shapes(self) -> None:
        # 只做形状校验，**不**启动任何进程：非法形状在 subprocess.run 之前被拒绝。
        for command in (
            ["/bin/sh", "-c", "id"],
            ["systemctl", "--user", "start", "bad unit"],
            ["systemctl", "start", "svc.service"],
            ["systemd-run", "--user", "--unit", "svc.service"],  # 没有要跑的命令
        ):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    agent_mod.default_runner(command)


ACCOUNT = "account:319ff153f9792ed5d8bd353c3260b4e5"
OTHER_ACCOUNT = "account:7a1c0d33b8f64e0a9d2c55e1f0a7b482"


class AccountOwnedEngineTests(unittest.TestCase):
    """个人模式下服务归**账户**所有，而账户没有 uid。

    任务要交给「跑这个服务的那个 Linux 用户」的代理，所以 uid 只能来自 engine
    行里**登记时观测到的**事实：主体上取不到（账户没有 uid），请求体里的不可信。
    """

    def setUp(self) -> None:
        principals = PrincipalDirectory(
            [
                Principal("alice", "alice", ALICE_UID),
                Principal(principal=ACCOUNT, linux_user="alice", uid=None, kind="account"),
                # 与 ACCOUNT 无关的第二个账户：它也能看到 alice 那只桶，但没见过这条任务。
                Principal(principal=OTHER_ACCOUNT, linux_user="bob", uid=None, kind="account"),
            ]
        )
        bindings = [
            TokenBinding(
                f"{TOKEN_PREFIX}{ACCOUNT}",
                ACCOUNT,
                frozenset(NON_ADMIN_SCOPES),
                roles=frozenset(),
                label="test",
            )
        ]
        self.harness = Harness(principals=principals, bindings=bindings)
        self.addCleanup(self.harness.close)
        self.service = self.harness.service
        self.requester = self.harness.requester(ACCOUNT)

    def test_the_task_goes_to_the_uid_the_service_was_observed_running_as(self):
        engine_id = self.harness.make_engine(ACCOUNT, "svc")
        engine = self.service.store.engine_by_id(engine_id)
        self.assertEqual(engine.owner_principal, ACCOUNT)
        self.assertEqual(engine.owner_uid, ALICE_UID)

        task = self.service.enqueue_agent_task(
            self.requester, engine_id, "restart", unit="llmlab-svc.service"
        )

        # 队列落到 alice 的桶里：只有她的代理会来领。
        self.assertEqual(task.uid, ALICE_UID)
        self.assertEqual(task.requested_by, ACCOUNT)
        self.assertEqual(task.state, AGENT_TASK_QUEUED)

    def test_the_account_owner_can_read_back_the_task_it_asked_for(self):
        """写侧把任务排进 alice 的桶，读侧就不能让账户主人看不见自己刚下发的那条。"""

        engine_id = self.harness.make_engine(ACCOUNT, "svc")
        task = self.service.enqueue_agent_task(
            self.requester, engine_id, "start", unit="llmlab-svc.service"
        )

        self.assertEqual(task.uid, ALICE_UID)  # 桶不是账户自己的
        self.assertEqual(self.service.agent_task(self.requester, task.id).id, task.id)

    def test_another_account_sharing_the_bucket_cannot_see_that_task(self):
        """在同一个桶里 ≠ 是我的：``requested_by`` 必须全等。"""

        engine_id = self.harness.make_engine(ACCOUNT, "svc")
        task = self.service.enqueue_agent_task(
            self.requester, engine_id, "start", unit="llmlab-svc.service"
        )
        other = RequesterContext(
            principal=OTHER_ACCOUNT,
            uid=None,
            linux_user="bob",
            scopes=NON_ADMIN_SCOPES,
            roles=frozenset(),
        )

        with self.assertRaises(NotFound):
            self.service.agent_task(other, task.id)

    def test_an_engine_without_an_observed_uid_gets_no_agent(self):
        """没观测到运行 uid 时不能猜一个：说「没有可用代理」比投错人强。"""

        # 9000 在白名单里，但不在所有权 prover 的夹具里 —— 登记行因此没有 uid，
        # 落成哨兵值 -1（「账户自己没有 uid」）。
        engine_id = self.harness.make_engine(ACCOUNT, "svc-unobserved", port=9000)
        self.assertEqual(self.service.store.engine_by_id(engine_id).owner_uid, -1)

        with self.assertRaises(AccessDenied):
            self.service.enqueue_agent_task(
                self.requester, engine_id, "restart", unit="llmlab-svc.service"
            )


if __name__ == "__main__":
    unittest.main()
