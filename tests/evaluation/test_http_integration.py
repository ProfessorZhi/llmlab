"""回环 HTTP 集成：走真实 socket、真实 WSGI、真实 JSON。

前面的模块大多在**进程内**驱动 WSGI（快，且能精确控制 environ）。这一组刻意走一遍
真实的 TCP 回环，因为它能发现进程内驱动发现不了的东西：状态行与原因短语、header 的
真实拼写、``Content-Length`` 与实际字节数是否一致、以及 ``Host`` 校验在真实请求上
到底怎么表现。

它**不**联网：``wsgiref`` 只绑 127.0.0.1 的一个临时端口，测试结束即关闭。
"""

from __future__ import annotations

import json
import unittest

from backend.evaluation.api import MAX_BODY_BYTES
from dev.evaluation_preview import (
    PreviewApplication,
    build_service,
    host_authorities,
    parse_host_authority,
)
from tests.evaluation.support import (
    DEP_VINCE_PRIVATE,
    Harness,
    QuietHandler,
    build_harness,
    close_all,
    http_request,
    serve,
)

_LIVE: list[Harness] = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


class RealSocketContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h = _LIVE[0]
        cls._ctx = serve(cls.h)
        cls.host, cls.port = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._ctx.__exit__(None, None, None)

    def test_health_needs_no_credential(self) -> None:
        status, headers, body = http_request(self.host, self.port, "GET", "/api/evaluation/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertTrue(body["data"]["admission"]["allowed"] is False)

    def test_every_response_is_uncacheable_and_not_sniffed(self) -> None:
        """响应里有授权快照与报告引用，中间层不该缓存它。"""
        _s, headers, _b = http_request(
            self.host, self.port, "GET", "/api/evaluation/jobs",
            token=self.h.token_for("alice"),
        )
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_the_full_submit_read_cancel_flow_over_tcp(self) -> None:
        token = self.h.token_for("alice")

        status, _headers, body = http_request(
            self.host, self.port, "POST", "/api/evaluation/jobs", token=token,
            payload={"deployment_id": DEP_VINCE_PRIVATE, "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke", "params": {"limit": 2},
                     "idempotency_key": "tcp-flow-1"},
        )
        self.assertEqual(status, 200, msg=body)
        job_id = body["data"]["job_id"]
        self.assertTrue(body["synthetic"])
        self.assertEqual(body["data"]["dispatch"]["state"], "refused")

        status, _headers, body = http_request(
            self.host, self.port, "GET", f"/api/evaluation/jobs/{job_id}", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["job_id"], job_id)

        status, _headers, body = http_request(
            self.host, self.port, "POST", f"/api/evaluation/jobs/{job_id}/mock-run", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "completed")
        self.assertTrue(body["data"]["synthetic"])

        status, _headers, body = http_request(
            self.host, self.port, "POST", f"/api/evaluation/jobs/{job_id}/cancel", token=token
        )
        self.assertEqual(status, 409)  # 已完成的任务不能取消
        self.assertEqual(body["error"]["contract_code"], "evaluation.conflict")

    def test_a_rejected_identity_header_still_reaches_the_transport(self) -> None:
        """``X-Role`` 是自定义头，真实请求里必须同样被拒。"""
        status, _headers, body = http_request(
            self.host, self.port, "GET", "/api/evaluation/jobs",
            token=self.h.token_for("alice"),
            headers=(("X-Role", "platform_admin"),),
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["contract_code"], "evaluation.identity_assertion_rejected")

    def test_the_status_codes_are_the_documented_ones(self) -> None:
        cases = [
            ("GET", "/api/evaluation/jobs", None, None, 401),
            ("GET", "/api/evaluation/jobs", "bogus-token", None, 401),
            ("GET", "/api/evaluation/jobs/evj-000000000000000000000000", "alice", None, 404),
            ("POST", "/api/evaluation/jobs", "alice", {"eval_type": "nope"}, 400),
            ("GET", "/api/evaluation/nope", "alice", None, 404),
        ]
        for method, path, principal, payload, expected in cases:
            with self.subTest(method=method, path=path):
                token = self.h.token_for(principal) if principal in ("alice",) else principal
                status, _headers, _body = http_request(
                    self.host, self.port, method, path, token=token, payload=payload
                )
                self.assertEqual(status, expected)

    def test_a_wrong_method_is_405_with_an_allow_header(self) -> None:
        status, headers, _body = http_request(
            self.host, self.port, "DELETE", "/api/evaluation/jobs",
            token=self.h.token_for("alice"),
        )
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET, POST")

    def test_a_malformed_json_body_is_400(self) -> None:
        import http.client

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(
                "POST", "/api/evaluation/jobs", body=b"{not json",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.h.token_for('alice')}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertEqual(payload["error"]["contract_code"], "evaluation.invalid_request")
        finally:
            connection.close()

    def test_a_non_object_json_body_is_400(self) -> None:
        import http.client

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(
                "POST", "/api/evaluation/jobs", body=b"[1, 2, 3]",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.h.token_for('alice')}"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()

    def test_an_oversized_body_is_refused_before_it_is_read(self) -> None:
        import http.client

        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(
                "POST", "/api/evaluation/jobs",
                body=b"x" * (MAX_BODY_BYTES + 1),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.h.token_for('alice')}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertIn("exceeds", payload["error"]["message"])
        finally:
            connection.close()

    def test_the_500_contract_code_is_reachable_for_internal_errors(self) -> None:
        """内部契约违反走 500：调用方没做错事，不该被告知"你的请求有错"。"""
        from unittest import mock

        from backend.evaluation.errors import InternalError
        from backend.evaluation.service import EvaluationService

        with mock.patch.object(
            EvaluationService,
            "capabilities",
            side_effect=InternalError("synthetic internal failure"),
        ):
            status, _headers, body = http_request(
                self.host, self.port, "GET", "/api/evaluation/health"
            )
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["contract_code"], InternalError.contract_code)
        self.assertIsNone(body["error"]["message"])

    def test_the_openapi_document_is_served_over_tcp(self) -> None:
        status, _headers, body = http_request(
            self.host, self.port, "GET", "/api/evaluation/openapi.json"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["openapi"], "3.1.0")

    def test_credentials_never_appear_in_a_response(self) -> None:
        token = self.h.token_for("alice")
        for path in ("/api/evaluation/jobs", "/api/evaluation/monitoring",
                     "/api/evaluation/datasets", "/api/evaluation/modules"):
            with self.subTest(path=path):
                _s, _headers, body = http_request(
                    self.host, self.port, "GET", path, token=token
                )
                self.assertNotIn(token, json.dumps(body, sort_keys=True))


class PreviewHostGuardTests(unittest.TestCase):
    """预览容器只回答它自己的 loopback authority。走真实请求。"""

    @classmethod
    def setUpClass(cls) -> None:
        import threading
        import wsgiref.simple_server

        assembly = build_service()
        cls.app = PreviewApplication(assembly)
        cls.server = wsgiref.simple_server.make_server(
            "127.0.0.1", 0, cls.app, handler_class=QuietHandler
        )
        cls.port = int(cls.server.server_address[1])
        cls.app.bind_port(cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.assembly = assembly

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_the_preview_answers_its_own_authority(self) -> None:
        for authority in host_authorities(self.port):
            if authority.startswith("[::1]"):
                continue  # 服务只绑 IPv4 回环
            with self.subTest(authority=authority):
                status, _headers, body = http_request(
                    "127.0.0.1", self.port, "GET", "/api/evaluation/health",
                    headers=(("Host", authority),),
                )
                self.assertEqual(status, 200, msg=body)

    def test_a_foreign_host_is_refused(self) -> None:
        for authority in (
            "evil.com",
            f"evil.com:{self.port}",
            f"127.0.0.1.nip.io:{self.port}",
            f"127.0.0.1:{self.port + 1}",
            f"127.0.0.1:{self.port},evil.com",
        ):
            with self.subTest(authority=authority):
                status, _headers, body = http_request(
                    "127.0.0.1", self.port, "GET", "/api/evaluation/health",
                    headers=(("Host", authority),),
                )
                self.assertEqual(status, 400, msg=body)
                self.assertEqual(body["error"]["contract_code"], "preview.host_rejected")

    def test_the_dev_only_route_exists_in_the_preview(self) -> None:
        """生产默认不注册它，但预览注册——这正是"开发入口，不是部署"。"""
        status, _headers, body = http_request(
            "127.0.0.1", self.port, "POST", "/api/evaluation/jobs",
            token="synthetic-alice",
            payload={"deployment_id": "dep-strata-alice", "eval_type": "correctness",
                     "dataset_slug": "synthetic-qa-smoke"},
        )
        self.assertEqual(status, 200, msg=body)
        job_id = body["data"]["job_id"]
        status, _headers, body = http_request(
            "127.0.0.1", self.port, "POST", f"/api/evaluation/jobs/{job_id}/mock-run",
            token="synthetic-alice",
        )
        self.assertEqual(status, 200, msg=body)
        self.assertTrue(body["data"]["synthetic"])
        self.assertEqual(body["data"]["executed_by"], "mock")

    def test_the_preview_reports_itself_synthetic(self) -> None:
        _s, _h, body = http_request(
            "127.0.0.1", self.port, "GET", "/api/evaluation/admission",
            token="synthetic-alice",
        )
        self.assertTrue(body["synthetic"])
        self.assertFalse(body["data"]["can_dispatch"])
        self.assertFalse(body["data"]["runner_can_run_real"])


class HostAuthorityParsingTests(unittest.TestCase):
    def test_only_the_exact_loopback_authority_passes(self) -> None:
        self.assertEqual(parse_host_authority("127.0.0.1:8765", port=8765), "127.0.0.1")
        self.assertEqual(parse_host_authority("localhost:8765", port=8765), "localhost")
        self.assertEqual(parse_host_authority("[::1]:8765", port=8765), "::1")
        self.assertEqual(parse_host_authority("LOCALHOST:8765", port=8765), "localhost")

    def test_a_host_without_a_port_is_accepted(self) -> None:
        """``Host`` 允许省略端口；省略时不做端口校验（调用方仍只绑环回）。"""
        self.assertEqual(parse_host_authority("127.0.0.1", port=8765), "127.0.0.1")

    def test_partial_matches_are_not_partial_matches(self) -> None:
        for value in (
            "127.0.0.1:8765.evil.com",
            "127.0.0.1:8765/evil",
            "127.0.0.1:8765,evil.com",
            "127.0.0.1:8765 ",
            "evil.com",
            "0.0.0.0:8765",
            "127.0.0.1:9999",
            "",
            None,
            4,
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_host_authority(value, port=8765))

    def test_an_out_of_range_port_is_refused(self) -> None:
        for value in ("127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:99999"):
            with self.subTest(value=value):
                self.assertIsNone(parse_host_authority(value, port=0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
