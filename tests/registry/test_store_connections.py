"""一条连接跟一个线程走 —— 线程结束后，描述符要还回去。

这一支来自一次真事故：控制台的资源监控页开始按秒刷新之后，BFF 在十几分钟里把
``ulimit -n``（本机 1024）用完，此后每个 ``sqlite3.connect`` 都报
``unable to open database file``，连登录页都打不开（``/auth/csrf`` 500）。

原因不在请求处理，在连接的去向：``Store`` 按**线程**缓存连接，这是并发读安全
所需要的；但 BFF 的服务器是「一个请求一个线程」，线程结束之后再没有人用到它那
条连接，它却一直留在 ``self._connections`` 里。每来一个请求漏一个描述符。

所以这里钉两件事：

* 结束的线程留下的连接会被关掉（描述符数不随请求数增长）；
* 还在跑的线程的连接**不会**被误关 —— 那会让并发请求撞上
  ``Cannot operate on a closed database``。
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from backend.registry.store import Store


def _open_handles(directory: str) -> int:
    """数当前进程在这个目录下开着多少个文件（借 ``/proc/self/fd``）。"""
    handles = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:  # 描述符在这一瞬间被关掉了
            continue
        if target.startswith(directory):
            handles += 1
    return handles


@unittest.skipUnless(os.path.isdir("/proc/self/fd"), "需要 /proc 才能数描述符")
class FinishedThreadConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.store = Store(str(Path(self.dir) / "registry.db"))
        self.addCleanup(self.store.close)

    def _one_request(self) -> None:
        self.store.connection.execute("select 1").fetchone()

    def _run_requests(self, count: int) -> None:
        """模拟 ``count`` 个「一次请求一个线程」——一个接一个，像轮询那样。"""
        for _ in range(count):
            thread = threading.Thread(target=self._one_request)
            thread.start()
            thread.join()

    def test_finished_threads_do_not_keep_their_connections(self):
        """60 个「一个请求一个线程」之后，连接数与描述符数都回到了起点。

        这正是线上的形状：请求一个接一个地来，彼此不重叠。修之前每来一个请求就
        多一条永不关闭的连接，六十个请求之后 ``_connections`` 会长到 61。
        """
        self._one_request()  # 主线程自己那条，算基线
        baseline = _open_handles(self.dir)

        self._run_requests(60)

        self.assertLessEqual(
            len(self.store._connections), 3,
            f"结束的线程留下了连接：{len(self.store._connections)} 条",
        )
        self.assertLess(
            _open_handles(self.dir), baseline + 12,
            "60 个请求之后描述符还在往上堆（每个连接是 db + wal + shm 三个）",
        )

    def test_connections_of_live_threads_are_left_alone(self):
        """另一个线程正在用它的连接时，新线程开机不能把它关掉。"""
        started = threading.Event()
        finish = threading.Event()
        errors: list[BaseException] = []

        def long_running() -> None:
            try:
                self.store.connection.execute("select 1").fetchone()
                started.set()
                finish.wait(5)
                # 期间别的线程开了新连接并触发清理；这一条必须仍然可用。
                self.store.connection.execute("select 1").fetchone()
            except BaseException as exc:  # noqa: BLE001 - 测试要把异常带回主线程
                errors.append(exc)

        keeper = threading.Thread(target=long_running)
        keeper.start()
        started.wait(5)
        try:
            self._run_requests(10)
        finally:
            finish.set()
            keeper.join(5)

        self.assertEqual(errors, [], "活着的线程的连接被误关了")
        self.assertTrue(any(ref() is not None for ref, _ in self.store._connections))


if __name__ == "__main__":
    unittest.main()
