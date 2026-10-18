"""Shared offline test doubles.

Nothing in this module opens a socket. Every provider interaction in the suite
goes through :class:`FakeTransport`, so the real :class:`FeishuOAuthProvider`
code path — URL construction, request shapes, response parsing, tenant checks —
is exercised without any network access, and no real Feishu application
credentials exist anywhere in the repository.
"""

from __future__ import annotations

import io
import json
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Any

from backend.auth import config as auth_config
from backend.auth.errors import AuthError

SYNTHETIC_APP_ID = "cli_synthetic_app_id"
SYNTHETIC_APP_SECRET = "synthetic-app-secret-value"
SYNTHETIC_REDIRECT = "https://portal.example.com/auth/feishu/callback"
SYNTHETIC_TENANT = "tenant_synthetic_a"
SYNTHETIC_OPEN_ID = "ou_synthetic_open_id"
SYNTHETIC_UNION_ID = "on_synthetic_union_id"


class FakeTransport:
    """Scripted replacement for the real HTTPS transport.

    Each queued item is either ``(status, body)`` — where ``body`` is a dict or
    a raw string — or an exception instance to raise (timeouts, refused
    connections, ...). Every call is recorded for assertions.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict[str, Any]] = []

    def queue(self, *responses: Any) -> "FakeTransport":
        self.responses.extend(responses)
        return self

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}: no scripted response left")
        spec = self.responses.pop(0)
        if isinstance(spec, BaseException):
            raise spec
        return spec

    # --- convenience for assertions -----------------------------------------

    @property
    def urls(self) -> list[str]:
        return [request["url"] for request in self.requests]


def app_token_response(token: str = "t-synthetic-app-token", expire: int = 7200) -> tuple[int, dict]:
    return (200, {"code": 0, "msg": "ok", "app_access_token": token, "expire": expire})


def user_token_response(
    *,
    access_token: str = "u-synthetic-user-token",
    open_id: str = SYNTHETIC_OPEN_ID,
    union_id: str | None = SYNTHETIC_UNION_ID,
    tenant_key: str = SYNTHETIC_TENANT,
    name: str = "Synthetic Person",
) -> tuple[int, dict]:
    data: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 7200,
        "name": name,
        "open_id": open_id,
        "tenant_key": tenant_key,
    }
    if union_id is not None:
        data["union_id"] = union_id
    return (200, {"code": 0, "msg": "ok", "data": data})


def user_info_response(
    *,
    open_id: str = SYNTHETIC_OPEN_ID,
    union_id: str | None = SYNTHETIC_UNION_ID,
    tenant_key: str = SYNTHETIC_TENANT,
    name: str = "Synthetic Person",
) -> tuple[int, dict]:
    data: dict[str, Any] = {"name": name, "open_id": open_id, "tenant_key": tenant_key}
    if union_id is not None:
        data["union_id"] = union_id
    return (200, {"code": 0, "msg": "ok", "data": data})


def error_response(code: int, msg: str = "synthetic upstream error", status: int = 200):
    return (status, {"code": code, "msg": msg})


def raw_response(status: int, body: str):
    return (status, body)


def json_body(response: tuple[int, Any]) -> str:
    return json.dumps(response[1])


def success_script(**overrides: Any) -> list[Any]:
    """The three upstream calls a successful login makes, in order."""
    return [
        app_token_response(),
        user_token_response(**overrides),
        user_info_response(**overrides),
    ]


class FakeFeishuProvider:
    """Protocol-level double, used where a test needs the provider *itself* to fail.

    Route-level tests use the real provider with :class:`FakeTransport`; this
    double exists for the handful of cases where the interesting behaviour is
    "the provider raised", not "the provider's HTTP handling is correct".
    """

    supports_pkce = False

    def __init__(self, *, result: Any = None, raises: BaseException | None = None) -> None:
        self.result = result
        self.raises = raises
        self.authorize_calls: list[dict[str, Any]] = []
        self.exchange_calls: list[dict[str, Any]] = []

    def authorization_url(self, *, state: str, code_challenge: str | None = None) -> str:
        self.authorize_calls.append({"state": state, "code_challenge": code_challenge})
        return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?state={state}"

    def exchange_code(self, *, code: str, code_verifier: str | None = None) -> Any:
        self.exchange_calls.append({"code": code, "code_verifier": code_verifier})
        if self.raises is not None:
            raise self.raises
        return self.result


def make_config(root: pathlib.Path, **env: str) -> auth_config.AuthConfig:
    """A validated AuthConfig rooted at ``root``."""
    base = {"LLM_LAB_AUTH_STATE_DIR": str(root)}
    base.update(env)
    return auth_config.AuthConfig.from_env(base)


def feishu_env(**overrides: str) -> dict[str, str]:
    base = {
        "LLM_LAB_FEISHU_APP_ID": SYNTHETIC_APP_ID,
        "LLM_LAB_FEISHU_APP_SECRET": SYNTHETIC_APP_SECRET,
        "LLM_LAB_FEISHU_REDIRECT_URI": SYNTHETIC_REDIRECT,
        "LLM_LAB_FEISHU_TENANT_ALLOWLIST": SYNTHETIC_TENANT,
    }
    base.update(overrides)
    return base


def temp_dir(prefix: str) -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


# --- HTTP --------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    set_cookies: list[str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    @property
    def location(self) -> str | None:
        return self.headers.get("location")

    def error_code(self) -> str | None:
        payload = self.json()
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return payload["error"].get("code")
        return None

    def set_cookie(self, name: str) -> str | None:
        for raw in self.set_cookies:
            if raw.split("=", 1)[0].strip() == name:
                return raw
        return None


@dataclass
class WsgiClient:
    """A tiny in-process WSGI client with a cookie jar.

    Exercising the real WSGI callable — not the handler functions directly — is
    what makes these tests a check of the *complete* HTTP chain.
    """

    app: Any
    host: str = "portal.example.com"
    scheme: str = "https"
    remote_addr: str = "203.0.113.10"
    cookies: dict[str, str] = field(default_factory=dict)
    send_origin: bool = True

    def run(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
        send_origin: bool | None = None,
        content_type: str | None = "application/json",
        clear_cookies: bool = False,
    ) -> HttpResponse:
        if clear_cookies:
            self.cookies.clear()

        if "/" in path:
            path_only, _, query = path.partition("?")
        else:  # pragma: no cover - defensive
            path_only, query = path, ""

        if raw_body is not None:
            body = raw_body
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
        else:
            body = b""

        host_name, _, port = self.host.partition(":")
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "SCRIPT_NAME": "",
            "PATH_INFO": path_only,
            "QUERY_STRING": query,
            "SERVER_NAME": host_name,
            "SERVER_PORT": port or ("443" if self.scheme == "https" else "80"),
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": self.remote_addr,
            "HTTP_HOST": self.host,
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": self.scheme,
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(body)),
        }
        if content_type:
            environ["CONTENT_TYPE"] = content_type
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

        wants_origin = self.send_origin if send_origin is None else send_origin
        if wants_origin:
            environ["HTTP_ORIGIN"] = f"{self.scheme}://{self.host}"

        for key, value in (headers or {}).items():
            name = key.upper().replace("-", "_")
            if name == "CONTENT_LENGTH":
                environ["CONTENT_LENGTH"] = value
                continue
            if name == "CONTENT_TYPE":
                environ["CONTENT_TYPE"] = value
                continue
            environ["HTTP_" + name] = value

        captured: dict[str, Any] = {}

        def start_response(status: str, response_headers: list, exc_info=None):
            captured["status"] = status
            captured["headers"] = response_headers
            return lambda chunk: None

        chunks = self.app(environ, start_response)
        try:
            response_body = b"".join(chunks or [])
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):  # pragma: no cover - defensive
                close()

        status_code = int(str(captured["status"]).split(" ", 1)[0])
        flat: dict[str, str] = {}
        set_cookies: list[str] = []
        for name, value in captured["headers"]:
            if name.lower() == "set-cookie":
                set_cookies.append(value)
            else:
                flat[name.lower()] = value

        for raw in set_cookies:
            head, _, attrs = raw.partition(";")
            cookie_name, _, cookie_value = head.partition("=")
            cookie_name = cookie_name.strip()
            if "max-age=0" in attrs.lower():
                self.cookies.pop(cookie_name, None)
            else:
                self.cookies[cookie_name] = cookie_value.strip()

        return HttpResponse(
            status=status_code, headers=flat, set_cookies=set_cookies, body=response_body
        )


def temp_config(**env: str):
    """Config in a throwaway state directory. Returns (config, tmpdir)."""
    root = temp_dir("llm-lab-auth-cfg-")
    return make_config(root, **env), root


__all__ = [
    "AuthError",
    "FakeFeishuProvider",
    "FakeTransport",
    "HttpResponse",
    "SYNTHETIC_APP_ID",
    "SYNTHETIC_APP_SECRET",
    "SYNTHETIC_OPEN_ID",
    "SYNTHETIC_REDIRECT",
    "SYNTHETIC_TENANT",
    "SYNTHETIC_UNION_ID",
    "WsgiClient",
    "app_token_response",
    "error_response",
    "feishu_env",
    "json_body",
    "make_config",
    "raw_response",
    "success_script",
    "temp_config",
    "temp_dir",
    "user_info_response",
    "user_token_response",
]
