"""Offline harness for the ops BFF.

Everything here is synthetic: synthetic Linux accounts, a synthetic PAM backend
from ``tests.auth_linux.support``, an in-process broker transport (no socket),
a test-double prober and the Registry's ``MockGatewayAdapter``.

**What that is worth.** The PAM double proves the machinery *around* PAM —
that an allowlisted account reaches the backend, that a refused one does not,
that the verified identity becomes a session. It proves **nothing** about PAM.
No real Linux password is verified anywhere in this suite, because this build
ships no real PAM backend at all (``backend/auth_linux/pam_backend.py`` only
provides ``UnavailablePamBackend``, which refuses everything).

The two-user path is the point of the harness: ``alice`` (4001) and ``bob``
(4003) are two different synthetic Linux accounts, and ``dana`` — which is
*not* in the account table and *not* an alias for ``bob`` — is used to prove
there is no name-collapsing anywhere in the chain.
"""

from __future__ import annotations

import os
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from backend.atlas import identity as atlas_identity
from backend.auth import config as auth_config
from backend.auth import hashing as auth_hashing
from backend.auth.routes import Application as AuthApplication
from backend.bff import admin_mode as bff_admin_mode
from backend.bff import atlas_adapter as bff_atlas_adapter
from backend.bff import host_summary as bff_host_summary
from backend.auth_linux import throttle as linux_throttle
from backend.auth_linux.broker_client import BrokerClient
from backend.auth_linux.handoff import linux_user_id
from backend.auth_linux.policy import AccountPolicy, AdminGrant, Allowlist
from backend.auth_linux.provider import LinuxPamAuthProvider
from backend.auth_linux.service import LinuxAuthService
from backend.bff import observability_adapter as obs_adapter
from backend.bff import stacks as bff_stacks
from backend.bff.app import OpsApplication
from backend.bff.evaluation_adapter import ModuleEvaluationAdapter
from backend.bff.ops import OpsConfig
from backend.registry.gateway import MockGatewayAdapter
from backend.registry.health import ProbeResult, StaticProber
from backend.observability import engines as obs_engines
from backend.observability import identity as obs_identity
from backend.evaluation import identity as eval_identity
from backend.evaluation import targets as eval_targets
from backend.registry.identity import Principal, PrincipalDirectory, TokenDirectory
from backend.registry.registry import RegistryConfig, RegistryService
from backend.registry.store import Store
from tests.auth.support import WsgiClient
from tests.auth_linux.support import (  # noqa: F401 - re-exported for tests
    ADMIN_NAME,
    ADMIN_UID,
    GOOD_PASSWORD,
    OTHER_MEMBER_NAME,
    OTHER_MEMBER_UID,
    REAL_ACCOUNT_NAME,
    REAL_ACCOUNT_UID,
    SYNTHETIC_HOST_ID,
    UNBOUND_NAME,
    WRONG_PASSWORD,
    FakePamBackend,
    FakePwdSource,
    InProcessTransport,
    make_account,
)

MEMBER_NAME = "alice"
MEMBER_UID = 4001

#: Obviously synthetic, and never used against anything but the in-test double.
#: The shared admin password is a *deployment* secret written by an operator; no
#: real one exists in this repository and none is ever derived from a user's real
#: Linux password.
ADMIN_MODE_PASSWORD = "synthetic-shared-admin-mode-0003"
OTHER_ADMIN_MODE_PASSWORD = "synthetic-other-shared-admin-mode-0004"

_ADMIN_CREDENTIAL_CACHE: dict[str, str] = {}


def admin_credential_line(password: str = ADMIN_MODE_PASSWORD) -> str:
    """A real scrypt hash of a synthetic password, computed once per process.

    Deliberately the **real** ``backend.auth.hashing`` implementation rather than
    a stub: the point of these tests is that elevation goes through the same
    constant-time, salted verification the deployment will use. It is cached
    only because scrypt at the security floor costs ~30 ms, which would
    otherwise be paid by every one of the harness builds in this suite.
    """
    if password not in _ADMIN_CREDENTIAL_CACHE:
        _ADMIN_CREDENTIAL_CACHE[password] = auth_hashing.hash_password(password)
    return _ADMIN_CREDENTIAL_CACHE[password]


def write_admin_credential(state_dir: str, password: str = ADMIN_MODE_PASSWORD) -> Path:
    """Create the ``0600`` credential file an operator would have created."""
    path = Path(state_dir) / bff_admin_mode.DEFAULT_CREDENTIAL_FILE_NAME
    path.write_text(admin_credential_line(password) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path

#: 评测任务的归属项目。与 harness 里建的 ``ProjectDirectory`` 一致。
EVAL_PROJECT_ID = "proj-lab"

#: Atlas（Calls）侧的归属项目。与评测**同一个项目目录的另一面**：一台部署里的
#: 「项目」只有一个定义，两个模块各自持有自己那份只读视图，而不是各编一套。
ATLAS_PROJECT_ID = EVAL_PROJECT_ID

#: 主机投影 fixture 的目录。**沿用观测模块自己那一组**，不新造一份：那批文件已经
#: 被 ``tests/observability/test_host_projection.py`` 逐条钉住（live / 过期 / 未来 /
#: 缺 sample_at / synthetic=true / 离线），在这里重写一份只会多出一份会漂移的样本。
HOST_SUMMARY_FIXTURES = (
    Path(__file__).resolve().parents[1] / "observability" / "fixtures" / "host_summary"
)

#: 评测模块自带数据集目录里的一个**合成**数据集。
DEFAULT_EVAL_DATASET = "synthetic-qa-smoke"

#: The upstream the mock prober reports reachable. Port 8000 is in the
#: Registry's default allowlist and is *not* the reserved upstream port 8080.
ENGINE_TARGET_URL = "http://127.0.0.1:8000/v1"

_ACCOUNTS = (
    (ADMIN_NAME, ADMIN_UID),
    (MEMBER_NAME, MEMBER_UID),
    (OTHER_MEMBER_NAME, OTHER_MEMBER_UID),
    (REAL_ACCOUNT_NAME, REAL_ACCOUNT_UID),
)


class _FixedReadbackRouter:
    """一个只报告**固定代数**的 Router 替身（与 Registry #122 的夹具同形）。

    它不发布新快照，只回答 ``applied_revision()``。真实的 ``FileRouterAdapter`` 从
    Gateway 发布的 ``status.json`` 读同一个数字；这个替身把它钉住，让「调用门」可以
    被单独验证，而不必真的跑一次 repoint。
    """

    name = "fixed_readback_double"
    attached = True

    def __init__(self, revision: int) -> None:
        self._revision = int(revision)

    def publish(self, snapshot: dict):
        from backend.registry.router import ROUTER_SYNC_PENDING, RouterPublishResult

        return RouterPublishResult(
            published=False,
            applied_revision=None,
            snapshot_sha256=None,
            state=ROUTER_SYNC_PENDING,
            reason="test_fixture",
            detail="fixture: this double only reports an already-confirmed revision",
        )

    def applied_revision(self) -> int | None:
        return self._revision


@dataclass
class Harness:
    """A fully wired, offline stack. Nothing listens on a socket."""

    app: OpsApplication
    service: RegistryService
    auth_app: AuthApplication
    linux_service: LinuxAuthService
    principals: PrincipalDirectory
    accounts: FakePwdSource
    pam: FakePamBackend
    gateway: MockGatewayAdapter
    prober: StaticProber
    host_id: str = SYNTHETIC_HOST_ID
    state_dir: str = ""
    now: object = None
    key_directory: object = None
    observability: object = None
    evaluation: object = None
    atlas: object = None
    atlas_directory: object = None
    host_summary: object = None
    host_projection: object = None
    admin_mode: object = None
    admin_clock: object = None
    clients: dict = field(default_factory=dict)

    # -- identities --------------------------------------------------------

    def principal_of(self, name: str) -> str:
        uid = dict(_ACCOUNTS)[name]
        return linux_user_id(self.host_id, uid)

    def role_of(self, name: str) -> str:
        return "platform_admin" if name == ADMIN_NAME else "member"

    def requester_for(self, name: str):
        """The ``RequesterContext`` the BFF would build for ``name``.

        Registration has no browser route in this build — models are registered
        by the CLI / registry API, and the V6 drawer is explicitly read-only —
        so the tests register through the service with this context. Deriving it
        from the *same* ``bff.scopes`` table the identity path uses keeps the two
        sides from disagreeing about who can see what.
        """
        from backend.bff import scopes as bff_scopes

        return _requester(
            principal=self.principal_of(name),
            uid=dict(_ACCOUNTS)[name],
            linux_user=name,
            scopes=bff_scopes.registry_scopes_for(self.role_of(name)),
            role=self.role_of(name),
        )

    def session_id(self, name: str) -> str:
        """The signed-in client's session cookie value for ``name``.

        Signs the client in first if it has not been used yet, so a test can ask
        for "alice's session" without a ceremony it does not care about.
        """
        client = self.client(name)
        return client.cookies[self.app.cfg.cookie_name]

    def session_id_for(self, name: str, *, admin: bool = False) -> str:
        """The cookie value of a *particular* session: plain, or elevated."""
        client = self.admin_client(name) if admin else self.client(name)
        return client.cookies[self.app.cfg.cookie_name]

    def sync_eval_targets(self) -> None:
        """把 Registry 的 deployment 同步成评测侧的 ``TargetCatalog``。

        评测模块要一份自己的目标目录，而「登记了什么、是否就绪、共享给谁」的权威
        在 Registry。映射规则机械可查：``ready`` 取该 deployment 名下是否有已验证
        就绪的 Endpoint（注册 != 就绪），``visibility``/``grantees`` 直接来自那些
        Endpoint 的 ACL。
        """
        endpoints_by_deployment: dict[str, list] = {}
        for endpoint in self.service.store.list_endpoints():
            endpoints_by_deployment.setdefault(endpoint.deployment_id, []).append(endpoint)

        records = []
        for deployment in self.service.store.list_deployments():
            endpoints = endpoints_by_deployment.get(deployment.id, [])
            if any(item.visibility == eval_identity.VISIBILITY_ALL_APPROVED for item in endpoints):
                visibility = eval_identity.VISIBILITY_ALL_APPROVED
            elif any(item.visibility == eval_identity.VISIBILITY_USERS for item in endpoints):
                visibility = eval_identity.VISIBILITY_USERS
            else:
                visibility = eval_identity.VISIBILITY_PRIVATE
            records.append(
                eval_targets.TargetRecord(
                    deployment_id=deployment.id,
                    project_id=EVAL_PROJECT_ID,
                    owner_principal=deployment.owner_principal,
                    owner_uid=deployment.owner_uid,
                    ready=any(item.status == "ready" for item in endpoints),
                    visibility=visibility,
                    grantees=frozenset(
                        grantee
                        for item in endpoints
                        for grantee in self.service.store.grantees(item.id)
                    ),
                    model_ref=deployment.model_id,
                    display_name=deployment.name,
                )
            )
        self.evaluation.stack.service.replace_targets(eval_targets.TargetCatalog(records))

    def submit_eval(self, owner: str, deployment_id: str, **fields):
        """以 ``owner`` 的受信主体提交一次评测。走的是模块自己的服务方法。"""
        requester = self.evaluation.requester_for(self.trusted_principal(owner))
        request = {
            "deployment_id": deployment_id,
            "eval_type": "correctness",
            "dataset_slug": DEFAULT_EVAL_DATASET,
        }
        request.update(fields)
        return self.evaluation.stack.service.submit(requester=requester, request=request)

    def acknowledge_route(self, endpoint_id: str) -> int:
        """让一条 Endpoint 的**当前** revision 拥有一次 Gateway 回读确认。

        为什么现在需要它：Registry 的 P0 调用门（PR #122）要求
        ``router_sync_applied`` —— 也就是 ``router_generation == revision``、证据是
        ``gateway_readback``、**并且** Router 此刻回读的代数与这条 Endpoint 的
        revision 相等，且这条 Endpoint 在可导出的快照里。新建的 Endpoint 一律停在
        ``pending_router_sync``（``router_generation=0``），所以在它上面调用会先被
        409 拒绝，而不是被转发。

        这里做的是**那次回执成功时本来就会写下的同一行**，加一个只报同一代数的
        Router 替身；它模拟「本部署已经有过一次成功回读」，没有绕过任何回读逻辑，
        也**不**修改 deployment 目标（那会把测试变成在证明另一件事）。

        在没有 #122 的树上，这段额外状态不会被读取 —— 夹具因此在**两边**都成立。
        """
        import dataclasses

        from backend.registry import models as registry_models

        endpoint = self.service.store.endpoint_by_id(endpoint_id)
        assert endpoint is not None, f"unknown endpoint {endpoint_id}"
        deployment = self.service.store.deployment_by_id(endpoint.deployment_id)
        assert deployment is not None, f"endpoint {endpoint_id} has no deployment"

        # 1) 没有 secret_ref 的 engine 会被 ``build_routing_snapshot`` 整条排除，
        #    于是「在快照里」这一条永远不成立。
        refs = dict(self.service.config.engine_secret_refs)
        refs.setdefault(
            deployment.engine_id, {"file": "/etc/llmlab/engines.env", "var": "ENGINE_KEY"}
        )
        self.service.config = dataclasses.replace(
            self.service.config, engine_secret_refs=refs
        )

        # 2) 一个只报「已确认代数」的 Router 替身。
        self.service.router = _FixedReadbackRouter(endpoint.revision)

        # 3) 写下回读成功时的那一行。
        outcome = self.service.store.ack_router_generation(
            endpoint_id,
            generation=endpoint.revision,
            actor_principal="system:router-sync",
            actor_uid=0,
            at=self.service._now(),
            evidence=registry_models.ROUTER_EVIDENCE_GATEWAY_READBACK,
        )
        assert outcome.get("outcome") == "ok", outcome
        return endpoint.revision

    def close(self) -> None:
        """Release the SQLite handles a harness owns. Tests call this."""
        try:
            self.service.store.close()
        except Exception:  # pragma: no cover - best effort in teardown
            pass

    def trusted_principal(self, name: str, *, admin: bool = False):
        """What the BFF itself resolves for ``name``'s live session.

        Goes through ``OpsApplication.resolve_session`` — the production path —
        rather than rebuilding the context here, so a test that registers as a
        principal and reads as a session is exercising one identity, not two
        lookalikes. ``admin=True`` resolves the *elevated* session, which is the
        only way to get the global view under the V6 model.
        """
        return self.app.resolve_session(self.session_id_for(name, admin=admin))

    # -- HTTP --------------------------------------------------------------

    def client(self, name: str, *, host: str = "127.0.0.1:8790") -> WsgiClient:
        """A browser-like client for ``name``, signed in over HTTP.

        A **plain** session: under the V6 model this is a ``member`` no matter who
        ``name`` is, including ``carol``. Use :meth:`admin_client` for a session that
        has entered admin mode.
        """
        return self._signed_in(name, admin=False, host=host)

    def admin_client(self, name: str, *, host: str = "127.0.0.1:8790") -> WsgiClient:
        """A *separate* signed-in session for ``name`` that has entered admin mode.

        Separate on purpose. Elevation is a property of one session, so a test that
        wants to compare "the same person, elevated and not" needs two sessions —
        and getting a second one here means the isolation is exercised by the same
        path production uses, rather than asserted from a shortcut.
        """
        return self._signed_in(name, admin=True, host=host)

    def _signed_in(self, name: str, *, admin: bool, host: str) -> WsgiClient:
        key = (name, admin)
        if key in self.clients:
            return self.clients[key]
        client = WsgiClient(app=self.app, host=host, scheme="http")
        signed_in = login(client, name, GOOD_PASSWORD)
        assert signed_in.status == 200, f"login for {name} failed: {signed_in.status}"
        if admin:
            entered = enter_admin_mode(client)
            assert entered.status == 200, f"admin mode for {name} failed: {entered.body!r}"
        self.clients[key] = client
        return client


def _requester(*, principal: str, uid: int, linux_user: str, scopes, role: str):
    from backend.registry.identity import RequesterContext

    return RequesterContext(
        principal=principal,
        uid=uid,
        linux_user=linux_user,
        scopes=frozenset(scopes),
        roles=frozenset({role}),
    )


def _deployment_model_lookup(service):
    """``deployment_id -> model_id``，走 Registry 自己的存储。"""

    def lookup(deployment_id):
        if not isinstance(deployment_id, str) or not deployment_id:
            return None
        deployment = service.store.deployment_by_id(deployment_id)
        return deployment.model_id if deployment is not None else None

    return lookup


def build_harness(
    *,
    state_dir: str,
    host_id: str = SYNTHETIC_HOST_ID,
    admin_names: tuple[str, ...] = (ADMIN_NAME,),
    mock_gateway: bool = True,
    prober_healthy: bool = True,
    clock=None,
    now=None,
    with_observability: bool = True,
    with_evaluation: bool = True,
    with_atlas: bool = True,
    with_host_summary: bool = True,
    host_projection: object = None,
    #: 引擎实时读数的取数器。默认 ``None`` = 应用用模块默认实现（真去读环回的
    #: ``/metrics``）。测试必须显式塞一个假的进来：一次单测不该依赖本机有没有
    #: 引擎、有没有那份 key 文件。
    engine_live: object = None,
    #: 模型服务密钥仓库。默认 ``None`` = 本部署**没有装配**这一支（两条路由回 503）。
    #: 路由测试必须塞一个指向临时目录、且钉住引擎环境文件路径的仓库：默认值会去碰
    #: 真实用户的 ``~/.config/strata/api-key.env``，那是这台机器上的运行事实，
    #: 不是一次单测可以改的东西。
    model_service_keys: object = None,
    with_admin_mode: bool = True,
    admin_mode_configured: bool = True,
    admin_mode_ttl_seconds: int = bff_admin_mode.DEFAULT_TTL_SECONDS,
    admin_mode_password: str = ADMIN_MODE_PASSWORD,
    admin_clock=None,
) -> Harness:
    # --- Linux side ------------------------------------------------------
    accounts = FakePwdSource()
    for name, uid in _ACCOUNTS:
        accounts.add(make_account(name, uid))

    allowlist = Allowlist(frozenset(name for name, _ in _ACCOUNTS))
    admin_grant = AdminGrant(
        host_id=host_id,
        entries=frozenset(
            (uid, name) for name, uid in _ACCOUNTS if name in set(admin_names)
        ),
    )
    policy = AccountPolicy(allowlist=allowlist, host_id=host_id, admin_grant=admin_grant)

    pam = FakePamBackend({name: GOOD_PASSWORD for name, _ in _ACCOUNTS})
    from tests.auth_linux.support import build_broker

    broker, _config, _accounts, _audit, _throttle, _clock = build_broker(
        accounts=accounts,
        pam=pam,
        policy=policy,
        host_id=host_id,
    )
    # A peer that the broker accepts, so the in-process frame is a real one.
    from tests.auth_linux.support import peer as make_peer

    peer = make_peer(uid=MEMBER_UID, gid=MEMBER_UID)
    transport = InProcessTransport(
        lambda frame, timeout: broker.process_frame(frame, peer)
    )
    provider = LinuxPamAuthProvider(
        accounts=accounts,
        policy=policy,
        broker=BrokerClient(transport),
        throttle=linux_throttle.InMemoryThrottle(),
        host_id=host_id,
        audit=None,
        available=True,
    )
    linux_service = LinuxAuthService(
        provider, host_id=host_id, allowlist=allowlist, admin_grant=admin_grant
    )

    # --- Registry side ---------------------------------------------------
    principals = PrincipalDirectory(
        [
            Principal(linux_user_id(host_id, uid), name, uid, display_name=name)
            for name, uid in _ACCOUNTS
        ]
    )
    prober = StaticProber()
    if prober_healthy:
        prober.set(8000, ProbeResult(reachable=True, latency_ms=0.5))
    gateway = MockGatewayAdapter()
    service_kwargs = {}
    if clock is not None:
        service_kwargs["clock"] = clock
    service = RegistryService(
        store=Store(":memory:"),
        tokens=TokenDirectory(()),
        principals=principals,
        config=RegistryConfig(mode="synthetic"),
        prober=prober,
        gateway=gateway if mock_gateway else None,
        **service_kwargs,
    )

    # --- Observability side ----------------------------------------------
    # 一个 key 一个主体：归属只来自这份受信目录（行里不带 uid）。
    key_directory = obs_identity.KeyDirectory(
        [
            obs_identity.GatewayKeyBinding(
                f"key-{name}", linux_user_id(host_id, uid), "proj-lab", f"{name}-workload"
            )
            for name, uid in _ACCOUNTS
        ]
    )
    resolved_now = now or (clock() if clock is not None else datetime.now(timezone.utc))

    def _visible_endpoints(identity) -> frozenset[str]:
        try:
            return frozenset(
                item.id for item in service.policy(identity.requester).visible_endpoints()
            )
        except Exception:
            # 会话没有 registry:read 时这里会拒；观测段本来就该是空的。
            return frozenset()

    observability = None
    if with_observability:
        stack = bff_stacks.build_observability_stack(
            directory=key_directory,
            engine_registry=lambda identity: obs_adapter.engine_registry_from_bff(
                service=service, identity=identity
            ),
            # 语料按会话生成：只引用该会话有权使用的 endpoint，否则样本会被
            # 观测模块如实丢掉（out_of_scope_endpoint），页面上就是「没有数据」。
            gateway_text=lambda identity: bff_stacks.demo_gateway_corpus(
                key_directory, now=resolved_now, endpoints=sorted(_visible_endpoints(identity))
            ),
            provenance=bff_stacks.synthetic_provenance(),
            engine_probe=obs_engines.MockEngineProbe(default_up=True),
            host_report=bff_stacks.synthetic_host_report(resolved_now),
        )
        observability = obs_adapter.ModuleObservabilityAdapter(
            stack,
            endpoint_scope=_visible_endpoints,
            clock=lambda: resolved_now,
        )

    # --- Evaluation side --------------------------------------------------
    # 项目目录建在评测分支**之外**：Atlas 的 ``authorized_projects`` 用的是**同一份**
    # 目录。一台部署里「谁属于哪个项目」只有一个答案，两个模块各持一份只读视图，
    # 而不是各编一套成员名单。
    eval_principals, _tokens = bff_stacks.principal_directories(
        host_id=host_id, users=_ACCOUNTS
    )
    projects = bff_stacks.project_directory(
        projects=((EVAL_PROJECT_ID, "Lab", [linux_user_id(host_id, uid) for _n, uid in _ACCOUNTS]),),
        home=((linux_user_id(host_id, uid), EVAL_PROJECT_ID) for _n, uid in _ACCOUNTS),
    )

    evaluation = None
    if with_evaluation:
        eval_stack = bff_stacks.build_evaluation_stack(
            principals=eval_principals,
            projects=projects,
            targets=eval_targets.empty_catalog(),
            clock=(lambda: resolved_now),
        )
        evaluation = ModuleEvaluationAdapter(
            eval_stack,
            # 任务的目标是 deployment，前端那一列是「模型」：用 Registry 的连接补上。
            model_lookup=_deployment_model_lookup(service),
        )

    # --- Calls (Atlas) side ------------------------------------------------
    # key → 主体/项目的绑定只来自这份**受信目录**：行里不带 uid，归属规则只有这一条。
    # 与观测侧那份目录刻意分开，因为两个模块各自校验自己那份类型 —— 它们互相不认识。
    atlas_directory = atlas_identity.KeyDirectory(
        [
            atlas_identity.GatewayKeyBinding(
                f"key-{name}", linux_user_id(host_id, uid), ATLAS_PROJECT_ID, f"{name}-workload"
            )
            for name, uid in _ACCOUNTS
        ]
    )

    def _atlas_projects(identity) -> frozenset[str]:
        """这个主体被授权看哪些项目。权威是上面那份项目目录，不是请求。"""
        home = projects.home_project(identity.principal)
        return frozenset({home}) if home else frozenset()

    atlas = None
    if with_atlas:
        atlas = bff_atlas_adapter.ModuleAtlasAdapter(
            bff_stacks.build_atlas_stack(
                directory=atlas_directory,
                gateway_text=lambda _identity: demo_atlas_corpus(
                    atlas_directory,
                    (linux_user_id(host_id, uid) for _n, uid in _ACCOUNTS),
                    now=resolved_now,
                ),
                project_scope=_atlas_projects,
            ),
            clock=(lambda: resolved_now),
        )

    # --- Host summary side -------------------------------------------------
    # ``host_projection`` 缺省是 ``None``：**没有投影**。附件默认值是「本部署没有
    # 这台主机的事实」，而不是一份看起来正常的主机读数 —— 后者正是
    # ``validate_host_summary`` 会拒绝、而一个粗心的默认值会漏过去的东西。
    host_summary = None
    if with_host_summary:
        host_summary = bff_host_summary.ModuleHostSummaryAdapter(
            bff_stacks.build_host_summary_stack(
                read=_projection_reader(host_projection),
            ),
            clock=(lambda: resolved_now),
        )

    # --- Portal auth -----------------------------------------------------
    cfg = auth_config.AuthConfig.from_env({}, state_dir=state_dir)
    from dataclasses import replace

    cfg = replace(cfg, cookie_secure=False)
    auth_app = AuthApplication(cfg)
    # 建表 / 迁移。没有这一步，第一个请求就会以 500 收场。
    auth_app.initialize()

    # --- Shared admin mode ------------------------------------------------
    # Wired exactly as a deployment would: a real scrypt credential file at 0600,
    # a real elevation store, and the route the UI calls. A harness built with
    # ``admin_mode_configured=False`` has *no* credential file, which is the
    # production default and means nobody can elevate.
    resolved_admin_clock = admin_clock if admin_clock is not None else _time.time
    admin_service = None
    if with_admin_mode:
        if admin_mode_configured:
            write_admin_credential(state_dir, admin_mode_password)
        admin_service = bff_admin_mode.build_service(
            bff_admin_mode.AdminModeConfig(
                enabled=True,
                credential_path=Path(state_dir) / bff_admin_mode.DEFAULT_CREDENTIAL_FILE_NAME,
                store_path=Path(state_dir) / bff_admin_mode.DEFAULT_STORE_FILE_NAME,
                ttl_seconds=admin_mode_ttl_seconds,
            ),
            clock=resolved_admin_clock,
        )

    app = OpsApplication(
        auth_app=auth_app,
        service=service,
        accounts=accounts,
        linux_service=linux_service,
        observability=observability,
        evaluation=evaluation,
        atlas=atlas,
        host_summary=host_summary,
        engine_live=engine_live,
        model_service_keys=model_service_keys,
        # 应用自己的时钟也钉在 harness 的 ``resolved_now`` 上。
        #
        # 不传它的时候 ``OpsApplication._now()`` 回的是**真实墙钟**，而同一份 harness
        # 里的观测/评测/Atlas 适配器都用 ``resolved_now``。于是 ``build_harness(now=X)``
        # 造出的数据会在「应用认为的当下」与「适配器认为的当下」之间差出一段 ——
        # 能力清单会把它如实报成 ``sample_in_future``，而那不是被测代码的问题，
        # 是脚手架给了两个时间源。同一次判定有两个当下，是这里最不该有的东西。
        clock=(lambda: resolved_now),
        ops_config=OpsConfig(
            registry_mode="synthetic",
            gateway_adapter="mock" if mock_gateway else "detached",
            gateway_attached=mock_gateway,
        ),
        admin_mode=admin_service,
    )

    return Harness(
        app=app,
        service=service,
        auth_app=auth_app,
        linux_service=linux_service,
        principals=principals,
        accounts=accounts,
        pam=pam,
        gateway=gateway,
        prober=prober,
        host_id=host_id,
        state_dir=state_dir,
        now=resolved_now,
        key_directory=key_directory,
        observability=observability,
        evaluation=evaluation,
        atlas=atlas,
        atlas_directory=atlas_directory,
        host_summary=host_summary,
        host_projection=host_projection,
        admin_mode=admin_service,
        admin_clock=resolved_admin_clock,
    )


# --- request helpers ----------------------------------------------------------


def csrf(client: WsgiClient) -> str:
    """Fetch a CSRF token the way the browser does.

    Writes are refused without one — that refusal is the existing auth
    application's rule, reused rather than reimplemented, and it is asserted in
    ``test_share_requires_csrf_and_same_origin``.
    """
    return client.run("GET", "/auth/csrf").json()["data"]["csrf_token"]


def login(client: WsgiClient, name: str, password: str):
    return client.run(
        "POST",
        "/auth/linux/login",
        payload={"login_name": name, "password": password},
        headers={"X-CSRF-Token": csrf(client)},
    )


def enter_admin_mode(client: WsgiClient, password: str = ADMIN_MODE_PASSWORD):
    """Enter admin mode the way the UI does: a real POST with a real CSRF token."""
    return client.run(
        "POST",
        "/auth/admin/enter",
        payload={"password": password},
        headers={"X-CSRF-Token": csrf(client)},
    )


def exit_admin_mode(client: WsgiClient):
    return client.run(
        "POST",
        "/auth/admin/exit",
        payload={},
        headers={"X-CSRF-Token": csrf(client)},
    )


def admin_status(client: WsgiClient):
    return client.run("GET", "/auth/admin/status")


def admin_mode_of(response) -> dict:
    return response.json()["data"]["admin_mode"]


def fetch_ops(client: WsgiClient, visibility: str | None = None):
    path = "/api/v2/ops"
    if visibility:
        path = f"{path}?visibility={visibility}"
    return client.run("GET", path)


def share(client: WsgiClient, deployment_id: str, *, mode: str, principals=None):
    return client.run(
        "PUT",
        f"/api/v2/ops/deployments/{deployment_id}/share",
        payload={"mode": mode, "principals": list(principals or [])},
        headers={"X-CSRF-Token": csrf(client)},
    )


def ops_of(response) -> dict:
    return response.json()["data"]["ops"]


def register_baseline(harness: Harness, owner: str = MEMBER_NAME) -> dict:
    """Register one engine → model → deployment → endpoint as ``owner``.

    Registration is metadata-only and has no browser route in this build (the V6
    drawer is read-only by design), so it goes through the Registry service with
    the same trusted context the BFF would build. The endpoint starts **private**
    and, with the test-double prober attached, is verified ``ready`` immediately
    — ``ready`` is what sharing and invoking both require.
    """
    requester = harness.requester_for(owner)
    service = harness.service
    engine, _ = service.register_engine(
        requester,
        {
            "name": f"engine-{owner}",
            "target_url": ENGINE_TARGET_URL,
            "protocol": "openai_compatible",
        },
    )
    model, _ = service.register_model(requester, {"name": f"model-{owner}"})
    deployment, _ = service.register_deployment(
        requester,
        {
            "name": f"deployment-{owner}",
            "engine_id": engine["data"]["id"],
            "model_id": model["data"]["id"],
        },
    )
    endpoint, _ = service.register_endpoint(
        requester,
        {"name": f"endpoint-{owner}", "deployment_id": deployment["data"]["id"]},
    )
    harness.sync_eval_targets()
    return {
        "engine_id": engine["data"]["id"],
        "model_id": model["data"]["id"],
        "deployment_id": deployment["data"]["id"],
        "endpoint_id": endpoint["data"]["id"],
    }


# --- Calls（Atlas）与主机投影 ------------------------------------------------


def demo_atlas_corpus(
    directory,
    principals,
    *,
    now,
    hours: int = 24,
    per_key: int = 3,
    marker: str = "SYNTHETIC-BFF-CALLS",
) -> str:
    """按受信 key 目录生成一批**合成** Gateway 行，供 Calls 面使用。

    与 ``backend.bff.stacks.demo_gateway_corpus`` 同样的做法与同样的理由，但**写在
    测试里**：那是一份给开发与测试用的语料，不该进生产装配模块。观测侧那份之所以
    在 ``stacks.py``，是因为 devserver 也要用它；Calls 这一侧目前只有测试要。

    归属**只**来自传入的目录：行里不带 uid、不带任何自报主体。每个 key 给不同行数，
    这样「B 只看到自己的行」可以被观察出来，而不是因为人人相等而看不出来。
    确定性来自主体名在入参里的位置，不是随机数。
    """
    import json
    from datetime import timedelta, timezone as _tz

    rows: list[str] = []
    span = max(1, int(hours * 60))
    for position, principal in enumerate(sorted(principals)):
        for index, key_id in enumerate(directory.owned_by(principal)):
            binding = directory.lookup(key_id)
            if binding is None or binding.revoked:
                continue
            rows_for_key = per_key + (position % 2)
            for step in range(rows_for_key):
                minutes_ago = int(span * (step + 1) / (rows_for_key + 1))
                moment = (now - timedelta(minutes=minutes_ago)).astimezone(_tz.utc).replace(
                    microsecond=0
                )
                rows.append(
                    json.dumps(
                        {
                            "ts": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "key_id": key_id,
                            "fixture": marker,
                            "label": binding.workload or "synthetic-workload",
                            "method": "POST",
                            "path": "/v1/chat/completions",
                            "model": f"model-{index}",
                            "status": 200 if step != rows_for_key - 1 else 429,
                            "outcome": "success" if step != rows_for_key - 1 else "failed",
                            "stream": True,
                            "queue_ms": 5 + step,
                            "ttft_ms": 120 + step * 10,
                            "upstream_ms": 800 + step * 50,
                            "total_ms": 800 + step * 50,
                            "bytes_out": 4096,
                            "usage": {"input_tokens": 100 + step, "output_tokens": 200 + step},
                        },
                        sort_keys=True,
                    )
                )
    return "\n".join(rows) + ("\n" if rows else "")


def load_host_summary_fixture(name: str) -> dict:
    """读一份观测模块自己的主机投影 fixture（见 :data:`HOST_SUMMARY_FIXTURES`）。

    返回**整个 fixture**（``note`` / ``now`` / ``expect`` / ``payload``）。测试里
    要喂给路由的是 ``fixture["payload"]``，要用的是 ``fixture_now(fixture)`` ——
    把「哪一部分是机体、哪一部分是期望」分开，免得某天有人把 ``expect`` 也发出去。
    """
    import json

    return json.loads((HOST_SUMMARY_FIXTURES / name).read_text(encoding="utf-8"))


def fixture_now(fixture: dict):
    """fixture 自己的参考时钟。**带时区**是它自己的约定，缺了就直接失败。"""
    from backend.observability.host_projection import parse_instant

    moment = parse_instant(fixture.get("now"))
    if moment is None:
        raise AssertionError("a host summary fixture must carry a timezone-aware 'now'")
    return moment


def _projection_reader(payload):
    """把一份固定的投影（或 ``None``）包成 ``read()``。

    ``None`` 表示**这次没读到**：它不是「一份空的投影」。两条路的 HTTP 结局不同
    （503 vs 200 + 逐条 UNKNOWN），测试要能分别走到。
    """
    return lambda: payload


# --- deterministic generation -------------------------------------------------


class FixedClock:
    """A clock frozen at one instant, so a generated payload is reproducible."""

    def __init__(self, moment=None) -> None:
        from datetime import datetime, timezone

        self._moment = moment or datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self._moment


@contextmanager
def deterministic_ids(prefix: str = "id"):
    """Replace the Registry's id factory with a counter.

    Ids come from ``secrets.token_hex``, which is exactly what you want in
    production and exactly what stops a payload from being reproducible in a
    test. Patching the module-level factory — rather than post-processing the
    output — keeps the generated fixture a genuine product of the real code
    path, so the byte-for-byte comparison actually means something.
    """
    from unittest import mock

    counter = {"n": 0}

    def factory(kind: str) -> str:
        counter["n"] += 1
        return f"{kind}_{prefix}{counter['n']:03d}"

    job_counter = {"n": 0}

    def job_factory() -> str:
        job_counter["n"] += 1
        return f"evj-{prefix}{job_counter['n']:021d}"

    def result_factory() -> str:
        job_counter["n"] += 1
        return f"evr-{prefix}{job_counter['n']:021d}"

    # 两个模块的 id 来源都要钉住：Registry 用 ``new_id``，评测用
    # ``new_job_id``/``new_result_id``。少钉一个，整个 fixture 就不可复现。
    # ``service`` does ``from .models import new_job_id``, so patching the
    # defining module alone would leave the service calling the original.
    with mock.patch("backend.registry.registry.new_id", side_effect=factory), mock.patch(
        "backend.evaluation.models.new_job_id", side_effect=job_factory
    ), mock.patch(
        "backend.evaluation.models.new_result_id", side_effect=result_factory
    ), mock.patch(
        "backend.evaluation.service.new_job_id", side_effect=job_factory
    ), mock.patch(
        "backend.evaluation.service.new_result_id", side_effect=result_factory
    ):
        yield


__all__ = [
    "ADMIN_MODE_PASSWORD",
    "ADMIN_NAME",
    "ATLAS_PROJECT_ID",
    "HOST_SUMMARY_FIXTURES",
    "OTHER_ADMIN_MODE_PASSWORD",
    "admin_credential_line",
    "admin_mode_of",
    "admin_status",
    "bff_stacks",
    "demo_atlas_corpus",
    "enter_admin_mode",
    "exit_admin_mode",
    "fixture_now",
    "load_host_summary_fixture",
    "write_admin_credential",
    "ADMIN_UID",
    "ENGINE_TARGET_URL",
    "GOOD_PASSWORD",
    "Harness",
    "MEMBER_NAME",
    "MEMBER_UID",
    "REAL_ACCOUNT_NAME",
    "REAL_ACCOUNT_UID",
    "SYNTHETIC_HOST_ID",
    "UNBOUND_NAME",
    "WRONG_PASSWORD",
    "build_harness",
    "fetch_ops",
    "login",
    "ops_of",
    "share",
]
