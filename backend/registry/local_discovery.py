"""本机发现：把「可能可以登记」的推理服务与模型目录列成**候选**。

发现只产出候选，不登记、不认领、不共享：用户（网页）或 Agent（CLI）看了候选后，
走与手动登记**同一个** ``register_engine`` / ``register_model``，所有权与唯一性
规则照旧由 Registry 判定。所以这里没有任何写入路径。

两类来源，都只读：

* **推理服务**：``/proc/net/tcp(6)`` 里在回环（或通配）地址上 LISTEN、且端口在
  Registry 允许列表内的 socket（全员可读，不读任何 ``/proc/<pid>``）。对每个端口
  只做两次短超时的只读 GET：``/v1/models``（拿模型 id）与一个框架指纹路径。
  监听 uid 反查用户名，作为「运行用户」展示。
* **模型目录**：只在管理员配置的根目录（``--model-root``）里找，深度与条目数都有
  上限，不跟随软链出根。识别 Hugging Face 目录（``config.json`` + 权重文件）与
  GGUF 文件；只读 ``config.json``（大小有上限），权重文件**只看名字与大小**，
  从不打开。

不会做的事：扫描全盘、探测非回环地址、探测允许列表外的端口、读取别人的进程、
自动登记或自动共享。
"""

from __future__ import annotations

import http.client
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .ownership import (
    ListeningSocket,
    ProcNetListenerTable,
    ProcNetUnavailable,
    ProcNetUnparseable,
    linux_user_for_uid,
)

#: 一次 GET 的上限。候选只需要一个模型清单与一个版本号。
MAX_BODY_BYTES = 256 * 1024
MAX_MODEL_IDS = 20
MAX_CONFIG_BYTES = 256 * 1024
MAX_MODEL_CANDIDATES = 200
MAX_WALK_DEPTH = 4
MAX_WALK_ENTRIES = 20000

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")

#: 切分 GGUF 的命名约定：``<name>-00001-of-00002.gguf``。
_SHARD = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})$", re.IGNORECASE)
#: 多模态投影文件的前缀：与模型一起用，但本身不是一个可加载的模型。
MMPROJ_PREFIX = "mmproj"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")

#: 框架指纹：(路径, 必须出现的字段, 框架名)。只读、小响应；拿不准就是 ``None``。
#: 只认「这个框架才会给」的字段：一个对任何路径都回 JSON 的服务不会被误认成某个框架。
FRAMEWORK_PROBES: tuple[tuple[str, frozenset[str], str], ...] = (
    ("/get_server_info", frozenset({"tp_size", "model_path"}), "sglang"),
    ("/version", frozenset({"version"}), "vllm"),
)

Fetch = Callable[[int, str], "tuple[int, bytes] | None"]


def loopback_fetch(timeout_seconds: float = 1.0) -> Fetch:
    """缺省的只读 GET：只连 127.0.0.1，不跟随重定向，响应体有上限。"""

    def fetch(port: int, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_seconds)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            return response.status, response.read(MAX_BODY_BYTES)
        except (OSError, http.client.HTTPException):
            return None
        finally:
            connection.close()

    return fetch


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_model_ids(body: bytes) -> list[str] | None:
    """OpenAI 兼容的 ``/v1/models``（``{"data":[{"id":...}]}``）⇒ 安全的 id 列表。"""
    payload = _json(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    ids = []
    for item in payload["data"]:
        model_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(model_id, str) and _SAFE_ID.match(model_id):
            ids.append(model_id)
        if len(ids) >= MAX_MODEL_IDS:
            break
    return ids


def _framework(fetch: Fetch, port: int) -> str | None:
    for path, markers, name in FRAMEWORK_PROBES:
        answer = fetch(port, path)
        if answer is None:
            continue
        status, body = answer
        payload = _json(body) if 200 <= status < 300 else None
        if isinstance(payload, dict) and markers <= payload.keys():
            return name
    return None


@dataclass(frozen=True)
class EngineCandidate:
    port: int
    target_url: str
    run_as_uid: int | None
    run_as_user: str | None
    openai_compatible: bool
    models: tuple[str, ...]
    framework: str | None
    registered: bool
    registered_by_me: bool
    suggested_name: str | None

    def to_dict(self) -> dict:
        return {
            "kind": "engine",
            "port": self.port,
            "target_url": self.target_url,
            "run_as_uid": self.run_as_uid,
            "run_as_user": self.run_as_user,
            "openai_compatible": self.openai_compatible,
            "models": list(self.models),
            "framework": self.framework,
            "registered": self.registered,
            "registered_by_me": self.registered_by_me,
            "suggested_name": self.suggested_name,
        }


@dataclass(frozen=True)
class ModelCandidate:
    path: str
    root: str
    format: str
    size_bytes: int
    architecture: str | None
    model_type: str | None
    registered: bool
    suggested_name: str
    registered_by_me: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": "model",
            "registered_by_me": self.registered_by_me,
            "path": self.path,
            "root": self.root,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "architecture": self.architecture,
            "model_type": self.model_type,
            "registered": self.registered,
            "suggested_name": self.suggested_name,
        }


def suggest_name(raw: str | None) -> str | None:
    """模型 id / 目录名 ⇒ 一个满足 Registry 命名规则的建议名（拿不准就是 ``None``）。"""
    if not raw:
        return None
    tail = raw.rstrip("/").split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-._")
    return cleaned[:128] or None


@dataclass
class LocalDiscovery:
    """可注入的本机发现器。生产缺省读真表、连真回环；测试注入 fixture。"""

    allowed_ports: Iterable[int]
    model_roots: tuple[str, ...] = ()
    table: ProcNetListenerTable = field(default_factory=ProcNetListenerTable)
    fetch: Fetch = field(default_factory=loopback_fetch)
    uid_namer: Callable[[int], str | None] = linux_user_for_uid

    # --- 推理服务 ------------------------------------------------------------

    def _listeners(self) -> tuple[list[ListeningSocket], str | None]:
        try:
            return self.table.listeners(), None
        except ProcNetUnavailable:
            return [], "proc_net_unreadable"
        except ProcNetUnparseable:
            return [], "proc_net_unparseable"

    def engines(self, *, registered_targets: dict[tuple[str, int], bool]) -> tuple[list[EngineCandidate], str | None]:
        """``registered_targets``：``(host, port) -> 是否本人登记``，由 Registry 给。"""
        sockets, problem = self._listeners()
        allowed = set(self.allowed_ports)
        by_port: dict[int, set[int]] = {}
        for item in sockets:
            if item.port not in allowed:
                continue
            if not _serves_loopback(item.ip):
                continue
            by_port.setdefault(item.port, set()).add(item.uid)

        found = []
        for port in sorted(by_port):
            uids = by_port[port]
            uid = next(iter(uids)) if len(uids) == 1 else None
            answer = self.fetch(port, "/v1/models")
            models = None
            if answer is not None and 200 <= answer[0] < 300:
                models = parse_model_ids(answer[1])
            mine = registered_targets.get(("127.0.0.1", port))
            found.append(
                EngineCandidate(
                    port=port,
                    target_url=f"http://127.0.0.1:{port}",
                    run_as_uid=uid,
                    run_as_user=self.uid_namer(uid) if uid is not None else None,
                    openai_compatible=models is not None,
                    models=tuple(models or ()),
                    framework=_framework(self.fetch, port) if models is not None else None,
                    registered=mine is not None,
                    registered_by_me=bool(mine),
                    suggested_name=suggest_name(models[0]) if models else None,
                )
            )
        return found, problem

    # --- 模型目录 ------------------------------------------------------------

    def models(self, *, registered_paths: dict[str, bool]) -> tuple[list[ModelCandidate], list[str]]:
        """``registered_paths``：``权重路径 -> 是否本人登记``，由 Registry 给。"""
        found: list[ModelCandidate] = []
        unavailable: list[str] = []
        for root in self.model_roots:
            base = Path(root)
            try:
                resolved_root = base.resolve(strict=True)
            except (OSError, RuntimeError):
                unavailable.append(root)
                continue
            if not resolved_root.is_dir():
                unavailable.append(root)
                continue
            for candidate in _walk_models(resolved_root):
                if len(found) >= MAX_MODEL_CANDIDATES:
                    return found, unavailable
                # 分片模型有一组路径；只要其中任意一片被登记过，就算这个模型已登记。
                paths = candidate.get("paths") or [candidate["path"]]
                found.append(
                    ModelCandidate(
                        path=candidate["path"],
                        root=str(resolved_root),
                        format=candidate["format"],
                        size_bytes=candidate["size"],
                        architecture=candidate.get("architecture"),
                        model_type=candidate.get("model_type"),
                        registered=any(p in registered_paths for p in paths),
                        registered_by_me=any(registered_paths.get(p) for p in paths),
                        suggested_name=suggest_name(
                            candidate.get("label")
                            or (Path(candidate["path"]).stem if candidate["format"] == "gguf" else candidate["path"])
                        )
                        or "model",
                    )
                )
        return found, unavailable


def _serves_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "0.0.0.0", "::", "::1", "::ffff:127.0.0.1") or ip.startswith("127.")


def _walk_models(root: Path) -> Iterable[dict]:
    """有界遍历：不跟随软链，不出根，深度与条目数有上限。"""
    seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        files = {}
        subdirs = []
        for entry in entries:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                return
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files[entry.name] = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue

        weights = {name: size for name, size in files.items() if name.endswith(WEIGHT_SUFFIXES)}
        if "config.json" in files and weights:
            info = _read_config(directory / "config.json", files["config.json"])
            yield {
                "path": str(directory),
                "format": "hf",
                "size": sum(weights.values()),
                **info,
            }
            continue  # 一个模型目录里的子目录不再当作独立模型
        singles: list[tuple[str, int]] = []
        shards: dict[str, list[tuple[int, str, int]]] = {}
        for name, size in files.items():
            if not name.endswith(".gguf"):
                continue
            label = name[: -len(".gguf")]
            match = _SHARD.match(label)
            if match is not None:
                shards.setdefault(match.group("stem"), []).append(
                    (int(match.group("index")), name, size)
                )
            elif label.lower().startswith(MMPROJ_PREFIX):
                # ``mmproj-*.gguf`` 是多模态投影，不能单独加载，也不会被单独登记成模型。
                continue
            else:
                singles.append((label, name, size))

        for label, name, size in singles:
            yield {"path": str(directory / name), "format": "gguf", "size": size, "label": label}

        for stem, parts in shards.items():
            # 一个切分的模型是一个模型：``…-00001-of-00002`` 与 ``…-00002-of-00002``
            # 若各自作为候选出现，用户会「登记」半个权重文件，而界面还会显示成两个模型。
            parts.sort()
            yield {
                "path": str(directory / parts[0][1]),
                "paths": [str(directory / name) for _index, name, _size in parts],
                "format": "gguf",
                # 只是已存在的片之和；缺片时这里会偏小 —— 发现不声称「这个模型完整」。
                "size": sum(size for _index, _name, size in parts),
                "label": stem,
            }
        if depth < MAX_WALK_DEPTH:
            stack.extend((path, depth + 1) for path in reversed(subdirs))


def _read_config(path: Path, size: int) -> dict:
    if size > MAX_CONFIG_BYTES:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    arch = payload.get("architectures")
    model_type = payload.get("model_type")
    return {
        "architecture": arch[0] if isinstance(arch, list) and arch and isinstance(arch[0], str) else None,
        "model_type": model_type if isinstance(model_type, str) else None,
    }


__all__ = [
    "EngineCandidate",
    "LocalDiscovery",
    "ModelCandidate",
    "loopback_fetch",
    "parse_model_ids",
    "suggest_name",
]
