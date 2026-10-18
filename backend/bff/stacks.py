"""观测面与评测面的装配。**默认值全部是「本部署此刻诚实的取值」。**

这个模块存在的理由是把两件事固定在一处，好让它们可以被断言、也可以被审查：

* **真实派发是关的。** ``AdmissionPolicy.closed`` + ``GpuFacts.not_configured``
  （没读 GPU ⇒ 显存未知，不是 0）+ 未配置的报告根。准入是**多个条件的合取**，
  而这里把其中每一项都设成未满足。这不是「忘了打开」，是刻意的默认。
* **合成来源必须自称合成。** ``Provenance(source=..., synthetic=True)`` 会被观测
  模块原样带进每一份 DTO 的顶层 ``synthetic`` 与 ``display_hints``。

装配本身沿用各模块自己的构造器：``EvaluationService(...)`` 与
``ObservabilityStack`` 都只是把注入的目录、探针与时钟收起来，没有第二套实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from ..auth_linux.handoff import linux_user_id
from ..atlas.dto import Provenance as AtlasProvenance
from ..evaluation import admission as eval_admission
from ..evaluation import dto as eval_dto
from ..evaluation import identity as eval_identity
from ..evaluation import reports as eval_reports
from ..evaluation import service as eval_service
from ..evaluation import store as eval_store
from ..evaluation import targets as eval_targets
from ..evaluation.authz import Project, ProjectDirectory
from ..observability import engines as obs_engines
from ..observability import host as obs_host
from ..observability import host_projection
from ..observability.dto import Provenance
from ..observability.engines import EngineRegistry
from ..observability.identity import KeyDirectory
from .atlas_adapter import AtlasStack
from .evaluation_adapter import EvaluationStack
from .host_summary import HostSummaryStack
from .observability_adapter import ObservabilityStack

#: 观测侧的合成来源名。**不是**前端白名单里的那三个 —— 它必须看起来就是合成。
SYNTHETIC_OBSERVABILITY_SOURCE = "synthetic-fixture"
SYNTHETIC_EVALUATION_SOURCE = "backend.bff.synthetic"

#: 一个虚拟的 Gateway key → uid 绑定，用来演示「归属只来自受信目录」。
#: 本部署没有真实 Gateway，因此这是空目录的显式替代品，不是伪造的流量。
EMPTY_GATEWAY_KEYS: tuple = ()


def synthetic_provenance(source: str = SYNTHETIC_OBSERVABILITY_SOURCE) -> Provenance:
    """一份**自称合成**的来源声明。"""
    return Provenance(source=source, synthetic=True)


def observation_provenance(source: str) -> Provenance:
    """非合成声明。调用方必须真的接了对应来源才会走到这里。"""
    return Provenance(source=source, synthetic=False)


def synthetic_atlas_provenance(
    source: str = SYNTHETIC_OBSERVABILITY_SOURCE,
) -> AtlasProvenance:
    """Atlas 侧的一份**自称合成**的来源声明。

    Atlas 有自己的 ``Provenance`` 类型（字段同名、但归属不同模块），所以这里不能
    把观测那份直接传过去 —— 两个模块各自校验自己的类型，正是它们彼此不认识的
    表现。``source`` 默认取 ``synthetic-fixture``：Atlas 的
    ``_display_hints`` 会照它决定要不要打上合成标记，写 ``gateway-usage-export``
    是唯一能让它进 live 分支的取值，而本部署没有那个导出口。
    """
    return AtlasProvenance(source=source, synthetic=True)


# --------------------------------------------------------------------------
# 观测
# --------------------------------------------------------------------------


def build_observability_stack(
    *,
    directory: KeyDirectory,
    engine_registry: EngineRegistry,
    gateway_text: Callable[[object], str] = lambda _identity: "",
    provenance: Provenance | None = None,
    engine_probe: object | None = None,
    host_report: object | None = None,
    limits: object | None = None,
) -> ObservabilityStack:
    """装配观测数据面。

    ``gateway_text`` 默认返回空串：没有导出口就没有样本，读数一律
    ``unknown``/``no_sample``，**不是 0**。
    """
    return ObservabilityStack(
        directory=directory,
        engine_registry=engine_registry,
        provenance=provenance or synthetic_provenance(),
        gateway_text=gateway_text,
        engine_probe=engine_probe,
        host_report=host_report,
        limits=limits,
    )


def empty_key_directory() -> KeyDirectory:
    """没有 Gateway key 目录时的诚实取值：空目录，所有流量都归属不了。"""
    return KeyDirectory(EMPTY_GATEWAY_KEYS)


def engine_registry_from_records(
    records: Iterable[Mapping[str, object]],
) -> EngineRegistry:
    """从一组普通映射建观测侧的引擎表。"""
    return EngineRegistry(
        [
            obs_engines.EngineRecord(
                engine_id=str(record["engine_id"]),
                endpoint_id=str(record["endpoint_id"]),
                display_name=str(record.get("display_name") or record["engine_id"]),
                visibility=str(
                    record.get("visibility") or obs_engines.VISIBILITY_PRIVATE
                ),
                owner_uid=(
                    str(record["owner_uid"]) if record.get("owner_uid") else None
                ),
                replicas_declared=record.get("replicas_declared"),  # type: ignore[arg-type]
            )
            for record in records
        ]
    )


def unavailable_engine_probe() -> object:
    """没有探针时的默认：所有引擎状态 unknown，原因 source_unavailable。"""
    return obs_engines.UnavailableEngineProbe()


def null_host_reader() -> object:
    """没有 NVML 时的默认 GPU 读数器：一律 unsupported，绝不编造。"""
    return obs_host.NullGpuReader()


def synthetic_host_report(now=None):
    """一份**显式合成**的主机读数，用于开发/测试。

    它带 ``synthetic=True``，并且整卡显存是 host-wide 的共享量 —— 与任何用户的
    计费口径无关（观测模块的结构里就没有按人的显存字段）。
    """
    collector = obs_host.MockHostCollector(now=now)
    return collector.collect(now, synthetic=True)


# --------------------------------------------------------------------------
# 评测
# --------------------------------------------------------------------------


def principal_directories(
    *, host_id: str, users: Iterable[tuple[str, int]]
) -> tuple[eval_identity.PrincipalDirectory, eval_identity.TokenDirectory]:
    """评测侧的主体目录。主体名与 Registry/观测用同一个 ``linux:<host>:<uid>``。"""
    principals = eval_identity.PrincipalDirectory(
        [
            eval_identity.Principal(
                linux_user_id(host_id, uid), name, uid, display_name=name
            )
            for name, uid in users
        ]
    )
    return principals, eval_identity.TokenDirectory(())


def project_directory(
    *, projects: Iterable[tuple[str, str, Iterable[str]]], home: Iterable[tuple[str, str]]
) -> ProjectDirectory:
    return ProjectDirectory(
        [
            Project(project_id, display_name, frozenset(members))
            for project_id, display_name, members in projects
        ],
        home_projects=list(home),
    )


def default_evaluation_config(
    *, source: str = SYNTHETIC_EVALUATION_SOURCE, notes: Iterable[str] = ()
) -> eval_service.EvaluationConfig:
    """本部署的评测配置：**合成模式 + 派发关闭 + 不读 GPU + 无报告根**。"""
    return eval_service.EvaluationConfig(
        mode=eval_dto.MODE_SYNTHETIC,
        source=source,
        mock_runner_enabled=True,
        runner_kind="mock",
        # 真实派发永远是关的。准入是合取，这里让每一项都不满足。
        admission=eval_admission.AdmissionPolicy.closed(
            label="本部署不派发真实评测"
        ),
        gpu_facts=eval_admission.GpuFacts.not_configured(
            note="本部署不读 GPU：显存占用未知，不是 0。"
        ),
        report_root=eval_reports.unconfigured_root(),
        native_entry_available=False,
        audit_notes=tuple(notes) or ("SYNTHETIC: 目录、数据集与结果均为合成。",),
    )


def build_evaluation_stack(
    *,
    principals: eval_identity.PrincipalDirectory,
    projects: ProjectDirectory,
    targets: eval_targets.TargetCatalog,
    clock: Callable[[], object] | None = None,
    config: eval_service.EvaluationConfig | None = None,
    store: object | None = None,
    runner: object | None = None,
) -> EvaluationStack:
    """装配评测服务。runner 不注入时用模块自带的 ``MockRunner``（它自称合成）。"""
    kwargs: dict = {}
    if clock is not None:
        kwargs["clock"] = clock
        resolved_store = store if store is not None else eval_store.Store(":memory:", clock=clock)
    else:
        resolved_store = store if store is not None else eval_store.Store(":memory:")
    service = eval_service.EvaluationService(
        store=resolved_store,
        tokens=eval_identity.TokenDirectory(()),
        principals=principals,
        projects=projects,
        targets=targets,
        runner=runner,
        config=config or default_evaluation_config(),
        **kwargs,
    )
    return EvaluationStack(service=service)



# --------------------------------------------------------------------------
# 合成流量（**仅开发与测试**）
# --------------------------------------------------------------------------


def demo_gateway_corpus(
    directory: KeyDirectory,
    *,
    now,
    hours: int = 24,
    per_key: int = 4,
    endpoints: Iterable[str] = (),
    marker: str = "SYNTHETIC-BFF",
) -> str:
    """按受信 key 目录生成一批**合成** Gateway 行。

    与 ``dev/observability_preview.py:demo_rows`` 同样的做法与同样的理由：本部署
    没有真实导出口，而一个空窗口只会让所有读数都是 ``no_sample``——开发期看不到
    任何形态。所以这里造一批明确带 ``fixture`` 标记的行。

    归属**只**来自传入的 ``KeyDirectory``：行里不带 uid，也不带 ``x_principal``；
    观测模块的归属规则是 ``key_id -> uid``，这是唯一一条。

    ``endpoints`` 必须是**该会话有权使用**的 endpoint id：观测模块只统计引用了
    可见 endpoint 的行，给一个不可见的 endpoint 等于让样本在原地消失。调用方通常
    把当前会话的可见端点集传进来；为空时语料里的行会退化成不可归属，这是刻意的。
    """
    import json
    from datetime import timedelta, timezone

    rows: list[str] = []
    span = max(1, int(hours * 60))
    targets = [str(item) for item in endpoints]
    if not targets:
        return ""
    # 每个主体给**不同**的行数：这样「B 只看到自己的行」是可以被观察的，而不是
    # 因为恰好人人相等而看不出来。确定性来自 key 在目录里的位置，不是随机数。
    for position, uid in enumerate(directory.uids()):
        for key_id in directory.owned_by(uid):
            binding = directory.lookup(key_id)
            if binding is None or binding.revoked:
                continue
            rows_for_key = per_key + (position % 3)
            for index in range(rows_for_key):
                endpoint = targets[index % len(targets)]
                minutes_ago = int(span * (index + 1) / (rows_for_key + 1))
                moment = (now - timedelta(minutes=minutes_ago)).astimezone(
                    timezone.utc
                ).replace(microsecond=0)
                rows.append(
                    json.dumps(
                        {
                            "ts": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "key_id": key_id,
                            "fixture": marker,
                            "label": binding.workload or "synthetic-workload",
                            "method": "POST",
                            "path": "/v1/chat/completions",
                            "model": endpoint,
                            "status": 429 if index == rows_for_key - 1 else 200,
                            "outcome": "failed" if index == rows_for_key - 1 else "success",
                            "stream": True,
                            "queue_ms": 4 + index,
                            "ttft_ms": 110 + index * 20,
                            "upstream_ms": 900 + index * 100,
                            "total_ms": 900 + index * 100,
                            "bytes_out": 4096,
                            "usage": {"input_tokens": 100 + index, "output_tokens": 200 + index},
                        },
                        sort_keys=True,
                    )
                )
    return "\n".join(rows) + ("\n" if rows else "")



@dataclass(frozen=True)
class ObservabilityWiring:
    """观测侧接线需要的一小组对象，方便在 devserver 与测试间共享。"""

    stack: ObservabilityStack
    adapter_kwargs: Mapping[str, object]


# --------------------------------------------------------------------------
# Calls（Atlas）与主机投影
# --------------------------------------------------------------------------


def build_atlas_stack(
    *,
    directory,
    provenance: AtlasProvenance | None = None,
    gateway_text: Callable[[object], str] = lambda _identity: "",
    project_scope: Callable[[object], frozenset[str]] = lambda _identity: frozenset(),
    limits: object | None = None,
    gateway_attached: bool = False,
) -> AtlasStack:
    """装配 Calls 数据面。

    ``gateway_text`` 默认返回空串：没有导出口就没有样本，``/api/atlas/calls``
    的诚实结局是「这个窗口里没有可归属的调用」，**不是**一条编出来的记录。

    ``gateway_attached`` 默认 ``False``：要声称这次的数据来自已接线的真实
    Gateway，必须由装配方显式写下来。Atlas 自己的 ``_transport_block`` 也是这个
    口径（``live = gateway_attached and not synthetic``），本层照抄，不替它把默认
    翻过来。
    """
    return AtlasStack(
        directory=directory,
        provenance=provenance or synthetic_atlas_provenance(),
        gateway_text=gateway_text,
        project_scope=project_scope,
        limits=limits,
        gateway_attached=gateway_attached,
    )


#: 观测 Lane（#74 / PR #88）的零参读取器**还没有合进本分支的树**。
#:
#: 这条 seam 是 ``backend.observability.sources.host_summary_reader``；它在 #88 上。
#: 本 Lane 的分支叠在 #85 上，因此**不能**在 import 期假设它存在 —— 那会让「另一个
#: Lane 还没合入」表现成一次 ImportError 崩溃，而不是一次如实的 503。
REASON_HOST_SOURCE_NOT_DELIVERED = "host_source_reader_not_delivered"

#: Calls Lane（#77 / PR #90）的 ``AtlasSource`` 没有交过来。
REASON_CALLS_SOURCE_NOT_DELIVERED = "calls_source_not_delivered"


def _load_host_summary_reader():
    """Host Lane #74 交付的那条零参 seam。**未合入时抛 ``ImportError``。**

    单独一个函数，是为了让「这个分支的树里到底有没有它」可以被一条**确定性**的测试
    模拟出来：直接 ``from ... import`` 写在装配函数体里，测试就只能靠「本机当前装了
    什么」来碰运气 —— 而那正是「测试随环境变色」的开端。

    延迟 import 本身也是刻意的：一个 Lane 的合入顺序不该决定另一个 Lane 的模块
    能不能被 import。
    """
    from ..observability.sources import host_summary_reader

    return host_summary_reader


def build_host_summary_stack_from_observability(
    *,
    enabled: bool = False,
    base_url: str | None = None,
    max_sample_age_s: int = host_projection.DEFAULT_MAX_SAMPLE_AGE_S,
    future_tolerance_s: int = host_projection.DEFAULT_FUTURE_TOLERANCE_S,
) -> tuple[HostSummaryStack | None, str | None]:
    """把 Host Lane #74 的**零参**读取器接成 :class:`HostSummaryStack`。

    返回 ``(stack, reason)``。``stack is None`` 时 ``reason`` 说明是哪一种没接上：

    * ``host_source_reader_not_delivered`` —— 这个分支的树里还没有那条 seam
      （#88 尚未合入）。这是一个**已知的跨 Lane 依赖**，不是故障；
    * 读取器存在但 ``enabled=False`` —— 那是 Host Lane 自己的默认值：
      「本部署没有装配来源」优于「随便去读一个环回端口」。这种情况仍然返回一个
      stack，因为 stack 本身是装配好的；它每次 ``read()`` 回 ``None``，于是
      ``/api/infra/summary`` 如实以 503 收场。
    """
    try:
        host_summary_reader = _load_host_summary_reader()
    except ImportError:
        return None, REASON_HOST_SOURCE_NOT_DELIVERED

    kwargs: dict = {"enabled": enabled}
    if base_url is not None:
        kwargs["base_url"] = base_url
    read = host_summary_reader(**kwargs)
    return (
        build_host_summary_stack(
            read=read,
            max_sample_age_s=max_sample_age_s,
            future_tolerance_s=future_tolerance_s,
        ),
        None,
    )


def build_atlas_stack_from_source(
    source: object,
    *,
    project_scope: Callable[[object], frozenset[str]],
) -> tuple[AtlasStack | None, str | None]:
    """把 Calls Lane #77 的 ``AtlasSource`` 接成 :class:`AtlasStack`。

    返回 ``(stack, reason)``，形状与 :func:`build_host_summary_stack_from_observability`
    一致 —— 两个来源的装配失败应当能被同一段调用方代码处理。

    ``source`` 按**鸭子类型**读（``available`` / ``reason`` / ``directory`` /
    ``gateway_text`` / ``provenance`` / ``gateway_attached``），不 import
    ``backend.atlas.corpus``：那个模块在 #90 上，本 Lane 的分支不假设它在。这样
    「另一个 Lane 还没合入」是一个 ``None`` + 稳定 reason，不是一次 ImportError。

    ``project_scope`` **没有默认值**。一个空的授权项目集合会让 Atlas 的
    ``resolve_own_call_scope`` 拒绝每一次调用 —— 那是 fail-closed，但对一个只是
    忘了传参数的装配方来说，它表现成「这个人从来没有调用过」。既然 ProjectDirectory
    的权威在装配方手里，就把这件事逼到签名上。

    ``source.available is False`` 时**整份**来源不接：Atlas 的 ``AtlasSource``
    刻意不给半套东西（只给目录不给语料，会让每一行都归不了属、渲染成一份看起来
    正常的空账本）。这里沿用同一个决定。
    """
    if source is None:
        return None, REASON_CALLS_SOURCE_NOT_DELIVERED
    if not bool(getattr(source, "available", False)):
        return None, str(getattr(source, "reason", None) or REASON_CALLS_SOURCE_NOT_DELIVERED)

    return (
        AtlasStack(
            directory=getattr(source, "directory", None),
            provenance=getattr(source, "provenance", None),
            gateway_text=getattr(source, "gateway_text", None),
            project_scope=project_scope,
            gateway_attached=bool(getattr(source, "gateway_attached", False)),
        ),
        None,
    )


def build_host_summary_stack(
    *,
    read: Callable[[], object] = lambda: None,
    max_sample_age_s: int = host_projection.DEFAULT_MAX_SAMPLE_AGE_S,
    future_tolerance_s: int = host_projection.DEFAULT_FUTURE_TOLERANCE_S,
) -> HostSummaryStack:
    """装配主机投影来源。

    ``read`` 默认返回 ``None`` —— 「这次没读到」。那是本模块**唯一**诚实的默认：
    一个默认返回合成投影的实现，会让每一台没接投影的部署都显示一份看起来很正常的
    主机读数，而 ``validate_host_summary`` 恰恰会在 ``synthetic is not False`` 上
    拒绝它。生产里传进来的应当是一个**已经有刷新周期**的读取器
    （``backend.observability.host_refresh``），而不是一个裸的 HTTP 客户端 ——
    缓存策略是部署决策，本层不自己缓存。
    """
    return HostSummaryStack(
        read=read,
        max_sample_age_s=max_sample_age_s,
        future_tolerance_s=future_tolerance_s,
    )


__all__ = [
    "EMPTY_GATEWAY_KEYS",
    "SYNTHETIC_EVALUATION_SOURCE",
    "SYNTHETIC_OBSERVABILITY_SOURCE",
    "ObservabilityWiring",
    "REASON_CALLS_SOURCE_NOT_DELIVERED",
    "REASON_HOST_SOURCE_NOT_DELIVERED",
    "build_atlas_stack",
    "build_atlas_stack_from_source",
    "build_evaluation_stack",
    "build_host_summary_stack",
    "build_host_summary_stack_from_observability",
    "build_observability_stack",
    "default_evaluation_config",
    "demo_gateway_corpus",
    "empty_key_directory",
    "engine_registry_from_records",
    "null_host_reader",
    "observation_provenance",
    "principal_directories",
    "project_directory",
    "synthetic_atlas_provenance",
    "synthetic_host_report",
    "synthetic_provenance",
    "unavailable_engine_probe",
]
