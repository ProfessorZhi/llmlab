"""Two vocabularies, one source: the trusted role.

The front-end reads ``portal:*`` scopes and ignores roles entirely
(``web/js/features/capability.js``); the Registry's row-level ACL reads
``registry:*`` scopes and also ignores roles (``backend/registry/identity.py``).
Both are already published, so the integration layer has to satisfy both — and
the property that matters is that they can never disagree: a session with the
admin view must have the admin read scope, and a session without one must not.
"""

from __future__ import annotations

import unittest

from backend.auth import store as auth_store
from backend.bff import scopes as bff_scopes
from backend.observability import identity as observability_identity
from backend.registry import identity as registry_identity


class VocabularyTests(unittest.TestCase):
    def test_portal_vocabulary_matches_the_browser_module(self):
        """Every scope here must be one `capability.js` knows, and vice versa."""
        source = (
            __import__("pathlib")
            .Path(__file__)
            .resolve()
            .parents[2]
            .joinpath("web/js/features/capability.js")
            .read_text(encoding="utf-8")
        )
        for scope in bff_scopes.PORTAL_KNOWN_SCOPES:
            self.assertIn(f"'{scope}'", source, f"{scope} is not in capability.js")

    def test_registry_vocabulary_is_the_registrys_own(self):
        """Re-using the constant is what stops the two lists drifting."""
        self.assertIs(
            bff_scopes.REGISTRY_KNOWN_SCOPES, registry_identity.KNOWN_SCOPES
        )

    def test_all_four_admin_reads_are_inseparable(self):
        """One decision, four vocabularies.

        A session that can read the deployment-wide view must be able to read the
        global observability overview and the global evaluation queue, and vice
        versa. Four scopes that could disagree is four ways for the admin view to
        show a half-truth.
        """
        from backend.evaluation import identity as evaluation_identity
        from backend.observability import identity as observability_identity

        for role in (*bff_scopes.ROLE_SCOPES, "ghost", None, 7):
            granted = bff_scopes.role_scopes(role)
            truth = {
                bff_scopes.PORTAL_ADMIN_READ in granted.portal,
                registry_identity.SCOPE_ADMIN_READ in granted.registry,
                observability_identity.SCOPE_ADMIN_READ in granted.observability,
                evaluation_identity.SCOPE_EVAL_ADMIN_READ in granted.evaluation,
            }
            self.assertEqual(len(truth), 1, f"admin read disagreed with itself for {role!r}")

    def test_the_observability_and_evaluation_vocabularies_are_theirs(self):
        from backend.evaluation import identity as evaluation_identity
        from backend.observability import identity as observability_identity

        self.assertIs(
            bff_scopes.OBSERVABILITY_KNOWN_SCOPES, observability_identity.ALL_SCOPES
        )
        self.assertIs(
            bff_scopes.EVALUATION_KNOWN_SCOPES, evaluation_identity.KNOWN_SCOPES
        )
        for role in bff_scopes.ROLE_SCOPES.values():
            self.assertTrue(role.observability <= observability_identity.ALL_SCOPES)
            self.assertTrue(role.evaluation <= evaluation_identity.KNOWN_SCOPES)

    def test_no_role_ever_gets_real_dispatch_and_everyone_gets_host_readings(self):
        """Real dispatch stays out of the role table; host readings are for all.

        Real evaluation dispatch is closed by the admission policy *and* by an
        unwired adapter; withholding the scope is the third lock. Host-wide
        GPU/host readings are the lab's shared resource view, so since
        2026-10-21 (owner decision) members carry them too.
        """
        from backend.evaluation import identity as evaluation_identity
        from backend.observability import identity as observability_identity

        member = bff_scopes.role_scopes(auth_store.ROLE_MEMBER)
        admin = bff_scopes.role_scopes(auth_store.ROLE_ADMIN)
        self.assertNotIn(evaluation_identity.SCOPE_EVAL_DISPATCH_REAL, admin.evaluation)
        self.assertNotIn(evaluation_identity.SCOPE_EVAL_DISPATCH_REAL, member.evaluation)
        self.assertNotIn(
            evaluation_identity.SCOPE_EVAL_ADMIN_MANAGE, admin.evaluation
        )
        self.assertIn(observability_identity.SCOPE_HOST_READ, member.observability)
        self.assertIn(observability_identity.SCOPE_HOST_READ, admin.observability)
        self.assertNotIn(observability_identity.SCOPE_ADMIN_READ, member.observability)

    def test_every_portal_implies_mapping_holds(self):
        for portal, registry in bff_scopes.PORTAL_IMPLIES_REGISTRY.items():
            for role in (*bff_scopes.ROLE_SCOPES, "ghost"):
                granted = bff_scopes.role_scopes(role)
                if portal in granted.portal:
                    self.assertIn(
                        registry,
                        granted.registry,
                        f"{role!r} has {portal} but not {registry}",
                    )


class DerivationTests(unittest.TestCase):
    def test_a_member_gets_work_scopes_and_no_admin_view(self):
        member = bff_scopes.role_scopes(auth_store.ROLE_MEMBER)
        self.assertIn(bff_scopes.PORTAL_MODULES_READ, member.portal)
        self.assertIn(bff_scopes.PORTAL_ENDPOINTS_SHARE, member.portal)
        self.assertNotIn(bff_scopes.PORTAL_ADMIN_READ, member.portal)
        self.assertIn(registry_identity.SCOPE_REGISTRY_WRITE, member.registry)
        self.assertNotIn(registry_identity.SCOPE_ADMIN_READ, member.registry)

    def test_an_admin_gets_the_global_view_and_still_not_manage(self):
        """Being an administrator means seeing everything, not editing it.

        Sharing and unsharing stay owner-only unless a deployment separately
        grants `registry:admin:manage`. Keeping that out of the role is what
        stops the word "admin" from quietly handing out write access.
        """
        admin = bff_scopes.role_scopes(auth_store.ROLE_ADMIN)
        self.assertIn(bff_scopes.PORTAL_ADMIN_READ, admin.portal)
        self.assertIn(registry_identity.SCOPE_ADMIN_READ, admin.registry)
        self.assertNotIn(registry_identity.SCOPE_ADMIN_MANAGE, admin.registry)

    def test_an_unknown_role_grants_nothing(self):
        for role in ("root", "superuser", "", None, 42, ["platform_admin"]):
            granted = bff_scopes.role_scopes(role)
            self.assertEqual(granted.portal, frozenset())
            self.assertEqual(granted.registry, frozenset())
            self.assertEqual(granted.observability, frozenset())
            self.assertEqual(granted.evaluation, frozenset())

    def test_fail_closed_is_a_value_not_a_hope(self):
        self.assertEqual(bff_scopes.role_scopes("nonsense"), bff_scopes.NO_SCOPES)
        self.assertFalse(bff_scopes.is_platform_admin(elevated=False))
        self.assertTrue(bff_scopes.is_platform_admin(elevated=True))


class ElevationIsTheOnlyPromotionTests(unittest.TestCase):
    """The 2026-10-15 fix, pinned.

    Before it, ``role_scopes`` was fed the user table's ``role``, so ``carol`` —
    whose Linux-side platform administrator grant writes ``platform_admin`` into
    that column — got the global read view from a plain PAM sign-in. Login was
    deciding what he could see, which is exactly what the V6 model separates.
    """

    def test_a_stored_platform_admin_role_grants_nothing_on_its_own(self):
        promoted = bff_scopes.session_scopes(elevated=False)
        self.assertNotIn(bff_scopes.PORTAL_ADMIN_READ, promoted.portal)
        self.assertNotIn(registry_identity.SCOPE_ADMIN_READ, promoted.registry)

    def test_the_stored_role_is_never_an_input_to_the_decision(self):
        """``session_scopes`` takes elevation and nothing else.

        Asserted against the signature, because the failure mode being fixed was
        a *caller* passing the user row's role in. A parameter that no longer
        exists cannot be passed by accident.
        """
        import inspect

        parameters = inspect.signature(bff_scopes.session_scopes).parameters
        self.assertEqual(list(parameters), ["elevated"])

    def test_elevation_is_the_only_thing_that_opens_the_admin_view(self):
        self.assertFalse(bff_scopes.is_platform_admin(auth_store.ROLE_ADMIN))
        self.assertFalse(bff_scopes.is_platform_admin(auth_store.ROLE_MEMBER))
        self.assertTrue(bff_scopes.is_platform_admin(elevated=True))

    def test_host_readings_are_shared_but_the_admin_view_is_not(self):
        """Every session sees host readings; only elevation adds the admin view."""
        member = bff_scopes.session_scopes(elevated=False)
        elevated = bff_scopes.session_scopes(elevated=True)
        self.assertIn(observability_identity.SCOPE_HOST_READ, member.observability)
        self.assertIn(observability_identity.SCOPE_HOST_READ, elevated.observability)
        self.assertNotIn(observability_identity.SCOPE_ADMIN_READ, member.observability)
        self.assertIn(observability_identity.SCOPE_ADMIN_READ, elevated.observability)

    def test_the_effective_role_is_derived_from_elevation_alone(self):
        self.assertEqual(
            bff_scopes.session_role(elevated=False), auth_store.ROLE_MEMBER
        )
        self.assertEqual(bff_scopes.session_role(elevated=True), auth_store.ROLE_ADMIN)

    def test_scopes_are_returned_sorted_and_deduplicated(self):
        portal = bff_scopes.portal_scopes_for(auth_store.ROLE_ADMIN)
        self.assertEqual(list(portal), sorted(portal))
        self.assertEqual(len(portal), len(set(portal)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
