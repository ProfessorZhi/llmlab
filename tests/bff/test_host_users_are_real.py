"""受信主体目录里不许出现本机并不存在的人。

``HOST_USERS``（carol / alice / dave / bob，uid 4000-4003）是**测试夹具**：用例需要
一份与机器无关、永远不变的名单，它就该留着。但它一旦被当成**部署**的名单，受信主体
目录里就多了四个虚构的人 —— 共享范围、服务归属、按用户统计全都要问这份目录。

这里钉住的不是「名单长什么样」，而是三件行为：

* ``real_host_users()`` 只回真实存在的账户，uid 落在普通用户区间；
* 部署默认（``resolve_host_users(synthetic=False)``）走的是真实名单，不是夹具；
* ``build_application`` 的 ``host_users`` **真的**决定主体目录 —— 传空表就没有 Linux
  主体，传真实名单就查不到 uid 4000 这号人。
"""

from __future__ import annotations

import pwd
import tempfile
import unittest

from backend.auth_linux.handoff import linux_user_id
from backend.bff import devserver

HOST_ID = devserver.DEFAULT_HOST_ID


class RealHostUsers(unittest.TestCase):
    def test_every_entry_is_a_real_account(self):
        users = devserver.real_host_users()
        self.assertTrue(users, "这台机器上至少应该有一个 uid>=1000 的账户")
        for name, uid in users:
            with self.subTest(name=name):
                self.assertEqual(pwd.getpwuid(uid).pw_name, name)
                self.assertGreaterEqual(uid, devserver.UID_FIRST_HUMAN)
                self.assertLess(uid, devserver.UID_NOBODY)

    def test_the_list_is_sorted_and_free_of_duplicates(self):
        uids = [uid for _name, uid in devserver.real_host_users()]
        self.assertEqual(uids, sorted(set(uids)))

    def test_the_deployment_default_is_not_the_fixture(self):
        self.assertEqual(
            devserver.resolve_host_users(synthetic=False), devserver.real_host_users()
        )
        self.assertEqual(devserver.resolve_host_users(synthetic=True), devserver.HOST_USERS)

    def test_no_fixture_uid_is_served_by_the_real_list(self):
        real = {uid for _name, uid in devserver.real_host_users()}
        for name, uid in devserver.HOST_USERS:
            with self.subTest(name=name):
                self.assertNotIn(uid, real)


class TheDirectoryFollowsHostUsers(unittest.TestCase):
    def test_an_empty_host_list_means_no_linux_principals(self):
        with tempfile.TemporaryDirectory() as state_dir:
            app = devserver.build_application(state_dir=state_dir, host_users=())
            self.assertEqual(app.service.principals.approved_principals(), frozenset())

    def test_a_real_list_does_not_contain_the_fixture_family(self):
        with tempfile.TemporaryDirectory() as state_dir:
            app = devserver.build_application(
                state_dir=state_dir, host_users=devserver.real_host_users()
            )
            for name, uid in devserver.HOST_USERS:
                with self.subTest(name=name):
                    self.assertIsNone(
                        app.service.principals.lookup(linux_user_id(HOST_ID, uid)),
                        f"{name} 不该出现在真实部署的主体目录里",
                    )

    def test_the_fixture_list_still_works_when_asked_for(self):
        # 夹具没有被删掉，只是不再当默认部署名单：测试与开发还要靠它保持确定性。
        with tempfile.TemporaryDirectory() as state_dir:
            app = devserver.build_application(state_dir=state_dir, host_users=devserver.HOST_USERS)
            for name, uid in devserver.HOST_USERS:
                with self.subTest(name=name):
                    principal = app.service.principals.lookup(linux_user_id(HOST_ID, uid))
                    self.assertIsNotNone(principal)
                    self.assertEqual(principal.linux_user, name)


if __name__ == "__main__":
    unittest.main()
