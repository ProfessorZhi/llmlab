"""每用户「默认 Key」的创建、轮换与本机落盘（规格 §4.3 / D5 / §6.2）。

这个模块**只**做三件事，且刻意都不碰身份解析：

1. ``ensure_default_key``：没有 label 为 ``default`` 的**有效** Key 就建一把
   （无模型限制、默认限速、无过期），有就复用；
2. ``rotate_default_key``：吊销旧的 default，建一把新的；
3. ``write_key_file`` / ``default_key_path``：把明文写到
   ``~/.config/llmlab/default_key``（目录 0700、文件 0600）。

## 与 Gateway 的对接面

``service`` 就是 ``backend.gateway.service.GatewayService`` 那一层，**主体由调用方
预先解析好并挂在 service 上**（或显式作为 ``principal=`` 传进来）。本模块不读会话、
不读 cookie、不接受请求体里的自报身份。用到的既有方法/字段（与 §4.3 一一对应）：

* ``service.principal`` —— 已解析的 ``bff.identity.Principal`` 对象；
* ``service.list_keys(principal) -> {"keys": [...]}`` —— 每行含 ``key_id``、
  ``label``、``state``（``active`` / ``revoked`` / ``expired``）、``created_at``；
* ``service.create_key(principal, {"label": "default"}) -> {"key": {..., "key": 明文}}``
  —— 不传 models / rpm / concurrency / ttl_days，即走 Gateway 的默认值
  （无模型限制、``DEFAULT_RPM``、``DEFAULT_CONCURRENCY``、无过期）；
* ``service.revoke_key(principal, key_id)`` —— 吊销。

## 为什么并发逻辑放在这里

``GatewayStore`` 的 ``keys`` 表**没有** ``(principal, label)`` 唯一约束，``create_key``
也不是「先查后插」的单个事务（每次调用各自连接、自动提交）。因此做不到在数据库层
让两个进程只插一把。这里的做法是**先创建、再收敛**：创建后重新列一次，若出现多把
有效的 default，按 ``(created_at, key_id)`` 这个**所有进程都算得出同一个答案**的顺序
保留最早的一把，吊销其余（包括自己刚建的那把）。这样至少有一个进程拿到的明文对应
最终活着的那把；其余进程返回 ``plaintext=None``，不会拿到一把随即被吊销的凭据。

明文只在**这一次创建并胜出**时出现在返回值里；日志与异常消息从不包含明文。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: §4.3：默认 Key 的 label 固定为 ``default``。
DEFAULT_LABEL = "default"

_CONFIG_DIR = ".config"
_APP_DIR = "llmlab"
_KEY_FILE = "default_key"

#: rotate 在极端并发下（自己的新 Key 被另一个 rotate 吊销）最多重来几次。
#: 设上限是为了让「一直抢不到」变成一条明确的错误，而不是无限循环。
_ROTATE_ATTEMPTS = 5


class DefaultKeyError(RuntimeError):
    """默认 Key 无法安全收敛。消息只含 key id / 原因，绝不含明文。"""


class KeyFileExists(FileExistsError):
    """目标文件已存在且内容不同：调用方决定是轮换、备份还是放弃。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        # 只报路径：明文既不该进异常，也不该进日志。
        super().__init__(f"refusing to overwrite the existing key file at {self.path}")


@dataclass(frozen=True, repr=False)
class DefaultKey:
    """一次 ensure / rotate 的结果。

    ``plaintext`` 只在「本调用创建了最终胜出的那把自己」时非空；其余情况为
    ``None``，调用方应复用已有 Key 或从 §6.2 的文件里读。``repr`` 刻意隐藏明文，
    避免 ``print(result)`` / 异常链把凭据带进日志。
    """

    plaintext: str | None
    key_id: str
    created: bool

    def __repr__(self) -> str:
        shown = "None" if self.plaintext is None else "<redacted>"
        return f"DefaultKey(key_id={self.key_id!r}, created={self.created!r}, plaintext={shown})"


def default_key_path(home: Path) -> Path:
    """§6.2：``~/.config/llmlab/default_key``。"""
    return Path(home) / _CONFIG_DIR / _APP_DIR / _KEY_FILE


def ensure_default_key(service: Any, *, principal: Any | None = None) -> DefaultKey:
    """确保当前主体有一把有效的 default Key；需要时创建并**只这一次**返回明文。

    ``principal`` 缺省时取 ``service.principal``（调用方已经解析、绑定好的主体）。
    """
    who = _principal_of(service, principal)

    existing = _active_defaults(service, who)
    if existing:
        winner, extras = _pick_winner(existing)
        _revoke(service, who, extras)
        return DefaultKey(plaintext=None, key_id=winner["key_id"], created=False)

    plaintext, key_id = _created_key(service.create_key(who, {"label": DEFAULT_LABEL}))

    # 创建后再查一次：这一查就是并发自愈。收敛顺序与进程无关，所以两个进程即使
    # 同时各插一把，也会一致地留下同一把、吊销另一把；不会互相吊销到一把不剩。
    live = _active_defaults(service, who)
    if not live:
        # 极端情况下（例如 Key 立即过期）列表里看不到自己刚建的那把：它确实是
        # 本调用创建的，按契约把明文交出去，而不是把它当成“别人的 Key”藏起来。
        return DefaultKey(plaintext=plaintext, key_id=key_id, created=True)

    winner, extras = _pick_winner(live)
    _revoke(service, who, extras)
    if winner["key_id"] == key_id:
        return DefaultKey(plaintext=plaintext, key_id=key_id, created=True)
    # 本调用建的那把在收敛中输了（明文只属于它，已经随吊销作废）：返回胜出者，
    # 但绝不把已作废的明文交出去。
    return DefaultKey(plaintext=None, key_id=winner["key_id"], created=False)


def rotate_default_key(service: Any, *, principal: Any | None = None) -> DefaultKey:
    """吊销旧的 default 并创建新的，返回新明文。

    并发 rotate 的处理：每次「吊销全部 → 创建 → 复查」。若自己的新 Key 在复查时
    已被另一个 rotate 吊销，就重来一次（有上限）。这样成功返回的明文一定对应
    复查时还活着的那把。
    """
    who = _principal_of(service, principal)
    last_reason = "raced"

    for _ in range(_ROTATE_ATTEMPTS):
        _revoke(service, who, _active_defaults(service, who))
        plaintext, key_id = _created_key(service.create_key(who, {"label": DEFAULT_LABEL}))

        live = _active_defaults(service, who)
        if not any(key["key_id"] == key_id for key in live):
            last_reason = f"the new key {key_id} was revoked by a concurrent rotation"
            continue
        _revoke(service, who, [key for key in live if key["key_id"] != key_id])
        return DefaultKey(plaintext=plaintext, key_id=key_id, created=True)

    raise DefaultKeyError(f"could not rotate the default key safely: {last_reason}")


def write_key_file(path: Path, plaintext: str) -> None:
    """把明文写到 ``path``：目录 0700、文件 0600，且**绝不静默覆盖**。

    已存在且内容相同 ⇒ 幂等成功（顺手把权限收紧到 0600）；
    已存在且内容不同（或是个软链）⇒ 抛 :class:`KeyFileExists`，由调用方决定下一步。

    实现上先写同目录临时文件，再用 ``os.link`` **原子占位**：目标已存在时 link 会
    失败而不会像裸 ``os.replace`` 那样静默覆盖掉竞态窗口里刚出现的文件。文件系统
    不支持硬链接时才退回「先检查再 replace」。
    """
    path = Path(path)
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext must be a non-empty string")

    directory = path.parent
    directory_existed = directory.exists()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 只对平台自己的目录收紧权限：新建的（mkdir 已给 0700）或名为 llmlab 的那个。
    # 不无条件 chmod 任意父目录 —— 那样如果调用方传了 /tmp/default_key，
    # 就会把整个 /tmp 改成 0700。
    if not directory_existed or directory.name == _APP_DIR:
        os.chmod(directory, 0o700)

    payload = plaintext.encode("utf-8")
    if path.exists() or path.is_symlink():
        _reuse_or_raise(path, payload)
        return

    fd, tmp_name = tempfile.mkstemp(prefix=".default-key-", dir=str(directory))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            # 竞态：检查之后、发布之前另一个进程写好了文件。仍然不覆盖。
            _reuse_or_raise(path, payload)
        except OSError:
            # 少数文件系统不支持硬链接；此时退回到存在性检查 + replace。
            if path.exists() or path.is_symlink():
                _reuse_or_raise(path, payload)
            os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


# --- 内部工具 ---------------------------------------------------------------


def _principal_of(service: Any, principal: Any | None) -> Any:
    """取调用方提供的主体。**不做任何身份解析**，只决定「用谁的名字」。"""
    who = principal
    if who is None:
        who = getattr(service, "principal", None)
        if callable(who):
            who = who()
    if who is None or not getattr(who, "principal", None):
        raise ValueError(
            "a principal is required: pass principal=... or bind service.principal "
            "(this module never derives identity itself)"
        )
    return who


def _active_defaults(service: Any, principal: Any) -> list[dict]:
    """当前主体的有效 default Key（``state == "active"``）。

    ``list_keys`` 会连已吊销 / 已过期的 Key 一起返回，这里只留活着的；否则每次
    ensure 都会把历史上轮换掉的 default 当成「已存在」。
    """
    listing = service.list_keys(principal)
    keys = listing.get("keys") if isinstance(listing, dict) else None
    if not isinstance(keys, list):
        raise DefaultKeyError("the gateway service returned an unexpected key list")
    return [
        key
        for key in keys
        if isinstance(key, dict)
        and key.get("label") == DEFAULT_LABEL
        and key.get("state") == "active"
    ]


def _pick_winner(keys: list[dict]) -> tuple[dict, list[dict]]:
    """按 ``(created_at, key_id)`` 选唯一的胜出者。

    这个顺序只依赖 Key 自身的数据，不依赖本进程的时序，所以并发的每个进程都会
    得到同一个答案 —— 这是「自愈」不会变成「互杀」的前提。
    """
    ordered = sorted(keys, key=lambda key: (str(key.get("created_at") or ""), str(key.get("key_id"))))
    return ordered[0], ordered[1:]


def _revoke(service: Any, principal: Any, keys: list[dict]) -> None:
    for key in keys:
        service.revoke_key(principal, key["key_id"])


def _created_key(result: Any) -> tuple[str, str]:
    """从 ``GatewayService.create_key`` 的返回里取 (明文, key_id)。"""
    record = result.get("key") if isinstance(result, dict) else None
    plaintext = record.get("key") if isinstance(record, dict) else None
    key_id = record.get("key_id") if isinstance(record, dict) else None
    if not isinstance(plaintext, str) or not plaintext:
        raise DefaultKeyError("the gateway service did not return the new key material")
    if not isinstance(key_id, str) or not key_id:
        raise DefaultKeyError("the gateway service did not return the new key id")
    return plaintext, key_id


def _reuse_or_raise(path: Path, payload: bytes) -> None:
    """已存在时的唯一出口：内容相同就复用，否则抛错且一个字节都不改动。"""
    if path.is_symlink():
        # 软链可能把写入引到别处；宁可报冲突也不顺着链走。
        raise KeyFileExists(path)
    try:
        current = path.read_bytes()
    except OSError:
        # 读不了就无法证明内容相同 —— 按冲突处理，避免覆盖。
        raise KeyFileExists(path) from None
    if current != payload:
        raise KeyFileExists(path)
    os.chmod(path, 0o600)
