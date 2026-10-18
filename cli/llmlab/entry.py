"""``llmlab`` 命令行入口的实现。

用法概览（``llmlab --help`` 会打印同一份内容）：

    llmlab whoami
    llmlab engines     list | register --name N --target-url U | get ID | verify ID
    llmlab models      list | register --name N [--weight-path P] | get ID
    llmlab deployments list | register --name N --engine E --model M | get ID | verify ID
    llmlab endpoints   list | register --name N --deployment D | get ID | verify ID
                       | share ID --mode users --grantee G | share ID --mode all-approved
                       | unshare ID [--grantee G] | acl ID
    llmlab usage       --mine        （别名：llmlab logs --mine）
    llmlab invoke      ID [--prompt TEXT | --payload JSON]
    llmlab routing     config | snapshot      （只读；需 registry:admin:read）
    llmlab status

全局：``--json`` 机器可读输出；``--token`` / ``--token-file`` 指定凭据；
``--api-base`` 指定服务地址（明文只允许 loopback）。

凭据缺失时**退出码 2 且不发任何请求** —— 这是刻意的 fail closed。
"""

from __future__ import annotations

import argparse
import sys

from . import commands
from .client import ApiClient, DEFAULT_TIMEOUT, resolve_api_base, resolve_token
from .errors import CliError, EXIT_USAGE

PROG = "llmlab"

HANDLERS = {
    "whoami": commands.cmd_whoami,
    "engines": commands.cmd_engines,
    "models": commands.cmd_models,
    "deployments": commands.cmd_deployments,
    "endpoints": commands.cmd_endpoints,
    "usage": commands.cmd_usage,
    "logs": commands.cmd_usage,
    "invoke": commands.cmd_invoke,
    "routing": commands.cmd_routing,
    "status": commands.cmd_status,
}


def _common_parser() -> argparse.ArgumentParser:
    """全局选项。挂到主解析器**和**每个子命令上。

    ``default=SUPPRESS`` 是必须的：不加它，子解析器会用自己那份默认值把主解析器
    已经解析出的取值覆盖掉（``llmlab --json whoami`` 会变成 json=False）。
    用 SUPPRESS 则「没写就不设值」，于是两边谁写都算数，读的时候再给缺省。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出（供 Agent 使用）"
    )
    common.add_argument("--token", default=argparse.SUPPRESS, help="Portal 签发的 token")
    common.add_argument(
        "--token-file", default=argparse.SUPPRESS, help="存放 token 的文件（须 0600、属主本人、非软链）"
    )
    common.add_argument("--api-base", default=argparse.SUPPRESS, help="Registry 地址")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS)
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog=PROG,
        parents=[common],
        description=(
            "Atelier Observatory 平台 Registry 的 CLI：登记/验证/共享 Engine、Model、"
            "Deployment 与 Endpoint。凭据来自 Portal 签发的 token；CLI 不使用 Linux "
            "账密、不调 sudo、不开远端 shell。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("whoami", parents=[common], help="显示身份、scope 与可见范围")
    subparsers.add_parser("status", parents=[common], help="显示服务与各来源的可用性")

    engines = subparsers.add_parser("engines", parents=[common], help="Engine 的登记与验证")
    engines_sub = engines.add_subparsers(dest="engines_command")
    engines_sub.add_parser("list", parents=[common], help="列出自己名下/被共享到的 engine")
    register_engine = engines_sub.add_parser("register", parents=[common], help="登记一个 engine")
    register_engine.add_argument("--name", required=True)
    register_engine.add_argument(
        "--target-url", required=True, help="同主机 loopback 地址，例如 http://127.0.0.1:8000/v1"
    )
    register_engine.add_argument("--protocol", help="openai_compatible / http_json")
    register_engine.add_argument("--description")
    register_engine.add_argument("--tag", action="append", help="可重复")
    register_engine.add_argument("--idempotency-key", help="重试时给同一个键即可安全重放")
    engines_sub.add_parser("get", parents=[common], help="查看一个 engine").add_argument("object_id")
    engines_sub.add_parser("verify", parents=[common], help="触发一次验证").add_argument("object_id")

    models = subparsers.add_parser("models", parents=[common], help="Model 的登记")
    models_sub = models.add_subparsers(dest="models_command")
    models_sub.add_parser("list", parents=[common], help="列出可见的 model")
    register_model = models_sub.add_parser("register", parents=[common], help="登记一个 model")
    register_model.add_argument("--name", required=True)
    register_model.add_argument("--weight-path", help="权重路径引用（Registry 不会打开它）")
    register_model.add_argument("--format")
    register_model.add_argument("--parameter-count")
    register_model.add_argument("--description")
    register_model.add_argument("--idempotency-key")
    models_sub.add_parser("get", parents=[common], help="查看一个 model").add_argument("object_id")

    deployments = subparsers.add_parser("deployments", parents=[common], help="Deployment 的登记与验证")
    deployments_sub = deployments.add_subparsers(dest="deployments_command")
    deployments_sub.add_parser("list", parents=[common], help="列出可见的 deployment")
    register_deployment = deployments_sub.add_parser("register", parents=[common], help="登记一次部署")
    register_deployment.add_argument("--name", required=True)
    register_deployment.add_argument("--engine", required=True, help="engine id")
    register_deployment.add_argument("--model", required=True, help="model id")
    register_deployment.add_argument("--idempotency-key")
    deployments_sub.add_parser("get", parents=[common], help="查看一个 deployment").add_argument("object_id")
    deployments_sub.add_parser("verify", parents=[common], help="验证部署（要求 engine 已验证）").add_argument(
        "object_id"
    )

    endpoints = subparsers.add_parser("endpoints", parents=[common], help="Endpoint 的登记、验证与共享")
    endpoints_sub = endpoints.add_subparsers(dest="endpoints_command")
    endpoints_sub.add_parser("list", parents=[common], help="列出自己名下的与被共享给自己的 endpoint")
    register_endpoint = endpoints_sub.add_parser("register", parents=[common], help="登记一个 endpoint（默认私有）")
    register_endpoint.add_argument("--name", required=True)
    register_endpoint.add_argument("--deployment", required=True, help="deployment id")
    register_endpoint.add_argument("--idempotency-key")
    endpoints_sub.add_parser("get", parents=[common], help="查看一个 endpoint").add_argument("object_id")
    endpoints_sub.add_parser("verify", parents=[common], help="验证一个 endpoint").add_argument("object_id")
    share = endpoints_sub.add_parser("share", parents=[common], help="共享一个已验证的 endpoint")
    share.add_argument("object_id")
    share.add_argument(
        "--mode",
        required=True,
        choices=["private", "users", "all-approved"],
        help="private / users（配合 --grantee）/ all-approved",
    )
    share.add_argument("--grantee", action="append", help="可重复；--mode users 时必填")
    unshare = endpoints_sub.add_parser("unshare", parents=[common], help="撤销共享（立即生效）")
    unshare.add_argument("object_id")
    unshare.add_argument("--grantee", help="只撤销某一个受托人；省略则全部收回")
    endpoints_sub.add_parser("acl", parents=[common], help="查看授权名单（仅 owner 与管理员）").add_argument(
        "object_id"
    )
    endpoints_sub.add_parser(
        "routings", parents=[common], help="这个 alias 的路由历史（从哪个模型换到哪个）"
    ).add_argument("object_id")
    repoint = endpoints_sub.add_parser(
        "repoint", parents=[common], help="把稳定 alias 指向另一个已就绪的 deployment"
    )
    repoint.add_argument("object_id")
    repoint.add_argument("--deployment", required=True, help="目标 deployment id")
    repoint.add_argument(
        "--expected-revision",
        type=int,
        help="乐观并发控制；省略则先读一次再用它读到的值",
    )
    repoint.add_argument("--reason", help="写进审计的说明")
    repoint.add_argument(
        "--regrant",
        action="append",
        help="显式重新授权（可重复）。不给则共享收回为 private —— 旧授权不会带到新模型",
    )
    rollback = endpoints_sub.add_parser(
        "rollback", parents=[common], help="回到上一个 deployment（走同一套检查）"
    )
    rollback.add_argument("object_id")
    rollback.add_argument("--expected-revision", type=int)
    rollback.add_argument("--reason")
    rollback.add_argument("--regrant", action="append")
    applied = endpoints_sub.add_parser(
        "routing-applied",
        parents=[common],
        help="运维回执：路由器已按第 N 代配置生效（需 admin_manage）",
    )
    applied.add_argument("object_id")
    applied.add_argument("--generation", type=int, help="与当前 revision 精确相等")

    usage = subparsers.add_parser("usage", parents=[common], help="自己的用量（数据源未接入时明确 NOT_CONFIGURED）")
    usage.add_argument("--mine", action="store_true", required=True, help="只看自己的")
    logs = subparsers.add_parser("logs", parents=[common], help="同上（别名）")
    logs.add_argument("--mine", action="store_true", required=True, help="只看自己的")

    invoke = subparsers.add_parser("invoke", parents=[common], help="经授权网关调用一个共享 endpoint")
    invoke.add_argument("object_id")
    invoke.add_argument("--prompt", help="便捷形式：包成 {\"prompt\": ...}")
    invoke.add_argument("--payload", help="完整 JSON 载荷")

    routing = subparsers.add_parser(
        "routing", parents=[common], help="路由快照与只读路由契约（需 registry:admin:read）"
    )
    routing_sub = routing.add_subparsers(dest="routing_command")
    routing_sub.add_parser(
        "snapshot",
        parents=[common],
        help="导出给 Gateway 的 llmlab.endpoint-routing/1 快照（只读，不含凭据）",
    )
    routing_sub.add_parser(
        "config",
        parents=[common],
        help="本仓库自己的只读路由契约 registry-routing/1",
    )

    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    handler = HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - argparse 已限制取值
        parser.print_help()
        return EXIT_USAGE

    # 每个命令都是租户作用域的，所以**全部**需要凭据（whoami/status 也一样）。
    # 用 getattr 读全局选项：它们用 SUPPRESS 注册，没写就没有该属性。
    try:
        api_base = resolve_api_base(getattr(args, "api_base", None))
        token = resolve_token(getattr(args, "token", None), getattr(args, "token_file", None))
        client = ApiClient(api_base, token, timeout=getattr(args, "timeout", DEFAULT_TIMEOUT))
        return handler(client, args, bool(getattr(args, "json", False)))
    except CliError as exc:
        print(f"{PROG}: error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"{PROG}: hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except BrokenPipeError:  # pragma: no cover - 管道被下游关闭
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        print(f"{PROG}: interrupted", file=sys.stderr)
        return 130


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
