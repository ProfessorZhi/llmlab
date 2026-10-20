"""Repository-wide regression for the current development boundary contract.

This is a docs assertion gate. It cannot attest Dell host/runtime/production status.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class CanonicalDevelopmentBoundaryTests(unittest.TestCase):
    def test_single_public_authority(self):
        text = read("docs/access-boundaries.md")
        self.assertIn("ProfessorZhi/llmlab", text)
        self.assertIn("唯一正式源码仓", text)
        self.assertNotIn("是**并列关系**", text)

    def test_dell_only_as_development_target(self):
        text = read("docs/access-boundaries.md")
        self.assertIn("Dell RTX A6000", text)
        self.assertIn("临时 worktree", text)
        self.assertNotIn("/home/alice/projects/llm-lab-portal", text)

    def test_old_repositories_are_archived(self):
        text = read("docs/access-boundaries.md")
        self.assertIn("Private + Archived", text)
        self.assertIn("ProfessorZhi/llm-lab-portal", text)
        self.assertIn("不在旧仓继续开发", text)

    def test_existing_services_stay_out_of_scope(self):
        text = read("docs/access-boundaries.md")
        for term in ("/srv/llm-lab/", "/opt/strata", "Caddy", "Gateway", "GPU", "systemd", "PAM"):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_development_is_not_production(self):
        text = read("docs/access-boundaries.md")
        self.assertIn("DEV_READY", text)
        self.assertIn("PRODUCTION_GO", text)
        self.assertIn("三位独立 Linux 主体", text)

    def test_readme_preserves_historical_state_and_current_boundary(self):
        text = read("README.md")
        self.assertIn("状态（2026-10-16）", text)
        self.assertIn("开发基线状态（2026-10-20）", text)
        self.assertIn("生产仍为 NO-GO", text)

    def test_pr_history_points_new_work_to_canonical_repo(self):
        text = read("docs/legacy/pr-history.md")
        self.assertIn("ProfessorZhi/llmlab", text)
        self.assertIn("Private + Archived", text)
        self.assertNotIn("New work happens on the branches of `llm-lab-portal`", text)


if __name__ == "__main__":
    unittest.main()
