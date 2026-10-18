"""路由表本身是可断言的事实，而不是一串 if 的副产品。

三条性质分开验，因为它们的失败后果不同：

* **表的内部一致性**（纯数据）——一条写路由忘了 ``csrf=True``，或者一条路径里
  出现了 ``:8080``，不需要起任何服务就能被发现；
* **表与实现的绑定**——``handler`` 必须真的在 ``OpsApplication`` 上存在，否则
  表可以宣称一条路由器根本没有；
* **表与行为的绑定**——逐条真的发一次请求，断言「匿名得到 401」「方法不对得到
  405」。声明与行为不一致时，**行为**是我们要的那个答案，所以这条必须实测。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import route_map as RM
from backend.bff.app import OpsApplication
from tests.bff import support as S


class RouteTableShapeTests(unittest.TestCase):
    """纯数据：不需要 harness，只需要表自己。"""

    def test_every_route_declares_closed_values(self):
        for spec in RM.ROUTES:
            with self.subTest(route=spec.name):
                self.assertTrue(spec.methods, "a route with no method is unreachable")
                self.assertIn(spec.auth, RM.AUTH_LEVELS)
                self.assertIn(spec.kind, RM.KINDS)
                self.assertIn(spec.source, RM.SOURCES)
                self.assertTrue(spec.owner, "every route belongs to a lane")
                self.assertTrue(spec.path.startswith("/"), spec.path)

    def test_writes_require_csrf_and_reads_do_not_claim_it(self):
        for spec in RM.ROUTES:
            with self.subTest(route=spec.name):
                self.assertEqual(
                    spec.csrf,
                    spec.is_write,
                    "a write must go through the CSRF/same-origin preflight",
                )

    def test_no_route_names_the_upstream_gateway_port(self):
        """8080 是上游 Gateway 的端口；它不该出现在任何同源路由上。"""
        for spec in RM.ROUTES:
            with self.subTest(route=spec.name):
                self.assertNotIn(":8080", spec.path)

    def test_the_anonymous_surface_is_exactly_the_intended_three(self):
        """匿名可达的路由是**一个封闭集合**，不是「反正现在只有这些」。

        新增一条匿名路由必须同时改这条断言——那正是我们希望被逼着做的一次思考。
        """
        anonymous = {spec.name for spec in RM.ROUTES if spec.is_anonymous}
        self.assertEqual(
            anonymous,
            {"auth.linux.login", "auth.linux.status", "auth.admin.status"},
        )

    def test_the_only_anonymous_write_is_the_login_entry(self):
        anonymous_writes = {
            spec.name for spec in RM.ROUTES if spec.is_anonymous and spec.is_write
        }
        self.assertEqual(anonymous_writes, {"auth.linux.login"})

    def test_route_names_are_unique(self):
        names = [spec.name for spec in RM.ROUTES]
        self.assertEqual(len(names), len(set(names)))

    def test_no_two_plain_routes_share_a_path(self):
        """两条 ``path`` 相同的无变量路由，先匹配到谁就是谁——表里不该有这种歧义。"""
        plain = [spec.path for spec in RM.ROUTES if spec.pattern is None]
        self.assertEqual(len(plain), len(set(plain)))

    def test_handlers_are_unique_and_exist_on_the_application(self):
        bound = [spec for spec in RM.ROUTES if spec.handler is not None]
        names = [spec.handler for spec in bound]
        self.assertEqual(len(names), len(set(names)))
        for spec in bound:
            with self.subTest(route=spec.name):
                self.assertTrue(
                    hasattr(OpsApplication, spec.handler),
                    f"{spec.handler!r} is declared but not implemented",
                )

    def test_handlers_index_covers_every_bound_route(self):
        index = RM.handlers()
        for spec in RM.ROUTES:
            if spec.handler is None:
                continue
            with self.subTest(route=spec.name):
                self.assertEqual(index[spec.handler], spec.name)

    def test_match_resolves_every_declared_route(self):
        for spec in RM.ROUTES:
            with self.subTest(route=spec.name):
                if spec.pattern is None:
                    self.assertIs(RM.match(spec.path), spec)
                else:
                    # 给变量段填一个普通取值，确认模式真的匹配。
                    prefix, suffix = spec.pattern
                    self.assertIs(RM.match(f"{prefix}dep_example{suffix}"), spec)

    def test_an_unknown_path_matches_nothing(self):
        for path in ("/", "/api", "/api/v2", "/api/v2/ops/nope", "/nope"):
            with self.subTest(path=path):
                self.assertIsNone(RM.match(path))

    def test_a_pattern_route_does_not_match_a_nested_path(self):
        spec = RM.ROUTES_BY_NAME["ops.share"]
        prefix, suffix = spec.pattern
        self.assertIsNone(RM.match(f"{prefix}a/b{suffix}"))


class RouteBehaviourTests(unittest.TestCase):
    """实测：表说什么，服务就做什么。"""

    @classmethod
    def setUpClass(cls):
        cls.h = S.build_harness(state_dir=tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def _anonymous(self) -> S.WsgiClient:
        return S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def _path(self, spec: RM.RouteSpec) -> str:
        if spec.pattern is None:
            return spec.path
        prefix, suffix = spec.pattern
        return f"{prefix}dep_example{suffix}"

    def _anonymous_call(self, spec: RM.RouteSpec):
        client = self._anonymous()
        headers = {}
        if spec.is_write:
            # 写操作先过 CSRF/同源：没有 token 时得到的是 403，那是**另一道**门。
            # 这里要验的是身份那道，所以把 CSRF 备齐。
            headers["X-CSRF-Token"] = S.csrf(client)
        return client.run(
            spec.methods[0],
            self._path(spec),
            payload={} if spec.is_write else None,
            headers=headers,
        )

    def test_every_session_route_refuses_an_anonymous_caller(self):
        """逐条路由发一个匿名请求，必须得到 401（不是 200，也不是 404）。

        状态码是这一条要验的全部：前端的取数状态机按**状态码**分流（401 → 未登录、
        403 → 无权限、其余 → UNKNOWN），所以「每一条受保护路由都答 401」是可以被
        实测的承诺，而错误 token 的用词见下一条。
        """
        for spec in RM.ROUTES:
            if spec.auth != RM.AUTH_SESSION:
                continue
            with self.subTest(route=spec.name):
                response = self._anonymous_call(spec)
                self.assertEqual(
                    response.status,
                    401,
                    f"{spec.name}: expected 401 for an anonymous caller, "
                    f"got {response.status} {response.json()!r}",
                )

    def test_authenticated_401_vocabulary_is_pinned_on_both_sides(self):
        """同一个源上，「你没有登录」有两个错误 token，而这是**已知**的分歧。

        BFF 自己的路由用 ``no_authenticated_session``（``backend/bff/identity.py``
        的重试理由是「身份只从会话来」，那条路径解析不出主体时说的就是这句）；
        ``/auth/*`` 由既有的 ``backend.auth`` 实现，它说的是 ``not_authenticated``。

        两者都是 401，前端都读成「未登录」，所以这不是一个功能性缺陷——但它是一个
        真实的**用词**分歧。统一它要改 ``backend/auth/**``，而那条路径归 IAM Lane；
        在那边决定之前，集成 Lane 能做的是把它**钉住**：这条测试同时断言两个 token，
        于是任何一边悄悄改词都会在这里失败，而不会等到某天有人按 token 做判断。

        这正是 Issue #49 里「UID/Principal errors consistent」这一条当前的真实边界：
        本 Lane 拥有的那一半是一致的（BFF 自有的每条路由都是同一个 token），交叉的
        那一半是记录在案、待协调的。
        """
        bff_code = "no_authenticated_session"
        delegated_code = "not_authenticated"

        for spec in RM.ROUTES:
            if spec.auth != RM.AUTH_SESSION:
                continue
            with self.subTest(route=spec.name):
                response = self._anonymous_call(spec)
                expected = delegated_code if spec.handler is None else bff_code
                self.assertEqual(response.status, 401, spec.name)
                self.assertEqual(
                    response.error_code(),
                    expected,
                    f"{spec.name} changed its unauthenticated token",
                )

    def test_the_two_vocabularies_are_the_only_two(self):
        """整个会话面上，401 的 token 只有上面那两个——没有第三种悄悄出现。"""
        seen = set()
        for spec in RM.ROUTES:
            if spec.auth != RM.AUTH_SESSION:
                continue
            seen.add(self._anonymous_call(spec).error_code())
        self.assertEqual(seen, {"no_authenticated_session", "not_authenticated"})
        # 匿名只读状态路由不该反过来泄露任何身份信息。
        self.assertEqual(
            self._anonymous().run("GET", "/auth/admin/status").status, 200
        )

    def test_every_route_refuses_a_method_it_does_not_declare(self):
        for spec in RM.ROUTES:
            if spec.handler is None:
                # 由既有 auth 应用实现的路径，方法判定归它。
                continue
            with self.subTest(route=spec.name):
                client = self.h.client("alice")
                wrong = "GET" if "GET" not in spec.methods else "DELETE"
                response = client.run(wrong, self._path(spec), payload={})
                self.assertEqual(response.status, 405, spec.name)

    def test_a_path_outside_the_table_is_never_served(self):
        client = self.h.client("alice")
        for path in ("/", "/api", "/api/v2", "/api/v2/ops/nope", "/api/internal/ops"):
            with self.subTest(path=path):
                response = client.run("GET", path)
                self.assertEqual(response.status, 404, path)
                self.assertEqual(response.error_code(), "not_found")

    def test_the_frontend_contracts_the_bff_does_not_serve_yet(self):
        """前端在调、而同源 BFF **仍然没有**承接的那几条路径。

        Issue #49 要求的同源入口是 ``/auth/*``、``/api/v2/ops``、
        ``/api/v2/registry``、``/api/atlas/calls``、``/api/atlas/modules``、
        ``/api/infra/summary``。

        **2026-10-17（Issue #79）之后这张清单变短了，而且是按这张用例自己写下的
        规矩变短的**：``/api/atlas/calls``（Calls/Atlas Lane #77）与
        ``/api/infra/summary``（Host Lane #74）已经接进 ``ROUTES``，于是它们从
        下面这一组里被**删掉**，而不是让断言向实现让步。它们接上之后的行为由
        ``test_portal_routes.py`` 正反两面覆盖（匿名 401、未装配 503、
        任意上游参数 400）。

        仍然没接的是 Registry 的 ``/api/v2/registry/*`` 与 Atlas 的
        ``/api/atlas/modules``：那两条的数据面还在别的 Lane 手里，``backend/bff``
        只拥有接线，而接线还没发生。这条用例把「还没发生」钉成事实——它们今天必须
        **404**，而不是被某个兜底分支或某个 dev 预览实现偷偷服务掉。
        """
        client = self.h.client("alice")
        not_yet_wired = (
            "/api/v2/registry/endpoints",
            "/api/v2/registry/endpoints/anything",
            "/api/atlas/modules",
        )
        for path in not_yet_wired:
            with self.subTest(path=path):
                response = client.run("GET", path)
                self.assertEqual(
                    response.status,
                    404,
                    f"{path} is served but is not in the route table",
                )
                self.assertEqual(response.error_code(), "not_found")

    def test_every_session_route_rejects_a_forged_identity_header(self):
        """自报身份在**每一条**受保护路由上都是 400，不是「路由 A 检查了、B 忘了」。"""
        for spec in RM.ROUTES:
            if spec.auth != RM.AUTH_SESSION or spec.handler is None:
                continue
            for header in ("X-UID", "X-Role", "X-Admin", "X-Owner", "X-Linux-User"):
                with self.subTest(route=spec.name, header=header):
                    client = self.h.client("alice")
                    headers = {header: "4000"}
                    if spec.is_write:
                        headers["X-CSRF-Token"] = S.csrf(client)
                    response = client.run(
                        spec.methods[0],
                        self._path(spec),
                        payload={} if spec.is_write else None,
                        headers=headers,
                    )
                    self.assertEqual(
                        response.status,
                        400,
                        f"{spec.name} accepted {header}: {response.json()!r}",
                    )
                    self.assertEqual(
                        response.error_code(), "identity_assertion_rejected"
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
