"""评测中心的 CLI 数据契约。

CLI 与 HTTP 面**共用同一个 service**，因此「谁能做什么」在两处不可能不一致：
CLI 不是一条绕过 HTTP 授权检查的旁路，它走的是同一个 :class:`EvaluationService`。

契约的形状
----------

* 每个命令成功时把**一份完整的 DTO 信封**写到 stdout（``ensure_ascii=False``、
  ``sort_keys=True``），因此 ``| jq`` 与逐字节回归都成立；
* 失败时把 ``{"ok": false, "error": {...}}`` 写到 stdout，把人类可读的一句话写到
  stderr，并返回 :data:`EXIT_CONTRACT`；
* 退出码是契约的一部分，不是实现细节：

  ====== =========================================
  退出码 含义
  ====== =========================================
  0      成功
  1      契约拒绝（授权、参数、状态冲突……）
  2      用法错误（参数拼错、缺必填）
  3      传输/环境错误（没有凭据、存储打不开）
  ====== =========================================

凭据**只**从环境变量 :data:`TOKEN_ENV` 或 ``--token`` 读，且**从不**回显：
错误信息里不会出现它。参数值也不回显——参数里可能有用户自己认定敏感的东西。

``mock-run`` 需要显式 ``--allow-mock``：这个命令让 mock runner 出一个合成结果，
是一个开发动作，不该因为"敲对了命令名"就发生。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, TextIO

from . import errors
from .dto import STATUS_LABELS
from .models import JOB_STATUSES
from .service import REQUEST_FIELDS, EvaluationService

EXIT_OK = 0
EXIT_CONTRACT = 1
EXIT_USAGE = 2
EXIT_TRANSPORT = 3

#: 凭据环境变量名。CLI 从不回显它的值。
TOKEN_ENV = "LLMLAB_EVAL_TOKEN"

PROGRAM = "llm-lab-evaluation"


@dataclass(frozen=True)
class Command:
    """一条命令的契约描述（供 ``describe_contract`` 与测试使用）。"""

    name: str
    summary: str
    usage: str
    requires_mock_flag: bool = False
    #: 是否需要一个可信主体。``health`` 不需要——这与 HTTP 面
    #: （``/api/evaluation/health`` 的 ``auth=False``）保持一致：同一个端点不该有
    #: 两种身份要求。
    requires_auth: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "summary": self.summary,
            "usage": self.usage,
            "requires_mock_flag": self.requires_mock_flag,
            "requires_auth": self.requires_auth,
        }


COMMANDS: tuple[Command, ...] = (
    Command("health", "适配器、准入与报告根的只读状态", "health", requires_auth=False),
    Command("datasets", "评测类型、允许清单数据集、参数范围与安全预设", "datasets"),
    Command("admission", "真实派发此刻为何被允许/被禁止", "admission"),
    Command("jobs", "列出可见的评测任务", "jobs [--status S] [--limit N] [--offset N]"),
    Command("show", "一个任务的状态", "show JOB_ID"),
    Command("result", "一个任务的结果摘要与报告引用", "result JOB_ID [--version N]"),
    Command(
        "submit",
        "提交一个评测任务（进入队列）",
        "submit --deployment ID --type T (--dataset SLUG | --preset NAME) "
        "[--param k=v ...] [--idempotency KEY]",
    ),
    Command("cancel", "取消一个任务（幂等）", "cancel JOB_ID [--reason R]"),
    Command("retry", "重试一个失败/取消的任务（幂等）", "retry JOB_ID"),
    Command(
        "mock-run",
        "开发专用：让 mock runner 产出一份合成结果",
        "mock-run JOB_ID --allow-mock",
        requires_mock_flag=True,
    ),
)


def parse_params(pairs: Iterable[str]) -> dict:
    """把 ``k=v`` 解析成参数字典。

    值的类型推断是**保守**的：``true``/``false`` -> bool，整数 -> int，浮点 -> float，
    其余原样为字符串。刻意**不**做 JSON 解析——``--param limit=[1,2]`` 应该被数据集
    的参数校验拒绝（"limit 必须是数字"），而不是在 CLI 里悄悄变成一个列表。
    """
    params: dict = {}
    for pair in pairs:
        if not isinstance(pair, str) or "=" not in pair:
            raise ValueError(f"--param expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError("--param needs a non-empty key")
        if key in params:
            # 同一个键给两次：不在两种含义里替用户选一个。
            raise ValueError(f"--param {key!r} was given more than once")
        params[key] = _coerce(raw)
    return params


def _coerce(raw: str) -> object:
    text = raw.strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return raw


def describe_contract() -> dict:
    """CLI 契约的机器可读描述。"""
    return {
        "program": PROGRAM,
        "entry_point": "python -m backend.evaluation",
        "token_env": TOKEN_ENV,
        "exit_codes": {
            "0": "success",
            "1": "contract rejection",
            "2": "usage error",
            "3": "transport or environment error",
        },
        "commands": [command.to_dict() for command in COMMANDS],
        "submit_fields": sorted(REQUEST_FIELDS),
        "job_statuses": list(JOB_STATUSES),
        "status_labels": dict(STATUS_LABELS),
        "notes": [
            "CLI 与 HTTP 共用同一个 EvaluationService；CLI 不是绕过授权的旁路。",
            "凭据只从环境变量或 --token 读取，永不回显。",
            "mock-run 需要 --allow-mock：它产出的是合成样本，不是真实评测。",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="LLM Lab Portal 评测中心 CLI（评测任务契约，非 EvalScope 本体）",
    )
    parser.add_argument("--token", default=None, help=f"不透明凭据（缺省读 ${TOKEN_ENV}）")
    sub = parser.add_subparsers(dest="command", required=True)

    for command in COMMANDS:
        child = sub.add_parser(command.name, help=command.summary, description=command.summary)
        if command.name in ("show", "result", "cancel", "retry", "mock-run"):
            child.add_argument("job_id")
        if command.name == "jobs":
            child.add_argument("--status", default=None, choices=list(JOB_STATUSES))
            child.add_argument("--limit", type=int, default=None)
            child.add_argument("--offset", type=int, default=0)
        if command.name == "result":
            child.add_argument("--version", type=int, default=None)
        if command.name == "submit":
            child.add_argument("--deployment", required=True)
            child.add_argument("--type", dest="eval_type", required=True)
            child.add_argument("--dataset", default=None)
            child.add_argument("--preset", default=None)
            child.add_argument("--param", action="append", default=[])
            child.add_argument("--idempotency", default=None)
        if command.name == "cancel":
            child.add_argument("--reason", default=None)
        if command.name == "mock-run":
            child.add_argument("--allow-mock", action="store_true")
    return parser


def _command_spec(name: str) -> Command:
    for command in COMMANDS:
        if command.name == name:
            return command
    raise errors.InvalidRequest(f"unknown command: {name!r}")  # pragma: no cover


def main(
    argv: Iterable[str] | None = None,
    *,
    service: EvaluationService,
    out: TextIO | None = None,
    err: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """运行一次 CLI。``service`` 由调用方注入（测试与预览都这么做）。"""
    stdout = out if out is not None else sys.stdout
    stderr = err if err is not None else sys.stderr
    env = environ if environ is not None else os.environ

    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse 自己退出：用法错误
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK

    spec = _command_spec(args.command)

    requester = None
    if spec.requires_auth:
        token = args.token or env.get(TOKEN_ENV)
        if not token:
            # 没有凭据就不猜身份。这是 3（环境错误），不是 1（契约拒绝）：
            # 调用方还没有让服务端做出任何判断。
            _emit_error(
                stdout,
                stderr,
                errors.Unauthenticated(
                    f"no credential supplied; set ${TOKEN_ENV} or pass --token"
                ),
            )
            return EXIT_TRANSPORT

        try:
            requester = service.requester_from_token(token)
        except errors.EvaluationError as exc:
            _emit_error(stdout, stderr, exc)
            return EXIT_TRANSPORT

    try:
        payload = _dispatch(args, service=service, requester=requester)
    except errors.EvaluationError as exc:
        _emit_error(stdout, stderr, exc)
        return EXIT_CONTRACT
    except ValueError as exc:
        _emit_error(stdout, stderr, errors.InvalidRequest(str(exc)))
        return EXIT_USAGE

    _emit(stdout, payload)
    return EXIT_OK


def _dispatch(args: argparse.Namespace, *, service: EvaluationService, requester) -> dict:
    name = args.command
    if name == "health":
        return service.capabilities_envelope()
    if name == "datasets":
        return service.datasets(requester=requester)
    if name == "admission":
        return service.admission_view(requester=requester)
    if name == "jobs":
        return service.jobs(
            requester=requester,
            status=args.status,
            limit=args.limit,
            offset=args.offset or 0,
        )
    if name == "show":
        return service.job(requester=requester, job_id=args.job_id)
    if name == "result":
        return service.result(requester=requester, job_id=args.job_id, version=args.version)
    if name == "submit":
        request: dict = {
            "deployment_id": args.deployment,
            "eval_type": args.eval_type,
        }
        if args.dataset is not None:
            request["dataset_slug"] = args.dataset
        if args.preset is not None:
            request["preset"] = args.preset
        if args.param:
            request["params"] = parse_params(args.param)
        if args.idempotency is not None:
            request["idempotency_key"] = args.idempotency
        return service.submit(requester=requester, request=request)
    if name == "cancel":
        return service.cancel_job(requester=requester, job_id=args.job_id, reason=args.reason)
    if name == "retry":
        return service.retry_job(requester=requester, job_id=args.job_id)
    if name == "mock-run":
        if not args.allow_mock:
            raise errors.InvalidRequest(
                "mock-run produces synthetic samples only; pass --allow-mock to confirm"
            )
        return service.run_mock_attempt(requester=requester, job_id=args.job_id)
    raise errors.InvalidRequest(f"unknown command: {name!r}")  # pragma: no cover


def _emit(stream: TextIO, payload: object) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    stream.write("\n")


def _emit_error(stream: TextIO, err_stream: TextIO, exc: errors.EvaluationError) -> None:
    _emit(
        stream,
        {
            "ok": False,
            "error": {
                "contract_code": exc.contract_code,
                "message": str(exc) if getattr(exc, "expose_message", True) else None,
            },
        },
    )
    err_stream.write(f"{exc.contract_code}: {str(exc) if getattr(exc, 'expose_message', True) else 'internal error'}\n")


__all__ = [
    "COMMANDS",
    "EXIT_CONTRACT",
    "EXIT_OK",
    "EXIT_TRANSPORT",
    "EXIT_USAGE",
    "PROGRAM",
    "TOKEN_ENV",
    "Command",
    "build_parser",
    "describe_contract",
    "main",
    "parse_params",
]
