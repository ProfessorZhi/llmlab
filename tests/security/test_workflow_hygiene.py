"""Every workflow in the repository is read-only and secret-free.

This lived inside the retired Atelier release-gate suite (``tests/release/``).
The release lane is gone, but the property it guarded is not: this is a public
repository running workflows on pull requests, so a workflow that can write to
the repository, read the secrets context, or use ``pull_request_target`` is a
defect regardless of which page it builds.

Nothing here reaches the network or needs a third-party package.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"


class WorkflowHygieneTests(unittest.TestCase):
    """Every workflow in the repository is read-only and secret-free."""

    def test_the_workflow_directory_is_not_empty(self):
        # A guard against a refactor that moves the workflows and leaves these
        # assertions iterating over nothing, i.e. passing vacuously.
        self.assertTrue(sorted(WORKFLOWS.glob("*.yml")), "no workflows found")

    def test_no_workflow_references_a_secret(self):
        """No workflow may read the secrets context.

        The pattern requires a property or index access after ``secrets``
        (``secrets.NAME`` / ``secrets["x"]``) so that ordinary prose — a comment
        explaining that this file uses no secrets — is not mistaken for a use.
        """
        offenders = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text("utf-8")
            if re.search(r"\bsecrets\.[A-Za-z_]", text) or re.search(r"\bsecrets\s*\[", text):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"workflows reference secrets: {offenders}")

    def test_every_workflow_declares_read_only_contents_permission(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=path.name):
                text = path.read_text("utf-8")
                self.assertIn("permissions:", text)
                self.assertIn("contents: read", text)
                self.assertNotIn("contents: write", text)

    def test_no_workflow_uses_pull_request_target(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=path.name):
                self.assertNotIn("pull_request_target", path.read_text("utf-8"))

    def test_no_workflow_pushes_or_commits(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text("utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("git push", text)
                self.assertNotIn("git commit", text)

    def test_actions_are_pinned_to_a_major_version_tag(self):
        """Third-party actions are referenced by tag, never by a floating branch."""
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text("utf-8")
            for ref in re.findall(r"uses:\s*([^\s#]+)", text):
                if ref.startswith("./"):
                    continue
                with self.subTest(action=ref):
                    self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@v\d+$")


if __name__ == "__main__":
    unittest.main()
