"""Strict ``Host`` authority validation for the local preview server.

Regression suite for the defect where ``dev/preview_auth.py`` decided "is this
request addressed to loopback?" by taking everything **before the last colon**
of the ``Host`` header and comparing *that* to a handful of loopback spellings::

    raw  = "127.0.0.1:59008.evil.com"
    host = raw.rsplit(":", 1)[0]        # -> "127.0.0.1"
    ...accepted, 200, and served.

The part after the colon was never examined, so any authority that merely
*started* with a loopback spelling was served. The same one-line rule also

* never checked the port at all (``Host: 127.0.0.1:80``, ``:0``, ``:abc`` and a
  bare ``:`` all passed), and
* silently discarded the port of a legitimate host.

Two further failures are only reachable below the WSGI layer, which is why this
file speaks to a real ``wsgiref`` server over a real loopback socket:

* ``wsgiref`` collapses repeated headers into one comma-joined WSGI value before
  the application runs, so the application cannot tell that two ``Host`` headers
  arrived — the refusal has to happen in the request handler. A ``Host`` header
  repeated once, the second copy empty, reduced to ``"127.0.0.1:59008,"`` and the
  old guard read the loopback address straight out of it;
* a request carrying **no** ``Host`` fell back to ``SERVER_NAME``, which
  ``wsgiref`` fills from ``socket.getfqdn()`` — ``"localhost"`` on some hosts.
  A request that named no authority at all could therefore be served.

Every assertion here is a strict ``200`` / ``400`` comparison, and every case is
driven through the exact spelling that reaches the guard. Nothing in this file
contacts Feishu, the Gateway, Strata or any real service.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

import dev.preview_auth as preview

from tests.integration_auth import support


# --- raw-request helpers -----------------------------------------------------
#
# ``http.client`` will not emit two ``Host`` headers, nor a header value it
# judges malformed, so the layer-specific cases are written out by hand.


def _raw_request(path: str, host_lines: list[bytes], version: bytes = b"HTTP/1.1") -> bytes:
    """A hand-written request with exactly the ``Host`` value lines given.

    Zero ``host_lines`` produces a request with no ``Host`` header at all.
    """
    return _raw_request_lines(path, [b"Host: " + line for line in host_lines], version)


def _raw_request_lines(
    path: str, header_lines: list[bytes], version: bytes = b"HTTP/1.1"
) -> bytes:
    """A hand-written request with exactly the raw header lines given."""
    head = b"GET " + path.encode("ascii") + b" " + version + b"\r\n"
    for line in header_lines:
        head += line + b"\r\n"
    head += b"Connection: close\r\n\r\n"
    return head


def _raw_status(payload: bytes) -> int:
    """The status code of a hand-written response, or a loud failure."""
    if not payload:
        raise AssertionError("the server closed the connection without answering")
    status_line = payload.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise AssertionError(f"unparsable status line: {status_line!r}")
    return int(parts[1])


class _PreviewServerCase(unittest.TestCase):
    """One real preview server per class; every case opens a fresh connection."""

    server: support.PreviewServer
    port: int
    authority: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = support.PreviewServer()
        cls.port = cls.server.port
        cls.authority = f"127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.close()

    # --- asking the question -------------------------------------------------

    def get_with_host(self, host: str, path: str = "/auth/csrf"):
        """A real request whose ``Host`` header is exactly ``host``."""
        return self.server.client().get(path, headers={"Host": host})

    def raw_with_hosts(self, host_lines: list[bytes], path: str = "/auth/csrf") -> int:
        reply = self.server.client().raw_exchange(_raw_request(path, host_lines))
        return _raw_status(reply)

    # --- the two answers -----------------------------------------------------

    def assertServed(self, host: str, path: str = "/auth/csrf") -> None:
        reply = self.get_with_host(host, path)
        self.assertEqual(
            reply.status, 200, f"Host: {host!r} 是本预览的合法权威，应 200，实际 {reply.status}"
        )
        self.assertIn(b"csrf_token", reply.body, f"Host: {host!r} 的响应不是真实路由的响应")

    def assertRefused(self, host: str, path: str = "/auth/csrf") -> None:
        reply = self.get_with_host(host, path)
        self.assertEqual(
            reply.status, 400, f"Host: {host!r} 必须被拒绝（400），实际 {reply.status}"
        )
        # 拒绝必须发生在任何路由之前：正文里不能出现任何路由的结果。
        self.assertNotIn(b"csrf_token", reply.body, f"Host: {host!r} 仍然触达了真实路由")
        self.assertEqual(reply.error_code(), "invalid_request", f"Host: {host!r} 的错误码")


# ===========================================================================
# 1. The three loopback spellings this preview answers to
# ===========================================================================


class AcceptedAuthorityTests(_PreviewServerCase):
    def test_the_three_loopback_spellings_with_the_real_port(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertServed(f"{host}:{self.port}")

    def test_the_three_loopback_spellings_without_a_port(self):
        # 未声明端口时不比对端口：RFC 7230 的 Host = uri-host [":" port] 里端口
        # 是可选的，而"这个请求是不是发给我们这台环回预览的"由 hostname 决定。
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertServed(host)

    def test_the_hostname_is_matched_case_insensitively(self):
        # uri-host 的 reg-name 大小写不敏感（RFC 3986 §3.2.2）。
        for host in ("LOCALHOST", "LocalHost", "localHOst"):
            with self.subTest(host=host):
                self.assertServed(f"{host}:{self.port}")


# ===========================================================================
# 2. The reported defect: a loopback *prefix* is not the loopback authority
# ===========================================================================


class AuthoritySuffixTests(_PreviewServerCase):
    """The exact shapes that the ``rsplit(":", 1)[0]`` rule used to serve."""

    def test_a_loopback_prefix_with_an_appended_suffix_is_refused(self):
        # 缺陷本体：旧代码在最后一个冒号处切开，只看前半段，于是这一整类都被放行。
        for host in (
            f"127.0.0.1:{self.port}.evil.com",
            f"127.0.0.1:{self.port}.attacker.example",
            f"localhost:{self.port}.evil.com",
            f"[::1]:{self.port}.evil.com",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)

    def test_a_bare_loopback_lookalike_name_is_refused(self):
        for host in (
            f"127.0.0.1.evil.com:{self.port}",
            f"localhost.evil.com:{self.port}",
            f"127.0.0.1.nip.io:{self.port}",
            f"127.0.0.1.attacker.example:{self.port}",
            f"notlocalhost:{self.port}",
            f"localhosts:{self.port}",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)

    def test_a_comma_separated_authority_list_is_refused(self):
        # 逗号列表是"一个字段里塞了两个权威"的经典形态，无论站在哪一侧解析都不该被接受。
        for host in (
            f"127.0.0.1:{self.port},evil.com",
            f"evil.com,127.0.0.1:{self.port}",
            f"127.0.0.1:{self.port},",
            f",127.0.0.1:{self.port}",
            f"localhost:{self.port},localhost:{self.port}",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)

    def test_userinfo_in_the_authority_is_refused(self):
        # URI 的 userinfo（``user@host``）不属于 Host 头字段，也不该被剥掉后接受。
        for host in (
            f"admin@127.0.0.1:{self.port}",
            f"@127.0.0.1:{self.port}",
            f"127.0.0.1:{self.port}@evil.com",
            f"user:pass@127.0.0.1:{self.port}",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)

    def test_trailing_dots_and_stray_separators_are_refused(self):
        for host in (
            f"127.0.0.1.:{self.port}",
            f"localhost.:{self.port}",
            f"127.0.0.1:{self.port}.",
            f"127.0.0.1:{self.port}/evil",
            f"127.0.0.1:{self.port} evil.com",
            f"127.0.0.1 :{self.port}",
            f"127.0.0.1:{self.port}:80",
            f"127.0.0.1:{self.port};evil",
            f"127.0.0.1:{self.port}?evil",
            f"127.0.0.1:{self.port}#evil",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)

    def test_percent_encoding_is_refused(self):
        # 不做任何百分号解码：解码会让"检查的字符串"与"使用的字符串"变成两个。
        for host in (
            f"127.0.0.1%00.evil.com:{self.port}",
            f"127.0.0.1:{self.port}%00",
            f"%31%32%37.0.0.1:{self.port}",
            f"local%68ost:{self.port}",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)

    def test_alternative_ipv4_and_ipv6_encodings_are_refused(self):
        # 这些写法在"先规范化再比较"的实现里会解析回 127.0.0.1；本预览只认字面量。
        for host in (
            f"2130706433:{self.port}",
            f"0x7f000001:{self.port}",
            f"0177.0.0.1:{self.port}",
            f"127.1:{self.port}",
            f"127.0.0.1.:{self.port}",
            f"::1:{self.port}",
            f"[::2]:{self.port}",
            f"[0:0:0:0:0:0:0:1]:{self.port}",
            f"[::ffff:127.0.0.1]:{self.port}",
            f"[127.0.0.1]:{self.port}",
        ):
            with self.subTest(host=host):
                self.assertRefused(host)


# ===========================================================================
# 3. The port, when it is stated, has to be the port we are listening on
# ===========================================================================


class AuthorityPortTests(_PreviewServerCase):
    @property
    def a_different_valid_port(self) -> int:
        return (self.port % 65535) + 1

    def test_a_port_that_is_not_the_listening_port_is_refused(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertRefused(f"{host}:{self.a_different_valid_port}")

    def test_the_default_and_privileged_ports_are_refused(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            for port in (80, 443, 8080, 3000):
                with self.subTest(host=host, port=port):
                    self.assertRefused(f"{host}:{port}")

    def test_a_port_outside_the_legal_range_is_refused(self):
        for port in (0, 65536, 99999, 999999):
            with self.subTest(port=port):
                self.assertRefused(f"127.0.0.1:{port}")

    def test_a_port_that_is_not_plain_ascii_digits_is_refused(self):
        for suffix in ("abc", "12a", "0x50", " 59008", "59008 ", "+59008", "59008.0", ""):
            with self.subTest(suffix=suffix):
                self.assertRefused(f"127.0.0.1:{suffix}")


# ===========================================================================
# 4. Structure: repeated Host headers, and no Host at all
# ===========================================================================


class DuplicateHostHeaderTests(_PreviewServerCase):
    """``wsgiref`` merges repeated headers; the refusal must happen before that."""

    def test_two_identical_host_headers_are_refused(self):
        authority = self.authority.encode("ascii")
        self.assertEqual(
            self.raw_with_hosts([authority, authority]),
            400,
            "重复的 Host 头必须 400（RFC 7230 §5.4），而不是被逗号黏成一个值",
        )

    def test_a_second_empty_host_header_cannot_smuggle_a_loopback_prefix(self):
        # 这是旧实现真正的失守点：两个头黏成 ``"127.0.0.1:59008,"``，而
        # ``rsplit(":", 1)[0]`` 从里面读出了 ``127.0.0.1`` 并放行。
        authority = self.authority.encode("ascii")
        self.assertEqual(self.raw_with_hosts([authority, b""]), 400)

    def test_a_loopback_host_followed_by_a_foreign_host_is_refused(self):
        authority = self.authority.encode("ascii")
        for second in (b"evil.com", b"evil.com:80", b"127.0.0.1:59008.evil.com"):
            with self.subTest(second=second):
                self.assertEqual(self.raw_with_hosts([authority, second]), 400)

    def test_a_foreign_host_followed_by_a_loopback_host_is_refused(self):
        authority = self.authority.encode("ascii")
        for first in (b"evil.com", b"127.0.0.1:59008.evil.com", b""):
            with self.subTest(first=first):
                self.assertEqual(self.raw_with_hosts([first, authority]), 400)

    def test_differently_cased_repeats_are_still_repeats(self):
        # 头字段名大小写不敏感：``Host`` 与 ``host`` 是同一个字段的两次出现。
        authority = self.authority.encode("ascii")
        payload = self.server.client().raw_exchange(
            _raw_request_lines(
                "/auth/csrf", [b"Host: " + authority, b"host: 127.0.0.1:1"]
            )
        )
        self.assertEqual(_raw_status(payload), 400)


class MissingHostHeaderTests(_PreviewServerCase):
    """A request that names no authority is refused rather than guessed at.

    HTTP/1.1 requires ``Host`` (RFC 7230 §5.4) and the base handler would enforce
    that — except ``wsgiref``'s request handler pins ``protocol_version`` to
    ``HTTP/1.0``, which switches the check off even for an HTTP/1.1 request line.
    HTTP/1.0 makes ``Host`` optional; this preview still refuses it, because the
    guard *is* the only authority check this container has and a request it
    cannot check is a request it must not answer. See
    ``docs/preview-host-security.md``.
    """

    def test_an_http_1_1_request_without_host_is_refused(self):
        self.assertEqual(self.raw_with_hosts([]), 400)

    def test_an_http_1_0_request_without_host_is_refused(self):
        payload = self.server.client().raw_exchange(
            _raw_request("/auth/csrf", [], version=b"HTTP/1.0")
        )
        self.assertEqual(_raw_status(payload), 400)

    def test_an_empty_host_header_is_refused(self):
        # ``Host:`` 空值绝不能退回到 ``SERVER_NAME``——wsgiref 把它填成
        # ``socket.getfqdn()``，在某些主机上正好是 ``localhost``。
        self.assertEqual(self.raw_with_hosts([b""]), 400)


class MalformedRequestShapeTests(_PreviewServerCase):
    """Shapes the ``Host`` value can arrive folded or wrapped in."""

    def test_an_obsolete_line_folded_host_is_refused(self):
        # ``Host: 127.0.0.1:<port>`` 后面跟一行以空格开头的续行（RFC 7230 已废弃的
        # obs-fold）。解析后字段值里带着续行内容，而旧守卫的 ``rsplit(":", 1)[0]``
        # 仍然从里面读出了 ``127.0.0.1``。
        raw = (
            "GET /auth/csrf HTTP/1.1\r\n"
            f"Host: {self.authority}\r\n"
            " evil.com\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        payload = self.server.client().raw_exchange(raw)
        self.assertEqual(_raw_status(payload), 400)

    def test_an_absolute_form_request_target_does_not_reach_a_route(self):
        # RFC 7230 §5.4：请求行是 absolute-form 时权威来自请求行。本预览不据此
        # 路由——该形态一律落到 404，绝不因为 Host 合法就被当成正常路径服务。
        raw = (
            "GET http://evil.com/auth/csrf HTTP/1.1\r\n"
            f"Host: {self.authority}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        payload = self.server.client().raw_exchange(raw)
        self.assertEqual(_raw_status(payload), 404)
        self.assertNotIn(b"csrf_token", payload)


class NonAsciiHostHeaderTests(_PreviewServerCase):
    def test_fullwidth_digit_and_letter_spellings_are_refused(self):
        # 全角数字/字母、Unicode 同形字都不该在"看起来像"之后被接受。
        for raw in (
            "１２７.０.０.１:%d" % self.port,
            "ｌｏｃａｌｈｏｓｔ:%d" % self.port,
            "ⅼocalhost:%d" % self.port,  # U+217C ROMAN NUMERAL SMALL LETTER L
            "localhost。%d" % self.port,  # U+3002 IDEOGRAPHIC FULL STOP
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.raw_with_hosts([raw.encode("utf-8")]), 400)

    def test_control_characters_in_the_host_value_are_refused(self):
        authority = self.authority.encode("ascii")
        for raw in (
            authority + b"\x00",
            authority + b"\x01",
            authority + b"\x7f",
            b"\x00" + authority,
            b"127.0.0.1\x00.evil.com:%d" % self.port,
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.raw_with_hosts([raw]), 400)


# ===========================================================================
# 5. The guard is a guard of the whole container, not of one path
# ===========================================================================


class HostGuardAppliesToEveryRouteTests(_PreviewServerCase):
    """The same check has to cover the product router and the protected page.

    ``/auth/*`` is served by ``backend.auth.routes.Application`` and ``/atlas``
    by the DEMO branch; if the host check only covered the static surface, a
    rejected authority would still reach the login page and the CSRF endpoint.
    """

    PATHS = ("/auth/csrf", "/login", "/atlas", "/")

    def test_a_legitimate_authority_reaches_every_route(self):
        # 合法权威在这些路径上必须走到各自真实的处理分支，而不是被 Host 守卫拦下：
        # 静态页与 CSRF 端点 200，受保护页在匿名时 302 到登录页。
        expected = {"/auth/csrf": 200, "/login": 200, "/atlas": 302, "/": 302}
        for path, status in expected.items():
            with self.subTest(path=path):
                reply = self.get_with_host(self.authority, path)
                self.assertEqual(reply.status, status, f"{path} 未被合法权威送达")

    def test_a_prefixed_authority_is_refused_on_every_route(self):
        bad = f"127.0.0.1:{self.port}.evil.com"
        for path in self.PATHS:
            with self.subTest(path=path):
                self.assertRefused(bad, path)

    def test_a_foreign_authority_is_refused_on_every_route(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                self.assertRefused("portal.example.com", path)

    def test_a_wrong_port_is_refused_on_every_route(self):
        bad = f"127.0.0.1:{self.port % 65535 + 1}"
        for path in self.PATHS:
            with self.subTest(path=path):
                self.assertRefused(bad, path)

    def test_a_repeated_host_header_is_refused_on_every_route(self):
        authority = self.authority.encode("ascii")
        for path in self.PATHS:
            with self.subTest(path=path):
                self.assertEqual(self.raw_with_hosts([authority, authority], path), 400)


# ===========================================================================
# 6. The decision itself, driven without a socket
# ===========================================================================


class ParseHostAuthorityTests(unittest.TestCase):
    """The allowlist as a pure function: one spelling in, one answer out."""

    def test_it_returns_the_canonical_host_and_port(self):
        for raw, expected in (
            ("127.0.0.1:59008", ("127.0.0.1", 59008)),
            ("localhost:59008", ("localhost", 59008)),
            ("[::1]:59008", ("::1", 59008)),
            ("LOCALHOST:59008", ("localhost", 59008)),
            ("127.0.0.1", ("127.0.0.1", None)),
            ("localhost", ("localhost", None)),
            ("[::1]", ("::1", None)),
            ("127.0.0.1:1", ("127.0.0.1", 1)),
            ("127.0.0.1:65535", ("127.0.0.1", 65535)),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(preview.parse_host_authority(raw), expected)

    def test_it_returns_nothing_for_every_other_spelling(self):
        for raw in (
            "",
            "portal.example.com",
            "127.0.0.1:59008.evil.com",
            "127.0.0.1:59008,evil.com",
            "127.0.0.1:59008 evil.com",
            "127.0.0.1.1",
            "127.0.0.1.",
            "127.0.0.1:",
            "127.0.0.1:0",
            "127.0.0.1:65536",
            "127.0.0.1:123456",
            "127.0.0.1:80a",
            "127.0.0.1:+80",
            "127.0.0.1: 80",
            "127.0.0.1:59008:80",
            "admin@127.0.0.1:59008",
            "::1:59008",
            "[::1",
            "::1]",
            "[::1]:59008.evil.com",
            "127.0.0.1%00",
            "１２７.０.０.１:59008",
            "localhost。",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(preview.parse_host_authority(raw), raw)


class ApplicationLevelHostGuardTests(unittest.TestCase):
    """The same refusal, with the ``environ`` spelled out by hand.

    One shape cannot be produced by a socket at all: a request that names **no**
    authority while ``SERVER_NAME`` happens to be ``localhost``. ``wsgiref``
    fills ``SERVER_NAME`` from ``socket.getfqdn()``, so whether the old guard's
    fallback accepted such a request depended on the machine's FQDN — on this
    host it does not, so the socket-level test above passes for the wrong reason.
    Driving the application directly makes the case deterministic everywhere.
    """

    PORT = 59008

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-preview-env-"))
        cls.app = preview.build_preview_application(cls.root / "state", local_http_dev=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def status(self, **overrides) -> int:
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/auth/csrf",
            "QUERY_STRING": "",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": str(self.PORT),
        }
        environ.update(overrides)

        seen: dict[str, int] = {}

        def start_response(status, _headers, _exc_info=None):
            seen["status"] = int(status.split(" ", 1)[0])

        b"".join(self.app(environ, start_response))
        return seen["status"]

    def test_a_request_with_no_host_at_all_is_refused(self):
        # 请求根本没声明权威，即便 SERVER_NAME 恰好是 localhost 也不能被当成环岛请求。
        self.assertEqual(self.status(), 400)

    def test_server_name_cannot_rescue_a_foreign_host_header(self):
        self.assertEqual(
            self.status(HTTP_HOST="portal.example.com", SERVER_NAME="localhost"), 400
        )

    def test_the_guards_own_port_is_the_one_that_was_listened_on(self):
        authority = f"127.0.0.1:{self.PORT}"
        self.assertEqual(self.status(HTTP_HOST=authority), 200)
        self.assertEqual(self.status(HTTP_HOST=f"127.0.0.1:{self.PORT + 1}"), 400)
        self.assertEqual(self.status(HTTP_HOST=authority, SERVER_PORT=str(self.PORT + 1)), 400)

    def test_an_unbound_port_cannot_be_verified_and_is_refused(self):
        # 没有 SERVER_PORT ⇒ 无法核对端口 ⇒ 失败关闭，而不是跳过检查。
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/auth/csrf",
            "QUERY_STRING": "",
            "HTTP_HOST": f"127.0.0.1:{self.PORT}",
        }
        seen: dict[str, int] = {}

        def start_response(status, _headers, _exc_info=None):
            seen["status"] = int(status.split(" ", 1)[0])

        b"".join(self.app(environ, start_response))
        self.assertEqual(seen["status"], 400)


__all__ = [
    "AcceptedAuthorityTests",
    "ApplicationLevelHostGuardTests",
    "AuthorityPortTests",
    "AuthoritySuffixTests",
    "DuplicateHostHeaderTests",
    "HostGuardAppliesToEveryRouteTests",
    "MalformedRequestShapeTests",
    "MissingHostHeaderTests",
    "NonAsciiHostHeaderTests",
    "ParseHostAuthorityTests",
]
