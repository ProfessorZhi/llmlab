"""Feishu (Lark) web-login OAuth adapter.

Endpoints are the official ones and are **not** configurable, so a typo in an
environment variable can never point the token exchange at a look-alike host.
They were taken from the vendor documentation rather than guessed:

* web login / authorisation:
  https://accounts.feishu.cn/open-apis/authen/v1/authorize
* app access credential:
  https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal
* authorisation code -> user credential:
  https://open.feishu.cn/open-apis/authen/v1/access_token
  (documented as "获取 user_access_token" — create-2)
* user profile:
  https://open.feishu.cn/open-apis/authen/v1/user_info

Two things this adapter refuses to do:

* **Silently downgrade.** PKCE is *not* enabled, because the documented
  ``/authen/v1/access_token`` endpoint takes no ``code_verifier``. Asking for
  PKCE therefore raises instead of quietly dropping the parameter. Likewise an
  empty tenant allowlist is a hard failure, not "allow everyone".
* **Keep the user credential.** The ``user_access_token`` is used for exactly
  one profile call and then dropped. It is never stored, never logged, never
  placed in a session row or a cookie.

``union_id`` deserves a note: it is shared across every application of one ISV,
so it must not be used as a global person key. We record it for auditing only;
identity is always ``(our app_id, open_id)``, which is the app-scoped pair.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from . import errors

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
APP_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
USER_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 256 * 1024

# Upstream codes that mean "this person may not sign in" rather than "we are
# broken". See the error tables on the two upstream endpoints.
_ACCESS_DENIED_CODES = frozenset({20003, 20005, 20008, 20021, 20022, 20023, 20028})


@dataclass(frozen=True)
class FeishuIdentity:
    """The only thing we keep from a successful login."""

    open_id: str
    union_id: str | None
    tenant_key: str
    display_name: str
    avatar_url: str | None = None
    sid: str | None = None


class Transport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]: ...


class FeishuProvider(Protocol):
    supports_pkce: bool

    def authorization_url(self, *, state: str, code_challenge: str | None = None) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str | None = None) -> FeishuIdentity: ...


class UrllibTransport:
    """The real transport. Verified TLS, bounded body, explicit timeout."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self._context = context if context is not None else ssl.create_default_context()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        if not url.startswith("https://"):
            raise errors.ConfigError(detail=f"refusing a non-https upstream call to {url!r}")

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)

        with urllib.request.urlopen(
            request, timeout=timeout if timeout is not None else DEFAULT_TIMEOUT, context=self._context
        ) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw = response.read(MAX_RESPONSE_BYTES)

        text = raw.decode("utf-8", "replace")
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, text


class FeishuOAuthProvider:
    supports_pkce = False  # the documented endpoints take no code_verifier

    def __init__(
        self,
        *,
        app_id: str | None,
        app_secret: str | None,
        redirect_uri: str | None,
        tenant_allowlist: frozenset[str] | set[str],
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        pkce_enabled: bool = False,
    ) -> None:
        if not app_id or not app_secret or not redirect_uri:
            raise errors.ConfigError(
                detail="Feishu app_id, app_secret and redirect_uri must all be configured"
            )
        if not str(redirect_uri).startswith("https://"):
            raise errors.ConfigError(detail="Feishu redirect_uri must be https")
        if pkce_enabled and not self.supports_pkce:
            raise errors.ConfigError(
                detail=(
                    "PKCE was requested but the documented Feishu endpoints do not accept "
                    "code_verifier; enable it only after confirming support, never by dropping it"
                )
            )

        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri
        self._tenants = frozenset(tenant_allowlist or ())
        self.transport = transport if transport is not None else UrllibTransport()
        self._timeout = timeout

    # --- authorisation -------------------------------------------------------

    def authorization_url(self, *, state: str, code_challenge: str | None = None) -> str:
        if code_challenge and not self.supports_pkce:
            raise errors.ConfigError(
                detail="this provider does not support PKCE; refusing to drop the challenge"
            )
        params: list[tuple[str, str]] = [
            ("client_id", self._app_id),
            ("redirect_uri", self._redirect_uri),
            ("response_type", "code"),
            ("state", state),
        ]
        if code_challenge:
            params.append(("code_challenge", code_challenge))
            params.append(("code_challenge_method", "S256"))
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    # --- token exchange ------------------------------------------------------

    def exchange_code(self, *, code: str, code_verifier: str | None = None) -> FeishuIdentity:
        if not isinstance(code, str) or not code:
            raise errors.InvalidRequest(detail="authorisation code is missing")
        if not self._tenants:
            # Configuration validation already refuses this; kept as defence in
            # depth so an empty allowlist can never mean "every tenant is fine".
            raise errors.NotConfigured(detail="the Feishu tenant allowlist is empty")

        app_token = self._fetch_app_access_token()
        token_data = self._fetch_user_access_token(app_token, code)

        token_open_id = _text(token_data.get("open_id"))
        token_tenant = _text(token_data.get("tenant_key"))
        if not token_open_id:
            raise errors.ProviderUnavailable(detail="upstream response carried no open_id")
        if not token_tenant:
            raise errors.ProviderUnavailable(detail="upstream response carried no tenant_key")
        self._enforce_tenant(token_tenant)

        profile = self._fetch_user_info(_text(token_data.get("access_token")))
        if profile:
            profile_open_id = _text(profile.get("open_id"))
            profile_tenant = _text(profile.get("tenant_key"))
            if profile_open_id and profile_open_id != token_open_id:
                raise errors.ProviderUnavailable(
                    detail="the profile does not match the credential that was issued"
                )
            if profile_tenant and profile_tenant != token_tenant:
                raise errors.ProviderUnavailable(
                    detail="the tenant changed between the credential and the profile"
                )

        return FeishuIdentity(
            open_id=token_open_id,
            union_id=_text(profile.get("union_id")) or _text(token_data.get("union_id")),
            tenant_key=token_tenant,
            display_name=_text(profile.get("name")) or _text(token_data.get("name")) or token_open_id,
            avatar_url=_text(profile.get("avatar_url")) or _text(token_data.get("avatar_url")),
            sid=_text(token_data.get("sid")),
        )

    # --- upstream steps ------------------------------------------------------

    def _fetch_app_access_token(self) -> str:
        status, body = self._call(
            "POST",
            APP_ACCESS_TOKEN_URL,
            payload={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        data = self._payload(status, body)
        if data.get("code") != 0:
            raise errors.ProviderUnavailable(
                detail=f"app credential refused (code={data.get('code')})"
            )
        token = _text(data.get("app_access_token"))
        if not token:
            raise errors.ProviderUnavailable(detail="app credential response carried no token")
        return token

    def _fetch_user_access_token(self, app_access_token: str, code: str) -> dict[str, Any]:
        status, body = self._call(
            "POST",
            USER_ACCESS_TOKEN_URL,
            payload={"grant_type": "authorization_code", "code": code},
            headers={"Authorization": f"Bearer {app_access_token}"},
        )
        data = self._payload(status, body)
        self._raise_for_upstream_code(data)
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise errors.ProviderUnavailable(detail="token response carried no data object")
        return payload

    def _fetch_user_info(self, user_access_token: str | None) -> dict[str, Any]:
        if not user_access_token:
            raise errors.ProviderUnavailable(detail="no user credential to read the profile with")
        status, body = self._call(
            "GET", USER_INFO_URL, headers={"Authorization": f"Bearer {user_access_token}"}
        )
        data = self._payload(status, body)
        self._raise_for_upstream_code(data)
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise errors.ProviderUnavailable(detail="profile response carried no data object")
        return payload

    # --- helpers -------------------------------------------------------------

    def _call(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        try:
            return self.transport.request_json(
                method, url, payload=payload, headers=headers, timeout=self._timeout
            )
        except errors.AuthError:
            raise
        except (
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            ValueError,
        ) as exc:
            raise errors.ProviderUnavailable(
                detail=f"upstream transport failed: {type(exc).__name__}"
            ) from None

    def _payload(self, status: int, body: Any) -> dict[str, Any]:
        if isinstance(body, (bytes, bytearray)):
            body = bytes(body).decode("utf-8", "replace")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                raise errors.ProviderUnavailable(
                    detail=f"upstream returned a non-JSON body (http {status})"
                ) from None
        if not isinstance(body, dict):
            raise errors.ProviderUnavailable(detail=f"unexpected upstream body (http {status})")
        if status >= 500:
            raise errors.ProviderUnavailable(detail=f"upstream error status {status}")
        if status >= 400:
            raise errors.ProviderUnavailable(detail=f"upstream refused the request (http {status})")
        return body

    def _raise_for_upstream_code(self, data: dict[str, Any]) -> None:
        code = data.get("code")
        if code == 0:
            return
        if code in _ACCESS_DENIED_CODES:
            # The user, not the deployment: expired/replayed code, resigned,
            # frozen, unregistered.
            raise errors.AccessDenied(detail=f"provider refused the sign-in (code={code})")
        raise errors.ProviderUnavailable(detail=f"provider error (code={code})")

    def _enforce_tenant(self, tenant_key: str) -> None:
        if tenant_key not in self._tenants:
            raise errors.AccessDenied(detail="the identity is outside the allowed tenants")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def provider_from_config(cfg, *, transport: Transport | None = None) -> FeishuOAuthProvider | None:
    """Build a provider, or ``None`` when Feishu is not configured at all.

    ``None`` is a legitimate state (local administrator only); it is what makes
    ``/auth/feishu/start`` answer ``not_configured`` without any network call.
    """
    if not cfg.feishu_configured:
        return None
    return FeishuOAuthProvider(
        app_id=cfg.feishu_app_id,
        app_secret=cfg.feishu_app_secret,
        redirect_uri=cfg.feishu_redirect_uri,
        tenant_allowlist=cfg.feishu_tenant_allowlist,
        transport=transport,
        pkce_enabled=cfg.feishu_pkce_enabled,
    )


__all__ = [
    "APP_ACCESS_TOKEN_URL",
    "AUTHORIZE_URL",
    "DEFAULT_TIMEOUT",
    "FeishuIdentity",
    "FeishuOAuthProvider",
    "Transport",
    "UrllibTransport",
    "USER_ACCESS_TOKEN_URL",
    "USER_INFO_URL",
    "provider_from_config",
]
