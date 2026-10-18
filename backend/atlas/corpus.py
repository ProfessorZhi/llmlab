"""受信 Key Directory 与 Gateway 语料的**接入 seam**（离线、有界、fail-closed）。

``docs/atlas-data-contract.md`` §8 把两件事列为「仍未接线」：

* §8.1 **``KeyDirectory`` 的真实来源。** 现在由调用方注入内存对象。生产需要一个
  经审批的配置/DB 适配器，并带轮换与吊销流程。**当前没有任何真实 key 目录。**
* §8.3 **``usage.jsonl`` 的读取与投递。** 本模块只接受调用方给的**字符串**。
  真实接入需要 Gateway 侧的导出/流式读取与背压策略。

本模块交付这两条 seam 的**服务端一侧**，让 BFF 装配方可以把
「一份受信文档 + 一个语料路径」变成 ``KeyDirectory`` 与 ``gateway_text``，
而这条链路上的每一步都可以在**不接触 Dell**的前提下被测试。

四条贯穿全模块的纪律
--------------------

**一、有界，而且是在读进内存之前就有界。** ``ingest`` 的 ``max_bytes`` 只能拦住
已经变成 ``str`` 的东西 —— 用它去兜一个不受信的导出文件，等于先把整个文件读进
内存再问「是不是太大了」。所以这里的尺寸门在 ``stat`` 上，**先于** ``open``；
读取本身也只取 ``max_bytes + 1`` 个字节，多出来的那一个字节就是「stat 与 read
之间文件长大了」的证据。

**二、fail closed，不给部分结果。** 一份读了一半的目录会让「某个 key 归谁」
悄悄从「一个名字」变成「不知道」，而下游每一条授权判定都建立在这个答案上。
所以解析、越界、超限、编码这任何一种失败都抛 :class:`SourceUnavailable`，
不返回「能读多少算多少」。

**三、路径受限于装配方指定的那棵树。** 符号链接先解析再判归属：一个位于 root 内、
却指向 root 外的链接，与 ``../`` 穿越是同一件事。判断依据是 ``realpath`` 之后的
包含关系，不是字符串前缀。

**四、不读真实数据。** 本模块不指向任何真实 ``usage.jsonl``，也不内置任何真实
Key Directory。生产由装配方给路径；测试用临时目录里的合成文档。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .dto import Provenance
from .errors import InvalidRequest, SourceUnavailable
from .identity import GatewayKeyBinding, KeyDirectory

#: 受信目录文档的模式版本。字段增删必须同步本常量与 docs/atlas-data-contract.md。
KEY_DIRECTORY_SCHEMA_VERSION = "atlas-key-directory/1"

#: 目录文档的顶层字段白名单 —— 多一个没人审批过的字段就是一份没审批过的文档。
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "revision", "bindings"})

#: 单条绑定的字段白名单。``x_principal`` 这类自报身份字段在这里就会被挡下，
#: 而不是读进来之后「记得不要用」。
_BINDING_FIELDS = frozenset({"key_id", "principal", "project", "workload", "revoked"})

# --- 稳定的失败原因码 --------------------------------------------------------
#
# 统一前缀 ``source.``：目录与语料共用一套词汇，因为装配方关心的是「这条来源
# 为什么不可用」，而不是「这是哪条来源」—— 后者由调用点已经决定了。
REASON_INVALID_DOCUMENT = "source.invalid_document"
REASON_SCHEMA_MISMATCH = "source.schema_mismatch"
REASON_TOO_MANY_BINDINGS = "source.too_many_bindings"
REASON_DUPLICATE_KEY_ID = "source.duplicate_key_id"
REASON_TOO_LARGE = "source.too_large"
REASON_FILE_MISSING = "source.file_missing"
REASON_NOT_A_REGULAR_FILE = "source.not_a_regular_file"
REASON_OUTSIDE_ROOT = "source.outside_root"
REASON_NOT_UTF8 = "source.not_utf8"
REASON_UNREADABLE = "source.unreadable"


@dataclass(frozen=True)
class DirectoryLimits:
    """受信目录文档的滥用上限。默认值即产品默认，测试可覆写。"""

    max_bytes: int = 4 * 1024 * 1024
    max_bindings: int = 50_000
    max_field_len: int = 512


@dataclass(frozen=True)
class FileFacts:
    """一次 ``stat`` 的结果。与 ``auth_linux.securefile`` 同形，刻意不共用该类 ——
    目录归属（``backend/atlas/**`` 由本 Lane 排他拥有）比少写一个 dataclass 重要。"""

    exists: bool
    is_regular: bool
    size: int


def default_fs_probe(path: Path) -> FileFacts:
    """默认探针。测试可注入替身，从而在没有 root、也不创建大文件的前提下
    验证「尺寸门先于读取」。"""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return FileFacts(exists=False, is_regular=False, size=0)
    except OSError as exc:  # 权限等：不是「不存在」，是「问不出来」
        raise SourceUnavailable(REASON_UNREADABLE) from exc
    return FileFacts(
        exists=True,
        is_regular=stat.S_ISREG(info.st_mode),
        size=info.st_size,
    )


def _resolve_within(path: str | Path, root: str | Path) -> Path:
    """解析 ``path`` 并断言它落在 ``root`` 之内；越界抛 :class:`SourceUnavailable`。

    判断依据是 ``realpath`` 之后的**包含关系**：符号链接在这里被解开，所以
    「root 内的链接指向 root 外」与 ``../`` 穿越是同一条拒绝。
    """
    target = Path(os.path.realpath(str(path)))
    real_root = Path(os.path.realpath(str(root)))
    if target != real_root and real_root not in target.parents:
        raise SourceUnavailable(REASON_OUTSIDE_ROOT)
    return target


def read_bounded_text(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> str:
    """在 ``root`` 之内**有界**地读取一个 UTF-8 文本文件。

    顺序即边界：``realpath 归属`` -> ``stat 尺寸`` -> ``open 读取``。
    尺寸不合格时**根本不打开文件** —— 这正是「有界」区别于「读完再判断」的地方。
    """
    target = _resolve_within(path, root)
    facts = fs_probe(target)
    if not facts.exists:
        raise SourceUnavailable(REASON_FILE_MISSING)
    if not facts.is_regular:
        raise SourceUnavailable(REASON_NOT_A_REGULAR_FILE)
    if facts.size > max_bytes:
        raise SourceUnavailable(REASON_TOO_LARGE)

    try:
        with open(target, "rb") as handle:
            # 多读一个字节：读到第 max_bytes+1 个，就说明 stat 与 read 之间文件长大了
            # （TOCTOU）。此时宁可拒绝，也不要读进一段没有上界的字节。
            payload = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SourceUnavailable(REASON_UNREADABLE) from exc

    if len(payload) > max_bytes:
        raise SourceUnavailable(REASON_TOO_LARGE)

    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnavailable(REASON_NOT_UTF8) from exc


@dataclass(frozen=True)
class LoadedKeyDirectory:
    """一份通过校验的受信目录：目录本体 + 供审计与轮换比对的两个计数。"""

    revision: str
    directory: KeyDirectory
    key_count: int
    revoked_count: int


def _is_bounded_text(value: Any, max_len: int) -> bool:
    """非空、去空白后仍非空、且不超过上限的字符串。``bool`` 不是字符串，天然被排除。"""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_len


def _parse_binding(raw: Any, limits: DirectoryLimits) -> GatewayKeyBinding:
    if not isinstance(raw, dict):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)
    if set(raw) - _BINDING_FIELDS:
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)

    key_id = raw.get("key_id")
    principal = raw.get("principal")
    project = raw.get("project")
    for value in (key_id, principal, project):
        if not _is_bounded_text(value, limits.max_field_len):
            raise SourceUnavailable(REASON_INVALID_DOCUMENT)

    workload = raw.get("workload")
    if workload is not None and not _is_bounded_text(workload, limits.max_field_len):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)

    revoked = raw.get("revoked", False)
    # ``isinstance(revoked, bool)`` 而不是 ``bool(revoked)``：字符串 ``"false"``
    # 是真值，用它当吊销标记会让一把已吊销的 key 复活。
    if not isinstance(revoked, bool):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)

    return GatewayKeyBinding(
        key_id=key_id.strip(),
        principal=principal.strip(),
        project=project.strip(),
        workload=workload.strip() if workload is not None else None,
        revoked=revoked,
    )


def parse_key_directory_document(
    text: str,
    *,
    limits: DirectoryLimits = DirectoryLimits(),
) -> LoadedKeyDirectory:
    """把一份 ``atlas-key-directory/1`` 文档解析成受信目录。失败一律 fail closed。"""
    if not isinstance(text, str):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)

    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT) from None

    if not isinstance(document, dict):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)
    if set(document) - _TOP_LEVEL_FIELDS:
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)
    if document.get("schema_version") != KEY_DIRECTORY_SCHEMA_VERSION:
        # 版本不符必须拒绝：按 v1 的规则读 v2 的文档会得到一份「看起来成功了」的目录。
        raise SourceUnavailable(REASON_SCHEMA_MISMATCH)

    revision = document.get("revision")
    if not _is_bounded_text(revision, limits.max_field_len):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)

    raw_bindings = document.get("bindings")
    if not isinstance(raw_bindings, list):
        raise SourceUnavailable(REASON_INVALID_DOCUMENT)
    if len(raw_bindings) > limits.max_bindings:
        raise SourceUnavailable(REASON_TOO_MANY_BINDINGS)

    bindings = [_parse_binding(raw, limits) for raw in raw_bindings]

    seen: set[str] = set()
    for binding in bindings:
        if binding.key_id in seen:
            # 两把同 id 的 key 意味着「这把 key 归谁」没有唯一答案。挑一个 = 猜授权边界。
            raise SourceUnavailable(REASON_DUPLICATE_KEY_ID)
        seen.add(binding.key_id)

    return LoadedKeyDirectory(
        revision=revision.strip(),
        directory=KeyDirectory(bindings),
        key_count=len(bindings),
        revoked_count=sum(1 for binding in bindings if binding.revoked),
    )


def load_key_directory(
    path: str | Path,
    *,
    root: str | Path,
    limits: DirectoryLimits = DirectoryLimits(),
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> LoadedKeyDirectory:
    """从 ``root`` 之内的一份文档加载受信目录（有界读取 + 严格解析）。"""
    text = read_bounded_text(
        path, root=root, max_bytes=limits.max_bytes, fs_probe=fs_probe
    )
    return parse_key_directory_document(text, limits=limits)


@dataclass(frozen=True)
class CorpusLimits:
    """Gateway 语料的滥用上限。

    ``max_bytes`` 与 :class:`backend.atlas.ingest.IngestLimits` 的默认值**同值**，
    这不是巧合：若两者不同，BFF 拿到的原文会先被 ingest 以另一个上限拒掉，而
    「两处的默认值不一样」这件事没有任何人会发现。
    """

    max_bytes: int = 32 * 1024 * 1024


@dataclass(frozen=True)
class CorpusRead:
    """一次语料读取的**信封** —— 它如实区分「读不出来」与「读到了空」。

    ``available=False`` 时 ``text`` 恒为空串、``bytes_read`` 恒为 0：这个组合的
    意思是「本部署没有可用的语料」，而 ``available=True, text=""`` 的意思是
    「语料可用，但这份导出是空的」。两者在数据上无法区分，结论却相反，所以不能
    塌成同一个 ``str``。
    """

    available: bool
    reason: str | None
    text: str
    bytes_read: int


class FileCorpusReader:
    """把一个**装配方选定**的语料文件读成 ``gateway_text`` 需要的形状。

    它替换掉的是 ``dev/atlas_calls.py`` 里那条 ``FIXTURE_PATH.read_text()``：
    那一条把整个文件读进内存之后才由 ``ingest`` 判上限，而这里的尺寸门在 ``stat``
    上、先于 ``open``。两处的**语义**因此不同，不只是效率不同。

    本类不缓存、不轮询、不开 socket。每次 ``read`` 重新 stat + 读 —— 一个会在请求
    之间变化的语料文件，其新鲜度由调用方决定，不由这里悄悄记住。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        root: str | Path,
        limits: CorpusLimits = CorpusLimits(),
        fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
    ) -> None:
        self._path = path
        self._root = root
        self._limits = limits
        self._fs_probe = fs_probe

    @property
    def path(self) -> str | Path:
        return self._path

    def read(self, identity: object | None = None) -> CorpusRead:
        """读一次语料。

        ``identity`` 被**显式丢弃**：语料文件由装配方选定，不由身份选。这一点写成
        代码事实而不是靠「没写那行」—— 一旦它参与选择，一个请求参数就能改变被读的
        文件，而这正是本 seam 存在的意义所要挡住的。
        """
        del identity
        try:
            text = read_bounded_text(
                self._path,
                root=self._root,
                max_bytes=self._limits.max_bytes,
                fs_probe=self._fs_probe,
            )
        except SourceUnavailable as exc:
            return CorpusRead(available=False, reason=exc.reason, text="", bytes_read=0)
        return CorpusRead(
            available=True,
            reason=None,
            text=text,
            bytes_read=len(text.encode("utf-8")),
        )

    def __call__(self, identity: object | None = None) -> str:
        """喂给 BFF ``AtlasStack.gateway_text`` 的那条 seam。

        不可用时**抛错**，不返回空串：一条静默的空串会让「这台机器还没接上语料」
        渲染成「你最近没有调用」。装配方应当先做一次 :meth:`read` 探活，只在
        ``available`` 时把本对象接上去（见 :func:`build_atlas_source`）。
        """
        read = self.read(identity)
        if not read.available:
            raise SourceUnavailable(read.reason)
        return read.text


@dataclass(frozen=True)
class AtlasSource:
    """装配方注入 BFF ``AtlasStack`` 的那三样东西，外加一个可用性判决。

    **刻意不 import ``backend.bff``。** 依赖方向是 BFF 依赖 Atlas：BFF Lane 拥有
    ``backend/bff/atlas_adapter.py``，它把这里的字段填进自己的 ``AtlasStack``。
    反过来 import 会让「谁拥有哪一层」只剩一句注释。

    ``available=False`` 时 ``directory`` 与 ``gateway_text`` 一并是 ``None`` ——
    **不给半套东西**。只把目录置空、却把语料接上去，会让每一行都因为「key 不在
    受信目录」而归不了属，最终渲染出一份看起来正常的空账本。
    """

    available: bool
    reason: str | None
    directory: KeyDirectory | None
    gateway_text: Callable[[object], str] | None
    provenance: Provenance
    gateway_attached: bool
    directory_revision: str | None


def build_atlas_source(
    *,
    directory_path: str | Path,
    directory_root: str | Path,
    corpus_path: str | Path,
    corpus_root: str | Path,
    provenance: Provenance,
    gateway_attached: bool = False,
    directory_limits: DirectoryLimits = DirectoryLimits(),
    corpus_limits: CorpusLimits = CorpusLimits(),
    fs_probe: Callable[[Path], FileFacts] = default_fs_probe,
) -> AtlasSource:
    """把「一份受信文档 + 一个语料路径」装配成可注入 BFF 的来源。

    **目录先于语料判定。** 两条来源都坏时报目录那一条：归属依据比内容更根本，
    而先报「语料读不到」会把排查引向错误的方向。

    ``gateway_attached`` 只由装配方显式声明，工厂**不做任何推断** —— 尤其不因为
    「文件读得到」就把它翻成真。读到一个文件，不等于那文件来自生产 Gateway。
    """
    if not isinstance(provenance, Provenance):
        raise InvalidRequest("provenance must be a Provenance")

    try:
        loaded = load_key_directory(
            directory_path,
            root=directory_root,
            limits=directory_limits,
            fs_probe=fs_probe,
        )
    except SourceUnavailable as exc:
        return AtlasSource(
            available=False,
            reason=exc.reason,
            directory=None,
            gateway_text=None,
            provenance=provenance,
            gateway_attached=gateway_attached,
            directory_revision=None,
        )

    reader = FileCorpusReader(
        corpus_path, root=corpus_root, limits=corpus_limits, fs_probe=fs_probe
    )
    probe = reader.read()
    if not probe.available:
        return AtlasSource(
            available=False,
            reason=probe.reason,
            directory=None,
            gateway_text=None,
            provenance=provenance,
            gateway_attached=gateway_attached,
            directory_revision=None,
        )

    return AtlasSource(
        available=True,
        reason=None,
        directory=loaded.directory,
        gateway_text=reader,
        provenance=provenance,
        gateway_attached=gateway_attached,
        directory_revision=loaded.revision,
    )


__all__ = [
    "KEY_DIRECTORY_SCHEMA_VERSION",
    "REASON_DUPLICATE_KEY_ID",
    "REASON_FILE_MISSING",
    "REASON_INVALID_DOCUMENT",
    "REASON_NOT_A_REGULAR_FILE",
    "REASON_NOT_UTF8",
    "REASON_OUTSIDE_ROOT",
    "REASON_SCHEMA_MISMATCH",
    "REASON_TOO_LARGE",
    "REASON_TOO_MANY_BINDINGS",
    "REASON_UNREADABLE",
    "AtlasSource",
    "CorpusLimits",
    "CorpusRead",
    "DirectoryLimits",
    "FileCorpusReader",
    "FileFacts",
    "LoadedKeyDirectory",
    "default_fs_probe",
    "load_key_directory",
    "parse_key_directory_document",
    "read_bounded_text",
]
