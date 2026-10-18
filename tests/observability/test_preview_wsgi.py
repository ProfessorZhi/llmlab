"""预览 adapter 的 Mock E2E：用真实 WSGI 调用（不启 socket）跑通四条路由。

预览本身不是交付物，但它是 UI 唯一能对着跑的东西 —— 所以它也必须守同样的规则：
身份不来自 URL、数据全合成、拒绝要有稳定的状态码。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dev.observability_preview import (  # noqa: E402
    DEMO_PRINCIPALS,
    FORBIDDEN_QUERY_PARAMS,
    LOOPBACK_HOSTS,
    ROUTES,
    ObservabilityPreviewApp,
    build_app,
    demo_directory,
    demo_registry,
)

from backend.observability import (  # noqa: E402
    KIND_ENGINE_STATUS,
    KIND_TIMELINE,
    KIND_USAGE_ADMIN,
    KIND_USAGE_SELF,
    QUALITY_UNKNOWN,
    validate_engine_status_dto,
    validate_timeline_dto,
    validate_usage_dto,
)


def call(app, path, query="", method="GET"):
    """在进程内调用 WSGI 应用，返回 ``(status, headers, payload)``。"""
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "8712",
        "wsgi.url_scheme": "http",
        "wsgi.input": None,
    }
    body = b"".join(app(environ, start_response))
    status_code = int(captured["status"].split(" ", 1)[0])
    payload = json.loads(body.decode("utf-8")) if body else None
    return status_code, captured["headers"], payload


class RoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("alice")

    def test_every_route_returns_a_valid_dto_for_an_admin(self) -> None:
        """管理主体是唯一能看到全部四条路由的演示身份。"""
        expected = {
            "/observability/usage/self": (KIND_USAGE_SELF, validate_usage_dto),
            "/observability/usage/admin": (KIND_USAGE_ADMIN, validate_usage_dto),
            "/observability/engines": (KIND_ENGINE_STATUS, validate_engine_status_dto),
            "/observability/timeline": (KIND_TIMELINE, validate_timeline_dto),
        }
        self.assertEqual(set(ROUTES), set(expected))
        admin_app = build_app("admin")
        for path, (kind, validator) in expected.items():
            with self.subTest(path=path):
                status, headers, payload = call(admin_app, path)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["kind"], kind)
                validator(payload)

    def test_the_three_non_admin_routes_work_for_a_plain_user(self) -> None:
        for path in (
            "/observability/usage/self",
            "/observability/engines",
            "/observability/timeline",
        ):
            with self.subTest(path=path):
                status, _, payload = call(self.app, path)
                self.assertEqual(status, 200, payload)

    def test_the_admin_route_requires_a_management_scope(self) -> None:
        """alice 没有管理 scope —— 与模块层一致地 403。"""
        status, _, payload = call(build_app("alice"), "/observability/usage/admin")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["contract_code"], "observability.access_denied")

    def test_the_admin_route_works_for_an_admin_principal(self) -> None:
        status, _, payload = call(build_app("admin"), "/observability/usage/admin")
        self.assertEqual(status, 200)
        self.assertTrue(payload["scope"]["admin"])

    def test_unknown_route_is_404_and_lists_the_routes(self) -> None:
        status, _, payload = call(self.app, "/observability/nope")
        self.assertEqual(status, 404)
        self.assertEqual(sorted(payload["routes"]), sorted(ROUTES))

    def test_non_get_methods_are_rejected(self) -> None:
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                status, _, payload = call(self.app, "/observability/usage/self", method=method)
                self.assertEqual(status, 405)
                self.assertIn("read-only", payload["error"]["detail"])

    def test_healthz_reports_synthetic_only(self) -> None:
        status, headers, payload = call(self.app, "/observability/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["synthetic"])
        self.assertEqual(headers["X-Preview-Only"], "synthetic-loopback")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_index_lists_the_query_parameters(self) -> None:
        status, _, payload = call(self.app, "/observability/")
        self.assertEqual(status, 200)
        self.assertIn("requested_uid", payload["query_parameters"])
        self.assertIn("promql", payload["refused_query_parameters"])


class IdentityComesFromTheSessionTest(unittest.TestCase):
    """预览里也不养成「从 URL 读身份」的习惯。"""

    def setUp(self) -> None:
        self.app = build_app("alice")

    def test_identity_query_parameters_are_refused(self) -> None:
        for name in ("uid", "principal", "role", "scopes", "token", "key"):
            with self.subTest(param=name):
                status, _, payload = call(
                    self.app, "/observability/usage/self", f"{name}=u-2001"
                )
                self.assertEqual(status, 403, payload)
                self.assertIn(name, payload["error"]["detail"])

    def test_arbitrary_query_parameters_are_refused(self) -> None:
        for name in ("promql", "query", "url", "path"):
            with self.subTest(param=name):
                status, _, _ = call(
                    self.app, "/observability/usage/self", f"{name}=evil"
                )
                self.assertEqual(status, 403)

    def test_the_refused_list_covers_the_dangerous_parameters(self) -> None:
        for name in ("uid", "principal", "role", "scope", "token", "promql", "url"):
            with self.subTest(param=name):
                self.assertIn(name, FORBIDDEN_QUERY_PARAMS)

    def test_requested_uid_is_allowed_for_an_admin_but_denied_for_a_user(self) -> None:
        status, _, payload = call(
            build_app("admin"), "/observability/usage/admin", "requested_uid=u-2001"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["scope"]["viewing_uid"], "u-2001")

        status, _, payload = call(
            self.app, "/observability/usage/self", "requested_uid=u-2001"
        )
        self.assertEqual(status, 403)

    def test_the_principal_is_fixed_at_construction(self) -> None:
        status, _, payload = call(self.app, "/observability/usage/self")
        self.assertEqual(payload["scope"]["uid"], DEMO_PRINCIPALS["alice"].uid)

    def test_each_demo_principal_gets_its_own_view(self) -> None:
        alice = call(build_app("alice"), "/observability/usage/self")[2]
        carol = call(build_app("carol"), "/observability/usage/self")[2]
        self.assertEqual(alice["scope"]["uid"], "u-1001")
        self.assertEqual(carol["scope"]["uid"], "u-2001")
        self.assertNotEqual(alice["scope"]["endpoints"], carol["scope"]["endpoints"])

    def test_an_unknown_demo_principal_fails_loudly(self) -> None:
        with self.assertRaises(SystemExit):
            build_app("nobody")

    def test_a_non_principal_is_rejected(self) -> None:
        from backend.observability import InvalidRequest

        with self.assertRaises(InvalidRequest):
            ObservabilityPreviewApp("alice")


class QueryHandlingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("alice")

    def test_window_parameters_are_honoured(self) -> None:
        now = datetime.now(timezone.utc)
        window_to = (now + timedelta(minutes=1)).replace(microsecond=0)
        window_from = window_to - timedelta(hours=3)
        query = (
            f"from={window_from.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            f"&to={window_to.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        status, _, payload = call(self.app, "/observability/usage/self", query)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["window"]["from"], window_from.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(payload["window"]["hours"], 3)

    def test_a_naive_timestamp_is_rejected(self) -> None:
        status, _, payload = call(
            self.app, "/observability/usage/self", "from=2026-10-15T08:00:00"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["contract_code"], "observability.invalid_request")

    def test_an_unbounded_window_is_rejected(self) -> None:
        status, _, payload = call(
            self.app,
            "/observability/usage/self",
            "from=2000-01-01T00:00:00Z&to=2026-10-15T12:00:00Z",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["contract_code"], "observability.invalid_window")

    def test_an_unsupported_bucket_is_rejected(self) -> None:
        status, _, payload = call(self.app, "/observability/usage/self", "bucket=1w")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["contract_code"], "observability.invalid_bucket")

    def test_paging_is_honoured_and_capped(self) -> None:
        status, _, payload = call(self.app, "/observability/usage/self", "limit=1&offset=0")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["page"]["limit"], 1)

        status, _, payload = call(self.app, "/observability/usage/self", "limit=9999")
        self.assertEqual(status, 400)

    def test_non_integer_paging_is_rejected(self) -> None:
        status, _, payload = call(self.app, "/observability/usage/self", "limit=many")
        self.assertEqual(status, 400)

    def test_an_unauthorised_endpoint_filter_is_rejected(self) -> None:
        status, _, payload = call(
            self.app, "/observability/usage/self", "requested_endpoint=model-shared-b"
        )
        self.assertEqual(status, 403)


class SyntheticOnlyTest(unittest.TestCase):
    def test_every_response_declares_itself_synthetic(self) -> None:
        for name in sorted(DEMO_PRINCIPALS):
            app = build_app(name)
            for path in sorted(ROUTES):
                with self.subTest(principal=name, path=path):
                    status, _, payload = call(app, path)
                    if status != 200:
                        continue
                    self.assertIs(payload["synthetic"], True)
                    self.assertEqual(payload["source"], "synthetic-preview")
                    self.assertTrue(
                        any("SYNTHETIC" in hint for hint in payload["display_hints"])
                    )

    def test_no_demo_response_mentions_a_real_identity(self) -> None:
        """演示数据只用虚构标识符 —— 真实名册（carol / alice / dave / bob）不得出现。"""
        blob = json.dumps(call(build_app("admin"), "/observability/usage/admin")[2],
                          sort_keys=True)
        for real in ("carol", "alice", "dave", "bob"):
            with self.subTest(token=real):
                self.assertNotIn(real, blob)

    def test_the_demo_directory_binds_only_synthetic_keys(self) -> None:
        directory = demo_directory()
        self.assertEqual(len(directory), 5)
        for uid in directory.uids():
            with self.subTest(uid=uid):
                self.assertTrue(uid.startswith("u-"))

    def test_alice_cannot_see_carols_private_engine_through_the_preview(self) -> None:
        payload = call(build_app("alice"), "/observability/engines")[2]
        self.assertEqual(
            {engine["endpoint_id"] for engine in payload["engines"]}, {"model-shared-a"}
        )
        self.assertNotIn("model-private-c", json.dumps(payload, sort_keys=True))

    def test_carol_can_see_her_own_private_engine(self) -> None:
        payload = call(build_app("carol"), "/observability/engines")[2]
        self.assertIn(
            "model-private-c", {engine["endpoint_id"] for engine in payload["engines"]}
        )

    def test_the_preview_exposes_an_unknown_engine_on_purpose(self) -> None:
        """开发期的 UI 必须见得到 UNKNOWN —— 否则它会以为「没数据」等于「一切正常」。"""
        payload = call(build_app("admin"), "/observability/engines")[2]
        self.assertGreaterEqual(payload["counts"]["unknown"], 1)

    def test_an_empty_window_reports_unknown_not_zero(self) -> None:
        payload = call(
            build_app("alice"),
            "/observability/usage/self",
            "from=2020-01-01T00:00:00Z&to=2020-01-01T01:00:00Z",
        )[2]
        self.assertEqual(payload["quality"]["calls"], QUALITY_UNKNOWN)
        self.assertIsNone(payload["metrics"]["principal.requests"]["value"])


class BindPolicyTest(unittest.TestCase):
    def test_loopback_hosts_are_the_declared_set(self) -> None:
        self.assertEqual(set(LOOPBACK_HOSTS), {"127.0.0.1", "::1", "localhost"})

    def test_a_non_loopback_bind_is_refused(self) -> None:
        from dev.observability_preview import main

        for host in ("0.0.0.0:8712", "10.0.0.5:8712", "example.com:8712"):
            with self.subTest(bind=host):
                with self.assertRaises(SystemExit):
                    main(["--bind", host, "--as", "alice"])

    def test_an_unknown_principal_is_refused(self) -> None:
        from dev.observability_preview import main

        with self.assertRaises(SystemExit):
            main(["--as", "nobody"])


class DemoDataTest(unittest.TestCase):
    def test_demo_rows_land_inside_the_default_window(self) -> None:
        """预览任何时候打开都该有数据落在默认窗口里。"""
        payload = call(build_app("admin"), "/observability/usage/admin")[2]
        self.assertGreater(payload["counters"]["calls"], 0)

    def test_the_demo_source_file_marks_every_row_synthetic(self) -> None:
        from dev.observability_preview import demo_rows

        text = demo_rows(datetime.now(timezone.utc))
        self.assertIn("SYNTHETIC-PREVIEW", text)
        for line in text.splitlines():
            if line.startswith("{"):
                self.assertIn("SYNTHETIC-PREVIEW", line)

    def test_the_demo_feed_contains_the_awkward_cases(self) -> None:
        from dev.observability_preview import demo_rows

        text = demo_rows(datetime.now(timezone.utc))
        self.assertIn("x_principal", text)          # 自报身份
        self.assertIn("prompt", text)               # 正文类字段
        self.assertIn("key-retired", text)          # 已吊销
        self.assertIn("unknown-key-demo", text)     # 未注册
        self.assertIn('{"ts":"broken"', text)       # 坏行

    def test_the_demo_registry_is_small_and_explicit(self) -> None:
        self.assertEqual(len(demo_registry()), 3)


if __name__ == "__main__":
    unittest.main()
