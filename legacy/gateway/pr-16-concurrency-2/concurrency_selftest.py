#!/usr/bin/env python3
"""Synthetic concurrency acceptance: does the gate hold at one, and at two?

WHY THIS IS A FILE OF ITS OWN.  `selftest.py` accepts the gateway as a product:
the key store, the relay, the privacy rule, the console document.  It walks the
limit path once, at the deployed width of one slot, as one stop on that tour.
This file asks a narrower and more repetitive question -- *what does the gate do
at concurrency 1, and what would it do at 2* -- and asks it of the two shapes
the brief names, so the answer is a measured pair rather than one reading and an
extrapolation.

WHY THE NUMBERS ARE SMALL HERE AND ARE NOT THE DEPLOYED ONES.  Every case below
runs with holds of a few hundred milliseconds and waits of a second or so, so
the whole file finishes in under a minute and can be run on every change.  The
*deployed* values are not tested here; they are asserted, value for value, in
part 0, where `profiles.py` is read directly.  Keeping the two apart is the
point: a test that reproduces 120 s of queueing takes two minutes to say one
thing, and a test that scales the numbers down and then claims to have verified
120 s would be the kind of reading-into-the-result this project keeps warning
about.

WHY THE ENGINE IS MEASURED AND NOT THE GATEWAY'S OWN REPORT.  `gate.stats()`
is the gateway describing itself.  If the admission predicate were wrong, the
same wrong number would appear in both the description and the check.  So the
synthetic engine in this file counts how many requests are *inside it* at once,
per caller, and that count is what the concurrency checks assert on.  The
gateway's own `peak_active` is checked too -- but as a second, weaker witness,
and one of the checks requires it to have actually *reached* the limit, because
a limit that has never been reached is a limit whose value nothing has tested
(limits.py, on `peak_active`).

WHAT "NO GHOST REQUEST" MEANS HERE.  A caller that is refused -- because the
queue is full, or because it waited past its deadline -- must not appear in the
engine's log at all.  A caller that is *accepted* must appear there exactly
once.  Both are counted, because the two failures are different: the first is a
refusal that leaked work into the box anyway, the second is a retry that
multiplied it.

WHAT THIS FILE DOES NOT CLAIM, AND REPORTS INSTEAD.  A caller that hangs up
*while it is still queued* is not noticed by `Gate.acquire`, which is blocked on
a condition variable and not on the client's socket.  When a slot frees, that
departed caller's request is still forwarded to the engine, and only the write
back discovers that nobody is listening.  The slot is not leaked -- the release
is in a `finally` and this file checks it -- but one generation is spent on a
caller who left.  The measurement is printed as `FINDING` rather than asserted,
because asserting today's behaviour would make the check fail the day somebody
fixes it.

Run it from the repository root, or from anywhere:

    python3 -m deploy.dell.gateway.concurrency_selftest
    python3 legacy/gateway/concurrency_selftest.py

Exit status is 0 only when every check passed, exactly like `selftest.py`.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from deploy.dell.gateway import profiles  # noqa: E402
from deploy.dell.gateway.gateway import (  # noqa: E402
    Gateway, Handler, Server, build_parser, parse_args_with_profile,
)
from deploy.dell.gateway.keystore import KeyStore  # noqa: E402
from deploy.dell.gateway.limits import Gate  # noqa: E402
from deploy.dell.gateway.telemetry import Meters, StatusFile, UsageLog  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# The engine's own credential, distinct from every caller key.  Searched for in
# what the engine was shown, so "the gateway substituted its key" is observed
# rather than assumed.
UPSTREAM_KEY = "strata-key-for-the-synthetic-engine"

# What a refused caller is told to wait.  One second, not the deployed five, so
# the retry case is a test rather than a coffee break.
RETRY_AFTER = 1.0

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
FINDINGS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """One assertion, recorded rather than raised -- every finding, one run."""
    if ok:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        "" if ok else "\n          %s" % detail))
    return ok


def finding(text: str) -> None:
    """A measured observation that is not a pass/fail claim.

    Used where the honest statement is "this is what it does today", and where
    turning it into a check would either enshrine a defect or fail on the day
    somebody repairs it.
    """
    FINDINGS.append(text)
    print("FINDING  %s" % text)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll, never sleep a fixed amount: a fixed sleep is flaky or slow, usually
    both."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# The synthetic engine
# ---------------------------------------------------------------------------
class Engine(BaseHTTPRequestHandler):
    """A model service that counts how many requests are inside it at once.

    It answers two questions this file cannot answer from the gateway side:
    *how many* requests were running concurrently at the thing that actually
    does the work, and *which caller* each of them belonged to.  The second is
    why every synthetic request carries a `selftest_label`: the gateway strips
    the caller's credential (that is its job), so the only way to attribute a
    request at the engine is a field in the body, which the gateway forwards
    unchanged.  `selftest_*` fields are test scaffolding and are not a claim
    about what Strata accepts -- the mock validates no schema at all.
    """

    protocol_version = "HTTP/1.0"     # closes the connection; no framing invented
    lock = threading.Lock()
    calls: list[dict] = []
    live = 0
    live_by_label: dict[str, int] = {}
    peak = 0
    peak_by_label: dict[str, int] = {}
    auth_seen: set[str] = set()
    t0 = [0.0]

    _GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    # -- state, reset per case so two cases cannot read each other's history --
    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.calls = []
            cls.live = 0
            cls.live_by_label = {}
            cls.peak = 0
            cls.peak_by_label = {}
            cls.auth_seen = set()
            cls.t0[0] = time.monotonic()

    @classmethod
    def labels_seen(cls) -> list[str]:
        with cls.lock:
            return [c["label"] for c in cls.calls]

    @classmethod
    def label_calls(cls, label: str) -> int:
        return cls.labels_seen().count(label)

    @classmethod
    def overlaps(cls) -> int:
        """How many pairs of engine calls were in flight at the same time.

        The evidence for "two at once" that cannot be produced by a counter
        read at the wrong moment: two intervals that genuinely intersect.
        """
        with cls.lock:
            spans = [(c["enter"], c["exit"]) for c in cls.calls
                     if c.get("exit") is not None]
        n = 0
        for i, (a0, a1) in enumerate(spans):
            for b0, b1 in spans[i + 1:]:
                if a0 < b1 and b0 < a1:
                    n += 1
        return n

    # -- plumbing --------------------------------------------------------
    def log_message(self, *args):     # the gateway's log is under test, not this
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except self._GONE:
            self.close_connection = True

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except self._GONE:
            self.close_connection = True

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _note_auth(self) -> bool:
        shown = self.headers.get("Authorization") or ""
        with self.lock:
            self.auth_seen.add(shown)
        if shown == "Bearer" + " " + UPSTREAM_KEY:
            return True
        self._json(401, {"error": {"message": "invalid api key"}})
        return False

    def do_GET(self):
        # The gateway's own 60 s liveness probe lands here.  It is authorised
        # like anything else and is deliberately not counted as a chat call.
        if not self._note_auth():
            return
        self._json(200, {"object": "list",
                         "data": [{"id": "synthetic-model", "object": "model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not self._note_auth():
            return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        label = str(payload.get("selftest_label") or "?")
        hold = float(payload.get("selftest_hold") or 0.0)
        streaming = bool(payload.get("selftest_stream"))

        entry = {"label": label, "enter": time.monotonic() - self.t0[0],
                 "exit": None}
        with self.lock:
            Engine.calls.append(entry)
            Engine.live += 1
            Engine.peak = max(Engine.peak, Engine.live)
            Engine.live_by_label[label] = Engine.live_by_label.get(label, 0) + 1
            Engine.peak_by_label[label] = max(
                Engine.peak_by_label.get(label, 0), Engine.live_by_label[label])
        try:
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i in range(3):
                    self._write(b'data: {"choices":[{"delta":{"content":"t%d"}}]}\n\n'
                                % i)
                    time.sleep(0.35)
                self._write(b"data: [DONE]\n\n")
            else:
                if hold:
                    time.sleep(hold)
                self._json(200, {"id": "cmpl-synthetic", "object": "chat.completion",
                                 "model": "synthetic-model",
                                 # Echoed so each caller can prove it got *its own*
                                 # answer and not a neighbour's under load.
                                 "echo": label,
                                 "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant",
                                                          "content": "ok"}}],
                                 "usage": {"prompt_tokens": 3,
                                           "completion_tokens": 1,
                                           "total_tokens": 4}})
        finally:
            with self.lock:
                entry["exit"] = time.monotonic() - self.t0[0]
                Engine.live -= 1
                Engine.live_by_label[label] -= 1
                if Engine.live_by_label[label] <= 0:
                    Engine.live_by_label.pop(label, None)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def call(url, key, body, timeout=30.0):
    """Returns (status, headers, parsed-json-or-{}).  Never raises."""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": "Bearer" + " " + key,
                 "Content-Type": "application/json"})
    try:
        resp = OPENER.open(req, timeout=timeout)
        try:
            raw = resp.read()
        finally:
            resp.close()
        try:
            return resp.status, dict(resp.headers), json.loads(raw or b"{}")
        except ValueError:
            return resp.status, dict(resp.headers), {}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        finally:
            exc.close()
        try:
            return exc.code, dict(exc.headers), json.loads(raw or b"{}")
        except ValueError:
            return exc.code, dict(exc.headers), {}


def chat(label, hold=0.0, stream=False) -> bytes:
    return json.dumps({
        "model": "synthetic-model",
        "messages": [{"role": "user", "content": "synthetic prompt"}],
        "selftest_label": label,
        "selftest_hold": hold,
        "selftest_stream": stream,
    }).encode()


class Case:
    """One gateway, one synthetic engine, one fresh key store, one scratch dir."""

    def __init__(self, tmp: str, *, global_limit: int, per_user_limit: int,
                 queue_limit: int, wait_timeout: float, labels):
        self.tmp = tmp
        os.makedirs(tmp, exist_ok=True)
        self.engine = ThreadingHTTPServer(("localhost", 0), Engine)
        threading.Thread(target=self.engine.serve_forever, daemon=True).start()
        Engine.reset()

        self.store = KeyStore(os.path.join(tmp, "keys.json"))
        self.keys = {lab: self.store.issue(lab)[0] for lab in labels}
        self.gate = Gate(global_limit=global_limit, per_user_limit=per_user_limit,
                         queue_limit=queue_limit, wait_timeout_s=wait_timeout,
                         retry_after_s=RETRY_AFTER)
        self.gw = Gateway(
            upstream_url="http://localhost:%d" % self.engine.server_address[1],
            upstream_key=UPSTREAM_KEY, store=self.store, gate=self.gate,
            meters=Meters(),
            usage_log=UsageLog(os.path.join(tmp, "usage.jsonl")),
            status_file=StatusFile(os.path.join(tmp, "status.json")),
            max_body=8 << 20, upstream_timeout=30.0, tick_interval=0.05,
            probe_interval=600.0)
        self.gw.start()

        class H(Handler):
            pass

        self.srv = Server(("localhost", 0), H, self.gw)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://localhost:%d/v1/chat/completions" % self.port

    def go(self, label, hold=0.0, stream=False, timeout=30.0):
        return call(self.url, self.keys[label], chat(label, hold, stream), timeout)

    def send_raw(self, label, hold=0.0):
        """A request whose socket the caller keeps hold of, so it can hang up
        at a chosen moment -- something `urllib` will not let a test do."""
        sock = socket.create_connection(("localhost", self.port), timeout=10)
        body = chat(label, hold)
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                     b"Authorization: Bearer" + b" " + self.keys[label].encode()
                     + b"\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        return sock

    def usage_lines(self) -> list[dict]:
        path = os.path.join(self.tmp, "usage.jsonl")
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def close(self) -> None:
        self.engine.shutdown()
        self.engine.server_close()
        self.srv.shutdown()
        self.srv.server_close()
        self.gw.stop()
        self.store.flush()


def in_thread(fn):
    """Run `fn` on a thread, and hand back a box its return value lands in."""
    box = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as exc:          # noqa: BLE001 - reported, not raised
            box["error"] = repr(exc)

    th = threading.Thread(target=runner)
    th.start()
    return th, box


# ===========================================================================
# Part 0 -- the profiles, as values.  No sockets, no timing, no threads.
# ===========================================================================
def part0_profiles(tmp: str) -> None:
    print("\n-- 0. the named profiles, and the default that must not move ------")

    prod = profiles.settings(profiles.DEFAULT_PROFILE)
    check("the default profile is the deployed configuration, value for value",
          prod == {"global_limit": 1, "per_user_limit": 1, "queue_limit": 8,
                   "wait_timeout": 120.0, "retry_after": 5.0},
          "default profile is %r" % (prod,))
    check("the default profile is named 'production'",
          profiles.DEFAULT_PROFILE == "production",
          "it is %r" % profiles.DEFAULT_PROFILE)

    cand = profiles.settings("concurrency-2-candidate")
    check("the candidate raises the global limit to 2 and nothing else",
          cand == {"global_limit": 2, "per_user_limit": 1, "queue_limit": 8,
                   "wait_timeout": 120.0, "retry_after": 5.0},
          "candidate is %r" % (cand,))
    check("the candidate leaves the per-user limit at 1, so one caller still "
          "cannot hold both slots",
          cand["per_user_limit"] == 1, "per_user_limit is %r" % cand["per_user_limit"])
    check("the candidate keeps a queue and a wait deadline rather than removing "
          "the refusal path",
          cand["queue_limit"] >= 1 and cand["wait_timeout"] > 0,
          "queue_limit %r wait_timeout %r" % (cand["queue_limit"],
                                              cand["wait_timeout"]))

    # The invariant that makes a profile honest: the gateway's global limit is
    # the number that must match the engine's `parallel`.
    for name in profiles.names():
        check("profile %r admits only as many at once as the engine it "
              "declares (%d)" % (name, profiles.engine_parallel_required(name)),
              profiles.settings(name)["global_limit"]
              == profiles.engine_parallel_required(name),
              "global %r vs engine parallel %r"
              % (profiles.settings(name)["global_limit"],
                 profiles.engine_parallel_required(name)))
    check("the candidate names `parallel 2` as its precondition, so applying it "
          "alone is visibly a half of a two-part change",
          profiles.engine_parallel_required("concurrency-2-candidate") == 2,
          "it requires parallel %r"
          % profiles.engine_parallel_required("concurrency-2-candidate"))

    # The table itself, not just this run: a profile that weakens the gate is a
    # startup error, so the check is that such a table is refused.
    bad = {"engine_parallel_required": 2, "global_limit": 4, "per_user_limit": 1,
           "queue_limit": 8, "wait_timeout": 120.0, "retry_after": 5.0}
    saved = profiles.PROFILES.get("__probe__")
    try:
        profiles.PROFILES["__probe__"] = bad
        refused = False
        try:
            profiles._validate()
        except SystemExit:
            refused = True
        check("a profile whose global limit exceeds the engine's parallel is "
              "refused at startup rather than run",
              refused,
              "a global limit above `parallel` moves the queue into the engine, "
              "where queue_limit does not bound it")
    finally:
        if saved is None:
            profiles.PROFILES.pop("__probe__", None)
        else:
            profiles.PROFILES["__probe__"] = saved

    try:
        profiles.settings("no-such-profile")
        unknown_refused = False
    except SystemExit:
        unknown_refused = True
    check("an unknown profile name is refused, not silently defaulted",
          unknown_refused, "an unknown name fell back to something")

    # -- the command line: the default must be reachable by typing nothing.
    bare = build_parser().parse_args([])
    check("the bare parser still defaults to the deployed numbers (so importing "
          "profiles cannot widen the gateway)",
          (bare.global_limit, bare.per_user_limit, bare.queue_limit,
           bare.wait_timeout, bare.retry_after) == (1, 1, 8, 120.0, 5.0),
          "bare defaults are %r"
          % ((bare.global_limit, bare.per_user_limit, bare.queue_limit,
              bare.wait_timeout, bare.retry_after),))

    none = parse_args_with_profile([])
    check("no flags at all is the production profile",
          (none.profile, none.global_limit) == ("production", 1),
          "parsed %r / %r" % (none.profile, none.global_limit))
    named_prod = parse_args_with_profile(["--profile", "production"])
    check("naming the default profile changes nothing",
          (named_prod.profile, named_prod.global_limit,
           named_prod.per_user_limit) == ("production", 1, 1),
          "parsed %r" % ((named_prod.profile, named_prod.global_limit),))
    named_cand = parse_args_with_profile(
        ["--profile", "concurrency-2-candidate"])
    check("the candidate profile is reachable by name and yields 2 global / 1 "
          "per user",
          (named_cand.profile, named_cand.global_limit, named_cand.per_user_limit,
           named_cand.queue_limit, named_cand.wait_timeout)
          == ("concurrency-2-candidate", 2, 1, 8, 120.0),
          "parsed %r" % ((named_cand.profile, named_cand.global_limit,
                          named_cand.per_user_limit, named_cand.queue_limit,
                          named_cand.wait_timeout),))
    overridden = parse_args_with_profile(
        ["--profile", "concurrency-2-candidate", "--global-limit", "3"])
    check("an explicit numeric flag wins over the profile it came from, so a "
          "profile cannot overwrite a number an operator typed",
          overridden.global_limit == 3 and overridden.per_user_limit == 1,
          "parsed global %r per-user %r"
          % (overridden.global_limit, overridden.per_user_limit))

    # And the deployed file itself, which this branch must not have touched.
    site = os.path.join(ROOT, "config", "site", "dell.toml")
    with open(site, encoding="utf-8") as fh:
        text = fh.read()
    block = text.split("[services.api-gateway]", 1)[1].split("\n[tunnels", 1)[0]
    check("the deployed launch line still says global 1 / per-user 1, so this "
          "branch has not widened production",
          '"--global-limit", "1"' in block and '"--per-user-limit", "1"' in block,
          "config/dell-site.example.toml no longer pins the deployed limits to 1")
    check("the deployed launch line does not select the candidate profile",
          '--profile' not in block and 'concurrency-2-candidate' not in block,
          "the site config now names a profile; that is a production change")


# ===========================================================================
# Part 1 -- concurrency 1, the deployed width
# ===========================================================================
def part1_one_slot(tmp: str) -> None:
    print("\n-- 1. concurrency 1: one slot, a real queue, hard refusals --------")
    case = Case(os.path.join(tmp, "one"), global_limit=1, per_user_limit=1,
                queue_limit=2, wait_timeout=1.2,
                labels=("alice", "bob", "carol", "dave"))
    try:
        st, _, body = case.go("alice")
        check("a business request is served at one slot",
              st == 200 and body.get("echo") == "alice",
              "status %r body %r" % (st, body))
        check("the engine saw the request the gateway said it forwarded",
              Engine.label_calls("alice") == 1,
              "engine log %r" % (Engine.labels_seen(),))
        check("the engine never saw a caller's key",
              not any("llmlab_" in a for a in Engine.auth_seen),
              "an authentication header carried a caller key: %r" % Engine.auth_seen)

        # -- saturation, then a second caller that must wait ------------------
        Engine.reset()
        th_hold, box_hold = in_thread(lambda: case.go("alice", hold=0.6))
        check("CONTROL: the first caller really occupies the single slot",
              wait_for(lambda: case.gate.stats()["active"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())

        th_wait, box_wait = in_thread(lambda: case.go("bob", hold=0.0))
        check("a second caller waits rather than being run at the same time",
              wait_for(lambda: case.gate.stats()["waiting"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())
        check("nothing the gateway is holding has reached the engine yet, so the "
              "wait is real and not a schedule artefact",
              Engine.labels_seen() == ["alice"],
              "engine log while queued: %r" % Engine.labels_seen())

        th_hold.join(timeout=20)
        th_wait.join(timeout=20)
        st_h, _, body_h = box_hold.get("value", (None, None, {}))
        st_w, _, body_w = box_wait.get("value", (None, None, {}))
        check("the caller that held the slot is answered",
              st_h == 200 and body_h.get("echo") == "alice",
              "holder status %r body %r" % (st_h, body_h))
        check("the caller that queued is answered too -- waiting is not failing",
              st_w == 200 and body_w.get("echo") == "bob",
              "waiter status %r body %r" % (st_w, body_w))
        check("the queued request reached the engine exactly once",
              Engine.label_calls("bob") == 1,
              "engine log %r" % (Engine.labels_seen(),))
        check("the engine never ran two requests at once, because there is one "
              "slot", Engine.peak == 1, "engine peak was %r" % Engine.peak)
        check("the gate reports that it actually reached its limit (a limit "
              "nothing has reached is a limit nothing has tested)",
              case.gate.stats()["peak_active"] == 1,
              "gate: %r" % case.gate.stats())

        # -- a full queue refuses at once, and forwards nothing ---------------
        Engine.reset()
        th1, _ = in_thread(lambda: case.go("alice", hold=0.6))
        check("CONTROL: slot occupied again", wait_for(
            lambda: case.gate.stats()["active"] == 1, timeout=3), "gate: %r"
            % case.gate.stats())
        th2, _ = in_thread(lambda: case.go("bob", hold=0.6))
        check("CONTROL: the queue is now full (limit 2: bob plus one more)",
              wait_for(lambda: case.gate.stats()["waiting"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())
        th3, _ = in_thread(lambda: case.go("carol", hold=0.6))
        check("CONTROL: the queue filled", wait_for(
            lambda: case.gate.stats()["waiting"] == 2, timeout=3), "gate: %r"
            % case.gate.stats())

        t0 = time.monotonic()
        st_f, hdrs_f, body_f = case.go("dave", hold=0.6)
        elapsed = time.monotonic() - t0
        check("a caller arriving at a full queue is refused at once rather than "
              "held", st_f == 429 and elapsed < 0.5,
              "status %r after %.2fs" % (st_f, elapsed))
        check("the refusal tells the caller when to come back",
              hdrs_f.get("Retry-After") is not None,
              "no Retry-After header: the client can only guess")
        check("the refusal is in the shape an OpenAI client parses",
              body_f.get("error", {}).get("code") == "rate_limited",
              "body %r" % (body_f,))
        check("NO GHOST: a caller refused for a full queue never reached the "
              "engine", Engine.label_calls("dave") == 0,
              "engine log %r" % (Engine.labels_seen(),))
        check("the 429 records the caller's usage rather than dropping it",
              any(l.get("status") == 429 and l.get("key_id")
                  for l in case.usage_lines()),
              "no 429 line in the usage log")
        for th in (th1, th2, th3):
            th.join(timeout=20)

        # -- a caller that waits past its deadline is refused, and forwards
        #    nothing ---------------------------------------------------------
        #
        # Deterministic on purpose: the holder runs for 2.5 s and the deadline
        # is 1.2 s, so the victim's deadline provably expires *while a slot is
        # held* and cannot be overtaken into a 200 by a race with the queue.
        Engine.reset()
        th_a, _ = in_thread(lambda: case.go("alice", hold=2.5))
        check("CONTROL: the single slot is held for longer than the deadline",
              wait_for(lambda: case.gate.stats()["active"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())
        check("CONTROL: the queue has room, so the refusal under test is the "
              "deadline and not the queue bound",
              case.gate.stats()["waiting"] < case.gate.queue_limit,
              "gate: %r" % case.gate.stats())
        t0 = time.monotonic()
        st_t, _, body_t = case.go("carol", hold=0.1)
        waited = time.monotonic() - t0
        check("a caller that outlives the wait deadline is refused",
              st_t == 429, "status %r after %.2fs" % (st_t, waited))
        check("it was held for about the deadline before the refusal, not "
              "refused instantly and not held forever",
              1.0 < waited < 2.4, "it waited %.2fs" % waited)
        check("the message says it was the wait and not the queue",
              "waited" in body_t.get("error", {}).get("message", ""),
              "message %r" % body_t.get("error", {}).get("message"))
        check("NO GHOST: a caller that timed out in the queue never reached the "
              "engine", "carol" not in Engine.labels_seen(),
              "engine log %r" % Engine.labels_seen())
        th_a.join(timeout=20)

        check("CONTROL: no slot and no queue entry survived the refusals",
              wait_for(lambda: case.gate.stats()["active"] == 0
                       and case.gate.stats()["waiting"] == 0, timeout=5),
              "gate: %r -- something leaked" % case.gate.stats())
    finally:
        case.close()


# ===========================================================================
# Part 2 -- concurrency 2, the candidate width
# ===========================================================================
def part2_two_slots(tmp: str) -> None:
    print("\n-- 2. concurrency 2: two at the engine, still one per caller -------")
    case = Case(os.path.join(tmp, "two"), global_limit=2, per_user_limit=1,
                queue_limit=4, wait_timeout=3.0,
                labels=("alice", "bob", "carol", "dave"))
    try:
        st, _, body = case.go("alice")
        check("a business request is served at two slots",
              st == 200 and body.get("echo") == "alice",
              "status %r body %r" % (st, body))

        # -- two different callers, genuinely at once ------------------------
        Engine.reset()
        th_a, box_a = in_thread(lambda: case.go("alice", hold=0.8))
        th_b, box_b = in_thread(lambda: case.go("bob", hold=0.8))
        saturated = wait_for(lambda: Engine.peak == 2, timeout=4)
        check("CONTROL: two requests are inside the engine at the same time "
              "(measured at the engine, not read off the gateway)",
              saturated, "engine peak was %r; gate %r"
              % (Engine.peak, case.gate.stats()))
        th_a.join(timeout=20)
        th_b.join(timeout=20)
        # Read the overlap only once both have finished: an interval has no end
        # until the call returns, and a check made while they are in flight
        # would be comparing a span against "not yet over".
        check("their intervals genuinely overlap, so the peak was not two "
              "requests that merely followed one another",
              Engine.overlaps() >= 1,
              "engine call spans do not intersect: %r" % Engine.calls)
        st_a = box_a.get("value", (None,))[0]
        st_b = box_b.get("value", (None,))[0]
        check("both callers are answered", st_a == 200 and st_b == 200,
              "alice %r bob %r" % (st_a, st_b))
        check("each caller got its own answer and not a neighbour's",
              box_a.get("value", (None, None, {}))[2].get("echo") == "alice"
              and box_b.get("value", (None, None, {}))[2].get("echo") == "bob",
              "alice got %r, bob got %r"
              % (box_a.get("value", (None, None, {}))[2].get("echo"),
                 box_b.get("value", (None, None, {}))[2].get("echo")))
        check("the gate reports that it reached two",
              case.gate.stats()["peak_active"] == 2,
              "gate: %r" % case.gate.stats())
        check("the engine never saw more than the two slots the profile allows",
              Engine.peak <= 2, "engine peak %r" % Engine.peak)

        # -- one caller may still not hold both slots ------------------------
        Engine.reset()
        th_a1, box_a1 = in_thread(lambda: case.go("alice", hold=0.7))
        th_a2, box_a2 = in_thread(lambda: case.go("alice", hold=0.7))
        th_b1, box_b1 = in_thread(lambda: case.go("bob", hold=0.7))
        saw_two_alice = wait_for(
            lambda: Engine.peak_by_label.get("alice", 0) >= 2, timeout=1.2)
        check("one caller's second request never runs at the engine at the same "
              "time as their first, even with two slots free",
              not saw_two_alice,
              "the engine ran two 'alice' requests at once: %r"
              % Engine.peak_by_label)
        for th in (th_a1, th_a2, th_b1):
            th.join(timeout=20)
        check("alice's queued second request is still served once a slot frees",
              box_a2.get("value", (None,))[0] == 200,
              "alice's second call returned %r" % (box_a2.get("value", (None,))[0],))
        check("alice's second request reached the engine exactly once",
              Engine.label_calls("alice") == 2,
              "engine log %r" % Engine.labels_seen())

        # -- fairness: several callers, two labels' worth of pressure --------
        Engine.reset()
        labels = ["alice", "bob", "carol", "dave", "alice", "bob"]
        threads, boxes = [], []
        for i, lab in enumerate(labels):
            th, box = in_thread(lambda l=lab, n=i: case.go(l, hold=0.25))
            threads.append(th)
            boxes.append(box)
        for th in threads:
            th.join(timeout=30)
        statuses = [b.get("value", (None, None, {}))[0] for b in boxes]
        echoes = [b.get("value", (None, None, {}))[2].get("echo") for b in boxes]
        check("every caller in a burst of six is served, so no caller is locked "
              "out by the others", statuses == [200] * 6,
              "statuses %r" % statuses)
        check("no caller received another caller's answer under load",
              echoes == labels,
              "asked %r, got %r" % (labels, echoes))
        check("the engine still never exceeded two at once during the burst",
              Engine.peak <= 2, "engine peak %r" % Engine.peak)
        check("no caller held two engine slots at once during the burst",
              max([Engine.peak_by_label.get(l, 0) for l in set(labels)] or [0]) <= 1,
              "per-caller engine peak %r" % Engine.peak_by_label)
        check("the engine saw every request exactly once, so nothing was "
              "duplicated under concurrency",
              sorted(Engine.labels_seen()) == sorted(labels),
              "engine log %r" % Engine.labels_seen())
        check("CONTROL: nothing leaked after the burst",
              wait_for(lambda: case.gate.stats()["active"] == 0
                       and case.gate.stats()["waiting"] == 0, timeout=5),
              "gate: %r" % case.gate.stats())

        order = Engine.labels_seen()
        finding("queue service order at two slots (first eight): %r -- the gate "
                "wakes every waiter with notify_all and lets them race, so this "
                "is not FIFO and a long waiter can be overtaken. Measured, not "
                "asserted: an ordering claim here would be flaky." % (order[:8],))
    finally:
        case.close()


# ===========================================================================
# Part 3 -- a caller who leaves must not keep the box
# ===========================================================================
def part3_interruption(tmp: str) -> None:
    print("\n-- 3. a caller who leaves: the slot comes back ---------------------")
    case = Case(os.path.join(tmp, "gone"), global_limit=2, per_user_limit=1,
                queue_limit=4, wait_timeout=3.0,
                labels=("alice", "bob", "carol", "dave"))
    try:
        Engine.reset()
        th_a, _ = in_thread(lambda: case.go("alice", hold=0.9))
        th_b, _ = in_thread(lambda: case.go("bob", hold=0.9))
        check("CONTROL: both slots are occupied",
              wait_for(lambda: case.gate.stats()["active"] == 2, timeout=3),
              "gate: %r" % case.gate.stats())
        th_c, _ = in_thread(lambda: case.go("carol", hold=0.1))
        check("CONTROL: a third caller is queued",
              wait_for(lambda: case.gate.stats()["waiting"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())

        # carol's socket: opened by hand, so the test can hang up mid-answer
        sock = case.send_raw("dave", hold=0.0)
        check("CONTROL: the hand-built caller is queued too",
              wait_for(lambda: case.gate.stats()["waiting"] == 2, timeout=3),
              "gate: %r" % case.gate.stats())
        # let it through to the engine, then leave while the answer is arriving
        th_a.join(timeout=20)
        first = sock.recv(4096)
        check("CONTROL: the hand-built caller's answer has started arriving",
              bool(first), "nothing came back to read")
        sock.close()
        check("the slot comes back when the client hangs up mid-answer",
              wait_for(lambda: case.gate.stats()["active"] <= 1, timeout=6),
              "gate: %r -- a slot leaked on disconnect" % case.gate.stats())
        st, _, _ = case.go("alice", hold=0.0)
        check("the next caller is served normally after that disconnect",
              st == 200, "status %r" % st)
        th_b.join(timeout=20)
        th_c.join(timeout=20)

        # -- the window this file must report rather than assert ------------
        check("CONTROL: everything is settled before the queued-disconnect case",
              wait_for(lambda: case.gate.stats()["active"] == 0
                       and case.gate.stats()["waiting"] == 0, timeout=5),
              "gate: %r" % case.gate.stats())
        Engine.reset()
        th_h1, _ = in_thread(lambda: case.go("alice", hold=0.9))
        th_h2, _ = in_thread(lambda: case.go("bob", hold=0.9))
        check("CONTROL: two slots held for the queued-disconnect case",
              wait_for(lambda: case.gate.stats()["active"] == 2, timeout=3),
              "gate: %r" % case.gate.stats())
        victim = case.send_raw("carol", hold=0.05)
        queued = wait_for(lambda: case.gate.stats()["waiting"] == 1, timeout=3)
        check("CONTROL: the victim is queued, not admitted",
              queued, "gate: %r" % case.gate.stats())
        victim.close()                    # gone while it is still waiting
        th_h1.join(timeout=20)
        th_h2.join(timeout=20)
        check("a caller who hangs up while queued still gives the slot back",
              wait_for(lambda: case.gate.stats()["active"] == 0
                       and case.gate.stats()["waiting"] == 0, timeout=6),
              "gate: %r -- a slot leaked on a queued disconnect"
              % case.gate.stats())
        check("the departed caller's request was not run twice, so nothing "
              "multiplied at the engine",
              Engine.label_calls("carol") <= 1,
              "engine saw %d carol requests" % Engine.label_calls("carol"))
        finding("a caller that hangs up while QUEUED: the engine saw %d request(s) "
                "for the departed caller. Gate.acquire blocks on a condition "
                "variable and not on the client socket, so the request is "
                "forwarded anyway and only the write back discovers the caller "
                "has gone. The slot is returned (checked above); one generation "
                "is spent on a caller who left. At the candidate's queue depth "
                "and 120 s deadline this is the largest source of wasted engine "
                "work." % Engine.label_calls("carol"))
        check("CONTROL: nothing leaked at the end of the interruption cases",
              wait_for(lambda: case.gate.stats()["active"] == 0
                       and case.gate.stats()["waiting"] == 0, timeout=5),
              "gate: %r" % case.gate.stats())
    finally:
        case.close()


# ===========================================================================
# Part 4 -- refusal, then a retry: one accepted request, one engine call
# ===========================================================================
def part4_retry(tmp: str) -> None:
    print("\n-- 4. retry after a refusal: no phantom, no duplicate -------------")
    case = Case(os.path.join(tmp, "retry"), global_limit=1, per_user_limit=1,
                queue_limit=1, wait_timeout=2.0,
                labels=("alice", "bob", "carol"))
    try:
        Engine.reset()
        th_h, _ = in_thread(lambda: case.go("alice", hold=0.8))
        check("CONTROL: the single slot is held",
              wait_for(lambda: case.gate.stats()["active"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())
        th_q, _ = in_thread(lambda: case.go("bob", hold=0.0))
        check("CONTROL: the single queue slot is taken",
              wait_for(lambda: case.gate.stats()["waiting"] == 1, timeout=3),
              "gate: %r" % case.gate.stats())

        st, hdrs, body = case.go("carol", hold=0.0)
        check("the retrying caller is refused first",
              st == 429, "status %r" % st)
        retry_after = hdrs.get("Retry-After")
        check("the Retry-After the caller is told is a usable number of seconds",
              retry_after is not None and float(retry_after) >= 1,
              "Retry-After was %r" % retry_after)
        check("NO GHOST: the refused attempt never reached the engine",
              Engine.label_calls("carol") == 0,
              "engine log after the refusal: %r" % (Engine.labels_seen(),))
        check("the refused attempt is recorded as rate-limited and not as a "
              "request that worked",
              any(l.get("outcome") == "rate_limited"
                  and l.get("error", "").startswith("limit_")
                  for l in case.usage_lines()),
              "usage lines: %r" % (case.usage_lines(),))

        th_h.join(timeout=20)
        th_q.join(timeout=20)
        time.sleep(float(retry_after))            # obey the instruction we were given
        st2, _, body2 = case.go("carol", hold=0.0)
        check("the retry, made after the interval it was told to wait, is served",
              st2 == 200 and body2.get("echo") == "carol",
              "retry status %r body %r" % (st2, body2))
        check("the accepted request reached the engine exactly once -- the "
              "refused attempt left nothing behind and the retry made nothing "
              "extra", Engine.label_calls("carol") == 1,
              "engine saw %d carol requests for one accepted call"
              % Engine.label_calls("carol"))
        check("the gate is empty again after the retry",
              wait_for(lambda: case.gate.stats()["active"] == 0
                       and case.gate.stats()["waiting"] == 0, timeout=5),
              "gate: %r" % case.gate.stats())
    finally:
        case.close()


# ===========================================================================
# Part 5 -- what the record says about queue and service time
# ===========================================================================
def part5_reporting(tmp: str) -> None:
    print("\n-- 5. the numbers: queue, service, total, and the limit scope ------")
    case = Case(os.path.join(tmp, "report"), global_limit=2, per_user_limit=1,
                queue_limit=1, wait_timeout=1.0,
                labels=("alice", "bob", "carol", "dave"))
    try:
        Engine.reset()
        # one straight-through request, one that has to queue, one refusal
        case.go("alice", hold=0.0)
        th_a, _ = in_thread(lambda: case.go("alice", hold=0.7))
        th_b, _ = in_thread(lambda: case.go("bob", hold=0.7))
        check("CONTROL: both slots held for the reporting case",
              wait_for(lambda: case.gate.stats()["active"] == 2, timeout=3),
              "gate: %r" % case.gate.stats())
        th_c, _ = in_thread(lambda: case.go("carol", hold=0.2))     # queues
        wait_for(lambda: case.gate.stats()["waiting"] == 1, timeout=3)
        st_r, _, _ = case.go("dave", hold=0.2)                      # queue full -> 429
        for th in (th_a, th_b, th_c):
            th.join(timeout=20)
        time.sleep(0.2)

        lines = case.usage_lines()
        check("CONTROL: the usage log has a line per request", len(lines) >= 4,
              "only %d lines" % len(lines))
        forwarded = [l for l in lines if l.get("outcome") == "ok"]
        refused = [l for l in lines if l.get("status") == 429]
        check("CONTROL: the log carries both a forwarded line and a 429",
              forwarded and refused and st_r == 429,
              "forwarded %d refused %d" % (len(forwarded), len(refused)))

        check("a forwarded line reports queue, time-to-first-byte, upstream and "
              "total as four separate numbers",
              all(k in forwarded[0] for k in ("queue_ms", "ttft_ms", "upstream_ms",
                                              "total_ms")),
              "fields present: %r" % sorted(forwarded[0]))
        straight = [l for l in forwarded if l.get("label") == "alice"
                    and l.get("queue_ms", 0) == 0]
        check("the request that did not wait records no queue time",
              straight and all(l["queue_ms"] < 5 for l in straight),
              "queue_ms values %r" % [l.get("queue_ms") for l in forwarded])
        queued = [l for l in forwarded if l.get("label") == "carol"]
        check("the request that did wait records its queue time, so the wait is "
              "visible rather than folded into the service time",
              queued and queued[0]["queue_ms"] > 200,
              "carol's queue_ms was %r"
              % (queued[0].get("queue_ms") if queued else None))

        # The semantics, pinned: `total_ms` is service time.  Queue time is a
        # separate field and is NOT included in it.
        worst = 0.0
        for l in forwarded:
            worst = max(worst, abs(l["total_ms"]
                                   - (l["ttft_ms"] + l["upstream_ms"])))
        check("on a forwarded line total_ms is the service time -- "
              "time-to-first-byte plus the upstream exchange -- and the queue "
              "wait is deliberately not part of it",
              worst <= 0.5, "total_ms differs from ttft+upstream by %.1f ms" % worst)
        for l in refused:
            check("on a 429 line total_ms is the time spent queueing, which is "
                  "the opposite meaning to the same field on a forwarded line",
                  l["ttft_ms"] == 0 and l["upstream_ms"] == 0
                  and abs(l["total_ms"] - l["queue_ms"]) <= 0.2,
                  "429 line: %r" % l)
        finding("the usage log has no end-to-end field: `total_ms` means "
                "service time on a forwarded line and queue time on a 429 line, "
                "so a reader adding total_ms across both is adding two different "
                "quantities. Nothing is wrong with either number; the column is "
                "just not one column. An end-to-end value (queue + service) "
                "would be an additive field, and is left as a recommendation "
                "rather than made here.")

        scope_codes = {l.get("error") for l in refused}
        check("a 429 names the limit it hit in the record rather than only in the "
              "message", all(str(c).startswith("limit_") for c in scope_codes),
              "429 error fields: %r" % scope_codes)
        finding("the limit scopes the gate can report today are %r -- 'queue' and "
                "'timeout' (limits.py: TooMany). There is no per-user scope, so a "
                "caller refused because they already hold their own slot is "
                "reported as having waited: at the candidate width, with the "
                "per-user limit at 1 and a 120 s deadline, a user's second call "
                "waits two minutes and is then told 'none came free' when the "
                "true reason is 'you already have one running'."
                % sorted(scope_codes))

        doc = case.gw.status_document()
        check("the status document names the profile the process is running",
              "profile" in doc, "the document has %r" % sorted(doc))
        check("that profile is 'custom' here, because this gateway was built by "
              "a test and did not choose one",
              doc["profile"] == "custom", "profile is %r" % doc.get("profile"))
        check("the status document still carries the limits themselves",
              doc.get("limits", {}).get("global_limit") == 2,
              "limits %r" % doc.get("limits"))
    finally:
        case.close()


def main() -> int:
    print("=" * 72)
    print("LLM Lab API gateway -- synthetic concurrency acceptance")
    print("=" * 72)
    tmp = tempfile.mkdtemp(prefix="llmlab-concurrency-selftest-")
    started = time.monotonic()
    captured = io.StringIO()
    real_stderr = sys.stderr
    try:
        sys.stderr = captured
        part0_profiles(tmp)
        part1_one_slot(tmp)
        part2_two_slots(tmp)
        part3_interruption(tmp)
        part4_retry(tmp)
        part5_reporting(tmp)
    finally:
        sys.stderr = real_stderr

    logs = captured.getvalue()
    check("the access log never contains the engine's key",
          UPSTREAM_KEY not in logs, "the engine's key is in the log")
    check("the access log never contains a caller's key",
          "llmlab_" not in logs, "a caller's key is in the log")
    check("no traceback reached the log (a disconnect is not an incident)",
          "Traceback" not in logs,
          "the log ends with:\n%s" % logs[-600:] if "Traceback" in logs else "")

    print("-" * 72)
    print("%d passed, %d failed, %d findings  (%.1fs)"
          % (len(PASSED), len(FAILED), len(FINDINGS), time.monotonic() - started))
    for name, detail in FAILED:
        print("  FAILED: %s\n          %s" % (name, detail))
    print("temporary files left in %s" % tmp)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
