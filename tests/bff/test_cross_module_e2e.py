"""四个角色，一条纵切，三个模块。

    carol      uid 4000  成员；Linux 侧被可信授权标为 platform_admin，
                       但**普通会话仍是成员**，只有在自己的会话里进入管理员
                       模式（共享管理口令，≤ 15 分钟）才看到全局
    alice    uid 4001  成员 —— 登记者 A
    dave  uid 4002  成员 —— 无关的第三人
    bob  uid 4003  成员 —— 被共享者 B

整条路径都由 HTTP 驱动（同一个 WSGI callable），会话来自真正的
``POST /auth/linux/login``：

    A 登记元数据 → Endpoint 出生即 private
      → B 看不见（私有视图 0 行；直接读是 404 而不是 403）
        → A 按 **uid** 共享给 B
          → B 在共享视图看见它，并且**调用成功**（MockGatewayAdapter）
            → A 撤销 → B 立刻失去可见性与调用权
              → 进入管理员模式的会话在三个模块里都看到全局 metadata，
                且看不到任何原文；另一个**未提权**的 carol 会话什么都看不到

PAM 用的仍是测试替身：它证明的是周围机制，不证明 PAM。真实派发、真实 Gateway、
真实观测来源在本部署都不存在，因此对应的数字一律是 UNKNOWN 或 SYNTHETIC。

管理员模式用的也是真实实现（真实 scrypt 口令文件 + 真实提权存储 + 真实路由），
口令本身是合成值：这个仓库里没有、也不会有一个部署用的共享口令。
"""

from __future__ import annotations

import json
import tempfile
import unittest

from backend.registry import errors as registry_errors
from tests.bff import support as S

ROLES = ("carol", "alice", "dave", "bob")


class CrossModuleEndToEnd(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.alice = self.h.client("alice")
        self.bob = self.h.client("bob")
        self.third = self.h.client("dave")
        self.admin = self.h.admin_client("carol")
        # 同一个人、另一条会话，没有进入管理员模式。
        self.plain_admin_account = self.h.client("carol")
        self.ids = S.register_baseline(self.h, "alice")

    # -- helpers ----------------------------------------------------------

    def ops(self, client, visibility=None):
        return S.ops_of(S.fetch_ops(client, visibility))

    def _rows_for(self, name: str) -> int:
        """这个主体自己的 key 在合成语料里有多少行。"""
        corpus = S.bff_stacks.demo_gateway_corpus(
            self.h.key_directory, now=self.h.now, endpoints=["ep-synthetic"], per_key=4
        )
        key = f"key-{name}"
        return sum(
            1 for line in corpus.strip().splitlines() if json.loads(line)["key_id"] == key
        )

    # -- 1. private by default --------------------------------------------

    def test_the_endpoint_is_private_and_b_cannot_see_it(self):
        self.assertEqual(self.ops(self.alice)["registry"]["owned"][0]["share"]["mode"], "private")
        self.assertEqual(self.ops(self.bob, "shared")["registry"]["shared"], [])
        self.assertEqual(self.ops(self.third, "shared")["registry"]["shared"], [])
        with self.assertRaises(registry_errors.NotFound):
            self.h.service.policy(
                self.h.requester_for("bob")
            ).require_endpoint_read(self.ids["endpoint_id"])

    def test_b_cannot_see_as_usage_or_as_an_evaluation_run(self):
        S.share(self.alice, self.ids["deployment_id"], mode="all")
        self.h.submit_eval("alice", self.ids["deployment_id"])

        # B's usage counts B's own key's rows and nothing else. The fixture
        # gives each key a different number of rows precisely so this is
        # observable rather than accidentally equal.
        bob_usage = self.h.observability.usage_dto(
            identity=self.h.trusted_principal("bob")
        )
        alice_usage = self.h.observability.usage_dto(
            identity=self.h.trusted_principal("alice")
        )
        self.assertEqual(bob_usage["scope"]["uid"], self.h.principal_of("bob"))
        self.assertNotEqual(
            bob_usage["counters"]["calls"],
            alice_usage["counters"]["calls"],
            "A and B must not be looking at the same rows",
        )
        self.assertEqual(
            bob_usage["counters"]["calls"], self._rows_for("bob")
        )
        self.assertEqual(
            alice_usage["counters"]["calls"], self._rows_for("alice")
        )
        # …and A's evaluation job is not in B's queue at all.
        self.assertEqual(self.ops(self.bob)["evaluation"]["runs"], [])
        self.assertEqual(self.ops(self.third)["evaluation"]["runs"], [])
        self.assertEqual(len(self.ops(self.alice)["evaluation"]["runs"]), 1)

    # -- 2. the share ------------------------------------------------------

    def test_sharing_to_a_uid_gives_b_visibility_and_invocation(self):
        response = S.share(
            self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"]
        )
        self.assertEqual(response.status, 200, response.body)

        (shared,) = self.ops(self.bob, "shared")["registry"]["shared"]
        self.assertEqual(shared["endpoint_id"], self.ids["endpoint_id"])
        self.assertFalse(shared["owner_is_self"])
        self.assertEqual(shared["share"]["mode"], "selected")
        self.assertTrue(shared["share"]["revocable"])

        # The third member is unaffected: a `selected` share names one person.
        self.assertEqual(self.ops(self.third, "shared")["registry"]["shared"], [])

        # And B can actually call it, through the mock adapter.
        #
        # 先补一次 **Gateway 回读确认**：Registry 的 P0 调用门（PR #122）要求
        # ``router_sync_applied`` —— 新建的 Endpoint 一律停在 ``pending_router_sync``，
        # 在它上面调用会被 409 拒绝而不是被转发。这段夹具写下的正是那次回执成功时
        # 本来就会写下的同一行；它不改变 deployment 目标，也没有绕过回读逻辑。
        self.h.acknowledge_route(self.ids["endpoint_id"])
        principal = self.h.app.resolve_session(self.h.session_id("bob"))
        result = self.h.service.invoke(
            principal.requester,
            self.ids["endpoint_id"],
            {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertTrue(result["data"]["result"]["forwarded"])
        self.assertTrue(result["data"]["result"]["synthetic"])
        self.assertEqual(result["data"]["gateway"]["adapter"], "mock")
        self.assertEqual(result["data"]["gateway"]["authorization"], "server_side_enforced")

    def test_revocation_is_immediate_and_total(self):
        S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        self.assertEqual(len(self.ops(self.bob, "shared")["registry"]["shared"]), 1)

        S.share(self.alice, self.ids["deployment_id"], mode="private")
        self.assertEqual(self.ops(self.bob, "shared")["registry"]["shared"], [])
        self.assertEqual(self.h.service.store.grantees(self.ids["endpoint_id"]), ())
        with self.assertRaises(registry_errors.NotFound):
            self.h.service.invoke(
                self.h.app.resolve_session(self.h.session_id("bob")).requester,
                self.ids["endpoint_id"],
                {},
            )

    # -- 3. the admin ------------------------------------------------------

    def test_the_admin_sees_global_metadata_across_all_three_modules(self):
        S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        self.h.submit_eval("alice", self.ids["deployment_id"])
        admin_ops = self.ops(self.admin)

        # registry: the deployment-wide list
        self.assertTrue(admin_ops["admin"]["available"])
        self.assertEqual(len(admin_ops["registry"]["all"]), 1)
        # observability: global series exist for this session
        self.assertIn("global", {s["scope"] for s in admin_ops["trends"]["series"]})
        # evaluation: another owner's run, labelled global
        self.assertEqual([run["scope"] for run in admin_ops["evaluation"]["runs"]], ["global"])
        # admin: the trusted principal list
        self.assertEqual(
            {row["uid"] for row in admin_ops["admin"]["principals"]},
            {self.h.principal_of(name) for name in ROLES},
        )

    def test_the_admin_view_is_metadata_only(self):
        """Global vision is not global *content*."""
        S.share(self.alice, self.ids["deployment_id"], mode="all")
        self.h.submit_eval("alice", self.ids["deployment_id"])
        blob = json.dumps(self.ops(self.admin), ensure_ascii=False)
        for field in ("prompt", "completion", "messages", "payload", "plaintext", "secret"):
            self.assertNotIn(f'"{field}"', blob, field)

    def test_the_admin_flag_is_not_a_way_in(self):
        """There is no `X-Admin` header on the request path; only elevation opens it.

        Asserted on the *plain* session, which is the one that would have
        something to gain: an already-elevated session refusing the header only
        shows the header is inert, not that it cannot promote.
        """
        forged = self.plain_admin_account.run(
            "GET", "/api/v2/ops", headers={"X-Admin": "true"}
        )
        self.assertEqual(forged.status, 400)

    def test_the_same_account_without_elevation_sees_no_global_metadata(self):
        """The headline rule, on the same person and the same build.

        `carol` is the granted Linux administrator and holds the same session
        machinery as everyone else. Without entering admin mode, every module
        answers exactly as it does for any other member.
        """
        S.share(self.alice, self.ids["deployment_id"], mode="all")
        self.h.submit_eval("alice", self.ids["deployment_id"])
        plain = self.ops(self.plain_admin_account)
        self.assertFalse(plain["admin"]["available"])
        self.assertEqual(plain["registry"]["all"], [])
        self.assertEqual(plain["admin"]["principals"], [])
        self.assertEqual({s["scope"] for s in plain["trends"]["series"]}, {"mine"})
        for run in plain["evaluation"]["runs"]:
            self.assertEqual(run["scope"], "mine")

    def test_a_member_is_not_an_admin_in_any_module(self):
        member = self.ops(self.alice)
        self.assertFalse(member["admin"]["available"])
        self.assertEqual(member["registry"]["all"], [])
        self.assertEqual({s["scope"] for s in member["trends"]["series"]}, {"mine"})
        for run in member["evaluation"]["runs"]:
            self.assertEqual(run["scope"], "mine")

    # -- 4. the shared invariants -----------------------------------------

    def test_no_session_is_401_on_every_route(self):
        from backend.bff.app import ENGINES_PATH, OPS_PATH, USAGE_PATH

        anon = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        for path in (OPS_PATH, USAGE_PATH, ENGINES_PATH):
            response = anon.run("GET", path)
            self.assertEqual(response.status, 401, path)
            self.assertEqual(response.error_code(), "no_authenticated_session", path)

    def test_forged_identity_headers_are_refused_on_every_route(self):
        from backend.bff.app import ENGINES_PATH, OPS_PATH, USAGE_PATH

        for path in (OPS_PATH, USAGE_PATH, ENGINES_PATH):
            for header in ("X-UID", "X-Admin", "X-Role", "X-Owner", "X-Principal"):
                response = self.alice.run("GET", path, headers={header: "4000"})
                self.assertEqual(response.status, 400, f"{path} {header}")

    def test_a_forged_cookie_gets_nothing(self):
        forged = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        forged.cookies[self.h.app.cfg.cookie_name] = "not-a-real-session"
        self.assertEqual(S.fetch_ops(forged).status, 401)

    def test_no_payload_anywhere_names_the_upstream_port(self):
        from backend.bff.app import ENGINES_PATH, USAGE_PATH

        S.share(self.alice, self.ids["deployment_id"], mode="all")
        for name in ROLES:
            client = self.h.client(name)
            blobs = [json.dumps(self.ops(client, v), ensure_ascii=False) for v in (None, "shared")]
            for path in (USAGE_PATH, ENGINES_PATH):
                response = client.run("GET", path)
                self.assertEqual(response.status, 200, path)
                blobs.append(response.body.decode("utf-8"))
            for blob in blobs:
                self.assertNotIn(":8080", blob)
                self.assertNotIn("://", blob)

    def test_the_whole_matrix_produces_a_contract_valid_payload(self):
        """Every role, both visibilities: the invariants the front end checks."""
        S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        self.h.submit_eval("alice", self.ids["deployment_id"])
        for name in ROLES:
            for visibility in (None, "shared"):
                ops = self.ops(self.h.client(name), visibility)
                self.assertEqual(ops["schema_version"], "portal-ops/1")
                self.assertTrue(ops["notes"])
                self.assertTrue(ops["synthetic"])
                # A valued metric would need a whitelisted source; none exists.
                for metric in ops["metrics"]["items"]:
                    if metric["value"] is not None:
                        self.fail(f"{name} got a metric value: {metric}")
                # No GPU number, ever.
                for row in ops["registry"]["owned"]:
                    for field in ("gpu_count", "vram_total_mib", "vram_used_mib"):
                        self.assertIsNone(row["resources"][field])
                # Global-only rows need the admin scope.
                if not ops["admin"]["available"]:
                    self.assertEqual(ops["registry"]["all"], [])
                    for run in ops["evaluation"]["runs"]:
                        self.assertNotEqual(run["scope"], "global")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
