"""同源 BFF 的 ``Host`` 权威校验：这个请求**是不是发给我们的**。

``dev/preview_auth.py`` 的 ``_host_guard`` 是这条规则的第一份实现（Issue #8 修掉的
是「只取最后一个冒号之前」那版：``127.0.0.1:59008.evil.com`` 会被当成
``127.0.0.1`` 服务）。本模块把同一条规则做成**可配置权威集合**的版本，因为 BFF
的部署形态比预览多一种：预览永远只在环回，而 BFF 将来可能在 Caddy 后面用真实
主机名。语义没有放宽——**判定的输入仍然是整个字段值**：

* ``fullmatch``，没有任何「截断到冒号之前」的步骤，也没有 ``$`` 那种会在末尾
  换行前就匹配的写法；
* 端口必须是 1..65535 的 ASCII 十进制数字；写出来的端口要与请求**实际上到达的**
  端口一致（``SERVER_PORT``，由 ``wsgiref`` 从绑定 socket 填，不是客户端说的）；
* ``SERVER_NAME`` 刻意**不**作为回退：``wsgiref`` 用 ``socket.getfqdn()`` 填它，
  在某些主机上就是 ``localhost``——拿它兜底，等于让「没写权威的请求」被当成
  「写了环回权威的请求」。

默认策略是**只有环回**（``trusted=∅``、``allow_loopback=True``）：一个没被
显式配置过的部署不会因为「反正没人配」而接受任何外部主机名。生产要在反代后面
用真实主机名，必须由运维显式传 ``trusted_authorities``——这是本层 fail-closed
的那一半。

两条实现的关系由 ``tests/bff/test_host_guard.py`` 的同一张用例表钉住：两边对同一
组输入必须给出同样的「接受/拒绝」结论。规则只有一份语义，不是两份各自漂移的代码。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable

#: 环回的三种 ``Host`` 写法。``::1`` 在头里必须带方括号（``uri-host`` 的
#: IP-literal 形态），规范化之后不带——所以这个集合里的 ``::1`` 是**主机**，
#: 不是头里的写法。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

MIN_PORT = 1
MAX_PORT = 65535

#: 主机名总长上限（RFC 1035 §2.3.4 的 255 octets，含标签分隔点）。
MAX_HOST_LENGTH = 255
MAX_LABEL_LENGTH = 63

#: ``reg-name`` 的一个标签：字母数字开头结尾，中间允许连字符。
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)

#: 只认 ASCII 十进制数字的端口。``[0-9]`` 配 ``re.ASCII`` 让 ``１２３`` 进不来；
#: 最多五位，所以 ``65536`` 能到这里、由下面的范围检查挡掉。
_PORT_RE = re.compile(r"[0-9]{1,5}", re.ASCII)

#: 拒绝理由的稳定 token。前端与测试都读它，不读人话。
REASON_MISSING_HOST = "missing_host"
REASON_MALFORMED_HOST = "malformed_host"
REASON_FOREIGN_HOST = "foreign_host"
REASON_PORT_MISMATCH = "port_mismatch"

#: ``Host`` 权威不成立时的 HTTP 码。421 是 RFC 9110 §15.5.20 的
#: "Misdirected Request"：请求寄给了一个**不能**为这个权威作答的源服务器。
#: 与既有 sidecar 对未知 Host 的答复（421）保持一致。
MISDIRECTED_STATUS = 421


@dataclass(frozen=True)
class Authority:
    """一个已解析、已规范化的 ``Host`` 权威。"""

    host: str
    port: int | None = None

    @property
    def text(self) -> str:
        """可比较的规范写法。IPv6 带括号，与 ``Host`` 头里的写法一致。"""
        if self.port is None:
            return self.host if ":" not in self.host else f"[{self.host}]"
        return (
            f"{self.host}:{self.port}"
            if ":" not in self.host
            else f"[{self.host}]:{self.port}"
        )


@dataclass(frozen=True)
class HostRefusal:
    """一次拒绝：稳定的 ``reason`` + 给日志/响应体的人话 ``detail``。"""

    reason: str
    detail: str
    status: int = MISDIRECTED_STATUS


def _parse_reg_name(text: str) -> str | None:
    """``reg-name`` → 小写规范形；不是合法主机名就 ``None``。

    ``Host`` 里的主机名大小写不敏感，所以 ``LOCALHOST`` 与 ``localhost`` 是同一个
    权威——但只有 **ASCII** 大小写算数：``re.ASCII`` 的那条性质在这里由 ``isascii``
    把守，这样 Unicode 里那些「长得像」的字母（以及全角数字）不会被 ``lower()``
    折成 ASCII 再当成合法主机名收下。
    """
    if not text or len(text) > MAX_HOST_LENGTH or not text.isascii():
        return None
    lowered = text.lower()
    for label in lowered.split("."):
        if not _LABEL_RE.fullmatch(label):
            return None
    return lowered


def _parse_host(text: str) -> str | None:
    """``uri-host`` → 规范形主机；三种形态（IP-literal / IPv4 / reg-name）。"""
    if not text:
        return None
    if text.startswith("["):
        # IP-literal：必须以方括号收尾。没有收尾的 ``[`` 是畸形，不是主机名。
        if not text.endswith("]"):
            return None
        try:
            return ipaddress.IPv6Address(text[1:-1]).compressed
        except ValueError:
            return None
    if "]" in text:
        return None
    # 一个裸的 IPv4 字面量按数字校验，而不是按 reg-name 收下：``999.1.1.1``
    # 不是主机名，也不该因为「看着像」就被接受。
    if re.fullmatch(r"[0-9.]+", text, re.ASCII):
        try:
            return str(ipaddress.IPv4Address(text))
        except ValueError:
            return None
    return _parse_reg_name(text)


def parse_authority(raw: object) -> Authority | None:
    """``Host`` 头字段值 → :class:`Authority`，否则 ``None``。

    ``None`` 覆盖「外部主机」「畸形字段值」「端口超范围」三种情况，调用方不需要
    ——也不应该——把它们分开处理才能拒绝。语法的一半与数值的一半在这里一起判定，
    这样调用方不可能只做了一半。

    重复的 ``Host`` 头会被传输层黏成 ``a, b``；逗号不是主机名字符，所以那种值在
    这里自然是 ``None``，不需要单独一条「重复头」规则。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    # ``strip`` 之后与原值不同，说明字段值两端有空白——``uri-host`` 里没有空白，
    # 这是畸形，不是「礼貌地容错掉」。
    if text != raw or not text:
        return None

    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        host_text, rest = text[: end + 1], text[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port_text = rest[1:]
        else:
            port_text = None
    else:
        if text.count(":") > 1:
            # 未加括号的 IPv6，或某种冒号走私：都不是 ``Host = uri-host [":" port]``。
            return None
        host_text, sep, port_text = text.partition(":")
        if not sep:
            port_text = None

    host = _parse_host(host_text)
    if host is None:
        return None

    if port_text is None:
        return Authority(host=host, port=None)
    if not _PORT_RE.fullmatch(port_text):
        return None
    port = int(port_text)
    if not (MIN_PORT <= port <= MAX_PORT):
        return None
    return Authority(host=host, port=port)


def normalize_authority(text: object) -> str | None:
    """配置项里的权威写法 → 与 :attr:`Authority.text` 同一种规范形。

    运维配置可以写 ``portal.example.com``（任意端口）或 ``portal.example.com:8443``
    （只认这个端口）。写不出来就是配置错误，返回 ``None`` 由调用方拒绝启动。
    """
    parsed = parse_authority(text)
    if parsed is None:
        return None
    # 配置里可以写「裸主机名 + 端口」；``parse_authority`` 已经把它们规范过了。
    if parsed.port is None:
        return parsed.text
    return parsed.text


@dataclass(frozen=True)
class AuthorityPolicy:
    """哪些权威算「我们」。

    ``trusted`` 是规范形字符串（``host`` 或 ``host:port``）：写端口就必须精确匹配。
    ``allow_loopback`` 默认 ``True``：环回部署（开发进程、本机验收）不需要额外配置，
    而任何非环回权威都必须由运维显式登记。
    """

    trusted: frozenset[str] = frozenset()
    allow_loopback: bool = True

    @classmethod
    def build(
        cls,
        authorities: Iterable[str] = (),
        *,
        allow_loopback: bool = True,
    ) -> "AuthorityPolicy":
        """从配置字符串建策略。任何一条写不出来就抛 ``ValueError``。

        配置错误必须在启动时说清楚，而不是运行时静默地少信一条、或者多信一条。
        """
        normalized: set[str] = set()
        for raw in authorities:
            entry = normalize_authority(raw)
            if entry is None:
                raise ValueError(f"not a usable Host authority: {raw!r}")
            normalized.add(entry)
        return cls(trusted=frozenset(normalized), allow_loopback=allow_loopback)

    def allows(self, authority: Authority) -> bool:
        if authority.text in self.trusted:
            return True
        if authority.host in self.trusted:
            return True
        return bool(self.allow_loopback and authority.host in LOOPBACK_HOSTS)

    def check(
        self,
        host_header: object,
        *,
        server_port: object = None,
    ) -> HostRefusal | None:
        """头值 + 实际到达端口 → ``None`` 表示放行，否则给出拒绝理由。

        ``server_port`` 是 ``SERVER_PORT``：``wsgiref`` 从**绑定 socket** 填它，
        所以那是真正的监听端口，不是客户端说的第二份。头里写了端口、而我们**问不出**
        实际端口时，端口这一项不是「跳过」，是**不通过**——一个无法核对的声明
        不该被当成核对通过。
        """
        if host_header is None or (isinstance(host_header, str) and not host_header):
            return HostRefusal(
                reason=REASON_MISSING_HOST,
                detail=(
                    "The request carried no Host header, so its authority cannot "
                    "be checked."
                ),
            )

        authority = parse_authority(host_header)
        if authority is None:
            return HostRefusal(
                reason=REASON_MALFORMED_HOST,
                detail=(
                    "This server answers only to the authorities it serves, named "
                    "exactly, with no surrounding text."
                ),
            )

        if not self.allows(authority):
            return HostRefusal(
                reason=REASON_FOREIGN_HOST,
                detail="This server does not answer to that host.",
            )

        if authority.port is not None:
            actual = _as_port(server_port)
            if actual is None or authority.port != actual:
                return HostRefusal(
                    reason=REASON_PORT_MISMATCH,
                    detail=(
                        "The port in the Host header is not the port this server "
                        "is listening on."
                    ),
                )
        return None


def _as_port(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "LOOPBACK_HOSTS",
    "MAX_HOST_LENGTH",
    "MAX_PORT",
    "MIN_PORT",
    "MISDIRECTED_STATUS",
    "REASON_FOREIGN_HOST",
    "REASON_MALFORMED_HOST",
    "REASON_MISSING_HOST",
    "REASON_PORT_MISMATCH",
    "Authority",
    "AuthorityPolicy",
    "HostRefusal",
    "normalize_authority",
    "parse_authority",
]
