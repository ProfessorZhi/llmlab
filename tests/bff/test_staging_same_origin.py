"""Fail-closed contract for the same-origin staging composition.

These tests pin the properties that make one origin safe to review:

* static files and ``/api/**`` are answered by the same WSGI app (no proxy);
* the authority guard runs first — an unlisted ``Host`` gets ``421``;
* ``..``, encoded separators, backslashes, NUL, symlinks and directories are
  refused rather than served;
* an unauthenticated Ops read is ``401`` JSON — never a demo fixture, never a
  synthetic number;
* static responses carry no cookie and no cross-user cache; API responses are
  ``no-store``;
* the QA artifact and the production artifact stay distinct: the server adds no
  demo mode of its own.

No network is opened and no real Linux credential is touched: the WSGI callable
is invoked directly with synthetic environ dicts.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from wsgiref.util import setup_testing_defaults

from backend.bff import staging

WEB_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web"
)


def call(app, path, *, method="GET", host="127.0.0.1:8899", port="8899", query=""):
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "HTTP_HOST": host,
            "SERVER_PORT": port,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.input": io.BytesIO(b""),
            "CONTENT_LENGTH": "0",
        }
    )
    status: list[str] = []
    headers: list[tuple[str, str]] = []

    def start_response(value, header_list, exc_info=None):
        status.append(value)
        headers.extend(header_list)

    body = b"".join(app(environ, start_response))
    return status[0], dict(headers), body


class StagingSameOriginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.app = staging.build_staging_application(
            state_dir=cls._tmp.name, web_root=WEB_ROOT
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # --- static ---------------------------------------------------------

    def test_index_is_served_from_the_same_origin(self):
        status, headers, body = call(self.app, "/index.html")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b'data-build-profile="production"', body)

    def test_root_maps_to_index(self):
        status, _, body = call(self.app, "/")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b"<html", body)

    def test_javascript_keeps_its_mime_type(self):
        status, headers, body = call(self.app, "/js/app.js")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertTrue(body)

    def test_unknown_path_is_404(self):
        status, _, _ = call(self.app, "/no-such-file.html")
        self.assertTrue(status.startswith("404"))

    def test_directory_is_not_listed(self):
        status, _, body = call(self.app, "/js")
        self.assertTrue(status.startswith("404"))
        self.assertNotIn(b"app.js", body)

    # --- traversal ------------------------------------------------------

    def test_parent_traversal_is_refused(self):
        for path in (
            "/../backend/bff/app.py",
            "/js/../../backend/bff/app.py",
            "/..%2fbackend/bff/app.py",
            "/js%5c..%5capp.py",
            "/js/..",
            "/nul%00.html",
        ):
            with self.subTest(path=path):
                status, _, _ = call(self.app, path)
                self.assertTrue(status.startswith("404"), status)

    def test_a_symlink_inside_the_web_root_is_not_followed(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "ok.txt"), "w", encoding="utf-8") as handle:
                handle.write("ok\n")
            os.symlink(os.path.join(root, "ok.txt"), os.path.join(root, "link.txt"))
            app = staging.StagingApp(bff=self.app.bff, web_root=root)
            self.assertTrue(call(app, "/ok.txt")[0].startswith("200"))
            self.assertTrue(call(app, "/link.txt")[0].startswith("404"))

    # --- authority guard -------------------------------------------------

    def test_an_unlisted_host_is_refused_before_anything_else(self):
        status, _, body = call(self.app, "/index.html", host="evil.example", port="8899")
        self.assertTrue(status.startswith("421"), status)
        self.assertNotIn(b"<html", body)

    def test_a_loopback_host_is_allowed(self):
        for host in ("127.0.0.1:8899", "localhost:8899"):
            with self.subTest(host=host):
                status, _, _ = call(self.app, "/index.html", host=host)
                self.assertTrue(status.startswith("200"), status)

    # --- API fail-closed -------------------------------------------------

    def test_unauthenticated_ops_is_401_json_not_a_fixture(self):
        status, headers, body = call(self.app, "/api/v2/ops")
        self.assertTrue(status.startswith("401"), status)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "no_authenticated_session")
        self.assertNotIn(b"synthetic", body)
        self.assertNotIn(b"fixture", body.lower())

    def test_linux_status_reports_verification_unavailable(self):
        status, _, body = call(self.app, "/auth/linux/status")
        self.assertTrue(status.startswith("200"), status)
        data = json.loads(body)["data"]
        self.assertEqual(data["password_verification"], "unavailable_in_this_build")
        self.assertEqual(data["allowlist_size"], 0)

    def test_api_responses_are_not_cacheable(self):
        _, headers, _ = call(self.app, "/api/v2/ops")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_no_static_response_sets_a_cookie(self):
        for path in ("/index.html", "/qa.html", "/js/app.js"):
            with self.subTest(path=path):
                _, headers, _ = call(self.app, path)
                self.assertNotIn("Set-Cookie", headers)

    # --- artifacts stay distinct ----------------------------------------

    def test_qa_artifact_is_served_verbatim(self):
        status, _, body = call(self.app, "/qa.html", query="demo=1")
        self.assertTrue(status.startswith("200"))
        self.assertIn(b'data-build-profile="qa"', body)

    def test_the_server_never_injects_demo_mode(self):
        _, _, index_body = call(self.app, "/index.html")
        self.assertNotIn(b"data-demo-switch", index_body)

    # --- methods ---------------------------------------------------------

    def test_head_has_headers_and_no_body(self):
        status, headers, body = call(self.app, "/index.html", method="HEAD")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(body, b"")

    def test_post_to_a_static_path_is_405(self):
        status, _, _ = call(self.app, "/index.html", method="POST")
        self.assertTrue(status.startswith("405"), status)


class StagingStartupGuardTest(unittest.TestCase):
    def test_non_loopback_bind_is_refused(self):
        for host in ("0.0.0.0", "203.0.113.10", "::", "example.com"):
            with self.subTest(host=host):
                with self.assertRaises(staging.StagingConfigError):
                    staging.validate_bind_host(host)

    def test_reserved_upstream_port_is_refused(self):
        with self.assertRaises(staging.StagingConfigError):
            staging.validate_bind_port(8080)

    def test_a_missing_state_dir_is_refused(self):
        with self.assertRaises(staging.StagingConfigError):
            staging.build_staging_application(state_dir="", web_root=WEB_ROOT)

    def test_a_missing_web_root_is_refused(self):
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaises(staging.StagingConfigError):
                staging.build_staging_application(
                    state_dir=state, web_root=os.path.join(state, "nope")
                )


if __name__ == "__main__":
    unittest.main()
