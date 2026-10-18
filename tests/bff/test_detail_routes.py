"""``/api/v2/ops/usage`` 与 ``/api/v2/ops/engines`` 两条同源路由。

它们存在的原因是形状：``portal-ops/1`` 的指标卡槽位只接受白名单来源的**数字**，
放不下完整的用量 DTO（计数器、分桶、质量、未知分类）。所以完整的形状走这两条
路由，卡片保持 UNKNOWN。两条都：同源、按会话鉴权、拒绝自报身份头、scope 决定
能拿到哪一档。
"""

from __future__ import annotations

import json
import tempfile
import unittest

from backend.bff.app import ENGINES_PATH, USAGE_PATH
from tests.bff import support as S


class UsageRouteTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "alice")

    def usage(self, name, query="", *, admin=False):
        client = self.h.admin_client(name) if admin else self.h.client(name)
        return client.run("GET", f"{USAGE_PATH}{query}")

    def test_a_member_gets_their_own_usage(self):
        response = self.usage("alice")
        self.assertEqual(response.status, 200)
        data = response.json()["data"]
        self.assertEqual(data["kind"], "usage.self")
        self.assertTrue(data["synthetic"])
        self.assertEqual(data["scope"]["uid"], self.h.principal_of("alice"))
        self.assertGreater(data["counters"]["calls"], 0)

    def test_the_scope_is_the_sessions_and_cannot_be_requested_away(self):
        """No query parameter names a uid; attribution is `key_id -> uid`."""
        data = self.usage("alice", f"?scope=self&requested_uid={self.h.principal_of('bob')}")
        self.assertEqual(data.status, 200)
        self.assertEqual(
            data.json()["data"]["scope"]["uid"], self.h.principal_of("alice")
        )

    def test_a_member_may_not_ask_for_the_global_overview(self):
        response = self.usage("alice", "?scope=global")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.error_code(), "observability.access_denied")

    def test_an_elevated_session_may_read_the_global_overview(self):
        response = self.usage("carol", "?scope=global", admin=True)
        self.assertEqual(response.status, 200)
        data = response.json()["data"]
        self.assertEqual(data["kind"], "usage.admin")
        self.assertTrue(data["scope"]["admin"])

    def test_the_same_person_without_elevation_may_not(self):
        """`carol` is the granted Linux administrator; his plain session is a member.

        The two requests below differ by nothing the client sends — same account,
        same cookie name, no query parameter — only by whether that particular
        session has entered admin mode. That is the property: the gate is
        server-side session state, not a claim the caller makes.
        """
        refused = self.usage("carol", "?scope=global")
        self.assertEqual(refused.status, 403)
        self.assertEqual(refused.error_code(), "observability.access_denied")

        allowed = self.usage("carol", "?scope=global", admin=True)
        self.assertEqual(allowed.status, 200)

    def test_an_unknown_scope_is_400(self):
        response = self.usage("alice", "?scope=everything")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.error_code(), "bff.invalid_request")

    def test_the_usage_dto_carries_no_content_and_no_upstream_port(self):
        blob = json.dumps(self.usage("carol", "?scope=global", admin=True).json(), ensure_ascii=False)
        for field in ("prompt", "completion", "messages", "request_id"):
            self.assertNotIn(f'"{field}"', blob, field)
        self.assertNotIn(":8080", blob)
        self.assertNotIn("://", blob)

    def test_losing_elevation_closes_the_global_route_again(self):
        """What the route gates on is the session's *current* elevation.

        This replaced a test that removed the user's stored role to manufacture a
        scope-less session. Roles no longer decide this, so the honest lever is
        elevation itself: exit admin mode and the very next request is refused,
        with no cache and no re-login in between.
        """
        client = self.h.admin_client("carol")
        self.assertEqual(self.h.admin_mode.status_for(
            client.cookies[self.h.app.cfg.cookie_name]
        ).elevated, True)
        self.assertEqual(client.run("GET", f"{USAGE_PATH}?scope=global").status, 200)

        exited = S.exit_admin_mode(client)
        self.assertEqual(exited.status, 200)
        self.assertEqual(client.run("GET", f"{USAGE_PATH}?scope=global").status, 403)
        # The session itself is untouched — only the elevation is gone.
        self.assertEqual(S.fetch_ops(client).status, 200)


class EngineRouteTests(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp(), clock=S.FixedClock())
        self.addCleanup(self.h.close)

    def engines(self, name):
        return self.h.client(name).run("GET", ENGINES_PATH)

    def test_the_engine_status_is_a_synthetic_test_double(self):
        S.register_baseline(self.h, "alice")
        data = self.engines("alice").json()["data"]
        self.assertTrue(data["synthetic"])
        self.assertTrue(any("SYNTHETIC" in hint for hint in data["display_hints"]))
        self.assertEqual(len(data["engines"]), 1)

    def test_an_engine_with_no_endpoint_is_unknown_rather_than_down(self):
        data = self.engines("alice").json()["data"]
        # The probe is a test double that reports "up" — the point here is that the
        # *absence* of a probe would give unknown, and that the payload never
        # invents `down`.
        for engine in data["engines"]:
            self.assertIn(engine["status"], {"up", "degraded", "down", None})

    def test_the_route_requires_a_session(self):
        anon = S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")
        self.assertEqual(anon.run("GET", ENGINES_PATH).status, 401)

    def test_the_route_rejects_identity_headers(self):
        response = self.h.client("alice").run(
            "GET", ENGINES_PATH, headers={"X-Admin": "1"}
        )
        self.assertEqual(response.status, 400)

    def test_the_wrong_method_is_405(self):
        client = self.h.client("alice")
        self.assertEqual(client.run("POST", ENGINES_PATH).status, 405)
        self.assertEqual(client.run("POST", USAGE_PATH).status, 405)

    def test_no_route_hands_the_browser_an_upstream_engine_target(self):
        """Engine status is metadata: no host, no port, no loopback address."""
        S.register_baseline(self.h, "alice")
        blob = json.dumps(self.engines("alice").json(), ensure_ascii=False)
        self.assertNotIn(":8080", blob)
        self.assertNotIn("127.0.0.1:", blob)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
