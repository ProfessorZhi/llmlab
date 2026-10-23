"""§4.1 的验收条件，尽量少用替身：

    起一个假引擎 ⇒ 出现在「我的服务」；停掉 ⇒ offline，行还在。

这里只有两件事是假的——**时钟**（不真等 30 s）和**服务本身**（一个真的
HTTP 服务器，不是 mock 的 fetch）。``/proc/net/tcp`` 是真的，端口属主是真的，
HTTP 探测是真的。如果这台机器读不了 ``/proc``，整个文件跳过而不是假装通过。
"""

from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from backend.registry.identity import Principal, PrincipalDirectory
from backend.registry.ingest_runner import IngestRunner
from backend.registry.ingest_scheduler import IngestScheduler
from backend.registry.models import ENGINE_RUNTIME_ATTACHED, ENGINE_RUNTIME_OFFLINE
from backend.registry.observed_services import auto_engine_id
from backend.registry.ownership import ProcNetListenerTable, ProcNetUnavailable
from backend.registry.store import Store

MODEL_ID = "qwen3-4b-instruct-q4_k_m"


class _EngineHandler(http.server.BaseHTTPRequestHandler):
    """最小的 OpenAI 兼容服务：只认 /v1/models，其余一律 404。"""

    def do_GET(self):  # noqa: N802 - http.server 的接口
        if self.path == "/v1/models":
            body = json.dumps(
                {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):  # 测试输出里不需要访问日志
        pass


def _proc_net_available() -> bool:
    try:
        ProcNetListenerTable().listeners()
    except ProcNetUnavailable:
        return False
    return True


@unittest.skipUnless(_proc_net_available(), "这台机器读不了 /proc/net/tcp")
class LiveIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "registry.sqlite3")
        self.addCleanup(self.store.close)

        self.uid = os.getuid()
        # 目录里放的是**本进程**的 uid：/proc 里的端口属主就是它，不需要伪造。
        self.directory = PrincipalDirectory([Principal("owner", "owner", self.uid)])
        self.runner = IngestRunner(
            store=self.store,
            directory=self.directory,
            table=ProcNetListenerTable(),
            probe_budget_seconds=15.0,
        )
        self.server = None

    def start_engine(self) -> int:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EngineHandler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_engine)
        return self.server.server_address[1]

    def stop_engine(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def engine(self, port: int):
        return self.store.engine_by_id(auto_engine_id(self.uid, port))

    def test_a_running_engine_is_attached_and_then_goes_offline(self):
        port = self.start_engine()

        self.runner.tick()
        engine = self.engine(port)
        self.assertIsNotNone(engine, "真实 HTTP 服务应当被自动接入")
        self.assertEqual(engine.state, ENGINE_RUNTIME_ATTACHED)
        self.assertEqual(engine.launch["model_ids"], [MODEL_ID])
        self.assertEqual(engine.target["url"], f"http://127.0.0.1:{port}")

        self.stop_engine()
        self.runner.tick()
        self.assertEqual(self.engine(port).state, ENGINE_RUNTIME_ATTACHED)
        self.runner.tick()

        offline = self.engine(port)
        self.assertEqual(offline.state, ENGINE_RUNTIME_OFFLINE)
        self.assertEqual(offline.id, auto_engine_id(self.uid, port))

    def test_a_port_belonging_to_someone_else_is_never_probed(self):
        """uid 不在映射里 ⇒ 连探测都不发。否则一轮会去敲遍全机的回环端口。"""

        port = self.start_engine()
        probes: list[int] = []

        def counting_fetch(port_number: int, _path: str):
            probes.append(port_number)
            return None

        self.runner.fetch = counting_fetch
        self.directory = PrincipalDirectory([Principal("someone", "someone", self.uid + 1)])
        self.runner.directory = self.directory

        self.runner.tick()

        self.assertNotIn(port, probes)
        self.assertIsNone(self.engine(port))


class SchedulerTest(unittest.TestCase):
    """调度器只负责「反复跑」和「出错不停」，不碰外部世界。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "registry.sqlite3")
        self.addCleanup(self.store.close)

    def _scheduler(self, ticks) -> IngestScheduler:
        class FakeRunner:
            def __init__(self) -> None:
                self.calls = 0

            def tick(self):
                self.calls += 1
                return ticks(self.calls)

        quiet = IngestScheduler(
            runner=FakeRunner(), interval_seconds=0.01, on_error=lambda _detail: None
        )
        self.addCleanup(quiet.shutdown)
        return quiet

    def test_a_failing_round_does_not_stop_the_loop(self):
        def boom(call):
            if call == 1:
                raise RuntimeError("磁盘满了")
            return "ok"

        scheduler = self._scheduler(boom)

        self.assertIsNone(scheduler.run_once())
        self.assertEqual(scheduler.failures, 1)
        self.assertIn("磁盘满了", scheduler.last_error)

        self.assertEqual(scheduler.run_once(), "ok")
        self.assertEqual(scheduler.rounds, 1)
        # 失败一次之后成功，也不清空 last_error：这个循环值得被看一眼。
        self.assertIsNotNone(scheduler.last_error)

    def test_shutdown_stops_the_thread_promptly(self):
        scheduler = self._scheduler(lambda _call: None)
        thread = scheduler.start()

        self.assertTrue(thread.is_alive())
        scheduler.shutdown(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(scheduler.stop.is_set())
        self.assertGreaterEqual(scheduler.rounds, 1, "起来先跑一轮，不等 30 s")


if __name__ == "__main__":
    unittest.main()
