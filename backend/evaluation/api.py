"""评测中心的 HTTP 面：路由表 + WSGI 应用 + 契约错误到状态码的映射。

三条纪律，与 ``backend.registry.api`` / ``backend.auth.wsgi`` 一致：

**契约层不发 HTTP。** ``errors.py`` 里的每个错误只有一个 ``contract_code``；状态码
在这里（:data:`CONTRACT_STATUS`）统一决定。因此"某个错误该是 403 还是 404"是一个
可以逐行 review 的映射表，而不是散落在业务代码里的 ``raise HTTPException``。

**路由表是唯一真源。** :data:`ROUTES` 同时驱动请求分发与 ``openapi.py`` 的产出，
所以"文档里写了但代码里没有"或"代码里有但文档没写"会由 ``tests/evaluation``
的漂移测试抓住。

**开发专用路由不在生产路由表里。** 让 mock runner 出一个结果的那个入口
（``POST /jobs/{id}/mock-run``）默认**不注册**，只有显式 ``enable_mock_routes=True``
才出现。这与 ``dev/preview_auth.py`` 里"预览路由不在生产路由器里"是同一个做法。

传输层的职责边界：解析 HTTP、取出凭据、拒绝自报身份、把请求交给 service、
把 service 的结论翻译成状态码。**没有**任何授权判断发生在这一层之外的地方——
这一层自己也不做授权判断。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qs

from . import dto, errors
from .dto import SCHEMA_VERSION
from .identity import (
    FORBIDDEN_IDENTITY_FIELDS,
    FORBIDDEN_IDENTITY_HEADERS,
    extract_bearer_token,
)
from .service import EvaluationService

#: 请求体上限。提交请求是一段很小的 JSON，64 KiB 已经非常宽松。
MAX_BODY_BYTES = 64 * 1024

#: 契约错误 -> HTTP 状态码。**只在这里**决定。
CONTRACT_STATUS: Mapping[str, int] = {
    errors.InvalidRequest.contract_code: 400,
    errors.IdentityAssertionRejected.contract_code: 400,
    errors.Unauthenticated.contract_code: 401,
    errors.AccessDenied.contract_code: 403,
    errors.SharedTargetGpuStressRefused.contract_code: 403,
    errors.NotFound.contract_code: 404,
    errors.TargetNotAuthorized.contract_code: 404,
    errors.Conflict.contract_code: 409,
    # 有界下载被拒（超出声明上限 / 不是普通文件 / 此刻不在）。413 而不是 400：引用本身没
    # 问题，是**这一份产物**不能按声明的上界交出去。
    errors.ReportDownloadRefused.contract_code: 413,
    errors.TargetNotReady.contract_code: 409,
    errors.OutsideTimeWindow.contract_code: 409,
    errors.DispatchForbidden.contract_code: 409,
    errors.RateLimited.contract_code: 429,
    errors.RunnerUnavailable.contract_code: 503,
    errors.NotConfigured.contract_code: 501,
    errors.ReportPathRejected.contract_code: 400,
    errors.ReportSourceMalformed.contract_code: 503,
    errors.InternalError.contract_code: 500,
    errors.StorageError.contract_code: 500,
    errors.EvaluationError.contract_code: 500,
}

_AUTH_HEADER = "HTTP_AUTHORIZATION"

_MISSING = object()


@dataclass(frozen=True)
class QueryParam:
    name: str
    description: str
    schema: dict

    def to_openapi(self) -> dict:
        return {
            "name": self.name,
            "in": "query",
            "required": False,
            "description": self.description,
            "schema": dict(self.schema),
        }


@dataclass(frozen=True)
class Route:
    """一条路由。``template`` 里的 ``{name}`` 段是路径参数。"""

    method: str
    template: str
    handler: str
    operation_id: str
    summary: str
    description: str = ""
    auth: bool = True
    dev_only: bool = False
    #: 请求体是**必需**的（提交）。
    accepts_body: bool = False
    #: 请求体是**可选**的（取消/重试可以带一条 reason）。二者都为假时，处理函数
    #: 根本不会收到 ``body``——这样"哪些端点读请求体"在路由表里一眼可见。
    body_optional: bool = False
    query_params: tuple[QueryParam, ...] = ()
    response_schema: str = "#/components/schemas/Envelope"

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(part for part in self.template.split("/") if part)

    def to_openapi(self) -> dict:
        operation: dict = {
            "operationId": self.operation_id,
            "summary": self.summary,
            "description": self.description
            or ("开发专用入口：默认不注册。" if self.dev_only else ""),
            "tags": ["evaluation"],
            "responses": {
                "200": {
                    "description": "契约响应",
                    "content": {"application/json": {"schema": {"$ref": self.response_schema}}},
                },
                "400": {"description": "请求形状非法或自报身份"},
                "401": {"description": "凭据缺失或未知"},
                "403": {"description": "身份已确认但操作不被允许"},
                "404": {"description": "不存在或无权知道其存在"},
                "409": {"description": "与当前状态冲突"},
                "429": {"description": "超出提交配额"},
            },
        }
        if self.accepts_body:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/EvaluationSubmitRequest"}
                    }
                },
            }
        if self.query_params:
            operation["parameters"] = [param.to_openapi() for param in self.query_params]
        if self.auth:
            operation["security"] = [{"bearerAuth": []}]
        if self.dev_only:
            operation["x-dev-only"] = True
        return operation


_JOB_ID_PARAM = {
    "name": "job_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "pattern": "^evj-[0-9a-f]{24}$"},
}


def _with_job_id(operation: dict) -> dict:
    operation.setdefault("parameters", []).insert(0, dict(_JOB_ID_PARAM))
    return operation


#: 路由表。产出与分发共用这一份。
ROUTES: tuple[Route, ...] = (
    Route(
        method="GET",
        template="/api/evaluation/health",
        handler="health",
        operation_id="evaluationHealth",
        summary="适配器、准入与报告根的只读状态",
        description="不需要身份；不含任何主体信息。",
        auth=False,
    ),
    Route(
        method="GET",
        template="/api/evaluation/openapi.json",
        handler="openapi_document",
        operation_id="evaluationOpenapi",
        summary="本契约的 OpenAPI 文档",
        auth=False,
    ),
    Route(
        method="GET",
        template="/api/evaluation/datasets",
        handler="datasets",
        operation_id="evaluationDatasets",
        summary="评测类型、允许清单数据集、参数范围与安全预设",
    ),
    Route(
        method="GET",
        template="/api/evaluation/admission",
        handler="admission",
        operation_id="evaluationAdmission",
        summary="真实派发此刻为何被允许/被禁止",
    ),
    Route(
        method="GET",
        template="/api/evaluation/modules",
        handler="modules",
        operation_id="evaluationModules",
        summary="V6 评测中心的入口授权（控制面）",
    ),
    Route(
        method="GET",
        template="/api/evaluation/monitoring",
        handler="monitoring",
        operation_id="evaluationMonitoring",
        summary="监控面聚合：状态计数与测不到的取值",
    ),
    Route(
        method="GET",
        template="/api/evaluation/report-sources",
        handler="report_sources",
        operation_id="evaluationReportSources",
        summary="报告来源（EvalScope / 旧 Benchmark）的只读状态",
    ),
    Route(
        method="GET",
        template="/api/evaluation/reports",
        handler="reports",
        operation_id="evaluationListReports",
        summary="按来源与权限列出的只读报告",
        query_params=(
            QueryParam(
                "include_unowned",
                "把归属无法核实的历史记录纳入候选（仍需 admin:manage 才会真正显示）",
                {"type": "boolean"},
            ),
        ),
    ),
    Route(
        method="GET",
        template="/api/evaluation/reports/{report_ref}",
        handler="report_detail",
        operation_id="evaluationGetReport",
        summary="按不透明引用取一份报告（不存在与不可见共用 404）",
    ),
    Route(
        method="GET",
        template="/api/evaluation/jobs",
        handler="jobs",
        operation_id="evaluationListJobs",
        summary="列出可见的评测任务",
        query_params=(
            QueryParam("status", "按状态过滤", {"type": "string"}),
            QueryParam("limit", "页大小（1..200）", {"type": "integer", "minimum": 1, "maximum": 200}),
            QueryParam("offset", "偏移", {"type": "integer", "minimum": 0}),
        ),
    ),
    Route(
        method="POST",
        template="/api/evaluation/jobs",
        handler="submit",
        operation_id="evaluationSubmitJob",
        summary="提交一个评测任务（进入队列）",
        accepts_body=True,
    ),
    Route(
        method="GET",
        template="/api/evaluation/jobs/{job_id}",
        handler="job",
        operation_id="evaluationGetJob",
        summary="一个任务的状态",
    ),
    Route(
        method="GET",
        template="/api/evaluation/jobs/{job_id}/result",
        handler="result",
        operation_id="evaluationGetResult",
        summary="一个任务的结果摘要与报告引用",
        # ``version`` **必须**在这里声明：查询参数的取值域由路由决定
        # （见 ``EvaluationApplication._read_query``），不声明就等于在 HTTP 面上
        # 静默丢弃它——响应里却还广告着 ``versions_available``。
        query_params=(
            QueryParam("version", "取特定版本的结果；缺省为最新版本", {"type": "integer", "minimum": 1}),
        ),
    ),
    Route(
        method="POST",
        template="/api/evaluation/jobs/{job_id}/cancel",
        handler="cancel",
        operation_id="evaluationCancelJob",
        summary="取消一个任务（幂等）",
        body_optional=True,
    ),
    Route(
        method="POST",
        template="/api/evaluation/jobs/{job_id}/retry",
        handler="retry",
        operation_id="evaluationRetryJob",
        summary="重试一个失败/取消的任务（幂等）",
        body_optional=True,
    ),
    Route(
        method="POST",
        template="/api/evaluation/jobs/{job_id}/mock-run",
        handler="mock_run",
        operation_id="evaluationMockRun",
        summary="开发专用：让 mock runner 产出一份合成结果",
        dev_only=True,
    ),
)


#: 全部路由声明过的查询参数的**并集**。
#:
#: 仅供文档与自检使用：**解析的取值域是 ``route.query_params``**，不是这个集合。
#: 一个全局集合会让"某个参数只对某条路由有意义"变成一句需要靠约定维持的话。
QUERY_PARAMS = frozenset(param.name for route in ROUTES for param in route.query_params)


def routes_for(*, enable_mock_routes: bool) -> tuple[Route, ...]:
    """按配置解析出实际注册的路由表。"""
    if enable_mock_routes:
        return ROUTES
    return tuple(route for route in ROUTES if not route.dev_only)


@dataclass
class Response:
    status: int
    payload: object
    headers: dict[str, str] = field(default_factory=dict)


class EvaluationApplication:
    """WSGI 应用。解析、分发、翻译错误；不做授权判断。"""

    def __init__(self, service: EvaluationService, *, enable_mock_routes: bool = False) -> None:
        self._service = service
        self._routes = routes_for(enable_mock_routes=enable_mock_routes)
        self._dev_routes_enabled = bool(enable_mock_routes)

    # -- WSGI ---------------------------------------------------------------

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        response = self.handle(environ)
        body = json.dumps(response.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            # 评测响应里含授权快照与报告引用，不应被任何中间层缓存。
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(body)),
            **response.headers,
        }
        start_response(f"{response.status} {_REASON.get(response.status, '')}".strip(), list(headers.items()))
        return [body]

    def handle(self, environ: dict) -> Response:
        try:
            return self._dispatch(environ)
        except errors.EvaluationError as exc:
            return self._error(exc)
        except dto.DtoError as exc:
            # ``DtoError`` 是 ``ValueError`` 的子类，因此**必须**排在它前面。一份 DTO
            # 偏离了自己的字段清单是**我们的**缺陷，不是调用方的——报成 400 既误导
            # 用户，又让真实缺陷在监控里看起来像噪声。
            return self._error(errors.InternalError(f"internal DTO contract violation: {exc}"))
        except (ValueError, TypeError) as exc:  # 传输层自己的形状错误
            return self._error(errors.InvalidRequest(str(exc) or "malformed request"))

    # -- 分发 ---------------------------------------------------------------

    def _dispatch(self, environ: dict) -> Response:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))

        rejected = self._reject_identity_headers(environ)
        if rejected is not None:
            return rejected

        matched, path_params = self._match(method, path)
        if matched is None:
            # 路径存在但方法不对 -> 405；否则 404，且不区分二者之外的任何信息。
            allowed = [route.method for route in self._routes if self._path_matches(route, path)]
            if allowed:
                return self._error(
                    errors.InvalidRequest(f"method {method} is not allowed for this path"),
                    status=405,
                    extra_headers={"Allow": ", ".join(sorted(set(allowed)))},
                )
            return self._error(errors.NotFound("no such evaluation endpoint"), status=404)

        # 查询参数**在匹配之后**解析：取值域由命中的那条路由决定。
        query = self._parse_query(environ.get("QUERY_STRING", ""), matched)
        body = self._read_body(environ) if (matched.accepts_body or matched.body_optional) else None
        return self._invoke(matched, environ, path_params, query, body)

    def _match(self, method: str, path: str) -> tuple[Route | None, dict[str, str]]:
        for route in self._routes:
            if route.method != method:
                continue
            params = self._extract(route, path)
            if params is not None:
                return route, params
        return None, {}

    def _path_matches(self, route: Route, path: str) -> bool:
        return self._extract(route, path) is not None

    @staticmethod
    def _extract(route: Route, path: str) -> dict[str, str] | None:
        wanted = route.segments
        got = tuple(part for part in path.split("/") if part)
        if len(wanted) != len(got):
            return None
        params: dict[str, str] = {}
        for expected, actual in zip(wanted, got):
            if expected.startswith("{") and expected.endswith("}"):
                params[expected[1:-1]] = actual
            elif expected != actual:
                return None
        return params

    def _invoke(
        self,
        route: Route,
        environ: dict,
        path_params: Mapping[str, str],
        query: Mapping[str, object],
        body: object,
    ) -> Response:
        handler = getattr(self, route.handler, None)
        if handler is None:  # pragma: no cover - 路由表与实现漂移时暴露
            # 路由表与实现同名，因此漂移会在这里、而不是在一个 404 里暴露出来。
            raise AssertionError(f"route {route.operation_id!r} has no handler")

        kwargs: dict = {"query": query, **path_params}
        if route.accepts_body or route.body_optional:
            kwargs["body"] = body

        if not route.auth:
            return Response(200, handler(**kwargs))

        token = extract_bearer_token(environ.get(_AUTH_HEADER))
        header_names = [key[5:].replace("_", "-") for key in environ if key.startswith("HTTP_")]
        requester = self._service.resolve_request(
            token=token, header_names=header_names, body=body
        )
        return Response(200, handler(requester=requester, **kwargs))

    # -- 传输层检查 ---------------------------------------------------------

    def _reject_identity_headers(self, environ: dict) -> Response | None:
        names = {key[5:].replace("_", "-").lower() for key in environ if key.startswith("HTTP_")}
        offending = sorted(names & FORBIDDEN_IDENTITY_HEADERS)
        if offending:
            exc = errors.IdentityAssertionRejected(
                "identity must not be asserted by the client; "
                f"remove header(s): {', '.join(offending)}"
            )
            return self._error(exc)
        return None

    def _parse_query(self, raw: object, route: Route) -> dict:
        """读取**该路由声明过的**查询参数。

        取值域来自路由表（``route.query_params``），而不是一个全局集合：这样"哪些参数
        有意义"与 OpenAPI 文档、与处理函数读的键三者不可能漂移。一个路由没声明的参数
        （例如 ``?version=99`` 打在没有 ``version`` 的路由上）会被忽略——但它**不会**
        被假装采纳，因此调用方不会以为自己选了一个版本。
        """
        if not isinstance(raw, str) or not raw:
            return {}
        parsed = parse_qs(raw, keep_blank_values=True)

        # 授权类名字出现在查询串里 = 一次伪造尝试。拒绝，而不是忽略。
        forged = sorted(name.lower() for name in parsed if name.lower() in FORBIDDEN_IDENTITY_FIELDS)
        if forged:
            raise errors.IdentityAssertionRejected(
                f"identity must not be asserted in the query string; remove: {', '.join(forged)}"
            )

        declared = {param.name for param in route.query_params}
        result: dict = {}
        for name in declared:
            values = parsed.get(name)
            if not values:
                continue
            if len(values) > 1:
                # 同一个参数出现两次：不在两种含义里替调用方选一个。
                raise errors.InvalidRequest(f"query parameter {name!r} was supplied more than once")
            value = values[0]
            if value == "":
                raise errors.InvalidRequest(f"query parameter {name!r} must not be empty")
            result[name] = value
        return result

    @staticmethod
    def _read_body(environ: dict) -> object:
        raw_length = environ.get("CONTENT_LENGTH") or "0"
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise errors.InvalidRequest("Content-Length is not a number") from None
        if length < 0:
            raise errors.InvalidRequest("Content-Length must not be negative")
        if length > MAX_BODY_BYTES:
            raise errors.InvalidRequest(f"request body exceeds {MAX_BODY_BYTES} bytes")

        stream = environ.get("wsgi.input")
        if stream is None:
            return {}
        data = stream.read(length) if length else b""
        if len(data) != length:
            raise errors.InvalidRequest("request body was shorter than Content-Length")
        if not data:
            return {}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise errors.InvalidRequest("request body is not valid UTF-8 JSON") from None
        if not isinstance(parsed, dict):
            raise errors.InvalidRequest("request body must be a JSON object")
        return parsed

    # -- 错误翻译 -----------------------------------------------------------

    def _error(self, exc: errors.EvaluationError, *, status: int | None = None, extra_headers: dict | None = None) -> Response:
        code = status if status is not None else CONTRACT_STATUS.get(exc.contract_code, 500)
        body = {
            "ok": False,
            "error": {
                "contract_code": exc.contract_code,
                # 只有声明可外发的错误才带消息：存储层的话术里有库路径与 schema 细节。
                "message": str(exc) if getattr(exc, "expose_message", True) else None,
                "schema_version": SCHEMA_VERSION,
            },
        }
        headers = dict(extra_headers or {})
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, int) and retry_after > 0:
            headers["Retry-After"] = str(retry_after)
        return Response(code, body, headers)

    # -- 处理器 -------------------------------------------------------------

    def health(self, *, query: Mapping[str, object]) -> dict:
        # 匿名入口：scope 里的 principal 是 None，而不是"某个默认主体"。
        return self._service.capabilities_envelope()

    def openapi_document(self, *, query: Mapping[str, object]) -> dict:
        from .openapi import build_openapi_document

        return build_openapi_document()

    def datasets(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.datasets(requester=requester)

    def admission(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.admission_view(requester=requester)

    def modules(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.module_gate(requester=requester)

    def monitoring(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.monitoring(requester=requester)

    def report_sources(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.report_sources(requester=requester)

    def reports(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.reports(
            requester=requester,
            include_unowned=_bool_or_false(query.get("include_unowned")),
        )

    def report_detail(
        self,
        *,
        requester,
        report_ref: str,
        query: Mapping[str, object],
        body: object = None,
    ) -> dict:
        return self._service.report_detail(requester=requester, report_ref=report_ref)

    def jobs(self, *, requester, query: Mapping[str, object]) -> dict:
        return self._service.jobs(
            requester=requester,
            status=query.get("status"),
            limit=_int_or_none(query.get("limit"), "limit"),
            offset=_int_or_none(query.get("offset"), "offset") or 0,
        )

    def submit(self, *, requester, query: Mapping[str, object], body: object) -> dict:
        return self._service.submit(requester=requester, request=body or {})

    def job(self, *, requester, job_id: str, query: Mapping[str, object], body: object = None) -> dict:
        return self._service.job(requester=requester, job_id=job_id)

    def result(self, *, requester, job_id: str, query: Mapping[str, object], body: object = None) -> dict:
        version = _int_or_none(query.get("version"), "version")
        return self._service.result(requester=requester, job_id=job_id, version=version)

    def cancel(self, *, requester, job_id: str, query: Mapping[str, object], body: object = None) -> dict:
        reason = None
        if isinstance(body, Mapping):
            unknown = sorted(set(body) - {"reason"})
            if unknown:
                raise errors.InvalidRequest(f"unsupported cancel field(s): {', '.join(unknown)}")
            reason = body.get("reason")
        return self._service.cancel_job(requester=requester, job_id=job_id, reason=reason)

    def retry(self, *, requester, job_id: str, query: Mapping[str, object], body: object = None) -> dict:
        return self._service.retry_job(requester=requester, job_id=job_id, request=body or {})

    def mock_run(self, *, requester, job_id: str, query: Mapping[str, object], body: object = None) -> dict:
        # 双重条件：应用没开这个路由就到不了这里；服务层还会再确认一次 runner 就是 mock。
        if not self._dev_routes_enabled:  # pragma: no cover - 路由表已挡
            raise errors.NotFound("no such evaluation endpoint")
        return self._service.run_mock_attempt(requester=requester, job_id=job_id)


def _int_or_none(raw: object, what: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    if not re.fullmatch(r"-?\d+", text):
        raise errors.InvalidRequest(f"{what} must be an integer")
    return int(text)


#: 查询串里的布尔字面量。**只**认这几个写法：一个拼错的取值是错误，不是 ``False``。
_TRUE_LITERALS = frozenset({"true", "1"})
_FALSE_LITERALS = frozenset({"false", "0"})


def _bool_or_false(raw: object) -> bool:
    """把查询串里的 ``include_unowned`` 解析成布尔量；缺省与非法都**不**放行。

    缺省是 ``False``（保守方向）。非法取值抛 400——静默当成 ``False`` 会让一个写错的
    客户端以为自己拿到了无归属记录。
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    raise errors.InvalidRequest("include_unowned must be a boolean")


def create_app(service: EvaluationService, *, enable_mock_routes: bool = False) -> EvaluationApplication:
    """构造 WSGI 应用。``enable_mock_routes`` 默认 **False**。"""
    if not isinstance(service, EvaluationService):
        raise errors.InvalidRequest("create_app needs an EvaluationService")
    return EvaluationApplication(service, enable_mock_routes=enable_mock_routes)


_REASON = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Content Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


__all__ = [
    "CONTRACT_STATUS",
    "MAX_BODY_BYTES",
    "QUERY_PARAMS",
    "ROUTES",
    "EvaluationApplication",
    "QueryParam",
    "Response",
    "Route",
    "create_app",
    "routes_for",
]
