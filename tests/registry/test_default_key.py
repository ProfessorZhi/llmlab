"""默认 Key 的契约（§4.3 / D5 / §6.2）。

全部用内存替身，不碰真实 Gateway 库、不写真实 HOME：

* ``FakeGatewayService`` 只实现 ``default_key`` 用到的三个方法与 ``principal``
  属性，返回形状与 ``GatewayService`` 保持一致；
* 文件相关用例全部落在 ``tmpdir``。

并发用例的重点不是「只调用了一次 create」——数据库层做不到——而是**收敛后只有
一把有效的 default**，且被交出去的明文恰好是活着的那把。
"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from backend.registry.default_key import (
    DEFAULT_LABEL,
    DefaultKey,
    KeyFileExists,
    default_key_path,
    ensure_default_key,
    rotate_default_key,
    write_key_file,
)

HOME_DIRS = (".config", "llmlab")


@dataclass(frozen=True)
class FakePrincipal:
    principal: str = "alice"
    is_platform_admin: bool = False


class FakeGatewayService:
    """内存版 GatewayService：记录调用、在内存里存 Key。

    行为与真实现对齐的部分：``create_key`` 不传限制字段时用默认值（无模型限制、
    rpm 60、并发 2、无过期），明文只在 create 的返回里出现一次；``list_keys``
    连已吊销的 Key 一起返回，由调用方按 ``state`` 过滤。
    """

    def __init__(self, principal: str = "alice", *, create_delay: float = 0.0) -> None:
        self.principal = FakePrincipal(principal)
        self.calls: list[tuple] = []
        self.create_delay = create_delay
        # 设好之后，每个 create 都在**插入之前**等着一起放行 —— 保证所有线程都
        # 已经看过「还没有 default」才各自去建，自愈逻辑因此一定被走到。
        self.pre_create_barrier: threading.Barrier | None = None
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._plaintext: dict[str, str] = {}
        self._seq = 0

    # --- GatewayService 的对接面 ------------------------------------------

    def list_keys(self, principal) -> dict:
        with self._lock:
            self.calls.append(("list_keys", principal.principal))
            rows = [dict(r) for r in self._records.values() if r["owner"] == principal.principal]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"keys": rows}

    def create_key(self, principal, body: dict) -> dict:
        if self.pre_create_barrier is not None:
            self.pre_create_barrier.wait()
        with self._lock:
            self.calls.append(("create_key", principal.principal, dict(body)))
            self._seq += 1
            key_id = f"key_{self._seq:04d}"
            plaintext = f"sk-lab-fake-{self._seq:04d}-{principal.principal}"
            self._records[key_id] = {
                "key_id": key_id,
                "prefix": "sk-lab-fake",
                "label": body.get("label"),
                "owner": principal.principal,
                "models": None,
                "rpm": 60,
                "concurrency": 2,
                "created_at": f"2026-10-23T00:00:{self._seq:02d}Z",
                "expires_at": None,
                "state": "active",
                "log_bodies": False,
            }
            self._plaintext[key_id] = plaintext
            snapshot = {**self._records[key_id], "key": plaintext}
        # 在锁外停顿：模拟「插入已提交，但调用方还没来得及复查」的窗口，
        # 让并发用例真的撞上两把 default 同时存在的情况。
        if self.create_delay:
            time.sleep(self.create_delay)
        return {"key": snapshot}

    def revoke_key(self, principal, key_id: str) -> dict:
        with self._lock:
            self.calls.append(("revoke_key", principal.principal, key_id))
            record = self._records[key_id]
            if record["owner"] != principal.principal:
                raise AssertionError("refusing to revoke another principal's key")
            record["state"] = "revoked"
            return {"key": dict(record)}

    # --- 测试辅助 ---------------------------------------------------------

    def active_defaults(self, principal=None) -> list[dict]:
        who = principal or self.principal
        with self._lock:
            rows = [dict(r) for r in self._records.values() if r["owner"] == who.principal]
        return [r for r in rows if r["label"] == DEFAULT_LABEL and r["state"] == "active"]

    def all_keys(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records.values()]

    def authenticate(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        with self._lock:
            for key_id, value in self._plaintext.items():
                if value == plaintext and self._records[key_id]["state"] == "active":
                    return key_id
        return None

    def calls_of(self, name: str) -> list[tuple]:
        with self._lock:
            return [call for call in self.calls if call[0] == name]


class DefaultKeyEnsureTests(unittest.TestCase):
    def test_first_ensure_creates_with_plaintext_and_default_limits(self) -> None:
        service = FakeGatewayService()

        result = ensure_default_key(service)

        self.assertIsInstance(result, DefaultKey)
        self.assertTrue(result.created)
        self.assertIsNotNone(result.plaintext)
        self.assertEqual(service.authenticate(result.plaintext), result.key_id)

        [record] = service.active_defaults()
        self.assertEqual(record["key_id"], result.key_id)
        self.assertEqual(record["label"], DEFAULT_LABEL)
        self.assertIsNone(record["models"])  # 无模型限制
        self.assertEqual(record["rpm"], 60)  # 默认限速
        self.assertEqual(record["concurrency"], 2)
        self.assertIsNone(record["expires_at"])  # 无过期

        creates = service.calls_of("create_key")
        self.assertEqual(len(creates), 1)
        # 只传 label：其余字段交给 Gateway 的默认值，不在这里另造一套策略。
        self.assertEqual(creates[0][2], {"label": DEFAULT_LABEL})

    def test_second_ensure_reuses_key_and_never_reveals_plaintext(self) -> None:
        service = FakeGatewayService()
        first = ensure_default_key(service)

        second = ensure_default_key(service)

        self.assertFalse(second.created)
        self.assertIsNone(second.plaintext)
        self.assertEqual(second.key_id, first.key_id)
        self.assertEqual(len(service.calls_of("create_key")), 1)
        self.assertEqual(len(service.active_defaults()), 1)

    def test_principal_can_be_supplied_by_the_caller(self) -> None:
        service = FakeGatewayService("alice")
        who = FakePrincipal("bob")

        result = ensure_default_key(service, principal=who)

        self.assertTrue(result.created)
        self.assertEqual(service.active_defaults(who)[0]["key_id"], result.key_id)
        self.assertEqual(service.active_defaults(), [])
        self.assertEqual(service.calls_of("create_key")[0][1], "bob")

    def test_missing_principal_is_refused(self) -> None:
        class Bare:
            def list_keys(self, principal):  # pragma: no cover - 不应被调用
                raise AssertionError("no identity means no call")

        with self.assertRaises(ValueError):
            ensure_default_key(Bare())


class DefaultKeyRotateTests(unittest.TestCase):
    def test_rotate_revokes_old_and_returns_working_new_key(self) -> None:
        service = FakeGatewayService()
        old = ensure_default_key(service)
        assert old.plaintext is not None

        new = rotate_default_key(service)

        self.assertTrue(new.created)
        self.assertIsNotNone(new.plaintext)
        self.assertNotEqual(new.plaintext, old.plaintext)
        self.assertNotEqual(new.key_id, old.key_id)
        self.assertEqual(service.authenticate(old.plaintext), None)  # 旧 key 失效
        self.assertEqual(service.authenticate(new.plaintext), new.key_id)  # 新 key 可用
        [record] = service.active_defaults()
        self.assertEqual(record["key_id"], new.key_id)
        old_record = next(r for r in service.all_keys() if r["key_id"] == old.key_id)
        self.assertEqual(old_record["state"], "revoked")


class KeyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_path_and_permissions(self) -> None:
        path = default_key_path(self.home)
        self.assertEqual(path, self.home / ".config" / "llmlab" / "default_key")

        write_key_file(path, "sk-lab-file-secret")

        self.assertEqual(path.read_text(encoding="utf-8"), "sk-lab-file-secret")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_existing_different_content_is_not_overwritten(self) -> None:
        path = default_key_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sk-lab-old", encoding="utf-8")

        with self.assertRaises(KeyFileExists) as caught:
            write_key_file(path, "sk-lab-new-secret")

        self.assertEqual(path.read_text(encoding="utf-8"), "sk-lab-old")
        message = str(caught.exception)
        self.assertIn(str(path), message)
        self.assertNotIn("sk-lab-new-secret", message)

    def test_same_content_is_idempotent_and_tightens_mode(self) -> None:
        path = default_key_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sk-lab-same", encoding="utf-8")
        os.chmod(path, 0o644)

        write_key_file(path, "sk-lab-same")  # 不抛错

        self.assertEqual(path.read_text(encoding="utf-8"), "sk-lab-same")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_symlink_is_treated_as_a_conflict(self) -> None:
        path = default_key_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.home / "elsewhere"
        target.write_text("sk-lab-target", encoding="utf-8")
        path.symlink_to(target)

        with self.assertRaises(KeyFileExists):
            write_key_file(path, "sk-lab-target")  # 内容相同也不行

        self.assertEqual(target.read_text(encoding="utf-8"), "sk-lab-target")
        self.assertTrue(path.is_symlink())


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_ensure_leaves_exactly_one_default(self) -> None:
        workers = 8
        service = FakeGatewayService(create_delay=0.01)
        barrier = threading.Barrier(workers)
        service.pre_create_barrier = barrier
        results: list[DefaultKey] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait()
                result = ensure_default_key(service)
            except BaseException as exc:  # noqa: BLE001 - 失败要带出线程
                with lock:
                    errors.append(exc)
                return
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])

        # 每个线程都真的建了一把（屏障保证了这一点），所以下面断言的是自愈结果，
        # 不是「碰巧只有一个线程动手」。
        self.assertEqual(len(service.calls_of("create_key")), workers)

        active = service.active_defaults()
        self.assertEqual(len(active), 1, "并发 ensure 后必须只剩一把有效的 default")
        winner = active[0]["key_id"]
        # 收敛顺序与进程无关，所以每个线程都该报同一把胜出者。
        self.assertEqual({result.key_id for result in results}, {winner})

        with_plaintext = [result for result in results if result.plaintext is not None]
        self.assertEqual(len(with_plaintext), 1, "只有真正创建了胜出者的那次返回明文")
        self.assertEqual(service.authenticate(with_plaintext[0].plaintext), winner)
        # 输掉的那几把必须被吊销，而不是留在库里当第二把 default。
        losers = [r for r in service.all_keys() if r["key_id"] != winner]
        self.assertTrue(all(r["state"] == "revoked" for r in losers))


class PlaintextDisciplineTests(unittest.TestCase):
    def test_repr_never_shows_plaintext(self) -> None:
        secret = "sk-lab-very-secret-value"
        result = DefaultKey(plaintext=secret, key_id="key_0001", created=True)

        self.assertNotIn(secret, repr(result))
        self.assertNotIn(secret, str(result))
        self.assertIn("key_0001", repr(result))

    def test_exception_never_shows_plaintext(self) -> None:
        secret = "sk-lab-very-secret-value"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "default_key"
            path.write_text("sk-lab-something-else", encoding="utf-8")
            with self.assertRaises(KeyFileExists) as caught:
                write_key_file(path, secret)
        rendered = f"{caught.exception!r} {caught.exception}"
        self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
