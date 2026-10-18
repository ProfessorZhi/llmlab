"""匿名 Linux 登录面上的一对性质：**状态只报计数，请求不能自报身份**。

``GET /auth/linux/status`` 是匿名的 —— 它在登录之前就要能回答「这条路此刻通不通」。
匿名 + 读 = 最容易长出「顺手多给一点」的地方。两条性质一起守：

1. **请求不能自报身份。** ``?uid=0`` / ``?role=x`` / ``?owner=x`` 不是「提示」，
   是一次断言：它们不改变任何东西，但必须被**拒绝**（400
   ``identity_assertion_rejected``），而不是被忽略。忽略意味着下游某一层可以把它
   捡起来用，而代码里没有一处说过「不」。
2. **答复只报计数，不报名单。** 一个 count 对运维够用；一串账号名是一个目标清单。
   这一条与 IAM PR #100 的 ``test_the_status_endpoint_is_anonymous_and_names_no_account``
   是同一个契约的两侧（那边验 auth 层，这边验 BFF 路由），所以断言刻意保持一致。

``backend.auth`` 的 write preflight 也会查字段，但 ``/auth/linux/status`` 是**读**
路由、根本不走 preflight；这道门只能在本层的路由表里。
"""

from __future__ import annotations

import json
import tempfile
import unittest

from tests.bff import support as S

#: 匿名状态下必须被拒的字段。``0``/``platform_admin`` 是「最坏的那个取值」。
FORGED_QUERY_FIELDS = (
    ("uid", "0"),
    ("role", "platform_admin"),
    ("roles", "platform_admin"),
    ("owner", "linux:synthetic-host:0"),
    ("principal", "linux:synthetic-host:0"),
    ("is_admin", "1"),
)


class LinuxStatusIdentityAssertionTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.anonymous = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    # --- 被拒 ------------------------------------------------------------

    def test_a_query_field_that_names_an_identity_is_400(self):
        for field, value in FORGED_QUERY_FIELDS:
            with self.subTest(field=field):
                response = self.anonymous.run(
                    "GET", f"/auth/linux/status?{field}={value}"
                )
                self.assertEqual(response.status, 400, response.json())
                self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_the_refusal_happens_even_though_nothing_would_have_used_the_field(self):
        """被拒的理由不是「用了它」，而是「请求试图指定身份」。

        这一条把「读了不用」这种实现明确排除在外：如果实现改成先忽略、再在别处
        断言，这条测试仍然成立 —— 而它成立的**方式**是 400，不是 200。
        """
        response = self.anonymous.run("GET", "/auth/linux/status?uid=0")
        self.assertEqual(response.status, 400)
        self.assertNotIn(b'"enabled"', response.body)

    # --- 未受影响的正向面 -------------------------------------------------

    def test_the_unparameterised_status_stays_200(self):
        response = self.anonymous.run("GET", "/auth/linux/status")
        self.assertEqual(response.status, 200, response.json())

    def test_the_status_reports_counts_and_names_no_account(self):
        response = self.anonymous.run("GET", "/auth/linux/status")
        self.assertEqual(response.status, 200)
        data = response.json()["data"]

        # 计数在，名单不在（与 IAM #100 的同名断言一致）。
        self.assertEqual(data["allowlist_size"], 4)
        self.assertNotIn("allowed_users", data)
        self.assertNotIn("platform_admins", data)
        for field, value in data.items():
            with self.subTest(field=field):
                self.assertNotIsInstance(value, (list, tuple, set, frozenset))

        # 整份**序列化后**的答复里不出现任何账号名。
        # ``broker_socket`` 是部署方给的路径、可能含用户名，项目自己的
        # ``tests/auth_linux/test_service.py`` 也把它排除在同类扫描之外，这里沿用
        # 同一个口径（见 PR 说明里的 handoff）。
        rendered = json.dumps(
            {k: v for k, v in data.items() if k != "broker_socket"}, ensure_ascii=False
        )
        for name in (
            S.ADMIN_NAME,
            S.MEMBER_NAME,
            S.OTHER_MEMBER_NAME,
            S.REAL_ACCOUNT_NAME,
        ):
            with self.subTest(name=name):
                self.assertNotIn(name, rendered)


class LinuxLoginIdentityAssertionTests(unittest.TestCase):
    """登录路由同样不能靠字段自报身份 —— 而且被拒时不能发出会话。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def _login(self, *, path="/auth/linux/login", **overrides):
        payload = {"login_name": S.MEMBER_NAME, "password": S.GOOD_PASSWORD}
        payload.update(overrides)
        return self.client.run(
            "POST", path, payload=payload, headers={"X-CSRF-Token": S.csrf(self.client)}
        )

    def test_a_forged_field_in_the_login_body_is_400_and_mints_no_session(self):
        for field, value in (
            ("role", "platform_admin"),
            ("uid", 0),
            ("owner", "linux:synthetic-host:0"),
            ("principal", "linux:synthetic-host:0"),
        ):
            with self.subTest(field=field):
                response = self._login(**{field: value})
                self.assertEqual(response.status, 400, response.json())
                self.assertEqual(response.error_code(), "identity_assertion_rejected")
                self.assertIsNone(
                    response.set_cookie(self.h.app.cfg.cookie_name),
                    "a refused login must not hand out a session cookie",
                )

    def test_a_forged_field_in_the_login_query_is_400(self):
        response = self._login(path="/auth/linux/login?uid=0")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_the_normal_login_still_works(self):
        """``login_name`` 不是身份断言字段 —— 它是被验证的凭据标识符。"""
        response = self._login()
        self.assertEqual(response.status, 200, response.json())
        self.assertIsNotNone(response.set_cookie(self.h.app.cfg.cookie_name))

    def test_the_identity_field_guard_does_not_change_credential_failures(self):
        response = self._login(password=S.WRONG_PASSWORD)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "invalid_credentials")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
