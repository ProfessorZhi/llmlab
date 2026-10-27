"""代理任务的两条 BFF 路由：下发 start/stop/restart，按 id 查任务。

这里验的是**接线**，不是服务层语义：谁能下发由 Registry 的 owner 校验决定，
BFF 只负责把会话主体交过去、把请求体的形状收窄、把结果与状态码如实报出来。

最关键的一条断言是「202 + queued 且**不是** done」：平台下发的是任务，代理还
没领取、也没执行，控制台不能把「已下发」读成「服务已经起来」。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from tests.bff import support as S

SERVE = "/api/v2/registry/engines/{}/serve"
TASK = "/api/v2/registry/agent-tasks/{}"


class EngineServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llm-lab-bff-agent-task-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.h = S.build_harness(state_dir=self.tmp)
        self.addCleanup(self.h.close)
        # 引擎归 alice（Linux 主体，带受信 uid）；dave 是另一个人。
        self.engine_id = S.register_baseline(self.h, "alice")["engine_id"]
        self.owner = self.h.client("alice")
        self.other = self.h.client("dave")

    # --- helpers -------------------------------------------------------------

    def serve(self, client, engine_id=None, body=None):
        return client.run(
            "POST",
            SERVE.format(engine_id or self.engine_id),
            payload=body,
            headers={"X-CSRF-Token": S.csrf(client)},
        )

    def task(self, client, task_id):
        return client.run("GET", TASK.format(task_id))

    def anonymous(self):
        return S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    # --- tests ---------------------------------------------------------------

    def test_an_anonymous_caller_gets_401_on_both_routes(self):
        client = self.anonymous()
        # 写路由先把 CSRF 备齐：这里要验的是身份那道门，不是 CSRF。
        served = self.serve(client, body={"kind": "restart"})
        self.assertEqual(served.status, 401)
        fetched = self.task(client, "task_example")
        self.assertEqual(fetched.status, 401)

    def test_the_owner_enqueues_a_restart_and_gets_queued_not_done(self):
        result = self.serve(self.owner, body={"kind": "restart"})
        self.assertEqual(result.status, 202, result.body)
        task = result.json()["data"]
        self.assertEqual(task["state"], "queued")
        self.assertNotEqual(task["state"], "done")
        self.assertIsNone(task["result"])
        self.assertEqual(task["kind"], "restart")
        self.assertEqual(task["uid"], 4001)
        self.assertEqual(task["payload"]["engine_id"], self.engine_id)
        self.assertEqual(task["payload"]["kind"], "restart")
        # 请求体里没有的东西也不该跑进 payload（尤其 uid / 凭据）。
        self.assertNotIn("argv", task["payload"])
        self.assertEqual(set(task), {
            "id", "uid", "kind", "payload", "requested_by",
            "created_at", "state", "result", "updated_at",
        })

    def test_another_principal_cannot_enqueue_for_someone_elses_engine(self):
        result = self.serve(self.other, body={"kind": "restart"})
        self.assertEqual(result.status, 403, result.body)
        self.assertEqual(result.error_code(), "registry.access_denied")

    def test_self_asserted_fields_in_the_body_are_rejected_with_400(self):
        for field, value in (
            ("argv", ["systemctl", "restart", "qwen"]),
            ("uid", 4002),
            ("owner", "account:someone-else"),
            ("principal", "linux:other-host:4002"),
        ):
            with self.subTest(field=field):
                result = self.serve(self.owner, body={"kind": "restart", field: value})
                self.assertEqual(result.status, 400, result.body)
                self.assertEqual(result.error_code(), "registry.invalid_request")

    def test_an_unknown_kind_is_rejected_with_400(self):
        for kind in ("explode", "", None, 1):
            with self.subTest(kind=kind):
                result = self.serve(self.owner, body={"kind": kind})
                self.assertEqual(result.status, 400, result.body)
                self.assertEqual(result.error_code(), "registry.invalid_request")

    def test_only_the_requester_can_read_a_task_and_others_get_404(self):
        created = self.serve(self.owner, body={"kind": "start", "unit": "llmlab-qwen.service"})
        self.assertEqual(created.status, 202, created.body)
        task_id = created.json()["data"]["id"]
        self.assertEqual(created.json()["data"]["payload"]["unit"], "llmlab-qwen.service")

        mine = self.task(self.owner, task_id)
        self.assertEqual(mine.status, 200, mine.body)
        self.assertEqual(mine.json()["data"]["id"], task_id)
        self.assertEqual(mine.json()["data"]["state"], "queued")

        # 存在但无权知道 ⇒ 404，与「不存在」同一个答案（不可枚举）。
        theirs = self.task(self.other, task_id)
        self.assertEqual(theirs.status, 404, theirs.body)
        self.assertEqual(theirs.error_code(), "registry.not_found")

    def test_a_missing_task_is_404(self):
        self.assertEqual(self.task(self.owner, "task_does_not_exist").status, 404)
