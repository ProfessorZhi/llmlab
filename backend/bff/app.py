"""同源 BFF 的 WSGI 应用：``/api/v2/ops``、几条只读来源，与 Linux 登录入口。

路由表本身在 :mod:`backend.bff.route_map` —— **那才是唯一真源**，这里是它的
实现侧。``_dispatch`` 的形状是 fail-closed 的：先在表里查到声明才可能走到任何
实现，查不到就只剩「交给既有 auth 应用」与 404 两种结局。

    GET    /api/v2/ops                              管理后台的全部数据
    GET    /api/v2/ops/usage, /engines              观测模块的完整 DTO
    PUT    /api/v2/ops/deployments/{id}/share       改一个部署的共享范围
    POST   /api/v2/ops/endpoints/{id}/invoke        同源调用（薄桥接 Registry 的权威路径）
    GET    /api/infra/summary                       固定来源的主机投影（服务端判定）
    GET    /api/portal/services                     同一份判定的服务就绪切面
    GET    /api/portal/capabilities                 每条来源此刻对这个会话能不能读
    GET    /api/atlas/calls                         只有我自己的调用元数据
    GET    /api/evaluation/{report-sources,reports,reports/{ref}}
    POST   /auth/linux/login                        Linux 账号 + 密码登录
    GET    /auth/linux/status                       这条登录路径此刻的真实状态
    GET    /auth/admin/status                       这条会话此刻是否处于管理员模式
    POST   /auth/admin/enter                        用共享管理口令提权（限时 ≤ 15 分钟）
    POST   /auth/admin/exit                         退出管理员模式
    POST   /auth/logout                             登出（成功后一并清掉提权行）
    *      /auth/*                                  其余原样交给既有 auth 应用

几条边界，都是本文件存在的理由：

* **凭据只从 cookie 来，身份只由服务端推。** 每个受保护的路由先
  :func:`~backend.bff.identity.reject_identity_assertions` 再
  :func:`~backend.bff.identity.resolve_principal`。``X-UID`` / ``X-Admin``
  之类头出现即 400，不是「读到不用」。
* **写操作复用既有 CSRF / 同源实现。** 直接调既有 auth 应用的
  ``_preflight``，而不是在这里再写一份 Origin 比较——同一条安全规则有两份
  实现，就等于迟早有一份是过期的。
* **数据的来源与目标都由服务端装配决定。** ``?source=`` / ``?url=`` /
  ``?promql=`` / ``?path=`` / ``?owner=`` / ``?key=`` 在一个**封闭参数名**上
  被 400（:func:`~backend.bff.identity.reject_proxy_assertions`）。这些处理器
  本来就没有收它们的参数，守卫把「没有那根线」变成一件可以被断言的事实。
* **未接线与没数据是两个不同的答复。** 来源没装配 ⇒ 503；一次真实查询确实没
  找到 ⇒ 200 + 空结果。用 200 的空数据冒充前者，是契约违约而非降级。
* **浏览器拿不到任何离开同源的路径。** 响应里出现的每个 href 都由
  ``isInternalHref`` 那一套约束（根相对、无 ``://``、无 ``:8080``）；本应用
  也**拒绝**在 8080 上监听——那是上游 Gateway 的端口。
* **错误形状与前端状态机对齐**：401 → 未登录，403 → 无权限，其余 → UNKNOWN。
  错误体是 ``{"error": {"code", "message"}}``，正是前端读 ``error.code`` 的形状。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from ..atlas import errors as atlas_errors
from ..auth import errors as auth_errors
from ..auth import store as auth_store
from ..auth.routes import REASON_PHRASES
from ..auth.wsgi import build_request
from ..auth_linux import errors as linux_errors
from ..evaluation import api as evaluation_api
from ..evaluation import errors as evaluation_errors
from ..observability import errors as observability_errors
from ..observability import identity as observability_identity
from ..registry import api as registry_api
from ..registry import errors as registry_errors
from ..registry.models import VISIBILITY_ALL_APPROVED, VISIBILITY_USERS
from ..registry.registry import AGENT_TASK_KINDS
from . import admin_mode as bff_admin_mode
from . import atlas_adapter as bff_atlas
from . import capabilities as bff_capabilities
from . import engine_live as bff_engine_live
from . import events as bff_events
from . import host_summary as bff_host_summary
from . import identity as bff_identity
from . import invoke as bff_invoke
from . import ops as bff_ops
from ..gateway import service as bff_gateway_service
from ..runtime import params as runtime_params
from . import integrations as bff_integrations
from . import origin as bff_origin
from . import route_map as bff_route_map
from . import scopes as bff_scopes
from . import sharing as bff_sharing
from .handoff import PortalSessionHandoff, sign_in_with_linux

#: :8080 是上游 Gateway 的端口。BFF 自己绝不监听它，也不给浏览器任何指向它的
#: 字符串——这条规则在 Registry 的 ``urls.RESERVED_UPSTREAM_PORTS`` 与前端
#: ``isInternalHref`` 里各有一份，这里是第三份，且它拒绝的是**绑定**。
RESERVED_BIND_PORTS = frozenset({8080})

DEFAULT_REASON = "OK"

#: 「调用方没有提供这个参数」的哨兵。**不是** ``None``：``None`` 是「WSGI environ
#: 里没有这个键」这个**事实**，而它是一条要被拒绝的请求。见 :meth:`OpsApplication.handle`。
_UNSET = object()

#: 观测模块的 contract_code ⇒ HTTP 状态。
#:
#: 观测模块自己没有 HTTP 层（它是纯函数模块），所以这张表是本层补的；评测模块
#: 自带 ``api.CONTRACT_STATUS``，直接复用，不重抄。
OBSERVABILITY_STATUS: dict[str, int] = {
    observability_errors.ContractError.contract_code: 500,
    observability_errors.AccessDenied.contract_code: 403,
    observability_errors.UnknownMetric.contract_code: 400,
    observability_errors.InputLimitExceeded.contract_code: 400,
    observability_errors.InvalidWindow.contract_code: 400,
    observability_errors.InvalidBucket.contract_code: 400,
    observability_errors.InvalidRequest.contract_code: 400,
    observability_errors.SourceUnavailable.contract_code: 503,
    observability_errors.ConfigRejected.contract_code: 500,
}

#: Atlas（Calls）的 contract_code ⇒ HTTP 状态。
#:
#: Atlas 与观测模块一样是纯函数模块、不带 HTTP 层，所以这张表是本层补的。
#: ``atlas.source_unavailable`` 与 ``atlas.unverified_source`` 都是 **503**：
#: 它们回答的都是「本部署没有一条可用的来源」，而不是「你的请求错了」。
#: 前者按**字符串**写，因为它由 Calls Lane #77 的新模块定义，本分支的树上还没有
#: 那个类 —— 一个不存在的类永远不会被抛出，而一个字符串键在它合入之后自然生效。
ATLAS_STATUS: dict[str, int] = {
    atlas_errors.ContractError.contract_code: 500,
    atlas_errors.AccessDenied.contract_code: 403,
    atlas_errors.InputLimitExceeded.contract_code: 400,
    atlas_errors.InvalidWindow.contract_code: 400,
    atlas_errors.InvalidBucket.contract_code: 400,
    atlas_errors.InvalidRequest.contract_code: 400,
    atlas_errors.UnverifiedSource.contract_code: 503,
    "atlas.source_unavailable": 503,
}

#: 评测的 contract_code ⇒ HTTP 状态，**BFF 面**那一份。
#:
#: 评测模块自己的 HTTP 面（``api.CONTRACT_STATUS``）把 ``NotConfigured`` 讲成 501；
#: 浏览器面的契约（``PORTAL_BFF_API_CONTRACT.md`` §2.1）要求把「这条来源在这个部署里
#: 不存在」讲成 **503**。两张映射各有各的读者，本表是 BFF 的这一份：它以模块的映射
#: 为底，只覆盖浏览器面确实不同的那两条。``evaluation.report_download_refused`` 同样
#: 按字符串写（定义在 Evaluation Lane #78 的新模块里）。
EVALUATION_STATUS: dict[str, int] = {
    **evaluation_api.CONTRACT_STATUS,
    evaluation_errors.NotConfigured.contract_code: 503,
    "evaluation.report_download_refused": 413,
}

OPS_PATH = "/api/v2/ops"
LINUX_LOGIN_PATH = "/auth/linux/login"
LINUX_STATUS_PATH = "/auth/linux/status"
USAGE_PATH = "/api/v2/ops/usage"
ENGINES_PATH = "/api/v2/ops/engines"

#: 本轮接进来的同源只读路由。三条以前只有 module / preview，没有同源 BFF 出口；
#: 每一条都以 503 收场，当它自己的来源没有装配时 —— 见各自 handler 的说明。
INFRA_SUMMARY_PATH = "/api/infra/summary"
SYSTEM_RESOURCES_PATH = "/api/infra/system"
MODEL_SERVICE_KEY_PATH = "/api/model-service/key"
ENGINE_LIVE_PATH = "/api/v2/engine/live"
ATLAS_CALLS_PATH = "/api/atlas/calls"
EVAL_REPORT_SOURCES_PATH = "/api/evaluation/report-sources"
EVAL_REPORTS_PATH = "/api/evaluation/reports"
CAPABILITIES_PATH = "/api/portal/capabilities"
SERVICES_PATH = "/api/portal/services"
EVENTS_PATH = "/api/portal/events"
INVOKE_PATH_PREFIX = "/api/v2/ops/endpoints/"

#: 由**本层**拥有整套字段策略的路由：请求里出现 ``uid`` / ``role`` / ``owner`` /
#: ``principal`` / ``is_admin`` 之类的**字段名**（查询串或 JSON 体）即 400。
#:
#: 为什么是一张**声明表**而不是每个处理函数自己记得检查：``role`` 在某些载荷里是
#: 合法内容（聊天消息的发言人角色），所以不能做全局中间件；但「这条路由有没有这道
#: 门」必须是一个能读、能测的集合，否则它就是每个新处理函数作者的记性。
#:
#: ``backend.auth`` 的 write preflight 也做同一件事，但它**故意不覆盖 BFF-only 路由**
#: （IAM #100 的 ``IDENTITY_GUARDED_PATHS`` 注释写明这一点，理由是本层拥有自己的字段
#: 策略）。所以 ``/auth/admin/*`` 与 ``/auth/linux/status`` 的门只能在这里。
#: ``/auth/linux/login`` 一并列出：它在 IAM 的集合里，但本分支早于 #100 合入，
#: 提前上锁让「谁先合」不决定这条路径安不安全；两道门抛的是同一个 400 与同一个 token。
IDENTITY_FIELD_GUARDED = frozenset(
    {
        "auth.linux.login",
        "auth.linux.status",
        "auth.admin.status",
        "auth.admin.enter",
        "auth.admin.exit",
    }
)

#: 共享管理员模式。命名沿用既有的 ``/auth/*`` 命名空间，三条都是本层自己的
#: 路由（``/auth/`` 的兜底会把其余路径原样交给既有 auth 应用）。
ADMIN_ENTER_PATH = "/auth/admin/enter"
ADMIN_EXIT_PATH = "/auth/admin/exit"
ADMIN_STATUS_PATH = "/auth/admin/status"
LOGOUT_PATH = "/auth/logout"


class OpsApplication:
    """把 auth / auth_linux / registry 三块拼成前端要的那一个面。"""

    def __init__(
        self,
        *,
        auth_app,
        service,
        accounts,
        linux_service=None,
        token_bindings: Iterable[object] = (),
        observability=None,
        evaluation=None,
        atlas=None,
        host_summary=None,
        system_resources=None,
        engine_live=None,
        model_service_keys=None,
        ops_config=None,
        links: list[dict] | None = None,
        clock: Callable[[], float] | None = None,
        admin_mode=None,
        authority_policy=None,
        token_source=None,
        integrations=None,
        registry_api=None,
        gateway=None,
        gateway_app=None,
        param_store=None,
        param_fetch=None,
        integration_runtime=None,
        benchmarks=None,
    ) -> None:
        self.auth_app = auth_app
        self.cfg = auth_app.cfg
        self.service = service
        self.accounts = accounts
        self.linux_service = linux_service
        self.token_bindings = tuple(token_bindings)
        self.observability = observability
        self.evaluation = evaluation
        #: Calls/Atlas 来源。``None`` = 本部署没有接上它：``/api/atlas/calls``
        #: 以 503 收场，而不是回一份空列表——「没接」与「没调用」是两件事。
        self.atlas = atlas
        #: 主机投影来源。同样 ``None`` = 没接上，``/api/infra/summary`` 以 503
        #: 收场，而不是回一份全 UNKNOWN 的 200。
        self.host_summary = host_summary
        #: 系统资源读取器（CPU/内存/交换区/磁盘/网络/按用户）。同样
        #: ``None`` = 没接上，路由以 503 收场。
        self.system_resources = system_resources
        #: 引擎运行时读数的取数器。``None`` = 用模块默认实现（自己读环回
        #: ``/metrics``）；测试与进程内调用可以塞一个假的进来。
        self.engine_live = engine_live
        #: 模型服务密钥的来源（读清单 + 签发）。与其它来源不同，它下发的
        #: 是**凭据本身**，所以读写都只对平台管理员开放。
        self.model_service_keys = model_service_keys
        self.ops_config = ops_config or bff_ops.OpsConfig()
        self.links = list(links or _default_links())
        self._sessions = auth_app.service.sessions
        self.handoff = PortalSessionHandoff(store=auth_app.service.db, sessions=self._sessions)
        self.clock = clock
        #: 共享管理员模式。``None`` = 本部署没有这个功能：没有人能提权，
        #: ``resolve_session`` 也就一律只给出成员主体。
        self.admin_mode = admin_mode
        #: ``Host`` 权威策略。默认**只认环回**：没被显式配置过的部署不会因为
        #: 「反正没人配」而接受任何外部主机名。反代后面用真实主机名属于部署决策，
        #: 由运维显式传进来。
        self.authority_policy = authority_policy or bff_origin.AuthorityPolicy()
        #: 插件跳转地址的存储。``None`` = 本部署没接：两条路由以 503 收场。
        self.integrations = integrations
        self.integration_runtime = integration_runtime
        self.benchmarks = benchmarks
        #: Registry 的 token API（CLI / Agent 用）。挂上之后，带 ``Authorization``
        #: 的 ``/api/v2/registry/*`` 请求原样交给它；浏览器（cookie）请求仍走本层的
        #: 会话路由。``None`` = 本进程不提供 CLI 入口。
        self.registry_api = registry_api
        #: 集成网关：``gateway`` 是管理面（``backend.gateway.service.GatewayService``），
        #: ``gateway_app`` 是同源 ``/v1/*`` 的调用入口。``None`` = 本部署没有网关。
        self.gateway = gateway
        self.gateway_app = gateway_app
        #: 推理服务目标参数（``backend.runtime.params.ParamStore``）与只读实测函数。
        self.param_store = param_store
        self.param_fetch = param_fetch
        #: scoped CLI token 通道。``None`` 时建一条**关闭**的：它仍然负责带
        #: ``Authorization`` 头的请求，只是对它们一律答 401。token 的签发/撤销/审计
        #: 归 IAM Lane；在它落地之前，把通道开着就是一条没有审计的旁路。
        #:
        #: 关闭态下 ``introspector`` 不会被调用一次（``resolve`` 在检查 ``enabled``
        #: 之后就返回了），所以这里挂的是本仓库既有的 Registry 凭据目录适配器：
        #: 它是契约测试与将来装配的落点，不是一个被启用的签发者。
        self.token_source = token_source or bff_identity.ScopedTokenPrincipalSource(
            introspector=bff_identity.RegistryTokenIntrospector(
                getattr(service, "tokens", None), getattr(service, "principals", None)
            ),
            accounts=accounts,
            enabled=False,
        )
        self.session_source = bff_identity.SessionPrincipalSource(
            self.resolve_session, cookie_name=self.cfg.cookie_name
        )

    # --- WSGI ---------------------------------------------------------------

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> Iterable[bytes]:
        if self._is_registry_token_request(environ):
            return self._serve_registry_api(environ, start_response)
        if self.gateway_app is not None and (environ.get("PATH_INFO") or "").startswith("/v1/"):
            # 同源的模型调用入口：先过同一道 Host 权威守卫，再交给网关（它只认 API 密钥，不看会话）。
            refusal = self.check_authority(
                host_header=environ.get("HTTP_HOST"), server_port=environ.get("SERVER_PORT")
            )
            if refusal is not None:
                start_response(
                    f"{refusal.status} {REASON_PHRASES.get(refusal.status, DEFAULT_REASON)}",
                    list(refusal.headers),
                )
                return [refusal.body]
            return self.gateway_app(environ, start_response)
        request = build_request(environ, max_body=self.cfg.max_request_bytes)
        response = self.handle(
            request,
            host_header=environ.get("HTTP_HOST"),
            server_port=environ.get("SERVER_PORT"),
        )
        reason = REASON_PHRASES.get(response.status, DEFAULT_REASON)
        start_response(f"{response.status} {reason}", list(response.headers))
        return [response.body]

    def _is_registry_token_request(self, environ) -> bool:
        path = environ.get("PATH_INFO") or ""
        return (
            self.registry_api is not None
            and path.startswith(REGISTRY_API_PREFIX)
            and bool(environ.get("HTTP_AUTHORIZATION"))
        )

    def _serve_registry_api(self, environ, start_response):
        """CLI / Agent 通道：``Bearer`` 令牌 → Registry 自己的 API（自己判身份）。

        同一个 ``Host`` 权威守卫先跑；同时带会话 cookie 的请求按凭据混淆拒绝——
        任何一侧优先都是一次隐式降级（见 :meth:`_invoke_preflight`）。
        """
        refusal = self.check_authority(
            host_header=environ.get("HTTP_HOST"), server_port=environ.get("SERVER_PORT")
        )
        if refusal is None and self.cfg.cookie_name in _cookie_names(environ.get("HTTP_COOKIE")):
            refusal = _error_response(
                400,
                bff_identity.REASON_CREDENTIAL_CONFUSION,
                "send either a session cookie or an Authorization header, not both",
            )
        if refusal is not None:
            start_response(
                f"{refusal.status} {REASON_PHRASES.get(refusal.status, DEFAULT_REASON)}",
                list(refusal.headers),
            )
            return [refusal.body]
        return self.registry_api(environ, start_response)

    def check_authority(self, *, host_header: object, server_port: object):
        """``Host`` 权威守卫。不通过就给出 421，且**在任何 cookie/CSRF 之前**。

        它比身份更靠前是刻意的：一个不是发给我们的请求，不该被我们的会话表
        看上一眼，也不该有机会走到任何一条会写东西的路径。返回值是 WSGI 响应或
        ``None``。
        """
        refusal = self.authority_policy.check(host_header, server_port=server_port)
        if refusal is None:
            return None
        return _error_response(refusal.status, refusal.reason, refusal.detail)

    # --- 路由 ---------------------------------------------------------------

    def handle(
        self,
        request,
        *,
        host_header: object = _UNSET,
        server_port: object = _UNSET,
    ) -> Any:
        """一个请求 ⇒ 一个响应。异常在这里收口，不外泄内部细节。

        ``host_header`` / ``server_port`` 由 :meth:`__call__` 从 WSGI environ 带进来。
        **省略**它们（``_UNSET``）才跳过权威校验——那是测试与进程内调用才有的用法，
        线上路径永远是 :meth:`__call__`。注意「省略」与「传了 ``None``」是两回事：
        后者意味着 environ 里真的没有那个键，那是**一条要拒绝的请求**
        （``missing_host``），不是一次要跳过的检查。用「参数不是 None 才检查」来
        区分两者，会让一个既没有 ``Host`` 也没有 ``SERVER_PORT`` 的请求直接绕过整道
        守卫——这正是本方法要避免的那种 fail-open。
        """
        if host_header is not _UNSET or server_port is not _UNSET:
            refusal = self.check_authority(
                host_header=host_header, server_port=server_port
            )
            if refusal is not None:
                return refusal

        path = request.path
        try:
            return self._dispatch(request, path)
        except bff_identity.IdentityError as exc:
            return _error_response(exc.status, exc.reason, exc.detail or exc.reason)
        except registry_errors.RegistryError as exc:
            status = registry_api.CONTRACT_STATUS.get(exc.contract_code, 500)
            return _error_response(status, exc.contract_code, str(exc))
        except observability_errors.ContractError as exc:
            status = OBSERVABILITY_STATUS.get(exc.contract_code, 500)
            return _error_response(status, exc.contract_code, str(exc))
        except atlas_errors.ContractError as exc:
            status = ATLAS_STATUS.get(exc.contract_code, 500)
            # ``SourceUnavailable`` 带一个更具体的 ``reason``（``source.file_missing``
            # / ``source.outside_root`` / ``source.too_large`` ……）。它是稳定码、
            # 不含内容，所以直接当错误 token 下发：运维据此知道该去查哪一道。
            code = getattr(exc, "reason", None) or exc.contract_code
            return _error_response(status, str(code), str(exc) or str(code))
        except evaluation_errors.EvaluationError as exc:
            status = EVALUATION_STATUS.get(exc.contract_code, 500)
            return _error_response(status, exc.contract_code, str(exc))
        except linux_errors.LinuxAuthError as exc:
            return _error_response(exc.http_status, exc.code, exc.message)
        except bff_admin_mode.AdminModeError as exc:
            headers = []
            retry_after = getattr(exc, "retry_after", None)
            if retry_after:
                headers.append(("Retry-After", str(int(retry_after))))
            return _error_response(exc.status, exc.code, exc.message, headers=headers)
        except auth_errors.AuthError as exc:
            return _auth_error_response(exc)
        except Exception:  # noqa: BLE001 - 内部细节不外泄
            return _error_response(500, "bff.internal_error", "The request could not be served.")

    def _dispatch(self, request, path: str):
        """路由表驱动：表里没有的路径不会被服务。

        这一段的形状本身就是 fail-closed 的：**先**在表里查到声明，才可能走到任何
        实现；查不到就只剩「交给既有 auth 应用」与 404 两种结局。没有一条
        「因为没匹配上某个 if，于是掉进某个默认分支」的路。
        """
        spec = bff_route_map.match(path)
        if spec is not None:
            if spec.handler is None:
                # 这条路径由既有 auth 应用自己实现（``/auth/logout``）；方法不匹配
                # 也交给它——它才是那条路径的唯一实现者，包括它自己的 405。
                return self._delegated(request, spec)
            if request.method not in spec.methods:
                return _error_response(405, "method_not_allowed", "Method not allowed.")
            self._guard_route(request, spec)
            if spec.pattern is not None:
                # 带变量的路径：把捕获到的那一段交给实现。``match`` 已经验过形状，
                # 所以这里拿到的不会是 ``None``；实现自己决定它合不合法。
                return getattr(self, spec.handler)(request, _path_capture(path, spec.pattern))
            return getattr(self, spec.handler)(request)

        if bff_route_map.is_delegated(path):
            # ``/auth/`` 下的其余路径原样交给既有应用。
            return self.auth_app.handle(request)

        return _error_response(404, "not_found", f"no route for {request.method} {path}")

    def _delegated(self, request, spec):
        """交给既有 auth 应用执行的路径。

        登出必须先由既有应用真正执行，**成功后**才清提权：反过来的话，一次因 CSRF
        失败的登出会顺手把用户降成 member，而人还以为自己已经登出了。降权方向是
        安全的，但「登出失败却掉了管理员模式」是个会让人困惑的副作用，不值得。
        """
        response = self.auth_app.handle(request)
        if spec.name == "auth.logout" and response.status == 200:
            self._forget_elevation(request)
        return response

    def _guard_route(self, request, spec: bff_route_map.RouteSpec) -> None:
        """按路由表在**调用实现之前**施加的通用守卫。

        两条都在实现之外再做一次（实现自己也会做），不是重复劳动：

        * 实现里那一次让处理函数**被直接调用时**也是安全的；
        * 这里这一次让「每条路由都过同一道门」成为**表**的性质，而不是每个处理
          函数各自的记性——新加一条路由忘了写守卫，在表这一层就会被发现。

        抛出的 :class:`~backend.bff.identity.IdentityError` 由 :meth:`handle` 统一
        翻成 400/401/403，与身份解析的失败形状一致。
        """
        self._reject_asserted_identity(request)
        if spec.name in IDENTITY_FIELD_GUARDED:
            # 字段级：查询串**与** JSON 体都看。这道门在任何处理器之前，也在
            # CSRF/同源之前 —— 一个自报身份的请求不该先被当成一次合法的写操作
            # 去校验 Origin，它根本不该被当成一次写操作。
            bff_identity.reject_identity_field_assertions(
                query_keys=request.query.keys(),
                body_fields=_body_field_names(request),
            )
        if spec.is_data_route:
            # 数据来源由服务端装配决定；客户端不能挑。前端页面 URL 上的
            # ``?source=`` 是**页面**的取数模式，不是这些 API 的参数。
            bff_identity.reject_source_assertions(
                request.headers.keys(), request.query.keys()
            )
            # 与上一条并列的另一半：来源说的是「数据是谁给的」，这一条拒绝的是
            # 「去哪个地址取」。BFF 的每条数据路由目标都在装配里，一个能收
            # ``?url=`` / ``?promql=`` / ``?path=`` 的处理器就是一跳内网跳板。
            bff_identity.reject_proxy_assertions(request.query.keys())

    # --- GET /api/v2/ops ----------------------------------------------------

    def _ops(self, request):
        self._reject_asserted_identity(request)
        principal = self._principal(request)

        visibility = request.query_one("visibility") or bff_ops.VISIBILITY_PRIVATE_PORTAL
        if visibility not in bff_ops.VISIBILITIES:
            return _error_response(
                400, "bff.invalid_request", f"unknown visibility: {visibility}"
            )

        payload = bff_ops.build_ops(
            principal=principal,
            service=self.service,
            visibility=visibility,
            token_bindings=self.token_bindings,
            observability=self.observability,
            evaluation=self.evaluation,
            role_lookup=self._role_lookup,
            links=self.links,
            config=self.ops_config,
        )
        return _json_response(200, {"data": {"ops": payload}})

    # --- GET /api/v2/ops/usage, /engines ------------------------------------

    def _usage(self, request):
        """完整的用量 DTO（观测模块自己的形状），同源、按会话鉴权。

        指标卡槽位放不下它：``portal-ops/1`` 的 ``metrics.items[]`` 只接受白名单
        来源的**数字**，而本部署的观测数据是合成的。所以完整的用量（计数器、
        分桶、质量、未知分类）走这条路由，而卡片保持 UNKNOWN。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if not hasattr(self.observability, "usage_dto"):
            return _error_response(
                503,
                "bff.observability_adapter_not_attached",
                "the observability adapter is not attached",
            )
        scope = request.query_one("scope") or "self"
        if scope not in ("self", "global"):
            return _error_response(400, "bff.invalid_request", f"unknown scope: {scope}")
        dto = self.observability.usage_dto(identity=principal, scope=scope)
        return _json_response(200, {"data": dto})

    def _engines(self, request):
        """引擎状态 DTO。同样同源、按会话鉴权。

        V6 现有面板没有引擎状态槽位，因此这条路由是**接口层**的接线：数据可得、
        受权限约束、格式就是观测模块自己的 DTO；把它画出来属于 UI 变更，不在本
        阶段范围内。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if not hasattr(self.observability, "engine_status_dto"):
            return _error_response(
                503,
                "bff.observability_adapter_not_attached",
                "the observability adapter is not attached",
            )
        dto = self.observability.engine_status_dto(identity=principal)
        return _json_response(200, {"data": dto})

    # --- POST /api/v2/ops/endpoints/{id}/invoke -----------------------------

    def _invoke_endpoint(self, request, endpoint_id: str):
        """同源调用入口（SEAM-1）。

        这是**最薄**的一层：授权不是这里判的。``RegistryService.invoke`` 自己依次做
        invoke scope、端点可见性（不可见即 404，与不存在同形）、端点就绪、后端两根轴
        （所有权/协议，按部署策略）、载荷必须是有界 JSON 对象、**审计先于转发**，最后
        把调用交给 Gateway adapter —— 而 adapter 自己再按调用者的 key 判一次 ACL。
        本层只负责把受信主体交过去，并把答复收窄成可安全下发的形状。

        身份只来自凭据：会话 cookie 或 scoped token，由 ``resolve_credential`` 决定
        走哪条通道。**没有任何一个参数**能指定 uid/owner/key/上游地址 —— 那些名字在
        到达这里之前就被 400 拒绝（``reject_proxy_assertions``），而 endpoint 只出现
        在路径里，由 Registry 的行级 ACL 判可见性。

        未接线（detached adapter）⇒ Registry 抛 ``upstream_unavailable`` ⇒ **503**，
        不是一份伪造的成功。
        """
        self._reject_asserted_identity(request)
        self._invoke_preflight(request)
        principal = self._principal(request)

        payload = _json_body(request)
        if payload is None:
            return _error_response(
                400, "bff.invalid_request", "a JSON object body is required"
            )

        envelope = self.service.invoke(principal.requester, endpoint_id, payload)

        # Router 同步状态作为**来源事实**附上。它是 Registry 的行级视图，不是本层的
        # 判断：``applied`` 只由 Gateway 回读（revision + snapshot SHA）产生。
        # 读不到就不写 —— 一个猜出来的 ``applied`` 比没有更糟。
        router_sync = None
        try:
            endpoint = self.service.policy(principal.requester).require_endpoint_read(endpoint_id)
            router_sync = endpoint.router_sync_block()
        except registry_errors.RegistryError:  # pragma: no cover - 刚刚才调用过它
            router_sync = None

        return _json_response(
            200, {"data": bff_invoke.build_invoke_dto(envelope, router_sync=router_sync)}
        )

    # --- GET /api/infra/summary ---------------------------------------------

    def _infra_summary(self, request):
        """固定来源的主机投影，**在服务端判过**才下发。

        三条以前没有同源出口的路由之一。它的来源不是请求里的地址：适配器背后
        是一个零参数的读取器（见 ``host_summary`` 模块），所以这条处理器结构上
        没有一根线可以把 ``?url=`` 接到一个上游。

        未装配 ⇒ 503；装配了但这次没读到 ⇒ 503；读到了 ⇒ 交给
        ``validate_host_summary`` 判，``accepted`` 只说明信封可信，每个指标还要
        各自过样本时间与单位。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if self.host_summary is None:
            return _error_response(
                503,
                "bff.host_summary_adapter_not_attached",
                "the host summary adapter is not attached",
            )
        dto = self.host_summary.summary(identity=principal)
        return _json_response(200, {"data": dto})

    # --- GET /api/v2/engine/live --------------------------------------------

    def _service_metrics(self, request, engine_id: str):
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        engine = self.service.policy(principal.requester).require_engine_read(engine_id)
        from .service_metrics import read
        key = self.gateway.core.upstream_key(engine) if self.gateway else None
        dto = read(engine, key=key)
        dto["engine_id"] = engine.id
        return _json_response(200, {"data": dto})

    def _engine_live(self, request):
        """引擎自己的运行时读数，由**本层**拿着那把 key 去取。

        为什么不把这个地址直接交给浏览器：Strata 的 ``/metrics`` 要
        ``Authorization: Bearer``，而它自带控制台要求人手填一把 key。让人把凭据
        填进浏览器，等于把它留在历史、扩展和截屏里 —— 而一把能读引擎指标的 key
        通常也能调用引擎。所以取数留在服务端，前端只拿到已经投影好的槽位、排队
        与吞吐；响应体里没有、也不该有 key。

        引擎读不到时**不是** 503，而是 200 + ``reachable: false``：
        「引擎没起来」与「平台还没配凭据」都是这幅图要显示的东西，把它们讲成
        「这条接口不存在」只会让人去查错地方。此时所有数值字段都是 ``None``。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if self.service.config.personal_mode:
            return _error_response(400, "bff.service_required", "select a service to read its metrics")
        reader = self.engine_live or bff_engine_live.read_engine_live
        try:
            dto = reader()
        except Exception as exc:  # noqa: BLE001 - 读不到就是读不到
            return _error_response(
                503,
                "bff.engine_live_unavailable",
                "the engine live reader failed: " + type(exc).__name__,
            )
        return _json_response(200, {"data": dto})

    # --- GET /api/model-service/key -----------------------------------------

    def _model_service_admin(self, request):
        """密钥两条路由共用的门：只认会话里的平台管理员。

        ``is_platform_admin`` 只看 ``elevated``，不看 ``role`` —— 这里不重复
        判断角色，免得写出一个「看起来更严、实际更松」的条件。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if self.service.config.personal_mode:
            return None, _error_response(403, "bff.owner_managed_credentials", "upstream credentials are managed by the service owner")
        if not bff_scopes.is_platform_admin(elevated=principal.elevated):
            return None, _error_response(
                403, "bff.access_denied", "a platform administrator is required"
            )
        if self.model_service_keys is None:
            return None, _error_response(
                503,
                "bff.model_service_key_not_attached",
                "the model service key store is not attached",
            )
        return principal, None

    def _model_service_key(self, request):
        """调用本机推理引擎用的密钥（完整明文，整份清单）。

        这是本层**唯一**一条把凭据原样下发给前端的路由。产品要求管理台能点开
        看完整密钥 —— 运维要把它填进客户端，一个只剩前六位的掩码帮不上忙。
        代价因此写在这里：这条路由必须比其它路由更窄，只对平台管理员开放。

        没有密钥时回 200 + 空清单，而不是 404：「这一页能打开、只是还没发过」
        和「这条路不存在」对使用者是两件事。
        """
        principal, error = self._model_service_admin(request)
        if error is not None:
            return error
        try:
            keys = self.model_service_keys.list()
        except Exception as exc:  # noqa: BLE001 - 读不到就是读不到
            return _error_response(
                503,
                "bff.model_service_key_unavailable",
                "the model service key store failed: " + type(exc).__name__,
            )
        primary = keys[0] if keys else {"key": None}
        return _json_response(200, {"data": {**primary, "keys": keys}})

    def _model_service_key_create(self, request):
        """签发一把新密钥。

        响应里带着 ``restart``：密钥写进引擎的环境文件之后引擎必须重载才会
        认它。把「已签发」和「已生效」混成一句话，是这一页最容易撒的谎。
        """
        # 这是一次写入，而且写的是凭据：CSRF/同源必须在**签发之前**过。路由表里的
        # ``csrf=True`` 只是声明，真正的校验在每条写路由自己这里调用。
        self._write_preflight(request)
        principal, error = self._model_service_admin(request)
        if error is not None:
            return error
        body = _json_body(request)
        if body is None:
            return _error_response(
                400, "bff.invalid_request", "a JSON object body is required"
            )
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            return _error_response(400, "bff.invalid_request", "label must be a string")
        try:
            record, restart = self.model_service_keys.mint(label)
        except Exception as exc:  # noqa: BLE001 - 写不进去就是写不进去
            return _error_response(
                503,
                "bff.model_service_key_unwritable",
                "the model service key store failed: " + type(exc).__name__,
            )
        return _json_response(201, {"data": {**record, "restart": restart}})

    # --- GET /api/infra/system ----------------------------------------------

    def _system_resources(self, request):
        """本机的 CPU / 内存 / 交换区 / 磁盘读写 / 网络带宽 / 按用户。

        与 ``/api/infra/summary`` 走**同一道门**（``observability:host:read``）：
        两份都是整机共享量，不该有一条比另一条更宽的路。差别只在来源 ——
        summary 是 Prometheus/NVML 的指标投影，这里是 BFF 直读 ``/proc``
        （这台机没有 node_exporter，装一个要 root）。

        未装配 ⇒ 503；读失败 ⇒ 503；读到 ⇒ 200。**没有**「读不到就给一份
        全 0」的降级：0 B/s 是一个测量结果，不能拿来替「没量到」。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        granted = bff_scopes.observability_scopes_for(getattr(principal, "effective_role", None))
        if observability_identity.SCOPE_HOST_READ not in granted:
            return _error_response(
                403,
                "bff.access_denied",
                "missing required scope " + observability_identity.SCOPE_HOST_READ,
            )
        if self.system_resources is None:
            return _error_response(
                503,
                "bff.system_resources_not_attached",
                "the system resource reader is not attached",
            )
        try:
            data = self.system_resources()
        except Exception as exc:  # noqa: BLE001 - 读不到就是读不到，不伪造
            return _error_response(
                503,
                "bff.system_resources_unavailable",
                "the system resource reader failed: " + type(exc).__name__,
            )
        return _json_response(200, {"data": data})

    # --- GET /api/atlas/calls -----------------------------------------------

    def _atlas_calls(self, request):
        """**本人**的逐条调用元数据。

        身份只从会话来：``?key=`` 之类的「看哪把 key 的记录」在这里**没有**参数
        —— Atlas 的 ``resolve_own_call_scope`` 会把「别人的 key / 不存在的 key /
        我自己的已吊销 key」塌成同一个拒绝，正是为了不做成枚举他人 key 的预言机，
        本层若允许先自报一个再判，那层防护就被绕过了。

        与 ``/api/v2/ops/usage`` 同为「未接线时 503」：一份空列表是**有意义的
        测量结果**（这个窗口里你确实没有调用），不能拿它替「本部署没有这条来源」。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if self.atlas is None or not getattr(self.atlas, "attached", False):
            return _error_response(
                503,
                bff_atlas.REASON_ATLAS_NOT_ATTACHED,
                "the atlas (calls) adapter is not attached",
            )

        limit = request.query_one("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return _error_response(400, "bff.invalid_request", "limit must be an integer")

        before_ts = request.query_one("before_ts")
        if before_ts is not None:
            before_ts = _parse_instant(before_ts)
            if before_ts is None:
                return _error_response(
                    400, "bff.invalid_request", "before_ts must be an ISO-8601 instant with an offset"
                )

        before_call_id = request.query_one("before_call_id")
        if (before_ts is None) != (before_call_id is None):
            # 游标要么都给，要么都不给 —— 与 Atlas 自己的口径一致，先在这里说清楚，
            # 免得半截游标被下游当成一次「从头开始」。
            return _error_response(
                400, "bff.invalid_request", "before_ts and before_call_id must be given together"
            )

        dto = self.atlas.self_calls(
            identity=principal,
            outcome=request.query_one("outcome") or "all",
            model=request.query_one("model") or None,
            limit=limit,
            before_ts=before_ts,
            before_call_id=before_call_id,
        )
        return _json_response(200, {"data": dto})

    # --- GET /api/evaluation/* ----------------------------------------------

    def _eval_adapter(self):
        """评测适配器，未接线上时返回 ``None``。三条报告路由共用一个判定。"""
        if self.evaluation is None or not getattr(self.evaluation, "attached", False):
            return None
        return self.evaluation

    def _eval_unattached(self):
        return _error_response(
            503,
            bff_ops.REASON_EVALUATION_NOT_READY,
            "the evaluation adapter is not attached",
        )

    def _eval_report_sources(self, request):
        """两条报告来源各自的可用状态。

        ``source``（``evalscope`` vs ``legacy-benchmark``）由评测模块盖章，本层
        **不做第二次映射** —— 那正是「旧 Benchmark run 被当成 EvalScope 报告」
        得以发生的地方。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        adapter = self._eval_adapter()
        if adapter is None:
            return self._eval_unattached()
        if not hasattr(adapter, "report_sources"):
            return self._eval_unattached()
        return _json_response(200, {"data": adapter.report_sources(identity=principal)})

    def _eval_reports(self, request):
        """报告目录。未知归属的记录由评测模块自己隐藏，本层不设第二道可见性。"""
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        adapter = self._eval_adapter()
        if adapter is None or not hasattr(adapter, "reports"):
            return self._eval_unattached()
        # ``include_unowned`` 只是**转述**：真正的放行在评测模块的
        # ``authz.report_visibility``，那里要求 ``evaluation:admin:manage``。
        # 所以一个成员即便把它设成 true 也看不到无归属记录 —— 判定没有两份。
        include_unowned = _truthy(request.query_one("include_unowned"))
        return _json_response(
            200,
            {"data": adapter.reports(identity=principal, include_unowned=include_unowned)},
        )

    def _eval_report_detail(self, request, report_ref: str):
        """单份报告。未授权与不存在由评测模块塌成同一个答复。"""
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        adapter = self._eval_adapter()
        if adapter is None or not hasattr(adapter, "report_detail"):
            return self._eval_unattached()
        include_unowned = _truthy(request.query_one("include_unowned"))
        return _json_response(
            200,
            {
                "data": adapter.report_detail(
                    identity=principal,
                    report_ref=report_ref,
                    include_unowned=include_unowned,
                )
            },
        )

    def _eval_report_download(self, request, report_ref: str):
        """**受限下载的判定**，不是字节。

        这是评测 Lane #78 / PR #91 交付的那第四条 seam，由本 Lane 挂载。三件事必须
        说清楚，因为它们是这条路由存在的全部理由：

        1. **它不返回文件。** 响应体是一份**计划**：这份报告你有没有资格发起读取、
           此刻能不能真的读出来、上界是多少、为什么不能。字节只在受限读取器**显式
           打开**且有界检查通过之后才由模块产出——本部署两者都不成立，所以正确的
           回答是 ``content_read: false`` + 一个原因，**不是**一个 0 字节的成功下载。
        2. **路径不是参数。** ``report_ref`` 是路径里一个不透明引用（``evrp-…``）；
           相对路径只作为内部字段存在、不进 DTO（下发相对路径 = 下发产物布局）。
           遍历防护、绝对路径拒绝、后缀白名单全在模块内部判定，本层不重写一份。
        3. **超界拒绝而不截断。** 一份超过上界的报告返回 413，且不报一个字节数——
           「读不到」时那个数就是未知。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        adapter = self._eval_adapter()
        if adapter is None or not getattr(adapter, "supports_download", False):
            # 评测 Lane 还没把这个方法交付到这个分支的树上：本部署**没有这条功能**，
            # 那是 503，不是 500（服务崩了），更不是 200 的空下载。
            return self._eval_unattached()
        include_unowned = _truthy(request.query_one("include_unowned"))
        return _json_response(
            200,
            {
                "data": adapter.report_download(
                    identity=principal,
                    report_ref=report_ref,
                    include_unowned=include_unowned,
                )
            },
        )

    # --- GET /api/portal/services -------------------------------------------

    def _portal_services(self, request):
        """固定服务列表与它们的就绪证据。

        它是 ``/api/infra/summary`` 那份投影的**一个切面**，不是第二个来源：同一份
        已判定的 verdict，这里只取出 ``services`` 段。所以它走同一道门
        （``observability:host:read``）、同一个未装配结局（503）——
        「服务健康」不该有一条比主机读数更宽或更窄的旁路。

        判定器只上报投影**真的报了**的那几个服务（``prometheus`` / ``gateway`` /
        ``grafana`` / ``evalscope`` / ``strata``）。缺的那几个在这里是**不出现**，
        而不是出现成 ``up: false`` —— 「这份投影没有报它」与「它挂了」是两件事，
        混起来会让一次投影端的改版看起来像一次全机故障。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if self.host_summary is None:
            return _error_response(
                503,
                "bff.host_summary_adapter_not_attached",
                "the host summary adapter is not attached",
            )
        verdict = self.host_summary.verdict(identity=principal)
        return _json_response(
            200,
            {
                "data": {
                    "schema_version": bff_host_summary.SCHEMA_VERSION,
                    "route": INFRA_SUMMARY_PATH,
                    "source": verdict.source,
                    "accepted": verdict.accepted,
                    "reason": verdict.reason,
                    "observed_at": verdict.observed_at,
                    "services": {
                        key: item.to_dict() for key, item in verdict.services.items()
                    },
                }
            },
        )

    # --- GET /api/portal/events ---------------------------------------------

    def _portal_events(self, request):
        """操作员范围的有界审计时间线（V1-11 的 logs/audit 面）。

        三条边界，每一条都由**结构**而不是措辞保证：

        * **范围固定。** 要求 ``portal:admin:read``；成员得到 403。跨主体审计是这个
          范围唯一的用途，因此这里不提供「顺便看看自己那份」的第二条路径 ——
          多一条路径就多一次「谁能看见谁」的论证。
        * **有界。** ``limit`` 只接受 1..``MAX_LIMIT``；超界是 400，不是静默截断。
          没有时间范围扫描、没有游标遍历。
        * **脱敏。** 白名单字段之外的一切（含两个账本各自的 ``detail`` 列）在
          ``backend.bff.events`` 里就被丢掉，根本不会到这里。

        两条来源都没接上时以 **503** 收场，而不是回一份空时间线：「没接线」与
        「账本里确实一条都没有」（``empty``）是两件不同的事。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if bff_scopes.PORTAL_ADMIN_READ not in principal.portal_scopes:
            return _error_response(
                403,
                # 复用仓库既有的 reason token（``bff.ops.REASON_MISSING_SCOPE``），
                # 不为一条新路由发明一个新的 403 词。
                bff_ops.REASON_MISSING_SCOPE,
                f"missing required scope {bff_scopes.PORTAL_ADMIN_READ}",
            )

        raw_limit = request.query_one("limit")
        if raw_limit is None:
            limit = bff_events.DEFAULT_LIMIT
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return _error_response(400, "bff.invalid_request", "limit must be an integer")
            if not 1 <= limit <= bff_events.MAX_LIMIT:
                return _error_response(
                    400,
                    "bff.invalid_request",
                    f"limit must be between 1 and {bff_events.MAX_LIMIT}",
                )

        dto = bff_events.build_events_dto(
            admin_mode=self.admin_mode,
            service=self.service,
            requester=principal.requester,
            limit=limit,
            now=self._now(),
        )
        if bff_events.all_sources_unconfigured(dto):
            return _error_response(
                503,
                "portal.events_not_configured",
                "no audit source is configured on this deployment",
            )
        return _json_response(200, {"data": dto})

    # --- GET /api/portal/capabilities ---------------------------------------

    def _capabilities(self, request):
        """装配状态清单：每条来源此刻对**这个会话**是 ready / stale / auth_required /
        not_configured。

        它回答的是前端一直只能靠猜的那个问题。在它之前，「这条来源没有数据」
        与「你没有权限看这条来源」在浏览器上长得一模一样 —— 一个把两者画成同一块
        空白的页面，会让运维去查一个根本不存在的故障。

        清单**不宣称任何一条来源是 LIVE**：``ready`` 说的是「装配在、权限在、
        最近一次样本落在声明的 TTL 内」，它既不是一次现场核验，也不是对 Dell
        那台机器的任何断言。``source_fact()`` 缺省给出 ``no_sample_yet``，
        所以一个接了线但没取过数的来源会如实报 ``stale``。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        now = self._now()
        granted = bff_scopes.session_scopes(elevated=principal.elevated)
        evidence = {
            seam.source: bff_capabilities.evidence_for(
                seam, getattr(self, seam.adapter_attribute, None), now=now
            )
            for seam in bff_capabilities.SOURCE_SEAMS
        }
        payload = bff_capabilities.build_capabilities(
            subject=principal.principal,
            granted_scopes=frozenset(
                granted.portal | granted.observability | granted.evaluation
            ),
            evidence=evidence,
            now=now,
        )
        return _json_response(200, {"data": payload})

    def _now(self):
        """服务端当下。可注入，好让「过期」这件事在没有等待的测试里也能被验。"""
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)

    # --- PUT /api/v2/ops/deployments/{id}/share -----------------------------

    # --- /api/portal/integrations ------------------------------------------

    def _integration_store(self):
        if self.integrations is None:
            raise _IntegrationsNotAttached()
        return self.integrations

    def _integrations(self, request):
        """插件目录 + 跳转地址。对看不到某个地址的会话，那一项的 ``ui_url`` 是 ``null``。"""
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        try:
            store = self._integration_store()
        except _IntegrationsNotAttached:
            return _error_response(503, "bff.integrations_not_attached", "plugin settings are not attached")
        is_admin = bff_scopes.PORTAL_ADMIN_READ in principal.portal_scopes
        rows = store.listing(is_admin=is_admin)
        if self.integration_runtime:
            runtime = self.integration_runtime(principal)
            for row in rows:
                row["runtime"] = runtime.get(row["key"])
        return _json_response(200, {"data": {"integrations": rows}})

    def _benchmarks(self, request):
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        if self.benchmarks is None:
            return _error_response(503, "evaluation.not_configured", "real benchmark adapter is not configured")
        return _json_response(200, {"data": self.benchmarks.listing(principal)})

    def _benchmark_reconcile(self, request):
        return self._benchmark_submit(request, operation='reconcile')

    def _benchmark_stop(self, request):
        return self._benchmark_submit(request, operation='stop')

    def _benchmark_submit(self, request, operation='submit'):
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        if self.benchmarks is None:
            return _error_response(503, "evaluation.not_configured", "real benchmark adapter is not configured")
        body = _json_body(request)
        if body is None:
            return _error_response(400, "evaluation.invalid_request", "a JSON object body is required")
        try:
            action = {'submit': self.benchmarks.submit, 'reconcile': self.benchmarks.reconcile, 'stop': self.benchmarks.stop}[operation]
            result = action(principal, body)
        except bff_gateway_service.ManagementError as exc:
            return _error_response(exc.status, exc.code, exc.message)
        return _json_response(202 if operation == 'submit' else 200, {"data": {"run": result}})

    def _integration_update(self, request, key: str):
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        if bff_scopes.PORTAL_ADMIN_READ not in principal.portal_scopes:
            return _error_response(403, "bff.forbidden", "only administrators can change plugin settings")
        try:
            store = self._integration_store()
        except _IntegrationsNotAttached:
            return _error_response(503, "bff.integrations_not_attached", "plugin settings are not attached")
        body = _json_body(request)
        if body is None:
            return _error_response(400, "bff.invalid_request", "a JSON object body is required")
        try:
            store.update(key, ui_url=body.get("ui_url"), visibility=body.get("visibility"))
        except KeyError:
            return _error_response(404, "bff.not_found", f"unknown plugin {key!r}")
        except bff_integrations.InvalidIntegration as exc:
            return _error_response(400, "bff.invalid_request", str(exc))
        return _json_response(200, {"data": {"integrations": store.listing(is_admin=True)}})

    # --- /api/gateway/* ------------------------------------------------------

    def _gateway_call(self, request, fn, *, write: bool = False, body: bool = False):
        self._reject_asserted_identity(request)
        if write:
            self._write_preflight(request)
        principal = self._principal(request)
        if self.gateway is None:
            return _error_response(503, "bff.gateway_not_attached", "the gateway is not attached")
        payload = None
        if body:
            payload = _json_body(request)
            if payload is None:
                return _error_response(400, "bff.invalid_request", "a JSON object body is required")
        try:
            data = fn(principal, payload) if body else fn(principal)
        except bff_gateway_service.ManagementError as exc:
            return _error_response(exc.status, exc.code, exc.message)
        return _json_response(200, {"ok": True, "data": data})

    def _gateway_overview(self, request):
        return self._gateway_call(request, lambda p: self.gateway.overview(
            p, window=request.query_one("window"), view=request.query_one("view")))

    def _gateway_keys(self, request):
        return self._gateway_call(request, lambda p: self.gateway.list_keys(p, view=request.query_one("view")))

    def _gateway_key_create(self, request):
        return self._gateway_call(request, lambda p, b: self.gateway.create_key(p, b), write=True, body=True)

    def _gateway_key_revoke(self, request, key_id: str):
        return self._gateway_call(request, lambda p: self.gateway.revoke_key(p, key_id), write=True)

    def _gateway_key_update(self, request, key_id: str):
        return self._gateway_call(request, lambda p, b: self.gateway.update_key(p, key_id, b), write=True, body=True)

    def _gateway_log_detail(self, request, request_id: str):
        return self._gateway_call(request, lambda p: self.gateway.log_detail(p, request_id))

    def _gateway_models(self, request):
        return self._gateway_call(request, lambda p: self.gateway.list_routes(p))

    def _gateway_model_publish(self, request):
        return self._gateway_call(request, lambda p, b: self.gateway.publish_route(p, b), write=True, body=True)

    def _gateway_model_update(self, request, route_id: str):
        return self._gateway_call(request, lambda p, b: self.gateway.update_route(p, route_id, b), write=True, body=True)

    def _gateway_logs(self, request):
        return self._gateway_call(request, lambda p: self.gateway.logs(
            p, view=request.query_one("view"), model=request.query_one("model"),
            outcome=request.query_one("outcome"), key_id=request.query_one("key_ref"),
            limit=request.query_one("limit")))

    def _gateway_playground(self, request):
        return self._gateway_call(request, lambda p, b: self.gateway.playground(p, b), write=True, body=True)

    # --- /api/v2/registry/engines/{id}/params -------------------------------

    def _engine_for_params(self, principal, engine_id: str, *, write: bool):
        policy = self.service.policy(principal.requester)
        try:
            engine = policy.require_engine_read(engine_id)
        except registry_errors.NotFound:
            # 通过网关共享给全实验室的服务，成员也能看它的参数（只读）。
            shared = self.gateway is not None and any(
                r.engine_id == engine_id and r.enabled and r.visibility == "all" and not self.service.config.personal_mode
                for r in self.gateway.store.routes()
            )
            engine = self.service.store.engine_by_id(engine_id) if shared else None
            if engine is None:
                raise
        if write and engine.owner_principal != principal.principal and (self.service.config.personal_mode or not principal.is_platform_admin):
            raise registry_errors.AccessDenied("only the service owner or an administrator can change its parameters")
        return engine

    def _params_payload(self, principal, engine) -> dict:
        framework = (engine.launch or {}).get("framework") or next((t.split(":", 1)[1] for t in (engine.tags or ()) if t.startswith("framework:")), None)
        schema = runtime_params.schema_for(framework)
        target = engine.target or {}
        def authenticated_fetch(_host, _port, path):
            from .service_metrics import fetch
            try:
                code, body = fetch(engine, path, key=self.gateway.core.upstream_key(engine) if self.gateway else None)
                return (code, body.encode()) if code == 200 else None
            except (OSError, ValueError):
                return None
        observed = (
            runtime_params.observe(framework, target.get("host"), int(target.get("port")),
                                   fetch=self.param_fetch or authenticated_fetch)
            if schema else {"reachable": None, "values": {}}
        )
        saved = self.param_store.get(engine.id) if self.param_store else None
        desired = (saved or {}).get("values") or {}
        return {
            "engine_id": engine.id,
            "engine_name": engine.name,
            "framework": framework,
            "supported": schema is not None,
            "schema": [p.to_dict() for p in schema["params"]] if schema else [],
            "observed": observed,
            "desired": desired,
            "desired_updated_at": (saved or {}).get("updated_at"),
            "state": runtime_params.compare(desired, observed["values"], schema) if schema else "none",
            "command": runtime_params.render_command(framework, desired, port=target.get("port")) if (schema and desired) else None,
            "can_edit": engine.owner_principal == principal.principal or (principal.is_platform_admin and not self.service.config.personal_mode),
            "apply": "restart_by_owner",
        }

    def _engine_params(self, request, engine_id: str):
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        engine = self._engine_for_params(principal, engine_id, write=False)
        return _json_response(200, {"ok": True, "data": self._params_payload(principal, engine)})

    def _engine_params_update(self, request, engine_id: str):
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        if self.param_store is None:
            return _error_response(503, "bff.params_not_attached", "service parameters are not attached")
        engine = self._engine_for_params(principal, engine_id, write=True)
        body = _json_body(request)
        if body is None:
            return _error_response(400, "bff.invalid_request", "a JSON object body is required")
        payload = self._params_payload(principal, engine)
        if not payload["supported"]:
            return _error_response(400, "bff.invalid_request", "this framework has no parameter schema")
        try:
            values = runtime_params.validate(payload["framework"], body.get("values"))
        except ValueError as exc:
            return _error_response(400, "bff.invalid_request", str(exc))
        self.param_store.put(engine.id, payload["framework"], values, principal.principal)
        return _json_response(200, {"ok": True, "data": self._params_payload(principal, engine)})

    # --- 代理任务：下发启动/停止/重启，按 id 查结果 ---------------------------

    #: 这条请求体**只**认这两个键。其余（``argv`` / ``uid`` / ``owner`` /
    #: ``principal`` ……）一律 400，而不是静默忽略：本层是浏览器入口，执行入口
    #: 只有登记行上的 unit 与服务端的 argv 形状，客户端不能在这里自选。
    _SERVE_BODY_FIELDS = frozenset({"kind", "unit"})

    def _engine_verify(self, request, engine_id: str):
        """让平台**现在**探一次这个服务，把今天的结果写回登记。

        与 ``_engine_serve`` 是两件事，别混：serve 是「任务已下发给你的代理」，
        这里的结果**同步**返回，测的就是此刻。探测失败也照样写回（状态变成
        unavailable），因为「刚才探过、没探通」本身就是今天的结论。
        """
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        view = self.service.verify_engine(principal.requester, engine_id)
        data = dict(view.get("data") or {})
        # 探测源缺失时 verify_engine 不改状态，只在 notes 里说明；原样带出去，
        # 前端才能说「没测」而不是「测过、没问题」。
        data["notes"] = list(view.get("notes") or ())
        return _json_response(200, {"ok": True, "data": data})

    def _engine_serve(self, request, engine_id: str):
        """请本人的代理启动/停止/重启这个服务。

        成功是 **202 Accepted**：任务只是排进了 owner 自己 uid 的队列
        （``state == "queued"``），代理还没领取、更没说执行成功。把它报成
        200/``done`` 会让前端把「已下发」误读成「已起来」。
        """
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        body = _json_body(request)
        if not isinstance(body, dict):
            raise registry_errors.InvalidRequest("a JSON object body is required")
        unknown = sorted(set(body) - self._SERVE_BODY_FIELDS)
        if unknown:
            # 身份与执行入口都只能来自服务端：收到自报字段是拒绝，不是忽略。
            raise registry_errors.InvalidRequest(
                f"unsupported field(s): {', '.join(unknown)}"
            )
        kind = body.get("kind")
        if kind not in AGENT_TASK_KINDS:
            raise registry_errors.InvalidRequest(
                f"unsupported agent task kind: {kind!r}"
            )
        task = self.service.enqueue_agent_task(
            principal.requester, engine_id, kind, unit=body.get("unit")
        )
        return _json_response(202, {"data": task.to_dict()})

    def _agent_task(self, request, task_id: str):
        """按 id 查一条已下发的任务。

        只读、不写：``queued`` / ``running`` 都只说「还没结果」，``done`` /
        ``failed`` 才是代理回报之后的终态。他人的任务与不存在的任务共用 404
        （Registry 的 ``NotFound``），不把「存在但无权看」变成一个可枚举的信号。
        """
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        task = self.service.agent_task(principal.requester, task_id)
        return _json_response(200, {"data": task.to_dict()})

    # --- POST /api/v2/registry/engines[/preflight] --------------------------

    def _registry_body(self, request):
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        body = _json_body(request)
        if body is None:
            raise registry_errors.InvalidRequest("a JSON object body is required")
        return principal, body

    def _registry_preflight(self, request):
        """网页登记前的只读预检。owner 只来自会话；Registry 不写库、不写审计。"""
        principal, body = self._registry_body(request)
        envelope = self.service.preflight_engine(principal.requester, body)
        return _json_response(200, envelope)

    def _registry_discovery(self, request):
        """本机候选（只读）。"""
        self._reject_asserted_identity(request)
        principal = self._principal(request)
        return _json_response(200, self.service.discover(principal.requester))

    def _registry_model_register(self, request):
        """网页登记模型。与 CLI 的 ``models register`` 走同一个 ``register_model``。"""
        principal, body = self._registry_body(request)
        envelope, created = self.service.register_model(
            principal.requester, body, idempotency_key=request.header("idempotency-key")
        )
        return _json_response(201 if created else 200, envelope)

    def _registry_register(self, request):
        """网页登记推理服务。与 CLI 登记走同一个 ``RegistryService.register_engine``。"""
        principal, body = self._registry_body(request)
        envelope, created = self.service.register_engine(
            principal.requester, body, idempotency_key=request.header("idempotency-key")
        )
        return _json_response(201 if created else 200, envelope)

    def _share(self, request, deployment_id: str):
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)

        if bff_scopes.PORTAL_ENDPOINTS_SHARE not in principal.portal_scopes:
            return _error_response(
                403,
                registry_errors.AccessDenied.contract_code,
                f"missing required scope {bff_scopes.PORTAL_ENDPOINTS_SHARE}",
            )

        body = _json_body(request)
        if body is None:
            return _error_response(400, "bff.invalid_request", "a JSON object body is required")

        mode = bff_sharing.portal_to_registry_mode(body.get("mode"))
        grantees = bff_sharing.resolve_grantees(
            body.get("principals"),
            principals=self.service.principals,
            owner_principal=principal.principal,
        )
        if mode == VISIBILITY_USERS and not grantees:
            raise registry_errors.InvalidRequest('mode "selected" needs at least one principal')

        requester = principal.requester
        policy = self.service.policy(requester)
        # 404 与 403 的分界由 Registry 决定：看不见 = 不存在。
        deployment = policy.require_deployment_read(deployment_id)

        mine = [
            item
            for item in policy.visible_endpoints()
            if item.deployment_id == deployment.id and item.owner_principal == principal.principal
        ]
        if not mine:
            # 这个部署名下没有一条属于我的 Endpoint：注册与共享是分开的，
            # 没有 Endpoint 就没有可共享的东西——不是一个可以「顺手建一条」的场景。
            return _error_response(
                404,
                registry_errors.NotFound.contract_code,
                f"deployment {deployment_id!r} has no endpoint owned by this principal",
            )

        for endpoint in mine:
            if mode == VISIBILITY_ALL_APPROVED:
                self.service.share_endpoint(
                    requester, endpoint.id, {"mode": VISIBILITY_ALL_APPROVED}
                )
            elif mode == VISIBILITY_USERS:
                self.service.share_endpoint(
                    requester, endpoint.id, {"mode": VISIBILITY_USERS, "grantees": list(grantees)}
                )
            else:
                # private：撤销全部授权并把可见性收回私有，一步到位。
                self.service.unshare_endpoint(requester, endpoint.id)

        payload = bff_ops.build_ops(
            principal=principal,
            service=self.service,
            visibility=bff_ops.VISIBILITY_PRIVATE_PORTAL,
            token_bindings=self.token_bindings,
            observability=self.observability,
            evaluation=self.evaluation,
            role_lookup=self._role_lookup,
            links=self.links,
            config=self.ops_config,
        )
        row = next(
            (item for item in payload["registry"]["owned"] if item["deployment_id"] == deployment_id),
            None,
        )
        if row is None:  # pragma: no cover - 刚刚才读到过它
            return _error_response(
                500, "bff.internal_error", "the deployment disappeared while sharing"
            )
        return _json_response(
            200,
            {
                "data": {"deployment": row},
                "meta": {"mode": body.get("mode"), "principals": list(grantees)},
            },
        )

    # --- Linux 登录 ---------------------------------------------------------

    def _linux_status(self, request):
        del request
        if self.linux_service is None:
            return _json_response(
                200,
                {
                    "data": {
                        "enabled": False,
                        "available": False,
                        "reason": bff_identity.REASON_NOT_LINUX,
                    }
                },
            )
        status = self.linux_service.status()
        data = status.to_dict()
        # 真实 PAM 后端没有实现：这里如实说自己只到「配置好了没」这一层，
        # 不声称任何账号的密码真的可以用。
        data["password_verification"] = "unavailable_in_this_build"
        return _json_response(200, {"data": data})

    def _linux_login(self, request):
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        if self.linux_service is None:
            return _error_response(
                503, "linux_auth_not_configured", "Linux sign-in is not configured on this server."
            )
        body = _json_body(request)
        if body is None:
            return _error_response(400, "bff.invalid_request", "a JSON object body is required")

        cookie_name = self.cfg.cookie_name
        issued = sign_in_with_linux(
            linux_service=self.linux_service,
            handoff=self.handoff,
            sessions=self._sessions,
            login_name=str(body.get("login_name") or ""),
            password=str(body.get("password") or ""),
            client_key=_client_key(request),
            pre_login_session_id=request.cookies.get(cookie_name),
        )

        user = self.auth_app.service.db.get_user(issued.user.id)
        identity = (
            self.auth_app.service.public_identity(user)
            if user is not None
            else {"id": issued.user.id, "display_name": None, "role": None}
        )
        return _json_response(
            200,
            {"data": {"session": {"subject": identity.get("id")}, "identity": identity}},
            headers=[
                (
                    "Set-Cookie",
                    self._sessions.cookie_header(
                        issued.session_id, name=cookie_name, secure=self.cfg.cookie_secure
                    ),
                )
            ],
        )

    # --- 共享管理员模式 -----------------------------------------------------

    def _admin_mode(self) -> bff_admin_mode.AdminModeService:
        if self.admin_mode is None:
            raise bff_admin_mode.NotConfigured(detail="admin mode is not wired in this build")
        return self.admin_mode

    def _admin_status(self, request):
        """当前会话此刻的管理员模式状态。

        GET、只读、不需要 CSRF；没有会话也是一个合法的答案（``elevated: false``）。
        它回答的是"这个部署配了管理口令没有"，与 ``/auth/linux/status`` 同性质——
        一个服务器状态的公开说明，不是任何人的秘密。
        """
        self._reject_asserted_identity(request)
        if self.admin_mode is None:
            return _json_response(
                200,
                {"data": {"admin_mode": {"configured": False, "elevated": False,
                                         "ttl_seconds": bff_admin_mode.DEFAULT_TTL_SECONDS,
                                         "expires_at": None}}},
            )
        state = self.admin_mode.status_for(request.cookies.get(self.cfg.cookie_name))
        return _json_response(200, {"data": {"admin_mode": state.to_dict()}})

    def _admin_enter(self, request):
        """用共享口令把**当前这条会话**提权。

        顺序是刻意的，也是本路由的全部安全性所在：

        1. 拒绝自报身份的头（``X-Admin`` 之类出现即 400）；
        2. 复用既有 CSRF / 同源校验——提权是一个写操作；
        3. ``_principal`` 把 cookie 解成**受信主体**；共享口令提权**只**适用于
           ``linux_pam`` 会话。没有登录、Feishu 会话在这一步拿不到主体；平台账户
           与本机 ``admin`` 拿得到主体，但它们的角色由账户管理决定、不需要也不
           允许用共享口令提权，所以在这里明确拒绝，走不到口令校验；
        4. 只有到这里，才把主体 + 口令交给 ``AdminModeService``。
        """
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        if principal.user is None or principal.user.auth_source != auth_store.SOURCE_LINUX:
            raise bff_identity.NotLinuxSession(
                "shared-password admin mode applies to Linux sessions only; "
                "platform accounts get their role from account management"
            )

        body = _json_body(request)
        if body is None:
            return _error_response(400, "bff.invalid_request", "a JSON object body is required")

        state = self._admin_mode().enter(
            session_id=request.cookies.get(self.cfg.cookie_name),
            principal=principal.principal,
            uid=principal.uid,
            linux_user=principal.linux_user,
            password=body.get("password"),
            client_key=_client_key(request),
        )
        return _json_response(200, {"data": {"admin_mode": state.to_dict()}})

    def _admin_exit(self, request):
        """退出管理员模式。幂等，且不要求会话仍然处于提权状态。"""
        self._reject_asserted_identity(request)
        self._write_preflight(request)
        principal = self._principal(request)
        state = self._admin_mode().exit(
            session_id=request.cookies.get(self.cfg.cookie_name),
            principal=principal.principal,
            uid=principal.uid,
            linux_user=principal.linux_user,
        )
        return _json_response(200, {"data": {"admin_mode": state.to_dict()}})

    def _forget_elevation(self, request) -> None:
        """登出后清掉这条会话的提权行。失败不影响登出本身。"""
        if self.admin_mode is None:
            return
        try:
            self.admin_mode.forget_session(request.cookies.get(self.cfg.cookie_name))
        except Exception:  # noqa: BLE001 - 登出已经成功，不因为清理失败而报错
            return

    # --- 公共小工具 ---------------------------------------------------------

    def _reject_asserted_identity(self, request) -> None:
        bff_identity.reject_identity_assertions(request.headers.keys())

    def resolve_session(self, session_id: object):
        """会话 cookie 值 ⇒ :class:`~backend.bff.identity.Principal`。

        这是 BFF 对内的**受信身份接缝**：凡是需要「这个请求是谁」的地方——
        包括将来把 invoke 接到同一套身份上——都走这一个方法，不要各自再解析
        一遍 cookie 或 header。

        管理员模式在这里被读入，因此每个受保护路由拿到的 ``portal_scopes`` 与
        ``effective_role`` 都已经反映了**此刻**的提权状态：提权一过期，下一次
        解析就自动回到成员，不需要任何缓存失效逻辑。
        """
        return bff_identity.resolve_principal(
            session_id=session_id,
            auth_service=self.auth_app.service,
            accounts=self.accounts,
            admin_mode=self.admin_mode,
        )

    def resolve_credential(self, request):
        """请求 ⇒ :class:`Principal`，由**哪条凭据通道负责**决定。

        顺序是刻意的：认 token 的通道在前，会话通道兜底。一个同时带着 cookie 与
        ``Authorization`` 的请求因此不会「静默地按 cookie 处理」——那正是凭据混淆
        最容易被忽略的那种结局。token 通道默认关闭，于是这种请求得到 401，而不是
        得到一次成功的会话解析。
        """
        for source in (self.token_source, self.session_source):
            if source.handles(request):
                return source.resolve(request)
        # 会话通道永远 handles，所以这里够不到；留着是为了让「没有通道负责」
        # 有一个明确的结局，而不是靠调用方相信循环一定返回。
        raise bff_identity.NoSession("no credential was presented")

    def _principal(self, request):
        return self.resolve_credential(request)

    def _invoke_preflight(self, request) -> None:
        """``invoke`` 专用的写前置：**由凭据通道决定**要不要 SESSION-CSRF。

        CSRF 保护的是**环境凭据**：浏览器会自作主张把 session cookie 附到跨站请求
        上，所以「带 cookie 的写」必须证明请求来自本方源。``Authorization: Bearer``
        不是环境凭据 —— 浏览器不会替任何人自动附加它，一个跨站页面拿不到它。
        因此对**只有** token 的请求要求 CSRF，得到的只有一种结果：合法 CLI 无法
        调用（Issue #68 D4），而攻击者一分钱都没多花。这不是更安全，是接错线。

        于是这里按通道分流，**且只有这一条路由这样分流**：

        * **只有 ``Authorization``**：token 通道负责身份，不要求 session CSRF/Origin。
          令牌无效时 ``_principal`` 照常 401 ——「带了个坏 token」不能换来任何豁免，
          因为此时根本没走到转发。
        * **只有 session cookie**：走既有 ``_write_preflight``（CSRF + 同源），
          与 ``share`` / ``admin/*`` 完全一致。
        * **两者同时出现**：400 ``credential_confusion_rejected``。任何一侧优先都会
          是一次隐式降级：优先 cookie 会拒掉 CLI，优先 token 会让一个带会话的请求
          借 token 之名跳过 CSRF。

        刻意**不**把这条路做成全局策略：其它写入口（``share``、``admin/*``）仍然只
        接受会话 + CSRF，本轮没有任何决定要求放开它们。
        """
        authorization = bff_identity.authorization_header(request)
        cookie = request.cookies.get(self.cfg.cookie_name)
        if authorization is not None:
            if cookie:
                raise bff_identity.CredentialConfusion(
                    "send either a session cookie or a scoped token, not both"
                )
            # 请求体上界仍然要管：超界在任何通道上都是 413，不是「token 就免检」。
            self._reject_oversized_body(request)
            # 纵深防御：CLI 不发 ``Origin``，所以「没有 Origin」不是失败；但一个
            # **带了**跨源 Origin 的 token 请求仍然拒绝 —— 浏览器不可能给跨源请求
            # 附上这个头，除非它真的以为自己在跨源调用。
            origin = (getattr(request, "headers", None) or {}).get("origin")
            if origin is not None and not self.auth_app._origin_ok(request):  # noqa: SLF001
                raise auth_errors.CsrfRejected(detail="Origin was present and cross-origin")
            return
        self._write_preflight(request)

    def _reject_oversized_body(self, request) -> None:
        """与 auth preflight 同一条上界，只做大小这一件事。

        token 通道不走 ``_write_preflight``（那会连 CSRF 一起做），但**体积**不是
        CSRF 的一部分：一个 64 KiB 以上的载荷在到达 Registry 之前就该被 413 拒绝。
        """
        if len(request.body) > self.cfg.max_request_bytes:
            raise auth_errors.PayloadTooLarge()

    def _write_preflight(self, request) -> None:
        """写操作的 CSRF / 同源校验，**复用**既有实现。

        刻意调用既有 auth 应用的 ``_preflight``：Origin 整串比较与一次性 CSRF
        token 是两条已经有独立测试的安全规则，在这里重写一份并不会更安全，
        只会让「哪一份是对的」变成一个需要考古的问题。
        """
        self.auth_app._preflight(request)  # noqa: SLF001 - 复用即本意，见上

    def _role_lookup(self, principal: str) -> str | None:
        """某个主体的**常驻**角色，供管理视图列出他人时使用。

        刻意返回常驻角色（永远是成员）而不是用户表的 ``role``：提权是**每条会话**
        的状态，不是某个人的属性。把 ``carol`` 在用户表里的 ``platform_admin`` 写到
        这份名单上，等于在 UI 里宣称"这个人一直是管理员"，而新模型下他一登出就是
        普通成员。查不到主体时不写角色，而不是给个默认值。
        """
        user = self.auth_app.service.db.get_user(principal)
        if user is None:
            return None
        return bff_scopes.session_role(elevated=False)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _default_links() -> list[dict]:
    """同源内部链接。href 全部是根相对路径，永远不含 :8080。"""
    return [
        {
            "label": "我的调用日志",
            "href": "/atlas#calls",
            "kind": "internal",
            "requires": bff_scopes.PORTAL_CALLS_SELF_READ,
        }
    ]


def _path_capture(path: str, pattern: tuple[str, str]) -> str | None:
    """一条带变量路径里**被捕获的那一段**。

    形状的合法性已经由 ``route_map.match`` 判过（非空、不含 ``/``），所以走到这里
    的路径一定匹配；``None`` 只留给「形状对但解出来是空」这种边角，此时实现自己
    决定怎么答复。``unquote`` 放在这里而不是各处理器里，是为了让两条带变量的路由
    对「编码过的 id」有同一个口径。
    """
    prefix, suffix = pattern
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    end = len(path) - len(suffix) if suffix else len(path)
    middle = path[len(prefix) : end]
    if not middle or "/" in middle:
        return None

    from urllib.parse import unquote

    return unquote(middle)


def _parse_instant(value: object) -> datetime | None:
    """查询串里的一个瞬时 ⇒ 带时区的 ``datetime``；不是就 ``None``。

    复用观测模块的 :func:`parse_instant`：naive 时间戳**不接受**——无从判定它属于
    哪个时区，因而无从判定它有没有过期。一个 naive 的游标正是「这条记录该不该在
    这一页里」这个问题的答案被悄悄丢掉的地方。
    """
    from ..observability.host_projection import parse_instant

    return parse_instant(value)


def _truthy(value: object) -> bool:
    """查询串里的一个布尔。**只认两种写法**，其余一律 ``False``。

    不用 ``bool(value)``：``"false"`` 是一个非空字符串，因此是 truthy 的 —— 把
    ``?include_unowned=false`` 读成 true，正是这类参数最典型的翻车方式。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _client_key(request) -> str:
    """节流用的客户端标识。取服务端看到的来源地址，不取任何可伪造的头。"""
    return str(getattr(request, "client_ip", "") or "unknown")


def _body_field_names(request) -> tuple[str, ...]:
    """请求体的**字段名**，只用于身份断言守卫。

    只取 key、只取顶层、**不校验值**：这道门回答的是「请求有没有试图自报身份」，
    而不是「这个请求体合不合法」——后者归各自处理函数。

    解析失败时返回空元组，而不是抛错：一个畸形 JSON 体应当由该路由自己的
    ``_json_body`` 判成 400 ``bff.invalid_request``，而不是在这里被改写成
    ``identity_assertion_rejected``。错误 **token** 也是有语义的，不能让一个解析
    细节把它换掉。
    """
    body = getattr(request, "body", b"") or b""
    if not body:
        return ()
    try:
        payload = request.json_body()
    except auth_errors.AuthError:
        return ()
    if not isinstance(payload, dict):
        return ()
    return tuple(str(name) for name in payload)


class _IntegrationsNotAttached(Exception):
    pass


#: CLI / Agent 的 Registry API 前缀（与 ``backend.registry.api.PREFIX`` 相同）。
REGISTRY_API_PREFIX = "/api/v2/registry/"


def _cookie_names(raw: object) -> set[str]:
    if not isinstance(raw, str) or not raw:
        return set()
    return {part.split("=", 1)[0].strip() for part in raw.split(";") if part.strip()}


def _json_body(request) -> dict | None:
    raw = request.body
    if not raw:
        return None
    try:
        return request.json_body()
    except auth_errors.AuthError:
        return None


def _json_response(status: int, payload: Any, *, headers: list | None = None):
    from ..auth.routes import Response

    merged = [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")]
    merged.extend(headers or [])
    return Response.json(status, payload, headers=merged)


def _error_response(status: int, code: str, message: str, *, headers: list | None = None):
    return _json_response(status, {"error": {"code": code, "message": message}}, headers=headers)


def _auth_error_response(exc: auth_errors.AuthError):
    """既有 auth 的异常 ⇒ 与前端状态机对齐的响应。"""
    code = getattr(exc, "code", None) or type(exc).__name__.lower()
    if isinstance(exc, auth_errors.NotAuthenticated):
        status = 401
    elif isinstance(exc, auth_errors.Forbidden):
        status = 403
    elif isinstance(exc, auth_errors.RateLimited):
        status = 429
    elif isinstance(exc, auth_errors.PayloadTooLarge):
        status = 413
    elif isinstance(exc, auth_errors.InvalidRequest):
        status = 400
    else:
        status = 500
    return _error_response(status, code, str(exc) or code)


__all__ = [
    "ADMIN_ENTER_PATH",
    "ADMIN_EXIT_PATH",
    "ADMIN_STATUS_PATH",
    "ATLAS_CALLS_PATH",
    "CAPABILITIES_PATH",
    "DEFAULT_REASON",
    "ENGINES_PATH",
    "EVAL_REPORTS_PATH",
    "EVAL_REPORT_SOURCES_PATH",
    "EVENTS_PATH",
    "INFRA_SUMMARY_PATH",
    "SYSTEM_RESOURCES_PATH",
    "MODEL_SERVICE_KEY_PATH",
    "INVOKE_PATH_PREFIX",
    "LOGOUT_PATH",
    "SERVICES_PATH",
    "USAGE_PATH",
    "LINUX_LOGIN_PATH",
    "LINUX_STATUS_PATH",
    "OPS_PATH",
    "RESERVED_BIND_PORTS",
    "OpsApplication",
]
