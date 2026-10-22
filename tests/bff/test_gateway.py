"""Integrated gateway (``/v1/*``) and service parameters, end to end.

A fake OpenAI-compatible engine runs on a kernel-assigned loopback port; the
whole BFF serves both the console API and ``/v1/*``. The fake engine records
what it receives so the tests can prove what crosses (the engine's own key, the
rewritten model name) and what does not (the caller's key, identity headers).
Passwords and keys are fictional.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import socketserver
import tempfile
import threading
import unittest
import wsgiref.simple_server
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.auth import bootstrap
from backend.bff import devserver
from tests.auth.support import WsgiClient
from tests.auth.test_bootstrap import ScriptedReader

ADMIN_PASSWORD = "synthetic-admin-passphrase-1"
MEMBER_PASSWORD = "synthetic-member-pass-1"
UPSTREAM_SECRET = "engine-own-secret-value"


class FakeEngine(BaseHTTPRequestHandler):
    seen: list = []
    props = {"default_generation_settings": {"n_ctx": 8192}, "total_slots": 4}

    def log_message(self, *args):  # pragma: no cover
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/props":
            return self._json(200, FakeEngine.props)
        if self.path == "/v1/models":
            return self._json(200, {"data": [{"id": "qwen-upstream"}]})
        return self._json(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        FakeEngine.seen.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        if self.headers.get("Authorization") != f"Bearer {UPSTREAM_SECRET}":
            return self._json(401, {"error": {"message": "bad engine key"}})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in ("Hel", "lo"):
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            usage = {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
            self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
            return
        return self._json(200, {"choices": [{"message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 5, "completion_tokens": 1}})


class _Server(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    daemon_threads = True


class _Quiet(wsgiref.simple_server.WSGIRequestHandler):
    def log_message(self, *args):  # pragma: no cover
        pass


class GatewayTests(unittest.TestCase):
    def setUp(self):
        FakeEngine.seen = []
        self.tmp = tempfile.mkdtemp(prefix="llm-lab-gateway-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = ThreadingHTTPServer(("127.0.0.1", 0), FakeEngine)
        self.engine_port = self.engine.server_address[1]
        threading.Thread(target=self.engine.serve_forever, daemon=True).start()
        self.addCleanup(self.engine.server_close)
        self.addCleanup(self.engine.shutdown)
        secret_file = Path(self.tmp) / "engine.env"
        secret_file.write_text(f"# engine key\nexport STRATA_API_KEY='{UPSTREAM_SECRET}'\n")

        self.app = devserver.build_application(
            state_dir=self.tmp, engine_secret_refs=(f"qwen-engine={secret_file}:STRATA_API_KEY",)
        )
        self.addCleanup(self.app.service.store.close)
        self.app.service.config = dataclasses.replace(
            self.app.service.config, allowed_ports=frozenset({self.engine_port})
        )
        self.server = wsgiref.simple_server.make_server("127.0.0.1", 0, self.app, server_class=_Server, handler_class=_Quiet)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        bootstrap.initialize_admin_password(devserver._local_config(self.tmp), read_password=ScriptedReader(ADMIN_PASSWORD, ADMIN_PASSWORD))
        self.admin = self.client()
        self.post(self.admin, "/auth/local/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.owner_user, self.owner = self.activate("fjd")
        self.member_user, self.member = self.activate("vince")
        r = self.post(self.owner, "/api/v2/registry/engines", {
            "name": "qwen-engine", "protocol": "openai_compatible",
            "target_url": f"http://127.0.0.1:{self.engine_port}", "tags": ["framework:strata"],
        })
        self.assertEqual(r.status, 201, r.body)
        self.engine_id = r.json()["data"]["id"]

    # --- helpers -------------------------------------------------------------

    def client(self):
        return WsgiClient(app=self.app, host=self.host, scheme="http")

    def csrf(self, client):
        return client.run("GET", "/auth/csrf").json()["data"]["csrf_token"]

    def post(self, client, path, payload):
        return client.run("POST", path, payload=payload, headers={"X-CSRF-Token": self.csrf(client)})

    def activate(self, username, **extra):
        r = self.post(self.admin, "/auth/admin/users", {"username": username, **extra})
        user, temporary = r.json()["data"]["user"], r.json()["data"]["temporary_password"]
        web = self.client()
        self.post(web, "/auth/account/login", {"username": username, "password": temporary})
        self.post(web, "/auth/account/password", {"current_password": temporary, "new_password": MEMBER_PASSWORD})
        return user, web

    def publish(self, **extra):
        r = self.post(self.owner, "/api/gateway/models/publish",
                      {"engine_id": self.engine_id, "model": "qwen", "upstream_model": "qwen-upstream", **extra})
        self.assertEqual(r.status, 200, r.body)
        return r.json()["data"]["route"]

    def key(self, client, **extra):
        r = self.post(client, "/api/gateway/keys/create", {"label": "laptop", **extra})
        self.assertEqual(r.status, 200, r.body)
        return r.json()["data"]["key"]

    def v1(self, key, path="/v1/chat/completions", payload=None, method="POST", headers=None):
        hdrs = {"Authorization": f"Bearer {key}", **(headers or {})}
        return self.client().run(method, path, payload=payload, headers=hdrs, send_origin=False)

    # --- tests ---------------------------------------------------------------

    def test_a_member_key_calls_a_shared_service_through_the_gateway(self):
        self.publish()
        key = self.key(self.member)
        self.assertTrue(key["key"].startswith("sk-lab-"))
        models = self.v1(key["key"], "/v1/models", method="GET").json()
        self.assertEqual([m["id"] for m in models["data"]], ["qwen"])
        self.assertEqual(models["data"][0]["owned_by"], "fjd")
        r = self.v1(key["key"], payload={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"X-Forwarded-User": "someone-else"})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.json()["choices"][0]["message"]["content"], "Hi")
        seen = FakeEngine.seen[-1]
        self.assertEqual(seen["body"]["model"], "qwen-upstream", "the public name is rewritten to the engine's")
        self.assertEqual(seen["headers"]["Authorization"], f"Bearer {UPSTREAM_SECRET}")
        self.assertNotIn(key["key"], json.dumps(seen), "the caller's key never reaches the engine")
        self.assertNotIn("X-Forwarded-User", seen["headers"])

        logs = self.member.run("GET", "/api/gateway/logs").json()["data"]["rows"]
        self.assertEqual(len(logs), 1)
        self.assertEqual((logs[0]["prompt_tokens"], logs[0]["completion_tokens"]), (5, 1))
        self.assertEqual(logs[0]["outcome"], "success")
        self.assertEqual(logs[0]["key_label"], "laptop")
        dumped = json.dumps(logs)
        self.assertNotIn("hi", dumped.replace("chat", ""), "no prompt text is ever logged")

    def test_streams_are_relayed_and_usage_comes_from_the_last_chunk(self):
        self.publish()
        key = self.key(self.member)["key"]
        r = self.v1(key, payload={"model": "qwen", "stream": True, "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status, 200)
        self.assertIn(b"Hel", r.body)
        self.assertEqual(FakeEngine.seen[-1]["body"]["stream_options"], {"include_usage": True})
        row = self.member.run("GET", "/api/gateway/logs").json()["data"]["rows"][0]
        self.assertTrue(row["stream"])
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"]), (7, 2))
        self.assertIsNotNone(row["ttft_ms"])

    def test_private_routes_and_key_model_lists_are_enforced(self):
        self.publish(visibility="private")
        member_key = self.key(self.member)["key"]
        r = self.v1(member_key, payload={"model": "qwen", "messages": []})
        self.assertEqual(r.status, 404)
        self.assertEqual(r.json()["error"]["code"], "model_not_found")
        owner_key = self.key(self.owner, models=["other-model"])["key"]
        self.assertEqual(self.v1(owner_key, payload={"model": "qwen", "messages": []}).status, 404)
        self.assertEqual(self.v1(self.key(self.owner)["key"], payload={"model": "qwen", "messages": []}).status, 200)

    def test_bad_revoked_and_disabled_keys_are_refused(self):
        self.publish()
        self.assertEqual(self.v1("sk-lab-nope", payload={"model": "qwen"}).status, 401)
        self.assertEqual(self.v1("", payload={"model": "qwen"}).status, 401)
        key = self.key(self.member)
        self.post(self.member, f"/api/gateway/keys/{key['key_id']}/revoke", {})
        self.assertEqual(self.v1(key["key"], payload={"model": "qwen"}).status, 401)
        key2 = self.key(self.member)["key"]
        self.post(self.admin, "/auth/admin/users/status", {"user_id": self.member_user["id"], "status": "disabled"})
        self.assertEqual(self.v1(key2, payload={"model": "qwen"}).status, 401)

    def test_rate_limits_answer_429(self):
        self.publish()
        key = self.key(self.member, rpm=1)["key"]
        body = {"model": "qwen", "messages": [{"role": "user", "content": "x"}]}
        self.assertEqual(self.v1(key, payload=body).status, 200)
        r = self.v1(key, payload=body)
        self.assertEqual(r.status, 429)
        self.assertEqual(r.json()["error"]["code"], "rate_limited")

    def test_unreachable_engine_is_a_502_and_still_logged(self):
        self.publish()
        self.engine.shutdown()
        self.engine.server_close()
        key = self.key(self.member)["key"]
        r = self.v1(key, payload={"model": "qwen", "messages": []})
        self.assertEqual(r.status, 502)
        row = self.member.run("GET", "/api/gateway/logs").json()["data"]["rows"][0]
        self.assertEqual((row["status"], row["outcome"]), (502, "failed"))

    def test_views_member_owner_and_admin(self):
        self.publish()
        key = self.key(self.member)["key"]
        self.v1(key, payload={"model": "qwen", "messages": [{"role": "user", "content": "x"}]})
        mine = self.member.run("GET", "/api/gateway/overview").json()["data"]
        self.assertEqual(mine["calls"]["requests"], 1)
        self.assertEqual(mine["calls"]["by_model"][0]["model"], "qwen")
        owner = self.owner.run("GET", "/api/gateway/overview").json()["data"]
        self.assertEqual(owner["calls"]["requests"], 0)
        self.assertEqual(owner["my_services"]["by_caller"][0]["name"], "vince")
        self.assertEqual(self.member.run("GET", "/api/gateway/overview?view=all").status, 403)
        admin = self.admin.run("GET", "/api/gateway/keys?view=all").json()["data"]["keys"]
        self.assertEqual([k["owner_name"] for k in admin], ["vince"])
        self.assertTrue(all("key" not in k for k in admin), "a listing never carries a plaintext key")
        self.assertEqual(self.owner.run("GET", "/api/gateway/keys").json()["data"]["keys"], [])

    def test_only_the_owner_or_admin_publishes_and_edits(self):
        r = self.post(self.member, "/api/gateway/models/publish", {"engine_id": self.engine_id, "model": "steal"})
        self.assertEqual(r.status, 403)
        route = self.publish()
        r = self.post(self.member, f"/api/gateway/models/{route['route_id']}/update", {"enabled": False})
        self.assertEqual(r.status, 404)
        r = self.post(self.owner, f"/api/gateway/models/{route['route_id']}/update", {"visibility": "private"})
        self.assertEqual(r.json()["data"]["route"]["visibility"], "private")
        self.assertEqual(self.post(self.owner, "/api/gateway/models/publish",
                                   {"engine_id": self.engine_id, "model": "qwen"}).status, 409)

    def test_logs_are_per_key_and_bodies_are_opt_in(self):
        self.publish()
        quiet = self.key(self.member, label="quiet")
        loud = self.key(self.member, label="loud", log_bodies=True)
        self.assertTrue(loud["log_bodies"])
        ask = {"model": "qwen", "messages": [{"role": "user", "content": "secret question"}]}
        self.v1(quiet["key"], payload=ask)
        self.v1(loud["key"], payload=ask)
        self.v1(loud["key"], payload={**ask, "stream": True})

        keys = {k["label"]: k for k in self.member.run("GET", "/api/gateway/keys").json()["data"]["keys"]}
        self.assertEqual((keys["quiet"]["usage"]["requests_24h"], keys["loud"]["usage"]["requests_24h"]), (1, 2))
        self.assertEqual(keys["loud"]["usage"]["tokens_7d"], 6 + 9)

        rows = self.member.run("GET", f"/api/gateway/logs?key_ref={quiet['key_id']}").json()["data"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["has_body"])
        detail = self.member.run("GET", f"/api/gateway/logs/{rows[0]['request_id']}").json()["data"]
        self.assertIsNone(detail["body"], "metadata only unless the key opted in")
        self.assertEqual(detail["engine_name"], "qwen-engine")

        loud_rows = self.member.run("GET", f"/api/gateway/logs?key_ref={loud['key_id']}").json()["data"]["rows"]
        self.assertTrue(all(r["has_body"] for r in loud_rows))
        streamed = next(r for r in loud_rows if r["stream"])
        plain = next(r for r in loud_rows if not r["stream"])
        d = self.member.run("GET", f"/api/gateway/logs/{plain['request_id']}").json()["data"]
        self.assertEqual(d["body"]["request"]["messages"][0]["content"], "secret question")
        self.assertEqual(d["body"]["response"]["choices"][0]["message"]["content"], "Hi")
        d = self.member.run("GET", f"/api/gateway/logs/{streamed['request_id']}").json()["data"]
        self.assertEqual(d["body"]["response_text"], "Hello")
        self.assertNotIn("secret", json.dumps(self.member.run("GET", "/api/gateway/logs").json()),
                         "the brief log never carries bodies")

        # 别人看不到；管理员能看；只有本人能开关内容记录。
        self.assertEqual(self.owner.run("GET", f"/api/gateway/logs/{plain['request_id']}").status, 404)
        self.assertEqual(self.admin.run("GET", f"/api/gateway/logs/{plain['request_id']}").status, 200)
        self.assertEqual(self.post(self.admin, f"/api/gateway/keys/{quiet['key_id']}/update", {"log_bodies": True}).status, 404)
        r = self.post(self.member, f"/api/gateway/keys/{loud['key_id']}/update", {"log_bodies": False})
        self.assertFalse(r.json()["data"]["key"]["log_bodies"])
        self.v1(loud["key"], payload=ask)
        newest = self.member.run("GET", f"/api/gateway/logs?key_ref={loud['key_id']}").json()["data"]["rows"][0]
        self.assertFalse(newest["has_body"])

    def test_playground_goes_through_the_gateway_and_is_logged(self):
        self.publish()
        r = self.post(self.member, "/api/gateway/playground",
                      {"model": "qwen", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.json()["data"]["message"]["content"], "Hi")
        row = self.member.run("GET", "/api/gateway/logs").json()["data"]["rows"][0]
        self.assertEqual(row["channel"], "playground")

    # --- service parameters --------------------------------------------------

    def test_parameters_are_measured_saved_and_checked(self):
        # An unpublished service is invisible to other members; publishing it lab-wide lets them read it.
        self.assertEqual(self.member.run("GET", f"/api/v2/registry/engines/{self.engine_id}/params").status, 404)
        self.publish()
        r = self.member.run("GET", f"/api/v2/registry/engines/{self.engine_id}/params")
        self.assertEqual(r.status, 200, r.body)
        data = r.json()["data"]
        self.assertEqual(data["framework"], "strata")
        self.assertEqual(data["observed"]["values"], {"parallel": 4, "ctx_per_slot": 8192, "ctx_size": 32768})
        self.assertFalse(data["can_edit"])
        self.assertIn("ctx_size", [p["key"] for p in data["schema"]])

        denied = self.post(self.member, f"/api/v2/registry/engines/{self.engine_id}/params/update", {"values": {"parallel": 2}})
        self.assertEqual(denied.status, 403)
        bad = self.post(self.owner, f"/api/v2/registry/engines/{self.engine_id}/params/update", {"values": {"parallel": 999}})
        self.assertEqual(bad.status, 400)
        unknown = self.post(self.owner, f"/api/v2/registry/engines/{self.engine_id}/params/update", {"values": {"rm": "-rf"}})
        self.assertEqual(unknown.status, 400)

        r = self.post(self.owner, f"/api/v2/registry/engines/{self.engine_id}/params/update",
                      {"values": {"ctx_size": 65536, "parallel": 8, "flash_attn": True}})
        data = r.json()["data"]
        self.assertEqual(data["state"], "pending")
        self.assertIn("--ctx-size 65536", data["command"])
        self.assertIn("--parallel 8", data["command"])
        self.assertIn("--flash-attn", data["command"])
        self.assertIn(f"--port {self.engine_port}", data["command"])

        FakeEngine.props = {"default_generation_settings": {"n_ctx": 8192}, "total_slots": 8}
        self.addCleanup(setattr, FakeEngine, "props", {"default_generation_settings": {"n_ctx": 8192}, "total_slots": 4})
        data = self.owner.run("GET", f"/api/v2/registry/engines/{self.engine_id}/params").json()["data"]
        self.assertEqual(data["state"], "applied")


if __name__ == "__main__":
    unittest.main()
