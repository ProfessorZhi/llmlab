"""同源 ``POST .../invoke`` 的形状层：把 Registry 的信封收成一份**可安全下发**的答复。

SEAM-1（Issue #68 的离线纵切）说的是：``backend/bff/route_map.py`` 不挂
``/api/v2/registry/*``，所以浏览器同源入口**没有**「B 调用 A 共享的 Endpoint」这条路，
V1-07 的浏览器入口因此不成立。

本模块不重新实现调用。授权、可见性、就绪、载荷上界、审计先于转发、Gateway 自己的
key/ACL 判定 —— 全部在 ``RegistryService.invoke`` 里，本层只做三件事：

1. 把**受信主体**（会话或 scoped token 解析出来的 ``RequesterContext``）交过去；
2. 把 Registry 的信封收成前端契约的形状；
3. 把 Router 的路由同步状态作为**来源事实**附上。

为什么响应要收窄
----------------

``TrustedGatewayAdapter`` 返回的 ``result`` 里带着 Gateway 的原始响应体
（``result.body``），测试替身还会带一个 ``echo``（把请求载荷原样回显）。把这两样
原样转发给浏览器，等于把这条新路由变成一条**任意内容通道**：一个请求可以把
prompt 送出去，再把上游说的任何东西取回来。V1-07 的验收需要的是**授权、归属、
``request_id`` 与撤权后立刻拒绝**这些元数据，不是模型正文。

所以这里只保留一组**元数据字段**（:data:`RESULT_FIELDS`），并且把
``body`` / ``echo`` 明确排除。**这是一条可以由 owner 推翻的产品决定**，不是安全不
变量：要展示模型正文，应当连同「谁能看、留存多久、是否脱敏」一起单独评审。写在一
个 `if` 里，好让它被看见，而不是藏在注释里。
"""

from __future__ import annotations

from typing import Iterable

SCHEMA_VERSION = "portal-invoke/1"

#: Registry 自己的 invoke 载荷硬上限（``registry.MAX_INVOKE_PAYLOAD_BYTES``）。
#: **本层不重抄它的值去做判定** —— 那是 Registry 的门。这里只记录前端应当知道的
#: 那个数；BFF 侧还有一道更小的整请求上界（``AuthConfig.max_request_bytes``），
#: 因此一次调用的有效上界是两者中**较小**的那个。
REGISTRY_MAX_PAYLOAD_BYTES = 64 * 1024

#: 从 Gateway adapter 的 ``result`` 里保留下来的键。**元数据，不是正文。**
RESULT_FIELDS = (
    "request_id",
    "synthetic",
    "gateway",
    "forwarded",
    "served_by",
    "http_status",
    "model",
    "tokens",
    "usage",
    "client_latency_ms",
)

#: 明确**不**下发的键，写出来是为了让「为什么不给」在代码里可读、可测。
WITHHELD_RESULT_FIELDS = ("body", "echo")


def _scalar_or_none(value: object) -> object:
    """只放行标量。一个嵌套结构不该成为一条未审阅的下发通道。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return None


def _bounded_usage(value: object) -> dict | None:
    """``usage`` 只保留整数字段。

    Gateway 报的 usage 是计费/计量口径，形状由它自己决定；本层只挑出数字，
    不把一个自由形状的 dict 整体转发。
    """
    if not isinstance(value, dict):
        return None
    out = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if not isinstance(key, str) or len(key) > 64:
            continue
        out[key] = item
    return out or None


def build_invoke_dto(envelope: object, *, router_sync: object = None) -> dict:
    """Registry 的 invoke 信封 ⇒ ``portal-invoke/1``。

    ``envelope`` 是 ``RegistryService.invoke`` 的返回值（``{"data": {...}, ...}``）。
    形状不对时返回一份**空但诚实**的块，而不是抛错：调用**已经发生过**，这时抛一个
    500 会让调用方以为它没发生。
    """
    data = (envelope or {}).get("data") if isinstance(envelope, dict) else None
    data = data if isinstance(data, dict) else {}

    gateway = data.get("gateway") if isinstance(data.get("gateway"), dict) else {}
    raw_result = data.get("result") if isinstance(data.get("result"), dict) else {}

    result = {}
    for key in RESULT_FIELDS:
        if key not in raw_result:
            continue
        value = raw_result[key]
        if key == "usage":
            value = _bounded_usage(value)
        else:
            value = _scalar_or_none(value)
        result[key] = value

    metering = data.get("metering") if isinstance(data.get("metering"), dict) else {}
    sync = router_sync if isinstance(router_sync, dict) else None

    return {
        "schema_version": SCHEMA_VERSION,
        "endpoint_id": _scalar_or_none(data.get("endpoint_id")),
        "route_path": _scalar_or_none(data.get("route_path")),
        "model": _scalar_or_none(data.get("model")),
        "deployment_id": _scalar_or_none(data.get("deployment_id")),
        "gateway": {
            "adapter": _scalar_or_none(gateway.get("adapter")),
            "attached": _scalar_or_none(gateway.get("attached")),
            "authorization": _scalar_or_none(gateway.get("authorization")),
        },
        "result": result,
        "metering": {
            "recorded": _scalar_or_none(metering.get("recorded")),
            "source": _scalar_or_none(metering.get("source")),
            "tokens": _scalar_or_none(metering.get("tokens")),
            "latency_ms": _scalar_or_none(metering.get("latency_ms")),
        },
        # Router 同步状态是**来源事实**，不是本层给出的判断。``applied`` 只由
        # Gateway 回读（revision + snapshot SHA）产生；运维回执是 ``attested``。
        "router_sync": {
            "state": _scalar_or_none(sync.get("state")),
            "pending": _scalar_or_none(sync.get("pending")),
            "applied": _scalar_or_none(sync.get("applied")),
            "applied_generation": _scalar_or_none(sync.get("applied_generation")),
            "current_generation": _scalar_or_none(sync.get("current_generation")),
            "evidence": _scalar_or_none(sync.get("evidence")),
            "reason": _scalar_or_none(sync.get("reason")),
        }
        if sync is not None
        else None,
        "notes": [
            "调用经 Registry 的行级 ACL 授权后由 Gateway adapter 转发；浏览器不直连上游。",
            "本路由只下发调用元数据，不含模型正文；正文是否可下发是一次单独的评审决定。",
            "``router_sync`` 原样转述 Registry 的行级事实：``applied`` 只由 Gateway 回读产生。",
        ],
    }


def withheld_fields(allow: Iterable[str] = RESULT_FIELDS) -> tuple[str, ...]:
    """``result`` 里被刻意扣下的键。给测试与文档一个可读取的答案。"""
    allowed = set(allow)
    return tuple(key for key in WITHHELD_RESULT_FIELDS if key not in allowed)


__all__ = [
    "REGISTRY_MAX_PAYLOAD_BYTES",
    "RESULT_FIELDS",
    "SCHEMA_VERSION",
    "WITHHELD_RESULT_FIELDS",
    "build_invoke_dto",
    "withheld_fields",
]
