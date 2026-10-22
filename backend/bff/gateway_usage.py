"""把 Gateway 的 ``usage.jsonl`` 读成观测层能吃的那份 JSONL。

为什么需要这一层：``backend/observability/gateway.py`` 的入参契约窄得是对的
（``outcome ∈ {success, failed, cancelled}``、``stream`` 必须是布尔、token 用
``input_tokens`` / ``output_tokens``），而生产 Gateway 写的是自己的一套名字
（``ok`` / ``rate_limited``、``stream: null``、``prompt_tokens`` / ``completion_tokens``）。
两边都不该为对方改：观测层的契约是安全边界，Gateway 是一台**已经跑着的**既有服务。
于是中间放一层**翻译**。它只做三件事，每一件都能拿原始行复核：

1. **改名**（不是改数）：``ok→success``、``rate_limited→failed``（429 的形状由
   ``status`` 表达，观测层会单独数 ``throttled_429``）、``prompt_tokens→input_tokens``、
   ``completion_tokens→output_tokens``、``stream: null → false``。数值一律原样搬。
2. **端点归属**：Gateway 的行里 ``model`` 是模型名，观测层的可见性判据却是 Registry
   的 **endpoint id**。两个名字之间的对应关系是**登记事实**（endpoint → deployment →
   model），所以这里是查表。查不到（模型没登记、或同一个模型挂了多个 endpoint）
   就不写 ``endpoint``：那一行会老实地落进 ``endpoint_missing``，而不是被塞进一个
   猜出来的端点。
3. **读尾巴**：只读文件最后一段（字节上限），并丢掉被截断的第一行 —— 半行 JSON
   既不该算 malformed，也不该被当成完整观测。

**这里不产生任何行**：文件不存在、读不了、是空的，就返回空串，并把这些事实写进
:meth:`GatewayUsageFeed.summary`，由页面如实显示「未接入 / 读不到 / 日志为空」。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

#: 生产 Gateway 的默认用量日志（``llmlab:llm-team``，组读）。
DEFAULT_USAGE_PATH = "/srv/llm-lab/state/api-gateway/usage.jsonl"

#: 生产 Gateway 的默认 key 台账（同一个目录）。
DEFAULT_KEYS_PATH = "/srv/llm-lab/state/api-gateway/keys.json"

#: 一次最多读这么多字节。日志会一直长，读全量等于把请求延迟绑在文件大小上。
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: 观测层的 outcome 词表。Gateway 侧的名字映射过去；映射不了的行不翻译。
OUTCOME_ALIASES = {
    "ok": "success",
    "success": "success",
    "failed": "failed",
    "failure": "failed",
    "rate_limited": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

#: token 字段改名。
TOKEN_ALIASES = (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"))

#: 原样搬过去的时间量字段。
DURATION_FIELDS = ("queue_ms", "ttft_ms", "upstream_ms", "total_ms")

#: 原样搬过去的字符串字段。
PLAIN_FIELDS = ("ts", "key_id", "label", "method", "path", "model")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalise_row(row, *, endpoint_for: Mapping[str, str] | None = None) -> dict | None:
    """一行 Gateway 记录 ⇒ 观测层契约的一行。翻译不了返回 ``None``。

    ``None`` **不是**「丢掉就算了」：调用方会把它们计数（``unmapped``），所以
    「读到了 173 行、翻译不了 0 行」与「翻译不了 173 行」在页面上是可区分的。
    """
    if not isinstance(row, dict):
        return None
    key_id = row.get("key_id")
    if not isinstance(key_id, str) or not key_id.strip():
        return None
    status = row.get("status")
    if not _is_int(status) or not 100 <= status <= 599:
        return None

    raw_outcome = row.get("outcome")
    outcome = OUTCOME_ALIASES.get(
        raw_outcome.strip().lower() if isinstance(raw_outcome, str) else ""
    )
    if outcome is None:
        return None

    out: dict = {"key_id": key_id.strip(), "status": status, "outcome": outcome}
    for name in PLAIN_FIELDS:
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()
    if "ts" not in out:
        # 没有时间戳的行无法落进任何窗口，也无法证明它属于哪个桶。
        return None
    for name in DURATION_FIELDS:
        value = row.get(name)
        if _is_number(value):
            out[name] = value
    bytes_out = row.get("bytes_out")
    if _is_int(bytes_out) and bytes_out >= 0:
        out["bytes_out"] = bytes_out

    # ``stream`` 在 Gateway 里只有「是流式」时才写 true，其余是 null。观测层要布尔。
    stream = row.get("stream")
    out["stream"] = stream is True

    usage = row.get("usage")
    if isinstance(usage, dict):
        mapped = {}
        for source_name, target_name in TOKEN_ALIASES:
            value = usage.get(source_name)
            if _is_int(value):
                mapped[target_name] = value
        if mapped:
            out["usage"] = mapped

    if endpoint_for:
        target = endpoint_for.get(out.get("model", ""))
        if target:
            out["endpoint"] = target
    return out


def read_tail(path: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, int]:
    """读文件最后 ``max_bytes`` 字节。返回 ``(text, 实际字节数)``。

    截断点在行中间时丢掉那段残行：它既不是合法 JSON，也不该污染 ``malformed``。
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        handle.seek(start)
        blob = handle.read()
    if start > 0:
        first_newline = blob.find(b"\n")
        blob = b"" if first_newline < 0 else blob[first_newline + 1 :]
    return blob.decode("utf-8", errors="replace"), len(blob)


class GatewayUsageFeed:
    """一台 Gateway 的用量日志 ⇒ 每次请求可用的规范化 JSONL。

    ``endpoint_for(identity)`` 是**按会话**给的 ``model -> endpoint_id`` 表：
    可见端点本来就是按会话算的（Registry 的 ACL），所以这张表也只能按会话建。
    不传就直接不写 ``endpoint``（全部落 ``endpoint_missing``）。
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        endpoint_for: Callable[[object], Mapping[str, str]] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        window_hours: int = 24,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path or DEFAULT_USAGE_PATH
        self._endpoint_for = endpoint_for
        self._max_bytes = max_bytes
        self._window_hours = window_hours
        self._now = now or (lambda: datetime.now(timezone.utc))
        #: 上一次读盘的结果，按 ``(mtime_ns, size)`` 作废。日志只在写入时变。
        self._cache: tuple[tuple[int, int], list, dict] | None = None

    # -- 读盘 --------------------------------------------------------------

    def _load(self) -> tuple[list, dict]:
        try:
            stat = os.stat(self.path)
        except OSError as exc:
            return [], {
                "readable": False,
                "error": exc.__class__.__name__,
                "bytes_read": 0,
                "lines": 0,
                "unmapped": 0,
            }
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == signature:
            return self._cache[1], self._cache[2]

        text, bytes_read = read_tail(self.path, max_bytes=self._max_bytes)
        rows: list = []
        unmapped = 0
        lines = 0
        for raw in text.splitlines():
            if not raw.strip():
                continue
            lines += 1
            try:
                parsed = json.loads(raw)
            except ValueError:
                unmapped += 1
                continue
            row = normalise_row(parsed)
            if row is None:
                unmapped += 1
                continue
            rows.append(row)
        stats = {
            "readable": True,
            "error": None,
            "bytes_read": bytes_read,
            "lines": lines,
            "unmapped": unmapped,
        }
        self._cache = (signature, rows, stats)
        return rows, stats

    # -- 给观测层 ----------------------------------------------------------

    def text(self, identity) -> str:
        """该会话可用的规范化 JSONL 文本。端点表按会话现算。"""
        rows, _stats = self._load()
        if not rows:
            return ""
        endpoint_map = self._endpoint_for(identity) if self._endpoint_for else None
        out: list[str] = []
        for row in rows:
            item = dict(row)
            if endpoint_map:
                target = endpoint_map.get(item.get("model", ""))
                if target:
                    item["endpoint"] = target
            out.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(out) + "\n"

    # -- 给页面 ------------------------------------------------------------

    def summary(self) -> dict:
        """这份源本身的状况。**不含任何按人的数字**，所以谁都能看。"""
        rows, stats = self._load()
        last = None
        in_window = 0
        if rows:
            start = self._now() - timedelta(hours=self._window_hours)
            for row in rows:
                stamp = row.get("ts")
                if not isinstance(stamp, str):
                    continue
                if last is None or stamp > last:
                    last = stamp
                try:
                    moment = datetime.fromisoformat(
                        stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
                    )
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if moment >= start:
                    in_window += 1
        return {
            "feed": "gateway-usage-export",
            "path": self.path,
            "window_hours": self._window_hours,
            "readable": stats["readable"],
            "error": stats["error"],
            "bytes_read": stats["bytes_read"],
            "lines": stats["lines"],
            "unmapped": stats["unmapped"],
            "events": len(rows),
            "last_event_ts": last,
            "in_window": in_window,
        }


def load_key_owners(path: str | None) -> dict[str, str]:
    """读 ``gateway-key-owners.json``：``key_id -> 主体名`` 的**人工声明**。

    为什么需要它：Gateway 的 ``keys.json`` 只有 ``id/label/note/requests…``，
    **没有属主字段**。``label`` 是签发时代写的名字，不是一条归属声明；拿它去猜
    「这条调用是谁的」正是观测层明令禁止的事（归属只能来自受信 key 目录）。

    所以归属必须由人显式写下来。文件形状::

        {
          "schema": "gateway-key-owners/1",
          "keys": {
            "k_7a0684a0": {"owner": "linux:<host_id>:1003", "evidence": "…"}
          }
        }

    文件不存在 = 没有任何 key 有属主 = 所有调用都归不了人（这是诚实结局，不是错误）。
    解析失败同样回空表：宁可「归不了人」，也不拿半份声明去归属。
    """
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    entries = document.get("keys")
    if not isinstance(entries, dict):
        return {}
    owners: dict[str, str] = {}
    for key_id, entry in entries.items():
        if not isinstance(key_id, str) or not key_id.strip():
            continue
        owner = entry.get("owner") if isinstance(entry, dict) else entry
        if isinstance(owner, str) and owner.strip():
            owners[key_id.strip()] = owner.strip()
    return owners


__all__ = [
    "DEFAULT_KEYS_PATH",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_USAGE_PATH",
    "GatewayUsageFeed",
    "load_key_owners",
    "normalise_row",
    "read_tail",
]
