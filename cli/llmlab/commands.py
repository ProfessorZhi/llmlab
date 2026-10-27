"""``llmlab`` 子命令的实现。

命令面刻意与 Registry 的三条链一一对应（注册 / 验证 / 共享），而不是把后端
端点原样搬过来：

    llmlab whoami                     我是谁、我能做什么
    llmlab engines   list|register|get|verify
    llmlab models    list|register|get
    llmlab deployments list|register|get|verify
    llmlab endpoints list|register|get|verify|share|unshare|acl
    llmlab usage --mine               （``logs --mine`` 是它的别名）
    llmlab invoke <id>               经授权网关调用一个共享 Endpoint
    llmlab status                    服务与来源的可用性

每个命令都支持 ``--json``：给 CC Agent 用的形态。**没有 ``--json`` 时**输出人类可读
的表格；两种形态都来自同一份响应，不存在「JSON 里是 null、表格里是 0」这种分歧。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Callable

from .client import ApiClient
from .errors import CliError, EXIT_OK, EXIT_USAGE

#: 人类可读输出里的 null 表示。刻意与 JSON 的 ``null`` 一致，不用 0 顶替。
NULL = "—"


def _fmt(value: object) -> str:
    if value is None:
        return NULL
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value) if value else NULL
    return str(value)


def _print_envelope(envelope: dict, as_json: bool, *, default_render: Callable[[dict], None]) -> None:
    if as_json:
        print(json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=False))
        return
    default_render(envelope.get("data") or {})
    notes = envelope.get("notes") or []
    hints = envelope.get("display_hints") or []
    for line in hints:
        print(f"! {line}")
    for line in notes:
        print(f"· {line}")


def _table(rows: list[list[str]], header: list[str]) -> None:
    if not rows:
        print("(empty)")
        return
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _health_cell(record: dict) -> str:
    health = record.get("health") or {}
    state = health.get("state", "unknown")
    latency = health.get("latency_ms")
    if latency is None:
        # 没有量过就不写数字 —— 「正正常常的一个数」是最容易骗到人的东西。
        return state
    return f"{state}/{latency}ms"


# --- 各命令 ---------------------------------------------------------------


def cmd_whoami(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    envelope = client.get("/whoami")
    _print_envelope(
        envelope,
        as_json,
        default_render=lambda data: print(
            f"principal : {_fmt(data['identity']['principal'])}\n"
            f"uid       : {_fmt(data['identity']['uid'])}\n"
            f"linux user: {_fmt(data['identity']['linux_user'])}\n"
            f"scopes    : {_fmt(data['authorized']['scopes'])}\n"
            f"roles     : {_fmt(data['authorized']['roles'])}  (roles 不产生权限)\n"
            f"can write : {_fmt(data['capabilities']['can_write'])}\n"
            f"can share : {_fmt(data['capabilities']['can_share'])}\n"
            f"can invoke: {_fmt(data['capabilities']['can_invoke'])}\n"
            f"can read all: {_fmt(data['capabilities']['can_read_all'])}\n"
            f"visible   : engines={data['visible_counts']['engines']} "
            f"models={data['visible_counts']['models']} "
            f"deployments={data['visible_counts']['deployments']} "
            f"endpoints={data['visible_counts']['endpoints']}\n"
            f"mode      : {_fmt(data['deployment']['mode'])} "
            f"(prober={'on' if data['deployment']['prober_attached'] else 'off'}, "
            f"gateway={_fmt(data['deployment']['gateway_adapter'])})"
        ),
    )
    return EXIT_OK


def cmd_engines(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    if args.engines_command == "list":
        envelope = client.get("/engines")

        def render(data: dict) -> None:
            rows = [
                [
                    item["id"],
                    item["name"],
                    _fmt(item["owner"]),
                    _fmt(item["status"]),
                    _health_cell(item),
                    _fmt((item.get("target") or {}).get("url")) or "redacted",
                ]
                for item in data["items"]
            ]
            _table(rows, ["ID", "NAME", "OWNER", "STATUS", "HEALTH", "TARGET"])
            print(f"{data['count']} engine(s)")

        _print_envelope(envelope, as_json, default_render=render)
        return EXIT_OK

    if args.engines_command == "register":
        body: dict = {
            "name": args.name,
            "target_url": args.target_url,
            "engine_mode": getattr(args, "engine_mode", "user") or "user",
        }
        if args.protocol:
            body["protocol"] = args.protocol
        if args.description:
            body["description"] = args.description
        tags = list(args.tag or [])
        if getattr(args, "framework", None):
            tags.append(f"framework:{args.framework}")
        if tags:
            body["tags"] = tags
        envelope = client.post("/engines", body, idempotency_key=args.idempotency_key)
        data = envelope.get("data") or {}
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda payload: print(
                f"registered engine {payload['id']} ({payload['name']})\n"
                f"  status   : {_fmt(payload['status'])}\n"
                f"  health   : {_health_cell(payload)}  (reason: {_fmt((payload.get('health') or {}).get('reason'))})\n"
                f"  verified : {_fmt((payload.get('verification') or {}).get('status'))}\n"
                f"  replay   : {_fmt(payload.get('idempotent_replay'))}"
            ),
        )
        return EXIT_OK

    if args.engines_command == "preflight":
        envelope = client.post(
            "/engines/preflight",
            {
                "name": args.name,
                "target_url": args.target_url,
                "engine_mode": getattr(args, "engine_mode", "user") or "user",
            },
        )

        def render_preflight(data: dict) -> None:
            for key, check in (data.get("checks") or {}).items():
                mark = {True: "ok", False: "FAIL", None: "n/a"}.get(check.get("ok"), "?")
                extra = ""
                evidence = check.get("evidence") or {}
                if evidence.get("linux_user") or evidence.get("uid") is not None:
                    extra = f"  (run as {_fmt(evidence.get('linux_user'))}, uid {_fmt(evidence.get('uid'))})"
                print(f"  {key:15s} {mark:5s} {_fmt(check.get('detail'))}{extra}")
            print(f"all_ok: {_fmt(data.get('all_ok'))}")

        _print_envelope(envelope, as_json, default_render=render_preflight)
        return EXIT_OK

    if args.engines_command == "get":
        envelope = client.get(f"/engines/{args.object_id}")
        _print_envelope(
            envelope, as_json, default_render=lambda data: print(json.dumps(data, indent=2, ensure_ascii=False))
        )
        return EXIT_OK

    if args.engines_command == "verify":
        envelope = client.post(f"/engines/{args.object_id}/verify")
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"engine {data['id']}: status={_fmt(data['status'])} "
                f"verification={_fmt((data.get('verification') or {}).get('status'))} "
                f"health={_health_cell(data)}"
            ),
        )
        return EXIT_OK
    raise CliError(EXIT_USAGE, f"unknown engines subcommand: {args.engines_command}")


def cmd_models(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    if args.models_command == "list":
        envelope = client.get("/models")

        def render(data: dict) -> None:
            rows = [
                [
                    item["id"],
                    item["name"],
                    _fmt(item["owner"]),
                    _fmt(item["status"]),
                    "(redacted)" if item.get("weight_path_redacted") else _fmt(item.get("weight_path")),
                ]
                for item in data["items"]
            ]
            _table(rows, ["ID", "NAME", "OWNER", "STATUS", "WEIGHT PATH"])
            print(f"{data['count']} model(s)")

        _print_envelope(envelope, as_json, default_render=render)
        return EXIT_OK

    if args.models_command == "register":
        body: dict = {"name": args.name}
        if args.weight_path:
            body["weight_path"] = args.weight_path
        if args.format:
            body["format"] = args.format
        if args.parameter_count:
            body["parameter_count"] = args.parameter_count
        if args.description:
            body["description"] = args.description
        envelope = client.post("/models", body, idempotency_key=args.idempotency_key)
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"registered model {data['id']} ({data['name']})\n"
                f"  weight ref: {_fmt(data.get('weight_path'))} "
                f"sha={_fmt(data.get('weight_path_sha256'))}\n"
                f"  weight_present: {_fmt(data.get('weight_present'))} "
                "(Registry 从不打开权重文件，所以这里是未观测而不是 false)"
            ),
        )
        return EXIT_OK

    if args.models_command == "get":
        envelope = client.get(f"/models/{args.object_id}")
        _print_envelope(
            envelope, as_json, default_render=lambda data: print(json.dumps(data, indent=2, ensure_ascii=False))
        )
        return EXIT_OK
    raise CliError(EXIT_USAGE, f"unknown models subcommand: {args.models_command}")


def cmd_deployments(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    if args.deployments_command == "list":
        envelope = client.get("/deployments")

        def render(data: dict) -> None:
            rows = [
                [
                    item["id"],
                    item["name"],
                    _fmt(item["owner"]),
                    _fmt(item["status"]),
                    _fmt(item["ready"]),
                    _health_cell(item),
                ]
                for item in data["items"]
            ]
            _table(rows, ["ID", "NAME", "OWNER", "STATUS", "READY", "HEALTH"])
            print(f"{data['count']} deployment(s)")

        _print_envelope(envelope, as_json, default_render=render)
        return EXIT_OK

    if args.deployments_command == "register":
        envelope = client.post(
            "/deployments",
            {"name": args.name, "engine_id": args.engine, "model_id": args.model},
            idempotency_key=args.idempotency_key,
        )
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"registered deployment {data['id']} ({data['name']})\n"
                f"  registered : {_fmt(data['registered'])}\n"
                f"  ready      : {_fmt(data['ready'])}  "
                "(注册 != 就绪：ready 需要一次真实的 engine 验证)"
            ),
        )
        return EXIT_OK

    if args.deployments_command == "get":
        envelope = client.get(f"/deployments/{args.object_id}")
        _print_envelope(
            envelope, as_json, default_render=lambda data: print(json.dumps(data, indent=2, ensure_ascii=False))
        )
        return EXIT_OK

    if args.deployments_command == "verify":
        envelope = client.post(f"/deployments/{args.object_id}/verify")
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"deployment {data['id']}: status={_fmt(data['status'])} ready={_fmt(data['ready'])}"
            ),
        )
        return EXIT_OK
    raise CliError(EXIT_USAGE, f"unknown deployments subcommand: {args.deployments_command}")


def cmd_endpoints(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    if args.endpoints_command == "list":
        envelope = client.get("/endpoints")

        def render(data: dict) -> None:
            rows = [
                [
                    item["id"],
                    item["name"],
                    _fmt(item["owner"]),
                    _fmt(item["visibility"]),
                    _fmt(item["status"]),
                    _fmt(item["shareable"]),
                    _fmt(item.get("route_path")),
                ]
                for item in data["items"]
            ]
            _table(rows, ["ID", "NAME", "OWNER", "VISIBILITY", "STATUS", "SHAREABLE", "ROUTE"])
            print(f"{data['count']} endpoint(s)")

        _print_envelope(envelope, as_json, default_render=render)
        return EXIT_OK

    if args.endpoints_command == "register":
        envelope = client.post(
            "/endpoints",
            {"name": args.name, "deployment_id": args.deployment},
            idempotency_key=args.idempotency_key,
        )
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"registered endpoint {data['id']} ({data['name']})\n"
                f"  visibility: {_fmt(data['visibility'])}  (默认私有；共享走 share)\n"
                f"  status    : {_fmt(data['status'])}  shareable={_fmt(data['shareable'])}\n"
                f"  route     : {_fmt(data.get('route_path'))}"
            ),
        )
        return EXIT_OK

    if args.endpoints_command == "get":
        envelope = client.get(f"/endpoints/{args.object_id}")
        _print_envelope(
            envelope, as_json, default_render=lambda data: print(json.dumps(data, indent=2, ensure_ascii=False))
        )
        return EXIT_OK

    if args.endpoints_command == "verify":
        envelope = client.post(f"/endpoints/{args.object_id}/verify")
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"endpoint {data['id']}: status={_fmt(data['status'])} shareable={_fmt(data['shareable'])}"
            ),
        )
        return EXIT_OK

    if args.endpoints_command == "share":
        # 先按**原始**取值判断再归一化：反过来会把 'all-approved' 先变成
        # 'all_approved'，于是那句相等判断永远不成立。
        if args.mode == "all-approved":
            mode = "all_approved_platform_users"
        else:
            mode = args.mode
        body: dict = {"mode": mode}
        if args.grantee:
            body["grantees"] = list(args.grantee)
        envelope = client.post(f"/endpoints/{args.object_id}/share", body)
        meta = envelope.get("meta") or {}
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"endpoint {data['id']} shared:\n"
                f"  mode      : {_fmt(data['visibility'])}\n"
                f"  grantees  : {_fmt(meta.get('grantees'))}\n"
                f"  ready     : {_fmt(data['shareable'])}\n"
                f"  revoked previously: {_fmt(meta.get('revoked_previously'))}"
            ),
        )
        return EXIT_OK

    if args.endpoints_command == "unshare":
        body = {"grantee": args.grantee} if args.grantee else {}
        envelope = client.post(f"/endpoints/{args.object_id}/unshare", body)
        meta = envelope.get("meta") or {}
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"endpoint {data['id']} now {_fmt(data['visibility'])} "
                f"(removed={_fmt(meta.get('removed'))})"
            ),
        )
        return EXIT_OK

    if args.endpoints_command == "acl":
        envelope = client.get(f"/endpoints/{args.object_id}/acl")
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"endpoint {data['endpoint_id']} acl:\n"
                f"  mode     : {_fmt(data['mode'])}\n"
                f"  grantees : {_fmt(data['grantees'])}\n"
                f"  ready    : {_fmt(data['ready'])}"
            ),
        )
        return EXIT_OK

    if args.endpoints_command == "routings":
        envelope = client.get(f"/endpoints/{args.object_id}/routings")

        def render_routings(data: dict) -> None:
            rows = [
                [
                    str(item["revision"]),
                    _fmt(item.get("model_name")),
                    _fmt(item.get("previous_model_name")),
                    _fmt(item["deployment_id"]),
                    _fmt(item["activated_at"]),
                    _fmt(item["actor"]),
                ]
                for item in data["items"]
            ]
            _table(
                rows,
                ["REV", "MODEL", "WAS", "DEPLOYMENT", "ACTIVATED AT", "ACTOR"],
            )
            print(f"route path: {_fmt(data['route_path'])}  current revision: {data['current_revision']}")

        _print_envelope(envelope, as_json, default_render=render_routings)
        return EXIT_OK

    if args.endpoints_command in ("repoint", "rollback"):
        if args.endpoints_command == "repoint":
            # --deployment 由 argparse 强制（required=True），这里不必再查一次。
            body: dict = {"target_deployment_id": args.deployment}
        else:
            body = {}
        # CAS：没有 --expected-revision 就先读一次；给了就照用（脚本要精确控制竞争时用）。
        if args.expected_revision is not None:
            body["expected_revision"] = args.expected_revision
        else:
            current = client.get(f"/endpoints/{args.object_id}")["data"]
            body["expected_revision"] = current["revision"]
        if args.reason:
            body["reason"] = args.reason
        if args.regrant:
            body["reauthorize"] = {"mode": "users", "grantees": list(args.regrant)}

        envelope = client.post(f"/endpoints/{args.object_id}/{args.endpoints_command}", body)
        meta = envelope.get("meta") or {}

        def render_switch(data: dict, meta: dict = meta) -> None:
            print(
                f"{meta.get('operation')}: {_fmt(meta.get('previous_model_id'))} -> "
                f"{_fmt(meta.get('model_id'))}\n"
                f"  route path: {_fmt(data['route_path'])}  (alias 不变)\n"
                f"  revision  : {_fmt(meta.get('revision_from'))} -> {_fmt(meta.get('revision_to'))}\n"
                f"  visibility: {_fmt(meta.get('visibility'))}  "
                f"granted={_fmt(meta.get('granted'))} "
                f"revoked={_fmt(meta.get('revoked'))} "
                f"reestablished={_fmt(meta.get('reestablished'))}\n"
                f"  router    : {_fmt(meta.get('router_sync'))}  "
                f"applied={_fmt(meta.get('router_applied'))}"
            )

        _print_envelope(envelope, as_json, default_render=render_switch)
        return EXIT_OK

    if args.endpoints_command == "routing-applied":
        if args.generation is None:
            raise CliError(EXIT_USAGE, "--generation is required")
        envelope = client.post(
            f"/endpoints/{args.object_id}/routing-applied", {"generation": args.generation}
        )
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"endpoint {data['id']}: router sync "
                f"{_fmt(data['router_sync']['state'])} "
                f"(generation {_fmt(data['router_generation'])}, "
                f"evidence {_fmt(data['router_sync'].get('evidence'))})"
            ),
        )
        return EXIT_OK
    raise CliError(EXIT_USAGE, f"unknown endpoints subcommand: {args.endpoints_command}")


def cmd_usage(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    """``usage --mine`` / ``logs --mine``：**没接上就是 NOT_CONFIGURED**。"""
    if not getattr(args, "mine", False):
        raise CliError(
            EXIT_USAGE,
            "this command only supports --mine in this build",
            hint="llmlab usage --mine",
        )
    envelope = client.get("/usage/mine")

    def render(data: dict) -> None:
        print(f"status: {_fmt(data.get('status'))}  reason: {_fmt(data.get('reason'))}")
        rows = []
        for name, metric in (data.get("metrics") or {}).items():
            rows.append(
                [
                    name,
                    _fmt(metric.get("value")),
                    _fmt(metric.get("state")),
                    _fmt(metric.get("source")),
                    _fmt(metric.get("unit")),
                ]
            )
        _table(rows, ["METRIC", "VALUE", "STATE", "SOURCE", "UNIT"])
        for line in data.get("notes") or []:
            print(f"· {line}")

    _print_envelope(envelope, as_json, default_render=render)
    return EXIT_OK


def cmd_invoke(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    if args.payload and args.prompt:
        raise CliError(EXIT_USAGE, "use either --payload or --prompt, not both")
    if args.payload:
        try:
            body = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            raise CliError(EXIT_USAGE, f"--payload is not valid JSON: {exc.msg}") from None
        if not isinstance(body, dict):
            raise CliError(EXIT_USAGE, "--payload must be a JSON object")
    elif args.prompt is not None:
        body = {"prompt": args.prompt}
    else:
        body = {}
    envelope = client.post(f"/endpoints/{args.object_id}/invoke", body)
    _print_envelope(
        envelope,
        as_json,
        default_render=lambda data: print(
            f"invoked endpoint {data['endpoint_id']} (model {_fmt(data['model'])})\n"
            f"  gateway   : {_fmt((data.get('gateway') or {}).get('adapter'))} "
            f"attached={_fmt((data.get('gateway') or {}).get('attached'))}\n"
            f"  metering  : tokens={_fmt((data.get('metering') or {}).get('tokens'))} "
            f"latency={_fmt((data.get('metering') or {}).get('latency_ms'))}\n"
            f"  result    : {json.dumps((data.get('result') or {}), ensure_ascii=False)}"
        ),
    )
    return EXIT_OK


def cmd_routing(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    """``routing``：管理员导出 Gateway 快照，或读只读路由契约。

    两条都**只是读**：本仓库不写任何 Gateway。``snapshot`` 是写给 Gateway 的
    ``llmlab.endpoint-routing/1``（并列仓库 PR #20 的形状），``config`` 是本仓库
    自己的只读契约。两者都需要 ``registry:admin:read``。
    """
    sub = getattr(args, "routing_command", None)
    if sub == "config":
        envelope = client.get("/routing-config")
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"contract  : {_fmt(data.get('contract'))} read_only={_fmt(data.get('read_only'))}\n"
                f"entries   : {len(data.get('entries') or [])} "
                f"excluded={_fmt(data.get('excluded_endpoints'))}\n"
                f"pending   : {_fmt((data.get('pending_router_sync') or {}).get('count'))} "
                "endpoint(s) awaiting a router ack"
            ),
        )
        return EXIT_OK
    if sub == "snapshot":
        envelope = client.get("/routing-snapshot")
        _print_envelope(
            envelope,
            as_json,
            default_render=lambda data: print(
                f"schema    : {_fmt(data.get('schema'))}\n"
                f"revision  : {_fmt(data.get('revision'))} "
                f"generated_at={_fmt(data.get('generated_at'))}\n"
                f"engines   : {len(data.get('engines') or [])} "
                f"endpoints={len(data.get('endpoints') or [])}\n"
                f"router    : attached={_fmt((data.get('router') or {}).get('attached'))} "
                f"adapter={_fmt((data.get('router') or {}).get('adapter'))}\n"
                f"excluded  : {json.dumps(data.get('excluded') or [], ensure_ascii=False)}"
            ),
        )
        return EXIT_OK
    raise CliError(
        EXIT_USAGE,
        "routing needs a subcommand",
        hint="llmlab routing config | snapshot",
    )


def cmd_status(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    envelope = client.get("/status")
    _print_envelope(
        envelope,
        as_json,
        default_render=lambda data: print(
            f"registry   : {_fmt(data['registry']['state'])} "
            f"mode={_fmt(data['registry']['mode'])} schema={_fmt(data['registry']['schema_version'])}\n"
            f"observation: prober={_fmt(data['observation']['prober_attached'])} "
            f"reason={_fmt(data['observation']['reason'])}\n"
            f"gateway    : attached={_fmt(data['gateway']['attached'])} "
            f"adapter={_fmt(data['gateway']['adapter'])} reason={_fmt(data['gateway']['reason'])}\n"
            f"router     : attached={_fmt((data.get('router') or {}).get('attached'))} "
            f"adapter={_fmt((data.get('router') or {}).get('adapter'))} "
            f"reason={_fmt((data.get('router') or {}).get('reason'))}\n"
            f"usage pipe : state={_fmt(data['usage_pipeline']['state'])} "
            f"reason={_fmt(data['usage_pipeline']['reason'])}\n"
            f"counts     : {json.dumps(data['counts'], ensure_ascii=False)}"
        ),
    )
    return EXIT_OK


def _print_agent_reports(payload: dict) -> None:
    """``discover`` 里的「别人自己报上来的」那一块。没有就什么都不打。"""

    reports = payload.get("agent_reports") or []
    if not reports:
        return
    if payload.get("agent_reports_scope") == "all":
        heading = "agent reports (every user on this host):"
    else:
        heading = "agent reports (yours; an admin sees everyone's):"
    print(heading)
    rows = []
    for report in reports:
        counts = report.get("counts") or {}
        age = report.get("age_seconds")
        rows.append(
            [
                _fmt(report.get("linux_user")),
                f"uid {report.get('uid')}",
                _fmt(report.get("host_id")),
                _fmt(report.get("agent_id")),
                f"{counts.get('engines', 0)}/{counts.get('installed_engines', 0)}/{counts.get('models', 0)}",
                NULL if age is None else f"{int(age) // 60}m",
                "stale" if report.get("stale") else "fresh",
            ]
        )
    _table(rows, ["USER", "UID", "HOST", "AGENT", "RUN/DISK/MODELS", "AGE", "STATE"])
    print("note: a report is a discovery, not a registration; registering still goes")
    print("      through the same ownership checks as `engines register` / `models register`.")


def cmd_discover(client: ApiClient, args: argparse.Namespace, as_json: bool) -> int:
    """``discover``：本机候选清单；``--register`` / ``--register-model`` 直接登记其中一个。

    登记走与 ``engines register`` / ``models register`` 完全相同的接口：所有权、
    运行用户记录与「一个地址只归一个账户」都由服务端判定。
    """
    envelope = client.get("/discovery")
    data = envelope.get("data") or {}

    if args.register is not None:
        found = next((item for item in data.get("engines", []) if item.get("port") == args.register), None)
        if found is None:
            raise CliError(EXIT_USAGE, f"no candidate inference service on port {args.register}",
                           hint="run `llmlab discover` to see what is listening")
        name = args.name or found.get("suggested_name")
        if not name:
            raise CliError(EXIT_USAGE, "no suggested name for this service", hint="pass --name")
        body: dict = {"name": name, "target_url": found["target_url"], "protocol": "openai_compatible"}
        if found.get("framework"):
            body["tags"] = [f"framework:{found['framework']}"]
        created = client.post("/engines", body)
        _print_envelope(
            created,
            as_json,
            default_render=lambda payload: print(
                f"registered engine {payload['id']} ({payload['name']}) on port {args.register}; "
                f"run as {_fmt((payload.get('ownership') or {}).get('linux_user'))}"
            ),
        )
        return EXIT_OK

    if args.register_model is not None:
        found = next((item for item in data.get("models", []) if item.get("path") == args.register_model), None)
        if found is None:
            raise CliError(EXIT_USAGE, f"no candidate model at {args.register_model}",
                           hint="run `llmlab discover` to see the candidates under the configured roots")
        body = {"name": args.name or found["suggested_name"], "weight_path": found["path"], "format": found["format"]}
        created = client.post("/models", body)
        _print_envelope(
            created,
            as_json,
            default_render=lambda payload: print(f"registered model {payload['id']} ({payload['name']})"),
        )
        return EXIT_OK

    def render(payload: dict) -> None:
        configured = payload.get("configured")
        if not configured:
            print(
                "this deployment does not scan its own host; register manually with "
                "`engines register` / `models register`"
            )
        else:
            engines = payload.get("engines") or []
            rows = [
                [
                    str(item["port"]),
                    _fmt(item.get("run_as_user") or item.get("run_as_uid")),
                    _fmt(item.get("framework")),
                    ", ".join(item.get("models") or [])
                    or ("—" if item.get("openai_compatible") else "not OpenAI-compatible"),
                    "mine"
                    if item.get("registered_by_me")
                    else ("taken" if item.get("registered") else "available"),
                ]
                for item in engines
            ]
            print("inference services:")
            _table(rows, ["PORT", "RUN AS", "FRAMEWORK", "MODELS", "STATE"])
            models = payload.get("models") or []
            rows = [
                [
                    item["path"],
                    item["format"],
                    _fmt(item.get("architecture")),
                    f"{item.get('size_bytes', 0) / 1024**3:.1f} GiB",
                    "registered" if item.get("registered") else "available",
                ]
                for item in models
            ]
            print("models:")
            _table(rows, ["PATH", "FORMAT", "ARCH", "SIZE", "STATE"])
            installed = payload.get("installed_engines") or []
            rows = [
                [
                    item["name"],
                    _fmt(item.get("version")),
                    item.get("found_as", "—"),
                    item["path"],
                ]
                for item in installed
            ]
            print("installed engines (on disk, running or not):")
            _table(rows, ["ENGINE", "VERSION", "FOUND AS", "PATH"])
            for problem in payload.get("problems") or []:
                print(f"note: {problem}")

        # 别人的代理报上来的事实。它**不依赖**本机发现：服务端读不到那个人的
        # HOME，是那个人自己报的。所以这一块在 configured=false 时也要打印。
        _print_agent_reports(payload)
        channel = payload.get("agent_channel") or {}
        if channel and not channel.get("listening"):
            print(f"agent channel: off ({channel.get('problem') or 'not started'})")
        elif channel.get("listening"):
            print(f"agent channel: {channel.get('path')} (listening)")

        if configured:
            print(
                "register one with: llmlab discover --register PORT [--name NAME]  |  "
                "--register-model PATH"
            )

    _print_envelope(envelope, as_json, default_render=render)
    return EXIT_OK


#: ``agent --watch`` 的最小间隔。扫描会走目录树，太勤没有意义。
MIN_AGENT_INTERVAL = 10.0
#: 连续失败多少次后放弃（``--watch`` 下）。断线重试是常态，但死循环不是。
MAX_AGENT_FAILURES = 5


def cmd_agent(args: argparse.Namespace, as_json: bool) -> int:
    """``agent``：以**自己的**身份把本机事实报给平台。**不需要 token。**

    这是唯一一条不走 HTTP 的命令：身份由平台的 unix socket 用 ``SO_PEERCRED``
    从内核取出，所以没有凭据可配，也没有 ``--token``。
    """

    from . import agent as agent_mod

    socket_path = args.socket or agent_mod.DEFAULT_SOCKET
    roots = tuple(args.root) if args.root else None
    interval = max(MIN_AGENT_INTERVAL, float(args.interval))

    def once() -> None:
        payload = agent_mod.collect_report(agent_id=args.agent_id, roots=roots)
        if args.dry_run:
            agent_mod.describe(payload)
            return
        receipt = agent_mod.send_report(payload, socket_path=socket_path)
        if as_json:
            print(json.dumps({"report": payload, "receipt": receipt}, ensure_ascii=False, sort_keys=True))
        else:
            print(agent_mod.human_summary(payload, receipt))

    def run_claimed_tasks() -> None:
        """领一轮任务并逐条执行、回报。身份与上报完全相同：socket 的对端 uid。"""

        for task in agent_mod.claim_tasks(socket_path=socket_path):
            task_id = str(task.get("id") or "")
            # run_task 自己把「没有可执行入口」/「unit 非法」/执行器失败都变成
            # failed 结果，所以这里不需要按异常分支处理任务本身。
            outcome = agent_mod.run_task(task)
            try:
                agent_mod.send_task_result(
                    task_id,
                    outcome["state"],
                    outcome.get("result"),
                    socket_path=socket_path,
                )
            except CliError as exc:
                # 结果送不回去只影响这一条；watch 循环要继续，下一轮再上报。
                print(f"llmlab agent: task {task_id}: {exc.message}", file=sys.stderr)
                if exc.hint:
                    print(f"llmlab agent: hint: {exc.hint}", file=sys.stderr)
            if as_json:
                print(json.dumps(
                    {"task": task_id, "state": outcome["state"]},
                    ensure_ascii=False,
                    sort_keys=True,
                ))
            else:
                print(f"task {task_id}: {outcome['state']}")

    if not args.watch:
        once()
        return EXIT_OK

    failures = 0
    try:
        while True:
            try:
                once()
                failures = 0
            except CliError as exc:
                failures += 1
                print(f"llmlab agent: {exc.message}", file=sys.stderr)
                if exc.hint:
                    print(f"llmlab agent: hint: {exc.hint}", file=sys.stderr)
                if failures >= MAX_AGENT_FAILURES:
                    print(
                        f"llmlab agent: giving up after {failures} consecutive failures",
                        file=sys.stderr,
                    )
                    raise
            if not args.dry_run:
                try:
                    run_claimed_tasks()
                except CliError as exc:
                    # 「领取失败」只影响这一轮：不累计 failures，也不退出循环。
                    print(f"llmlab agent: task claim failed: {exc.message}", file=sys.stderr)
                    if exc.hint:
                        print(f"llmlab agent: hint: {exc.hint}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        return EXIT_OK
