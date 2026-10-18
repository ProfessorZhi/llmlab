"""Harness for driving the real preview server over loopback TCP.

Design notes that matter:

* **The admin password is generated in-process and never printed.** It is
  returned to the test so the test can use it, and it never reaches stdout,
  stderr, a log line, a command line or a file.
* **A fresh state directory per server**, under the platform temp root, removed
  by the caller. Nothing is shared between tests.
* The server is built through the same seam the ``bootstrap`` CLI uses
  (``initialize_admin_password(cfg, read_password=...)``), never through a
  password preset — there is no such preset anywhere in the product.
"""

from __future__ import annotations

import http.client
import pathlib
import secrets
import shutil
import socket
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.auth import bootstrap, store
from backend.auth.config import AuthConfig

import dev.preview_auth as preview

from dev import atlas_calls, portal_modules

SESSION_COOKIE = "llm_lab_session"

#: Deliberately long and random: the product enforces a 16-character minimum,
#: and a fixed literal here would be a password in the repository.
def random_password() -> str:
    return "preview-" + secrets.token_urlsafe(24)


class ScriptedReader:
    """Feeds canned answers to the interactive prompt seam.

    Mirrors ``tests.auth.test_bootstrap.ScriptedReader``. It records prompts
    only — never the answers.
    """

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self.answers.pop(0)


@dataclass
class _Reply:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    def all_headers(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8")) if self.body else None

    def error_code(self) -> str | None:
        payload = self.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            return str(code) if isinstance(code, str) else None
        return None

    @property
    def set_cookies(self) -> list[str]:
        return self.all_headers("set-cookie")


@dataclass
class LoopbackClient:
    """A minimal HTTP client with a cookie jar, pointed at one preview server."""

    port: int
    cookies: dict[str, str] = field(default_factory=dict)
    timeout: float = 5.0
    host_header: str | None = None

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.host}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        cookies: bool = True,
        content_type: str | None = None,
    ) -> _Reply:
        sent: dict[str, str] = {"Host": self.host_header or self.host}
        if cookies and self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if content_type:
            sent["Content-Type"] = content_type
        sent.update(headers or {})

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            if body is None:
                connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                for key, value in sent.items():
                    if key == "Content-Length":
                        continue
                    connection.putheader(key, value)
                connection.endheaders()
            else:
                connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                sent.setdefault("Content-Length", str(len(body)))
                for key, value in sent.items():
                    connection.putheader(key, value)
                connection.endheaders(body)

            response = connection.getresponse()
            payload = response.read()
            reply = _Reply(
                status=response.status,
                reason=response.reason or "",
                headers=[(k, v) for k, v in response.getheaders()],
                body=payload,
            )
        finally:
            connection.close()

        for raw in reply.set_cookies:
            pair = raw.split(";", 1)[0].strip()
            name, _, value = pair.partition("=")
            if not name:
                continue
            if value == "":
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return reply

    # --- convenience --------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> _Reply:
        return self.request("GET", path, **kwargs)

    def csrf_token(self) -> str:
        """A fresh CSRF value, exactly as the browser would obtain it."""
        reply = self.get("/auth/csrf")
        assert reply.status == 200, reply.status
        payload = reply.json()
        token = payload["data"]["csrf_token"]
        assert isinstance(token, str) and token
        return token

    def write(self, method: str, path: str, payload: Any, *, csrf: str | None = None) -> _Reply:
        """A same-origin write with the ``X-CSRF-Token`` header the UI sends."""
        import json

        token = self.csrf_token() if csrf is None else csrf
        return self.request(
            method,
            path,
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            headers={"Origin": self.origin, "X-CSRF-Token": token},
        )

    def login(self, password: str, *, username: str = "admin") -> _Reply:
        return self.write("POST", "/auth/local/login", {"username": username, "password": password})

    def raw_exchange(self, raw: bytes, *, timeout: float = 3.0) -> bytes:
        """Send bytes verbatim and read whatever comes back.

        Used where ``http.client`` would "helpfully" add a ``Content-Length``
        and hide the very behaviour under test.
        """
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(raw)
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)


class PreviewServer:
    """A preview server bound to an ephemeral loopback port, served in a thread."""

    def __init__(
        self,
        *,
        local_http_dev: bool = True,
        mock_feishu: bool = False,
        atlas_grants: Iterable[Any] = (),
        self_calls_subject: str | None = None,
        self_calls_content_consent: Iterable[str] = (),
        portal_grants: Iterable[Any] = (),
    ) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="llm-lab-preview-it-"))
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.password = random_password()

        self.app = preview.build_preview_application(
            self.state_dir,
            local_http_dev=local_http_dev,
            mock_feishu=mock_feishu,
            atlas_grants=atlas_grants,
            self_calls_grant=(
                None
                if self_calls_subject is None
                else atlas_calls.SelfCallsGrant.for_subject(self_calls_subject)
            ),
            self_calls_content_consent=self_calls_content_consent,
            portal_grants=portal_grants,
        )
        self.cfg: AuthConfig = self.app.cfg
        # Same seam the CLI uses; no preset, no default.
        bootstrap.initialize_admin_password(
            self.cfg, read_password=ScriptedReader(self.password, self.password)
        )

        self._server = preview.make_server(self.app, port=0)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> LoopbackClient:
        return LoopbackClient(port=self.port)

    # --- fixtures -----------------------------------------------------------

    def create_member(self, *, open_id: str, display_name: str) -> str:
        """Create (or refresh) an active member shadow; returns the user id.

        Membership is an out-of-band administrative action — there is
        deliberately no HTTP route that grants a role or a project, so the
        harness uses the store directly, exactly as an operator would.
        """
        user = self.app.auth.service.db.upsert_feishu_user(
            app_id="cli_mock_app_id", open_id=open_id, union_id=None,
            tenant_key="tenant_mock", display_name=display_name,
        )
        return user.id

    def grant_project(self, user_id: str, project_id: str) -> None:
        self.app.auth.service.db.grant_project(user_id, project_id, role=store.ROLE_MEMBER)

    def session_cookie_for(self, user_id: str) -> str:
        """A live session id for ``user_id``, obtained through the real manager."""
        return self.app.auth.service.sessions.create(user_id).session_id

    # --- Atlas preview grants (server-side configuration, never a request field) ---

    def admin_user_id(self) -> str:
        """The account id the administrator's Session actually carries.

        Read back out of the store rather than recomputed here: the id is the
        product's own derivation, and a test that reimplemented it would be
        testing its own copy instead of the thing under test.
        """
        return self.app.auth.service.db.ensure_local_user(self.cfg.admin_username).id

    def grant_atlas(self, subject: str, *projects: str, scopes: Iterable[str] | None = None) -> None:
        """Add one server-side Atlas grant for ``subject``.

        This is the same seam the CLI uses (``--atlas-demo-grant``): an
        in-process configuration action an operator performs. There is no HTTP
        route anywhere that can reach it, which is the point — the browser
        cannot grant itself an Atlas scope, and neither can an admin role.
        """
        from dev import atlas_preview

        self.app.atlas_grants.set(
            atlas_preview.AtlasPreviewGrant(
                subject=subject,
                projects=tuple(projects),
                scopes=(
                    atlas_preview.DEFAULT_GRANT_SCOPES if scopes is None else tuple(scopes)
                ),
            )
        )

    def grant_self_calls(self, subject: str, *, content_consent: bool = False) -> None:
        """Set the server-side self-calls grant for ``subject``.

        The same seam the CLI uses (``--calls-self-grant``): an in-process
        configuration action. No HTTP route can reach it, so a browser cannot
        grant itself this view, and neither can an admin role.

        There is exactly **one** such grant at a time -- it carries the key
        directory that decides who each key belongs to, so a second one would
        have to share that directory and show the first account's keys to
        someone else. Granting a different subject therefore replaces it.

        ``content_consent`` records a *content-access opt-in* on the subject's
        record. It is deliberately a separate decision from the read grant, and
        it never makes the endpoint return prompt/completion text — see
        ``docs/portal-call-log-contract.md``.
        """
        self.app.self_calls_grant = atlas_calls.SelfCallsGrant.for_subject(subject)
        if content_consent:
            self.app.self_calls_consent.grant(subject)

    def grant_portal_modules(self, subject: str, scopes: Iterable[str] | None = None) -> None:
        """Set one server-side Portal-module grant for ``subject``.

        The same seam the CLI uses (``--portal-modules-grant``): an in-process
        configuration action. Default is the read scope only; the tests that need
        a native-console entry pass the extra scope explicitly, which is exactly
        the point — opening an entry is a separate decision from reading the
        catalogue.
        """
        self.app.portal_grants.set(
            portal_modules.PortalModuleGrant(
                subject=subject,
                scopes=(
                    portal_modules.DEFAULT_GRANT_SCOPES if scopes is None else tuple(scopes)
                ),
            )
        )

    def disable_user(self, user_id: str) -> None:
        self.app.auth.service.db.set_user_status(user_id, store.STATUS_DISABLED)

    def revoke_session(self, session_id: str) -> None:
        self.app.auth.service.sessions.revoke(session_id)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "PreviewServer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "LoopbackClient",
    "PreviewServer",
    "ScriptedReader",
    "SESSION_COOKIE",
    "random_password",
]
