"""本地开发/测试用的 loopback BFF 进程。与 ``backend.registry.devserver`` 同一套纪律。

* **不是常驻服务**，不 daemon 化、没有 systemd 单元；前台进程，Ctrl-C 即止。
* **只监听环回地址**。``0.0.0.0`` 会被拒绝——那是暴露给整个内网的意图，属于部署
  决策，不是开发便利。
* **拒绝监听 8080**。那是上游 Gateway 的端口（``registry.urls.RESERVED_UPSTREAM_PORTS``
  与前端 ``isInternalHref`` 都把它当保留值）。BFF 若占了它，浏览器就可能在同源
  下直连到没有授权的那一跳。
* 默认 ``mock`` Gateway + 无观测源：所有 GPU 指标是 UNKNOWN，试跑台关闭。

合成身份（``--synthetic-logins``）只会**打印**主体名与 uid，**绝不打印任何密码或
token**：本进程没有任何真实凭据可用（PAM 后端未实现），打印一个假的只会让人以为
它能用。
"""

from __future__ import annotations

import argparse
import socketserver
import sys
import wsgiref.simple_server
from dataclasses import replace

from ..auth import config as auth_config
from ..auth.routes import Application as AuthApplication
from ..auth_linux import accounts as linux_accounts
from ..auth_linux import policy as linux_policy
from ..auth_linux import service as linux_service_module
from ..auth_linux.handoff import linux_user_id
from ..evaluation import targets as eval_targets
from ..observability import engines as obs_engines
from ..observability import identity as obs_identity
from ..registry.gateway import DetachedGatewayAdapter, MockGatewayAdapter
from ..registry.identity import Principal, PrincipalDirectory, TokenDirectory
from ..registry.health import LoopbackHttpProber, LoopbackProtocolProber, StaticProber
from ..registry.ownership import ProcNetOwnershipProver
from ..registry.registry import RegistryConfig, RegistryService
from ..registry.store import Store
from . import origin as bff_origin
from .account_directory import AccountAwareDirectory
from . import stacks as bff_stacks
from .app import RESERVED_BIND_PORTS, OpsApplication
from .evaluation_adapter import ModuleEvaluationAdapter
from .observability_adapter import ModuleObservabilityAdapter, engine_registry_from_bff
from .ops import OpsConfig

ALLOWED_BIND_HOSTS = ("127.0.0.1", "::1")

#: 与 ``backend.registry.devserver`` 同一份合成 uid 表。``dana`` 不在这里 ——
#: 它**不是** ``bob`` 的别名，这一点由测试单独断言。
HOST_USERS = (("carol", 4000), ("alice", 4001), ("dave", 4002), ("bob", 4003))

DEFAULT_HOST_ID = "dell-test-01"
DEFAULT_PORT = 8790
DEFAULT_DATABASE = ":memory:"


class _ThreadingWSGIServer(socketserver.ThreadingMixIn, wsgiref.simple_server.WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


def validate_bind_host(host: str) -> str:
    if host not in ALLOWED_BIND_HOSTS:
        raise SystemExit(
            f"refusing to bind {host!r}: only {ALLOWED_BIND_HOSTS} are allowed. "
            "0.0.0.0 exposes the portal to the whole network; that is a deployment "
            "decision, not a development convenience."
        )
    return host


def validate_bind_port(port: int) -> int:
    if int(port) in RESERVED_BIND_PORTS:
        raise SystemExit(
            f"refusing to bind port {port}: it is reserved for the upstream gateway. "
            "The browser must never be able to reach an unauthorized hop on the same origin."
        )
    if not (0 < int(port) < 65536):
        raise SystemExit(f"refusing to bind port {port}: not a usable TCP port")
    return int(port)


def build_application(
    *,
    state_dir: str,
    database: str = DEFAULT_DATABASE,
    host_id: str = DEFAULT_HOST_ID,
    admin_names: tuple[str, ...] = (),
    mock_gateway: bool = True,
    prober: object | None = None,
    accounts: object | None = None,
    allowed_logins: tuple[str, ...] | None = None,
    trusted_authorities: tuple[str, ...] = (),
    observe_local_engines: bool = False,
) -> OpsApplication:
    """把三块拼起来。**只建对象，不开端口。**

    ``state_dir`` 必须显式给出：会话库落在哪里是一件要被看见的事，不是默认值。

    ``trusted_authorities`` 是 ``Host`` 权威的白名单。**默认空 = 只认环回**：
    本进程只绑环回地址，所以一份空名单就是「除了环回谁都不认」。放在反代后面用
    真实主机名运行，属于部署决策，由运维显式写进来——解析不出来的条目会让这里
    直接抛错，而不是被静默丢掉。
    """
    authority_policy = bff_origin.AuthorityPolicy.build(trusted_authorities)
    accounts = accounts if accounts is not None else linux_accounts.SystemPwdSource()

    auth_app = AuthApplication(_local_config(state_dir))
    # 建表 / 迁移；否则第一个请求会以 500 收场。
    auth_app.initialize()

    # 静态的合成 Linux 主体 + 管理员随时创建的平台账户（每次查询都读账户库）。
    principals = AccountAwareDirectory(
        PrincipalDirectory(
            [
                Principal(linux_user_id(host_id, uid), name, uid, display_name=name)
                for name, uid in HOST_USERS
            ]
        ),
        auth_app.service.accounts,
    )

    local = {}
    if observe_local_engines:
        # 只读本机事实：回环 HTTP 探测、/v1/models、/proc/net/tcp 的监听 uid。
        local = {
            "prober": prober if prober is not None else LoopbackHttpProber(),
            "protocol_prober": LoopbackProtocolProber(),
            "ownership_prover": ProcNetOwnershipProver(),
        }
    service = RegistryService(
        store=Store(database),
        tokens=TokenDirectory(()),
        principals=principals,
        config=RegistryConfig(mode="synthetic"),
        prober=local.get("prober", prober),
        protocol_prober=local.get("protocol_prober"),
        ownership_prover=local.get("ownership_prover"),
        gateway=MockGatewayAdapter() if mock_gateway else DetachedGatewayAdapter(),
    )

    observability = build_observability_adapter(service=service, accounts=accounts)
    evaluation = build_evaluation_adapter(service=service)

    linux_service = build_linux_service(
        host_id=host_id,
        admin_names=admin_names,
        accounts=accounts,
        allowed_logins=allowed_logins,
    )

    return OpsApplication(
        auth_app=auth_app,
        service=service,
        accounts=accounts,
        linux_service=linux_service,
        observability=observability,
        evaluation=evaluation,
        ops_config=OpsConfig(
            registry_mode="synthetic",
            gateway_adapter="mock" if mock_gateway else "detached",
            gateway_attached=mock_gateway,
        ),
        authority_policy=authority_policy,
    )


def _visible_endpoints(service):
    """``(identity) -> 该会话可见的 Endpoint id``。观测模块按 endpoint 判定可见性。"""

    def scope(identity) -> frozenset:
        from ..registry import errors as registry_errors

        try:
            return frozenset(
                item.id for item in service.policy(identity.requester).visible_endpoints()
            )
        except registry_errors.RegistryError:
            return frozenset()

    return scope


def build_observability_adapter(*, service, accounts, now=None):
    """装一个**合成**的观测数据面。

    与 ``dev/observability_preview.py`` 同样的口径：没有真实导出口，就用明确带
    ``fixture`` 标记的合成行；归属只来自受信 key 目录。读数因此一律 UNKNOWN
    （来源不在前端白名单），只有曲线带 SYNTHETIC。
    """
    from datetime import datetime, timezone

    moment = now or datetime.now(timezone.utc)
    key_directory = obs_identity.KeyDirectory(
        [
            obs_identity.GatewayKeyBinding(
                f"key-{name}", linux_user_id(host_id_of(), uid), "proj-lab", f"{name}-workload"
            )
            for name, uid in HOST_USERS
        ]
    )
    stack = bff_stacks.build_observability_stack(
        directory=key_directory,
        engine_registry=lambda identity: engine_registry_from_bff(
            service=service, identity=identity
        ),
        gateway_text=lambda identity: bff_stacks.demo_gateway_corpus(
            key_directory,
            now=moment,
            endpoints=sorted(_visible_endpoints(service)(identity)),
        ),
        provenance=bff_stacks.synthetic_provenance(),
        engine_probe=obs_engines.MockEngineProbe(default_up=True),
        host_report=bff_stacks.synthetic_host_report(moment),
    )
    return ModuleObservabilityAdapter(
        stack, endpoint_scope=_visible_endpoints(service), clock=lambda: moment
    )


def host_id_of() -> str:
    return DEFAULT_HOST_ID


def build_evaluation_adapter(*, service):
    """装一个**派发关闭**的评测面。

    目标目录在启动时是空的（此时还没有任何 deployment）。因此在这个进程里提交评测
    会以「目标未授权」收场 —— 这与「真实派发关闭」是两件不同的事，但结论一致：
    不会有任何真实评测跑起来。用 CLI 登记了 deployment 之后，需要重启该进程才会
    重新同步目标目录。
    """
    principals, _tokens = bff_stacks.principal_directories(
        host_id=DEFAULT_HOST_ID, users=HOST_USERS
    )
    projects = bff_stacks.project_directory(
        projects=(
            (
                "proj-lab",
                "Lab",
                [linux_user_id(DEFAULT_HOST_ID, uid) for _name, uid in HOST_USERS],
            ),
        ),
        home=((linux_user_id(DEFAULT_HOST_ID, uid), "proj-lab") for _name, uid in HOST_USERS),
    )
    eval_stack = bff_stacks.build_evaluation_stack(
        principals=principals,
        projects=projects,
        targets=eval_targets.empty_catalog(),
    )

    def lookup(deployment_id):
        if not isinstance(deployment_id, str) or not deployment_id:
            return None
        deployment = service.store.deployment_by_id(deployment_id)
        return deployment.model_id if deployment is not None else None

    return ModuleEvaluationAdapter(eval_stack, model_lookup=lookup)


def _local_config(state_dir: str):
    """开发进程跑在 ``http://127.0.0.1`` 上。

    ``Secure`` cookie 会让浏览器**根本不回传**它，于是出现「登录成功、下一个请求
    401」这种纯本地的假故障。所以这里显式关掉 ``cookie_secure``——这是本进程唯一
    偏离默认的一处，且只在环回地址上有意义。
    """
    cfg = auth_config.AuthConfig.from_env({}, state_dir=state_dir)
    return replace(cfg, cookie_secure=False)


def build_linux_service(
    *,
    host_id: str,
    admin_names: tuple[str, ...] = (),
    accounts=None,
    allowed_logins: tuple[str, ...] | None = None,
):
    """装一个**能失败得诚实**的 Linux 登录服务。

    两点刻意为之：

    * 策略与允许名单是**内存**造的。生产里那份是 root 拥有的文件；这个进程没有、
      也不假装有。默认**没有任何管理员**——``--platform-admin carol`` 才在本次进程
      的内存里加一条，且它随进程消失。
    * broker 指向一个不存在的 socket，因此每次登录都会以 503
      ``pam_unavailable`` 收场。这是本构建里唯一诚实的结局：真 PAM 后端没有实现
      （``backend/auth_linux/pam_backend.py`` 只提供 ``UnavailablePamBackend``），
      一个返回 ``authenticated`` 的测试替身**不能**当作「密码能用」的证据。
    """
    from ..auth_linux import broker_client, throttle as linux_throttle

    accounts = accounts if accounts is not None else linux_accounts.SystemPwdSource()
    names = allowed_logins if allowed_logins is not None else tuple(n for n, _ in HOST_USERS)
    allowlist = linux_policy.Allowlist(frozenset(names))
    entries = frozenset(
        (uid, name) for name, uid in HOST_USERS if name in set(admin_names)
    )
    admin_grant = linux_policy.AdminGrant(host_id=host_id, entries=entries)

    policy = linux_policy.AccountPolicy(
        allowlist=allowlist, host_id=host_id, admin_grant=admin_grant
    )
    provider = linux_service_module.LinuxPamAuthProvider(
        accounts=accounts,
        policy=policy,
        broker=broker_client.BrokerClient(
            broker_client.UnixSocketTransport(_unreachable_socket_path())
        ),
        throttle=linux_throttle.InMemoryThrottle(),
        host_id=host_id,
        audit=None,
        available=True,
    )
    return linux_service_module.LinuxAuthService(
        provider,
        host_id=host_id,
        allowlist=allowlist,
        admin_grant=admin_grant,
    )


def _unreachable_socket_path():
    """一个**故意不存在**的 socket 路径。

    不落在 ``/run``、``/srv``、``/opt`` 或任何需要 root 的位置；连接失败正是
    这里想要的结果。
    """
    import pathlib

    return pathlib.Path("/nonexistent/llm-lab-pam-broker.sock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m backend.bff",
        description="Loopback-only ops BFF for the V6 management UI. Not a service.",
    )
    parser.add_argument("--host", default=ALLOWED_BIND_HOSTS[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--state-dir",
        required=True,
        help="where the session database lives (required: first run must be explicit)",
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--host-id", default=DEFAULT_HOST_ID)
    parser.add_argument(
        "--platform-admin",
        action="append",
        default=[],
        metavar="LOGIN",
        help="grant platform-admin in this process only (in-memory; no root-owned file exists)",
    )
    parser.add_argument(
        "--with-static-prober",
        action="store_true",
        help="attach a test-double prober so endpoints can become verified",
    )
    parser.add_argument(
        "--observe-local-engines",
        action="store_true",
        help=(
            "probe registered loopback engines for real (HTTP, /v1/models) and read "
            "/proc/net/tcp to record which Linux user runs them; read-only"
        ),
    )
    parser.add_argument(
        "--synthetic-logins",
        action="store_true",
        help="print the synthetic identities (no passwords, no tokens)",
    )
    parser.add_argument(
        "--trusted-authority",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help=(
            "a Host authority this process answers to, e.g. portal.example.com:8443. "
            "Repeatable. Default: loopback only — a process behind a reverse proxy "
            "must name the authority it is served under, because an unconfigured "
            "deployment should not accept any external hostname 'because nobody "
            "configured one'."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host = validate_bind_host(args.host)
    port = validate_bind_port(args.port)

    app = build_application(
        state_dir=args.state_dir,
        database=args.database,
        host_id=args.host_id,
        admin_names=tuple(args.platform_admin),
        mock_gateway=True,
        prober=StaticProber() if args.with_static_prober else None,
        trusted_authorities=tuple(args.trusted_authority),
        observe_local_engines=args.observe_local_engines,
    )

    if args.synthetic_logins:
        print("synthetic logins on this host (no passwords, no tokens):", file=sys.stderr)
        for name, uid in HOST_USERS:
            role = "platform_admin" if name in set(args.platform_admin) else "member"
            print(f"  {name:10s} uid={uid:>5} role={role}", file=sys.stderr)
        print(
            "  real PAM is NOT implemented: a login attempt ends in 503 pam_unavailable.",
            file=sys.stderr,
        )

    server = wsgiref.simple_server.make_server(
        host, port, app, server_class=_ThreadingWSGIServer
    )
    print(f"ops BFF on http://{host}:{port}", file=sys.stderr)
    print(f"  GET  {host}:{port}/api/v2/ops", file=sys.stderr)
    print(f"  PUT  {host}:{port}/api/v2/ops/deployments/<id>/share", file=sys.stderr)
    print(f"  GET  {host}:{port}/api/v2/ops/usage", file=sys.stderr)
    print(f"  GET  {host}:{port}/api/v2/ops/engines", file=sys.stderr)
    print(f"  POST {host}:{port}/auth/linux/login", file=sys.stderr)
    print("  (no session until a Linux login succeeds; PAM is unavailable in this build)",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


__all__ = [
    "ALLOWED_BIND_HOSTS",
    "DEFAULT_HOST_ID",
    "DEFAULT_PORT",
    "HOST_USERS",
    "build_application",
    "main",
    "validate_bind_host",
    "validate_bind_port",
]
