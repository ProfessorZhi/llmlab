"""REST 传输层：**薄**适配器。所有策略都在 service / authz 里。

这一层只做五件事，顺序即安全边界顺序：

1. **构造请求**：只从 WSGI environ 取头与体；拒绝 chunked / ``Transfer-Encoding``
   （长度不可信 => 不读）；超过上限的体 -> 413。
2. **解析凭据 -> 受信身份**：``Authorization: Bearer`` 经受信目录解析；失败 401。
   ``wsgi.url_scheme`` 是唯一的 scheme 来源 —— **不**看 ``X-Forwarded-Proto``。
3. **拒绝自报身份**：``X-Owner`` / ``X-Uid`` / ``X-Role`` 这类头出现即 400。
4. **分发**到 service 的用例方法。
5. **把契约错误映射成状态码**（``CONTRACT_STATUS``）：契约层不认识 HTTP，
   映射表只在这里存在。

响应体有两种形态，刻意与仓库既有传输层一致：

* 成功 —— ``{"ok": true, ...契约信封}``
* 失败 —— ``{"ok": false, "error": {"code": ..., "message": ...}}``

错误信息**永不**回显凭据；未预期的异常只回类型与 request id，不回消息。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Callable, Iterable

from .errors import RegistryError
from .identity import (
    PrincipalDirectory,
    TokenDirectory,
    extract_bearer_token,
    reject_identity_assertions,
    resolve_requester,
)
from .registry import RegistryService

#: 契约错误 -> HTTP 状态。**只**在这里存在（同 backend.auth 的传输层做法）。
#:
#: 传输层自己的错误（路由、请求体、凭据解析）也走同一个 ``registry.*`` 命名空间：
#: 调用方不该需要知道一个 401 是「router 发现没有头」还是「service 发现 token 无效」
#: 才能分支 —— 那是实现细节，不是契约。
CONTRACT_STATUS: dict[str, int] = {
    "registry.error": 500,
    "registry.invalid_request": 400,
    "registry.identity_assertion_rejected": 400,
    "registry.unauthenticated": 401,
    # 「本部署不知道该问谁」是**部署**事实，换凭据不会变好 —— 503，不是 401。
    "registry.token_authority_unavailable": 503,
    "registry.access_denied": 403,
    "registry.not_found": 404,
    "registry.method_not_allowed": 405,
    "registry.conflict": 409,
    "registry.not_verified": 409,
    # 「换个动作再来」与「重读再重试」是两种不同的客户端反应，码也不同。
    "registry.revision_conflict": 409,
    "registry.ownership_unverified": 409,
    "registry.protocol_unverified": 409,
    "registry.length_required": 411,
    "registry.payload_too_large": 413,
    "registry.unsupported_media_type": 415,
    "registry.internal_error": 500,
    "registry.upstream_unavailable": 503,
    "registry.storage_error": 500,
}

#: 路由前缀。所有端点都在它下面。
PREFIX = "/api/v2/registry"

#: 请求体上限。
MAX_BODY_BYTES = 256 * 1024

#: 只允许 JSON。
JSON_CONTENT_TYPES = ("application/json", "application/json; charset=utf-8")


class TransportError(Exception):
    """传输层错误（与契约错误分开：它们发生在进入 service 之前）。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _json_response(payload: dict, status: int = 200, request_id: str | None = None) -> tuple:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=False).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        # 元数据是租户相关的：任何共享缓存都不该留存它。
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Length", str(len(body))),
    ]
    if request_id:
        headers.append(("X-Request-Id", request_id))
    return status, headers, body


def _error_response(
    status: int, code: str, message: str, request_id: str | None = None
) -> tuple:
    return _json_response(
        {"ok": False, "error": {"code": code, "message": message}}, status, request_id
    )


def _header(environ: dict, name: str) -> str | None:
    key = "HTTP_" + name.upper().replace("-", "_")
    if name.lower() == "content-type":
        key = "CONTENT_TYPE"
    elif name.lower() == "content-length":
        key = "CONTENT_LENGTH"
    value = environ.get(key)
    return value if isinstance(value, str) else None


def _header_names(environ: dict) -> list[str]:
    return [
        key[5:].replace("_", "-").lower()
        for key in environ
        if key.startswith("HTTP_") and isinstance(environ.get(key), str)
    ]


def read_body(environ: dict) -> bytes:
    """读请求体。**长度必须被可信地声明**，否则不读。

    拒绝 ``Transfer-Encoding``（含 chunked）：分块编码的长度在读取过程中才确定，
    本层没有理由接受一个「边读边变长」的请求体。
    """
    if _header(environ, "transfer-encoding"):
        raise TransportError(411, "registry.length_required", "chunked requests are not accepted")

    raw_length = _header(environ, "content-length")
    # 空字符串就是「没有声明长度」：``wsgiref.simple_server`` 在无该头时正是这样填的。
    # 把它当非法会把**每一个无体的 GET** 变成 400。
    if raw_length is None or raw_length.strip() == "":
        return b""
    if not re.fullmatch(r"[0-9]{1,10}", raw_length.strip()):
        raise TransportError(400, "registry.invalid_request", "malformed Content-Length")
    length = int(raw_length)
    if length > MAX_BODY_BYTES:
        raise TransportError(413, "registry.payload_too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
    if length == 0:
        return b""
    stream = environ.get("wsgi.input")
    if stream is None:
        raise TransportError(400, "registry.invalid_request", "no request body stream")
    return stream.read(length)


def parse_json_body(raw: bytes, content_type: str | None) -> object:
    if not raw:
        return None
    if content_type is not None and not content_type.lower().startswith("application/json"):
        raise TransportError(415, "registry.unsupported_media_type", "body must be application/json")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TransportError(400, "registry.invalid_request", "body is not valid JSON") from None


# --- 路由表 -----------------------------------------------------------------

_ID = r"(?P<object_id>[A-Za-z0-9_\-]{1,64})"
#: Endpoint 上的**写**动作。``acl`` / ``routings`` 不在这里：它们是读，走 GET。
_ACTION = r"(?P<action>verify|share|unshare|invoke|repoint|rollback|routing-applied)"

#: (方法, 编译后的正则, service 方法名)。按顺序匹配。
ROUTES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("GET", re.compile(rf"^{PREFIX}/healthz$"), "_route_healthz"),
    ("GET", re.compile(rf"^{PREFIX}/whoami$"), "_route_whoami"),
    ("GET", re.compile(rf"^{PREFIX}/status$"), "_route_status"),
    ("GET", re.compile(rf"^{PREFIX}/usage/mine$"), "_route_usage_mine"),
    ("GET", re.compile(rf"^{PREFIX}/audit$"), "_route_audit"),
    ("GET", re.compile(rf"^{PREFIX}/routing-config$"), "_route_routing_config"),
    ("GET", re.compile(rf"^{PREFIX}/routing-snapshot$"), "_route_routing_snapshot"),
    ("GET", re.compile(rf"^{PREFIX}/engines$"), "_route_engines_list"),
    ("POST", re.compile(rf"^{PREFIX}/engines$"), "_route_engines_create"),
    # 只读预检（登记表单用）：必须排在 ``engines/{id}`` 之前，否则 ``preflight``
    # 会被当成一个 engine id。它是 POST 只因为要带一个 JSON 体；它不写任何东西。
    ("POST", re.compile(rf"^{PREFIX}/engines/preflight$"), "_route_engines_preflight"),
    ("GET", re.compile(rf"^{PREFIX}/engines/{_ID}$"), "_route_engines_get"),
    ("POST", re.compile(rf"^{PREFIX}/engines/{_ID}/{_ACTION}$"), "_route_engines_action"),
    ("GET", re.compile(rf"^{PREFIX}/models$"), "_route_models_list"),
    ("POST", re.compile(rf"^{PREFIX}/models$"), "_route_models_create"),
    ("GET", re.compile(rf"^{PREFIX}/models/{_ID}$"), "_route_models_get"),
    ("GET", re.compile(rf"^{PREFIX}/deployments$"), "_route_deployments_list"),
    ("POST", re.compile(rf"^{PREFIX}/deployments$"), "_route_deployments_create"),
    ("GET", re.compile(rf"^{PREFIX}/deployments/{_ID}$"), "_route_deployments_get"),
    ("POST", re.compile(rf"^{PREFIX}/deployments/{_ID}/{_ACTION}$"), "_route_deployments_action"),
    ("GET", re.compile(rf"^{PREFIX}/endpoints$"), "_route_endpoints_list"),
    ("POST", re.compile(rf"^{PREFIX}/endpoints$"), "_route_endpoints_create"),
    ("GET", re.compile(rf"^{PREFIX}/endpoints/{_ID}$"), "_route_endpoints_get"),
    # 读授权名单是 GET：把它塞进写动作那条 POST 路由会让 CLI 的 GET 变成 405。
    ("GET", re.compile(rf"^{PREFIX}/endpoints/{_ID}/acl$"), "_route_endpoints_acl"),
    ("GET", re.compile(rf"^{PREFIX}/endpoints/{_ID}/routings$"), "_route_endpoints_routings"),
    ("POST", re.compile(rf"^{PREFIX}/endpoints/{_ID}/{_ACTION}$"), "_route_endpoints_action"),
)


class RegistryApplication:
    """WSGI 可调用对象。**不监听端口、不起进程** —— 那是 ``devserver`` 的事。"""

    def __init__(
        self,
        service: RegistryService,
        *,
        tokens: TokenDirectory | None = None,
        principals: PrincipalDirectory | None = None,
        debug_errors: bool = False,
    ) -> None:
        self.service = service
        self.tokens = tokens if tokens is not None else service.tokens
        self.principals = principals if principals is not None else service.principals
        self.debug_errors = bool(debug_errors)

    # -- WSGI 入口 --------------------------------------------------------

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        request_id = uuid.uuid4().hex[:16]
        try:
            status, headers, body = self._dispatch(environ, request_id)
        except TransportError as exc:
            status, headers, body = _error_response(exc.status, exc.code, exc.message, request_id)
        except RegistryError as exc:
            code = exc.contract_code
            if getattr(exc, "expose_message", True):
                message = str(exc)
            else:
                # 存储层的消息里有库路径与 schema 细节：进日志，不进响应。
                message = f"storage failure; request id {request_id}"
            status, headers, body = _error_response(
                CONTRACT_STATUS.get(code, 500), code, message, request_id
            )
        except Exception as exc:  # noqa: BLE001 - 传输层兜底，绝不外泄内部细节
            message = (
                f"{type(exc).__name__}: {exc}"
                if self.debug_errors
                else f"internal error ({type(exc).__name__}); request id {request_id}"
            )
            status, headers, body = _error_response(500, "registry.internal_error", message, request_id)
        start_response(f"{status} {_reason(status)}", headers)
        return [body]

    def _dispatch(self, environ: dict, request_id: str) -> tuple:
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        # 1) 只认 wsgi.url_scheme；转发头不参与任何判断。
        scheme = environ.get("wsgi.url_scheme")
        if scheme not in ("http", "https"):
            raise TransportError(400, "registry.invalid_request", "unrecognised request scheme")

        # 2) 认路径（含 405 判定），再认身份。
        matched = None
        path_exists = False
        for route_method, pattern, handler in ROUTES:
            found = pattern.match(path)
            if not found:
                continue
            path_exists = True
            if route_method == method:
                matched = (found, handler)
                break
        if matched is None:
            if path_exists:
                raise TransportError(405, "registry.method_not_allowed", f"{method} is not allowed here")
            raise TransportError(404, "registry.not_found", "no such route")

        found, handler = matched

        # 3) 未认证的存活探针：刻意不碰 token、也不碰库。
        if handler == "_route_healthz":
            return _json_response(self._healthz_payload(), 200, request_id)

        raw_body = read_body(environ)
        body = parse_json_body(raw_body, _header(environ, "content-type"))

        # 4) 构造受信身份。失败一律 401（缺头/畸形头不可区分）。
        authorization = _header(environ, "authorization")
        token = extract_bearer_token(authorization)
        if token is None:
            raise TransportError(
                401,
                "registry.unauthenticated",
                "a Bearer credential issued by the Portal is required",
            )
        requester = resolve_requester(token, self.tokens, self.principals)

        # 5) 认证之后才检查「自报身份」：伪造尝试需要先是一个已知主体才谈得上审计。
        reject_identity_assertions(_header_names(environ), body)

        envelope = self._invoke(handler, requester, found.groupdict(), body, environ)
        return _json_response({"ok": True, **envelope}, 200, request_id)

    # -- 各路由 -----------------------------------------------------------

    def _healthz_payload(self) -> dict:
        """未认证存活探针。**只**说明「本进程能应答」，不含任何租户数据。"""
        return {
            "ok": True,
            "data": {
                "state": "available",
                "mode": self.service.config.mode,
                "synthetic": self.service.config.synthetic,
                "dto_schema_version": self.service.store.schema_version(),
                "authentication": "required_for_all_other_routes",
            },
        }

    def _invoke(
        self,
        handler: str,
        requester: object,
        params: dict,
        body: object,
        environ: dict,
    ) -> dict:
        object_id = params.get("object_id")
        action = params.get("action")
        idempotency_key = _header(environ, "idempotency-key")

        if handler == "_route_whoami":
            return self.service.whoami(requester)
        if handler == "_route_status":
            return self.service.service_status(requester)
        if handler == "_route_usage_mine":
            return self.service.usage_mine(requester)
        if handler == "_route_audit":
            return self.service.audit_log(requester)
        if handler == "_route_routing_config":
            return self.service.routing_config(requester)
        if handler == "_route_routing_snapshot":
            return self.service.routing_snapshot(requester)

        if handler == "_route_engines_list":
            return self.service.list_engines(requester)
        if handler == "_route_engines_create":
            envelope, created = self.service.register_engine(
                requester, _require_object(body), idempotency_key=idempotency_key
            )
            envelope["created"] = created
            return envelope
        if handler == "_route_engines_preflight":
            return self.service.preflight_engine(requester, _require_object(body))
        if handler == "_route_engines_get":
            return self.service.get_engine(requester, object_id)
        if handler == "_route_engines_action":
            if action != "verify":
                raise TransportError(404, "registry.not_found", "no such engine action")
            return self.service.verify_engine(requester, object_id)

        if handler == "_route_models_list":
            return self.service.list_models(requester)
        if handler == "_route_models_create":
            envelope, created = self.service.register_model(
                requester, _require_object(body), idempotency_key=idempotency_key
            )
            envelope["created"] = created
            return envelope
        if handler == "_route_models_get":
            return self.service.get_model(requester, object_id)

        if handler == "_route_deployments_list":
            return self.service.list_deployments(requester)
        if handler == "_route_deployments_create":
            envelope, created = self.service.register_deployment(
                requester, _require_object(body), idempotency_key=idempotency_key
            )
            envelope["created"] = created
            return envelope
        if handler == "_route_deployments_get":
            return self.service.get_deployment(requester, object_id)
        if handler == "_route_deployments_action":
            if action != "verify":
                raise TransportError(404, "registry.not_found", "no such deployment action")
            return self.service.verify_deployment(requester, object_id)

        if handler == "_route_endpoints_list":
            return self.service.list_endpoints(requester)
        if handler == "_route_endpoints_create":
            envelope, created = self.service.register_endpoint(
                requester, _require_object(body), idempotency_key=idempotency_key
            )
            envelope["created"] = created
            return envelope
        if handler == "_route_endpoints_get":
            return self.service.get_endpoint(requester, object_id)
        if handler == "_route_endpoints_acl":
            return self.service.endpoint_acl(requester, object_id)
        if handler == "_route_endpoints_routings":
            return self.service.endpoint_routings(requester, object_id)
        if handler == "_route_endpoints_action":
            if action == "verify":
                return self.service.verify_endpoint(requester, object_id)
            if action == "share":
                return self.service.share_endpoint(
                    requester, object_id, _require_object(body)
                )
            if action == "unshare":
                return self.service.unshare_endpoint(
                    requester, object_id, _optional_object(body)
                )
            if action == "invoke":
                return self.service.invoke(requester, object_id, _require_object(body))
            if action == "repoint":
                return self.service.repoint_endpoint(
                    requester, object_id, _require_object(body)
                )
            if action == "rollback":
                return self.service.rollback_endpoint(
                    requester, object_id, _optional_object(body)
                )
            if action == "routing-applied":
                return self.service.ack_routing_applied(
                    requester, object_id, _require_object(body)
                )
            raise TransportError(404, "registry.not_found", "no such endpoint action")

        raise TransportError(404, "registry.not_found", "no such route")  # pragma: no cover


def _require_object(body: object) -> dict:
    if not isinstance(body, dict):
        raise TransportError(400, "registry.invalid_request", "a JSON object body is required")
    return body


def _optional_object(body: object) -> dict:
    if body is None:
        return {}
    return _require_object(body)


_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    411: "Length Required",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _reason(status: int) -> str:
    return _REASONS.get(status, "Unknown")


def create_app(
    service: RegistryService,
    *,
    debug_errors: bool = False,
) -> RegistryApplication:
    """构造 WSGI 应用。**不启动任何监听。**"""
    return RegistryApplication(service, debug_errors=debug_errors)
