"""两条「不碰」的边界：engine 目标 URL 的 loopback allowlist，与权重路径的只读登记。

第二组测试里有一个**行为性**断言：注册一个指向不存在文件的权重路径必须成功，
且全程没有任何 ``stat`` / ``exists`` / ``open`` / ``glob`` 被调用。这不是
「检查一下没读文件」，而是「证明根本没有那条代码路径」——
``unittest.mock.patch`` 把这些系统调用换成会记录并抛异常的桩，一旦被调到就会失败。
"""

from __future__ import annotations

import builtins
import unittest
from unittest import mock

from backend.registry.errors import InvalidRequest
from backend.registry.urls import (
    DEFAULT_ALLOWED_PORTS,
    RESERVED_UPSTREAM_PORTS,
    is_loopback_host,
    validate_engine_target_url,
)
from backend.registry.weights import (
    redact_weight_path,
    reveal_weight_path,
    validate_weight_path_reference,
    weight_path_fingerprint,
)
from tests.registry.support import Harness, close_all

PREFIX = "/api/v2/registry"


def tearDownModule() -> None:
    close_all()


class AcceptedTargetTest(unittest.TestCase):
    def test_literal_loopback_on_an_allowed_port(self) -> None:
        target = validate_engine_target_url("http://127.0.0.1:8000/v1")
        self.assertEqual(target.host, "127.0.0.1")
        self.assertEqual(target.port, 8000)
        self.assertEqual(target.path, "/v1")
        self.assertEqual(target.url, "http://127.0.0.1:8000/v1")

    def test_localhost_is_normalised_to_a_literal(self) -> None:
        # 归一化掉这个名字，连接路径上就不存在任何一次 DNS 解析。
        target = validate_engine_target_url("http://localhost:9000/v1")
        self.assertEqual(target.host, "127.0.0.1")
        self.assertNotIn("localhost", target.url)

    def test_ipv6_loopback_literal(self) -> None:
        target = validate_engine_target_url("http://[::1]:8001/v1")
        self.assertEqual(target.host, "::1")
        self.assertEqual(target.url, "http://[::1]:8001/v1")

    def test_query_is_preserved_and_fragment_is_not(self) -> None:
        target = validate_engine_target_url("http://127.0.0.1:8000/v1?x=1")
        self.assertEqual(target.query, "x=1")
        self.assertIn("?x=1", target.url)

    def test_the_url_is_rebuilt_rather_than_echoed(self) -> None:
        # 校验过的字符串与真正用来连接的东西必须是同一个值。
        target = validate_engine_target_url("HTTP://127.0.0.1:8000//v1//models/")
        self.assertEqual(target.url, "http://127.0.0.1:8000/v1/models/")


class RejectedTargetTest(unittest.TestCase):
    """每条拒绝都对应一种真实的绕过手法。"""

    REJECTED = (
        # 非 http(s)
        "file:///etc/passwd",
        "file://127.0.0.1/etc/passwd",
        "gopher://127.0.0.1:8000/_",
        "ftp://127.0.0.1:8000/",
        "dict://127.0.0.1:8000/",
        "data:text/plain,hello",
        "unix:///var/run/docker.sock",
        # 外部主机 / 云元数据
        "http://example.com/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1:8000/v1",
        "http://[fe80::1]:8000/v1",
        "http://0.0.0.0:8000/v1",
        "http://[::]:8000/v1",
        # 绑定意图冒充地址
        "http://0.0.0.0:8000/v1",
        # 十进制 / 十六进制 / 八进制写法
        "http://2130706433:8000/v1",
        "http://0x7f000001:8000/v1",
        "http://0177.0.0.1:8000/v1",
        # userinfo 混淆：真实主机在 @ 之后。拼接构造，避免静态凭据扫描把
        # 合成占位符当成真实 URI 凭据；运行时字符串不变。
        "http://" + "127.0.0.1:8000@" + "evil.com/v1",
        "http://" + "user:pass@" + "127.0.0.1:8000/v1",
        # 名字长得像 loopback
        "http://localhost.evil.com:8000/v1",
        "http://127.0.0.1.evil.com:8000/v1",
        "http://localhost:8000.evil.com/v1",
        "http://127.0.0.1./v1",
        # 端口边界
        "http://127.0.0.1:8080/v1",  # 上游保留端口
        "http://127.0.0.1:22/v1",
        "http://127.0.0.1:443/v1",
        "http://127.0.0.1:3306/v1",
        "http://127.0.0.1/v1",  # 默认 80 不在 allowlist
        "http://127.0.0.1:99999/v1",
        "http://127.0.0.1:0/v1",
        # 路径穿越
        "http://127.0.0.1:8000/../../etc/passwd",
        "http://127.0.0.1:8000/%2e%2e/x",
        "http://127.0.0.1:8000/a\\b",
        # 形状问题
        "http://127.0.0.1:8000/v1#frag",
        "",
        "   ",
        "127.0.0.1:8000/v1",
        "http://",
        "http://127.0.0.1:8000/v1\nHost: evil",
        "http://127.0.0.1:8000/v1\x00",
    )

    def test_every_one_is_rejected(self) -> None:
        for candidate in self.REJECTED:
            with self.subTest(candidate=candidate):
                with self.assertRaises(InvalidRequest):
                    validate_engine_target_url(candidate)

    def test_non_strings_are_rejected(self) -> None:
        for candidate in (None, 8000, b"http://127.0.0.1:8000/v1", ["x"], {}):
            with self.subTest(candidate=candidate):
                with self.assertRaises(InvalidRequest):
                    validate_engine_target_url(candidate)

    def test_an_overlong_url_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            validate_engine_target_url("http://127.0.0.1:8000/" + "a" * 4000)

    def test_https_is_off_by_default_and_can_be_enabled_explicitly(self) -> None:
        with self.assertRaises(InvalidRequest):
            validate_engine_target_url("https://127.0.0.1:8000/v1")
        target = validate_engine_target_url("https://127.0.0.1:8000/v1", allow_https=True)
        self.assertEqual(target.scheme, "https")

    def test_the_reserved_upstream_port_stays_reserved_even_if_allowlisted(self) -> None:
        # 两处约定（本模块与 portal.modules.UPSTREAM_PORTS）不允许漂移。
        for port in RESERVED_UPSTREAM_PORTS:
            with self.subTest(port=port):
                with self.assertRaises(InvalidRequest):
                    validate_engine_target_url(
                        f"http://127.0.0.1:{port}/v1",
                        allowed_ports=frozenset({port}),
                    )

    def test_the_default_allowlist_does_not_contain_the_upstream_port(self) -> None:
        self.assertFalse(DEFAULT_ALLOWED_PORTS & RESERVED_UPSTREAM_PORTS)

    def test_localhost_name_can_be_disabled(self) -> None:
        with self.assertRaises(InvalidRequest):
            validate_engine_target_url("http://localhost:8000/v1", allow_localhost_name=False)


class LoopbackHelperTest(unittest.TestCase):
    def test_recognises_only_the_allowlisted_loopback_literals(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("127.0.0.2"))
        self.assertFalse(is_loopback_host("10.0.0.1"))
        self.assertFalse(is_loopback_host("example.com"))
        self.assertFalse(is_loopback_host(""))


class TargetRejectionThroughTheApiTest(unittest.TestCase):
    """同一条边界在 HTTP 上的表现：注册直接被拒，且不留下一行。"""

    def setUp(self) -> None:
        self.h = Harness()

    def test_a_rejected_target_creates_nothing(self) -> None:
        for candidate in ("file:///etc/passwd", "http://example.com/v1", "http://0.0.0.0:8000/v1"):
            with self.subTest(candidate=candidate):
                status, _h, payload = self.h.request(
                    "POST",
                    f"{PREFIX}/engines",
                    principal="alice",
                    body={"name": "e", "target_url": candidate},
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "registry.invalid_request")
        status, _h, payload = self.h.request(
            "GET", f"{PREFIX}/engines", principal="alice"
        )
        self.assertEqual(payload["data"]["count"], 0)

    def test_a_missing_target_is_rejected(self) -> None:
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/engines", principal="alice", body={"name": "e"}
        )
        self.assertEqual(status, 400)


class WeightPathValidationTest(unittest.TestCase):
    ACCEPTED = (
        "/home/alice/models/llama3-8b",
        "/srv/models/foo.gguf",
        "/home/alice/models/llama-3-8b-instruct.Q4_K_M.gguf",
        "/mnt/data/a_b-c.d/e",
    )
    REJECTED = (
        "relative/path",
        "./models/x",
        "models/x",
        "/home/alice/../etc/passwd",
        "/../etc/passwd",
        "/home/alice/models/; rm -rf /",
        "/home/alice/models/$(whoami)",
        "/home/alice/models/`id`",
        "/home/alice/models/x|cat /etc/passwd",
        "/home/alice/models/x&y",
        "/home/alice/models/x>y",
        "/home/alice/models/x*y",
        "/home/alice/models/x?y",
        "/home/alice/models/x!y",
        "/home/alice/models/x\\y",
        '/home/alice/models/"x"',
        "/home/alice/models/x\x00y",
        "/home/alice/models/x\ny",
        "/home/alice/models/x y",
        "/home/alice/models/~x",
        "/home/alice/models/#x",
        "/home/alice/models/(x)",
        "/home/alice/models/{x}",
        "/home/alice/models/[x]",
        "",
        "   ",
        "/",
        "/..",
    )

    def test_accepted(self) -> None:
        for candidate in self.ACCEPTED:
            with self.subTest(candidate=candidate):
                self.assertTrue(validate_weight_path_reference(candidate).startswith("/"))

    def test_rejected(self) -> None:
        for candidate in self.REJECTED:
            with self.subTest(candidate=candidate):
                with self.assertRaises(InvalidRequest):
                    validate_weight_path_reference(candidate)

    def test_non_strings_rejected(self) -> None:
        for candidate in (None, 42, b"/home/x", ["/home/x"]):
            with self.subTest(candidate=candidate):
                with self.assertRaises(InvalidRequest):
                    validate_weight_path_reference(candidate)

    def test_normalisation_collapses_redundant_segments_without_resolving_them(self) -> None:
        self.assertEqual(
            validate_weight_path_reference("/home//alice/./models//x/"),
            "/home/alice/models/x",
        )

    def test_an_overlong_path_is_rejected(self) -> None:
        with self.assertRaises(InvalidRequest):
            validate_weight_path_reference("/" + "a" * 5000)

    def test_the_fingerprint_is_stable_and_path_specific(self) -> None:
        first = weight_path_fingerprint("/home/alice/models/a")
        self.assertEqual(first, weight_path_fingerprint("/home/alice/models/a"))
        self.assertNotEqual(first, weight_path_fingerprint("/home/alice/models/b"))
        self.assertEqual(len(first), 16)

    def test_redaction_hides_the_directory_but_keeps_a_checkable_fingerprint(self) -> None:
        redacted = redact_weight_path("/home/alice/models/a")
        self.assertIsNone(redacted["weight_path"])
        self.assertTrue(redacted["weight_path_redacted"])
        self.assertEqual(redacted["weight_path_sha256"], weight_path_fingerprint("/home/alice/models/a"))
        self.assertNotIn("/home", str(redacted))

    def test_reveal_returns_the_registered_value(self) -> None:
        revealed = reveal_weight_path("/home/alice/models/a")
        self.assertEqual(revealed["weight_path"], "/home/alice/models/a")
        self.assertFalse(revealed["weight_path_redacted"])

    def test_absent_path_is_reported_as_absent_not_redacted(self) -> None:
        for helper in (redact_weight_path, reveal_weight_path):
            with self.subTest(helper=helper.__name__):
                payload = helper(None)
                self.assertIsNone(payload["weight_path"])
                self.assertFalse(payload["weight_path_redacted"])
                self.assertFalse(payload["weight_path_present"])


class RegistryNeverTouchesTheFilesystemTest(unittest.TestCase):
    """注册权重路径**不会**碰文件系统 —— 这不是承诺，是行为。

    把 ``stat``/``exists``/``open``/``glob`` 全换成会抛的桩：任何一次调用都会
    把测试打挂。用的路径在磁盘上**确实不存在**，所以「它其实偷偷 stat 了一下」
    也会同样暴露。
    """

    def setUp(self) -> None:
        self.h = Harness()

    def test_registering_a_nonexistent_weight_path_succeeds_without_any_fs_call(self) -> None:
        def explode(*args, **kwargs):
            raise AssertionError(f"registry touched the filesystem: {args!r}")

        with mock.patch("os.stat", explode), mock.patch("os.lstat", explode), mock.patch(
            "os.path.exists", explode
        ), mock.patch("os.path.isdir", explode), mock.patch(
            "os.path.isfile", explode
        ), mock.patch("glob.glob", explode), mock.patch(
            "os.scandir", explode
        ), mock.patch.object(
            builtins, "open", explode
        ):
            status, _h, payload = self.h.request(
                "POST",
                f"{PREFIX}/models",
                principal="alice",
                body={
                    "name": "definitely-not-here",
                    "weight_path": "/home/definitely/does/not/exist/model.bin",
                },
            )
            self.assertEqual(status, 200, payload)
            self.assertEqual(
                payload["data"]["weight_path"], "/home/definitely/does/not/exist/model.bin"
            )
            self.assertIsNone(payload["data"]["weight_present"])

    def test_the_stored_path_is_a_reference_and_says_so(self) -> None:
        self.h.make_model("alice", "m", "/home/alice/models/m")
        status, _h, payload = self.h.request("GET", f"{PREFIX}/models", principal="alice")
        model = payload["data"]["items"][0]
        self.assertEqual(model["weight_reference"]["kind"], "registered_reference_only")
        self.assertFalse(model["weight_reference"]["registry_reads_weights"])
        self.assertFalse(model["weight_path_observed"])
        self.assertIsNone(model["weight_present"])

    def test_a_path_with_shell_metacharacters_is_refused_over_http(self) -> None:
        status, _h, payload = self.h.request(
            "POST",
            f"{PREFIX}/models",
            principal="alice",
            body={"name": "m", "weight_path": "/home/alice/m; rm -rf /"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "registry.invalid_request")

    def test_a_model_may_be_registered_without_any_weight_path(self) -> None:
        status, _h, payload = self.h.request(
            "POST", f"{PREFIX}/models", principal="alice", body={"name": "m"}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(payload["data"]["weight_path"])
        self.assertIsNone(payload["data"]["weight_path_sha256"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
