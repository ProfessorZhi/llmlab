"""评测中心的服务层：把身份、授权、准入、持久层与 DTO 编到一起。

这里的顺序就是安全设计，所以它写在一处，逐条说明：

提交（:meth:`EvaluationService.submit`）
    1. **scope**：缺 ``evaluation:submit`` 即拒。
    2. **请求形状**：未知字段与禁止字段（命令/路径/地址/正文）在解析前全部拒绝。
    3. **幂等重放**：同 (owner, project, key) 且同内容 -> 直接返回原来那个任务，
       不重新消耗配额，也不产生第二个任务。
    4. **项目**：归属项目来自服务端目录，**不是**请求。没有归属项目 -> 404。
    5. **目标授权**：deployment 必须存在、就绪、且对调用者授权（自己的，或被共享的）。
       未授权与不存在共用 404，不做存在性预言机。
    6. **压测**：对**他人共享**的目标，throughput / latency 默认拒绝——共享不等于
       可以打满别人的显存。
    7. **配额**：滑动窗口，先查后记。超限 -> 429 + Retry-After。
    8. **准入**：真实派发必须全体条件成立。本部署一条都不成立，因此任务进入排队，
       并以 ``dispatch_refused`` 记录原因；``synthetic=True``。
    9. **审计**：只记 metadata。

取消（:meth:`EvaluationService.cancel_job`）与重试（:meth:`EvaluationService.retry_job`）
    两者都是**幂等**的。取消一个已取消的任务返回同一个状态并标 ``already_canceled``；
    重试用一个由原任务推出的幂等键，因此重复调用只会产生**一个**新任务。

    "看不见" -> 404，"看得见但不是你的且没有 manage" -> 403，"是你的但缺 scope" -> 403。
    三种拒绝都不改变状态。

结果（:meth:`EvaluationService.result`）
    报告**引用**需要 ``evaluation:report:read``（或全局读）。没有它时，响应里仍然
    说明"这份报告存在、只是未授权"，而不是一个 null——UI 才能区分"没有报告"与
    "你没权限看"。

本模块不做 IO 之外的事：不联网、不起子进程、不碰 GPU、不读报告内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Mapping

from . import authz, dto, models
from .admission import (
    AdmissionDecision,
    AdmissionPolicy,
    GpuFacts,
    RunnerFacts,
    evaluate_admission,
)
from .authz import ProjectDirectory
from .datasets import EVAL_TYPE_SAFETY_PRESET, build_eval_spec, catalog_dto
from .download import (
    MAX_DOWNLOAD_BYTES,
    BoundedReportReader,
    describe_bounds,
    download_plan,
)
from .errors import (
    AccessDenied,
    Conflict,
    InvalidRequest,
    NotFound,
    RateLimited,
    SharedTargetGpuStressRefused,
    TargetNotAuthorized,
    TargetNotReady,
)
from .identity import (
    PrincipalDirectory,
    RequesterContext,
    SCOPE_EVAL_CANCEL,
    SCOPE_EVAL_DISPATCH_REAL,
    SCOPE_EVAL_READ,
    SCOPE_EVAL_RETRY,
    SCOPE_EVAL_SUBMIT,
    TokenDirectory,
    reject_identity_assertions,
    resolve_requester,
)
from .limits import (
    DEFAULT_GLOBAL_SUBMIT_LIMIT,
    DEFAULT_PRINCIPAL_SUBMIT_LIMIT,
    SCOPE_SUBMIT_GLOBAL,
    SCOPE_SUBMIT_PER_PRINCIPAL,
    RateLimitPolicy,
    TimeWindow,
)
from .models import (
    DISPATCH_DISPATCHED,
    DISPATCH_NOT_DISPATCHED,
    DISPATCH_REFUSED,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Clock,
    EvalJob,
    EvalResult,
    EvalTarget,
    deterministic_job_id,
    new_job_id,
    new_result_id,
    utc_now,
    validate_idempotency_key,
    validate_status,
    validate_transition,
)
from .reports import ReportRoot, unconfigured_root
from .provenance import (
    KNOWN_REPORT_SOURCES,
    REASON_SOURCE_UNREACHABLE,
    SOURCE_EMPTY,
    SOURCE_OK,
    SOURCE_STALE,
    ReportSourceSnapshot,
)
from .report_catalog import DEFAULT_STALENESS_SECONDS, ReportCatalog
from .report_readers import unconfigured_source_snapshots, unreachable_snapshot
from .runner import ADAPTER_NOT_WIRED, RUNNER_MOCK, MockRunner, unavailable_runner_facts
from .store import Store
from .targets import TargetCatalog

#: 提交请求**允许**出现的字段。不在其中的一律拒绝——这不是"宽容地忽略一个多余字段"，
#: 而是让调用方立刻知道自己写错了客户端。
REQUEST_FIELDS = frozenset(
    {"deployment_id", "eval_type", "dataset_slug", "params", "preset", "idempotency_key"}
)

#: 重试请求允许出现的字段：**没有**。
#:
#: 以前这里还接受一个 ``idempotency_key``，但 :meth:`EvaluationService.retry_job` 总是
#: 自己推出键（``{原 id}#retry#{attempt+1}``），于是那个字段被**静默丢弃**——
#: 一个"接受了但没有效果"的字段比不接受更糟：调用方会以为自己控制了幂等范围。
RETRY_REQUEST_FIELDS = frozenset()


@dataclass(frozen=True)
class EvaluationConfig:
    """服务端配置。**没有**一项来自请求。"""

    mode: str = dto.MODE_SYNTHETIC
    source: str = "backend.evaluation"
    #: 是否允许 mock runner 产出合成结果。本部署为 True：它是唯一能出结果的 runner。
    mock_runner_enabled: bool = True
    runner_kind: str = RUNNER_MOCK
    principal_submit_limit: RateLimitPolicy = field(
        default_factory=lambda: RateLimitPolicy(
            limit=DEFAULT_PRINCIPAL_SUBMIT_LIMIT, scope=SCOPE_SUBMIT_PER_PRINCIPAL
        )
    )
    global_submit_limit: RateLimitPolicy = field(
        default_factory=lambda: RateLimitPolicy(
            limit=DEFAULT_GLOBAL_SUBMIT_LIMIT, scope=SCOPE_SUBMIT_GLOBAL
        )
    )
    admission: AdmissionPolicy = field(default_factory=AdmissionPolicy.closed)
    gpu_facts: GpuFacts = field(default_factory=GpuFacts.not_configured)
    report_root: ReportRoot = field(default_factory=unconfigured_root)
    #: 报告来源快照的提供者。缺省是"两条来源都未接线"——它产出的是 ``unconfigured``，
    #: **不是** 0 份报告。真实接线时由运维注入一个返回适配器快照的可调用对象。
    report_sources: Callable[[], Iterable[ReportSourceSnapshot]] = unconfigured_source_snapshots
    #: 报告新鲜度窗口（秒）。``ok`` 快照超过它即按 ``stale`` 处理。
    report_staleness_seconds: int = DEFAULT_STALENESS_SECONDS
    #: 报告**内容**下载是否打开。缺省 **False**：本部署只交付"引用 + 授权结论"，正文不
    #: 从这个面出去（``reports.ReportRoot.read_bytes`` 同样保持关闭）。打开它是一次有意的
    #: 部署决定，且仍受 :attr:`report_download_max_bytes` 上界约束。
    report_download_enabled: bool = False
    #: 单份报告下载的字节上限。超界即拒绝，**不**截断——一份被截断的报告看起来像完整的。
    report_download_max_bytes: int = MAX_DOWNLOAD_BYTES
    #: EvalScope 原生界面入口是否开放。本部署不开放（子路径代理未接线）。
    native_entry_available: bool = False
    list_limit: int = 50
    audit_notes: tuple[str, ...] = ()


class EvaluationService:
    def __init__(
        self,
        *,
        store: Store,
        tokens: TokenDirectory,
        principals: PrincipalDirectory,
        projects: ProjectDirectory,
        targets: TargetCatalog,
        runner: object | None = None,
        clock: Clock = utc_now,
        config: EvaluationConfig | None = None,
        runner_facts: RunnerFacts | None = None,
        gpu_facts: GpuFacts | None = None,
    ) -> None:
        if not isinstance(store, Store):
            raise InvalidRequest("store must be a Store")
        self._store = store
        self._tokens = tokens
        self._principals = principals
        self._projects = projects
        self._targets = targets
        self._clock = clock
        self._config = config or EvaluationConfig()
        if self._config.mode not in dto.MODES:
            raise InvalidRequest(f"unknown evaluation mode: {self._config.mode!r}")

        # mock 是唯一 runner 时，runner 默认就是 MockRunner。真实适配器要接线，
        # 必须显式传进来——"忘了传"不会静默降级成 mock。
        if runner is None:
            self._runner = MockRunner() if self._config.mock_runner_enabled else None
        else:
            self._runner = runner

        self._runner_facts = runner_facts or self._facts_for_runner(self._runner)
        self._gpu_facts = gpu_facts if gpu_facts is not None else self._config.gpu_facts
        self._report_root = self._config.report_root
        # 有界读取器与 ``ReportRoot`` 是**两个**决定：根目录接线了，不代表这个部署愿意把
        # 正文发出去。缺省两边都关，因此"内容不从这里流出"是结构事实而不是纪律。
        self._download_reader = BoundedReportReader(
            self._report_root,
            enabled=self._config.report_download_enabled,
            max_bytes=self._config.report_download_max_bytes,
        )

    # -- 身份 ---------------------------------------------------------------

    def replace_targets(self, targets: TargetCatalog) -> None:
        """换掉目标目录。

        真实接线里这是必需的：``backend.registry`` 的 deployment 目录会随用户的
        注册/验证/共享/撤回而变化。**共享被撤回后，已经存在的任务不受影响**
        （报告的可复现性要求"当时用的是谁的目标、以什么权限"），但**新的**提交与重试
        必须按目录的当前状态重新判定——见 :meth:`retry_job`。
        """
        if not isinstance(targets, TargetCatalog):
            raise InvalidRequest("targets must be a TargetCatalog")
        self._targets = targets

    def _facts_for_runner(self, runner: object | None) -> RunnerFacts:
        if runner is None:
            return unavailable_runner_facts(detail="no runner is wired")
        capabilities = getattr(runner, "capabilities", None)
        if callable(capabilities):
            from .runner import runner_facts_from

            return runner_facts_from(capabilities())
        return unavailable_runner_facts(detail="runner does not report capabilities")

    def requester_from_token(self, token: object) -> RequesterContext:
        """凭据 -> 受信主体。目录里没有就是 401，没有兜底。"""
        return resolve_requester(token, self._tokens, self._principals)

    def resolve_request(
        self, *, token: object, header_names: Iterable[str], body: object
    ) -> RequesterContext:
        """传输层入口：先拒绝自报身份，再解析凭据。"""
        reject_identity_assertions(header_names, body)
        return self.requester_from_token(token)

    # -- 准入 ---------------------------------------------------------------

    def runner_can_run_real(self) -> bool:
        """当前装配的 runner 有没有能力跑**真实**评测。

        两个条件都要显式成立：``is_real is True`` **且** ``synthetic_only`` 不是 True。
        缺一个属性就当作"不是真实 runner"——这是 fail closed 的方向：一个没声明自己
        身份的 runner，不能被当成真的。
        """
        runner = self._runner
        if runner is None:
            return False
        if getattr(runner, "is_real", False) is not True:
            return False
        return getattr(runner, "synthetic_only", True) is not True

    def admission_decision(
        self,
        *,
        requester: RequesterContext | None = None,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        """本节此刻能否派发真实评测——以及**对谁**。

        ``requester`` 为 ``None`` 表示匿名/无主体上下文；此时"主体级派发许可"这一条
        按"没有"处理（fail closed），而不是跳过。
        """
        moment = now or self._clock()

        # 注入的 runner 事实是给运维/测试用的，但它**不能**让一个装了 mock runner 的
        # 部署获批真实派发。一个乐观的注入配上 mock，会产出一份自称"真实"的合成报告——
        # 这正是本模块最不能容忍的组合，因此在准入的入口处就被抹平。
        #
        # 抹平的方式是**整体**替换，而不是只改一个布尔量：当装配的 runner 不是真实
        # runner 时，本部署**没有**任何真实适配器，因此它的 kind 与 version 都是
        # "不存在"，而不是"mock 自称的 1.0.0"。把 mock 的版本号当作 adapter_version
        # 报出去，会让运维以为真有一个 EvalScope 1.0.0 适配器在位。
        facts = self._runner_facts
        if not self.runner_can_run_real():
            facts = RunnerFacts(
                real_runner_available=False,
                adapter_kind=None,
                adapter_version=None,
                reasons=(ADAPTER_NOT_WIRED,),
            )

        return evaluate_admission(
            self._config.admission,
            runner=facts,
            gpu=self._gpu_facts,
            now=moment,
            dispatch_scope_granted=self.dispatch_scope_granted(requester),
        )

    @staticmethod
    def dispatch_scope_granted(requester: RequesterContext | None) -> bool:
        """主体是否持有 ``evaluation:dispatch:real``。

        真实派发是**主体级**的一道门，不是"部署开了就人人可派发"。本部署不授予任何主体
        这个 scope，因此它恒为 ``False``；但把它实现出来（而不是只在文档里写）意味着，
        将来运维打开部署开关时，仍然需要一个针对具体主体的授权决定。
        """
        if requester is None:
            return False
        return requester.has_scope(SCOPE_EVAL_DISPATCH_REAL)

    # -- 渲染辅助 -----------------------------------------------------------

    def _envelope(
        self, data: object, requester: RequesterContext | None, *, notes: Iterable[str] = ()
    ) -> dict:
        projects = (
            sorted(self._projects.member_projects(requester.principal))
            if requester is not None
            else []
        )
        payload = dto.envelope(
            data,
            mode=self._config.mode,
            source=self._config.source,
            notes=tuple(self._config.audit_notes) + tuple(notes),
            scope=dto.scope_block(requester, projects=projects),
        )
        dto.validate_dto(payload)
        return payload

    def _render_job(self, requester: RequesterContext, job: EvalJob) -> dict:
        result = self._store.get_result(job.job_id)
        # ``include_report_ref=True`` 表示"渲染报告区块"；``report`` 只在**有权看引用**
        # 时才给。没有权限时区块仍然出现，内容是"存在但未授权"——UI 才能把
        # 「没有报告」与「你没权限看这份报告」分开，而不是两者都渲染成 null。
        authorized = authz.may_read_report(requester, job, self._projects)
        report = None
        if authorized and result is not None and result.report_ref:
            report = self._report_root.reference(result.report_ref)
        return dto.render_job_status(
            job,
            result=result,
            include_report_ref=True,
            report=report,
        )

    def _load_job(self, job_id: object) -> EvalJob:
        job = self._store.get_job(job_id)
        if job is None:
            # 与「看得见但它不是你的」共用同一个错误对象（见 authz.job_not_available）：
            # 两条路径的响应必须逐字节同形，否则这里就是一个存在性预言机。
            raise authz.job_not_available()
        return job

    def _audit(
        self,
        requester: RequesterContext,
        *,
        action: str,
        outcome: str,
        job: EvalJob | None = None,
        reason: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        self._store.record_audit(
            actor_principal=requester.principal,
            actor_uid=requester.uid,
            action=action,
            outcome=outcome,
            job_id=job.job_id if job else None,
            reason=reason,
            detail=detail,
            at=self._clock(),
        )

    # -- 提交 ---------------------------------------------------------------

    def submit(self, *, requester: RequesterContext, request: Mapping[str, object]) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_SUBMIT)
        if not isinstance(request, Mapping):
            raise InvalidRequest("the request body must be a JSON object")

        # 身份字段与禁止字段在这里再挡一次（API 层已经挡过）：服务层可能被 CLI 或
        # 测试直接调用，不能假设调用它的那层做过检查。
        reject_identity_assertions((), dict(request))
        from .datasets import reject_forbidden_spec_fields

        reject_forbidden_spec_fields(request)

        unknown = sorted(set(request) - REQUEST_FIELDS)
        if unknown:
            raise InvalidRequest(f"unsupported request field(s): {', '.join(unknown)}")

        project_id = self._projects.require_home_project(requester.principal)
        idempotency_key = validate_idempotency_key(request.get("idempotency_key"))

        # 规格与目标先解析：这样"同一个幂等键被用于**不同**请求"才能被识别出来。
        spec = build_eval_spec(
            eval_type=request.get("eval_type"),
            dataset_slug=request.get("dataset_slug"),
            params=request.get("params"),
            preset=request.get("preset"),
        )

        target_record = self._targets.resolve(request.get("deployment_id"))
        if target_record is None:
            # 未知目标与未授权目标共用 404。见 targets.py 的说明。
            raise NotFound("no such deployment is available to this principal")
        authorization = target_record.authorized_for(requester, self._principals)
        if not authorization.authorized:
            raise NotFound("no such deployment is available to this principal")
        if not target_record.ready:
            raise TargetNotReady(
                f"deployment {target_record.deployment_id!r} is registered but not verified ready"
            )
        if spec.gpu_stressing_eval_type and not authorization.gpu_stress_allowed:
            raise SharedTargetGpuStressRefused(
                f"{spec.eval_type} would stress a shared target; "
                "an explicit grant from the owner or an administrator is required"
            )

        # 幂等重放：在配额之前。重放**不**消耗配额，也不产生第二个任务。
        #
        # 但"同一个键"必须是**同一件事**。键被复用来提交另一个数据集或另一个目标时，
        # 静默返回第一个任务会让调用方以为自己评测了 B，实际拿到的是 A 的任务——
        # 这比报错糟得多，因此这里显式比对内容并给出 409。
        if idempotency_key is not None:
            existing = self._store.find_by_idempotency(
                owner_principal=requester.principal,
                project_id=project_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if existing.spec.to_dict() != spec.to_dict() or (
                    existing.target.deployment_id != target_record.deployment_id
                ):
                    raise Conflict(
                        "idempotency key was already used for a different request"
                    )
                self._audit(
                    requester,
                    action="submit",
                    outcome="replayed",
                    job=existing,
                    detail={"idempotency": True},
                )
                return self._envelope(
                    self._render_job(requester, existing)
                    | {"replayed": True, "replay_of": existing.job_id},
                    requester,
                    notes=["幂等重放：这是同一个任务，不是第二个。"],
                )

        self._enforce_rate_limits(requester)

        decision = self.admission_decision(requester=requester)
        moment = self._clock()
        target = EvalTarget(
            deployment_id=target_record.deployment_id,
            project_id=target_record.project_id,
            owner_principal=target_record.owner_principal,
            shared=authorization.shared,
            gpu_stress_allowed=authorization.gpu_stress_allowed,
            engine=target_record.engine,
        )
        job = EvalJob(
            job_id=(
                deterministic_job_id(
                    owner_principal=requester.principal,
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                )
                if idempotency_key
                else new_job_id()
            ),
            owner_principal=requester.principal,
            owner_uid=requester.uid,
            project_id=project_id,
            idempotency_key=idempotency_key,
            spec=spec,
            target=target,
            status=STATUS_QUEUED,
            # 本部署 mock 是唯一 runner：任何结果都是合成的。
            synthetic=True,
            runner_kind=self._config.runner_kind,
            dispatch_state=DISPATCH_REFUSED if not decision.allowed else DISPATCH_NOT_DISPATCHED,
            dispatch_reason=decision.reason,
            created_at=moment,
            updated_at=moment,
        )

        stored = self._store.create_job(job)
        self._record_rate_event(requester)
        self._audit(
            requester,
            action="submit",
            outcome="queued",
            job=stored,
            reason=decision.reason if not decision.allowed else None,
            detail={
                "eval_type": spec.eval_type,
                "dataset_slug": spec.dataset_slug,
                "dataset_version": spec.dataset_version,
                "preset": spec.preset,
                "deployment_id": target.deployment_id,
                "target_owner": target.owner_principal,
                "shared": target.shared,
                "gpu_stress_allowed": target.gpu_stress_allowed,
                "to_status": stored.status,
                "dispatch_state": stored.dispatch_state,
                "synthetic": True,
                "runner_kind": stored.runner_kind,
            },
        )

        payload = self._render_job(requester, stored)
        payload["admission"] = dto.render_admission(decision)
        return self._envelope(
            payload,
            requester,
            notes=[
                "任务已进入队列；本部署不派发真实评测，因此不会有真实 runner 执行它。",
            ]
            if not decision.allowed
            else ["任务已进入队列。"],
        )

    def _enforce_rate_limits(self, requester: RequesterContext) -> None:
        now = self._clock()
        for policy, key in (
            (self._config.principal_submit_limit, requester.principal),
            (self._config.global_submit_limit, "*"),
        ):
            since = policy.window_start(now)
            used = self._store.count_rate_events(policy.scope, key, since=since)
            oldest = self._store.oldest_rate_event_at(policy.scope, key, since=since)
            decision = policy.evaluate(used=used, now=now, oldest_event=oldest)
            if not decision.allowed:
                raise RateLimited(
                    "evaluation submission quota exceeded", retry_after=decision.retry_after
                )

    def _record_rate_event(self, requester: RequesterContext) -> None:
        now = self._clock()
        self._store.record_rate_event(SCOPE_SUBMIT_PER_PRINCIPAL, requester.principal, at=now)
        self._store.record_rate_event(SCOPE_SUBMIT_GLOBAL, "*", at=now)

    # -- 读取 ---------------------------------------------------------------

    def job(self, *, requester: RequesterContext, job_id: object) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_READ)
        job = self._load_job(job_id)
        authz.require_job_read(requester, job, self._projects)
        return self._envelope(self._render_job(requester, job), requester)

    def jobs(
        self,
        *,
        requester: RequesterContext,
        status: object = None,
        limit: object = None,
        offset: object = 0,
    ) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_READ)
        filter_kwargs = authz.visible_job_filter(requester, self._projects)

        statuses = None
        if status is not None:
            if isinstance(status, (list, tuple)):
                statuses = [validate_status(item) for item in status]
            else:
                statuses = [validate_status(status)]

        bounded = self._bounded_list_limit(limit)
        jobs = self._store.list_jobs(statuses=statuses, limit=bounded, offset=offset, **filter_kwargs)

        rendered = [self._render_job(requester, job) for job in jobs]
        data = {
            "jobs": rendered,
            "returned": len(rendered),
            "limit": bounded,
            "offset": int(offset) if isinstance(offset, int) and not isinstance(offset, bool) else 0,
            "empty_state": None if rendered else dto.empty_state_block(what="evaluation jobs"),
        }
        return self._envelope(data, requester)

    def _bounded_list_limit(self, limit: object) -> int:
        if limit is None:
            return int(self._config.list_limit)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise InvalidRequest("limit must be an integer")
        if limit < 1:
            raise InvalidRequest("limit must be >= 1")
        return min(limit, 200)

    def result(self, *, requester: RequesterContext, job_id: object, version: object = None) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_READ)
        job = self._load_job(job_id)
        authz.require_job_read(requester, job, self._projects)

        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise InvalidRequest("version must be a positive integer")

        result = self._store.get_result(job.job_id, version=version)
        authorized = authz.may_read_report(requester, job, self._projects)
        report = None
        if authorized and result is not None and result.report_ref:
            report = self._report_root.reference(result.report_ref)

        summary = dto.render_result_summary(result, include_report_ref=True, report=report)
        if summary["available"]:
            summary["reproducibility"] = dto.reproducibility_block(job)

        data = {
            "job_id": job.job_id,
            "job_status": job.status,
            "status_label": dto.STATUS_LABELS.get(job.status, job.status),
            "synthetic": job.synthetic,
            "attempt": job.attempt,
            "result": summary,
            "versions_available": [item.version for item in self._store.list_results(job.job_id)],
        }
        return self._envelope(data, requester)

    # -- 取消 / 重试 --------------------------------------------------------

    def cancel_job(
        self, *, requester: RequesterContext, job_id: object, reason: object = None
    ) -> dict:
        job = self._load_job(job_id)
        authz.require_job_mutation(requester, job, self._projects, scope=SCOPE_EVAL_CANCEL)

        if isinstance(reason, str) and len(reason) > 200:
            raise InvalidRequest("cancel reason is too long")

        if job.status == STATUS_CANCELED:
            # 幂等：再取消一次不报错，也不产生第二次迁移。
            return self._cancel_payload(requester, job, already_canceled=True, changed=False)
        if job.terminal:
            raise Conflict(f"a {job.status} job cannot be canceled")

        now = self._clock()
        updated = self._store.transition_job(
            job.job_id,
            expected_status=job.status,
            new_status=STATUS_CANCELED,
            at=now,
            cancel_requested=True,
            finished_at=now,
        )
        if updated is None:
            # 竞争者先改了状态。重读一次，按"已经处理过"处理成幂等成功——
            # 只要最终状态是 canceled，"另一个人先取消了"就不是错误。
            current = self._store.get_job(job.job_id)
            if current is not None and current.status == STATUS_CANCELED:
                self._audit(requester, action="cancel", outcome="idempotent", job=current)
                return self._cancel_payload(requester, current, already_canceled=True, changed=False)
            raise Conflict("the job changed state concurrently; re-read it and try again")

        self._audit(
            requester,
            action="cancel",
            outcome="canceled",
            job=updated,
            reason=reason if isinstance(reason, str) else None,
            detail={"from_status": job.status, "to_status": updated.status},
        )
        return self._cancel_payload(requester, updated, already_canceled=False, changed=True)

    def _cancel_payload(
        self, requester: RequesterContext, job: EvalJob, *, already_canceled: bool, changed: bool
    ) -> dict:
        data = self._render_job(requester, job)
        data["already_canceled"] = already_canceled
        data["changed"] = changed
        notes = (
            ["该任务此前已取消；本次调用没有改变任何状态（幂等）。"]
            if already_canceled
            else ["任务已取消。"]
        )
        return self._envelope(data, requester, notes=notes)

    def retry_job(
        self,
        *,
        requester: RequesterContext,
        job_id: object,
        request: Mapping[str, object] | None = None,
    ) -> dict:
        job = self._load_job(job_id)
        authz.require_job_mutation(requester, job, self._projects, scope=SCOPE_EVAL_RETRY)

        payload = dict(request or {})
        reject_identity_assertions((), payload)
        unknown = sorted(set(payload) - RETRY_REQUEST_FIELDS)
        if unknown:
            raise InvalidRequest(f"unsupported retry field(s): {', '.join(unknown)}")

        if not job.retryable:
            raise Conflict(
                f"a {job.status} job cannot be retried; only failed or canceled jobs can"
            )

        # 重试前的**重新授权**：这一点容易漏掉，而漏掉意味着一次被撤回的共享仍然能
        # 继续生成新任务。目录的当前状态才是判据，不是当初的快照。
        target_record = self._targets.resolve(job.target.deployment_id)
        if target_record is None:
            raise NotFound("no such deployment is available to this principal")
        authorization = target_record.authorized_for(requester, self._principals)
        if not authorization.authorized:
            raise NotFound("no such deployment is available to this principal")
        if not target_record.ready:
            raise TargetNotReady(
                f"deployment {target_record.deployment_id!r} is registered but not verified ready"
            )
        if job.spec.gpu_stressing_eval_type and not authorization.gpu_stress_allowed:
            raise SharedTargetGpuStressRefused(
                f"{job.spec.eval_type} would stress a shared target; "
                "an explicit grant from the owner or an administrator is required"
            )

        # 重试的幂等键由原任务推出：重复调用只会产生**一个**新任务。
        key = f"{job.job_id}#retry#{job.attempt + 1}"
        existing = self._store.find_by_idempotency(
            owner_principal=job.owner_principal, project_id=job.project_id, idempotency_key=key
        )
        if existing is not None:
            data = self._render_job(requester, existing)
            data["replayed"] = True
            data["retry_of"] = job.job_id
            return self._envelope(
                data, requester, notes=["幂等重放：这次重试此前已经创建过任务。"]
            )

        decision = self.admission_decision(requester=requester)
        now = self._clock()
        # 快照按**现在**的授权重建：重试是在当前共享/许可状态下跑的一次新任务。
        retry_target = EvalTarget(
            deployment_id=target_record.deployment_id,
            project_id=target_record.project_id,
            owner_principal=target_record.owner_principal,
            shared=authorization.shared,
            gpu_stress_allowed=authorization.gpu_stress_allowed,
            engine=target_record.engine,
        )
        retry = EvalJob(
            job_id=new_job_id(),
            owner_principal=job.owner_principal,
            owner_uid=job.owner_uid,
            project_id=job.project_id,
            idempotency_key=key,
            spec=job.spec,
            target=retry_target,
            status=STATUS_QUEUED,
            synthetic=True,
            runner_kind=job.runner_kind,
            dispatch_state=DISPATCH_REFUSED if not decision.allowed else DISPATCH_NOT_DISPATCHED,
            dispatch_reason=decision.reason,
            created_at=now,
            updated_at=now,
            attempt=job.attempt + 1,
            retry_of=job.job_id,
        )
        stored = self._store.create_job(retry)
        # 一次重试牵涉**两个**任务，因此两边都留痕：原任务上记"被重试过、重试成了哪个",
        # 新任务上记"它是一次重试的产物"。只记一边会让另一边的审计链断掉。
        self._audit(
            requester,
            action="retry",
            outcome="queued",
            job=job,
            reason=decision.reason if not decision.allowed else None,
            detail={
                "attempt": stored.attempt,
                "retry_job_id": stored.job_id,
                "from_status": job.status,
                "to_status": stored.status,
                "dispatch_state": stored.dispatch_state,
            },
        )
        self._audit(
            requester,
            action="submit",
            outcome="queued",
            job=stored,
            reason=decision.reason if not decision.allowed else None,
            detail={
                "attempt": stored.attempt,
                "retry_of": job.job_id,
                "eval_type": stored.spec.eval_type,
                "deployment_id": stored.target.deployment_id,
                "to_status": stored.status,
                "dispatch_state": stored.dispatch_state,
                "synthetic": True,
            },
        )
        data = self._render_job(requester, stored)
        data["retry_of"] = job.job_id
        data["replayed"] = False
        return self._envelope(data, requester, notes=["已创建一个重试任务（新任务，原任务保留）。"])

    # -- 开发专用的 mock 执行路径 -------------------------------------------

    def run_mock_attempt(self, *, requester: RequesterContext, job_id: object) -> dict:
        """让 mock runner 把一个排队中的任务跑成结果。**只产出合成样本。**

        这是本部署唯一能产生"已完成"的路径，因此它必须明确得让人无法误解：

        * 只有 mock runner 在位时才允许（真实适配器在位时直接拒绝）；
        * 每次都写 ``synthetic=True``；
        * 不碰 GPU、不读文件、不联网；
        * 审计里记 ``runner_kind=mock``。
        """
        if not self._config.mock_runner_enabled or not isinstance(self._runner, MockRunner):
            raise Conflict("the mock execution path is not enabled for this deployment")

        job = self._load_job(job_id)
        authz.require_job_mutation(requester, job, self._projects, scope=SCOPE_EVAL_SUBMIT)

        if job.terminal:
            raise Conflict(f"a {job.status} job cannot be executed again")
        if job.cancel_requested:
            # 取消请求已经下达：不要"顺便跑一下"。
            canceled = self._store.transition_job(
                job.job_id,
                expected_status=job.status,
                new_status=STATUS_CANCELED,
                at=self._clock(),
                finished_at=self._clock(),
            )
            if canceled is None:
                canceled = self._store.get_job(job.job_id) or job
            raise Conflict(f"a cancellation was requested for this job ({canceled.status})")

        now = self._clock()
        if job.status == STATUS_QUEUED:
            running = self._store.transition_job(
                job.job_id,
                expected_status=STATUS_QUEUED,
                new_status=STATUS_RUNNING,
                at=now,
                started_at=now,
            )
            if running is None:
                raise Conflict("the job changed state concurrently")
            job = running

        sample = self._runner.run(job, attempt=job.attempt)
        if sample.synthetic is not True:
            # 结构性不变量：mock 不允许产出非合成样本。真的发生就是 bug。
            raise AssertionError("the mock runner produced a non-synthetic sample")

        result = EvalResult(
            result_id=new_result_id(),
            job_id=job.job_id,
            version=1,
            synthetic=True,
            metrics=sample.metrics,
            report_ref=sample.report_ref,
            summary=sample.summary,
            created_at=self._clock(),
        )
        stored_result = self._store.add_result(result)

        finished = self._store.transition_job(
            job.job_id,
            expected_status=STATUS_RUNNING,
            new_status=STATUS_COMPLETED,
            at=self._clock(),
            finished_at=self._clock(),
            synthetic=True,
        )
        if finished is None:
            raise Conflict("the job changed state concurrently")

        self._audit(
            requester,
            action="mock_run",
            outcome="completed",
            job=finished,
            detail={
                "runner_kind": self._config.runner_kind,
                "synthetic": True,
                "result_version": stored_result.version,
                "report_referenced": bool(stored_result.report_ref),
                "eval_type": finished.spec.eval_type,
            },
        )
        data = self._render_job(requester, finished)
        data["executed_by"] = "mock"
        return self._envelope(
            data,
            requester,
            notes=[
                "本结果由 mock runner 产出，synthetic=true；它不代表任何真实模型的性能或质量。",
            ],
        )

    # -- 元数据视图 ---------------------------------------------------------

    def datasets(self, *, requester: RequesterContext) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_READ)
        return self._envelope(catalog_dto(), requester)

    def admission_view(self, *, requester: RequesterContext) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_READ)
        decision = self.admission_decision(requester=requester)
        data = dto.render_admission(decision)
        data["report_root"] = self._report_root.describe()
        data["runner"] = decision.runner.to_dict()
        # 这一条是"装配"的事实，不是"策略"的：装了 mock 就不可能真实派发。
        data["runner_can_run_real"] = self.runner_can_run_real()
        return self._envelope(data, requester)

    def module_gate(self, *, requester: RequesterContext) -> dict:
        # 模块目录需要可读权限；缺权限时**仍然**返回一份说明为什么不可用的视图，
        # 而不是 403 空白——「未开放时说明原因」是前端契约的一部分。
        decision = self.admission_decision(requester=requester)
        data = dto.render_module_gate(
            requester=requester if requester.has_scope(SCOPE_EVAL_READ) else None,
            admission=decision,
            report_root=self._report_root,
            native_entry_available=self._config.native_entry_available,
        )
        if not requester.has_scope(SCOPE_EVAL_READ):
            data["state"] = dto.MODULE_UNAVAILABLE
            data["reason"] = dto.REASON_MISSING_SCOPE
        return self._envelope(data, requester)

    def monitoring(self, *, requester: RequesterContext) -> dict:
        authz.require_scope(requester, SCOPE_EVAL_READ)
        # 计数与列表用**同一组**过滤条件（见 ``store.count_by_status`` 的说明），
        # 否则面板上的总数会与它旁边列出的任务数对不上。
        filter_kwargs = authz.visible_job_filter(requester, self._projects)
        counts = self._store.count_by_status(**filter_kwargs)
        visible = self._store.list_jobs(limit=200, **filter_kwargs)
        last_result_at = None
        results_total = 0
        for job in visible:
            latest = self._store.get_result(job.job_id)
            if latest is not None:
                results_total += 1
                stamp = models.isoformat(latest.created_at)
                if stamp is not None and (last_result_at is None or stamp > last_result_at):
                    last_result_at = stamp

        decision = self.admission_decision(requester=requester)
        data = dto.render_monitoring(
            counts=counts,
            requester=requester,
            admission=decision,
            report_root=self._report_root,
            last_result_at=last_result_at,
            results_total=results_total,
        )
        data["module"] = dto.render_module_gate(
            requester=requester,
            admission=decision,
            report_root=self._report_root,
            native_entry_available=self._config.native_entry_available,
        )
        return self._envelope(data, requester)

    # -- 报告目录（EvalScope / 旧 Benchmark，只读） -------------------------

    def _report_snapshots(self) -> tuple[ReportSourceSnapshot, ...]:
        """当前配置下的来源快照。

        提供者抛异常时**不**把它折成"0 份"：那会把一次读取失败说成"确实没有报告"。
        取而代之的是两条 ``unreachable`` 快照——UI 据此显示"未知"，而不是 0。
        """
        provider = self._config.report_sources
        if not callable(provider):
            return unconfigured_source_snapshots()
        try:
            snapshots = tuple(provider())
        except Exception:  # noqa: BLE001 - 来源提供者故障 = 未知，不是 0
            return tuple(
                unreachable_snapshot(source, detail=REASON_SOURCE_UNREACHABLE)
                for source in KNOWN_REPORT_SOURCES
            )
        for snapshot in snapshots:
            if not isinstance(snapshot, ReportSourceSnapshot):
                raise InvalidRequest("the report source provider returned a non-snapshot")
        return snapshots

    def report_catalog(self) -> ReportCatalog:
        """本次读取的只读目录。每次调用都重新取快照——目录是快照，不是缓存。"""
        return ReportCatalog(
            self._report_snapshots(),
            now=self._clock(),
            staleness_seconds=self._config.report_staleness_seconds,
        )

    def _render_catalog_reports(
        self,
        requester: RequesterContext,
        catalog: ReportCatalog,
        *,
        include_unowned: bool,
    ) -> list[dict]:
        rows = []
        for entry, reason in catalog.visible(
            requester, self._projects, include_unowned=include_unowned
        ):
            # 预览与下载各自再判一次授权；这里两者同权（都读的是**引用**，不是内容）。
            allowed, _why = authz.may_read_report_reference(
                requester,
                entry.record.owner,
                self._projects,
                include_unowned=include_unowned,
            )
            rows.append(
                dto.render_report_entry(
                    entry,
                    visibility=reason,
                    preview_authorized=allowed,
                    download_authorized=allowed,
                )
            )
        return rows

    def _report_empty_state(self, scoped_status: tuple[dict, ...], rows: list[dict]) -> dict | None:
        """没有可见报告时，说清楚**是哪一种**没有。

        判据**只**来自主体范围内的来源状态（:meth:`ReportCatalog.scoped_source_status`）：
        "这个主体是不是连一条可见记录都没有"。全局记录数不参与——那会让别人的报告改变
        我的空态，从而把"来源里有东西"泄漏出去。
        """
        if rows:
            return None
        answered = [
            row["source"]
            for row in scoped_status
            if row["state"] in (SOURCE_OK, SOURCE_EMPTY, SOURCE_STALE)
        ]
        if not answered:
            # 全部来源未接线或不可达 -> 未知。**不是** 0。
            return dto.empty_state_block(what="evaluation reports")
        return dto.counted_zero_block(what="evaluation reports", sources=answered)

    def reports(
        self, *, requester: RequesterContext, include_unowned: object = False
    ) -> dict:
        """可见的报告列表，按来源分开。缺 ``evaluation:read`` 即拒。"""
        authz.require_scope(requester, SCOPE_EVAL_READ)
        flag = self._unowned_flag(include_unowned)
        catalog = self.report_catalog()
        rows = self._render_catalog_reports(requester, catalog, include_unowned=flag)
        sources = dto.render_report_sources(
            catalog.scoped_source_status(requester, self._projects, include_unowned=flag)
        )
        data = dto.render_report_catalog(
            reports=rows,
            sources=sources,
            include_unowned=flag,
            empty_state=self._report_empty_state(
                catalog.scoped_source_status(requester, self._projects, include_unowned=flag),
                rows,
            ),
        )
        return self._envelope(
            data,
            requester,
            notes=[
                "报告内容读取在本部署是关闭的；这里给出的是引用、来源与授权结论。",
            ],
        )

    def report_detail(
        self,
        *,
        requester: RequesterContext,
        report_ref: object,
        include_unowned: object = False,
    ) -> dict:
        """按不透明引用取一条。不存在与不可见共用同一条 404。"""
        authz.require_scope(requester, SCOPE_EVAL_READ)
        flag = self._unowned_flag(include_unowned)
        catalog = self.report_catalog()
        entry, reason = catalog.lookup(
            requester, self._projects, report_ref, include_unowned=flag
        )
        allowed, _why = authz.may_read_report_reference(
            requester, entry.record.owner, self._projects, include_unowned=flag
        )
        data = {
            "report": dto.render_report_entry(
                entry,
                visibility=reason,
                preview_authorized=allowed,
                download_authorized=allowed,
            ),
            "source_state": entry.source_state,
        }
        return self._envelope(data, requester)

    def report_sources(self, *, requester: RequesterContext) -> dict:
        """每条来源的**主体范围内**状态。**不**下发全局存在性、也**不**跨用户统计。

        这里刻意不用 :meth:`ReportCatalog.pipeline_status`：全局 ``ok`` 依构造至少意味着
        一条报告存在，对普通成员就是一个跨租户存在性侧信道。
        """
        authz.require_scope(requester, SCOPE_EVAL_READ)
        catalog = self.report_catalog()
        data = {
            "sources": dto.render_report_sources(
                catalog.scoped_source_status(requester, self._projects)
            ),
            # 每条来源的接入面貌（不回显 base URL）：这是部署配置，不是任何人的数据。
            "configured": sorted(snapshot.source for snapshot in catalog.snapshots),
        }
        return self._envelope(data, requester)

    def report_download(
        self,
        *,
        requester: RequesterContext,
        report_ref: object,
        include_unowned: object = False,
    ) -> dict:
        """受限下载的**判定**。返回计划，**不**返回字节。

        它沿用 :meth:`report_detail` 的取件路径：同一个目录、同一次 ACL、同一条"不存在与
        不可见共用 404"的 404。因此下载面**不可能**比详情面看见更多——一份在详情里拿不到
        的报告，在这里也拿不到，而且回答逐字节相同。

        判定分两层，DTO 里也分成两个字段：

        * ``authorized`` —— 策略允许不允许你发起这次读取；
        * ``content_read`` —— 这一份此刻**真的**能不能读出来。

        本部署 ``content_read`` 恒为 ``False``，原因如实写着 ``report_content_reading_disabled``
        （读取器未打开）；根目录也没接线，因此即便打开读取器，原因会是
        ``report_root_not_configured``。两者都不是"0 字节的成功下载"。
        """
        authz.require_scope(requester, SCOPE_EVAL_READ)
        flag = self._unowned_flag(include_unowned)
        catalog = self.report_catalog()
        entry, _reason = catalog.lookup(
            requester, self._projects, report_ref, include_unowned=flag
        )
        plan = download_plan(
            requester,
            entry,
            self._projects,
            reader=self._download_reader,
            include_unowned=flag,
        )
        return self._envelope(
            plan.to_dict(),
            requester,
            notes=[
                "这是下载**判定**，不是正文；字节只在显式打开内容读取且有界通过后才流动。",
            ],
        )

    @staticmethod
    def _unowned_flag(raw: object) -> bool:
        """``include_unowned`` 只有布尔真值才算数；任何其它取值按 ``False``（fail closed）。

        这个开关**本身不授权**任何东西：它只是"请把无归属的记录也纳入候选"的一次请求，
        真正的放行在 :func:`authz.report_visibility`——那里要求 ``admin:manage``。因此一个
        普通成员传 ``include_unowned=true`` 仍然看不到任何无归属记录。
        """
        return raw is True

    def capabilities(self) -> dict:
        """适配器与准入的只读描述（无需身份；不含任何主体信息）。"""
        runner = self._runner
        describe = getattr(runner, "describe", None)
        adapter = describe() if callable(describe) else (
            runner.capabilities().to_dict() if callable(getattr(runner, "capabilities", None)) else None
        )
        return {
            "schema_version": dto.SCHEMA_VERSION,
            "admission": self.admission_decision().to_dict(),
            "runner": self._runner_facts.to_dict(),
            "adapter": adapter,
            "report_root": self._report_root.describe(),
            # 下载面的上界与开关：这是**部署**描述（上限多少、开没开），不是任何人的数据。
            "report_download": describe_bounds(self._download_reader),
            "dispatch_scope_required": SCOPE_EVAL_DISPATCH_REAL,
            "time_window": self._config.admission.window.to_dict(),
            "runner_can_run_real": self.runner_can_run_real(),
        }

    def capabilities_envelope(self, *, requester: RequesterContext | None = None) -> dict:
        """:meth:`capabilities` 的**信封**版本。

        健康检查也要有 ``schema_version`` / ``synthetic`` 与 ``scope``（此处
        ``principal`` 为 ``None``，因为是匿名访问）——每条响应都声明自己的身份与形态，
        前端就不必为"这一个端点"写特例。
        """
        return self._envelope(self.capabilities(), requester)


def default_config(**overrides: object) -> EvaluationConfig:
    """本部署的默认配置：GPU 已满，mock 是唯一 runner，不派发真实评测。"""
    return EvaluationConfig(**overrides)  # type: ignore[arg-type]


__all__ = [
    "RETRY_REQUEST_FIELDS",
    "REQUEST_FIELDS",
    "EvaluationConfig",
    "EvaluationService",
    "default_config",
]
