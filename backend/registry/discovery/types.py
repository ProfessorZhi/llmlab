"""S1 的共用类型。

四个纯函数模块（``rules`` / ``facts`` / ``fingerprint`` / ``builds``）之间只通过
本文件的 dataclass 交换数据，互不 import。这样每个模块都能单独测试，也为
§5.6 的上报 payload 提供唯一的形状来源。

这里没有 I/O，除了 ``Path`` 本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

#: §5.2：GGUF 与权重分片的体积下限。低于它的多半是词表、投影矩阵或测试桩。
MIN_MODEL_BYTES = 100 * 1024 * 1024

#: §5.2：名字命中这个前缀的 GGUF 一律不是模型（llama.cpp 的词表文件）。
VOCAB_FILE_PREFIX = "ggml-vocab-"

ModelKind = Literal["gguf", "hf", "diffusers"]

Framework = Literal["llama.cpp", "strata", "vllm", "sglang", "ollama", "unknown"]

#: §5.5：build 支持的权重格式。
FRAMEWORK_FORMATS: Mapping[str, tuple[str, ...]] = {
    "llama.cpp": ("gguf",),
    "strata": ("gguf",),
    "vllm": ("hf",),
    "sglang": ("hf",),
    "ollama": ("ollama",),
}


@dataclass(frozen=True)
class FileStamp:
    """一个文件的识别信息。只来自 ``stat``，从不打开文件。"""

    path: Path
    size: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class ModelCandidate:
    """扫描得到的一个「像模型」的东西。

    ``path`` 对 GGUF 是文件，对 HF / diffusers 是目录。
    ``weights`` 是参与体积统计的权重文件（GGUF 就是它自己；HF 是各分片）。
    """

    path: Path
    kind: ModelKind
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    weights: tuple[FileStamp, ...] = ()
    config: Path | None = None
    mmproj: Path | None = None


@dataclass(frozen=True)
class FlagSpec:
    """引擎 ``--help`` 里的一条参数。"""

    name: str  # 含前导 "--"
    type: str  # "int" | "float" | "str" | "bool" | "choice" | "unknown"
    default: str | None = None
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class BuildCandidate:
    """一个推理引擎的二进制。``enrich`` 之后才带上版本、commit 与 flags。"""

    binary: Path
    framework: Framework
    root: Path | None = None
    version: str | None = None
    git_commit: str | None = None
    cmake: Mapping[str, str] = field(default_factory=dict)
    flags: tuple[FlagSpec, ...] = ()
    formats: tuple[str, ...] = ()
    #: 真正要执行的 argv 是 ``[binary, *argv_prefix, "--help"]``。SGLang 常见的
    #: 安装方式只有包、没有 ``bin/sglang``，那时 binary 是解释器，
    #: argv_prefix 是 ``("-m", "sglang.launch_server")``。
    argv_prefix: tuple[str, ...] = ()

    @property
    def fingerprint_source(self) -> Path:
        """版本缓存的键用的就是二进制本身。"""
        return self.binary


@dataclass(frozen=True)
class ModelFacts:
    """§5.4。缺的字段就是 ``None``，不猜。"""

    format: ModelKind
    architecture: str | None = None
    name: str | None = None
    file_type: str | None = None
    context_length: int | None = None
    block_count: int | None = None
    embedding_length: int | None = None
    head_count: int | None = None
    head_count_kv: int | None = None
    expert_count: int | None = None
    task: str = "unknown"  # chat | vision | embedding | decision | image | video | unknown
    vision: bool = False
    raw: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SkipRecord:
    """一个被跳过的东西和原因。``sync --explain`` 直接用它。"""

    path: Path
    reason: str  # excluded_dir | ignored_glob | vocab_file | below_min_size | duplicate | unreadable


@dataclass(frozen=True)
class ScanResult:
    models: tuple[ModelCandidate, ...] = ()
    builds: tuple[BuildCandidate, ...] = ()
    skipped: tuple[SkipRecord, ...] = ()

    def merge(self, other: "ScanResult") -> "ScanResult":
        """合并多个根的结果；同一路径只保留一次（先到先得）。"""
        seen = {m.path for m in self.models}
        models = self.models + tuple(m for m in other.models if m.path not in seen)
        seen_builds = {b.binary for b in self.builds}
        builds = self.builds + tuple(b for b in other.builds if b.binary not in seen_builds)
        return ScanResult(models, builds, self.skipped + other.skipped)


# --- §5.3 指纹匹配 ---------------------------------------------------------


#: 一个 location 在上一次上报里的样子。
@dataclass(frozen=True)
class PreviousLocation:
    model_id: str
    location_id: str
    path: Path
    fingerprint: str
    device: int
    inode: int


#: 本次扫描看到的一个 location。
@dataclass(frozen=True)
class CurrentLocation:
    path: Path
    fingerprint: str
    device: int
    inode: int


MatchStatus = Literal[
    "new", "unchanged", "moved_same_disk", "moved_cross_disk", "copied", "needs_confirmation", "missing"
]


@dataclass(frozen=True)
class MatchOutcome:
    """一条匹配结论。

    ``previous`` 为 ``None`` 表示这是新出现的 location；``current`` 为 ``None``
    表示旧 location 消失了且没找到新家（``missing``）。
    """

    status: MatchStatus
    previous: PreviousLocation | None = None
    current: CurrentLocation | None = None
    model_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class MatchResult:
    outcomes: tuple[MatchOutcome, ...] = ()

    def by_status(self, status: MatchStatus) -> tuple[MatchOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == status)
