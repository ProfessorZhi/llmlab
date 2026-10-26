"""把代理上报里的「装着哪些引擎」落成 ``runtime_builds`` 行，所有者是**本人的平台账户**。

这是引擎库与模型库的**同一条路**：代理在自己的身份下扫一遍磁盘，服务端把扫到的
东西记在**这个人**名下（``account:<id>``，``owner_uid = -1``），于是「本机装了
llama.cpp」和「引擎库里有 llama.cpp」是同一个事实的两种说法，而不是两句话。

与 :mod:`reported_models` 的三处差异，都来自 build 与模型是两种东西：

1. **去重键是路径，不是名字。** 一个 build 就是磁盘上的一个文件；同名同路径的
   再次上报是同一个 build，换了版本的同一个路径**也**是同一个 build（旧的那份
   已经不存在了）。所以键是 ``(framework, binary_path)``，版本只是被刷新的观测值。
2. **观测值会被刷新。** 模型的名称、说明可以被人改，所以那里是「只补空、不覆盖」；
   build 的每一列都是机器看见的，最新一轮扫描就是最新事实。但刷新只会**补上**
   非空值 —— 可执行文件的版本号探测不到（扫描器不执行它找到的程序），一轮
   ``version=None`` 不该把上一轮读到的版本抹掉。
3. **不标 missing。** ``mark_build_missing`` 存在，但这条路径上不用它：build 属于
   **账户**，而一个账户下的 linux 用户可能从不止一台机器/不止一个 agent 上报
   （``MAX_AGENTS_PER_PRINCIPAL``）。甲机器这一轮没报某个 build，不证明它被卸了。
   过期的判断交给 ``last_seen_at``：页面照实说「最后一次见到」，而不是替它说「没了」。

``state`` 因此在写入时永远是 ``present``：它表示「这一行对应一个此刻还在的上报」，
而不是「这时它还在磁盘上」（后者需要每轮都看得到，而我们只知道上一次）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
import json
from typing import Iterable

from .models import RUNTIME_BUILD_PRESENT, RuntimeBuild
from .reported_models import ACCOUNT_OWNER_UID
from .store import Store

#: 代理认出引擎的三种方式。与 ``agent_reports._FOUND_AS`` 是同一组值；这里再列
#: 一遍是因为本模块自己也要挡住形状不对的条目（它不该信任上游一定洗过）。
KNOWN_FOUND_AS = frozenset({"executable", "python_package", "source_tree"})


def build_fingerprint(framework: str, binary_path: str, version: str | None) -> str:
    """一份 build 的身份：``framework + binary_path + version``。

    代理给不出 inode / mtime（扫描器只读目录），所以二进制「换了没换」在平台这
    一侧只能靠路径与版本判断。算法与 :mod:`reported_models` 的同形：排序后的
    JSON，前 32 位。
    """

    return sha256(
        json.dumps(
            {
                "kind": "runtime_build",
                "framework": framework,
                "binary_path": binary_path,
                "version": version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]


@dataclass(frozen=True)
class BuildAdoption:
    """一次自动入账的结果：可执行文件路径到行的映射，以及这次**新建**了哪几行。"""

    #: ``{可执行文件路径: build id}``，只覆盖这次见过的路径。
    adopted: dict[str, str] = field(default_factory=dict)
    #: 这次真正写入的新行 id。非空意味着「值得记一条审计」。
    created: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.adopted)


def adopt_reported_builds(
    store: Store,
    *,
    owner: str,
    builds: Iterable[dict],
    now: datetime,
) -> BuildAdoption:
    """把一份上报里的 ``installed_engines`` 落成 ``runtime_builds`` 行。

    单条条目形状不对只跳过这一条：一次扫描里的一条怪路径不该让整个引擎库丢一轮。
    """

    adopted: dict[str, str] = {}
    created: list[str] = []
    if not owner:
        return BuildAdoption()

    by_path: dict[tuple[str, str], RuntimeBuild] = {
        (row.framework, row.binary_path): row for row in store.builds_for_owner(owner)
    }

    for entry in builds:
        if not isinstance(entry, dict):
            continue
        framework = entry.get("engine")
        path = entry.get("path")
        if (
            not isinstance(framework, str)
            or not framework
            or not isinstance(path, str)
            or not path.startswith("/")
            or "\x00" in path
            or path in adopted
        ):
            continue

        version = _text(entry.get("version"))
        found_as = entry.get("found_as")
        found_as = found_as if found_as in KNOWN_FOUND_AS else None
        env = _text(entry.get("env"))
        env = env if env is not None and env.startswith("/") else None
        fingerprint = build_fingerprint(framework, path, version)

        existing = by_path.get((framework, path))
        if existing is not None:
            adopted[path] = existing.id
            refreshed = replace(
                existing,
                # 补上，不清空：这一轮没读到版本，不代表上一轮读到的版本是假的。
                version=version if version is not None else existing.version,
                found_as=found_as if found_as is not None else existing.found_as,
                env=env if env is not None else existing.env,
                fingerprint=fingerprint,
                state=RUNTIME_BUILD_PRESENT,
                last_seen_at=now,
            )
            if refreshed != existing:
                store.upsert_build(refreshed)
                by_path[(framework, path)] = refreshed
            continue

        build = RuntimeBuild(
            id=_new_build_id(),
            owner_principal=owner,
            owner_uid=ACCOUNT_OWNER_UID,
            framework=framework,
            binary_path=path,
            version=version,
            fingerprint=fingerprint,
            flags={},
            formats=(),
            found_as=found_as,
            env=env,
            state=RUNTIME_BUILD_PRESENT,
            last_seen_at=now,
        )
        store.upsert_build(build)
        adopted[path] = build.id
        created.append(build.id)
        by_path[(framework, path)] = build
    return BuildAdoption(adopted=adopted, created=tuple(created))


def _text(raw: object) -> str | None:
    """非空字符串，否则 ``None``。数字/布尔都不算版本号。"""

    if isinstance(raw, str):
        return raw or None
    return None


def _new_build_id() -> str:
    """与登记同形的 id（``bld_<16 hex>``）。延迟导入见 ``reported_models``。"""

    from .registry import new_id

    return new_id("bld")


__all__ = [
    "BuildAdoption",
    "KNOWN_FOUND_AS",
    "adopt_reported_builds",
    "build_fingerprint",
]
