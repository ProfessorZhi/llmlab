"""第 20 条同源路由在**机器目录**里的登记：声明要与代码逐字段对得上。

独立 QA 指出的问题很具体：``POST /api/v2/ops/endpoints/{id}/invoke`` 在
``docs/integrations/portal-capabilities.v1.json`` 里**没有记录**。一个没有登记的
浏览器路由，对 QA 与前端来说等于不存在；而一个登记错的，比不存在更糟 —— 它会宣称
一条并不存在（或并不那样工作）的路径。

这一组因此只做一件事，但做到底：**目录里的那一行与 ``route_map`` 里的那一条，
在每一个可核对的字段上都必须相等**（method / path / scope / auth / 状态词），并且
那一行的含义必须只有 ``bff_declared`` —— 它**不是**「能用」，更**不是** LIVE。

行为面的断言不在这里重复：``tests/bff/test_v1_invoke_route.py`` 已经逐条覆盖
匿名 401、伪造字段 400、CSRF 403、跨 UID 404、超界 413、未接线 503、以及响应只含
可信元数据（``body`` / ``echo`` / 凭据都不下发）。本文件只补两条它没有的：
**「声明 ≠ 可用」的行为对照**，以及**会话失效后的 401**。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from backend.bff import route_map as RM
from tests.bff import support as S

REPO = pathlib.Path(__file__).resolve().parents[2]
CATALOG = REPO / "docs/integrations/portal-capabilities.v1.json"

INVOKE_PATH = "/api/v2/ops/endpoints/{endpoint_id}/invoke"
INVOKE_SCOPE = "registry:endpoint:invoke"

#: 目录里任何位置都不该出现的「已上线」词。
FORBIDDEN_LIVE_WORDS = ("live", "ready", "deployed")


class _CatalogueCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(CATALOG.read_text(encoding="utf-8"))
        cls.rows = {(row["method"], row["path"]): row for row in cls.registry["routes"]}


class InvokeRowTests(_CatalogueCase):
    def test_the_invoke_route_is_registered_exactly_once(self):
        hits = [row for row in self.registry["routes"] if row["path"] == INVOKE_PATH]
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["method"], "POST")

    def test_the_row_matches_the_route_spec_field_by_field(self):
        """目录与 ``route_map`` 逐字段相等 —— 不是「大概一致」。"""
        row = self.rows[("POST", INVOKE_PATH)]
        spec = RM.ROUTES_BY_NAME["ops.invoke"]
        self.assertEqual(spec.path, INVOKE_PATH)
        self.assertEqual(spec.methods, (row["method"],))
        self.assertEqual(spec.scope, row["required_scope"])
        self.assertEqual(row["required_scope"], INVOKE_SCOPE)
        self.assertEqual(spec.auth, RM.AUTH_SESSION)
        # 写操作 + CSRF：目录用 ``session-csrf`` 表达这两件事。
        self.assertTrue(spec.is_write)
        self.assertTrue(spec.csrf)
        self.assertEqual(row["required_auth"], "session-csrf")

    def test_the_row_is_bff_declared_and_never_a_live_claim(self):
        row = self.rows[("POST", INVOKE_PATH)]
        self.assertEqual(row["implementation_status"], "bff_declared")
        self.assertEqual(row["host_runtime_verification"], "not_run")
        for word in FORBIDDEN_LIVE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, row["implementation_status"])
                self.assertNotIn(word, row["host_runtime_verification"])

    def test_the_provider_is_one_of_the_declared_providers(self):
        row = self.rows[("POST", INVOKE_PATH)]
        self.assertIn(row["provider"], {item["id"] for item in self.registry["providers"]})

    def test_the_row_carries_no_measurement_or_example_payload(self):
        row = self.rows[("POST", INVOKE_PATH)]
        for key in ("value", "observed_at", "sample_at", "example_payload", "sample_data"):
            with self.subTest(key=key):
                self.assertNotIn(key, row)

    def test_the_row_states_that_router_ack_is_reported_not_enforced(self):
        """把那个**已知缺口**固化在机器可读的登记里，而不是只写在 PR 描述里。

        这是一条**文档钉**，不是契约：它保证将来有人重写 notes 时，不能顺手把
        「Router ACK 只是被转述、并没有被强制」这句话删掉而让目录显得比实现更强。
        """
        notes = self.rows[("POST", INVOKE_PATH)]["notes"]
        lowered = notes.lower()
        # 不写「not LIVE」这四个字，是因为 ``test_capability_catalog_truth.py`` 有一条
        # 按子串拒绝 ``live`` 的守卫（那是 R3 立的，挫但有效）。与其为了文案去松一条
        # 守卫，不如让文案天然满足它 —— 意思用「声明，不是可用性主张」表达得更准。
        self.assertNotIn("live", lowered)
        self.assertIn("not host verified", lowered)
        self.assertIn("declaration only", lowered)
        self.assertTrue(
            ("not enforced" in lowered) or ("owner decision" in lowered),
            "the row must keep saying the router-ACK gate is not enforced here",
        )


class CrossCheckTests(_CatalogueCase):
    """``/api/v2/ops*`` 与 ``/api/portal*`` 两个家族：双向一致。"""

    FAMILIES = ("/api/v2/ops", "/api/portal")

    def _declared(self, prefix: str) -> set[tuple[str, str]]:
        return {
            (method, route.path)
            for route in RM.ROUTES
            for method in route.methods
            if route.path.startswith(prefix)
        }

    def _catalogued(self, prefix: str, *, status: str | None = None) -> set[tuple[str, str]]:
        return {
            (row["method"], row["path"])
            for row in self.registry["routes"]
            if row["path"].startswith(prefix)
            and (status is None or row["implementation_status"] == status)
        }

    def test_every_declared_ops_route_is_in_the_catalogue(self):
        for prefix in self.FAMILIES:
            with self.subTest(family=prefix):
                missing = self._declared(prefix) - self._catalogued(prefix)
                self.assertEqual(missing, set(), f"{prefix}: declared but not catalogued")

    def test_every_bff_declared_catalogue_row_is_declared_in_code(self):
        """目录说「BFF 声明了它」的，代码就必须真的声明了它。

        **不**反过来要求「目录里有的都已被声明」：``/api/portal/metrics/history``
        合法地仍是 ``planned``（没有可用的有界样本源），它出现在目录里正是为了让
        人看见「这条**还**没有挂载」。把两个方向混成一条，会逼着人去删掉那条诚实
        的 ``planned`` 行。
        """
        for prefix in self.FAMILIES:
            with self.subTest(family=prefix):
                orphan = self._catalogued(prefix, status="bff_declared") - self._declared(prefix)
                self.assertEqual(orphan, set(), f"{prefix}: catalogued bff_declared but not declared")

    def test_every_declared_ops_route_is_catalogued_as_bff_declared(self):
        for prefix in self.FAMILIES:
            with self.subTest(family=prefix):
                declared = self._declared(prefix)
                catalogued = self._catalogued(prefix, status="bff_declared")
                self.assertEqual(declared - catalogued, set())

    def test_a_planned_portal_row_stays_unmounted(self):
        """``metrics/history`` 仍是 ``planned``，而且确实没有对应路由 —— 这是诚实的。"""
        key = ("GET", "/api/portal/metrics/history")
        self.assertEqual(self.rows[key]["implementation_status"], "planned")
        self.assertNotIn(key, self._declared("/api/portal"))

    def test_the_status_vocabulary_stays_closed(self):
        vocabulary = self.registry["status_vocabulary"]
        self.assertIn("bff_declared", vocabulary)
        self.assertEqual(
            vocabulary["bff_declared"],
            "declared in BFF route_map, may be unconfigured",
        )
        for word in FORBIDDEN_LIVE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, vocabulary)


class DeclaredIsNotAvailableTests(unittest.TestCase):
    """**「登记了」不等于「能用」** —— 用行为把这句话钉住。"""

    def test_the_route_is_catalogued_bff_declared_and_still_answers_503_when_unwired(self):
        h = S.build_harness(
            state_dir=tempfile.mkdtemp(), prober_healthy=True, mock_gateway=False
        )
        self.addCleanup(h.close)
        ref = S.register_baseline(h, "alice")
        client = h.client("alice")
        response = client.run(
            "POST",
            f"/api/v2/ops/endpoints/{ref['endpoint_id']}/invoke",
            payload={"messages": []},
            headers={"X-CSRF-Token": S.csrf(client)},
        )
        self.assertEqual(response.status, 503, response.json())
        self.assertEqual(response.error_code(), "registry.upstream_unavailable")
        # 同一条路由在目录里仍然是 ``bff_declared`` —— 那个词从来不表示「能用」。
        row = next(
            r
            for r in json.loads(CATALOG.read_text(encoding="utf-8"))["routes"]
            if r["path"] == INVOKE_PATH and r["method"] == "POST"
        )
        self.assertEqual(row["implementation_status"], "bff_declared")
        self.assertEqual(row["host_runtime_verification"], "not_run")

    def test_an_expired_session_is_401_and_nothing_is_forwarded(self):
        """会话失效后**下一次**调用就是 401 —— 不靠客户端缓存，也不靠下个进程。"""
        h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(h.close)
        ref = S.register_baseline(h, "alice")
        # 一次 Gateway 回读确认：否则 Registry 的 P0 门会先以 409 拒绝，
        # 这条用例就不再是在验「会话失效后是否 401」。
        h.acknowledge_route(ref["endpoint_id"])
        client = h.client("alice")
        path = f"/api/v2/ops/endpoints/{ref['endpoint_id']}/invoke"

        ok = client.run(
            "POST", path, payload={"messages": []}, headers={"X-CSRF-Token": S.csrf(client)}
        )
        self.assertEqual(ok.status, 200, ok.json())

        # 让这条会话失效（与「过期」同一条路径：下一次解析一定拿不到主体）。
        h.auth_app.service.logout(h.session_id("alice"))

        after = client.run(
            "POST", path, payload={"messages": []}, headers={"X-CSRF-Token": S.csrf(client)}
        )
        self.assertEqual(after.status, 401, after.json())
        self.assertEqual(after.error_code(), "no_authenticated_session")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
