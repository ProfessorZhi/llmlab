"""端口允许列表：发现与登记共用同一份，管理员必须能把它变宽。

允许列表是这条路径上唯一的范围控制：不在里面的端口，既不会被发现，也**登记不了**
—— 不是「发现了但拒绝」，是根本看不见。所以它必须是可以由部署决定的，而不是写死
在代码里的五个端口（否则任何一个跑在 8765 上的引擎都永远进不来）。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import devserver
from backend.registry.urls import DEFAULT_ALLOWED_PORTS


class AllowedPortsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def config(self, **kwargs):
        app = devserver.build_application(state_dir=self._tmp.name, **kwargs)
        return app.service.config

    def test_the_built_in_ports_survive(self):
        config = self.config()

        self.assertEqual(config.allowed_ports, DEFAULT_ALLOWED_PORTS)

    def test_extra_ports_are_added_not_substituted(self):
        """追加而不是替换：多开一个端口不该悄悄关掉其余四个。"""

        config = self.config(allowed_ports=(8770, 8771))

        self.assertEqual(config.allowed_ports, DEFAULT_ALLOWED_PORTS | {8770, 8771})

    def test_an_impossible_port_is_rejected_at_startup(self):
        with self.assertRaises(Exception):
            self.config(allowed_ports=(70000,))


if __name__ == "__main__":
    unittest.main()
