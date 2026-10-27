"""把代理上报里的模型条目落成 ``models`` 行，所有者是**本人的平台账户**。

这条路径与手动登记写同一张表，但来源不同（``source="auto"``），审计时一眼可辨。
与 :mod:`observed_services` 的分工是「知道多少」：

* 观测（``/v1/models``）只知道**名字**，所以那些行没有权重路径；
* 上报知道**路径、格式、大小**——而且服务端已经 stat 过、确认文件属于上报者的
  uid（``agent_reports.AgentReportValidator._path``）。所以这些行可以带
  ``weight_path``，用户不必再手工把同一个文件登记一遍。

即便带着路径，它仍然只是**引用**：本模块不打开、不移动、不复制任何文件。

**所有者是账户，不是 linux 主体。** 模型库是账户的视图（``account:<id>`` 拥有
``models`` 行，``owner_uid = -1``）；``linux:<host>:<uid>`` 是代理通道的身份，
拿它当所有者会让「我自己的模型」在自己的库里看不见。linux 用户没有对应账户
（系统账户、还没开账户的人）时**不登记**——没有可归属的所有者，就不替谁做主。

去重规则（同一 owner 下）：

1. 同路径、可信内容指纹或 inode 匹配 → 沿用身份并刷新位置；
2. 否则新建一行 ``source=auto``。

第 1 条里的「同名」涵盖引擎自报的一行（它没有路径）：那种行是「这台机器在服务
这个模型」，这里再建一行只是同一件事说两遍。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from datetime import datetime
from hashlib import sha256
import json
from typing import Iterable

from .errors import Conflict, InvalidRequest
from .health import unknown
from .models import (
    MAX_NAME_LENGTH,
    MODEL_REGISTERED,
    SOURCE_AUTO,
    Model,
    ModelLocation,
    validate_name,
)
from .store import Store
from .weights import weight_path_fingerprint

#: 平台账户没有自己的 uid。账户拥有的行统一记这个值（与账号登录登记的行一致）。
ACCOUNT_OWNER_UID = -1

AUTO_REPORTED_MODEL_HEALTH = (
    "weight path reported by the local agent and stat'ed by the server; "
    "the file itself is never opened by the registry"
)

AUTO_REPORTED_MODEL_DESCRIPTION = "自动登记：本机代理上报的权重文件（只是引用，平台不打开它）"

#: 与 ``local_discovery.suggest_name`` 同一套清洗规则：名称会进 URL 与路由配置，
#: 不能带路径分隔符。这里对上报值**再**洗一遍，因为上报载荷是客户端给的。
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = _UNSAFE.sub("-", raw).strip("-._")
    return cleaned[:MAX_NAME_LENGTH] or None


def reported_name(path: str, suggested: object) -> str | None:
    """给一行模型起名：优先上报方建议的名字，其次文件名去扩展名。

    两边都洗不出一段合法名字时返回 ``None``——一条没有名字的模型行在库里既搜
    不到也打不开，不如不建。
    """

    for raw in (suggested, os.path.basename(path).rsplit(".", 1)[0]):
        cleaned = _sanitize(raw)
        if cleaned is None:
            continue
        try:
            return validate_name(cleaned, what="model name")
        except InvalidRequest:  # pragma: no cover - 清洗后已不可能非法
            continue
    return None


def _fingerprint(name: str, path: str) -> str:
    return sha256(
        json.dumps(
            {"kind": "model", "name": name, "source": SOURCE_AUTO, "weight_path": path},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]


@dataclass(frozen=True)
class Adoption:
    """一次自动登记的结果：路径到行的映射，以及这次**新建**了哪几行。"""

    #: ``{上报路径: 账本行 id}``，只覆盖这次见过的路径。
    adopted: dict[str, str] = field(default_factory=dict)
    #: 这次真正写入的新行 id。非空意味着「值得记一条审计」。
    created: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.adopted)


def adopt_reported_models(
    store: Store,
    *,
    owner: str,
    models: Iterable[dict],
    now: datetime,
    uid: int = -1,
    full: bool = False,
    scan_roots: tuple[str, ...] = (),
) -> Adoption:
    """把一份上报里的模型条目落成 ``models`` 行。

    单条条目出错只跳过这一条——一次上报里的一个坏名字不该让整份快照丢失。
    审计只看 :attr:`Adoption.created`：每 5 分钟一轮的上报里，**没有新行**的
    绝大多数轮次不该在审计表里留下痕迹。
    """

    adopted: dict[str, str] = {}
    created: list[str] = []
    if not owner:
        return Adoption()

    rows = [item for item in store.list_models() if item.owner_principal == owner]
    by_id = {row.id: row for row in rows}
    by_path = {item.weight_path: item.id for item in rows if item.weight_path}
    by_name = {item.name: item for item in rows}
    by_fingerprint = {row.facts.get("discovery_fingerprint"): row.id for row in rows
                      if row.facts.get("discovery_fingerprint")}
    by_inode = {}
    for row in rows:
        for loc in store.locations_for(row.id):
            by_path[loc.path] = row.id
            if loc.uid == uid and loc.device is not None and loc.inode is not None:
                by_inode[(loc.device, loc.inode)] = row.id
    entries = [e for e in models if isinstance(e, dict)]
    seen_paths = {e.get("path") for e in entries if isinstance(e.get("path"), str)}
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or path in adopted:
            continue
        name = reported_name(path, entry.get("suggested_name"))
        if name is None:
            continue
        fingerprint = entry.get("fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9]{1,20}:[a-f0-9]{64}", fingerprint):
            fingerprint = None
        found = by_path.get(path)
        if found is None and fingerprint:
            found = by_fingerprint.get(fingerprint)
        if found is None and entry.get("device") is not None and entry.get("inode") is not None:
            candidate = by_inode.get((entry["device"], entry["inode"]))
            # Reused inode with different content is a replacement, not a move.
            if candidate and (not fingerprint or not by_id[candidate].facts.get("discovery_fingerprint")
                              or by_id[candidate].facts["discovery_fingerprint"] == fingerprint):
                found = candidate
        same_name = by_name.get(name)
        if found is None and same_name is not None and not same_name.weight_path:
            found = same_name.id
        facts = dict(entry.get("facts") or {})
        if fingerprint:
            facts["discovery_fingerprint"] = fingerprint
        task = facts.get("task") or entry.get("model_type")
        if task not in {"chat", "vision", "decision", "embedding", "rerank", "image", "video"}:
            task = "unknown"
        fmt = entry.get("format")
        if found is None:
            if name in by_name:
                # Same display name is not evidence that two files are identical.
                suffix = sha256(path.encode()).hexdigest()[:8]
                name = name[:MAX_NAME_LENGTH - 9] + "-" + suffix
            model = Model(
                id=_new_model_id(), owner_principal=owner, owner_uid=ACCOUNT_OWNER_UID,
                name=name, weight_path=path, weight_path_sha256=weight_path_fingerprint(path),
                format=fmt, parameter_count=None, status=MODEL_REGISTERED,
                health=unknown(AUTO_REPORTED_MODEL_HEALTH), source=SOURCE_AUTO,
                created_at=now, updated_at=now, last_seen_at=now,
                description=AUTO_REPORTED_MODEL_DESCRIPTION, size_bytes=_size(entry.get("size_bytes")),
                kind=fmt, task=task, facts=facts,
            )
            try:
                store.insert_model(model, idempotency_key=None, fingerprint=_fingerprint(name, path))
            except Conflict:
                model = store.model_by_owner_name(owner, name)
                if model is None:
                    raise
            else:
                created.append(model.id)
            found = model.id
            by_name[name] = model
            by_id[found] = model
        else:
            model = by_id[found]
            facts = {**model.facts, **facts}
            # Prefer a still-present primary location; copies do not randomly change it.
            primary = model.weight_path if model.weight_path in seen_paths else path
            store.refresh_discovered_model(found, path=primary, path_hash=weight_path_fingerprint(primary),
                                           kind=fmt, task=task, facts=facts,
                                           size_bytes=_size(entry.get("size_bytes")), now=now)
            by_id[found] = store.model_by_id(found)
        store.upsert_location(ModelLocation(model_id=found, path=path, uid=uid,
                                            device=entry.get("device"), inode=entry.get("inode"),
                                            mtime=entry.get("mtime"), last_seen_at=now))
        adopted[path] = found
        by_path[path] = found
        if fingerprint:
            by_fingerprint[fingerprint] = found
    if full and scan_roots:
        for model in by_id.values():
            locations = store.locations_for(model.id)
            keep = [loc.path for loc in locations if loc.path in seen_paths or loc.uid != uid
                    or not any(loc.path == root or loc.path.startswith(root.rstrip("/") + "/")
                               for root in scan_roots)]
            store.mark_locations_missing(model.id, keep)
    return Adoption(adopted=adopted, created=tuple(created))


def _size(raw: object) -> int | None:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def _new_model_id() -> str:
    """与手工登记同形的 id（``mdl_<16 hex>``）。延迟导入见 ``observed_services``。"""

    from .registry import new_id

    return new_id("mdl")


__all__ = [
    "ACCOUNT_OWNER_UID",
    "AUTO_REPORTED_MODEL_DESCRIPTION",
    "AUTO_REPORTED_MODEL_HEALTH",
    "Adoption",
    "adopt_reported_models",
    "reported_name",
]
