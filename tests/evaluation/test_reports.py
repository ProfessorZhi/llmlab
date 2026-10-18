"""报告引用：只读安全路径、不泄密、内容永不外发。

三层防线各有独立的测试：

1. **形状**（纯字符串）——不依赖任何目录存在，因此可以逐条列举非法形态；
2. **落点**（需要根目录）——用 ``tempfile`` 里自己造的合成文件与软链接；
3. **内容**——本部署关闭，且断言这个关闭是**结构性**的。

另有一条跨层断言：**任何** DTO 里都不出现绝对路径。报告引用如果漏出根目录，
一次响应就会把服务器上的目录布局告诉浏览器。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.evaluation.errors import NotConfigured, ReportPathRejected
from backend.evaluation.reports import (
    REFERENCE_KNOWN,
    REFERENCE_NOT_CONFIGURED,
    REFERENCE_UNKNOWN,
    REPORT_EXTENSIONS,
    ReportRoot,
    normalize_report_ref,
    reference_digest,
    unconfigured_reference,
    unconfigured_root,
)
from tests.evaluation.support import DEP_VINCE_PRIVATE, build_harness, close_all

_LIVE = []


def setUpModule() -> None:  # noqa: N802
    _LIVE.append(build_harness())


def tearDownModule() -> None:  # noqa: N802
    _LIVE.clear()
    close_all()


class ShapeTests(unittest.TestCase):
    """第一层：路径语法。纯字符串，不碰文件系统。"""

    def test_a_plain_relative_reference_is_accepted(self) -> None:
        self.assertEqual(normalize_report_ref("runs/abc/report.json"), "runs/abc/report.json")

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        self.assertEqual(normalize_report_ref("  runs/a.json  "), "runs/a.json")

    def test_every_declared_extension_is_accepted(self) -> None:
        for extension in sorted(REPORT_EXTENSIONS):
            with self.subTest(extension=extension):
                self.assertTrue(normalize_report_ref(f"runs/a{extension}"))

    def test_traversal_and_absolute_forms_are_refused(self) -> None:
        bad = [
            "../secrets.json",
            "runs/../../etc/passwd.json",
            "/etc/passwd.json",
            "~/report.json",
            "runs/./report.json",
            "runs//report.json",
            "",
            "   ",
        ]
        for ref in bad:
            with self.subTest(ref=ref):
                with self.assertRaises(ReportPathRejected):
                    normalize_report_ref(ref)

    def test_windows_and_url_forms_are_refused(self) -> None:
        bad = [
            "runs\\report.json",
            r"C:\reports\a.json",
            "file:///etc/passwd.json",
            "http://example.invalid/a.json",
            "runs/a.json:stream",
        ]
        for ref in bad:
            with self.subTest(ref=ref):
                with self.assertRaises(ReportPathRejected):
                    normalize_report_ref(ref)

    def test_control_characters_are_refused(self) -> None:
        for ref in ("runs/a\x00.json", "runs/a\n.json", "runs/a\ttab.json", "runs/\x7f.json"):
            with self.subTest(ref=ref):
                with self.assertRaises(ReportPathRejected):
                    normalize_report_ref(ref)

    def test_the_extension_allowlist_is_enforced(self) -> None:
        for ref in ("runs/a.exe", "runs/a.sh", "runs/a.png", "runs/a", "runs/a.json.exe"):
            with self.subTest(ref=ref):
                with self.assertRaises(ReportPathRejected):
                    normalize_report_ref(ref)

    def test_an_overlong_reference_is_refused(self) -> None:
        with self.assertRaises(ReportPathRejected):
            normalize_report_ref("a" * 600 + ".json")
        with self.assertRaises(ReportPathRejected):
            normalize_report_ref("a" * 200 + ".json")

    def test_non_strings_are_refused(self) -> None:
        for ref in (None, 4, [], {}, True, Path("runs/a.json")):
            with self.subTest(ref=ref):
                with self.assertRaises(ReportPathRejected):
                    normalize_report_ref(ref)

    def test_the_digest_is_over_the_reference_not_the_content(self) -> None:
        """算内容就得读文件，而读文件正是要避免的。"""
        expected = hashlib.sha256(b"runs/a.json").hexdigest()
        self.assertEqual(reference_digest("runs/a.json"), expected)


class UnconfiguredRootTests(unittest.TestCase):
    """本部署的实际状态：没有接线。"""

    def test_an_unconfigured_reference_is_honest_about_it(self) -> None:
        reference = unconfigured_reference("runs/a.json")
        self.assertEqual(reference.state, REFERENCE_NOT_CONFIGURED)
        self.assertFalse(reference.available)
        self.assertEqual(reference.reason, "report_root_not_configured")
        self.assertEqual(reference.to_dict()["content_read"], False)

    def test_resolving_against_an_unconfigured_root_is_refused(self) -> None:
        root = unconfigured_root()
        self.assertFalse(root.configured)
        with self.assertRaises(NotConfigured):
            root.resolve("runs/a.json")

    def test_reading_content_is_disabled_everywhere(self) -> None:
        for root in (unconfigured_root(), ReportRoot(tempfile.mkdtemp())):
            with self.subTest(configured=root.configured):
                with self.assertRaises(NotConfigured) as ctx:
                    root.read_bytes("runs/a.json")
                self.assertEqual(ctx.exception.contract_code, "evaluation.not_configured")

    def test_the_root_description_never_echoes_the_path(self) -> None:
        secret = tempfile.mkdtemp()
        described = json.dumps(ReportRoot(secret).describe(), sort_keys=True)
        self.assertNotIn(secret, described)
        self.assertIn("disabled", described)


class ConfiguredRootTests(unittest.TestCase):
    """第二层：落点。用自己造的合成文件，不读任何真实报告。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root_path / "runs" / "abc").mkdir(parents=True)
        (self.root_path / "runs" / "abc" / "report.json").write_text(
            '{"synthetic": true}', encoding="utf-8"
        )
        self.root = ReportRoot(self.root_path)

    def test_a_legitimate_reference_resolves_inside_the_root(self) -> None:
        resolved = self.root.resolve("runs/abc/report.json")
        self.assertTrue(resolved.startswith(str(self.root_path.resolve())))

    def test_a_traversal_attempt_is_refused(self) -> None:
        outside = self.root_path.parent / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        with self.assertRaises(ReportPathRejected):
            self.root.resolve("../outside.json")

    def test_a_symlink_to_outside_is_refused(self) -> None:
        """``realpath`` 的包含性检查必须挡住软链接逃逸。"""
        outside_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside_dir, True)
        (outside_dir / "secret.json").write_text('{"real": true}', encoding="utf-8")
        link = self.root_path / "escape.json"
        try:
            os.symlink(outside_dir / "secret.json", link)
        except (OSError, NotImplementedError):  # pragma: no cover - 平台不支持符号链接
            self.skipTest("this platform does not support symlinks")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(ReportPathRejected):
            self.root.resolve("escape.json")

    def test_a_symlinked_directory_is_refused_too(self) -> None:
        outside_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside_dir, True)
        (outside_dir / "a.json").write_text("{}", encoding="utf-8")
        link = self.root_path / "linked"
        try:
            os.symlink(outside_dir, link, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("this platform does not support symlinks")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(ReportPathRejected):
            self.root.resolve("linked/a.json")

    def test_a_missing_file_is_reported_as_unknown_not_zero(self) -> None:
        reference = self.root.reference("runs/abc/absent.json")
        self.assertEqual(reference.state, REFERENCE_UNKNOWN)
        self.assertFalse(reference.available)
        self.assertEqual(reference.reason, "report_file_absent")
        self.assertIn("不是", reference.note or "")

    def test_an_existing_file_is_reported_as_known(self) -> None:
        reference = self.root.reference("runs/abc/report.json")
        self.assertEqual(reference.state, REFERENCE_KNOWN)
        self.assertTrue(reference.available)

    def test_a_sibling_directory_sharing_a_prefix_is_not_inside_the_root(self) -> None:
        """``/x/root-evil`` 不是 ``/x/root`` 的子路径。

        ``str.startswith`` 会把它们看成有关系；``commonpath`` 不会。这条断言把"看起来
        像前缀"与"真的是子路径"分开。
        """
        sibling = Path(str(self.root_path) + "-evil")
        sibling.mkdir()
        self.addCleanup(shutil.rmtree, sibling, True)
        (sibling / "a.json").write_text("{}", encoding="utf-8")
        link = self.root_path / "sibling.json"
        try:
            os.symlink(sibling / "a.json", link)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("this platform does not support symlinks")
        self.addCleanup(lambda: link.unlink(missing_ok=True))
        with self.assertRaises(ReportPathRejected):
            self.root.resolve("sibling.json")

    def test_the_filesystem_root_is_refused_as_a_report_root(self) -> None:
        """根目录是 ``/`` 时包含性检查恒真——那不是防线失效，是配置把整个文件系统
        当成了报告目录。拒绝它，而不是让一个恒真的检查看起来像一道防线。"""
        with self.assertRaises(ReportPathRejected) as ctx:
            ReportRoot(os.sep)
        self.assertIn("vacuous", str(ctx.exception))

    def test_a_relative_report_root_is_refused(self) -> None:
        with self.assertRaises(ReportPathRejected):
            ReportRoot("reports")

    def test_the_reference_never_carries_an_absolute_path(self) -> None:
        payload = json.dumps(self.root.reference("runs/abc/report.json").to_dict(), sort_keys=True)
        self.assertNotIn(str(self.root_path), payload)
        self.assertNotIn("/tmp", payload)


class DtoLeakTests(unittest.TestCase):
    """跨层：任何 DTO 里都不出现绝对路径或根目录。"""

    def test_no_dto_renders_an_absolute_path(self) -> None:
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, eval_type="latency",
                       dataset_slug="synthetic-qa-smoke")
        h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice")

        for path in (
            f"/api/evaluation/jobs/{job['job_id']}",
            f"/api/evaluation/jobs/{job['job_id']}/result",
            "/api/evaluation/monitoring",
            "/api/evaluation/health",
        ):
            with self.subTest(path=path):
                _status, _headers, body = h.request("GET", path, as_principal="alice")
                text = json.dumps(body, sort_keys=True)
                self.assertNotIn("/home/", text)
                self.assertNotIn("/tmp/", text)
                self.assertNotIn("/srv/", text)
                self.assertNotIn("/opt/", text)

    def test_an_unauthorized_caller_gets_no_report_reference(self) -> None:
        """这是回归测试：DTO 层曾经有一个"顺手补上引用"的回退分支，会绕过授权。"""
        h = _LIVE[0]
        job = h.submit("alice", deployment_id=DEP_VINCE_PRIVATE, eval_type="latency",
                       dataset_slug="synthetic-qa-smoke")
        h.request("POST", f"/api/evaluation/jobs/{job['job_id']}/mock-run", as_principal="alice")

        # 只有读权限、没有报告权限的主体。
        from backend.evaluation.identity import SCOPE_EVAL_READ, RequesterContext

        partial = RequesterContext(
            principal="alice", uid=4001, linux_user="alice",
            scopes=frozenset({SCOPE_EVAL_READ}), roles=frozenset({"member"}),
        )
        data = h.service.job(requester=partial, job_id=job["job_id"])["data"]
        report = data["result"]["report"]
        self.assertIsNone(report["ref"])
        self.assertFalse(report["available"])
        self.assertEqual(report["reason"], "report_reference_not_authorized")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
