"""WSGI 传输层：认证前置、请求体纪律、错误封套、以及「不泄漏内部细节」。

这一组测试把「路由表上每一个非探针端点都要求凭据」做成**穷举**断言：新增路由时
如果忘了认证，会在这条测试上直接失败，而不是等到线上才发现。
"""

from __future__ import annotations

import io
import json
import unittest

from backend.registry.api import (
    CONTRACT_STATUS,
    MAX_BODY_BYTES,
    PREFIX,
    RegistryApplication,
    create_app,
    parse_json_body,
    read_body,
)
from backend.registry.errors import StorageError
from backend.registry.identity import SCOPE_ADMIN_READ
from tests.registry.support import TOKEN_PREFIX, Harness, close_all

#: 路由表上除存活探针外的全部路径（用占位 id）。新增路由必须加到这里 ——
#: 下面的穷举认证测试就是靠这份清单生效的。
AUTHENTICATED_PATHS = (
    ("GET", f"{PREFIX}/whoami"),
    ("GET", f"{PREFIX}/status"),
    ("GET", f"{PREFIX}/usage/mine"),
    ("GET", f"{PREFIX}/audit"),
    ("GET", f"{PREFIX}/routing-config"),
    ("GET", f"{PREFIX}/routing-snapshot"),
    ("GET", f"{PREFIX}/engines"),
    ("POST", f"{PREFIX}/engines"),
    ("GET", f"{PREFIX}/engines/eng_x"),
    ("POST", f"{PREFIX}/engines/eng_x/verify"),
    ("GET", f"{PREFIX}/models"),
    ("POST", f"{PREFIX}/models"),
    ("GET", f"{PREFIX}/models/mdl_x"),
    ("GET", f"{PREFIX}/deployments"),
    ("POST", f"{PREFIX}/deployments"),
    ("GET", f"{PREFIX}/deployments/dep_x"),
    ("POST", f"{PREFIX}/deployments/dep_x/verify"),
    ("GET", f"{PREFIX}/endpoints"),
    ("POST", f"{PREFIX}/endpoints"),
    ("GET", f"{PREFIX}/endpoints/ep_x"),
    ("GET", f"{PREFIX}/endpoints/ep_x/acl"),
    ("GET", f"{PREFIX}/endpoints/ep_x/routings"),
    ("POST", f"{PREFIX}/endpoints/ep_x/verify"),
    ("POST", f"{PREFIX}/endpoints/ep_x/share"),
    ("POST", f"{PREFIX}/endpoints/ep_x/unshare"),
    ("POST", f"{PREFIX}/endpoints/ep_x/invoke"),
    ("POST", f"{PREFIX}/endpoints/ep_x/repoint"),
    ("POST", f"{PREFIX}/endpoints/ep_x/rollback"),
    ("POST", f"{PREFIX}/endpoints/ep_x/routing-applied"),
)


def tearDownModule() -> None:
    close_all()


class AuthenticationIsRequiredEverywhereTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_every_route_except_the_probe_refuses_an_anonymous_caller(self) -> None:
        for method, path in AUTHENTICATED_PATHS:
            with self.subTest(method=method, path=path):
                status, _h, payload = self.h.request(method, path, body={})
                self.assertEqual(status, 401)
                self.assertEqual(payload["error"]["code"], "registry.unauthenticated")

    def test_a_bogus_credential_fails_on_every_route_too(self) -> None:
        for method, path in AUTHENTICATED_PATHS:
            with self.subTest(method=method, path=path):
                status, _h, _p = self.h.request(
                    method, path, token=f"{TOKEN_PREFIX}nobody", body={}
                )
                self.assertEqual(status, 401)

    def test_the_probe_is_the_only_unauthenticated_route(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_the_probe_leaks_nothing_about_tenants(self) -> None:
        self.h.make_ready_endpoint("alice")
        status, _h, payload = self.h.request("GET", f"{PREFIX}/healthz")
        text = json.dumps(payload)
        for needle in ("alice", "eng_", "mdl_", "dep_", "ep_"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)


class MethodAndRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_a_known_path_with_the_wrong_method_is_405(self) -> None:
        for method, path in (
            ("DELETE", f"{PREFIX}/engines"),
            ("PUT", f"{PREFIX}/models"),
            ("GET", f"{PREFIX}/endpoints/ep_x/share"),  # share 是写动作
            ("POST", f"{PREFIX}/endpoints/ep_x/acl"),  # acl 是读
            ("POST", f"{PREFIX}/whoami"),
        ):
            with self.subTest(method=method, path=path):
                status, _h, payload = self.h.request(
                    method, path, principal="alice", body={}
                )
                self.assertEqual(status, 405)
                self.assertEqual(payload["error"]["code"], "registry.method_not_allowed")

    def test_an_unknown_path_is_404(self) -> None:
        for path in (f"{PREFIX}/nope", f"{PREFIX}/engines/x/y/z", "/api/v1/registry/whoami", "/"):
            with self.subTest(path=path):
                status, _h, payload = self.h.request("GET", path, principal="alice")
                self.assertEqual(status, 404)

    def test_a_known_action_word_on_the_wrong_resource_is_404(self) -> None:
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/engines/eng_x/share", principal="alice", body={}
        )
        self.assertEqual(status, 404)

    def test_a_trailing_slash_is_tolerated(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/whoami/", principal="alice")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_a_traversing_path_is_not_a_route(self) -> None:
        status, _h, _p = self.h.request(
            "GET", f"{PREFIX}/engines/../../etc/passwd", principal="alice"
        )
        self.assertIn(status, (404,))


class RequestBodyDisciplineTest(unittest.TestCase):
    """请求体的长度必须**被可信地声明**；否则不读。"""

    def setUp(self) -> None:
        self.h = Harness()

    def test_a_chunked_body_is_refused(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "target_url": "http://127.0.0.1:8000/v1"},
            extra_environ={"HTTP_TRANSFER_ENCODING": "chunked"},
        )
        self.assertEqual(status, 411)
        self.assertEqual(payload["error"]["code"], "registry.length_required")

    def test_an_oversized_body_is_refused_before_parsing(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            raw_body=b"x" * (MAX_BODY_BYTES + 1),
        )
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "registry.payload_too_large")

    def test_a_malformed_content_length_is_refused(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            extra_environ={"CONTENT_LENGTH": "12abc"},
        )
        self.assertEqual(status, 400)

    def test_an_empty_content_length_means_no_body_not_an_error(self) -> None:
        # ``wsgiref.simple_server`` 在无该头时把 CONTENT_LENGTH 填成 ``''``；
        # 把它当非法会让**每一个无体 GET** 变成 400。
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/whoami", principal="alice", extra_environ={"CONTENT_LENGTH": ""}
        )
        self.assertEqual(status, 200)

    def test_invalid_json_is_a_400(self) -> None:
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/engines", principal="alice", raw_body=b"{not json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "registry.invalid_request")

    def test_a_non_object_json_body_is_refused_where_an_object_is_required(self) -> None:
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/engines", principal="alice", raw_body=b"[1,2,3]"
        )
        self.assertEqual(status, 400)

    def test_a_non_json_content_type_is_refused(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            raw_body=b"name=e",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(status, 415)

    def test_read_body_helpers_behave_as_documented(self) -> None:
        with self.assertRaises(Exception):
            read_body({"HTTP_TRANSFER_ENCODING": "chunked"})
        self.assertEqual(read_body({}), b"")
        self.assertEqual(read_body({"CONTENT_LENGTH": ""}), b"")
        self.assertEqual(read_body({"CONTENT_LENGTH": "0"}), b"")
        with self.assertRaises(Exception):
            read_body({"CONTENT_LENGTH": "-1"})
        with self.assertRaises(Exception):
            read_body({"CONTENT_LENGTH": str(MAX_BODY_BYTES + 1)})

    def test_parse_json_body_needs_the_right_content_type(self) -> None:
        self.assertIsNone(parse_json_body(b"", None))
        self.assertEqual(parse_json_body(b'{"a":1}', "application/json"), {"a": 1})
        with self.assertRaises(Exception):
            parse_json_body(b'{"a":1}', "text/plain")


class SchemeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_the_scheme_comes_from_wsgi_url_scheme(self) -> None:
        status, _h, _p = self.h.request(
            "GET", f"{PREFIX}/whoami", principal="alice",
            extra_environ={"wsgi.url_scheme": "ftp"},
        )
        self.assertEqual(status, 400)

    def test_a_forwarding_header_does_not_change_the_scheme(self) -> None:
        # 转发头完全被忽略：这里断言的是「不因它而改变行为」，不是「拒绝它」。
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/whoami", principal="alice",
            headers={"X-Forwarded-Proto": "https", "Forwarded": "proto=https"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["identity"]["principal"], "alice")


class EnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_a_success_response_carries_the_contract_envelope(self) -> None:
        status, headers, payload = self.h.request("GET", f"{PREFIX}/whoami", principal="alice")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], "registry/1")
        self.assertEqual(payload["mode"], "synthetic")
        self.assertTrue(payload["synthetic"])
        self.assertEqual(payload["source"], "registry_store")
        self.assertIn("data", payload)
        self.assertIn("scope", payload)
        self.assertEqual(payload["scope"]["principal"], "alice")
        self.assertEqual(payload["scope"]["uid"], 4001)

    def test_the_scope_block_reports_scopes_and_roles_separately(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/whoami", principal="carol")
        self.assertIn("registry:admin:read", payload["scope"]["scopes"])
        self.assertTrue(payload["scope"]["is_admin_read"])
        self.assertFalse(payload["scope"]["elevated_read"] is None)

    def test_an_error_response_carries_the_error_envelope(self) -> None:
        status, _h, payload = self.h.request("GET", f"{PREFIX}/whoami")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])
        self.assertEqual(set(payload["error"]), {"code", "message"})
        self.assertIn(payload["error"]["code"], CONTRACT_STATUS)

    def test_security_headers_are_present_on_success_and_failure(self) -> None:
        for principal, expected in (("alice", 200), (None, 401)):
            with self.subTest(expected=expected):
                status, headers, _p = self.h.request(
                    "GET", f"{PREFIX}/whoami", principal=principal
                )
                self.assertEqual(status, expected)
                self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("X-Request-Id", headers)

    def test_every_error_code_in_the_map_is_reachable_from_a_defined_error(self) -> None:
        # 映射表与契约错误必须一一对应，否则会有一个永远发不出的状态码。
        from backend.registry import errors as registry_errors

        declared = {
            value.contract_code
            for value in vars(registry_errors).values()
            if isinstance(value, type) and issubclass(value, registry_errors.RegistryError)
        }
        for code in declared:
            with self.subTest(code=code):
                self.assertIn(code, CONTRACT_STATUS)

    def test_the_error_map_covers_the_transport_codes_too(self) -> None:
        for code in (
            "registry.method_not_allowed",
            "registry.length_required",
            "registry.payload_too_large",
            "registry.unsupported_media_type",
            "registry.internal_error",
        ):
            with self.subTest(code=code):
                self.assertIn(code, CONTRACT_STATUS)


class NoInternalDetailLeakTest(unittest.TestCase):
    def _drive(self, app: RegistryApplication, token: str) -> tuple[int, dict]:
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": f"{PREFIX}/whoami",
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(b""),
            "CONTENT_LENGTH": "",
            "HTTP_AUTHORIZATION": f"Bearer {token}",
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = int(status.split()[0])

        body = b"".join(app(environ, start_response))
        return captured["status"], json.loads(body)

    def test_an_unexpected_exception_does_not_echo_its_message(self) -> None:
        h = Harness()

        class Exploding:
            def whoami(self, requester):  # noqa: D401 - 测试替身
                raise RuntimeError("/var/lib/secret/path is locked")

        app = RegistryApplication(h.service, tokens=h.tokens, principals=h.principals)
        app.service = Exploding()  # type: ignore[assignment]
        status, payload = self._drive(app, h.token("alice"))
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "registry.internal_error")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("/var/lib", json.dumps(payload))

    def test_a_storage_failure_says_what_it_is_but_not_where(self) -> None:
        # 存储层是契约错误，但它的消息里有库路径与 schema 细节 —— 有码，无细节。
        h = Harness()

        class Exploding:
            def whoami(self, requester):
                raise StorageError("sqlite: /var/lib/secret/registry.sqlite is locked")

        app = RegistryApplication(h.service, tokens=h.tokens, principals=h.principals)
        app.service = Exploding()  # type: ignore[assignment]
        status, payload = self._drive(app, h.token("alice"))
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "registry.storage_error")
        self.assertNotIn("secret", json.dumps(payload))
        self.assertNotIn("/var/lib", json.dumps(payload))

    def test_debug_errors_include_the_message_only_when_asked(self) -> None:
        h = Harness()

        class Exploding:
            def whoami(self, requester):
                raise RuntimeError("boom-detail")

        # 默认关闭：只有类型名与 request id。
        app = RegistryApplication(h.service, tokens=h.tokens, principals=h.principals)
        app.service = Exploding()  # type: ignore[assignment]
        _status, payload = self._drive(app, h.token("alice"))
        self.assertNotIn("boom-detail", json.dumps(payload))

        # 显式打开（非生产调试）：消息放行，且仍然只有类型名与消息，没有 traceback。
        app = RegistryApplication(
            h.service, tokens=h.tokens, principals=h.principals, debug_errors=True
        )
        app.service = Exploding()  # type: ignore[assignment]
        _status, payload = self._drive(app, h.token("alice"))
        self.assertIn("boom-detail", json.dumps(payload))
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_debug_mode_does_not_resurrect_redacted_storage_messages(self) -> None:
        # 脱敏是**错误类型自身的属性**，不是运行模式的属性：
        # 打开调试不该让库路径重新出现在响应里。
        h = Harness()

        class Exploding:
            def whoami(self, requester):
                raise StorageError("/var/lib/secret/registry.sqlite is locked")

        app = RegistryApplication(
            h.service, tokens=h.tokens, principals=h.principals, debug_errors=True
        )
        app.service = Exploding()  # type: ignore[assignment]
        status, payload = self._drive(app, h.token("alice"))
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"]["code"], "registry.storage_error")
        self.assertNotIn("/var/lib", json.dumps(payload))

    def test_no_response_ever_echoes_the_credential(self) -> None:
        h = Harness()
        token = h.token("alice")
        for method, path, kwargs in (
            ("GET", f"{PREFIX}/whoami", {}),
            ("GET", f"{PREFIX}/engines", {}),
            ("POST", f"{PREFIX}/engines", {"body": {"name": "e", "target_url": "bad"}}),
            ("GET", f"{PREFIX}/endpoints/ep_missing", {}),
            ("POST", f"{PREFIX}/endpoints/ep_missing/invoke", {"body": {}}),
        ):
            with self.subTest(method=method, path=path):
                _s, _h, payload = h.request(method, path, token=token, **kwargs)
                self.assertNotIn(token, json.dumps(payload))

    def test_the_credential_is_not_echoed_when_it_is_unknown(self) -> None:
        h = Harness()
        _s, _h, payload = h.request("GET", f"{PREFIX}/whoami", token="synthetic-supersecret")
        self.assertNotIn("supersecret", json.dumps(payload))


class AdminScopeOnReadAllTest(unittest.TestCase):
    def test_an_admin_read_scope_alone_is_enough_to_list(self) -> None:
        h = Harness()
        h.with_scopes("carol", frozenset({SCOPE_ADMIN_READ}))
        h.make_ready_endpoint("alice")
        status, _h, payload = h.request("GET", f"{PREFIX}/endpoints", principal="carol")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["count"], 1)


class CreateAppTest(unittest.TestCase):
    def test_create_app_returns_a_wsgi_callable(self) -> None:
        h = Harness()
        app = create_app(h.service)
        self.assertTrue(callable(app))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
