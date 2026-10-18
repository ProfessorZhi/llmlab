"""只读 ``GET /api/atlas/modules`` 的真实 HTTP 契约测试。

对面是 ``dev/preview_auth.py`` 启动的**真服务**：真 ``wsgiref``、真 SQLite、
真 Session Cookie、真 Host/Cookie 守卫。因此这里验的是「会话真的决定了这个模块
图层能说什么」，而不是「两份契约各自自说自话」。

分五组：

1. **访问控制** —— 匿名 401、登出/吊销**立即** 401；登录了但没有显式 grant 一律
   403（包括 admin）；浏览器自报的 subject / role 一律无效。
2. **失败关闭** —— 本构建没有 Gateway / NVML / 大盘，所以模块状态必须是
   unknown / disabled / unavailable，并且**每一个**都说明原因；GPU 指标**没有
   任何一个是数字**。
3. **入口** —— 没有一个是绝对 URL，没有一个指向上游端口。
4. **可见性** —— 默认 Private 且看不到同事的 Endpoint；切到 Shared 只在已授权
   项目内放宽，且不会隐藏自己的东西。
5. **生产路由面** —— 该路径不在 ``backend.auth.routes.ROUTES`` 里。
"""

from __future__ import annotations

import json
import unittest

from backend.auth import errors, routes
from backend.portal.modules import (
    REASON_NO_VERIFIED_SOURCE,
    SCOPE_ADMIN_READ,
    SCOPE_ENGINES_NATIVE,
    SCOPE_MODULES_READ,
    STATE_AVAILABLE,
)

from dev import portal_modules

from tests.integration_auth import support

MODULES_PATH = "/api/atlas/modules"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.server = support.PreviewServer()
        self.addCleanup(self.server.close)
        self.client = self.server.client()

    def sign_in_as_admin(self) -> str:
        reply = self.client.login(self.server.password)
        self.assertEqual(reply.status, 200, reply.body)
        return self.server.admin_user_id()

    def sign_in_as_member(self, *, open_id: str, display_name: str) -> str:
        user_id = self.server.create_member(open_id=open_id, display_name=display_name)
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)
        return user_id

    def modules(self, client=None, path: str = MODULES_PATH, **kwargs):
        return (client or self.client).get(path, **kwargs)

    def portal(self, reply) -> dict:
        self.assertEqual(reply.status, 200, reply.body)
        payload = reply.json()
        self.assertTrue(payload["ok"])
        return payload["data"]["portal"]

    def by_id(self, dto, module_id):
        return next(m for m in dto["modules"] if m["id"] == module_id)


# ---------------------------------------------------------------------------
# 1. 访问控制
# ---------------------------------------------------------------------------


class AccessControlTests(_Base):
    def test_anonymous_is_401_not_403(self) -> None:
        reply = self.modules()
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.error_code(), errors.NotAuthenticated.code)

    def test_a_signed_in_admin_without_a_grant_is_403(self) -> None:
        self.sign_in_as_admin()
        reply = self.modules()
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), errors.Forbidden.code)

    def test_a_member_without_a_grant_is_403(self) -> None:
        self.sign_in_as_member(open_id="ou_pm1", display_name="成员")
        self.assertEqual(self.modules().status, 403)

    def test_signing_out_takes_effect_immediately(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject)
        self.assertEqual(self.modules().status, 200)
        reply = self.client.write("POST", "/auth/logout", {})
        self.assertIn(reply.status, (200, 204), reply.body)
        self.assertEqual(self.modules().status, 401)

    def test_a_revoked_session_is_401(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject)
        self.server.revoke_session(self.client.cookies[support.SESSION_COOKIE])
        reply = self.modules()
        self.assertEqual(reply.status, 401)
        self.assertIn(
            reply.error_code(),
            {errors.NotAuthenticated.code, errors.SessionExpired.code},
        )

    def test_a_disabled_account_is_403(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject)
        self.server.disable_user(subject)
        reply = self.modules()
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), errors.AccountDisabled.code)

    def test_each_granted_account_sees_only_its_own_subject(self) -> None:
        """目录是一张按 subject 索引的**表**：多给一个账号不会收回另一个。

        真正要钉住的是：未被授权的账号仍然 403，而且无论谁在看，DTO 里的
        subject 永远是**会话自己的**，不会是别人的。
        """
        admin = self.sign_in_as_admin()
        self.server.grant_portal_modules(admin)
        self.assertEqual(self.portal(self.modules())["scope"]["subject"], admin)

        other = self.sign_in_as_member(open_id="ou_pm2", display_name="另一个账号")
        self.assertEqual(self.modules().status, 403, "an ungranted account must still be refused")

        self.server.grant_portal_modules(other)
        self.assertEqual(self.portal(self.modules())["scope"]["subject"], other)

        self.sign_in_as_admin()
        dto = self.portal(self.modules())
        self.assertEqual(dto["scope"]["subject"], admin, "a granted account keeps its own view")

        third = self.sign_in_as_member(open_id="ou_pm3", display_name="第三个账号")
        self.assertEqual(self.modules().status, 403, third)

    def test_forged_identity_input_changes_nothing(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject)
        baseline = self.portal(self.modules())

        for query in ("subject=u-demo-colleague", "role=admin", "x_principal=u-demo-colleague", "subject="):
            with self.subTest(query=query):
                reply = self.modules(path=f"{MODULES_PATH}?{query}")
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(self.portal(reply), baseline)

        spoofed = self.modules(
            headers={"X-Forwarded-User": "u-demo-colleague", "X-Role": "admin", "X-Project": "proj-b"}
        )
        self.assertEqual(self.portal(spoofed), baseline)

    def test_write_verbs_are_405(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                reply = self.client.request(method, MODULES_PATH, body=b"{}")
                self.assertEqual(reply.status, 405)


# ---------------------------------------------------------------------------
# 2. 失败关闭
# ---------------------------------------------------------------------------


class FailClosedTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(self.subject)
        self.dto = self.portal(self.modules())

    def test_the_preview_never_claims_to_be_live(self) -> None:
        self.assertTrue(self.dto["synthetic"])
        self.assertEqual(self.dto["transport"]["mode"], "demo")
        self.assertFalse(self.dto["transport"]["gateway_attached"])
        self.assertEqual(self.dto["deployment"]["engine"], None)

    def test_every_module_either_works_or_says_why_not(self) -> None:
        for module in self.dto["modules"]:
            with self.subTest(module=module["id"]):
                if module["state"] == STATE_AVAILABLE:
                    self.assertIsNone(module["reason"])
                else:
                    self.assertTrue(module["reason"], "a non-available module needs a reason")

    def test_the_preview_reports_the_modules_a_bare_read_grant_cannot_open(self) -> None:
        """只给 ``portal:modules:read`` 时，读目录可以，进原生界面不行。"""
        self.assertEqual(self.by_id(self.dto, "evaluation")["reason"], "missing_required_scope")

    def test_the_playground_is_disabled_and_says_it_is_this_build(self) -> None:
        playground = self.by_id(self.dto, "playground")
        self.assertEqual(playground["state"], "disabled")
        self.assertEqual(playground["reason"], "preview_build_has_no_real_backend")

    def test_no_gpu_metric_is_ever_a_number(self) -> None:
        """这是「不显示假 GPU 数字」在真实 HTTP 上的落点。"""
        gpu = [
            m
            for module in self.dto["modules"]
            for m in module["metrics"]
            if m["name"].startswith("gpu_")
        ]
        self.assertTrue(gpu, "the preview should still report that it cannot measure this")
        for metric in gpu:
            self.assertIsNone(metric["value"], metric["name"])
            self.assertEqual(metric["state"], "unknown")
            self.assertEqual(metric["reason"], REASON_NO_VERIFIED_SOURCE)
            self.assertIsNone(metric["source"])

    def test_no_metric_carries_a_number_at_all_in_this_build(self) -> None:
        for module in self.dto["modules"]:
            for metric in module["metrics"]:
                self.assertIsNone(metric["value"], f"{module['id']}/{metric['name']}")

    def test_no_native_console_entry_exists_without_a_gateway(self) -> None:
        """没有 Gateway 就没有任何原生控制台入口。

        ``self_calls`` 的链接不在此列：它指向外壳里的另一个视图（同源路径），
        与 Gateway 是否接线无关 —— 所以这里按 ``kind`` 而不是按「有没有链接」断言。
        """
        for module in self.dto["modules"]:
            native = [link for link in module["links"] if link["kind"] == "native"]
            self.assertEqual(native, [], module["id"])
        kinds = {link["kind"] for module in self.dto["modules"] for link in module["links"]}
        self.assertTrue(kinds <= {"internal"}, kinds)

    def test_the_administration_module_reports_the_real_session(self) -> None:
        """唯一一个在本构建里就有真实内容的模块 —— 它显示的是服务端的判定本身。"""
        administration = self.by_id(self.dto, "administration")
        self.assertEqual(administration["state"], STATE_AVAILABLE)
        self.assertEqual(administration["data"]["subject"], self.subject)
        self.assertEqual(administration["data"]["scopes"], [SCOPE_MODULES_READ])

    def test_an_extra_scope_does_not_open_a_link_the_deployment_cannot_serve(self) -> None:
        """给了 ``portal:engines:native`` 也不行：本构建没有 Strata、没有 Gateway。

        这条把「两个条件是 AND，不是 OR」钉在真实 HTTP 上。
        """
        self.server.grant_portal_modules(self.subject, scopes=(SCOPE_MODULES_READ, SCOPE_ENGINES_NATIVE))
        dto = self.portal(self.modules())
        engines = self.by_id(dto, "engines")
        self.assertEqual(engines["links"], [])
        self.assertNotEqual(engines["state"], STATE_AVAILABLE)


# ---------------------------------------------------------------------------
# 3. 入口
# ---------------------------------------------------------------------------


class LinkSafetyTests(_Base):
    def test_no_link_anywhere_is_absolute_or_names_an_upstream_port(self) -> None:
        subject = self.sign_in_as_admin()
        # Grant every scope, so that if a link could ever be emitted, it is.
        self.server.grant_portal_modules(
            subject,
            scopes=(
                SCOPE_MODULES_READ,
                SCOPE_ENGINES_NATIVE,
                SCOPE_ADMIN_READ,
                "portal:evaluation:native",
                "portal:observability:native",
                "portal:endpoints:share",
                "atlas:calls:self:read",
            ),
        )
        dto = self.portal(self.modules())
        blob = json.dumps(dto, ensure_ascii=False)
        self.assertNotIn("://", blob)
        self.assertNotIn(":8080", blob)
        for module in dto["modules"]:
            for link in module["links"]:
                href = link["href"]
                if href is None:
                    continue
                self.assertTrue(href.startswith("/"), href)

    def test_the_self_calls_link_points_into_the_shell(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject, scopes=(SCOPE_MODULES_READ, "atlas:calls:self:read"))
        dto = self.portal(self.modules())
        links = self.by_id(dto, "self_calls")["links"]
        self.assertEqual([link["href"] for link in links], ["/atlas#calls"])


# ---------------------------------------------------------------------------
# 4. 可见性
# ---------------------------------------------------------------------------


class EndpointVisibilityTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(self.subject)
        self.dto = self.portal(self.modules())

    def test_the_default_view_is_private(self) -> None:
        block = self.dto["endpoints"]
        self.assertEqual(block["visibility"], "private")
        self.assertTrue(block["items"])
        self.assertTrue(all(item["owner"] == "self" for item in block["items"]))

    def test_a_colleagues_endpoint_is_absent_from_the_private_view(self) -> None:
        blob = json.dumps(self.dto["endpoints"], ensure_ascii=False)
        for absent in ("colleague-chat", "other-project", "u-demo-colleague"):
            self.assertNotIn(absent, blob)

    def test_switching_to_shared_adds_shared_endpoints_in_my_projects_only(self) -> None:
        shared = self.portal(self.modules(path=f"{MODULES_PATH}?visibility=shared"))
        ids = [item["endpoint_id"] for item in shared["endpoints"]["items"]]
        self.assertEqual(shared["endpoints"]["visibility"], "shared")
        # ep-demo-2 is mine and shared; ep-demo-9 is a colleague's and not
        # shared; ep-demo-8 is shared but lives in proj-b, which this subject is
        # not authorised for.
        self.assertIn("ep-demo-1", ids)
        self.assertIn("ep-demo-2", ids)
        self.assertNotIn("ep-demo-9", ids)
        self.assertNotIn("ep-demo-8", ids)

    def test_an_unknown_visibility_is_400(self) -> None:
        # `%20` because a literal space cannot travel in a request line.
        for value in ("everything", "ALL", "%20private%20", "private%2Cshared"):
            with self.subTest(value=value):
                self.assertEqual(self.modules(path=f"{MODULES_PATH}?visibility={value}").status, 400)

    def test_a_repeated_visibility_parameter_is_400(self) -> None:
        reply = self.modules(path=f"{MODULES_PATH}?visibility=private&visibility=shared")
        self.assertEqual(reply.status, 400)


# ---------------------------------------------------------------------------
# 5. 只读与生产路由面
# ---------------------------------------------------------------------------


class ProductionSurfaceTests(_Base):
    def test_the_endpoint_is_absent_from_the_production_router(self) -> None:
        self.assertNotIn(MODULES_PATH, set(routes.ROUTES.values()))

    def test_the_three_preview_endpoints_are_distinct_paths(self) -> None:
        from dev import atlas_calls, atlas_preview

        paths = {MODULES_PATH, atlas_calls.CALLS_PATH, atlas_preview.ATLAS_USAGE_PATH}
        self.assertEqual(len(paths), 3)
        self.assertTrue(paths.isdisjoint(set(routes.ROUTES.values())))

    def test_response_headers_are_the_json_ones(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_portal_modules(subject)
        reply = self.modules()
        self.assertIn("application/json", reply.header("Content-Type") or "")
        self.assertEqual(reply.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(reply.header("Cache-Control"), "no-store")

    def test_the_deep_link_redirects_into_the_shell(self) -> None:
        """调用日志现在是外壳里的一个视图，不再是自己的一个文档。"""
        reply = self.client.get("/calls")
        self.assertEqual(reply.status, 302)
        self.assertEqual(reply.header("Location"), "/atlas#calls")

    def test_the_old_standalone_document_is_gone(self) -> None:
        for path in ("/calls.html", "/css/calls.css", "/js/calls/contract.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, cookies=False).status, 404)

    def test_the_shell_is_served_with_the_call_log_view_in_it(self) -> None:
        subject = self.sign_in_as_admin()
        reply = self.client.get("/atlas")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.body.decode("utf-8")
        self.assertIn('id="view-calls"', body)
        self.assertIn("我的调用日志", body)
        self.assertEqual(body.count('class="view'), 8)


if __name__ == "__main__":
    unittest.main()
