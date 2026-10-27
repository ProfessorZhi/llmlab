"""Plugin page jump addresses (``/api/portal/integrations``).

Every plugin may carry one native-UI address that the console links out to.
Admins set it; members see it unless it is admin-only. Runs offline through the
real BFF composition; passwords are fictional.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest

from backend.auth import bootstrap
from backend.bff import devserver
from backend.bff.integrations import InvalidIntegration, validate_ui_url
from tests.auth.support import WsgiClient
from tests.auth.test_bootstrap import ScriptedReader

ADMIN_PASSWORD = "synthetic-admin-passphrase-1"
MEMBER_PASSWORD = "synthetic-member-pass-1"
HOST = "127.0.0.1:8790"


class ValidationTests(unittest.TestCase):
    def test_accepted_shapes(self):
        self.assertEqual(validate_ui_url("https://dell.example:8443/strata/"), "https://dell.example:8443/strata/")
        self.assertEqual(validate_ui_url("  /grafana/d/lab  "), "/grafana/d/lab")
        self.assertIsNone(validate_ui_url(""))
        self.assertIsNone(validate_ui_url(None))

    def test_refused_shapes(self):
        for bad in ("javascript:alert(1)", "//evil.example/", "/\\evil", "ftp://x/", "https://u:p@host/",
                    "http://", "has space/", "x" * 600, 42, "data:text/html,hi"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidIntegration):
                    validate_ui_url(bad)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llm-lab-bff-integrations-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = devserver.build_application(state_dir=self.tmp)
        self.addCleanup(self.app.service.store.close)
        bootstrap.initialize_admin_password(
            devserver._local_config(self.tmp), read_password=ScriptedReader(ADMIN_PASSWORD, ADMIN_PASSWORD)
        )
        self.admin = self.client()
        self.assertEqual(self.post(self.admin, "/auth/local/login", {"username": "admin", "password": ADMIN_PASSWORD}).status, 200)
        r = self.post(self.admin, "/auth/admin/users", {"username": "zhang"})
        temporary = r.json()["data"]["temporary_password"]
        self.member = self.client()
        self.post(self.member, "/auth/account/login", {"username": "zhang", "password": temporary})
        self.post(self.member, "/auth/account/password", {"current_password": temporary, "new_password": MEMBER_PASSWORD})

    def client(self):
        return WsgiClient(app=self.app, host=HOST, scheme="http")

    def csrf(self, client):
        return client.run("GET", "/auth/csrf").json()["data"]["csrf_token"]

    def post(self, client, path, payload):
        return client.run("POST", path, payload=payload, headers={"X-CSRF-Token": self.csrf(client)})

    def put(self, client, key, payload):
        return client.run("PUT", f"/api/portal/integrations/{key}", payload=payload, headers={"X-CSRF-Token": self.csrf(client)})

    def listing(self, client):
        r = client.run("GET", "/api/portal/integrations")
        self.assertEqual(r.status, 200, r.body)
        return {row["key"]: row for row in r.json()["data"]["integrations"]}

    def test_the_catalogue_starts_without_addresses(self):
        rows = self.listing(self.member)
        self.assertEqual(set(rows), {"strata", "vllm", "sglang", "prometheus", "grafana", "evalscope"})
        self.assertTrue(all(row["ui_url"] is None and row["has_ui"] is False for row in rows.values()))
        self.assertEqual(rows["grafana"]["visibility"], "admin")
        self.assertEqual(rows["strata"]["visibility"], "all")

    def test_admin_sets_an_address_and_members_see_it(self):
        r = self.put(self.admin, "strata", {"ui_url": "https://dell.example/strata/"})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(self.listing(self.member)["strata"]["ui_url"], "https://dell.example/strata/")

    def test_an_admin_only_address_is_not_sent_to_members(self):
        self.put(self.admin, "grafana", {"ui_url": "/grafana/"})
        member = self.listing(self.member)["grafana"]
        self.assertIsNone(member["ui_url"])
        self.assertTrue(member["has_ui"])
        self.assertEqual(self.listing(self.admin)["grafana"]["ui_url"], "/grafana/")
        self.put(self.admin, "grafana", {"ui_url": "/grafana/", "visibility": "all"})
        self.assertEqual(self.listing(self.member)["grafana"]["ui_url"], "/grafana/")

    def test_clearing_removes_the_entry(self):
        self.put(self.admin, "evalscope", {"ui_url": "/evalscope/"})
        self.put(self.admin, "evalscope", {"ui_url": ""})
        self.assertFalse(self.listing(self.member)["evalscope"]["has_ui"])

    def test_members_cannot_change_addresses(self):
        r = self.put(self.member, "strata", {"ui_url": "https://evil.example/"})
        self.assertEqual(r.status, 403)
        self.assertIsNone(self.listing(self.admin)["strata"]["ui_url"])

    def test_bad_input_is_refused(self):
        self.assertEqual(self.put(self.admin, "strata", {"ui_url": "javascript:alert(1)"}).status, 400)
        self.assertEqual(self.put(self.admin, "strata", {"ui_url": "/x", "visibility": "everyone"}).status, 400)
        self.assertEqual(self.put(self.admin, "not-a-plugin", {"ui_url": "/x"}).status, 404)

    def test_writes_need_csrf_and_a_session(self):
        no_csrf = self.admin.run("PUT", "/api/portal/integrations/strata", payload={"ui_url": "/x"})
        self.assertEqual(no_csrf.status, 403)
        self.assertEqual(self.client().run("GET", "/api/portal/integrations").status, 401)

    def test_the_settings_file_is_private(self):
        self.put(self.admin, "strata", {"ui_url": "/strata/"})
        mode = stat.S_IMODE(os.stat(os.path.join(self.tmp, "integrations.json")).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
