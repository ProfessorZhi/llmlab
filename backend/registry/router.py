"""路由同步边界：把 Registry 的状态**导出**成 Gateway 认识的那份快照。

这份模块存在的理由只有一个，来自总 Tracker 与审计文档反复点名的那条不变量：

    「Registry 里改了一行」**不等于**「线上路由已经改了」。

``gateway.build_routing_config()`` 那份契约是**本仓库自己**的形状
（``registry-routing/1``），运维读它、照着**手动**改 Gateway。这条路径没有错，
但它把「生效」这一步留给了人。本模块补的是另一半：一份与并列仓库
``ProfessorZhi/llm-lab`` 的 Gateway（PR #20，schema ``llmlab.endpoint-routing/1``）
**逐字段对齐**的快照，以及一个可注入的 :class:`RouterAdapter` —— 它把快照交给
Gateway，再从 Gateway **自己发布的**状态里读回「到底生效到第几代」，只有读回来的
代数与快照一致，才轮到 Registry 去 ack。

因此这条链上每一个「已生效」都有一份**外部证据**：

    repoint -> 导出快照(修订 R) -> 交给 Gateway -> 读回 Gateway 的
    ``status.json.routing.{revision,snapshot_sha256}`` -> 与 R / 我们的 sha 逐位相等
    -> 才 ack 到 router_generation=R

任何一环缺失（没有 adapter、Gateway 没读、代数对不上、sha 对不上）都**停在**
``pending_router_sync``，绝不前进。缺省 adapter 是 :class:`DetachedRouterAdapter`
—— 它什么都不做，于是缺省行为与「本仓库不写 Gateway」一字不差。

**不变量**：本模块从不读取 Gateway 的凭据，也从不判定调用者的权限；它只做
「导出 + 交付 + 读回状态」。ACL 的最终判定始终在 Gateway 侧（见 ``gateway.py``）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .errors import InvalidRequest
from .models import (
    PROTOCOL_OPENAI_COMPAT,
    REASON_NO_ROUTER_ADAPTER,
    ROUTER_SYNC_APPLIED,
    ROUTER_SYNC_PENDING,
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
    Deployment,
    Endpoint,
    Engine,
    Model,
)
from .urls import is_loopback_host

#: 与并列仓库 Gateway（PR #20）逐字段对齐的 schema 字符串。
ROUTING_SNAPSHOT_SCHEMA = "llmlab.endpoint-routing/1"

#: Gateway 侧认识的三种 ACL。与 Registry 的 ``visibility`` 是三对三的映射。
ACL_KIND_PRIVATE = "private"
ACL_KIND_USERS = "users"
ACL_KIND_ALL = "all"
ACL_KINDS = frozenset({ACL_KIND_PRIVATE, ACL_KIND_USERS, ACL_KIND_ALL})

#: 快照里 endpoint 的状态。Registry 没有「暂停/吊销」这个维度（未 ready 的会被
#: **排除**而不是发布成 suspended），所以这里只会出现 ``active``。
ENDPOINT_STATE_ACTIVE = "active"

#: 导出/交付失败时的稳定理由 token。
REASON_SNAPSHOT_INVALID = "routing_snapshot_invalid"
REASON_SNAPSHOT_NOT_LOADED = "snapshot_not_loaded_by_gateway"
REASON_SNAPSHOT_REVISION_MISMATCH = "gateway_revision_mismatch"
REASON_SNAPSHOT_DIGEST_MISMATCH = "gateway_digest_mismatch"
REASON_GATEWAY_STATUS_UNREADABLE = "gateway_status_unreadable"


class RoutingSnapshotError(InvalidRequest):
    """导出的快照不符合 Gateway 接受的形状 —— 与其发一份会被拒的，不如不发。"""


def canonical_snapshot_bytes(snapshot: dict) -> bytes:
    """快照的**规范字节**：哈希与写盘用的是同一串，避免「校验过的」和「发出去的」是两个值。"""
    return json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def snapshot_sha256(snapshot: dict) -> str:
    return hashlib.sha256(canonical_snapshot_bytes(snapshot)).hexdigest()


def _iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_routing_snapshot(
    *,
    endpoints: list[Endpoint],
    deployments: dict[str, Deployment],
    engines: dict[str, Engine],
    models: dict[str, Model],
    grantees: dict[str, tuple[str, ...]],
    secret_refs: dict[str, dict],
    linux_user_by_principal: dict[str, str],
    generated_at: datetime,
    source: str = "llm-lab-portal registry",
) -> dict:
    """把 Registry 的状态导出成 ``llmlab.endpoint-routing/1``。

    几条刻意的取舍：

    * **只发布 ready 且非 private 以外的全部** —— 与 ``build_routing_config``
      不同，``private`` 的 Endpoint **也在**快照里，因为 Gateway 需要知道它存在
      （它的 owner 能调它，别人不能）。真正的可见性判定在 Gateway 侧。
    * **没有 ``secret_ref`` 的 engine 整个被排除**，连带它的 endpoint。理由：
      Gateway 的契约里 ``secret_ref`` 是「密钥在哪个文件的哪个变量」，一份没有它的
      engine 条目在 Gateway 那边无法取到上游凭据。宁可少发，也不发一份注定残缺的。
    * **绝不写内联密钥**：``secret_ref`` 只点名文件与变量名，值不进快照。
    * ``model_alias`` = Endpoint 的稳定 ``route_path``；``model_id`` = Model 名。
      调用方说 alias，引擎听到 id，这个改写只在这里发生一次。
    """
    if not isinstance(generated_at, datetime):
        raise RoutingSnapshotError("generated_at must be a datetime")
    if not isinstance(secret_refs, dict):
        raise RoutingSnapshotError("secret_refs must be a mapping")

    engine_entries: list[dict] = []
    published_engine_ids: dict[str, str] = {}
    excluded: list[dict] = []

    def _engine_entry(engine: Engine) -> dict | None:
        target = engine.target or {}
        host = str(target.get("host") or "")
        port = int(target.get("port") or 0)
        ref = secret_refs.get(engine.id)
        if not isinstance(ref, dict) or not str(ref.get("file") or "").strip() or not str(
            ref.get("var") or ""
        ).strip():
            excluded.append({"engine_id": engine.id, "reason": "no_secret_ref"})
            return None
        if not is_loopback_host(host) or port <= 0:
            excluded.append({"engine_id": engine.id, "reason": "upstream_not_loopback"})
            return None
        entry = {
            "engine_id": engine.id,
            "kind": "openai-compatible",
            "upstream": {"transport": "http", "host": host, "port": port},
            # 只点名，不携带值。
            "secret_ref": {
                "file": str(ref.get("file")).strip(),
                "var": str(ref.get("var")).strip(),
            },
        }
        if engine.protocol == PROTOCOL_OPENAI_COMPAT:
            entry["models_path"] = "/v1/models"
        return entry

    for engine in engines.values():
        entry = _engine_entry(engine)
        if entry is not None:
            engine_entries.append(entry)
            published_engine_ids[engine.id] = engine.id

    endpoint_entries: list[dict] = []
    for endpoint in sorted(endpoints, key=lambda item: item.route_path):
        if not endpoint.shareable:
            excluded.append({"endpoint_id": endpoint.id, "reason": "not_ready"})
            continue
        deployment = deployments.get(endpoint.deployment_id)
        if deployment is None:
            excluded.append({"endpoint_id": endpoint.id, "reason": "deployment_missing"})
            continue
        if deployment.engine_id not in published_engine_ids:
            excluded.append({"endpoint_id": endpoint.id, "reason": "engine_not_published"})
            continue
        model = models.get(deployment.model_id)
        if model is None:
            excluded.append({"endpoint_id": endpoint.id, "reason": "model_missing"})
            continue

        owner_linux = linux_user_by_principal.get(endpoint.owner_principal)
        if not owner_linux:
            excluded.append({"endpoint_id": endpoint.id, "reason": "owner_unknown"})
            continue

        if endpoint.visibility == VISIBILITY_PRIVATE:
            acl: dict = {"kind": ACL_KIND_PRIVATE}
        elif endpoint.visibility == VISIBILITY_ALL_APPROVED:
            acl = {"kind": ACL_KIND_ALL}
        elif endpoint.visibility == VISIBILITY_USERS:
            names = []
            for grantee in sorted(grantees.get(endpoint.id, ())):
                linux = linux_user_by_principal.get(grantee)
                if linux:
                    names.append(linux)
            acl = {"kind": ACL_KIND_USERS, "principals": names}
        else:
            # 无法表达的可见性绝不猜：猜错就是把一份错误的 ACL 发给了 Gateway。
            excluded.append({"endpoint_id": endpoint.id, "reason": "unknown_visibility"})
            continue

        endpoint_entries.append(
            {
                "endpoint_id": endpoint.id,
                "engine_id": deployment.engine_id,
                "model_alias": endpoint.route_path,
                "model_id": model.name,
                "owner_uid": owner_linux,
                "acl": acl,
                "state": ENDPOINT_STATE_ACTIVE,
            }
        )

    revision = max((item.revision for item in endpoints if item.shareable), default=0)
    snapshot = {
        "schema": ROUTING_SNAPSHOT_SCHEMA,
        "revision": int(revision),
        "generated_at": _iso_utc(generated_at),
        "source": source,
        "engines": engine_entries,
        "endpoints": endpoint_entries,
        # 本模块自加的、非 Gateway 契约字段：说清楚哪些**没被**发出去，免得
        # 「快照里没有」被读成「本来就没有」。Gateway 会忽略它不认识的键。
        "excluded": excluded,
    }
    validate_routing_snapshot(snapshot)
    return snapshot


def validate_routing_snapshot(snapshot: object) -> None:
    """严格校验一份快照**是不是** Gateway 会接受的那份形状。

    刻意与 Gateway 的读侧同构：宁可在这里抛 :class:`RoutingSnapshotError`，
    也不要把一份会被 Gateway 拒的（或被它部分读走的）文件交出去。
    """
    if not isinstance(snapshot, dict):
        raise RoutingSnapshotError("snapshot must be an object")
    if snapshot.get("schema") != ROUTING_SNAPSHOT_SCHEMA:
        raise RoutingSnapshotError(
            f"schema must be {ROUTING_SNAPSHOT_SCHEMA!r}, got {snapshot.get('schema')!r}"
        )
    revision = snapshot.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise RoutingSnapshotError("revision must be a non-negative integer")
    if not isinstance(snapshot.get("generated_at"), str) or not snapshot["generated_at"]:
        raise RoutingSnapshotError("generated_at must be a non-empty string")

    engines = snapshot.get("engines")
    if not isinstance(engines, list):
        raise RoutingSnapshotError("engines must be a list")
    engine_ids: set[str] = set()
    for entry in engines:
        if not isinstance(entry, dict):
            raise RoutingSnapshotError("engine entries must be objects")
        engine_id = entry.get("engine_id")
        if not isinstance(engine_id, str) or not engine_id:
            raise RoutingSnapshotError("engine_id must be a non-empty string")
        if engine_id in engine_ids:
            raise RoutingSnapshotError(f"duplicate engine_id {engine_id!r}")
        engine_ids.add(engine_id)
        upstream = entry.get("upstream")
        if not isinstance(upstream, dict) or upstream.get("transport") not in ("http", "unix"):
            raise RoutingSnapshotError(
                f"engine {engine_id!r} upstream.transport must be 'http' or 'unix'"
            )
        if upstream["transport"] == "http":
            if not is_loopback_host(str(upstream.get("host") or "")):
                raise RoutingSnapshotError(
                    f"engine {engine_id!r} upstream.host must be loopback"
                )
            port = upstream.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
                raise RoutingSnapshotError(f"engine {engine_id!r} upstream.port is invalid")
        else:
            path = upstream.get("path")
            if not isinstance(path, str) or not path.startswith("/"):
                raise RoutingSnapshotError(
                    f"engine {engine_id!r} unix upstream needs an absolute path"
                )
        ref = entry.get("secret_ref")
        if not isinstance(ref, dict) or not str(ref.get("file") or "") or not str(
            ref.get("var") or ""
        ):
            raise RoutingSnapshotError(
                f"engine {engine_id!r} secret_ref must name a file and a var"
            )
        # 值绝不能内联 —— 快照会落盘，密钥不该落在 Registry 写的文件里。
        if "value" in ref or "secret" in ref or "key" in ref:
            raise RoutingSnapshotError(
                f"engine {engine_id!r} secret_ref must not carry a value"
            )

    endpoints = snapshot.get("endpoints")
    if not isinstance(endpoints, list):
        raise RoutingSnapshotError("endpoints must be a list")
    seen: set[str] = set()
    for entry in endpoints:
        if not isinstance(entry, dict):
            raise RoutingSnapshotError("endpoint entries must be objects")
        endpoint_id = entry.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise RoutingSnapshotError("endpoint_id must be a non-empty string")
        if endpoint_id in seen:
            raise RoutingSnapshotError(f"duplicate endpoint_id {endpoint_id!r}")
        seen.add(endpoint_id)
        if entry.get("engine_id") not in engine_ids:
            raise RoutingSnapshotError(
                f"endpoint {endpoint_id!r} references an engine that is not in the snapshot"
            )
        for field_name in ("model_alias", "model_id", "owner_uid"):
            value = entry.get(field_name)
            if not isinstance(value, str) or not value:
                raise RoutingSnapshotError(
                    f"endpoint {endpoint_id!r} {field_name} must be a non-empty string"
                )
        acl = entry.get("acl")
        if not isinstance(acl, dict) or acl.get("kind") not in ACL_KINDS:
            raise RoutingSnapshotError(
                f"endpoint {endpoint_id!r} acl.kind must be one of {sorted(ACL_KINDS)}"
            )
        if acl["kind"] == ACL_KIND_USERS:
            principals = acl.get("principals")
            if not isinstance(principals, list) or not all(
                isinstance(item, str) and item for item in principals
            ):
                raise RoutingSnapshotError(
                    f"endpoint {endpoint_id!r} acl.principals must be a list of names"
                )
        if entry.get("state") not in (ENDPOINT_STATE_ACTIVE, "suspended", "revoked"):
            raise RoutingSnapshotError(
                f"endpoint {endpoint_id!r} state must be active/suspended/revoked"
            )


@dataclass(frozen=True)
class RouterPublishResult:
    """一次「交给 Gateway 并读回状态」的结果。**没有猜测** —— 只有证据与理由。"""

    published: bool
    applied_revision: int | None
    snapshot_sha256: str | None
    state: str
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "published": self.published,
            "applied_revision": self.applied_revision,
            "snapshot_sha256": self.snapshot_sha256,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
        }


class RouterAdapter(Protocol):
    """把一份已导出的快照交给 Gateway，并读回 Gateway **自己说的**生效代数。

    实现**不得**在没有外部证据时返回 ``published=True`` / 一个代数：那正是
    「谎报已切换」的成因。
    """

    name: str
    attached: bool

    def publish(self, snapshot: dict) -> RouterPublishResult:  # pragma: no cover - protocol
        ...

    def applied_revision(self) -> int | None:  # pragma: no cover - protocol
        ...


class DetachedRouterAdapter:
    """**缺省**：本部署没有接任何路由器同步。于是什么都不做、什么都不声称。

    它存在的意义是把「本仓库不写 Gateway」这条既有事实**放进类型里**：repoint
    之后状态仍是 ``pending_router_sync``，理由 ``no_router_adapter_in_this_deployment``。
    """

    name = "detached_router"
    attached = False

    def publish(self, snapshot: dict) -> RouterPublishResult:
        return RouterPublishResult(
            published=False,
            applied_revision=None,
            snapshot_sha256=None,
            state=ROUTER_SYNC_PENDING,
            reason=REASON_NO_ROUTER_ADAPTER,
            detail=(
                "本部署没有配置 RouterAdapter；快照没有被交给任何 Gateway，"
                "状态诚实地停在 pending_router_sync。"
            ),
        )

    def applied_revision(self) -> int | None:
        return None


@dataclass
class FileRouterAdapter:
    """把快照**原子地**写到 Gateway 会读的那个路径，再读回它发布的状态。

    这是与并列仓库 Gateway（PR #20）最贴近的接法：Gateway 用
    ``--routing-snapshot PATH`` 读一个**只读且受信**的文件，并在
    ``status.json.routing`` 里发布「实际加载的 sha256 / revision / generated_at」。
    所以本 adapter 做两件事：

    1. **写**：规范字节 -> 同目录临时文件 -> ``os.replace``（原子；Gateway 的
       ``stat`` 只会看到「旧」或「新」，不会看到半个文件）。写入权限 0644，
       即**非**组/其他可写 —— 这正是 Gateway 会拒绝的形态之一。
    2. **读回**：解析 ``status_path``，取 ``routing.{revision,snapshot_sha256}``，
       与本次写入的 ``revision`` / 我们的 sha **逐位比对**。相等才算
       ``published=True``。Gateway 说 ``routing: null``（没加载任何快照）、
       代数对不上、sha 对不上，都各自给出**不同**的理由，而不是笼统的失败。

    生产约束（不满足就 fail-closed，且写在这里而不是散在注释里）：快照文件必须
    是**普通文件**、属主是 root 或 Gateway 自己的 uid、且非组/其他可写。Registry
    进程通常以普通账号运行，因此这条路径要求运维预先建好一个双方都能访问的目录
    （例如 Gateway 读、Registry 组可写的目录）。**本文档不假装这一步已经做过。**
    """

    snapshot_path: str
    status_path: str
    name: str = "file_router"
    attached: bool = True

    def __post_init__(self) -> None:
        for attr in ("snapshot_path", "status_path"):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value.strip():
                raise InvalidRequest(f"{attr} must be a non-empty path")
            if os.path.islink(value):
                # 软链可以把写入引到别处 —— 与 token 文件的纪律一致，直接拒绝。
                raise InvalidRequest(f"{attr} must not be a symlink")
        if os.path.abspath(self.snapshot_path) == os.path.abspath(self.status_path):
            raise InvalidRequest("snapshot_path and status_path must differ")

    def _write(self, snapshot: dict) -> str:
        payload = canonical_snapshot_bytes(snapshot)
        digest = hashlib.sha256(payload).hexdigest()
        directory = os.path.dirname(os.path.abspath(self.snapshot_path)) or "."
        handle, tmp_path = tempfile.mkstemp(prefix=".routing-", dir=directory)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, self.snapshot_path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return digest

    def _read_status(self) -> dict | None:
        try:
            with open(self.status_path, "rb") as stream:
                raw = stream.read()
        except OSError:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def publish(self, snapshot: dict) -> RouterPublishResult:
        validate_routing_snapshot(snapshot)
        digest = snapshot_sha256(snapshot)
        try:
            written_digest = self._write(snapshot)
        except OSError as exc:
            return RouterPublishResult(
                published=False,
                applied_revision=None,
                snapshot_sha256=digest,
                state=ROUTER_SYNC_PENDING,
                reason=REASON_GATEWAY_STATUS_UNREADABLE,
                detail=f"无法写入快照文件：{type(exc).__name__}",
            )
        assert written_digest == digest  # 同一串字节，绝不出现两个哈希

        status = self._read_status()
        if status is None:
            return RouterPublishResult(
                published=False,
                applied_revision=None,
                snapshot_sha256=digest,
                state=ROUTER_SYNC_PENDING,
                reason=REASON_GATEWAY_STATUS_UNREADABLE,
                detail="Gateway 状态文件缺失或不可解析；快照已写入，但未被确认加载。",
            )

        routing = status.get("routing")
        if not isinstance(routing, dict):
            return RouterPublishResult(
                published=False,
                applied_revision=None,
                snapshot_sha256=digest,
                state=ROUTER_SYNC_PENDING,
                reason=REASON_SNAPSHOT_NOT_LOADED,
                detail="Gateway 报告 routing 为空：没有加载任何快照。",
            )
        applied = routing.get("revision")
        applied_sha = routing.get("snapshot_sha256")
        if not isinstance(applied, int) or isinstance(applied, bool):
            return RouterPublishResult(
                published=False,
                applied_revision=None,
                snapshot_sha256=digest,
                state=ROUTER_SYNC_PENDING,
                reason=REASON_GATEWAY_STATUS_UNREADABLE,
                detail="Gateway 的 routing.revision 不是整数。",
            )
        if applied != snapshot["revision"]:
            return RouterPublishResult(
                published=False,
                applied_revision=applied,
                snapshot_sha256=digest,
                state=ROUTER_SYNC_PENDING,
                reason=REASON_SNAPSHOT_REVISION_MISMATCH,
                detail=(
                    f"Gateway 生效的是第 {applied} 代，快照是第 {snapshot['revision']} 代；"
                    "拒绝把落后的代数当成已生效。"
                ),
            )
        if applied_sha != digest:
            return RouterPublishResult(
                published=False,
                applied_revision=applied,
                snapshot_sha256=digest,
                state=ROUTER_SYNC_PENDING,
                reason=REASON_SNAPSHOT_DIGEST_MISMATCH,
                detail="Gateway 加载的内容与本次导出的 sha256 不一致。",
            )
        return RouterPublishResult(
            published=True,
            applied_revision=applied,
            snapshot_sha256=digest,
            state=ROUTER_SYNC_APPLIED,
            reason=None,
            detail="Gateway 已按本次快照的 sha256 与代数确认加载。",
        )

    def applied_revision(self) -> int | None:
        status = self._read_status()
        if status is None:
            return None
        routing = status.get("routing")
        if not isinstance(routing, dict):
            return None
        applied = routing.get("revision")
        return applied if isinstance(applied, int) and not isinstance(applied, bool) else None


__all__ = [
    "ACL_KIND_ALL",
    "ACL_KIND_PRIVATE",
    "ACL_KIND_USERS",
    "DetachedRouterAdapter",
    "FileRouterAdapter",
    "REASON_SNAPSHOT_DIGEST_MISMATCH",
    "REASON_SNAPSHOT_INVALID",
    "REASON_SNAPSHOT_NOT_LOADED",
    "REASON_SNAPSHOT_REVISION_MISMATCH",
    "ROUTER_SYNC_APPLIED",
    "ROUTER_SYNC_PENDING",
    "ROUTING_SNAPSHOT_SCHEMA",
    "RouterAdapter",
    "RouterPublishResult",
    "RoutingSnapshotError",
    "build_routing_snapshot",
    "canonical_snapshot_bytes",
    "snapshot_sha256",
    "validate_routing_snapshot",
]
