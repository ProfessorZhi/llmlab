"""本地开发/测试用的**临时** loopback 服务。

必须先说清楚它不是什么：

* **不是常驻服务。** 仓库硬约束（``docs/access-boundaries.md`` §6）写着
  「本仓库不启动任何常驻服务或后台进程」。所以这里没有 daemon 化、没有 systemd
  单元、没有 ``--background``。它就是一个前台进程，用完 Ctrl-C，
  只服务于「本地跑一遍看它能不能用」。
* **不是生产入口。** 生产接线是 Caddy -> Gateway -> 后端，属于独立部署动作。
* **不监听对外地址。** 只允许 ``127.0.0.1`` / ``::1``。给它 ``0.0.0.0`` 会直接拒绝：
  那不是一个「地址」，是一个把服务暴露给整个内网的意图。

合成身份（``--synthetic-identities``）会**打印**每个主体的一次性 token 到 stderr，
并显式标注 SYNTHETIC。这些 token 只存在于该进程的内存里：不落盘、不进 git、
重启即失效。刻意如此 —— 一个会被写进文件的长期凭据，正是本仓库反复拒绝的东西。
"""

from __future__ import annotations

import argparse
import json
import secrets
import socketserver
import sys
import wsgiref.simple_server
from dataclasses import dataclass

from .api import create_app
from .gateway import (
    DetachedGatewayAdapter,
    FileGatewayKeyResolver,
    MockGatewayAdapter,
    TrustedGatewayAdapter,
)
from .health import LoopbackHttpProber, LoopbackProtocolProber
from .identity import (
    SCOPE_ADMIN_MANAGE,
    SCOPE_ADMIN_READ,
    SCOPE_ENDPOINT_INVOKE,
    SCOPE_ENDPOINT_SHARE,
    SCOPE_REGISTRY_READ,
    SCOPE_REGISTRY_WRITE,
    Principal,
    PrincipalDirectory,
    TokenBinding,
    TokenDirectory,
)
from .ingest_runner import IngestRunner
from .ingest_scheduler import IngestScheduler
from .ownership import (
    EMPTY_POLICY,
    OWNERSHIP_PROVER_KINDS,
    OWNERSHIP_PROVER_NONE,
    OperatorPolicy,
    load_operator_policy,
)
from .registry import RegistryConfig, RegistryService
from .router import FileRouterAdapter
from .store import Store

#: 只允许这些绑定地址。``0.0.0.0`` / ``::`` 会被显式拒绝并说明原因。
ALLOWED_BIND_HOSTS = ("127.0.0.1", "::1")


class _ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    """每请求一线程的 WSGI server。纯标准库，不引入外部依赖。

    ``daemon_threads`` 让 Ctrl-C 能立刻返回，而不是等长连接结束；
    ``allow_reuse_address`` 让反复起停开发进程不会被 TIME_WAIT 卡住。
    """

    daemon_threads = True
    allow_reuse_address = True

DEFAULT_PORT = 8787

#: 开发服务器使用的**合成**账号表（见 docs/registry-cli-contract.md §11）。
#:
#: 这些 UID 不对应任何真实主机账户；``dana`` 刻意不在表内，用来证明登录名
#: 不会被折叠成 ``bob``。
HOST_USERS = (
    ("carol", 4000),
    ("alice", 4001),
    ("dave", 4002),
    ("bob", 4003),
)

COMMON_SCOPES = frozenset(
    {
        SCOPE_REGISTRY_READ,
        SCOPE_REGISTRY_WRITE,
        SCOPE_ENDPOINT_SHARE,
        SCOPE_ENDPOINT_INVOKE,
    }
)

#: 运维/管理员：全局只读 + 可授权管理 + **登记权**。
#:
#: 最后一条是必需的：把一台**既有**服务（例如那台已经在 8080 上跑着的 Strata）
#: 登记进来本身就是一次写操作。系统服务登记要求「能登记」与「有权承认既有服务」
#: 同时具备 —— 只有 admin_manage 的人不该能凭空造出一行 engine。
ADMIN_SCOPES = frozenset(
    {
        SCOPE_REGISTRY_READ,
        SCOPE_REGISTRY_WRITE,
        SCOPE_ADMIN_READ,
        SCOPE_ADMIN_MANAGE,
    }
)


@dataclass(frozen=True)
class SyntheticIdentities:
    """合成凭据集合。``token`` -> 主体/scope 的映射只活在这个对象里。"""

    principals: PrincipalDirectory
    tokens: TokenDirectory
    issued: tuple[tuple[str, str, tuple[str, ...]], ...]

    def render(self) -> str:
        lines = [
            "SYNTHETIC IDENTITIES —— 仅供本地开发，非生产凭据，进程退出即失效",
            "=" * 72,
        ]
        for principal, token, scopes in self.issued:
            bound = self.principals.lookup(principal)
            uid = bound.uid if bound is not None else None
            lines.append(f"{principal:<10} uid={uid!s:<5} token={token}")
            lines.append(f"{'':<10} scopes={' '.join(scopes)}")
        lines.append("=" * 72)
        lines.append("把这些 token 给 LLMLAB_TOKEN 使用，例如：")
        lines.append("  LLMLAB_TOKEN=<token> python3 -m cli.llmlab whoami --json")
        return "\n".join(lines)


def build_synthetic_identities() -> SyntheticIdentities:
    """造一份**合成**身份。所有 token 随机生成、只存内存。"""
    principals = PrincipalDirectory(
        [Principal(name, name, uid) for name, uid in HOST_USERS]
    )
    bindings: list[TokenBinding] = []
    issued: list[tuple[str, str, tuple[str, ...]]] = []
    for name, _uid in HOST_USERS:
        scopes = ADMIN_SCOPES if name == "carol" else COMMON_SCOPES
        token = f"synthetic-{name}-{secrets.token_hex(12)}"
        bindings.append(
            TokenBinding(token, name, scopes, roles=frozenset(), label="synthetic-dev")
        )
        issued.append((name, token, tuple(sorted(scopes))))
    return SyntheticIdentities(
        principals=principals,
        tokens=TokenDirectory(bindings),
        issued=tuple(issued),
    )


def rebound_identities(
    identities: SyntheticIdentities, overrides: dict[str, int]
) -> SyntheticIdentities:
    """把合成账户绑到**本机真实 uid** 上。

    默认的 4000–4003 不对应任何主机账户，这对登录测试是对的（不许把登录名折叠
    成某个真实用户），但自动接入要回答的是「这个端口属于哪个账户」——那必须用
    真正的 uid 才能命中 ``/proc`` 里的属主。token 不变，只换 uid 绑定。
    """

    principals = PrincipalDirectory(
        [Principal(name, name, overrides.get(name, uid)) for name, uid in HOST_USERS]
    )
    return SyntheticIdentities(
        principals=principals, tokens=identities.tokens, issued=identities.issued
    )


def parse_uid_bindings(items: list[str]) -> dict[str, int]:
    """``NAME=UID`` 列表 -> ``{name: uid}``。写错直接起不来，不静默忽略。"""

    known = {name for name, _uid in HOST_USERS}
    overrides: dict[str, int] = {}
    for raw in items or []:
        name, sep, uid_text = str(raw).partition("=")
        name, uid_text = name.strip(), uid_text.strip()
        if not sep or not name or not uid_text:
            raise SystemExit(f"--bind-uid expects NAME=UID, got {raw!r}")
        if name not in known:
            raise SystemExit(f"--bind-uid: unknown account {name!r}; known: {sorted(known)}")
        try:
            uid = int(uid_text)
        except ValueError:
            raise SystemExit(f"--bind-uid: uid must be an integer, got {uid_text!r}") from None
        if uid < 0:
            raise SystemExit(f"--bind-uid: uid must be >= 0, got {uid}")
        overrides[name] = uid
    return overrides


def build_service_from_env(
    *,
    database: str,
    identities: SyntheticIdentities | None,
    attach_loopback_prober: bool = False,
    mock_gateway: bool = False,
    mode: str = "synthetic",
    operator_policy: OperatorPolicy | None = None,
    production_posture: bool = False,
    trusted_gateway_base: str | None = None,
    gateway_key_file: str | None = None,
    router_snapshot: str | None = None,
    router_status: str | None = None,
    engine_secret_refs: dict | None = None,
    ownership_prover_kind: str = OWNERSHIP_PROVER_NONE,
) -> RegistryService:
    identities = identities or build_synthetic_identities()

    # 三条 adapter 路径，优先级从「最真实」到「最保守」：
    # 受信 Gateway（运维给了基址 + 调用者 key 目录）> mock 替身 > 缺省的 detached。
    # 任何一个开关缺参数都直接拒绝启动，不退化成「悄悄没接」。
    if trusted_gateway_base:
        if not gateway_key_file:
            raise SystemExit(
                "--trusted-gateway requires --gateway-keys: refusing to attach a gateway "
                "with no caller key directory (every call would be a 503 anyway)"
            )
        gateway: object = TrustedGatewayAdapter(
            base_url=trusted_gateway_base,
            keys=FileGatewayKeyResolver(gateway_key_file),
        )
    elif mock_gateway:
        gateway = MockGatewayAdapter()
    else:
        gateway = DetachedGatewayAdapter()

    # 路由同步：两个路径都给才挂；只给一个说明配置写错了。
    if bool(router_snapshot) != bool(router_status):
        raise SystemExit("--router-snapshot and --router-status must be given together")
    router: object | None = (
        FileRouterAdapter(snapshot_path=router_snapshot, status_path=router_status)
        if router_snapshot and router_status
        else None
    )

    extra: dict = {"ownership_prover_kind": ownership_prover_kind}
    if engine_secret_refs:
        extra["engine_secret_refs"] = dict(engine_secret_refs)
    config = (
        RegistryConfig.production_posture(
            mode=mode, operator_policy=operator_policy or EMPTY_POLICY, **extra
        )
        if production_posture
        else RegistryConfig(
            mode=mode, operator_policy=operator_policy or EMPTY_POLICY, **extra
        )
    )
    return RegistryService(
        store=Store(database),
        tokens=identities.tokens,
        principals=identities.principals,
        config=config,
        prober=LoopbackHttpProber() if attach_loopback_prober else None,
        # 协议轴跟着传输轴一起挂：挂了真的 prober 才可能读到真的模型清单。
        protocol_prober=LoopbackProtocolProber() if attach_loopback_prober else None,
        # 所有权 prover 由 ``RegistryConfig.ownership_prover_kind`` 选：缺省 ``none``
        # （fail-closed，只能靠运维策略承认既有服务）；``proc_net`` 读全员可读的
        # /proc/net/tcp(6) 的 uid 列（见 ownership.ProcNetOwnershipProver）。
        gateway=gateway,
        router=router,
    )


def _parse_engine_secret_refs(items: list) -> dict:
    """``ENGINE_ID=FILE:VAR`` -> ``{engine_id: {"file": ..., "var": ...}}``。

    只接受**位置**，不接受值：一份带内联 key 的快照会把生产凭据写进 Registry
    生成的文件里。任何一段为空都直接拒绝启动。
    """
    refs: dict = {}
    for raw in items or []:
        if not isinstance(raw, str) or "=" not in raw:
            raise SystemExit(f"--engine-secret-ref must be ENGINE_ID=FILE:VAR, got {raw!r}")
        engine_id, target = raw.split("=", 1)
        if ":" not in target:
            raise SystemExit(f"--engine-secret-ref target must be FILE:VAR, got {target!r}")
        file_part, var = target.rsplit(":", 1)
        engine_id, file_part, var = engine_id.strip(), file_part.strip(), var.strip()
        if not engine_id or not file_part or not var:
            raise SystemExit(f"--engine-secret-ref has an empty part: {raw!r}")
        refs[engine_id] = {"file": file_part, "var": var}
    return refs


def validate_bind_host(host: str) -> str:
    if host not in ALLOWED_BIND_HOSTS:
        raise SystemExit(
            f"refusing to bind {host!r}: only {ALLOWED_BIND_HOSTS} are allowed. "
            "0.0.0.0 exposes the service to the whole network; that is a deployment "
            "decision, not a development convenience."
        )
    return host


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.registry",
        description="临时 loopback Registry 开发服务（非常驻、非生产）",
    )
    parser.add_argument("command", choices=["serve", "print-identities"], default="serve", nargs="?")
    parser.add_argument("--db", default=":memory:", help="SQLite 路径（默认内存库）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--probe-loopback",
        action="store_true",
        help="启用真实的 loopback 探测（默认关闭 = 所有健康为 UNKNOWN）",
    )
    parser.add_argument(
        "--mock-gateway",
        action="store_true",
        help="挂一个测试用 mock gateway（默认未接线，invoke 会诚实返回 503）",
    )
    parser.add_argument(
        "--trusted-gateway",
        help="受信 Gateway 基址（只允许回环 http，例如 http://127.0.0.1:8200）；"
        "配 --gateway-keys 使用。挂上之后 invoke 会真的转发给它",
    )
    parser.add_argument(
        "--gateway-keys",
        help="调用者 key 目录（JSON {principal: key}，须 0600 属主本人普通文件、非软链）",
    )
    parser.add_argument(
        "--router-snapshot",
        help="路由快照写入路径（Gateway 读它）；必须与 --router-status 同时给",
    )
    parser.add_argument(
        "--router-status",
        help="Gateway 发布的 status.json 路径（用来读回生效的代数与 sha256）",
    )
    parser.add_argument(
        "--engine-secret-ref",
        action="append",
        default=[],
        metavar="ENGINE_ID=FILE:VAR",
        help="快照里某个 engine 的 secret_ref（只点名文件与变量，不含值）；可重复",
    )
    parser.add_argument(
        "--operator-policy",
        help="运维策略文件（root/服务账号拥有的 JSON）：承认哪些既有服务可以被登记",
    )
    parser.add_argument(
        "--ownership-prover",
        choices=OWNERSHIP_PROVER_KINDS,
        default=OWNERSHIP_PROVER_NONE,
        help="所有权 prover：none（缺省，fail-closed）或 proc_net（读 /proc/net/tcp(6) "
        "的 uid 列；只经 fixture 单测验证，真实主机验证待做）",
    )
    parser.add_argument(
        "--production-posture",
        action="store_true",
        help="打开所有权强制与协议验证（本地开发默认关闭）",
    )
    parser.add_argument(
        "--auto-ingest",
        action="store_true",
        help="每轮扫 /proc/net/tcp，把本机账户上正在跑的 OpenAI 兼容服务接进账本（§4.1）。"
        "默认关闭：它会读全机的回环监听端口，不是每个开发场景都想要",
    )
    parser.add_argument(
        "--ingest-interval",
        type=float,
        default=30.0,
        help="自动接入的周期秒数（默认 30，与设计文档一致；调小只是为了演示）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出身份清单")
    parser.add_argument(
        "--bind-uid",
        action="append",
        default=[],
        metavar="NAME=UID",
        help="把合成账户绑到本机真实 uid（可重复）。默认的 4000–4003 在 /proc 里不存在，"
        "自动接入看不见任何东西；把它绑成本机 uid 才能真的接上正在跑的服务",
    )
    args = parser.parse_args(argv)

    host = validate_bind_host(args.host)
    identities = build_synthetic_identities()
    uid_overrides = parse_uid_bindings(args.bind_uid)
    if uid_overrides:
        identities = rebound_identities(identities, uid_overrides)

    if args.command == "print-identities":
        if args.json:
            print(
                json.dumps(
                    {
                        "synthetic": True,
                        "identities": [
                            {
                                "principal": p,
                                "uid": getattr(identities.principals.lookup(p), "uid", None),
                                "token": t,
                                "scopes": list(s),
                            }
                            for p, t, s in identities.issued
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(identities.render(), file=sys.stderr)
        return 0

    # 策略文件加载失败**必须**让进程起不来：一个「悄悄没读到策略」的服务
    # 会让人以为 8080 已经可以被登记了。
    policy = load_operator_policy(args.operator_policy) if args.operator_policy else None
    secret_refs = _parse_engine_secret_refs(args.engine_secret_ref)
    service = build_service_from_env(
        database=args.db,
        identities=identities,
        attach_loopback_prober=args.probe_loopback,
        mock_gateway=args.mock_gateway,
        operator_policy=policy,
        production_posture=args.production_posture,
        trusted_gateway_base=args.trusted_gateway,
        gateway_key_file=args.gateway_keys,
        router_snapshot=args.router_snapshot,
        router_status=args.router_status,
        engine_secret_refs=secret_refs,
        ownership_prover_kind=args.ownership_prover,
    )
    application = create_app(service)
    print(identities.render(), file=sys.stderr)

    # §4.1 的循环。默认不挂：它读全机回环端口，是一个应该有意识打开的开关。
    scheduler = None
    if args.auto_ingest:
        if args.ingest_interval <= 0:
            raise SystemExit("--ingest-interval must be positive")
        scheduler = IngestScheduler(
            runner=IngestRunner(store=service.store, directory=service.principals),
            interval_seconds=args.ingest_interval,
        )
        scheduler.start()
        print(
            f"  auto_ingest=on  interval={args.ingest_interval:g}s  "
            f"uids={sorted(service.principals.uids())}  "
            "（只认目录里绑定了 uid 的账户；未映射 uid 的端口不探测）",
            file=sys.stderr,
        )
    print(
        f"\n临时 Registry 监听 http://{host}:{args.port}/api/v2/registry\n"
        f"  mode={service.config.mode}  prober={'on' if service.prober else 'off'}  "
        f"gateway={getattr(service.gateway, 'name')}\n"
        f"  router={getattr(service.router, 'name')}  "
        f"engine_secret_refs={len(service.config.engine_secret_refs)}\n"
        f"  ownership_policy={service.config.ownership_policy}  "
        f"ownership_prover={getattr(service.ownership_prover, 'name', 'unknown')}  "
        f"require_protocol_verification={service.config.require_protocol_verification}\n"
        f"  operator_policy={service.operator_policy.provenance}\n"
        "  这是前台开发进程：Ctrl-C 结束，不要当作服务部署。",
        file=sys.stderr,
    )
    # 多线程：默认的 simple_server 是单线程的，一个请求里再发一个 loopback 请求
    # （健康探测、或另一个 CLI 并发调用）会被自己挡住，表现为「探测超时」——
    # 那会把「服务其实活着」误报成 unavailable。
    #
    # 本 Python 的 ``wsgiref.simple_server`` 里没有现成的多线程 server 类，
    # 所以在这里就地组合一个，而不是引入任何外部依赖。
    server = wsgiref.simple_server.make_server(
        host, args.port, application, server_class=_ThreadingWSGIServer
    )
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。", file=sys.stderr)
        finally:
            if scheduler is not None:
                scheduler.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
