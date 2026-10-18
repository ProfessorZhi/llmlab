"""Phase B 合成候选的跨 Lane 端到端验证（Issue #59）。

**这个文件只测「合成」这一件事。** 七条 Lane 各自在基线 `7fcc7d4f` 上跑绿的单元
测试，不能证明它们拼起来还能用：接缝在两边各自正确、合在一起错位，正是集成工作
唯一要回答的问题，也是任何单条 Lane 都测不到的部分。所以这里不重复各 Lane 已经
覆盖的单元断言，只测**跨 Lane 的那几条缝**：

1. **IAM #52 的 token 服务 ↔ BFF #57 的 token 通道。** Phase A 写下的是契约（一个
   提供 ``introspect(token)`` 的对象），IAM 后来实现的是 ``ScopedTokenService``。
   两者是否真的对得上，只有把它们接起来才知道。
2. **BFF #57 的路由/ACL + Registry #58 的登记与共享**，走完整的
   A 注册 → 共享给 B → B invoke → A 撤权 → B 立即被拒。
3. **合成后的 fail-closed 三件套**（Host / 来源 / CSRF）在同一个进程里同时成立。

**边界**：全部 credentials 是进程内 synthetic fixture，不碰生产 PAM/Gateway，
不监听任何生产端口，CPU-only。凡涉及真实 Gateway 的，一律标注为 mock 且
``synthetic``；这里**不**声称生产可用。
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

from backend.auth import tokens as auth_tokens
from backend.bff import identity as bff_identity
from backend.bff import origin as bff_origin
from backend.registry import errors as registry_errors
from backend.registry import models as registry_models
from backend.registry.router import ROUTER_SYNC_APPLIED
from tests.bff import support as S

#: 与 ``tests.bff.support`` 的合成账号表一致：alice=4001、bob=4003。
VINCE_UID = S.MEMBER_UID
KAIDONG_UID = S.REAL_ACCOUNT_UID


class TokenChannelAgainstTheRealIamService(unittest.TestCase):
    """把 Phase A 的契约接到 IAM #52 真正实现的那个服务上。

    Phase A（PR #57）引入这条通道时，IAM 的服务还不在这条分支上，所以当时只能
    挂一个「按 ``TokenIntrospection`` 字段形状写的 fixture」。现在两条分支都在，
    契约可以对着**真实现**验。
    """

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-candidate-tokens-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        alice = self.h.principal_of("alice")
        self.directory = auth_tokens.MappingPrincipalState(
            {alice: auth_tokens.STATE_APPROVED}
        )
        self.service = auth_tokens.ScopedTokenService(
            auth_tokens.TokenStore(self.tmp / "scoped-tokens.sqlite3"),
            principal_state=self.directory,
        )
        self.service.initialize()
        self.alice = alice

    # -- helpers ----------------------------------------------------------

    def issue(self, **kwargs):
        params = {
            "principal": self.alice,
            "uid": VINCE_UID,
            "linux_user": "alice",
            "scopes": ("registry:read",),
            "roles": ("member",),
            "label": "candidate-e2e",
        }
        params.update(kwargs)
        return self.service.issue(**params)

    def enable_channel(self, *, accounts=None):
        """把 BFF 的 token 通道指向 IAM 的真服务。"""
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=self.service,
            accounts=self.h.accounts if accounts is None else accounts,
            enabled=True,
        )

    def call(self, token, path="/api/v2/ops", **headers):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        return client.run(
            "GET", path, headers={"Authorization": f"Bearer {token}", **headers}
        )

    # -- the seam ---------------------------------------------------------

    def test_the_contract_matches_the_real_service_without_an_adapter(self):
        """没有任何中间适配层：``introspect`` 的返回对象直接就被本层读懂了。

        这条一旦失败，说明 Phase A 写下的字段形状与 IAM 的实现漂移了——那正是
        集成候选要发现的东西。
        """
        self.enable_channel()
        issued = self.issue()
        response = self.call(issued.token)
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(S.ops_of(response)["scope"]["subject"], self.alice)

    def test_the_token_carries_only_the_scopes_it_was_issued_with(self):
        self.enable_channel()
        issued = self.issue(scopes=("registry:read", "observability:self:read"))
        principal = self.h.app.token_source.resolve(_bearer(issued.token))
        self.assertEqual(principal.principal, self.alice)
        self.assertEqual(principal.uid, VINCE_UID)
        self.assertEqual(principal.linux_user, "alice")
        # token 不是会话：没有会话行、没有提权。
        self.assertIsNone(principal.user)
        self.assertFalse(principal.elevated)
        # Registry / 模块那道门读的是 requester.scopes：签发时给的都在，没给的都不在。
        self.assertEqual(
            principal.requester.scopes,
            frozenset({"registry:read", "observability:self:read"}),
        )
        self.assertEqual(principal.requester.token_label, "candidate-e2e")

    def test_a_cli_token_gets_no_portal_capabilities(self):
        """**集成发现（不是缺陷，但必须写下来）。**

        ``portal:*`` 是**前端能力词表**，模块 scope（如
        ``observability:self:read``）不在其中——两个词表本来就各管各的面
        （见 ``backend/bff/scopes.py``）。于是一条带着模块 scope 的 CLI token 走到
        ``/api/v2/ops`` 时，``scope.scopes`` 是空的，前端的 capability 门会把界面
        上的控件全部禁用。

        对一条 **CLI** token 这是**正确**的：它不是浏览器会话，``/api/v2/ops`` 也
        不是它的入口。但它意味着「给 token 加了模块 scope，Web 上就能看到那一页」
        这个直觉是错的。本断言把这个事实钉住，好让将来真要让 token 在 Web 上有
        能力时，改的是**词表映射**这一处，而不是有人悄悄往 ``portal_scopes`` 里
        塞一条模块 scope 来「让它能过」。
        """
        self.enable_channel()
        issued = self.issue(scopes=("registry:read", "observability:self:read"))
        principal = self.h.app.token_source.resolve(_bearer(issued.token))
        self.assertEqual(principal.portal_scopes, ())
        # 但模块那道门是**开着**的：观测 adapter 读的是 requester.scopes。
        self.assertIn("observability:self:read", principal.requester.scopes)
        ops = S.ops_of(self.call(issued.token))
        self.assertEqual(ops["scope"]["scopes"], [])

    def test_a_suspended_principal_is_denied_on_the_next_call(self):
        """issuer 的**当前**账号状态说了算，不是签发那一刻的存档。"""
        self.enable_channel()
        issued = self.issue()
        self.assertEqual(self.call(issued.token).status, 200)

        self.directory.suspend(self.alice)
        denied = self.call(issued.token)
        self.assertEqual(denied.status, 401)
        self.assertEqual(denied.error_code(), "no_authenticated_session")

    def test_rotation_ends_the_old_token_and_the_new_one_works(self):
        """轮换：旧 token 下一次调用即失效，新 token 接上同一主体。"""
        self.enable_channel()
        issued = self.issue()
        self.assertEqual(self.call(issued.token).status, 200)

        rotated = self.service.rotate_for_principal(
            self.alice, issued.token_id, allowed_scopes=("registry:read",)
        )
        self.assertNotEqual(rotated.token, issued.token)

        old = self.call(issued.token)
        self.assertEqual(old.status, 401, "a rotated-away token must stop working")
        self.assertEqual(old.error_code(), "no_authenticated_session")

        new = self.call(rotated.token)
        self.assertEqual(new.status, 200, new.json())
        self.assertEqual(S.ops_of(new)["scope"]["subject"], self.alice)

    def test_revocation_is_immediate_on_the_next_call(self):
        self.enable_channel()
        issued = self.issue()
        self.assertEqual(self.call(issued.token).status, 200)
        self.assertTrue(self.service.revoke_for_principal(self.alice, issued.token_id))
        self.assertEqual(self.call(issued.token).status, 401)

    def test_a_forged_identity_header_is_refused_even_with_a_valid_token(self):
        """自报身份在 token 通道上同样是 400，不是「以绑定为准、忽略头」。"""
        self.enable_channel()
        issued = self.issue()
        response = self.call(issued.token, **{"X-UID": str(KAIDONG_UID)})
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "identity_assertion_rejected")

    def test_the_account_directory_check_still_applies_to_a_real_token(self):
        """IAM 的 introspection 与合成账号目录必须对同一个 uid 说同一个人。"""
        self.enable_channel(accounts=self.h.accounts)
        # 一个 uid 在 IAM 的目录里 approved、但在账号目录里不存在的主体：
        # IAM 照自己的权威签发，BFF 这一层再按**同一条**账号目录规则拒绝——
        # 两条通道对「这个 uid 是谁」不能给出两个答案。
        ghost = "linux:dell-test-01:4242"
        self.directory.approve(ghost)
        issued = self.issue(principal=ghost, uid=4242, linux_user="ghost")
        response = self.call(issued.token)
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_two_principals_are_two_subjects_through_the_real_service(self):
        self.enable_channel()
        bob = self.h.principal_of("bob")
        self.directory.approve(bob)
        mine = self.issue()
        theirs = self.issue(
            principal=bob, uid=KAIDONG_UID, linux_user="bob", label="other"
        )
        a = S.ops_of(self.call(mine.token))["scope"]["subject"]
        b = S.ops_of(self.call(theirs.token))["scope"]["subject"]
        self.assertNotEqual(a, b)


class CrossUidRevocationChain(unittest.TestCase):
    """A 注册 → 共享给 B → B invoke → A 撤权 → B 立即被拒。

    覆盖的是「合成之后」的这条链：BFF 的路由与 ACL（#57）接在 Registry 的登记与
    共享（#58）上，转发层是 **mock** Gateway —— 因此每一步都标 ``synthetic``，
    **不是**生产验证。
    """

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.alice = self.h.client("alice")
        self.bob = self.h.client("bob")
        self.ids = S.register_baseline(self.h, "alice")
        # Registry 的调用门（#122 P0）要求路由**已由 Gateway 回读确认**：新建
        # Endpoint 一律停在 ``pending_router_sync``，直接 invoke 会被 409 拒绝。
        # ``acknowledge_route`` 写下的正是「一次成功回读本来就会写下的同一行」
        # （revision 相等 + evidence=gateway_readback + 可导出快照），没有绕过
        # 任何回读逻辑；在没有该门的树上这段状态不会被读取，夹具两边都成立。
        self.h.acknowledge_route(self.ids["endpoint_id"])

    def shared_endpoint_ids(self):
        ops = S.ops_of(S.fetch_ops(self.bob, "shared"))
        return {row["endpoint_id"] for row in ops["registry"]["shared"]}

    def invoke_as_bob(self):
        principal = self.h.app.resolve_session(self.h.session_id("bob"))
        return self.h.service.invoke(
            principal.requester,
            self.ids["endpoint_id"],
            {"model": "model-alice", "messages": [{"role": "user", "content": "hi"}]},
        )

    def test_the_whole_chain_from_private_to_revoked(self):
        endpoint_id = self.ids["endpoint_id"]

        # 1. 私有：B 看不见，也调不动 —— 拒绝发生在任何 Gateway 调用之前。
        self.assertNotIn(endpoint_id, self.shared_endpoint_ids())
        with self.assertRaises(registry_errors.NotFound):
            self.invoke_as_bob()
        self.assertEqual(self.h.gateway.calls, [])

        # 2. A 共享给 B。
        response = S.share(
            self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"]
        )
        self.assertEqual(response.status, 200, response.body)

        # 2b. 调用之前先把「路由已生效」的四件事逐条钉住：状态、证据来源、
        #     Router **此刻**回读的代数、以及这条 Endpoint 真的在可导出快照里。
        #     只写其中一条会让门禁被「看起来已 applied」的部分状态蒙混过去。
        endpoint = self.h.service.store.endpoint_by_id(endpoint_id)
        self.assertEqual(endpoint.router_sync_state(), ROUTER_SYNC_APPLIED)
        self.assertEqual(
            endpoint.router_ack_evidence,
            registry_models.ROUTER_EVIDENCE_GATEWAY_READBACK,
        )
        self.assertEqual(self.h.service.router.applied_revision(), endpoint.revision)
        self.assertIn(
            endpoint_id,
            {
                row["endpoint_id"]
                for row in self.h.service._routing_snapshot()["endpoints"]
            },
        )
        self.assertEqual(self.h.gateway.calls, [])

        # 3. B 现在看得见，并且能真的调一次（mock 转发层，标 synthetic）。
        self.assertIn(endpoint_id, self.shared_endpoint_ids())
        result = self.invoke_as_bob()
        self.assertTrue(result["data"]["result"]["forwarded"])
        self.assertTrue(result["data"]["result"]["synthetic"])
        self.assertEqual(result["data"]["gateway"]["adapter"], "mock")
        self.assertEqual(len(self.h.gateway.calls), 1)

        # 4. A 撤权 → B 立即被拒，且授权行是真的没了；被拒的调用不碰 Gateway。
        revoked = S.share(self.alice, self.ids["deployment_id"], mode="private")
        self.assertEqual(revoked.status, 200, revoked.body)
        self.assertNotIn(endpoint_id, self.shared_endpoint_ids())
        self.assertEqual(
            self.h.service.store.grantees(endpoint_id), ()
        )
        with self.assertRaises(registry_errors.NotFound):
            self.invoke_as_bob()
        self.assertEqual(len(self.h.gateway.calls), 1)

    def test_bobs_own_view_never_shows_alices_deployment_as_owned(self):
        S.share(self.alice, self.ids["deployment_id"], mode="all")
        mine = S.ops_of(S.fetch_ops(self.bob))["registry"]["owned"]
        self.assertEqual(mine, [])
        self.assertEqual(
            S.ops_of(S.fetch_ops(self.bob))["registry"]["all"], []
        )


class ComposedFailClosed(unittest.TestCase):
    """三件 fail-closed 的守卫在**同一个进程**里同时成立。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_anonymous_is_401(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        self.assertEqual(client.run("GET", "/api/v2/ops").status, 401)

    def test_a_stale_host_is_421_before_anything_else(self):
        client = self.h.client("alice")
        token = S.csrf(client)
        client.host = "stale-portal.internal"
        response = client.run(
            "GET", "/api/v2/ops", headers={"X-CSRF-Token": token}
        )
        self.assertEqual(response.status, 421)
        self.assertEqual(response.error_code(), bff_origin.REASON_FOREIGN_HOST)

    def test_a_client_declared_source_is_400(self):
        response = self.h.client("alice").run("GET", "/api/v2/ops?source=live")
        self.assertEqual(response.status, 400)
        self.assertEqual(
            response.error_code(), bff_identity.REASON_SOURCE_ASSERTED
        )

    def test_a_write_without_csrf_is_403_and_changes_nothing(self):
        ids = S.register_baseline(self.h, "alice")
        response = self.h.client("alice").run(
            "PUT",
            f"/api/v2/ops/deployments/{ids['deployment_id']}/share",
            payload={"mode": "all", "principals": []},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(
            self.h.service.store.endpoint_by_id(ids["endpoint_id"]).visibility,
            "private",
        )

    def test_the_bff_refuses_to_bind_the_upstream_gateway_port(self):
        """8080 是上游 Gateway 的端口；合成候选不许把它接到浏览器那一侧。"""
        from backend.bff import devserver

        self.assertIn(8080, devserver.RESERVED_BIND_PORTS)
        with self.assertRaises(SystemExit):
            devserver.validate_bind_port(8080)


def _bearer(token: str):
    """一个最小的请求替身，只带 ``Authorization``。"""

    class _Request:
        def __init__(self):
            self.cookies: dict[str, str] = {}
            self.headers = {"authorization": f"Bearer {token}"}

    return _Request()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
