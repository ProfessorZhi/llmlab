"""Engine 目标 URL 的 loopback allowlist（SSRF 防线）。

平台简化设计里 §「模型文件不用移动」的对偶：**服务也不必暴露**。用户在自己账户下
跑一个 engine，它的监听地址今天是本机 loopback 上的某个端口；Gateway 只接收
「共享 Endpoint」的请求。因此 Registry 接受的 engine target URL 只有一种形态：

    同主机 loopback + 显式端口 allowlist

这里做的是**严格解析后判定**，不是字符串前缀匹配。理由：前缀匹配在
``http://127.0.0.1.evil.com/``、``http://2130706433/``（十进制 IP）、
``http://127.0.0.1@evil.com/``（userinfo 混淆）这几种写法上会全数放行。

绝不自动接受 ``0.0.0.0``：它不是一个「地址」，而是一个**绑定意图** ——
接受它就等于把内网可达的监听地址登记成平台端点。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .errors import InvalidRequest

#: 允许的 scheme。``https`` 默认关闭（见 ``allow_https``）：本机回环上跑明文不构成
#: 额外暴露面，而打开 https 会让「同主机」这个前提多一层依赖（证书校验走谁）。
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: 明示拒绝的 scheme。单独列出来只是为了给出**准确**的错误信息，
#: 而不是让它们落进泛化的「不支持」里 —— ``file://`` 是被安全测试点名的一种。
EXPLICITLY_REJECTED_SCHEMES = frozenset(
    {
        "file",
        "gopher",
        "ftp",
        "ftps",
        "sftp",
        "tftp",
        "data",
        "dict",
        "ldap",
        "ldaps",
        "ssh",
        "scp",
        "jar",
        "netdoc",
        "unix",
        "ws",
        "wss",
        "blob",
    }
)

#: 字面 loopback 主机名 -> 归一化后的 IP 字面量。
#:
#: 刻意把 ``localhost`` **映射成字面量**而不是「放行这个名字」：这样连接路径上
#: 不存在任何一次 DNS 解析，也就不存在解析结果被改写的可能（DNS rebinding）。
LOOPBACK_HOST_ALIASES = {
    "localhost": "127.0.0.1",
    "ip6-localhost": "::1",
}

#: 允许的 loopback 字面量地址。
ALLOWED_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})

#: 端口 allowlist。刻意**不含** 22/80/443/3306/6379/5432 等常见服务端口：
#: 即使主机名限制在回环，一台机器上还跑着别的本机服务，端口是第二道边界。
#:
#: 也刻意**不含 8080**：该端口是既有上游推理服务（Strata）的地址，仓库里已有
#: 同向约定 ``backend.portal.modules.UPSTREAM_PORTS``。把 8080 排除在外，
#: 是「不自动暴露 0.0.0.0:8080」在**登记层**的对应动作 —— 平台不会替任何人
#: 把上游推理端口登记成一个共享 Endpoint。用户自己的 engine 应当绑定高位端口。
DEFAULT_ALLOWED_PORTS = frozenset({8000, 8001, 8081, 9000, 9001})

#: 与 ``backend.portal.modules.UPSTREAM_PORTS`` 对齐的保留端口集合。
#: 即便运维把它显式加进 allowlist，这里也会**先**拒绝，避免两处约定漂移。
RESERVED_UPSTREAM_PORTS = frozenset({8080})

#: 纯数字 / 十六进制 / 八进制主机名。这些是「同一个 IP 的另一种写法」，
#: 而那种写法恰好能绕过按字符串写的 allowlist —— 一律拒绝，不做解码。
_AMBIGUOUS_NUMERIC_HOST = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+|0[0-7]+)$")

#: URL 里不允许出现的控制字符（含 \n \r \t 与 DEL）。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class OperatorTarget:
    """一条由**运维/root 拥有**的策略所承认的既有服务目标。

    存在理由是一个具体的冲突：既有的上游推理服务（Strata）就在
    ``127.0.0.1:8080``，而 8080 被 :data:`RESERVED_UPSTREAM_PORTS` 保留着。
    用户当然不该能占用 8080，但运维**需要**能把那台已经跑着的服务登记进来。

    所以放宽口子只有一种形状：**精确匹配** (scheme, host, port) + 路径前缀，
    且这条 entry 必须来自 root/operator 拥有的配置文件。它**不**放宽任何别的
    边界 —— host 仍然必须是 loopback 字面量，scheme 仍然只允许 http(s)，
    userinfo / 歧义数字主机 / 路径穿越一照旧拒绝。
    """

    scheme: str
    host: str
    port: int
    path_prefix: str = "/"
    label: str = ""
    source: str = "operator_policy"

    def __post_init__(self) -> None:
        if self.scheme not in ALLOWED_SCHEMES:
            raise InvalidRequest(f"operator target scheme must be http(s): {self.scheme!r}")
        if not is_loopback_host(self.host):
            # 运维策略也**不能**把目标放到回环之外。这条边界没有例外。
            raise InvalidRequest(
                f"operator target host must be a loopback literal: {self.host!r}"
            )
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not (
            1 <= self.port <= 65535
        ):
            raise InvalidRequest(f"operator target port out of range: {self.port!r}")
        if not isinstance(self.path_prefix, str) or not self.path_prefix.startswith("/"):
            raise InvalidRequest("operator target path_prefix must start with '/'")
        if ".." in self.path_prefix:
            raise InvalidRequest("operator target path_prefix must not contain '..'")

    def matches(self, *, scheme: str, host: str, port: int, path: str) -> bool:
        """**精确**匹配。刻意不做通配、不做正则、不做子串：一条策略 entry 只承认一个端点。"""
        if scheme != self.scheme or port != self.port:
            return False
        if host != self.host:
            return False
        prefix = self.path_prefix if self.path_prefix.endswith("/") else self.path_prefix + "/"
        candidate = path if path.endswith("/") else path + "/"
        return candidate.startswith(prefix) or path == self.path_prefix

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "path_prefix": self.path_prefix,
            "label": self.label,
            "source": self.source,
        }


@dataclass(frozen=True)
class NormalizedTarget:
    """校验通过后的规范形态。

    ``url`` 是**由本模块重新拼出来**的，不是调用方输入的原文 —— 后面所有地方
    （健康探测、路由配置）都用它，避免「校验过的字符串」与「真正用来连接的东西」
    是两个不同的值。

    ``operator_blessed`` 记录这次放行**是不是**靠一条运维策略换来的（例如 8080）。
    它进 DTO 与审计：一次「绕过保留端口」的登记必须在数据里看得见，
    而不是只在某个 if 分支里发生过。
    """

    scheme: str
    host: str
    port: int
    path: str
    query: str
    url: str
    operator_blessed: bool = False
    operator_label: str | None = None

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "query": self.query,
            "url": self.url,
            "operator_blessed": self.operator_blessed,
            "operator_label": self.operator_label,
        }

    @property
    def origin(self) -> str:
        """``scheme://host:port`` —— 供探测与运维策略匹配用的**基址**（不含路径）。

        协议验证只在这个基址上加**白名单路径**，绝不使用用户提供的任意路径：
        否则「验证协议」就变成了一个可以指向任意本机路径的探测器。
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def _reject(reason: str) -> InvalidRequest:
    return InvalidRequest(f"engine target url rejected: {reason}")


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def validate_engine_target_url(
    raw: object,
    *,
    allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    allow_https: bool = False,
    allow_localhost_name: bool = True,
    operator_allowlist: tuple[OperatorTarget, ...] = (),
) -> NormalizedTarget:
    """把 ``raw`` 解析成受信的 :class:`NormalizedTarget`，否则抛 ``InvalidRequest``。

    判定顺序即安全边界顺序：先 scheme，再 userinfo，再 host，最后 port/path。

    ``operator_allowlist`` 是本函数**唯一**的放宽口子，且它只放宽「端口」这一条：
    命中一条运维策略 entry 时，保留端口（8080）与端口 allowlist 都放行，
    host / scheme / userinfo / 路径穿越等所有其它边界**照旧**。
    """
    if not isinstance(raw, str):
        raise _reject("must be a string")

    candidate = raw.strip()
    if not candidate:
        raise _reject("must not be empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise _reject(f"longer than {MAX_URL_LENGTH} characters")
    if _CONTROL_CHARS.search(candidate):
        raise _reject("contains control characters")
    if any(ch.isspace() for ch in candidate):
        raise _reject("contains whitespace")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:  # 例如非法端口、畸形 IPv6
        raise _reject(f"unparseable: {exc}") from exc

    scheme = (parts.scheme or "").lower()
    if scheme in EXPLICITLY_REJECTED_SCHEMES:
        # 点名 ``file``：它读的是**本机文件系统**，与「登记一个 HTTP 服务」是两回事。
        raise _reject(f"scheme {scheme!r} is not permitted for engine targets")
    if scheme not in ALLOWED_SCHEMES:
        raise _reject(f"unsupported scheme {scheme!r}")
    if scheme == "https" and not allow_https:
        raise _reject("https targets are disabled in this deployment")

    # userinfo 混淆：``http://127.0.0.1@evil.com/`` 的真实主机是 evil.com。
    # 任何 userinfo 一律拒绝（回环服务不需要凭据，凭据也不该出现在 URL 里）。
    if parts.username is not None or parts.password is not None:
        raise _reject("userinfo (user:pass@) is not permitted")

    if parts.fragment:
        raise _reject("fragment is not permitted")

    host = parts.hostname
    if not host:
        raise _reject("missing host")
    if "%" in host:
        # IPv6 zone id（fe80::1%eth0）会让「同一个地址」有多种写法。
        raise _reject("zone identifiers are not permitted")
    if host.endswith("."):
        raise _reject("trailing dot in host is not permitted")

    lowered = host.lower()
    if _AMBIGUOUS_NUMERIC_HOST.match(lowered):
        # 2130706433 == 127.0.0.1，0x7f000001 同理。allowlist 是按字面量写的，
        # 而这些写法在别的解析器里指向同一个地址 —— 不做解码，直接拒绝。
        raise _reject(f"ambiguous numeric host {host!r}")

    if lowered in LOOPBACK_HOST_ALIASES:
        if not allow_localhost_name:
            raise _reject("localhost name is not permitted; use 127.0.0.1")
        resolved = LOOPBACK_HOST_ALIASES[lowered]
    else:
        resolved = lowered

    try:
        address = ipaddress.ip_address(resolved)
    except ValueError as exc:
        # 任何 DNS 名字都到此为止：本部署没有「解析后再判断」的路径。
        raise _reject(f"host {host!r} is not a loopback literal") from exc

    if address.is_unspecified:  # 0.0.0.0 / ::
        raise _reject("0.0.0.0 (unspecified) is a bind intent, not a target")
    if not address.is_loopback or str(address) not in ALLOWED_LOOPBACK_ADDRESSES:
        raise _reject(f"host {host!r} is not in the loopback allowlist")

    try:
        port = parts.port
    except ValueError as exc:
        raise _reject(f"invalid port: {exc}") from exc
    if port is None:
        port = _default_port(scheme)

    raw_path = parts.path or "/"
    blessed: OperatorTarget | None = None
    for entry in operator_allowlist:
        if isinstance(entry, OperatorTarget) and entry.matches(
            scheme=scheme, host=str(address), port=port, path=raw_path
        ):
            blessed = entry
            break

    if blessed is None:
        if port in RESERVED_UPSTREAM_PORTS:
            # 与 backend.portal.modules.UPSTREAM_PORTS 同向：上游推理端口不是
            # 用户 engine 的登记目标，无论 allowlist 怎么配。
            # 唯一的例外是命中一条运维策略的既有服务（见 OperatorTarget）。
            raise _reject(
                f"port {port} is reserved for the upstream service; "
                "registering it requires an operator-owned allowlist entry"
            )
        if port not in allowed_ports:
            raise _reject(f"port {port} is not in the allowlist")

    path = raw_path
    decoded = unquote(path)
    if _CONTROL_CHARS.search(decoded) or "\\" in decoded or "\\" in path:
        raise _reject("path contains control characters or backslashes")
    segments = [seg for seg in decoded.split("/")]
    if any(seg == ".." for seg in segments):
        # ``%2e%2e`` 解码后也是 ``..``，同一条判断覆盖。
        raise _reject("path contains a parent-directory segment")
    normalized_segments = [seg for seg in segments if seg not in ("", ".")]
    path = "/" + "/".join(normalized_segments)
    if normalized_segments and decoded.endswith("/"):
        path += "/"

    query = parts.query
    if _CONTROL_CHARS.search(query):
        raise _reject("query contains control characters")

    host_for_url = f"[{address}]" if address.version == 6 else str(address)
    url = f"{scheme}://{host_for_url}:{port}{path}"
    if query:
        url = f"{url}?{query}"

    return NormalizedTarget(
        scheme=scheme,
        host=str(address),
        port=port,
        path=path,
        query=query,
        url=url,
        operator_blessed=blessed is not None,
        operator_label=(blessed.label or blessed.source) if blessed is not None else None,
    )


def is_loopback_host(host: str) -> bool:
    """``host`` 是否落在这个部署接受的 loopback allowlist 内（供别处复用）。"""
    if not isinstance(host, str) or not host:
        return False
    lowered = host.lower()
    if lowered in LOOPBACK_HOST_ALIASES:
        lowered = LOOPBACK_HOST_ALIASES[lowered]
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_loopback and str(address) in ALLOWED_LOOPBACK_ADDRESSES
