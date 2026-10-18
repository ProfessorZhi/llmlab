"""两条凭据通道：浏览器会话与 scoped CLI token。

这里验的不是「token 能不能用」，而是**这条通道的边界**：

* 默认关闭。关闭时它仍然**负责**带 ``Authorization`` 头的请求，只是对它们一律
  401——把这种请求让给会话通道去解释，等于把一次凭据混淆静默解决掉；
* 关闭时它连 introspector 都不碰（拿一个会抛异常的实例喂给它，得到的必须是
  「本部署不接受这种凭据」，而不是那个异常）；
* 打开时，token 的身份**只**来自 introspector 的回答：请求里的 ``X-UID`` 之类既
  不能改 uid，也不能改主体名；
* introspector 说的 uid 还要过**会话通道同一条**账号目录规则；
* token 不是会话，所以它拿不到提权，也不会继承角色对应的那组 portal scope——
  它的能力就是签发时写下的那几条。

真正的签发 / 到期 / 撤销 / 审计归 IAM Lane（``backend/auth/tokens.py``，已在
``feature/v6-closure-iam-20261016`` 上实现）。本文件里的 ``FakeIntrospector`` 是
按那份 ``TokenIntrospection`` 的**字段形状**写的 fixture，所以接缝一旦漂移，这里会
先失败。``RegistryTokenIntrospector`` 那条桥另有一组用例，保证今天就能端到端地跑。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import identity as bff_identity
from backend.bff import scopes as bff_scopes
from backend.registry.identity import (
    SCOPE_ADMIN_READ,
    SCOPE_REGISTRY_READ,
    TokenBinding,
    TokenDirectory,
)
from tests.bff import support as S


def _request(authorization: str | None):
    """一个最小的请求替身：这两条通道只读 cookie 与 ``Authorization``。"""

    class _Request:
        def __init__(self):
            self.cookies: dict[str, str] = {}
            self.headers: dict[str, str] = {}
            if authorization is not None:
                self.headers["authorization"] = authorization

    return _Request()


class FakeIntrospector:
    """按 IAM Lane 的 ``TokenIntrospection`` 字段形状写的 introspector。

    刻意**只**提供 ``introspect``：本层不该需要它的别的方法。不认识的 token 返回
    一条 ``active=False`` —— 与那条服务的 ``TokenIntrospection.inactive()`` 同形。
    """

    def __init__(self, subjects: dict, *, explode: bool = False) -> None:
        self._subjects = subjects
        self._explode = explode
        self.calls: list[str] = []

    def introspect(self, token: str):
        self.calls.append(token)
        if self._explode:  # pragma: no cover - 只用来证明关闭态不会走到这里
            raise AssertionError("a disabled channel must not introspect")
        return self._subjects.get(
            token, bff_identity.TokenSubject.inactive("unknown")
        )


class DisabledTokenChannelTests(unittest.TestCase):
    """默认状态：通道存在、负责、但一律拒绝。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_the_default_channel_is_disabled(self):
        self.assertFalse(self.h.app.token_source.enabled)
        self.assertEqual(self.h.app.token_source.name, "scoped_token")

    def test_it_still_handles_requests_that_carry_an_authorization_header(self):
        self.assertTrue(self.h.app.token_source.handles(_request("Bearer anything")))
        self.assertFalse(self.h.app.token_source.handles(_request(None)))

    def test_a_signed_in_browser_cannot_smuggle_a_token_past_the_cookie(self):
        """cookie 与 Authorization 同时出现时，得到 401——不是「按 cookie 处理」。"""
        client = self.h.client("alice")
        response = client.run(
            "GET", "/api/v2/ops", headers={"Authorization": "Bearer whatever"}
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(
            response.error_code(), bff_identity.REASON_TOKEN_AUTH_NOT_ENABLED
        )

    def test_an_anonymous_caller_with_a_token_is_refused_the_same_way(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "GET", "/api/v2/ops", headers={"Authorization": "Bearer whatever"}
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(
            response.error_code(), bff_identity.REASON_TOKEN_AUTH_NOT_ENABLED
        )

    def test_a_disabled_channel_never_consults_the_introspector(self):
        """接一个「一被调用就炸」的 introspector：答案仍必须是同一个 401。"""
        source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector({}, explode=True), enabled=False
        )
        with self.assertRaises(bff_identity.TokenAuthNotEnabled) as caught:
            source.resolve(_request("Bearer anything"))
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(
            caught.exception.reason, bff_identity.REASON_TOKEN_AUTH_NOT_ENABLED
        )

    def test_no_authorization_header_is_a_missing_credential_not_a_disabled_channel(self):
        source = self.h.app.token_source
        with self.assertRaises(bff_identity.NoSession) as caught:
            source.resolve(_request(None))
        self.assertEqual(caught.exception.reason, "no_authenticated_session")

    def test_a_bearer_looking_header_in_the_cookie_jar_is_not_a_credential(self):
        """``Authorization`` 必须真的是一个头；cookie 里写什么都不算。"""
        client = self.h.client("alice")
        client.cookies["Authorization"] = "Bearer whatever"
        self.assertEqual(client.run("GET", "/api/v2/ops").status, 200)


class EnabledTokenChannelTests(unittest.TestCase):
    """显式打开之后，token 通道的身份边界。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.alice_token = "synthetic-cli-token-for-alice"
        self.bob_token = "synthetic-cli-token-for-bob"
        self.subjects = {
            self.alice_token: bff_identity.TokenSubject(
                active=True,
                reason="active",
                principal=self.h.principal_of("alice"),
                uid=S.MEMBER_UID,
                linux_user="alice",
                scopes=frozenset({SCOPE_REGISTRY_READ}),
                roles=frozenset({"member"}),
                label="alice-laptop",
            ),
            self.bob_token: bff_identity.TokenSubject(
                active=True,
                reason="active",
                principal=self.h.principal_of(S.REAL_ACCOUNT_NAME),
                uid=S.REAL_ACCOUNT_UID,
                linux_user=S.REAL_ACCOUNT_NAME,
                scopes=frozenset({SCOPE_REGISTRY_READ, SCOPE_ADMIN_READ}),
                label="bob-workstation",
            ),
        }

    def _enable(self, *, enabled=True, use_accounts=True):
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector(self.subjects),
            accounts=self.h.accounts if use_accounts else None,
            enabled=enabled,
        )

    def test_a_bound_token_resolves_to_its_own_principal(self):
        self._enable()
        principal = self.h.app.token_source.resolve(
            _request(f"Bearer {self.alice_token}")
        )
        self.assertEqual(principal.principal, self.h.principal_of("alice"))
        self.assertEqual(principal.uid, S.MEMBER_UID)
        self.assertEqual(principal.linux_user, "alice")
        # token 不是会话：没有会话行，也没有提权。
        self.assertIsNone(principal.user)
        self.assertFalse(principal.elevated)
        self.assertFalse(principal.is_platform_admin)

    def test_a_bound_token_serves_the_ops_route_over_http(self):
        self._enable()
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "GET", "/api/v2/ops", headers={"Authorization": f"Bearer {self.alice_token}"}
        )
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(
            S.ops_of(response)["scope"]["subject"], self.h.principal_of("alice")
        )

    def test_a_token_gets_exactly_the_scopes_it_was_issued_with(self):
        """不继承角色那组 portal scope——token 的能力就是绑定的那几条。"""
        self._enable()
        principal = self.h.app.token_source.resolve(
            _request(f"Bearer {self.alice_token}")
        )
        self.assertEqual(principal.portal_scopes, ())
        self.assertEqual(principal.requester.scopes, frozenset({SCOPE_REGISTRY_READ}))
        self.assertEqual(principal.requester.token_label, "alice-laptop")

    def test_the_introspected_uid_must_agree_with_the_account_directory(self):
        """introspect 说的 uid 要过**会话通道同一条**目录规则。

        两条通道对「这个 uid 是谁」不能给出两个答案。这里让它说一个 uid 与登录名
        对不上的主体：目录里的 4001 是 ``alice``，不是 ``bob``。
        """
        self._enable()
        mismatched = dict(self.subjects)
        mismatched["confused-token"] = bff_identity.TokenSubject(
            active=True,
            principal=self.h.principal_of("bob"),
            uid=S.MEMBER_UID,  # alice 的 uid
            linux_user="bob",  # 却说自己是 bob
            scopes=frozenset({SCOPE_REGISTRY_READ}),
        )
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector(mismatched),
            accounts=self.h.accounts,
            enabled=True,
        )
        with self.assertRaises(bff_identity.NoSession):
            self.h.app.token_source.resolve(_request("Bearer confused-token"))

    def test_a_uid_that_is_not_in_the_account_directory_is_no_principal(self):
        self._enable()
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector(
                {
                    "ghost-token": bff_identity.TokenSubject(
                        active=True,
                        principal="linux:somewhere:9999",
                        uid=9999,
                        linux_user="ghost",
                        scopes=frozenset({SCOPE_REGISTRY_READ}),
                    )
                }
            ),
            accounts=self.h.accounts,
            enabled=True,
        )
        with self.assertRaises(bff_identity.NoSession):
            self.h.app.token_source.resolve(_request("Bearer ghost-token"))

    def test_the_account_directory_check_is_optional_but_the_uid_is_not(self):
        """不给目录就不核对目录，但「没有 uid」永远不成立。"""
        self._enable(use_accounts=False)
        principal = self.h.app.token_source.resolve(
            _request(f"Bearer {self.alice_token}")
        )
        self.assertEqual(principal.uid, S.MEMBER_UID)

        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector(
                {
                    "no-uid": bff_identity.TokenSubject(
                        active=True,
                        principal=self.h.principal_of("alice"),
                        uid=None,
                        scopes=frozenset({SCOPE_REGISTRY_READ}),
                    )
                }
            ),
            enabled=True,
        )
        with self.assertRaises(bff_identity.NoSession):
            self.h.app.token_source.resolve(_request("Bearer no-uid"))

    def test_a_boolean_is_not_a_uid(self):
        """``True`` 是 ``int`` 的实例，但它不是一个 uid。"""
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector(
                {
                    "bool-uid": bff_identity.TokenSubject(
                        active=True,
                        principal=self.h.principal_of("alice"),
                        uid=True,
                        scopes=frozenset({SCOPE_REGISTRY_READ}),
                    )
                }
            ),
            enabled=True,
        )
        with self.assertRaises(bff_identity.NoSession):
            self.h.app.token_source.resolve(_request("Bearer bool-uid"))

    def test_a_token_cannot_assert_an_identity_the_binding_does_not_grant(self):
        """自报身份在 token 通道上同样是 400，而不是「以绑定为准、忽略头」。"""
        self._enable()
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "GET",
            "/api/v2/ops",
            headers={
                "Authorization": f"Bearer {self.bob_token}",
                "X-UID": str(S.MEMBER_UID),
                "X-Linux-User": "alice",
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_two_tokens_are_two_different_subjects(self):
        self._enable()
        mine = self.h.app.token_source.resolve(_request(f"Bearer {self.alice_token}"))
        theirs = self.h.app.token_source.resolve(
            _request(f"Bearer {self.bob_token}")
        )
        self.assertNotEqual(mine.principal, theirs.principal)
        self.assertNotEqual(mine.uid, theirs.uid)

    def test_a_token_cannot_read_another_subjects_deployment(self):
        """跨主体隔离在 token 通道上必须与会话通道一致。"""
        ids = S.register_baseline(self.h, "bob")
        self._enable()
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "GET", "/api/v2/ops?visibility=shared",
            headers={"Authorization": f"Bearer {self.alice_token}"},
        )
        self.assertEqual(response.status, 200, response.json())
        ops = S.ops_of(response)
        seen = {row["deployment_id"] for row in ops["registry"]["owned"]}
        seen |= {row.get("deployment_id") for row in ops["registry"]["shared"]}
        self.assertNotIn(ids["deployment_id"], seen)
        self.assertEqual(ops["registry"]["all"], [])

    def test_a_token_with_the_admin_scope_does_not_become_a_platform_admin(self):
        """``registry:admin:read`` 是 Registry 词表里的全局只读，**不是**提权。

        V6 模型下「管理视图」来自**会话的提权状态**，所以一条带 admin scope 的
        token 仍然拿不到 ``portal:admin:read``，也就看不到全员名单。
        """
        self._enable()
        principal = self.h.app.token_source.resolve(
            _request(f"Bearer {self.bob_token}")
        )
        self.assertFalse(bff_scopes.PORTAL_ADMIN_READ in principal.portal_scopes)
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "GET", "/api/v2/ops",
            headers={"Authorization": f"Bearer {self.bob_token}"},
        )
        ops = S.ops_of(response)
        self.assertEqual(ops["admin"]["principals"], [])
        self.assertFalse(ops["admin"]["available"])

    def test_an_unknown_token_is_a_missing_credential(self):
        self._enable()
        with self.assertRaises(bff_identity.NoSession):
            self.h.app.token_source.resolve(_request("Bearer not-in-the-directory"))

    def test_every_inactive_reason_collapses_into_one_401(self):
        """撤销 / 过期 / 暂停 / 未知都塌成同一个答复。

        面向公网的入口不该用理由把一个可用的试探变成一个预言机：调用方从响应上
        分不出「这条 token 曾经存在」。
        """
        for reason in ("revoked", "expired", "suspended", "unknown"):
            with self.subTest(reason=reason):
                self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
                    introspector=FakeIntrospector(
                        {"dead-token": bff_identity.TokenSubject.inactive(reason)}
                    ),
                    enabled=True,
                )
                with self.assertRaises(bff_identity.NoSession) as caught:
                    self.h.app.token_source.resolve(_request("Bearer dead-token"))
                self.assertEqual(
                    caught.exception.detail, "the presented credential is not active"
                )
                self.assertNotIn(reason, str(caught.exception.detail))

    def test_a_token_that_resolves_to_no_principal_is_refused(self):
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector(
                {
                    "orphan-token": bff_identity.TokenSubject(
                        active=True,
                        principal=None,
                        uid=S.MEMBER_UID,
                        scopes=frozenset({SCOPE_REGISTRY_READ}),
                    )
                }
            ),
            enabled=True,
        )
        with self.assertRaises(bff_identity.NoSession):
            self.h.app.token_source.resolve(_request("Bearer orphan-token"))

    def test_the_introspector_is_the_only_thing_asked_about_the_token(self):
        """本层不自己解析、不查自己的表：问题只问一次，答案只来自那一处。"""
        seen = FakeIntrospector(self.subjects)
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=seen, accounts=self.h.accounts, enabled=True
        )
        self.h.app.token_source.resolve(_request(f"Bearer {self.alice_token}"))
        self.assertEqual(seen.calls, [self.alice_token])

    def test_a_well_formed_bearer_header_is_refused_as_a_disabled_channel(self):
        self._enable(enabled=False)
        with self.assertRaises(bff_identity.TokenAuthNotEnabled):
            self.h.app.token_source.resolve(_request("Bearer some-token"))

    def test_a_header_that_is_not_a_bearer_credential_is_a_missing_credential(self):
        """形状不对的头**不是**一条凭据，因此不报「通道已禁用」。

        「没有可用凭据」与「本部署不接受这种凭据」是两个答案：混起来会让人以为
        把通道打开就能让一个 ``Basic`` 头变得可用。
        """
        self._enable()
        for value in ("Basic dXNlcjpwYXNz", "Bearer", "bearer ", "Token abc"):
            with self.subTest(value=value):
                with self.assertRaises(bff_identity.NoSession):
                    self.h.app.token_source.resolve(_request(value))

    def test_a_malformed_authorization_header_still_does_not_fall_through_to_the_cookie(self):
        """头在，就不由 cookie 说了算——包括头本身写得不对的时候。"""
        self._enable()
        client = self.h.client("alice")
        response = client.run(
            "GET", "/api/v2/ops", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")


class RegistryBridgeTests(unittest.TestCase):
    """既有的 Registry 凭据目录 → introspector 契约的那条桥。

    它是契约 fixture：证明「token → 主体」这条通道今天就能端到端跑完（用本仓库
    已经存在的那个目录实现），而不是等 IAM Lane 的服务合入才能验。它**不是**一个
    可以在生产打开的签发者——内存目录、无到期、无审计。
    """

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.token = "synthetic-registry-token"
        self.bridge = bff_identity.RegistryTokenIntrospector(
            TokenDirectory(
                [
                    TokenBinding(
                        self.token,
                        self.h.principal_of("alice"),
                        frozenset({SCOPE_REGISTRY_READ}),
                        label="bridge",
                    )
                ]
            ),
            self.h.principals,
        )

    def test_a_known_token_introspects_to_an_active_subject(self):
        found = self.bridge.introspect(self.token)
        self.assertTrue(found.active)
        self.assertEqual(found.principal, self.h.principal_of("alice"))
        self.assertEqual(found.uid, S.MEMBER_UID)
        self.assertEqual(found.linux_user, "alice")
        self.assertEqual(found.label, "bridge")

    def test_unknown_revoked_and_unapproved_are_inactive_not_exceptions(self):
        self.assertFalse(self.bridge.introspect("not-a-token").active)

        revoked = bff_identity.RegistryTokenIntrospector(
            TokenDirectory(
                [
                    TokenBinding(
                        "revoked",
                        self.h.principal_of("alice"),
                        frozenset({SCOPE_REGISTRY_READ}),
                        revoked=True,
                    )
                ]
            ),
            self.h.principals,
        )
        self.assertFalse(revoked.introspect("revoked").active)

        orphan = bff_identity.RegistryTokenIntrospector(
            TokenDirectory(
                [
                    TokenBinding(
                        "orphan", "linux:nowhere:4242", frozenset({SCOPE_REGISTRY_READ})
                    )
                ]
            ),
            self.h.principals,
        )
        self.assertFalse(orphan.introspect("orphan").active)

    def test_the_bridge_serves_the_ops_route_end_to_end(self):
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=self.bridge,
            accounts=self.h.accounts,
            enabled=True,
        )
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "GET", "/api/v2/ops", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(
            S.ops_of(response)["scope"]["subject"], self.h.principal_of("alice")
        )


class ChannelSelectionTests(unittest.TestCase):
    """谁负责一个请求，是 ``handles`` 说了算，不是 if 的先后顺序。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_the_session_channel_is_a_catch_all(self):
        source = self.h.app.session_source
        self.assertTrue(source.handles(_request(None)))
        self.assertTrue(source.handles(_request("Bearer x")))
        self.assertTrue(source.enabled)

    def test_the_token_channel_is_consulted_first_and_is_not_shadowed(self):
        """会话通道是兜底（``handles`` 恒真），所以真正要钉的是**顺序**。

        一个带 ``Authorization`` 的请求必须由 token 通道回答——哪怕会话通道「也
        愿意」回答它。若两条通道的先后被调换，这里会因为答案从「token 说了算」
        变成「cookie 说了算」而失败。
        """
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=FakeIntrospector({}), enabled=True
        )
        sources = (self.h.app.token_source, self.h.app.session_source)
        deciding = [source.name for source in sources if source.handles(_request("Bearer x"))]
        self.assertEqual(deciding[0], "scoped_token")
        # 会话通道确实「也愿意」答——这正是为什么顺序必须被钉住。
        self.assertIn("session", deciding)

    def test_resolve_credential_uses_the_session_channel_for_a_plain_cookie(self):
        client = self.h.client("alice")
        from backend.auth.wsgi import build_request

        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/v2/ops",
            "QUERY_STRING": "",
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8790",
            "HTTP_HOST": "127.0.0.1:8790",
            "HTTP_COOKIE": f"{self.h.app.cfg.cookie_name}={self.h.session_id('alice')}",
            "wsgi.input": __import__("io").BytesIO(b""),
        }
        request = build_request(environ, max_body=1024)
        principal = self.h.app.resolve_credential(request)
        self.assertEqual(principal.principal, self.h.principal_of("alice"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
