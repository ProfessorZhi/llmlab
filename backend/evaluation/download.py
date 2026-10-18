"""报告下载的**有界**接缝：谁能下载、以什么为界、以及这个部署此刻到底能不能。

为什么这一层要有自己的模块
--------------------------

``dto.report_access_block`` 已经把"预览/下载的授权结论"摆进了前端契约，``service`` 也
已经为每一条可见记录算好了 ``download_authorized``。缺的从来不是那个布尔量，而是
**"能下载"到底意味着什么**：一份多大算大、什么后缀允许、文件名怎么给、没接线的时候
该说什么。把这些留给每个调用点自己去想，就会长出第二条规则。

本模块给出**一个**判定：:func:`download_plan`。它消费已经算好的归属与可见性
（``authz``），产出一份 :class:`ReportDownloadPlan` —— 授权结论、来源标签、内容类型、安全的
文件名、**声明**的字节上限，以及此刻能不能真的读到内容。它**不**返回字节：正文只在
:class:`BoundedReportReader` 里流动，而那个读取器在本部署是**关着的**。

两个引用，各归各的
------------------

客户端手里的是**不透明引用**（``provenance.opaque_report_ref``：``evrp-`` + 一段摘要），
它**不是**路径——这正是"前端手里的 id 无法被改写成一条别的路径"的机制。服务端内部用的是
**相对引用**（``reports.normalize_report_ref`` 判过的那个）。

:class:`ReportDownloadPlan` 因此同时持有两者，但只有前者进 DTO：``path_ref`` 是内部字段，
**不**出现在 :meth:`ReportDownloadPlan.to_dict` 里。下发一条相对路径就是下发产物布局，
而 ``authz`` 已经把"拿到指向报告文件的引用"当作一次单独的授权决定。

有界 = 两个方向都关着
---------------------

* **上界**：``max_bytes`` 是声明出来的，而读取器拒绝的是一份**超出**上界的文件——它
  **不**截断。一份被截断的报告比一份被拒绝的报告危险得多：前者看起来像一份完整的报告。
* **默认关**：:class:`BoundedReportReader` 的 ``enabled`` 缺省是 ``False``，``ReportRoot``
  缺省是未接线。因此"本部署不从这里流出报告正文"是一条**结构**事实（
  ``test_static_guarantees`` 里那条"内容读取默认关闭"的断言仍然成立），而不是一句写在
  文档里的承诺。

不存在的报告与别人的报告
------------------------

:func:`download_plan` 的**第一条**判定是可见性，而它的拒绝与
``authz.report_not_available()`` **共用同一个异常**。因此这个端点不会变成"这份报告存在
吗"的预言机：一份不可见的报告与一份不存在的报告，回答逐字节相同。

本模块仍然遵守包级纪律
----------------------

只用标准库、不联网、不起进程、不写盘、不调 ``open()``——读取走
:meth:`pathlib.Path.read_bytes`，且**先**用 ``stat`` 判过上界再读、读后**再**复核一次长度。
这不是"读完了再看大小"：超界的文件根本不进入内存。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import authz
from .authz import ProjectDirectory
from .errors import NotConfigured, ReportDownloadRefused, ReportPathRejected
from .identity import RequesterContext
from .provenance import OPAQUE_REPORT_PREFIX, SOURCE_LABELS, validate_source
from .report_catalog import ReportEntry
from .reports import (
    REASON_CONTENT_DISABLED,
    REASON_FILE_ABSENT,
    REASON_ROOT_NOT_CONFIGURED,
    REASON_SYMLINK_REFUSED,
    REPORT_EXTENSIONS,
    ReportRoot,
    normalize_report_ref,
    reference_digest,
    unconfigured_root,
)

#: 下载契约的版本。与 ``dto.SCHEMA_VERSION`` 是同一种东西：一份独立的、可对上的形状声明。
SCHEMA_VERSION = "eval-report-download/1"

#: 一次下载的字节上限：4 MiB。评测报告是**结构化文本**（见 :data:`DOWNLOAD_CONTENT_TYPES`），
#: 这个数比任何一份真实报告都宽松；它约束的不是"典型大小"，而是"失控时最多看重多少"。
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024

#: 后缀 -> 内容类型。**封闭词表**：不在表里的后缀没有内容类型，也就不会被下载
#: （``reports.normalize_report_ref`` 先把它们挡在引用那一步了）。
#:
#: ``.jsonl`` 在这里是**结构化文本**的一种（``application/x-ndjson``），不是"用量日志"
#: 的代名词——真正的用量日志是别的来源的事，本模块既不认识也不读它。
DOWNLOAD_CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
}

#: 拒绝原因（稳定 token）。与 ``reports`` / ``provenance`` 的 reason 词表同一个风格：
#: 供 UI 与审计区分"是哪一个上界说了不"，但不参与判定。
REASON_DOWNLOAD_DISABLED = "report_download_disabled"
REASON_EXCEEDS_DOWNLOAD_BOUND = "report_exceeds_download_bound"
REASON_NOT_A_REGULAR_FILE = "report_is_not_a_regular_file"
#: 引用的**形状/落点**被判非法（越界、绝对路径、段非法、根目录是 ``/`` 等）。具体是哪一条
#: 由 ``reports`` 决定并进日志；下发给前端的只有这一个封闭 token——一条会把内部判定细节
#: 透出去的原因字符串，本身就是一次信息泄露。
REASON_PATH_REFUSED = "report_reference_refused"

#: ``ReportDownloadPlan.reason`` 的封闭取值域。新增一个值必须同时改
#: ``tests/evaluation/test_v1_report_download.py`` 里那条封闭集合断言——那正是要被逼着
#: 做的一次思考：这个新原因在前端要画成什么？
DOWNLOAD_REASONS: tuple[str, ...] = (
    REASON_CONTENT_DISABLED,
    REASON_ROOT_NOT_CONFIGURED,
    REASON_DOWNLOAD_DISABLED,
    REASON_EXCEEDS_DOWNLOAD_BOUND,
    REASON_NOT_A_REGULAR_FILE,
    REASON_FILE_ABSENT,
    REASON_SYMLINK_REFUSED,
    REASON_PATH_REFUSED,
)


def content_type_for(path_ref: object) -> str | None:
    """一条**相对引用**的内容类型；后缀不在词表里就是 ``None``（不是"猜一个"）。

    ``None`` 是一个**如实**的答案：它意味着"这个面不为这种产物声明类型"，而不是
    "我们忘了填"。调用方据此拒绝，而不是发一个万能的 ``application/octet-stream``——
    后者会把一次拒绝伪装成一次成功的下载。
    """
    normalized = normalize_report_ref(path_ref)
    extension = os.path.splitext(normalized)[1].lower()
    return DOWNLOAD_CONTENT_TYPES.get(extension)


def filename_for(path_ref: object) -> str:
    """下载时给浏览器的文件名。**只有 basename**，不含目录、不含根路径。

    引用在构造期就被 ``normalize_report_ref`` 判过（无 ``..``、无绝对路径、无分隔符），
    因此这里的 basename 不可能是一次穿越。仍然再断言一次：这个值会进
    ``Content-Disposition``，一个带分隔符的文件名在那里是头注入的形状。
    """
    normalized = normalize_report_ref(path_ref)
    basename = normalized.rsplit("/", 1)[-1]
    if not basename or "/" in basename or "\\" in basename:
        raise ReportPathRejected("a report download filename must be a bare basename")
    return basename


@dataclass(frozen=True)
class ReportDownloadPlan:
    """一次报告下载的**判定**。**永远不含正文，也永远不含相对路径与绝对路径。**

    构造即校验：``content_read=True`` 的计划必须带着内容类型、文件名与一个**不超**上界的
    字节数，且不得带拒绝原因；``content_read=False`` 的计划必须带着拒绝原因、且**不得**
    报一个字节数（"读不到"时那个数就是未知，报出来等于假装知道）。这条不变式让"一个
    说不清为什么不能下载的计划"在构造期就被拒绝，而不是等到前端把它画成一块空白。
    """

    #: 客户端手里的**不透明**引用（``evrp-…``）。这是这个 DTO 唯一对外的那一个 id。
    report_ref: str
    #: 服务端内部的相对引用。**只读**，且**不进** :meth:`to_dict`。
    path_ref: str
    source: str
    source_label: str
    authorized: bool
    content_read: bool
    max_bytes: int
    content_type: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    reason: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.report_ref, str) or not self.report_ref.startswith(
            OPAQUE_REPORT_PREFIX
        ):
            raise ReportPathRejected("a download plan is keyed by an opaque report reference")
        # 相对引用在构造期再判一次：这个对象在任何路径上都不该持有一条没判过的路径。
        object.__setattr__(self, "path_ref", normalize_report_ref(self.path_ref))
        validate_source(self.source)
        if self.source_label != SOURCE_LABELS[self.source]:
            raise ReportPathRejected("a plan's source label must match the closed vocabulary")
        if not isinstance(self.authorized, bool) or not isinstance(self.content_read, bool):
            raise ReportPathRejected("a download plan's flags must be booleans")
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool):
            raise ReportPathRejected("a download plan needs an integer byte bound")
        if self.max_bytes <= 0:
            raise ReportPathRejected("a download bound must be positive")

        if self.content_read:
            if not self.authorized:
                raise ReportPathRejected("a readable plan must be authorized")
            if self.reason is not None:
                raise ReportPathRejected("a readable plan must not carry a refusal reason")
            if self.content_type not in set(DOWNLOAD_CONTENT_TYPES.values()):
                raise ReportPathRejected("a readable plan needs a declared content type")
            if not self.filename or "/" in self.filename or "\\" in self.filename:
                raise ReportPathRejected("a readable plan needs a bare filename")
            if self.size_bytes is None or self.size_bytes < 0:
                raise ReportPathRejected("a readable plan needs a non-negative size")
            if self.size_bytes > self.max_bytes:
                raise ReportPathRejected("a readable plan must fit its own declared bound")
        else:
            if self.reason is None:
                raise ReportPathRejected("a non-readable plan must say why")
            if self.reason not in DOWNLOAD_REASONS:
                raise ReportPathRejected(f"unknown download reason: {self.reason!r}")
            if self.size_bytes is not None:
                # 读不到内容时，一个字节数会让人以为"我们知道它多大"。未知就是 None。
                raise ReportPathRejected("a non-readable plan must not assert a size")

        if self.filename is not None and ("/" in self.filename or "\\" in self.filename):
            raise ReportPathRejected("a download filename must be a bare basename")

    def to_dict(self) -> dict:
        """下发给前端的形状。**没有** ``path_ref``，也**没有**任何字节。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "report_ref": self.report_ref,
            "source": self.source,
            "source_label": self.source_label,
            "authorized": self.authorized,
            "content_read": self.content_read,
            "content_type": self.content_type,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "max_bytes": self.max_bytes,
            "reason": self.reason,
            "note": self.note,
        }


@dataclass(frozen=True)
class BoundedRead:
    """一次**有界**读取的结果。拒绝也是一个结果，不是异常。

    异常留给"这件事根本不该被问"（畸形引用、读取器未接线）；一份**具体产物**超界或不在，
    是这个面的正常答案之一，因此它连同原因一起被返回，由 :class:`ReportDownloadPlan`
    决定怎么讲给用户。
    """

    ok: bool
    body: bytes | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.ok:
            if self.reason is not None:
                raise ReportDownloadRefused("a successful read must not carry a reason")
            if self.body is None or self.sha256 is None or self.size_bytes is None:
                raise ReportDownloadRefused("a successful read carries body, digest and size")
            if len(self.sha256) != 64:
                raise ReportDownloadRefused("a read digest is a sha256 hex string")
        else:
            if self.reason not in DOWNLOAD_REASONS:
                raise ReportDownloadRefused(f"unknown read refusal: {self.reason!r}")
            if self.body is not None:
                # 一次被拒的读取**不**留下字节——否则"拒绝"与"截断"会长得一样。
                raise ReportDownloadRefused("a refused read carries no bytes")


class BoundedReportReader:
    """有界只读器：把一份**已授权**的相对引用变成不多于 ``max_bytes`` 的字节。

    它与 ``reports.ReportRoot.read_bytes`` 是**两个决定**，刻意共存：后者说"本部署的报告
    内容读取是关闭的"，且保持关闭；本类说"若某部署**显式**打开读取，它也只能这样读"。
    打开它的第一步必须是有意地把 ``enabled`` 改成 ``True``，而不是发现"好像本来就能读"。
    """

    def __init__(
        self,
        root: ReportRoot | None = None,
        *,
        enabled: bool = False,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
    ) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ReportDownloadRefused("max_bytes must be a positive integer")
        self._root = root if root is not None else unconfigured_root()
        self._enabled = bool(enabled)
        self._max_bytes = max_bytes

    @property
    def configured(self) -> bool:
        return self._root.configured

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def describe(self) -> dict:
        """给 DTO / 运维看的描述。**不回显**根目录路径。"""
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "max_bytes": self._max_bytes,
            "content_types": sorted(DOWNLOAD_CONTENT_TYPES),
            "note": (
                "本部署的报告内容读取是关闭的。"
                if not self._enabled
                else (
                    "报告内容读取已打开；单份产物不超过声明的字节上限，超界即拒绝（不截断）。"
                    if self.configured
                    else "读取器已打开，但报告根目录未接线，因此仍无可读内容。"
                )
            ),
        }

    def probe(self, path_ref: object) -> tuple[int | None, str | None]:
        """**不读内容**地探一次：返回 ``(size_bytes, refusal_reason)``。

        判定顺序与 :meth:`read_bounded` 的前半段完全一致，且共用 :meth:`_locate`——
        两份判定漂移过一次，就会得到"计划说能下载、读取却说不行"这种自相矛盾的答案。

        不接线 / 未启用**不**在这里返回原因：那是**部署**事实，由 :func:`download_plan`
        先答，这里只回答"具体这份产物"。
        """
        located = self._locate(path_ref)
        if isinstance(located, str):
            return None, located
        try:
            size = located.stat().st_size
        except OSError:
            return None, REASON_FILE_ABSENT
        if not located.is_file():
            return None, REASON_NOT_A_REGULAR_FILE
        if size > self._max_bytes:
            # 超界是**拒绝**，不是截断。见模块说明。
            return None, REASON_EXCEEDS_DOWNLOAD_BOUND
        return size, None

    def read_bounded(self, path_ref: object) -> BoundedRead:
        """读一份已授权的相对引用，返回不多于 ``max_bytes`` 的字节。

        **先 stat 再读、读后再复核**：超界的文件根本不进入内存，而一个在 stat 与 read 之间
        长大的文件会被第二次长度检查挡下。
        """
        if not self._enabled:
            raise NotConfigured(REASON_DOWNLOAD_DISABLED)
        if not self._root.configured:
            raise NotConfigured(REASON_ROOT_NOT_CONFIGURED)

        located = self._locate(path_ref)
        if isinstance(located, str):
            return BoundedRead(ok=False, reason=located)

        try:
            size = located.stat().st_size
        except OSError:
            return BoundedRead(ok=False, reason=REASON_FILE_ABSENT)
        if not located.is_file():
            return BoundedRead(ok=False, reason=REASON_NOT_A_REGULAR_FILE)
        if size > self._max_bytes:
            return BoundedRead(ok=False, reason=REASON_EXCEEDS_DOWNLOAD_BOUND)

        body = located.read_bytes()
        if len(body) > self._max_bytes:
            # stat 与 read 之间长大了。拒绝，仍然不截断。
            return BoundedRead(ok=False, reason=REASON_EXCEEDS_DOWNLOAD_BOUND)
        return BoundedRead(
            ok=True,
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )

    def _locate(self, path_ref: object) -> Path | str:
        """把相对引用定位成一个可读路径，或返回一个拒绝原因 token。

        形状（``normalize_report_ref``）与落点（``ReportRoot.resolve``：realpath 包含性、
        软链接拒绝、根目录不是 ``/``）都由 ``reports`` 负责。这里只把它的异常翻译成
        原因 token——**包括** :class:`~backend.evaluation.errors.NotConfigured`：根目录没
        接线时那份引用**不是**畸形，只是无处可解析。
        """
        normalized = normalize_report_ref(path_ref)
        try:
            resolved = self._root.resolve(normalized)
        except NotConfigured:
            return REASON_ROOT_NOT_CONFIGURED
        except ReportPathRejected as exc:
            # 只有 **软链接** 这一条被单独认出来：它是运维最需要区分的一条（根目录里的
            # 一个链接通常是一次事故的现场）。其余一律折进封闭 token。
            if str(exc) == REASON_SYMLINK_REFUSED:
                return REASON_SYMLINK_REFUSED
            return REASON_PATH_REFUSED
        return Path(resolved)


def download_plan(
    requester: RequesterContext,
    entry: ReportEntry,
    projects: ProjectDirectory,
    *,
    reader: BoundedReportReader,
    include_unowned: bool = False,
) -> ReportDownloadPlan:
    """一条目录记录的下载判定。**第一条**是可见性，且拒绝与"不存在"同形。

    ``authorized`` 说的是"允许你发起这次读取"（``authz.may_read_report_reference``，与
    列表里的 ``download_authorized`` 同一个判定）；``content_read`` 说的是"这一份此刻真的
    能读出来吗"。两者分开，是因为前者是一份**策略**结果，后者是一个**部署 + 产物**事实
    ——把后者折进前者，会让"本部署不提供下载"看起来像"你没有权限"。
    """
    allowed, _why = authz.may_read_report_reference(
        requester, entry.record.owner, projects, include_unowned=include_unowned
    )
    if not allowed:
        # 与"报告不存在"共用同一个异常：这个端点不得成为存在性预言机。
        raise authz.report_not_available()

    opaque = entry.ref_id
    path_ref = entry.record.ref
    source = entry.record.source
    content_type = content_type_for(path_ref)
    filename = filename_for(path_ref)

    def refused(reason: str, note: str | None = None) -> ReportDownloadPlan:
        return ReportDownloadPlan(
            report_ref=opaque,
            path_ref=path_ref,
            source=source,
            source_label=SOURCE_LABELS[source],
            authorized=True,
            content_read=False,
            max_bytes=reader.max_bytes,
            content_type=content_type,
            filename=filename,
            reason=reason,
            note=note,
        )

    if not reader.enabled:
        return refused(REASON_DOWNLOAD_DISABLED)
    if not reader.configured:
        return refused(REASON_ROOT_NOT_CONFIGURED)

    size, refusal = reader.probe(path_ref)
    if refusal is not None:
        return refused(refusal)
    if size is None:
        # probe 的契约是"要么给尺寸、要么给原因"。走到这里说明它破了契约——如实报成一个
        # 拒绝，而不是让一个 None 混进"可以读"的那一支。
        return refused(REASON_PATH_REFUSED)
    return ReportDownloadPlan(
        report_ref=opaque,
        path_ref=path_ref,
        source=source,
        source_label=SOURCE_LABELS[source],
        authorized=True,
        content_read=True,
        max_bytes=reader.max_bytes,
        content_type=content_type,
        filename=filename,
        size_bytes=size,
    )


def describe_bounds(reader: BoundedReportReader) -> dict:
    """本部署的下载面描述：上限、允许的后缀与类型、当前开关。运维视角，不含任何主体。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "max_bytes": reader.max_bytes,
        "allowed_extensions": sorted(REPORT_EXTENSIONS),
        "content_types": dict(DOWNLOAD_CONTENT_TYPES),
        "reasons": list(DOWNLOAD_REASONS),
        "reader": reader.describe(),
    }


def reference_fingerprint(path_ref: object) -> str:
    """相对引用的稳定指纹，供下载响应头部使用（``ETag`` / 幂等对照）。

    它算的是**规范化引用字符串**的摘要，不读文件——与 ``reports.reference_digest`` 是
    同一个东西，在这里再导出一次是为了让下载面只 import 一个模块。
    """
    return reference_digest(normalize_report_ref(path_ref))


__all__ = [
    "DOWNLOAD_CONTENT_TYPES",
    "DOWNLOAD_REASONS",
    "MAX_DOWNLOAD_BYTES",
    "REASON_DOWNLOAD_DISABLED",
    "REASON_EXCEEDS_DOWNLOAD_BOUND",
    "REASON_NOT_A_REGULAR_FILE",
    "REASON_PATH_REFUSED",
    "SCHEMA_VERSION",
    "BoundedRead",
    "BoundedReportReader",
    "ReportDownloadPlan",
    "content_type_for",
    "describe_bounds",
    "download_plan",
    "filename_for",
    "reference_fingerprint",
]
