"""跨 UID 的存在性遮蔽：不可见 == 不存在，**对读和写都一样**。

`docs/integrations/PORTAL_BFF_API_CONTRACT.md` §2.2 与 Issue #76 都写了一条：

    不可从错误区分「存在但无权」。

它容易被只测一半：GET 一个别人的 Endpoint 回 404，看起来就够了；但**写动作**
（verify / share / unshare / repoint / rollback / routing-applied / invoke）
如果先判权限再判可见性，就会用 403 把「这个东西存在」说出来。所以这里的断言是
成对的：

* 同一个动作打在「别人的对象」和「不存在的 id」上，**状态码与错误码完全一致**；
* 响应体里不出现 owner、principal、name、route_path —— 连一个字符串片段都不行；
* 列表接口给出已认证的空结果，而不是"全局没有"。

安全动作的判定顺序是对的：`AccessPolicy.require_endpoint_read()` 先跑，所以
非 admin 在**任何**动作上都先得到 404。
"""

from __future__ import annotations

import unittest

from tests.registry.support import Harness

from backend.registry.api import PREFIX

OWNER = "alice"
OTHER = "dave"
ADMIN = "carol"  # 对照组：全局只读者本应看得见


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.endpoint = self.h.make_ready_endpoint(OWNER, name="owned-svc")
        deployment = self.h.service.store.deployment_by_id(
            self.h.service.store.endpoint_by_id(self.endpoint).deployment_id
        )
        self.engine = deployment.engine_id
        self.model = deployment.model_id
        self.deployment = deployment.id
        self.target = self.h.make_sibling_deployment(OWNER, self.endpoint, "next")

    def call(self, method: str, path: str, principal: str, body: dict | None = None):
        return self.h.request(method, path, principal=principal, body=body)


class ReadsAreIndistinguishableTest(_Fixture):
    def test_each_resource_family_redacts_the_same_way(self) -> None:
        cases = (
            ("endpoints", self.endpoint, "ep_absent_xyz"),
            ("engines", self.engine, "eng_absent_xyz"),
            ("models", self.model, "mdl_absent_xyz"),
            ("deployments", self.deployment, "dep_absent_xyz"),
        )
        for collection, real_id, absent_id in cases:
            with self.subTest(collection=collection):
                forbidden = self.call(
                    "GET", f"{PREFIX}/{collection}/{real_id}", OTHER
                )
                missing = self.call(
                    "GET", f"{PREFIX}/{collection}/{absent_id}", OTHER
                )
                self.assertEqual(forbidden[0], missing[0], forbidden[2])
                self.assertEqual(
                    forbidden[2]["error"]["code"], missing[2]["error"]["code"]
                )
                self.assertEqual(forbidden[0], 404)

    def test_the_list_is_an_authenticated_empty_not_a_global_absence(self) -> None:
        for collection in ("engines", "models", "deployments", "endpoints"):
            with self.subTest(collection=collection):
                status, _h, payload = self.call(
                    "GET", f"{PREFIX}/{collection}", OTHER
                )
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["items"], [])
                self.assertEqual(payload["data"]["count"], 0)

    def test_an_admin_still_sees_it(self) -> None:
        """对照组：遮蔽是针对**权限**的，不是把对象藏起来不让任何人看。"""
        status, _h, payload = self.call(
            "GET", f"{PREFIX}/endpoints/{self.endpoint}", ADMIN
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["data"]["id"], self.endpoint)


class WritesDoNotLeakExistenceTest(_Fixture):
    """写在可见性之前不得判权限 —— 否则 403 就是一台存在性探针。"""

    def _actions(self) -> tuple[tuple[str, str, dict | None], ...]:
        return (
            ("POST", f"{PREFIX}/endpoints/{{id}}/verify", None),
            ("POST", f"{PREFIX}/endpoints/{{id}}/share", {"mode": "users", "grantees": ["bob"]}),
            ("POST", f"{PREFIX}/endpoints/{{id}}/unshare", {}),
            (
                "POST",
                f"{PREFIX}/endpoints/{{id}}/repoint",
                {"target_deployment_id": self.target, "expected_revision": 1},
            ),
            ("POST", f"{PREFIX}/endpoints/{{id}}/rollback", {"expected_revision": 1}),
            ("POST", f"{PREFIX}/endpoints/{{id}}/routing-applied", {"generation": 1}),
            ("POST", f"{PREFIX}/endpoints/{{id}}/invoke", {"messages": []}),
        )

    def test_every_write_action_is_404_on_someone_elses_endpoint(self) -> None:
        for method, template, body in self._actions():
            with self.subTest(action=template.rsplit("/", 1)[-1]):
                forbidden = self.call(method, template.format(id=self.endpoint), OTHER, body)
                missing = self.call(method, template.format(id="ep_absent_xyz"), OTHER, body)
                self.assertEqual(forbidden[0], missing[0], forbidden[2])
                self.assertEqual(
                    forbidden[2]["error"]["code"], missing[2]["error"]["code"]
                )
                self.assertEqual(forbidden[0], 404)

    def test_the_refusal_carries_none_of_the_owners_identifiers(self) -> None:
        endpoint = self.h.service.store.endpoint_by_id(self.endpoint)
        _status, _h, payload = self.call(
            "POST", f"{PREFIX}/endpoints/{self.endpoint}/verify", OTHER
        )
        rendered = repr(payload)
        for secret in (
            OWNER,
            "owned-svc",
            endpoint.route_path,
            self.engine,
            self.model,
        ):
            with self.subTest(needle=secret):
                self.assertNotIn(secret, rendered)

    def test_the_endpoint_is_untouched_by_a_refused_write(self) -> None:
        before = self.call("GET", f"{PREFIX}/endpoints/{self.endpoint}", OWNER)[2]["data"]
        grantees_before = self.h.service.store.grantees(self.endpoint)
        for method, template, body in self._actions():
            self.call(method, template.format(id=self.endpoint), OTHER, body)
        after = self.call("GET", f"{PREFIX}/endpoints/{self.endpoint}", OWNER)[2]["data"]
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["visibility"], before["visibility"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(self.h.service.store.grantees(self.endpoint), grantees_before)


class ASecurityRefusalNeverBecomesAnOracleTest(_Fixture):
    def test_the_same_refusal_shape_is_returned_for_forbidden_and_missing(self) -> None:
        forbidden = self.call("GET", f"{PREFIX}/endpoints/{self.endpoint}", OTHER)
        missing = self.call("GET", f"{PREFIX}/endpoints/ep_absent_xyz", OTHER)
        # 除了调用方自己写进 URL 的 id 之外，两个响应体逐字节相同。
        f_norm = repr(forbidden[2]).replace(self.endpoint, "<id>")
        m_norm = repr(missing[2]).replace("ep_absent_xyz", "<id>")
        self.assertEqual(f_norm, m_norm)


if __name__ == "__main__":
    unittest.main()
