"""Socket-level tests for the WSGI request-body reader.

``tests.auth.support.WsgiClient`` hands ``io.BytesIO`` to ``wsgi.input``, and
``BytesIO.read(n)`` simply returns whatever happens to be left in the buffer. A
real server hands the application a *socket-backed* reader instead, and
``read(n)`` on one of those blocks until ``n`` octets have arrived or the peer
closes the connection. So a read performed "just in case" on a request that
declared no body hangs the request until the client gives up. A browser ``GET``
is exactly that request: no ``Content-Length`` and no body.

Two layers cover the contract:

* :class:`DeclaredLengthTests` unit-tests :func:`backend.auth.wsgi.read_body`
  against a recording stream, and asserts the stream is *never touched* for a
  request whose length was not declared — the strongest available statement of
  "this read cannot block".
* :class:`LoopbackServerTests` drives the whole application over a genuine
  ``127.0.0.1`` socket (``wsgiref`` server + ``http.client``), which is the only
  way to observe the blocking behaviour end to end, and pins the HTTP status
  codes for each body shape.

No test here ever adds ``Content-Length: 0`` to a request whose entire point is
that it has none. Every credential is a synthetic value defined in this file.
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import threading
import unittest
from dataclasses import dataclass
from socketserver import ThreadingMixIn

from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from backend.auth import bootstrap, routes, wsgi
from tests.auth import support
from tests.auth.test_bootstrap import ScriptedReader
from tests.auth.test_store import FakeClock

#: A fictional administrator passphrase. It exists only inside this process and
#: is unrelated to any real deployment credential.
SYNTHETIC_ADMIN_PASSWORD = "synthetic-wsgi-socket-passphrase-1"

SESSION_COOKIE = "llm_lab_session"

#: Client-side ceiling for every exchange. A read that must not block has to
#: finish far below this; the value is only long enough to be unambiguous.
CLIENT_TIMEOUT = 3.0


class RecordingStream:
    """A ``wsgi.input`` double that hands out bytes and records every read.

    The recorded ``read()`` sizes are the evidence: an empty ``reads`` list
    proves the application never reached for the stream at all, which is exactly
    what a socket-backed reader requires when no body was declared.
    """

    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        if not self.payload:
            return b""
        chunk, self.payload = self.payload[:size], self.payload[size:]
        return chunk


class DeclaredLengthTests(unittest.TestCase):
    """``read_body`` must read only what the server declared, never more."""

    MAX = 64

    def stream(self, payload: bytes = b"") -> RecordingStream:
        return RecordingStream(payload)

    def read(self, stream: RecordingStream, **environ_extra) -> bytes:
        return wsgi.read_body({"wsgi.input": stream, **environ_extra}, self.MAX)

    # --- nothing declared: the stream stays untouched -----------------------

    def test_missing_wsgi_input_is_an_empty_body(self):
        self.assertEqual(wsgi.read_body({}, self.MAX), b"")

    def test_absent_content_length_never_reads_the_stream(self):
        stream = self.stream()
        self.assertEqual(self.read(stream), b"")
        self.assertEqual(stream.reads, [])

    def test_absent_content_length_ignores_bytes_already_buffered(self):
        # A body that arrived without a declared length (decoded chunked, a
        # pipelined leftover, ...) must not be guessed at: fail closed instead.
        stream = self.stream(b'{"username": "admin"}')
        self.assertEqual(self.read(stream), b"")
        self.assertEqual(stream.reads, [])

    def test_empty_content_length_never_reads_the_stream(self):
        stream = self.stream()
        self.assertEqual(self.read(stream, CONTENT_LENGTH=""), b"")
        self.assertEqual(stream.reads, [])

    def test_zero_content_length_never_reads_the_stream(self):
        stream = self.stream()
        self.assertEqual(self.read(stream, CONTENT_LENGTH="0"), b"")
        self.assertEqual(stream.reads, [])

    def test_malformed_content_length_never_reads_the_stream(self):
        for raw in ("not-a-length", "12.5", "1_0", "+5", "0x10", " "):
            with self.subTest(content_length=raw):
                stream = self.stream(b"ignored")
                self.assertEqual(self.read(stream, CONTENT_LENGTH=raw), b"")
                self.assertEqual(stream.reads, [])

    def test_negative_content_length_never_reads_the_stream(self):
        for raw in ("-1", "-8192"):
            with self.subTest(content_length=raw):
                stream = self.stream(b"ignored")
                self.assertEqual(self.read(stream, CONTENT_LENGTH=raw), b"")
                self.assertEqual(stream.reads, [])

    def test_transfer_encoding_is_not_trusted_as_a_length(self):
        # Chunked framing is the server's job (PEP 3333); an application that
        # tried to read one here would read an unbounded stream.
        stream = self.stream(b"5\r\nhello\r\n0\r\n\r\n")
        self.assertEqual(
            self.read(stream, HTTP_TRANSFER_ENCODING="chunked"), b""
        )
        self.assertEqual(stream.reads, [])

    # --- a declared length is read, and only within the cap -----------------

    def test_declared_length_is_read_exactly(self):
        stream = self.stream(b"hello")
        self.assertEqual(self.read(stream, CONTENT_LENGTH="5"), b"hello")
        self.assertEqual(stream.reads, [5])

    def test_declared_length_over_the_cap_stops_at_one_octet_past_it(self):
        stream = self.stream(b"x" * 100)
        body = self.read(stream, CONTENT_LENGTH="100")
        self.assertEqual(len(body), self.MAX + 1)
        self.assertEqual(stream.reads, [self.MAX + 1])

    def test_declared_length_under_the_cap_is_not_inflated(self):
        stream = self.stream(b"x" * 100)
        self.assertEqual(self.read(stream, CONTENT_LENGTH="10"), b"x" * 10)
        self.assertEqual(stream.reads, [10])


class _QuietHandler(WSGIRequestHandler):
    """A request handler that keeps the suite output readable."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib name
        pass


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """serves each request on its own daemon thread.

    Threading matters for two reasons: a request that (wrongly) blocks cannot
    pin the accept loop, and shutdown stays prompt because the stalled worker is
    a daemon that dies with the process instead of holding ``shutdown()`` open.
    """

    daemon_threads = True


@dataclass(frozen=True)
class Reply:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.header_all(name)
        return values[0] if values else None

    def header_all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]

    def json(self):
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def error_code(self) -> str | None:
        payload = self.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return payload["error"].get("code")
        return None


class LoopbackServer:
    """A real ``wsgiref`` server bound to an ephemeral ``127.0.0.1`` port."""

    def __init__(self, app, **server_kwargs) -> None:
        self._server = make_server(
            "127.0.0.1",
            0,
            app,
            server_class=_ThreadingWSGIServer,
            handler_class=_QuietHandler,
            **server_kwargs,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )

    def __enter__(self) -> "LoopbackServer":
        self._thread.start()
        self.host, self.port = self._server.server_address[:2]
        return self

    def __exit__(self, *exc_info) -> bool:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=CLIENT_TIMEOUT * 2)
        return False

    def request(
        self,
        method: str,
        path: str,
        *,
        body=None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        """Send one request. A stalled server surfaces as a client timeout."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=CLIENT_TIMEOUT)
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}))
            response = conn.getresponse()
            return Reply(response.status, tuple(response.getheaders()), response.read())
        finally:
            conn.close()

    def exchange(self, request: bytes) -> bytes:
        """Write exact request bytes and read until the server closes.

        Byte-level control is what lets a test prove the request really carried
        no ``Content-Length``, rather than trusting a client library not to add
        one. A stalled server raises the socket timeout instead of returning.
        """
        with socket.create_connection(
            (self.host, self.port), timeout=CLIENT_TIMEOUT
        ) as sock:
            sock.settimeout(CLIENT_TIMEOUT)
            sock.sendall(request)
            received = bytearray()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return bytes(received)
                received.extend(chunk)


class LoopbackServerTests(unittest.TestCase):
    """The whole HTTP chain, over a real socket, for every body shape."""

    #: A tiny cap keeps the oversized-body fixture small and unambiguous.
    MAX_REQUEST_BYTES = 256

    def setUp(self):
        self.tmp = support.temp_dir("llm-lab-wsgi-loopback-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = support.make_config(
            self.tmp,
            **{
                **support.feishu_env(),
                "LLM_LAB_MAX_REQUEST_BYTES": str(self.MAX_REQUEST_BYTES),
            },
        )
        self.app = routes.create_app(
            self.cfg, transport=support.FakeTransport(), clock=FakeClock()
        )
        self.app.initialize()
        bootstrap.initialize_admin_password(
            self.cfg,
            read_password=ScriptedReader(SYNTHETIC_ADMIN_PASSWORD, SYNTHETIC_ADMIN_PASSWORD),
        )
        self.server = LoopbackServer(self.app)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)

    # --- helpers -------------------------------------------------------------

    @property
    def authority(self) -> str:
        return f"{self.server.host}:{self.server.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"

    def csrf_handshake(self) -> tuple[str, str | None]:
        """Fetch a CSRF token the way a browser does: a plain bodyless GET."""
        reply = self.server.request("GET", "/auth/csrf")
        self.assertEqual(reply.status, 200, reply.body[:200])
        token = reply.json()["data"]["csrf_token"]
        set_cookie = reply.header("set-cookie")
        cookie = set_cookie.split(";", 1)[0] if set_cookie else None
        return token, cookie

    def write_headers(self, token: str, cookie: str | None, **extra: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Origin": self.origin,
            "X-CSRF-Token": token,
            **extra,
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    # --- bodyless requests: the defect this module exists for ---------------

    def test_browser_get_without_content_length_is_answered_promptly(self):
        # Byte-for-byte what a browser sends for a navigation/GET: no
        # Content-Length, no body, connection closed after the response.
        raw = self.server.exchange(
            b"GET /auth/csrf HTTP/1.1\r\n"
            b"Host: " + self.authority.encode("ascii") + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        status_line = raw.split(b"\r\n", 1)[0]
        self.assertIn(b" 200 ", status_line, raw[:200])
        self.assertIn(b"csrf_token", raw)

    def test_get_with_explicit_zero_content_length_is_answered_promptly(self):
        reply = self.server.request(
            "GET", "/auth/csrf", headers={"Content-Length": "0"}
        )
        self.assertEqual(reply.status, 200, reply.body[:200])
        self.assertIn("csrf_token", reply.json()["data"])

    def test_head_without_content_length_is_answered_promptly(self):
        reply = self.server.request("HEAD", "/auth/csrf")
        self.assertIn(reply.status, (200, 405), reply.status)

    # --- declared bodies -----------------------------------------------------

    def test_post_with_a_declared_json_body_is_read_in_full(self):
        token, cookie = self.csrf_handshake()
        reply = self.server.request(
            "POST",
            "/auth/local/login",
            body=json.dumps({"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD}),
            headers=self.write_headers(token, cookie),
        )
        self.assertEqual(reply.status, 200, reply.body[:200])
        self.assertIsNotNone(reply.header("set-cookie"))
        self.assertIn(SESSION_COOKIE, reply.header("set-cookie"))

    def test_oversized_body_is_rejected_with_413(self):
        token, cookie = self.csrf_handshake()
        payload = json.dumps(
            {"username": "admin", "password": "x" * (self.MAX_REQUEST_BYTES + 64)}
        )
        self.assertGreater(len(payload), self.MAX_REQUEST_BYTES)
        reply = self.server.request(
            "POST",
            "/auth/local/login",
            body=payload,
            headers=self.write_headers(token, cookie),
        )
        self.assertEqual(reply.status, 413, reply.body[:200])
        self.assertEqual(reply.error_code(), "payload_too_large")

    # --- undeclared or malformed lengths fail closed, and never hang ---------

    def test_malformed_content_length_is_rejected_with_400(self):
        token, cookie = self.csrf_handshake()
        reply = self.server.request(
            "POST",
            "/auth/local/login",
            headers=self.write_headers(token, cookie, **{"Content-Length": "not-a-length"}),
        )
        self.assertEqual(reply.status, 400, reply.body[:200])
        self.assertEqual(reply.error_code(), "invalid_request")

    def test_negative_content_length_is_rejected_with_400(self):
        token, cookie = self.csrf_handshake()
        reply = self.server.request(
            "POST",
            "/auth/local/login",
            headers=self.write_headers(token, cookie, **{"Content-Length": "-5"}),
        )
        self.assertEqual(reply.status, 400, reply.body[:200])
        self.assertEqual(reply.error_code(), "invalid_request")

    def test_missing_content_length_on_a_write_is_rejected_with_400(self):
        token, cookie = self.csrf_handshake()
        # A POST that announces no length at all must not be treated as "read
        # until EOF": it is rejected, and it is rejected without blocking.
        reply = self.server.request(
            "POST",
            "/auth/local/login",
            headers=self.write_headers(token, cookie, **{"Content-Length": ""}),
        )
        self.assertEqual(reply.status, 400, reply.body[:200])
        self.assertEqual(reply.error_code(), "invalid_request")

    def test_chunked_transfer_encoding_is_not_treated_as_a_body(self):
        token, cookie = self.csrf_handshake()
        body = json.dumps({"username": "admin", "password": SYNTHETIC_ADMIN_PASSWORD})
        chunked = f"{len(body):x}\r\n{body}\r\n0\r\n\r\n".encode("utf-8")
        reply = self.server.request(
            "POST",
            "/auth/local/login",
            body=chunked,
            headers=self.write_headers(
                token, cookie, **{"Transfer-Encoding": "chunked"}
            ),
        )
        # Chunked decoding belongs to the server / a trusted proxy in front of
        # it; this application sees no usable body and says so, promptly.
        self.assertEqual(reply.status, 400, reply.body[:200])
        self.assertEqual(reply.error_code(), "invalid_request")


if __name__ == "__main__":  # pragma: no cover - manual entry point
    unittest.main()
