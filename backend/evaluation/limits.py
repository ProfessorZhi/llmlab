"""提交配额与时间窗。

两个不同的东西，刻意放在一起也只是为了便于对照，它们的语义毫不相干：

**配额（RateLimitPolicy）** 管的是**提交**。单机平台上，一次批量脚本能在一分钟内
堆出几百个排队任务，而队列背后的 runner 只有一个。配额是滑动窗口计数，不是令牌桶：
窗口计数的语义「过去 N 秒里已经提交了 M 次」可以被用户直接理解，也直接对应到数据库里
那几行事件。

**时间窗（TimeWindow）** 管的是**真实派发**。真实评测会占 GPU，而占 GPU 的时段是
运维决定的事，不是用户决定的事。窗口用 UTC 的分钟数表达，允许跨零点（``22:00`` ->
``06:00`` 是合法的）。

两者都**只拒绝，不排队**：配额超了就是 429 + ``Retry-After``，窗口关了就是拒绝，
而不是"先收下、以后再偷偷跑"。一个偷偷跑的评测，比一个被拒绝的评测糟得多——用户会
以为它没跑。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import InvalidRequest

#: 配额事件的作用域名。写进数据库，因此改动要当心兼容。
SCOPE_SUBMIT_PER_PRINCIPAL = "evaluation.submit.principal"
SCOPE_SUBMIT_GLOBAL = "evaluation.submit.global"

_MINUTE_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")

#: 单主体：默认 1 分钟 10 次提交。
DEFAULT_PRINCIPAL_SUBMIT_LIMIT = 10
#: 全部署：默认 1 分钟 40 次提交。单机上这是"整台机器的写入速率"上限。
DEFAULT_GLOBAL_SUBMIT_LIMIT = 40
DEFAULT_RATE_WINDOW_SECONDS = 60
#: 配额窗口的最长保留时间：超过这个时间的窗口事件没有意义，可以清理。
MAX_RATE_WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after: int | None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "remaining": self.remaining,
            "retry_after": self.retry_after,
        }


@dataclass(frozen=True)
class RateLimitPolicy:
    """滑动窗口配额。"""

    limit: int
    window_seconds: int = DEFAULT_RATE_WINDOW_SECONDS
    scope: str = SCOPE_SUBMIT_PER_PRINCIPAL

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise InvalidRequest("rate limit must be a positive integer")
        if (
            isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, int)
            or self.window_seconds < 1
        ):
            raise InvalidRequest("rate window must be a positive integer of seconds")
        if self.window_seconds > MAX_RATE_WINDOW_SECONDS:
            raise InvalidRequest(f"rate window must not exceed {MAX_RATE_WINDOW_SECONDS} seconds")
        if not isinstance(self.scope, str) or not self.scope:
            raise InvalidRequest("rate scope must be a non-empty string")

    @property
    def window_delta(self) -> timedelta:
        return timedelta(seconds=self.window_seconds)

    def window_start(self, now: datetime) -> datetime:
        return now - self.window_delta

    def evaluate(self, *, used: int, now: datetime, oldest_event: datetime | None = None) -> RateLimitDecision:
        """给定窗口内已用次数，判断是否放行。

        ``oldest_event`` 用来把 ``Retry-After`` 算**准**：它是窗口里最早那次提交的
        时刻，那一刻滑出窗口时配额就腾出一个位置。没有它时退化为窗口长度的上界
        ——上界是诚实的，只是不够有用。
        """
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise InvalidRequest("used must be a non-negative integer")

        if used < self.limit:
            return RateLimitDecision(allowed=True, remaining=self.limit - used, retry_after=None)

        if oldest_event is None:
            return RateLimitDecision(allowed=False, remaining=0, retry_after=self.window_seconds)

        # 最早那次提交在 window_seconds 后滑出窗口。向上取整到秒，至少 1。
        frees_at = oldest_event + self.window_delta
        seconds = math.ceil((frees_at - now).total_seconds())
        return RateLimitDecision(allowed=False, remaining=0, retry_after=max(1, seconds))

    def to_dict(self) -> dict:
        return {"limit": self.limit, "window_seconds": self.window_seconds, "scope": self.scope}


@dataclass(frozen=True)
class TimeWindow:
    """允许真实派发的 UTC 窗口（按日重复）。

    ``start_minute`` / ``end_minute`` 是当天的分钟数（0..1439）。``None`` 表示不限制。
    ``start > end`` 表示跨零点。``start == end`` 是**空窗口**——一个"每天都关闭"的
    窗口是合法的配置，而不是一个应当被悄悄解释成"全天开放"的笔误。
    """

    start_minute: int | None = None
    end_minute: int | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("start_minute", self.start_minute), ("end_minute", self.end_minute)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value < 1440):
                raise InvalidRequest(f"{name} must be a minute of the day (0..1439)")
        if (self.start_minute is None) != (self.end_minute is None):
            raise InvalidRequest("a time window needs both ends, or neither")

    @property
    def unrestricted(self) -> bool:
        return self.start_minute is None

    @property
    def always_closed(self) -> bool:
        return not self.unrestricted and self.start_minute == self.end_minute

    def contains(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise InvalidRequest("a time window is evaluated against an aware datetime")
        if self.unrestricted:
            return True
        if self.always_closed:
            return False
        minute = now.astimezone(timezone.utc).hour * 60 + now.astimezone(timezone.utc).minute
        assert self.start_minute is not None and self.end_minute is not None
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        # 跨零点：例如 22:00 -> 06:00
        return minute >= self.start_minute or minute < self.end_minute

    def describe(self) -> str:
        if self.unrestricted:
            return "unrestricted"
        assert self.start_minute is not None and self.end_minute is not None
        if self.always_closed:
            return "closed"
        return (
            f"{self.start_minute // 60:02d}:{self.start_minute % 60:02d}"
            f"-{self.end_minute // 60:02d}:{self.end_minute % 60:02d} UTC"
        )

    def to_dict(self) -> dict:
        return {
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
            "label": self.label,
            "describe": self.describe(),
            "unrestricted": self.unrestricted,
            "always_closed": self.always_closed,
        }

    @classmethod
    def unrestricted_window(cls, *, label: str | None = None) -> "TimeWindow":
        return cls(label=label)

    @classmethod
    def closed(cls, *, label: str | None = None) -> "TimeWindow":
        """一个每天都不开放的窗口。本部署的真实派发就是这个状态。"""
        return cls(start_minute=0, end_minute=0, label=label)

    @classmethod
    def parse(cls, start: str, end: str, *, label: str | None = None) -> "TimeWindow":
        return cls(start_minute=_parse_minute(start, "start"), end_minute=_parse_minute(end, "end"), label=label)


def _parse_minute(raw: object, what: str) -> int:
    if not isinstance(raw, str) or not _MINUTE_RE.match(raw):
        raise InvalidRequest(f"{what} must look like HH:MM (24-hour UTC)")
    hours, minutes = raw.split(":")
    return int(hours) * 60 + int(minutes)


__all__ = [
    "DEFAULT_GLOBAL_SUBMIT_LIMIT",
    "DEFAULT_PRINCIPAL_SUBMIT_LIMIT",
    "DEFAULT_RATE_WINDOW_SECONDS",
    "MAX_RATE_WINDOW_SECONDS",
    "SCOPE_SUBMIT_GLOBAL",
    "SCOPE_SUBMIT_PER_PRINCIPAL",
    "RateLimitDecision",
    "RateLimitPolicy",
    "TimeWindow",
]
