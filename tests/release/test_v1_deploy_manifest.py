"""Dry-run gate for the canary/rollback manifest (Lane Release/QA #69, Wave-5).

`test_v1_static_bundle.py` proves what is *in* the artifact. This proves what a
manifest is allowed to *say* before an operator reads it — without running any
production shell, without touching a service, and without a host path outside a
temp directory.

The rules under test are the ones that make a runbook safe to hand over:

* the rollback target must be a **known-good** digest. A rollback manifest that
  names a digest nobody recorded is refused outright, because an unverifiable
  rollback is a second outage;
* a manifest cannot carry a command, authorise execution, point at a reserved
  production/upstream port, bind publicly, or escape the allowed roots;
* the same inputs produce the same bytes, so two operators can compare digests;
* the bundle is re-hashed from its own files, so a manifest cannot describe an
  artifact that has drifted.

Everything here is local and bounded: no network, no subprocess that starts a
service, no sudo, no Chrome. Bundles are built under ``tempfile.mkdtemp()``.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools" / "release"
PACKAGER = TOOLS / "build_static_bundle.mjs"
DEPLOY = TOOLS / "deploy_manifest.mjs"
REHEARSAL = TOOLS / "rehearsal"
NODE = shutil.which("node") or "node"

#: The commit the rehearsal manifests were generated from.
CANDIDATE_COMMIT = "5141b1b4ae3b966bf8df7cf1dba623a90421d68b"
#: Recorded previous release (built from PR #94's tree). Real, reproducible.
PREVIOUS_BUNDLE = "a2dc0ee92b74fa9d480313b93f51acf7e273016ff5a017b8293f472973cf384f"


def run(argv: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(REPO))


class ManifestHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="v1-deploy-manifest-")
        cls.tmp = pathlib.Path(cls._tmp)
        cls.bundle = cls.tmp / "bundle"
        built = run([NODE, str(PACKAGER), "--out", str(cls.bundle), "--json"])
        if built.returncode != 0:
            raise unittest.SkipTest(f"packager failed: {built.stderr}")
        cls.bundle_sha = json.loads(
            (cls.bundle / "release-manifest.json").read_text("utf-8")
        )["bundle_sha256"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ------------------------------------------------------------- helpers

    def build(self, out: pathlib.Path, known: str | None = None) -> subprocess.CompletedProcess:
        known = known or f"{PREVIOUS_BUNDLE},{self.bundle_sha}"
        return run([
            NODE, str(DEPLOY), "build",
            "--bundle", str(self.bundle),
            "--commit", CANDIDATE_COMMIT,
            "--known", known,
            "--out", str(out),
        ])

    def check(self, doc_or_path, *extra: str) -> subprocess.CompletedProcess:
        """Run `check`, supplying the manifest's own root as an approved root.

        `--allow-root` is required by the tool now (deny-by-default), so a test
        that wants to exercise *another* rule has to say which root it approves.
        A test that wants to prove the deny-by-default behaviour passes its own
        `--allow-root` (or none) explicitly.
        """
        path = doc_or_path
        doc = doc_or_path if isinstance(doc_or_path, dict) else json.loads(
            pathlib.Path(doc_or_path).read_text("utf-8"))
        if isinstance(doc_or_path, dict):
            path = self.tmp / "candidate.json"
            path.write_text(json.dumps(doc_or_path), "utf-8")
        argv = [NODE, str(DEPLOY), "check", str(path), *extra]
        if "--allow-root" not in extra:
            root = (doc.get("target") or {}).get("root")
            if root:
                argv += ["--allow-root", root]
        return run(argv)

    def canary(self) -> dict:
        out = self.tmp / "canary.json"
        result = self.build(out)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text("utf-8"))

    # -------------------------------------------------- determinism

    def test_the_same_candidate_produces_byte_identical_manifests(self) -> None:
        a, b = self.tmp / "a.json", self.tmp / "b.json"
        self.assertEqual(self.build(a).returncode, 0)
        self.assertEqual(self.build(b).returncode, 0)
        self.assertEqual(a.read_bytes(), b.read_bytes(),
                         "two builds of the same candidate must be identical")

    def test_the_manifest_is_key_order_stable(self) -> None:
        doc = self.canary()
        reserialized = json.dumps(dict(reversed(list(doc.items()))), indent=2, sort_keys=True) + "\n"
        path = self.tmp / "reserialized.json"
        path.write_text(reserialized, "utf-8")
        self.assertEqual(self.check(path).returncode, 0,
                         "key order must not change the verdict")

    # ------------------------------------------------ integrity / allowlist

    def test_the_bundle_digest_is_recomputed_from_its_files(self) -> None:
        doc = self.canary()
        self.assertRegex(doc["candidate"]["bundle_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(doc["candidate"]["bundle_sha256"], self.bundle_sha)
        self.assertGreater(doc["candidate"]["bundle_file_count"], 50)

    def test_a_tampered_bundle_is_refused(self) -> None:
        tampered = self.tmp / "tampered"
        shutil.copytree(self.bundle, tampered)
        target = tampered / "index.html"
        target.write_text(target.read_text("utf-8") + "\n<!-- drift -->\n", "utf-8")
        out = self.tmp / "tampered.json"
        result = run([
            NODE, str(DEPLOY), "build",
            "--bundle", str(tampered), "--commit", CANDIDATE_COMMIT,
            "--known", f"{PREVIOUS_BUNDLE},{self.bundle_sha}", "--out", str(out),
        ])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("content changed", result.stderr)

    def test_a_missing_file_is_refused(self) -> None:
        holed = self.tmp / "holed"
        shutil.copytree(self.bundle, holed)
        (holed / "js" / "app.js").unlink()
        out = self.tmp / "holed.json"
        result = run([
            NODE, str(DEPLOY), "build",
            "--bundle", str(holed), "--commit", CANDIDATE_COMMIT,
            "--known", f"{PREVIOUS_BUNDLE},{self.bundle_sha}", "--out", str(out),
        ])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # The containment walk reports the missing component by path.
        self.assertIn("missing path component", result.stderr)
        self.assertIn("js/app.js", result.stderr)

    def test_the_manifest_never_names_a_qa_artifact(self) -> None:
        blob = json.dumps(self.canary())
        self.assertNotIn("qa.html", blob)
        self.assertNotIn("qa2.html", blob)

    # ------------------------------------------------- rollback refusal

    def test_a_rollback_to_an_unknown_digest_is_refused(self) -> None:
        doc = self.canary()
        doc["kind"] = "rollback"
        doc["rollback"] = {"method": "symlink-swap",
                           "to_bundle_sha256": "f" * 64,
                           "validated": True}
        result = self.check(doc)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not in known_good_bundles", result.stderr)
        self.assertIn("refusing an unknown SHA", result.stderr)

    def test_a_rollback_to_the_candidate_itself_is_refused(self) -> None:
        doc = self.canary()
        doc["kind"] = "rollback"
        doc["rollback"] = {"method": "symlink-swap",
                           "to_bundle_sha256": doc["candidate"]["bundle_sha256"],
                           "validated": True}
        result = self.check(doc)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("must target a different bundle", result.stderr)

    def test_an_unrehearsed_rollback_is_refused(self) -> None:
        doc = self.canary()
        doc["kind"] = "rollback"
        doc["rollback"] = {"method": "symlink-swap",
                           "to_bundle_sha256": PREVIOUS_BUNDLE,
                           "validated": False}
        result = self.check(doc)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("rollback.validated", result.stderr)

    # --------------------------------------------------- safety surface

    def test_reserved_ports_are_refused(self) -> None:
        for port in (8443, 18443, 8080, 8123):
            with self.subTest(port=port):
                doc = self.canary()
                doc["service"]["port"] = port
                result = self.check(doc)
                self.assertEqual(result.returncode, 1)
                self.assertIn("reserved production/upstream port", result.stderr)

    def test_a_public_bind_is_refused(self) -> None:
        doc = self.canary()
        doc["service"]["bind"] = "0.0.0.0"
        result = self.check(doc)
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be 127.0.0.1", result.stderr)

    def test_an_unapproved_root_is_refused(self) -> None:
        doc = self.canary()
        result = self.check(doc, "--allow-root", "/srv/some-other-project")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an allowed root", result.stderr)

    def test_the_allowed_root_is_accepted(self) -> None:
        doc = self.canary()
        result = self.check(doc, "--allow-root", doc["target"]["root"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_rehearsal_must_declare_non_production(self) -> None:
        doc = self.canary()
        doc["target"]["non_production"] = False
        result = self.check(doc, "--rehearsal")
        self.assertEqual(result.returncode, 1)
        self.assertIn("non_production", result.stderr)

    def test_every_approval_gate_must_be_named(self) -> None:
        for gate in ("pam-root-uid", "gateway-strata", "caddy-systemd-8443", "owner-go"):
            with self.subTest(gate=gate):
                doc = self.canary()
                doc["approval_gates"] = [g for g in doc["approval_gates"] if g != gate]
                result = self.check(doc)
                self.assertEqual(result.returncode, 1)
                self.assertIn(gate, result.stderr)

    def test_a_manifest_may_not_authorise_execution(self) -> None:
        doc = self.canary()
        doc["execution_authorized"] = True
        result = self.check(doc)
        self.assertEqual(result.returncode, 1)
        self.assertIn("execution_authorized", result.stderr)

    def test_a_command_shaped_field_is_refused(self) -> None:
        doc = self.canary()
        doc["service"]["command"] = "systemctl restart atelier-v1-portal-canary"
        result = self.check(doc)
        self.assertEqual(result.returncode, 1)
        self.assertIn("command-shaped", result.stderr)

    def test_a_candidate_digest_missing_from_the_known_set_is_refused_at_build(self) -> None:
        out = self.tmp / "noknown.json"
        result = self.build(out, known=PREVIOUS_BUNDLE)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not in --known", result.stderr)

    # ---------------------------------------------- the release tools are inert

    def test_release_tools_contain_no_shell_out(self) -> None:
        """These tools must not be able to run a privileged command by accident."""
        # Match an *invocation shape*, not the bare word: the tool legitimately
        # carries the strings "sudo"/"systemctl" in its command-key denylist, and
        # a scan that flags its own guard would be useless.
        banned = re.compile(
            r"child_process|execSync\s*\(|spawnSync\s*\(|execFileSync\s*\("
            r"|\bsudo\s|\bsystemctl\s|\bcaddy\s+(?:reload|run|stop)"
        )
        for path in sorted(TOOLS.glob("*.mjs")):
            text = path.read_text("utf-8")
            self.assertIsNone(banned.search(text), f"{path.name} contains a privileged/shell construct")

    def test_the_committed_rehearsal_manifests_validate(self) -> None:
        for name in ("canary.rehearsal.json", "rollback.rehearsal.json"):
            with self.subTest(manifest=name):
                path = REHEARSAL / name
                self.assertTrue(path.is_file(), f"missing {name}")
                doc = json.loads(path.read_text("utf-8"))
                self.assertEqual(doc["candidate"]["source_commit"], CANDIDATE_COMMIT)
                result = self.check(path, "--allow-root", doc["target"]["root"])
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_committed_rollback_names_a_recorded_previous_release(self) -> None:
        doc = json.loads((REHEARSAL / "rollback.rehearsal.json").read_text("utf-8"))
        self.assertEqual(doc["kind"], "rollback")
        self.assertEqual(doc["rollback"]["to_bundle_sha256"], PREVIOUS_BUNDLE)
        self.assertIn(PREVIOUS_BUNDLE, doc["known_good_bundles"])
        self.assertNotEqual(doc["rollback"]["to_bundle_sha256"],
                            doc["candidate"]["bundle_sha256"])


if __name__ == "__main__":
    unittest.main()
