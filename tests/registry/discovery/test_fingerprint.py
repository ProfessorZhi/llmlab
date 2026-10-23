"""§5.3 指纹与匹配的测试。

全部在 ``tempfile`` 临时目录里造文件，不碰真实 home、不联网。匹配测试用的
路径是合成的 ``/models/...``：``match_previous`` 只做字符串与 (device, inode)
比较，从不打开文件，所以不需要真的存在。

每个匹配场景都断言**完整的** ``(status, 旧路径, 新路径, model_id)`` 列表，
而不只是「状态集合里包含某几项」：``model_id`` 是否沿袭、输出顺序是否稳定
都属于契约的一部分。
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path

from backend.registry.discovery.fingerprint import (
    CHUNK,
    fingerprint_bytes,
    fingerprint_file,
    fingerprint_hf_dir,
    fingerprint_model,
    match_previous,
)
from backend.registry.discovery.types import (
    CurrentLocation,
    ModelCandidate,
    PreviousLocation,
)

FINGERPRINT_RE = re.compile(r"^\d+:[0-9a-f]{64}$")


def _pattern(size: int, seed: int = 0) -> bytes:
    """确定性的伪随机内容；不用 os.urandom，失败才能复现。"""
    block = bytes((index + seed) % 256 for index in range(256))
    return (block * (size // len(block) + 1))[:size]


def _sha(*parts: bytes) -> str:
    return hashlib.sha256(b"".join(parts)).hexdigest()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _prev(model_id: str, location_id: str, path: str, fp: str, device: int, inode: int) -> PreviousLocation:
    return PreviousLocation(model_id, location_id, Path(path), fp, device, inode)


def _curr(path: str, fp: str, device: int, inode: int) -> CurrentLocation:
    return CurrentLocation(Path(path), fp, device, inode)


def _describe(result) -> list[tuple[str, str | None, str | None, str | None]]:
    return [
        (
            outcome.status,
            str(outcome.previous.path) if outcome.previous is not None else None,
            str(outcome.current.path) if outcome.current is not None else None,
            outcome.model_id,
        )
        for outcome in result.outcomes
    ]


class FingerprintFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_large_file_uses_first_and_last_chunk(self) -> None:
        data = _pattern(3 * CHUNK + 123)
        path = _write(self.root / "big.gguf", data)

        result = fingerprint_file(path)

        self.assertRegex(result, FINGERPRINT_RE)
        self.assertEqual(result, f"{len(data)}:{_sha(data[:CHUNK], data[-CHUNK:])}")

    def test_middle_change_keeps_fingerprint_but_edges_change_it(self) -> None:
        data = bytearray(_pattern(3 * CHUNK))
        path = _write(self.root / "model.gguf", bytes(data))
        baseline = fingerprint_file(path)

        # 中段（第一个 chunk 与最后一个 chunk 之间）改动：指纹不变。
        data[3 * CHUNK // 2] ^= 0xFF
        _write(path, bytes(data))
        self.assertEqual(fingerprint_file(path), baseline)

        # 首段改动：变。
        data[100] ^= 0xFF
        _write(path, bytes(data))
        head_changed = fingerprint_file(path)
        self.assertNotEqual(head_changed, baseline)

        # 尾段改动：变。
        data[-100] ^= 0xFF
        _write(path, bytes(data))
        self.assertNotEqual(fingerprint_file(path), head_changed)

    def test_boundary_sizes(self) -> None:
        # 0 字节：sha256 空串。
        empty = _write(self.root / "empty.gguf", b"")
        self.assertEqual(fingerprint_file(empty), f"0:{_sha(b'')}")

        # 恰好 1 个 chunk：只有「前 chunk」，尾部为空，不该把同一段喂两遍。
        one_mib = _pattern(CHUNK, seed=1)
        path = _write(self.root / "one-mib.gguf", one_mib)
        self.assertEqual(fingerprint_file(path), f"{CHUNK}:{_sha(one_mib)}")

        # 1 chunk + 1 字节：整读一遍后切成前 chunk ‖ 余下的 1 字节，等于全文哈希。
        over = _pattern(CHUNK + 1, seed=2)
        path = _write(self.root / "over.gguf", over)
        self.assertEqual(fingerprint_file(path), f"{CHUNK + 1}:{_sha(over)}")

        # 恰好 2 个 chunk：单次读取的边界，等于全文哈希。
        two_mib = _pattern(2 * CHUNK, seed=3)
        path = _write(self.root / "two-mib.gguf", two_mib)
        self.assertEqual(fingerprint_file(path), f"{2 * CHUNK}:{_sha(two_mib)}")

        # 刚过边界：改成读首、尾两个 chunk，中间那个字节不参与。
        bigger = _pattern(2 * CHUNK + 1, seed=4)
        path = _write(self.root / "bigger.gguf", bigger)
        self.assertEqual(
            fingerprint_file(path),
            f"{2 * CHUNK + 1}:{_sha(bigger[:CHUNK], bigger[-CHUNK:])}",
        )

    def test_custom_chunk(self) -> None:
        data = _pattern(5000, seed=5)
        path = _write(self.root / "small.bin", data)

        self.assertEqual(
            fingerprint_file(path, chunk=1024),
            f"{len(data)}:{_sha(data[:1024], data[-1024:])}",
        )

        tiny = _pattern(100, seed=6)
        tiny_path = _write(self.root / "tiny.bin", tiny)
        self.assertEqual(fingerprint_file(tiny_path, chunk=1024), f"100:{_sha(tiny)}")

    def test_chunk_must_be_positive(self) -> None:
        path = _write(self.root / "x.bin", b"x")
        with self.assertRaises(ValueError):
            fingerprint_file(path, chunk=0)

    def test_missing_path_is_unreadable(self) -> None:
        self.assertEqual(fingerprint_file(self.root / "nope.gguf"), "unreadable:0")

    def test_dangling_symlink_is_unreadable(self) -> None:
        link = self.root / "link.gguf"
        try:
            os.symlink(self.root / "gone.gguf", link)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
            self.skipTest(f"无法创建符号链接: {exc}")
        self.assertEqual(fingerprint_file(link), "unreadable:0")

    def test_directory_is_unreadable_with_known_size(self) -> None:
        directory = self.root / "a-directory"
        directory.mkdir()
        size = directory.stat().st_size
        self.assertEqual(fingerprint_file(directory), f"unreadable:{size}")

    def test_permission_denied_is_unreadable(self) -> None:
        if getattr(os, "geteuid", lambda: -1)() == 0:
            self.skipTest("root 会绕过权限位，这个场景只能在普通用户下验证")
        path = _write(self.root / "locked.gguf", _pattern(4096))
        size = path.stat().st_size
        os.chmod(path, 0)
        try:
            self.assertEqual(fingerprint_file(path), f"unreadable:{size}")
        finally:
            os.chmod(path, 0o600)

    def test_fingerprint_bytes_orders_head_before_tail(self) -> None:
        self.assertEqual(fingerprint_bytes(b"abc", b"def", 6), f"6:{_sha(b'abcdef')}")
        self.assertNotEqual(
            fingerprint_bytes(b"abc", b"def", 6),
            fingerprint_bytes(b"def", b"abc", 6),
        )

    def test_fingerprint_bytes_size_comes_from_argument(self) -> None:
        # size 不是 len(head)+len(tail)：中段没读，大小必须由调用方给出。
        self.assertEqual(fingerprint_bytes(b"", b"", 42), f"42:{_sha(b'')}")
        with self.assertRaises(ValueError):
            fingerprint_bytes(b"", b"", -1)


class FingerprintHfDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_dir(self, name: str = "hub", config: bytes | None = b'{"architectures":["X"]}') -> Path:
        directory = self.root / name
        directory.mkdir(parents=True)
        if config is not None:
            _write(directory / "config.json", config)
        _write(directory / "model-00001-of-00002.safetensors", _pattern(4000, seed=1))
        _write(directory / "model-00002-of-00002.safetensors", _pattern(3000, seed=2))
        return directory

    def test_format_and_total_size(self) -> None:
        directory = self._make_dir()
        result = fingerprint_hf_dir(directory)
        self.assertRegex(result, FINGERPRINT_RE)
        self.assertEqual(result.split(":", 1)[0], str(4000 + 3000))

    def test_deterministic_regardless_of_creation_order(self) -> None:
        first = self._make_dir("first")
        second = self.root / "second"
        second.mkdir()
        _write(second / "config.json", b'{"architectures":["X"]}')
        # 故意反过来创建。
        _write(second / "model-00002-of-00002.safetensors", _pattern(3000, seed=2))
        _write(second / "model-00001-of-00002.safetensors", _pattern(4000, seed=1))

        self.assertEqual(fingerprint_hf_dir(first), fingerprint_hf_dir(second))

    def test_adding_a_shard_changes_fingerprint(self) -> None:
        directory = self._make_dir()
        before = fingerprint_hf_dir(directory)
        _write(directory / "model-00003-of-00002.safetensors", _pattern(500, seed=3))
        after = fingerprint_hf_dir(directory)

        self.assertNotEqual(before, after)
        self.assertEqual(after.split(":", 1)[0], str(4000 + 3000 + 500))

    def test_shard_size_change_changes_fingerprint(self) -> None:
        directory = self._make_dir()
        before = fingerprint_hf_dir(directory)
        _write(directory / "model-00001-of-00002.safetensors", _pattern(4001, seed=1))
        self.assertNotEqual(fingerprint_hf_dir(directory), before)

    def test_shard_rename_changes_fingerprint(self) -> None:
        directory = self._make_dir()
        before = fingerprint_hf_dir(directory)
        (directory / "model-00002-of-00002.safetensors").rename(
            directory / "model-00002-of-00001.safetensors"
        )
        self.assertNotEqual(fingerprint_hf_dir(directory), before)

    def test_config_change_changes_fingerprint(self) -> None:
        directory = self._make_dir()
        before = fingerprint_hf_dir(directory)
        _write(directory / "config.json", b'{"architectures":["Y"]}')
        self.assertNotEqual(fingerprint_hf_dir(directory), before)

    def test_missing_config_is_distinct_from_empty_config(self) -> None:
        without = self._make_dir("without", config=None)
        with_empty = self._make_dir("with-empty", config=b"")

        self.assertNotEqual(fingerprint_hf_dir(without), fingerprint_hf_dir(with_empty))
        # 加一个空的 config.json 也必须改变指纹（空串 ≠ 空文件的 sha256）。
        _write(without / "config.json", b"")
        self.assertEqual(fingerprint_hf_dir(without), fingerprint_hf_dir(with_empty))

    def test_non_weight_files_are_ignored(self) -> None:
        directory = self._make_dir()
        before = fingerprint_hf_dir(directory)
        _write(directory / "README.md", b"hello")
        _write(directory / "tokenizer.json", b"{}")
        self.assertEqual(fingerprint_hf_dir(directory), before)

    def test_nested_weights_are_part_of_manifest(self) -> None:
        flat = self._make_dir("flat", config=None)
        nested = self.root / "nested"
        nested.mkdir()
        _write(nested / "unet" / "model-00001-of-00002.safetensors", _pattern(4000, seed=1))
        _write(nested / "unet" / "model-00002-of-00002.safetensors", _pattern(3000, seed=2))

        # 同样是 7000 字节、同样的名字，但相对路径不同 ⇒ 不是同一个模型。
        self.assertNotEqual(fingerprint_hf_dir(flat), fingerprint_hf_dir(nested))

        # 给 flat 也塞进子目录后，清单对齐，指纹应当相同。
        (flat / "unet").mkdir()
        for name in ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
            (flat / name).rename(flat / "unet" / name)
        self.assertEqual(fingerprint_hf_dir(flat), fingerprint_hf_dir(nested))

    def test_relative_path_matches_absolute(self) -> None:
        directory = self._make_dir()
        relative = Path(os.path.relpath(directory, os.getcwd()))
        self.assertEqual(fingerprint_hf_dir(relative), fingerprint_hf_dir(directory))

    def test_missing_directory_is_unreadable(self) -> None:
        self.assertEqual(fingerprint_hf_dir(self.root / "absent"), "unreadable:0")

    def test_regular_file_path_is_unreadable(self) -> None:
        path = _write(self.root / "not-a-dir.safetensors", b"x")
        self.assertEqual(fingerprint_hf_dir(path), "unreadable:0")

    def test_dangling_shard_makes_directory_unreadable(self) -> None:
        directory = self._make_dir()
        link = directory / "model-00003-of-00002.safetensors"
        try:
            os.symlink(self.root / "gone.safetensors", link)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
            self.skipTest(f"无法创建符号链接: {exc}")
        self.assertEqual(fingerprint_hf_dir(directory), "unreadable:0")


class FingerprintModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _candidate(self, path: Path, kind: str) -> ModelCandidate:
        stat = path.stat()
        return ModelCandidate(
            path=path,
            kind=kind,  # type: ignore[arg-type]
            size_bytes=stat.st_size,
            device=stat.st_dev,
            inode=stat.st_ino,
            mtime_ns=stat.st_mtime_ns,
        )

    def test_gguf_uses_file_fingerprint(self) -> None:
        path = _write(self.root / "model.gguf", _pattern(1000))
        candidate = self._candidate(path, "gguf")
        self.assertEqual(fingerprint_model(candidate), fingerprint_file(path))

    def test_hf_and_diffusers_use_directory_fingerprint(self) -> None:
        directory = self.root / "hub"
        _write(directory / "config.json", b"{}")
        _write(directory / "model.safetensors", _pattern(1000))
        expected = fingerprint_hf_dir(directory)

        self.assertEqual(fingerprint_model(self._candidate(directory, "hf")), expected)
        self.assertEqual(fingerprint_model(self._candidate(directory, "diffusers")), expected)

    def test_unknown_kind_is_a_programming_error(self) -> None:
        path = _write(self.root / "model.gguf", b"x")
        candidate = dataclasses.replace(self._candidate(path, "gguf"), kind="safetensors")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            fingerprint_model(candidate)


ALPHA = "/models/alpha.gguf"
BRAVO = "/models/bravo.gguf"
CHARLIE = "/models/charlie.gguf"
DELTA = "/models/delta.gguf"
ECHO = "/models/echo.gguf"
GOLF = "/models/golf.gguf"
XRAY = "/models/xray.gguf"


class MatchPreviousTests(unittest.TestCase):
    def test_empty_inputs(self) -> None:
        result = match_previous([], [])
        self.assertEqual(result.outcomes, ())

    def test_same_path_same_inode_is_unchanged(self) -> None:
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(ALPHA, "100:aa", 1, 10)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [("unchanged", ALPHA, ALPHA, "M1")],
        )

    def test_same_inode_new_path_is_moved_same_disk(self) -> None:
        # 指纹故意给一个不同的值：同 inode 是物理证据，优先级高于指纹。
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(BRAVO, "999:ff", 1, 10)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [("moved_same_disk", ALPHA, BRAVO, "M1")],
        )

    def test_same_fingerprint_different_inode_is_moved_cross_disk(self) -> None:
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(BRAVO, "100:aa", 2, 20)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [("moved_cross_disk", ALPHA, BRAVO, "M1")],
        )

    def test_copy_when_previous_location_still_present(self) -> None:
        # 旧位置这次还在（第 1 步按 inode 配上），同指纹的新位置是副本。
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(ALPHA, "100:aa", 1, 10), _curr(CHARLIE, "100:aa", 2, 20)]

        result = match_previous(previous, current)

        self.assertEqual(
            _describe(result),
            [
                ("unchanged", ALPHA, ALPHA, "M1"),
                ("copied", ALPHA, CHARLIE, "M1"),
            ],
        )
        self.assertEqual(len(result.by_status("unchanged")), 1)
        self.assertEqual(len(result.by_status("copied")), 1)

    def test_copy_after_same_disk_rename(self) -> None:
        # 旧 location 在第 1 步就被用掉（改名），副本仍要认成 copied。
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(BRAVO, "100:aa", 1, 10), _curr(CHARLIE, "100:aa", 2, 20)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [
                ("moved_same_disk", ALPHA, BRAVO, "M1"),
                ("copied", ALPHA, CHARLIE, "M1"),
            ],
        )

    def test_one_previous_two_new_needs_confirmation(self) -> None:
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(BRAVO, "100:aa", 2, 20), _curr(CHARLIE, "100:aa", 3, 30)]

        result = match_previous(previous, current)

        self.assertEqual(
            _describe(result),
            [
                ("needs_confirmation", ALPHA, None, "M1"),
                ("new", None, BRAVO, None),
                ("new", None, CHARLIE, None),
            ],
        )
        # 旧 id 不能被自动改指向。
        self.assertEqual([o.model_id for o in result.by_status("new")], [None, None])

    def test_missing_when_nothing_matches(self) -> None:
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(BRAVO, "555:ee", 2, 20)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [
                ("missing", ALPHA, None, "M1"),
                ("new", None, BRAVO, None),
            ],
        )

    def test_multiple_previous_with_same_fingerprint_needs_confirmation(self) -> None:
        previous = [
            _prev("M1", "L1", ALPHA, "100:aa", 1, 10),
            _prev("M2", "L2", BRAVO, "100:aa", 2, 20),
        ]
        current = [_curr(CHARLIE, "100:aa", 3, 30)]

        result = match_previous(previous, current)

        self.assertEqual(
            _describe(result),
            [
                ("needs_confirmation", ALPHA, None, "M1"),
                ("needs_confirmation", BRAVO, None, "M2"),
                ("needs_confirmation", None, CHARLIE, None),
            ],
        )

    def test_hardlink_two_paths_share_one_inode(self) -> None:
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(ALPHA, "100:aa", 1, 10), _curr(DELTA, "100:aa", 1, 10)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [
                ("unchanged", ALPHA, ALPHA, "M1"),
                ("copied", ALPHA, DELTA, "M1"),
            ],
        )

    def test_inode_group_without_original_path_is_ambiguous(self) -> None:
        previous = [_prev("M1", "L1", ALPHA, "100:aa", 1, 10)]
        current = [_curr(BRAVO, "100:aa", 1, 10), _curr(DELTA, "100:aa", 1, 10)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [
                ("needs_confirmation", ALPHA, None, "M1"),
                ("needs_confirmation", None, BRAVO, None),
                ("needs_confirmation", None, DELTA, None),
            ],
        )

    def test_two_previous_sharing_one_inode(self) -> None:
        previous = [
            _prev("M1", "L1", ALPHA, "100:aa", 1, 10),
            _prev("M2", "L2", BRAVO, "100:aa", 1, 10),
        ]
        current = [_curr(ALPHA, "100:aa", 1, 10)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [
                ("unchanged", ALPHA, ALPHA, "M1"),
                ("needs_confirmation", BRAVO, None, "M2"),
            ],
        )

    def test_unreadable_fingerprints_are_not_identity_evidence(self) -> None:
        # 所有读不了的文件指纹都是 "unreadable:0"；拿它配对会把两个无关的
        # 坏模型认成同一个，所以不配对。
        previous = [_prev("M1", "L1", ALPHA, "unreadable:0", 1, 10)]
        current = [_curr(BRAVO, "unreadable:0", 2, 20)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [
                ("missing", ALPHA, None, "M1"),
                ("new", None, BRAVO, None),
            ],
        )

    def test_unreadable_inode_match_still_works(self) -> None:
        # 内容读不到但 inode 还在：仍然可以断定是同盘移动。
        previous = [_prev("M1", "L1", ALPHA, "unreadable:0", 1, 10)]
        current = [_curr(BRAVO, "unreadable:0", 1, 10)]

        self.assertEqual(
            _describe(match_previous(previous, current)),
            [("moved_same_disk", ALPHA, BRAVO, "M1")],
        )

    def test_output_order_is_previous_path_then_current_path(self) -> None:
        previous = [
            _prev("M1", "L1", ALPHA, "100:aa", 1, 10),
            _prev("M2", "L2", BRAVO, "200:bb", 1, 20),
            _prev("M3", "L3", CHARLIE, "300:cc", 1, 30),
            _prev("M4", "L4", DELTA, "400:dd", 1, 40),
        ]
        current = [
            _curr(GOLF, "400:dd", 2, 70),
            _curr(ALPHA, "100:aa", 1, 10),
            _curr(XRAY, "900:99", 2, 90),
            _curr(DELTA, "400:dd", 1, 40),
            _curr(ECHO, "300:cc", 2, 60),
        ]

        expected = [
            ("unchanged", ALPHA, ALPHA, "M1"),
            ("missing", BRAVO, None, "M2"),
            ("moved_cross_disk", CHARLIE, ECHO, "M3"),
            ("unchanged", DELTA, DELTA, "M4"),
            ("copied", DELTA, GOLF, "M4"),
            ("new", None, XRAY, None),
        ]

        result = match_previous(previous, current)
        self.assertEqual(_describe(result), expected)

        # 输入顺序不该影响结果。
        self.assertEqual(_describe(match_previous(list(reversed(previous)), list(reversed(current)))), expected)

    def test_statuses_are_retrievable_by_status(self) -> None:
        previous = [
            _prev("M1", "L1", ALPHA, "100:aa", 1, 10),
            _prev("M2", "L2", BRAVO, "200:bb", 1, 20),
        ]
        current = [_curr(ALPHA, "100:aa", 1, 10)]

        result = match_previous(previous, current)

        self.assertEqual({o.status for o in result.outcomes}, {"unchanged", "missing"})
        self.assertEqual([o.model_id for o in result.by_status("missing")], ["M2"])


if __name__ == "__main__":
    unittest.main()
