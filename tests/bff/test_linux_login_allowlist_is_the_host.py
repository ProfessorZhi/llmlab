"""登录允许名单必须是**这台机器上的人**，不是测试夹具。

这是一个真实的部署缺陷的回归测试。``build_linux_service`` 的允许名单与管理员
授权都从 ``HOST_USERS``（carol / alice / dave / bob，uid 4000-4003）算 —— 那是一份
夹具。``build_application`` 建应用时用的是本机真实用户（``real_host_users``），可
名单那一步没跟着走，于是：

* 允许名单里是四个**并不存在**的账户，本机真正要登录的人一个都不在里面；
* ``--platform-admin <真名>`` 也授权不出任何东西（名字不在夹具里），而且不报错。

这两件事都只在部署时才看得见，测试替身里永远是对的 —— 所以要一条用例专门证明
「名单跟着 ``host_users`` 走」。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import devserver

#: 三个**合成**的账户：名字与 uid 都不在 ``HOST_USERS`` 里，数量也不同（3 ≠ 4）。
HOST = (("kaidong", 4004), ("laver", 4006), ("zhengji", 4003))


class LinuxLoginAllowlistTests(unittest.TestCase):
    def build(self, **options):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = devserver.build_application(
            state_dir=tmp.name, host_users=options.pop("host_users", HOST),
            agent_socket=None, **options,
        )
        self.addCleanup(app.service.store.close)
        return app

    def test_the_allowlist_is_the_host_users_and_not_the_fixture(self) -> None:
        status = self.build().linux_service.status()

        self.assertEqual(status.allowlist_size, len(HOST))

    def test_a_fixture_name_grants_nothing_on_a_real_host(self) -> None:
        """``--platform-admin carol`` 在一台没有 carol 的机器上是空授权，而不是别人的管理员。"""

        status = self.build(admin_names=("carol",)).linux_service.status()

        self.assertEqual(status.platform_admin_grants, 0)

    def test_a_real_host_user_can_be_granted_admin_by_name(self) -> None:
        status = self.build(admin_names=("kaidong",)).linux_service.status()

        self.assertEqual(status.platform_admin_grants, 1)

    def test_an_explicit_allowlist_still_wins(self) -> None:
        status = self.build(allowed_logins=("only-me",)).linux_service.status()

        self.assertEqual(status.allowlist_size, 1)

    def test_without_host_users_the_fixture_is_still_the_default(self) -> None:
        """夹具缺省是给测试用的；这条钉住它没有被这次修复顺手改掉。"""

        fixture = devserver.build_linux_service(host_id=devserver.DEFAULT_HOST_ID)

        self.assertEqual(fixture.status().allowlist_size, len(devserver.HOST_USERS))


if __name__ == "__main__":
    unittest.main()
