"""分位数（纯函数）。

**契约核心：** 分位数只从筛选后的**每条原始请求样本**计算。任何「先算每组 p95、
再对 p95 求平均」的做法都是错的，本模块不提供该入口 —— 与
``backend/atlas/stats.py`` 同一口径，回归见 ``tests/observability/test_percentiles.py``。
"""

from __future__ import annotations

import math

#: 分位数算法标识，写进 DTO 便于审计复算。
PERCENTILE_METHOD = "linear-interpolation-type7"


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


def percentile_block(samples: list[float], *, missing: int = 0) -> dict:
    """把一串原始样本折成 ``p50 / p95 / samples`` 块。

    ``samples == 0`` 时两个分位数都是 ``None`` —— 这个形状本身就是
    「不许用 0 顶替」的执行点，见 ``dto.validate_*`` 的同名断言。
    """
    ordered = sorted(samples)
    return {
        "p50_ms": percentile(ordered, 0.5),
        "p95_ms": percentile(ordered, 0.95),
        "samples": len(ordered),
        "missing": missing,
        "basis": "raw-request-samples",
    }


__all__ = ["PERCENTILE_METHOD", "percentile", "percentile_block"]
