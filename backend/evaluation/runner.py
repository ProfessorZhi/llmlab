"""Runner 适配层：mock runner + EvalScope 适配器**桩**。

本模块里没有任何一行会真的去跑评测。这是一个刻意的结构性事实，而不是纪律：

* :class:`MockRunner` **只**产出 ``synthetic=True`` 的样本。它没有"返回真实结果"的
  代码路径——`run()` 里 ``synthetic`` 是写死的常量，不是参数。本部署 GPU 已满，
  mock 是唯一能产出结果的 runner，因此每一个结果都带 ``synthetic=True``。
* :class:`EvalScopeHttpAdapter` / :class:`EvalScopeCliAdapter` 是**桩**：
  ``run()`` 一律抛 :class:`~backend.evaluation.errors.RunnerUnavailable`。
  它们能回答的只有两件事——**版本检查**与**能力检测**——而且答案来自**注入的事实**，
  不是来自一次真实探测。这里没有 HTTP 客户端，没有 ``subprocess``，没有命令字符串。

为什么不去猜 EvalScope 的命令行
-------------------------------
"猜一条命令行"是一个看起来在进展、实则制造债务的动作：一条猜错的 EvalScope 命令
在最好的情况下报错，在最坏的情况下用错误的参数跑出一份看起来正常的结果，然后被归档
成一次基线。宁可在 ``decode`` 层留一个显式的 ``not_wired``。

为什么硬编码拒绝 ``:9000``
--------------------------
生产 EvalScope 在那个端口上。一个"配错了就在生产上跑起来"的默认值是这类接线最危险的
失效模式，因此 :func:`parse_runner_base_url` 把 9000 当作**绊线**：带这个端口的地址
一律拒绝，错误信息里说明原因。这不是"暂时不方便"，是一个应当长期存在的检查。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from .errors import InvalidRequest, RunnerUnavailable
from .models import ALLOWED_METRIC_NAMES, MetricSample, metric_known, metric_unknown

# ---------------------------------------------------------------------------
# runner 种类
# ---------------------------------------------------------------------------

RUNNER_MOCK = "mock"
RUNNER_EVALSCOPE_HTTP = "evalscope_http"
RUNNER_EVALSCOPE_CLI = "evalscope_cli"
RUNNER_KINDS = (RUNNER_MOCK, RUNNER_EVALSCOPE_HTTP, RUNNER_EVALSCOPE_CLI)

# 适配器能力原因。稳定字符串。
ADAPTER_NOT_WIRED = "adapter_not_wired"
ADAPTER_NOT_CONFIGURED = "adapter_not_configured"
ADAPTER_VERSION_UNKNOWN = "adapter_version_unknown"
ADAPTER_VERSION_UNSUPPORTED = "adapter_version_unsupported"
ADAPTER_KIND_UNKNOWN = "adapter_kind_unknown"

#: 生产 EvalScope 的端口。**绊线**：带它的地址一律拒绝。
EVALSCOPE_PRODUCTION_PORT = 9000
REASON_PRODUCTION_PORT_REFUSED = "production_port_refused"
REASON_NON_LOOPBACK_REFUSED = "non_loopback_refused"
REASON_BAD_SCHEME = "bad_scheme"
REASON_USERINFO_REFUSED = "userinfo_refused"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?$")

#: 本适配器认识的最低 EvalScope 版本。**刻意不猜**：真实版本探测未接线，
#: 因此这个下界此刻只是一个契约声明，不是"我们已经验证过的兼容性"。
MIN_SUPPORTED_EVALSCOPE_VERSION = "1.0.0"


def parse_version(raw: object) -> tuple[int, int, int] | None:
    """把 ``"1.2.3"`` 解析成 ``(1, 2, 3)``。形状不认识就返回 ``None``（不猜）。"""
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        return None
    major, minor, patch, extra = match.groups()
    if extra is not None:
        # 四段式不是我们认识的东西；宁可说"不认识"。
        return None
    return int(major), int(minor), int(patch or 0)


@dataclass(frozen=True)
class VersionGate:
    """版本下界检查。"""

    minimum: str = MIN_SUPPORTED_EVALSCOPE_VERSION

    def __post_init__(self) -> None:
        if parse_version(self.minimum) is None:
            raise InvalidRequest("minimum version must look like MAJOR.MINOR[.PATCH]")

    def evaluate(self, raw: object) -> tuple[bool, str | None, str | None]:
        """返回 ``(ok, normalized_version, reason)``。"""
        parsed = parse_version(raw)
        if parsed is None:
            return False, None, ADAPTER_VERSION_UNKNOWN
        floor = parse_version(self.minimum)
        assert floor is not None
        if parsed < floor:
            return False, ".".join(str(part) for part in parsed), ADAPTER_VERSION_UNSUPPORTED
        return True, ".".join(str(part) for part in parsed), None


def parse_runner_base_url(raw: object) -> str:
    """校验一个 runner 基址；不合规就抛 :class:`InvalidRequest`。

    规则（全部是否决式，没有一条是"宽容地修一下"）：

    * scheme 只能是 ``http`` / ``https``；
    * **不能**带 userinfo（``http://<user>:<password>@host``）——那是一种会渗进日志的凭据形态；
    * host 必须是环回（``127.0.0.1`` / ``::1`` / ``localhost``）；
    * **端口不能是 9000**（生产 EvalScope），这是绊线。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidRequest("runner base url must be a non-empty string")
    candidate = raw.strip()
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise InvalidRequest(f"runner base url must be http or https ({REASON_BAD_SCHEME})")
    if parts.username is not None or parts.password is not None:
        raise InvalidRequest(f"runner base url must not carry credentials ({REASON_USERINFO_REFUSED})")
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise InvalidRequest(
            f"runner base url must target loopback ({REASON_NON_LOOPBACK_REFUSED})"
        )
    try:
        port = parts.port
    except ValueError:
        raise InvalidRequest("runner base url has an unparseable port") from None
    if port == EVALSCOPE_PRODUCTION_PORT:
        raise InvalidRequest(
            "refusing to point at the production EvalScope port 9000 "
            f"({REASON_PRODUCTION_PORT_REFUSED})"
        )
    return candidate


def redact_base_url(raw: str | None) -> dict:
    """给 DTO 用的基址描述：**不回显**原串，只回显它的类别。"""
    if raw is None:
        return {"present": False, "host_class": None, "port": None}
    parts = urlsplit(raw)
    try:
        port = parts.port
    except ValueError:  # pragma: no cover - parse_runner_base_url 已挡
        port = None
    return {
        "present": True,
        "host_class": "loopback",
        "scheme": parts.scheme,
        "port": port,
        "path_depth": len([p for p in (parts.path or "").split("/") if p]),
        "query_present": bool(parts.query),
    }


# ---------------------------------------------------------------------------
# 样本与能力
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerSample:
    """runner 的一次产出。

    ``synthetic`` **没有默认值**：忘了声明会变成 ``TypeError``，而不是一个默认
    ``False``——那会让一份合成样本冒充真实评测。
    """

    synthetic: bool
    metrics: Mapping[str, MetricSample]
    report_ref: str | None = None
    summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.synthetic, bool):
            raise InvalidRequest("synthetic must be an explicit boolean")
        unknown = sorted(set(self.metrics) - ALLOWED_METRIC_NAMES)
        if unknown:
            raise InvalidRequest(f"runner produced unsupported metric(s): {', '.join(unknown)}")
        for name, sample in self.metrics.items():
            if not isinstance(sample, MetricSample):
                raise InvalidRequest(f"metric {name!r} is not a MetricSample")

    def to_dict(self) -> dict:
        return {
            "synthetic": self.synthetic,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "report_ref": self.report_ref,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class CapabilityReport:
    """一个 adapter 的能力检测结果。**只描述自己**，不预测未来。"""

    kind: str
    available: bool
    synthetic_only: bool
    wired: bool
    detected_version: str | None
    version_supported: bool | None
    supported_eval_types: tuple[str, ...]
    reasons: tuple[str, ...]
    base_url: dict | None = None

    def __post_init__(self) -> None:
        if self.kind not in RUNNER_KINDS:
            raise InvalidRequest(f"unknown runner kind: {self.kind!r}")
        if self.available and not self.wired:
            # 没接线的 adapter 不可能"可用"。这条不变量让 available 无法被误设。
            raise InvalidRequest("an unwired adapter cannot report itself available")

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "available": self.available,
            "synthetic_only": self.synthetic_only,
            "wired": self.wired,
            "detected_version": self.detected_version,
            "version_supported": self.version_supported,
            "supported_eval_types": list(self.supported_eval_types),
            "reasons": list(self.reasons),
            "base_url": dict(self.base_url) if self.base_url else None,
        }


# ---------------------------------------------------------------------------
# mock runner
# ---------------------------------------------------------------------------


class MockRunner:
    """确定性 mock runner。**只**产出合成样本。

    它不做 IO、不读文件、不联网、不加载模型。它唯一的用途是在真实 runner 不可用的
    部署里验证链路形状（提交 -> 排队 -> 结果 DTO -> 报告引用），并让 UI 有一个诚实的
    空态之外的东西可以渲染——一个明确标注 ``synthetic`` 的东西。
    """

    kind = RUNNER_MOCK
    synthetic_only = True
    is_real = False

    def __init__(self, *, version: str = "1.0.0") -> None:
        if parse_version(version) is None:
            raise InvalidRequest("mock runner version must look like a version")
        self._version = version

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            kind=self.kind,
            available=True,
            synthetic_only=True,
            wired=True,
            detected_version=self._version,
            version_supported=True,
            supported_eval_types=(
                "correctness",
                "throughput",
                "latency",
                "safety_preset",
            ),
            reasons=(),
        )

    def run(self, job: object, *, attempt: int = 1) -> RunnerSample:
        """产出一份**确定性的**合成样本。

        ``synthetic=True`` 是写死的常量。这里没有参数能让它变成 ``False``——这是
        "mock 永不冒充真实评测"得以成立的地方。
        """
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise InvalidRequest("attempt must be a positive integer")

        eval_type = str(getattr(getattr(job, "spec", None), "eval_type", "correctness"))
        job_id = str(getattr(job, "job_id", "unknown"))

        metrics: dict[str, MetricSample] = {
            "samples": metric_known("samples", 8),
            "samples_failed": metric_known("samples_failed", 0),
        }
        if eval_type == "correctness":
            metrics["accuracy"] = metric_known("accuracy", 0.5, unit="ratio")
            # 真实分位数需要真实样本；mock 不产生它们。
            metrics["latency_p99_ms"] = metric_unknown(
                "latency_p99_ms", note="合成样本不产生分位数；这不是 0。"
            )
        elif eval_type == "throughput":
            metrics["throughput_rps"] = metric_known("throughput_rps", 1.0, unit="req/s")
            metrics["latency_p99_ms"] = metric_unknown(
                "latency_p99_ms", note="合成样本不产生分位数；这不是 0。"
            )
        elif eval_type == "latency":
            metrics["latency_p50_ms"] = metric_known("latency_p50_ms", 12.0, unit="ms")
            metrics["latency_p99_ms"] = metric_known("latency_p99_ms", 24.0, unit="ms")
        else:
            metrics["refusal_rate"] = metric_known("refusal_rate", 0.0, unit="ratio")

        return RunnerSample(
            synthetic=True,
            metrics=metrics,
            # 相对、只读的引用形状；报告根未接线时它不会被解析成路径（见 reports.py）。
            report_ref=f"synthetic/{job_id}/attempt-{attempt}.json",
            summary="合成样本：本部署 GPU 已满，未运行真实评测。",
        )


# ---------------------------------------------------------------------------
# EvalScope 适配器（桩）
# ---------------------------------------------------------------------------


class _EvalScopeAdapterBase:
    """版本检查与能力检测的公共部分。**不执行任何东西**。"""

    kind = ""
    is_real = True
    synthetic_only = False

    def __init__(
        self,
        *,
        version: str | None = None,
        gate: VersionGate | None = None,
        supported_eval_types: Iterable[str] = (),
        wired: bool = False,
        base_url: str | None = None,
    ) -> None:
        if self.kind not in (RUNNER_EVALSCOPE_HTTP, RUNNER_EVALSCOPE_CLI):
            raise InvalidRequest("adapter kind must be evalscope_http or evalscope_cli")
        self._version = version
        self._gate = gate if gate is not None else VersionGate()
        self._supported = tuple(sorted(set(supported_eval_types)))
        self._wired = bool(wired)
        # 基址在构造时校验（含 :9000 绊线），因此一个指向生产的地址根本建不起来。
        self._base_url = parse_runner_base_url(base_url) if base_url is not None else None

    def capabilities(self) -> CapabilityReport:
        reasons: list[str] = []
        if self._base_url is None:
            reasons.append(ADAPTER_NOT_CONFIGURED)
        if not self._wired:
            reasons.append(ADAPTER_NOT_WIRED)

        supported, version, version_reason = self._gate.evaluate(self._version)
        if version_reason is not None:
            reasons.append(version_reason)

        available = bool(self._wired and self._base_url is not None and supported)
        return CapabilityReport(
            kind=self.kind,
            available=available,
            synthetic_only=False,
            wired=self._wired,
            detected_version=version,
            version_supported=supported,
            supported_eval_types=self._supported,
            reasons=tuple(reasons),
            base_url=redact_base_url(self._base_url),
        )

    def describe(self) -> dict:
        """给运维/DTO 的只读描述：版本检查 + 能力检测，仅此而已。"""
        report = self.capabilities()
        return {
            "contract": "evalscope-adapter/1",
            "capabilities": report.to_dict(),
            "version_gate": {
                "minimum": self._gate.minimum,
                "checked": report.detected_version,
                "supported": report.version_supported,
            },
            "note": (
                "这是一个 **stub（桩）**：它不做版本探测的执行、不发 HTTP、不起子进程，"
                "也不包含任何 EvalScope 命令行。接线的先决条件是运维提供真实的版本与能力事实。"
            ),
        }

    def run(self, job: object, *, attempt: int = 1) -> RunnerSample:
        """桩的执行路径：一律拒绝。"""
        report = self.capabilities()
        detail = ", ".join(report.reasons) or "adapter is not wired"
        raise RunnerUnavailable(f"{self.kind} adapter cannot run evaluations yet: {detail}")


class EvalScopeHttpAdapter(_EvalScopeAdapterBase):
    """EvalScope 的 HTTP 接口适配器桩。"""

    kind = RUNNER_EVALSCOPE_HTTP

    def probe(self) -> None:  # pragma: no cover - 存在的意义是"没有实现"
        raise RunnerUnavailable(
            "capability probing over HTTP is not wired; facts must be injected by the operator"
        )


class EvalScopeCliAdapter(_EvalScopeAdapterBase):
    """EvalScope 的命令行适配器桩。

    **刻意不包含任何命令字符串。** ``run()`` 不会拼出一行 ``evalscope ...``——
    猜一条命令行是本模块唯一能产生的、看起来像进展的错误动作。
    """

    kind = RUNNER_EVALSCOPE_CLI

    def probe(self) -> None:  # pragma: no cover - 存在的意义是"没有实现"
        raise RunnerUnavailable(
            "running an EvalScope CLI probe is not wired; facts must be injected by the operator"
        )


def adapter_by_kind(kind: str, **kwargs: object) -> _EvalScopeAdapterBase:
    if kind == RUNNER_EVALSCOPE_HTTP:
        return EvalScopeHttpAdapter(**kwargs)  # type: ignore[arg-type]
    if kind == RUNNER_EVALSCOPE_CLI:
        return EvalScopeCliAdapter(**kwargs)  # type: ignore[arg-type]
    raise InvalidRequest(f"unknown EvalScope adapter kind: {kind!r}")


def unavailable_runner_facts(*, kind: str | None = None, detail: str | None = None):
    """本部署的真实 runner 事实：不可用。

    返回 :class:`~backend.evaluation.admission.RunnerFacts`，避免本模块反向依赖
    ``admission`` 的具体类型。
    """
    from .admission import RunnerFacts

    return RunnerFacts(
        real_runner_available=False,
        adapter_kind=kind,
        adapter_version=None,
        reasons=(detail or ADAPTER_NOT_WIRED,),
    )


def runner_facts_from(report: CapabilityReport):
    """从一个能力报告推出准入用的事实。

    ``real_runner_available`` 要求 ``available and not synthetic_only``：一个
    **只产出合成样本**的 runner 永远不算"真实 runner"，因此它无法满足准入的第 2 条。
    这个推断是结构性的——mock runner 报 ``available=True``，但它不可能让准入通过。
    """
    from .admission import RunnerFacts

    return RunnerFacts(
        real_runner_available=report.available and not report.synthetic_only,
        adapter_kind=report.kind,
        adapter_version=report.detected_version,
        reasons=report.reasons or (() if report.available else (ADAPTER_NOT_WIRED,)),
    )


def runner_kinds() -> tuple[str, ...]:
    return RUNNER_KINDS


def utc_stamp(moment: datetime) -> str:
    from .models import isoformat

    return isoformat(moment) or ""


__all__ = [
    "ADAPTER_NOT_CONFIGURED",
    "ADAPTER_NOT_WIRED",
    "ADAPTER_KIND_UNKNOWN",
    "ADAPTER_VERSION_UNKNOWN",
    "ADAPTER_VERSION_UNSUPPORTED",
    "EVALSCOPE_PRODUCTION_PORT",
    "LOOPBACK_HOSTS",
    "MIN_SUPPORTED_EVALSCOPE_VERSION",
    "REASON_BAD_SCHEME",
    "REASON_NON_LOOPBACK_REFUSED",
    "REASON_PRODUCTION_PORT_REFUSED",
    "REASON_USERINFO_REFUSED",
    "RUNNER_EVALSCOPE_CLI",
    "RUNNER_EVALSCOPE_HTTP",
    "RUNNER_KINDS",
    "RUNNER_MOCK",
    "CapabilityReport",
    "EvalScopeCliAdapter",
    "EvalScopeHttpAdapter",
    "MockRunner",
    "RunnerSample",
    "VersionGate",
    "adapter_by_kind",
    "parse_runner_base_url",
    "parse_version",
    "redact_base_url",
    "runner_facts_from",
    "runner_kinds",
    "unavailable_runner_facts",
    "utc_stamp",
]
