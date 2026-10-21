"""「登记模型服务」之前的**只读**预检：四个检查，各自 ``{ok, detail}``。

前端登记表单在提交 ``POST /engines`` 之前，想先知道「这个目标能不能登记成功、
登记之后会处于什么状态」。本模块把这件事收敛成四个独立的检查：

* ``port_reachable`` —— 回环 TCP connect，短超时（:func:`tcp_connect_check`，可注入）；
* ``ownership`` —— 所有权 prover 的结论（见 ``ownership.py``）；
* ``openai_compat`` —— 协议 prober 的结论（只读 ``GET /v1/models``，见 ``health.py``）；
* ``name_unique`` —— 名字在调用者自己的 engine 名字空间里是否空着。

``ok`` 是**三值**的，刻意如此：

* ``True`` —— 查了，通过；
* ``False`` —— 查了，没通过（``detail`` 是机器可读的原因 token）；
* ``None`` —— **本部署没有这条来源**，没查。它不是失败，更不是通过 ——
  与仓库其余地方「UNKNOWN 不得显示成 0」同一条纪律。

本模块**不**写库、不写审计、不登记任何东西；它只把已经由服务层算好的结论
摆成前端要的形状，外加一个真正会开 socket 的函数（只连回环）。
"""

from __future__ import annotations

import socket
from typing import Callable

from .errors import InvalidRequest
from .health import PROTOCOL_UNKNOWN, ProtocolObservation
from .ownership import REASON_NO_OWNERSHIP_SOURCE, OwnershipProof
from .urls import NormalizedTarget, is_loopback_host

PREFLIGHT_SCHEMA = "registry-engine-preflight/1"

CHECK_PORT_REACHABLE = "port_reachable"
CHECK_OWNERSHIP = "ownership"
CHECK_OPENAI_COMPAT = "openai_compat"
CHECK_NAME_UNIQUE = "name_unique"
CHECKS = (CHECK_PORT_REACHABLE, CHECK_OWNERSHIP, CHECK_OPENAI_COMPAT, CHECK_NAME_UNIQUE)

DETAIL_NOT_CONFIGURED = "not_configured_in_this_deployment"
DETAIL_CONNECTED = "connected"
DETAIL_NAME_AVAILABLE = "available"
DETAIL_NAME_TAKEN = "engine_name_taken"
DETAIL_OWNERSHIP_VERIFIED = "verified"

#: 预检 connect 的超时上界。登记表单不该因为一个黑洞端口卡住好几秒。
MAX_CONNECT_TIMEOUT_SECONDS = 5.0


def tcp_connect_check(
    target: NormalizedTarget,
    *,
    timeout_seconds: float = 1.5,
    connect: Callable[..., object] | None = None,
) -> tuple[bool, str]:
    """对一个**回环**目标做一次 TCP connect。返回 ``(连上了吗, 原因 token)``。

    只连接、立刻关闭；不发任何字节。非回环目标直接拒绝（二次确认 —— 调用方拿到的
    本该已经是校验过的 ``NormalizedTarget``）。``connect`` 可注入，缺省
    ``socket.create_connection``。
    """
    if not isinstance(target, NormalizedTarget):
        raise InvalidRequest("target must be a NormalizedTarget")
    if not is_loopback_host(target.host):
        raise InvalidRequest(f"refusing to connect to non-loopback host {target.host!r}")
    timeout = float(timeout_seconds)
    if not (0 < timeout <= MAX_CONNECT_TIMEOUT_SECONDS):
        raise InvalidRequest(f"timeout_seconds must be in (0, {MAX_CONNECT_TIMEOUT_SECONDS}]")
    opener = connect if connect is not None else socket.create_connection
    try:
        connection = opener((target.host, target.port), timeout=timeout)
    except socket.timeout:
        return False, "timeout"
    except ConnectionRefusedError:
        return False, "connection_refused"
    except OSError as exc:
        return False, f"connect_failed:{type(exc).__name__}"
    close = getattr(connection, "close", None)
    if callable(close):
        close()
    return True, DETAIL_CONNECTED


def _check(ok: bool | None, detail: str) -> dict:
    return {"ok": ok, "detail": detail}


def port_check(
    target: NormalizedTarget, port_checker: Callable[[NormalizedTarget], tuple[bool, str]] | None
) -> dict:
    if port_checker is None:
        return _check(None, DETAIL_NOT_CONFIGURED)
    reachable, detail = port_checker(target)
    return _check(bool(reachable), str(detail))


def ownership_check(proof: OwnershipProof) -> dict:
    """``ok=None`` 只给「本部署没有所有权来源」；其余未证明一律 ``False``。"""
    if not isinstance(proof, OwnershipProof):
        raise InvalidRequest("ownership must be an OwnershipProof")
    if proof.verified:
        check = _check(True, DETAIL_OWNERSHIP_VERIFIED)
    else:
        reason = proof.reason or proof.method
        check = _check(None if reason == REASON_NO_OWNERSHIP_SOURCE else False, reason)
    # 佐证原样附上（method / uid / detail / source），好让表单说清「为什么」。
    check["evidence"] = proof.to_dict()
    return check


def protocol_check(observation: ProtocolObservation) -> dict:
    if not isinstance(observation, ProtocolObservation):
        raise InvalidRequest("protocol must be a ProtocolObservation")
    if observation.verified:
        return _check(True, observation.reason)
    if observation.state == PROTOCOL_UNKNOWN:
        return _check(None, observation.reason)
    return _check(False, observation.reason)


def name_check(existing_engine_id: str | None) -> dict:
    if existing_engine_id is None:
        return _check(True, DETAIL_NAME_AVAILABLE)
    return _check(False, DETAIL_NAME_TAKEN)


def build_engine_preflight(
    *,
    target: NormalizedTarget,
    name: str,
    existing_engine_id: str | None,
    ownership: OwnershipProof,
    port_checker: Callable[[NormalizedTarget], tuple[bool, str]] | None,
    protocol: ProtocolObservation,
) -> dict:
    """把服务层算好的结论摆成 ``registry-engine-preflight/1``。

    ``all_ok`` 只有在**四个检查都是 ``True``** 时才为真 —— ``None`` 不算通过。
    """
    checks = {
        CHECK_PORT_REACHABLE: port_check(target, port_checker),
        CHECK_OWNERSHIP: ownership_check(ownership),
        CHECK_OPENAI_COMPAT: protocol_check(protocol),
        CHECK_NAME_UNIQUE: name_check(existing_engine_id),
    }
    return {
        "schema": PREFLIGHT_SCHEMA,
        "read_only": True,
        "name": name,
        "target": target.to_dict(),
        "checks": checks,
        "all_ok": all(item["ok"] is True for item in checks.values()),
    }
