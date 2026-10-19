"""Offline public-tree safety gate.

The repository is public and was assembled from archived private sources.  This
suite pins the properties that make that safe:

* no private key material or live-looking credential ever enters the tree;
* no host identity from the archived private repositories is reintroduced;
* no content-body capture module or default path is reintroduced (legacy PR
  #17 is deliberately excluded, not merely disabled);
* no raw usage/log/database artifact is tracked outside the synthetic
  fixtures the repository already documents.

The scanner lives in ``tools/security/public_tree_scan.py`` and is deliberately
deterministic and offline: it reads tracked text files only, never a network or
a live credential.  Sensitive sample strings below are built at runtime so this
test file does not itself become the leak it is meant to catch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "tools" / "security" / "public_tree_scan.py"

# Runtime-built samples: the literal identifier never appears in this file.
PRIVATE_KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
GITHUB_TOKEN = "gh" + "p_" + "A" * 36
BEARER_TOKEN = "Bearer " + "A1b2" * 6
SYNTHETIC_PRIVATE_HOST = "synthetic" + "-private-node-01"
SYNTHETIC_PRIVATE_IP = "10." + "20.30.40"
DEVICE_REF = "device:" + "real-node-01"
REAL_HOME = "/" + "home/" + "realuser"


def digest(value: str) -> str:
    return hashlib.sha256(value.lower().encode()).hexdigest()


def scanner():
    """Import the scanner lazily so a missing module is a per-test failure."""

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from tools.security import public_tree_scan

    return public_tree_scan


class PublicTreeSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relpath: str, text: str) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self, *, paths=None):
        return scanner().scan_root(self.root, paths=paths)

    def rules(self, *, paths=None) -> list[str]:
        return [finding.rule for finding in self.scan(paths=paths)]

    # --- the repository itself ------------------------------------------------

    def test_the_tracked_public_tree_has_no_findings(self):
        findings = scanner().scan_root(REPO_ROOT)
        self.assertEqual([], findings, [f.as_dict() for f in findings])

    def test_reviewed_identifiers_are_stored_as_sha256_digests(self):
        known = scanner().KNOWN_PRIVATE_IDENTIFIER_SHA256
        self.assertTrue(known)
        for value in known:
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    # --- credential rules -----------------------------------------------------

    def test_detects_a_private_key_header(self):
        self.write("leak.txt", PRIVATE_KEY_HEADER + "\n")
        self.assertEqual(["private-key"], self.rules())

    def test_detects_a_github_token(self):
        self.write("leak.txt", "token=" + GITHUB_TOKEN + "\n")
        self.assertEqual(["credential"], self.rules())

    def test_detects_a_live_looking_bearer_token(self):
        self.write("leak.txt", BEARER_TOKEN + "\n")
        self.assertEqual(["credential"], self.rules())

    # --- host identity rules --------------------------------------------------

    def test_detects_a_reviewed_private_identifier_token(self):
        self.write("leak.md", "ran on " + SYNTHETIC_PRIVATE_HOST + "\n")
        findings = scanner().scan_root(
            self.root,
            known_identifier_digests=frozenset({digest(SYNTHETIC_PRIVATE_HOST)}),
        )
        self.assertEqual(["private-source-identifier"], [f.rule for f in findings])

    def test_detects_a_reviewed_private_ip_token(self):
        self.write("leak.md", "host " + SYNTHETIC_PRIVATE_IP + "\n")
        findings = scanner().scan_root(
            self.root,
            known_identifier_digests=frozenset({digest(SYNTHETIC_PRIVATE_IP)}),
        )
        self.assertEqual(["private-source-identifier"], [f.rule for f in findings])

    def test_detects_a_non_placeholder_device_reference(self):
        self.write("leak.md", DEVICE_REF + "\n")
        self.assertEqual(["device-reference"], self.rules())

    def test_detects_an_absolute_real_user_home_path(self):
        self.write("leak.md", REAL_HOME + "/models\n")
        self.assertEqual(["absolute-home-path"], self.rules())

    # --- rejected content capture --------------------------------------------

    def test_detects_a_content_capture_module(self):
        self.write("backend/contentlog.py", "CAPTURE = True\n")
        self.assertEqual(["content-capture"], self.rules())

    def test_detects_a_rejected_full_body_default(self):
        self.write("backend/capture.py", "full" + "_body_capture = True\n")
        self.assertEqual(["content-capture"], self.rules())

    def test_the_runtime_still_names_the_rejected_content_fields(self):
        gateway = (REPO_ROOT / "backend" / "observability" / "gateway.py").read_text(
            encoding="utf-8"
        )
        for field in ("prompt", "completion", "messages", "content"):
            self.assertIn('"' + field + '"', gateway)

    # --- raw artifacts --------------------------------------------------------

    def test_detects_a_raw_usage_artifact(self):
        self.write("usage.jsonl", '{"prompt": "x"}\n')
        self.assertEqual(["raw-artifact"], self.rules())

    def test_allows_the_documented_synthetic_fixtures(self):
        self.write("tests/backend/fixtures/usage.jsonl", '{"SYNTHETIC": true}\n')
        self.write("dev/fixtures/usage.jsonl", '{"SYNTHETIC": true}\n')
        self.assertEqual([], self.rules())

    # --- allow-listed documentation forms ------------------------------------

    def test_allows_placeholders_examples_and_documentation_ranges(self):
        self.write(
            "safe.md",
            "\n".join(
                (
                    "{{LAB_HOST}}",
                    "device:example-node",
                    "/home/alice/projects/llmlab",
                    "/home/bob",
                    "/home/carol",
                    "203.0.113.10",
                    "Bearer synthetic-test-token",
                    "Bearer not-in-the-directory",
                )
            )
            + "\n",
        )
        self.assertEqual([], self.rules())

    # --- path safety / fail closed -------------------------------------------

    def test_rejects_an_absolute_path(self):
        with self.assertRaises(scanner().UnsafePathError):
            self.scan(paths=["/" + "etc/passwd"])

    def test_rejects_parent_traversal(self):
        for relpath in (
            "../outside.txt",
            "sub/../../outside.txt",
            "./safe.txt",
            "a//b.txt",
        ):
            with self.subTest(relpath=relpath):
                with self.assertRaises(scanner().UnsafePathError):
                    self.scan(paths=[relpath])

    def test_rejects_a_symlink_leaf_without_reading_the_target(self):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name) / "outside.txt"
        outside.write_text(GITHUB_TOKEN + "\n", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)

        with self.assertRaises(scanner().UnsafePathError) as caught:
            self.scan(paths=["link.txt"])
        self.assertNotIn("gh", str(caught.exception).lower())

    def test_rejects_a_symlink_directory_component(self):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        (Path(outside_dir.name) / "secret.txt").write_text(
            GITHUB_TOKEN + "\n", encoding="utf-8"
        )
        (self.root / "linkdir").symlink_to(outside_dir.name, target_is_directory=True)

        with self.assertRaises(scanner().UnsafePathError):
            self.scan(paths=["linkdir/secret.txt"])

    def test_a_missing_path_fails_closed(self):
        with self.assertRaises(scanner().ScanReadError):
            self.scan(paths=["missing.txt"])

    def test_a_directory_path_fails_closed(self):
        (self.root / "subdir").mkdir()
        with self.assertRaises(scanner().ScanReadError):
            self.scan(paths=["subdir"])

    def test_an_unreadable_file_fails_closed(self):
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("POSIX non-root is required to exercise a permission failure")
        path = self.write("unreadable.txt", "x\n")
        path.chmod(0)
        try:
            with self.assertRaises(scanner().ScanReadError):
                self.scan(paths=["unreadable.txt"])
        finally:
            path.chmod(0o600)

    # --- strict placeholder validation ---------------------------------------

    def test_a_real_bearer_token_is_not_exempted_by_an_unrelated_placeholder(self):
        self.write("leak.md", BEARER_TOKEN + "  # {{TOKEN_GOES_HERE}}\n")
        self.assertEqual(["credential"], self.rules())

    def test_a_real_device_is_not_exempted_by_an_unrelated_placeholder(self):
        self.write("leak.md", DEVICE_REF + "  # {{DEVICE_ID}}\n")
        self.assertEqual(["device-reference"], self.rules())

    def test_a_real_home_is_not_exempted_by_an_unrelated_placeholder(self):
        self.write("leak.md", REAL_HOME + "/models  # {{HOME}}\n")
        self.assertEqual(["absolute-home-path"], self.rules())

    def test_a_placeholder_is_exempt_only_as_the_matched_value(self):
        self.write(
            "safe.md",
            "\n".join(
                (
                    "Bearer {{TOKEN}}",
                    "Bearer synthetic-test-token",
                    "device:{{DEVICE}}",
                    "device:example-node",
                    "/home/{{USER}}/models",
                    "/home/alice/models",
                )
            )
            + "\n",
        )
        self.assertEqual([], self.rules())

    # --- scope / history boundary --------------------------------------------

    def test_scan_scope_is_the_tracked_tree_not_git_history(self):
        text = SCANNER.read_text(encoding="utf-8").lower()
        for needle in ("git history", "gitleaks", "trufflehog", "tracked"):
            self.assertIn(needle, text)

    # --- CLI contract ---------------------------------------------------------

    def test_cli_exits_nonzero_and_reports_json_for_a_finding(self):
        self.write("leak.txt", GITHUB_TOKEN + "\n")
        proc = subprocess.run(
            [sys.executable, str(SCANNER), "--root", str(self.root), "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(1, len(payload["findings"]))
        self.assertEqual("credential", payload["findings"][0]["rule"])

    def test_cli_exits_zero_for_a_clean_tree(self):
        self.write("safe.md", "{{LAB_HOST}}\n")
        proc = subprocess.run(
            [sys.executable, str(SCANNER), "--root", str(self.root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_cli_fails_closed_for_an_unsafe_path(self):
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name) / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)
        proc = subprocess.run(
            [sys.executable, str(SCANNER), "--root", str(self.root), "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, proc.returncode, proc.stderr)
        self.assertIn("FAILED", proc.stderr)


if __name__ == "__main__":
    unittest.main()
