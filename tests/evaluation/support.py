"""测试脚手架：合成目录 + 可控时钟 + 环回 HTTP 驱动。

刻意**不**提供"连真实后端跑一遍"的开关：所有主体、项目与目标都在这里显式合成，
所有凭据都以 ``synthetic-`` 开头，任何越界的"看起来像真的"都会在断言里被抓住。

时钟是注入的（:class:`Clock`），因此时间窗与配额窗口可以被精确地推进到边界上——
一个用 ``time.sleep`` 测的窗口，测的是机器的心情，不是契约。
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import threading
import wsgiref.simple_server
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # noqa: E402 - 让 tests 能直接 import backend.* / dev.*
    sys.path.insert(0, str(ROOT))

from backend.evaluation import api as api_module  # noqa: E402
from backend.evaluation.admission import (  # noqa: E402
    AdmissionPolicy,
    GpuFacts,
)
from backend.evaluation.authz import Project, ProjectDirectory  # noqa: E402
from backend.evaluation.limits import RateLimitPolicy  # noqa: E402
from backend.evaluation.identity import (  # noqa: E402
    Principal,
    PrincipalDirectory,
    RequesterContext,
    SCOPE_EVAL_ADMIN_MANAGE,
    SCOPE_EVAL_ADMIN_READ,
    SCOPE_EVAL_CANCEL,
    SCOPE_EVAL_READ,
    SCOPE_EVAL_REPORT_READ,
    SCOPE_EVAL_RETRY,
    SCOPE_EVAL_SUBMIT,
    TokenBinding,
    TokenDirectory,
)
from backend.evaluation.reports import ReportRoot, unconfigured_root  # noqa: E402
from backend.evaluation.provenance import (  # noqa: E402
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    ReportOwner,
    ReportRecord,
    ReportSourceSnapshot,
)
from backend.evaluation.service import EvaluationConfig, EvaluationService  # noqa: E402
from backend.evaluation.store import Store  # noqa: E402
from backend.evaluation.targets import TargetCatalog, TargetRecord  # noqa: E402

from tests.evaluation import HOST_USERS  # noqa: E402

#: 所有凭据都带这个前缀，方便断言"响应里不该出现凭据"。
TOKEN_PREFIX = "synthetic-"

#: 固定的"现在"。所有需要确定性的测试都从这里开始。
EPOCH = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

MEMBER_SCOPES = frozenset(
    {
        SCOPE_EVAL_READ,
        SCOPE_EVAL_SUBMIT,
        SCOPE_EVAL_CANCEL,
        SCOPE_EVAL_RETRY,
        SCOPE_EVAL_REPORT_READ,
    }
)
ADMIN_SCOPES = frozenset({SCOPE_EVAL_READ, SCOPE_EVAL_ADMIN_READ, SCOPE_EVAL_ADMIN_MANAGE})
#: 只有全局只读、没有可授权管理 —— 用来证明「能看全局 != 能改全局」。
ADMIN_READ_ONLY_SCOPES = frozenset({SCOPE_EVAL_READ, SCOPE_EVAL_ADMIN_READ})

PROJECT_ALPHA = "proj-alpha"
PROJECT_BETA = "proj-beta"

DEP_VINCE_PRIVATE = "dep-alice-private"
DEP_ZHENGJI_PRIVATE = "dep-dave-private"
DEP_KAIDONG_PRIVATE = "dep-bob-private"
DEP_SHARED_USERS = "dep-shared-users"
DEP_SHARED_ALL = "dep-shared-all"
DEP_SHARED_STRESS_GRANTED = "dep-shared-stress-granted"
DEP_UNVERIFIED = "dep-unverified"

#: 本进程内创建过的 Harness。测试模块在 ``tearDownModule`` 里调 :func:`close_all`
#: 把它们的内存库关掉，否则每条 sqlite 连接都会在 GC 时冒一个 ResourceWarning，
#: 把真正的失败淹掉。
_LIVE: list["Harness"] = []


def close_all() -> None:
    while _LIVE:
        _LIVE.pop().close()


class Clock:
    """可推进的时钟。``advance`` 只走整数秒，因此断言可以写成逐秒比对。"""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0) -> datetime:
        self.now = self.now + timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment


@dataclass
class Harness:
    """一份装配好的评测中心 + 一个进程内 WSGI 驱动器。"""

    service: EvaluationService
    clock: Clock
    store: Store
    config: EvaluationConfig
    app: api_module.EvaluationApplication
    identities: dict = field(default_factory=dict)

    # -- 身份 ---------------------------------------------------------------

    @staticmethod
    def token_for(principal: str) -> str:
        return f"{TOKEN_PREFIX}{principal}"

    def requester(self, principal: str) -> RequesterContext:
        return self.service.requester_from_token(self.token_for(principal))

    def forged_requester(self, principal: str, *, roles: Iterable[str], scopes: Iterable[str]):
        """造一个"自称管理员"的上下文。

        它的用途是**反证**：即使一个主体的 ``roles`` 里写满 ``platform_admin``，
        只要 scope 没给，授权判定一样拒绝。``role`` 不产生权限这件事，必须被断言，
        而不是被相信。
        """
        uid = dict(HOST_USERS)[principal]
        return RequesterContext(
            principal=principal,
            uid=uid,
            linux_user=principal,
            scopes=frozenset(scopes),
            roles=frozenset(roles),
        )

    # -- 请求 ---------------------------------------------------------------

    def body(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: object = None,
        headers: Iterable[tuple[str, str]] = (),
        query: str = "",
        host: str = "127.0.0.1:8765",
    ) -> tuple[int, dict, dict]:
        """在**进程内**驱动 WSGI，返回 ``(status, headers, parsed_body)``。"""
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "wsgi.input": io.BytesIO(raw),
            "HTTP_HOST": host,
        }
        if token is not None:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        for name, value in headers:
            environ["HTTP_" + name.upper().replace("-", "_")] = value

        captured: dict = {}

        def start_response(status: str, headers_list: list) -> None:
            captured["status"] = int(status.split()[0])
            captured["headers"] = dict(headers_list)

        data = b"".join(self.app(environ, start_response))
        return captured["status"], captured["headers"], json.loads(data)

    def request(self, method: str, path: str, *, as_principal: str | None = None, **kwargs):
        token = self.token_for(as_principal) if as_principal else kwargs.pop("token", None)
        return self.body(method, path, token=token, **kwargs)

    def submit(self, principal: str, **fields) -> dict:
        """提交一个任务并返回 ``data``；失败时把错误抛出来。"""
        payload = {"eval_type": "correctness", "dataset_slug": "synthetic-qa-smoke"}
        payload.update(fields)
        status, _headers, body = self.request(
            "POST", "/api/evaluation/jobs", as_principal=principal, payload=payload
        )
        if status != 200:
            raise AssertionError(f"submit failed: {status} {body}")
        return body["data"]

    def close(self) -> None:
        self.store.close()


def build_directories():
    """合成目录：两个项目、四个主体、七个评测目标。

    ``proj-alpha`` 与 ``proj-beta`` 的存在是为了让跨项目的负控（"读别人的项目 = 404"）
    有一个真实的对象，而不是靠构造一个不存在的项目 id 来假装。
    """
    principals = PrincipalDirectory(
        [
            Principal(principal, principal, uid, display_name=f"{principal} (synthetic)")
            for principal, uid in HOST_USERS
        ]
    )
    tokens = TokenDirectory(
        [
            TokenBinding(Harness.token_for("carol"), "carol", ADMIN_SCOPES, frozenset({"platform_admin"})),
            TokenBinding(
                f"{TOKEN_PREFIX}carol-readonly", "carol", ADMIN_READ_ONLY_SCOPES,
                frozenset({"platform_admin"}),
            ),
            TokenBinding(Harness.token_for("alice"), "alice", MEMBER_SCOPES, frozenset({"member"})),
            TokenBinding(Harness.token_for("dave"), "dave", MEMBER_SCOPES, frozenset({"member"})),
            TokenBinding(Harness.token_for("bob"), "bob", MEMBER_SCOPES, frozenset({"member"})),
        ]
    )
    projects = ProjectDirectory(
        [
            Project(PROJECT_ALPHA, "Alpha", frozenset({"alice", "bob", "carol"})),
            Project(PROJECT_BETA, "Beta", frozenset({"dave"})),
        ],
        home_projects=[
            ("carol", PROJECT_ALPHA),
            ("alice", PROJECT_ALPHA),
            ("bob", PROJECT_ALPHA),
            ("dave", PROJECT_BETA),
        ],
    )
    targets = TargetCatalog(
        [
            TargetRecord(
                DEP_VINCE_PRIVATE, PROJECT_ALPHA, "alice", 4001, ready=True,
                model_ref="mdl-qwen-synth", engine="strata",
            ),
            TargetRecord(
                DEP_KAIDONG_PRIVATE, PROJECT_ALPHA, "bob", 4003, ready=True,
                model_ref="mdl-llama-synth", engine="strata",
            ),
            TargetRecord(
                DEP_ZHENGJI_PRIVATE, PROJECT_BETA, "dave", 4002, ready=True,
                model_ref="mdl-vl-synth", engine="strata",
            ),
            TargetRecord(
                DEP_SHARED_USERS, PROJECT_BETA, "dave", 4002, ready=True,
                visibility="users", grantees=frozenset({"alice"}), model_ref="mdl-shared-synth",
            ),
            TargetRecord(
                DEP_SHARED_ALL, PROJECT_BETA, "dave", 4002, ready=True,
                visibility="all_approved", model_ref="mdl-shared-all-synth",
            ),
            TargetRecord(
                DEP_SHARED_STRESS_GRANTED, PROJECT_BETA, "dave", 4002, ready=True,
                visibility="users", grantees=frozenset({"alice"}),
                gpu_stress_granted=frozenset({"alice"}), model_ref="mdl-stress-synth",
            ),
            # 登记了但**没验证就绪**：注册 != 就绪。
            TargetRecord(
                DEP_UNVERIFIED, PROJECT_ALPHA, "alice", 4001, ready=False,
                model_ref="mdl-unverified-synth", engine="strata",
            ),
        ]
    )
    return principals, tokens, projects, targets


def build_harness(
    *,
    clock: Clock | None = None,
    enable_mock_routes: bool = True,
    config_overrides: Mapping[str, object] | None = None,
    gpu_facts: GpuFacts | None = None,
    admission: AdmissionPolicy | None = None,
    report_root: ReportRoot | None = None,
    runner: object | None = None,
) -> Harness:
    """装配一份测试用的评测中心。默认值与生产同向：不派发真实评测。"""
    the_clock = clock if clock is not None else Clock()
    principals, tokens, projects, targets = build_directories()
    store = Store(":memory:", clock=the_clock)

    overrides: dict = {
        "mode": "synthetic",
        "source": "tests.evaluation.support",
        "mock_runner_enabled": True,
        "runner_kind": "mock",
        # 默认配额刻意放得很宽：测试里的时钟是冻结的，因此窗口永不滑动，一个"正常"
        # 的上限会让任何一个提交稍多的模块在别处炸掉。配额本身由
        # ``test_store_and_lifecycle.RateLimitTests`` 用它自己的 harness 显式测试。
        "principal_submit_limit": RateLimitPolicy(limit=1000, window_seconds=60),
        "global_submit_limit": RateLimitPolicy(
            limit=10_000, window_seconds=60, scope="evaluation.submit.global"
        ),
        "admission": admission if admission is not None else AdmissionPolicy.closed(),
        "gpu_facts": gpu_facts if gpu_facts is not None else GpuFacts.not_configured(),
        "report_root": report_root if report_root is not None else unconfigured_root(),
        "native_entry_available": False,
        "audit_notes": ("TEST: 合成目录。",),
    }
    if config_overrides:
        overrides.update(config_overrides)
    config = EvaluationConfig(**overrides)

    service = EvaluationService(
        store=store,
        tokens=tokens,
        principals=principals,
        projects=projects,
        targets=targets,
        clock=the_clock,
        config=config,
        runner=runner,
    )
    app = api_module.create_app(service, enable_mock_routes=enable_mock_routes)
    harness = Harness(service=service, clock=the_clock, store=store, config=config, app=app)
    harness.identities = {
        principal: harness.requester(principal) for principal, _uid in HOST_USERS
    }
    _LIVE.append(harness)
    return harness


# ---------------------------------------------------------------------------
# 报告目录（EvalScope / 旧 Benchmark）脚手架
# ---------------------------------------------------------------------------

#: 目录里合成引用的后缀。报告路径白名单里有 ``.jsonl``，但测试**刻意不用它**——
#: ``.jsonl`` 是真实用量日志的形态，套件里不出现那个后缀（见 test_static_guarantees）。
REPORT_REF_EVALSCOPE = "evalscope/reports/synth-run-1.json"
REPORT_REF_LEGACY = "legacy/benchmark/synth-mirror-1.json"


def report_owner(principal: str, uid: int | None = None, project_id: str = PROJECT_ALPHA) -> ReportOwner:
    """按 ``HOST_USERS`` 里的事实造一个归属。不给 uid 就按目录里的 uid。"""
    resolved = uid if uid is not None else dict(HOST_USERS)[principal]
    return ReportOwner(principal=principal, uid=resolved, project_id=project_id)


def report_record(
    source: str,
    ref: str,
    *,
    report_id: str | None = None,
    owner: ReportOwner | None = None,
    dimensions: Mapping[str, object] | None = None,
    created_at: datetime | None = EPOCH,
    observed_at: datetime | None = EPOCH,
    synthetic: bool = False,
    title: str | None = None,
) -> ReportRecord:
    """一条合成报告记录。维度用 :func:`backend.evaluation.models.metric_known` 等构造。"""
    return ReportRecord(
        source=source,
        source_report_id=report_id or f"{source}-{ref}",
        ref=ref,
        synthetic=synthetic,
        owner=owner,
        title=title,
        observed_at=observed_at,
        created_at=created_at,
        dimensions=dict(dimensions or {}),
    )


def report_snapshot(
    source: str,
    state: str,
    records: Iterable[ReportRecord] = (),
    *,
    observed_at: datetime | None = EPOCH,
    synthetic: bool = False,
    detail: str | None = None,
) -> ReportSourceSnapshot:
    return ReportSourceSnapshot(
        source=source,
        state=state,
        synthetic=synthetic,
        observed_at=observed_at,
        records=tuple(records),
        detail=detail,
    )


def build_harness_with_reports(
    snapshots: Iterable[ReportSourceSnapshot] = (),
    *,
    clock: Clock | None = None,
    staleness_seconds: int = 3600,
    enable_mock_routes: bool = True,
    **harness_kwargs: object,
) -> Harness:
    """一份把报告来源接成**固定快照**的 harness。

    快照是注入的（不联网、不读盘）：目录只消费事实，事实由测试给。``snapshots`` 用元组
    固化，因此断言不会因为"提供者被多调用了一次"而漂移。
    """
    frozen = tuple(snapshots)
    overrides = dict(harness_kwargs.pop("config_overrides", {}) or {})
    overrides["report_sources"] = lambda: frozen
    overrides["report_staleness_seconds"] = staleness_seconds
    return build_harness(
        clock=clock,
        enable_mock_routes=enable_mock_routes,
        config_overrides=overrides,
        **harness_kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# 真实环回 HTTP
# ---------------------------------------------------------------------------


class QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
    """关掉 per-request 日志行。

    测试输出里应当只有测试结果：一串 `"POST /... 400"` 会把真正的失败淹掉。
    """

    def log_message(self, format, *args) -> None:  # noqa: A002 - 父类签名
        return None


@contextlib.contextmanager
def serve(harness: Harness):
    """在**真实 socket**上跑一份 app，产出 ``(host, port)``。

    单线程、只绑环回。测试结束时 shutdown + close + join，不留下监听端口。
    """
    server = wsgiref.simple_server.make_server(
        "127.0.0.1", 0, harness.app, handler_class=QuietHandler
    )
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: object = None,
    headers: Iterable[tuple[str, str]] = (),
    timeout: float = 5.0,
) -> tuple[int, dict, object]:
    """走一次真实的 TCP 请求，返回 ``(status, headers, decoded_body)``。"""
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json"}
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    for name, value in headers:
        request_headers[name] = value
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        decoded = json.loads(raw) if raw else None
        return response.status, dict(response.getheaders()), decoded
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 静态扫描辅助
# ---------------------------------------------------------------------------


def package_files(pattern: str = "*.py") -> list[Path]:
    """``backend/evaluation`` 与 ``dev/evaluation_preview.py`` 的源码文件。"""
    files = sorted((ROOT / "backend" / "evaluation").glob(f"**/{pattern}"))
    preview = ROOT / "dev" / "evaluation_preview.py"
    if preview.exists():
        files.append(preview)
    return files


def read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def import_names(tree) -> set[str]:
    """``ast`` 树里所有**绝对** import 的模块名（供"不许 import 什么"的断言使用）。

    相对 import（``level > 0``，如 ``from .errors import X``）被跳过：它们的 ``module``
    是包内的模块名，不是顶层包名。一个包内部 ``from .datasets import ...`` 不是"依赖了
    一个叫 datasets 的第三方库"。
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
    return names


__all__ = [
    "ADMIN_READ_ONLY_SCOPES",
    "ADMIN_SCOPES",
    "DEP_KAIDONG_PRIVATE",
    "DEP_SHARED_ALL",
    "DEP_SHARED_STRESS_GRANTED",
    "DEP_SHARED_USERS",
    "DEP_UNVERIFIED",
    "DEP_VINCE_PRIVATE",
    "DEP_ZHENGJI_PRIVATE",
    "EPOCH",
    "MEMBER_SCOPES",
    "PROJECT_ALPHA",
    "PROJECT_BETA",
    "REPORT_REF_EVALSCOPE",
    "REPORT_REF_LEGACY",
    "SOURCE_EVALSCOPE",
    "SOURCE_LEGACY_BENCHMARK",
    "TOKEN_PREFIX",
    "Clock",
    "QuietHandler",
    "Harness",
    "build_directories",
    "build_harness",
    "build_harness_with_reports",
    "close_all",
    "http_request",
    "import_names",
    "package_files",
    "read_source",
    "report_owner",
    "report_record",
    "report_snapshot",
    "serve",
]
