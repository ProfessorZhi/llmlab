"""Issue #49 要求的六类负向用例，一处跑完。

这条 Lane 的验收条款里点名了六类负向回归：匿名 401、伪造 UID/role、会话 CSRF、
来源误用、跨 UID 拒绝、过期/陈旧 Host。它们**分散**在各自的文件里各验一层——
那样每一层都验得深，但没有一个地方能回答「这六条今天是否都还是真的」。

这个文件就是那个地方：每条类别在这里各跑一次端到端（真的经 WSGI 发请求），并且
`DeepCoverageTests` 把该类别**更细的那份覆盖**按名字钉住——那些用例被改名或删掉
时，这里会失败，而不是让「已验证」变成一句没人核对的旧话。

这里刻意**不**重抄那六条的完整矩阵：抄一份的结果是两份会漂移的用例。深的那份留在
原处，这里只做汇总与链接。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import identity as bff_identity
from backend.bff import origin as bff_origin
from tests.bff import support as S

#: 六类负向 → 汇总用例（本文件里真的会跑的那一条）与它们**更细的**那份覆盖在哪。
#: 名字写全：改名或删掉任何一条，这里就会失败。
MATRIX = {
    "anonymous401": {
        "summary": "test_anonymous_is_401",
        "deep": (
            "tests.bff.test_cross_module_e2e.CrossModuleEndToEnd."
            "test_no_session_is_401_on_every_route"
        ),
    },
    "forged-uid-role": {
        "summary": "test_a_forged_uid_or_role_is_400_not_a_denial_to_hide",
        "deep": (
            "tests.bff.test_cross_module_e2e.CrossModuleEndToEnd."
            "test_forged_identity_headers_are_refused_on_every_route"
        ),
    },
    "session-csrf": {
        "summary": "test_a_write_without_csrf_is_refused_and_changes_nothing",
        "deep": (
            "tests.admin_mode.test_http_routes.CookiesAndCsrfTests."
            "test_entering_without_a_csrf_token_is_refused"
        ),
    },
    "source-misuse": {
        "summary": "test_a_client_declared_source_is_400",
        "deep": (
            "tests.bff.test_source_guard.SourceAssertionRejectionTests."
            "test_a_declared_source_in_a_header_is_rejected"
        ),
    },
    "cross-uid-denial": {
        "summary": "test_a_cross_uid_read_is_denied_and_the_share_flips_it",
        "deep": (
            "tests.bff.test_two_user_acl.TwoUserACLTests."
            "test_b_cannot_see_a_before_the_share"
        ),
    },
    "stale-host": {
        "summary": "test_a_stale_host_is_421_even_with_a_valid_session",
        "deep": (
            "tests.bff.test_host_guard.HostGuardOverWsgiTests."
            "test_an_unknown_host_is_421_even_with_a_valid_session_cookie"
        ),
    },
}


class TheSixNegativeRegressions(unittest.TestCase):
    """每条一条，端到端，经真正的 WSGI 入口。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        self.ids = S.register_baseline(self.h, "bob")

    def _anonymous(self):
        return S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def test_anonymous_is_401(self):
        response = self._anonymous().run("GET", "/api/v2/ops")
        self.assertEqual(response.status, 401)
        self.assertEqual(response.error_code(), "no_authenticated_session")

    def test_a_forged_uid_or_role_is_400_not_a_denial_to_hide(self):
        for headers in (
            {"X-UID": "4003"},
            {"X-Role": "platform_admin"},
            {"X-Admin": "true"},
            {"X-Owner": self.h.principal_of("bob")},
        ):
            with self.subTest(headers=sorted(headers)):
                response = self.h.client("alice").run(
                    "GET", "/api/v2/ops", headers=headers
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(
                    response.error_code(), "identity_assertion_rejected"
                )

    def test_a_write_without_csrf_is_refused_and_changes_nothing(self):
        client = self.h.client("bob")
        response = client.run(
            "PUT",
            f"/api/v2/ops/deployments/{self.ids['deployment_id']}/share",
            payload={"mode": "all", "principals": []},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(
            self.h.service.store.endpoint_by_id(self.ids["endpoint_id"]).visibility,
            "private",
        )

    def test_a_client_declared_source_is_400(self):
        response = self.h.client("alice").run("GET", "/api/v2/ops?source=live")
        self.assertEqual(response.status, 400)
        self.assertEqual(
            response.error_code(), bff_identity.REASON_SOURCE_ASSERTED
        )

    def test_a_cross_uid_read_is_denied_and_the_share_flips_it(self):
        alice = self.h.client("alice")

        def alice_sees() -> set:
            ops = S.ops_of(S.fetch_ops(alice, "shared"))
            return {row["endpoint_id"] for row in ops["registry"]["shared"]}

        self.assertNotIn(self.ids["endpoint_id"], alice_sees())
        S.share(self.h.client("bob"), self.ids["deployment_id"], mode="all")
        self.assertIn(self.ids["endpoint_id"], alice_sees())

    def test_a_stale_host_is_421_even_with_a_valid_session(self):
        client = self.h.client("alice")
        client.host = "stale-portal.internal"
        response = client.run("GET", "/api/v2/ops")
        self.assertEqual(response.status, 421)
        self.assertEqual(response.error_code(), bff_origin.REASON_FOREIGN_HOST)


class DeepCoverageTests(unittest.TestCase):
    """汇总里的每一条，背后都要有真的实现，而且更细的那份覆盖必须还在。"""

    def test_the_matrix_covers_every_required_category(self):
        """集合恰好是那六个：少一个说明漏了，多一个说明该更新 Issue #49 的条款。"""
        self.assertEqual(
            set(MATRIX),
            {
                "anonymous401",
                "forged-uid-role",
                "session-csrf",
                "source-misuse",
                "cross-uid-denial",
                "stale-host",
            },
        )

    def test_every_category_has_a_summary_case_that_really_runs(self):
        for category, entry in MATRIX.items():
            with self.subTest(category=category):
                self.assertTrue(
                    callable(
                        getattr(TheSixNegativeRegressions, entry["summary"], None)
                    ),
                    f"{entry['summary']} is missing from the summary suite",
                )

    def test_every_category_still_has_its_deep_coverage(self):
        loader = unittest.defaultTestLoader
        for category, entry in MATRIX.items():
            with self.subTest(category=category, test=entry["deep"]):
                try:
                    loaded = loader.loadTestsFromName(entry["deep"])
                except Exception as exc:  # noqa: BLE001 - 直接报出来更好读
                    self.fail(f"{entry['deep']} could not be loaded: {exc!r}")
                self.assertGreater(
                    loaded.countTestCases(), 0, f"{entry['deep']} no longer exists"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
