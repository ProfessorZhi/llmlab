"""§5.3：模型指纹与「移动 / 复制 / 消失」匹配。

设计约束与取舍：

* **禁止全量哈希**。模型动辄几十 GB，扫描器每小时还要对账一次，读全文件既
  慢又会把盘打满。指纹只取「前 1 MiB ‖ 后 1 MiB」，够用来认出「同一个文件被
  挪走了」，又不承担全量读取代价。中段被改而首尾未变时会漏判——这是规格
  明确接受的取舍。
* 指纹里必须带 ``size``：只哈希首尾时，一个被截断/追加的文件可能首尾不变，
  大小是零成本的额外身份位，也让人能一眼看出这条指纹属于多大的文件。
* 读取失败不抛异常。扫描是一趟「尽量多收」的批处理，单个文件的权限问题或
  断链不该让整次上报作废；用一个显式的 ``unreadable:{size}`` 哨兵把「读不了」
  和「内容相同」区分开。
* 本模块只依赖标准库与 :mod:`backend.registry.discovery.types`，不读写数据库，
  也不碰 home 目录——路径全部由调用方给。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Sequence

from backend.registry.discovery.types import (
    CurrentLocation,
    MatchOutcome,
    MatchResult,
    MatchStatus,
    ModelCandidate,
    PreviousLocation,
)

#: 首尾各读多少字节。1 MiB 足以让「同内容不同文件」几乎不可能撞上，又小到
#: 一次对账扫上千个模型也只读十几 MB。
CHUNK = 1 << 20

#: 读不到内容时指纹的前缀。带 ``size`` 后缀，是为了让「同名但大小不同」的
#: 两个坏文件至少还能在展示层区分开。
_UNREADABLE_PREFIX = "unreadable:"

#: 算作「权重分片」的后缀。HF 用 safetensors/bin/pth，diffusers 常用 safetensors，
#: GGUF 目录也可能混进单文件权重。只列这些是为了不把 README、tokenizer 之类
#: 的附带文件算进模型身份，否则换个 tokenizer 就会让模型看起来「变了」。
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf", ".onnx")


def fingerprint_bytes(head: bytes, tail: bytes, size: int) -> str:
    """按「前段 ‖ 后段」的顺序哈希，拼出 ``{size}:{sha256}``。

    ``size`` 由调用方单独给出，而不是用 ``len(head) + len(tail)`` 推算：中段
    本来就没读，真实大小只能来自 ``stat``。这也让同一个文件在不同 ``chunk``
    下仍然得到「大小部分」一致的结果。
    """
    if size < 0:
        raise ValueError("size 不能为负数")
    digest = hashlib.sha256(head)
    digest.update(tail)
    return f"{size}:{digest.hexdigest()}"


def fingerprint_file(path: Path, *, chunk: int = CHUNK) -> str:
    """一个文件的首尾指纹；读不了时返回 ``unreadable:{size}``。

    ``chunk`` 可调只为了测试能用手写的小文件覆盖到各分支，生产永远用默认值。
    """
    if chunk <= 0:
        raise ValueError("chunk 必须为正数")

    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        # 断链或路径不存在时连大小都拿不到，只能报 0。这里不用 lstat：
        # 链接自身的大小对「模型有多大」毫无意义。
        return _UNREADABLE_PREFIX + "0"

    try:
        with path.open("rb") as handle:
            if size <= 2 * chunk:
                # 首尾会在中间重叠（甚至完全重合），分两次 seek 读等于把同一段
                # 读两遍；整读一遍再切，I/O 只发生一次。
                data = handle.read(size)
                head = data[:chunk]
                # 切成「紧跟头部的那一段」而不是再取一次末尾 chunk：两段永不相交，
                # 既不会把同一字节喂给哈希两次，也让 ≤2*chunk 的文件指纹恰好
                # 等于全文 sha256（内容相同的复制品因此能和整文件哈希对上）。
                tail = data[chunk:]
            else:
                head = handle.read(chunk)
                handle.seek(size - chunk)
                tail = handle.read(chunk)
    except OSError:
        # 目录、权限不足、I/O 错误都走这里。大小已知，带上它便于人工判断。
        return f"{_UNREADABLE_PREFIX}{size}"

    return fingerprint_bytes(head, tail, size)


def _config_digest(config: Path) -> str:
    """``config.json`` 的完整 sha256；缺失或不可读时返回空串。

    config 是元数据（§5.4 给它的上限是 256 KB），不在「禁止全量哈希」的
    约束范围内；而且它的每个字节都可能改变模型语义，截断哈希反而更容易
    把两个不同的模型认成同一个。空串与「空文件的 sha256」是两个不同的值，
    所以「没有 config」和「config 是空的」不会混为一谈。
    """
    try:
        digest = hashlib.sha256()
        with config.open("rb") as handle:
            for block in iter(lambda: handle.read(CHUNK), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _collect_weights(root: Path) -> list[tuple[str, int]]:
    """目录下（含子目录）所有权重分片的 ``(相对路径, 大小)``，已排序。

    递归是为了 diffusers：它的权重在 ``unet/``、``vae/`` 等子目录里。相对路径
    用 ``/`` 分隔，保证同一棵树在不同平台上算出同一个清单。
    """
    found: list[tuple[str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            if not name.lower().endswith(_WEIGHT_SUFFIXES):
                continue
            full = Path(dirpath) / name
            relative = full.relative_to(root).as_posix()
            # 只 stat 不打开：清单要的是「有哪些分片、各多大」，读内容既慢
            # 又违反 §5.3 的禁止全量哈希。
            found.append((relative, full.stat().st_size))
    found.sort()
    return found


def fingerprint_hf_dir(path: Path) -> str:
    """HF / diffusers 目录的指纹：``{权重总字节}:{sha256}``。

    哈希输入是「config 的 sha256」+「按名字排序的分片 (路径, 大小)」清单。
    这样加一个分片、改一个分片大小、或改 config 都会换指纹，而顺序、mtime、
    无关文件（README、tokenizer）都不影响——对账不该因为无关文件被碰到就
    认为模型变了。
    """
    # 转成绝对路径再遍历：``relative_to`` 在 root 是 "." 之类相对路径时会抛
    # ValueError，而模型目录的根是扫描器给的，不该假设它一定是绝对的。
    path = Path(os.path.abspath(path))
    try:
        if not path.is_dir():
            return _UNREADABLE_PREFIX + "0"
        weights = _collect_weights(path)
    except OSError:
        # 目录不可读，或某个分片断链：整目录的身份不可信，宁可不认它，
        # 也不要拿残缺清单去和旧指纹比对。
        return _UNREADABLE_PREFIX + "0"

    total = sum(size for _, size in weights)

    digest = hashlib.sha256()
    # 各段带类型前缀并以 NUL/换行分隔：文件名里不可能出现 NUL，因此不存在
    # 「两个不同清单拼出同一串字节」的歧义。
    digest.update(b"config\0")
    digest.update(_config_digest(path / "config.json").encode("ascii"))
    digest.update(b"\n")
    for name, size in weights:
        digest.update(b"weight\0")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")

    return f"{total}:{digest.hexdigest()}"


def fingerprint_model(candidate: ModelCandidate) -> str:
    """按候选类型分派到文件或目录指纹。

    GGUF 是单文件，其余两种是目录。未知类型直接抛 ``ValueError``：这属于
    调用方的编程错误，悄悄退化成「文件指纹」只会产出一堆永远匹配不上的
    垃圾 location。
    """
    if candidate.kind == "gguf":
        return fingerprint_file(candidate.path)
    if candidate.kind in ("hf", "diffusers"):
        return fingerprint_hf_dir(candidate.path)
    raise ValueError(f"未知的模型类型: {candidate.kind!r}")


# --- §5.3 匹配 -------------------------------------------------------------


def _is_identity_fingerprint(value: str) -> bool:
    """这条指纹能否当成身份证据。

    ``unreadable:*`` 是「没读到内容」的哨兵，所有读不了的文件都长得一样。
    拿它去配对会把两个毫不相干的坏模型认成同一个，所以它只用于展示，
    不参与指纹配对（同 inode 的配对不受影响——那是文件系统给的证据）。
    """
    return bool(value) and not value.startswith(_UNREADABLE_PREFIX)


def match_previous(
    previous: Sequence[PreviousLocation], current: Sequence[CurrentLocation]
) -> MatchResult:
    """把本次扫描到的 location 和上一次上报的对应起来。

    顺序就是 §5.3 的顺序，不能交换：先 inode，再指纹。

    * inode 配对不读一个字节，且是**物理**证据：同 (device, inode) 必然是同一个
      文件。先做它，才能把「同盘重命名 / 硬链接」和「另一块盘上的副本」分开；
      如果先按指纹配，同盘重命名会被误判成跨盘移动，而硬链接的两条路径还会
      抢同一个旧 location。
    * 指纹配对时索引里保留**已经用掉的**旧 location。不然「旧位置还在 + 新位置
      是副本」会走到「没有旧 location」分支，被当成一个全新的模型，丢掉旧 id。
    * 多条旧 location 撞同一个指纹说明历史数据本身有重复，任何自动配对都会
      挑错一个，所以只报 ``needs_confirmation``。

    输出顺序是确定的：先按旧路径，再按新路径。调用方（上报、CLI 展示）
    不该依赖集合的遍历顺序。
    """
    # 排序输入，让后续「谁先被配走」不取决于调用方传进来的顺序。
    prevs = sorted(previous, key=lambda loc: (str(loc.path), loc.model_id, loc.location_id))
    currs = sorted(current, key=lambda loc: (str(loc.path), loc.fingerprint, loc.device, loc.inode))

    prev_used = [False] * len(prevs)
    curr_used = [False] * len(currs)
    outcomes: list[MatchOutcome] = []

    def emit(
        status: MatchStatus,
        previous_loc: PreviousLocation | None,
        current_loc: CurrentLocation | None,
        model_id: str | None,
        reason: str = "",
    ) -> None:
        outcomes.append(MatchOutcome(status, previous_loc, current_loc, model_id, reason))

    # ---- 第 1 步：(device, inode) -----------------------------------------
    prev_by_key: dict[tuple[int, int], list[int]] = {}
    for index, loc in enumerate(prevs):
        prev_by_key.setdefault((loc.device, loc.inode), []).append(index)
    curr_by_key: dict[tuple[int, int], list[int]] = {}
    for index, loc in enumerate(currs):
        curr_by_key.setdefault((loc.device, loc.inode), []).append(index)

    for key in sorted(set(prev_by_key) & set(curr_by_key)):
        prev_indexes = prev_by_key[key]
        curr_indexes = curr_by_key[key]

        if len(prev_indexes) == 1:
            old = prevs[prev_indexes[0]]
            same_path = [
                j for j in curr_indexes
                if not curr_used[j] and currs[j].path == old.path
            ]
            if same_path:
                # 路径相同的那条是原样；其余同 inode 的路径是同一份数据的另一个
                # 名字（硬链接），按 §5.3 记成 copied，model_id 沿用旧的。
                unchanged = same_path[0]
                for j in curr_indexes:
                    if curr_used[j]:
                        continue
                    curr_used[j] = True
                    if j == unchanged:
                        emit("unchanged", old, currs[j], old.model_id)
                    else:
                        emit("copied", old, currs[j], old.model_id, "same_inode")
            elif len(curr_indexes) == 1:
                j = curr_indexes[0]
                curr_used[j] = True
                emit("moved_same_disk", old, currs[j], old.model_id)
            else:
                # 旧路径没了，同 inode 下冒出多条新路径：哪条是改名、哪些是副本
                # 无法断定，交给人工。不把 old 挂到任何一条 curr 上，避免读的人
                # 以为已经配好了。
                emit("needs_confirmation", old, None, old.model_id, "ambiguous_inode")
                for j in curr_indexes:
                    curr_used[j] = True
                    emit("needs_confirmation", None, currs[j], None, "ambiguous_inode")
            prev_used[prev_indexes[0]] = True
            continue

        # 多条旧 location 共享同一个 inode：先把路径完全相同的配掉，剩下的
        # 不再猜。
        for i in prev_indexes:
            if prev_used[i]:
                continue
            for j in curr_indexes:
                if curr_used[j]:
                    continue
                if currs[j].path == prevs[i].path:
                    prev_used[i] = True
                    curr_used[j] = True
                    emit("unchanged", prevs[i], currs[j], prevs[i].model_id)
                    break
        for i in prev_indexes:
            if not prev_used[i]:
                prev_used[i] = True
                emit("needs_confirmation", prevs[i], None, prevs[i].model_id, "ambiguous_inode")
        for j in curr_indexes:
            if not curr_used[j]:
                curr_used[j] = True
                emit("needs_confirmation", None, currs[j], None, "ambiguous_inode")

    # ---- 第 2 步：指纹 -----------------------------------------------------
    # 索引覆盖全部旧 location（包括第 1 步已用掉的），见本函数 docstring。
    prev_by_fp: dict[str, list[int]] = {}
    for index, loc in enumerate(prevs):
        if _is_identity_fingerprint(loc.fingerprint):
            prev_by_fp.setdefault(loc.fingerprint, []).append(index)

    pending: dict[str, list[int]] = {}
    for j, loc in enumerate(currs):
        if curr_used[j]:
            continue
        if not _is_identity_fingerprint(loc.fingerprint):
            curr_used[j] = True
            emit("new", None, loc, None, "unreadable")
            continue
        pending.setdefault(loc.fingerprint, []).append(j)

    for fp in sorted(pending):
        group = pending[fp]
        candidates = prev_by_fp.get(fp, [])

        if not candidates:
            for j in group:
                curr_used[j] = True
                emit("new", None, currs[j], None)
            continue

        if len(candidates) > 1:
            # 历史数据里同一指纹对应多条旧 location：不瞎配。
            for i in candidates:
                if not prev_used[i]:
                    prev_used[i] = True
                    emit("needs_confirmation", prevs[i], None, prevs[i].model_id, "ambiguous_fingerprint")
            for j in group:
                curr_used[j] = True
                emit("needs_confirmation", None, currs[j], None, "ambiguous_fingerprint")
            continue

        old_index = candidates[0]
        old = prevs[old_index]

        if prev_used[old_index]:
            # 旧位置这次还在（第 1 步已经配上了），新位置又是同内容 ⇒ 复制。
            for j in group:
                curr_used[j] = True
                emit("copied", old, currs[j], old.model_id, "previous_present")
            continue

        if len(group) == 1:
            j = group[0]
            prev_used[old_index] = True
            curr_used[j] = True
            emit("moved_cross_disk", old, currs[j], old.model_id)
            continue

        # 一条旧 location、多条同指纹的新 location：不知道用户想要哪一条，
        # 旧 id 保持不动，新位置先当新模型，等确认。
        prev_used[old_index] = True
        emit("needs_confirmation", old, None, old.model_id, "multiple_new_locations")
        for j in group:
            curr_used[j] = True
            emit("new", None, currs[j], None)

    # ---- 收尾 --------------------------------------------------------------
    for i, loc in enumerate(prevs):
        if not prev_used[i]:
            prev_used[i] = True
            emit("missing", loc, None, loc.model_id)

    for j, loc in enumerate(currs):
        if not curr_used[j]:
            curr_used[j] = True
            emit("new", None, loc, None)

    outcomes.sort(key=_outcome_sort_key)
    return MatchResult(tuple(outcomes))


def _outcome_sort_key(outcome: MatchOutcome) -> tuple[int, str, str, str, str]:
    """先按旧路径、再按新路径；没有旧的（new）统一排在后面。

    末尾带上 status 与 model_id，是为了同一路径上真有多条结果时也有全序，
    让上报 diff 不会因为两次扫描的插入顺序不同而抖动。
    """
    model_id = outcome.model_id or ""
    if outcome.previous is not None:
        current_path = str(outcome.current.path) if outcome.current is not None else ""
        return (0, str(outcome.previous.path), current_path, outcome.status, model_id)
    current_path = str(outcome.current.path) if outcome.current is not None else ""
    return (1, "", current_path, outcome.status, model_id)
