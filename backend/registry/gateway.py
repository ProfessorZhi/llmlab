"""Gateway 边界：**只读配置契约**，或一个可注入的 mock adapter。

这条边界的形状是刻意的，来自平台简化设计里的一句话：

    「Gateway 只接收共享 Endpoint 请求」

因此本模块提供两样东西，且**只有**这两样：

1. :func:`build_routing_config` —— 一份**只读**的路由配置契约。它描述「哪些
   Endpoint 可以被路由、归谁、走哪个上游、对谁开放」。它是给运维/Gateway 读的
   **数据**，本仓库不把它写进任何生产 Gateway，也不签发任何 key。
2. :class:`GatewayAdapter` 协议 —— 真正把一次已授权的调用送出去的那个洞。
   本仓库提供**三种**实现，缺省永远是 fail-closed 的那一种：

   * :class:`DetachedGatewayAdapter`（**缺省**）：本部署没有接 Gateway，于是
     诚实地回 503。它不伪造 200。
   * :class:`MockGatewayAdapter`（**测试替身**）：确定性、无网络、显式标注合成。
   * :class:`TrustedGatewayAdapter`（**真实但可注入**）：把一次已授权的调用
     转发给一个**运维配置的** Gateway 基址（只允许回环、明确拒绝 :8080）。
     它**不**持有任何上游推理 key：调用者能不能调这个 endpoint 由 Gateway
     用自己保存的 key 记录独立判定（``server-side ACL``）；本模块只负责
     把**调用者自己的** Gateway key 交出去，绝不代替 Gateway 做这个判断。

没有任何实现会去读生产 Gateway 配置、或对 ``:8080``（上游推理端口）发起未授权
请求。``invoke`` 的每一次成功都意味着「服务端授权通过 + adapter 真的把请求送到
了某个地方」；没有 adapter，就没有成功的调用，也没有伪造的响应。

**为什么缺省不是** :class:`TrustedGatewayAdapter`：接一个 Gateway 是一次部署
决定，不是一次代码决定。没有运维显式配置基址与调用者 key 目录，就不该有任何
东西被送出去 —— 于是缺省是「没接」。
"""

from __future__ import annotations

import http.client
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Protocol

from . import urls as urls_mod
from .errors import AccessDenied, InvalidRequest, UpstreamUnavailable
from .models import Deployment, Endpoint, Engine, Model

#: 上游不可达/未接线时的稳定理由 token。
REASON_GATEWAY_NOT_ATTACHED = "gateway_not_attached"
REASON_UPSTREAM_ERROR = "upstream_error"
#: 调用者没有可用的 Gateway key —— 于是没有可信身份，一次都不转发。
REASON_GATEWAY_KEY_UNAVAILABLE = "gateway_key_unavailable"
#: Gateway 自己按 key 的 owner 判定「这个调用者不能调这个 endpoint」。
REASON_GATEWAY_ACL_DENIED = "gateway_acl_denied"
#: Gateway 的答复不是本模块认识的形状（非 JSON、缺字段、状态码语义不明）。
REASON_GATEWAY_PROTOCOL_ERROR = "gateway_protocol_error"
#: 连不上配置的 Gateway 基址。
REASON_GATEWAY_UNREACHABLE = "gateway_unreachable"


@dataclass(frozen=True)
class RoutingEntry:
    """一条路由契约条目。**只读**：本仓库不消费它去改任何地方。"""

    endpoint_id: str
    route_path: str
    owner_principal: str
    owner_uid: int
    protocol: str
    model_name: str
    upstream_host: str
    upstream_port: int
    upstream_path: str
    visibility: str
    grantee_count: int
    ready: bool

    def to_dict(self) -> dict:
        return {
            "endpoint_id": self.endpoint_id,
            "route_path": self.route_path,
            "owner": self.owner_principal,
            "owner_uid": self.owner_uid,
            "protocol": self.protocol,
            "model": self.model_name,
            "upstream": {
                "host": self.upstream_host,
                "port": self.upstream_port,
                "path": self.upstream_path,
            },
            "visibility": self.visibility,
            "grantee_count": self.grantee_count,
            "ready": self.ready,
            # 这份契约不含任何凭据：Gateway 的 key 由 Gateway 自己管。
            "credentials": None,
        }


def build_routing_config(
    endpoints: list[Endpoint],
    deployments: dict[str, Deployment],
    engines: dict[str, Engine],
    models: dict[str, Model],
    grantee_counts: dict[str, int],
) -> dict:
    """只读路由契约。**只包含已 ready 且非 private 的 Endpoint**。

    两个过滤条件都是刻意的：

    * 未 ``ready`` 的上游可能根本不在监听 —— 把它写进路由配置会让 Gateway 把
      流量送到一个空洞；
    * ``private`` 的 Endpoint 没有「可以被路由给谁」的语义，写进去只是噪声，
      还会让这份配置本身成为一份「谁注册了什么」的清单。
    """
    entries: list[RoutingEntry] = []
    for endpoint in sorted(endpoints, key=lambda item: item.route_path):
        deployment = deployments.get(endpoint.deployment_id)
        if deployment is None:
            continue
        engine = engines.get(deployment.engine_id)
        model = models.get(deployment.model_id)
        if engine is None or model is None:
            continue
        target = engine.target or {}
        entries.append(
            RoutingEntry(
                endpoint_id=endpoint.id,
                route_path=endpoint.route_path,
                owner_principal=endpoint.owner_principal,
                owner_uid=endpoint.owner_uid,
                protocol=engine.protocol,
                model_name=model.name,
                upstream_host=str(target.get("host") or ""),
                upstream_port=int(target.get("port") or 0),
                upstream_path=str(target.get("path") or "/"),
                visibility=endpoint.visibility,
                grantee_count=int(grantee_counts.get(endpoint.id, 0)),
                ready=endpoint.shareable,
            )
        )
    routable = [entry for entry in entries if entry.ready and entry.visibility != "private"]
    return {
        "contract": "registry-routing/1",
        "read_only": True,
        "entries": [entry.to_dict() for entry in routable],
        "excluded_endpoints": len(entries) - len(routable),
        "notes": [
            "本文件是只读契约，由 Registry 生成；本仓库不写入任何 Gateway。",
            "不含任何凭据：per-user key 的签发与校验属于 Gateway。",
        ],
    }


class GatewayAdapter(Protocol):
    """把一次**已授权**的调用送出去。实现必须自己保证不再做身份判定。

    ``requester`` 是**服务端凭据推出**的调用者上下文（见 ``identity``），由
    ``RegistryService.invoke`` 传入。它**不是**客户端自报的东西。只有需要
    「调用者自己的 Gateway key」的 adapter（:class:`TrustedGatewayAdapter`）会
    用它；确定性替身忽略它即可。加这个参数是**只增**的：既有 adapter 给
    ``requester=None`` 一个缺省就仍然能构造与调用（``backend/bff`` 不动）。
    """

    name: str

    def forward(
        self,
        *,
        endpoint: Endpoint,
        deployment: Deployment,
        engine: Engine,
        model: Model,
        payload: dict,
        requester: object | None = None,
    ) -> dict:  # pragma: no cover - protocol
        ...


class DetachedGatewayAdapter:
    """本部署**没有**接 Gateway 时的默认 adapter：诚实地说「没接」。

    它不伪造一个 200 响应，也不假装调用成功了。``invoke`` 会因此返回
    ``503 registry.upstream_unavailable``，理由 ``gateway_not_attached`` ——
    这正是「no fake results in live paths」在调用路径上的样子。
    """

    name = "detached"
    attached = False

    def forward(
        self,
        *,
        endpoint: Endpoint,
        deployment: Deployment,
        engine: Engine,
        model: Model,
        payload: dict,
        requester: object | None = None,
    ) -> dict:
        raise UpstreamUnavailable(
            f"{REASON_GATEWAY_NOT_ATTACHED}: no gateway adapter is wired in this "
            f"deployment (host would have been {engine.target.get('host')}:"
            f"{engine.target.get('port')})"
        )


@dataclass
class MockGatewayAdapter:
    """**测试替身**。确定性、无网络、并把每次调用记下来供断言。

    ``synthetic`` 恒为 ``True``，且结果里显式标注 —— 一个 mock 的答复永远不能被
    当成「真实推理可用」的证据。
    """

    name = "mock"
    attached = True
    calls: list[dict] = field(default_factory=list)
    failing: bool = False
    degraded: bool = False

    def forward(
        self,
        *,
        endpoint: Endpoint,
        deployment: Deployment,
        engine: Engine,
        model: Model,
        payload: dict,
        requester: object | None = None,
    ) -> dict:
        if not isinstance(payload, dict):
            raise InvalidRequest("invoke payload must be an object")
        record = {
            "endpoint_id": endpoint.id,
            "deployment_id": deployment.id,
            "engine_id": engine.id,
            "model_id": model.id,
            "model": model.name,
            "route_path": endpoint.route_path,
            "payload_keys": sorted(payload.keys()),
        }
        self.calls.append(record)
        if self.failing:
            raise UpstreamUnavailable(f"{REASON_UPSTREAM_ERROR}: mock adapter is set to fail")
        return {
            "synthetic": True,
            "gateway": self.name,
            "forwarded": True,
            "model": model.name,
            "endpoint_id": endpoint.id,
            "degraded": bool(self.degraded),
            # 刻意**不**给出 token 数 / 延迟：mock 没有量过它们，
            # 编一个数出来正是这个仓库反复拒绝的那种行为。
            "tokens": None,
            "latency_ms": None,
            "echo": {"payload_keys": sorted(payload.keys())},
            "notes": [
                "MOCK: 这条答复由测试替身生成，不是真实推理结果，也不是可用性证据。"
            ],
        }


#: 一次转发最多读回的响应体字节数。上游不能用一个巨大正文把本进程的内存吃干。
MAX_GATEWAY_RESPONSE_BYTES = 1024 * 1024


class GatewayKeyResolver(Protocol):
    """``principal -> 该调用者自己的 Gateway key``。

    key 是**不透明**的：本模块不解析它、不校验它、也不派生它。它由运维在
    Gateway 侧登记（``admin key create <label> --owner <principal>``），这里只是
    把调用者的那一把找出来。目录里没有就是「没有」，**没有兜底、没有共用 key**。
    """

    name: str

    def resolve(self, principal: str) -> str | None:  # pragma: no cover - protocol
        ...


class NoGatewayKeys:
    """缺省：本部署**没有**任何调用者 key 目录。于是谁都转发不出去。"""

    name = "none"

    def resolve(self, principal: str) -> str | None:
        return None


@dataclass(frozen=True)
class StaticGatewayKeyResolver:
    """**测试替身**：一份进程内的 ``principal -> key`` 映射。

    与 ``StaticProber`` / ``StaticOwnershipProver`` 同一套纪律：它放在产品模块里
    被显式标注（``name`` 进 DTO），而不是让测试自造一个容易被误当成真实目录的东西。
    """

    keys: dict
    name: str = "static_test_double"

    def __post_init__(self) -> None:
        if not isinstance(self.keys, dict):
            raise InvalidRequest("keys must be a mapping")
        for principal, key in self.keys.items():
            if not isinstance(principal, str) or not principal:
                raise InvalidRequest("key directory principals must be non-empty strings")
            if not isinstance(key, str) or not key.strip():
                raise InvalidRequest(f"gateway key for {principal!r} must be a non-empty string")

    def resolve(self, principal: str) -> str | None:
        return self.keys.get(principal)


def _load_gateway_key_file(path: str) -> dict:
    """读一份 ``{principal: gateway_key}`` 的 JSON 文件，**只接受**安全形态。

    与 CLI 读 token 文件同一套纪律：属主本人的 0600 普通文件、不是软链。
    与 token 不同的是：这里一次读**多把** key（每行一个调用者），因此更要紧 ——
    一份 0644 的 key 目录等于把所有人的 Gateway key 摊在磁盘上。
    """
    import json as _json
    import os as _os
    import stat as _stat

    try:
        info = _os.lstat(path)
    except OSError as exc:
        raise InvalidRequest(f"gateway key file is unreadable: {type(exc).__name__}") from exc
    if _stat.S_ISLNK(info.st_mode):
        raise InvalidRequest("gateway key file must not be a symbolic link")
    if not _stat.S_ISREG(info.st_mode):
        raise InvalidRequest("gateway key file must be a regular file")
    if info.st_uid != _os.getuid():
        raise InvalidRequest("gateway key file must be owned by the current user")
    if info.st_mode & (_stat.S_IRWXG | _stat.S_IRWXO):
        raise InvalidRequest(
            "gateway key file must not be readable or writable by group/other"
        )
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
    except OSError as exc:
        raise InvalidRequest(f"gateway key file is unreadable: {type(exc).__name__}") from exc
    try:
        parsed = _json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise InvalidRequest("gateway key file is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise InvalidRequest("gateway key file must be a JSON object")
    keys: dict = {}
    for principal, key in parsed.items():
        if not isinstance(principal, str) or not principal:
            raise InvalidRequest("gateway key file principals must be non-empty strings")
        if not isinstance(key, str) or not key.strip():
            raise InvalidRequest(f"gateway key for {principal!r} must be a non-empty string")
        keys[principal] = key
    return keys


@dataclass(frozen=True)
class FileGatewayKeyResolver:
    """从一份 **安全的** JSON 文件读 ``principal -> Gateway key``（真实目录，非替身）。

    加载失败会**抛错**，不会退化成「空目录」：一个「悄悄没读到 key」的部署会表现为
    「所有调用都 503」，而真相其实是配置没读到 —— 那正是要避免的静默失败。
    """

    path: str
    name: str = "file_gateway_keys"

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise InvalidRequest("path must be a non-empty string")
        # key 目录是运维配置，不是热加载的东西：构造时读一次。
        object.__setattr__(self, "_keys", _load_gateway_key_file(self.path))

    @property
    def principals(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))  # type: ignore[attr-defined]

    def resolve(self, principal: str) -> str | None:
        return self._keys.get(principal)  # type: ignore[attr-defined]


def _normalize_gateway_base(raw: object) -> tuple[str, str, int]:
    """校验一个**运维配置的** Gateway 基址，返回 ``(origin, host, port)``。

    这条防线与 engine 目标那条（``urls.validate_engine_target_url``）是**两件事**，
    所以它在这里重写而不是复用：Gateway 基址不是「用户登记的 engine」，它由运维
    写进部署配置，约束也因此更窄：

    * 只允许 ``http``（回环上的明文；``https`` 与任何远端地址一律拒绝）；
    * 主机必须是回环字面量（``127.0.0.1`` / ``localhost`` / ``::1``）；
    * 端口不得是 :data:`urls.RESERVED_UPSTREAM_PORTS`（``8080``）——
      网关是**唯一**该跟上游说话的东西，Registry 绝不直连它；
    * 不得带 userinfo / query / fragment / 非空路径。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidRequest("gateway base url must be a non-empty string")
    candidate = raw.strip()
    if len(candidate) > urls_mod.MAX_URL_LENGTH:
        raise InvalidRequest("gateway base url is too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise InvalidRequest("gateway base url contains control characters")

    parsed = urls_mod.urlsplit(candidate)
    if parsed.scheme != "http":
        raise InvalidRequest(
            f"gateway base url must be http on loopback, not {parsed.scheme or 'schemeless'!r}"
        )
    if parsed.username or parsed.password:
        raise InvalidRequest("gateway base url must not carry credentials")
    if parsed.query or parsed.fragment:
        raise InvalidRequest("gateway base url must not carry a query or fragment")
    if parsed.path not in ("", "/"):
        raise InvalidRequest("gateway base url must be an origin (no path)")

    host = (parsed.hostname or "").strip()
    if not host or not urls_mod.is_loopback_host(host):
        raise InvalidRequest(f"gateway base url must point at loopback, not {host!r}")
    try:
        port = parsed.port
    except ValueError as exc:  # 端口不是数字 / 越界
        raise InvalidRequest("gateway base url has an invalid port") from exc
    if port is None:
        port = 80
    if port in urls_mod.RESERVED_UPSTREAM_PORTS:
        raise InvalidRequest(
            f"gateway base url must not use reserved upstream port {port}; "
            "the Registry never talks to the inference upstream directly"
        )
    origin = f"http://{host}:{port}"
    return origin, host, port


@dataclass
class TrustedGatewayAdapter:
    """把一次**已授权**的调用转发给一个运维配置的 Gateway（**唯一**的出口）。

    与 :class:`MockGatewayAdapter` 的区别是这条路径是**真的**：它打开一个到
    ``base_url`` 的 TCP 连接并把调用送出去。与「直连上游」的区别是它**只**连
    这一个基址：``engine.target``（用户登记里那个 host:port）在这条路径上**根本不
    被读取**，因此浏览器侧或调用方都不可能借它把流量引到 ``:8080`` 或别的服务上。

    三条 fail-closed 规则，缺任何一条就**一次都不转发**：

    1. 没有配置基址 -> 这个类根本构造不出来（:func:`_normalize_gateway_base`）；
    2. 调用者没有自己的 Gateway key -> ``503 gateway_key_unavailable``；
    3. Gateway 按 key 的 owner 拒绝 -> 把 Gateway 的 **403** 如实翻成
       ``gateway_acl_denied``，**不**降级成本地放行。

    第 3 条是「server-side ACL」在代码里的样子：最终能不能调这个 endpoint，
    由 Gateway 用它自己保存的 key 记录判定，本模块无权替它说「可以」。
    """

    base_url: str
    keys: GatewayKeyResolver = field(default_factory=NoGatewayKeys)
    timeout: float = 2.0
    invoke_path: str = "/v1/chat/completions"
    name: str = "trusted_http"
    attached: bool = True

    def __post_init__(self) -> None:
        origin, host, port = _normalize_gateway_base(self.base_url)
        object.__setattr__(self, "base_url", origin)
        self._host = host
        self._port = port
        if not hasattr(self.keys, "resolve") or not callable(self.keys.resolve):
            raise InvalidRequest("keys must implement resolve(principal)")
        if not (0 < float(self.timeout) <= 10):
            raise InvalidRequest("timeout must be in (0, 10]")
        if not isinstance(self.invoke_path, str) or not self.invoke_path.startswith("/"):
            raise InvalidRequest("invoke_path must be an absolute path")

    def resolve_key(self, requester: object | None) -> str | None:
        principal = getattr(requester, "principal", None)
        if not isinstance(principal, str) or not principal:
            return None
        return self.keys.resolve(principal)

    def forward(
        self,
        *,
        endpoint: Endpoint,
        deployment: Deployment,
        engine: Engine,
        model: Model,
        payload: dict,
        requester: object | None = None,
    ) -> dict:
        if not isinstance(payload, dict):
            raise InvalidRequest("invoke payload must be an object")

        key = self.resolve_key(requester)
        if not key:
            raise UpstreamUnavailable(
                f"{REASON_GATEWAY_KEY_UNAVAILABLE}: no gateway key is registered for the "
                "caller, so no trusted identity can be presented to the gateway"
            )

        # 服务端已经用 Registry 的行级 ACL 选定了这个 endpoint；`model` 因此由
        # **服务端**钉成这条 Endpoint 的稳定 alias，而不是让调用方在载荷里选。
        # 上游不是调用方能挑的（Gateway 侧再按 alias 映射成引擎内部 model_id）。
        body = dict(payload)
        body["model"] = endpoint.route_path
        encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        request_id = f"req_{secrets.token_hex(8)}"
        started = time.monotonic()
        connection = http.client.HTTPConnection(self._host, self._port, timeout=float(self.timeout))
        try:
            connection.request(
                "POST",
                self.invoke_path,
                body=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                    "X-Request-Id": request_id,
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_GATEWAY_RESPONSE_BYTES)
            status = int(response.status)
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamUnavailable(
                f"{REASON_GATEWAY_UNREACHABLE}: gateway at {self.base_url} did not answer "
                f"({type(exc).__name__})"
            ) from exc
        finally:
            connection.close()

        elapsed_ms = round((time.monotonic() - started) * 1000, 3)

        if status in (401, 403):
            # Gateway 自己按 key 的 owner 判定「你不能调这个 endpoint」。
            # 这一条**不**降级：Gateway 说不，就是不。
            raise AccessDenied(
                f"{REASON_GATEWAY_ACL_DENIED}: the gateway refused this call for the "
                f"caller's key (http {status})"
            )
        if status >= 500:
            raise UpstreamUnavailable(
                f"{REASON_UPSTREAM_ERROR}: gateway answered {status}"
            )
        if not (200 <= status < 300):
            raise UpstreamUnavailable(
                f"{REASON_GATEWAY_PROTOCOL_ERROR}: gateway answered {status}"
            )

        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamUnavailable(
                f"{REASON_GATEWAY_PROTOCOL_ERROR}: gateway response was not JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise UpstreamUnavailable(
                f"{REASON_GATEWAY_PROTOCOL_ERROR}: gateway response was not a JSON object"
            )

        usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else None
        tokens = None
        if usage is not None:
            total = usage.get("total_tokens")
            tokens = total if isinstance(total, int) and not isinstance(total, bool) else None

        return {
            # 合成与否**照抄 Gateway 自己说的**：一个真实 Gateway 不会标 synthetic，
            # 而一个测试替身会。本模块不替它下这个结论。
            "synthetic": bool(parsed.get("synthetic", False)),
            "gateway": self.name,
            "forwarded": True,
            "served_by": "gateway",
            "request_id": request_id,
            "http_status": status,
            "model": endpoint.route_path,
            "body": parsed,
            # usage / tokens 只在 Gateway 真的给了它们的时候才有值；缺就是 None，
            # 不编数（与 Mock 的纪律一致）。
            "usage": usage,
            "tokens": tokens,
            "client_latency_ms": elapsed_ms,
            "notes": [
                "这条答复来自配置的 Gateway；Registry 只做了行级 ACL 与转发。",
                "上游推理端口不由本模块直连：engine.target 在这条路径上不被读取。",
            ],
        }
