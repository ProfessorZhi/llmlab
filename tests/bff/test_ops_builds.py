"""`registry.builds` —— 引擎库在 ops 载荷里的那一块。

这里只钉 BFF 这一层能钉的两件事：

* **可见性**来自 Registry 的策略（别人的 build 不出现在我的载荷里）；
* **可执行文件路径**与权重路径同一个待遇：owner 看原文，别人只看指纹，并且
  `binary_path_redacted` 必须说清楚它的路径是被拿掉了，而不是「这个 build 没有路径」。

落成规则（怎么去重、什么时候刷新）在 `tests/registry/test_reported_builds.py`；
浏览器端怎么读它，在 `tests/web-features/engines-library.test.js`。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from backend.bff import ops as bff_ops
from backend.registry.models import RUNTIME_BUILD_PRESENT, RuntimeBuild
from backend.registry.weights import path_fingerprint
from tests.bff import support as S

NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)


def _build(build_id: str, owner: str, path: str, **overrides) -> RuntimeBuild:
    payload = dict(
        id=build_id,
        owner_principal=owner,
        owner_uid=-1,
        framework="llama.cpp",
        binary_path=path,
        version="b4120",
        fingerprint="f" * 32,
        found_as="executable",
        state=RUNTIME_BUILD_PRESENT,
        last_seen_at=NOW,
    )
    payload.update(overrides)
    return RuntimeBuild(**payload)


class OpsBuildRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.principal = self.h.app.resolve_session(self.h.session_id("alice"))
        self.mine = _build("bld_mine", self.principal.principal, "/home/alice/bin/llama-server")
        self.theirs = _build(
            "bld_theirs",
            self.h.principal_of("bob"),
            "/home/bob/venv/lib/python3.12/site-packages/llama_cpp_python-0.3.36.dist-info",
            framework="llama.cpp",
            found_as="python_package",
            env="/home/bob/venv",
        )

    def payload(self) -> dict:
        return bff_ops.build_ops(principal=self.principal, service=self.h.service)

    def rows(self) -> dict:
        self.h.service.store.upsert_build(self.mine)
        self.h.service.store.upsert_build(self.theirs)
        return {row["build_id"]: row for row in self.payload()["registry"]["builds"]}

    def test_the_registry_exposes_an_engine_library_slot(self) -> None:
        self.assertIsInstance(self.payload()["registry"]["builds"], list)

    def test_the_owner_gets_the_path_and_the_venv(self) -> None:
        mine = self.rows()["bld_mine"]

        self.assertEqual(mine["binary_path"], "/home/alice/bin/llama-server")
        self.assertFalse(mine["binary_path_redacted"])
        self.assertTrue(mine["owner_is_self"])
        self.assertEqual(mine["version"], "b4120")
        self.assertEqual(mine["found_as"], "executable")
        self.assertEqual(mine["state"], "present")

    def test_someone_elses_build_keeps_the_fingerprint_and_loses_the_path(self) -> None:
        """打码规则本身：与权重路径同一条，与「怎么被看到的」无关。"""

        theirs = bff_ops.build_record(
            self.theirs, subject=self.principal.principal, principals=self.h.service.principals
        )

        self.assertIsNone(theirs["binary_path"])
        self.assertIsNone(theirs["env"], "虚拟环境同样是一条家目录路径")
        self.assertTrue(theirs["binary_path_redacted"])
        self.assertEqual(
            theirs["binary_path_sha256"], path_fingerprint(self.theirs.binary_path)
        )
        self.assertFalse(theirs["owner_is_self"])

    def test_a_build_referenced_by_a_visible_service_becomes_visible(self) -> None:
        """可见性的第二半：服务看得见，它跑的那个 build 也就看得见（打码后）。"""

        ids = S.register_baseline(self.h, "bob")
        S.share(self.h.client("bob"), ids["deployment_id"], mode="all")
        self.h.service.store.upsert_build(self.theirs)
        self.h.service.store.update_engine_runtime(
            ids["engine_id"], runtime_build_id="bld_theirs"
        )

        rows = {row["build_id"]: row for row in self.payload()["registry"]["builds"]}

        self.assertIn("bld_theirs", rows)
        self.assertIsNone(rows["bld_theirs"]["binary_path"])
        self.assertTrue(rows["bld_theirs"]["binary_path_redacted"])

    def test_a_build_nobody_can_see_is_not_in_the_payload(self) -> None:
        self.h.service.store.upsert_build(
            _build("bld_hidden", "account:nobody", "/srv/hidden/llama-server")
        )

        ids = {row["build_id"] for row in self.payload()["registry"]["builds"]}
        self.assertNotIn("bld_hidden", ids)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
