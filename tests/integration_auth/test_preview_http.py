"""HTTP-level integration tests for the real local preview server.

These drive a real ``wsgiref`` server on ``127.0.0.1`` over real sockets — the
same container the operator runs — so the cookie really travels back through a
client, the origin/CSRF guards fire as a browser would trigger them, and a
bodyless ``GET`` is proven not to hang.

Scope discipline (the preview is a development convenience, never a
deployment):

* loopback only, ephemeral port, shut down and removed after each test;
* no Feishu application, no external request, no database or source path ever
  reachable over HTTP;
* the only protected content served is the synthetic DEMO page.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from backend.auth import bootstrap, errors, store
from backend.auth.config import AuthConfig

import dev.preview_auth as preview

from tests.integration_auth import support


def _all_text(exc: BaseException) -> str:
    """The public message *and* the diagnostic detail, lower-cased.

    ``AuthError`` keeps the two apart on purpose: ``message`` is what a client
    would be shown, ``detail`` is operator-facing. At startup nothing reaches a
    client, so a test that wants the operator's guidance has to look at both.
    """
    return f"{exc} {getattr(exc, 'detail', '') or ''}".lower()


class StartupGuardTests(unittest.TestCase):
    """Every refusal that happens before a socket is opened."""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-preview-guard-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def app(self, **kwargs) -> preview.PreviewApplication:
        return preview.build_preview_application(
            self.root / "state", local_http_dev=True, **kwargs
        )

    def bootstrap_admin(self, app: preview.PreviewApplication) -> str:
        """Create the credential through the same seam the CLI uses."""
        password = support.random_password()
        bootstrap.initialize_admin_password(
            app.cfg, read_password=support.ScriptedReader(password, password)
        )
        return password

    def test_refuses_to_build_without_the_explicit_local_http_dev_flag(self):
        # 生产要求 HTTPS + Secure Cookie。没有显式声明本地 HTTP 开发模式时，
        # 预览必须**拒绝构建**，而不是悄悄用弱化过的 Cookie 跑起来。
        with self.assertRaises(errors.AuthError) as caught:
            preview.build_preview_application(self.root / "state", local_http_dev=False)
        self.assertIn("local-http-dev", _all_text(caught.exception))

    def test_state_dir_must_be_absolute(self):
        with self.assertRaises(errors.AuthError) as caught:
            preview.build_preview_application("relative/state", local_http_dev=True)
        self.assertIn("absolute", _all_text(caught.exception))

    def test_local_http_dev_is_the_only_way_cookie_secure_is_dropped(self):
        app = self.app()
        self.assertFalse(app.cfg.cookie_secure, "本地 HTTP 开发模式下才允许去掉 Secure")
        self.assertTrue(app.cfg.cookie_httponly)
        self.assertEqual(app.cfg.cookie_samesite, "Lax")

    def test_the_configuration_is_hermetic(self):
        # 预览的配置从**空环境**构建：操作者 shell 里的
        # LLM_LAB_FEISHU_APP_ID / LLM_LAB_ALLOW_INSECURE_REDIRECT_URI 之类
        # 绝不能影响它，也不该有任何真实凭据被顺带读进来。
        app = self.app()
        self.assertFalse(app.cfg.feishu_configured)
        self.assertEqual(app.cfg.trusted_origins, frozenset())
        self.assertFalse(app.cfg.allow_insecure_redirect_uri)

    def test_make_server_refuses_a_non_loopback_host(self):
        app = self.app()
        self.bootstrap_admin(app)
        for host in ("0.0.0.0", "::", "192.168.1.10", "portal.example.com"):
            with self.assertRaises(errors.AuthError) as caught:
                preview.make_server(app, host=host)
            self.assertIn("loopback", _all_text(caught.exception), host)

    def test_make_server_refuses_to_start_without_an_initialised_administrator(self):
        # 没有任何默认口令，也没有 CLI 参数可以预置口令：没初始化就不启动，
        # 并且明确告诉操作者该跑哪条交互式命令。
        app = self.app()
        with self.assertRaises(errors.AuthError) as caught:
            preview.make_server(app)
        text = _all_text(caught.exception)
        self.assertIn("bootstrap", text)
        self.assertIn("init", text)
        self.assertIn("--state-dir", text)

    def test_make_server_starts_once_the_administrator_exists(self):
        app = self.app()
        self.bootstrap_admin(app)
        server = preview.make_server(app, port=0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertGreater(int(server.server_address[1]), 0)
            self.assertEqual(app.port, int(server.server_address[1]))
        finally:
            server.server_close()


class PreviewTestCase(unittest.TestCase):
    """A server per test, in local HTTP dev mode by default."""

    mock_feishu = False

    def setUp(self) -> None:
        self.server = support.PreviewServer(mock_feishu=self.mock_feishu)
        self.addCleanup(self.server.close)
        self.client = self.server.client()
        self.origin = self.server.origin

    def admin_client(self) -> support.LoopbackClient:
        """A client already carrying a real, server-issued admin session."""
        client = self.server.client()
        reply = client.login(self.server.password)
        self.assertEqual(reply.status, 200, reply.body.decode("utf-8", "replace"))
        return client


# ===========================================================================
# 2. Static surface: allowlist, traversal, MIME
# ===========================================================================


class StaticSurfaceTests(PreviewTestCase):
    def test_login_page_and_its_three_assets_are_served(self):
        page = self.client.get("/login")
        self.assertEqual(page.status, 200)
        self.assertIn("text/html", page.header("content-type") or "")
        self.assertIn("登录 Dell LLM Lab", page.body.decode("utf-8"))
        self.assertEqual(page.header("cache-control"), "no-store")

        for path, kind in [
            ("/auth.css", "text/css"),
            ("/auth.js", "text/javascript"),
            ("/auth-core.js", "text/javascript"),
        ]:
            asset = self.client.get(path)
            self.assertEqual(asset.status, 200, path)
            self.assertIn(kind, asset.header("content-type") or "", path)
            self.assertEqual(asset.header("x-content-type-options"), "nosniff", path)

    def test_login_page_loads_only_same_origin_resources(self):
        body = self.client.get("/login").body.decode("utf-8")
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)

    def test_arbitrary_paths_are_never_read_from_disk(self):
        for path in [
            "/package.json",
            "/README.md",
            "/auth.sqlite3",
            "/admin_password.hash",
            "/backend/auth/routes.py",
            "/../../etc/passwd",
            "/..%2F..%2Fetc%2Fpasswd",
            "/%2e%2e/%2e%2e/README.md",
            "/auth-core.js.map",
            "/login/",
        ]:
            reply = self.client.get(path)
            self.assertEqual(reply.status, 404, f"{path} 必须 404，实际 {reply.status}")
            self.assertNotIn(b"def ", reply.body, path)

    def test_writes_are_refused_on_the_static_surface(self):
        reply = self.client.request("POST", "/login", body=b"", content_type="text/plain")
        self.assertEqual(reply.status, 405)


# ===========================================================================
# 3. Host / cookie hardening
# ===========================================================================


class RequestHardeningTests(PreviewTestCase):
    def test_a_non_loopback_host_header_is_refused(self):
        client = self.server.client()
        client.host_header = "portal.example.com"
        reply = client.get("/auth/csrf")
        self.assertEqual(reply.status, 400)
        self.assertNotIn(b"csrf_token", reply.body)

    def test_loopback_spellings_are_accepted(self):
        for host in ("127.0.0.1", "[::1]", "localhost"):
            client = self.server.client()
            client.host_header = f"{host}:{self.server.port}"
            reply = client.get("/auth/csrf")
            self.assertEqual(reply.status, 200, host)

    def test_a_host_that_only_starts_with_a_loopback_address_is_refused(self):
        # Issue #8 的原始复现：旧守卫在最后一个冒号处切开 Host，只看前半段，
        # 于是 ``127.0.0.1:<port>.evil.com`` 被当成 ``127.0.0.1`` 放行并服务。
        # 完整的权威字符串必须逐字匹配，没有任何"取前缀"的余地。
        client = self.server.client()
        client.host_header = f"127.0.0.1:{self.server.port}.evil.com"
        reply = client.get("/auth/csrf")
        self.assertEqual(reply.status, 400, "环回地址前缀不是环回权威")
        self.assertNotIn(b"csrf_token", reply.body)

    def test_a_port_that_is_not_the_listening_port_is_refused(self):
        # 端口不是装饰：声明了就必须等于真正在监听的那个。
        client = self.server.client()
        client.host_header = f"127.0.0.1:{self.server.port % 65535 + 1}"
        self.assertEqual(client.get("/auth/csrf").status, 400)

    def test_a_repeated_host_header_is_refused_before_wsgi_flattens_it(self):
        # 两个 Host 头在任何合法请求里都不该出现，而 wsgiref 会把它们逗号黏成
        # 一个 WSGI 值，应用层无从分辨——因此必须在 request handler 层就拒绝。
        raw = (
            "GET /auth/csrf HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.port}\r\n"
            f"Host: 127.0.0.1:{self.server.port}.evil.com\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        payload = self.client.raw_exchange(raw, timeout=3.0)
        self.assertTrue(payload, "服务端必须应答，而不是挂起")
        self.assertIn(b"400", payload.split(b"\r\n", 1)[0])

    def test_a_request_without_any_host_header_is_refused(self):
        raw = b"GET /auth/csrf HTTP/1.1\r\nConnection: close\r\n\r\n"
        payload = self.client.raw_exchange(raw, timeout=3.0)
        self.assertTrue(payload, "服务端必须应答，而不是挂起")
        self.assertIn(b"400", payload.split(b"\r\n", 1)[0])

    def test_duplicate_cookie_names_are_refused(self):
        # 同一个名字出现两次时，不同解析器会取到不同的值；与其挑一个，不如拒绝。
        reply = self.client.get(
            "/auth/me",
            headers={"Cookie": "llm_lab_session=aaa; llm_lab_session=bbb"},
        )
        self.assertEqual(reply.status, 400)
        self.assertNotIn(b"authenticated", reply.body)

    def test_a_duplicated_csrf_header_cannot_authenticate(self):
        # 重复的同名请求头到底层会变成逗号拼接的一个值，不再是任何一个合法 token。
        # 断言它按"CSRF 不通过"处理，而不是被当成两个候选中的一个。
        reply = self.client.request(
            "POST",
            "/auth/local/login",
            body=json.dumps({"username": "admin", "password": self.server.password}).encode(),
            content_type="application/json",
            headers={"Origin": self.origin, "X-CSRF-Token": "one,two"},
        )
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), "csrf_rejected")


# ===========================================================================
# 4. Anonymous access to the protected pages
# ===========================================================================


class AnonymousAccessTests(PreviewTestCase):
    def test_anonymous_root_and_atlas_are_redirected_to_login(self):
        for path in ("/", "/atlas"):
            reply = self.client.get(path)
            self.assertEqual(reply.status, 302, path)
            location = reply.header("location") or ""
            self.assertTrue(location.startswith("/login"), f"{path} → {location}")
            self.assertIn("next=", location)
            self.assertNotIn(preview.DEMO_BANNER_ID.encode(), reply.body, f"{path} 泄漏了 DEMO")

    def test_a_forged_session_cookie_is_still_refused(self):
        client = self.server.client()
        client.cookies[support.SESSION_COOKIE] = "synthetic-not-a-real-session"
        for path in ("/", "/atlas"):
            reply = client.get(path)
            self.assertEqual(reply.status, 302, path)
            self.assertNotIn(preview.DEMO_BANNER_ID.encode(), reply.body)

    def test_the_redirect_carries_the_requested_path(self):
        location = self.client.get("/atlas").header("location") or ""
        self.assertIn("next=%2Fatlas", location)

    def test_a_revoked_session_is_refused(self):
        client = self.admin_client()
        self.assertEqual(client.get("/atlas").status, 200)

        self.assertEqual(client.write("POST", "/auth/logout", {}).status, 200)
        reply = client.get("/atlas")
        self.assertEqual(reply.status, 302, "退出后不得再读到 DEMO")
        self.assertNotIn(preview.DEMO_BANNER_ID.encode(), reply.body)

    def test_a_revoked_session_row_is_refused(self):
        client = self.admin_client()
        admin = self.server.app.auth.service.db.ensure_local_user("admin")
        self.server.app.auth.service.db.revoke_all_for_user(admin.id)
        reply = client.get("/atlas")
        self.assertEqual(reply.status, 302)
        self.assertNotIn(preview.DEMO_BANNER_ID.encode(), reply.body)


# ===========================================================================
# 5. Authorisation inside the protected pages
# ===========================================================================


class AuthorisationTests(PreviewTestCase):
    def test_an_administrator_with_no_project_grant_reaches_the_demo(self):
        # 本地管理员在后端就是 0 个项目授权；预览不能因此把人挡在门外。
        client = self.admin_client()
        reply = client.get("/atlas")
        self.assertEqual(reply.status, 200)
        self.assertIn(preview.DEMO_BANNER_ID.encode(), reply.body)

    def test_a_member_without_projects_cannot_reach_the_demo(self):
        user_id = self.server.create_member(open_id="ou_pending", display_name="Synthetic Pending")
        client = self.server.client()
        client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        for path in ("/", "/atlas"):
            reply = client.get(path)
            self.assertEqual(reply.status, 403, f"{path} 必须拒绝 0 项目的成员")
            self.assertNotIn(preview.DEMO_BANNER_ID.encode(), reply.body)
        # 但仍能读到自己的身份：拒绝的是内容，不是身份
        self.assertEqual(client.get("/auth/me").status, 200)

    def test_a_member_with_a_project_grant_reaches_the_demo(self):
        user_id = self.server.create_member(open_id="ou_member", display_name="Synthetic Member")
        self.server.grant_project(user_id, "synthetic-atlas")
        client = self.server.client()
        client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        reply = client.get("/atlas")
        self.assertEqual(reply.status, 200)
        self.assertIn(preview.DEMO_BANNER_ID.encode(), reply.body)

    def test_a_disabled_account_cannot_read_the_demo(self):
        client = self.admin_client()
        admin = self.server.app.auth.service.db.ensure_local_user("admin")
        self.server.app.auth.service.db.set_user_status(admin.id, store.STATUS_DISABLED)
        reply = client.get("/atlas")
        self.assertEqual(reply.status, 302)
        self.assertNotIn(preview.DEMO_BANNER_ID.encode(), reply.body)


# ===========================================================================
# 6. The DEMO page is unmistakably a demo
# ===========================================================================


class DemoPageTests(PreviewTestCase):
    def test_the_demo_page_is_prominently_marked_and_locked_down(self):
        reply = self.admin_client().get("/atlas")
        self.assertEqual(reply.status, 200)

        body = reply.body.decode("utf-8")
        self.assertIn(f'id="{preview.DEMO_BANNER_ID}"', body, "必须带醒目的 DEMO 标记")
        self.assertIn("合成演示数据", body)
        self.assertIn("本地预览环境", body)
        self.assertIn("未连接生产", body)
        # 横幅必须在正文之前，避免被页面自身的布局盖住
        self.assertLess(body.index(preview.DEMO_BANNER_ID), body.index("实验室概览"))

        csp = reply.header("content-security-policy") or ""
        self.assertIn("connect-src 'none'", csp, "DEMO 页面不得发起任何网络请求")
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(reply.header("cache-control"), "no-store")
        self.assertEqual(reply.header("x-content-type-options"), "nosniff")

    def test_the_demo_page_serves_the_prototype_and_names_no_real_endpoint(self):
        body = self.admin_client().get("/atlas").body.decode("utf-8")
        # 内容确实来自 prototypes/v5/index.html，而不是某个占位页
        self.assertIn("<title>Dell LLM Lab — Atelier Observatory · 05</title>", body)
        self.assertIn('id="view-observability"', body)
        # 且没有把任何真实主机名/地址带出去。这里用保留的合成端点做负向断言，
        # 不再在 Public 仓库里保留生产地址或内部域名。
        for host in ("192.0.2.99", "internal.example.com", "sso.example.com", "login.example.com"):
            self.assertNotIn(host, body, f"DEMO 页面不应出现真实端点 {host}")

    def test_the_no_project_page_is_also_a_locked_down_page(self):
        user_id = self.server.create_member(open_id="ou_pending2", display_name="Synthetic Pending 2")
        client = self.server.client()
        client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)
        reply = client.get("/atlas")
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.header("cache-control"), "no-store")
        csp = reply.header("content-security-policy") or ""
        self.assertIn("frame-ancestors 'none'", csp)
        # 这一页自带一个内联 <style>，策略必须允许它：挂上不允许内联样式的策略，
        # 浏览器会拦掉样式（页面裸奔）并记一条 CSP 违规 —— 浏览器 E2E 抓的就是这个。
        self.assertIn("<style>", reply.body.decode("utf-8"))
        self.assertIn("style-src 'unsafe-inline'", csp)
        # 但脚本仍然一个都不许有
        self.assertNotIn("script-src", csp)

    def test_the_login_page_carries_a_restrictive_policy_too(self):
        csp = self.client.get("/login").header("content-security-policy") or ""
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)


# ===========================================================================
# 7. The real login round trip over the wire
# ===========================================================================


class LoginRoundTripTests(PreviewTestCase):
    def test_real_admin_login_then_me_then_logout(self):
        client = self.server.client()

        csrf = client.get("/auth/csrf")
        self.assertEqual(csrf.status, 200)
        payload = csrf.json()
        self.assertTrue(payload["ok"])
        self.assertIn("local_admin", payload["data"]["providers"])
        self.assertFalse(payload["data"]["providers"]["feishu"], "默认不配置飞书")
        token = payload["data"]["csrf_token"]

        login = client.request(
            "POST",
            "/auth/local/login",
            body=json.dumps({"username": "admin", "password": self.server.password}).encode(),
            content_type="application/json",
            headers={"Origin": self.origin, "X-CSRF-Token": token},
        )
        self.assertEqual(login.status, 200, login.body.decode("utf-8", "replace"))
        session_cookie = login.header("set-cookie") or ""
        self.assertIn(support.SESSION_COOKIE, session_cookie)
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=Lax", session_cookie)
        # 仅在显式声明的本地 HTTP 开发模式下才允许没有 Secure
        self.assertNotIn("Secure", session_cookie)

        me = client.get("/auth/me")
        self.assertEqual(me.status, 200)
        identity = me.json()["data"]
        self.assertTrue(identity["authenticated"])
        self.assertTrue(identity["user"]["is_platform_admin"])
        self.assertEqual(identity["user"]["role"], store.ROLE_ADMIN)
        self.assertEqual(identity["user"]["auth_source"], store.SOURCE_LOCAL)
        self.assertEqual(identity["projects"], [])

        logout = client.write("POST", "/auth/logout", {})
        self.assertEqual(logout.status, 200)
        self.assertIn("Max-Age=0", logout.header("set-cookie") or "")

        after = client.get("/auth/me")
        self.assertEqual(after.status, 401)
        self.assertEqual(after.error_code(), "not_authenticated")

    def test_the_ui_request_shape_is_accepted_verbatim(self):
        # 浏览器发的就是这一组：同源 Origin + X-CSRF-Token 头 + 只含凭据的 JSON 体。
        client = self.server.client()
        token = client.csrf_token()
        reply = client.request(
            "POST",
            "/auth/local/login",
            body=json.dumps({"username": "admin", "password": self.server.password}).encode(),
            content_type="application/json",
            headers={"Origin": self.origin, "X-CSRF-Token": token, "Accept": "application/json"},
        )
        self.assertEqual(reply.status, 200)

    def test_a_write_without_the_csrf_header_is_refused(self):
        reply = self.client.request(
            "POST",
            "/auth/local/login",
            body=json.dumps({"username": "admin", "password": self.server.password}).encode(),
            content_type="application/json",
            headers={"Origin": self.origin},
        )
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), "csrf_rejected")

    def test_a_cross_origin_write_is_refused(self):
        reply = self.client.request(
            "POST",
            "/auth/logout",
            body=b"{}",
            content_type="application/json",
            headers={"Origin": "https://evil.example", "X-CSRF-Token": self.client.csrf_token()},
        )
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), "csrf_rejected")

    def test_a_body_carried_csrf_value_does_not_count(self):
        # 协议已统一：CSRF 只走请求头。请求体里放 token 不构成任何授权。
        token = self.client.csrf_token()
        reply = self.client.request(
            "POST",
            "/auth/local/login",
            body=json.dumps(
                {"username": "admin", "password": self.server.password, "csrf_token": token}
            ).encode(),
            content_type="application/json",
            headers={"Origin": self.origin},
        )
        self.assertEqual(reply.status, 403)

    def test_the_wrong_password_is_a_generic_401(self):
        reply = self.client.login(self.server.password + "-wrong")
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.error_code(), "invalid_credentials")
        self.assertNotIn(self.server.password.encode(), reply.body)

    def test_error_responses_keep_the_ok_false_envelope(self):
        reply = self.client.get("/auth/me")
        self.assertEqual(reply.status, 401)
        payload = reply.json()
        self.assertIs(payload["ok"], False)
        self.assertEqual(set(payload["error"]), {"code", "message"})


# ===========================================================================
# 8. Feishu availability is honest, and never probed with side effects
# ===========================================================================


class FeishuAvailabilityTests(PreviewTestCase):
    mock_feishu = False

    def test_start_is_503_when_no_feishu_application_is_configured(self):
        reply = self.client.get("/auth/feishu/start", cookies=False)
        self.assertEqual(reply.status, 503)
        self.assertEqual(reply.error_code(), "not_configured")

    def test_csrf_advertises_feishu_as_unavailable_and_sets_no_cookie(self):
        reply = self.client.get("/auth/csrf", cookies=False)
        self.assertFalse(reply.json()["data"]["providers"]["feishu"])
        self.assertEqual(reply.set_cookies, [], "/auth/csrf 不得下发任何 Cookie")


# ===========================================================================
# 9. No hang on a request with no declared body
# ===========================================================================


class NoHangTests(PreviewTestCase):
    """The WSGI fix under test: a bodyless request must not block the server.

    ``http.client`` always sends a ``Content-Length`` (even ``0``), which hides
    the defect, so these speak the protocol by hand.
    """

    def test_get_without_content_length_answers_immediately(self):
        raw = (
            "GET /auth/csrf HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.port}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        payload = self.client.raw_exchange(raw, timeout=3.0)
        self.assertTrue(payload, "服务端必须应答，而不是挂起")
        self.assertIn(b"200", payload.split(b"\r\n", 1)[0])
        self.assertIn(b"csrf_token", payload)

    def test_a_bodyless_write_is_decided_by_the_guard_not_by_waiting(self):
        # 没有声明长度 ⇒ 空请求体 ⇒ 立刻走到 CSRF 守卫并 403（而不是等一个永远不来的体）。
        raw = (
            "POST /auth/logout HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.port}\r\n"
            f"Origin: {self.origin}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        payload = self.client.raw_exchange(raw, timeout=3.0)
        self.assertTrue(payload)
        self.assertIn(b"403", payload.split(b"\r\n", 1)[0])

    def test_a_declared_length_is_still_read(self):
        body = json.dumps({"username": "admin", "password": self.server.password}).encode()
        reply = self.client.request(
            "POST",
            "/auth/local/login",
            body=body,
            content_type="application/json",
            headers={"Origin": self.origin, "X-CSRF-Token": self.client.csrf_token()},
        )
        self.assertEqual(reply.status, 200)


# ===========================================================================
# 10. Mock Feishu (opt-in, fully offline)
# ===========================================================================


class MockFeishuTests(PreviewTestCase):
    mock_feishu = True

    def test_mock_mode_advertises_feishu_and_redirects_to_a_loopback_url(self):
        reply = self.client.get("/auth/feishu/start", cookies=False)

        self.assertEqual(reply.status, 302)
        location = reply.header("location") or ""
        self.assertIn("127.0.0.1", location, "mock 授权页必须仍在本机")
        self.assertIn(preview.MOCK_AUTHORIZE_PATH, location)
        self.assertNotIn("feishu.cn", location)

        flow = reply.header("set-cookie") or ""
        self.assertIn(
            self.server.app.cfg.oauth_binding_cookie_name, flow, "必须下发浏览器绑定 Cookie"
        )
        self.assertIn("SameSite=Lax", flow)
        self.assertIn("HttpOnly", flow)

    def test_the_csrf_endpoint_reports_feishu_as_available_in_mock_mode(self):
        # 只读探测：UI 从 /auth/csrf 就能知道飞书可用，不必去戳有副作用的 start。
        reply = self.client.get("/auth/csrf", cookies=False)
        self.assertTrue(reply.json()["data"]["providers"]["feishu"])
        self.assertEqual(reply.set_cookies, [])

    def test_mock_configuration_is_synthetic(self):
        cfg = self.server.app.cfg
        self.assertIn("mock", (cfg.feishu_app_id or "").lower())
        self.assertTrue(all("mock" in tenant.lower() for tenant in cfg.feishu_tenant_allowlist))

    def test_the_browser_binding_cookie_cannot_be_skipped(self):
        start = self.client.get("/auth/feishu/start", cookies=False)
        location = start.header("location") or ""
        state = self._query(location, "state")

        # 另一个浏览器拿到同样的回调 URL —— 经典 login-CSRF —— 必须被拒。
        naked = self.server.client()
        reply = naked.get(
            f"/auth/feishu/callback?state={state}&code={preview.mock_code_for(state)}"
        )
        self.assertEqual(reply.status, 302)
        self.assertIn("error=", reply.header("location") or "")
        self.assertNotIn(support.SESSION_COOKIE, reply.header("set-cookie") or "")

    def test_a_full_mock_callback_signs_in_without_any_network(self):
        client = self.server.client()
        start = client.get("/auth/feishu/start", cookies=False)
        self.assertEqual(start.status, 302)
        location = start.header("location") or ""
        state = self._query(location, "state")
        self.assertTrue(state)

        # 带上发起流程时下发的那枚浏览器绑定 Cookie
        for raw in start.set_cookies:
            pair = raw.split(";", 1)[0].strip()
            name, _, value = pair.partition("=")
            client.cookies[name] = value

        callback = client.get(
            f"/auth/feishu/callback?state={state}&code={preview.mock_code_for(state)}"
        )
        self.assertEqual(callback.status, 302, callback.body.decode("utf-8", "replace"))
        self.assertIn(support.SESSION_COOKIE, callback.header("set-cookie") or "")

        me = client.get("/auth/me")
        self.assertEqual(me.status, 200)
        identity = me.json()["data"]
        self.assertEqual(identity["user"]["auth_source"], store.SOURCE_FEISHU)
        self.assertFalse(identity["user"]["is_platform_admin"])
        self.assertEqual(identity["projects"], [], "新成员没有任何项目授权")

        # 新成员进不了受保护页面 —— 由服务端判定，不由页面判定
        self.assertEqual(client.get("/atlas").status, 403)

    def test_each_mock_authorisation_issues_its_own_code(self):
        """同一台预览服务器上连续两次登录都必须成功。

        真实飞书每次授权都发一枚**一次性**的新 code，后端也照这个规则拒绝重放
        （``claim_provider_code``）。mock 若图省事发一个常量 code，第二次登录就会
        被后端以 ``invalid_state`` 拒绝——浏览器 E2E 里连续走两遍 mock 飞书流程时
        抓到的正是这个。
        """
        codes = []
        for attempt in (1, 2):
            client = self.server.client()
            start = client.get("/auth/feishu/start", cookies=False)
            state = self._query(start.header("location") or "", "state")
            code = preview.mock_code_for(state)
            codes.append(code)
            for raw in start.set_cookies:
                pair = raw.split(";", 1)[0].strip()
                name, _, value = pair.partition("=")
                client.cookies[name] = value

            callback = client.get(f"/auth/feishu/callback?state={state}&code={code}")
            self.assertEqual(
                callback.status, 302, callback.body.decode("utf-8", "replace")
            )
            self.assertNotIn(
                "error=",
                callback.header("location") or "",
                f"第 {attempt} 次登录不得因为 code 被重放而被拒",
            )
            self.assertEqual(client.get("/auth/me").status, 200)

        self.assertNotEqual(codes[0], codes[1], "每次授权都必须发一枚新的 code")
        self.assertNotIn(
            preview.MOCK_CODE_PREFIX, codes, "code 不能是一个与 state 无关的常量"
        )

    def test_the_mock_provider_refuses_a_code_it_did_not_issue(self):
        provider = preview.MockFeishuProvider(lambda: "http://127.0.0.1:0")
        for bad in ("", preview.MOCK_CODE_PREFIX, preview.MOCK_CODE_PREFIX + "-", "anything-else"):
            with self.subTest(code=bad):
                with self.assertRaises(errors.AccessDenied):
                    provider.exchange_code(code=bad)
        identity = provider.exchange_code(code=preview.mock_code_for("synthetic-state"))
        self.assertEqual(identity.open_id, preview.MOCK_OPEN_ID)

    def test_the_mock_consent_page_stays_on_this_machine(self):
        start = self.client.get("/auth/feishu/start", cookies=False)
        location = start.header("location") or ""
        path = location[location.index(preview.MOCK_AUTHORIZE_PATH) :]
        page = self.client.get(path, cookies=False)
        self.assertEqual(page.status, 200)
        body = page.body.decode("utf-8")
        self.assertIn("Mock 飞书授权页", body)
        # 页面上唯一的可点击目标就是本机回调；没有任何绝对链接，也就没有出口。
        self.assertNotIn("https://", body)
        self.assertNotIn('href="http', body)
        self.assertIn('href="/auth/feishu/callback?', body)
        self.assertNotIn("https://open.feishu.cn", body)

    @staticmethod
    def _query(url: str, name: str) -> str:
        import urllib.parse

        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get(name, "")


class _InterruptingServer:
    """A stand-in for the bound server whose foreground loop is Ctrl+C'd.

    Only the three things ``main`` touches are modelled: the bound address it
    prints, the loop it enters, and the close it owes afterwards.
    """

    def __init__(self, port: int = 43210) -> None:
        self.server_address = ("127.0.0.1", port)
        self.interrupted = False
        self.closed = False

    def serve_forever(self) -> None:
        self.interrupted = True
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


class CliFirstRunTests(unittest.TestCase):
    """首次运行的 CLI 契约，用真实的 ``python -m`` 子进程来验。

    这一组正对着曾经的那个首启缺陷：``--state-dir`` 是可选的，于是 CLI 会先凭空
    造一个私有临时目录，再把「这个目录里没有管理员凭据」的检查叠在它前面——必然
    失败，还留下一个没人要的空目录，并打印一条指向操作者从没指定过的路径的
    bootstrap 命令。README 同时声称「省略 --state-dir 会创建临时目录」，但那样
    的目录根本没有任何交互式初始化的机会。

    这里没有任何口令预置：唯一存在过的凭据，是经由与 ``bootstrap init`` 完全同
    一条交互式 prompt 缝、在进程内、用随机值创建的。它不打印、不落盘、不进命令行。
    """

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-preview-cli-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state_dir = self.root / "state"
        # 给子进程一个私有的 TEMP：这样「它有没有凭空造目录」是靠数条目回答的，
        # 而不是靠猜目录名。所有三个变量名都给，免得不同平台各认一个。
        self.temp = self.root / "tmp"
        self.temp.mkdir(mode=0o700)

    # --- helpers ------------------------------------------------------------

    #: How long the child gets to announce its port before the test gives up.
    #: Bounded on purpose: a handshake that can wait forever turns one broken
    #: run into a hung suite.
    HANDSHAKE_TIMEOUT = 10.0

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            TEMP=str(self.temp),
            TMP=str(self.temp),
            TMPDIR=str(self.temp),
            # The child's banner must reach the pipe as it is written, not when
            # the process exits -- otherwise the port handshake blocks on a
            # buffer that only fills at shutdown.
            PYTHONUNBUFFERED="1",
        )
        return env

    def run_cli(self, *args: str) -> "subprocess.CompletedProcess[str]":
        """Run the real CLI to completion. Only for invocations expected to refuse."""
        return subprocess.run(
            [sys.executable, "-m", "dev.preview_auth", *args],
            cwd=str(preview.REPO_ROOT),
            env=self.child_env(),
            capture_output=True,
            text=True,
            timeout=90,
        )

    def spawn_cli(self, *args: str) -> "subprocess.Popen[str]":
        """Start the real CLI as a foreground process, as an operator would."""
        proc = subprocess.Popen(
            # ``-u`` on top of PYTHONUNBUFFERED: the banner is what the
            # handshake waits on, so it must not sit in a pipe buffer.
            [sys.executable, "-u", "-m", "dev.preview_auth", *args],
            cwd=str(preview.REPO_ROOT),
            env=self.child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc: "subprocess.Popen[str]") -> None:
        """Guarantee the child is gone, whatever happened in the test body.

        Registered as a cleanup for every spawned process, so a failing
        assertion can never leave a listener behind.
        """
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:  # pragma: no cover - defensive
                    pass

    def spawn_and_wait_for_port(self, *args: str):
        """Start the CLI and read the loopback port it announces on stdout.

        The port comes from the operator-facing banner (``sign in: ...``) — what
        a human sees — not from a test-only side channel. A reader thread does
        the blocking work so the test itself waits on a bounded event, and a
        child that never announces anything is killed before the failure is
        reported rather than left running.
        """
        proc = self.spawn_cli(*args, "--port", "0")
        announced: list[int] = []
        ready = threading.Event()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                match = re.search(r"sign in:\s+http://127\.0\.0\.1:(\d+)/login", line)
                if match and not announced:
                    announced.append(int(match.group(1)))
                    ready.set()

        threading.Thread(target=pump, daemon=True).start()
        if not ready.wait(timeout=self.HANDSHAKE_TIMEOUT):
            self._reap(proc)  # the pipes only read safely once the child is gone
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            self.fail(
                f"预览没有在 {self.HANDSHAKE_TIMEOUT:.0f} 秒内报告端口；stderr={stderr!r}"
            )
        return proc, announced[0]

    def bootstrap_here(self) -> str:
        """Create the administrator the way an operator does: interactively.

        In-process, through the same prompt seam the CLI uses. The password is
        random and is never printed, never passed on a command line and never
        written down as text — only its hash reaches the state directory.
        """
        cfg = AuthConfig.from_env({}, state_dir=self.state_dir)
        password = support.random_password()
        bootstrap.initialize_admin_password(
            cfg, read_password=support.ScriptedReader(password, password)
        )
        return password

    # --- the defect this file was missing -----------------------------------

    def test_the_cli_requires_an_explicit_state_dir(self):
        # 不给目录时必须**立刻**拒绝，而且绝不能凭空造一个目录出来：曾经的实现
        # 先 mkdtemp 再失败，既留下垃圾，又打印一条指向凭空路径的命令。
        result = self.run_cli("--local-http-dev")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("--state-dir", result.stderr)
        self.assertEqual(
            [p.name for p in self.temp.iterdir()],
            [],
            "缺少 --state-dir 时不得创建任何目录",
        )

    def test_the_cli_fails_closed_on_a_directory_that_was_never_bootstrapped(self):
        # 指定的目录还没 bootstrap：不启动、不写凭据，并打印该怎么创建。
        result = self.run_cli("--local-http-dev", "--state-dir", str(self.state_dir))
        self.assertEqual(result.returncode, 2, result.stdout)
        text = (result.stdout + result.stderr).lower()
        self.assertIn("bootstrap", text)
        self.assertIn("--state-dir", text)
        self.assertFalse(
            (self.state_dir / "admin_password.hash").exists(),
            "拒绝启动时不得写入任何凭据",
        )

    def test_the_cli_refuses_to_bind_anything_but_loopback(self):
        self.bootstrap_here()
        for host in ("0.0.0.0", "::", "192.168.1.10", "portal.example.com"):
            with self.subTest(host=host):
                result = self.run_cli(
                    "--local-http-dev", "--state-dir", str(self.state_dir), "--host", host
                )
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("loopback", (result.stdout + result.stderr).lower())

    def test_the_cli_refuses_a_state_dir_inside_the_repository(self):
        # 凭据与 Session 库是本地开发数据，绝不允许落在可以提交的树里。
        inside = preview.REPO_ROOT / ".state-should-never-be-created-here"
        self.addCleanup(shutil.rmtree, inside, ignore_errors=True)
        self.assertFalse(inside.exists())
        result = self.run_cli("--local-http-dev", "--state-dir", str(inside))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("repository", (result.stdout + result.stderr).lower())
        self.assertFalse(inside.exists(), "被拒绝的 state_dir 不得被创建出来")

    def test_the_cli_refuses_a_state_dir_that_is_a_symlink(self):
        # 一个链接可以在被检查之后被重新指向仓库，所以直接拒绝，不跟过去。
        target = self.root / "real-state"
        target.mkdir(mode=0o700)
        link = self.root / "linked-state"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("本机（未提权的 Windows）不接受创建符号链接")
        result = self.run_cli("--local-http-dev", "--state-dir", str(link))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("symbolic link", (result.stdout + result.stderr).lower())

    # --- the happy path ------------------------------------------------------

    def test_the_cli_serves_a_bootstrapped_directory_on_loopback(self):
        # 先 bootstrap，再启动：指定的同一个目录里已经有一个交互式建好的管理员。
        self.bootstrap_here()
        _proc, port = self.spawn_and_wait_for_port(
            "--local-http-dev", "--state-dir", str(self.state_dir)
        )

        # 真的在环回上监听了：走真 socket 取一次真实登录页。
        reply = support.LoopbackClient(port=port).get("/login")
        self.assertEqual(reply.status, 200, reply.body[:200])
        self.assertIn(b"/auth.js", reply.body)

        # 运行期的两样东西都落在仓库之外——这正是它们进不了仓库的原因。
        produced = sorted(p.name for p in self.state_dir.iterdir())
        self.assertIn("admin_password.hash", produced)
        self.assertIn("auth.sqlite3", produced)
        self.assertNotIn(
            preview.REPO_ROOT.resolve(),
            self.state_dir.resolve().parents,
            "state_dir 必须在仓库之外",
        )

    def test_main_shuts_the_socket_down_when_the_foreground_is_interrupted(self):
        """Ctrl+C 的语义，不依赖在 Windows 上发控制台事件。

        真的去发 ``CTRL_C_EVENT`` / ``SIGINT`` 既不可移植、又容易把整个进程组一起
        打断；而 ``main`` 对 Ctrl+C 的全部处理其实只有一件事：中断 ``serve_forever``
        之后必须 ``server_close()`` 并返回 0。就把这一点直接钉住。
        """
        self.bootstrap_here()
        stub = _InterruptingServer()
        with mock.patch.object(preview, "make_server", return_value=stub):
            code = preview.main(["--local-http-dev", "--state-dir", str(self.state_dir)])

        self.assertTrue(stub.interrupted, "main 必须真的进入前台服务循环")
        self.assertTrue(stub.closed, "被中断之后必须关掉监听套接字")
        self.assertEqual(code, 0, "被 Ctrl+C 打断的前台预览应以 0 退出")


class StateDirRedirectTests(unittest.TestCase):
    """``validate_state_dir`` 必须挡住 **Windows 目录 junction**。

    ``Path.is_symlink()`` 对 junction 返回 ``False``：junction 是 reparse point
    （``FILE_ATTRIBUTE_REPARSE_POINT``，0x400，实测 junction = 0x410、普通目录 =
    0x10），不是 symlink，``S_ISLNK`` 不成立。而 ``mklink /J`` **不需要管理员权限**
    （不带 ``/J`` 的真 symlink 才需要）。于是在 Windows 上，一条只查 symlink 的
    防线对 junction 完全不设防，而 ``is_symlink() is False`` 又会让人以为检查生效了。

    这一组同时钉住三件事：junction 本身要被拒、**祖先**是 junction 也要被拒（否则
    把 state 放进去就绕过了）、以及普通目录（含本机 8.3 短名 TEMP）**不得**被误拒。
    """

    #: 本机普通目录的属性位，用来对照 junction 的 0x410。
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-preview-link-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def make_junction(link: pathlib.Path, target: pathlib.Path) -> None:
        """建一个真的目录 junction；本机建不出来就跳过，不假装通过。"""
        if os.name != "nt":
            raise unittest.SkipTest("junction 是 Windows 文件系统的概念")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise unittest.SkipTest(
                f"本机 mklink /J 不可用：{completed.stderr or completed.stdout}"
            )

    # --- 被绕过的那个事实本身 -------------------------------------------------

    def test_a_junction_is_not_reported_as_a_symlink(self):
        # 这条测试存在的意义是钉住「为什么必须另加一条判据」：如果哪天
        # is_symlink() 开始认得 junction 了，下面那条防线就可以简化。
        target = self.root / "real-state"
        target.mkdir(mode=0o700)
        link = self.root / "junction-state"
        self.make_junction(link, target)

        self.assertFalse(link.is_symlink(), "junction 不是 symlink——这正是漏洞所在")
        self.assertFalse(os.path.islink(link), "os.path.islink 同样认不出来")
        self.assertTrue(link.is_dir(), "对文件系统而言它就是一个目录，所以会被照常使用")
        attributes = link.lstat().st_file_attributes
        self.assertTrue(
            attributes & self.FILE_ATTRIBUTE_REPARSE_POINT,
            f"junction 必须带 reparse 位（实测 0x{attributes:04x}）",
        )

    # --- 直接调用：junction 本身、以及祖先 --------------------------------------

    def test_a_state_dir_that_is_a_junction_is_refused(self):
        target = self.root / "real-state"
        target.mkdir(mode=0o700)
        link = self.root / "junction-state"
        self.make_junction(link, target)

        with self.assertRaises(errors.ConfigError) as caught:
            preview.validate_state_dir(link)
        self.assertIn("junction", caught.exception.detail.lower())

    def test_a_state_dir_behind_a_junction_ancestor_is_refused(self):
        # 只查最后一段是不够的：把真实目录套进一个 junction，state 自己是个
        # 好端端的普通目录，但它的凭据路径整体已经被改向了。
        real = self.root / "real-parent"
        (real / "state").mkdir(parents=True, mode=0o700)
        link = self.root / "junction-parent"
        self.make_junction(link, real)

        behind = link / "state"
        self.assertTrue(behind.is_dir(), "对文件系统而言这就是个普通目录")

        with self.assertRaises(errors.ConfigError) as caught:
            preview.validate_state_dir(behind)
        self.assertIn("junction", caught.exception.detail.lower())

    # --- 不得误拒 --------------------------------------------------------------

    def test_an_ordinary_directory_is_still_accepted(self):
        plain = self.root / "plain-state"
        plain.mkdir(mode=0o700)
        self.assertEqual(preview.validate_state_dir(plain), plain)

    def test_a_directory_that_does_not_exist_yet_is_still_accepted_under_a_clean_parent(self):
        # CLI 的路径：目录还没 bootstrap，`validate_state_dir` 只做校验、不创建。
        not_yet = self.root / "not-created-yet"
        self.assertFalse(not_yet.exists())
        self.assertEqual(preview.validate_state_dir(not_yet), not_yet)
        self.assertFalse(not_yet.exists(), "校验不得创建目录")

    def test_the_platform_temp_directory_is_never_falsely_rejected(self):
        # 本机 %TEMP% 是 8.3 短名（C:\\Users\\BOB~1.EXAMPLE\\...），而
        # `Temp.resolve()` 会还原成长名——所以**绝不能**用「resolve() 是否等于原
        # 路径」来判断有没有被改向，那样每个普通 TEMP 目录都会被误拒。
        for spelling in {str(tempfile.gettempdir()), str(pathlib.Path(tempfile.gettempdir()).resolve())}:
            with self.subTest(spelling=spelling):
                candidate = pathlib.Path(spelling) / "llm-lab-short-name-probe"
                self.assertEqual(preview.validate_state_dir(candidate), candidate)

    # --- 走真实 CLI 子进程 -----------------------------------------------------

    def test_the_cli_refuses_a_junction_state_dir_by_name(self):
        # 目标目录故意**没有** bootstrap：这样在修复前，CLI 会因为「目录里没有
        # 管理员」而退出 2，但消息里**不会**提到链接。断言消息本身，才不会把
        # 「恰好同样退出 2」误当成本防线生效。
        target = self.root / "real-state"
        target.mkdir(mode=0o700)
        link = self.root / "junction-state"
        self.make_junction(link, target)

        completed = subprocess.run(
            [sys.executable, "-m", "dev.preview_auth", "--local-http-dev",
             "--state-dir", str(link)],
            cwd=str(preview.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
        combined = (completed.stdout + completed.stderr).lower()
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertIn("junction", combined)
        self.assertNotIn("no administrator credential", combined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
