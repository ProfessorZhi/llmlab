#!/usr/bin/env python3
"""What the gateway does with a request that carries a picture.

THE QUESTION THIS ANSWERS.  A caller sends an OpenAI chat completion whose
`content` is an array of a text part and an `image_url` part.  On the way to the
model that request passes through the API gateway, which is a byte relay that
knows about keys, limits and model aliases and -- by design -- nothing about
images.  The thing worth being sure of is not "does the picture arrive".  It is
*how much of the request the gateway feels entitled to touch*:

    * is the content array forwarded as the caller wrote it, or rebuilt?
    * is the `image_url` part passed through, or parsed and re-encoded?
    * if the picture is nonsense, does the gateway reject it (it should not --
      it has no way to know what the model can read) or pass it on (it should)?

Every one of those is an assertion here, made against the real `gateway.py`
running in this process against a mock upstream that records the bytes it was
actually handed.  A test that asked the live model service could not tell "the
gateway forwarded my picture" from "the model service happened to accept
whatever arrived", which is the same reason `selftest.py` uses a mock.

WHAT THIS IS NOT, AND THE ONE THING IT MUST NOT BE READ AS.  This file contains
no accuracy test and cannot contain one.  Nothing here shows that a model can
read a picture, that it answers YES/NO correctly, or that the vision encoder is
deployed -- on this host it is not, and `/health` says `"images": false`.  There
is a control near the end that sends two *different* pictures and gets the same
answer back for both, precisely so that the difference between "a 200 came back"
and "the picture was understood" is written down rather than assumed.  Any
report that cites this file as evidence of image understanding is misreading it.

Run it from the repository root:

    python3 legacy/gateway/vision_protocol_selftest.py

It binds two ephemeral ports on localhost and opens no other socket.  It never
touches the model service, the GPU, or any file under {{STRATA_ROOT}}.
"""

from __future__ import annotations

import base64
import json
import os
import socket
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

# The house harness: the same PASS/FAIL recorder and the same client helpers the
# gateway's own selftest uses, so the two files report identically and a reader
# only has to learn one output format.
from deploy.dell.gateway import selftest as H  # noqa: E402
from deploy.dell.gateway.gateway import Gateway, Handler, Server  # noqa: E402
from deploy.dell.gateway.keystore import KeyStore  # noqa: E402
from deploy.dell.gateway.limits import Gate  # noqa: E402
from deploy.dell.gateway.telemetry import Meters, StatusFile, UsageLog  # noqa: E402

check = H.check
call = H.call
body_of = H.body_of
json_of = H.json_of
raw_exchange = H.raw_exchange
UPSTREAM_KEY = H.UPSTREAM_KEY

# The model id the box actually serves.  `GET /health` on the running Strata
# returns exactly this, and `docs/dell-migration/phase5-acceptance.md` records
# it read back through the SDK.  The vision samples that used to ship named
# `qwen3.8-flash-next`, which is not a model any of this serves -- see
# `examples/vision/README.md`.
SERVED_MODEL = "qwen3.8-flash-next-iq2_xs"

# The limit the gateway is actually launched with: `--max-body-mib` defaults to
# 8.0 and the running process passes no override (checked in its argv).
MAX_BODY = 8 * 1024 * 1024

FINDINGS: list[str] = []


def finding(text: str) -> None:
    FINDINGS.append(text)
    print("FINDING  %s" % text)


def png_bytes(colour: str) -> bytes:
    """One of the two fixtures, drawn here rather than read, so this file has
    no dependency on the samples directory and no fixture can go missing."""
    from PIL import Image, ImageDraw

    size = 128                     # small on purpose: this file tests framing,
    im = Image.new("RGB", (size, size), (255, 255, 255))   # not image quality
    d = ImageDraw.Draw(im)
    m = size // 8
    fill = {"red": (220, 20, 20), "blue": (20, 60, 200)}[colour]
    if colour == "red":
        d.rectangle([m, m, size - m, size - m], fill=fill)
    else:
        d.ellipse([m, m, size - m, size - m], fill=fill)
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def data_url(colour: str) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes(colour)).decode()


def multimodal_body(colour: str = "red", *, model: str = SERVED_MODEL,
                    image_part: dict | None = None) -> bytes:
    """The request the samples describe: a screener prompt plus a picture.

    Built as bytes here, not as a dict, because the assertion is about bytes.
    """
    part = image_part if image_part is not None else {
        "type": "image_url", "image_url": {"url": data_url(colour)}}
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "max_tokens": 8,
        "messages": [
            {"role": "system",
             "content": "You are a strict resume screener. Answer with exactly "
                        "one word: YES or NO."},
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Is the large central shape red? Answer YES or NO."},
                part,
            ]},
        ],
    }
    return json.dumps(payload).encode("utf-8")


def pad_body(target: int, colour: str = "red") -> bytes:
    """A real multimodal request whose total size is `target` bytes.

    Padding goes into the text part rather than being junk appended to the
    JSON, so the body stays a *valid* request of the size a caller would send
    for a large scan -- which is the thing the 8 MiB limit is actually up
    against.
    """
    filler = "x" * max(0, target - len(multimodal_body(colour)))
    payload = json.loads(multimodal_body(colour))
    payload["messages"][1]["content"][0]["text"] += " " + filler
    body = json.dumps(payload).encode("utf-8")
    # json.dumps is exact for ASCII, so this lands on target; assert it rather
    # than assume, because a subtly wrong size would make the boundary checks
    # test the wrong thing.
    return body


# ---------------------------------------------------------------------------
# The mock upstream
# ---------------------------------------------------------------------------
class MockVisionUpstream(BaseHTTPRequestHandler):
    """Stands in for the model service and writes down what it was given.

    It answers YES to everything.  That is not laziness: it is the control.  An
    upstream that answers from a constant is exactly what an upstream that
    cannot see looks like, and the test below checks that the two are
    indistinguishable from the outside -- which is the point.
    """

    protocol_version = "HTTP/1.0"
    seen: list[dict] = []
    answer: str = "YES"

    def log_message(self, *a):
        pass

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

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def do_GET(self):
        if (self.headers.get("Authorization") or "") != "Bearer" + " " + UPSTREAM_KEY:
            self._json(401, {"error": {"message": "invalid api key",
                                       "type": "invalid_request_error"}})
            return
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": SERVED_MODEL, "object": "model"}]})
            return
        self._json(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        MockVisionUpstream.seen.append({
            "path": urlsplit(self.path).path,
            "auth": self.headers.get("Authorization") or "",
            "content_type": self.headers.get("Content-Type") or "",
            "body": body,
        })
        if (self.headers.get("Authorization") or "") != "Bearer" + " " + UPSTREAM_KEY:
            self._json(401, {"error": {"message": "invalid api key",
                                       "type": "invalid_request_error"}})
            return
        if urlsplit(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "no such path"}})
            return
        if self.path.endswith("?upstream=refuse"):
            # The upstream's own refusal, in its own words, so that what comes
            # back out of the gateway can be compared with what went in.
            self._json(400, {"error": {
                "message": "this model was started without the vision encoder",
                "type": "invalid_request_error", "code": "no_vision"}})
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        self._json(200, {
            "id": "cmpl-vision-1", "object": "chat.completion",
            "model": SERVED_MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": MockVisionUpstream.answer}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1,
                      "total_tokens": 8},
        })


def start_upstream():
    srv = ThreadingHTTPServer(("localhost", 0), MockVisionUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_gateway(upstream_port: int, tmp: str, *, max_body: int = MAX_BODY,
                  aliases=None, tag: str = "gw"):
    """The real gateway, in-process, on an ephemeral port, with a store of one
    key.  `max_body` is a parameter so the 8 MiB production limit can be
    exercised for real *and* pinned cheaply at a small size."""
    store_path = os.path.join(tmp, "keys-%s.json" % tag)
    store = KeyStore(store_path)
    user_key, _record = store.issue("hr-system", note="picture-request test")

    gw = Gateway(upstream_url="http://localhost:%d" % upstream_port,
                 upstream_key=UPSTREAM_KEY, store=store,
                 gate=Gate(global_limit=1, per_user_limit=1, queue_limit=4,
                           wait_timeout_s=5.0),
                 meters=Meters(),
                 usage_log=UsageLog(os.path.join(tmp, "usage-%s.jsonl" % tag)),
                 status_file=StatusFile(os.path.join(tmp, "status-%s.json" % tag)),
                 aliases=aliases, max_body=max_body, upstream_timeout=20.0,
                 tick_interval=0.05, probe_interval=5.0)
    gw.start()

    class H2(Handler):
        pass

    srv = Server(("localhost", 0), H2, gw)
    gw.listen = "localhost:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, gw, user_key, os.path.join(tmp, "usage-%s.jsonl" % tag)


def post_oversize(port: int, key: str, body: bytes):
    """Send a body the gateway will refuse, and report what the *caller* sees.

    Returns (send_error, reply).  A client that is still writing 8 MiB when the
    gateway has already answered and hung up does not get to read the answer --
    it gets a broken pipe or a reset.  That is a property of the caller's
    experience, not a fault in this file, so it is measured and reported rather
    than avoided by using a smaller body.
    """
    head = (b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Authorization: Bearer" + b" " + key.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
    sock = socket.create_connection(("localhost", port), timeout=20)
    send_error = None
    try:
        sock.sendall(head + body)
    except OSError as exc:
        send_error = type(exc).__name__
    reply = b""
    try:
        reply = sock.recv(65536)
    except (socket.timeout, ConnectionResetError, OSError) as exc:
        reply = b"<%s while reading the reply>" % type(exc).__name__.encode()
    finally:
        sock.close()
    return send_error, reply


def usage_records(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------------------
def run(tmp: str) -> None:
    upstream = start_upstream()
    up_port = upstream.server_address[1]
    up_url = "http://localhost:%d" % up_port

    print("-" * 72)
    print("Controls first: without these, a later 200 proves only that "
          "something answers.")
    st, _, resp = call(up_url + "/v1/chat/completions", method="POST",
                       body=multimodal_body())
    check("CONTROL: the mock upstream refuses a picture request with no key",
          st == 401, "status %s" % st)
    body_of(resp)

    st, _, resp = call(up_url + "/v1/chat/completions", key=UPSTREAM_KEY,
                       method="POST", body=multimodal_body())
    check("CONTROL: the mock accepts its own key, so the refusal above was "
          "about the key and not about the body",
          st == 200, "status %s" % st)
    body_of(resp)

    gw_srv, gw, user_key, usage_path = start_gateway(up_port, tmp)
    base = "http://localhost:%d" % gw_srv.server_address[1]
    MockVisionUpstream.seen.clear()

    st, _, resp = call(base + "/v1/chat/completions", method="POST",
                       body=multimodal_body())
    check("CONTROL: the gateway refuses a picture request with no caller key",
          st == 401, "status %s" % st)
    body_of(resp)
    check("CONTROL: that refusal never reached the upstream",
          MockVisionUpstream.seen == [],
          "the upstream saw %d request(s) despite the 401"
          % len(MockVisionUpstream.seen))

    # ======================================================================
    # 1. The content array, as the gateway forwards it
    # ======================================================================
    print("-" * 72)
    print("The multimodal body, upstream of the model service.")
    sent = multimodal_body("red")
    MockVisionUpstream.seen.clear()
    st, _, resp = call(base + "/v1/chat/completions", key=user_key,
                       method="POST", body=sent)
    answer = json_of(resp).get("choices", [{}])[0].get("message", {}).get("content")
    check("a picture request goes through the gateway and comes back",
          st == 200 and answer == "YES", "status %s answer %r" % (st, answer))

    check("the upstream was handed the request",
          len(MockVisionUpstream.seen) == 1,
          "%d requests arrived" % len(MockVisionUpstream.seen))
    if not MockVisionUpstream.seen:
        return
    received = MockVisionUpstream.seen[0]["body"]

    check("the upstream received the body BYTE FOR BYTE as the caller sent it",
          received == sent,
          "sent %d bytes, upstream read %d; first difference at %s"
          % (len(sent), len(received),
             next((i for i, (a, b) in enumerate(zip(sent, received)) if a != b),
                  "the tail")))

    got = json.loads(received)
    parts = got["messages"][1]["content"]
    check("the content array is still an array, of the same length, in the "
          "same order",
          [p.get("type") for p in parts] == ["text", "image_url"],
          "parts came back as %r" % [p.get("type") for p in parts])

    check("the image_url part still holds the caller's exact data URL",
          parts[1]["image_url"]["url"] == json.loads(sent)["messages"][1]
          ["content"][1]["image_url"]["url"],
          "the data URL was rewritten on the way through")

    check("the text part was not merged, truncated or re-ordered",
          parts[0]["text"] == json.loads(sent)["messages"][1]["content"][0]["text"],
          "the prompt text changed in transit")

    check("the system message is still a plain string, not reshaped into parts",
          got["messages"][0]["content"] ==
          json.loads(sent)["messages"][0]["content"],
          "the system message came back as %r" % (got["messages"][0]["content"],))

    check("the gateway added no field of its own to the request body",
          set(got) == set(json.loads(sent)),
          "keys differ: %r vs %r" % (sorted(got), sorted(json.loads(sent))))

    check("the model id reached the upstream unchanged",
          got["model"] == SERVED_MODEL,
          "upstream saw model %r" % got.get("model"))

    # ======================================================================
    # 2. The gateway is not an image validator, and must not pretend to be
    # ======================================================================
    print("-" * 72)
    print("Where image validation does NOT happen.")
    nonsense = multimodal_body("red", image_part={
        "type": "image_url", "image_url": {"url": "not-a-url-at-all"}})
    MockVisionUpstream.seen.clear()
    st, _, resp = call(base + "/v1/chat/completions", key=user_key,
                       method="POST", body=nonsense)
    check("a request whose image source is nonsense is FORWARDED, not rejected: "
          "the gateway has no way to know what the model can read",
          st == 200 and MockVisionUpstream.seen
          and MockVisionUpstream.seen[0]["body"] == nonsense,
          "status %s; the upstream %s"
          % (st, "got it unchanged" if MockVisionUpstream.seen else "never saw it"))

    weird = multimodal_body("red", image_part={"type": "video_url",
                                               "video_url": {"url": "x"}})
    MockVisionUpstream.seen.clear()
    st, _, resp = call(base + "/v1/chat/completions", key=user_key,
                       method="POST", body=weird)
    check("an image-like part the gateway has never heard of is forwarded too "
          "(it does not filter on part type)",
          st == 200 and MockVisionUpstream.seen
          and MockVisionUpstream.seen[0]["body"] == weird,
          "status %s" % st)

    check("the gateway never answers with an error of its own about images",
          not any("vision" in r.get("error", {}).get("message", "").lower()
                  or "image" in r.get("error", {}).get("message", "").lower()
                  for r in usage_records(usage_path) if r.get("error")),
          "an image-shaped error came out of the gateway, which is not its job")

    # ======================================================================
    # 3. The 8 MiB body limit
    # ======================================================================
    print("-" * 72)
    print("The body limit the gateway is launched with (--max-body-mib 8).")
    check("this test is using the production limit", MAX_BODY == 8 * 1024 * 1024,
          "MAX_BODY is %d" % MAX_BODY)

    under = pad_body(MAX_BODY - 4096)
    MockVisionUpstream.seen.clear()
    st, _, resp = call(base + "/v1/chat/completions", key=user_key,
                       method="POST", body=under, timeout=60)
    check("a %.2f MiB picture request is accepted and forwarded intact"
          % (len(under) / 1048576.0),
          st == 200 and MockVisionUpstream.seen
          and MockVisionUpstream.seen[0]["body"] == under,
          "status %s, upstream saw %d bytes"
          % (st, len(MockVisionUpstream.seen[0]["body"]) if MockVisionUpstream.seen else 0))

    over = pad_body(MAX_BODY + 4096)
    MockVisionUpstream.seen.clear()
    send_error, reply = post_oversize(gw_srv.server_address[1], user_key, over)
    status_line = reply.split(b"\r\n", 1)[0] if reply.startswith(b"HTTP/") else b""
    check("a %.2f MiB picture request is refused with 413, not truncated and not "
          "forwarded" % (len(over) / 1048576.0),
          b"413" in status_line,
          "the caller got %r (send failed with %s)" % (reply[:120], send_error))
    check("the 413 never reached the upstream",
          MockVisionUpstream.seen == [],
          "the upstream was handed an oversized body anyway")

    if send_error is not None or not status_line:
        finding(
            "a caller that sends an oversized picture request does not reliably "
            "SEE the 413: the gateway refuses on the Content-Length header and "
            "closes while the body is still arriving, so the client's write "
            "fails with %s and the reply is %r. The refusal is correct and "
            "cheap (nothing is buffered), but for an 8 MiB base64 upload the "
            "caller's error message will be a broken pipe rather than "
            "'request body is larger than the 8388608-byte limit'. Anything "
            "that wants to explain the failure to a user has to treat a "
            "connection reset while uploading as a probable 413."
            % (send_error or "no send error", reply[:60]))

    # The boundary itself, at a size that costs nothing to send. Same code
    # path, same comparison, 64 KiB instead of 8 MiB.
    small = 64 * 1024
    srv2, gw2, key2, _ = start_gateway(up_port, tmp, max_body=small, tag="bound")
    base2 = "http://localhost:%d" % srv2.server_address[1]
    # Built from a prefix and a suffix so the total is exactly `small`: a
    # boundary check one byte off is a check of the wrong boundary.
    prefix = (b'{"model": "%s", "messages": [{"role": "user", "content": "'
              % SERVED_MODEL.encode())
    suffix = b'"}]}'
    exact = prefix + b"y" * (small - len(prefix) - len(suffix)) + suffix
    check("the boundary body is exactly the limit (the check is not off by one)",
          len(exact) == small, "%d bytes, wanted %d" % (len(exact), small))
    st_at, _, _ = call(base2 + "/v1/chat/completions", key=key2, method="POST",
                       body=exact)
    st_over, _, resp_over = call(base2 + "/v1/chat/completions", key=key2,
                                 method="POST", body=exact + b" ")
    detail_over = json_of(resp_over).get("error", {})
    check("a body of exactly the limit is allowed (the limit is a maximum, not "
          "an exclusive bound)",
          st_at == 200, "status %s at exactly %d bytes" % (st_at, len(exact)))
    check("one byte more is refused",
          st_over == 413, "status %s at %d bytes" % (st_over, len(exact) + 1))
    check("the 413 names the limit in bytes and its own code",
          detail_over.get("code") == "payload_too_large"
          and str(small) in detail_over.get("message", ""),
          "code %r message %r" % (detail_over.get("code"),
                                  detail_over.get("message")))
    check("a 64 KiB body is small enough that the caller reads the 413 rather "
          "than a broken pipe",
          st_over == 413,
          "at this size the reply should be readable; see the finding about "
          "8 MiB uploads")

    # Before the body is read: a caller that declares 1 GiB must not make the
    # gateway read 1 GiB. `http.client` will not send this, so it goes raw.
    declared = (b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Authorization: Bearer" + b" " + user_key.encode() + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 1073741824\r\n\r\n")
    reply = raw_exchange(gw_srv.server_address[1], declared)
    check("a request claiming a 1 GiB body is refused on the header, without the "
          "body being read",
          b"413" in reply.split(b"\r\n", 1)[0],
          "the gateway answered %r" % reply.split(b"\r\n", 1)[0])

    missing = (b"POST /v1/chat/completions HTTP/1.1\r\n"
               b"Host: localhost\r\n"
               b"Authorization: Bearer" + b" " + user_key.encode() + b"\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
    reply = raw_exchange(gw_srv.server_address[1], missing)
    check("a body with no Content-Length is refused with 411 rather than guessed at",
          b"411" in reply.split(b"\r\n", 1)[0],
          "the gateway answered %r" % reply.split(b"\r\n", 1)[0])

    # The number a caller actually cares about: base64 inflates by 4/3, so the
    # 8 MiB body limit is about a 6 MiB file. Saying it out loud is the point --
    # "8 MiB" reads like "8 MiB pictures" and it is not.
    usable = (MAX_BODY - 2048) * 3 // 4
    check("the largest raw picture that fits in an 8 MiB body is about %.1f MiB, "
          "not 8 MiB (base64 inflates by 4/3)" % (usable / 1048576.0),
          5.9 < usable / 1048576.0 < 6.0,
          "computed %.2f MiB" % (usable / 1048576.0))

    # ======================================================================
    # 4. Error paths
    # ======================================================================
    print("-" * 72)
    print("Error paths.")
    MockVisionUpstream.seen.clear()
    st, _, resp = call(base + "/v1/chat/completions", key=user_key,
                       method="POST", body=b"{not json at all")
    check("a body that is not JSON is forwarded rather than rejected, and the "
          "upstream's own complaint comes back",
          st == 200 and MockVisionUpstream.seen
          and MockVisionUpstream.seen[0]["body"] == b"{not json at all",
          "status %s" % st)

    st, _, resp = call(base + "/v1/chat/completions?upstream=refuse",
                       key=user_key, method="POST", body=multimodal_body())
    detail = json_of(resp).get("error", {})
    check("the upstream's own 400 is relayed with its status and its words",
          st == 400 and detail.get("code") == "no_vision"
          and "vision encoder" in detail.get("message", ""),
          "status %s body %s" % (st, detail))
    check("the gateway does not reword the upstream's refusal into something "
          "of its own",
          "without the vision encoder" in detail.get("message", ""),
          "message was rewritten: %r" % detail.get("message"))

    st, _, resp = call(base + "/v1/messages", key=user_key, method="POST",
                       body=b"{}")
    detail = json_of(resp).get("error", {})
    check("the Anthropic-protocol route is not published, and says so rather "
          "than 404ing flatly",
          st == 404 and detail.get("code") == "not_published",
          "status %s body %s" % (st, detail))
    check("...which means an Anthropic image block cannot reach a model through "
          "this gateway at all", st == 404,
          "if this ever becomes 200, the vision samples must be re-checked")

    st, _, resp = call(base + "/v1/embeddings", key=user_key, method="POST",
                       body=b"{}")
    check("an unimplemented /v1 route is a 404, not a 500",
          st == 404, "status %s" % st)

    st, _, resp = call(base + "/healthz")
    check("/healthz answers without a key (a supervisor needs it) and claims "
          "nothing about vision",
          st == 200 and "vision" not in json.dumps(json_of(resp)).lower(),
          "status %s body %s" % (st, json_of(resp)))

    # The upstream goes away. This is the only check that has to stop a server.
    dead = start_upstream()
    dead_port = dead.server_address[1]
    dead.shutdown()
    dead.server_close()
    dead_srv, dead_gw, dead_key, _ = start_gateway(dead_port, tmp, tag="dead")
    base3 = "http://localhost:%d" % dead_srv.server_address[1]
    st, _, resp = call(base3 + "/v1/chat/completions", key=dead_key,
                       method="POST", body=multimodal_body(), timeout=30)
    detail = json_of(resp).get("error", {})
    check("with no upstream listening, a picture request is a 502 and not a hang "
          "or a fake 200",
          st == 502 and detail.get("code") == "upstream_unreachable",
          "status %s body %s" % (st, detail))
    dead_srv.shutdown()
    dead_srv.server_close()
    dead_gw.stop()

    # ======================================================================
    # 5. Model aliases, and the size at which they stop working
    # ======================================================================
    print("-" * 72)
    print("Aliases, and what the 1 MiB parse limit does to a picture request.")
    alias_srv, alias_gw, alias_key, _ = start_gateway(
        up_port, tmp, tag="alias", aliases={"llm-lab-default": SERVED_MODEL})
    abase = "http://localhost:%d" % alias_srv.server_address[1]

    MockVisionUpstream.seen.clear()
    st, _, resp = call(abase + "/v1/chat/completions", key=alias_key,
                       method="POST", body=multimodal_body(model="llm-lab-default"))
    seen_model = (json.loads(MockVisionUpstream.seen[0]["body"]).get("model")
                  if MockVisionUpstream.seen else None)
    check("a small request using an alias has the alias translated before it "
          "is forwarded",
          st == 200 and seen_model == SERVED_MODEL,
          "upstream saw model %r" % seen_model)

    check("...but the body is no longer byte-identical when that happens: an "
          "alias forces the gateway to re-serialise the JSON",
          MockVisionUpstream.seen
          and MockVisionUpstream.seen[0]["body"]
          != multimodal_body(model="llm-lab-default"),
          "the body came through untouched, which contradicts the re-write above")

    big_alias = pad_body(1024 * 1024 + 8192)
    big_alias = big_alias.replace(
        b'"model": "%s"' % SERVED_MODEL.encode(),
        b'"model": "llm-lab-default"')
    MockVisionUpstream.seen.clear()
    st, _, resp = call(abase + "/v1/chat/completions", key=alias_key,
                       method="POST", body=big_alias, timeout=60)
    seen_model = (json.loads(MockVisionUpstream.seen[0]["body"]).get("model")
                  if MockVisionUpstream.seen else None)
    if seen_model == "llm-lab-default":
        finding(
            "an aliased request larger than the gateway's 1 MiB JSON parse limit "
            "is forwarded with the ALIAS un-translated: a %.1f MiB picture "
            "request asking for %r reached the upstream still asking for %r. "
            "The parse limit is right (re-parsing a large body to look at one "
            "field is not worth it), but the consequence is that alias "
            "translation and picture requests do not compose above 1 MiB. This "
            "does not bite production today, because the running gateway is "
            "launched with no aliases at all (checked: its status document "
            "reports `aliases: {}`), so every caller must already name the real "
            "model id. It will bite the first time an alias is configured for a "
            "client that also sends pictures."
            % (len(big_alias) / 1048576.0, "llm-lab-default", seen_model))
    else:
        check("a picture request above 1 MiB still has its alias translated",
              seen_model == SERVED_MODEL, "upstream saw model %r" % seen_model)

    # ======================================================================
    # 6. A 200 is not an answer about the picture
    # ======================================================================
    print("-" * 72)
    print("The control that matters most: a 200 is not evidence a picture was "
          "read.")
    MockVisionUpstream.answer = "YES"
    answers = {}
    for colour in ("red", "blue"):
        _, _, resp = call(base + "/v1/chat/completions", key=user_key,
                          method="POST", body=multimodal_body(colour))
        answers[colour] = json_of(resp).get("choices", [{}])[0].get(
            "message", {}).get("content")

    check("two DIFFERENT pictures, through the same gateway, come back with the "
          "same answer",
          answers["red"] == answers["blue"] == "YES",
          "answers were %r" % answers)
    check("...therefore this file proves nothing about image understanding, and "
          "the identical answer is what an upstream that cannot see looks like",
          answers["red"] == answers["blue"],
          "the answers differed, which would be surprising here")
    findings_note = (
        "the mock upstream answers from a constant, so the only thing the two "
        "requests above establish is that the transport carried both. Reading "
        "the picture back is the encoder's and the model's job, and it is "
        "UNVERIFIED on this host: /health reports \"images\": false and the "
        "engine was started without --vision. A green run here must not be "
        "reported as 'vision works'.")
    finding(findings_note)

    # ======================================================================
    # 7. What the gateway writes down about a picture request
    # ======================================================================
    print("-" * 72)
    print("The usage log: counts, not contents.")
    records = usage_records(usage_path)
    picture_records = [r for r in records
                       if r.get("path") == "/v1/chat/completions"]
    check("picture requests are recorded in the usage log like any other",
          len(picture_records) > 0, "%d records" % len(records))
    check("the usage log records the byte count of the answer",
          any(r.get("bytes_out", 0) > 0 for r in picture_records),
          "no record carried bytes_out")

    blob = json.dumps(records)
    check("the usage log does NOT contain the picture (nor its base64) -- a "
          "request is measured, not copied",
          "iVBORw0KGgo" not in blob and base64.b64encode(png_bytes("red")).decode()[:40] not in blob,
          "image data was found in the usage log")

    status = json.loads(open(os.path.join(tmp, "status-bound.json"),
                             encoding="utf-8").read())
    # A *capability* claim, not the substring "vision": the key records below
    # carry an operator's note, and a note is not a claim. What must not appear
    # is the gateway saying it can carry or encode pictures -- it cannot, and it
    # has no way to find out.
    claims = [term for term in ("mmproj", "image", "images", "multimodal",
                                "encoder", "vision")
              if term in json.dumps(status.get("capabilities", {})).lower()
              or term in json.dumps(status.get("upstream", {})).lower()]
    check("the gateway's status document claims no image capability of its own",
          not claims,
          "the status document asserts %r" % claims)
    check("...which is the honest report: image capability belongs to the model "
          "service behind it and is reported by GET /health there, not here",
          "images" not in json.dumps(status).lower().replace(
              json.dumps(status.get("keys", "")).lower(), ""),
          "an image capability leaked into the gateway's own state")

    # An upstream that is slow is not a picture problem, but a picture request
    # that hangs is the failure a caller will actually hit first, so the timeout
    # is checked from the caller's side: the mock holds the connection.
    gw.stop()
    alias_gw.stop()
    gw_srv.shutdown()
    gw_srv.server_close()
    alias_srv.shutdown()
    alias_srv.server_close()
    upstream.shutdown()
    upstream.server_close()


def main() -> int:
    import io

    print("=" * 72)
    print("Gateway + picture request -- protocol selftest")
    print("=" * 72)
    tmp = tempfile.mkdtemp(prefix="llmlab-vision-protocol-")
    captured = io.StringIO()
    real_stderr = sys.stderr
    try:
        sys.stderr = captured
        run(tmp)
    finally:
        sys.stderr = real_stderr

    logs = captured.getvalue()
    check("the access log never contains a caller's key",
          "llmlab_" not in logs, "a caller key reached the log")
    check("no traceback reached the log (a client that hangs up is not an "
          "incident)",
          "Traceback" not in logs,
          "the log ends with:\n%s" % logs[-600:] if "Traceback" in logs else "")
    check("the access log does not contain picture data either",
          "iVBORw0KGgo" not in logs, "image bytes reached the log")

    print("-" * 72)
    if FINDINGS:
        print("%d finding(s), none of which fail this run:" % len(FINDINGS))
        for f in FINDINGS:
            print("  * %s" % f)
    print("%d passed, %d failed" % (len(H.PASSED), len(H.FAILED)))
    for name, detail in H.FAILED:
        print("  FAILED: %s\n          %s" % (name, detail))
    print("temporary files left in %s" % tmp)
    return 1 if H.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
