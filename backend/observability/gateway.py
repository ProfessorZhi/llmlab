"""Gateway 元数据行的解析、归属与按人聚合。

输入是 Gateway 现有 ``usage.jsonl`` 形状的**元数据**行：

``ts,key_id,request_id,label,method,path,model,endpoint,status,outcome,stream,usage,bytes_out,queue_ms,ttft_ms,upstream_ms,total_ms``

**这里不做授权，只做三件事**：一行是否可被信任为一条观测、它归属谁、按归属聚合。
归属**唯一**来自 :class:`~backend.observability.identity.KeyDirectory`。

正文类字段（``prompt`` / ``completion`` / ``messages`` …）一律拒绝该行；而
**自报身份类字段**（``uid`` / ``principal`` / ``project`` / ``x_project`` /
``X-Forwarded-User`` …）被**静默忽略** —— 既不采信也不报错。静默是有意的：报错等于
告诉上游「你写这个字段我们看见了」，而它本来就该被当作不存在。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .dto import (
    QUALITY_MEASURED,
    QUALITY_PARTIAL,
    QUALITY_UNKNOWN,
    REASON_NO_SAMPLE,
    MetricReading,
)
from .errors import InputLimitExceeded, InvalidRequest
from .percentiles import percentile, percentile_block

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

#: 客户端可自报的**身份 / 归属**类字段名。
#:
#: 这些字段被读取后**直接丢弃**。它们既不参与归属，也不参与分组；列在这里是为了让
#: 「我们见过这个字段并且决定不用它」成为一条可审计的代码事实，而不是靠「没写那行」。
#:
#: 名字一律写成**归一化**形式（小写、连字符换下划线）—— 因为 HTTP 头习惯写连字符
#: （``X-Forwarded-User``）而行内的键习惯写下划线。归一化函数见
#: :func:`normalise_field_name`；少了这一步，一个连字符写法就会从诊断里溜过去
#: （它仍然**不会**被采信，只是「我们见过」这句话会变成假话）。
SELF_REPORTED_IDENTITY_FIELDS = frozenset(
    {
        "uid",
        "user",
        "username",
        "principal",
        "subject",
        "owner",
        "project",
        "project_id",
        "x_project",
        "x_principal",
        "x_forwarded_user",
        "x_user",
        "x_auth_request_user",
        "x_auth_request_email",
        "endpoint_owner",
    }
)


def normalise_field_name(name: object) -> str:
    """把字段名归一化成可比较的形式：小写、连字符与下划线等价。

    ``"X-Forwarded-User"`` 与 ``"x_forwarded_user"`` 归一化后相同 —— 二者指的是
    同一个客户端自报的身份。
    """
    return str(name).strip().lower().replace("-", "_")

#: ``request_id`` 的最大长度（字符）。理由同 atlas：挡住「把正文塞进 request_id」。
MAX_REQUEST_ID_LEN = 128

_CONTROL_CHARS = frozenset(
    chr(c) for c in list(range(0x20)) + [0x7F] + list(range(0x80, 0xA0))
)

_DURATION_FIELDS = ("queue_ms", "ttft_ms", "upstream_ms", "total_ms")
_STRING_FIELDS = ("label", "method", "path", "model", "endpoint")


class _InvalidRow(Exception):
    """内部信号：这一行不可信，计入 malformed。"""


@dataclass(frozen=True)
class IngestLimits:
    """滥用上限。默认值即产品默认，测试可覆写。"""

    max_bytes: int = 32 * 1024 * 1024
    max_lines: int = 200_000
    max_line_bytes: int = 64 * 1024


@dataclass(frozen=True)
class RawGatewayEvent:
    """一条通过校验的 Gateway 元数据观测。**不含任何自报身份**。"""

    ts: datetime
    key_id: str
    label: str | None
    method: str | None
    path: str | None
    model: str | None
    endpoint: str | None
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
    request_id: str | None = None


@dataclass(frozen=True)
class IngestResult:
    events: tuple[RawGatewayEvent, ...]
    total_lines: int
    malformed_lines: int
    content_field_rejected: int
    #: 见过但**丢弃**的自报身份字段名（去重排序）。仅用于诊断展示，不参与判定。
    self_reported_fields_seen: tuple[str, ...] = ()


def _reject_json_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant not allowed: {name}")


#: 正文类字段检测的最大嵌套深度。超过即视为不可信（**不是**「没找到」）。
MAX_CONTENT_SCAN_DEPTH = 6


def _has_content_field(row: object, depth: int = 0) -> bool:
    """这一行（含嵌套对象）里是否出现正文类字段名。

    **必须递归。** 只查顶层的话，``{"usage": {"prompt": "…"}}`` 这种嵌一层的写法会
    被判成干净的行 —— 正文没进 ``RawGatewayEvent``（那里只读已知的标量字段），但
    「正文类字段 → 拒绝整行」这条不变量与它的计数会被悄悄绕过，而那个计数正是运维
    用来确认「上游有没有往元数据里塞正文」的依据。

    深度超过 :data:`MAX_CONTENT_SCAN_DEPTH` 时**返回真**：一个深到看不清形状的结构
    不该被当作「我检查过了，是干净的」。
    """
    if depth > MAX_CONTENT_SCAN_DEPTH:
        return True
    if isinstance(row, dict):
        for key, value in row.items():
            if str(key).lower() in CONTENT_FIELD_NAMES:
                return True
            if isinstance(value, (dict, list, tuple)) and _has_content_field(value, depth + 1):
                return True
        return False
    if isinstance(row, (list, tuple)):
        return any(_has_content_field(item, depth + 1) for item in row)
    return False


def _self_reported_fields(row: dict) -> tuple[str, ...]:
    """行里出现的自报身份字段（**归一化**后的名字，排序去重）。调用方**只**用于诊断。"""
    return tuple(
        sorted(
            {
                normalise_field_name(key)
                for key in row
                if normalise_field_name(key) in SELF_REPORTED_IDENTITY_FIELDS
            }
        )
    )


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
    text = value.strip()
    return text or None


def _parse_request_id(value: Any) -> str | None:
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


def _build_event(row: dict) -> RawGatewayEvent:
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

    return RawGatewayEvent(
        ts=_parse_timestamp(row.get("ts")),
        key_id=key_id.strip(),
        label=_parse_optional_string(row.get("label"), "label"),
        method=_parse_optional_string(row.get("method"), "method"),
        path=_parse_optional_string(row.get("path"), "path"),
        model=_parse_optional_string(row.get("model"), "model"),
        endpoint=_parse_optional_string(row.get("endpoint"), "endpoint"),
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
    """把原始 JSONL 文本解析成观测序列。超限抛 :class:`InputLimitExceeded`。"""
    if not isinstance(text, str):
        raise InvalidRequest("input must be a string")
    if len(text.encode("utf-8")) > limits.max_bytes:
        raise InputLimitExceeded(f"input exceeds max_bytes={limits.max_bytes}")

    events: list[RawGatewayEvent] = []
    total_lines = 0
    malformed = 0
    content_rejected = 0
    self_reported: set[str] = set()

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
        # 自报身份字段：**先记录「见过」再丢弃**，绝不进入 _build_event。
        seen = _self_reported_fields(row)
        if seen:
            self_reported.update(seen)
        try:
            events.append(_build_event(row))
        except _InvalidRow:
            malformed += 1

    return IngestResult(
        events=tuple(events),
        total_lines=total_lines,
        malformed_lines=malformed,
        content_field_rejected=content_rejected,
        self_reported_fields_seen=tuple(sorted(self_reported)),
    )


# ---------------------------------------------------------------------------
# 按人聚合
# ---------------------------------------------------------------------------


def event_endpoint(event: RawGatewayEvent) -> str | None:
    """一条观测指向哪个端点。

    ``endpoint`` 优先，退回 ``model``（现有 Gateway 只写 ``model``）。两个都没有就
    返回 ``None`` —— 上层计入 ``endpoint_missing``，**不**给它编一个 ``"unknown"``
    分组，否则那个分组会变成所有缺失值的垃圾桶，且名字看起来像一个真实端点。
    """
    if event.endpoint:
        return event.endpoint
    if event.model:
        return event.model
    return None


@dataclass
class UsageAccumulator:
    """一组请求的计数 + 原始样本。样本存原始值，聚合时才算分位数。"""

    calls: int = 0
    success: int = 0
    failed: int = 0
    cancelled: int = 0
    throttled_429: int = 0
    outcome_status_mismatch: int = 0
    latency_samples: list[float] = field(default_factory=list)
    latency_missing: int = 0
    queue_samples: list[float] = field(default_factory=list)
    queue_missing: int = 0
    ttft_samples: list[float] = field(default_factory=list)
    ttft_missing: int = 0
    ttft_excluded_non_stream: int = 0
    input_tokens_total: int = 0
    input_tokens_seen: int = 0
    input_tokens_missing: int = 0
    output_tokens_total: int = 0
    output_tokens_seen: int = 0
    output_tokens_missing: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    def add(self, event: RawGatewayEvent) -> None:
        self.calls += 1
        if self.first_ts is None or event.ts < self.first_ts:
            self.first_ts = event.ts
        if self.last_ts is None or event.ts > self.last_ts:
            self.last_ts = event.ts

        http_ok = 200 <= event.status <= 299
        outcome_ok = event.outcome == "success"
        if http_ok != outcome_ok:
            self.outcome_status_mismatch += 1

        if event.status == 429:
            self.throttled_429 += 1

        # 排队时长对**每一次**调用都有意义（含失败），所以先收。
        if event.queue_ms is not None:
            self.queue_samples.append(event.queue_ms)
        else:
            self.queue_missing += 1

        if not (http_ok and outcome_ok):
            if event.status != 429:
                if event.outcome == "cancelled":
                    self.cancelled += 1
                else:
                    self.failed += 1
            return

        self.success += 1
        if event.total_ms is not None and event.total_ms > 0:
            self.latency_samples.append(event.total_ms)
        else:
            self.latency_missing += 1

        if event.stream:
            if event.ttft_ms is not None and event.ttft_ms > 0:
                self.ttft_samples.append(event.ttft_ms)
            else:
                self.ttft_missing += 1
        else:
            self.ttft_excluded_non_stream += 1

        self._add_tokens(event)

    def _add_tokens(self, event: RawGatewayEvent) -> None:
        if event.input_tokens is None:
            self.input_tokens_missing += 1
        else:
            self.input_tokens_seen += 1
            self.input_tokens_total += event.input_tokens
        if event.output_tokens is None:
            self.output_tokens_missing += 1
        else:
            self.output_tokens_seen += 1
            self.output_tokens_total += event.output_tokens

    # -- 质量 --------------------------------------------------------------

    def quality(self) -> str:
        """本组数据的质量。**这是全模块关于 `quality.calls` 的唯一实现。**

        （早先这里有一份 `observed_lines` 版本、`service.py` 另有一份按 `calls` 判定的
        副本，两份都没人调用其中一份 —— 契约规则有两个实现就迟早会漂移，
        所以现在只留这一个，由 `service._calls_quality` 委派过来。）

        ``calls == 0`` -> ``unknown``：**空日志与没有接线在数据上无法区分**，
        所以「0 次请求」不是一个结论。这与「确实数了、结果是零」是两件事 ——
        后者用整数 ``0`` 表达（例如 ``unknown.revoked_key == 0``）。
        """
        if self.calls == 0:
            return QUALITY_UNKNOWN
        if (
            self.latency_missing
            or self.queue_missing
            or self.ttft_missing
            or self.outcome_status_mismatch
        ):
            return QUALITY_PARTIAL
        return QUALITY_MEASURED

    def latency_block(self) -> dict:
        return percentile_block(self.latency_samples, missing=self.latency_missing)

    def queue_block(self) -> dict:
        return percentile_block(self.queue_samples, missing=self.queue_missing)

    def ttft_block(self) -> dict:
        block = percentile_block(self.ttft_samples, missing=self.ttft_missing)
        block["excluded_non_stream"] = self.ttft_excluded_non_stream
        return block

    def token_block(self) -> dict:
        return {
            "input_total": self.input_tokens_total if self.input_tokens_seen else None,
            "output_total": self.output_tokens_total if self.output_tokens_seen else None,
            "input_missing": self.input_tokens_missing,
            "output_missing": self.output_tokens_missing,
        }

    def counter_block(self) -> dict:
        return {
            "calls": self.calls,
            "success": self.success,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "throttled_429": self.throttled_429,
        }

    # -- 读数 --------------------------------------------------------------

    def readings(
        self,
        *,
        source: str,
        now: datetime,
        quality: str,
    ) -> dict:
        """把累加器折成带四件套信封的读数表（``metric_id -> reading``）。

        ``observed_at`` 取**最后一条事件的时间**，而不是 ``now``：读数的观测时刻是
        数据本身的时刻。没有事件时 ``observed_at`` 为 ``None`` 且质量是 ``unknown``。

        **三个地方刻意不产出数字：**

        * 整组质量未知（窗口内一条都没观察到）-> 全部读数 unknown；
        * 没有 token 样本 -> token 总量 unknown（**不是 0**），因为「没记 token」
          与「token 是 0」在数据上不同；
        * 没有延迟/排队样本 -> 对应分位数 unknown（**不是 0 ms**）。
        """
        observed = self.last_ts
        unknown = quality == QUALITY_UNKNOWN

        def absent(metric_id: str, reason: str) -> dict:
            return MetricReading.unknown(
                metric_id, source=source, reason=reason, now=now
            ).to_dict()

        def numeric(metric_id: str, value) -> dict:
            if unknown:
                return absent(metric_id, REASON_NO_SAMPLE)
            return MetricReading.from_metric(
                metric_id,
                value=value,
                source=source,
                quality=quality,
                observed_at=observed,
                now=now,
            ).to_dict()

        def percentile_reading(metric_id: str, samples: list[float], p: float) -> dict:
            if unknown or not samples:
                return absent(metric_id, REASON_NO_SAMPLE)
            return MetricReading.from_metric(
                metric_id,
                value=percentile(sorted(samples), p),
                source=source,
                quality=quality,
                observed_at=observed,
                now=now,
            ).to_dict()

        def token_reading(metric_id: str, total: int, seen: int) -> dict:
            if unknown or seen == 0:
                # 一条 token 样本都没有 —— 无从测得，不是 0。
                return absent(metric_id, REASON_NO_SAMPLE)
            return MetricReading.from_metric(
                metric_id,
                value=total,
                source=source,
                quality=quality,
                observed_at=observed,
                now=now,
            ).to_dict()

        return {
            "principal.requests": numeric("principal.requests", self.calls),
            # 「失败」= 未成功（含被限流的 429 与主动取消）。
            # 不用 failed+cancelled：那会把 429 漏掉，让限流在错误率里隐形。
            "principal.errors": numeric(
                "principal.errors", self.calls - self.success
            ),
            "principal.throttled_429": numeric(
                "principal.throttled_429", self.throttled_429
            ),
            "principal.tokens.input": token_reading(
                "principal.tokens.input", self.input_tokens_total, self.input_tokens_seen
            ),
            "principal.tokens.output": token_reading(
                "principal.tokens.output", self.output_tokens_total, self.output_tokens_seen
            ),
            "principal.queue_p50_ms": percentile_reading(
                "principal.queue_p50_ms", self.queue_samples, 0.5
            ),
            "principal.queue_p95_ms": percentile_reading(
                "principal.queue_p95_ms", self.queue_samples, 0.95
            ),
            "principal.latency_p50_ms": percentile_reading(
                "principal.latency_p50_ms", self.latency_samples, 0.5
            ),
            "principal.latency_p95_ms": percentile_reading(
                "principal.latency_p95_ms", self.latency_samples, 0.95
            ),
        }


__all__ = [
    "ALLOWED_OUTCOMES",
    "CONTENT_FIELD_NAMES",
    "IngestLimits",
    "IngestResult",
    "MAX_REQUEST_ID_LEN",
    "RawGatewayEvent",
    "SELF_REPORTED_IDENTITY_FIELDS",
    "UsageAccumulator",
    "event_endpoint",
    "ingest_jsonl",
    "normalise_field_name",
]
