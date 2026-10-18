"""Share modes and grantee resolution.

The user types a free-text grantee. Whatever form they use, what lands in the
ACL is a **principal** — never a uid, never a login name, never anything the
request asserted about itself. And a name that is not an account resolves to
nothing: there is no alias table, so `dana` cannot become `bob`.
"""

from __future__ import annotations

import tempfile
import unittest

from backend.registry import errors as registry_errors
from backend.registry.identity import Principal, PrincipalDirectory
from backend.registry.models import (
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
)
from backend.bff import sharing as bff_sharing
from tests.bff import support as S

HOST = "dell-test-01"


def directory() -> PrincipalDirectory:
    from backend.auth_linux.handoff import linux_user_id

    return PrincipalDirectory(
        [
            Principal(linux_user_id(HOST, 4000), "carol", 4000),
            Principal(linux_user_id(HOST, 4001), "alice", 4001),
            Principal(linux_user_id(HOST, 4003), "bob", 4003),
            Principal(linux_user_id(HOST, 1099), "suspended", 1099, state="suspended"),
        ]
    )


class ModeTranslationTests(unittest.TestCase):
    def test_the_three_modes_translate_both_ways(self):
        self.assertEqual(bff_sharing.portal_to_registry_mode("private"), VISIBILITY_PRIVATE)
        self.assertEqual(bff_sharing.portal_to_registry_mode("selected"), VISIBILITY_USERS)
        self.assertEqual(
            bff_sharing.portal_to_registry_mode("all"), VISIBILITY_ALL_APPROVED
        )
        for portal in ("private", "selected", "all"):
            registry = bff_sharing.portal_to_registry_mode(portal)
            self.assertEqual(bff_sharing.registry_to_portal_mode(registry), portal)

    def test_an_unknown_mode_is_refused(self):
        for bad in ("public", "PRIVATE", "", None, 1, ["private"]):
            with self.assertRaises(registry_errors.InvalidRequest, msg=repr(bad)):
                bff_sharing.portal_to_registry_mode(bad)

    def test_the_narrowest_share_is_a_lower_bound(self):
        """One row, several endpoints: never claim more than the narrowest."""
        private = {"mode": "private", "principals": [], "revocable": True}
        selected = {"mode": "selected", "principals": ["a", "b"], "revocable": True}
        other = {"mode": "selected", "principals": ["b"], "revocable": True}
        every = {"mode": "all", "principals": [], "revocable": True}

        self.assertEqual(bff_sharing.narrowest_share([])["mode"], "private")
        self.assertEqual(bff_sharing.narrowest_share([private, every])["mode"], "private")
        self.assertEqual(bff_sharing.narrowest_share([selected, every])["mode"], "selected")
        # The intersection, not the union: only `b` is on every selected endpoint.
        self.assertEqual(
            bff_sharing.narrowest_share([selected, other])["principals"], ["b"]
        )
        # An empty intersection must not be reported as "selected" with nobody.
        self.assertEqual(
            bff_sharing.narrowest_share(
                [selected, {"mode": "selected", "principals": ["c"], "revocable": True}]
            )["mode"],
            "private",
        )


class GranteeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.principals = directory()
        self.owner = f"linux:{HOST}:4001"

    def resolve(self, token):
        return bff_sharing.resolve_grantee(
            token, principals=self.principals, owner_principal=self.owner
        )

    def test_a_uid_resolves_to_a_principal(self):
        self.assertEqual(self.resolve("4003"), f"linux:{HOST}:4003")
        self.assertEqual(self.resolve(" 4000 "), f"linux:{HOST}:4000")

    def test_a_principal_name_passes_through(self):
        self.assertEqual(self.resolve(f"linux:{HOST}:4003"), f"linux:{HOST}:4003")

    def test_an_exact_linux_account_name_resolves(self):
        self.assertEqual(self.resolve("bob"), f"linux:{HOST}:4003")
        self.assertEqual(self.resolve("carol"), f"linux:{HOST}:4000")

    def test_an_alias_is_not_invented(self):
        """`dana` is not `bob`. Nothing here folds one into the other."""
        for token in ("dana", "Dana", "bob1", "dana.dong", "local:dana"):
            with self.assertRaises(registry_errors.InvalidRequest, msg=token):
                self.resolve(token)

    def test_an_unknown_uid_is_refused_rather_than_created(self):
        for token in ("9999", "0", "1005"):
            with self.assertRaises(registry_errors.InvalidRequest, msg=token):
                self.resolve(token)

    def test_a_suspended_principal_is_refused(self):
        """Suspended principals are absent from the approved index entirely."""
        with self.assertRaises(registry_errors.InvalidRequest) as caught:
            self.resolve("1099")
        self.assertIn("approved", str(caught.exception))
        # And by name too, so the refusal does not depend on how it was spelled.
        with self.assertRaises(registry_errors.InvalidRequest):
            self.resolve(f"linux:{HOST}:1099")

    def test_sharing_with_yourself_is_refused(self):
        for token in ("4001", self.owner, "alice"):
            with self.assertRaises(registry_errors.InvalidRequest) as caught:
                self.resolve(token)
            self.assertIn("owner", str(caught.exception))

    def test_junk_is_refused(self):
        for token in ("", "   ", None, 4003, ["4003"], "linux:", "1e4"):
            with self.assertRaises(registry_errors.InvalidRequest, msg=repr(token)):
                self.resolve(token)

    def test_resolution_is_deduplicated_and_sorted(self):
        resolved = bff_sharing.resolve_grantees(
            ["4003", f"linux:{HOST}:4003", "bob", "4000"],
            principals=self.principals,
            owner_principal=self.owner,
        )
        self.assertEqual(resolved, (f"linux:{HOST}:4000", f"linux:{HOST}:4003"))

    def test_none_resolves_to_nothing(self):
        self.assertEqual(
            bff_sharing.resolve_grantees(
                None, principals=self.principals, owner_principal=self.owner
            ),
            (),
        )


class ShareRouteTests(unittest.TestCase):
    """The same rules, exercised through the route rather than the helper."""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.alice = self.h.client("alice")
        self.ids = S.register_baseline(self.h, "alice")

    def test_a_login_name_and_a_uid_produce_the_same_acl(self):
        first = S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["bob"])
        self.assertEqual(first.status, 200, first.body)
        by_name = self.h.service.store.grantees(self.ids["endpoint_id"])

        S.share(self.alice, self.ids["deployment_id"], mode="private")
        second = S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["4003"])
        self.assertEqual(second.status, 200, second.body)
        by_uid = self.h.service.store.grantees(self.ids["endpoint_id"])

        self.assertEqual(by_name, by_uid, "the two spellings must reach one ACL")

    def test_selected_with_no_principals_is_refused(self):
        response = S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=[])
        self.assertEqual(response.status, 400)

    def test_an_unknown_grantee_is_refused_and_changes_nothing(self):
        response = S.share(self.alice, self.ids["deployment_id"], mode="selected", principals=["dana"])
        self.assertEqual(response.status, 400)
        self.assertEqual(
            self.h.service.store.endpoint_by_id(self.ids["endpoint_id"]).visibility,
            "private",
        )

    def test_an_unknown_mode_is_refused(self):
        response = S.share(self.alice, self.ids["deployment_id"], mode="public")
        self.assertEqual(response.status, 400)

    def test_sharing_a_deployment_with_no_endpoint_is_404_not_an_implicit_create(self):
        """Registering metadata and sharing it are separate acts."""
        requester = self.h.requester_for("alice")
        engine, _ = self.h.service.register_engine(
            requester, {"name": "eng-b", "target_url": S.ENGINE_TARGET_URL}
        )
        model, _ = self.h.service.register_model(requester, {"name": "model-b"})
        deployment, _ = self.h.service.register_deployment(
            requester,
            {"name": "dep-b", "engine_id": engine["data"]["id"], "model_id": model["data"]["id"]},
        )
        response = S.share(self.alice, deployment["data"]["id"], mode="all")
        self.assertEqual(response.status, 404)
        # No endpoint was conjured up to satisfy the request.
        self.assertEqual(
            [e for e in self.h.service.store.list_endpoints() if e.deployment_id == deployment["data"]["id"]],
            [],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
