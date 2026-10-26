"""``llmlab agent``：无凭据的那条命令。

端到端跑的是**真子进程**：只有子进程才能证明「没有 token 也能上报」—— 父进程里
残留的环境变量或已解析的 client 都可能把这件事掩盖过去。服务端是**真 socket**
（临时目录里的一个 unix socket），只有 ``report_agent`` 是被替换的，因为它属于
另一层，另有测试。

另有一组纯函数测试：扫描结果里绝不能出现归属字段（``run_as_uid`` /
``target_url`` / ``registered``），因为服务端会把带这些字段的条目整条丢掉。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cli.llmlab import agent as agent_mod
from cli.llmlab.entry import run as cli_run
from cli.llmlab.errors import EXIT_TRANSPORT
from backend.registry.agent_channel import AgentChannel
from backend.registry.agent_reports import REPORT_VERSION
from backend.registry.identity import Principal, PrincipalDirectory
from tests.registry.support import ROOT

#: 子进程里必须**没有**的变量：有任何一个，这条用例就测不出「不需要凭据」。
SCRUBBED_ENV = ("LLMLAB_TOKEN", "LLMLAB_TOKEN_FILE", "LLMLAB_API_BASE")


class _Receiver:
    """``report_agent`` 的替身：把收到的载荷记下来。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.requester = None

    def report_agent(self, requester, payload):
        self.requester = requester
        self.payloads.append(dict(payload))
        return SimpleNamespace(
            response=lambda: {
                "agent_id": payload.get("agent_id"),
                "host_id": "lab-01",
                "received_at": "2026-10-26T09:00:00+00:00",
                "accepted": {"engines": 0, "installed_engines": 0, "models": 0},
                "rejected": {},
                "problems": [],
            }
        )


class _FakeDiscovery:
    """``LocalDiscovery`` 的替身：三份清单与截断标志都由测试指定。"""

    def __init__(self, *, engines=(), models=(), installed=(), problems=(), model_problems=()):
        self._engines = list(engines)
        self._models = list(models)
        self._installed = list(installed)
        self._problems = list(problems)
        self._model_problems = list(model_problems)

    def engines(self, *, registered_targets):
        return self._engines, (self._problems[0] if self._problems else None)

    def models(self, *, registered_paths):
        return self._models, self._model_problems

    def installed_engines(self):
        return self._installed, []


def _engine(port=8000, framework="vllm", models=("qwen",)):
    return SimpleNamespace(
        port=port, target_url=f"http://127.0.0.1:{port}", run_as_uid=4001,
        run_as_user="alice", openai_compatible=True, models=tuple(models),
        framework=framework, registered=False, registered_by_me=False,
        suggested_name="qwen",
    )


def _model(path="/home/alice/m.gguf"):
    return SimpleNamespace(
        path=path, root="/home/alice", format="gguf", size_bytes=1024,
        architecture=None, model_type=None, registered=False,
        registered_by_me=False, suggested_name="m",
    )


def _installed():
    return SimpleNamespace(
        engine="llama.cpp", name="llama.cpp", found_as="executable",
        version=None, path="/usr/local/bin/llama-server", env=None,
    )


class CollectReportTests(unittest.TestCase):
    def test_the_payload_carries_no_ownership_fields(self) -> None:
        """归属由服务端写死。发了 ``run_as_uid`` 只会让整条被丢掉。"""

        payload = agent_mod.collect_report(
            agent_id="lab-01",
            discovery=_FakeDiscovery(engines=[_engine()], models=[_model()], installed=[_installed()]),
        )

        engine = payload["engines"][0]
        self.assertEqual(set(engine), {"port", "framework", "models", "suggested_name"})
        self.assertNotIn("run_as_uid", engine)
        self.assertNotIn("target_url", engine)

        self.assertEqual(payload["report_version"], REPORT_VERSION)
        self.assertEqual(payload["agent_id"], "lab-01")
        self.assertTrue(payload["full"])

    def test_a_truncated_walk_is_reported_as_incomplete(self) -> None:
        payload = agent_mod.collect_report(
            agent_id="lab-01",
            discovery=_FakeDiscovery(model_problems=["model_walk_truncated:/data"]),
        )

        self.assertFalse(payload["full"])
        self.assertIn("model_walk_truncated:/data", payload["problems"])

    def test_a_missing_proc_table_is_a_problem_not_a_crash(self) -> None:
        payload = agent_mod.collect_report(
            agent_id="lab-01", discovery=_FakeDiscovery(problems=["proc_net_unreadable"])
        )

        self.assertEqual(payload["engines"], [])
        self.assertIn("proc_net_unreadable", payload["problems"])

    def test_the_agent_id_comes_from_the_hostname_or_the_flag(self) -> None:
        self.assertEqual(agent_mod.default_agent_id(), agent_mod.default_agent_id())
        self.assertRegex(agent_mod.default_agent_id(), r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        payload = agent_mod.collect_report(agent_id="explicit", discovery=_FakeDiscovery())
        self.assertEqual(payload["agent_id"], "explicit")


class AgentCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket_path = str(Path(self.tmp.name) / "agent.sock")
        self.root = Path(self.tmp.name) / "models"
        self.root.mkdir()

        self.receiver = _Receiver()
        uid = os.getuid()
        self.channel = AgentChannel(
            path=self.socket_path,
            service=self.receiver,
            principals=PrincipalDirectory([Principal(f"linux:lab-01:{uid}", "me", uid)]),
        )
        self.addCleanup(self.channel.stop)
        self.assertFalse(self.channel.start().problem)

    def cli(self, *argv: str, expect: int = 0) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
        result = subprocess.run(
            [
                sys.executable, "-m", "cli.llmlab", "agent",
                "--socket", self.socket_path, "--root", str(self.root), *argv,
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_a_report_round_trips_without_any_token(self) -> None:
        result = self.cli("--json", "--agent-id", "lab-01")

        receipt = json.loads(result.stdout)["receipt"]
        self.assertEqual(receipt["host_id"], "lab-01")
        self.assertEqual(len(self.receiver.payloads), 1)
        self.assertEqual(self.receiver.payloads[0]["agent_id"], "lab-01")
        # 身份来自 socket，不是请求体。
        self.assertEqual(self.receiver.requester.uid, os.getuid())
        self.assertEqual(self.channel.status.reports, 1)

    def test_dry_run_paints_the_payload_and_touches_no_socket(self) -> None:
        result = self.cli("--dry-run")

        payload = json.loads(result.stdout)
        self.assertEqual(payload["report_version"], REPORT_VERSION)
        self.assertEqual(self.receiver.payloads, [])

    def test_a_missing_socket_exits_7_with_a_hint(self) -> None:
        result = self.cli("--socket", str(Path(self.tmp.name) / "absent.sock"), expect=EXIT_TRANSPORT)

        self.assertIn("no agent socket", result.stderr)
        self.assertIn("hint:", result.stderr)

    def test_the_agent_command_never_asks_for_a_token(self) -> None:
        """在进程内跑一遍 ``run``：没有 token 也不该走到「凭据缺失」。"""

        argv = ["agent", "--dry-run", "--socket", self.socket_path, "--root", str(self.root)]
        self.assertEqual(cli_run(argv), 0)

    def test_a_refused_report_becomes_an_exit_code_not_a_traceback(self) -> None:
        from backend.registry.errors import AccessDenied

        class _Refuse(_Receiver):
            def report_agent(self, requester, payload):
                raise AccessDenied("missing required scope registry:agent:report")

        self.channel.service = _Refuse()
        result = self.cli("--json", expect=3)

        self.assertIn("registry.access_denied", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unmapped_uid_explains_what_to_do(self) -> None:
        """内核给的 uid 在主体目录里查不到 —— 该找管理员，不是重试。"""

        self.channel.principals = PrincipalDirectory(())
        result = self.cli("--json", expect=3)

        self.assertIn("agent.uid_not_mapped", result.stderr)
        self.assertIn("管理员", result.stderr)


if __name__ == "__main__":
    unittest.main()
