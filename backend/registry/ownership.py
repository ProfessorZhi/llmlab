"""「这个引擎是谁的」：运维策略（谁能被登记）与所有权证明（谁证明得了）。

本模块把两件相关的事放在一起，因为它们的答案互为补充：

1. **运维策略**（:class:`OperatorPolicy`）—— 一份由 root/运维拥有的配置文件，
   逐条承认「这台机器上确实存在这个服务」。既有 Strata 在 ``127.0.0.1:8080``，
   8080 又是保留端口；唯一的合法通路就是运维显式写下一条 entry。
2. **所有权证明**（:class:`OwnershipProver`）—— 一个引擎指向某个回环端口，
   「它是不是登记者的进程」这个问题的答案。

两件都不能靠自报。**没有证明就是没有证明**：默认状态是
``ownership_unverified``，它不是一个警告，而是一条会让共享/调用被拒的判定
（由 ``RegistryConfig.ownership_policy`` 决定是否强制，见 ``registry.py``）。

``uid`` / socket 主人的**自动**证明在本轮是 **BLOCKED**：

* 要回答「这个回环端口属于哪个 uid」，得读 ``/proc/net/tcp`` 拿 inode，
  再扫 ``/proc/<pid>/fd`` 反查进程，然后用 ``os.stat`` 拿 socket 属主 ——
  这两步分别需要 root 权限与遍历**其他用户**的进程表。
* 本仓库的可达边界不支持这个（``docs/access-boundaries.md`` 明确不触碰其他
  Linux 用户的进程与家目录），而且这段代码无法在 CI 里被真实验证 ——
  一段「跑不了、验不了」的特权扫描代码不该以「已完成」的姿态进库。

所以交付的是**契约**：:class:`OwnershipProver` 这个洞是留给那个特权
owner-agent 的，本轮只有两个实现 —— 一个默认的 fail-closed
（:class:`NoOwnershipProver`）与一个显式标注的测试替身
（:class:`StaticOwnershipProver`）。运维承认的服务走 :class:`OperatorPolicy`，
那条路是**真的能跑**的。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import Iterable, Protocol

from .errors import InvalidRequest, StorageError
from .urls import NormalizedTarget, OperatorTarget, is_loopback_host

# --- 所有权状态 -------------------------------------------------------------

#: 没有证明。这是**默认**，也是唯一诚实的缺省。
OWNERSHIP_UNVERIFIED = "ownership_unverified"
#: 由 root/运维拥有的策略文件逐条承认（既有服务的通路）。
OWNERSHIP_OPERATOR_VERIFIED = "operator_verified"
#: 由某个 prover 依据本机事实（uid / socket 属主）证明过。
OWNERSHIP_UID_VERIFIED = "uid_verified"

OWNERSHIP_STATES = (
    OWNERSHIP_UNVERIFIED,
    OWNERSHIP_OPERATOR_VERIFIED,
    OWNERSHIP_UID_VERIFIED,
)

#: 哪些状态算「证明过」。刻意是一个显式集合，而不是 ``!= UNVERIFIED`` 之类的
#: 取反 —— 以后新增状态时，取反会**默认放行**，枚举会默认拒绝。
OWNERSHIP_VERIFIED_STATES = frozenset({OWNERSHIP_OPERATOR_VERIFIED, OWNERSHIP_UID_VERIFIED})

REASON_NO_OWNERSHIP_SOURCE = "no_ownership_source_in_this_deployment"
REASON_OPERATOR_POLICY = "operator_policy_entry"
REASON_TEST_DOUBLE = "static_test_double"

#: 所有权强制策略。
#:
#: * ``advisory`` —— **记录**所有权状态，但不阻断共享/调用。这是兼容缺省：
#:   既有的 ``backend/bff`` 用默认配置构造服务，而本模块不允许修改它，
#:   所以缺省必须保持历史行为，否则会静默改掉一个不属于本模块的对外路径。
#: * ``enforced`` —— 未证明即拒绝共享与调用。**生产应当用这一档**
#:   （见 ``RegistryConfig.production_posture()``）。
#:
#: 这条缺省的取舍被显式记进 ``docs/registry-cli-contract.md``：一个默认关闭的
#: 安全开关必须写在明处，而不是藏在默认值里。
OWNERSHIP_POLICY_ADVISORY = "advisory"
OWNERSHIP_POLICY_ENFORCED = "enforced"
OWNERSHIP_POLICIES = (OWNERSHIP_POLICY_ADVISORY, OWNERSHIP_POLICY_ENFORCED)


@dataclass(frozen=True)
class OwnershipProof:
    """一次所有权判定的结果。``state`` 之外的东西都是佐证，不是授权依据。"""

    state: str
    method: str
    uid: int | None = None
    detail: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.state not in OWNERSHIP_STATES:
            raise InvalidRequest(f"unknown ownership state: {self.state!r}")
        if self.uid is not None and (
            not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0
        ):
            raise InvalidRequest("ownership proof uid must be a non-negative integer")

    @property
    def verified(self) -> bool:
        return self.state in OWNERSHIP_VERIFIED_STATES

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "method": self.method,
            "verified": self.verified,
            "uid": self.uid,
            "detail": self.detail,
            "source": self.source,
        }


def unverified_proof(
    reason: str = REASON_NO_OWNERSHIP_SOURCE, *, detail: str | None = None
) -> OwnershipProof:
    return OwnershipProof(
        state=OWNERSHIP_UNVERIFIED, method=reason, detail=detail, source=None
    )


class OwnershipProver(Protocol):
    """把一个回环目标映射到「它的进程属于谁」。

    实现**必须**只读本机事实，且必须能证明失败 —— 拿不准就返回
    ``ownership_unverified``，不要猜。
    """

    name: str

    def prove(self, target: NormalizedTarget) -> OwnershipProof:  # pragma: no cover - protocol
        ...


class NoOwnershipProver:
    """默认实现：**永远**说不出来。fail-closed 的那一半。

    它存在的意义不是「还没做」，而是「本部署确实没有这条来源」——
    与 ``health.SOURCE_NONE`` 同一个纪律：没有来源就是 UNKNOWN，不是「大概没问题」。
    """

    name = "none"

    def prove(self, target: NormalizedTarget) -> OwnershipProof:
        if not isinstance(target, NormalizedTarget):
            raise InvalidRequest("target must be a NormalizedTarget")
        return unverified_proof(REASON_NO_OWNERSHIP_SOURCE)


class StaticOwnershipProver:
    """**测试替身**：按 (host, port) 预置证明。

    与 ``health.StaticProber`` 同等地位：唯一被允许的另一种确定性来源，
    显式标注为替身，且**只**用于离线测试与合成开发。
    """

    name = "static_test_double"

    def __init__(self, verdicts: dict[tuple[str, int], int] | None = None) -> None:
        self._verdicts = {key: int(value) for key, value in (verdicts or {}).items()}

    def set(self, host: str, port: int, uid: int) -> None:
        self._verdicts[(host, int(port))] = int(uid)

    def prove(self, target: NormalizedTarget) -> OwnershipProof:
        uid = self._verdicts.get((target.host, target.port))
        if uid is None:
            return unverified_proof(REASON_TEST_DOUBLE, detail="no static verdict for this target")
        return OwnershipProof(
            state=OWNERSHIP_UID_VERIFIED,
            method=REASON_TEST_DOUBLE,
            uid=uid,
            detail="injected fixture verdict",
            source=self.name,
        )


# --- 运维策略 ---------------------------------------------------------------

OPERATOR_POLICY_SCHEMA = "registry-operator/1"

#: 策略文件大小上限。一份「承认若干既有服务」的清单不该很大。
MAX_POLICY_BYTES = 64 * 1024


@dataclass(frozen=True)
class OperatorPolicy:
    """一份已加载的运维策略。

    ``provenance`` 是**给审计看的**：文件路径 + 属主 + 权限位。一次「因为运维
    承认所以放行了 8080」的决定，必须能回答「依据是哪份文件、谁写的」。
    """

    targets: tuple[OperatorTarget, ...] = ()
    provenance: str = "empty"
    loaded_from: str | None = None

    def match(self, target: NormalizedTarget) -> OperatorTarget | None:
        for entry in self.targets:
            if entry.matches(
                scheme=target.scheme,
                host=target.host,
                port=target.port,
                path=target.path,
            ):
                return entry
        return None

    @property
    def empty(self) -> bool:
        return not self.targets

    def to_dict(self) -> dict:
        return {
            "schema": OPERATOR_POLICY_SCHEMA,
            "provenance": self.provenance,
            "loaded_from": self.loaded_from,
            "targets": [entry.to_dict() for entry in self.targets],
        }


EMPTY_POLICY = OperatorPolicy()


def default_policy_owners() -> frozenset[int]:
    """策略文件可接受的属主：root，**或**正在运行本服务的那个账号。

    「正在运行本服务的账号」是运维在这里的具体形态：如果服务跑在 ``nobody``
    下，那权限文件就必须属于 root 或 nobody。任何**别的**用户拥有的文件都不算
    运维策略 —— 否则任何一个本机用户都能靠写一个文件给自己开 8080。
    """
    return frozenset({0, os.getuid()})


def load_operator_policy(
    path: str | os.PathLike[str],
    *,
    allowed_owners: Iterable[int] | None = None,
) -> OperatorPolicy:
    """从一份 **root/运维拥有** 的 JSON 文件加载策略。

    拒绝（抛 ``StorageError``）：软链、非普通文件、属主不在允许集合内、
    组/其他人可写、超大、JSON 不合法、schema 不认识、entry 不合法。
    每一条都对应一种真实的失手方式，而不是防御性编程的仪式。
    """
    owners = (
        frozenset(int(owner) for owner in allowed_owners)
        if allowed_owners is not None
        else default_policy_owners()
    )
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise StorageError(f"operator policy not found: {path}") from exc
    except OSError as exc:
        raise StorageError(f"cannot stat operator policy: {exc.strerror}") from exc

    if stat.S_ISLNK(info.st_mode):
        raise StorageError("operator policy must not be a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise StorageError("operator policy must be a regular file")
    if info.st_uid not in owners:
        raise StorageError(
            f"operator policy must be owned by root or by the service account "
            f"(uid in {sorted(owners)}), not uid {info.st_uid}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise StorageError("operator policy must not be group- or world-writable")
    if info.st_size > MAX_POLICY_BYTES:
        raise StorageError(f"operator policy exceeds {MAX_POLICY_BYTES} bytes")

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise StorageError(f"operator policy is not valid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise StorageError(f"cannot read operator policy: {exc.strerror}") from exc

    if not isinstance(raw, dict):
        raise StorageError("operator policy must be a JSON object")
    if raw.get("schema") != OPERATOR_POLICY_SCHEMA:
        raise StorageError(f"unsupported operator policy schema: {raw.get('schema')!r}")

    entries = raw.get("targets", [])
    if not isinstance(entries, list):
        raise StorageError("operator policy 'targets' must be a list")

    parsed: list[OperatorTarget] = []
    provenance = f"{path} (uid={info.st_uid} mode={oct(stat.S_IMODE(info.st_mode))})"
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise StorageError(f"operator policy target #{index} must be an object")
        try:
            host = str(entry.get("host", ""))
            if not is_loopback_host(host):
                raise InvalidRequest(
                    f"operator target host must be a loopback literal: {host!r}"
                )
            parsed.append(
                OperatorTarget(
                    scheme=str(entry.get("scheme", "http")).lower(),
                    host=host,
                    port=int(entry["port"]),
                    path_prefix=str(entry.get("path_prefix", "/")),
                    label=str(entry.get("label", "")),
                    source=provenance,
                )
            )
        except (InvalidRequest, KeyError, TypeError, ValueError) as exc:
            raise StorageError(f"operator policy target #{index} is invalid: {exc}") from exc

    return OperatorPolicy(
        targets=tuple(parsed), provenance=provenance, loaded_from=str(path)
    )


def operator_targets_for_fixture(
    targets: Iterable[OperatorTarget], *, provenance: str = "injected_fixture"
) -> OperatorPolicy:
    """**测试/合成**用：直接用一组已构造的 entry 造一份策略（不读任何文件）。

    标注为 fixture 的 provenance 会进 DTO 与审计，因此它不可能被误当成
    「某份 root 文件里的策略」。
    """
    for entry in targets:
        if not isinstance(entry, OperatorTarget):
            raise InvalidRequest("targets must be OperatorTarget values")
    return OperatorPolicy(targets=tuple(targets), provenance=provenance, loaded_from=None)
