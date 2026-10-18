"""Browser smoke test for the packaged V1 static bundle (Lane #69).

The static tests prove what is *in* the bundle. This proves what it *does*: it
serves the staged directory on loopback and drives it in real Chrome over CDP.

Three properties are asserted, because each of them would turn a packaging
mistake into a production incident:

1. the artifact declares ``data-build-profile="production"``, so the design
   samples are not merely hidden but unreachable;
2. no URL — ``?demo=1``, ``?source=mock``, a tracking parameter — puts a
   synthetic number or a demo portal object on screen, even though the fixture
   modules are shipped (the application imports them; deleting them blindly
   would break production boot);
3. the login page stays fail-closed with no PAM backend and never issues a
   request carrying a password.

Chrome is optional in this environment, so the test **skips** rather than
pretending to pass when it is absent — the same rule the repository's browser
suites follow. A skip is not evidence; it is recorded as a skip.

Nothing here touches the network outside loopback or writes into the repository.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
PACKAGER = REPO / "tools" / "release" / "build_static_bundle.mjs"
SMOKE = REPO / "tools" / "release" / "smoke_static_bundle.mjs"
NODE = shutil.which("node")
CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable") \
    or shutil.which("chromium") or shutil.which("chromium-browser")


class StaticBundleSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="v1-bundle-smoke-")
        cls.tmp = pathlib.Path(cls._tmp)
        cls.out = cls.tmp / "bundle"
        build = subprocess.run(
            [NODE, str(PACKAGER), "--out", str(cls.out), "--json"],
            capture_output=True, text=True, timeout=180, cwd=str(REPO),
        )
        cls.build_result = build
        cls.smoke_result = None
        if build.returncode == 0:
            cls.smoke_result = subprocess.run(
                [NODE, str(SMOKE), str(cls.out), "--json"],
                capture_output=True, text=True, timeout=420, cwd=str(REPO),
                env={"PATH": os.environ.get("PATH", ""), "CHROME_PATH": CHROME or "",
                     "HOME": os.environ.get("HOME", "")},
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self) -> None:
        if NODE is None:
            self.skipTest("node is not installed")
        if CHROME is None:
            self.skipTest("no Chrome/Chromium on this host; the smoke cannot run")
        self.assertEqual(self.build_result.returncode, 0, self.build_result.stderr)
        self.assertIsNotNone(self.smoke_result)

    def test_the_packaged_bundle_passes_the_browser_smoke(self) -> None:
        result = self.smoke_result
        self.assertEqual(result.returncode, 0,
                         f"smoke failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        report = json.loads(result.stdout)
        failed = [r for r in report["results"] if not r["ok"]]
        self.assertEqual(failed, [], f"failed checks: {failed}")
        self.assertGreater(len(report["results"]), 10, "the smoke must actually check things")

    def test_the_smoke_covers_both_regressions_it_exists_for(self) -> None:
        report = json.loads(self.smoke_result.stdout)
        names = [r["name"] for r in report["results"]]
        joined = "\n".join(names)
        self.assertIn("?demo=1", joined, "the demo opt-in must be probed")
        self.assertIn("?source=mock", joined, "the legacy mock shortcut must be probed")
        self.assertIn("login page", joined, "the login gate must be probed")
        self.assertIn("off-origin request", joined, "off-origin traffic must be probed")


if __name__ == "__main__":
    unittest.main()
