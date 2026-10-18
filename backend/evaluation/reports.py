"""报告引用：只读、相对、可校验的路径，**永不**读取内容。

平台要做的事是"把一份已授权的结果指给用户看"，不是"替用户把报告读出来"。因此本模块
产出的是 :class:`ReportReference`——一个**引用**（相对路径 + 摘要 + 状态），而不是字节。

三层防线
--------

1. **形状（纯字符串，不碰文件系统）**。:func:`normalize_report_ref` 只做路径语法的
   校验：没有绝对路径、没有 ``..``、没有盘符、没有反斜杠、没有 URL、没有控制字符、
   后缀在白名单里。这一层不依赖任何根目录存在，因此可以独立测试。
2. **落点（需要根目录）**。:class:`ReportRoot` 把相对引用解析成绝对路径，并要求
   ``realpath`` 之后仍然在根目录**之内**。软链接逃逸由 ``realpath`` 的包含性检查兜住。
3. **内容（本部署关闭）**。:meth:`ReportRoot.read_bytes` 一律抛
   :class:`~backend.evaluation.errors.NotConfigured`。这不是"还没写"，是一个决定：
   报告内容只经由运维有意的、独立授权的通道流动，不从这个 HTTP 面出去。

为什么要一个 ``reference_sha256``
--------------------------------
摘要算的是**规范化后的相对引用**，不是文件内容（算内容就得读文件，而那正是要避免的）。
它的用途是让前端能稳定地识别"还是同一份引用"，而不需要把根目录的绝对路径下发到浏览器。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import NotConfigured, ReportPathRejected

#: 报告引用的后缀白名单。eval 产物是结构化文本，二进制/图像产物**不在**这里——
#: 一个 ``.png`` 报告引用在本部署里没有合法来源（测试也从不读真实图像）。
REPORT_EXTENSIONS = frozenset({".json", ".jsonl", ".csv", ".txt", ".md"})

#: 单个引用的长度上限。
MAX_REPORT_REF_LENGTH = 512

#: 单段路径的最大长度。
MAX_REPORT_SEGMENT = 96

#: 段内允许的字符。刻意不含空格、引号、百分号、冒号、分号。
_ALLOWED_SEGMENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

# 引用状态取值域。
REFERENCE_KNOWN = "known"
REFERENCE_UNKNOWN = "unknown"
REFERENCE_NOT_CONFIGURED = "not_configured"
REFERENCE_STATES = (REFERENCE_KNOWN, REFERENCE_UNKNOWN, REFERENCE_NOT_CONFIGURED)

# 原因字符串。
REASON_ROOT_NOT_CONFIGURED = "report_root_not_configured"
REASON_FILE_ABSENT = "report_file_absent"
REASON_CONTENT_DISABLED = "report_content_reading_disabled"
REASON_SYMLINK_REFUSED = "report_reference_is_a_symlink"


def normalize_report_ref(raw: object) -> str:
    """校验并规范化一个相对报告引用。任何不合规都抛 :class:`ReportPathRejected`。

    这里**没有**"尽力修正"的分支：不做 ``strip('..')``、不做 ``lstrip('/')``、不做
    ``normpath``。修正路径正是目录穿越得以成立的那一步——一个非法引用是错误，不是
    一个等待被解释成合法引用的意图。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ReportPathRejected("a report reference must be a non-empty string")

    candidate = raw.strip()
    if len(candidate) > MAX_REPORT_REF_LENGTH:
        raise ReportPathRejected(
            f"a report reference must be at most {MAX_REPORT_REF_LENGTH} characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise ReportPathRejected("a report reference must not contain control characters")
    if "\\" in candidate:
        raise ReportPathRejected("a report reference must use forward slashes only")
    if "://" in candidate:
        raise ReportPathRejected("a report reference must not be a URL")
    if candidate.startswith("/") or candidate.startswith("~"):
        raise ReportPathRejected("a report reference must be relative")
    if ":" in candidate:
        # 盘符、NTFS 数据流、以及各种"看起来像协议"的写法，一并挡在外面。
        raise ReportPathRejected("a report reference must not contain a colon")

    segments = candidate.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ReportPathRejected("a report reference must not contain empty or dot segments")
        if len(segment) > MAX_REPORT_SEGMENT:
            raise ReportPathRejected(
                f"a report reference segment must be at most {MAX_REPORT_SEGMENT} characters"
            )
        if segment.startswith("-"):
            raise ReportPathRejected("a report reference segment must not start with '-'")
        if not set(segment) <= _ALLOWED_SEGMENT_CHARS:
            raise ReportPathRejected("a report reference contains a disallowed character")

    extension = os.path.splitext(segments[-1])[1].lower()
    if extension not in REPORT_EXTENSIONS:
        raise ReportPathRejected(
            f"a report reference must end in one of {sorted(REPORT_EXTENSIONS)}"
        )
    return "/".join(segments)


def reference_digest(ref: str) -> str:
    """规范化引用的摘要。**不读文件**，只摘引用的字符串。"""
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReportReference:
    """一份报告的**引用**。绝不包含绝对路径，绝不包含内容。"""

    ref: str
    state: str
    available: bool
    reference_sha256: str
    reason: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.state not in REFERENCE_STATES:
            raise ReportPathRejected(f"unknown report reference state: {self.state!r}")
        if self.available and self.state != REFERENCE_KNOWN:
            raise ReportPathRejected("only a known reference can be available")
        if not isinstance(self.reference_sha256, str) or len(self.reference_sha256) != 64:
            raise ReportPathRejected("a report reference needs a sha256 digest")

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "state": self.state,
            "available": self.available,
            # 本部署的立场：内容不经这个面出去。写进 DTO，因此前端不必猜。
            "content_read": False,
            "content_read_reason": REASON_CONTENT_DISABLED,
            "reference_sha256": self.reference_sha256,
            "reason": self.reason,
            "note": self.note,
        }


def unconfigured_reference(ref: str, *, note: str | None = None) -> ReportReference:
    """根目录未接线时的引用形状。

    这是**本部署的实际状态**：引用本身是合法的（因此 UI 仍能展示"有一个产物"，
    状态是 ``not_configured``），但它不会、也不能被解析成一个路径。
    """
    normalized = normalize_report_ref(ref)
    return ReportReference(
        ref=normalized,
        state=REFERENCE_NOT_CONFIGURED,
        available=False,
        reference_sha256=reference_digest(normalized),
        reason=REASON_ROOT_NOT_CONFIGURED,
        note=note or "报告根目录未接线；此引用不会被解析为路径，也不代表报告不存在。",
    )


class ReportRoot:
    """一个只读的报告根目录。``None`` 表示本部署没有接线。"""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            self._root = None
            self._real_root = None
            return

        # 判据用**传入的**根，不是 realpath 之后的：``os.path.realpath("reports")`` 会
        # 相对当前工作目录解析成一个绝对路径，于是"是不是绝对路径"这个检查永远不会触发。
        # 一个相对根的含义取决于进程当时在哪个目录里——那是安全相关的路径不该有的隐含上下文。
        if not os.path.isabs(os.fspath(root)):
            raise ReportPathRejected("the report root must be an absolute path")

        self._root = os.fspath(root)
        self._real_root = os.path.realpath(self._root)

        # 根目录是文件系统根（``/``）时，包含性检查**恒真**——因为任何路径都在 ``/`` 之下。
        # 那不是"检查失效"，而是那个配置本身把整个文件系统当成了报告目录。拒绝它，
        # 而不是让一个恒真的检查看起来像一道防线。
        if os.path.normpath(self._real_root) == os.sep:
            raise ReportPathRejected(
                "refusing the filesystem root as the report root: containment would be vacuous"
            )

    @property
    def configured(self) -> bool:
        return self._root is not None

    def describe(self) -> dict:
        """给 DTO 用的描述。**不回显**根目录路径。"""
        return {
            "configured": self.configured,
            "content_reading": "disabled",
            "allowed_extensions": sorted(REPORT_EXTENSIONS),
            "note": (
                "报告根目录路径不下发；本部署的报告内容读取是关闭的。"
                if self.configured
                else "报告根目录未接线。"
            ),
        }

    def resolve(self, ref: object) -> str:
        """把相对引用解析成绝对路径，或拒绝。

        三个检查缺一不可：形状（:func:`normalize_report_ref`）、``realpath`` 包含性
        （挡软链接逃逸）、以及最终文件本身不是软链接。
        """
        normalized = normalize_report_ref(ref)
        if self._real_root is None:
            raise NotConfigured(REASON_ROOT_NOT_CONFIGURED)

        candidate = os.path.realpath(os.path.join(self._real_root, *normalized.split("/")))
        # 包含性检查用 realpath 之后的结果：软链接指向根目录之外时，这里就穿了。
        if not self._within_root(candidate):
            raise ReportPathRejected("a report reference must stay inside the report root")

        if os.path.islink(os.path.join(self._real_root, *normalized.split("/"))):
            raise ReportPathRejected(REASON_SYMLINK_REFUSED)
        return candidate

    def _within_root(self, candidate: str) -> bool:
        if self._real_root is None:  # pragma: no cover - 调用方已判过
            return False
        # 用 ``commonpath`` 而不是字符串前缀：``/data/root-evil`` 不该被当成
        # ``/data/root`` 的子路径。它把"看起来像前缀"与"真的是子路径"分开。
        try:
            return os.path.commonpath([self._real_root, candidate]) == self._real_root
        except ValueError:
            # 不同驱动器 / 一个是相对一个绝对：不可能在根之内。
            return False

    def reference(self, ref: object) -> ReportReference:
        """产出引用的 DTO 形状。不读内容，未接线时也不碰文件系统。"""
        normalized = normalize_report_ref(ref)
        digest = reference_digest(normalized)
        if self._real_root is None:
            return unconfigured_reference(normalized)

        try:
            resolved = self.resolve(normalized)
        except ReportPathRejected as exc:
            return ReportReference(
                ref=normalized,
                state=REFERENCE_UNKNOWN,
                available=False,
                reference_sha256=digest,
                reason=str(exc) or "report_reference_rejected",
            )

        if not os.path.isfile(resolved):
            return ReportReference(
                ref=normalized,
                state=REFERENCE_UNKNOWN,
                available=False,
                reference_sha256=digest,
                reason=REASON_FILE_ABSENT,
                note="引用合法但此刻在报告根目录里找不到该文件；这不是一个 0 字节的报告。",
            )

        return ReportReference(
            ref=normalized,
            state=REFERENCE_KNOWN,
            available=True,
            reference_sha256=digest,
        )

    def read_bytes(self, ref: object) -> bytes:
        """读取报告内容：本部署**关闭**。

        刻意留下这个方法而不是"干脆不提供"：接线的第一步必须是有意地把这里改掉，
        而不是发现"好像本来就能读"。
        """
        normalize_report_ref(ref)
        raise NotConfigured(REASON_CONTENT_DISABLED)


def unconfigured_root() -> ReportRoot:
    """本部署的报告根：未接线。"""
    return ReportRoot(None)


__all__ = [
    "MAX_REPORT_REF_LENGTH",
    "MAX_REPORT_SEGMENT",
    "REASON_CONTENT_DISABLED",
    "REASON_FILE_ABSENT",
    "REASON_ROOT_NOT_CONFIGURED",
    "REASON_SYMLINK_REFUSED",
    "REFERENCE_KNOWN",
    "REFERENCE_NOT_CONFIGURED",
    "REFERENCE_STATES",
    "REFERENCE_UNKNOWN",
    "REPORT_EXTENSIONS",
    "ReportReference",
    "ReportRoot",
    "normalize_report_ref",
    "reference_digest",
    "unconfigured_reference",
    "unconfigured_root",
]
