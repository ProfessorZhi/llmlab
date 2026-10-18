"""两根轴：**传输可达** 与 **协议是模型服务** —— 它们不是同一件事。

原来的判定是「2xx 即 healthy」，于是任何一个在回环端口上应答 200 的东西
（静态文件服务器、别的用户的服务、随便一个 daemon）都能被登记成「模型就绪」。
这一组测试把那个等号拆掉。

用到的上游是**本进程临时起的 loopback 模拟器**（端口由内核分配），
没有 GPU、没有生产服务、没有真实用户进程。
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import unittest

from backend.registry.errors import InvalidRequest
from backend.registry.health import (
    ALLOWED_HEALTH_PATHS,
    ALLOWED_MODEL_LIST_PATHS,
    PROTOCOL_UNKNOWN,
    PROTOCOL_UNVERIFIED,
    PROTOCOL_VERIFIED,
    REASON_MODEL_LIST_MISSING,
    REASON_MODEL_LIST_VERIFIED,
    REASON_NO_MODEL_IDENTITY_FOR_PROTOCOL,
    REASON_NOT_CONFIGURED,
    REASON_TRANSPORT_UNREACHABLE,
    LoopbackProtocolProber,
    ProtocolObservation,
    StaticProtocolProber,
    parse_model_list,
)
from backend.registry.registry import RegistryConfig
from backend.registry.urls import validate_engine_target_url
from tests.registry.support import Harness, close_all

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _CannedServer(http.server.BaseHTTPRequestHandler):
    """按路径给固定应答的 loopback 模拟器。记录它被请求过哪些路径。"""

    protocol_version = "HTTP/1.0"
    #: ``{path: (status, content_type, body)}``；未列出的路径回 404。
    routes: dict = {}
    seen: list = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        type(self).seen.append(self.path)
        status, content_type, body = type(self).routes.get(
            self.path, (404, "application/json", b'{"error":"not found"}')
        )
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


class LoopbackSimulator:
    """一次性 loopback 上游。``routes`` 决定它长什么样。"""

    def __init__(self, routes: dict) -> None:
        handler = type("_Handler", (_CannedServer,), {"routes": routes, "seen": []})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self.server.server_address[1])
        self.handler = handler
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def seen(self) -> list:
        return list(self.handler.seen)


MODEL_LIST_BODY = json.dumps(
    {"object": "list", "data": [{"id": "qwen-big"}, {"id": "llama-small"}]}
)


class ParseModelListTest(unittest.TestCase):
    def test_a_proper_openai_model_list_is_accepted(self) -> None:
        self.assertEqual(parse_model_list(MODEL_LIST_BODY.encode()), 2)

    def test_an_empty_list_is_still_a_model_service(self) -> None:
        # 空清单说明「它确实会说模型清单，只是现在没有模型」——
        # 与「这不是个模型服务」是两件不同的事。
        self.assertEqual(parse_model_list(b'{"object":"list","data":[]}'), 0)

    def test_the_object_field_may_be_absent(self) -> None:
        self.assertEqual(parse_model_list(b'{"data":[{"id":"a"}]}'), 1)

    def test_every_entry_must_carry_an_id(self) -> None:
        self.assertIsNone(parse_model_list(b'{"data":[{"name":"a"}]}'))
        self.assertIsNone(parse_model_list(b'{"data":[{"id":""}]}'))
        self.assertIsNone(parse_model_list(b'{"data":[{"id":1}]}'))

    def test_arbitrary_json_is_not_a_model_list(self) -> None:
        for body in (
            b"{}",
            b'{"data": "not-a-list"}',
            b'{"data": {"id": "a"}}',
            b'{"object": "chat.completion", "data": [{"id": "a"}]}',
            b"[1,2,3]",
            b"hello",
            b"<html></html>",
            b"",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_model_list(body))

    def test_non_utf8_is_rejected(self) -> None:
        self.assertIsNone(parse_model_list(b"\xff\xfe\x00"))

    def test_the_probe_paths_are_a_fixed_allowlist(self) -> None:
        # 「协议验证」绝不能用用户给的任意 path，否则它就是一个通用本机探测器。
        self.assertIn("/v1/models", ALLOWED_MODEL_LIST_PATHS)
        self.assertIn("/models", ALLOWED_MODEL_LIST_PATHS)
        # health 类路径存在，但**不**能给出模型身份。
        self.assertTrue(set(ALLOWED_HEALTH_PATHS) & set(ALLOWED_MODEL_LIST_PATHS) == set())


class LoopbackProtocolProberTest(unittest.TestCase):
    def setUp(self) -> None:
        self.prober = LoopbackProtocolProber(timeout_seconds=2.0)
        self.simulators: list[LoopbackSimulator] = []
        self.addCleanup(self._stop_all)

    def _stop_all(self) -> None:
        for simulator in self.simulators:
            simulator.stop()

    def _serve(self, routes: dict) -> LoopbackSimulator:
        simulator = LoopbackSimulator(routes)
        self.simulators.append(simulator)
        return simulator

    def _target(self, port: int, path: str = "/v1"):
        return validate_engine_target_url(
            f"http://127.0.0.1:{port}{path}", allowed_ports=frozenset({port})
        )

    def test_a_real_model_list_is_verified(self) -> None:
        simulator = self._serve(
            {"/v1/models": (200, "application/json", MODEL_LIST_BODY)}
        )
        result = self.prober.probe(self._target(simulator.port), "openai_compatible")
        self.assertEqual(result.state, PROTOCOL_VERIFIED)
        self.assertEqual(result.checked_path, "/v1/models")
        self.assertEqual(result.model_ids, 2)
        self.assertTrue(result.verified)

    def test_it_falls_back_to_the_second_allowed_path(self) -> None:
        simulator = self._serve({"/models": (200, "application/json", MODEL_LIST_BODY)})
        result = self.prober.probe(self._target(simulator.port), "openai_compatible")
        self.assertEqual(result.state, PROTOCOL_VERIFIED)
        self.assertEqual(result.checked_path, "/models")

    def test_a_200_that_is_not_a_model_list_is_unverified(self) -> None:
        # 这就是要抓的那一类：有个东西在 200，但它不是模型服务。
        for body, content_type in (
            ("<html><body>index</body></html>", "text/html"),
            ('{"status":"ok"}', "application/json"),
            ("ok", "text/plain"),
        ):
            with self.subTest(content_type=content_type):
                simulator = self._serve(
                    {"/v1/models": (200, content_type, body)}
                )
                result = self.prober.probe(self._target(simulator.port), "openai_compatible")
                self.assertEqual(result.state, PROTOCOL_UNVERIFIED)
                self.assertEqual(result.reason, REASON_MODEL_LIST_MISSING)

    def test_a_health_endpoint_is_not_model_identity(self) -> None:
        # /healthz 回 200 只说明「有东西在应答」—— 那不足以叫「模型就绪」。
        simulator = self._serve(
            {"/healthz": (200, "application/json", '{"status":"ok"}')}
        )
        result = self.prober.probe(self._target(simulator.port), "openai_compatible")
        self.assertEqual(result.state, PROTOCOL_UNVERIFIED)
        self.assertEqual(result.reason, REASON_MODEL_LIST_MISSING)

    def test_a_closed_port_is_transport_unreachable(self) -> None:
        port = _free_port()  # 绑定后立刻释放 -> 确定没人监听
        result = self.prober.probe(self._target(port), "openai_compatible")
        self.assertEqual(result.state, PROTOCOL_UNVERIFIED)
        self.assertEqual(result.reason, REASON_TRANSPORT_UNREACHABLE)

    def test_only_allowlisted_paths_are_requested(self) -> None:
        # 引擎的 target path 是用户给的，可能很怪；协议验证**只**打白名单路径。
        simulator = self._serve(
            {"/v1/models": (200, "application/json", MODEL_LIST_BODY)}
        )
        target = self._target(simulator.port, path="/some/weird/user/path")
        self.prober.probe(target, "openai_compatible")
        self.assertEqual(simulator.seen, ["/v1/models"])
        for seen in simulator.seen:
            self.assertIn(seen, ALLOWED_MODEL_LIST_PATHS)

    def test_a_protocol_with_no_identity_is_unverified_not_verified(self) -> None:
        simulator = self._serve({"/v1/models": (200, "application/json", MODEL_LIST_BODY)})
        result = self.prober.probe(self._target(simulator.port), "http_json")
        self.assertEqual(result.state, PROTOCOL_UNVERIFIED)
        self.assertEqual(result.reason, REASON_NO_MODEL_IDENTITY_FOR_PROTOCOL)
        self.assertEqual(simulator.seen, [])  # 一个请求都不发

    def test_it_refuses_a_non_loopback_target(self) -> None:
        target = validate_engine_target_url("http://127.0.0.1:8000/v1")
        object.__setattr__(target, "host", "192.0.2.9")
        with self.assertRaises(InvalidRequest):
            self.prober.probe(target, "openai_compatible")

    def test_it_requires_a_normalized_target(self) -> None:
        with self.assertRaises(InvalidRequest):
            self.prober.probe("http://127.0.0.1:8000/v1", "openai_compatible")  # type: ignore[arg-type]


class ObservationShapeTest(unittest.TestCase):
    def test_an_unknown_observation_may_not_carry_a_status(self) -> None:
        with self.assertRaises(InvalidRequest):
            ProtocolObservation(
                state=PROTOCOL_UNKNOWN, reason=REASON_NOT_CONFIGURED, protocol="x", http_status=200
            )

    def test_model_ids_may_only_be_set_when_verified(self) -> None:
        with self.assertRaises(InvalidRequest):
            ProtocolObservation(
                state=PROTOCOL_UNVERIFIED,
                reason=REASON_MODEL_LIST_MISSING,
                protocol="x",
                model_ids=3,
            )

    def test_an_unknown_protocol_state_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            ProtocolObservation(state="probably", reason="x", protocol="y")


class ProtocolGatingTest(unittest.TestCase):
    """开关打开时，只有 **两根轴都过** 才算 ready。"""

    def _harness(self, *, protocol_prober, require_protocol: bool) -> Harness:
        return Harness(
            config=RegistryConfig(
                mode="synthetic", require_protocol_verification=require_protocol
            ),
            protocol_prober=protocol_prober,
        )

    def test_without_protocol_verification_a_200_is_still_ready(self) -> None:
        # 兼容缺省：传输可达即 ready，但协议状态如实是 unknown。
        h = Harness(protocol_prober=None)  # 没有协议来源
        h.make_ready_endpoint("alice")
        engine = h.request("GET", f"{PREFIX}/engines", principal="alice")[2]["data"]["items"][0]
        self.assertEqual(engine["verification"]["status"], "VERIFIED")
        self.assertEqual(engine["verification"]["protocol"]["state"], PROTOCOL_UNKNOWN)
        self.assertFalse(engine["verification"]["protocol_verified"])
        self.assertIn("protocol_unverified", engine["blockers"])

    def test_with_protocol_required_a_non_model_2xx_is_not_ready(self) -> None:
        # 上游只会回一个空洞的 200 —— 传输通了，协议没通。
        prober = StaticProtocolProber(
            {
                ("127.0.0.1", 8000): ProtocolObservation(
                    state=PROTOCOL_UNVERIFIED,
                    reason=REASON_MODEL_LIST_MISSING,
                    protocol="openai_compatible",
                )
            }
        )
        h = self._harness(protocol_prober=prober, require_protocol=True)
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "not-a-model", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["status"], "unavailable")
        self.assertFalse(payload["data"]["verified"])
        self.assertTrue(payload["data"]["verified_transport"])
        self.assertFalse(payload["data"]["verified_protocol"])
        self.assertIn("protocol_unverified", payload["data"]["verification"]["blockers"])

    def test_with_protocol_required_a_real_model_list_passes(self) -> None:
        prober = StaticProtocolProber(
            {
                ("127.0.0.1", 8000): ProtocolObservation(
                    state=PROTOCOL_VERIFIED,
                    reason=REASON_MODEL_LIST_VERIFIED,
                    protocol="openai_compatible",
                    checked_path="/v1/models",
                    http_status=200,
                    model_ids=2,
                )
            }
        )
        h = self._harness(protocol_prober=prober, require_protocol=True)
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "real", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(payload["data"]["status"], "verified")
        self.assertTrue(payload["data"]["verified_protocol"])

    def test_an_endpoint_of_a_protocol_unverified_engine_is_not_ready(self) -> None:
        prober = StaticProtocolProber(
            {
                ("127.0.0.1", 8000): ProtocolObservation(
                    state=PROTOCOL_UNVERIFIED,
                    reason=REASON_MODEL_LIST_MISSING,
                    protocol="openai_compatible",
                )
            }
        )
        h = self._harness(protocol_prober=prober, require_protocol=True)
        engine_id = h.make_engine("alice", "e")
        model_id = h.make_model("alice", "m")
        deployment_id = h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )[2]["data"]["id"]
        # deployment 也过不去：它的上一级（engine）没 verified。
        status, _h, payload = h.request(
            "POST", f"{PREFIX}/deployments/{deployment_id}/verify", principal="alice"
        )
        self.assertEqual(status, 409, payload)

    def _degraded_protocol_harness(self) -> tuple[Harness, str]:
        """把链路建到 ready（那一刻协议是成立的），然后把协议轴打成未验证。

        这是 ``ProtocolUnverified`` 这道门**唯一**能真实到达的路径：如果协议一开始
        就不成立，engine 压根不会 verified，链在更早的一级（NotVerified）就断了。
        真实世界的对应场景是「上游被换成了别的东西，但 Endpoint 那一行还是 ready」。
        """
        prober = StaticProtocolProber(
            {
                ("127.0.0.1", 8000): ProtocolObservation(
                    state=PROTOCOL_VERIFIED,
                    reason=REASON_MODEL_LIST_VERIFIED,
                    protocol="openai_compatible",
                    checked_path="/v1/models",
                    http_status=200,
                    model_ids=1,
                )
            }
        )
        h = self._harness(protocol_prober=prober, require_protocol=True)
        endpoint_id = h.make_ready_endpoint("alice")
        engine_id = h.store.list_engines()[0].id
        # 上游变了：协议轴不再成立，重新验证一次把它打成 unavailable。
        prober.set(
            "127.0.0.1",
            8000,
            ProtocolObservation(
                state=PROTOCOL_UNVERIFIED,
                reason=REASON_MODEL_LIST_MISSING,
                protocol="openai_compatible",
            ),
        )
        h.request("POST", f"{PREFIX}/engines/{engine_id}/verify", principal="alice")
        return h, endpoint_id

    def test_sharing_is_refused_once_the_protocol_state_degrades(self) -> None:
        h, endpoint_id = self._degraded_protocol_harness()
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.protocol_unverified")

    def test_invoking_is_refused_once_the_protocol_state_degrades(self) -> None:
        h, endpoint_id = self._degraded_protocol_harness()
        h.gateway.calls.clear()
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/invoke",
            principal="alice",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.protocol_unverified")
        # 一个字节都没有转出去。
        self.assertEqual(h.gateway.calls, [])

    def test_the_gate_uses_the_actual_engine_state(self) -> None:
        prober = StaticProtocolProber({})  # 一切目标都「没有结论」
        h = self._harness(protocol_prober=prober, require_protocol=True)
        # 手工把链路建成 ready（绕过 engine 的协议判定，用 store 直接改写）。
        engine_id = h.make_engine("alice", "e")
        model_id = h.make_model("alice", "m")
        deployment_id = h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "d", "engine_id": engine_id, "model_id": model_id},
        )[2]["data"]["id"]
        # 引擎在 require_protocol 下不会是 verified，所以 verify 会被上一级挡住。
        status, _h, payload = h.request(
            "POST", f"{PREFIX}/deployments/{deployment_id}/verify", principal="alice"
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_verified")

    def test_the_endpoint_dto_exposes_the_backend_axes(self) -> None:
        h = Harness()
        endpoint_id = h.make_ready_endpoint("alice")
        data = h.request(
            "GET", f"{PREFIX}/endpoints/{endpoint_id}", principal="alice"
        )[2]["data"]
        self.assertTrue(data["backend"]["transport_verified"])
        self.assertTrue(data["backend"]["protocol_verified"])
        self.assertEqual(data["backend"]["deployment_id"], data["deployment_id"])
        self.assertIn("ownership_policy", data["policy"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
