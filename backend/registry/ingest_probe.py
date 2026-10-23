"""§4.1 的输入侧：把 ``/proc/net/tcp{,6}`` 与一次只读探测变成状态机的输入。

与 :mod:`ingest` 分开，是因为这里的每个函数都碰外部世界（内核表、HTTP），
而状态机本身必须是纯的。探测也刻意只读：两次 GET，超时由调用方给（默认 1.5 s，
比状态机的 30 s 周期短一个数量级，慢服务不会拖住整轮）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ingest import ProbeOutcome, SeenPort
from .local_discovery import FRAMEWORK_PROBES, Fetch, parse_model_ids
from .ownership import ListeningSocket, ProcNetUnavailable, ProcNetUnparseable

#: §4.1 只说 127.0.0.1 / ::1。绑定在 0.0.0.0 的服务虽然也能从回环访问，但它同时
#: 暴露在局域网口上，不属于「平台内服务」的默认形态；要不要认它由部署决定。
LOOPBACK_LITERALS = frozenset({"127.0.0.1", "::1"})

DEFAULT_PROBE_TIMEOUT = 1.5


def seen_ports(
    listeners: list[ListeningSocket],
    *,
    include_wildcard: bool = False,
) -> list[SeenPort]:
    """把 LISTEN 行收敛成「回环上的端口 + 属主 uid」。

    同一端口若被多个 uid 同时监听（``SO_REUSEPORT`` 或 v4/v6 双栈），它是歧义的
    ——我们无法断言这个服务属于谁，因此**整条丢弃**：宁可少一个候选，不可把
    alice 的端口记到 bob 名下。
    """

    by_port: dict[int, set[int]] = {}
    hosts: dict[int, str] = {}
    for item in listeners:
        if item.ip not in LOOPBACK_LITERALS:
            if not include_wildcard or item.ip not in ("0.0.0.0", "::"):
                continue
        by_port.setdefault(item.port, set()).add(item.uid)
        hosts.setdefault(item.port, item.ip)

    result: list[SeenPort] = []
    for port, uids in sorted(by_port.items()):
        if len(uids) != 1:
            continue
        uid = next(iter(uids))
        result.append(SeenPort(uid=uid, port=port, host=hosts[port]))
    return result


def read_seen_ports(
    table,
    *,
    include_wildcard: bool = False,
) -> tuple[list[SeenPort], str | None]:
    """读表并返回 ``(端口, 问题)``；读不到表不是异常，是一个可上报的问题。"""

    try:
        listeners = table.listeners()
    except ProcNetUnavailable:
        return [], "proc_net_unreadable"
    except ProcNetUnparseable:
        return [], "proc_net_unparseable"
    return seen_ports(listeners, include_wildcard=include_wildcard), None


def framework_of(fetch: Fetch, port: int) -> str | None:
    """框架指纹：只认「这个框架才会给」的字段（与 ``local_discovery`` 同源）。"""

    for path, markers, name in FRAMEWORK_PROBES:
        answer = fetch(port, path)
        if answer is None:
            continue
        status, body = answer
        if not 200 <= status < 300:
            continue
        payload = _json(body)
        if isinstance(payload, dict) and markers <= payload.keys():
            return name
    return None


def probe_port(fetch: Fetch, port: int) -> ProbeOutcome:
    """一次探测 = `/v1/models` + 框架指纹。

    ``ok`` 的判据是**任意一个**证据成立：OpenAI 兼容的模型清单，或一个框架指纹。
    只拿到 404 的端口（sshd、Jupyter、随便什么）会被判失败，三次之后丢弃。
    """

    answer = fetch(port, "/v1/models")
    if answer is not None:
        status, body = answer
        if 200 <= status < 300:
            models = parse_model_ids(body)
            # 200 但体不是 {"data": [...]}：可能是别的服务恰好对任何路径都回 200，
            # 不能凭状态码就把它接进账本。
            if models is not None:
                return ProbeOutcome(
                    ok=True,
                    framework=framework_of(fetch, port),
                    model_ids=tuple(models),
                )
    framework = framework_of(fetch, port)
    if framework is not None:
        return ProbeOutcome(ok=True, framework=framework)
    return ProbeOutcome(ok=False, detail="未识别为 OpenAI 兼容服务")


def _json(body: bytes):
    import json  # noqa: PLC0415 - 只在这里用一次

    try:
        return json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
