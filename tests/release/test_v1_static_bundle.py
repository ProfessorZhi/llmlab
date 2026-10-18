"""Release/packaging gate for the V1 static bundle (Lane Release/Packaging #69).

The repository had no release artifact allowlist. ``web/`` is the front-end
*source* tree: it also holds the QA preview (``web/qa.html``), the generator and
dev server (``web/tools/**``), a package manifest and a README. Serving that
directory — which is exactly what ``web/tools/serve.mjs`` does for local review —
therefore also serves the QA build.

These tests pin the properties that make the packaged bundle safe to hand to an
operator, and they do it by **running the packager**, not by reading it:

* the bundle is the entry-point closure and nothing else;
* ``qa.html``, build tooling, manifests, docs, fixtures and logs never ship;
* every reference inside the bundle resolves to a file in the bundle;
* a page may not ship declaring a non-production build profile;
* a *future* QA-style entry cannot be auto-published — the build fails instead;
* the same input produces the same bytes, so a hash identifies a release.

Nothing here touches the network, spawns a server, needs Chrome, or writes into
the repository. Every bundle is built under ``tempfile.mkdtemp()``.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "release" / "build_static_bundle.mjs"
ALLOWLIST = REPO / "tools" / "release" / "release-allowlist.json"
SOURCE_ROOT = REPO / "web"
NODE = shutil.which("node") or "node"

#: Paths that must never appear in a published bundle.
FORBIDDEN_EXACT = [
    "qa.html",
    "README.md",
    "package.json",
    "tools",
    "tools/serve.mjs",
    "tools/extract-from-prototype.mjs",
]
FORBIDDEN_PATTERNS = [
    re.compile(r"(^|/)qa[^/]*\.html$"),
    re.compile(r"(^|/)package\.json$"),
    re.compile(r"(^|/)[^/]*\.md$"),
    re.compile(r"(^|/)[^/]*\.test\.(js|mjs|py)$"),
    re.compile(r"(^|/)fixtures?/"),
    re.compile(r"(^|/)[^/]*\.log$"),
    re.compile(r"(^|/)[^/]*\.map$"),
]


def run_build(source: pathlib.Path, out: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(TOOL), "--source", str(source), "--out", str(out), "--json"],
        capture_output=True, text=True, timeout=120, cwd=str(REPO),
    )


def bundle_paths(root: pathlib.Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
    )


def manifest_of(out: pathlib.Path) -> dict:
    return json.loads((out / "release-manifest.json").read_text(encoding="utf-8"))


class StaticBundleTests(unittest.TestCase):
    """Build the real bundle once per test class and assert over the artifact."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="v1-bundle-")
        cls.tmp = pathlib.Path(cls._tmp)
        cls.out = cls.tmp / "bundle"
        cls.result = run_build(REPO, cls.out)
        cls.manifest = manifest_of(cls.out) if cls.result.returncode == 0 else None

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ---------------------------------------------------------------- build

    def test_the_packager_builds_the_current_tree(self) -> None:
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.assertIsNotNone(self.manifest)
        self.assertEqual(self.manifest["schema"], "atelier-v1-static-release/1")

    def test_both_entry_points_are_published(self) -> None:
        published = bundle_paths(self.out)
        self.assertIn("index.html", published)
        self.assertIn("auth/index.html", published)
        self.assertEqual(
            sorted(self.manifest["entry_points"]), ["auth/index.html", "index.html"],
        )

    # ------------------------------------------------------------- denylist

    def test_no_denied_artifact_is_published(self) -> None:
        published = bundle_paths(self.out)
        for exact in FORBIDDEN_EXACT:
            self.assertNotIn(exact, published, f"{exact} must not be published")
        for rel in published:
            for pattern in FORBIDDEN_PATTERNS:
                self.assertIsNone(
                    pattern.search(rel), f"{rel} matches a forbidden pattern ({pattern.pattern})",
                )

    def test_the_qa_preview_exists_in_source_but_is_not_published(self) -> None:
        # The whole point: the file is there, and the packager still refuses it.
        self.assertTrue((SOURCE_ROOT / "qa.html").is_file())
        self.assertNotIn("qa.html", bundle_paths(self.out))

    def test_nothing_outside_the_web_tree_is_published(self) -> None:
        published = bundle_paths(self.out)
        allowed = {"release-manifest.json"}
        extra = [p for p in published if p not in allowed and not (SOURCE_ROOT / p).is_file()]
        self.assertEqual(extra, [], f"bundle contains paths absent from web/: {extra}")

    # ---------------------------------------------------- entry closure

    def test_every_reference_resolves_inside_the_bundle(self) -> None:
        """No 404 at runtime: every local reference points at a published file."""
        published = set(bundle_paths(self.out))
        missing: list[str] = []
        html_ref = re.compile(r'\b(?:src|href)\s*=\s*["\']([^"\']+)["\']', re.I)
        js_ref = re.compile(r'\b(?:import|export)\b[^;]*?\bfrom\s*["\']([^"\']+)["\']')
        js_side = re.compile(r'\bimport\s+["\']([^"\']+)["\']')
        css_ref = re.compile(r'\burl\(\s*["\']?([^"\')]+?)["\']?\s*\)')
        css_import = re.compile(r'@import\s+(?:url\(\s*)?["\']([^"\']+)["\']')
        skip = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)

        for rel in published:
            if rel == "release-manifest.json":
                continue
            text = (self.out / rel).read_text(encoding="utf-8", errors="replace")
            if rel.endswith(".html"):
                refs = html_ref.findall(text)
            elif rel.endswith((".js", ".mjs")):
                refs = js_ref.findall(text) + js_side.findall(text)
            elif rel.endswith(".css"):
                refs = css_ref.findall(text) + css_import.findall(text)
            else:
                refs = []
            base = pathlib.PurePosixPath(rel).parent
            for raw in refs:
                if skip.match(raw):
                    continue
                target = (base / raw.split("?")[0].split("#")[0])
                target = pathlib.PurePosixPath(*target.parts)
                # normalise `a/../b`
                parts: list[str] = []
                for part in target.parts:
                    if part == "..":
                        if not parts:
                            missing.append(f"{rel} -> {raw} (escapes the bundle)")
                            break
                        parts.pop()
                    elif part != ".":
                        parts.append(part)
                else:
                    if "/".join(parts) not in published:
                        missing.append(f"{rel} -> {raw}")
        self.assertEqual(missing, [], f"dangling references: {missing}")

    def test_the_demo_fixture_ships_because_the_app_imports_it(self) -> None:
        """It must not be deleted blindly: removing it breaks production boot."""
        published = bundle_paths(self.out)
        self.assertIn("js/data/demo-fixture.js", published)
        # …and the gate that makes it inert is shipped with it.
        self.assertIn("js/build-profile.js", published)
        self.assertIn("js/demo-mode.js", published)
        gate = (self.out / "js" / "demo-mode.js").read_text(encoding="utf-8")
        self.assertIn("isQaBuild", gate, "the sample gate must be profile-based, not URL-based")

    # --------------------------------------------------- build profile

    def test_no_published_page_declares_a_non_production_profile(self) -> None:
        for rel in bundle_paths(self.out):
            if not rel.endswith(".html"):
                continue
            html = (self.out / rel).read_text(encoding="utf-8")
            match = re.search(r'<html\b[^>]*\bdata-build-profile\s*=\s*["\']([^"\']*)["\']', html, re.I)
            if match is not None:
                self.assertEqual(
                    match.group(1), "production",
                    f"{rel} declares data-build-profile={match.group(1)!r}",
                )

    def test_the_main_entry_declares_production(self) -> None:
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-build-profile="production"', html)
        self.assertNotIn('data-build-profile="qa"', html)

    def test_undeclared_profiles_are_recorded_in_the_manifest(self) -> None:
        """An absent attribute is fail-closed at runtime, but must be visible."""
        self.assertIn("undeclared_profile", self.manifest)
        for rel in self.manifest["undeclared_profile"]:
            html = (self.out / rel).read_text(encoding="utf-8")
            self.assertNotRegex(html, r'data-build-profile\s*=', f"{rel} actually declares a profile")

    # ----------------------------------------------------------- manifest

    def test_manifest_hashes_match_the_published_bytes(self) -> None:
        import hashlib

        for entry in self.manifest["files"]:
            data = (self.out / entry["path"]).read_bytes()
            self.assertEqual(entry["bytes"], len(data), entry["path"])
            self.assertEqual(
                entry["sha256"], hashlib.sha256(data).hexdigest(), entry["path"],
            )

    def test_the_bundle_hash_is_stable_across_builds(self) -> None:
        second = self.tmp / "bundle-2"
        result = run_build(REPO, second)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            manifest_of(second)["bundle_sha256"], self.manifest["bundle_sha256"],
            "the same source must produce the same release hash",
        )
        self.assertEqual(bundle_paths(second), bundle_paths(self.out))

    def test_the_manifest_lists_every_published_file(self) -> None:
        listed = {e["path"] for e in self.manifest["files"]}
        published = set(bundle_paths(self.out)) - {"release-manifest.json"}
        self.assertEqual(listed, published)


class PackagerRefusalTests(unittest.TestCase):
    """The packager must fail, not silently skip, when the tree stops being safe."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="v1-bundle-refuse-")
        self.tmp = pathlib.Path(self._tmp)
        self.source = self.tmp / "checkout"
        shutil.copytree(SOURCE_ROOT, self.source / "web")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def build(self, out_name: str = "out") -> subprocess.CompletedProcess:
        return run_build(self.source, self.tmp / out_name)

    def test_a_future_qa_entry_linked_from_index_is_refused(self) -> None:
        """The exact regression this gate exists for: a new QA page auto-published."""
        (self.source / "web" / "qa2.html").write_text(
            '<!doctype html><html data-build-profile="qa"><body>qa</body></html>',
            encoding="utf-8",
        )
        index = self.source / "web" / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "</body>", '<a href="./qa2.html">qa</a></body>',
            ),
            encoding="utf-8",
        )
        result = self.build()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("unapproved HTML entry", result.stderr)
        self.assertFalse((self.tmp / "out" / "qa2.html").exists())

    def test_a_denied_file_needed_by_the_closure_is_refused(self) -> None:
        index = self.source / "web" / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "</body>", '<script src="./tools/serve.mjs"></script></body>',
            ),
            encoding="utf-8",
        )
        result = self.build()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("denies", result.stderr)

    def test_a_qa_profile_entry_named_explicitly_is_still_refused(self) -> None:
        """Approving the entry point is not enough: a `qa` document must not ship."""
        allow = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
        allow["entry_points"].append("qa.html")
        allow["deny_paths"] = [p for p in allow["deny_paths"] if p != "qa.html"]
        allow["deny_globs"] = [g for g in allow["deny_globs"] if g != "**/qa*.html"]
        custom = self.tmp / "allowlist.json"
        custom.write_text(json.dumps(allow), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(TOOL), "--source", str(self.source), "--out", str(self.tmp / "out"),
             "--allowlist", str(custom), "--json"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("data-build-profile", result.stderr)

    def test_writing_inside_the_source_tree_is_refused(self) -> None:
        result = subprocess.run(
            [NODE, str(TOOL), "--source", str(REPO), "--out", str(REPO / "release-out"), "--json"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refusing to write", result.stderr)
        self.assertFalse((REPO / "release-out").exists())


if __name__ == "__main__":
    unittest.main()
