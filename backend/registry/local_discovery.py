"""本机发现：把「可能可以登记」的推理服务与模型目录列成**候选**。

发现只产出候选，不登记、不认领、不共享：用户（网页）或 Agent（CLI）看了候选后，
走与手动登记**同一个** ``register_engine`` / ``register_model``，所有权与唯一性
规则照旧由 Registry 判定。所以这里没有任何写入路径。

两类来源，都只读：

* **推理服务**：``/proc/net/tcp(6)`` 里在回环（或通配）地址上 LISTEN、且端口在
  Registry 允许列表内的 socket（全员可读，不读任何 ``/proc/<pid>``）。对每个端口
  只做两次短超时的只读 GET：``/v1/models``（拿模型 id）与一个框架指纹路径。
  监听 uid 反查用户名，作为「运行用户」展示。
* **模型目录**：在管理员配置的根目录（``--model-root``）里找；没有配置时用
  :data:`DEFAULT_MODEL_ROOTS` 这几个约定位置（``/data``、``/srv``、``$HOME``），
  不是全盘扫描。深度与条目数都有上限，不跟随软链出根，噪声目录
  （``node_modules`` 之类）不进去。识别 Hugging Face 目录（``config.json`` + 权重
  文件）与 GGUF 文件（切分的分片归成一个模型）；只读 ``config.json``（大小有上限），
  权重文件**只看名字与大小**，从不打开。

不会做的事：扫描全盘、探测非回环地址、探测允许列表外的端口、读取别人的进程、
自动登记或自动共享。
"""

from __future__ import annotations

import http.client
import glob
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
#: 条目预算。实测（本机、已剪掉 node_modules 等噪声目录）：整个 ``$HOME``
#: 深度 4 是 17 万条目、0.26 秒。两万太小——它会在一个无关的项目目录里用完，
#: 于是一个真实存在的模型「没有被发现」。
MAX_WALK_ENTRIES = 200_000

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")

#: 认识的推理引擎：``(id, 显示名, 可执行文件名, python 分发包名)``。
#: 只列**能自己起服务**的那几个；``transformers`` 这类库不算引擎。
KNOWN_ENGINES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("vllm", "vLLM", ("vllm",), ("vllm",)),
    ("sglang", "SGLang", ("sglang",), ("sglang",)),
    ("llama.cpp", "llama.cpp", ("llama-server", "llama-cli"), ("llama_cpp_python", "llama-cpp-python")),
    ("ollama", "Ollama", ("ollama",), ("ollama",)),
    ("tensorrt-llm", "TensorRT-LLM", ("trtllm-serve",), ("tensorrt_llm",)),
    ("tgi", "Text Generation Inference", ("text-generation-launcher",), ("text_generation",)),
    ("mlx", "MLX", ("mlx_lm.server",), ("mlx_lm", "mlx-lm")),
    ("strata", "Strata", ("strata", "strata-server"), ("strata",)),
)

#: 认出「这是一份引擎的源码/构建树」的文件。只用来判断目录，不解析内容。
TREE_MARKERS = ("CMakeLists.txt", "pyproject.toml", "setup.py", "Makefile", "go.mod")

#: 除了 ``PATH``，还要看一眼的地方（真实部署很少把引擎放进服务账户的 PATH）。
EXTRA_BIN_GLOBS = (
    "/usr/local/bin",
    "/opt/*/bin",
    "/opt/*/venv/bin",
    "~/.local/bin",
    "~/miniconda3/bin",
    "~/anaconda3/bin",
)

#: 没有显式配置 ``--model-root`` 时自动查找的位置。
#:
#: 「自动」不等于「全盘」：全盘要跨 1.9 TiB、跨用户目录、可能落在网络存储上，
#: 一次请求要跑几分钟，还会把与模型无关的东西全读一遍。这里只覆盖模型通常被放在
#: 哪儿的约定位置，管理员可以用 ``--model-root`` 换成别处。
DEFAULT_MODEL_ROOTS = ("/data", "/srv", "~")

#: 这些目录名下面不可能有模型，却能让有界遍历的条目预算瞬间用光
#: （一个 ``node_modules`` 就能吃掉两万个条目，``$HOME`` 于是「什么都没找到」）。
#: 跳过它们不是排除用户文件，是把预算花在可能的地方。
NOISY_DIR_NAMES = frozenset(
    {
        "node_modules",
        "site-packages",
        "dist-packages",
        "__pycache__",
        ".git",
        ".svn",
        ".hg",
        ".npm",
        ".cache",
        ".mozilla",
        ".venv",
        "venv",
    }
)

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
class InstalledEngineCandidate:
    """磁盘上**装着的**推理引擎——与「正在监听的进程」是两件事。

    一个引擎装好了但今天没启动，它仍然说明「这台机器能跑什么」。反过来，一个服务
    在监听也不说明装了哪个引擎（可能是别人起的、也可能是容器）。
    """

    engine: str
    name: str
    found_as: str
    version: str | None
    path: str
    env: str | None

    def to_dict(self) -> dict:
        return {
            "kind": "installed_engine",
            "engine": self.engine,
            "name": self.name,
            "found_as": self.found_as,
            "version": self.version,
            "path": self.path,
            "env": self.env,
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
    #: ``None`` = 用 :data:`DEFAULT_MODEL_ROOTS` 自动查找；空元组 = 明确不找。
    model_roots: tuple[str, ...] | None = None
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

    def effective_model_roots(self) -> tuple[str, ...]:
        """实际要查的根目录；页面上的「已查找：…」就是它。"""

        if self.model_roots is not None:
            return self.model_roots
        return tuple(str(Path(root).expanduser()) for root in DEFAULT_MODEL_ROOTS)

    def models(self, *, registered_paths: dict[str, bool]) -> tuple[list[ModelCandidate], list[str]]:
        """``registered_paths``：``权重路径 -> 是否本人登记``，由 Registry 给。

        第二个返回值是**问题清单**（完整的 ``problem`` 串），不是根目录清单：
        「这个目录不存在」和「这个目录有，但条目预算在找到之前就用完了」是两件
        不同的事，都出现在页面上，但排查方向不一样。
        """
        found: list[ModelCandidate] = []
        problems: list[str] = []
        for root in self.effective_model_roots():
            base = Path(root)
            try:
                resolved_root = base.resolve(strict=True)
            except (OSError, RuntimeError):
                problems.append(f"model_root_unavailable:{root}")
                continue
            if not resolved_root.is_dir():
                problems.append(f"model_root_unavailable:{root}")
                continue
            flags: dict[str, bool] = {}
            for candidate in _walk_models(resolved_root, flags=flags):
                if len(found) >= MAX_MODEL_CANDIDATES:
                    problems.append(f"model_candidates_capped:{MAX_MODEL_CANDIDATES}")
                    return found, problems
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
            if flags.get("truncated"):
                problems.append(f"model_walk_truncated:{root}")
        return found, problems

    # --- 磁盘上的引擎 --------------------------------------------------------

    def installed_engines(self) -> tuple[list[InstalledEngineCandidate], list[str]]:
        """装了哪些推理引擎——**不启动任何东西**，也不执行找到的程序。

        版本号只从两处读：``*.dist-info`` 目录名（pip 自己写的），和运行中的
        解释器（``importlib.metadata``）。至于可执行文件的版本，那得跑一次
        ``--version``——发现循环不执行它找到的东西，这条边界比一个版本号值钱。
        """

        found: list[InstalledEngineCandidate] = []
        seen: set[tuple[str, str]] = set()

        def add(engine: str, name: str, found_as: str, version: str | None, path: str, env: str | None) -> None:
            key = (engine, path)
            if key in seen:
                return
            seen.add(key)
            found.append(
                InstalledEngineCandidate(
                    engine=engine,
                    name=name,
                    found_as=found_as,
                    version=version,
                    path=path,
                    env=env,
                )
            )

        # 1) 可执行文件：PATH 与几个约定 bin 目录。
        for directory in self._executable_dirs():
            try:
                entries = sorted(os.listdir(directory))
            except OSError:
                continue
            present = set(entries)
            for engine, name, executables, _packages in KNOWN_ENGINES:
                for executable in executables:
                    if executable not in present:
                        continue
                    path = directory / executable
                    if not path.is_file() or not os.access(path, os.X_OK):
                        continue
                    add(engine, name, "executable", None, str(path), None)

        # 2) 模型根目录下的虚拟环境：bin/ 里的可执行文件 + site-packages 里的分发包。
        problems: list[str] = []
        for root in self.effective_model_roots():
            base = Path(root)
            try:
                resolved_root = base.resolve(strict=True)
            except (OSError, RuntimeError):
                continue  # 根目录不存在这件事已经由 models() 报过，不重复报
            flags: dict[str, bool] = {}
            for env_dir in _walk_envs(resolved_root, flags=flags):
                self._inspect_env(env_dir, add)
            if flags.get("truncated"):
                problems.append(f"env_walk_truncated:{root}")

        # 3) 源码/构建树：``/opt/strata`` 这种「装在这儿但没进 PATH」的引擎。
        for tree in self._engine_trees():
            for engine, name, _executables, _packages in KNOWN_ENGINES:
                if tree.name.lower() != engine:
                    continue
                add(engine, name, "source_tree", None, str(tree), None)

        # 4) 本服务自己的解释器里装了什么。
        try:
            import importlib.metadata as metadata

            for engine, name, _executables, packages in KNOWN_ENGINES:
                for package in packages:
                    try:
                        version = metadata.version(package)
                    except metadata.PackageNotFoundError:
                        continue
                    add(engine, name, "python_package", version, f"python:{package}", "service")
                    break
        except Exception:  # 元数据坏了不该让整页发现失败
            pass

        found.sort(key=lambda item: (item.engine, item.path))
        return found, problems

    def _engine_trees(self) -> list[Path]:
        """按目录名找引擎的源码/构建树：``/opt/strata``、``/opt/vllm`` 这类。

        只认「目录名就是引擎名、而且里面确实有构建入口」的目录。名字对但空着的
        目录不算——一个叫 ``vllm`` 的空文件夹不是引擎。
        """

        bases = [Path("/opt"), Path("/usr/local")]
        bases.extend(Path(root).expanduser() for root in self.effective_model_roots())
        trees: list[Path] = []
        seen: set[str] = set()
        known = {engine for engine, *_ in KNOWN_ENGINES}
        for base in bases:
            try:
                children = sorted(os.scandir(base), key=lambda e: e.name)
            except OSError:
                continue
            for entry in children:
                try:
                    if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name.lower() not in known:
                    continue
                if str(entry.path) in seen:
                    continue
                try:
                    names = set(os.listdir(entry.path))
                except OSError:
                    continue
                if not names.intersection(TREE_MARKERS):
                    continue
                seen.add(str(entry.path))
                trees.append(Path(entry.path))
        return trees

    def _executable_dirs(self) -> list[Path]:
        candidates = [Path(raw) for raw in os.environ.get("PATH", "").split(os.pathsep) if raw]
        for pattern in EXTRA_BIN_GLOBS:
            candidates.extend(Path(match) for match in glob.glob(os.path.expanduser(pattern)))
        dirs: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            text = str(path)
            if text not in seen:
                seen.add(text)
                dirs.append(path)
        return dirs

    def _inspect_env(self, env_dir: Path, add) -> None:
        """看一个虚拟环境：``bin/`` 里的引擎可执行文件，``site-packages`` 里的分发包。

        不执行这个环境里的 python，也不 ``import`` 任何东西——读目录名就够了。
        """

        for bin_name in ("bin", "Scripts"):
            bin_dir = env_dir / bin_name
            if not bin_dir.is_dir():
                continue
            for engine, name, executables, _packages in KNOWN_ENGINES:
                for executable in executables:
                    candidate = bin_dir / executable
                    if candidate.is_file():
                        add(engine, name, "executable", None, str(candidate), str(env_dir))

        for site_dir in sorted(env_dir.glob("lib/python*/site-packages")) + sorted(
            env_dir.glob("lib64/python*/site-packages")
        ):
            try:
                entries = sorted(os.listdir(site_dir))
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith(".dist-info"):
                    continue
                stem = entry[: -len(".dist-info")]
                package, _, version = stem.partition("-")
                for engine, name, _executables, packages in KNOWN_ENGINES:
                    if package not in packages:
                        continue
                    add(
                        engine,
                        name,
                        "python_package",
                        version or None,
                        str(site_dir / entry),
                        str(env_dir),
                    )


def _serves_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "0.0.0.0", "::", "::1", "::ffff:127.0.0.1") or ip.startswith("127.")


def _walk_models(root: Path, flags: dict[str, bool] | None = None) -> Iterable[dict]:
    """有界遍历：不跟随软链，不出根，深度与条目数有上限。

    ``flags["truncated"]`` 在条目预算用完时置位：**静默**截断是最坏的一种失败——
    页面上只会显示「没有发现模型」，而管理员无从知道是磁盘上没有、还是没扫到。
    """
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
                if flags is not None:
                    flags["truncated"] = True
                return
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in NOISY_DIR_NAMES:
                        continue
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


def _walk_envs(root: Path, flags: dict[str, bool] | None = None) -> Iterable[Path]:
    """在根目录下找虚拟环境（有 ``pyvenv.cfg`` 的目录），找到就不再往下走。

    与模型遍历同一套边界：不跟随软链、不出根、深度与条目数有上限、跳过噪声目录。
    进到一个环境之后**不深入** ``site-packages`` —— 那里装着几千个与引擎无关的包，
    只会把预算吃光；:meth:`LocalDiscovery._inspect_env` 只读它的目录名。
    """

    seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        files: set[str] = set()
        subdirs: list[Path] = []
        is_env = False
        for entry in entries:
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                if flags is not None:
                    flags["truncated"] = True
                return
            if entry.name.startswith(".") and entry.name != ".venv":
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    # 虚拟环境本身就是要找的东西，别当成噪声跳过去。
                    if entry.name in NOISY_DIR_NAMES and entry.name not in (".venv", "venv"):
                        continue
                    subdirs.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.add(entry.name)
            except OSError:
                continue
        if "pyvenv.cfg" in files:
            yield directory
            is_env = True
        if not is_env and depth < MAX_WALK_DEPTH:
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
