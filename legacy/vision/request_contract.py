#!/usr/bin/env python3
"""The image half of the request contract, checked against Strata's own code.

WHAT THIS IS.  Strata's served image path has four separable pieces:

    1. the OpenAI `image_url` / Anthropic `image` part  -> a source string,
       done by `serve.frontend.openai_to_messages` + `images_of`;
    2. that source string -> bytes, done by `Vision.load` / `Vision.download`;
    3. arbitrary bytes -> something the encoder's decoder can read, done by
       `Vision.normalize`;
    4. bytes -> embeddings, done by the `strata-vision` process.

Steps 1-3 are pure Python.  They are the whole of what a request has to get
right before any GPU is involved, they are where every preventable 400 lives,
and none of them needs a model.  So they are checked here, by importing
Strata's real module and calling its real functions.

THIS IS NOT A MOCK OF STRATA.  It imports `serve/server.py` and `serve/frontend.py`
from an installed Strata and calls the functions a live request calls.  A test
that re-implemented them would only prove the re-implementation agrees with
itself.

WHAT THIS DELIBERATELY DOES NOT DO:

  * no GPU, no CUDA, no `nvidia-smi` -- and it is not merely "not needed": the
    encoder's launcher is replaced with a tripwire, so if any code path here
    tried to start `strata-vision` the run would fail loudly instead of quietly
    doing it;
  * no model weights are opened;
  * no request is sent to a running server -- not 8080, not 8200, nothing;
  * the only socket opened is to a mock HTTP server on localhost that this file
    starts and stops itself, because `Vision.download`'s size cap is a real
    behaviour and reading its source is not the same as running it;
  * nothing is written under Strata's tree.  `sys.dont_write_bytecode` is set
    before the import so no `.pyc` is created next to the installed code.

WHAT IT DOES NOT PROVE, AND MUST NOT BE READ AS PROVING.  Step 4 is untested
here, so *nothing below is evidence that the model can answer a question about
a picture*.  These checks say a request with a picture in it is *accepted and
turned into bytes*.  Whether the answer is any good needs the image encoder and
the model, i.e. a GPU and a maintenance window.  A green run of this file is a
statement about the request contract, not about answer quality.

RUN IT:

    python3 examples/vision/request_contract.py
    STRATA_SERVE_ROOT={{STRATA_ROOT}} python3 examples/vision/request_contract.py

Without an installed Strata it reports SKIP for the contract checks and still
exits 0 -- the file is meant to be runnable on the Windows workstation, where
there is no Strata at all.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Windows console is GBK by default; one non-GBK byte in a detail string turns a
# reportable failure into a UnicodeEncodeError. Same fix as the repo's selftests.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[str] = []

# Findings are behaviours that are *real and reproducible* but which the brief
# did not ask about, and which no assertion below should silently bless. They are
# printed loudly and do not fail the run; see GAPS in the report.
FINDINGS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """House style: record, never raise. One run reports everything wrong."""
    if ok:
        PASSED.append(name)
        print("PASS  %s" % name)
    else:
        FAILED.append((name, detail))
        print("FAIL  %s%s" % (name, ("  -- " + detail) if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print("SKIP  %s  -- %s" % (name, why))


def finding(text: str) -> None:
    FINDINGS.append(text)
    print("FINDING  %s" % text)


def raises(fn, *a, **kw):
    """-> (exception or None, is_ValueError). The contract's failures are 400s,
    so 'it raised' is not enough: an AttributeError is a 500."""
    try:
        fn(*a, **kw)
    except BaseException as exc:  # noqa: BLE001 - reporting, not handling
        return exc, isinstance(exc, ValueError)
    return None, False


# ---------------------------------------------------------------------------
# The local-only download endpoint
# ---------------------------------------------------------------------------
class _ImageHost(BaseHTTPRequestHandler):
    """Serves the fixtures over localhost so `Vision.download`'s cap is measured.

    Loopback, a port the OS picks, started and stopped by this file. It never
    reaches the LAN, and it is not the model service.
    """

    protocol_version = "HTTP/1.0"
    body: bytes = b""
    cap: int = 0
    # A client that gives up mid-body is normal traffic, and the mock is held to
    # the same rule as the code under test: no traceback on a hang-up.
    _GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    def log_message(self, *a):   # silence: this is not the thing under test
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except self._GONE:
            self.close_connection = True

    def _reply(self, status: int, length):
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self):
        try:
            if self.path == "/ok":
                self._reply(200, len(self.body))
                self.wfile.write(self.body)
            elif self.path == "/lying":
                # Claims more than the cap, sends almost nothing. The cap has to
                # be enforced on the announced length, or a caller can make the
                # server allocate a declared 4 GB.
                self._reply(200, self.cap + 1)
                self.wfile.write(self.body)
            elif self.path == "/oversize":
                n = self.cap + 1024
                self._reply(200, n)
                chunk = b"\0" * 65536
                sent = 0
                while sent < n:
                    self.wfile.write(chunk[:min(65536, n - sent)])
                    sent += min(65536, n - sent)
            elif self.path == "/slow":
                # Sends a valid Content-Length, then nothing, then hangs up.
                self._reply(200, 1024)
                self.wfile.flush()
            else:
                self._reply(404, 0)
        except self._GONE:
            self.close_connection = True


def start_image_host(body: bytes, cap: int):
    _ImageHost.body, _ImageHost.cap = body, cap
    srv = ThreadingHTTPServer(("localhost", 0), _ImageHost)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://localhost:%d" % srv.server_address[1]


# ---------------------------------------------------------------------------
# Import Strata, or do not
# ---------------------------------------------------------------------------
def load_strata():
    """-> (server_mod, frontend_mod, fixtures) or (None, None, fixtures).

    `sys.dont_write_bytecode` is set first: importing the installed module must
    not leave a `.pyc` in its directory. Strata's own tree is a read-only input
    here, and a test that quietly writes into it is not read-only whatever it
    says at the top.
    """
    root = os.environ.get("STRATA_SERVE_ROOT", "{{STRATA_ROOT}}")
    if not os.path.isfile(os.path.join(root, "serve", "server.py")):
        return None, None, root
    sys.dont_write_bytecode = True
    if root not in sys.path:
        sys.path.insert(0, root)
    import serve.server as server_mod          # noqa: PLC0415 - optional dependency
    from serve import frontend as frontend_mod  # noqa: PLC0415
    return server_mod, frontend_mod, root


def build_fixtures(tmp: str) -> dict:
    from make_synthetic_image import make_all  # noqa: PLC0415

    make_all(tmp)
    out = {}
    for name in ("red_square.png", "blue_circle.png", "photo.webp"):
        p = os.path.join(tmp, name)
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                out[name] = fh.read()
    return out


# ---------------------------------------------------------------------------
# 1. The OpenAI content array -> sources, and its order
# ---------------------------------------------------------------------------
def check_part_parsing(frontend, fixtures):
    png = fixtures["red_square.png"]
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()

    req = {"model": "m", "messages": [
        {"role": "system", "content": "be a strict screener"},
        {"role": "user", "content": [
            {"type": "text", "text": "Is the large central shape red?"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]}
    messages, _tools, _opts = frontend.openai_to_messages(req)

    check("openai_to_messages keeps the text part and the image part of one message",
          messages[0]["role"] == "system" and messages[1]["role"] == "user",
          "roles came back as %r" % [m.get("role") for m in messages])

    parts = messages[1]["content"]
    check("the content array stays an array, in the order the caller wrote it",
          isinstance(parts, list) and [p.get("type") for p in parts] == ["text", "image"],
          "parts came back as %r" % (parts,))
    check("the text of a mixed message is not merged into the image part",
          parts[0].get("text") == "Is the large central shape red?" if isinstance(parts, list) else False,
          "text came back as %r" % (parts[0] if isinstance(parts, list) else parts,))

    sources = frontend.images_of(messages)
    check("images_of finds exactly the one image, as its source string",
          sources == [data_url],
          "got %r" % (sources if len(str(sources)) < 200 else [s[:40] for s in sources],))

    # Two images in one message, and one in a later message: order is what a
    # caller depends on, because the prompt's <|image_pad|> placeholders are
    # filled by position.
    two = "data:image/png;base64," + base64.b64encode(fixtures["blue_circle.png"]).decode()
    req2 = {"messages": [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": "and this one?"},
            {"type": "image_url", "image_url": {"url": two}},
        ]},
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_url}}]},
    ]}
    m2, _t, _o = frontend.openai_to_messages(req2)
    got = frontend.images_of(m2)
    check("images are collected in document order across parts and messages",
          got == [data_url, two, data_url],
          "expected [red, blue, red]; got %r" % ([s[:28] for s in got],))

    m3, _t, _o = frontend.openai_to_messages(
        {"messages": [{"role": "user", "content": "just words"}]})
    check("a plain string message yields no images and is left alone",
          frontend.images_of(m3) == [] and m3[0]["content"] == "just words",
          "%r / %r" % (frontend.images_of(m3), m3[0]["content"]))


# ---------------------------------------------------------------------------
# 2. source string -> bytes
# ---------------------------------------------------------------------------
def check_source_to_bytes(server, fixtures, tmp):
    V = server.Vision
    png = fixtures["red_square.png"]
    path = os.path.join(tmp, "red_square.png")

    got = V.load("data:image/png;base64," + base64.b64encode(png).decode())
    check("a data: URL is decoded to exactly the bytes that were encoded", got == png,
          "%d bytes back, %d sent" % (len(got), len(png)))
    check("a bare local path is read", V.load(path) == png,
          "reading %s did not give the fixture back" % path)
    check("file:///path is the same file, with the prefix stripped",
          V.load("file://" + path) == png, "the file:// spelling disagreed")

    # Negative controls for the source string. These are the 400s a caller can
    # actually provoke, and they are checked before anything positive below.
    refused = [
        ("file://host/share/x.png", "a file:// URL naming another host (UNC)"),
        ("//host/share/x.png", "a //host/share network path"),
        ("\\\\host\\share\\x.png", "a \\\\host\\share network path"),
        ("ftp://example.invalid/a.png", "a scheme that is not data/http/https/file"),
        (os.path.join(tmp, "does-not-exist.png"), "a local path with nothing at it"),
    ]
    bad = []
    for source, why in refused:
        exc, is_value = raises(V.load, source)
        if exc is None:
            bad.append("%s was ACCEPTED" % why)
        elif not is_value:
            bad.append("%s raised %s, not ValueError (a 500, not a 400)"
                       % (why, type(exc).__name__))
    check("every unreadable image source is refused as a ValueError (i.e. a 400)",
          not bad, "; ".join(bad))

    # A hole worth recording rather than asserting on: the source is expected to
    # be a string, and nothing in front of it checks.
    exc, is_value = raises(V.load, 123)
    if exc is not None and not is_value:
        finding("a non-string image source ({\"image_url\": {\"url\": 123}}) is not "
                "rejected before use: Vision.load raises %s, which is an unhandled "
                "error rather than the ValueError the rest of this path returns. "
                "Gateways and clients can both send it." % type(exc).__name__)

    # base64 with validate=False silently drops non-alphabet characters, so a
    # mangled data URL becomes an empty image instead of an error.
    exc, _ = raises(V.load, "data:image/png;base64,###@@@!!!")
    if exc is None:
        finding("a data: URL whose payload is not base64 at all "
                "(data:image/png;base64,###@@@!!!) decodes to zero bytes without "
                "raising; the failure surfaces later, as a 400 from the encoder or "
                "as an empty picture, not at the point the caller got it wrong")


def check_download(server, fixtures, tmp):
    V = server.Vision
    cap = server.IMAGE_URL_MAX
    check("the download cap is the 32 MiB the source comment claims",
          cap == 32 * 1024 * 1024, "IMAGE_URL_MAX is %d" % cap)

    srv, base = start_image_host(fixtures["red_square.png"], cap)
    try:
        got = V.download(base + "/ok")
        check("an http URL on the loopback mock is downloaded as its bytes",
              got == fixtures["red_square.png"], "%d bytes back" % len(got))

        exc, is_value = raises(V.download, base + "/lying")
        check("a server that *announces* more than the cap is refused before the body is read",
              is_value and "32 MiB" in str(exc),
              "got %r" % (exc,))

        exc, is_value = raises(V.download, base + "/oversize")
        check("a server that really sends more than the cap is refused too",
              is_value and "32 MiB" in str(exc), "got %r" % (exc,))

        exc, is_value = raises(V.download, base + "/nope")
        check("an http error from the image host is a ValueError, not a traceback",
              is_value, "got %r" % (exc,))
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# 3. bytes -> bytes the decoder can read
# ---------------------------------------------------------------------------
def check_normalize(server, fixtures):
    V = server.Vision
    png = fixtures["red_square.png"]

    check("a PNG is passed through byte-for-byte (no needless re-encode)",
          V.normalize(png) == png, "the PNG came back changed")

    for name in ("JPEG", "BMP", "GIF"):
        from PIL import Image  # noqa: PLC0415
        buf = io.BytesIO()
        with Image.open(io.BytesIO(png)) as im:
            im.convert("RGB").save(buf, name)
        data = buf.getvalue()
        check("a %s is passed through untouched as well" % name,
              V.normalize(data) == data, "%s was re-encoded" % name)

    if "photo.webp" in fixtures:
        out = V.normalize(fixtures["photo.webp"])
        check("a WebP (which the decoder cannot read) is converted to PNG",
              out[:8] == b"\x89PNG\r\n\x1a\n",
              "WebP came back with magic %r" % out[:8])
        check("the converted WebP is a real picture, not an empty file",
              len(out) > 100, "%d bytes" % len(out))
    else:
        skip("WebP -> PNG conversion", "this Pillow has no WebP encoder")

    exc, is_value = raises(V.normalize, b"this is not an image")
    check("bytes that are not an image are refused as a ValueError",
          is_value, "got %r" % (exc,))

    exc, is_value = raises(V.normalize, b"")
    check("empty bytes are refused as a ValueError",
          is_value, "got %r" % (exc,))


def check_exif(server, fixtures, tmp):
    V = server.Vision
    check("a PNG has no orientation tag to apply",
          V._exif_rotated(fixtures["red_square.png"]) is False,
          "a plain PNG was reported as rotated")
    jpg_path = os.path.join(tmp, "portrait_exif.jpg")
    if not os.path.isfile(jpg_path):
        skip("EXIF orientation detection", "portrait_exif.jpg was not generated")
        return
    with open(jpg_path, "rb") as fh:
        data = fh.read()
    check("a JPEG tagged Orientation=6 is recognised as stored rotated",
          V._exif_rotated(data) is True, "the rotated fixture was not detected")


# ---------------------------------------------------------------------------
# 3b. The shipped samples, filled in and parsed
# ---------------------------------------------------------------------------
# Read off the running box (`GET /health` on Strata, and the model list the
# gateway reports). The samples used to say `qwen3.8-flash-next`, which nothing
# serves; keeping the real id here is what stops that regressing.
SERVED_MODEL = "qwen3.8-flash-next-iq2_xs"


def check_samples(frontend, fixtures, tmp):
    png = fixtures["red_square.png"]
    png_path = os.path.join(tmp, "red_square.png")

    for name in ("chat_yesno.openai.json", "chat_yesno.anthropic.json"):
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            skip("%s parses and names the served model" % name, "not present")
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        try:
            sample = json.loads(text)
        except ValueError as exc:
            check("%s is valid JSON" % name, False, str(exc))
            continue
        check("%s is valid JSON" % name, True)
        check("%s names the model this box actually serves (%s)"
              % (name, SERVED_MODEL),
              sample.get("model") == SERVED_MODEL,
              "the sample asks for %r" % sample.get("model"))
        # The *request* must not reference it. The prose above it may -- saying
        # what was wrong is the point of the comment, and a check that scanned
        # the whole file would forbid explaining the fix.
        request_part = {k: v for k, v in sample.items() if not k.startswith("_")}
        check("%s no longer requests the picture that was never in the "
              "repository" % name,
              "resume_sample" not in json.dumps(request_part),
              "a reference to resume_sample survived in the request itself")

    # The OpenAI sample, with its placeholder filled, through the real parser.
    with open(os.path.join(HERE, "chat_yesno.openai.json"), encoding="utf-8") as fh:
        sample = json.loads(fh.read())
    part = sample["messages"][1]["content"][1]
    check("the OpenAI sample puts the picture in an image_url part, after the text",
          [p.get("type") for p in sample["messages"][1]["content"]]
          == ["text", "image_url"],
          "parts are %r" % [p.get("type") for p in sample["messages"][1]["content"]])

    # Fill the placeholder with the drawn fixture and let Strata read it.
    sample["messages"][1]["content"][1] = {
        "type": "image_url", "image_url": {"url": "file://" + png_path}}
    messages, _t, _o = frontend.openai_to_messages(sample)
    sources = frontend.images_of(messages)
    check("the filled OpenAI sample parses to exactly one image, and it is the "
          "drawn fixture",
          sources == ["file://" + png_path],
          "images came out as %r" % ([s[:40] for s in sources],))
    check("the question survives the parser with the picture",
          "red" in json.dumps(messages[1]["content"]).lower(),
          "the prompt text is not in the parsed message")

    # The Anthropic sample, through the Anthropic parser.
    with open(os.path.join(HERE, "chat_yesno.anthropic.json"),
              encoding="utf-8") as fh:
        a_sample = json.loads(fh.read())
    data = base64.b64encode(png).decode()
    a_sample["messages"][0]["content"][1]["source"]["data"] = data
    if hasattr(frontend, "anthropic_to_messages"):
        a_msgs, _t, _o = frontend.anthropic_to_messages(a_sample)
        a_sources = frontend.images_of(a_msgs)
        want = "data:image/png;base64," + data
        check("the filled Anthropic sample parses to an image source too",
              a_sources == [want],
              "images came out as %r" % ([str(s)[:40] for s in a_sources],))
        check("the Anthropic sample's system prompt and question survive parsing",
              any(m.get("role") == "system" for m in a_msgs)
              and "red" in json.dumps(a_msgs).lower(),
              "roles %r" % [m.get("role") for m in a_msgs])
    else:
        skip("the Anthropic sample parses", "frontend has no anthropic_to_messages")


# ---------------------------------------------------------------------------
# 4. The tripwire: nothing here may start an encoder or a GPU
# ---------------------------------------------------------------------------
def install_tripwire(server):
    """Make starting the image encoder an error, and say so if it happens.

    The claim "this test needs no GPU" is worth exactly as much as the chance of
    being wrong about it. `Vision._start` is the only way the encoder process is
    created, so it is replaced -- if any call below reaches it, the run fails
    with a name rather than quietly loading a model on the production card.
    """
    fired = []
    original = server.Vision._start

    def tripwire(self, *a, **kw):
        fired.append("Vision._start was called")
        raise AssertionError("the request contract must not start the encoder")

    server.Vision._start = tripwire
    return fired, lambda: setattr(server.Vision, "_start", original)


# ---------------------------------------------------------------------------
def main() -> int:
    import tempfile

    print("=" * 72)
    print("Strata image request contract -- CPU only, no GPU, no engine")
    print("=" * 72)

    tmp = tempfile.mkdtemp(prefix="strata-vision-contract-")
    fixtures = build_fixtures(tmp)
    check("the fixtures are drawn (no download, no real document)",
          "red_square.png" in fixtures and "blue_circle.png" in fixtures,
          "missing: %s" % sorted({"red_square.png", "blue_circle.png"} - set(fixtures)))

    server, frontend, root = load_strata()
    if server is None:
        for name in ("OpenAI content array -> sources",
                     "source string -> bytes", "http(s) download and its 32 MiB cap",
                     "bytes -> a picture the decoder reads", "EXIF orientation"):
            skip(name, "no installed Strata at %s (set STRATA_SERVE_ROOT)" % root)
        print("-" * 72)
        print("%d passed, %d failed, %d skipped -- the request contract was NOT "
              "checked." % (len(PASSED), len(FAILED), len(SKIPPED)))
        print("This machine has no Strata to read the contract from, so only the "
              "fixtures were drawn. Point STRATA_SERVE_ROOT at an installed "
              "Strata to run the rest.")
        return 1 if FAILED else 0

    print("contract read from %s" % os.path.join(root, "serve"))
    fired, restore = install_tripwire(server)
    try:
        check_part_parsing(frontend, fixtures)
        check_source_to_bytes(server, fixtures, tmp)
        check_download(server, fixtures, tmp)
        check_samples(frontend, fixtures, tmp)
        check_normalize(server, fixtures)
        check_exif(server, fixtures, tmp)
    finally:
        restore()

    check("nothing in this run started the image encoder",
          not fired, "; ".join(fired))
    check("importing Strata was done with bytecode writing off, so the installed "
          "tree was not written to",
          sys.dont_write_bytecode is True,
          "dont_write_bytecode is %r; a .pyc may have been left in %s"
          % (sys.dont_write_bytecode, root))
    check("this process never initialised CUDA (no torch/cupy import happened)",
          "torch" not in sys.modules and "cupy" not in sys.modules,
          "a GPU library was imported: %r"
          % [m for m in ("torch", "cupy") if m in sys.modules])

    print("-" * 72)
    if FINDINGS:
        print("%d finding(s) recorded, none of which fail this run:" % len(FINDINGS))
        for f in FINDINGS:
            print("  * %s" % f)
    print("%d passed, %d failed, %d skipped" % (len(PASSED), len(FAILED), len(SKIPPED)))
    for name, detail in FAILED:
        print("  FAILED: %s\n          %s" % (name, detail))
    print("temporary files left in %s" % tmp)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
