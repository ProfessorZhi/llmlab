"""The edges of the BFF: writes, routing, and what must never be reachable.

Three separate concerns, all of which have bitten this repository before:

* a write without CSRF or from another origin (reused from the existing auth
  application, not reimplemented here);
* the browser never being handed a way off the origin — in particular never a
  path naming port 8080, which is the upstream gateway's port;
* the honest answer when the Linux login path has no real PAM behind it.
"""

from __future__ import annotations

import tempfile
import unittest

from backend.auth_linux import errors as linux_errors
from backend.bff import devserver
from tests.bff import support as S


class WriteGuardTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.client = self.h.client("alice")
        self.ids = S.register_baseline(self.h, "alice")

    def test_a_share_without_csrf_is_refused(self):
        response = self.client.run(
            "PUT",
            f"/api/v2/ops/deployments/{self.ids['deployment_id']}/share",
            payload={"mode": "all", "principals": []},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(
            self.h.service.store.endpoint_by_id(self.ids["endpoint_id"]).visibility,
            "private",
        )

    def test_a_share_from_another_origin_is_refused(self):
        response = self.client.run(
            "PUT",
            f"/api/v2/ops/deployments/{self.ids['deployment_id']}/share",
            payload={"mode": "all", "principals": []},
            headers={
                "X-CSRF-Token": S.csrf(self.client),
                "Origin": "http://evil.example.com",
            },
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(
            self.h.service.store.endpoint_by_id(self.ids["endpoint_id"]).visibility,
            "private",
        )

    def test_a_share_with_no_origin_header_at_all_is_refused(self):
        response = self.client.run(
            "PUT",
            f"/api/v2/ops/deployments/{self.ids['deployment_id']}/share",
            payload={"mode": "all", "principals": []},
            headers={"X-CSRF-Token": S.csrf(self.client)},
            send_origin=False,
        )
        self.assertEqual(response.status, 403)

    def test_a_malformed_body_is_refused(self):
        for payload in ({}, {"mode": "all", "principals": "bob"}, {"principals": []}):
            response = S.share(self.client, self.ids["deployment_id"], mode=payload.get("mode", ""), principals=payload.get("principals") or [])
            self.assertEqual(response.status, 400, repr(payload))

    def test_a_csrf_token_is_single_use_on_the_anonymous_path(self):
        """The login route is a write too, so it cannot be forged cross-site."""
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        token = S.csrf(client)
        first = client.run(
            "POST",
            "/auth/linux/login",
            payload={"login_name": "alice", "password": S.WRONG_PASSWORD},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(first.status, 401)
        replay = client.run(
            "POST",
            "/auth/linux/login",
            payload={"login_name": "alice", "password": S.GOOD_PASSWORD},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(replay.status, 403, "a replayed CSRF token must lose")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.client = self.h.client("alice")

    def test_an_unknown_ops_path_is_404(self):
        response = self.client.run("GET", "/api/v2/ops/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.error_code(), "not_found")

    def test_a_known_path_with_the_wrong_method_is_405(self):
        self.assertEqual(self.client.run("POST", "/api/v2/ops").status, 405)
        self.assertEqual(self.client.run("GET", "/auth/linux/login").status, 405)
        self.assertEqual(self.client.run("POST", "/auth/linux/status").status, 405)

    def test_the_rest_of_the_auth_surface_still_works(self):
        self.assertEqual(self.client.run("GET", "/auth/csrf").status, 200)
        me = self.client.run("GET", "/auth/me")
        self.assertEqual(me.status, 200)
        self.assertEqual(
            me.json()["data"]["user"]["id"], self.h.principal_of("alice")
        )

    def test_an_unknown_visibility_is_400(self):
        self.assertEqual(S.fetch_ops(self.client, "everything").status, 400)

    def test_the_default_visibility_is_private(self):
        response = S.fetch_ops(self.client)
        self.assertEqual(response.status, 200)
        self.assertEqual(S.ops_of(response)["scope"]["visibility"], "private")
        self.assertEqual(S.ops_of(response)["registry"]["shared"], [])


class NoWayOffTheOriginTests(unittest.TestCase):
    """Every href the BFF hands the browser stays on this origin."""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")
        S.share(self.h.client("alice"), self.ids["deployment_id"], mode="all")

    def _payloads(self):
        for name in ("alice", "carol", "bob"):
            for visibility in (None, "shared"):
                yield S.ops_of(S.fetch_ops(self.h.client(name), visibility))

    def test_no_href_escapes_the_origin_or_names_the_upstream_port(self):
        raw = []
        for ops in self._payloads():
            raw.extend(link.get("href") for link in ops["links"])
            for row in ops["registry"]["owned"]:
                raw.extend(link.get("href") for link in row["links"])
            for row in ops["registry"]["shared"]:
                raw.extend(link.get("href") for link in row["links"])
            endpoint = ops["playground"].get("endpoint")
            if endpoint:
                raw.append(endpoint)
        self.assertTrue(raw, "the scrape found no links at all")
        for href in raw:
            if href is None:
                continue
            self.assertTrue(href.startswith("/"), href)
            self.assertFalse(href.startswith("//"), href)
            self.assertNotIn("://", href)
            self.assertNotIn(":8080", href, "the upstream port must never appear")

    def test_no_payload_carries_prompt_content_or_an_unmasked_key(self):
        import json
        import re

        for ops in self._payloads():
            blob = json.dumps(ops, ensure_ascii=False)
            for field in ("prompt", "completion", "messages", "plaintext", "secret"):
                self.assertNotIn(f'"{field}"', blob, field)
            # Any key prefix present must be masked — a full key has no mask
            # character in it, so this is the whole "the page cannot show a key"
            # property in one line.
            for prefix in re.findall(r'"prefix":\s*"([^"]*)"', blob):
                self.assertIn("•", prefix)

    def test_every_credential_shown_is_masked(self):
        from backend.bff.ops import _key_record
        from backend.registry.identity import TokenBinding

        binding = TokenBinding("super-secret-token-value", self.h.principal_of("alice"), frozenset({"registry:read"}), label="laptop")
        row = _key_record(binding)
        self.assertNotIn("super-secret", str(row))
        self.assertNotIn(binding.token, str(row))
        self.assertIn("•", row["prefix"])
        self.assertLessEqual(len(row["prefix"]), 24)


class LinuxLoginHonestyTests(unittest.TestCase):
    """The login route exists, is wired, and tells the truth about PAM."""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def test_status_says_password_verification_is_unavailable(self):
        data = self.h.client("alice").run("GET", "/auth/linux/status").json()["data"]
        self.assertEqual(data["password_verification"], "unavailable_in_this_build")
        self.assertTrue(data["enabled"])

    def test_no_password_is_echoed_back_on_a_failed_login(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        response = S.login(client, "alice", S.WRONG_PASSWORD)
        self.assertEqual(response.status, 401)
        self.assertNotIn(S.WRONG_PASSWORD.encode(), response.body)
        self.assertNotIn(S.GOOD_PASSWORD.encode(), response.body)
        self.assertNotIn(self.h.app.cfg.cookie_name, client.cookies)

    def test_a_rate_limited_account_is_429_without_a_session(self):
        client = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        for _ in range(6):
            response = S.login(client, "alice", S.WRONG_PASSWORD)
        self.assertEqual(response.status, 429)
        self.assertNotIn(self.h.app.cfg.cookie_name, client.cookies)


class BindGuardTests(unittest.TestCase):
    """The loopback process refuses to become something else."""

    def test_the_upstream_port_is_never_bound(self):
        with self.assertRaises(SystemExit) as caught:
            devserver.validate_bind_port(8080)
        self.assertIn("reserved", str(caught.exception))

    def test_the_default_port_is_not_the_upstream_port(self):
        self.assertNotIn(devserver.DEFAULT_PORT, devserver.RESERVED_BIND_PORTS)

    def test_only_loopback_addresses_may_be_bound(self):
        for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            with self.assertRaises(SystemExit, msg=host):
                devserver.validate_bind_host(host)
        self.assertEqual(devserver.validate_bind_host("127.0.0.1"), "127.0.0.1")

    def test_a_nonsense_port_is_refused(self):
        for port in (0, -1, 70000):
            with self.assertRaises(SystemExit, msg=str(port)):
                devserver.validate_bind_port(port)

    def test_the_dev_server_uses_an_unreachable_broker(self):
        """No socket on this machine is touched by the login path."""
        accounts = S.FakePwdSource([S.make_account(S.MEMBER_NAME, S.MEMBER_UID)])
        service = devserver.build_linux_service(
            host_id=S.SYNTHETIC_HOST_ID, accounts=accounts
        )
        self.assertTrue(service.status().enabled)
        with self.assertRaises(linux_errors.PamUnavailable):
            service.login("alice", S.GOOD_PASSWORD, client_key="127.0.0.1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
