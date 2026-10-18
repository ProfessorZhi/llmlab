"""Local, loopback-only preview of the Portal sign-in flow.

This module is a **development entry point, not a deployment**. It exists so the
real authentication backend and the real login page can be exercised together —
over an actual TCP socket, with cookies actually travelling back through a
client — without a reverse proxy, a TLS certificate, or anything that serves
traffic. It is what ``tests/integration_auth`` drives.

What it is
    A WSGI container that composes three things that already exist: the
    unchanged :class:`backend.auth.routes.Application`, the unchanged static
    files in ``web/auth/``, and a synthetic DEMO page rendered from
    ``prototypes/v5/index.html``.

    It also exposes one read-only JSON endpoint, ``GET /api/atlas/usage``,
    which returns an :class:`AtlasDTO` built by the pure functions in
    :mod:`backend.atlas` over the offline synthetic fixture in
    :mod:`dev.atlas_preview`. That endpoint lives here and nowhere else: it has
    no counterpart in :data:`backend.auth.routes.ROUTES`, so a production
    listener never serves it. Access requires **both** a live Session *and* an
    explicit, server-side grant — see :meth:`PreviewApplication._atlas_usage`.

    And one more, ``GET /api/atlas/calls``, which returns the **caller's own**
    per-call log — a different contract, a different scope and a different
    fixture (:mod:`dev.atlas_calls`). It is the second of the two independent
    views: ``/api/atlas/usage`` is an authorised *project* aggregate, this one
    is ``principal == subject`` over the trusted key directory. Neither implies
    the other; see :meth:`PreviewApplication._calls` and
    ``docs/portal-call-log-contract.md``.

What it deliberately is *not*
    * It is not a second implementation of the auth policy. Every guard that
      decides "who is signed in" and "may this account see anything" is the
      product's own: ``AuthService.current_session`` + ``Application.authorize``
      + ``AuthService.projects_for``. The container only decides *what to serve*
      once the backend has answered.
    * It is not reachable off the machine. ``make_server`` refuses any host but
      loopback, and a request whose ``Host`` header is not *exactly* one of the
      three loopback spellings — with the port this server is actually listening
      on, whenever a port is given at all — is refused with 400 rather than
      being served. A request that arrives without a usable ``Host``, or with
      more than one, is refused too. See ``docs/preview-host-security.md``.
    * It is not a production cookie configuration. ``cookie_secure`` is only
      dropped when the caller states, explicitly, that this is a plain-HTTP local
      development run; without that statement the module refuses to build and
      production keeps requiring HTTPS + ``Secure``.
    * It has no default credential and no way to preset one. If no administrator
      has been bootstrapped in the state directory, startup fails and prints the
      interactive ``bootstrap init`` command. There is no ``--password``.
    * It never reaches the network. Feishu is either absent (the honest
      "unavailable" state) or a loopback mock; the real app id/secret are not
      read from anywhere, including the operator's environment, because the
      configuration is built from an *empty* environment.
    * It cannot read arbitrary files. The static surface is two exact-path
      allowlists -- ``web/auth/`` (four files, including the login page) and
      ``web/`` (the call-log page's assets, reached only through ``/calls``).
      Every other path is a 404 regardless of what it spells.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import socketserver
import stat
import sys
import time
import urllib.parse
import wsgiref.simple_server
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from backend import atlas as atlas_contract
from backend.auth import bootstrap, errors, local_admin
from backend.auth.config import AuthConfig
from backend.auth.feishu import FeishuIdentity
from backend.auth.routes import REASON_PHRASES, Application
from backend.auth.wsgi import parse_cookies

from dev import atlas_calls, atlas_preview, portal_modules

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_AUTH_DIR = REPO_ROOT / "web" / "auth"
WEB_ROOT = REPO_ROOT / "web"
DEMO_SOURCE = REPO_ROOT / "prototypes" / "v6" / "index.html"

#: The only address the preview will ever bind to, and the only kind of
#: authority a request may name. Canonical spellings: ``::1`` is unbracketed
#: here because this set names *hosts*, while a ``Host`` header names an
#: ``uri-host``, where an IPv6 literal must be bracketed.
DEFAULT_HOST = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: The three loopback authorities as they may appear in a ``Host`` header.
_LOOPBACK_SPELLINGS = ("127.0.0.1", "localhost", r"\[::1\]")

#: ``Host = uri-host [ ":" port ]`` (RFC 7230 §5.4), matched with
#: :meth:`re.Pattern.fullmatch` against the **whole** field value, with no
#: "everything before the colon" step anywhere. That totality is the point: an
#: authority this preview answers to is a fixed string, so anything else is
#: refused, rather than being partially recognised.
#:
#: * nothing may precede or follow the authority, so ``127.0.0.1:59008.evil.com``,
#:   ``127.0.0.1:59008,evil.com`` and ``127.0.0.1:59008/evil`` do not contain a
#:   recognised prefix that is then stripped away — they are not this authority;
#: * ``fullmatch`` rather than an anchored ``match``: ``$`` also matches just
#:   before a trailing newline, which is one more "almost the whole value" the
#:   guard should not have to reason about;
#: * the port, when present, is one to five ASCII digits and nothing else: no
#:   sign, no whitespace, no hex, no decimal point, no second colon;
#: * ``re.ASCII`` makes ``re.IGNORECASE`` apply to ASCII letters only
#:   (``LOCALHOST`` matches a reg-name; Unicode look-alikes and fullwidth digits
#:   do not) and ``[0-9]`` mean ASCII digits only (``１２３`` is not a port);
#: * five digits is the most the grammar can match, so ``0`` and ``65536`` get
#:   as far as the pattern and are turned away by the range check in
#:   :func:`parse_host_authority`.
_HOST_HEADER_RE = re.compile(
    r"(?P<host>" + "|".join(_LOOPBACK_SPELLINGS) + r")(?::(?P<port>[0-9]{1,5}))?",
    re.IGNORECASE | re.ASCII,
)

#: A stated port has to be a real one, and has to be ours.
MIN_PORT = 1
MAX_PORT = 65535


def parse_host_authority(raw: str) -> tuple[str, int | None] | None:
    """``Host`` header value -> ``(canonical host, port or None)``, else ``None``.

    ``None`` means "not an authority this preview answers to", which covers a
    foreign host, a malformed field value and a port outside ``1..65535`` alike;
    the caller does not need to tell those apart to refuse the request. The
    syntactic and the numeric halves of that judgement live here together on
    purpose — a caller cannot apply one and forget the other.

    A returned host is one of the spellings in ``_LOOPBACK_SPELLINGS``,
    lower-cased and canonicalised (``[::1]`` becomes ``::1``) so it can be
    compared with :data:`LOOPBACK_HOSTS`. A returned port still has to be
    compared with the port the server is actually listening on; that is the one
    judgement this function cannot make on its own.
    """
    match = _HOST_HEADER_RE.fullmatch(raw)
    if match is None:
        return None

    host = match.group("host").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    port_text = match.group("port")
    if port_text is None:
        return host, None

    port = int(port_text)
    if not (MIN_PORT <= port <= MAX_PORT):
        return None
    return host, port


#: Injected into the DEMO page. Used by the tests as the positive marker that
#: the page really is the marked demo and not some other document.
DEMO_BANNER_ID = "llm-lab-demo-banner"

#: The DEMO page is a self-contained document: inline <style>, inline <script>,
#: no subresource of any kind. ``connect-src 'none'`` is therefore not a
#: restriction on the page, it is a statement of fact — and it is what stops a
#: future edit to the prototype from silently reaching a real service.
DEMO_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

#: The login page talks to its own origin and nothing else.
LOGIN_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

MOCK_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)

#: The call-log page shares the login page's shape: its own origin only, no
#: inline script, no inline style. Identical to :data:`LOGIN_CSP` by intent, not
#: by coincidence -- and the document also carries the same policy as a meta tag,
#: so the two cannot drift apart silently.
CALLS_CSP = LOGIN_CSP

#: The one self-contained informational page this module renders itself (the
#: "no project authorisation" refusal) ships a single inline <style> block and
#: no script at all. It therefore needs ``style-src 'unsafe-inline'`` — the
#: LOGIN_CSP is *not* usable here: with ``style-src 'self'`` the browser blocks
#: that block and the page renders unstyled. Nothing else is relaxed: there is
#: still no script source, no connect source and no form target.
INFO_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)

#: The read-only Atlas endpoint answers with JSON and nothing else. A CSP on a
#: JSON document is not what stops script execution — ``Content-Type`` and
#: ``nosniff`` are — but it costs nothing and it means a future edit that
#: started rendering this payload as a document would already be locked down.
JSON_CSP = "default-src 'none'; connect-src 'none'; base-uri 'none'; frame-ancestors 'none'"

#: ``path -> (filename in web/auth/, media type, CSP or None)``. An exact-match
#: dictionary, so a path is readable only by being written out here.
STATIC_ROUTES: dict[str, tuple[str, str, str | None]] = {
    "/login": ("index.html", "text/html; charset=utf-8", LOGIN_CSP),
    "/auth.css": ("auth.css", "text/css; charset=utf-8", None),
    "/auth.js": ("auth.js", "text/javascript; charset=utf-8", None),
    "/auth-core.js": ("auth-core.js", "text/javascript; charset=utf-8", None),
}

FAVICON_PATH = "/favicon.ico"

#: ``/calls`` is now a **deep link** into the single-page shell, not a page of
#: its own. The call log became a view of the V5 shell (``#view-calls``), so the
#: standalone document it used to have would be a second, drifting copy of the
#: same screen. A permanent redirect keeps every bookmark working and leaves one
#: URL that renders the view.
CALLS_DEEP_LINK = "/calls"
CALLS_VIEW_HASH = "/atlas#calls"

#: Paths gated behind a session. ``/`` and ``/atlas`` serve the synthetic DEMO
#: page; ``/calls`` serves the call-log page and is gated on its **own** grant --
#: see :meth:`PreviewApplication._protected`.
PROTECTED_PAGES = frozenset({"/", "/atlas"})

AUTH_PREFIX = "/auth"

MOCK_AUTHORIZE_PATH = "/dev/mock/feishu/authorize"

# --- the offline Feishu mock -------------------------------------------------
#
# Synthetic values, spelled so that nobody can mistake them for a deployment.
# They are not read from the environment and are not used to reach Feishu: the
# mock provider never performs a token exchange, it returns a fixed identity.

MOCK_APP_ID = "cli_mock_preview_not_a_real_app"
MOCK_APP_SECRET = "mock-preview-value-not-a-credential"
MOCK_REDIRECT_URI = "https://127.0.0.1/mock-feishu/callback"
MOCK_TENANT_KEY = "tenant_mock_preview"
MOCK_CODE_PREFIX = "synthetic-demo-code"
MOCK_OPEN_ID = "ou_mock_preview_member"
MOCK_DISPLAY_NAME = "合成演示成员"


def mock_code_for(state: str) -> str:
    """The authorisation code this mock would issue for one sign-in attempt.

    A real provider hands out a **fresh, single-use** code per authorisation and
    the backend enforces exactly that (``claim_provider_code``). A constant here
    would make the *second* sign-in on one preview server fail with
    ``invalid_state`` — the backend being right and the mock being wrong. Keying
    the code on ``state`` (already unique per attempt) keeps the mock honest,
    still entirely offline, and with no memory to leak between attempts.
    """
    return f"{MOCK_CODE_PREFIX}-{state}"


DEMO_BANNER_TEMPLATE = """<div id="{banner_id}" role="region" aria-label="演示环境声明" style="position:relative;z-index:9999;margin:0;padding:14px clamp(20px,4vw,66px);background:#7a2f22;color:#fff8f4;font:13px/1.5 'Segoe UI',system-ui,sans-serif;text-align:center">
<strong style="letter-spacing:.08em">DEMO · 本地预览环境 · 合成演示数据</strong><br>
<span style="opacity:.92">此页面由 <code>dev/preview_auth.py</code> 在 127.0.0.1 上提供，仅用于验证登录流程是否工作。
页面内容为 <code>prototypes/v5/index.html</code> 的设计原型，全部为合成的示例数值，
<strong>未连接生产 Gateway、Strata 或任何真实数据源</strong>，也不代表任何真实用量或授权。</span>
</div>
"""

MOCK_PAGE_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock 飞书授权（本地预览）</title>
<style>
body{{margin:0;padding:48px 24px;background:#12181f;color:#eef2f6;
font:15px/1.7 'Segoe UI',system-ui,sans-serif}}
main{{max-width:640px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 8px}}
p{{color:#9fb0c0}}
code{{background:#1d2733;padding:2px 6px;border-radius:4px;font-size:13px;word-break:break-all}}
a{{display:inline-block;margin-top:20px;padding:12px 20px;border-radius:8px;
background:#2f6fb8;color:#fff;text-decoration:none;font-weight:600}}
small{{display:block;margin-top:26px;color:#7b8a99}}
</style></head><body><main>
<h1>Mock 飞书授权页（本地演示）</h1>
<p>这是 <strong>离线</strong> 的授权页替身：它不会连接 feishu.cn，也不会交换任何凭据。
点击下面的链接即可继续本地回调，模拟"用户点了同意"。</p>
<p><code>state = {state}</code></p>
<a href="{target}" rel="noopener">同意并继续（本地回调）</a>
<small>仅供本机开发预览使用。生产环境由真实的飞书企业应用承担这一步。</small>
</main></body></html>
"""


class MockFeishuProvider:
    """An offline stand-in for the Feishu provider.

    It implements only the two methods ``AuthService`` calls, and both stay on
    this machine: ``authorization_url`` points at the preview's own mock consent
    page, and ``exchange_code`` returns a fixed synthetic identity instead of
    calling Feishu. No app secret is used for anything.
    """

    supports_pkce = False

    def __init__(self, base_url: Callable[[], str]) -> None:
        self._base_url = base_url

    def authorization_url(self, *, state: str, code_challenge: str | None = None) -> str:
        if code_challenge:
            raise errors.ConfigError(
                detail="the mock provider does not support PKCE; refusing to drop the challenge"
            )
        query = urllib.parse.urlencode({"state": state})
        return f"{self._base_url()}{MOCK_AUTHORIZE_PATH}?{query}"

    def exchange_code(self, *, code: str, code_verifier: str | None = None) -> FeishuIdentity:
        # Only a code this provider could have issued is accepted: the fixed
        # prefix plus the state it was minted for. A bare or foreign value is
        # refused exactly as the real provider refuses one.
        if not code.startswith(f"{MOCK_CODE_PREFIX}-") or len(code) <= len(MOCK_CODE_PREFIX) + 1:
            # Same rule as the real provider: an unusable code is the user's
            # problem, not the deployment's, and the message says nothing about
            # what was supplied.
            raise errors.AccessDenied(detail="the mock provider only accepts its own demo code")
        return FeishuIdentity(
            open_id=MOCK_OPEN_ID,
            union_id=None,
            tenant_key=MOCK_TENANT_KEY,
            display_name=MOCK_DISPLAY_NAME,
        )


@dataclass
class _Response:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


def _reason(status: int) -> str:
    return REASON_PHRASES.get(status, "OK")


def _json_error(status: int, code: str, message: str) -> _Response:
    body = json.dumps(
        {"ok": False, "error": {"code": code, "message": message}}, separators=(",", ":")
    ).encode("utf-8")
    return _Response(
        status=status,
        headers=[("Content-Type", "application/json; charset=utf-8")],
        body=body,
    )


def _html_page(status: int, title: str, paragraphs: Iterable[str]) -> _Response:
    body = "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            '<meta name="robots" content="noindex">',
            f"<title>{html.escape(title)}</title>",
            "<style>body{margin:0;padding:48px 24px;background:#f5f4ed;color:#202c23;"
            "font:15px/1.7 'Segoe UI',system-ui,sans-serif}"
            "main{max-width:640px;margin:0 auto}h1{font-size:20px;margin:0 0 12px}"
            "p{color:#5a685a}a{color:#2f6fb8}</style></head><body><main>",
            f"<h1>{html.escape(title)}</h1>",
            *(f"<p>{html.escape(text)}</p>" for text in paragraphs),
            "</main></body></html>",
        ]
    ).encode("utf-8")
    return _Response(
        status=status,
        headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Security-Policy", INFO_CSP),
        ],
        body=body,
    )


#: ``FILE_ATTRIBUTE_REPARSE_POINT`` on Windows. A **directory junction** carries
#: it (measured: junction = ``0x410``, ordinary directory = ``0x10``) while
#: ``stat.S_ISLNK`` stays false, so ``Path.is_symlink()`` cannot see one.
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

#: ``IO_REPARSE_TAG_MOUNT_POINT`` -- the tag that distinguishes a junction from
#: the other reparse kinds. Only used to name the thing in the error message;
#: the refusal is driven by the attribute bit, not the tag.
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _redirect_kind(component: pathlib.Path) -> str | None:
    """How *component* redirects elsewhere, or ``None`` if it is a real entry.

    Judged with ``lstat``, which never follows the link, so a component is
    classified by what it *is* rather than by what it points at. Two independent
    signals, because neither one covers both platforms:

    * ``stat.S_ISLNK`` -- the POSIX mechanism, and on Windows the real-symlink
      case;
    * the ``FILE_ATTRIBUTE_REPARSE_POINT`` bit -- what a Windows **junction**
      carries. ``mklink /J`` creates one with no elevation at all, and
      ``S_ISLNK`` is false for it, so a symlink-only check is blind to it.

    A component that does not exist yet returns ``None``: nothing was made
    there, so nothing can have been redirected there.
    """
    try:
        status = component.lstat()
    except OSError:
        # Missing, or unreadable. Either way this module is not the thing that
        # would create it, and a path that cannot be inspected here is judged by
        # the checks it does reach rather than refused on a guess.
        return None

    if stat.S_ISLNK(status.st_mode):
        return "symbolic link"
    if getattr(status, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        if getattr(status, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT:
            return "junction"
        return "reparse point"
    return None


def find_redirecting_component(path: pathlib.Path) -> tuple[pathlib.Path, str] | None:
    """The shallowest existing component of *path* that redirects elsewhere.

    Walking from the filesystem root down to the last component -- rather than
    looking only at the leaf -- is the whole point. ``<junction>/state`` is an
    ordinary directory to every check that inspects only the leaf, while the
    path the credential would actually be written through has already been
    redirected. Refusing the ancestor refuses the whole subtree.

    Nothing is resolved while walking: ``resolve()`` would follow exactly the
    links this function exists to find. It is also why the caller must not
    compare ``resolve()`` with the original spelling -- on this machine
    ``%TEMP%`` is the 8.3 short form (``C:\\Users\\BOB~1.EXAMPLE\\...``) and
    ``resolve()`` rewrites it to the long name, so every ordinary short-path
    directory would look "redirected".

    Known limitation (deliberate): on platforms where the *system* itself
    redirects a well-known ancestor -- macOS ``/var`` and ``/tmp`` are symlinks
    -- a legitimate path under them is refused too. Those platforms are not
    supported by this preview; on Windows and Linux no such ancestor exists.
    """
    parts = path.parts
    for depth in range(1, len(parts) + 1):
        component = pathlib.Path(*parts[:depth])
        kind = _redirect_kind(component)
        if kind is not None:
            return component, kind
    return None


def validate_state_dir(state_dir: str | pathlib.Path) -> pathlib.Path:
    """Vet the operator's state directory before anything is written into it.

    Three refusals, and deliberately no repair:

    * a **relative** path, because "which directory am I in" must not decide
      where a credential lands;
    * a path that **is, or sits behind, a link** -- not only a symbolic link but
      any Windows reparse point, junctions included -- because ``state_dir`` is
      where a credential is written and a link can be repointed after the
      operator has looked at it;
    * a path **inside this checkout**, because the credential file and the
      session database are local development data and must never be committable.

    Nothing here is deleted, created, or chmod-ed: a directory the operator
    already made is used as-is (``PreviewApplication`` opens it with
    ``exist_ok=True``), so a rejected path is left exactly as it was found.

    Defensive depth, not a proof: the link walk and the use of the directory are
    separate syscalls, so this narrows the window for a local attacker rather
    than closing it. It is not a TOCTOU guarantee, and it is not claimed to be
    one -- the real boundary remains "bind loopback only, and never put a
    production credential in a preview state directory".
    """
    path = pathlib.Path(state_dir)
    if not path.is_absolute():
        raise errors.ConfigError(detail=f"state_dir must be an absolute path: {state_dir!r}")

    redirect = find_redirecting_component(path)
    if redirect is not None:
        component, kind = redirect
        raise errors.ConfigError(
            detail=(
                f"state_dir must not be, and must not sit behind, a link: {component} "
                f"is a {kind}. Point --state-dir at a real directory whose every parent "
                "is also a real directory, so the credential path cannot be redirected "
                "between inspection and use."
            )
        )

    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise errors.ConfigError(
            detail=(
                f"state_dir must live outside the repository ({REPO_ROOT}): {path}. "
                "The credential file and the session database are local development "
                "data and must never be committable."
            )
        )

    return path


def _preview_query(environ: Mapping[str, Any]) -> dict[str, str]:
    """The Atlas endpoint's recognised query parameters, each at most once.

    Same rule as :meth:`PreviewApplication._cookie_guard`: a parameter supplied
    twice has a meaning that depends on which reader looks at it, so it is
    refused rather than resolved to one of them. Only the names in
    :data:`dev.atlas_preview.QUERY_PARAMS` are read at all — everything else in
    the query string is ignored, which is the point. ``role``, ``key_id``,
    ``label`` and friends are *not* parameters here, so nothing they say can
    reach the authorisation decision.
    """
    raw = urllib.parse.parse_qs(
        str(environ.get("QUERY_STRING") or ""), keep_blank_values=True
    )
    resolved: dict[str, str] = {}
    for name in atlas_preview.QUERY_PARAMS:
        values = raw.get(name)
        if not values:
            continue
        if len(values) > 1:
            raise errors.InvalidRequest(
                detail=f"the {name!r} parameter was supplied more than once"
            )
        value = values[0]
        if not value.strip():
            raise errors.InvalidRequest(detail=f"the {name!r} parameter was empty")
        resolved[name] = value
    return resolved


def _calls_query(environ: Mapping[str, Any]) -> dict[str, str]:
    """The self-calls endpoint's recognised query parameters, each at most once.

    Same rules as :func:`_preview_query`, and the same point: only
    :data:`dev.atlas_calls.QUERY_PARAMS` is read. ``subject``, ``principal``,
    ``role``, ``x_principal`` are **not** parameters of this endpoint, so nothing
    they say can reach the authorisation decision -- the only subject this
    endpoint knows is the one the Session carries.
    """
    raw = urllib.parse.parse_qs(
        str(environ.get("QUERY_STRING") or ""), keep_blank_values=True
    )
    resolved: dict[str, str] = {}
    for name in atlas_calls.QUERY_PARAMS:
        values = raw.get(name)
        if not values:
            continue
        if len(values) > 1:
            raise errors.InvalidRequest(
                detail=f"the {name!r} parameter was supplied more than once"
            )
        value = values[0]
        if not value.strip():
            raise errors.InvalidRequest(detail=f"the {name!r} parameter was empty")
        resolved[name] = value
    return resolved


def _modules_query(environ: Mapping[str, Any]) -> dict[str, str]:
    """The module catalogue's recognised query parameters, each at most once.

    Only :data:`dev.portal_modules.QUERY_PARAMS` is read. ``subject`` and
    ``role`` are not parameters here either, so the only subject this endpoint
    knows is the one the Session carries.
    """
    raw = urllib.parse.parse_qs(
        str(environ.get("QUERY_STRING") or ""), keep_blank_values=True
    )
    resolved: dict[str, str] = {}
    for name in portal_modules.QUERY_PARAMS:
        values = raw.get(name)
        if not values:
            continue
        if len(values) > 1:
            raise errors.InvalidRequest(
                detail=f"the {name!r} parameter was supplied more than once"
            )
        value = values[0]
        if not value.strip():
            raise errors.InvalidRequest(detail=f"the {name!r} parameter was empty")
        resolved[name] = value
    return resolved


class PreviewApplication:
    """The preview container. Callable as a WSGI application."""

    def __init__(
        self,
        state_dir: str | pathlib.Path,
        *,
        local_http_dev: bool,
        mock_feishu: bool = False,
        clock: Callable[[], float] | None = None,
        initialize: bool = True,
        atlas_grants: Iterable[atlas_preview.AtlasPreviewGrant] = (),
        portal_grants: Iterable[portal_modules.PortalModuleGrant] = (),
        self_calls_grant: atlas_calls.SelfCallsGrant | None = None,
        self_calls_content_consent: Iterable[str] = (),
    ) -> None:
        # Fail closed on the one flag that weakens a production rule. There is
        # no configuration file, no environment variable and no default that can
        # turn this on: the caller has to say it out loud.
        if not local_http_dev:
            raise errors.ConfigError(
                detail=(
                    "the preview serves plain HTTP; pass local_http_dev=True (CLI: "
                    "--local-http-dev) to state explicitly that this is a local "
                    "development run. Without it production requires HTTPS and a "
                    "Secure cookie, so the preview refuses to start."
                )
            )

        state_path = validate_state_dir(state_dir)

        self.state_dir = state_path
        self.local_http_dev = True
        self.mock_feishu = bool(mock_feishu)
        self._port: int | None = None

        # Zero grants by default, and nothing derives one. A preview that
        # started with the administrator already able to read usage would be
        # handing out a permission nobody asked for; ``role`` is not consulted
        # anywhere in this path.
        self.atlas_grants = atlas_preview.AtlasPreviewGrants(atlas_grants)

        # The Portal module catalogue's own authorisation surface. Same shape and
        # same rule as the Atlas grants: absent by default, keyed on the account
        # id the Session carries, unreachable from any request, and never implied
        # by a role.
        self.portal_grants = portal_modules.PortalModuleGrants(portal_grants)

        # The self-calls view's own, separate authorisation surface. Same rule as
        # the Atlas grants above: absent by default, keyed on the account id the
        # Session carries, and unreachable from any request.
        #
        # It is ONE grant rather than a set, and it carries its key directory
        # with it. Both halves matter: the directory decides who a key belongs
        # to, so if it were rebuilt from whoever happened to be calling, every
        # granted account would "own" the same demo keys and the cross-user
        # boundary would invert. See dev.atlas_calls.SelfCallsGrant.
        #
        # The read grant and the content opt-in are two *different* decisions on
        # purpose. Being allowed to read your own call metadata must not imply
        # that a "content access" opt-in exists on your record, and vice versa --
        # so they are two parameters, not one flag.
        self.self_calls_grant = self_calls_grant
        self.self_calls_consent = atlas_calls.ContentConsentRegistry(
            self_calls_content_consent
        )

        # Hermetic configuration: an *empty* environment. A stray
        # LLM_LAB_FEISHU_APP_ID, LLM_LAB_ALLOW_INSECURE_REDIRECT_URI or
        # LLM_LAB_TRUSTED_ORIGINS in the operator's shell must not be able to
        # reach the preview, and no real provider credential may be picked up by
        # accident.
        cfg = AuthConfig.from_env({}, state_dir=state_path)
        cfg = cfg.replace(cookie_secure=not local_http_dev)
        if self.mock_feishu:
            cfg = cfg.replace(
                feishu_app_id=MOCK_APP_ID,
                feishu_app_secret=MOCK_APP_SECRET,
                feishu_redirect_uri=MOCK_REDIRECT_URI,
                feishu_tenant_allowlist=frozenset({MOCK_TENANT_KEY}),
            )

        provider = MockFeishuProvider(self._mock_base_url) if self.mock_feishu else None
        self.auth = Application(cfg, provider=provider, clock=clock or time.time)

        self._demo_html = self._render_demo()
        if initialize:
            try:
                state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError:  # pragma: no cover - the database will report it
                pass
            self.auth.initialize()

    # --- wiring --------------------------------------------------------------

    @property
    def cfg(self) -> AuthConfig:
        return self.auth.cfg

    @property
    def port(self) -> int | None:
        """The port the server actually bound, once :func:`make_server` has run."""
        return self._port

    def bind_port(self, port: int) -> None:
        """Record the port the server actually bound. Needed by the mock provider."""
        self._port = int(port)

    def _mock_base_url(self) -> str:
        if self._port is None:
            raise errors.ConfigError(detail="the preview server is not bound to a port yet")
        return f"http://{DEFAULT_HOST}:{self._port}"

    # --- WSGI ----------------------------------------------------------------

    def __call__(self, environ, start_response):
        path = str(environ.get("PATH_INFO") or "/")
        guard = self._guards(environ)
        if guard is not None:
            return _send(start_response, guard)

        if path == AUTH_PREFIX or path.startswith(AUTH_PREFIX + "/"):
            # The product's own router, unmodified, still owning /auth/*.
            return self.auth(environ, start_response)

        return _send(start_response, self.handle(environ))

    # --- guards --------------------------------------------------------------

    def _guards(self, environ: Mapping[str, Any]) -> _Response | None:
        return self._host_guard(environ) or self._cookie_guard(environ)

    def _host_guard(self, environ: Mapping[str, Any]) -> _Response | None:
        """Refuse any request not addressed, exactly, to this loopback server.

        Defence in depth: the socket is already bound to 127.0.0.1, but a
        container or a tunnel in front of it could rewrite the authority, and a
        request naming a public hostname is not a request this module should ever
        answer. It is refused rather than served, and refused before any cookie
        or CSRF value is looked at.

        "Exactly" is load-bearing. The whole field value is parsed (see
        :func:`parse_host_authority`); nothing is trimmed off the end, nothing is
        decoded, and a stated port is compared with the port this server is
        really listening on. The value is never echoed back in the refusal.

        ``SERVER_NAME`` is deliberately **not** consulted. ``wsgiref`` fills it
        from ``socket.getfqdn()``, which is ``localhost`` on some hosts — using
        it as a fallback would let a request that named no authority at all be
        treated as one that had.
        """
        raw = str(environ.get("HTTP_HOST") or "")
        parsed = parse_host_authority(raw)
        if parsed is None:
            return self._refuse_host(
                "This preview answers only to the loopback hosts it serves, "
                "named exactly, with no surrounding text.",
            )

        host, port = parsed
        # Belt and braces: the pattern above is the header-side allowlist, and
        # this is the same allowlist the socket binds against. Both have to
        # agree, so a future edit to either one cannot widen the other.
        if host not in LOOPBACK_HOSTS:
            return self._refuse_host(
                "This preview answers only to the loopback hosts it serves.",
            )

        if port is not None:
            listening = self._listening_port(environ)
            if listening is None or port != listening:
                return self._refuse_host(
                    "The port in the Host header is not the port this preview is "
                    "listening on.",
                )
        return None

    @staticmethod
    def _listening_port(environ: Mapping[str, Any]) -> int | None:
        """The port the request actually arrived on, or ``None`` if unknowable.

        ``wsgiref`` sets ``SERVER_PORT`` from the **bound socket** (``server_bind``
        calls ``setup_environ``), so this is the real listening port and not a
        second copy of what the client said. ``None`` means the WSGI application
        is being driven without a socket — the caller cannot be verified, so the
        port check is not skipped, it is failed.
        """
        raw = environ.get("SERVER_PORT")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _refuse_host(detail: str) -> _Response:
        return _json_error(400, "invalid_request", detail)

    def _cookie_guard(self, environ: Mapping[str, Any]) -> _Response | None:
        """Refuse a request that repeats a cookie name.

        ``Cookie: a=1; a=2`` is not a request with two cookies, it is a request
        whose meaning depends on which parser reads it — the product's parser
        takes the last one, a proxy might take the first. Nothing legitimate
        produces it, so it is refused instead of being resolved.
        """
        raw = str(environ.get("HTTP_COOKIE") or "")
        seen: set[str] = set()
        for chunk in raw.split(";"):
            name = chunk.partition("=")[0].strip()
            if not name:
                continue
            if name in seen:
                return _json_error(
                    400,
                    "invalid_request",
                    "A cookie name was supplied more than once; the request is refused "
                    "rather than resolved to one of them.",
                )
            seen.add(name)
        return None

    # --- routing -------------------------------------------------------------

    def handle(self, environ: Mapping[str, Any]) -> _Response:
        path = str(environ.get("PATH_INFO") or "/")
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()

        if self.mock_feishu and path == MOCK_AUTHORIZE_PATH:
            if method != "GET":
                return _json_error(405, "method_not_allowed", "Method not allowed.")
            return self._mock_authorize(environ)

        if path == atlas_preview.ATLAS_USAGE_PATH:
            return self._atlas_usage(environ, method)

        if path == atlas_calls.CALLS_PATH:
            return self._calls(environ, method)

        if path == portal_modules.MODULES_PATH:
            return self._modules(environ, method)

        if path == CALLS_DEEP_LINK:
            # 302 rather than 200-with-the-same-document: the shell is the one
            # place the view exists, and a redirect makes that a fact of the URL
            # space instead of a convention.
            return _Response(302, headers=[("Location", CALLS_VIEW_HASH)])

        if path in STATIC_ROUTES:
            return self._static(method, path)

        if path == FAVICON_PATH:
            return _Response(204)

        if path in PROTECTED_PAGES:
            return self._protected(environ, path)

        return _json_error(404, "not_found", "Not found.")

    # --- static surface ------------------------------------------------------

    def _static(self, method: str, path: str) -> _Response:
        if method not in ("GET", "HEAD"):
            return _json_error(405, "method_not_allowed", "Method not allowed.")
        filename, content_type, csp = STATIC_ROUTES[path]
        try:
            body = (WEB_AUTH_DIR / filename).read_bytes()
        except OSError:
            return _json_error(
                500,
                "server_misconfigured",
                "A login page asset is missing from this checkout.",
            )
        headers = [("Content-Type", content_type)]
        if csp:
            headers.append(("Content-Security-Policy", csp))
        return _Response(200, headers=headers, body=body)

    # --- the gated DEMO ------------------------------------------------------

    def _protected(self, environ: Mapping[str, Any], path: str) -> _Response:
        """Gate ``/``, ``/atlas`` and ``/calls``.

        The session check is the product's own: ``AuthService.current_session``
        plus ``Application.authorize`` — the same two calls ``/auth/me`` uses.
        There is no parallel notion of "logged in" in this module, and no
        client-side check is trusted for anything.

        Past that, each page is gated on the **same authority as its data**:

        * ``/`` and ``/atlas`` show the demo shell, so they need an account that
          can see something — an administrator, or a member with a project;
        * ``/calls`` shows **your own** call log, so it needs the self-calls
          grant. Borrowing the project rule here would be a second, weaker path
          into the same page — and gating it on ``is_admin`` would hand every
          administrator a personal view nobody granted them.
        """
        cookies = parse_cookies(environ.get("HTTP_COOKIE"))
        try:
            session = self.auth.authorize(
                self.auth.service.current_session(cookies.get(self.cfg.cookie_name))
            )
        except (errors.NotAuthenticated, errors.AccountDisabled):
            # Not signed in, signed out, expired, revoked, or disabled: in every
            # one of those cases the answer is the login page, never content.
            return self._login_redirect(path)

        if not session.user.is_admin and not self.auth.service.projects_for(session.user):
            return _html_page(
                403,
                "尚未获得项目授权",
                [
                    "你的身份已通过校验，但这个账号还没有任何已授权的项目，因此没有可展示的内容。",
                    "请联系管理员完成项目授权；授权生效后重新登录即可。",
                ],
            )

        return _Response(
            200,
            headers=[
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", DEMO_CSP),
            ],
            body=self._demo_html,
        )

    def _login_redirect(self, path: str) -> _Response:
        query = urllib.parse.urlencode({"next": path})
        return _Response(302, headers=[("Location", f"{self.cfg.login_error_path}?{query}")])

    # --- the read-only Atlas endpoint ----------------------------------------

    def _atlas_usage(self, environ: Mapping[str, Any], method: str) -> _Response:
        """``GET /api/atlas/usage`` — who you are, then what you may read.

        Two independent gates, in this order, and neither substitutes for the
        other:

        1. **A live Session.** ``AuthService.current_session`` is the product's
           own resolver, so an unknown, expired, revoked, signed-out or
           disabled account is refused exactly as it is everywhere else. No
           cookie was tampered with to get here: it is the same call
           ``/auth/me`` makes.
        2. **An explicit, server-side grant.** ``atlas_grants`` is keyed on the
           **subject the Session carries** — never on ``role``, never on
           anything the request said. An administrator with no grant gets 403;
           a member with a grant gets exactly the projects that grant names.

        The client's own words about itself (``X-Project``, ``role``,
        ``key_id``, ``label``, ``X-Forwarded-User``) are never read: only
        :data:`dev.atlas_preview.QUERY_PARAMS` is parsed, and even those can
        only *narrow* what the grant already allows — ``backend.atlas`` raises
        ``AccessDenied`` for a project outside the authorised set.
        """
        # A read-only endpoint. Stated before anything else so a write is
        # refused by method, and no write ever reaches the aggregation path.
        if method != "GET":
            return _json_error(405, "method_not_allowed", "Method not allowed.")

        cookies = parse_cookies(environ.get("HTTP_COOKIE"))
        try:
            session = self.auth.authorize(
                self.auth.service.current_session(cookies.get(self.cfg.cookie_name))
            )
        except errors.AuthError as exc:
            # The product's own taxonomy carries the status: 401 for a missing,
            # expired or revoked session, 403 for a disabled account.
            return _json_error(exc.http_status, exc.code, exc.message)

        # Checked *before* the query string is parsed, so a caller without a
        # grant always learns the same thing (403) regardless of what it sent.
        grant = self.atlas_grants.for_subject(session.user.id)
        if grant is None:
            return _json_error(403, "forbidden", errors.Forbidden.message)

        try:
            query = _preview_query(environ)
        except errors.AuthError as exc:
            return _json_error(exc.http_status, exc.code, exc.message)

        try:
            payload = atlas_preview.build_payload(
                user=session.user,
                grant=grant,
                observed_at=self.auth.clock(),
                requested_project=query.get("project"),
                requested_subject=query.get("subject"),
                bucket=query.get("bucket") or atlas_preview.DEFAULT_BUCKET,
            )
        except atlas_contract.AccessDenied:
            # A project outside the grant, or a subject that is not the
            # session's. Refused — never quietly widened to the session's own.
            return _json_error(403, "forbidden", errors.Forbidden.message)
        except (
            atlas_contract.InvalidRequest,
            atlas_contract.InvalidWindow,
            atlas_contract.InvalidBucket,
        ):
            # Deliberately generic: the contract's messages quote the supplied
            # value back, and this endpoint does not repeat client input.
            return _json_error(400, "invalid_request", errors.InvalidRequest.message)
        except atlas_contract.InputLimitExceeded:
            return _json_error(413, "payload_too_large", errors.PayloadTooLarge.message)
        except atlas_contract.ContractError:
            # A DTO that fails its own self-check is a bug here, not a client
            # error. Say nothing about which invariant failed.
            return _json_error(500, "server_error", "Atlas aggregation failed.")

        return _Response(
            200,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Security-Policy", JSON_CSP),
            ],
            body=json.dumps({"ok": True, "data": payload}, separators=(",", ":")).encode(
                "utf-8"
            ),
        )

    def _calls(self, environ: Mapping[str, Any], method: str) -> _Response:
        """``GET /api/atlas/calls`` -- your own calls, and only your own.

        Three gates, in this order. None of them substitutes for another:

        1. **A live Session.** ``AuthService.current_session`` is the product's
           own resolver, so signing out, expiring, revoking or disabling an
           account is refused here exactly as it is at ``/auth/me`` -- a
           signed-out browser gets the product's own 401, never a 403 and never
           an empty list.
        2. **An explicit, server-side grant.** ``self_calls_subjects`` holds
           account ids, never roles. An administrator with no entry gets 403.
        3. **Server-side key ownership.** ``build_dto`` attributes a row only
           when the *trusted directory* says the key's ``principal`` is this
           subject -- see :func:`backend.atlas.identity.resolve_own_call_scope`.
           Asking for a colleague's key, a key that does not exist, or one of
           your own revoked keys is the *same* 403.

        Content is never returned, whatever the opt-in state says. See
        :mod:`backend.atlas.calls`.
        """
        if method != "GET":
            return _json_error(405, "method_not_allowed", "Method not allowed.")

        cookies = parse_cookies(environ.get("HTTP_COOKIE"))
        try:
            session = self.auth.authorize(
                self.auth.service.current_session(cookies.get(self.cfg.cookie_name))
            )
        except errors.AuthError as exc:
            return _json_error(exc.http_status, exc.code, exc.message)

        # Checked *before* the query string is parsed, so a caller without a grant
        # always learns the same thing (403) regardless of what it sent. The
        # comparison is to the configured subject itself, not to membership in a
        # set: only one account has a key directory here.
        subject = session.user.id
        grant = self.self_calls_grant
        if grant is None or grant.subject != subject:
            return _json_error(403, "forbidden", errors.Forbidden.message)

        try:
            query = _calls_query(environ)
        except errors.AuthError as exc:
            return _json_error(exc.http_status, exc.code, exc.message)

        try:
            limit = (
                atlas_calls.parse_limit(query["limit"])
                if "limit" in query
                else atlas_contract.CALLS_DEFAULT_LIMIT
            )
            window_from = (
                atlas_calls.parse_ts(query["from"], field="from")
                if "from" in query
                else None
            )
            window_to = (
                atlas_calls.parse_ts(query["to"], field="to") if "to" in query else None
            )
            before_ts = (
                atlas_calls.parse_ts(query["before_ts"], field="before_ts")
                if "before_ts" in query
                else None
            )
        except ValueError:
            # Deliberately generic: these messages quote the supplied value back,
            # and this endpoint does not repeat client input.
            return _json_error(400, "invalid_request", errors.InvalidRequest.message)

        try:
            payload = atlas_calls.build_payload(
                user=session.user,
                grant=grant,
                observed_at=self.auth.clock(),
                requested_key=query.get("key"),
                outcome=query.get("outcome") or "all",
                model=query.get("model"),
                limit=limit,
                window_from=window_from,
                window_to=window_to,
                before_ts=before_ts,
                before_call_id=query.get("before_call_id"),
                content_consent=self.self_calls_consent.state_for(subject),
            )
        except atlas_contract.AccessDenied:
            # Not your key, no such key, or your own revoked key: one refusal.
            return _json_error(403, "forbidden", errors.Forbidden.message)
        except (
            atlas_contract.InvalidRequest,
            atlas_contract.InvalidWindow,
            atlas_contract.InvalidBucket,
        ):
            return _json_error(400, "invalid_request", errors.InvalidRequest.message)
        except atlas_contract.InputLimitExceeded:
            return _json_error(413, "payload_too_large", errors.PayloadTooLarge.message)
        except atlas_contract.ContractError:
            return _json_error(500, "server_error", "Call-log aggregation failed.")

        return _Response(
            200,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Security-Policy", JSON_CSP),
            ],
            body=json.dumps({"ok": True, "data": payload}, separators=(",", ":")).encode(
                "utf-8"
            ),
        )

    def _modules(self, environ: Mapping[str, Any], method: str) -> _Response:
        """``GET /api/atlas/modules`` -- what each module may honestly say to you.

        Same three gates as :meth:`_calls`, and the same reasoning: a live
        Session, an explicit server-side grant keyed on the account id, and then
        a DTO whose every claim was decided server-side. The ``visibility``
        parameter only chooses between two subsets the server already computed;
        it cannot widen either.

        The deployment facts behind this response are fixed in
        :data:`dev.portal_modules.PREVIEW_FACTS` and describe *this* build --
        no gateway, no NVML, no dashboards. Nothing on the request path, and no
        CLI flag, can change them.
        """
        if method != "GET":
            return _json_error(405, "method_not_allowed", "Method not allowed.")

        cookies = parse_cookies(environ.get("HTTP_COOKIE"))
        try:
            session = self.auth.authorize(
                self.auth.service.current_session(cookies.get(self.cfg.cookie_name))
            )
        except errors.AuthError as exc:
            return _json_error(exc.http_status, exc.code, exc.message)

        grant = self.portal_grants.for_subject(session.user.id)
        if grant is None:
            return _json_error(403, "forbidden", errors.Forbidden.message)

        try:
            query = _modules_query(environ)
        except errors.AuthError as exc:
            return _json_error(exc.http_status, exc.code, exc.message)

        try:
            visibility = (
                portal_modules.parse_visibility(query["visibility"])
                if "visibility" in query
                else "private"
            )
        except ValueError:
            return _json_error(400, "invalid_request", errors.InvalidRequest.message)

        try:
            payload = portal_modules.build_payload(
                user=session.user,
                grant=grant,
                observed_at=self.auth.clock(),
                visibility=visibility,
            )
        except atlas_contract.AccessDenied:
            return _json_error(403, "forbidden", errors.Forbidden.message)
        except atlas_contract.InvalidRequest:
            return _json_error(400, "invalid_request", errors.InvalidRequest.message)
        except atlas_contract.ContractError:
            return _json_error(500, "server_error", "Module catalogue failed.")

        return _Response(
            200,
            headers=[
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Security-Policy", JSON_CSP),
            ],
            body=json.dumps({"ok": True, "data": payload}, separators=(",", ":")).encode(
                "utf-8"
            ),
        )

    def _render_demo(self) -> bytes:
        """Load the design prototype and stamp it, unmistakably, as a demo."""
        try:
            source = DEMO_SOURCE.read_text(encoding="utf-8")
        except OSError:
            raise errors.ConfigError(
                detail=f"the demo prototype is missing from this checkout: {DEMO_SOURCE}"
            ) from None

        banner = DEMO_BANNER_TEMPLATE.format(banner_id=DEMO_BANNER_ID)
        marker = re.search(r"<body[^>]*>", source)
        if marker is None:  # pragma: no cover - the prototype always has one
            raise errors.ConfigError(detail="the demo prototype has no <body> element")
        return (source[: marker.end()] + banner + source[marker.end() :]).encode("utf-8")

    # --- the offline mock consent page ---------------------------------------

    def _mock_authorize(self, environ: Mapping[str, Any]) -> _Response:
        query = urllib.parse.parse_qs(str(environ.get("QUERY_STRING") or ""))
        state = (query.get("state") or [""])[0]
        target = "/auth/feishu/callback?" + urllib.parse.urlencode(
            {"state": state, "code": mock_code_for(state) if state else ""}
        )
        body = MOCK_PAGE_TEMPLATE.format(
            state=html.escape(state or "(missing)"),
            target=html.escape(target, quote=True),
        ).encode("utf-8")
        return _Response(
            200,
            headers=[
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Security-Policy", MOCK_CSP),
            ],
            body=body,
        )


def _send(start_response, response: _Response) -> list[bytes]:
    headers = [
        ("Content-Length", str(len(response.body))),
        *_SECURITY_HEADERS,
        *response.headers,
    ]
    start_response(f"{response.status} {_reason(response.status)}", headers)
    return [response.body]


# --- server ------------------------------------------------------------------


class ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    """Threaded, so a browser's parallel connections cannot deadlock the preview.

    A real browser opens several connections at once and keeps them open; a
    single-threaded server would answer the first and block on it while the
    browser waits on the second.
    """

    daemon_threads = True
    allow_reuse_address = True


class PreviewRequestHandler(wsgiref.simple_server.WSGIRequestHandler):
    """A handler that refuses a request whose ``Host`` is missing or repeated.

    This has to happen *here*, below the application, for two reasons:

    ``wsgiref`` flattens the headers into ``environ`` before the application
    runs, joining repeated names with a comma — two ``Host: 127.0.0.1:59008``
    headers become the single value ``"127.0.0.1:59008,127.0.0.1:59008"`` and
    ``Host: 127.0.0.1:59008`` followed by an empty ``Host:`` becomes
    ``"127.0.0.1:59008,"``. By the time the WSGI application sees the request it
    cannot tell that two headers arrived, and RFC 7230 §5.4 makes a request with
    more than one ``Host`` invalid however it is spelled. So the check runs on
    the parsed header list, and the request never reaches the application.

    ``wsgiref`` also pins ``protocol_version`` to ``HTTP/1.0``, which silently
    disables the base handler's own "Host is required" check (the base class only
    applies it when ``protocol_version`` is at least ``HTTP/1.1``) *even for an
    HTTP/1.1 request line*. That check is reinstated here for every request
    version: for HTTP/1.1 because the RFC requires the header, and for HTTP/1.0 —
    where the RFC makes it optional — because this container has no other way to
    learn the authority it is being asked for, and a request it cannot check is a
    request it must not answer. ``docs/preview-host-security.md`` records both
    decisions.

    Only the ``Host`` *structure* is judged here (zero or more than one). The
    *value* is judged by :meth:`PreviewApplication._host_guard`, which is the one
    place the loopback allowlist lives, so a request that gets past this handler
    still faces the same check as one that never needed it.
    """

    def parse_request(self) -> bool:  # type: ignore[override]
        if not super().parse_request():
            return False

        hosts = self.headers.get_all("Host") or []
        if len(hosts) == 1:
            return True

        if len(hosts) > 1:
            detail = (
                "More than one Host header was supplied; the request is refused "
                "rather than resolved to one of them."
            )
        else:
            detail = "The request carried no Host header, so its authority cannot be checked."
        self._refuse(detail)
        # A response has been sent; ``handle_one_request`` returns on False.
        return False

    def _refuse(self, detail: str) -> None:
        """Answer with the same 400 envelope the WSGI guards use."""
        body = json.dumps(
            {"ok": False, "error": {"code": "invalid_request", "message": detail}},
            separators=(",", ":"),
        ).encode("utf-8")

        self.send_response(400, "Bad Request")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in _SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        # A refusal is never a request to keep the connection for.
        self.close_connection = True
        self.wfile.write(body)


def build_preview_application(
    state_dir: str | pathlib.Path,
    *,
    local_http_dev: bool,
    mock_feishu: bool = False,
    clock: Callable[[], float] | None = None,
    atlas_grants: Iterable[atlas_preview.AtlasPreviewGrant] = (),
    portal_grants: Iterable[portal_modules.PortalModuleGrant] = (),
    self_calls_grant: atlas_calls.SelfCallsGrant | None = None,
    self_calls_content_consent: Iterable[str] = (),
) -> PreviewApplication:
    return PreviewApplication(
        state_dir,
        local_http_dev=local_http_dev,
        mock_feishu=mock_feishu,
        clock=clock,
        atlas_grants=atlas_grants,
        portal_grants=portal_grants,
        self_calls_grant=self_calls_grant,
        self_calls_content_consent=self_calls_content_consent,
    )


def resolve_account_id(app: PreviewApplication, raw: str, *, flag: str) -> str:
    """``SUBJECT`` -> an account id, or a refusal.

    ``SUBJECT`` is either the local administrator's username -- resolved through
    the store's own derivation, so the operator never has to read an id out of
    the database -- or a full account id (``local:…`` / ``feishu:…``), taken
    verbatim. Anything else is refused rather than stored as an id that could
    never match anything.
    """
    subject = raw.strip()
    if not subject:
        raise errors.ConfigError(detail=f"{flag} {raw!r} names no subject")

    if subject.startswith(("local:", "feishu:")):
        return subject
    if subject == app.cfg.admin_username:
        return app.auth.service.db.ensure_local_user(subject).id
    raise errors.ConfigError(
        detail=(
            f"{flag} {raw!r}: {subject!r} is neither the local administrator's "
            f"username ({app.cfg.admin_username!r}) nor a full account id "
            "('local:…' or 'feishu:…')"
        )
    )


def resolve_demo_grant(app: PreviewApplication, raw: str) -> atlas_preview.AtlasPreviewGrant:
    """``SUBJECT`` -> a server-side Atlas grant over the demo project set.

    ``SUBJECT`` is either the local administrator's username — resolved through
    the store's own derivation, so the operator never has to read an id out of
    the database — or a full account id (``local:…`` / ``feishu:…``), taken
    verbatim. Anything else is refused rather than stored as an id that could
    never match anything.

    There is deliberately **no** way to name projects here. An account id
    contains a colon of its own, so a ``SUBJECT:PROJECT`` grammar would have to
    guess where the subject ends — and guessing at an authorisation input is
    exactly what this codebase refuses to do everywhere else. The grant
    therefore covers the demo project set and nothing else: this flag can only
    ever *open* the synthetic endpoint, never widen anyone's reach beyond the
    fixture. Narrowing one subject is an in-process configuration action
    (:meth:`dev.atlas_preview.AtlasPreviewGrants.set`), not a command-line one.

    This runs on the startup path only. Nothing here is reachable from a
    request, which is what makes the grant server-trusted.
    """
    account_id = resolve_account_id(app, raw, flag="--atlas-demo-grant")

    return atlas_preview.AtlasPreviewGrant(
        subject=account_id, projects=atlas_preview.DEMO_PROJECTS
    )


def make_server(
    app: PreviewApplication, host: str = DEFAULT_HOST, port: int = 0
):
    """Bind the preview. Refuses anything that is not a safe local start.

    Two refusals, both before a socket is opened:

    * a non-loopback bind address, because this module must never be reachable
      from another machine;
    * a state directory with no administrator credential, because there is no
      default password, no ``--password`` flag and nothing to fall back to. The
      message names the interactive command that creates one.
    """
    if host not in LOOPBACK_HOSTS:
        raise errors.ConfigError(
            detail=f"the preview only binds to loopback; {host!r} is refused"
        )

    status = bootstrap.admin_credential_status(app.cfg)
    if status.get("state") != local_admin.STATE_ACTIVE:
        raise errors.ConfigError(
            detail=(
                f"no administrator credential in {app.cfg.state_dir} "
                f"(state={status.get('state')!r}). Create one interactively first:\n"
                f"    python -m backend.auth.bootstrap init --state-dir {app.cfg.state_dir}"
            )
        )

    server = wsgiref.simple_server.make_server(
        host,
        port,
        app,
        server_class=ThreadingWSGIServer,
        handler_class=PreviewRequestHandler,
    )
    app.bind_port(server.server_address[1])
    return server


# --- command line ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dev.preview_auth",
        description=(
            "Serve a loopback-only preview of the Portal sign-in flow with a "
            "synthetic DEMO page behind it. Development use only: plain HTTP, "
            "no gateway, no real data."
        ),
        epilog=(
            "First run is two commands, and no password ever appears on a command\n"
            "line -- the first one prompts for it interactively:\n"
            "    python -m backend.auth.bootstrap init --state-dir <ABSOLUTE DIR>\n"
            "    python -m dev.preview_auth --local-http-dev --state-dir <ABSOLUTE DIR>"
            " [--mock-feishu]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--local-http-dev",
        action="store_true",
        help=(
            "required: state that this is a local plain-HTTP development run, "
            "which is the only case in which the Secure cookie attribute is dropped"
        ),
    )
    parser.add_argument(
        "--state-dir",
        required=True,
        help=(
            "required: absolute directory (outside this checkout) holding the "
            "credential file and the auth database. It must already contain an "
            "administrator, created with `python -m backend.auth.bootstrap init "
            "--state-dir <dir>`. There is deliberately no temporary-directory "
            "default: a brand-new directory has no administrator, so a run that "
            "invented one could never be signed into."
        ),
    )
    parser.add_argument(
        "--mock-feishu",
        action="store_true",
        help="enable the offline Feishu mock instead of the honest 'unavailable' state",
    )
    parser.add_argument("--port", type=int, default=0, help="0 picks a free ephemeral port")
    parser.add_argument("--host", default=DEFAULT_HOST, help="loopback address only")
    parser.add_argument(
        "--atlas-demo-grant",
        action="append",
        default=[],
        metavar="SUBJECT",
        help=(
            "development only, repeatable: let one account read the synthetic "
            "GET /api/atlas/usage endpoint over the demo project set. SUBJECT "
            "is either the local administrator's username or a full account id "
            "('local:…' / 'feishu:…'). Projects cannot be named here: an "
            "account id contains a colon of its own, so the grant covers the "
            "demo projects and nothing else, and this flag can only open the "
            "endpoint, never widen anyone's reach. There is no default and no "
            "role-based fallback: without this flag every request to that "
            "endpoint is refused with 403, administrator included."
        ),
    )
    parser.add_argument(
        "--calls-self-grant",
        action="append",
        default=[],
        metavar="SUBJECT",
        help=(
            "development only: let one account read the synthetic "
            "GET /api/atlas/calls endpoint, which lists that account's own "
            "calls over the demo key set. SUBJECT is either the local "
            "administrator's username or a full account id ('local:…' / "
            "'feishu:…'). At most one account may be named: the demo fixture "
            "covers one principal, and a second account would get an empty "
            "view that reads like 'you have no calls' rather than 'the demo "
            "has no rows for you'. There is no default and no role-based "
            "fallback: without this flag that endpoint answers 403, "
            "administrator included."
        ),
    )
    parser.add_argument(
        "--portal-modules-grant",
        action="append",
        default=[],
        metavar="SUBJECT",
        help=(
            "development only, repeatable: let one account read the synthetic "
            "GET /api/atlas/modules catalogue. SUBJECT is either the local "
            "administrator's username or a full account id ('local:…' / "
            "'feishu:…'). This grants portal:modules:read ONLY -- the right to "
            "be told which modules exist and why each is or is not usable. It "
            "opens no native console; extra scopes are an in-process "
            "configuration action, not a command-line one. Without this flag "
            "that endpoint answers 403, administrator included."
        ),
    )
    parser.add_argument(
        "--calls-content-consent",
        action="append",
        default=[],
        metavar="SUBJECT",
        help=(
            "development only, repeatable: record a content-access opt-in for "
            "an account, so the preview can show the 'opt-in granted' state. "
            "This only changes a status field: prompt/completion text is not "
            "collected by this pipeline and is never returned, whatever this "
            "flag says."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    if not args.local_http_dev:
        print(
            "error: refusing to start without --local-http-dev. The preview serves plain "
            "HTTP; production requires HTTPS and a Secure cookie.",
            file=sys.stderr,
        )
        return 2

    # No fallback: ``--state-dir`` is required, so there is no branch here that
    # could invent a directory. A guessed path could not contain an
    # administrator anyway, and minting one would leave litter behind on the
    # way to a guaranteed failure.
    try:
        app = build_preview_application(
            args.state_dir, local_http_dev=True, mock_feishu=args.mock_feishu
        )
        # Resolved here rather than inside the container: naming an account is
        # an operator action, and the store is what knows which account a name
        # belongs to.
        for raw in args.atlas_demo_grant:
            app.atlas_grants.set(resolve_demo_grant(app, raw))

        # The self-calls surface is resolved the same way, and for the same
        # reason: naming an account is an operator action, and only the store
        # knows which account a username belongs to.
        #
        # One account, and both halves of the grant -- subject and key directory
        # -- are built here, before the socket opens. Nothing on the request path
        # can regenerate either.
        self_calls_subjects = [
            resolve_account_id(app, raw, flag="--calls-self-grant")
            for raw in args.calls_self_grant
        ]
        if len(self_calls_subjects) > 1:
            raise errors.ConfigError(
                detail=(
                    "--calls-self-grant accepts exactly one account: the demo "
                    "fixture covers a single principal, and a second account "
                    "sharing this directory would be shown the first account's "
                    "keys instead of its own"
                )
            )
        for raw in args.portal_modules_grant:
            app.portal_grants.set(
                portal_modules.PortalModuleGrant(
                    subject=resolve_account_id(
                        app, raw, flag="--portal-modules-grant"
                    )
                )
            )

        if self_calls_subjects:
            app.self_calls_grant = atlas_calls.SelfCallsGrant.for_subject(
                self_calls_subjects[0]
            )
        for raw in args.calls_content_consent:
            app.self_calls_consent.grant(
                resolve_account_id(app, raw, flag="--calls-content-consent")
            )

        server = make_server(app, host=args.host, port=args.port)
    except errors.AuthError as exc:
        print(f"error: {exc.message} ({exc.detail or exc.code})", file=sys.stderr)
        return 2

    origin = f"http://{args.host}:{server.server_address[1]}"
    print(f"state dir:  {app.cfg.state_dir}")
    print(f"sign in:    {origin}/login")
    print(f"demo page:  {origin}/atlas   (requires a session; the reader is redirected otherwise)")
    print(f"call log:   {origin}{CALLS_DEEP_LINK}   -> 302 -> {CALLS_VIEW_HASH}")
    portal = len(app.portal_grants)
    print(
        f"modules api:{origin}{portal_modules.MODULES_PATH}   "
        + (
            f"({portal} explicit grant(s); every other account is 403)"
            if portal
            else "(no grant configured, so every request is 403)"
        )
    )
    grants = len(app.atlas_grants)
    print(
        f"atlas api:  {origin}{atlas_preview.ATLAS_USAGE_PATH}   "
        f"(synthetic preview data; "
        + (
            f"{grants} explicit grant(s); every other request is 403)"
            if grants
            else "no grants configured, so every request is 403)"
        )
    )
    print(
        f"calls api:  {origin}{atlas_calls.CALLS_PATH}   "
        "(your own calls only; "
        + (
            "one explicit self-grant; every other account is 403)"
            if app.self_calls_grant is not None
            else "no self-grant configured, so every request is 403)"
        )
    )
    print(f"feishu:     {'offline mock enabled' if args.mock_feishu else 'not configured (honest unavailable)'}")
    print("press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = [
    "DEMO_BANNER_ID",
    "DEFAULT_HOST",
    "INFO_CSP",
    "JSON_CSP",
    "LOOPBACK_HOSTS",
    "MAX_PORT",
    "MIN_PORT",
    "MockFeishuProvider",
    "PreviewApplication",
    "PreviewRequestHandler",
    "ThreadingWSGIServer",
    "build_parser",
    "build_preview_application",
    "main",
    "make_server",
    "mock_code_for",
    "parse_host_authority",
    "resolve_demo_grant",
    "validate_state_dir",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
