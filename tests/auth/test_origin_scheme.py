"""Origin / Referer same-origin tests (P1).

``Application._origin_ok()`` used to compare only ``Origin.netloc`` with the
request's ``Host``. That drops the scheme, so a state-changing request that
arrived over HTTPS was accepted if it carried ``Origin: http://<same host>`` —
an origin the attacker's plain-HTTP page can set and that the browser will
happily attach to a real cross-origin ``fetch``. The comparison must cover the
full ``scheme://host[:port]`` tuple.

The scheme used for that comparison must come from the *server's* view of the
request (``wsgi.url_scheme``, set by the WSGI container); ``X-Forwarded-Proto``
and ``Forwarded`` are attacker-controlled request headers and must never be
able to upgrade the comparison.

Also covered here: the negative cases the review asked for — ``Origin: null``,
a scheme-relative origin, and an untrusted ``Referer`` — plus the open-redirect
vectors, so a regression in any of them fails loudly.
"""

import shutil
import unittest
import urllib.parse

from backend.auth import bootstrap, routes
from tests.auth import support
from tests.auth.test_bootstrap import ScriptedReader
from tests.auth.test_store import FakeClock

SESSION_COOKIE = "llm_lab_session"
SYNTHETIC_ADMIN_PASSWORD = "synthetic-admin-passphrase-1"
LOGIN_BODY = {"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD}


class OriginTestCase(unittest.TestCase):
    HOST = "portal.example.com"

    def setUp(self):
        self.tmp = support.temp_dir("llm-lab-auth-origin-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.cfg = support.make_config(self.tmp, **support.feishu_env())
        self.transport = support.FakeTransport()
        self.app = routes.create_app(self.cfg, transport=self.transport, clock=self.clock)
        self.app.initialize()
        # A local administrator has to exist, otherwise an accepted request
        # would answer 503 and mask what the Origin guard actually decided.
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(SYNTHETIC_ADMIN_PASSWORD, SYNTHETIC_ADMIN_PASSWORD)
        )
        self.client = support.WsgiClient(self.app, host=self.HOST, scheme="https")

    # --- helpers -------------------------------------------------------------

    def csrf_token(self, client=None):
        response = (client or self.client).run("GET", "/auth/csrf")
        self.assertEqual(response.status, 200)
        return response.json()["data"]["csrf_token"]

    def post_login(self, client=None, *, origin=None, referer=None, extra=None):
        """A real state-changing request carrying a valid CSRF token."""
        client = client or self.client
        headers = {"X-CSRF-Token": self.csrf_token(client)}
        if origin is not None:
            headers["Origin"] = origin
        if referer is not None:
            headers["Referer"] = referer
        headers.update(extra or {})
        return client.run(
            "POST",
            "/auth/local/login",
            payload=LOGIN_BODY,
            headers=headers,
            # The explicit Origin/Referer above is the only one sent.
            send_origin=False,
        )


class OriginSchemeTests(OriginTestCase):
    def test_matching_scheme_and_host_is_accepted(self):
        """The guard must not be so strict that it breaks the happy path."""
        response = self.post_login(origin=f"https://{self.HOST}")
        self.assertEqual(response.status, 200)

    def test_http_origin_on_an_https_request_is_refused(self):
        response = self.post_login(origin=f"http://{self.HOST}")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "csrf_rejected")

    def test_https_origin_on_an_http_request_is_refused(self):
        plain = support.WsgiClient(self.app, host=self.HOST, scheme="http")
        response = self.post_login(plain, origin=f"https://{self.HOST}")
        self.assertEqual(response.status, 403)

    def test_scheme_relative_origin_is_refused(self):
        response = self.post_login(origin=f"//{self.HOST}")
        self.assertEqual(response.status, 403)

    def test_origin_null_is_refused(self):
        response = self.post_login(origin="null")
        self.assertEqual(response.status, 403)

    def test_an_origin_with_a_foreign_port_is_refused(self):
        response = self.post_login(origin=f"https://{self.HOST}:8443")
        self.assertEqual(response.status, 403)


class ForwardedHeaderTests(OriginTestCase):
    def test_x_forwarded_proto_cannot_upgrade_the_request_scheme(self):
        plain = support.WsgiClient(self.app, host=self.HOST, scheme="http")
        response = self.post_login(
            plain,
            origin=f"https://{self.HOST}",
            extra={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(response.status, 403)

    def test_forwarded_header_cannot_upgrade_the_request_scheme(self):
        plain = support.WsgiClient(self.app, host=self.HOST, scheme="http")
        response = self.post_login(
            plain,
            origin=f"https://{self.HOST}",
            extra={"Forwarded": f"proto=https;host={self.HOST}"},
        )
        self.assertEqual(response.status, 403)

    def test_x_forwarded_host_cannot_substitute_the_host(self):
        response = self.post_login(
            origin="https://evil.example",
            extra={"X-Forwarded-Host": "evil.example"},
        )
        self.assertEqual(response.status, 403)


class RefererTests(OriginTestCase):
    def test_a_same_scheme_referer_is_accepted(self):
        response = self.post_login(referer=f"https://{self.HOST}/login")
        self.assertEqual(response.status, 200)

    def test_a_scheme_mismatched_referer_is_refused(self):
        response = self.post_login(referer=f"http://{self.HOST}/login")
        self.assertEqual(response.status, 403)

    def test_an_untrusted_referer_is_refused(self):
        response = self.post_login(referer="https://evil.example/login")
        self.assertEqual(response.status, 403)

    def test_a_missing_origin_and_referer_is_refused(self):
        response = self.post_login()
        self.assertEqual(response.status, 403)


class TrustedOriginAllowlistTests(OriginTestCase):
    def test_an_explicitly_trusted_origin_is_accepted(self):
        cfg = support.make_config(
            self.tmp,
            **support.feishu_env(),
            LLM_LAB_TRUSTED_ORIGINS="https://portal.internal.example:8443",
        )
        app = routes.create_app(cfg, transport=support.FakeTransport(), clock=self.clock)
        app.initialize()
        client = support.WsgiClient(app, host="127.0.0.1:8000", scheme="https")

        token = client.run("GET", "/auth/csrf").json()["data"]["csrf_token"]
        response = client.run(
            "POST",
            "/auth/local/login",
            payload=LOGIN_BODY,
            headers={
                "X-CSRF-Token": token,
                "Origin": "https://portal.internal.example:8443",
            },
            send_origin=False,
        )
        self.assertEqual(response.status, 200)

    def test_the_allowlist_is_empty_by_default(self):
        """No proxy is trusted unless an operator names one."""
        self.assertEqual(set(self.cfg.trusted_origins), set())


class OpenRedirectTests(OriginTestCase):
    HOSTILE = (
        "https://evil.example/",
        "//evil.example/",
        "http://evil.example",
        "https://evil.example\\@portal.example.com/",
        "javascript:alert(1)",
        "/\\evil.example",
    )

    def test_next_parameter_can_never_leave_the_site(self):
        for hostile in self.HOSTILE:
            with self.subTest(next=hostile):
                response = self.client.run(
                    "GET",
                    "/auth/feishu/start?next=" + urllib.parse.quote(hostile, safe=""),
                )
                self.assertEqual(response.status, 302)
                self.assertTrue(response.location.startswith("https://accounts.feishu.cn/"))

    def test_the_callback_target_is_always_a_local_path(self):
        for index, hostile in enumerate(self.HOSTILE):
            with self.subTest(next=hostile):
                self.transport.queue(*support.success_script())
                started = self.client.run(
                    "GET",
                    "/auth/feishu/start?next=" + urllib.parse.quote(hostile, safe=""),
                )
                state = dict(
                    urllib.parse.parse_qsl(urllib.parse.urlsplit(started.location).query)
                )["state"]
                # A fresh authorisation code per run: codes are single-use, so
                # reusing one would fail for an unrelated reason.
                done = self.client.run(
                    "GET", f"/auth/feishu/callback?code=code-{index}&state={state}"
                )
                self.assertEqual(done.location, self.cfg.post_login_path)
                self.assertTrue(done.location.startswith("/"))
                self.assertFalse(done.location.startswith("//"))

    def test_a_local_next_path_survives_the_callback(self):
        self.transport.queue(*support.success_script())
        started = self.client.run("GET", "/auth/feishu/start?next=%2Fdashboard")
        state = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(started.location).query)
        )["state"]
        done = self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertEqual(done.location, "/dashboard")


__all__ = [
    "ForwardedHeaderTests",
    "OpenRedirectTests",
    "OriginSchemeTests",
    "RefererTests",
    "TrustedOriginAllowlistTests",
]
