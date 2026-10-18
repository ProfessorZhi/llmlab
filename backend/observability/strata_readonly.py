"""Read-only Strata JSON status adapter.

The Portal already projects Strata GPU gauges through the fixed metric
projection in :mod:`backend.observability.sources` (``STRATA_HOST_METRICS``).
This module is a separate, read-only **JSON status** channel: it answers
whether the model API is answering, whether it requires a key, and what the
node itself reports about the GPU right now. It is not a second metrics
pipeline and does not replace that projection.

Hard boundaries
---------------
* fixed paths only; the adapter cannot be pointed at an arbitrary URL;
* read-only: there is no ``chat``/``completions`` call and no billing path;
* the transport owns the bearer; this module only learns ``key_present`` and a
  bounded, display-safe fingerprint, never the key;
* ``model_api`` and ``gpu`` are separate facts; neither is inferred from the
  other;
* ``/metrics`` is preferred for GPU, ``/v1/status`` is the fallback and the
  independent second opinion;
* a value that is missing, non-numeric, non-finite or out of range is ``None``;
  a real ``0`` stays ``0``;
* ``gpu.source_present`` says a telemetry source answered; ``gpu.available``
  additionally requires at least one trustworthy numeric GPU gauge;
* 401/403 is a credential problem, 502/503/504 is an unavailable upstream, and
  neither is silently called an outage;
* the payload is JSON; this module never renders a text scrape format.

Endpoint access is part of the contract: ``/health`` is anonymous; the other
three paths require a bearer.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, Protocol

STRATA_SCHEMA = "strata-readonly/1"

#: The only paths this adapter may request. The absolute URL is the transport's
#: concern; it is never supplied by a caller or a query string.
STRATA_PATHS: Mapping[str, str] = MappingProxyType(
    {
        "health": "/health",
        "models": "/v1/models",
        "status": "/v1/status",
        "metrics": "/metrics",
    }
)

#: Access rule per endpoint. The adapter passes this value to the transport so
#: the rule is visible in code and testable, rather than assumed inside the
#: transport.
STRATA_AUTH: Mapping[str, str] = MappingProxyType(
    {
        "health": "anonymous",
        "models": "bearer",
        "status": "bearer",
        "metrics": "bearer",
    }
)

READ_ONLY_PATHS = frozenset(STRATA_PATHS.values())

#: One byte is one byte; memory is reported in bytes and the DTO uses MiB.
_MIB = 1 / 1048576

#: The gauges that make a GPU reading trustworthy. ``source_present`` can be
#: true while every one of these is ``None`` (a source answered but said
#: nothing usable); ``available`` is only true when at least one is numeric.
GPU_TRUST_GAUGES = ("used_mib", "total_mib", "util_pct", "temp_c", "power_w")

#: A fingerprint is display-safe only if it is short and plain. A transport
#: that returns a long/arbitrary string gets ``None`` rather than having that
#: string rendered into a panel.
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class TransportResult:
    """One response from the transport.

    ``error`` is a short, non-secret category (``transport-error``,
    ``payload-not-object``) and is never rendered verbatim to a browser.
    """

    ok: bool
    payload: object
    http_status: int
    error: str | None = None


class StrataTransport(Protocol):
    """Server-side transport that owns the base URL and the bearer.

    The adapter never sees the raw key: it can only ask whether one is present
    and read a bounded, display-safe fingerprint.
    """

    @property
    def key_present(self) -> bool:  # pragma: no cover - protocol
        ...

    @property
    def key_fingerprint(self) -> str:  # pragma: no cover - protocol
        ...

    def get_json(
        self, path: str, *, auth: str, timeout_s: float
    ) -> TransportResult:  # pragma: no cover - protocol
        ...


def _iso_z(moment: float) -> str:
    dt = datetime.fromtimestamp(float(moment), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dict(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


def _text(value: object, max_chars: int = 120) -> str | None:
    """A bounded, control-free display string, or ``None``."""
    if not isinstance(value, str):
        return None
    cleaned = _CONTROL_RE.sub("", value).strip()
    return cleaned[:max_chars] or None


def _display_safe_fingerprint(value: object) -> str | None:
    """Accept only a short, plain fingerprint; never an arbitrary string."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _FINGERPRINT_RE.fullmatch(candidate) else None


def _gauge(value: object, *, lo: float, hi: float, digits: int = 0):
    """A finite numeric gauge inside ``[lo, hi]``; otherwise ``None``.

    ``bool`` is rejected (it is not a measurement), ``NaN``/``inf`` are
    rejected, and a real ``0`` is preserved. ``digits=0`` returns an ``int``.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    if not math.isfinite(number) or number < lo or number > hi:
        return None
    rounded = round(number, digits)
    return int(rounded) if digits == 0 else rounded


def _scaled(value: object, scale: float, *, lo: float, hi: float, digits: int = 1):
    """Scale a raw value before applying the same finite/range rules."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return _gauge(float(value) * scale, lo=lo, hi=hi, digits=digits)


def _source(result: TransportResult) -> dict:
    if result.ok:
        kind = "ok"
    elif result.error == "payload-not-object":
        kind = "contract"
    elif result.error == "transport-error":
        kind = "transport"
    elif result.http_status in (401, 403):
        kind = "credential"
    elif result.http_status in (502, 503, 504):
        kind = "unavailable"
    elif result.http_status:
        kind = "http"
    else:
        kind = "unreachable"
    return {
        "ok": bool(result.ok),
        "http_status": int(result.http_status or 0),
        "error_kind": kind,
    }


def _model_list(payload: object) -> list[dict]:
    doc = _dict(payload)
    if doc is None:
        return []
    items = doc.get("data")
    if not isinstance(items, list):
        items = doc.get("models")
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, dict):
            status = status.get("value")
        meta = _dict(item.get("meta"))
        out.append(
            {
                "id": _text(item.get("id") or item.get("name")),
                "status": _text(status),
                "context": _gauge(
                    meta.get("n_ctx") if meta is not None else None,
                    lo=0,
                    hi=10**9,
                    digits=0,
                ),
            }
        )
    return out


class StrataReadonly:
    """Read-only snapshot of one Strata deployment.

    ``snapshot()`` performs exactly four GETs — one per fixed path — and never a
    completion call. The transport is injected so this module has no socket, no
    URL parameter and no key of its own.
    """

    def __init__(
        self,
        transport: StrataTransport,
        *,
        timeout_s: float = 5.0,
        clock=time.time,
    ) -> None:
        if not callable(getattr(transport, "get_json", None)):
            raise TypeError("transport must implement get_json(path, auth=..., timeout_s=...)")
        self._transport = transport
        self._timeout_s = float(timeout_s)
        self._clock = clock

    def _call(self, name: str) -> TransportResult:
        path = STRATA_PATHS[name]
        try:
            result = self._transport.get_json(
                path, auth=STRATA_AUTH[name], timeout_s=self._timeout_s
            )
        except Exception:  # noqa: BLE001 - a transport fault is an unknown, not a crash
            return TransportResult(False, None, 0, "transport-error")
        if result.ok and not isinstance(result.payload, dict):
            return TransportResult(False, None, result.http_status, "payload-not-object")
        return result

    def snapshot(self) -> dict:
        health = self._call("health")
        models = self._call("models")
        status = self._call("status")
        metrics = self._call("metrics")

        health_doc = _dict(health.payload) if health.ok else None
        models_doc = _dict(models.payload) if models.ok else None
        status_doc = _dict(status.payload) if status.ok else None
        metrics_doc = _dict(metrics.payload) if metrics.ok else None

        model_ok = health.ok and models.ok
        reachable = health.ok or models.ok
        codes = (health.http_status, models.http_status)
        if model_ok:
            error_kind = None
        elif any(code in (401, 403) for code in codes):
            error_kind = "credential"
        elif not reachable and any(code in (502, 503, 504) for code in codes):
            error_kind = "unavailable"
        elif not reachable:
            error_kind = "unreachable"
        else:
            error_kind = "partial"

        api_key = health_doc.get("api_key") if health_doc is not None else None
        loaded = health_doc.get("loaded") if health_doc is not None else None

        model_api = {
            "source": "Strata /health + /v1/models",
            "reachable": reachable,
            "ok": model_ok,
            "auth_required": api_key if isinstance(api_key, bool) else None,
            "loaded": loaded if isinstance(loaded, bool) else None,
            "model_list": _model_list(models_doc),
            "health_http_status": int(health.http_status or 0),
            "models_http_status": int(models.http_status or 0),
            "error_kind": error_kind,
        }

        return {
            "schema": STRATA_SCHEMA,
            "observed_at": _iso_z(self._clock()),
            "key_present": bool(getattr(self._transport, "key_present", False)),
            "key_fingerprint": _display_safe_fingerprint(
                getattr(self._transport, "key_fingerprint", "")
            ),
            "model_api": model_api,
            "gpu": _gpu_block(metrics_doc, status_doc, metrics, status),
            "engine": _engine_block(metrics_doc),
            "host": _host_block(metrics_doc),
            "sources": {
                "health": _source(health),
                "models": _source(models),
                "status": _source(status),
                "metrics": _source(metrics),
            },
            "read_only": True,
        }


def _gpu_block(
    metrics: dict | None,
    status: dict | None,
    metrics_result: TransportResult,
    status_result: TransportResult,
) -> dict:
    hw = _dict(metrics.get("hardware")) if metrics is not None else None
    hw_static = _dict(metrics.get("hardware_static")) if metrics is not None else None
    machine = _dict(status.get("machine")) if status is not None else None
    gpu_status = _dict(machine.get("gpu")) if machine is not None else None

    source_present = hw is not None or gpu_status is not None
    if hw is not None:
        source = "Strata /metrics hardware"
    elif gpu_status is not None:
        source = "Strata /v1/status machine.gpu (fallback: /metrics unavailable)"
    else:
        source = "none"

    block: dict = {
        "sources": {"metrics": _source(metrics_result), "status": _source(status_result)},
        "cross_check": None,
        "source_present": source_present,
        "available": False,
        "source": source,
        "reason": None,
        "name": None,
        "used_mib": None,
        "total_mib": None,
        "util_pct": None,
        "temp_c": None,
        "power_w": None,
        "power_limit_w": None,
        "pcie_gen": None,
        "pcie_gen_max": None,
        "pcie_width": None,
        "pcie_rx_mb": None,
        "pcie_tx_mb": None,
    }

    if hw is not None:
        block.update(
            {
                "name": _text(hw_static.get("gpu_name")) if hw_static is not None else None,
                "used_mib": _scaled(hw.get("gpu_mem_used"), _MIB, lo=0, hi=10**12),
                "total_mib": _scaled(hw.get("gpu_mem_total"), _MIB, lo=0, hi=10**12),
                "util_pct": _gauge(hw.get("gpu_util"), lo=0, hi=100, digits=1),
                "temp_c": _gauge(hw.get("gpu_temp"), lo=-40, hi=150, digits=1),
                "power_w": _gauge(hw.get("gpu_power"), lo=0, hi=2000, digits=1),
                "power_limit_w": _gauge(hw.get("gpu_power_limit"), lo=0, hi=2000, digits=1),
                "pcie_gen": _gauge(hw.get("gpu_pcie_gen"), lo=0, hi=10, digits=0),
                "pcie_gen_max": _gauge(hw.get("gpu_pcie_gen_max"), lo=0, hi=10, digits=0),
                "pcie_width": _gauge(hw.get("gpu_pcie_width"), lo=0, hi=64, digits=0),
                "pcie_rx_mb": _gauge(hw.get("gpu_pcie_rx_mb"), lo=0, hi=10**9, digits=1),
                "pcie_tx_mb": _gauge(hw.get("gpu_pcie_tx_mb"), lo=0, hi=10**9, digits=1),
            }
        )
    elif gpu_status is not None:
        block.update(
            {
                "name": _text(gpu_status.get("name")),
                "used_mib": _gauge(gpu_status.get("used_mib"), lo=0, hi=10**9, digits=1),
                "total_mib": _gauge(gpu_status.get("total_mib"), lo=0, hi=10**9, digits=1),
                "util_pct": _gauge(gpu_status.get("util_pct"), lo=0, hi=100, digits=1),
                "temp_c": _gauge(gpu_status.get("temp_c"), lo=-40, hi=150, digits=1),
                "power_w": _gauge(gpu_status.get("power_w"), lo=0, hi=2000, digits=1),
            }
        )

    block["available"] = any(block[name] is not None for name in GPU_TRUST_GAUGES)
    if not source_present:
        why: list[str] = []
        if not metrics_result.ok:
            why.append("metrics HTTP %s" % (metrics_result.http_status or "unreachable"))
        if not status_result.ok:
            why.append("status HTTP %s" % (status_result.http_status or "unreachable"))
        block["reason"] = "no telemetry endpoint answered (%s)" % (
            "; ".join(why) or "unknown"
        )
    elif not block["available"]:
        block["reason"] = "telemetry source present but no trustworthy numeric gauge"

    # Cross-check is a fact to show, not a gate. Both endpoints report GPU
    # memory at their own instant, so a small gap is normal; only memory is
    # compared because utilisation and power genuinely move between calls.
    if hw is not None and gpu_status is not None:
        a = block.get("used_mib")
        b = _gauge(gpu_status.get("used_mib"), lo=0, hi=10**9, digits=1)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0:
            block["cross_check"] = {
                "metrics_used_mib": a,
                "status_used_mib": b,
                "delta_pct": round(abs(a - b) / b * 100.0, 2),
            }
    return block


def _engine_block(metrics: dict | None) -> dict | None:
    if metrics is None:
        return None
    live = _dict(metrics.get("live")) or {}
    totals = _dict(metrics.get("totals")) or {}
    engine = _dict(metrics.get("engine")) or {}
    return {
        "source": "Strata /metrics",
        "state": _text(live.get("state")),
        "queued": _gauge(live.get("queued"), lo=0, hi=10**9, digits=0),
        "tok_s": _gauge(live.get("tok_s"), lo=0, hi=10**9, digits=1),
        "model": _text(engine.get("model")),
        "version": _text(engine.get("version")),
        "max_context": _gauge(engine.get("max_context"), lo=0, hi=10**9, digits=0),
        "requests": _gauge(totals.get("requests"), lo=0, hi=10**12, digits=0),
        "prompt_tokens": _gauge(totals.get("prompt_tokens"), lo=0, hi=10**15, digits=0),
        "output_tokens": _gauge(totals.get("output_tokens"), lo=0, hi=10**15, digits=0),
    }


def _host_block(metrics: dict | None) -> dict | None:
    if metrics is None:
        return None
    hw = _dict(metrics.get("hardware"))
    if hw is None:
        return None
    hw_static = _dict(metrics.get("hardware_static")) or {}
    return {
        "source": "Strata /metrics hardware",
        "cpu_pct": _gauge(hw.get("cpu"), lo=0, hi=100, digits=1),
        "ram_used_mib": _scaled(hw.get("ram_used"), _MIB, lo=0, hi=10**12),
        "ram_total_mib": _scaled(hw.get("ram_total"), _MIB, lo=0, hi=10**12),
        "disk_read_mb": _gauge(hw.get("disk_read_mb"), lo=0, hi=10**9, digits=1),
        "disk_write_mb": _gauge(hw.get("disk_write_mb"), lo=0, hi=10**9, digits=1),
        "cpu_name": _text(hw_static.get("cpu_name")),
        "cores": _gauge(hw_static.get("cores"), lo=0, hi=4096, digits=0),
        "threads": _gauge(hw_static.get("threads"), lo=0, hi=8192, digits=0),
    }


__all__ = [
    "GPU_TRUST_GAUGES",
    "READ_ONLY_PATHS",
    "STRATA_AUTH",
    "STRATA_PATHS",
    "STRATA_SCHEMA",
    "StrataReadonly",
    "StrataTransport",
    "TransportResult",
]
