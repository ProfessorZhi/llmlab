"""Scoped-token service: the security properties, one test each.

Everything here runs offline against a throwaway SQLite file. No real
credential, password, PAM configuration or network access is involved: the
principals are synthetic, and the tokens are minted and discarded inside the
test process.
"""

import pathlib
import shutil
import sqlite3
import tempfile
import unittest

from backend.auth import errors, tokens
from backend.auth.tokens import (
    REASON_ACTIVE,
    REASON_EXPIRED,
    REASON_REVOKED,
    REASON_SUSPENDED,
    REASON_UNKNOWN,
    REASON_UNKNOWN_PRINCIPAL,
    STATE_APPROVED,
    STATE_SUSPENDED,
    MappingPrincipalState,
    ScopedTokenService,
    TokenStore,
)
from tests.auth.test_store import FakeClock

LINUX_PRINCIPAL = "linux:llm-lab-dell:1001"
LINUX_UID = 1001
LINUX_USER = "carol"
OTHER_PRINCIPAL = "linux:llm-lab-dell:4001"


class TokenTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-tokens-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.store = TokenStore(self.tmp / "scoped-tokens.sqlite3", clock=self.clock)
        # The trusted directory is a required collaborator, and a principal it
        # does not know is refused. Tests therefore state their principal set
        # explicitly rather than relying on a permissive default.
        self.directory = MappingPrincipalState({LINUX_PRINCIPAL: STATE_APPROVED})
        self.service = ScopedTokenService(
            self.store, principal_state=self.directory, clock=self.clock
        )
        self.service.initialize()

    # --- helpers -------------------------------------------------------------

    def approve(self, principal: str) -> None:
        self.directory.approve(principal)

    def issue(
        self,
        *,
        principal=LINUX_PRINCIPAL,
        uid=LINUX_UID,
        linux_user=LINUX_USER,
        scopes=("registry:read",),
        roles=("member",),
        label=None,
        ttl_seconds=None,
    ):
        return self.service.issue(
            principal=principal,
            uid=uid,
            linux_user=linux_user,
            scopes=scopes,
            roles=roles,
            label=label,
            ttl_seconds=ttl_seconds,
        )

    def rotate(self, token_id, *, allowed=("registry:read",), principal=LINUX_PRINCIPAL, **kw):
        return self.service.rotate_for_principal(
            principal, token_id, allowed_scopes=allowed, **kw
        )

    def stored_bytes(self) -> bytes:
        """Every byte the store has written, WAL sidecars included."""
        blob = b""
        for path in sorted(self.tmp.iterdir()):
            if path.is_file():
                blob += path.read_bytes()
        return blob


# --- the plaintext exists exactly once ---------------------------------------


class PlaintextDisciplineTests(TokenTestCase):
    def test_the_plaintext_is_never_written_to_disk(self):
        issued = self.issue()
        # The token is long and high-entropy; if it were stored verbatim it
        # would appear in the database bytes. Only its digest should.
        self.assertNotIn(issued.token.encode("utf-8"), self.stored_bytes())
        self.assertIn(
            tokens.digest_token(issued.token).encode("utf-8"), self.stored_bytes()
        )

    def test_listing_never_returns_a_plaintext(self):
        issued = self.issue(label="cli on dell")
        records = self.service.list_for_principal(LINUX_PRINCIPAL)
        self.assertEqual(len(records), 1)
        rendered = repr(records[0])
        self.assertNotIn(issued.token, rendered)
        self.assertNotIn("token", records[0].to_public_dict())

    def test_introspection_never_returns_a_plaintext(self):
        issued = self.issue()
        view = self.service.introspect(issued.token)
        self.assertNotIn(issued.token, repr(view))
        self.assertNotIn("token", view.to_registry_dict())

    def test_repr_of_an_issued_token_redacts_the_secret(self):
        issued = self.issue()
        self.assertNotIn(issued.token, repr(issued))
        self.assertIn(issued.token_id, repr(issued))

    def test_two_tokens_for_the_same_principal_are_different(self):
        first = self.issue()
        second = self.issue()
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.token_id, second.token_id)

    def test_a_token_carries_at_least_256_bits_of_entropy(self):
        issued = self.issue()
        self.assertTrue(issued.token.startswith(tokens.TOKEN_PREFIX))
        body = issued.token[len(tokens.TOKEN_PREFIX) :]
        # token_urlsafe(32) is 43 base64 characters; anything shorter would mean
        # the entropy floor had been lowered.
        self.assertGreaterEqual(len(body), 43)


# --- introspection is a refusal on every negative path -----------------------


class IntrospectionTests(TokenTestCase):
    def test_a_live_token_resolves_to_its_recorded_principal(self):
        issued = self.issue(scopes=("registry:read", "registry:write"), label="laptop")
        view = self.service.introspect(issued.token)
        self.assertTrue(view.active)
        self.assertEqual(view.reason, REASON_ACTIVE)
        self.assertEqual(view.principal, LINUX_PRINCIPAL)
        self.assertEqual(view.uid, LINUX_UID)
        self.assertEqual(view.linux_user, LINUX_USER)
        self.assertEqual(view.scopes, frozenset({"registry:read", "registry:write"}))
        self.assertEqual(view.roles, frozenset({"member"}))
        self.assertEqual(view.label, "laptop")
        self.assertEqual(view.token_id, issued.token_id)

    def test_the_registry_view_is_the_documented_shape(self):
        issued = self.issue()
        payload = self.service.introspect(issued.token).to_registry_dict()
        self.assertEqual(payload["principal"], LINUX_PRINCIPAL)
        self.assertEqual(payload["uid"], LINUX_UID)
        self.assertEqual(payload["linux_user"], LINUX_USER)
        self.assertEqual(payload["state"], STATE_APPROVED)
        self.assertTrue(payload["approved"])
        self.assertEqual(payload["scopes"], ["registry:read"])

    def test_an_unknown_token_is_inactive(self):
        view = self.service.introspect(tokens.TOKEN_PREFIX + "not-a-real-token")
        self.assertFalse(view.active)
        self.assertEqual(view.reason, REASON_UNKNOWN)
        self.assertIsNone(view.principal)

    def test_a_string_that_is_not_a_token_is_refused_without_a_lookup(self):
        for candidate in ("", "   ", "secret", "Bearer abc", 12345, None, b"llmlab_x"):
            with self.subTest(candidate=repr(candidate)):
                self.assertEqual(self.service.introspect(candidate).reason, REASON_UNKNOWN)

    def test_a_revoked_token_is_inactive(self):
        issued = self.issue()
        self.assertTrue(self.service.revoke(issued.token))
        view = self.service.introspect(issued.token)
        self.assertFalse(view.active)
        self.assertEqual(view.reason, REASON_REVOKED)

    def test_an_expired_token_is_inactive(self):
        issued = self.issue(ttl_seconds=tokens.MIN_TTL_SECONDS)
        self.assertTrue(self.service.introspect(issued.token).active)
        self.clock.advance(tokens.MIN_TTL_SECONDS)
        view = self.service.introspect(issued.token)
        self.assertFalse(view.active)
        self.assertEqual(view.reason, REASON_EXPIRED)

    def test_expiry_boundary_is_exclusive(self):
        issued = self.issue(ttl_seconds=tokens.MIN_TTL_SECONDS)
        self.clock.advance(tokens.MIN_TTL_SECONDS - 1)
        self.assertTrue(self.service.introspect(issued.token).active)
        self.clock.advance(1)
        self.assertFalse(self.service.introspect(issued.token).active)

    def test_introspection_records_last_use(self):
        issued = self.issue()
        self.clock.advance(30)
        self.service.introspect(issued.token)
        record = self.service.list_for_principal(LINUX_PRINCIPAL)[0]
        self.assertEqual(record.last_used_at, self.clock.now)

    def test_a_disabled_principal_makes_an_already_issued_token_inactive(self):
        # BLOCKER B: issue while approved, then disable the account. The stored
        # row still says "approved" — the live directory is what decides.
        issued = self.issue()
        self.assertTrue(self.service.introspect(issued.token).active)

        self.directory.suspend(LINUX_PRINCIPAL)

        view = self.service.introspect(issued.token)
        self.assertFalse(view.active)
        self.assertEqual(view.reason, REASON_SUSPENDED)
        # ...and the row is untouched: no revocation step was needed.
        record = self.service.list_for_principal(LINUX_PRINCIPAL)[0]
        self.assertEqual(record.state, STATE_APPROVED)
        self.assertIsNone(record.revoked_at)

    def test_the_live_state_overrides_a_stale_snapshot(self):
        # Even if the stored snapshot is doctored to say "approved", a suspended
        # directory entry still refuses. The row is history, never authority.
        issued = self.issue()
        self._force_state(issued.token_id, STATE_APPROVED)
        self.directory.suspend(LINUX_PRINCIPAL)
        self.assertFalse(self.service.introspect(issued.token).active)

    def test_a_principal_missing_from_the_directory_is_inactive(self):
        # No unknown-principal fallback: not being in the directory is a refusal,
        # not a licence to treat the token as anonymous or as still approved.
        issued = self.issue()
        self.directory.forget(LINUX_PRINCIPAL)
        view = self.service.introspect(issued.token)
        self.assertFalse(view.active)
        self.assertEqual(view.reason, REASON_UNKNOWN_PRINCIPAL)

    def test_reactivating_a_principal_restores_its_token(self):
        # The check is live in both directions: re-approving makes the token
        # usable again, which is what "the directory is the authority" means.
        issued = self.issue()
        self.directory.suspend(LINUX_PRINCIPAL)
        self.assertFalse(self.service.introspect(issued.token).active)
        self.directory.approve(LINUX_PRINCIPAL)
        view = self.service.introspect(issued.token)
        self.assertTrue(view.active)
        self.assertEqual(view.state, STATE_APPROVED)

    def test_the_registry_view_reports_the_live_state(self):
        issued = self.issue()
        self.directory.suspend(LINUX_PRINCIPAL)
        self.assertFalse(self.service.introspect(issued.token).active)
        self.directory.approve(LINUX_PRINCIPAL)
        self.assertTrue(self.service.introspect(issued.token).to_registry_dict()["approved"])

    def _force_state(self, token_id: str, state: str) -> None:
        conn = sqlite3.connect(str(self.store.path))
        try:
            conn.execute(
                "UPDATE scoped_tokens SET state = ? WHERE token_id = ?", (state, token_id)
            )
            conn.commit()
        finally:
            conn.close()


# --- revocation ---------------------------------------------------------------


class RevocationTests(TokenTestCase):
    def test_revoke_by_presenting_the_token(self):
        issued = self.issue()
        self.assertTrue(self.service.revoke(issued.token))
        self.assertFalse(self.service.introspect(issued.token).active)
        # Revoking twice is a no-op, not an error.
        self.assertTrue(self.service.revoke(issued.token))

    def test_revoking_an_unknown_token_reports_false(self):
        self.assertFalse(self.service.revoke(tokens.TOKEN_PREFIX + "nope"))
        self.assertFalse(self.service.revoke("not-a-token"))
        self.assertFalse(self.service.revoke(None))

    def test_a_principal_may_revoke_its_own_token_by_id(self):
        issued = self.issue()
        self.assertTrue(self.service.revoke_for_principal(LINUX_PRINCIPAL, issued.token_id))
        self.assertFalse(self.service.introspect(issued.token).active)

    def test_revoking_someone_elses_token_is_forbidden(self):
        # The cross-UID case: principal4001 must not be able to revoke 1001's
        # credential, and the refusal is a Forbidden rather than a quiet False.
        issued = self.issue()
        other = "linux:llm-lab-dell:4001"
        with self.assertRaises(errors.Forbidden):
            self.service.revoke_for_principal(other, issued.token_id)
        # The token must still be live after the refused attempt.
        self.assertTrue(self.service.introspect(issued.token).active)

    def test_revoking_an_unknown_id_reports_false(self):
        self.assertFalse(self.service.revoke_for_principal(LINUX_PRINCIPAL, "no-such-id"))

    def test_revoke_all_is_scoped_to_one_principal(self):
        self.approve(OTHER_PRINCIPAL)
        mine_first = self.issue()
        mine_second = self.issue()
        theirs = self.issue(principal=OTHER_PRINCIPAL, uid=4001, linux_user="other")
        removed = self.service.revoke_all_for_principal(LINUX_PRINCIPAL)
        self.assertEqual(removed, 2)
        self.assertFalse(self.service.introspect(mine_first.token).active)
        self.assertFalse(self.service.introspect(mine_second.token).active)
        # Another principal's token is untouched.
        self.assertTrue(self.service.introspect(theirs.token).active)


# --- rotation -----------------------------------------------------------------


class RotationTests(TokenTestCase):
    def test_rotation_revokes_the_old_token_and_issues_a_new_one(self):
        issued = self.issue()
        rotated = self.rotate(issued.token_id)
        self.assertNotEqual(rotated.token, issued.token)
        self.assertFalse(self.service.introspect(issued.token).active)
        self.assertEqual(self.service.introspect(issued.token).reason, REASON_REVOKED)
        self.assertTrue(self.service.introspect(rotated.token).active)

    def test_rotation_preserves_the_authority_of_the_original(self):
        issued = self.issue(scopes=("registry:read", "registry:write"), label="laptop")
        rotated = self.rotate(
            issued.token_id, allowed=("registry:read", "registry:write")
        )
        view = self.service.introspect(rotated.token)
        self.assertEqual(view.scopes, issued.scopes)
        self.assertEqual(view.label, "laptop")
        self.assertEqual(view.principal, LINUX_PRINCIPAL)
        self.assertEqual(view.uid, LINUX_UID)

    def test_rotation_links_the_new_token_to_the_old_id(self):
        issued = self.issue()
        rotated = self.rotate(issued.token_id)
        records = {r.token_id: r for r in self.service.list_for_principal(LINUX_PRINCIPAL)}
        self.assertEqual(records[rotated.token_id].rotation_of, issued.token_id)
        self.assertIsNotNone(records[issued.token_id].rotated_at)

    def test_rotating_someone_elses_token_is_forbidden(self):
        issued = self.issue()
        self.approve(OTHER_PRINCIPAL)
        with self.assertRaises(errors.Forbidden):
            self.rotate(issued.token_id, principal=OTHER_PRINCIPAL)
        self.assertTrue(self.service.introspect(issued.token).active)

    def test_rotating_an_unknown_token_is_a_client_error(self):
        with self.assertRaises(errors.InvalidRequest):
            self.rotate("no-such-id")

    def test_rotation_can_shorten_the_lifetime(self):
        issued = self.issue()
        rotated = self.rotate(issued.token_id, ttl_seconds=tokens.MIN_TTL_SECONDS)
        self.assertEqual(rotated.expires_at - rotated.issued_at, tokens.MIN_TTL_SECONDS)

    # --- BLOCKER A: a dead or downgraded token is not a ticket to a new one ---

    def test_a_revoked_token_cannot_be_rotated(self):
        issued = self.issue()
        self.service.revoke(issued.token)
        with self.assertRaises(errors.NotAuthenticated):
            self.rotate(issued.token_id)
        # The refusal must not have quietly minted anything.
        self.assertEqual(len(self.service.list_for_principal(LINUX_PRINCIPAL)), 1)

    def test_a_token_the_principal_revoked_by_id_cannot_be_rotated(self):
        issued = self.issue()
        self.assertTrue(self.service.revoke_for_principal(LINUX_PRINCIPAL, issued.token_id))
        with self.assertRaises(errors.NotAuthenticated):
            self.rotate(issued.token_id)

    def test_an_expired_token_cannot_be_rotated(self):
        issued = self.issue(ttl_seconds=tokens.MIN_TTL_SECONDS)
        self.clock.advance(tokens.MIN_TTL_SECONDS)
        with self.assertRaises(errors.NotAuthenticated):
            self.rotate(issued.token_id)
        self.assertEqual(len(self.service.list_for_principal(LINUX_PRINCIPAL)), 1)

    def test_a_disabled_principal_cannot_rotate(self):
        issued = self.issue()
        self.directory.suspend(LINUX_PRINCIPAL)
        with self.assertRaises(errors.Forbidden):
            self.rotate(issued.token_id)

    def test_a_principal_missing_from_the_directory_cannot_rotate(self):
        issued = self.issue()
        self.directory.forget(LINUX_PRINCIPAL)
        with self.assertRaises(errors.Forbidden):
            self.rotate(issued.token_id)

    def test_a_downgraded_session_cannot_reissue_its_old_wider_token(self):
        # The headline case: an administrator-scoped token exists, the session's
        # policy is then reduced, and rotating must NOT hand the wider scope back.
        issued = self.issue(scopes=("registry:read", "registry:admin:manage"))
        with self.assertRaises(errors.Forbidden) as caught:
            self.rotate(issued.token_id, allowed=("registry:read",))
        self.assertIn("registry:admin:manage", caught.exception.detail)
        # Nothing was minted, and the old token is untouched (still revocable).
        self.assertEqual(len(self.service.list_for_principal(LINUX_PRINCIPAL)), 1)
        self.assertTrue(self.service.introspect(issued.token).active)

    def test_rotation_cannot_add_a_scope_even_if_the_policy_allows_it(self):
        # Widening is what issue() is for; rotation narrows or preserves only.
        issued = self.issue(scopes=("registry:read",))
        with self.assertRaises(errors.Forbidden) as caught:
            self.rotate(
                issued.token_id,
                allowed=("registry:read", "registry:write"),
                scopes=("registry:read", "registry:write"),
            )
        self.assertIn("cannot add scopes", caught.exception.detail)

    def test_rotation_may_narrow_explicitly(self):
        issued = self.issue(scopes=("registry:read", "registry:write"))
        rotated = self.rotate(
            issued.token_id,
            allowed=("registry:read",),
            scopes=("registry:read",),
        )
        self.assertEqual(rotated.scopes, frozenset({"registry:read"}))
        self.assertFalse(self.service.introspect(issued.token).active)

    def test_a_stale_role_snapshot_cannot_restore_privilege(self):
        # The old token was minted for a platform_admin. Rotating it after the
        # role has gone must not carry the old scope back into a live token.
        issued = self.issue(
            scopes=("registry:read", "registry:admin:manage"),
            roles=("platform_admin",),
        )
        with self.assertRaises(errors.Forbidden):
            self.rotate(issued.token_id, allowed=("registry:read",))
        remaining = self.service.list_for_principal(LINUX_PRINCIPAL)
        self.assertEqual(len(remaining), 1)
        self.assertIsNone(remaining[0].rotated_at)

    def test_allowed_scopes_is_mandatory(self):
        # Omitting the current policy must be a programming error, never a
        # permissive default.
        issued = self.issue()
        with self.assertRaises(TypeError):
            self.service.rotate_for_principal(LINUX_PRINCIPAL, issued.token_id)


# --- issuance policy ----------------------------------------------------------


class IssuancePolicyTests(TokenTestCase):
    def test_a_malformed_scope_is_refused(self):
        for bad in ("registry", "Registry:Read", "registry:", ":read", "a b:c", "x" * 40 + ":y"):
            with self.subTest(scope=bad):
                with self.assertRaises(errors.InvalidRequest):
                    self.issue(scopes=(bad,))

    def test_an_empty_scope_set_is_refused(self):
        with self.assertRaises(errors.InvalidRequest):
            self.issue(scopes=())

    def test_a_bare_string_is_not_a_scope_collection(self):
        with self.assertRaises(errors.InvalidRequest):
            self.issue(scopes="registry:read")

    def test_duplicate_scopes_collapse(self):
        issued = self.issue(scopes=("registry:read", "registry:read"))
        self.assertEqual(issued.scopes, frozenset({"registry:read"}))

    def test_ttl_must_be_a_sane_integer(self):
        with self.assertRaises(errors.InvalidRequest):
            self.issue(ttl_seconds=tokens.MIN_TTL_SECONDS - 1)
        with self.assertRaises(errors.InvalidRequest):
            self.issue(ttl_seconds=tokens.MAX_TTL_SECONDS + 1)
        with self.assertRaises(errors.InvalidRequest):
            self.issue(ttl_seconds="3600")
        with self.assertRaises(errors.InvalidRequest):
            self.issue(ttl_seconds=True)

    def test_a_disabled_principal_gets_no_token(self):
        self.directory.suspend(LINUX_PRINCIPAL)
        with self.assertRaises(errors.Forbidden):
            self.issue(scopes=("registry:read",))

    def test_a_principal_outside_the_directory_gets_no_token(self):
        # No unknown-principal fallback on the issuance path either.
        self.directory.forget(LINUX_PRINCIPAL)
        with self.assertRaises(errors.Forbidden):
            self.issue(scopes=("registry:read",))

    def test_a_service_without_a_directory_is_not_constructible(self):
        # The collaborator is mandatory: there is no permissive default that
        # would let a deployment silently skip the live-state check.
        with self.assertRaises(errors.ConfigError):
            ScopedTokenService(self.store, principal_state=None)
        with self.assertRaises(errors.ConfigError):
            ScopedTokenService(self.store, principal_state=object())

    def test_principal_must_be_a_non_empty_string(self):
        for bad in ("", "   ", None, 1001):
            with self.subTest(principal=repr(bad)):
                with self.assertRaises(errors.InvalidRequest):
                    self.issue(principal=bad)

    def test_a_control_character_in_the_principal_is_refused(self):
        with self.assertRaises(errors.InvalidRequest):
            self.issue(principal="linux:host:1001\nx")

    def test_a_label_is_cleaned_and_bounded(self):
        self.assertEqual(self.issue(label="  cli\n").label, "cli")
        self.assertIsNone(self.issue(label="   ").label)
        self.assertIsNone(self.issue(label=None).label)
        with self.assertRaises(errors.InvalidRequest):
            self.issue(label="x" * (tokens.MAX_LABEL_LENGTH + 1))
        with self.assertRaises(errors.InvalidRequest):
            self.issue(label=42)

    def test_a_role_cannot_be_used_to_smuggle_control_characters(self):
        with self.assertRaises(errors.InvalidRequest):
            self.issue(roles=("member\nx",))

    def test_a_role_grants_no_scope_by_itself(self):
        # The point of the previous test is that roles are inert metadata: an
        # administrator role must not widen the scope set that was asked for.
        issued = self.issue(roles=("platform_admin",), scopes=("registry:read",))
        self.assertEqual(issued.scopes, frozenset({"registry:read"}))
        self.assertEqual(issued.roles, frozenset({"platform_admin"}))


# --- rate limits --------------------------------------------------------------


class RateLimitTests(TokenTestCase):
    def test_a_principal_cannot_hold_more_than_the_active_cap(self):
        for _ in range(tokens.MAX_ACTIVE_PER_PRINCIPAL):
            self.issue()
        with self.assertRaises(errors.RateLimited):
            self.issue()

    def test_revoking_frees_a_slot(self):
        issued = [self.issue() for _ in range(tokens.MAX_ACTIVE_PER_PRINCIPAL)]
        self.service.revoke(issued[0].token)
        # One fewer live token: another issuance fits.
        self.issue()

    def test_revoking_does_not_reset_the_issuance_window(self):
        # Revoking is not a way to mint an unlimited number of tokens: the
        # window cap counts creations, not live tokens.
        created = []
        for _ in range(tokens.MAX_ISSUANCE_PER_WINDOW):
            created.append(self.issue())
            self.service.revoke(created[-1].token)
        with self.assertRaises(errors.RateLimited) as caught:
            self.issue()
        self.assertEqual(caught.exception.retry_after, tokens.ISSUANCE_WINDOW_SECONDS)

    def test_the_window_lifts_after_it_passes(self):
        for _ in range(tokens.MAX_ISSUANCE_PER_WINDOW):
            issued = self.issue()
            self.service.revoke(issued.token)
        self.clock.advance(tokens.ISSUANCE_WINDOW_SECONDS + 1)
        self.issue()

    def test_the_cap_is_per_principal(self):
        self.approve(OTHER_PRINCIPAL)
        for _ in range(tokens.MAX_ACTIVE_PER_PRINCIPAL):
            self.issue()
        # A different principal is unaffected by the first one's cap.
        self.issue(principal=OTHER_PRINCIPAL, uid=4001, linux_user="other")


# --- persistence --------------------------------------------------------------


class AuthorizationBoundaryTests(TokenTestCase):
    """What this service does *not* do, asserted so it cannot be assumed away.

    ``introspect()`` answers "is this credential valid, and for whom". It does
    **not** re-authorise scopes: scope rules live with the Registry/Gateway,
    which own the vocabulary and the resource ownership. A test that pins this
    boundary is the difference between a documented gap and a claimed feature.
    """

    def test_a_scope_reduction_is_not_visible_to_introspection(self):
        # The token keeps the scopes it was minted with. Only the consuming
        # service knows the current rule, so only it can enforce a reduction —
        # which is why the Gateway must check live authorization per call and
        # must not treat this scope list as authority.
        issued = self.issue(scopes=("registry:read", "registry:write"))
        view = self.service.introspect(issued.token)
        self.assertTrue(view.active)
        self.assertEqual(
            view.scopes, frozenset({"registry:read", "registry:write"})
        )

    def test_introspection_reports_the_scopes_verbatim_never_widened(self):
        # Whatever a caller passes at issue time is what comes back, unchanged —
        # no inference from role, no merging with anything else.
        issued = self.issue(scopes=("registry:read",), roles=("platform_admin",))
        self.assertEqual(
            self.service.introspect(issued.token).scopes, frozenset({"registry:read"})
        )


class PersistenceTests(TokenTestCase):
    def test_a_token_survives_a_reopened_store(self):
        issued = self.issue(label="durable")
        reopened = ScopedTokenService(
            TokenStore(self.store.path, clock=self.clock),
            principal_state=self.directory,
            clock=self.clock,
        )
        view = reopened.introspect(issued.token)
        self.assertTrue(view.active)
        self.assertEqual(view.label, "durable")

    def test_revocation_survives_a_reopened_store(self):
        issued = self.issue()
        self.service.revoke(issued.token)
        reopened = ScopedTokenService(
            TokenStore(self.store.path, clock=self.clock),
            principal_state=self.directory,
            clock=self.clock,
        )
        self.assertFalse(reopened.introspect(issued.token).active)

    def test_a_reopened_store_still_honours_live_suspension(self):
        # Persistence must not become a way to outlive a disabled account: the
        # reopened service reads the same live directory.
        issued = self.issue()
        self.directory.suspend(LINUX_PRINCIPAL)
        reopened = ScopedTokenService(
            TokenStore(self.store.path, clock=self.clock),
            principal_state=self.directory,
            clock=self.clock,
        )
        self.assertFalse(reopened.introspect(issued.token).active)

    def test_purge_removes_expired_rows(self):
        issued = self.issue(ttl_seconds=tokens.MIN_TTL_SECONDS)
        self.clock.advance(tokens.MIN_TTL_SECONDS + 1)
        removed = self.service.purge_expired(grace_seconds=0)
        self.assertEqual(removed, 1)
        self.assertEqual(self.service.list_for_principal(LINUX_PRINCIPAL), [])

    def test_purge_keeps_a_freshly_revoked_row_for_audit(self):
        issued = self.issue()
        self.service.revoke(issued.token)
        self.assertEqual(self.service.purge_expired(grace_seconds=3600), 0)
        self.assertEqual(len(self.service.list_for_principal(LINUX_PRINCIPAL)), 1)

    def test_the_store_file_is_private(self):
        mode = self.store.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


# --- the principal reference helper ------------------------------------------


class PrincipalRefTests(unittest.TestCase):
    class _User:
        def __init__(self, id, display_name="someone"):
            self.id = id
            self.display_name = display_name

    def test_a_linux_principal_yields_its_uid_and_account_name(self):
        principal, uid, linux_user = tokens.principal_ref(
            self._User("linux:llm-lab-dell:1001", "carol")
        )
        self.assertEqual(principal, "linux:llm-lab-dell:1001")
        self.assertEqual(uid, 1001)
        self.assertEqual(linux_user, "carol")

    def test_a_non_linux_principal_has_no_uid_and_no_linux_name(self):
        for user_id in ("feishu:abc123", "local:deadbeef"):
            with self.subTest(user_id=user_id):
                principal, uid, linux_user = tokens.principal_ref(self._User(user_id, "X"))
                self.assertEqual(principal, user_id)
                self.assertIsNone(uid)
                self.assertIsNone(linux_user)

    def test_a_malformed_linux_id_does_not_invent_a_uid(self):
        for user_id in ("linux:host:not-a-number", "linux::1001", "linux:host:"):
            with self.subTest(user_id=user_id):
                _principal, uid, linux_user = tokens.principal_ref(self._User(user_id))
                self.assertIsNone(uid)
                self.assertIsNone(linux_user)


# --- assembly -----------------------------------------------------------------


class BuildServiceTests(unittest.TestCase):
    def _directory(self):
        return MappingPrincipalState({LINUX_PRINCIPAL: STATE_APPROVED})

    def test_build_token_service_reads_bounded_overrides(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-tokens-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        service = tokens.build_token_service(
            tmp, principal_state=self._directory(), environ={"LLM_LAB_TOKEN_TTL_SECONDS": "120"}
        )
        service.initialize()
        issued = service.issue(
            principal=LINUX_PRINCIPAL,
            uid=LINUX_UID,
            linux_user=LINUX_USER,
            scopes=("registry:read",),
        )
        self.assertEqual(issued.expires_at - issued.issued_at, 120)

    def test_a_non_integer_override_is_a_config_error(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-tokens-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with self.assertRaises(errors.ConfigError):
            tokens.build_token_service(
                tmp,
                principal_state=self._directory(),
                environ={"LLM_LAB_TOKEN_TTL_SECONDS": "soon"},
            )

    def test_principal_state_is_required(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-tokens-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with self.assertRaises(TypeError):
            tokens.build_token_service(tmp)


class AuthStorePrincipalStateTests(unittest.TestCase):
    """The adapter that makes "disabled account" a live question."""

    class _User:
        def __init__(self, active):
            self.is_active = active

    class _Store:
        def __init__(self, users):
            self._users = users

        def get_user(self, user_id):
            return self._users.get(user_id)

    def test_an_active_user_is_approved(self):
        directory = tokens.AuthStorePrincipalState(
            self._Store({LINUX_PRINCIPAL: self._User(active=True)})
        )
        self.assertEqual(directory.current_state(LINUX_PRINCIPAL), STATE_APPROVED)

    def test_a_disabled_user_is_suspended(self):
        directory = tokens.AuthStorePrincipalState(
            self._Store({LINUX_PRINCIPAL: self._User(active=False)})
        )
        self.assertEqual(directory.current_state(LINUX_PRINCIPAL), STATE_SUSPENDED)

    def test_an_unknown_user_is_none_not_approved(self):
        directory = tokens.AuthStorePrincipalState(self._Store({}))
        self.assertIsNone(directory.current_state(LINUX_PRINCIPAL))

    def test_a_user_without_the_attribute_is_refused(self):
        class Bare:
            pass

        directory = tokens.AuthStorePrincipalState(self._Store({LINUX_PRINCIPAL: Bare()}))
        self.assertEqual(directory.current_state(LINUX_PRINCIPAL), STATE_SUSPENDED)

    def test_a_directory_without_get_user_is_a_config_error(self):
        directory = tokens.AuthStorePrincipalState(object())
        with self.assertRaises(errors.ConfigError):
            directory.current_state(LINUX_PRINCIPAL)

    def test_disabling_a_user_in_the_store_deactivates_its_token(self):
        # End to end over the real adapter: issue → disable → introspect inactive.
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-tokens-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        users = {LINUX_PRINCIPAL: self._User(active=True)}
        service = ScopedTokenService(
            TokenStore(tmp / "scoped-tokens.sqlite3"),
            principal_state=tokens.AuthStorePrincipalState(self._Store(users)),
        )
        service.initialize()
        issued = service.issue(
            principal=LINUX_PRINCIPAL,
            uid=LINUX_UID,
            linux_user=LINUX_USER,
            scopes=("registry:read",),
        )
        self.assertTrue(service.introspect(issued.token).active)

        users[LINUX_PRINCIPAL] = self._User(active=False)

        view = service.introspect(issued.token)
        self.assertFalse(view.active)
        self.assertEqual(view.reason, REASON_SUSPENDED)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
