"""引擎所有权与运维策略：8080 的唯一合法通路，以及「没证明就不许用」。

**本文件不开任何 socket。** 所有的探测都是注入的夹具
（``StaticProber`` / ``StaticProtocolProber`` / ``StaticOwnershipProver``），
所以「登记一条被运维承认的 8080」在这里测的是**准入路径**，不是「Strata 活着」。
真实 8080 与任何真实用户进程都不被访问 —— 那既不在本仓库的边界内，
也不是这一层能证伪的事。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.registry.errors import InvalidRequest, StorageError
from backend.registry.ownership import (
    EMPTY_POLICY,
    OWNERSHIP_OPERATOR_VERIFIED,
    OWNERSHIP_POLICY_ENFORCED,
    OWNERSHIP_UNVERIFIED,
    OWNERSHIP_UID_VERIFIED,
    OPERATOR_POLICY_SCHEMA,
    NoOwnershipProver,
    OperatorPolicy,
    OwnershipProof,
    StaticOwnershipProver,
    load_operator_policy,
    operator_targets_for_fixture,
    unverified_proof,
)
from backend.registry.registry import RegistryConfig
from backend.registry.urls import OperatorTarget, validate_engine_target_url
from tests.registry.support import HEALTHY_PORT, Harness, close_all

PREFIX = "/api/v2/registry"

#: 既有上游推理服务的端口：**硬保留**，只有运维策略能放行。
STRATA_PORT = 8080


def tearDownModule() -> None:
    close_all()


def blessed_strata_policy(*, port: int = STRATA_PORT) -> OperatorPolicy:
    """一份夹具策略：承认「127.0.0.1:<port> 上有一台既有服务」。"""
    return operator_targets_for_fixture(
        [
            OperatorTarget(
                scheme="http",
                host="127.0.0.1",
                port=port,
                path_prefix="/",
                label="existing upstream (fixture)",
                source="injected_fixture",
            )
        ]
    )


class OperatorTargetTest(unittest.TestCase):
    def test_a_loopback_entry_is_accepted(self) -> None:
        entry = OperatorTarget(scheme="http", host="127.0.0.1", port=8080)
        self.assertTrue(entry.matches(scheme="http", host="127.0.0.1", port=8080, path="/v1"))

    def test_an_entry_may_not_point_outside_loopback(self) -> None:
        # 运维策略**也**没有把目标放到回环之外的能力。这条边界没有例外。
        for host in ("10.0.0.1", "example.com", "0.0.0.0"):
            with self.subTest(host=host):
                with self.assertRaises(InvalidRequest):
                    OperatorTarget(scheme="http", host=host, port=8080)

    def test_scheme_port_and_prefix_are_validated(self) -> None:
        with self.assertRaises(InvalidRequest):
            OperatorTarget(scheme="file", host="127.0.0.1", port=8080)
        with self.assertRaises(InvalidRequest):
            OperatorTarget(scheme="http", host="127.0.0.1", port=0)
        with self.assertRaises(InvalidRequest):
            OperatorTarget(scheme="http", host="127.0.0.1", port=8080, path_prefix="v1")
        with self.assertRaises(InvalidRequest):
            OperatorTarget(scheme="http", host="127.0.0.1", port=8080, path_prefix="/../x")

    def test_matching_is_exact_not_fuzzy(self) -> None:
        entry = OperatorTarget(scheme="http", host="127.0.0.1", port=8080, path_prefix="/v1")
        self.assertTrue(entry.matches(scheme="http", host="127.0.0.1", port=8080, path="/v1/models"))
        # 端口不同、主机不同、前缀之外：都不算。
        self.assertFalse(entry.matches(scheme="http", host="127.0.0.1", port=8081, path="/v1"))
        self.assertFalse(entry.matches(scheme="http", host="::1", port=8080, path="/v1"))
        self.assertFalse(entry.matches(scheme="http", host="127.0.0.1", port=8080, path="/other"))


class OperatorPolicyLoadingTest(unittest.TestCase):
    """策略文件必须来自 root/运维 —— 任何一个本机用户都不该能自己开 8080。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "operator.json"
        self.addCleanup(self.tmp.cleanup)

    def _write(self, payload: object, *, mode: int = 0o600) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.path.chmod(mode)

    def test_a_well_formed_owner_controlled_file_is_accepted(self) -> None:
        self._write(
            {
                "schema": OPERATOR_POLICY_SCHEMA,
                "targets": [
                    {"scheme": "http", "host": "127.0.0.1", "port": 8080, "label": "strata"}
                ],
            }
        )
        policy = load_operator_policy(self.path)
        self.assertEqual(len(policy.targets), 1)
        self.assertIn("mode=0o600", policy.provenance)

    def test_a_group_or_world_writable_file_is_refused(self) -> None:
        self._write({"schema": OPERATOR_POLICY_SCHEMA, "targets": []}, mode=0o666)
        with self.assertRaises(StorageError) as ctx:
            load_operator_policy(self.path)
        self.assertIn("world-writable", str(ctx.exception))

    def test_a_symlink_is_refused(self) -> None:
        real = Path(self.tmp.name) / "real.json"
        real.write_text(json.dumps({"schema": OPERATOR_POLICY_SCHEMA, "targets": []}))
        real.chmod(0o600)
        link = Path(self.tmp.name) / "link.json"
        link.symlink_to(real)
        with self.assertRaises(StorageError) as ctx:
            load_operator_policy(link)
        self.assertIn("symbolic link", str(ctx.exception))

    def test_a_directory_is_refused(self) -> None:
        with self.assertRaises(StorageError):
            load_operator_policy(self.tmp.name)

    def test_a_file_owned_by_someone_else_is_refused(self) -> None:
        # 不用真的造一个别人的文件：把「可接受的属主」收窄到别的 uid 即可 ——
        # 测的是那条判定本身，而它正是拒绝的依据。
        self._write({"schema": OPERATOR_POLICY_SCHEMA, "targets": []})
        with self.assertRaises(StorageError) as ctx:
            load_operator_policy(self.path, allowed_owners={os.getuid() + 12345})
        self.assertIn("owned by root or by the service account", str(ctx.exception))

    def test_a_missing_file_is_refused(self) -> None:
        with self.assertRaises(StorageError):
            load_operator_policy(Path(self.tmp.name) / "nope.json")

    def test_malformed_json_is_refused(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaises(StorageError):
            load_operator_policy(self.path)

    def test_an_unknown_schema_is_refused(self) -> None:
        self._write({"schema": "something-else/9", "targets": []})
        with self.assertRaises(StorageError):
            load_operator_policy(self.path)

    def test_a_non_loopback_entry_in_the_file_is_refused(self) -> None:
        # 手写一份坏策略：不能靠改文件把目标放到内网。
        self._write(
            {
                "schema": OPERATOR_POLICY_SCHEMA,
                "targets": [{"scheme": "http", "host": "192.0.2.9", "port": 8080}],
            }
        )
        with self.assertRaises(StorageError):
            load_operator_policy(self.path)

    def test_an_oversized_file_is_refused(self) -> None:
        self.path.write_text("x" * (70 * 1024), encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaises(StorageError):
            load_operator_policy(self.path)

    def test_a_non_list_targets_field_is_refused(self) -> None:
        self._write({"schema": OPERATOR_POLICY_SCHEMA, "targets": {"port": 8080}})
        with self.assertRaises(StorageError):
            load_operator_policy(self.path)

    def test_the_empty_policy_blesses_nothing(self) -> None:
        self.assertTrue(EMPTY_POLICY.empty)
        self.assertIsNone(
            EMPTY_POLICY.match(
                validate_engine_target_url(f"http://127.0.0.1:{HEALTHY_PORT}/v1")
            )
        )


class ReservedPortAdmissionTest(unittest.TestCase):
    """8080 的准入：用户永远不行，运维策略 + 管理员才行。"""

    def test_a_plain_user_cannot_reach_8080_even_with_a_blessing_in_place(self) -> None:
        # 策略里**明确承认了** 8080，但登记的是一条普通用户的请求 ——
        # allowlist 是按登记模式选的，所以运维的承认对普通路径不生效。
        h = Harness(config=RegistryConfig(mode="synthetic", operator_policy=blessed_strata_policy()))
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "sneaky", "target_url": f"http://127.0.0.1:{STRATA_PORT}/v1"},
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "registry.invalid_request")
        self.assertIn("reserved", payload["error"]["message"])

    def test_the_blessing_does_not_leak_into_user_mode_at_all(self) -> None:
        # 同样一个目标，用户模式被拒、system_existing 通过 —— 差别只来自模式与权限。
        blessed = validate_engine_target_url(
            f"http://127.0.0.1:{STRATA_PORT}/v1",
            operator_allowlist=blessed_strata_policy().targets,
        )
        self.assertTrue(blessed.operator_blessed)
        self.assertEqual(blessed.operator_label, "existing upstream (fixture)")
        with self.assertRaises(InvalidRequest):
            validate_engine_target_url(f"http://127.0.0.1:{STRATA_PORT}/v1")

    def test_system_existing_requires_admin_manage(self) -> None:
        h = Harness(config=RegistryConfig(mode="synthetic", operator_policy=blessed_strata_policy()))
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={
                "name": "strata",
                "engine_mode": "system_existing",
                "target_url": f"http://127.0.0.1:{STRATA_PORT}/v1",
            },
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["code"], "registry.access_denied")

    def test_admin_with_a_matching_entry_may_register_the_existing_service(self) -> None:
        h = Harness(config=RegistryConfig(mode="synthetic", operator_policy=blessed_strata_policy()))
        status, _h, payload = self.h_register(h)
        self.assertEqual(status, 200, payload)
        data = payload["data"]
        # 登记成功 ≠ 它现在活着：夹具 prober 对 8080 没有结论，所以是 unavailable。
        # 这**正是**想要的诚实：准入通过了，可达性仍然要有证据。
        self.assertEqual(data["status"], "unavailable")
        self.assertEqual(data["mode"], "system_existing")
        self.assertEqual(data["source"], "operator_existing_service")
        self.assertEqual(data["ownership"]["state"], OWNERSHIP_OPERATOR_VERIFIED)
        self.assertTrue(data["ownership"]["verified"])
        self.assertTrue(data["target"]["operator_blessed"])

    def h_register(self, h: Harness):
        return h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="carol",
            body={
                "name": "strata",
                "engine_mode": "system_existing",
                "target_url": f"http://127.0.0.1:{STRATA_PORT}/v1",
                "protocol": "openai_compatible",
            },
        )

    def test_admin_without_a_matching_entry_is_refused(self) -> None:
        # 策略承认的是 8080，请求的却是 8000：没有 entry 命中，就不算「既有服务」。
        h = Harness(config=RegistryConfig(mode="synthetic", operator_policy=blessed_strata_policy()))
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="carol",
            body={
                "name": "not-blessed",
                "engine_mode": "system_existing",
                "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1",
            },
        )
        self.assertEqual(status, 403, payload)
        self.assertIn("operator-owned", payload["error"]["message"])

    def test_system_existing_without_any_policy_is_refused(self) -> None:
        # 没有任何策略时，8080 这个**目标本身**就不合法（400），而不是
        # 「合法但没被承认」（403）。两者是不同的事实，两边都要说清楚：
        # 400 的消息还会直接告诉调用方唯一的出路是运维策略。
        h = Harness()
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="carol",
            body={
                "name": "strata",
                "engine_mode": "system_existing",
                "target_url": f"http://127.0.0.1:{STRATA_PORT}/v1",
            },
        )
        self.assertEqual(status, 400, payload)
        self.assertIn("operator-owned allowlist entry", payload["error"]["message"])

    def test_an_unknown_engine_mode_is_refused(self) -> None:
        h = Harness()
        status, _h, _p = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "e", "engine_mode": "root", "target_url": "http://127.0.0.1:8000/v1"},
        )
        self.assertEqual(status, 400)

    def test_the_audit_records_the_operator_blessing(self) -> None:
        h = Harness(config=RegistryConfig(mode="synthetic", operator_policy=blessed_strata_policy()))
        self.h_register(h)
        _s, _h, audit = h.request("GET", f"{PREFIX}/audit", principal="carol")
        registers = [
            e
            for e in audit["data"]["items"]
            if e["action"] == "register" and e["object_type"] == "engine"
        ]
        self.assertTrue(registers)
        detail = registers[0]["detail"]
        # 一次「靠运维策略放行」的登记必须在数据里看得见。
        self.assertTrue(detail["operator_blessed"])
        self.assertEqual(detail["mode"], "system_existing")
        self.assertEqual(detail["ownership_state"], OWNERSHIP_OPERATOR_VERIFIED)

    def test_a_user_registration_on_a_normal_port_is_not_marked_blessed(self) -> None:
        h = Harness(config=RegistryConfig(mode="synthetic", operator_policy=blessed_strata_policy()))
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="alice",
            body={"name": "mine", "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1"},
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["data"]["target"]["operator_blessed"])
        self.assertEqual(payload["data"]["mode"], "user")


class OwnershipProofTest(unittest.TestCase):
    def test_the_default_prover_never_claims_ownership(self) -> None:
        prover = NoOwnershipProver()
        target = validate_engine_target_url(f"http://127.0.0.1:{HEALTHY_PORT}/v1")
        proof = prover.prove(target)
        self.assertEqual(proof.state, OWNERSHIP_UNVERIFIED)
        self.assertFalse(proof.verified)

    def test_the_fixture_prover_is_explicitly_a_test_double(self) -> None:
        prover = StaticOwnershipProver({("127.0.0.1", HEALTHY_PORT): 4001})
        target = validate_engine_target_url(f"http://127.0.0.1:{HEALTHY_PORT}/v1")
        proof = prover.prove(target)
        self.assertEqual(proof.state, OWNERSHIP_UID_VERIFIED)
        self.assertEqual(proof.method, "static_test_double")
        self.assertEqual(proof.uid, 4001)

    def test_an_unlisted_target_stays_unverified(self) -> None:
        prover = StaticOwnershipProver({})
        target = validate_engine_target_url("http://127.0.0.1:8081/v1")
        self.assertFalse(prover.prove(target).verified)

    def test_proof_shapes_are_validated(self) -> None:
        with self.assertRaises(InvalidRequest):
            OwnershipProof(state="probably-fine", method="guess")
        with self.assertRaises(InvalidRequest):
            OwnershipProof(state=OWNERSHIP_UID_VERIFIED, method="x", uid=-1)

    def test_only_enumerated_states_count_as_verified(self) -> None:
        # 用枚举而不是「!= UNVERIFIED」：以后新增状态时，取反会默认放行，枚举会默认拒绝。
        self.assertTrue(OwnershipProof(OWNERSHIP_OPERATOR_VERIFIED, "m").verified)
        self.assertFalse(OwnershipProof(OWNERSHIP_UNVERIFIED, "m").verified)
        self.assertFalse(unverified_proof().verified)


class OwnershipEnforcementTest(unittest.TestCase):
    """``enforced`` 档：没有证明就不许共享/调用。"""

    def _harness(self, *, policy: str, prover: object | None) -> Harness:
        return Harness(
            config=RegistryConfig(mode="synthetic", ownership_policy=policy),
            ownership_prover=prover,
        )

    def test_advisory_records_but_does_not_block(self) -> None:
        h = self._harness(policy="advisory", prover=None)
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 200, payload)
        # 但所有权状态仍然如实是「未证明」。
        engine = h.request("GET", f"{PREFIX}/engines", principal="alice")[2]["data"]["items"][0]
        self.assertEqual(engine["ownership"]["state"], OWNERSHIP_UNVERIFIED)

    def test_enforced_blocks_sharing_an_unproven_engine(self) -> None:
        h = self._harness(policy=OWNERSHIP_POLICY_ENFORCED, prover=None)
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.ownership_unverified")

    def test_enforced_blocks_invoking_an_unproven_engine(self) -> None:
        h = self._harness(policy=OWNERSHIP_POLICY_ENFORCED, prover=None)
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/invoke",
            principal="alice",
            body={"prompt": "hi"},
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.ownership_unverified")
        self.assertEqual(h.gateway.calls, [])

    def test_enforced_allows_a_proven_engine(self) -> None:
        h = self._harness(
            policy=OWNERSHIP_POLICY_ENFORCED,
            prover=StaticOwnershipProver({("127.0.0.1", HEALTHY_PORT): 4001}),
        )
        endpoint_id = h.make_ready_endpoint("alice")
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/endpoints/{endpoint_id}/share",
            principal="alice",
            body={"mode": "users", "grantees": ["dave"]},
        )
        self.assertEqual(status, 200, payload)

    def test_enforced_allows_an_operator_admitted_service_without_any_prover(self) -> None:
        # 运维策略本身就是一种证明 —— 不需要本机启发式。
        h = Harness(
            config=RegistryConfig(
                mode="synthetic",
                ownership_policy=OWNERSHIP_POLICY_ENFORCED,
                operator_policy=blessed_strata_policy(port=HEALTHY_PORT),
            ),
            ownership_prover=None,
        )
        status, _h, payload = h.request(
            "POST",
            f"{PREFIX}/engines",
            principal="carol",
            body={
                "name": "strata",
                "engine_mode": "system_existing",
                "target_url": f"http://127.0.0.1:{HEALTHY_PORT}/v1",
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["data"]["ownership"]["verified"])

    def test_repoint_under_enforcement_requires_a_proven_target(self) -> None:
        # 重指向是**新**路径，没有历史包袱：它在两种策略下都要求目标已证明吗？
        # 不 —— 它遵守同一个开关。这里的断言就是这条规则本身。
        h = self._harness(policy=OWNERSHIP_POLICY_ENFORCED, prover=None)
        first = h.make_ready_deployment("alice", "a")
        second = h.make_ready_deployment("alice", "b")
        endpoint_id = h.make_endpoint_on("alice", "svc", first)
        status, _h, payload = h.repoint("alice", endpoint_id, second)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "registry.ownership_unverified")

    def test_the_engine_dto_names_the_missing_proof(self) -> None:
        h = self._harness(policy=OWNERSHIP_POLICY_ENFORCED, prover=None)
        h.make_engine("alice", "e")
        engine = h.request("GET", f"{PREFIX}/engines", principal="alice")[2]["data"]["items"][0]
        self.assertIn("ownership_unverified", engine["blockers"])
        self.assertEqual(engine["ownership"]["state"], OWNERSHIP_UNVERIFIED)
        self.assertIn(engine["verification"]["ownership_policy"], ("advisory", "enforced"))


class ConfigPostureTest(unittest.TestCase):
    def test_an_unknown_ownership_policy_is_refused(self) -> None:
        with self.assertRaises(InvalidRequest):
            RegistryConfig(ownership_policy="yolo")

    def test_the_production_posture_turns_both_switches_on(self) -> None:
        config = RegistryConfig.production_posture()
        self.assertTrue(config.ownership_enforced)
        self.assertTrue(config.require_protocol_verification)
        # 但仍然**不**默认放开端口或 https。
        self.assertFalse(config.allow_https_targets)

    def test_the_compatibility_defaults_are_the_permissive_ones(self) -> None:
        # 这是被显式记录下来的取舍：默认必须保持历史行为，因为
        # backend/bff 用默认配置构造服务，而本模块不允许修改它。
        config = RegistryConfig()
        self.assertFalse(config.ownership_enforced)
        self.assertFalse(config.require_protocol_verification)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
