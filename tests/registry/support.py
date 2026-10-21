"""测试脚手架：合成身份 + 内存库 + 确定性替身。

刻意**不**提供「真跑一遍生产」的开关：所有替身都在这里显式标注为替身，
并且所有 token 字符串都以 ``synthetic-`` 开头，任何越界的「看起来像真的」都
会在断言里被抓住。
"""

from __future__ import annotations

import dataclasses
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # noqa: E402 - 让 tests 能直接 import backend.*
    sys.path.insert(0, str(ROOT))

from backend.registry.api import create_app  # noqa: E402
from backend.registry.gateway import MockGatewayAdapter  # noqa: E402
from backend.registry.health import (  # noqa: E402
    PROTOCOL_VERIFIED,
    REASON_MODEL_LIST_VERIFIED,
    REASON_TRANSPORT_UNREACHABLE,
    ProbeResult,
    ProtocolObservation,
    StaticProber,
    StaticProtocolProber,
)
from backend.registry.identity import (  # noqa: E402
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TokenBinding,
    TokenDirectory,
    resolve_requester,
)
from backend.registry.registry import RegistryConfig, RegistryService  # noqa: E402
from backend.registry.ownership import StaticOwnershipProver  # noqa: E402
from backend.registry.store import Store  # noqa: E402

#: 合成账号与 UID（见 tests/registry/__init__.py）。``dana`` 刻意不在表内。
HOST_USERS = (("carol", 4000), ("alice", 4001), ("dave", 4002), ("bob", 4003))

#: 全部凭据都带这个前缀，方便断言「响应里不该出现凭据」。
TOKEN_PREFIX = "synthetic-"

NON_ADMIN_SCOPES = frozenset(
    {
        "registry:read",
        "registry:write",
        "registry:endpoint:share",
        "registry:endpoint:invoke",
    }
)
#: 运维/管理员：除了全局只读与可授权管理，还要 ``registry:write`` ——
#: 登记一台**既有**服务本身是一次写操作，operator 需要「能登记」+「有权承认既有服务」
#: 两件事同时具备。刻意保持二者都必需：只有 admin_manage 而没有 write 的人，
#: 不该能凭空造出一条 engine 行。
ADMIN_SCOPES = frozenset(
    {
        "registry:read",
        "registry:write",
        "registry:admin:read",
        "registry:admin:manage",
    }
)
#: 只有全局只读、没有可授权管理 —— 用来证明「能看全局 != 能改全局」。
ADMIN_READ_ONLY_SCOPES = frozenset({"registry:read", "registry:admin:read"})

#: 引擎目标端口替身的默认映射。
HEALTHY_PORT = 8000
DEGRADED_PORT = 8001
UNREACHABLE_PORT = 8081
#: 夹具里所有目标都写在这个字面量上（与 ``urls.LOOPBACK_HOST_ALIASES`` 的结果一致）。
LOOPBACK_HOST = "127.0.0.1"

#: 「没传这个参数」的标记。用它而不是 None，是因为 None 在 prober 上另有含义。
_UNSET = object()

#: 本进程内创建过的 Harness。测试模块在 ``tearDownModule`` 里调 :func:`close_all`
#: 把它们的内存库关掉 —— 否则每条 sqlite 连接都会在 GC 时冒一个 ResourceWarning，
#: 把真正的失败淹掉。
_LIVE: list["Harness"] = []


def close_all() -> None:
    """关闭所有仍然打开的 Harness（供各测试模块的 ``tearDownModule`` 调用）。"""
    while _LIVE:
        _LIVE.pop().close()


def default_prober() -> StaticProber:
    """确定性 prober：三个端口分别对应 健康 / 降级 / 不可达。

    不可达那一条**没有** latency —— 与真实探测失败时的形状一致。
    """
    return StaticProber(
        {
            HEALTHY_PORT: ProbeResult(reachable=True, latency_ms=4.2),
            DEGRADED_PORT: ProbeResult(reachable=True, latency_ms=11.0, degraded=True),
            UNREACHABLE_PORT: ProbeResult(reachable=False, detail="connection refused"),
        }
    )


def default_protocol_prober() -> StaticProtocolProber:
    """确定性**协议** prober（第二根轴）：把两个「活着的」端口判成模型服务。

    这是本文件里最需要说清楚的一条：测试用的协议结论是**注入的 fixture**，
    它的存在只证明「代码会照着协议结论做判断」，**不**证明任何真实端口上跑着
    一个模型服务。真实的那条路是 ``health.LoopbackProtocolProber``（只读
    ``/v1/models``），它需要真的有一个上游才能给出 ``verified``。
    """
    verified = ProtocolObservation(
        state=PROTOCOL_VERIFIED,
        reason=REASON_MODEL_LIST_VERIFIED,
        protocol="openai_compatible",
        checked_path="/v1/models",
        http_status=200,
        model_ids=3,
        source="static_test_double",
    )
    return StaticProtocolProber(
        {
            (LOOPBACK_HOST, HEALTHY_PORT): verified,
            (LOOPBACK_HOST, DEGRADED_PORT): verified,
            (LOOPBACK_HOST, UNREACHABLE_PORT): ProtocolObservation(
                state="unverified",
                reason=REASON_TRANSPORT_UNREACHABLE,
                protocol="openai_compatible",
                source="static_test_double",
            ),
        }
    )


def default_ownership_prover() -> StaticOwnershipProver:
    """确定性**所有权** prover：把夹具端口判成「属于 alice(4001)」。

    同样只是 fixture。真实那条路是可选的 ``ownership.ProcNetOwnershipProver``
    （读 ``/proc/net/tcp(6)`` 的 uid 列），它自己的测试在 ``test_proc_net_ownership.py``。
    """
    return StaticOwnershipProver(
        {
            (LOOPBACK_HOST, HEALTHY_PORT): 4001,
            (LOOPBACK_HOST, DEGRADED_PORT): 4001,
        }
    )


class _FixedReadbackRouter:
    """一个只报告**固定代数**的 Router 替身。

    用于「这次回读已经成功过」的夹具：它不发布新快照，只回答
    ``applied_revision()``。真实的 `FileRouterAdapter` 从 Gateway 发布的
    ``status.json`` 读同一个数字；这个替身把它钉住，让 invoke 的路由门可以被
    单独验证。
    """

    name = "fixed_readback_double"
    attached = True

    def __init__(self, revision: int) -> None:
        self._revision = int(revision)

    def publish(self, snapshot: dict):
        from backend.registry.router import ROUTER_SYNC_PENDING, RouterPublishResult

        return RouterPublishResult(
            published=False,
            applied_revision=None,
            snapshot_sha256=None,
            state=ROUTER_SYNC_PENDING,
            reason="test_fixture",
            detail="fixture: this double only reports an already-confirmed revision",
        )

    def applied_revision(self) -> int | None:
        return self._revision


class Harness:
    """一份完整的测试装置：内存库 + 合成目录 + WSGI 应用 + 便捷断言入口。"""

    def __init__(
        self,
        *,
        prober: object | None = _UNSET,
        protocol_prober: object | None = _UNSET,
        ownership_prover: object | None = _UNSET,
        gateway: object | None = None,
        router: object | None = None,
        config: RegistryConfig | None = None,
        principals: PrincipalDirectory | None = None,
        bindings: list[TokenBinding] | None = None,
        mode: str = "synthetic",
    ) -> None:
        self.principals = principals or PrincipalDirectory(
            [Principal(name, name, uid) for name, uid in HOST_USERS]
        )
        if bindings is None:
            bindings = []
            for name, _uid in HOST_USERS:
                scopes = ADMIN_SCOPES if name == "carol" else NON_ADMIN_SCOPES
                bindings.append(
                    TokenBinding(
                        f"{TOKEN_PREFIX}{name}",
                        name,
                        frozenset(scopes),
                        roles=frozenset(),
                        label="test",
                    )
                )
        self.bindings = bindings
        self.tokens = TokenDirectory(bindings)
        # ``prober=None`` 是一个**有意义**的取值（本部署没有观测源），
        # 所以缺省不能用 None 表示 —— 那会让「想测没有观测源」的用例静默拿到一个真 prober。
        self.prober = default_prober() if prober is _UNSET else prober
        self.protocol_prober = (
            default_protocol_prober() if protocol_prober is _UNSET else protocol_prober
        )
        self.ownership_prover = (
            default_ownership_prover() if ownership_prover is _UNSET else ownership_prover
        )
        self.gateway = MockGatewayAdapter() if gateway is None else gateway
        #: ``None`` 让服务用缺省的 ``DetachedRouterAdapter``（不写任何 Gateway）。
        self.router = router
        self.store = Store(":memory:")
        self.service = RegistryService(
            store=self.store,
            tokens=self.tokens,
            principals=self.principals,
            config=config or RegistryConfig(mode=mode),
            prober=self.prober,
            gateway=self.gateway,
            protocol_prober=self.protocol_prober,
            ownership_prover=self.ownership_prover,
            router=router,
        )
        self.app = create_app(self.service)
        _LIVE.append(self)

    # -- 生命周期 ---------------------------------------------------------

    def close(self) -> None:
        """关掉内存库。可重复调用。"""
        self.store.close()
        for index, live in enumerate(_LIVE):
            if live is self:
                del _LIVE[index]
                break

    # -- 身份 -------------------------------------------------------------

    def token(self, principal: str) -> str:
        return f"{TOKEN_PREFIX}{principal}"

    def requester(self, principal: str) -> RequesterContext:
        return resolve_requester(self.token(principal), self.tokens, self.principals)

    def with_scopes(
        self,
        principal: str,
        scopes: frozenset[str],
        *,
        roles: frozenset[str] = frozenset({"admin"}),
    ) -> Harness:
        """把某个主体的 scope 换成另一套（用于「有身份但没权限」的用例）。

        必须**替换**而非追加：追加会留下原来那条 token，于是 ``harness.token(name)``
        仍然解析到旧 scope，测试就变成了永远通过的假测试。
        ``roles`` 默认给一个 admin —— 用来证明 role 本身不产生任何权限。
        """
        self.bindings = [b for b in self.bindings if b.principal != principal]
        self.bindings.append(
            TokenBinding(
                f"{TOKEN_PREFIX}{principal}",
                principal,
                frozenset(scopes),
                roles=frozenset(roles),
                label="test",
            )
        )
        self.tokens = TokenDirectory(self.bindings)
        self.service.tokens = self.tokens
        self.app.tokens = self.tokens
        return self

    # -- 传输层模拟 -------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        principal: str | None = None,
        token: str | None = None,
        body: object = None,
        raw_body: bytes | None = None,
        headers: dict | None = None,
        content_type: str | None = "application/json",
        extra_environ: dict | None = None,
    ) -> tuple[int, dict, dict]:
        """在进程内驱动 WSGI 应用。返回 ``(status, headers, payload)``。"""
        payload = raw_body
        if payload is None and body is not None:
            payload = json.dumps(body).encode("utf-8")
        payload = payload or b""

        environ: dict = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": path,
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(payload),
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8787",
            "CONTENT_LENGTH": str(len(payload)),
        }
        if payload and content_type is not None:
            environ["CONTENT_TYPE"] = content_type
        resolved_token = token if token is not None else (
            self.token(principal) if principal else None
        )
        if resolved_token:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {resolved_token}"
        for key, value in (headers or {}).items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value
        if extra_environ:
            environ.update(extra_environ)

        captured: dict = {}

        def start_response(status: str, response_headers: list) -> None:
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = dict(response_headers)

        raw = b"".join(self.app(environ, start_response))
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return captured["status"], captured.get("headers", {}), parsed

    # -- 常用编排 ---------------------------------------------------------

    def make_engine(self, principal: str, name: str, *, port: int = HEALTHY_PORT) -> str:
        status, _headers, payload = self.request(
            "POST",
            "/api/v2/registry/engines",
            principal=principal,
            body={"name": name, "target_url": f"http://127.0.0.1:{port}/v1"},
        )
        assert status == 200, payload
        return payload["data"]["id"]

    def make_model(self, principal: str, name: str, weight_path: str | None = "/home/alice/models/m"):
        status, _headers, payload = self.request(
            "POST",
            "/api/v2/registry/models",
            principal=principal,
            body={"name": name, "weight_path": weight_path},
        )
        assert status == 200, payload
        return payload["data"]["id"]

    def make_deployment(self, principal: str, name: str, engine_id: str, model_id: str) -> str:
        status, _headers, payload = self.request(
            "POST",
            "/api/v2/registry/deployments",
            principal=principal,
            body={"name": name, "engine_id": engine_id, "model_id": model_id},
        )
        assert status == 200, payload
        deployment_id = payload["data"]["id"]
        status, _headers, payload = self.request(
            "POST", f"/api/v2/registry/deployments/{deployment_id}/verify", principal=principal
        )
        assert status == 200, payload
        return deployment_id

    def make_ready_deployment(self, principal: str, name: str, *, port: int = HEALTHY_PORT) -> str:
        """engine + model + deployment，并验证到 ``ready``。返回 deployment id。"""
        engine_id = self.make_engine(principal, f"{name}-engine", port=port)
        model_id = self.make_model(principal, f"{name}-model")
        return self.make_deployment(principal, f"{name}-deployment", engine_id, model_id)

    def publish_engine(
        self,
        engine_id: str | None = None,
        *,
        file: str = "/etc/llmlab/engines.env",
        var: str = "ENGINE_KEY",
    ) -> str:
        """让一个 engine 进入可发布状态（补 ``engine_secret_refs``），返回其 id。

        ``build_routing_snapshot`` 只发布**有 secret_ref** 的 engine；没有它的
        engine 会带着它的 endpoint 一起被排除。所以任何「一条真实路由被回执 /
        被 Gateway 执行」的测试都必须先调用这里 —— 否则它测的是一个**空快照**，
        而空快照上的 ``published=True`` 什么也证明不了。
        """
        if engine_id is None:
            engines = self.store.list_engines()
            assert engines, "publish_engine() 之前必须先登记一个 engine"
            engine_id = engines[0].id
        refs = dict(self.service.config.engine_secret_refs)
        refs[engine_id] = {"file": file, "var": var}
        self.service.config = dataclasses.replace(
            self.service.config, engine_secret_refs=refs
        )
        return engine_id

    def make_sibling_deployment(self, principal: str, endpoint_id: str, name: str) -> str:
        """在 Endpoint **当前**的 engine/model 上再建一个 ready deployment。

        repoint 的合法目标。刻意复用同一个 engine：``make_ready_deployment``
        每次会新建 engine，而新 engine 默认不在快照里 —— 拿它当 repoint 目标
        会让被测 Endpoint 恰好掉出快照，于是测试在证明一件别的事。
        """
        endpoint = self.store.endpoint_by_id(endpoint_id)
        assert endpoint is not None, f"unknown endpoint {endpoint_id}"
        source = self.store.deployment_by_id(endpoint.deployment_id)
        assert source is not None, f"endpoint {endpoint_id} has no deployment"
        return self.make_deployment(principal, name, source.engine_id, source.model_id)

    def confirm_route(
        self,
        endpoint_id: str,
        principal: str = "alice",
        *,
        router: object | None = None,
    ) -> int:
        """让这个 Endpoint 的当前路由被一次**真实回读**确认，返回新 revision。

        `invoke` 的路由门要求 ``router_sync_applied``（revision 与 snapshot SHA
        都由 Gateway 回读确认过）。新建与刚 repoint 的 Endpoint 都还没到那一步。
        这里走完整的一次确认：

        1. 补 engine 的 ``secret_ref`` —— 没有它 `build_routing_snapshot` 会排除
           整条路由，回执绑定检查会让 ACK 落空（见 PR #107）；
        2. 装一个 ``publish()`` 与 ``applied_revision()`` 一致的 Router 替身；
        3. repoint 到**同一 engine** 上的兄弟 deployment —— 这一步才触发
           ``_attempt_router_sync``，只有回执成功才会推到 applied。

        依赖旧行为（无 ACK 也 200）的测试应当改用它，而不是删掉或跳过断言。
        """
        from .test_trusted_gateway_and_router import ConfirmingRouterAdapter

        endpoint = self.store.endpoint_by_id(endpoint_id)
        assert endpoint is not None, f"unknown endpoint {endpoint_id}"
        deployment = self.store.deployment_by_id(endpoint.deployment_id)
        assert deployment is not None, f"endpoint {endpoint_id} has no deployment"
        self.publish_engine(deployment.engine_id)
        if router is None:
            router = ConfirmingRouterAdapter()
        self.service.router = router
        self._route_counter = getattr(self, "_route_counter", 0) + 1
        target = self.make_sibling_deployment(
            principal, endpoint_id, f"routed-{self._route_counter}"
        )
        status, _headers, payload = self.repoint(principal, endpoint_id, target)
        assert status == 200, payload
        assert payload["meta"]["router_sync"] == "router_sync_applied", payload["meta"]
        return int(payload["data"]["revision"])

    def acknowledge_route(self, endpoint_id: str, *, router: object | None = None) -> int:
        """把**当前** revision 记为一次 Gateway 回读确认，**不改变** deployment。

        用于「测试刚刚 repoint 到一个**指定**目标」之后：再 repoint 一次会换掉
        那个目标，测试就变成了在证明另一件事。这里写入的是
        ``_attempt_router_sync`` 在回执成功时写的**同一行**
        （``generation == revision``、``evidence=gateway_readback``），并装一个
        报告同一代数的 Router 替身。它模拟的是「本部署已经有过一次成功回读」，
        不是绕过回读逻辑。
        """
        from .test_trusted_gateway_and_router import ConfirmingRouterAdapter

        endpoint = self.store.endpoint_by_id(endpoint_id)
        assert endpoint is not None, f"unknown endpoint {endpoint_id}"
        deployment = self.store.deployment_by_id(endpoint.deployment_id)
        assert deployment is not None, f"endpoint {endpoint_id} has no deployment"
        self.publish_engine(deployment.engine_id)
        if router is None:
            router = _FixedReadbackRouter(endpoint.revision)
        self.service.router = router
        outcome = self.store.ack_router_generation(
            endpoint_id,
            generation=endpoint.revision,
            actor_principal="system:router-sync",
            actor_uid=0,
            at=self.service._now(),
            evidence="gateway_readback",
        )
        assert outcome.get("outcome") == "ok", outcome
        return endpoint.revision

    def make_endpoint_on(self, principal: str, name: str, deployment_id: str) -> str:
        """把 Endpoint 建到**指定的** deployment 上并验证。返回 endpoint id。"""
        status, _headers, payload = self.request(
            "POST",
            "/api/v2/registry/endpoints",
            principal=principal,
            body={"name": name, "deployment_id": deployment_id},
        )
        assert status == 200, payload
        endpoint_id = payload["data"]["id"]
        status, _headers, payload = self.request(
            "POST", f"/api/v2/registry/endpoints/{endpoint_id}/verify", principal=principal
        )
        assert status == 200, payload
        return endpoint_id

    def make_ready_endpoint(
        self, principal: str = "alice", *, name: str = "svc", port: int = HEALTHY_PORT
    ) -> str:
        """建到「Endpoint 已 ready、可共享」为止的完整链。"""
        deployment_id = self.make_ready_deployment(principal, name, port=port)
        return self.make_endpoint_on(principal, name, deployment_id)

    def endpoint_revision(self, principal: str, endpoint_id: str) -> int:
        """读当前 revision —— repoint 的 CAS 需要调用方先读一次。"""
        status, _headers, payload = self.request(
            "GET", f"/api/v2/registry/endpoints/{endpoint_id}", principal=principal
        )
        assert status == 200, payload
        return payload["data"]["revision"]

    def repoint(self, principal: str, endpoint_id: str, deployment_id: str, **extra):
        body = {"target_deployment_id": deployment_id, **extra}
        if "expected_revision" not in body:
            body["expected_revision"] = self.endpoint_revision(principal, endpoint_id)
        return self.request(
            "POST",
            f"/api/v2/registry/endpoints/{endpoint_id}/repoint",
            principal=principal,
            body=body,
        )

    def endpoints_visible_to(self, principal: str) -> list[dict]:
        status, _headers, payload = self.request(
            "GET", "/api/v2/registry/endpoints", principal=principal
        )
        assert status == 200, payload
        return payload["data"]["items"]
