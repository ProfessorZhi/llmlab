"""Issue #68 **D4**：同源 ``invoke`` 必须能被 scoped token 调用，而不是只认 cookie。

#121 的纵切把这条缺口写成了可执行的事实：一个只有 ``Authorization: Bearer``、
**没有** session cookie 的请求，仍然要走 ``_write_preflight``，于是因为没有 CSRF
得到 **403**。结果是「合法 CLI 无法调用」——而 CSRF 保护的从来不是这种请求。

这一组钉住修好之后的分界，并且**每一条拒绝都配一次 Gateway 调用计数**：

| 凭据形态 | 期望 |
| --- | --- |
| 只有有效 Bearer | **200**，转发一次，主体来自 token |
| 只有 session cookie，无 CSRF | **403**，不转发（浏览器面的规则不变） |
| 只有 session cookie + CSRF | **200** |
| cookie **与** Bearer 同时出现 | **400** `credential_confusion_rejected`，不转发 |
| 无效 Bearer | **401**，不转发（「拿个坏 token」换不到任何豁免） |
| 已撤销 Bearer | **401**，不转发 |
| Bearer + 跨源 Origin | **403**，不转发（CLI 不发 Origin，所以「没有」不算失败） |
| Bearer + 超界体 | **413**，不转发 |
| Bearer + 伪造 ``X-UID`` / ``?uid=`` / ``?owner=`` / ``?key=`` / ``?url=`` | **400**，不转发 |

身份来自**真的** ``ScopedTokenService``（真实签发/撤销/内省）接进 BFF 的**真实**
``ScopedTokenPrincipalSource``，不是测试自造的 principal。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from backend.auth import tokens as auth_tokens
from backend.bff import identity as bff_identity
from tests.bff import support as S

INVOKE = "/api/v2/ops/endpoints/{endpoint_id}/invoke"

#: 一次调用需要的 scope。``registry:endpoint:invoke`` 由 Registry 的 ACL 判；
#: 显式签发它，测试才是在证明「token 通道能用」，而不是「恰好有 admin」。
SCOPES = ("registry:endpoint:invoke", "registry:read")

MEMBER_UID = 4001


class _TokenCase(unittest.TestCase):
    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.iam = auth_tokens.ScopedTokenService(
            auth_tokens.TokenStore(tmp / "scoped-tokens.sqlite3"),
            principal_state=auth_tokens.MappingPrincipalState(
                {self.h.principal_of("alice"): auth_tokens.STATE_APPROVED}
            ),
        )
        self.iam.initialize()
        # 真实 IAM 服务接进 BFF 的真实 token 通道（一行装配）。
        self.h.app.token_source = bff_identity.ScopedTokenPrincipalSource(
            introspector=self.iam,
            accounts=self.h.accounts,
            enabled=True,
        )
        self.gateway = _CountingGateway()
        self.h.service.gateway = self.gateway
        self.ref = S.register_baseline(self.h, "alice")
        # Registry P0 调用门要求一次 Gateway 回读确认；补上之后这条端点在
        # 「有门」和「没门」的树上都可以被转发。
        self.h.acknowledge_route(self.ref["endpoint_id"])

    @property
    def path(self) -> str:
        return INVOKE.format(endpoint_id=self.ref["endpoint_id"])

    def issue(self, *, scopes=SCOPES):
        return self.iam.issue(
            principal=self.h.principal_of("alice"),
            uid=MEMBER_UID,
            linux_user="alice",
            scopes=scopes,
            roles=("member",),
            label="d4-test",
        )

    def bearer_client(self):
        return S.WsgiClient(app=self.h.app, host="127.0.0.1:8790", scheme="http")

    def call(self, client, *, headers=None, payload=None):
        return client.run(
            "POST",
            self.path,
            payload=payload if payload is not None else {"messages": []},
            headers=headers or {},
        )


class _CountingGateway:
    """受信形状的 Gateway 替身：记录每次转发。"""

    name = "counting-double"
    attached = True

    def __init__(self):
        self.calls: list[dict] = []

    def forward(self, *, endpoint, deployment, engine, model, payload, requester=None):
        self.calls.append({"principal": getattr(requester, "principal", None)})
        return {
            "request_id": "req_synthetic_token_channel_0001",
            "synthetic": True,
            "gateway": self.name,
            "forwarded": True,
            "served_by": "gateway",
            "http_status": 200,
            "model": endpoint.route_path,
            "body": {"content": "SYNTHETIC"},
            "echo": payload,
            "usage": {"total_tokens": 3},
            "tokens": 3,
            "client_latency_ms": 0.5,
        }


class BearerOnlyTests(_TokenCase):
    def test_a_valid_scoped_token_needs_no_session_cookie_and_no_csrf(self):
        issued = self.issue()
        response = self.call(
            self.bearer_client(), headers={"Authorization": f"Bearer {issued.token}"}
        )
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(len(self.gateway.calls), 1)
        # 主体来自 token 的内省结果，不是请求里的任何字段。
        self.assertEqual(
            self.gateway.calls[0]["principal"], self.h.principal_of("alice")
        )
        data = response.json()["data"]
        self.assertEqual(
            data["result"]["request_id"], "req_synthetic_token_channel_0001"
        )
        # 响应仍然只含元数据：模型正文与请求回显都不下发。
        self.assertNotIn("body", data["result"])
        self.assertNotIn("echo", data["result"])

    def test_an_invalid_token_is_401_and_gets_no_exemption(self):
        """「带了个坏 token」不能换来「跳过 CSRF 然后再落回会话」。"""
        response = self.call(
            self.bearer_client(), headers={"Authorization": "Bearer not-a-real-token"}
        )
        self.assertEqual(response.status, 401, response.json())
        self.assertEqual(response.error_code(), "no_authenticated_session")
        self.assertEqual(self.gateway.calls, [])

    def test_a_revoked_token_is_401_on_the_very_next_call(self):
        issued = self.issue()
        client = self.bearer_client()
        headers = {"Authorization": f"Bearer {issued.token}"}
        self.assertEqual(self.call(client, headers=headers).status, 200)
        self.assertEqual(len(self.gateway.calls), 1)

        self.assertTrue(self.iam.revoke(issued.token), "the token must actually revoke")
        after = self.call(client, headers=headers)
        self.assertEqual(after.status, 401, after.json())
        self.assertEqual(
            len(self.gateway.calls), 1, "a revoked token must not reach the gateway"
        )

    def test_a_cross_origin_origin_header_on_the_token_channel_is_refused(self):
        """CLI 不发 ``Origin``，所以「没有」不是失败；带了跨源的就要拒。"""
        issued = self.issue()
        response = self.call(
            self.bearer_client(),
            headers={
                "Authorization": f"Bearer {issued.token}",
                "Origin": "https://evil.example",
            },
        )
        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(self.gateway.calls, [])

    def test_an_oversized_body_is_413_on_the_token_channel_too(self):
        issued = self.issue()
        big = json.dumps(
            {"messages": [{"content": "x" * (self.h.app.cfg.max_request_bytes + 64)}]}
        )
        response = self.bearer_client().run(
            "POST",
            self.path,
            raw_body=big.encode(),
            headers={
                "Authorization": f"Bearer {issued.token}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(response.status, 413, response.json())
        self.assertEqual(self.gateway.calls, [])

    def test_forged_identity_is_still_refused_on_the_token_channel(self):
        issued = self.issue()
        client = self.bearer_client()
        auth = {"Authorization": f"Bearer {issued.token}"}

        forged_header = self.call(client, headers={**auth, "X-UID": "4003"})
        self.assertEqual(forged_header.status, 400, forged_header.json())
        self.assertEqual(forged_header.error_code(), "identity_assertion_rejected")

        for query in ("uid=4003", "owner=x", "key=other", "url=http://127.0.0.1:8080"):
            with self.subTest(query=query):
                response = client.run(
                    "POST", f"{self.path}?{query}", payload={"messages": []}, headers=auth
                )
                self.assertEqual(response.status, 400, response.json())
                self.assertIn(
                    response.error_code(),
                    ("proxy_assertion_rejected", "source_assertion_rejected"),
                )
        self.assertEqual(self.gateway.calls, [])


class CookieChannelTests(_TokenCase):
    def test_a_cookie_request_still_needs_csrf(self):
        client = self.h.client("alice")
        response = self.call(client)
        self.assertEqual(response.status, 403, response.json())
        self.assertEqual(self.gateway.calls, [])

    def test_a_cookie_request_with_csrf_still_works(self):
        client = self.h.client("alice")
        response = self.call(client, headers={"X-CSRF-Token": S.csrf(client)})
        self.assertEqual(response.status, 200, response.json())
        self.assertEqual(len(self.gateway.calls), 1)


class MixedCredentialTests(_TokenCase):
    def test_cookie_plus_bearer_is_refused_rather_than_silently_downgraded(self):
        """两条凭据通道同时在场时，任何一侧优先都是一次隐式降级。"""
        issued = self.issue()
        client = self.h.client("alice")  # 带 session cookie
        response = self.call(
            client,
            headers={
                "Authorization": f"Bearer {issued.token}",
                "X-CSRF-Token": S.csrf(client),
            },
        )
        self.assertEqual(response.status, 400, response.json())
        self.assertEqual(response.error_code(), "credential_confusion_rejected")
        self.assertEqual(self.gateway.calls, [])


class TokenChannelDisabledTests(unittest.TestCase):
    def test_an_authorization_header_on_a_deployment_without_the_channel_is_401(self):
        """默认部署**没有**启用 token 通道：它不接受这种凭据，也不因此放宽 CSRF。"""
        h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(h.close)
        ref = S.register_baseline(h, "alice")
        h.acknowledge_route(ref["endpoint_id"])
        client = S.WsgiClient(app=h.app, host="127.0.0.1:8790", scheme="http")
        response = client.run(
            "POST",
            INVOKE.format(endpoint_id=ref["endpoint_id"]),
            payload={"messages": []},
            headers={"Authorization": "Bearer anything"},
        )
        self.assertEqual(response.status, 401, response.json())
        self.assertEqual(response.error_code(), "token_auth_not_enabled")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
