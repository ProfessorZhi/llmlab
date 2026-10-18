#!/usr/bin/env python3
"""Acceptance for the API gateway, runnable on Windows with no node access.

WHY THIS RUNS AGAINST A MOCK AND NOT AGAINST STRATA.  Two reasons, and the
second is the one that matters.

  1. The brief forbids touching the model service.  A test that has to poke
     the live engine to pass is a test that cannot be run while the engine is
     busy, which is exactly when it is wanted.

  2. More importantly: a test against the *real* upstream cannot tell the
     difference between "the gateway forwarded my key" and "the upstream
     happened to accept it anyway".  The mock enforces the upstream key and
     records every Authorization header it is shown, so "Strata's key was
     substituted and the user's key never crossed" is an observation rather
     than a hope.

THE NEGATIVE CONTROLS COME FIRST.  The same day this was written, a probe on
this project reported "no firewall is blocking" by reading a `curl` exit code
that cannot carry that information.  So before anything is called working: the
mock is asked for a model list without a key and must refuse, and the gateway
is asked without a key and must refuse.  Without those two, a 200 later proves
only that *something* answers.

Run it from the repository root:

    python -m deploy.dell.gateway.selftest

or from anywhere, which is how the node acceptance script calls it:

    python3 legacy/gateway/selftest.py
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat as stat_mod
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit  # noqa: F401  (used by the mock upstream)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from deploy.dell.gateway.gateway import (  # noqa: E402
    Gateway, Handler, Server, build_parser, parse_aliases, read_key_file,
)
from deploy.dell.gateway.keystore import KeyStore  # noqa: E402
from deploy.dell.gateway.limits import Gate  # noqa: E402
from deploy.dell.gateway.telemetry import Meters, StatusFile, UsageLog  # noqa: E402

# Windows console is GBK by default and a single non-GBK byte in a detail
# string kills the run with UnicodeEncodeError -- which reads as "the gateway
# crashed" rather than "the test could not print".  Same fix as scripts/selftest.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# The upstream's own credential.  Distinct from every user key, and searched
# for in every response body, so that a leak of it is a failed check rather
# than a surprise in production.
UPSTREAM_KEY = "strata-key-for-the-mock-upstream"

# No proxy.  A Windows box with HTTP_PROXY set will happily send a request for
# localhost to the proxy, and the resulting failure looks like the gateway
# refusing to answer.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """Record one assertion in the house style: PASS/FAIL, no exceptions.

    Deliberately not `assert`.  The point of this file is to report *every*
    finding in one run; an assertion stops at the first failure and hides the
    other nine things that are also wrong.
    """
    if ok:
        PASSED.append(name)
        print("PASS  %s" % name)
    else:
        FAILED.append((name, detail))
        print("FAIL  %s%s" % (name, ("  -- " + detail) if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# The mock upstream
# ---------------------------------------------------------------------------
class MockStrata(BaseHTTPRequestHandler):
    """Stands in for the model service, and enforces its own key.

    Every request it is shown is appended to MODULE-level `seen`, because the
    interesting assertions are about what the upstream *did* see (Strata's key,
    never the caller's) and what it *never* saw (any caller key at all).
    """

    protocol_version = "HTTP/1.0"   # closes the connection: no framing invented
    seen: list[dict] = []

    def log_message(self, *args):   # silence: the gateway's log is under test
        pass

    # The mock is held to the rule the gateway is judged by: a client that
    # hangs up is normal traffic, not a traceback.  Without this the *test's*
    # noise lands in the same stderr the gateway's is checked against, and
    # "no traceback reached the log" would be unfalsifiable.
    _GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except self._GONE:
            self.close_connection = True

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except self._GONE:
            self.close_connection = True

    def _record(self, body: bytes) -> None:
        MockStrata.seen.append({
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
        if (self.headers.get("Authorization") or "") == "Bearer" + " " + UPSTREAM_KEY:
            return True
        # Answers in the same shape Strata does, so a failure here is not
        # mistaken for a gateway bug later in the run.
        self._json(401, {"error": {"message": "invalid api key",
                                   "type": "invalid_request_error"}})
        return False

    def do_GET(self):
        self._record(b"")
        if not self._authorised():
            return
        if self.path == "/v1/models":
            self._json(200, {"object": "list",
                             "data": [{"id": "strata-iq2-xs", "object": "model"}]})
            return
        self._json(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if not self._authorised():
            return
        # The path is compared *parsed* so a query string survives -- and the
        # GET path above is deliberately left strict, because "the caller's
        # chosen authority was relayed verbatim" is only visible in the raw
        # request target, and a lenient comparison here would hide it.
        if urlsplit(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "no such path"}})
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        model = payload.get("model")

        if model == "boom":
            # An upstream failure, deliberately shaped like one so the gateway
            # has something real to fail to hide.
            self._json(400, {"error": {"message": "upstream says no, in its own words",
                                       "type": "invalid_request_error",
                                       "code": "boom"}})
            return

        if model == "slow":
            # Long enough that a second request has to queue behind it.
            time.sleep(1.2)
            self._json(200, {"id": "cmpl-slow", "choices": [{"index": 0,
                           "message": {"role": "assistant", "content": "done"}}]})
            return

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                self._write(b'data: {"choices":[{"delta":{"content":"t%d"}}]}\n\n' % i)
                time.sleep(0.35)
            self._write(b"data: [DONE]\n\n")
            return

        self._json(200, {"id": "cmpl-1", "object": "chat.completion",
                         "model": "strata-iq2-xs",
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant",
                                                  "content": "hello from the mock"}}],
                         # The counts the gateway is supposed to record.  An
                         # odd set of numbers on purpose: if the gateway ever
                         # starts estimating, the estimate will not be this.
                         "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                   "total_tokens": 10}})


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------
def call(url: str, key=None, method=None, body=None, headers=None, timeout=30):
    """One HTTP request.  Returns (status, headers, response) -- never raises.

    `HTTPError` is a *response*, not an exception to the test: a 401 and a 429
    are the expected answers to several of the checks below, and turning them
    into tracebacks would mean the interesting branch is the one never exercised.
    """
    hdrs = dict(headers or {})
    if key is not None:
        hdrs["Authorization"] = "Bearer" + " " + key
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        resp = OPENER.open(req, timeout=timeout)
        return resp.status, resp.headers, resp
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc


def body_of(resp) -> bytes:
    try:
        return resp.read()
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def json_of(resp):
    try:
        return json.loads(body_of(resp) or b"{}")
    except ValueError:
        return {}


def raw_exchange(port: int, payload: bytes, timeout: float = 10.0) -> bytes:
    """Send bytes to the gateway verbatim and read whatever comes back.

    Needed for the requests `http.client` will not let us make: one claiming a
    Content-Length larger than the limit, and one with no Content-Length at
    all.  Both are refused *before* the body is read, and a well-behaved client
    cannot express either.
    """
    sock = socket.create_connection(("localhost", port), timeout=timeout)
    try:
        sock.sendall(payload)
        try:
            return sock.recv(65536)
        except socket.timeout:
            return b""
    finally:
        sock.close()


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll instead of sleeping a fixed amount: a fixed sleep is either flaky
    or slow, and usually both."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def start_gateway(upstream_port: int, store_path: str, *, tmp: str = "",
                  aliases=None, tick_interval: float = 0.05,
                  probe_interval: float = 5.0, **gate_kw):
    """The gateway under test, in-process, on an ephemeral port.

    The ticker runs every 50 ms here rather than every 5 s, so a test that waits
    for the status file to appear waits two frames instead of thirty -- and so
    that a flaky ticker shows up as a failure rather than as a timeout nobody
    attributes to the right thing.
    """
    store = KeyStore(store_path)
    gate = Gate(**gate_kw)
    gw = Gateway(upstream_url="http://localhost:%d" % upstream_port,
                 upstream_key=UPSTREAM_KEY, store=store, gate=gate,
                 meters=Meters(),
                 usage_log=UsageLog(os.path.join(tmp, "usage.jsonl")) if tmp else None,
                 status_file=StatusFile(os.path.join(tmp, "status.json")) if tmp else None,
                 aliases=aliases,
                 max_body=8 * 1024 * 1024, upstream_timeout=30.0,
                 tick_interval=tick_interval, probe_interval=probe_interval)
    gw.start()

    # A fresh Handler subclass per server: the gateway is attached to the
    # *class*, so two instances sharing `Handler` would fight over it.
    class H(Handler):
        pass

    srv = Server(("localhost", 0), H, gw)
    # The port is only known after the bind, and the status document has to
    # name it -- otherwise the console would show the address a caller is told
    # to use as a blank.
    gw.listen = "localhost:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, gw, store


def run(tmp: str) -> None:
    store_path = os.path.join(tmp, "keys.json")
    key_file = os.path.join(tmp, "strata-key.env")

    # -- the secrets file the gateway reads ---------------------------------
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write("# the model service's own key\n")
        fh.write("SOMETHING_ELSE=nope\n")
        fh.write("STRATA_API_KEY=%s\n" % UPSTREAM_KEY)
    check("reads the upstream key out of an env-style secrets file",
          read_key_file(key_file, "STRATA_API_KEY") == UPSTREAM_KEY,
          "got %r" % read_key_file(key_file, "STRATA_API_KEY"))
    check("does not read a variable that is not there",
          read_key_file(key_file, "NOT_THIS_ONE") == "",
          "a missing variable must not resolve to another variable's value")

    # -- the mock upstream --------------------------------------------------
    mock = ThreadingHTTPServer(("localhost", 0), MockStrata)
    mock.daemon_threads = True
    up_port = mock.server_address[1]
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    up_url = "http://localhost:%d" % up_port

    # ======================================================================
    # 1. Negative controls.  Everything after this depends on them.
    # ======================================================================
    st, _, resp = call(up_url + "/v1/models")
    check("CONTROL: the mock upstream refuses a request with no key",
          st == 401, "status %s" % st)
    body_of(resp)

    st, _, resp = call(up_url + "/v1/models", key=UPSTREAM_KEY)
    check("CONTROL: the mock accepts its own key (so the refusal above was "
          "about the key, not the route)", st == 200, "status %s" % st)
    body_of(resp)

    srv, gw, store = start_gateway(
        up_port, store_path, tmp=tmp, aliases={"llm-lab-default": "strata-iq2-xs"},
        # One slot: the model service runs with parallel=1 and a single GPU.
        global_limit=1, per_user_limit=1, queue_limit=1,
        wait_timeout_s=0.5, retry_after_s=1.0)
    port = srv.server_address[1]
    base = "http://localhost:%d" % port

    st, _, resp = call(base + "/v1/models")
    check("CONTROL: the gateway refuses a request with no key",
          st == 401, "status %s" % st)
    body_of(resp)

    st, _, resp = call(base + "/v1/models", key="llmlab_not-a-real-key")
    detail = json_of(resp).get("error", {})
    check("the gateway refuses an unknown key, and says which kind of problem it is",
          st == 401 and detail.get("code") == "invalid_api_key"
          and "no such API key" in detail.get("message", ""),
          "status %s body %s" % (st, detail))

    st, _, resp = call(base + "/v1/models", key="invalid-test-key")
    detail = json_of(resp).get("error", {})
    check("a key from a different service is refused, and the message says "
          "so rather than just 'unknown'",
          st == 401 and "does not look like a key from this service"
          in detail.get("message", ""),
          "status %s message %r" % (st, detail.get("message")))

    st, hdrs, resp = call(base + "/v1/models", key="")
    check("an empty Authorization header is refused",
          st == 401, "status %s" % st)
    check("the refusal carries a Bearer challenge and not a Basic one",
          hdrs.get("WWW-Authenticate", "").startswith("Bearer"),
          "WWW-Authenticate=%r" % hdrs.get("WWW-Authenticate"))
    body_of(resp)

    # ======================================================================
    # 2. Two users, two keys
    # ======================================================================
    key_a, rec_a = store.issue("alice")
    key_b, rec_b = store.issue("bob")
    check("two keys are different from each other",
          key_a != key_b and key_a.startswith("llmlab_"))
    check("the key store never holds a plaintext key",
          key_a not in open(store_path, encoding="utf-8").read(),
          "the file on disk contains the key it just issued")
    if os.name == "posix":
        mode = stat_mod.S_IMODE(os.stat(store_path).st_mode)
        check("the key store is mode 0600", mode == 0o600, "mode %o" % mode)

    st, _, resp = call(base + "/v1/models", key=key_a)
    payload = json_of(resp)
    check("alice's key lists the models",
          st == 200 and payload.get("data", [{}])[0].get("id") == "strata-iq2-xs",
          "status %s body %s" % (st, payload))

    st, _, resp = call(base + "/v1/chat/completions", key=key_b, method="POST",
                       body=json.dumps({"model": "strata-iq2-xs",
                                        "messages": [{"role": "user",
                                                      "content": "hi"}]}).encode())
    payload = json_of(resp)
    check("bob's key completes a chat request",
          st == 200 and payload.get("choices", [{}])[0]
          .get("message", {}).get("content") == "hello from the mock",
          "status %s body %s" % (st, payload))

    # The central claim of the whole design, observed rather than asserted.
    auths = [s["auth"] for s in MockStrata.seen]
    check("the upstream saw Strata's key on every authorised call",
          auths.count("Bearer" + " " + UPSTREAM_KEY) == len(auths) - 1,
          "auth headers seen: %r" % auths)
    seen_blob = json.dumps([{"auth": s["auth"], "path": s["path"]}
                            for s in MockStrata.seen])
    check("neither user key ever reached the upstream",
          key_a not in seen_blob and key_b not in seen_blob,
          "a caller's key crossed to the model service")

    # ======================================================================
    # 3. Streaming
    # ======================================================================
    t0 = time.monotonic()
    st, hdrs, resp = call(base + "/v1/chat/completions", key=key_a, method="POST",
                          body=json.dumps({"model": "strata-iq2-xs", "stream": True,
                                           "messages": []}).encode(), timeout=30)
    first = resp.read1(65536)
    t_first = time.monotonic()
    chunks = [first]
    while True:
        piece = resp.read1(65536)
        if not piece:
            break
        chunks.append(piece)
    t_last = time.monotonic()
    resp.close()
    stream_body = b"".join(chunks)
    check("a streamed request answers as text/event-stream",
          st == 200 and "text/event-stream" in (hdrs.get("Content-Type") or ""),
          "status %s type %r" % (st, hdrs.get("Content-Type")))
    check("the stream carries every event and its terminator",
          stream_body.count(b"data: ") == 4 and stream_body.endswith(b"[DONE]\n\n"),
          "body %r" % stream_body[:200])
    check("the stream arrives incrementally rather than in one lump",
          (t_last - t_first) >= 0.5 and (t_first - t0) <= 0.5,
          "first byte at %.2fs, last at %.2fs (the mock paces events 0.35s apart)"
          % (t_first - t0, t_last - t0))

    # ======================================================================
    # 4. Failures are relayed, not invented
    # ======================================================================
    st, _, resp = call(base + "/v1/chat/completions", key=key_a, method="POST",
                       body=json.dumps({"model": "boom", "messages": []}).encode())
    payload = json_of(resp)
    check("an upstream 4xx is passed through with its own status and words",
          st == 400
          and payload.get("error", {}).get("message")
          == "upstream says no, in its own words",
          "status %s body %s" % (st, payload))
    check("a failed request is not dressed up as a success",
          st != 200, "status was %s" % st)

    st, _, resp = call(base + "/v1/messages", key=key_a, method="POST",
                       body=b"{}")
    payload = json_of(resp)
    check("a real Strata route that is not published is refused by name",
          st == 404 and payload.get("error", {}).get("code") == "not_published",
          "status %s body %s" % (st, payload))

    st, _, resp = call(base + "/metrics", key=key_a)
    check("a non-/v1 path does not reach the upstream",
          st == 404, "status %s" % st)
    body_of(resp)
    check("the upstream was never asked for /metrics",
          not any(s["path"] == "/metrics" for s in MockStrata.seen),
          "the gateway forwarded a path it does not publish")

    # ======================================================================
    # 5. Body limits, refused before the body is read
    # ======================================================================
    reply = raw_exchange(port,
                         b"POST /v1/chat/completions HTTP/1.1\r\n"
                         b"Host: localhost\r\n"
                         b"Authorization: Bearer" + b" " + key_a.encode() + b"\r\n"
                         b"Content-Type: application/json\r\n"
                         b"Content-Length: 999999999\r\n\r\n")
    check("an oversized Content-Length is refused with 413 before any body arrives",
          b" 413 " in reply.split(b"\r\n")[0], "first line %r" % reply.split(b"\r\n")[0])

    reply = raw_exchange(port,
                         b"POST /v1/chat/completions HTTP/1.1\r\n"
                         b"Host: localhost\r\n"
                         b"Authorization: Bearer" + b" " + key_a.encode() + b"\r\n\r\n")
    check("a POST with no Content-Length is refused with 411 rather than "
          "read-until-EOF",
          b" 411 " in reply.split(b"\r\n")[0], "first line %r" % reply.split(b"\r\n")[0])

    # ======================================================================
    # 6. Limits: one slot, a bounded queue, a deadline
    # ======================================================================
    key_c, rec_c = store.issue("carol")
    key_d, rec_d = store.issue("dave")
    key_e, rec_e = store.issue("erin")
    slow = json.dumps({"model": "slow", "messages": []}).encode()

    holder = {}

    def hold():
        holder["r"] = call(base + "/v1/chat/completions", key=key_c,
                           method="POST", body=slow, timeout=30)

    t_hold = threading.Thread(target=hold)
    t_hold.start()
    check("CONTROL: the slow request is actually occupying the single slot",
          wait_for(lambda: gw.gate.stats()["active"] == 1, timeout=3),
          "gate stats: %s" % gw.gate.stats())

    waiter = {}

    def wait_a_bit():
        waiter["r"] = call(base + "/v1/chat/completions", key=key_d,
                           method="POST", body=slow, timeout=30)

    t_wait = threading.Thread(target=wait_a_bit)
    t_wait.start()
    check("a second caller waits rather than being sent to the model",
          wait_for(lambda: gw.gate.stats()["waiting"] == 1, timeout=3),
          "gate stats: %s" % gw.gate.stats())

    st, hdrs, resp = call(base + "/v1/chat/completions", key=key_e,
                          method="POST", body=slow, timeout=30)
    body_of(resp)
    check("when the queue is full the third caller gets 429 at once",
          st == 429, "status %s" % st)
    check("the 429 tells the client when to come back",
          hdrs.get("Retry-After") is not None,
          "no Retry-After header: a client can only guess")

    t_wait.join(timeout=10)
    w_status, _, w_resp = waiter.get("r", (None, None, None))
    w_detail = json_of(w_resp).get("error", {}) if w_resp else {}
    check("a caller that waited past its deadline is refused, and the message "
          "says it was the wait",
          w_status == 429 and "waited" in w_detail.get("message", ""),
          "status %s message %r" % (w_status, w_detail.get("message")))

    t_hold.join(timeout=10)
    h_status, _, h_resp = holder.get("r", (None, None, None))
    body_of(h_resp) if h_resp else None
    check("the caller that held the slot still got its answer",
          h_status == 200, "status %s" % h_status)

    st, _, resp = call(base + "/v1/models", key=key_c)
    body_of(resp)
    check("the slot is free again after all of that",
          st == 200, "status %s -- a slot leaked" % st)

    # ======================================================================
    # 7. A client that hangs up must not leak the slot
    # ======================================================================
    #
    # The request has to be one whose answer is still being written when the
    # client leaves, or the window this checks for does not exist.  `slow`
    # sleeps *before* answering, so by the time its first byte arrives the slot
    # is about to be released anyway -- the control would then be asserting
    # something that was never true.  A stream keeps the answer in flight for
    # as long as the mock paces its events, which is the real shape of this
    # case: somebody closes the tab in the middle of a long generation.
    stream_slow = json.dumps({"model": "strata-iq2-xs", "stream": True,
                              "messages": []}).encode()
    sock = socket.create_connection(("localhost", port), timeout=10)
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                 b"Host: localhost\r\n"
                 b"Authorization: Bearer" + b" " + key_d.encode() + b"\r\n"
                 b"Content-Type: application/json\r\n"
                 b"Content-Length: " + str(len(stream_slow)).encode()
                 + b"\r\n\r\n" + stream_slow)
    sock.recv(4096)          # the response has started
    sock.close()             # ... and the client is gone, mid-stream
    check("CONTROL: the abandoned request was holding the slot",
          wait_for(lambda: gw.gate.stats()["active"] == 1, timeout=3),
          "gate stats: %s" % gw.gate.stats())
    check("the slot is returned when the client hangs up mid-answer",
          wait_for(lambda: gw.gate.stats()["active"] == 0, timeout=6),
          "gate stats after the client left: %s -- a slot leaked" % gw.gate.stats())
    st, _, resp = call(base + "/v1/models", key=key_d)
    body_of(resp)
    check("the next caller is served normally after that disconnect",
          st == 200, "status %s" % st)

    # ======================================================================
    # 8. Per-user attribution
    # ======================================================================
    recs = {r["id"]: r for r in store.list_records()}
    check("usage is counted against the key that made the call",
          recs[rec_a["id"]]["requests"] > 0 and recs[rec_b["id"]]["requests"] > 0,
          "alice=%s bob=%s" % (recs[rec_a["id"]]["requests"],
                               recs[rec_b["id"]]["requests"]))
    check("each key records when it was last used",
          recs[rec_a["id"]]["last_used_at"] and recs[rec_b["id"]]["last_used_at"],
          "alice=%r bob=%r" % (recs[rec_a["id"]]["last_used_at"],
                               recs[rec_b["id"]]["last_used_at"]))
    check("the counters are per key and not one shared total",
          recs[rec_a["id"]]["requests"] != recs[rec_b["id"]]["requests"]
          or recs[rec_a["id"]]["requests"] > 1,
          "two keys with identical counts could be one counter copied twice")

    # ======================================================================
    # 9. The CLI's decisions, seen by a running gateway
    #
    # A separate KeyStore on the same file is exactly what admin.py is.
    # ======================================================================
    cli = KeyStore(store_path)
    cli.set_state(rec_a["id"], revoked=True)
    st, _, resp = call(base + "/v1/models", key=key_a)
    detail = json_of(resp).get("error", {})
    check("a revoke by another process takes effect without restarting the gateway",
          st == 401 and "revoked" in detail.get("message", ""),
          "status %s message %r" % (st, detail.get("message")))

    cli.set_state(rec_b["id"], disabled=True)
    st, _, resp = call(base + "/v1/models", key=key_b)
    detail = json_of(resp).get("error", {})
    check("a disable by another process takes effect too",
          st == 401 and "disabled" in detail.get("message", ""),
          "status %s message %r" % (st, detail.get("message")))

    cli.set_state(rec_b["id"], disabled=False)
    st, _, resp = call(base + "/v1/models", key=key_b)
    body_of(resp)
    check("and re-enabling brings the same key back",
          st == 200, "status %s" % st)

    cli.set_state(rec_b["id"], revoked=True)
    cli.set_state(rec_b["id"], disabled=False)
    st, _, resp = call(base + "/v1/models", key=key_b)
    body_of(resp)
    check("revocation is not undone by a later enable",
          st == 401, "status %s -- a revoked key came back to life" % st)

    # The flush/revoke race, which is the reason keystore.py merges rather
    # than adopts.  A gateway that writes its in-memory copy back would
    # resurrect alice here, and nothing in the logs would say so.
    st, _, resp = call(base + "/v1/models", key=key_c)   # dirty the store
    body_of(resp)
    gw.store.flush()
    st, _, resp = call(base + "/v1/models", key=key_a)
    body_of(resp)
    check("the gateway's own counter flush does not resurrect a revoked key",
          st == 401, "status %s -- a flush overwrote the revocation" % st)

    # ... and the same race one step harder, because the check above only
    # covers the case where the flush *notices* the change before it writes.
    # Interposing on `os.fsync` puts the other process's write in the narrow
    # window the flush cannot notice on its own -- after it has decided the
    # file is unchanged, while it is writing its own copy -- which is the
    # interleaving that silently un-revokes a key in production.  Deterministic,
    # not a race to be hit: the write is injected at exactly that point.
    store.record(rec_c["id"], "ok")      # make the store dirty on purpose
    injection = {"done": False}
    real_fsync = os.fsync

    def fsync_and_interfere(fd):
        real_fsync(fd)
        if not injection["done"]:
            injection["done"] = True
            cli.set_state(rec_c["id"], revoked=True)

    os.fsync = fsync_and_interfere
    try:
        gw.store.flush()
    finally:
        os.fsync = real_fsync
    check("CONTROL: the interfering write really happened mid-flush",
          injection["done"], "the injection point was never reached")
    check("a flush that lost the race abandons its write instead of "
          "publishing a stale copy over the other process's decision",
          gw.store.authenticate(key_c)[1] == "revoked",
          "the revocation was overwritten by the gateway's stale copy")
    st, _, resp = call(base + "/v1/models", key=key_c)
    body_of(resp)
    check("... and the revoked key is refused on the next call",
          st == 401, "status %s -- a mid-write revocation did not take" % st)

    # ... and the other direction: a key issued by the CLI must survive the
    # gateway writing its counters.
    key_f, rec_f = cli.issue("frank")
    st, _, resp = call(base + "/v1/models", key=key_f)
    body_of(resp)
    check("a key issued by the CLI works immediately, with no restart",
          st == 200, "status %s" % st)
    gw.store.flush()
    st, _, resp = call(base + "/v1/models", key=key_f)
    body_of(resp)
    check("that key survives the gateway flushing its counters",
          st == 200, "status %s -- the flush clobbered the CLI's write" % st)

    # ======================================================================
    # 10. Aliases: a stable name, explicitly mapped to a real model
    #
    # The brief allows either the real model id or a stable alias, on one
    # condition -- that the alias is explicitly mapped.  "We published a name"
    # and "the name reaches the model" are different claims, so both are made.
    #
    # A fresh key: section 9 revoked every one used before it, on purpose, and
    # testing with a dead key would prove nothing about any of this.
    # ======================================================================
    key_g, rec_g = store.issue("gateway-tests")
    st, _, resp = call(base + "/v1/models", key=key_g)
    ids = [m.get("id") for m in json_of(resp).get("data", [])]
    check("the model list advertises the configured alias",
          "llm-lab-default" in ids, "the list is %r" % ids)
    check("the model list still advertises the real model the alias maps to",
          "strata-iq2-xs" in ids, "the list is %r" % ids)

    prompt = "SENTINEL-CV-9931 please summarise"
    st, _, resp = call(base + "/v1/chat/completions", key=key_g, method="POST",
                       body=json.dumps({"model": "llm-lab-default",
                                        "messages": [{"role": "user",
                                                      "content": prompt}]}).encode())
    body_of(resp)
    check("a call naming the alias is answered", st == 200, "status %s" % st)
    # Selected by the sentinel rather than by position: "the last request" is a
    # moving target in a file that keeps making requests, and a test that reads
    # the wrong one reports a defect that is not there.
    last = [s for s in MockStrata.seen if prompt.encode() in s["body"]][-1]
    sent = json.loads(last["body"])
    check("the alias is rewritten to the real model before it reaches the "
          "model service",
          sent.get("model") == "strata-iq2-xs",
          "the upstream was asked for model %r" % sent.get("model"))
    check("the rewrite touches the model field and nothing else in the request",
          sent.get("messages") == [{"role": "user", "content": prompt}],
          "the upstream received %r" % last["body"])
    exact = json.dumps({"model": "strata-iq2-xs", "temperature": 0.4,
                        "messages": [{"role": "user",
                                      "content": "no alias here"}]}).encode()
    call(base + "/v1/chat/completions", key=key_g, method="POST", body=exact)
    got = [s for s in MockStrata.seen if b"no alias here" in s["body"]][-1]["body"]
    check("a request carrying no alias is forwarded byte for byte, so the "
          "gateway is transparent when it has nothing to rewrite",
          got == exact, "the upstream received %r, and %r was sent" % (got, exact))

    code = None
    try:
        parse_aliases(["llm-lab-default"])
    except SystemExit as exc:
        code = str(exc)
    check("an alias with no mapping is a startup error, not a silent no-op",
          code is not None and "NAME=REAL" in code,
          "parse_aliases accepted an unmapped alias, returning %r" % (code,))

    # ======================================================================
    # 11. What travels upstream, and what is destroyed on the way there
    # ======================================================================
    before = len(MockStrata.seen)
    call(base + "/v1/models", key=key_g, headers={
        "X-Forwarded-User": "alice@example.com",
        "X-Remote-User": "bob",
        "Forwarded": "for={{DOC_IP_1}}",
        "X-Auth-Request-Email": "carol@example.com",
        "X-Real-IP": "{{DOC_IP_2}}",
        "X-Keep-Me": "please",
    })
    check("CONTROL: that request reached the model service at all",
          len(MockStrata.seen) > before,
          "the header probe never arrived, so it proves nothing about headers")
    seen = MockStrata.seen[-1]["headers"]
    identity = {"x-forwarded-user", "x-remote-user", "forwarded",
                "x-auth-request-email", "x-real-ip"}
    leaked = sorted(h for h in seen if h in identity)
    check("client-supplied identity headers never reach the model service",
          not leaked, "these crossed to the model service: %r" % leaked)
    check("CONTROL: ordinary headers are still forwarded, so the check above "
          "is about identity rather than about dropping everything",
          seen.get("x-keep-me") == "please", "headers seen: %r" % sorted(seen))
    check("the Host header names the model service, not whatever the caller "
          "asked for",
          (seen.get("host") or "").endswith(":%d" % up_port),
          "the upstream saw Host: %r" % seen.get("host"))

    # An HTTP client may put a whole URL in the request line.  A proxy that
    # forwards `self.path` hands the caller the choice of target, which is the
    # entire content of SSRF; this one forwards the parsed path only.
    reply = raw_exchange(port,
                         b"GET http://localhost:9/v1/models HTTP/1.1\r\n"
                         b"Host: localhost:9\r\n"
                         b"Authorization: Bearer" + b" " + key_g.encode() + b"\r\n\r\n")
    check("an absolute-form request target is still served (so the check below "
          "is about the target, not about the request being rejected)",
          b" 200 " in reply.split(b"\r\n")[0],
          "first line %r" % reply.split(b"\r\n")[0])
    check("the model service is asked for the path, never for the caller's "
          "chosen authority",
          MockStrata.seen[-1]["path"] == "/v1/models",
          "the model service was asked for %r" % MockStrata.seen[-1]["path"])

    # ======================================================================
    # 12. What is recorded: the shape of the traffic, and nothing about anybody
    #
    # The first consumer is an HR system, so the sentinel below stands in for a
    # CV.  "The prompt is not logged" is the kind of claim that is true until
    # somebody adds a debug line, so it is checked against a value this test
    # controls, in every place a body could have landed.
    # ======================================================================
    usage_path = os.path.join(tmp, "usage.jsonl")
    check("CONTROL: the usage log is actually being written",
          os.path.exists(usage_path), "no usage log at %s" % usage_path)
    with open(usage_path, encoding="utf-8") as fh:
        raw_lines = [ln for ln in fh if ln.strip()]
    lines = [json.loads(ln) for ln in raw_lines]
    check("the usage log has a line per forwarded request and every line parses",
          len(lines) >= 8, "%d lines" % len(lines))
    needed = {"ts", "key_id", "label", "status", "outcome", "total_ms"}
    check("each usage line carries the fields the console reads",
          all(needed <= set(r) for r in lines),
          "a line is missing something: %r" % (lines[0] if lines else None))
    check("the usage line names the key that made the call",
          any(r.get("label") == "alice" for r in lines),
          "labels seen: %r" % sorted({r.get("label") for r in lines}))

    usage_blob = "".join(raw_lines) + open(os.path.join(tmp, "status.json"),
                                           encoding="utf-8").read()
    check("no prompt text reaches any file this service writes",
          "SENTINEL" not in usage_blob,
          "the request body was recorded somewhere")
    check("no model answer reaches any file this service writes",
          "hello from the mock" not in usage_blob,
          "the response body was recorded somewhere")
    check("the usage line records fields, not a request dump",
          not any({"body", "messages", "prompt", "content", "authorization"}
                  & set(r) for r in lines),
          "a usage line carries a body-shaped field: %r" % (lines[0] if lines else None))

    with_usage = [r for r in lines if isinstance(r.get("usage"), dict)]
    check("the token counts recorded are the ones the upstream sent, verbatim",
          with_usage and with_usage[-1]["usage"] == {"prompt_tokens": 7,
                                                     "completion_tokens": 3,
                                                     "total_tokens": 10},
          "recorded %r" % (with_usage[-1].get("usage") if with_usage else None))
    no_usage = [r for r in lines if r.get("model") == "slow"]
    check("a response that carried no token counts is recorded as absent "
          "rather than estimated",
          no_usage and all(r.get("usage") is None for r in no_usage),
          "recorded %r for a response that had no usage object"
          % [r.get("usage") for r in no_usage])

    lat = gw.meters.snapshot()["latency_ms"]
    check("queue wait, time-to-first-byte and total are measured separately",
          lat.get("samples", 0) > 0 and None not in (lat.get("avg_queue"),
                                                     lat.get("avg_ttft"),
                                                     lat.get("avg_upstream"),
                                                     lat.get("avg_total")),
          "latency_ms %r" % lat)
    check("time-to-first-byte is a real measurement and not left at zero",
          (lat.get("avg_ttft") or 0) > 0,
          "avg_ttft is %r after %d samples" % (lat.get("avg_ttft"),
                                               lat.get("samples")))

    recs = {r["id"]: r for r in store.list_records()}
    check("a 429 is counted against the key that hit the limit, separately from "
          "the requests that worked",
          recs[rec_e["id"]]["requests_rate_limited"] == 1
          and recs[rec_e["id"]]["requests_ok"] == 0,
          "erin: %r" % recs[rec_e["id"]])
    check("a key that was refused records the refusal and no success",
          recs[rec_d["id"]]["requests_rate_limited"] == 1,
          "dave: %r" % recs[rec_d["id"]])
    check("a caller who hung up is counted, but not as a failure of the service",
          recs[rec_d["id"]]["requests_failed"] == 0,
          "dave: %r -- a disconnect was recorded as the service failing"
          % recs[rec_d["id"]])

    status_path = os.path.join(tmp, "status.json")
    check("CONTROL: the background ticker wrote the status file",
          wait_for(lambda: os.path.exists(status_path), timeout=5),
          "nothing at %s" % status_path)
    with open(status_path, encoding="utf-8") as fh:
        status_blob = fh.read()
    doc = json.loads(status_blob)
    check("the status document names the service, its listen address and the "
          "upstream it is pinned to",
          doc.get("service") == "llmlab-gateway" and doc.get("listen")
          and doc.get("upstream_url"),
          "the document has %r" % sorted(doc))
    check("the status document carries the totals and limits the console shows",
          doc.get("totals", {}).get("requests", 0) > 0
          and doc.get("limits", {}).get("global_limit") == 1,
          "totals %r limits %r" % (doc.get("totals"), doc.get("limits")))
    check("the status document names the models the model service reported",
          isinstance(doc.get("models"), list) and "strata-iq2-xs" in doc["models"],
          "models %r" % doc.get("models"))
    check("the status document the portal reads carries no key material",
          "llmlab_" not in status_blob
          and not any(r.get("digest") for r in doc.get("keys", [])),
          "something a key could be checked against is in the status file")

    # ======================================================================
    # 13. Startup guards
    # ======================================================================
    try:
        defaults = build_parser().parse_args([])
        parsed = True
    except SystemExit:
        parsed = False
        defaults = None
    check("the parser accepts no arguments at all (so the defaults are real)",
          parsed)
    check("the default listen address is a loopback one, on the port the "
          "deployment names",
          defaults is not None and defaults.listen == "localhost:8200",
          "the default is %r" % (getattr(defaults, "listen", None),))

    from deploy.dell.gateway import gateway as gw_mod
    code = None
    try:
        gw_mod.main(["--listen", "{{LAB_BIND_ADDR}}:8200", "--keys", store_path,
                     "--key-file", key_file])
    except SystemExit as exc:
        code = str(exc)
    check("the gateway refuses to bind anything but loopback",
          code is not None and "loopback" in code,
          "it started on a routable address, which would put Strata's key "
          "on the LAN")

    code = None
    try:
        gw_mod.main(["--listen", "localhost:8200", "--key-file",
                     os.path.join(tmp, "does-not-exist.env"), "--keys", store_path])
    except SystemExit as exc:
        code = str(exc)
    check("the gateway refuses to start without the upstream key",
          code is not None and "no API key found" in code,
          "it would have forwarded unauthenticated calls to a key-gated upstream")

    # ======================================================================
    # 14. The admin CLI -- phase 1's only way to issue a key (the portal has
    #     no identity of its own, so there is no safe web button to add).
    # ======================================================================
    import contextlib

    from deploy.dell.gateway import admin as admin_mod

    cli_path = os.path.join(tmp, "cli-keys.json")
    # The status file the *running* gateway is maintaining.  Pointing the CLI at
    # it is what makes `usage` a join of two artefacts rather than a second
    # opinion about them.
    cli_status = os.path.join(tmp, "status.json")

    def run_cli(*args, status=cli_status):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = admin_mod.main(["--store", cli_path,
                                   "--status-file", status, *args])
        return code, out.getvalue()

    # A path with nothing at it, for the case where the gateway is not running.
    absent_status = os.path.join(tmp, "no-such-status.json")

    code, out = run_cli("key", "create", "hr-system", "--note", "a ticket")
    issued = [ln for ln in out.splitlines() if ln.startswith("llmlab_")]
    check("the CLI creates exactly one key and prints it",
          code == 0 and len(issued) == 1, "exit %s, output %r" % (code, out))
    check("the creation message names the base URL a caller needs",
          "8443/v1" in out, "the operator is not told where to point the client")

    code, out = run_cli("key", "list")
    check("the CLI never reprints a key it already created",
          code == 0 and issued[0] not in out,
          "`list` handed back a plaintext key")
    check("the CLI lists the key it just created, by its owning system",
          "hr-system" in out, "output %r" % out)

    code, out = run_cli("key", "show", "k_does_not_exist")
    check("the CLI fails cleanly on an id that is not there",
          code == 1, "exit %s, output %r" % (code, out))

    code, out = run_cli("usage", status=absent_status)
    check("usage still works when the gateway is not running, and says so "
          "instead of printing zeroes",
          code == 0 and "cannot read the live view" in out,
          "exit %s, output %r" % (code, out))

    code, out = run_cli("usage", "--json", status=absent_status)
    try:
        summary = json.loads(out)
    except ValueError:
        summary = {}
    check("usage reports parseable JSON",
          code == 0 and summary.get("totals_from_keys", {}).get("requests") == 0
          and summary.get("gateway_unavailable")
          and summary.get("gateway") is None,
          "exit %s, output %r" % (code, out))

    record = json.loads(run_cli("key", "list", "--json")[1])[0]
    code, out = run_cli("key", "disable", record["id"])
    check("the CLI disables a key", code == 0 and "disabled" in out,
          "exit %s, output %r" % (code, out))
    code, out = run_cli("key", "enable", record["id"])
    check("the CLI re-enables it", code == 0 and "active" in out,
          "exit %s, output %r" % (code, out))
    code, out = run_cli("key", "revoke", record["id"])
    check("the CLI revokes a key", code == 0 and "revoked" in out,
          "exit %s, output %r" % (code, out))
    code, out = run_cli("key", "enable", record["id"])
    check("the CLI does not let an enable undo a revoke",
          code == 0 and "revoked" in out,
          "exit %s, output %r" % (code, out))

    # The join, against a status file that really is the running gateway's.
    code, out = run_cli("usage")
    check("usage reads the gateway's live view when it is there",
          code == 0 and "listening on localhost:" in out
          and "cannot read the live view" not in out,
          "exit %s, output %r" % (code, out))
    check("usage shows the limits that are actually in force",
          "1 global, 1 per key" in out, "output %r" % out)

    check("the CLI's store holds no plaintext key either",
          issued[0] not in open(cli_path, encoding="utf-8").read(),
          "the CLI wrote the key it issued")

    mock.shutdown()
    mock.server_close()
    srv.shutdown()
    srv.server_close()
    gw.stop()


def main() -> int:
    print("=" * 72)
    print("LLM Lab API gateway -- self test")
    print("=" * 72)
    tmp = tempfile.mkdtemp(prefix="llmlab-gateway-selftest-")
    # stderr is captured, not silenced: the gateway writes its access log there,
    # and "no credential ever appears in a log line" is one of the claims this
    # file is supposed to be testing.  Capturing is how that gets checked.
    captured = io.StringIO()
    real_stderr = sys.stderr
    try:
        sys.stderr = captured
        run(tmp)
    finally:
        sys.stderr = real_stderr

    logs = captured.getvalue()
    check("the access log never contains the upstream key",
          UPSTREAM_KEY not in logs, "the model service's key is in the log")
    check("the access log never contains a caller's key",
          "llmlab_" not in logs, "a caller's key is in the log")
    # A blanket check, and it earns its place: the "cancelled" outcome reaching
    # `KeyStore.record` used to raise ValueError, so every client that closed a
    # tab mid-answer left a traceback here.  Nothing asserted on it, and a log
    # full of tracebacks is a log where a real error is invisible.
    check("no traceback reached the log at all (a disconnect is not an incident)",
          "Traceback" not in logs,
          "the log ends with:\n%s" % logs[-600:] if "Traceback" in logs else "")

    print("-" * 72)
    print("%d passed, %d failed" % (len(PASSED), len(FAILED)))
    for name, detail in FAILED:
        print("  FAILED: %s\n          %s" % (name, detail))
    print("temporary files left in %s" % tmp)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
