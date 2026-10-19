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

import json
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
KNOWN_HOST_ID = "tokyo" + "-shared-dev-01"
OLD_LAB_IP = "192.168." + "32.179"
DEVICE_REF = "device:" + "real-node-01"
REAL_HOME = "/" + "home/" + "realuser"


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

    def test_scanner_source_does_not_reintroduce_a_known_identifier(self):
        text = SCANNER.read_text(encoding="utf-8")
        for identifier in (KNOWN_HOST_ID, OLD_LAB_IP):
            self.assertNotIn(identifier, text)

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

    def test_detects_a_known_private_source_identifier(self):
        self.write("leak.md", "ran on " + KNOWN_HOST_ID + "\n")
        self.assertEqual(["private-source-identifier"], self.rules())

    def test_detects_the_old_private_lab_ip(self):
        self.write("leak.md", "host " + OLD_LAB_IP + "\n")
        self.assertEqual(["private-source-identifier"], self.rules())

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


if __name__ == "__main__":
    unittest.main()
