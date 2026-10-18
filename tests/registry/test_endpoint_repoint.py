"""Endpoint 重指向（repoint）：稳定 alias、原子切换、ACL 不带过去、诚实报 pending。

这一组测试盯的是「换小模型」这件事的**全部**安全前提，每一条都对应一个具体的
失手方式：

* alias 稳不稳（换了 alias，调用方全部要改）；
* 授权集合会不会被静默带到新模型上（旧模型签发的授权不是给新模型的）；
* 两个并发请求会不会互相覆盖（没有 CAS 就会出现「后写的赢」而没人知道）；
* 跨 owner 会不会被冒领（别人的算力挂到自己的对外路径上）；
* 状态会不会被谎报成「已切换」（本仓库没有任何 Gateway 适配器）。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from backend.registry.identity import (
    Principal,
    PrincipalDirectory,
    TokenBinding,
    TokenDirectory,
    resolve_requester,
)
from backend.registry.registry import RegistryConfig, RegistryService
from backend.registry.store import Store
from tests.registry.support import (
    ADMIN_READ_ONLY_SCOPES,
    HEALTHY_PORT,
    NON_ADMIN_SCOPES,
    UNREACHABLE_PORT,
    Harness,
    close_all,
)

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


class StableAliasTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.first = self.h.make_ready_deployment("alice", "big-qwen")
        self.second = self.h.make_ready_deployment("alice", "small-llama")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.first)

    def test_repoint_keeps_the_alias_and_moves_the_deployment(self) -> None:
        before = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="alice"
        )[2]["data"]
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, self.second)
        self.assertEqual(status, 200, payload)
        after = payload["data"]
        # alias 不变 —— 这正是「换模型而 API 路径不动」。
        self.assertEqual(after["route_path"], before["route_path"])
        self.assertEqual(after["name"], before["name"])
        # deployment 变了。
        self.assertEqual(before["deployment_id"], self.first)
        self.assertEqual(after["deployment_id"], self.second)

    def test_repoint_bumps_the_revision_by_exactly_one(self) -> None:
        before = self.h.endpoint_revision("alice", self.endpoint_id)
        payload = self.h.repoint("alice", self.endpoint_id, self.second)[2]
        self.assertEqual(payload["data"]["revision"], before + 1)
        self.assertEqual(payload["meta"]["revision_from"], before)
        self.assertEqual(payload["meta"]["revision_to"], before + 1)

    def test_invoke_after_repoint_reaches_the_new_model(self) -> None:
        self.h.gateway.calls.clear()
        self.h.repoint("alice", self.endpoint_id, self.second)
        # repoint 只改 Registry；路由要等 Gateway 回读确认后才允许转发。
        # 这里确认的是**当前** revision，不换掉刚指向的 self.second。
        self.h.acknowledge_route(self.endpoint_id)
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="alice",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.h.gateway.calls), 1)
        # 网关看到的是**新** deployment，且模型名跟着换了。
        self.assertEqual(self.h.gateway.calls[0]["deployment_id"], self.second)
        self.assertEqual(payload["data"]["model"], "small-llama-model")


class HonestRouterSyncTest(unittest.TestCase):
    """没有 Gateway 适配器，就不许说「已切换」。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.first = self.h.make_ready_deployment("alice", "a")
        self.second = self.h.make_ready_deployment("alice", "b")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.first)

    def test_a_fresh_endpoint_is_pending_not_applied(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="alice"
        )
        self.assertTrue(payload["data"]["pending_router_sync"])
        self.assertEqual(payload["data"]["router_sync"]["state"], "pending_router_sync")
        self.assertEqual(payload["data"]["router_sync"]["applied_generation"], 0)

    def test_repoint_reports_pending_router_sync_not_success(self) -> None:
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, self.second)
        self.assertTrue(payload["meta"]["pending_router_sync"])
        self.assertFalse(payload["meta"]["router_applied"])
        self.assertEqual(payload["meta"]["router_sync"], "pending_router_sync")
        self.assertEqual(payload["data"]["router_sync"]["state"], "pending_router_sync")
        self.assertTrue(
            any("pending_router_sync" in note for note in payload["notes"]), payload["notes"]
        )

    def test_an_operator_ack_turns_pending_into_attested_not_applied(self) -> None:
        """回执只有一个整数，所以它买到的是 ``router_sync_attested``，不是已切流。

        这条断言从前写的是 ``router_sync_applied``。同一个 token 也是
        ``FileRouterAdapter`` 读回 Gateway 自己发布的 revision **与** snapshot SHA
        之后才敢用的那个 —— 而 ``docs/architecture.md`` §5.2 规则 3 正是把
        「Gateway 发布的 generation/revision 与 snapshot SHA 回执」列为路由应用的
        必要条件。一个整数不该和一份逐位相等的回读共用一个词。
        """
        self.h.repoint("alice", self.endpoint_id, self.second)
        revision = self.h.endpoint_revision("alice", self.endpoint_id)
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/routing-applied",
            principal="carol",
            body={"generation": revision},
        )
        self.assertEqual(status, 200, payload)
        block = payload["data"]["router_sync"]
        self.assertEqual(block["state"], "router_sync_attested")
        self.assertFalse(block["applied"])
        self.assertFalse(payload["meta"]["router_applied"])
        self.assertFalse(payload["data"]["pending_router_sync"])

    def test_a_stale_ack_is_refused(self) -> None:
        # 落后的回执会让系统以为已经生效 —— 那正是「谎报已切换」的成因。
        self.h.repoint("alice", self.endpoint_id, self.second)
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/routing-applied",
            principal="carol",
            body={"generation": 1},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.revision_conflict")

    def test_ack_requires_admin_manage(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        revision = self.h.endpoint_revision("alice", self.endpoint_id)
        status, _h, _p = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/routing-applied",
            principal="alice",
            body={"generation": revision},
        )
        self.assertEqual(status, 403)

    def test_routing_config_names_the_pending_endpoints(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/routing-config", principal="carol"
        )
        pending = payload["data"]["pending_router_sync"]
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["items"][0]["endpoint_id"], self.endpoint_id)


class NoSilentSharingTest(unittest.TestCase):
    """换后端**不能**把为旧模型签发的授权带过去。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.first = self.h.make_ready_deployment("alice", "a")
        self.second = self.h.make_ready_deployment("alice", "b")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.first)
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)

    def test_repoint_by_default_revokes_the_existing_share(self) -> None:
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, self.second)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["revoked"], ["dave"])
        self.assertEqual(payload["meta"]["visibility"], "private")
        # 受托人立刻看不见了。
        self.assertEqual(self.h.endpoints_visible_to("dave"), [])

    def test_the_revoked_grantee_cannot_invoke_the_new_model(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/invoke",
            principal="dave",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 404, payload)
        self.assertEqual(self.h.gateway.calls, [])

    def test_the_audit_says_the_acl_was_not_carried_over(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        _s, _h, payload = self.h.request(
            "GET", f"{PREFIX}/audit", principal="alice"
        )
        repoints = [e for e in payload["data"]["items"] if e["action"] == "repoint"]
        self.assertTrue(repoints)
        detail = repoints[0]["detail"]
        self.assertFalse(detail["acl_carried_over"])
        self.assertEqual(detail["visibility_after"], "private")
        self.assertEqual(detail["grantees_after"], [])

    def test_an_explicit_reauthorization_keeps_sharing_and_is_audited(self) -> None:
        status, _h, payload = self.h.repoint(
            "alice",
            self.endpoint_id,
            self.second,
            reauthorize={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["visibility"], "users")
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)
        # 重新授权是**显式**的，所以它在审计里是一条独立的 grant。
        _s, _h, audit = self.h.request("GET", f"{PREFIX}/audit", principal="alice")
        grants = [
            e
            for e in audit["data"]["items"]
            if e["action"] == "grant"
            and e["detail"].get("reason") == "repoint_reauthorization"
        ]
        self.assertEqual(len(grants), 1)

    def test_reauthorization_requires_its_own_scope(self) -> None:
        # 有 repoint 的能力不等于有「把服务开放给别人」的能力。
        h = Harness()
        first = h.make_ready_deployment("alice", "a")
        second = h.make_ready_deployment("alice", "b")
        endpoint_id = h.make_endpoint_on("alice", "svc", first)
        h.with_scopes("alice", frozenset({"registry:read", "registry:write"}))
        status, _h, payload = h.repoint(
            "alice",
            endpoint_id,
            second,
            reauthorize={"mode": "all_approved_platform_users"},
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_all_approved_reauthorization_is_explicit_too(self) -> None:
        status, _h, payload = self.h.repoint(
            "alice",
            self.endpoint_id,
            self.second,
            reauthorize={"mode": "all_approved_platform_users"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["meta"]["visibility"], "all_approved_platform_users")


class OptimisticConcurrencyTest(unittest.TestCase):
    """CAS：没有它，两个并发 repoint 会「后写的赢」而没人知道。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.first = self.h.make_ready_deployment("alice", "a")
        self.second = self.h.make_ready_deployment("alice", "b")
        self.third = self.h.make_ready_deployment("alice", "c")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.first)

    def test_a_stale_expected_revision_is_refused_and_changes_nothing(self) -> None:
        stale = self.h.endpoint_revision("alice", self.endpoint_id)
        self.h.repoint("alice", self.endpoint_id, self.second)  # 把 revision 推走
        status, _h, payload = self.h.repoint(
            "alice", self.endpoint_id, self.third, expected_revision=stale
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.revision_conflict")
        # 目标**没有**被改动。
        current = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}", principal="alice"
        )[2]["data"]
        self.assertEqual(current["deployment_id"], self.second)

    def test_expected_revision_is_mandatory(self) -> None:
        # 不给默认值：默认值会让竞争悄悄过关，而这道 CAS 的价值全在于强迫先读一次。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/repoint",
            principal="alice",
            body={"target_deployment_id": self.second},
        )
        self.assertEqual(status, 400, payload)
        self.assertIn("expected_revision", payload["error"]["message"])

    def test_a_nonsense_expected_revision_is_refused(self) -> None:
        for bad in (0, -1, "2", 1.5, True, None):
            with self.subTest(bad=bad):
                status, _h, _p = self.h.request(
                    "POST",
                    f"{PREFIX}/endpoints/{self.endpoint_id}/repoint",
                    principal="alice",
                    body={"target_deployment_id": self.second, "expected_revision": bad},
                )
                self.assertEqual(status, 400)


class RepointRaceTest(unittest.TestCase):
    """真的开两个线程抢同一个 revision（文件库、各自连接）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "race.sqlite")
        principals = PrincipalDirectory(
            [
                Principal("alice", "alice", 4001),
                Principal("dave", "dave", 4002),
                Principal("carol", "carol", 4000),
            ]
        )
        tokens = TokenDirectory(
            [TokenBinding("synthetic-alice", "alice", NON_ADMIN_SCOPES)]
        )
        from tests.registry.support import (
            default_ownership_prover,
            default_prober,
            default_protocol_prober,
        )

        self.service = RegistryService(
            store=self.store,
            tokens=tokens,
            principals=principals,
            config=RegistryConfig(mode="synthetic"),
            prober=default_prober(),
            protocol_prober=default_protocol_prober(),
            ownership_prover=default_ownership_prover(),
        )
        self.alice = resolve_requester("synthetic-alice", tokens, principals)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _engine(self, name: str) -> str:
        return self.service.register_engine(
            self.alice, {"name": name, "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"}
        )[0]["data"]["id"]

    def _model(self, name: str) -> str:
        return self.service.register_model(self.alice, {"name": name})[0]["data"]["id"]

    def _deployment(self, name: str) -> str:
        deployment_id = self.service.register_deployment(
            self.alice,
            {"name": name, "engine_id": self._engine(f"{name}-e"), "model_id": self._model(f"{name}-m")},
        )[0]["data"]["id"]
        self.service.verify_deployment(self.alice, deployment_id)
        return deployment_id

    def test_two_racing_repoints_produce_exactly_one_winner(self) -> None:
        first, second, third = self._deployment("a"), self._deployment("b"), self._deployment("c")
        endpoint_id = self.service.register_endpoint(
            self.alice, {"name": "svc", "deployment_id": first}
        )[0]["data"]["id"]
        revision = self.service.get_endpoint(self.alice, endpoint_id)["data"]["revision"]

        results: list[str] = []
        lock = threading.Lock()

        def attempt(target: str) -> None:
            try:
                self.service.repoint_endpoint(
                    self.alice,
                    endpoint_id,
                    {"target_deployment_id": target, "expected_revision": revision},
                )
                outcome = "ok"
            except Exception as exc:  # noqa: BLE001 - 记下契约码即可
                outcome = getattr(exc, "contract_code", type(exc).__name__)
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=attempt, args=(second,)),
            threading.Thread(target=attempt, args=(third,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(sorted(results), ["ok", "registry.revision_conflict"], results)
        # 落库的 revision 只前进了一格：没有「两次都写成功」。
        final = self.service.get_endpoint(self.alice, endpoint_id)["data"]
        self.assertEqual(final["revision"], revision + 1)


class RepointAuthorizationTest(unittest.TestCase):
    """谁能重指向、指向谁。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.mine = self.h.make_ready_deployment("alice", "mine")
        self.also_mine = self.h.make_ready_deployment("alice", "also-mine")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.mine)

    def test_a_non_owner_cannot_repoint(self) -> None:
        # 不可见 -> 404（与不存在同形）。刻意直接 POST：helper 会先 GET 一次，
        # 而那一步就已经是 404 了，测不到 repoint 本身。
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/repoint",
            principal="dave",
            body={"target_deployment_id": self.also_mine, "expected_revision": 1},
        )
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_found")

    def test_a_grantee_cannot_repoint(self) -> None:
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        # 可见（所以不是 404），但不能改（所以是 403）。
        status, _h, payload = self.h.repoint("dave", self.endpoint_id, self.also_mine)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_admin_manage_can_repoint_on_behalf(self) -> None:
        status, _h, payload = self.h.repoint("carol", self.endpoint_id, self.also_mine)
        self.assertEqual(status, 200, payload)
        # 审计记的是**实际动手的人**（alice 自己的审计里看不到这一条）。
        _s, _h, audit = self.h.request("GET", f"{PREFIX}/audit", principal="carol")
        repoints = [e for e in audit["data"]["items"] if e["action"] == "repoint"]
        self.assertEqual(repoints[0]["actor"], "carol")

    def test_admin_read_alone_cannot_repoint(self) -> None:
        h = Harness()
        h.with_scopes("carol", ADMIN_READ_ONLY_SCOPES)
        mine = h.make_ready_deployment("alice", "mine")
        other = h.make_ready_deployment("alice", "other")
        endpoint_id = h.make_endpoint_on("alice", "svc", mine)
        status, _h, _p = h.repoint("carol", endpoint_id, other)
        self.assertEqual(status, 403)

    def test_a_self_reported_admin_header_does_not_grant_anything(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/repoint",
            principal="dave",
            body={"target_deployment_id": self.also_mine, "expected_revision": 1},
            headers={"X-Admin": "true", "X-Role": "admin", "X-Owner": "alice"},
        )
        # 自报身份一律拒绝；而这里连端点对他都不可见，所以先撞上 400。
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "registry.identity_assertion_rejected")

    def test_a_self_reported_admin_body_field_does_not_grant_anything(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/repoint",
            principal="dave",
            body={"target_deployment_id": self.also_mine, "expected_revision": 1, "is_admin": True},
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "registry.identity_assertion_rejected")


class CrossOwnerTargetTest(unittest.TestCase):
    """只能指向**自己**的 Deployment。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.mine = self.h.make_ready_deployment("alice", "mine")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.mine)

    def test_an_invisible_foreign_deployment_is_404(self) -> None:
        theirs = self.h.make_ready_deployment("dave", "theirs")
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, theirs)
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_found")

    def test_a_visible_but_foreign_deployment_is_403(self) -> None:
        # dave 把它的 endpoint 共享给 alice，于是 alice **看得见**那个 deployment。
        theirs = self.h.make_ready_deployment("dave", "theirs")
        their_endpoint = self.h.make_endpoint_on("dave", "their-svc", theirs)
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{their_endpoint}/share",
            principal="dave",
            body={"mode": "users", "grantees": ["alice"]},
        )
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, theirs)
        # 看得见就不说「不存在」—— 说真话：它不是你的。
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")
        self.assertIn("another owner", payload["error"]["message"])

    def test_even_an_admin_cannot_mix_owners(self) -> None:
        # 代管不等于可以把自己的路径挂到别人的算力上。
        theirs = self.h.make_ready_deployment("dave", "theirs")
        status, _h, payload = self.h.repoint("carol", self.endpoint_id, theirs)
        self.assertEqual(status, 403, payload)


class TargetReadinessTest(unittest.TestCase):
    """目标必须真的 ready —— 历史与「以前行过」都不算。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.mine = self.h.make_ready_deployment("alice", "mine")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.mine)

    def test_a_registered_but_unverified_deployment_is_refused(self) -> None:
        engine_id = self.h.make_engine("alice", "unverified-e", port=UNREACHABLE_PORT)
        model_id = self.h.make_model("alice", "unverified-m")
        raw = self.h.request(
            "POST",
            f"{PREFIX}/deployments",
            principal="alice",
            body={"name": "unverified-d", "engine_id": engine_id, "model_id": model_id},
        )[2]
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, raw["data"]["id"])
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_verified")

    def test_a_missing_target_is_404(self) -> None:
        status, _h, payload = self.h.repoint("alice", self.endpoint_id, "dep_nope")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["code"], "registry.not_found")

    def test_a_missing_target_declaration_is_400(self) -> None:
        status, _h, _p = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/repoint",
            principal="alice",
            body={"expected_revision": 1},
        )
        self.assertEqual(status, 400)


class RoutingHistoryTest(unittest.TestCase):
    """历史只追加：换模型不删旧行、不丢旧模型数据。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.first = self.h.make_ready_deployment("alice", "big")
        self.second = self.h.make_ready_deployment("alice", "small")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.first)
        self.h.repoint("alice", self.endpoint_id, self.second)

    def test_the_history_records_both_hops_with_models(self) -> None:
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}/routings", principal="alice"
        )
        self.assertEqual(status, 200, payload)
        items = payload["data"]["items"]
        # 第 1 行是创建时的初始路由，第 2 行是这次 repoint。
        self.assertEqual(len(items), 2)
        row = items[0]
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["deployment_id"], self.second)
        self.assertEqual(row["previous_deployment_id"], self.first)
        # 「从哪个模型换到哪个模型」是这张表存在的理由。
        self.assertEqual(row["model_name"], "small-model")
        self.assertEqual(row["previous_model_name"], "big-model")
        self.assertEqual(row["actor"], "alice")
        self.assertEqual(row["actor_uid"], 4001)

    def test_rolling_back_writes_another_append_only_row(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/rollback",
            principal="alice",
            body={"expected_revision": self.h.endpoint_revision("alice", self.endpoint_id)},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["deployment_id"], self.first)
        self.assertEqual(payload["meta"]["operation"], "rollback")
        items = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}/routings", principal="alice"
        )[2]["data"]["items"]
        self.assertEqual([row["revision"] for row in items], [3, 2, 1])

    def test_the_old_model_and_its_deployment_are_not_deleted(self) -> None:
        _s, _h, models = self.h.request("GET", f"{PREFIX}/models", principal="alice")
        names = {item["name"] for item in models["data"]["items"]}
        self.assertIn("big-model", names)
        self.assertIn("small-model", names)
        _s, _h, deployments = self.h.request(
            "GET", f"{PREFIX}/deployments", principal="alice"
        )
        self.assertEqual(deployments["data"]["count"], 2)

    def test_the_history_is_owner_and_admin_only(self) -> None:
        status, _h, _p = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}/routings", principal="dave"
        )
        self.assertEqual(status, 404)  # 不可见 -> 与不存在同形
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        status, _h, _p = self.h.request(
            "GET", f"{PREFIX}/endpoints/{self.endpoint_id}/routings", principal="dave"
        )
        self.assertEqual(status, 403)  # 可见了，但历史只给 owner 与管理员


class RollbackTest(unittest.TestCase):
    """回滚走**同一套**检查，不是特权通道。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.first = self.h.make_ready_deployment("alice", "first")
        self.second = self.h.make_ready_deployment("alice", "second")
        self.endpoint_id = self.h.make_endpoint_on("alice", "svc", self.first)

    def test_rollback_without_history_is_404(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/rollback",
            principal="alice",
            body={"expected_revision": 1},
        )
        self.assertEqual(status, 404, payload)

    def test_rollback_requires_having_moved_somewhere(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/rollback",
            principal="alice",
            body={"expected_revision": self.h.endpoint_revision("alice", self.endpoint_id)},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["deployment_id"], self.first)
        self.assertTrue(payload["meta"]["pending_router_sync"])

    def test_rollback_also_resets_the_acl(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(len(self.h.endpoints_visible_to("dave")), 1)
        self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/rollback",
            principal="alice",
            body={"expected_revision": self.h.endpoint_revision("alice", self.endpoint_id)},
        )
        self.assertEqual(self.h.endpoints_visible_to("dave"), [])

    def test_a_non_owner_cannot_roll_back(self) -> None:
        self.h.repoint("alice", self.endpoint_id, self.second)
        status, _h, _p = self.h.request(
            "POST",
            f"{PREFIX}/endpoints/{self.endpoint_id}/rollback",
            principal="dave",
            body={"expected_revision": 2},
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
