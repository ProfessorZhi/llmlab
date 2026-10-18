"""Tests for backend.auth.store — the persistent state layer."""

import pathlib
import shutil
import sqlite3
import tempfile
import unittest

from backend.auth import store


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-auth-store-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()
        self.db = store.Database(self.tmp / "auth.sqlite3", clock=self.clock)
        self.db.initialize()


class SchemaTests(StoreTestCase):
    def test_initialize_is_idempotent(self):
        self.db.initialize()
        self.db.initialize()

    def test_creates_the_database_file(self):
        self.assertTrue((self.tmp / "auth.sqlite3").exists())

    def test_an_existing_database_is_brought_forward_to_the_binding_column(self):
        """A deployment that ran an earlier build must keep working.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        database written before browser binding existed would have no
        ``binding_hash`` column and the first ``/auth/feishu/start`` would fail
        at runtime. ``initialize()`` has to add it.
        """
        legacy = self.tmp / "legacy.sqlite3"
        conn = sqlite3.connect(str(legacy))
        conn.execute(
            "CREATE TABLE login_states ("
            "state_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, "
            "expires_at REAL NOT NULL, code_verifier TEXT, next_path TEXT)"
        )
        conn.commit()
        conn.close()

        db = store.Database(legacy, clock=self.clock)
        db.initialize()

        conn = sqlite3.connect(str(legacy))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(login_states)")}
        conn.close()
        self.assertIn("binding_hash", columns)

        # And the migrated column is usable straight away.
        db.insert_login_state(
            state_hash="h", expires_at=self.clock() + 60, binding_hash="digest"
        )
        self.assertIsNotNone(db.consume_login_state_bound("h", "digest"))

    def test_an_existing_identity_index_is_rebuilt_over_the_joint_key(self):
        """``CREATE UNIQUE INDEX IF NOT EXISTS`` would silently keep the old one.

        A database written before ``tenant_key`` joined the identity key would
        go on enforcing ``(auth_source, app_id, external_id)`` and reject the
        second tenant's identity for the same person.
        """
        legacy = self.tmp / "legacy-index.sqlite3"
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            "CREATE TABLE users ("
            "id TEXT PRIMARY KEY, auth_source TEXT NOT NULL, external_id TEXT, "
            "app_id TEXT, union_id TEXT, tenant_key TEXT, display_name TEXT NOT NULL, "
            "role TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, "
            "last_login_at REAL);"
            "CREATE UNIQUE INDEX users_external_identity "
            "ON users(auth_source, app_id, external_id);"
        )
        conn.commit()
        conn.close()

        db = store.Database(legacy, clock=self.clock)
        db.initialize()

        conn = sqlite3.connect(str(legacy))
        columns = [row[2] for row in conn.execute("PRAGMA index_info(users_external_identity)")]
        conn.close()
        self.assertEqual(columns, ["auth_source", "app_id", "tenant_key", "external_id"])

        # The whole point: two tenants, one open_id, two distinct people.
        first = db.upsert_feishu_user(
            app_id="cli_a", open_id="ou_1", union_id=None,
            tenant_key="tenant_a", display_name="A",
        )
        second = db.upsert_feishu_user(
            app_id="cli_a", open_id="ou_1", union_id=None,
            tenant_key="tenant_b", display_name="B",
        )
        self.assertNotEqual(first.id, second.id)


class LocalAdminTests(StoreTestCase):
    def test_local_admin_is_created_once_and_is_platform_admin(self):
        first = self.db.ensure_local_user("admin")
        second = self.db.ensure_local_user("admin")
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.role, store.ROLE_ADMIN)
        self.assertEqual(first.auth_source, store.SOURCE_LOCAL)

    def test_local_admin_is_never_created_by_feishu(self):
        """A Feishu identity must never land on the local administrator slot."""
        admin = self.db.ensure_local_user("admin")
        user = self.db.upsert_feishu_user(
            app_id="cli_a",
            open_id="ou_admin",
            union_id="on_admin",
            tenant_key="tenant_a",
            display_name="admin",
        )
        self.assertNotEqual(user.id, admin.id)
        self.assertEqual(user.role, store.ROLE_MEMBER)


class FeishuUserTests(StoreTestCase):
    def _upsert(self, **overrides):
        payload = {
            "app_id": "cli_a",
            "open_id": "ou_1",
            "union_id": "on_1",
            "tenant_key": "tenant_a",
            "display_name": "Someone",
        }
        payload.update(overrides)
        return self.db.upsert_feishu_user(**payload)

    def test_new_feishu_user_defaults_to_member_with_no_grants(self):
        user = self._upsert()
        self.assertEqual(user.role, store.ROLE_MEMBER)
        self.assertEqual(user.status, store.STATUS_ACTIVE)
        self.assertEqual(self.db.list_project_grants(user.id), [])

    def test_repeat_login_returns_the_same_user_and_refreshes_the_name(self):
        first = self._upsert()
        second = self._upsert(display_name="Renamed")
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.display_name, "Renamed")

    def test_same_open_id_from_a_different_app_is_a_different_user(self):
        """open_id is app-scoped; it must never be treated as globally unique."""
        first = self._upsert(app_id="cli_a")
        second = self._upsert(app_id="cli_b")
        self.assertNotEqual(first.id, second.id)

    def test_tenant_key_is_recorded_from_the_provider_not_the_caller(self):
        user = self._upsert(tenant_key="tenant_a")
        self.assertEqual(user.tenant_key, "tenant_a")

    def test_the_identity_is_the_joint_app_tenant_and_open_id(self):
        """``(app_id, tenant_key, open_id)`` together own the local identity.

        ``open_id`` is issued per application *and* per tenant, so keying on
        ``(app_id, open_id)`` would let two allowlisted tenants collapse onto a
        single local user — and with it that user's project grants. Every part
        of the triple must therefore separate identities.
        """
        base = self._upsert()
        self.assertEqual(self._upsert().id, base.id)
        self.assertNotEqual(self._upsert(tenant_key="tenant_b").id, base.id)
        self.assertNotEqual(self._upsert(app_id="cli_b").id, base.id)
        self.assertNotEqual(self._upsert(open_id="ou_2").id, base.id)

    def test_a_different_tenant_with_the_same_open_id_keeps_its_own_grants(self):
        first = self._upsert(tenant_key="tenant_a")
        other = self._upsert(tenant_key="tenant_b")
        self.db.grant_project(first.id, "atlas", "viewer")
        self.assertEqual(
            [grant.project_id for grant in self.db.list_project_grants(other.id)], []
        )

    def test_disabling_a_user_is_visible_on_read_back(self):
        user = self._upsert()
        self.db.set_user_status(user.id, store.STATUS_DISABLED)
        reloaded = self.db.get_user(user.id)
        self.assertEqual(reloaded.status, store.STATUS_DISABLED)


class SessionPersistenceTests(StoreTestCase):
    def _session(self, raw_id="raw-session-id", **overrides):
        payload = {
            "session_hash": store.session_hash(raw_id),
            "user_id": "user-1",
            "csrf_token": "csrf-token-plaintext",
            "absolute_expires_at": self.clock() + 3600,
        }
        payload.update(overrides)
        return payload

    def test_insert_and_read_back(self):
        self.db.insert_session(**self._session())
        record = self.db.get_session(store.session_hash("raw-session-id"))
        self.assertIsNotNone(record)
        self.assertEqual(record.user_id, "user-1")
        self.assertIsNone(record.revoked_at)

    def test_unknown_session_hash_returns_none(self):
        self.assertIsNone(self.db.get_session(store.session_hash("nope")))

    def test_csrf_token_round_trips_so_auth_csrf_can_hand_it_back(self):
        self.db.insert_session(**self._session())
        record = self.db.get_session(store.session_hash("raw-session-id"))
        self.assertEqual(record.csrf_token, "csrf-token-plaintext")

    def test_raw_session_id_is_never_written_to_disk(self):
        raw = "raw-session-id-that-must-not-be-persisted"
        self.db.insert_session(**self._session(raw_id=raw))
        blob = (self.tmp / "auth.sqlite3").read_bytes()
        self.assertNotIn(raw.encode(), blob)

    def test_revoke_marks_the_session(self):
        self.db.insert_session(**self._session())
        digest = store.session_hash("raw-session-id")
        self.db.revoke_session(digest)
        self.assertIsNotNone(self.db.get_session(digest).revoked_at)

    def test_revoke_all_for_user_revokes_every_session(self):
        self.db.insert_session(**self._session(raw_id="a"))
        self.db.insert_session(**self._session(raw_id="b"))
        self.db.revoke_all_for_user("user-1")
        self.assertIsNotNone(self.db.get_session(store.session_hash("a")).revoked_at)
        self.assertIsNotNone(self.db.get_session(store.session_hash("b")).revoked_at)

    def test_purge_expired_removes_old_sessions(self):
        self.db.insert_session(**self._session(raw_id="old", absolute_expires_at=self.clock() - 1))
        self.db.insert_session(**self._session(raw_id="new"))
        self.db.purge_expired(self.clock())
        self.assertIsNone(self.db.get_session(store.session_hash("old")))
        self.assertIsNotNone(self.db.get_session(store.session_hash("new")))

    def test_touch_updates_last_seen(self):
        self.db.insert_session(**self._session())
        digest = store.session_hash("raw-session-id")
        self.clock.advance(60)
        self.db.touch_session(digest, self.clock())
        self.assertEqual(self.db.get_session(digest).last_seen_at, self.clock())


class LoginStateTests(StoreTestCase):
    def _state(self, raw="state-1", **overrides):
        payload = {
            "state_hash": store.session_hash(raw),
            "expires_at": self.clock() + 600,
            "code_verifier": None,
            "next_path": "/dashboard",
        }
        payload.update(overrides)
        return payload

    def test_consume_returns_the_record_once(self):
        self.db.insert_login_state(**self._state())
        first = self.db.consume_login_state(store.session_hash("state-1"))
        self.assertIsNotNone(first)
        self.assertEqual(first.next_path, "/dashboard")

    def test_state_can_only_be_consumed_once(self):
        """Replay of the same state must fail closed."""
        self.db.insert_login_state(**self._state())
        self.assertIsNotNone(self.db.consume_login_state(store.session_hash("state-1")))
        self.assertIsNone(self.db.consume_login_state(store.session_hash("state-1")))

    def test_expired_state_is_not_consumable(self):
        self.db.insert_login_state(**self._state(expires_at=self.clock() - 1))
        self.assertIsNone(self.db.consume_login_state(store.session_hash("state-1")))

    def test_unknown_state_is_not_consumable(self):
        self.assertIsNone(self.db.consume_login_state(store.session_hash("ghost")))

    # --- browser-bound states ------------------------------------------------

    def _bound(self, raw="state-1", binding="digest-a", **overrides):
        return self._state(raw, binding_hash=binding, **overrides)

    def test_a_bound_state_round_trips_its_binding_digest(self):
        self.db.insert_login_state(**self._bound())
        record = self.db.consume_login_state_bound(
            store.session_hash("state-1"), "digest-a"
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.binding_hash, "digest-a")

    def test_a_bound_state_is_refused_for_a_different_binding(self):
        self.db.insert_login_state(**self._bound())
        self.assertIsNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "digest-b")
        )

    def test_a_refused_binding_does_not_destroy_the_state(self):
        """Otherwise anyone who saw the state could deny the real browser."""
        self.db.insert_login_state(**self._bound())
        self.assertIsNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "wrong")
        )
        self.assertIsNotNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "digest-a")
        )

    def test_a_bound_state_can_still_only_be_consumed_once(self):
        self.db.insert_login_state(**self._bound())
        self.assertIsNotNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "digest-a")
        )
        self.assertIsNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "digest-a")
        )

    def test_an_expired_bound_state_is_not_consumable(self):
        self.db.insert_login_state(**self._bound(expires_at=self.clock() - 1))
        self.assertIsNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "digest-a")
        )

    def test_an_unbound_state_can_never_be_consumed_as_a_bound_one(self):
        """The anonymous CSRF tokens share this table and carry no binding."""
        self.db.insert_login_state(**self._state())
        self.assertIsNone(
            self.db.consume_login_state_bound(store.session_hash("state-1"), "")
        )


class UsedCodeTests(StoreTestCase):
    def test_first_use_is_accepted(self):
        self.assertTrue(
            self.db.claim_provider_code(store.session_hash("code-1"), self.clock() + 600)
        )

    def test_second_use_is_refused(self):
        self.db.claim_provider_code(store.session_hash("code-1"), self.clock() + 600)
        self.assertFalse(
            self.db.claim_provider_code(store.session_hash("code-1"), self.clock() + 600)
        )

    def test_expired_claim_is_pruned_and_reusable(self):
        self.db.claim_provider_code(store.session_hash("code-1"), self.clock() - 1)
        self.assertTrue(
            self.db.claim_provider_code(store.session_hash("code-1"), self.clock() + 600)
        )


class SessionHashTests(unittest.TestCase):
    def test_hash_is_stable_and_not_the_input(self):
        digest = store.session_hash("abc")
        self.assertEqual(digest, store.session_hash("abc"))
        self.assertNotEqual(digest, "abc")
        self.assertGreaterEqual(len(digest), 64)

    def test_token_ids_carry_at_least_256_bits(self):
        seen = {store.new_secret_id() for _ in range(50)}
        self.assertEqual(len(seen), 50)
        for value in seen:
            self.assertGreaterEqual(len(value), 43)  # 32 bytes base64url


if __name__ == "__main__":
    unittest.main()
