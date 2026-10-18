"""Regression tests for the release-gate verdict.

The defect these exist for: the verdict used to be inline YAML shell that only
*warned* when the browser was missing, then checked the CPU gates and exited 0.
A browser job that never ran reports its result as ``skipped``, which matched
neither the failure branch nor the success branch — so a run with **zero**
browser E2E printed "All runnable gates succeeded" and produced a green check
named "Gate verdict". Prose in the job summary cannot change a check's meaning;
only the exit code can.

So the decision now lives in ``.github/workflows/release-gate-verdict.sh`` and
these tests pin its exit codes for the four cases the review named — browser
passed / skipped / failed / detector errored — plus the surrounding cases that
would otherwise let a green result leak back in.

Every case is a subprocess call to that script. No network, no browser, no
third-party package.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
VERDICT = REPO / ".github" / "workflows" / "release-gate-verdict.sh"
WORKFLOW = REPO / ".github" / "workflows" / "atelier-v6-release-gates.yml"

GREEN_CPU = {"--design": "success", "--python": "success", "--node": "success"}


def run_verdict(*args: str) -> subprocess.CompletedProcess:
    """Invoke the verdict script with a deterministic environment.

    ``GITHUB_STEP_SUMMARY`` is removed so a test run can never append to a real
    step summary, and so the output is the same on a runner and on a laptop.
    """
    env = dict(os.environ)
    env.pop("GITHUB_STEP_SUMMARY", None)
    return subprocess.run(
        ["bash", str(VERDICT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def release(**overrides: str) -> subprocess.CompletedProcess:
    args = {"--mode": "release", **GREEN_CPU}
    args.update(overrides)
    flat = [item for pair in args.items() for item in pair]
    return run_verdict(*flat)


class ReleaseAcceptanceVerdictTests(unittest.TestCase):
    """`--mode release` must be fail-closed. Nothing short of real browser
    evidence may produce exit 0."""

    def test_passes_when_the_browser_ran_and_succeeded(self):
        r = release(**{"--browser-available": "true", "--browser-result": "success"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Verdict: PASS", r.stdout)

    def test_fails_when_the_browser_job_was_skipped(self):
        """The exact regression: browser missing must not be green."""
        r = release(**{"--browser-available": "false", "--browser-result": "skipped"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Verdict: FAIL", r.stdout)
        self.assertIn("no browser detected", r.stdout)

    def test_fails_when_browser_acceptance_failed(self):
        r = release(**{"--browser-available": "true", "--browser-result": "failure"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Verdict: FAIL", r.stdout)
        self.assertIn("did not succeed", r.stdout)

    def test_fails_when_the_detector_itself_errored(self):
        """A detector that never ran yields an empty output and a skipped
        browser job. That is not evidence of browser acceptance either."""
        r = release(**{"--browser-available": "", "--browser-result": "skipped"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Verdict: FAIL", r.stdout)
        self.assertIn("no browser detected", r.stdout)
        self.assertIn("<empty>", r.stderr)

    def test_fails_when_browser_acceptance_was_cancelled(self):
        r = release(**{"--browser-available": "true", "--browser-result": "cancelled"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_fails_when_browser_arguments_are_omitted_entirely(self):
        r = run_verdict("--mode", "release", *[x for p in GREEN_CPU.items() for x in p])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_fails_when_a_cpu_gate_failed_even_though_the_browser_passed(self):
        r = release(**{"--python": "failure", "--browser-available": "true", "--browser-result": "success"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Python regression", r.stderr)

    def test_fails_when_a_cpu_gate_was_skipped(self):
        """A skipped CPU gate is not a success either."""
        r = release(**{"--node": "skipped", "--browser-available": "true", "--browser-result": "success"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_reports_every_failing_gate_not_just_the_first(self):
        r = release(**{"--design": "failure", "--node": "skipped", "--browser-available": "false", "--browser-result": "skipped"})
        self.assertEqual(r.returncode, 1)
        for label in ("V5 frozen design", "Node regression", "no browser was detected"):
            with self.subTest(label=label):
                self.assertIn(label, r.stderr)

    def test_a_skipped_browser_is_never_described_as_a_pass(self):
        r = release(**{"--browser-available": "false", "--browser-result": "skipped"})
        self.assertIn("not a pass", r.stdout)
        self.assertNotIn("Browser acceptance | PASS", r.stdout)


class CpuUnitCheckVerdictTests(unittest.TestCase):
    """`--mode cpu` reports. It must not fail on browser absence, and it must
    still fail honestly when a CPU gate itself did not succeed."""

    def test_passes_with_the_browser_skipped(self):
        r = run_verdict("--mode", "cpu", *[x for p in GREEN_CPU.items() for x in p],
                        "--browser-available", "false", "--browser-result", "skipped")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Verdict: PASS", r.stdout)
        self.assertIn("SKIPPED", r.stdout)
        self.assertIn("not a pass", r.stdout)

    def test_passes_without_any_browser_arguments(self):
        r = run_verdict("--mode", "cpu", *[x for p in GREEN_CPU.items() for x in p])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_fails_when_a_cpu_gate_failed(self):
        r = run_verdict("--mode", "cpu", "--design", "success", "--python", "failure",
                        "--node", "success")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_the_exit_code_ignores_browser_state_entirely(self):
        """That is the whole point of the split: CPU unit checks report, the
        release-acceptance gate decides. A browser job that failed must not make
        the CPU-only report fail as well, or the two signals collapse back into
        one and the informative one can no longer be read on its own."""
        for available, result in (("true", "failure"), ("true", "cancelled"), ("", "skipped")):
            with self.subTest(browser_available=available, browser_result=result):
                r = run_verdict("--mode", "cpu", *[x for p in GREEN_CPU.items() for x in p],
                                "--browser-available", available, "--browser-result", result)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("Verdict: PASS", r.stdout)
                self.assertNotIn("Browser acceptance | PASS", r.stdout)


class VerdictUsageTests(unittest.TestCase):
    """A malformed invocation must not be mistaken for a passing gate."""

    def test_unknown_mode_is_a_usage_error(self):
        r = run_verdict("--mode", "banana", *[x for p in GREEN_CPU.items() for x in p])
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)

    def test_missing_mode_is_a_usage_error(self):
        r = run_verdict(*[x for p in GREEN_CPU.items() for x in p])
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)

    def test_unknown_argument_is_a_usage_error(self):
        r = run_verdict("--mode", "cpu", "--bogus", "1")
        self.assertEqual(r.returncode, 64, r.stdout + r.stderr)


class WorkflowWiringTests(unittest.TestCase):
    """The workflow must actually consult the fail-closed verdict."""

    def setUp(self):
        self.text = WORKFLOW.read_text("utf-8")

    def test_the_workflow_invokes_the_verdict_script_in_release_mode(self):
        self.assertIn("--mode release", self.text)
        self.assertIn("release-gate-verdict.sh", self.text)

    def test_a_fail_closed_release_acceptance_job_exists(self):
        self.assertIn("release-acceptance:", self.text)

    def test_the_informative_cpu_job_is_separate_from_release_acceptance(self):
        self.assertIn("cpu-unit-checks:", self.text)

    def test_the_lenient_verdict_wording_is_gone(self):
        """`All runnable gates succeeded` was printed on the green-with-no-browser
        path. Its absence is the regression guard."""
        self.assertNotIn("All runnable gates succeeded", self.text)

    def test_the_verdict_is_no_longer_decided_inline(self):
        """The decision must live in the tested script, not in YAML shell."""
        self.assertNotIn("::warning::browser acceptance SKIPPED", self.text)

    def test_still_read_only_cpu_only_and_secret_free(self):
        self.assertIn("contents: read", self.text)
        self.assertIn('CUDA_VISIBLE_DEVICES: ""', self.text)
        # Precise pattern, not a bare "secrets." substring: the header comment
        # legitimately says the file references no secrets, and asserting on the
        # bare prefix would flag that prose.
        self.assertIsNone(re.search(r"\bsecrets\.[A-Za-z_]", self.text))
        self.assertIsNone(re.search(r"\bsecrets\s*\[", self.text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
