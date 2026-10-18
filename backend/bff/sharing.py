"""共享范围：前端三值 ↔ Registry 三值，以及「把受托人解析成受信主体」。

两边用的是同一件事的两套词：

    portal ``private``   ⇄  registry ``private``
    portal ``selected``  ⇄  registry ``users``                    （点名共享）
    portal ``all``       ⇄  registry ``all_approved_platform_users``

Registry 的那套是**权威**：``share_endpoint`` 只接受它。前端那套已经发布，
所以翻译放在这里，而不是改任何一边。

受托人解析是本模块的第二个职责，也是「共享给指定 UID」落地的地方：用户打的
可以是 **UID**（``4003``）、**主体名**（``linux:dell-test-01:4003``）或**Linux 账号名**
（``bob``）。三者都**必须**在受信目录里查得到才成立，且都以同一份
``PrincipalDirectory`` 为存在性与审批状态的唯一裁判：

* 账号名走的是 ``PwdSource`` + 目录的**精确**匹配，没有别名表 —— 因此
  ``dana`` 不会变成 ``bob``，它只会「查无此人」而被拒。
  （合成表里有 ``bob``(4003) 而没有 ``dana``；测试两种都覆盖。）
* uid 只在**已审批**的主体里找；目录里没有该 uid 就是 400，不是「先建一个」。

一条可以单独断言的性质：无论用户打了哪种形式，最终写进 ACL 的一定是**主体名**，
永远不是 UID、不是账号名、不是请求里的任何自报字段。
"""

from __future__ import annotations

from ..registry import errors as registry_errors
from ..registry.models import (
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
)

#: 前端 ⇒ Registry。
PORTAL_TO_REGISTRY_MODE = {
    "private": VISIBILITY_PRIVATE,
    "selected": VISIBILITY_USERS,
    "all": VISIBILITY_ALL_APPROVED,
}

#: Registry ⇒ 前端。
REGISTRY_TO_PORTAL_MODE = {
    VISIBILITY_PRIVATE: "private",
    VISIBILITY_USERS: "selected",
    VISIBILITY_ALL_APPROVED: "all",
}

#: 共享范围由窄到宽。用于「多个 Endpoint 意见不一致时取最窄的那个」——
#: 宁可少报可见性，也不能多报。
MODE_WIDTH = {"private": 0, "selected": 1, "all": 2}

PRIVATE_SHARE = {"mode": "private", "principals": [], "revocable": True}


def portal_to_registry_mode(mode: object) -> str:
    if not isinstance(mode, str) or mode not in PORTAL_TO_REGISTRY_MODE:
        raise registry_errors.InvalidRequest(
            f"unsupported share mode: {mode!r}; expected one of "
            f"{', '.join(sorted(PORTAL_TO_REGISTRY_MODE))}"
        )
    return PORTAL_TO_REGISTRY_MODE[mode]


def registry_to_portal_mode(mode: object) -> str:
    return REGISTRY_TO_PORTAL_MODE.get(mode, "private")  # type: ignore[arg-type]


def endpoint_share_block(endpoint, grantees: tuple[str, ...]) -> dict:
    """一个 Endpoint 的共享范围，翻成前端形状。"""
    mode = registry_to_portal_mode(endpoint.visibility)
    if mode == "private":
        return dict(PRIVATE_SHARE)
    if mode == "all":
        return {"mode": "all", "principals": [], "revocable": True}
    return {
        "mode": "selected",
        "principals": sorted(grantees),
        "revocable": True,
    }


def narrowest_share(blocks: list[dict]) -> dict:
    """多个 Endpoint 的共享范围合成一个**下界**。

    一个 Deployment 可能挂着不止一个 Endpoint，而前端每行只有一个共享控件。
    合成的规则是「取最窄」：只要有一个还是 private，这一行就是 private。
    ``selected`` 时受托人取**交集**（只有每个 Endpoint 都给了的人才算），
    交集为空就退回 private —— 因为契约不允许 ``selected`` 不点名任何人，
    而「点了一个其实没全给的人」比「少报成私有」更糟。
    """
    if not blocks:
        return dict(PRIVATE_SHARE)
    width = min(MODE_WIDTH.get(block["mode"], 0) for block in blocks)
    if width == 0:
        return dict(PRIVATE_SHARE)
    if width == 2:
        return {"mode": "all", "principals": [], "revocable": True}
    shared = None
    for block in blocks:
        if block["mode"] != "selected":
            continue
        names = set(block["principals"])
        shared = names if shared is None else (shared & names)
    if not shared:
        return dict(PRIVATE_SHARE)
    return {"mode": "selected", "principals": sorted(shared), "revocable": True}


# --------------------------------------------------------------------------
# 受托人解析
# --------------------------------------------------------------------------


def _uid_index(principals) -> dict[int, str]:
    """已审批主体里的 ``uid ⇒ principal``。未审批的一律不进索引。"""
    index: dict[int, str] = {}
    for name in principals.approved_principals():
        record = principals.lookup(name)
        if record is not None:
            index.setdefault(record.uid, name)
    return index


def _name_index(principals) -> dict[str, str]:
    """已审批主体里的 ``linux_user ⇒ principal``。精确匹配，无别名。"""
    index: dict[str, str] = {}
    for name in principals.approved_principals():
        record = principals.lookup(name)
        if record is not None and record.linux_user:
            index.setdefault(record.linux_user, name)
    return index


def resolve_grantee(token: object, *, principals, owner_principal: str) -> str:
    """一个用户输入 ⇒ 受信主体名。解析不出来就抛 400。"""
    if not isinstance(token, str) or not token.strip():
        raise registry_errors.InvalidRequest("each principal must be a non-empty string")
    raw = token.strip()

    # 1. 已经是主体名：目录里能查到就直接用（存在性仍由目录判定）。
    if principals.lookup(raw) is not None:
        principal = raw
    # 2. 纯数字：当作 Linux UID，在已审批主体里找。
    elif raw.isascii() and raw.isdigit():
        found = _uid_index(principals).get(int(raw))
        if found is None:
            raise registry_errors.InvalidRequest(
                f"uid {raw} does not match any approved principal"
            )
        principal = found
    # 3. Linux 账号名：精确匹配，不折叠大小写、不去前缀后缀、无别名。
    else:
        found = _name_index(principals).get(raw)
        if found is None:
            raise registry_errors.InvalidRequest(
                f"unknown principal or linux account: {raw!r}"
            )
        principal = found

    record = principals.lookup(principal)
    if record is None:  # pragma: no cover - 上面三条都查过表
        raise registry_errors.InvalidRequest(f"unknown principal {principal!r}")
    if not record.approved:
        raise registry_errors.InvalidRequest(f"principal {principal!r} is not approved")
    if principal == owner_principal:
        # 给自己授权是一条永不生效的规则；留着只会让 ACL 看起来比实际更大。
        raise registry_errors.InvalidRequest(
            "the owner is always able to see their own endpoint"
        )
    return principal


def resolve_grantees(tokens: object, *, principals, owner_principal: str) -> tuple[str, ...]:
    """批量解析并去重排序。"""
    if tokens is None:
        return ()
    if not isinstance(tokens, (list, tuple)):
        raise registry_errors.InvalidRequest("principals must be a list")
    resolved = {
        resolve_grantee(token, principals=principals, owner_principal=owner_principal)
        for token in tokens
    }
    return tuple(sorted(resolved))


__all__ = [
    "MODE_WIDTH",
    "PORTAL_TO_REGISTRY_MODE",
    "PRIVATE_SHARE",
    "REGISTRY_TO_PORTAL_MODE",
    "endpoint_share_block",
    "narrowest_share",
    "portal_to_registry_mode",
    "registry_to_portal_mode",
    "resolve_grantee",
    "resolve_grantees",
]
