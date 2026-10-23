"""把 §4.1 观测到的回环服务映射成账本里的 ``engines`` 行。

自动接入与「用户登记」走的是同一张表，但**不是同一条写入路径**：登记要过所有权
证明与 scope 检查，观测不需要——内核已经告诉我们这个端口的属主 uid。两者最终
写出的行用 ``source`` 区分（``auto`` vs ``client_registered``），审计时一眼可辨。

服务身份用 ``(uid, port)``，所以 id 是确定性的 ``auto:<uid>:<port>``：进程重启
换了 pid 也不会在账本里多出一行，下一轮观测直接更新同一行。
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from .models import (
    ENGINE_MODE_USER,
    ENGINE_RUNTIME_ATTACHED,
    ENGINE_RUNTIME_OFFLINE,
    SOURCE_AUTO,
    Engine,
    isoformat,
)
from .store import Store

AUTO_ID_PREFIX = "auto:"


def auto_engine_id(uid: int, port: int) -> str:
    return f"{AUTO_ID_PREFIX}{int(uid)}:{int(port)}"


def is_auto_engine(engine_id: str) -> bool:
    return engine_id.startswith(AUTO_ID_PREFIX)


def _target(port: int) -> dict:
    """与 ``NormalizedTarget.to_dict()`` 同形。

    不走校验器是因为这里的值**不是**用户输入：端口来自内核表，主机固定是回环。
    形状保持一致，读的一方（健康探测、路由配置）才不需要分支。
    """

    return {
        "scheme": "http",
        "host": "127.0.0.1",
        "port": int(port),
        "path": "",
        "query": "",
        "url": f"http://127.0.0.1:{int(port)}",
        "operator_blessed": False,
        "operator_label": None,
    }


def _unique_name(store: Store, principal: str, wanted: str, *, port: int) -> str:
    """``engines`` 在 (owner, name) 上有唯一索引：自动名字撞了手工登记的名字时让路。"""

    if store.engine_by_owner_name(principal, wanted) is None:
        return wanted
    candidate = f"{wanted}-{port}"
    suffix = 2
    while store.engine_by_owner_name(principal, candidate) is not None:
        candidate = f"{wanted}-{port}-{suffix}"
        suffix += 1
    return candidate


def attach_observed(
    store: Store,
    *,
    uid: int,
    principal: str,
    port: int,
    framework: str | None,
    model_ids: tuple[str, ...],
    visibility: str,
    now: datetime,
) -> Engine:
    """把（或更新）一个被观测到的服务写进账本，返回账本里的那一行。

    ``visibility`` 在**接入的这一刻**快照进 ``launch_json``：§4.2 说默认值只影响
    之后接入的服务，所以不能在将来建路由时再去读一次当时的最新默认值。
    """

    engine_id = auto_engine_id(uid, port)
    launch = {
        "model_ids": list(model_ids),
        "framework": framework,
        # 快照：默认可见性在接入时是什么，这个服务就按什么处理。
        "visibility": visibility,
    }

    existing = store.engine_by_id(engine_id)
    if existing is not None:
        # 守护进程重启后 tracked 是空的，已接入的服务会再走一次 ATTACH。这时
        # 模型名/框架可能确实变了（同一个端口换了服务），但**可见性不能重取**：
        # 那会让一次重启把旧服务的快照改写成今天的管理员默认值，正是 §4.2
        # 要防的「同时应用到已有服务」。原快照有值就沿用。
        previous = existing.launch if isinstance(existing.launch, dict) else {}
        launch["visibility"] = previous.get("visibility", launch["visibility"])
        store.update_engine_runtime(
            engine_id,
            state=ENGINE_RUNTIME_ATTACHED,
            last_seen_at=now,
            launch=launch,
        )
        refreshed = store.engine_by_id(engine_id)
        assert refreshed is not None  # 刚更新过，除非并发删除
        return refreshed

    wanted = _wanted_name(model_ids, framework, port)
    name = _unique_name(store, principal, wanted, port=port)
    target = _target(port)
    fingerprint = sha256(f"{engine_id}|{target['url']}|{name}".encode()).hexdigest()

    engine = Engine(
        id=engine_id,
        owner_principal=principal,
        owner_uid=int(uid),
        name=name,
        target=target,
        protocol="openai",
        status="active",
        health={"state": "unknown", "detail": "observed by the daemon"},
        source=SOURCE_AUTO,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        mode=ENGINE_MODE_USER,
        state=ENGINE_RUNTIME_ATTACHED,
        launch=launch,
    )
    store.insert_engine(engine, idempotency_key=None, fingerprint=fingerprint)
    return engine


def mark_observed_online(store: Store, *, uid: int, port: int, now: datetime) -> None:
    """之前离线、现在回来了：只恢复状态。

    不重新走 :func:`attach_observed`，因为那次调用会把 ``launch_json`` 里的可见性
    快照覆盖成本轮的最新默认值——而 §4.2 明确默认值只影响**新接入**的服务。
    """

    engine_id = auto_engine_id(uid, port)
    if store.engine_by_id(engine_id) is None:
        return
    store.update_engine_runtime(engine_id, state=ENGINE_RUNTIME_ATTACHED, last_seen_at=now)


def mark_observed_offline(store: Store, *, uid: int, port: int, now: datetime) -> None:
    """服务不见了：只改运行时状态，**不删行**。

    路由保留、调用返回 503，用户回来时还是同一个服务对象——这正是 §4.1 表里
    ``offline`` 与「丢弃」的区别。
    """

    engine_id = auto_engine_id(uid, port)
    if store.engine_by_id(engine_id) is None:
        return
    store.update_engine_runtime(engine_id, state=ENGINE_RUNTIME_OFFLINE, last_seen_at=now)


def observed_engines(store: Store) -> list[Engine]:
    return store.engines_by_source(SOURCE_AUTO)


def _wanted_name(model_ids: tuple[str, ...], framework: str | None, port: int) -> str:
    """给用户看的名字：优先模型名，其次「框架-端口」。

    只保留 Registry 命名允许的字符——这个名字会出现在网页与 CLI 里，也会进
    唯一索引，不能带空格或斜杠。
    """

    for raw in model_ids:
        cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in "._-").strip("-._")
        if cleaned:
            return cleaned[:96]
    label = (framework or "service").replace(".", "-").replace(" ", "-")
    return f"{label}-{int(port)}"


def stamp(now: datetime) -> str:
    """便于调用方把时间写进日志/上报；格式与库里的时间戳一致。"""

    return isoformat(now)
