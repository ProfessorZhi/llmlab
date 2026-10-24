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
import json
import os
from pathlib import Path
import socketserver
import sys
import wsgiref.simple_server
from dataclasses import replace

from ..auth import config as auth_config
from ..auth.routes import Application as AuthApplication
from ..auth.service import AuthService
from ..auth.store import SOURCE_LINUX
from ..auth.tokens import AuthStorePrincipalState, build_token_service
from ..auth_linux import accounts as linux_accounts
from ..auth_linux import policy as linux_policy
from ..auth_linux import service as linux_service_module
from ..auth_linux.handoff import linux_user_id
from ..evaluation import targets as eval_targets
from ..observability import engines as obs_engines
from ..observability import identity as obs_identity
from ..registry.gateway import DetachedGatewayAdapter, MockGatewayAdapter
from ..gateway.proxy import GatewayApplication, GatewayCore, parse_secret_refs
from ..gateway.service import GatewayService
from ..gateway.store import GatewayStore
from ..registry.api import RegistryApplication
from ..runtime.params import ParamStore
from ..registry.identity import Principal, PrincipalDirectory, TrustedTokenIntrospector
from ..registry.health import LoopbackHttpProber, LoopbackProtocolProber, StaticProber
from ..registry.local_discovery import DEFAULT_MODEL_ROOTS, LocalDiscovery
from ..registry.ownership import EMPTY_POLICY, ProcNetOwnershipProver, load_operator_policy
from ..registry.registry import RegistryConfig, RegistryService
from ..registry.store import Store
from . import origin as bff_origin
from . import scopes as bff_scopes
from .account_directory import AccountAwareDirectory
from .model_service_keys import ModelServiceKeyStore
from .integrations import IntegrationStore
from . import stacks as bff_stacks
from .app import RESERVED_BIND_PORTS, OpsApplication
from .evaluation_adapter import ModuleEvaluationAdapter
from .observability_adapter import ModuleObservabilityAdapter, engine_registry_from_bff
from .gateway_usage import GatewayUsageFeed, load_key_owners
from .host_summary import HostSummaryStack, ModuleHostSummaryAdapter
from .ops import OpsConfig

ALLOWED_BIND_HOSTS = ("127.0.0.1", "::1")

#: 与 ``backend.registry.devserver`` 同一份合成 uid 表。``dana`` 不在这里 ——
#: 它**不是** ``bob`` 的别名，这一点由测试单独断言。
#: **测试夹具**：四个机器上并不存在的合成 Linux 主体。开发与用例靠它保持确定性，
#: 生产装配不该用它（见 ``real_host_users``）。
HOST_USERS = (("carol", 4000), ("alice", 4001), ("dave", 4002), ("bob", 4003))

#: 普通账户的 uid 起点（``/etc/login.defs`` 的 ``UID_MIN`` 默认值）。
UID_FIRST_HUMAN = 1000
#: ``nobody`` / ``nogroup``，以及内核保留区。
UID_NOBODY = 65534


def real_host_users() -> tuple[tuple[str, int], ...]:
    """本机**真实存在**的 Linux 用户：uid 落在 [1000, 65534)，按 uid 排序。

    服务账户（``llmlab`` 这类）也留下：它确实是这台机器上的一个用户，而且是网关
    进程的属主 —— 按 uid 归属调用量时靠的就是它。把它们过滤掉只会让归属变成空白，
    那不是更诚实，那是少了一半事实。
    """
    import pwd  # 只在真正要算名单时导入：本模块的其余部分不依赖 NSS。

    found = [
        (record.pw_name, record.pw_uid)
        for record in pwd.getpwall()
        if UID_FIRST_HUMAN <= record.pw_uid < UID_NOBODY
    ]
    return tuple(sorted(found, key=lambda item: item[1]))


def resolve_host_users(*, synthetic: bool) -> tuple[tuple[str, int], ...]:
    """``--synthetic-host-users`` 时用夹具，否则用本机真实用户。"""
    return HOST_USERS if synthetic else real_host_users()

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
    registry_mode: str = "synthetic",
    prober: object | None = None,
    accounts: object | None = None,
    allowed_logins: tuple[str, ...] | None = None,
    trusted_authorities: tuple[str, ...] = (),
    observe_local_engines: bool = False,
    live_host_summary: bool = False,
    #: None = 用自动默认位置；() = 不扫（缺省：库的默认值不该去读真实主机）。
    model_roots: tuple[str, ...] | None = (),
    engine_secret_refs: tuple[str, ...] = (),
    operator_policy: object | None = None,
    host_users: tuple[tuple[str, int], ...] = HOST_USERS,
    gateway_state_dir: str | None = None,
    gateway_key_owners: str | None = None,
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

    cfg = _local_config(state_dir)
    auth_service = AuthService(cfg)
    # CLI / Agent 令牌：存在状态目录里；「这个账户还有效吗」每次都现查用户表。
    token_service = build_token_service(
        state_dir, principal_state=AuthStorePrincipalState(auth_service.db)
    )
    auth_app = AuthApplication(
        cfg,
        service=auth_service,
        tokens=token_service,
        token_scope_policy=_token_scope_policy,
    )
    # 建表 / 迁移；否则第一个请求会以 500 收场。
    auth_app.initialize()
    token_service.initialize()

    # 静态 Linux 主体 + 管理员随时创建的平台账户（每次查询都读账户库）。
    # ``host_users`` 缺省是夹具；``main()`` 会传本机真实用户。
    principals = AccountAwareDirectory(
        PrincipalDirectory(
            [
                Principal(linux_user_id(host_id, uid), name, uid, display_name=name)
                for name, uid in host_users
            ]
        ),
        auth_app.service.accounts,
    )

    # ``operator_policy`` 是保留端口（8080 这类上游推理端口）的**唯一**放行口。
    # 没有它，登记上游服务会以 "port reserved" 收场；有了它，命中 entry 的服务
    # 还会拿到 operator_verified 的所有权证明。缺省空策略 = 一条都不放行。
    config = RegistryConfig(
        mode=registry_mode,
        operator_policy=operator_policy or EMPTY_POLICY,
    )
    local = {}
    if observe_local_engines:
        # 只读本机事实：回环 HTTP 探测、/v1/models、/proc/net/tcp 的监听 uid。
        local = {
            "prober": prober if prober is not None else LoopbackHttpProber(),
            "protocol_prober": LoopbackProtocolProber(),
            "ownership_prover": ProcNetOwnershipProver(),
            # 发现与登记共用同一份端口允许列表：发现不到登记不了的东西。
            "discovery": LocalDiscovery(
                allowed_ports=config.allowed_ports,
                # None = 没点名就按 DEFAULT_MODEL_ROOTS 自动找；() = 明确不找。
                model_roots=None if model_roots is None else tuple(model_roots),
            ),
        }
    service = RegistryService(
        store=Store(database),
        tokens=TrustedTokenIntrospector(token_service, principals=principals),
        principals=principals,
        config=config,
        prober=local.get("prober", prober),
        protocol_prober=local.get("protocol_prober"),
        ownership_prover=local.get("ownership_prover"),
        discovery=local.get("discovery"),
        gateway=MockGatewayAdapter() if mock_gateway else DetachedGatewayAdapter(),
    )

    # 集成网关：虚拟密钥、模型路由与调用元数据存在状态目录；上游引擎的 key 只点名位置。
    gateway_store = GatewayStore(Path(state_dir) / "gateway.sqlite3")
    gateway_store.initialize()
    gateway_core = GatewayCore(
        store=gateway_store, registry=service, secret_refs=parse_secret_refs(engine_secret_refs)
    )
    gateway_service = GatewayService(gateway_core)
    param_store = ParamStore(Path(state_dir) / "runtime.sqlite3")
    param_store.initialize()

    observability = build_observability_adapter(
        service=service,
        accounts=accounts,
        # live 的 Registry 不许配一条编出来的调用曲线。
        synthesize_gateway=(registry_mode != "live"),
        host_users=host_users,
        # 只有 live 的 Registry 才配读真实调用日志：合成的登记行配真实的调用量
        # 等于把两半不同的现实缝在一张页面上。
        gateway_state_dir=(gateway_state_dir if registry_mode == "live" else None),
        key_owners_path=gateway_key_owners,
    )
    evaluation = build_evaluation_adapter(service=service, host_users=host_users)
    host_summary = None
    system_resources = None
    model_service_keys = _model_service_keys_store(state_dir)
    if live_host_summary:
        from .live_sources import DellLiveSources

        live_sources = DellLiveSources()
        system_resources = live_sources.system_summary
        host_summary = ModuleHostSummaryAdapter(
            HostSummaryStack(
                read=live_sources.host_summary,
                runtime_read=live_sources.runtime_summary,
            )
        )

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
        host_summary=host_summary,
        system_resources=system_resources,
        model_service_keys=model_service_keys,
        ops_config=OpsConfig(
            registry_mode=registry_mode,
            gateway_adapter="mock" if mock_gateway else "detached",
            gateway_attached=mock_gateway,
        ),
        authority_policy=authority_policy,
        integrations=IntegrationStore(Path(state_dir) / "integrations.json"),
        registry_api=RegistryApplication(service),
        gateway=gateway_service,
        gateway_app=GatewayApplication(gateway_core),
        param_store=param_store,
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


def build_observability_adapter(
    *,
    service,
    accounts,
    now=None,
    synthesize_gateway=True,
    host_users=HOST_USERS,
    gateway_state_dir: str | None = None,
    key_owners_path: str | None = None,
):
    """装一个观测数据面。

    * ``synthesize_gateway=True``：夹具部署。目录与语料都**明确自称合成**，前端只把
      它当演示，带数字的读数一律降级成 UNKNOWN。
    * ``synthesize_gateway=False``：真部署，**不编任何调用行**。给了
      ``gateway_state_dir`` 就读那台 Gateway 真实的 ``usage.jsonl``（经
      :class:`~backend.bff.gateway_usage.GatewayUsageFeed` 翻译成观测契约）；
      没给就退回空文本 —— 观测层会如实报 ``no_sample``，而那与「零次调用」是两件事。
    """
    from datetime import datetime, timezone

    moment = now or datetime.now(timezone.utc)
    feed = None
    feed_extra = None

    if synthesize_gateway:
        key_directory = obs_identity.KeyDirectory(
            [
                obs_identity.GatewayKeyBinding(
                    f"key-{name}", linux_user_id(host_id_of(), uid), "proj-lab", f"{name}-workload"
                )
                for name, uid in host_users
            ]
        )
        gateway_text = lambda identity: bff_stacks.demo_gateway_corpus(
            key_directory,
            now=moment,
            endpoints=sorted(_visible_endpoints(service)(identity)),
        )
        provenance = bff_stacks.synthetic_provenance()
    else:
        key_directory, feed_extra = _gateway_key_directory(
            keys_path=(
                os.path.join(gateway_state_dir, "keys.json") if gateway_state_dir else None
            ),
            owners_path=key_owners_path,
        )
        if gateway_state_dir:
            feed = GatewayUsageFeed(
                os.path.join(gateway_state_dir, "usage.jsonl"),
                endpoint_for=lambda identity: model_endpoint_map(service, identity),
            )
            gateway_text = feed.text
            # 三个前端认可的来源之一。写它等于声明「这个数来自真实导出口」，
            # 所以只在真的接了那个文件时才写。
            provenance = bff_stacks.observation_provenance("gateway-usage-export")
        else:
            feed = None
            gateway_text = lambda identity: ""
            provenance = bff_stacks.synthetic_provenance()

    stack = bff_stacks.build_observability_stack(
        directory=key_directory,
        engine_registry=lambda identity: engine_registry_from_bff(
            service=service, identity=identity
        ),
        gateway_text=gateway_text,
        provenance=provenance,
        engine_probe=obs_engines.MockEngineProbe(default_up=True),
        host_report=bff_stacks.synthetic_host_report(moment),
    )
    return ModuleObservabilityAdapter(
        stack,
        endpoint_scope=_visible_endpoints(service),
        clock=lambda: moment,
        # 合成曲线只能挂在合成载荷上；真实来源才配得上真实曲线。
        trends_available=(feed is not None) or synthesize_gateway,
        feed=feed,
        feed_extra=feed_extra,
    )


def model_endpoint_map(service, identity) -> dict[str, str]:
    """``model 名 -> endpoint id``，只含该会话可见的端点。

    Gateway 的行里 ``model`` 是**模型名**，观测层判可见性用的是 Registry 的
    **endpoint id**。两者之间是登记事实（endpoint → deployment → model），所以这里是
    查表。同一个模型挂了多个 endpoint 时有歧义 —— 不映射，让那一行落进
    ``endpoint_missing``，而不是被塞进一个猜出来的端点。
    """
    from ..registry import errors as registry_errors

    try:
        policy = service.policy(identity.requester)
        models = {item.id: item.name for item in policy.visible_models()}
        deployments = {item.id: item for item in policy.visible_deployments()}
        endpoints = list(policy.visible_endpoints())
    except registry_errors.RegistryError:
        return {}

    by_name: dict[str, list[str]] = {}
    for endpoint in endpoints:
        deployment = deployments.get(endpoint.deployment_id)
        if deployment is None:
            continue
        name = models.get(deployment.model_id)
        if not name:
            continue
        by_name.setdefault(name, []).append(endpoint.id)
    return {name: ids[0] for name, ids in by_name.items() if len(ids) == 1}


def _gateway_key_directory(*, keys_path, owners_path):
    """用 Gateway 自己的 key 台账 + 一份**人工**属主声明建受信 key 目录。

    ``keys.json`` 只有 ``id/label/note/requests…``，**没有属主字段**。``label`` 是
    签发时写的名字，不是一条归属声明 —— 拿它去推断「这条调用是谁的」正是观测层
    明令禁止的做法。所以归属只能来自 ``gateway-key-owners.json`` 里人写下的那一行。

    没有声明的 key **不进目录**：它们的流量会落进 ``unknown_key``（管理员在诊断里
    看得见），不会被算到任何人头上。返回 ``(目录, 计数)``。
    """
    empty = obs_identity.KeyDirectory([])
    if not keys_path:
        return empty, None
    try:
        with open(keys_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return empty, None
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return empty, None
    # 声明文件缺席同样要报数：`keys_total=N, keys_bound=0` 是「有 N 把 key，
    # 一把都没有属主」——这条事实比一句 None 有用得多。
    owners = load_key_owners(owners_path)

    bindings = []
    seen: set[str] = set()
    total = 0
    for item in entries:
        if not isinstance(item, dict):
            continue
        total += 1
        key_id = item.get("id")
        if not isinstance(key_id, str) or not key_id.strip() or key_id in seen:
            continue
        owner = owners.get(key_id)
        if not owner:
            continue
        seen.add(key_id)
        bindings.append(
            obs_identity.GatewayKeyBinding(
                key_id=key_id,
                uid=owner,
                project="api-gateway",
                workload=item.get("label") if isinstance(item.get("label"), str) else None,
                revoked=bool(item.get("revoked")),
            )
        )
    return obs_identity.KeyDirectory(bindings), {"keys_total": total, "keys_bound": len(bindings)}


def host_id_of() -> str:
    return DEFAULT_HOST_ID


def build_evaluation_adapter(*, service, host_users=HOST_USERS):
    """装一个**派发关闭**的评测面。

    目标目录在启动时是空的（此时还没有任何 deployment）。因此在这个进程里提交评测
    会以「目标未授权」收场 —— 这与「真实派发关闭」是两件不同的事，但结论一致：
    不会有任何真实评测跑起来。用 CLI 登记了 deployment 之后，需要重启该进程才会
    重新同步目标目录。
    """
    principals, _tokens = bff_stacks.principal_directories(
        host_id=DEFAULT_HOST_ID, users=host_users
    )
    projects = bff_stacks.project_directory(
        projects=(
            (
                "proj-lab",
                "Lab",
                [linux_user_id(DEFAULT_HOST_ID, uid) for _name, uid in host_users],
            ),
        ),
        home=((linux_user_id(DEFAULT_HOST_ID, uid), "proj-lab") for _name, uid in host_users),
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


def _token_scope_policy(session) -> frozenset[str]:
    """一张 CLI 令牌最多能带哪些 Registry scope：与这条会话在网页上能做的一致。

    平台账户与本机 admin 的角色来自账户管理，所以管理员账户的令牌带管理员只读；
    Linux 会话一律按成员签发（它的提权是会话级的共享口令模式，不随令牌带走）。
    """
    user = session.user
    elevated = user.is_admin and user.auth_source != SOURCE_LINUX
    return bff_scopes.session_scopes(elevated=elevated).registry



def _model_service_keys_store(state_dir: str):
    """``<state>`` 里的模型推理密钥清单。

    每次调用都重新读盘：密钥轮换是运维要能在不重启 BFF 的前提下完成的事。
    没有清单文件时回空表（= 还没签发过），不编一把出来。
    """
    return ModelServiceKeyStore(state_dir)


def _local_config(state_dir: str):
    """开发进程跑在 ``http://127.0.0.1`` 上。

    ``Secure`` cookie 会让浏览器**根本不回传**它，于是出现「登录成功、下一个请求
    401」这种纯本地的假故障。所以这里显式关掉 ``cookie_secure``——这是本进程唯一
    偏离默认的一处，且只在环回地址上有意义。
    """
    # The devserver deliberately builds its config from an EMPTY environment so a
    # stray LLM_LAB_* variable cannot redirect the admin hash file, the state dir
    # or the provider list. ``TRUSTED_ORIGINS`` is the one exception, and it is
    # the documented way to deploy behind a TLS terminator: the public origin
    # (https://host:port) can never equal the ``http://<Host>`` this process
    # sees, so without it every same-origin POST — login included — is refused.
    # Only this one key is lifted out of os.environ; nothing else is read.
    cfg = auth_config.AuthConfig.from_env(
        {"LLM_LAB_TRUSTED_ORIGINS": os.environ.get("LLM_LAB_TRUSTED_ORIGINS", "")},
        state_dir=state_dir,
    )
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
        "--live-host-summary",
        action="store_true",
        help=(
            "serve fixed, read-only local NVML/Strata/Gateway observations from the "
            "session-protected /api/infra/summary route"
        ),
    )
    parser.add_argument(
        "--model-root",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "a directory the model discovery may look in (repeatable; only with "
            "--observe-local-engines). Only config.json and file names/sizes are read. "
            "Given at least once, these replace the automatic defaults ("
            + ", ".join(DEFAULT_MODEL_ROOTS)
            + ")"
        ),
    )
    parser.add_argument(
        "--no-model-scan",
        action="store_true",
        help="关闭模型目录的自动查找（推理服务的发现不受影响）",
    )
    parser.add_argument(
        "--engine-secret-ref",
        action="append",
        default=[],
        metavar="ENGINE=FILE:VAR",
        help=(
            "where the gateway reads an engine's own API key (engine name or id; the file holds "
            "VAR=value). Only the location is configured; the key never reaches a browser or a log"
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
    parser.add_argument(
        "--registry-mode",
        choices=("synthetic", "live"),
        default="synthetic",
        help=(
            "登记记录的诚实性声明。缺省 synthetic：这些行是夹具，控制台会把它们藏起来。"
            " 改成 live 等于声明「每一行描述的都是本机真实在跑、且属主真实的既有服务」"
        ),
    )
    parser.add_argument(
        "--gateway-state-dir",
        default="/srv/llm-lab/state/api-gateway",
        metavar="DIR",
        help=(
            "真实 Gateway 的状态目录（usage.jsonl / keys.json）。只有 --registry-mode live "
            "时才读；读不到就如实报「未接入」，绝不编调用行"
        ),
    )
    parser.add_argument(
        "--gateway-key-owners",
        default=None,
        metavar="FILE",
        help=(
            "key_id -> 属主主体名 的人工声明（schema gateway-key-owners/1）。"
            "keys.json 没有属主字段，没有这份声明就归不了人"
        ),
    )
    parser.add_argument(
        "--operator-policy",
        default=None,
        metavar="FILE",
        help=(
            "a root/service-owned JSON file listing existing services an operator "
            "admits for registration (schema registry-operator/1). This is the only "
            "path that admits a reserved upstream port such as 8080"
        ),
    )
    parser.add_argument(
        "--synthetic-host-users",
        action="store_true",
        help=(
            "受信主体目录用 carol/alice/dave/bob 这套测试夹具（uid 4000-4003）。"
            "缺省用本机真实的 Linux 用户：夹具用户在这台机器上并不存在，"
            "拿它当部署等于往受信目录里放四个虚构的人"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host = validate_bind_host(args.host)
    port = validate_bind_port(args.port)

    operator_policy = None
    if args.operator_policy:
        try:
            operator_policy = load_operator_policy(args.operator_policy)
        except Exception as exc:  # noqa: BLE001 - 策略文件不合格就不要启动
            raise SystemExit(f"refusing to start: {exc}") from exc

    # 受信主体目录：缺省是本机真实用户。夹具是测试用的，不是部署用的。
    host_users = resolve_host_users(synthetic=args.synthetic_host_users)

    app = build_application(
        state_dir=args.state_dir,
        database=args.database,
        host_id=args.host_id,
        admin_names=tuple(args.platform_admin),
        mock_gateway=True,
        registry_mode=args.registry_mode,
        prober=StaticProber() if args.with_static_prober else None,
        trusted_authorities=tuple(args.trusted_authority),
        observe_local_engines=args.observe_local_engines,
        live_host_summary=args.live_host_summary,
        model_roots=None if args.no_model_scan else (tuple(args.model_root) or None),
        engine_secret_refs=tuple(args.engine_secret_ref),
        operator_policy=operator_policy,
        host_users=host_users,
        gateway_state_dir=args.gateway_state_dir,
        gateway_key_owners=args.gateway_key_owners,
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
    "real_host_users",
    "resolve_host_users",
    "main",
    "validate_bind_host",
    "validate_bind_port",
]
