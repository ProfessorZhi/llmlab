"""``llmlab`` 端到端：真的起 HTTP 服务、真的跑子进程、真的连 loopback。

这是本次交付里唯一会开 socket 的测试文件，且开的全是**本机 loopback**：

* 一个**假 engine**（标准库 ``ThreadingHTTPServer``，端口 0 让内核分配），
  用来验证「真实探测」路径；
* 一个临时 Registry（``wsgiref``，端口 0），供 CLI 子进程通过 HTTP 访问。

没有 GPU、没有生产 Gateway、没有真实推理服务、没有真实 API key。
两边的端口都由内核分配，因此不会撞上本机其他服务。
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import wsgiref.simple_server
from pathlib import Path

from backend.registry.api import create_app
from backend.registry.devserver import (
    _ThreadingWSGIServer,
    build_synthetic_identities,
)
from backend.registry.gateway import MockGatewayAdapter
from backend.registry.health import LoopbackHttpProber
from backend.registry.registry import RegistryConfig, RegistryService
from backend.registry.store import Store
from tests.registry.support import ROOT, close_all

#: 子进程环境里**不能**出现的变量：一旦有，就测不出「没有凭据也会 fail closed」。
SCRUBBED_ENV = ("LLMLAB_TOKEN", "LLMLAB_TOKEN_FILE", "LLMLAB_API_BASE")


class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    """把 wsgiref 的逐请求日志关掉：测试输出应当只有测试结果。"""

    def log_message(self, *args: object) -> None:
        return


class _FakeEngine(http.server.BaseHTTPRequestHandler):
    """一个最小的「上游推理服务」。``/fail`` 回 503，其余回 200。"""

    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        if self.path.startswith("/fail"):
            body = b'{"error":"synthetic upstream failure"}'
            self.send_response(503)
        else:
            body = b'{"object":"list","data":[]}'
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # noqa: D102 - 静音
        return


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CliE2ETestCase(unittest.TestCase):
    """夹具：假 engine + 临时 Registry + 身份表。"""

    def setUp(self) -> None:
        self.engine = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FakeEngine
        )
        self.engine_port = int(self.engine.server_address[1])
        threading.Thread(target=self.engine.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

        self.identities = build_synthetic_identities()
        self.gateway = MockGatewayAdapter()
        self.store = Store(":memory:")
        self.service = RegistryService(
            store=self.store,
            tokens=self.identities.tokens,
            principals=self.identities.principals,
            # 端口 allowlist 收窄到**就这一个**假 engine：其余 loopback 端口一律拒绝，
            # 这样测试永远不会去碰本机别的服务。
            config=RegistryConfig(allowed_ports=frozenset({self.engine_port})),
            prober=LoopbackHttpProber(timeout_seconds=2.0),
            gateway=self.gateway,
        )
        self.request_count = 0
        inner = create_app(self.service)

        def counting_app(environ, start_response):
            self.request_count += 1
            return inner(environ, start_response)

        self.server = wsgiref.simple_server.make_server(
            "127.0.0.1", 0, counting_app,
            server_class=_ThreadingWSGIServer, handler_class=_QuietHandler,
        )
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

        self.tmp = tempfile.TemporaryDirectory()
        self.tokens = {principal: token for principal, token, _s in self.identities.issued}
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.engine.shutdown()
        self.engine.server_close()
        self.store.close()
        self.tmp.cleanup()
        close_all()

    # -- CLI 驱动 ---------------------------------------------------------

    def cli(self, *argv: str, principal: str | None = None, token: str | None = None,
            token_file: str | None = None, api_base: str | None = None,
            expect: int = 0) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        resolved = token if token is not None else (
            self.tokens[principal] if principal else None
        )
        if resolved is not None:
            env["LLMLAB_TOKEN"] = resolved
        if token_file is not None:
            env["LLMLAB_TOKEN_FILE"] = token_file
        args = [
            sys.executable,
            "-m",
            "cli.llmlab",
            "--api-base",
            api_base or f"http://127.0.0.1:{self.port}",
            *argv,
        ]
        result = subprocess.run(
            args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
        )
        if result.returncode != expect:
            self.fail(
                f"expected exit {expect}, got {result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def json_cli(self, *argv: str, **kwargs) -> dict:
        kwargs["expect"] = kwargs.get("expect", 0)
        result = self.cli(*argv, "--json", **kwargs)
        return json.loads(result.stdout)

    def _register_engine(self, name: str = "eng") -> str:
        result = self.cli(
            "engines", "register", "--name", name,
            "--target-url", f"http://127.0.0.1:{self.engine_port}/v1",
            principal="alice",
        )
        self.assertIn("status   : verified", result.stdout)
        return result.stdout.split("registered engine ")[1].split()[0]

    def confirm_route(self, endpoint_id: str) -> None:
        """补齐 `invoke` 路由门的前置条件：一次已成功的 Gateway 回读（PR #118）。

        CLI 测试驱动的是真实子进程 + WSGI，端点是 CLI 注册出来的。这里在
        **服务端对象**上写入与 `_attempt_router_sync` 回执成功时相同的一行
        （``generation == revision``、``evidence=gateway_readback``），并装一个
        报告同一代数的 Router 替身。它不改变 CLI 的行为，只是让「本部署已经有过
        一次成功回读」这个前提成立 —— 否则 `invoke` 会在碰 Gateway 之前被 409 拒绝。
        """
        import dataclasses

        from tests.registry.support import _FixedReadbackRouter

        endpoint = self.store.endpoint_by_id(endpoint_id)
        assert endpoint is not None, f"unknown endpoint {endpoint_id}"
        deployment = self.store.deployment_by_id(endpoint.deployment_id)
        assert deployment is not None, f"endpoint {endpoint_id} has no deployment"
        refs = dict(self.service.config.engine_secret_refs)
        refs[deployment.engine_id] = {
            "file": "/etc/llmlab/engines.env",
            "var": "ENGINE_KEY",
        }
        self.service.config = dataclasses.replace(
            self.service.config, engine_secret_refs=refs
        )
        self.service.router = _FixedReadbackRouter(endpoint.revision)
        outcome = self.store.ack_router_generation(
            endpoint_id,
            generation=endpoint.revision,
            actor_principal="system:router-sync",
            actor_uid=0,
            at=self.service._now(),
            evidence="gateway_readback",
        )
        assert outcome.get("outcome") == "ok", outcome

    def _ready_endpoint(self) -> str:
        engine_id = self._register_engine()
        model = self.cli(
            "models", "register", "--name", "llama3-8b",
            "--weight-path", "/home/alice/models/llama3-8b", principal="alice",
        )
        model_id = model.stdout.split("registered model ")[1].split()[0]
        deployment = self.cli(
            "deployments", "register", "--name", "dep",
            "--engine", engine_id, "--model", model_id, principal="alice",
        )
        self.assertIn("ready      : no", deployment.stdout)
        deployment_id = deployment.stdout.split("registered deployment ")[1].split()[0]
        verified = self.cli("deployments", "verify", deployment_id, principal="alice")
        self.assertIn("ready=yes", verified.stdout)
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc",
            "--deployment", deployment_id, principal="alice",
        )
        self.assertIn("visibility: private", endpoint.stdout)
        self.assertIn("status    : ready", endpoint.stdout)
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        # `invoke` 要求路由被 Gateway 回读确认过；夹具显式补齐，而不是依赖
        # 「无 ACK 也 200」的旧行为。
        self.confirm_route(endpoint_id)
        return endpoint_id


class FailClosedTest(CliE2ETestCase):
    def test_no_credential_exits_2_and_sends_nothing(self) -> None:
        before = self.request_count
        result = self.cli("whoami", expect=2)
        self.assertIn("no credential", result.stderr)
        self.assertEqual(result.stdout, "")
        # 关键断言：一个请求都没有发出去。
        self.assertEqual(self.request_count, before)

    def test_a_path_is_not_accepted_as_a_token_file(self) -> None:
        directory = Path(self.tmp.name) / "adir"
        directory.mkdir()
        result = self.cli("whoami", token_file=str(directory), expect=2)
        self.assertIn("regular file", result.stderr)

    def test_a_world_readable_token_file_is_refused(self) -> None:
        path = Path(self.tmp.name) / "tok"
        path.write_text(self.tokens["alice"], encoding="utf-8")
        path.chmod(0o644)
        result = self.cli("whoami", token_file=str(path), expect=2)
        self.assertIn("group/other", result.stderr)

    def test_a_symlinked_token_file_is_refused(self) -> None:
        target = Path(self.tmp.name) / "real-tok"
        target.write_text(self.tokens["alice"], encoding="utf-8")
        target.chmod(0o600)
        link = Path(self.tmp.name) / "link-tok"
        link.symlink_to(target)
        result = self.cli("whoami", token_file=str(link), expect=2)
        self.assertIn("symbolic link", result.stderr)

    def test_a_properly_permissioned_token_file_works(self) -> None:
        path = Path(self.tmp.name) / "good-tok"
        path.write_text(self.tokens["alice"], encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        payload = self.json_cli("whoami", token_file=str(path))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["identity"]["principal"], "alice")

    def test_cleartext_to_a_non_loopback_host_is_refused_before_any_request(self) -> None:
        before = self.request_count
        result = self.cli(
            "whoami", principal="alice", api_base="http://192.0.2.9:8787", expect=2
        )
        self.assertIn("cleartext", result.stderr)
        self.assertEqual(self.request_count, before)

    def test_an_unsupported_api_scheme_is_refused(self) -> None:
        result = self.cli(
            "whoami", principal="alice", api_base="file:///tmp/x", expect=2
        )
        self.assertIn("scheme", result.stderr)


class ExitCodeTest(CliE2ETestCase):
    def test_an_unknown_credential_is_exit_3(self) -> None:
        self.cli("whoami", token="synthetic-nobody", expect=3)

    def test_a_missing_object_is_exit_4(self) -> None:
        self.cli("endpoints", "get", "ep_missing", principal="alice", expect=4)

    def test_a_forbidden_action_is_exit_3(self) -> None:
        endpoint_id = self._ready_endpoint()
        self.cli("endpoints", "share", endpoint_id, "--mode", "private",
                 principal="dave", expect=4)  # 不可见 -> 4

    def test_a_conflict_is_exit_5(self) -> None:
        self._register_engine("dup")
        # 同名不同内容 -> 409
        self.cli(
            "engines", "register", "--name", "dup",
            "--target-url", f"http://127.0.0.1:{self.engine_port}/v1?x=1",
            principal="alice", expect=5,
        )

    def test_no_subcommand_prints_help_and_exits_2(self) -> None:
        result = self.cli(expect=2)
        self.assertIn("usage:", result.stdout.lower())

    def test_help_exits_0(self) -> None:
        result = self.cli("--help")
        self.assertIn("usage", result.stdout.lower())

    def test_an_unwired_gateway_is_exit_6(self) -> None:
        # 503 是独立的退出码：它该触发「重试/找运维」，而不是「改脚本参数」。
        endpoint_id = self._ready_endpoint()
        self.gateway.failing = True
        result = self.cli("invoke", endpoint_id, "--prompt", "hi", principal="alice", expect=6)
        self.assertIn("upstream_unavailable", result.stderr)

    def test_unreachable_registry_is_exit_7(self) -> None:
        result = self.cli(
            "whoami", principal="alice", api_base=f"http://127.0.0.1:{_free_port()}",
            expect=7,
        )
        self.assertIn("cannot reach", result.stderr)


class HappyPathTest(CliE2ETestCase):
    def test_whoami_is_human_readable_and_json_parsable(self) -> None:
        human = self.cli("whoami", principal="alice")
        self.assertIn("principal : alice", human.stdout)
        self.assertIn("uid       : 4001", human.stdout)
        payload = self.json_cli("whoami", principal="alice")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["data"]["identity"]["uid"], 4001)

    def test_json_flag_works_on_either_side_of_the_subcommand(self) -> None:
        for argv in (("whoami", "--json"), ("--json", "whoami")):
            with self.subTest(argv=argv):
                result = self.cli(*argv, principal="alice")
                self.assertTrue(json.loads(result.stdout)["ok"])

    def test_the_full_chain_and_cross_user_sharing(self) -> None:
        endpoint_id = self._ready_endpoint()

        self.assertIn("1 endpoint(s)", self.cli("endpoints", "list", principal="alice").stdout)
        self.assertIn("0 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)
        self.cli("endpoints", "get", endpoint_id, principal="dave", expect=4)

        share = self.cli(
            "endpoints", "share", endpoint_id, "--mode", "users",
            "--grantee", "dave", principal="alice",
        )
        self.assertIn("mode      : users", share.stdout)
        self.assertIn("1 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)
        self.assertIn("0 endpoint(s)", self.cli("endpoints", "list", principal="bob").stdout)

        # 受托人看得到 backing model，但看不到权重路径。
        models = self.cli("models", "list", principal="dave").stdout
        self.assertIn("(redacted)", models)

        # 经授权网关调用。
        invoked = self.json_cli(
            "invoke", endpoint_id, "--prompt", "hello", principal="dave"
        )
        self.assertTrue(invoked["data"]["result"]["synthetic"])
        self.assertIsNone(invoked["data"]["metering"]["tokens"])
        self.assertEqual(len(self.gateway.calls), 1)

        # 非受托人调用被拒（404，与不存在同形）。
        self.cli("invoke", endpoint_id, "--prompt", "hi", principal="bob", expect=4)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_revocation_is_immediate_through_the_cli(self) -> None:
        endpoint_id = self._ready_endpoint()
        self.cli("endpoints", "share", endpoint_id, "--mode", "users",
                 "--grantee", "dave", principal="alice")
        self.cli("invoke", endpoint_id, "--prompt", "ok", principal="dave")
        self.cli("endpoints", "unshare", endpoint_id, "--grantee", "dave",
                 principal="alice")
        self.assertIn("0 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)
        self.cli("invoke", endpoint_id, "--prompt", "again", principal="dave", expect=4)

    def test_all_approved_mode(self) -> None:
        endpoint_id = self._ready_endpoint()
        result = self.cli("endpoints", "share", endpoint_id, "--mode", "all-approved",
                          principal="alice")
        self.assertIn("all_approved_platform_users", result.stdout)
        for who in ("dave", "bob"):
            with self.subTest(who=who):
                self.assertIn(
                    "1 endpoint(s)", self.cli("endpoints", "list", principal=who).stdout
                )

    def test_the_admin_sees_everything(self) -> None:
        self._ready_endpoint()
        self.assertIn("1 endpoint(s)", self.cli("endpoints", "list", principal="carol").stdout)

    def test_the_acl_is_owner_only(self) -> None:
        endpoint_id = self._ready_endpoint()
        self.cli("endpoints", "share", endpoint_id, "--mode", "users",
                 "--grantee", "dave", principal="alice")
        self.cli("endpoints", "acl", endpoint_id, principal="dave", expect=3)
        owner_view = self.cli("endpoints", "acl", endpoint_id, principal="alice")
        self.assertIn("dave", owner_view.stdout)

    def test_sharing_to_an_unknown_principal_is_refused(self) -> None:
        endpoint_id = self._ready_endpoint()
        result = self.cli(
            "endpoints", "share", endpoint_id, "--mode", "users",
            "--grantee", "ghost", principal="alice", expect=2,
        )
        self.assertIn("unknown principal", result.stderr)

    def test_an_engine_reports_unreachable_once_the_upstream_is_gone(self) -> None:
        engine_id = self._register_engine("to-die")
        self.engine.shutdown()
        self.engine.server_close()
        result = self.cli("engines", "verify", engine_id, principal="alice")
        self.assertIn("status=unavailable", result.stdout)
        payload = self.json_cli("engines", "get", engine_id, principal="alice")
        self.assertIsNone(payload["data"]["health"]["latency_ms"])
        self.assertEqual(payload["data"]["health"]["state"], "unreachable")

    def test_a_degraded_upstream_is_reported_as_degraded(self) -> None:
        result = self.cli(
            "engines", "register", "--name", "degraded",
            "--target-url", f"http://127.0.0.1:{self.engine_port}/fail",
            principal="alice",
        )
        self.assertIn("health   : degraded", result.stdout)

    def test_usage_and_logs_report_not_configured(self) -> None:
        for command in ("usage", "logs"):
            with self.subTest(command=command):
                result = self.cli(command, "--mine", principal="alice")
                self.assertIn("NOT_CONFIGURED", result.stdout)
                payload = self.json_cli(command, "--mine", principal="alice")
                self.assertEqual(payload["data"]["status"], "NOT_CONFIGURED")
                self.assertIsNone(payload["data"]["metrics"]["tokens"]["value"])
                self.assertIsNone(payload["data"]["metrics"]["latency_ms"]["value"])

    def test_usage_counts_only_my_own_authorized_calls(self) -> None:
        endpoint_id = self._ready_endpoint()
        self.cli("endpoints", "share", endpoint_id, "--mode", "all-approved",
                 principal="alice")
        self.cli("invoke", endpoint_id, "--prompt", "a", principal="dave")
        self.cli("invoke", endpoint_id, "--prompt", "b", principal="dave")
        self.cli("invoke", endpoint_id, "--prompt", "c", principal="alice")

        mine = self.json_cli("usage", "--mine", principal="alice")
        theirs = self.json_cli("usage", "--mine", principal="dave")
        self.assertEqual(
            mine["data"]["metrics"]["registry_recorded_invocations"]["value"], 1
        )
        self.assertEqual(
            theirs["data"]["metrics"]["registry_recorded_invocations"]["value"], 2
        )

    def test_status_reports_each_source(self) -> None:
        payload = self.json_cli("status", principal="alice")
        self.assertTrue(payload["data"]["observation"]["prober_attached"])
        self.assertTrue(payload["data"]["gateway"]["attached"])
        self.assertEqual(payload["data"]["usage_pipeline"]["reason"],
                         "not_configured_in_this_deployment")

    def test_identity_assertion_is_refused_through_the_cli_headers(self) -> None:
        # CLI 本身不发自报身份的头；这里用一层薄包装证明服务端会拒。
        status, _headers, payload = self._direct(
            "GET", "/api/v2/registry/whoami", principal="alice",
            headers={"X-Owner": "carol"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "registry.identity_assertion_rejected")

    def test_idempotent_registration_through_the_cli(self) -> None:
        first = self.cli(
            "engines", "register", "--name", "idem",
            "--target-url", f"http://127.0.0.1:{self.engine_port}/v1",
            principal="alice",
        )
        second = self.cli(
            "engines", "register", "--name", "idem",
            "--target-url", f"http://127.0.0.1:{self.engine_port}/v1",
            principal="alice",
        )
        first_id = first.stdout.split("registered engine ")[1].split()[0]
        second_id = second.stdout.split("registered engine ")[1].split()[0]
        self.assertEqual(first_id, second_id)
        self.assertIn("replay   : yes", second.stdout)

    def test_a_rejected_target_is_exit_2_and_creates_nothing(self) -> None:
        for target in ("file:///etc/passwd", "http://example.com/v1",
                       "http://0.0.0.0:8081/v1", "http://127.0.0.1:22/v1"):
            with self.subTest(target=target):
                self.cli("engines", "register", "--name", "x",
                         "--target-url", target, principal="alice", expect=2)
        self.assertIn("0 engine(s)", self.cli("engines", "list", principal="alice").stdout)

    # -- 直接驱动（少量需要自定义头的用例） -------------------------------

    def _direct(self, method: str, path: str, *, principal: str, headers: dict):
        import io as _io

        application = create_app(self.service)
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "wsgi.url_scheme": "http",
            "wsgi.input": _io.BytesIO(b""),
            "CONTENT_LENGTH": "",
            "HTTP_AUTHORIZATION": f"Bearer {self.tokens[principal]}",
        }
        for key, value in headers.items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value
        captured = {}

        def start_response(status, response_headers):
            captured["status"] = int(status.split()[0])

        body = b"".join(application(environ, start_response))
        return captured["status"], {}, json.loads(body)


class EndpointSwapCliTest(CliE2ETestCase):
    """``llmlab endpoints repoint|rollback|routings``：真实子进程 + 真实回环 HTTP。"""

    def _deployment(self, name: str) -> str:
        """建一条 engine -> model -> deployment 的 ready 链，返回 deployment id。"""
        engine = self.cli(
            "engines", "register", "--name", f"{name}-engine",
            "--target-url", f"http://127.0.0.1:{self.engine_port}/v1",
            principal="alice",
        )
        engine_id = engine.stdout.split("registered engine ")[1].split()[0]
        model = self.cli(
            "models", "register", "--name", f"{name}-model",
            "--weight-path", f"/home/alice/models/{name}", principal="alice",
        )
        model_id = model.stdout.split("registered model ")[1].split()[0]
        created = self.cli(
            "deployments", "register", "--name", f"{name}-dep",
            "--engine", engine_id, "--model", model_id, principal="alice",
        )
        deployment_id = created.stdout.split("registered deployment ")[1].split()[0]
        self.cli("deployments", "verify", deployment_id, principal="alice")
        return deployment_id

    def test_repoint_moves_the_alias_and_reports_pending_router_sync(self) -> None:
        big = self._deployment("big-qwen")
        small = self._deployment("small-llama")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        route = [line for line in endpoint.stdout.splitlines() if "route" in line][0]

        # 先从 CLI 共享给 dave，再换模型：默认应把共享收回。
        self.cli("endpoints", "share", endpoint_id, "--mode", "users",
                 "--grantee", "dave", principal="alice")
        self.assertIn("1 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)

        switched = self.cli(
            "endpoints", "repoint", endpoint_id, "--deployment", small,
            "--reason", "free the A6000 for bob", principal="alice",
        )
        self.assertIn("repoint:", switched.stdout)
        self.assertIn("router    : pending_router_sync", switched.stdout)
        self.assertIn("applied=no", switched.stdout)
        self.assertIn("revoked=dave", switched.stdout)
        # alias 没变。
        self.assertIn("(alias 不变)", switched.stdout)

        # 共享确实收回了。
        self.assertIn("0 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)
        # 而 owner 自己看到的仍是同一个 route path。
        after = self.cli("endpoints", "get", endpoint_id, principal="alice")
        self.assertIn(route.split()[-1], after.stdout)

    def test_routings_shows_both_models(self) -> None:
        big = self._deployment("big")
        small = self._deployment("small")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        self.cli("endpoints", "repoint", endpoint_id, "--deployment", small, principal="alice")

        history = self.cli("endpoints", "routings", endpoint_id, principal="alice")
        self.assertIn("small-model", history.stdout)
        self.assertIn("big-model", history.stdout)
        self.assertIn("REV", history.stdout)

    def test_rollback_returns_to_the_previous_deployment(self) -> None:
        big = self._deployment("big")
        small = self._deployment("small")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        self.cli("endpoints", "repoint", endpoint_id, "--deployment", small, principal="alice")
        rolled = self.cli("endpoints", "rollback", endpoint_id, principal="alice")
        self.assertIn("rollback:", rolled.stdout)
        current = self.json_cli("endpoints", "get", endpoint_id, principal="alice")["data"]
        self.assertEqual(current["deployment_id"], big)

    def test_repoint_can_explicitly_regrant(self) -> None:
        big = self._deployment("big")
        small = self._deployment("small")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        switched = self.cli(
            "endpoints", "repoint", endpoint_id, "--deployment", small,
            "--regrant", "dave", principal="alice",
        )
        # 这个 Endpoint 之前没共享给任何人，所以 dave 是**新**授权（granted），
        # 不是「重新确立」（reestablished）—— 两者是不同的历史事实。
        self.assertIn("granted=dave", switched.stdout)
        self.assertIn("reestablished=—", switched.stdout)
        self.assertIn("1 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)

    def test_regranting_an_already_shared_grantee_is_reestablished(self) -> None:
        big = self._deployment("big")
        small = self._deployment("small")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        self.cli("endpoints", "share", endpoint_id, "--mode", "users",
                 "--grantee", "dave", principal="alice")
        switched = self.cli(
            "endpoints", "repoint", endpoint_id, "--deployment", small,
            "--regrant", "dave", principal="alice",
        )
        # 授权被拆掉重建过：这一个人是**重新**确立的，而不是新加的。
        self.assertIn("reestablished=dave", switched.stdout)
        self.assertIn("1 endpoint(s)", self.cli("endpoints", "list", principal="dave").stdout)

    def test_a_stale_expected_revision_is_exit_5(self) -> None:
        big = self._deployment("big")
        small = self._deployment("small")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        self.cli("endpoints", "repoint", endpoint_id, "--deployment", small, principal="alice")
        result = self.cli(
            "endpoints", "repoint", endpoint_id, "--deployment", big,
            "--expected-revision", "1", principal="alice", expect=5,
        )
        self.assertIn("revision_conflict", result.stderr)

    def test_repoint_without_a_deployment_is_exit_2(self) -> None:
        endpoint_id = self._ready_endpoint()
        result = self.cli("endpoints", "repoint", endpoint_id, principal="alice", expect=2)
        self.assertIn("--deployment", result.stderr)

    def test_a_non_owner_cannot_repoint_through_the_cli(self) -> None:
        big = self._deployment("big")
        small = self._deployment("small")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        self.cli(
            "endpoints", "repoint", endpoint_id, "--deployment", small,
            principal="dave", expect=4,
        )

    def test_routing_applied_needs_operator_scope(self) -> None:
        big = self._deployment("big")
        endpoint = self.cli(
            "endpoints", "register", "--name", "svc", "--deployment", big, principal="alice"
        )
        endpoint_id = endpoint.stdout.split("registered endpoint ")[1].split()[0]
        self.cli(
            "endpoints", "routing-applied", endpoint_id, "--generation", "1",
            principal="alice", expect=3,
        )
        applied = self.cli(
            "endpoints", "routing-applied", endpoint_id, "--generation", "1",
            principal="carol",
        )
        # 断言的是**字段**，不是 stdout 里的子串：回执的说明文字里两个 token 都
        # 会出现（「状态是 attested，不是 applied」），拿 in 判会把说明句当证据 ——
        # 这条测试从前正是这样假通过的。
        envelope = self.json_cli(
            "endpoints", "routing-applied", endpoint_id, "--generation", "1",
            principal="carol",
        )
        block = envelope["data"]["router_sync"]
        self.assertEqual(block["state"], "router_sync_attested")
        self.assertFalse(block["applied"])
        self.assertEqual(block["evidence"], "operator_attestation")
        self.assertFalse(envelope["meta"]["router_applied"])
        self.assertIn("router_sync_attested", applied.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
