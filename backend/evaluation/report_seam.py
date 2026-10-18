"""V1 BFF 的评测报告接缝：本 Lane 交付什么、以什么形状、以及哪几条路由由谁挂。

为什么 seam 要写进代码
----------------------

BFF（Issue #79 / PR #85）把它的接缝写成一张**声明式**的表
（``backend/bff/capabilities.py:SOURCE_SEAMS``），并说得很清楚：那份表"同时就是 DTO seam
声明"，别的 Lane "接的是表里的一个条目，不是一份口口相传的约定"。本模块就是评测这一侧的
**对应物**：它把本 Lane 交给 BFF 的东西写成一张可被测试读到的表，而不是一段 PR 描述里的
自然语言。

两张表各自说自己的那一半，这是刻意的：

* BFF 那张说"**我**挂哪条路由、我拒绝做什么"；
* 本表说"**评测**提供哪几条路由、要什么 scope、由应用的哪个属性承载"。

因此本模块**不**复制 BFF 的判定，也不 import 它（``test_static_guarantees`` 禁止跨包
import，而 BFF 的文件也不在本 Lane 的 base 上）。两边合流的判据是一个**字段级**的对齐：
``source`` / ``route`` / ``scope`` / ``adapter_attribute`` 必须逐字相同，由 Integrator
在合流时核对。本模块把这几条写死，正是为了让那次核对是一次字符串比对，而不是一次阅读。

本 Lane 新增的第四条路由
------------------------

BFF 这一轮挂了三条只读路由（``report-sources`` / ``reports`` / ``reports/{report_ref}``）。
Issue #78 要的是四项：列表、详情、**受限下载**，以及来源状态。第四条
（``reports/{report_ref}/download``）在本 Lane 实现（``backend/evaluation/download.py`` +
``EvaluationService.report_download``），**由 BFF Owner 挂载**——``backend/bff/**`` 与它的
``route_map.py`` 不是本 Lane 的改动面。

错误与状态码也在这里声明
------------------------

下载路由在没接线时的正确回答是什么，只有一次机会说清楚。本模块把三个契约码到状态码的
对应写进 :data:`SEAM_ERROR_STATUS`，因为**本仓库的评测 API 自己不发这个状态码**：它对
``NotConfigured`` 的映射是 501（见 ``api.CONTRACT_STATUS``），而 BFF 面按契约要把"这条
来源在这个部署里不存在"讲成 **503**。两张映射各有各的读者，本表是给 BFF 的那一份。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import errors
from .download import SCHEMA_VERSION as DOWNLOAD_SCHEMA_VERSION
from .download import BoundedReportReader, describe_bounds
from .identity import SCOPE_EVAL_READ, RequesterContext

#: 接缝声明的版本。BFF 侧对齐时比对这个字符串，而不是靠比较两份文档的措辞。
SCHEMA_VERSION = "eval-report-seam/1"

#: 本 Lane 在 V1 分派表（Issue #80）里的编号。与 BFF 那张表用的编号是同一套。
OWNER_EVALUATION = "Evaluation#78"

#: 装配形态：这个来源由一个**可选注入的适配器**提供，适配器自己报 ``attached``。
#: 取值与 BFF 侧同名，因为它们是同一条契约的两半。
ATTACHMENT_ADAPTER = "adapter"
#: 装配形态：应用的必需依赖（本 Lane 不使用；写下来是为了让上面那条取值的封闭性可被断言）。
ATTACHMENT_REQUIRED = "required"

ATTACHMENTS = (ATTACHMENT_ADAPTER, ATTACHMENT_REQUIRED)

#: 本 Lane 提供报告面的那一个应用属性名。BFF 侧 ``OpsApplication(evaluation=...)`` 的
#: 形参名必须与它逐字相同——两边写死同一个 token，是为了让"清单里宣称的来源"与"应用里
#: 真的装配的那个对象"能被一条字符串比对连上。
ADAPTER_ATTRIBUTE = "evaluation"


@dataclass(frozen=True)
class ReportRouteSeam:
    """一条报告路由的**装配接缝**：它是谁、喂哪条路由、要什么权限、多新算新。

    判据一律是封闭取值 + 结构校验：一个写错的 ``attachment`` 或一条不是同源绝对路径的
    ``route`` 在**构造期**就被拒绝，而不是等到某个部署把它挂错了才发现。
    """

    source: str
    route: str
    scope: str | None
    ttl_seconds: int | None
    adapter_attribute: str
    attachment: str
    note: str = ""
    owner: str = OWNER_EVALUATION

    def __post_init__(self) -> None:
        if not self.source or "/" in self.source:
            raise errors.InvalidRequest("a source id is a bare token, not a path")
        if not self.route.startswith("/"):
            raise errors.InvalidRequest("route must be an absolute same-origin path")
        if self.attachment not in ATTACHMENTS:
            raise errors.InvalidRequest(f"unknown attachment kind: {self.attachment!r}")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise errors.InvalidRequest("ttl_seconds must be positive when declared")
        if not self.adapter_attribute:
            raise errors.InvalidRequest("a seam must name the attribute that carries it")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "owner": self.owner,
            "route": self.route,
            "scope": self.scope,
            "ttl_seconds": self.ttl_seconds,
            "adapter_attribute": self.adapter_attribute,
            "attachment": self.attachment,
            "note": self.note,
        }


#: 声明式 seam 表。**这是本 Lane 交给 BFF 的唯一真源**：合流时逐字段比对它。
#:
#: 四条路由全部要求 ``evaluation:read``：报告目录的**可见性**（谁能看见哪一条）由
#: ``backend.evaluation`` 的行级 ACL 判定，而"能不能进这个面"由 scope 判定。``ttl_seconds``
#: 一律是 ``None``（不由 TTL 管新鲜度）：逐来源的新鲜度由评测模块自己的 DTO 判定（见
#: ``report_catalog.DEFAULT_STALENESS_SECONDS``），在 BFF 层再判一次会得到"清单说 ready、
#: DTO 说 stale"这种自相矛盾的两份答案。
REPORT_ROUTE_SEAMS: tuple[ReportRouteSeam, ...] = (
    ReportRouteSeam(
        source="evaluation",
        route="/api/evaluation/report-sources",
        scope=SCOPE_EVAL_READ,
        ttl_seconds=None,
        adapter_attribute=ADAPTER_ATTRIBUTE,
        attachment=ATTACHMENT_ADAPTER,
        note="每条来源的**主体范围内**状态；不下发全局存在性，也不跨用户统计。",
    ),
    ReportRouteSeam(
        source="evaluation",
        route="/api/evaluation/reports",
        scope=SCOPE_EVAL_READ,
        ttl_seconds=None,
        adapter_attribute=ADAPTER_ATTRIBUTE,
        attachment=ATTACHMENT_ADAPTER,
        note="只读报告目录；EvalScope 与 legacy-benchmark 由模块盖章，BFF 不重映射 source。",
    ),
    ReportRouteSeam(
        source="evaluation",
        route="/api/evaluation/reports/{report_ref}",
        scope=SCOPE_EVAL_READ,
        ttl_seconds=None,
        adapter_attribute=ADAPTER_ATTRIBUTE,
        attachment=ATTACHMENT_ADAPTER,
        note="按不透明引用取一条；不存在与不可见共用同一条 404。",
    ),
    ReportRouteSeam(
        source="evaluation",
        route="/api/evaluation/reports/{report_ref}/download",
        scope=SCOPE_EVAL_READ,
        ttl_seconds=None,
        adapter_attribute=ADAPTER_ATTRIBUTE,
        attachment=ATTACHMENT_ADAPTER,
        note=(
            "**本 Lane 新增**：受限下载判定（owner ACL + 相对路径遍历防护 + 字节上限）。"
            "本部署的内容读取关闭、报告根未接线，因此如实回答 not_configured，"
            "不是一个 0 字节的成功下载。"
        ),
    ),
)

ROUTES = tuple(seam.route for seam in REPORT_ROUTE_SEAMS)

#: 下载路由。单独取一个常量，因为 BFF 挂载时要引用它，而"从元组里数第几个"不是引用。
DOWNLOAD_ROUTE = "/api/evaluation/reports/{report_ref}/download"

#: 契约码 -> 本路由面**应当**下发的状态码。给 BFF 的那一份，见模块说明。
SEAM_ERROR_STATUS: dict[str, int] = {
    errors.NotFound.contract_code: 404,
    errors.NotConfigured.contract_code: 503,
    errors.ReportDownloadRefused.contract_code: 413,
    errors.ReportPathRejected.contract_code: 400,
}


def manifest() -> dict:
    """本 Lane 交付的接缝清单。一份**无主体**的部署描述，任何登录成员都能读。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER_EVALUATION,
        "adapter_attribute": ADAPTER_ATTRIBUTE,
        "seams": [seam.to_dict() for seam in REPORT_ROUTE_SEAMS],
        "error_status": dict(SEAM_ERROR_STATUS),
        "download": describe_bounds(BoundedReportReader()),
        "download_schema_version": DOWNLOAD_SCHEMA_VERSION,
    }


def self_check() -> None:
    """本表的自洽性。构造期校验挡不住"两条 seam 撞了同一条路由"，因此单独一条。

    这是给测试与启动期用的一个**纯函数**：它不 IO、不联网，读的只是模块常量。
    """
    seen: set[str] = set()
    for seam in REPORT_ROUTE_SEAMS:
        if seam.route in seen:
            raise errors.InvalidRequest(f"duplicate seam route: {seam.route}")
        seen.add(seam.route)
    if DOWNLOAD_ROUTE not in seen:
        raise errors.InvalidRequest("the download route must be declared as a seam")


class EvaluationReportSeam:
    """可注入的评测报告接缝。

    它把 ``EvaluationService`` 的报告方法摆成 BFF 直接可用的形状（``data`` 块），并额外
    提供 BFF 适配器还没有的第四条：:meth:`report_download`。

    **它不决定任何事情。**可见性、来源隔离、新鲜度、下载上界全部在
    ``backend.evaluation`` 内部判定；本类只做主体桥接与形状摆位。这正是一条 seam 应有的
    样子：BFF 侧的 ``ModuleEvaluationAdapter`` 与它读的是同一个 service，因此两边不会
    对"谁看得见哪份报告"给出两份答案。
    """

    name = "evaluation"
    attached = True

    #: 本接缝要求的方法。缺一个就在构造期失败——一个残缺的接缝应当在装配时炸掉，
    #: 而不是在某条路由上表现成 500。
    REQUIRED_SERVICE_METHODS = (
        "report_sources",
        "reports",
        "report_detail",
        "report_download",
    )

    def __init__(self, service: object, *, reader: BoundedReportReader | None = None) -> None:
        missing = [
            name for name in self.REQUIRED_SERVICE_METHODS if not callable(getattr(service, name, None))
        ]
        if missing:
            raise errors.InvalidRequest(
                "an evaluation report seam needs a service with: " + ", ".join(sorted(missing))
            )
        self._service = service
        self._reader = reader

    @property
    def service(self) -> object:
        return self._service

    def report_sources(self, *, requester: RequesterContext) -> dict:
        return self._service.report_sources(requester=requester)["data"]

    def reports(self, *, requester: RequesterContext, include_unowned: bool = False) -> dict:
        return self._service.reports(
            requester=requester, include_unowned=bool(include_unowned)
        )["data"]

    def report_detail(
        self, *, requester: RequesterContext, report_ref: str, include_unowned: bool = False
    ) -> dict:
        return self._service.report_detail(
            requester=requester, report_ref=report_ref, include_unowned=bool(include_unowned)
        )["data"]

    def report_download(
        self, *, requester: RequesterContext, report_ref: str, include_unowned: bool = False
    ) -> dict:
        """第四条接缝：受限下载的**判定**。它返回计划，不返回字节。

        字节只在读取器**显式打开**且这一份产物落在上界之内时才由
        :meth:`BoundedReportReader.read_bounded` 产出——而本部署两者都不成立，因此这里
        的正确回答是"计划里 ``content_read=False`` 且原因是 ``not_configured``"。
        """
        return self._service.report_download(
            requester=requester, report_ref=report_ref, include_unowned=bool(include_unowned)
        )["data"]

    def bounds(self) -> dict:
        reader = self._reader or BoundedReportReader()
        return describe_bounds(reader)


__all__ = [
    "ADAPTER_ATTRIBUTE",
    "ATTACHMENT_ADAPTER",
    "ATTACHMENT_REQUIRED",
    "ATTACHMENTS",
    "DOWNLOAD_ROUTE",
    "OWNER_EVALUATION",
    "REPORT_ROUTE_SEAMS",
    "ROUTES",
    "SCHEMA_VERSION",
    "SEAM_ERROR_STATUS",
    "EvaluationReportSeam",
    "ReportRouteSeam",
    "manifest",
    "self_check",
]
