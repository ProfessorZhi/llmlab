"""发现层的纯函数（设计文档 §5.2–§5.5，实施切片 S1）。

这一层**只读**：给定一个根目录，输出「看起来是模型 / 引擎的东西」，不登记、
不认领、不移动任何文件，也不调用 LLM（决策 D7）。上层的代理（S3）负责把这些
结果通过 socket 上报，守护进程（S2）负责与账本对账——两者都不在这里。

拆成四个互不 import 的模块，是为了每块都能单独测：

* :mod:`rules` —— 走目录，按 §5.2 的排除与阈值给出候选；
* :mod:`facts` —— 打开候选，读 GGUF 头 / HF config，推断 task；
* :mod:`fingerprint` —— §5.3 的指纹与「移动 / 复制 / 歧义 / 缺失」判定；
* :mod:`builds` —— §5.5 的引擎版本、commit、CMake 开关与 ``--help`` 参数。

共用类型在 :mod:`types`，它们与 §5.6 的上报 payload 一一对应。
"""

from .builds import ProbeCache, enrich, find_git_commit, parse_help, probe_version, read_cmake_cache, read_help
from .facts import infer_task, read_diffusers, read_gguf, read_hf, read_model
from .fingerprint import fingerprint_file, fingerprint_hf_dir, fingerprint_model, match_previous
from .rules import DEFAULT_EXCLUDED_DIRS, Limits, scan_tree
from .types import (
    MIN_MODEL_BYTES,
    BuildCandidate,
    CurrentLocation,
    FileStamp,
    FlagSpec,
    MatchOutcome,
    MatchResult,
    ModelCandidate,
    ModelFacts,
    PreviousLocation,
    ScanResult,
    SkipRecord,
)

__all__ = [
    "DEFAULT_EXCLUDED_DIRS",
    "MIN_MODEL_BYTES",
    "BuildCandidate",
    "CurrentLocation",
    "FileStamp",
    "FlagSpec",
    "Limits",
    "MatchOutcome",
    "MatchResult",
    "ModelCandidate",
    "ModelFacts",
    "PreviousLocation",
    "ProbeCache",
    "ScanResult",
    "SkipRecord",
    "enrich",
    "find_git_commit",
    "fingerprint_file",
    "fingerprint_hf_dir",
    "fingerprint_model",
    "infer_task",
    "match_previous",
    "parse_help",
    "probe_version",
    "read_cmake_cache",
    "read_diffusers",
    "read_gguf",
    "read_hf",
    "read_help",
    "read_model",
    "scan_tree",
]
