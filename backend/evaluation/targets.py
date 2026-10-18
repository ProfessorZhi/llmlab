"""受信评测目标目录：``deployment_id -> TargetRecord``。

评测目标是**别人的资源**，所以「能不能拿它跑评测」必须由服务端目录回答，而不是由
请求体回答。本模块只做一件事：给定一个受信的 :class:`~backend.evaluation.identity.RequesterContext`
与一个 deployment id，回答授权、共享与压测三个问题。

三条规则
--------

1. **未授权 = 查无此目标。** 私有目标对非 owner 抛 :class:`~backend.evaluation.errors.TargetNotAuthorized`
   （映射为 404），而不是 403 —— 否则这个端点就成了一个可以逐 id 枚举「谁的模型部署在
   这台机器上」的预言机。与 ``backend.registry`` 的验收标准一致。

2. **共享 != 可以压测。** 共享的语义是「你可以调用它」。throughput / latency 会打满
   显存，是一个重得多的动作，因此对非 owner 默认**拒绝**，除非 owner 或管理员在目录里
   显式把该主体加进 ``gpu_stress_granted``。共享永远不会"顺带"获得压测权。

3. **目录里不存路径与地址。** ``TargetRecord`` 只持有不透明的 registry 引用
   （``model_ref`` / ``endpoint_ref``），并在构造时拒绝任何看起来像路径或 URL 的值。
   评测中心因此不可能通过「读一个字段」拿到权重路径或别人的 loopback 地址——
   这不是运行期检查，是**存不进去**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .errors import InvalidRequest
from .identity import (
    PrincipalDirectory,
    RequesterContext,
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_MODES,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
)


def _reject_pathlike(value: object, *, what: str) -> str | None:
    """拒绝任何像文件路径或网络地址的引用值。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(f"{what} must be a non-empty string or null")
    candidate = value.strip()
    if len(candidate) > 128:
        raise InvalidRequest(f"{what} exceeds 128 characters")
    if "://" in candidate or candidate.startswith("/") or candidate.startswith("~"):
        raise InvalidRequest(f"{what} must be an opaque registry reference, not a path or URL")
    if any(sep in candidate for sep in ("/", "\\", "\x00")):
        raise InvalidRequest(f"{what} must be an opaque registry reference, not a path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise InvalidRequest(f"{what} contains control characters")
    return candidate


@dataclass(frozen=True)
class TargetRecord:
    """一个可被评测的部署/端点登记。"""

    deployment_id: str
    project_id: str
    owner_principal: str
    owner_uid: int
    ready: bool
    visibility: str = VISIBILITY_PRIVATE
    grantees: frozenset[str] = frozenset()
    gpu_stress_granted: frozenset[str] = frozenset()
    engine: str | None = None
    model_ref: str | None = None
    endpoint_ref: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_id, str) or not self.deployment_id.strip():
            raise InvalidRequest("deployment_id must be a non-empty string")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise InvalidRequest("project_id must be a non-empty string")
        if not isinstance(self.owner_principal, str) or not self.owner_principal.strip():
            raise InvalidRequest("owner_principal must be a non-empty string")
        if not isinstance(self.owner_uid, int) or isinstance(self.owner_uid, bool) or self.owner_uid < 0:
            raise InvalidRequest("owner_uid must be a non-negative integer")
        if not isinstance(self.ready, bool):
            raise InvalidRequest("ready must be a boolean")
        if self.visibility not in VISIBILITY_MODES:
            raise InvalidRequest(f"unknown visibility mode: {self.visibility!r}")
        # 目录里不存路径与地址：见模块 docstring 第 3 条。
        object.__setattr__(self, "model_ref", _reject_pathlike(self.model_ref, what="model_ref"))
        object.__setattr__(
            self, "endpoint_ref", _reject_pathlike(self.endpoint_ref, what="endpoint_ref")
        )

    def authorized_for(
        self, requester: RequesterContext, principals: PrincipalDirectory
    ) -> TargetAuthorization:
        """这个主体能不能用这个目标，以及能不能压测它。"""
        if not isinstance(requester, RequesterContext):
            raise InvalidRequest("requester must be a RequesterContext")

        if requester.principal == self.owner_principal:
            return TargetAuthorization(
                authorized=True,
                shared=False,
                gpu_stress_allowed=True,
                reason="owner",
            )

        if self.visibility == VISIBILITY_PRIVATE:
            return TargetAuthorization(
                authorized=False,
                shared=True,
                gpu_stress_allowed=False,
                reason="target_is_private",
            )

        if self.visibility == VISIBILITY_USERS:
            granted = requester.principal in self.grantees
        else:
            # all_approved：成员来源是受信主体目录，不是请求。
            granted = requester.principal in principals.approved_principals()

        if not granted:
            return TargetAuthorization(
                authorized=False,
                shared=True,
                gpu_stress_allowed=False,
                reason="not_granted",
            )

        return TargetAuthorization(
            authorized=True,
            shared=True,
            # 共享的默认是「不压测」。显式授予才改。
            gpu_stress_allowed=requester.principal in self.gpu_stress_granted,
            reason="shared",
        )

    def to_dict(self, *, include_acl: bool = False) -> dict:
        payload = {
            "deployment_id": self.deployment_id,
            "project_id": self.project_id,
            "owner": self.owner_principal,
            "ready": self.ready,
            "engine": self.engine,
            "model_ref": self.model_ref,
            "endpoint_ref": self.endpoint_ref,
            "display_name": self.display_name,
        }
        if include_acl:
            payload["acl"] = {
                "mode": self.visibility,
                "grantees": sorted(self.grantees),
                "gpu_stress_granted": sorted(self.gpu_stress_granted),
            }
        else:
            # 非 owner 不看到名单：共享模式的形状本身就是别人的授权信息。
            payload["acl"] = None
        return payload


@dataclass(frozen=True)
class TargetAuthorization:
    authorized: bool
    shared: bool
    gpu_stress_allowed: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "authorized": self.authorized,
            "shared": self.shared,
            "gpu_stress_allowed": self.gpu_stress_allowed,
            "reason": self.reason,
        }


class TargetCatalog:
    """受信目标目录：纯内存、无 IO、不探测、不打开任何东西。"""

    def __init__(self, records: Iterable[TargetRecord]) -> None:
        by_id: dict[str, TargetRecord] = {}
        for record in records:
            if not isinstance(record, TargetRecord):
                raise InvalidRequest("catalog entries must be TargetRecord")
            if record.deployment_id in by_id:
                raise InvalidRequest(f"duplicate deployment: {record.deployment_id!r}")
            by_id[record.deployment_id] = record
        self._by_id = by_id

    def resolve(self, deployment_id: object) -> TargetRecord | None:
        if not isinstance(deployment_id, str) or not deployment_id.strip():
            return None
        return self._by_id.get(deployment_id.strip())

    def sorted_ids(self) -> list[str]:
        return sorted(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)


def empty_catalog() -> TargetCatalog:
    """没有任何登记的目标目录。

    这是一个**如实的**状态：本部署此刻没有接线到 ``backend.registry`` 的目标目录，
    所以每个提交都会以「查无此目标」被拒（fail closed），而不是被放进一个没有落点的队列。
    """
    return TargetCatalog(())


__all__ = [
    "TargetAuthorization",
    "TargetCatalog",
    "TargetRecord",
    "empty_catalog",
]
