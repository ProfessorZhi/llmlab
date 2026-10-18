"""The `portal-ops/1` payload itself: shape, provenance, and the stub sections.

The front-end validates this payload against ``web/js/features/contract.js``
before it paints anything, so a shape error here is a blank panel there. The
cross-language check lives in ``tests/web-features/ops-bff-payload.test.js``;
what this file covers is the parts that are cheaper to assert from Python — the
health/runtime mapping, the mode claim, the stubs, and the masked key list.
"""

from __future__ import annotations

import tempfile
import unittest

from backend.auth_linux import handoff as linux_handoff
from backend.registry import health as registry_health
from backend.registry.identity import TokenBinding
from backend.bff import ops as bff_ops
from tests.bff import support as S


class HealthMappingTests(unittest.TestCase):
    """`healthy` while not running is a combination the contract refuses."""

    def test_every_registry_state_maps_to_a_coherent_pair(self):
        for state, (health, runtime) in bff_ops._HEALTH_TO_PORTAL.items():
            _, _, _, _ = bff_ops._health_columns({"state": state})
            if health == "healthy":
                self.assertEqual(runtime, "running", state)
            if health == "unknown":
                self.assertEqual(runtime, "unknown", state)

    def test_an_unknown_state_must_carry_a_reason(self):
        health, runtime, health_reason, runtime_reason = bff_ops._health_columns(
            {"state": "unknown"}
        )
        self.assertEqual((health, runtime), ("unknown", "unknown"))
        self.assertTrue(health_reason, "an unknown health must say why")
        self.assertTrue(runtime_reason)

    def test_the_registrys_own_reason_is_passed_through_not_translated(self):
        _, _, health_reason, _ = bff_ops._health_columns(
            {"state": "unknown", "reason": registry_health.REASON_PROBE_TIMEOUT}
        )
        self.assertEqual(health_reason, registry_health.REASON_PROBE_TIMEOUT)

    def test_an_unrecognised_state_degrades_to_unknown(self):
        health, runtime, reason, _ = bff_ops._health_columns({"state": "banana"})
        self.assertEqual((health, runtime), ("unknown", "unknown"))
        self.assertTrue(reason)


class TransportModeTests(unittest.TestCase):
    def test_a_synthetic_build_never_claims_to_be_live(self):
        config = bff_ops.OpsConfig(
            registry_mode="synthetic", gateway_adapter="mock", gateway_attached=True
        )
        self.assertTrue(config.synthetic)
        self.assertEqual(config.transport_mode, "demo")

    def test_live_requires_an_attached_gateway(self):
        """The contract refuses `live` without one — and so does this config."""
        detached = bff_ops.OpsConfig(
            registry_mode="live", gateway_adapter="detached", gateway_attached=False
        )
        self.assertEqual(detached.transport_mode, "demo")

        attached = bff_ops.OpsConfig(
            registry_mode="live", gateway_adapter="real", gateway_attached=True
        )
        self.assertEqual(attached.transport_mode, "live")


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def payload(self, name="alice", visibility=None, *, admin=False):
        client = self.h.admin_client(name) if admin else self.h.client(name)
        return S.ops_of(S.fetch_ops(client, visibility))

    def test_the_payload_declares_its_own_schema_and_provenance(self):
        ops = self.payload()
        self.assertEqual(ops["schema_version"], bff_ops.OPS_SCHEMA)
        self.assertTrue(ops["notes"], "a payload with no notes has stopped explaining itself")
        self.assertEqual(ops["transport"]["mode"], "demo")
        self.assertTrue(ops["transport"]["synthetic"])
        self.assertEqual(ops["transport"]["gateway_attached"], True)

    def test_a_synthetic_payload_does_not_claim_to_be_live(self):
        ops = self.payload()
        self.assertEqual(ops["synthetic"], True)
        self.assertNotEqual(ops["transport"]["mode"], "live")

    def test_the_wired_observability_section_is_unknown_not_zero(self):
        """指标卡里没有数字，哪怕观测模块真的量到了一条读数。"""
        ops = self.payload()
        for metric in ops["metrics"]["items"]:
            self.assertIsNone(metric["value"])
            self.assertEqual(metric["state"], "unknown")
            self.assertIsNone(metric["updated_at"])
            self.assertTrue(metric["reason"])
        self.assertIsNone(ops["metrics"]["my_source"])
        self.assertIsNone(ops["metrics"]["global_source"])

    def test_the_wired_evaluation_section_is_never_ready(self):
        ops = self.payload()
        self.assertFalse(ops["evaluation"]["submit"]["ready"])
        self.assertTrue(ops["evaluation"]["submit"]["reason"])

    def test_the_detached_stubs_still_behave_as_documented(self):
        """适配器没接线时的形状不变 —— 它是 fail-closed 的那一档。"""
        identity = self.h.app.resolve_session(self.h.session_id("alice"))
        detached_obs = bff_ops.DetachedObservabilityAdapter()
        for metric in detached_obs.metrics(identity=identity, is_admin=False):
            self.assertIsNone(metric["value"])
            self.assertEqual(metric["reason"], bff_ops.REASON_OBSERVABILITY_NOT_ATTACHED)
        self.assertIsNone(
            detached_obs.trends(identity=identity, is_admin=False, window="24h")
        )

        detached_eval = bff_ops.DetachedEvaluationAdapter()
        self.assertEqual(detached_eval.runs(identity=identity, is_admin=False), [])
        submit = detached_eval.submit(identity=identity, is_admin=False)
        self.assertFalse(submit["ready"])
        self.assertEqual(submit["reason"], bff_ops.REASON_EVALUATION_NOT_READY)

    def test_the_registry_adapter_is_reported_as_attached(self):
        registry = self.payload()["registry"]
        self.assertTrue(registry["adapter_attached"])
        self.assertIsNone(registry["reason"])

    def test_the_links_are_withheld_rather_than_hidden_by_scope(self):
        """A member holds the self-calls scope, so the link is live."""
        ops = self.payload()
        (link,) = ops["links"]
        self.assertEqual(link["href"], "/atlas#calls")
        self.assertEqual(link["requires"], "atlas:calls:self:read")

    def test_an_admin_principal_row_carries_the_standing_role_only(self):
        """The list states what each person *is*, never what one session may do.

        Elevation belongs to a session, so a principal row may not claim
        `platform_admin` — `carol` is a granted Linux administrator and his row
        still says `member`, because the moment he signs out that is what he is.
        A principal with no Portal user row yet gets **no** `roles` key rather
        than a default one: the directory knows who exists, not what they may
        do, and inventing a role is how an admin table starts lying.
        """
        self.h.client("alice")
        self.h.client("carol")
        ops = self.payload("carol", admin=True)
        rows = {row["uid"]: row for row in ops["admin"]["principals"]}

        for name in ("carol", "alice"):
            with self.subTest(name=name):
                row = rows[self.h.principal_of(name)]
                self.assertEqual(row["roles"], ["member"])
                self.assertNotIn("portal:admin:read", row["scopes"])

        # Never signed in, so no Portal row exists for them.
        self.assertNotIn("roles", rows[self.h.principal_of("bob")])
        self.assertNotIn("scopes", rows[self.h.principal_of("bob")])

    def test_the_admin_list_needs_an_elevated_session_to_exist_at_all(self):
        """A member gets no list; the same person elevated does."""
        self.h.client("carol")
        plain = self.payload("carol")
        self.assertFalse(plain["admin"]["available"])
        self.assertEqual(plain["admin"]["principals"], [])

        elevated = self.payload("carol", admin=True)
        self.assertTrue(elevated["admin"]["available"])
        self.assertTrue(elevated["admin"]["principals"])

    def test_no_principal_row_names_a_linux_account_or_a_uid_number(self):
        """The admin list is about platform principals, not machine accounts."""
        ops = self.payload("carol", admin=True)
        for row in ops["admin"]["principals"]:
            self.assertTrue(row["uid"].startswith("linux:"))
            self.assertNotEqual(row["uid"], "4003")
            self.assertNotIn("bob", row["uid"])

    def test_the_key_list_shows_masked_prefixes_only(self):
        from backend.bff.app import OpsApplication
        from backend.bff.ops import OpsConfig

        secret = "synthetic-bearer-token-that-must-never-be-rendered"
        bindings = (
            TokenBinding(
                secret,
                self.h.principal_of("alice"),
                frozenset({"registry:read"}),
                label="workstation",
            ),
            TokenBinding(
                "another-secret",
                self.h.principal_of("bob"),
                frozenset({"registry:read"}),
                label="someone-else",
            ),
        )
        app = OpsApplication(
            auth_app=self.h.auth_app,
            service=self.h.service,
            accounts=self.h.accounts,
            token_bindings=bindings,
            ops_config=OpsConfig(gateway_attached=True),
        )
        client = S.WsgiClient(app=app, host="127.0.0.1:8790", scheme="http")
        client.cookies.update(self.h.client("alice").cookies)
        ops = S.ops_of(S.fetch_ops(client))

        blob = str(ops)
        self.assertNotIn(secret, blob)
        self.assertNotIn("another-secret", blob)
        # Only my own binding, and only its mask.
        self.assertEqual(len(ops["keys"]), 1)
        key = ops["keys"][0]
        self.assertEqual(key["name"], "workstation")
        self.assertEqual(key["status"], "active")
        self.assertEqual(key["scopes"], ["registry:read"])
        self.assertIn("•", key["prefix"])
        self.assertLessEqual(len(key["prefix"]), 24)
        self.assertNotIn(key["name"], ("someone-else",))

    def test_the_shared_view_never_leaks_the_co_grantee_list_to_a_grantee(self):
        """The registry hides the ACL from non-owners; the BFF must not undo it."""
        ids = S.register_baseline(self.h, "dave")
        S.share(self.h.client("dave"), ids["deployment_id"], mode="selected", principals=["4003", "alice"])
        shared = S.ops_of(S.fetch_ops(self.h.client("bob"), "shared"))["registry"]["shared"]
        row = next(r for r in shared if r["endpoint_id"] == ids["endpoint_id"])
        self.assertEqual(row["share"]["principals"], [self.h.principal_of("bob")])

    def test_a_health_reason_is_present_whenever_health_is_unknown(self):
        for ops in (self.payload(), self.payload("carol", admin=True)):
            for row in ops["registry"]["owned"]:
                if row["health"] == "unknown":
                    self.assertTrue(row["health_reason"], row["deployment_id"])

    def test_an_invalid_visibility_is_refused(self):
        with self.assertRaises(ValueError):
            bff_ops.build_ops(
                principal=self.h.app.resolve_session(self.h.session_id("alice")),
                service=self.h.service,
                visibility="everything",
            )


class IdentityShapeTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_the_principal_is_the_handoffs_own_identifier(self):
        """The BFF does not invent a second identifier scheme."""
        principal = self.h.app.resolve_session(self.h.session_id("bob"))
        self.assertEqual(
            principal.principal,
            linux_handoff.linux_user_id(S.SYNTHETIC_HOST_ID, S.REAL_ACCOUNT_UID),
        )

    def test_the_scope_block_lists_the_portal_vocabulary_only(self):
        from backend.bff import scopes as bff_scopes

        ops = S.ops_of(S.fetch_ops(self.h.client("alice")))
        for scope in ops["scope"]["scopes"]:
            self.assertIn(scope, bff_scopes.PORTAL_KNOWN_SCOPES)
        self.assertEqual(ops["scope"]["roles"], ["member"])
        self.assertEqual(ops["scope"]["subject"], self.h.principal_of("alice"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
