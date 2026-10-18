"""主机 LIVE 面板的**刷新契约**：一份可执行的参考实现（issue #38）。

背景
----

``web/js/infra/host-status.js`` 目前只在挂载时 ``refresh()`` 一次，然后就停了。
浏览器开着一夜，面板会继续显示当时的数字和 ``LIVE`` 徽标 —— 而服务端每次查询
只保证 ``<=90s`` 的新鲜度。**一张不再更新的快照不是「当前实时」。**

修复落在 UI Lane（那是他们的文件）。本模块交付的是**那份修复的契约**：把它写成
一个确定性、可注入时钟与请求的参考状态机，于是「30 秒一次、同时最多一个请求、
切到后台就停、切回来立刻刷、时间回拨不许当新鲜、失败必须清旧值」这些句子
变成了可以跑的用例，而不是评审时各说各话的散文。

UI Lane 可以逐条对照移植到 JS；``tests/observability/fixtures/host_summary/`` 下的
fixture 是两边**共用**的输入。

为什么是「参考实现」而不是「另一个实现」
----------------------------------------

真正的客户端是 ``host-status.js``。本模块**不**被生产代码 import —— 它是契约的
可执行定义 + 给 JS 测试的边界条件。它刻意做到：

* **纯逻辑**：不 import 任何 IO、不 sleep、不碰 DOM；时间与请求都由调用方注入。
* **同源**：它判 LIVE 用的仍然是 :func:`validate_host_summary`（服务端侧那份
  严格校验），所以「什么算可信」只有一份定义，浏览器端与服务端不会各判各的。

五条硬规则（每条都有回归）
--------------------------

1. **同时最多一个在途请求。** 一个慢响应不许被第二个请求叠上来 —— 叠上来就有
   两个响应竞争同一块 UI 状态。
2. **切到后台就停。** ``hidden`` 期间不发新请求；定时器不空转。
3. **切回来立刻刷。** 可见性恢复时如果已经超过了间隔，**马上**发一次，而不是
   再等满一个周期。
4. **过期就清空。** 每次判定（不只是每次响应）都重算年龄：本地时钟走过
   ``max_sample_age_s`` 之后，哪怕一个字节都没变，也必须从 LIVE 掉到 UNKNOWN。
   这一条正是「挂起一夜」场景的解药。
5. **失败与时间回拨都清旧值。** 请求失败、契约不符、以及**本地时间倒退**，
   一律清掉当前数字并显示 UNKNOWN —— 绝不保留上一个 LIVE 数值。

外加一条竞态规则：**迟到的响应不许覆盖更新的结果**（``seq`` 单调；只有最新一次
请求的响应会被采纳）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .host_projection import (
    DEFAULT_FUTURE_TOLERANCE_S,
    DEFAULT_MAX_SAMPLE_AGE_S,
    validate_host_summary,
)

__all__ = [
    "DISPLAY_LIVE",
    "DISPLAY_UNKNOWN",
    "REASON_CLOCK_ROLLBACK",
    "REASON_CONTRACT",
    "REASON_EXPIRED",
    "REASON_NOT_FETCHED",
    "REASON_REQUEST_FAILED",
    "HostRefreshPolicy",
    "HostRefreshMachine",
]

DISPLAY_LIVE = "live"
DISPLAY_UNKNOWN = "unknown"

#: 还没成功取过任何数据。**不是** ``offline`` —— 「没试过」与「试过但没有源」
#: 是不同的失败，混成一句话会让运维查错方向。
REASON_NOT_FETCHED = "not-yet-fetched"

#: 请求失败（超时 / 断连 / 401 / 403 / 404）。
REASON_REQUEST_FAILED = "request-failed"

#: 响应形状不符契约（合成、来源不对、``sample_at`` 缺失或未来）。
REASON_CONTRACT = "contract-violation"

#: 本地时钟走过样本年龄阈值。
REASON_EXPIRED = "expired"

#: 本地时钟倒退。此刻**任何**基于时间的新鲜度判断都失去依据。
REASON_CLOCK_ROLLBACK = "clock-rollback"


@dataclass(frozen=True)
class HostRefreshPolicy:
    """刷新的有界参数。全部可调，但都有安全默认值。

    ``max_in_flight`` 默认 1 且**不允许**被调成 0 或负数 —— 一个「允许并发」的
    客户端不是更快，是更乱。
    """

    interval_seconds: float = 30.0
    max_in_flight: int = 1
    max_sample_age_s: int = DEFAULT_MAX_SAMPLE_AGE_S
    future_tolerance_s: int = DEFAULT_FUTURE_TOLERANCE_S

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        if self.max_sample_age_s <= 0:
            raise ValueError("max_sample_age_s must be positive")


@dataclass
class _Display:
    """面板此刻应该画什么。``summary`` 只有在 ``state == LIVE`` 时非空。"""

    state: str
    summary: dict | None
    reason: str | None
    sample_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "summary": self.summary,
            "reason": self.reason,
            "sample_at": self.sample_at,
        }


class HostRefreshMachine:
    """面板刷新循环的参考状态机。

    用法（调用方负责真正的时间源与 fetch）::

        machine = HostRefreshMachine(now=datetime.now(timezone.utc))
        seq = machine.tick(datetime.now(timezone.utc))
        if seq is not None:
            try:
                machine.response(now, seq, await fetch(HOST_STATUS_PATH))
            except Exception:
                machine.failure(now, seq, REASON_REQUEST_FAILED)
        render(machine.display(now))

    所有方法都是纯状态转移：同样的调用序列必得同样的结果，无 IO、无定时器。
    """

    def __init__(
        self,
        *,
        now: datetime,
        policy: HostRefreshPolicy | None = None,
    ) -> None:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be a timezone-aware datetime")
        self.policy = policy or HostRefreshPolicy()
        self._now = now.astimezone(timezone.utc)
        self._hidden = False
        self._in_flight = 0
        self._sent_seq = 0
        self._applied_seq = 0
        self._last_request_at: datetime | None = None
        self._display = _Display(state=DISPLAY_UNKNOWN, summary=None, reason=REASON_NOT_FETCHED)
        #: 审计轨迹：每一步决策的理由。测试用它证明「没有偷偷多打一次请求」。
        self.events: list[dict] = []

    # --- 只读视图 ---------------------------------------------------------

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def hidden(self) -> bool:
        return self._hidden

    @property
    def sent_seq(self) -> int:
        return self._sent_seq

    def display(self, now: datetime) -> dict:
        """此刻应该渲染的东西。**每次调用都会重算年龄**（规则 4）。"""
        self._sync_clock(now)
        self._expire_if_aged()
        return self._display.to_dict()

    # --- 事件 -------------------------------------------------------------

    def tick(self, now: datetime) -> int | None:
        """一次定时器到期。返回要发出的请求序号，或 ``None``（不发）。"""
        rollback = self._sync_clock(now)
        if rollback:
            # 时间倒退之后，「这份样本还新不新」这个问题没有答案了 —— 清掉，
            # 并且立刻重取，而不是继续拿倒退后的时钟去算年龄。
            self._clear(REASON_CLOCK_ROLLBACK)
            return self._request(now, forced=True)

        self._expire_if_aged()

        if self._hidden:
            self._note("skip", "hidden")
            return None
        if self._in_flight >= self.policy.max_in_flight:
            # 规则 1：慢响应期间绝不叠第二个请求。
            self._note("skip", "in-flight")
            return None
        if not self._interval_elapsed(now):
            self._note("skip", "interval")
            return None
        return self._request(now)

    def visibility(self, now: datetime, *, hidden: bool) -> int | None:
        """页面前后台切换。恢复可见时若已越过间隔则**立刻**刷新（规则 3）。"""
        self._sync_clock(now)
        self._hidden = bool(hidden)
        self._note("visibility", "hidden" if hidden else "visible")
        if hidden:
            # 规则 2：后台不空转。在途请求让它自然结束（其结果会被采纳）。
            return None
        self._expire_if_aged()
        if self._in_flight >= self.policy.max_in_flight:
            return None
        if not self._interval_elapsed(now):
            return None
        return self._request(now)

    def response(self, now: datetime, seq: int, payload: Any) -> None:
        """收到一次响应。只有**最新一次**请求的响应会被采纳。"""
        self._sync_clock(now)
        if seq != self._sent_seq:
            # 迟到的回包不许覆盖更新的结果（规则 6）。
            self._note("ignored-late", f"seq={seq}")
            return
        self._in_flight = max(0, self._in_flight - 1)

        verdict = validate_host_summary(
            payload,
            now=now.astimezone(timezone.utc),
            max_sample_age_s=self.policy.max_sample_age_s,
            future_tolerance_s=self.policy.future_tolerance_s,
        )
        if not verdict.accepted or not verdict.any_live:
            # 规则 5：契约不符就清旧值，绝不保留上一个 LIVE 数字。
            self._clear(f"{REASON_CONTRACT}:{verdict.reason or 'no-live-metric'}")
            self._note("cleared", self._display.reason)
            return

        self._applied_seq = seq
        # 整份快照**原子替换**：首屏数字与底部健康读数来自同一个响应，不会混批。
        self._display = _Display(
            state=DISPLAY_LIVE,
            summary=dict(payload),
            reason=None,
            sample_at=self._newest_sample_at(verdict),
        )
        self._note("applied", f"seq={seq}")

    def failure(self, now: datetime, seq: int, reason: str = REASON_REQUEST_FAILED) -> None:
        """一次请求失败（超时 / 断连 / 401 / 403 / 404）。"""
        self._sync_clock(now)
        if seq != self._sent_seq:
            self._note("ignored-late", f"seq={seq}")
            return
        self._in_flight = max(0, self._in_flight - 1)
        # 规则 5：失败清旧值。一个陈旧但「看起来是数字」的面板比一个 UNKNOWN
        # 危险得多 —— 它会被当成当前的机器状态。
        self._clear(reason)
        self._note("cleared", reason)

    # --- 内部 -------------------------------------------------------------

    def _sync_clock(self, now: datetime) -> bool:
        """更新本地时钟；返回是否发生了**回拨**。"""
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be a timezone-aware datetime")
        moment = now.astimezone(timezone.utc)
        if moment < self._now:
            self._now = moment
            return True
        self._now = moment
        return False

    def _interval_elapsed(self, now: datetime) -> bool:
        if self._last_request_at is None:
            return True
        elapsed = (now.astimezone(timezone.utc) - self._last_request_at).total_seconds()
        return elapsed >= self.policy.interval_seconds

    def _request(self, now: datetime, *, forced: bool = False) -> int:
        self._sent_seq += 1
        self._in_flight += 1
        self._last_request_at = now.astimezone(timezone.utc)
        self._note("request", f"seq={self._sent_seq}{' forced' if forced else ''}")
        return self._sent_seq

    def _expire_if_aged(self) -> None:
        """规则 4：本地时钟走过阈值即清空 —— 与有没有新响应无关。"""
        if self._display.state != DISPLAY_LIVE or self._display.sample_at is None:
            return
        sample_at = self._parse(self._display.sample_at)
        if sample_at is None:
            self._clear(REASON_EXPIRED)
            return
        age = (self._now - sample_at).total_seconds()
        if age > self.policy.max_sample_age_s:
            self._clear(REASON_EXPIRED)
            self._note("cleared", REASON_EXPIRED)

    def _clear(self, reason: str) -> None:
        self._display = _Display(state=DISPLAY_UNKNOWN, summary=None, reason=reason)

    @staticmethod
    def _parse(value: str) -> datetime | None:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            return None
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _newest_sample_at(verdict) -> str | None:
        stamps = [
            item.sample_at
            for item in verdict.metrics.values()
            if item.live and item.sample_at
        ]
        return max(stamps) if stamps else None

    def _note(self, event: str, detail: str) -> None:
        self.events.append({"event": event, "detail": detail})
