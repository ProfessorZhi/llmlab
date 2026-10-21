"""Platform accounts own inference services (owner decision 2026-10-21, option A).

A person runs an engine under whichever Linux user they use, then signs in to
their admin-created platform account and registers it. The Registry records
which Linux uid actually runs the service, refuses a second account claiming
the same address, and — only when an admin has set ``run_as`` for an account —
requires the listener to be that Linux user.

Everything runs offline through the real BFF composition
(``backend.bff.devserver.build_application``). The listener table is fixture
text in ``/proc/net/tcp`` format; no socket is opened. Passwords are fictional.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from backend.auth import bootstrap
from backend.bff import devserver
from backend.registry.ownership import ProcNetListenerTable, ProcNetOwnershipProver
from tests.auth.support import WsgiClient
from tests.auth.test_bootstrap import ScriptedReader

ADMIN_PASSWORD = "synthetic-admin-passphrase-1"
MEMBER_PASSWORD = "synthetic-member-pass-1"
HOST = "127.0.0.1:8790"

# 127.0.0.1:8000 (0x1F40) is served by uid 1001, 127.0.0.1:8001 (0x1F41) by 1002.
TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 51001 1 0000000000000000 100 0 0 10 0
   1: 0100007F:1F41 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1002        0 51002 1 0000000000000000 100 0 0 10 0
"""
USERS = {"zhang": 1001, "li": 1002}
NAMES = {uid: name for name, uid in USERS.items()}


class AccountEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llm-lab-bff-accounts-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = devserver.build_application(state_dir=self.tmp)
        self.addCleanup(self.app.service.store.close)
        self.app.service.ownership_prover = ProcNetOwnershipProver(
            table=ProcNetListenerTable.from_texts(tcp=TCP),
            user_resolver=lambda name: USERS[name],
            uid_namer=NAMES.get,
        )
        bootstrap.initialize_admin_password(
            devserver._local_config(self.tmp),
            read_password=ScriptedReader(ADMIN_PASSWORD, ADMIN_PASSWORD),
        )
        self.admin = self.client()
        r = self.post(self.admin, "/auth/local/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(r.status, 200, r.body)

    # --- helpers -------------------------------------------------------------

    def client(self):
        return WsgiClient(app=self.app, host=HOST, scheme="http")

    def csrf(self, client):
        return client.run("GET", "/auth/csrf").json()["data"]["csrf_token"]

    def post(self, client, path, payload):
        return client.run("POST", path, payload=payload, headers={"X-CSRF-Token": self.csrf(client)})

    def account(self, username, **extra):
        r = self.post(self.admin, "/auth/admin/users", {"username": username, **extra})
        self.assertEqual(r.status, 201, r.body)
        user, temporary = r.json()["data"]["user"], r.json()["data"]["temporary_password"]
        client = self.client()
        self.assertEqual(self.post(client, "/auth/account/login", {"username": username, "password": temporary}).status, 200)
        return user, client, temporary

    def activate(self, username, **extra):
        user, client, temporary = self.account(username, **extra)
        r = self.post(client, "/auth/account/password", {"current_password": temporary, "new_password": MEMBER_PASSWORD})
        self.assertEqual(r.status, 200, r.body)
        return user, client

    def register(self, client, name, port, path="/api/v2/registry/engines"):
        return self.post(client, path, {"name": name, "protocol": "openai_compatible", "target_url": f"http://127.0.0.1:{port}"})

    # --- tests ---------------------------------------------------------------

    def test_an_account_session_resolves_to_its_own_principal(self):
        user, client = self.activate("wang")
        r = client.run("GET", "/api/v2/ops")
        self.assertEqual(r.status, 200, r.body)
        # Members see the shared host readings route (503 here: nothing attached), not a 403.
        self.assertNotEqual(client.run("GET", "/api/infra/summary").status, 403)

    def test_a_one_time_password_session_gets_no_data(self):
        _user, client, _temporary = self.account("sun")
        r = client.run("GET", "/api/v2/ops")
        self.assertEqual(r.status, 403)
        self.assertEqual(r.error_code(), "password_change_required")

    def test_preflight_records_the_runtime_linux_user(self):
        _user, client = self.activate("zhang")
        r = self.register(client, "qwen", 8000, path="/api/v2/registry/engines/preflight")
        self.assertEqual(r.status, 200, r.body)
        check = r.json()["data"]["checks"]["ownership"]
        self.assertIs(check["ok"], True)
        self.assertEqual(check["detail"], "runtime_observed")
        self.assertEqual(check["evidence"]["uid"], 1001)
        self.assertEqual(check["evidence"]["linux_user"], "zhang")

    def test_register_makes_the_account_the_owner(self):
        user, client = self.activate("zhang")
        r = self.register(client, "qwen", 8000)
        self.assertEqual(r.status, 201, r.body)
        engine = r.json()["data"]
        self.assertEqual(engine["owner"], user["id"])
        self.assertEqual(engine["owner_uid"], 1001)
        self.assertEqual(engine["ownership"]["state"], "runtime_observed")
        self.assertEqual(engine["ownership"]["linux_user"], "zhang")

    def test_an_account_may_register_a_service_run_by_any_linux_user(self):
        # Accounts are independent of Linux: "li" the account may run under uid 1001.
        _user, client = self.activate("li")
        r = self.register(client, "borrowed-shell", 8000)
        self.assertEqual(r.status, 201, r.body)
        self.assertEqual(r.json()["data"]["ownership"]["linux_user"], "zhang")

    def test_a_second_account_cannot_claim_the_same_address(self):
        _a, first = self.activate("zhang")
        _b, second = self.activate("li")
        self.assertEqual(self.register(first, "qwen", 8000).status, 201)
        pre = self.register(second, "mine", 8000, path="/api/v2/registry/engines/preflight")
        self.assertIs(pre.json()["data"]["checks"]["ownership"]["ok"], False)
        self.assertEqual(pre.json()["data"]["checks"]["ownership"]["detail"], "target_registered_by_another_account")
        self.assertEqual(self.register(second, "mine", 8000).status, 409)

    def test_run_as_restricts_an_account_to_one_linux_user(self):
        _user, client = self.activate("li", linux_user="li")
        wrong = self.register(client, "x", 8000, path="/api/v2/registry/engines/preflight")
        check = wrong.json()["data"]["checks"]["ownership"]
        self.assertIs(check["ok"], False)
        self.assertEqual(check["detail"], "owner_mismatch:1001")
        right = self.register(client, "y", 8001, path="/api/v2/registry/engines/preflight")
        self.assertIs(right.json()["data"]["checks"]["ownership"]["ok"], True)
        self.assertEqual(right.json()["data"]["checks"]["ownership"]["detail"], "verified")

    def test_registration_needs_a_session_csrf_and_no_asserted_owner(self):
        _user, client = self.activate("zhang")
        anonymous = self.client()
        self.assertEqual(self.register(anonymous, "x", 8000).status, 401)
        no_csrf = client.run("POST", "/api/v2/registry/engines", payload={"name": "x", "target_url": "http://127.0.0.1:8000"})
        self.assertEqual(no_csrf.status, 403)
        asserted = self.post(client, "/api/v2/registry/engines", {"name": "x", "target_url": "http://127.0.0.1:8000", "owner": "account:other"})
        self.assertEqual(asserted.status, 400)

    def engine_names(self, client):
        r = client.run("GET", "/api/v2/ops")
        self.assertEqual(r.status, 200, r.body)
        return sorted(row["name"] for row in r.json()["data"]["ops"]["registry"]["engines"])

    def test_an_admin_account_sees_every_service_and_a_member_only_its_own(self):
        _z, zhang = self.activate("zhang")
        _l, li = self.activate("li")
        _b, boss = self.activate("boss", role="admin")
        self.assertEqual(self.register(zhang, "qwen", 8000).status, 201)
        self.assertEqual(self.register(li, "bge", 8001).status, 201)
        self.assertEqual(self.engine_names(boss), ["bge", "qwen"])
        self.assertEqual(self.engine_names(zhang), ["qwen"])
        self.assertEqual(self.engine_names(self.admin), ["bge", "qwen"], "the bootstrap admin reads the whole lab")
        row = self.app.service.store.list_engines()[0]
        self.assertTrue(row.owner_principal.startswith("account:"))

    def test_accounts_cannot_use_the_shared_password_admin_mode(self):
        _user, client = self.activate("zhang")
        r = self.post(client, "/auth/admin/enter", {"password": "anything-at-all"})
        self.assertEqual(r.status, 403)
        self.assertEqual(r.error_code(), "linux_auth_not_enabled")

    def test_a_disabled_account_drops_out_of_the_approved_directory(self):
        user, _client = self.activate("chen")
        directory = self.app.service.principals
        self.assertIn(user["id"], directory.approved_principals())
        self.post(self.admin, "/auth/admin/users/status", {"user_id": user["id"], "status": "disabled"})
        self.assertNotIn(user["id"], directory.approved_principals())
        self.assertFalse(directory.lookup(user["id"]).approved)


if __name__ == "__main__":
    unittest.main()
