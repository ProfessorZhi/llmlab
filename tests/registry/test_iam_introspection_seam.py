"""SEAM-2：把 IAM 的 `ScopedTokenService` 接成 Registry 的**受信** token 权威。

## 这条缝原来为什么不存在

`resolve_requester` 对第二个参数做 ``isinstance(tokens, TokenDirectory)`` 硬校验。
IAM 的真实服务不是 `TokenDirectory`，所以 Registry 模块的 HTTP 层**接不上**它。
把 IAM token 复制进第二个 `TokenDirectory` 能绕过去，但那是**第二套 token 账本**：
撤销、到期、主体停用都要在两边各同步一次，而漏掉任何一次都表现为「已撤销的凭据仍然有效」。
本模块不这样做。

## 本文件钉住的东西

1. **受信适配器**把 IAM 的回答翻成 Registry 的主体，**只读回答、不读 token 内容**；
   一个长得像 JSON claims 的字符串不会被当作身份。
2. token 来自**签发方**：伪造 / 过期 / 已撤销 / 主体停用 / 主体移除，下一次请求
   立刻 401，不需要任何同步步骤。
3. 回答必须与**受信主体目录**一致：principal / uid / linux_user 三者任一不符即拒。
4. 回答里的 scope 必须落在本部署认识的词表内；未知 scope 整条 token 被拒
   （静默丢弃会把一次配置漂移变成一次权限变化）。
5. **任意对象不能当权威**：只有 `TokenDirectory` 与 `TrustedTokenIntrospector`
   两种；别的类型是部署配置错误，按 503 失败关闭，绝不合成一个主体。
6. 既有 `TokenDirectory` 路径**逐字不变**（CLI 的既有测试继续成立）。
7. 换了权威之后，Router ACK 与 owner ACL 的信任边界不变。
"""

from __future__ import annotations

import io
import json
import pathlib
import shutil
import tempfile
import unittest

from backend.auth import tokens as auth_tokens
from backend.registry.errors import (
    InvalidRequest,
    TokenAuthorityUnavailable,
    Unauthenticated,
)
from backend.registry.identity import (
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TokenBinding,
    TokenDirectory,
    TrustedTokenIntrospector,
    resolve_requester,
)
from backend.registry.registry import RegistryConfig, RegistryService
from backend.registry.store import Store
from tests.registry.test_trusted_gateway_and_router import ConfirmingRouterAdapter

ALICE = "linux:dell-test-01:4001"
BOB = "linux:dell-test-01:4003"
DAVE = "linux:dell-test-01:4002"

PRINCIPALS = PrincipalDirectory(
    [
        Principal(ALICE, "alice", 4001),
        Principal(BOB, "bob", 4003),
        Principal(DAVE, "dave", 4002),
    ]
)


def _call(app, method: str, path: str, *, token: str | None = None, body: object = None):
    """在进程内驱动 Registry 的 WSGI 应用，返回 ``(status, headers, payload)``。"""
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    environ: dict = {
        "REQUEST_METHOD": method.upper(),
        "PATH_INFO": path,
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(payload),
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "8787",
        "CONTENT_LENGTH": str(len(payload)),
    }
    if payload:
        environ["CONTENT_TYPE"] = "application/json"
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    captured: dict = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(str(status).split()[0])
        captured["headers"] = dict(headers)

    raw = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], json.loads(raw.decode("utf-8") or "{}")


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Iam:
    """一套真的 IAM 签发服务 + 它的受信主体状态目录。"""

    def __init__(self, tmp: pathlib.Path, clock: _Clock) -> None:
        self.clock = clock
        self.state = auth_tokens.MappingPrincipalState(
            {
                ALICE: auth_tokens.STATE_APPROVED,
                BOB: auth_tokens.STATE_APPROVED,
                DAVE: auth_tokens.STATE_APPROVED,
            }
        )
        self.service = auth_tokens.ScopedTokenService(
            auth_tokens.TokenStore(tmp / "scoped-tokens.sqlite3", clock=clock),
            principal_state=self.state,
            clock=clock,
        )
        self.service.initialize()

    def issue(self, name: str, *, scopes=("registry:read",), ttl_seconds=None):
        # IAM 的 ``principal`` 是平台主体 id（``linux:<host>:<uid>``），
        # ``linux_user`` 才是登录名 —— 与 ``PrincipalDirectory`` 的键一致。
        principal, uid = {"alice": (ALICE, 4001), "bob": (BOB, 4003), "dave": (DAVE, 4002)}[name]
        return self.service.issue(
            principal=principal,
            uid=uid,
            linux_user=name,
            scopes=scopes,
            roles=("member",),
            label="seam2-test",
            ttl_seconds=ttl_seconds,
        )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = pathlib.Path(tempfile.mkdtemp(prefix="seam2-"))
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.clock = _Clock()
        self.iam = _Iam(self._dir, self.clock)
        self.authority = TrustedTokenIntrospector(
            self.iam.service, principals=PRINCIPALS, clock=self.clock
        )

    def resolve(self, token: object) -> RequesterContext:
        return resolve_requester(token, self.authority, PRINCIPALS)


# ---------------------------------------------------------------------------
# 1. 受信适配器：接受签发方的活 token
# ---------------------------------------------------------------------------


class AcceptsIssuerTokensTest(_Base):
    def test_a_live_iam_token_resolves_to_the_right_principal(self) -> None:
        issued = self.iam.issue("bob", scopes=("registry:read", "registry:endpoint:invoke"))
        requester = self.resolve(issued.token)
        self.assertEqual(requester.principal, BOB)
        self.assertEqual(requester.uid, 4003)
        self.assertEqual(requester.linux_user, "bob")
        self.assertEqual(
            requester.scopes, frozenset({"registry:read", "registry:endpoint:invoke"})
        )
        self.assertEqual(requester.roles, frozenset({"member"}))
        self.assertEqual(requester.token_label, "seam2-test")

    def test_two_tokens_are_two_subjects(self) -> None:
        a = self.resolve(self.iam.issue("alice").token)
        b = self.resolve(self.iam.issue("bob").token)
        self.assertNotEqual(a.principal, b.principal)
        self.assertNotEqual(a.uid, b.uid)

    def test_scopes_come_from_the_issuer_not_the_request(self) -> None:
        issued = self.iam.issue("alice", scopes=("registry:read",))
        requester = self.resolve(issued.token)
        self.assertEqual(requester.scopes, frozenset({"registry:read"}))
        self.assertNotIn("registry:admin:manage", requester.scopes)


# ---------------------------------------------------------------------------
# 2. 伪造 / 过期 / 撤销 / 停用：下一次请求立刻 401
# ---------------------------------------------------------------------------


class WrongCredentialFailsImmediatelyTest(_Base):
    def test_an_unknown_token_is_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self.resolve("llmlab_scoped_not_a_real_token")

    def test_a_token_that_looks_like_claims_cannot_mint_a_principal(self) -> None:
        # 本模块**不解析** token；一个像 JSON 的字符串只是未知凭据。
        forged = '{"principal": "%s", "uid": 4001, "scopes": ["registry:admin:manage"]}' % ALICE
        with self.assertRaises(Unauthenticated):
            self.resolve(forged)

    def test_an_empty_or_non_string_token_is_401(self) -> None:
        for bad in ("", None, 123, b"bytes"):
            with self.subTest(token=bad):
                with self.assertRaises(Unauthenticated):
                    self.resolve(bad)

    def test_an_expired_token_is_401_on_the_next_call(self) -> None:
        issued = self.iam.issue("alice", ttl_seconds=60)
        self.assertEqual(self.resolve(issued.token).principal, ALICE)
        self.clock.now += 61
        with self.assertRaises(Unauthenticated):
            self.resolve(issued.token)

    def test_a_revoked_token_is_401_on_the_next_call(self) -> None:
        issued = self.iam.issue("alice")
        self.assertEqual(self.resolve(issued.token).principal, ALICE)
        self.assertTrue(self.iam.service.revoke(issued.token))
        with self.assertRaises(Unauthenticated):
            self.resolve(issued.token)

    def test_a_suspended_principal_is_401_on_the_next_call(self) -> None:
        issued = self.iam.issue("alice")
        self.assertEqual(self.resolve(issued.token).principal, ALICE)
        self.iam.state.suspend(ALICE)
        with self.assertRaises(Unauthenticated):
            self.resolve(issued.token)

    def test_a_removed_principal_is_401_on_the_next_call(self) -> None:
        issued = self.iam.issue("alice")
        self.assertEqual(self.resolve(issued.token).principal, ALICE)
        self.iam.state.forget(ALICE)
        with self.assertRaises(Unauthenticated):
            self.resolve(issued.token)


# ---------------------------------------------------------------------------
# 3. 回答必须与受信主体目录一致；scope 词表封闭
# ---------------------------------------------------------------------------


class _FakeIntrospector:
    """一个**测试内**的回答者：用来喂出形状不对的回答。"""

    def __init__(self, answer: object) -> None:
        self.answer = answer

    def introspect(self, raw_token: object) -> object:
        return self.answer


def _answer(**overrides):
    base = dict(
        active=True,
        reason="active",
        principal=ALICE,
        uid=4001,
        linux_user="alice",
        state=auth_tokens.STATE_APPROVED,
        scopes=frozenset({"registry:read"}),
        roles=frozenset(),
        label="fake",
        token_id="tok_1",
        expires_at=None,
    )
    base.update(overrides)
    return _Answer(**base)


class _Answer:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class MismatchWithTheDirectoryTest(_Base):
    def _authority_for(self, answer) -> TrustedTokenIntrospector:
        return TrustedTokenIntrospector(
            _FakeIntrospector(answer), principals=PRINCIPALS, clock=self.clock
        )

    def _resolve(self, answer) -> RequesterContext:
        return resolve_requester("whatever", self._authority_for(answer), PRINCIPALS)

    def test_a_matching_answer_is_accepted(self) -> None:
        self.assertEqual(self._resolve(_answer()).principal, ALICE)

    def test_an_inactive_answer_is_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(active=False))

    def test_an_unapproved_answer_is_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(state="suspended"))

    def test_a_contradictory_uid_is_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(uid=4003))

    def test_a_contradictory_linux_user_is_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(linux_user="bob"))

    def test_an_unknown_principal_is_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(principal="linux:dell-test-01:9999", uid=9999))

    def test_an_unknown_scope_rejects_the_whole_token(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(scopes=frozenset({"registry:read", "registry:root"})))

    def test_malformed_answers_are_401(self) -> None:
        cases = (
            _answer(principal=""),
            _answer(principal=None),
            _answer(uid=True),
            _answer(uid="4001"),
            _answer(linux_user=None),
            _answer(scopes="registry:read"),
        )
        for index, answer in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(Unauthenticated):
                    self._resolve(answer)

    def test_an_expired_answer_is_401_even_if_the_issuer_forgot(self) -> None:
        with self.assertRaises(Unauthenticated):
            self._resolve(_answer(expires_at=self.clock.now - 1))


# ---------------------------------------------------------------------------
# 4. 任意对象不能当权威；不可用的权威 503
# ---------------------------------------------------------------------------


class TheAuthorityMustBeTrustedTest(_Base):
    def test_the_raw_iam_service_is_refused(self) -> None:
        # 它是**真服务**，但没有经过本层适配：不接受。
        with self.assertRaises(InvalidRequest) as raised:
            resolve_requester(
                self.iam.issue("alice").token, self.iam.service, PRINCIPALS
            )
        message = str(raised.exception)
        self.assertIn("TokenDirectory", message)
        self.assertIn("TrustedTokenIntrospector", message)

    def test_an_arbitrary_object_is_refused_as_503(self) -> None:
        class Duck:
            def introspect(self, raw_token):  # pragma: no cover - never called
                return _answer()

        with self.assertRaises(TokenAuthorityUnavailable):
            resolve_requester("x", Duck(), PRINCIPALS)

    def test_an_authority_that_cannot_answer_is_503_not_401(self) -> None:
        class Broken:
            def introspect(self, raw_token):
                raise RuntimeError("authority offline")

        authority = TrustedTokenIntrospector(Broken(), principals=PRINCIPALS)
        with self.assertRaises(TokenAuthorityUnavailable):
            resolve_requester("x", authority, PRINCIPALS)

    def test_a_missing_authority_is_401_not_success(self) -> None:
        # 缺省部署 = 空目录：任何 token 都解析不出主体。
        with self.assertRaises(Unauthenticated):
            resolve_requester("x", TokenDirectory(()), PRINCIPALS)


# ---------------------------------------------------------------------------
# 5. 既有 TokenDirectory 路径逐字不变
# ---------------------------------------------------------------------------


class TheDirectoryPathIsUnchangedTest(unittest.TestCase):
    def test_a_directory_binding_still_resolves(self) -> None:
        directory = TokenDirectory(
            [TokenBinding("synthetic-alice", ALICE, frozenset({"registry:read"}))]
        )
        requester = resolve_requester("synthetic-alice", directory, PRINCIPALS)
        self.assertEqual(requester.principal, ALICE)
        self.assertEqual(requester.scopes, frozenset({"registry:read"}))

    def test_a_revoked_binding_is_still_401(self) -> None:
        directory = TokenDirectory(
            [TokenBinding("synthetic-alice", ALICE, frozenset({"registry:read"}), revoked=True)]
        )
        with self.assertRaises(Unauthenticated):
            resolve_requester("synthetic-alice", directory, PRINCIPALS)


# ---------------------------------------------------------------------------
# 6. 换权威之后，HTTP 信任边界与 Router ACK 不变
# ---------------------------------------------------------------------------


class _ServiceWithIam(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.service = RegistryService(
            store=self.store,
            tokens=self.authority,
            principals=PRINCIPALS,
            config=RegistryConfig(mode="synthetic"),
        )
        from backend.registry.health import ProbeResult, StaticProber

        self.prober = StaticProber({8000: ProbeResult(reachable=True, latency_ms=0.5)})
        self.service.prober = self.prober

    def requester(self, name: str) -> RequesterContext:
        scopes = (
            "registry:read",
            "registry:write",
            "registry:endpoint:share",
            "registry:endpoint:invoke",
        )
        return self.resolve(self.iam.issue(name, scopes=scopes).token)


class RouterTrustIsUnchangedTest(_ServiceWithIam):
    def test_router_ack_still_binds_to_the_published_snapshot(self) -> None:
        owner = self.requester("alice")
        service = self.service
        engine, _ = service.register_engine(
            owner, {"name": "eng", "target_url": "http://127.0.0.1:8000/v1"}
        )
        model, _ = service.register_model(owner, {"name": "mdl"})
        deployment, _ = service.register_deployment(
            owner,
            {
                "name": "dep",
                "engine_id": engine["data"]["id"],
                "model_id": model["data"]["id"],
            },
        )
        service.verify_deployment(owner, deployment["data"]["id"])
        endpoint, _ = service.register_endpoint(
            owner,
            {"name": "ep", "deployment_id": deployment["data"]["id"]},
        )
        service.verify_endpoint(owner, endpoint["data"]["id"])

        engine_id = engine["data"]["id"]
        import dataclasses

        service.config = dataclasses.replace(
            service.config,
            engine_secret_refs={engine_id: {"file": "/etc/llmlab/e.env", "var": "K"}},
        )
        service.router = ConfirmingRouterAdapter()

        # 同一 engine 上的兄弟 deployment，才是合法的 repoint 目标。
        model2, _ = service.register_model(owner, {"name": "mdl2"})
        target, _ = service.register_deployment(
            owner,
            {"name": "dep2", "engine_id": engine_id, "model_id": model2["data"]["id"]},
        )
        service.verify_deployment(owner, target["data"]["id"])
        revision = service.store.endpoint_by_id(endpoint["data"]["id"]).revision
        view = service.repoint_endpoint(
            owner,
            endpoint["data"]["id"],
            {"target_deployment_id": target["data"]["id"], "expected_revision": revision},
        )
        self.assertEqual(view["meta"]["router_sync"], "router_sync_applied")
        self.assertEqual(
            view["meta"]["router_sync_report"]["acked_endpoints"], [endpoint["data"]["id"]]
        )

    def test_a_foreign_uid_cannot_read_the_endpoint(self) -> None:
        from backend.registry.api import create_app

        app = create_app(self.service)
        owner = self.requester("alice")
        engine, _ = self.service.register_engine(
            owner, {"name": "eng", "target_url": "http://127.0.0.1:8000/v1"}
        )
        model, _ = self.service.register_model(owner, {"name": "mdl"})
        deployment, _ = self.service.register_deployment(
            owner,
            {
                "name": "dep",
                "engine_id": engine["data"]["id"],
                "model_id": model["data"]["id"],
            },
        )
        self.service.verify_deployment(owner, deployment["data"]["id"])
        endpoint, _ = self.service.register_endpoint(
            owner,
            {"name": "ep", "deployment_id": deployment["data"]["id"]},
        )
        self.service.verify_endpoint(owner, endpoint["data"]["id"])

        token_b = self.iam.issue("bob").token
        status, _headers, payload = _call(
            app,
            "GET",
            f"/api/v2/registry/endpoints/{endpoint['data']['id']}",
            token=token_b,
        )
        self.assertEqual(status, 404, payload)


if __name__ == "__main__":
    unittest.main()
