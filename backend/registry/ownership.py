"""「这个引擎是谁的」：运维策略（谁能被登记）与所有权证明（谁证明得了）。

本模块把两件相关的事放在一起，因为它们的答案互为补充：

1. **运维策略**（:class:`OperatorPolicy`）—— 一份由 root/运维拥有的配置文件，
   逐条承认「这台机器上确实存在这个服务」。既有 Strata 在 ``127.0.0.1:8080``，
   8080 又是保留端口；唯一的合法通路就是运维显式写下一条 entry。
2. **所有权证明**（:class:`OwnershipProver`）—— 一个引擎指向某个回环端口，
   「它是不是登记者的进程」这个问题的答案。

两件都不能靠自报。**没有证明就是没有证明**：默认状态是
``ownership_unverified``，它不是一个警告，而是一条会让共享/调用被拒的判定
（由 ``RegistryConfig.ownership_policy`` 决定是否强制，见 ``registry.py``）。

``uid`` / socket 主人的**自动**证明：从 BLOCKED 到一条**不需要 root** 的路。

上一轮把它判成 BLOCKED，前提是「要知道回环端口属于哪个 uid，得读
``/proc/net/tcp`` 拿 inode，再扫 ``/proc/<pid>/fd`` 反查进程」—— 后一步需要
root 与遍历**其他用户**的进程表。那个前提**错了一半**：``/proc/net/tcp`` 与
``/proc/net/tcp6`` 本身是全员可读的，而且**每一行已经带着 socket 属主的 uid**
（``uid`` 列，内核在建 socket 时记下的有效 uid），连同本地 ``地址:端口`` 与
状态（``0A`` = LISTEN）。不需要 inode 反查，不需要碰任何人的 ``/proc/<pid>``。

于是有了 :class:`ProcNetOwnershipProver`：

* 期望 uid 来自**受信目录**给出的登记者 Linux 用户名，经 ``pwd.getpwnam``
  解析 —— 从不读请求里自报的 uid；
* 在 LISTEN 表里找绑定在该端口、且**能服务这个回环目标**的 socket
  （精确回环地址、IPv4-mapped 等价写法、或通配 ``0.0.0.0`` / ``::``，通配会被
  记进 detail）；
* **恰好**一个 uid 且等于期望 uid 才是 ``uid_verified``；否则给出结构化原因
  （``not_listening`` / ``owner_mismatch:<uid>`` / ``ambiguous`` /
  ``unknown_user`` …），状态保持 ``ownership_unverified``。

诚实的边界，照实写：

* 这条路**只**由单元测试（注入的 ``/proc/net/tcp(6)`` fixture 表）与一条
  「本测试进程自己开一个回环监听、再从真实 ``/proc/net/tcp`` 认出它」的冒烟测试
  验证过；**真实主机上的验证仍待做**（尚未在 Dell 上对真实引擎、跨 Linux 用户跑过）。
* ``/proc/net/*`` 是**按网络命名空间**的：Registry 必须与引擎处在同一个
  netns，否则读到的是另一张表（结论会是 ``not_listening``，fail-closed，不会误放行）。
* 它证明的是「监听这个端口的 socket 属于谁」，不是「这个进程是哪个二进制」。
  ``uid`` 列是 socket 创建者的有效 uid —— 对「这个引擎是谁的」这个问题，这正是答案。
* 它是**可选**的：``RegistryConfig.ownership_prover_kind`` 缺省仍是 ``none``，
  即 fail-closed 的 :class:`NoOwnershipProver`；只有显式选 ``proc_net`` 才启用。

运维承认的既有服务仍走 :class:`OperatorPolicy`，且优先于任何 prover。
"""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, NamedTuple, Protocol

from .errors import InvalidRequest, StorageError
from .urls import NormalizedTarget, OperatorTarget, is_loopback_host

# --- 所有权状态 -------------------------------------------------------------

#: 没有证明。这是**默认**，也是唯一诚实的缺省。
OWNERSHIP_UNVERIFIED = "ownership_unverified"
#: 由 root/运维拥有的策略文件逐条承认（既有服务的通路）。
OWNERSHIP_OPERATOR_VERIFIED = "operator_verified"
#: 由某个 prover 依据本机事实（uid / socket 属主）证明过。
OWNERSHIP_UID_VERIFIED = "uid_verified"
#: 平台账户登记（不限定 Linux 用户时）：本机事实观测到**恰好一个**运行 uid，
#: 且该目标没有被其他主体登记过。证明的是「这个服务由谁在跑、归谁登记」，
#: 不是「登记者就是那个 Linux 用户」——账户与 Linux 用户本来就相互独立。
OWNERSHIP_RUNTIME_OBSERVED = "runtime_observed"

OWNERSHIP_STATES = (
    OWNERSHIP_UNVERIFIED,
    OWNERSHIP_OPERATOR_VERIFIED,
    OWNERSHIP_UID_VERIFIED,
    OWNERSHIP_RUNTIME_OBSERVED,
)

#: 哪些状态算「证明过」。刻意是一个显式集合，而不是 ``!= UNVERIFIED`` 之类的
#: 取反 —— 以后新增状态时，取反会**默认放行**，枚举会默认拒绝。
OWNERSHIP_VERIFIED_STATES = frozenset(
    {OWNERSHIP_OPERATOR_VERIFIED, OWNERSHIP_UID_VERIFIED, OWNERSHIP_RUNTIME_OBSERVED}
)

REASON_NO_OWNERSHIP_SOURCE = "no_ownership_source_in_this_deployment"
REASON_OPERATOR_POLICY = "operator_policy_entry"
REASON_TEST_DOUBLE = "static_test_double"

#: ``/proc/net/tcp(6)`` 那条路的 ``method``（**怎么**证明的），与失败原因分开。
METHOD_PROC_NET = "proc_net_listener_uid"

#: :class:`ProcNetOwnershipProver` 的失败原因（``OwnershipProof.reason``）。
#: 刻意是**封闭**的 token 集合（``owner_mismatch`` 后面跟观测到的 uid）：前端与审计
#: 按 token 分支，而不是按一段会被改写的人话。
REASON_NOT_LISTENING = "not_listening"
REASON_OWNER_MISMATCH = "owner_mismatch"
REASON_AMBIGUOUS = "ambiguous"
REASON_UNKNOWN_USER = "unknown_user"
REASON_TARGET_NOT_LOOPBACK = "target_not_loopback"
REASON_PROC_NET_UNREADABLE = "proc_net_unreadable"
REASON_PROC_NET_UNPARSEABLE = "proc_net_unparseable"
#: 目标已被另一个主体登记。由 Registry 判（它知道谁登记过什么），不是 prover。
REASON_TARGET_TAKEN = "target_registered_by_another_account"

#: 可选的 prover 种类（``RegistryConfig.ownership_prover_kind``）。
OWNERSHIP_PROVER_NONE = "none"
OWNERSHIP_PROVER_PROC_NET = "proc_net"
OWNERSHIP_PROVER_KINDS = (OWNERSHIP_PROVER_NONE, OWNERSHIP_PROVER_PROC_NET)

#: 所有权强制策略。
#:
#: * ``advisory`` —— **记录**所有权状态，但不阻断共享/调用。这是兼容缺省：
#:   既有的 ``backend/bff`` 用默认配置构造服务，而本模块不允许修改它，
#:   所以缺省必须保持历史行为，否则会静默改掉一个不属于本模块的对外路径。
#: * ``enforced`` —— 未证明即拒绝共享与调用。**生产应当用这一档**
#:   （见 ``RegistryConfig.production_posture()``）。
#:
#: 这条缺省的取舍被显式记进 ``docs/registry-cli-contract.md``：一个默认关闭的
#: 安全开关必须写在明处，而不是藏在默认值里。
OWNERSHIP_POLICY_ADVISORY = "advisory"
OWNERSHIP_POLICY_ENFORCED = "enforced"
OWNERSHIP_POLICIES = (OWNERSHIP_POLICY_ADVISORY, OWNERSHIP_POLICY_ENFORCED)


@dataclass(frozen=True)
class OwnershipProof:
    """一次所有权判定的结果。``state`` 之外的东西都是佐证，不是授权依据。

    ``reason`` 是**机器可读**的失败原因（``not_listening`` /
    ``owner_mismatch:<uid>`` / ``ambiguous`` / ``unknown_user`` …）；证明成功时为
    ``None``。``method`` 回答「用什么方法判的」，两者不混用。
    """

    state: str
    method: str
    uid: int | None = None
    detail: str | None = None
    source: str | None = None
    reason: str | None = None
    #: ``uid`` 在本机对应的用户名（只读 ``pwd`` 反查，查不到为 ``None``）。仅供展示。
    linux_user: str | None = None

    def __post_init__(self) -> None:
        if self.state not in OWNERSHIP_STATES:
            raise InvalidRequest(f"unknown ownership state: {self.state!r}")
        if self.uid is not None and (
            not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0
        ):
            raise InvalidRequest("ownership proof uid must be a non-negative integer")

    @property
    def verified(self) -> bool:
        return self.state in OWNERSHIP_VERIFIED_STATES

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "method": self.method,
            "verified": self.verified,
            "uid": self.uid,
            "detail": self.detail,
            "source": self.source,
            "reason": self.reason,
            "linux_user": self.linux_user,
        }


def unverified_proof(
    reason: str = REASON_NO_OWNERSHIP_SOURCE, *, detail: str | None = None
) -> OwnershipProof:
    return OwnershipProof(
        state=OWNERSHIP_UNVERIFIED, method=reason, detail=detail, source=None, reason=reason
    )


class OwnershipProver(Protocol):
    """把一个回环目标映射到「它的进程属于谁」。

    实现**必须**只读本机事实，且必须能证明失败 —— 拿不准就返回
    ``ownership_unverified``，不要猜。

    ``owner_linux_user`` 是登记者的 Linux 用户名，**只**来自服务端受信目录
    （``RequesterContext.linux_user`` / ``PrincipalDirectory``），从不来自请求体。
    不需要它的实现（缺省、测试替身）可以忽略它。
    """

    name: str

    def prove(  # pragma: no cover - protocol
        self, target: NormalizedTarget, *, owner_linux_user: str | None = None
    ) -> OwnershipProof:
        ...


class NoOwnershipProver:
    """默认实现：**永远**说不出来。fail-closed 的那一半。

    它存在的意义不是「还没做」，而是「本部署确实没有这条来源」——
    与 ``health.SOURCE_NONE`` 同一个纪律：没有来源就是 UNKNOWN，不是「大概没问题」。
    """

    name = "none"

    def prove(
        self, target: NormalizedTarget, *, owner_linux_user: str | None = None
    ) -> OwnershipProof:
        del owner_linux_user
        if not isinstance(target, NormalizedTarget):
            raise InvalidRequest("target must be a NormalizedTarget")
        return unverified_proof(REASON_NO_OWNERSHIP_SOURCE)

    def observe(self, target: NormalizedTarget) -> OwnershipProof:
        return self.prove(target)


class StaticOwnershipProver:
    """**测试替身**：按 (host, port) 预置证明。

    与 ``health.StaticProber`` 同等地位：唯一被允许的另一种确定性来源，
    显式标注为替身，且**只**用于离线测试与合成开发。
    """

    name = "static_test_double"

    def __init__(self, verdicts: dict[tuple[str, int], int] | None = None) -> None:
        self._verdicts = {key: int(value) for key, value in (verdicts or {}).items()}

    def set(self, host: str, port: int, uid: int) -> None:
        self._verdicts[(host, int(port))] = int(uid)

    def prove(
        self, target: NormalizedTarget, *, owner_linux_user: str | None = None
    ) -> OwnershipProof:
        del owner_linux_user
        uid = self._verdicts.get((target.host, target.port))
        if uid is None:
            return unverified_proof(REASON_TEST_DOUBLE, detail="no static verdict for this target")
        return OwnershipProof(
            state=OWNERSHIP_UID_VERIFIED,
            method=REASON_TEST_DOUBLE,
            uid=uid,
            detail="injected fixture verdict",
            source=self.name,
        )

    def observe(self, target: NormalizedTarget) -> OwnershipProof:
        uid = self._verdicts.get((target.host, target.port))
        if uid is None:
            return unverified_proof(REASON_TEST_DOUBLE, detail="no static verdict for this target")
        return OwnershipProof(
            state=OWNERSHIP_RUNTIME_OBSERVED,
            method=REASON_TEST_DOUBLE,
            uid=uid,
            detail="injected fixture verdict",
            source=self.name,
        )


# --- /proc/net/tcp(6)：全员可读的 LISTEN 表 ---------------------------------

PROC_NET_TCP = "/proc/net/tcp"
PROC_NET_TCP6 = "/proc/net/tcp6"

#: ``st`` 列里 LISTEN 的取值（``include/net/tcp_states.h``：``TCP_LISTEN = 10``）。
TCP_STATE_LISTEN = "0A"

#: 一行里至少要有到 ``inode`` 为止的这些列。
_PROC_NET_MIN_COLUMNS = 10


class ProcNetUnavailable(Exception):
    """LISTEN 表读不到。与「读到了但没有人监听」是两件事。"""


class ProcNetUnparseable(Exception):
    """LISTEN 表读到了，但有一行看不懂。

    看不懂的行**不**被跳过：被跳过的那一行恰好可能是另一个 uid 的监听者，
    跳过它就会把 ``ambiguous`` 静默变成 ``uid_verified``。
    """


class ListeningSocket(NamedTuple):
    """一条 LISTEN 行：``(ip, port, uid, inode)``。``ip`` 是规范化后的字面量。"""

    ip: str
    port: int
    uid: int
    inode: int


def _decode_word(word: str) -> bytes:
    """一个 32 位字（8 个 hex）-> 网络序字节。内核按**主机字节序**打印它。"""
    raw = bytes.fromhex(word)
    return raw[::-1] if sys.byteorder == "little" else raw


def decode_proc_net_address(field: str) -> tuple[str, int]:
    """``0100007F:4659`` -> ``("127.0.0.1", 18009)``；IPv6 为 32 个 hex。

    IPv4 是一个 32 位字；IPv6 是四个 32 位字，**每个字各自**按主机字节序打印
    （不是整个 128 位倒序）。端口是普通的大端 hex。
    """
    if not isinstance(field, str) or field.count(":") != 1:
        raise ValueError(f"malformed address field: {field!r}")
    address_hex, port_hex = field.split(":")
    if len(port_hex) != 4:
        raise ValueError(f"malformed port: {port_hex!r}")
    port = int(port_hex, 16)
    if len(address_hex) == 8:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.IPv4Address(
            _decode_word(address_hex)
        )
    elif len(address_hex) == 32:
        ip = ipaddress.IPv6Address(
            b"".join(_decode_word(address_hex[i : i + 8]) for i in range(0, 32, 8))
        )
    else:
        raise ValueError(f"malformed address: {address_hex!r}")
    return str(ip), port


def parse_proc_net_tcp(text: str) -> list[ListeningSocket]:
    """解析一份 ``/proc/net/tcp`` 或 ``/proc/net/tcp6`` 的文本，只返回 LISTEN 行。

    首行表头（以 ``sl`` 开头）与空行被忽略；其余任何一行看不懂都抛
    :class:`ProcNetUnparseable`（理由见该类）。
    """
    if not isinstance(text, str):
        raise ProcNetUnparseable("proc net table must be text")
    sockets: list[ListeningSocket] = []
    for number, line in enumerate(text.splitlines(), start=1):
        columns = line.split()
        if not columns or columns[0] == "sl":
            continue
        try:
            if len(columns) < _PROC_NET_MIN_COLUMNS or not columns[0].endswith(":"):
                raise ValueError("too few columns")
            state = columns[3].upper()
            if len(state) != 2:
                raise ValueError(f"malformed state {columns[3]!r}")
            int(state, 16)
            ip, port = decode_proc_net_address(columns[1])
            if not columns[7].isdigit() or not columns[9].isdigit():
                raise ValueError("uid/inode must be non-negative decimals")
            uid = int(columns[7], 10)
            inode = int(columns[9], 10)
        except ValueError as exc:
            raise ProcNetUnparseable(f"line {number}: {exc}") from None
        if state != TCP_STATE_LISTEN:
            continue
        sockets.append(ListeningSocket(ip=ip, port=port, uid=uid, inode=inode))
    return sockets


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="ascii") as handle:
        return handle.read()


class ProcNetListenerTable:
    """本机（本 netns）的 TCP LISTEN 表，读自 ``/proc/net/tcp`` + ``/proc/net/tcp6``。

    ``reader`` 可注入（测试传 fixture 文本）。某一张表 ``FileNotFoundError``
    视为「这个协议族没开」（例如内核关了 IPv6）；**两张都**读不到，或出现其它
    ``OSError``，抛 :class:`ProcNetUnavailable` —— 读不到不是「没人监听」。
    """

    def __init__(
        self,
        reader: Callable[[str], str] | None = None,
        *,
        paths: tuple[str, ...] = (PROC_NET_TCP, PROC_NET_TCP6),
    ) -> None:
        self._reader = reader if reader is not None else _read_text_file
        self._paths = tuple(paths)
        if not self._paths:
            raise InvalidRequest("ProcNetListenerTable needs at least one path")

    @classmethod
    def from_texts(
        cls, *, tcp: str | None = None, tcp6: str | None = None
    ) -> ProcNetListenerTable:
        """**测试/离线**用：直接给两张表的文本；``None`` 表示该文件不存在。"""
        texts = {PROC_NET_TCP: tcp, PROC_NET_TCP6: tcp6}

        def reader(path: str) -> str:
            found = texts.get(path)
            if found is None:
                raise FileNotFoundError(path)
            return found

        return cls(reader)

    @property
    def source(self) -> str:
        return ",".join(self._paths)

    def listeners(self) -> list[ListeningSocket]:
        sockets: list[ListeningSocket] = []
        read_any = False
        for path in self._paths:
            try:
                text = self._reader(path)
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError) as exc:
                reason = getattr(exc, "strerror", None) or type(exc).__name__
                raise ProcNetUnavailable(f"cannot read {path}: {reason}") from None
            read_any = True
            sockets.extend(parse_proc_net_tcp(text))
        if not read_any:
            raise ProcNetUnavailable(f"none of {list(self._paths)} could be read")
        return sockets


def _normalized_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """把 IPv4-mapped IPv6（``::ffff:127.0.0.1``）折回 IPv4，其余原样。"""
    ip = ipaddress.ip_address(raw)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def listeners_serving(
    host: str, port: int, sockets: Iterable[ListeningSocket]
) -> list[tuple[ListeningSocket, bool]]:
    """哪些 LISTEN socket **可能**接下发往 ``host:port`` 的连接。

    返回 ``[(socket, wildcard)]``。判定是保守的 —— 宁可多算也不漏算：
    多算只会让结论变成 ``ambiguous``，漏算可能让它变成错误的 ``uid_verified``。

    * 地址与目标相同（含 ``::ffff:127.0.0.1`` 这类 mapped 写法）-> 精确命中；
    * 同族通配（``0.0.0.0`` 之于 IPv4、``::`` 之于 IPv6）-> 通配命中；
    * ``::`` 之于 IPv4 目标 -> 通配命中（双栈 socket；``IPV6_V6ONLY`` 从这张表
      看不出来，所以按「可能接得到」算）。

    绑在**别的**地址上的 socket（例如 ``::1`` 之于 ``127.0.0.1``、或某个网卡地址）
    接不到这个目标的连接，不算。
    """
    target = _normalized_ip(host)
    matched: list[tuple[ListeningSocket, bool]] = []
    for item in sockets:
        if item.port != port:
            continue
        bound = _normalized_ip(item.ip)
        if bound == target:
            matched.append((item, False))
        elif bound.is_unspecified and (
            bound.version == target.version or (bound.version == 6 and target.version == 4)
        ):
            matched.append((item, True))
    return matched


def linux_user_for_uid(uid: int) -> str | None:
    """uid -> 用户名，只用于展示；查不到（或非 Linux）返回 ``None``，从不抛错。"""
    try:
        import pwd  # noqa: PLC0415

        return str(pwd.getpwuid(int(uid)).pw_name)
    except (ImportError, KeyError, ValueError, OverflowError, TypeError):
        return None


def resolve_linux_uid(username: str) -> int:
    """缺省的用户名 -> uid 解析：``pwd.getpwnam``。不存在抛 ``KeyError``。"""
    import pwd  # noqa: PLC0415 - 只在 Linux 上真正用到；延迟导入让模块在别处也能被 import

    return int(pwd.getpwnam(username).pw_uid)


def _format_binding(item: ListeningSocket, wildcard: bool) -> str:
    host = f"[{item.ip}]" if ":" in item.ip else item.ip
    suffix = " (wildcard)" if wildcard else ""
    return f"{host}:{item.port} uid={item.uid} inode={item.inode}{suffix}"


class ProcNetOwnershipProver:
    """依据 ``/proc/net/tcp(6)`` 的 ``uid`` 列证明「这个回环端口是登记者的」。

    **不需要 root**：两张表全员可读，且行内已带 socket 属主 uid；不读任何
    ``/proc/<pid>``。期望 uid 只来自受信目录给出的用户名（``pwd`` 解析，可注入）。

    结论（``OwnershipProof.reason``）：

    * ``unknown_user`` —— 没有受信用户名，或该用户名在本机不存在；
    * ``target_not_loopback`` —— 目标不是回环字面量（不该发生，二次确认）；
    * ``proc_net_unreadable`` / ``proc_net_unparseable`` —— 表读不到 / 看不懂；
    * ``not_listening`` —— 没有能服务这个目标的 LISTEN socket；
    * ``ambiguous`` —— 能服务它的 socket 不止一个 uid；
    * ``owner_mismatch:<uid>`` —— 恰好一个 uid，但不是登记者的；
    * 否则 ``uid_verified``（``reason`` 为 ``None``，``uid`` 为期望 uid）。

    验证状态：只由单元测试的 fixture 表验证过；真实主机验证仍待做。
    """

    name = "proc_net_tcp"

    def __init__(
        self,
        *,
        table: ProcNetListenerTable | None = None,
        user_resolver: Callable[[str], int] | None = None,
        uid_namer: Callable[[int], str | None] | None = None,
    ) -> None:
        self._table = table if table is not None else ProcNetListenerTable()
        self._resolve_uid = user_resolver if user_resolver is not None else resolve_linux_uid
        self._name_uid = uid_namer if uid_namer is not None else linux_user_for_uid

    def _fail(self, reason: str, detail: str) -> OwnershipProof:
        return OwnershipProof(
            state=OWNERSHIP_UNVERIFIED,
            method=METHOD_PROC_NET,
            detail=detail,
            source=self._table.source,
            reason=reason,
        )

    def prove(
        self, target: NormalizedTarget, *, owner_linux_user: str | None = None
    ) -> OwnershipProof:
        if not isinstance(target, NormalizedTarget):
            raise InvalidRequest("target must be a NormalizedTarget")
        if not is_loopback_host(target.host):
            return self._fail(
                REASON_TARGET_NOT_LOOPBACK, f"refusing to reason about {target.host!r}"
            )

        if not isinstance(owner_linux_user, str) or not owner_linux_user.strip():
            return self._fail(REASON_UNKNOWN_USER, "no trusted owner linux user for this request")
        try:
            expected_uid = int(self._resolve_uid(owner_linux_user))
        except (KeyError, ValueError, TypeError):
            return self._fail(
                REASON_UNKNOWN_USER,
                f"linux user {owner_linux_user!r} does not exist on this host",
            )

        observed = self._single_listener_uid(target)
        if isinstance(observed, OwnershipProof):
            return observed
        uid, bindings = observed
        if uid != expected_uid:
            return self._fail(
                f"{REASON_OWNER_MISMATCH}:{uid}",
                f"expected uid {expected_uid} ({owner_linux_user}); observed {bindings}",
            )
        return OwnershipProof(
            state=OWNERSHIP_UID_VERIFIED,
            method=METHOD_PROC_NET,
            uid=expected_uid,
            detail=f"{owner_linux_user} owns {bindings}",
            source=self._table.source,
            reason=None,
            linux_user=owner_linux_user,
        )

    def observe(self, target: NormalizedTarget) -> OwnershipProof:
        """不比对用户，只回答「这个回环端口由哪一个 uid 在监听」。

        平台账户（未被限定 Linux 用户）登记时用它：结论是
        ``runtime_observed`` + 观测到的 uid；没人监听、多个 uid、表读不到时与
        :meth:`prove` 给同样的失败原因。
        """
        if not isinstance(target, NormalizedTarget):
            raise InvalidRequest("target must be a NormalizedTarget")
        if not is_loopback_host(target.host):
            return self._fail(
                REASON_TARGET_NOT_LOOPBACK, f"refusing to reason about {target.host!r}"
            )
        observed = self._single_listener_uid(target)
        if isinstance(observed, OwnershipProof):
            return observed
        uid, bindings = observed
        return OwnershipProof(
            state=OWNERSHIP_RUNTIME_OBSERVED,
            method=METHOD_PROC_NET,
            uid=uid,
            detail=f"observed {bindings}",
            source=self._table.source,
            reason=None,
            linux_user=self._name_uid(uid),
        )

    def _single_listener_uid(self, target: NormalizedTarget) -> tuple[int, str] | OwnershipProof:
        """``(uid, bindings)``，或一个说明为什么说不出来的失败证明。"""
        try:
            sockets = self._table.listeners()
        except ProcNetUnavailable as exc:
            return self._fail(REASON_PROC_NET_UNREADABLE, str(exc))
        except ProcNetUnparseable as exc:
            return self._fail(REASON_PROC_NET_UNPARSEABLE, str(exc))

        matched = listeners_serving(target.host, target.port, sockets)
        if not matched:
            return self._fail(
                REASON_NOT_LISTENING,
                f"no LISTEN socket can serve {target.host}:{target.port}",
            )
        bindings = "; ".join(_format_binding(item, wildcard) for item, wildcard in matched)

        uids = sorted({item.uid for item, _wildcard in matched})
        if len(uids) > 1:
            return self._fail(
                REASON_AMBIGUOUS, f"listeners with different uids {uids}: {bindings}"
            )
        return uids[0], bindings


def build_ownership_prover(kind: str) -> object:
    """按配置选 prover。缺省 ``none`` -> fail-closed 的 :class:`NoOwnershipProver`。"""
    if kind == OWNERSHIP_PROVER_NONE:
        return NoOwnershipProver()
    if kind == OWNERSHIP_PROVER_PROC_NET:
        return ProcNetOwnershipProver()
    raise InvalidRequest(f"unknown ownership prover kind: {kind!r}")


# --- 运维策略 ---------------------------------------------------------------

OPERATOR_POLICY_SCHEMA = "registry-operator/1"

#: 策略文件大小上限。一份「承认若干既有服务」的清单不该很大。
MAX_POLICY_BYTES = 64 * 1024


@dataclass(frozen=True)
class OperatorPolicy:
    """一份已加载的运维策略。

    ``provenance`` 是**给审计看的**：文件路径 + 属主 + 权限位。一次「因为运维
    承认所以放行了 8080」的决定，必须能回答「依据是哪份文件、谁写的」。
    """

    targets: tuple[OperatorTarget, ...] = ()
    provenance: str = "empty"
    loaded_from: str | None = None

    def match(self, target: NormalizedTarget) -> OperatorTarget | None:
        for entry in self.targets:
            if entry.matches(
                scheme=target.scheme,
                host=target.host,
                port=target.port,
                path=target.path,
            ):
                return entry
        return None

    @property
    def empty(self) -> bool:
        return not self.targets

    def to_dict(self) -> dict:
        return {
            "schema": OPERATOR_POLICY_SCHEMA,
            "provenance": self.provenance,
            "loaded_from": self.loaded_from,
            "targets": [entry.to_dict() for entry in self.targets],
        }


EMPTY_POLICY = OperatorPolicy()


def default_policy_owners() -> frozenset[int]:
    """策略文件可接受的属主：root，**或**正在运行本服务的那个账号。

    「正在运行本服务的账号」是运维在这里的具体形态：如果服务跑在 ``nobody``
    下，那权限文件就必须属于 root 或 nobody。任何**别的**用户拥有的文件都不算
    运维策略 —— 否则任何一个本机用户都能靠写一个文件给自己开 8080。
    """
    return frozenset({0, os.getuid()})


def load_operator_policy(
    path: str | os.PathLike[str],
    *,
    allowed_owners: Iterable[int] | None = None,
) -> OperatorPolicy:
    """从一份 **root/运维拥有** 的 JSON 文件加载策略。

    拒绝（抛 ``StorageError``）：软链、非普通文件、属主不在允许集合内、
    组/其他人可写、超大、JSON 不合法、schema 不认识、entry 不合法。
    每一条都对应一种真实的失手方式，而不是防御性编程的仪式。
    """
    owners = (
        frozenset(int(owner) for owner in allowed_owners)
        if allowed_owners is not None
        else default_policy_owners()
    )
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise StorageError(f"operator policy not found: {path}") from exc
    except OSError as exc:
        raise StorageError(f"cannot stat operator policy: {exc.strerror}") from exc

    if stat.S_ISLNK(info.st_mode):
        raise StorageError("operator policy must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise StorageError("operator policy must be a regular file")
    if info.st_uid not in owners:
        raise StorageError(
            f"operator policy must be owned by root or by the service account "
            f"(uid in {sorted(owners)}), not uid {info.st_uid}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise StorageError("operator policy must not be group- or world-writable")
    if info.st_size > MAX_POLICY_BYTES:
        raise StorageError(f"operator policy exceeds {MAX_POLICY_BYTES} bytes")

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise StorageError(f"operator policy is not valid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise StorageError(f"cannot read operator policy: {exc.strerror}") from exc

    if not isinstance(raw, dict):
        raise StorageError("operator policy must be a JSON object")
    if raw.get("schema") != OPERATOR_POLICY_SCHEMA:
        raise StorageError(f"unsupported operator policy schema: {raw.get('schema')!r}")

    entries = raw.get("targets", [])
    if not isinstance(entries, list):
        raise StorageError("operator policy 'targets' must be a list")

    parsed: list[OperatorTarget] = []
    provenance = f"{path} (uid={info.st_uid} mode={oct(stat.S_IMODE(info.st_mode))})"
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise StorageError(f"operator policy target #{index} must be an object")
        try:
            host = str(entry.get("host", ""))
            if not is_loopback_host(host):
                raise InvalidRequest(
                    f"operator target host must be a loopback literal: {host!r}"
                )
            parsed.append(
                OperatorTarget(
                    scheme=str(entry.get("scheme", "http")).lower(),
                    host=host,
                    port=int(entry["port"]),
                    path_prefix=str(entry.get("path_prefix", "/")),
                    label=str(entry.get("label", "")),
                    source=provenance,
                )
            )
        except (InvalidRequest, KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"operator policy target #{index} is invalid: {exc}") from exc

    return OperatorPolicy(
        targets=tuple(parsed), provenance=provenance, loaded_from=str(path)
    )


def operator_targets_for_fixture(
    targets: Iterable[OperatorTarget], *, provenance: str = "injected_fixture"
) -> OperatorPolicy:
    """**测试/合成**用：直接用一组已构造的 entry 造一份策略（不读任何文件）。

    标注为 fixture 的 provenance 会进 DTO 与审计，因此它不可能被误当成
    「某份 root 文件里的策略」。
    """
    for entry in targets:
        if not isinstance(entry, OperatorTarget):
            raise InvalidRequest("targets must be OperatorTarget values")
    return OperatorPolicy(targets=tuple(targets), provenance=provenance, loaded_from=None)
