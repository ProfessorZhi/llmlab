"""网关管理面：密钥、模型路由、用量概览、调用日志、在线体验。

身份一律来自 BFF 解析好的会话主体（``bff.identity.Principal``）；请求体里不会出现
「替谁」。可见范围：

* 自己：自己的密钥、自己的调用；
* 服务所有者：自己发布的路由被谁调用了多少（只有次数、token、耗时，没有正文）；
* 管理员（``view=all``）：全部。

日志以**密钥**为单位：每个人看自己每把密钥的调用。默认只记元数据；密钥所有者可以为
某一把密钥打开「记录请求内容」，之后这把密钥的请求 / 响应正文（各截断到 64 KiB）保留
7 天，只有调用者本人能在单条请求详情里看到；管理员只看元数据。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Callable

from . import store as gw_store
from .proxy import CHANNEL_PLAYGROUND, Caller, GatewayCore, GatewayError
from ..registry.presence import service_state

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
WINDOWS = {"24h": 24 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}
PLAYGROUND_MAX_TOKENS = 1024
PLAYGROUND_RPM = 20


class ManagementError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentile(values: list[float], p: float) -> float | None:
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    idx = (len(xs) - 1) * p
    lo, hi = int(idx), min(int(idx) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (idx - lo)


class GatewayService:
    def __init__(self, core: GatewayCore, *, public_base: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.core = core
        self.store = core.store
        self._clock = clock
        self._public_base = public_base

    # --- helpers -------------------------------------------------------------

    def _name(self, principal: str) -> str:
        record = self.core.registry.principals.lookup(principal)
        if record is None:
            return principal
        return record.display_name or record.linux_user or principal

    @staticmethod
    def is_admin(principal) -> bool:
        return bool(getattr(principal, "is_platform_admin", False))

    def _view_all(self, principal, view: str | None) -> bool:
        if view == "all":
            if not self.is_admin(principal):
                raise ManagementError(403, "gateway.forbidden", "the lab-wide view needs an administrator")
            return True
        return False

    # --- keys ----------------------------------------------------------------

    def key_dict(self, record: gw_store.KeyRecord) -> dict:
        now = self._clock()
        return {
            "key_id": record.id,
            "prefix": record.prefix,
            "label": record.label,
            "owner": record.principal,
            "owner_name": self._name(record.principal),
            "models": list(record.models) if record.models is not None else None,
            "rpm": record.rpm,
            "concurrency": record.concurrency,
            "created_at": _iso(record.created_at),
            "expires_at": _iso(record.expires_at),
            "last_used_at": _iso(record.last_used_at),
            "state": "revoked" if record.revoked_at else ("expired" if not record.live(now) else "active"),
            "log_bodies": record.log_bodies,
        }

    def list_keys(self, principal, *, view: str | None = None) -> dict:
        everyone = self._view_all(principal, view)
        records = self.store.keys_for(None if everyone else principal.principal)
        now = self._clock()
        rows = self.store.usage_rows(since=now - WINDOWS["7d"], principal=None if everyone else principal.principal)
        stats: dict[str, dict] = {}
        for r in rows:
            if not r["key_id"]:
                continue
            st = stats.setdefault(r["key_id"], {"requests_7d": 0, "requests_24h": 0, "tokens_7d": 0, "errors_7d": 0})
            st["requests_7d"] += 1
            st["requests_24h"] += 1 if r["ts"] >= now - WINDOWS["24h"] else 0
            st["tokens_7d"] += (r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
            st["errors_7d"] += 0 if r["outcome"] == "success" else 1
        empty = {"requests_7d": 0, "requests_24h": 0, "tokens_7d": 0, "errors_7d": 0}
        return {"keys": [{**self.key_dict(r), "usage": stats.get(r.id, empty)} for r in records],
                "view": "all" if everyone else "mine"}

    def create_key(self, principal, body: dict) -> dict:
        label = body.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 64):
            raise ManagementError(400, "gateway.invalid_request", "label must be a string of at most 64 characters")
        models = body.get("models")
        if models is not None:
            if not isinstance(models, list) or not all(isinstance(m, str) and ALIAS_RE.match(m) for m in models):
                raise ManagementError(400, "gateway.invalid_request", "models must be a list of model names")
            models = tuple(dict.fromkeys(models)) or None
        rpm = _bounded_int(body.get("rpm"), gw_store.DEFAULT_RPM, 1, gw_store.MAX_RPM, "rpm")
        concurrency = _bounded_int(body.get("concurrency"), gw_store.DEFAULT_CONCURRENCY, 1,
                                   gw_store.MAX_CONCURRENCY, "concurrency")
        log_bodies = body.get("log_bodies", False)
        if not isinstance(log_bodies, bool):
            raise ManagementError(400, "gateway.invalid_request", "log_bodies must be true or false")
        ttl_days = body.get("ttl_days")
        expires_at = None
        if ttl_days is not None:
            days = _bounded_int(ttl_days, 30, 1, 3650, "ttl_days")
            expires_at = self._clock() + days * 86400
        if self.store.count_live_keys(principal.principal) >= gw_store.MAX_KEYS_PER_PRINCIPAL:
            raise ManagementError(429, "gateway.too_many_keys", "revoke an unused key before creating another")
        record, plaintext = self.store.create_key(
            principal=principal.principal, label=(label or "").strip() or None, models=models,
            rpm=rpm, concurrency=concurrency, expires_at=expires_at, log_bodies=log_bodies,
        )
        out = self.key_dict(record)
        out["key"] = plaintext  # 只在这一次返回
        return {"key": out}

    def update_key(self, principal, key_id: str, body: dict) -> dict:
        """改用途、开关「记录请求内容」。只有密钥所有者本人能改（管理员也不能替别人打开正文记录）。"""
        record = self.store.key_by_id(key_id)
        if record is None or record.principal != principal.principal:
            raise ManagementError(404, "gateway.not_found", "no such key")
        label = body.get("label")
        if label is not None and (not isinstance(label, str) or len(label) > 64):
            raise ManagementError(400, "gateway.invalid_request", "label must be a string of at most 64 characters")
        log_bodies = body.get("log_bodies")
        if log_bodies is not None and not isinstance(log_bodies, bool):
            raise ManagementError(400, "gateway.invalid_request", "log_bodies must be true or false")
        self.store.update_key(key_id, label=label.strip() if isinstance(label, str) else None, log_bodies=log_bodies)
        return {"key": self.key_dict(self.store.key_by_id(key_id))}

    def revoke_key(self, principal, key_id: str) -> dict:
        record = self.store.key_by_id(key_id)
        if record is None or (record.principal != principal.principal and (self.core.personal_mode or not self.is_admin(principal))):
            raise ManagementError(404, "gateway.not_found", "no such key")
        self.store.revoke_key(key_id)
        return {"key": self.key_dict(self.store.key_by_id(key_id))}

    # --- routes --------------------------------------------------------------

    def route_dict(self, route: gw_store.Route, principal) -> dict:
        engine = self.core.engine(route.engine_id)
        return {
            "route_id": route.id,
            "model": route.alias,
            "engine_id": route.engine_id,
            "engine_name": engine.name if engine else None,
            "engine_status": engine.status if engine else "missing",
            "engine_state": service_state(engine) if engine else "offline",
            "framework": _framework(engine) if engine else None,
            "upstream_model": route.upstream_model,
            "owner": route.owner,
            "owner_name": self._name(route.owner),
            "owner_is_self": route.owner == principal.principal,
            "visibility": route.visibility,
            "enabled": route.enabled,
            "upstream_credential": bool(engine and self.core.upstream_key(engine)),
            "created_at": _iso(route.created_at),
        }

    def list_routes(self, principal) -> dict:
        admin = self.is_admin(principal)
        rows = [
            self.route_dict(r, principal)
            for r in self.store.routes()
            if admin or r.owner == principal.principal or (r.visibility == gw_store.VISIBILITY_ALL and r.enabled)
        ]
        if self.core.personal_mode:
            rows = [row for row in rows if row["owner_is_self"]]
        return {"routes": rows, "base_url": self._base_url(), "sharing_enabled": not self.core.personal_mode}

    def _owned_engine(self, principal, engine_id: object):
        if not isinstance(engine_id, str) or not engine_id:
            raise ManagementError(400, "gateway.invalid_request", "engine_id is required")
        engine = self.core.engine(engine_id)
        if engine is None:
            raise ManagementError(404, "gateway.not_found", "no such inference service")
        if engine.owner_principal != principal.principal and (self.core.personal_mode or not self.is_admin(principal)):
            raise ManagementError(403, "gateway.forbidden", "only the service owner or an administrator can publish it")
        return engine

    def publish_route(self, principal, body: dict) -> dict:
        engine = self._owned_engine(principal, body.get("engine_id"))
        alias = body.get("model") or engine.name
        if not isinstance(alias, str) or not ALIAS_RE.match(alias):
            raise ManagementError(400, "gateway.invalid_request", "model name may use letters, digits and . _ : / -")
        visibility = body.get("visibility", gw_store.VISIBILITY_PRIVATE if self.core.personal_mode else gw_store.VISIBILITY_ALL)
        if self.core.personal_mode and visibility != gw_store.VISIBILITY_PRIVATE:
            raise ManagementError(400, "gateway.sharing_disabled", "service sharing is not enabled")
        if visibility not in gw_store.VISIBILITIES:
            raise ManagementError(400, "gateway.invalid_request", "visibility must be 'all' or 'private'")
        upstream = body.get("upstream_model")
        if not upstream:
            candidates = (engine.launch or {}).get("model_ids") or []
            if len(candidates) == 1:
                upstream = candidates[0]
            elif self.core.personal_mode:
                raise ManagementError(400, "gateway.upstream_model_required", "请填写引擎 /v1/models 返回的上游模型名。")
        if upstream is not None and (not isinstance(upstream, str) or len(upstream) > 200):
            raise ManagementError(400, "gateway.invalid_request", "upstream_model must be a short string")
        try:
            route = self.store.create_route(
                alias=alias, engine_id=engine.id, upstream_model=(upstream or None), owner=engine.owner_principal,
                visibility=visibility, created_by=principal.principal,
            )
        except ValueError as exc:
            raise ManagementError(409, "gateway.conflict", str(exc)) from None
        return {"route": self.route_dict(route, principal)}

    def update_route(self, principal, route_id: str, body: dict) -> dict:
        route = self.store.route_by_id(route_id)
        if route is None or (route.owner != principal.principal and (self.core.personal_mode or not self.is_admin(principal))):
            raise ManagementError(404, "gateway.not_found", "no such model route")
        if body.get("delete") is True:
            self.store.delete_route(route_id)
            return {"deleted": route_id}
        visibility = body.get("visibility")
        if self.core.personal_mode and visibility not in (None, gw_store.VISIBILITY_PRIVATE):
            raise ManagementError(400, "gateway.sharing_disabled", "service sharing is not enabled")
        if visibility is not None and visibility not in gw_store.VISIBILITIES:
            raise ManagementError(400, "gateway.invalid_request", "visibility must be 'all' or 'private'")
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ManagementError(400, "gateway.invalid_request", "enabled must be true or false")
        upstream = body.get("upstream_model")
        if upstream is not None and (not isinstance(upstream, str) or len(upstream) > 200):
            raise ManagementError(400, "gateway.invalid_request", "upstream_model must be a short string")
        self.store.update_route(route_id, visibility=visibility, enabled=enabled, upstream_model=upstream)
        return {"route": self.route_dict(self.store.route_by_id(route_id), principal)}

    # --- usage ---------------------------------------------------------------

    def overview(self, principal, *, window: str | None = None, view: str | None = None) -> dict:
        everyone = self._view_all(principal, view)
        span = WINDOWS.get(window or "24h", WINDOWS["24h"])
        now = self._clock()
        since = now - span
        rows = self.store.usage_rows(since=since, principal=None if everyone else principal.principal)
        mine = _aggregate(rows, now, span, self._name)
        served = None
        if not everyone and not self.core.personal_mode:
            owned = self.store.usage_rows(since=since, route_owner=principal.principal)
            if owned or any(r.owner == principal.principal for r in self.store.routes()):
                served = _aggregate(owned, now, span, self._name)
        keys = self.store.keys_for(None if everyone else principal.principal)
        labels = {k.id: (k.label or k.prefix + "…") for k in self.store.keys_for(None)}
        for item in mine["by_key"]:
            item["label"] = "在线体验" if item["channel"] == CHANNEL_PLAYGROUND else labels.get(item["key_id"], "已删除的密钥")
            item["caller_name"] = self._name(item["caller"])
        return {
            "window": window if window in WINDOWS else "24h",
            "view": "all" if everyone else "mine",
            "calls": mine,
            "my_services": served,
            "keys_active": sum(1 for k in keys if k.live(now)),
            "routes": len(self.store.routes()),
            "inflight": self.core.limiter.inflight(),
            "base_url": self._base_url(),
        }

    def _log_row(self, r, keys: dict) -> dict:
        key = keys.get(r["key_id"])
        return {
            "request_id": r["request_id"], "ts": _iso(r["ts"]), "model": r["model"],
            "key_id": r["key_id"], "key_label": key.label if key else None, "key_prefix": key.prefix if key else None,
            "channel": r["channel"], "caller": r["principal"], "caller_name": self._name(r["principal"]),
            "status": r["status"], "outcome": r["outcome"], "stream": bool(r["stream"]),
            "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
            "ttft_ms": r["ttft_ms"], "total_ms": r["total_ms"], "error": r["error"], "has_body": bool(r["has_body"]),
        }

    def logs(self, principal, *, view: str | None = None, model: str | None = None,
             outcome: str | None = None, key_id: str | None = None, limit: object = None) -> dict:
        everyone = self._view_all(principal, view)
        n = _bounded_int(limit, 100, 1, 500, "limit")
        rows = self.store.usage_rows(
            since=self._clock() - WINDOWS["30d"], principal=None if everyone else principal.principal,
            model=model or None, outcome=outcome or None, key_id=key_id or None, limit=n,
        )
        keys = {k.id: k for k in self.store.keys_for(None if everyone else principal.principal)}
        return {"view": "all" if everyone else "mine", "rows": [self._log_row(r, keys) for r in rows]}

    def log_detail(self, principal, request_id: str) -> dict:
        """单条请求的全量记录。自己发起的，或管理员；正文只在那把密钥打开了记录时才有。"""
        row = self.store.usage_by_request(request_id)
        if row is None or (row["principal"] != principal.principal and not self.is_admin(principal)):
            raise ManagementError(404, "gateway.not_found", "no such request")
        keys = {k.id: k for k in self.store.keys_for(row["principal"])}
        out = self._log_row(row, keys)
        route = self.store.route_by_id(row["route_id"]) if row["route_id"] else None
        engine = self.core.engine(route.engine_id) if route else None
        out.update({
            "path": row["path"],
            "route_owner": row["route_owner"],
            "route_owner_name": self._name(row["route_owner"]) if row["route_owner"] else None,
            "engine_name": engine.name if engine else None,
            "upstream_model": route.upstream_model if route else None,
            "body": None,
        })
        body = self.store.body_for(request_id) if row["has_body"] and row["principal"] == principal.principal else None
        out["body_access"] = "owner" if row["principal"] == principal.principal else "metadata_only"
        if body is not None:
            out["body"] = {
                "request": _maybe_json(body["request"]),
                "response": _maybe_json(body["response"]) if not row["stream"] else body["response"],
                "response_text": _sse_text(body["response"]) if row["stream"] else None,
                "truncated": bool(body["truncated"]),
            }
        return out

    # --- playground ----------------------------------------------------------

    def playground(self, principal, body: dict) -> dict:
        """在线体验：以会话身份经网关调一次（非流式、有上限），照常记账。"""
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 50:
            raise ManagementError(400, "gateway.invalid_request", "messages must be a non-empty list")
        for item in messages:
            if not isinstance(item, dict) or item.get("role") not in ("system", "user", "assistant"):
                raise ManagementError(400, "gateway.invalid_request", "each message needs a role and content")
        max_tokens = _bounded_int(body.get("max_tokens"), 512, 1, PLAYGROUND_MAX_TOKENS, "max_tokens")
        payload = {"model": body.get("model"), "messages": messages, "max_tokens": max_tokens, "stream": False}
        temperature = body.get("temperature")
        if isinstance(temperature, (int, float)) and 0 <= temperature <= 2:
            payload["temperature"] = temperature
        caller = Caller(principal=principal.principal, key_id=None, channel=CHANNEL_PLAYGROUND,
                        rpm=PLAYGROUND_RPM, concurrency=1)
        try:
            status, _headers, chunks = self.core.forward(caller, "/v1/chat/completions", payload)
        except GatewayError as exc:
            raise ManagementError(exc.status, f"gateway.{exc.code}", exc.message) from None
        raw = b"".join(chunks)
        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ManagementError(502, "gateway.bad_upstream", "the inference service answered with something that is not JSON") from None
        if status >= 400:
            message = (answer.get("error") or {}).get("message") if isinstance(answer, dict) else None
            raise ManagementError(status, "gateway.upstream_error", message or f"the inference service answered {status}")
        choice = (answer.get("choices") or [{}])[0]
        return {
            "model": payload["model"],
            "message": choice.get("message"),
            "finish_reason": choice.get("finish_reason"),
            "usage": answer.get("usage"),
        }

    def _base_url(self) -> str | None:
        return self._public_base() if self._public_base else None


def _maybe_json(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _sse_text(raw: str | None) -> str | None:
    """把流式响应的分片拼回正文，方便在详情里阅读（原始分片也一并返回）。"""
    if not raw:
        return None
    parts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        try:
            chunk = json.loads(line[5:].strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                parts.append(delta["content"])
            elif isinstance(choice.get("text"), str):
                parts.append(choice["text"])
    return "".join(parts)


def _framework(engine) -> str | None:
    for tag in engine.tags or ():
        if tag.startswith("framework:"):
            return tag.split(":", 1)[1]
    return None


def _bounded_int(value: object, default: int, low: int, high: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ManagementError(400, "gateway.invalid_request", f"{name} must be an integer") from None
    if not (low <= number <= high):
        raise ManagementError(400, "gateway.invalid_request", f"{name} must be between {low} and {high}")
    return number


def _aggregate(rows, now: float, span: float, name: Callable[[str], str]) -> dict:
    total = len(rows)
    ok = sum(1 for r in rows if r["outcome"] == "success")
    prompt_values = [r["prompt_tokens"] for r in rows if r["prompt_tokens"] is not None]
    completion_values = [r["completion_tokens"] for r in rows if r["completion_tokens"] is not None]
    prompt = sum(prompt_values) if prompt_values or not rows else None
    completion = sum(completion_values) if completion_values or not rows else None
    lat = [r["total_ms"] for r in rows if r["outcome"] == "success"]
    ttft = [r["ttft_ms"] for r in rows if r["outcome"] == "success" and r["stream"]]
    buckets = 24 if span <= 86400 else (7 if span <= 7 * 86400 else 30)
    step = span / buckets
    series = [0] * buckets
    for r in rows:
        idx = int((r["ts"] - (now - span)) // step)
        if 0 <= idx < buckets:
            series[idx] += 1
    by_model: dict[str, dict] = {}
    by_caller: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    outcomes = {"success": 0, "failed": 0, "cancelled": 0}
    # 星期 × 小时（服务器本地时间，周一为第 0 行），给热力图用。
    heat = [[0] * 24 for _ in range(7)]
    pairs: dict[tuple[str, str], int] = {}
    for r in rows:
        local = time.localtime(r["ts"])
        heat[local.tm_wday][local.tm_hour] += 1
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        kid = r["key_id"] or f"{r['channel']}:{r['principal']}"
        k = by_key.setdefault(kid, {"key_id": r["key_id"], "channel": r["channel"], "caller": r["principal"],
                                    "requests": 0, "tokens": 0, "errors": 0})
        k["requests"] += 1
        k["tokens"] += (r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
        k["errors"] += 0 if r["outcome"] == "success" else 1
        pair = (r["principal"], r["model"] or "—")
        pairs[pair] = pairs.get(pair, 0) + 1
        m = by_model.setdefault(r["model"] or "—", {"model": r["model"] or "—", "requests": 0, "tokens": 0, "errors": 0})
        m["requests"] += 1
        m["tokens"] += (r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
        m["errors"] += 0 if r["outcome"] == "success" else 1
        c = by_caller.setdefault(r["principal"], {"caller": r["principal"], "name": name(r["principal"]), "requests": 0, "tokens": 0})
        c["requests"] += 1
        c["tokens"] += (r["prompt_tokens"] or 0) + (r["completion_tokens"] or 0)
    return {
        "requests": total,
        "success_rate": (ok / total) if total else None,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "usage_reported": sum(r["prompt_tokens"] is not None and r["completion_tokens"] is not None for r in rows),
        "latency_p50_ms": _percentile(lat, 0.5),
        "latency_p95_ms": _percentile(lat, 0.95),
        "ttft_p50_ms": _percentile(ttft, 0.5),
        "series": {"buckets": buckets, "step_seconds": step, "requests": series},
        "by_model": sorted(by_model.values(), key=lambda x: -x["requests"]),
        "by_caller": sorted(by_caller.values(), key=lambda x: -x["requests"]),
        "by_key": sorted(by_key.values(), key=lambda x: -x["requests"]),
        "outcomes": outcomes,
        "heatmap": heat,
        "by_caller_model": [
            {"caller": c, "name": name(c), "model": m, "requests": n}
            for (c, m), n in sorted(pairs.items(), key=lambda kv: -kv[1])
        ],
    }
