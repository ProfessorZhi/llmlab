#!/usr/bin/env python3
"""Acceptance for multi-engine routing, runnable offline -- no node, no GPU.

    python -m deploy.dell.gateway.selftest_routing

WHAT THIS IS TESTING, AND WHY THIS IS THE THING WORTH TESTING.  The gateway now
forwards to more than one engine, and the only interesting question about that
is *who decides*.  A caller picks a name, the name picks an engine, and whether
this caller may use that engine must come from a server-side record.  None of
that is testable by happy path alone, so every positive check here has a
negative control beside it:

  * a share that works, then the same share withdrawn, with no restart;
  * a private model absent from a stranger's list and present in its owner's;
  * a refusal that stands while `X-User`, `X-Forwarded-User` and `X-Remote-User`
    all claim to be somebody else -- and the same request succeeding when the
    identity comes from the key.

THE ENGINES ARE MOCKS, AND THAT IS THE POINT.  Each one enforces its own key and
records every request it is shown, so "the caller's key never crossed", "one
engine never saw another engine's key" and "the engine was asked for the real
model id" are observations rather than statements of intent.  A test against
the live engines could not tell "the gateway switched keys" from "the engine
happened to accept it anyway" -- and it would have to touch the model service,
which the brief forbids.

Nothing here contacts anything but localhost on ports this file bound itself.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from deploy.dell.gateway import admin as admin_cli            # noqa: E402
from deploy.dell.gateway import routing                        # noqa: E402
from deploy.dell.gateway.gateway import (                      # noqa: E402
    Gateway, Handler, Server, read_key_file,
)
from deploy.dell.gateway.keystore import KeyStore              # noqa: E402
from deploy.dell.gateway.limits import Gate                    # noqa: E402
from deploy.dell.gateway.routing import (                      # noqa: E402
    Router, parse_allowed_addresses,
)
# The house style, the HTTP helpers and the legacy upstream mock come from the
# phase-5 self test.  Reusing them is deliberate: this file is a second chapter
# of the same acceptance, not a second, differently-shaped one.
from deploy.dell.gateway.selftest import (                     # noqa: E402
    FAILED, MockStrata, PASSED, UPSTREAM_KEY, body_of, call, check, json_of,
    wait_for,
)
from deploy.dell.gateway.telemetry import (                    # noqa: E402
    Meters, StatusFile, UsageLog,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ENGINE_A_KEY = "engine-alpha-key-never-handed-out"
ENGINE_B_KEY = "engine-beta-key-never-handed-out"
GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# engine_id -> every request that engine was shown.
ENGINE_SEEN: dict[str, list[dict]] = {}

# Set in `run`: one env-style secrets file, one variable per engine.
SECRETS_PATH = ""


# ---------------------------------------------------------------------------
# a mock engine
# ---------------------------------------------------------------------------
class EngineServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, spec: dict):
        # Mutable on purpose: a control test un-sets a behaviour and asserts the
        # same engine then answers normally, which is what turns "the redirect
        # was refused" into evidence rather than a coincidence.
        self.spec = spec
        super().__init__(addr, handler)


class MockEngine(BaseHTTPRequestHandler):
    """An OpenAI-compatible engine that enforces its own key."""

    protocol_version = "HTTP/1.0"     # closes the connection: no framing invented

    def log_message(self, *args):
        pass

    @property
    def spec(self) -> dict:
        return self.server.spec

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except GONE:
            self.close_connection = True

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except GONE:
            self.close_connection = True

    def _record(self, body: bytes) -> None:
        ENGINE_SEEN.setdefault(self.spec["engine_id"], []).append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization") or "",
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _authorised(self) -> bool:
        if (self.headers.get("Authorization") or "") == "Bearer" + " " + self.spec["key"]:
            return True
        self._json(401, {"error": {"message": "invalid api key",
                                   "type": "invalid_request_error"}})
        return False

    def _garbage(self) -> bool:
        """Answer with something that is not HTTP at all, when told to.

        `http.client` raises `BadStatusLine` for this, which is not an
        `OSError` -- the one class of bad answer an `except OSError` does not
        cover.  It is reachable on the *health* path, which is a GET, so it is
        wired into both verbs rather than only the one that serves requests.
        """
        if not (self.spec.get("behaviour") or {}).get("garbage"):
            return False
        self.wfile.write(b"this is not a status line\r\n\r\n")
        self.close_connection = True
        return True

    def do_GET(self):
        self._record(b"")
        if self._garbage():
            return
        if not self._authorised():
            return
        if urlsplit(self.path).path == "/v1/models":
            self._json(200, {"object": "list",
                             "data": [{"id": self.spec["model_id"],
                                       "object": "model"}]})
            return
        self._json(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if self._garbage():
            return
        if not self._authorised():
            return
        if urlsplit(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "no such path"}})
            return
        payload = json.loads(body or b"{}")
        behaviour = self.spec.get("behaviour") or {}

        if behaviour.get("redirect"):
            # Somewhere off the box, so that "not followed" and "not relayed"
            # are two separately observable facts.
            self.send_response(302)
            self.send_header("Location", "http://attacker.invalid/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if behaviour.get("slow"):
            time.sleep(behaviour["slow"])

        if behaviour.get("fail"):
            self._json(400, {"error": {"message": "this engine says no, in its "
                                                  "own words",
                                       "type": "invalid_request_error",
                                       "code": "engine_boom"}})
            return

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self._write(b'data: {"choices":[{"delta":{"content":"e%d"}}]}\n\n'
                            % i)
                time.sleep(0.05)
            self._write(b"data: [DONE]\n\n")
            return

        # `model` is echoed back verbatim: the assertion that the alias was
        # rewritten to the engine's real model id reads it off this reply.
        self._json(200, {
            "id": "cmpl-%s" % self.spec["engine_id"],
            "object": "chat.completion",
            "model": payload.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "hello from %s"
                                                % self.spec["engine_id"]}}],
            # Deliberately odd counts: if the gateway ever starts estimating,
            # the estimate will not be these.
            "usage": {"prompt_tokens": 11, "completion_tokens": 5,
                      "total_tokens": 16},
        })


def start_engine(engine_id: str, key: str, model_id: str) -> EngineServer:
    spec = {"engine_id": engine_id, "key": key, "model_id": model_id,
            "behaviour": {}}
    srv = EngineServer(("localhost", 0), MockEngine, spec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def posts(engine_id: str) -> list[dict]:
    return [r for r in ENGINE_SEEN.get(engine_id, []) if r["method"] == "POST"]


def auths_seen() -> list[str]:
    return [r["auth"] for seen in ENGINE_SEEN.values() for r in seen]


def start_legacy():
    MockStrata.seen = []
    srv = ThreadingHTTPServer(("localhost", 0), MockStrata)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------
def engine_entry(engine_id, port, *, key_var="ENGINE_A_KEY", transport="http",
                 host="localhost", path=None):
    if transport == "unix":
        up = {"transport": "unix", "path": path}
    else:
        up = {"transport": "http", "host": host, "port": port}
    entry = {"engine_id": engine_id, "kind": "openai-compatible", "upstream": up}
    if key_var is not None:
        entry["secret_ref"] = {"file": SECRETS_PATH, "var": key_var}
    return entry


def endpoint(endpoint_id, engine_id, alias, model_id, owner, *, acl=None,
             state=None, note=""):
    ep = {"endpoint_id": endpoint_id, "engine_id": engine_id,
          "model_alias": alias, "model_id": model_id, "owner_uid": owner}
    if acl is not None:
        ep["acl"] = acl
    if state is not None:
        ep["state"] = state
    if note:
        ep["note"] = note
    return ep


def write_snapshot(path: str, doc: dict, mode: int = 0o600) -> str:
    """One snapshot on disk.  The length changes with the content, which is what
    moves the `stat`, which is what makes the router re-read it on the next
    request -- that is the mechanism under test in section C, not a detail of
    the test."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2) + "\n")
    os.chmod(path, mode)
    return path


def good_snapshot(port_a: int, port_b: int, *, revision: int = 1) -> dict:
    """Two engines, two owners, and all three ACL kinds."""
    return {
        "schema": routing.SCHEMA,
        "revision": revision,
        "generated_at": "2026-10-15T00:00:00Z",
        "source": "selftest_routing",
        "engines": [
            engine_entry("engine-alpha", port_a, key_var="ENGINE_A_KEY"),
            engine_entry("engine-beta", port_b, key_var="ENGINE_B_KEY"),
        ],
        "endpoints": [
            endpoint("ep-alice-private", "engine-alpha", "alice-private",
                     "real-alice-model", "alice", acl={"kind": "private"}),
            endpoint("ep-bob-private", "engine-beta", "bob-private",
                     "real-bob-model", "bob", acl={"kind": "private"}),
            endpoint("ep-shared-all", "engine-alpha", "shared-team",
                     "real-alice-model", "alice", acl={"kind": "all"}),
            endpoint("ep-shared-uid", "engine-alpha", "shared-by-uid",
                     "real-alice-model", "alice",
                     acl={"kind": "users", "principals": ["uid:2002"]}),
        ],
    }


def variant(base: dict, **changes) -> dict:
    doc = json.loads(json.dumps(base))
    doc.update(changes)
    return doc


# ---------------------------------------------------------------------------
# the gateway under test
# ---------------------------------------------------------------------------
class Harness:
    """One gateway process-in-a-thread, with its own files.

    Its own status and usage files, not the shared ones, because two gateways
    writing one `status.json` would make every reading below a reading of
    whichever ticker wrote last.
    """

    def __init__(self, legacy_port, store_path, tmp, snapshot_path, *,
                 name="main", allowed=(), upstream_timeout=30.0,
                 probe_interval=3600.0, **gate_kw):
        self.tmp = tmp
        self.store = KeyStore(store_path)
        self.gate = Gate(**gate_kw)
        self.router = None
        if snapshot_path is not None:
            self.router = Router(
                snapshot_path,
                allowed_addresses=parse_allowed_addresses(allowed),
                reserved_aliases={},
                key_reader=read_key_file)
        self.status_path = os.path.join(tmp, "status-%s.json" % name)
        self.usage_path = os.path.join(tmp, "usage-%s.jsonl" % name)
        self.gw = Gateway(
            upstream_url="http://localhost:%d" % legacy_port,
            upstream_key=UPSTREAM_KEY, store=self.store, gate=self.gate,
            meters=Meters(), usage_log=UsageLog(self.usage_path),
            status_file=StatusFile(self.status_path), router=self.router,
            max_body=8 * 1024 * 1024, upstream_timeout=upstream_timeout,
            tick_interval=0.05, probe_interval=probe_interval)
        self.gw.start()

        class H(Handler):
            pass

        self.srv = Server(("localhost", 0), H, self.gw)
        self.gw.listen = "localhost:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return "http://localhost:%d%s" % (self.srv.server_address[1], path)

    def chat(self, key, model, *, headers=None, extra=None, timeout=30):
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        body.update(extra or {})
        return call(self.url("/v1/chat/completions"), key=key, method="POST",
                    body=json.dumps(body).encode(), headers=headers,
                    timeout=timeout)

    def status(self, timeout: float = 5.0) -> dict:
        # The ticker writes on its first tick, so there is a moment at startup
        # with no document at all -- a real state the console has to cope with,
        # not something to paper over.  Waiting rather than sleeping a fixed
        # amount is what `wait_for` is for.  `timeout` is a parameter because
        # the first tick also probes every engine, and one that is deliberately
        # unreachable holds that first write for the length of its own connect
        # timeout.
        if not wait_for(lambda: os.path.isfile(self.status_path), timeout=timeout):
            raise AssertionError("no status file appeared at %s"
                                 % self.status_path)
        with open(self.status_path, encoding="utf-8") as fh:
            return json.load(fh)

    def usage(self) -> list[dict]:
        if not os.path.isfile(self.usage_path):
            return []
        with open(self.usage_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
def run(tmp: str) -> None:
    global SECRETS_PATH
    SECRETS_PATH = os.path.join(tmp, "engines.env")
    with open(SECRETS_PATH, "w", encoding="utf-8") as fh:
        fh.write("# engine credentials, one variable each\n")
        fh.write("ENGINE_A_KEY=%s\n" % ENGINE_A_KEY)
        fh.write("ENGINE_B_KEY=%s\n" % ENGINE_B_KEY)
    os.chmod(SECRETS_PATH, 0o600)

    store_path = os.path.join(tmp, "keys.json")
    snapshot_path = os.path.join(tmp, "routing.json")

    _legacy_srv, legacy_port = start_legacy()
    a_srv = start_engine("engine-alpha", ENGINE_A_KEY, "real-alice-model")
    b_srv = start_engine("engine-beta", ENGINE_B_KEY, "real-bob-model")
    a_port = a_srv.server_address[1]
    b_port = b_srv.server_address[1]

    # Four principals and one key bound to nobody.
    seed = KeyStore(store_path)
    alice_key, alice_rec = seed.issue("alice-laptop", owner="alice")
    bob_key, bob_rec = seed.issue("bob-laptop", owner="bob")
    uid_key, _uid_rec = seed.issue("bob-script", owner="uid:2002")
    loose_key, loose_rec = seed.issue("hr-system")
    carol_key, _carol_rec = seed.issue("carol-laptop", owner="carol")

    # == A. the legacy deployment is untouched ==============================
    h = Harness(legacy_port, store_path, tmp, None, name="legacy",
                global_limit=1, per_user_limit=1, queue_limit=4,
                wait_timeout_s=1.0)
    st = h.status()
    check("without a snapshot the status says so in one word (routing: null)",
          st.get("routing") is None, "routing=%r" % (st.get("routing"),))
    check("CONTROL: an unbound key still calls the legacy model, exactly as it "
          "did before routing existed",
          call(h.url("/v1/models"), key=loose_key)[0] == 200)
    check("CONTROL: and the legacy chat route answers it too",
          h.chat(loose_key, "strata-iq2-xs")[0] == 200)
    check("CONTROL: the legacy upstream still refuses a request with no key",
          call(h.url("/v1/models"))[0] == 401)
    check("the status document states what it does NOT measure rather than "
          "showing a zero",
          st["resource_status"]["per_user_gpu_share"]["measured"] is False
          and st["resource_status"]["gpu"]["measured"] is False,
          json.dumps(st.get("resource_status")))
    check("a key with no owner is allowed by nobody: the binding is empty, not "
          "a default",
          all(not k.get("owner") for k in st["keys"]
              if k["id"] == loose_rec["id"]))

    # One slot, no queue room: any second concurrent request must be refused,
    # which is what makes the "routing does not create capacity" check below
    # deterministic rather than a race with the timeout.
    r = Harness(legacy_port, store_path, tmp, snapshot_path, name="routed",
                global_limit=1, per_user_limit=1, queue_limit=0,
                wait_timeout_s=1.0, probe_interval=0.2)
    write_snapshot(snapshot_path, good_snapshot(a_port, b_port))

    # == B. routing, ACLs, and the three kinds ==============================
    status, _h, resp = r.chat(alice_key, "alice-private")
    check("alice reaches her own private model on her own engine",
          status == 200 and b"engine-alpha" in body_of(resp), "status=%s" % status)
    check("the request went to her engine and not to the other one",
          bool(posts("engine-alpha")) and not posts("engine-beta"))
    check("her engine was asked for the real model id, not the public alias",
          posts("engine-alpha")[-1]["body"]
          and json.loads(posts("engine-alpha")[-1]["body"])["model"]
          == "real-alice-model")
    check("her engine saw its own key",
          posts("engine-alpha")[-1]["auth"] == "Bearer" + " " + ENGINE_A_KEY)
    check("her engine never saw Strata's key",
          ("Bearer" + " " + UPSTREAM_KEY) not in auths_seen())
    check("her engine never saw her own key",
          all(a != "Bearer" + " " + alice_key for a in auths_seen()))

    status, _h, resp = r.chat(bob_key, "bob-private")
    check("the same shape works for the second user on the second engine",
          status == 200 and b"engine-beta" in body_of(resp), "status=%s" % status)
    check("the second engine saw its own key and never the first engine's",
          posts("engine-beta")[-1]["auth"] == "Bearer" + " " + ENGINE_B_KEY
          and all(r_["auth"] != "Bearer" + " " + ENGINE_A_KEY
                  for r_ in ENGINE_SEEN["engine-beta"]))
    check("the engines' keys never reached the legacy upstream either",
          {r_["auth"] for r_ in MockStrata.seen} == {"Bearer" + " " + UPSTREAM_KEY})

    status, _h, resp = r.chat(bob_key, "alice-private")
    body = json_of(resp)
    check("bob is refused alice's private model",
          status == 404
          and body.get("error", {}).get("code") == "model_not_found",
          "status=%s body=%s" % (status, body))
    check("...and the answer is the one an unknown name gets, not a distinct "
          "'this exists but is not yours' -- otherwise a private alias could be "
          "found by guessing at it one refusal at a time",
          "not shared" not in json.dumps(body)
          and "forbidden" not in json.dumps(body), json.dumps(body))
    check("...while the real reason goes to the usage log, where the audience "
          "is the operator who has to act on it",
          wait_for(lambda: any(u.get("error") == "not_shared_with_you"
                               for u in r.usage()), timeout=3.0))
    check("CONTROL: alice is not refused her own model, so the 404 above was "
          "about who asked", r.chat(alice_key, "alice-private")[0] == 200)

    names_bob = sorted(m.get("id") for m in json_of(
        call(r.url("/v1/models"), key=bob_key)[2]).get("data", []))
    names_alice = sorted(m.get("id") for m in json_of(
        call(r.url("/v1/models"), key=alice_key)[2]).get("data", []))
    check("a private model is not enumerable by a stranger",
          "alice-private" not in names_bob, str(names_bob))
    check("...while each user does see their own private model",
          "bob-private" in names_bob and "alice-private" in names_alice,
          "%s / %s" % (names_bob, names_alice))
    check("...and neither sees the other's", "bob-private" not in names_alice,
          str(names_alice))
    check("an all-platform-users model is in everybody's list",
          "shared-team" in names_bob and "shared-team" in names_alice)
    check("the legacy upstream's own model is still listed",
          "strata-iq2-xs" in names_bob, str(names_bob))

    # A GET can carry a body, so "route only the chat path" has to be a rule and
    # not a consequence of GETs being bodyless.  Proxying a model list to an
    # engine would hand the caller that engine's own model ids -- the internal
    # names behind other people's private endpoints.
    status, _h, resp = call(r.url("/v1/models"), key=bob_key, method="GET",
                            body=json.dumps({"model": "shared-team"}).encode())
    listed = json.dumps(json_of(resp))
    check("a model list is never proxied to an engine, even when the request "
          "carries a model name (a GET may carry a body)",
          status == 200 and "real-alice-model" not in listed
          and "shared-team" in listed, listed[:220])

    check("an endpoint shared with a trusted uid is reachable by a key bound to "
          "that uid", r.chat(uid_key, "shared-by-uid")[0] == 200)
    check("...and NOT by a key bound to a user name, even the name of the "
          "account that uid is: the two forms are matched exactly, not resolved "
          "into each other", r.chat(bob_key, "shared-by-uid")[0] == 404)
    check("...and not by a third user who is merely bound to somebody",
          r.chat(carol_key, "shared-by-uid")[0] == 404)
    check("an all-platform-users model is callable by any bound key",
          r.chat(carol_key, "shared-team")[0] == 200)
    check("...but a bound key that owns nothing still cannot reach somebody "
          "else's private model", r.chat(carol_key, "bob-private")[0] == 404)

    # == C. sharing, and un-sharing, without a restart ======================
    doc = good_snapshot(a_port, b_port, revision=2)
    doc["endpoints"][0]["acl"] = {"kind": "users", "principals": ["bob"]}
    write_snapshot(snapshot_path, doc)
    check("sharing a private model with a named user takes effect on the very "
          "next request, with no restart",
          r.chat(bob_key, "alice-private")[0] == 200)
    check("...and it appears in that user's model list",
          "alice-private" in [m.get("id") for m in json_of(
              call(r.url("/v1/models"), key=bob_key)[2]).get("data", [])])

    doc["revision"] = 3
    doc["endpoints"][0]["acl"] = {"kind": "private"}
    write_snapshot(snapshot_path, doc)
    check("withdrawing the share refuses the next request, with no restart",
          r.chat(bob_key, "alice-private")[0] == 404)
    check("...and it is gone from that user's list again",
          "alice-private" not in [m.get("id") for m in json_of(
              call(r.url("/v1/models"), key=bob_key)[2]).get("data", [])])

    doc["revision"] = 4
    doc["endpoints"][0]["state"] = "revoked"
    write_snapshot(snapshot_path, doc)
    check("an endpoint whose sharing has been revoked is refused even for its "
          "owner", r.chat(alice_key, "alice-private")[0] == 404)
    doc["revision"] = 5
    doc["endpoints"][0]["state"] = "active"
    doc["endpoints"][0]["acl"] = {"kind": "private"}
    write_snapshot(snapshot_path, doc)
    check("CONTROL: putting it back makes it work again, so the two refusals "
          "above were the snapshot and not a broken route",
          r.chat(alice_key, "alice-private")[0] == 200)

    # == D. identity cannot come from the caller ============================
    spoof = {"X-User": "alice", "X-Forwarded-User": "alice",
             "X-Remote-User": "alice", "X-Forwarded-Email": "alice@example"}
    status, _h, resp = r.chat(bob_key, "alice-private", headers=spoof)
    check("CONTROL: every spoofed identity header at once changes nothing",
          status == 404, "status=%s" % status)
    status, _h, resp = r.chat(alice_key, "alice-private", headers=spoof)
    check("...and the same headers neither help nor break the real owner",
          status == 200, "status=%s" % status)
    check("a caller cannot claim ownership in the body either",
          r.chat(bob_key, "alice-private",
                 extra={"user": "alice", "owner": "alice"})[0] == 404)
    check("a registered model is not a door of its own: no key and a made-up "
          "key are both refused before any routing happens",
          r.chat(None, "alice-private")[0] == 401
          and r.chat("llmlab_" + "x" * 43, "alice-private")[0] == 401)
    check("a key with no owner binding is refused a registered model",
          r.chat(loose_key, "shared-team")[0] == 403)
    check("...and the refusal is in words an operator can act on",
          "owner binding" in json.dumps(json_of(
              r.chat(loose_key, "shared-team")[2])))
    check("...while that key still works on the legacy upstream (its old job)",
          r.chat(loose_key, "strata-iq2-xs")[0] == 200)
    check("...and it sees none of the registered models in its list",
          not {"alice-private", "bob-private", "shared-team", "shared-by-uid"}
          & set(m.get("id") for m in json_of(
              call(r.url("/v1/models"), key=loose_key)[2]).get("data", [])))

    with contextlib.redirect_stdout(io.StringIO()):
        rc = admin_cli.main(["--store", store_path, "key", "bind",
                             loose_rec["id"], "alice"])
    check("the admin CLI can bind an existing key to a person", rc == 0)
    check("...and the gateway honours it on the very next request, with no "
          "restart", r.chat(loose_key, "alice-private")[0] == 200)
    with contextlib.redirect_stdout(io.StringIO()):
        admin_cli.main(["--store", store_path, "key", "bind",
                        loose_rec["id"], "--clear"])
    check("...and clearing the binding takes that away again",
          r.chat(loose_key, "alice-private")[0] == 403)

    bare = io.StringIO()
    with contextlib.redirect_stderr(bare), contextlib.redirect_stdout(bare):
        rc = admin_cli.main(["--store", store_path, "key", "bind",
                             alice_rec["id"]])
    check("a bare `key bind` with no owner and no --clear is refused rather "
          "than read as a request to clear: one of those two readings silently "
          "strips a key's access to every shared model",
          rc != 0
          and KeyStore(store_path).get(alice_rec["id"])["owner"] == "alice",
          "%s / %s" % (rc, bare.getvalue()[:120]))

    rejected = None
    try:
        KeyStore(store_path).bind(alice_rec["id"], "../../etc/passwd")
    except ValueError as exc:
        rejected = str(exc)
    check("a binding that is neither a user name nor a uid is refused by the "
          "store", bool(rejected), repr(rejected))
    check("an unknown model name still goes to the legacy upstream: a snapshot "
          "adds routes, it does not take the old one away",
          r.chat(alice_key, "strata-iq2-xs")[0] == 200)

    # == E. an engine may not redirect, and nothing may be injected =========
    a_srv.spec["behaviour"] = {"redirect": True}
    status, hdrs, resp = r.chat(alice_key, "alice-private")
    check("an engine that answers with a redirect is refused", status == 502,
          "status=%s" % status)
    check("...with a reason that names the redirect rather than a generic "
          "upstream error",
          json_of(resp).get("error", {}).get("code")
          == "upstream_redirect_refused", json.dumps(json_of(resp)))
    check("...and the Location it wanted to send the caller to is not relayed",
          not hdrs.get("Location"), "Location=%r" % (hdrs.get("Location"),))
    a_srv.spec["behaviour"] = {}
    check("CONTROL: the same engine with the redirect taken away answers "
          "normally", r.chat(alice_key, "alice-private")[0] == 200)

    a_srv.spec["behaviour"] = {"fail": True}
    status, _h, resp = r.chat(alice_key, "alice-private")
    check("an engine's own 4xx is passed through with its own status and words",
          status == 400 and b"in its own words" in body_of(resp),
          "status=%s" % status)
    a_srv.spec["behaviour"] = {}

    # == F. streams, timeouts, and per-engine health ========================
    stream_body = json.dumps({"model": "alice-private", "stream": True,
                              "messages": [{"role": "user", "content": "hi"}]
                              }).encode()
    status, hdrs, resp = call(r.url("/v1/chat/completions"), key=alice_key,
                              method="POST", body=stream_body)
    raw = body_of(resp)
    check("a streamed answer from an engine is relayed as an event stream",
          status == 200 and "text/event-stream" in (hdrs.get("Content-Type") or ""),
          "status=%s ctype=%s" % (status, hdrs.get("Content-Type")))
    check("...carrying every event and its terminator",
          raw.count(b"data: ") == 4 and raw.endswith(b"data: [DONE]\n\n"),
          repr(raw[-40:]))
    check("...and it is logged as a stream, not as a buffered body",
          any(u.get("stream") for u in r.usage()))

    slow = Harness(legacy_port, store_path, tmp, snapshot_path, name="timeout",
                   global_limit=1, per_user_limit=1, queue_limit=0,
                   upstream_timeout=0.4, probe_interval=3600.0)
    a_srv.spec["behaviour"] = {"slow": 1.5}
    started = time.monotonic()
    status, _h, resp = slow.chat(alice_key, "alice-private", timeout=30)
    took = time.monotonic() - started
    check("an engine that outlives the budget answers 504, not 200",
          status == 504, "status=%s" % status)
    check("...and it says so in the same vocabulary the legacy route uses",
          json_of(resp).get("error", {}).get("code") == "upstream_timeout",
          json.dumps(json_of(resp)))
    check("...and it gave up at the budget rather than waiting it out",
          took < 1.4, "%.2fs" % took)
    a_srv.spec["behaviour"] = {}
    check("CONTROL: the same engine, fast again, answers 200",
          slow.chat(alice_key, "alice-private")[0] == 200)

    check("both engines are reported healthy, separately, each with its own "
          "timestamp",
          wait_for(lambda: all(e["healthy"] is True and e["checked_at"]
                               for e in r.status()["routing"]["engines"]),
                   timeout=5.0),
          json.dumps(r.status()["routing"]["engines"]))
    a_srv.spec["behaviour"] = None
    a_srv.spec["key"] = "a-key-this-engine-no-longer-accepts"
    check("an engine that stops accepting its key goes unhealthy, and the "
          "other one does not",
          wait_for(lambda: ({
              e["engine_id"]: e["healthy"]
              for e in r.status()["routing"]["engines"]}).get("engine-alpha") is False
              and ({
                  e["engine_id"]: e["healthy"]
                  for e in r.status()["routing"]["engines"]}).get("engine-beta")
              is True, timeout=5.0),
          json.dumps(r.status()["routing"]["engines"]))
    a_srv.spec["key"] = ENGINE_A_KEY

    # An engine answering nonsense is a *reading*, and the ticker that takes it
    # is also the only thing that persists key counters and rewrites
    # status.json.  A ticker that dies on one bad engine does not look like a
    # crash -- the gateway keeps answering -- it looks like a console frozen at
    # the moment things went wrong.
    a_srv.spec["behaviour"] = {"garbage": True}
    time.sleep(0.7)                       # at least one probe reaches it
    states = {e["engine_id"]: e["healthy"]
              for e in r.status()["routing"]["engines"]}
    before = os.stat(r.status_path).st_mtime_ns
    time.sleep(0.5)
    after = os.stat(r.status_path).st_mtime_ns
    a_srv.spec["behaviour"] = {}
    check("an engine answering something that is not HTTP is recorded against "
          "that engine, and does not stop the ticker that persists the "
          "counters", states.get("engine-alpha") is False and after > before,
          "engine-alpha=%r status advanced=%s" % (states.get("engine-alpha"),
                                                  after > before))
    check("...and the other engine is still reported healthy through all of it",
          states.get("engine-beta") is True, json.dumps(states))

    check("the status document names the engines, the endpoints and the "
          "snapshot's digest",
          {e["engine_id"] for e in r.status()["routing"]["engines"]}
          == {"engine-alpha", "engine-beta"}
          and len(r.status()["routing"]["endpoints"]) == 4
          and len(r.status()["routing"]["snapshot_sha256"]) == 64)
    check("the status document carries no engine secret",
          ENGINE_A_KEY not in json.dumps(r.status())
          and ENGINE_B_KEY not in json.dumps(r.status()))
    # The node acceptance (`72-accept-gateway.sh`) asserts that the console's
    # view contains no `"digest"` key at all, so that it can never become a
    # lookup table of the values keys are checked against.  This phase adds a
    # hash to that same document, and pins the invariant here so the collision
    # cannot come back.
    check("...and nothing added here puts a `digest` key back into the "
          "console's view",
          '"digest"' not in json.dumps(r.status()),
          json.dumps(r.status())[:160])

    # == G. limits, metering and the correlation id =========================
    check("a routed request is still counted against the key that made it",
          any(k.get("id") == alice_rec["id"] and int(k.get("requests") or 0) > 0
              for k in r.status()["keys"]))
    tokens = r.status()["totals"]["tokens_by_key"]
    check("the upstream's own token counts are attributed to that key",
          tokens.get(alice_rec["id"], {}).get("total_tokens", 0) >= 16,
          json.dumps(tokens))
    check("...and are not attributed to a key that never called it",
          bob_rec["id"] not in tokens
          or tokens[bob_rec["id"]].get("total_tokens", 0)
          < tokens[alice_rec["id"]].get("total_tokens", 0),
          json.dumps(tokens))
    check("the totals say how many reports they are made of, so an absent "
          "figure cannot read as a measured zero",
          all(row.get("reports", 0) > 0 for row in tokens.values()),
          json.dumps(tokens))

    routed = [u for u in r.usage() if u.get("engine_id")]
    check("the usage log names the engine, the endpoint, the owner and the "
          "caller principal",
          bool(routed) and all(u["engine_id"] and u["endpoint_id"]
                               and u["owner_uid"] and u["caller_principal"]
                               for u in routed),
          json.dumps(routed[-1] if routed else {}))
    legacy_lines = [u for u in r.usage()
                    if u.get("engine_id") is None and u.get("status") == 200]
    check("a request on the legacy route logs null for engine and endpoint, "
          "which is the reading 'nothing was routed'",
          bool(legacy_lines), json.dumps(legacy_lines[-1] if legacy_lines else {}))
    refused_lines = [u for u in r.usage() if u.get("outcome") == "refused"]
    check("a routing refusal is logged as a refusal, with a stable reason",
          bool(refused_lines) and refused_lines[-1]["error"] in
          ("not_shared_with_you", "principal_unbound", "endpoint_revoked"),
          json.dumps(refused_lines[-1] if refused_lines else {}))

    status, hdrs, resp = r.chat(alice_key, "alice-private",
                                headers={"X-Request-Id": "forged-by-the-client"})
    rid = hdrs.get("X-Request-Id")
    check("every response carries a server-side correlation id",
          bool(rid) and len(rid) == 32, "X-Request-Id=%r" % (rid,))
    check("a client-supplied X-Request-Id is not honoured",
          rid != "forged-by-the-client", "X-Request-Id=%r" % (rid,))
    check("...and it never reached the engine either",
          all(r_["headers"].get("x-request-id") != "forged-by-the-client"
              for r_ in posts("engine-alpha")))
    # The usage line is written *after* the answer has gone out -- the
    # measurements it carries do not exist before then -- so this polls rather
    # than reading the file the instant the response returns.
    check("the id the caller was shown is the id in the log line",
          wait_for(lambda: any(u.get("request_id") == rid for u in r.usage()),
                   timeout=3.0))
    check("CONTROL: two requests get two different ids",
          r.chat(alice_key, "alice-private")[1].get("X-Request-Id") != rid)

    # Two requests over ONE connection.  An id minted per *connection* would
    # pass every check above and still show two different requests sharing an id
    # in the log, which is the property the id exists for.
    conn = http.client.HTTPConnection("localhost", r.srv.server_address[1],
                                      timeout=10)
    try:
        conn.request("GET", "/healthz")
        first = conn.getresponse()
        first.read()
        id1 = first.getheader("X-Request-Id")
        conn.request("GET", "/healthz")
        second = conn.getresponse()
        second.read()
        id2 = second.getheader("X-Request-Id")
    finally:
        conn.close()
    check("the id is per request, not per connection: two requests on one "
          "kept-alive connection get two ids",
          bool(id1) and bool(id2) and id1 != id2, "%r then %r" % (id1, id2))

    # One slot, two engines: routing does not create capacity.
    a_srv.spec["behaviour"] = {"slow": 1.5}
    held: dict = {}

    def hold():
        held["r"] = r.chat(alice_key, "alice-private", timeout=30)

    th = threading.Thread(target=hold, daemon=True)
    th.start()
    check("CONTROL: the slow request really is holding the single slot",
          wait_for(lambda: r.gate.stats()["active"] == 1, timeout=5.0))
    status, _h, resp = r.chat(bob_key, "bob-private")
    check("a request to a *second* engine is still refused while the first is "
          "running: the concurrency limit is the box's, and routing does not "
          "multiply it", status == 429, "status=%s" % status)
    check("...while a request refused for identity is answered at once instead "
          "of queueing for a slot", r.chat(bob_key, "alice-private")[0] == 404)
    th.join(timeout=15)
    a_srv.spec["behaviour"] = {}
    check("the caller that held the slot got its answer",
          bool(held.get("r")) and held["r"][0] == 200)
    check("...and the slot is free again",
          wait_for(lambda: r.gate.stats()["active"] == 0, timeout=5.0))

    # == H. a snapshot that cannot be trusted is not loaded =================
    def fresh(name: str, doc: dict, mode: int = 0o600) -> str:
        return write_snapshot(os.path.join(tmp, "%s.json" % name), doc, mode)

    def not_loaded(desc, doc, name, mode=0o600):
        """A snapshot refused *whole*: nothing is routed, and the legacy route
        keeps working, which is what makes refusing safe rather than an outage."""
        hh = Harness(legacy_port, store_path, tmp, fresh(name, doc, mode),
                     name=name, global_limit=1, per_user_limit=1)
        st = hh.status()["routing"]
        legacy_ok = hh.chat(alice_key, "strata-iq2-xs")[0] == 200
        check(desc, st["loaded"] is False and bool(st["error"]) and legacy_ok,
              json.dumps(st)[:240])

    def engine_dropped(desc, doc, name, why_says):
        """A snapshot that loads, minus the engine that was refused.

        An engine entry the gateway will not accept takes its endpoints with
        it, and the reason is published -- one bad address in a registry export
        does not have to revert everybody to a single upstream, and it does not
        get in quietly either.
        """
        hh = Harness(legacy_port, store_path, tmp, fresh(name, doc), name=name,
                     global_limit=1, per_user_limit=1)
        st = hh.status()["routing"]
        reasons = " | ".join(r_["why"] for r_ in st["rejected_entries"])
        gone = all(ep["engine_id"] != "engine-alpha" for ep in st["endpoints"])
        names = [m.get("id") for m in json_of(
            call(hh.url("/v1/models"), key=alice_key)[2]).get("data", [])]
        check(desc, "engine-alpha" not in {e["engine_id"] for e in st["engines"]}
              and why_says in reasons and gone
              and "shared-by-uid" not in names,
              "engines=%s rejected=%s models=%s"
              % ([e["engine_id"] for e in st["engines"]], reasons[:200], names))
        return hh

    base = good_snapshot(a_port, b_port)

    not_loaded("a group-writable snapshot is refused whole, and the legacy "
               "route keeps working", variant(base), "groupwritable", 0o660)
    not_loaded("a snapshot with the wrong schema string is refused whole",
               variant(base, schema="llmlab.endpoint-routing/99"), "badschema")

    non_loop = variant(base)
    non_loop["engines"][0]["upstream"] = {"transport": "http",
                                          "host": "{{PEER_IP}}", "port": 9999}
    engine_dropped("an engine on an address the operator did not allowlist is "
                   "refused, and its models go with it", non_loop, "nonloop",
                   "neither loopback nor on the operator's allowlist")

    named = variant(base)
    named["engines"][0]["upstream"] = {"transport": "http",
                                       "host": "engine.internal", "port": 9999}
    engine_dropped("an engine named by hostname is refused: nothing is "
                   "resolved at connect time", named, "hostname",
                   "is not an IP literal")

    filed = variant(base)
    filed["engines"][0]["upstream"] = {"transport": "file",
                                       "path": "/etc/passwd"}
    engine_dropped("a file:// upstream is refused by the schema, so no code "
                   "path could ever fetch it", filed, "filescheme",
                   "transport must be")

    inline = variant(base)
    inline["engines"][0]["secret_ref"] = {"value": "inline-test-value"}
    engine_dropped("an inline secret in the snapshot is refused", inline,
                   "inlinesecret", "inline secrets are refused")

    skipped = variant(base)
    skipped["engines"][0]["kind"] = "some-other-protocol"
    engine_dropped("an engine announcing a protocol this gateway cannot speak "
                   "is refused rather than forwarded to", skipped, "badkind",
                   "kind must be")

    allowed = Harness(legacy_port, store_path, tmp,
                      fresh("allowlisted", non_loop), name="allowed",
                      global_limit=1, per_user_limit=1, allowed=["{{PEER_CIDR}}"])
    check("CONTROL: the same non-loopback engine loads once the operator "
          "allowlists the address on the command line -- and only there, never "
          "from the snapshot",
          allowed.status(timeout=30.0)["routing"]["loaded"] is True,
          json.dumps(allowed.status(timeout=30.0)["routing"])[:240])

    # An entry that is only *individually* wrong does not take the snapshot
    # down: the good endpoints keep working and the reason is published.
    one_bad = variant(base)
    one_bad["endpoints"][1]["engine_id"] = "engine-that-does-not-exist"
    one_bad["endpoints"].append(endpoint("ep-bad-acl", "engine-alpha",
                                         "bad-acl", "real-alice-model", "alice",
                                         acl={"kind": "everyone"}))
    hh = Harness(legacy_port, store_path, tmp, fresh("one-bad", one_bad),
                 name="onebad", global_limit=1, per_user_limit=1)
    st = hh.status()["routing"]
    check("one malformed endpoint does not take the whole snapshot down",
          st["loaded"] is True and len(st["endpoints"]) == 3,
          json.dumps(st)[:200])
    check("...it is dropped, and the reason is published rather than lost",
          len(st["rejected_entries"]) == 2
          and all(r_.get("why") for r_ in st["rejected_entries"]),
          json.dumps(st["rejected_entries"]))
    check("...and the dropped endpoint is not routable and not enumerable",
          "bad-acl" not in [m.get("id") for m in json_of(
              call(hh.url("/v1/models"), key=alice_key)[2]).get("data", [])])

    conflict = Router(fresh("conflict", base),
                      reserved_aliases={"alice-private": "strata-iq2-xs"},
                      key_reader=read_key_file)
    check("a snapshot that redeclares a name the operator already published "
          "with --alias is refused rather than silently taking precedence",
          conflict.error is not None and "two meanings" in conflict.error,
          repr(conflict.error))

    missing = variant(base)
    missing["engines"] = [
        engine_entry("engine-alpha", a_port, key_var="ENGINE_A_KEY"),
        engine_entry("engine-none", b_port, key_var="ENGINE_NOT_THERE")]
    hh = Harness(legacy_port, store_path, tmp, fresh("no-secret", missing),
                 name="nosecret", global_limit=1, per_user_limit=1)
    engines = {e["engine_id"]: e for e in hh.status()["routing"]["engines"]}
    check("an engine whose credential cannot be read is reported unusable, and "
          "is routed to by nobody",
          engines["engine-none"]["usable"] is False
          and "ENGINE_NOT_THERE" in (engines["engine-none"]["why"] or ""),
          json.dumps(engines["engine-none"]))
    check("...and it is not probed as if it were merely down",
          engines["engine-none"]["healthy"] is None
          and engines["engine-none"]["checked_at"] is not None,
          json.dumps(engines["engine-none"]))

    # == I. the documented shape is the shape that loads ====================
    example = os.path.join(ROOT, "deploy", "dell", "gateway",
                           "routing-snapshot.example.json")
    ex = Router(example, key_reader=read_key_file)
    check("the snapshot shipped as the worked example is one this gateway "
          "actually loads, so the documented shape is not a wish",
          ex.error is None and len(ex.engines) == 3 and len(ex.endpoints) == 3,
          "%s / %s" % (ex.error, sorted(ex.by_alias)))
    check("...and it shows all three ACL kinds",
          {ep.acl_kind for ep in ex.endpoints} == {"private", "users", "all"},
          sorted(ep.acl_kind for ep in ex.endpoints))
    check("...and it is not readable by a caller: no engine's secret is in it",
          ENGINE_A_KEY not in open(example, encoding="utf-8").read())

    # The *startup* path, which is not the same as "a Router can be built".
    # `main()` prints a line about the snapshot before it serves anything, and
    # this check exists because a rename that missed that line took the whole
    # gateway down -- legacy route included -- the first time a snapshot was
    # configured, while every other check in this file went on passing.
    import deploy.dell.gateway.gateway as gwmod
    started = io.StringIO()
    real_serve = gwmod.Server.serve_forever
    gwmod.Server.serve_forever = lambda self: (_ for _ in ()).throw(
        KeyboardInterrupt)
    try:
        with contextlib.redirect_stdout(started), contextlib.redirect_stderr(
                started):
            rc = gwmod.main([
                "--listen", "localhost:0",
                "--upstream", "http://localhost:%d" % legacy_port,
                "--key-file", SECRETS_PATH, "--key-var", "ENGINE_A_KEY",
                "--keys", os.path.join(tmp, "startup-keys.json"),
                "--usage-log", os.path.join(tmp, "startup-usage.jsonl"),
                "--status-file", os.path.join(tmp, "startup-status.json"),
                "--routing-snapshot", example])
    finally:
        gwmod.Server.serve_forever = real_serve
    check("the gateway starts when a snapshot is configured, and says so: the "
          "startup path is the one nothing else here exercises",
          rc == 0 and "routing: 3 engine(s)" in started.getvalue(),
          started.getvalue()[-260:])

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = admin_cli.main(["--store", store_path, "key", "create",
                             "carol-laptop", "--owner", "carol"])
    check("the admin CLI binds a new key at creation time",
          rc == 0 and "bound to carol" in out.getvalue()
          and any(k.get("owner") == "carol"
                  for k in KeyStore(store_path).list_records()),
          out.getvalue()[:120])
    bad = io.StringIO()
    with contextlib.redirect_stderr(bad), contextlib.redirect_stdout(bad):
        rc = admin_cli.main(["--store", store_path, "key", "create",
                             "typo", "--owner", "Not A User"])
    check("...and refuses an owner that is not a user name or uid, rather "
          "than binding it to something nothing will ever match",
          rc != 0 and "owner must be" in bad.getvalue(), bad.getvalue()[:160])


def main(argv=None) -> int:
    with tempfile.TemporaryDirectory(prefix="llmlab-routing-selftest-") as tmp:
        try:
            run(tmp)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILED.append(("the run raised", "see the traceback above"))
    print("-" * 72)
    for name, detail in FAILED:
        print("FAILED  %s\n        %s" % (name, detail))
    print("%d passed, %d failed" % (len(PASSED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
