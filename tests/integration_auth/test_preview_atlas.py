"""只读 ``GET /api/atlas/usage`` 的真实 HTTP 契约测试。

对面是 ``dev/preview_auth.py`` 启动的**真服务**：真 ``wsgiref``、真 SQLite、
真 Session Cookie、真 Host/Cookie 守卫。因此这里验的是「认证身份真的喂给了
Atlas 聚合」，而不是「两份契约各自自说自话」。

分五组：

1. **访问控制** —— 匿名 401；登录了但**没有显式服务端 grant** 一律 403
   （包括 admin）；跨项目请求 403；浏览器自报的 role / project / key_id /
   label / X- 头一律无效；登出与吊销**立即**失效。
2. **载荷真实性** —— ``synthetic=true``、``source=synthetic-preview``、
   window / quality / unknown / observed_at 齐备；UNKNOWN 保持 ``null`` 不写 0；
   并与 ``backend.atlas.build_atlas_dto`` 的输出**逐字节一致**。
3. **只读与「不读真实数据」** —— 写动词 405 且 fixture 不变；一次请求只读
   声明的合成 fixture 这一个文件。
4. **响应头与既有路由回归** —— CSP / no-store / nosniff 等；``/login`` 与
   ``/atlas`` 行为不变。
5. **生产路由面** —— ``/api/atlas/usage`` **不在** ``backend.auth.routes.ROUTES``
   里，生产路由器对它只会 404。
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest
from datetime import datetime, timezone
from unittest import mock

from backend.atlas import (
    SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
    SCOPE_USAGE_READ,
    SCOPE_USAGE_UNATTRIBUTED,
    AtlasRequest,
    Provenance,
    RequesterContext,
    build_atlas_dto,
)
from backend.auth import errors, routes, store

import dev.preview_auth as preview

from dev import atlas_preview

from tests.integration_auth import support

ATLAS_PATH = "/api/atlas/usage"

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class _Base(unittest.TestCase):
    """A real preview server, no session yet."""

    def setUp(self) -> None:
        self.server = support.PreviewServer()
        self.addCleanup(self.server.close)
        self.client = self.server.client()

    # --- helpers ------------------------------------------------------------

    def sign_in_as_admin(self) -> str:
        reply = self.client.login(self.server.password)
        self.assertEqual(reply.status, 200, reply.body)
        return self.server.admin_user_id()

    def sign_in_as_member(self, *, open_id: str, display_name: str, projects: tuple[str, ...] = ()) -> str:
        user_id = self.server.create_member(open_id=open_id, display_name=display_name)
        for project in projects:
            self.server.grant_project(user_id, project)
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)
        return user_id

    def atlas(self, client=None, path: str = ATLAS_PATH, **kwargs):
        return (client or self.client).get(path, **kwargs)


# ---------------------------------------------------------------------------
# 1. 访问控制
# ---------------------------------------------------------------------------


class AccessControlTests(_Base):
    """「谁已经登录」和「谁能看用量」是两件事，后者必须显式授予。"""

    def test_anonymous_is_401(self):
        reply = self.atlas()

        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.error_code(), "not_authenticated")

    def test_a_bogus_cookie_is_401_not_403(self):
        self.client.cookies[support.SESSION_COOKIE] = "not-a-real-session-id"

        reply = self.atlas()

        self.assertEqual(reply.status, 401)

    def test_admin_without_an_explicit_grant_is_403(self):
        """role 不产生权限：本地管理员本身**没有** Atlas 访问权。"""
        self.sign_in_as_admin()

        reply = self.atlas()
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), "forbidden")

        # 会话本身是好的 —— 同一个 Cookie 拿 /auth/me 仍是 200。
        me = self.client.get("/auth/me")
        self.assertEqual(me.status, 200)
        self.assertEqual(me.json()["data"]["user"]["role"], store.ROLE_ADMIN)

    def test_member_without_any_project_is_403(self):
        user_id = self.sign_in_as_member(open_id="ou_member_no_project", display_name="无项目成员")
        self.assertFalse(self.server.app.auth.service.projects_for(
            self.server.app.auth.service.db.get_user(user_id)
        ))

        reply = self.atlas()

        self.assertEqual(reply.status, 403)

    def test_member_with_a_project_but_no_grant_is_still_403(self):
        """有项目授权 ≠ 有 Atlas scope。两个门是分开的。"""
        self.sign_in_as_member(
            open_id="ou_member_with_project", display_name="有项目成员", projects=("proj-a",)
        )

        reply = self.atlas()

        self.assertEqual(reply.status, 403)

    def test_a_grant_on_an_unrelated_project_does_not_help(self):
        user_id = self.sign_in_as_member(open_id="ou_member_other", display_name="别的项目成员")
        self.server.grant_atlas(user_id, "proj-somewhere-else")

        reply = self.atlas()

        # grant 指向一个 fixture 里根本不存在的项目：合法身份、零数据，
        # 但**不是** 403（授权确实存在，只是没有数据）——关键是不得泄露 proj-a。
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json()["data"]["atlas"]["groups"], [])
        self.assertNotIn("proj-a", reply.body.decode("utf-8"))

    def test_a_granted_member_sees_only_the_granted_project(self):
        user_id = self.sign_in_as_member(open_id="ou_member_granted", display_name="受权成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas()
        self.assertEqual(reply.status, 200, reply.body)

        dto = reply.json()["data"]["atlas"]
        self.assertEqual([group["id"] for group in dto["groups"]], ["proj-a"])
        self.assertEqual(dto["scope"]["projects"], ["proj-a"])
        self.assertEqual(dto["scope"]["subject"], user_id)

        body = reply.body.decode("utf-8")
        self.assertNotIn("proj-b", body)
        self.assertNotIn("key-gamma", body)

    def test_a_cross_project_request_is_403(self):
        user_id = self.sign_in_as_member(open_id="ou_member_cross", display_name="跨项目成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?project=proj-b")

        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), "forbidden")

    def test_a_matching_project_request_is_allowed(self):
        user_id = self.sign_in_as_member(open_id="ou_member_same", display_name="同项目成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?project=proj-a")

        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json()["data"]["atlas"]["scope"]["projects"], ["proj-a"])

    def test_a_foreign_subject_is_403(self):
        user_id = self.sign_in_as_member(open_id="ou_member_subject", display_name="借名成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?subject=local-somebody-else")

        self.assertEqual(reply.status, 403)

    def test_the_own_subject_may_be_echoed_back(self):
        user_id = self.sign_in_as_member(open_id="ou_member_own", display_name="自报成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?subject={user_id}")

        self.assertEqual(reply.status, 200, reply.body)

    # --- forged fields ------------------------------------------------------

    def test_forged_browser_fields_never_grant_access(self):
        """role / project / key_id / label 与一切 X- 头都不参与授权。"""
        self.sign_in_as_admin()

        reply = self.client.get(
            f"{ATLAS_PATH}?project=proj-a&role=platform_admin&key_id=key-alpha"
            "&label=resume-parse&scopes=atlas:usage:read&workload=resume-parse",
            headers={
                "X-Project": "proj-a",
                "X-Principal": "u-1001",
                "X-Workload": "resume-parse",
                "X-Key-Id": "key-alpha",
                "X-Forwarded-User": "u-1001",
                "X-Atlas-Role": store.ROLE_ADMIN,
                "X-Atlas-Scopes": SCOPE_USAGE_READ,
            },
        )

        self.assertEqual(reply.status, 403)

    def test_a_project_header_cannot_widen_a_grant(self):
        user_id = self.sign_in_as_member(open_id="ou_member_widen", display_name="试图扩权成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.client.get(
            ATLAS_PATH,
            headers={"X-Project": "proj-b", "X-Workload": "eval-suite", "X-Principal": "u-2001"},
        )

        self.assertEqual(reply.status, 200, reply.body)
        dto = reply.json()["data"]["atlas"]
        self.assertEqual([group["id"] for group in dto["groups"]], ["proj-a"])

    def test_a_grant_injected_for_someone_else_does_not_apply(self):
        """grant 表按会话给出的 subject 查，不按角色、不按请求字段。"""
        other = self.server.create_member(open_id="ou_member_other_grant", display_name="别人")
        self.server.grant_atlas(other, "proj-a")
        self.sign_in_as_admin()

        reply = self.atlas()

        self.assertEqual(reply.status, 403)

    def test_the_grants_table_starts_empty(self):
        """默认零授权：CLI 不传 --atlas-demo-grant 时，谁都不能读。"""
        self.assertEqual(len(self.server.app.atlas_grants), 0)

    # --- session lifecycle --------------------------------------------------

    def test_logout_takes_effect_immediately(self):
        self.sign_in_as_admin()
        self.server.grant_atlas(self.server.admin_user_id(), "proj-a")
        self.assertEqual(self.atlas().status, 200)

        self.assertEqual(self.client.write("POST", "/auth/logout", {}).status, 200)

        reply = self.atlas()
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.error_code(), "not_authenticated")

    def test_a_revoked_session_is_401(self):
        user_id = self.sign_in_as_member(open_id="ou_member_revoked", display_name="被吊销成员")
        self.server.grant_atlas(user_id, "proj-a")
        session_id = self.client.cookies[support.SESSION_COOKIE]
        self.assertEqual(self.atlas().status, 200)

        self.server.revoke_session(session_id)

        self.assertEqual(self.atlas().status, 401)

    def test_a_disabled_account_is_403(self):
        user_id = self.sign_in_as_member(open_id="ou_member_disabled", display_name="被停用成员")
        self.server.grant_atlas(user_id, "proj-a")
        self.assertEqual(self.atlas().status, 200)

        self.server.disable_user(user_id)

        reply = self.atlas()
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), "account_disabled")

    # --- request shape ------------------------------------------------------

    def test_write_verbs_are_405(self):
        self.sign_in_as_admin()
        self.server.grant_atlas(self.server.admin_user_id(), "proj-a")

        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                # An explicit zero-length body, so the client never leaves the
                # server waiting on a framing it did not send.
                reply = self.client.request(method, ATLAS_PATH, body=b"")
                self.assertEqual(reply.status, 405)
                self.assertEqual(reply.error_code(), "method_not_allowed")

    def test_the_endpoint_is_read_only_even_with_a_valid_csrf_token(self):
        self.sign_in_as_admin()
        self.server.grant_atlas(self.server.admin_user_id(), "proj-a")

        reply = self.client.write("POST", ATLAS_PATH, {"project": "proj-a"})

        self.assertEqual(reply.status, 405)


    def test_a_repeated_query_parameter_is_refused(self):
        user_id = self.sign_in_as_member(open_id="ou_member_dup", display_name="重复参数成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?project=proj-a&project=proj-b")

        self.assertEqual(reply.status, 400)
        self.assertEqual(reply.error_code(), "invalid_request")

    def test_an_unsupported_bucket_is_400(self):
        user_id = self.sign_in_as_member(open_id="ou_member_bucket", display_name="桶参数成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?bucket=9h")

        self.assertEqual(reply.status, 400)

    def test_a_supported_bucket_is_honoured(self):
        user_id = self.sign_in_as_member(open_id="ou_member_day", display_name="按日成员")
        self.server.grant_atlas(user_id, "proj-a")

        reply = self.atlas(path=f"{ATLAS_PATH}?bucket=1d")

        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.json()["data"]["atlas"]["buckets"]["unit"], "1d")
        self.assertEqual(reply.json()["data"]["atlas"]["window"]["bucket"], "1d")

    def test_unknown_query_parameters_are_ignored(self):
        user_id = self.server.create_member(open_id="ou_member_noise", display_name="噪声成员")
        self.server.grant_atlas(user_id, "proj-a")
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        reply = self.atlas(path=f"{ATLAS_PATH}?nonsense=1&role=platform_admin")

        self.assertEqual(reply.status, 200, reply.body)


# ---------------------------------------------------------------------------
# 2. 载荷真实性
# ---------------------------------------------------------------------------


class PayloadTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.user_id = self.server.create_member(open_id="ou_payload", display_name="载荷成员")
        self.server.grant_atlas(self.user_id, "proj-a", "proj-b")
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(self.user_id)
        reply = self.atlas()
        self.assertEqual(reply.status, 200, reply.body)
        self.reply = reply
        self.document = reply.json()
        self.dto = self.document["data"]["atlas"]

    def test_the_envelope_is_the_product_one(self):
        self.assertIs(self.document["ok"], True)
        self.assertIn("data", self.document)

    def test_it_is_marked_synthetic_and_never_claims_to_be_gateway_production(self):
        self.assertIs(self.dto["synthetic"], True)
        self.assertEqual(self.dto["source"], "synthetic-preview")

        body = self.reply.body.decode("utf-8")
        for forbidden in ("gateway", "production", "prod-", "/srv/", "/home/"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_demo_is_announced_in_the_hints(self):
        hints = " ".join(self.dto["display_hints"])
        self.assertIn("SYNTHETIC", hints)

    def test_it_carries_the_window_quality_and_observed_at(self):
        window = self.dto["window"]
        self.assertEqual(window["bucket"], "1h")
        self.assertEqual(window["timezone"], "UTC")
        self.assertEqual(window["from"], atlas_preview.DEMO_WINDOW_FROM)
        self.assertEqual(window["to"], atlas_preview.DEMO_WINDOW_TO)

        self.assertIn(self.dto["quality"]["calls"], ("measured", "partial", "unknown"))
        self.assertRegex(self.document["data"]["observed_at"], _ISO_Z)

    def test_the_shape_is_the_atlas_dto_and_not_an_atlas_bundle(self):
        """AtlasBundle 的 ``scope`` 是字符串且带 ``series``；AtlasDTO 不是。

        前端 ``web/js/data`` 的 live 模式拒绝 ``synthetic=true``，所以这个端点
        本来就不能直接喂给它 —— 这个断言把「两者形状不同」钉住。
        """
        self.assertEqual(self.dto["schema_version"], "atlas-usage/1")
        self.assertIsInstance(self.dto["scope"], dict)
        self.assertNotIn("series", self.dto)
        for key in ("window", "quality", "totals", "unknown", "groups", "buckets"):
            with self.subTest(key=key):
                self.assertIn(key, self.dto)

    def test_unknown_stays_null_rather_than_zero_without_the_scope(self):
        self.assertIsNone(self.dto["unknown"]["unknown_key"])
        self.assertIsNone(self.dto["unknown"]["malformed_lines"])
        self.assertIsNone(self.dto["unknown"]["content_field_rejected"])
        self.assertEqual(self.dto["quality"]["calls"], "unknown")
        self.assertIs(self.dto["unattributed"]["visible"], False)

    def test_the_unknown_counters_become_numbers_only_when_granted(self):
        self.server.app.atlas_grants.set(
            atlas_preview.AtlasPreviewGrant(
                subject=self.user_id,
                projects=("proj-a", "proj-b"),
                scopes=(
                    SCOPE_USAGE_READ,
                    SCOPE_USAGE_UNATTRIBUTED,
                    SCOPE_USAGE_GLOBAL_DIAGNOSTICS,
                ),
            )
        )

        dto = self.atlas().json()["data"]["atlas"]

        self.assertIsInstance(dto["unknown"]["unknown_key"], int)
        self.assertIsInstance(dto["unknown"]["malformed_lines"], int)
        self.assertIn(dto["quality"]["calls"], ("measured", "partial"))
        self.assertIs(dto["unattributed"]["visible"], True)

    def test_the_window_is_half_open_and_the_fixture_is_what_we_think_it_is(self):
        """窗口边界、吊销 key、窗口外行 —— 三个数都来自合成 fixture 的确切内容。

        ``out_of_window`` / ``revoked_key`` 是**范围受限**计数器：它们只由
        「服务端目录判定属于本次授权项目」的行累加，所以持有 ``atlas:usage:read``
        的主体看到的是确实数过的零，而不是一个猜出来的数。
        """
        self.assertEqual(self.dto["totals"]["calls"], 7)
        self.assertEqual(self.dto["unknown"]["out_of_window"], 1)
        self.assertEqual(self.dto["unknown"]["revoked_key"], 1)
        self.assertEqual([group["id"] for group in self.dto["groups"]], ["proj-a", "proj-b"])

    def test_a_group_with_no_latency_samples_reports_null_not_zero(self):
        """只有失败调用的组：``samples == 0`` ⇒ p50/p95 必须是 null，不能写 0。"""
        user_id = self.server.create_member(open_id="ou_empty_group", display_name="空组成员")
        self.server.grant_atlas(user_id, "proj-empty")
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        dto = self.atlas().json()["data"]["atlas"]

        self.assertEqual(dto["groups"], [])
        self.assertEqual(dto["latency"]["samples"], 0)
        self.assertIsNone(dto["latency"]["p50_ms"])
        self.assertIsNone(dto["latency"]["p95_ms"])
        self.assertIsNone(dto["ttft"]["p50_ms"])
        self.assertEqual(dto["quality"]["latency"], "unknown")

    def test_it_matches_the_contract_builder_byte_for_byte(self):
        """路由不得对 DTO 做任何增删：与服务端合同构建器的输出完全一致。"""
        expected = build_atlas_dto(
            AtlasRequest(
                requester=RequesterContext(
                    subject=self.user_id,
                    roles=frozenset({store.ROLE_MEMBER}),
                    scopes=frozenset({SCOPE_USAGE_READ}),
                    authorized_projects=frozenset({"proj-a", "proj-b"}),
                ),
                requested_subject=None,
                requested_project=None,
                window_from=_utc(atlas_preview.DEMO_WINDOW_FROM),
                window_to=_utc(atlas_preview.DEMO_WINDOW_TO),
                bucket="1h",
            ),
            atlas_preview.read_fixture_text(),
            atlas_preview.DEMO_DIRECTORY,
            Provenance(source=atlas_preview.SYNTHETIC_SOURCE, synthetic=True),
        )

        self.assertEqual(
            json.dumps(self.dto, sort_keys=True), json.dumps(expected, sort_keys=True)
        )


# ---------------------------------------------------------------------------
# 3. 只读，且不读任何真实数据
# ---------------------------------------------------------------------------


class NoRealDataTests(_Base):
    def test_a_request_reads_exactly_one_file_and_it_is_the_synthetic_fixture(self):
        user_id = self.server.create_member(open_id="ou_reader", display_name="读取成员")
        self.server.grant_atlas(user_id, "proj-a", "proj-b")
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        reads: list[str] = []
        real_read_text = pathlib.Path.read_text

        def spy(self, *args, **kwargs):
            reads.append(str(self))
            return real_read_text(self, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "read_text", spy):
            reply = self.atlas()

        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reads, [str(atlas_preview.FIXTURE_PATH)])

    def test_no_usage_jsonl_is_named_anywhere_in_the_read_path(self):
        self.assertNotIn("usage.jsonl", str(atlas_preview.FIXTURE_PATH))
        for path in (atlas_preview.FIXTURE_PATH, pathlib.Path(preview.__file__)):
            self.assertNotIn("usage.jsonl", path.read_text(encoding="utf-8"))

    def test_every_fixture_line_is_explicitly_marked_synthetic(self):
        lines = [line for line in atlas_preview.read_fixture_text().splitlines() if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            with self.subTest(line=line[:48]):
                self.assertIn("SYNTHETIC", line)

    def test_the_module_reaches_no_network_and_no_environment(self):
        """金丝雀：这个模块是纯的 —— 没有 socket、没有 urllib、没有环境变量。"""
        source = pathlib.Path(atlas_preview.__file__).read_text(encoding="utf-8")
        for token in (
            "socket",
            "urllib.request",
            "urlopen",
            "http.client",
            "subprocess",
            "os.environ",
            "getenv",
            "requests.",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_the_fixture_is_unmodified_by_serving_it(self):
        before = atlas_preview.FIXTURE_PATH.read_bytes()

        self.atlas()

        self.assertEqual(atlas_preview.FIXTURE_PATH.read_bytes(), before)


# ---------------------------------------------------------------------------
# 4. 响应头与既有路由回归
# ---------------------------------------------------------------------------


class HeaderAndRegressionTests(_Base):
    def test_the_json_route_ships_the_standard_security_headers(self):
        user_id = self.server.create_member(open_id="ou_headers", display_name="响应头成员")
        self.server.grant_atlas(user_id, "proj-a")
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        reply = self.atlas()

        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.header("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(reply.header("Cache-Control"), "no-store")
        self.assertEqual(reply.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(reply.header("Referrer-Policy"), "no-referrer")
        self.assertEqual(reply.header("Content-Security-Policy"), preview.JSON_CSP)

    def test_an_error_response_carries_the_same_headers(self):
        reply = self.atlas()

        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.header("Cache-Control"), "no-store")
        self.assertEqual(reply.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(reply.header("Referrer-Policy"), "no-referrer")

    def test_the_error_envelope_never_repeats_what_was_supplied(self):
        user_id = self.server.create_member(open_id="ou_echo", display_name="回显成员")
        self.server.grant_atlas(user_id, "proj-a")
        self.client.cookies[support.SESSION_COOKIE] = self.server.session_cookie_for(user_id)

        reply = self.atlas(path=f"{ATLAS_PATH}?bucket=<script>alert(1)</script>")

        self.assertEqual(reply.status, 400)
        self.assertNotIn("script", reply.body.decode("utf-8"))

    def test_the_login_page_csp_is_unchanged(self):
        reply = self.client.get("/login")

        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.header("Content-Security-Policy"), preview.LOGIN_CSP)

    def test_the_demo_page_still_redirects_an_anonymous_visitor(self):
        reply = self.client.get("/atlas")

        self.assertEqual(reply.status, 302)
        self.assertIn("/login", reply.header("Location"))

    def test_the_demo_page_csp_is_unchanged(self):
        self.sign_in_as_admin()

        reply = self.client.get("/atlas")

        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.header("Content-Security-Policy"), preview.DEMO_CSP)


# ---------------------------------------------------------------------------
# 5. 生产路由面
# ---------------------------------------------------------------------------


class ProductionSurfaceTests(unittest.TestCase):
    """The route belongs to the loopback preview and to nothing else."""

    def test_the_production_router_has_no_atlas_route_at_all(self):
        self.assertNotIn(("GET", ATLAS_PATH), routes.ROUTES)
        for _method, path in routes.ROUTES:
            with self.subTest(path=path):
                self.assertNotIn("atlas", path)

    def test_the_production_router_answers_404_for_the_atlas_path(self):
        from backend.auth.config import AuthConfig
        from backend.auth.routes import Application, Request

        cfg = AuthConfig.from_env({}, state_dir=pathlib.Path(atlas_preview.__file__).parent)
        reply = Application(cfg).handle(Request(method="GET", path=ATLAS_PATH))

        self.assertEqual(reply.status, 404)
        self.assertIn(b'"not_found"', reply.body)

    def test_the_path_appears_nowhere_in_the_backend_package(self):
        """``/api/atlas/usage`` is a preview route, not a product route."""
        repo = pathlib.Path(__file__).resolve().parents[2]

        in_backend = sorted(
            path.relative_to(repo).as_posix()
            for path in (repo / "backend").rglob("*.py")
            if ATLAS_PATH in path.read_text(encoding="utf-8")
        )

        self.assertEqual(in_backend, [])
        self.assertEqual(atlas_preview.ATLAS_USAGE_PATH, ATLAS_PATH)


# ---------------------------------------------------------------------------
# 6. 授权从哪来：CLI / 注入面
# ---------------------------------------------------------------------------


class GrantConfigurationTests(_Base):
    """Who may read is decided by server-side configuration and by nothing else.

    These drive the same three steps ``main()`` does — parse, resolve against
    the store, register on the app — so the seam the operator actually uses is
    the seam under test.
    """

    def apply_cli(self, *entries: str) -> preview.PreviewApplication:
        args = preview.build_parser().parse_args(
            [
                "--local-http-dev",
                "--state-dir",
                str(self.server.state_dir),
                *[token for entry in entries for token in ("--atlas-demo-grant", entry)],
            ]
        )
        for raw in args.atlas_demo_grant:
            self.server.app.atlas_grants.set(
                preview.resolve_demo_grant(self.server.app, raw)
            )
        return self.server.app

    def test_an_administrator_username_resolves_to_the_account_the_session_carries(self):
        grant = preview.resolve_demo_grant(self.server.app, "admin")

        self.assertEqual(grant.subject, self.server.admin_user_id())
        self.assertEqual(grant.projects, atlas_preview.DEMO_PROJECTS)
        self.assertEqual(grant.scopes, (SCOPE_USAGE_READ,))

    def test_the_grant_never_reaches_past_the_demo_projects(self):
        """No spelling of the flag can widen the reach beyond the fixture.

        The endpoint has exactly two projects to show, so a grant naming more
        would not expose anything real — but it would be a lie about what the
        operator asked for, and the same grammar would one day be pointed at a
        live directory. It is spelled out instead: the project set is fixed.
        """
        for raw in ("admin", "local:0123456789abcdef", "feishu:0123456789abcdef"):
            with self.subTest(raw=raw):
                grant = preview.resolve_demo_grant(self.server.app, raw)
                self.assertEqual(grant.projects, atlas_preview.DEMO_PROJECTS)

    def test_an_explicit_account_id_is_taken_as_given(self):
        """A full account id is a whole id, colon included — not a subject prefix.

        ``local:`` and ``feishu:`` are part of the id, which is why the flag
        takes no ``:PROJECT`` suffix: splitting on the first colon would read
        this as the subject ``feishu`` and quietly resolve it to nothing.
        """
        for raw in ("feishu:0123456789abcdef", "local:fedcba9876543210"):
            with self.subTest(raw=raw):
                grant = preview.resolve_demo_grant(self.server.app, raw)

                self.assertEqual(grant.subject, raw)
                self.assertEqual(grant.scopes, (SCOPE_USAGE_READ,))

    def test_an_unknown_name_is_refused_rather_than_stored(self):
        for raw in ("", "   ", "somebody-else", ":proj-a"):
            with self.subTest(raw=raw):
                with self.assertRaises(errors.ConfigError):
                    preview.resolve_demo_grant(self.server.app, raw)

    def test_a_duplicate_subject_in_a_static_list_is_a_mistake_not_an_override(self):
        grant = atlas_preview.AtlasPreviewGrant(subject="local:abc", projects=("proj-a",))

        with self.assertRaises(ValueError):
            atlas_preview.AtlasPreviewGrants([grant, grant])

    def test_a_grant_with_no_project_is_refused_at_configuration_time(self):
        with self.assertRaises(ValueError):
            atlas_preview.AtlasPreviewGrant(subject="local:abc", projects=())

    def test_the_cli_flag_actually_opens_the_endpoint(self):
        self.apply_cli("admin")
        self.sign_in_as_admin()

        reply = self.atlas()

        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(
            [group["id"] for group in reply.json()["data"]["atlas"]["groups"]],
            ["proj-a", "proj-b"],
        )

    def test_a_project_suffix_is_refused_rather_than_guessed_at(self):
        """``admin:proj-a`` reads like a narrowing, so it must not be a silent one.

        An account id carries a colon of its own, so there is no way to tell
        ``feishu:<hex>:proj-a`` from a subject named ``feishu``. Rather than
        guess at an authorisation input, the grammar has no project suffix —
        and the near-miss a human would type is refused out loud instead of
        being parsed into something they did not ask for.
        """
        for raw in ("admin:proj-a", "admin:proj-a,proj-b"):
            with self.subTest(raw=raw):
                with self.assertRaises(errors.ConfigError):
                    preview.resolve_demo_grant(self.server.app, raw)

    def test_the_flag_is_repeatable_and_defaults_to_nothing(self):
        parser = preview.build_parser()

        empty = parser.parse_args(["--local-http-dev", "--state-dir", "/tmp"])
        self.assertEqual(empty.atlas_demo_grant, [])

        two = parser.parse_args(
            [
                "--local-http-dev",
                "--state-dir",
                "/tmp",
                "--atlas-demo-grant",
                "admin",
                "--atlas-demo-grant",
                "feishu:abc",
            ]
        )
        self.assertEqual(two.atlas_demo_grant, ["admin", "feishu:abc"])

    def test_grants_may_also_be_injected_at_construction(self):
        """The same seam ``tests/`` and any future embedder uses."""
        server = support.PreviewServer()
        self.addCleanup(server.close)

        server.app.atlas_grants.set(
            atlas_preview.AtlasPreviewGrant(
                subject="local:somebody", projects=("proj-a",)
            )
        )

        self.assertEqual(len(server.app.atlas_grants), 1)
        self.assertIsNone(server.app.atlas_grants.for_subject("local:nobody"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
