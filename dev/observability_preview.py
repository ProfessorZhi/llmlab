"""环回 WSGI 预览：把 ``backend/observability/**`` 接到一个**只有合成数据**的本地服务上。

用途是给 UI（V6）一个可跑烟的 Mock E2E 目标，以及给人肉看一眼 DTO 长什么样。
**它不是部署**：只绑环回、只提供合成数据、没有真实 key 目录、没有真实主机读数。

## 与生产的三条硬区别

1. **只绑环回。** ``--host`` 只接受 ``127.0.0.1`` / ``::1`` / ``localhost``；
   别的值直接拒绝启动。
2. **身份由启动参数决定，不由请求决定。** ``--as`` 在**启动时**选定一个演示主体。
   请求里任何 ``uid`` / ``role`` / ``scope`` 参数一律 400 —— 在预览里也不养成
   「从 URL 里读身份」的习惯，因为那种代码最容易在接线生产时被留下来。
   （``requested_uid`` 是例外：它是**管理主体主动指定查看对象**的合法参数，
   由模块自己鉴权，普通主体传它会被 403。）
3. **数据全合成。** 目录、登记表、Gateway 行、主机读数、引擎探测全部是编造的，
   并且响应里 ``synthetic=true``。引擎探测刻意混入一个「问不到」的，好让 UI 在
   开发期就见得到 UNKNOWN 的真实形态，而不是一屏绿色。

用法::

    python -m dev.observability_preview --as alice
    # 然后 curl http://127.0.0.1:8712/observability/usage/self
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from wsgiref.simple_server import make_server

from backend.observability import (
    SCOPE_ADMIN_READ,
    SCOPE_HOST_READ,
    SCOPE_SELF_READ,
    AccessDenied,
    ContractError,
    EngineProbeResult,
    EngineRecord,
    EngineRegistry,
    GatewayKeyBinding,
    InputLimitExceeded,
    InvalidBucket,
    InvalidRequest,
    InvalidWindow,
    KeyDirectory,
    MockEngineProbe,
    MockHostCollector,
    ObservabilityRequest,
    Provenance,
    RequesterPrincipal,
    UnknownMetric,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    build_admin_overview_dto,
    build_engine_status_dto,
    build_self_usage_dto,
    build_timeline_dto,
)

#: 预览默认端口。取一个高位端口，避开常规服务。
DEFAULT_PORT = 8712

#: 允许绑定的主机。**白名单**，不是「非空即通过」。
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

#: 预览数据的来源声明。
PREVIEW_PROVENANCE = Provenance(source="synthetic-preview", synthetic=True)

#: 演示主体。每个都对应一份虚构的授权。
DEMO_PRINCIPALS = {
    "alice": RequesterPrincipal(
        uid="u-1001",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ}),
        authorized_endpoints=frozenset({"model-shared-a"}),
    ),
    "bob": RequesterPrincipal(
        uid="u-1002",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ}),
        authorized_endpoints=frozenset({"model-shared-a", "model-shared-b"}),
    ),
    "carol": RequesterPrincipal(
        uid="u-2001",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ}),
        authorized_endpoints=frozenset({"model-shared-b", "model-private-c"}),
    ),
    # 只看得到整机读数、看不到全体用户摘要 —— 用来演示两个 scope 是分开的。
    "host": RequesterPrincipal(
        uid="u-8001",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ, SCOPE_HOST_READ}),
        authorized_endpoints=frozenset(),
    ),
    # 有管理 scope —— 看得到全体摘要与全部引擎。
    "admin": RequesterPrincipal(
        uid="u-9001",
        roles=frozenset({"user"}),
        scopes=frozenset({SCOPE_SELF_READ, SCOPE_ADMIN_READ, SCOPE_HOST_READ}),
        authorized_endpoints=frozenset(),
    ),
    # **只有 role 自称 admin、没有任何管理 scope** —— 用来演示 role 不开门。
    "role-admin": RequesterPrincipal(
        uid="u-9002",
        roles=frozenset({"admin", "owner"}),
        scopes=frozenset({SCOPE_SELF_READ}),
        authorized_endpoints=frozenset({"model-shared-a"}),
    ),
}


def demo_directory() -> KeyDirectory:
    """虚构的受信 key 目录。真实目录将来由 BFF 从受信配置加载。"""
    return KeyDirectory(
        [
            GatewayKeyBinding("key-alpha", "u-1001", "proj-a", "resume-parse"),
            GatewayKeyBinding("key-orphan", "u-1001", "proj-a", None),
            GatewayKeyBinding("key-beta", "u-1002", "proj-a", "code-assist"),
            GatewayKeyBinding("key-gamma", "u-2001", "proj-b", "eval-suite"),
            GatewayKeyBinding("key-retired", "u-3001", "proj-a", "resume-parse", revoked=True),
        ]
    )


def demo_registry() -> EngineRegistry:
    """虚构的引擎登记表。"""
    return EngineRegistry(
        [
            EngineRecord(
                engine_id="engine-shared-a",
                endpoint_id="model-shared-a",
                display_name="Shared A（演示）",
                visibility=VISIBILITY_SHARED,
                replicas_declared=2,
            ),
            EngineRecord(
                engine_id="engine-shared-b",
                endpoint_id="model-shared-b",
                display_name="Shared B（演示）",
                visibility=VISIBILITY_SHARED,
                replicas_declared=2,
            ),
            EngineRecord(
                engine_id="engine-private-c",
                endpoint_id="model-private-c",
                display_name="Carol 的私有 C（演示）",
                visibility=VISIBILITY_PRIVATE,
                owner_uid="u-2001",
                replicas_declared=1,
            ),
        ]
    )


def demo_engine_probe() -> MockEngineProbe:
    """演示探测：一好、一降级、一问不到。

    刻意**不**让全部为 up —— 开发期的 UI 必须见过 UNKNOWN 与 degraded 的真实形态。
    时间戳在调用时生成，所以预览里不会一上来就显示「过期」。
    """
    now = datetime.now(timezone.utc)
    return MockEngineProbe(
        {
            "engine-shared-a": EngineProbeResult(
                up=True, observed_at=now, replicas_up=2, latency_p95_ms=812.0
            ),
            "engine-shared-b": EngineProbeResult(
                up=True, observed_at=now, replicas_up=1, latency_p95_ms=1450.5
            ),
            "engine-private-c": EngineProbeResult(up=None, reason="source_unavailable"),
        }
    )


#: 合成 Gateway 行。每条都带 ``"fixture":"SYNTHETIC-PREVIEW"`` 标记。
def demo_rows(now: datetime) -> str:
    """以 ``now`` 为基准生成一批**窗口内**的合成行。

    用相对时间而不是写死日期，预览在任何时候打开都有数据落在默认 1 小时窗口里。
    """
    def moment(minutes_ago: int) -> str:
        stamp = (now - timedelta(minutes=minutes_ago)).astimezone(timezone.utc)
        return stamp.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    def line(ts, key_id, endpoint, *, status=200, outcome="success", stream=True,
             queue_ms=5, ttft_ms=120, total_ms=1000, tokens=(100, 200), extra=None):
        payload = {
            "ts": ts,
            "key_id": key_id,
            "fixture": "SYNTHETIC-PREVIEW",
            "label": "resume-parse",
            "method": "POST",
            "path": "/v1/chat/completions",
            "model": endpoint,
            "status": status,
            "outcome": outcome,
            "stream": stream,
            "queue_ms": queue_ms,
            "ttft_ms": ttft_ms if stream else None,
            "upstream_ms": total_ms,
            "total_ms": total_ms,
            "bytes_out": 4096,
            "usage": (
                None if tokens is None
                else {"input_tokens": tokens[0], "output_tokens": tokens[1]}
            ),
        }
        if extra:
            payload.update(extra)
        return json.dumps(payload, sort_keys=True)

    rows = [
        line(moment(52), "key-alpha", "model-shared-a", queue_ms=4, ttft_ms=110, total_ms=900),
        line(moment(38), "key-alpha", "model-shared-a", queue_ms=6, ttft_ms=140, total_ms=1150),
        line(moment(25), "key-orphan", "model-shared-a", stream=False, ttft_ms=None,
             queue_ms=2, total_ms=700, tokens=(20, 30)),
        line(moment(18), "key-alpha", "model-shared-a", status=429, outcome="failed",
             stream=False, ttft_ms=None, queue_ms=1, total_ms=None, tokens=None),
        # 其他主体 —— 普通主体在自己的响应里看不到它们，管理视角看得到。
        line(moment(44), "key-beta", "model-shared-a", queue_ms=3, total_ms=820,
             tokens=(50, 150)),
        line(moment(30), "key-gamma", "model-shared-b", queue_ms=9, ttft_ms=900,
             total_ms=5000, tokens=(10, 400)),
        # 未注册 key + 自报身份（必须被忽略）
        line(moment(12), "unknown-key-demo", "model-shared-b", queue_ms=0, ttft_ms=40,
             total_ms=50, tokens=(1, 1),
             extra={"x_principal": "u-1001", "X-Forwarded-User": "u-1001"}),
        # 已吊销 key
        line(moment(9), "key-retired", "model-shared-a", stream=False, ttft_ms=None,
             queue_ms=1, total_ms=400, tokens=(5, 5)),
        # 带正文类字段（会被拒绝计数）
        line(moment(6), "key-gamma", "model-shared-b", queue_ms=4, ttft_ms=200,
             total_ms=1100, tokens=(5, 5),
             extra={"prompt": "<SYNTHETIC-PREVIEW: 正文类字段会被拒绝>"}),
    ]
    rows.insert(7, '{"ts":"broken","key_id":"key-gamma","fixture":"SYNTHETIC-PREVIEW"')
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# WSGI
# ---------------------------------------------------------------------------

#: 路由表：路径 -> (视图名, 是否需要 directory/registry)。
ROUTES = {
    "/observability/usage/self": "usage_self",
    "/observability/usage/admin": "usage_admin",
    "/observability/engines": "engines",
    "/observability/timeline": "timeline",
}

#: **不接受**的查询参数：身份与授权只能来自会话，永远不能来自 URL。
FORBIDDEN_QUERY_PARAMS = ("uid", "user", "principal", "subject", "role", "roles",
                          "scope", "scopes", "endpoints", "token", "key", "path",
                          "url", "promql", "query", "expr")

#: 会被转成 HTTP 状态码的契约错误。
_STATUS_BY_ERROR = (
    (AccessDenied, 403),
    (InvalidWindow, 400),
    (InvalidBucket, 400),
    (InputLimitExceeded, 400),
    (UnknownMetric, 400),
    (InvalidRequest, 400),
)


def _status_for(exc: ContractError) -> int:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 500


def _json_response(start_response, status: int, payload: dict):
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    start_response(
        f"{status} {'OK' if status == 200 else 'Preview'}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Preview-Only", "synthetic-loopback"),
        ],
    )
    return [body]


def _error_response(start_response, status: int, exc: ContractError):
    return _json_response(
        start_response,
        status,
        {
            "error": {
                "contract_code": getattr(exc, "contract_code", "observability.contract_error"),
                "detail": str(exc),
            },
            # 明确声明：这不是生产响应，失败也不是「服务不可用」。
            "synthetic": True,
        },
    )


def _single(query: dict, name: str):
    values = query.get(name)
    if not values:
        return None
    return values[-1]


def _parse_int(query: dict, name: str, default):
    raw = _single(query, name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidRequest(f"{name} must be an integer") from exc


def _parse_moment(query: dict, name: str) -> datetime | None:
    raw = _single(query, name)
    if raw is None or raw == "":
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidRequest(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidRequest(f"{name} must carry an explicit UTC offset")
    return parsed


class ObservabilityPreviewApp:
    """把四种视图挂到一个环回 WSGI 应用上。身份在**构造期**固定。"""

    def __init__(self, principal: RequesterPrincipal, *, principal_name: str = "demo") -> None:
        if not isinstance(principal, RequesterPrincipal):
            raise InvalidRequest("principal must be a RequesterPrincipal")
        self.principal = principal
        self.principal_name = principal_name
        self.directory = demo_directory()
        self.registry = demo_registry()

    # -- 请求解析 ----------------------------------------------------------

    def _build_request(self, query: dict, now: datetime) -> ObservabilityRequest:
        for name in FORBIDDEN_QUERY_PARAMS:
            if name in query:
                # 身份来自会话，不来自 URL。即便在预览里也不开这个口子。
                raise AccessDenied(
                    f"query parameter {name!r} is not accepted: identity and scope come "
                    "from the BFF session, never from the URL"
                )

        window_to = _parse_moment(query, "to") or now
        window_from = _parse_moment(query, "from") or (window_to - timedelta(hours=1))

        return ObservabilityRequest(
            principal=self.principal,
            window_from=window_from,
            window_to=window_to,
            requested_uid=_single(query, "requested_uid"),
            requested_endpoint=_single(query, "requested_endpoint"),
            bucket=_single(query, "bucket") or "1h",
            limit=_parse_int(query, "limit", 50),
            offset=_parse_int(query, "offset", 0),
        )

    # -- 视图 --------------------------------------------------------------

    def _render(self, view: str, request_obj: ObservabilityRequest, now: datetime) -> dict:
        text = demo_rows(now)
        common = (self.directory, self.registry, PREVIEW_PROVENANCE)

        if view == "usage_self":
            return build_self_usage_dto(request_obj, text, *common, now=now)
        if view == "usage_admin":
            return build_admin_overview_dto(
                request_obj,
                text,
                *common,
                now=now,
                host_report=MockHostCollector().collect(now),
                engine_probe=demo_engine_probe(),
            )
        if view == "engines":
            return build_engine_status_dto(
                request_obj, self.registry, PREVIEW_PROVENANCE, now=now,
                engine_probe=demo_engine_probe(),
            )
        if view == "timeline":
            return build_timeline_dto(request_obj, text, *common, now=now)
        raise InvalidRequest(f"unknown view {view!r}")  # pragma: no cover - 路由表固定

    # -- WSGI --------------------------------------------------------------

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        now = datetime.now(timezone.utc).replace(microsecond=0)

        if method not in ("GET", "HEAD"):
            return _json_response(
                start_response,
                405,
                {"error": {"contract_code": "observability.method_not_allowed",
                           "detail": "preview is read-only"},
                 "synthetic": True},
            )

        if path in ("/observability/healthz", "/healthz"):
            return _json_response(
                start_response,
                200,
                {
                    "ok": True,
                    "synthetic": True,
                    "principal": self.principal_name,
                    "views": sorted(ROUTES),
                    "notes": ["环回预览：数据全部为合成，不得作为生产依据。"],
                },
            )

        if path in ("/observability", "/observability/"):
            return _json_response(
                start_response,
                200,
                {
                    "synthetic": True,
                    "principal": self.principal_name,
                    "routes": sorted(ROUTES),
                    "query_parameters": ["bucket", "from", "to", "limit", "offset",
                                         "requested_uid", "requested_endpoint"],
                    "refused_query_parameters": list(FORBIDDEN_QUERY_PARAMS),
                    "notes": [
                        "身份由启动参数 --as 决定；URL 里给不出身份。",
                        "所有时间窗有界，桶数有硬上限。",
                        "缺测显示为 UNKNOWN，不是 0。",
                    ],
                },
            )

        view = ROUTES.get(path)
        if view is None:
            return _json_response(
                start_response,
                404,
                {"error": {"contract_code": "observability.no_such_view",
                           "detail": f"no preview route for {path!r}"},
                 "synthetic": True,
                 "routes": sorted(ROUTES)},
            )

        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        try:
            request_obj = self._build_request(query, now)
            dto = self._render(view, request_obj, now)
        except ContractError as exc:
            return _error_response(start_response, _status_for(exc), exc)
        return _json_response(start_response, 200, dto)


def build_app(principal_name: str = "alice") -> ObservabilityPreviewApp:
    """按名字取一个演示主体并组装应用。"""
    key = principal_name.strip().lower()
    principal_obj = DEMO_PRINCIPALS.get(key)
    if principal_obj is None:
        raise SystemExit(
            f"unknown demo principal {principal_name!r}; "
            f"choose one of: {', '.join(sorted(DEMO_PRINCIPALS))}"
        )
    return ObservabilityPreviewApp(principal_obj, principal_name=key)


def _split_host_port(value: str) -> tuple[str, int]:
    if ":" not in value:
        return value, DEFAULT_PORT
    host, _, port = value.rpartition(":")
    try:
        return host, int(port)
    except ValueError as exc:
        raise SystemExit(f"bad --bind value {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "环回预览：把监控后端接到合成数据上。"
            "只绑环回、只提供合成数据、不接任何生产组件。"
        )
    )
    parser.add_argument(
        "--as",
        dest="principal_name",
        default="alice",
        help=f"演示主体（{', '.join(sorted(DEMO_PRINCIPALS))}）；默认 alice",
    )
    parser.add_argument(
        "--bind",
        default=f"127.0.0.1:{DEFAULT_PORT}",
        help="host:port，host 必须是环回地址（默认 %(default)s）",
    )
    args = parser.parse_args(argv)

    host, port = _split_host_port(args.bind)
    if host not in LOOPBACK_HOSTS:
        # fail closed：宁可起不来，也不把预览暴露到环回之外。
        raise SystemExit(
            f"refusing to bind {host!r}: this preview only serves loopback "
            f"({', '.join(LOOPBACK_HOSTS)}). It has no authentication."
        )

    app = build_app(args.principal_name)
    with make_server(host, port, app) as server:
        print(f"observability preview (SYNTHETIC ONLY) on http://{host}:{port}")
        print(f"demo principal: {args.principal_name} ({app.principal.uid})")
        for route in sorted(ROUTES):
            print(f"  {route}")
        print("  /observability/healthz")
        print("press Ctrl+C to stop")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":  # pragma: no cover - 手动入口
    sys.exit(main())
