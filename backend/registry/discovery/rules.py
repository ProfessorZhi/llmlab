"""§5.2 的纯扫描规则：把一棵目录树折叠成模型、build 与跳过记录。

设计约束与取舍：

* **只 stat，不 open。** 识别一个模型只需要名字、体积与 inode，不需要读内容；
  这样每小时的常规对账不会把几十 GB 的权重页拉进 page cache。
* **目录软链不追，文件软链跟。** HF cache 的 ``snapshots/<rev>/`` 里，权重与
  config 都是指向 ``blobs/`` 的软链；只有跟到目标才能报出真实体积。反过来，
  跟随目录软链既可能把扫描带出 root，也可能制造环，因此默认不追
  （``Limits.follow_symlinks`` 显式打开时才追，并用 (device, inode) 防环）。
* **坏输入只降级，不中断。** 断链、权限不足、扫描期间被删的条目，统一记
  ``unreadable`` 后继续；``max_depth`` / ``max_entries`` 触发时只停止深入，
  已经得到的结果照常返回。
* **输出顺序即契约。** 模型与 build 先按路径排序，再按 (device, inode) 去重，
  “先到先得”因此是确定性的；同一输入跑两次必须得到相等的 ``ScanResult``。
"""

from __future__ import annotations

import fnmatch
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .types import (
    MIN_MODEL_BYTES,
    VOCAB_FILE_PREFIX,
    BuildCandidate,
    FileStamp,
    ModelCandidate,
    ScanResult,
    SkipRecord,
)

__all__ = ["DEFAULT_EXCLUDED_DIRS", "Limits", "scan_tree"]

#: §5.2：任何深度都不进入的目录名。它们是构建缓存或虚拟环境，里面即使有
#: ``*.gguf`` 也只是测试桩，不是用户模型。
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        "__pycache__",
        "CMakeFiles",
    }
)

#: 虚拟环境根目录名。它们整体被排除，但 §5.5 要求仍能从 ``bin/`` 发现引擎，
#: 所以单独对这两个名字做一次浅探测（见 ``_probe_env_bin``）。
_ENV_DIR_NAMES = frozenset({"venv", ".venv"})

#: 参与体积统计的权重后缀（§5.2 的“组件权重”）。GGUF 单独按文件名判定。
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")

#: §5.5：可执行文件名 → 框架。键必须与规范逐字一致。
_BUILD_FRAMEWORKS: dict[str, str] = {
    "llama-server": "llama.cpp",
    "strata": "strata",
    "vllm": "vllm",
    "sglang": "sglang",
    "ollama": "ollama",
}

#: 给 .cache / uv 排除规则用的相对路径常量。
_CACHE_PREFIX = (".cache",)
_HF_CACHE_PREFIX = (".cache", "huggingface", "hub")
_UV_STORE_PREFIX = (".local", "share", "uv")


@dataclass(frozen=True)
class Limits:
    """扫描预算。默认值对应“一个普通家目录一次全量扫描”的经验上限。

    ``max_depth`` 从 root 算 0；``max_entries`` 统计真正交给规则处理的目录项，
    被排除的目录也计数，因为 stat/名字判断本身就有成本。
    """

    max_depth: int = 12
    max_entries: int = 200_000
    follow_symlinks: bool = False


def scan_tree(
    root: Path,
    *,
    ignore_globs: Sequence[str] = (),
    limits: Limits | None = None,
) -> ScanResult:
    """扫描 ``root``，返回模型、build 与被跳过项。

    ``ignore_globs`` 是 ``~/.llmlab-ignore`` 的行内容；每条 glob 同时按相对
    root 的 POSIX 路径和 basename 匹配（``fnmatch`` 语义），命中即跳过。
    """

    return _Scanner(Path(root), tuple(ignore_globs), limits or Limits()).run()


# ---------------------------------------------------------------------------
# 路径规则
# ---------------------------------------------------------------------------


def _cache_path_allowed(rel_parts: tuple[str, ...]) -> bool:
    """§5.2 的 ``~/.cache`` 闸门。

    root 被当作家目录时，``.cache`` 下只有 ``huggingface/hub`` 这条路可走：
    ``.cache`` 本身与 ``.cache/huggingface`` 只是为了下探而放行，其他子项
    （``pip``、``torch``、直接躺在 ``.cache`` 里的文件……）一律 excluded_dir。
    进入 hub 之后恢复普通规则。

    用 root 相对路径而不是绝对路径判断，好处是调用方把任意临时目录当 root
    时规则依旧成立，也不需要读真实的 HOME。
    """

    if not rel_parts or rel_parts[0] != ".cache":
        return True
    if len(rel_parts) >= len(_HF_CACHE_PREFIX):
        return rel_parts[: len(_HF_CACHE_PREFIX)] == _HF_CACHE_PREFIX
    # 只允许“通往 hub 的路径”本身：.cache、.cache/huggingface、.cache/huggingface/hub
    return rel_parts in {_CACHE_PREFIX, _HF_CACHE_PREFIX[:2], _HF_CACHE_PREFIX}


def _is_uv_store(rel_parts: tuple[str, ...]) -> bool:
    """§5.2 单列的 ``~/.local/share/uv``：uv 的工具安装树，不是用户模型。"""

    return len(rel_parts) >= len(_UV_STORE_PREFIX) and rel_parts[: len(_UV_STORE_PREFIX)] == _UV_STORE_PREFIX


def _is_third_party_models(path: Path, name: str, is_dir: bool) -> bool:
    """``third_party/*/models``：任何 third_party 祖先下的 models 子目录。

    这些通常是 llama.cpp / vLLM 仓库自带的测试模型，体积会骗过 100 MB 阈值。
    按祖先名判断而不是按固定深度，兼容 ``third_party/llama.cpp/models`` 与
    更深的 vendored 布局。
    """

    return is_dir and name == "models" and "third_party" in path.parent.parts


def _matches_ignore(rel_parts: tuple[str, ...], name: str, globs: Sequence[str]) -> bool:
    """相对 POSIX 路径与 basename 两套匹配，任一命中即忽略。"""

    rel_posix = "/".join(rel_parts)
    for pattern in globs:
        if not pattern:
            continue
        # 用户常写 ``dir/``；去掉尾斜杠后按同一条规则处理文件与目录。
        pat = pattern.rstrip("/") or pattern
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def _is_gguf_name(name: str) -> bool:
    return name.lower().endswith(".gguf")


def _is_mmproj_name(name: str) -> bool:
    """``mmproj*.gguf`` 是视觉投影辅助文件，永远不单独算模型。"""

    lowered = name.lower()
    return lowered.startswith("mmproj") and lowered.endswith(".gguf")


def _is_weight_name(name: str) -> bool:
    return name.lower().endswith(_WEIGHT_SUFFIXES)


def _stamp(path: Path, st: os.stat_result) -> FileStamp:
    return FileStamp(
        path=path,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        device=st.st_dev,
        inode=st.st_ino,
    )


# ---------------------------------------------------------------------------
# 扫描器
# ---------------------------------------------------------------------------


class _Scanner:
    """一次 ``scan_tree`` 的全部可变状态。

    单独成类只是为了把递归过程中的累加器藏起来；对外仍然只有一个纯函数
    入口，``_Scanner`` 不持有任何全局状态。
    """

    def __init__(self, root: Path, ignore_globs: tuple[str, ...], limits: Limits) -> None:
        self.root = root
        self.ignore_globs = ignore_globs
        self.limits = limits
        self.models: list[ModelCandidate] = []
        self.builds: list[BuildCandidate] = []
        self.skipped: list[SkipRecord] = []
        self._entries_seen = 0
        self._limit_hit = False

    # -- 入口 ---------------------------------------------------------------

    def run(self) -> ScanResult:
        if self.limits.max_entries <= 0:
            return self._finish()
        # 显式栈而不是递归：用户可以把 max_depth 调得很大，极端深的目录不应
        # 变成 RecursionError。
        stack: list[tuple[Path, int, tuple[str, ...]]] = [(self.root, 0, ())]
        while stack and not self._limit_hit:
            dir_path, depth, rel_parts = stack.pop()
            if depth > self.limits.max_depth:
                self._add_skip(dir_path, "max_depth")
                continue
            self._scan_dir(dir_path, depth, rel_parts, stack)
        return self._finish()

    def _finish(self) -> ScanResult:
        # 先按路径排序，再按 (device, inode) 去重：同一文件被硬链接了两次时，
        # 结果不依赖 scandir 的返回顺序。
        ordered_models = sorted(self.models, key=lambda m: str(m.path))
        seen: set[tuple[int, int]] = set()
        models: list[ModelCandidate] = []
        for model in ordered_models:
            key = (model.device, model.inode)
            if key in seen:
                self.skipped.append(SkipRecord(model.path, "duplicate"))
                continue
            seen.add(key)
            models.append(model)

        builds_by_path: dict[str, BuildCandidate] = {}
        for build in sorted(self.builds, key=lambda b: str(b.binary)):
            builds_by_path.setdefault(str(build.binary), build)

        # 同一条路径可能被不同阶段看到（例如 diffusers 的递归体积统计与目录
        # 扫描都会 stat 到坏软链）；完全相同的记录只保留一条。
        unique_skips: dict[tuple[str, str], SkipRecord] = {}
        for record in self.skipped:
            unique_skips.setdefault((str(record.path), record.reason), record)
        skipped = sorted(unique_skips.values(), key=lambda r: (str(r.path), r.reason))

        return ScanResult(
            models=tuple(models),
            builds=tuple(builds_by_path.values()),
            skipped=tuple(skipped),
        )

    # -- 目录遍历 -----------------------------------------------------------

    def _scan_dir(
        self,
        dir_path: Path,
        depth: int,
        rel_parts: tuple[str, ...],
        stack: list[tuple[Path, int, tuple[str, ...]]],
    ) -> None:
        try:
            with os.scandir(dir_path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            # 权限不足、扫描期间被删、root 不是目录，都在这里降级。
            self._add_skip(dir_path, "unreadable")
            return

        files: list[tuple[str, Path, os.stat_result]] = []
        subdirs: list[tuple[Path, tuple[str, ...]]] = []

        for entry in entries:
            if self._entries_seen >= self.limits.max_entries:
                self._limit_hit = True
                self._add_skip(dir_path, "max_entries")
                return
            self._entries_seen += 1

            name = entry.name
            rel_here = rel_parts + (name,)
            path = dir_path / name

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                self._add_skip(path, "unreadable")
                continue

            # .cache / uv 的路径闸门对文件也生效：``~/.cache`` 根下直接放一个
            # 大 gguf 同样不该被当成模型。
            if not _cache_path_allowed(rel_here) or _is_uv_store(rel_here):
                self._add_skip(path, "excluded_dir")
                continue
            if _is_third_party_models(path, name, is_dir):
                self._add_skip(path, "excluded_dir")
                continue

            if is_dir and name in DEFAULT_EXCLUDED_DIRS:
                self._add_skip(path, "excluded_dir")
                # §5.5 与 §5.2 在这里有张力：venv 整体排除，但 vendored 引擎
                # 恰恰在 venv/bin 里。只做一次 bin/ 的浅探测，不碰 lib/ 与
                # site-packages，模型扫描的排除语义保持不变。
                if name in _ENV_DIR_NAMES:
                    self._probe_env_bin(path)
                continue

            if _matches_ignore(rel_here, name, self.ignore_globs):
                self._add_skip(path, "ignored_glob")
                continue

            if is_dir:
                subdirs.append((path, rel_here))
                continue

            try:
                st = entry.stat(follow_symlinks=True)
            except OSError:
                # 断链软链（HF cache 里被 prune 掉的 blob）、权限、竞态。
                self._add_skip(path, "unreadable")
                continue

            if stat.S_ISDIR(st.st_mode):
                # 指向目录的软链：默认既不进入也不报错，它不是坏路径。
                if self.limits.follow_symlinks:
                    subdirs.append((path, rel_here))
                continue
            if not stat.S_ISREG(st.st_mode):
                continue

            files.append((name, path, st))
            if name in _BUILD_FRAMEWORKS and (st.st_mode & 0o111):
                self.builds.append(
                    BuildCandidate(
                        binary=path,
                        framework=_BUILD_FRAMEWORKS[name],  # type: ignore[arg-type]
                        root=None,
                    )
                )

        if self._limit_hit:
            return

        models = self._models_in_dir(dir_path, files)
        if models:
            # 规则 5：命中模型后不再深入该目录，避免把权重分片、optimizer
            # 状态或 diffusers 组件当成独立模型。
            self.models.extend(models)
            return

        # 非 ``venv`` / ``.venv`` 命名的 Python 环境（conda、``envs/xxx``、
        # ``~/.local/share/uv/...``）也按 §5.5 探测引擎。判据只看 ``bin/python``
        # 是否存在，代价是每个含 ``bin`` 的目录多一次 stat；不做任何遍历。
        if (dir_path / "bin" / "python").exists():
            self._probe_env_bin(dir_path)

        for sub_path, sub_rel in subdirs:
            stack.append((sub_path, depth + 1, sub_rel))

    def _probe_env_bin(self, env_path: Path) -> None:
        """在虚拟环境的 ``bin/`` 里找引擎，仅此一层。"""

        bin_dir = env_path / "bin"
        found: set[str] = set()
        try:
            with os.scandir(bin_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            entries = []
        for entry in entries:
            framework = _BUILD_FRAMEWORKS.get(entry.name)
            if framework is None:
                continue
            try:
                st = entry.stat(follow_symlinks=True)
            except OSError:
                self._add_skip(bin_dir / entry.name, "unreadable")
                continue
            if stat.S_ISREG(st.st_mode) and (st.st_mode & 0o111):
                found.add(entry.name)
                self.builds.append(
                    BuildCandidate(
                        binary=bin_dir / entry.name,
                        framework=framework,  # type: ignore[arg-type]
                        root=None,
                    )
                )

        # §5.5：``python -m sglang.launch_server`` 以「包存在」判定。不少 SGLang
        # 安装只有 site-packages 里的包、没有 console script；这里只看目录是否
        # 存在，不进入 site-packages 做任何遍历。
        if "sglang" not in found:
            package = next(iter(sorted(env_path.glob("lib/python*/site-packages/sglang"))), None)
            interpreter = bin_dir / "python"
            if package is not None and package.is_dir() and interpreter.exists():
                self.builds.append(
                    BuildCandidate(
                        binary=interpreter,
                        framework="sglang",
                        root=None,
                        argv_prefix=("-m", "sglang.launch_server"),
                    )
                )

    # -- 目录级模型识别 -----------------------------------------------------

    def _models_in_dir(
        self,
        dir_path: Path,
        files: list[tuple[str, Path, os.stat_result]],
    ) -> list[ModelCandidate]:
        names = {name for name, _, _ in files}

        # a. diffusers：model_index.json 是权威标记，没有体积阈值（§5.2 未给）。
        if "model_index.json" in names:
            dir_stat = self._stat_dir(dir_path)
            if dir_stat is None:
                return []
            weights = self._collect_weights_recursive(dir_path)
            return [
                ModelCandidate(
                    path=dir_path,
                    kind="diffusers",
                    size_bytes=sum(w.size for w in weights),
                    device=dir_stat.st_dev,
                    inode=dir_stat.st_ino,
                    mtime_ns=dir_stat.st_mtime_ns,
                    weights=tuple(weights),
                    config=dir_path / "model_index.json",
                )
            ]

        # b. HF：config.json + 组件权重达到阈值。
        if "config.json" in names:
            weights = sorted(
                (_stamp(path, st) for name, path, st in files if _is_weight_name(name)),
                key=lambda s: str(s.path),
            )
            total = sum(w.size for w in weights)
            if total >= MIN_MODEL_BYTES:
                dir_stat = self._stat_dir(dir_path)
                if dir_stat is None:
                    return []
                return [
                    ModelCandidate(
                        path=dir_path,
                        kind="hf",
                        size_bytes=total,
                        device=dir_stat.st_dev,
                        inode=dir_stat.st_ino,
                        mtime_ns=dir_stat.st_mtime_ns,
                        weights=tuple(weights),
                        config=dir_path / "config.json",
                    )
                ]
            # 只有确实带权重文件的目录才值得记一条；光有 config.json 的源码
            # 目录很常见，逐条上报会淹没 ``sync --explain``。
            if weights:
                self._add_skip(dir_path, "below_min_size")

        # c. GGUF：逐个文件判断；同目录的 mmproj 是附属物，不是模型。
        mmproj = self._pick_mmproj(files)
        found: list[ModelCandidate] = []
        for name, path, st in files:
            if not _is_gguf_name(name):
                continue
            if name.startswith(VOCAB_FILE_PREFIX):
                self._add_skip(path, "vocab_file")
                continue
            if _is_mmproj_name(name):
                continue
            if st.st_size < MIN_MODEL_BYTES:
                self._add_skip(path, "below_min_size")
                continue
            found.append(
                ModelCandidate(
                    path=path,
                    kind="gguf",
                    size_bytes=st.st_size,
                    device=st.st_dev,
                    inode=st.st_ino,
                    mtime_ns=st.st_mtime_ns,
                    weights=(_stamp(path, st),),
                    config=None,
                    mmproj=mmproj,
                )
            )
        return found

    @staticmethod
    def _pick_mmproj(files: list[tuple[str, Path, os.stat_result]]) -> Path | None:
        """同目录有多个 mmproj 时取名字排序第一个，保证可重复。"""

        candidates = sorted(name for name, _, _ in files if _is_mmproj_name(name))
        if not candidates:
            return None
        for name, path, _ in files:
            if name == candidates[0]:
                return path
        return None

    def _stat_dir(self, dir_path: Path) -> os.stat_result | None:
        try:
            return os.stat(dir_path)
        except OSError:
            self._add_skip(dir_path, "unreadable")
            return None

    def _collect_weights_recursive(self, top: Path) -> list[FileStamp]:
        """diffusers 的权重分散在 ``unet/``、``vae/`` 等组件目录，必须递归。

        只 `stat`，不打开；不进入排除目录，也不跟随目录软链——与主扫描保持
        相同的边界，避免体积统计把无关的大文件卷进来。
        """

        stamps: list[FileStamp] = []
        stack: list[tuple[Path, tuple[str, ...]]] = [(top, ())]
        while stack:
            current, rel = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                self._add_skip(current, "unreadable")
                continue
            for entry in entries:
                name = entry.name
                path = current / name
                rel_here = rel + (name,)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    self._add_skip(path, "unreadable")
                    continue
                if is_dir:
                    if name in DEFAULT_EXCLUDED_DIRS:
                        continue
                    if _is_third_party_models(path, name, True):
                        continue
                    if not _cache_path_allowed(rel_here):
                        continue
                    stack.append((path, rel_here))
                    continue
                try:
                    st = entry.stat(follow_symlinks=True)
                except OSError:
                    self._add_skip(path, "unreadable")
                    continue
                if stat.S_ISREG(st.st_mode) and _is_weight_name(name):
                    stamps.append(_stamp(path, st))
        stamps.sort(key=lambda s: str(s.path))
        return stamps

    def _add_skip(self, path: Path, reason: str) -> None:
        self.skipped.append(SkipRecord(path=path, reason=reason))
