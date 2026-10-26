"""行级 ACL：可见性**只有这一处**判定。

三条不变量的实现都在这个文件里，别处不允许再写一遍：

1. **默认私有。** 一个 Endpoint 建出来就是 ``private``。要看别人的东西，
   必须有一条**显式**的授权行（或它声明了对全部已审批用户开放）。
2. **不可见 = 404，不是 403。** 无权知道其存在时，响应与「根本不存在」逐字节相同；
   ``NotFound`` 与 ``AccessDenied`` 的分界是「调用者是否已经知道这个对象存在」。
   一旦把「无权看」答成 403，端点就成了一个可以逐 id 枚举他人资源的预言机。
3. **管理员能力来自 scope，不来自角色。** ``role == admin`` 不产生任何权限；
   ``registry:admin:read`` / ``registry:admin:manage`` 才产生。

「共享一个 Endpoint」是**唯一**的跨主体可见性来源，且它只传递必要的那么一点：
被共享的方法能被调用，背后 deployment/model 的元数据可见（权重路径对非 owner 打码），
而 engine 的 loopback 目标地址**对非 owner 打码** —— 否则共享一次服务就等于
把 owner 本机在监听哪些端口列了出来。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import AccessDenied, InvalidRequest, NotFound
from .identity import (
    SCOPE_ADMIN_MANAGE,
    SCOPE_ADMIN_READ,
    SCOPE_ENDPOINT_INVOKE,
    SCOPE_ENDPOINT_SHARE,
    SCOPE_REGISTRY_READ,
    SCOPE_REGISTRY_WRITE,
    PrincipalDirectory,
    RequesterContext,
)
from .models import (
    ENDPOINT_READY,
    VISIBILITY_ALL_APPROVED,
    VISIBILITY_PRIVATE,
    VISIBILITY_USERS,
    Deployment,
    Endpoint,
    Engine,
    Model,
    RuntimeBuild,
)
from .store import Store


@dataclass(frozen=True)
class VisibilitySets:
    """一次请求算出来的可见 id 集合。同一请求内复用，不重复查库。"""

    endpoints: frozenset[str]
    deployments: frozenset[str]
    engines: frozenset[str]
    models: frozenset[str]
    builds: frozenset[str]
    global_read: bool


class AccessPolicy:
    """**每次请求**建一个：它缓存本次的可见性计算。"""

    def __init__(
        self,
        requester: RequesterContext,
        store: Store,
        principals: PrincipalDirectory,
    ) -> None:
        if not isinstance(requester, RequesterContext):
            raise InvalidRequest("requester must be a RequesterContext")
        self.requester = requester
        self.store = store
        self.principals = principals
        self._sets: VisibilitySets | None = None

    # -- 可见性计算 -------------------------------------------------------

    @property
    def is_owner_of_everything(self) -> bool:
        return SCOPE_ADMIN_READ in self.requester.scopes

    def visibility(self) -> VisibilitySets:
        """算出本次请求可见的五类 id。

        规则是对每一类都**同样**的一条：

            「自己名下的」 ∪ 「由某个可见的上层对象引用到的」

        再加管理员的全集。第 4 步的 ``own`` 这一半不能少：一个刚建出来、还没被任何
        Endpoint 引用的 deployment，如果只靠「被可见 Endpoint 引用」来判定，
        它的 owner 反而看不见它 —— 那正是本次最初写错的地方。
        """
        if self._sets is not None:
            return self._sets

        me = self.requester.principal
        everything = self.is_owner_of_everything

        # 1. Endpoint：自己名下的、显式共享给自己的、对全部已审批用户开放的。
        all_endpoints = self.store.list_endpoints()
        if everything:
            endpoint_ids = frozenset(item.id for item in all_endpoints)
        else:
            granted = self.store.endpoint_ids_granted_to(me)
            approved = self.principals.approved_principals()
            collected = set()
            for endpoint in all_endpoints:
                if endpoint.owner_principal == me:
                    collected.add(endpoint.id)
                elif endpoint.visibility == VISIBILITY_USERS and endpoint.id in granted:
                    collected.add(endpoint.id)
                elif endpoint.visibility == VISIBILITY_ALL_APPROVED and me in approved:
                    # 成员来源是**受信目录**的审批状态，不是请求里的任何字段。
                    collected.add(endpoint.id)
            endpoint_ids = frozenset(collected)

        visible_endpoints = [item for item in all_endpoints if item.id in endpoint_ids]
        referenced_deployments = {item.deployment_id for item in visible_endpoints}

        # 2. Deployment：自己名下的 ∪ 被可见 Endpoint 引用到的。
        all_deployments = self.store.list_deployments()
        deployment_ids = (
            {item.id for item in all_deployments}
            if everything
            else {
                item.id
                for item in all_deployments
                if item.owner_principal == me or item.id in referenced_deployments
            }
        )

        visible_deployments = [item for item in all_deployments if item.id in deployment_ids]
        referenced_engines = {item.engine_id for item in visible_deployments}
        referenced_models = {item.model_id for item in visible_deployments}

        # 3. Engine / Model：自己名下的 ∪ 被可见 Deployment 引用到的。
        all_engines = self.store.list_engines()
        all_models = self.store.list_models()
        engine_ids = (
            {item.id for item in all_engines}
            if everything
            else {
                item.id
                for item in all_engines
                if item.owner_principal == me or item.id in referenced_engines
            }
        )
        model_ids = (
            {item.id for item in all_models}
            if everything
            else {
                item.id
                for item in all_models
                if item.owner_principal == me or item.id in referenced_models
            }
        )

        # 4. Build：自己名下的 ∪ 被可见 Engine 引用到的。
        #
        # 这一步现在只会命中前一半：没有任何代码写 ``engines.runtime_build_id``
        # （服务进程跑在哪个文件上，要读 ``/proc/<pid>/exe``，而扫描器刻意不读）。
        # 规则先立在这里，是为了「服务标上了它的 build」那天不用再改一遍授权。
        all_builds = self.store.list_builds()
        referenced_builds = {
            item.runtime_build_id
            for item in all_engines
            if item.id in engine_ids and item.runtime_build_id
        }
        build_ids = (
            {item.id for item in all_builds}
            if everything
            else {
                item.id
                for item in all_builds
                if item.owner_principal == me or item.id in referenced_builds
            }
        )

        self._sets = VisibilitySets(
            endpoints=endpoint_ids,
            deployments=frozenset(deployment_ids),
            engines=frozenset(engine_ids),
            models=frozenset(model_ids),
            builds=frozenset(build_ids),
            global_read=everything,
        )
        return self._sets

    def can_see_engine_raw_target(self, engine: Engine) -> bool:
        """engine 的 loopback 目标地址只对 owner 与全局只读者可见。"""
        return self.is_owner_of_everything or engine.owner_principal == self.requester.principal

    def can_see_weight_path(self, model: Model) -> bool:
        return self.is_owner_of_everything or model.owner_principal == self.requester.principal

    def can_see_binary_path(self, build: RuntimeBuild) -> bool:
        """build 的可执行文件路径与权重路径同一条规则。"""
        return self.is_owner_of_everything or build.owner_principal == self.requester.principal

    # -- 读 -----------------------------------------------------------------

    def _require_scope(self, scope: str, *, action: str) -> None:
        if scope not in self.requester.scopes:
            raise AccessDenied(f"missing required scope {scope} for {action}")

    def require_read_scope(self) -> None:
        # 管理员只读 scope 也算「能读」：它本身就是为了跨 owner 读而存在的。
        if SCOPE_REGISTRY_READ in self.requester.scopes or self.is_owner_of_everything:
            return
        raise AccessDenied(f"missing required scope {SCOPE_REGISTRY_READ}")

    def require_write_scope(self) -> None:
        self._require_scope(SCOPE_REGISTRY_WRITE, action="registry write")

    def visible_endpoints(self) -> list[Endpoint]:
        self.require_read_scope()
        ids = self.visibility().endpoints
        # 保持 store 的插入序（rowid）：时间戳只有秒级精度，用它排序不稳定。
        return [item for item in self.store.list_endpoints() if item.id in ids]

    def visible_engines(self) -> list[Engine]:
        self.require_read_scope()
        ids = self.visibility().engines
        return [item for item in self.store.list_engines() if item.id in ids]

    def visible_models(self) -> list[Model]:
        self.require_read_scope()
        ids = self.visibility().models
        return [item for item in self.store.list_models() if item.id in ids]

    def visible_deployments(self) -> list[Deployment]:
        self.require_read_scope()
        ids = self.visibility().deployments
        return [item for item in self.store.list_deployments() if item.id in ids]

    def visible_builds(self) -> list[RuntimeBuild]:
        self.require_read_scope()
        ids = self.visibility().builds
        return [item for item in self.store.list_builds() if item.id in ids]

    def require_engine_read(self, engine_id: str) -> Engine:
        self.require_read_scope()
        if engine_id not in self.visibility().engines:
            # 不可见与不存在给同一个答案。
            raise NotFound(f"engine {engine_id!r} not found")
        found = self.store.engine_by_id(engine_id)
        if found is None:  # pragma: no cover - 集合由库推出，理论不可达
            raise NotFound(f"engine {engine_id!r} not found")
        return found

    def require_model_read(self, model_id: str) -> Model:
        self.require_read_scope()
        if model_id not in self.visibility().models:
            raise NotFound(f"model {model_id!r} not found")
        found = self.store.model_by_id(model_id)
        if found is None:  # pragma: no cover
            raise NotFound(f"model {model_id!r} not found")
        return found

    def require_deployment_read(self, deployment_id: str) -> Deployment:
        self.require_read_scope()
        if deployment_id not in self.visibility().deployments:
            raise NotFound(f"deployment {deployment_id!r} not found")
        found = self.store.deployment_by_id(deployment_id)
        if found is None:  # pragma: no cover
            raise NotFound(f"deployment {deployment_id!r} not found")
        return found

    def require_endpoint_read(self, endpoint_id: str) -> Endpoint:
        self.require_read_scope()
        if endpoint_id not in self.visibility().endpoints:
            raise NotFound(f"endpoint {endpoint_id!r} not found")
        found = self.store.endpoint_by_id(endpoint_id)
        if found is None:  # pragma: no cover
            raise NotFound(f"endpoint {endpoint_id!r} not found")
        return found

    # -- 写 -----------------------------------------------------------------

    def require_owner_for(
        self, owner_principal: str, *, object_type: str, object_id: str, action: str
    ) -> None:
        """只有 owner（或可授权管理员）能改。返回 403 或 404 取决于可见性。"""
        me = self.requester.principal
        if owner_principal == me:
            return
        if SCOPE_ADMIN_MANAGE in self.requester.scopes:
            return
        raise AccessDenied(f"{action} requires ownership of {object_type} {object_id!r}")

    def require_endpoint_owner_or_admin(self, endpoint: Endpoint, *, action: str) -> None:
        """对**已可见**的 endpoint 做写操作。可见但非 owner -> 403。"""
        if endpoint.owner_principal == self.requester.principal:
            return
        if SCOPE_ADMIN_MANAGE in self.requester.scopes:
            return
        raise AccessDenied(
            f"{action} requires ownership of endpoint {endpoint.id!r}"
        )

    def require_share_scope(self) -> None:
        if SCOPE_ENDPOINT_SHARE in self.requester.scopes:
            return
        if SCOPE_ADMIN_MANAGE in self.requester.scopes:
            return
        raise AccessDenied(f"missing required scope {SCOPE_ENDPOINT_SHARE} for sharing")

    def require_invoke_scope(self) -> None:
        self._require_scope(SCOPE_ENDPOINT_INVOKE, action="endpoint invoke")

    # -- 共享目标校验 -------------------------------------------------------

    def validate_grantees(self, grantees: list[str]) -> tuple[str, ...]:
        """校验受托人列表：必须是**受信目录里已审批**的主体，且不含自己。

        「不含自己」不是洁癖：给自己授权是一条永不生效的规则，留着会让 ACL 看起来
        比实际更大，也会让 ``grantees`` 这类审计字段失真。
        """
        if not isinstance(grantees, (list, tuple)):
            raise InvalidRequest("grantees must be a list of principals")
        cleaned: list[str] = []
        for item in grantees:
            if not isinstance(item, str) or not item.strip():
                raise InvalidRequest("each grantee must be a non-empty principal string")
            name = item.strip()
            if name == self.requester.principal:
                raise InvalidRequest("the owner is always able to see their own endpoint")
            principal = self.principals.lookup(name)
            if principal is None:
                raise InvalidRequest(f"unknown principal {name!r}")
            if not principal.approved:
                raise InvalidRequest(f"principal {name!r} is not approved")
            cleaned.append(name)
        return tuple(sorted(set(cleaned)))

    def require_endpoint_ready(self, endpoint: Endpoint, *, action: str) -> None:
        """未验证的 Endpoint 不可共享、不可调用 —— 注册 != 可用。"""
        from .errors import NotVerified

        if endpoint.status != ENDPOINT_READY:
            raise NotVerified(
                f"endpoint {endpoint.id!r} is {endpoint.status!r}; "
                f"{action} requires a verified endpoint"
            )


def assert_no_default_private_trap(endpoint: Endpoint) -> None:
    """构造期的自检：任何新建 Endpoint 的可见性不得是「看似开放」的取值。

    这条断言存在的意义是防回归：如果哪天有人给 ``visibility`` 加了一个新的默认值，
    这里会先炸，而不是等它在生产上悄悄生效。
    """
    if endpoint.visibility not in (VISIBILITY_PRIVATE, VISIBILITY_USERS, VISIBILITY_ALL_APPROVED):
        raise InvalidRequest(f"unsupported visibility mode: {endpoint.visibility!r}")
