"""Release-gate invariants for the Lane Release/Legacy #50 deliverables.

These tests are the executable half of the release lane: they check the claims
that are easy to make in a document and cheap to get wrong.

Three kinds of invariant live here:

1. **The design-provenance chain is intact.** ``prototypes/v5`` is the frozen
   design authority; ``prototypes/v6`` is *derived* from it; ``web/`` is
   *derived* from v6. Every digest below is written out again here rather than
   imported from the JavaScript tools. That duplication is the point: if only
   the tools were checked, editing a pinned constant inside a tool would move
   the goalposts silently. An independent copy makes such an edit fail.

2. **The CI files stay read-only and secret-free.** The lane is not allowed to
   introduce an environment secret or a workflow that can write to the
   repository, so the property is asserted over the whole workflow directory
   rather than trusted.

3. **The lane's own executable files cannot touch production.** The operator
   drill is a *plan printer*; a shell script that could stop a service is
   exactly the artefact this lane must not ship.

Nothing here reaches the network, spawns a service, or needs a third-party
package.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
LANE_DIR = pathlib.Path(__file__).resolve().parent

# Independent copies of the pinned digests. See the module docstring for why
# they are not imported from the tools that also pin them.
FROZEN_V5_SHA256 = "04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf"
# Re-pinned by the Phase B integration candidate (Issue #59): Release #51 wrote
# this against its own baseline 7fcc7d4f (V6 = a7ba7a6c…), and UI #54 then
# changed prototypes/v6/index.html, so the composed tree derives 883a16c8….
# The V6.1 frontend completion branch (Issue #56) then changed
# prototypes/v6/additions.{html,css}, so V6 derived bc145702….
# The V1 production-demo-gate branch (Issue #66, PR #86/#93) changed
# prototypes/v6/additions.html again, so V6 now derives b05e7544…. Release
# #69's packaging lane measured that from the source chain itself
# (`derive-from-v5.mjs --check`) rather than from a document, and re-pinned
# the three live literals — this constant, the CI literal and the runbook.
# The frozen V5 authority above is unchanged. Kept as a literal on purpose.
FROZEN_V6_SHA256 = "b05e754453edfb5238249957dd1e622d452bbff57cbdf951eb7d4ee5fa7f4573"

# The documents this lane is responsible for. They are new files: the existing
# docs/ files are shared audit material owned by other lanes.
PARITY_MATRIX = REPO / "docs" / "atelier-v6-legacy-parity-matrix-2026-10-16.md"
TRUST_BOUNDARY = REPO / "docs" / "atelier-v6-legacy-trust-boundary-2026-10-16.md"
HOSTING_AND_ROLLBACK = (
    REPO / "docs" / "atelier-v6-service-hosting-and-rollback-2026-10-16.md"
)
RELEASE_CHECKLIST = REPO / "docs" / "atelier-v6-release-checklist-2026-10-16.md"

CI_WORKFLOW = WORKFLOWS / "ci.yml"
FROZEN_FILE = REPO / "prototypes" / "FROZEN.sha256"
DRILL_SCRIPT = LANE_DIR / "drill-preview-rollback.sh"
ROLLBACK_PLAN = LANE_DIR / "preview-rollback-plan.txt"

STATUS_TOKENS = ("READY", "PARTIAL", "BLOCKED", "UNKNOWN")


def lf_sha256(path: pathlib.Path) -> str:
    """sha256 of ``path`` with CRLF folded to LF.

    A clone with ``core.autocrlf=true`` (Windows) checks the same committed blob
    out with CRLF, so hashing the raw bytes would report drift that is not there.
    """
    text = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(text).hexdigest()


class FrozenDesignChainTests(unittest.TestCase):
    """prototypes/v5 -> prototypes/v6 -> web/ is recomputed, not asserted."""

    def test_v5_prototype_matches_the_frozen_digest(self):
        self.assertEqual(
            lf_sha256(REPO / "prototypes" / "v5" / "index.html"), FROZEN_V5_SHA256
        )

    def test_v6_prototype_matches_its_pinned_digest(self):
        self.assertEqual(
            lf_sha256(REPO / "prototypes" / "v6" / "index.html"), FROZEN_V6_SHA256
        )

    def test_the_committed_sha256_file_pins_the_same_two_digests(self):
        """The CI gate reads ``prototypes/FROZEN.sha256`` via ``sha256sum -c``.

        That file is a third, independent copy of the two literals. If it is not
        cross-checked here it can drift from this module and from the tools, and
        the gate would then fail for a reason nobody can locate.
        """
        lines = [
            line.split()
            for line in FROZEN_FILE.read_text("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            lines,
            [
                [FROZEN_V5_SHA256, "prototypes/v5/index.html"],
                [FROZEN_V6_SHA256, "prototypes/v6/index.html"],
            ],
        )

    def test_both_tools_pin_the_same_frozen_v5_digest(self):
        derive = (REPO / "prototypes" / "v6" / "derive-from-v5.mjs").read_text("utf-8")
        extract = (
            REPO / "web" / "tools" / "extract-from-prototype.mjs"
        ).read_text("utf-8")
        for name, text in (("derive-from-v5.mjs", derive), ("extract-from-prototype.mjs", extract)):
            with self.subTest(tool=name):
                self.assertIn(FROZEN_V5_SHA256, text)

    def test_extract_tool_pins_the_v6_digest_that_derive_produces(self):
        extract = (
            REPO / "web" / "tools" / "extract-from-prototype.mjs"
        ).read_text("utf-8")
        self.assertIn(FROZEN_V6_SHA256, extract)

    def test_derivation_is_one_way_and_never_writes_the_v5_authority(self):
        """The 05 prototype is read-only by construction, not by discipline.

        ``derive-from-v5.mjs`` writes exactly one file (``OUT``). If a future
        edit repointed a write at the frozen source, regenerating the design
        authority would become possible from a script — the one thing the frozen
        file is there to prevent.
        """
        derive = (REPO / "prototypes" / "v6" / "derive-from-v5.mjs").read_text("utf-8")
        writes = re.findall(r"writeFileSync\(\s*([A-Za-z_$][\w$]*)", derive)
        self.assertEqual(writes, ["OUT"], f"unexpected write targets: {writes}")
        out_decl = re.search(r"const OUT = resolve\([^)]*\)", derive)
        self.assertIsNotNone(out_decl, "derive tool no longer declares an OUT target")
        self.assertNotIn("'v5'", out_decl.group(0))
        self.assertNotIn('"v5"', out_decl.group(0))


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


class CiWorkflowTests(unittest.TestCase):
    """There is exactly one entry workflow, and it splits the work by dependency.

    Two workflows that both ran ``unittest discover`` + ``npm test`` + the
    browser suite produced three copies of the same signal and one of them
    triggered on a branch that no longer exists. The replacement keeps the
    properties that mattered and pins the split that makes a failure locatable.
    """

    def setUp(self):
        self.assertTrue(
            CI_WORKFLOW.is_file(), f"missing {CI_WORKFLOW.relative_to(REPO)}"
        )
        self.text = CI_WORKFLOW.read_text("utf-8")

    def test_it_is_the_only_entry_workflow(self):
        self.assertEqual(
            sorted(p.name for p in WORKFLOWS.glob("*.yml")), ["ci.yml"]
        )

    def test_it_runs_on_main_pushes_and_pull_requests(self):
        self.assertIn("pull_request:", self.text)
        self.assertIn("branches: [main]", self.text)

    def test_it_is_pinned_cpu_only(self):
        self.assertIn('CUDA_VISIBLE_DEVICES: ""', self.text)

    def test_the_three_jobs_exist_and_are_named_for_their_dependency(self):
        for job in ("unit:", "node:", "browser:"):
            with self.subTest(job=job):
                self.assertRegex(self.text, rf"(?m)^  {re.escape(job)}$")

    def test_unit_job_has_no_browser_or_node_dependency(self):
        """The split exists because one Chrome test made the unit job 6 minutes.

        A browser import in ``unit`` is not a style problem: it turns a fast
        signal into a slow one and makes a Python failure look like an
        environment failure.
        """
        unit = self.text.split("  unit:", 1)[1].split("\n  node:", 1)[0]
        self.assertIn("unittest discover -s tests -t .", unit)
        for browser_marker in ("Chrome", "chromium", "CHROME_PATH", "smoke_static_bundle"):
            with self.subTest(marker=browser_marker):
                self.assertNotIn(browser_marker, unit)

    def test_browser_job_is_fail_closed_without_chrome(self):
        browser = self.text.split("  browser:", 1)[1]
        self.assertIn("Chrome/Chromium unavailable", browser)
        self.assertIn("exit 1", browser)
        self.assertIn("::error::", browser)

    def test_browser_job_runs_the_packaged_bundle_smoke(self):
        browser = self.text.split("  browser:", 1)[1]
        self.assertIn("build_static_bundle.mjs", browser)
        self.assertIn("smoke_static_bundle.mjs", browser)

    def test_frozen_digests_are_checked_with_sha256sum(self):
        self.assertIn("sha256sum -c prototypes/FROZEN.sha256", self.text)

    def test_runs_both_derivation_checks(self):
        self.assertIn("derive-from-v5.mjs --check", self.text)
        self.assertIn("extract-from-prototype.mjs --check", self.text)

    def test_no_job_is_allowed_to_continue_on_error(self):
        """A red job must stay red; the only suppression is the absence of one."""
        self.assertNotIn("continue-on-error", self.text)

    def test_concurrency_is_bounded(self):
        self.assertIn("concurrency:", self.text)
        self.assertIn("cancel-in-progress: true", self.text)


class LaneOwnedArtefactTests(unittest.TestCase):
    """The lane's executable artefacts cannot act on production."""

    # Every shell script this lane ships is checked, not just the obvious one:
    # a guard that only looks at the drill would miss a privileged command added
    # to a helper beside it. (``release-gate-verdict.sh`` used to be the second
    # entry; it was removed with the duplicate release-gates workflow — the
    # fail-closed property it encoded now lives in the browser job's exit code.)
    LANE_SCRIPTS = (DRILL_SCRIPT,)

    FORBIDDEN_COMMANDS = (
        "systemctl",
        "service ",
        "caddy",
        "pam_",
        "sudo ",
        "su -",
        "rm -rf",
        "/etc/systemd",
        "kill ",
        "iptables",
    )

    def test_no_lane_owned_shell_script_can_act_on_production(self):
        """A script that can stop a service is not an artefact this lane ships."""
        for path in self.LANE_SCRIPTS:
            with self.subTest(script=path.name):
                text = path.read_text("utf-8").lower()
                present = [cmd for cmd in self.FORBIDDEN_COMMANDS if cmd in text]
                self.assertEqual(present, [], f"{path.name} contains privileged commands: {present}")

    def test_lane_owned_shell_scripts_are_syntactically_valid(self):
        for path in self.LANE_SCRIPTS:
            with self.subTest(script=path.name):
                proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_every_lane_script_is_listed_in_the_guard(self):
        """The guard is only as good as its list, so the list is asserted.

        If the lane grows another script, this fails until it is added — rather
        than the new script silently escaping the privileged-command check.
        """
        on_disk = sorted(
            [p.name for p in WORKFLOWS.glob("*.sh")] + [p.name for p in LANE_DIR.glob("*.sh")]
        )
        self.assertEqual(sorted(p.name for p in self.LANE_SCRIPTS), on_disk)

    def test_the_drill_refuses_to_execute_without_separate_approval(self):
        text = DRILL_SCRIPT.read_text("utf-8")
        self.assertIn("--execute", text)
        self.assertIn("approval", text.lower())

    def test_the_printed_plan_exists_and_is_ordered(self):
        """The plan is data the drill prints, so it must be present and numbered."""
        lines = ROLLBACK_PLAN.read_text("utf-8").splitlines()
        numbered = [line for line in lines if re.match(r"^\d+\.\s+\S", line)]
        self.assertGreaterEqual(len(numbered), 5, "rollback plan lost its ordered steps")

    def test_the_plan_names_the_production_areas_it_must_not_touch(self):
        text = ROLLBACK_PLAN.read_text("utf-8").lower()
        for area in ("model", "gateway", "proxy", "authentication"):
            with self.subTest(area=area):
                self.assertIn(area, text)


class LaneOwnedDocumentTests(unittest.TestCase):
    """The lane's documents exist and stay internally consistent."""

    def test_all_lane_documents_exist(self):
        for path in (PARITY_MATRIX, TRUST_BOUNDARY, HOSTING_AND_ROLLBACK, RELEASE_CHECKLIST):
            with self.subTest(doc=path.name):
                self.assertTrue(path.is_file(), f"missing {path.relative_to(REPO)}")

    def test_hosting_document_pins_the_same_digests_as_the_tools(self):
        """The version pin in the runbook must agree with the code, or it is fiction."""
        text = HOSTING_AND_ROLLBACK.read_text("utf-8")
        self.assertIn(FROZEN_V5_SHA256, text)
        self.assertIn(FROZEN_V6_SHA256, text)

    def test_every_parity_matrix_row_carries_a_verdict(self):
        """No capability may be listed without saying whether it is done.

        Rows are keyed ``L01``…``Lnn``; a row without one of the status tokens
        would leave a legacy capability silently unmapped.
        """
        rows = [
            line
            for line in PARITY_MATRIX.read_text("utf-8").splitlines()
            if re.match(r"^\|\s*L\d+\s*\|", line)
        ]
        self.assertGreaterEqual(len(rows), 12, "parity matrix lost its capability rows")
        for row in rows:
            with self.subTest(row=row[:28]):
                self.assertTrue(
                    any(token in row for token in STATUS_TOKENS),
                    f"row has no verdict: {row}",
                )

    def test_no_lane_owned_file_contains_a_credential(self):
        """Greppable secret shapes must not reach GitHub from this lane."""
        patterns = [
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
            re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
            re.compile(r"(?i)\bpassword\s*[:=]\s*['\"][^'\"]{6,}['\"]"),
            re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*['\"][^'\"]{10,}['\"]"),
        ]
        owned = [
            PARITY_MATRIX,
            TRUST_BOUNDARY,
            HOSTING_AND_ROLLBACK,
            RELEASE_CHECKLIST,
            CI_WORKFLOW,
            FROZEN_FILE,
            DRILL_SCRIPT,
            ROLLBACK_PLAN,
            LANE_DIR / "test_release_gates.py",
        ]
        for path in owned:
            text = path.read_text("utf-8")
            for pattern in patterns:
                with self.subTest(file=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text), f"{path.name} looks like it holds a credential")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
