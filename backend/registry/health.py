"""健康与验证：**没有观测就是 UNKNOWN**，不是 0，也不是「大概正常」。

仓库里已经有一条反复出现的纪律（``backend/portal/modules.py``）：
「没有可信来源，就是 ``null`` + ``unknown`` 而不是 ``0``」。本模块把它落到
Registry 的 engine / deployment / endpoint 上。

两条不可退让的规则：

1. **未观测 -> UNKNOWN。** 没有 prober（本部署没有配置任何观测源）时，
   ``state`` 是 ``unknown``、``value`` 是 ``None``、``reason`` 是稳定的契约 token。
   绝不用「反正刚才注册成功了，就算 healthy 吧」顶替。
2. **验证必须真的发生过。** ``verified`` 是「一次真实的、同主机 loopback 的探测
   成功过」的记录，不是「用户说他跑起来了」。因此本模块把 prober 做成**注入项**：
   不注入，就没有任何东西能变成 verified。

``LoopbackHttpProber`` 是唯一会真正打开 socket 的实现，它只用**已经过
``urls.validate_engine_target_url`` 归一化**的目标，并在连接前再查一次 loopback ——
两层，因为这两件事（校验、连接）在任何一次重构里都可能被分开。
"""

from __future__ import annotations

import http.client
import json
import time
from dataclasses import dataclass
from typing import Protocol

from .errors import InvalidRequest
from .urls import NormalizedTarget, is_loopback_host

# --- 状态取值域（与 backend.portal.modules 的词汇表保持一致）---------------

STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"
STATE_UNREACHABLE = "unreachable"
STATE_UNKNOWN = "unknown"
HEALTH_STATES = (STATE_HEALTHY, STATE_DEGRADED, STATE_UNREACHABLE, STATE_UNKNOWN)

#: 观测来源。``none`` 表示**结构性**没有来源 —— 不是「这次没测到」。
SOURCE_PROBE = "loopback_probe"
SOURCE_NONE = "none"

#: 稳定的拒绝/缺省理由 token（会原样进 DTO，供前端直接展示）。
REASON_NOT_CONFIGURED = "not_configured_in_this_deployment"
REASON_NO_VERIFIED_SOURCE = "no_verified_metric_source"
REASON_NEVER_PROBED = "never_probed"
REASON_PROBE_FAILED = "probe_failed"
REASON_PROBE_OK = "probe_succeeded"
REASON_PROBE_TIMEOUT = "probe_timed_out"

#: 严重度排序。``unknown`` 刻意**不在**表内：它不是一个「比较级」，
#: 而是「没有数据」，不参与取最坏值（见 :func:`aggregate_health`）。
_SEVERITY = {STATE_HEALTHY: 0, STATE_DEGRADED: 1, STATE_UNREACHABLE: 2}


def unknown(reason: str = REASON_NOT_CONFIGURED) -> dict:
    """一个诚实的 UNKNOWN：``value`` 为 ``None``，并带上原因。"""
    return {
        "state": STATE_UNKNOWN,
        "source": SOURCE_NONE,
        "observed_at": None,
        "latency_ms": None,
        "reason": reason,
        "detail": None,
        "verified": False,
    }


def unavailable(reason: str = REASON_NO_VERIFIED_SOURCE) -> dict:
    """结构性不可用（本部署没有这条来源）。与 ``unknown`` 同形，语义更硬。"""
    return unknown(reason)


@dataclass(frozen=True)
class ProbeResult:
    """一次探测的**原始事实**。``latency_ms`` 只有在真的量过时才有值。"""

    reachable: bool
    latency_ms: float | None = None
    detail: str | None = None
    degraded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.reachable, bool):
            raise InvalidRequest("ProbeResult.reachable must be a bool")
        if self.reachable and self.latency_ms is not None and self.latency_ms < 0:
            raise InvalidRequest("latency_ms must not be negative")
        if not self.reachable and self.latency_ms is not None:
            # 不可达却报出一个延迟 —— 这正是要防的伪造。
            raise InvalidRequest("an unreachable probe must not report a latency")


@dataclass(frozen=True)
class HealthObservation:
    """健康观测的规范化形态。"""

    state: str
    source: str
    observed_at: str | None
    latency_ms: float | None
    reason: str | None
    detail: str | None
    verified: bool

    def __post_init__(self) -> None:
        if self.state not in HEALTH_STATES:
            raise InvalidRequest(f"unknown health state: {self.state!r}")
        if self.state == STATE_UNKNOWN and self.latency_ms is not None:
            raise InvalidRequest("an unknown observation must not carry a latency")
        if self.state in (STATE_UNREACHABLE,) and self.latency_ms is not None:
            raise InvalidRequest("an unreachable observation must not carry a latency")

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "source": self.source,
            "observed_at": self.observed_at,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
            "detail": self.detail,
            "verified": self.verified,
        }


def observation_from_probe(
    result: ProbeResult, *, observed_at: str, source: str = SOURCE_PROBE
) -> HealthObservation:
    """把一次探测结果变成观测。**只有**可达时才可能带延迟。"""
    if not isinstance(result, ProbeResult):
        raise InvalidRequest("result must be a ProbeResult")
    if not result.reachable:
        return HealthObservation(
            state=STATE_UNREACHABLE,
            source=source,
            observed_at=observed_at,
            latency_ms=None,
            reason=REASON_PROBE_FAILED,
            detail=result.detail,
            verified=False,
        )
    return HealthObservation(
        state=STATE_DEGRADED if result.degraded else STATE_HEALTHY,
        source=source,
        observed_at=observed_at,
        latency_ms=result.latency_ms,
        reason=REASON_PROBE_OK,
        detail=result.detail,
        # ``verified`` 的含义是「**探测到了它**」，与资源 status 的 ``verified``
        # 同一件事。质量差异由 ``state`` 承载（healthy vs degraded）——
        # 否则会出现「status 说 verified、health 说 verified=false」这种自相矛盾的载荷。
        verified=result.reachable,
    )


class Prober(Protocol):
    """探测一个已归一化的 loopback 目标。实现必须只连 loopback。"""

    def probe(self, target: NormalizedTarget) -> ProbeResult:  # pragma: no cover - protocol
        ...


class LoopbackHttpProber:
    """唯一会真正打开 socket 的 prober。只连 ``127.0.0.1`` / ``::1``。

    路径固定为 ``target.path``，方法 ``GET``，超时显式且很短。不跟随重定向
    （一个 3xx 也可能把请求带出回环，而 http.client 不自动跟随，正是我们要的）。
    """

    def __init__(self, *, timeout_seconds: float = 1.5) -> None:
        if not (0 < float(timeout_seconds) <= 10):
            raise InvalidRequest("timeout_seconds must be in (0, 10]")
        self._timeout = float(timeout_seconds)

    def probe(self, target: NormalizedTarget) -> ProbeResult:
        if not isinstance(target, NormalizedTarget):
            raise InvalidRequest("target must be a NormalizedTarget")
        # 第二层：即便拿到了一个 NormalizedTarget，也再确认一次它确实在回环。
        if not is_loopback_host(target.host):
            raise InvalidRequest(f"refusing to probe non-loopback host {target.host!r}")

        started = time.monotonic()
        connection = http.client.HTTPConnection(target.host, target.port, timeout=self._timeout)
        try:
            path = target.path + (f"?{target.query}" if target.query else "")
            connection.request("GET", path)
            response = connection.getresponse()
            response.read(0)  # 只看状态行，不读正文
            elapsed_ms = round((time.monotonic() - started) * 1000, 3)
            if 200 <= response.status < 300:
                return ProbeResult(reachable=True, latency_ms=elapsed_ms)
            if 300 <= response.status < 400:
                # 一个把调用者往外送的重定向不算「健康」。
                return ProbeResult(
                    reachable=False,
                    detail=f"redirect ({response.status}) is not accepted",
                )
            if response.status in (401, 403):
                # 有东西在监听并且拒绝了 —— 服务活着，但接口形态不对。
                return ProbeResult(
                    reachable=True,
                    latency_ms=elapsed_ms,
                    degraded=True,
                    detail=f"service answered {response.status}",
                )
            return ProbeResult(
                reachable=True,
                latency_ms=elapsed_ms,
                degraded=True,
                detail=f"service answered {response.status}",
            )
        except (OSError, http.client.HTTPException) as exc:
            return ProbeResult(reachable=False, detail=type(exc).__name__)
        finally:
            connection.close()


class StaticProber:
    """**测试替身**：按端口给出预置结果。

    刻意放在产品模块里而不是测试目录：它是「本部署没有 prober」之外唯一被允许的
    另一种确定性来源，把它显式标注为替身（``name`` 字段进 DTO 的 ``source``）
    比让测试自造一个更不容易被误当成真实观测。
    """

    NAME = "static_test_double"

    def __init__(self, results: dict[int, ProbeResult] | None = None) -> None:
        self._results = dict(results or {})

    def set(self, port: int, result: ProbeResult) -> None:
        self._results[int(port)] = result

    def probe(self, target: NormalizedTarget) -> ProbeResult:
        return self._results.get(
            target.port,
            ProbeResult(reachable=False, detail="no static result for this port"),
        )


def aggregate_health(observations: list[HealthObservation], *, subject: str = "deployment") -> dict:
    """把多条观测折成一条。**unknown 不参与取最坏值**，它只是「没有数据」。

    规则（顺序即语义）：

    1. 一条观测量都没有 —— 或**全部**是 unknown —— 结果为 ``unknown``；
    2. 否则取已观测项里最严重的一级；
    3. 特例：最严重是 ``unreachable`` 但仍有别的项是 healthy/degraded，
       结果为 ``degraded`` —— 一部分服务活着，与「全挂」是两种事实。
    """
    if not isinstance(observations, list):
        raise InvalidRequest("observations must be a list")
    for item in observations:
        if not isinstance(item, HealthObservation):
            raise InvalidRequest("observations must contain HealthObservation values")

    observed = [item for item in observations if item.state != STATE_UNKNOWN]
    if not observed:
        return {
            "subject": subject,
            "state": STATE_UNKNOWN,
            "reason": REASON_NO_VERIFIED_SOURCE,
            "observed_sources": 0,
            "total": len(observations),
            "worst_of": None,
            "latency_ms": None,
        }

    worst = max(observed, key=lambda item: _SEVERITY[item.state])
    states = {item.state for item in observed}
    state = worst.state
    if state == STATE_UNREACHABLE and (states & {STATE_HEALTHY, STATE_DEGRADED}):
        state = STATE_DEGRADED

    latencies = [item.latency_ms for item in observed if item.latency_ms is not None]
    return {
        "subject": subject,
        "state": state,
        "reason": worst.reason,
        "observed_sources": len({item.source for item in observed}),
        "total": len(observations),
        "worst_of": worst.to_dict(),
        "latency_ms": max(latencies) if latencies else None,
    }


def metric(
    value: float | int | None,
    *,
    state: str | None = None,
    source: str | None = None,
    unit: str = "count",
    label: str | None = None,
) -> dict:
    """一个有来源的指标，或一个诚实的 ``unknown``（同 ``portal.modules.metric``）。"""
    if value is None:
        return {
            "value": None,
            "state": STATE_UNKNOWN,
            "source": None,
            "unit": unit,
            "label": label,
        }
    resolved_state = state or STATE_HEALTHY
    if resolved_state not in HEALTH_STATES:
        raise InvalidRequest(f"unknown metric state: {resolved_state!r}")
    if resolved_state == STATE_UNKNOWN:
        # 「未知但有个值」是自相矛盾的组合，直接拒绝而不是产出它。
        raise InvalidRequest("an unknown metric must not carry a value")
    if source is None:
        raise InvalidRequest("a measured metric must declare its source")
    return {
        "value": value,
        "state": resolved_state,
        "source": source,
        "unit": unit,
        "label": label,
    }


# ---------------------------------------------------------------------------
# 第二根轴：**协议**验证（与上面的「传输可达」分开）
# ---------------------------------------------------------------------------
#
# 分开的理由是一次具体的失手：原来的 ``LoopbackHttpProber`` 只要拿到 2xx 就把
# engine 判成 ``healthy``，于是**任何一个**在回环端口上应答 200 的东西
# （一个静态文件服务器、一个别的用户的服务、一个随便的 daemon）都能被登记成
# 「模型就绪」。2xx 证明的是「有个东西在监听」，不是「这是个模型服务」。
#
# 所以这里再加一根轴，而且它只走**白名单路径**：

#: 协议验证的结论。
PROTOCOL_VERIFIED = "verified"
PROTOCOL_UNVERIFIED = "unverified"
PROTOCOL_UNKNOWN = "unknown"
PROTOCOL_STATES = (PROTOCOL_VERIFIED, PROTOCOL_UNVERIFIED, PROTOCOL_UNKNOWN)

#: 稳定理由 token。
REASON_MODEL_LIST_VERIFIED = "model_list_verified"
REASON_MODEL_LIST_MISSING = "model_list_missing"
REASON_HEALTH_ONLY_NO_MODEL_IDENTITY = "health_path_is_not_model_identity"
REASON_NO_MODEL_IDENTITY_FOR_PROTOCOL = "protocol_has_no_model_identity"
REASON_TRANSPORT_UNREACHABLE = "transport_unreachable"

#: 每个声明协议允许探测的**只读、非敏感**路径，按顺序尝试。
#:
#: 刻意是固定表而不是「用用户给的 path」：否则「协议验证」就变成了一个可以指向
#: 本机任意路径的探测器。这些路径不携带 prompt、不携带凭据、只读。
#:
#: 只有 ``/v1/models`` / ``/models`` 这一族能给出**模型身份**（一个模型 id 列表），
#: 因此只有它们能把协议判成 ``verified``。health 类路径最多给到「有个东西在应答」——
#: 那不足以说明它是个模型服务（见 :data:`ALLOWED_HEALTH_PATHS`）。
ALLOWED_MODEL_LIST_PATHS = ("/v1/models", "/models")

#: 各协议的「模型身份」候选路径。空元组 = 该协议**没有**可验证的模型身份。
PROTOCOL_MODEL_IDENTITY_PATHS: dict[str, tuple[str, ...]] = {
    "openai_compatible": ALLOWED_MODEL_LIST_PATHS,
    "http_json": (),
}

#: health 类路径。它们**不**足以判定 ``verified``，只作为「有东西在应答」的旁证。
ALLOWED_HEALTH_PATHS = ("/healthz", "/health", "/v1/health", "/readyz")

#: 读取响应体的字节上限。模型清单可能很大，但没必要全读进来。
MAX_PROTOCOL_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProtocolObservation:
    """协议验证的结论。``state`` 与传输轴的 ``state`` **是两件事**。"""

    state: str
    reason: str
    protocol: str
    checked_path: str | None = None
    http_status: int | None = None
    model_ids: int | None = None
    observed_at: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.state not in PROTOCOL_STATES:
            raise InvalidRequest(f"unknown protocol state: {self.state!r}")
        if self.state == PROTOCOL_UNKNOWN and self.http_status is not None:
            # 「没测」却带着一个状态码，是自相矛盾的载荷。
            raise InvalidRequest("an unknown protocol observation must not carry a status")
        if self.model_ids is not None and self.state != PROTOCOL_VERIFIED:
            # 只有验证通过时才可能数出模型，否则那个数字是编的。
            raise InvalidRequest("model_ids may only be set when the protocol is verified")

    @property
    def verified(self) -> bool:
        return self.state == PROTOCOL_VERIFIED

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "verified": self.verified,
            "protocol": self.protocol,
            "checked_path": self.checked_path,
            "http_status": self.http_status,
            "model_ids": self.model_ids,
            "observed_at": self.observed_at,
            "source": self.source,
        }


def unverified_protocol(reason: str, *, protocol: str = "unknown") -> ProtocolObservation:
    return ProtocolObservation(state=PROTOCOL_UNVERIFIED, reason=reason, protocol=protocol)


def unknown_protocol(reason: str = REASON_NOT_CONFIGURED, *, protocol: str = "unknown") -> ProtocolObservation:
    return ProtocolObservation(state=PROTOCOL_UNKNOWN, reason=reason, protocol=protocol)


def parse_model_list(raw: bytes) -> int | None:
    """从一个 ``/v1/models`` 响应里读出「模型数量」，读不出就返回 ``None``。

    接受两种形状（都是 OpenAI 兼容服务真实会返回的）：

    * ``{"object": "list", "data": [{"id": "..."}, ...]}``
    * ``{"data": [{"id": "..."}, ...]}``

    必须**每一条都有 ``id``**：只看到一个空数组 ``[]`` 也算通过（那说明它确实
    是一个会说模型清单的服务，只是当前没有模型），但一个没有 ``data`` 数组的
    任意 JSON 不算。
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    object_kind = payload.get("object")
    if object_kind is not None and object_kind != "list":
        return None
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            return None
    return len(data)


class ProtocolProber(Protocol):
    """判定「这个回环目标是不是这个协议下的模型服务」。"""

    name: str

    def probe(  # pragma: no cover - protocol
        self, target: NormalizedTarget, protocol: str
    ) -> ProtocolObservation:
        ...


class NoProtocolProber:
    """默认实现：本部署没有协议验证来源。结论是 ``unknown`` —— 不是 ``verified``。"""

    name = "none"

    def probe(self, target: NormalizedTarget, protocol: str) -> ProtocolObservation:
        return unknown_protocol(REASON_NOT_CONFIGURED, protocol=protocol)


class StaticProtocolProber:
    """**测试替身**：按 (host, port) 预置协议结论。"""

    name = "static_test_double"

    def __init__(self, observations: dict[tuple[str, int], ProtocolObservation] | None = None) -> None:
        self._observations = dict(observations or {})

    def set(self, host: str, port: int, observation: ProtocolObservation) -> None:
        self._observations[(host, int(port))] = observation

    def probe(self, target: NormalizedTarget, protocol: str) -> ProtocolObservation:
        found = self._observations.get((target.host, target.port))
        if found is None:
            return ProtocolObservation(
                state=PROTOCOL_UNKNOWN,
                reason=REASON_NOT_CONFIGURED,
                protocol=protocol,
                source=self.name,
            )
        return found


class LoopbackProtocolProber:
    """真的去读一次模型清单。**只读**、无 prompt、无凭据、路径来自白名单。

    流程：在目标的 ``scheme://host:port`` 基址上依次试
    :data:`PROTOCOL_MODEL_IDENTITY_PATHS` 里的路径，第一个能解析出模型清单的
    即为 ``verified``；全部失败则 ``unverified``。
    """

    def __init__(self, *, timeout_seconds: float = 1.5) -> None:
        if not (0 < float(timeout_seconds) <= 10):
            raise InvalidRequest("timeout_seconds must be in (0, 10]")
        self._timeout = float(timeout_seconds)

    def _get(self, target: NormalizedTarget, path: str) -> tuple[int | None, bytes, str | None]:
        connection = http.client.HTTPConnection(target.host, target.port, timeout=self._timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(MAX_PROTOCOL_BODY_BYTES)
            return response.status, body, None
        except (OSError, http.client.HTTPException) as exc:
            return None, b"", type(exc).__name__
        finally:
            connection.close()

    def probe(self, target: NormalizedTarget, protocol: str) -> ProtocolObservation:
        if not isinstance(target, NormalizedTarget):
            raise InvalidRequest("target must be a NormalizedTarget")
        if not is_loopback_host(target.host):
            raise InvalidRequest(f"refusing to probe non-loopback host {target.host!r}")

        candidates = PROTOCOL_MODEL_IDENTITY_PATHS.get(protocol, ())
        if not candidates:
            # 这个协议没有可验证的模型身份 —— 如实说「无法验证」，而不是放行。
            return ProtocolObservation(
                state=PROTOCOL_UNVERIFIED,
                reason=REASON_NO_MODEL_IDENTITY_FOR_PROTOCOL,
                protocol=protocol,
                source=SOURCE_PROBE,
            )

        last_status: int | None = None
        last_path: str | None = None
        for path in candidates:
            status, body, error = self._get(target, path)
            last_path = path
            if error is not None:
                return ProtocolObservation(
                    state=PROTOCOL_UNVERIFIED,
                    reason=REASON_TRANSPORT_UNREACHABLE,
                    protocol=protocol,
                    checked_path=path,
                    source=SOURCE_PROBE,
                )
            last_status = status
            if status is None or not (200 <= status < 300):
                continue
            count = parse_model_list(body)
            if count is None:
                # 2xx 但不是模型清单 —— 这正是要抓的那种「任意服务」。
                continue
            return ProtocolObservation(
                state=PROTOCOL_VERIFIED,
                reason=REASON_MODEL_LIST_VERIFIED,
                protocol=protocol,
                checked_path=path,
                http_status=status,
                model_ids=count,
                source=SOURCE_PROBE,
            )

        return ProtocolObservation(
            state=PROTOCOL_UNVERIFIED,
            reason=REASON_MODEL_LIST_MISSING,
            protocol=protocol,
            checked_path=last_path,
            http_status=last_status,
            source=SOURCE_PROBE,
        )


def build_verification(
    *,
    transport: HealthObservation | None,
    protocol: ProtocolObservation | None,
    require_protocol: bool,
) -> dict:
    """把两根轴拼成一份**不会互相冒名**的验证结论。

    ``verified`` 的语义在这一个函数里定义清楚：

    * ``require_protocol=False``（本仓库的兼容缺省）：传输可达即 ``verified``，
      但 ``protocol.verified`` 仍然如实为 ``False``，且 ``blockers`` 会点名
      「协议未验证」—— 于是没有哪个前端能把它读成「模型已就绪」。
    * ``require_protocol=True``：**两根轴都通过**才算 ``verified``。
    """
    transport_payload = transport.to_dict() if transport is not None else unknown(
        REASON_NOT_CONFIGURED
    )
    protocol_payload = (
        protocol.to_dict() if protocol is not None else unknown_protocol()
    )

    transport_ok = bool(transport_payload.get("verified"))
    protocol_verified = bool(protocol_payload.get("verified"))

    blockers: list[str] = []
    if not transport_ok:
        blockers.append("transport_unreachable")
    if not protocol_verified:
        blockers.append("protocol_unverified")

    verified = transport_ok and (protocol_verified or not require_protocol)
    return {
        "transport": transport_payload,
        "protocol": protocol_payload,
        "require_protocol": bool(require_protocol),
        "transport_verified": transport_ok,
        "protocol_verified": protocol_verified,
        "verified": verified,
        "ready": verified,
        "blockers": blockers,
    }
