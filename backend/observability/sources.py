"""指标来源适配：Mock provider 与**固定表达式**的 Prometheus 只读 provider。

**本模块的两条硬边界：**

1. **Web 层永远提供不了 PromQL。** :meth:`PrometheusProvider.read` 的入参只有一个
   ``metric_id``；表达式来自 :data:`FIXED_EXPRESSIONS` 这张**代码里写死**的登记表。
   结构上没有「把用户字符串拼进查询」的路径 —— 不是靠校验，是靠没有那个参数。
2. **Web 层永远提供不了 URL。** 端点地址是 :class:`PrometheusProvider` 的构造期配置，
   且构造时即校验必须落在环回或显式允许列表内（:func:`validate_base_url`）。
   一个指向任意外网地址的 provider 根本构造不出来。

一条表达式若返回多条序列，而该表达式**没有**声明归约方式，我们返回
``reason=ambiguous_series`` 而**不是**随手取第一条或求和 —— 隐含的归约选择会让
「整机利用率」这种数字在无人察觉的情况下改变口径。

本模块后半段（“主机投影的生产采样 Reader”）另有一组**主机投影专用**的固定指标
（:data:`STRATA_HOST_METRICS`）与读取器（:class:`HostProjectionReader`）：它把六个
``llmlab_strata_*`` 只读指标采成 ``/api/infra/summary`` 那份契约的形状，供 BFF 的
``HostSummaryStack(read=...)`` 注入。两条硬边界在那组接口上**同样成立** ——
``read()`` 无参数，端点仍是构造期配置。
"""

from __future__ import annotations

import ipaddress
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol

from .dto import (
    CATALOGUE,
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    REASON_AMBIGUOUS_SERIES,
    REASON_NO_SAMPLE,
    REASON_SOURCE_UNAVAILABLE,
    MetricReading,
    StateReading,
    TextReading,
)
from .errors import ConfigRejected, InvalidRequest, SourceUnavailable, UnknownMetric
from .host_projection import (
    DEFAULT_FUTURE_TOLERANCE_S,
    DEFAULT_MAX_SAMPLE_AGE_S,
    HOST_METRIC_UNITS,
    PROJECTION_PATH,
    PROJECTION_SOURCE,
    PROJECTION_VERSION,
)
from .trust import SOURCE_MOCK, SOURCE_PROMETHEUS_READONLY

#: Prometheus 查询的默认超时（秒）。采集是**只读探测**，不能拖住请求。
DEFAULT_TIMEOUT_SECONDS = 2.0

#: 允许的归约方式。表达式必须**显式**声明其一（或声明 ``single``）。
REDUCERS = ("single", "sum", "max", "min", "avg")


# ---------------------------------------------------------------------------
# 从 provider 拿到的原始样本
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderSample:
    """provider 层的一条原始观测。**尚未**带上目录里的 scope / unit —— 那是
    :meth:`to_reading` 的事，免得 provider 有机会自称作用域。"""

    metric_id: str
    source: str
    synthetic: bool
    value: float | int | str | None
    quality: str = QUALITY_MEASURED
    observed_at: datetime | None = None
    reason: str | None = None

    def to_reading(self, now: datetime | None = None):
        """按目录把样本转成对应的读数类型。

        类型由 ``CATALOGUE[metric_id].unit`` **唯一决定**，provider 无从选择 ——
        否则一个 provider 就能把一条状态量伪装成数值量（或反之）绕过校验。
        """
        spec = CATALOGUE.get(self.metric_id)
        if spec is None:
            raise UnknownMetric(f"metric not in catalogue: {self.metric_id!r}")

        common = {
            "source": self.source,
            "quality": self.quality,
            "observed_at": self.observed_at,
            "reason": self.reason,
            "now": now,
        }
        if spec.unit == "state":
            # 取值域取自**目录**：provider 无从另说一套合法状态。
            return StateReading.from_metric(
                self.metric_id,
                state=self.value if isinstance(self.value, str) else None,
                allowed_states=spec.allowed_states or (),
                **common,
            )
        if spec.unit == "text":
            return TextReading.from_metric(
                self.metric_id,
                text=self.value if isinstance(self.value, str) else None,
                **common,
            )
        # unknown 读数必须带 value=None；其余情况原样交给 from_metric 去校验类型。
        numeric = None if self.quality == QUALITY_UNKNOWN else self.value
        return MetricReading.from_metric(self.metric_id, value=numeric, **common)


class MetricProvider(Protocol):
    """只读指标来源。实现必须声明 :attr:`source` 与 :attr:`synthetic`。"""

    source: str
    synthetic: bool

    def read(self, metric_id: str) -> ProviderSample:  # pragma: no cover - Protocol
        ...


def _unknown_sample(metric_id: str, source: str, synthetic: bool, reason: str) -> ProviderSample:
    return ProviderSample(
        metric_id=metric_id,
        source=source,
        synthetic=synthetic,
        value=None,
        quality=QUALITY_UNKNOWN,
        observed_at=None,
        reason=reason,
    )


class BaseProvider:
    """把「来源不可达」统一折成一条 ``quality=unknown`` 的样本。

    这一步很关键：**不可达不等于 0**。若让 ``SourceUnavailable`` 直接冒泡到 service
    层，很容易在某个分支里被写成 ``0``；这里一次性把失败变成「没测到 + 原因码」。
    """

    source: str = "base"
    synthetic: bool = False

    def read(self, metric_id: str) -> ProviderSample:  # pragma: no cover - 抽象
        raise NotImplementedError

    def safe_read(self, metric_id: str) -> ProviderSample:
        try:
            return self.read(metric_id)
        except SourceUnavailable:
            return _unknown_sample(
                metric_id, self.source, self.synthetic, REASON_SOURCE_UNAVAILABLE
            )

    def to_readings(self, metric_ids: Iterable[str], now: datetime | None = None) -> dict:
        out: dict[str, dict] = {}
        for metric_id in metric_ids:
            out[metric_id] = self.safe_read(metric_id).to_reading(now).to_dict()
        return out

    def available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Mock provider —— 合成数据，永远 synthetic=True
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockPoint:
    value: float | int | str | None
    observed_at: datetime | None = None
    quality: str = QUALITY_MEASURED
    reason: str | None = None


class MockMetricProvider(BaseProvider):
    """离线的合成指标来源。

    ``synthetic`` **恒为 True 且不可覆写** —— 这是构造参数之外的常量，好让
    「把 mock 当 live 报出去」在结构上不可能。测试与预览 adapter 只用它。
    """

    source = SOURCE_MOCK
    synthetic = True

    def __init__(self, points: Mapping[str, MockPoint] | None = None) -> None:
        cleaned: dict[str, MockPoint] = {}
        for metric_id, point in (points or {}).items():
            if metric_id not in CATALOGUE:
                raise UnknownMetric(f"metric not in catalogue: {metric_id!r}")
            if not isinstance(point, MockPoint):
                raise InvalidRequest("mock points must be MockPoint instances")
            cleaned[metric_id] = point
        self._points = MappingProxyType(cleaned)

    def read(self, metric_id: str) -> ProviderSample:
        if metric_id not in CATALOGUE:
            raise UnknownMetric(f"metric not in catalogue: {metric_id!r}")
        point = self._points.get(metric_id)
        if point is None:
            # 没给点就是「这份合成数据里没有这项」—— 报 no_sample，不报 0。
            return _unknown_sample(metric_id, self.source, True, REASON_NO_SAMPLE)
        return ProviderSample(
            metric_id=metric_id,
            source=self.source,
            synthetic=True,
            value=point.value,
            quality=point.quality,
            observed_at=point.observed_at,
            reason=point.reason,
        )


# ---------------------------------------------------------------------------
# Prometheus —— 固定表达式 + 环回/白名单端点
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrometheusExpression:
    """一条**写死在代码里**的 PromQL。

    ``reducer`` 必须显式声明：多序列结果不会被隐含地求和或取首条。
    """

    metric_id: str
    query: str
    reducer: str
    description: str

    def __post_init__(self) -> None:
        if self.metric_id not in CATALOGUE:
            raise UnknownMetric(f"expression for unknown metric {self.metric_id!r}")
        if self.reducer not in REDUCERS:
            raise InvalidRequest(f"unsupported reducer {self.reducer!r}")
        if not isinstance(self.query, str) or not self.query.strip():
            raise InvalidRequest("expression query must be a non-empty string")


#: 固定表达式登记表。**这是本模块唯一能发给 Prometheus 的查询集合。**
#:
#: 表中只有物理 / 服务类的聚合表达式。**刻意不含任何按 key、按用户的 PromQL**：
#: 按人计量走 Gateway 已验证 key 的元数据（``backend/observability/gateway.py``），
#: 不经过 Prometheus —— 两套口径混用正是「把总卡利用率当成用户账单」的来源。
FIXED_EXPRESSIONS: Mapping[str, PrometheusExpression] = MappingProxyType(
    {
        "node.cpu.utilization_percent": PrometheusExpression(
            metric_id="node.cpu.utilization_percent",
            query='100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
            reducer="single",
            description="整机 CPU 空闲率反推使用率",
        ),
        "node.memory.available_bytes": PrometheusExpression(
            metric_id="node.memory.available_bytes",
            query="node_memory_MemAvailable_bytes",
            reducer="single",
            description="可用物理内存",
        ),
        "node.memory.total_bytes": PrometheusExpression(
            metric_id="node.memory.total_bytes",
            query="node_memory_MemTotal_bytes",
            reducer="single",
            description="物理内存总量",
        ),
        "node.disk.used_bytes": PrometheusExpression(
            metric_id="node.disk.used_bytes",
            query='node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_avail_bytes{mountpoint="/"}',
            reducer="single",
            description="根文件系统已用字节",
        ),
        # 这两个读数**必须**同基：它们构成 `used / total` 这个比值。早先
        # `total` 用 `max`、`used` 用 `sum`，于是在多卡机器上分子分母来自不同的
        # 总体 —— 两卡各满一半就能算出「已用 > 总量」。**同基**是这一对读数的
        # 硬要求，不是风格问题；回归：`test_sources` 的 reducer 同基断言。
        "device.gpu.memory.total_bytes": PrometheusExpression(
            metric_id="device.gpu.memory.total_bytes",
            query="DCGM_FI_DEV_FB_TOTAL * 1024 * 1024",
            reducer="sum",
            description="本机全部卡的显存总量合计（与 used 同基，可作分母）",
        ),
        "device.gpu.memory.used_bytes": PrometheusExpression(
            metric_id="device.gpu.memory.used_bytes",
            query="DCGM_FI_DEV_FB_USED * 1024 * 1024",
            reducer="sum",
            description="本机全部卡的已用显存合计（**共享量，不是任何人的用量**）",
        ),
        "device.gpu.utilization_percent": PrometheusExpression(
            metric_id="device.gpu.utilization_percent",
            query="avg(DCGM_FI_DEV_GPU_UTIL)",
            reducer="single",
            description="整卡平均利用率",
        ),
        "device.gpu.temperature_celsius": PrometheusExpression(
            metric_id="device.gpu.temperature_celsius",
            query="max(DCGM_FI_DEV_GPU_TEMP)",
            reducer="single",
            description="整卡最高温度",
        ),
        "service.up": PrometheusExpression(
            metric_id="service.up",
            query="up{job=~\"prometheus|grafana\"}",
            reducer="min",
            description="采集目标是否可达（取最小 = 有任何一个掉线就是 down）",
        ),
    }
)


#: 唯一被当作环回接受的**字面**主机名。
#:
#: 只有 ``localhost`` 这一个名字，因为它是 POSIX 约定、必然在本机 hosts 里。
#: 其余一律必须是 IP **字面量**，见 :func:`_is_loopback`。
LOOPBACK_LITERAL_HOSTS = frozenset({"localhost"})


def _is_loopback(host: str) -> bool:
    """``host`` 是否是环回地址 —— **不做任何名字解析**。

    两条刻意为之的性质：

    * **不查 DNS。** 早先的实现调用 ``getaddrinfo``，于是校验路径上会有一次可阻塞的
      网络查询；更要紧的是，它把「解析结果是不是 127.x」当成了判据 —— 任何一个
      A 记录指向 ``127.0.0.1`` 的**外部**域名（``*.nip.io``、``localtest.me`` 之类）
      都会被判成环回。那是一类货真价实的 SSRF 绕过：真正的判据应该是
      「这就是本机」，而不是「它说自己指向本机」。
    * **只认字面量。** 判据因此是「IP 字面量且落在环回段」，或显式白名单里的名字 ——
      而白名单是运维写死的一行配置，不由外部输入决定。
    """
    if not isinstance(host, str) or not host:
        return False
    candidate = host.strip().strip("[]").lower()
    if candidate in LOOPBACK_LITERAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # 不是 IP 字面量 —— 一个普通域名。它必须显式出现在 allowed_hosts 里。
        return False


def validate_base_url(base_url: str, allowed_hosts: Iterable[str] = ()) -> str:
    """校验 provider 的端点地址。失败抛 :class:`ConfigRejected`。

    规则（全部为「拒绝」而非「净化」—— 改写一个地址只会让人以为它还是原来那个）：

    * 必须是 ``http`` / ``https`` 绝对 URL；
    * 不得带 userinfo（``user:pass@``）、query、fragment —— 凭据不进配置串；
    * host 必须是环回，**或**显式出现在 ``allowed_hosts`` 里。
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigRejected("prometheus base_url must be a non-empty string")
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ConfigRejected(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ConfigRejected("base_url must carry a host")
    if parsed.username or parsed.password:
        raise ConfigRejected("base_url must not carry userinfo")
    if parsed.query or parsed.fragment:
        raise ConfigRejected("base_url must not carry a query or fragment")

    allowed = {str(h).strip() for h in allowed_hosts}
    if parsed.hostname not in allowed and not _is_loopback(parsed.hostname):
        raise ConfigRejected(
            f"base_url host {parsed.hostname!r} is neither loopback nor in the allowlist"
        )
    return base_url.strip()


class PrometheusTransport(Protocol):
    """只读查询通道。测试注入假实现，生产用 :class:`UrllibTransport`。"""

    def query(self, base_url: str, query: str, timeout_seconds: float) -> dict:  # pragma: no cover
        ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """把**任何**重定向当作失败，而不是跟随。

    ``urllib`` 默认跟随 302/301。若不关掉，:func:`validate_base_url` 建立的
    「只准环回」策略在**请求期**就被绕过了：一个被指向的（或已被攻陷的）Prometheus
    只要回一个 ``302 Location: http://169.254.169.254/...``，客户端就会去取那个地址。
    构造期校验管不住跳转，所以这里让跳转直接失败。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect refused (policy: no redirects): {msg}", headers, fp
        )


#: 不跟随重定向的 opener。
_OPENER = urllib.request.build_opener(_NoRedirects)


class UrllibTransport:
    """``urllib`` 的只读 GET。**只**发 ``/api/v1/query``，不带任何写方法、不跟随重定向。"""

    def query(self, base_url: str, query: str, timeout_seconds: float) -> dict:
        params = urllib.parse.urlencode({"query": query})
        url = f"{base_url.rstrip('/')}/api/v1/query?{params}"
        request = urllib.request.Request(url, method="GET")
        try:
            with _OPENER.open(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SourceUnavailable(f"prometheus query failed: {exc}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise SourceUnavailable("prometheus returned a non-JSON body") from exc


def _reduce(values: list[float], reducer: str) -> float | None:
    if not values:
        return None
    if reducer == "single":
        # 声明了 single 却拿到多条 —— 交给调用方报 ambiguous，不在这里悄悄取一条。
        return values[0] if len(values) == 1 else None
    if reducer == "sum":
        return math.fsum(values)
    if reducer == "max":
        return max(values)
    if reducer == "min":
        return min(values)
    if reducer == "avg":
        return math.fsum(values) / len(values)
    raise InvalidRequest(f"unsupported reducer {reducer!r}")  # pragma: no cover - 构造期已挡


class PrometheusProvider(BaseProvider):
    """固定表达式的 Prometheus 只读 provider。

    ``synthetic`` 恒为 ``False``：它读的是真实时序。但**「非合成」不等于「已经接线」** ——
    本任务不部署 Prometheus，生产启用前 ``available()`` 会如实返回 False。

    来源名逐字取自受信白名单（:data:`SOURCE_PROMETHEUS_READONLY`）—— 早先它自称
    ``"prometheus"``，而那不在任何白名单里：一份按契约接好线的读数会被消费方
    **全部静默拒绝**。名字对不上，是「接对了却不显示」这类最难查的失败。
    """

    source = SOURCE_PROMETHEUS_READONLY
    synthetic = False

    def __init__(
        self,
        base_url: str,
        *,
        transport: PrometheusTransport | None = None,
        expressions: Mapping[str, PrometheusExpression] | None = None,
        allowed_hosts: Iterable[str] = (),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        enabled: bool = False,
    ) -> None:
        self.base_url = validate_base_url(base_url, allowed_hosts)
        self._transport = transport if transport is not None else UrllibTransport()
        self._expressions = dict(expressions if expressions is not None else FIXED_EXPRESSIONS)
        for metric_id, expression in self._expressions.items():
            if metric_id != expression.metric_id:
                raise InvalidRequest(
                    f"expression key {metric_id!r} disagrees with its metric_id"
                )
        self._timeout = float(timeout_seconds)
        #: 默认**关**：本任务不接生产 Prometheus，未显式打开时一律如实报不可达。
        self._enabled = bool(enabled)

    @property
    def expressions(self) -> Mapping[str, PrometheusExpression]:
        return MappingProxyType(dict(self._expressions))

    def available(self) -> bool:
        return self._enabled

    def read(self, metric_id: str) -> ProviderSample:
        expression = self._expressions.get(metric_id)
        if expression is None:
            # 未登记的指标名 —— 与「你没权限」是不同的失败，所以是不同的异常。
            raise UnknownMetric(f"no fixed expression registered for {metric_id!r}")
        if not self._enabled:
            return _unknown_sample(
                metric_id, self.source, False, REASON_SOURCE_UNAVAILABLE
            )

        payload = self._transport.query(self.base_url, expression.query, self._timeout)
        return self._sample_from_payload(metric_id, expression, payload)

    # -- 解析 ---------------------------------------------------------------

    def _sample_from_payload(
        self, metric_id: str, expression: PrometheusExpression, payload: object
    ) -> ProviderSample:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise SourceUnavailable("prometheus reported a non-success status")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SourceUnavailable("prometheus payload has no data object")

        result = data.get("result")
        if not isinstance(result, list):
            raise SourceUnavailable("prometheus payload has no result list")
        if not result:
            return _unknown_sample(metric_id, self.source, False, REASON_NO_SAMPLE)

        parsed = self._parse_vector(result)
        if parsed is None:
            # 非有限值：Prometheus 用 NaN/+Inf 表示「这一格没有可用数据」。
            # 既不当作 0，也不当作一个测量结果。
            return _unknown_sample(metric_id, self.source, False, REASON_NO_SAMPLE)

        values, observed_at = parsed
        spec = CATALOGUE[metric_id]

        # **先归约，再解释。** 之前这里对 state 指标直接取 ``values[0]``，
        # 于是 ``min`` 这个为 up 指标专门选的归约方式被悄悄忽略 —— 多个采集目标里
        # 只要有一个掉线，结果仍是 up。回归：ServiceUpUsesMinTest。
        reduced = _reduce(values, expression.reducer)
        if reduced is None:
            return _unknown_sample(metric_id, self.source, False, REASON_AMBIGUOUS_SERIES)

        if spec.unit == "state":
            if reduced not in (0.0, 1.0):
                # 只可能是 0/1；别的一律说明我们对这个指标的理解是错的。
                return _unknown_sample(
                    metric_id, self.source, False, REASON_AMBIGUOUS_SERIES
                )
            return ProviderSample(
                metric_id=metric_id,
                source=self.source,
                synthetic=False,
                value="up" if reduced == 1.0 else "down",
                observed_at=observed_at,
            )

        quality = QUALITY_MEASURED if len(values) == 1 else QUALITY_PARTIAL
        return ProviderSample(
            metric_id=metric_id,
            source=self.source,
            synthetic=False,
            value=reduced,
            quality=quality,
            observed_at=observed_at,
        )

    def _parse_vector(self, result: list) -> tuple[list[float], datetime] | None:
        """把即时向量结果取成一个「数值列表 + 最新时间戳」。

        两种失败**分开**：

        * **形状不对**（不是 ``{value: [ts, "v"]}``，例如区间向量）-> 抛
          :class:`SourceUnavailable`。这是我们问错了东西，不是数据本身模糊；
        * **数值非有限**（``NaN`` / ``+Inf``）-> 返回 ``None``，上层报
          ``no_sample``。Prometheus 用它们表示「没有数据」。
        """
        values: list[float] = []
        latest: datetime | None = None
        for series in result:
            if not isinstance(series, dict) or "value" not in series:
                raise SourceUnavailable(
                    "prometheus result is not an instant vector (expected a 'value' pair)"
                )
            pair = series["value"]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise SourceUnavailable("prometheus sample is not a (timestamp, value) pair")
            timestamp, raw = pair
            try:
                number = float(raw)
                moment = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError, OSError) as exc:
                raise SourceUnavailable(f"prometheus sample is unparseable: {exc}") from exc
            if not math.isfinite(number):
                return None
            values.append(number)
            if latest is None or moment > latest:
                latest = moment
        if not values or latest is None:
            raise SourceUnavailable("prometheus vector carried no usable sample")
        return values, latest


# ---------------------------------------------------------------------------
# 主机投影（``/api/infra/summary``）的生产采样 Reader
# ---------------------------------------------------------------------------
#
# 背景：那份投影此前只有 **Node** 侧的产生方（``web/tools/verified-infra.mjs``），
# Python 服务端只有校验方（:mod:`backend.observability.host_projection`）。于是
# PR #85 的 ``HostSummaryStack(read=...)`` 没有可注入的真实来源 —— 它要的是一个
# **零参数**的 ``read()``，而仓库里没有那个东西。
#
# 本节补上的就是这个产生方，并且把它钉在三条边界上：
#
# 1. **查询名写死在** :data:`STRATA_HOST_METRICS`。``read()`` 无参数，所以请求侧
#    仍然给不出 PromQL；端点仍是构造期配置且必须环回 / 白名单。
# 2. **单位 / 键取自** ``host_projection.HOST_METRIC_UNITS`` —— 那是唯一真源，
#    本模块不另立一份，免得服务端校验器与产生方对不上而互相静默拒绝。
# 3. **产物直接喂给** ``validate_host_summary``。产生方不能产出一份自己校验器
#    不接受的投影；那等于两个真相。
#
# 与 Node 产生方的**刻意差异**（只有一处）：``enabled=False`` 时 ``read()`` 返回
# ``None`` 而不是一份全 UNKNOWN 的信封。Node 的 ``offlineSummary`` 服务于「路由
# 开着但主机取不到」，而这里 ``enabled=False`` 的语义是「本部署没有装配这条来源」
# —— BFF 对这两件事的结论不同（503 未接线 vs 200 逐条降级），混成一句会让一次
# 接线遗漏伪装成一次数据故障。

#: 生产 Prometheus 的环回地址。主机采集只走本机，不做一个网络客户端。
PROMETHEUS_LOOPBACK_ORIGIN = "http://127.0.0.1:9090"

#: 投影里 UNKNOWN 条目的原因码。**必须是** ``host_projection.UPSTREAM_REASONS``
#: 的子集 —— 一个不在词表里的原因会退化成没有理由的 ``unknown``。
PROJECTION_REASON_NO_SAMPLE = "no-sample"
PROJECTION_REASON_STALE = "stale"
PROJECTION_REASON_NON_FINITE = "non-finite-sample"
PROJECTION_REASON_PROMETHEUS_ERROR = "prometheus-error"
PROJECTION_REASON_TIMEOUT = "timeout"
PROJECTION_REASON_UNREACHABLE = "unreachable"

#: 主机投影允许的归约方式。**只有** ``max``：这些是按设备上报的量规，诚实的整机读数是
#: 「观测到的最高那一张卡」。``sum`` 会把若干张卡拼成一个**没有任何一张卡有过**的
#: 机器级数字；多卡机器上那是编出来的口径，不是测量。
HOST_REDUCERS = ("max",)

#: 固定的服务健康目标。**只读 HTTP 状态**，全部环回。
#:
#: 这里**没有** ``strata``：Strata 的状态取自 ``llmlab_strata_up`` 这条指标，
#: 因为它的 ``/health`` 响应体里带 ``api_key`` —— 那个端点根本不该被取。
HOST_HEALTH_TARGETS: Mapping[str, str] = MappingProxyType(
    {
        "prometheus": f"{PROMETHEUS_LOOPBACK_ORIGIN}/-/healthy",
        "gateway": "http://127.0.0.1:8200/healthz",
        "grafana": "http://127.0.0.1:3000/api/health",
        "evalscope": "http://127.0.0.1:9000/health",
    }
)


@dataclass(frozen=True)
class HostMetricSource:
    """一个投影键的来源定义：**真实 exporter 指标名** + 单位 + 换算 + 归约。

    ``key`` / ``unit`` 由 :data:`host_projection.HOST_METRIC_UNITS` 校验，所以
    「把摄氏度印成百分比」这类错位在**构造期**就被拒绝，而不是在下发之后。
    """

    key: str
    metric: str
    unit: str
    scale: float
    aggregation: str

    def __post_init__(self) -> None:
        expected_unit = HOST_METRIC_UNITS.get(self.key)
        if expected_unit is None:
            raise InvalidRequest(f"{self.key!r} is not a host projection key")
        if self.unit != expected_unit:
            raise InvalidRequest(
                f"{self.key}: unit {self.unit!r} disagrees with the contract "
                f"({expected_unit!r})"
            )
        if self.aggregation not in HOST_REDUCERS:
            raise InvalidRequest(
                f"{self.key}: unsupported host aggregation {self.aggregation!r}"
            )
        if not isinstance(self.metric, str) or not self.metric.strip():
            raise InvalidRequest(f"{self.key}: metric name must be a non-empty string")
        if (
            isinstance(self.scale, bool)
            or not isinstance(self.scale, (int, float))
            or not math.isfinite(float(self.scale))
            or float(self.scale) <= 0
        ):
            raise InvalidRequest(f"{self.key}: scale must be a positive finite number")


#: 主机投影的固定指标登记表。**这是本模块唯一能发给 Prometheus 的主机查询集合。**
#:
#: 六个名字逐字取自 Dell Prometheus ``/api/v1/metadata`` 的实测（2026-10-16），并与
#: ``web/tools/verified-infra.mjs`` 的 ``METRICS`` 漂移回归绑在一起
#: （``tests/observability/test_host_source_reader.py``）。**不要**把它们换回
#: ``DCGM_*`` 之类的通用名：那些从未在这台机器上被核验过，而「代码里写了查询」
#: 不等于「这台机器真的在报这个指标」。
#:
#: 显存两项的 ``scale`` 是 ``1MiB`` 的倒数：契约里的单位是 ``mib``，而 exporter 报的
#: 是**字节**。换算放在产生方，而不是让每个消费方各乘一次 —— 一处漏乘就是一个
#: 看起来合理、实际差 1048576 倍的数字。
STRATA_HOST_METRICS: Mapping[str, HostMetricSource] = MappingProxyType(
    {
        "gpu_utilization_ratio": HostMetricSource(
            key="gpu_utilization_ratio",
            metric="llmlab_strata_gpu_utilization_ratio",
            unit="ratio",
            scale=1.0,
            aggregation="max",
        ),
        "gpu_temperature_celsius": HostMetricSource(
            key="gpu_temperature_celsius",
            metric="llmlab_strata_gpu_temperature_celsius",
            unit="celsius",
            scale=1.0,
            aggregation="max",
        ),
        "gpu_power_watts": HostMetricSource(
            key="gpu_power_watts",
            metric="llmlab_strata_gpu_power_watts",
            unit="watts",
            scale=1.0,
            aggregation="max",
        ),
        "strata_up": HostMetricSource(
            key="strata_up",
            metric="llmlab_strata_up",
            unit="bool",
            scale=1.0,
            aggregation="max",
        ),
        "gpu_memory_used_mib": HostMetricSource(
            key="gpu_memory_used_mib",
            metric="llmlab_strata_gpu_memory_used_bytes",
            unit="mib",
            scale=1.0 / 1048576,
            aggregation="max",
        ),
        "gpu_memory_total_mib": HostMetricSource(
            key="gpu_memory_total_mib",
            metric="llmlab_strata_gpu_memory_total_bytes",
            unit="mib",
            scale=1.0 / 1048576,
            aggregation="max",
        ),
    }
)


def _assert_registry_covers_the_contract() -> None:
    """登记表必须**恰好**覆盖契约里的键。

    少一个键 -> 那份投影永远缺一个字段，而校验器会把缺键判成 ``unknown-metric-key``
    （「我们不知道投影端是哪个版本」）；多一个键 -> 我们发了一个契约里没有的东西。
    两种都是接线错误，所以在**导入期**就炸，而不是等某次请求。
    """
    if set(STRATA_HOST_METRICS) != set(HOST_METRIC_UNITS):
        missing = sorted(set(HOST_METRIC_UNITS) - set(STRATA_HOST_METRICS))
        extra = sorted(set(STRATA_HOST_METRICS) - set(HOST_METRIC_UNITS))
        raise ConfigRejected(
            f"host projection registry disagrees with the contract "
            f"(missing={missing}, extra={extra})"
        )


_assert_registry_covers_the_contract()


def _js_iso(moment: datetime) -> str:
    """UTC ISO-8601 毫秒精度 ``Z`` 结尾 —— 与 Node 的 ``toISOString()`` 同形。"""
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _js_round(value: float) -> int:
    """``Math.round`` 的语义（``.5`` 向上取整）。Python 的 ``round`` 是银行家舍入，
    两者在 ``x.5`` 上会给出不同的年龄，而年龄是要与 Node 侧逐字节比对的。"""
    return int(math.floor(value + 0.5))


def _unknown_host_metric(spec: HostMetricSource, reason: str, *, age_s: int | None = None) -> dict:
    entry = {
        "state": "unknown",
        "value": None,
        "unit": spec.unit,
        "source": PROJECTION_SOURCE,
        "metric": spec.metric,
        "reason": reason,
    }
    if age_s is not None:
        # 过期条目保留年龄：``sample_aged_out``（量到过、但太旧）与 ``no_sample_yet``
        # （从来没量到）对运维是两个结论。
        entry["age_s"] = age_s
    return entry


def project_host_vector(
    payload: object,
    spec: HostMetricSource,
    *,
    now_ms: float,
    max_sample_age_s: int = DEFAULT_MAX_SAMPLE_AGE_S,
    future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S,
) -> dict:
    """把一条 Prometheus 即时向量投影成契约里的那一个指标条目。

    返回两种之一：

    * ``{"value", "series", "age_s", "sample_at"}`` —— 有一条可用样本；
    * ``{"reason"[,"age_s"]}`` —— 没有可用样本，**且原因码来自契约词表**。

    **一条也没取到时返回 ``reason`` 而不是 ``0``。** 这个函数从不产出数字来填补
    「没测到」，它只回报为什么没测到。

    与 ``verified-infra.mjs`` 的 ``projectVector`` 逐条对齐：
    ``NaN`` / ``±Inf`` 不是测量结果；过期（``> max_sample_age_s``）与未来
    （``< -future_tolerance_s``）的样本都不许污染聚合；序列**标签一律不读** ——
    多卡机器上「这一条是哪张卡」是归属，而归并不是归属，读标签就会滑向编造归属。
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {"reason": PROJECTION_REASON_PROMETHEUS_ERROR}
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        return {"reason": PROJECTION_REASON_PROMETHEUS_ERROR}
    result = data.get("result")
    if not isinstance(result, list) or not result:
        return {"reason": PROJECTION_REASON_NO_SAMPLE}

    samples: list[tuple[float, float]] = []
    oldest_stale_age: float | None = None
    for series in result:
        if not isinstance(series, dict):
            continue
        raw = series.get("value")
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        stamp, text = raw[0], raw[1]
        # 空白字符串等同于「没有值」；一个空字符串既不新也不旧，它只是没回答问题。
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            moment = float(stamp)
            number = float(text)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(moment) or not math.isfinite(number):
            continue
        # Prometheus 的 ``NaN`` / ``+Inf`` 表示「这一格没有数据」；负的时间戳或读数
        # 不是这台机器会产生的量，一律当噪声丢掉而不是当测量。
        if moment <= 0 or number < 0:
            continue
        age = now_ms / 1000.0 - moment
        if age > max_sample_age_s:
            oldest_stale_age = age if oldest_stale_age is None else min(oldest_stale_age, age)
            continue
        if age < -future_tolerance_s:
            # 未来时间戳是数据质量问题，不是「更新的一笔」；不许它把一份过期的
            # 聚合「刷新」成新鲜的。
            continue
        samples.append((moment, number))

    if not samples:
        if oldest_stale_age is not None:
            return {"reason": PROJECTION_REASON_STALE, "age_s": _js_round(oldest_stale_age)}
        # 参考实现（Node）在这条路径上也报 ``non-finite-sample`` —— 它把「所有序列
        # 都被未来容差挡掉」与「数值非有限」合并成了同一个原因码。这里保持逐字一致：
        # 状态（unknown）与值（null）才是消费方真正依赖的，改原因码属于契约变更，
        # 要由契约 Owner 走一次评审，不是本地悄悄改掉。
        return {"reason": PROJECTION_REASON_NON_FINITE}

    if spec.aggregation != "max":  # pragma: no cover - 构造期已挡
        raise InvalidRequest(f"{spec.key}: unsupported aggregation {spec.aggregation!r}")

    newest = max(moment for moment, _ in samples)
    highest = max(number for _, number in samples)
    return {
        "value": highest * spec.scale,
        "series": len(samples),
        "age_s": _js_round(max(0.0, now_ms / 1000.0 - newest)),
        "sample_at": _js_iso(datetime.fromtimestamp(newest, tz=timezone.utc)),
    }


class HostHealthProbe(Protocol):
    """服务可达性探测。返回 ``True``/``False``/``None``（``None`` = 这次问不出来）。"""

    def probe(self, url: str, timeout_seconds: float) -> bool | None:  # pragma: no cover
        ...


class UrllibHealthProbe:
    """固定目标的 **HTTP 状态** 探针。不跟随重定向，响应体读完即丢。

    **连得上不算就绪。** 判据是状态码 ``200``，不是「TCP 连上了」：一个在监听、
    但 ``/healthz`` 回 500 的服务是「没就绪」，把它读成「在」会让一次半死状态在
    面板上显示为正常。响应体从不解析、从不透传 —— 于是 Strata ``/health`` 里那个
    ``api_key`` 结构上无法经由这里出去。
    """

    def __init__(self, opener=None) -> None:
        self._opener = opener if opener is not None else _OPENER

    def probe(self, url: str, timeout_seconds: float) -> bool | None:
        request = urllib.request.Request(url, method="GET")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                response.read()
                return getattr(response, "status", None) == 200
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 是「在听，但没就绪」；``HTTPError`` 也是 ``URLError`` 的子类，
            # 所以这一支必须排在前面，否则一次 500 会被读成「不可达」而不是「没就绪」。
            return exc.code == 200
        except (urllib.error.URLError, OSError, ValueError):
            return None


class HostProjectionReader:
    """把六个固定 Strata 指标采样成 ``/api/infra/summary`` 形状的**只读**读取器。

    ``read()`` **不接受任何参数** —— 那是注入缝的安全边界本身。它能收参数的话，
    迟早会有人从请求里给它填一个。

    它**不缓存**：缓存是部署决策（见 ``host_refresh``），不是读取器的。每次
    ``read()`` 都重新采样，端点是构造期配置。
    """

    source = PROJECTION_SOURCE
    version = PROJECTION_VERSION
    path = PROJECTION_PATH

    def __init__(
        self,
        base_url: str = PROMETHEUS_LOOPBACK_ORIGIN,
        *,
        transport: PrometheusTransport | None = None,
        metrics: Mapping[str, HostMetricSource] | None = None,
        health_probe: HostHealthProbe | None = None,
        allowed_hosts: Iterable[str] = (),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
        max_sample_age_s: int = DEFAULT_MAX_SAMPLE_AGE_S,
        future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S,
    ) -> None:
        self.base_url = validate_base_url(base_url, allowed_hosts)
        self._transport = transport if transport is not None else UrllibTransport()
        self._metrics = dict(metrics if metrics is not None else STRATA_HOST_METRICS)
        for key, spec in self._metrics.items():
            if key != spec.key:
                raise InvalidRequest(f"metric key {key!r} disagrees with its spec")
        self._health = health_probe if health_probe is not None else UrllibHealthProbe()
        self._timeout = float(timeout_seconds)
        #: 默认**关**：本任务不接生产 Prometheus，未显式打开时如实回 ``None``。
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_sample_age_s = int(max_sample_age_s)
        self._future_tolerance_s = int(future_tolerance_s)

    @property
    def metrics(self) -> Mapping[str, HostMetricSource]:
        return MappingProxyType(dict(self._metrics))

    def available(self) -> bool:
        return self._enabled

    def read(self) -> dict | None:
        """采一次样并投影。**返回 ``None`` 表示「本部署没有装配来源」**。

        与「测到了但都没数据」区分开：前者让 BFF 明确回 503（去查装配），后者是一份
        逐条 UNKNOWN 的合法投影（去查投影端）。上游抖动（超时 / 断连 / 非 success）
        走的是后一条路 —— 那是一份可渲染的、诚实的「没测到」。
        """
        if not self._enabled:
            return None

        started = self._clock().astimezone(timezone.utc)
        now_ms = started.timestamp() * 1000.0

        metrics: dict[str, dict] = {}
        for key, spec in self._metrics.items():
            metrics[key] = self._read_one(spec, now_ms)

        services: dict[str, dict] = {}
        for key, url in HOST_HEALTH_TARGETS.items():
            services[key] = self._probe_service(url)

        # Strata 的状态**只**来自它自己那条指标，不来自任何 HTTP 探测。
        strata = metrics["strata_up"]
        if strata["state"] == "known":
            services["strata"] = {
                "state": "known",
                "up": bool(strata["value"] >= 1),
                "reason": None,
            }
        else:
            services["strata"] = {
                "state": "unknown",
                "up": None,
                "reason": strata.get("reason"),
            }

        any_known = any(entry["state"] == "known" for entry in metrics.values())
        return {
            "version": PROJECTION_VERSION,
            "source": PROJECTION_SOURCE,
            "synthetic": False,
            "readonly": True,
            "state": "known" if any_known else "unknown",
            "observed_at": _js_iso(started),
            "metrics": metrics,
            "services": services,
        }

    # -- 内部 ---------------------------------------------------------------

    def _read_one(self, spec: HostMetricSource, now_ms: float) -> dict:
        try:
            payload = self._transport.query(self.base_url, spec.metric, self._timeout)
        except SourceUnavailable as exc:
            return _unknown_host_metric(spec, _reason_for_transport_failure(exc))
        except Exception:  # noqa: BLE001 - 任何上游异常都只能变成「没测到」
            return _unknown_host_metric(spec, PROJECTION_REASON_PROMETHEUS_ERROR)

        projected = project_host_vector(
            payload,
            spec,
            now_ms=now_ms,
            max_sample_age_s=self._max_sample_age_s,
            future_tolerance_s=self._future_tolerance_s,
        )
        if "value" not in projected:
            return _unknown_host_metric(
                spec, projected["reason"], age_s=projected.get("age_s")
            )
        return {
            "state": "known",
            "value": projected["value"],
            "unit": spec.unit,
            "source": PROJECTION_SOURCE,
            "metric": spec.metric,
            "series": projected["series"],
            "age_s": projected["age_s"],
            "sample_at": projected["sample_at"],
        }

    def _probe_service(self, url: str) -> dict:
        try:
            up = self._health.probe(url, self._timeout)
        except Exception:  # noqa: BLE001 - 探针故障只能变成「这次问不出来」
            up = None
        if up is None:
            return {
                "state": "unknown",
                "up": None,
                "reason": PROJECTION_REASON_UNREACHABLE,
            }
        return {"state": "known", "up": bool(up), "reason": None}


def _reason_for_transport_failure(exc: SourceUnavailable) -> str:
    """把一次传输失败折成契约词表里的原因码。

    超时与「连不上」对运维的指向不同（上游慢 / 配额 vs 进程不在），所以不合并 ——
    合并之后，面板上的 ``unreachable`` 既可能是 Prometheus 挂了，也可能是它只是忙。
    """
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    if isinstance(cause, TimeoutError):
        return PROJECTION_REASON_TIMEOUT
    return PROJECTION_REASON_UNREACHABLE


def build_host_projection_reader(
    *,
    base_url: str = PROMETHEUS_LOOPBACK_ORIGIN,
    **kwargs,
) -> HostProjectionReader:
    """装配一个主机投影读取器。

    ``enabled`` 默认 ``False``：把「本部署没有装配来源」作为默认值，而不是把
    「随便去读一个环回端口」作为默认值。生产启用是一次显式的决定。
    """
    return HostProjectionReader(base_url, **kwargs)


def host_summary_reader(
    *,
    base_url: str = PROMETHEUS_LOOPBACK_ORIGIN,
    **kwargs,
) -> Callable[[], object]:
    """**注入缝**：返回 PR #85 ``HostSummaryStack(read=...)`` 直接可用的零参 callable。

    BFF 侧只需要::

        from backend.observability.sources import host_summary_reader
        stack = HostSummaryStack(read=host_summary_reader(enabled=True))

    返回的 callable 不接参数（接线层给不了 URL / PromQL），并且在未启用时回
    ``None``，于是 ``HostSummaryStack`` 会如实以 ``SourceUnavailable`` 收场。
    """
    return build_host_projection_reader(base_url=base_url, **kwargs).read
