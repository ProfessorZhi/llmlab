"""WSGI adapter.

Kept deliberately thin: translate a PEP 3333 environ into a
:class:`~backend.auth.routes.Request`, and a :class:`~backend.auth.routes.Response`
back out. All policy lives in :mod:`backend.auth.routes`.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from .routes import REASON_PHRASES, Application, Request, Response

DEFAULT_REASON = "OK"


def parse_cookies(header: str | None) -> dict[str, str]:
    jar: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        name, separator, value = chunk.partition("=")
        if not separator:
            continue
        name = name.strip()
        if name:
            jar[name] = value.strip()
    return jar


def header_environ(environ: Mapping[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].lower().replace("_", "-")] = str(value)
    for key, name in (("CONTENT_TYPE", "content-type"), ("CONTENT_LENGTH", "content-length")):
        raw = environ.get(key)
        if raw:
            headers[name] = str(raw)
    return headers


def read_body(environ: Mapping[str, Any], max_bytes: int) -> bytes:
    """Read at most ``max_bytes + 1`` octets, and only when the length is known.

    PEP 3333 leaves ``CONTENT_LENGTH`` unset for a request that carries no body
    — a plain browser ``GET`` is the common case — and never promises that a
    read will reach the count it was asked for. Every real server hands the
    application a ``wsgi.input`` backed by the client socket, so reading when no
    length was declared blocks until the peer sends that much or hangs up: the
    request stalls and the client times out first.

    So the rule is fail-closed. The body is read only when the server declared a
    positive, well-formed length; anything else — header absent, empty,
    malformed or negative — is an empty body and the stream is left untouched.

    The extra octet beyond ``max_bytes`` is what lets the router detect an
    oversized body without trusting ``CONTENT_LENGTH``, and without ever
    buffering the whole payload.
    """
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""

    declared = _declared_length(environ)
    if declared is None or declared <= 0:
        return b""

    try:
        return stream.read(min(max_bytes + 1, declared)) or b""
    except (ValueError, OSError):  # pragma: no cover - defensive
        return b""


def _declared_length(environ: Mapping[str, Any]) -> int | None:
    """The octet count the server declared for this request, or ``None``.

    ``None`` means "no usable declared body length", which covers three shapes
    that must all behave identically — read nothing. The header is absent (PEP
    3333's bodyless request), it is present but empty, or it is not a plain
    decimal integer. Only ASCII digits are accepted: ``int()`` would gladly take
    ``"+5"``, ``"1_0"`` or non-ASCII digits, none of which is a legal
    ``Content-Length`` and all of which are worth refusing rather than
    reinterpreting.

    A request carrying ``Transfer-Encoding`` is also ``None``. Chunked framing
    is the server's job under PEP 3333 — it must be decoded before the
    application runs — so reading it here would pull an unbounded stream and
    treat framing octets as payload. The supported deployment therefore
    terminates chunked encoding at the trusted reverse proxy in front of the
    Portal (Caddy), which forwards a ``Content-Length``; a request that still
    arrives chunked is refused, surfacing as the router's ordinary 400 rather
    than a hang or a body smuggled past the length checks.
    """
    if environ.get("HTTP_TRANSFER_ENCODING"):
        return None
    raw = environ.get("CONTENT_LENGTH")
    if raw is None:
        return None
    text = str(raw)
    if not text.isascii() or not text.isdigit():
        return None
    return int(text)


def build_request(environ: Mapping[str, Any], *, max_body: int) -> Request:
    headers = header_environ(environ)
    query: dict[str, list[str]] = {}
    for pair in str(environ.get("QUERY_STRING") or "").split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        query.setdefault(_unquote(key), []).append(_unquote(value))

    host = headers.get("host", "")
    return Request(
        method=str(environ.get("REQUEST_METHOD", "GET")).upper(),
        path=str(environ.get("PATH_INFO") or "/"),
        query=query,
        headers=headers,
        cookies=parse_cookies(headers.get("cookie")),
        body=read_body(environ, max_body),
        client_ip=str(environ.get("REMOTE_ADDR") or ""),
        scheme=str(environ.get("wsgi.url_scheme") or "https"),
        host=host,
    )


def _unquote(value: str) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(value.replace("+", " "))


def serve(
    app: Application,
    environ: Mapping[str, Any],
    start_response: Callable[[str, list[tuple[str, str]]], Any],
) -> Iterable[bytes]:
    request = build_request(environ, max_body=app.cfg.max_request_bytes)
    response: Response = app.handle(request)
    reason = REASON_PHRASES.get(response.status, DEFAULT_REASON)
    start_response(f"{response.status} {reason}", list(response.headers))
    return [response.body]


__all__ = ["build_request", "header_environ", "parse_cookies", "read_body", "serve"]
