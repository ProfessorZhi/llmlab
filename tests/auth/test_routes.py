"""End-to-end HTTP tests for the auth backend.

Every test drives the real WSGI callable, so the whole chain is covered:
environ parsing, routing, CSRF and Origin checks, the session cookie, the
Feishu callback, the JSON contract and the error taxonomy.

Two placeholders are used throughout and neither is a real credential:

* ``SYNTHETIC_ADMIN_PASSWORD`` — a fictional administrator password that exists
  only inside this test process;
* ``support.SYNTHETIC_APP_SECRET`` — a fictional Feishu application secret.
"""

import shutil
import unittest
import urllib.parse

from backend.auth import bootstrap, errors, local_admin, routes, store
from tests.auth import support
from tests.auth.test_bootstrap import ScriptedReader
from tests.auth.test_store import FakeClock

SYNTHETIC_ADMIN_PASSWORD = "synthetic-admin-passphrase-1"
SESSION_COOKIE = "llm_lab_session"


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir("llm-lab-auth-http-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.transport = support.FakeTransport()
        self.cfg = support.make_config(self.tmp, **support.feishu_env())
        self.app = routes.create_app(self.cfg, transport=self.transport, clock=self.clock)
        self.app.initialize()
        self.client = support.WsgiClient(self.app)

    # --- helpers -------------------------------------------------------------

    def bootstrap_admin(self, password=SYNTHETIC_ADMIN_PASSWORD):
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(password, password)
        )

    def new_app(self, *, transport=None, **env):
        cfg = support.make_config(self.tmp, **{**support.feishu_env(), **env})
        app = routes.create_app(
            cfg, transport=transport if transport is not None else support.FakeTransport(),
            clock=self.clock,
        )
        app.initialize()
        return app, support.WsgiClient(app)

    def csrf_token(self, client=None):
        response = (client or self.client).run("GET", "/auth/csrf")
        self.assertEqual(response.status, 200)
        return response.json()["data"]["csrf_token"]

    def login_admin(self, client=None, password=SYNTHETIC_ADMIN_PASSWORD):
        client = client or self.client
        return client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": password},
            headers={"X-CSRF-Token": self.csrf_token(client)},
        )

    def start_feishu(self, client=None, query=""):
        client = client or self.client
        response = client.run("GET", "/auth/feishu/start" + query)
        self.assertEqual(response.status, 302)
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(response.location).query))["state"]
        return response, state


class BootstrapEndpointTests(HttpTestCase):
    def test_csrf_endpoint_advertises_the_available_sign_in_methods(self):
        response = self.client.run("GET", "/auth/csrf")
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("csrf_token", payload["data"])
        self.assertEqual(
            payload["data"]["providers"], {"local_admin": False, "feishu": True}
        )

    def test_local_login_is_not_configured_before_bootstrap(self):
        response = self.login_admin()
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "not_configured")

    def test_not_configured_response_never_mentions_the_absence_of_a_hash(self):
        body = self.login_admin().body.decode("utf-8")
        for leak in ("hash", "scrypt", "password file"):
            self.assertNotIn(leak, body.lower())


class LocalLoginTests(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()

    def test_successful_login_issues_a_hardened_cookie(self):
        response = self.login_admin()
        self.assertEqual(response.status, 200)
        cookie = response.set_cookie(SESSION_COOKIE)
        self.assertIsNotNone(cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_cookie_value_is_not_the_csrf_token(self):
        response = self.login_admin()
        payload = response.json()
        self.assertNotIn(payload["data"]["csrf_token"], response.set_cookie(SESSION_COOKIE))

    def test_response_never_echoes_the_password_or_a_hash(self):
        body = self.login_admin().body.decode("utf-8")
        self.assertNotIn(SYNTHETIC_ADMIN_PASSWORD, body)
        self.assertNotIn("scrypt$", body)

    def test_wrong_password_is_a_generic_401(self):
        response = self.login_admin(password=SYNTHETIC_ADMIN_PASSWORD + "x")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "invalid_credentials")
        self.assertEqual(response.json()["error"]["message"], errors.InvalidCredentials.message)

    def test_unknown_username_is_indistinguishable_from_a_wrong_password(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "somebody", "password": SYNTHETIC_ADMIN_PASSWORD},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "invalid_credentials")

    def test_failed_login_sets_no_cookie(self):
        response = self.login_admin(password=SYNTHETIC_ADMIN_PASSWORD + "x")
        self.assertIsNone(response.set_cookie(SESSION_COOKIE))

    def test_get_is_not_allowed(self):
        response = self.client.run("GET", "/auth/local/login")
        self.assertEqual(response.status, 405)

    def test_non_json_body_is_rejected(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            raw_body=b"username=admin&password=nope",
            content_type="application/x-www-form-urlencoded",
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 400)

    def test_missing_fields_are_rejected(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin"},
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 400)


class CsrfTests(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()

    def test_write_without_a_csrf_token_is_refused(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "csrf_rejected")

    def test_write_with_a_bogus_csrf_token_is_refused(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD},
            headers={"X-CSRF-Token": store.new_secret_id()},
        )
        self.assertEqual(response.status, 403)

    def test_csrf_token_cannot_be_replayed(self):
        token = self.csrf_token()
        payload = {"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD}
        self.client.run("POST", "/auth/local/login", payload=payload, headers={"X-CSRF-Token": token})
        replay = self.client.run(
            "POST", "/auth/local/login", payload=payload, headers={"X-CSRF-Token": token}
        )
        self.assertEqual(replay.status, 403)

    def test_write_without_origin_or_referer_is_refused(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD},
            headers={"X-CSRF-Token": self.csrf_token()},
            send_origin=False,
        )
        self.assertEqual(response.status, 403)

    def test_cross_origin_write_is_refused(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD},
            headers={"X-CSRF-Token": self.csrf_token(), "Origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403)

    def test_referer_is_accepted_when_origin_is_absent(self):
        token = self.csrf_token()
        response = self.client.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD},
            headers={
                "X-CSRF-Token": token,
                "Referer": f"https://{self.client.host}/login",
            },
            send_origin=False,
        )
        self.assertEqual(response.status, 200)


class RateLimitTests(HttpTestCase):
    MAX_FAILURES = 3

    def setUp(self):
        super().setUp()
        self.bootstrap_admin()
        _, self.limited = self.new_app(LLM_LAB_LOGIN_MAX_FAILURES=str(self.MAX_FAILURES))

    def attempt(self, password):
        token = self.csrf_token(self.limited)
        return self.limited.run(
            "POST",
            "/auth/local/login",
            payload={"username": "admin", "password": password},
            headers={"X-CSRF-Token": token},
        )

    def test_lockout_after_repeated_failures(self):
        for _ in range(self.MAX_FAILURES):
            self.attempt(SYNTHETIC_ADMIN_PASSWORD + "x")
        response = self.attempt(SYNTHETIC_ADMIN_PASSWORD + "x")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.error_code(), "rate_limited")

    def test_lockout_also_blocks_the_correct_password(self):
        for _ in range(self.MAX_FAILURES):
            self.attempt(SYNTHETIC_ADMIN_PASSWORD + "x")
        response = self.attempt(SYNTHETIC_ADMIN_PASSWORD)
        self.assertEqual(response.status, 429)

    def test_retry_after_header_is_present(self):
        for _ in range(self.MAX_FAILURES):
            self.attempt(SYNTHETIC_ADMIN_PASSWORD + "x")
        response = self.attempt(SYNTHETIC_ADMIN_PASSWORD)
        self.assertIn("retry-after", response.headers)


class SessionAndIdentityTests(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()
        self.login_admin()

    def test_me_returns_the_identity_without_credentials(self):
        response = self.client.run("GET", "/auth/me")
        self.assertEqual(response.status, 200)
        payload = response.json()["data"]
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["role"], store.ROLE_ADMIN)
        self.assertTrue(payload["user"]["is_platform_admin"])
        self.assertEqual(payload["projects"], [])

    def test_me_never_exposes_upstream_or_credential_material(self):
        body = self.client.run("GET", "/auth/me").body.decode("utf-8")
        for leak in ("open_id", "union_id", "tenant_key", "scrypt", "hash", "Bearer",
                     SYNTHETIC_ADMIN_PASSWORD):
            self.assertNotIn(leak, body)

    def test_me_without_a_session_is_401(self):
        fresh = support.WsgiClient(self.app)
        response = fresh.run("GET", "/auth/me")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "not_authenticated")

    def test_me_with_a_forged_cookie_is_401(self):
        fresh = support.WsgiClient(self.app)
        fresh.cookies[SESSION_COOKIE] = store.new_secret_id()
        self.assertEqual(fresh.run("GET", "/auth/me").status, 401)

    def test_expired_session_is_401(self):
        self.clock.advance(self.cfg.session_absolute_seconds + 1)
        response = self.client.run("GET", "/auth/me")
        self.assertEqual(response.status, 401)

    def test_logout_requires_csrf(self):
        response = self.client.run("POST", "/auth/logout")
        self.assertEqual(response.status, 403)

    def test_logout_revokes_the_session_and_clears_the_cookie(self):
        issued_cookie = self.client.cookies[SESSION_COOKIE]
        token = self.csrf_token()
        response = self.client.run("POST", "/auth/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.set_cookie(SESSION_COOKIE))

        replay = support.WsgiClient(self.app)
        replay.cookies[SESSION_COOKIE] = issued_cookie
        self.assertEqual(replay.run("GET", "/auth/me").status, 401)

    def test_logout_without_a_session_is_401(self):
        fresh = support.WsgiClient(self.app)
        token = self.csrf_token(fresh)
        response = fresh.run("POST", "/auth/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status, 401)

    def test_logout_with_a_disabled_account_clears_the_cookie(self):
        """A disabled account still owns the cookie in its browser.

        The server refuses every protected request already; failing the logout
        as well leaves the browser holding a credential it cannot discard.
        Logout revokes whatever session the cookie names and clears it.
        """
        user = self.app.service.db.get_user(
            self.app.service.current_session(self.client.cookies[SESSION_COOKIE]).user.id
        )
        self.app.service.db.set_user_status(user.id, "disabled")
        token = self.csrf_token()

        response = self.client.run("POST", "/auth/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.set_cookie(SESSION_COOKIE))

        self.assertEqual(self.client.run("GET", "/auth/me").status, 401)

    def test_logout_with_an_idle_expired_session_clears_the_cookie(self):
        token = self.csrf_token()
        self.clock.advance(self.cfg.session_idle_seconds + 1)

        response = self.client.run("POST", "/auth/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.set_cookie(SESSION_COOKIE))
        self.assertEqual(self.client.run("GET", "/auth/me").status, 401)


class FeishuNotConfiguredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir("llm-lab-auth-nofeishu-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.transport = support.FakeTransport()
        self.cfg = support.make_config(self.tmp)
        self.app = routes.create_app(self.cfg, transport=self.transport)
        self.app.initialize()
        self.client = support.WsgiClient(self.app)

    def test_start_reports_not_configured(self):
        response = self.client.run("GET", "/auth/feishu/start")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.error_code(), "not_configured")

    def test_start_makes_no_outbound_request(self):
        self.client.run("GET", "/auth/feishu/start")
        self.assertEqual(self.transport.requests, [])

    def test_providers_advertise_feishu_as_unavailable(self):
        payload = self.client.run("GET", "/auth/csrf").json()["data"]["providers"]
        self.assertFalse(payload["feishu"])

    def test_callback_without_configuration_is_a_fixed_local_redirect(self):
        response = self.client.run("GET", "/auth/feishu/callback?code=x&state=y")
        self.assertEqual(response.status, 302)
        self.assertTrue(response.location.startswith("/login?"))


class FeishuLoginTests(HttpTestCase):
    def test_start_redirects_to_the_official_authorization_endpoint(self):
        response, state = self.start_feishu()
        parsed = urllib.parse.urlsplit(response.location)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         "https://accounts.feishu.cn/open-apis/authen/v1/authorize")
        self.assertEqual(query["client_id"], support.SYNTHETIC_APP_ID)
        self.assertEqual(query["redirect_uri"], support.SYNTHETIC_REDIRECT)
        self.assertEqual(query["state"], state)

    def test_start_performs_no_token_exchange(self):
        self.start_feishu()
        self.assertEqual(self.transport.requests, [])

    def test_start_never_leaks_the_app_secret(self):
        response, _ = self.start_feishu()
        self.assertNotIn(support.SYNTHETIC_APP_SECRET, response.location)

    def test_callback_completes_and_redirects_to_the_fixed_local_path(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?code=one-time-code&state={state}")
        self.assertEqual(response.status, 302)
        self.assertEqual(response.location, self.cfg.post_login_path)

    def test_callback_issues_a_hardened_session_cookie(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?code=one-time-code&state={state}")
        cookie = response.set_cookie(SESSION_COOKIE)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_feishu_user_lands_in_the_session_as_a_plain_member(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu()
        self.client.run("GET", f"/auth/feishu/callback?code=one-time-code&state={state}")
        payload = self.client.run("GET", "/auth/me").json()["data"]
        self.assertEqual(payload["user"]["role"], store.ROLE_MEMBER)
        self.assertFalse(payload["user"]["is_platform_admin"])
        self.assertEqual(payload["projects"], [])

    def test_the_user_credential_never_reaches_the_client(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?code=one-time-code&state={state}")
        blob = response.body + str(response.set_cookies).encode()
        self.assertNotIn(b"u-synthetic-user-token", blob)
        self.assertNotIn(b"t-synthetic-app-token", blob)

    def test_state_can_only_be_used_once(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu()
        first = self.client.run("GET", f"/auth/feishu/callback?code=one-time-code&state={state}")
        self.assertEqual(first.status, 302)
        second = self.client.run("GET", f"/auth/feishu/callback?code=one-time-code&state={state}")
        self.assertEqual(second.status, 302)
        self.assertIn("error=invalid_state", second.location)

    def test_unknown_state_is_refused(self):
        response = self.client.run(
            "GET", f"/auth/feishu/callback?code=c&state={store.new_secret_id()}"
        )
        self.assertTrue(response.location.startswith("/login?"))
        self.assertIn("error=", response.location)

    def test_missing_code_is_refused(self):
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?state={state}")
        self.assertIn("error=invalid_request", response.location)

    def test_provider_denial_is_surfaced_without_details(self):
        _, state = self.start_feishu()
        response = self.client.run(
            "GET", f"/auth/feishu/callback?error=access_denied&state={state}"
        )
        self.assertIn("error=access_denied", response.location)

    def test_unknown_tenant_is_refused_and_no_session_is_issued(self):
        self.transport.queue(*support.success_script(tenant_key="tenant_not_allowed"))
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertIn("error=access_denied", response.location)
        self.assertIsNone(response.set_cookie(SESSION_COOKIE))

    def test_provider_timeout_is_mapped_to_temporarily_unavailable(self):
        self.transport.queue(TimeoutError("upstream timed out"))
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertIn("error=temporarily_unavailable", response.location)

    def test_provider_transport_failure_is_mapped(self):
        self.transport.queue(OSError("connection refused"))
        _, state = self.start_feishu()
        response = self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertIn("error=temporarily_unavailable", response.location)

    def test_error_redirect_never_leaves_the_site(self):
        for location in (
            self.client.run("GET", "/auth/feishu/callback?code=c&state=bad").location,
            self.client.run("GET", "/auth/feishu/callback").location,
        ):
            with self.subTest(location=location):
                self.assertTrue(location.startswith("/"))
                self.assertFalse(location.startswith("//"))

    def test_provider_that_raises_is_mapped_by_the_route(self):
        raising = support.FakeFeishuProvider(raises=errors.ProviderUnavailable())
        app, client = self.new_app()
        app.provider = raising
        _, state = self.start_feishu(client=client)
        response = client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertIn("error=temporarily_unavailable", response.location)

    def test_open_redirect_attempt_is_ignored(self):
        response = self.client.run(
            "GET", "/auth/feishu/start?next=" + urllib.parse.quote("https://evil.example/", safe="")
        )
        self.assertEqual(response.status, 302)
        self.assertTrue(response.location.startswith("https://accounts.feishu.cn/"))

        self.transport.queue(*support.success_script())
        _, state = self.start_feishu(query="?next=" + urllib.parse.quote("https://evil.example/", safe=""))
        done = self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertEqual(done.location, self.cfg.post_login_path)

    def test_local_next_path_is_honoured(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu(query="?next=%2Fdashboard")
        done = self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        self.assertEqual(done.location, "/dashboard")

    def test_feishu_display_name_admin_does_not_grant_administrator(self):
        for name in ("admin", "admin@example.com", "Administrator"):
            with self.subTest(name=name):
                self.transport.queue(*support.success_script(name=name, open_id=f"ou_{abs(hash(name))}"))
                _, state = self.start_feishu()
                self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
                payload = self.client.run("GET", "/auth/me").json()["data"]
                self.assertEqual(payload["user"]["role"], store.ROLE_MEMBER)
                self.assertFalse(payload["user"]["is_platform_admin"])


class PrivilegeTests(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()

    def test_member_session_fails_an_admin_guard(self):
        self.transport.queue(*support.success_script())
        _, state = self.start_feishu()
        self.client.run("GET", f"/auth/feishu/callback?code=c&state={state}")
        session = self.app.service.current_session(self.client.cookies[SESSION_COOKIE])
        with self.assertRaises(errors.Forbidden):
            self.app.authorize(session, require_admin=True)

    def test_admin_session_passes_the_admin_guard(self):
        self.login_admin()
        session = self.app.service.current_session(self.client.cookies[SESSION_COOKIE])
        self.assertTrue(self.app.authorize(session, require_admin=True).user.is_admin)

    def test_no_route_can_grant_a_role_or_a_project(self):
        for method, path in routes.ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.assertNotIn("grant", path)
                self.assertNotIn("admin", path)
                self.assertNotIn("role", path)

    def test_only_the_documented_routes_exist(self):
        self.assertEqual(
            {(method, path) for method, path in routes.ROUTES},
            {
                ("GET", "/auth/csrf"),
                ("POST", "/auth/local/login"),
                ("GET", "/auth/feishu/start"),
                ("GET", "/auth/feishu/callback"),
                ("GET", "/auth/me"),
                ("POST", "/auth/logout"),
                # Scoped CLI tokens (Issue #45). Documented in the module
                # docstring's contract table; there is deliberately no
                # introspection route.
                ("GET", "/auth/tokens"),
                ("POST", "/auth/tokens"),
                ("POST", "/auth/tokens/revoke"),
                ("POST", "/auth/tokens/rotate"),
                ("POST", "/auth/tokens/revoke-all"),
            },
        )


class TransportHygieneTests(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()

    def test_oversized_body_is_refused_before_parsing(self):
        response = self.client.run(
            "POST",
            "/auth/local/login",
            raw_body=b"x" * (self.cfg.max_request_bytes + 100),
            headers={"X-CSRF-Token": self.csrf_token()},
        )
        self.assertEqual(response.status, 413)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.client.run("GET", "/auth/nope").status, 404)

    def test_responses_are_marked_non_cacheable(self):
        for path in ("/auth/csrf", "/auth/me"):
            with self.subTest(path=path):
                headers = self.client.run("GET", path).headers
                self.assertEqual(headers.get("cache-control"), "no-store")

    def test_responses_are_not_sniffable(self):
        headers = self.client.run("GET", "/auth/csrf").headers
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")

    def test_json_content_type_is_declared(self):
        headers = self.client.run("GET", "/auth/csrf").headers
        self.assertIn("application/json", headers.get("content-type", ""))

    def test_session_cookie_is_absent_from_the_database_ciphertext_free_view(self):
        self.login_admin()
        raw = self.client.cookies[SESSION_COOKIE]
        blob = (self.tmp / "auth.sqlite3").read_bytes()
        self.assertNotIn(raw.encode(), blob)

    def test_administrator_credential_is_not_in_the_repository_tree(self):
        """The hash file lives under the state dir, which is outside the repo."""
        self.assertNotEqual(
            self.cfg.admin_hash_file.parent, self.cfg.state_dir.parent / "backend"
        )
        self.assertTrue(
            str(self.cfg.admin_hash_file).startswith(str(self.cfg.state_dir))
        )


class ServiceUnitTests(HttpTestCase):
    def test_public_identity_is_minimal(self):
        user = self.app.service.db.ensure_local_user("admin")
        payload = self.app.service.public_identity(user)
        self.assertEqual(
            set(payload), {"id", "display_name", "role", "auth_source", "is_platform_admin"}
        )

    def test_audit_trail_records_outcomes_without_credentials(self):
        self.bootstrap_admin()
        self.login_admin()
        events = self.app.service.db.list_audit_events()
        self.assertTrue(events)
        joined = repr(events)
        self.assertNotIn(SYNTHETIC_ADMIN_PASSWORD, joined)
        self.assertNotIn("scrypt$", joined)

    def test_disabled_local_admin_is_reported_as_disabled(self):
        self.bootstrap_admin()
        bootstrap.disable_admin_login(
            self.cfg, read_password=ScriptedReader(SYNTHETIC_ADMIN_PASSWORD)
        )
        response = self.login_admin()
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "account_disabled")

    def test_admin_credential_status_is_secret_free(self):
        self.bootstrap_admin()
        report = local_admin.AdminCredentialStore(
            self.cfg.admin_hash_file, username="admin", enforce_permissions=False
        ).status()
        encoded = self.cfg.admin_hash_file.read_text(encoding="utf-8").strip()
        rendered = repr(report)
        # The operator-facing report must carry no fragment of what is stored:
        # not the full encoding, not its parameter string, not its salt or
        # digest. Naming the algorithm is fine — that is public, it is in this
        # repository's source — but `scrypt$...` is credential material.
        self.assertNotIn(encoded, rendered)
        self.assertNotIn("scrypt$", rendered)
        self.assertNotIn(encoded.split("$")[1], rendered)  # n=...,r=...,p=...
        self.assertNotIn(encoded.rsplit("$", 1)[1], rendered)  # digest
        self.assertNotIn(SYNTHETIC_ADMIN_PASSWORD, rendered)


if __name__ == "__main__":
    unittest.main()
