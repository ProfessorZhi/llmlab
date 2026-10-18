"""EvalScope 评测中心的**平台层**契约。

这个包不做评测。它做的是让"谁可以评测什么、结果怎么被看见、真实执行什么时候
被允许"变成服务端可以回答、可以审计、可以测试的问题。EvalScope 本体
（真实基准、真实 runner）**不在**这里，也不由本包调用。

本部署的立场（写在这里，因为它是所有模块共同的前提）
--------------------------------------------------

1. **GPU 已满，真实评测不派发。** 准入是一个合取条件（管理员开关 + 真实 runner +
   已知版本 + 时间窗 + 观测到的显存富余），本部署一条都不成立，因此
   ``admission.allowed`` 恒为 ``False``，任务只进入队列。
2. **mock 是唯一 runner，因此每个结果都是合成的。** ``synthetic`` 字段没有默认值，
   mock runner 的产出里它是写死的常量。合成样本永远不冒充真实评测。
3. **身份只能来自凭据。** ``owner`` / ``uid`` / ``role`` / ``project`` 出现在请求里
   （header、body 或查询串）一律**拒绝整个请求**，而不是静默忽略。
4. **测不到就是 ``None``。** 缺失的指标、未观测的显存、未接线的报告根，都是
   ``None`` + 一个状态字符串，不是 ``0``。UI 上"等待数据接入"与"得分 0"必须能分开。
5. **未授权与不存在共用 404。** 否则端点就成了逐 id 枚举他人资源的预言机。
6. **真实执行需要一个单独的决定。** throughput / latency 会压 GPU，对他人共享的
   目标默认拒绝；真实派发还需要 ``evaluation:dispatch:real``——本部署不授予任何主体。
7. **来源不是负载说了算。** EvalScope 报告与旧 Benchmark 镜像 run 由**适配器**盖章，
   负载里的 ``source`` 字段不被读取。归属无法核实的历史 run 默认对所有人隐藏
   （见 ``provenance`` / ``report_readers``）。
8. **报告正文默认不从这个面出去。** 下载的**判定**（授权、来源、上界、原因）照常给出，
   但"能不能读"与"有没有权限"是两个字段：本部署的内容读取关闭、报告根未接线，因此
   ``content_read`` 恒为 ``False`` 并给出原因——不是一个 0 字节的成功下载
   （见 ``download``）。

模块布局
--------

======================  ==========================================================
模块                    职责
======================  ==========================================================
``identity``            受信主体目录、scope、自报身份拒绝
``authz``               项目/行级可见性与写权限（唯一回答"看得见什么"的地方）
``targets``             受信评测目标目录：授权、共享、压测许可
``datasets``            评测类型、数据集允许清单、参数范围、安全预设
``models``              领域对象与显式状态机
``store``               SQLite 持久层（含幂等与审计）
``limits``              提交配额与时间窗
``admission``           GPU 准入（真实派发的闸门）
``runner``              mock runner 与 EvalScope 适配器桩
``reports``             报告引用的只读安全路径
``provenance``          报告来源词表（EvalScope vs 旧 Benchmark）、记录与快照
``report_catalog``      来源快照 -> 按 ACL 过滤的只读目录（新鲜度、0 vs 未知）
``report_readers``      EvalScope ``/api/v1/reports`` 与旧镜像的只读适配器
``download``            报告受限下载的判定与有界读取（默认关闭，超界即拒绝）
``report_seam``         V1 BFF 的接缝清单（哪几条路由、什么 scope）与可注入接缝
``dto``                 V6 UI / Swagger 所需的 JobStatus、ResultSummary
``service``             编排：身份 -> 授权 -> 准入 -> 持久 -> DTO
``api``                 WSGI 应用与契约错误到状态码的映射
``openapi``             由路由表生成的 OpenAPI 文档
``cli``                 CLI 数据契约
======================  ==========================================================

依赖方向是单向的：``api`` / ``cli`` -> ``service`` -> 其余。``identity`` / ``models`` /
``datasets`` 不依赖任何 IO 模块，因此可以单独测试。

待合并项（不是遗漏，是刻意的缝）
--------------------------------
``identity`` 是与 ``backend.registry.identity`` **同一份契约的两份实现**：本包的上游
基线 ``main`` 里还没有 ``backend`` 包，跨包 import 会让"registry 尚未合并"变成
"评测中心无法启动"。合并时应当由其中一份成为真源，另一份改为 import；
:meth:`identity.RequesterContext.from_foreign` 已经提供了鸭子类型适配，
因此接线本身不需要转换层。同理，``targets.TargetCatalog`` 的真实实现应当来自
``backend.registry``（deployment 目录），本包只消费它的形状。
"""

from __future__ import annotations

from .admission import (
    AdmissionDecision,
    AdmissionPolicy,
    GpuFacts,
    RunnerFacts,
    evaluate_admission,
)
from .authz import Project, ProjectDirectory
from .datasets import EVAL_TYPES, build_eval_spec
from .download import BoundedReportReader, ReportDownloadPlan, download_plan
from .dto import SCHEMA_VERSION
from .identity import (
    Principal,
    PrincipalDirectory,
    RequesterContext,
    TokenBinding,
    TokenDirectory,
    resolve_requester,
)
from .models import JOB_STATUSES, EvalJob, EvalResult
from .provenance import (
    KNOWN_REPORT_SOURCES,
    SOURCE_EVALSCOPE,
    SOURCE_LEGACY_BENCHMARK,
    ReportOwner,
    ReportRecord,
    ReportSourceSnapshot,
)
from .report_catalog import ReportCatalog, ReportEntry
from .report_readers import EvalScopeReportsReader, LegacyBenchmarkReader
from .report_seam import REPORT_ROUTE_SEAMS, EvaluationReportSeam
from .reports import ReportRoot
from .runner import MockRunner
from .service import EvaluationConfig, EvaluationService
from .store import Store
from .targets import TargetCatalog, TargetRecord

__all__ = [
    "EVAL_TYPES",
    "JOB_STATUSES",
    "KNOWN_REPORT_SOURCES",
    "REPORT_ROUTE_SEAMS",
    "SCHEMA_VERSION",
    "SOURCE_EVALSCOPE",
    "SOURCE_LEGACY_BENCHMARK",
    "AdmissionDecision",
    "AdmissionPolicy",
    "BoundedReportReader",
    "EvalJob",
    "EvalResult",
    "EvalScopeReportsReader",
    "EvaluationConfig",
    "EvaluationReportSeam",
    "EvaluationService",
    "GpuFacts",
    "LegacyBenchmarkReader",
    "MockRunner",
    "Principal",
    "PrincipalDirectory",
    "Project",
    "ProjectDirectory",
    "ReportCatalog",
    "ReportDownloadPlan",
    "ReportEntry",
    "ReportOwner",
    "ReportRecord",
    "ReportRoot",
    "ReportSourceSnapshot",
    "RequesterContext",
    "RunnerFacts",
    "Store",
    "TargetCatalog",
    "TargetRecord",
    "TokenBinding",
    "TokenDirectory",
    "build_eval_spec",
    "evaluate_admission",
    "resolve_requester",
]
