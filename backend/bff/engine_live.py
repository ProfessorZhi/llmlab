"""引擎实时状态：由 BFF **代取**，密钥不出后端。

Strata 的 ``GET /metrics`` 要 ``Authorization: Bearer``，而它自带控制台的
Settings 里要人手填一把 key。让使用者去填等于把一把可用凭据塞进浏览器
（历史、扩展、截屏都会留下它），所以这里反过来做：BFF 拿着引擎自己的 key
去问，前端只调本模块的路由，**永远拿不到 key 本身**。

三条口径：

1. **读不到就是读不到。** ``reachable: false`` + ``reason``，数值字段一律
   ``None`` —— 不能填 0。排队 0 条和「不知道排了没有」是两件事。
2. **只搬运元数据。** Strata 的 ``/metrics`` 本来就不含提示词与输出正文，
   这里再投影一次，只留时延、token 数、状态；不转述任何请求内容。
3. **不缓存。** 这是「现在怎么样」的读数，隔一秒的旧值比没有更糟。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENGINE_LIVE_SCHEMA = "engine-live/1"
ENGINE_LIVE_SOURCE = "strata-metrics-proxied"

#: 引擎自己的那份 key：``STRATA_API_KEY=<key1>,<key2>…``，**第一条是引擎本体的**，
#: 后面才是发给外部使用者的。取第一条 —— 它由部署生成，不随交付轮换。
DEFAULT_KEY_FILE = "~/.config/strata/api-key.env"
DEFAULT_URL = "http://127.0.0.1:8080/metrics"

#: 引擎在本机，环回；2 秒足够，超时说明它已经不正常了，不该拖住整个页面。
DEFAULT_TIMEOUT_S = 2.0

#: 最近请求只带这么多条：再多就是「请求日志」该干的事，而那条路要走网关。
RECENT_LIMIT = 5


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def read_engine_key(path: str | None = None) -> str | None:
    """从引擎环境文件里取第一条 key；读不到就没有（不是空串）。"""
    target = os.path.expanduser(path or DEFAULT_KEY_FILE)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line.startswith("STRATA_API_KEY="):
            continue
        raw = line.split("=", 1)[1]
        for item in raw.split(","):
            item = item.strip()
            if item and not item.startswith("#"):
                return item
    return None


def _unreachable(reason: str, observed_at: str) -> dict:
    """失败时的形状必须与成功时**同一套字段**，只是值全是 None。

    这样前端不需要两套渲染路径，也不会把「没有数」误读成「数是 0」。
    """
    return {
        "schema_version": ENGINE_LIVE_SCHEMA,
        "source": ENGINE_LIVE_SOURCE,
        "synthetic": False,
        "readonly": True,
        "observed_at": observed_at,
        "reachable": False,
        "reason": reason,
        "engine": None,
        "live": None,
        "throughput": None,
        "totals": None,
        "recent": None,
    }


def _num(value):
    """只放行真正的数；其余一律 None（不是 0，也不是字符串 "0"）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def project(payload: object, *, observed_at: str) -> dict:
    """把 Strata 的 ``/metrics`` 投影成这张窄表。形状不对就整份判坏。"""
    if not isinstance(payload, dict):
        return _unreachable("bad_payload", observed_at)
    engine = payload.get("engine") if isinstance(payload.get("engine"), dict) else {}
    live = payload.get("live") if isinstance(payload.get("live"), dict) else {}

    slots = []
    for item in live.get("slots") or []:
        if isinstance(item, dict):
            slots.append({
                "index": _num(item.get("slot")),
                "state": item.get("state") if isinstance(item.get("state"), str) else None,
                "held_tokens": _num(item.get("held_tokens")),
            })

    recent = []
    for item in (payload.get("requests") or [])[:RECENT_LIMIT]:
        if not isinstance(item, dict):
            continue
        at = _num(item.get("time"))
        recent.append({
            "at": _iso(at) if at is not None else None,
            "duration_s": _num(item.get("duration_s")),
            "finish": item.get("finish") if isinstance(item.get("finish"), str) else None,
            "prompt_tokens": _num(item.get("prompt_tokens")),
            "output_tokens": _num(item.get("output_tokens")),
            "decode_tok_s": _num(item.get("decode_tok_s")),
        })

    totals = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    since = _num(totals.get("since"))

    return {
        "schema_version": ENGINE_LIVE_SCHEMA,
        "source": ENGINE_LIVE_SOURCE,
        "synthetic": False,
        "readonly": True,
        "observed_at": observed_at,
        "reachable": True,
        "reason": None,
        "engine": {
            "model": engine.get("model") if isinstance(engine.get("model"), str) else None,
            "max_context": _num(engine.get("max_context")),
            "images": engine.get("images") if isinstance(engine.get("images"), bool) else None,
            "batch_slots": _num(engine.get("batch_slots")),
            "version": engine.get("version") if isinstance(engine.get("version"), str) else None,
            "state": live.get("state") if isinstance(live.get("state"), str) else None,
            "parallel": _num(live.get("parallel")),
        },
        "live": {
            "state": live.get("state") if isinstance(live.get("state"), str) else None,
            "running": _num(live.get("running")),
            "queued": _num(live.get("queued")),
            "waiting": _num(live.get("waiting")),
            "slots": slots,
        },
        "throughput": {
            "tok_s": _num(live.get("tok_s")),
            "tok_s_mean": _num(live.get("tok_s_mean")),
            "prefill_tok_s_mean": _num(live.get("prefill_tok_s_mean")),
        },
        "totals": {
            "since": _iso(since) if since is not None else None,
            "requests": _num(totals.get("requests")),
            "prompt_tokens": _num(totals.get("prompt_tokens")),
            "output_tokens": _num(totals.get("output_tokens")),
        },
        "recent": recent,
    }


def fetch_engine_metrics(*, url: str = DEFAULT_URL, key: str, timeout: float = DEFAULT_TIMEOUT_S) -> object:
    """取一次 ``/metrics``。异常不让它冒出去 —— 返回异常对象，由调用方分类。"""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": "Bearer " + key},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_engine_live(
    *,
    url: str = DEFAULT_URL,
    key_file: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    clock=time.time,
    fetch=None,
) -> dict:
    observed_at = _iso(clock())
    key = read_engine_key(key_file)
    if not key:
        # 部署没配 key ≠ 引擎坏了。这两件事在页面上要说得不一样。
        return _unreachable("key_not_configured", observed_at)
    getter = fetch or fetch_engine_metrics
    try:
        payload = getter(url=url, key=key, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return _unreachable("unauthorized" if exc.code in (401, 403) else "http_error", observed_at)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _unreachable("unreachable", observed_at)
    except ValueError:
        return _unreachable("bad_payload", observed_at)
    return project(payload, observed_at=observed_at)


__all__ = [
    "ENGINE_LIVE_SCHEMA",
    "ENGINE_LIVE_SOURCE",
    "DEFAULT_KEY_FILE",
    "DEFAULT_URL",
    "read_engine_key",
    "read_engine_live",
    "project",
]
