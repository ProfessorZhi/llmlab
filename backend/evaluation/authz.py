"""行级授权：谁能看见哪个评测任务，以及谁能对它做什么。

这是**唯一**回答「看得到什么」的地方。DTO 层只接收一个已经算好的布尔量，
SQL 层只接收一个已经算好的 owner/项目过滤条件——「谁看得见什么」散落在多处，
是这类系统出错的典型方式。

可见性规则（按顺序）
--------------------

1. 持有 ``evaluation:admin:read`` 或 ``evaluation:admin:manage`` → 全局可见。
   carol 的运维视角。
2. 否则：``job.owner_principal == requester.principal`` **且** requester 是该任务
   所属项目的成员。
3. 否则 → :class:`~backend.evaluation.errors.NotFound`（404）。

第 3 条刻意与「不存在」共用同一个错误。分开会让 ``GET /jobs/{id}`` 变成一个可以逐 id
枚举他人评测任务存在性的预言机；而评测任务的存在性本身就是信息（谁在评测什么模型）。

写操作（取消 / 重试）的规则
--------------------------

* 看不见任务 → 404（同上，不做存在性泄漏）。
* 看得见、但不是 owner、又没有 ``admin:manage`` → 403。**这只可能发生在持有
  ``admin:read`` 的主体身上**——普通成员根本看不见别人的任务，因此他们拿到的是 404。
  两种拒绝都是拒绝，区别只在「你是否已经被允许知道它存在」。
* 是 owner 但缺少对应 scope（cancel / retry）→ 403。

``role`` 在任何一步都不参与。``role == admin`` 而没有 scope，看到的仍是拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import AccessDenied, InvalidRequest, NotFound
from .identity import RequesterContext, SCOPE_EVAL_ADMIN_MANAGE, SCOPE_EVAL_ADMIN_READ, SCOPE_EVAL_REPORT_READ
from .models import EvalJob
from .provenance import ReportOwner

#: 拒绝原因。稳定字符串，供审计与 UI 展示，不参与判定。
REASON_MISSING_SCOPE = "missing_required_scope"
REASON_NOT_PROJECT_MEMBER = "not_a_project_member"
REASON_NOT_OWNER = "not_the_owner"
REASON_MISSING_REPORT_SCOPE = "missing_report_scope"

#: 报告可见性原因（**通过**时的理由；拒绝时用下面的 ``REASON_REPORT_*``）。
VISIBILITY_REASON_OWNER = "owner_and_project_member"
VISIBILITY_REASON_GLOBAL = "global_reader"
VISIBILITY_REASON_ADMIN_UNOWNED = "admin_managed_unowned"

#: 报告可见性原因（**拒绝**时的理由）。
REASON_REPORT_OWNER_UNVERIFIED = "report_owner_not_verified"
REASON_REPORT_NOT_OWNER = "not_the_report_owner_or_member"

#: 「任务不存在」与「任务存在但与你无关」的**唯一**消息。
#:
#: 二者必须逐字节同形，否则 ``GET /jobs/{id}`` 就是一个存在性预言机：状态码与
#: ``contract_code`` 相同但 ``message`` 不同，就足以让人逐 id 问出"这个任务存在吗"。
#: 而任务 id 并非不可猜——幂等提交的 id 是 ``sha256(owner ‖ project ‖ key)[:24]``，
#: 知道对方的键约定就能算出来。所以消息必须**共用同一个常量**，而不是各自措辞。
JOB_NOT_AVAILABLE_MESSAGE = "no such evaluation job"


def job_not_available() -> NotFound:
    """构造「任务不可见」的拒绝。

    所有"看不见某个任务"的路径都必须经过这里，这样"同形"就不是靠各处的措辞自觉，
    而是只有一个来源。
    """
    return NotFound(JOB_NOT_AVAILABLE_MESSAGE)


#: 「报告不存在」与「报告存在但与你无关」的**唯一**消息。与 :data:`JOB_NOT_AVAILABLE_MESSAGE`
#: 同样的理由：二者必须逐字节同形，否则报告引用就成了一个存在性预言机。
REPORT_NOT_AVAILABLE_MESSAGE = "no such evaluation report"


def report_not_available() -> NotFound:
    """构造「报告不可见」的拒绝。目录里所有"看不见某份报告"的路径都走这里。"""
    return NotFound(REPORT_NOT_AVAILABLE_MESSAGE)


@dataclass(frozen=True)
class Project:
    project_id: str
    display_name: str
    members: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise InvalidRequest("project_id must be a non-empty string")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise InvalidRequest("project display_name must be a non-empty string")
        if not all(isinstance(m, str) and m for m in self.members):
            raise InvalidRequest("project members must be non-empty strings")


class ProjectDirectory:
    """受信项目目录：``project_id -> members``，外加「每个主体的归属项目」。

    ``home_projects`` 是一个**服务端配置**：``principal -> project_id``。它是新任务
    落到哪个项目的唯一依据。之所以不从请求里取：项目归属决定可见范围，因此它是一个
    授权输入（见 ``identity.FORBIDDEN_IDENTITY_FIELDS`` 里的 ``project``）。

    构造时会核对「归属项目确实是该主体的成员项目」——一份自相矛盾的目录是配置错误，
    应当在启动时炸掉，而不是在某个用户提交时表现成"查无此项目"。
    """

    def __init__(
        self,
        projects: Iterable[Project],
        *,
        home_projects: Iterable[tuple[str, str]] = (),
    ) -> None:
        by_id: dict[str, Project] = {}
        for project in projects:
            if not isinstance(project, Project):
                raise InvalidRequest("directory entries must be Project")
            if project.project_id in by_id:
                raise InvalidRequest(f"duplicate project: {project.project_id!r}")
            by_id[project.project_id] = project
        self._by_id = by_id

        homes: dict[str, str] = {}
        for principal, project_id in home_projects:
            if not isinstance(principal, str) or not principal:
                raise InvalidRequest("home project needs a principal")
            if principal in homes:
                raise InvalidRequest(f"duplicate home project for {principal!r}")
            project = by_id.get(project_id)
            if project is None:
                raise InvalidRequest(f"home project {project_id!r} is not in the directory")
            if principal not in project.members:
                raise InvalidRequest(
                    f"home project {project_id!r} does not list {principal!r} as a member"
                )
            homes[principal] = project_id
        self._homes = homes

    def lookup(self, project_id: object) -> Project | None:
        if not isinstance(project_id, str):
            return None
        return self._by_id.get(project_id)

    def home_project(self, principal: object) -> str | None:
        if not isinstance(principal, str):
            return None
        return self._homes.get(principal)

    def require_home_project(self, principal: object) -> str:
        """取归属项目；没有配置就抛 :class:`NotFound`（fail closed）。

        没有归属项目意味着"这个主体还没有可以被指派资源的地方"，因此它**不能**提交
        评测。凭空造一个项目出来会让一次提交落进一个谁都看不见的地方。
        """
        home = self.home_project(principal)
        if home is None:
            raise NotFound("this principal has no evaluation project configured")
        return home

    def is_member(self, project_id: str, principal: str) -> bool:
        project = self.lookup(project_id)
        return project is not None and principal in project.members

    def member_projects(self, principal: str) -> frozenset[str]:
        return frozenset(
            project.project_id for project in self._by_id.values() if principal in project.members
        )

    def sorted_ids(self) -> list[str]:
        return sorted(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)


def empty_projects() -> ProjectDirectory:
    return ProjectDirectory(())


def is_global_reader(requester: RequesterContext) -> bool:
    return requester.is_admin_read or requester.is_admin_manage


def require_scope(requester: RequesterContext, scope: str) -> None:
    """缺 scope 即拒绝。``role`` 不参与——这正是「伪造角色不得提权」的那道门。"""
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a trusted RequesterContext")
    if not requester.has_scope(scope):
        raise AccessDenied(f"missing required scope: {scope}", reason=REASON_MISSING_SCOPE)


def require_member(requester: RequesterContext, project_id: str, projects: ProjectDirectory) -> Project:
    """提交要求：主体必须是该项目的成员。

    项目不存在，或主体不是成员 → :class:`NotFound`。一个不是成员的人不应该通过错误码
    的差别学到「这个项目存在」。
    """
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a trusted RequesterContext")
    project = projects.lookup(project_id)
    if project is None or requester.principal not in project.members:
        raise NotFound(f"project {project_id!r} is not available to this principal")
    return project


def can_read_job(requester: RequesterContext, job: EvalJob, projects: ProjectDirectory) -> bool:
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a trusted RequesterContext")
    if is_global_reader(requester):
        return True
    if job.owner_principal != requester.principal:
        return False
    return projects.is_member(job.project_id, requester.principal)


def require_job_read(requester: RequesterContext, job: EvalJob, projects: ProjectDirectory) -> None:
    if not can_read_job(requester, job, projects):
        # 用共享的那个消息常量：与"任务不存在"逐字节同形。
        raise job_not_available()


def visible_job_filter(requester: RequesterContext, projects: ProjectDirectory) -> dict:
    """把可见性翻译成 :meth:`Store.list_jobs` 的参数。

    这是「查询也走同一条规则」的那一步：列表端点不重新实现一遍授权，而是复用这里
    算出的过滤条件。
    """
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a trusted RequesterContext")
    if is_global_reader(requester):
        return {"all_owners": True}
    return {
        "all_owners": False,
        "owner_principal": requester.principal,
        # 只列自己所属项目里的任务：一个已经离开项目的人，不该继续从列表里看到旧任务。
        "project_ids": sorted(projects.member_projects(requester.principal)),
    }


def require_job_mutation(
    requester: RequesterContext,
    job: EvalJob,
    projects: ProjectDirectory,
    *,
    scope: str,
) -> None:
    """取消 / 重试共用的写权限判定。"""
    # 先判可见性：看不见就是 404，不给存在性预言机。
    require_job_read(requester, job, projects)

    owns = job.owner_principal == requester.principal
    manages = requester.is_admin_manage

    if not owns and not manages:
        # 走到这里说明 requester 看得见别人的任务，那只能是持有 admin:read 的主体。
        raise AccessDenied(
            "reading every job does not grant changing every job", reason=REASON_NOT_OWNER
        )
    if not manages and not requester.has_scope(scope):
        raise AccessDenied(f"missing required scope: {scope}", reason=REASON_MISSING_SCOPE)


def may_read_report(requester: RequesterContext, job: EvalJob, projects: ProjectDirectory) -> bool:
    """:func:`require_report_access` 的**不抛异常**版本。

    用于渲染：一个没有报告权限的调用者仍应看到任务状态与"有结果"这个事实，
    只是拿不到那份引用。渲染层据此把报告块渲染成"存在但未授权"，而不是一个 null。
    """
    if not can_read_job(requester, job, projects):
        return False
    if is_global_reader(requester):
        return True
    return requester.has_scope(SCOPE_EVAL_REPORT_READ)


def require_report_access(
    requester: RequesterContext, job: EvalJob, projects: ProjectDirectory
) -> None:
    """报告的**引用**（不是内容）需要额外一次授权。

    知道「有一个结果」与「拿到指向报告文件的引用」是两次不同的决定：后者是一个路径，
    泄露它意味着泄露产物布局。因此 ``evaluation:report:read`` 单独成 scope。
    """
    require_job_read(requester, job, projects)
    if is_global_reader(requester):
        return
    if not requester.has_scope(SCOPE_EVAL_REPORT_READ):
        raise AccessDenied(
            "the report reference requires its own scope", reason=REASON_MISSING_REPORT_SCOPE
        )


# ---------------------------------------------------------------------------
# 报告目录（独立于评测任务）的可见性
#
# 目录里的报告**不是**任务：它们来自 EvalScope 或旧镜像，没有 job_id，因此不能复用
# 上面的任务可见性。规则是同一条纪律的一个变体：
#
# 1. **归属无法核实**（``owner is None``，即旧镜像里没有 UID 绑定的历史产物）默认
#    对**所有人**隐藏——包括只有 ``admin:read`` 的主体。唯一能看到它的是显式传入
#    ``include_unowned`` 的 ``admin:manage`` 主体。这与 Issue #34 的"无法核实 owner
#    的历史数据只能隐藏，或按明确管理员审核的迁移映射显示"一致。
# 2. 有归属：全局读者可见；否则必须同时**是 owner 主体**且**是该项目的成员**。
# 3. 其余一律拒绝。
#
# 拒绝**不**在这层区分"不存在"与"无权"：调用方（``ReportCatalog``）把两种拒绝折进
# 同一条 :func:`report_not_available`，避免报告存在性被逐 id 枚举。
# ---------------------------------------------------------------------------


def report_visibility(
    requester: RequesterContext,
    owner: ReportOwner | None,
    projects: ProjectDirectory,
    *,
    include_unowned: bool = False,
) -> tuple[bool, str]:
    """一份目录报告对 ``requester`` 是否可见，以及原因。**不抛异常**。"""
    if not isinstance(requester, RequesterContext):
        raise InvalidRequest("requester must be a trusted RequesterContext")
    if owner is None:
        if include_unowned and requester.is_admin_manage:
            return True, VISIBILITY_REASON_ADMIN_UNOWNED
        return False, REASON_REPORT_OWNER_UNVERIFIED
    if is_global_reader(requester):
        return True, VISIBILITY_REASON_GLOBAL
    # principal **与** uid 都要对上：归属映射写错一个字段就不该放行。
    if owner.principal == requester.principal and owner.uid == requester.uid:
        if projects.is_member(owner.project_id, requester.principal):
            return True, VISIBILITY_REASON_OWNER
    return False, REASON_REPORT_NOT_OWNER


def may_read_report_reference(
    requester: RequesterContext,
    owner: ReportOwner | None,
    projects: ProjectDirectory,
    *,
    include_unowned: bool = False,
) -> tuple[bool, str]:
    """预览/下载一份目录报告的**引用**是否被授权（内容本身另说）。

    可见性之上再加一道：``evaluation:report:read``（全局读者豁免）。这样"列表里看得到
    这条记录"与"能向它发起一次报告读取"是两个决定。
    """
    allowed, reason = report_visibility(
        requester, owner, projects, include_unowned=include_unowned
    )
    if not allowed:
        return False, reason
    if is_global_reader(requester):
        return True, reason
    if not requester.has_scope(SCOPE_EVAL_REPORT_READ):
        return False, REASON_MISSING_REPORT_SCOPE
    return True, reason


__all__ = [
    "JOB_NOT_AVAILABLE_MESSAGE",
    "REASON_MISSING_REPORT_SCOPE",
    "REASON_MISSING_SCOPE",
    "REASON_NOT_OWNER",
    "REASON_NOT_PROJECT_MEMBER",
    "REASON_REPORT_NOT_OWNER",
    "REASON_REPORT_OWNER_UNVERIFIED",
    "REPORT_NOT_AVAILABLE_MESSAGE",
    "VISIBILITY_REASON_ADMIN_UNOWNED",
    "VISIBILITY_REASON_GLOBAL",
    "VISIBILITY_REASON_OWNER",
    "Project",
    "ProjectDirectory",
    "can_read_job",
    "empty_projects",
    "is_global_reader",
    "job_not_available",
    "may_read_report",
    "may_read_report_reference",
    "report_not_available",
    "report_visibility",
    "require_job_mutation",
    "require_job_read",
    "require_member",
    "require_report_access",
    "require_scope",
    "visible_job_filter",
]
