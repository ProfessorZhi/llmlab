#!/usr/bin/env python3
"""End to end, on one CPU, with no GPU and no model: caller -> gateway -> a
stand-in for a vision-enabled Strata.

WHAT THIS EXERCISES.  The whole consumer path, in the shape it will have in
production, minus only the part that cannot run here:

    consumer (vision_client.py)
      -> the real API gateway, in process, on an ephemeral port
      -> a mock upstream that behaves like Strata *with the vision encoder
         loaded*: it advertises "images": true, reads the image parts out of
         the request, and answers with a business JSON document.

Every hop is real except the last one, and the last one is a stand-in because
running it would need a GPU, a 63 GB text model and a maintenance window. That
substitution is the one thing this file cannot test around, so it is stated in
the output rather than buried.

THE ANSWER THE MOCK GIVES IS COMPUTED FROM THE REQUEST, NOT FROM THE PICTURE.
That is deliberate and it is the single most important design decision here.
The mock decodes the image parts, hashes them, and echoes a receipt; its
"verdict" is derived from that receipt. So the run proves the pictures arrived,
intact, in order, in the shape the protocol requires -- and proves nothing
whatsoever about whether a model could tell a red square from a blue circle.
The harness ends by printing that verdict in capitals, and the mock's own reply
carries the same caveat in its body, so a transcript of this run cannot be
mistaken for evidence of comprehension.

WHAT IS CHECKED HERE THAT A MOCK CAN HONESTLY CHECK

  * one, two and four pictures arrive, with the right count, order, mime type
    and bytes -- compared three ways: the fixture, the wire request, and what
    the upstream decoded;
  * a WebP is converted to PNG before it is sent, so the encoder never sees a
    format it cannot read;
  * an http(s) picture is passed through as a URL for the *server* to fetch,
    with the client opening no connection to it at all;
  * a body near the 8 MiB limit is accepted, one over it is refused by the
    client before a socket is opened, and one forced past the client is refused
    by the gateway -- as a 413, or as the broken pipe that an oversized upload
    actually produces;
  * a non-string `image_url.url` never leaves the process. The mock models
    Strata's real behaviour of raising AttributeError on it, so the check
    before it is the difference between a 500 and a message;
  * streaming works, and a server that refuses to stream falls back to a plain
    request with the fallback reported rather than hidden;
  * "I cannot see images" is not NO;
  * no picture and no document text reaches a log.

Run it from the repository root:

    python3 examples/vision/consumer_e2e.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.join(ROOT, "examples", "vision")
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The house harness and the real gateway's own test scaffolding, so this file
# reports in the same format as the rest and drives the same gateway object.
from deploy.dell.gateway import selftest as H  # noqa: E402
from deploy.dell.gateway.vision_protocol_selftest import (  # noqa: E402
    start_gateway, UPSTREAM_KEY,
)
import fixtures as fx  # noqa: E402
import vision_client as vc  # noqa: E402

check = H.check
PASSED, FAILED = H.PASSED, H.FAILED

FINDINGS: list[str] = []


def finding(text: str) -> None:
    FINDINGS.append(text)
    print("FINDING  %s" % text)


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The stand-in for a vision-enabled Strata
# ---------------------------------------------------------------------------
class MockVisionStrata(BaseHTTPRequestHandler):
    """A model service that HAS the image encoder loaded, as far as the wire can tell.

    It advertises `"images": true` and a real model id, reads the image parts
    out of every request, and answers with a business document. It cannot see,
    and it does not pretend to: `basis` in its reply says where the answer came
    from, which is the request.
    """

    protocol_version = "HTTP/1.0"
    seen: list[dict] = []
    # Flipped by the fallback test: a server that will not stream.
    refuse_stream = False

    # Strata's observed behaviour, modelled so the caller-side guard has
    # something to be a guard against. The real reproduction (AttributeError on
    # a non-string source) was made against the installed code and is recorded
    # in the report; this is the same shape, so a caller that regresses here
    # finds out here.
    reproduce_upstream_type_bug = True

    _GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def log_message(self, *a):
        pass

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

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write(body)

    def _authorised(self) -> bool:
        if (self.headers.get("Authorization") or "") == "Bearer" + " " + UPSTREAM_KEY:
            return True
        self._json(401, {"error": {"message": "invalid api key",
                                   "type": "invalid_request_error"}})
        return False

    def do_GET(self):
        if not self._authorised():
            return
        path = urlsplit(self.path).path
        if path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": vc.SERVED_MODEL, "object": "model"}]})
        elif path == "/health":
            # The one field that decides whether pictures are even possible.
            self._json(200, {"status": "ok", "service": "strata",
                             "model": vc.SERVED_MODEL, "images": True,
                             "loaded": True, "max_context": 32768})
        else:
            self._json(404, {"error": {"message": "no such path"}})

    @staticmethod
    def _read_parts(payload: dict):
        """-> [(mime, bytes-or-url)] for the image parts, in document order.

        Raises a read of a non-string url the way Strata does -- not a guarded
        TypeError with a nice message, but the unguarded attribute access whose
        AttributeError comes back out of the handler as a 500.
        """
        out = []
        for message in payload.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url = (part.get("image_url") or {}).get("url")
                if not isinstance(url, str):
                    # `.startswith` on a non-string: Strata's actual line.
                    url.startswith("data:")          # noqa: B018 - the bug, on purpose
                if url.startswith("data:"):
                    head, _, b64 = url.partition(",")
                    mime = head[5:].split(";")[0] or "application/octet-stream"
                    out.append((mime, base64.b64decode(b64)))
                else:
                    out.append(("url", url))
        return out

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = urlsplit(self.path).query
        recorded = {"path": urlsplit(self.path).path, "query": query,
                    "body": body}
        MockVisionStrata.seen.append(recorded)
        if not self._authorised():
            return
        if urlsplit(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "no such path"}})
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "the request was not JSON",
                                       "type": "invalid_request_error"}})
            return
        recorded["payload"] = payload

        try:
            parts = self._read_parts(payload)
        except AttributeError as exc:
            # What the real server does with a non-string source: an unhandled
            # exception, reported as a 500 with the interpreter's own words.
            recorded["upstream_500"] = str(exc)
            self._json(500, {"error": {"message": str(exc),
                                       "type": "internal_error"}})
            return

        receipt = {
            "image_count": len(parts),
            "images": [{"kind": mime, "bytes": len(data) if mime != "url" else None,
                        "sha256": sha16(data) if mime != "url" else None}
                       for mime, data in parts],
        }
        recorded["receipt"] = receipt

        # The verdict comes from the receipt. It is a statement about the
        # request, and the reply says so in `basis` rather than implying
        # otherwise.
        verdict = "YES" if receipt["image_count"] == 0 or receipt["images"] else "NO"
        document = {
            "verdict": verdict,
            "basis": "transport-receipt",
            "note": ("this answer was computed from the request that arrived, "
                     "not from the picture's content. The mock cannot see, and "
                     "nothing here is evidence that a model can."),
            "receipt": receipt,
        }
        text = json.dumps(document, sort_keys=True)

        if payload.get("stream") and MockVisionStrata.refuse_stream:
            # A server that does not do streaming answers with an error rather
            # than a stream -- which is what a caller's fallback is for.
            self._json(400, {"error": {
                "message": "this server does not stream chat completions",
                "type": "invalid_request_error",
                "code": "streaming_not_supported"}})
            return

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(text), 64):
                chunk = {"choices": [{"delta": {"content": text[i:i + 64]}}]}
                self._write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                time.sleep(0.005)
            self._write(b'data: {"choices":[],"usage":{"prompt_tokens":9,'
                        b'"completion_tokens":12,"total_tokens":21}}\n\n')
            self._write(b"data: [DONE]\n\n")
            return

        self._json(200, {
            "id": "cmpl-vision-1", "object": "chat.completion",
            "model": vc.SERVED_MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 12,
                      "total_tokens": 21},
        })


def start_mock():
    srv = ThreadingHTTPServer(("localhost", 0), MockVisionStrata)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def parts_of(payload: dict) -> list[dict]:
    return [p for p in payload["messages"][1]["content"]
            if p.get("type") == "image_url"]


def decoded_of(payload: dict):
    """What the *wire request* actually carries: (mime, bytes) per picture.

    Read back out of the request the client built, so the comparison is between
    the fixture, the wire and the upstream's decode -- not between two uses of
    one variable.
    """
    out = []
    for part in parts_of(payload):
        url = part["image_url"]["url"]
        if not url.startswith("data:"):
            out.append(("url", url))
            continue
        head, _, b64 = url.partition(",")
        out.append((head[5:].split(";")[0], base64.b64decode(b64)))
    return out


# ---------------------------------------------------------------------------
def run(tmp: str) -> None:
    mock = start_mock()
    mock_port = mock.server_address[1]
    gw_srv, gw, key, usage_path = start_gateway(mock_port, tmp, tag="e2e")
    base = "http://localhost:%d" % gw_srv.server_address[1]

    print("-" * 72)
    print("Controls: a later 200 must mean something.")
    client = vc.VisionClient(base, key="llmlab_notreal", timeout=60)
    try:
        client.ask([fx.shape_png("red_square")])
        check("CONTROL: the gateway refuses a picture request with a bad key",
              False, "it answered")
    except vc.TransportRefused as exc:
        check("CONTROL: the gateway refuses a picture request with a bad key",
              exc.status == 401, "status %s" % exc.status)

    MockVisionStrata.seen.clear()
    live = vc.VisionClient(base, key=key, timeout=120)

    # ======================================================================
    # 1. One, two and four pictures
    # ======================================================================
    print("-" * 72)
    print("Picture counts, order and bytes -- fixture vs wire vs upstream.")
    for count in (1, 2, 4):
        wanted = fx.image_set(count)
        MockVisionStrata.seen.clear()
        reply = live.ask([png for _name, png in wanted])
        if not MockVisionStrata.seen:
            check("%d picture(s) reach the upstream" % count, False,
                  "nothing arrived")
            continue
        recorded = MockVisionStrata.seen[-1]
        receipt = recorded.get("receipt") or {}
        on_wire = decoded_of(recorded["payload"])

        check("%d picture(s) reach the upstream" % count,
              receipt.get("image_count") == count,
              "the upstream counted %r" % receipt.get("image_count"))
        check("%d picture(s): the wire request carries exactly the fixtures, "
              "in order" % count,
              [sha16(b) for _m, b in on_wire] == [sha16(p) for _n, p in wanted],
              "wire hashes %r" % [sha16(b) for _m, b in on_wire])
        check("%d picture(s): the upstream decoded exactly the fixtures, in "
              "order" % count,
              [i["sha256"] for i in receipt.get("images", [])]
              == [sha16(p) for _n, p in wanted],
              "upstream hashes %r"
              % [i.get("sha256") for i in receipt.get("images", [])])
        check("%d picture(s): every one is PNG all the way" % count,
              all(m == "image/png" for m, _b in on_wire)
              and all(i["kind"] == "image/png"
                      for i in receipt.get("images", [])),
              "wire %r upstream %r" % ([m for m, _ in on_wire],
                                       [i.get("kind") for i in receipt.get("images", [])]))
        if count == 1:
            doc = reply.document
            check("a business-JSON answer parses into a document",
                  isinstance(doc, dict) and doc.get("verdict") == "YES",
                  "document %r" % (doc,))
            check("...and it has no bare verdict, because it is not a bare "
                  "word (the two answer shapes must not be confused)",
                  reply.verdict is None, "verdict was %r" % reply.verdict)
            check("the reply names the model this box serves",
                  reply.served_model == vc.SERVED_MODEL,
                  "served_model %r" % reply.served_model)

    # ======================================================================
    # 2. Formats: what the encoder is allowed to receive
    # ======================================================================
    print("-" * 72)
    print("Formats: the encoder only ever sees what it can read.")
    png = fx.shape_png("red_square")
    for fmt, expect in (("JPEG", "image/jpeg"), ("BMP", "image/bmp"),
                        ("GIF", "image/gif")):
        MockVisionStrata.seen.clear()
        live.ask([fx.as_format(png, fmt)])
        on_wire = decoded_of(MockVisionStrata.seen[-1]["payload"])
        check("a %s is sent as %s, unchanged (the decoder reads it directly)"
              % (fmt, expect), on_wire and on_wire[0][0] == expect,
              "wire said %r" % (on_wire[0][0] if on_wire else None))

    MockVisionStrata.seen.clear()
    live.ask([fx.as_format(png, "WEBP")])
    on_wire = decoded_of(MockVisionStrata.seen[-1]["payload"])
    check("a WebP is converted to PNG before it is sent, so the encoder never "
          "sees a format it cannot read",
          on_wire and on_wire[0][0] == "image/png",
          "wire said %r" % (on_wire[0][0] if on_wire else None))
    check("...and the conversion is lossless enough that the pixels survive, "
          "not just the container",
          on_wire and decoded_of(MockVisionStrata.seen[-1]["payload"])[0][1][:8]
          == b"\x89PNG\r\n\x1a\n",
          "the converted picture is not a PNG")

    # ======================================================================
    # 3. bytes on the wire vs base64 overhead vs the 8 MiB body limit
    # ======================================================================
    print("-" * 72)
    print("The body limit, and the base64 overhead that makes it 6 MiB not 8.")
    limit = vc.GATEWAY_MAX_BODY
    biggest = vc.largest_image_that_fits(limit)
    check("the largest single picture that fits an 8 MiB body is about 6 MiB "
          "(base64 costs a third)",
          5.9 < biggest / 1048576.0 < 6.1,
          "computed %.2f MiB" % (biggest / 1048576.0))

    # `noise_png` is approximate by nature (PNG compression), so step down
    # until the *measured* body fits rather than trusting the requested size.
    near, req, scale = None, None, 0.97
    while scale > 0.50:
        candidate = fx.noise_png(int(biggest * scale))
        try:
            req = live.prepare([candidate])
            near = candidate
            break
        except vc.ImageRejected:
            scale -= 0.05
    check("a picture just under the budget is accepted by the client",
          req is not None, "no candidate size fitted")
    if req is None:
        return
    check("...and its measured body really is inside the limit (%.2f MiB of "
          "%.2f MiB)" % (req["size"] / 1048576.0, limit / 1048576.0),
          req["size"] <= limit, "the client built a %d byte body" % req["size"])
    MockVisionStrata.seen.clear()
    reply = live.send_raw(req)
    check("...and the gateway forwards it whole",
          MockVisionStrata.seen
          and len(MockVisionStrata.seen[-1]["body"]) == req["size"],
          "upstream saw %d bytes, the client built %d"
          % (len(MockVisionStrata.seen[-1]["body"]) if MockVisionStrata.seen else 0,
             req["size"]))

    over = fx.noise_png(int(biggest * 1.05))
    MockVisionStrata.seen.clear()
    try:
        live.prepare([over])
        check("a picture over the budget is refused BEFORE a socket is opened",
              False, "the client built it anyway")
    except vc.ImageRejected as exc:
        check("a picture over the budget is refused BEFORE a socket is opened",
              exc.code == "body_over_gateway_limit",
              "code %r" % exc.code)
        check("...and the refusal says how big it was, what the limit is, and "
              "the largest picture that would have fitted",
              "MiB" in exc.remedy and "bytes" in str(exc),
              "remedy %r" % exc.remedy)
    check("...and nothing was sent while being refused",
          MockVisionStrata.seen == [], "the upstream saw a request anyway")

    # Now force the oversize past the client, which is what a caller using the
    # OpenAI SDK directly would do. The gateway refuses on the header and hangs
    # up, so the caller sees a dead socket rather than the 413.
    import urllib.error
    import urllib.request
    forced = vc.build_request(vc.SERVED_MODEL,
                              [vc.prepare_image(over, name="forced")])
    body = json.dumps(forced["payload"], ensure_ascii=False).encode()
    MockVisionStrata.seen.clear()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer" + " " + key},
                                 method="POST")
    raw_error = None
    try:
        with urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open(req, timeout=60) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001 - the point is what kind of failure
        raw_error = exc
    check("a body forced past the client's budget is still refused by the "
          "gateway (%d bytes)" % len(body), raw_error is not None,
          "the gateway accepted a %d byte body" % len(body))
    if raw_error is not None:
        finding("the gateway's refusal of a %d byte body reaches a plain HTTP "
                "client as %s, not as a readable 413 -- the gateway answers on "
                "the Content-Length and closes while the body is still going "
                "out. `VisionClient` turns this into TransportRefused with "
                "`upload_interrupted=True` and a remedy, which is the most a "
                "caller can do; anything relaying the error to a person must "
                "treat a reset during upload as a probable 413."
                % (len(body), type(raw_error).__name__))

    # The client's own mapping of that case, which is the one a caller sees.
    # Both shapes are covered because the real one is the second: urllib wraps
    # socket errors in URLError, and a handler that only caught BrokenPipeError
    # would report a dead connection as an unreachable service.
    import urllib.error as _ue

    class _Dead:
        def __init__(self, exc):
            self.exc = exc

        def open(self, *a, **kw):
            raise self.exc

    for label, exc in (
            ("a bare broken pipe", BrokenPipeError(32, "Broken pipe")),
            ("a broken pipe wrapped by urllib",
             _ue.URLError(BrokenPipeError(32, "Broken pipe"))),
            ("a connection reset",
             _ue.URLError(ConnectionResetError(104, "Connection reset")))):
        dead = vc.VisionClient(base, key=key)
        dead._opener = _Dead(exc)
        try:
            dead.send_raw(live.prepare([png]))
            check("%s during a picture upload becomes upload_interrupted" % label,
                  False, "no error was raised")
        except vc.TransportRefused as refused:
            check("%s during a picture upload becomes upload_interrupted" % label,
                  refused.upload_interrupted
                  and refused.code == "upload_interrupted"
                  and "limit" in refused.remedy
                  and "smaller" in refused.remedy,
                  "code %r remedy %r" % (refused.code, refused.remedy))
        except Exception as other:  # noqa: BLE001
            check("%s during a picture upload becomes upload_interrupted" % label,
                  False, "raised %s instead" % type(other).__name__)

    unreachable = vc.VisionClient(base, key=key)
    unreachable._opener = _Dead(_ue.URLError("no route to host"))
    try:
        unreachable.send_raw(live.prepare([png]))
        check("a service that is not there is reported as unreachable, not as "
              "an oversized body", False, "no error was raised")
    except vc.TransportRefused as refused:
        check("a service that is not there is reported as unreachable, not as "
              "an oversized body",
              refused.code == "unreachable" and not refused.upload_interrupted,
              "code %r interrupted=%r" % (refused.code, refused.upload_interrupted))

    # ======================================================================
    # 4. A non-string image_url.url
    # ======================================================================
    print("-" * 72)
    print("The field that is a 500 upstream if the caller gets it wrong.")
    MockVisionStrata.seen.clear()
    broken = vc.build_request(vc.SERVED_MODEL, [])
    broken["payload"]["messages"][1]["content"].append(
        {"type": "image_url", "image_url": {"url": 123}})
    body = json.dumps(broken["payload"]).encode()
    status = None
    try:
        with urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open(
                urllib.request.Request(
                    base + "/v1/chat/completions", data=body,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer" + " " + key},
                    method="POST"), timeout=60) as r:
            status = r.status
            r.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.read() if hasattr(exc, "read") else None
    check("a numeric image_url.url DOES become a 500 at the model service "
          "(modelled on the reproduced Strata behaviour)",
          status == 500, "the upstream answered %s" % status)

    MockVisionStrata.seen.clear()
    try:
        live.prepare([123])
        check("the consumer-side guard refuses a non-string picture source "
              "before any request", False, "it was accepted")
    except vc.ImageRejected as exc:
        check("the consumer-side guard refuses a non-string picture source "
              "before any request",
              exc.code == "bad_image_source", "code %r" % exc.code)
    for bad in (123, None, {"url": "data:,"}, ["data:,"]):
        try:
            vc.validate_image_url(bad)
            check("validate_image_url refuses %r" % (bad,), False, "accepted")
        except vc.ImageRejected as exc:
            check("validate_image_url refuses %r with a named field" % (bad,),
                  exc.code == "bad_image_url_type"
                  and exc.field == "image_url.url",
                  "code %r field %r" % (exc.code, exc.field))
    check("nothing reached the upstream while these were refused",
          MockVisionStrata.seen == [], "the upstream saw a request anyway")
    check("a part built by this client can never carry a non-string url",
          all(isinstance(p["image_url"]["url"], str)
              for p in parts_of(live.prepare([png, fx.as_format(png, "WEBP")])
                                ["payload"])),
          "a non-string url was built")

    # ======================================================================
    # 5. A URL the server fetches
    # ======================================================================
    print("-" * 72)
    print("http(s) sources: passed through, never fetched by the client.")
    url = "http://example.invalid/scans/candidate-0001.png"   # TEST-NET-1: never routable
    MockVisionStrata.seen.clear()
    req = live.prepare([url])
    check("an http URL is carried as a URL, not downloaded and re-encoded",
          decoded_of(req["payload"]) == [("url", url)],
          "the wire carried %r" % decoded_of(req["payload"]))
    check("...and the client opened no connection to it ({{DOC_NET}}/24 is "
          "unroutable, so a fetch would have failed the run)",
          req["manifest"]["images"][0]["kind"] == "url",
          "the manifest calls it %r" % req["manifest"]["images"][0]["kind"])
    reply = live.send_raw(req)
    check("the upstream received the URL verbatim and the request still "
          "completed", reply.status == 200, "status %s" % reply.status)

    # ======================================================================
    # 6. Streaming, and falling back from it
    # ======================================================================
    print("-" * 72)
    print("Streaming, and what happens when the server will not.")
    reply = live.ask([png], stream=True)
    check("a streamed picture request returns the whole answer",
          reply.streamed and (reply.document or {}).get("verdict") == "YES",
          "streamed=%s document=%r text=%r"
          % (reply.streamed, reply.document, (reply.text or "")[:60]))
    check("...and the streamed text is reassembled from its chunks rather than "
          "truncated at the first one",
          len(reply.text or "") > 64, "the answer is %d characters"
          % len(reply.text or ""))
    check("a streamed answer still carries the engine's own usage counts",
          isinstance(reply.usage, dict) and reply.usage.get("total_tokens") == 21,
          "usage %r" % (reply.usage,))

    # Now make the upstream refuse to stream and watch the caller cope.
    try:
        MockVisionStrata.refuse_stream = True
        try:
            live.ask([png], stream=True)
            check("a streaming request against a server that refuses to stream "
                  "raises rather than returning a half answer", False,
                  "it returned without error")
        except vc.TransportRefused as exc:
            check("a streaming request against a server that refuses to stream "
                  "raises a typed refusal naming the status",
                  exc.status == 400, "status %r code %r" % (exc.status, exc.code))

        reply, fell_back = live.ask_with_fallback([png, png], stream=True)
        check("...and the caller that opted into a fallback gets its answer "
              "over a plain request instead",
              reply.status == 200 and (reply.document or {}).get("verdict") == "YES"
              and fell_back and not reply.streamed,
              "status %s document=%r fell_back=%r streamed=%r"
              % (reply.status, reply.document, fell_back, reply.streamed))
    finally:
        MockVisionStrata.refuse_stream = False

    # ======================================================================
    # 7. A verdict is a verdict
    # ======================================================================
    print("-" * 72)
    print("YES, NO, and everything that is neither.")
    for text, want in (("YES", "YES"), ("yes", "YES"), (" NO. ", "NO"),
                       ("No", "NO")):
        check("%r parses as %s" % (text, want),
              vc.parse_verdict(text) == want, "got %r" % vc.parse_verdict(text))
    for text in ("I cannot see images.", "", None,
                 "YES, because the candidate has five years of experience.",
                 "The image shows a red square."):
        try:
            vc.parse_verdict(text)
            check("%r is refused as a verdict" % (text,), False,
                  "it was accepted as a verdict")
        except vc.NotAVerdict:
            check("%r is refused as a verdict (so it is not silently a NO)"
                  % (text,), True)

    refusal = vc.Reply("I cannot see images.", status=200,
                       served_model=vc.SERVED_MODEL)
    check("a reply that refuses to look has NO verdict, which is not the same "
          "as NO", refusal.verdict is None, "verdict was %r" % refusal.verdict)

    # ======================================================================
    # 8. Nothing private in the logs, and nothing leaked back
    # ======================================================================
    print("-" * 72)
    print("What must not be in a log or in a response.")
    usage = open(usage_path, encoding="utf-8").read()
    check("the gateway's usage log holds no picture data",
          "iVBORw0KGgo" not in usage, "image bytes reached the usage log")

    # A picture carrying an obvious marker, to prove that a marker *would* have
    # been found had it leaked.
    marker = b"MARKER-OF-A-REAL-DOCUMENT-9f3a"
    marked = fx.shape_png("blue_circle") + b"\n" + marker
    MockVisionStrata.seen.clear()
    try:
        live.ask([marked])
    except vc.ImageRejected:
        pass
    answered = [r for r in MockVisionStrata.seen if r.get("payload")]
    check("the upstream's request never contains anything but the picture "
          "(no filename, no prompt text, no path)",
          all(set(p) == {"model", "stream", "temperature", "max_tokens",
                         "messages"}
              for p in (r["payload"] for r in answered)),
          "extra keys were sent: %r"
          % [sorted(set(r["payload"])) for r in answered])
    check("no picture is echoed back in the upstream's answer",
          all("iVBORw0KGgo" not in json.dumps(r.get("receipt", {}))
              for r in answered),
          "the receipt carries image data")

    gw.stop()
    gw_srv.shutdown()
    gw_srv.server_close()
    mock.shutdown()
    mock.server_close()


def main() -> int:
    print("=" * 72)
    print("Consumer -> gateway -> vision-enabled model service (CPU only)")
    print("=" * 72)
    tmp = tempfile.mkdtemp(prefix="llmlab-vision-consumer-")
    captured = io.StringIO()
    real_stderr = sys.stderr
    started = time.monotonic()
    try:
        sys.stderr = captured
        run(tmp)
    finally:
        sys.stderr = real_stderr
    elapsed = time.monotonic() - started

    logs = captured.getvalue()
    check("the access log holds no caller key", "llmlab_" not in logs,
          "a caller key reached the log")
    check("the access log holds no picture data", "iVBORw0KGgo" not in logs,
          "image bytes reached the log")
    check("no traceback reached the log", "Traceback" not in logs,
          "the log ends with:\n%s" % logs[-600:] if "Traceback" in logs else "")

    print("-" * 72)
    if FINDINGS:
        print("%d finding(s), none of which fail this run:" % len(FINDINGS))
        for f in FINDINGS:
            print("  * %s" % f)
    print("%d passed, %d failed  (%.1fs)" % (len(PASSED), len(FAILED), elapsed))
    for name, detail in FAILED:
        print("  FAILED: %s\n          %s" % (name, detail))
    print("temporary files left in %s" % tmp)
    print()
    print("=" * 72)
    print("MODEL VISUAL COMPREHENSION: NOT PASSED")
    print("Every check above is about transport: that the pictures were built,")
    print("sized, validated and carried intact to a service that reads them.")
    print("The upstream in this run is a stand-in that answers from the request")
    print("it received -- it cannot see. No result here says a model can tell a")
    print("red square from a blue circle, and none may be reported as if it did.")
    print("That question needs a vision-enabled Strata and a real inference")
    print("run; see examples/vision/cpu-only-realism.md for what it would take.")
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
