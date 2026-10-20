"""Negative-first tests for the production release mount contract.

No socket is opened, no service is started, no credential exists. The WSGI
skeleton is driven with synthetic environ dicts.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from wsgiref.util import setup_testing_defaults

from tools.release import contract, wsgi_mount

SHA = "a066a2d3ec663dd580c96a2e75ba1ad0d2484523"
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "web")


def manifest(**overrides):
    base = {
        "schema_version": contract.SCHEMA_VERSION,
        "package_name": "llmlab-portal",
        "package_version": "1.0.0",
        "git_sha": SHA,
        "bind_host": "127.0.0.1",
        "port": 18080,
        "state_dir": "/var/lib/llmlab/platform-auth",
        "static_root": WEB,
        "trusted_proxies": [],
        "cookie_secure": True,
        "cookie_http_only": True,
        "cookie_same_site": "Lax",
        "csrf_origin_allowlist": ["http://127.0.0.1:18080"],
        "static_cache_control": "no-store",
        "data_sources": ["registry-store", "gateway-metadata"],
        "gates": {},
    }
    base.update(overrides)
    return base


def call(app, path, *, method="GET", query=""):
    environ = {}
    setup_testing_defaults(environ)
    environ.update({
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "127.0.0.1",
        "HTTP_HOST": "127.0.0.1:18080",
        "SERVER_PORT": "18080",
        "wsgi.input": io.BytesIO(b""),
        "CONTENT_LENGTH": "0",
    })
    status, headers = [], []

    def start_response(value, header_list, exc_info=None):
        status.append(value)
        headers.extend(header_list)

    body = b"".join(app(environ, start_response))
    return status[0], dict(headers), body


class ManifestTest(unittest.TestCase):
    def test_a_complete_manifest_validates(self):
        m = contract.validate_manifest(manifest())
        self.assertEqual(m.git_sha, SHA)
        self.assertEqual(m.mount_plan.resolve("/index.html"), "static")

    def test_a_non_loopback_bind_is_refused(self):
        for host in ("0.0.0.0", "192.168.32.179", "::", "example.com"):
            with self.subTest(host=host):
                with self.assertRaises(contract.ContractError):
                    contract.validate_manifest(manifest(bind_host=host))

    def test_reserved_and_privileged_ports_are_refused(self):
        for port in (443, 8080, 8200, 8443, 8444, 8765, 9090, 18443, 38849, 80, 99999):
            with self.subTest(port=port):
                with self.assertRaises(contract.ContractError):
                    contract.validate_manifest(manifest(port=port))

    def test_the_git_sha_must_be_exact(self):
        for sha in (SHA[:12], SHA.upper(), "z" * 40, "a" * 39):
            with self.subTest(sha=sha):
                with self.assertRaises(contract.ContractError):
                    contract.validate_manifest(manifest(git_sha=sha))

    def test_the_package_version_must_be_semver(self):
        for version in ("1.0", "v1.0.0", "latest", ""):
            with self.subTest(version=version):
                with self.assertRaises(contract.ContractError):
                    contract.validate_manifest(manifest(package_version=version))

    def test_insecure_cookies_are_refused(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(cookie_secure=False))
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(cookie_http_only=False))
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(cookie_same_site="None"))

    def test_csrf_needs_an_exact_origin(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(csrf_origin_allowlist=[]))
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(csrf_origin_allowlist=["*"]))
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(csrf_origin_allowlist=["http://x/y"]))

    def test_qa_fixtures_and_host_paths_are_not_release_sources(self):
        for source in ("qa-fixture", "tests/fixtures/qa", "/srv/llm-lab/state", "/home/x/data"):
            with self.subTest(source=source):
                with self.assertRaises(contract.ContractError):
                    contract.validate_manifest(manifest(data_sources=[source]))

    def test_static_cache_must_be_no_store(self):
        with self.assertRaises(contract.ContractError):
            contract.validate_manifest(manifest(static_cache_control="public, max-age=600"))

    def test_missing_required_fields_are_named(self):
        with self.assertRaises(contract.ContractError) as caught:
            contract.validate_manifest({"schema_version": contract.SCHEMA_VERSION})
        self.assertIn("missing manifest fields", str(caught.exception))


class MountPlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = contract.validate_manifest(manifest()).mount_plan

    def test_the_three_surfaces_resolve(self):
        self.assertEqual(self.plan.resolve("/index.html"), "static")
        self.assertEqual(self.plan.resolve("/api/v2/ops"), "api")
        self.assertEqual(self.plan.resolve("/auth/platform/login"), "platform")

    def test_linux_and_admin_auth_are_not_mounted(self):
        for path in ("/auth/linux/login", "/auth/linux/status", "/auth/admin/enter",
                     "/auth/admin/status"):
            with self.subTest(path=path):
                self.assertEqual(self.plan.resolve(path), "refuse")

    def test_a_malformed_path_is_refused(self):
        for path in ("", "relative", "/../x", "/a/../b"):
            with self.subTest(path=path):
                self.assertNotEqual(self.plan.resolve(path), "static")


class ReadinessTest(unittest.TestCase):
    def test_default_is_not_ready(self):
        report = contract.validate_manifest(manifest()).readiness()
        self.assertEqual(report["state"], "NOT_READY")
        self.assertEqual(set(report["missing"]), set(contract.GATES))

    def test_one_missing_gate_keeps_it_not_ready(self):
        gates = {gate: True for gate in contract.GATES}
        gates["inference_upstream_verified"] = False
        report = contract.validate_manifest(manifest(gates=gates)).readiness()
        self.assertEqual(report["state"], "NOT_READY")
        self.assertEqual(report["missing"], ["inference_upstream_verified"])

    def test_all_gates_make_it_ready_without_claiming_deployment(self):
        gates = {gate: True for gate in contract.GATES}
        report = contract.validate_manifest(manifest(gates=gates)).readiness()
        self.assertEqual(report["state"], "READY")
        self.assertIn("not a deployment", report["note"])

    def test_a_missing_gate_never_defaults_to_true(self):
        gates = {gate: True for gate in contract.GATES if gate != "rollback_drill_verified"}
        report = contract.validate_manifest(manifest(gates=gates)).readiness()
        self.assertEqual(report["state"], "NOT_READY")
        self.assertFalse(report["gates"]["rollback_drill_verified"])


class WsgiMountTest(unittest.TestCase):
    def setUp(self):
        self.manifest = contract.validate_manifest(manifest())
        self.app = wsgi_mount.ReleaseMountApp(self.manifest)

    def test_the_ui_is_served_on_the_same_origin(self):
        status, headers, body = call(self.app, "/index.html")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b'data-build-profile="production"', body)

    def test_static_responses_carry_no_cookie_and_no_store(self):
        for path in ("/index.html", "/qa.html", "/js/app.js"):
            with self.subTest(path=path):
                status, headers, _ = call(self.app, path)
                self.assertTrue(status.startswith("200"))
                self.assertNotIn("Set-Cookie", headers)
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_traversal_and_unknown_paths_are_404(self):
        for path in ("/nope.html", "/../backend/bff/app.py", "/js/../../x",
                     "/js%5c..%5capp.js"):
            with self.subTest(path=path):
                status, _, _ = call(self.app, path)
                self.assertTrue(status.startswith("404"), status)

    def test_platform_and_api_are_not_ready_until_supplied(self):
        for path in ("/auth/platform/status", "/api/v2/ops"):
            with self.subTest(path=path):
                status, _, body = call(self.app, path)
                self.assertTrue(status.startswith("503"), status)
                payload = json.loads(body)
                self.assertEqual(payload["state"], "NOT_READY")

    def test_linux_auth_is_absent_not_503(self):
        # 503 would say "the Linux path exists but is unavailable"; 404 says the
        # release does not mount it at all.
        status, _, _ = call(self.app, "/auth/linux/login")
        self.assertTrue(status.startswith("404"), status)

    def test_readyz_is_503_until_every_gate_is_proven(self):
        status, _, body = call(self.app, "/readyz")
        self.assertTrue(status.startswith("503"), status)
        self.assertEqual(json.loads(body)["state"], "NOT_READY")

    def test_readyz_turns_200_only_with_all_gates(self):
        gates = {gate: True for gate in contract.GATES}
        app = wsgi_mount.ReleaseMountApp(
            contract.validate_manifest(manifest(gates=gates)), static_root=WEB
        )
        status, _, body = call(app, "/readyz")
        self.assertTrue(status.startswith("200"), status)
        self.assertEqual(json.loads(body)["state"], "READY")

    def test_supplied_handlers_are_delegated(self):
        def platform(environ, start_response):
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b'{"platform":true}']

        def api(environ, start_response):
            start_response("200 OK", [("Content-Type", "application/json")])
            return [b'{"api":true}']

        app = wsgi_mount.ReleaseMountApp(self.manifest, static_root=WEB,
                                         platform_app=platform, api_app=api)
        self.assertEqual(call(app, "/auth/platform/status")[2], b'{"platform":true}')
        self.assertEqual(call(app, "/api/v2/ops")[2], b'{"api":true}')

    def test_methods_and_head(self):
        status, _, _ = call(self.app, "/index.html", method="POST")
        self.assertTrue(status.startswith("405"))
        status, headers, body = call(self.app, "/index.html", method="HEAD")
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"")

    def test_a_symlink_in_the_static_root_is_not_followed(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "ok.txt"), "w", encoding="utf-8") as handle:
                handle.write("ok\n")
            os.symlink(os.path.join(root, "ok.txt"), os.path.join(root, "link.txt"))
            app = wsgi_mount.ReleaseMountApp(self.manifest, static_root=root)
            self.assertTrue(call(app, "/ok.txt")[0].startswith("200"))
            self.assertTrue(call(app, "/link.txt")[0].startswith("404"))


class CliTest(unittest.TestCase):
    def test_validate_and_ready_exit_codes(self):
        from tools.release import cli

        with tempfile.TemporaryDirectory() as root:
            good = os.path.join(root, "good.json")
            with open(good, "w", encoding="utf-8") as handle:
                json.dump(manifest(), handle)
            self.assertEqual(cli.main(["validate", good]), 0)
            self.assertEqual(cli.main(["ready", good]), 3)

            bad = os.path.join(root, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                json.dump(manifest(bind_host="0.0.0.0"), handle)
            self.assertEqual(cli.main(["validate", bad]), 1)


if __name__ == "__main__":
    unittest.main()
