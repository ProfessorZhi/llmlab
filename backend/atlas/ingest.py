"""Gateway 元数据行的解析与校验（纯函数）。

输入是 Gateway 现有 ``usage.jsonl`` 形状的**元数据**行：

``ts,key_id,label,method,path,model,status,outcome,stream,usage,bytes_out,queue_ms,ttft_ms,upstream_ms,total_ms``

可选字段 ``request_id`` 见 :data:`MAX_REQUEST_ID_LEN`。

这里不做授权、不做分组，只做「一行是否可被信任为一条观测」的判定。
任何含 prompt / completion / messages 等**正文**字段的行一律拒绝。
未知字符段（例如伪造的 ``x_project``）被静默忽略 —— 既不采信，也不报错。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import InputLimitExceeded, InvalidRequest

#: 允许的 outcome 取值（严格白名单）。
ALLOWED_OUTCOMES = frozenset({"success", "failed", "cancelled"})

#: 一旦出现即视为「正文泄漏」而拒绝该行的字段名（大小写不敏感）。
CONTENT_FIELD_NAMES = frozenset(
    {
        "prompt",
        "prompts",
        "completion",
        "completions",
        "messages",
        "content",
        "text",
        "input_text",
        "output_text",
    }
)

_DURATION_FIELDS = ("queue_ms", "ttft_ms", "upstream_ms", "total_ms")
_STRING_FIELDS = ("label", "method", "path", "model")

#: ``request_id`` 的最大长度（字符）。
#:
#: 它是上游签发的**不透明句柄**，只用于「这条调用是哪一条」的对账，而不是内容通道。
#: 给一个上界是为了挡住「把整段正文塞进 request_id 当元数据」这种绕行：现有契约
#: 拒绝所有正文类字段，但如果 request_id 可以任意长，正文就只是换了个字段名。
#: 超长 / 含控制字符的 ``request_id`` 与坏行同等处理（该行计入 malformed）。
MAX_REQUEST_ID_LEN = 128

#: ``request_id`` 中不允许出现的字符类别：C0/C1 控制字符与 DEL。
#:
#: 换行尤其重要 —— 日志是逐行 JSONL，一个带 ``\n`` 的 id 能让下游读者把一行读成两行。
_CONTROL_CHARS = frozenset(chr(c) for c in list(range(0x20)) + [0x7F] + list(range(0x80, 0xA0)))


class _InvalidRow(Exception):
    """内部信号：这一行不可信，计入 malformed。"""


@dataclass(frozen=True)
class IngestLimits:
    """滥用上限。默认值即产品默认，测试可覆写。"""

    max_bytes: int = 32 * 1024 * 1024
    max_lines: int = 200_000
    max_line_bytes: int = 64 * 1024


@dataclass(frozen=True)
class RawEvent:
    """一条通过校验的 Gateway 元数据观测。"""

    ts: datetime
    key_id: str
    label: str | None
    method: str | None
    path: str | None
    model: str | None
    status: int
    outcome: str
    stream: bool
    input_tokens: int | None
    output_tokens: int | None
    bytes_out: int | None
    queue_ms: float | None
    ttft_ms: float | None
    upstream_ms: float | None
    total_ms: float | None
    #: 上游签发的不透明调用句柄；缺省为 ``None``。**永不**由本模块编造：
    #: 没有就是没有，编一个「看起来像真的」的 id 会让对账指向一条不存在的调用。
    request_id: str | None = None


@dataclass(frozen=True)
class IngestResult:
    events: tuple[RawEvent, ...]
    total_lines: int
    malformed_lines: int
    content_field_rejected: int


def _reject_json_constant(name: str) -> Any:
    """JSON 里的裸 ``NaN`` / ``Infinity`` 不是合法 JSON —— 直接判该行为坏行。"""
    raise ValueError(f"non-finite JSON constant not allowed: {name}")


def _has_content_field(row: dict) -> bool:
    return any(str(key).lower() in CONTENT_FIELD_NAMES for key in row)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _InvalidRow("ts must be a non-empty string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _InvalidRow("ts is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidRow("ts must carry an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _parse_duration(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidRow(f"{field_name} must be a number or null")
    number = float(value)
    if not math.isfinite(number):
        raise _InvalidRow(f"{field_name} must be finite")
    if number < 0:
        raise _InvalidRow(f"{field_name} must not be negative")
    return number


def _parse_token_count(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InvalidRow(f"{field_name} must be a non-negative integer or null")
    if value < 0:
        raise _InvalidRow(f"{field_name} must not be negative")
    return value


def _parse_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidRow(f"{field_name} must be a string or null")
    return value


def _parse_request_id(value: Any) -> str | None:
    """解析可选的不透明调用句柄。缺省 / 空白 -> ``None``；不合法 -> 坏行。

    这里的严格程度和 ``key_id`` 一致，但**不**做「清洗后放行」：与其把上游给的
    字符串改写成一个我们自己的版本（对账时两边对不上），不如判该行不可信。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidRow("request_id must be a string or null")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_REQUEST_ID_LEN:
        raise _InvalidRow(f"request_id exceeds max length {MAX_REQUEST_ID_LEN}")
    if any(char in _CONTROL_CHARS for char in text):
        raise _InvalidRow("request_id must not contain control characters")
    return text


def _build_event(row: dict) -> RawEvent:
    key_id = row.get("key_id")
    if not isinstance(key_id, str) or not key_id.strip():
        raise _InvalidRow("key_id must be a non-empty string")

    status = row.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise _InvalidRow("status must be an integer")
    if not 100 <= status <= 599:
        raise _InvalidRow("status must be a valid HTTP status code")

    outcome = row.get("outcome")
    if not isinstance(outcome, str) or outcome not in ALLOWED_OUTCOMES:
        raise _InvalidRow("outcome is not in the allowed set")

    stream = row.get("stream")
    if not isinstance(stream, bool):
        raise _InvalidRow("stream must be a boolean")

    usage = row.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise _InvalidRow("usage must be an object or null")
    usage = usage or {}

    bytes_out = row.get("bytes_out")
    if bytes_out is not None:
        if isinstance(bytes_out, bool) or not isinstance(bytes_out, int) or bytes_out < 0:
            raise _InvalidRow("bytes_out must be a non-negative integer or null")

    return RawEvent(
        ts=_parse_timestamp(row.get("ts")),
        key_id=key_id.strip(),
        label=_parse_optional_string(row.get("label"), "label"),
        method=_parse_optional_string(row.get("method"), "method"),
        path=_parse_optional_string(row.get("path"), "path"),
        model=_parse_optional_string(row.get("model"), "model"),
        status=status,
        outcome=outcome,
        stream=stream,
        input_tokens=_parse_token_count(usage.get("input_tokens"), "input_tokens"),
        output_tokens=_parse_token_count(usage.get("output_tokens"), "output_tokens"),
        bytes_out=bytes_out,
        queue_ms=_parse_duration(row.get("queue_ms"), "queue_ms"),
        ttft_ms=_parse_duration(row.get("ttft_ms"), "ttft_ms"),
        upstream_ms=_parse_duration(row.get("upstream_ms"), "upstream_ms"),
        total_ms=_parse_duration(row.get("total_ms"), "total_ms"),
        request_id=_parse_request_id(row.get("request_id")),
    )


def ingest_jsonl(text: str, limits: IngestLimits = IngestLimits()) -> IngestResult:
    """把原始 JSONL 文本解析成观测序列。超限抛 InputLimitExceeded。"""
    if not isinstance(text, str):
        raise InvalidRequest("input must be a string")
    if len(text.encode("utf-8")) > limits.max_bytes:
        raise InputLimitExceeded(
            f"input exceeds max_bytes={limits.max_bytes}"
        )

    events: list[RawEvent] = []
    total_lines = 0
    malformed = 0
    content_rejected = 0

    for raw in text.splitlines():
        if not raw.strip():
            continue
        total_lines += 1
        if total_lines > limits.max_lines:
            raise InputLimitExceeded(f"input exceeds max_lines={limits.max_lines}")
        if len(raw.encode("utf-8")) > limits.max_line_bytes:
            malformed += 1
            continue
        try:
            row = json.loads(raw, parse_constant=_reject_json_constant)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(row, dict):
            malformed += 1
            continue
        if _has_content_field(row):
            content_rejected += 1
            continue
        try:
            events.append(_build_event(row))
        except _InvalidRow:
            malformed += 1

    return IngestResult(
        events=tuple(events),
        total_lines=total_lines,
        malformed_lines=malformed,
        content_field_rejected=content_rejected,
    )
