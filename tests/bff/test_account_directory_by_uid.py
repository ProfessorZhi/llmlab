"""``AccountAwareDirectory`` 必须真的回答 ``by_uid``。

这是一个回归测试，来自一个真实的坑：子类调 ``super().__init__(())`` 建父类时，
父类的两份 uid 映射是**空表**。父类 ``by_uid`` 只读那张表，所以不显式覆盖就会
对这台机器上**每一个**真实 Linux 用户回答「没有这个人」。

后果不是某个页面少一行：代理通道正是靠 ``by_uid(SO_PEERCRED 给的 uid)`` 认人的，
空表等于把所有人拒在门外，而且拒得很「合理」—— 客户端只会看到 uid 未映射。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from backend.auth_linux.handoff import linux_user_id
from backend.bff import devserver


class AccountDirectoryByUidTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="llm-lab-bff-byuid-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.users = (("carol", 4000), ("alice", 4001))
        self.app = devserver.build_application(
            state_dir=self.tmp, host_users=self.users, agent_socket=None
        )
        self.addCleanup(self.app.service.store.close)
        self.directory = self.app.service.principals

    def test_a_real_linux_uid_maps_to_its_principal(self) -> None:
        found = self.directory.by_uid(4001)

        self.assertIsNotNone(found, "by_uid 对被吞掉的父类空表会返回 None")
        self.assertEqual(found.principal, linux_user_id(devserver.DEFAULT_HOST_ID, 4001))
        self.assertEqual(found.linux_user, "alice")
        self.assertTrue(found.approved)

    def test_an_unknown_uid_still_answers_none(self) -> None:
        self.assertIsNone(self.directory.by_uid(4999))

    def test_the_uid_set_is_the_host_users(self) -> None:
        self.assertEqual(self.directory.uids(), frozenset({4000, 4001}))

    def test_a_lookup_by_principal_name_still_works(self) -> None:
        """静态目录那条路不能被这次修复弄坏。"""

        found = self.directory.lookup(linux_user_id(devserver.DEFAULT_HOST_ID, 4000))

        self.assertIsNotNone(found)
        self.assertEqual(found.linux_user, "carol")


if __name__ == "__main__":
    unittest.main()
