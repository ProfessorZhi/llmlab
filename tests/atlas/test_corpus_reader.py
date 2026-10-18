"""Gateway 语料的有界读取（契约 §8.3）。

契约 §8.3：「本模块只接受调用方给的**字符串**。真实接入需要 Gateway 侧的导出/
流式读取与背压策略。」

本文件锁定那份读取器的离线行为。两个反复出现的主题：

* **有界发生在读进内存之前** —— 与目录加载器同一条纪律，测试方式也相同（注入
  会谎报尺寸的探针，并指向一个不存在的路径：若实现先开文件就会得到别的原因）；
* **「读不出来」不等于「没有调用」** —— ``read()`` 用 ``available``/``reason``
  如实回答前者；而 ``__call__``（喂给 BFF 的那条 seam）在不可用时**抛错**，
  绝不悄悄返回空串。把两者混成一个空列表，就是把接线故障渲染成一次测量结果。
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

from tests.atlas import support

from backend.atlas.corpus import (
    REASON_FILE_MISSING,
    REASON_NOT_UTF8,
    REASON_OUTSIDE_ROOT,
    REASON_TOO_LARGE,
    CorpusLimits,
    FileCorpusReader,
    FileFacts,
)
from backend.atlas.errors import SourceUnavailable


class CorpusReaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _reader(self, path=None, **kwargs) -> FileCorpusReader:
        target = path if path is not None else self.root / "usage.jsonl"
        return FileCorpusReader(target, root=self.root, **kwargs)

    def test_reads_the_text_byte_exact(self):
        text = support.corpus_text()
        support.write_file(self.root / "usage.jsonl", text)

        read = self._reader().read()

        self.assertTrue(read.available)
        self.assertIsNone(read.reason)
        self.assertEqual(read.text, text)
        self.assertEqual(read.bytes_read, len(text.encode("utf-8")))

    def test_a_readable_but_empty_corpus_is_available_with_empty_text(self):
        """空文件是**可用**的来源：它诚实的结局是「没有样本」，由 ingest 定质量，
        而不是被本层提前判成「来源缺失」。"""
        support.write_file(self.root / "usage.jsonl", "")

        read = self._reader().read()

        self.assertTrue(read.available)
        self.assertEqual(read.text, "")
        self.assertEqual(read.bytes_read, 0)

    def test_a_missing_file_reports_unavailable_with_a_reason(self):
        read = self._reader().read()

        self.assertFalse(read.available)
        self.assertEqual(read.reason, REASON_FILE_MISSING)
        self.assertEqual(read.text, "")
        self.assertEqual(read.bytes_read, 0)

    def test_call_raises_instead_of_returning_an_empty_string(self):
        """这条 seam 直接喂给 BFF 的 ``gateway_text``；静默的空串会让接线故障看起来
        像「你最近没有调用」。"""
        with self.assertRaises(SourceUnavailable) as raised:
            self._reader()(None)

        self.assertEqual(raised.exception.reason, REASON_FILE_MISSING)

    def test_call_returns_the_text_when_available(self):
        text = support.corpus_text()
        support.write_file(self.root / "usage.jsonl", text)

        self.assertEqual(self._reader()(None), text)

    def test_refuses_an_oversized_corpus(self):
        support.write_file(self.root / "usage.jsonl", "x" * 4096)

        read = self._reader(limits=CorpusLimits(max_bytes=64)).read()

        self.assertFalse(read.available)
        self.assertEqual(read.reason, REASON_TOO_LARGE)

    def test_refuses_an_oversized_corpus_without_opening_it(self):
        """**尺寸门必须先于读取。** 探针谎报「存在、是常规文件、但有 1 GiB」，
        路径却指向一个**实际不存在**的位置：若实现先开文件，得到的会是别的失败原因，
        这条断言就看不到 ``too_large``。测的是顺序，不是结论。"""

        def probe(_path):
            return FileFacts(exists=True, is_regular=True, size=1 << 30)

        oversized = FileCorpusReader(
            self.root / "does-not-exist.jsonl",
            root=self.root,
            limits=CorpusLimits(max_bytes=64),
            fs_probe=probe,
        )

        read = oversized.read()

        self.assertFalse(read.available)
        self.assertEqual(read.reason, REASON_TOO_LARGE)

    def test_refuses_a_corpus_outside_the_root(self):
        sibling = tempfile.TemporaryDirectory()
        self.addCleanup(sibling.cleanup)
        outside = support.write_file(pathlib.Path(sibling.name) / "usage.jsonl", support.corpus_text())

        read = self._reader(path=outside).read()

        self.assertFalse(read.available)
        self.assertEqual(read.reason, REASON_OUTSIDE_ROOT)

    def test_refuses_non_utf8_bytes(self):
        path = self.root / "usage.jsonl"
        path.write_bytes(b"\xff\xfe not utf-8")

        read = self._reader(path=path).read()

        self.assertFalse(read.available)
        self.assertEqual(read.reason, REASON_NOT_UTF8)

    def test_identity_is_accepted_and_not_used_to_pick_the_file(self):
        """``gateway_text`` 的签名收 ``identity``。本 seam 的语料是**装配方选定的
        那一个文件**，身份不参与选择 —— 否则一个请求参数就能改变被读的语料。"""
        text = support.corpus_text()
        support.write_file(self.root / "usage.jsonl", text)

        reader = self._reader()
        self.assertEqual(reader(object()), text)
        self.assertEqual(reader.read(identity=object()).text, text)

    def test_limits_default_to_the_ingest_defaults(self):
        """默认上限必须与 ``ingest.IngestLimits`` 同值，否则 BFF 拿到的原文会先被
        ingest 以另一个上限拒掉，而两处的默认值不同这件事没有任何人会发现。"""
        from backend.atlas.ingest import IngestLimits

        limits = CorpusLimits()
        self.assertEqual(limits.max_bytes, IngestLimits().max_bytes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
