"""OAuth browser-binding tests (P0).

The attack these tests exist for: an OAuth ``state`` alone protects against
*replay*, not against a *valid callback URL being opened by somebody else*. An
attacker who obtains a victim's authorisation URL (or simply races them on a
shared NAT) can hand it to a second browser; if the server accepts the callback
on the strength of ``state`` alone, the second browser is the one that ends up
holding the session. That is login CSRF / session confusion: the victim then
operates inside an account the attacker can see.

The defence under test: ``GET /auth/feishu/start`` mints an independent,
unpredictable per-flow ``browser_nonce``, hands it to *that* browser only, in a
short-lived ``HttpOnly; Secure; SameSite=Lax; Path=/`` cookie, and stores only a
digest of it against the ``state``. The callback must present the same nonce
back, it is compared in constant time, and only then is the state consumed and
the upstream code exchanged.

Every test here drives the real WSGI callable, so the cookie is carried by a
real cookie jar rather than being asserted against hand-built environ dicts.
No test opens a socket: the provider is the synthetic one from
:mod:`tests.auth.support`.
"""

import shutil
import sqlite3
import unittest
import urllib.parse

from backend.auth import routes
from tests.auth import support
from tests.auth.test_store import FakeClock

SESSION_COOKIE = "llm_lab_session"


class BindingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir("llm-lab-auth-binding-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.transport = support.FakeTransport()
        self.cfg = support.make_config(self.tmp, **support.feishu_env())
        self.binding = self.cfg.oauth_binding_cookie_name
        self.app = routes.create_app(self.cfg, transport=self.transport, clock=self.clock)
        self.app.initialize()
        # Two browsers behind the same NAT: identical REMOTE_ADDR, separate
        # cookie jars. They are the whole point of the P0.
        self.browser = support.WsgiClient(self.app)
        self.other = support.WsgiClient(self.app)
        self.assertEqual(self.browser.remote_addr, self.other.remote_addr)

    # --- helpers -------------------------------------------------------------

    def start(self, client=None, query=""):
        client = client or self.browser
        response = client.run("GET", "/auth/feishu/start" + query)
        self.assertEqual(response.status, 302)
        parsed = urllib.parse.urlsplit(response.location)
        state = dict(urllib.parse.parse_qsl(parsed.query))["state"]
        return response, state

    def callback(self, client, state, code="one-time-code"):
        return client.run(
            "GET", f"/auth/feishu/callback?code={code}&state={state}"
        )

    def assert_refused(self, response):
        self.assertEqual(response.status, 302)
        self.assertIn("error=invalid_state", response.location)
        self.assertIsNone(response.set_cookie(SESSION_COOKIE))

    def rows(self, sql, params=()):
        conn = sqlite3.connect(str(self.cfg.state_db_path))
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class StartSetsAProtectedBindingCookie(BindingTestCase):
    def test_start_issues_a_browser_binding_cookie(self):
        response, _ = self.start()
        cookie = response.set_cookie(self.binding)
        self.assertIsNotNone(cookie, "GET /auth/feishu/start must set a binding cookie")

    def test_binding_cookie_is_hardened(self):
        response, _ = self.start()
        cookie = response.set_cookie(self.binding)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)

    def test_binding_cookie_is_host_only_and_expires_quickly(self):
        response, _ = self.start()
        cookie = response.set_cookie(self.binding)
        # No Domain attribute: the cookie must not leak to sibling subdomains.
        self.assertNotIn("Domain=", cookie)
        # Short, flow-scoped lifetime rather than a durable credential.
        self.assertIn("Max-Age=", cookie)
        max_age = int(cookie.split("Max-Age=", 1)[1].split(";")[0])
        self.assertGreater(max_age, 0)
        self.assertLessEqual(max_age, 3600)

    def test_binding_nonce_carries_more_than_128_bits(self):
        response, _ = self.start()
        cookie = response.set_cookie(self.binding)
        value = cookie.split("=", 1)[1].split(";")[0].strip()
        # secrets.token_urlsafe(n) yields >= n*4/3 characters; 128 bits needs 22.
        self.assertGreaterEqual(len(value), 32)

    def test_binding_nonce_is_not_the_state_and_not_in_the_redirect(self):
        response, state = self.start()
        cookie = response.set_cookie(self.binding)
        nonce = cookie.split("=", 1)[1].split(";")[0].strip()
        self.assertNotEqual(nonce, state)
        # The nonce travels in a cookie only — never in a URL the browser, a
        # proxy log or the provider could observe.
        self.assertNotIn(nonce, response.location)
        self.assertNotIn(nonce.encode(), response.body)

    def test_binding_nonce_is_never_stored_in_plaintext(self):
        response, state = self.start()
        nonce = response.set_cookie(self.binding).split("=", 1)[1].split(";")[0].strip()

        for row in self.rows("SELECT * FROM login_states"):
            for value in tuple(row):
                self.assertNotEqual(str(value), nonce)
        for row in self.rows("SELECT detail FROM audit_events"):
            self.assertNotIn(nonce, str(row["detail"]))

    def test_a_second_start_rotates_the_binding_cookie(self):
        first_cookie = self.start()[0].set_cookie(self.binding)
        second_cookie = self.start()[0].set_cookie(self.binding)
        self.assertNotEqual(first_cookie, second_cookie)


class CallbackRequiresTheSameBrowser(BindingTestCase):
    def test_callback_without_the_binding_cookie_is_refused(self):
        _, state = self.start(self.browser)
        self.assert_refused(self.callback(self.other, state))

    def test_a_foreign_binding_cookie_is_refused(self):
        _, state = self.start(self.browser)
        # The second browser runs its own flow, so it holds a *valid* but
        # unrelated binding cookie.
        self.start(self.other)
        self.assert_refused(self.callback(self.other, state))

    def test_a_tampered_binding_cookie_is_refused(self):
        _, state = self.start()
        self.browser.cookies[self.binding] = "x" * 43
        self.assert_refused(self.callback(self.browser, state))

    def test_a_state_from_an_earlier_flow_does_not_match_the_newer_cookie(self):
        """Starting again rebinds this browser, so the older state goes stale.

        The nonce is bound to the flow, not to the browser identity: once this
        browser has started a second flow its jar holds the second nonce, and
        the first state no longer corresponds to anything it can present.
        """
        _, first_state = self.start()
        self.start()
        self.assert_refused(self.callback(self.browser, first_state))

    def test_a_cookie_is_only_ever_accepted_for_its_own_state(self):
        self.transport.queue(*support.success_script())
        first_start, first_state = self.start()
        _, second_state = self.start()

        # Present the *first* flow's cookie value with the *second* flow's
        # state: a valid-looking pair that must still not be interchangeable.
        nonce = first_start.set_cookie(self.binding).split("=", 1)[1].split(";")[0].strip()
        self.browser.cookies[self.binding] = nonce
        self.assert_refused(self.callback(self.browser, second_state))
        self.assertEqual(self.transport.requests, [])

    def test_an_expired_flow_is_refused(self):
        _, state = self.start()
        self.clock.advance(3600)
        self.assert_refused(self.callback(self.browser, state))

    def test_no_upstream_request_is_made_when_binding_fails(self):
        """Fail closed *before* the authorisation code reaches Feishu."""
        _, state = self.start(self.browser)
        response = self.callback(self.other, state)
        self.assert_refused(response)
        self.assertEqual(self.transport.requests, [])

    def test_a_cookie_less_attempt_does_not_burn_the_state(self):
        """A stranger must not be able to deny the real browser its sign-in."""
        self.transport.queue(*support.success_script())
        _, state = self.start(self.browser)
        self.assert_refused(self.callback(self.other, state))

        completed = self.callback(self.browser, state)
        self.assertEqual(completed.status, 302)
        self.assertEqual(completed.location, self.cfg.post_login_path)
        self.assertIsNotNone(completed.set_cookie(SESSION_COOKIE))


class BindingCookieLifecycle(BindingTestCase):
    def test_the_cookie_is_cleared_after_a_successful_sign_in(self):
        self.transport.queue(*support.success_script())
        _, state = self.start()
        response = self.callback(self.browser, state)
        self.assertIsNotNone(response.set_cookie(self.binding))
        self.assertIn("Max-Age=0", response.set_cookie(self.binding))
        self.assertNotIn(self.binding, self.browser.cookies)

    def test_the_cookie_is_cleared_after_a_refused_callback(self):
        _, state = self.start(self.browser)
        response = self.callback(self.other, state)
        cookie = response.set_cookie(self.binding)
        self.assertIsNotNone(cookie, "a failed callback must also clear the flow cookie")
        self.assertIn("Max-Age=0", cookie)

    def test_the_session_cookie_alone_does_not_complete_a_flow(self):
        """The two cookies are independent: holding a session proves nothing."""
        self.transport.queue(*support.success_script())
        _, state = self.start(self.browser)
        self.callback(self.browser, state)

        self.other.cookies[SESSION_COOKIE] = self.browser.cookies[SESSION_COOKIE]
        _, second_state = self.start(self.other)
        self.start(self.other)  # rotate away from `second_state`
        self.assert_refused(self.callback(self.other, second_state))


class ReplayIsStillRefused(BindingTestCase):
    def test_a_completed_callback_cannot_be_replayed(self):
        self.transport.queue(*support.success_script())
        _, state = self.start()
        first = self.callback(self.browser, state)
        self.assertEqual(first.location, self.cfg.post_login_path)

        second = self.callback(self.browser, state)
        self.assert_refused(second)
        # Nothing was sent upstream a second time.
        self.assertEqual(len(self.transport.requests), 3)


__all__ = [
    "BindingCookieLifecycle",
    "CallbackRequiresTheSameBrowser",
    "ReplayIsStillRefused",
    "StartSetsAProtectedBindingCookie",
]
