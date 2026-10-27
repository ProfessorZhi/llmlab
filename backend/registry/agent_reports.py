"""每用户代理上报的**校验**：把一份自报的扫描快照变成可以入库的事实。

为什么需要这一层：报告来自用户自己的进程，路径与端口都是**它说的**。平台的
规矩是「身份只来自凭据，数据归属不来自请求」，所以这里不做「相信并记录」，
而是逐条核对平台自己能核对的那部分：

* **端口**：``/proc/net/tcp`` 是全员可读的，每一行带着 socket 属主的 uid。
  上报的端口此刻必须真的在 LISTEN，而且只属于上报者一个 uid。核对不了就丢弃，
  不猜。
* **路径**：服务端 ``stat`` 得到就用它的 ``st_uid``；``stat`` 不到时，只接受
  词法上落在**上报者本人** home 下的路径（那正是服务端读不到、而代理存在的
  理由）。落在别人 home 下的一律丢弃 —— 这条哪怕 stat 不到也成立。
* **能自报的字段一律不采信**：``run_as_uid`` / ``run_as_user`` / ``target_url`` /
  ``registered`` 都不进快照；客户端带了身份字段的条目整条丢弃。

拒绝不是整份失败：一个人有一千个文件不该让他的报告永远发不出来。被拒的条目
按理由计数返回，并汇总成稳定的 problem 串；页面与 CLI 都能看见「丢了什么、
为什么」。唯一的整体失败是**快照太大**（由传输层判，见 ``agent_channel``）。

这一层不碰数据库、不开 socket、不跑任何被发现的程序。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .errors import InvalidRequest
from .identity import FORBIDDEN_IDENTITY_FIELDS
from .local_discovery import KNOWN_ENGINES, _serves_loopback, is_safe_model_id
from .models import MAX_NAME_LENGTH
from .ownership import (
    ProcNetListenerTable,
    ProcNetUnavailable,
    ProcNetUnparseable,
    linux_user_for_uid,
)

#: 协议的版本。字段形状不兼容地变化时 +1；旧版本会被 400 拒绝。
REPORT_VERSION = 1

#: 超过这个年龄的报告在页面上标「已过期」。30 分钟 = 30 个扫描周期：
#: 一个短暂重启的代理不该立刻看起来像死了。
STALE_AFTER_SECONDS = 30 * 60

#: 写路径顺手清理的老行。报告是「现在在哪」的快照，不是历史。
PRUNE_AFTER_SECONDS = 30 * 24 * 3600

#: 一个主体最多能有多少个代理 id。防止一个进程反复换 id 把表撑大。
MAX_AGENTS_PER_PRINCIPAL = 8

#: 每份报告的条目上限。超出的**截断**并如实写一条 problem —— 整份 413 会让
#: 一个模型很多的人永远同步不了。
MAX_ENGINES = 64
MAX_INSTALLED_ENGINES = 64
MAX_MODELS = 200
MAX_MODELS_PER_ENGINE = 20
MAX_PROBLEMS = 50

#: 一次校验最多 stat 多少个路径。有权限的成员可以让服务端 stat 任意路径；
#: 600 个足以覆盖一台工作站的全部模型，又不至于让一个挂死的网络盘拖住一个请求。
MAX_STAT_PATHS = 600

MAX_PATH_LENGTH = 4096
MAX_TEXT_LENGTH = 256

#: 代理 id 的形状。它只用于区分同一个人的多台机器，不承担身份。
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 客户端**永远不能**在这份 payload 里自带的字段。命中整条丢弃。
_ITEM_IDENTITY_FIELDS = frozenset(FORBIDDEN_IDENTITY_FIELDS) | frozenset(
    {"run_as_uid", "run_as_user", "target_url", "registered", "registered_by_me"}
)

#: 路径落在 ``/home/<name>/…`` 或 ``/data/home/<name>/…`` 时的用户名。
_HOME_PATH = re.compile(r"^/(?:home|data/home)/([^/]+)(?:/|$)")

_FOUND_AS = frozenset({"executable", "python_package", "source_tree"})
_MODEL_FORMATS = frozenset({"hf", "gguf", "diffusers"})
_ENGINE_IDS = frozenset(engine for engine, *_ in KNOWN_ENGINES)

#: 条目的拒绝理由（稳定的契约 token，前端与审计按它分支）。
REASON_PROC_NET_UNREADABLE = "proc_net_unavailable"
REASON_PORT_INVALID = "port_invalid"
REASON_PORT_NOT_LISTENING = "port_not_listening"
REASON_PORT_UID_MISMATCH = "port_uid_mismatch"
REASON_PORT_UID_AMBIGUOUS = "port_uid_ambiguous"
REASON_PATH_OTHER_HOME = "path_other_user_home"
REASON_PATH_MISSING = "path_missing"
REASON_PATH_UNVERIFIABLE = "path_unverifiable"
REASON_PATH_UID_MISMATCH = "path_uid_mismatch"
REASON_PATH_INVALID = "path_invalid"
REASON_PATH_DUPLICATE = "duplicate_path"
REASON_STAT_BUDGET = "stat_budget_exhausted"
REASON_IDENTITY_FIELD = "identity_field_present"
REASON_NOT_OBJECT = "item_not_object"
REASON_FORMAT = "format_invalid"
REASON_SIZE = "size_invalid"
REASON_FOUND_AS = "found_as_invalid"
REASON_ENGINE_UNKNOWN = "engine_unknown"
REASON_TOO_MANY = "too_many"

#: 哪些拒绝理由值得写进审计：它们只可能来自伪造，或一个实现错了的代理。
OWNERSHIP_REASONS = frozenset(
    {REASON_PORT_UID_MISMATCH, REASON_PATH_UID_MISMATCH, REASON_PATH_OTHER_HOME}
)


def _system_homes() -> dict[int, str]:
    """``uid -> home``。只在真实主机上问 NSS；测试注入自己的映射。"""

    import pwd

    homes: dict[int, str] = {}
    try:
        for record in pwd.getpwall():
            if record.pw_dir:
                homes[int(record.pw_uid)] = record.pw_dir
    except OSError:  # pragma: no cover - NSS 不可用时没有更好的答案
        return {}
    return homes


def _bounded_text(value: object, limit: int = MAX_TEXT_LENGTH) -> str | None:
    """一段只用于展示的文本：去掉控制字符，超长截断，不是字符串就 ``None``。"""

    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ord(ch) >= 0x20 and ord(ch) != 0x7F).strip()
    return cleaned[:limit] or None


@dataclass(frozen=True)
class AgentReport:
    """校验通过的快照。只有服务端算出来的字段，没有一条来自客户端的自报。"""

    agent_id: str
    host_id: str
    uid: int
    linux_user: str
    report_version: int
    scanned_at: str | None
    received_at: datetime
    full: bool
    engines: tuple[dict, ...]
    installed_engines: tuple[dict, ...]
    models: tuple[dict, ...]
    problems: tuple[str, ...]
    rejected: dict[str, dict[str, int]]
    scan_roots: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "engines": len(self.engines),
            "installed_engines": len(self.installed_engines),
            "models": len(self.models),
        }

    def snapshot(self) -> dict:
        """入库的部分。``received_at`` 是列，不重复进 JSON。"""

        return {
            "agent_id": self.agent_id,
            "report_version": self.report_version,
            "scanned_at": self.scanned_at,
            "full": self.full,
            "scan_roots": list(self.scan_roots),
            "engines": list(self.engines),
            "installed_engines": list(self.installed_engines),
            "models": list(self.models),
            "rejected": {bucket: dict(items) for bucket, items in self.rejected.items()},
        }

    def response(self) -> dict:
        """给上报者的回执：收了多少、丢了多少、为什么。"""

        return {
            "agent_id": self.agent_id,
            "host_id": self.host_id,
            "received_at": self.received_at.isoformat(),
            "accepted": self.counts(),
            "rejected": {bucket: dict(items) for bucket, items in self.rejected.items()},
            "problems": list(self.problems),
        }

    def ownership_violations(self) -> dict[str, int]:
        """只挑出「归属不符」的计数，供审计用。"""

        return {
            f"{bucket}:{reason}": count
            for bucket, reasons in self.rejected.items()
            for reason, count in reasons.items()
            if reason in OWNERSHIP_REASONS
        }


@dataclass
class _Context:
    """一次校验的可变状态。放在这里而不是 ``self`` 上：校验器会被多个连接线程共用。"""

    uid: int
    linux_user: str
    homes: dict[int, str]
    stat_budget: int = MAX_STAT_PATHS
    rejected: dict[str, dict[str, int]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def reject(self, bucket: str, reason: str) -> None:
        counts = self.rejected.setdefault(bucket, {})
        counts[reason] = counts.get(reason, 0) + 1


@dataclass
class AgentReportValidator:
    """把一份 payload 变成 :class:`AgentReport`。纯函数式：不写库、不记日志。"""

    allowed_ports: frozenset[int] = frozenset()
    #: 读 LISTEN 表。测试注入 fixture 文本。
    listener_table: object = field(default_factory=ProcNetListenerTable)
    stat_path: Callable[[str], object] = os.stat
    homes: Callable[[], dict[int, str]] = _system_homes

    def validate(
        self,
        *,
        principal: str,
        uid: int,
        linux_user: str,
        host_id: str,
        payload: dict,
        received_at: datetime,
    ) -> AgentReport:
        """``principal`` / ``uid`` / ``linux_user`` / ``host_id`` 都来自受信目录。"""

        if not isinstance(payload, dict):
            raise InvalidRequest("an agent report must be a JSON object")

        version = payload.get("report_version", REPORT_VERSION)
        if version != REPORT_VERSION:
            raise InvalidRequest(
                f"unsupported agent report_version: {version!r} "
                f"(this server speaks {REPORT_VERSION})"
            )

        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not _AGENT_ID.match(agent_id):
            raise InvalidRequest("agent_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")

        scanned_at = self._scanned_at(payload.get("scanned_at"))
        full = payload.get("full", False)
        if not isinstance(full, bool):
            raise InvalidRequest("full must be a boolean")

        ctx = _Context(uid=uid, linux_user=linux_user, homes=self.homes())
        engines = self._engines(payload.get("engines"), ctx)
        installed = self._installed(payload.get("installed_engines"), ctx)
        models = self._models(payload.get("models"), ctx)

        reported = payload.get("problems")
        if isinstance(reported, list):
            for item in reported[:MAX_PROBLEMS]:
                text = _bounded_text(item, 512)
                if text is not None:
                    ctx.problems.append(text)

        ctx.problems.extend(summarize_rejections(ctx.rejected))
        return AgentReport(
            agent_id=agent_id,
            host_id=host_id,
            uid=uid,
            linux_user=linux_user,
            report_version=REPORT_VERSION,
            scanned_at=scanned_at,
            received_at=received_at,
            full=full,
            engines=tuple(engines),
            installed_engines=tuple(installed),
            models=tuple(models),
            problems=tuple(_finalize_problems(ctx.problems)),
            rejected=ctx.rejected,
            scan_roots=tuple(os.path.normpath(p) for p in _strings(payload.get("scan_roots"))[:16]
                             if os.path.isabs(p) and "\x00" not in p and len(p) <= MAX_PATH_LENGTH),
        )

    # -- 条目 -------------------------------------------------------------

    def _engines(self, raw: object, ctx: _Context) -> list[dict]:
        items = _as_list(raw, "engines")
        accepted: list[dict] = []
        table = self._listener_uids()
        for item in items[:MAX_ENGINES]:
            if not isinstance(item, dict):
                ctx.reject("engines", REASON_NOT_OBJECT)
                continue
            if _asserts_identity(item):
                ctx.reject("engines", REASON_IDENTITY_FIELD)
                continue
            port = item.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
                ctx.reject("engines", REASON_PORT_INVALID)
                continue
            if table is None:
                ctx.reject("engines", REASON_PROC_NET_UNREADABLE)
                continue
            owners = table.get(port)
            if not owners:
                ctx.reject("engines", REASON_PORT_NOT_LISTENING)
                continue
            if len(owners) > 1:
                ctx.reject("engines", REASON_PORT_UID_AMBIGUOUS)
                continue
            if next(iter(owners)) != ctx.uid:
                ctx.reject("engines", REASON_PORT_UID_MISMATCH)
                continue

            framework = item.get("framework")
            if framework not in _ENGINE_IDS:
                framework = None
            model_ids = [
                model_id
                for model_id in _strings(item.get("models"))
                if is_safe_model_id(model_id)
            ][:MAX_MODELS_PER_ENGINE]
            accepted.append(
                {
                    "port": port,
                    "target_url": f"http://127.0.0.1:{port}",
                    # 归属与运行用户由服务端写死：客户端说什么都不算。
                    "run_as_uid": ctx.uid,
                    "run_as_user": ctx.linux_user,
                    "openai_compatible": bool(model_ids),
                    "models": model_ids,
                    "framework": framework,
                    "suggested_name": _bounded_text(
                        item.get("suggested_name"), MAX_NAME_LENGTH
                    ),
                    # 端口不在登记允许列表里：看得见，但登记会被拒。这是服务端
                    # 自己的规则，客户端无从声明。
                    "registrable": port in self.allowed_ports,
                }
            )
        if len(items) > MAX_ENGINES:
            ctx.reject("engines", REASON_TOO_MANY)
            ctx.problems.append(f"agent_report_engines_capped:{len(items) - MAX_ENGINES}")
        return accepted

    def _installed(self, raw: object, ctx: _Context) -> list[dict]:
        items = _as_list(raw, "installed_engines")
        accepted: list[dict] = []
        for item in items[:MAX_INSTALLED_ENGINES]:
            if not isinstance(item, dict):
                ctx.reject("installed_engines", REASON_NOT_OBJECT)
                continue
            if _asserts_identity(item):
                ctx.reject("installed_engines", REASON_IDENTITY_FIELD)
                continue
            engine = item.get("engine")
            if engine not in _ENGINE_IDS:
                ctx.reject("installed_engines", REASON_ENGINE_UNKNOWN)
                continue
            found_as = item.get("found_as")
            if found_as not in _FOUND_AS:
                ctx.reject("installed_engines", REASON_FOUND_AS)
                continue
            path, reason = self._path(item.get("path"), ctx)
            if path is None:
                ctx.reject("installed_engines", reason)
                continue
            env: str | None = None
            if item.get("env") is not None:
                env, _env_reason = self._path(item.get("env"), ctx)
                # env 只是展示用；不合格不连累 path —— path 才是归属证据。
            accepted.append(
                {
                    "engine": engine,
                    "name": _bounded_text(item.get("name")) or engine,
                    "found_as": found_as,
                    "version": _bounded_text(item.get("version"), 64),
                    "path": path,
                    "env": env,
                }
            )
        if len(items) > MAX_INSTALLED_ENGINES:
            ctx.reject("installed_engines", REASON_TOO_MANY)
            ctx.problems.append(
                f"agent_report_installed_engines_capped:{len(items) - MAX_INSTALLED_ENGINES}"
            )
        return accepted

    def _models(self, raw: object, ctx: _Context) -> list[dict]:
        items = _as_list(raw, "models")
        accepted: list[dict] = []
        seen: set[str] = set()
        for item in items[:MAX_MODELS]:
            if not isinstance(item, dict):
                ctx.reject("models", REASON_NOT_OBJECT)
                continue
            if _asserts_identity(item):
                ctx.reject("models", REASON_IDENTITY_FIELD)
                continue
            path, reason = self._path(item.get("path"), ctx)
            if path is None:
                ctx.reject("models", reason)
                continue
            if path in seen:
                ctx.reject("models", REASON_PATH_DUPLICATE)
                continue
            fmt = item.get("format")
            if fmt not in _MODEL_FORMATS:
                ctx.reject("models", REASON_FORMAT)
                continue
            size = item.get("size_bytes")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                ctx.reject("models", REASON_SIZE)
                continue
            seen.add(path)
            accepted.append(
                {
                    "path": path,
                    "format": fmt,
                    "size_bytes": size,
                    "architecture": _bounded_text(item.get("architecture")),
                    "model_type": _bounded_text(item.get("model_type")),
                    "suggested_name": _bounded_text(
                        item.get("suggested_name"), MAX_NAME_LENGTH
                    ),
                    **_model_metadata(item),
                }
            )
        if len(items) > MAX_MODELS:
            ctx.reject("models", REASON_TOO_MANY)
            ctx.problems.append(f"agent_report_models_capped:{len(items) - MAX_MODELS}")
        return accepted

    # -- 单个事实 ---------------------------------------------------------

    def _listener_uids(self) -> dict[int, set[int]] | None:
        """``port -> {uid}``；表读不懂就是 ``None``（整桶 fail closed）。"""

        try:
            listeners = self.listener_table.listeners()
        except (ProcNetUnavailable, ProcNetUnparseable):
            return None
        table: dict[int, set[int]] = {}
        for item in listeners:
            if not _serves_loopback(item.ip):
                continue
            table.setdefault(item.port, set()).add(item.uid)
        return table

    def _path(self, raw: object, ctx: _Context) -> tuple[str | None, str]:
        """校验一个路径。返回 ``(path, "")`` 或 ``(None, reason)``。"""

        if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LENGTH:
            return None, REASON_PATH_INVALID
        if "\x00" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
            return None, REASON_PATH_INVALID
        if not os.path.isabs(raw) or ".." in raw.split("/"):
            return None, REASON_PATH_INVALID

        path = os.path.normpath(raw)
        lexical = _lexical_home_owner(path)
        if lexical is not None and lexical != ctx.linux_user:
            return None, REASON_PATH_OTHER_HOME
        if _under_other_home(path, ctx):
            return None, REASON_PATH_OTHER_HOME

        # 软链可以逃出词法范围：本人 home 下的一个链接指向别处时，只有 realpath
        # 之后的名字才算数。realpath 失败（例如不可读的中间目录）就按词法结果。
        real = os.path.realpath(path)
        if real != path:
            real_owner = _lexical_home_owner(real)
            if real_owner is not None and real_owner != ctx.linux_user:
                return None, REASON_PATH_OTHER_HOME
            if _under_other_home(real, ctx):
                return None, REASON_PATH_OTHER_HOME

        if ctx.stat_budget <= 0:
            return None, REASON_STAT_BUDGET
        ctx.stat_budget -= 1
        try:
            info = self.stat_path(path)
        except FileNotFoundError:
            return None, REASON_PATH_MISSING
        except PermissionError:
            # 服务端读不到，恰恰是「这个文件可能在别人家里」的反面证据：
            # 词法检查已经确认它在上报者**本人**的 home 下，才允许接受。
            if lexical == ctx.linux_user or _under_own_home(path, ctx):
                return path, ""
            return None, REASON_PATH_UNVERIFIABLE
        except OSError:
            return None, REASON_PATH_UNVERIFIABLE

        owner = getattr(info, "st_uid", None)
        if owner is not None and owner != ctx.uid:
            return None, REASON_PATH_UID_MISMATCH
        return path, ""

    @staticmethod
    def _scanned_at(raw: object) -> str | None:
        """客户端时钟，**只用于展示**。``stale`` 永远按服务端的 ``received_at`` 算。"""

        if raw is None:
            return None
        if not isinstance(raw, str) or not raw:
            raise InvalidRequest("scanned_at must be an ISO-8601 string")
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise InvalidRequest("scanned_at must be an ISO-8601 string") from None
        if parsed.tzinfo is None:
            raise InvalidRequest("scanned_at must carry a timezone")
        return parsed.isoformat()


def _model_metadata(item: dict) -> dict:
    """Untrusted observations, never used to establish ownership."""
    result = {}
    fingerprint = item.get("fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[0-9]{1,20}:[a-f0-9]{64}", fingerprint):
        result["fingerprint"] = fingerprint
    for key in ("device", "inode"):
        value = item.get(key)
        if type(value) is int and 0 <= value < 2**63:
            result[key] = value
    value = item.get("mtime")
    if type(value) in (int, float) and 0 <= value < 10**12:
        result["mtime"] = value
    raw = item.get("facts")
    if isinstance(raw, dict):
        facts = {}
        for key in ("architecture", "name", "file_type", "task", "format"):
            value = _bounded_text(raw.get(key), 128)
            if value is not None:
                facts[key] = value
        for key in ("context_length", "block_count", "embedding_length", "head_count", "head_count_kv", "expert_count"):
            value = raw.get(key)
            if type(value) is int and 0 <= value < 2**40:
                facts[key] = value
        if type(raw.get("vision")) is bool:
            facts["vision"] = raw["vision"]
        result["facts"] = facts
    return result


def _as_list(raw: object, name: str) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InvalidRequest(f"{name} must be a list")
    return raw


def _strings(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _asserts_identity(item: dict) -> bool:
    """这一条自己声明了归属/身份 -> 整条丢弃，而不是「读了不用」。"""

    return bool(set(item) & _ITEM_IDENTITY_FIELDS)


def _under_other_home(path: str, ctx: _Context) -> bool:
    for owner_uid, home in ctx.homes.items():
        if owner_uid == ctx.uid or not home:
            continue
        home = home.rstrip("/")
        if not home or home == "/":
            continue
        if path == home or path.startswith(home + "/"):
            return True
    return False


def _under_own_home(path: str, ctx: _Context) -> bool:
    if _lexical_home_owner(path) == ctx.linux_user:
        return True
    for owner_uid, home in ctx.homes.items():
        home = home.rstrip("/")
        if not home or home == "/" or owner_uid != ctx.uid:
            continue
        if path == home or path.startswith(home + "/"):
            return True
    return False


def _finalize_problems(problems: list[str]) -> list[str]:
    """去重、按出现顺序保留，最多 ``MAX_PROBLEMS`` 条。"""

    seen: set[str] = set()
    kept: list[str] = []
    for item in problems:
        if item not in seen:
            seen.add(item)
            kept.append(item)
    return kept[:MAX_PROBLEMS]


def _lexical_home_owner(path: str) -> str | None:
    match = _HOME_PATH.match(path)
    return match.group(1) if match else None


def summarize_rejections(rejected: dict[str, dict[str, int]]) -> list[str]:
    """``{bucket: {reason: n}}`` ⇒ 稳定的 problem 串，供快照与页面展示。"""

    return [
        f"agent_report_{bucket}_dropped:{reason}:{count}"
        for bucket in sorted(rejected)
        for reason, count in sorted(rejected[bucket].items())
        if reason != REASON_TOO_MANY
    ][:MAX_PROBLEMS]


__all__ = [
    "AgentReport",
    "AgentReportValidator",
    "MAX_AGENTS_PER_PRINCIPAL",
    "OWNERSHIP_REASONS",
    "PRUNE_AFTER_SECONDS",
    "REPORT_VERSION",
    "STALE_AFTER_SECONDS",
    "summarize_rejections",
]
