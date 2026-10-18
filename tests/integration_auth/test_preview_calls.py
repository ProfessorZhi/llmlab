"""只读 ``GET /api/atlas/calls`` 的真实 HTTP 契约测试。

对面是 ``dev/preview_auth.py`` 启动的**真服务**：真 ``wsgiref``、真 SQLite、
真 Session Cookie、真 Host/Cookie 守卫。因此这里验的是「会话身份真的决定了
你看到哪几条调用」，而不是「两份契约各自自说自话」。

分五组：

1. **访问控制** —— 匿名 401、登出/吊销**立即** 401；登录了但没有显式
   self-grant 一律 403（包括 admin）；**跨用户 key 403**（同事的 key、不存在的
   key、自己已吊销的 key 是**同一个** 403）；浏览器自报的 subject / principal /
   role 一律无效。
2. **只列自己的调用** —— 明细里只有本人 key 的行；同项目同事 key 的行不出现；
   正文金丝雀（prompt）不出现。
3. **载荷真实性** —— ``synthetic=true``、``source=synthetic-preview`` 且
   ``transport.mode=demo``：**绝不**自称 live；缺失值保持 ``null`` 不写 0。
4. **正文与 opt-in** —— ``content.included`` 恒为 ``false``；opt-in 状态只改变
   ``content.consent`` 字段，不改变任何一行。
5. **只读、参数与生产路由面** —— 写动词 405；重复参数 400；未知参数被忽略；
   该路径**不在** ``backend.auth.routes.ROUTES`` 里，生产路由器只会 404。
"""

from __future__ import annotations

import json
import unittest

from backend.auth import errors, routes

from dev import atlas_calls

from tests.integration_auth import support

CALLS_PATH = "/api/atlas/calls"

#: 演示目录里属于演示主体自己的 key（不含那把已吊销的）。
OWN_VISIBLE_KEYS = {"key-alpha", "key-orphan"}

#: 演示目录里**别人**的 key。
FOREIGN_KEYS = {"key-beta", "key-gamma"}


class _Base(unittest.TestCase):
    """A real preview server, no session yet."""

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

    def calls(self, client=None, path: str = CALLS_PATH, **kwargs):
        return (client or self.client).get(path, **kwargs)

    def log(self, reply) -> dict:
        self.assertEqual(reply.status, 200, reply.body)
        payload = reply.json()
        self.assertTrue(payload["ok"])
        return payload["data"]["call_log"]


# ---------------------------------------------------------------------------
# 1. 访问控制
# ---------------------------------------------------------------------------


class AccessControlTests(_Base):
    def test_anonymous_is_401_not_403(self) -> None:
        """没登录是 401（产品自己的口径），不是 403，也**不是**一个空列表。"""
        reply = self.calls()
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.error_code(), errors.NotAuthenticated.code)

    def test_a_bogus_cookie_is_401_not_403(self) -> None:
        self.client.cookies[support.SESSION_COOKIE] = "not-a-real-session"
        reply = self.calls()
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.error_code(), errors.NotAuthenticated.code)

    def test_signing_out_takes_effect_immediately(self) -> None:
        """登出后**立刻** 401 —— 不是「403 或者一份缓存下来的列表」。"""
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        self.assertEqual(self.calls().status, 200)

        reply = self.client.write("POST", "/auth/logout", {})
        self.assertIn(reply.status, (200, 204), reply.body)

        self.assertEqual(self.calls().status, 401)

    def test_a_revoked_session_is_401(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        self.assertEqual(self.calls().status, 200)

        self.server.revoke_session(self.client.cookies[support.SESSION_COOKIE])
        reply = self.calls()
        self.assertEqual(reply.status, 401)
        # 产品自己的细分口径：被吊销的会话报 ``session_expired``（``NotAuthenticated``
        # 的子类）。断言的是**类别**，不是某一个具体 code。
        self.assertIn(
            reply.error_code(),
            {errors.NotAuthenticated.code, errors.SessionExpired.code},
        )

    def test_a_signed_in_admin_without_a_grant_is_403(self) -> None:
        """有会话但**没有显式 self-grant** —— 403，管理员也不例外。"""
        self.sign_in_as_admin()
        reply = self.calls()
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), errors.Forbidden.code)

    def test_a_member_without_a_grant_is_403(self) -> None:
        self.sign_in_as_member(open_id="ou_nogrant", display_name="别人")
        self.assertEqual(self.calls().status, 403)

    def test_a_disabled_account_is_403(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        self.server.disable_user(subject)
        reply = self.calls()
        self.assertEqual(reply.status, 403)
        self.assertEqual(reply.error_code(), errors.AccountDisabled.code)

    def test_the_grant_is_for_one_subject_and_everyone_else_is_403(self) -> None:
        """授权绑定到**一个**主体：另一个有会话的账号（即使也被授过权）拿不到。

        这条同时是「目录不能在请求里按调用者重建」的回归：如果目录是按当下这个
        调用者生成的，下面第二个账号就会「拥有」同一批演示 key，从而看到别人的
        调用 —— 跨用户边界恰好反过来。
        """
        admin = self.sign_in_as_admin()
        self.server.grant_self_calls(admin)
        self.assertEqual(self.calls().status, 200)

        other = self.sign_in_as_member(open_id="ou_other", display_name="另一个账号")
        self.assertEqual(self.calls().status, 403)

        # 现在把唯一的那份授权换成这个账号：它拿到的必须是**它自己名下**的 key，
        # 而管理员那条会话随即变成 403。
        self.server.grant_self_calls(other)
        dto = self.log(self.calls())
        self.assertEqual(dto["scope"]["subject"], other)
        self.assertTrue(dto["calls"])

        self.sign_in_as_admin()
        self.assertEqual(self.calls().status, 403)

    def test_a_colleagues_key_is_403(self) -> None:
        """跨用户 key：演示主体请求 ``key-beta``（属于 u-demo-colleague）必须被拒。"""
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        for key in sorted(FOREIGN_KEYS):
            with self.subTest(key=key):
                reply = self.calls(path=f"{CALLS_PATH}?key={key}")
                self.assertEqual(reply.status, 403, reply.body)
                self.assertEqual(reply.error_code(), errors.Forbidden.code)

    def test_every_foreign_or_unknown_key_is_the_same_refusal(self) -> None:
        """同事的 key / 不存在的 key / 自己已吊销的 key —— 同一个响应。

        否则靠状态码或错误码的差异就能枚举出「哪把 key 真实存在」。
        """
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        seen = set()
        for key in ("key-beta", "key-gamma", "no-such-key", "key-retired", "../key-alpha"):
            reply = self.calls(path=f"{CALLS_PATH}?key={key}")
            seen.add((reply.status, reply.error_code(), reply.body))
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(seen.pop()[0], 403)

    def test_a_forged_subject_parameter_changes_nothing(self) -> None:
        """``subject`` / ``principal`` / ``role`` 不是本端点的参数，说什么都无效。"""
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        baseline = self.log(self.calls())

        for query in (
            "subject=u-demo-colleague",
            "principal=u-demo-colleague",
            "role=admin",
            "x_principal=u-demo-colleague",
            "subject=",
        ):
            with self.subTest(query=query):
                reply = self.calls(path=f"{CALLS_PATH}?{query}")
                # 精确地不看这些名字：它们既不改身份，也不触发 400。
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(self.log(reply), baseline)

    def test_a_forged_header_changes_nothing(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        baseline = self.log(self.calls())
        spoofed = self.calls(
            headers={
                "X-Forwarded-User": "u-demo-colleague",
                "X-Principal": "u-demo-colleague",
                "X-Project": "proj-b",
            }
        )
        self.assertEqual(self.log(spoofed), baseline)


# ---------------------------------------------------------------------------
# 2. 只列自己的调用
# ---------------------------------------------------------------------------


class OwnCallsOnlyTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.subject = self.sign_in_as_admin()
        self.server.grant_self_calls(self.subject)
        self.dto = self.log(self.calls())

    def test_only_own_keys_are_listed(self) -> None:
        listed = {row["key_id"] for row in self.dto["calls"]}
        self.assertTrue(listed)
        self.assertTrue(listed <= OWN_VISIBLE_KEYS, listed)

    def test_a_colleagues_rows_are_absent_from_the_body(self) -> None:
        """正文里一个他人 key 的字样都不该有。

        ``key-retired`` 是例外、也不该被抹掉：它是**本人**已吊销的 key，出现在
        ``scope.revoked_keys`` 与提示语里是如实告知。要断言的是它不出现在**明细行**里。
        """
        blob = json.dumps(self.dto["calls"], ensure_ascii=False)
        for absent in FOREIGN_KEYS | {"key-retired", "unknown-key-demo", "key-outside"}:
            self.assertNotIn(absent, blob)

    def test_the_whole_body_never_names_a_colleague_or_an_unregistered_key(self) -> None:
        blob = json.dumps(self.dto, ensure_ascii=False)
        for absent in FOREIGN_KEYS | {"unknown-key-demo", "key-outside", "u-demo-colleague"}:
            self.assertNotIn(absent, blob)

    def test_the_own_key_set_is_echoed_for_audit(self) -> None:
        self.assertEqual(sorted(self.dto["scope"]["keys"]), sorted(OWN_VISIBLE_KEYS))
        self.assertEqual(self.dto["scope"]["revoked_keys"], ["key-retired"])

    def test_the_revoked_key_rows_are_counted_but_not_listed(self) -> None:
        self.assertEqual(self.dto["counts"]["revoked_key_rows"], 1)

    def test_narrowing_to_one_own_key_works(self) -> None:
        dto = self.log(self.calls(path=f"{CALLS_PATH}?key=key-orphan"))
        self.assertEqual({row["key_id"] for row in dto["calls"]}, {"key-orphan"})

    def test_the_prompt_canary_never_reaches_the_wire(self) -> None:
        reply = self.calls()
        self.assertNotIn(b"SYNTHETIC-CANARY", reply.body)

    def test_what_the_caller_sent_is_not_echoed_back(self) -> None:
        reply = self.calls(path=f"{CALLS_PATH}?key=no-such-key")
        self.assertEqual(reply.status, 403)
        self.assertNotIn(b"no-such-key", reply.body)


# ---------------------------------------------------------------------------
# 3. 载荷真实性
# ---------------------------------------------------------------------------


class PayloadHonestyTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.subject = self.sign_in_as_admin()
        self.server.grant_self_calls(self.subject)
        self.dto = self.log(self.calls())

    def test_the_envelope_is_the_product_one(self) -> None:
        payload = self.calls().json()
        self.assertEqual(set(payload), {"ok", "data"})
        self.assertEqual(set(payload["data"]), {"observed_at", "call_log"})

    def test_it_is_marked_synthetic_and_never_claims_to_be_live(self) -> None:
        self.assertTrue(self.dto["synthetic"])
        self.assertEqual(self.dto["source"], atlas_calls.SYNTHETIC_SOURCE)
        self.assertEqual(self.dto["transport"]["mode"], "demo")
        self.assertFalse(self.dto["transport"]["gateway_attached"])

    def test_no_path_can_make_the_preview_claim_live(self) -> None:
        for query in ("", "?live=1", "?gateway=1", "?transport=live"):
            with self.subTest(query=query):
                reply = self.calls(path=CALLS_PATH + query)
                self.assertEqual(self.log(reply)["transport"]["mode"], "demo")

    def test_the_hints_say_it_is_synthetic(self) -> None:
        self.assertTrue(any("SYNTHETIC" in hint for hint in self.dto["display_hints"]))

    def test_it_carries_the_window_and_when_it_was_built(self) -> None:
        self.assertEqual(self.dto["window"]["from"], atlas_calls.DEMO_WINDOW_FROM)
        self.assertEqual(self.dto["window"]["to"], atlas_calls.DEMO_WINDOW_TO)
        observed = self.calls().json()["data"]["observed_at"]
        self.assertTrue(observed.endswith("Z"))

    def test_the_token_and_latency_the_ui_needs_are_present(self) -> None:
        row = self.dto["calls"][0]
        for field in (
            "ts",
            "status",
            "outcome",
            "tokens",
            "queue_ms",
            "ttft_ms",
            "total_ms",
            "request_id",
            "call_id",
            "model",
        ):
            self.assertIn(field, row)

    def test_request_ids_are_the_synthetic_ones_from_the_fixture(self) -> None:
        present = {row["request_id"] for row in self.dto["calls"] if row["request_id"]}
        self.assertTrue(present)
        self.assertTrue(all(value.startswith("req-SYN-") for value in present), present)

    def test_missing_values_stay_null_and_are_never_zeroed(self) -> None:
        """429 那条：usage / queue / ttft / total 全缺 —— 必须是 ``null``，不是 ``0``。"""
        throttled = [row for row in self.dto["calls"] if row["status"] == 429]
        self.assertTrue(throttled)
        for row in throttled:
            for field in ("queue_ms", "ttft_ms", "upstream_ms", "total_ms"):
                self.assertIsNone(row[field], field)
            self.assertEqual(
                row["tokens"], {"input": None, "output": None, "total": None}
            )

    def test_no_row_reports_a_zero_where_nothing_was_measured(self) -> None:
        for row in self.dto["calls"]:
            if row["tokens"]["total"] == 0:
                self.fail(f"a zero token total was invented: {row}")

    def test_the_body_is_exactly_the_contract_builder_output(self) -> None:
        """线上拿到的 DTO 与直接调用 ``dev.atlas_calls.build_dto`` **完全相同**。

        这条把「端点只是把会话用户喂给契约」变成一个可检验的等式，而不是一句注释。
        """
        from dev import atlas_calls as module

        session = self.server.app.auth.service.current_session(
            self.client.cookies[support.SESSION_COOKIE]
        )
        expected = module.build_dto(
            user=session.user, grant=module.SelfCallsGrant.for_subject(self.subject)
        )
        self.assertEqual(self.dto, expected)

    def test_the_response_is_the_configured_grant_and_not_a_rebuilt_one(self) -> None:
        """线上看到的主体与 key 集合，就是配置期那一份 grant 的内容。

        可见集合 = 目录里属于该主体的 key **减去**已吊销的那些；被减掉的部分必须
        出现在 ``scope.revoked_keys`` 里，而不是凭空消失。
        """
        grant = self.server.app.self_calls_grant
        self.assertIsNotNone(grant)
        owned = set(grant.directory.owned_by(grant.subject))
        revoked = {
            key_id for key_id in owned if grant.directory.lookup(key_id).revoked
        }
        self.assertEqual(self.dto["scope"]["subject"], grant.subject)
        self.assertEqual(set(self.dto["scope"]["keys"]), owned - revoked)
        self.assertEqual(set(self.dto["scope"]["revoked_keys"]), revoked)


# ---------------------------------------------------------------------------
# 4. 正文与 opt-in
# ---------------------------------------------------------------------------


class ContentTests(_Base):
    def test_content_is_never_included_without_opt_in(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        dto = self.log(self.calls())
        self.assertIs(dto["content"]["included"], False)
        self.assertEqual(dto["content"]["consent"], "absent")

    def test_opt_in_changes_the_status_and_nothing_else(self) -> None:
        """opt-in 只改一个状态字段 —— 明细逐字节不变，正文依旧不出现。"""
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        absent = self.log(self.calls())

        self.server.grant_self_calls(subject, content_consent=True)
        granted = self.log(self.calls())

        self.assertIs(absent["content"]["included"], False)
        self.assertIs(granted["content"]["included"], False)
        self.assertEqual(absent["calls"], granted["calls"])
        self.assertEqual(absent["content"]["consent"], "absent")
        self.assertEqual(granted["content"]["consent"], "granted")
        self.assertNotEqual(absent["content"]["reason"], granted["content"]["reason"])

    def test_no_row_field_looks_like_content(self) -> None:
        subject = self.sign_in_as_admin()
        self.server.grant_self_calls(subject)
        dto = self.log(self.calls())
        forbidden = {"prompt", "completion", "messages", "content", "text", "label"}
        for row in dto["calls"]:
            self.assertEqual({field for field in row if field.lower() in forbidden}, set())


# ---------------------------------------------------------------------------
# 5. 只读、参数与生产路由面
# ---------------------------------------------------------------------------


class ReadOnlyAndParamsTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.subject = self.sign_in_as_admin()
        self.server.grant_self_calls(self.subject)

    def test_write_verbs_are_405(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                reply = self.client.request(method, CALLS_PATH, body=b"{}")
                self.assertEqual(reply.status, 405)

    def test_a_repeated_parameter_is_refused(self) -> None:
        reply = self.calls(path=f"{CALLS_PATH}?key=key-alpha&key=key-orphan")
        self.assertEqual(reply.status, 400)
        self.assertEqual(reply.error_code(), errors.InvalidRequest.code)

    def test_an_empty_parameter_is_refused(self) -> None:
        self.assertEqual(self.calls(path=f"{CALLS_PATH}?key=").status, 400)

    def test_an_unsupported_outcome_is_400(self) -> None:
        self.assertEqual(self.calls(path=f"{CALLS_PATH}?outcome=exploded").status, 400)

    def test_a_non_numeric_limit_is_400(self) -> None:
        # ``%2B3`` 是字面上的 ``+3``（裸 ``+`` 在查询串里是空格）。
        for value in ("abc", "-1", "1.5", "99999", "0", "%2B3", "1e2", "3x"):
            with self.subTest(limit=value):
                self.assertEqual(self.calls(path=f"{CALLS_PATH}?limit={value}").status, 400)

    def test_a_bad_cursor_is_400(self) -> None:
        self.assertEqual(
            self.calls(path=f"{CALLS_PATH}?before_ts=not-a-time&before_call_id=call_x").status,
            400,
        )
        self.assertEqual(self.calls(path=f"{CALLS_PATH}?before_ts=2026-10-14T03:00:00Z").status, 400)

    def test_limit_narrows_the_page_and_reports_truncation(self) -> None:
        dto = self.log(self.calls(path=f"{CALLS_PATH}?limit=1"))
        self.assertEqual(len(dto["calls"]), 1)
        self.assertTrue(dto["counts"]["truncated"])
        self.assertIsNotNone(dto["counts"]["next_before"])

    def test_the_endpoint_is_absent_from_the_production_router(self) -> None:
        """生产路由器里没有这条路径 —— 一个真实监听器不会提供它。"""
        self.assertNotIn(CALLS_PATH, set(routes.ROUTES.values()))

    def test_the_two_preview_endpoints_are_distinct_paths(self) -> None:
        from dev import atlas_preview

        self.assertNotEqual(CALLS_PATH, atlas_preview.ATLAS_USAGE_PATH)
        self.assertNotIn(atlas_preview.ATLAS_USAGE_PATH, set(routes.ROUTES.values()))

    def test_response_headers_are_the_json_ones(self) -> None:
        reply = self.calls()
        self.assertIn("application/json", reply.header("Content-Type") or "")
        self.assertEqual(reply.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(reply.header("Cache-Control"), "no-store")

    def test_the_time_filter_narrows_the_window_server_side(self) -> None:
        """时间筛选是**服务端**口径：返回的 ``window`` 就是实际生效的那一个。"""
        dto = self.log(self.calls(path=f"{CALLS_PATH}?from=2026-10-14T01:00:00Z"))
        self.assertEqual(dto["window"]["from"], "2026-10-14T01:00:00Z")
        self.assertEqual(dto["window"]["to"], atlas_calls.DEMO_WINDOW_TO)
        # 01:00 之后只剩 03:00 / 03:40 两条；被排除的是 00:05 两条、00:20 一条
        # （都在新 from 之前）以及 04:30 一条（在演示 to 之后），共 4 条。
        self.assertEqual(dto["counts"]["available"], 2)
        self.assertEqual(dto["counts"]["window_excluded"], 4)

    def test_a_narrowed_window_that_matches_nothing_is_an_honest_empty_list(self) -> None:
        dto = self.log(
            self.calls(
                path=(
                    f"{CALLS_PATH}?from=2026-10-13T00:00:00Z&to=2026-10-13T01:00:00Z"
                )
            )
        )
        self.assertEqual(dto["calls"], [])
        self.assertEqual(dto["counts"]["returned"], 0)
        self.assertEqual(dto["counts"]["available"], 0)

    def test_a_reversed_or_naive_window_is_400(self) -> None:
        for query in (
            "from=2026-10-14T04:00:00Z&to=2026-10-14T01:00:00Z",
            "from=2026-10-14T01:00:00",
            "from=not-a-time",
            "to=",
        ):
            with self.subTest(query=query):
                self.assertEqual(self.calls(path=f"{CALLS_PATH}?{query}").status, 400)

    def test_a_window_longer_than_the_contract_limit_is_400(self) -> None:
        reply = self.calls(
            path=f"{CALLS_PATH}?from=2026-01-01T00:00:00Z&to=2026-12-31T00:00:00Z"
        )
        self.assertEqual(reply.status, 400)

# ---------------------------------------------------------------------------
# 6. 页面本身
# ---------------------------------------------------------------------------


class PageRouteTests(_Base):
    """``/calls`` 现在是指向外壳内视图的深链，不再是独立文档。

    这个视图的渲染由 `web/js/calls/*` 与 `tests/calls-log.test.js` 覆盖；这里只
    钉住**路由**：旧文档与其资源必须真的不可达，否则仓库里就会留下一份会腐烂的
    第二实现。
    """

    def test_the_deep_link_redirects_into_the_shell(self) -> None:
        reply = self.client.get("/calls")
        self.assertEqual(reply.status, 302)
        self.assertEqual(reply.header("Location"), "/atlas#calls")

    def test_the_deep_link_needs_no_session_to_redirect(self) -> None:
        """重定向本身不泄露任何东西，真正的把关在视图与端点两处。"""
        reply = self.client.get("/calls", cookies=False)
        self.assertEqual(reply.status, 302)

    def test_the_standalone_document_and_its_assets_are_gone(self) -> None:
        for path in ("/calls.html", "/css/calls.css", "/js/calls/controller.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, cookies=False).status, 404)

    def test_the_shell_carries_the_call_log_view(self) -> None:
        self.sign_in_as_admin()
        reply = self.client.get("/atlas")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.body.decode("utf-8")
        self.assertIn('id="view-calls"', body)
        self.assertIn('id="callsHost"', body)
        # 筛选控件是外壳的一部分，且只暴露端点真正读取的那几个参数。
        for element_id in ("callsFrom", "callsTo", "callsOutcome", "callsKey", "callsModel", "callsLimit"):
            self.assertIn(f'id="{element_id}"', body)

    def test_the_shell_carries_every_module_panel(self) -> None:
        self.sign_in_as_admin()
        body = self.client.get("/atlas").body.decode("utf-8")
        for module_id in ("engines", "playground", "observability", "evaluation", "administration"):
            with self.subTest(module=module_id):
                self.assertIn(f'data-module="{module_id}"', body)
                self.assertIn(f'id="module-{module_id}-body"', body)

    def test_the_shell_still_has_the_seven_original_views(self) -> None:
        """新增第八个视图不能动原来的七个。"""
        self.sign_in_as_admin()
        body = self.client.get("/atlas").body.decode("utf-8")
        for name in ("overview", "models", "api", "observability", "atlas", "evaluation", "admin"):
            with self.subTest(view=name):
                self.assertIn(f'id="view-{name}"', body)


if __name__ == "__main__":
    unittest.main()
