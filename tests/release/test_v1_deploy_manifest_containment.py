"""P0 regression: the deploy manifest must be deny-by-default and path-contained.

Two fail-open defects were found in `tools/release/deploy_manifest.mjs` at PR
#103 (`9190d30`) by source audit:

**A — the approved-root list was only enforced when non-empty.**
``validateManifest(doc, {allowedRoots = []})`` did
``else if (allowedRoots.length && !allowedRoots.includes(target.root))``. With
the list empty the whole check short-circuited, so omitting ``--allow-root``
accepted *any* ``target.root`` — the gate said yes precisely when it had been
told nothing.

**B — bundle entries were joined and read without containment.**
``verifyBundle`` did ``join(bundleDir, entry.path)`` and ``readFileSync`` on the
result. A manifest entry of ``../outside.txt`` — or an ordinary entry whose file
had been replaced by a symlink — made the verifier hash bytes from outside the
artifact and still report success.

Every test below is written to **fail on the old tool and pass on the fixed
one**: they are the payloads themselves, not a description of them. Point
``DEPLOY_MANIFEST_TOOL`` at an old copy to see the red.

Both defects are about *refusal*, so the assertions are on exit codes and on the
reason text, never on "it did not crash".

Everything runs on temporary fixtures. No host path outside ``tempfile`` is
written, no service is started, no Chrome, no network.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unicodedata
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DEPLOY = REPO / "tools" / "release" / "deploy_manifest.mjs"
#: Overridable so the same suite can be run against an older tool revision.
DEPLOY = pathlib.Path(os.environ.get("DEPLOY_MANIFEST_TOOL", str(DEFAULT_DEPLOY)))
NODE = shutil.which("node") or "node"

CANDIDATE_COMMIT = "5141b1b4ae3b966bf8df7cf1dba623a90421d68b"
ROOT = "/srv/atelier-v1-portal"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(REPO))


def manifest_for(files: list[tuple[str, bytes]]) -> dict:
    entries = [{"path": p, "bytes": len(b), "sha256": sha256(b)} for p, b in files]
    digest = sha256(("\n".join(f"{e['path']}:{e['sha256']}" for e in entries) + "\n").encode())
    return {
        "schema": "atelier-v1-static-release/1",
        "entry_points": ["index.html"],
        "file_count": len(entries),
        "total_bytes": sum(len(b) for _, b in files),
        "bundle_sha256": digest,
        "files": entries,
    }


def write_bundle(directory: pathlib.Path, files: list[tuple[str, bytes]]) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    for rel, data in files:
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    doc = manifest_for(files)
    (directory / "release-manifest.json").write_text(json.dumps(doc, indent=2) + "\n", "utf-8")
    return doc


class ContainmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="p0-containment-")
        cls.tmp = pathlib.Path(cls._tmp)
        cls.bundle = cls.tmp / "bundle"
        cls.manifest = write_bundle(cls.bundle, [
            ("index.html", b"<!doctype html><html></html>\n"),
            ("js/app.js", b"export const x = 1;\n"),
        ])
        cls.bundle_sha = cls.manifest["bundle_sha256"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ------------------------------------------------------------- helpers

    def build(self, bundle: pathlib.Path, out: pathlib.Path,
              known: str | None = None) -> subprocess.CompletedProcess:
        return run([
            NODE, str(DEPLOY), "build",
            "--bundle", str(bundle),
            "--commit", CANDIDATE_COMMIT,
            "--known", known or self.bundle_sha,
            "--out", str(out),
        ])

    def canary_doc(self) -> dict:
        out = self.tmp / "canary.json"
        result = self.build(self.bundle, out)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text("utf-8"))

    def check(self, doc: dict, *extra: str) -> subprocess.CompletedProcess:
        path = self.tmp / "candidate.json"
        path.write_text(json.dumps(doc), "utf-8")
        return run([NODE, str(DEPLOY), "check", str(path), *extra])

    def escape_bundle(self, entries: list[tuple[str, bytes]], outside: bytes) -> pathlib.Path:
        """A bundle whose manifest claims a file that lives outside it."""
        esc = self.tmp / f"escape-{len(list(self.tmp.iterdir()))}"
        write_bundle(esc, [("index.html", b"<!doctype html>\n")])
        doc = json.loads((esc / "release-manifest.json").read_text("utf-8"))
        for rel, data in entries:
            doc["files"].append({"path": rel, "bytes": len(data), "sha256": sha256(data)})
        doc["bundle_sha256"] = sha256(
            ("\n".join(f"{e['path']}:{e['sha256']}" for e in doc["files"]) + "\n").encode()
        )
        (esc / "release-manifest.json").write_text(json.dumps(doc, indent=2) + "\n", "utf-8")
        (self.tmp / "outside.txt").write_bytes(outside)
        return esc

    def assert_refused(self, result: subprocess.CompletedProcess, needle: str) -> None:
        self.assertEqual(result.returncode, 1, f"expected refusal, got {result.returncode}\n{result.stderr}")
        self.assertIn(needle, result.stderr)

    # ------------------------------------------------------ defect A: roots

    def test_omitting_allow_root_refuses_every_target(self) -> None:
        """The exact payload: a valid manifest, a foreign root, no --allow-root."""
        doc = self.canary_doc()
        doc["target"]["root"] = "/tmp/attacker-chosen-root"
        result = self.check(doc)  # deliberately no --allow-root
        self.assert_refused(result, "deny-by-default")

    def test_an_empty_allow_root_flag_is_refused(self) -> None:
        doc = self.canary_doc()
        result = self.check(doc, "--allow-root", "")
        self.assert_refused(result, "deny-by-default")

    def test_a_root_outside_the_approved_set_is_refused(self) -> None:
        doc = self.canary_doc()
        result = self.check(doc, "--allow-root", "/srv/some-other-project")
        self.assert_refused(result, "not an allowed root")

    def test_a_relative_root_is_refused(self) -> None:
        doc = self.canary_doc()
        doc["target"]["root"] = "srv/atelier-v1-portal"
        result = self.check(doc, "--allow-root", "srv/atelier-v1-portal")
        self.assert_refused(result, "must be absolute")

    def test_an_unnormalised_root_is_refused(self) -> None:
        doc = self.canary_doc()
        doc["target"]["root"] = ROOT + "/"
        result = self.check(doc, "--allow-root", ROOT + "/")
        self.assert_refused(result, 'must not end with "/"')

    def test_a_root_with_dotdot_is_refused(self) -> None:
        doc = self.canary_doc()
        doc["target"]["root"] = "/srv/atelier-v1-portal/../other"
        result = self.check(doc, "--allow-root", "/srv/atelier-v1-portal/../other")
        self.assert_refused(result, "not normalised")

    def test_the_filesystem_root_is_refused(self) -> None:
        doc = self.canary_doc()
        doc["target"]["root"] = "/"
        result = self.check(doc, "--allow-root", "/")
        self.assert_refused(result, "filesystem root")

    def test_the_approved_root_itself_is_accepted(self) -> None:
        """Deny-by-default must not have become deny-everything."""
        doc = self.canary_doc()
        result = self.check(doc, "--allow-root", ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    # ------------------------------------------- defect B: entry containment

    def test_a_dotdot_entry_is_refused(self) -> None:
        """The exact payload: a manifest entry that walks out of the bundle."""
        esc = self.escape_bundle([("../outside.txt", b"SECRET\n")], b"SECRET\n")
        result = self.build(esc, self.tmp / "dotdot.json", known="f" * 64)
        self.assert_refused(result, "..")
        self.assertIn("entry path has an empty", result.stderr)

    def test_an_absolute_entry_is_refused(self) -> None:
        esc = self.escape_bundle([("/etc/passwd", b"root:x:0:0\n")], b"root:x:0:0\n")
        self.assert_refused(self.build(esc, self.tmp / "abs.json"), "entry path is absolute")

    def test_a_backslash_entry_is_refused(self) -> None:
        esc = self.escape_bundle([("js\\..\\outside.txt", b"SECRET\n")], b"SECRET\n")
        self.assert_refused(self.build(esc, self.tmp / "bs.json"), "backslash")

    def test_a_drive_prefixed_entry_is_refused(self) -> None:
        esc = self.escape_bundle([("C:/outside.txt", b"SECRET\n")], b"SECRET\n")
        self.assert_refused(self.build(esc, self.tmp / "drive.json"), "drive or stream prefix")

    def test_a_non_nfc_entry_is_refused(self) -> None:
        decomposed = unicodedata.normalize("NFD", "caf\u00e9.css")
        self.assertNotEqual(decomposed, "caf\u00e9.css", "fixture must actually be decomposed")
        esc = self.escape_bundle([(decomposed, b"body{}\n")], b"body{}\n")
        self.assert_refused(self.build(esc, self.tmp / "nfc.json"), "NFC")

    def test_a_control_character_entry_is_refused(self) -> None:
        esc = self.escape_bundle([("js/\u0000app.js", b"x\n")], b"x\n")
        self.assert_refused(self.build(esc, self.tmp / "ctl.json"), "control character")

    def test_a_symlinked_entry_is_refused(self) -> None:
        """The second payload: a contained-looking path that is really a link."""
        sym = self.tmp / "bundle-symlink"
        shutil.copytree(self.bundle, sym)
        outside = self.tmp / "outside.txt"
        outside.write_bytes(b"SECRET-OUTSIDE\n")
        victim = sym / "js" / "app.js"
        victim.unlink()
        os.symlink(str(outside), victim)
        doc = json.loads((sym / "release-manifest.json").read_text("utf-8"))
        for entry in doc["files"]:
            if entry["path"] == "js/app.js":
                entry["bytes"] = outside.stat().st_size
                entry["sha256"] = sha256(outside.read_bytes())
        doc["bundle_sha256"] = sha256(
            ("\n".join(f"{e['path']}:{e['sha256']}" for e in doc["files"]) + "\n").encode()
        )
        (sym / "release-manifest.json").write_text(json.dumps(doc, indent=2) + "\n", "utf-8")

        result = self.build(sym, self.tmp / "sym.json", known=doc["bundle_sha256"])
        self.assert_refused(result, "symbolic link in path")

    def test_a_symlinked_directory_component_is_refused(self) -> None:
        """A link one level up is just as good an escape as a link on the file."""
        sym = self.tmp / "bundle-symlink-dir"
        shutil.copytree(self.bundle, sym)
        outside_dir = self.tmp / "outside-dir"
        outside_dir.mkdir()
        (outside_dir / "app.js").write_bytes(b"SECRET-DIR\n")
        shutil.rmtree(sym / "js")
        os.symlink(str(outside_dir), sym / "js")
        doc = json.loads((sym / "release-manifest.json").read_text("utf-8"))
        for entry in doc["files"]:
            if entry["path"] == "js/app.js":
                entry["bytes"] = len(b"SECRET-DIR\n")
                entry["sha256"] = sha256(b"SECRET-DIR\n")
        doc["bundle_sha256"] = sha256(
            ("\n".join(f"{e['path']}:{e['sha256']}" for e in doc["files"]) + "\n").encode()
        )
        (sym / "release-manifest.json").write_text(json.dumps(doc, indent=2) + "\n", "utf-8")

        result = self.build(sym, self.tmp / "symdir.json", known=doc["bundle_sha256"])
        self.assert_refused(result, "symbolic link in path")

    def test_a_symlinked_bundle_root_is_refused(self) -> None:
        link = self.tmp / "bundle-link"
        os.symlink(str(self.bundle), link)
        result = self.build(link, self.tmp / "rootlink.json")
        self.assert_refused(result, "symbolic link in path")

    def test_a_symlinked_manifest_file_is_refused(self) -> None:
        real = self.tmp / "real-canary.json"
        self.assertEqual(self.build(self.bundle, real).returncode, 0)
        link = self.tmp / "linked-canary.json"
        os.symlink(str(real), link)
        result = run([NODE, str(DEPLOY), "check", str(link), "--allow-root", ROOT])
        self.assert_refused(result, "symbolic link in path")

    # ------------------------------------------------- output location guard

    def test_writing_the_manifest_inside_the_repo_is_refused(self) -> None:
        out = REPO / "release-manifest-out.json"
        # The old tool really did write this file during the failing-before run;
        # start clean so the assertion measures this tool, not the residue.
        out.unlink(missing_ok=True)
        try:
            result = self.build(self.bundle, out)
            self.assert_refused(result, "refusing to write inside the repository work tree")
            self.assertFalse(out.exists(), "the tool must not have written into the work tree")
        finally:
            out.unlink(missing_ok=True)

    # ------------------------------------- no over-restriction of real assets

    def test_ordinary_names_still_verify(self) -> None:
        """Dots, dashes, underscores and NFC non-ASCII are legitimate."""
        bundle = self.tmp / "ordinary"
        files = [
            ("index.html", b"<!doctype html>\n"),
            ("js/render-sunburst.js", b"export const a = 1;\n"),
            ("css/V6.1_tokens.min.css", b"body{color:#000}\n"),
            ("assets/图表.css", "body{color:#111}\n".encode("utf-8")),
            ("data/a.b.c.json", b"{}\n"),
        ]
        doc = write_bundle(bundle, files)
        result = self.build(bundle, self.tmp / "ordinary.json", known=doc["bundle_sha256"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_real_production_bundle_still_verifies(self) -> None:
        """The 65-file artifact must not be collateral damage of the hardening."""
        out = self.tmp / "real-bundle"
        packed = run([NODE, str(REPO / "tools" / "release" / "build_static_bundle.mjs"),
                      "--out", str(out), "--json"])
        self.assertEqual(packed.returncode, 0, packed.stderr)
        digest = json.loads((out / "release-manifest.json").read_text("utf-8"))["bundle_sha256"]
        result = self.build(out, self.tmp / "real-canary.json", known=digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads((self.tmp / "real-canary.json").read_text("utf-8"))
        self.assertEqual(doc["candidate"]["bundle_sha256"], digest)
        self.assertGreater(doc["candidate"]["bundle_file_count"], 50)
        self.assertNotIn("qa.html", json.dumps(doc))


if __name__ == "__main__":
    unittest.main()
