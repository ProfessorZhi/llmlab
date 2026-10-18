#!/usr/bin/env python3
"""The consumer side of the image path: what a caller must get right, and where.

This is the half of the picture contract that lives *here* rather than in
Strata. A caller -- the portal, a script, an evaluation -- has obligations the
model service cannot discharge for it:

  * an `image_url.url` that is not a string. Strata's own input layer reads the
    source and calls `.startswith` on it, so a JSON number or null reaches the
    request handler and raises `AttributeError`, which surfaces as a 500. That
    is a real upstream bug (recorded, not fixed here: see UPSTREAM below), and
    the caller's job is to not send it -- a 500 tells an operator nothing while
    a refusal raised here names the field.
  * the body limit. The gateway in front of the model refuses a body over
    `--max-body-mib` (8 by default) with a 413, and base64 inflates by 4/3, so
    "8 MiB" is about a 6 MiB picture. A caller that computes this wrong finds
    out as a broken pipe rather than a message.
  * a verdict that is actually a verdict. A 200 whose text is "I cannot see
    images" is not a NO. Parsing has to refuse to guess, or a screener will
    quietly treat a refusal as a rejection.

WHY ERRORS ARE TYPED AND CARRY A REMEDY. Every refusal below is a class with
`code`, `field` and `remedy`. The consumer of this is a portal that has to put
something in front of a person, and "ValueError: bad image" is not that. The
remedy text is written to be shown as-is.

UPSTREAM, RECORDED NOT FIXED. Two behaviours belong to Strata and are out of
scope for a read-only consumer:
  1. a non-string image source raises AttributeError (a 500) instead of the
     ValueError the rest of that path returns;
  2. a data: URL whose payload is not base64 decodes to zero bytes silently,
     because base64 decoding ignores non-alphabet characters.
Both were reproduced against the installed code in a previous round and are
listed in the report. This module defends against (1) on the caller's side and
cannot help with (2) beyond sniffing the bytes it is given.

WHAT THIS MODULE DOES NOT DO. It does not decide, and cannot decide, whether a
model can read a picture. `Reply.verdict` is what the *text* said. Whether the
text was informed by the image is a question for an inference run against a
vision-enabled model, and nothing in this file or its tests answers it.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

# The model the box actually serves. `GET /health` on Strata reports it, and
# `GET /v1/models` through the gateway agrees. Nothing else exists.
SERVED_MODEL = "qwen3.8-flash-next-iq2_xs"

# `--max-body-mib` on the gateway defaults to 8.0 and the running process
# passes no override, so this is the number a caller is actually up against.
GATEWAY_MAX_BODY = 8 * 1024 * 1024

# Formats the encoder's decoder (stb_image, inside strata-vision) reads
# directly, and formats that have to be turned into PNG first. The split is
# Strata's, not a preference: sending a WebP means Pillow converts it, and a
# node without Pillow refuses it.
DIRECT_MIME = {"image/png", "image/jpeg", "image/bmp", "image/gif"}
CONVERT_MIME = {"image/webp", "image/tiff", "image/avif"}

# A MAGIC-BYTE table, not a filename suffix. The suffix is the caller's claim;
# these are the bytes that will actually be decoded.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"BM", "image/bmp"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# What one image part costs beyond its base64 payload: the JSON around it. It
# is measured rather than estimated -- see `RequestBudget` -- but a caller
# wants a number for "how many pictures fit", so a per-image allowance is kept
# and the exact figure is computed when the request is built.
_PER_IMAGE_ALLOWANCE = 512


class ImageRejected(ValueError):
    """The caller's own input is wrong. Raised before any socket is opened.

    `code` is stable and greppable; `remedy` is written to be shown to a person.
    """

    def __init__(self, code: str, field: str, message: str, remedy: str):
        super().__init__(message)
        self.code = code
        self.field = field
        self.remedy = remedy


class TransportRefused(Exception):
    """The far end refused, and the refusal was understood.

    `status` is the HTTP status when there was one. `upload_interrupted` marks
    the case where the socket died while the body was still going out -- which
    is what an oversized upload looks like from this side, because the gateway
    answers on the Content-Length and hangs up.
    """

    def __init__(self, status, code, message, remedy, *, upload_interrupted=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.remedy = remedy
        self.upload_interrupted = upload_interrupted


class NotAVerdict(ValueError):
    """The answer was not YES or NO. Deliberately not coerced to NO."""


# ---------------------------------------------------------------------------
# Reading a picture
# ---------------------------------------------------------------------------
def sniff_mime(data: bytes) -> str | None:
    """The type of these bytes, or None if they are not a picture we know.

    From the content, never from a filename or a caller's `media_type`: those
    are claims, and the point of checking is that they might be wrong.
    """
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def convert_to_png(data: bytes) -> bytes:
    """Turn a format the decoder cannot read into one it can.

    Pillow is imported here rather than at module load: a caller that only ever
    sends PNG should not need it installed, and the error when it is missing
    should name Pillow rather than fail at import.
    """
    try:
        import io

        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImageRejected(
            "needs_pillow", "image",
            "this picture is %s, which the vision encoder cannot read "
            "directly, and Pillow is not installed to convert it (%s)"
            % ("a format needing conversion", exc),
            "install Pillow on the calling machine (python3 -m pip install "
            "pillow), or send PNG/JPEG/BMP/GIF instead") from None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - any failure is "not a picture"
        raise ImageRejected(
            "undecodable_image", "image",
            "this picture could not be decoded (%s)" % exc,
            "re-export the picture as PNG or JPEG") from None


def prepare_image(data: bytes, *, name: str = "image") -> dict:
    """-> the metadata for one picture, converted if it has to be.

    Raises ImageRejected for bytes that are not a picture at all, or that are
    a format this deployment cannot turn into one.
    """
    if not data:
        raise ImageRejected(
            "empty_image", name, "this picture is zero bytes",
            "check the upload: an empty file is usually a failed read, not an "
            "empty picture")
    mime = sniff_mime(data)
    if mime is None:
        raise ImageRejected(
            "not_an_image", name,
            "these bytes are not a picture in a format this service reads "
            "(png, jpeg, bmp, gif, webp, tiff, avif)",
            "send a PNG or JPEG")
    if mime in DIRECT_MIME:
        payload, sent_mime, converted = data, mime, False
    elif mime in CONVERT_MIME:
        payload, sent_mime, converted = convert_to_png(data), "image/png", True
    else:  # pragma: no cover - _MAGIC and this table agree today
        raise ImageRejected(
            "unsupported_image", name, "unsupported picture type %s" % mime,
            "send a PNG or JPEG")
    return {"kind": "bytes", "name": name, "source_mime": mime,
            "sent_mime": sent_mime, "payload": payload, "raw_bytes": len(data),
            "converted": converted}


# ---------------------------------------------------------------------------
# Fitting inside the body limit
# ---------------------------------------------------------------------------
def base64_len(n: int) -> int:
    """Exactly what base64 will cost for n bytes: 4 characters per 3 bytes,
    rounded up. `4*n//3 + 4` is the usual approximation and it is wrong at
    every n that is not a multiple of 3."""
    return 4 * ((n + 2) // 3)


def body_bytes(model: str, images: list[dict], prompt: str, system: str) -> int:
    """The size of the request that would be sent, measured by building it.

    Not a formula: the JSON around the payload is part of the limit, and a
    caller who budgets only for the base64 finds the difference at the 413.
    """
    return len(json.dumps(
        build_request(model, images, prompt, system).__getitem__("payload"),
        ensure_ascii=False).encode("utf-8"))


def largest_image_that_fits(body_limit: int = GATEWAY_MAX_BODY,
                            model: str = SERVED_MODEL) -> int:
    """The largest raw picture that fits alone in a body of `body_limit`.

    Solved by bisection against the real builder rather than by algebra. The
    answer is what a caller should be told when their upload is refused, and a
    wrong answer there is worse than none.
    """
    lo, hi = 0, body_limit           # hi is certainly too large
    def fits(n: int) -> bool:
        img = {"kind": "bytes", "name": "probe", "source_mime": "image/png",
               "sent_mime": "image/png", "payload": b"\0" * n,
               "raw_bytes": n, "converted": False}
        return body_bytes(model, [img], PROMPT, SYSTEM) <= body_limit
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


SYSTEM = ("You are a strict resume screener. Answer with exactly one word: "
          "YES or NO. No punctuation, no explanation.")
PROMPT = "Is the large central shape red? Answer with exactly one word: YES or NO."


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------
def validate_image_url(value) -> str:
    """The `image_url.url` the request will carry, or a refusal naming the field.

    THE CHECK THAT MATTERS MOST IS THE FIRST ONE. Strata reads this value and
    calls `.startswith` on it, so a JSON number or `null` here becomes an
    `AttributeError` inside the request handler -- a 500, with a traceback, on
    a path whose every other input error is a 400. It is reproduced against the
    installed code and is real; it is also the caller's to avoid, because the
    caller is the one who chose to put a number there.
    """
    if not isinstance(value, str):
        raise ImageRejected(
            "bad_image_url_type", "image_url.url",
            "image_url.url must be a string; got %s. Strata's input layer calls "
            ".startswith on this value, so anything else raises AttributeError "
            "inside the request handler and comes back as a 500 rather than a "
            "400 (upstream bug, recorded separately)." % type(value).__name__,
            "send the URL as a string, or use the bytes form, which builds the "
            "data URL for you")
    if not value:
        raise ImageRejected(
            "empty_image_url", "image_url.url",
            "image_url.url is an empty string",
            "send a data: URL, an http(s) URL, or the picture's bytes")
    if value.startswith("data:"):
        if "," not in value:
            raise ImageRejected(
                "malformed_data_url", "image_url.url",
                "this data: URL has no comma, so it has no payload",
                "build it as data:<media-type>;base64,<payload>")
        return value
    if value.lower().startswith(("http://", "https://")):
        return value
    raise ImageRejected(
        "unsupported_image_url", "image_url.url",
        "image_url.url is neither a data: URL nor an http(s) URL (%r)"
        % value[:60],
        "send a data: URL, an http(s) URL, or the picture's bytes. Local paths "
        "are read on the *server's* filesystem and mean whatever the server's "
        "working directory makes them mean, so they are not offered here")


def build_image_part(image: dict) -> dict:
    """One `image_url` part, as a data URL.

    A data URL rather than a path or a link, for two reasons that matter to a
    consumer: a path is read on the *server's* filesystem (so it means whatever
    the server's working directory makes it mean), and an http URL makes the
    model service fetch an address of the caller's choosing. Neither is a thing
    a portal should be able to ask for by accident -- `build_url_part` exists
    for the caller that means it.
    """
    b64 = base64.b64encode(image["payload"]).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": "data:%s;base64,%s" % (image["sent_mime"], b64),
                          # Recorded for the caller's own log. The OpenAI shape
                          # has nowhere to publish it; see the report.
                          "detail": "high"}}


def build_url_part(url) -> dict:
    """A part for a picture the *server* fetches, validated but not followed.

    Nothing here opens the URL. The model service downloads it, under its own
    32 MiB cap, from its own network position -- which is a different thing
    from the caller's and is why this is offered deliberately rather than by
    accident.
    """
    return {"type": "image_url", "image_url": {"url": validate_image_url(url)}}


def build_request(model: str, items: list[dict], prompt: str = PROMPT,
                  system: str = SYSTEM, *, max_tokens: int = 8,
                  temperature: float = 0, stream: bool = False) -> dict:
    """The request body and a manifest of what is in it.

    `items` is what `prepare` produces: each entry is either bytes this process
    read (`{"kind": "bytes", ...}`) or a URL the server will fetch
    (`{"kind": "url", ...}`). Ordered text-then-pictures, which is the order
    the encoder fills the prompt's image placeholders in.
    """
    content = [{"type": "text", "text": prompt}]
    manifest_images = []
    for item in items:
        if item["kind"] == "url":
            content.append(build_url_part(item["url"]))
            manifest_images.append({"name": item["name"], "kind": "url",
                                    "url": item["url"], "sent_mime": None,
                                    "source_mime": None, "raw_bytes": None,
                                    "converted": False, "encoded_bytes": len(item["url"])})
        else:
            content.append(build_image_part(item))
            manifest_images.append({
                "name": item["name"], "kind": "bytes",
                "sent_mime": item["sent_mime"], "source_mime": item["source_mime"],
                "raw_bytes": item["raw_bytes"], "converted": item["converted"],
                "encoded_bytes": base64_len(len(item["payload"]))})
    payload = {
        "model": model,
        "stream": bool(stream),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
    }
    return {"payload": payload,
            "manifest": {"model": model, "image_count": len(items),
                         "images": manifest_images}}


def check_budget(request: dict, body_limit: int = GATEWAY_MAX_BODY) -> int:
    """Raise unless the built request fits. Returns its size.

    This is the check the 413 exists to replace, done where the caller can be
    told *which* picture is too big and by how much.
    """
    size = len(json.dumps(request["payload"], ensure_ascii=False).encode("utf-8"))
    if size > body_limit:
        biggest = max((i["encoded_bytes"] for i in request["manifest"]["images"]),
                      default=0)
        raise ImageRejected(
            "body_over_gateway_limit", "images",
            "this request is %d bytes; the gateway refuses a body over %d "
            "bytes (%.1f MiB). The pictures are %d of them and the largest is "
            "%d bytes once base64-encoded (base64 adds a third)."
            % (size, body_limit, body_limit / 1048576.0,
               len(request["manifest"]["images"]), biggest),
            "send fewer or smaller pictures. One picture alone may be up to "
            "about %.1f MiB: %d bytes. Downscale to fit."
            % (largest_image_that_fits(body_limit) / 1048576.0,
               largest_image_that_fits(body_limit)))
    return size


# ---------------------------------------------------------------------------
# Sending it
# ---------------------------------------------------------------------------
class Reply:
    """What came back, with the distinction the whole exercise turns on.

    `text` is what the model said. `verdict` is YES/NO if the text was exactly
    that, and None otherwise. Nothing here claims the picture was read.
    """

    def __init__(self, text, *, status, served_model, usage=None,
                 streamed=False, fell_back=False, raw=None):
        self.text = text
        self.status = status
        self.served_model = served_model
        self.usage = usage
        self.streamed = streamed
        self.fell_back = fell_back
        self.raw = raw

    @property
    def verdict(self):
        """'YES', 'NO', or None. None means the answer was not a verdict --
        it is not the same as NO and must not be treated as one.

        A reply that asked for a business JSON document has no bare verdict,
        because its text is an object rather than one word. That is why this is
        None for one, and why `document` exists: the two shapes are both
        supported and a caller must say which it asked for.
        """
        try:
            return parse_verdict(self.text)
        except NotAVerdict:
            return None

    @property
    def document(self):
        """The answer parsed as a JSON object, or None if it is not one.

        For a caller whose prompt asked for business JSON rather than a bare
        word. Deliberately returns None rather than raising: "the model
        answered in prose" is an outcome a screener has to decide about, not a
        crash.
        """
        if not self.text:
            return None
        text = self.text.strip()
        if text.startswith("```"):                 # a fenced block is common
            text = text.strip("`")
            text = text[text.find("{"):] if "{" in text else text
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            doc = json.loads(text[start:end + 1])
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def __repr__(self):
        return "<Reply status=%s verdict=%r text=%r>" % (
            self.status, self.verdict, (self.text or "")[:40])


def parse_verdict(text: str) -> str:
    """Strictly YES or NO. Anything else raises.

    The rule is deliberately unforgiving. A screener that maps "I cannot view
    images" to NO rejects a good candidate for the wrong reason, and the caller
    would never see it -- which is exactly the failure that a lenient parser
    here would hide.
    """
    if text is None:
        raise NotAVerdict("no answer at all")
    t = text.strip().strip(".").strip()
    if t.upper() == "YES":
        return "YES"
    if t.upper() == "NO":
        return "NO"
    raise NotAVerdict(
        "the answer was not YES or NO: %r. A refusal or an explanation is not "
        "a NO -- treat this as 'no verdict' and decide separately." % (text[:120],))


class VisionClient:
    """A caller for `POST /v1/chat/completions` with pictures in it.

    Deliberately small and stdlib-only: the portal has its own HTTP stack and
    this is here to state the contract, so that whoever wires it up can see
    exactly what has to be true. `send_raw` takes a prebuilt body, which is how
    a caller using the OpenAI SDK would reuse everything except the POST.
    """

    def __init__(self, base_url: str, key: str | None = None, *,
                 model: str = SERVED_MODEL,
                 body_limit: int = GATEWAY_MAX_BODY,
                 timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.model = model
        self.body_limit = body_limit
        self.timeout = timeout
        # No proxy: a Windows box with HTTP_PROXY set sends localhost to the
        # proxy, and the failure looks like the service refusing.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    # -- the caller-side checks, before any socket ------------------------
    def prepare(self, sources, *, system=SYSTEM, prompt=PROMPT,
                max_tokens=8, temperature=0, stream=False):
        """`sources` -> a request ready to send, fully validated.

        Accepts bytes, a path, or an open file for each picture. Every refusal
        here happens before a connection is made, which is the point: the
        caller finds out what is wrong without spending an upload on it.
        """
        items = []
        for index, source in enumerate(sources):
            name = "image[%d]" % index
            if isinstance(source, (bytes, bytearray)):
                items.append(prepare_image(bytes(source), name=name))
            elif isinstance(source, str) and source.lower().startswith(
                    ("http://", "https://")):
                # A URL the server will fetch. Validated for type here -- the
                # field that becomes a 500 upstream -- and not opened.
                items.append({"kind": "url", "name": name,
                              "url": validate_image_url(source)})
            elif isinstance(source, str) and source.startswith("data:"):
                items.append({"kind": "url", "name": name,
                              "url": validate_image_url(source)})
            elif isinstance(source, (str, os.PathLike)):
                name = os.path.basename(str(source)) or name
                try:
                    with open(source, "rb") as fh:
                        data = fh.read()
                except OSError as exc:
                    raise ImageRejected(
                        "unreadable_image", name,
                        "this picture could not be read (%s)" % exc,
                        "check the path and that the file is readable by the "
                        "process sending it") from None
                items.append(prepare_image(data, name=name))
            else:
                raise ImageRejected(
                    "bad_image_source", name,
                    "a picture must be bytes, a path, a data: URL or an http(s) "
                    "URL; got %s" % type(source).__name__,
                    "pass the file's bytes or its path")

        if not items:
            raise ImageRejected(
                "no_images", "images",
                "a picture request with no pictures in it",
                "send at least one picture, or use the text endpoint")

        request = build_request(self.model, items, prompt, system,
                                max_tokens=max_tokens, temperature=temperature,
                                stream=stream)
        request["size"] = check_budget(request, self.body_limit)
        return request

    # -- and then the socket ---------------------------------------------
    def send_raw(self, request: dict) -> Reply:
        """POST a request built by `prepare` (or by an SDK adapter)."""
        body = json.dumps(request["payload"], ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer" + " " + self.key
        req = urllib.request.Request(self.base_url + "/v1/chat/completions",
                                     data=body, headers=headers, method="POST")
        streamed = bool(request["payload"].get("stream"))
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise self._refusal(exc) from None
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            # The gateway answers on the Content-Length and hangs up, so an
            # oversized body arrives here as a dead socket rather than a 413.
            raise TransportRefused(
                None, "upload_interrupted",
                "the connection died while the request was still being sent "
                "(%s). For a picture request this is almost always the body "
                "limit: the gateway refuses on the Content-Length and closes "
                "before reading it, so the 413 never reaches the caller."
                % type(exc).__name__,
                "this request was %d bytes against a %d byte limit. Send "
                "smaller pictures." % (len(body), self.body_limit),
                upload_interrupted=True) from None
        except urllib.error.URLError as exc:
            # urllib wraps most socket errors in URLError, so the interrupted
            # upload arrives here rather than in the tuple above. The `reason`
            # is what distinguishes "the far end hung up mid-body" from "there
            # is nothing listening".
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (BrokenPipeError, ConnectionResetError,
                                   ConnectionAbortedError)):
                raise TransportRefused(
                    None, "upload_interrupted",
                    "the connection died while the request was still being "
                    "sent (%s). For a picture request this is almost always the "
                    "body limit: the gateway refuses on the Content-Length and "
                    "closes before reading it, so the 413 never reaches the "
                    "caller." % type(reason).__name__,
                    "this request was %d bytes against a %d byte limit. Send "
                    "smaller pictures." % (len(body), self.body_limit),
                    upload_interrupted=True) from None
            raise TransportRefused(
                None, "unreachable",
                "the service could not be reached: %s" % exc,
                "check the address and that the gateway is running") from None

        with resp:
            status = resp.status
            served = self.model
            if streamed:
                text, usage = self._read_stream(resp)
            else:
                doc = json.loads(resp.read() or b"{}")
                text, usage = self._read_plain(doc)
                # The id the service says it served with, when it says.
                served = doc.get("model") or self.model
        return Reply(text, status=status, served_model=served,
                     usage=usage, streamed=streamed)

    def ask(self, sources, **kw):
        """prepare + send, the whole thing."""
        return self.send_raw(self.prepare(sources, **kw))

    def ask_with_fallback(self, sources, **kw):
        """Stream if asked, and fall back to a plain request if streaming is
        refused. Returns (reply, fell_back).

        The fallback is not hypothetical: streaming is a different response
        shape, and a proxy or an older server that does not do it answers with
        an error rather than a stream. A caller that would rather have the
        answer than the stream should say so -- and should be told which one it
        got, which is what `Reply.fell_back` is for.
        """
        want_stream = bool(kw.get("stream"))
        if not want_stream:
            return self.ask(sources, **kw), False
        try:
            return self.ask(sources, **kw), False
        except TransportRefused as exc:
            if exc.status not in (400, 404, 405, 415, 501):
                raise
            kw = dict(kw, stream=False)
            return self.ask(sources, **kw), True

    # -- reading what came back -------------------------------------------
    @staticmethod
    def _read_plain(doc: dict):
        choices = doc.get("choices") or []
        text = ""
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
        return text, doc.get("usage")

    def _read_stream(self, resp):
        """SSE: `data: {...}` lines until `data: [DONE]`.

        The last chunk carrying a `usage` wins, because that is the one the
        engine sends after the answer; a stream with none reports none rather
        than an estimate.
        """
        pieces, usage, seen_done = [], None, False
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                seen_done = True
                continue
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    pieces.append(delta["content"])
        if not seen_done:
            raise TransportRefused(
                None, "stream_truncated",
                "the stream ended without its [DONE] marker, so the answer may "
                "be cut short",
                "retry without streaming, or treat this answer as incomplete")
        return "".join(pieces), usage

    @staticmethod
    def _refusal(exc) -> TransportRefused:
        """Turn an HTTP error into something a caller can act on."""
        status = exc.code
        detail = {}
        try:
            detail = (json.loads(exc.read() or b"{}").get("error") or {})
        except Exception:  # noqa: BLE001 - a refusal we cannot parse is still a refusal
            pass
        code = detail.get("code") or "http_%d" % status
        message = detail.get("message") or exc.reason or "refused"
        remedies = {
            413: "the body is over the gateway's --max-body-mib (%d bytes). "
                 "Fewer or smaller pictures." % GATEWAY_MAX_BODY,
            401: "the API key is missing, disabled or revoked.",
            429: "the service is single-slot and busy; read Retry-After and "
                 "come back.",
            502: "the model service is not reachable from the gateway.",
            504: "the model service did not answer in time.",
        }
        remedy = remedies.get(status)
        if remedy is None and status == 400:
            # A 400 about pictures is the engine saying it was not started
            # with the encoder -- which is the current state of the box.
            remedy = ("if this mentions vision or images: the model service is "
                      "running without the image encoder. Check GET /health on "
                      "it for \"images\": true before sending pictures.")
        return TransportRefused(status, code, message,
                                remedy or "see the response body for detail")


def encode_signature(items) -> str:
    """A short, stable digest of the pictures a request carries.

    Used by the tests to say "these exact bytes arrived, in this order" without
    putting the picture in a log. A URL item contributes its URL, not its
    content: nothing here fetches anything.
    """
    import hashlib
    h = hashlib.sha256()
    for item in items:
        if item.get("kind") == "url":
            h.update(b"url\0%s\0" % item["url"].encode("utf-8", "replace"))
        else:
            h.update(b"%s\0%d\0" % (item["sent_mime"].encode(),
                                    len(item["payload"])))
            h.update(item["payload"])
    return h.hexdigest()[:16]


__all__ = [
    "SERVED_MODEL", "GATEWAY_MAX_BODY", "DIRECT_MIME", "CONVERT_MIME",
    "SYSTEM", "PROMPT",
    "ImageRejected", "TransportRefused", "NotAVerdict", "Reply", "VisionClient",
    "sniff_mime", "convert_to_png", "prepare_image", "base64_len", "body_bytes",
    "largest_image_that_fits", "build_image_part", "build_url_part",
    "validate_image_url", "build_request", "check_budget", "parse_verdict",
    "encode_signature",
]
