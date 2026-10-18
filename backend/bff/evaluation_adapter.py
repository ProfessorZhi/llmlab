"""``backend/evaluation`` ⇒ ``portal-ops/1`` 的 evaluation 段。

同样是翻译层：任务的可见性、状态、结果与「能不能真的派发」全部由
``backend/evaluation`` 决定（``EvaluationService.jobs`` / ``module_gate`` /
``admission``），本层只把它的输出摆进前端契约。

三条刻意的取值
--------------

1. **``scope`` 是按主体算出来的，不是让客户端选的。** 管理员看到别人的任务时
   那一条是 ``global``，自己的仍是 ``mine``；前端契约要求 ``global`` 必须伴随
   ``portal:admin:read``，而这条 scope と同じ角色推出的。
2. **``metric`` 只在 ``status == "done"`` 且**确有**命中样本时才给数字。** 前端
   契约直接拒绝「没结束却有分数的行」。本部署的 mock runner 产出的样本带
   ``synthetic: true``，因此这些分数在结构上就是合成样本，不是测量。
3. **``submit.ready`` 恒为 ``false``。** 真实评测派发在本部署**永远**关闭
   （``AdmissionPolicy.closed`` + 未接线的 adapter + 从不签发 ``evaluation:dispatch:real``）。
   前端那颗按钮的语义是「派发一次评测」，所以它必须保持 disabled —— 而且 UI 分支
   本来就把 ``data-eval-submit`` 列为「永不绑定」的惰性控件，这里把它点亮会同时
   制造一个假的入口和一条自相矛盾的测试。原因字符串直接取模块自己的准入原因，
   不另编一个。
4. **报告目录不被二次改写。** :meth:`reports` / :meth:`report_detail` /
   :meth:`report_sources` 是薄接缝：``source``（``evalscope`` vs ``legacy-benchmark``）
   由 ``backend.evaluation`` 盖章，本层不再映射一遍——那正是"旧 run 被当成 EvalScope
   报告"得以发生的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..evaluation import authz as eval_authz
from ..evaluation import models as eval_models
from ..evaluation.errors import EvaluationError
from ..evaluation.identity import SCOPE_EVAL_READ as eval_read_scope
from ..evaluation.identity import RequesterContext
from . import scopes as bff_scopes

#: 评测模块的状态 ⇒ 前端 ``EVAL_STATUS``。
#: ``canceled`` 在前端词表里没有对应值，因此如实报成 ``unknown`` 并给出原因，
#: 而不是硬塞进 ``failed``（被取消不是失败）。
_STATUS_TO_PORTAL = {
    eval_models.STATUS_QUEUED: "queued",
    eval_models.STATUS_RUNNING: "running",
    eval_models.STATUS_COMPLETED: "done",
    eval_models.STATUS_FAILED: "failed",
    eval_models.STATUS_CANCELED: "unknown",
}

#: 前端允许的分数单位来源。样本单位原样透传，没有就 null。
DEFAULT_LIST_LIMIT = 50


@dataclass(frozen=True)
class EvaluationStack:
    """接线评测面所需的一切。``service`` 由调用方装配（沿用模块自己的构造）。"""

    service: object
    list_limit: int = DEFAULT_LIST_LIMIT


class ModuleEvaluationAdapter:
    """把评测模块接成 BFF 的 ``EvaluationAdapter``。"""

    name = "evaluation"
    attached = True

    def __init__(
        self,
        stack: EvaluationStack,
        *,
        model_lookup: Callable[[str], str | None] | None = None,
        clock: Callable[[], object] | None = None,
    ) -> None:
        if not hasattr(stack.service, "jobs"):
            raise ValueError("service must be an EvaluationService")
        self.stack = stack
        #: ``deployment_id -> model_id``。评测任务的目标是一台**部署**，而前端那一列
        #: 叫「模型」；两者的权威在 Registry，所以这个连接由 BFF 注入，而不是让本层
        #: 去猜、也不是把部署 id 冒充成模型 id。
        self._model_lookup = model_lookup
        self._clock = clock

    # --- 主体桥接 ---------------------------------------------------------

    def requester_for(self, identity) -> RequesterContext:
        """受信主体 ⇒ 评测模块的 ``RequesterContext``。

        四个 set 都按评测自己的词表重建：直接把 Registry 的 scope 原样搬过来会
        让 ``evaluation:read`` 缺席，每个调用都会 403 —— 那不是更安全，是接错线。
        """
        # ``effective_role``, never ``identity.user.role``: the stored role can say
        # ``platform_admin`` (a root-owned Linux grant wrote it), and reading it
        # here is how a plain PAM session used to end up with the global queue.
        role = identity.effective_role
        return RequesterContext(
            principal=identity.principal,
            uid=identity.uid,
            linux_user=identity.linux_user,
            scopes=bff_scopes.evaluation_scopes_for(role),
            roles=frozenset(identity.requester.roles),
        )

    # --- 可用性 -----------------------------------------------------------

    def availability(self, *, identity) -> tuple[bool, str | None]:
        """这个会话能不能读评测队列。同样是 scope 判定，不是试探。"""
        if eval_read_scope in bff_scopes.evaluation_scopes_for(identity.effective_role):
            return True, None
        return False, "missing_required_scope"

    # --- runs / submit ----------------------------------------------------

    def runs(self, *, identity, is_admin: bool) -> list[dict]:
        requester = self.requester_for(identity)
        try:
            envelope = self.stack.service.jobs(
                requester=requester, limit=self.stack.list_limit
            )
        except EvaluationError:
            return []
        jobs = ((envelope.get("data") or {}).get("jobs")) or []
        rows = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            deployment_id = (job.get("target") or {}).get("deployment_id")
            rows.append(
                _run_row(
                    job,
                    subject=identity.principal,
                    is_admin=is_admin,
                    model=self._model_lookup(deployment_id) if self._model_lookup else None,
                )
            )
        return rows

    def submit(self, *, identity, is_admin: bool) -> dict:
        del is_admin
        requester = self.requester_for(identity)
        try:
            decision = self.stack.service.admission_decision(requester=requester)
        except EvaluationError as exc:
            return {"ready": False, "reason": _reason_of(exc)}
        return {
            # 永远不开。见模块说明第 3 条。
            "ready": False,
            "reason": decision.reason or eval_authz.REASON_MISSING_SCOPE,
        }

    # --- 完整 DTO（供同源路由使用） ---------------------------------------

    def module_gate(self, *, identity) -> dict:
        requester = self.requester_for(identity)
        return self.stack.service.module_gate(requester=requester)

    # --- 报告目录（同源 BFF 用） -------------------------------------------

    # 这三条是**薄接缝**：报告目录的可见性、来源隔离与新鲜度全部由
    # ``backend.evaluation`` 决定，本层只把受信主体桥接进去，然后把模块已经渲染好的
    # ``data`` 块原样交给 BFF。**刻意不做二次改写**——把 ``source`` 在这里再映射一遍，
    # 正是"旧 Benchmark run 被前端当成 EvalScope 报告"得以发生的地方。
    #
    # ``include_unowned`` 只是转述：真正的放行在 ``authz.report_visibility``，那里要求
    # ``evaluation:admin:manage``。因此一个成员即便传 true 也看不到无归属记录。

    def reports(self, *, identity, include_unowned: bool = False) -> dict:
        requester = self.requester_for(identity)
        return self.stack.service.reports(
            requester=requester, include_unowned=bool(include_unowned)
        )["data"]

    def report_detail(self, *, identity, report_ref: str, include_unowned: bool = False) -> dict:
        requester = self.requester_for(identity)
        return self.stack.service.report_detail(
            requester=requester, report_ref=report_ref, include_unowned=bool(include_unowned)
        )["data"]

    def report_sources(self, *, identity) -> dict:
        requester = self.requester_for(identity)
        return self.stack.service.report_sources(requester=requester)["data"]

    @property
    def supports_download(self) -> bool:
        """这个 service 有没有交付**第四条** seam（``report_download``）。

        评测 Lane #78 / PR #91 新增了它。本适配器在 #91 合入之前也会被装配，
        所以这里必须能回答「有没有」，而不是在路由上表现成一次 ``AttributeError``
        → 500。没有 ⇒ 路由回 503，那是「本部署没有这条功能」，不是「服务崩了」。
        """
        return callable(getattr(self.stack.service, "report_download", None))

    def report_download(
        self, *, identity, report_ref: str, include_unowned: bool = False
    ) -> dict:
        """受限下载的**判定**（不是字节）。

        与 ``reports`` / ``report_detail`` 同一条薄接缝：可见性、来源隔离、路径
        遍历防护、字节上限全部在 ``backend/evaluation`` 内部判定；本层只把受信主体
        桥接进去，再把模块已经渲染好的 ``data`` 块原样交出去。

        ``include_unowned`` 依旧只是**转述** —— 真正的放行在评测模块的
        ``authz.report_visibility``。一个成员把它设成 true 也看不到无归属报告。
        """
        requester = self.requester_for(identity)
        return self.stack.service.report_download(
            requester=requester,
            report_ref=report_ref,
            include_unowned=bool(include_unowned),
        )["data"]

    def admission_view(self, *, identity) -> dict:
        requester = self.requester_for(identity)
        return self.stack.service.admission_view(requester=requester)

    def capabilities(self) -> dict:
        return self.stack.service.capabilities()


def _reason_of(exc: Exception) -> str:
    code = getattr(exc, "contract_code", None)
    return str(code).split(".")[-1] if code else "evaluation_unavailable"


def _run_row(job: dict, *, subject: str, is_admin: bool, model: str | None = None) -> dict:
    """一条已渲染的评测任务 ⇒ 前端的一行。"""
    raw_status = str(job.get("status") or "")
    status = _STATUS_TO_PORTAL.get(raw_status, "unknown")
    owner = job.get("owner")
    scope = "mine" if owner == subject else "global"

    result = job.get("result") or {}
    metric, unit = _metric_of(result, status=status)

    target = job.get("target") or {}
    # 任务的渲染 DTO 是一台 *deployment*（``EvalTarget``），不含模型引用；因此模型
    # 由 BFF 用 Registry 的连接补上，取不到就退回 deployment id（并如实显示）。
    model_id = model or target.get("deployment_id") or job.get("job_id")
    # ``render_job_status`` does not carry the spec, so the dataset only appears
    # once a result exists (inside ``reproducibility``). A queued job therefore
    # reports no dataset rather than one read out of the store behind the
    # service's back — the UI renders that as UNKNOWN.
    reproducibility = result.get("reproducibility") or {}
    spec_slug = reproducibility.get("dataset_slug")

    reason = None
    if status == "unknown":
        reason = f"unsupported_status:{raw_status}" if raw_status else "status_missing"
    elif status == "done" and metric is None:
        reason = "result_metrics_unavailable"
    elif status in ("queued", "running"):
        reason = (job.get("dispatch") or {}).get("reason")

    return {
        "run_id": job.get("job_id"),
        "scope": scope,
        "model_id": model_id,
        "dataset": spec_slug,
        "status": status,
        "metric": metric,
        "metric_unit": unit,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "reason": reason,
    }


def _metric_of(result: dict, *, status: str):
    """任务的分数：**只有已完成、且确有命中样本**才返回数字。"""
    if status != "done" or not result.get("available"):
        return None, None
    samples = result.get("metrics") or {}
    if not isinstance(samples, dict):
        return None, None
    # 首选 accuracy；否则取第一个「已知」的样本。绝不挑一个 unknown 充数。
    ordered = ["accuracy"] + sorted(name for name in samples if name != "accuracy")
    for name in ordered:
        sample = samples.get(name)
        if not isinstance(sample, dict):
            continue
        if sample.get("state") != eval_models.METRIC_KNOWN:
            continue
        value = sample.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        unit = sample.get("unit")
        return value, unit if isinstance(unit, str) and unit else None
    return None, None


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "EvaluationStack",
    "ModuleEvaluationAdapter",
]
