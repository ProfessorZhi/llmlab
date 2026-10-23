"""S1 的验收测试：把四个纯函数模块串成一条链。

单元测试各自证明自己的规则；这里只回答一个问题——**把它们放在一起时，仿
kaidong 的目录是否恰好得到 4 个模型 / 2 个 build，并且发现、facts、指纹、
移动判定彼此对得上**。这正是设计文档 §8 对 S1 的验收口径。

刻意不做的事：不执行任何真实二进制（build 探测注入假 runner），不读 HOME。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.registry.discovery import (
    CurrentLocation,
    PreviousLocation,
    enrich,
    fingerprint_model,
    match_previous,
    read_model,
    scan_tree,
)
from tests.registry.discovery.fixture_tree import (
    EXPECTED_BUILD_COUNT,
    EXPECTED_MODEL_COUNT,
    build_kaidong_like_tree,
)


def _previous(models, ids) -> list[PreviousLocation]:
    """把一次扫描的结果当成「上一次上报」。"""

    return [
        PreviousLocation(
            model_id=ids[i],
            location_id=f"loc-{i}",
            path=model.path,
            fingerprint=fingerprint_model(model),
            device=model.device,
            inode=model.inode,
        )
        for i, model in enumerate(models)
    ]


def _current(models) -> list[CurrentLocation]:
    return [
        CurrentLocation(path=m.path, fingerprint=fingerprint_model(m), device=m.device, inode=m.inode)
        for m in models
    ]


class KaidongLikeTreeTests(unittest.TestCase):
    """S1 主验收项。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_kaidong_like_tree(Path(self._tmp.name))

    def test_the_fixture_yields_exactly_four_models_and_two_builds(self):
        result = scan_tree(self.root)

        self.assertEqual(len(result.models), EXPECTED_MODEL_COUNT, [str(m.path) for m in result.models])
        self.assertEqual(len(result.builds), EXPECTED_BUILD_COUNT, [str(b.binary) for b in result.builds])
        # 词表与体积阈值是真实噪声的主要来源，必须留下可解释的跳过记录。
        reasons = {record.reason for record in result.skipped}
        self.assertIn("vocab_file", reasons)
        self.assertIn("below_min_size", reasons)
        self.assertIn("excluded_dir", reasons)

    def test_scanning_twice_gives_the_same_answer(self):
        """同一棵树扫两遍必须逐字段相等，否则上层的「变化检测」会自己抖动。"""

        self.assertEqual(scan_tree(self.root), scan_tree(self.root))

    def test_every_model_has_a_stable_fingerprint(self):
        result = scan_tree(self.root)
        first = [fingerprint_model(m) for m in result.models]
        second = [fingerprint_model(m) for m in scan_tree(self.root).models]

        self.assertEqual(first, second)
        for value in first:
            size, _, digest = value.partition(":")
            self.assertTrue(size.isdigit(), value)
            self.assertEqual(len(digest), 64, value)

    def test_facts_never_crash_on_a_file_that_only_looks_like_a_model(self):
        """fixture 用 truncate 造稀疏文件，GGUF 内容全是零字节。

        按扩展名猜格式是发现层最容易犯的错：一个坏 GGUF 必须降级，而不是让
        整次扫描失败。HF 目录里的 config.json 是真的，所以它仍能给出 chat；
        带 mmproj 的那份 GGUF 仍应被判成 vision。
        """

        tasks = {}
        for model in scan_tree(self.root).models:
            with self.subTest(path=str(model.path)):
                facts = read_model(model)  # 不抛异常就是本测试的一半
                tasks[model.path.name] = facts.task
                if model.kind == "gguf" and model.mmproj is None:
                    self.assertEqual(facts.task, "unknown")
                    self.assertIn("error", facts.raw)

        self.assertIn("chat", tasks.values())
        self.assertIn("vision", tasks.values())

    def test_builds_are_enriched_without_executing_anything(self):
        """§5.5：版本与参数靠 ``--version`` / ``--help``，测试里注入假 runner。"""

        calls: list[list[str]] = []

        def fake_runner(argv, timeout):
            calls.append(list(argv))
            if "--version" in argv:
                return (0, "", "version: 9123 (deadbeef)\n")
            return (0, "--ctx-size N  size of the prompt context (default: 2048)\n", "")

        builds = [enrich(b, runner=fake_runner) for b in scan_tree(self.root).builds]

        self.assertEqual(len(builds), EXPECTED_BUILD_COUNT)
        self.assertTrue(all(b.version for b in builds), builds)
        self.assertTrue(all(b.formats for b in builds), builds)
        self.assertEqual(len(calls), 2 * EXPECTED_BUILD_COUNT)  # 每个 build 一次 version + 一次 help
        # formats 跟着框架走，而不是跟着文件扩展名走。
        by_framework = {b.framework: b.formats for b in builds}
        self.assertEqual(by_framework["llama.cpp"], ("gguf",))
        self.assertEqual(by_framework["vllm"], ("hf",))


class MovementTests(unittest.TestCase):
    """§5.3 的判定必须能从扫描结果直接算出来，不需要额外的账本。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_kaidong_like_tree(Path(self._tmp.name))

    def _gguf(self, models):
        return next(m for m in models if m.kind == "gguf")

    def test_rename_on_the_same_disk_keeps_the_model_identity(self):
        before = scan_tree(self.root)
        moved = self._gguf(before.models)
        ids = [f"model-{i}" for i in range(len(before.models))]
        origin_id = ids[before.models.index(moved)]

        target = moved.path.with_name("renamed-" + moved.path.name)
        os.replace(moved.path, target)
        after = scan_tree(self.root)

        result = match_previous(_previous(before.models, ids), _current(after.models))
        by_path = {str(o.current.path): o for o in result.outcomes if o.current is not None}

        self.assertEqual(by_path[str(target)].status, "moved_same_disk")
        self.assertEqual(by_path[str(target)].model_id, origin_id)

    def test_a_second_copy_becomes_a_second_location_not_a_new_model(self):
        before = scan_tree(self.root)
        original = self._gguf(before.models)
        ids = [f"model-{i}" for i in range(len(before.models))]
        origin_id = ids[before.models.index(original)]

        # 同体积、同首尾内容的稀疏文件 ⇒ 同指纹、不同 inode，正是「拷到别处」。
        copy_path = self.root / "copies" / original.path.name
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        with copy_path.open("wb") as handle:
            handle.truncate(original.size_bytes)
        after = scan_tree(self.root)

        result = match_previous(_previous(before.models, ids), _current(after.models))
        copied = [o for o in result.outcomes if o.status == "copied"]

        self.assertEqual(len(copied), 1, result.outcomes)
        self.assertEqual(copied[0].model_id, origin_id)
        self.assertEqual(copied[0].current.path, copy_path)

    def test_a_vanished_model_is_reported_missing(self):
        before = scan_tree(self.root)
        gone = self._gguf(before.models)
        ids = [f"model-{i}" for i in range(len(before.models))]

        gone.path.unlink()
        after = scan_tree(self.root)

        result = match_previous(_previous(before.models, ids), _current(after.models))
        missing = result.by_status("missing")
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].previous.path, gone.path)


if __name__ == "__main__":
    unittest.main()
