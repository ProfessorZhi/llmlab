"""D3：Registry 只消费**自己的**六个 scope，外模块的合法 scope 不能把 token 整条拒掉。

## 缺陷（修复前实测，来自 TEST-ONLY 纵切 #121）

IAM 允许合法的多 namespace scope。真实 token 会同时携带
``registry:read`` / ``atlas:calls:self:read`` / ``observability:self:read`` /
``portal:modules:read``。而 ``TrustedTokenIntrospector._validate`` 要求
``scopes ⊆ KNOWN_SCOPES``，Registry 的 ``KNOWN_SCOPES`` 只有六个 registry 项 ——
于是**一条完全合法的 token 被整条 401**，Registry 侧什么都做不了。

## 规则（本文件钉住）

| 输入的 scope | 结果 |
| --- | --- |
| 形状非法（大小写、空段、空白、控制字符、段数越界） | **整条 token 401** |
| `registry:` 命名空间、且是本部署已知的六项之一 | 保留，参与授权 |
| `registry:` 命名空间、但本部署不认识 | **整条 token 401**（不猜它是什么意思） |
| 其它命名空间（`atlas:` / `observability:` / `portal:` …） | **保留 token，丢弃该 scope** |

丢弃是**收窄**：外模块的 scope 本来就不产生任何 Registry 权限。反过来把整条
token 拒掉，才是把「Registry 不认识这个 scope」误判成「这个凭据无效」。

**不扩容授权词表**：`KNOWN_SCOPES` 仍是六个。未知 registry scope 不会因为
「看起来像是更高的权限」而被接受 —— 它让整条 token 失败关闭。

## 与 #122 的关系

token 能用了不等于能调用。`invoke` 仍要求路由被 Gateway 回读确认为 applied；
本文件同时断言「带着合法 token 的 pending 端点 → 409 且 ``gateway.calls == 0``」。
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

from backend.auth import tokens as auth_tokens
from backend.registry.errors import Unauthenticated
from backend.registry.identity import (
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TrustedTokenIntrospector,
    resolve_requester,
)
from tests.registry.support import Harness

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

#: 真实的跨模块 scope 组合：一条 IAM token 会同时带上它们。
FOREIGN_SCOPES = (
    "atlas:calls:self:read",
    "observability:self:read",
    "portal:modules:read",
    "evaluation:read",
)


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


DEFAULT_ACCOUNTS = {
    "alice": (ALICE, 4001),
    "bob": (BOB, 4003),
    "dave": (DAVE, 4002),
}


class _Iam:
    def __init__(self, tmp: pathlib.Path, clock: _Clock, *, accounts=None) -> None:
        self.clock = clock
        #: 登录名 -> (平台 principal id, uid)。Registry 的 ``PrincipalDirectory``
        #: 用哪个 id 决定这里发什么；两个夹具必须对同一个 uid 给出同一个答案。
        self.accounts = dict(accounts or DEFAULT_ACCOUNTS)
        self.state = auth_tokens.MappingPrincipalState(
            {
                principal: auth_tokens.STATE_APPROVED
                for principal, _uid in self.accounts.values()
            }
        )
        self.service = auth_tokens.ScopedTokenService(
            auth_tokens.TokenStore(tmp / "scoped-tokens.sqlite3", clock=clock),
            principal_state=self.state,
            clock=clock,
        )
        self.service.initialize()

    def issue(self, name: str, *, scopes, ttl_seconds=None):
        principal, uid = self.accounts[name]
        return self.service.issue(
            principal=principal,
            uid=uid,
            linux_user=name,
            scopes=scopes,
            roles=("member",),
            label="d3-test",
            ttl_seconds=ttl_seconds,
        )


class _Answer:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _FakeIntrospector:
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


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = pathlib.Path(tempfile.mkdtemp(prefix="d3-"))
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.clock = _Clock()
        self.iam = _Iam(self._dir, self.clock)
        self.authority = TrustedTokenIntrospector(
            self.iam.service, principals=PRINCIPALS, clock=self.clock
        )

    def resolve(self, token: object) -> RequesterContext:
        return resolve_requester(token, self.authority, PRINCIPALS)

    def resolve_answer(self, answer) -> RequesterContext:
        authority = TrustedTokenIntrospector(
            _FakeIntrospector(answer), principals=PRINCIPALS, clock=self.clock
        )
        return resolve_requester("whatever", authority, PRINCIPALS)


# ---------------------------------------------------------------------------
# 1. D3 复现：真实 token 带外模块 scope 不再被整条拒
# ---------------------------------------------------------------------------


class ForeignScopesDoNotRejectTheTokenTest(_Base):
    def test_a_real_iam_token_with_foreign_scopes_is_accepted(self) -> None:
        issued = self.iam.issue(
            "alice", scopes=("registry:read",) + FOREIGN_SCOPES
        )
        requester = self.resolve(issued.token)
        self.assertEqual(requester.principal, ALICE)
        self.assertEqual(requester.uid, 4001)

    def test_only_the_registry_scopes_reach_the_requester(self) -> None:
        issued = self.iam.issue(
            "alice",
            scopes=("registry:read", "registry:endpoint:invoke") + FOREIGN_SCOPES,
        )
        requester = self.resolve(issued.token)
        self.assertEqual(
            requester.scopes,
            frozenset({"registry:read", "registry:endpoint:invoke"}),
        )
        for foreign in FOREIGN_SCOPES:
            with self.subTest(scope=foreign):
                self.assertNotIn(foreign, requester.scopes)

    def test_a_foreign_scope_never_grants_registry_authority(self) -> None:
        # 一个长得像更高权限的**外模块** scope：不是 registry 命名空间，丢弃。
        issued = self.iam.issue(
            "alice", scopes=("registry:read", "portal:admin:manage", "atlas:admin:manage")
        )
        requester = self.resolve(issued.token)
        self.assertFalse(requester.is_admin_manage)
        self.assertFalse(requester.is_admin_read)
        self.assertEqual(requester.scopes, frozenset({"registry:read"}))

    def test_a_namespace_that_merely_contains_registry_is_foreign(self) -> None:
        issued = self.iam.issue("alice", scopes=("registry:read", "registryx:admin:manage"))
        requester = self.resolve(issued.token)
        self.assertFalse(requester.is_admin_manage)
        self.assertEqual(requester.scopes, frozenset({"registry:read"}))

    def test_a_token_with_only_foreign_scopes_is_valid_but_has_no_authority(self) -> None:
        issued = self.iam.issue("alice", scopes=FOREIGN_SCOPES)
        requester = self.resolve(issued.token)
        self.assertEqual(requester.principal, ALICE)
        self.assertEqual(requester.scopes, frozenset())
        self.assertFalse(requester.has_scope("registry:read"))

    def test_two_principals_keep_their_own_registry_scopes(self) -> None:
        a = self.resolve(self.iam.issue("alice", scopes=("registry:read",) + FOREIGN_SCOPES).token)
        b = self.resolve(
            self.iam.issue(
                "bob", scopes=("registry:read", "registry:endpoint:invoke") + FOREIGN_SCOPES
            ).token
        )
        self.assertNotEqual(a.principal, b.principal)
        self.assertEqual(a.scopes, frozenset({"registry:read"}))
        self.assertEqual(
            b.scopes, frozenset({"registry:read", "registry:endpoint:invoke"})
        )


# ---------------------------------------------------------------------------
# 2. 仍然 fail closed：未知 registry scope / 形状非法
# ---------------------------------------------------------------------------


class UnknownRegistryScopesFailClosedTest(_Base):
    def test_an_unknown_registry_scope_rejects_the_whole_token(self) -> None:
        for forged in (
            "registry:admin:superuser",
            "registry:root",
            "registry:admin",
            "registry:read_all",
        ):
            with self.subTest(scope=forged):
                with self.assertRaises(Unauthenticated):
                    self.resolve_answer(_answer(scopes=frozenset({"registry:read", forged})))

    def test_malformed_scopes_reject_the_whole_token(self) -> None:
        malformed = (
            "Registry:read",          # 大写
            "registry::read",         # 空段
            "registry:",              # 空段
            ":read",                  # 空 namespace
            "registry read",          # 空白
            "registry:\tread",        # 控制字符
            "r",                      # 没有 namespace 分隔
            "a:b:c:d:e:f",            # 段数越界（>5）
            "",
        )
        for scope in malformed:
            with self.subTest(scope=scope):
                with self.assertRaises(Unauthenticated):
                    self.resolve_answer(_answer(scopes=frozenset({scope})))

    def test_a_non_string_scope_rejects_the_whole_token(self) -> None:
        with self.assertRaises(Unauthenticated):
            self.resolve_answer(_answer(scopes=frozenset({"registry:read", 123})))

    def test_the_shape_rule_matches_the_issuer(self) -> None:
        """本层不 import ``backend.auth``，但形状规则必须与签发方一致。"""
        from backend.auth.tokens import _SCOPE_RE
        from backend.registry.identity import _valid_scope_shape

        corpus = (
            "registry:read",
            "registry:endpoint:invoke",
            "atlas:calls:self:read",
            "observability:self:read",
            "portal:modules:read",
            "a:b",
            "a:b:c:d:e",
            "Registry:read",
            "registry::read",
            "registry:",
            ":read",
            "registry read",
            "r",
            "a:b:c:d:e:f",
            "",
        )
        for scope in corpus:
            with self.subTest(scope=scope):
                self.assertEqual(
                    bool(_SCOPE_RE.match(scope)),
                    _valid_scope_shape(scope),
                    f"shape rule drifted for {scope!r}",
                )


# ---------------------------------------------------------------------------
# 3. 失效语义不变，且现在与「有无 registry scope」分开
# ---------------------------------------------------------------------------


class InvalidationStillFailsClosedTest(_Base):
    def test_expiry_revocation_and_suspension_still_401(self) -> None:
        expired = self.iam.issue("alice", scopes=("registry:read",) + FOREIGN_SCOPES, ttl_seconds=60)
        revoked = self.iam.issue("alice", scopes=("registry:read",) + FOREIGN_SCOPES)
        suspended = self.iam.issue("alice", scopes=("registry:read",) + FOREIGN_SCOPES)
        self.assertEqual(self.resolve(expired.token).principal, ALICE)

        self.clock.now += 61
        self.assertTrue(self.iam.service.revoke(revoked.token))
        self.iam.state.suspend(ALICE)

        for token in (expired.token, revoked.token, suspended.token):
            with self.subTest(token=token[-6:]):
                with self.assertRaises(Unauthenticated):
                    self.resolve(token)

    def test_an_unknown_token_is_still_401(self) -> None:
        with self.assertRaises(Unauthenticated):
            self.resolve("llmlab_scoped_not_a_real_token")


# ---------------------------------------------------------------------------
# 4. 端到端：403（无 registry scope）vs 200（有），以及 #122 的路由门
# ---------------------------------------------------------------------------


class HttpAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)
        # 端点必须在替换 token 权威**之前**建好：`make_ready_endpoint` 走 HTTP，
        # 用的是 Harness 自带的合成 TokenDirectory。
        self.endpoint = self.h.make_ready_endpoint("alice", name="d3-svc")

        self._dir = pathlib.Path(tempfile.mkdtemp(prefix="d3-http-"))
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.clock = _Clock()
        # IAM 必须按 harness 的 PrincipalDirectory 发 token：同一个 uid 两套 id
        # 会让 `resolve_requester` 的对账把每条 token 都拒掉。
        accounts = {
            name: (self.h.principals.lookup(name).principal, self.h.principals.lookup(name).uid)
            for name in ("alice", "bob", "dave")
        }
        self.iam = _Iam(self._dir, self.clock, accounts=accounts)
        self.authority = TrustedTokenIntrospector(
            self.iam.service, principals=self.h.principals, clock=self.clock
        )
        self.h.service.tokens = self.authority
        self.h.app.tokens = self.authority

    def _get(self, token: str, path: str = "/api/v2/registry/engines"):
        return self.h.request("GET", path, token=token)

    def test_foreign_only_token_gets_403_not_401(self) -> None:
        issued = self.iam.issue("alice", scopes=FOREIGN_SCOPES)
        status, _headers, payload = self._get(issued.token)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_registry_read_plus_foreign_gets_200(self) -> None:
        issued = self.iam.issue("alice", scopes=("registry:read",) + FOREIGN_SCOPES)
        status, _headers, payload = self._get(issued.token)
        self.assertEqual(status, 200, payload)

    def test_unknown_registry_scope_gets_401(self) -> None:
        issued = self.iam.issue("alice", scopes=("registry:read", "registry:root"))
        status, _headers, payload = self._get(issued.token)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["code"], "registry.unauthenticated")


class RouteGateStillAppliesTest(HttpAuthorityTest):
    """#122 的门禁不因 D3 修复而放宽。"""

    def test_pending_route_is_409_with_zero_gateway_calls(self) -> None:
        endpoint = self.endpoint
        issued = self.iam.issue(
            "alice",
            scopes=(
                "registry:read",
                "registry:endpoint:invoke",
                "registry:endpoint:share",
            )
            + FOREIGN_SCOPES,
        )
        self.h.gateway.calls.clear()
        status, _headers, payload = self.h.request(
            "POST",
            f"/api/v2/registry/endpoints/{endpoint}/invoke",
            token=issued.token,
            body={"messages": []},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_verified")
        self.assertEqual(self.h.gateway.calls, [])

    def test_a_confirmed_route_is_forwarded_exactly_once(self) -> None:
        endpoint = self.endpoint
        # 权威已经换成 IAM，`confirm_route` 的 HTTP repoint 用不了合成 token；
        # `acknowledge_route` 直接写回执行且不改变 deployment，正合适。
        self.h.acknowledge_route(endpoint)
        issued = self.iam.issue(
            "alice",
            scopes=(
                "registry:read",
                "registry:endpoint:invoke",
                "registry:endpoint:share",
            )
            + FOREIGN_SCOPES,
        )
        status, _headers, payload = self.h.request(
            "POST",
            f"/api/v2/registry/endpoints/{endpoint}/invoke",
            token=issued.token,
            body={"messages": []},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.h.gateway.calls), 1)


if __name__ == "__main__":
    unittest.main()
