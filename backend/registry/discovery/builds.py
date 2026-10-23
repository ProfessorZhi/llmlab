"""§5.5：引擎 build 的版本、commit、CMake 开关与 ``--help`` 参数发现。

为什么把这些东西单独放一个模块：这里的每一步都要执行**外部二进制**，而那个
二进制不受我们控制——它可能卡住、可能根本不存在，也可能是别的用户放进来的
东西。所以所有执行都收拢在可注入的 ``Runner`` 后面：测试传假 runner 就完全
不碰真实进程，生产侧默认走 ``default_runner``，并统一把超时压在 5 s。

本模块只 import ``types.py``，不 import 其它 S1 模块；读文件也只读 ``HEAD``、
``packed-refs``、``CMakeCache.txt`` 这类小文本，不调用 git、不联网。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from backend.registry.discovery.types import (
    FRAMEWORK_FORMATS,
    BuildCandidate,
    FlagSpec,
)

__all__ = [
    "Runner",
    "default_runner",
    "probe_version",
    "read_help",
    "parse_help",
    "find_git_commit",
    "read_cmake_cache",
    "detect_framework",
    "enrich",
    "ProbeCache",
]

#: 执行一个 argv 并返回 ``(returncode, stdout, stderr)``。返回码为负表示「根本
#: 没跑起来」（超时、文件不存在、没有权限），此时 stderr 是原因而不是程序输出。
Runner = Callable[[list[str], float], tuple[int, str, str]]

#: ``--help`` 输出的截断上限。按字符而不是字节计，避免把一个多字节字符切两半。
_MAX_HELP_CHARS = 256 * 1024

#: 版本号兜底行也截断，防止某个工具把整篇 changelog 当成版本行。
_MAX_VERSION_LINE = 120

#: llama.cpp 的构建号形如 ``b6512``；用 ``\b`` 挡住 ``abc123`` 之类误命中。
_LLAMA_BUILD_RE = re.compile(r"\bb\d+\b")

#: 通用语义化版本。先找 llama.cpp 风格，找不到再退到这里。
_SEMVER_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")

_DEFAULT_RE = re.compile(r"\(default:\s*([^)]*)\)")

#: llama.cpp 的默认值后面常跟一句解释：``(default: 0, 0 = loaded from model)``。
#: 参数表只认识开头的数字，所以只取数值前缀。
_NUMBER_PREFIX_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

#: 真机 help 把候选值放在续行：``allowed values: f32, f16, ...``。
_ALLOWED_VALUES_RE = re.compile(r"allowed values:\s*([^()]+?)(?:\s*\(|$)")

#: git 对象 id。SHA-1 是 40 位，SHA-256 仓库是 64 位；7 位是缩写下限。
_HEX_RE = re.compile(r"[0-9a-fA-F]{7,64}")

#: basename → 框架。与 §5.5 的候选清单一致；``detect_framework`` 和 rules 的
#: 识别口径必须相同，否则扫描出的 build 与这里补出的 framework 会打架。
_FRAMEWORK_BY_BASENAME = {
    "llama-server": "llama.cpp",
    "strata": "strata",
    "vllm": "vllm",
    "sglang": "sglang",
    "ollama": "ollama",
}

#: llama.cpp 一行选项：可选的短选项 + **一个或多个**长别名 + 余下部分。
#: 真机输出里同一个开关常挂多个别名（``-ngl, --gpu-layers, --n-gpu-layers N``），
#: 参数表用的是其中的 ``--n-gpu-layers``，只认第一个会把核心参数判成「不支持」。
#: 短选项允许多字母（``-ngl``、``-ctk``）；没有长别名时整行不匹配，从而不建条目。
_LLAMA_OPTION_RE = re.compile(
    r"^\s*(?:-[A-Za-z0-9][A-Za-z0-9_.-]*\s*,\s*)?"
    r"(?P<names>--[A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?:\s*,\s*--[A-Za-z0-9][A-Za-z0-9_.-]*)*)(?P<rest>.*)$"
)

#: argparse 一行选项：可能是一个短选项 + 长选项，也可能是
#: ``--flag, --no-flag`` 这种布尔开关对。要求有缩进，借此放过 ``options:`` 之类
#: 的组标题。
_ARGPARSE_OPTION_RE = re.compile(
    r"^\s+(?P<names>-{1,2}[A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?:\s*,\s*-{1,2}[A-Za-z0-9][A-Za-z0-9_.-]*)*)(?P<rest>.*)$"
)

#: 一个「像 metavar」的单词：普通标识符、``{a,b}`` 或 ``[on|off]``。
_METAVAR_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_.:-]*|\{[^{}]*\}|\[[^\[\]]*\]"
)


# --- 执行 -----------------------------------------------------------------


def default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """在二进制所在目录执行 argv。

    为什么 ``cwd`` 取二进制所在目录：很多引擎会按相对路径加载同目录下的资源
    （ggml 的 dylib、vLLM 的 entrypoint 插件），在调用方 cwd 下跑会找不到。

    超时和「跑不起来」都收敛成 ``(-1, "", 原因)``：调用方只需要判断返回码，
    不必自己区分 ``TimeoutExpired`` 与 ``FileNotFoundError``。
    """
    if not argv:
        return (-1, "", "empty argv")
    cwd = Path(argv[0]).parent
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (-1, "", f"timeout after {timeout:g}s")
    except OSError as exc:
        return (-1, "", f"{type(exc).__name__}: {exc}")
    return (completed.returncode, completed.stdout or "", completed.stderr or "")


def _combine(stdout: str, stderr: str) -> str:
    """把 stdout 和 stderr 按出现顺序拼起来。

    有些工具把版本写到 stderr（尤其是启动失败时），只读 stdout 会漏。
    """
    parts = [part for part in (stdout, stderr) if part]
    return "\n".join(parts)


# --- 版本与 help -----------------------------------------------------------


def probe_version(
    binary: Path,
    *,
    argv_prefix: Sequence[str] = (),
    timeout: float = 5.0,
    runner: Runner | None = None,
) -> str | None:
    """``<binary> --version`` 的第一个版本号。

    优先 ``b<数字>``（llama.cpp 的构建号），再退到语义化版本。两者都没有时，
    只有**成功退出**的输出才值得取第一行——非零退出时的输出通常是报错信息，
    拿它当版本会把 "error: no CUDA device" 写进上报。
    """
    run = runner if runner is not None else default_runner
    returncode, stdout, stderr = run([str(binary), *argv_prefix, "--version"], timeout)
    if returncode != 0:
        return None
    text = _combine(stdout, stderr)
    match = _LLAMA_BUILD_RE.search(text) or _SEMVER_RE.search(text)
    if match:
        return match.group(0)
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:_MAX_VERSION_LINE]
    return None


def read_help(
    binary: Path,
    *,
    argv_prefix: Sequence[str] = (),
    timeout: float = 5.0,
    runner: Runner | None = None,
) -> str | None:
    """``<binary> --help`` 的原始文本（stdout + stderr，最多 256K 字符）。

    非零退出但确实有输出时仍然返回：argparse 一类的工具偶尔以 2 退出却照样
    打印 usage，丢掉它不如交给 ``parse_help`` 去忽略解析不了的行。返回码为负
    则一律返回 ``None``——那时输出是 runner 写的原因，不是 help。
    """
    run = runner if runner is not None else default_runner
    returncode, stdout, stderr = run([str(binary), *argv_prefix, "--help"], timeout)
    if returncode < 0:
        return None
    text = _combine(stdout, stderr)[:_MAX_HELP_CHARS]
    return text if text.strip() else None


# --- help 解析 -------------------------------------------------------------


def _extract_default(help_text: str) -> str | None:
    """取 ``(default: X)`` 里的 X；空值视为「帮助没给出默认值」。"""
    match = _DEFAULT_RE.search(help_text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _numeric_prefix(value: str | None) -> str | None:
    """取出 ``"0, 0 = loaded from model"`` 里的 ``"0"``。

    数字后面的解释不属于默认值，原样留着会让参数表拿它去做类型转换并失败。
    """
    if value is None:
        return None
    match = _NUMBER_PREFIX_RE.match(value.strip())
    return match.group(0) if match else None


def _choices_from_allowed_values(help_text: str) -> tuple[str, ...]:
    """从续行 ``allowed values: a, b, c`` 里取候选值。

    llama.cpp 只在部分选项上用这种写法（例如 ``--cache-type-k``），它和
    ``{a,b}`` 一样是枚举，只是排版在下一行。
    """
    match = _ALLOWED_VALUES_RE.search(help_text)
    if not match:
        return ()
    values = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    return values if len(values) >= 2 else ()


def _drop_negations(names: list[str]) -> list[str]:
    """去掉 ``--no-x``，只留同一行里出现的正向开关 ``--x``。

    ``--perf, --no-perf`` 是同一个布尔开关的两种写法；若两个都建条目，参数表
    求交时会多出一个没人认识的 ``--no-perf``。名称集合里没有对应正向开关时
    保留原样（它可能就是独立参数）。
    """
    kept = []
    for name in names:
        if name.startswith("--no-") and "--" + name[len("--no-") :] in names:
            continue
        kept.append(name)
    return kept


def _split_value_and_help(rest: str) -> tuple[str, str]:
    """把选项行余下部分切成「取值说明」和「描述」。

    两边的列之间至少两个空格，描述内部一般只有一个空格；按 ``\\s{2,}`` 切分
    正好落在列分隔处。没有 metavar 的布尔开关，描述里通常也不含双空格，
    于是整段都算描述。
    """
    stripped = rest.strip()
    if not stripped:
        return ("", "")
    parts = re.split(r"\s{2,}", stripped, maxsplit=1)
    # 只有切出来的第一段真的像 metavar 时才算取值说明；否则整行都是描述
    # （例如 "--verbose          verbose logging" 的第一段会是一句话）。
    if _looks_like_metavar(parts[0]):
        return (parts[0], parts[1].strip() if len(parts) > 1 else "")
    return ("", " ".join(parts).strip())


def _looks_like_metavar(token: str) -> bool:
    return bool(_METAVAR_RE.fullmatch(token))


def _choices_from_token(token: str) -> tuple[str, ...]:
    """从 ``{a,b}`` / ``[on|off]`` / 裸 ``on|off`` 里取候选值。

    单个值不算 choice（``[N]`` 是「可选地接受一个整数」）。
    """
    value = token.strip()
    inner = ""
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1]
    elif value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
    elif "|" in value:
        inner = value
    if "|" in inner:
        parts = inner.split("|")
    elif "," in inner:
        parts = inner.split(",")
    else:
        return ()
    choices = tuple(part.strip() for part in parts if part.strip())
    return choices if len(choices) >= 2 else ()


def _type_from_default(default: str | None) -> str | None:
    """从默认值反推类型；推不出就返回 ``None``，由调用方决定 fallback。"""
    if default is None:
        return None
    value = default.strip()
    if value.lower() in {"true", "false"}:
        return "bool"
    try:
        int(value)
    except ValueError:
        pass
    else:
        return "int"
    try:
        float(value)
    except ValueError:
        return None
    return "float"


def _llama_type(token: str, default: str | None) -> str:
    """llama.cpp 的 metavar 只有很少几种：N / INT / FLOAT / F / 其它。"""
    upper = token.strip().strip("[]").upper()
    if upper in {"FLOAT", "DOUBLE", "F"}:
        return "float"
    if upper in {"N", "INT", "INTEGER", "INTS"}:
        # llama.cpp 对同一个 N 既表示整数也表示浮点，只能看默认值是否带小数点。
        return "float" if default is not None and "." in default else "int"
    return "str"


def _parse_llama(text: str) -> tuple[FlagSpec, ...]:
    """llama.cpp 的 ``--help``。

    真机输出比早期的固定列排版复杂：一行可能挂多个长别名，描述和
    ``(default:`` / ``allowed values:`` 都会折到下一行。所以这里和 argparse
    一样维护 pending 条目，把缩进续行拼回来，再统一做类型推断。
    """
    flags: dict[str, FlagSpec] = {}
    pending: dict[str, object] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        names = pending["names"]
        assert isinstance(names, list)
        token = str(pending["token"])
        help_text = str(pending["help"]).strip()
        default = _extract_default(help_text) if help_text else None

        choices: tuple[str, ...] = ()
        if token and _looks_like_metavar(token):
            choices = _choices_from_token(token)
        if not choices:
            choices = _choices_from_allowed_values(help_text)

        if choices:
            flag_type = "choice"
        elif token:
            flag_type = _llama_type(token, default)
        else:
            flag_type = "bool"
        if default is not None and default.lower() in {"true", "false"} and not choices:
            flag_type = "bool"
        # 数值默认值只保留数字前缀；``auto`` 这类非数字原样保留。
        if flag_type in {"int", "float"} and default is not None:
            number = _numeric_prefix(default)
            if number is not None:
                default = number

        # 每个长别名各出一条；同一行内共享同一份 type/default/choices/help。
        for name in names:
            flags.setdefault(
                name,
                FlagSpec(
                    name=name,
                    type=flag_type,
                    default=default,
                    choices=choices,
                    help=help_text,
                ),
            )
        pending = None

    for raw in text.splitlines():
        if not raw.strip():
            flush()
            continue
        match = _LLAMA_OPTION_RE.match(raw)
        if match:
            flush()
            names = [name.strip() for name in match.group("names").split(",")]
            token, help_text = _split_value_and_help(match.group("rest"))
            pending = {
                "names": _drop_negations(names),
                "token": token,
                "help": help_text,
            }
            continue
        # 顶格行是新内容的分界。
        if not raw[:1].isspace():
            flush()
            continue
        # 只有短别名的行（``-ub,   N ...``）也是分界，否则它的描述会被并到
        # 上一条的 help 里。但描述续行可能以 ``-1 =`` 开头，所以只把
        # 「短选项 + 逗号」这种列布局当作分界，而不是任何以 ``-`` 开头的行。
        if re.match(r"-[A-Za-z0-9][A-Za-z0-9_.-]*\s*,", raw.lstrip()):
            flush()
            continue
        if pending is not None:
            pending["help"] = (str(pending["help"]) + " " + raw.strip()).strip()
    flush()
    return tuple(flags.values())


def _is_bool_pair(names: list[str]) -> bool:
    """``--enable-x, --no-enable-x`` 是 argparse 的 BooleanOptionalAction。"""
    longs = [name for name in names if name.startswith("--")]
    for name in longs:
        if name.startswith("--no-") and "--" + name[len("--no-") :] in longs:
            return True
    return False


def _argparse_type(token: str, default: str | None) -> str:
    """argparse 只给 metavar，不给类型，只能从形状推。

    全大写（``MAX_MODEL_LEN``／``PORT``）是 argparse 默认由 dest 生成的 metavar，
    类型只能看默认值；其余形状（``ModelName``）按字符串处理。
    """
    if re.fullmatch(r"[A-Z0-9_]+", token):
        return _type_from_default(default) or "unknown"
    return "str"


def _parse_argparse(text: str) -> tuple[FlagSpec, ...]:
    """vLLM / SGLang / 通用 argparse 的 ``--help``。

    与 llama.cpp 的关键差别：长选项的描述常常折到下一行（甚至默认值也在续行
    里），所以这里维护一个 pending 条目，把缩进的续行拼回描述，遇到空行或
    顶格标题时收尾。
    """
    flags: dict[str, FlagSpec] = {}
    pending: dict[str, object] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        names = pending["names"]
        assert isinstance(names, list)
        long_names = [name for name in names if name.startswith("--")]
        is_pair = _is_bool_pair(long_names)
        # 与 llama.cpp 同理：``--no-x`` 不单独建条目。
        long_names = _drop_negations(long_names)
        if long_names:
            name = long_names[0]
            help_text = str(pending["help"])
            default = _extract_default(help_text)
            token = str(pending["token"])
            choices = _choices_from_token(token) if _looks_like_metavar(token) else ()
            if is_pair:
                flag_type = "bool"
            elif choices:
                flag_type = "choice"
            elif token:
                flag_type = _argparse_type(token, default)
            else:
                # 没有任何取值说明：store_true / store_false，按布尔处理。
                flag_type = "bool"
            flags.setdefault(
                name,
                FlagSpec(
                    name=name,
                    type=flag_type,
                    default=default,
                    choices=choices,
                    help=help_text,
                ),
            )
        pending = None

    for raw in text.splitlines():
        if not raw.strip():
            flush()
            continue
        if not raw[:1].isspace():
            # 顶格的组标题（``options:`` / ``Engine arguments:``）是分节标志。
            flush()
            continue
        match = _ARGPARSE_OPTION_RE.match(raw)
        if match:
            flush()
            token, help_text = _split_value_and_help(match.group("rest"))
            pending = {
                "names": [n.strip() for n in match.group("names").split(",")],
                "token": token,
                "help": help_text,
            }
            continue
        if pending is not None:
            pending["help"] = (str(pending["help"]) + " " + raw.strip()).strip()
    flush()
    return tuple(flags.values())


def parse_help(framework: str, text: str) -> tuple[FlagSpec, ...]:
    """按框架把 ``--help`` 文本转成 ``FlagSpec`` 元组。

    解析失败的行走忽略；整段文本再离谱也不能抛——它来自任意外部二进制，一次
    解析异常不该让整次扫描失败。无法识别的框架按通用 argparse 处理，因为
    Python 生态里的推理引擎基本都是 argparse。
    """
    if not text:
        return ()
    try:
        if framework in {"llama.cpp", "strata"}:
            return _parse_llama(text)
        return _parse_argparse(text)
    except Exception:
        return ()


# --- 仓库与 CMake ----------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _find_git_dir(start: Path, max_up: int) -> Path | None:
    """从 start（或它所在目录）向上找 ``.git``，支持 worktree 的 gitdir 文件。"""
    current = start if start.is_dir() else start.parent
    for _ in range(max_up + 1):
        dotgit = current / ".git"
        if dotgit.is_dir():
            return dotgit
        if dotgit.is_file():
            text = _read_text(dotgit)
            first = text.strip().splitlines()[0] if text and text.strip() else ""
            if first.startswith("gitdir:"):
                raw = first[len("gitdir:") :].strip()
                target = Path(raw)
                if not target.is_absolute():
                    target = current / target
                return target if target.is_dir() else None
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _lookup_packed_ref(packed: Path, ref: str) -> str | None:
    text = _read_text(packed)
    if not text:
        return None
    found: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, name = parts
        if name == ref and _HEX_RE.fullmatch(sha):
            found = sha
    return found


def _resolve_ref(gitdir: Path, ref: str) -> str | None:
    """先查 loose ref，再查 packed-refs；worktree 还要看 ``commondir``。

    为什么要看 commondir：worktree 自己的 gitdir 只有 HEAD，``refs/heads/*``
    和 ``packed-refs`` 放在主仓库里。不处理它，worktree 里的 build 永远取不到
    commit。
    """
    roots = [gitdir]
    common_text = _read_text(gitdir / "commondir")
    if common_text and common_text.strip():
        common = Path(common_text.strip())
        if not common.is_absolute():
            common = gitdir / common
        if common != gitdir:
            roots.append(common)
    for root in roots:
        loose = _read_text(root / ref)
        if loose:
            value = loose.strip()
            if _HEX_RE.fullmatch(value):
                return value
        packed = _lookup_packed_ref(root / "packed-refs", ref)
        if packed:
            return packed
    return None


def find_git_commit(start: Path, *, max_up: int = 6) -> str | None:
    """解析最近的 ``.git/HEAD``，不调用 git 命令。

    ``max_up`` 表示从 start 起最多再往上走几层（不含 start 自己），所以
    ``max_up=0`` 只检查 start 目录。
    """
    gitdir = _find_git_dir(start, max_up)
    if gitdir is None:
        return None
    head = _read_text(gitdir / "HEAD")
    if head is None:
        return None
    head = head.strip()
    if head.startswith("ref:"):
        ref = head[len("ref:") :].strip()
        if not ref or ref.startswith("/") or "\\" in ref or ".." in ref.split("/"):
            return None
        return _resolve_ref(gitdir, ref)
    return head if _HEX_RE.fullmatch(head) else None


def read_cmake_cache(start: Path, *, max_up: int = 6) -> dict[str, str]:
    """向上找 ``CMakeCache.txt``，只留下会影响 build 能力的开关。

    全量 Cache 有几千个条目（编译器路径、探测结果），上报出去既大又没意义；
    ``GGML_*`` / ``LLAMA_*`` 决定后端（CUDA / ROCm / Metal），
    ``CMAKE_BUILD_TYPE`` 决定优化与调试，才是 §5.5 要的东西。
    值保持原样字符串，不去猜类型。
    """
    current = start if start.is_dir() else start.parent
    for _ in range(max_up + 1):
        cache = current / "CMakeCache.txt"
        if cache.is_file():
            return _parse_cmake_cache(cache)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


def _parse_cmake_cache(path: Path) -> dict[str, str]:
    text = _read_text(path)
    if text is None:
        return {}
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith(("#", "//")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        # Cache 行形如 ``GGML_CUDA:BOOL=ON``；``:`` 后面是 CMake 的类型提示。
        name = key.split(":", 1)[0].strip()
        if name.startswith(("GGML_", "LLAMA_")) or name == "CMAKE_BUILD_TYPE":
            entries[name] = value
    return entries


# --- 框架识别与整合 --------------------------------------------------------


def detect_framework(binary: Path) -> str | None:
    """只看 basename 认框架；认不出返回 ``None``，不猜。"""
    return _FRAMEWORK_BY_BASENAME.get(binary.name)


class ProbeCache:
    """按 ``(path, inode, mtime_ns, size)`` 缓存版本与 help 解析结果。

    为什么键里要有 inode 和 mtime：同一个路径可能被重新编译覆盖，只有路径
    会命中过期结果；只看 mtime_ns 在粗粒度文件系统上又区分不了快速重建，
    加上 inode 和 size 更稳。缓存里刻意允许存 ``None``——「这个二进制不支持
    --version」也是值得记住的结论，否则每次扫描都要再超时一次。
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[object, ...], str | None] = {}
        self._flags: dict[tuple[object, ...], tuple[FlagSpec, ...]] = {}

    @staticmethod
    def _key(binary: Path) -> tuple[object, ...] | None:
        try:
            stat = binary.stat()
        except OSError:
            # 文件不在（或没权限 stat）时不缓存，但仍允许调用方正常探测。
            return None
        return (binary, stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def lookup_version(self, binary: Path) -> tuple[bool, str | None]:
        """返回 ``(是否命中, 版本)``；命中缓存里的 ``None`` 也算命中。"""
        key = self._key(binary)
        if key is None or key not in self._versions:
            return (False, None)
        return (True, self._versions[key])

    def store_version(self, binary: Path, version: str | None) -> None:
        key = self._key(binary)
        if key is not None:
            self._versions[key] = version

    def lookup_flags(
        self, binary: Path, framework: str
    ) -> tuple[bool, tuple[FlagSpec, ...]]:
        key = self._key(binary)
        if key is None:
            return (False, ())
        full = key + (framework,)
        if full not in self._flags:
            return (False, ())
        return (True, self._flags[full])

    def store_flags(
        self, binary: Path, framework: str, flags: tuple[FlagSpec, ...]
    ) -> None:
        key = self._key(binary)
        if key is not None:
            self._flags[key + (framework,)] = flags


def enrich(
    candidate: BuildCandidate,
    *,
    runner: Runner | None = None,
    cache: ProbeCache | None = None,
) -> BuildCandidate:
    """补齐一个 build 的版本、commit、CMake 开关、flags 与支持的格式。

    框架未知时先按 basename 再认一次；仍认不出就原样返回，**不做任何执行**——
    对一个来源不明的可执行文件跑 ``--version`` 不是我们该做的事，也可能卡住
    整个扫描。
    """
    framework = candidate.framework
    if framework == "unknown":
        detected = detect_framework(candidate.binary)
        if detected is None:
            return candidate
        framework = detected  # type: ignore[assignment]

    run = runner if runner is not None else default_runner
    probe_cache = cache if cache is not None else ProbeCache()
    binary = candidate.binary

    hit, version = probe_cache.lookup_version(binary)
    if not hit:
        version = probe_version(binary, argv_prefix=candidate.argv_prefix, runner=run)
        probe_cache.store_version(binary, version)

    hit, flags = probe_cache.lookup_flags(binary, framework)
    if not hit:
        help_text = read_help(binary, argv_prefix=candidate.argv_prefix, runner=run)
        flags = parse_help(framework, help_text or "")
        probe_cache.store_flags(binary, framework, flags)

    start = binary.parent
    return replace(
        candidate,
        framework=framework,  # type: ignore[arg-type]
        version=version,
        git_commit=find_git_commit(start),
        cmake=read_cmake_cache(start),
        flags=flags,
        formats=FRAMEWORK_FORMATS.get(framework, ()),
    )
