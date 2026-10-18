"""评测中心的本地回环预览。**开发入口，不是部署。**

它把真实的 :class:`backend.evaluation.service.EvaluationService`（真实的授权、准入、
状态机、持久层与 DTO）接到一个只绑环回的 WSGI 容器上，目录则是一份**显式合成的**装配：
四个平台主体的 UID 与主机上真实的 Linux 账号一致，但**凭据是合成的**，目标目录是合成
的，评测数据集全部是合成条目。

它不是什么
----------

* **不是生产服务。** ``make_server`` 拒绝非环回地址；``Host`` 头不是本机此刻监听的
  那个 authority 就 400。这与 ``dev/preview_auth.py`` 的取值一致。
* **不碰 GPU。** 准入策略是 ``AdmissionPolicy.closed()``，GPU 事实是
  ``GpuFacts.not_configured()``。预览里没有任何一条路径能让真实派发变绿。
* **不产生真实评测。** runner 是 :class:`~backend.evaluation.runner.MockRunner`，
  每个结果都带 ``synthetic=true``。``/jobs/{id}/mock-run`` 只在**这个容器里**注册，
  它是 ``dev_only`` 路由，生产装配默认不注册（见 ``api.routes_for``）。
* **不读任何真实数据。** 没有任何文件读取、没有 ``usage.jsonl``、没有模型权重、
  没有提示词。报告根是 ``ReportRoot(None)``——未接线。
* **没有默认口令。** 预览的"凭据"是打印在启动横幅上的合成字符串，它们只能解析到
  合成的目录；猜到它们不会得到任何生产权限，因为这里根本没有生产可连。

用法
----

``python -m dev.evaluation_preview --port 8765``      启动，然后用 curl 打。
``python -m dev.evaluation_preview --cli datasets``   命令行跑一条命令（不需要端口）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import wsgiref.simple_server
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

from backend.evaluation import cli as evaluation_cli
from backend.evaluation.admission import AdmissionPolicy, GpuFacts
from backend.evaluation.authz import Project, ProjectDirectory
from backend.evaluation.identity import (
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
from backend.evaluation.models import utc_now
from backend.evaluation.reports import unconfigured_root
from backend.evaluation.service import EvaluationConfig, EvaluationService
from backend.evaluation.store import Store
from backend.evaluation.targets import TargetCatalog, TargetRecord

#: 只绑这个地址。
DEFAULT_HOST = "127.0.0.1"

#: ``Host`` 头里合法的三个环回拼写。IPv6 字面量必须带方括号。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: 凭据前缀。测试断言"响应里不该出现凭据"时用它。
TOKEN_PREFIX = "synthetic-"

#: 合成主体与合成 UID，和主机账户无关（``dana`` 不存在，故不在表内）。
HOST_USERS: tuple[tuple[str, int], ...] = (
    ("carol", 4000),
    ("alice", 4001),
    ("dave", 4002),
    ("bob", 4003),
)

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

#: 只读管理员：能看全局，不能改全局。用来演示"能看 != 能改"。
ADMIN_READ_ONLY_SCOPES = frozenset({SCOPE_EVAL_READ, SCOPE_EVAL_ADMIN_READ})

#: ``Host = uri-host [ ":" port ]``（RFC 7230 §5.4）。IPv6 字面量必须带方括号。
#: 用 :meth:`re.Pattern.fullmatch` 匹配**整个**字段值，且不做任何预处理。
_AUTHORITY_RE = re.compile(r"(?P<host>[^:]+|\[[0-9A-Fa-f:]+\])(?::(?P<port>[0-9]{1,5}))?")


def token_for(principal: str) -> str:
    return f"{TOKEN_PREFIX}{principal}"


@dataclass(frozen=True)
class PreviewAssembly:
    service: EvaluationService
    identities: Mapping[str, RequesterContext]
    tokens: tuple[str, ...]

    def token_for_principal(self, principal: str) -> str | None:
        return token_for(principal) if principal in self.identities else None


def build_service(
    *,
    clock: Callable[[], datetime] = utc_now,
    store: Store | None = None,
) -> PreviewAssembly:
    """装配一份**合成**的评测中心。纯内存库，无 IO。"""
    principals = PrincipalDirectory(
        [
            Principal(principal, principal, uid, display_name=f"{principal} (synthetic)")
            for principal, uid in HOST_USERS
        ]
    )
    bindings = [
        TokenBinding(token_for("carol"), "carol", ADMIN_SCOPES, frozenset({"platform_admin"})),
        TokenBinding(token_for("alice"), "alice", MEMBER_SCOPES, frozenset({"member"})),
        TokenBinding(token_for("dave"), "dave", MEMBER_SCOPES, frozenset({"member"})),
        TokenBinding(token_for("bob"), "bob", MEMBER_SCOPES, frozenset({"member"})),
        # 只读管理员：同样是 carol 这个主体，只是换了凭据与 scope。
        TokenBinding(
            f"{TOKEN_PREFIX}carol-readonly", "carol", ADMIN_READ_ONLY_SCOPES, frozenset({"platform_admin"})
        ),
    ]
    tokens = TokenDirectory(bindings)

    projects = ProjectDirectory(
        [
            Project("proj-strata", "Strata 推理服务", frozenset({"alice", "bob", "carol"})),
            Project("proj-vision", "视觉能力", frozenset({"dave"})),
        ],
        home_projects=[
            ("carol", "proj-strata"),
            ("alice", "proj-strata"),
            ("bob", "proj-strata"),
            ("dave", "proj-vision"),
        ],
    )

    targets = TargetCatalog(
        [
            TargetRecord(
                "dep-strata-alice", "proj-strata", "alice", 4001, ready=True,
                model_ref="mdl-qwen-synth", engine="strata", display_name="alice/strata/qwen(synth)",
            ),
            TargetRecord(
                "dep-strata-bob", "proj-strata", "bob", 4003, ready=True,
                model_ref="mdl-llama-synth", engine="strata", display_name="bob/strata(synth)",
            ),
            TargetRecord(
                "dep-vision-dave", "proj-vision", "dave", 4002, ready=True,
                visibility="users", grantees=frozenset({"alice"}),
                model_ref="mdl-vl-synth", engine="strata", display_name="dave/vision(synth)",
            ),
            TargetRecord(
                "dep-shared-all", "proj-vision", "dave", 4002, ready=True,
                visibility="all_approved", model_ref="mdl-vl-shared-synth", engine="strata",
                display_name="dave/vision/shared(synth)",
            ),
            # 登记了但**没验证就绪**：用来演示"注册 != 就绪"。
            TargetRecord(
                "dep-unverified", "proj-strata", "alice", 4001, ready=False,
                model_ref="mdl-unverified-synth", engine="strata", display_name="未验证(synth)",
            ),
        ]
    )

    config = EvaluationConfig(
        mode="synthetic",
        source="dev.evaluation_preview",
        mock_runner_enabled=True,
        runner_kind="mock",
        # 预览里真实派发也必须是关的：与生产同一个结论。
        admission=AdmissionPolicy.closed(label="预览构建不派发真实评测"),
        gpu_facts=GpuFacts.not_configured(
            note="预览不读 GPU；显存占用未知，不是 0。"
        ),
        report_root=unconfigured_root(),
        native_entry_available=False,
        audit_notes=("PREVIEW: 目录与数据集均为合成，不代表生产状态。",),
    )

    service = EvaluationService(
        store=store if store is not None else Store(":memory:", clock=clock),
        tokens=tokens,
        principals=principals,
        projects=projects,
        targets=targets,
        clock=clock,
        config=config,
    )
    identities = {
        principal: service.requester_from_token(token_for(principal))
        for principal, _uid in HOST_USERS
    }
    identities[f"{TOKEN_PREFIX}carol-readonly"] = service.requester_from_token(
        f"{TOKEN_PREFIX}carol-readonly"
    )
    return PreviewAssembly(service=service, identities=identities, tokens=tuple(b.token for b in bindings))


# ---------------------------------------------------------------------------
# Host 校验
# ---------------------------------------------------------------------------


def parse_host_authority(value: object, *, port: int) -> str | None:
    """校验 ``Host`` 头，返回规范化的 host；不合法返回 ``None``。

    三条刻意的严格性，都是"要么整体是，要么不是"：

    * **不做 ``strip()``。** 前后带空白的 authority 是畸形输入，不是"差不多对"的输入。
      宽容地 trim 掉它，正是让一个"只回答固定字符串"的校验开始接受变体的第一步。
    * **不做"取冒号前面那一段"。** ``127.0.0.1:8765.evil.com`` 里没有任何前缀会被剥出来
      当合法值。
    * **用 ``fullmatch`` 而不是 ``match`` + ``$``。** ``$`` 也会匹配结尾换行之前的位置，
      于是 ``"127.0.0.1:8765\\n"`` 会"几乎整体匹配"——一个不该需要被 reasoning 的例外。
    """
    if not isinstance(value, str):
        return None
    candidate = value
    if not candidate or "," in candidate:
        # 多个 Host 值（请求走私 / 代理拼接）不是我们能理解的东西。
        return None
    match = _AUTHORITY_RE.fullmatch(candidate)
    if match is None:
        return None
    raw_host = match.group("host")
    raw_port = match.group("port")

    if raw_host.startswith("["):
        if not raw_host.endswith("]"):
            return None
        host = raw_host[1:-1].lower()
    else:
        host = raw_host.lower()

    if host not in LOOPBACK_HOSTS:
        return None
    if raw_port is not None:
        number = int(raw_port)
        if number != port or not (1 <= number <= 65535):
            # 端口必须**正是**本进程此刻在听的那个。
            return None
    return host


def host_authorities(port: int) -> tuple[str, ...]:
    return (
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    )


# ---------------------------------------------------------------------------
# WSGI 容器
# ---------------------------------------------------------------------------


class PreviewApplication:
    """回环预览容器：Host 校验 + 合成目录 + dev-only mock 路由。"""

    def __init__(self, assembly: PreviewAssembly) -> None:
        from backend.evaluation.api import create_app

        self._assembly = assembly
        self._app = create_app(assembly.service, enable_mock_routes=True)
        self._port = 0

    def bind_port(self, port: int) -> None:
        self._port = int(port)

    def __call__(self, environ: dict, start_response: Callable):
        host = parse_host_authority(environ.get("HTTP_HOST"), port=self._port)
        if host is None:
            body = json.dumps(
                {
                    "ok": False,
                    "error": {
                        "contract_code": "preview.host_rejected",
                        "message": (
                            "this preview answers only to its own loopback authority "
                            f"({', '.join(host_authorities(self._port))})"
                        ),
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            start_response(
                "400 Bad Request",
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
            return [body]
        return self._app(environ, start_response)


def serve(*, host: str, port: int, assembly: PreviewAssembly) -> int:
    if host != DEFAULT_HOST:
        # 只允许环回。一个能被局域网访问的预览不是预览。
        sys.stderr.write(f"refusing to bind {host!r}: the preview binds loopback only\n")
        return 2

    application = PreviewApplication(assembly)
    with wsgiref.simple_server.make_server(host, port, application) as server:
        actual = int(server.server_address[1])
        application.bind_port(actual)
        _banner(assembly, port=actual)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            sys.stderr.write("\nstopped\n")
    return 0


def _banner(assembly: PreviewAssembly, *, port: int) -> None:
    lines = [
        "",
        "LLM Lab Portal — EvalScope 评测中心（回环预览，合成数据）",
        f"  listening:  http://127.0.0.1:{port}",
        f"  health:     http://127.0.0.1:{port}/api/evaluation/health",
        f"  openapi:    http://127.0.0.1:{port}/api/evaluation/openapi.json",
        f"  datasets:   http://127.0.0.1:{port}/api/evaluation/datasets",
        f"  admission:  http://127.0.0.1:{port}/api/evaluation/admission",
        "",
        "  SYNTHETIC 凭据（仅此预览有效；解析到的是一份合成目录）:",
    ]
    for principal, _uid in HOST_USERS:
        lines.append(f"    {principal:<9} {token_for(principal)}")
    lines.append(f"    {'carol(ro)':<9} {TOKEN_PREFIX}carol-readonly")
    lines += [
        "",
        "  Allowed Host headers: " + ", ".join(host_authorities(port)),
        "  GPU 事实未观测、真实派发关闭：本预览永远不会运行真实评测。",
        "  press Ctrl+C to stop",
        "",
    ]
    sys.stderr.write("\n".join(lines))


def _cli(argv: list[str], assembly: PreviewAssembly) -> int:
    return evaluation_cli.main(argv, service=assembly.service, environ={})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev.evaluation_preview",
        description="评测中心回环预览（开发入口，不是部署）",
    )
    parser.add_argument("--port", type=int, default=8765, help="环回端口（0 = 随机）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="只允许 127.0.0.1")
    parser.add_argument(
        "--cli",
        nargs=argparse.REMAINDER,
        default=None,
        help="不启服务，改为跑一条 CLI 命令；凭据从 LLMLAB_EVAL_TOKEN 读",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assembly = build_service()

    if args.cli is not None:
        # 命令行模式从真实环境读凭据（合成目录只认 synthetic-* 前缀）。
        import os

        return evaluation_cli.main(args.cli, service=assembly.service, environ=os.environ)

    return serve(host=args.host, port=args.port, assembly=assembly)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "ADMIN_READ_ONLY_SCOPES",
    "ADMIN_SCOPES",
    "DEFAULT_HOST",
    "HOST_USERS",
    "LOOPBACK_HOSTS",
    "MEMBER_SCOPES",
    "TOKEN_PREFIX",
    "PreviewApplication",
    "PreviewAssembly",
    "build_parser",
    "build_service",
    "host_authorities",
    "main",
    "parse_host_authority",
    "serve",
    "token_for",
]
