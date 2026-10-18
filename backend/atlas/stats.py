"""分位数与聚合累加器（纯函数）。

**契约核心：** 分位数只从筛选后的**每条原始请求样本**计算。
任何「先算每组 p95、再对 p95 求平均」的做法都是错的，本模块不提供该入口。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .ingest import RawEvent

#: 分位数算法标识，写进 DTO 便于审计复算。
PERCENTILE_METHOD = "linear-interpolation-type7"

#: 「成功」的判定：既是 2xx，outcome 又自报成功。两者不一致时按不成功处理。
SUCCESS_STATUS_MIN = 200
SUCCESS_STATUS_MAX = 299


def percentile(sorted_values: list[float], p: float) -> float | None:
    """从**已升序排序**的原始样本算 p 分位数（线性插值，type 7）。

    空样本返回 ``None``（UNKNOWN），**不是 0**。单样本返回该样本本身。
    """
    n = len(sorted_values)
    if n == 0:
        return None
    if p <= 0:
        return float(sorted_values[0])
    if p >= 1:
        return float(sorted_values[-1])
    rank = (n - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(sorted_values[lower])
    lo = float(sorted_values[lower])
    hi = float(sorted_values[upper])
    return lo + (hi - lo) * (rank - lower)


def is_success(event: RawEvent) -> bool:
    return (
        event.outcome == "success"
        and SUCCESS_STATUS_MIN <= event.status <= SUCCESS_STATUS_MAX
    )


@dataclass
class StatsAccumulator:
    """一组请求的计数 + 原始样本。样本存原始值，聚合时才算分位数。"""

    calls: int = 0
    success: int = 0
    failed: int = 0
    cancelled: int = 0
    throttled_429: int = 0
    outcome_status_mismatch: int = 0
    latency_samples: list[float] = field(default_factory=list)
    latency_missing: int = 0
    ttft_samples: list[float] = field(default_factory=list)
    ttft_missing: int = 0
    ttft_excluded_non_stream: int = 0
    input_tokens_total: int = 0
    input_tokens_seen: int = 0
    input_tokens_missing: int = 0
    output_tokens_total: int = 0
    output_tokens_seen: int = 0
    output_tokens_missing: int = 0

    def add(self, event: RawEvent) -> None:
        self.calls += 1
        http_ok = SUCCESS_STATUS_MIN <= event.status <= SUCCESS_STATUS_MAX
        outcome_ok = event.outcome == "success"
        if http_ok != outcome_ok:
            self.outcome_status_mismatch += 1

        if event.status == 429:
            self.throttled_429 += 1

        if not (http_ok and outcome_ok):
            if event.status != 429:
                if event.outcome == "cancelled":
                    self.cancelled += 1
                else:
                    self.failed += 1
            return

        self.success += 1
        if event.total_ms is not None and event.total_ms > 0:
            self.latency_samples.append(event.total_ms)
        else:
            self.latency_missing += 1

        if event.stream:
            if event.ttft_ms is not None and event.ttft_ms > 0:
                self.ttft_samples.append(event.ttft_ms)
            else:
                self.ttft_missing += 1
        else:
            self.ttft_excluded_non_stream += 1

        self._add_tokens(event)

    def _add_tokens(self, event: RawEvent) -> None:
        if event.input_tokens is None:
            self.input_tokens_missing += 1
        else:
            self.input_tokens_seen += 1
            self.input_tokens_total += event.input_tokens
        if event.output_tokens is None:
            self.output_tokens_missing += 1
        else:
            self.output_tokens_seen += 1
            self.output_tokens_total += event.output_tokens

    # ---- 输出 ----------------------------------------------------------

    def latency_block(self) -> dict:
        samples = sorted(self.latency_samples)
        return {
            "p50_ms": percentile(samples, 0.5),
            "p95_ms": percentile(samples, 0.95),
            "samples": len(samples),
            "basis": "raw-request-samples",
        }

    def ttft_block(self) -> dict:
        samples = sorted(self.ttft_samples)
        return {
            "p50_ms": percentile(samples, 0.5),
            "p95_ms": percentile(samples, 0.95),
            "samples": len(samples),
            "excluded_non_stream": self.ttft_excluded_non_stream,
        }

    def token_block(self) -> dict:
        return {
            "input_total": self.input_tokens_total if self.input_tokens_seen else None,
            "output_total": self.output_tokens_total if self.output_tokens_seen else None,
            "input_missing": self.input_tokens_missing,
            "output_missing": self.output_tokens_missing,
        }

    def counter_block(self) -> dict:
        return {
            "calls": self.calls,
            "success": self.success,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "throttled_429": self.throttled_429,
            # 历史命名：`requests` **等于 `success`**（成功请求数），不是请求总量。
            # 请求总量是 `calls`。保留字段名是为了不破坏既有消费方，口径见
            # docs/atlas-data-contract.md §5.2；回归：RequestsFieldSemanticsTest。
            "requests": self.success,
        }

    def quality_latency(self) -> str:
        if self.success == 0 or not self.latency_samples:
            return "unknown"
        return "partial" if self.latency_missing else "measured"

    def quality_ttft(self) -> str:
        if not self.ttft_samples:
            return "unknown"
        return "partial" if self.ttft_missing else "measured"

    def quality_tokens(self) -> str:
        if self.input_tokens_seen == 0 and self.output_tokens_seen == 0:
            return "unknown"
        if self.input_tokens_missing or self.output_tokens_missing:
            return "partial"
        return "measured"
