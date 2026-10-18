"""只读报告目录：把若干**来源快照**合成一份按 ACL 过滤的列表。

目录**不做 IO**。它只消费适配器已经产出的 :class:`ReportSourceSnapshot`——网络/磁盘
读取由 ``report_readers.py`` 的适配器负责，而本部署里那些适配器是未接线的。这条分工
让"谁看得见什么"（``authz``）与"来源说了什么"（上游）在目录里各占一层，谁都不越界。

一次读取 = 一份目录
------------------

目录是**只读、一次性**的：构造时给定 ``now``，新鲜度在这一刻算定。这不是缓存，也不
是"会自己刷新"的东西——一个会随时间改变判定的目录，会让"这份报告此刻是否算陈旧"变成
一个取决于读它的人何时读的问题。

新鲜度（stale）
---------------

一条来源说 ``ok``，但它的 ``observed_at`` 已经超出 ``staleness_seconds``，那么它**此刻**
是 ``stale``：记录仍然列出（用户可以看到"最后一次已知的样子"），但整体被打上陈旧标记，
UI 据此清空或降级。``ok`` 却没有 ``observed_at`` 同样按陈旧处理——"我很好但我不知道是
什么时候"，在新鲜度上等于不知道。

0 vs 未知
---------

``empty`` 是"问过了、真的是 0"；``unconfigured`` / ``unreachable`` 是"没问到"。目录把
这个区别原样带进 :meth:`ReportCatalog.source_status`，因此渲染层可以据此决定显示 ``0``
还是 ``None``。目录自己**不**把 ``unconfigured`` 折成 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from . import authz
from .authz import ProjectDirectory
from .errors import ReportSourceMalformed
from .identity import RequesterContext
from .models import MetricSample, isoformat
from .provenance import (
    REASON_NO_VISIBLE_REPORTS,
    REASON_SOURCE_STALE,
    REASON_SOURCE_TIMESTAMP_IN_FUTURE,
    REPORT_DIMENSIONS,
    SOURCE_EMPTY,
    SOURCE_LABELS,
    SOURCE_OK,
    SOURCE_STALE,
    SOURCE_UNCONFIGURED,
    SOURCE_UNREACHABLE,
    ReportRecord,
    ReportSourceSnapshot,
)

#: 默认新鲜度窗口：一小时。超过它的 ``ok`` 快照按 ``stale`` 处理，而不是继续当作新鲜。
DEFAULT_STALENESS_SECONDS = 3600

#: 允许的时钟偏斜（秒）。时间戳落在这个宽限之内仍按"刚刚"处理；**超出**则视为陈旧。
#: 没有这条界，一个远在未来的 ``observed_at`` 会算出负 age，让一份快照**永远**新鲜。
FUTURE_SKEW_SECONDS = 60


@dataclass(frozen=True)
class ReportEntry:
    """目录里的一条报告：记录 + 它在**本次读取**下的新鲜度结论。"""

    record: ReportRecord
    source_state: str
    stale: bool

    @property
    def ref_id(self) -> str:
        return self.record.ref_id

    @property
    def source(self) -> str:
        return self.record.source

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        return self.record.missing_dimensions

    def dimension(self, name: str) -> MetricSample:
        return self.record.dimension(name)

    @property
    def dimensions(self) -> dict[str, MetricSample]:
        """**全部**声明维度，缺失的以显式 ``unknown`` 补齐（绝不是 0）。"""
        return {name: self.record.dimension(name) for name in REPORT_DIMENSIONS}


class ReportCatalog:
    """若干来源快照合成的一份只读目录。"""

    def __init__(
        self,
        snapshots: Iterable[ReportSourceSnapshot] = (),
        *,
        now: datetime,
        staleness_seconds: int = DEFAULT_STALENESS_SECONDS,
    ) -> None:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ReportSourceMalformed("catalog 'now' must be a timezone-aware datetime")
        if not isinstance(staleness_seconds, int) or isinstance(staleness_seconds, bool):
            raise ReportSourceMalformed("staleness_seconds must be an integer")
        if staleness_seconds < 0:
            raise ReportSourceMalformed("staleness_seconds must not be negative")

        self._now = now
        self._staleness_seconds = staleness_seconds

        by_source: dict[str, ReportSourceSnapshot] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, ReportSourceSnapshot):
                raise ReportSourceMalformed("a catalog is built from ReportSourceSnapshot")
            if snapshot.source in by_source:
                # 一个来源在一次读取里只有**一个**结论，否则"这份来源是空的吗"会有两个答案。
                raise ReportSourceMalformed(f"duplicate snapshot for source {snapshot.source!r}")
            by_source[snapshot.source] = snapshot
        self._snapshots = tuple(by_source[source] for source in sorted(by_source))

        entries: list[ReportEntry] = []
        seen: set[str] = set()
        for snapshot in self._snapshots:
            state, stale = self._effective_state(snapshot)
            for record in snapshot.records:
                entry = ReportEntry(record=record, source_state=state, stale=stale)
                if entry.ref_id in seen:
                    # 不透明引用必须唯一：重复意味着一次 lookup 有两个可能的目标。
                    raise ReportSourceMalformed("duplicate report reference in catalog")
                seen.add(entry.ref_id)
                entries.append(entry)
        self._entries = tuple(entries)

    # -- 新鲜度 -------------------------------------------------------------

    def _freshness(self, observed_at: datetime | None) -> tuple[bool, str | None]:
        """一个时间戳相对 ``now`` 的新鲜度。返回 ``(stale, reason)``。

        三种都按陈旧处理，且各自有原因：没有时间戳（"很好但不知道什么时候"）、
        **落点在未来且超出偏斜容差**（负 age 会让一份快照永远新鲜）、以及超出新鲜度窗口。
        """
        if observed_at is None:
            return True, REASON_SOURCE_STALE
        age = (self._now - observed_at).total_seconds()
        if age < -FUTURE_SKEW_SECONDS:
            return True, REASON_SOURCE_TIMESTAMP_IN_FUTURE
        if age > self._staleness_seconds:
            return True, REASON_SOURCE_STALE
        return False, None

    def _effective_state(self, snapshot: ReportSourceSnapshot) -> tuple[str, bool]:
        if snapshot.state != SOURCE_OK:
            # 非 ``ok`` 的状态原样透传（``stale`` 也照旧陈旧）。
            return snapshot.state, snapshot.state == SOURCE_STALE
        stale, _reason = self._freshness(snapshot.observed_at)
        return (SOURCE_STALE, True) if stale else (SOURCE_OK, False)

    # -- 视图 ---------------------------------------------------------------

    @property
    def snapshots(self) -> tuple[ReportSourceSnapshot, ...]:
        return self._snapshots

    def pipeline_status(self) -> tuple[dict, ...]:
        """每条来源的**全局管道健康**：状态、时间、陈旧与否。

        **仅供运维/管理视角**（与 :meth:`scoped_source_status` 相对）。这里的状态是
        全局事实：``ok`` 依构造至少有一条报告。因此把它直接下发会给每一个登录成员一个
        跨租户存在性侧信道——"别人的报告在不在"。用户面必须走 :meth:`scoped_source_status`。
        """
        rows = []
        for snapshot in self._snapshots:
            state, stale = self._effective_state(snapshot)
            rows.append(
                {
                    "source": snapshot.source,
                    "source_label": SOURCE_LABELS[snapshot.source],
                    "state": state,
                    "synthetic": snapshot.synthetic,
                    "observed_at": isoformat(snapshot.observed_at),
                    "stale": stale,
                    "reason": REASON_SOURCE_STALE if stale else snapshot.detail,
                }
            )
        return tuple(rows)

    def _scoped_freshness(self, records: tuple[ReportRecord, ...]) -> tuple[bool, str | None, object]:
        """**某个主体的可见记录**的新鲜度。返回 ``(stale, reason, newest_observed)``。

        时间戳取自这些记录本身（``observed_at``，退回 ``created_at``），而不是快照的
        读取时刻——快照的读取时刻是**全部**租户共享的管道事实，用它会让他人的写入改变
        我的响应。
        """
        stamps = [
            stamp
            for record in records
            for stamp in (record.observed_at, record.created_at)
            if stamp is not None
        ]
        if not stamps:
            return True, REASON_SOURCE_STALE, None
        newest = max(stamps)
        stale, reason = self._freshness(newest)
        return stale, reason, newest

    def scoped_source_status(
        self,
        requester: RequesterContext,
        projects: ProjectDirectory,
        *,
        include_unowned: bool = False,
    ) -> tuple[dict, ...]:
        """**主体范围内**的来源状态。这是唯一可以下发给普通成员的那一份。

        规则：

        * 这个主体在该来源里有可见记录 -> 状态由**这些记录自己**的时间戳决定
          （``ok`` / ``stale``）；
        * 没有可见记录，而来源未接线 / 不可达 -> 如实报 ``unconfigured`` / ``unreachable``
          （这是管道事实，与任何人的数据无关）；
        * 没有可见记录，但来源是接线且可读的 -> 一律 ``empty`` + ``REASON_NO_VISIBLE_REPORTS``。

        最后一条是关键：它**不**区分"来源里真的什么都没有"与"来源里有别人的报告"。因此
        同一名成员在别人新增一份报告前后的响应**逐字节一致**——存在性不再是可推断的。
        """
        visible = self.visible(requester, projects, include_unowned=include_unowned)
        by_source: dict[str, list[ReportRecord]] = {}
        for entry, _reason in visible:
            by_source.setdefault(entry.source, []).append(entry.record)

        rows = []
        for snapshot in self._snapshots:
            records = tuple(by_source.get(snapshot.source, ()))
            if records:
                stale, reason, observed = self._scoped_freshness(records)
                state = SOURCE_STALE if stale else SOURCE_OK
                observed_at = isoformat(observed)
            else:
                state, stale = self._unwired_state(snapshot), False
                observed_at = None
                reason = (
                    snapshot.detail if state in (SOURCE_UNCONFIGURED, SOURCE_UNREACHABLE)
                    else REASON_NO_VISIBLE_REPORTS
                )
            rows.append(
                {
                    "source": snapshot.source,
                    "source_label": SOURCE_LABELS[snapshot.source],
                    "state": state,
                    "synthetic": snapshot.synthetic,
                    "observed_at": observed_at,
                    "stale": stale,
                    "reason": reason,
                    "visible_count": len(records),
                }
            )
        return tuple(rows)

    def _unwired_state(self, snapshot: ReportSourceSnapshot) -> str:
        """无可见记录时，这条来源对外**唯一**安全的说法。

        未接线 / 不可达是管道事实，可以如实说；接线且可读时一律 ``empty``——不区分
        "真的没有"与"有别人的"。``ok`` 永远不在这里出现：它依构造意味着"存在至少一条报告"。
        """
        if snapshot.state in (SOURCE_UNCONFIGURED, SOURCE_UNREACHABLE):
            return snapshot.state
        return SOURCE_EMPTY

    def entries(self) -> tuple[ReportEntry, ...]:
        """**全部**记录（未经 ACL）。只有测试与服务内部聚合用得到它。"""
        return self._entries

    def visible(
        self,
        requester: RequesterContext,
        projects: ProjectDirectory,
        *,
        include_unowned: bool = False,
    ) -> tuple[tuple[ReportEntry, str], ...]:
        """按 ACL 过滤后的记录。返回 ``(entry, 可见原因)``。"""
        result = []
        for entry in self._entries:
            allowed, reason = authz.report_visibility(
                requester,
                entry.record.owner,
                projects,
                include_unowned=include_unowned,
            )
            if allowed:
                result.append((entry, reason))
        return tuple(result)

    def visible_by_source(
        self,
        requester: RequesterContext,
        projects: ProjectDirectory,
        *,
        include_unowned: bool = False,
    ) -> dict[str, int]:
        """每条来源里**对这个调用者可见**的条数。按来源分开，不合并成一个总数。"""
        counts = {snapshot.source: 0 for snapshot in self._snapshots}
        for entry, _reason in self.visible(
            requester, projects, include_unowned=include_unowned
        ):
            counts[entry.source] = counts.get(entry.source, 0) + 1
        return counts

    def lookup(
        self,
        requester: RequesterContext,
        projects: ProjectDirectory,
        ref_id: object,
        *,
        include_unowned: bool = False,
    ) -> tuple[ReportEntry, str]:
        """按不透明引用取一条。

        找不到、或找到了但不可见，**共用同一条 404**：区分二者会让这个端点变成
        "这份报告存在吗"的预言机，而报告的存在性本身就是信息。
        """
        if not isinstance(ref_id, str) or not ref_id.strip():
            raise authz.report_not_available()
        for entry in self._entries:
            if entry.ref_id != ref_id:
                continue
            allowed, reason = authz.report_visibility(
                requester,
                entry.record.owner,
                projects,
                include_unowned=include_unowned,
            )
            if allowed:
                return entry, reason
            raise authz.report_not_available()
        raise authz.report_not_available()


def empty_catalog(*, now: datetime, staleness_seconds: int = DEFAULT_STALENESS_SECONDS) -> ReportCatalog:
    """本部署的缺省目录：一条来源都没接线。"""
    return ReportCatalog((), now=now, staleness_seconds=staleness_seconds)


__all__ = [
    "DEFAULT_STALENESS_SECONDS",
    "ReportCatalog",
    "ReportEntry",
    "empty_catalog",
]
