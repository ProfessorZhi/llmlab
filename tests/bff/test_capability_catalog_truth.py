"""机器可读目录与 BFF 真实行为的一致性：**声明 ≠ 可用，更 ≠ LIVE**。

这个文件的由来是一次真实的红线：集成候选上
``tests.test_v6_capability_catalog.test_all_planned_endpoints_are_explicitly_not_bff_declared``
红了 —— 因为 ``backend/bff/route_map.py`` 已经声明了
``GET /api/portal/capabilities``，而 ``docs/integrations/portal-capabilities.v1.json``
还把它写成 ``planned``。

修数据是**一半**。另一半是这条红线背后的语义，必须由行为来钉住：

* 一条路由被“声明”了（``bff_declared``），**不等于**它的来源可用；
* 来源没装配时，正确的答复是 **503**，而不是一份 200 的空数据；
* 即使某条路由以 200 收场（能力清单就是如此——**它就是答案**），也绝不能出现
  ``ready``/``live`` 这类词被用来声称主机侧事实。

因此这一组做两件事：把目录与 ``route_map`` 对齐（静态），再把「未接线 ⇒ 不是
LIVE」跑一遍真实 WSGI 请求（行为）。两件都只碰 BFF 自己的行与自己的路由。
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

#: 目录里任何位置都不该出现的“已上线”词。目录自己的
#: ``test_does_not_claim_live_production`` 也钉了这一条；这里是为了让 BFF Lane 在
#: 自己这一侧也读得到同一个事实，而不是只能靠根目录那条测试兜着。
FORBIDDEN_LIVE_WORDS = ("live", "ready", "deployed", "verified_on_host")


class CatalogueStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(CATALOG.read_text(encoding="utf-8"))
        cls.rows = {(row["method"], row["path"]): row for row in cls.registry["routes"]}

    def test_every_bff_declared_catalogue_row_exists_in_the_route_table(self):
        """目录说“BFF 声明了它”，代码就必须真的声明了它。"""
        patterns = {(method, route.path) for route in RM.ROUTES for method in route.methods}
        declared = {
            key for key, row in self.rows.items()
            if row["implementation_status"] == "bff_declared"
        }
        self.assertLessEqual(declared, patterns, declared - patterns)

    def test_the_two_portal_routes_are_no_longer_labelled_planned(self):
        """红线本身：这两条曾是 ``planned``，而代码已经声明了它们。"""
        for path in ("/api/portal/capabilities", "/api/portal/services"):
            with self.subTest(path=path):
                row = self.rows[("GET", path)]
                self.assertEqual(row["implementation_status"], "bff_declared")

    def test_no_catalogue_row_claims_live_or_host_verification(self):
        for key, row in self.rows.items():
            with self.subTest(route=key):
                self.assertEqual(row["host_runtime_verification"], "not_run")
                self.assertNotIn(row["implementation_status"], FORBIDDEN_LIVE_WORDS)
                notes = str(row.get("notes") or "").lower()
                self.assertNotIn("live", notes.replace("never claims live", ""))

    def test_the_catalogue_status_vocabulary_stays_closed(self):
        """状态词是**封闭表**。这里不新增任何词，也不引入 ``ready``/``live``。"""
        vocabulary = self.registry["status_vocabulary"]
        self.assertIn("bff_declared", vocabulary)
        self.assertEqual(
            vocabulary["bff_declared"],
            "declared in BFF route_map, may be unconfigured",
        )
        for word in FORBIDDEN_LIVE_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, vocabulary)

    def test_the_catalogue_matches_what_the_routes_actually_require(self):
        """``required_auth`` / ``required_scope`` 与代码里的门逐条对上。

        目录不是第二套 ACL —— 但它写着“这条路由要什么”，而那个答案的权威在
        ``route_map``。两边不一致时，错的永远是目录。
        """
        for name, path in (("portal.capabilities", "/api/portal/capabilities"),
                           ("portal.services", "/api/portal/services")):
            spec = RM.ROUTES_BY_NAME[name]
            row = self.rows[("GET", path)]
            with self.subTest(route=name):
                self.assertEqual(spec.auth, RM.AUTH_SESSION)
                self.assertEqual(row["required_scope"], spec.scope)
                self.assertFalse(spec.is_write)
                self.assertTrue(row["required_auth"].startswith("session"))


class UnattachedSourceIsNotLiveTests(unittest.TestCase):
    """行为面：来源没装配时，每条 BFF 声明的数据路由都不是一个“看起来能用”的 200。"""

    @classmethod
    def setUpClass(cls):
        # 每一个可选来源都摘掉：这正是“声明了但没接线”的那个部署。
        cls.h = S.build_harness(
            state_dir=tempfile.mkdtemp(),
            with_observability=False,
            with_evaluation=False,
            with_atlas=False,
            with_host_summary=False,
        )

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_services_answers_503_not_a_fabricated_service_list(self):
        response = self.h.admin_client("carol").run("GET", "/api/portal/services")
        self.assertEqual(response.status, 503, response.json())
        self.assertNotIn("services", response.json().get("data") or {})

    def test_infra_summary_answers_503_not_a_page_of_unknowns(self):
        response = self.h.admin_client("carol").run("GET", "/api/infra/summary")
        self.assertEqual(response.status, 503, response.json())
        self.assertIsNone(response.json().get("data"))

    def test_capabilities_never_reports_ready_when_nothing_is_attached(self):
        """这条路由**是** 200 —— 因为“没有来源可用”本身就是它要给的答案。

        但它的每一行都必须是 ``not_configured``（或 Registry 那条 ``ready`` 之外的
        合法取值），绝不能出现一个 ``ready``；而 ``ready`` 才是唯一可能被前端读成
        “可以取数”的取值。
        """
        response = self.h.client("alice").run("GET", "/api/portal/capabilities")
        self.assertEqual(response.status, 200, response.json())
        data = response.json()["data"]
        states = {item["source"]: item["state"] for item in data["sources"]}
        for source in ("observability", "host_summary", "atlas_calls", "evaluation"):
            with self.subTest(source=source):
                self.assertEqual(states[source], "not_configured")
                self.assertNotEqual(states[source], "ready")
        # Registry 是应用的必需依赖，它没有“装了但关着”的中间态；它不在本条断言的
        # 范围内，但也不能借这条路由被说成主机侧事实。
        self.assertNotIn("live", response.body.decode().lower().replace("never", ""))

    def test_no_bff_route_serves_a_200_empty_list_in_place_of_an_unattached_source(self):
        """一次真实但为空的查询（200 + 空）与“没接线”（503）不能混用。

        这条把两条语义相反的路由放在一起对照，免得将来有人把其中一条“统一”成另一条。
        """
        calls = self.h.client("alice").run("GET", "/api/atlas/calls")
        reports = self.h.client("alice").run("GET", "/api/evaluation/reports")
        with self.subTest(route="atlas.calls"):
            self.assertEqual(calls.status, 503)
            self.assertEqual(calls.error_code(), "atlas_adapter_not_attached")
        with self.subTest(route="evaluation.reports"):
            self.assertEqual(reports.status, 503)
            self.assertEqual(reports.error_code(), "evaluation_backend_not_ready")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
