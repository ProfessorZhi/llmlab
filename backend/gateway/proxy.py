"""同源 ``/v1/*``：OpenAI 兼容的调用入口（WSGI）。

规则（沿用 ``legacy/gateway/pr-20`` 的五条）：

1. **默认拒绝**：只转发下表里的方法与路径；目标是路由表里登记过的回环服务，
   请求里的任何东西都改变不了它。
2. **上游凭据只在服务端**：引擎自己的 key 从部署配置的文件里读（只点名位置），
   在出站时附上；不进响应、不进日志。调用者自己的密钥绝不转发给上游。
3. **身份头一律丢弃**：只转发 ``Content-Type`` / ``Accept``，其余请求头不过去。
4. **失败如实转发**：上游的 4xx/5xx 原样回；连不上是 502，超时是 504。
5. **正文从不落盘**：只记元数据（时间、密钥、主体、模型、状态、token、耗时）。

流式响应边读边写，不整体缓冲；客户端断开时立刻释放并发名额（引擎那边已经开始的
生成停不下来，这一点不承诺）。
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import store as gw_store
from ..registry.presence import service_state

ALLOWED = {
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"),
    ("POST", "/v1/embeddings"),
}
MAX_BODY_BYTES = 16 * 1024 * 1024
UPSTREAM_TIMEOUT_SECONDS = 600
_TAIL_BYTES = 64 * 1024

CHANNEL_KEY = "api_key"
CHANNEL_PLAYGROUND = "playground"


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, *, kind: str = "invalid_request_error",
                 retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.kind = kind
        self.retry_after = retry_after

    def body(self) -> bytes:
        return json.dumps(
            {"error": {"message": self.message, "type": self.kind, "code": self.code}}, ensure_ascii=False
        ).encode("utf-8")


# --- 上游凭据 ----------------------------------------------------------------


def parse_secret_refs(items: Iterable[str]) -> dict[str, tuple[str, str]]:
    """``ENGINE=FILE:VAR``（ENGINE 是引擎名或 id）⇒ ``{engine: (file, var)}``。只点名位置。"""
    refs: dict[str, tuple[str, str]] = {}
    for raw in items or ():
        if "=" not in raw or ":" not in raw.split("=", 1)[1]:
            raise ValueError(f"expected ENGINE=FILE:VAR, got {raw!r}")
        engine, target = raw.split("=", 1)
        file_part, var = target.rsplit(":", 1)
        if not engine.strip() or not file_part.strip() or not var.strip():
            raise ValueError(f"expected ENGINE=FILE:VAR, got {raw!r}")
        refs[engine.strip()] = (os.path.expanduser(file_part.strip()), var.strip())
    return refs


def read_env_value(path: str, var: str) -> str | None:
    """从 ``VAR=value`` 形式的文件里读一个值。读不到就是 ``None``（不抛、不记录值）。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == var:
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


# --- 限流 --------------------------------------------------------------------


class Limiter:
    """单进程内的按密钥并发与每分钟请求数。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}
        self._recent: dict[str, deque] = {}
        self._clock = clock

    def acquire(self, key: str, *, rpm: int, concurrency: int) -> None:
        now = self._clock()
        with self._lock:
            window = self._recent.setdefault(key, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= rpm:
                retry = max(1, int(60 - (now - window[0])) + 1)
                raise GatewayError(429, "rate_limited", f"rate limit of {rpm} requests per minute reached",
                                   kind="rate_limit_error", retry_after=retry)
            if self._inflight.get(key, 0) >= concurrency:
                raise GatewayError(429, "concurrency_limited",
                                   f"this key already has {concurrency} request(s) in flight",
                                   kind="rate_limit_error", retry_after=1)
            window.append(now)
            self._inflight[key] = self._inflight.get(key, 0) + 1

    def release(self, key: str) -> None:
        with self._lock:
            self._inflight[key] = max(0, self._inflight.get(key, 0) - 1)

    def inflight(self) -> int:
        with self._lock:
            return sum(self._inflight.values())


# --- 核心 --------------------------------------------------------------------


@dataclass
class Caller:
    principal: str
    key_id: str | None
    channel: str
    rpm: int
    concurrency: int
    models: tuple[str, ...] | None = None
    log_bodies: bool = False


@dataclass
class GatewayCore:
    store: gw_store.GatewayStore
    registry: object
    secret_refs: dict[str, tuple[str, str]] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    limiter: Limiter = field(default_factory=Limiter)

    @property
    def personal_mode(self) -> bool:
        return bool(getattr(self.registry.config, "personal_mode", False))

    # -- 身份 --

    def principal_active(self, principal: str) -> bool:
        record = self.registry.principals.lookup(principal)
        return record is not None and record.approved

    def caller_for_key(self, raw: str | None) -> Caller:
        if not raw or not raw.startswith(gw_store.KEY_PREFIX):
            raise GatewayError(401, "invalid_api_key", "a valid LLM Lab API key is required",
                               kind="authentication_error")
        record = self.store.key_by_plaintext(raw)
        if record is None or not record.live(self.clock()) or not self.principal_active(record.principal):
            raise GatewayError(401, "invalid_api_key", "a valid LLM Lab API key is required",
                               kind="authentication_error")
        return Caller(principal=record.principal, key_id=record.id, channel=CHANNEL_KEY,
                      rpm=record.rpm, concurrency=record.concurrency, models=record.models,
                      log_bodies=record.log_bodies)

    # -- 路由 --

    def engine(self, engine_id: str):
        return self.registry.store.engine_by_id(engine_id)

    def can_use(self, principal: str, route: gw_store.Route, *, models: tuple[str, ...] | None = None) -> bool:
        if not route.enabled:
            return False
        if models is not None and route.alias not in models:
            return False
        if (self.personal_mode or route.visibility == gw_store.VISIBILITY_PRIVATE) and route.owner != principal:
            return False
        return self.engine(route.engine_id) is not None

    def routes_for(self, caller: Caller) -> list[gw_store.Route]:
        return [r for r in self.store.routes() if self.can_use(caller.principal, r, models=caller.models)]

    def resolve(self, caller: Caller, model: object) -> gw_store.Route:
        if not isinstance(model, str) or not model:
            raise GatewayError(400, "model_required", "the request body must name a model")
        route = self.store.route_by_alias(model)
        if route is None or not self.can_use(caller.principal, route, models=caller.models):
            # 不存在与无权限说同一句话：密钥不是枚举模型的预言机。
            raise GatewayError(404, "model_not_found", f"model {model!r} is not available to this key")
        return route

    def upstream_key(self, engine) -> str | None:
        ref = self.secret_refs.get(engine.id) or self.secret_refs.get(engine.name)
        return read_env_value(*ref) if ref else None

    # -- 转发 --

    def models_payload(self, caller: Caller) -> dict:
        names = {}
        for route in self.routes_for(caller):
            owner = self.registry.principals.lookup(route.owner)
            names[route.alias] = (owner.display_name or owner.linux_user) if owner else route.owner
        return {"object": "list", "data": [
            {"id": alias, "object": "model", "created": 0, "owned_by": owner} for alias, owner in names.items()
        ]}

    def forward(self, caller: Caller, path: str, payload: dict) -> tuple[int, list[tuple[str, str]], Iterable[bytes]]:
        route = self.resolve(caller, payload.get("model"))
        engine = self.engine(route.engine_id)
        if engine is None or service_state(engine) in ("offline", "stale") or engine.status == "unavailable":
            raise GatewayError(503, "service_offline", "the inference service is offline", kind="api_error")
        target = engine.target or {}
        stream = bool(payload.get("stream"))
        body = dict(payload)
        if route.upstream_model:
            body["model"] = route.upstream_model
        if stream and path == "/v1/chat/completions" and "stream_options" not in body:
            # 让上游在最后一个分片里报 token 数（OpenAI 标准字段，客户端 SDK 都认）。
            body["stream_options"] = {"include_usage": True}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

        limit_key = caller.key_id or f"{caller.channel}:{caller.principal}"
        self.limiter.acquire(limit_key, rpm=caller.rpm, concurrency=caller.concurrency)
        started = time.monotonic()
        request_id = "req_" + uuid.uuid4().hex[:16]
        record = {
            "ts": self.clock(), "request_id": request_id, "key_id": caller.key_id, "principal": caller.principal,
            "route_id": route.id, "model": route.alias, "route_owner": route.owner, "path": path,
            "status": None, "outcome": "failed", "stream": 1 if stream else 0, "prompt_tokens": None,
            "completion_tokens": None, "ttft_ms": None, "total_ms": None, "channel": caller.channel,
            "error": None, "has_body": 0,
        }
        # 只有密钥所有者打开了「记录请求内容」才留正文；默认只记元数据。
        capture = {"request": _clip(json.dumps(payload, ensure_ascii=False))} if caller.log_bodies else None
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json"}
        secret = self.upstream_key(engine)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        connection = http.client.HTTPConnection(target.get("host"), int(target.get("port")),
                                                timeout=UPSTREAM_TIMEOUT_SECONDS)
        try:
            connection.request("POST", path, body=raw, headers=headers)
            response = connection.getresponse()
        except TimeoutError:
            record["error"] = "upstream_timeout"
            self._finish(record, started, limit_key, connection, status=504, capture=capture)
            raise GatewayError(504, "upstream_timeout", "the inference service did not answer in time",
                               kind="api_error") from None
        except OSError:
            record["error"] = "upstream_unreachable"
            self._finish(record, started, limit_key, connection, status=502, capture=capture)
            raise GatewayError(502, "upstream_unreachable", "the inference service is not reachable",
                               kind="api_error") from None
        if self.store is not None and caller.key_id:
            self.store.touch_key(caller.key_id)
        record["status"] = response.status
        content_type = response.getheader("Content-Type") or "application/json"
        out_headers = [("Content-Type", content_type), ("Cache-Control", "no-store"),
                       ("X-Request-Id", request_id)]
        return response.status, out_headers, self._relay(response, record, started, limit_key, connection, stream,
                                                           capture)

    def _relay(self, response, record, started, limit_key, connection, stream, capture=None):
        tail = bytearray()
        head = bytearray() if capture is not None else None
        overflow = False
        outcome = "failed"
        try:
            first = True
            while True:
                chunk = response.read1(65536) if stream else response.read(65536)
                if not chunk:
                    break
                if first:
                    record["ttft_ms"] = (time.monotonic() - started) * 1000
                    first = False
                tail.extend(chunk)
                if head is not None:
                    room = gw_store.BODY_LIMIT_BYTES - len(head)
                    head.extend(chunk[:max(0, room)])
                    overflow = overflow or len(chunk) > room
                if len(tail) > _TAIL_BYTES:
                    del tail[: len(tail) - _TAIL_BYTES]
                yield chunk
            outcome = "success" if 200 <= (record["status"] or 0) < 300 else "failed"
        except GeneratorExit:
            outcome = "cancelled"
            raise
        except OSError:
            outcome = "failed"
        finally:
            prompt, completion = _usage_from_tail(bytes(tail), stream)
            record["prompt_tokens"], record["completion_tokens"] = prompt, completion
            record["outcome"] = outcome
            if (record["status"] or 0) >= 400:
                record["error"] = _error_from_tail(bytes(tail)) or f"upstream answered {record['status']}"
            elif outcome == "cancelled":
                record["error"] = "client_cancelled"
            if capture is not None:
                capture["response"] = bytes(head).decode("utf-8", errors="replace")
                capture["truncated"] = overflow
            self._finish(record, started, limit_key, connection, status=record["status"], capture=capture)

    def _finish(self, record, started, limit_key, connection, *, status, capture=None) -> None:
        record["status"] = status
        record["total_ms"] = (time.monotonic() - started) * 1000
        try:
            connection.close()
        finally:
            self.limiter.release(limit_key)
            try:
                if capture is not None:
                    record["has_body"] = 1
                    self.store.record_body(record["request_id"], request=capture.get("request"),
                                           response=capture.get("response"),
                                           truncated=bool(capture.get("truncated")))
                self.store.record(record)
            except Exception:  # noqa: BLE001 - 记账失败不能让一次成功的调用变成失败
                pass


def _clip(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= gw_store.BODY_LIMIT_BYTES:
        return text
    return raw[: gw_store.BODY_LIMIT_BYTES].decode("utf-8", errors="ignore")


def _error_from_tail(tail: bytes) -> str | None:
    """上游错误体里的 ``error.message``（截短）。只在失败时取，用来在日志里说明原因。"""
    try:
        body = json.loads(tail.decode("utf-8", errors="ignore"))
    except (json.JSONDecodeError, ValueError):
        return None
    err = body.get("error") if isinstance(body, dict) else None
    message = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else None)
    return message[:300] if isinstance(message, str) and message else None


def _usage_from_tail(tail: bytes, stream: bool) -> tuple[int | None, int | None]:
    """从响应尾部取上游自己报的 token 数（只取数字，正文不留）。"""
    candidates = []
    text = tail.decode("utf-8", errors="ignore")
    if stream:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:") and '"usage"' in line:
                candidates.append(line[5:].strip())
    else:
        candidates.append(text)
    for raw in reversed(candidates):
        try:
            usage = json.loads(raw).get("usage")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(usage, dict):
            p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
            return (p if isinstance(p, int) else None, c if isinstance(c, int) else None)
    return None, None


# --- WSGI --------------------------------------------------------------------


class GatewayApplication:
    """``/v1/*`` 的 WSGI 入口。由 BFF 按路径前缀挂载（同源、同一个进程）。"""

    def __init__(self, core: GatewayCore) -> None:
        self.core = core

    def __call__(self, environ, start_response):
        try:
            status, headers, body = self._handle(environ)
        except GatewayError as exc:
            headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")]
            if exc.retry_after:
                headers.append(("Retry-After", str(exc.retry_after)))
            start_response(f"{exc.status} {_reason(exc.status)}", headers)
            return [exc.body()]
        start_response(f"{status} {_reason(status)}", headers)
        return body

    def _handle(self, environ):
        method = (environ.get("REQUEST_METHOD") or "GET").upper()
        path = (environ.get("PATH_INFO") or "").rstrip("/") or "/"
        if (method, path) not in ALLOWED:
            raise GatewayError(404, "not_found", f"{method} {path} is not served by this gateway")
        caller = self.core.caller_for_key(_bearer(environ))
        if method == "GET":
            body = json.dumps(self.core.models_payload(caller), ensure_ascii=False).encode("utf-8")
            return 200, [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")], [body]
        payload = _read_json(environ)
        return self.core.forward(caller, path, payload)


def _bearer(environ) -> str | None:
    auth = environ.get("HTTP_AUTHORIZATION") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (environ.get("HTTP_X_API_KEY") or "").strip() or None


def _read_json(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        raise GatewayError(400, "body_required", "a JSON body is required")
    if length > MAX_BODY_BYTES:
        raise GatewayError(413, "payload_too_large", "the request body is too large")
    raw = environ["wsgi.input"].read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GatewayError(400, "invalid_json", "the body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise GatewayError(400, "invalid_json", "the body must be a JSON object")
    return payload


_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            413: "Payload Too Large", 429: "Too Many Requests", 500: "Internal Server Error",
            502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}


def _reason(status: int) -> str:
    return _REASONS.get(status, "OK" if status < 400 else "Error")
