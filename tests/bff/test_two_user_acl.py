"""The offline mock end-to-end, across two real users.

This is the vertical slice the whole change exists to prove, driven entirely
through HTTP against the same WSGI callable the browser would hit:

    A registers metadata (engine → model → deployment → endpoint)
      → the endpoint starts private
        → B cannot see it
          → A shares it to B's *uid*
            → B sees it, and invoking it through the mock gateway succeeds
              → A revokes
                → B cannot see it again

Nothing here is a screenshot and nothing here is a real model. The forwarded
call is answered by the Registry's ``MockGatewayAdapter``, which labels its own
output ``synthetic`` and returns ``tokens: null`` because it measured nothing.
The PAM double proves the surrounding machinery, not PAM.
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import ops as bff_ops
from tests.bff import support as S


class TwoUserACLTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.alice = self.h.client("alice")          # uid 4001
        self.bob = self.h.client("bob")          # uid 4003
        self.ids = S.register_baseline(self.h, "alice")

    # -- helpers ----------------------------------------------------------

    def owned(self, client):
        return S.ops_of(S.fetch_ops(client))["registry"]["owned"]

    def shared(self, client):
        return S.ops_of(S.fetch_ops(client, "shared"))["registry"]["shared"]

    # -- registration -----------------------------------------------------

    def test_an_endpoint_is_private_the_moment_it_exists(self):
        (row,) = self.owned(self.alice)
        self.assertEqual(row["share"]["mode"], "private")
        self.assertEqual(row["share"]["principals"], [])
        self.assertTrue(row["share"]["revocable"])
        self.assertTrue(row["owner_is_self"])
        self.assertEqual(row["owner_uid"], self.h.principal_of("alice"))

        # The registry's own record agrees, and says so in its own words.
        endpoint = self.h.service.store.endpoint_by_id(self.ids["endpoint_id"])
        self.assertEqual(endpoint.visibility, "private")

    def test_ownership_is_decided_by_the_session_not_the_route(self):
        """`owner_is_self` is computed from the session subject, so B's own view
        of A's deployment never says "mine" — and A's own view does."""
        self.assertTrue(self.owned(self.alice)[0]["owner_is_self"])
        self.assertEqual(self.owned(self.bob), [])

    # -- before the share -------------------------------------------------

    def test_b_cannot_see_a_before_the_share(self):
        self.assertEqual(self.owned(self.bob), [])
        self.assertEqual(self.shared(self.bob), [])
        # And not through a direct read either: invisible is 404, not 403, so
        # the endpoint cannot be used to test whether an id exists.
        from backend.registry import errors as registry_errors

        with self.assertRaises(registry_errors.NotFound):
            self.h.service.policy(self.h.requester_for("bob")).require_endpoint_read(
                self.ids["endpoint_id"]
            )

    # -- the share --------------------------------------------------------

    def test_sharing_to_a_uid_makes_it_visible_to_that_user(self):
        response = S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        self.assertEqual(response.status, 200, response.body)

        (row,) = self.shared(self.bob)
        self.assertEqual(row["endpoint_id"], self.ids["endpoint_id"])
        self.assertFalse(row["owner_is_self"])
        self.assertEqual(row["owner_uid"], self.h.principal_of("alice"))
        self.assertEqual(row["share"]["mode"], "selected")
        self.assertTrue(row["share"]["revocable"])
        self.assertEqual(row["granted_by"], self.h.principal_of("alice"))

        # What was written is a principal, not a uid and not a login name.
        stored = self.h.service.store.grantees(self.ids["endpoint_id"])
        self.assertEqual(stored, (self.h.principal_of("bob"),))

    def test_the_owners_own_view_shows_the_share_scope(self):
        S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        (row,) = self.owned(self.alice)
        self.assertEqual(row["share"]["mode"], "selected")
        self.assertEqual(row["share"]["principals"], [self.h.principal_of("bob")])

    def test_b_can_then_invoke_it_and_the_mock_gateway_answers(self):
        S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        # Registry 的 P0 调用门（PR #122）要求一次 Gateway 回读确认过的路由
        # （``router_sync_applied``）；新建的 Endpoint 停在 ``pending_router_sync``。
        # 这里补上那条**回执成功时本来就会写下的行**，不改变 deployment 目标。
        self.h.acknowledge_route(self.ids["endpoint_id"])
        principal = self.h.app.resolve_session(self.h.session_id("bob"))
        result = self.h.service.invoke(
            principal.requester,
            self.ids["endpoint_id"],
            {"model": "model-alice", "messages": [{"role": "user", "content": "hi"}]},
        )
        data = result["data"]
        self.assertEqual(data["endpoint_id"], self.ids["endpoint_id"])
        self.assertTrue(data["result"]["forwarded"])
        self.assertTrue(data["result"]["synthetic"], "a mock must label its own output")
        self.assertIsNone(data["result"]["tokens"])
        self.assertIsNone(data["result"]["latency_ms"])
        self.assertEqual(data["gateway"]["adapter"], "mock")
        self.assertEqual(data["gateway"]["authorization"], "server_side_enforced")

    def test_b_cannot_invoke_it_before_the_share(self):
        from backend.registry import errors as registry_errors

        principal = self.h.app.resolve_session(self.h.session_id("bob"))
        with self.assertRaises(registry_errors.NotFound):
            self.h.service.invoke(principal.requester, self.ids["endpoint_id"], {})

    # -- revocation -------------------------------------------------------

    def test_revoking_hides_it_again_immediately(self):
        S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        self.assertEqual(len(self.shared(self.bob)), 1)

        revoked = S.share(self.alice, self.ids["deployment_id"], mode="private")
        self.assertEqual(revoked.status, 200)
        self.assertEqual(revoked.json()["data"]["deployment"]["share"]["mode"], "private")
        self.assertEqual(revoked.json()["data"]["deployment"]["share"]["principals"], [])

        self.assertEqual(self.shared(self.bob), [])
        # The grant row is gone, not merely flagged.
        self.assertEqual(self.h.service.store.grantees(self.ids["endpoint_id"]), ())

        from backend.registry import errors as registry_errors

        principal = self.h.app.resolve_session(self.h.session_id("bob"))
        with self.assertRaises(registry_errors.NotFound):
            self.h.service.invoke(principal.requester, self.ids["endpoint_id"], {})

    def test_sharing_with_everyone_is_reachable_and_also_revocable(self):
        response = S.share(self.alice, self.ids["deployment_id"], mode="all")
        self.assertEqual(response.status, 200, response.body)
        (row,) = self.shared(self.bob)
        self.assertEqual(row["share"]["mode"], "all")
        self.assertTrue(row["share"]["revocable"])

        S.share(self.alice, self.ids["deployment_id"], mode="private")
        self.assertEqual(self.shared(self.bob), [])

    # -- only the owner may change it -------------------------------------

    def test_a_non_owner_gets_404_not_403_for_a_deployment_they_cannot_see(self):
        """B holds the share scope but is not the owner.

        The answer is 404, not 403: B cannot see the deployment at all, and a
        403 would turn the endpoint into an oracle for which ids exist.
        """
        response = S.share(self.bob, self.ids["deployment_id"], mode="all")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.error_code(), "registry.not_found")
        self.assertEqual(
            self.h.service.store.endpoint_by_id(self.ids["endpoint_id"]).visibility,
            "private",
        )

    def test_a_session_without_the_share_scope_is_refused_by_the_route(self):
        """The scope is a server-side gate, not a disabled button.

        This used to borrow the user row to manufacture a scope-less session.
        Every member now holds `portal:endpoints:share` by construction (sharing
        *your own* endpoint is the point of being a member), so no session can
        reach the gate that way — the gate is driven directly instead. What is
        asserted is unchanged: no scope, no share, and nothing altered.
        """
        from dataclasses import replace
        from unittest import mock

        client = self.h.client("dave")
        stripped = replace(
            self.h.app.resolve_session(self.h.session_id("dave")),
            portal_scopes=(),
            effective_role="no_such_role",
        )
        with mock.patch.object(self.h.app, "_principal", return_value=stripped):
            response = S.share(client, self.ids["deployment_id"], mode="all")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "registry.access_denied")
        self.assertEqual(
            self.h.service.store.endpoint_by_id(self.ids["endpoint_id"]).visibility,
            "private",
        )

    # -- the admin view ---------------------------------------------------

    def test_an_administrator_sees_the_global_list_only_while_elevated(self):
        """`carol` is the granted Linux administrator, and that is not enough.

        The two requests below come from the same account, over the same route,
        differing only in whether that particular session has entered admin mode.
        """
        admin_ops = S.ops_of(S.fetch_ops(self.h.admin_client("carol")))
        self.assertTrue(admin_ops["admin"]["available"])
        self.assertEqual(len(admin_ops["registry"]["all"]), 1)
        self.assertIn(
            self.h.principal_of("alice"),
            [row["owner_uid"] for row in admin_ops["registry"]["all"]],
        )

        plain_ops = S.ops_of(S.fetch_ops(self.h.client("carol")))
        self.assertFalse(plain_ops["admin"]["available"])
        self.assertEqual(plain_ops["registry"]["all"], [])

        member_ops = S.ops_of(S.fetch_ops(self.bob))
        self.assertFalse(member_ops["admin"]["available"])
        self.assertEqual(member_ops["registry"]["all"], [])
        self.assertEqual(member_ops["admin"]["principals"], [])

    def test_the_admin_view_lists_principals_from_the_trusted_directory(self):
        admin_ops = S.ops_of(S.fetch_ops(self.h.admin_client("carol")))
        listed = {row["uid"] for row in admin_ops["admin"]["principals"]}
        self.assertEqual(
            listed,
            {self.h.principal_of(name) for name in ("carol", "alice", "dave", "bob")},
        )

    # -- the page never lies ----------------------------------------------

    def test_nothing_in_the_member_view_claims_a_gpu_measurement(self):
        for client, visibility in (
            (self.alice, None),
            (self.alice, "shared"),
            (self.h.client("carol"), None),
            (self.h.admin_client("carol"), None),
        ):
            ops = S.ops_of(S.fetch_ops(client, visibility))
            for metric in ops["metrics"]["items"]:
                self.assertIsNone(metric["value"], metric["name"])
                self.assertEqual(metric["state"], "unknown")
                self.assertIsNone(metric["source"])
                self.assertTrue(metric["reason"])
            # 曲线是存在的，但它是合成序列：数值必须有 SYNTHETIC 出处。
            if ops["trends"] is not None:
                self.assertTrue(ops["trends"]["synthetic"])
                self.assertTrue(ops["trends"]["reason"])
            for row in ops["registry"]["owned"]:
                for field in ("gpu_count", "vram_total_mib", "vram_used_mib", "vram_free_mib"):
                    self.assertIsNone(row["resources"][field])
                self.assertIsNone(row["resources"]["source"])

    def test_the_playground_stays_disabled_even_with_the_mock_gateway(self):
        """A test double is not an authorized interactive backend."""
        for name in ("alice", "carol", "bob"):
            ops = S.ops_of(S.fetch_ops(self.h.client(name)))
            self.assertFalse(ops["playground"]["authorized"])
            self.assertIsNone(ops["playground"]["backend"])
            self.assertEqual(ops["playground"]["reason"], bff_ops.REASON_NO_AUTHORIZED_BACKEND)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
