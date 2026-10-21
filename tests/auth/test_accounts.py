"""HTTP tests for platform accounts (Issue #17).

Accounts are independent of Linux: nothing here touches PAM or the host user
database. Every test drives the real WSGI callable, so CSRF, Origin, the
identity-field guard, the session cookie and the error taxonomy are all on the
path. Passwords below are fictional and exist only inside this test process.
"""

import sqlite3
import unittest

from backend.auth import accounts, routes
from tests.auth import support
from tests.auth.test_routes import SESSION_COOKIE, HttpTestCase

MEMBER_PASSWORD = "synthetic-member-pass-1"
ADMIN_ROUTES = [
    ("GET", "/auth/admin/users", None),
    ("POST", "/auth/admin/users", {"username": "x1"}),
    ("POST", "/auth/admin/users/reset-password", {"user_id": "account:00"}),
    ("POST", "/auth/admin/users/role", {"user_id": "account:00", "role": "admin"}),
    ("POST", "/auth/admin/users/linux", {"user_id": "account:00", "linux_user": "x"}),
    ("POST", "/auth/admin/users/status", {"user_id": "account:00", "status": "disabled"}),
]


class AccountTestCase(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap_admin()

    def post(self, path, payload, client=None):
        client = client or self.client
        return client.run("POST", path, payload=payload, headers={"X-CSRF-Token": self.csrf_token(client)})

    def admin_client(self):
        self.client.cookies.clear()
        self.assertEqual(self.login_admin().status, 200)
        return self.client

    def create(self, username, **extra):
        response = self.post("/auth/admin/users", {"username": username, **extra})
        self.assertEqual(response.status, 201, response.body)
        data = response.json()["data"]
        return data["user"], data["temporary_password"]

    def account_login(self, username, password, client=None):
        return self.post("/auth/account/login", {"username": username, "password": password}, client)

    def activate(self, username, **extra):
        """Create an account and replace its one-time password; returns (user, client)."""
        user, temporary = self.create(username, **extra)
        member = support.WsgiClient(self.app)
        self.assertEqual(self.account_login(username, temporary, member).status, 200)
        changed = self.post(
            "/auth/account/password",
            {"current_password": temporary, "new_password": MEMBER_PASSWORD},
            member,
        )
        self.assertEqual(changed.status, 200, changed.body)
        return user, member


class AdminManagementTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.admin_client()

    def test_create_returns_a_one_time_password_and_never_a_hash(self):
        user, temporary = self.create("zhang", display_name="张伟", linux_user="zhang")
        self.assertRegex(temporary, r"^[a-z2-9]{4}(-[a-z2-9]{4}){3}$")
        self.assertTrue(user["id"].startswith("account:"))
        self.assertEqual(user["username"], "zhang")
        self.assertEqual(user["linux_user"], "zhang")
        self.assertEqual(user["role"], "member")
        self.assertTrue(user["must_change_password"])
        listed = self.client.run("GET", "/auth/admin/users")
        self.assertEqual(listed.status, 200)
        self.assertNotIn(b"scrypt", listed.body)
        self.assertNotIn(temporary.encode(), listed.body)

    def test_principal_id_is_not_derived_from_the_username(self):
        first, _ = self.create("same")
        self.post("/auth/admin/users/status", {"user_id": first["id"], "status": "disabled"})
        second, _ = self.create("other")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotIn("same", first["id"])

    def test_duplicate_and_malformed_usernames_are_refused(self):
        self.create("dup")
        self.assertEqual(self.post("/auth/admin/users", {"username": "DUP"}).status, 400)
        for bad in ("", "a", "1abc", "has space", "x" * 40, "../etc"):
            with self.subTest(username=bad):
                self.assertEqual(self.post("/auth/admin/users", {"username": bad}).status, 400)

    def test_bad_linux_label_and_role_are_refused(self):
        self.assertEqual(self.post("/auth/admin/users", {"username": "a1", "linux_user": "root;rm"}).status, 400)
        self.assertEqual(self.post("/auth/admin/users", {"username": "a2", "role": "platform_admin"}).status, 400)

    def test_role_linux_and_status_changes_are_reflected(self):
        user, _ = self.create("li")
        r = self.post("/auth/admin/users/role", {"user_id": user["id"], "role": "admin"})
        self.assertEqual(r.json()["data"]["user"]["role"], "admin")
        r = self.post("/auth/admin/users/linux", {"user_id": user["id"], "linux_user": ""})
        self.assertIsNone(r.json()["data"]["user"]["linux_user"])
        r = self.post("/auth/admin/users/status", {"user_id": user["id"], "status": "disabled"})
        self.assertEqual(r.json()["data"]["user"]["status"], "disabled")
        self.assertEqual(self.post("/auth/admin/users/status", {"user_id": user["id"], "status": "gone"}).status, 400)

    def test_unknown_target_is_a_client_error(self):
        r = self.post("/auth/admin/users/reset-password", {"user_id": "account:missing"})
        self.assertEqual(r.status, 400)

    def test_identity_fields_in_a_query_string_are_still_refused(self):
        r = self.client.run("GET", "/auth/admin/users?user_id=x")
        self.assertEqual(r.error_code(), "identity_assertion_rejected")

    def test_target_fields_are_only_allowed_on_their_own_route(self):
        r = self.post("/auth/admin/users/role", {"user_id": "account:00", "role": "admin", "linux_user": "x"})
        self.assertEqual(r.error_code(), "identity_assertion_rejected")
        r = self.post("/auth/account/password", {"current_password": "a", "new_password": "b" * 12, "user_id": "x"})
        self.assertEqual(r.error_code(), "identity_assertion_rejected")


class AuthorizationTests(AccountTestCase):
    def test_anonymous_callers_are_refused(self):
        for method, path, payload in ADMIN_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                self.client.cookies.clear()
                if method == "GET":
                    r = self.client.run(method, path)
                else:
                    r = self.post(path, payload)
                self.assertEqual(r.status, 401)

    def test_member_sessions_are_refused_even_with_a_matching_linux_label(self):
        self.admin_client()
        _user, member = self.activate("fjd", linux_user="fjd")
        for method, path, payload in ADMIN_ROUTES:
            with self.subTest(route=f"{method} {path}"):
                r = member.run(method, path) if method == "GET" else self.post(path, payload, member)
                self.assertEqual(r.status, 403)

    def test_two_users_with_linux_like_names_stay_separate(self):
        self.admin_client()
        a, client_a = self.activate("alice", linux_user="alice")
        b, client_b = self.activate("bob", linux_user="alice")
        me_a = client_a.run("GET", "/auth/me").json()["data"]["user"]
        me_b = client_b.run("GET", "/auth/me").json()["data"]["user"]
        self.assertEqual(me_a["id"], a["id"])
        self.assertEqual(me_b["id"], b["id"])
        self.assertNotEqual(me_a["id"], me_b["id"])

    def test_admin_cannot_demote_or_disable_themself(self):
        self.admin_client()
        promoted, member = self.activate("boss")
        self.post("/auth/admin/users/role", {"user_id": promoted["id"], "role": "admin"})
        r = self.post("/auth/admin/users/role", {"user_id": promoted["id"], "role": "member"}, member)
        self.assertEqual(r.status, 403)
        r = self.post("/auth/admin/users/status", {"user_id": promoted["id"], "status": "disabled"}, member)
        self.assertEqual(r.status, 403)
        # …but a promoted account can manage others.
        self.assertEqual(self.post("/auth/admin/users", {"username": "newbie"}, member).status, 201)


class LoginTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.admin_client()
        self.user, self.member = self.activate("wang", display_name="王磊")

    def test_login_sets_a_session_and_me_reports_the_account(self):
        client = support.WsgiClient(self.app)
        r = self.account_login("WANG", MEMBER_PASSWORD, client)
        self.assertEqual(r.status, 200)
        self.assertIsNotNone(r.set_cookie(SESSION_COOKIE))
        me = client.run("GET", "/auth/me").json()["data"]["user"]
        self.assertEqual(me["username"], "wang")
        self.assertEqual(me["auth_source"], accounts.SOURCE_PLATFORM)
        self.assertFalse(me["must_change_password"])
        self.assertFalse(me["is_platform_admin"])

    def test_failures_are_generic(self):
        unknown = self.account_login("nobody", "whatever-123")
        wrong = self.account_login("wang", "not-the-password")
        self.post("/auth/admin/users/status", {"user_id": self.user["id"], "status": "disabled"})
        disabled = self.account_login("wang", MEMBER_PASSWORD, support.WsgiClient(self.app))
        for r in (unknown, wrong, disabled):
            self.assertEqual(r.status, 401)
            self.assertEqual(r.error_code(), "invalid_credentials")
        self.assertEqual(unknown.json()["error"], wrong.json()["error"])

    def test_repeated_failures_are_rate_limited(self):
        for _ in range(self.cfg.login_max_failures):
            self.account_login("wang", "wrong-password-x")
        r = self.account_login("wang", MEMBER_PASSWORD)
        self.assertEqual(r.status, 429)
        self.clock.advance(self.cfg.lockout_seconds + self.cfg.login_window_seconds + 1)
        self.assertEqual(self.account_login("wang", MEMBER_PASSWORD).status, 200)

    def test_login_requires_csrf_and_same_origin(self):
        client = support.WsgiClient(self.app)
        r = client.run("POST", "/auth/account/login", payload={"username": "wang", "password": MEMBER_PASSWORD})
        self.assertEqual(r.error_code(), "csrf_rejected")
        r = client.run(
            "POST", "/auth/account/login",
            payload={"username": "wang", "password": MEMBER_PASSWORD},
            headers={"X-CSRF-Token": self.csrf_token(client), "Origin": "https://evil.example"},
        )
        self.assertEqual(r.error_code(), "csrf_rejected")

    def test_disabling_revokes_live_sessions(self):
        self.assertEqual(self.member.run("GET", "/auth/me").status, 200)
        self.post("/auth/admin/users/status", {"user_id": self.user["id"], "status": "disabled"})
        self.assertIn(self.member.run("GET", "/auth/me").status, (401, 403))


class PasswordTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.admin_client()

    def test_one_time_password_session_can_only_change_the_password(self):
        # An admin-role account proves the gate: its role would pass the admin
        # check, so only the one-time password can be what refuses it.
        _user, temporary = self.create("chen", role="admin")
        client = support.WsgiClient(self.app)
        self.assertEqual(self.account_login("chen", temporary, client).status, 200)
        me = client.run("GET", "/auth/me").json()["data"]["user"]
        self.assertTrue(me["must_change_password"])
        r = client.run("GET", "/auth/admin/users")
        self.assertEqual(r.status, 403)
        self.assertEqual(r.error_code(), "password_change_required")
        self.post("/auth/account/password", {"current_password": temporary, "new_password": MEMBER_PASSWORD}, client)
        self.assertEqual(client.run("GET", "/auth/admin/users").status, 200)

    def test_change_rotates_the_session_and_revokes_others(self):
        user, temporary = self.create("zhao")
        first = support.WsgiClient(self.app)
        second = support.WsgiClient(self.app)
        self.account_login("zhao", temporary, first)
        self.account_login("zhao", temporary, second)
        old_cookie = first.cookies[SESSION_COOKIE]
        r = self.post("/auth/account/password", {"current_password": temporary, "new_password": MEMBER_PASSWORD}, first)
        self.assertEqual(r.status, 200)
        self.assertNotEqual(first.cookies[SESSION_COOKIE], old_cookie)
        self.assertFalse(r.json()["data"]["user"]["must_change_password"])
        self.assertEqual(first.run("GET", "/auth/me").status, 200)
        self.assertEqual(second.run("GET", "/auth/me").status, 401)
        self.assertEqual(self.account_login("zhao", temporary).status, 401)

    def test_weak_reused_or_wrong_current_passwords_are_refused(self):
        _user, temporary = self.create("sun")
        client = support.WsgiClient(self.app)
        self.account_login("sun", temporary, client)
        short = self.post("/auth/account/password", {"current_password": temporary, "new_password": "short"}, client)
        self.assertEqual(short.status, 400)
        same = self.post("/auth/account/password", {"current_password": temporary, "new_password": temporary}, client)
        self.assertEqual(same.status, 400)
        wrong = self.post("/auth/account/password", {"current_password": "nope-nope-nope", "new_password": MEMBER_PASSWORD}, client)
        self.assertEqual(wrong.error_code(), "invalid_credentials")

    def test_reset_issues_a_new_one_time_password_and_revokes_sessions(self):
        user, member = self.activate("qian")
        r = self.post("/auth/admin/users/reset-password", {"user_id": user["id"]})
        temporary = r.json()["data"]["temporary_password"]
        self.assertTrue(r.json()["data"]["user"]["must_change_password"])
        self.assertEqual(member.run("GET", "/auth/me").status, 401)
        self.assertEqual(self.account_login("qian", MEMBER_PASSWORD).status, 401)
        self.assertEqual(self.account_login("qian", temporary).status, 200)

    def test_local_admin_has_no_platform_password_to_change(self):
        r = self.post("/auth/account/password", {"current_password": "x" * 12, "new_password": "y" * 12})
        self.assertEqual(r.status, 403)


class StorageTests(AccountTestCase):
    def test_only_hashes_reach_the_database(self):
        self.admin_client()
        _user, temporary = self.create("zhou")
        conn = sqlite3.connect(str(self.cfg.state_db_path))
        try:
            dump = "\n".join(conn.iterdump())
        finally:
            conn.close()
        self.assertNotIn(temporary, dump)
        self.assertIn("scrypt$", dump)

    def test_route_table_lists_every_target_field_route(self):
        paths = {path for _method, path in routes.ROUTES}
        self.assertTrue(set(routes.TARGET_IDENTITY_FIELDS) <= paths)


if __name__ == "__main__":
    unittest.main()
