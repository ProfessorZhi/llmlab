"""受信 Key Directory 的文档解析与有界加载（契约 §8.1）。

契约 §8.1 说得很直白：「现在由调用方注入内存对象。生产需要一个经审批的配置/
DB 适配器，并带轮换与吊销流程。**当前没有任何真实 key 目录。**」

本文件锁定的是那份适配器的**离线可测**部分：一份受信文档 -> ``KeyDirectory``。
它不访问 Dell、不读真实目录；所有输入都是本文件构造的虚构文档。

这里每一条拒绝测试的目的都是同一件事：**解析失败必须是拒绝，不能是「尽力而为」
的部分结果**。一份读了一半的目录会让「某个 key 归谁」悄悄变成「不知道」，
而下游所有授权判定都建立在这个答案上。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from tests.atlas import support

from backend.atlas.corpus import (
    KEY_DIRECTORY_SCHEMA_VERSION,
    REASON_DUPLICATE_KEY_ID,
    REASON_FILE_MISSING,
    REASON_INVALID_DOCUMENT,
    REASON_NOT_A_REGULAR_FILE,
    REASON_NOT_UTF8,
    REASON_OUTSIDE_ROOT,
    REASON_SCHEMA_MISMATCH,
    REASON_TOO_LARGE,
    REASON_TOO_MANY_BINDINGS,
    DirectoryLimits,
    FileFacts,
    load_key_directory,
    parse_key_directory_document,
)
from backend.atlas.errors import SourceUnavailable


class ParseDocumentTest(unittest.TestCase):
    def test_parses_a_valid_document_into_a_key_directory(self):
        loaded = parse_key_directory_document(support.dumps(support.directory_document()))

        self.assertEqual(loaded.revision, support.DEMO_REVISION)
        self.assertEqual(loaded.key_count, 4)
        self.assertEqual(loaded.revoked_count, 1)

        binding = loaded.directory.lookup("key-alpha")
        self.assertIsNotNone(binding)
        self.assertEqual(binding.principal, "u-1001")
        self.assertEqual(binding.project, "proj-a")
        self.assertEqual(binding.workload, "resume-parse")
        self.assertFalse(binding.revoked)

    def test_revoked_entries_survive_the_round_trip(self):
        """轮换靠 ``revoked`` 表达，所以加载后那把 key 必须仍可被查到且标记为已吊销。"""
        loaded = parse_key_directory_document(support.dumps(support.directory_document()))

        retired = loaded.directory.lookup("key-retired")
        self.assertIsNotNone(retired)
        self.assertTrue(retired.revoked)

    def test_a_binding_without_workload_loads_as_none(self):
        loaded = parse_key_directory_document(support.dumps(support.directory_document()))

        self.assertIsNone(loaded.directory.lookup("key-orphan").workload)

    def test_rejects_an_unknown_top_level_field(self):
        """白名单而非黑名单：多一个没人审批过的字段就是一份没审批过的文档。"""
        document = support.directory_document()
        document["rotation_note"] = "ops forgot to remove this"

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_rejects_a_schema_version_mismatch(self):
        """版本不符必须拒绝 —— 按 v1 的规则解析 v2 的文档会读出错误但看似成功的目录。"""
        document = support.directory_document(schema_version="atlas-key-directory/2")

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_SCHEMA_MISMATCH)

    def test_rejects_an_empty_revision(self):
        document = support.directory_document(revision="   ")

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_rejects_a_duplicate_key_id(self):
        """两把同 id 的 key 意味着「这把 key 归谁」没有唯一答案 —— 拒绝，不挑一个。"""
        document = support.directory_document()
        document["bindings"] = support.directory_document()["bindings"] + [
            {"key_id": "key-alpha", "principal": "u-9999", "project": "proj-z"}
        ]

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_DUPLICATE_KEY_ID)

    def test_rejects_a_binding_with_an_unknown_field(self):
        document = support.directory_document()
        document["bindings"] = [
            {"key_id": "key-alpha", "principal": "u-1001", "project": "proj-a",
             "x_principal": "u-9999"}
        ]

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_rejects_a_binding_with_an_empty_project(self):
        document = support.directory_document()
        document["bindings"] = [{"key_id": "key-alpha", "principal": "u-1001", "project": "  "}]

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_rejects_a_non_boolean_revoked_flag(self):
        """``revoked: "true"``（字符串）若被当成真值，一把吊销的 key 就会复活。"""
        document = support.directory_document()
        document["bindings"] = [
            {"key_id": "key-alpha", "principal": "u-1001", "project": "proj-a", "revoked": "true"}
        ]

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(support.dumps(document))

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_rejects_more_bindings_than_the_limit_allows(self):
        document = support.directory_document()
        document["bindings"] = [
            {"key_id": f"key-{index}", "principal": "u-1001", "project": "proj-a"}
            for index in range(5)
        ]

        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(
                support.dumps(document), limits=DirectoryLimits(max_bindings=4)
            )

        self.assertEqual(raised.exception.reason, REASON_TOO_MANY_BINDINGS)

    def test_rejects_text_that_is_not_json(self):
        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document("{ not json")

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_rejects_a_json_array_document(self):
        with self.assertRaises(SourceUnavailable) as raised:
            parse_key_directory_document(json.dumps([1, 2, 3]))

        self.assertEqual(raised.exception.reason, REASON_INVALID_DOCUMENT)

    def test_schema_version_constant_is_the_documented_one(self):
        self.assertEqual(KEY_DIRECTORY_SCHEMA_VERSION, "atlas-key-directory/1")


class LoadFromFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_loads_a_document_from_a_file_under_the_root(self):
        path = support.write_file(
            self.root / "directory.json", support.dumps(support.directory_document())
        )

        loaded = load_key_directory(path, root=self.root)

        self.assertEqual(loaded.key_count, 4)
        self.assertEqual(loaded.directory.lookup("key-gamma").project, "proj-b")

    def test_refuses_a_missing_file(self):
        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(self.root / "nope.json", root=self.root)

        self.assertEqual(raised.exception.reason, REASON_FILE_MISSING)

    def test_refuses_a_directory_instead_of_a_file(self):
        (self.root / "adirectory").mkdir()

        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(self.root / "adirectory", root=self.root)

        self.assertEqual(raised.exception.reason, REASON_NOT_A_REGULAR_FILE)

    def test_refuses_a_path_outside_the_root(self):
        """``../`` 穿越必须被挡：受信目录只能来自装配方指定的那棵树。"""
        outside = self._sibling_dir() / "elsewhere.json"
        support.write_file(outside, support.dumps(support.directory_document()))

        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(outside, root=self.root)

        self.assertEqual(raised.exception.reason, REASON_OUTSIDE_ROOT)

    def test_refuses_a_symlink_that_escapes_the_root(self):
        """一个位于 root 内、却指向 root 外的符号链接，等价于穿越。"""
        outside = support.write_file(
            self._sibling_dir() / "target.json", support.dumps(support.directory_document())
        )
        link = self.root / "link.json"
        link.symlink_to(outside)

        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(link, root=self.root)

        self.assertEqual(raised.exception.reason, REASON_OUTSIDE_ROOT)

    def test_refuses_an_oversized_file_without_opening_it(self):
        """**尺寸门必须先于读取。**

        注入一个谎报「存在、是常规文件、但有 1 GiB」的探针，并且把路径指向一个
        **实际不存在**的位置：若实现先开文件（会得到 FileNotFoundError → 另一种
        原因），这个断言就不会看到 ``too_large``。「有界」这句话的全部价值就在于
        在把字节读进内存**之前**拒绝，所以这里测的是顺序而不是结论。
        """
        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(
                self.root / "does-not-exist.json",
                root=self.root,
                limits=DirectoryLimits(max_bytes=64),
                fs_probe=lambda _path: FileFacts(exists=True, is_regular=True, size=1 << 30),
            )

        self.assertEqual(raised.exception.reason, REASON_TOO_LARGE)

    def _sibling_dir(self) -> pathlib.Path:
        """一棵位于 ``root`` **之外**的临时目录（与 root 同级，不共用 /tmp 里的固定名）。"""
        if not hasattr(self, "_sibling"):
            sibling = tempfile.TemporaryDirectory()
            self.addCleanup(sibling.cleanup)
            self._sibling = pathlib.Path(sibling.name)
        return self._sibling

    def test_refuses_an_oversized_file(self):
        path = support.write_file(self.root / "big.json", "x" * 4096)

        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(path, root=self.root, limits=DirectoryLimits(max_bytes=64))

        self.assertEqual(raised.exception.reason, REASON_TOO_LARGE)

    def test_refuses_non_utf8_bytes(self):
        path = self.root / "binary.json"
        path.write_bytes(b"\xff\xfe\x00\x00 not utf-8")

        with self.assertRaises(SourceUnavailable) as raised:
            load_key_directory(path, root=self.root)

        self.assertEqual(raised.exception.reason, REASON_NOT_UTF8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
