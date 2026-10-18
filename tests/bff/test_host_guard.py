"""``Host`` 权威守卫：一个不是发给我们的请求，不会被我们服务。

这一层在身份之前。顺序是刻意的：一个寄给别的主机名的请求，不该让我们的会话表
看上一眼，更不该有机会走到任何一条会写东西的路径。所以这里除了「拒绝」，还断言
**拒绝发生在认证之前**——带着一条完全有效会话 cookie 的请求，只要权威不对，答案
仍然是 421，而不是 200。

用例表与 ``dev/preview_auth.py`` 共享一份语义（见
:class:`AuthorityParityTests`）：那一边是 Issue #8 修出来的、已经有实测用例的版本，
这边是把它推广成「可配置权威集合」的版本。两边对同一组输入必须给出同样的结论，
否则「同一条规则」就变成了两条会各自漂移的规则。
"""

from __future__ import annotations

import tempfile
import unittest

from backend.bff import origin as O
from dev import preview_auth as preview
from tests.bff import support as S

LOOPBACK_OK = ("127.0.0.1", "127.0.0.1:8790", "localhost:8790", "LOCALHOST:8790", "[::1]:8790")

#: 畸形 / 走私 / 外部权威。预览与 BFF 的**解析器**都必须拒。
REJECTED_BY_PARSER = (
    "",
    " ",
    "127.0.0.1:59008.evil.com",
    "127.0.0.1:59008,evil.com",
    "127.0.0.1:59008 evil.com",
    "127.0.0.1:59008/evil",
    "127.0.0.1:",
    "127.0.0.1:abc",
    "127.0.0.1:0",
    "127.0.0.1:65536",
    "127.0.0.1:80 ",
    " 127.0.0.1:8790",
    "::1",
    "[::1",
    "127.0.0.1:8790\n",
    "999.1.1.1",
    "127.0.0.1:1234567",
)


class ParseAuthorityTests(unittest.TestCase):
    def test_the_loopback_spellings_parse_and_canonicalise(self):
        for raw in ("127.0.0.1", "127.0.0.1:8790", "localhost", "[::1]:8790"):
            with self.subTest(raw=raw):
                parsed = O.parse_authority(raw)
                self.assertIsNotNone(parsed, raw)
                self.assertIn(parsed.host, O.LOOPBACK_HOSTS)

    def test_a_general_host_name_parses_too(self):
        """BFF 比预览多一种部署形态：反代后面的真实主机名。"""
        parsed = O.parse_authority("portal.example.com:8443")
        self.assertEqual(parsed.host, "portal.example.com")
        self.assertEqual(parsed.port, 8443)

    def test_hosts_are_case_insensitive_but_only_ascii_case(self):
        self.assertEqual(O.parse_authority("LOCALHOST").host, "localhost")
        self.assertEqual(O.parse_authority("Portal.Example.COM").host, "portal.example.com")
        # 全角数字与 Unicode 里「长得像」的写法都不是端口、也不是主机名。
        self.assertIsNone(O.parse_authority("127.0.0.1:１２３"))
        self.assertIsNone(O.parse_authority("evil．com"))

    def test_malformed_and_foreign_values_are_rejected_by_the_parser(self):
        for raw in REJECTED_BY_PARSER:
            with self.subTest(raw=raw):
                self.assertIsNone(O.parse_authority(raw))

    def test_a_duplicate_host_header_arrives_glued_and_is_refused(self):
        """传输层把重复的头黏成 ``a, b``；逗号不是主机名字符，于是自然被拒。"""
        self.assertIsNone(O.parse_authority("127.0.0.1:8790, evil.com"))
        self.assertIsNone(O.parse_authority("127.0.0.1:8790,"))


class AuthorityParityTests(unittest.TestCase):
    """与 ``dev/preview_auth.parse_host_authority`` 的**共享语义**。

    预览只服务环回，所以它的解析器对任何非环回的合法主机名也说 ``None``；BFF 的
    解析器要认得反代后面的主机名。因此可比较的那部分是：

    * 环回写法：两边都必须接受，且主机名规范化一致；
    * 畸形/走私写法：两边都必须拒绝——这是 Issue #8 修掉的那一类。
    """

    def test_loopback_agreement(self):
        for raw in LOOPBACK_OK:
            with self.subTest(raw=raw):
                theirs = preview.parse_host_authority(raw)
                ours = O.parse_authority(raw)
                self.assertIsNotNone(theirs, raw)
                self.assertIsNotNone(ours, raw)
                self.assertEqual(ours.host, theirs[0])
                self.assertEqual(ours.port, theirs[1])

    def test_malformed_agreement(self):
        for raw in REJECTED_BY_PARSER:
            with self.subTest(raw=raw):
                self.assertIsNone(preview.parse_host_authority(raw), raw)
                self.assertIsNone(O.parse_authority(raw), raw)


class AuthorityPolicyTests(unittest.TestCase):
    def test_the_default_policy_is_loopback_only(self):
        policy = O.AuthorityPolicy()
        self.assertIsNone(policy.check("127.0.0.1", server_port="8790"))
        self.assertIsNone(policy.check("127.0.0.1:8790", server_port="8790"))
        for raw in ("portal.example.com", "laptop.local", "10.0.0.5"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(policy.check(raw, server_port="8790"))

    def test_a_configured_authority_is_accepted_and_everything_else_is_not(self):
        policy = O.AuthorityPolicy.build(["portal.example.com:8443"])
        self.assertIsNone(policy.check("portal.example.com:8443", server_port="8443"))
        self.assertIsNotNone(policy.check("portal.example.com:9999", server_port="9999"))
        self.assertIsNotNone(policy.check("old.portal.example.com", server_port="8443"))

    def test_a_bare_host_entry_accepts_any_port_but_a_ported_entry_does_not(self):
        bare = O.AuthorityPolicy.build(["portal.example.com"])
        self.assertIsNone(bare.check("portal.example.com", server_port="443"))
        self.assertIsNone(bare.check("portal.example.com:8443", server_port="8443"))

    def test_a_stated_port_must_match_the_port_the_request_arrived_on(self):
        policy = O.AuthorityPolicy()
        refusal = policy.check("127.0.0.1:9999", server_port="8790")
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.reason, O.REASON_PORT_MISMATCH)

    def test_an_unknowable_actual_port_fails_rather_than_skips(self):
        """无法核对的声明不是「核对通过」。"""
        refusal = O.AuthorityPolicy().check("127.0.0.1:8790", server_port=None)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.reason, O.REASON_PORT_MISMATCH)

    def test_a_missing_host_header_is_refused(self):
        for raw in (None, ""):
            with self.subTest(raw=raw):
                refusal = O.AuthorityPolicy().check(raw, server_port="8790")
                self.assertEqual(refusal.reason, O.REASON_MISSING_HOST)

    def test_a_malformed_host_header_is_refused(self):
        refusal = O.AuthorityPolicy().check(
            "127.0.0.1:8790.evil.com", server_port="8790"
        )
        self.assertEqual(refusal.reason, O.REASON_MALFORMED_HOST)

    def test_an_unconfigured_authority_is_refused_as_foreign(self):
        refusal = O.AuthorityPolicy().check("stale-portal.internal", server_port="8790")
        self.assertEqual(refusal.reason, O.REASON_FOREIGN_HOST)

    def test_a_configuration_entry_that_cannot_be_parsed_raises(self):
        """配置错误必须在启动时说出来，而不是运行时静默地少信一条。"""
        for bad in ("", "  ", "evil.com/path", "127.0.0.1:0", "a b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    O.AuthorityPolicy.build([bad])


class HostGuardOverWsgiTests(unittest.TestCase):
    """从真正的 WSGI 入口发请求——这才是线上走的那条路。"""

    def setUp(self):
        self.h = S.build_harness(state_dir=tempfile.mkdtemp())
        self.addCleanup(self.h.close)

    def _client(self, host: str, **kwargs):
        return S.WsgiClient(app=self.h.app, host=host, scheme="http", **kwargs)

    def test_the_served_authority_still_works_when_signed_in(self):
        response = self.h.client("alice").run("GET", "/api/v2/ops")
        self.assertEqual(response.status, 200)

    def test_an_unknown_host_is_421_even_with_a_valid_session_cookie(self):
        """权威校验在身份之前：这正是「不是发给我们的请求」的含义。"""
        client = self.h.client("alice")
        self.assertIn(self.h.app.cfg.cookie_name, client.cookies)
        client.host = "stale-portal.internal"
        response = client.run("GET", "/api/v2/ops")
        self.assertEqual(response.status, 421)
        self.assertEqual(response.error_code(), O.REASON_FOREIGN_HOST)
        self.assertNotIn(b"ops", response.body)

    def test_a_smuggled_authority_is_421(self):
        for host in (
            "127.0.0.1:8790.evil.com",
            "127.0.0.1:8790,evil.com",
            "127.0.0.1:8790 evil.com",
        ):
            with self.subTest(host=host):
                response = self._client(host).run("GET", "/auth/admin/status")
                self.assertEqual(response.status, 421)
                self.assertEqual(response.error_code(), O.REASON_MALFORMED_HOST)

    def test_a_stale_port_is_421(self):
        """头里写的端口不是请求实际到达的端口——包括「曾经对过」的那个。"""
        client = self._client("127.0.0.1:8790")
        response = client.run(
            "GET", "/api/v2/ops", headers={"Host": "127.0.0.1:9999"}
        )
        self.assertEqual(response.status, 421)
        self.assertEqual(response.error_code(), O.REASON_PORT_MISMATCH)

    def test_a_missing_host_header_is_421(self):
        response = self._client("").run("GET", "/api/v2/ops")
        self.assertEqual(response.status, 421)
        self.assertEqual(response.error_code(), O.REASON_MISSING_HOST)

    def test_a_request_with_neither_host_nor_server_port_cannot_skip_the_guard(self):
        """environ 里两个键都不在时，守卫**不能**被跳过。

        这不是假想的形状：任何「只有 PATH_INFO 和 wsgi.input」的最小 environ 都是
        这样。用「参数不是 None 才检查」来判定的实现会让这种请求直接绕过整道守卫——
        这里从一个真的 environ 发起，断言它得到 421 而不是被服务。
        """
        import io
        import json

        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/v2/ops",
            "QUERY_STRING": "",
            "wsgi.input": io.BytesIO(b""),
        }
        captured: dict = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            return lambda chunk: None

        body = b"".join(self.h.app(environ, start_response))
        self.assertTrue(
            captured["status"].startswith("421"), captured.get("status")
        )
        self.assertEqual(json.loads(body)["error"]["code"], O.REASON_MISSING_HOST)

    def test_the_in_process_entry_point_keeps_the_guard_opt_out_explicit(self):
        """``handle()`` 省略权威参数才跳过检查；传 ``None`` 是「缺失」，不是「跳过」。"""
        from backend.auth.wsgi import build_request

        request = build_request(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/api/v2/ops",
                "QUERY_STRING": "",
                "HTTP_HOST": "127.0.0.1:8790",
                "SERVER_PORT": "8790",
                "wsgi.input": __import__("io").BytesIO(b""),
            },
            max_body=1024,
        )
        # 传了 None（= environ 里没有那个键）→ 拒绝。
        self.assertEqual(self.h.app.handle(request, host_header=None).status, 421)
        # 省略 → 进程内调用路径，跳过权威校验。
        self.assertEqual(self.h.app.handle(request).status, 401)

    def test_the_refusal_happens_before_any_write_is_even_looked_at(self):
        """一次权威不成立的写请求不会改动任何东西。"""
        client = self.h.client("alice")
        ids = S.register_baseline(self.h, "alice")
        # 先在**正确的**权威上取 CSRF：这份凭据是真的、没过期、也没被用掉，
        # 于是这条用例唯一被否掉的变量就是 Host 本身。
        token = S.csrf(client)
        client.host = "stale-portal.internal"
        response = client.run(
            "PUT",
            f"/api/v2/ops/deployments/{ids['deployment_id']}/share",
            payload={"mode": "all", "principals": []},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status, 421)
        self.assertEqual(
            self.h.service.store.endpoint_by_id(ids["endpoint_id"]).visibility,
            "private",
        )

    def test_a_configured_authority_is_served_when_it_matches_the_port(self):
        app = S.build_harness(state_dir=tempfile.mkdtemp()).app
        from backend.bff.origin import AuthorityPolicy

        app.authority_policy = AuthorityPolicy.build(
            ["portal.example.com:8443"], allow_loopback=False
        )
        ok = S.WsgiClient(app=app, host="portal.example.com:8443", scheme="http")
        self.assertNotEqual(ok.run("GET", "/auth/admin/status").status, 421)
        # 环回也被这条策略关掉了：一个部署要么认它被服务的那一个权威，要么不。
        loopback = S.WsgiClient(app=app, host="127.0.0.1:8443", scheme="http")
        self.assertEqual(loopback.run("GET", "/auth/admin/status").status, 421)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
