"""提权路由的 whole-request 身份断言守卫（BFF #79 / IAM #100 交接面）。

IAM 的 write preflight 做同样的字段检查，但它**故意不覆盖 BFF-only 路由**
（见 PR #100 的 ``IDENTITY_GUARDED_PATHS`` 注释：「BFF reuses this preflight for
CSRF/Origin. Its own routes keep their own field policy」）。``/auth/admin/enter``
正是其中之一，所以这道门只能在本层。

**本文件写的是「先失败」的那一半**：修复之前，一个带着正确管理口令、外加
``role=platform_admin`` 的请求会得到 **200**，字段被静默忽略。静默忽略不是安全：
它意味着下游任意一层（中间件、日志处理器、一次重构）都可以把那个字段重新捡起来，
而代码里没有任何一处会说「不」。

因此每条断言都成对：**被拒**（400 + ``identity_assertion_rejected``）**且没有发生**
（会话没有提权）。只验状态码不验副作用，会漏掉「400 了但已经提权」这种最坏情况。
"""

from __future__ import annotations

import tempfile
import unittest

from tests.bff import support as S

#: 修复前会被静默忽略、现在必须 400 的字段。取值都取「最坏的那个」：
#: 一个冒充管理员的值，比一个空值更能暴露「读到了会怎样」。
FORGED_FIELDS = (
    ("role", "platform_admin"),
    ("roles", ["platform_admin"]),
    ("uid", 0),
    ("owner", "linux:synthetic-host:0"),
    ("principal", "linux:synthetic-host:0"),
    ("is_admin", True),
    ("admin", True),
)


class AdminEnterIdentityAssertionTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def _enter(self, client, **fields):
        payload = {"password": S.ADMIN_MODE_PASSWORD}
        payload.update(fields)
        return client.run(
            "POST",
            "/auth/admin/enter",
            payload=payload,
            headers={"X-CSRF-Token": S.csrf(client)},
        )

    def _elevated(self, client) -> bool:
        return S.admin_status(client).json()["data"]["admin_mode"]["elevated"]

    # --- 被拒 ------------------------------------------------------------

    def test_a_forged_identity_field_in_the_body_is_400_with_the_iam_token(self):
        for field, value in FORGED_FIELDS:
            with self.subTest(field=field):
                client = self.h.client("alice")
                response = self._enter(client, **{field: value})
                self.assertEqual(response.status, 400, response.json())
                self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_a_forged_identity_field_in_the_query_is_400_too(self):
        """``?role=`` 与 body 里的 ``role`` 是同一类东西：都以字段名断言身份。"""
        for field, value in (("uid", "0"), ("role", "platform_admin"), ("owner", "x")):
            with self.subTest(field=field):
                client = self.h.client("alice")
                response = client.run(
                    "POST",
                    f"/auth/admin/enter?{field}={value}",
                    payload={"password": S.ADMIN_MODE_PASSWORD},
                    headers={"X-CSRF-Token": S.csrf(client)},
                )
                self.assertEqual(response.status, 400, response.json())
                self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_a_refused_attempt_does_not_elevate_the_session(self):
        """状态码之后的那一半：被拒的请求**没有发生任何事**。

        这是本文件最重要的一条。一个 400 但会话已经提权的实现，会让「拒绝」变成
        一句空话 —— 而且这种缺陷在只看状态码的测试里完全不可见。
        """
        client = self.h.client("alice")
        self.assertFalse(self._elevated(client))
        response = self._enter(client, role="platform_admin")
        self.assertEqual(response.status, 400)
        self.assertFalse(
            self._elevated(client),
            "a refused identity assertion must not have elevated the session",
        )

    def test_the_rejection_body_does_not_echo_the_forged_value(self):
        client = self.h.client("alice")
        response = self._enter(client, role="platform_admin")
        self.assertEqual(response.status, 400)
        self.assertNotIn(b"platform_admin", response.body)

    # --- 未受影响的正向面 -------------------------------------------------

    def test_the_normal_elevation_still_works(self):
        client = self.h.client("alice")
        response = self._enter(client)
        self.assertEqual(response.status, 200, response.json())
        self.assertTrue(self._elevated(client))

    def test_a_wrong_password_is_still_refused_the_old_way(self):
        """身份守卫不改变口令错误的语义：那仍然是「没提权」，不是 400 字段问题。"""
        client = self.h.client("alice")
        response = client.run(
            "POST",
            "/auth/admin/enter",
            payload={"password": "synthetic-wrong"},
            headers={"X-CSRF-Token": S.csrf(client)},
        )
        self.assertNotEqual(response.status, 200)
        self.assertFalse(self._elevated(client))

    def test_identity_headers_are_still_refused(self):
        """字段守卫没有削弱既有的**请求头**守卫。"""
        client = self.h.client("alice")
        response = client.run(
            "POST",
            "/auth/admin/enter",
            payload={"password": S.ADMIN_MODE_PASSWORD},
            headers={"X-CSRF-Token": S.csrf(client), "X-Role": "platform_admin"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")
        self.assertFalse(self._elevated(client))


class AdminSiblingRoutesTests(unittest.TestCase):
    """同一个声明集合里的 ``/auth/admin/{status,exit}`` 也拒绝自报身份字段。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_admin_status_refuses_a_field_assertion(self):
        client = self.h.client("alice")
        response = client.run("GET", "/auth/admin/status?uid=0")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_admin_exit_refuses_a_field_assertion(self):
        client = self.h.client("alice")
        response = client.run(
            "POST",
            "/auth/admin/exit?role=platform_admin",
            payload={},
            headers={"X-CSRF-Token": S.csrf(client)},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_the_unparameterised_status_is_unchanged(self):
        """不新增字段的正向路径一个字都没变。"""
        client = self.h.client("alice")
        response = client.run("GET", "/auth/admin/status")
        self.assertEqual(response.status, 200)
        self.assertFalse(S.admin_mode_of(response)["elevated"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
