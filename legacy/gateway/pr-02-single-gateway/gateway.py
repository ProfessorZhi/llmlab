"""The gateway's HTTP surface: a plain OpenAI-compatible door to Strata.

WHAT IT DOES, IN ONE LINE.  It authenticates a per-system Bearer key, decides
whether the box can take the request, forwards it to Strata with Strata's own
key attached, and relays the answer back byte for byte.

FIVE RULES THAT ARE THE WHOLE POINT.

  1. **Default deny, and the target is not the client's to choose.**  A path is
     forwarded only if it is on the allowlist below, and what is forwarded is
     the *parsed* path and query -- never the raw request line.  That second
     half matters: an HTTP client may send an absolute-form request target
     (`GET http://something-else/v1/models HTTP/1.1`), and a proxy that passes
     `self.path` along would hand that authority to the upstream.  The upstream
     address is a constant from the command line; a caller cannot influence it.

  2. **The real key never leaves.**  The upstream key is read once from a file
     at startup and attached on the way out.  It is not in this process's argv,
     it is not in a response, and it is not in a log line.

  3. **Client-supplied identity headers are destroyed, and the caller's own
     credential never crosses.**  `Authorization` and `x-api-key` are replaced;
     the `X-Forwarded-*` / `*-User` family is dropped rather than relayed,
     because a model service that learns to trust one of them would be
     trusting whatever the last caller typed.

  4. **Failures are relayed, not manufactured.**  A 4xx or 5xx from Strata is
     passed back with its own status and its own body.  An unreachable upstream
     is a 502 with the reason; a request that outlived its budget is a 504.
     Nothing here answers 200 with an invented body.

  5. **Bodies are never recorded.**  Latency, status, size and the upstream's
     own token counts are; prompts and completions are not.  See `telemetry.py`
     for why that is a requirement here and not a preference.

WHY THE RESPONSE IS RELAYED RATHER THAN BUFFERED.  `stream: true` is half of
what an OpenAI-compatible client does, and a proxy that reads the whole body
before writing any of it turns a stream into a long silence followed by a burst
-- which is indistinguishable, at the client, from the model hanging.

WHAT "CANCEL" DOES AND DOES NOT DO.  When a client hangs up mid-stream this
gateway stops reading, closes the upstream connection and releases its slot, so
the box is not held by a caller who has left.  It cannot stop Strata: the engine
has already begun generating and will run that request to completion.  Promising
otherwise would be a claim about code this deployment does not own.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .keystore import KeyStore, looks_like_one_of_ours
from .limits import Gate, TooMany
from .telemetry import Meters, StatusFile, UsageLog

# The compatibility surface, stated as data so that "what does this publish?"
# is answered by reading one table rather than by reading the dispatcher.
#
# `/v1/messages` -- Strata's native Anthropic-protocol endpoint -- is NOT here
# yet.  The brief allows it as a later extension and requires verifying the
# native protocol and response semantics first; publishing it now, before
# anything has tested its streaming shape, would be exactly the unverified
# claim the same brief warns about.
ALLOWLIST = {
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
}

# Hop-by-hop headers (RFC 7230 §6.1).  Forwarding these is how a proxy ends up
# negotiating a connection upgrade on behalf of a client it never spoke to.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}

# Headers that name a user, dropped on the way *upstream*.  Caddy already
# removes the ones coming in from the LAN, and this repeats it here on purpose:
# the two are separate hops, and the day somebody points a different front end
# at this port, the deletion should not depend on that front end being the
# current one.  Redundant defences on an identity boundary are cheap; the
# missing one is not.
IDENTITY_HEADERS = {
    "x-forwarded-user", "x-forwarded-email", "x-forwarded-groups",
    "x-remote-user", "remote-user", "x-auth-request-user",
    "x-auth-request-email", "x-auth-request-groups", "forwarded",
    "x-forwarded-host", "x-forwarded-for", "x-real-ip",
}

# Errors that mean "the client went away", as opposed to "this gateway broke".
# ConnectionAbortedError is WinError 10053 and is a *peer* of BrokenPipeError,
# not a subclass: a handler that catches only BrokenPipeError logs a traceback
# for every disconnect on Windows, which buries the real errors.
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Reading a non-streaming JSON body to pick the token counts out of it costs one
# parse, and there is a size past which that is the wrong trade -- so past this
# the body is relayed untouched and its usage is recorded as absent rather than
# guessed.
PARSE_LIMIT = 1024 * 1024


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def read_key_file(path: str, var: str) -> str:
    """One ``KEY=value`` out of a secrets file, mirroring
    `integrations/strata.py::read_secret` so the two agree on the format.

    Kept as a copy rather than an import: this module is deployed to the node
    as part of the gateway, and a cross-package import would make the gateway
    depend on the checkout's layout being the one it was written against.
    """
    if not var or not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == var:
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def parse_aliases(pairs) -> dict:
    """`NAME=REAL` pairs into a mapping, refusing anything ambiguous.

    The brief allows either the real model id or a stable alias, on one
    condition: that the alias is *explicitly* mapped to a real model.  A
    mapping that silently pointed at nothing would satisfy "we exposed a stable
    name" while making every call fail, so a malformed pair is a startup error
    rather than something to skip.
    """
    out: dict[str, str] = {}
    for item in pairs or []:
        name, sep, real = str(item).partition("=")
        name, real = name.strip(), real.strip()
        if not sep or not name or not real:
            raise SystemExit("--alias wants NAME=REAL, got %r" % (item,))
        if name in out and out[name] != real:
            raise SystemExit("--alias %s is mapped twice, to %r and to %r"
                             % (name, out[name], real))
        out[name] = real
    return out


class Gateway:
    """State shared by every request handler, and one background ticker."""

    def __init__(self, *, upstream_url: str, upstream_key: str, store: KeyStore,
                 gate: Gate, meters: Meters, usage_log: UsageLog | None = None,
                 status_file: StatusFile | None = None, aliases: dict | None = None,
                 max_body: int, upstream_timeout: float, listen: str = "",
                 tick_interval: float = 5.0, probe_interval: float = 60.0):
        self.upstream_url = upstream_url
        self.listen = listen
        self.upstream_key = upstream_key
        self.store = store
        self.gate = gate
        self.meters = meters
        self.usage_log = usage_log
        self.status_file = status_file
        self.aliases = dict(aliases or {})
        self.max_body = max_body
        self.upstream_timeout = upstream_timeout
        self.tick_interval = tick_interval
        self.probe_interval = probe_interval

        split = urlsplit(upstream_url)
        self.upstream_host = split.hostname or "localhost"
        self.upstream_port = split.port or (443 if split.scheme == "https" else 80)

        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True,
                                        name="gateway-ticker")

    # -- background work -------------------------------------------------
    def start(self) -> None:
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()

    def _tick_loop(self) -> None:
        """Persist counters, publish status, and check the upstream.

        One thread for all three because they are all "the slow clock": none of
        them belongs in the request path, and three threads doing three
        occasional things is three names to read in a stack dump for no gain.
        """
        next_probe = 0.0
        while not self._stop.wait(self.tick_interval):
            try:
                self.store.flush()
            except Exception as exc:  # noqa: BLE001
                # A flusher that dies silently is a store that stops recording
                # usage, which looks exactly like "nobody is calling the model".
                print("[gateway] key store flush failed: %r" % (exc,), flush=True)
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + self.probe_interval
                self.probe_upstream()
            if self.status_file is not None:
                self.status_file.write(self.status_document())

    def probe_upstream(self) -> None:
        """One tiny read against the model service: is it there, and what does
        it say it serves?

        `GET /v1/models` does not touch the GPU and does not load a model, so
        this is a monitor rather than a load.  It is also the only honest way
        to fill the console's "models" field: the alternative is to hardcode a
        model list in a config file and describe a wish as a fact.
        """
        conn = None
        try:
            conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port,
                                              timeout=10.0)
            conn.request("GET", "/v1/models",
                         headers={"Authorization": "Bearer" + " " + self.upstream_key})
            resp = conn.getresponse()
            body = resp.read(PARSE_LIMIT + 1)
            models = None
            if resp.status == 200 and len(body) <= PARSE_LIMIT:
                try:
                    data = json.loads(body).get("data")
                    if isinstance(data, list):
                        models = [m.get("id") for m in data if isinstance(m, dict)]
                except (ValueError, AttributeError):
                    models = None
            self.meters.note_upstream(ok=200 <= resp.status < 300, status=resp.status,
                                      models=models)
        except (socket.timeout, TimeoutError) as exc:
            self.meters.note_upstream(ok=False, status=None, error="timeout: %s" % exc)
        except OSError as exc:
            self.meters.note_upstream(ok=False, status=None, error=str(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def status_document(self) -> dict:
        """Everything the console needs, in one file, with no secrets in it.

        Assembled from the key store's *public* view (which strips digests), so
        the portal never has to know the store's format or its file mode.
        """
        snapshot = self.meters.snapshot()
        upstream = snapshot.pop("upstream")
        models = snapshot.pop("models")
        try:
            keys = self.store.list_records()
        except Exception as exc:  # noqa: BLE001
            keys = []
            snapshot.setdefault("errors", {})["keys_unreadable"] = str(exc)
        return {
            "service": "llmlab-gateway",
            "version": 1,
            "pid": os.getpid(),
            "started_at": self.meters.started_at,
            "uptime_s": self.meters.uptime_s(),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "listen": self.listen,
            "upstream_url": self.upstream_url,
            "upstream": upstream,
            "models": models,
            "aliases": dict(self.aliases),
            "limits": self.gate.stats(),
            "totals": snapshot,
            "keys": keys,
        }

    # -- small shared helpers --------------------------------------------
    def log_usage(self, record: dict) -> None:
        if self.usage_log is not None:
            self.usage_log.write(record)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "llmlab-gateway/1"
    sys_version = ""

    gateway: Gateway  # set by the server subclass

    # -- logging ---------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003
        """One line per request, to stderr, without the credential.

        The point of overriding this is *what* is in the line: the result and
        the route, never the key.  Nothing in this class ever formats the
        credential, and the self test greps this stream to keep it that way.
        """
        sys.stderr.write("[gateway] %s %s\n" % (self.log_date_time_string(),
                                                fmt % args))

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)

    def handle_one_request(self):
        """Swallow the disconnect class of error before it becomes a traceback.

        A client that hangs up is normal traffic, and a traceback per hang-up
        is how a real error gets lost in the noise.
        """
        try:
            super().handle_one_request()
        except CLIENT_GONE:
            self.close_connection = True

    # -- responses -------------------------------------------------------
    def _send_json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(body)
        except CLIENT_GONE:
            pass

    def _fail(self, status: int, message: str, code: str, extra: dict | None = None,
              kind: str = "") -> None:
        """An error in the shape an OpenAI-compatible client parses.

        The shape matters: a client that gets a bare string where it expected
        `error.message` reports its own parse failure, which sends whoever
        reads the report looking in the client.
        """
        self.gateway.meters.count_rejected(kind or code)
        self._send_json(status, {"error": {"message": message,
                                           "type": "invalid_request_error",
                                           "code": code}}, extra)

    def _presented_key(self) -> str:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _read_body(self, method: str) -> bytes | None:
        """The request body, or None after answering 411/413.

        Content-Length is required *where a body is meaningful*, rather than
        guessed: a body read by "read until the connection closes" is a body
        that never ends, and would let an unauthenticated caller pin a thread
        forever.  Where a body is NOT meaningful, requiring the header is a bug
        with a long reach -- `GET /v1/models` is half of the compatibility
        surface, and an HTTP client sends a GET with no Content-Length because
        there is no body to describe.
        """
        raw = self.headers.get("Content-Length")
        if raw is None:
            if method not in ("POST", "PUT", "PATCH"):
                if self.headers.get("Transfer-Encoding"):
                    self._fail(411, "chunked request bodies are not accepted",
                               "length_required", kind="chunked_body")
                    return None
                return b""
            self._fail(411, "Content-Length is required", "length_required",
                       kind="missing_length")
            return None
        try:
            length = int(raw)
        except ValueError:
            self._fail(400, "Content-Length is not a number", "bad_length",
                       kind="bad_length")
            return None
        if length < 0:
            self._fail(400, "Content-Length is negative", "bad_length",
                       kind="bad_length")
            return None
        if length > self.gateway.max_body:
            # Refuse *before* reading.  Reading first and then refusing spends
            # the memory the limit exists to protect.
            self._fail(413, "request body is larger than the %d-byte limit"
                       % self.gateway.max_body, "payload_too_large",
                       kind="body_too_large")
            return None
        return self.rfile.read(length)

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            # Answered without a key on purpose: it says "this process is
            # listening", which is what a supervisor needs and a caller does
            # not.  It names no upstream state, so it cannot be used to probe
            # Strata through here.
            self._send_json(200, {"status": "ok", "service": "llmlab-gateway"})
            return
        if not path.startswith("/v1/"):
            self._fail(404, "no such route on this gateway", "not_found",
                       kind="unknown_route")
            return
        if (method, path) not in ALLOWLIST:
            # A *known-but-unpublished* route is the interesting case: it means
            # the client is talking to a real Strata feature through a door
            # that was never opened for it.  Say which, rather than 404ing with
            # no explanation.
            self._fail(404, "%s is not published by this gateway" % path,
                       "not_published", kind="unpublished_route")
            return

        key = self._presented_key()
        record, reason = self.gateway.store.authenticate(key)
        if record is None or reason != "ok":
            detail = {
                "unknown": "no such API key",
                "disabled": "this API key is disabled",
                "revoked": "this API key has been revoked",
            }[reason]
            if not looks_like_one_of_ours(key) and key:
                detail += " (and it does not look like a key from this service)"
            # 401 with a Bearer challenge: correct for a Bearer scheme, and it
            # does not raise a browser password dialog the way Basic would.
            self._fail(401, detail, "invalid_api_key",
                       extra={"WWW-Authenticate": 'Bearer' + ' realm="llm-lab"'},
                       kind="bad_key_%s" % reason)
            return

        ident = record.get("id") or "?"
        user = record.get("label") or ident

        body = self._read_body(method)
        if body is None:
            return

        body, requested_model = self._apply_aliases(body)

        queue_t0 = time.monotonic()
        try:
            queue_s = self.gateway.gate.acquire(user)
        except TooMany as exc:
            waited_ms = round((time.monotonic() - queue_t0) * 1000.0, 1)
            self.gateway.meters.count_rate_limited()
            self.gateway.store.touch(ident)
            self.gateway.store.record(ident, "rate_limited")
            # Same fields as a forwarded request, with the model-shaped ones at
            # zero: a log whose lines have different columns is a log every
            # reader has to special-case, and the first thing to break is the
            # one consumer that assumed the shape.
            self.gateway.log_usage({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "key_id": ident, "label": user, "method": method,
                "path": path, "model": requested_model, "status": 429,
                "outcome": "rate_limited", "error": "limit_%s" % exc.scope,
                "stream": None, "usage": None, "bytes_out": 0,
                "queue_ms": waited_ms, "ttft_ms": 0.0, "upstream_ms": 0.0,
                "total_ms": waited_ms,
            })
            self._fail(429, exc.detail, "rate_limited",
                       extra={"Retry-After": max(1, int(round(exc.retry_after)))},
                       kind="rate_limited")
            return

        try:
            self.gateway.store.touch(ident)
            result = self._relay(method, path, body, user)
        finally:
            self.gateway.gate.release(user)

        outcome = result["outcome"]
        self.gateway.store.record(ident, outcome)
        self.gateway.meters.count_request(
            outcome, result["status"], queue_ms=queue_s * 1000.0,
            ttft_ms=result["ttft_ms"], upstream_ms=result["upstream_ms"],
            total_ms=result["total_ms"], bytes_out=result["bytes_out"],
            error=result["error"])
        self.gateway.log_usage({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "key_id": ident, "label": user, "method": method, "path": path,
            "model": result["model"] or requested_model,
            "status": result["status"], "outcome": outcome,
            "error": result["error"], "stream": result["stream"],
            "queue_ms": round(queue_s * 1000.0, 1),
            "ttft_ms": result["ttft_ms"], "upstream_ms": result["upstream_ms"],
            "total_ms": result["total_ms"], "bytes_out": result["bytes_out"],
            "usage": result["usage"],
        })

    # -- aliases ---------------------------------------------------------
    def _apply_aliases(self, body: bytes) -> tuple[bytes, str | None]:
        """Rewrite an alias in the request to the real model id.

        Returns (body, model-as-asked-for).  With no aliases configured -- the
        default -- this is a byte-for-byte passthrough, because the transparent
        thing to do with a request is to not touch it.  When an alias *is*
        configured, only the `model` field is changed; prompts, system
        messages, temperature and every other generation parameter are
        forwarded exactly as the caller sent them.
        """
        gw = self.gateway
        ctype = (self.headers.get("Content-Type") or "").lower()
        if len(body) > PARSE_LIMIT:
            return body, None
        model = None
        payload = None
        if body and ("json" in ctype or not ctype):
            try:
                payload = json.loads(body)
            except ValueError:
                return body, None
        if not isinstance(payload, dict):
            return body, None
        model = payload.get("model")
        if not isinstance(model, str):
            return body, None
        real = gw.aliases.get(model)
        if not real:
            return body, model
        payload["model"] = real
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), model

    # -- the relay -------------------------------------------------------
    def _relay(self, method: str, path: str, body: bytes, user: str) -> dict:
        """Forward one request and copy the answer back.  Never raises.

        Returns the measurements the caller logs; `_fail` has already written
        the response when the upstream could not be reached at all.
        """
        gw = self.gateway
        started = time.monotonic()
        result = {"status": 0, "outcome": "failed", "error": None,
                  "ttft_ms": 0.0, "upstream_ms": 0.0, "total_ms": 0.0,
                  "bytes_out": 0, "usage": None, "model": None, "stream": None}

        # Only the parsed path and query travel on.  `self.path` may be an
        # absolute-form target, and forwarding it verbatim would let the caller
        # name the authority -- which is the whole of what SSRF is.
        split = urlsplit(self.path)
        target = split.path + (("?" + split.query) if split.query else "")

        headers = {}
        for k, v in self.headers.items():
            low = k.lower()
            if low in HOP_BY_HOP or low in IDENTITY_HEADERS:
                continue
            if low in ("authorization", "x-api-key", "host", "content-length"):
                continue
            headers[k] = v
        headers["Content-Length"] = str(len(body))
        # The one line that makes this a gateway: the caller's key is gone and
        # Strata's own key takes its place.
        headers["Authorization"] = "Bearer" + " " + gw.upstream_key

        conn = http.client.HTTPConnection(gw.upstream_host, gw.upstream_port,
                                          timeout=gw.upstream_timeout)
        try:
            try:
                conn.request(method, target, body=body, headers=headers)
                resp = conn.getresponse()
            except (socket.timeout, TimeoutError):
                result.update(status=504, error="upstream_timeout")
                self._fail(504, "the model service did not answer within %.0fs"
                           % gw.upstream_timeout, "upstream_timeout",
                           kind="upstream_timeout")
                return self._finish(result, started)
            except OSError as exc:
                result.update(status=502, error="upstream_unreachable")
                self._fail(502, "cannot reach the model service: %s" % exc,
                           "upstream_unreachable", kind="upstream_unreachable")
                return self._finish(result, started)

            result["upstream_ms"] = 0.0
            self._relay_response(resp, path, result, started)
            return self._finish(result, started)
        except CLIENT_GONE:
            result.update(outcome="cancelled", error="client_disconnected")
            return self._finish(result, started)
        except (http.client.HTTPException, OSError) as exc:
            # The upstream died mid-answer.  The status line is already on the
            # wire, so this cannot be turned into a 502 -- what it can be is
            # *visible*: closing the connection truncates the body, which a
            # client reading a Content-Length response will notice, and which
            # an SSE client notices by the absence of its terminator.
            result.update(outcome="failed", error="upstream_disconnected")
            self.close_connection = True
            print("[gateway] upstream dropped mid-response: %r" % (exc,), flush=True)
            return self._finish(result, started)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _relay_response(self, resp, path: str, result: dict, started: float) -> None:
        """Copy the upstream response back, in chunks, without buffering it.

        THE ANSWER IS FORWARDED AS IT ARRIVES, AND THAT IS NOT AN OPTIMISATION.
        An earlier version of this method read a JSON body whole so it could
        parse the token counts out of it before replying.  The token counts are
        the same either way -- but the reply was then held until the upstream
        had finished, which means a client that gave up during a long
        generation kept its concurrency slot until the model was done, and the
        whole point of the slot accounting is that a caller who has left is not
        still costing the box.  So the count is taken from the copy that has
        already gone out (see `capture` below), and only one thing is ever
        buffered: the model list, when it has to be edited before sending.
        """
        length = resp.getheader("Content-Length")
        ctype = (resp.getheader("Content-Type") or "").lower()
        result["status"] = resp.status
        result["outcome"] = "ok" if 200 <= resp.status < 300 else "failed"
        if resp.status >= 400:
            result["error"] = "upstream_http_%d" % resp.status

        known = int(length) if (length is not None and length.isdigit()) else None
        jsonish = known is not None and known <= PARSE_LIMIT and "json" in ctype

        # The one case that cannot be streamed: an advertised model list with an
        # alias to add, because the edit changes the length.  Safe to buffer
        # because it is small and arrives at once -- and it is the reason this
        # branch exists rather than a general "buffer the JSON" rule.
        if jsonish and path == "/v1/models" and self.gateway.aliases:
            raw = b""
            while len(raw) < known:
                piece = resp.read(known - len(raw))
                if not piece:
                    break
                raw += piece
            raw = self._inspect_json(raw, path, result)
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
                result["bytes_out"] = len(raw)
            except CLIENT_GONE:
                result.update(outcome="cancelled", error="client_disconnected")
            result["ttft_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        if known is not None:
            self.send_header("Content-Length", length)
        else:
            # No length: the streaming case (SSE), or a chunked upstream.
            # Rather than re-frame it as chunked here, the answer is delimited
            # by closing the connection -- one framing, written once, in the
            # layer that owns the socket.  `read1`, not `read`: `read(n)` on a
            # socket-backed response blocks until it has n bytes or the stream
            # ends, so a 60-token SSE answer would arrive in one lump at the
            # end, i.e. a stream that streams nothing.
            self.send_header("Connection", "close")
            self.close_connection = True
            result["stream"] = True
        self.end_headers()

        # A copy of what was sent, kept only so the token counts can be read
        # afterwards.  Never used to change the answer, and bounded: past
        # `PARSE_LIMIT` it simply stops growing and `usage` is recorded as
        # absent rather than estimated.
        capture = bytearray() if jsonish else None
        sent = 0
        first = True
        while True:
            if known is None:
                chunk = resp.read1(65536)
            else:
                if sent >= known:
                    break
                chunk = resp.read(min(65536, known - sent))
            if not chunk:
                break
            if first:
                result["ttft_ms"] = round((time.monotonic() - started) * 1000.0, 1)
                first = False
            if capture is None:
                self._scan_sse(chunk, result)
            elif len(capture) < PARSE_LIMIT:
                capture += chunk[:PARSE_LIMIT - len(capture)]
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except CLIENT_GONE:
                result.update(outcome="cancelled", error="client_disconnected")
                return                       # the slot is released by the caller
            sent += len(chunk)
        result["bytes_out"] = sent
        if capture is not None:
            self._inspect_json(bytes(capture), path, result)

    def _inspect_json(self, raw: bytes, path: str, result: dict) -> bytes:
        """Read a JSON answer without changing it.

        For `/v1/models` this is also where the console's model list comes
        from, and where a configured alias is added -- by mirroring the shape
        the upstream itself used, so a client that reads `created` or
        `owned_by` off a real entry finds the same keys on ours.  If the
        upstream's answer is not the documented shape, nothing is invented: the
        body is relayed untouched and the field is left null.
        """
        try:
            payload = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(payload, dict):
            return raw
        usage = payload.get("usage")
        if isinstance(usage, dict):
            result["usage"] = usage          # verbatim, or absent -- never estimated
        if isinstance(payload.get("model"), str):
            result["model"] = payload["model"]

        if path == "/v1/models":
            data = payload.get("data")
            if not isinstance(data, list):
                return raw
            models = [m.get("id") for m in data if isinstance(m, dict)]
            self.gateway.meters.note_upstream(
                ok=200 <= result["status"] < 300, status=result["status"],
                models=models)
            if not self.gateway.aliases:
                return raw
            have = {m for m in models if isinstance(m, str)}
            template = next((m for m in data if isinstance(m, dict)), {})
            for alias, real in sorted(self.gateway.aliases.items()):
                if alias in have:
                    continue
                entry = {"id": alias, "object": "model"}
                for field in ("created", "owned_by"):
                    if field in template:
                        entry[field] = template[field]
                data.append(entry)
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return raw

    def _scan_sse(self, chunk: bytes, result: dict) -> None:
        """Pick up a token count if the stream carries one.

        OpenAI-compatible servers report usage in the final chunk when asked
        with `stream_options: {include_usage: true}`; some do not report it at
        all.  Both are fine -- the field stays null in the second case.  What is
        not fine is producing a number the upstream never sent.
        """
        if b'"usage"' not in chunk:
            return
        for line in chunk.splitlines():
            if not line.startswith(b"data:"):
                continue
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                continue
            usage = payload.get("usage") if isinstance(payload, dict) else None
            if isinstance(usage, dict):
                result["usage"] = usage

    def _finish(self, result: dict, started: float) -> dict:
        result["total_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        result["upstream_ms"] = round(max(0.0, result["total_ms"] - result["ttft_ms"]), 1)
        return result


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, gateway: Gateway):
        self.gateway = gateway
        handler.gateway = gateway
        super().__init__(addr, handler)

    def handle_error(self, request, client_address):
        """Same rule as the handler: a disconnect is not an incident."""
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            return
        super().handle_error(request, client_address)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="LLM Lab model API gateway")
    ap.add_argument("--listen", default="localhost:8200",
                    help="host:port; the host must be a loopback address")
    ap.add_argument("--upstream", default="http://localhost:8080",
                    help="the model service to forward to (fixed, not client-choosable)")
    ap.add_argument("--key-file", default="{{LAB_ROOT}}/services/secrets/strata-key.env")
    ap.add_argument("--key-var", default="STRATA_API_KEY")
    ap.add_argument("--keys", default="{{LAB_ROOT}}/llm-lab-data/api-gateway/keys.json",
                    help="the key store; must not be inside the git checkout")
    ap.add_argument("--usage-log",
                    default="{{LAB_ROOT}}/llm-lab-data/api-gateway/usage.jsonl",
                    help="one line per request, without any request or response body")
    ap.add_argument("--status-file",
                    default="{{LAB_ROOT}}/llm-lab-data/api-gateway/status.json",
                    help="the console's live view of this process")
    ap.add_argument("--alias", action="append", default=[], metavar="NAME=REAL",
                    help="advertise a stable model name and map it to a real one "
                         "(repeatable; omit for a pure passthrough)")
    ap.add_argument("--global-limit", type=int, default=1,
                    help="requests the model service can run at once (parallel=1)")
    ap.add_argument("--per-user-limit", type=int, default=1)
    ap.add_argument("--queue-limit", type=int, default=8)
    ap.add_argument("--wait-timeout", type=float, default=120.0)
    ap.add_argument("--retry-after", type=float, default=5.0)
    ap.add_argument("--max-body-mib", type=float, default=8.0)
    ap.add_argument("--upstream-timeout", type=float, default=300.0)
    ap.add_argument("--probe-interval", type=float, default=60.0,
                    help="seconds between upstream health reads")
    return ap


def _split_listen(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit("--listen wants host:port, got %r" % value)
    return host, int(port)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    host, port = _split_listen(args.listen)
    if not _is_loopback(host):
        # Not a preference.  The gateway holds Strata's key; publishing it on a
        # routable address would put a key-holding process on the LAN, and the
        # brief keeps "no new LAN listening port" in force.  Caddy is the thing
        # that faces outward, and it has TLS and a front-door policy.
        raise SystemExit(
            "refusing to listen on %s: the gateway must bind a loopback address "
            "(it is published through the reverse proxy, never directly)" % host)

    upstream = urlsplit(args.upstream)
    if upstream.scheme not in ("http", "https") or not upstream.hostname:
        raise SystemExit("--upstream must be an http(s) URL, got %r" % args.upstream)
    if not _is_loopback(upstream.hostname or ""):
        print("[gateway] warning: upstream %s is not loopback" % args.upstream,
              flush=True)

    key = read_key_file(args.key_file, args.key_var)
    if not key:
        # Starting without a key would forward unauthenticated requests to a
        # key-gated upstream, turning every caller's request into a 401 that
        # looks like a bad caller key.  Refuse instead, and say which file.
        raise SystemExit("no API key found: %s has no %s"
                         % (args.key_file, args.key_var))

    store = KeyStore(args.keys)
    gate = Gate(global_limit=args.global_limit,
                per_user_limit=args.per_user_limit,
                queue_limit=args.queue_limit,
                wait_timeout_s=args.wait_timeout,
                retry_after_s=args.retry_after)
    gw = Gateway(upstream_url=args.upstream, upstream_key=key, store=store,
                 gate=gate, meters=Meters(),
                 usage_log=UsageLog(args.usage_log),
                 status_file=StatusFile(args.status_file),
                 aliases=parse_aliases(args.alias),
                 max_body=int(args.max_body_mib * 1024 * 1024),
                 upstream_timeout=args.upstream_timeout,
                 listen=args.listen, probe_interval=args.probe_interval)
    gw.start()
    httpd = Server((host, port), Handler, gw)
    print("[gateway] listening on %s -> %s (global=%d per-user=%d queue=%d)"
          % (args.listen, args.upstream, gate.global_limit, gate.per_user_limit,
             gate.queue_limit), flush=True)
    if gw.aliases:
        print("[gateway] model aliases: %s"
              % ", ".join("%s=%s" % kv for kv in sorted(gw.aliases.items())),
              flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        gw.stop()
        store.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
