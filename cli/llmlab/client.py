"""HTTP 客户端与凭据解析。**Fail closed**：没有凭据就不发请求。

两条不可退让的规则：

1. **凭据只能来自 Portal 签发的 token。** CLI 不读 Linux 密码、不调 ``sudo``、
   不开远端 shell、不碰 ``/etc/shadow``、不回落到「先试试免密」。没有 token
   就是退出码 2，一个网络请求都不发。
2. **明文不带凭据出本机。** ``http://`` 只允许连 loopback；要连别的机器必须
   ``https://``。否则一次 ``--api-base http://192.0.2.9`` 就会把 token
   以明文送进内网。

token 从不进日志、不进异常消息、不进 ``--json`` 输出。``redact`` 是唯一的
展示函数，任何要打印的东西都先过它。
"""

from __future__ import annotations

import http.client
import json
import os
import stat
from urllib.parse import urlsplit

from .errors import (
    CliError,
    EXIT_TRANSPORT,
    EXIT_USAGE,
    exit_code_for_status,
)

DEFAULT_API_BASE = "http://127.0.0.1:8787"

API_PREFIX = "/api/v2/registry"

#: 环境变量名。``LLMLAB_TOKEN`` 直接给值，``LLMLAB_TOKEN_FILE`` 给路径。
ENV_TOKEN = "LLMLAB_TOKEN"
ENV_TOKEN_FILE = "LLMLAB_TOKEN_FILE"
ENV_API_BASE = "LLMLAB_API_BASE"

#: 允许明文传输的主机（只有 loopback）。
PLAINTEXT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 请求超时（秒）。
DEFAULT_TIMEOUT = 15.0

#: token 文件的最大尺寸：超过这个就不是一个凭据文件。
MAX_TOKEN_FILE_BYTES = 4096


def redact(value: str | None) -> str:
    """把任何可能含凭据的字符串变成可安全打印的形状。"""
    if not value:
        return "<none>"
    text = str(value)
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-2:]}(len={len(text)})"


def resolve_api_base(raw: str | None) -> str:
    """校验并归一化 API base。**明文只许回环。**"""
    candidate = (raw or os.environ.get(ENV_API_BASE) or DEFAULT_API_BASE).strip()
    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https"):
        raise CliError(
            EXIT_USAGE,
            f"unsupported API base scheme: {scheme or '(none)'!r}",
            hint="use http://127.0.0.1:<port> or an https:// endpoint",
        )
    if parts.username or parts.password:
        raise CliError(EXIT_USAGE, "credentials in the API base URL are not accepted")
    if not host:
        raise CliError(EXIT_USAGE, "API base must include a host")
    if scheme == "http" and host not in PLAINTEXT_HOSTS:
        raise CliError(
            EXIT_USAGE,
            f"refusing to send a credential in cleartext to {host!r}",
            hint="plain http is allowed for loopback only; use https for remote hosts",
        )
    base = f"{scheme}://{parts.hostname}"
    if parts.port:
        base += f":{parts.port}"
    return base


def _read_token_file(path: str) -> str:
    """读 token 文件。**只接受**属主本人的 0600 普通文件，且不是软链。

    这三条检查各自对应一种真实的失手方式：软链指到别处、权限过宽被别人读走、
    或把一个目录/设备当文件读。
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise CliError(EXIT_USAGE, f"credential file not found: {path}") from exc
    except OSError as exc:
        raise CliError(EXIT_USAGE, f"cannot stat credential file: {exc.strerror}") from exc

    if stat.S_ISLNK(info.st_mode):
        raise CliError(EXIT_USAGE, "credential file must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise CliError(EXIT_USAGE, "credential file must be a regular file")
    if info.st_uid != os.getuid():
        raise CliError(EXIT_USAGE, "credential file must be owned by the current user")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CliError(
            EXIT_USAGE,
            "credential file must not be readable or writable by group/other",
            hint=f"chmod 600 {path}",
        )
    if info.st_size > MAX_TOKEN_FILE_BYTES:
        raise CliError(EXIT_USAGE, "credential file is implausibly large")

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(MAX_TOKEN_FILE_BYTES)
    except OSError as exc:
        raise CliError(EXIT_USAGE, f"cannot read credential file: {exc.strerror}") from exc
    token = raw.strip()
    if not token:
        raise CliError(EXIT_USAGE, "credential file is empty")
    return token


def resolve_token(explicit: str | None = None, explicit_file: str | None = None) -> str:
    """解析凭据。**任何缺口都是退出码 2，且不发请求。**"""
    if explicit:
        token = explicit.strip()
        if not token:
            raise CliError(EXIT_USAGE, "--token was given but is empty")
        return token

    file_path = explicit_file or os.environ.get(ENV_TOKEN_FILE)
    if file_path:
        return _read_token_file(file_path)

    env_token = os.environ.get(ENV_TOKEN)
    if env_token and env_token.strip():
        return env_token.strip()

    raise CliError(
        EXIT_USAGE,
        "no credential available; refusing to send any request",
        hint=(
            f"set {ENV_TOKEN}, or point {ENV_TOKEN_FILE} at a 0600 file holding the "
            "token your Portal issued. The CLI never falls back to Linux credentials, "
            "sudo, or a remote shell."
        ),
    )


class ApiClient:
    """极小的 JSON-over-HTTP 客户端。只用标准库。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url
        self._token = token
        self.timeout = timeout
        parts = urlsplit(base_url)
        self._host = parts.hostname or "127.0.0.1"
        self._port = parts.port or (443 if parts.scheme == "https" else 80)
        self._secure = parts.scheme == "https"

    # -- 请求 -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """发一次请求。返回解析后的 JSON；失败抛 :class:`CliError`。"""
        if not path.startswith("/"):
            path = "/" + path
        payload = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            # 不允许任何中间层缓存或改写租户数据。
            "Cache-Control": "no-store",
        }
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        connection_class = (
            http.client.HTTPSConnection if self._secure else http.client.HTTPConnection
        )
        connection = connection_class(self._host, self._port, timeout=self.timeout)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException) as exc:
            # 消息里**不**包含请求头（那里面有 token）。
            raise CliError(
                EXIT_TRANSPORT,
                f"cannot reach {self.base_url}: {type(exc).__name__}",
                hint="is the registry process running, and is the base URL right?",
            ) from None
        finally:
            connection.close()

        if not raw:
            raise CliError(EXIT_TRANSPORT, f"empty response from {self.base_url} (HTTP {status})")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CliError(
                EXIT_TRANSPORT,
                f"response was not JSON (HTTP {status}); is the base URL pointing at the registry?",
            ) from None

        if status >= 400:
            error = parsed.get("error") if isinstance(parsed, dict) else None
            code = (error or {}).get("code", "http_error")
            message = (error or {}).get("message", f"HTTP {status}")
            raise CliError(exit_code_for_status(status), f"{code}: {message}")
        return parsed

    # -- 便捷方法 ---------------------------------------------------------

    def get(self, path: str) -> dict:
        return self.request("GET", API_PREFIX + path)

    def post(self, path: str, body: dict | None = None, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", API_PREFIX + path, body=body, idempotency_key=idempotency_key)

    def describe(self) -> str:
        return f"{self.base_url}{API_PREFIX} (credential {redact(self._token)})"
