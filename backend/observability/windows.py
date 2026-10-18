"""时间窗与桶边界（纯函数）。

**为什么窗口必须有上界：** 时间窗是 Web 层唯一能放大的输入。若窗口无界，一个
``from=1970`` 的请求就能让服务端把整台机器的历史全量拉出来 —— 既是拒绝服务面，
也是「用返回体积推断他人活动」的侧信道。这里给每个桶粒度一个**跨度上界**，
使得窗口内应有的桶数有硬上限（见 :data:`MAX_WINDOW_HOURS` 与 :data:`MAX_POINTS`）。

桶起点一律在 **UTC** 里下取整：DST 跨越时本地日历会给出 23/25 小时的「天」，
把「应有的桶数」变成一个取决于时区的量，于是覆盖率就不再可比。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .errors import InvalidBucket, InvalidWindow

#: 支持的桶粒度。
SUPPORTED_BUCKETS = ("5m", "1h", "1d")

_BUCKET_STEP = {
    "5m": timedelta(minutes=5),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
}

#: 每个粒度的**窗口跨度上界**（小时）。刻意让「点数」而非「天数」成为约束：
#:
#: * ``5m`` × 24h = 288 点 —— 够看一天的火线，不够拉一个月；
#: * ``1h`` × 7d  = 168 点；
#: * ``1d`` × 32d =  32 点。
#:
#: 三者都远小于 :data:`MAX_POINTS`，留了余量给未来的粒度扩展。
MAX_WINDOW_HOURS = {
    "5m": 24,
    "1h": 24 * 7,
    "1d": 24 * 32,
}

#: 任何一次响应里桶数量的硬上限。与 :data:`MAX_WINDOW_HOURS` 叠成两道闸。
MAX_POINTS = 512

def validate_window(
    window_from: datetime,
    window_to: datetime,
    bucket: str,
) -> tuple[datetime, datetime]:
    """校验窗口 / 粒度并归一化到 UTC。失败抛 :class:`InvalidWindow` / :class:`InvalidBucket`。"""
    for value, name in ((window_from, "from"), (window_to, "to")):
        if not isinstance(value, datetime):
            raise InvalidWindow(f"window {name} must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidWindow(f"window {name} must carry an explicit UTC offset")

    if bucket not in SUPPORTED_BUCKETS:
        raise InvalidBucket(f"unsupported bucket: {bucket!r}")

    start = window_from.astimezone(timezone.utc)
    end = window_to.astimezone(timezone.utc)
    if end <= start:
        raise InvalidWindow("window must be a non-empty half-open interval [from, to)")

    hours = (end - start).total_seconds() / 3600
    limit = MAX_WINDOW_HOURS[bucket]
    if hours > limit:
        raise InvalidWindow(
            f"window of {hours:.3f}h exceeds the {limit}h limit for bucket {bucket!r}"
        )

    if len(expected_bucket_starts(start, end, bucket)) > MAX_POINTS:
        raise InvalidWindow(f"window would produce more than {MAX_POINTS} buckets")

    return start, end


def _floor(moment: datetime, bucket: str) -> datetime:
    if bucket == "5m":
        return moment.replace(
            minute=(moment.minute // 5) * 5, second=0, microsecond=0
        )
    if bucket == "1h":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def expected_bucket_starts(
    window_from: datetime, window_to: datetime, bucket: str
) -> list[datetime]:
    """窗口内应有的桶起点（UTC，升序）。

    这是「覆盖率」的分母：``observed / expected``。它**不**是「给缺失的桶补 0」——
    缺失的桶不会出现在响应里，只会让 ``quality = partial``。
    """
    if bucket not in SUPPORTED_BUCKETS:
        raise InvalidBucket(f"unsupported bucket: {bucket!r}")
    step = _BUCKET_STEP[bucket]
    current = _floor(window_from.astimezone(timezone.utc), bucket)
    end = window_to.astimezone(timezone.utc)

    starts: list[datetime] = []
    while current < end:
        starts.append(current)
        current = current + step
        if len(starts) > MAX_POINTS:
            # 防御性上界：validate_window 已经拦过，这里再兜一次，避免被绕过时死循环。
            break
    return starts


def bucket_start_of(moment: datetime, bucket: str) -> datetime:
    """把一个时刻落到它所属桶的起点（UTC）。"""
    if bucket not in SUPPORTED_BUCKETS:
        raise InvalidBucket(f"unsupported bucket: {bucket!r}")
    if not isinstance(moment, datetime):
        raise InvalidWindow("moment must be a datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InvalidWindow("moment must carry an explicit UTC offset")
    return _floor(moment.astimezone(timezone.utc), bucket)


def within_window(moment: datetime, window_from: datetime, window_to: datetime) -> bool:
    """半开区间 ``[from, to)`` 判定。"""
    return window_from <= moment < window_to


__all__ = [
    "MAX_POINTS",
    "MAX_WINDOW_HOURS",
    "SUPPORTED_BUCKETS",
    "bucket_start_of",
    "expected_bucket_starts",
    "validate_window",
    "within_window",
]
